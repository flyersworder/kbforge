---
type: design-note
title: kbforge — The Fetch-Side Law
description: A core validator over normalize output that makes FetchResult.complete load-bearing, rejects colliding doc_ids, and refuses records that cannot be cited — closing an invariant the codebase currently holds only by absence of mechanism.
tags: [okf, fetch, validators, invariants, tombstones, producer]
timestamp: 2026-08-16T00:00:00Z
status: draft
okf_version: "0.2"
---

# kbforge — The Fetch-Side Law

**Status:** Draft v0.1 · **Amends:** [`../architecture.md`](../architecture.md)
§4.2 (incremental contract) and §7 (pipeline pseudocode)
**Prerequisite for:** [`2026-08-16-mcp-source-connector-design.md`](2026-08-16-mcp-source-connector-design.md),
but independent of it — this note contains no MCP.

## 1. Problem

kbforge has emit-side laws (§4.4) and canonicalization laws (§4.3). It has no
**fetch-side** law. Three consequences, all currently latent:

**`FetchResult.complete` is decorative.** A repo-wide grep finds it at
`models.py:113` (the definition), `local_files.py:123` (a comment observing it is
unconsumed), and one test asserting its default. Nothing reads it. `pipeline.run`
calls `diff(mirror_path, docs)` with two arguments; `mirror.diff` marks `removed`
only when a connector hands it `deleted=True`.

CLAUDE.md states the invariant as *"`FetchResult.complete` exists so a
rate-limited partial fetch can't manufacture removals."* That is true today only
**vacuously** — nothing manufactures removals at all, because deletions are
entirely connector-emitted. The guarantee holds by absence of mechanism, not by
enforcement, and it stops being safe the moment any connector derives deletions
from what a fetch did or did not return.

Note that `architecture.md:689` already documents the *intended* behaviour —
`changeset = mirror_and_diff(mirror, docs, result.complete)` — so the spec claims
a consumer the code does not have. This note reconciles them by adding the
consumer, not by editing the promise away.

**Colliding `doc_id`s lose a document silently.** Two records normalizing to the
same `doc_id` both land in `changeset.added` (`mirror.diff` never mutates, so
`prev is None` twice), both survive into `changed_docs`, and `synthesize.assemble`
then collapses them onto one `concept_path` with last-write-wins. Mirror and
bundle agree afterwards, so nothing looks broken: one document is simply absent
from the knowledge base, and `summary.claims_added` carries a doubled entry.

**The worse variant is not caught by any validator.** If one of the colliding
records carries `deleted=True`, the id lands in `changeset.removed` *and* in
`added`/`modified`, so the same path appears in `proposal.files` **and**
`proposal.files_removed`. `validate._check_projection_coherence` binds
`files`↔`concepts` and never inspects `files_removed`, so an internally
contradictory proposal — add this path, delete this path — reaches the publisher
with `run_validators() == []`.

## 2. The law

One new validator in `canonical.py`, beside `assert_stability`, and one call site
in `pipeline.run` immediately after the existing stability law:

```python
docs = connector.kbforge_normalize(result.records)
assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1
assert_fetch_contract(docs, complete=result.complete)          # fetch-side law
changeset = diff(mirror_path, docs)
```

It runs on `normalize` output rather than on `RawRecord`s for two reasons:
`doc_id` is what the mirror keys on, and tombstones only exist post-normalize —
`RawRecord` has no `deleted` field (`models.py:100-107`).

Placement does not touch the fixed pipeline order. It is a validator between two
existing stages, in the same position and of the same kind as `assert_stability`.

### 2.1 Three checks, with their messages fixed

The messages are specified here, not left to the implementer, because CLAUDE.md
requires tests to assert on failure *messages* rather than on a category — a test
that only asserts `pytest.raises(FetchContractError)` passes on whichever of the
three checks happened to fire, which is exactly the failure the convention exists
to prevent.

| Check | Message |
|---|---|
| `doc_id` unique across `docs` | `duplicate doc_id in fetch output: {id}` |
| `anchor.native_id` non-blank | `record has no native_id: doc_id={id}` |
| `complete=False` ⟹ no `deleted=True` | `incomplete fetch cannot emit a tombstone: {id}` |

Blankness follows `validate._blank` — Unicode `Cf`/`Zs`/`Cc`-aware, not
`str.strip()` — so a `native_id` of `U+200B` is rejected, matching how the
emit-side laws already treat blankness.

### 2.2 What it deliberately does not check

**Verbatim-ness.** Core has no independent access to the source, so it cannot
distinguish a returned document from an agent's summary of one. When an agentic
fetch eventually exists, this law closes the *identity* half of
retriever-not-extractor; the *verbatim* half remains contract. That limit belongs
in the docstring, in the same register as §4.4's honest accounting of its own
reduced-strength laws.

**`normalize` purity with respect to the clock.** This is worth stating because
the existing gate is weaker than it looks:

