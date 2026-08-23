---
type: design-note
title: okfquery — SQL over an OKF bundle
description: A standalone DuckDB reader for OKF v0.2 bundles. Ephemeral, additive about parse failures, and dependent on no kbforge code — the serving-side counterpart kbforge deliberately does not own.
tags: [okf, duckdb, serving, audit, tooling]
generated: { by: human:flyersworder, at: 2026-08-23T00:00:00Z }
status: shipped — unreleased; the package is packages/okfquery, this note keeps the rationale
okf_version: "0.2"
---

# okfquery — SQL over an OKF bundle

**Problem.** A published bundle answers "what does concept X say" by being read.
It answers nothing structural: which concepts cite a given upstream document,
what is stale and per which system, which links are hubs, whether a concept is
still synthesized from the bytes its `content_hash` claims. Those questions are
already latent in the frontmatter — `sources`, `links`, `generated` — and today
the only way to ask one is to write a script.

**What this is.** A package that loads an OKF v0.2 bundle into an in-memory
DuckDB connection and hands the connection back. Four tables from the bundle,
one optional view over a kbforge mirror. No daemon, no cache, no committed
artifact.

**What this is not.** It is not a stage, a plugin, or a kbforge subcommand. It
takes no part in fetch → … → publish and cannot be invoked from one.

## 1. Boundary — why this is a separate package

`README.md`'s layer table makes a specific claim: kbforge owns the **production
protocol**, and serving belongs to "MCP — or any context database that ingests
the bundle." A `kbforge query` subcommand would quietly retract that claim, and
would put DuckDB in the dependency tree of a library whose deterministic core
runs with no credentials and few dependencies.

A separate package inverts the cost into a benefit. `okfquery` depends on **no
kbforge code at runtime**, so it reads any OKF v0.2 bundle, including one no
kbforge run produced. That is what makes the portability claim in the layer
table demonstrable rather than asserted: someone evaluating OKF can use this
without adopting kbforge.

It lives at `packages/okfquery/`, following the `packages/kbforge-mcp`
precedent — with the difference that `kbforge-mcp` is an **ingest**-side plugin
that registers an entry point, and this registers nothing.

The distribution is `kbforge-okfquery`; the import name and console script are
both `okfquery`. PyPI already carries an unrelated `okf` — "A CLI for OKF", one
0.1.0 release by a different author, citing the same spec — and a bare
`okfquery` published beside it would read as that project's companion. The split
keeps each name honest about a different thing: the distribution says who ships
it, the import and command say what it reads. Namespacing the distribution costs
nothing here precisely because the package depends on no kbforge code — the name
records provenance, not a dependency.

## 2. Schema

```
concepts(path, type, title, description, generated_by, generated_at, facets, body)
sources (path, ordinal, id, resource, content_hash)
links   (path, target)
problems(path, kind, detail)
mirror  -- view over read_json_auto('<mirror>/*.json'), only when --mirror is given
```

`path` is the bundle-relative concept path and the join key throughout. It is
already the identity `synthesize.concept_path` mints, and OKF's one-file-one-concept
rule makes it a natural primary key.

**`sources.ordinal` is a column, not an artifact of unnesting.**
`synthesize.assemble` places the owning anchor first by convention (§6), and
that ordering is the only signal distinguishing "this system owns the concept"
from "this is cross-source grounding" (§7.1) — OKF has no field for it.
Discarding ordinal during unnest would destroy it.

The claim stops there, though. OKF specifies no ordering, and §1's whole point
is that this reads foreign bundles too — so `WHERE ordinal = 0` is the owner
query **for a kbforge-produced bundle** and is merely source order on any other,
returning an arbitrary entry with no error. The tool records the ordering
faithfully; it does not certify what the ordering means.

**`facets` is one JSON column, not columns.** `synthesize._facets` is open by
construction: any non-`OKF_OWNED` scalar or scalar-list from a connector's
`structured`. The OKF head is closed and the facets are open, which maps onto
DuckDB exactly — fixed columns for the head, `facets->>'owner'` for the rest.
`SELECT DISTINCT unnest(json_keys(facets)) FROM concepts` reports what a bundle
actually uses.

