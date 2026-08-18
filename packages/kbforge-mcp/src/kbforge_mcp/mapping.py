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

from mcp.types import CallToolResult

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


def _resource_blocks(result: CallToolResult) -> list:
    return [
        b
        for b in result.content
        if getattr(b, "type", "") in ("resource", "resource_link")
    ]


def _text_blocks(result: CallToolResult) -> list[str]:
    return [b.text for b in result.content if getattr(b, "type", "") == "text"]


def _ref_for(raw_id: str, title: str | None) -> DocRef:
    try:
        native = native_id_for(raw_id)
    except SlugError as exc:
        raise MappingError(f"unusable document id from server: {exc}") from exc
    return DocRef(
        raw_id=raw_id,
        native_id=native,
        url=raw_id if "://" in raw_id else None,
        title=title,
    )


def refs_from_select(result: CallToolResult, ids: IdsMapping | None) -> list[DocRef]:
    blocks = _resource_blocks(result)
    if blocks:  # tier 1 -- the protocol already carries the identity
        refs = []
        for b in blocks:
            uri = getattr(b, "uri", None) or getattr(
                getattr(b, "resource", None), "uri", None
            )
            if uri is None:
                raise MappingError("resource block carries no uri")
            # ResourceLink carries both; `title` is the human-facing one.
            refs.append(
                _ref_for(
                    str(uri), getattr(b, "title", None) or getattr(b, "name", None)
                )
            )
        return refs

    if result.structured_content is not None and ids is not None:  # tier 2
        rows = result.structured_content.get(ids.list)
        if rows is None:
            raise MappingError(
                f"select response has no {ids.list!r} key; keys are "
                f"{sorted(result.structured_content)}"
            )
        if not isinstance(rows, list):
            raise MappingError(f"select response key {ids.list!r} is not a list")
        refs = []
        for row in rows:
            raw = row.get(ids.id) if isinstance(row, dict) else None
            if raw is None:
                raise MappingError(f"select result row has no {ids.id!r} key: {row!r}")
            refs.append(_ref_for(str(raw), row.get(ids.title) if ids.title else None))
        return refs

    # tier 3 -- fails closed. No "first text block", no regex over an outline.
    raise MappingError(
        "select response carries neither resource blocks nor structuredContent; "
        "a prose-only select tool is not mappable -- configure 'static_ids' instead"
    )


def records_from_read(
    result: CallToolResult,
    ref: DocRef,
    spec: ReadSpec,
    media_type: str,
) -> list[RawRecord]:
    def record(
        payload: bytes, native_id: str, url: str | None, mtype: str
    ) -> RawRecord:
        return RawRecord(
            anchor_hint={"native_id": native_id, "url": url, "title": ref.title},
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
                payload = base64.b64decode(blob)
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
            return [record(payload, ref.native_id, ref.url, mime or media_type)]
        if carried:
            return [
                record(
                    payload,
                    _ref_for(uri, None).native_id,
                    uri if "://" in uri else ref.url,
                    mime or media_type,
                )
                for uri, payload, mime in carried
            ]

    if spec.text_key and result.structured_content is not None:  # tier 2
        body = result.structured_content.get(spec.text_key)
        if body is None:
            raise MappingError(
                f"read response has no {spec.text_key!r} key for {ref.native_id}"
            )
        return [record(str(body).encode("utf-8"), ref.native_id, ref.url, media_type)]

    texts = _text_blocks(result)  # tier 3 -- complete, because identity is an input
    if texts:
        return [
            record(
                "\n\n".join(texts).encode("utf-8"), ref.native_id, ref.url, media_type
            )
        ]

    raise MappingError(f"read response for {ref.native_id} carried no content")
