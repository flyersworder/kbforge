"""The fixed-order pipeline (architecture §7). The order is NOT pluggable; the
no-op and never-auto-merge rules are trust guarantees enforced here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    delete_sidecar,
    drifted,
    has_sidecars,
    resolve,
    write_sidecar,
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


class ConfigError(RuntimeError):
    """A connector rejected its config before any I/O."""


def _cursor_slot(state_dir: Path, connector: str) -> Path:
    return state_dir / f"cursor-{connector}.json"


def _load_cursor(state_dir: Path, connector: str) -> Cursor | None:
    slot = _cursor_slot(state_dir, connector)
    if not slot.exists():
        return None
    return Cursor.model_validate_json(slot.read_text("utf-8"))


def _save_cursor(state_dir: Path, cursor: Cursor, systems: set[str]) -> None:
    """`systems` is stamped here rather than trusted from the connector: every
    connector builds a fresh Cursor in fetch, so whatever it set would be lost."""
    state_dir.mkdir(parents=True, exist_ok=True)
    slot = _cursor_slot(state_dir, cursor.connector)
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
) -> NoOp | Aborted | Published:
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")

    synthesizer = synthesizer or StubSynthesizer()

    mirror_path = Path(mirror)
    state_path = Path(state_dir)

    prior = _load_cursor(state_path, info.name)
    result = connector.kbforge_fetch(config, prior)
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
    assert_fetch_contract(docs, complete=result.complete)  # §4.2 fetch contract

    grounds = getattr(synthesizer, "grounds", False)
    grounding_cfg = grounding_config or GroundingConfig()

    changeset = diff(mirror_path, docs)

    # The scan is gated three ways so a deployment that declares no grounding
    # keeps today's cheap no-op: the synthesizer must ground, and there must be
    # either something declared now or a sidecar from before (§5).
    scan = grounds and bool(
        grounding_cfg.grounding
        or any(d.grounded_by for d in docs)
        or has_sidecars(mirror_path)
    )
    if changeset.is_noop and not scan:
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
    by_id = {d.doc_id: d for d in mirror_docs}
    by_id.update({d.doc_id: d for d in docs if not d.deleted})
    hashes = {k: v.anchor.content_hash for k, v in by_id.items()}

    changed = set(changeset.added) | set(changeset.modified)
    removed_ids = set(changeset.removed)
    changed_docs = [d for d in docs if d.doc_id in changed]

    def _resolved(doc: CanonicalDocument) -> tuple[list[CanonicalDocument], list[str]]:
        return resolve(
            doc,
            declared_ids(doc, grounding_cfg),
            by_id,
            max_docs=grounding_cfg.max_grounding_docs,
        )

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

    drift: list[str] = []
    if scan:
        candidates = _drift_candidates(
            mirror_docs, by_id, systems, changed, removed_ids
        )
        drift = drifted(
            mirror_path,
            candidates,
            {d.doc_id: [g.doc_id for g in _resolved(d)[0]] for d in candidates},
            hashes,
        )
        changed_docs += [d for d in candidates if d.doc_id in set(drift)]

    if changeset.is_noop and not drift:
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
    referrers: list[CanonicalDocument] = []
    if removed_ids:
        referrers = [
            d
            for d in mirror_docs
            if d.anchor.system in systems
            and d.doc_id not in changed
            and d.doc_id not in removed_ids
            and removed_ids.intersection(d.relations)
        ]
        changed_docs += referrers

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
    # native_id, and law 2 never sees the dangling link.
    tombstoned = {concept_path(doc_id) for doc_id in changeset.removed}
    existing = (
        frozenset(
            {concept_path(d.doc_id) for d in mirror_docs if d.anchor.system in systems}
            | {concept_path(d.doc_id) for d in docs if not d.deleted}
        )
        - tombstoned
    )

    grounding_map: dict[str, list[CanonicalDocument]] = {}
    grounding_notes: list[str] = []
    if grounds:
        for doc in changed_docs:
            docs_for, notes_for = _resolved(doc)
            if docs_for:
                grounding_map[doc.doc_id] = docs_for
            grounding_notes += notes_for

    if grounds:
        # `Synthesizer` deliberately keeps its 0.7.0 shape, so the type checker
        # cannot narrow `synthesizer` to something accepting `grounding=` from
        # the `grounds` flag alone — that flag is a runtime capability check,
        # not a type-level one. The cast documents the mismatch rather than
        # papering over it: this branch is reached only when `synthesizer`
        # really does implement `GroundingSynthesizer` (every shipped
        # implementation sets `grounds = True` exactly when it does).
        proposal = cast(GroundingSynthesizer, synthesizer).synthesize(
            changed_docs, changeset, existing, grounding=grounding_map
        )
    else:
        proposal = synthesizer.synthesize(changed_docs, changeset, existing)
    proposal.summary.grounding_notes.extend(grounding_notes)

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
                f"{path}: re-synthesized because a document it is grounded in "
                "changed in another system; its own source is unchanged"
            )

    failures = run_validators(proposal, existing)
    if failures:
        return Aborted(failures=failures)

    url = publisher.kbforge_publish(proposal, publish_config)
    commit(mirror_path, docs)  # advance mirror ONLY after success
    for doc in changed_docs:
        if concept_path(doc.doc_id) not in proposal.files:
            # The synthesizer dropped this document, exactly as the two note
            # loops above guard for. Nothing was published from it this run, so
            # neither recording nor clearing its sidecar would describe the
            # bundle: leave whatever the last successful build recorded.
            continue
        docs_for = grounding_map.get(doc.doc_id) if grounds else None
        if docs_for:
            write_sidecar(
                mirror_path,
                doc.doc_id,
                {g.doc_id: g.anchor.content_hash for g in docs_for},
            )
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
    _save_cursor(state_path, result.cursor, systems)
    return Published(url=url)
