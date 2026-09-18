"""A reference web-source MCP server: Firecrawl behind the two-tool contract
kbforge-mcp consumes as-is.

- `search(query)` is the **selector**. It returns candidate URLs as
  `structuredContent` (`{"results": [{"url", "title"}]}`), filtered to the
  domain allowlist. It may be non-deterministic; kbforge only uses it to decide
  what to read.
- `read(url)` is the **reader**. It returns the page's own content as markdown,
  never a summary, so every concept stays traceable to the page it came from,
  plus the page's own title, so a page's title doesn't depend on which selector
  found it (`{"markdown": ..., "title": ...}` as `structuredContent`).

Swap `_search_backend` for another search API — Azure's Responses-API web
search, say, returning the `url_citation` URLs it cites — and nothing else
changes. Keep `read` fetching the page itself: an agent's answer text must never
become the document.

Environment:
  FIRECRAWL_API_KEY           required
  WEB_SOURCE_ALLOWED_DOMAINS  required for `search`; comma-separated, or '*'
  WEB_SOURCE_MAX_AGE_MS       optional; accept a cached scrape up to this age
                              (Firecrawl's default when unset; 0 = always live)
  FIRECRAWL_API_URL           optional; default https://api.firecrawl.dev/v2
"""

from __future__ import annotations

import os

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from kbforge_web_source.clean import allowed, clean_markdown, domains_from

server = MCPServer("kbforge-web-source")

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)


def _firecrawl(path: str, body: dict) -> dict:
    api = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev/v2")
    response = httpx.post(
        f"{api.rstrip('/')}/{path}",
        json=body,
        headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"firecrawl {path} failed: {payload.get('error')}")
    return payload["data"]


def _search_backend(query: str, limit: int, tbs: str) -> list[dict[str, str]]:
    """Candidate hits as `{"url", "title"}` dicts. The one function to replace
    for another search provider."""
    body: dict = {"query": query, "limit": limit}
    if tbs:
        body["tbs"] = tbs  # e.g. "qdr:m" = past month
    hits = _firecrawl("search", body).get("web") or []
    return [{"url": h["url"], "title": h.get("title") or h["url"]} for h in hits]


# The return annotation must be a parameterized dict for the SDK to emit
# `structuredContent`; a bare `dict` yields none, and kbforge-mcp then refuses
# the selector as prose.
@server.tool(annotations=_READ_ONLY)
def search(
    query: str, limit: int = 10, tbs: str = ""
) -> dict[str, list[dict[str, str]]]:
    """Web search, restricted to WEB_SOURCE_ALLOWED_DOMAINS. Returns candidate
    pages to read; never page content."""
    domains = domains_from(os.environ.get("WEB_SOURCE_ALLOWED_DOMAINS", ""))
    seen: set[str] = set()
    results = []
    for hit in _search_backend(query, limit, tbs):
        if allowed(hit["url"], domains) and hit["url"] not in seen:
            seen.add(hit["url"])
            results.append(hit)
    return {"results": results}


@server.tool(annotations=_READ_ONLY)
def read(url: str) -> dict[str, str]:
    """The page's main content as markdown, and its own title. Raises rather
    than returning an error page, so kbforge records a failed read instead of a
    junk concept."""
    body: dict = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    if max_age := os.environ.get("WEB_SOURCE_MAX_AGE_MS"):
        body["maxAge"] = int(max_age)
    data = _firecrawl("scrape", body)
    status = (data.get("metadata") or {}).get("statusCode", 200)
    if status >= 400:
        raise RuntimeError(f"{url} returned HTTP {status}")
    markdown = clean_markdown(data.get("markdown") or "")
    if not markdown.strip():
        raise RuntimeError(f"{url} yielded no content")
    title = str((data.get("metadata") or {}).get("title") or "").strip()
    return {"markdown": markdown, "title": title}


def main() -> None:
    server.run()  # stdio


if __name__ == "__main__":
    main()
