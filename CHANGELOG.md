# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Fixed

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

[Unreleased]: https://github.com/flyersworder/kbforge/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/flyersworder/kbforge/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/flyersworder/kbforge/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/flyersworder/kbforge/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/flyersworder/kbforge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/flyersworder/kbforge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/flyersworder/kbforge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/flyersworder/kbforge/releases/tag/v0.1.0
