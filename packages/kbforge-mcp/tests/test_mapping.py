from __future__ import annotations

import base64

import pytest
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

from kbforge_mcp.config import IdsMapping, ReadSpec
from kbforge_mcp.mapping import (
    DocRef,
    MappingError,
    records_from_read,
    refs_from_select,
)


def _text(body: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=body)])


def test_tier2_select_extracts_refs_from_structured_content():
    result = CallToolResult(
        content=[TextContent(type="text", text="ignored prose")],
        structured_content={
            "results": [
                {
                    "url": "https://docs.aws.amazon.com/s3/naming.html",
                    "title": "Naming",
                },
                {
                    "url": "https://docs.aws.amazon.com/s3/limits.html",
                    "title": "Limits",
                },
            ]
        },
    )
    refs = refs_from_select(result, IdsMapping(list="results", id="url", title="title"))
    assert [r.native_id for r in refs] == [
        "@docs.aws.amazon.com/s3/naming",
        "@docs.aws.amazon.com/s3/limits",
    ]
    # The reader must receive the ORIGINAL id, never the slug.
    assert refs[0].raw_id == "https://docs.aws.amazon.com/s3/naming.html"
    assert refs[0].url == "https://docs.aws.amazon.com/s3/naming.html"
    assert refs[0].title == "Naming"


def test_a_non_url_id_gets_no_url_only_an_identity():
    result = CallToolResult(
        content=[], structured_content={"items": [{"path": "docs/onboarding.md"}]}
    )
    refs = refs_from_select(result, IdsMapping(list="items", id="path"))
    assert refs[0].native_id == "docs/onboarding"
    assert refs[0].raw_id == "docs/onboarding.md"
    assert refs[0].url is None


def test_tier3_select_fails_closed_with_a_message_naming_the_remedy():
    # No prose heuristics. A bare-text select response is unsupported, and the
    # error must point at static_ids rather than guess.
    with pytest.raises(MappingError, match="static_ids"):
        refs_from_select(_text("- 1 Overview\n- 2 Architecture"), None)


def test_structured_content_without_ids_names_ids_not_static_ids_as_the_remedy():
    # This is the most likely real-world misconfiguration: a search tool that
    # does return structuredContent, but whose `ids` mapping was never
    # configured. The tier-3 message above is wrong advice here -- the
    # response does carry structuredContent, and the fix is `ids`, not
    # `static_ids` -- so this must be a distinct branch with distinct advice.
    result = CallToolResult(
        content=[], structured_content={"results": [{"url": "https://x/a"}]}
    )
    # `\bids\b` (not just "ids") because "static_ids" also contains "ids" as a
    # substring -- a bare "ids" match would pass against either message and
    # not actually pin which remedy got named.
    with pytest.raises(MappingError, match=r"\bids\b"):
        refs_from_select(result, None)


def test_scalar_wrapped_structured_content_with_no_ids_names_static_ids():
    # A `-> str` (or other non-object-returning) select tool still gets
    # structuredContent from the MCP SDK, auto-wrapped as the single key
    # `{"result": <the return value>}`. With no `ids` configured that is a
    # prose tool wearing a structuredContent-shaped costume, not real tier-2
    # data -- it must fail closed with the tier-3 message (`static_ids`), not
    # the "add ids" message the previous test pins for genuine structuredContent.
    result = CallToolResult(
        content=[], structured_content={"result": "- 1 Overview\n- 2 Architecture"}
    )
    with pytest.raises(MappingError, match="static_ids"):
        refs_from_select(result, None)


def test_ids_configured_with_list_named_result_still_maps():
    # `"result"` isn't a reserved key -- a real search tool may legitimately
    # use it as its row-list key. An explicitly configured `ids` mapping is
    # the operator telling us where the rows are, and must be honoured
    # regardless of the key set's shape, even though `{"result": ...}` is
    # also what the SDK's scalar auto-wrap looks like. What distinguishes
    # them here is the value's type: this "result" holds a list, the
    # auto-wrap never does (see `test_scalar_wrapped_...` above).
    result = CallToolResult(
        content=[],
        structured_content={"result": [{"url": "https://docs.example.com/a"}]},
    )
    refs = refs_from_select(result, IdsMapping(list="result", id="url"))
    assert [r.native_id for r in refs] == ["@docs.example.com/a"]


