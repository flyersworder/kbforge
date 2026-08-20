---
type: design-note
title: kbforge — cross-source grounding
description: One concept, one owning document, grounded in and citing related documents from other systems. Concept identity is untouched; the drift that makes a grounded concept stale is derived from the mirror rather than written across connectors.
tags: [okf, grounding, synthesis, mirror, provenance]
generated: { by: human:flyersworder, at: 2026-08-20T00:00:00Z }
status: design — not built
okf_version: "0.2"
---

# kbforge — cross-source grounding

**Problem.** A subject documented in three systems produces three near-duplicate
concepts, each citing one system and blind to the other two. The reviewer reads
three partial answers and reconciles them by hand.

**What this builds.** A concept still has exactly one **owning** document, which
alone determines its path. Synthesis may additionally read **grounding**
documents from any system, write a body informed by them, and cite them in
`sources` (§5.1). Attribution becomes honest across systems; concept identity
does not move.

**What this deliberately does not build.** Many documents collapsing onto one
concept. That requires a concept identity distinct from document identity, and it
is the *inverse* of the injectivity property the fetch-side law and the
`native_id` slug exist to guarantee — distinct `doc_id`s must never reduce to one
published file. If cross-system subject resolution proves robust here, promoting a
grounding document to co-owner is a later and much smaller step. If it proves
fragile, that is learned without having dismantled concept identity first.

## 1. Vocabulary

| Term | Meaning |
|---|---|
| **owning document** | the document whose `doc_id` feeds `concept_path`. Exactly one per concept. Unchanged. |
| **grounding document** | a document read for context and cited in `sources`. Never determines a path. Any system. |
| **grounding set** | for a document D, the resolved grounding documents, after qualification, deduplication and capping. |

`relations` is **not** the channel for this. `synthesize.py:157` turns
`relations` into `links` — an OKF §4.4 law-2 concern about *navigation between
concepts*. Grounding turns into `sources` — a law-3 concern about *provenance of
this concept's content*. They are different OKF fields answering different
questions, and conflating them would make a citation out of a cross-reference.
Hence a separate channel, not a widened one.

## 2. Declaring grounding: two sites, one consumption path

Both declaration sites reduce to the same thing before synthesis sees anything:

```
grounding_ids(D) = D.grounded_by  ∪  subject_map.get(D.doc_id, [])
```

One emitter change, one validator surface, two ways to feed it.

### 2.1 `grounded_by` on the document

`CanonicalDocument` gains `grounded_by: list[str] = Field(default_factory=list)`.
A default keeps every existing connector valid.

Ids are `doc_id`s. A **bare** id is prefixed with the emitting connector's own
system; a **qualified** one (`system:native_id`) passes through. Because this key
is new there is no ambiguity to inherit: `relations` cannot be widened the same
way, since `local_files.py:164` already coerces every entry to its own system and
an existing relation containing a colon would silently change meaning.

`local_files` reads a `grounded_by:` frontmatter list and must add the key to
`_RESERVED_KEYS`, or it sweeps into `structured` and then into facets.

This site serves sources you can edit.

### 2.2 The operator subject map

A YAML file naming groupings from outside any connector, for systems of record
whose documents you cannot edit — the realistic case:

```yaml
# grounding.yaml
max_grounding_docs: 5
grounding:
  confluence:payments/checkout:
    - servicenow:SVC0042
    - mcp-aws:"@docs.aws/lambda/latest"
```

Map **values must be fully qualified** — there is no emitting connector here to
imply a system, so a bare id is a config error, reported by validation rather than
guessed at. Map **keys** are likewise full `doc_id`s.

The file is parsed and validated **before fetch**, in the `problems_for()` shape
the connectors already use: malformed YAML, an unqualified id, or a key that
resolves against neither the mirror nor this run is reported as a message and exits
2, never discovered halfway through a run that has already spent tokens.

Passed as `kbforge run --grounding PATH`. A **pipeline-level** flag, not `--set`:
`--set` is connector config, and a connector must not know other systems exist —
`normalize` is pure, and a connector reaching for a foreign system's ids would
make it not so.

The map is applied in the pipeline and **never merged into `docs`**. If it were,
`commit()` would write config-dependent content into the mirror and editing the
map would mark documents modified for a reason that has nothing to do with the
source. Drift in the map is still detected — see §4, which compares the recorded
grounding *set*, not only the recorded hashes.

## 3. Resolution

Resolve each id against `mirror_docs ∪ docs`, **preferring `docs`** when both
carry it — this run's copy is the fresher one, and its hash is what the sidecar
must record. Then:

