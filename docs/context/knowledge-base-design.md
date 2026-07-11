---
type: design-note
title: Agent-Facing Application Knowledge Base — Design & Literature Reference
description: Architecture, component specs, and prior-art review for an OKF-based internal knowledge base for application managers, served to humans and agents via MCP.
tags: [okf, mcp, knowledge-base, application-management, rag, agent-governance]
timestamp: 2026-07-08T00:00:00Z
status: draft
okf_version: "0.1"
---

# Agent-Facing Application Knowledge Base — Design & Literature Reference

**Status:** Draft / working doc · **Last updated:** 2026-07-08

This document captures the design we converged on for an internal knowledge base
(KB) about our applications, curated for and partly maintained by LLM agents, and
served to both application managers and our agent fleet. It also records the
literature scan done to check we hadn't missed prior art or known failure modes.

This file is intentionally written as an OKF-conformant concept document (see the
frontmatter above) to demonstrate the format we propose to adopt.

---

## 1. Purpose & scope

Give application managers — and the agents that assist them — a single, trustworthy,
low-friction place to answer questions like *"who owns app X, what does it integrate
with, what's the incident runbook, and is any of this stale?"* without doing manual
archaeology across ServiceNow, Confluence, the CMDB, repos, and monitoring.

In scope: format, storage, an authoring/maintenance agent, a serving layer, and the
update loop. Out of scope (for now): replacing the systems of record themselves.

## 2. Problem statement

