"""The MCP session, and the two guards that stand between a sync run and a write.

Read-only is enforced structurally: the callable set IS the two configured tool
names, so there is no allowlist to misconfigure and no discovery loop that could
widen it. `read_only_hint` is defence in depth on top -- the SDK is explicit that
annotations are hints and "should never" drive tool decisions for untrusted
servers, so it is a guard against honest misconfiguration, never a boundary.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp import Client
from mcp.client import Transport
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.server import MCPServer, Server
from mcp.types import CallToolResult, TextContent

from kbforge_mcp.config import HttpTransport, McpSourceConfig

# Set by tests to point `open_session` at an in-process fixture server. It lives
# here, not in connector.py: connector imports client, so the reverse import
# would cycle. Never set in production.
_server_override: Server[Any] | MCPServer | Transport | str | None = None


class ToolNotAllowed(RuntimeError):
    """A tool outside the configured pair, or one declaring itself mutating."""


class ToolCallFailed(RuntimeError):
    """The server reported the call as an error (`is_error`)."""


class McpClient:
    def __init__(
        self,
        *,
        server: Server[Any] | MCPServer | Transport | str,
        allowed: frozenset[str] = frozenset(),
    ) -> None:
        # `server` is anything mcp.Client accepts: an in-process server object or
        # a Transport. There is no `url` parameter, because a URL with auth has to
        # become a Transport first (see `_http_transport`).
        self._target = server
        self._allowed = allowed
        self._client: Client | None = None
        self._read_only: dict[str, bool | None] = {}

    async def __aenter__(self) -> McpClient:
        self._client = await Client(self._target).__aenter__()
        try:
            listed = await self._client.list_tools()
        except BaseException:
            # `async with` only calls our `__aexit__` once `__aenter__` RETURNS.
            # A failure here happens after the inner `Client` has already
            # entered, so without this except it would never be exited: an
            # orphaned subprocess on stdio, a leaked httpx client on http.
            # Closing an already-broken session can itself raise (e.g. anyio's
            # task-group teardown surfacing the original error again, wrapped in
            # a BaseExceptionGroup) -- the `finally` guarantees `self._client` is
            # still cleared even then, so no state survives either way.
            try:
                await self._client.__aexit__(*sys.exc_info())
            finally:
                self._client = None
            raise
        self._read_only = {
            t.name: getattr(getattr(t, "annotations", None), "read_only_hint", None)
            for t in listed.tools
        }
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def call(self, name: str, args: dict) -> CallToolResult:
        if name not in self._allowed:
            raise ToolNotAllowed(
                f"tool {name!r} is not one of the configured tools "
                f"{sorted(self._allowed)}; the callable set is the config"
            )
        if self._read_only.get(name) is False:
            raise ToolNotAllowed(
                f"tool {name!r} declares itself mutating (read_only_hint=false); "
                f"refusing to call it from a source connector"
            )
        if self._client is None:
            raise RuntimeError("McpClient used outside its context manager")
        result = await self._client.call_tool(name, args)
        if result.is_error:
            text = " ".join(
                b.text for b in result.content if isinstance(b, TextContent)
            )
            raise ToolCallFailed(f"{name} failed: {text}")
        return result


@asynccontextmanager
async def _http_transport(url: str, headers: dict[str, str]):
    """`Client` has no `headers` parameter, so a bearer token rides on the httpx
    client the streamable-HTTP transport is built from. `Transport` is a Protocol
    -- an async context manager yielding TransportStreams -- so this qualifies.

    This is used for EVERY http source, authenticated or not. `StreamableHTTPTransport`
    looks like the natural no-auth shortcut and is not one: it does not implement the
    async context manager protocol, so `Client(StreamableHTTPTransport(url))` raises
    `TypeError: ... does not support the asynchronous context manager protocol`.
    Verified against a live public server."""
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            yield streams


@asynccontextmanager
async def open_session(cfg: McpSourceConfig):
    """One session per fetch: select and every read share it."""
    if _server_override is not None:
        client = McpClient(server=_server_override, allowed=cfg.tool_names)
    elif isinstance(cfg.transport, HttpTransport):
        headers = {}
        if cfg.transport.auth_env:
            token = os.environ.get(cfg.transport.auth_env)
            if not token:
                raise RuntimeError(
                    f"environment variable {cfg.transport.auth_env} is not set"
                )
            headers["Authorization"] = f"Bearer {token}"
        # One shape for authenticated and unauthenticated alike -- see the
        # docstring on `_http_transport` for why there is no no-auth shortcut.
        client = McpClient(
            server=_http_transport(cfg.transport.url, headers), allowed=cfg.tool_names
        )
    else:
        params = StdioServerParameters(
            command=cfg.transport.command,
            args=cfg.transport.args,
            env={k: os.environ[k] for k in cfg.transport.env if k in os.environ},
        )
        # stdio_client is an @asynccontextmanager yielding (read, write) streams,
        # which satisfies the Transport protocol.
        client = McpClient(server=stdio_client(params), allowed=cfg.tool_names)
    # `_server_override`, the http branch, and the stdio branch all end up here --
    # the fixture goes through the exact same statement as a real transport.
    async with client as c:
        yield c
