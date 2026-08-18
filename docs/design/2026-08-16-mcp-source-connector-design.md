---
type: design-note
title: kbforge — MCP as a Source Transport (what remains deferred)
description: The parts of the MCP source connector that 0.7.0 did not build — the deletion manifest, the cursor-identity collision that blocks it, the path collisions the fetch-side law does not yet close, and the untaken option for selectors whose ids arrive as JSON-in-text.
tags: [okf, mcp, connectors, agentic-fetch, selector, producer]
generated: { by: human:flyersworder, at: 2026-08-18T00:00:00Z }
status: partially shipped in 0.7.0 — §9–§10 remain deferred to 0.8.0
okf_version: "0.2"
---

# kbforge — MCP as a Source Transport (what remains deferred)

**Status:** §1–§8 **shipped in 0.7.0** as `packages/kbforge-mcp/`; their design now
lives in [`../architecture.md`](../architecture.md) §4.1 and this note no longer
restates it. What is below was deliberately *not* built, and is kept here because
the reasoning is not recoverable from the code — there is no code.
**Builds on:** [`2026-07-19-agentic-ingest-design.md`](2026-07-19-agentic-ingest-design.md) §3.

## What shipped, and where the rationale went

| Was | Now |
|---|---|
| §1 the config-only bet, and its measured limit | `architecture.md` §4.1 |
| §2 selector / reader, the eleven-server `resources/list` survey, read-only as a structural tool set | `architecture.md` §4.1 |
| §3 a separate distribution, and why never core | `architecture.md` §4.1 |
| §4–§7 shape, response mapping, transport, error handling | the code, and `architecture.md` §4.1 for the parts the code cannot state |
| §8's targets and three test layers | `packages/kbforge-mcp/tests/` — the layers are `fake_server.py` + `test_client.py` (in-process), `test_stdio.py` (subprocess), `test_live.py` (`--run-live`); why deepwiki was rejected is in `test_live.py`'s docstring |
| §8's mutation tests | performed against 0.7.0 at build time; they leave no artefact in the repo by design (each gate is broken in place and restored), so the standing rule is CLAUDE.md's "Verifying a gate" |
| §8's `mcp-server-git` negative fixture | **not built** — still here, §10.4 |
| the `assert_stability` clock blind spot (was §10.3) | `architecture.md` §4.3 |

One correction the build made to the original note, recorded because the note
asserted otherwise: GitHub is a live-test target for its **tier-1 reader**, which
nothing else exercises. Its `search_code` is *not* usable as a selector — see
§10.3 — so the live test drives it from a configured id list.

## 9. Deletion: why it needs a manifest, and why it is deferred

Connectors are mirror-blind by design; core never derives deletions (`mirror.diff`
marks `removed` only on an explicit `deleted=True`). So neither side can notice a
document vanished: the connector knows what it fetched but not what it fetched
last time, and core knows both but will not act on absence. A connector-owned
`(native_id → content_hash)` manifest in `Cursor.payload` is the only way to close
that gap.

Deletion support would then be a property of the selector, not the source:

| Selector | `complete` | Tombstones |
|---|---|---|
| a configured id list (`static_ids`) | `True` | **yes** — `manifest − current` |
| a query selector | `False` | never |
| an agentic selector | `False` | never |

The configured id list is the enumerating one precisely because it *is* the
scope: a list an operator wrote down is complete by construction, which is what
licenses `complete=True` and therefore tombstones.

The limitation to state plainly when this lands: **an agentic or RAG-driven sync
can add and update concepts but can never remove one.** Stale concepts accumulate
until an enumerating pass runs — the fail-safe direction, since a stale concept is
visible in review and a wrongly-deleted one is silent data loss.

**All of this is deferred to 0.8.0.** 0.7.0 needs no manifest and no cursor: like
`local_files`, it re-selects every run and lets the mirror diff do the work, which
is why §10.1 did not block the connector phase. It ships
`Cursor(connector="mcp", payload={})` and reads nothing back, which is
forward-compatible with either resolution below.

## 10. Deferred to 0.8.0

### 10.1 Cursor identity — blocks the manifest phase, not the connector

The pipeline is asymmetric about cursor identity: it loads with
`_load_cursor(state_path, info.name)` and saves with
`_cursor_slot(state_dir, cursor.connector)`. The two agree only when
`Cursor.connector == ConnectorInfo.name`. Both core connectors satisfy this with a
module constant.

But `kbforge_connector_info()` takes no config, so a generic connector's name is
static while its `system` is per-instance. Both branches fail silently:

- `Cursor(connector=config["system"])` → written to one slot, read from another.
  The manifest never returns, `manifest − current` is always empty, the static
  selector never tombstones anything — and an offline test that hand-seeds a
  cursor still passes.
