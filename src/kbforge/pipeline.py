"""The fixed-order pipeline (architecture §7). The order is NOT pluggable; the
no-op and never-auto-merge rules are trust guarantees enforced here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kbforge.canonical import assert_stability
from kbforge.mirror import commit, diff, load_all
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    RawRecord,
)
from kbforge.synthesize import StubSynthesizer, Synthesizer, concept_path
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


def _save_cursor(state_dir: Path, cursor: Cursor) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    slot = _cursor_slot(state_dir, cursor.connector)
    slot.write_text(cursor.model_dump_json(), "utf-8")


def run(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
    synthesizer: Synthesizer | None = None,
) -> NoOp | Aborted | Published:
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")

    synthesizer = synthesizer or StubSynthesizer()

    mirror_path = Path(mirror)
    state_path = Path(state_dir)

    result = connector.kbforge_fetch(config, _load_cursor(state_path, info.name))
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1

    changeset = diff(mirror_path, docs)
    if changeset.is_noop:
        return NoOp()

    changed = set(changeset.added) | set(changeset.modified)
    changed_docs = [d for d in docs if d.doc_id in changed]  # "scope"

    # Read once per run, and only past the no-op gate. The mirror is still the
    # pre-run published state here: commit() below is the only thing that
    # mutates it, and it runs only after a successful publish.
    #
    # This parses every JSON slot in the mirror, not just the ones this run
    # touches, so a run that used to cost O(changed) now costs O(mirror size)
    # whenever anything changed at all. Accepted: the defect this closes
    # (dangling links after a deletion) is not tombstone-specific — any
    # removal can leave an unchanged referrer with a stale link — so there is
    # no cheaper subset of the mirror that is still correct.
    mirror_docs = load_all(mirror_path)

    # A concept linking to a deleted one must be re-synthesized, or its link
    # survives as a dangling reference (§4.4 law 2) that nothing checks: the
    # validators only inspect concepts carried by this proposal. The mirror, not
    # `docs`, is the source — an incremental connector's fetch need not contain
    # the referrer.
    removed_ids = set(changeset.removed)
    referrers: list[CanonicalDocument] = []
    if removed_ids:
        referrers = [
            d
            for d in mirror_docs
            if d.doc_id not in changed
            and d.doc_id not in removed_ids
            and removed_ids.intersection(d.relations)
        ]
        changed_docs += referrers

    # Existing bundle paths feed §4.4 law 2: assemble() drops any link that is
    # not in here, so a link to an unchanged-but-still-published concept would
    # otherwise vanish from a re-rendered file. The mirror is the published
    # state, so it — not `docs` — is the honest source: an incremental
    # connector's fetch carries only what changed, and building this from
    # `docs` alone silently stripped every surviving link off any referrer
    # pulled into scope above. `docs` is unioned in because this run's additions
    # are not in the mirror yet. Tombstones are subtracted from both: a concept
    # this run deletes must not count as a resolvable link target.
    tombstoned = {concept_path(doc_id) for doc_id in changeset.removed}
    existing = (
        frozenset(
            {concept_path(d.doc_id) for d in mirror_docs}
            | {concept_path(d.doc_id) for d in docs if not d.deleted}
        )
        - tombstoned
    )
    proposal = synthesizer.synthesize(changed_docs, changeset, existing)

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

    failures = run_validators(proposal, existing)
    if failures:
        return Aborted(failures=failures)

    url = publisher.kbforge_publish(proposal, publish_config)
    commit(mirror_path, docs)  # advance mirror ONLY after success
    _save_cursor(state_path, result.cursor)
    return Published(url=url)