def test_a_configured_ids_mapping_outranks_the_scalar_wrap_heuristic():
    # Pins the PRECEDENCE -- that `ids is not None` is consulted before
    # `_is_scalar_wrapped`, not merely that the heuristic is tight enough --
    # which the test above does not. Its `{"result": [rows]}` fixture is
    # exempted by the heuristic's own value-type check (the SDK's auto-wrap
    # never holds a list), so it maps under EITHER ordering and cannot tell
    # them apart. This fixture can: the key set and the value type are both
    # exactly the auto-wrap's shape, so the heuristic says True, and only the
    # `ids` check running first keeps tier 2 in play.
    #
    # The two orderings then diverge in the MESSAGE, which is what to assert
    # on. Precedence to `ids`: tier 2 applies and complains the configured
    # key is not a list. Heuristic first: tier 2 is skipped entirely and the
    # tier-3 `static_ids` message comes out instead -- steering an operator
    # who has a perfectly good `ids` mapping towards hand-enumerating ids.
    result = CallToolResult(content=[], structured_content={"result": "not a row list"})
    with pytest.raises(MappingError, match="'result' is not a list"):
        refs_from_select(result, IdsMapping(list="result", id="url"))


def test_an_empty_select_result_is_legal_not_an_error():
    # Deliberate: a zero-hit query is a real state, not a failure. Raising
    # here would turn an ordinary no-op run into an aborted one. This is safe
    # because a query selector always yields `complete=False`, under which no
    # tombstone (corpus-wide deletion) is permitted -- see the comment at the
    # loop in refs_from_select.
    result = CallToolResult(content=[], structured_content={"results": []})
    assert refs_from_select(result, IdsMapping(list="results", id="url")) == []


def test_a_missing_list_key_is_an_error_not_an_empty_result():
    result = CallToolResult(content=[], structured_content={"data": []})
    with pytest.raises(MappingError, match="results"):
        refs_from_select(result, IdsMapping(list="results", id="url"))


