"""The canonical mirror and the read-only diff: architecture §7's pipeline
sequence, split into a pure `diff` and a success-only `commit`."""

from __future__ import annotations

import hashlib
from pathlib import Path

from kbforge.models import CanonicalDocument, ChangeSet


def slot_key(doc_id: str) -> str:
    """The on-disk name for a doc_id. Public so the grounding sidecar names its
    files identically -- two hashing schemes would silently orphan sidecars."""
    return hashlib.sha256(doc_id.encode("utf-8")).hexdigest()


def _slot(mirror: Path, doc_id: str) -> Path:
    return mirror / f"{slot_key(doc_id)}.json"


def _load(mirror: Path, doc_id: str) -> CanonicalDocument | None:
    slot = _slot(mirror, doc_id)
    if not slot.exists():
        return None
    return CanonicalDocument.model_validate_json(slot.read_text("utf-8"))


def load_all(mirror: Path) -> list[CanonicalDocument]:
    """Every document the mirror currently holds, sorted by doc_id.

    Tombstoned documents are absent by construction: commit() unlinks their
    slot, so the mirror only ever stores live documents.
    """
    if not mirror.is_dir():
        return []
    docs = [
        CanonicalDocument.model_validate_json(slot.read_text("utf-8"))
        for slot in sorted(mirror.glob("*.json"))
    ]
    return sorted(docs, key=lambda d: d.doc_id)


def diff(mirror: Path, docs: list[CanonicalDocument]) -> ChangeSet:
    """Read-only comparison against the mirror. Deletions are explicit tombstones
    (`deleted=True`); absence never implies one (§4.2). Never mutates the mirror."""
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for doc in docs:
        prev = _load(mirror, doc.doc_id)
        if doc.deleted:
            if prev is not None:
                removed.append(doc.doc_id)
            continue
        if prev is None:
            added.append(doc.doc_id)
        elif prev.anchor.content_hash != doc.anchor.content_hash:
            modified.append(doc.doc_id)
        else:
            unchanged += 1
    return ChangeSet(
        added=sorted(added),
        modified=sorted(modified),
        removed=sorted(removed),
        unchanged_count=unchanged,
    )


def commit(mirror: Path, docs: list[CanonicalDocument]) -> None:
    """Advance the mirror to the fetched state. Called only after a run fully
    succeeds, so a failed publish never leaves the mirror ahead of the bundle."""
    mirror.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        slot = _slot(mirror, doc.doc_id)
        if doc.deleted:
            slot.unlink(missing_ok=True)
        else:
            slot.write_text(doc.model_dump_json(), "utf-8")
