---
type: design-note
title: kbforge — Library Architecture & Connector Protocol (Sketch)
description: Package architecture, Pluggy hookspecs, connector protocol, and conformance rules for a Python library that standardizes the production side of OKF knowledge bundles.
tags: [okf, pluggy, connectors, knowledge-base, producer, agent-governance]
generated: { by: human:flyersworder, at: 2026-07-11T00:00:00Z }
status: draft
okf_version: "0.2"
---

# kbforge — Library Architecture & Connector Protocol

**Status:** describes kbforge 0.7.0; sections marked **not built** are specification, not shipped code · **Companion to:** [`context/knowledge-base-design.md`](context/knowledge-base-design.md)
**Name:** `kbforge` — *agent-first knowledge bases, forged from your systems of record.*
**Repo:** `flyersworder/kbforge` · connectors: `kbforge-<system>` · entry points: `kbforge.connectors`

## 0. What we are standardizing

OKF v0.2 standardizes the **artifact at rest** — files, frontmatter, `index.md`/`log.md`.
It says nothing about how bundles are produced, grounded, kept fresh, or trusted.
This library is the reference implementation of that missing half:

| Layer | Standardized by | Status |
|---|---|---|
| Artifact format | OKF v0.2 (Google) | exists |
| Semantic vocabulary (`type` taxonomy) | us, per domain | our design doc §5.4 |
| **Production protocol** (connectors, canonicalization, diff, provenance, publish) | **this library** | this spec |
| Serving protocol | MCP — or a context database that ingests the bundle (§4.4) | exists |

Design stance carried over from the main doc: **the core ships zero credentialed
connectors, zero CI logic.** Connectors are plugins; deployments are separate
repos. The interface is the product. Publishers are the exception that proves
it: publishing is the producer's own delivery mechanism, not an integration with
someone's system of record, so `github` and `gitlab` ship in core — reading their
credentials from the environment, never from config.

---

## 1. Package architecture

```
kbforge (core, PyPI)
├── kbforge/
│   ├── models.py          # Pydantic data model (§3)
│   ├── hookspecs.py       # Pluggy specs (§5) — ConnectorSpec, PublisherSpec
│   ├── registry.py        # plugin discovery via entry points
│   ├── pipeline.py        # the sync algorithm (§7) — core, NOT pluggable
│   ├── canonical.py       # stability checker, hashing (§4.3)
│   ├── mirror.py          # the replay-safe mirror and diff
│   ├── synthesize.py      # the stub synthesizer + the shared emit frame
│   ├── llm_synthesizer.py # the grounded LLM synthesizer (kbforge[llm])
│   ├── validate.py        # strict producer-side OKF checks + the §4.4 laws
│   ├── connectors/        # local_files, git_commits (credential-free references)
│   ├── publishers/        # dry-run, github, gitlab
│   └── __main__.py        # the CLI
│
kbforge-mcp          (packages/kbforge-mcp/ — any MCP server as a source, §4.1;
                      developed in this repo, released as its own distribution)
│
kbforge-<system>     (separate package per system of record, own release cycle;
                      none published yet — examples/github-issues-connector/ is
                      a complete worked reference, §6)
│
<deployment repo>          (private; config, credentials via CI vars, type vocab,
                            MR templates, schedule — everything org-specific)
```

**Not built:** `kbforge.testing`, the conformance test kit (§9), and the
`PipelineHooks` extension family (§5.3). Both are specified below and neither
ships; §10 sequences them.

Discovery: connectors register under the entry-point group **`kbforge.connectors`**.
`pip install kbforge-confluence` is the entire installation story.

```toml
# kbforge-confluence/pyproject.toml
[project.entry-points."kbforge.connectors"]
confluence = "kbforge_confluence.plugin"
```

**What is deliberately NOT pluggable:** the pipeline order (fetch → normalize →
mirror → diff → scope → synthesize → validate → publish), the no-op rule, and the
never-auto-merge rule. These are the trust guarantees of the standard; making them
pluggable would make them optional. Plugins extend *stages*; they cannot reorder or
remove them. (This is the same posture as ADR-2 in the main doc.)

---

## 2. Two plugin families

1. **Connectors** (`ConnectorSpec`, §5.1) — bring data *in* from a system of record.
   Two credential-free references ship in core; real systems of record are plugins.
2. **Publishers** (`PublisherSpec`, §5.2) — push proposals *out*. Three ship in core:
   `dry-run` (local), `github` (PR), `gitlab` (MR).

Synthesis (the LLM step) and validation are **core stages with narrow extension
hooks** (§5.3), not open plugin families — they carry the grounding contract and the
security posture, which we do not want third-party plugins silently weakening.

---

## 3. Data model (Pydantic)