def test_tier3_read_is_complete_because_identity_came_from_the_ref():
    # The reader is called with an id we already have, so its response only has
    # to supply bytes. Concatenating text blocks is deterministic, not a guess.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(
        content=[
            TextContent(type="text", text="first half"),
            TextContent(type="text", text="second half"),
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert len(records) == 1
    assert records[0].payload.decode() == "first half\n\nsecond half"
    assert records[0].anchor_hint["native_id"] == "docs/a"
    assert records[0].media_type == "text/markdown"


def test_tier2_read_takes_the_configured_text_key():
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(content=[], structured_content={"body": "the content"})
    spec = ReadSpec(tool="read", id_arg="path", text_key="body")
    records = records_from_read(result, ref, spec, "text/markdown")
    assert records[0].payload.decode() == "the content"


def test_a_tier2_read_with_an_empty_string_text_key_value_is_an_error():
    # `""` is "no body", same as `None` -- a zero-byte document is not a
    # legitimate read result. This must stay narrow: an int `0` or bool
    # `False` body is real content (it stringifies to a non-empty payload)
    # and must NOT be rejected by this same check.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(content=[], structured_content={"body": ""})
    spec = ReadSpec(tool="read", id_arg="path", text_key="body")
    with pytest.raises(MappingError, match="'body'"):
        records_from_read(result, ref, spec, "text/markdown")


@pytest.mark.parametrize(
    ("body", "payload"), [(0, b"0"), (False, b"False"), (0.0, b"0.0")]
)
def test_a_tier2_read_with_a_non_str_falsy_body_is_real_content(body, payload):
    # The other half of the narrowness the test above is named for, and the
    # half that actually constrains the implementation: with `if not body:`
    # these are rejected as "no body" identically to `""`, so without this
    # case the guard's `isinstance(body, str)` can be deleted and the whole
    # suite still passes. An int `0` stringifies to a one-byte document, and a
    # one-byte document is content.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(content=[], structured_content={"body": body})
    spec = ReadSpec(tool="read", id_arg="path", text_key="body")
    records = records_from_read(result, ref, spec, "text/markdown")
    assert [r.payload for r in records] == [payload]


def test_an_empty_read_response_is_an_error_not_an_empty_document():
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    with pytest.raises(MappingError, match="no content"):
        records_from_read(
            CallToolResult(content=[]),
            ref,
            ReadSpec(tool="r", id_arg="p"),
            "text/markdown",
        )


@pytest.mark.parametrize("text", ["", "   \n  "])
def test_a_present_but_empty_block_is_an_error_in_every_tier(text):
    # "An empty read is an error, not an empty document" is the rule tier 2's
    # comment cites as established -- and only tier 2 enforced it. An ABSENT
    # block (the test above) was covered; a PRESENT but empty one was not, and
    # that is the hole a real server actually produces:
    #
    #   tier 3, [TextContent(text="")]  ->  `_text_blocks` returns [""], a
    #       TRUTHY list holding an empty string, so `if texts:` passed and a
    #       RawRecord with payload=b"" was emitted. It synthesized and published
    #       an empty concept with nothing raising anywhere.
    #   tier 1, a resource whose `.text` is ""  ->  `carried`, same result.
    #
    # Whitespace-only is the same nothing: `kbforge_normalize` strips the text,
    # so "   " publishes exactly the empty concept this rule refuses.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    spec = ReadSpec(tool="r", id_arg="p")

    with pytest.raises(MappingError, match="no content"):  # tier 3
        records_from_read(
            CallToolResult(content=[TextContent(type="text", text=text)]),
            ref,
            spec,
            "text/markdown",
        )

    with pytest.raises(MappingError, match="empty resource"):  # tier 1
        records_from_read(
            CallToolResult(
                content=[
                    EmbeddedResource(
                        type="resource",
                        resource=TextResourceContents(
                            uri="https://example.com/docs/a.md", text=text
                        ),
                    )
                ]
            ),
            ref,
            spec,
            "text/markdown",
        )

    with pytest.raises(MappingError, match="'body'"):  # tier 2
        records_from_read(
            CallToolResult(content=[], structured_content={"body": text}),
            ref,
            ReadSpec(tool="r", id_arg="p", text_key="body"),
            "text/markdown",
        )


def test_a_tier2_body_that_is_not_scalar_is_refused_not_repr_published():
    # `str(body)` on a dict publishes "{'markdown': '...'}" as the document,
    # and nothing downstream can tell that from a real body. Pointing
    # `text_key` at a nested object is an ordinary misconfiguration.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(
        content=[], structured_content={"content": {"markdown": "# Title"}}
    )
    spec = ReadSpec(tool="read", id_arg="path", text_key="content")
    with pytest.raises(MappingError, match="not a document body") as exc:
        records_from_read(result, ref, spec, "text/markdown")
    assert "dict" in str(exc.value)


def test_a_malformed_blob_degrades_the_document_not_the_run():
    # `b64decode` raises binascii.Error, which is a ValueError and NOT a
    # MappingError, and `_fetch` catches only (ToolCallFailed, MappingError) --
    # so one corrupt blob propagated out of `kbforge_fetch` and killed the run.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/a.md",
                    blob="!!!not base64!!!",
                    mime_type="text/markdown",
                ),
            )
        ]
    )
    with pytest.raises(MappingError, match="not valid base64"):
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )


