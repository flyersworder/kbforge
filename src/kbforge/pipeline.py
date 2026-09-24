"""The fixed-order pipeline (architecture §7). The order is NOT pluggable; the
no-op and never-auto-merge rules are trust guarantees enforced here."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge.chunking import (
    ChunkingConfig,
    ChunkRecord,
    admit,
    merge_records,
    read_record,
    restore,
    snapshot,
    write_record,
)
from kbforge.described import delete_described, write_described
from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    delete_first_seen,
    delete_sidecar,
    drifted,
    has_sidecars,
    load_first_seen,
    record_first_seen,
    resolve_all,
    with_first_seen,
    write_sidecar,
)
from kbforge.hookspecs import PublisherSpec
from kbforge.links import (
    LinkResolution,
    LinksConfig,
    delete_links,
    expand,
    has_links_sidecars,
    links_drifted,
    read_links,
    resolve_links,
    write_links,
)
from kbforge.mirror import commit, diff, load_all
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    RawRecord,
)
from kbforge.related import with_related
from kbforge.synthesize import (
    GroundingSynthesizer,
    StubSynthesizer,
    Synthesizer,
    concept_path,
)
from kbforge.validate import Failure, run_validators


class ConnectorProtocol(Protocol):
    """Duck-typed connector interface (hookspec-based)."""

    def kbforge_connector_info(self) -> ConnectorInfo: ...

    def kbforge_validate_config(self, config: dict) -> list[str]: ...

    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult: ...

    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]: ...


class PublisherProtocol(Protocol):
    """Duck-typed publisher interface (hookspec-based)."""

    def kbforge_publisher_info(self) -> ConnectorInfo: ...

    def kbforge_publish(self, change: ProposedChange, config: dict) -> str: ...


@dataclass(frozen=True)
class NoOp:
    """No change detected — no MR opened. Ever."""


@dataclass(frozen=True)
class Aborted:
    """Validation failed — the artifact is non-conformant, so no MR opened."""

    failures: list[Failure]


@dataclass(frozen=True)
class Published:
    url: str


@dataclass(frozen=True)
class Waiting:
    """The last chunk's review request is still open, so this run did nothing:
    nothing fetched, nothing synthesized, no review request touched."""

    request: str
    branch_hint: str


class ConfigError(RuntimeError):
    """A connector rejected its config before any I/O."""


class RedoRefused(RuntimeError):
    """`kbforge redo` found nothing it may safely roll back."""


@dataclass(frozen=True)
class Redone:
    admitted: list[str]


def _instance_key(config: dict) -> str:
    """A short digest of the connector's config, so two INSTANCES of one connector
    get separate state.

    `kbforge_connector_info().name` is static while a generic connector's
    `system` is per-instance config — `kbforge-mcp` is named `mcp` and carries a
    configured `system`. A name-keyed slot therefore lets sibling instances
    overwrite each other on the one shared `--state` §5.4 documents: the
    connector-owned `payload` is cross-contaminated, and `Cursor.systems` is
    worse, because scope decides which system's concepts a run may touch."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _cursor_slot(state_dir: Path, connector: str, config: dict) -> Path:
    return state_dir / f"cursor-{connector}-{_instance_key(config)}.json"


def _chunk_slot(state_dir: Path, connector: str, config: dict) -> Path:
    """Keyed exactly like the cursor slot, for the same sibling-instance reason."""
    return state_dir / f"chunk-{connector}-{_instance_key(config)}.json"


def _load_cursor(state_dir: Path, connector: str, config: dict) -> Cursor | None:
    slot = _cursor_slot(state_dir, connector, config)
    if slot.exists():
        return Cursor.model_validate_json(slot.read_text("utf-8"))
    # One-time read-through for a slot written before instance keying, so an
    # upgrade does not force a full re-fetch. Its `systems` is dropped: that is
    # the field siblings could have clobbered, and an empty one only costs the
    # first run's drift scan, whereas a wrong one publishes on another system's
    # branch. The payload is kept as-is — sharing it is exactly what this run
    # would have done before the upgrade, and it stops after this run.
    legacy = state_dir / f"cursor-{connector}.json"
    if not legacy.exists():
        return None
    return Cursor.model_validate_json(legacy.read_text("utf-8")).model_copy(
        update={"systems": []}
    )


