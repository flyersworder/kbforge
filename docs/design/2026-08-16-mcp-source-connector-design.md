---
type: design-note
title: kbforge — The Generic MCP Source Connector
description: One configurable connector for every MCP-backed system of record, built on a selector/reader split that lets an agentic selector drop in later without touching provenance; plus the fetch-side law that makes the split enforceable rather than conventional.
tags: [okf, mcp, connectors, agentic-fetch, selector, fetch-side-laws, producer]
timestamp: 2026-08-16T00:00:00Z
status: draft
okf_version: "0.2"
---

# kbforge — The Generic MCP Source Connector

**Status:** Draft v0.1 · **Amends:** [`../architecture.md`](../architecture.md) §4.1
(makes the "MCP-source connector base" concrete and adds a fetch-side law to §4.3)
**Builds on:** [`2026-07-19-agentic-ingest-design.md`](2026-07-19-agentic-ingest-design.md)
§3 — this note supplies the mechanics that note deferred to its §9 "retriever
contract" open item.

## 1. Problem

kbforge's connector story is **N sources × bespoke Python**. That is why, five
releases in, the core still ships zero credentialed connectors: every deployment
that wants a real system of record must write and maintain a package. "The
interface is the product" is a defensible stance, but it is expensive when the
interface is the *only* road in.

Meanwhile MCP is converging on exactly the thing kbforge needs — a standard,
read-oriented interface to internal systems. kbforge already assumes MCP on the
way *out* (serving, §4.4). Assuming it on the way *in* makes the library
symmetric and collapses the connector problem:

| | Before | After |
|---|---|---|
| New MCP-backed source | write a Python package | **config** |
| New non-MCP source | write a Python package | write a Python package |

Plugins stop being the main road and become the escape hatch. That is a healthier
position for an extension point, and it does not weaken the plugin system — the
generic connector is itself just a connector.

## 2. The load-bearing rule — selector / reader

The agentic-ingest note (§3.1) requires an agentic `fetch` to be a **retriever,
not an extractor**: source documents, verbatim, each with a stable
`ResourceAnchor`. Applied naively to a RAG-backed MCP server that rule appears
unsatisfiable, because a RAG search returns *chunks*, not documents — query-
dependent, relevance-ranked fragments whose boundaries move whenever anyone
re-tunes the chunker or swaps the embedding model.

The resolution is that "fetch" is two jobs, and only one of them has to be
deterministic:

| | Job | May be non-deterministic? | Needs stable identity? |
|---|---|---|---|
| **Selector** | *which* documents are worth reading | **yes** | no |
| **Reader** | fetch those documents verbatim | **no** | **yes** |

**They need not be the same transport.** A RAG tool is a perfectly good selector
even though its output is unusable as content: the chunks are consumed as a
*pointer* and discarded. The reader then fetches each selected document whole,
by id. Chunk instability stops mattering — re-tune the chunker all you like, the
reader still produces the same canonical document from the same id.

This generalizes past any one source:

| Source | Selector | Reader |
|---|---|---|
| Share folder behind a RAG server | `search` tool | `resources/read`, `get_document`, or the filesystem |
| An internal system's MCP server | `search` tool | `resources/read` or REST get-by-id |
| Firecrawl | agent-guided search | scrape of the chosen URL |

### 2.1 The reader requirement is inherent, not imposed

To be a kbforge source an MCP server must expose two things: **something to
select with** (may be fuzzy, may be agentic) and **a stable read-by-id**.

The second looks like a kbforge quirk. It is not. The §4.4 laws promise a
reviewer can follow a concept's `sources` entry to the artifact it came from. A
source you can only *search*, never *address*, cannot back that promise — there
is no stable target for the anchor to point at. Any knowledge base whose
citations cannot be followed is not auditable, whoever built it.

So a server lacking read-by-id is not an awkward case to work around; it is a
source that cannot yet be cited, and the fix belongs on the server. The
alternative — reconstructing documents out of RAG chunks — would put unstable
chunk boundaries into the identity of every mirrored document, which is the
churn failure the no-op rule exists to prevent.

## 3. Position: core, not a plugin

The connector ships **in core**, behind a `kbforge[mcp]` extra, mirroring how
`kbforge[llm]` gates the LLM synthesizer.