```python
# kbforge/models.py
from datetime import datetime
from pydantic import BaseModel, Field


class ConnectorInfo(BaseModel):
    """Static self-description; used for registry listing and docs."""
    name: str                      # "confluence" — unique, entry-point name
    version: str
    source_system: str             # human label: "Atlassian Confluence"
    info_types: list[str]          # which KB info types this SoR is authoritative
                                   # for, e.g. ["runbook", "architecture-notes"]
    config_schema: type[BaseModel] # connector-specific config model


class Cursor(BaseModel):
    """Opaque incremental-sync watermark. Core persists it; only the owning
    connector interprets it (timestamp, sys_updated_on, etag set, git SHA...)."""
    connector: str
    payload: dict = Field(default_factory=dict)


class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Each anchor becomes one OKF v0.2 `sources` entry at emit time (§5.1), whose
    REQUIRED `resource` field takes this anchor's `url` — or, when there is none,
    falls back to "system:native_id" (see `synthesize._source_entry` for why
    that fallback is honest but not spec-sanctioned)."""
    system: str                    # "servicenow"
    native_id: str                 # sys_id / page id / repo path
    url: str | None = None         # human-clickable deep link
    retrieved_at: datetime
    content_hash: str              # hash of the CANONICAL form (§4.3), not the raw


class RawRecord(BaseModel):
    """One record as fetched. Persisted to the mirror's raw side for audit;
    never diffed directly (raw exports are volatile — see §4.3)."""
    anchor_hint: dict              # enough to build a ResourceAnchor later
    media_type: str                # "application/json", "text/html", ...
    payload: bytes


class FetchResult(BaseModel):
    records: list[RawRecord]
    cursor: Cursor                 # new watermark; core persists on success
    complete: bool = True          # False => partial fetch (rate-limited); core
                                   # may continue but must not treat absent
                                   # records as deletions


class CanonicalDocument(BaseModel):
    """The diff-stable unit. This is what the mirror stores and what change
    detection runs on. The stability laws in §4.3 apply here."""
    anchor: ResourceAnchor
    doc_id: str                    # stable across syncs: f"{system}:{native_id}"
    title: str
    text: str                      # normalized plain text / markdown
    structured: dict = Field(default_factory=dict)   # typed fields (owner, env...)
    relations: list[str] = Field(default_factory=list)  # doc_ids this links to
    deleted: bool = False          # tombstone — deletions are explicit, never
                                   # inferred from absence (see FetchResult.complete)


class ChangeSet(BaseModel):
    """Output of the core diff stage; input to synthesis scoping."""
    added: list[str]
    modified: list[str]
    removed: list[str]             # tombstoned doc_ids
    unchanged_count: int

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.modified or self.removed)


class ProposedChange(BaseModel):
    """What synthesis hands to a publisher: concept files + reviewable summary.
    The structured summary is what makes the MR a 90-second review, not
    archaeology (main doc §6 / reviewer-fatigue point)."""
    branch_hint: str
    files: dict[str, str]          # bundle-relative path -> full new content
    concepts: dict[str, "ConceptFrontmatter"]   # path -> validated frontmatter
                                   # projection; what the §4.4 validators check,
                                   # so the laws assert against structure, not
                                   # re-parsed markdown. `files` stays the
                                   # rendered content the publisher writes.
    summary: "ChangeSummary"


class ChangeSummary(BaseModel):
    """Producer-generated MR description, structured."""
    sources_changed: list[ResourceAnchor]
    claims_added: list[str]
    claims_modified: list[str]
    claims_removed: list[str]
    conflicts_flagged: list[str]   # "CMDB says owner=A; Confluence says owner=B"
    gaps_flagged: list[str]        # "no DR runbook found for app X"
    grounding_notes: list[str]     # claims whose evidence weakened


class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF v0.2 concept. Serialized to YAML
    frontmatter at write time; validated against the §4.4 laws before publish.
    OKF requires only a non-empty `type` and permits arbitrary extra keys.

    This is the §4.4 *projection*, not the whole frontmatter: the remaining
    strict-OKF fields (title, description, generated) live in the rendered
    file, where the strict validator checks them. At write time `generated_by`
    and `generated_at` serialize to the OKF `generated: {by, at}` block (§5.2,
    which supersedes v0.1's `timestamp`) and each anchor in `sources` to one
    OKF `sources` entry (§5.1) — so `whats_stale`, which reads the freshness
    stamp, sees law 4's."""
    type: str = ""                 # OKF's required field; validate checks non-empty
    facets: dict = Field(default_factory=dict)   # law 1: filterable keys
    sources: list[ResourceAnchor] = Field(default_factory=list)  # law 3: >=1,
                                   # enforced by validate (permissive so a
                                   # violation is reported, not a construct error)
    links: list[str] = Field(default_factory=list)   # law 2: must resolve
    generated_at: datetime | None = None         # law 4: retrieved_at; validate
                                   # requires presence. OKF `generated.at`.
    generated_by: str = ""         # OKF `generated.by`, an §7 actor:
                                   # "kbforge/<version>", or "kbforge/<model>"
                                   # when the LLM synthesizer wrote the prose
```

---

## 4. The connector protocol (contract, not just interface)

### 4.1 Lifecycle

```
validate_config ──▶ fetch(cursor) ──▶ normalize(records) ──▶ [core takes over]
     once/run          incremental        deterministic         mirror, diff,
                                                                scope, synth,
                                                                validate, publish
```

A connector implements exactly this and nothing downstream. Connectors never see
the bundle, never call the LLM, never touch git. (Rule-of-Two posture from main
doc §7: the component holding SoR credentials performs no consequential external
action — publishing is a different plugin family running in a different stage.)

**Transport is below the connector interface.** `fetch` names no protocol. A
connector may pull over REST, a vendor SDK, GraphQL, or an **MCP client** — the
core cannot tell and must not care. This is deliberate: baking HTTP (or MCP)
assumptions into the hookspec would leak transport into the trust boundary for no
benefit. Note the symmetry — kbforge already assumes MCP on the way *out*
(serving, §4.4); MCP on the way *in* is just one more transport under `fetch`.
kbforge can sit between MCP-in and MCP-out without *requiring* either.

**The MCP source connector, `kbforge-mcp` (shipped, not core).** When a SoR
already exposes an MCP server, a source is configuration rather than a package:
`packages/kbforge-mcp/` registers under `kbforge.connectors` like any third-party
connector and needs no core change. It stays out of core for a mechanical reason
rather than taste — `kbforge[llm]` gates the synthesizer behind a lazy import in a
CLI branch, so a missing extra costs nothing, whereas connectors are registered
*eagerly* and `kbforge list` calls `kbforge_connector_info()` on every one. A
module-level `import mcp` in a core connector would break `kbforge list` for
everyone who did not install the extra.

