---
type: design-note
title: kbforge — grounding rules (fresh evidence into existing concepts)
description: Templated, deterministic rules that ground existing concepts in matching documents from another system — web articles naming a product, market news about an application — ranked newest-first by a date facet or first-seen time, reusing the drift sidecar so a concept re-synthesizes only when its evidence changes.
tags: [okf, grounding, synthesis, mirror, provenance, web]
generated: { by: human:flyersworder, at: 2026-09-18T00:00:00Z }
status: shipped — unreleased; folded into architecture.md §7.1; this note keeps the rationale and §9
okf_version: "0.2"
---

# kbforge — grounding rules

**The short version.** Grounding today is declared by the grounded concept,
listing the ids that ground it, which must be known in advance. Web search
results aren't: the articles that should refresh a product concept or an
application overview arrive run by run. This note adds **rules** to the
grounding config. A rule says which concepts it grounds (`for`), which documents
may ground them (`from`), what makes a document relevant (`match`, phrases with
`{field}` placeholders filled per concept), and how many to keep (`newest`,
ranked by a date facet or by when kbforge first saw the document). Rules are
evaluated deterministically in the pipeline, over the mirror, in the owner's
run. They feed the existing resolution and the existing drift sidecar, so a
concept re-synthesizes only when its matched evidence changes, and every new
citation arrives in review with the rule that caused it.

## 1. Context

The motivating deployment is an application knowledge base: product rows from
Denodo (`kbforge-sql`), roadmap documents from an MCP server, and the web, split
into **Watch** (curated URLs) and **Scout** (allowlisted search) through
`kbforge-mcp` 0.2.0 and `examples/web-source-mcp`. Phase 1 made web pages
concepts of their own. This is phase 2: making them **evidence in the concepts
that already exist**. A competitor's gate-driver launch should refresh the
comparable product concepts, and a SiC market report should refresh the
application overview.

The knowledge base is the whole bundle, so the target of an article is
*whichever concepts it is relevant to*, never one fixed page.

## 2. Decisions, and why

**The concept declares, by rule, not the search.** Three shapes were weighed:

| Shape | Failure |
|---|---|
| The search stamps `grounds:` on what it finds | One URL is found by several searches and by Watch (measured live). A stateless connector can only record the last writer's link, so the link flips, as titles did before `title_key`. |
| The concept grounds on "the newest N documents of system X" | Forces one system per search. Overlapping URLs across those systems hit the bundle-path collision abort, and Watch pages can't be shared. |
| **The concept grounds on a rule over the mirror** | None of the above: selection doesn't care who fetched a page, so one shared `web` system keeps working. |

This keeps the property grounding was built on: the owner declares, and
resolution happens in core, never in the synthesizer. A synthesizer that chose
its own sources would be choosing its own provenance.

**Relevance is text matching, deterministic.** Semantic matching (an LLM or
embeddings) would put a non-deterministic judgement into which sources a concept
cites. Runs would stop being replayable, and the no-op rule would rest on a
model's opinion. If recall proves too low, a semantic matcher may later
*propose* links as reviewable data that feeds deterministic rules. It never
decides at synthesis time (§9).

**Recency is a named date facet, falling back to first-seen.** The mirror can't
tell us what's new: every publishing run re-commits all fetched documents
(`pipeline.run` → `commit(mirror_path, docs)`), and connectors stamp one
`retrieved_at` per run. So every page from the latest fetch shares a timestamp.
A publication date is the right signal but isn't always present. First-seen
always is, and a Scout's search time filter (`tbs: qdr:m`) keeps it close to
publication.

## 3. Configuration

Rules live in the existing grounding YAML (`kbforge run --grounding PATH`),
beside the subject map:

```yaml
max_grounding_docs: 5            # existing: cap for explicit grounding
grounding:                       # existing: explicit ids, unchanged
  local:applications/ev-traction.md: [web:@www.bosch-semiconductors.com/stories-and-events/eg120]
rules:                           # new
  - for:   {type: product}                    # which concepts this rule grounds
    from:  {system: web}                      # which documents may ground them
    match: ["{native_id}"]                    # any-of; {field} filled per concept
    newest: 3                                 # cap, newest first (default 3)
    by: published                             # optional date facet; else first-seen
  - for:   {doc: [local:applications/ev-traction.md]}
    from:  {system: web}
    match: ["traction inverter", "SiC MOSFET"]
    newest: 5
```

### 3.1 Semantics

- **`for`** selects owner concepts by `type` (the OKF `type`, i.e. `structured["type"]`),
  `system` (`anchor.system`), and/or `doc` (qualified `doc_id`s). Keys present
  are AND-ed. A concept may match several rules; their results are unioned.
- **`from`** restricts candidate grounding documents; `system` is its one key in
  this version. A document never grounds itself, a tombstoned candidate is
  skipped, and one artifact is cited once: the same filters `resolve` applies
  today.