def _save_cursor(
    state_dir: Path, cursor: Cursor, systems: set[str], config: dict
) -> None:
    """`systems` is stamped here rather than trusted from the connector: every
    connector builds a fresh Cursor in fetch, so whatever it set would be lost."""
    state_dir.mkdir(parents=True, exist_ok=True)
    slot = _cursor_slot(state_dir, cursor.connector, config)
    stamped = cursor.model_copy(update={"systems": sorted(systems)})
    slot.write_text(stamped.model_dump_json(), "utf-8")


def _drift_candidates(
    mirror_docs: list[CanonicalDocument],
    by_id: dict[str, CanonicalDocument],
    systems: set[str],
    changed: set[str],
    removed_ids: set[str],
) -> list[CanonicalDocument]:
    """The documents the drift scan evaluates, **as this run sees them**.

    Scope is this run's own output. Connector identity will not do:
    `kbforge_connector_info()` is static while a generic connector's `system` is
    per-instance (§7.1).

    `by_id.get(d.doc_id, d)` is the load-bearing part, and the reason this is a
    function rather than a comprehension inline in `run`. The candidate list is
    built from the mirror, but `grounded_by` is deliberately outside
    `content_hash` (§7.1), so a document whose only change is a `grounded_by`
    edit is `unchanged` in the diff and arrives here carrying its **pre-edit**
    declaration. Evaluating the mirror's copy compares the edit against itself,
    yields `NoOp` forever, and discards the fresh copy — a no-op run never
    commits. The fresh copy is also the one that must be re-synthesized.
    """
    return [
        by_id.get(d.doc_id, d)
        for d in mirror_docs
        if d.anchor.system in systems
        and d.doc_id not in changed
        and d.doc_id not in removed_ids
    ]


def _scope_failures(by_id: dict[str, CanonicalDocument]) -> list[Failure]:
    """A path collision on the shared mirror, reported rather than published
    into.

    `concept_path` drops the system prefix, so `wiki:readme` and `notes:readme`
    render one file on two sync branches; whichever merges second overwrites the
    other with no validator, no conflict, and no note. This check is also what
    makes resolving links by doc_id unambiguous across systems (#41): one doc_id
    per bundle path. System-qualified bundle paths would remove the collision at
    its root, but that rewrites every published path, so it is its own release
    (#42)."""
    failures: list[Failure] = []

    owners: dict[str, str] = {}
    for doc in sorted(by_id.values(), key=lambda d: d.doc_id):
        if doc.deleted:
            continue
        path = concept_path(doc.doc_id)
        prior = owners.setdefault(path, doc.doc_id)
        if prior != doc.doc_id:
            failures.append(
                Failure(
                    path,
                    "bundle-path-collision",
                    f"{prior} and {doc.doc_id} are different documents that render "
                    "one bundle path; whichever review request merges second would "
                    "overwrite the other",
                )
            )
    return failures


def _open_request_hook(
    publisher: PublisherProtocol, needed_by: str
) -> Callable[[str, dict], str | None]:
    """The publisher's optional `kbforge_open_request`, or a `ConfigError` naming
    who needs it (`--chunking`'s wait, or `redo`'s check) and why.

    `PublisherSpec`'s own method is a docstring-only default that returns None,
    so a publisher subclassing the spec without overriding it would inherit
    "never open" and chunk into open requests unchecked. It counts as missing."""
    open_request = getattr(publisher, "kbforge_open_request", None)
    inherited = (
        getattr(open_request, "__func__", None) is PublisherSpec.kbforge_open_request
    )
    if open_request is None or inherited:
        raise ConfigError(f"{publisher.kbforge_publisher_info().name}: {needed_by}")
    return open_request


def _open_chunk_request(
    open_request: Callable[[str, dict], str | None],
    record: ChunkRecord,
    publish_config: dict,
) -> tuple[str, str] | None:
    """`(request, branch_hint)` of the first open request on a recorded branch,
    or `None` if every recorded branch is merged or closed."""
    for hint in record.branch_hints:
        request = open_request(hint, publish_config)
        if request is not None:
            return request, hint
    return None


