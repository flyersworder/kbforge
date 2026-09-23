---
type: design-note
title: kbforge — editorial links and derived relations
description: Editorial links declared in a reviewed links.yaml, resolved by doc_id across systems and rendered as an OKF §6.1 body section that says why two concepts relate; relations implied by shared tags or facets derived at consumption time by okfquery rather than materialized by the producer.
tags: [okf, links, pipeline, mirror, cross-system, okfquery, review]
generated: { by: human:flyersworder, at: 2026-09-23T00:00:00Z }
status: proposed — issue #41
okf_version: "0.2"
---

# kbforge — editorial links and derived relations

**The short version.** #41 asks for three kinds of link that no source's data
carries. They split along the line OKF v0.2 draws:

- **Editorial links** (two taxonomies with no join key; a requirement that
  closes a gap on another slide) are new information. They are declared in a
  reviewed `links.yaml`, resolved by `doc_id` across systems, and rendered
  where OKF puts links — markdown in the body, with the reason in prose.
- **Relations implied by shared tags or facets** are already in the bundle's
  frontmatter. OKF §3.1 leaves such views to consumers, so `okfquery related`
  derives them at query time instead of kbforge materializing and maintaining
  them.

And every concept's links, editorial or from a connector's `relations`, now
also render in a kbforge-owned `## Related` body section, so a generic OKF
reader — and an agent reading one page — sees them.

## 1. Context

