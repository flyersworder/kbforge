---
type: design-note
title: kbforge — the project file, and the commands around it
description: One kbforge.yaml per knowledge base, run as one process under one mirror lock, so the cross-run rules a deployment has to remember today become structure; plus plan, status, reset and init.
tags: [okf, cli, usability, deployment, mirror, concurrency]
generated: { by: human:flyersworder, at: 2026-10-29T00:00:00+00:00 }
status: design — not built
okf_version: "0.2"
---

# kbforge — the project file, and the commands around it

**The short version.** kbforge's guarantees are structural *inside one run* and
held by convention *across runs*. A real deployment is N `kbforge run`
invocations — one per source system — that must share one `--mirror`, all pass
the same `--links`, keep each connector "always chunked or never", and never
overlap. Each of those is a sentence in the README that an operator has to
remember, and the CLAUDE.md principle is that a guarantee you can break by
forgetting a flag is not one. Separately, the first contact is steep: three
state-directory flags on every command, three `--*-set` flag families with
YAML-on-the-command-line quoting traps, and recovery by deleting files by hand.

This spec adds a project file, `kbforge.yaml`, that declares one knowledge base:
its sources, its synthesizer, its publisher, its links and grounding. `kbforge
run` with no `--connector` reads it and runs every source **in one process,
under one mirror lock**, calling the same `pipeline.run()` it calls today. Four
commands name what an operator is trying to do: `init`, `plan`, `status`,
`reset`.

Nothing about the pipeline changes. The fixed order, the no-op rule, never
merging, the §4.4 laws and the dual-carrier checks are untouched, and the
project file has no key that could reach them.

## 1. Who this is for

Two people, and the design must serve both (my call — the user did not pick
one):

- **The deployment operator** — e.g. one knowledge base per application, built
  from a SQL view, an internal MCP document server and a LiteLLM gateway, run
  from CI. Today they maintain a shell script of N long command lines that must
  agree with each other. Success: the script becomes `kbforge run`, and the
  agreements are no longer theirs to keep.
- **The first-time evaluator** — has a folder of markdown and ten minutes.
  Success: `kbforge init && kbforge plan && kbforge run` shows them what kbforge
  would propose and why, before they have configured an LLM or a forge.

## 2. Goals, non-goals, anti-goals

**Goals**

1. One file declares a knowledge base; `kbforge run` runs all of it.
2. The four cross-run conventions become structure (§5).
3. State lives in one directory by default; no state flag is required.
4. `plan` shows what a run would propose without changing anything.
5. `reset <source>` replaces the hand-deletion recovery procedure.
6. The existing flag-based CLI keeps working unchanged, so no deployment breaks.

**Non-goals** (each is real, and each is its own change)

