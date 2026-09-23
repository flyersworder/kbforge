"""The `_described/` cache: what a model wrote for a document, keyed by its
content hash, so a concept re-rendered without a source change (a referrer, an
arrival) reuses it rather than calling the model and churning the frontmatter.

Same rules as the grounding sidecar: written by the pipeline after a successful
publish, atomically; read tolerantly, because an unreadable record is a cache
miss whose repair -- describe again -- is exactly what a miss does."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from kbforge.grounding import write_atomic
from kbforge.mirror import slot_key
from kbforge.models import DescribedRecord

DESCRIBED_DIR = "_described"
"""A subdirectory: `load_all` globs `mirror/*.json`."""


def _path(mirror: Path, doc_id: str) -> Path:
    return mirror / DESCRIBED_DIR / f"{slot_key(doc_id)}.json"


def read_described(mirror: Path, doc_id: str) -> DescribedRecord | None:
    path = _path(mirror, doc_id)
    if not path.exists():
        return None
    try:
        return DescribedRecord.model_validate(json.loads(path.read_text("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return None


def write_described(mirror: Path, record: DescribedRecord) -> None:
    write_atomic(_path(mirror, record.doc_id), record.model_dump())


def delete_described(mirror: Path, doc_id: str) -> None:
    _path(mirror, doc_id).unlink(missing_ok=True)
