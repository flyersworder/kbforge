# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Grounded LLM synthesizer (`--synthesizer llm`, optional `kbforge[llm]` extra):
  the model writes only concept prose inside a kbforge-owned structural frame,
  reached through a LiteLLM provider (OpenRouter or a self-hosted gateway). The
  deterministic stub remains the default.

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

[Unreleased]: https://github.com/flyersworder/kbforge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/flyersworder/kbforge/releases/tag/v0.1.0