- System-qualified bundle paths (#42).
- Keying cursor slots by `system` instead of a config digest. Slots keep today's
  `cursor-<connector>-<digest>.json` naming, which is what makes migration free
  (§8). Editing a source's config still starts it on a fresh cursor; what
  changes is that `run` and `plan` now *say so* (§6.2).
- Atomic mirror writes. The lock removes overlap between kbforge runs; a crash
  mid-`commit` can still leave a torn slot, as today.
- Scheduling, watch mode, webhooks (see `2026-08-20-event-driven-runs-design.md`).
- Deprecating the flag CLI. It stays documented, below the project form.
- Auto-filling a connector's `system` from the source name. `kbforge-sql` and
  `kbforge-mcp` sources still write `system:` explicitly.
- Any change to the companion distributions.

**Anti-goals** — this would be a failure even if it works:

- A project-file key that reorders, skips or replaces a pipeline stage, makes
  the no-op rule conditional, or enables merging. There is no `stages:`,
  `hooks:`, `auto_merge:` or `force:` key, and `extra="forbid"` keeps one from
  being smuggled in as a typo that "does nothing".
- A second pipeline. Project mode is a loop over today's `run()` / `redo()`; it
  must not reimplement fetch, diff, scope, synthesis, validation or publish.
  `plan` is the one new pipeline entry point and it reuses the stage functions
  (§6.3).
- A `plan` that writes anything — mirror, cursor, chunk record, sidecar, `out/`
  — or calls a model.
- Secrets in the file. Credentials stay in env vars named by `*_env` keys, as
  today.

## 3. The file

`kbforge.yaml`, found in the current directory, or named with `--project PATH`
(my call: no upward search — a CI job and a human should never resolve
different files from different working directories). Project-mode commands
`chdir` to the file's directory once, in the CLI layer, before anything else
(my call). So every relative path — `state_dir`, `links`, `grounding`, and
connector config values such as `local_files`' `path` — means "relative to
`kbforge.yaml`", while kbforge itself still never rewrites connector config.
That matters: config is hashed into the cursor and chunk slot names
(`_instance_key`, `pipeline.py:130`). Rewriting paths to absolute would tie
every slot to the checkout location, so a CI runner in a different workspace
would start on a fresh cursor, and it would break §8. The digest is taken over
the config as written.

```yaml
# kbforge.yaml — one knowledge base
state_dir: .kbforge                 # optional; default .kbforge next to this file

synthesizer:                        # optional; default {name: stub}
  name: llm                         # stub | llm | describe
  model: deepseek/deepseek-v4-flash
  instructions: |
    Write for application managers. Prefer the product's own terms.

publisher:                          # optional; default {name: dry-run}
  name: github
  repo: acme/ev-traction-kb
  token_env: GITHUB_TOKEN

links: links.yaml                   # optional; a path, or the mapping inline
grounding: grounding.yaml           # optional; a path, or the mapping inline

sources:                            # required, at least one; runs in this order
  products:
    connector: sql
    config:
      system: products
      url_env: DENODO_URL
      query: SELECT product_id, product_name, description FROM iv_product
      id: [product_id]
      title: product_name
      text: description
  docs:
    connector: mcp
    config: { system: docs, ... }
    chunking: { max_concepts: 40, group_by: family }
  handbook:
    connector: local_files
    config: { path: ./handbook }
    synthesizer: { name: describe, model: deepseek/deepseek-v4-flash }
```

Schema (Pydantic, `extra="forbid"` at every level, error messages carry the
YAML path, e.g. `sources.docs.chunking.max_concepts: must be at least 1`):

| Key | Shape | Maps to today |
|---|---|---|
| `state_dir` | path | parent of `--mirror` / `--state` / `--out` (§4) |
| `synthesizer` | `{name, ...settings}` | `--synthesizer` + `--llm-set` |
| `publisher` | `{name, ...settings}` | `--publisher` + `--publish-set` |
| `links` | path or mapping | `--links` |
| `grounding` | path or mapping | `--grounding` |
| `sources.<name>.connector` | entry-point name | `--connector` |
| `sources.<name>.config` | mapping | `--set` |
| `sources.<name>.chunking` | `{max_concepts, group_by?}` | `--chunking` |
| `sources.<name>.synthesizer` | as top level | per-source override |
| `sources.<name>.publisher` | as top level | per-source override |

- **Source names** match `^[a-z][a-z0-9_-]*$`. They are labels for the CLI
  (`kbforge run docs`) and for output; they are not doc_id prefixes or branch
  names — those stay the connector's `system`, exactly as today.
- **Settings are the existing config objects.** `synthesizer` minus `name` is
  passed to `LLMConfig` / `DescribeConfig` as `--llm-set` is today; `publisher`
  minus `name` is today's `publish_config`; `config` is today's connector
  config. The YAML file is parsed once with `yaml.safe_load`, so the
  `--llm-set` quoting checks (`_parse_settings`) do not apply to it — the value
  is already a typed YAML scalar, which is the point.
- **`dry-run`'s `out_dir`** defaults to `<state_dir>/out`, as the CLI defaults it
  to `--out`.
- **Overrides are whole-object replacement**, not deep merge (my call: a
  per-source `synthesizer` that silently inherits half its keys from the top
  level is the kind of thing nobody can read off the file).
- **`links` and `grounding` are project-level only.** There is deliberately no
  per-source form; §5 says why.

## 4. State layout

```
<state_dir>/            # default .kbforge/ beside kbforge.yaml
  mirror/               # today's --mirror (shared across sources, as §7.1 requires)
  state/                # today's --state (cursor-*, chunk-* slots)
  out/                  # dry-run publisher output
  run.lock              # §5.1
  state/sources.json    # project-layer record, below
```

`sources.json` is the one new state file, written only by project mode. Per
source name it holds the connector name and slot digest the source last ran
with (so `reset` still works for a name removed from the file), the
`anchor.system` values its last run fetched, and the time of its last
`Published`. Every new command needs this record: §5.3 checks ownership
against it, §6.2 uses it for the config-change notice, §6.4 for "last
published", and §6.5 to know which mirror slots are the source's. `Cursor`
and `ChunkRecord` do not change shape. Both are `extra="forbid"`, so adding a
field would make a newer state directory unreadable to an older kbforge.

The mirror and state must persist between runs — in CI that means a cache or a
committed directory, exactly as today. `init` says so; nothing else changes.

## 5. Conventions that become structure

| Today's rule (README / architecture.md) | Under a project file |
|---|---|
| All runs that ground on each other must share one `--mirror` (§7.1) | There is one mirror per project; a source cannot name another. |
| Every run sharing a mirror must pass the same `--links` (§7.4) | `links` exists only at project level; every source's `run()` gets the same `LinksConfig`. Same for `grounding`. |
| Runs on one mirror must not overlap (event-driven-runs note §3) | §5.1: one lock per mirror, taken by every mode. |
| Keep a connector instance always chunked or never (§7.2) | §5.2: an unchunked publish retires the stale chunk record. |
| (unstated today) Two runs must not emit one `system` | §5.3: a system belongs to exactly one source. |

### 5.1 The run lock

`run`, `redo`, `plan` and `reset` take an exclusive, non-blocking lock on
`<mirror>/../run.lock` — in project mode that is `<state_dir>/run.lock`; in flag
mode, a `run.lock` beside the `--mirror` directory (my call: beside, not inside,
so `load_all`'s `*.json` glob and the `_*/` sidecar dirs never see it). If the
lock is held the command exits **3** with `Locked: another kbforge run holds
<path>; nothing was read or written.` It is acquired before anything is read
and held until the command finishes, for all sources in a project run. It must
be released by the OS when the process dies (an advisory `flock`-style lock,
not a create-if-absent file); the portable implementation is the builder's
call. Flag-mode runs take it too — the lock belongs to the mirror, not to the
project file, and a flag-mode run racing a project run on the same mirror is
exactly the bug.

This closes the "run lock" half of roadmap item 1. Atomic writes stay open.

### 5.2 Retiring a stale chunk record

Today an unchunked run on a connector instance that once ran chunked leaves the
old `chunk-*` record behind, and a later `redo` rolls back to it. Change in
`pipeline.run()` (so flag mode gets it too): when a run with `chunking is None`
returns `Published`, it deletes that instance's chunk slot. A `NoOp`, `Waiting`
or `Aborted` run leaves it alone — nothing advanced. The §7.2 sentence "Do not
mix chunked and unchunked runs … and then redo" is then deleted rather than
kept as a warning.

### 5.3 One system, one source

`local_files` and `git_commits` hard-code their `system`
(`local_files.py:25`, `git_commits.py:28`). Two `local_files` sources would
therefore emit doc_ids in one namespace and share one `sync/local_files`
branch, and `reset` (§6.5), which selects mirror slots by system, would wipe
both. Today nothing stops this across invocations. Under a project file it is
refused (my call).

A system is only known after a fetch, so the check runs there. `run()` gains
optional `foreign_systems: frozenset[str]` and `owners` (system to owning
source name, for the message) parameters, defaulting to empty, so flag mode is
unchanged. It is checked immediately after the fetch contract
(`pipeline.py:320`), before the diff — not beside `_scope_failures`, since the
two NoOp gates return before that point — and any fetched
document whose `anchor.system` is in that set yields `Aborted`:
`[source-ownership] system 'local_files' is already owned by source
'handbook'`. In project mode, `foreign_systems` is the union of the systems
`sources.json` records for every *other* source name, plus the systems any
earlier source fetched in the same invocation. An aborted source writes
nothing to `sources.json`.

The practical consequence for a user: point one `local_files` source at a
common parent directory instead of declaring two. `init`'s comments say so.

## 6. Commands

Mode is decided by `--connector`: present → today's flag CLI, byte-for-byte
unchanged in behavior and output (plus the lock); absent → project mode. Giving
`--connector` together with `--project`, or a source name together with
`--connector`, exits 2.

| Command | Does |
|---|---|
| `kbforge init [PATH]` | writes `kbforge.yaml` |
| `kbforge plan [SOURCE...]` | shows what `run` would propose; writes nothing |
| `kbforge run [SOURCE...]` | runs the sources, in file order |
| `kbforge status` | local state + open review requests; no fetch |
| `kbforge redo SOURCE` | today's `redo`, for one source |
| `kbforge reset SOURCE` | forgets one source's local state |
| `kbforge list` | unchanged |

`[SOURCE...]` defaults to every source; an unknown name exits 2 listing the
known ones.

### 6.1 Validation before anything runs

Project-mode `run`, `plan` and `redo` load and validate **the whole project**
before touching any source: the schema; every `connector` and `publisher` name
resolves; `kbforge_validate_config` for every selected source;
`kbforge_validate_publish_config` for every publisher in use; LLM config and
`validate_env()` for every synthesizer in use; `problems_for(grounding)`,
`links_problems(links)`. Any problem → print all of them, exit 2, nothing runs.
(Today each of these is checked per invocation; a project should not publish
source 1 and then discover source 3 is misconfigured.)

### 6.2 `run`

For each selected source in file order, build exactly the arguments today's
`main()` builds from flags and call `pipeline.run()`. Print one line per source
with today's wording, prefixed by the source name:

```
products  NoOp: no change detected; no MR opened.
docs      Waiting: review request !12 is still open; the next chunk follows once it is merged or closed.
handbook  Published: https://github.com/acme/ev-traction-kb/pull/31
```

- A source that fails (exit-1 or exit-2 class error from today's handlers,
  `Aborted` included) does not stop the others: sources are separate review
  requests on separate branches. Exit code: 0 if every source ended `Published`,
  `NoOp` or `Waiting`; 1 if any failed or aborted; 2 only for §6.1 failures.
- **Order.** File order. Put sources that others ground on first, so an owner's
  run in the same invocation already sees its grounding documents' new hashes
  (otherwise the drift lands one run later — still correct, just later). My
  call: no automatic ordering; `init`'s comments and the README say it once.
- **Config-change notice.** Before calling `run()`, if `sources.json` records a
  different slot digest for this source than its current config produces,
  print `products  note: config changed since the last run; starting from a
  fresh cursor (kbforge reset products clears the old state).` The behavior is
  today's; the only change is that it is no longer silent. The old slots stay
  on disk, as they do today.

### 6.3 `plan`

The question it answers: *if I ran now, what would each source propose, and
why?* It runs exactly what `run()` runs up to synthesis, in `run()`'s order:
the chunk `Waiting` check before the fetch, then fetch (with the stored
cursor) → normalize → `assert_stability` → `assert_fetch_contract` → `diff` →
grounding and link drift → chunk admission and the post-diff `Waiting` check →
referrer and arrival widening → `_scope_failures` and the §5.3 ownership
check. That covers `pipeline.py:300–652`. It stops before the grounding map is
built: no synthesis, no model call, no validate, no publish, and no write of
any kind. Output per source:

```
products  2 added, 1 modified, 0 removed, 3 drifted (grounding) → would publish 6 concepts
            + products:EG120          added
            ~ products:EG118          modified
            ↻ products:EG100          grounding drift: web:bosch-sic-page changed
docs      review request !12 open → would wait
handbook  no change → NoOp
```

Requirements:

- It must reach its verdict through the **same functions** `run()` uses, not a
  copy. The likely shape is extracting the pre-synthesis half of `run()` into a
  function both call, returning a value `plan` prints and `run` continues from.
  That refactor is the riskiest part of this spec: `run()` is ~600 lines with
  ordering that encodes real invariants (the no-op rule, the chunk `Waiting`
  checks before and after the diff, referrer widening). The builder must show,
  by test, that `run()`'s results are identical before and after the extraction
  across the existing suite — the existing suite passing unchanged is the bar.
- "Writes nothing" is a tested property: snapshot every file under `state_dir`
  (names + bytes) before and after `plan`, across a fixture with grounding,
  links and chunking active; the snapshots must be equal. Mutation-check it
  (make `plan` call `commit`, confirm the test fails).
- A `plan` that would `NoOp` must say NoOp, and a run right after it must
  return `NoOp` — the two verdicts are the same function's.
- Scope and ownership `Aborted`s happen before synthesis, so `plan` reports
  them with `run`'s wording. A §4.4-law `Aborted` needs synthesized output, so
  `plan` cannot predict it and says `would publish N concepts (validation runs
  at publish)`.
- **`plan` needs source credentials, nothing else** (my call). It fetches, so
  each connector's own env vars must be set. It does not need model
  credentials or the `kbforge[llm]` extra: whether a synthesizer grounds is the
  class attribute `grounds` (`synthesize.py:289`, `llm_synthesizer.py:245`),
  read from the class without constructing it. So `plan`'s §6.1 validation
  skips `validate_env()` and the synthesizer import. If a publisher's token
  env var is unset, `plan` skips the open-request check and prints `open
  request: unknown (GITHUB_TOKEN not set)` instead of failing. That keeps
  `plan` usable as a CI pre-check where only source credentials exist.

Fetching is not free (rate limits, a slow Denodo view), so `plan` costs one
fetch per selected source. That is acceptable for a command a human runs; it is
why `status` does not fetch.

### 6.4 `status`

Per source, from local state only plus one read-only forge call where the
publisher supports `kbforge_open_request`:

```
products  last published 2026-10-28 14:02  ·  no open request
docs      last published 2026-10-28 14:05  ·  chunk open as !12 (40 concepts), more to follow
handbook  never run
```

"Last published" comes from `sources.json`. It is not the cursor's mtime,
which is misleading for a chunked source: its cursor is held until the final
chunk (`pipeline.py:864`). The chunk part comes from the chunk record's
`pending` and `len(admitted)`. A backlog *size* is not stored anywhere, and
storing it would change `ChunkRecord`'s shape (§4), so `status` reports only
that more is to follow; `plan` gives the count. With no fetch and no diff,
`status` cannot report drift either; `plan` does that.

### 6.5 `reset SOURCE`

Replaces the README's "delete the mirror directory and the connector's
cursor" procedure, scoped to one source so the others are untouched. It
removes:

- that source's cursor slot and chunk slot, whichever exist (a source part-way
  through chunking has only the chunk slot, but its earlier chunks are already
  in the mirror);
- every mirror slot whose document's `anchor.system` is one of the source's
  systems, and those documents' `_grounding/`, `_links/`, `_described/` and
  `_first_seen/` entries;
- the source's `sources.json` entry.

The source's systems come from `sources.json`. If it has no entry (state
written by flag mode, or before this release), they are the union of the
cursor slot's stamped `systems` and the system prefixes of the chunk record's
`admitted` doc_ids. If both are empty (for example, a legacy cursor whose
`systems` was dropped on read-through, `pipeline.py:163`), the slots are still
removed, but no mirror slot is touched, and the output says
`systems unknown: mirror left as is`. Under §5.3 a system is owned by one
source, so reset cannot remove another source's documents.

