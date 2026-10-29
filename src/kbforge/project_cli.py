"""Project mode: the commands that read a `kbforge.yaml`.

Each source becomes exactly the arguments flag mode builds from its flags, so
the pipeline sees nothing new (project-file design §6). What this layer adds is
the order (file order), the validation of the whole project before anything
runs (§6.1), the lock held across every source, and `sources.json`."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pluggy
import yaml
from pydantic import ValidationError

from kbforge.assembly import (
    SYNTHESIZERS,
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
from kbforge.chunking import ChunkingConfig
from kbforge.grounding import GroundingConfig, problems_for
from kbforge.links import LinksConfig, links_problems
from kbforge.lock import MirrorLocked, mirror_lock
from kbforge.pipeline import (
    ConnectorProtocol,
    NoOp,
    Published,
    PublisherProtocol,
    instance_key,
    run,
)
from kbforge.project import Component, Project, ProjectError
from kbforge.registry import build_registry
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
    """Project-level, so every source shares the one object (§5)."""
    publisher_problems: list[str] = field(default_factory=list)
    """Only under `for_plan`: the publisher's config problems, which `plan`
    reports as "open request unknown" instead of failing on (otherwise they
    are fatal and `resolve` raises)."""


def _settings(component: Component) -> dict[str, Any]:
    return dict(component.model_extra or {})


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
    `ProjectError` carrying every problem found (§6.1). Relative paths in
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
        publish_config = _settings(pub_component)
        if pub_component.name == "dry-run":
            # As flag mode wires dry-run to --out (spec §3).
            publish_config.setdefault("out_dir", str(project.out()))
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


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cmd_run(project: Project, names: list[str]) -> int:
    """§6.2: every selected source in file order, under one lock."""
    previous = Path.cwd()
    # Connector config is never rewritten (it is hashed into the slot names),
    # so `path: ./handbook` must be read from beside the project file.
    os.chdir(project.root)
    try:
        try:
            with mirror_lock(project.mirror()):
                return _run_sources(project, names)
        except MirrorLocked as exc:
            print(
                f"Locked: another kbforge run holds {exc.path}; "
                "nothing was read or written."
            )
            return 3
    finally:
        os.chdir(previous)


def _run_sources(project: Project, names: list[str]) -> int:
    try:
        sources = resolve(project, build_registry(), names, for_plan=False)
    except ProjectError as exc:
        print(*exc.problems, sep="\n")
        return 2
    try:
        entries = read_sources(project.state())
    except (OSError, ValueError) as exc:
        print(f"{project.state() / 'sources.json'}: {exc}")
        return 2

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
    # Systems claimed earlier in this invocation, by source name.
    claimed: dict[str, str] = {}
    code = 0
    for rs in sources:
        prefix = f"{rs.name:<{width}}  "
        digest = instance_key(rs.config)
        old = entries.get(rs.name)
        if old is not None and (old.digest, old.connector) != (
            digest,
            rs.connector_name,
        ):
            print(
                f"{prefix}note: config changed since the last run; starting from a "
                f"fresh cursor (kbforge reset {rs.name} clears the old state)."
            )

        # Ownership counts only names still in the file: a renamed source keeps
        # its config, and so its cursor, and must not be blocked by its old name.
        owners: dict[str, str] = {
            system: other
            for other, entry in entries.items()
            if other != rs.name and other in project.sources
            for system in entry.systems
        }
        owners.update({s: o for s, o in claimed.items() if o != rs.name})
        foreign = frozenset(owners)

        outcome = execute(
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
            claimed.update(dict.fromkeys(systems, rs.name))
    return code
