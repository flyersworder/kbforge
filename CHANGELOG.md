# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/flyersworder/kbforge/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/flyersworder/kbforge/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/flyersworder/kbforge/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/flyersworder/kbforge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/flyersworder/kbforge/releases/tag/v0.1.0
