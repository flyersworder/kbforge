---
type: design-note
title: kbforge — Agent-Facing Artifact Contract
description: The emit-side contract that makes kbforge's "agent-first" claim checkable — four artifact laws, their enforcement in the validate stage, and the ConceptFrontmatter model — without kbforge owning the serving layer.
tags: [okf, agent-first, artifact-contract, validate, mcp, producer]
timestamp: 2026-07-18T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge — Agent-Facing Artifact Contract

**Status:** Draft v0.1 · **Amends:** [`../../architecture.md`](../../architecture.md)
(adds §4.4, a serving-contract statement, and extends §7 and §9)
**Companion:** [`../../context/knowledge-base-design.md`](../../context/knowledge-base-design.md)

## 1. Problem

The README calls kbforge **"agent-first."** The architecture is **producer-first**:
every load-bearing section (§3–§9) optimizes the life of the *knowledge-base
maintainer* — clean diffs, reviewable MRs, provenance, no-op detection. The agent's
convenience is assumed to fall out downstream.

Trace the path an agent actually takes to "connect to a knowledge base":

```
[ SoR: Confluence, CMDB ]
        │
        ▼
  kbforge   fetch → normalize → mirror → diff → scope → synthesize → validate → publish (MR)
        │
        ▼   (human reviews, merges)
[ OKF bundle on main: markdown + frontmatter ]
        │
        ▼
  [ MCP server ]  ◀──── the agent connects HERE
        │
        ▼
    [ agent ]
```

