# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (kbforge-okfquery)

- `okfquery related <path>` lists what one concept connects to: its links, the
  concepts linking to it (backlinks, which no single file shows), and the
  concepts sharing a value of `--by` facets (default `tags`), ranked by how many
  values they share and naming them. Derived at query time, as OKF §3.1 leaves
  tag views to consumers, so nothing is stored in the bundle and nothing goes
  stale. Lines use `okfquery index`'s format; an empty section prints `(none)`.

## [0.11.1] - 2026-09-19

### Fixed

- The LLM synthesizer failed on long sources (#35). Its default output budget,
  `max_tokens=1500`, cut the model's answer off inside `body` for a source near
  `max_source_chars` (about 1,800 output tokens were needed), and the provider
  still reported `finish_reason=tool_call`, so the run died with a
  ~150-line pydantic-ai traceback and no hint why. In an end-to-end run on two
  Wikipedia pages it failed 3 of 4 attempts; with the fix, 6 of 6 cold runs
  succeeded on the first attempt.
  - `max_tokens` now defaults to 4096.
  - A failed output raises `SynthesisError` naming the concept path and the
    model; when the last response used the whole budget it says the output was
    cut off and suggests `--llm-set max_tokens=`.
  - `output_retries` (default 2, set with `--llm-set output_retries=N`) retries
    a genuinely invalid output. It does not help a truncated one.
  - The CLI prints one line and exits 1. Nothing was published, so the mirror
    and cursor stay put and the next run retries.

## [kbforge-okfquery-v0.2.2] - 2026-09-19

### Fixed

- The README's CI jobs for `okfquery index` now survive a real cold start, both
  found by a second end-to-end run on GitLab:
  - A new bundle repo failed every pipeline until its first concept merged,
    because `okfquery index` rejects a bundle with no `concepts/` (deliberately:
    it is how a wrong `--bundle` shows). The GitLab job now runs only once
    `concepts/**/*.md` exists; the GitHub job exits cleanly until then.
  - Two merges seconds apart made the earlier job's push stale, and it failed
    with a rejected non-fast-forward push. Both jobs now run one at a time,
    index the branch tip rather than their own commit, and retry the push on a
    race.

  Both jobs, as printed, ran on a real forge: a cold start with no concepts
  (skipped, green), then two changes seconds apart (both green, one index
  listing both).

## [kbforge-okfquery-v0.2.1] - 2026-09-19

### Fixed

- The README's GitLab CI job for `okfquery index` was invalid YAML: a script item
  containing `: ` parsed as a mapping, so GitLab rejected the file and every
  pipeline failed. The script is now one block scalar, and the job pushes with
  its own `CI_JOB_TOKEN` (GitLab 17.2+, one project setting) instead of a stored
  personal token. Linted by GitLab and run on a real project: it regenerated a
  deleted `index.md` and pushed it back, and `[skip ci]` stopped a second
  pipeline.
- `okfquery index` percent-encoded every character `urllib.parse.quote` would,
  so a web-source concept's `concepts/@en.wikipedia.org/...` link came out as
  `concepts/%40en.wikipedia.org/...`, a path an agent reading the index as text
  cannot open. Only characters that break or redirect a link (space, `#`, `?`,
  `%`, brackets, parentheses, quotes, controls) are encoded now.

## [kbforge-okfquery-v0.2.0] - 2026-09-19

### Added

- `okfquery index` writes the OKF §8 root `index.md`: one line per concept
  (`* [Title](path) - description`), sectioned by `type` or by `--group-by
  FACET`, so an agent reading a bundle cold starts there and opens only what it
  needs. Deterministic, so a rerun on an unchanged bundle rewrites nothing;
  `--check` writes nothing and exits 1 when the index is missing or stale; a
  hand-written `index.md` (no generated-by marker) is refused without `--force`.
  Meant to run after merge on the bundle repo's default branch: the README has
  GitHub Actions and GitLab CI jobs. kbforge itself cannot emit it, because each
  system publishes on its own sync branch.

## [0.11.0] - 2026-09-19

### Added

- Chunked review: `kbforge run --chunking <file>` splits a change larger than
  `max_concepts` into one review request per chunk (optional `group_by` keeps
  groups together), holds the cursor until the final chunk, and returns
  `Waiting` while a chunk's request is open. `kbforge redo` rolls the last chunk
  back so a closed request can be re-proposed. Publishers gain an optional
  read-only `kbforge_open_request` hook (implemented by `dry-run`, `github`,
  `gitlab`); third-party publishers without it keep working, but cannot be used
  with `--chunking`.

### Fixed

- A concept published before the target of one of its relations existed lost
  that link under the link-resolvability law, and nothing rebuilt it when the
  target arrived, so the link stayed missing until the concept's own source
  changed (#32). Every run now rebuilds such referrers when their target is
  added, as it already did for referrers of a deleted concept. Unchunked runs
  may therefore propose a few more rebuilt concepts than before, each with a
  review note saying why.

## [0.10.0] - 2026-09-18

### Changed

- Development status is now **Beta** (README and the PyPI classifier). The prior
  "alpha — a working walking skeleton" text predated cross-source grounding, both
  companion distributions, and the live suites, and its "not built yet: a
  credentialed system-of-record connector" line had become misleading: `kbforge-mcp`
  makes any credentialed MCP server a source. What is actually missing is a
  *first-party* connector for a named system of record, which the Status section
  now says instead.

### Added

- A pre-publish guard (`.github/scripts/check_release_target.py`) that fails a
  release unless the distribution its tag names was built at the tagged version and
  is absent from PyPI. `skip-existing` protects against a half-published release but
  makes a correct no-op and a forgotten version bump the same green job.
- Grounding rules: `rules:` in the `--grounding` config ground existing concepts
  in matching documents from another system (`for` / `from` / `match` with
  `{field}` placeholders / `newest` / optional `by` date facet), ranked
  newest-first and capped. Explicit grounding is unchanged and outranks rules.
  Every rule-added citation is explained in the review summary. Takes effect
  with `--synthesizer llm`.

### Fixed

- The release tag reaches the publish job through `env:` instead of `${{ }}`
  interpolation into a `run:` block, where a tag containing a quote would have
  executed as shell — on the one job holding `id-token: write`.

### Known limits

- First-seen records start with this release: after upgrading, documents a
  source republishes all look equally new once, until the next genuinely new
  document arrives. An incremental connector that never re-fetches an old
  document never gives it a first-seen record either, so under a rule with no
  usable `by` facet it ranks last (undated), permanently.
- Grounding-rule matching is O(owners × mirror docs × text) every run with
  rules configured: measured 96s/run for 500 owners × 2,000 10KB documents.
  No index or prefilter yet; see the design note §9.

## [kbforge-mcp-v0.2.0] - 2026-09-18

Found by live-testing a Firecrawl-backed web source (`examples/web-source-mcp`).
kbforge itself is unchanged by this release.

### Added

- `read.title_key`, alongside `read.text_key`: the reader owns a document's title,
  so a page reached through two selectors (a curated `static_ids` list and a search)
  keeps one title instead of flipping between runs.

### Fixed

- A run whose reads **all** failed returned zero documents, which the pipeline
  reports as `NoOp` with exit 0 — a revoked key or a rate limit looked exactly like
  "nothing changed". It now raises `ReadsFailed`, naming the first failure.
- Partially failed runs were silent apart from `complete=False`; each failed read
  is now named in a warning on stderr.

## [kbforge-sql-v0.1.0] - 2026-09-18

First release of `kbforge-sql`, a separate distribution: any database SQLAlchemy
can reach becomes a kbforge source through configuration alone. kbforge itself
is unchanged by this release.

### Added

- The `sql` connector. One scoped `SELECT` per source defines the corpus; each
  row, or each set of rows sharing an id when `group` is configured, becomes one
  canonical document rendered as fixed-format markdown, so it serves as both an
  owning and a grounding document. `facets` become filterable frontmatter,
  `exclude` drops volatile columns before hashing, `url_template` gives each
  concept a clickable citation.
- Deletions from a full snapshot: the cursor keeps the id set of the last
  published run, and an id that leaves the result becomes an explicit
  tombstone — the first connector to emit them. An empty result fails the run
  rather than deleting everything, and a removal above `max_removed_fraction`
  (default 0.5) fails until rerun with `KBFORGE_SQL_ALLOW_REMOVALS=<system>`.
- Read-only by construction as far as kbforge can reach: the query runs in a
  transaction that is always rolled back and never committed, and is sent to
  the driver literally (`no_parameters`), so `%` and `:name` in SQL are safe.
  The account's grants are what actually prevent a write; a dedicated
  read-only account is the recommended deployment.
- Credentials by env var name only (`url_env`, optional `password_env`,
  injected unescaped); no error message echoes a URL or password. Connection
  errors are retried with capped backoff; bad SQL fails at once.
- No database driver is a dependency: install the one your engine needs
  (`denodo-sqlalchemy`, `psycopg`, `oracledb`, `pyodbc`, ...).

### Known limits

- Editing any config key moves the cursor slot and resets deletion memory, so
  rows a narrowed query drops leave stale concepts to remove by hand.
- Ids from different sources that render the same bundle path abort the run;
  prefix ids by kind in the query until bundle paths are system-qualified.

## [0.9.0] - 2026-08-23

### Added

- `kbforge-okfquery`, a separate distribution that turns a published OKF v0.2
  bundle into a queryable DuckDB database. `load(bundle, mirror=None)` returns the
  bare `duckdb.DuckDBPyConnection` — an OKF bundle *is* a DuckDB database, so
  wrapping it would only hide `COPY ... TO 'x.parquet'`, `ATTACH`, `INSTALL fts`,
  and joins against your own data. Four tables from the bundle (`concepts`,
  `sources` with `ordinal` preserved so the owning anchor stays distinguishable
  from grounding, `links`, `problems`) plus an optional `mirror` view, and a
  `query` / `shell` / `schema` / `check` CLI. Nothing under its `src/` imports
  kbforge: it reads any OKF v0.2 bundle, kbforge-produced or not, which is what
  makes the layer table's portability claim checkable rather than asserted.
  Import name and console script are `okfquery`; the distribution is namespaced
  because PyPI already carries an unrelated `okf`.
- Parse failures are rows, not exceptions. Every non-reserved scanned file yields
  exactly one `concepts` row — NULLs for what could not be read — plus zero or
  more `problems` rows across ten kinds, so "which files stopped parsing" is a
  query rather than a crash, and `okfquery check` exits 1 on any of them. An
  audit tool that dropped its unreadable files would hide precisely the ones most
  likely to be wrong.
- A live test for the mirror/bundle skew (`--run-live`): publish, merge, edit one
  concept, publish again, then query across the still-open review request. It
  demonstrates that an equi-join on `content_hash` returns nothing for a concept
  under review — because `pipeline.run` advances the mirror at publish while the
  bundle advances only at merge, and kbforge never merges — and that joining on
  `sources.id` and *comparing* hashes reports it correctly. The offline
  round-trip test cannot reach this: `dry_run` never opens a review request.

## [0.8.0] - 2026-08-21

### Added

- The pipeline **aborts**, before synthesis and with no review request opened,
  when two live documents claim one bundle path, and on a relation that crosses
  out of its own system. `concept_path` drops the system prefix, so under the
  shared mirror grounding requires, `wiki:readme` and `notes:readme` render one
  file on two sync branches and whichever merges second silently overwrote the
  other; a cross-system relation was silently dropped under §4.4 law 2 instead.
  System-qualified bundle paths would fix both at the root and are their own
  release — this turns silent loss into a reported `Failure`.
- A live test for a drift-triggered republish against a real forge
  (`--run-live`): two systems into one mirror on their own branches, the
  grounding document changed in its own system, and the owning concept
  re-synthesized into the SAME review request with the new grounding
  `content_hash` in `sources`. Every other live pipeline run is source-triggered;
  a drift run builds from the mirror rather than the fetch, which nothing had
  exercised against a forge.
- A live test that grounds a real model through a shared mirror
  (`--run-live`): two connectors, one `--mirror`, and the published concept
  asserted to cite both systems with the owning anchor first. Every other
  grounding test drives a stub synthesizer, so nothing had checked that a real
  model's multi-source output passes the §4.4 laws.
- Cross-source grounding: a concept still has exactly one owning document, but
  synthesis may now additionally read **grounding** documents from any system,
  write a body informed by them, and cite them in `sources` (§5.1) alongside
  the owning anchor, which is listed first by convention. Declared two ways —
  `CanonicalDocument.grounded_by` on the document, or an operator subject map
  passed as `kbforge run --grounding PATH` — both fully-qualified-`doc_id`
  only. A grounding synthesizer's next run rebuilds a concept whose grounding
  moved in another system, even when its own source did not change, via a
  mirror-side drift sidecar (`mirror/_grounding/`); the no-op rule is restated,
  not weakened, to cover it.
- A grounding sidecar is written through a temp file and read defensively: an
  unreadable one counts as never-grounded, so a process killed mid-write leaves
  a mirror that repairs itself on the next run instead of one that raises on
  every run forever.
- `GroundingSynthesizer`, a protocol separate from `Synthesizer` for exactly
  this capability, so no synthesizer written before this release stops being
  assignable. `LLMSynthesizer` implements it; `StubSynthesizer` does not, and
  the pipeline skips the whole drift scan when a synthesizer does not ground.

### Changed

- `Cursor` gains a core-owned `systems` field, stamped by the pipeline on every
  save and distinct from the connector-owned `payload`. It scopes the grounding
  drift scan on a run whose fetch is empty — the run that needs drift most,
  since drift exists to republish when the owner's own source did not change.
- `LLMConfig.max_source_chars` is again the whole-prompt budget its name and
  docs claim: grounding documents now **share one budget, split evenly**, so a
  grounded prompt is bounded by `2 x max_source_chars` rather than growing to
  `(1 + max_grounding_docs)` times an ungrounded one — 6x at the defaults.
- `docs/architecture.md` §4.4 law 3 documents the multi-source `sources` case
  and the owning-anchor-first convention; §7 gains a §7.1 subsection covering
  cross-source grounding end to end, folded in from
  `docs/design/2026-08-20-cross-source-grounding-design.md`, which now holds
  only the deferred phases and a fold table.
- `docs/architecture.md` §5.4 and §7 now state the deployment layout grounding
  requires, which the per-system claim previously left implicit: one **shared**
  `--mirror` across the per-system runs (drift is derived by reading it whole),
  cursor slots keyed by connector name, and a sync branch per system.

### Fixed

- Cursor slots are keyed by connector name **and a digest of the connector's
  config**, so two instances of one generic connector no longer overwrite each
  other's state on a shared `--state` directory. Name alone let a sibling's
  `systems` leak into a run's scope, and an empty-fetch run then published that
  system's concepts on that system's branch. A pre-keying slot is read once for
  its payload with `systems` dropped, so upgrading costs at most one run's drift
  scan rather than a re-fetch. The connector-owned `payload` was silently
  cross-contaminated the same way before this, on every release. This also closes
  half of the cursor-identity blocker
  `docs/design/2026-08-16-mcp-source-connector-design.md` §10.1 raised against the
  MCP deletion manifest; the load/save asymmetry in that section is untouched and
  still deferred. This also closes
  half of the cursor-identity blocker
  `docs/design/2026-08-16-mcp-source-connector-design.md` §10.1 raised against the
  MCP deletion manifest; the load/save asymmetry in that section is untouched and
  still deferred.
- Grounding declared before the system holding it has synced now survives an
  incremental connector's empty fetches. The scan's *scope* fell back to the
  cursor but the *gate* above it did not, so with no subject map and no sidecar
  the run returned `NoOp()` forever and the concept stayed ungrounded. A
  declaration that resolves to nothing now records an **empty** sidecar rather
  than none, which is what keeps the gate open; dropping the declaration still
  deletes it.
- A bare `grounded_by:` or `relations:` key in source frontmatter is valid YAML
  that parses to `None`, and reached `normalize` as an uncaught `TypeError` that
  killed the whole sync with a traceback.
- The grounding sidecar's temp file gets a unique name. A fixed
  `<slot>.json.tmp` is the same path for every writer, so two runs on the shared
  mirror could replace each other's half-written file into the live slot, and a
  crash left an orphan invisible to both `has_sidecars` and `delete_sidecar`.
- `referrers` is scoped to the run's own systems, like the drift scan and
  `existing`. Under the shared mirror grounding requires, an unscoped
  `referrers` pulled another system's concept into this run, where the scoped
  `existing` then stripped every one of its links as dangling under §4.4 law 2
  and republished it on this run's branch.
- `kbforge run --grounding` on a non-UTF-8 file exits 2 with a sentence rather
  than a traceback (`UnicodeDecodeError` is a `ValueError`, so it slipped past
  the `OSError` guard).
- `grounding.resolve`'s notes name bundle paths, matching every other note in
  `ChangeSummary.grounding_notes`; they named `doc_id`s, so one review body
  spoke two identifier formats.

### Known limits

- **One level deep.** A grounding document's own grounding is not followed.
  Transitive grounding is unbounded fan-in wearing a different hat.
- **The subject map is keyed by `doc_id`,** so a renamed `native_id` silently
  stops matching. `problems_for()`-style config validation should report map
  keys that resolve against neither the mirror nor this run.
- **A run pays O(mirror) whenever the drift scan runs** — that is, when the
  synthesizer grounds *and* something is declared now or a sidecar exists from
  before (architecture.md §7.1). A deployment that declares no grounding keeps
  the cheap no-op, which returns before the mirror is ever loaded.
- **Changing the synthesizer is undetected drift.** Switching stub → LLM, or
  changing the model or prompt, changes what every concept was built from, and
  nothing re-synthesizes for it — `generated.by` records the change without
  forcing one. Pre-existing, and orthogonal to grounding: a rebuild under a
  non-grounding synthesizer *clears* the concept's sidecar rather than leaving a
  stale one, so the two do not compound.
- **Two systems cannot share a `native_id`.** `concept_path` drops the system
  prefix, so the collision aborts the run rather than resolving. Cross-system
  `relations` abort for the same reason. Both lift when bundle paths become
  system-qualified, which rewrites every published path.
- **A tombstoned grounding target is cited one last time** (§7.1), so a review
  request can carry a `sources` entry for a document the same request removes.
  The next run drops it.
- **Mirror writes are not atomic and runs are not locked.** `commit` writes
  document slots in place while `load_all` parses every slot, so two runs
  overlapping on the now-required shared mirror can read a torn file. The
  sidecar got a temp file and `os.replace`; `commit` did not. See
  `docs/design/2026-08-20-event-driven-runs-design.md`, where the per-mirror run
  lock is the one core change proposed.
- **`generated.at` on a drift-triggered rebuild** comes from the owning
  document's `retrieved_at` (`synthesize.py:163`), which for an incremental
  connector may be the mirror's older timestamp rather than this run's.
  Pre-existing behaviour, shared with `referrers`; grounding makes it more
  frequent.
- **A drift rebuild can open a review request with no visible change.** When
  the owning document is re-fetched, `generated.at` moves and the diff is
  never empty. When it comes from the mirror instead — an incremental
  connector that did not re-fetch it — `retrieved_at` is unchanged, so a
  rebuild whose prose lands the same produces a byte-identical file. The
  no-op rule prevents an *unchanged source* from opening a review request; it
  cannot prevent this one, because the grounding genuinely did change.
- **Grounding does not create links.** A cited document is provenance, not
  navigation; if you also want a link, declare a relation.

## [0.7.0] - 2026-08-18

### Added

- `kbforge-mcp`, a separate distribution that turns a **mappable** MCP server with
  a select tool and a read-by-id tool into a kbforge source through configuration.
  Response mapping is protocol-first: MCP's own content-block types are the
  vocabulary, so the common case needs no config at all — but "mappable" is a real
  qualifier on the selector side, not a formality, and the first known-limits entry
  below says which servers it excludes and what to do instead.
- Read-only is structural — the callable tool set *is* the two configured tool
  names — with a `read_only_hint` refusal as defence in depth.

### Changed

- `pyproject.toml` declares a uv workspace; `testpaths` now covers
  `packages/kbforge-mcp/tests`.

### Fixed

- `--run-live` plumbing moved from `tests/conftest.py` to a repo-root `conftest.py`.
  As a sibling rather than an ancestor of `packages/*/tests`, the old location was
  never loaded when such a path was targeted directly, so live tests ran against the
  network with no opt-in — breaking the invariant that `uv run pytest` never touches
  it.

### Known limits

- A server can be perfectly machine-readable and still be unmappable as a
  **selector**. Protocol-first mapping takes ids from resource links or from
  `structuredContent`; GitHub's `search_code` returns machine-readable JSON inside a
  *text* block and declares no `structuredContent`, so it is refused, and kbforge's
  own live test against GitHub uses a configured `static_ids` list instead. "A new
  MCP-backed source is configuration" is unqualified for the reader and conditional
  for the selector. The opt-in flag that would close this is not built (design note
  §10.3).
- An MCP source's own framing survives into the rendered concept, because the
  connector is a retriever and does not edit a source's bytes: AWS's documentation
  server prefixes every document with `AWS Documentation from <url>:`, and a whole
  markdown document's own `#` heading renders below synthesis's `# {title}`. Both
  are emit-side; a fix belongs in synthesis, not in the connector.
- No deletion support. Like `local_files`, the connector re-selects every run and
  emits no tombstones, so a document removed at the source leaves a stale concept
  until the 0.8.0 manifest lands.

## [0.6.0] - 2026-08-16

### Added

- A fetch-side law (`assert_fetch_contract`) run between `normalize` and `diff`:
  `doc_id` must be unique, `native_id` must be non-blank, and an incomplete fetch
  (`FetchResult.complete=False`) may not carry a tombstone. This makes `complete`
  load-bearing for the first time — it was defined but unconsumed, so the
  documented "a partial fetch cannot manufacture removals" invariant previously
  held only because nothing derived removals from absence at all.

### Fixed

- A proposal carrying one path in both `files` and `files_removed` passed
  validation. `_check_projection_coherence` bound `files`↔`concepts` and never
  inspected `files_removed`, so a duplicate `doc_id` where one copy was
  tombstoned reached the publisher as both a write and a delete with
  `run_validators() == []`.
- `StabilityError` and the new `FetchContractError` are reported as messages with
  exit 2 rather than escaping `main()` as a traceback.

### Changed

- **Breaking for connector plugins:** a connector emitting duplicate `doc_id`s,
  a blank `native_id`, or a tombstone on an incomplete fetch now fails the run.
  Both in-tree connectors are unaffected.

## [0.5.0] - 2026-08-08

### Changed

- **BREAKING — provenance is emitted as OKF v0.2 `sources`, not `resource`.**
  kbforge wrote its anchors as a list of dicts under the `resource` key. Both
  OKF v0.1 and v0.2 define `resource` as a *singular optional URI for the
  underlying asset*, so this was a divergence from the day it was written, not
  something v0.2 broke — v0.2 simply gives provenance a correct home. Each
  `ResourceAnchor` now becomes one `sources` entry: the anchor's `url` fills the
  REQUIRED `resource` field, falling back to `system:native_id` when there is
  no URL (honest, but not one of the two kinds §5.1 enumerates — see
  `synthesize._source_entry`); `system:native_id` also becomes
  the stable `id` that per-claim footnote attribution will later join on; and
  `content_hash` rides along as a producer extension key (§4.1), which is what
  keeps a published concept auditable back to the canonical form it came from.
  Law 3 is unchanged in meaning and keeps its `anchor-presence` slug.
- **BREAKING — freshness is emitted as `generated: {by, at}`, not `timestamp`.**
  OKF v0.2 §13.1 supersedes `timestamp`. The modelling matters more than the
  rename: v0.1's `timestamp` meant "last meaningful change" and kbforge was
  filling it with the anchor's `retrieved_at`, a fetch time. v0.2 splits the two
  ideas, and the no-op rule is what makes `retrieved_at` an honest
  `generated.at` for the ordinary case — a concept is re-synthesized only when
  its canonical form changed, so the fetch that rewrote it is its last
  meaningful change. One exception: referrers pulled from the mirror after a
  tombstone are re-rendered to drop a dangling link without their canonical form
  changing, so their `generated.at` under-reports. That is the fail-safe
  direction — a consumer reads such a concept as staler than it is, never
  fresher. `generated.by` follows the §7 actor convention: `kbforge/<version>`
  for the stub synthesizer, `kbforge/<model>` for the LLM one. This does not
  set a trust tier: §5.3 derives the tier from `verified`, which kbforge does
  not emit, so every produced concept reads as *unverified* — and stays so
  after a merge. See the deferred-decisions note on why kbforge must not
  stamp `verified` itself.
- `ConceptFrontmatter` follows: `resources` → `sources`, `freshness` →
  `generated_at`, plus a new `generated_by`. `assemble()` takes a keyword-only
  `generated_by`. See Upgrading for what a third-party synthesizer must change.
- The strict-OKF required set is now `type`, `title`, `description`, `generated`.
  It stays deliberately stricter than OKF §11 conformance, which requires only a
  non-empty `type` — kbforge is a producer, and holds its own output to more than
  it asks of consumers.
- `local_files` reserves `generated` and `sources` so a source document cannot
  collide with them, and keeps the retired `timestamp` and `resource` reserved so
  a v0.1-era source document cannot reintroduce a superseded key as a facet.

### Upgrading

- **Existing bundles migrate lazily, per concept.** Synthesis is scoped to what
  changed, so upgrading does not rewrite a bundle: a concept whose source has not
  changed keeps its v0.1 `resource` and `timestamp` keys until its next real
  change, and a large bundle can sit mixed for a long time. Consumers should
  apply the OKF §13.1 fallbacks during the transition — read `sources` but fall
  back to `resource`, read `generated.at` but fall back to `timestamp`. To
  migrate in one shot instead, reset the mirror **and** the connector's cursor
  (see the README's note on abandoned review requests) so the next run
  re-proposes every concept.
- **Third-party synthesizers must rename before they run.** Anything
  constructing `ConceptFrontmatter` directly needs `resources` → `sources` and
  `freshness` → `generated_at`, and should set `generated_by`.
  `ConceptFrontmatter` now sets `extra="forbid"`, so a retired keyword raises
  at construction rather than silently yielding a projection with no anchors
  and no stamp that only fails three stages later at the gate.

### Notes

- Four v0.2 families are deliberately **not** emitted: `verified` (§5.2),
  `status: deprecated` (§5.4), `stale_after` (§5.5), and footnote attribution
  (§5.1). Each is blocked on a decision rather than on effort — `verified` most
  of all, since the stamp belongs to the merge event kbforge refuses to own, and
  a producer asserting a review that has not happened is a well-formed lie no
  §4.4 law could catch. Reasoning and options:
  [`docs/design/2026-08-08-okf-02-deferred-decisions.md`](docs/design/2026-08-08-okf-02-deferred-decisions.md).

## [0.4.0] - 2026-07-25

### Added

- **Deletion propagation.** A concept removed at the source — via an explicit
  tombstone (`CanonicalDocument.deleted=True`); absence is never inferred as
  deletion — is now actually deleted from the target repo, not just described.
  `ProposedChange.files_removed` carries the removal list, assigned by the
  pipeline after synthesis so an LLM synthesizer cannot delete a file it
  dislikes. Concepts still linking to a deleted one are pulled into scope and
  re-synthesized so their now-dangling links are dropped. Both forge adapters
  intersect removals with what is actually on the base tree first, since
  GitLab (400) and GitHub (422) both reject deleting an absent path.

### Fixed

- **Data loss when a review request was left open across runs.** `publish_to_forge`
  reset the sync branch to the default branch on every run; since the mirror
  advances after each successful publish, a run publishing while a previous
  review request was still open silently rebuilt the branch and lost everything
  the earlier run had put there. The base now resolves to the sync branch
  itself when a request is open, so runs accumulate into one review request;
  when none is open the branch still rebuilds from the default branch, so a
  merged or abandoned request leaves no stale branch behind.

  Note the branch self-heals but the *content* does not, and never did: the
  mirror advances on every successful publish, so a request closed without
  merging discards its contents permanently — those concepts are never
  re-proposed, and a published-then-abandoned deletion is not even seen as a
  removal by a later run. Abandon a review request by merging it, or by
  resetting **both** the mirror and the connector's cursor
  (`<state-dir>/cursor-<connector-name>.json`) — deleting the mirror alone
  does nothing for an incremental connector, whose surviving cursor still
  bounds the next fetch to records past it, so few or none come back and
  nothing is re-proposed.

- **Links to still-published concepts were stripped on an incremental fetch.**
  The set of resolvable link targets handed to synthesis was built from the
  current fetch alone. An incremental connector's fetch need not contain a
  concept that still exists, so a run carrying only a tombstone re-rendered the
  referrer with *every* link removed — including links to concepts that were
  still live and still published. §4.4 law 2 only fails on links that do not
  resolve, never on links that went missing, so it shipped silently. That set is
  now built from the mirror (the published state) unioned with the fetch, minus
  the run's own tombstones.

- **A fully-filtered removal set produced a degenerate commit payload.** With no
  files and every removal already absent from base, the adapters posted
  `actions: []` (GitLab) or `tree: []` (GitHub). Both forges reject it — GitLab
  400 "Provide at least one action, or set allow_empty to true", GitHub 422
  "Invalid tree info" — so every later run failed identically until the source
  changed. Nothing is now committed when the branch already is base; when the
  commit is also what creates the branch, GitLab sends `allow_empty` and GitHub
  commits base's own tree.

  On that create-branch path, the empty commit still opens (or updates) a
  review request, so the rare case where every removal is already gone from
  base is now visible rather than a hard failure: expect a request whose diff
  is empty, whose body can still describe a removal that was, in fact, already
  applied to the target (typically because an earlier run committed it but
  died before the mirror advanced). That is strictly better than the prior
  400/422 — closing or merging it is a no-op — but it is surprising enough to
  flag if you see it.

- The dry-run publisher applies `safe_join` to the paths it writes *and* the
  paths it deletes, so a connector-supplied `native_id` of `../../../etc/foo`
  can no longer reach outside the output directory. The forge publishers
  already guarded this.

## [0.3.0] - 2026-07-25

### Added

- `examples/github-issues-connector/` — a complete worked example of a credentialed
  connector (GitHub issues → OKF concepts) with token auth, pagination, and a real
  incremental cursor, plus a walkthrough README for writing your own connector.
- GitHub (`--publisher github`) and GitLab (`--publisher gitlab`) publishers that
  open or update a real pull/merge request from a `ProposedChange`. No new
  runtime dependencies — both run on stdlib `urllib`. Tokens are read from
  `GITHUB_TOKEN` / `GITLAB_TOKEN` (configurable via `token_env`), never the CLI.
  Both send `Authorization: Bearer`, so either forge accepts a personal,
  project or group access token as well as an OAuth token from `gh`/`glab`.
- `tests/test_forge_live.py` — an opt-in (`--run-live`) suite that publishes to
  real GitHub and GitLab scratch repos and reads the result back through `gh`
  and `glab`, so no assertion depends on the code that wrote the state. Covers
  the sequence offline tests structurally cannot: publish, republish, human
  merge, publish again.
- `--publisher NAME` and `--publish-set KEY=VALUE` CLI flags; `kbforge list` now
  lists publishers.
- `kbforge_validate_publish_config` hookspec so publisher config is checked
  before the pipeline runs.

### Changed

- Publishers are now resolved by name. Previously the first registered plugin
  implementing `kbforge_publish` won, which became order-dependent with more
  than one publisher installed.
- The core design stance is now "zero credentialed *connectors*" — publishing is
  delivery, not a system-of-record integration.

### Fixed

- `kbforge.__version__` reported `0.1.0` for the whole of the 0.2.0 release. It
  is now derived from installed package metadata, so `pyproject.toml` is the
  single source of truth and the two cannot drift again.

## [0.2.0] - 2026-07-19

### Added

- Grounded **LLM synthesizer** — `--synthesizer llm`, behind the optional
  `kbforge[llm]` extra. The model writes only concept prose (title, description,
  body) inside a kbforge-owned structural frame: anchors, links, facets, type, and
  timestamp are assembled deterministically, so the §4.4 validators gate structure
  the model cannot influence. Reached through Pydantic AI's LiteLLM provider, so
  OpenRouter and a self-hosted LiteLLM gateway share one config path, and the API
  key comes only from an environment variable. The deterministic stub remains the
  default; synthesizer selection is a generic `Synthesizer` seam injected into the
  pipeline.

## [0.1.0] - 2026-07-19

First release: a deterministic, credential-free walking skeleton of the kbforge
production protocol.

### Added

- **Fixed pipeline** — `fetch → normalize → mirror → diff → scope → synthesize →
  validate → publish`, run once by `kbforge run`. The order is not pluggable, and
  neither are the two trust guarantees enforced in it: the **no-op rule** (a sync
  that finds no change opens no merge request) and the **never-auto-merge rule**
  (a publisher proposes; it never merges).
- **Canonicalization** with the §4.3 stability law — `normalize` is deterministic,
  clock-free, and volatile-free, so identical input always yields identical content
  hashes. A byte-different but content-identical re-save (CRLF flips, a BOM, a
  re-export) is not a change.
- **Replay-safe mirror and read-only diff** — change is detected against a
  core-owned mirror; the mirror advances only after a run fully succeeds. Absence
  never implies a deletion.
- **Two built-in connectors**, both credential-free:
  - `local_files` — a folder of markdown-with-frontmatter, with an additive
    `ignore_globs` config and always-on defaults (`.venv`, `.git`, `node_modules`,
    tool caches) so pointing at a repository root does not sweep in dependencies.
  - `git_commits` — one concept per commit, with genuine incremental sync: the
    cursor is the last-synced SHA, so a re-run fetches only `<last>..<ref>`.
- **§4.4 agent-facing artifact laws**, enforced as core validators at the `validate`
  stage: facet well-formedness, link resolvability, anchor presence, and freshness
  legibility, plus a projection↔files coherence check. Nothing non-conformant ships.
- **Stub synthesizer** — deterministic, no LLM; reshapes canonical documents into
  OKF concepts and gives the validators real structure to check.
- **Dry-run publisher** — writes the proposed bundle to a local directory under a
  source-named branch; never merges; idempotent.
- **Plugin system** on Pluggy with entry-point discovery: any installed package
  advertising the `kbforge.connectors` or `kbforge.publishers` entry-point group is
  discovered without editing kbforge.
- **CLI** — `kbforge list` shows available connectors; `kbforge run --connector NAME
  --set KEY=VALUE ...` resolves the connector from the registry and takes YAML-typed
  config, with no per-connector knowledge in the CLI.

[Unreleased]: https://github.com/flyersworder/kbforge/compare/v0.11.1...HEAD
[0.11.1]: https://github.com/flyersworder/kbforge/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/flyersworder/kbforge/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/flyersworder/kbforge/compare/v0.9.0...v0.10.0
[kbforge-okfquery-v0.2.2]: https://github.com/flyersworder/kbforge/releases/tag/kbforge-okfquery-v0.2.2
[kbforge-okfquery-v0.2.1]: https://github.com/flyersworder/kbforge/releases/tag/kbforge-okfquery-v0.2.1
[kbforge-okfquery-v0.2.0]: https://github.com/flyersworder/kbforge/releases/tag/kbforge-okfquery-v0.2.0
[kbforge-mcp-v0.2.0]: https://github.com/flyersworder/kbforge/releases/tag/kbforge-mcp-v0.2.0
[kbforge-sql-v0.1.0]: https://github.com/flyersworder/kbforge/releases/tag/kbforge-sql-v0.1.0
[0.9.0]: https://github.com/flyersworder/kbforge/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/flyersworder/kbforge/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/flyersworder/kbforge/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/flyersworder/kbforge/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/flyersworder/kbforge/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/flyersworder/kbforge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/flyersworder/kbforge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/flyersworder/kbforge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/flyersworder/kbforge/releases/tag/v0.1.0
