"""Id column values -> a path-safe, injective native_id (spec §4.2).

The escape set is kbforge-mcp's (`kbforge_mcp/slug.py`, which documents each
character). It is copied rather than imported: kbforge-sql must not depend on
kbforge-mcp, and a shared helper belongs in core, which this release does not
touch. Each id value is ONE path segment, so none of slug.py's URL and path
handling applies -- only the per-segment escape and the two tail rules."""

from __future__ import annotations

import re
from collections.abc import Sequence

from kbforge.canonical import is_blank
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.values import Scalar

# `%` makes the escape injective; `/` keeps a value from becoming structure;
# the rest are illegal in an NTFS filename.
_ESCAPE = re.compile(r'[\x00-\x1f\x7f%/<>:"|?*\\]')
_UNSAFE_TAIL = (".", " ")


def _escape(part: str) -> str:
    escaped = _ESCAPE.sub(lambda m: f"%{ord(m.group()):02X}", part)
    # Windows refuses, and sometimes silently trims, a trailing dot or space.
    if escaped[-1:] in _UNSAFE_TAIL:
        escaped = f"{escaped[:-1]}%{ord(escaped[-1]):02X}"
    return escaped


def native_id_for(values: Sequence[Scalar], columns: Sequence[str]) -> str:
    parts: list[str] = []
    for column, value in zip(columns, values, strict=True):
        if value is None or (isinstance(value, str) and is_blank(value)):
            raise SqlSourceError(
                f"id column {column!r} is empty in a returned row; every row "
                "needs an id"
            )
        parts.append(_escape(str(value)))
    slug = "/".join(parts)
    # `concept_path` strips a `.md` suffix a second time downstream, which would
    # publish `x.md` and `x` to one file. Escaping the dot ends it.
    if slug.endswith(".md"):
        slug = f"{slug[:-3]}%2Emd"
    return slug
