# okfquery

SQL over an [OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle, via DuckDB. `okfquery` loads a published bundle into an in-memory DuckDB
connection and hands the connection back — no daemon, no cache, no committed
artifact. It depends on no kbforge code at runtime, so it reads any OKF v0.2
bundle, kbforge-built or not.

## Install and use

```bash
uv pip install kbforge-okfquery   # or: pip install kbforge-okfquery
okfquery query "select count(*) from concepts" --bundle path/to/bundle
okfquery shell --bundle path/to/bundle
```

`query` runs one SQL statement and prints the result (`--format table|json|csv`).
`shell` writes a temporary `.duckdb` file, opens the `duckdb` CLI on it, and
deletes the file when the shell exits.

## Schema

```
$ okfquery schema
CREATE TABLE concepts (
    path          VARCHAR NOT NULL,
    type          VARCHAR,
    title         VARCHAR,
    description   VARCHAR,
    generated_by  VARCHAR,
    -- TIMESTAMPTZ, never TIMESTAMP. Not because a naive column would corrupt
    -- the instant on this package's load path -- `load` binds an aware Python
    -- datetime through executemany, so the instant survives a naive column
    -- fine. What a naive column loses is the type: values come back with no
    -- tzinfo, so every comparison against now() (itself TIMESTAMPTZ) needs a
    -- cast, and the column asserts UTC by convention with nothing recording
    -- that it does. §4.4 law 4 exists to force an *aware* stamp; a column that
    -- cannot hold one throws away exactly the property the law buys.
    generated_at  TIMESTAMPTZ,
    facets        JSON,
    body          VARCHAR
);

CREATE TABLE sources (
    path          VARCHAR NOT NULL,
    -- A real column. kbforge's synthesize.assemble puts the OWNING anchor first
    -- and grounding anchors after it, and that ordering is the only thing
    -- telling owner from ground -- OKF has no field for it. Discarding ordinal
    -- during unnest would destroy the signal.
    ordinal       INTEGER NOT NULL,
    id            VARCHAR,
    resource      VARCHAR,
    content_hash  VARCHAR
);

CREATE TABLE links (
    path          VARCHAR NOT NULL,
    target        VARCHAR NOT NULL
);

CREATE TABLE problems (
    path          VARCHAR NOT NULL,
    kind          VARCHAR NOT NULL,
    detail        VARCHAR NOT NULL
);
```

`path` is the bundle-relative concept path and the join key throughout.
`sources.ordinal = 0` is the owning source for a kbforge-produced bundle (the
convention `synthesize.assemble` writes) — on a foreign bundle it is merely
source order, with no error to tell you that. `facets` is one JSON column, open
by construction, so `facets->>'owner'` reaches whatever a connector attached.
`problems` is additive: a file that failed to parse still gets a `concepts` row
with NULLs for what could not be read, plus one `problems` row per distinct
failure — nothing a broken concept says gets silently dropped from an audit.

## Queries, run against the messy test fixture

All four queries below were run as shown against
`packages/okfquery/tests/fixtures/messy` — 9 `.md` files under `concepts/`, 7 of
them concepts (`concepts/index.md` and `concepts/log.md` are reserved and
fenceless, so they are skipped; `concepts/claims/index.md` is reserved-*named*
but carries frontmatter, so it counts).

**What is built on a given upstream document:**

```bash
$ okfquery query "
SELECT c.path, s.ordinal
FROM concepts c JOIN sources s USING (path)
WHERE s.resource = 'https://wiki/ok';
" --bundle packages/okfquery/tests/fixtures/messy
┌─────────────────────────┬─────────┐
│          path           │ ordinal │
│         varchar         │  int32  │
├─────────────────────────┼─────────┤
│ concepts/ok/overview.md │       0 │
└─────────────────────────┴─────────┘
```

**Staleness by owning system** (`ordinal = 0` picks the owning source, not a
grounding one — see the ordinal note above):

```bash
$ okfquery query "
SELECT split_part(s.id, ':', 1) AS system,
       count(*) AS concepts,
       min(c.generated_at) AS oldest
FROM concepts c JOIN sources s USING (path)
WHERE s.ordinal = 0
GROUP BY 1 ORDER BY oldest;
" --bundle packages/okfquery/tests/fixtures/messy
┌─────────┬──────────┬──────────────────────────┐
│ system  │ concepts │          oldest          │
│ varchar │  int64   │ timestamp with time zone │
├─────────┼──────────┼──────────────────────────┤
│ wiki    │        3 │ 2026-08-23 00:00:00+00   │
└─────────┴──────────┴──────────────────────────┘
```

Only one system shows up on this fixture: most of the messy bundle's files
exist to exercise a `problems` kind and never got far enough to have a
`sources` entry at all, so they don't join into this query — which is correct,
not a bug in the query.

**Orphans — nothing links to them:**

```bash
$ okfquery query "
SELECT c.path FROM concepts c
WHERE NOT EXISTS (SELECT 1 FROM links l WHERE l.target = c.path);
" --bundle packages/okfquery/tests/fixtures/messy
┌───────────────────────────────────┐
│               path                │
│              varchar              │
├───────────────────────────────────┤
│ concepts/badyaml/overview.md      │
│ concepts/claims/index.md          │
│ concepts/incomplete/overview.md   │
│ concepts/nofence/overview.md      │
│ concepts/notmapping/overview.md   │
│ concepts/unterminated/overview.md │
└───────────────────────────────────┘
```

**Everything that failed to parse** (`--format csv` here, since the default
table renderer truncates long `detail` strings):

```bash
$ okfquery query "SELECT path, kind, detail FROM problems ORDER BY path, kind;" \
    --bundle packages/okfquery/tests/fixtures/messy --format csv
path,kind,detail
concepts/badyaml/overview.md,invalid-yaml,frontmatter is not valid YAML: ParserError
concepts/incomplete/overview.md,bad-links,'links' entry 3 is not a string
concepts/incomplete/overview.md,bad-sources,'sources' entry 0 has no 'resource' (§5.1 requires one)
concepts/incomplete/overview.md,bad-timestamp,'generated.at' 'not-a-date' is not an ISO-8601 datetime
concepts/incomplete/overview.md,missing-required,"missing required OKF keys: title, description"
concepts/nofence/overview.md,no-frontmatter,file does not open a '---' frontmatter fence
concepts/notmapping/overview.md,frontmatter-not-mapping,"frontmatter parses to list, not a mapping"
concepts/unterminated/overview.md,unterminated-frontmatter,frontmatter fence is opened but never closed by a closing '---'
```

## The mirror, and why both obvious joins are wrong

`--mirror <path>` attaches a `mirror` view over a kbforge mirror directory
(`read_json_auto('<mirror>/*.json')`) — the same `CanonicalDocument` slots
`mirror.slot_key` writes, `anchor` struct included. It answers a different
question than the bundle alone can: not "what does this concept say" but "does
it still say what its source currently does."

**Both of the joins you'd reach for first are lossy, in different ways:**

- `sources.id = mirror.doc_id` holds for every shipped connector, but
  `synthesize._source_entry` builds `id` from `anchor.system`/`anchor.native_id`
  while `doc_id` is the mirror's own field — nothing enforces the two agree for
  a third-party connector. Where they diverge, this join produces a silent
  false miss, not an error.
- `sources.content_hash = mirror.anchor.content_hash` as an **equi-join** is not
  the sound alternative either. `kbforge.pipeline.run` calls
  `commit(mirror_path, docs)` right after `kbforge_publish`, and publishing only
  opens a review request — kbforge never merges. Between publish and merge the
  mirror already holds the new hash while the bundle still carries the previous
  run's, so an equi-join on hash returns **zero rows for every concept with a
  review request open** — exactly the ones an audit most wants to see.

The correct form: join on `id` with a **`LEFT JOIN`**, not an inner join, then
**compare** hashes instead of joining on them — an inner join silently drops
every source with no mirror row at all, which is exactly the "never
published" case the query needs to report. A match means current; a mismatch
means an update is sitting in review; a source with no mirror row was never
published. Run for real against the messy fixture (`concepts/ok/overview.md`'s
two sources — an owning `wiki:ok` and a grounding `notes:ok` — plus two other
concepts whose sources have no mirror row at all) with a synthetic
two-document mirror covering only `wiki:ok` and `notes:ok`, one hash left
matching and one changed to simulate an update stuck in review:

```bash
$ okfquery query "
SELECT s.path,
       s.id,
       s.content_hash AS bundle_hash,
       m.anchor.content_hash AS mirror_hash,
       CASE WHEN m.doc_id IS NULL THEN 'never published'
            WHEN s.content_hash = m.anchor.content_hash THEN 'current'
            ELSE 'update in review' END AS status
FROM sources s LEFT JOIN mirror m ON s.id = m.doc_id
ORDER BY s.path, s.id;
" --bundle packages/okfquery/tests/fixtures/messy --mirror /path/to/mirror
┌─────────────────────────────────┬─────────────────┬─────────────┬─────────────┬──────────────────┐
│               path              │       id        │ bundle_hash │ mirror_hash │      status      │
│             varchar             │     varchar     │   varchar   │   varchar   │     varchar      │
├─────────────────────────────────┼─────────────────┼─────────────┼─────────────┼──────────────────┤
│ concepts/claims/index.md        │ wiki:claims     │ h-claims    │ NULL        │ never published  │
│ concepts/incomplete/overview.md │ wiki:incomplete │ NULL        │ NULL        │ never published  │
│ concepts/ok/overview.md         │ notes:ok        │ h-ground    │ h-ground-v2 │ update in review │
│ concepts/ok/overview.md         │ wiki:ok         │ h-own       │ h-own       │ current          │
└─────────────────────────────────┴─────────────────┴─────────────┴─────────────┴──────────────────┘
```

That does not fix the `id` gap above: a connector whose anchor disagrees with
its `doc_id` still misses this join silently. There is no workaround for that
here — only the honest statement of it.

## Not built

- **No full-text search out of the box.** `body` is a plain column; `INSTALL
  fts` and an index over it is a documented recipe, not a feature.
- **No remote bundles.** `--bundle` is a local checkout path; reading one over
  HTTP wants a caching story this package deliberately avoids.
- **No MCP server.** `load()` is already small enough to wrap in one `query`
  tool and a schema resource; nothing here does that yet.
