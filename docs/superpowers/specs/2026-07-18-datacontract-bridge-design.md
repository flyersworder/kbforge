---
type: design-note
title: kbforge ⇄ agentic-data-contracts — The Bridge
description: How kbforge (unstructured knowledge production) connects to agentic-data-contracts (structured-data governance) through the OKF bundle as a shared boundary artifact, without folding the two projects together.
tags: [kbforge, agentic-data-contracts, okf, bridge, semantic-layer, provenance, freshness]
timestamp: 2026-07-18T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge ⇄ agentic-data-contracts — The Bridge

**Status:** Draft v0.1 · **cross-project, future** (kbforge core unchanged;
`agentic-data-contracts` repo untouched by this note)
**Companions:** [`2026-07-18-agent-facing-artifact-contract-design.md`](2026-07-18-agent-facing-artifact-contract-design.md)
· [`../../architecture.md`](../../architecture.md) §8

## 1. Why bridge, not fold

The two projects govern *what an agent knows* on either side of a **substrate
boundary**:

| | `agentic-data-contracts` (ADC) | kbforge |
|---|---|---|
| Substrate | **structured** — SQL, metrics | **unstructured** — OKF concepts |
| Role | *consumption* — validates queries at query time | *production* — synthesizes & keeps knowledge fresh |
| Owns | executable metric SQL, query rules, domain catalog | provenance, freshness, grounded prose |

Folding them fails because it forces one representation to swallow the other:
either OKF markdown gets crammed into metric rows, or SQL-validated metrics get
flattened into prose. Both destroy what the losing side was for. Keep them
separate; connect them at the one place they already share meaning — the
**domain / metric identity**.

## 2. The boundary artifact is the OKF bundle

Neither project imports the other's internals. They meet at the artifact kbforge
already produces:

```
   SoR ──▶ kbforge ──▶ OKF bundle on `main` ──┬──▶ MCP serving  ──▶ agent (reads knowledge)
                       (concepts w/ frontmatter │
                        + anchors + freshness)   └──▶ OkfSource adapter ──▶ ADC contract ──▶ agent (queries data)
```

The bundle is the API. kbforge writes it (unchanged from the §4.4 contract); ADC
reads it through a new adapter. This keeps the earlier scope decision intact:
**kbforge core needs zero changes for the bridge** — the work lives downstream.

## 3. The consumer: an `OkfSource` adapter (ADC-side)

ADC already populates its contract from **source adapters** — `YamlSource`,
`DbtSource`, `CubeSource` (each fills `MetricDefinition.domains` from `meta.domains`,
and the domain catalog from a source). `OkfSource` is a sibling of those: it reads
a kbforge-produced OKF bundle and populates the domain catalog (and metric
*descriptions*) from concepts of an agreed type.

**This is mechanizable only because of the §4.4 artifact laws.** The adapter reads
exactly the three things those laws guarantee:

| §4.4 law | Guarantees | Fills, on the ADC side |
|---|---|---|
| 1 — facet survival | business fields are in **frontmatter**, not prose | `Domain.summary`, `owners`, metric description facets |
| 3 — anchor presence | every concept has a **`resource` anchor** | provenance for each definition (new to ADC) |
| 4 — freshness legibility | every concept has a **freshness stamp** | `Domain.last_reviewed` |
| 2 — link resolvability | cross-links **resolve** | related-concept context, safe to follow |

Had those fields stayed in prose, no adapter could read them deterministically. The
same contract that serves the MCP layer serves the bridge — one contract, two
consumers.

## 4. The division of ownership (the sharp line)

The bridge carries **business meaning + provenance + freshness**. It does **not**
carry executable logic:

- **kbforge grounds** *"what revenue means, who owns it, where that came from, how
  fresh it is."* → a `type: domain` / `type: metric-definition` OKF concept.
- **ADC owns** *"revenue = `SUM(amount) FILTER (WHERE status='completed')`"* — the
  executable SQL, tiers, query rules. kbforge never emits SQL.

They meet at the metric/domain **identity**: the concept names the metric; ADC
holds its query definition; the bridge attaches kbforge's grounded description +
anchor + freshness to that identity. Clean seam, no overlap of authority.

## 5. Three capabilities — the "more than `lookup_domain`"

1. **Grounded definitions.** ADC's `lookup_domain("revenue")` today returns
   hand-authored YAML with no provenance. Through the bridge it returns *"revenue
   is recognized at fulfillment — source: Finance Confluence p.123, retrieved 3d
   ago."* kbforge's `ResourceAnchor` rides across (law 3).

2. **One freshness signal.** kbforge's law-4 stamp *becomes* ADC's `last_reviewed`.
   `whats_stale` (kbforge) and `find_stale` (ADC) stop being two staleness notions
   over the same knowledge — one source of truth, produced from the SoR.

3. **Drift / conflict detection.** kbforge already emits `conflicts_flagged`
   ("CMDB says owner=A; Confluence says owner=B"). Extend the idea across the
   boundary: if the data contract *asserts* a business meaning but kbforge
   synthesizes a different one from the live SoR, that contradiction is
   detectable — a governance signal neither project has alone ("your contract says
   revenue is booked at order; the Finance wiki now says at fulfillment").

## 6. What the bridge needs that doesn't exist yet

- **A concept-type convention** for bridgeable concepts (e.g. `type: domain`,
  `type: metric-definition`) so `OkfSource` knows which concepts to map. This is a
  *vocabulary* concern (deployment-owned, main doc §5.4 / architecture §5.4), not a
  core change — the bridge just needs a documented convention both sides honor.
- **A frontmatter key mapping** (OKF facet → ADC field): `owner`→`owners`,
  freshness→`last_reviewed`, etc. Small, lives in the adapter.
- Nothing in kbforge core. The §4.4 laws already produce everything the adapter
  reads.

## 7. Relationship to `ai-agent-contracts`

An agent that both *queries structured data* (via ADC) and *reads knowledge* (via
kbforge's MCP output) is a single composed system. Both projects compile onto the
`ai-agent-contracts` seven-tuple (ADC via its existing `DataContract → Contract`
compiler; kbforge via the §8 mapping). So the bridge is not just two libraries
sharing a file — it's two contracted units whose composition is expressible in the
shared formal spine. The freshness primitive both converged on independently is a
candidate to lift *into* that spine rather than keep re-deriving per library.

## 8. Direction and scope

- **Primary direction:** kbforge → ADC (produce & refresh the grounded domain layer
  ADC serves). This is the valuable one and the focus here.
- **Reverse direction** (ADC's structured catalog as a *kbforge source*, so metric
  definitions become searchable OKF concepts in the KB) is plausible but secondary;
  recorded as an open item, not designed here.
- **Build scope:** the adapter is **ADC-side** work, done when there is a concrete
  consumer to build against. kbforge ships its side already the moment the §4.4
  laws are implemented. This note touches neither repo's code.

## 9. Open items

- Reverse direction (ADC catalog → kbforge source) — plausible, undesigned.
- The exact concept-type vocabulary for bridgeable concepts — settle with the
  deployment's type taxonomy (§5.4), not here.
- Whether drift detection (capability 3) lives in the `OkfSource` adapter, in a
  kbforge validator, or in a small standalone checker reading both sides.
- Lifting "freshness legibility" into `ai-agent-contracts` as a shared primitive.
