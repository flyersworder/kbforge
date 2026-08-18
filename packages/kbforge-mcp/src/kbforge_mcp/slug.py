"""Reduce a server-supplied document id to a path-safe `native_id`.

Server ids are frequently URLs. `synthesize.concept_path` builds a bundle path
straight from `native_id` (`concepts/{stem}/overview.md`), so a raw URL renders
to `concepts/https:/docs.aws.amazon.com/...`, and a `../..` id reaches
`safe_join` and dies as a PathError at publish time -- after synthesis, after
tokens. Both are refused here instead.

Identity and provenance are different things and `ResourceAnchor` has a field
for each: this produces `native_id`, while the untouched original becomes `url`.

**The property this function must hold is injectivity.** `doc_id` is
`f"{system}:{native_id}"`, so two source ids that reduce to one slug become one
doc_id, and `assert_fetch_contract` then aborts the *whole run* with "duplicate
doc_id" -- on every sync, unrecoverable without changing the source. Distinct
ids must therefore produce distinct slugs, or be refused outright.

The slug is not the level the property ultimately has to hold at, though, and a
test over slugs alone cannot see the level that matters. Two *distinct*
native_ids still become one published file if `concept_path` and `safe_join`
normalize them together downstream -- three distinct doc_ids, three
`change.files` keys, every core gate green, and then
`{safe_join(base, rel): body ...}` collapses them onto one key and the last
write wins. So the property is asserted end to end, over the published path, in
`test_distinct_ids_never_reduce_to_one_published_file`; everything below exists
to make that true.

Four equivalences are deliberate, and each one merges ids that name the *same
document*:

1. **A trailing document extension** on the final path segment (`policy` and
   `policy.md`). This is the rule this function was built with: it normalises
   the extension a server happens to serve, and turns the pair into one doc_id
   so the fetch-side law reports a loud duplicate rather than `concept_path`
   silently collapsing them one stage later. The extension set is explicit
   (`_DOCUMENT_EXTENSIONS`) precisely so it stays an *extension* rule: the
   pattern it replaced (`\\.[A-Za-z0-9]{1,5}\\Z`) also ate a trailing version or
   date, so `docs/spec-1.2` and `docs/spec-1.3` both became `docs/spec-1`, and
   `reports/2024.10` and `reports/2024.11` both became `reports/2024`.
2. **A URL's scheme, and the case of its host** (RFC 3986 §6.2.2): the same
   host over http and https is one document, and DNS is case-insensitive. The
   fold is applied to the host alone and not to the whole authority, because
   userinfo is not case-insensitive -- lowercasing the netloc whole would have
   merged `USER:PASS@a.com` with `user:pass@a.com`, an equivalence RFC 3986
   does not grant and this docstring did not claim. Everything else a URL
   carries -- host, port, userinfo, path, query, fragment -- is identity.
3. **POSIX path normalisation**: empty, repeated, leading and `.` segments do
   not name anything, so `/a//b` and `a/b` are the same path.
4. **Whitespace around the id as a whole**, which is transport noise rather
   than a name.

Everything else -- including the characters that are not path-safe -- is
preserved through the escape below rather than dropped or remapped, because
every lossy remapping reintroduces the bug: `:` -> `/` makes `A:b` collide with
the literal `A/b`, and `:` -> `-` makes it collide with `A-b`.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

# Stripped only for the text document formats this pipeline can actually
# synthesize from -- a binary payload is refused at the mapping seam anyway, so
# `.pdf` is identity here rather than an extension to normalise. An explicit set
# is also what core already does: `concept_path` strips exactly `.md`. Matched
# case-insensitively; a server that serves `.HTML` is serving the same format.
_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".txt",
        ".rst",
        ".adoc",
        ".asciidoc",
        ".htm",
        ".html",
        ".xhtml",
    }
)

# Percent-escaped, per segment, because a native_id becomes a path in a git
# repository that gets checked out on Windows and macOS as well as Linux:
# `<>:"|?*\` and the control characters are illegal in an NTFS filename, so a
# bundle containing one cannot be checked out at all. `:` is the one that
# matters in practice -- a namespaced id (`SPACE:page.md`, which is how
# Confluence names a page) is not a URL and must keep its namespace, and
# Windows is exactly why it cannot keep it as a literal `:`.
#
# `%` is escaped too, and that is what makes the escape injective rather than
# merely tidy: without it a literal `%3A` in a source id would be
# indistinguishable from an escaped `:`. `@` is escaped for the same reason --
# it is reserved below as the marker for the host segment, so no ordinary
# segment may begin with one.
#
# `/` is escaped for a different reason: structure. `_escape` only ever sees a
# netloc or an already-split path segment, neither of which can contain a `/`,
# so the only `/` it can meet is one that arrived inside a query or fragment --
# and splicing that in raw made the tail *structural* rather than opaque. It
# injected real path separators into the identity, which (a) collapsed
# `?x=/y`, `?x=//y` and `?x=/./y` onto one published file at `safe_join`'s
# `normpath`, invisibly to every gate before it, (b) let `?x=/../../secrets`
# past the `..` check below -- which runs on segments split from the *path*,
# before the tail exists -- and back into the publish-time PathError this whole
# module exists to prevent, and (c) put the trailing-dot rule out of reach of
# every piece but the last. Escaping it keeps the tail text.
#
# Consciously NOT handled: Windows reserved device names (`CON`, `NUL`,
# `COM1`...) also break a checkout, but they break it as *names*, not as
# identities, and core's own `concept_path` and `local_files` do not guard them
# either. Guarding them here only would put the line in a different place for
# one connector than for the library.
_ESCAPE = re.compile(r'[\x00-\x1f\x7f%@/<>:"|?*\\]')

_UNSAFE_TAIL = (".", " ")


class SlugError(ValueError):
    """A server-supplied id cannot be reduced to a path-safe native_id."""


def is_url(raw: str) -> bool:
    """True when an id is a URL, meaning it has *both* a scheme and an
    authority.

    `urlsplit` reads any leading `word:` as a scheme, so a bare `SPACE:page.md`
    parses with `scheme='space'` and an empty path -- treating that as a URL is
    what discarded the namespace of every non-URL namespaced id. Requiring the
    authority as well is the whole distinction.

    Public because `mapping.ref_for` decides `DocRef.url` with the same
    question, and two copies of the predicate are two places for it to drift.
    """
    parts = urlsplit(raw.strip())
    return bool(parts.scheme and parts.netloc)


def _escape(segment: str) -> str:
    escaped = _ESCAPE.sub(lambda m: f"%{ord(m.group()):02X}", segment)
    # Windows also refuses a name *ending* in a dot or a space, and silently
    # trims them on some paths, which would merge two distinct segments.
    if escaped[-1:] in _UNSAFE_TAIL:
        escaped = f"{escaped[:-1]}%{ord(escaped[-1]):02X}"
    return escaped


def _strip_document_extension(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    if dot and f".{ext.lower()}" in _DOCUMENT_EXTENSIONS:
        return stem
    return name


def native_id_for(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SlugError(f"document id is empty: {raw!r}")
    text = raw.strip()
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        # The host is part of the document's identity: the same path on two
        # hosts is two documents, and building identity from `parts.path` alone
        # merged them. The `@` prefix keeps URL identities and plain-path
        # identities disjoint -- without it the URL `https://a.com/docs/x` and
        # the plain id `a.com/docs/x` would both slug to `a.com/docs/x`.
        # Lowercased on the HOST only. `netloc.lower()` folds userinfo too,
        # merging `USER:PASS@a.com` with `user:pass@a.com` -- an equivalence
        # RFC 3986 §6.2.2 does not grant, since only scheme and host are
        # case-insensitive. The port is digits, so folding it is a no-op.
        userinfo, at, hostport = parts.netloc.rpartition("@")
        host: str | None = f"{userinfo}{at}{hostport.lower()}"
        path = parts.path
        # Query and fragment are identity too, carried on the final segment.
        # Dropping them (as this did) merges `page?v=1` with `page?v=2`. The
        # trade-off runs the other way for a server that hands back
        # session-scoped urls: their identity would churn every run. That is the
        # better failure of the two -- a churning id shows up as a stream of new
        # concepts an operator can see and fix in the source, where a merge
        # aborts the run outright. It is also not the live case: AWS's
        # `search_documentation` appends `?session=...&query_id=...` when it
        # FETCHES a page, not to the ids it returns (verified live -- the
        # native_ids it produces carry no query at all).
        tail = f"?{parts.query}" if parts.query else ""
        tail += f"#{parts.fragment}" if parts.fragment else ""
    else:
        # Not a URL: the whole string is the path, `:` and all.
        host, path, tail = None, text, ""

    segments = [s for s in path.split("/") if s not in ("", ".")]
    # A literal `..` segment and a percent-encoded one (`%2e%2e`) are both
    # refused. Neither git nor the filesystem decodes percent-encoding today,
    # so `%2e%2e` isn't currently exploitable -- it just produces an oddly
    # named directory -- but this function is the one thing standing between
    # a server-controlled id and a path escape, so the guarantee should hold
    # even if that stops being true. The *stored* identity is deliberately
    # never the decoded form: decoding would silently change identity for a
    # legitimate id like `report%20final.md` (-> `report final.md`), and
    # identity stability is what the mirror/diff design rests on. This check
    # only ever looks at the decoded form; the escape below is applied to the
    # literal one (so that id becomes `report%2520final`).
    if any(s == ".." or unquote(s) == ".." for s in segments):
        raise SlugError(f"document id escapes the bundle: {raw!r}")

    if segments:
        # Extension-stripping the final segment must leave *something*: a raw id
        # whose only content is an extension (".md", "docs/.md") is refused the
        # same way an empty id is, rather than falling back to the untouched
        # segment -- that fallback is what let ".md" through to collide with the
        # empty-id case on `concepts//overview.md`. Only the final segment is
        # checked: earlier segments are directory-like and untouched by
        # extension-stripping, so `docs/.gitignore/notes.md` is unaffected --
        # `.gitignore` never reaches this rule as the segment being stripped.
        stripped = _strip_document_extension(segments[-1])
        if not stripped:
            raise SlugError(f"document id has no content beyond its extension: {raw!r}")
        segments[-1] = stripped + tail
    elif tail:
        segments = [tail]
    if not segments and host is None:
        raise SlugError(f"document id has no path content: {raw!r}")

    out = [] if host is None else [f"@{_escape(host)}"]
    out.extend(_escape(s) for s in segments)
    slug = "/".join(out)
    # `concept_path` runs `native.removesuffix(".md")` on the whole native_id,
    # so a slug ending in `.md` gets stripped a SECOND time downstream, one
    # stage after this function's own extension rule and outside its reach.
    # That is the same silent two-documents-one-file collapse as the tail bug:
    # `docs/a.md.md` slugs to `docs/a.md` and `docs/a.md` slugs to `docs/a`,
    # two distinct doc_ids that `concept_path` then publishes to one path. A
    # query tail can produce the suffix too (`?x=a.md`). Escaping the dot ends
    # it: identity is settled here, and nothing downstream gets a second bite.
    # Injective, because no unescaped slug can end in `%2Emd` -- `%` is
    # escaped, and the only `%2E` this module emits is a segment's final
    # character.
    if slug.endswith(".md"):
        slug = f"{slug[:-3]}%2Emd"
    return slug
