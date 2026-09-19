"""Chunked review for oversized runs (design/2026-09-19-chunked-review-design.md).

Admission decides which changed documents a run publishes now; the chunk record
says what to put back if a reviewer asks for that chunk to be redone. Admission
is pure. The record functions are the only I/O here."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

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
