"""Database value -> canonical JSON-safe form (spec §4.3).

Everything the diff hashes passes through here, so this is where §4.3 law 1
(determinism) is won or lost for a SQL source. Unknown types are rejected
rather than str()-ed: a repr can embed memory addresses or driver-specific
formatting, and a silently dropped column is a fact synthesis never sees."""

from __future__ import annotations

import math
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from kbforge_sql.errors import SqlSourceError

Scalar = str | int | bool | None


def canonical(value: object, column: str) -> Scalar:
    if value is None:
        return None
    # bool before int: bool is an int subclass, and True must not become 1.
    if isinstance(value, bool | int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            msg = f"column {column!r} holds a non-finite decimal {value}"
            raise SqlSourceError(msg)
        # normalize() drops trailing zeros (1.50 -> 1.5); format "f" undoes the
        # exponent normalize() may introduce (100 -> 1E+2 -> "100").
        return format(value.normalize(), "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SqlSourceError(f"column {column!r} holds a non-finite float {value}")
        return repr(value)
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", text).rstrip()
    # datetime before date: datetime is a date subclass.
    if isinstance(value, datetime):
        if value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
        # Naive stays naive: guessing a timezone would invent a fact.
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise SqlSourceError(
        f"column {column!r} holds a {type(value).__name__} value, which has no "
        "canonical text form; add it to 'exclude' or cast it in the query"
    )