- **Self-reference is dropped.** A document does not ground itself; `sources` is
  a set-compare in `_check_sources_shape` (`validate.py:343`), so a duplicate
  resource would collapse and make the rendered and projected lists disagree.
- **Unresolvable ids are dropped with a grounding note, not an error.** The
  target may live in a system that has not synced yet. Failing the run would make
  one source's sync depend on another's, which one-connector-per-run exists to
  prevent. This mirrors law 2's existing dangling-link drop at
  `synthesize.py:162`.
- **Tombstoned targets are dropped**, same as a removed link target.
- **Fan-in is capped** at `max_grounding_docs` (default 5), a top-level key in the
  same file as the map — resolution happens in the pipeline (§6), so the cap is
  pipeline config and does not belong in `LLMConfig` beside `max_source_chars`,
  which governs prompt size rather than provenance. Over the cap, sort by
  `doc_id` and take the first N, with a grounding note recording what was
  dropped. Deterministic and never the model's choice — an LLM picking which
  sources to cite is an LLM editing provenance.
- **Deduplicate by resource string** (`anchor.url or f"{system}:{native_id}"`),
  **including against the owning anchor** — dropping self-reference by `doc_id`
  alone is not enough, since two distinct `doc_id`s can carry the same `url`. That is the key `_expected_resources` builds, so two anchors
  collapsing to one resource would cite the same artifact twice in the rendered
  file while the set-compare stayed happy.
- Per-document text is truncated by the existing `max_source_chars`.

## 4. Staleness: drift is derived, never written across connectors

A concept grounded in `servicenow:SVC0042` goes stale when that record changes in
ServiceNow's run. The owning system's next run must rebuild it — and the
ServiceNow run must not touch a Confluence-owned file, or branch-per-system
review dissolves.

No dirty flag is needed. The mirror is shared and read whole, so Confluence's run
can already see ServiceNow's *current* hash. The only missing fact is what that
hash was **when this concept was last built**.

**Sidecar.** The **pipeline** writes it, not `commit()`: the grounding sets are
pipeline state (§2.2 keeps the map out of `docs`), and `commit(mirror, docs)` takes
only documents. It is written on the `Published` path, immediately after `commit()`,
under the same slug `mirror._slot` derives — so a failed publish leaves both the
mirror and the sidecar at the previous run's state, together.

**The sidecar is deleted, not merely skipped, when it should not exist** — for a
document whose grounding set became empty, and for one this run tombstoned. Not
writing a file does not remove the one already there, and either omission is a
live defect rather than untidiness:

- *empty grounding set.* Drift rule 3 compares the current set against the
  recorded one. A stale sidecar recording `{B}` against a now-empty set fires on
  **every** run, re-synthesizing the same document forever.
- *tombstoned owner.* The slug is derived from `doc_id`, so an orphan sidecar is
  waiting for that `doc_id` to be created again, at which point drift is measured
  against hashes from a document's previous life.

This mirrors `commit()`, which already unlinks a tombstoned document's slot rather
than skipping the write.

For each owning document with a non-empty grounding set, write
`mirror/_grounding/<slug>.json`:

```json
{"doc_id": "confluence:payments/checkout",
 "grounding": {"servicenow:SVC0042": "<content_hash>"}}
```

`load_all` globs `mirror/*.json` at the root, so a subdirectory is invisible to
it. The sidecar lives in the mirror rather than beside cursors because it
describes *what was published last time*, which is the mirror's whole job — and
because the same `rm -rf` that resets a mirror must reset this, or the two drift.

**Scoping the scan.** "This connector's documents" is *not* derivable from the
connector: `kbforge_connector_info()` returns a static name, while a generic
connector's `system` is per-instance — `kbforge-mcp` is named `mcp` and carries a
configured `system`. This is the same identity gap the MCP note recorded as §10.1
(cursor slot keyed by connector name, `system` keyed per instance), surfacing
again, and here it matters more: an unscoped scan would re-synthesize *other*
systems' concepts and break the branch-per-system model this whole design exists
to preserve.

Scope by the run's own output instead, which needs no connector identity at all:

```python
systems = {d.anchor.system for d in docs}
```

If `docs` is empty the scan does not run. That is a real gap — a grounded concept
whose owner was not fetched cannot be found — but an empty fetch publishes
nothing anyway, and inventing a system name from config would put connector
knowledge back into core.

**Drift check.** On a later run, for each mirror document whose `anchor.system` is
in `systems` and which is not already in `changed`, re-synthesize when any holds:

1. a recorded grounding hash differs from that document's current hash in the mirror
2. a recorded id is now absent or tombstoned
3. the current grounding set differs from the recorded key set (this is what
   catches an edited subject map, and a `grounded_by` edit that a connector's
   `content_hash` does not cover)

