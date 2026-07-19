---
type: design-note
title: kbforge — Agentic Ingest, Refresh & KB Initialization
description: How the knowledge base is first created and kept current — an agentic retriever at the fetch seam bookending the deterministic mirror→diff spine, a staged refresh→discover roadmap, and a chunked-iterative bootstrap whose machine↔hand split is emergent from source quality.
tags: [okf, agentic-fetch, refresh, discover, bootstrap, mcp, connectors, producer]
timestamp: 2026-07-19T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge — Agentic Ingest, Refresh & KB Initialization

**Status:** Draft v0.1 · **Amends:** [`../../architecture.md`](../../architecture.md)
(sharpens §4.1 transport / MCP-source note and §4.2 incremental contract; builds on
§4.4 and §7)
**Related:** [`2026-07-18-agent-facing-artifact-contract-design.md`](2026-07-18-agent-facing-artifact-contract-design.md)
— the `resource` anchors its §4.4 laws guarantee are the *provenance* every refreshed
concept carries (and a lead surface for Refresh-plus); change detection itself runs on
the core-owned mirror, not the bundle.

## 1. Problem

The architecture specifies a fixed pipeline (§7) and a transport-agnostic `fetch`
(§4.1), but it leaves two lifecycle questions implicit:

1. **How does the KB stay current?** Connectors `fetch` on a schedule, but "what
   changed since last time" is a deterministic pull against a cursor. Is there a
   role for an *agent* — one that decides what is worth looking at, follows leads,
   and scouts external (web) sources, not only the configured system-of-record rows?
2. **How is the first KB created at all?** The mechanism exists (`cursor=None` means
   full backfill, §4.2) but the *experience* — what a founding import looks like,
   how it is reviewed, where its structure comes from — was never designed.

This note answers both. The through-line: an agent is genuinely more powerful for
the *judgment-heavy* parts of ingest, but its power must **bookend a deterministic
spine**, never replace it. The spine is what makes the agent trustable.

## 2. The load-bearing principle — agentic ends, deterministic middle

kbforge already puts an LLM in the pipeline: `synthesize` (§7). So this was never
"agent vs. no agent." The only question is *where* an agent's judgment is allowed to
act, and what stays deterministic between its acts.

**Decision:** keep the **canonical mirror → diff** spine exactly as specified. An
agent may act at the two *ends* — retrieval (`fetch`) and interpretation
(`synthesize`) — but the spine between them stays deterministic. It is not
bureaucracy; it is the only thing that provides three properties the trust model
depends on:

- **No-op economics.** The deterministic diff over canonical documents is what lets
  an MR open *only* on real change (`ChangeSet.is_noop`, §3/§7). This is what keeps a
  human willing to review. An agent that owned the whole loop would churn.
- **Provenance.** The grounding chain — claim → canonical doc → anchor → SoR — is
  checkable only because each link is a deterministic artifact, not an agent's
  recollection.
- **Replayability.** `normalize` is pure (no network, no clock, no randomness; §5.1,
  §9 purity test), so a failed run re-fetches and replays harmlessly (§4.2). An agent
  in the middle breaks at-least-once semantics.

Collapsing `fetch` + `synthesize` into one end-to-end agent throws all three away and
leaves an ungoverned wiki-writer. The power of kbforge is that the agents are
*bookends around a deterministic core*.

## 3. Fetch as an agentic retriever

Two different agentic judgments hide in "an agent does the ingest," and they belong
in different stages:

| Judgment | Question it answers | Stage | Output |
|---|---|---|---|
| **Retrieval** | *"what is worth looking at, and what is new?"* | `fetch` | source documents, **verbatim**, each with an anchor |
| **Interpretation** | *"what do these sources mean; where do they conflict?"* | `synthesize` | OKF concepts (reads only canonical docs) |

### 3.1 The retriever-not-extractor rule