This does not violate the "core ships zero credentialed connectors" stance. The
precedent is already in the repo: the GitHub and GitLab **publishers** are
credentialed, ship in core, and take their token from an env var. The real stance
is "no *system-of-record-specific* connectors in core" — a generic MCP client is
a transport that knows nothing about Confluence or share folders, so it is the
same category as the forge publishers, not the same category as a Confluence
connector.

**Selectors are config, not a third plugin family.** Every plugin family is
another place third-party code runs inside the pipeline, and the trust guarantees
must hold across all of them. Ship built-in selectors chosen by name; promote to
a family only on evidence that a custom one is needed. Anyone who genuinely needs
one can write an ordinary connector plugin today.

## 4. Components and config

One new module, `src/kbforge/connectors/mcp_source.py`:

```
McpConnector                  the four hookimpls; owns the fetch loop
  ├─ McpClient                thin transport; calls ONLY allowlisted tools
  ├─ Selector  (swappable)    "which documents?" → list[DocRef]
  │    ├─ EnumerateSelector   resources/list or a configured id list
  │    ├─ QuerySelector       configured queries through the select tool
  │    └─ AgenticSelector     LLM loop over allowlisted tools   (0.7.0, §9)
  └─ Reader    (fixed)        DocRef → RawRecord, verbatim
```

`DocRef` is the only thing crossing between them: `{native_id, url | None}` —
what the reader needs to fetch, and what the anchor needs to cite.

```yaml
server: https://mcp.company.com/share   # or a stdio command
system: share_folder                    # see §4.1
selector: enumerate | query
queries: ["retention policy", ...]      # query selector only
select_tool: search
read_tool: get_document                 # or "resources/read"
token_env: SHARE_MCP_TOKEN
```

### 4.1 `system` must be configured

`ProposedChange.branch_hint` is derived from the `doc_id` prefix
(`synthesize.assemble`). A generic connector that hardcoded `system="mcp"` would
collide every MCP-backed source in an organization onto one `sync/mcp` branch and
one merge request. Configuring it per instance keeps each source on its own
branch with its own review posture, and it is what lets an internal share folder
and an external web source coexist without sharing a trust boundary — a
separation that then falls out of config rather than needing a rule.

### 4.2 Read-only is enforced as an allowlist

§4.1 requires an agentic fetch to call only read/resource operations. That intent
cannot be enforced as literally written: MCP exposes a tool's name and schema,
never whether it has side effects. `delete_all` and `search` are
indistinguishable to a client.

The enforceable form is: **the connector calls only the tools named in config,
and never discovers-and-invokes tools dynamically.** Whatever names config lists
are callable; nothing else is, whether or not the server advertises it. That is a
checkable property, and it is the same mechanism that bounds the agentic selector
later — the agent receives the config allowlist (which may then include a web
search tool) and nothing beyond it, turning "trust the agent" into "the agent
cannot reach a write tool."

## 5. Data flow and the manifest cursor

```python
def kbforge_fetch(config, cursor):
    client  = McpClient(server, allowlist={select_tool, read_tool}, token)
    refs    = selector.select(client, config, cursor)   # non-deterministic: fine
    refs    = dedupe_by_native_id(refs)                 # not optional
    records = [reader.read(client, ref) for ref in refs]
    return FetchResult(records, cursor=manifest, complete=selector.complete)
```

The dedupe is load-bearing rather than hygiene: a RAG selector returns chunk
hits, and one document routinely produces several. Without collapsing to distinct
`native_id`s the reader fetches the same document repeatedly and hands core
several records with identical `doc_id`s, where the last silently wins in the
mirror. §6 makes that failure loud.

`normalize` stays close to trivial, for a pleasant reason: a RAG server has
already extracted text in order to index it, so asking it for a document yields
text rather than a `.pptx`. Binary extraction stays on the server, which is
better placed to do it. The residual risk is server-side extraction *drift*
(PDF whitespace jitter between reads); that is canonicalization's job (§4.3),
handled by normalizing whitespace hard.

### 5.1 Why the cursor is a manifest

The cursor payload is a `(native_id → content_hash)` **manifest**.

Connectors are mirror-blind by design — they never read core's mirror. Core, in
turn, never derives deletions: `mirror.diff` marks `removed` only when a
connector hands it `deleted=True`, and it does not receive `FetchResult.complete`
at all. So neither side can independently notice that a document vanished: the
connector knows what it fetched but not what it fetched last time, and core knows
both but will not act on absence.