It asks for confirmation listing the counts (`--yes` skips it), never contacts
the forge, and prints what it removed. The next `run` proposes everything for
that source as a first run would. A source with no slots and no entry has
nothing to reset; say so, exit 0.

Limit, stated in its help and the README, not solved: a deletion whose review
request was closed unmerged is gone from the mirror *and* the source, so even a
reset source never re-proposes it — the concept stays in the bundle until
removed by hand. This is today's limit under today's procedure, unchanged.

Flag mode gets no `reset`: it has no source name to scope by.

### 6.6 `init [PATH]`

Writes `kbforge.yaml` with one `local_files` source on `PATH` (default
`./docs`), the `stub` synthesizer and the `dry-run` publisher, so `plan` and
`run` work with no credentials. Commented-out `synthesizer` and `publisher`
blocks show the llm and github forms. Refuses to overwrite an existing file.
Prints three lines: what it wrote, `next: kbforge plan`, and that `.kbforge/`
must persist between runs.

## 7. README

Reorder, don't grow: what you get (a reviewed PR of agent-readable concepts,
re-runs that cost nothing when nothing changed) → `init` / `plan` / `run` →
the project file → publishing → why you can trust it (the no-op rule,
canonicalization, the laws) → positioning → the flag CLI, as "running one
source without a project file". Sentences that §5 makes unnecessary are
deleted, not kept as warnings: shared mirror, same `--links` on every run,
always-chunked-or-never, and the hand-reset procedure (replaced by `reset`).
`architecture.md` gets a short §7.5 on the project layer and the lock once
built, and this note is folded per the docs convention.