kbforge lives entirely on the top half. The agent lives on the bottom half and
connects through **MCP**, which the architecture declares out of scope ("Serving
protocol: MCP — exists"). So the agent never touches kbforge's architecture. It
touches two things kbforge does not currently own end-to-end: the **OKF artifact**
we emit, and the **MCP serving layer** we delegate away.

The gap is not in what §3–§9 specify — the producer architecture is sound. The gap
is in what is *assumed to happen after the MR merges*. Nobody is accountable for
whether the artifact is actually usable by an agent. This is the seam between OKF
(format at rest), MCP (transport), and kbforge (production): the end-to-end agent
retrieval experience falls in the cracks between all three.

**Decision (this note):** kbforge stays a producer — it does **not** build or own a
serving layer. Instead it makes the *artifact contract* explicitly agent-optimized
and checkable, so "agent-first" becomes an honest, enforced claim rather than a
downstream hope.

## 2. What the serving layer actually reads (the contract we depend on)

The companion doc §5.7 already specifies the MCP read server's surface. It is small,
and every affordance is powered by exactly one of three things in the artifact:

| MCP affordance | Powered by |
|---|---|
| `search_knowledge(query, filters?)` — hybrid semantic + frontmatter filter | **frontmatter** |
| `list_concepts(type?, tags?, owner?, updated_since?)` — faceted browse | **frontmatter** |
| `related_concepts(id)` — graph neighbours | **resolvable cross-links** + **resource anchors** |
| `whats_stale(area?)` — freshness | **frontmatter timestamps** |

So the artifact must expose three things and only three things for the agent to get
a good experience: **frontmatter fields, resolvable cross-links, and resource
anchors (with timestamps).** Naming this here turns "serving is out of scope" from a
hand-wave into a stated interface. kbforge guarantees the left column is
satisfiable; the MCP layer (someone else's code) does the serving.

This is deliberately a *documented assumption*, not something kbforge builds or
tests against a running server. kbforge's job ends at emitting an artifact from
which the right column can be built.

## 3. §4.4 — Agent-Facing Artifact Laws

Where the §4.3 canonicalization laws govern **ingest** (raw payload → canonical
document), these govern **emit** (canonical document → OKF concept file). They are
the emit-side mirror of §4.3 and carry the same status: load-bearing, mechanically
checkable, enforced at a fixed pipeline stage.

Each law is tied to the serving affordance it keeps alive. Break the law, and the
corresponding agent capability silently goes dark — the failure mode is not a crash
but an agent that quietly can't find, traverse, trace, or date what it needs.

1. **Facet survival.** Every `structured` field that synthesis relied on to make a
   claim MUST appear as a **frontmatter key** on the emitted concept — never only in
   prose. *Dies without it:* `list_concepts` / `search_knowledge` filters (an agent
   asking "who owns app X" must get a structured answer, not have to grep prose it
   cannot reliably parse).

2. **Link resolvability.** Every cross-link a concept emits MUST **resolve** to an
   existing concept file in the bundle, or be dropped — never left dangling. The
   relationship's *meaning* stays in the surrounding prose (OKF keeps links untyped;
   we do not invent an edge vocabulary). *Dies without it:* `related_concepts`
   returns a broken or empty graph, killing multi-hop reasoning ("app X depends on
   service Y — who owns Y?"). The agent recovers edge meaning by reading the prose,
   which LLM agents do well; it cannot recover a link that does not resolve.

3. **Anchor presence.** Every concept MUST carry ≥1 `resource` anchor in
   frontmatter, tracing to a canonical document (which traces to a SoR). *Dies
   without it:* provenance queries and anchor-based `related_concepts`; the agent
   cannot say "this claim traces to Confluence page 123." The §4.3 grounding chain
   (claim → canonical doc → anchor) is only *useful* to the agent if it survives to
   the emitted frontmatter — this law is what carries it across the emit boundary.

4. **Freshness legibility.** Every concept's frontmatter MUST carry a
   machine-readable freshness stamp (the source's `retrieved_at` / last-verified
   time from its anchor). *Dies without it:* `whats_stale`, and the agent's ability
   to caveat a stale answer.

**These four are the complete set for v0.1.** They are exactly the artifact
properties the §2 serving affordances read — no more (we do not legislate prose
quality, which is synthesis's job and not mechanically checkable) and no fewer
(dropping any one dims a specific agent capability).

### 3.1 Why Law 4 also resolves the freshness-vs-human-gate tension

The `never-auto-merge` rule (a trust guarantee) is in apparent tension with an
agent's need for current data: an owner changes in the CMDB, and the agent keeps
answering with the old owner until a human reviews the MR — possibly hours or days.

Law 4 dissolves this without weakening the gate. If staleness is **legible in the
artifact**, the agent (via `whats_stale`, or by reading the freshness stamp) can
caveat: *"owner per CMDB as of 3 days ago; an update may be pending review."* Slow
propagation stops being a silent correctness bug and becomes visible metadata the
agent can act on. We keep the human gate **and** keep the agent safe. No change to
the never-auto-merge rule is proposed.

## 4. `ConceptFrontmatter` — making the laws checkable

`ProposedChange.files` is currently `dict[str, str]` (bundle path → full new
content). Free-form text is not mechanically checkable against Laws 1/3/4. We
introduce a small Pydantic model for the *frontmatter* of an emitted concept so the
validators (§5) can assert against structure rather than parse arbitrary markdown.

```python
# kbforge/models.py (addition)
from datetime import datetime
from pydantic import BaseModel, Field


class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF concept file. Serialized to YAML
    frontmatter at write time; validated against the §4.4 laws before publish.

    OKF requires only a non-empty `type`; it permits arbitrary additional keys and
    requires consumers to ignore unknown ones. `facets` is where Law 1 lands, and it
    maps onto exactly the fields the MCP serving layer filters on (companion §5.7)."""

    type: str                                  # OKF's one required field (non-empty)
    facets: dict = Field(default_factory=dict) # Law 1: structured fields used in a
                                               #   claim, emitted as filterable keys
                                               #   (owner, env, tags, ...)
    resources: list[ResourceAnchor]            # Law 3: ≥1, provenance to a SoR
    links: list[str] = Field(default_factory=list)   # Law 2: doc_ids / bundle paths
                                               #   that MUST resolve within the bundle
    freshness: datetime                        # Law 4: source retrieved_at / verified
```

Notes:
- `ProposedChange.files` stays `dict[str, str]` for the *rendered* bundle content
  (what the publisher writes). `ConceptFrontmatter` is the *validated projection* the
  synthesis stage must also hand over per concept, so the validators have structure
  to check. Carrier: a **parallel `dict[path, ConceptFrontmatter]`** on
  `ProposedChange` (shown as `ProposedChange.concepts` in architecture.md §3) is the
  working default — you should not re-parse markdown you just emitted. The plan
  confirms it; the model itself is fixed here.
- `type` deliberately does not constrain the taxonomy — the type *vocabulary* is the
  deployment's concern (companion §5.4), not the core's.
- `resources` reuses the existing `ResourceAnchor` (architecture §3) unchanged — the
  same anchor produced at ingest flows through to emit; no parallel provenance type.
- **Serialization onto OKF keys.** `ConceptFrontmatter` is the §4.4 *projection*, not
  the whole frontmatter: the remaining strict-OKF fields (`title`, `description`,
  `timestamp`) live in the rendered file, where the existing strict validator checks
  them. At write time `freshness` serializes to the OKF `timestamp` key (companion
  §6's "last synced from source") and each anchor in `resources` to a `resource`
  entry — so `whats_stale`, which reads `timestamp`, sees Law 4's stamp under the
  key the serving layer already expects.

## 5. Enforcement — core validators in the existing validate stage

The §4.4 laws are enforced as **core validators run in the §7 validate stage**
(`run_validators`), *not* as a responsibility of synthesis. Rationale: synthesis is
the LLM step. You cannot trust an LLM to emit a complete, correct frontmatter every
time — you *check* its output. This is the same posture as the §4.3 law-1
enforcement (`assert_stability`): the contract is upheld by a mechanical gate, not by
the good behavior of the component that could violate it.

Placement in the fixed pipeline (§7), unchanged in order:

```
... synthesize → validate → publish ...
                    │
                    ├─ existing: strict OKF checks (4 required fields)
                    ├─ NEW: agent-facing artifact validators (§4.4 laws 1–4)
                    └─ existing extension hook: kbforge_extra_validators (§5.3)
```

- These validators are **core**, not the `kbforge_extra_validators` extension hook.
  The extension hook is additive-only and third-party (gitleaks, PII, link check);
  the §4.4 laws are trust guarantees of the standard and must not be opt-in — same
  reasoning that keeps the no-op and never-auto-merge rules non-pluggable (§1).
- A concept that violates any law **fails the run** (`abort_with_report`), the same
  gate as the strict OKF checks. No MR is opened for a non-conformant artifact.
- Law 2 (link resolvability) is checkable purely within the proposed bundle: for each
  link, the target concept file must exist in `files` or already in the bundle on
  `main`. No network, no serving layer needed — consistent with kbforge never
  depending on a running MCP server.

## 6. §9 — conformance test kit addition

One new test joins the §9 contract-test kit:

- **Agent-facing artifact test.** Given fixture canonical documents, run synthesis
  (or a fixture synthesis output) and assert all four §4.4 laws hold on the emitted
  concepts: every claimed facet is present in frontmatter (1), every link resolves
  (2), every concept carries a resolvable anchor (3), every concept carries a
  freshness stamp (4).

This sits alongside the §9 stability / volatility / tombstone / purity / anchor
tests. Passing it is part of what "kbforge conformant" means — the badge now covers
both the ingest laws (§4.3) and the emit laws (§4.4).

## 7. Positioning correction

With the artifact contract in place, "agent-first" is honest and defensible:
kbforge does not serve agents, but it **guarantees, and mechanically enforces, that
the artifact it emits carries exactly what an agent's serving layer needs** —
filterable facets, a traversable link graph, provenance anchors, and legible
freshness. The agent-convenience claim follows from the *artifact contract*, not
from a runtime kbforge owns. The README's "agent-first knowledge bases" line can
stand, backed by §4.4 rather than by assumption.

## 8. What this changes (concrete amendments to `architecture.md`)

**Status: applied.** These amendments are live in `architecture.md` as of this
note's companion commit; the list below records what changed and why.

- **§3 (models):** add `ConceptFrontmatter`; note that `ProposedChange` carries a
  validated frontmatter projection alongside `files`.
- **New §4.4:** the four Agent-Facing Artifact Laws (this note §3), as the emit-side
  companion to §4.3.
- **New §2-style statement** (or a subsection of §4.4): the serving contract kbforge
  depends on (this note §2) — documented assumption, not owned code.
- **§7 (pipeline):** `run_validators` explicitly includes the §4.4 core validators,
  before the `kbforge_extra_validators` extension hook.
- **§9 (test kit):** add the agent-facing artifact test.
- **README:** keep "agent-first"; point the claim at §4.4.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules. The fix closes the agent gap by *tightening an existing
contract*, which is the right-sized response.

## 9. Relationship to the agent-contracts family

kbforge is one of three sibling projects, each a *contract for agents* at a
different seam. The full table and the formal (seven-tuple) mapping now live in
`architecture.md` §8; the essentials:

- **`ai-agent-contracts`** — the formal spine (resource / temporal / lifecycle
  contracts). The §4.4 laws are emit-side **Φ** postconditions on the
  `synthesize → validate` composition; `run_validators` is their **Ψ** verifier.
  This extends the §8 mapping, which previously covered only connectors (ingest),
  to the emit side.
- **`agentic-data-contracts`** — the *consumption* half of "what the agent knows,"
  for **structured** data (SQL / metrics), enforced at query time. kbforge is the
  *production* half, for **unstructured** knowledge. The two converged
  independently on **freshness legibility** (this note's Law 4 / `whats_stale` ↔
  their `find_stale` / `last_reviewed` / `stale`) — evidence the primitive is real.

**Scope decision (this note):** the connection is documented **inside kbforge
only**; the sibling repos are not edited. The concrete "kbforge feeds
`lookup_domain`" bridge is recorded as a *future* item (architecture.md §8), not
built in v0.1.

## 10. Open items (deferred, not blocking)

- **Typed relations.** OKF keeps links untyped; we accept that for v0.1 (Law 2). If a
  real multi-hop agent use case demands typed edges, revisit adding a private
  `depends_on`/`owned_by` frontmatter vocabulary — a governed taxonomy like the
  companion §5.4 type vocabulary. Deferred deliberately.
- **Carrier for the validated frontmatter** — resolved to the parallel
  `dict[path, ConceptFrontmatter]` (`ProposedChange.concepts`, §4); the plan only
  confirms wiring, not the choice.
- **Facet ⇄ serving-filter alignment.** Law 1 says "structured fields used in a
  claim" become facets; the exact required facet keys (owner, env, ...) are a
  deployment/vocabulary concern, not core. The core checks *presence of the fields
  synthesis used*, not a fixed key list.
