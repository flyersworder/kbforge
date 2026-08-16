---
type: design-note
title: kbforge — MCP as a Source Transport
description: The selector/reader split that lets a RAG-backed or agentic MCP server satisfy retriever-not-extractor, shipped as a separate kbforge-mcp package; plus the four blockers that must be answered before the connector can be built.
tags: [okf, mcp, connectors, agentic-fetch, selector, producer]
generated: { by: human:flyersworder, at: 2026-08-16T00:00:00Z }
status: draft — blocked, see §7
okf_version: "0.2"
---

# kbforge — MCP as a Source Transport

**Status:** Draft v0.2 · **Blocked** on §7 · **Amends:**
[`../architecture.md`](../architecture.md) §4.1
**Builds on:** [`2026-07-19-agentic-ingest-design.md`](2026-07-19-agentic-ingest-design.md)
§3 — this note supplies part of the mechanics deferred to that note's §9
"retriever contract" open item.
**Depends on:** [`../architecture.md`](../architecture.md) §4.2, §4.4, §7 — the
fetch-side law shipped in 0.6.0, first and independently.

## 1. Problem

kbforge's connector story is **N sources × bespoke Python**. That is why, five
releases in, it still ships zero credentialed connectors: every deployment that
wants a real system of record must write and maintain a package. "The interface is
the product" is a defensible stance, but it is expensive when the interface is the
*only* road in.

MCP is converging on the read interface those connectors keep reimplementing.
kbforge already assumes MCP on the way *out* (serving, §4.4); a generic connector
on the way *in* would make new MCP-backed sources a configuration exercise and
leave bespoke plugins as the escape hatch rather than the main road.

**How much of that promise survives is currently unknown**, and §7 is why: the
config-only claim depends on a response-mapping design that does not exist yet.

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

| Source | Selector | Reader |
|---|---|---|
| Share folder behind a RAG server | `search` tool | `resources/read`, `get_document`, or the filesystem |
| An internal system's MCP server | `search` tool | `resources/read` or REST get-by-id |
| Firecrawl | agent-guided search | scrape of the chosen URL |

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

### 2.2 Read-only is enforced as an allowlist

`architecture.md:258` requires an agentic fetch to call only read/resource
operations. That cannot be enforced as written: MCP exposes a tool's name and
schema, never whether it has side effects. `delete_all` and `search` are
indistinguishable to a client.

The enforceable substitute: **the connector calls only the tools named in config,
and never discovers-and-invokes tools dynamically.** Whatever names config lists
are callable; nothing else is, whether or not the server advertises it. That is a
checkable property, and it is the mechanism that would later bound an agentic
selector — the agent receives the config allowlist (which may then include a web
search tool) and nothing beyond it.

Note the honest limit: this constrains *which* tools are reachable, never whether
a named tool is side-effect-free. Naming a mutating tool as `read_tool` is a
deployment error kbforge cannot detect.

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
> branch (`__main__.py:161-162`), so a missing extra costs nothing. Connectors are
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
  ├─ McpClient                thin transport; calls ONLY allowlisted tools
  ├─ Selector  (swappable)    "which documents?" → list[DocRef]
  │    ├─ EnumerateSelector   resources/list, or a configured id list
  │    ├─ QuerySelector       configured queries through the select tool
  │    └─ AgenticSelector     LLM loop over allowlisted tools   (later)
  └─ Reader    (fixed)        DocRef → RawRecord, verbatim
