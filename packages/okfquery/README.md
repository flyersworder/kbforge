# okfquery

SQL over an [OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle, via DuckDB. `okfquery` loads a published bundle into an in-memory DuckDB
connection and hands the connection back — no daemon, no cache, and no
committed artifact except the navigation `index.md` you opt into with
`okfquery index`. It depends on no kbforge code at runtime, so it reads any OKF v0.2
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

## `index`: a front door for agents

```bash
okfquery index --bundle path/to/bundle             # writes path/to/bundle/index.md
okfquery index --bundle . --group-by owner         # sections by a facet, not by type
okfquery index --bundle . --check                  # writes nothing; exit 1 if stale
```

An agent reading a bundle cold starts at the root `index.md` (OKF §8, progressive
disclosure): one line per concept, `* [Title](path) - description`, grouped under
one heading per `type` (or per `--group-by` facet; concepts without it go last).
It then opens only the concepts it needs. Without an index, its first step is
listing `concepts/*/overview.md` and opening files to learn what they are.

- **Derived, never edited.** The file starts with a generated-by marker; a
  hand-written `index.md` is refused unless you pass `--force`.
- **Deterministic.** No timestamps, total ordering: rerunning on an unchanged
  bundle rewrites nothing, so CI commits only when a listing changed.
- **No frontmatter**, not even the optional root `okf_version`: a reserved name
  that opens a fence is a concept, and the index would count itself.

**Run it after merge, not per kbforge run.** Each kbforge system publishes on its
own sync branch; a whole-bundle file on several branches would collide. Generate
it on the bundle repo's default branch instead. The sync branches never touch
it, so they inherit the latest one.

GitHub Actions (commits made with `GITHUB_TOKEN` do not retrigger workflows):

```yaml
on: { push: { branches: [main] } }
permissions: { contents: write }
jobs:
  index:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uvx --from kbforge-okfquery okfquery index --bundle .
      - run: |
          git add index.md && git diff --cached --quiet && exit 0
          git config user.name "okfquery" && git config user.email "okfquery@users.noreply.github.com"
          git commit -m "chore: regenerate index.md" && git push
```

GitLab CI. The script is one block scalar on purpose: a list item such as
`- git commit -m "chore: ..."` contains `: `, which YAML reads as a mapping, and
GitLab rejects the whole file. The push uses the job's own token, so no personal
token is stored anywhere; enable it once under **Settings → CI/CD → Job token
permissions → Allow Git push requests to the repository** (GitLab 17.2+), or via
the API with `ci_push_repository_for_job_token_allowed=true`:

```yaml
index:
  image: ghcr.io/astral-sh/uv:python3.12-bookworm-slim
  rules: [{ if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH' }]
  script:
    - |
      apt-get update -qq && apt-get install -yqq git
      uvx --from kbforge-okfquery okfquery index --bundle .
      git add index.md && git diff --cached --quiet && exit 0
      git -c user.name=okfquery -c user.email=okfquery@example.invalid commit -m "chore: regenerate index.md [skip ci]"
      git push "https://gitlab-ci-token:${CI_JOB_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git" "HEAD:${CI_COMMIT_BRANCH}"
```

On an older GitLab, push with a project access token instead: store it as a
masked CI variable and use `oauth2:${YOUR_TOKEN_VARIABLE}` in place of
`gitlab-ci-token:${CI_JOB_TOKEN}`. `[skip ci]` keeps the bot's commit from
starting another pipeline. Both recipes were run against a real forge.

If the default branch is protected against bot pushes, run
`okfquery index --check` in the **default-branch** pipeline instead, and when it
fails regenerate `index.md` in a small follow-up change of its own. Do not put
the check on merge requests: kbforge's sync branches never touch `index.md`, so
every request that adds or retitles a concept would fail it, and fixing that on
the sync branches makes several systems edit one whole-bundle file, the
collision this job exists to avoid.

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