*Fetch is two jobs, and only one of them must be deterministic.* This is the
general form of retriever-not-extractor, and it is what lets a RAG-backed or
agentic server be a source at all:

| | Job | May be non-deterministic? | Needs stable identity? |
|---|---|---|---|
| **Selector** | *which* documents are worth reading | **yes** | no |
| **Reader** | fetch those documents verbatim | **no** | **yes** |

A RAG search makes a fine selector even though its chunks are unusable as content:
they are consumed as a *pointer* and discarded, and the reader then fetches each
selected document whole, by id. Re-tune the chunker all you like — no identity
moves. The read-by-id requirement that falls out of this looks like a kbforge
quirk and is not one: §4.4 law 3 promises a reviewer can follow a concept's
`sources` entry back to the artifact it came from, and a source you can only
*search*, never *address*, cannot back that promise. A server lacking read-by-id
is not an awkward case to work around; it is a source that cannot yet be cited.

*Read-only is a structural tool set, not a config allowlist.* The constraint this
paragraph used to state — a connector may call only **read/resource** operations,
so the seven-tuple's **R = read-only** (§8) holds — cannot be enforced as written:
MCP exposes a tool's name and schema, never whether it has side effects, so
`delete_all` and `search` are indistinguishable to a client. The enforceable
substitute is stronger and costs no config at all: **the callable set is exactly
the two configured tool names**, the selector's and the reader's. There is no tool
discovery loop and no allowlist key, so there is nothing to widen, misconfigure,
or forget. A tool's `read_only_hint` annotation is defence in depth layered on
top — refused when explicitly `false`, permitted when unset, because the SDK's
sentinel for "never declared" and the spec's default of `false` are different
states and conflating them would reject nearly every real server. Both layers
constrain *which* tools are reachable, never whether a reachable one is
side-effect-free: naming a mutating tool as the reader is a deployment error
kbforge cannot detect, which is why config should prefer a server-side read-only
endpoint wherever the server publishes one.

*Mapping is protocol-first, and it does not reach every server.* MCP's own
content-block types are the mapping vocabulary — resource blocks first, then
`structuredContent`, then bare text — so the ordinary case needs no configuration
and no mini-language, and a selector response that is bare prose fails closed
rather than being guessed at. **The limit, observed against a live server rather
than hypothesized: a server can be perfectly machine-readable and still be
unmappable as a selector.** GitHub's `search_code` returns machine-readable JSON
*inside a text block* and declares no `structuredContent`, so this mapping sees
bare text and refuses it; kbforge's own live test therefore drives GitHub from a
configured id list (`static_ids`) rather than from its search tool. "A new
MCP-backed source is configuration" holds unconditionally for the reader — where
identity is an *input*, so concatenating text blocks is complete rather than a
heuristic — and holds for the selector only when the server publishes its ids as
resource links or as `structuredContent`. Anything else needs a configured id
list, which means enumerating the corpus by hand. An opt-in flag that parsed a
text block as JSON before applying the id mapping would close the gap; 0.7.0 did
not take it. That option, and everything else this connector defers — deletion,
the manifest, and the cursor collision that blocks it — is in
[`design/2026-08-16-mcp-source-connector-design.md`](design/2026-08-16-mcp-source-connector-design.md).

*Retriever-not-extractor has an emit-side consequence.* Because the connector
never edits a source's bytes, the source's own framing arrives intact and reaches
the rendered concept. Two instances are known and neither is a connector defect:
AWS's documentation server prefixes every document with `AWS Documentation from
<url>:`, and a whole markdown document carries its own `#` heading, which
synthesis then renders *below* its own `# {title}` — a doubled heading. Any fix
belongs in synthesis, which is the stage allowed to interpret; a connector that
tidied either would be extracting.

*Agentic fetch is a transport, not a stage.* An agentic `fetch` — one that *decides
which* sources are worth reading and may follow leads, including via an agentic MCP
server behind the interface — is a permitted transport; kbforge cannot tell and must
not care. Two constraints bound it. It must be a **retriever, not an extractor**: it
returns source documents *verbatim*, each with a `ResourceAnchor`, and never its own
prose summary — interpretation is `synthesize`'s job (which reads only canonical
docs), and letting agent prose enter the canonical form breaks both provenance (§4.4
law 3) and no-op detection (unbounded volatility no `normalize` can strip). And the
**read-only** rule above is absolute no matter how capable the server is. Full
design (agentic retriever, refresh vs. discover, bootstrap):
[`design/2026-07-19-agentic-ingest-design.md`](design/2026-07-19-agentic-ingest-design.md).

### 4.2 Incremental contract

- `fetch(config, cursor)` where `cursor=None` means full backfill — this is the
  **bootstrap** path that first creates the KB. Refresh is the opposite: it re-runs the
  scheduled pipeline and lets core's `diff` (§7) detect change against the mirror,
  so it **cannot** bootstrap — over an empty mirror there is nothing to diff. Connectors
  stay bundle-blind either way; a feed-less refresh connector expresses its cursor as a
  `(native_id, content_hash)` manifest — keyed on `native_id` because that is the
  identity a connector *has* at fetch time, whereas `doc_id` only exists once
  `normalize` has run — so re-polls still reduce to only real change. See
  [`design/2026-07-19-agentic-ingest-design.md`](design/2026-07-19-agentic-ingest-design.md).
- The connector returns a new `Cursor`; the core persists it **only after** the
  whole pipeline run succeeds (so failed runs re-fetch — at-least-once semantics;
  normalize determinism makes replays harmless).