The manifest closes that gap from the connector's side. It is the connector's
private memory of what it saw, and it is the only way an MCP source can propagate
a deletion.

### 5.2 Deletion support is a property of the selector

| Selector | `complete` (nominal) | Tombstones | Why |
|---|---|---|---|
| `enumerate` | `True` | **yes** — `manifest − current` | full listing; absence is evidence |
| `query` | `False` | never | the set is query-shaped |
| `agentic` | `False` | never | as above, plus per-run variation |

`complete` is *nominal* only for `enumerate`: §7 downgrades it to `False` for that
run if any read fails, which also suppresses that run's tombstones. `query` and
`agentic` are `False` unconditionally — there is no path by which they become
`True`.

Name the limitation plainly: **an agentic or RAG-driven sync can add and update
concepts but can never remove one.** Stale concepts accumulate until an
enumerating pass runs. This is the fail-safe direction — a stale concept is
visible and reviewable, a wrongly-deleted one is silent data loss — but a source
whose deletions matter wants both selectors on one source: `enumerate` on a slow
schedule for correctness, `query`/`agentic` on a fast one for responsiveness.
Both write the same manifest.

## 6. The fetch-side law

One new law in `canonical.py` beside `assert_stability`, one call site in
`pipeline.run`, immediately after the existing stability law and before `diff`:

```python
docs = connector.kbforge_normalize(result.records)
assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
assert_fetch_contract(docs, complete=result.complete)          # §4.3, fetch side
changeset = diff(mirror_path, docs)
```

It runs after `normalize` rather than on `RawRecord`s because `doc_id` is what
the mirror keys on, and because tombstones only exist post-normalize —
`RawRecord` has no `deleted` field.

| Check | Catches |
|---|---|
| `doc_id` unique across `docs` | the RAG dedup failure (§5): one document, several chunk hits, last write wins |
| `anchor.native_id` non-blank | records that cannot be cited — the fetch-side mirror of §4.4 law 3 |
| `complete=False` ⟹ no `deleted=True` | a partial fetch manufacturing removals |

The third check is the reason to build this now rather than alongside the agent.
CLAUDE.md states the invariant as *"`FetchResult.complete` exists so a
rate-limited partial fetch can't manufacture removals."* That is currently true
only **vacuously**: nothing consumes `complete`, and nothing manufactures
removals, because deletions are entirely connector-emitted. The guarantee holds
by absence of mechanism, not by enforcement. The moment a query-driven selector
exists, a connector author who reasons "it wasn't in the results, so it's gone"
writes a data-loss bug that no current check catches.

**What the law deliberately does not check: verbatim-ness.** Core has no
independent access to the source, so it cannot distinguish a returned document
from an agent's summary of one. The law closes the *identity* half of
retriever-not-extractor; the *verbatim* half remains contract. That limit belongs
in the docstring, in the same spirit as §4.4's honest accounting of its own
reduced-strength laws.

The law is **core and unconditional** — not a flag, not per-connector. An opt-in
trust guarantee is not a guarantee. It therefore also runs for `local_files` and
`git_commits`, which the implementation must verify still pass.

## 7. Error handling

One rule: **a failure may reduce what the run claims, never what it delivers
silently.**

| Failure | Response |
|---|---|
| server unreachable / auth rejected | fail the run; no partial publish |
| a ref the reader cannot fetch | skip it **and downgrade `complete=False`** |
| rate limit or budget exhausted mid-loop | return what was read, `complete=False` |
| slow server | finite timeout, as the forge publishers already do |

The second row matters more than it looks: a dropped read must force
`complete=False` even under an `enumerate` selector. Otherwise the run reports a
complete listing while missing documents, and `manifest − current` reads those
gaps as deletions.

The manifest needs the matching rule:

- `complete=True` → **replace** the manifest with what was listed
- `complete=False` → **merge** previous ∪ what was read

Replacing on a partial fetch would discard knowledge of documents seen in an
earlier run, and a later complete run would then fail to notice they had been
deleted. Merging keeps every failure pointing the same way: a missed deletion
(stale concept, visible in review) rather than a spurious one (silent data loss).

## 8. Testing

The default suite never touches the network, so an injected fake MCP client — the
same shape as the publishers' fake transport, which pins the request we *intended*
to send.

**Offline:**

