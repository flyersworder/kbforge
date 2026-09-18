# Example: a web source for kbforge-mcp

A reference MCP server, about 100 lines, that turns web search and web pages into a
kbforge source. It is not a kbforge plugin: [`kbforge-mcp`](../../packages/kbforge-mcp)
consumes it as-is, like any other MCP server. It wraps Firecrawl's HTTP API. Swap
the search backend for your own (Azure's Responses-API web search, say) and nothing
else changes.

It serves two jobs, and both use one server:

| Job | What it catches | kbforge-mcp selector |
|---|---|---|
| **Watch** | updates to pages you already track: product pages, newsrooms | `static_ids`: your curated URL list |
| **Scout** | what the list misses: new products, market news | the `search` tool, restricted to a domain allowlist |

## The contract

A wrapper for any search backend has to keep three rules. kbforge enforces the
same rules on itself.

1. **`search` selects; it never supplies content.** It returns candidate URLs as
   `structuredContent`: `{"results": [{"url": ..., "title": ...}]}`. It may be
   non-deterministic. An agentic search (an LLM that searches and cites) is fine
   *here*, as long as only the URLs it cites come back, never its answer text.
2. **`read(url)` returns the page's own content, never a summary.** It returns the
   same bytes for an unchanged page, so an unchanged page is a no-op in
   kbforge's diff. It raises on an error page rather than returning one.
3. **Both tools are read-only** (`readOnlyHint: true`).

The return annotation on `search` must be a parameterized dict
(`dict[str, list[dict[str, str]]]`). A bare `dict` makes the MCP SDK emit no
`structuredContent`, and kbforge-mcp then refuses the selector as prose.

## Run it

```bash
export FIRECRAWL_API_KEY=...
export WEB_SOURCE_ALLOWED_DOMAINS='bosch-semiconductors.com,st.com,nxp.com,ti.com,automotiveworld.com'
```

| Variable | Meaning |
|---|---|
| `FIRECRAWL_API_KEY` | required |
| `WEB_SOURCE_ALLOWED_DOMAINS` | required for `search`. Comma-separated, matched on whole labels (`ti.com` admits `news.ti.com`, not `evilti.com`). Empty is an error; `*` allows any. |
| `WEB_SOURCE_MAX_AGE_MS` | optional. Accept a cached scrape up to this age (Firecrawl's default when unset; `0` = always live). |
| `FIRECRAWL_API_URL` | optional; default `https://api.firecrawl.dev/v2` |

**Use one `system` for Watch and Scout, and don't map `title` in Scout.** A search
will find pages you already watch. Under two system names, that page would be two
documents rendering one bundle path, and kbforge aborts the run rather than let one
overwrite the other. Under one system it is one document, whoever found it. The
title has to come from the same place in both configs, or it flips between runs:
Watch has no search title to offer, so Scout must not supply one either.

```bash
T='{kind: stdio, command: uv, args: [run, --no-project, --with, "mcp>=2", --with, httpx,
    --directory, examples/web-source-mcp, python, -m, kbforge_web_source.server],
    env: [FIRECRAWL_API_KEY, WEB_SOURCE_ALLOWED_DOMAINS]}'

# Watch: the curated list
kbforge run --connector mcp --set system=web --set "transport=$T" \
  --set 'read={tool: read, id_arg: url}' \
  --set 'static_ids=[https://www.bosch-semiconductors.com/stories-and-events/eg120-redefining-efficiency-safety-and-reliability/]' \
  --mirror .kbforge/mirror --state .kbforge/state --out .kbforge/out

# Scout: the wider net (one config per query; tbs "qdr:m" = past month)
kbforge run --connector mcp --set system=web --set "transport=$T" \
  --set 'read={tool: read, id_arg: url}' \
  --set 'select={tool: search, args: {query: "SiC traction inverter gate driver new product", limit: 10, tbs: "qdr:m"}, ids: {list: results, id: url}}' \
  --mirror .kbforge/mirror --state .kbforge/state --out .kbforge/out
```

To feed a watched page into an application concept, add it to the grounding subject
map (`kbforge run --grounding`). Scout results can't be grounding yet, because
their URLs aren't known in advance. That needs a core change.

## What a live run against Firecrawl showed

Measured on 2026-09-18, not assumed:

- **Stable pages stay quiet.** Two live fetches (`maxAge: 0`) of a vendor article
  returned byte-identical markdown. Only Firecrawl's `scrapeId`/`indexId` metadata
  differed, and `read` never returns metadata.
- **Some pages embed per-request tokens.** A trade-press article behind Cloudflare
  Turnstile carries challenge links whose path changes on every fetch, which would
  make the article "modified" on every run. `clean.py` drops link targets on
  `challenges.cloudflare.com` and keeps their text. Turning that rule off makes
  the same live run publish a spurious change.
- **Search needs the allowlist.** A query about SiC gate drivers returned vendor
  press releases next to LinkedIn, Facebook and ResearchGate posts.
- **Search results were stable** across repeated identical queries. Set-based
  selection means a reordering changes nothing anyway, so only a genuinely new URL
  is a change.

## Limits worth knowing

- **Every page is a concept.** Scout results become reviewable concepts of their
  own. They don't update your application pages until kbforge can ground a
  concept on a search.
- **Nothing is deleted.** kbforge-mcp emits no tombstones, so a URL dropped from
  the Watch list, or no longer returned by Scout, leaves its concept in place.
- **Failed reads are silent in kbforge-mcp 0.1.0.** A read that fails (a 404, a
  rate limit) is skipped, and a run whose reads *all* fail reports `NoOp`. On a
  rate-limited plan, check the server's stderr for `429` responses before trusting
  a no-op.
- **One `limit`-sized result set per query.** Each query is its own source config.
  Budget Firecrawl credits as queries × limit reads per run, minus unchanged pages
  served from Firecrawl's cache.

## Tests

```bash
cd examples/web-source-mcp
uv run --no-project --with pytest --with "mcp>=2" --with httpx python -m pytest tests
```

These cover the allowlist and the markdown cleanup: the two rules the server
applies, as pure functions. The server itself is verified by the live runs above.
