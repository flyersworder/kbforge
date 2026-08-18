"""Reduce a server-supplied document id to a path-safe `native_id`.

Server ids are frequently URLs. `synthesize.concept_path` builds a bundle path
straight from `native_id` (`concepts/{stem}/overview.md`), so a raw URL renders
to `concepts/https:/docs.aws.amazon.com/...`, and a `../..` id reaches
`safe_join` and dies as a PathError at publish time -- after synthesis, after
tokens. Both are refused here instead.

Identity and provenance are different things and `ResourceAnchor` has a field
for each: this produces `native_id`, while the untouched original becomes `url`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A trailing extension only when it is short and alphanumeric, so `guide.html`
# loses its suffix but `api/v1.2/reference` keeps its version segment.
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


class SlugError(ValueError):
    """A server-supplied id cannot be reduced to a path-safe native_id."""


def native_id_for(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SlugError(f"document id is empty: {raw!r}")
    text = raw.strip()
    parts = urlsplit(text)
    # urlsplit already discards query and fragment into their own fields.
    path = parts.path if parts.scheme else text.split("?", 1)[0].split("#", 1)[0]

    segments = [s for s in path.split("/") if s not in ("", ".")]
    if ".." in segments:
        raise SlugError(f"document id escapes the bundle: {raw!r}")
    if not segments:
        raise SlugError(f"document id has no path content: {raw!r}")

    stripped = _EXTENSION.sub("", segments[-1])
    segments[-1] = stripped or segments[-1]
    return "/".join(segments)
