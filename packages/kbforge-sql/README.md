# kbforge-sql

`kbforge-sql` lets any database [SQLAlchemy](https://www.sqlalchemy.org/) can reach be a
[kbforge](https://github.com/flyersworder/kbforge) source. Install it alongside kbforge and
a source becomes one scoped `SELECT` instead of a Python package: each row the query
returns (or each group of rows sharing an id) becomes one canonical document, rendered as
fixed-format markdown. It registers itself under the `kbforge.connectors` entry-point
group, so `kbforge list` shows `sql` with no further wiring. Every run is a full snapshot,
which makes deletions derivable — an id seen last run and missing now becomes an explicit
tombstone — so this is the first kbforge connector that emits tombstones at all.

## Install

```bash
pip install kbforge-sql
```

`kbforge-sql` ships **no database driver**; the operator installs the one their engine
needs, for example:

```bash
pip install denodo-sqlalchemy   # Denodo
pip install "psycopg[binary]"   # PostgreSQL
pip install oracledb            # Oracle
pip install pyodbc              # SQL Server and other ODBC targets
```

## Configure a source

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
url_template: https://portal.example/products/{product_id}   # optional; {column} fields, id columns only
retries: 2                       # transient-error retries; default 2
max_removed_fraction: 0.5        # deletion ceiling, see below; default 0.5
```

A grouped source — a joined view where one application spans many rows, folded into one
document per application:

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

Columns outside `id` and `group.children` must be constant across an entity's rows; a
grouped entity whose rows disagree on one is a fetch error naming the id and the column.

kbforge takes connector config as repeated YAML-typed `--set` pairs, so that is one key
per flag:

```bash
kbforge run --connector sql \
  --set system=products \
  --set url_env=DENODO_URL \
  --set password_env=DENODO_PASSWORD \
  --set 'query=SELECT product_id, product_name, family, status, description, last_refreshed FROM iv_product' \
  --set 'id=[product_id]' \
  --set title=product_name \
  --set text=description \
  --set 'facets=[family, status]' \
  --set 'exclude=[last_refreshed]' \
  --set type=product \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

Because `--set` values are YAML-typed, a query with its own quoted string literal or a
multi-line `WHERE` clause is easier to get right in a shell variable or script than typed
inline; quote the whole `key=value` pair once and let the query itself carry its quotes.

The query reaches the driver exactly as written, with no parameter binding, so `LIKE 'EV-%'`,
Postgres `::` casts and `'10:30'` literals are safe on every driver. On the first result
the connector checks that every configured column exists (listing the real ones if not)
and that no two result columns share a name; `SELECT a.id, b.id` must alias them apart.

## First run: `dry-run` and a throwaway mirror

No tool proves a query executable across every dialect, so try a new source against a
mirror you can throw away before pointing it at the real one. kbforge's default publisher
is `dry-run`, so the query runs and every concept renders to local files with nothing
opened anywhere:

```bash
kbforge run --connector sql --set … \
  --mirror /tmp/try --state /tmp/try-state --out /tmp/try-out
```

Read the rendered files under `/tmp/try-out` before wiring in a real `--mirror` and
`--publisher`.

## Credentials

`url_env` and `password_env` hold environment variable **names**, never values. A
credential never appears in config, on a command line, or in an error message — errors
name the env var, not its content. With `password_env` set, the URL carries no password
and the raw value is injected with SQLAlchemy's `URL.set(password=...)`, so a password
containing `@`, `:`, `/` or `%` needs no percent-escaping.

The expected deployment is a service account, and the recommended one is a **dedicated
read-only account** granted `SELECT` only on the views its queries use. The connector
rolls back every transaction and never commits — no commit exists anywhere in the
package — but that is a bound on accidents, not a guarantee: kbforge cannot prove a SQL
string is free of side effects through a function call, a procedure, or a dialect
extension, so the database's grants are what actually prevent a write. (The connector
issues an explicit rollback even though SQLAlchemy also rolls back on connection close;
belt-and-braces, since the account's grants are what really carry the guarantee.)

`kbforge_validate_config` also rejects a blank `title` or a blank `id` column name before
any connection is attempted, alongside the checks in §3.1 of the design note.

A dropped or refused connection is retried up to `retries` times (default 2) with
exponential backoff capped at 30 seconds; bad SQL, a missing view, or a missing grant fails
at once, because retrying a typo only delays the message — on drivers that classify it that
way (see "Known limits").

## Deletions

Every run is a full snapshot: the connector keeps the id set from the last published run
in the cursor manifest, and any id missing from this run's result becomes an explicit
tombstone. Two guards sit in front of that, because a view that returns nothing — a cache
mid-refresh, a failed upstream load, a filter changed upstream — is not an error to the
database:

- **Empty result with a non-empty prior manifest fails the run** and emits no tombstones,
  rather than reading "the source is empty" as "delete every concept". An intentionally
  empty source is rare enough to be handled by removing its config instead.
- **Deletion ceiling.** If the tombstones would exceed `max_removed_fraction` (default
  `0.5`) of the prior manifest, the run fails and states the count and the fraction. For a
  deliberate large cleanup, rerun with `KBFORGE_SQL_ALLOW_REMOVALS` set (see below) rather
  than raising `max_removed_fraction` — see "Known limits" for why.

## Known limits

**Whether an error is retried is the driver's call.** The connector retries SQLAlchemy's
`OperationalError` and `InterfaceError`, which most drivers reserve for connection
failures. Some also raise `OperationalError` for statement errors — sqlite3 for a missing
table or a syntax error, pymysql for access denied and unmapped server errors — so on those
drivers a bad query is retried before it fails, and the message reads "OperationalError
after N attempt(s)". That costs time, never correctness. PostgreSQL (psycopg) reports these
as `ProgrammingError` and fails at once; check your driver with a deliberately misspelled
view on a first run, and set `retries: 0` if it misclassifies.

**Editing ANY config key resets deletion memory, not just the query.** Cursor slots are
keyed by a digest of the *whole* connector config (`pipeline._instance_key`), so editing
`query` — narrowing its `WHERE`, say — or any other key, including `max_removed_fraction`
itself, means the next run finds no prior cursor: `NoOp`, no tombstones, and the old slot
trips the ceiling again if the edit is ever reverted. The connector cannot fix this; it is
deliberately mirror-blind. After narrowing a query, remove the stale concepts by hand in
the review repository.

**A tripped deletion ceiling is cleared with `KBFORGE_SQL_ALLOW_REMOVALS`, not by raising
`max_removed_fraction`.** Raising the fraction is a config edit, so it hits the limit
above: it resets deletion memory instead of performing the cleanup, and the old cursor
slot trips the ceiling again once the fraction is reverted. Instead, set the environment
variable `KBFORGE_SQL_ALLOW_REMOVALS` to a comma-separated list of source `system` names
(for example `KBFORGE_SQL_ALLOW_REMOVALS=products`) and rerun with the config unchanged —
it is read at fetch time, out of band from the config, so the cursor slot and the rest of
`max_removed_fraction`'s guard stay intact. The empty-result guard always applies, even
with the override set.

**Ids can collide with another source's.** `concept_path` drops the system prefix, so a
product id `42` and an application id `42` render the same file, and the pipeline aborts
on the collision — numeric keys make this likely. Until bundle paths are system-qualified,
give ids a kind prefix in the query itself (`'product-' || product_id AS kb_id`, or the
`CONCAT` your dialect prefers) and use that column as `id`. No connector config is needed
for this.

**Ids differing only in case are one directory on a case-insensitive checkout.** `Foo` and
`foo` render distinct `native_id`s but the same path on the default macOS or Windows
filesystem, so a case-only id pair collides in a local checkout even though it would not
on Linux CI.

**Canonicalization can fold two raw ids into one entity.** NFC normalization and
trailing-whitespace stripping (§4.3) run before the duplicate-id check, so two raw id
values that canonicalize to the same string collapse onto one id: without `group`, that is
the duplicate-id error; with `group`, it is a silent merge into one entity's rows.

## Testing against your own database

The package ships a live test that is skipped unless you ask for it. Point it at a
read-only view and run it with your driver installed:

```bash
pip install kbforge-sql denodo-sqlalchemy pytest   # or psycopg[binary], oracledb, ...
export KBFORGE_SQL_LIVE_URL='denodo://kb_reader@denodo.example:9996/product_vdb'
export KBFORGE_SQL_LIVE_PASSWORD=...               # optional
export KBFORGE_SQL_LIVE_QUERY="SELECT product_id, product_name FROM iv_product WHERE product_name LIKE '%a%'"
export KBFORGE_SQL_LIVE_ID=product_id KBFORGE_SQL_LIVE_TITLE=product_name
pytest packages/kbforge-sql/tests/test_sql_live.py --run-live
```

It fetches twice and requires identical content hashes, which is the check that matters
for a new source: anything volatile in the result (a refresh timestamp, a computed
column) fails it, and belongs in `exclude`. Without the `KBFORGE_SQL_LIVE_QUERY` variables
it queries `information_schema`, which any PostgreSQL accepts.

## Design

The [design note](https://github.com/flyersworder/kbforge/blob/main/docs/design/2026-09-18-sql-source-connector-design.md)
holds the full rationale and what remains deferred (incremental fetch, relations between
rows, deletion memory across a query edit). The shipped design is in
[`docs/architecture.md`](https://github.com/flyersworder/kbforge/blob/main/docs/architecture.md) §4.1.
