"""Turn MCP tool results into DocRefs and RawRecords.

Protocol-first: MCP's own content-block types are the mapping vocabulary, so the
common case needs no configuration. Tiers are tried in order and the first that
applies wins.

The two stages carry very different burdens, because **identity is an input to
the reader, not an output of it**. A selector must produce ids it does not
already know, so a bare-prose response is unmappable and fails closed. A reader
is called with an id we already hold, so its response only has to supply bytes --
which makes "concatenate the text blocks" deterministic rather than a heuristic.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from mcp.types import CallToolResult, EmbeddedResource, ResourceLink, TextContent

from kbforge.models import RawRecord
from kbforge_mcp.config import IdsMapping, ReadSpec
from kbforge_mcp.slug import SlugError, is_url, native_id_for


class MappingError(RuntimeError):
    """A tool result cannot be mapped onto the fields kbforge needs."""


@dataclass(frozen=True)
class DocRef:
    """One selected document.

    `raw_id` is what the reader must be passed; `native_id` is the path-safe slug
    identity is built from. They differ whenever the server's id is a URL, and
    passing the slug back to the reader is the mistake this split exists to make
    impossible to write by accident.
    """

    raw_id: str
    native_id: str
    url: str | None
    title: str | None


def _resource_blocks(result: CallToolResult) -> list[EmbeddedResource | ResourceLink]:
    return [
        b for b in result.content if isinstance(b, (EmbeddedResource, ResourceLink))
    ]


def _text_blocks(result: CallToolResult) -> list[str]:
    return [b.text for b in result.content if isinstance(b, TextContent)]


def ref_for(raw_id: str, title: str | None) -> DocRef:
    """The one place a document id becomes a DocRef.

    Public because `selectors` needs it too: a hand-built DocRef there would
    duplicate the "does this id look like a url" predicate (`slug.is_url`)
    AND skip this
    SlugError -> MappingError conversion, leaving a bare RuntimeError where
    every other unmappable id raises MappingError.
    """
    try:
        native = native_id_for(raw_id)
    except SlugError as exc:
        raise MappingError(f"unusable document id: {exc}") from exc
    # `slug.is_url` and not a local `"://" in raw_id`: the slug builds identity
    # differently for a URL than for a plain id (a URL keeps its host), so a
    # second, looser predicate here would call something a URL for provenance
    # that identity treated as a plain path.
    return DocRef(
        raw_id=raw_id,
        native_id=native,
        url=raw_id if is_url(raw_id) else None,
        title=title,
    )


def _is_scalar_wrapped(structured_content: dict) -> bool:
    """True when `structuredContent` is the MCP SDK's auto-wrap of a
    non-object return value (`-> str`, `-> int`, ...) rather than genuine
    tier-2 data a select tool's author actually intended to be selected from.

    The SDK wraps any non-object return under the single key `"result"`, so
    that key name is the first signal. The key name alone isn't sufficient,
    though: `"result"` isn't reserved, and a real search tool could legitimately
    use it as its `ids.list` name (an operator who configures `ids: {list:
    "result", ...}` has said so explicitly, and that path never reaches this
    function -- see `refs_from_select`). What actually distinguishes the
    auto-wrap is the *value's* shape: the SDK wraps a scalar return, so the
    value under `"result"` is never itself a list there, whereas a genuine
    row list -- whatever key it lands under -- is a list by construction. A
    `"result"` key holding a list is therefore treated as real, only a
    `"result"` key holding something else (a string, in every case this
    connector exercises) is treated as the auto-wrap costume.
    """
    return set(structured_content) == {"result"} and not isinstance(
        structured_content["result"], list
    )


def _refs_from_rows(structured_content: dict, ids: IdsMapping) -> list[DocRef]:
    """Tier 2: the rows an operator's `ids` mapping points at."""
    rows = structured_content.get(ids.list)
    if rows is None:
        raise MappingError(
            f"select response has no {ids.list!r} key; keys are "
            f"{sorted(structured_content)}"
        )
    if not isinstance(rows, list):
        raise MappingError(f"select response key {ids.list!r} is not a list")
    refs = []
    # An empty `rows` list is legal and returns `[]`, not an error: a
    # zero-hit query result is a real state, and raising here would turn
    # an ordinary no-op run into an aborted one. This is safe only
    # because a query selector always yields `complete=False`, and
    # `assert_fetch_contract` refuses a tombstone (an implied
    # corpus-wide deletion) under `complete=False`; the other half of
    # the hazard -- a static selector configured with zero ids -- is
    # already closed by `problems_for` rejecting an empty `static_ids`.
    for row in rows:
        raw = row.get(ids.id) if isinstance(row, dict) else None
        if raw is None:
            raise MappingError(f"select result row has no {ids.id!r} key: {row!r}")
        refs.append(ref_for(str(raw), row.get(ids.title) if ids.title else None))
    return refs