```

`DocRef` carries `{native_id, url | None}` — what the reader needs to fetch and
what the anchor needs to cite. The reader stays fixed across all selectors: that
is the whole point, since swapping in an agentic selector then changes *what gets
read*, never *how*, leaving provenance and diffability untouched.

`system` is per-instance config, because `branch_hint` derives from the `doc_id`
prefix (`synthesize.assemble`). A connector hardcoding `system="mcp"` would
collide every MCP-backed source in an organization onto one `sync/mcp` branch and
one merge request. **This fixes the branch collision only — see §7.1 for the
cursor, which has the same collision and cannot be fixed this way.**

## 5. Deletion: why it needs a manifest, and why it is deferred

Connectors are mirror-blind by design; core never derives deletions (`mirror.diff`
marks `removed` only on an explicit `deleted=True`). So neither side can notice a
document vanished: the connector knows what it fetched but not what it fetched
last time, and core knows both but will not act on absence. A connector-owned
`(native_id → content_hash)` manifest in `Cursor.payload` is the only way to close
that gap.

Deletion support would then be a property of the selector, not the source:

| Selector | `complete` | Tombstones |
|---|---|---|
| `enumerate` | `True` (nominal) | **yes** — `manifest − current` |
| `query` | `False` | never |
| `agentic` | `False` | never |

with the limitation named plainly: **an agentic or RAG-driven sync can add and
update concepts but can never remove one.** Stale concepts accumulate until an
enumerating pass runs — the fail-safe direction, since a stale concept is visible
in review and a wrongly-deleted one is silent data loss.

**All of this is deferred** to a later phase than the connector itself, because
§7.1 is unresolved and because §7.5 shows the recommended "run both selectors on
one source" deployment is currently the dangerous one.

## 6. Live testing against deepwiki

`https://mcp.deepwiki.com/mcp` is a public, credential-free MCP server, usable for
`--run-live` alongside the forge suite. Its surface is an unusually clean
illustration of §2:

| Tool | Role |
|---|---|
| `read_wiki_structure(repoName)` | topic list — useful as facets, not a selector |
| `read_wiki_contents(repoName)` | **valid reader**; no page parameter, so granularity is one document per repo |
| `ask_question(repoName, question)` | **extractor — must never back a fetch** |

`ask_question` looks like the smart way in and is exactly what
retriever-not-extractor forbids: it returns freshly generated prose, so every run
would diff and churn a merge request.

Two caveats, both found in review and both real:

- Deepwiki exposes no MCP *resources*, so the live test must drive
  `EnumerateSelector` from **a configured id list of repo names** — a config field
  §4's sketch does not yet have (§7.2).
- Asserting "kbforge never calls the generative tool" is guaranteed by config
  rather than by kbforge, since §2.2 establishes that side-effect-freedom is not
  introspectable. Keep it as documentation; do not count it as coverage.
- Deepwiki content is AI-generated and may be regenerated when its upstream repo
  changes, so a long-interval no-op assertion would be testing deepwiki rather
  than kbforge. Back-to-back runs avoid that; whether even that is stable must be
  confirmed during implementation, and if it is not, the live test asserts the
  select-then-read mechanics and drops the no-op assertion.

## 7. Blockers

**None of these are "open items" in the deferrable sense. Each blocks the phase it
names, and this note should not become an implementation plan until §7.1–§7.3 are
answered.**

### 7.1 Cursor identity — blocks the manifest phase entirely

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
  always empty, `enumerate` never tombstones anything — and an offline test that
  hand-seeds a cursor still passes.
- `Cursor(connector="mcp")` → every MCP source in a deployment shares one
  manifest. Sharing a `--state` directory is the natural deployment (sharing a
  mirror is safe, since `doc_id`s are `system:`-prefixed), so source B's enumerate
  run replaces A's memory and A's deletions are then never propagated.

Resolving this needs either a core change (config-dependent connector identity, or
a cursor-key parameter on `run()`) or namespacing the manifest inside
`Cursor.payload` under the configured `system`. The second is enforceable without
touching core and is the current front-runner, but it has not been designed.

### 7.2 Response mapping — blocks the config-only promise

§1 claims a new MCP-backed source is configuration. Nothing in this note supports
that yet. Missing: the **argument name** to pass the id under (§6's own example is
`read_wiki_contents(repoName)` — not `id`, and another server will say
`document_id` or `uri`); how to extract `native_id`/`url` from an arbitrary select
tool's response envelope; how to derive `title`, `media_type`, and `payload` from
a tool result, where `CanonicalDocument.title` is required and both shipped
connectors populate it meaningfully.

Two engineers would build a JSONPath mini-language and a "first text content
block" heuristic respectively, and neither would serve the other's server. This is
the largest gap in the note.

