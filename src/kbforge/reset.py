"""`kbforge reset SOURCE`: forget one source's local state (project-file design
§6.5), so the next run proposes everything for it as a first run would.

Destructive, so exact: the source's own cursor and chunk slots (by connector
and digest, never the legacy shared `cursor-<connector>.json` another instance
may still read through), and the mirror documents whose `anchor.system` is one
of its systems. Under §5.3 a system has one owner, so that never reaches
another source's documents; with no known systems the mirror is not touched."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from kbforge.chunking import ChunkRecordError, read_record
from kbforge.described import delete_described
from kbforge.grounding import delete_first_seen, delete_sidecar
from kbforge.links import delete_links
from kbforge.mirror import commit, load_all
from kbforge.models import Cursor
from kbforge.sources_record import SourceEntry


@dataclass(frozen=True)
class ResetPlan:
    state_files: list[Path]
    """Existing cursor/chunk slots for every known digest."""
    doc_ids: list[str]
    """Mirror documents whose `anchor.system` is in `systems`, sorted."""
    systems: frozenset[str]


def cursor_slot(state: Path, connector: str, digest: str) -> Path:
    """Named as `pipeline._cursor_slot` names it, from the digest alone: a
    stale source has no config left to hash."""
    return Path(state) / f"cursor-{connector}-{digest}.json"


def chunk_slot(state: Path, connector: str, digest: str) -> Path:
    return Path(state) / f"chunk-{connector}-{digest}.json"


def state_slots(state: Path, connector: str, digests: set[str]) -> list[Path]:
    """The cursor and chunk slots that exist for these digests."""
    return [
        slot
        for digest in sorted(digests)
        for slot in (
            cursor_slot(state, connector, digest),
            chunk_slot(state, connector, digest),
        )
        if slot.exists()
    ]


def plan_reset(
    mirror: Path,
    state: Path,
    *,
    connector: str,
    digests: set[str],
    systems: frozenset[str],
) -> ResetPlan:
    doc_ids = (
        sorted(doc.doc_id for doc in load_all(mirror) if doc.anchor.system in systems)
        if systems
        else []
    )
    return ResetPlan(
        state_files=state_slots(state, connector, digests),
        doc_ids=doc_ids,
        systems=systems,
    )


def apply_reset(mirror: Path, rp: ResetPlan) -> None:
    """Mirror slots and their sidecars first, the state slots last: a reset
    interrupted part-way still has the slots that name its systems."""
    wanted = set(rp.doc_ids)
    tombstones = [
        doc.model_copy(update={"deleted": True})
        for doc in load_all(mirror)
        if doc.doc_id in wanted
    ]
    if tombstones:
        commit(mirror, tombstones)  # commit unlinks a tombstone's slot
    for doc_id in rp.doc_ids:
        delete_sidecar(mirror, doc_id)
        delete_first_seen(mirror, doc_id)
        delete_described(mirror, doc_id)
        delete_links(mirror, doc_id)
    for path in rp.state_files:
        path.unlink(missing_ok=True)


def systems_for(
    entry: SourceEntry | None, cursor_slot: Path, chunk_slot: Path
) -> frozenset[str]:
    """The entry's systems; else the cursor's stamped systems and the system
    prefixes of the chunk record's admitted doc_ids. A slot that cannot be
    read contributes nothing: it is still removed, and resetting is exactly
    what a torn slot calls for."""
    if entry is not None and entry.systems:
        return frozenset(entry.systems)
    systems: set[str] = set()
    try:
        cursor = Cursor.model_validate_json(cursor_slot.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    else:
        systems.update(cursor.systems)
    try:
        record = read_record(chunk_slot)
    except ChunkRecordError:
        record = None
    if record is not None:
        systems.update(doc_id.partition(":")[0] for doc_id in record.admitted)
    return frozenset(systems)