The knowledge app managers need is real but fragmented across mutually incompatible
surfaces (ITSM, wikis, CMDB, dashboards, repos, and senior colleagues' heads). Google
calls this the *context-assembly problem*: every agent and every person re-solves the
same "gather the relevant context first" step from scratch. The failure is not lack of
data; it's lack of a curated, canonical, freshness-aware layer over the sprawl.

## 3. Core design decisions (the layered mental model)

We are **borrowing patterns, not adopting any single tool wholesale.** Three distinct
layers, each mapped to a source of prior art:

| Layer | Choice | Borrowed from |
|---|---|---|
| **Format** | Open Knowledge Format (OKF) v0.1 | Google Cloud spec |
| **Producer** (authoring + maintenance agent) | Custom agent, patterned on OpenWiki + Karpathy LLM-wiki disciplines | LangChain OpenWiki; Karpathy gist |
| **Storage** | GitLab repo (bundle in git) | OKF "just files"; OpenWiki CI model |
| **Consumer** (serving) | MCP read server over an internal retrieval stack (semantic search + an LLM gateway such as LiteLLM), plus static visualizer for humans | Context7, docs-mcp-server, Hjarni |

The key realisation: **OKF is the format, OpenWiki is one producer, MCP is the serving
protocol.** They compose; they are not alternatives to one another. OKF's spec explicitly
notes it complements MCP — an MCP server can expose an OKF bundle as a knowledge source.

## 4. End-to-end architecture

```
Systems of record            Producer (CI job)                 Storage (GitLab)         Consumer
─────────────────            ─────────────────                 ────────────────         ────────
ServiceNow / CMDB  ──ingest──▶  sources/ (raw mirror)  ──diff──▶  OKF bundle  ──merge──▶  index build (CI)
Confluence / repos    (MCP/      │                                 (canonical    │            │
monitoring / vendor    API)      ▼ synthesize (grounded)            concepts)    ▼            ▼
docs [COTS only])                writes concept .md + opens MR ─────────────────▶ MR review   MCP read server
                                                                    (human gate)             ├─ search_knowledge
                                                                                             ├─ list_concepts
                                                                                             ├─ get_concept / resource
                                                                                             ├─ related_concepts
                                                                                             ├─ whats_stale
                                                                                             └─ trigger_refresh (dispatch only)
                                                                                                    │
                                                                              app managers ◀── static visualizer / search UI
                                                                              agent fleet  ◀── MCP
```

Two independent lifecycles share one repo: the **producer** (heavy, scheduled/evented,
write-capable) and the **server** (light, always-on, read-only). See ADR-1.

## 5. Component specifications

### 5.1 Systems of record (SoR)
Before anything else, declare per information type what the authoritative source is
(ownership → CMDB/HR; incidents → ITSM; runbooks → Confluence/repo; architecture → …).
Grounding is meaningless until this is settled. The OKF `resource` field then anchors
each concept back to its SoR.

### 5.2 Ingestion layer — `sources/`
Mirror the SoR into the repo as raw exports (read via MCP connectors or SoR APIs). This
is the piece Karpathy's pattern calls the immutable `raw/` layer, and it is what makes
the rest work: an OpenWiki-style producer needs *local ground truth to read and diff
against*. Diffing the KB against itself is circular; diffing the **source mirror** is the
real change signal. Connectors are the actual hard engineering here.

### 5.3 Producer agent (authoring + maintenance)
A custom agent (candidate stack: Pydantic AI v2, model calls via our LiteLLM gateway).
Disciplines borrowed from OpenWiki and Karpathy, adapted to emit OKF:

- **Grounding contract:** never assert what isn't traceable to an inspected source;
  prefer current source evidence over existing docs; flag conflicts rather than pick
  silently. Every concept carries a `resource` anchor + citations.
- **Change-scoped, surgical updates:** snapshot the `sources/` mirror; diff since the
  last successful run (OpenWiki's `gitHead`/last-run trick); regenerate only concepts
  whose sources moved. Soft diff budget: few sources changed → touch few concepts, no
  reformatting-only edits.
- **No-op detection:** content-hash the bundle; if nothing source-grounded changed, open
  no MR. The whole review gate depends on MRs being rare and meaningful.
- **Anti-sprawl / canonicalisation:** one canonical home per fact; merge stubs; no thin
  one-concept directories. (OKF makes concepts atomic, which reinforces this.)
- **Gap surfacing:** emit "owner: unknown", "no DR runbook found" as explicit flags for
  humans — a feature, not scaffolding.
- **Minimal write capability:** the agent can only *write markdown* and *open an MR*. It
  never merges and never commits to `main`. (Security: see §7.)

### 5.4 OKF format & proposed type vocabulary
OKF conformance requires only a non-empty `type` per concept; consumers must be
permissive (tolerate unknown types/keys, broken links, missing optional fields). The
reference producer-side check is stricter (`type`, `title`, `description`, `timestamp`) —
we should adopt the stricter check on our producer for quality.

OKF does **not** define a type taxonomy — we must. Draft vocabulary for the application
domain (extend as needed; the spec allows arbitrary additional frontmatter keys):

| `type` | Represents | Key extra fields (beyond standard) |
|---|---|---|
| `application` | A managed application/service | `owner`, `criticality`, `lifecycle` (prod/deprecated), `cmdb_id` |
| `runbook` | Operational procedure for an app | `owner`, `applies_to` (link), `last_verified` |
| `integration` | A dependency/interface between systems | `direction`, `endpoints` (link), `protocol` |
| `contact` | Ownership / escalation path | `role`, `team` |
| `decision` | An architecture/ops decision record | `status`, `supersedes` (link) |

Bundle shape (illustrative):
```
applications/
  index.md
  <app>/
    index.md          # progressive-disclosure listing
    overview.md       # type: application
    runbook.md        # type: runbook
    integrations.md   # type: integration
    ownership.md      # type: contact
    log.md            # date-grouped change history (reserved name)
```
Reserved filenames: `index.md` (directory listing, no frontmatter except bundle-root may
carry `okf_version`) and `log.md` (change history). Cross-links are plain markdown; the
relationship's meaning lives in the surrounding prose, not a typed edge.

### 5.5 GitLab storage & CI
The bundle lives in a GitLab repo. Git gives version history, MRs as the human-review
gate, RBAC via repo permissions, and CI for both the producer job and the index rebuild.
OKF's `log.md` layers an application-level audit trail over git's own history — useful in
a regulated / EU-data-residency compliance context (OKF is a pure file format, so data
stays in our git, our infra, our jurisdiction).

### 5.6 Index build
On merge to `main`, CI rebuilds a served index: embeddings (semantic) **plus** a
frontmatter index (structured). Stateless, versioned, rebuildable — fits MCP-on-OpenShift.

### 5.7 MCP read server (consumer)
Stateless read layer over the built index. **Resources vs tools:** expose concept docs as
MCP *resources* (list + read map 1:1 onto the OKF file tree); reserve *tools* for what
resources can't do. Deliberately small surface (Context7 ships only two tools and caps
calls per question — we follow that discipline):

- `search_knowledge(query, filters?)` — hybrid semantic + frontmatter filter; returns
  concept refs + snippets (not full bodies).
- `list_concepts(type?, tags?, owner?, updated_since?)` — faceted browse; the real
  "discover" affordance, powered by frontmatter.
- `get_concept(id)` — full doc (or expose as a resource read).
- `related_concepts(id)` — graph neighbours via links and `resource` anchors.
- `whats_stale(area?)` — timestamp-driven; concepts with no SoR or a long-untouched SoR.
- `trigger_refresh(area?)` — **dispatch only**; enqueues the producer CI pipeline. Holds
  no source credentials and no write access itself (see ADR-1).

Design invariants: every result carries provenance (`resource` + `timestamp`) so agents
can cite and humans can verify; progressive disclosure (search → refs+frontmatter+snippet,
then fetch one concept) keeps context lean; authz enforced at the MCP boundary per user
identity, not just at the repo.

Humans who want to browse directly use the static HTML visualizer (OKF ships one, no
backend) or a search UI over the same bundle — one bundle, two read surfaces.

## 6. The update loop ("autoscan")

Runs as a producer CI job, **not** inside the server. Baseline: weekly cron. Upgrade
path: event-driven (deployment webhook / ServiceNow change ticket triggers a scoped run),
with cron as the catch-all backstop.

Properties that make it trustworthy rather than annoying:
- **Diff against sources, not the KB.** (See §5.2.)
- **No-op → no MR.**
- **Scoped MRs routed by owner.** One MR per app/logical change; use the `owner`
  frontmatter to assign the reviewer to the concept's owner.
- **Never auto-merge.** Human merge is the backstop against bad synthesis *and* injected
  content (see §7).
- **Freshness honesty.** The loop updates `timestamp` = "last synced from source", which
  is not "verified against reality"; `whats_stale` surfaces the difference.

**Web scanning is a restricted mode, not the default** — see ADR-3.

## 7. Security model (this is load-bearing)

The producer combines all three legs of Simon Willison's **lethal trifecta**: access to
private data (SoR), exposure to untrusted content (vendor docs / tickets / web), and an
external/consequential action (opening MRs). The emerging **"Rule of Two"** guidance:
never let one execution path hold all three without a human on the trigger.

Mitigations we adopt:
- **Separate producer from server** so the always-on component never holds the trifecta
  (ADR-1).
- **Human-in-the-loop at merge.** The MR gate *is* the "human on the trigger". The agent
  can propose; only a human merges.
- **Outbound allowlist + no free egress** from the producer; treat exposure to untrusted
  content as a taint that blocks any consequential action beyond "open MR for review".
- **Content is an injection vector to downstream consumers.** EchoLeak-class attacks show
  hidden instructions can be indexed by a RAG system and executed on a routine query
  (exfiltration via, e.g., an image URL). Therefore: (a) the MR review gate also guards
  what enters the served index; (b) consumers must treat retrieved KB content as *data,
  not instructions*; (c) provenance (`resource`) lets us trace any poisoned concept.
- **Secrets exclusion** (from OpenWiki): never read or document secret values, `.env`,
  keys, tokens. App docs attract "where the credentials live" — document only that such
  config exists and where.
- **Least privilege / identity-scoped serving.** Filter results by the caller's identity;
  a runbook may point at sensitive infra.

## 8. Architecture Decision Records (ADRs)

**ADR-1 — The autoscan loop is a separate CI producer job, not code inside the MCP
server.** The server stays a stateless read layer. Rationale: (a) independent lifecycles
(heavy batch vs always-on reads); (b) scaling/availability; (c) **security** — co-locating
would assemble the lethal trifecta inside the most-connected component. *Nuance:* a thin
`trigger_refresh` tool on the server that only *dispatches* the pipeline is acceptable and
gives the "built-in" UX without putting logic or credentials in the server.

**ADR-2 — Read-only-plus-propose; never auto-merge.** Ship the server read-only first.
Write-back is limited to `propose_update` → opens an MR, never a direct commit. This is
the "agents help maintain their own KB" idea kept safe by the review boundary.

**ADR-3 — Web scanning is a narrow, opt-in mode restricted to COTS products.** For our
own apps the web is irrelevant (all signal is internal git + SoR). External scanning earns
its place only for vendor deprecation notices / CVEs / release notes of COTS products we
run — and it is the highest-risk injection surface, so it is gated and allowlisted.

**ADR-4 — Minimal tool surface.** Prefer resources over tools; cap the toolset (~5–6) and
consider a per-question call budget. Rationale: models select worse among many tools and
each schema costs context. (OpenWiki's anti-sprawl rule applied to the tool surface;
Context7 precedent.)

## 9. Open decisions (blocking a concrete pilot)

1. **COTS vs in-house apps.** If the applications are our own service repos, an
   OpenWiki-style producer fits almost directly (run per repo, aggregate into the bundle)
   and web scanning is out of scope. If they're COTS/business systems (e.g. SAP modules),
   there is no source tree we own → ingestion-from-SoR approach and ADR-3 web mode become
   central. *This single answer changes how much of OpenWiki is reusable and whether web
   scan exists at all.*
2. **Systems of record.** Which SoR is authoritative for each info type, and where do
   runbooks actually live today?
3. **Retrieval stack.** Reuse an existing in-house RAG service for the semantic half, or
   adopt/self-host an existing docs-MCP server (e.g. docs-mcp-server) as the serving base?
4. **Type vocabulary sign-off.** Confirm/extend the §5.4 draft.

## 10. Literature review & references

Checked to confirm we hadn't missed prior art or known failure modes. Findings: the
pattern is well-established (origin, format, producer, and consumer all have prior art);
the main under-weighted area was **security**; and academic work confirms existing
implementations are personal-grade, i.e. the industrial version is genuinely unbuilt.

**Origin pattern — Karpathy "LLM wiki"**
- Andrej Karpathy, *LLM Wiki* gist (early 2026; widely shared April 2026). Pattern (not
  product): an agent compiles raw sources once into a persistent, interlinked markdown
  wiki; queries hit the wiki, not raw docs (synthesis at write time vs RAG's per-query
  re-derivation). Three layers: `raw/` (immutable sources), `wiki/` (generated), schema
  file (`CLAUDE.md`/`AGENTS.md`). Three ops: ingest, query, lint. Lineage: Vannevar
  Bush's 1945 Memex — the unsolved problem was *who maintains it*; the LLM does.
  https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f
- rohitg00, *LLM Wiki v2* — adds confidence scoring, supersession, and triple retrieval
  (BM25 + vector + graph). Useful upgrade path for §5.6/§5.7.
  https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2

**Format — Open Knowledge Format (OKF)**
- Google Cloud, *How the Open Knowledge Format can improve data sharing* (S. McVeety, A.
  Hormati; 2026-06-12). The announcement + rationale (context-assembly problem).
  https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing
- OKF `SPEC.md` v0.1 (GoogleCloudPlatform/knowledge-catalog, Apache 2.0; ~1 page). Only
  `type` required; permissive consumption; reserved `index.md`/`log.md`; citations may be
  URLs, bundle-relative, or a `references/` mirror; does not subsume Avro/Protobuf/OpenAPI.
  https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
- *OKF: An Annotated Guide* (community) — practical read of the spec, incl. the citations
  point. https://okf.md/spec/
- Marc Bara, *A Standard, or Just a Folder?* — critical read: v0.1 delivers **structural,
  not semantic** interoperability; `type` values are unregistered so bundles can conform
  yet share no vocabulary; reference parser requires 4 fields though spec says 1. Informs
  §5.4 (we must own the vocabulary). https://medium.com/@marc.bara.iniesta/googles-new-format-for-agent-context-a-standard-or-just-a-folder-82fb21d92041
- W4G1/okf — third-party Rust implementation (validator/CLI: `okf validate` for CI, graph
  export). Evidence the format is trivially reimplementable off Google's toolchain.
  https://github.com/W4G1/okf

**Producer — OpenWiki**
- langchain-ai/openwiki — agent that writes/maintains repo docs; source of the grounding
  contract, git-diff scoping, content-snapshot no-op detection, surgical-update budget,
  anti-sprawl rules, and OpenAI-compatible base-URL support (points at a LiteLLM gateway).
  https://github.com/langchain-ai/openwiki

**Consumer — MCP knowledge/doc servers (prior art we can borrow, not reinvent)**
- Context7 (Upstash) — most-adopted docs MCP; exposes only 2 tools (`resolve-library-id`,
  `query-docs`) and instructs ≤3 calls/question. Validates minimal tool surface + call
  budget. https://github.com/upstash/context7
- docs-mcp-server (arabold) — open-source, **self-hostable** (Docker), optional embeddings
  for semantic search, probes for `llms.txt`. Strong candidate serving base / reference.
  https://github.com/arabold/docs-mcp-server
- Hjarni — hosted LLM-wiki with a **built-in MCP server** (read+write from any MCP client).
  Direct precedent for "wiki + MCP". https://hjarni.com/blog/karpathys-llm-wiki-is-right

**Security — the under-weighted axis**
- Simon Willison, *The lethal trifecta* (2025-06-16): private data + untrusted content +
  external communication ⇒ prompt-injection exfiltration. Mitigation: ensure ≥1 leg is
  absent per execution path; taint tracking + policy gating; outbound allowlist.
  https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/
- *Agents Rule of Two* (OWASP/community, 2025-10): ≤2 of {untrusted input, private data,
  external action} per session; need all three → human on the trigger. Basis for ADR-1/2.
- Beurer-Kellner et al., *Design Patterns for Securing LLM Agents against Prompt
  Injections* (arXiv:2506.08837, 2025). https://doi.org/10.48550/arXiv.2506.08837
- EchoLeak / Gemini-Enterprise RAG attacks — hidden instructions indexed by RAG, executed
  on a routine query, exfiltrated via image URL. Basis for "KB content is an injection
  vector to downstream consumers" (§7).
- OWASP Top 10 for LLM (2025) and for Agentic Applications (2026) — prompt injection now
  maps across most agentic categories; use as an audit checklist.

**Academic anchor**
- *Knowledge Compounding: An Empirical Economic Analysis of Self-Evolving Knowledge Wikis
  under the Agentic ROI Framework* (arXiv). Reviews current LLM-wiki implementations and
  finds them personal-grade prototypes lacking industrial capabilities (governance, quality
  control, multi-agent scoping) — i.e. the version we're designing is genuinely unbuilt.
  https://arxiv.org/abs/2604.11243

**Adjacent standards (context)**
- `llms.txt` — site-level convention for agent-readable content; complementary to OKF and
  probed by some doc-MCP servers.
- `AGENTS.md` / `CLAUDE.md` — the ad-hoc convention OKF generalises; still useful as the
  producer's schema/rules file (Karpathy's third layer).

## 11. Suggested pilot

Scope to **two or three representative applications**. Steps: (1) answer §9.1 (COTS vs
in-house) and §9.2 (SoR map); (2) stand up `sources/` ingestion for those apps; (3) run a
producer pass to emit OKF concepts, gated by MR; (4) rebuild index in CI; (5) thin MCP read
server with the §5.7 toolset, read-only; (6) point the static visualizer at the bundle for
the human side. Add `propose_update` (ADR-2) and `trigger_refresh` only once the corpus is
trusted. Resist building an elaborate tool suite over an empty KB — get real content first
and the server nearly writes itself.