def test_blob_corruption_that_would_be_silently_dropped_is_refused():
    # The half of `validate=True` that actually constrains the implementation.
    # `b64decode`'s default DISCARDS characters outside the base64 alphabet, so
    # a corrupted blob decodes to plausible-looking content and ships as a
    # document -- no exception, no signal. Here the injected `*` is dropped by
    # the default and the blob decodes cleanly to the original bytes, so only
    # `validate=True` can tell this from a good read. (The malformed-padding
    # case above raises either way, which is why it cannot pin this.)
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    good = base64.b64encode(b"# Title\n\nbody").decode()
    corrupt = f"{good[:4]}*{good[4:]}"
    assert base64.b64decode(corrupt) == b"# Title\n\nbody"  # the silent drop
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/a.md",
                    blob=corrupt,
                    mime_type="text/markdown",
                ),
            )
        ]
    )
    with pytest.raises(MappingError, match="not valid base64"):
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )


def test_a_line_wrapped_blob_is_decoded_not_rejected():
    # `validate=True` is what makes corruption visible instead of silently
    # dropped -- but line-wrapping base64 is an encoding convention, not
    # corruption, so whitespace is stripped before validating.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    wrapped = base64.b64encode(b"# Title\n\nbody" * 20).decode()
    wrapped = "\n".join(wrapped[i : i + 40] for i in range(0, len(wrapped), 40))
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/a.md",
                    blob=wrapped,
                    mime_type="text/markdown",
                ),
            )
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert records[0].payload == b"# Title\n\nbody" * 20


def test_tier1_select_extracts_refs_from_resource_links():
    # No `ids` config at all: the protocol already carries identity, so tier 1
    # needs none of tier 2's structured_content configuration.
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/naming.html",
                name="s3-naming",
                title="Naming",
            ),
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/limits.html",
                name="s3-limits",
                title="Limits",
            ),
        ]
    )
    refs = refs_from_select(result, None)
    assert [r.native_id for r in refs] == [
        "@docs.aws.amazon.com/s3/naming",
        "@docs.aws.amazon.com/s3/limits",
    ]
    # `title` is the human-facing field; it must be preferred over `name`.
    assert [r.title for r in refs] == ["Naming", "Limits"]
    assert refs[0].url == "https://docs.aws.amazon.com/s3/naming.html"


def test_tier1_one_to_one_read_keeps_the_requested_identity_not_the_uri():
    # Regression guard: GitHub's reader returns a `repo://` uri that embeds the
    # commit sha of the read (`repo://owner/repo/sha/<sha>/contents/<path>`).
    # Slugging that uri would put a fresh sha inside `native_id` on every call,
    # so identity would churn on every commit and nothing would ever diff as
    # `modified`. On a one-to-one read the ref we already asked for -- built
    # from the id we selected, not from what the server echoes back -- must
    # win instead.
    ref = DocRef(raw_id="SECURITY.md", native_id="SECURITY", url=None, title="Security")
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=(
                        "repo://owner/repo/sha/"
                        "76d64c822f5125032f89eb71dbdb94e42b434821/contents/"
                        "SECURITY.md"
                    ),
                    text="report vulnerabilities to security@example.com",
                    mime_type="text/plain",
                ),
            )
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert len(records) == 1
    assert records[0].anchor_hint["native_id"] == "SECURITY"
    assert "76d64c822f5125032f89eb71dbdb94e42b434821" not in str(records[0].anchor_hint)


def test_a_read_returning_two_documents_is_refused_not_silently_reidentified():
    # THE RULING HERE REVERSED. This used to be
    # `test_tier1_one_to_many_read_derives_identities_from_the_uris`, which
    # pinned a "read this folder" shape: with two content-bearing resources,
    # every identity was derived from the response's uris.
    #
    # The guard selecting that behaviour was `len(carried) == 1`, i.e. a COUNT,
    # and the single-resource branch exists for a specific reason -- a server
    # uri may encode volatile state, and GitHub's really does
    # (`repo://owner/repo/sha/<commit-sha>/contents/<path>`). So one extra
    # content-bearing resource silently flipped the REQUESTED document onto
    # uri-derived identity as well: its doc_id then changed on every upstream
    # commit, it always diffed as `added` and never `modified`, and stale
    # concepts accumulated with no tombstone to remove them. That is exactly
    # what the single-resource branch was written to prevent.
    #
    # A response cannot reliably distinguish "your document, plus extras" from
    # "the contents of the container you asked for", and only the second
    # licenses uri-derived identity. This connector fails closed everywhere
    # else it cannot tell (a prose-only selector, a non-text blob, a bodiless
    # read), so it fails closed here. The one-to-many branch had never run
    # against a real server: GitHub's file read carries one resource, AWS
    # carries none.
    ref = DocRef(raw_id="docs/", native_id="docs", url=None, title="Docs")
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="https://docs.aws.amazon.com/docs/a.html",
                    text="a content",
                    mime_type="text/plain",
                ),
            ),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="https://docs.aws.amazon.com/docs/b.html",
                    text="b content",
                    mime_type="text/plain",
                ),
            ),
        ]
    )
    with pytest.raises(MappingError) as exc:
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )
    # Names the document and the ambiguity, not just a count.
    assert "docs" in str(exc.value)
    assert "2 content-bearing resources" in str(exc.value)