def run(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
    synthesizer: Synthesizer | None = None,
    grounding_config: GroundingConfig | None = None,
    chunking: ChunkingConfig | None = None,
    links_config: LinksConfig | None = None,
) -> NoOp | Aborted | Published | Waiting:
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")

    # Before the fetch, so a waiting run costs one read-only forge call (§6).
    open_request: Callable[[str, dict], str | None] | None = None
    record: ChunkRecord | None = None
    if chunking is not None:
        open_request = _open_request_hook(
            publisher,
            "--chunking needs a publisher that implements kbforge_open_request, "
            "to wait between chunks; this one does not",
        )
        record = read_record(_chunk_slot(Path(state_dir), info.name, config))
        if record is not None and record.pending:
            opened = _open_chunk_request(open_request, record, publish_config)
            if opened is not None:
                request, hint = opened
                return Waiting(request=request, branch_hint=hint)

    synthesizer = synthesizer or StubSynthesizer()

    mirror_path = Path(mirror)
    state_path = Path(state_dir)

    prior = _load_cursor(state_path, info.name, config)
    result = connector.kbforge_fetch(config, prior)
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
    assert_fetch_contract(docs, complete=result.complete)  # §4.2 fetch contract

    grounds = getattr(synthesizer, "grounds", False)
    grounding_cfg = grounding_config or GroundingConfig()
    # Expanded once: symmetric reverses are what let B's run find A's entry.
    expanded = expand(links_config) if links_config is not None else {}

    changeset = diff(mirror_path, docs)

    # The scan is gated three ways so a deployment that declares no grounding
    # keeps today's cheap no-op: the synthesizer must ground, and there must be
    # either something declared now or a sidecar from before (§5). Rules make
    # the scan unconditional, since a concept with no grounding yet must still
    # be able to pick up its first matching document.
    scan = grounds and bool(
        grounding_cfg.grounding
        or grounding_cfg.rules
        or any(d.grounded_by for d in docs)
        or has_sidecars(mirror_path)
    )
    # Link drift (§7.4) is gated like grounding drift: `--links` given, or a
    # sidecar from before. Unlike grounding it runs under every synthesizer,
    # because links are frame, not prose.
    link_scan = links_config is not None or has_links_sidecars(mirror_path)
    if changeset.is_noop and not scan and not link_scan:
        return NoOp()

    # Read once per run, and only past the first no-op gate. The mirror is
    # still the pre-run published state here: commit() below is the only thing
    # that mutates it, and it runs only after a successful publish.
    #
    # This parses every JSON slot in the mirror, not just the ones this run
    # touches, so a run that used to cost O(changed) now costs O(mirror size)
    # whenever anything changed, or whenever the drift scan runs at all.
    # Accepted: the defect this closes (dangling links after a deletion, and
    # grounding drift) is not tombstone-specific — there is no cheaper subset
    # of the mirror that is still correct.
    mirror_docs = load_all(mirror_path)
    # Chunked review (design/2026-09-19): an oversized change publishes one
    # chunk now and leaves the rest as backlog, which stays visible to `diff`
    # because only admitted documents are committed. Everything below sees the
    # world as it will be once this chunk merges, so `admitted_docs` replaces
    # `docs` wherever a document could reach the bundle or the mirror. Without
    # --chunking the backlog is empty and `admitted_docs` is `docs`.
    changed = set(changeset.added) | set(changeset.modified)
    backlog: set[str] = set()
    if chunking is not None:
        chunk, _ = admit(
            [d for d in docs if d.doc_id in changed], chunking, chunking.max_concepts
        )
        backlog = changed - {d.doc_id for d in chunk}
        changed -= backlog
        # A change too big for one request must not start chunking into a
        # request the last chunk left open: the final chunk's request stays
        # open to small follow-ups, but the first of several chunks appended
        # to it rebuilds the unbounded request chunking exists to prevent
        # (§6). Before the drift scan and synthesis, so waiting costs no tokens.
        if backlog and record is not None and open_request is not None:
            opened = _open_chunk_request(open_request, record, publish_config)
            if opened is not None:
                request, hint = opened
                return Waiting(request=request, branch_hint=hint)
    admitted_docs = [d for d in docs if d.doc_id not in backlog]
    # Recency fallback for grounding rules; loaded once, only when the drift
    # scan runs and rules exist. Gated on `scan`, not `grounding_cfg.rules`
    # alone: `scan` already requires `grounds`, and a synthesizer that never
    # grounds never ranks anything by first-seen, so loading it under the
    # stub was pure waste on every run under a rules config.
    # Overlaid with THIS run's own documents (existing records win): a rule
    # can match a document committed in the same run as its owner, before its
    # sidecar exists, and without the overlay it would rank as undated on this
    # run and dated on the next identical-fetch run -- an unchanged world would
    # stop being a no-op.
    first_seen = (
        with_first_seen(load_first_seen(mirror_path), admitted_docs)
        if scan and grounding_cfg.rules
        else {}
    )
    by_id = {d.doc_id: d for d in mirror_docs}
    by_id.update({d.doc_id: d for d in admitted_docs if not d.deleted})
    # A doc this run tombstones is never in the update above (it is filtered
    # by `not d.deleted`), so without this its *stale, pre-run* mirror copy
    # -- still `deleted=False` -- would linger in `by_id` under its own
    # doc_id. Every `doc.deleted` guard downstream (`resolve`, `resolve_all`,
    # `rule_matches`) checks the copy IN `by_id`, so that guard would never
    # fire: a rule (or an explicit declaration) could cite a document the
    # same run is deleting, and the sidecar would record it as grounding.
    for doc in admitted_docs:
        if doc.deleted:
            by_id.pop(doc.doc_id, None)
    hashes = {k: v.anchor.content_hash for k, v in by_id.items()}

    removed_ids = set(changeset.removed)
    changed_docs = [d for d in docs if d.doc_id in changed]

    # Memoised: every drifted document is resolved once for the drift comparison
    # and again for `grounding_map`, from the same `by_id` and the same config,
    # so the second call is provably the first one's answer. Keyed by doc_id,
    # which is safe because every caller passes the `by_id` copy of a document.
    _resolutions: dict[str, tuple[list[CanonicalDocument], list[str]]] = {}

    def _resolved(doc: CanonicalDocument) -> tuple[list[CanonicalDocument], list[str]]:
        cached = _resolutions.get(doc.doc_id)
        if cached is None:
            cached = resolve_all(doc, grounding_cfg, by_id, first_seen)
            _resolutions[doc.doc_id] = cached
        return cached

    # Links, memoised for the same reason as grounding: the link-drift scan,
    # the synthesis copy, the note loop, `with_related` and the sidecar write
    # all resolve the same document from the same `by_id`.
    _link_resolutions: dict[str, LinkResolution] = {}

    def _links_of(doc: CanonicalDocument) -> LinkResolution:
        cached = _link_resolutions.get(doc.doc_id)
        if cached is None:
            cached = resolve_links(doc, expanded, by_id)
            _link_resolutions[doc.doc_id] = cached
        return cached

    # This run's systems. Used three times: to scope the drift scan, `referrers`,
    # and `existing` — grounding requires one shared mirror, so all three see
    # every system's documents and all three must filter to this run's own.
    #
    # The fallback is what makes drift work for an incremental connector. Drift
    # exists to republish when the owner's own source did NOT change, so the
    # runs that need it most are precisely the ones whose fetch is empty — and
    # for those, `docs` names no system at all. The prior cursor is the only
    # per-run record of which systems this connector owns. It is a fallback
    # rather than a union: unioning would let a reconfigured connector keep
    # scanning a system it no longer owns, which is the defect the scoping
    # closed in the first place.
    systems = {d.anchor.system for d in docs} or set(prior.systems if prior else ())

    # `changed | backlog`: a backlog document is rebuilt whole in its own
    # chunk, so rebuilding its stale mirror copy for drift now is waste.
    candidates = (
        _drift_candidates(mirror_docs, by_id, systems, changed | backlog, removed_ids)
        if scan or link_scan
        else []
    )
    drift: list[str] = []
    deferred_drift: set[str] = set()
    if scan:
        drift = drifted(
            mirror_path,
            candidates,
            {d.doc_id: [g.doc_id for g in _resolved(d)[0]] for d in candidates},
            hashes,
        )
        # Hoisted: as a comprehension condition this was rebuilt once per
        # candidate, and `candidates` is O(mirror).
        drift_ids = set(drift)
        drifted_docs = [d for d in candidates if d.doc_id in drift_ids]
        if chunking is not None:
            # Drift counts toward the cap: each is a concept the reviewer
            # reads. A deferred one writes no sidecar, so the next run finds
            # the same drift again.
            room = max(chunking.max_concepts - len(changed), 0)
            drifted_docs, _ = admit(drifted_docs, chunking, room)
            deferred_drift = drift_ids - {d.doc_id for d in drifted_docs}
            drift = [x for x in drift if x not in deferred_drift]
        changed_docs += drifted_docs

    # A concept whose managed links (targets or notes) moved since its last
    # publish: a links.yaml edit, or a target added or tombstoned by another
    # system's run. Rebuilt on its own system's run, never the other's. Never
    # changes `relations`, the mirror or links.yaml, so it converges.
    link_drift: list[str] = []
    deferred_link_drift: set[str] = set()
    if link_scan:
        already = set(drift) | deferred_drift
        link_drift = [
            x
            for x in links_drifted(
                mirror_path,
                candidates,
                {d.doc_id: _links_of(d).managed for d in candidates},
            )
            if x not in already
        ]
        link_ids = set(link_drift)
        link_docs = [d for d in candidates if d.doc_id in link_ids]
        if chunking is not None:
            # After changed documents and grounding drift, into what remains.
            room = max(chunking.max_concepts - len(changed) - len(drift), 0)
            link_docs, _ = admit(link_docs, chunking, room)
            deferred_link_drift = link_ids - {d.doc_id for d in link_docs}
            link_drift = [x for x in link_drift if x not in deferred_link_drift]
        changed_docs += link_docs

    if changeset.is_noop and not drift and not link_drift:
        return NoOp()

    # A concept linking to a deleted one must be re-synthesized, or its link
    # survives as a dangling reference (§4.4 law 2) that nothing checks: the
    # validators only inspect concepts carried by this proposal. The mirror, not
    # `docs`, is the source — an incremental connector's fetch need not contain
    # the referrer.
    #
    # Scoped to `systems`, and it is the third site that must be: the shared
    # mirror carries every system's documents, so a document in another system
    # can name a doc_id this run tombstones. Unscoped it is pulled in here, and
    # because `existing` IS scoped, law 2 then strips every one of ITS links as
    # dangling and republishes it — on the wrong branch, since `branch_hint`
    # comes from the first item and a deletion-only run has no other.
    # Under chunking, `changed` is the admitted set, so a backlog document that
    # links to a removed concept is rebuilt here from its mirror copy (§4.1).
    #
    # A recorded managed link counts as well as a relation: a same-system
    # links.yaml link to a removed concept is link drift, which the cap can
    # defer, and a deferred referrer would keep its dangling link on `main`
    # while this chunk deletes the target. Read from the `_links/` sidecar, not
    # re-resolved: it records what the published file carries.
    referrers: list[CanonicalDocument] = []
    if removed_ids:
        recorded = has_links_sidecars(mirror_path)
        referrers = [
            d
            for d in mirror_docs
            if d.anchor.system in systems
            and d.doc_id not in changed
            and d.doc_id not in removed_ids
            and (
                removed_ids.intersection(d.relations)
                or (
                    recorded
                    and removed_ids.intersection(
                        t for t, _ in read_links(mirror_path, d.doc_id)
                    )
                )
            )
        ]
        changed_docs += referrers

    # Arrival referrers, the mirror image of `referrers` (#32): a published
    # concept whose relation names a document this run adds lost that link
    # under law 2 when it was built, because the target did not exist yet -- a
    # chunked run's backlog (§4.1 of the chunked-review note), or simply a
    # target the source created later. Nothing else rebuilds it, so the link
    # would stay missing until its own source changed. Scoped and filtered
    # exactly like `referrers`.
    arrivals: list[CanonicalDocument] = []
    arrived = set(changeset.added) - backlog
    if arrived:
        arrivals = [
            d
            for d in mirror_docs
            if d.anchor.system in systems
            and d.doc_id not in changed
            and d.doc_id not in removed_ids
            and arrived.intersection(d.relations)
        ]
        changed_docs += arrivals

    # A deferred-drift document is not exempt from law 2: if it also links to a
    # concept this run removes, or itself gains a link to a concept this chunk
    # adds, `referrers`/`arrivals` rebuild it regardless of the cap (neither
    # filter checks `deferred_drift`). It is then published in THIS chunk with
    # fresh grounding, so treating it as still-deferred would leave it with no
    # "grounding changed" note, an inflated backlog count, and a chunk record
    # stuck `pending: True` even once nothing is actually left to redo.
    # Promoted back into `drift` rather than left alone, so the note loop and
    # the `pending`/"carries" accounting below see it as delivered.
    rebuilt = {d.doc_id for d in referrers + arrivals}
    rebuilt_deferred = deferred_drift & rebuilt
    if rebuilt_deferred:
        drift += sorted(rebuilt_deferred)
        deferred_drift -= rebuilt_deferred
    rebuilt_link = deferred_link_drift & rebuilt
    if rebuilt_link:
        link_drift += sorted(rebuilt_link)
        deferred_link_drift -= rebuilt_link

    # The drift scan and `referrers` can both select the same document — drift
    # knows nothing about `referrers`' filter and vice versa. Deduped once,
    # here, before either feeds the synthesizer or `summary.sources_changed`.
    seen_ids: set[str] = set()
    deduped: list[CanonicalDocument] = []
    for d in changed_docs:
        if d.doc_id in seen_ids:
            continue  # drift and referrers can select the same document
        seen_ids.add(d.doc_id)
        deduped.append(d)
    changed_docs = deduped

    # Every concept this run renders gets its links resolved by doc_id over the
    # whole mirror (§7.4): connector relations and editorial links alike, across
    # systems. The synthesizer receives a COPY whose `relations` are the
    # resolved targets, never the original: `commit()` below writes `docs`, and
    # config-dependent content must not reach the mirror (§7.1's rule).
    link_notes: list[str] = []
    link_targets: set[str] = set()
    synth_docs: list[CanonicalDocument] = []
    for doc in changed_docs:
        res = _links_of(doc)
        link_notes += res.notes
        link_targets |= {target for target, _ in res.links}
        synth_docs.append(
            doc.model_copy(update={"relations": [t for t, _ in res.links]})
        )

    # Existing bundle paths feed §4.4 law 2: assemble() drops any link that is
    # not in here, so a link to an unchanged-but-still-published concept would
    # otherwise vanish from a re-rendered file. The mirror is the published
    # state, so it — not `docs` — is the honest source: an incremental
    # connector's fetch carries only what changed, and building this from
    # `docs` alone silently stripped every surviving link off any referrer
    # pulled into scope above. `docs` is unioned in because this run's additions
    # are not in the mirror yet. Tombstones are subtracted from both: a concept
    # this run deletes must not count as a resolvable link target.
    # Scoped to `systems` for the same reason the drift scan is: cross-source
    # grounding requires ONE shared mirror, so `mirror_docs` now carries every
    # system's documents, and `concept_path` drops the system prefix — so
    # `wiki:readme.md` and `notes:readme.md` occupy one bundle path. Unscoped, a
    # link to a document that does not exist in this system publishes as
    # *surviving* because another system happens to hold one with the same
    # native_id, and law 2 never sees the dangling link. Resolved link targets
    # are unioned in on top: they were resolved by doc_id, not by path, so they
    # cannot be rescued by another system's document.
    tombstoned = {concept_path(doc_id) for doc_id in changeset.removed}
    existing = (
        frozenset(
            {concept_path(d.doc_id) for d in mirror_docs if d.anchor.system in systems}
            | {concept_path(d.doc_id) for d in admitted_docs if not d.deleted}
            | {concept_path(t) for t in link_targets}
        )
        - tombstoned
    )

    scope_failures = _scope_failures(by_id)
    if scope_failures:
        return Aborted(failures=scope_failures)

    grounding_map: dict[str, list[CanonicalDocument]] = {}
    grounding_notes: list[str] = []
    if grounds:
        for doc in changed_docs:
            docs_for, notes_for = _resolved(doc)
            if docs_for:
                grounding_map[doc.doc_id] = docs_for
            grounding_notes += notes_for

    # Summary claims come from the changeset, so a chunk must not claim its
    # backlog. Identical to `changeset` when nothing is backlog.
    chunk_changeset = changeset.model_copy(
        update={
            "added": [x for x in changeset.added if x not in backlog],
            "modified": [x for x in changeset.modified if x not in backlog],
        }
    )

    if grounds:
        # `Synthesizer` deliberately keeps its 0.7.0 shape, so the type checker
        # cannot narrow `synthesizer` to something accepting `grounding=` from
        # the `grounds` flag alone — that flag is a runtime capability check,
        # not a type-level one. The cast documents the mismatch rather than
        # papering over it: this branch is reached only when `synthesizer`
        # really does implement `GroundingSynthesizer` (every shipped
        # implementation sets `grounds = True` exactly when it does).
        proposal = cast(GroundingSynthesizer, synthesizer).synthesize(
            synth_docs, chunk_changeset, existing, grounding=grounding_map
        )
    else:
        proposal = synthesizer.synthesize(synth_docs, chunk_changeset, existing)
    proposal.summary.grounding_notes.extend(grounding_notes)
    proposal.summary.grounding_notes.extend(link_notes)

    # Assigned here, never taken from the synthesizer: deletion is structure,
    # not prose, so an LLM synthesizer cannot delete a file it dislikes.
    proposal.files_removed = sorted(concept_path(d) for d in changeset.removed)

    # A referrer is in change.files but in none of claims_added/modified/removed,
    # so without this the reviewer sees a file in the diff that the body never
    # accounts for. Guarded by membership in proposal.files: the stub always
    # renders every referrer it is handed, so this can't diverge today, but an
    # LLM synthesizer that drops or fails a doc must not leave a note
    # describing a file that never made it into the diff — the inverse of the
    # stale-link defect this run is closing.
    for doc in referrers:
        path = concept_path(doc.doc_id)
        if path not in proposal.files:
            continue
        proposal.summary.grounding_notes.append(
            f"{path}: re-synthesized to drop links to "
            "concepts removed in this run; its own source is unchanged"
        )

    for doc_id in drift:
        path = concept_path(doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized because its grounding changed since "
                "it was last published; its own source is unchanged"
            )

    for doc_id in link_drift:
        path = concept_path(doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized because its links changed since it was "
                "last published; its own source is unchanged"
            )

    for doc in arrivals:
        path = concept_path(doc.doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized to restore a link to a concept added in "
                "this run; its own source is unchanged"
            )

    # The merge-order window (§7.4): the mirror advances on publish, not merge,
    # so a cross-system link dangles on `main` until its target's request
    # merges. kbforge never merges; it tells the reviewer instead.
    for doc in changed_docs:
        path = concept_path(doc.doc_id)
        concept = proposal.concepts.get(path)
        if path not in proposal.files or concept is None:
            continue
        own = doc.doc_id.partition(":")[0]
        for target, _ in _links_of(doc).links:
            other = target.partition(":")[0]
            if other != own and concept_path(target) in concept.links:
                proposal.summary.grounding_notes.append(
                    f"{path}: links to {target} (system {other}); merge that "
                    "system's review request first, or the link dangles until "
                    "it does"
                )

    # Frame, not prose: rendered here so every synthesizer gets the same
    # section and none can forge it. Bound to the projection by
    # `validate._check_related_section` (architecture.md §7.4).
    with_related(
        proposal,
        {concept_path(i): d.title for i, d in by_id.items()},
        {
            concept_path(d.doc_id): {
                concept_path(t): note for t, note in _links_of(d).links if note
            }
            for d in changed_docs
        },
    )

    pending = bool(backlog or deferred_drift or deferred_link_drift)
    if pending:
        carried = len(changed) + len(drift) + len(link_drift)
        waiting = len(backlog) + len(deferred_drift) + len(deferred_link_drift)
        proposal.summary.grounding_notes.append(
            f"chunked review: this request carries {carried} of "
            f"{carried + waiting} changed concepts; "
            "the rest follow once it is merged or closed"
        )

    failures = run_validators(proposal, existing)
    if failures:
        return Aborted(failures=failures)

    # Asked before publishing, since afterwards the answer is always "open".
    # If this publish appends to the request the recorded chunk opened, redo
    # must roll back both runs, because closing that request discards both.
    appends = (
        record is not None
        and open_request is not None
        and proposal.branch_hint in record.branch_hints
        and open_request(proposal.branch_hint, publish_config) is not None
    )
    url = publisher.kbforge_publish(proposal, publish_config)
    cursor_slot = _cursor_slot(state_path, info.name, config)
    touched: set[str] = set()
    prior_mirror: dict[str, str | None] = {}
    prior_cursor: str | None = None
    if chunking is not None:
        # Captured before the commit: everything redo must put back (§5).
        touched = (
            changed
            | removed_ids
            | set(drift)
            | set(link_drift)
            | {d.doc_id for d in referrers + arrivals}
        )
        prior_mirror = snapshot(mirror_path, touched)
        prior_cursor = cursor_slot.read_text("utf-8") if cursor_slot.exists() else None
    commit(mirror_path, admitted_docs)  # advance mirror ONLY after success
    record_first_seen(mirror_path, admitted_docs)
    for doc in changed_docs:
        if concept_path(doc.doc_id) not in proposal.files:
            # The synthesizer dropped this document, exactly as the two note
            # loops above guard for. Nothing was published from it this run, so
            # neither recording nor clearing its sidecar would describe the
            # bundle: leave whatever the last successful build recorded.
            continue
        described_record = proposal.described.get(concept_path(doc.doc_id))
        if described_record is not None and described_record.doc_id == doc.doc_id:
            write_described(mirror_path, described_record)
        else:
            # Delete, not skip, for the grounding sidecar's reason: a record
            # left behind describes a concept that no longer ships its text,
            # and #41's tag reads would trust it. A record naming another
            # document is not written anywhere -- a synthesizer does not get
            # to write another concept's mirror state.
            delete_described(mirror_path, doc.doc_id)
        resolution = _links_of(doc)
        if resolution.declares_managed:
            # Written even when empty, for the grounding sidecar's reason: a
            # declared link unresolvable today must still be rescanned when its
            # target arrives, and the sidecar is what trips the scan.
            write_links(mirror_path, doc.doc_id, resolution.managed)
        else:
            delete_links(mirror_path, doc.doc_id)
        docs_for = grounding_map.get(doc.doc_id) if grounds else None
        if docs_for:
            write_sidecar(
                mirror_path,
                doc.doc_id,
                {g.doc_id: g.anchor.content_hash for g in docs_for},
            )
        elif grounds and declared_ids(doc, grounding_cfg):
            # Declared, but nothing resolved — the sibling system has not synced
            # yet. An EMPTY sidecar, not no sidecar: `has_sidecars` is the cheap
            # gate that decides whether the scan runs at all, and an incremental
            # connector whose later fetches are empty has nothing else to trip
            # it. Without this the concept is stranded ungrounded forever, which
            # is the stranding `grounding.drifted` exists to prevent. It cannot
            # loop: still-unresolvable means `current == set() == recorded`, and
            # dropping the declaration falls through to the delete below.
            write_sidecar(mirror_path, doc.doc_id, {})
        else:
            # A delete, not a skip: a stale sidecar fires rule 3 forever. Run
            # OUTSIDE the `grounds` guard, matching the tombstone loop below: a
            # rebuild under a non-grounding synthesizer republishes the concept
            # with single-source `sources`, so a sidecar left behind claims
            # grounding the shipped file does not have — and the next run under
            # a grounding synthesizer finds it unchanged and returns NoOp,
            # stranding the concept ungrounded permanently.
            delete_sidecar(mirror_path, doc.doc_id)
    for doc_id in changeset.removed:
        delete_sidecar(mirror_path, doc_id)
        delete_first_seen(mirror_path, doc_id)
        delete_described(mirror_path, doc_id)
        delete_links(mirror_path, doc_id)
    # Held while a backlog remains: the next chunk re-fetches from the same
    # cursor, which §4.2's at-least-once replay makes harmless (§4.2 of the
    # design note).
    if not pending:
        _save_cursor(state_path, result.cursor, systems, config)
    if chunking is not None:
        new_record = ChunkRecord(
            branch_hints=[proposal.branch_hint],
            pending=pending,
            admitted=sorted(touched),
            mirror=prior_mirror,
            cursor=prior_cursor,
        )
        if appends and record is not None:
            new_record = merge_records(record, new_record)
        write_record(_chunk_slot(state_path, info.name, config), new_record)
    return Published(url=url)


def redo(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
) -> Redone:
    """Roll the last chunk out of the mirror so the next run proposes it again
    (design/2026-09-19 §7). One level deep: waiting guarantees every earlier
    chunk was merged or closed before this one was synthesized. Closing a
    request still means discard; this is the only way to re-propose."""
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")
    open_request = _open_request_hook(
        publisher,
        "redo needs a publisher that implements kbforge_open_request; "
        "this one does not",
    )
    state_path = Path(state_dir)
    slot = _chunk_slot(state_path, info.name, config)
    record = read_record(slot)
    if record is None:
        raise RedoRefused(
            f"{info.name}: no chunk to redo (no chunk record in {state_path}; "
            "only a run with --chunking writes one)"
        )
    opened = _open_chunk_request(open_request, record, publish_config)
    if opened is not None:
        request, _ = opened  # the hint is not the resolved branch; see the CLI
        raise RedoRefused(
            f"review request {request} is still open; close it "
            "first, or the redone chunk would be appended to it"
        )
    restore(record, Path(mirror), _cursor_slot(state_path, info.name, config))
    slot.unlink()
    return Redone(admitted=record.admitted)
