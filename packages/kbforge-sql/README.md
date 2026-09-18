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
url_template: https://portal.example/products/{product_id}   # optional
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
rolls back every transaction and never commits, but that is a bound on accidents, not a
guarantee: kbforge cannot prove a SQL string is free of side effects through a function
call, a procedure, or a dialect extension, so the database's grants are what actually
prevent a write.

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
  `0.5`) of the prior manifest, the run fails and states the count and the fraction. Set
  it to `1.0` for a deliberate large cleanup.

## Known limits

**Editing the query resets deletion memory.** Cursor slots are keyed by a digest of the
whole connector config, so editing `query` — narrowing its `WHERE`, say — means the next
run finds no prior cursor, and rows the new query no longer returns leave stale concepts
with no tombstone. The connector cannot fix this; it is deliberately mirror-blind. After
narrowing a query, remove the stale concepts by hand in the review repository.

**Ids can collide with another source's.** `concept_path` drops the system prefix, so a
product id `42` and an application id `42` render the same file, and the pipeline aborts
on the collision — numeric keys make this likely. Until bundle paths are system-qualified,
give ids a kind prefix in the query itself (`'product-' || product_id AS kb_id`, or the
`CONCAT` your dialect prefers) and use that column as `id`. No connector config is needed
for this.

## Design

The [design note](https://github.com/flyersworder/kbforge/blob/main/docs/design/2026-09-18-sql-source-connector-design.md)
holds the full rationale and what remains deferred (incremental fetch, relations between
rows, deletion memory across a query edit). The shipped design is in
[`docs/architecture.md`](https://github.com/flyersworder/kbforge/blob/main/docs/architecture.md) §4.1.