**The mirror needs no parser.** `mirror.slot_key` writes flat
`<sha256(doc_id)>.json` files of serialized `CanonicalDocument`, which
`read_json_auto` reads natively, nested `anchor` struct included. The optional
mirror table is therefore a view, not a loader — one line, and it stays correct
if `CanonicalDocument` gains a field.

Two constraints on that one line. **The glob is `<mirror>/*.json`, never
`**/*.json`**: `grounding.SIDECAR_DIR` puts drift sidecars in
`<mirror>/_grounding/`, and those are flat `{doc_id: content_hash}` maps, not
`CanonicalDocument`s — sweeping them in would wreck the inferred schema.
`grounding.py`'s own docstring flags this same trap for `mirror.load_all`, which
is the precedent for keeping the glob shallow. And **`read_json_auto` raises on
a glob matching zero files** (`IOException: No files found that match the
pattern`), so `--mirror` against a fresh mirror must fail with a message naming
the empty directory rather than a DuckDB IO error. The view is not created in
that case; fabricating an empty one would mean inventing a `CanonicalDocument`
schema this package deliberately does not import.

## 3. Parsing, and why `problems` is additive

Scan is `concepts/**/*.md` under the bundle root — where `concept_path` puts
everything — so a repo's own `README.md` and `docs/` never enter. The reserved
rule still applies inside that glob: a directory listing (OKF §8) may sit beside
the concepts it lists, so the rule is applied at every level rather than only at
the bundle root.

**The reserved rule is copied from `validate._check_strict_okf`, deliberately.** A file is
skipped only if its basename is `index.md`/`log.md` **and** it does not open
with a `---` fence; an `index.md` that claims frontmatter is a concept. Since
this package does not import kbforge, the rule is replicated. That duplication
is accepted, not hidden: it carries a comment naming its origin and a test
asserting the frontmatter-bearing `index.md` case, because a loader and a
validator that disagree about what a concept *is* make every count this tool
reports wrong in a way no query would reveal.

Parse failure kinds, each distinct:

| kind | condition |
|---|---|
| `no-frontmatter` | no `---` fence, and not a reserved name |
| `unterminated-frontmatter` | opens `---`, never closes |
| `invalid-yaml` | closes, but YAML raises |
| `frontmatter-not-mapping` | parses to a scalar or list |
| `missing-required` | no `type` / `title` / `description` / `generated` / `sources` |
| `bad-generated` | not a mapping, or missing `by`/`at` |
| `bad-timestamp` | `generated.at` present but unparseable |
| `naive-timestamp` | `generated.at` parses but carries no UTC offset (law 4 requires one) |
| `bad-sources` | not a list, or an entry with no `resource` |
| `bad-links` | `links` is not a list, or holds a non-string entry |

This is where the tool parts ways with kbforge's own parser, and the divergence
is intentional. `validate._parse_frontmatter` collapses no-fence, unterminated
fence, and broken YAML alike to `{}` — the comment above that check in `_check_strict_okf` flags
that collapse as a hazard it routes around by re-checking the raw fence. The
collapse is correct for a gate, which only needs "no usable frontmatter" and
fails either way. It is wrong here, where *which* case occurred is the entire
finding: a missing fence is a renderer bug, broken YAML is a hand-edit, an
unterminated fence is a truncated write. Same input, same three cases, opposite
requirements. The module docstring says so, so the duplication is not later
"fixed" by importing kbforge's version.

**The invariant the loader is built around:**

> Every **non-reserved** scanned file gets exactly one `concepts` row.
> `problems` is additive, never exclusive.

A file with broken YAML still appears in `concepts` with NULLs for what could
not be read. Dropping it instead would make "show me everything stale" silently
exclude precisely the files most likely to be wrong — an audit tool hiding its
worst cases. It also makes correctness checkable with one query: `count(*) FROM concepts`
equals the number of scanned files minus the reserved ones, always. The
qualifier is not a weakening — stating the invariant over *all* scanned files
would make it false on any bundle containing a fenceless `index.md`, so the test
asserting it would fail against a correct loader and its mutation check would be
run against an already-red baseline.

Loading is explicit DDL plus inserts, not a dataframe round-trip: it pins column
types so an empty bundle still answers queries instead of erroring on a missing
table, and it types `generated_at` as **`TIMESTAMPTZ`**, not `TIMESTAMP`.

