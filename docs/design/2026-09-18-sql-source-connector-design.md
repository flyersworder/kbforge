---
type: design-note
title: kbforge — a relational database as a source (kbforge-sql)
description: A configuration-only SQL source connector — one scoped query per source, optional row grouping, deterministic markdown rendering, snapshot fetch with manifest-derived tombstones, and the guards that keep an empty or shrunken result from deleting the knowledge base.
tags: [okf, connectors, sql, sqlalchemy, denodo, deletion, producer]
generated: { by: human:flyersworder, at: 2026-09-18T00:00:00Z }
status: shipped in kbforge-sql 0.1.0 — §9 still deferred
okf_version: "0.2"
---

# kbforge — a relational database as a source (`kbforge-sql`)

**The short version.** A new distribution, `kbforge-sql`, makes any database
SQLAlchemy can reach a kbforge source through configuration alone. The operator
writes one scoped `SELECT`; each entity it returns becomes one canonical
document, rendered as fixed-format markdown so it can be both an owning document
and a grounding document. Every run is a full snapshot, so deletions are
derivable: an id seen last run and missing now becomes an explicit tombstone.
This is the first connector that emits tombstones at all.

The motivating deployment is product and application master data in Denodo,
combined with documents from an MCP server into a knowledge base scoped to one
application. Nothing below is Denodo-specific; Denodo is reached through its
SQLAlchemy dialect or its PostgreSQL-compatible interface like any other engine.

## 1. Scope

**In:** descriptive *entity* records — a product, an application, and their
attributes — from flat tables or joined views.

**Out:** metrics, aggregates, and executable metric SQL. Those belong to
`agentic-data-contracts` (see
[`2026-07-18-datacontract-bridge-design.md`](2026-07-18-datacontract-bridge-design.md)).
kbforge turns rows into cited prose; it does not become a reporting layer.

**Deferred** (§9): incremental fetch, relations between rows, and keeping
deletion memory across a query edit.

## 2. Packaging

`packages/kbforge-sql/`, package `kbforge_sql`, entry point
`sql = "kbforge_sql.connector:CONNECTOR"` under `kbforge.connectors`. It is
versioned independently, starts at 0.1.0, and is tagged `kbforge-sql-v0.1.0`.

Dependencies: `kbforge`, `sqlalchemy>=2`, `pydantic`. **No database driver.**
The operator installs the one driver their engine needs (`denodo-sqlalchemy`,
`psycopg`, `oracledb`, `pyodbc`). This follows the packaging rule in
architecture.md §4.1: SQLAlchemy is not a dependency every kbforge user should
pay for, and driver choice is a deployment decision.

Modules, each with one job:

| Module | Job |
|---|---|
| `config.py` | the pydantic config model and every offline check |
| `errors.py` | `SqlSourceError`, the one exception the connector raises |
| `values.py` | database value → canonical JSON-safe form |
| `identity.py` | id column values → path-safe, injective `native_id` |
| `render.py` | canonical entity → markdown text |
| `connector.py` | the four hooks; the only module that does I/O |

Using several sources means installing several companions — for example
`pip install kbforge-mcp kbforge-sql denodo-sqlalchemy`, which pulls `kbforge`
in. Each source is its own `kbforge run --connector …`, all sharing one
`--mirror`, as cross-source grounding requires (architecture.md §7.1).

## 3. Configuration

A flat source — one row, one document:

```yaml
system: products                 # per-instance identity; prefixes every doc_id
url_env: DENODO_URL              # env var NAME: denodo://kb_reader@host:9996/product_vdb
password_env: DENODO_PASSWORD    # optional env var NAME; injected via URL.set()
query: |
  SELECT product_id, product_name, family, status, description, last_refreshed
  FROM   iv_product
  WHERE  app_id = 'EV-TRACTION'
id: [product_id]                 # one or more columns -> native_id
title: product_name
text: description                # optional free-text column, placed verbatim
facets: [family, status]         # scalar columns -> filterable frontmatter
exclude: [last_refreshed]        # volatile columns, dropped before hashing
type: product                    # OKF `type` for every concept; default "concept"
url_template: https://portal.example/products/{product_id}   # optional
retries: 2                       # transient-error retries; default 2
max_removed_fraction: 0.5        # deletion ceiling, §6.3; default 0.5
```