- **`match`** is a list of phrases, any of which qualifies a candidate:
  - Matching is case-insensitive and on word boundaries (`(?<!\w)…(?!\w)`
    around the escaped phrase, not `\b…\b`, so a phrase starting or ending in
    punctuation still matches: `TLE9` matches `TLE9 driver` but not `TLE95`,
    and `SiC-MOSFET (1200V)` matches text ending `... (1200V) part.`). It runs
    over the candidate's `title` and `text`, both NFC-normalized first.
  - `{field}` is filled from the owner's `structured` fields (its facets), or
    the reserved names `title` and `native_id`. For a `kbforge-sql` source the
    id columns become the `native_id`, not a facet, so a product id is
    `{native_id}`, and any other column is available only if it is configured
    as a facet. The substitution uses the same plain `{name}` grammar as
    `kbforge-sql`'s `url_template`. It is never `str.format`, which reads `{a.b}`
    as an attribute lookup.
  - A phrase whose field is missing or blank for a given owner is **dropped for
    that owner**. Matching it as empty text would match everything, and failing
    the rule would let one incomplete product row block all the others. A rule
    left with no phrases for an owner matches nothing for it.
- **`newest`** caps each rule's matches per owner. Order is descending by the
  `by` facet, then by first-seen, then ascending `doc_id`, so the order is total
  and deterministic. A `by` value that doesn't parse as an ISO-8601 date or
  datetime counts as missing for that candidate (it falls back to first-seen,
  and there is a note). A naive datetime is compared as UTC, the one assumption
  needed to order it against an aware one.
- **Explicit grounding outranks rules and keeps its own cap.** Explicit ids
  resolve under `max_grounding_docs` as today. Rule matches come on top, each
  rule within its own `newest`, deduplicated against the explicit set and
  against earlier rules -- an explicit id, or a document an earlier rule
  already cites, is excluded from a rule's candidates before it ranks and caps
  them, not after, so it never consumes one of that rule's `newest` slots. A
  hand-picked source is never crowded out by news, and a rule cites up to
  `newest` documents nothing before it cites. The prompt stays bounded because
  `max_source_chars` is already split across all grounding documents
  (`llm_synthesizer._grounding_block`).

## 4. Pipeline

- `grounding.py` gains `rule_matches(owner, cfg, by_id, first_seen, *,
  exclude=frozenset()) -> tuple[list[tuple[str, str]], list[str]]`, returning
  `(doc_id, reason)` pairs in rank order plus notes. It is pure, over `by_id`:
  the mirror plus this fetch, the same map resolution already builds.
  `exclude` is a set of `resource_key` values skipped before ranking and the
  `newest` cap; `resolve_all` passes the owner's and the explicit set's.
  Patterns are compiled once per owner per rule.
- Resolution runs for **explicit ids** (`declared_ids`, unchanged, capped by
  `max_grounding_docs`) and for **rule matches** (already capped per rule),
  through the same self/tombstone/duplicate filters. The union is what the
  synthesizer receives and what the sidecar records.
- **The drift sidecar is reused unchanged.** It records the resolved set and
  hashes at publish. A newly matching document changes the set, and an edited
  matched document changes a hash. Either makes the owner drift and
  re-synthesize on its own run, and an unchanged world stays a no-op.
- **The scan gate** gains one condition: the drift scan runs when `rules` is
  non-empty, in addition to today's triggers (a grounding synthesizer, and
  something declared or a sidecar present). Without it, a concept with no
  grounding yet could never pick up its first matching article.
- **Scope is unchanged:** drift candidates are this run's own systems
  (`_drift_candidates`). A web run never re-synthesizes a product concept; the
  product source's next run does. No run writes another connector's state.

## 5. First-seen state

- One small record per document: `mirror/_first_seen/<slot>.json`, holding
  `{"doc_id": ..., "first_seen": <ISO datetime>}`, beside `mirror/_grounding/`
  and named with `mirror.slot_key` like the sidecars. It is a subdirectory
  because `load_all` globs `mirror/*.json`.
- It is written in the pipeline's commit step, **only for documents this run
  commits, and only when no record exists**. The value is that document's
  `anchor.retrieved_at`. Each system records only its own documents.
- It is written atomically (a unique temp file, then `os.replace`), as
  `write_sidecar` is. An unreadable record reads as absent and is rewritten on
  the next commit rather than raising, for the same reason `read_sidecar`
  tolerates corruption: raising would wedge every later run on the shared
  mirror.
- It is deleted when its document is tombstoned. It is never written by a failed
  or no-op run, so replaying a run cannot move a first-seen time.
- **Upgrade:** documents committed before this release have no record. They get
  one on their next commit, so everything a source republishes after upgrading
  looks equally new once. That is one-time, and noted in the CHANGELOG.
- It is loaded once per run, only when `rules` is non-empty (the pipeline
  can't know per-document ahead of ranking whether a `by` value will be
  usable). Before ranking, it is overlaid with this run's own non-deleted
  documents, existing records winning: a rule can match a document committed
  in the *same* run as the owner it grounds, before that document's own
  sidecar exists, so without the overlay it would rank as undated on this run
  and dated on the next identical-fetch run — an unchanged world would stop
  being a no-op. `with_first_seen` is the overlay.