That distinction is load-bearing, though not for the reason it first appears.
`synthesize._generated` writes `fm.generated_at.isoformat()`, and law 4
(`validate._check_freshness_legible`) requires only that the stamp be
offset-*aware* — not that the offset be UTC. Two DuckDB paths handle such a
stamp differently, and only one of them is the loader's:

```
-- SQL string-cast: truncates the offset, yielding the WRONG INSTANT
'2026-08-23T09:00:00+09:00'::timestamp    -> 2026-08-23 09:00
'2026-08-23T09:00:00+09:00'::timestamptz  -> 2026-08-23 00:00+00:00

-- parameter binding of an aware datetime, which is what `load` does:
INSERT ... VALUES (?)  into TIMESTAMP    -> 2026-08-23 00:00        (right instant, naive)
INSERT ... VALUES (?)  into TIMESTAMPTZ  -> 2026-08-23 00:00+00:00  (right instant, aware)
```

So a naive column does **not** corrupt the instant on the path this code takes —
an earlier draft of this note claimed it did, generalising from the cast. What a
naive column loses is the *type*: values come back with no `tzinfo`, so every
comparison against `now()` (itself `TIMESTAMPTZ`) needs a cast, and the column
asserts UTC by convention with nothing recording that it does. Law 4 exists to
force an aware stamp; storing it in a column that cannot hold one throws away
exactly the property the law buys. `load` also sets `timezone = 'UTC'` on the
connection so rendered values are comparable across bundles rather than
dependent on the reader's locale.

That session setting has one non-obvious cost: DuckDB's Python client needs
`pytz` to materialise a `TIMESTAMPTZ` as a `datetime` once a session timezone is
set, so `pytz` is a runtime dependency of the package — a consequence of the
type choice, not an independent one.

## 4. API and CLI

```python
from okfquery import load, SCHEMA_SQL

con = load(bundle: Path, mirror: Path | None = None) -> duckdb.DuckDBPyConnection
```

`load` returns **the DuckDB connection itself, unwrapped**. No query DSL, no
result objects, no `OkfDatabase` class. The proposition is that an OKF bundle is
just a DuckDB database; wrapping the connection would re-export a surface this
package does not own and would block what makes DuckDB worth reaching for —
`COPY (...) TO 'out.parquet'`, `ATTACH`, joining `concepts` against a CSV of
team owners, `INSTALL fts` over `body`. None of that costs a line of code here
as long as the real object comes back. It is also why the deferred MCP server
(§7) stays thin: `load()` plus one `query` tool.

`SCHEMA_SQL` is the DDL as text, feeding `okfquery schema` and later the MCP
schema resource from one definition.

```
okfquery query <sql> [--bundle .] [--mirror PATH] [--format table|json|csv]
okfquery shell       [--bundle .] [--mirror PATH]
okfquery schema
okfquery check       [--bundle .]      # exit 1 if problems is non-empty
```

There is no `--format parquet` and no `--output`: DuckDB writes parquet from
inside the SQL, and a second export path would be a worse version of a feature
already shipping.

`shell` is the only place anything is materialized — it writes a temp `.duckdb`
and runs the `duckdb` CLI against it, for history and completion instead of a
homegrown REPL. It **spawns via `subprocess` and deletes the file in a
`finally`**; it must not `exec`. `exec` replaces the Python process, so no
`finally`, `atexit`, or `TemporaryDirectory` finalizer would ever run, and a
database holding every concept body would outlive the session until the OS
cleared its temp dir — quietly breaking the ephemeral guarantee that motivated
the whole design. If the binary is absent it prints the path and the command
rather than falling back to a second REPL implementation.

`check` exists so a bundle repo's CI can fail on a concept that stopped parsing.
It is `query` plus an exit code, and it is the first thing to cut if this needs
to be smaller.

## 5. Testing

Fixtures are hand-written bundles on disk, not generated: the point is exact
control of the pathological cases. One clean bundle; one messy bundle holding a
plain concept, a grounded concept (two `sources`, exercising `ordinal`), a
faceted concept, a reserved `index.md` with no fence, an `index.md` *with* a
fence, a fenceless non-reserved file, broken YAML, an unterminated fence,
frontmatter parsing to a list, a `type`-less concept, an unparseable
`generated.at`, and a `sources` entry with no `resource`. One fixture per row of
the §3 table, so "every kind fires" is checkable against that table rather than
against the test file.

