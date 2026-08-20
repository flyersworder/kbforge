---
type: design-note
title: kbforge — cross-source grounding
description: One concept, one owning document, grounded in and citing related documents from other systems. Concept identity is untouched; the drift that makes a grounded concept stale is derived from the mirror rather than written across connectors.
tags: [okf, grounding, synthesis, mirror, provenance]
generated: { by: human:flyersworder, at: 2026-08-20T00:00:00Z }
status: shipped 0.8.0 — folded into architecture.md §4.4 law 3 and §7.1; this note now holds only what is still deferred
okf_version: "0.2"
---

# kbforge — cross-source grounding

**Problem.** A subject documented in three systems produces three near-duplicate
concepts, each citing one system and blind to the other two. The reviewer reads
three partial answers and reconciles them by hand.

**What shipped in 0.8.0.** A concept still has exactly one **owning** document,
which alone determines its path. Synthesis may additionally read **grounding**
documents from any system, write a body informed by them, and cite them in
`sources` (§5.1). Attribution becomes honest across systems; concept identity
does not move. The full design — vocabulary, the two declaration sites,
resolution, the drift sidecar, emission, and the stub/`grounds` capability
split — is now `docs/architecture.md` §4.4 (law 3) and §7.1. This note keeps
only what did not ship: the deliberately-deferred scope, the known limits, and
the phasing this design set out.

**What this deliberately does not build, even now.** Many documents collapsing
onto one concept. That requires a concept identity distinct from document
identity, and it is the *inverse* of the injectivity property the fetch-side
law and the `native_id` slug exist to guarantee — distinct `doc_id`s must never
reduce to one published file. If cross-system subject resolution proves robust
here, promoting a grounding document to co-owner is a later and much smaller
step. If it proves fragile, that is learned without having dismantled concept
identity first.

## Fold table

Sections 1–9 below described the design that 0.8.0 built. They are no longer
here — restating them in two places is how a doc drifts (CLAUDE.md's docs
layout rule) — and each landed as follows:

| Design-note section | Landed in |
|---|---|
| §1 Vocabulary | `architecture.md` §7.1, opening paragraphs |
| §2 Declaring grounding (both sites) | `architecture.md` §7.1, "Declaring grounding" |
| §3 Resolution | `architecture.md` §7.1, "Resolution" |
| §4 Staleness / drift / sidecar lifecycle | `architecture.md` §7.1, "Staleness is derived..." through "Mutual grounding converges" |
| §5 No-op rule, restated | `architecture.md` §7, no-op paragraph, and §7.1's own restatement |
| §6 Emission (`sources` ordering) | `architecture.md` §4.4 law 3, and §7.1 "Emission" |
| §7 The stub does not ground (`GroundingSynthesizer`) | `architecture.md` §7.1, "The stub does not ground" |
| §8 Untrusted content, one system wider | `architecture.md` §7.1, "Untrusted content, one system wider" |
| §9 Validators (no new law) | `architecture.md` §7.1, closing paragraph |
| §10 Testing | `tests/test_grounding.py`, `tests/test_pipeline.py`, `tests/test_synthesize.py` (gate-broken-in-place per CLAUDE.md; task reports carry the failure messages) |
| §11 Known limits | `CHANGELOG.md`, `## [Unreleased]` → `### Known limits`, verbatim |

## 12. Phasing

| Phase | Contents |
|---|---|
| **0.8.0** — shipped | `grounded_by`, subject map, resolution, sidecar, drift check, multi-source emission |
| **0.8.x** — deferred | the deletion manifest, reusing this note's cross-run state machinery |
| **later** — deferred | promoting a grounding document to co-owner (many-to-one concept identity) |

The deletion manifest was previously slated first. It needed the same cross-run
state cross-source grounding introduced, so building grounding first and
letting the manifest reuse it inverted that order deliberately.