def refs_from_select(result: CallToolResult, ids: IdsMapping | None) -> list[DocRef]:
    if ids is not None and result.structured_content is not None:
        # A CONFIGURED `ids` mapping is operator intent and outranks inference,
        # even the protocol's own. Tier 1 used to win unconditionally, so a
        # response carrying both resource blocks and rows discarded the mapping
        # with no error and no warning -- the inverse of the rule applied a few
        # lines down, where a configured `ids` is believed over the auto-wrap
        # shape heuristic precisely because the operator said so. Both readings
        # cannot be right; this is the one that keeps a configured mapping
        # meaningful wherever it can apply. Protocol-first still holds for the
        # unconfigured case, which is every case the mapping was designed for.
        return _refs_from_rows(result.structured_content, ids)

    blocks = _resource_blocks(result)
    if blocks:  # tier 1 -- the protocol already carries the identity
        refs = []
        for i, b in enumerate(blocks):
            uri = getattr(b, "uri", None) or getattr(
                getattr(b, "resource", None), "uri", None
            )
            if uri is None:
                raise MappingError(
                    f"resource block {i} (type={b.type!r}) carries no uri"
                )
            # ResourceLink carries both; `title` is the human-facing one.
            refs.append(
                ref_for(str(uri), getattr(b, "title", None) or getattr(b, "name", None))
            )
        return refs

    if result.structured_content is not None:
        # `ids` is None here: the branch at the top of this function took every
        # configured case. `ids.list` may legitimately be named `"result"` --
        # that name isn't reserved to the SDK's auto-wrap -- which is why the
        # heuristic below is for the UNCONFIGURED case only, where there is no
        # operator intent to defer to.
        if not _is_scalar_wrapped(result.structured_content):
            # structuredContent IS present here -- the fix is a configured
            # `ids` mapping, not `static_ids`. Conflating the two messages
            # steers an operator with a real search tool towards the wrong
            # remedy; keep this branch distinct from the tier-3 case below.
            raise MappingError(
                "select response carries structuredContent with keys "
                f"{sorted(result.structured_content)} but no 'ids' mapping is "
                "configured -- add 'ids' to the select spec"
            )
        # scalar-wrapped structuredContent with no `ids` configured is exactly
        # a prose tool wearing a structuredContent-shaped costume -- fall
        # through to the tier-3 message below, which names the right remedy.

    # tier 3 -- fails closed. No "first text block", no regex over an outline.
    raise MappingError(
        "select response carries neither resource blocks nor structuredContent; "
        "a prose-only select tool is not mappable -- configure 'static_ids' instead"
    )


