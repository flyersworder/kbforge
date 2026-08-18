# kbforge-mcp

`kbforge-mcp` lets an MCP server be a [kbforge](https://github.com/flyersworder/kbforge)
source. Install it alongside kbforge and a source becomes a block of YAML instead of
a Python package: name a **select** tool that says which documents are worth reading
and a **read** tool that fetches one verbatim by id, and the connector does the rest
— it registers itself under the `kbforge.connectors` entry-point group, so
`kbforge list` shows `mcp` with no further wiring. What it fetches goes through the
same pipeline as any other connector: normalize, mirror, diff, and a reviewed
proposal. It is a *retriever*: it hands kbforge the source's own bytes and never its
own summary of them, which is what keeps a concept's provenance followable back to
the document it came from.

## Configure a source

The AWS Documentation server, which needs network but no credentials:

```yaml
system: aws_docs                 # per-instance identity; prefixes every doc_id
transport:
  kind: stdio                    # explicit; never sniffed from the config shape
  command: uvx
  args: [awslabs.aws-documentation-mcp-server@latest]
  env: [AWS_DOCUMENTATION_PARTITION]   # names of variables to pass through
select:
  tool: search_documentation
  args: { search_phrase: S3 bucket naming rules, limit: 3 }
  ids: { list: search_results, id: url, title: title }
read:
  tool: read_documentation
  id_arg: url                    # the argument name the reader takes the id under
```

kbforge takes connector config as repeated YAML-typed `--set` pairs, so that is one
key per flag:

```bash
kbforge run --connector mcp \
  --set system=aws_docs \
  --set 'transport={kind: stdio, command: uvx, args: [awslabs.aws-documentation-mcp-server@latest]}' \
  --set 'select={tool: search_documentation, args: {search_phrase: S3 bucket naming rules, limit: 3}, ids: {list: search_results, id: url, title: title}}' \
  --set 'read={tool: read_documentation, id_arg: url}' \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

An HTTP server differs only where it must — `transport: {kind: http, url: ...,
auth_env: GITHUB_TOKEN}`. `auth_env` and `transport.env` hold environment variable
*names*; a credential never appears in config or on a command line. `read` also
takes `static_args` for constants the reader needs alongside the id (an `owner` and
`repo`, say), and a source whose select tool cannot be mapped is configured with an
explicit `static_ids` list instead. A run checks all of this before it opens a
session, so a misconfigured source fails offline rather than mid-fetch.

## The two-tool guarantee

**The set of tools this connector can call is exactly the two you configured.**
Not a default allowlist, not a filtered discovery loop — there is no code path that
calls a third tool. Point it at a write-capable server and the write tools are
unreachable, because nothing enumerates them.

This is structural because it cannot be anything else: MCP tells a client a tool's
name and schema, never whether it has side effects, so `search` and `delete_all`
are indistinguishable. Two weaker layers sit on top. A tool whose `read_only_hint`
annotation is explicitly `false` is refused (an unset hint is permitted — most
servers never set one). And where a server publishes a read-only endpoint, config
should prefer it; that is the only layer enforced outside kbforge's own process.

What none of the three can do is tell you whether the tool you *named* as the reader
is side-effect-free. Naming a mutating tool there is a deployment error kbforge
cannot detect.

## A limit worth knowing before you adopt it

Response mapping is protocol-first: ids come from MCP's own resource links or from
`structuredContent`, and a select response that is neither is refused rather than
guessed at. That refusal is a real constraint, not a corner case. **A server can be
perfectly machine-readable and still be unusable as a selector here.** GitHub's
`search_code` returns machine-readable JSON *inside a text block* and declares no
`structuredContent`; kbforge-mcp refuses it, and kbforge's own live test against
GitHub supplies a `static_ids` list instead of using its search tool at all.

So "a new source is configuration" is unqualified for the reader, and conditional
for the selector: it holds when the server publishes its result ids as resource
links or as `structuredContent`, and otherwise you enumerate the corpus by hand in
`static_ids`. Check that before you plan around this connector. The option that
would close the gap — an opt-in flag to parse a text block as JSON — is not built
(design note §10.3).

## Design

The [design note](https://github.com/flyersworder/kbforge/blob/main/docs/design/2026-08-16-mcp-source-connector-design.md)
holds what remains deferred, including deletion support: this connector re-selects
every run and lets the mirror diff do the work, and it emits no tombstones, so a
document removed at the source leaves a stale concept until the deletion manifest
lands. The shipped design — the selector/reader split, the protocol-first
mapping, the read-only posture — is in
[`docs/architecture.md`](https://github.com/flyersworder/kbforge/blob/main/docs/architecture.md) §4.1.