`fetch` is *allowed* to be non-deterministic — it is I/O. But an agentic `fetch` MUST
be a **retriever, not an extractor**: it decides *which* source documents are
relevant and hands them back **verbatim**, each carrying a stable `ResourceAnchor`.
It never emits its own prose summary at this stage. Two invariants force this:

- **Provenance (§4.4 Law 3).** If the agent returns *its interpretation* of what it
  read, there is no anchor to trace — the reviewer cannot click through to the
  source. The agent must return the sources; interpreting them is `synthesize`'s job,
  and `synthesize` reads only canonical documents.
- **No-op economics.** Normalization can only strip volatility it can *predict*
  (export timestamps, view counters; §4.3). An agent's own phrasing is *unbounded*
  volatility no normalizer can canonicalize away — so if the agent's prose enters the
  canonical form, every run diffs and churns an MR. Confine the agent's
  non-determinism to *which documents* it surfaces; keep *document content* verbatim,
  and the spine still works.

### 3.2 MCP transport, and agentic MCP servers

§4.1 already blesses MCP as one transport under `fetch`. This note sharpens two
points:

- The MCP **server behind `fetch` may itself be agentic** — it can run an agent that
  searches and follows leads. From kbforge's side this is invisible: it is still a
  transport handing back records. kbforge neither knows nor cares.
- The **read-only constraint is absolute** regardless of how smart the server is. MCP
  tools can have side effects; an agentic fetch may call only read/resource
  operations, preserving the seven-tuple's **R = read-only** (architecture §8) and
  the Rule-of-Two credential/action split (§4.1).

### 3.3 Conflicts are flagged, never silently resolved

Conflict *detection* is interpretation, so it belongs to `synthesize`, not to an
agentic `fetch` — a retriever may *surface* both disagreeing sources, but adjudicating
them is not its job (§3.1). When `synthesize` finds sources that disagree — "CMDB says
owner=A; Confluence says owner=B" — it MUST **surface** the conflict, not quietly pick
a winner. This is already the design: `ChangeSummary.conflicts_flagged` (§3).
`synthesize` may *propose* a resolution *with its reasoning*, but the disagreement
lands in the MR for a human to ratify. Reconciliation that is **legible** is fine;
reconciliation that is **silent** hides a judgment call from the human gate and
violates the never-auto-merge posture.

## 4. Keeping the KB current — the Refresh model

