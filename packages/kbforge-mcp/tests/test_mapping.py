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


def test_a_base64_blob_resource_decodes_and_its_mime_type_wins():
    ref = DocRef(raw_id="docs/a.bin", native_id="docs/a", url=None, title="A")
    raw_bytes = b"\x00\x01binary payload\xff"
    result = CallToolResult(
        content=[
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="https://example.com/docs/a.bin",
                    blob=base64.b64encode(raw_bytes).decode(),
                    mime_type="application/octet-stream",
                ),
            )
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert records[0].payload == raw_bytes
    # The resource's own mime_type beats the connector-configured default.
    assert records[0].media_type == "application/octet-stream"


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
