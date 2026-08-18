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
from dataclasses import dataclass

from mcp.types import CallToolResult, EmbeddedResource, ResourceLink, TextContent

from kbforge.models import RawRecord
from kbforge_mcp.config import IdsMapping, ReadSpec
from kbforge_mcp.slug import SlugError, native_id_for


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
    duplicate the "does this id look like a url" predicate below AND skip this
    SlugError -> MappingError conversion, leaving a bare RuntimeError where
    every other unmappable id raises MappingError.
    """
    try:
        native = native_id_for(raw_id)
    except SlugError as exc:
        raise MappingError(f"unusable document id: {exc}") from exc
    return DocRef(
        raw_id=raw_id,
        native_id=native,
        url=raw_id if "://" in raw_id else None,
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


def refs_from_select(result: CallToolResult, ids: IdsMapping | None) -> list[DocRef]:
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
        if ids is not None:
            # An explicitly configured `ids` mapping is the operator telling us
            # where the rows live; believe it regardless of the key set's shape.
            # `ids.list` may legitimately be named `"result"` -- that name isn't
            # reserved to the SDK's auto-wrap, so gating tier 2 on the key set
            # here would refuse a valid config with a false "not mappable"
            # message. The auto-wrap heuristic below is for the *unconfigured*
            # case only, where there is no operator intent to defer to.
            rows = result.structured_content.get(ids.list)  # tier 2
            if rows is None:
                raise MappingError(
                    f"select response has no {ids.list!r} key; keys are "
                    f"{sorted(result.structured_content)}"
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
                    raise MappingError(
                        f"select result row has no {ids.id!r} key: {row!r}"
                    )
                refs.append(
                    ref_for(str(raw), row.get(ids.title) if ids.title else None)
                )
            return refs

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
                payload = _text_payload(base64.b64decode(blob), ref, uri, res)
            else:
                continue  # a bare link with no content is not a document
            carried.append((uri, payload, getattr(res, "mime_type", None)))

        # One document in, one document out: the identity we ASKED for wins. A
        # server's own uri may encode volatile state -- GitHub returns
        # `repo://owner/repo/sha/<commit-sha>/contents/<path>`, and slugging that
        # would put a commit sha inside every native_id, so identity would churn
        # on every commit and nothing would ever diff as `modified`.
        # Only a one-to-many read (a "read this folder" tool) needs new
        # identities, and then the uris are the only source for them.
        if len(carried) == 1:
            uri, payload, mime = carried[0]
            return [
                record(payload, ref.native_id, ref.url, mime or media_type, ref.title)
            ]
        if carried:
            records = []
            for uri, payload, mime in carried:
                # Bind once: `ref_for` already computes the same "does this
                # id look like a url" predicate that decides `.url`, and a
                # second copy of that predicate inline is a second place for
                # the two to drift.
                r = ref_for(uri, None)
                # Every field comes from the per-document ref, title included.
                # `EmbeddedResource` carries no title of its own, so `r.title`
                # is None and `kbforge_normalize` derives a per-document title
                # from that document's native_id -- whereas reusing `ref.title`
                # here would stamp the *folder's* title onto all five documents
                # a "read this folder" call returned.
                records.append(
                    record(
                        payload,
                        r.native_id,
                        r.url or ref.url,
                        mime or media_type,
                        r.title,
                    )
                )
            return records

    if spec.text_key and result.structured_content is not None:  # tier 2
        body = result.structured_content.get(spec.text_key)
        # `None` and an empty string are both "no body"; an int `0` or bool
        # `False` are real content that happens to stringify to `"0"` /
        # `"False"`, so the truthiness check only applies once a str is
        # already established (the type guard), keeping tier 3's "an empty
        # read is an error, not an empty document" rule consistent here too.
        if body is None or (isinstance(body, str) and not body):
            raise MappingError(
                f"read response has no {spec.text_key!r} key for {ref.native_id}"
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
    if texts:
        return [
            record(
                "\n\n".join(texts).encode("utf-8"),
                ref.native_id,
                ref.url,
                media_type,
                ref.title,
            )
        ]

    raise MappingError(f"read response for {ref.native_id} carried no content")