## 8. Migration

An existing deployment keeps working with no change: flag mode is unchanged
except that it now takes the lock. To move to a project file, an operator
writes one source per existing invocation with the same connector config, sets
`state_dir` so that `<state_dir>/mirror` and `<state_dir>/state` are the
directories they already use (or moves them there), and runs `plan`. Because
slots are still keyed by connector name and config digest, the same config
finds the same cursor and chunk record, and `plan` should report NoOp for every
source that was up to date. That NoOp is the migration check, and the README
says so.

## 9. Testing

Offline, as usual, plus:

- Every §5 row has a test that fails if the structure is removed: two sources
  given different links is not expressible (schema test); two concurrent
  processes on one mirror → one exits 3 (real subprocesses, not a mock);
  chunked-then-unchunked-then-redo no longer rolls back; two `local_files`
  sources → the second aborts with `[source-ownership]`, and `reset` of one
  leaves the other's mirror slots byte-identical.
- `plan` writes nothing (§6.3), and `plan` NoOp ⇔ `run` NoOp on the same state.
- `run()` extraction is behavior-preserving: the existing suite passes
  unchanged.
- Project validation reports *all* problems and runs nothing.
- Mutation-check each new gate in place, per CLAUDE.md, asserting on messages.
- Live: one project with a SQL (SQLite) source and a `local_files` source,
  `llm` synthesizer, GitHub scratch repo — `init`-style config → `plan` →
  `run` (two PRs) → `run` (NoOp both) → edit one source → `plan` shows only it →
  `reset` it → `run` re-proposes it. Plus the flag CLI against the same mirror
  while a project run holds the lock.

## 10. Left to the builder

- Module layout (a `project.py` loader and a thin CLI layer is the obvious
  shape), and how `main()` is split between modes.
- The lock primitive and its Windows story.
- `sources.json`'s exact format, and writing it atomically (it is small;
  write-to-temp-then-rename is the obvious way).
- Exact `plan` / `status` formatting, within the content listed.
- Whether `links` / `grounding` inline mappings and file paths share one loader
  (they should; today's `load_links` / `load_grounding` take a path).
