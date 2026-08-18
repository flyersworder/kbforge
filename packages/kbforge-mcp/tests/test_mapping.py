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
    assert [r.native_id for r in refs] == ["s3/naming", "s3/limits"]
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
    assert [r.native_id for r in refs] == ["a"]


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
    assert [r.native_id for r in refs] == ["s3/naming", "s3/limits"]
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


def test_tier1_one_to_many_read_derives_identities_from_the_uris():
    # A one-to-many read (a "read this folder" tool) is the one case where the
    # ref we asked for cannot supply identity for every document it returns --
    # the uris on the response are the only source of fresh identities.
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
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert [r.anchor_hint["native_id"] for r in records] == ["docs/a", "docs/b"]
    assert [r.payload.decode() for r in records] == ["a content", "b content"]
    # url is law-3 provenance material and must be derived per document too --
    # without this assertion, dropping it entirely still passes.
    assert [r.anchor_hint["url"] for r in records] == [
        "https://docs.aws.amazon.com/docs/a.html",
        "https://docs.aws.amazon.com/docs/b.html",
    ]
    # And title comes from the per-document ref, not the parent: binding it to
    # `ref.title` would stamp the folder's "Docs" onto every document it
    # returned. `EmbeddedResource` carries no title, so it is None here and
    # `kbforge_normalize` derives one per document from its native_id.
    assert [r.anchor_hint["title"] for r in records] == [None, None]


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


def test_a_contentless_link_falls_through_to_the_text_blocks():
    # A ResourceLink with neither `text` nor `blob` carries no bytes, so tier 1
    # skips it rather than emitting an empty document -- but that means the
    # "resource blocks present" check at the top of tier 1 does not, by
    # itself, guarantee a tier-1 return: this deliberately falls through to
    # tier 3 and the TextContent block becomes the body. That is intentional
    # so a server that returns a link alongside the content as plain text
    # still works, but it does mean a prose preamble sitting next to a bare
    # link would be captured as the document body. Task 7's live test against
    # a real GitHub server is what settles which shape GitHub actually
    # returns -- do not "fix" this fallthrough without that evidence, since a
    # rule that refuses contentless-link responses would break the
    # link-plus-text shape if that turns out to be the real one.
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
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert len(records) == 1
    assert records[0].payload.decode() == "the actual content"
    assert records[0].anchor_hint["native_id"] == "docs/a"


def test_tier1_resource_blocks_win_over_a_configured_tier2_ids_mapping():
    # Protocol-first: this is the one place explicit config loses to protocol
    # inference. A response carrying BOTH resource blocks and structuredContent
    # with a fully configured `ids` mapping still resolves through tier 1 --
    # the protocol already carries identity, so there is nothing for the
    # configured mapping to add, and tier 1 is tried first unconditionally.
    result = CallToolResult(
        content=[
            ResourceLink(
                type="resource_link",
                uri="https://docs.aws.amazon.com/s3/naming.html",
                name="s3-naming",
                title="Naming",
            ),
        ],
        structured_content={
            "results": [{"url": "https://docs.aws.amazon.com/s3/ignored.html"}]
        },
    )
    refs = refs_from_select(result, IdsMapping(list="results", id="url"))
    assert [r.native_id for r in refs] == ["s3/naming"]
