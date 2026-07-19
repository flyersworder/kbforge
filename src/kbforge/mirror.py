"""The canonical mirror and the read-only diff (architecture §7's mirror_and_diff,
split into a pure `diff` and a success-only `commit`)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from kbforge.models import CanonicalDocument, ChangeSet


def _slot(mirror: Path, doc_id: str) -> Path:
    key = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
    return mirror / f"{key}.json"


def _load(mirror: Path, doc_id: str) -> CanonicalDocument | None:
    slot = _slot(mirror, doc_id)
    if not slot.exists():
        return None
    return CanonicalDocument.model_validate_json(slot.read_text("utf-8"))


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