- each selector against the fake: `enumerate` derives tombstones from
  `manifest − current`; `query` never does
- dedupe: the fake RAG returns one document across three chunk hits → one record
- manifest merge-vs-replace across a partial run followed by a complete one
- **allowlist:** the fake client records every call; assert no tool outside config
  was invoked — this is how the read-only guarantee is *tested* rather than asserted
- `assert_stability` over the MCP `normalize`

**The law is mutation-proved**, per the repo convention — break what it guards,
confirm the failure, mutate in place and restore with `git checkout --`, and
assert on failure *messages* rather than slugs:

- duplicate `doc_id` → fails
- `complete=False` with a tombstone → fails
- blank `native_id` → fails
- `local_files` and `git_commits` still pass unchanged

### 8.1 The live suite runs against deepwiki

`https://mcp.deepwiki.com/mcp` is a public, credential-free MCP server, which
makes it a usable live target for `--run-live` alongside the existing forge suite.
Its tool surface is also an unusually clean illustration of §2:

| Tool | Role |
|---|---|
| `read_wiki_structure(repoName)` | topic list — useful as facets, not a selector |
| `read_wiki_contents(repoName)` | **valid reader**; no page parameter, so granularity is one document per repo |
| `ask_question(repoName, question)` | **extractor — must never back a fetch** |

`ask_question` is the tool that looks like the smart way in and is exactly the one
the retriever-not-extractor rule forbids: it returns freshly generated prose, so
every run would diff and churn a merge request. The live test asserts kbforge
reads through the citable tool and never calls the generative one.

Because `read_wiki_contents` takes no page parameter, deepwiki exercises the
**coarse-granularity** case: `native_id` is `owner/repo`, the anchor URL is
`https://deepwiki.com/owner/repo`, and one repo yields one concept. That is a
legal and useful shape — stable id, verbatim content, followable citation.

Two runs back to back pin the composition: first run publishes, second is a
no-op. Deepwiki content is AI-generated and may be regenerated when its upstream
repo changes, so a *long-interval* stability assertion would be testing deepwiki
rather than kbforge; back-to-back runs avoid that. Whether content is stable
enough for even that must be confirmed during implementation — if it is not, the
live test asserts the select-then-read mechanics and the law, and drops the no-op
assertion.

## 9. Scope

**In scope (0.6.0):** the generic connector, `enumerate` and `query` selectors,
the manifest cursor, the fetch-side law, the offline suite, the deepwiki live
test.

**Out of scope (0.7.0):** the `AgenticSelector`. It needs the lead-following
bounds, budget, and source-allowlist design still open in the agentic-ingest note
§9. The point of the selector/reader split is that it then arrives as a *swap*
against a proven reader, changing what gets read but never how — so provenance and
diffability are unaffected by how clever selection becomes.

This is a **minor version bump, not a patch**: a third-party connector emitting
duplicate `doc_id`s will now fail loudly where it previously corrupted the mirror
quietly. That is the desirable direction, but it is a new failure mode for
existing plugins and must be released as one.

## 10. Amendments to `architecture.md`

To be applied in this note's companion commit:

- **§4.1** — replace the "Future convenience (not core)" MCP-source note with the
  shipped connector; state the selector/reader split as the general form of the
  retriever-not-extractor rule; record that read-only is enforced as a config
  allowlist because side-effect-freedom is not introspectable over MCP.
- **§4.3** — add `assert_fetch_contract` as a fetch-side law beside the
  canonicalization laws, including what it deliberately does not check.
- **§4.2** — record that a feed-less MCP source expresses its cursor as a
  `(native_id → content_hash)` manifest, with the merge-vs-replace rule.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules.

## 11. Open items

- **Read-skipping.** The manifest could let the connector skip re-reading unchanged
  documents, but base MCP `resources/list` carries no etag or mtime, so v1 reads
  everything the selector returns. Gated on a server-offered change signal.
- **Transport scope.** stdio and streamable HTTP both exist; v1 may ship one. Which,
  and whether SSE is worth carrying, is undecided.
- **Facets from `read_wiki_structure`-style metadata.** Whether selector-visible
  metadata should populate `structured` facets, given `_facets` drops anything in
  `OKF_OWNED`.
- **Agentic selector bounds** (0.7.0): lead count, depth, token budget, and where
  the source allowlist lives. Inherited from agentic-ingest §9.
