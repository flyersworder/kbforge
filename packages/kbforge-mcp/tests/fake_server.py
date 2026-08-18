"""A real MCP server covering every response shape the mapping must handle.

Driven in-process by a real Client, so these tests exercise real protocol
serialization with no network and no fakes.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("kbforge-mcp-fixture")

DOCS = {
    "docs/onboarding.md": "# Onboarding\n\nHow to get started.",
    "docs/retention.md": "# Retention\n\nHow long we keep things.",
}


# The return annotation must be PRECISE or the SDK emits no structuredContent at
# all. A bare `-> dict` yields `structured_content=None` silently (the tier-2
# selector test would then fail closed into tier 3), and `structured_output=True`
# on a bare `dict` raises InvalidSignature at registration. A parameterized dict
# or a Pydantic model works; both were verified against the installed SDK.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def search_docs(query: str) -> dict[str, list[dict[str, str]]]:
    """Tier-2 selector: ids in structuredContent."""
    return {"results": [{"path": p, "title": p} for p in sorted(DOCS)]}


# NOTE: a `-> str` tool ALSO gets structuredContent, wrapped as {"result": ...},
# alongside the text block. That is why `records_from_read`'s tier-2 branch is
# gated on `spec.text_key` being configured rather than on structured_content
# merely being present -- do not "simplify" that gate away, or every tier-3
# reader would silently take the tier-2 path.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def read_doc(path: str) -> str:
    """Tier-3 reader: bare markdown. Identity came in as `path`.

    Falls back to a `.md`-suffixed lookup so a caller may pass an id either
    with or without the extension -- realistic reader behaviour, and what lets
    `docs/retention.md` and `docs/retention` both resolve to real content for
    the slug-collision fetch-side-law test (both must succeed, or there is no
    duplicate `doc_id` to catch). `DOCS` itself keeps only the `.md` keys, so
    `search_docs`'s `sorted(DOCS)` result is unaffected.
    """
    if path not in DOCS and f"{path}.md" in DOCS:
        path = f"{path}.md"
    if path not in DOCS:
        raise ValueError(f"no such document: {path}")
    return DOCS[path]


# `-> str` gives this one structuredContent too ({"result": "..."}), but the
# tier-2 selector branch also requires `ids` to be configured, and the prose-only
# source configures none -- so it still lands in tier 3 and fails closed.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def outline(query: str) -> str:
    """Tier-3 selector: prose only. Must fail closed."""
    return "- 1 Onboarding\n- 2 Retention"


# No `annotations=` at all -- what most real servers, including both live test
# targets, actually send: `read_only_hint` is simply absent, not `False`. This is
# the case the "unset hint is permitted" client test needs to exercise for real,
# rather than by poking `McpClient._read_only` by hand.
@mcp.tool()
def peek_doc(path: str) -> str:
    """Tier-3 reader with no annotations declared at all."""
    if path not in DOCS:
        raise ValueError(f"no such document: {path}")
    return DOCS[path]


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False))
def delete_doc(path: str) -> str:
    """Declares itself mutating. Must never be called."""
    DOCS.pop(path, None)
    return "deleted"