def test_the_requested_identity_survives_a_sibling_text_block():
    # The shape GitHub's file read actually returns -- one content-bearing
    # resource plus a prose preamble -- must still take the identity we asked
    # for. The refusal above is about two CONTENT-BEARING resources, not about
    # any second block.
    ref = DocRef(raw_id="SECURITY.md", native_id="SECURITY", url=None, title="Sec")
    result = CallToolResult(
        content=[
            TextContent(type="text", text="successfully downloaded (SHA: 76d64c82)"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri="repo://o/r/sha/76d64c82/contents/SECURITY.md",
                    text="# Security Policy",
                    mime_type="text/markdown",
                ),
            ),
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert [r.anchor_hint["native_id"] for r in records] == ["SECURITY"]
    assert records[0].payload.decode() == "# Security Policy"


def test_a_base64_blob_resource_decodes_and_its_mime_type_wins():
    # A blob is base64 regardless of what it holds; a server may deliver
    # perfectly ordinary text this way (GitHub does). What is pinned here is
    # the base64 round-trip and the mime_type precedence -- NOT that arbitrary
    # bytes survive, which the next test refuses on purpose.
    ref = DocRef(raw_id="docs/a.txt", native_id="docs/a", url=None, title="A")
    raw_bytes = "# Retention\n\nKept 90 days. \u00a7 caf\u00e9".encode()
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/a.txt",
                    blob=base64.b64encode(raw_bytes).decode(),
                    mime_type="text/plain",
                ),
            )
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert records[0].payload == raw_bytes
    # The resource's own mime_type beats the connector-configured default.
    assert records[0].media_type == "text/plain"


def test_a_binary_blob_is_a_mapping_error_not_a_crash_in_normalize():
    # `kbforge_normalize` decodes every payload as utf-8 and, being pure, has
    # nowhere to put a failure -- so a PNG reaching it aborts the entire run
    # with a UnicodeDecodeError. Refusing it here makes it a MappingError,
    # which `_fetch` catches per document: skip the document, degrade
    # `complete`, keep the run. Reachable from a documented live target:
    # GitHub's `get_file_contents` returns a blob for any binary file.
    ref = DocRef(raw_id="docs/logo.png", native_id="docs/logo", url=None, title="Logo")
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe"
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/logo.png",
                    blob=base64.b64encode(png).decode(),
                    mime_type="image/png",
                ),
            )
        ]
    )
    with pytest.raises(MappingError, match="docs/logo.*not valid UTF-8"):
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )


def test_the_binary_refusal_names_the_document_and_the_mime_type():
    # The message is the operator's only clue about which document vanished
    # from an otherwise successful run, so pin its content, not just its type.
    ref = DocRef(raw_id="docs/logo.png", native_id="docs/logo", url=None, title="Logo")
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/logo.png",
                    blob=base64.b64encode(b"\xff\xfe\x00binary").decode(),
                    mime_type="image/png",
                ),
            )
        ]
    )
    with pytest.raises(MappingError) as exc:
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )
    message = str(exc.value)
    assert "docs/logo" in message
    assert "https://example.com/docs/logo.png" in message
    assert "image/png" in message


