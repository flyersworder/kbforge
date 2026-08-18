from __future__ import annotations

import pytest
from mcp.types import TextContent

from kbforge_mcp import client as _client_mod
from kbforge_mcp.client import McpClient, ToolCallFailed, ToolNotAllowed
from tests.fake_server import mcp as fixture_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _client(allowed: set[str]) -> McpClient:
    return McpClient(server=fixture_server, allowed=frozenset(allowed))


def _text(result) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def test_a_configured_tool_is_callable():
    async with await _client({"search_docs", "read_doc"}) as c:
        result = await c.call("read_doc", {"path": "docs/onboarding.md"})
        assert "Onboarding" in _text(result)


async def test_a_tool_outside_the_two_configured_names_is_never_called():
    # `outline` carries read_only_hint=True, so ONLY the structural guard -- the
    # allowed set, not the hint layer -- can refuse it here. Using a mutating tool
    # for this test would let the hint guard mask a broken structural guard: a
    # mutation that deletes the `name not in self._allowed` check entirely still
    # left the original version of this test green, because `delete_doc` is also
    # caught downstream by its own `read_only_hint=False`.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="is not one of the configured tools"):
            await c.call("outline", {"query": "*"})


async def test_a_tool_declaring_read_only_hint_false_is_refused_even_if_configured():
    async with await _client({"search_docs", "delete_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="declares itself mutating"):
            await c.call("delete_doc", {"path": "docs/onboarding.md"})


async def test_an_unset_read_only_hint_is_permitted():
    # Spec default is false, SDK sentinel for "not declared" is None. Refusing on
    # `not read_only_hint` would conflate them and reject every server that never
    # set the annotation -- which is most of them, including both live targets.
    # `peek_doc` carries no `annotations` at all, so this exercises the real
    # `getattr(annotations, "read_only_hint", None)` default rather than a test
    # poking `McpClient._read_only` by hand.
    async with await _client({"search_docs", "peek_doc"}) as c:
        result = await c.call("peek_doc", {"path": "docs/retention.md"})
        assert "Retention" in _text(result)


async def test_is_error_becomes_an_exception_and_never_document_content():
    # An errored call still returns content -- populated WITH THE ERROR MESSAGE.
    # Mapping it would ship the error text as a concept body.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolCallFailed, match="no such document"):
            await c.call("read_doc", {"path": "docs/missing.md"})


def _messages(exc: BaseException) -> list[str]:
    """Flatten an (Base)ExceptionGroup so `match=`-style assertions still work.

    Closing the real in-process session after `list_tools()` fails surfaces our
    injected error wrapped in anyio's own `BaseExceptionGroup` from tearing down
    its task group -- itself evidence that `__aexit__` really ran real cleanup,
    not a stub.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [m for sub in exc.exceptions for m in _messages(sub)]
    return [str(exc)]


async def test_a_failure_between_connect_and_ready_does_not_leak_the_session(
    monkeypatch,
):
    # Simulates a server that completes the handshake but then dies before
    # list_tools() returns (protocol error, timeout, ...). `async with` only
    # calls our `__aexit__` once `__aenter__` RETURNS, so without the try/except
    # in `__aenter__`, the already-entered inner `Client` would never be exited:
    # an orphaned subprocess on stdio, a leaked httpx client on http.
    exited: list[tuple[object, object, object]] = []
    real_aexit = _client_mod.Client.__aexit__

    async def spy_aexit(self, *exc):
        exited.append(exc)
        return await real_aexit(self, *exc)

    async def broken_list_tools(self, **kwargs):
        raise RuntimeError("server died mid-handshake")

    monkeypatch.setattr(_client_mod.Client, "__aexit__", spy_aexit)
    monkeypatch.setattr(_client_mod.Client, "list_tools", broken_list_tools)

    c = McpClient(server=fixture_server, allowed=frozenset({"read_doc"}))
    with pytest.raises(BaseException) as exc_info:
        await c.__aenter__()
    assert any("server died mid-handshake" in m for m in _messages(exc_info.value))

    # Private-attribute access: this is deliberately reaching past the public
    # API to confirm no session state survives the failure.
    assert c._client is None, "the failed session was not cleared"
    assert exited, "the inner Client was never exited -- the session leaked"
