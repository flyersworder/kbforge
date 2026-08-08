---
type: design-note
title: OKF v0.2 — the families kbforge does not emit yet
description: Why verified, status/deprecated, stale_after, and footnote attribution are decisions rather than implementations.
tags: [okf, provenance, trust, lifecycle, agent-governance]
generated: { by: human:flyersworder, at: 2026-08-08T00:00:00Z }
status: draft
okf_version: "0.2"
---

# OKF v0.2 — the families kbforge does not emit yet

**Status:** Draft · **Amends:** [`../architecture.md`](../architecture.md) §4.4

The 0.5.0 conformance pass moved kbforge's two existing emit-side laws onto OKF
v0.2 field names: law 3 now writes `sources` (§5.1), law 4 writes
`generated: {by, at}` (§5.2). That part was mechanical — the laws already meant
what the spec now standardizes.

v0.2 also adds three families kbforge has no equivalent for, plus one body
convention. None of them is a rename, and none is blocked on effort. Each is
blocked on a decision that touches something kbforge deliberately does *not*
own. This note records the decisions so they are made once, in the open, rather
than re-derived each time someone reads the spec.

## 1. `verified` and the human gate (§5.2, §5.3)

**The fit is exact, which is why it is tempting.** OKF derives a trust tier from
`verified`: no key ⇒ *unverified*; non-`human:` actors ⇒ *machine-confirmed*; a
`human:<id>` actor ⇒ *human-reviewed* (§5.3). kbforge never auto-merges — no
publisher has a merge method — so every concept that reaches `main` passed a
human review. Structurally, kbforge is the only producer in this space that can
claim the top tier by construction rather than by convention.

**Why it is not implemented.** kbforge cannot stamp `verified` at publish time,
because at publish time the review has not happened. The stamp belongs to the
merge event, and the merge event is precisely what kbforge refuses to own
(architecture §5.2). A producer that wrote `verified: {by: human:...}` into a
concept it was still *proposing* would be asserting a review that had not
occurred — and no §4.4 law could catch it, because the laws check artifact
structure, not the truth of a claim about the world. That is the worst class of
bug this project can ship: a trust signal that is well-formed and false.

**Options, for whoever takes this up.**

- A `kbforge stamp-verified` subcommand run by the deployment's CI on merge,
  reading the merging actor from the forge event. Keeps the stamp truthful and
  keeps kbforge out of the merge, but adds a second write to `main` and a
  CI-side integration kbforge otherwise has none of.
- Leave it entirely to the deployment. kbforge emits no `verified`, consumers
  read every kbforge concept as *unverified*, and a deployment that wants the
  tier writes it itself. Honest, zero new surface, and understates a real
  guarantee.

The second is the current behaviour by default rather than by decision. Making
it a decision is the point of this section.

## 2. `status: deprecated` versus hard delete (§5.4)

kbforge deletes on tombstone: `ProposedChange.files_removed`, assigned by the
pipeline. OKF v0.2 offers `deprecated` — "kept for links and history; no longer
current".

**The dangling-link argument does not apply here** — `pipeline.run` already
loads the whole mirror and pulls in every document whose `relations` intersect
the removed ids, precisely so referrers get re-rendered without their links. So
the case for `deprecated` is *not* "deletion leaves dangling links"; that was
fixed. The residual exposure is narrow: a bundle path present on `main` but
absent from the mirror, which is a mirror-reset situation, not a deletion one.

**The real argument is history.** `deprecated` keeps the file, so the link
resolves, the reader learns the concept is no longer current, and anyone
auditing what the KB used to assert can still find it. Deletion erases the claim
and the fact that it was ever made — and since the mirror advances on every
publish, kbforge itself cannot reconstruct it either.

Against that: the bundle grows without bound, and "deleted at the source" and
"deprecated in the KB" are not quite the same claim — a source deletion may mean
the thing never should have been published, not that it is superseded. A
producer cannot tell those apart from a tombstone alone.

This changes deletion semantics, which are load-bearing (architecture §4.2), so
it needs its own decision rather than riding along with a conformance pass.

## 3. `stale_after` (§5.5)

An absolute date; a concept is stale when `today >= stale_after`. This would
promote law 4 from "here is when we synced, you decide" to an actual freshness
policy, and give `whats_stale` a direct answer instead of an inference.

**What is missing is a config surface, not code.** The date has to come from
somewhere, and the only honest source is per-connector policy — "runbooks go
stale after 90 days", "CMDB ownership after 30" — which is deployment knowledge,
not connector knowledge and not core knowledge. Introducing it means deciding
where that policy lives without letting it become a way to weaken law 4.

Note the ordering constraint: an absolute date computed at synthesis time from a
TTL would move on every re-render, and re-renders are *mostly* driven by real
canonical change (the no-op rule), so it would mostly not churn. "Mostly" is
load-bearing — referrers pulled from the mirror after a tombstone are
re-rendered without their canonical form changing, which is the same exception
that makes `generated.at` under-report for those concepts (see `_generated` in
`synthesize.py`). A TTL-derived `stale_after` would need to decide whether a
referrer re-render resets the clock. Deriving it from `sources[].last_modified`
rather than from render time sidesteps the question entirely, and is probably
the right shape.

## 4. Footnote attribution (§5.1)

v0.2 standardizes per-claim attribution as a markdown footnote whose label is a
`sources[].id`:

```markdown
The `events_` table is sharded daily.[^ga4-schema]

[^ga4-schema]: GA4 BigQuery Export schema
```

This is the shape the deferred faithfulness judge (architecture §7) would check,
and the path from law 3's *anchor presence* to *anchor validity* — one of the
named paths to full strength in architecture.md §4.4. It is the most valuable of the four for
kbforge's grounding contract, because it is the first mechanism that ties an
individual claim, rather than a whole concept, back to a source.

**One thing to check before committing to it.** kbforge's `sources[].id` is
`system:native_id` — `local_files:apps/orders.md`. Whether `:` and `/` make
well-behaved markdown footnote labels across the parsers a consumer might use is
unverified. If they do not, the choice is between a sanitized label (and a join
key that no longer equals the doc_id) or a different `id` scheme (and a
migration). Settle that before emitting footnotes, not after.

## Not applicable

**`Attested Computation` and the `# Computation` heading (§10).** kbforge
produces knowledge, not sanctioned computations. A deployment whose SoR contains
runnable definitions could emit them from a connector, but nothing in core
should know about `runtime`, `parameters`, `executor`, or `attester`.

**`okf_version` in a bundle-root `index.md` (§12).** kbforge emits no `index.md`
at all. That is a real gap — progressive disclosure is an OKF affordance kbforge
currently leaves to the consumer to synthesize — but it is a missing feature,
not a conformance defect, and it belongs with whatever increment adds index
generation.