**Mutual grounding converges**, which is not obvious and is worth stating: drift
is keyed on the *source document's* `content_hash`, and re-synthesis never changes
that — it changes a concept, not a document. So A grounded in B and B grounded in
A rebuild at most once each, rather than ping-ponging forever.

**Deduplicate `changed_docs` after both expansions.** The drift scan and
`referrers` can select the same document — `referrers` filters on `d.doc_id not in
changed`, which does not know about drift. Rendering it twice would put it in
`items` twice and double its entry in `summary.sources_changed`, though `files` is
keyed by path and would merely overwrite.

This is `pipeline.py:134-144` — the `referrers` mechanism — generalized. That
precedent already pulls mirror documents the run never fetched into scope, and
already appends a `grounding_notes` line explaining why a file appears in the diff
whose own source did not change. Grounding drift gets its own note in the same
shape, for the same reason.

## 5. What this does to the no-op rule — argued, not assumed

The no-op rule is a stated trust guarantee: if `ChangeSet.is_noop`, the run
returns `NoOp()` before synthesis, which is what makes `generated.at` honest and
the token bill bounded.

Grounding drift means a concept can need rebuilding when **nothing this connector
fetched changed**. Returning `NoOp()` there would publish a lie by omission: the
concept's own grounding moved.

The invariant is therefore restated, not weakened: *a run synthesizes only when
something a concept is built from has changed.* Today the only such thing is the
owning document. This adds grounding documents to that set. What must not happen —
synthesis for an unchanged concept — still cannot.

**Cost, stated plainly.** `pipeline.py:111` returns before
`load_all(mirror_path)` at line 127, so a no-op run is cheap today. Detecting
grounding drift needs mirror state, so the load moves ahead of the gate and a
no-op run stops being free.

Two things bound that. The scan runs only when the synthesizer grounds (§7), and
only when `mirror/_grounding/` is non-empty — a directory listing, not a mirror
load. So the cost is proportional to grounding actually being *used*, not to
having selected the LLM synthesizer: a deployment that declares no grounding
keeps today's cheap no-op.

Where grounding is in use, every run pays O(mirror). Accepted for a first cut; a
`mirror/_grounding/index.json` of `doc_id → content_hash` would reduce the check to
two small reads, but it is a denormalized cache that can disagree with the slots,
so it is not built until the cost is measured and real.

## 6. Emission

`synthesize.py:161` — `sources=[doc.anchor]` — is the only 1:1 assumption in the
emitter:

```python
sources=[doc.anchor, *(g.anchor for g in grounding)]
```

Grounding documents reach the emitter through a new keyword parameter on the
protocol, defaulted so an existing third-party synthesizer stays valid:

```python
def synthesize(
    self,
    changed_docs: list[CanonicalDocument],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
    grounding: dict[str, list[CanonicalDocument]] | None = None,   # owning doc_id -> docs
) -> ProposedChange: ...
```

Resolution stays in the pipeline. A synthesizer receives resolved documents and
never gets the chance to decide what counts as a source.

**The owning anchor is first, by convention.** `_check_sources_shape` compares
resources as *sets* (`validate.py:343`), so order is free and the validator will
not enforce it — which is exactly why it is written down here: it is what makes
"primary" legible in the artifact without adding a field to the format.

`ChangeSummary.sources_changed` keeps its current meaning — what this run
fetched — so a grounding anchor does not appear there merely for being cited.

## 7. The stub does not ground

`StubSynthesizer` renders the canonical text verbatim. It must not cite a
grounding document whose content never reached the body — that would make
`sources` claim a provenance the artifact does not have.

So grounding is a property of the synthesizer, declared on the protocol:

```python
class Synthesizer(Protocol):
    grounds: bool = False        # LLMSynthesizer sets True
```

The pipeline reads it as `getattr(synthesizer, "grounds", False)`, not as an
attribute access. A default in a Protocol body documents the expected value; it
does not supply one to an implementer, so a third-party synthesizer written
against today's protocol would raise `AttributeError` on a direct read.

The pipeline skips the §4 drift scan entirely when `grounds` is False. Without
this, a stub run would re-synthesize a document to produce a byte-identical file.

## 8. Untrusted content, one system wider

Grounding lets text from system B reach a concept owned by system A. That widens
an existing surface rather than opening a new one — a source document has always
been untrusted input to synthesis — but it widens it in a direction a reviewer may
not expect, since the concept's path and primary citation both say "A".