### 7.3 Transport and client library — blocks the connector phase

`server: https://… # or a stdio command` carries two incompatible transports in
one string with no discriminator, and `kbforge_validate_config` must classify it
offline. Unnamed: the client library. The official `mcp` Python SDK is **async**
while `kbforge_fetch` is sync, so someone must decide on `asyncio.run` inside the
hookimpl — and that choice determines whether §6's "injected fake MCP client" is a
fake transport or a fake SDK session.

### 7.4 Where `retrieved_at` comes from

An MCP source has no mtime and no commit date; wall-clock at fetch is the only
value available. That is legal — `RawRecord.anchor_hint` exists precisely so
`fetch` supplies it and `normalize` stays clock-free — but it makes this the
connector where the temptation to call `datetime.now()` inside `normalize` is
highest, and `assert_stability` **cannot catch it**: `content_hash` excludes the
anchor by design (`canonical.py:16`), so both passes hash identically. Purity would
rest on convention exactly where convention is weakest.

### 7.5 Manifest scope, and `concept_path` collisions

Two hazards that only become reachable once `native_id`s are server-controlled:

- **Scope.** §5's "run `enumerate` and `query` on one source" is unsafe as stated:
  if `query` surfaces a document outside `enumerate`'s listing scope, the next
  enumerate computes `manifest − current` and tombstones it — a **spurious**
  removal, the exact data loss §5 claims to avoid. Needs either a
  partitioned-by-selector manifest or a stated rule that enumerate's scope is a
  superset of every other selector's.
- **Path collision.** `concept_path` does `native.removesuffix(".md").strip("/")`
  (`synthesize.py:39-43`), so `x:policy` and `x:policy.md` are distinct `doc_id`s
  rendering to one path; the fetch-side law checks `doc_id` uniqueness and would
  not fire, and `_check_projection_coherence` compares already-collapsed sets. A
  `native_id` of `../../.github/workflows/x` likewise reaches `safe_join` and dies
  as a `PathError` at publish time — after synthesis, after tokens. A `native_id`
  shape constraint belongs in the reader or the law.
- **Blank `doc_id` collision.** The same defect from the other end: `doc_id=""`
  passes `assert_fetch_contract`'s uniqueness check (nothing else is blank) and
  `concept_path("")` renders to `concepts//overview.md`, which normalizes onto
  `concepts/overview.md` — silently colliding with a root-level concept that
  every downstream validator then treats as legitimate.

Also unresolved and smaller: the manifest persists only on the `Published` path
(`pipeline.py:190`; `NoOp` returns at `:111`, `Aborted` at `:186`), so merge-vs-
replace semantics must be specified against a model where a no-op run's manifest
is discarded.

## 8. Phasing

| Phase | Contents | Gate |
|---|---|---|
| **0.6.0** | [fetch-side law](../architecture.md) (§4.2, §4.4, §7) | none — independent of MCP |
| **0.7.0** | `kbforge-mcp` package: client, allowlist, reader, `enumerate` + `query`, deepwiki live test | §7.2, §7.3 |
| **0.8.0** | manifest cursor, tombstones, merge-vs-replace | §7.1, §7.5 |
| later | `AgenticSelector` | bounds/budget/allowlist from agentic-ingest §9 |

## 9. Amendments to `architecture.md`

Deferred until 0.7.0 is unblocked; recorded now so the edits are not rediscovered:

- **§4.1** — replace the "Future convenience (not core)" note with the shipped
  package (the "never the core" judgement stands, §3); state the selector/reader
  split as the general form of retriever-not-extractor; record that read-only is
  enforced as a config allowlist, since side-effect-freedom is not introspectable.
- **§4.2, line 280** — this is an **edit**, not an addition. The line already reads
  "a feed-less refresh connector expresses its cursor as a `(doc_id,
  content_hash)` manifest". If the manifest is keyed on `native_id` instead, say
  why (`native_id` is the fetch-side identity; `doc_id` only exists post-normalize)
  and change the sentence rather than adding a second one.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules.