1. **Unit** — every `problems.kind` fires, asserted on `detail` and not only on
   the kind. Per CLAUDE.md, a test checking the slug alone passes when the
   loader reports the right kind for the wrong reason; the unterminated-fence
   and broken-YAML fixtures are the pair most likely to be conflated, so those
   two assert distinct messages.
2. **Invariant** — `count(*) FROM concepts` equals the messy bundle's scanned
   file count *minus its two reserved fenceless files*. One assertion for "no
   silent drops". The fixture deliberately contains both a reserved file that is
   skipped and a reserved-named file that is not, so the count is wrong under
   either sloppy reading of the rule.
3. **Round-trip** — run kbforge's pipeline (`local_files` + `dry_run`) to
   produce a real bundle, then load it. This is the anti-drift test: it catches
   the replicated reserved rule diverging from `validate.py`, and a change to
   `synthesize._render` that breaks the parser. It needs kbforge, so kbforge is
   a **dev** dependency of `okfquery` only; the runtime stays free of it.

Mutation checks follow CLAUDE.md's procedure — edit **in place**, restore with
`git checkout --`, never `cp -R`:

- Make the reserved rule exempt `index.md` unconditionally → the drift test must
  fail naming the frontmatter-bearing case.
- Make broken files skip instead of NULL-fill → the count assertion must fail.

All fixtures are on disk, so `uv run pytest` still never touches the network.

## 6. Known limits

**Both mirror joins are lossy, and they fail differently.** Join on `id`;
compare on `hash`.

`sources.id = mirror.doc_id` holds for every shipped connector, but
`synthesize._source_entry` builds `id` from `anchor.system`/`anchor.native_id`
while `doc_id` is its own field, and as `assemble`'s comment notes, nothing
enforces the two agree. A third-party connector where they diverge produces a
silent false miss — the dual-carrier defect again: two expressions of one
identity with no gate binding them.

`sources.content_hash = mirror.anchor.content_hash` is *not* the sound
alternative. `pipeline.run` calls `commit(mirror_path, docs)` immediately after
`kbforge_publish`, and publishing only **opens a review request** — kbforge
never merges. Between publish and merge the mirror already holds the new hash
while the bundle still carries the previous run's, so an equi-join on hash
returns zero rows for precisely the concepts under open review: the ones an
audit most wants to see.

Read the other way round, that is the useful query rather than the broken one.
Join on `id`, then **compare** hashes: rows that match are current, rows whose
hashes differ have an update in review, and rows missing from the bundle have
never been published. Hash collisions are not a concern here —
`canonical.content_hash` hashes a payload including `doc_id`, so byte-identical
documents in two systems still hash differently. What remains is the `id` gap
above, documented in the README rather than worked around.

**Bundle-only staleness is relative, not absolute.** `generated_at` comes from
the anchor's `retrieved_at`, which under-reports for concepts re-rendered to
drop a dangling link (`synthesize._generated`). Staleness queries inherit that;
the direction is safe (a concept reads staler than it is) but the value is not
a last-modified time.

**No full-text search out of the box.** `body` is a column, so `INSTALL fts` and
an index over it is a documented recipe rather than a feature. Building it in
would mean owning an index lifecycle, which contradicts the ephemeral choice.

## 7. Deferred

| Item | Why not now |
|---|---|
| **MCP serving server** | `load()` plus a `query` tool and a schema resource. Deferred because a CLI is already an agent interface where agents have a shell, and because a schema cannot be iterated on through a tool definition. Cheap once the library API is stable — which is what §4 is for. |
| **FTS / embeddings** | A recipe first. Promote only if the recipe proves load-bearing. |
| **Remote bundles** | Reading a bundle over HTTP rather than from a checkout. Wants a caching story, which the ephemeral choice deliberately avoids. |

## 8. Phasing

| Phase | Contents |
|---|---|
| **first** | loader, four tables, `problems`, `query`/`schema`, unit + invariant tests |
| **next** | `--mirror` view, `shell`, `check`, round-trip and mutation tests |
| **later** | MCP serving server, FTS recipe, remote bundles |
