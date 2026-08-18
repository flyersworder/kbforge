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
from urllib.parse import unquote, urlsplit

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
    # A literal `..` segment and a percent-encoded one (`%2e%2e`) are both
    # refused. Neither git nor the filesystem decodes percent-encoding today,
    # so `%2e%2e` isn't currently exploitable -- it just produces an oddly
    # named directory -- but this function is the one thing standing between
    # a server-controlled id and a path escape, so the guarantee should hold
    # even if that stops being true. The *stored* identity is deliberately
    # left un-decoded: decoding would silently change identity for a
    # legitimate id like `report%20final.md` (-> `report final.md`), and
    # identity stability is what the mirror/diff design rests on. This check
    # only ever looks at the decoded form; it never uses it as the native_id.
    if any(s == ".." or unquote(s) == ".." for s in segments):
        raise SlugError(f"document id escapes the bundle: {raw!r}")
    if not segments:
        raise SlugError(f"document id has no path content: {raw!r}")

    # Extension-stripping the final segment must leave *something*: a raw id
    # whose only content is an extension (".md", "docs/.md") is refused the
    # same way an empty id is, rather than falling back to the untouched
    # segment -- that fallback is what let ".md" through to collide with the
    # empty-id case on `concepts//overview.md`. Only the final segment is
    # checked: earlier segments are directory-like and untouched by
    # extension-stripping, so `docs/.gitignore/notes.md` is unaffected --
    # `.gitignore` never reaches this regex as the segment being stripped.
    stripped = _EXTENSION.sub("", segments[-1])
    if not stripped:
        raise SlugError(f"document id has no content beyond its extension: {raw!r}")
    segments[-1] = stripped
    return "/".join(segments)
