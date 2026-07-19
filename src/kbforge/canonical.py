"""Canonicalization: stable content hashing and the §4.3 law-1 stability check."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from kbforge.models import CanonicalDocument, RawRecord


class StabilityError(RuntimeError):
    """normalize() produced different canonical content for identical input."""


def content_hash(doc: CanonicalDocument) -> str:
    """SHA-256 over the canonical CONTENT — everything the diff must react to.
    The anchor is excluded: `retrieved_at` is volatile (§4.3 law 2) and the
    anchor's own content_hash would be circular."""
    payload = {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "text": doc.text,
        "structured": doc.structured,
        "relations": sorted(doc.relations),
        "deleted": doc.deleted,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_stability(
    normalize: Callable[[Sequence[RawRecord]], list[CanonicalDocument]],
    records: Sequence[RawRecord],
) -> None:
    """§4.3 law 1: normalize twice over identical input, require identical content
    hashes. A connector that fails is not deterministic and must be rejected."""
    first = [content_hash(d) for d in normalize(records)]
    second = [content_hash(d) for d in normalize(records)]
    if first != second:
        raise StabilityError("normalize() is not deterministic over identical input")
