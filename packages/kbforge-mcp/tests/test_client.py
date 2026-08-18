from __future__ import annotations

import pytest

from kbforge_mcp.client import McpClient, ToolCallFailed, ToolNotAllowed
from tests.fake_server import mcp as fixture_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _client(allowed: set[str]) -> McpClient:
    return McpClient(server=fixture_server, allowed=frozenset(allowed))


async def test_a_configured_tool_is_callable():
    async with await _client({"search_docs", "read_doc"}) as c:
        result = await c.call("read_doc", {"path": "docs/onboarding.md"})
        assert "Onboarding" in result.content[0].text


async def test_a_tool_outside_the_two_configured_names_is_never_called():
    # The callable set is structural: there is no config key that could widen it.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="delete_doc"):
            await c.call("delete_doc", {"path": "docs/onboarding.md"})
    assert (
        "docs/onboarding.md" in __import__("tests.fake_server", fromlist=["DOCS"]).DOCS
    )


async def test_a_tool_declaring_read_only_hint_false_is_refused_even_if_configured():
    async with await _client({"search_docs", "delete_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="declares itself mutating"):
            await c.call("delete_doc", {"path": "docs/onboarding.md"})


async def test_an_unset_read_only_hint_is_permitted():
    # Spec default is false, SDK sentinel for "not declared" is None. Refusing on
    # `not read_only_hint` would conflate them and reject every server that never
    # set the annotation -- which is most of them, including both live targets.
    async with await _client({"search_docs", "read_doc"}) as c:
        c._read_only[  # noqa: SLF001 - deliberately simulating an unannotated server
            "read_doc"
        ] = None
        assert await c.call("read_doc", {"path": "docs/retention.md"})


async def test_is_error_becomes_an_exception_and_never_document_content():
    # An errored call still returns content -- populated WITH THE ERROR MESSAGE.
    # Mapping it would ship the error text as a concept body.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolCallFailed, match="no such document"):
            await c.call("read_doc", {"path": "docs/missing.md"})