What bounds it is unchanged and must stay that way: kbforge owns the structural
frame, the model writes prose *inside* it, and `links`, `sources` and
`generated` are assigned by kbforge from resolved anchors, never taken from model
output. A grounding document that contains instructions can therefore influence
wording; it cannot introduce a citation, a link, or a path. The §4.4 laws check the
projection, and the projection is built from anchors the pipeline resolved.

The honest residue: prose in a concept owned by A can be shaped by B, and only the
`sources` list discloses that B was consulted. That is the reason the owning anchor
is listed first (§6) and the reason grounding is declared rather than inferred.

## 9. Validators

No new law. `_check_sources_shape` and `_check_carriers_agree` already handle a
multi-entry `sources` — `_expected_resources` (`validate.py:305`) maps every
anchor, not the first. The work is confirming that under test rather than
assuming it, including the dual-carrier case where the rendered list and the
projection must agree as sets.

## 10. Testing

Per CLAUDE.md, a test over a gate is worth what it catches: break each gate in
place, confirm the failure, restore with `git checkout --`.

- resolution: bare vs qualified, self-reference, unresolvable, tombstoned, over-cap ordering
- sidecar round trip, and that `load_all` ignores `_grounding/`
- drift: run connector A, change a grounding doc through connector B's run, assert A's next run pulls the owner into scope — and assert it does **not** when nothing drifted
- no-op interaction, both directions: no source change + no drift → `NoOp()`; no source change + drift → not a no-op
- emission: owning anchor first; `run_validators() == []` on a multi-source proposal
- stub does not ground, and skips the scan
- the map never reaches `commit()`: edit the map, assert `changeset.modified` stays
  empty **while the affected concept is still re-synthesized** — the two must not
  be conflated, since one is about the source and the other about the concept
- sidecar lifecycle, both deletions: empty the grounding set and assert the second
  run is a no-op rather than re-synthesizing forever; tombstone an owner, re-create
  the same `doc_id`, and assert drift is not measured against its previous life
- dedup: a document selected by BOTH the drift scan and `referrers` appears once in
  `summary.sources_changed`
- scoping: a mirror holding two systems, a run fetching one — assert the other
  system's concepts are never pulled into scope, however much they drifted

## 11. Known limits

- **One level deep.** A grounding document's own grounding is not followed.
  Transitive grounding is unbounded fan-in wearing a different hat.
- **The subject map is keyed by `doc_id`,** so a renamed `native_id` silently
  stops matching. `problems_for()`-style config validation should report map keys
  that resolve against neither the mirror nor this run.
- **Every run pays O(mirror)** once `grounds` is True (§5).
- **Changing the synthesizer is undetected drift.** Switching stub → LLM, or
  changing the model or prompt, changes what every concept was built from, and
  nothing re-synthesizes for it — `generated.by` records the change without
  forcing one. Pre-existing, but grounding makes the omission more visible,
  because sidecars written under a non-grounding synthesizer record hashes for
  grounding that never happened.
- **`generated.at` on a drift-triggered rebuild** comes from the owning document's
  `retrieved_at` (`synthesize.py:163`), which for an incremental connector may be
  the mirror's older timestamp rather than this run's. Pre-existing behaviour,
  shared with `referrers`; grounding makes it more frequent.
- **A drift rebuild can open a review request with no visible change.** When the
  owning document is re-fetched, `generated.at` moves and the diff is never empty.
  When it comes from the mirror instead — an incremental connector that did not
  re-fetch it — `retrieved_at` is unchanged, so a rebuild whose prose lands the
  same produces a byte-identical file. The no-op rule prevents an *unchanged
  source* from opening a review request; it cannot prevent this one, because the
  grounding genuinely did change.
- **Grounding does not create links.** A cited document is provenance, not
  navigation; if you also want a link, declare a relation.

## 12. Phasing

| Phase | Contents |
|---|---|
| **0.8.0** | this note: `grounded_by`, subject map, resolution, sidecar, drift check, multi-source emission |
| **0.8.x** | the deletion manifest, reusing this note's cross-run state machinery |
| later | promoting a grounding document to co-owner (many-to-one concept identity) |

The deletion manifest was previously slated first. It needs the same cross-run
state this introduces, so building this first and letting the manifest reuse it
inverts that order deliberately.

## 13. Amendments to `architecture.md` when this ships

§4.4 law 3 gains the multi-source case and the owning-anchor-first convention;
§7's "one connector per run" paragraph gains the sentence that grounding does not
weaken it — a run still fetches one system and publishes one system's concepts;
§7's no-op paragraph gains §5's restatement. No new pipeline stage, no new plugin
family, no change to the never-auto-merge rule.