Refresh reuses the existing pipeline on a schedule; it introduces **no new
change-detection mechanism**. The substrate is the **core-owned mirror** — the
`CanonicalDocument`s core stores, each with a `content_hash`, covering *every* fetched
source (§3; §7's `mirror_and_diff`). Connectors stay bundle-blind (§4.1): they never
read emitted concepts. So the mirror — not the bundle — is what "changed?" is asked
against, and because the mirror covers all fetched docs (not only those that became
concepts), there is no coverage gap.

The §4.4 emitted anchors play a different role here: **provenance, not change
detection.** Every refreshed concept still records where it came from, so each update
stays auditable and clickable — and those anchors and links become a *lead surface*
for Refresh-plus (§4.2). But Refresh-lite's diff runs on the mirror.

This splits into two stages of increasing agency:

### 4.1 Refresh-lite — the scheduled pipeline (no agent)

Refresh-lite *is* the existing pipeline on a schedule: connectors `fetch` under their
cursor (incremental — §4.2), `normalize`, and core's `mirror_and_diff` surfaces the
real changes against the mirror; `synthesize` handles only the deltas. **No LLM in the
retrieval loop, and no document-identity problem** — every fetched source already has a
stable `doc_id` in the mirror, so the dedup/churn risk that plagues open-web scouting
does not arise, and it cannot introduce the failure modes an agent can. Build and
test this backbone first, against seeded fixtures — but note its *first real
end-to-end run on live data is a Bootstrap* (§6), since a refresh over an empty mirror
has nothing to diff.

Cursor handling depends on the source: a connector with a native change-feed uses the
ordinary cursor-delta contract (§4.2); a feed-less source re-polls and expresses its
cursor as a `(doc_id, content_hash)` **manifest**, so the mirror diff still reduces a
full re-poll to only real change. Either way `FetchResult.complete=False` (§4.2)
handles a run that hits a rate or budget limit without implying deletions.

### 4.2 Refresh-plus — the agent as a bounded lead-follower

Refresh-lite, plus: the agent may follow a **bounded** number of leads off each known
source — a page that now links to a newly relevant page, content that moved or was
renamed, a sibling source that newly disagrees. This is where the agent first earns
its keep in the steady state. The bound (how many leads, how deep, what budget) is a
retriever-contract parameter, deferred to design (§9). Everything it surfaces is
still verbatim-with-anchor (§3.1) and still flows through the same deterministic
spine and the same human gate.

## 5. Growing the KB — Discover (deferred to v2)

Discover widens scope from "keep what we track current" to "find things we do not
have a concept for yet," across internal and external (web) sources. It is
**deferred**, because it is where the hard problems concentrate:

- **Document identity.** The web has no stable `native_id`. A URL is not stable
  identity of *content* (the same fact at three URLs; URLs rot; content mutates
  behind a URL). Without an identity answer, the diff cannot separate "genuinely new"
  from "same thing, different address" — the exact failure that churns MRs.
- **Trust boundary.** An agent wandering unknown sources needs a **source allowlist**
  (which domains / systems it may read) so provenance anchors point at sources a
  reviewer can trust.

Discover is mostly Refresh's machinery pointed at a wider net, so building Refresh
first is also the cheapest path to Discover. The identity and allowlist designs are
listed in §9.

## 6. Bringing the KB into being — Bootstrap

**Bootstrap is not Refresh.** Refresh on an empty KB is a no-op — there are no
anchors to walk — which *proves* initialization must be the deterministic
connector-backfill path, not the agent.

The mechanism is the ordinary pipeline with `cursor=None`: `fetch` pulls everything,
`normalize` canonicalizes, `mirror` stores, `diff` marks it all `added`, `scope` +
`synthesize` build the concepts, `validate` gates, `publish` opens the founding MR(s).

**Where the judgment lives.** At bootstrap `fetch` is at its *dumbest* ("pull all of
Confluence space X, all of CMDB table Y"). All the judgment — carving a raw pile into
concepts with types, grouping, dedup across sources — is in `synthesize`, run at
full-backfill scale. So bootstrap needs **no agentic fetch**; it needs the
`synthesize` step we already have. Agent value is highest at the *two ends* of the
lifecycle (bootstrap-synthesis and Discover) and lowest in the Refresh middle.

### 6.1 The founding import is chunked and iterative

A backfill can emit hundreds of concepts in one shot, which collides with the trust
model's "90-second MR review" economics — an un-reviewable MR gets rubber-stamped,
defeating the human gate on day one. So the founding import is produced in
**reviewable chunks** by some natural partition (by source, by app, by type):

- The human reviews a chunk. Good quality → batches grow, review lightens.
- Poor quality → reject, inject a golden exemplar or fix the taxonomy, and the *next*
  chunk synthesizes better.

This one decision closes both founding-import problems at once: **chunking is the
review posture**, and **between-chunk exemplar/taxonomy injection is where messy
sources get steered.**

### 6.2 The machine↔hand spectrum is emergent, governed by source quality

There is no discrete "seed mode" and no "trust knob." The founding KB sits on a
spectrum from fully machine-backfilled to fully hand-seeded, and its position is
governed by **source quality**, because:

> The machine can only **project** structure that already exists in the sources; it
> cannot **manufacture** structure that is not there.

Good sources already encode concept boundaries, ownership fields, and types — the
machine projects them, human seeding is light. Chaotic sources do not carry that
structure, so it must be supplied by the human (taxonomy, exemplars, conflict
adjudication) because there is nothing for the machine to project. The spectrum falls
out of things we already have:

- the **universal MR gate** = "trust the output as much as review warrants";
- the **chunked import** = the review-burden dial;
- **hand-seed inputs** (taxonomy + exemplars) — always available, just *more
  necessary* toward the chaotic end.

### 6.3 Chunking recurses; the partition function is deployment config

If a partition is still too large for a big domain, the same chunking mechanism
sub-divides it (subsystem, type, source-slice) against a target review size. Core
provides *chunked iteration*; the **partition function** — how a given domain is
sliced — is deployment-repo config, alongside the schedule and the type taxonomy.
Core never needs to know the org chart. (Whether core ships a default partition
strategy is a §9 open item.)

## 7. Build sequence

1. **The deterministic backbone** — `fetch → normalize → mirror → diff → scope →
   synthesize → validate → publish` for the incremental case, tested against seeded
   fixtures. This is Refresh-lite's machinery; no agent.
2. **Bootstrap** — `cursor=None` backfill through the chunked-iterative founding
   import. This is the *first real end-to-end run on live data* (a refresh over an
   empty mirror has nothing to diff). Same backbone at full scale; judgment in
   `synthesize`.
3. **Refresh-plus** — the agent as a bounded lead-follower on top of the proven,
   now-populated backbone.
4. **Discover** — widen to new topics / web; solve document-identity and the source
   allowlist.

(Steps 1–2 are the same backbone: step 1 builds and unit-tests it on fixtures, step 2
is its first live exercise. The agent enters only at step 3.)

## 8. Amendments to `architecture.md`

**Status: applied** in this note's companion commit.

- **§4.1 (transport / MCP-source connector base):** state explicitly that an agentic
  `fetch` (including an agentic MCP server behind it) is a permitted transport, and
  add the **retriever-not-extractor** rule as a fetch-side constraint, with the
  read-only constraint reaffirmed for the agentic case.
- **§4.2 (incremental contract):** note that `cursor=None` full backfill is the
  **bootstrap** path; that Refresh re-runs the scheduled pipeline and diffs against the
  core-owned mirror, so it **cannot** bootstrap (empty mirror → nothing to diff); and
  that a feed-less refresh connector's cursor is a `(doc_id, content_hash)` manifest.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules. Agentic ingest is expressed *within the existing seams*: an
agent is a `fetch` transport, and bootstrap is a `fetch` mode.

## 9. Open items (deferred, not blocking)

- **Retriever contract mechanics.** The manifest-cursor format for refresh
  connectors; the Refresh-plus lead-following bounds (count, depth, budget); how a
  refresh connector expresses "re-check these known anchors" through `ConnectorSpec`.
- **Discover — document identity.** An identity/dedup answer for web sources with no
  stable `native_id` (content-hash identity, canonical-URL resolution, near-duplicate
  collapse). Blocks Discover; does not block Refresh.
- **Discover — source allowlist.** Where the allowlist of readable domains/systems
  lives (deployment config, likely) and how it is enforced at the retriever.
- **Partition function for chunking.** Whether core ships a default partition strategy
  (by source / type) with the deployment overriding it, or leaves partitioning
  entirely to the deployment. Interacts with the §6.1 review posture.
- **Bootstrap review posture as a first-class flow.** Chunked-iterative import may
  warrant explicit tooling (chunk boundaries, per-chunk accept/reject/refine,
  exemplar injection) rather than being expressed only as repeated pipeline runs.
- **Conflict-resolution proposal mechanics.** §3.3 settles that `synthesize` *may*
  attach a proposed resolution for human ratification; what is open is the *how* —
  `ChangeSummary.conflicts_flagged` is `list[str]` today (§3), so carrying a structured
  proposal + reasoning needs a model change, and its MR rendering is undesigned. The
  permission is decided; only the mechanism (and the guard against silent
  reconciliation, §3.3) is open.
