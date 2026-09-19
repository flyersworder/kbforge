---
type: design-note
title: kbforge — chunked review for oversized runs
description: A per-run admission cap that splits any change too large to review — a cold start, a new source, a widened scope, a bulk upstream edit, a grounding sweep — into chunks published one review request at a time, waits for each to merge before synthesizing the next, and lets a reviewer redo a rejected chunk.
tags: [okf, bootstrap, review, pipeline, mirror, cursor, publisher]
generated: { by: human:flyersworder, at: 2026-09-19T00:00:00Z }
status: shipped — unreleased; folded into architecture.md §7.2; this note keeps the rationale and §10
okf_version: "0.2"
---

# kbforge — chunked review for oversized runs

**The short version.** A run whose change is larger than `max_concepts` admits
one chunk of it, synthesizes and publishes only that, and commits only that to
the mirror. The rest stays visible to `diff`, so the next run finds it again.
While a non-final chunk's review request is open, runs return `Waiting` before
synthesis. A reviewer who rejects a chunk closes the request and runs
`kbforge redo`, which rolls that chunk out of the mirror so it is proposed
again under whatever taxonomy or exemplars changed in between.

## 1. Context

`design/2026-07-19-agentic-ingest-design.md` §6.1 specifies the founding import
as *chunked and iterative*: a backfill emits hundreds of concepts, an
unreviewable request gets rubber-stamped, and steering between chunks is where
messy sources get corrected. Its §9 left the partition function and the
per-chunk review flow open. This note closes both.

It generalises the problem. A cold start is only the case where an oversized
run is guaranteed. The same happens when a source is added to a running
deployment (its first run is `cursor=None`), when a query or selector widens,
when an upstream migration touches every record, and when a new grounding rule
drifts many concepts at once. So the trigger is **run size**, not "first run".

Two facts about today's pipeline shape the design:

- Successive runs accumulate into the open request on a system's sync branch
  (architecture.md §5.2). A size cap alone therefore produces one growing
  request, not chunks.
- The mirror advances on publish, not merge, so closing a request unmerged
  discards its concepts permanently. Rejecting a chunk *in order to redo it*
  has no path today.

## 2. Decisions, and why

- **An admission cap inside `run`, not a separate bootstrap driver.** A driver
  wrapping the connector covers only the cold start and must fake a fetch per
  slice. Splitting at publish time spends the whole token bill up front and
  leaves nothing to learn between chunks. The cap is a narrowing between `diff`
  and `scope`, so the pipeline order is unchanged.
- **Wait for merge between chunks.** The next chunk is synthesized only after
  the previous one's request is no longer open, so taxonomy and exemplar fixes
  reach later chunks. Cost: a slow reviewer stalls that system's pipeline,
  ordinary small updates included, until the chunk merges.
- **Redo is explicit.** Closing a request keeps its current meaning (discard).
  `kbforge redo` is the only thing that re-proposes a chunk, so a closed request
  never silently comes back.
- **Cap plus optional group key.** Coherent chunks ("all runbooks") review
  faster than arbitrary slices, and the cap still catches surprise bulk edits
  that an operator-defined partition list would not.
- **Opt-in.** With no chunking config, behaviour is byte-for-byte today's.

## 3. Configuration

A new optional `--chunking <file>` on `kbforge run`, loaded like `--grounding`:

```yaml
max_concepts: 40      # required, >= 1
group_by: category    # optional; a key of CanonicalDocument.structured
```

The model is `extra="forbid"`, so a typo'd key is an error, and it is
validated before any fetch.

## 4. Admission

After `diff`, the run's **candidates** are the added and modified documents
plus the grounding-drift documents: every concept the reviewer would read.

- **Removals are always admitted in full and do not count.** On a cold start
  or a bulk change they therefore all land in the first chunk. A
  deletion is one line to review, and `kbforge-sql` already guards bulk
  removals with `max_removed_fraction`.
- **Ordering.** Candidates are grouped by `str(structured[group_by])`; a
  document without the key goes in a trailing group. Groups are ordered by key
  and documents within a group by `doc_id`. Without `group_by` there is one
  group. The ordering is deterministic, so a re-run admits the same chunk.