## 6. Validation and the review surface

**Offline validation** (`grounding.problems_for`: before any fetch, every
problem at once, `extra="forbid"` on every rule model):

- `for` is non-empty (at least one of `type`, `system`, `doc`); `from.system` is
  present; `match` is non-empty; `newest ≥ 1`.
- `doc` entries are qualified `doc_id`s, validated with `is_qualified` as the
  subject map is.
- Placeholders are well-formed `{name}` tokens; unpaired braces are an error.
- A `from.system` equal to a `for.system` is allowed, because self-grounding is
  filtered.

**Review notes.** When a rule contributes a grounding document, the review
request says why, through the existing `ChangeSummary.grounding_notes`:

> `concepts/IMC300/overview.md`: grounded by rule 1 (`{native_id}` = "IMC300")
> via `web:@www.automotiveworld.com/news/skyworks-unveils-sic-and-igbt-gate-driver-at-pcim-2026`

Notes also record a rule's `newest` cap dropping matches, and an unparseable
`by` value, as `resolve` already notes the explicit cap -- bounded so a match
count in the thousands cannot blow up the review body: the cap note lists
dropped doc_ids only up to 5, past which it names a count and the first 5
("dropped 1495 (first 5: web:a, web:b, …)"), and an unparseable-`by` note is
written only for a candidate the cap keeps, in doc_id order, never for one it
drops. A new citation always arrives with its reason; that is what keeps a
review of a re-synthesized concept short.

**The stub synthesizer doesn't ground** (`grounds = False`), so rules take
effect only with `--synthesizer llm`. With the stub, rules are validated and the
CLI prints one line saying they are inactive.

## 7. Invariants (CLAUDE.md)

- **No-op rule:** covered, because drift is part of it already, and rules add
  nothing that isn't in the mirror.
- **`normalize` is pure:** untouched. Rules run in the pipeline.
- **One bundle path, one owner:** untouched. Grounding never moves identity.
- **Resolution stays out of the synthesizer:** it still receives a resolved list.
- **Emit-side laws:** unchanged. Rule-grounded documents become `sources` entries
  exactly as explicit grounding does (§4.4 law 3).

**Untrusted content.** Rules widen how much web text reaches the synthesizer.
The existing bounds hold: the wrapper's domain allowlist, each rule's `newest`,
links resolved by kbforge rather than taken from model prose, and the human
review gate. None of these makes an untrusted page safe to publish unreviewed.

## 8. Testing

- **Matching:** word boundaries, case, NFC, placeholder filling, a missing or
  blank field dropping its phrase, and a rule left with no phrases matching
  nothing.
- **Ordering:** a `by` facet, the first-seen fallback, an unparseable `by`
  (with its note), the `doc_id` tiebreak, and the `newest` cap (with its note).
- **Resolution:** explicit ids outrank rules, keep their own cap, and duplicates
  are cited once; self and tombstones are skipped.
- **First-seen:** written on commit only when absent; untouched by a failed or
  no-op run; deleted on tombstone; an unreadable record is tolerated.
- **Pipeline, end to end:**
  - A newly matching article makes the owner drift and re-synthesize, with the
    rule note in the summary.
  - A non-matching article leaves it a no-op.
  - An edited matched article drifts via its hash.
  - A web run doesn't re-synthesize a product concept.
- **Validation:** every §6 rule, with exact messages.
- **Mutation checks:** the scan-gate condition, the word-boundary match, the
  missing-field drop, the explicit-first ordering, and the first-seen
  write-once. Each is broken in place and must fail with its gate's message.
- **Live:** `kbforge-sql` product rows (a local PostgreSQL) plus the Firecrawl
  web source under one shared mirror, with `--synthesizer llm`. A real article
  naming a product grounds that product's concept, and a rerun no-ops.

## 9. Deferred

- **A matching index or prefilter.** Matching is O(owners × mirror docs ×
  text) every run with rules -- measured 96s/run for 500 owners × 2,000 10KB
  docs. An index, or a sound case-insensitive substring prefilter ahead of the
  word-boundary regex, would cut this; `casefold()` and `re.IGNORECASE` do not
  agree on every codepoint (Turkish dotless ı / dotted İ), so a prefilter built
  on one and a match built on the other can silently disagree about a hit.
  Known limit for now; see the CHANGELOG.
- **Reader-provided dates in `kbforge-mcp`**, so web pages carry a `published`
  facet from their metadata and `by: published` works for Scout. Until then,
  web documents rank by first-seen. `kbforge-sql` date columns work with `by`
  immediately.
- **More `from` keys** (a facet, a `type`), once a deployment needs to separate
  kinds of documents within one system.
- **A semantic link proposer** that writes suggested links as reviewable data
  feeding deterministic rules. It never runs at synthesis time.
- **System-qualified bundle paths**, which would lift the "one shared `web`
  system" constraint that made the concept-side rule the only workable shape.
