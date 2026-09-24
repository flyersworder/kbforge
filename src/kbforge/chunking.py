"""Chunked review for oversized runs (design/2026-09-19-chunked-review-design.md).

Admission decides which changed documents a run publishes now; the chunk record
says what to put back if a reviewer asks for that chunk to be redone. Admission
is pure. The record functions are the only I/O here."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.described import DESCRIBED_DIR
from kbforge.grounding import FIRST_SEEN_DIR, SIDECAR_DIR
from kbforge.links import LINKS_DIR
from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument


class ChunkingConfig(BaseModel):
    """`extra="forbid"` so a typo'd key is an error rather than no cap at all."""

    model_config = ConfigDict(extra="forbid")

    max_concepts: int = Field(ge=1)
    group_by: str | None = None


def load_chunking(path: Path | None) -> ChunkingConfig | None:
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return ChunkingConfig.model_validate(raw)


def _group_key(doc: CanonicalDocument, group_by: str | None) -> tuple[bool, str]:
    """Missing keys sort last (`True` after `False`), then by the value's text."""
    if group_by is None:
        return (False, "")
    value = doc.structured.get(group_by)
    return (value is None, "" if value is None else str(value))


def admit(
    docs: list[CanonicalDocument], cfg: ChunkingConfig, capacity: int
) -> tuple[list[CanonicalDocument], bool]:
    """The first chunk of `docs` that fits in `capacity`, and whether any were
    left over (§4).

    Whole groups are packed in key order while they fit; packing stops at the
    first group that does not, so a group is never split across chunks unless
    it cannot fit an empty one. A group that cannot is split by doc_id and
    fills the chunk by itself. Sorted throughout, so a re-run of the same
    change admits the same chunk."""
    groups: dict[tuple[bool, str], list[CanonicalDocument]] = {}
    for doc in docs:
        groups.setdefault(_group_key(doc, cfg.group_by), []).append(doc)
    admitted: list[CanonicalDocument] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda d: d.doc_id)
        room = capacity - len(admitted)
        if len(group) <= room:
            admitted += group
            continue
        if not admitted:
            admitted = group[: max(room, 0)]
        break
    return admitted, len(admitted) < len(docs)


class ChunkRecord(BaseModel):
    """The last chunk a connector instance published (§5): enough to wait on
    its review request and to roll it back."""

    model_config = ConfigDict(extra="forbid")

    branch_hints: list[str]
    pending: bool
    """True when that publish left a backlog, so the next run must wait."""
    admitted: list[str]
    mirror: dict[str, str | None]
    """Mirror-relative path -> content before the chunk's commit; None = absent."""
    cursor: str | None
    """The cursor slot's content before the chunk; None = absent."""


def merge_records(older: ChunkRecord, newer: ChunkRecord) -> ChunkRecord:
    """One record for two runs published into the same still-open request, so
    redo rolls back everything a reviewer discards by closing it (§7).

    Rolling back means reaching the state before the OLDER run: a path both
    runs touched keeps the older record's prior content, and the cursor is the
    older one. Whether a backlog remains is the newer run's to say."""
    return ChunkRecord(
        branch_hints=list(dict.fromkeys(older.branch_hints + newer.branch_hints)),
        pending=newer.pending,
        admitted=sorted(set(older.admitted) | set(newer.admitted)),
        mirror=dict(newer.mirror) | older.mirror,
        cursor=older.cursor,
    )


def owned_paths(doc_id: str) -> list[str]:
    """Every mirror-relative file a run writes or deletes on behalf of `doc_id`:
    its slot, its grounding sidecar, its first-seen record, its described record,
    its links sidecar."""
    name = f"{slot_key(doc_id)}.json"
    return [
        name,
        f"{SIDECAR_DIR}/{name}",
        f"{FIRST_SEEN_DIR}/{name}",
        f"{DESCRIBED_DIR}/{name}",
        f"{LINKS_DIR}/{name}",
    ]


def _read(path: Path) -> str | None:
    return path.read_text("utf-8") if path.exists() else None


def _put(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")


def snapshot(mirror: Path, doc_ids: set[str]) -> dict[str, str | None]:
    return {
        rel: _read(mirror / rel)
        for doc_id in sorted(doc_ids)
        for rel in owned_paths(doc_id)
    }


def restore(record: ChunkRecord, mirror: Path, cursor_slot: Path) -> None:
    for rel, content in sorted(record.mirror.items()):
        _put(mirror / rel, content)
    _put(cursor_slot, record.cursor)


class ChunkRecordError(RuntimeError):
    """A chunk record that exists but cannot be read, torn by an interrupted
    write or edited by hand. Carries the path so the CLI can name it."""

    def __init__(self, path: Path, error: Exception):
        super().__init__(f"chunk record {path}: {error}")
        self.path = path
        self.error = error


def read_record(path: Path) -> ChunkRecord | None:
    if not path.exists():
        return None
    try:
        return ChunkRecord.model_validate_json(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        # ValueError covers pydantic's ValidationError (bad JSON or bad shape)
        # and UnicodeDecodeError. Either way, guessing is worse than stopping:
        # a record that cannot be read cannot be waited on or rolled back.
        raise ChunkRecordError(path, exc) from exc


def write_record(path: Path, record: ChunkRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(), "utf-8")
