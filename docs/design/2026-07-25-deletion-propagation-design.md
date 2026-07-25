---
type: design-note
title: kbforge — Branch Accumulation and Deletion Propagation
description: Make the sync branch a function of accumulated intent rather than the current run's delta, and give the publisher a machine-readable removal list.
tags: [okf, publisher, deletion, tombstone, sync-branch]
timestamp: 2026-07-25T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge — Branch Accumulation and Deletion Propagation

**Target:** 0.4.0 · **Companion to:** [`architecture.md`](../architecture.md) §5.2

## 1. Problem

Two defects with one cause: **the sync branch is a function of the current run's
delta, when it should be a function of accumulated intent.**

### 1.1 Unmerged work is rebuilt away (shipped bug, 0.3.0)

`publish_to_forge` resolves `base` to the default branch, and `put_files` resets
the branch to it. The mirror advances after every successful publish, so the next
run's `ChangeSet` no longer contains earlier work. A run that publishes while a
previous review request is still open therefore *replaces* its contents.

Reproduced live against a real GitLab project:

| Run | Source change | Files on the sync branch afterwards |
|---|---|---|
| 1 | add Alpha, add Beta | `probe/concepts/alpha/overview.md`, `probe/concepts/beta/overview.md` |
| 2 | modify Beta only | `probe/concepts/beta/overview.md` |

Alpha was silently discarded from an open merge request, with nothing in the body
to say so. This contradicts the README's stated behaviour — that a later run
"force-updates that branch and edits the existing PR/MR rather than opening a
second one" — which readers reasonably take to mean changes accumulate into one
review request.

The bug is invisible to the offline suite and to any single live call. It needs
two runs and a deliberately unmerged review request, the same shape as the GitLab
create-vs-update bug fixed in 0.3.0.

### 1.2 Deletions are described but never performed

Detection is already complete and correct:

| Stage | State |
|---|---|
| `CanonicalDocument.deleted` | explicit tombstones exist |
| `mirror.diff` | reports `removed` only for a tombstone with a prior mirror entry |
| `FetchResult.complete` | guards against absence implying deletion |
| `synthesize.py:94` | `summary.claims_removed = sorted(changeset.removed)` |
| `summary.py:16` | renders a `## Removed` heading into the review body |
| `mirror.commit` | `slot.unlink()` retires the tombstone's mirror entry |

Only the last hop is missing. `ProposedChange` carries no machine-readable
removal list, so the publisher deletes nothing — and the review request displays
a `## Removed` section over a diff that removes nothing. The reviewer reads the
heading, approves in the 90 seconds the design promises, and the stale concept
remains in the knowledge base permanently.

For an agent-facing knowledge base this is worse than an omission. A concept for
a decommissioned service that never expires makes the agent confidently wrong.

### 1.3 Why they are one change

Deletion cannot be shipped on the current model. Run 1 deletes X correctly; run 2
changes something unrelated, resets the branch to base — where X still exists,
because the review request has not merged — and X returns. A deletion feature
layered on §1.1 would work once and then silently undo itself, which is worse
than not deleting at all.

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Base when a review request is open | the sync branch itself | work accumulates; no external state needed |
| Manual commits on the sync branch | preserved | a reviewer can correct a concept without losing it |
| Who decides deletions | the pipeline, deterministically | an LLM synthesizer must not be able to delete a file |
| Concepts linking to a deleted one | pulled into scope and re-synthesized | keeps §4.4 law 2 true in the shipped bundle |
| Removal set | intersected with what is on base | idempotent, retry-safe, and GitLab 400s on absent paths |

### 2.1 Amendment to a documented property

The README currently states: *"Manual commits pushed onto the sync branch are
discarded by the next run."* That property disappears — it is a direct
consequence of the reset that causes §1.1. kbforge no longer solely owns the
branch. The residual sharp edge, which the README must state instead: a human
edit to a concept kbforge later regenerates is overwritten by that regeneration.

## 3. Architecture

### 3.1 Publish sequence

```
pr_id = find_open_pr(branch)                        # moved ahead of put_files
base  = branch if pr_id else (cfg.base or client.default_branch())
put_files(branch, base, files, removed, message)
update_pr(pr_id, ...) if pr_id else create_pr(branch, target, ...)
```

where `target = cfg.base or client.default_branch()`.

With a review request open, the branch builds on itself. Without one — never
opened, merged, or closed unmerged — the branch is rebuilt from the default
branch and self-heals. `create_pr` always targets the real base and never the
branch, because it only runs when no review request exists.

`ForgeClient.put_files` gains a `removed: list[str]` parameter. Its contract
changes from *"reset `branch` to `base`, then apply exactly `files`"* to:

> Set `branch` to `base`, apply `files`, and delete `removed`, as one commit.
> Paths on `base` that appear in neither list are inherited.

### 3.2 Deletion authority

`ProposedChange` gains `files_removed: list[str] = []`.

The **pipeline** assigns it after synthesis returns, overwriting whatever the
synthesizer produced:

```python
proposal.files_removed = sorted(concept_path(d) for d in changeset.removed)
```

Deletion is structure, not prose — the same posture that already keeps anchors,
links, facets, type and timestamp out of the model's reach (§4.4). A synthesizer
cannot delete a file it dislikes, because its output for that field is discarded
rather than validated.

