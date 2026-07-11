# kbforge — Repository Scaffold Design

**Date:** 2026-07-11 · **Status:** approved

## Goal

Turn the `kbforge` folder — currently two design notes and an empty git repo with no
commits — into a working Python project: uv-managed, prek-guarded, CI-checked, and
mirrored to `github.com/flyersworder/kbforge`.

This scaffold is **infrastructure only**. It deliberately ships no protocol code: no
Pydantic models, no Pluggy hookspecs, no pipeline. Those are specified in
`docs/architecture.md` §3/§5/§7 and get their own spec → plan → TDD cycle. Mixing them
into the initial commit would smuggle implementation work into a chore.

Tooling mirrors `flyersworder/agentic-data-contracts`, with one deliberate upgrade
(see "ty hook" below).

## Non-goals

- Implementing `models.py`, `hookspecs.py`, `registry.py`, `pipeline.py`, `canonical.py`,
  `validate.py`, or `kbforge/testing/`.
- Writing any connector (`kbforge-confluence`, `kbforge-servicenow`, …).
- Publishing to PyPI. The `publish` CI job exists but only fires on a GitHub release.

## Design

### 1. Documentation layout

The two notes play different roles and should not sit as peers:

| Was | Becomes | Role |
|---|---|---|
| `kbforge-library-spec.md` | `docs/architecture.md` | **This repo's own architecture.** Matches the agentic-data-contracts convention. |
| `agent-app-knowledge-base-design.md` | `docs/context/knowledge-base-design.md` | The **motivating system** kbforge was extracted from. Context, not spec. |

The `Companion to:` cross-reference in `docs/architecture.md` is updated to the new path
so the link does not break.

### 2. Scrub for public release

The repo goes public, so employer-identifying detail is removed. Exactly two terms
qualify, four occurrences in total:

- The **employer's name** (2 occurrences) → generic phrasing ("org-specific", "a
  regulated / EU-data-residency compliance context").
- An **internal project codename** for the in-house RAG service (2 occurrences) → "an
  internal retrieval stack".

This spec deliberately does not reproduce either term: a scrub document that quotes what
it scrubbed is not a scrub. The `grep` in Verification below is the check that both are
gone, and it must come up empty across the whole tree — this file included.

Public product names — GitLab, LiteLLM, Confluence, ServiceNow, Pydantic AI — are
**kept**. They are not identifying, and removing them would strip the docs of the
concreteness that makes them useful.

### 3. Package

- `src/` layout, hatchling backend. Not cosmetic: with `src/`, `kbforge` is importable
  only after install, so `uv sync` puts it in the venv and Pluggy's
  `load_setuptools_entrypoints("kbforge.connectors")` genuinely exercises entry-point
  discovery in tests. A flat layout would import from CWD and silently bypass it.
- `src/kbforge/__init__.py` exporting `__version__ = "0.1.0"`; `src/kbforge/py.typed`.
- Runtime deps: `pluggy>=1.5`, `pydantic>=2` — the two the architecture is built on.
- Dev deps: `pytest`, `pytest-cov`, `ruff`. **Not `ty`** — the prek hook supplies it.
- `requires-python = ">=3.12"`; `.python-version` pins 3.14 for local dev.
- One smoke test (`tests/test_import.py`) so the CI matrix has something to run and a
  broken package surfaces immediately.

### 4. prek hooks

`.pre-commit-config.yaml` (prek reads it natively; keeps the repo usable by plain
`pre-commit` too):

- `astral-sh/ruff-pre-commit` — `ruff-check --fix`, `ruff-format`.
- `astral-sh/ty-pre-commit` @ `v0.0.58` — the **official** ty hook.

The ty hook is the one deliberate divergence from agentic-data-contracts, which predates
it and carries a hand-rolled `local` hook running `ty check` as a system command. The
official hook shells out to uv's preview `uv check`, which resolves the project's own
`pyproject.toml` dependencies. The local hook could not, which is why
agentic-data-contracts needs `unresolved-import = "ignore"` under `[tool.ty.rules]`.
kbforge does **not** need that suppression, so ty will actually catch a mistyped `pluggy`
import rather than shrug at it. Each hook `rev` transitively pins both the ty version and
a compatible uv version, so the `rev` is the entire pin.

### 5. CI

`.github/workflows/ci.yml`, four jobs, SHA-pinned actions, `permissions: {}` at the top
with per-job grants:

- **lint** — `ruff check`, `ruff format --check`, and `uvx ty check` so type errors gate
  pull requests rather than only local commits.
- **test** — matrix over Python 3.12 / 3.13 / 3.14, `pytest --cov=kbforge`.
- **security** — `uvx uv-secure uv.lock`.
- **publish** — on release only, PyPI trusted publishing (`id-token: write`).

### 6. GitHub

`gh repo create flyersworder/kbforge --public --source=. --push`, single initial commit,
`main` as the default branch.

## Verification

Before the repo is pushed, all of the following must pass locally and be shown, not
asserted:

1. `uv sync` resolves and creates `uv.lock`.
2. `uv run pytest` — smoke test passes.
3. `prek run --all-files` — ruff-check, ruff-format, and ty all pass.
4. A case-insensitive `grep` for the employer name and the internal codename returns
   nothing anywhere in the tree.