A grouped source — a joined view where one application spans many rows, folded
into one document per application:

```yaml
system: applications
url_env: DENODO_URL
password_env: DENODO_PASSWORD
query: |
  SELECT app_id, app_name, segment, app_description,
         product_id, product_name, product_status
  FROM   iv_product_application
  WHERE  app_id = 'EV-TRACTION'
id: [app_id]
title: app_name
text: app_description
facets: [segment]
type: application
group:
  children: [product_id, product_name, product_status]
  order_by: [product_id]
  heading: Products              # optional; defaults to the source `system`
```

Columns outside `id` and `group.children` must be constant across an entity's
rows; a grouped entity whose rows disagree on one is an error naming the id and
the column, because picking either value would be a guess.

`url_env` and `password_env` hold environment variable **names**. A credential
never appears in config, on a command line, or in an error message. With
`password_env` set, the URL carries no password and the raw password is
injected with SQLAlchemy's `URL.set(password=...)`, so a password containing
`@`, `:`, `/` or `%` needs no percent-escaping. The expected deployment is a
service account; the recommended one is a **dedicated read-only account**
granted `SELECT` only on the views its queries use, because that grant is the
only layer that actually prevents a write (§7).

`type` is the *kind* of concept (`product`, `application`), not the instance.
One query returns one kind of entity, so a fixed `type` per source is enough.

### 3.1 Checks before any connection (`kbforge_validate_config`)

Every problem is reported at once, before any I/O:

- `system`, `url_env`, `query`, `id`, `title` present and non-blank; `id`
  non-empty.
- The env vars named by `url_env` and `password_env` are set.
- `id`, `title`, `text` and `facets` do not appear in `exclude`.
- No facet is named like an OKF-owned key (`type`, `title`, `description`,
  `generated`, `sources`, `links`). `synthesize._facets` would silently drop it;
  rejecting it here says so.
- `group.children` does not include any `id` column; `group.order_by` is a
  subset of `group.children`.
- `url_template` references only `id` columns. A link that interpolated any
  other column would move whenever that column changed.
- `retries >= 0`; `0 < max_removed_fraction <= 1`.

### 3.2 Checks on the first result

Every configured column (`id`, `title`, `text`, `facets`, `exclude`,
`group.*`) must exist in the result. A mismatch fails the run and lists the
columns the query actually returned, so a typo or a renamed view column is a
one-line fix rather than a silently empty attribute.

## 4. Fetch → normalize

### 4.1 `fetch` — the only step with I/O or a clock

1. Build the URL from `url_env` (and `password_env`); `create_engine`; open one
   connection.
2. Begin a transaction, execute `query`, fetch **all** rows, **roll back**,
   close. There is no commit anywhere in the package.
3. Check columns (§3.2).
4. Drop `exclude` columns; convert every value to canonical form (§4.3).
5. Group rows by the `id` tuple. Without `group`, an id with more than one row
   is an error naming the id — two rows must never collapse silently onto one
   concept. With `group`, non-child columns that disagree within an entity are
   an error naming the id and the column (§3).
6. Emit one `RawRecord` per entity: `media_type: application/json`, payload the
   canonical entity serialized with sorted keys, `anchor_hint` carrying
   `native_id`, the rendered `url_template`, and `retrieved_at`. `retrieved_at`
   is one fetch-clock timestamp for the whole run (a row has no mtime).
7. Apply the deletion guards (§6.3), then emit a tombstone record per id that is
   in the prior manifest and missing now (§6).