These are bundle-relative paths, matching `files` keys. `publish_to_forge` then
applies `safe_join(cfg.base_path, rel)` to `files_removed` exactly as it already
does to `files`, and for the same reason: both are derived from
connector-supplied `doc_id`s and are equally capable of naming
`../../.github/workflows/x.yml`. The publisher owns `base_path`, so the pipeline
never joins paths itself.

No partial-fetch guard is required. kbforge deletes only on explicit tombstones,
and a truncated fetch cannot fabricate one, so `FetchResult.complete == False`
needs no additional handling here.

### 3.3 Referrer expansion

Deleting concept B while concept A still links to it would leave a dangling link.
`synthesize.py:86` already drops dangling links (`links=sorted(p for p in links
if p in known)`), but only for concepts being re-synthesized. An unchanged A is
not in the proposal, so neither synthesis nor the validators ever see it, and the
bundle ships a §4.4 law 2 violation in silence.

The pipeline therefore scans the mirror for documents whose `relations` name a
removed `doc_id` and adds them to `changed_docs`:

```python
removed_ids = set(changeset.removed)
referrers = [
    d for d in load_all(mirror_path)
    if not d.deleted
    and d.doc_id not in changed
    and removed_ids.intersection(d.relations)
]
```

The mirror is the source, not `docs`: an incremental connector's fetch may not
contain the referrer, while the mirror always holds the last-known full state.
This requires a new `mirror.load_all(mirror: Path) -> list[CanonicalDocument]`.

Cost: extra synthesis for concepts whose only change is a vanished link. Under
`--synthesizer llm` that is real tokens, and it is the price of law 2 remaining
true.

### 3.4 Honest `existing`

`pipeline.py:118` builds `existing` from every fetched document, tombstones
included, so law 2 would resolve a link to a concept the same run deletes.
Tombstoned documents must be excluded:

```python
existing = frozenset(concept_path(d.doc_id) for d in docs if not d.deleted)
```

## 4. Adapters

Both adapters intersect `removed` with the paths actually present on `base`
before emitting any delete action. This is **required, not defensive**: both
forges reject deleting a path that is not there.

Each mechanism below was verified against the live scratch repositories rather
than taken from documentation, after the 0.3.0 spec asserted a GitLab behaviour
(`force: true` replacing the tree) that turned out to be false and shipped a
Critical bug:

| Adapter | Deletion mechanism | Absent path | Base listing |
|---|---|---|---|
| GitLab | `{"action": "delete", "file_path": path}` in the existing `actions[]` | **400** `A file with this name doesn't exist` | `_existing_paths()`, already present for create-vs-update |
| GitHub | tree entry with `"sha": None` alongside `mode` and `type` | **422** | new listing of the base tree, one call |
| dry-run | `unlink(missing_ok=True)` under the output directory | tolerated | not applicable |

GitHub's base-tree listing is the one genuinely new call, and its necessity is
the finding above: without it, a re-run after a partial failure — or any run
whose removal set has already been applied — fails outright with a 422. The
intersection also makes deletion idempotent, so a retry deletes what remains and
nothing more.

## 5. Error handling

Unchanged in kind: every failure remains a `PublishError` subclass reported by
the CLI as a message, and the mirror still advances only after a successful
publish, so a failed run retries the same change — now including its deletions.

One new consideration: a run whose `files` is empty but `files_removed` is not
must still publish. `ChangeSet.is_noop` already returns False when `removed` is
non-empty, so a deletion-only run reaches the publisher; the adapters must not
assume at least one file write.

## 6. Non-goals

- **Rebasing the sync branch when `main` moves** under an open review request.
  The branch may go stale; the forge's own merge handles it, and conflicts are
  unlikely because only kbforge writes concept files.
- **Detecting deletions from absence.** Unchanged: tombstones stay explicit.
- **Flagging non-kbforge commits** in the review body. Considered and declined
  as scope; the README documents the sharp edge instead.

## 7. Testing

Offline tests cover each unit: `files_removed` construction and its overwrite of
synthesizer output, `safe_join` on removal paths, referrer expansion, the
`existing` tombstone exclusion, base resolution with and without an open review
request, and each adapter's delete payload.

The two cases that decide whether this works are live, because both defects live
in the steady state and neither is visible in a single run:

1. **Accumulation** — publish A and B, modify only B, republish. A must survive.
   This is §1.1 reproduced; it fails on `main` today.
2. **Deletion without resurrection** — publish A and B, tombstone A, confirm A is
   deleted and the body shows `## Removed`; then change B and publish again, and
   confirm **A stays deleted**.

Case 2's second half is the resurrection trap of §1.3. Both extend
`tests/test_forge_live.py` and run against the existing scratch repos.

## 8. Build sequence

Each step leaves the suite green.

1. `mirror.load_all()` with tests.
2. `ProposedChange.files_removed`; pipeline assigns it, excludes tombstones from
   `existing`, and expands referrers. Publishers still ignore the field.
3. `ForgeClient.put_files` grows `removed`; `publish_to_forge` reorders so
   `find_open_pr` precedes it and base resolves to the branch when open.
4. GitLab adapter: delete actions, filtered by `_existing_paths`.
5. GitHub adapter: base-tree listing and `sha: None` entries.
6. dry-run adapter: unlink removed paths.
7. Live tests for both scenarios in §7.
8. Docs: README (drop both stale bullets, state the new sharp edge),
   architecture.md §5.2, CHANGELOG.