- **Packing is two-phase, not one candidate set.** Added and modified
  documents are packed first, against the full cap; grounding-drift documents
  then fill whatever room remains, with the same group/doc_id ordering. This
  is required rather than a simplification: drift detection reads `by_id`,
  which already reflects this chunk's admission of added and modified
  documents, so computing drift and admission from one shared candidate set
  would be circular. One consequence follows directly: a drifted concept never
  displaces a changed one. Within each phase, a group larger than its
  remaining room is split by `doc_id` and fills that phase's capacity by
  itself. The **admitted** set is the union of both phases. Everything else is
  **backlog** (unadmitted changes) or **deferred drift**.
- **Final chunk.** A run whose backlog and deferred drift are both empty is
  final. A run under the cap is a one-chunk run and is final.

### 4.1 What the run sees

Every stage past admission sees the world *as it will be once this chunk
merges*. Nothing about an unadmitted document may reach the bundle.

- **`by_id`** is the mirror plus admitted documents, minus admitted tombstones.
  A grounding rule cannot match a backlog document until it is admitted. It
  then drifts its concept in a later chunk, which is the ordinary drift path.
- **`existing`** is the mirror's paths for this run's systems plus admitted
  additions, minus tombstones. A link to a backlog concept is dropped by
  §4.4 law 2 exactly as a link to a nonexistent one is. Law 2 is not weakened.
- **Deletion referrers** exclude *admitted* documents, not *changed* ones. A
  document that is modified but still in backlog, and links to a concept this
  chunk removes, is re-synthesized from its mirror copy with the link dropped.
  Its own modification arrives in its own chunk.
- **Arrival referrers (new).** When an added document is admitted, mirror
  documents in this run's systems whose `relations` name it, and that are not
  admitted, are re-synthesized so the link dropped in an earlier chunk comes
  back. They do not count toward the cap, and each gets a note in the review
  body: `re-synthesized to restore a link to a concept added in this chunk; its
  own source is unchanged`. A deferred grounding-drift document that is rebuilt
  as a deletion or arrival referrer is promoted out of the deferred set in the
  same run, so it carries its drift note and does not leave the chunk pending.

### 4.2 Commit

After a successful publish:

- `commit` receives admitted documents and admitted tombstones only.
  `record_first_seen` likewise.
- The sidecar loop runs over admitted documents and referrers, as it does over
  `changed_docs` today.
- **The cursor is saved only on the final chunk.** While a backlog remains, the
  next run re-fetches from the same prior cursor. This is the at-least-once
  replay that §4.2 already guarantees is harmless: admitted documents diff as
  unchanged, and admitted tombstones are no longer in the mirror, so they are
  not removals. A full-snapshot connector such as `kbforge-sql` keeps deriving
  tombstones from the prior id set, which is correct because that set is still
  the published one.

## 5. The chunk record

`<state>/chunk-<connector>-<config-digest>.json`, keyed exactly like the cursor
slot. Written only under `--chunking`, after every successful publish,
including one-chunk runs:

```json
{
  "branch_hints": ["sync/wiki"],
  "pending": true,
  "admitted": ["wiki:a", "wiki:b"],
  "mirror": {
    "<slot_key(wiki:a)>.json": null,
    "<slot_key(wiki:b)>.json": "<previous slot JSON>",
    "_grounding/<slot_key(wiki:b)>.json": "<previous sidecar JSON>",
    "_first_seen/<slot_key(wiki:a)>.json": null
  },
  "cursor": "<previous cursor slot JSON, or null>"
}
```

- `branch_hints` is the published proposal's own `branch_hint`, so the wait
  check asks about exactly the branch the chunk went to, with no second
  derivation of `sync/{system}` to drift from the synthesizer's.
- `pending` is true when the publish left a backlog.
- `mirror` maps every mirror-relative file the run writes or deletes on
  behalf of a touched document (admitted documents, tombstones, referrers, and
  drifted documents: slot, sidecar and first-seen record each) to its content
  *before* the commit. `null` means the file did not exist. `cursor` does the
  same for the cursor slot.

## 6. Waiting

First, before the fetch: if the chunk record has `pending: true`, the
pipeline asks the publisher whether a request is open on
each recorded `branch_hint`. If one is, `run` returns
**`Waiting(request, branch_hint)`**. It fetches nothing, opens nothing and
synthesizes nothing, so the no-op rule's promise (no review
request for a concept nothing changed under) is untouched and the token bill
stays bounded. The CLI prints the request and exits 0.