def test_a_contentless_link_is_refused_rather_than_falling_through():
    # THE RULING HERE REVERSED, and the reason is evidence rather than taste.
    # A ResourceLink with neither `text` nor `blob` carries no bytes, so tier 1
    # skips it -- which means the "resource blocks present" check at the top of
    # tier 1 does not, by itself, guarantee a tier-1 return. This USED to fall
    # through to tier 3, making a sibling TextContent the document body. That
    # was pinned rather than refused because refusing might break a legitimate
    # server that returns a link alongside its content as plain text, and there
    # was no evidence either way.
    #
    # The evidence arrived with the live GitHub test, and it runs the other way.
    # GitHub's `get_file_contents` on a FILE returns a resource block that
    # carries the content, so tier 1 fires on it correctly and this refusal
    # cannot reach that shape. On a DIRECTORY it returns exactly the harmful
    # shape: one contentless ResourceLink per entry plus a prose preamble --
    # under the fallthrough, the preamble shipped as the document body.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://example.com/docs/a.md",
                name="a",
            ),
            TextContent(type="text", text="the actual content"),
        ]
    )
    with pytest.raises(MappingError) as exc:
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )
    # Names the document, so an operator knows which id to drop.
    assert "docs/a" in str(exc.value)
    assert "none of which had content" in str(exc.value)


def test_a_directory_listing_is_refused_not_read_as_its_preamble():
    # The shape GitHub's `get_file_contents` returns for a directory.
    ref = DocRef(raw_id="docs/", native_id="docs", url=None, title="Docs")
    result = CallToolResult(
        content=[
            TextContent(type="text", text="successfully listed directory docs/"),
            ResourceLink(type="resource_link", uri="repo://o/r/docs/a.md", name="a"),
            ResourceLink(type="resource_link", uri="repo://o/r/docs/b.md", name="b"),
        ]
    )
    with pytest.raises(MappingError, match="docs"):
        records_from_read(
            result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
        )


def test_a_configured_ids_mapping_wins_over_tier1_resource_blocks():
    # THE RULING HERE REVERSED. This used to pin protocol-first as absolute:
    # a response carrying BOTH resource blocks and structuredContent resolved
    # through tier 1 and the operator's `ids` mapping was discarded -- with no
    # error and no warning, so a mapping that looked configured did nothing.
    #
    # That is the inverse of the rule a few lines below in the same function,
    # where a configured `ids` is believed OVER the auto-wrap shape heuristic
    # precisely because the operator said where the rows live. Both readings
    # cannot be right. Configured intent wins wherever it can apply; inference,
    # protocol-level or not, is for the unconfigured case -- which is every
    # case the protocol-first mapping was designed for.
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/from-the-link.html",
                name="s3-naming",
                title="Naming",
            ),
        ],
        structured_content={
            "results": [{"url": "https://docs.aws.amazon.com/s3/from-the-mapping.html"}]
        },
    )
    refs = refs_from_select(result, IdsMapping(list="results", id="url"))
    assert [r.native_id for r in refs] == ["@docs.aws.amazon.com/s3/from-the-mapping"]


def test_tier1_still_wins_when_no_ids_mapping_is_configured():
    # Protocol-first is unchanged for the unconfigured case, which is the case
    # it was designed for: with no `ids` there is no operator intent to defer to.
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/naming.html",
                name="s3-naming",
                title="Naming",
            ),
        ],
        structured_content={"results": [{"url": "https://x.com/ignored.html"}]},
    )
    refs = refs_from_select(result, None)
    assert [r.native_id for r in refs] == ["@docs.aws.amazon.com/s3/naming"]


def test_a_configured_ids_mapping_falls_back_to_tier1_without_structured_content():
    # The precedence flip applies only where the mapping CAN apply. A server
    # that returns resource links and no structuredContent at all still maps,
    # rather than failing on a missing key.
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/naming.html",
                name="s3-naming",
            ),
        ],
    )
    refs = refs_from_select(result, IdsMapping(list="results", id="url"))
    assert [r.native_id for r in refs] == ["@docs.aws.amazon.com/s3/naming"]
