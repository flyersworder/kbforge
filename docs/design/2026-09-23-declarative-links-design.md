---
type: design-note
title: kbforge — declarative links (links.yaml)
description: A deployment-level links file — an explicit map plus tag and facet rules — resolved in the pipeline by doc_id over the whole shared mirror, applied to synthesis-time copies only, kept current across systems by a managed-links sidecar, and allowed to cross systems for the first time.
tags: [okf, links, pipeline, mirror, grounding, cross-system, review]
generated: { by: human:flyersworder, at: 2026-09-23T00:00:00Z }
status: proposed — issue #41; depends on the describe synthesizer (#40) for model-written tags
okf_version: "0.2"
---

# kbforge — declarative links

**The short version.** `kbforge run --links links.yaml` adds links no source's
data carries: editorial pairs between taxonomies, relations inside one document
that share no keyword, and relations implied by shared tags or facet values.
Links resolve by `doc_id` over the whole shared mirror, so they may cross
systems. They are applied to copies handed to the synthesizer, never to the
mirror. A `_links/` sidecar records each concept's managed links, and a concept
whose set moved is re-rendered on its own system's next run, so review stays
one branch per system.

## 1. Context

kbforge already renders, validates and maintains links: `relations` become
`links`, dangling targets are dropped under §4.4 law 2, and a link is restored
when its target arrives (#32). But only a connector can set `relations`, and
only `local_files` does. In one deployment (72 concepts from 5 sources, no
links), the tasks an agent answered worst were the ones that needed a relation
between two pages it had found. An index and good descriptions (#40) help
discovery; they cannot state that A relates to B.

Two of the three motivating cases cross systems (a planning deck's variant and a
database's application page; reports and roadmap topics). Today the pipeline
aborts on any cross-system relation. Most of this design is about making that
safe.

## 2. Decisions, and why

| Decision | Why |
|---|---|
| A pipeline flag `--links`, not `--set` | A connector must not know other systems exist (`normalize` is pure). Same reasoning as `--grounding` (architecture.md §7.1). |
| Resolve by `doc_id` over the whole mirror + this run | The `bundle-path-collision` check already runs over the whole mirror, so a `doc_id` maps to exactly one bundle path. The scoping that protects `existing` exists to stop resolution *by path*; resolution by id does not need it. |
| Lift the `cross-system-relation` abort, for `links.yaml` **and** connector `relations` | One resolution rule for both. Keeping the abort for relations would make the same link legal or fatal depending on where it was declared. |
| System-qualified bundle paths are not a prerequisite | They fix path collisions, not the merge-order window (§6), which is the only real cost of cross-system links. Links are resolved by `doc_id` and rendered through `concept_path`, so if qualified paths ship later, links follow unchanged. Tracked separately. |
| Applied to synthesis-time copies, never to `docs` | `commit()` would otherwise write config-dependent content into the mirror, and editing `links.yaml` would mark documents modified for a reason unrelated to their source (§7.1's rule for the subject map). |
| Upkeep by a sidecar of doc-id sets, not content hashes | A link depends on whether its target exists, not on what it says. |
| Link drift runs under every synthesizer | Links are frame, not prose. Grounding drift is gated on `grounds`; link drift cannot be. |

## 3. Configuration

```yaml
links:                                    # explicit, editorial
  planning_deck:variants/commercial-vehicles:
    - {to: db_apps:applications/777-commercial-vehicles, symmetric: true}
  planning_deck:gaps/redundant-supply:
    - planning_deck:requirements/fail-mode   # a plain id is one-way
rules:
  - for:  {system: reports}               # type / system / doc, AND-ed
    to:   {system: planning_deck}         # type / system, AND-ed
    match: {tags: any}                    # or {facet: scenario}
    max: 10                               # per concept; default 10
    symmetric: false                      # default false
```

- `links` maps a qualified `doc_id` to a list whose items are either a qualified
  `doc_id` (one-way) or `{to: <doc_id>, symmetric: <bool>}`.
- `rules[].for` selects owners exactly as grounding rules do (`type`, `system`,
  `doc`). `rules[].to` selects candidate targets by `type` and/or `system`.
- `match` names exactly one of `tags: any` (share at least one tag) or
  `facet: <name>` (equal scalar value, or a shared value if either side is a
  list).
- Unknown keys are rejected (`extra="forbid"`), so a typo is a config error.

**Tags** are the tags a concept ships (describe design §5): source tags from
`structured["tags"]` ∪ model tags from the concept's `_described/` sidecar.
Reading the sidecar keeps rule evaluation a pure function of mirror state.

### 3.1 Validation (`links_problems`, before any fetch)

- Every key, target and `for.doc` entry is a qualified `doc_id`; bare ids are
  rejected, as in `grounding.yaml`.
- An explicit entry linking a concept to itself is an error.
- A rule needs at least one `for` selector and at least one `to` selector,
  exactly one of `match.tags` / `match.facet`, and `max >= 1`.
  `match.tags` accepts only `any`.
- A reference to a document not in the mirror is **not** an error: it may live
  in a system that has not synced yet (§7.1's shape-versus-resolution rule).
- Under `stub`/`llm`, a tag rule when no document carries source tags gets one
  CLI warning ("tag rules read source tags and describe tags; none found"),
  not an error.

## 4. Resolution

Once per run, in the pipeline, against `by_id` (the whole mirror overlaid with
this run's documents, this run's tombstones removed, exactly as grounding
resolution uses it). For a concept `D`:

```
declared(D) = D.relations
            ∪ explicit[D]
            ∪ {S : D ∈ explicit[S] and the entry is symmetric}
            ∪ rule_targets(D)                          # rules whose `for` selects D
            ∪ {S : D ∈ rule_targets(S), rule symmetric}
resolved(D) = {T ∈ declared(D) : T ∈ by_id, T ≠ D}
```

- **Missing or tombstoned target.** Dropped. When it was declared in
  `links.yaml` (entry or rule), a `grounding_notes` note says so; a missing
  connector relation is dropped silently, as today.
- **Rule ranking and cap.** Candidates are ranked by the number of shared tags
  or facet values (most first), then by `doc_id`, and cut at `max`, with a
  note naming what was dropped (first five, then a count, as grounding rule
  notes do). Without the cap, `tags: any` over a large bundle links nearly
  everything to everything.
- **Determinism.** Every step is sorted, so the same mirror and config yield the
  same set.

**Emission.** The pipeline hands the synthesizer
`doc.model_copy(update={"relations": sorted(resolved(D))})` and adds each
resolved target's `concept_path` to `existing`. `assemble`'s filter and
`_check_links_resolve` (law 2) then accept cross-system targets with no change
of their own. `links` stays a sorted list of bundle paths bound across both
carriers by `_check_carriers_agree`. `commit()` still receives the connector's
own documents.

**`_scope_failures`.** The `cross-system-relation` failure is removed. The
`bundle-path-collision` check is unchanged, and it is what makes id-based
resolution unambiguous.

## 5. Upkeep across runs

### 5.1 The managed-links sidecar

`mirror/_links/<slot_key>.json` records `{doc_id, links: [doc_id, …]}`: the
concept's **managed** links — the resolved links that do not come from a
same-system connector `relation`. Same-system relations keep today's upkeep
(`referrers` for removals, `arrivals` for additions, #32), which needs no scan.
So a deployment with no `--links` and no cross-system relations writes no
sidecars and pays nothing.

Rules shared with `_grounding/`:

- written by the pipeline after a successful publish, through `_write_atomic`,
  only for concepts in `proposal.files`, and only when the managed set is
  non-empty;
- deleted when the managed set empties and when the owner is tombstoned — a
  stale sidecar would drift the concept on every run forever;
- read tolerantly: unreadable reads as empty;
- a missing sidecar is an empty set, not "exempt".

### 5.2 Link drift

**Gate.** The scan runs when `--links` is given or `mirror/_links/` is
non-empty (a directory listing, like `has_sidecars`). It loads the mirror, so
under that gate a no-op run costs O(mirror), as grounding's does.

**Rule.** A mirror document whose `anchor.system` is in this run's `systems`,
and which is not already changed, removed, or in the backlog, is re-rendered
when its current managed set differs from its recorded one. This covers:

- an edit to `links.yaml` — only concepts whose set moved are rebuilt;
- a target added or tombstoned in another system's run — the referrer is
  rebuilt on its own system's next run, never on the other system's branch;
- a symmetric reverse link whose owner is in another system — that system's
  run finds the drift;
- a target that arrives after the link was declared.

**Convergence.** Re-rendering never changes a document's `relations`, the
mirror, or `links.yaml`, so it never changes the set it is compared on. Mutual
and symmetric links rebuild at most once each.

**Notes.** A drifted concept gets "re-synthesized because its links changed
since it was last published; its own source is unchanged", as grounding drift
does. It is deduplicated against drift, referrers and arrivals once, with the
existing dedupe.

**Cost by synthesizer.** Under `stub`, a rebuild renders bytes. Under
`describe`, it reuses the `_described/` cache, so no model call. Under `llm`, a
link-only rebuild re-synthesizes the body, as referrers and arrivals already do.
Stated, not special-cased.

### 5.3 The no-op rule

A run synthesizes only when something a concept is built from has changed.
`links.yaml` and other systems' documents are now things a concept's links are
built from, so the rule reads: return `NoOp()` before synthesis when
`ChangeSet.is_noop` and there is no grounding drift **and no link drift**. It is
still never "open a review request and see". CLAUDE.md's invariant text gains
the clause, as it gained grounding.

### 5.4 Chunking

Link drift counts toward `max_concepts`, admitted after changed documents and
grounding drift, into the room that remains. A deferred concept writes no
sidecar, so the next run finds the same drift. `touched` (the redo snapshot)
includes link-drifted documents, and `chunking`'s per-document file list
(`chunking.py`, today slot + `_grounding/` + `_first_seen/`) gains `_links/`, or
`redo` would leave a sidecar describing a rolled-back publish.

**Cost of rules.** A symmetric rule means evaluating every rule for every
candidate owner, not just the concepts this run touches, so rule evaluation is
O(owners × candidates) whenever the scan runs. Accepted at current bundle sizes
(tens to hundreds of concepts); a prefilter or index is the same work #30 tracks
for grounding rules.

## 6. The merge-order window

The mirror advances on publish, not merge. If system A's request links to
`B:x` while B's request is still open, merging A first puts a link on `main`
that dangles until B merges. If B's request is closed instead, it dangles until
B re-proposes or A is re-rendered. Within one system this cannot happen: target
and referrer ride the same sync branch.

It widens an existing hazard (mirror ahead of `main` after a close, which
`redo` exists for) rather than adding a new kind. It is disclosed, not hidden:
the review note names every cross-system target,
`links to db_apps:applications/777-commercial-vehicles (system db_apps)`, so a
reviewer can merge in order. CLAUDE.md's "one bundle path, one owner" text
changes: the collision abort stays; the cross-system abort is replaced by a
pointer to this section.

## 7. Invariants (CLAUDE.md)

- **Pipeline order:** unchanged; resolution and drift sit where grounding's do.
- **No-op rule:** extended by one clause (§5.3), not weakened.
- **One bundle path, one owner:** the collision abort is unchanged; the
  cross-system abort is lifted with the reasoning in §2 and §6.
- **`normalize` is pure / config never in the mirror:** links reach synthesis
  copies only (§4).
- **Dual carrier:** `links` is already bound; nothing new rendered.
- **kbforge never merges:** unchanged. The merge-order window is surfaced to
  the reviewer, not solved by merging.

## 8. Testing

Offline:

1. **Explicit.** One-way adds a link on the source only. Symmetric adds the
   reverse; with the target in system B, the reverse appears on B's run and
   not A's.
2. **Rules.** A tag rule links exactly the pairs that share a tag and nothing
   for untagged concepts, from source tags and from `_described/` tags. A facet
   rule links on equal values. Ranking and the `max` cap, with the cap note.
3. **Drift.**
   - Editing `links.yaml` alone rebuilds only the concepts whose set changed.
   - A target missing on run 1 gives a note and no link; after it arrives, the
     referrer's next run adds the link.
   - B tombstoning a target removes A's link on A's next run.
   - An unchanged world is `NoOp`.
   - No `--links` and no sidecars: the scan never runs (asserted by counting
     mirror loads, as the grounding gate is tested).
4. **Mirror purity.** Mirror documents committed with `--links` are
   byte-identical to those committed without it.
5. **Cross-system.** The `cross-system-relation` abort is gone; a path
   collision still aborts with its existing message; the review note names
   cross-system targets. Mutation check, in place and restored with
   `git checkout --`: scoping target resolution back to this run's systems
   must fail the cross-system test with the link-resolvability message.
6. **Chunking.** Link drift counts toward the cap; a deferred concept is picked
   up by the next run; redo restores `_links/` sidecars.
7. **Validation.** Each `links_problems` message, asserted by text.

Live: two `local_files` systems, a scratch mirror, and a private GitHub scratch
repo. One symmetric cross-system entry and one tag rule over `describe` tags,
run system by system. Each system's review request touches only its own
concepts, and the links are in the files.

## 9. Deferred

- **System-qualified bundle paths.** Remove the collision abort at its root;
  a separate, breaking release.
- **Relations from source data** (e.g. a `relations` column in kbforge-sql):
  connector work, separate.
- **Link-only rebuilds without re-synthesis under `llm`**: re-render the frame
  around the previously published body. Worth it only if link drift under `llm`
  proves costly.
- **A semantic link proposer:** deliberately not this; links here are declared
  and deterministic, for the same reason grounding rules are.