A merged request and a closed one both release the next chunk. Closing still
means discard.

### 6.1 The publisher seam

A new **optional**, non-abstract hook on `PublisherSpec`:

```python
@hookspec
def kbforge_open_request(self, branch_hint: str, config: dict) -> str | None:
    """Id of the open review request for this branch, or None. Read-only."""
```

It resolves the branch exactly as `kbforge_publish` does, so a configured
`branch` override is honoured. GitLab and GitHub implement it with their
existing `find_open_pr`, and `dry-run` returns `None`, so repeated dry runs step
through the chunks. A publisher without the hook, run with `--chunking`, is a
config error at startup. Degrading to appending would rebuild the unbounded
request this exists to prevent.

## 7. Redo

```bash
kbforge redo --connector … --set … --mirror … --state … --publisher … [--publish-set …]
```

It takes the same flags as `run` minus synthesis, because it needs the config
digest and the open-request check. It refuses (exit 1, a message naming the
reason, nothing touched) when:

- there is no chunk record (nothing to redo), or
- a request is open on a recorded branch ("close it first", or the redone
  chunk would append to it).

Otherwise it writes every `restore` entry back (`null` deletes), restores the
cursor slot, and deletes the record. The next `run` re-diffs and admits the same
documents. Redo is one level deep: waiting guarantees every earlier chunk was
merged or closed. Redoing a *merged* chunk is allowed. It re-proposes the chunk
as updates, which is a legitimate "regenerate".

## 8. Invariants (CLAUDE.md)

- **Pipeline order:** unchanged. Admission is a narrowing between `diff` and
  `scope`, not a stage, and it is not pluggable.
- **No-op rule:** `Waiting` returns before synthesis. A chunk synthesizes only
  documents that changed or drifted, plus referrers whose links changed.
- **Never merges:** the new hook is read-only.
- **Emit-side laws:** untouched. §4.1 exists so that law 2 holds without
  modification.
- **One bundle path, one owner:** `_scope_failures` runs over the admitted set
  plus referrers, as today. A collision between two backlog documents surfaces
  in the chunk that admits the first of them.
- **Explicit tombstones:** redo restores a tombstoned document's slot. It never
  infers a deletion.

## 9. Testing

Offline, with a fake publisher whose open-request state the test controls:

- packing: the cap, whole-group packing, oversized-group split, trailing
  missing-key group, determinism across re-runs;
- the cursor is held while backlog remains and saved on the final chunk; a held
  cursor replays without re-proposing admitted documents;
- `existing` excludes backlog concepts (a link to one is dropped), and arrival
  referrers restore it in the chunk that admits the target, with the note;
- deletion referrers that are still in backlog are rebuilt from their mirror
  copy;
- `Waiting` returns before synthesis (a synthesizer that raises is never
  called) and releases on merged or closed;
- redo round-trips the mirror, sidecars, first-seen and cursor byte for byte,
  and refuses without a record or with an open request;
- a publisher without the hook plus `--chunking` is a config error; no
  `--chunking` means no record file and today's behaviour.

Each gate gets a mutation check (mutate in place, restore with
`git checkout --`), and tests assert on messages, not only on result types.

Live (`--run-live`, the GitLab scratch repo, `max_concepts: 3`): chunk 1 opens
a request, the next run waits, merging releases chunk 2, closing plus redo
re-proposes it.

## 10. Deferred

- **Referrer volume.** Arrival referrers do not count toward the cap, so a hub
  concept linked from hundreds of documents can make one chunk large. Ordering
  by link topology would reduce this; it is not needed to be correct.
- **Concurrency.** Two runs over one mirror can interleave admission and
  commit. That is #29's run lock, and chunking neither fixes nor worsens it.
- **Arrival referrers outside chunking.** A concept whose relation names a
  document that does not exist yet loses that link under law 2, and nothing
  re-synthesizes it when the document later arrives. That gap predates this
  note and applies to unchunked incremental runs too. Arrival referrers close it
  only under `--chunking`, which keeps unchunked runs byte-for-byte unchanged;
  closing it everywhere is a separate change.
- **Per-system caps** and **a default `group_by`**: one global cap, no default
  key, until a deployment needs otherwise.
