---
type: design-note
title: kbforge — event-driven runs, and the one lock they need
description: Why a change notification may trigger a run but never supply its data, why the periodic sweep survives anyway, and the single core change event-driven triggering actually requires.
tags: [okf, deployment, scheduling, concurrency, mirror]
generated: { by: human:flyersworder, at: 2026-08-20T00:00:00Z }
status: design — only the run lock is proposed for building
okf_version: "0.2"
---

# kbforge — event-driven runs, and the one lock they need

**The short version.** kbforge owns no scheduler, so nothing stops a webhook from
invoking `kbforge run` today — `architecture.md:64` already puts the schedule in
the deployment repo. Almost everything here is therefore *already supported*. One
thing is not, and it is latent under cron too: **there is no run lock.**

## 1. A notification triggers a run; it never supplies one

The rule, and the whole reason this is cheap:

> A change notification is a **hint that it may be worth running**. It is never
> input to the run.

The run still fetches, still normalizes, still diffs against the mirror. If the
notification was wrong, duplicated, or about something irrelevant, `ChangeSet` is
empty and the no-op rule returns `NoOp()` before synthesis — one fetch, no tokens,
no review request.

So a trigger never has to be *correct*, only *frequent enough*. That is a much
weaker thing to ask of someone else's webhook.

The trap this avoids is acting on the payload: "the webhook says X was deleted, so
tombstone X." That is deriving state from a notification, which the fetch seam
already forbids — deletions are explicit tombstones, absence never implies one, and
`FetchResult.complete` exists so a partial view cannot manufacture removals (§4.2).
An event payload is a *thinner* view than a partial fetch, so it earns less trust,
not more.

## 2. The periodic sweep does not go away

Two independent reasons, either sufficient:

- **Webhooks are dropped.** Every provider loses some. A system whose only trigger
  is an event will silently stop syncing, and nothing reports it, because "no
  events" and "no changes" look identical.
- **Not every source can notify.** Confluence and Jira have webhooks; ServiceNow
  has business rules. MCP defines `notifications/resources/list_changed`, but that
  needs a persistent session, and the eleven-server survey behind `kbforge-mcp`
  found resources effectively unused in practice — a **tool-based MCP source has no
  change channel at all**.

The shape is therefore events for latency, cron for correctness. The sweep drops in
frequency; it never drops out. A deployment that deletes its cron once webhooks
work has traded a bounded staleness window for an unbounded one.

## 3. The gap: no run lock (the only thing to build)

There is no locking and no atomic write anywhere in `src/kbforge/`. `commit()` is a
plain loop of `slot.write_text(...)`, and two runs sharing a mirror will interleave.

Cron makes this unlikely — runs are spaced, and an overlap needs a run to exceed its
interval. Events make it ordinary: one bulk edit fires hundreds of notifications,
and a naive receiver starts hundreds of concurrent runs against one mirror. Beyond
the corrupted mirror, that is also hundreds of review requests.

**Proposed:** a lock file per mirror, held for the whole run. A second run for the
same mirror either waits or exits with a distinct status — never proceeds.

It belongs in core rather than in a receiver because the mirror is core's. Every
other part of triggering is genuinely the deployment's, but "two processes may not
advance one mirror at once" is an invariant about kbforge's own state, and an
invariant a deployment can forget to honour is not one.

This is worth building whether or not anyone ever wires a webhook: two overlapping
crons reach the same race by a slower road.

## 4. Coalescing, and why it stays outside

A receiver still needs a debounce window and a retry, or the lock merely converts a
stampede into a queue of near-identical runs, each opening its own review request.
That is real work and it is the deployment's: the right window depends on the
source's edit patterns, and the right retry depends on the provider's delivery
guarantees. kbforge has no view of either.

The alternative — a "trigger" plugin family alongside connectors and publishers —
buys a plugin seam for something with no kbforge-specific contract to satisfy. A
receiver is a shell script and a queue. YAGNI until a deployment shows otherwise.

## 5. Chaining, and what it does for grounding

Cross-source grounding (see the note of the same date) rebuilds a stale concept on
**the owning system's next run**. Under cron the staleness window is that system's
interval. Under events it is unbounded, because ServiceNow's webhook triggers
ServiceNow's run and nothing triggers Confluence's.

The fix needs no state and no core change: after a run of system B publishes,
trigger runs for the systems that ground in B. The operator already holds that
mapping — it is the subject map. That the grounding design's "rebuild on the
owner's next run" choice survives event-driven triggering unchanged, and gets
*better* latency from it, is a point in its favour.

## 6. Not built

| | Status |
|---|---|
| a run lock per mirror | **proposed** — the only core change here |
| webhook receiver, debounce, retry | deployment's, by design (§4) |
| a `kbforge watch` command or trigger plugin family | not built; no contract to justify a seam |
| MCP `notifications/*` as a change channel | not built; needs a persistent session, and tool-based sources have none |
| dropping the periodic sweep once events work | **never** (§2) |