def _decode_blob(blob: str, ref: DocRef, uri: str) -> bytes:
    """`b64decode` raises `binascii.Error` (a ValueError, NOT a MappingError) on
    a padding failure, and `_fetch` catches only `(ToolCallFailed, MappingError)`
    -- so one corrupt blob propagated straight out of `kbforge_fetch` and killed
    the run, the exact opposite of the per-document degradation `_text_payload`
    argues for below. Converted here so a corrupt document is skipped and
    `complete` degrades, like every other per-document failure.

    `validate=True`, because the default silently DROPS every character outside
    the base64 alphabet: a truncated or corrupted blob decodes to plausible-
    looking garbage and ships as a document instead of being reported. Whitespace
    is stripped first rather than rejected -- line-wrapping base64 is an ordinary
    encoding convention, not corruption, and `validate=True` would otherwise
    refuse a perfectly good wrapped blob.
    """
    try:
        return base64.b64decode("".join(blob.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MappingError(
            f"read response for {ref.native_id} carried a resource at {uri} "
            f"whose blob is not valid base64 ({exc}); the document is skipped "
            "rather than the run aborted"
        ) from exc


def _text_payload(payload: bytes, ref: DocRef, uri: str, res: object) -> bytes:
    """Refuse a blob whose bytes are not text, here rather than in normalize.

    `BlobResourceContents` is base64, not necessarily text -- GitHub's
    `get_file_contents` returns one for any binary file. `kbforge_normalize`
    decodes every payload as utf-8 (architecture §4.3 makes it pure, so it has
    nowhere to put a failure), and a PNG reaching it raises `UnicodeDecodeError`
    out of the whole run. Failing here instead makes it a `MappingError`, which
    `_fetch` already catches per document: that document is skipped and
    `complete` degrades to False, which is the connector's established posture
    for a per-document failure and strictly better than aborting a run over one
    image. This is deliberately not binary support -- a text-concept pipeline
    has nothing to synthesize from a PNG.
    """
    try:
        payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        mime = getattr(res, "mime_type", None)
        raise MappingError(
            f"read response for {ref.native_id} carried a non-text resource at "
            f"{uri} (mime_type={mime!r}): its bytes are not valid UTF-8 "
            f"({exc.reason} at byte {exc.start}); kbforge synthesizes text "
            "concepts and cannot ingest binary content"
        ) from exc
    return payload


def records_from_read(
    result: CallToolResult,
    ref: DocRef,
    spec: ReadSpec,
    media_type: str,
) -> list[RawRecord]:
    def record(
        payload: bytes,
        native_id: str,
        url: str | None,
        mtype: str,
        title: str | None,
    ) -> RawRecord:
        return RawRecord(
            anchor_hint={"native_id": native_id, "url": url, "title": title},
            media_type=mtype,
            payload=payload,
        )

    blocks = _resource_blocks(result)
    if blocks:  # tier 1 -- one call may legitimately yield many documents
        carried = []
        for b in blocks:
            res = getattr(b, "resource", b)
            uri = str(getattr(res, "uri", ref.raw_id))
            text, blob = getattr(res, "text", None), getattr(res, "blob", None)
            if text is not None:
                payload = text.encode("utf-8")
            elif blob is not None:
                payload = _text_payload(_decode_blob(blob, ref, uri), ref, uri, res)
            else:
                continue  # a bare link with no content is not a document
            if not payload.strip():
                # "An empty read is an error, not an empty document" -- the rule
                # the tier-2 branch below cites as established. A resource block
                # that IS present and carries an empty string is the same
                # nothing as a response with no blocks at all, and it produced a
                # published concept with an empty body and no error anywhere.
                raise MappingError(
                    f"read response for {ref.native_id} carried an empty "
                    f"resource at {uri}; an empty read is an error, not an "
                    "empty document"
                )
            carried.append((uri, payload, getattr(res, "mime_type", None)))

        if not carried:
            # Every resource block was a bare link with no bytes. Falling
            # through to tier 3 here made a sibling TextContent the document,
            # and GitHub's `get_file_contents` on a *directory* returns exactly
            # that shape: one ResourceLink per entry plus a prose preamble, so
            # the preamble shipped as the document body.
            #
            # This reverses the earlier ruling recorded on
            # `test_a_contentless_link_is_refused_rather_than_falling_through`:
            # the fallthrough was pinned rather than refused because refusing
            # might break a server that returns a link alongside its content as
            # text, and there was no evidence either way. The evidence arrived
            # with the live GitHub test. GitHub's *file* read returns a resource
            # block that CARRIES content, so tier 1 fires correctly there and
            # this refusal cannot reach it; its *directory* read is the harmful
            # shape. A response that announced resources and then carried none
            # of their bytes is not a document.
            raise MappingError(
                f"read response for {ref.native_id} carried "
                f"{len(blocks)} resource block(s), none of which had content "
                "(every one was a bare link); a link is a pointer, not a "
                "document -- a directory listing cannot be read as one"
            )
        if len(carried) > 1:
            # ONE DOCUMENT IN, ONE DOCUMENT OUT -- and a count can no longer
            # decide otherwise. The single-resource branch below exists because
            # a server's own uri may encode volatile state: GitHub returns
            # `repo://owner/repo/sha/<commit-sha>/contents/<path>`, and slugging
            # that puts a commit sha inside `native_id`, so identity churns on
            # every commit, every document diffs as `added` and never
            # `modified`, and stale concepts pile up with no tombstone to remove
            # them. Selecting that branch by `len(carried) == 1` meant a single
            # extra content-bearing resource silently flipped the REQUESTED
            # document onto uri-derived identity too -- the precise failure the
            # branch was written to prevent.
            #
            # A response cannot reliably tell "your document, plus extras" from
            # "the contents of the container you asked for", and only the second
            # licenses deriving identity from uris. So this fails closed rather
            # than guessing, which is this design's posture everywhere else
            # (a prose-only selector, a non-text blob, a bodiless read). No
            # configured source needs one-to-many today, and the branch that
            # handled it had never run against a real server -- GitHub's file
            # read carries exactly one resource, AWS carries none. A source that
            # genuinely reads containers should say so explicitly rather than be
            # inferred from a count; nothing here forecloses adding that, and
            # until a real server demonstrates the shape there is nothing to
            # design against.
            raise MappingError(
                f"read response for {ref.native_id} carried {len(carried)} "
                "content-bearing resources; a read is one document in, one "
                "document out, and which of these IS that document cannot be "
                "told from the response -- deriving identity from the server's "
                "uris would put volatile state (a commit sha) into every "
                "native_id. Point 'read.tool' at a single-document reader."
            )
        uri, payload, mime = carried[0]
        return [record(payload, ref.native_id, ref.url, mime or media_type, ref.title)]

    if spec.text_key and result.structured_content is not None:  # tier 2
        body = result.structured_content.get(spec.text_key)
        # `None` and a blank string are both "no body"; an int `0` or bool
        # `False` are real content that happens to stringify to `"0"` /
        # `"False"`, so the emptiness check only applies once a str is
        # already established (the type guard), keeping the "an empty read is
        # an error, not an empty document" rule consistent across all three
        # tiers. Whitespace-only counts as blank: `kbforge_normalize` strips
        # the text, so `"   "` publishes exactly the empty concept this rule
        # exists to refuse.
        if body is None or (isinstance(body, str) and not body.strip()):
            raise MappingError(
                f"read response has no {spec.text_key!r} key for {ref.native_id}"
            )
        if not isinstance(body, (str, int, float)):
            # Anything else -- a dict, a list -- would be published as its
            # PYTHON REPR by the `str()` below, and nothing downstream can tell
            # `"{'markdown': '...'}"` from a document. That is an ordinary
            # misconfiguration (`text_key: content` against
            # `{"content": {"markdown": "..."}}`), so it gets the MappingError
            # `_fetch` already degrades on rather than a corrupt concept.
            # `bool` passes as a subclass of int, deliberately: see above.
            raise MappingError(
                f"read response key {spec.text_key!r} for {ref.native_id} holds "
                f"a {type(body).__name__}, not a document body: {body!r:.80}. "
                "Point 'text_key' at the key holding the text itself."
            )
        return [
            record(
                str(body).encode("utf-8"),
                ref.native_id,
                ref.url,
                media_type,
                ref.title,
            )
        ]

    texts = _text_blocks(result)  # tier 3 -- complete, because identity is an input
    body = "\n\n".join(texts)
    # `texts` alone is the wrong test: `[TextContent(text="")]` is a TRUTHY list
    # holding an empty string, so a present-but-empty block passed straight
    # through as a document with `payload=b""`, which synthesized and published
    # an empty concept with nothing raising anywhere. The rule the tier-2 branch
    # above cites as established was enforced only there; it holds in all three
    # tiers now. Stripped for the same reason tier 2 strips.
    if body.strip():
        return [
            record(
                body.encode("utf-8"),
                ref.native_id,
                ref.url,
                media_type,
                ref.title,
            )
        ]

    raise MappingError(
        f"read response for {ref.native_id} carried no content"
        + (f" ({len(texts)} text block(s), all empty)" if texts else "")
    )