8. Return `FetchResult(records, Cursor(connector="sql", payload={"ids": [...]}),
   complete=True)`.

The fetch is all-or-nothing. A failure part-way through raises; the connector
never returns a partial row set, because under the snapshot model a partial
result is indistinguishable from deletions.

### 4.2 Identity

`native_id` is the `id` column values, each canonicalized (§4.3) and
percent-escaped, joined with `/`. The escape set is `kbforge-mcp`'s (`slug.py`):
`%`, `/`, the control characters, and `<>:"|?*\` — a native_id becomes a
path in a repository checked out on Windows as well as Linux, and `:` is
illegal in an NTFS filename. A trailing `.` or space is escaped too, and a
native_id ending in `.md` has that dot escaped, because `concept_path` strips a
`.md` suffix a second time downstream. Escaping `%` and `/` is what makes the
join injective: the composite `("a/b", "c")` and `("a", "b/c")` cannot meet. A
NULL or blank id value is an error naming the column. `doc_id` is
`f"{system}:{native_id}"`.

### 4.3 Canonical values (`values.py`)

| Database value | Canonical form |
|---|---|
| `NULL` | omitted from facets; `—` in the rendered text |
| `str` | NFC-normalized, CRLF/CR → LF, trailing whitespace stripped |
| `int`, `bool` | unchanged |
| `Decimal` | normalized string, never a float (`1.50` → `"1.5"`) |
| `float` | shortest round-trip `repr`; NaN and ±inf rejected, naming the column |
| `date` | ISO `YYYY-MM-DD` |
| `datetime`, tz-aware | ISO 8601 in UTC |
| `datetime`, naive | ISO 8601, kept naive — never guessed into a timezone |
| `UUID` | its canonical string form |
| `bytes`, anything else | rejected, naming the column: `exclude` it or cast it in SQL |

Rejecting rather than dropping is deliberate. A silently dropped column is a
fact synthesis was never allowed to see, with nothing to say so (§4.3 law 3,
semantic sufficiency).

### 4.4 `normalize` — pure

Parses each payload and builds the `CanonicalDocument`:

- `title` ← the `title` column; `structured` ← the non-null `facets` plus
  `type`; `relations` ← none (§9).
- `text` ← the fixed-format rendering below.
- Tombstone records become `CanonicalDocument(deleted=True)` with the same
  `doc_id` scheme.

`text` must carry every value, not just the facets. Grounding documents reach
the LLM synthesizer with their `text` only (`llm_synthesizer._grounding_block`
does not pass `structured`), and facets hold only scalars and lists of scalars
(`synthesize._facets`), so a grouped child list could never be a facet. The
main use — an application concept grounded by product rows — works only if the
row is in the text.

### 4.5 Rendering (`render.py`)

```markdown
<the `text` column, verbatim, if configured and non-null>

## Attributes
- **app_id:** EV-TRACTION
- **segment:** Automotive