- `Cursor(connector="mcp")` → every MCP source in a deployment shares one
  manifest. Sharing a `--state` directory is the natural deployment (sharing a
  mirror is safe, since `doc_id`s are `system:`-prefixed), so source B's
  enumerating run replaces A's memory and A's deletions are then never propagated.

Resolving this needs either a core change (config-dependent connector identity, or
a cursor-key parameter on `run()`) or namespacing the manifest inside
`Cursor.payload` under the configured `system`. The second is enforceable without
touching core and remains the front-runner; it has not been designed.

### 10.2 Manifest scope, and the collisions the slug does not close

- **Scope.** Running an enumerating and a query selector on one source is unsafe:
  if the query surfaces a document outside the static list, the next enumerating
  run computes `manifest − current` and tombstones it — a **spurious** removal,
  the exact data loss §9 claims to avoid. Needs either a partitioned-by-selector
  manifest or a stated rule that the static selector's scope is a superset of
  every other selector's.
- **Suffix collision.** `concept_path` collapses `x:policy` and `x:policy.md` onto
  one path; the fetch-side law checks `doc_id` uniqueness and does not fire, and
  `_check_projection_coherence` compares already-collapsed sets. The `native_id`
  slug removes the extension, which makes this reachable *within* one source
  rather than only across two. The general fix is a core-side `concept_path`
  injectivity check.
- **Blank `doc_id`.** `doc_id=""` passes `assert_fetch_contract`'s uniqueness
  check and `concept_path("")` renders `concepts//overview.md`, normalizing onto
  `concepts/overview.md` and silently colliding with a root-level concept. The
  slug's non-empty constraint closes this connector-side; the core-side law does
  not.

Also smaller and unresolved: the manifest persists only on the `Published` path,
so merge-vs-replace semantics must be specified against a model where a no-op
run's manifest is discarded.

### 10.3 Selectors whose ids arrive as JSON inside a text block

The protocol-first tiers take a selector's ids from resource links or from
`structuredContent`, and refuse anything else rather than guess. GitHub's
`search_code` — measured, not assumed — returns machine-readable JSON *inside a
`TextContent` block* and declares no `structuredContent`. It is therefore refused,
and a server that is perfectly machine-readable is nonetheless unmappable as a
selector. This is the first real dent in the note's original claim that a new
MCP-backed source is configuration; it is not an implementation bug, the tiers
behave as specified.

The option, deliberately untaken in 0.7.0: an **opt-in** `json_text: true` on the
select spec that parses a designated text block as JSON before applying the
existing `ids` mapping. Opt-in matters — as a default it would be exactly the
prose heuristic the tiers exist to keep out, since "the text parses as JSON" is a
guess about a server's intent, whereas a config key is an operator's assertion
about a server they know. It was not built because it arrived from live testing
after the plan was fixed, and adding an unreviewed config key late is how mapping
surfaces grow by accident. Until it exists, such a source is configured with an
explicit id list, which means enumerating the corpus by hand.

### 10.4 The negative fixture that was planned and not built

`mcp-server-git` was to be carried as a **fixture for the limit, not a passing
gate**: five of its twelve tools mutate, and it has no read-by-id at all —
`git_show(revision)` returns a commit's patch, not a document — so it fails the
reader requirement and `kbforge_validate_config` has nothing valid to accept. The
test would have asserted the validation refusal only.

What it was there to make concrete is the residue that no layer closes: were
someone to configure `read.tool: git_commit`, kbforge would call it, and only an
explicit `read_only_hint: False` on that tool would stop it. Whether the server
sets that annotation was never checked, so the fixture would have *documented* the
gap rather than closed it. The residue itself is stated in `architecture.md` §4.1
and does not depend on this fixture existing; what is missing is a test that makes
a reader feel it. Cheap to add whenever someone wants it.

## 11. Phasing

| Phase | Contents | Gate |
|---|---|---|
| **0.6.0** ✅ | fetch-side law (`architecture.md` §4.2, §4.4, §7) | none — shipped |
| **0.7.0** ✅ | `kbforge-mcp`: client, two-tool set, tiered mapping, static + query selectors, AWS docs + GitHub live tests | none |
| **0.8.0** | manifest cursor, tombstones, merge-vs-replace | §10.1, §10.2 |
| later | an agentic selector | bounds/budget/tool set from agentic-ingest §9 |
| later | `json_text` selectors | §10.3 |
| later | the `mcp-server-git` negative fixture | §10.4 |

## 12. Amendments to `architecture.md` — applied

Applied when 0.7.0 shipped: §4.1's "future convenience" note became the shipped
package, the selector/reader split, the structural two-tool set, and the mapping
limit §10.3 records; §4.2's manifest sentence was re-keyed from `doc_id` to
`native_id`; §4.3 gained the `assert_stability` blind spot. No new pipeline stage,
no new plugin family, no change to the no-op or never-auto-merge rules.
