from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent

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
