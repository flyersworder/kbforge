---
type: design-note
title: kbforge — MCP as a Source Transport
description: The selector/reader split that lets a RAG-backed or agentic MCP server satisfy retriever-not-extractor, shipped as a separate kbforge-mcp package; the protocol-first response mapping that makes a new source config rather than code; and what remains deferred to 0.8.0.
tags: [okf, mcp, connectors, agentic-fetch, selector, producer]
generated: { by: human:flyersworder, at: 2026-08-18T00:00:00Z }
status: draft — ready to plan for 0.7.0; §10 remains deferred to 0.8.0
okf_version: "0.2"
---

# kbforge — MCP as a Source Transport

**Status:** Draft v0.3 · **Ready to plan** (0.7.0) · **Amends:**
[`../architecture.md`](../architecture.md) §4.1
**Builds on:** [`2026-07-19-agentic-ingest-design.md`](2026-07-19-agentic-ingest-design.md)
§3 — this note supplies part of the mechanics deferred to that note's §9
"retriever contract" open item.
**Depends on:** [`../architecture.md`](../architecture.md) §4.2, §4.4, §7 — the
fetch-side law shipped in 0.6.0, first and independently.

**Changed in v0.3.** The two blockers that gated the connector phase are resolved:
response mapping (§5) and transport/client library (§6), both from direct
observation of real servers rather than from reasoning about the protocol. The
live-test target moved from deepwiki to the AWS Documentation and GitHub MCP
servers (§8) after deepwiki was measured against the requirements and failed
them. Deletion and the cursor remain deferred (§10).

## 1. Problem

kbforge's connector story is **N sources × bespoke Python**. That is why, five
releases in, it still ships zero credentialed connectors: every deployment that
wants a real system of record must write and maintain a package. "The interface is
the product" is a defensible stance, but it is expensive when the interface is the
*only* road in.

MCP is converging on the read interface those connectors keep reimplementing.
kbforge already assumes MCP on the way *out* (serving, §4.4); a generic connector
on the way *in* makes new MCP-backed sources a configuration exercise and leaves
bespoke plugins as the escape hatch rather than the main road.