- Deletions must be **explicit tombstones** (`CanonicalDocument.deleted=True`).
  Absence from an incremental fetch never implies deletion; `FetchResult.complete`
  exists so rate-limited partial fetches don't trigger false "removed" diffs. This
  is enforced, not merely intended: `assert_fetch_contract` (§7) rejects a
  tombstone on an incomplete fetch before it ever reaches `diff`.
- `assert_fetch_contract` (§7) is the fetch-side law a connector's `normalize`
  output must satisfy, checked once per run, before `diff`:
  1. **Unique `doc_id`.** Two records sharing an id silently collapse onto one
     concept, last-write-wins, with nothing visibly broken.
  2. **Non-blank `native_id`.** A record without one cannot be cited — the
     fetch-side mirror of the §4.4 anchor-presence law.
  3. **No tombstone from an incomplete fetch** — the rule above, enforced.

### 4.3 Canonicalization laws (the load-bearing part)

`normalize()` must satisfy three laws. These are what defuse the noisy-diff risk
(main doc review: "SoR exports embed volatile fields; without normalization,
no-op detection fails and MR economics collapse").

1. **Determinism.** Same raw payload → byte-identical `CanonicalDocument`
   (stable key order, stable list order, normalized whitespace/encoding).
2. **Volatility exclusion.** Fields that change without meaning changing —
   export timestamps, view counters, `sys_mod_count`, ad-banner HTML — must not
   survive into the canonical form. `retrieved_at` lives on the anchor, which is
   excluded from the diff hash.
3. **Semantic sufficiency.** Everything synthesis is allowed to claim must be
   present in the canonical form — synthesis never reaches back to raw payloads.
   (Keeps the grounding contract checkable: claims trace to canonical docs,
   canonical docs trace to anchors.)

The core **enforces** law 1 mechanically: the test kit (§9) and an optional
runtime check normalize twice and compare hashes; a connector that fails is
rejected at registration in strict mode.

**The one thing that check cannot see.** Law 2 puts `retrieved_at` on the anchor
and the anchor outside the diff hash — so a `normalize` that called the clock
itself would produce a *different* document on the second pass and an *identical*
hash, and `assert_stability` would pass. The blind spot is structural, not a gap
to close: the hash excludes the anchor for good reasons. A connector with no
source-side mtime (an MCP source has none) is where the temptation to reach for
the clock in `normalize` is highest, and the only guard is a test that takes the
clock away between two `normalize` passes and requires the anchors to agree.
`kbforge-mcp` ships one.

**Why this cannot be deferred downstream.** Systems that index documents for
retrieval do deduplicate, but at the storage layer and on **byte identity** — hash
the incoming content, skip the write if it matches what is already stored. That
check is correct and nearly free, and on a real system-of-record export it
essentially never fires: serialization timestamps, collection ordering, and
rendered chrome differ on every pull, so the bytes differ, so the corpus is
re-stored and re-indexed in full on every refresh (and re-summarized, where the
index is LLM-generated). Volatility exclusion has to happen *before* the hash — at
ingest, inside the unit that knows which fields carry meaning, which is the
connector and nothing further downstream. That is why law 2 is a connector
obligation rather than a core utility, and why the no-op rule (§7) is stated over
`CanonicalDocument` and never over `RawRecord`. It is also the reason the no-op
rule is not merely a cost optimization: without it, every refresh presents as a
full-corpus change, and a human gate over a full-corpus change is not a gate.

### 4.4 Agent-facing artifact laws (the emit side)

§4.3 governs **ingest** (raw → canonical). These govern **emit** (canonical →
OKF concept), and carry the same status: load-bearing, mechanically checkable,
enforced at a fixed stage. They exist because the README's "agent-first" claim is
otherwise an *assumption about what happens after the MR merges* — kbforge is a
producer; the agent connects downstream via MCP and never touches this
architecture. These laws make the claim checkable.

**The serving contract we depend on (documented, not owned).** The main doc
§5.7 fixes the MCP read server's surface. Every affordance is powered by one of
three things in the artifact — naming them turns "serving is out of scope" into a
stated interface:

| MCP affordance | Powered by |
|---|---|
| `search_knowledge`, `list_concepts` — faceted browse | **frontmatter** |
| `related_concepts(id)` — graph neighbours | **resolvable links** + **anchors** |
| `whats_stale(area?)` — freshness | **frontmatter timestamps** |

kbforge guarantees the left column is satisfiable; it does **not** build the
serving layer — and deliberately does not require that layer to be an MCP server
we recognize. A context database that ingests the bundle (OpenViking and its kin)
is an equally valid consumer; it reads the same three artifact features under
different names. Enumerating the features rather than the API is what keeps the
serving side swappable: the laws are stated against *affordances*, so a bundle
that satisfies them is portable across serving implementations we have never
seen. The four laws are exactly "emit what those affordances read":

1. **Facet survival.** Every `structured` field synthesis relied on to make a
   claim appears as a **frontmatter key**, never only in prose. *Without it:*
   `list_concepts` / `search_knowledge` filters go dark (an agent asking "who owns
   app X" needs a structured answer, not prose to grep).
2. **Link resolvability.** Every cross-link **resolves** to an existing concept
   file (or is dropped, never dangling); meaning stays in the prose — OKF keeps
   links untyped, and we do not invent an edge vocabulary. *Without it:*
   `related_concepts` returns a broken graph, killing multi-hop reasoning.
3. **Anchor presence.** Every concept carries ≥1 `sources` entry in frontmatter
   (OKF §5.1), tracing to a canonical doc → a SoR. *Without it:* provenance and
   anchor-based `related_concepts`; the §4.3 grounding chain is only *useful* if
   it survives to the emitted frontmatter.
4. **Freshness legibility.** Every concept's frontmatter carries a machine-readable
   freshness stamp — `generated.at` (OKF §5.2), holding the anchor's
   `retrieved_at`. *Without it:* `whats_stale`, and the agent's ability to caveat
   a stale answer.

These four are the complete set for v0.1 of *this contract* (kbforge's own
versioning, not OKF's) — exactly what the serving affordances read,
no more (prose quality is synthesis's job, not mechanically checkable) and no fewer.

**The laws now speak OKF's vocabulary.** OKF v0.1 standardized only the artifact
shell, so laws 3 and 4 had to invent field names — `resource` for provenance,
`timestamp` for freshness. v0.2 makes provenance, trust, and lifecycle
first-class, and the laws now read its fields directly: law 3 checks `sources`
(§5.1), law 4 checks `generated.at` (§5.2). Nothing about what they enforce
changed; the encoding stopped being private. Two families v0.2 adds — `verified`
(§5.2) and `stale_after` (§5.5) — are deliberately not emitted yet, and neither
is `status: deprecated` (§5.4) in place of kbforge's hard delete; see
[`design/2026-08-08-okf-02-deferred-decisions.md`](design/2026-08-08-okf-02-deferred-decisions.md).

**What the runtime enforces vs. what the laws name.** The validators check what a
`ProposedChange` can decide alone, so two of the four run at reduced strength. The
reduction is deliberate and bounded, and stating it is the difference between a
checked claim and a slogan:

- **Law 1 is *well-formedness*, not *survival*.** The runtime check (slug
  `facet-wellformedness`) verifies that the facets present are filterable scalars
  or flat lists. It cannot verify the completeness direction — "a field the claim
  relied on must appear as a facet, not only in prose" — because that needs the
  source `CanonicalDocument.structured` synthesis read, which is not in a
  `ProposedChange`. Structurally impossible with the current inputs, not merely
  undecidable. Note the scope: the core checks presence of *the fields synthesis
  used*, never a fixed key list — which facet keys matter (owner, env, …) is a
  deployment vocabulary concern.
- **Law 3 checks anchor *presence*, not *validity*.** ≥1 anchor is required, but an
  anchor with empty identity fields still satisfies it. The grounding chain is only
  as strong as synthesis fills the anchor.
- **Law 2's confidence is contingent on normalization that does not exist yet.**
  Resolution is exact string membership, so it assumes links are already
  bundle-root-relative and normalized. A raw markdown cross-link
  (`../y/overview.md`, a `#section` anchor, a self-link) is not what it handles.
  Until the normalizing renderer lands, law 2 protects the *declared* `links`
  field under that precondition, not arbitrary body links.

Law 4 runs at full strength and additionally requires a timezone-aware stamp (a
naive one crashes `whats_stale`'s aware-minus-naive subtraction). Underneath the
four, a **projection↔files coherence** check runs first: the laws inspect
`concepts`, but the publisher writes `files`, so every non-reserved file must have
a projection and vice versa — otherwise a file ships unvalidated and
`run_validators == []` would be a false pass. The strict pass additionally binds
the OKF-owned keys by value (§7), because coherence binds path *sets* and the two
carriers are read by different consumers. A third binding under the same slug
requires `files` and `files_removed` to be disjoint: nothing else inspects
`files_removed`, so a path in both — the emit-side symptom of a duplicate
`doc_id` where one copy was tombstoned (§4.2) — would otherwise reach the
publisher as both a write and a delete with `run_validators() == []` still
reading clean.

**Named paths from reduced to full strength** (deferred, not blocking):

- **Law 1 survival.** Thread the source `CanonicalDocument.structured` into the
  validator so it can check that fields the claim relied on became facets.
- **Law 3 anchor validity.** Min-length constraints on `ResourceAnchor.system` /
  `native_id` / `content_hash`. A model-level constraint is acceptable here: an
  anchor with no identity is nonsensical to *represent*, unlike a law-violating
  but meaningful concept.
- **Law 2 link normalization.** Build the renderer that normalizes emitted
  cross-links to bundle-root-relative paths, and extract the links actually present
  in the rendered body rather than only the declared field.
- **Freshness sanity bound.** Law 4 rejects naive stamps but not future-dated ones.
  A "not in the future" check needs a clock, which the pure validator deliberately
  lacks — so it would live in the pipeline, if wanted.
- **Typed relations.** OKF keeps links untyped and we accept that. If a real
  multi-hop use case demands typed edges, revisit a governed private vocabulary
  (`depends_on`, `owned_by`) like the type vocabulary in §5.4 of the companion doc.

**Law 4 also dissolves the freshness-vs-human-gate tension.** The never-auto-merge
rule (a trust guarantee) seems to conflict with an agent's need for current data: a
CMDB owner change waits for MR review. But if staleness is *legible in the
artifact*, the agent (via `whats_stale`) caveats — "owner per CMDB as of 3 days
ago; update may be pending." Slow propagation becomes visible metadata, not a
silent correctness bug. Human gate intact, agent safe; no change to the rule.

---

## 5. Pluggy hookspecs

```python
# kbforge/hookspecs.py
import pluggy
from kbforge.models import (
    ConnectorInfo, Cursor, FetchResult, RawRecord,
    CanonicalDocument, ProposedChange,
)

PROJECT = "kbforge"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)
```

### 5.1 `ConnectorSpec`

```python
class ConnectorSpec:
    """One plugin class per system of record."""

    @hookspec
    def kbforge_connector_info(self) -> ConnectorInfo:
        """Static self-description. Called at registration."""

    @hookspec
    def kbforge_validate_config(self, config: dict) -> list[str]:
        """Return human-readable problems ([] = ok). Called once per run,
        before any network I/O. Credential *presence* is checked here;
        credential *values* come from env/CI vars, never from the bundle."""

    @hookspec
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        """Pull raw records changed since `cursor` (None = full backfill).
        Must respect rate limits internally; may return complete=False."""

    @hookspec
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        """Deterministic, volatile-free, semantically sufficient (§4.3).
        Pure function of its input: no network, no clock, no randomness."""
```

### 5.2 `PublisherSpec`

```python
class PublisherSpec:
    """Where proposals go. MUST NOT merge (§5.2)."""

    @hookspec
    def kbforge_publisher_info(self) -> ConnectorInfo: ...

    @hookspec
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        """Open a review request (MR/PR). Returns its URL.
        MUST NOT merge. Must be idempotent per (branch_hint, content-hash):
        re-running a failed pipeline updates the same MR, never opens twins."""
```

Three publishers ship in core: `dry-run` (default; writes to a directory),
`github` (pull requests) and `gitlab` (merge requests). All three implement
`kbforge_publish` and `kbforge_validate_publish_config`; none implements a merge
method, which is how §5.2's never-merge rule is enforced structurally rather
than by convention.

The two forge publishers sit behind a `ForgeClient` protocol whose methods name
intentions (`put_files`, `find_open_pr`) rather than REST endpoints, because the
forges decompose "commit these files" incompatibly — GitLab in one call, GitHub
in four. They are unconditional rather than gated behind an extra: both run on
stdlib `urllib`, so there is no dependency for an extra to install. Deliberately
absent, so their absence reads as a decision rather than an omission: labels,
reviewers and draft PRs; pagination in `find_open_pr` (a branch has at most one
open PR); rate-limit backoff (a run makes fewer than ten calls); and auto-merge,
which is excluded permanently rather than deferred.

Their offline tests inject a fake transport, which pins the request we intended
to make but cannot judge whether the intent was right — a real forge caught two
defects the offline suite structurally could not see. Hence `tests/test_forge_live.py`
(`--run-live`), which asserts through `gh`/`glab` rather than through kbforge's
own readers.

While a review request is open, a run sets the sync branch from the branch
itself rather than from the default branch, so successive runs accumulate into
one review request. When none is open the branch is rebuilt from the default
branch, so a merged or abandoned request leaves no stale *branch* behind.

That is a property of the branch only, and emphatically not of the content.
The mirror advances after every successful publish, so a concept carried by a
request that is closed without merging is never re-proposed: the target repo
lacks it permanently. The same holds in reverse for a deletion — once published,
the doc is gone from the mirror, so a later tombstone is not even a removal.
Closing a kbforge review request without merging therefore discards its contents
for good. Abandon a request by merging it, or by resetting **both** the mirror
and the connector's cursor (`_load_cursor`/`_save_cursor` in `pipeline.py` keep
it in the state directory, at `<state-dir>/cursor-<connector-name>.json`,
separate from the mirror). Deleting the mirror alone is not enough for an
incremental connector: the surviving cursor still bounds `kbforge_fetch` to
records past it, so the next run can fetch few or no records, `ChangeSet.is_noop`
fires, and nothing is re-proposed. Only deleting both re-proposes everything
from scratch.

Deletions travel
as `ProposedChange.files_removed`, assigned by the pipeline rather than by a
synthesizer: deletion is structure, not prose, so a model cannot delete a
concept it dislikes.

Declined as scope, deliberately rather than by omission: rebasing the sync
branch when the default branch moves under an open review request (the branch
may go stale; the forge's own merge handles it, and conflicts are unlikely
because only kbforge writes concept files); inferring a deletion from a
document's absence (tombstones stay explicit — see §4.2); and flagging
non-kbforge commits pushed onto the sync branch in the review body (the README
documents the sharp edge instead).

### 5.3 Core-stage extension hooks (narrow, additive-only) — **not built**

Specified, not implemented: `hookspecs.py` defines `ConnectorSpec` and
`PublisherSpec` only, and `registry.build_registry` registers those two. Nothing
calls `kbforge_extra_validators` or `kbforge_run_observer` today, so a plugin
advertising them is silently inert. The shape below is the intended one; it is
recorded here so the §4.4 laws' "core, never additive" posture has something
concrete to contrast with.

```python
class PipelineHooks:
    """Observability + additive checks. Cannot veto-free the pipeline's own
    gates; a hook can only ADD failures, never remove them."""

    @hookspec
    def kbforge_extra_validators(self) -> list["Validator"]:
        """Contribute bundle validators run in CI stage: secret scan (gitleaks),
        PII scan (GDPR / contact-type concepts), external-URL link check, vocab
        conformance. Bundle-internal link resolvability is NOT an extra — that
        is §4.4 law 2, enforced core."""

    @hookspec
    def kbforge_run_observer(self, event: str, payload: dict) -> None:
        """Telemetry: stage timings, token spend (LiteLLM budget), diff sizes,
        no-op rate. Feeds the silent-staleness alerting from main-doc review."""
```

### 5.4 Registration and dispatch

Multiple connectors coexist; hooks are dispatched **per connector**, not
broadcast — the registry keeps one `PluginManager` but drives each connector
through a `subset_hook_caller`, so `fetch` on Confluence never fans out to
ServiceNow:

```python
# kbforge/registry.py (sketch)
import pluggy
from kbforge import hookspecs

def build_registry() -> dict[str, "BoundConnector"]:
    pm = pluggy.PluginManager(hookspecs.PROJECT)
    pm.add_hookspecs(hookspecs.ConnectorSpec)
    pm.add_hookspecs(hookspecs.PublisherSpec)
    pm.add_hookspecs(hookspecs.PipelineHooks)
    pm.load_setuptools_entrypoints("kbforge.connectors")

    registry = {}
    for plugin in pm.get_plugins():
        caller = pm.subset_hook_caller  # bind hooks to this plugin only
        info = plugin.kbforge_connector_info()
        registry[info.name] = BoundConnector(info=info, plugin=plugin)
    return registry
```

---

## 6. Example connector skeleton

```python
# kbforge_confluence/plugin.py
from kbforge.hookspecs import hookimpl
from kbforge.models import *


class ConfluenceConfig(BaseModel):
    base_url: str
    space_keys: list[str]
    token_env_var: str = "CONFLUENCE_TOKEN"   # name of the env var, never the value


class ConfluenceConnector:

    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="confluence",
            version="0.1.0",
            source_system="Atlassian Confluence",
            info_types=["runbook", "architecture-notes"],
            config_schema=ConfluenceConfig,
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        problems = []
        cfg = ConfluenceConfig.model_validate(config)
        if not os.environ.get(cfg.token_env_var):
            problems.append(f"env var {cfg.token_env_var} not set")
        return problems

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        cfg = ConfluenceConfig.model_validate(config)
        since = (cursor.payload.get("last_sync") if cursor else None)
        pages, watermark = _cql_pages_modified_since(cfg, since)   # handles paging
        return FetchResult(
            records=[_to_raw(p) for p in pages],
            cursor=Cursor(connector="confluence", payload={"last_sync": watermark}),
        )

    @hookimpl
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        docs = []
        for r in records:
            page = json.loads(r.payload)
            docs.append(CanonicalDocument(
                doc_id=f"confluence:{page['id']}",
                title=page["title"].strip(),
                text=_storage_format_to_markdown(page["body"]),  # strips macros,
                structured={"space": page["space"]["key"],       # view counters,
                            "labels": sorted(page["labels"])},   # volatile HTML
                relations=sorted(_extract_page_links(page)),
                anchor=_anchor(page, r),
            ))
        return docs
```

---

## 7. The core pipeline (fixed order — this IS the standard)

```python
# kbforge/pipeline.py — shape of the run loop; see the module for the real thing
def run(connector, publisher, *, config, mirror, state_dir, publish_config,
        synthesizer=None) -> NoOp | Aborted | Published:
    problems = connector.kbforge_validate_config(config)
    if problems: raise ConfigError(...)                       # fail fast, no I/O

    result = connector.kbforge_fetch(config, load_cursor(state_dir, name))
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
    assert_fetch_contract(docs, complete=result.complete)      # §4.2 fetch contract
    changeset = diff(mirror, docs)

    if changeset.is_noop:
        return NoOp()                                         # no MR. ever.

    proposal = synthesizer.synthesize(                        # LLM stage; grounding
        changed_docs, changeset, existing_paths,              # contract lives here,
    )                                                         # scoped to changed

    failures = run_validators(proposal, existing_paths)       # strict OKF + §4.4
    if failures: return Aborted(failures)                     # laws, core only

    url = publisher.kbforge_publish(proposal, publish_config) # opens MR; never merges
    save_cursor(state_dir, result.cursor)                     # only on full success
    return Published(url=url)
```

`assert_fetch_contract` runs on `normalize` output rather than raw records —
`doc_id` is what the mirror keys on, and a tombstone only exists post-normalize.
Two things it deliberately leaves unchecked, so a reader does not credit it with
more than it does: it cannot confirm a fetched document is verbatim, since core
has no independent access to the source and so cannot distinguish a returned
document from an agent's summary of one — it closes the *identity* half of
retriever-not-extractor (§4.1), leaving the *verbatim* half a contract
obligation on the connector. And it does not make `normalize` clock-purity
checkable: `assert_stability` compares `content_hash`, which excludes the
anchor by design (§4.3 law 2, so `retrieved_at` doesn't make its own hash
circular), so a `datetime.now()` called inside `normalize` hashes identically
on both passes and passes that gate.

**One connector per run**, not a registry fan-out: the CLI resolves a single
connector by name and calls `run` with it, so multi-source assembly is a
deployment's business (run kbforge once per system of record, each with its own
mirror, cursor, and sync branch) rather than the core's. That keeps the no-op
rule and the branch-per-system model decidable from one run's inputs.

Everything main-doc §5.3 requires falls out of the seams: change-scoped updates
(diff drives synthesis scope), no-op detection (`is_noop` gate), grounding
(synthesis reads only canonical docs, emits `sources` = anchors), reviewability
(`ChangeSummary` becomes the MR body), and the security split (fetch stage holds
credentials but no external action; publish stage acts but holds no SoR access).

The §4.4 artifact laws are checked inside `run_validators` as **core** validators —
never the additive `kbforge_extra_validators` hook (§5.3). They are trust guarantees
of the standard, so making them opt-in would make them optional — the same posture
as the no-op and never-auto-merge rules. `synthesize` is a stage backed by a
`Synthesizer` object injected into `run` — `StubSynthesizer` by default,
`LLMSynthesizer` optionally; when the LLM is used, it writes prose inside a
kbforge-owned frame, and you *check* its output against the laws, you do not trust
it to emit them (same posture as `assert_stability` for §4.3 law 1). A concept
that violates any law fails the run; no MR opens for a non-conformant artifact.
The LLM synthesizer is deliberately minimal — one canonical doc → one concept, a
per-concept token budget, oversized sources truncated with a `grounding_notes` flag,
and the model reached through Pydantic AI's LiteLLM provider (so OpenRouter and a
self-hosted gateway share one config path). Deferred to later increments: a
faithfulness judge (a second pass verifying each prose claim traces to the source),
multi-doc merge/split, and recursive chunking for sources beyond the context window.
Law 2 is checkable purely within the
proposed bundle plus `main` — no network, no running MCP server. Fields are
permissive by design — the validate stage is the single accountable gate for the
laws, so a violating concept is constructed and reported, never rejected at
construction. `run_validators` therefore runs a **projection↔files coherence** check
before the per-concept laws (`_check_projection_coherence`): because the gate is the
single point of accountability, it must also catch the producer *omitting* a
projection for a file it ships — otherwise the gate is bypassed by silence, not by a
detectable bad emission.

---

## 8. Connection to the agent-contracts family

kbforge is one of three sibling projects, each a *contract for agents* at a
different seam:

| Project | Governs | Substrate | Seam |
|---|---|---|---|
| [`ai-agent-contracts`](https://pypi.org/project/ai-agent-contracts/) | resources, time, lifecycle — the formal spine | any agent | runtime budget |
| [`agentic-data-contracts`](https://pypi.org/project/agentic-data-contracts/) | semantic consistency + rules, enforced at query time | **structured** data (SQL / metrics) | agent *consumes* structured data |
| **kbforge** | grounding, freshness, provenance of produced knowledge | **unstructured** knowledge (OKF docs) | agent *consumes* knowledge |

The two data-contract projects are the **consumption and production halves of
"what the agent knows"** — one structured, one unstructured. They converged
independently on the same primitive: **freshness must be legible to the agent.**
kbforge law 4 + `whats_stale` mirror `agentic-data-contracts`' `find_stale()` /
`last_reviewed` / `stale` — strong evidence the pattern is real, not retrofitted.

**Formal mapping (the spine).** Each connector is a bounded execution unit that
maps onto the seven-tuple: **I** = (config, cursor); **O** = (canonical docs,
cursor′); **S** = the SoR named in `ConnectorInfo`; **R** = read-only,
rate-limited; **T** = per-run invocation; **Φ** = the canonicalization laws (§4.3)
as checkable postconditions; **Ψ** = the stability/tombstone invariants the test
kit verifies. The §4.4 artifact laws extend this to the **emit** side: they are
additional **Φ** postconditions on the `synthesize → validate` composition, and
`run_validators` is their **Ψ** verifier. The pipeline is then a *composition* of
contracted units with the trust properties (no-op, human gate) provable at the
composition level — a small production instance of the Paper 2
conservation-under-composition argument, worth a footnote there.

**A future connection (not built).** `agentic-data-contracts`' `lookup_domain`
returns hand-authored YAML business context ("revenue is recognized at
fulfillment") that goes stale. kbforge produces exactly that kind of grounded,
provenanced, fresh domain knowledge as OKF concepts — so a `type: domain` concept
could *feed and refresh* what `lookup_domain` serves, via an `OkfSource` adapter on
ADC's side (sibling to its `YamlSource` / `DbtSource` / `CubeSource`), carrying
provenance and freshness across the boundary. The §4.4 laws are exactly what make
that adapter mechanizable. kbforge grounds *what a metric means and where it came
from*; ADC keeps owning the executable SQL. Full design (boundary artifact, the
three capabilities — grounded definitions, shared freshness, drift detection):
[`design/2026-07-18-datacontract-bridge-design.md`](design/2026-07-18-datacontract-bridge-design.md).
Cross-project, out of scope for v0.1; kbforge core needs no change for it.

---

## 9. Conformance & the contract-test kit — **not built**

`kbforge.testing` does not exist yet. What follows is the specification for it,
not a description of shipped code; a connector author today writes these checks
by hand, and `examples/github-issues-connector/tests/` is the closest worked
reference. Sequenced in §10.

The kit would ship a reusable suite any connector repo runs in its CI:

- **Stability test:** normalize the same fixtures twice → identical hashes (law 1).
- **Volatility test:** author provides two raw exports of the *same unchanged
  content taken at different times* (the "export twice a week apart" spike from
  the main-doc review, turned into a permanent fixture) → identical canonical
  docs (law 2).
- **Tombstone test:** deletions surface as explicit tombstones; partial fetches
  never produce `removed` entries.
- **Purity test:** `normalize` runs with network access blocked and a frozen clock.
- **Anchor test:** every doc carries a resolvable `ResourceAnchor`.
- **Agent-facing artifact test (§4.4):** given fixture canonical docs, run (or
  fixture) synthesis and assert all four emit-side laws hold — every claimed facet
  is in frontmatter (1), every link resolves (2), every concept carries a
  resolvable anchor (3), every concept carries a freshness stamp (4).

A connector passing the kit could claim **"kbforge conformant"** — that badge,
not the core code, is what would make the connector ecosystem trustworthy, and it
is the operational meaning of the "standard" we are materializing. Until the kit
ships, the badge does not exist and conformance is a claim an author makes for
themselves.

## 10. Build sequence

**Done.** Core is on PyPI (0.5.0), with the fixed pipeline, both credential-free
reference connectors, three publishers, the stub and LLM synthesizers, and the
§4.4 gate. Hookspecs are frozen in practice — `ConnectorSpec` and `PublisherSpec`
have not changed shape since 0.1.0 — and `examples/github-issues-connector/` is a
complete worked credentialed connector proving the plugin seam end to end.

**Next, in order:**

1. **A credentialed system-of-record connector**, as a separate package. It is
   the first thing that will exercise tombstones and therefore deletion
   propagation against a real source, since neither built-in connector emits one.
2. **The contract-test kit** (§9) plus a `cookiecutter-kbforge-connector`
   template. That is the moment this becomes a standard others can implement
   rather than a library we happen to own — and it wants a real third-party
   connector (1) to have shaken out the interface first.
3. **`PipelineHooks`** (§5.3), if a deployment actually needs the additive
   validator or observer seam. Nothing has asked for it yet.

Open items, unchanged: attachment/binary handling in the mirror (object store vs
git), a `SynthesizerSpec` decision (keep closed vs open cautiously), and async
fetch (likely `anyio` — cheap early, painful to retrofit).