> `content_hash` excludes the anchor by design — *"`retrieved_at` is volatile
> (§4.3 law 2) and the anchor's own content_hash would be circular"*
> (`canonical.py:16`). So `assert_stability` compares content hashes that omit
> `retrieved_at`, and a `datetime.now()` called inside `normalize` produces
> identical hashes on both passes and passes the gate.

Both shipped connectors take `retrieved_at` from the filesystem or from git, so
nothing exercises this today. It is recorded here because the law will be read as
"the fetch-side gate" and a reader should not infer coverage it does not have.
Closing it is out of scope (§5).

### 2.3 Failure surfacing

`assert_fetch_contract` raises `FetchContractError(RuntimeError)`, mirroring
`StabilityError`.

`__main__.main` currently catches only `ConfigError`, `PublishError`, and
`PathError` (`__main__.py:191-194`), so `StabilityError` already escapes as a
traceback. Since this law is a **new failure mode for existing third-party
connectors**, a traceback is the wrong first impression: both `StabilityError`
and `FetchContractError` are added to the caught set and reported as a
message with exit 2, like every other operator-facing failure.

That is a small, deliberate scope addition — fixing the surfacing only for the
new law would leave the older one worse, for no reason.

## 3. The projection-coherence gap

Separately from the law, and in the same release because it closes the worse half
of the same defect: `validate._check_projection_coherence` gains a check that
`proposal.files` and `proposal.files_removed` are disjoint.

- law slug: `projection-coherence` (existing)
- message: `path is both written and removed: {path}`

The law in §2 prevents the connector-side cause (duplicate `doc_id`s, one
tombstoned). This check prevents the *emit-side symptom* regardless of cause, and
it belongs on the emit side because that is where the two path sets exist. Both
are cheap; shipping only one leaves a contradictory proposal reachable by any
future path that produces one.

## 4. Testing

The existing connectors pass unchanged, and the plan verifies rather than assumes
it: `local_files` keys `doc_id` on `rel` from `sorted(root.rglob("*.md"))`
(injective), `git_commits` keys on `%H` within one `git log` range (likewise);
both return the default `complete=True`; neither emits a tombstone. No test in the
suite currently uses `complete=False`, a blank `native_id`, or duplicate
`doc_id`s.

**Unit tests** — one per check, asserting on the §2.1 message, not the exception
type alone.

**Mutation tests, which are the point.** CLAUDE.md's rule is *break the thing the
gate guards and confirm it fails* — constructing a bad input and asserting the law
raises only verifies the law's own body. The real mutations, applied **in place**
and restored with `git checkout --` (never in a `cp -R`, whose `.venv` resolves
the package to the original source):

1. Delete the `assert_fetch_contract` call from `pipeline.run` → a test must fail.
2. Delete the `files`/`files_removed` disjointness check from `validate` → a test
   must fail.
3. Construct the end-to-end defect through a fake connector — two records
   normalizing to one `doc_id`, one tombstoned — and confirm that **without** the
   law the run produces a proposal whose `files` and `files_removed` share a path
   and `run_validators()` returns `[]`. This is the test that proves the defect
   was real, and it is written first.

**Full suite green** at 275 passed / 6 skipped or better, and `prek run
--all-files` clean.

## 5. Scope

**In (0.6.0):** `assert_fetch_contract` with three checks and fixed messages; the
`FetchContractError`/`StabilityError` CLI surfacing; the `files`/`files_removed`
disjointness check; tests including the three mutations.

**Out:**

- **Anything MCP.** This note is a prerequisite for
  [the MCP connector](2026-08-16-mcp-source-connector-design.md) but shares no code
  with it.
- **Making `normalize` clock-purity checkable** (§2.2). It needs either a second
  hash covering the anchor or a fetch-side clock injection, and neither is
  motivated until a connector without a natural `retrieved_at` exists.
- **`concept_path` collision from *distinct* `doc_id`s.** `concept_path` does
  `native.removesuffix(".md").strip("/")` (`synthesize.py:39-43`), so `x:policy`
  and `x:policy.md` render to one path while remaining distinct `doc_id`s — the
  §2 law would not fire, and `_check_projection_coherence` compares the already
  collapsed sets. Unreachable for both shipped connectors (every `local_files`
  id ends in `.md`, every `git_commits` id is a SHA); reachable as soon as
  `native_id`s are server-controlled. It is therefore the MCP note's problem, and
  is recorded there.

**Version:** minor, not patch. A third-party connector emitting duplicate
`doc_id`s now fails loudly where it previously dropped a document silently. That
is the desirable direction and it is still a new failure mode.

## 6. Amendments to `architecture.md`

- **§4.2** — record that `FetchResult.complete` is enforced: an incomplete fetch
  may not carry a tombstone.
- **§7** — the pseudocode at line 689 reads
  `mirror_and_diff(mirror, docs, result.complete)`, which describes a consumer the
  code does not have. Replace it with the real call plus the law, so the document
  names one consumer of `complete` rather than two.
- **§4.4** — note the new `projection-coherence` message alongside the existing
  path-set binding.

No new pipeline stage, no new plugin family, no change to the no-op or
never-auto-merge rules.
