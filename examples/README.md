# Examples

Worked examples of extending kbforge. Each is a self-contained package; none needs a
change to kbforge core. A connector plugin is discovered through an entry point, and a
source can equally be an MCP server that `kbforge-mcp` consumes through configuration.

- [**github-issues-connector**](github-issues-connector/) — a complete credentialed
  connector (~160 lines) that syncs a repository's GitHub issues into OKF concepts,
  with token auth, pagination, and a real incremental cursor. The template for
  writing your own connector.
- [**web-source-mcp**](web-source-mcp/) — a reference MCP server (~100 lines) that
  makes web search and web pages a source for `kbforge-mcp` with no plugin at all:
  a `search` selector behind a domain allowlist and a `read` tool that returns each
  page's own markdown. Wraps Firecrawl, and the search backend is the one function
  to swap (e.g. for Azure web search). Live-tested; its README records what real
  pages did.
