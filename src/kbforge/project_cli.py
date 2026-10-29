"""Project mode: the commands that read a `kbforge.yaml`.

Each source becomes exactly the arguments flag mode builds from its flags, so
the pipeline sees nothing new (architecture.md §7.5). What this layer adds is
the order (file order), the validation of the whole project before anything
runs, the lock held across every source, and `sources.json`."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy
import yaml
from pydantic import ValidationError

from kbforge.assembly import (
    SYNTHESIZERS,
    Outcome,
    SetupError,
    build_synthesizer,
    check_publisher,
    connectors_of,
    execute,
    load_grounding_value,
    load_links_value,
    publishers_of,
    synthesizer_grounds,
)
from kbforge.chunking import ChunkingConfig, ChunkRecord, ChunkRecordError, read_record
from kbforge.grounding import GroundingConfig, problems_for
from kbforge.links import LinksConfig, links_problems
from kbforge.lock import MirrorLocked, mirror_lock
from kbforge.pipeline import (
    ConfigError,
    ConnectorProtocol,
    NoOp,
    Planned,
    Published,
    PublisherProtocol,
    Waiting,
    _chunk_slot,
    _open_request_hook,
    instance_key,
    plan,
    redo,
    run,
)
from kbforge.project import DEFAULT_FILE, Component, Project, ProjectError
from kbforge.publishers._http import PublishError
from kbforge.registry import build_registry
from kbforge.reset import (
    ResetPlan,
    apply_reset,
    chunk_slot,
    cursor_slot,
    plan_reset,
    state_slots,
    systems_for,
)
from kbforge.sources_record import SourceEntry, read_sources, write_sources
from kbforge.synthesize import Synthesizer

_LOAD_ERRORS = (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError)
"""The four operator mistakes about a named file, as flag mode catches them."""


@dataclass
class ResolvedSource:
    """One source, as the arguments flag mode would have built for it."""

    name: str
    connector_name: str
    connector: ConnectorProtocol
    publisher: PublisherProtocol
    config: dict
    publish_config: dict
    synthesizer: Synthesizer | None
    """None for `stub`, and always under `for_plan` (plan never builds one)."""
    grounds: bool
    chunking: ChunkingConfig | None
    grounding_config: GroundingConfig
    links_config: LinksConfig | None
    """Project-level, so every source shares the one object (§7.5)."""
    publisher_problems: list[str] = field(default_factory=list)
    """Only under `for_plan`: the publisher's config problems, which `plan`
    reports as "open request unknown" instead of failing on (otherwise they
    are fatal and `resolve` raises)."""


def _settings(component: Component) -> dict[str, Any]:
    return dict(component.model_extra or {})


def _publish_config(project: Project, component: Component) -> dict[str, Any]:
    config = _settings(component)
    if component.name == "dry-run":
        # As flag mode wires dry-run to --out.
        config.setdefault("out_dir", str(project.out()))
    return config


def _check_synthesizer_shape(name: str, settings: dict) -> list[str]:
    """`LLMConfig`/`DescribeConfig` accept these settings — the shape only:
    no `validate_env()`, no model import (what `plan` may check)."""
    if name not in ("llm", "describe"):
        return []
    from kbforge.llm_synthesizer import DescribeConfig, LLMConfig

    config_cls = DescribeConfig if name == "describe" else LLMConfig
    try:
        config_cls(**settings)
    except (ValueError, TypeError) as exc:
        return [str(exc)]
    return []


def resolve(
    project: Project,
    registry: pluggy.PluginManager,
    names: list[str],
    *,
    for_plan: bool,
) -> list[ResolvedSource]:
    """The selected sources (all when `names` is empty), in file order, or
    `ProjectError` carrying every problem found. Relative paths in
    connector config are read against the cwd, so the caller has already
    `chdir`ed to `project.root`."""
    connectors = connectors_of(registry)
    publishers = publishers_of(registry)
    problems: list[str] = []

    unknown = [n for n in names if n not in project.sources]
    if unknown:
        known = ", ".join(project.sources)
        problems += [f"unknown source {n!r}; known: {known}" for n in unknown]
    selected = [n for n in project.sources if not names or n in names]

    # A component shared by several sources (the project-level default) is
    # checked and built once, and its problems are reported once: under its
    # top-level key, or the source's name for a per-source override.
    synthesizers: dict[int, Synthesizer | None] = {}
    checked_publishers: set[int] = set()

    def synthesizer_for(component: Component, label: str) -> Synthesizer | None:
        if id(component) in synthesizers:
            return synthesizers[id(component)]
        name, settings = component.name, _settings(component)
        built: Synthesizer | None = None
        found: list[str] = []
        if name not in SYNTHESIZERS:
            available = ", ".join(SYNTHESIZERS)
            found = [f"unknown synthesizer {name!r}; available: {available}"]
        elif name == "stub" and settings:
            found = [f"synthesizer 'stub' takes no settings: {', '.join(settings)}"]
        elif for_plan:
            found = _check_synthesizer_shape(name, settings)
        else:
            try:
                built = build_synthesizer(name, settings, project.mirror())
            except SetupError as exc:
                found = [str(exc)]
        problems.extend(f"{label}{p}" for p in found)
        synthesizers[id(component)] = built
        return built

    resolved: list[ResolvedSource] = []
    for name in selected:
        spec = project.sources[name]
        connector = connectors.get(spec.connector)
        if connector is None:
            available = ", ".join(sorted(connectors)) or "(none)"
            problems.append(
                f"{name}: unknown connector {spec.connector!r}; available: {available}"
            )
        else:
            problems += [
                f"{name}: {p}" for p in connector.kbforge_validate_config(spec.config)
            ]

        pub_component = spec.publisher or project.publisher
        pub_label = f"{name}: " if spec.publisher else "publisher: "
        publisher = publishers.get(pub_component.name)
        publish_config = _publish_config(project, pub_component)
        pub_problems: list[str] = []
        if publisher is None:
            available = ", ".join(sorted(publishers)) or "(none)"
            problems.append(
                f"{pub_label}unknown publisher {pub_component.name!r}; "
                f"available: {available}"
            )
        else:
            pub_problems = check_publisher(
                publisher, pub_component.name, publish_config
            )
            if not for_plan and id(pub_component) not in checked_publishers:
                problems += [f"{pub_label}{p}" for p in pub_problems]
            checked_publishers.add(id(pub_component))

        synth_component = spec.synthesizer or project.synthesizer
        synthesizer = synthesizer_for(
            synth_component, f"{name}: " if spec.synthesizer else "synthesizer: "
        )

        if connector is None or publisher is None:
            continue
        resolved.append(
            ResolvedSource(
                name=name,
                connector_name=spec.connector,
                connector=connector,
                publisher=publisher,
                config=spec.config,
                publish_config=publish_config,
                synthesizer=synthesizer,
                grounds=synthesizer_grounds(synth_component.name),
                chunking=spec.chunking,
                grounding_config=GroundingConfig(),
                links_config=None,
                publisher_problems=pub_problems if for_plan else [],
            )
        )

    grounding = GroundingConfig()
    try:
        grounding = load_grounding_value(project.grounding)
    except _LOAD_ERRORS as exc:
        problems.append(f"grounding config {project.grounding}: {exc}")
    else:
        if shape := problems_for(grounding):
            problems.append(f"grounding config: {'; '.join(shape)}")

    links: LinksConfig | None = None
    try:
        links = load_links_value(project.links)
    except _LOAD_ERRORS as exc:
        problems.append(f"links config {project.links}: {exc}")
    else:
        if links is not None and (shape := links_problems(links)):
            problems.append(f"links config: {'; '.join(shape)}")

    if problems:
        raise ProjectError(problems)
    for rs in resolved:
        rs.grounding_config = grounding
        rs.links_config = links
    return resolved


@dataclass
class StatusSource:
    """What `status` needs of a source: the connector's name and config, which
    name its state slots, and the publisher it asks about open requests."""

    name: str
    connector_name: str
    config: dict
    publisher: PublisherProtocol
    publish_config: dict
    publisher_problems: list[str]
    """Reported as "open request unknown", never fatal."""


def resolve_status(
    project: Project, registry: pluggy.PluginManager
) -> list[StatusSource]:
    """Every source, for `status`, or `ProjectError` for an unknown publisher.

    Lighter than `resolve` on purpose: no connector plugin lookup, no
    `kbforge_validate_config`, no synthesizer, links or grounding. `status`
    reads local state and asks the forge, so it must answer where a run could
    not: a source's directory gone, its credentials unset, its plugin not
    installed here."""
    publishers = publishers_of(registry)
    problems: list[str] = []
    found: list[StatusSource] = []
    for name, spec in project.sources.items():
        pub_component = spec.publisher or project.publisher
        publisher = publishers.get(pub_component.name)
        if publisher is None:
            available = ", ".join(sorted(publishers)) or "(none)"
            label = f"{name}: " if spec.publisher else "publisher: "
            problem = (
                f"{label}unknown publisher {pub_component.name!r}; "
                f"available: {available}"
            )
            if problem not in problems:
                problems.append(problem)
            continue
        publish_config = _publish_config(project, pub_component)
        found.append(
            StatusSource(
                name=name,
                connector_name=spec.connector,
                config=spec.config,
                publisher=publisher,
                publish_config=publish_config,
                publisher_problems=check_publisher(
                    publisher, pub_component.name, publish_config
                ),
            )
        )
    if problems:
        raise ProjectError(problems)
    return found


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _locked(project: Project, body: Callable[[], int]) -> int:
    """`body` from beside the project file, under the mirror lock."""
    previous = Path.cwd()
    # Connector config is never rewritten (it is hashed into the slot names),
    # so `path: ./handbook` must be read from beside the project file.
    os.chdir(project.root)
    try:
        try:
            with mirror_lock(project.mirror()):
                return body()
        except MirrorLocked as exc:
            print(
                f"Locked: another kbforge run holds {exc.path}; "
                "nothing was read or written."
            )
            return 3
    finally:
        os.chdir(previous)


def _load(
    project: Project, names: list[str], *, for_plan: bool
) -> tuple[list[ResolvedSource], dict[str, SourceEntry]] | int:
    """The selected sources and the recorded entries, or the exit code (2)
    after printing what was wrong."""
    try:
        sources = resolve(project, build_registry(), names, for_plan=for_plan)
    except ProjectError as exc:
        print(*exc.problems, sep="\n")
        return 2
    try:
        entries = read_sources(project.state())
    except (OSError, ValueError) as exc:
        print(f"{project.state() / 'sources.json'}: {exc}")
        return 2
    return sources, entries


def _owners(
    project: Project, entries: dict[str, SourceEntry], name: str
) -> dict[str, str]:
    """system -> source that owns it, for every source but `name`.

    Ownership counts only names still in the file: a renamed source keeps its
    config, and so its cursor, and must not be blocked by its old name."""
    return {
        system: other
        for other, entry in entries.items()
        if other != name and other in project.sources
        for system in entry.systems
    }


def _execute(call: Callable[[], Any], connector_name: str) -> Outcome:
    """`execute`, and also any other exception a plugin raises (a
    connector's own error type, say), reported as one line, exit 1. Flag mode
    runs one source and keeps the traceback; here one plugin's exception must
    not stop the sources after it (§7.5)."""
    try:
        return execute(call, connector_name)
    except Exception as exc:
        return Outcome(1, [f"error: {type(exc).__name__}: {exc}"])


def cmd_run(project: Project, names: list[str]) -> int:
    """Every selected source in file order, under one lock."""
    return _locked(project, lambda: _run_sources(project, names))


def cmd_plan(project: Project, names: list[str]) -> int:
    """What each selected source would propose. Writes nothing; takes
    the lock because it reads the mirror."""
    return _locked(project, lambda: _plan_sources(project, names))


def _plan_sources(project: Project, names: list[str]) -> int:
    loaded = _load(project, names, for_plan=True)
    if isinstance(loaded, int):
        return loaded
    sources, entries = loaded
    # Systems claimed earlier in this invocation, in memory only: plan never
    # writes sources.json.
    claimed: dict[str, list[str]] = {}
    width = max(len(s.name) for s in sources)
    code = 0
    for rs in sources:
        prefix = f"{rs.name:<{width}}  "
        owners = _owners(project, entries, rs.name)
        for other, systems in claimed.items():
            for system in systems:
                owners.setdefault(system, other)
        if rs.publisher_problems:
            print(
                f"{prefix}note: open request unknown "
                f"({'; '.join(rs.publisher_problems)}); a run may wait instead"
            )
        outcome = _execute(
            lambda rs=rs, owners=owners: plan(
                rs.connector,
                rs.publisher,
                config=rs.config,
                mirror=str(project.mirror()),
                state_dir=str(project.state()),
                publish_config=rs.publish_config,
                grounds=rs.grounds,
                grounding_config=rs.grounding_config,
                chunking=rs.chunking,
                links_config=rs.links_config,
                foreign_systems=frozenset(owners),
                owners=owners,
                check_open_requests=not rs.publisher_problems,
            ),
            rs.connector_name,
        )
        result = outcome.result
        if isinstance(result, NoOp):
            lines = ["no change → NoOp"]
        elif isinstance(result, Waiting):
            lines = [f"review request {result.request} open → would wait"]
        elif isinstance(result, Planned):
            lines = _planned_lines(result)
        else:
            lines = outcome.lines
            if outcome.code != 0:
                code = 1
        for line in lines:
            print(f"{prefix}{line}")
        if isinstance(result, (NoOp, Planned)):
            claimed[rs.name] = sorted(result.systems)
    return code


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _planned_lines(p: Planned) -> list[str]:
    # One document, one line, counted once, under its first kind below: a
    # drifted document a removal or an arrival also rebuilds is in both its
    # drift list and `rebuilt`.
    seen: set[str] = set()
    kinds: list[tuple[str, list[str], str]] = []
    for mark, ids, what in (
        ("+", p.added, "added"),
        ("~", p.modified, "modified"),
        ("-", p.removed, "removed"),
        ("↻", p.grounding_drift, "grounding drift"),
        ("↻", p.link_drift, "link drift"),
        ("↻", p.rebuilt, "rebuilt for links"),
    ):
        fresh = [doc_id for doc_id in sorted(set(ids)) if doc_id not in seen]
        seen.update(fresh)
        kinds.append((mark, fresh, what))
    added, modified, removed, grounding, linked, rebuilt = (k[1] for k in kinds)
    drifted = len(grounding) + len(linked)
    proposed = len(added) + len(modified) + drifted + len(rebuilt)
    # A removal is part of the proposal too (the request deletes the
    # concept), so a deletion-only plan does not read as proposing nothing.
    parts = [_count(proposed, "concept")] if proposed or not removed else []
    if removed:
        parts.append(_count(len(removed), "removal"))
    head = (
        f"{len(added)} added, {len(modified)} modified, {len(removed)} removed, "
        f"{drifted} drifted, {len(rebuilt)} rebuilt → would propose "
        + " and ".join(parts)
    )
    if p.deferred:
        head += f"; {p.deferred} more follow in later chunks"
    head += " (validation runs at publish)"
    lines = [head]
    for mark, ids, what in kinds:
        lines += [f"  {mark} {doc_id}  {what}" for doc_id in ids]
    return lines


def _run_sources(project: Project, names: list[str]) -> int:
    loaded = _load(project, names, for_plan=False)
    if isinstance(loaded, int):
        return loaded
    sources, entries = loaded

    grounding = sources[0].grounding_config if sources else None
    if (
        grounding is not None
        and grounding.rules
        and not any(s.grounds for s in sources)
    ):
        print(
            "grounding rules are validated but inactive: no selected source's "
            "synthesizer grounds; use synthesizer llm"
        )

    width = max(len(s.name) for s in sources)
    code = 0
    for rs in sources:
        prefix = f"{rs.name:<{width}}  "
        digest = instance_key(rs.config)
        old = entries.get(rs.name)
        changed = old is not None and (
            old.digest != digest or old.connector != rs.connector_name
        )
        if changed:
            # Not a pointer to `reset`: by the time a reader acts on this,
            # the run has recorded the new digest, so `reset` would find the
            # new slots and wipe this source's mirror documents.
            print(
                f"{prefix}note: config changed since the last run; starting from a "
                "fresh cursor (the old one stays in state/, unused)."
            )

        # `entries` is updated as each source finishes, so the systems an
        # earlier source fetched in this same invocation are already in it.
        owners = _owners(project, entries, rs.name)
        foreign = frozenset(owners)

        outcome = _execute(
            lambda rs=rs, foreign=foreign, owners=owners: run(
                rs.connector,
                rs.publisher,
                config=rs.config,
                mirror=str(project.mirror()),
                state_dir=str(project.state()),
                publish_config=rs.publish_config,
                synthesizer=rs.synthesizer,
                grounding_config=rs.grounding_config,
                chunking=rs.chunking,
                links_config=rs.links_config,
                foreign_systems=foreign,
                owners=owners,
            ),
            rs.connector_name,
        )
        for line in outcome.lines:
            print(f"{prefix}{line}")
        if outcome.code != 0:
            code = 1

        result = outcome.result
        if isinstance(result, (NoOp, Published)):
            systems = sorted(result.systems) or (old.systems if old else [])
            entries[rs.name] = SourceEntry(
                connector=rs.connector_name,
                digest=digest,
                systems=systems,
                last_published=(
                    _now()
                    if isinstance(result, Published)
                    else (old.last_published if old else None)
                ),
            )
            write_sources(project.state(), entries)
    return code


def cmd_status(project: Project) -> int:
    """Per source, from local state plus one read-only forge call where
    the publisher supports it. No lock and no fetch: it only reads."""
    previous = Path.cwd()
    os.chdir(project.root)  # publisher config is read as a run would read it
    try:
        return _status(project)
    finally:
        os.chdir(previous)


def _utc(stamp: str) -> str:
    return datetime.fromisoformat(stamp).astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _read_chunk(slot: Path) -> tuple[ChunkRecord | None, str | None]:
    try:
        return read_record(slot), None
    except ChunkRecordError as exc:
        return None, f"chunk record unreadable ({exc.error})"


def _find_open(
    rs: StatusSource, systems: list[str], hints: list[str], config: dict
) -> tuple[str | None, str | None]:
    """`(request, None)`, or `(None, reason)` if the forge could not say."""
    hook = _open_request_hook(rs.publisher, "status checks open review requests")
    branches = dict.fromkeys([*(f"sync/{s}" for s in systems), *hints])
    try:
        for branch in branches:
            if (request := hook(branch, config)) is not None:
                return request, None
    except PublishError as exc:
        return None, f"open request unknown ({exc})"
    return None, None


def _status(project: Project) -> int:
    try:
        sources = resolve_status(project, build_registry())
    except ProjectError as exc:
        print(*exc.problems, sep="\n")
        return 2
    try:
        entries = read_sources(project.state())
    except (OSError, ValueError) as exc:
        print(f"{project.state() / 'sources.json'}: {exc}")
        return 2
    stale = [name for name in entries if name not in project.sources]
    width = max(len(n) for n in [*(s.name for s in sources), *stale])
    state = project.state()

    for rs in sources:
        entry = entries.get(rs.name)
        slot = _chunk_slot(state, rs.connector_name, rs.config)
        record, bad = _read_chunk(slot)
        parts = _published_parts(entry, slot)
        request: str | None = None
        try:
            _open_request_hook(rs.publisher, "status checks open review requests")
        except ConfigError:
            pass  # no real hook: nothing to ask the forge
        else:
            if entry is None and record is None and not bad:
                pass  # never run: nothing could be open
            elif rs.publisher_problems:
                parts.append(
                    f"open request unknown ({'; '.join(rs.publisher_problems)})"
                )
            else:
                request, unknown = _find_open(
                    rs,
                    entry.systems if entry else [],
                    record.branch_hints if record else [],
                    rs.publish_config,
                )
                parts.append(
                    unknown
                    or (
                        f"review request {request} open"
                        if request
                        else "no open request"
                    )
                )
        parts += [bad] if bad else _chunk_parts(record, request)
        print(f"{rs.name:<{width}}  " + " · ".join(parts))

    for name in stale:
        entry = entries[name]
        slot = state / f"chunk-{entry.connector}-{entry.digest}.json"
        record, bad = _read_chunk(slot)
        parts = _published_parts(entry, slot)
        if bad:
            parts.append(bad)
        elif record is not None and record.pending:
            parts.append(f"chunk of {len(record.admitted)} pending")
        parts.append(f"not in kbforge.yaml (kbforge reset {name} clears its state)")
        print(f"{name:<{width}}  " + " · ".join(parts))
    return 0


def _published_parts(entry: SourceEntry | None, slot: Path) -> list[str]:
    if entry is None:
        return ["never published"] if slot.exists() else ["never run"]
    if entry.last_published is None:
        return ["never published"]
    return [f"last published {_utc(entry.last_published)}"]


def _chunk_parts(record: ChunkRecord | None, request: str | None) -> list[str]:
    if record is None or not record.pending:
        return []
    if request is not None:
        return [f"chunk of {len(record.admitted)} open as {request}, more to follow"]
    return ["next chunk proposes on the next run"]


def cmd_redo(project: Project, name: str) -> int:
    """Today's `redo`, for one source of the file, under the lock."""
    return _locked(project, lambda: _redo(project, name))


def _redo(project: Project, name: str) -> int:
    # `for_plan`: redo never synthesizes, so it must not need model
    # credentials. It does ask the forge whether a request is open, so a
    # publisher problem is still fatal.
    loaded = _load(project, [name], for_plan=True)
    if isinstance(loaded, int):
        return loaded
    [rs], _ = loaded
    if rs.publisher_problems:
        print(*(f"{rs.name}: {p}" for p in rs.publisher_problems), sep="\n")
        return 2
    outcome = execute(
        lambda: redo(
            rs.connector,
            rs.publisher,
            config=rs.config,
            mirror=str(project.mirror()),
            state_dir=str(project.state()),
            publish_config=rs.publish_config,
        ),
        rs.connector_name,
    )
    for line in outcome.lines:
        print(f"{rs.name}  {line}")
    return outcome.code


def cmd_reset(project: Project, name: str, *, yes: bool) -> int:
    """Forget one source's local state. Works for a name still in the
    file and for a stale `sources.json` entry (its connector and digest name
    the slots). Never contacts the forge; takes the lock."""
    return _locked(project, lambda: _reset(project, name, yes=yes))


def _reset(project: Project, name: str, *, yes: bool) -> int:
    state, mirror = project.state(), project.mirror()
    try:
        entries = read_sources(state)
    except (OSError, ValueError) as exc:
        print(f"{state / 'sources.json'}: {exc}")
        return 2
    spec = project.sources.get(name)
    entry = entries.get(name)
    if spec is None and entry is None:
        known = ", ".join(dict.fromkeys([*project.sources, *entries]))
        print(f"unknown source {name!r}; known: {known}")
        return 2

    # The slots `name` may own: the current config's (if the name is in the
    # file) and the one it last ran with, which differ after a config change.
    pairs: list[tuple[str, str]] = []
    if spec is not None:
        pairs.append((spec.connector, instance_key(spec.config)))
    if entry is not None:
        pairs.append((entry.connector, entry.digest))
    pairs = list(dict.fromkeys(pairs))

    # What another source owns, which resetting `name` must never touch. Stale
    # names own nothing (as in `_owners`); a source in the file owns the
    # systems its entry records and every slot it resolves to now or last ran
    # with. A renamed source shares its old name's digest and systems, and a
    # system can pass to a new source once its old one leaves the file.
    owned = _owners(project, entries, name)
    taken: dict[tuple[str, str], str] = {}
    for other, other_spec in project.sources.items():
        if other == name:
            continue
        taken.setdefault((other_spec.connector, instance_key(other_spec.config)), other)
        if (other_entry := entries.get(other)) is not None:
            taken.setdefault((other_entry.connector, other_entry.digest), other)
    mine = [pair for pair in pairs if pair not in taken]
    # Systems go with the slot they were fetched through, so a taken slot's
    # systems are its taker's even before the taker has run (a rename has no
    # entry yet): the cursor's stamped systems, the chunk record's admitted
    # prefixes, and those of any entry, stale or not, that records the slot.
    # Wiping their mirror documents would leave that cursor with no mirror
    # behind it.
    for (c, d), other in taken.items():
        slot_systems = set(
            systems_for(None, cursor_slot(state, c, d), chunk_slot(state, c, d))
        )
        slot_systems.update(
            system
            for recorded in entries.values()
            if (recorded.connector, recorded.digest) == (c, d)
            for system in recorded.systems
        )
        for system in sorted(slot_systems):
            owned.setdefault(system, other)

    if entry is not None and entry.systems:
        found = frozenset(entry.systems)
    else:
        found = frozenset().union(
            *(
                systems_for(None, cursor_slot(state, c, d), chunk_slot(state, c, d))
                for c, d in mine
            )
        )
    systems = frozenset(found - owned.keys())

    by_connector: dict[str, set[str]] = {}
    for c, d in mine:
        by_connector.setdefault(c, set()).add(d)
    plans = [
        plan_reset(mirror, state, connector=c, digests=ds, systems=systems)
        for c, ds in by_connector.items()
    ] or [plan_reset(mirror, state, connector="", digests=set(), systems=systems)]
    rp = ResetPlan(
        state_files=[f for plan_ in plans for f in plan_.state_files],
        doc_ids=plans[0].doc_ids,
        systems=systems,
    )

    if not rp.state_files and not rp.doc_ids and entry is None:
        print(f"{name}: nothing to reset")
        return 0
    for system in sorted(found & owned.keys()):
        print(
            f"{name}: system {system!r} belongs to source {owned[system]!r}; "
            "its documents were left in place"
        )
    for pair in pairs:
        if pair in taken and state_slots(state, pair[0], {pair[1]}):
            print(
                f"{name}: state slots shared with source {taken[pair]!r} "
                "were left in place"
            )
    if not found:
        print(f"{name}: systems unknown: mirror left as is")
    if not yes:
        prompt = (
            f"Reset {name}: remove {len(rp.doc_ids)} mirror document(s) and "
            f"{len(rp.state_files)} state file(s)? [y/N] "
        )
        try:
            answer = input(prompt)
        except EOFError:
            print()  # end the prompt's line
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Reset cancelled.")
            return 0

    apply_reset(mirror, rp)
    if entry is not None:
        # Last: an interrupted reset keeps the entry that names its systems.
        del entries[name]
        write_sources(state, entries)
    print(
        f"{name}: removed {len(rp.doc_ids)} mirror document(s) and "
        f"{len(rp.state_files)} state file(s)"
    )
    return 0


# `kbforge init`. `{docs_path}` is the only placeholder; literal braces are
# doubled. The test finds the examples as a `# synthesizer:` / `# publisher:`
# line plus the `#   ` lines under it, so keep that shape.
INIT_TEMPLATE = """\
# kbforge.yaml: one knowledge base. `kbforge plan` shows what a run would do.
# Relative paths below are relative to this file.

# Offline defaults: no model, no forge, no credentials.
synthesizer: {{ name: stub }}
publisher: {{ name: dry-run }}

# To write with a model (set OPENROUTER_API_KEY, or add `api_base:` for a gateway):
# synthesizer:
#   name: llm
#   model: deepseek/deepseek-v4-flash

# To open review requests on GitHub (kbforge never merges them):
# publisher:
#   name: github
#   repo: acme/knowledge-base
#   token_env: GITHUB_TOKEN

# Sources run in this order, so list a source others ground on first.
# Only one local_files source works per project: point it at a common parent.
sources:
  docs:
    connector: local_files
    config:
      path: {docs_path}
"""


def cmd_init(target: Path, docs_path: str) -> int:
    """Write the starter project to `target`; never overwrite one."""
    scalar = yaml.dump(docs_path, width=10**6).strip().removesuffix("...").strip()
    try:
        with target.open("x", encoding="utf-8") as f:
            f.write(INIT_TEMPLATE.format(docs_path=scalar))
    except FileExistsError:
        print(f"{target.name} already exists; not overwritten")
        return 2
    except OSError as exc:
        print(f"{target}: {exc}")
        return 2
    flag = (
        ""
        if target.name == DEFAULT_FILE and target.parent == Path()
        else f" --project {target}"
    )
    print(f"wrote {target.name}")
    # Read as `plan` will read it: relative to the project file.
    if not (target.parent / docs_path).exists():
        print(
            f"note: {docs_path} does not exist yet; "
            "create it or edit sources.docs.config.path"
        )
    print(f"next: kbforge plan{flag}")
    print("keep .kbforge/ between runs: it holds the mirror that makes re-runs free")
    return 0