That bet is now underwritten by the ecosystem rather than by hope.
[Atlassian's official Rovo MCP server](https://github.com/atlassian/atlassian-mcp-server)
reached GA in February 2026, exposing **Confluence** — kbforge's canonical system
of record — at `https://mcp.atlassian.com/v1/mcp`, with its tools grouped by
intent as `read` · `write` · `search`. Atlassian arrived at the selector/reader
split (§2) independently and named it the same way. A generic MCP connector
therefore reaches Confluence without anyone writing a Confluence connector, which
is the single most valuable thing kbforge could do with one release.

## 2. The load-bearing rule — selector / reader

The agentic-ingest note (§3.1) requires an agentic `fetch` to be a **retriever,
not an extractor**: source documents, verbatim, each with a stable
`ResourceAnchor`. Applied naively to a RAG-backed MCP server that rule appears
unsatisfiable, because a RAG search returns *chunks* — query-dependent,
relevance-ranked fragments whose boundaries move whenever anyone re-tunes the
chunker or swaps the embedding model.

The resolution is that "fetch" is two jobs, and only one of them must be
deterministic:

| | Job | May be non-deterministic? | Needs stable identity? |
|---|---|---|---|
| **Selector** | *which* documents are worth reading | **yes** | no |
| **Reader** | fetch those documents verbatim | **no** | **yes** |

**They need not be the same transport.** A RAG tool is a fine selector even though
its output is unusable as content: the chunks are consumed as a *pointer* and
discarded. The reader then fetches each selected document whole, by id. Chunk
instability stops mattering — re-tune the chunker all you like, the reader still
produces the same canonical document from the same id.

### 2.1 The reader requirement is inherent, not imposed

To be a kbforge source an MCP server must expose **something to select with** (may
be fuzzy, may be agentic) and **a stable read-by-id**.

The second looks like a kbforge quirk. It is not. The §4.4 laws promise a reviewer
can follow a concept's `sources` entry to the artifact it came from. A source you
can only *search*, never *address*, cannot back that promise — there is no stable
target for the anchor to point at. Any knowledge base whose citations cannot be
followed is not auditable, whoever built it.

So a server lacking read-by-id is not an awkward case to work around; it is a
source that cannot yet be cited, and the fix belongs on the server. The
alternative — reconstructing documents out of RAG chunks — would put unstable
chunk boundaries into the identity of every mirrored document, which is the churn
failure the no-op rule exists to prevent.

### 2.2 MCP *resources* are not the reader, in practice

The obvious reader is `resources/list` + `resources/read`: read-only by
definition, stable URIs, spec-defined, no mapping required. It was measured rather
than assumed, and it is not available.

Across eleven MCP servers surveyed — GitHub, deepwiki, Context7, BigQuery, Data
Commons, Google Drive, Gmail, Calendar, Playwright, Chrome DevTools, Vercel —
`resources/list` returned **four** entries in total, all of them GitHub's
MCP-App *UI templates* (`ui://…`, `text/html;profile=mcp-app`). Not one content
resource anywhere.

Servers put their content behind **tools**. A design that requires resources would
work against approximately nothing, so the reader is a tool call and §5 is
unavoidable rather than optional.

### 2.3 The asymmetry: identity is an *input* to the reader

The reader is called with an id the selector already produced. Its response
therefore never has to *supply* identity — only bytes. Nothing has to be guessed,
so "concatenate the text content blocks" is a complete and deterministic reader
mapping even for a server that returns bare prose.

The hard mapping problem exists **only in the selector**, which must turn a
response into a list of ids it did not already know.

This is what shrinks §5 from "map arbitrary tool responses onto five fields" to
"extract an id list from a selector response" — a far smaller thing to specify,
and the reason the AWS Documentation server's `read_documentation(url) -> str` is
not a problem case at all despite returning unstructured markdown.

### 2.4 Read-only is structural, not a config list

`architecture.md:258` requires an agentic fetch to call only read/resource
operations. That cannot be enforced as written: MCP exposes a tool's name and
schema, never whether it has side effects. `delete_all` and `search` are
indistinguishable to a client.

The enforceable substitute is stronger than an allowlist key, and it costs no
config at all: **the callable set is exactly `{select.tool, read.tool}`** — the
two tool names already required by §5.2. There is no code path that calls a third
tool and no tool discovery loop, so there is no allowlist to misconfigure, forget,
or widen. Against a write-capable server such as Atlassian's, this is what stands
between a sync run and `createConfluencePage`.

Layered on top as defence in depth, MCP tools may carry a `ToolAnnotations`
block. kbforge **refuses to call a tool whose `read_only_hint` is explicitly
`False`**, and warns when it is unset.

The asymmetry is deliberate and load-bearing. The spec's default for
`read_only_hint` is `false` while the SDK's sentinel for "not declared" is `None`,
so the two states are distinguishable — and the naive rule
`if not read_only_hint: refuse` would conflate them and reject every server that
simply never set the annotation, which is most of them, including both live-test
targets.

Stronger than either, where a server offers it: **a server-side read-only mode**.
GitHub's remote server serves read tools only from `/readonly` URL paths
(`…/mcp/x/repos/readonly`) or under an `X-MCP-Readonly` header, and Atlassian
grants at the `read` / `write` / `search` permission-group level. Enforcement
there is outside kbforge's process entirely, so config SHOULD prefer a read-only
endpoint whenever the server publishes one — as §5.2's GitHub example does.

Note the honest limit, which the SDK states itself: annotations are hints, "not
guaranteed to provide a faithful description of tool behavior," and clients
"should never make tool use decisions based on ToolAnnotations received from
untrusted servers." The hint is a guard against honest misconfiguration, never a
security boundary. The structural two-tool set is the control.

And the limit that survives all three layers: **they constrain which tools are
reachable, never whether a reachable tool is side-effect-free.** Naming a mutating
tool as `read.tool` is a deployment error kbforge cannot detect. That is why the
read-only endpoint matters — it is the only layer that does not take the
deployment's word for it.

## 3. Position: a separate `kbforge-mcp` package

The connector ships as its own distribution, discovered through the
`kbforge.connectors` entry-point group. `registry.py:36` already calls
`load_setuptools_entrypoints(CONNECTOR_ENTRYPOINTS)`, so this needs **zero core
changes**.

This agrees with `architecture.md:254-256`, which already specifies an MCP-source
connector base as "*a separate package or clearly-optional helper, never the
core*." An earlier draft argued for core on the grounds that the GitHub and
GitLab publishers are credentialed and ship in core. That precedent is real but
does not transfer, for a mechanical reason:

> `kbforge[llm]` gates the LLM synthesizer through a lazy import inside a CLI
> branch (`__main__.py:163`), so a missing extra costs nothing. Connectors are
> registered **eagerly** — `registry.py:9-10` imports them at module top level and
> `build_registry()` instantiates each one, and `kbforge list` calls
> `kbforge_connector_info()` on every registered plugin. A module-level `import
> mcp` in a core connector breaks `kbforge list` for every user without the extra;
> guarding it makes `--connector mcp` report `unknown connector` rather than
> "install the extra."

A separate distribution has neither problem, and it keeps the credentialed
transport out of core's dependency surface entirely.

**Selectors stay config, not a third plugin family.** Every plugin family is
another place third-party code runs inside the pipeline. Ship built-in selectors
chosen by name; promote to a family only on evidence.

## 4. Shape

```
McpConnector                  the four hookimpls; owns the fetch loop
  ├─ McpClient                thin transport; calls ONLY the two configured tools
  ├─ Selector  (swappable)    "which documents?" → list[DocRef]
  │    ├─ StaticSelector      a configured id list — no select call at all
  │    ├─ QuerySelector       configured queries through the select tool
  │    └─ AgenticSelector     LLM loop over the configured tools        (later)
  └─ Reader    (fixed)        DocRef → RawRecord, verbatim
```

`DocRef` carries `{native_id, url | None, title | None}` — what the reader needs
to fetch, what the anchor needs to cite, and what §5.3 needs for a required field.
The reader stays fixed across all selectors: that is the whole point, since
swapping in an agentic selector then changes *what gets read*, never *how*,
leaving provenance and diffability untouched.

`system` is per-instance config, because `branch_hint` derives from the `doc_id`
prefix (`synthesize.assemble`). A connector hardcoding `system="mcp"` would
collide every MCP-backed source in an organization onto one `sync/mcp` branch and
one merge request. **This fixes the branch collision only — see §10.1 for the
cursor, which has the same collision and cannot be fixed this way.**

## 5. Response mapping

The claim in §1 that a new source is configuration rests here. The design is
**protocol-first**: MCP's own content-block types are the mapping vocabulary,
because a type the protocol already defines needs no configuration and no
mini-language. Config appears only where the protocol leaves a genuine choice.

### 5.1 The three tiers

Tiers are tried in a fixed order; the first that applies wins. Because identity
flows into the reader rather than out of it (§2.3), the two stages have very
different burdens:

| Stage | Tier 1 — resource blocks | Tier 2 — `structuredContent` | Tier 3 — bare text |
|---|---|---|---|
| **Selector** → id list | `resource_link.uri`, no config | list path + id field (§5.2) | **unsupported** — use `StaticSelector` |
| **Reader** → payload | `.text` / `.blob`, `.mimeType` | designated field | concatenate text blocks |

Tier 1 is one-to-many for the reader: a single call returning several resource
blocks yields several `RawRecord`s, which is how a "read this folder" tool
behaves. It falls out of the model rather than needing a special case.

Tier 3 **fails closed for selectors**. kbforge-mcp ships no prose heuristics — no
"first text content block," no regex over an outline. A server whose select tool
returns only prose is configured with an explicit `StaticSelector` id list or it
is not configured at all, and `kbforge_validate_config` says so offline by name.
That is what stops a heuristic from quietly becoming the de facto standard.

### 5.2 Config

The whole mapping surface, against the two live targets. AWS Documentation
(stdio, credential-free):

```yaml
- connector: mcp
  system: aws_docs                 # per-instance identity → doc_id prefix
  transport:
    kind: stdio                    # explicit discriminator — never sniffed
    command: uvx
    args: ["awslabs.aws-documentation-mcp-server@latest"]
  select:
    tool: search_documentation
    args: { search_phrase: "S3 bucket naming", limit: 20 }
    ids: { list: results, id: url, title: title }
  read:
    tool: read_documentation
    id_arg: url
```

GitHub (HTTP, credentialed) differs only where it must:

```yaml
  transport: { kind: http, url: https://api.githubcopilot.com/mcp/x/repos/readonly,
               auth_env: GITHUB_TOKEN }
  select: { tool: search_code, args: { query: "repo:acme/handbook path:docs" },
            ids: { list: items, id: path } }
  read:   { tool: get_file_contents, id_arg: path,
            static_args: { owner: acme, repo: handbook } }
```

`id_arg` answers the question v0.2 flagged as unanswerable in the abstract — the
argument name to pass the id under. It is not `id`; it is `url` for one target and
`path` for the other, and it is per-source config precisely because no default
could be right.

`static_args` is not decoration: GitHub's reader needs `owner` and `repo`
alongside the id. A design written against one target would have omitted it —
which is the over-fitting failure v0.2 predicted, caught by carrying two targets
through the config rather than one.

`auth_env` names an environment variable. Credentials never appear in config or on
the command line (`architecture.md:35`).

### 5.3 `native_id` is not the URL

Server-controlled ids are frequently URLs: AWS returns
`https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html`.
`concept_path` does `native.removesuffix(".md").strip("/")` and then `safe_join`
(`synthesize.py:39-43`), so that id renders to `concepts/https:/docs.aws.amazon.com/…`
— the §10.2 path hazard arriving on day one rather than hypothetically.

The reader therefore derives two values from one id, using fields
`ResourceAnchor` already has because identity and provenance were never the same
thing:

- **`native_id`** — a path-safe slug: URL path only, no scheme, no host, no query,
  no extension (`AmazonS3/latest/userguide/bucketnamingrules`). This is what
  `doc_id` and `concept_path` are built from.
- **`url`** — the full original URL, which is what the OKF `sources` entry cites.

The slug is additionally constrained: no leading `/`, no `.` or `..` segment, and
non-empty after normalization. That closes the `../../.github/workflows/x`
traversal at fetch time rather than as a `PathError` at publish time — after
synthesis, after tokens.

`title` is the one field with no protocol home. It comes from the selector's
`ids.title` when configured, else a tier-1 resource block's `name`, else the final
segment of the slug. Deterministic, and honest about being derived.

## 6. Transport and client library

**Discriminator.** `transport.kind` is an explicit `stdio | http` enum, replacing
v0.2's single `server:` string that carried two incompatible transports with no
way to tell them apart. `kbforge_validate_config` classifies offline from the
enum, with no URL sniffing and no network I/O.

**Library and async.** The official `mcp` Python SDK, whose v2 `Client` accepts a
URL string, a custom `Transport`, or an in-process server object. It is async-only,
so `kbforge_fetch` calls `asyncio.run()` **once**, wrapping a single
`async with Client(...)` session that performs the select and every read. The sync
hookimpl is preserved, the connection is reused across reads, and teardown is
deterministic.

The in-process constructor also settles a question v0.2 could not: the offline
test seam is neither a fake transport nor a fake session but a **real server**
(§8.2).

## 7. Error handling

**`is_error` is the sharpest trap in the connector.** Tool execution errors do not
raise client-side; they return a result whose `content` is populated *with the
error message*. Map that without checking and the error text becomes the document
body, passes synthesis, and ships as a concept. The SDK documentation is explicit:
always check `is_error` before processing `structured_content`. kbforge checks it
before anything else touches a result.

The rest turns on one rule, and it is where the 0.6.0 fetch-side law earns its
keep:

| Failure | Response |
|---|---|
| Selector truncated (paging, rate limit) | `complete=False` |
| One read fails (404, permission) | skip the document **and** `complete=False` |
| `is_error: true` on a read | treat as a read failure — never as content |
| Session, auth, or transport failure | raise; abort the run |

Row two is load-bearing. Skipping a document while still reporting
`complete=True` would, the moment §9's manifest lands, manufacture a deletion out
of a transient 403. `assert_fetch_contract` already refuses a tombstone under
`complete=False` (0.6.0), so the honest flag is what keeps that door shut.
Per-document failures degrade the run; session failures abort it. These are
different things and the connector must not blur them.

**Reserved keys.** Anything the connector puts in `CanonicalDocument.structured`
becomes a facet, and facets merge into top-level frontmatter. A
`structuredContent` key named `sources` or `generated` would shadow an OKF key in
the rendered file while the projection kept the good value — the dual-carrier bug.
`local_files._RESERVED_KEYS` exists for exactly this; kbforge-mcp needs its own,
covering `OKF_OWNED` plus the retired v0.1 names.

**Untrusted content.** Source content reaches the synthesizer as data, and
Atlassian's own documentation warns about prompt injection and tool poisoning
through MCP. kbforge's existing structural defences bound the blast radius rather
than eliminate it: links are resolved by kbforge from declared relations and never
taken from model prose, and deletion is structure rather than prose
(`ProposedChange.files_removed` is assigned by the pipeline, overwriting whatever
a synthesizer sets). Nothing here is new to MCP; it is worth stating because MCP
widens the set of sources that reach it.

## 8. Targets and testing

### 8.1 Why these targets

deepwiki was v0.2's live-test target. Measured against §2 it fails: it exposes no
resources; `read_wiki_contents(repoName)` takes no page parameter, so granularity
is one document per repo; `read_wiki_structure` returns a prose outline rather
than ids; and `ask_question` is an extractor that retriever-not-extractor
forbids. Its content is also AI-generated from the upstream repo, so kbforge would
be synthesizing concepts out of another model's synthesis and citing generated
prose as provenance. **Dropped.**

| Target | Role | Transport | Credentials | Exercises |
|---|---|---|---|---|
| [AWS Documentation](https://awslabs.github.io/mcp/servers/aws-documentation-mcp-server) | live test | stdio | **none** | tier-2 selector, tier-3 reader, URL→slug |
| GitHub | live test | http | `GITHUB_TOKEN` | tier-1 resource blocks, `static_args` |
| [Atlassian Confluence](https://github.com/atlassian/atlassian-mcp-server) | design target | http | OAuth / API token | write-capable server; §2.4 |
| `mcp-server-git` | negative fixture | stdio | none | §2.4's limit — five of its twelve tools mutate |

`mcp-server-git` is a **fixture for the limit, not a passing gate**. It has no
read-by-id tool at all — `git_show(revision)` returns a commit's patch, not a
document — so it fails §2.1 and `kbforge_validate_config` has nothing valid to
accept. What it demonstrates is the §2.4 residue: were someone to configure
`read.tool: git_commit`, kbforge would call it, and only an explicit
`read_only_hint: False` on that tool would stop it. Whether the server sets that
annotation is unverified and must be checked during implementation; if it does
not, the fixture documents the gap rather than closing it, and the test asserts
the validation refusal only.

AWS Documentation is the primary live test because it is officially maintained,
entirely read-only, and needs **no credentials** — so unlike every other live test
in this repo it can run in CI unattended. GitHub is the second because it returns
tier-1 resource blocks, which nothing else here does.

Confluence is a design target rather than a test target: it needs an Atlassian
Cloud site and admin enablement for API-token auth. The config in §5.2 must
plausibly serve it, verified by inspection, not by CI.

### 8.2 Three layers

1. **In-process, always runs, no network.** A `FastMCP` server defined in the test
   suite, driven by a real `Client(server)`. It emits one response per tier, an
   `is_error` result, and a tool annotated `read_only_hint=False`. Real client,
   real protocol, real serialization — no fakes. The control is *authored* rather
   than borrowed because no single real server produces all these shapes, which is
   the whole reason v0.2's response-mapping guesswork went unchecked.
2. **Subprocess stdio, no network.** The same server over a real stdio transport,
   covering the branch an in-process client skips.
3. **`--run-live`.** AWS Documentation (network, no credentials) and GitHub
   (`GITHUB_TOKEN`).

### 8.3 Mutation tests

Per CLAUDE.md, a test over a gate is worth what it catches. Each gate below is
broken **in place**, confirmed failing, and restored with `git checkout --`, with
assertions on the failure *message* rather than a slug:

- the two-tool callable set (§2.4) — try to call a third tool
- the `read_only_hint is False` refusal, *and* that `None` is still permitted
- the `is_error` check (§7) — remove it and the error text becomes content
- the `complete=False` downgrade on a failed read (§7)
- the `native_id` slug and traversal constraint (§5.3)
- `normalize` clock-freedom (§10.3)

## 9. Deletion: why it needs a manifest, and why it is deferred

Connectors are mirror-blind by design; core never derives deletions (`mirror.diff`
marks `removed` only on an explicit `deleted=True`). So neither side can notice a
document vanished: the connector knows what it fetched but not what it fetched
last time, and core knows both but will not act on absence. A connector-owned
`(native_id → content_hash)` manifest in `Cursor.payload` is the only way to close
that gap.

Deletion support would then be a property of the selector, not the source:

| Selector | `complete` | Tombstones |
|---|---|---|
| `StaticSelector` | `True` | **yes** — `manifest − current` |
| `QuerySelector` | `False` | never |
| `AgenticSelector` | `False` | never |

`StaticSelector` is the enumerating one precisely because its id list *is* the
scope: a configured list is complete by construction, which is what licenses
`complete=True` and therefore tombstones.

with the limitation named plainly: **an agentic or RAG-driven sync can add and
update concepts but can never remove one.** Stale concepts accumulate until an
enumerating pass runs — the fail-safe direction, since a stale concept is visible
in review and a wrongly-deleted one is silent data loss.

**All of this is deferred to 0.8.0.** 0.7.0 needs no manifest and no cursor: like
`local_files`, it re-selects every run and lets the mirror diff do the work, which
is why §10.1 does not block the connector phase.

## 10. Deferred to 0.8.0

### 10.1 Cursor identity — blocks the manifest phase, not the connector

The pipeline is asymmetric about cursor identity: it loads with
`_load_cursor(state_path, info.name)` (`pipeline.py:105`) and saves with
`_cursor_slot(state_dir, cursor.connector)` (`pipeline.py:81`). The two agree only
when `Cursor.connector == ConnectorInfo.name`. Both shipped connectors satisfy
this with a module constant.

But `kbforge_connector_info()` takes no config (`hookspecs.py:36`), so a generic
connector's name is static while its `system` is per-instance. Both branches fail
silently:

- `Cursor(connector=config["system"])` → written to `cursor-share_folder.json`,
  read from `cursor-mcp.json`. The manifest never returns, `manifest − current` is
  always empty, the static selector never tombstones anything — and an offline test that
  hand-seeds a cursor still passes.
- `Cursor(connector="mcp")` → every MCP source in a deployment shares one
  manifest. Sharing a `--state` directory is the natural deployment (sharing a
  mirror is safe, since `doc_id`s are `system:`-prefixed), so source B's enumerating
  run replaces A's memory and A's deletions are then never propagated.

Resolving this needs either a core change (config-dependent connector identity, or
a cursor-key parameter on `run()`) or namespacing the manifest inside
`Cursor.payload` under the configured `system`. The second is enforceable without
touching core and remains the front-runner; it has not been designed.

0.7.0 emits `Cursor(connector="mcp", payload={})` and reads nothing back, which is
forward-compatible with either resolution.

### 10.2 Manifest scope, and the collisions §5.3 does not close

- **Scope.** Running `StaticSelector` and `QuerySelector` on one source is unsafe:
  if the query surfaces a document outside the static list, the next enumerating run
  computes `manifest − current` and tombstones it — a **spurious** removal, the
  exact data loss §9 claims to avoid. Needs either a partitioned-by-selector
  manifest or a stated rule that the static selector's scope is a superset of every
  other selector's.
- **Suffix collision.** `concept_path` collapses `x:policy` and `x:policy.md` onto
  one path; the fetch-side law checks `doc_id` uniqueness and does not fire, and
  `_check_projection_coherence` compares already-collapsed sets. §5.3's slug
  removes the extension, which makes this *reachable* within one source rather
  than only across two — so 0.7.0 must reject a slug collision inside a single
  fetch, and the general fix (a core-side `concept_path` injectivity check)
  belongs with 0.8.0.
- **Blank `doc_id`.** `doc_id=""` passes `assert_fetch_contract`'s uniqueness
  check and `concept_path("")` renders `concepts//overview.md`, normalizing onto
  `concepts/overview.md` and silently colliding with a root-level concept. §5.3's
  non-empty constraint closes this connector-side; the core-side law does not yet.

Also smaller and unresolved: the manifest persists only on the `Published` path
(`pipeline.py:191`; `NoOp` returns at `:112`, `Aborted` at `:187`), so
merge-vs-replace semantics must be specified against a model where a no-op run's
manifest is discarded.

### 10.3 `retrieved_at`, and a guard that is not a convention

An MCP source has no mtime and no commit date; wall-clock at fetch is the only
value available. That is legal — `RawRecord.anchor_hint` exists precisely so
`fetch` supplies it and `normalize` stays clock-free — but it makes this the
connector where the temptation to call `datetime.now()` inside `normalize` is
highest, and `assert_stability` **cannot catch it**: `content_hash` excludes the
anchor by design (`canonical.py:33-36`), so both passes hash identically.

This is not deferred, only recorded here beside its cause. 0.7.0 ships the guard
`assert_stability` structurally cannot provide: a test that monkeypatches the
clock between two `normalize` calls over the same records and asserts
byte-identical output. Convention is replaced by a failing test.

## 11. Phasing

| Phase | Contents | Gate |
|---|---|---|
| **0.6.0** ✅ | [fetch-side law](../architecture.md) (§4.2, §4.4, §7) | none — shipped |
| **0.7.0** | `kbforge-mcp`: client, two-tool set, tiered mapping, `StaticSelector` + `QuerySelector`, AWS docs + GitHub live tests | none — §5, §6 resolved |
| **0.8.0** | manifest cursor, tombstones, merge-vs-replace | §10.1, §10.2 |
| later | `AgenticSelector` | bounds/budget/tool set from agentic-ingest §9 |

## 12. Amendments to `architecture.md`

Made when 0.7.0 ships, recorded now so the edits are not rediscovered:

- **§4.1** — replace the "Future convenience (not core)" note with the shipped
  package (the "never the core" judgement stands, §3); state the selector/reader
  split as the general form of retriever-not-extractor; record that read-only is
  enforced as a **structural two-tool set** (§2.4), not a config allowlist, since
  side-effect-freedom is not introspectable.
- **§4.2, line 280** — this is an **edit**, not an addition. The line already reads
  "a feed-less refresh connector expresses its cursor as a `(doc_id,
  content_hash)` manifest". If the manifest is keyed on `native_id` instead, say
  why (`native_id` is the fetch-side identity; `doc_id` only exists post-normalize)
  and change the sentence rather than adding a second one.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules.