## Products
| product_id | product_name | product_status |
|---|---|---|
| IMC300 | … | active |
```

- *Attributes* lists the `id` columns first, then every remaining column
  except `title`, `text` and `group.children`, in **query column order** —
  stable, and the order the SQL author chose. The id is in the text because a
  grounding reader sees nothing else (§4.4).
- The child table appears only with `group`. Its heading is `group.heading`,
  defaulting to `system`. Rows sort by `group.order_by`, then by the whole
  canonical row as a tiebreaker, so ties cannot reorder between runs.
- A child row whose values are all NULL is dropped: that is what a `LEFT
  JOIN` returns for an entity with no children, and it is not a child.
- `|` in a cell is escaped as `\|`; a newline in a cell becomes `<br>`.
- There is no templating. Wording is the synthesizer's job; the connector's is
  to present the row faithfully and stably.

## 5. Executability, and a first run

No tool proves a query executable across dialects, so the checks are layered:
the offline checks (§3.1), then the real query against the real database, then
the column check (§3.2), then `assert_stability` and `assert_fetch_contract`.

The loop for a new source needs no new command. kbforge's default publisher is
`dry-run`, so

```bash
kbforge run --connector sql --set … --mirror /tmp/try --state /tmp/try-state --out /tmp/try-out
```

executes the query, renders every concept to local files, and opens nothing.
The README documents this as the first step for any new source, with a
throwaway mirror so the trial does not seed the real one.

## 6. Deletions

### 6.1 The manifest

The cursor payload is `{"ids": [sorted native_ids]}` from the current fetch.
Next run, every prior id missing from the result becomes a tombstone record.
This is the first connector to emit tombstones; the pipeline side has existed
since 0.4.0 and is exercised end to end by this connector's tests.

### 6.2 Why it is safe on the existing pipeline

- `pipeline.run` saves the cursor **only on a successful publish**. A failed or
  aborted run keeps the old manifest, so the next run re-emits the same
  tombstones — at-least-once, as §4.2 requires.
- `mirror.diff` ignores a tombstone for an id the mirror never held, so a stale
  manifest entry is harmless.
- A `NoOp` run saves no cursor, which is correct: no-op means the id set did not
  change.
- `run` loads the cursor under `info.name` and saves it under
  `cursor.connector` — the asymmetry that blocks MCP deletion
  ([`2026-08-16-mcp-source-connector-design.md`](2026-08-16-mcp-source-connector-design.md)
  §10.1). Here both are `"sql"`, and a test pins it.

### 6.3 Guards against a shrunken result

A view that returns nothing — a Denodo cache mid-refresh, a failed upstream
load, a filter changed upstream — is not an error to the database. Under the
snapshot model it would tombstone every concept, and the next review request
would propose deleting the knowledge base. The review gate would catch it; a
correct pipeline should not depend on a reviewer noticing.

- **Empty result with a non-empty prior manifest → the run fails**, naming the
  source, and emits no tombstones. An intentionally empty source is rare enough
  to be handled by removing its config. This guard applies unconditionally,
  even with the override below set.
- **Deletion ceiling.** If the tombstones would exceed `max_removed_fraction` of
  the prior manifest, the run fails and states the count and the fraction. Do
  **not** clear this by raising `max_removed_fraction` — that is a config edit,
  and §6.4 means it resets deletion memory instead of performing the cleanup.
  Instead, set the environment variable `KBFORGE_SQL_ALLOW_REMOVALS` to a
  comma-separated list of source `system` names and rerun with the config
  unchanged; the connector reads it at fetch time, out of band from the
  config, so it clears a tripped ceiling without touching the cursor slot.

Both guards run before any tombstone is emitted.

### 6.4 Known limit: editing the config resets deletion memory

Cursor slots are keyed by a digest of the whole connector config
(`pipeline._instance_key`). Editing **any** config key — not only `query` —
means the next run finds no prior cursor: no tombstones, a silent `NoOp`, and
the old slot trips the ceiling again if the edit is ever reverted. This is why
raising `max_removed_fraction` is not the cleanup path (§6.3): it is itself a
config edit, so it resets the very manifest the ceiling reads. Narrowing a
query, say, still leaves rows the new query no longer returns as stale
concepts with no tombstone; remove them by hand in the review repository. The
root fix is in core (§9).

### 6.5 Known limit: path collisions with other sources

`concept_path` drops the system prefix (CLAUDE.md, "one bundle path, one
owner"), so a product id `42` and an application id `42` render the same file,
and the pipeline aborts on the collision. Numeric keys make this likely. Until
bundle paths are system-qualified, give ids a kind prefix in the query itself —
`'product-' || product_id AS kb_id`, or `CONCAT(...)` where the dialect
prefers it — and use that column as `id`. No connector config is needed for this.

## 7. Errors and read-only posture

**Transient failures cost a delayed update, never a wrong one.** If `fetch`
raises, the run stops before diff, synthesis, mirror commit, or cursor save; the
next scheduled run starts from the same state.

- **Retry transient errors only.** SQLAlchemy `OperationalError` and
  `InterfaceError` (refused, dropped, timed-out connections) retry the whole
  query up to `retries` times with exponential backoff (1s, 2s, 4s, …).
  Re-running a `SELECT` is safe. `ProgrammingError` and every other error — bad
  SQL, a missing view, no permission — fail immediately: retrying a typo only
  delays the message.
- **Messages** name the source `system` and the error class. A URL appears only
  as `render_as_string(hide_password=True)`. The run exits non-zero, which is
  all a scheduler needs to alert.

**Read-only is layered, and only the last layer is a guarantee.** The only
statement executed is the configured `query`; it runs inside a transaction that
is always rolled back and never committed; and the account should be read-only.
kbforge cannot prove a SQL string has no side effects — a statement can write
through a function call, a procedure, or a dialect extension — so the rollback
bounds accidents and the database's grants are what prevent writes. This is the
SQL counterpart of `kbforge-mcp`'s rule that it calls exactly the two configured
tools and cannot tell whether either one has side effects.

## 8. Testing

**Offline** (`uv run pytest`, no network). SQLite through SQLAlchemy is a real
engine, so these tests execute real queries rather than mocks.

- Config: every §3.1 rule, all reported before a connection is attempted; the
  §3.2 column-mismatch message lists the available columns.
- Values: each §4.3 row — `Decimal("1.50")` and `Decimal("1.5")` render the same,
  NaN is rejected, naive datetimes stay naive, bytes are rejected.
- Rendering: a golden file per shape (flat, grouped, NULLs, `|` and newlines in
  cells); child order is stable under shuffled input rows.
- Identity: composite ids containing `/` and `%` stay distinct.
- Stability: `assert_stability` passes on real query output; a clock-removal
  test takes the clock away between two `normalize` passes and requires
  identical anchors (architecture.md §4.3).
- Deletions, through the real `pipeline.run`: add → remove a row → tombstone →
  concept removed; an aborted run re-emits its tombstone; an empty result fails
  with no tombstones; an exceeded ceiling fails; the cursor round-trips under
  `"sql"`.
- Errors: a transient error is retried and then succeeds; a `ProgrammingError`
  is not retried; the password appears in no error message.
- Read-only: a configured `INSERT`/`DELETE` changes nothing, verified by reading
  the table afterwards.

**Mutation checks.** For each gate — the empty-result guard, the deletion
ceiling, the rollback, the duplicate-id check, the column check — break it in
place, confirm the test named for it fails with *that gate's* message, and
restore with `git checkout --`.

**Live** (`--run-live`). A PostgreSQL database named by `KBFORGE_SQL_LIVE_URL`
exercises a real network driver end to end through the dry-run publisher.
Denodo can only be live-tested from inside a network that reaches it: point
`KBFORGE_SQL_LIVE_URL` (and `KBFORGE_SQL_LIVE_PASSWORD`) at a read-only view,
run the live suite, and record the result in the pull request.

## 9. Deferred

- **Incremental fetch** via a `cursor_column`. It cannot see deletions, and joined
  views rarely have a trustworthy change timestamp. Add it when a source is too
  large to snapshot, paired with a periodic full run for deletions.
- **Relations between rows** (for example product → family as a concept link).
  This needs a way to name the target source's `doc_id` from a column, and
  concept links across sources hit the cross-system relation abort until paths
  are system-qualified.
- **Deletion memory across a query edit** (§6.4). Keying cursor slots by
  `system` rather than the whole config would keep the manifest through an
  edit. That is a core change to `_instance_key` affecting every connector
  (and `kbforge-mcp`'s deletion work), with its own legacy read-through, so it
  gets its own design.