kbforge already renders, validates and maintains links: `relations` become the
`links` frontmatter key, dangling targets are dropped under §4.4 law 2, and a
link is restored when its target arrives (#32). But only a connector can set
`relations`, and only `local_files` does. In one deployment (72 concepts from 5
sources, no links), the tasks an agent answered worst were the ones where it
found both pages but did not see that they relate.

Two facts about OKF v0.2 shape this design:

- **OKF links are markdown links in the body** (§6.1), and "the specific kind
  (parent/child, references, joins-with, depends-on) is conveyed by the
  surrounding prose, not by the link itself". kbforge's `links` frontmatter key
  is a producer extension a generic consumer does not read, and it says
  nothing about *why* two concepts relate — exactly what the agent missed.
- **Tag views are a consumer concern.** "A consumer that wants a tag-browsing
  view can synthesize one at consumption time by scanning frontmatter" (§3.1).
  Materializing tag-implied links in the producer means storing derived data
  that goes stale, and paying drift detection and an every-pair rule
  evaluation to keep it fresh.

## 2. Decisions, and why

| Decision | Why |
|---|---|
| Editorial links from a pipeline flag `--links`, not `--set` | A connector must not know other systems exist (`normalize` is pure). Same reasoning as `--grounding` (architecture.md §7.1). |
| Tag- and facet-implied relations derived by `okfquery related`, not materialized | OKF §3.1 puts them on the consumer. Always current, no mirror state, no drift, no cross-system window, no every-pair cost, and no dependency on #40. |
| Every link renders in a kbforge-owned `## Related` body section | OKF §6.1 links live in the body. One rule for editorial links and connector `relations`, so every link is visible to any OKF reader. |
| Rendered by the pipeline, after synthesis, from the projection's `links` | Frame, not prose: every synthesizer, including third-party ones, gets the same section, and none can forge it. Same posture as `files_removed`. |
| Bound to the projection by a validator | The dual-carrier rule: the section is a second carrier of `links`. This delivers part of architecture.md's deferred "law 2 link normalization" (extract the links actually present in the body). |
| Resolve by `doc_id` over the whole mirror + this run | The `bundle-path-collision` check already runs over the whole mirror, so a `doc_id` maps to exactly one bundle path. The scoping that protects `existing` exists to stop resolution *by path*; resolution by id does not need it. |
| Lift the `cross-system-relation` abort, for `links.yaml` **and** connector `relations` | One resolution rule for both. Keeping the abort for relations would make the same link legal or fatal depending on where it was declared. |
| System-qualified bundle paths are not a prerequisite | They fix path collisions (#42), not the merge-order window (§6). Links resolve by `doc_id` and render through `concept_path`, so they follow that change unchanged. |
| Applied to synthesis-time copies, never to `docs` | `commit()` would otherwise write config-dependent content into the mirror (§7.1's rule for the subject map). |
| Upkeep by a sidecar of doc-id sets | A link depends on whether its target exists, not on what it says. |

## 3. Configuration

```yaml
# links.yaml
links:
  planning_deck:variants/commercial-vehicles:
    - to: db_apps:applications/777-commercial-vehicles
      note: the database page for this variant's application
      symmetric: true
  planning_deck:gaps/redundant-supply:
    - to: planning_deck:requirements/fail-mode
      note: the requirement that closes this gap
    - planning_deck:requirements/diagnostics      # a plain id: one-way, no note
```

- `links` maps a qualified `doc_id` to a list whose items are a qualified
  `doc_id`, or `{to, note?, symmetric?}`.
- `note` is one line of prose rendered after the link. On a symmetric entry it
  renders on both sides.
- `symmetric: true` also links the target back to the source.
- Unknown keys are rejected (`extra="forbid"`), so a typo is a config error.

### 3.1 Validation (`links_problems`, before any fetch)

- Every key and `to` is a qualified `doc_id`; bare ids are rejected, as in
  `grounding.yaml`.
- An entry linking a concept to itself is an error.
- A `note` is a single non-blank line.
- A reference to a document not in the mirror is **not** an error: it may live
  in a system that has not synced yet (§7.1's shape-versus-resolution rule).

## 4. Resolution

Once per run, in the pipeline, against `by_id` (the whole mirror overlaid with
this run's documents, this run's tombstones removed, exactly as grounding
resolution uses it). For a concept `D`:

```
declared(D) = D.relations
            ∪ {T : T ∈ links[D]}
            ∪ {S : D ∈ links[S] and that entry is symmetric}
resolved(D) = {T ∈ declared(D) : T ∈ by_id, T ≠ D}
notes(D, T) = the entry's note, for editorial links that carry one
```

- **Missing or tombstoned target.** Dropped. When it was declared in
  `links.yaml`, a review note says so; a missing connector relation is dropped
  silently, as today.
- **Determinism.** Everything is sorted by `doc_id`.

The pipeline hands the synthesizer
`doc.model_copy(update={"relations": sorted(resolved(D))})` and adds each
resolved target's `concept_path` to `existing`. `assemble`'s filter and
`_check_links_resolve` (law 2) then accept cross-system targets with no change
of their own. `commit()` still receives the connector's own documents.

**`_scope_failures`.** The `cross-system-relation` failure is removed. The
`bundle-path-collision` check is unchanged, and it is what makes id-based
resolution unambiguous.

## 5. Rendering: the `## Related` section

After synthesis and before validation, for every file in `proposal.files`
whose projection has non-empty `links`, the pipeline appends:

```markdown
<!-- kbforge:related -->
## Related

- [Fail-mode requirement](/concepts/requirements/fail-mode/overview.md) — the requirement that closes this gap
- [Diagnostics requirement](/concepts/requirements/diagnostics/overview.md)
```

- **Order and text.** Sorted by path, matching `links`. The link text is the
  target document's `title` from `by_id`; the note, when present, follows an
  em dash.
- **Paths are bundle-absolute** (leading `/`), OKF §6.1's recommended form.
  Frontmatter `links` keeps its current form; changing it is a separate
  compatibility question for okfquery.
- **The marker** is an HTML comment, invisible when rendered, that lets the
  validator find the section unambiguously; a source body may well contain its
  own `## Related` heading.
- **Titles can go stale.** If a target is retitled, the referrer shows the old
  text until the referrer is next rendered. Rebuilding referrers on every title
  change would re-render pages for a cosmetic difference; OKF readers follow
  the path, not the text.
- **Existing bundles.** Every concept with links gains the section the next
  time it is rendered, including concepts whose links come from `local_files`
  relations. Nothing is rewritten until then.

**The binding validator** (`_check_related_section`, beside
`_check_carriers_agree`): the targets listed after the **last** marker,
leading `/` stripped, must equal `concept.links` as a list, and a concept with
no links must have no marker. A source whose text contains the literal marker
therefore fails validation loudly rather than shipping ambiguous links.
Mutation-checked: a link only in the section, a link only in the frontmatter,
and a section on a link-less concept each fail with their own message.

## 6. Upkeep across runs

### 6.1 The managed-links sidecar

`mirror/_links/<slot_key>.json` records `{doc_id, links: [[doc_id, note], …]}`:
the concept's **managed** links, meaning the resolved links that do not come
from a same-system connector `relation`, with their notes. Same-system
relations keep today's upkeep (`referrers` for removals, `arrivals` for
additions, #32), which needs no scan. So a deployment with no `--links` and no
cross-system relations writes no sidecars and pays nothing.

Rules shared with `_grounding/`:

- written by the pipeline after a successful publish, through `_write_atomic`,
  only for concepts in `proposal.files`, and only when the managed set is
  non-empty;
- deleted when the managed set empties and when the owner is tombstoned — a
  stale sidecar would drift the concept on every run forever;
- read tolerantly: unreadable reads as empty; a missing sidecar is empty, not
  "exempt";
- added to `chunking`'s per-document file list (today slot + `_grounding/` +
  `_first_seen/`), so `redo` restores it.

### 6.2 Link drift

**Gate.** The scan runs when `--links` is given or `mirror/_links/` is
non-empty (a directory listing, like `has_sidecars`). It loads the mirror, so
under that gate a no-op run costs O(mirror), as grounding's does. Resolution
is O(links declared), not O(concept pairs): there are no rules to evaluate.

**Rule.** A mirror document whose `anchor.system` is in this run's `systems`,
and which is not already changed, removed, or in the backlog, is re-rendered
when its current managed set (targets and notes) differs from its recorded
one. This covers:

- an edit to `links.yaml`, including a changed note — only concepts whose set
  moved are rebuilt;
- a target added or tombstoned in another system's run — the referrer is
  rebuilt on its own system's next run, never on the other system's branch;
- a symmetric reverse link whose owner is in another system — that system's run
  finds the drift;
- a target that arrives after the link was declared.

**Convergence.** Re-rendering never changes a document's `relations`, the
mirror, or `links.yaml`, so it never changes the set it is compared on.

**Notes.** A drifted concept gets "re-synthesized because its links changed
since it was last published; its own source is unchanged". It is deduplicated
against grounding drift, referrers and arrivals once, with the existing dedupe.

**Runs under every synthesizer**, unlike grounding drift: links are frame.
Under `stub`, a rebuild renders bytes. Under `describe`, it reuses the
`_described/` cache, so no model call. Under `llm`, a link-only rebuild
re-synthesizes the body, as referrers and arrivals already do. Stated, not
special-cased.

### 6.3 The no-op rule

A run synthesizes only when something a concept is built from has changed.
`links.yaml` and other systems' documents are now things a concept's links are
built from, so the rule reads: return `NoOp()` before synthesis when
`ChangeSet.is_noop` and there is no grounding drift **and no link drift**. It is
still never "open a review request and see". CLAUDE.md's invariant text gains
the clause, as it gained grounding.

### 6.4 Chunking

Link drift counts toward `max_concepts`, admitted after changed documents and
grounding drift, into the room that remains. A deferred concept writes no
sidecar, so the next run finds the same drift. `touched` (the redo snapshot)
includes link-drifted documents.

## 7. The merge-order window

The mirror advances on publish, not merge. If system A's request links to
`B:x` while B's request is still open, merging A first puts a link on `main`
that dangles until B merges. If B's request is closed instead, it dangles until
B re-proposes or A is re-rendered. Within one system this cannot happen: target
and referrer ride the same sync branch.

OKF readers "MUST tolerate broken links" (§6.1, §11), so this is legal in OKF
terms; kbforge's law 2 is stricter than OKF on purpose. It widens an existing
hazard (mirror ahead of `main` after a close, which `redo` exists for) rather
than adding a new kind, and it is disclosed: the review note names every
cross-system target, `links to db_apps:applications/777-commercial-vehicles
(system db_apps)`, so a reviewer can merge in order. CLAUDE.md's "one bundle
path, one owner" text changes: the collision abort stays; the cross-system
abort is replaced by a pointer to this section.

## 8. Derived relations: `okfquery related` (kbforge-okfquery) — shipped in 0.3.0

```
okfquery related concepts/reports/q3-800v/overview.md [--limit 10] [--by tags,scenario]
```

Prints, for one concept:

- **Links to** — its frontmatter `links`;
- **Linked from** — concepts whose `links` name it (backlinks, which no single
  file shows);
- **Shares** — other concepts sharing at least one value of the `--by` facets
  (default `tags`), ranked by the number of shared values, then by path,
  excluding those already listed above; each line names what is shared
  (`shares tags: 800v, sic`).

Each line is `* [Title](path) - description`, the `okfquery index` format, so an
agent reads it the same way. It is pure SQL over the existing `concepts`,
`links` and `facets` columns: no schema change, no mirror. `tags` is a facet to
okfquery (architecture.md §7.3), so no special case.

This is a kbforge-okfquery feature and releases on its own (`kbforge-okfquery`
0.3.0), independently of kbforge (CLAUDE.md, Releasing). It depends on nothing
in this design and can ship first.

## 9. Invariants (CLAUDE.md)

- **Pipeline order:** unchanged; resolution and drift sit where grounding's do,
  and the `## Related` render sits between synthesize and validate.
- **No-op rule:** extended by one clause (§6.3), not weakened.
- **One bundle path, one owner:** the collision abort is unchanged; the
  cross-system abort is lifted with the reasoning in §2 and §7.
- **`normalize` is pure / config never in the mirror:** links reach synthesis
  copies only (§4).
- **Dual carrier:** the `## Related` section is a second carrier of `links`,
  bound by a new validator (§5).
- **kbforge never merges:** unchanged. The merge-order window is surfaced to
  the reviewer, not solved by merging.

## 10. Testing

Offline, kbforge:

1. **Explicit.** A one-way entry links the source only, with its note in the
   `## Related` line. A symmetric entry links both, note on both sides; with
   the target in system B, the reverse appears on B's run and not A's.
2. **Rendering.** Every synthesizer's output gets the same section from the
   same links; a concept without links gets none; paths are bundle-absolute;
   link text is the target's title. The section is present for plain
   `local_files` relations too.
3. **Binding validator.** The three mutation checks in §5, each asserting its
   own message.
4. **Drift.**
   - Editing `links.yaml` alone (a target, or only a note) rebuilds only the
     concepts whose set changed.
   - A target missing on run 1 gives a note and no link; after it arrives, the
     referrer's next run adds the link.
   - B tombstoning a target removes A's link on A's next run.
   - An unchanged world is `NoOp`.
   - No `--links` and no sidecars: the scan never runs (asserted by counting
     mirror loads, as the grounding gate is tested).
5. **Mirror purity.** Mirror documents committed with `--links` are
   byte-identical to those committed without it.
6. **Cross-system.** The `cross-system-relation` abort is gone; a path
   collision still aborts with its existing message; the review note names
   cross-system targets. Mutation check, in place and restored with
   `git checkout --`: scoping target resolution back to this run's systems must
   fail the cross-system test with the link-resolvability message.
7. **Chunking.** Link drift counts toward the cap; a deferred concept is picked
   up by the next run; redo restores `_links/` sidecars.
8. **Validation.** Each `links_problems` message, asserted by text.

Offline, okfquery: `related` lists links, backlinks and shared-facet neighbours
with the shared values named, ranks by overlap, excludes already-linked
concepts, and honours `--by` and `--limit`.

Live: two `local_files` systems, a scratch mirror, and a private GitHub scratch
repo. One symmetric cross-system entry with a note, run system by system: each
system's review request touches only its own concepts, and both `## Related`
sections carry the link and the note. Then `okfquery related` on the result.

## 11. Deferred

- **System-qualified bundle paths** (#42).
- **Bundle-absolute paths in frontmatter `links`**, to match the body; needs
  okfquery to accept both forms first.
- **Relations from source data** (e.g. a `relations` column in kbforge-sql):
  connector work, separate.
- **Link-only rebuilds without re-synthesis under `llm`**: re-render the frame
  around the previously published body. Worth it only if link drift under `llm`
  proves costly.
- **Materialized rule links** (#41's original `rules:`): revisit only if
  consumer-side derivation proves insufficient, e.g. for readers that do not
  run okfquery.
