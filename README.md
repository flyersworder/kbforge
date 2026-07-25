# kbforge

[![PyPI](https://img.shields.io/pypi/v/kbforge.svg)](https://pypi.org/project/kbforge/)
[![CI](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml/badge.svg)](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Agent-first knowledge bases, forged from your systems of record.**

The [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(OKF) v0.1 standardizes the *artifact at rest* — markdown concept files, frontmatter,
`index.md`, `log.md`. It says nothing about how those bundles get **produced**: how you
pull from a wiki or a CMDB, how you tell a real change from an export timestamp jittering,
how a claim stays traceable to its source, and how an update reaches `main` without a human
losing an afternoon to review.

`kbforge` is the missing half — the production protocol.

| Layer | Standardized by |
|---|---|
| Artifact format | OKF v0.1 |
| **Production protocol** — connectors, canonicalization, diff, provenance, publish | **kbforge** |
| Serving protocol | MCP |

"Agent-first" is a *checkable* claim, not a downstream hope. kbforge stays a producer —
the agent connects over MCP, which kbforge doesn't own — but every publish is gated on
four **agent-facing artifact laws** (facet well-formedness, link resolvability, anchor
presence, freshness legibility), plus a projection↔files coherence check so nothing
ships unvalidated. That gate is what puts the frontmatter, links, and provenance an
agent's serving layer needs into the artifact. What each law enforces at full versus
reduced strength (and the paths to full strength) is spelled out honestly in
[architecture.md §4.4](docs/architecture.md#44-agent-facing-artifact-laws-the-emit-side)
and the [artifact-contract spec](docs/design/2026-07-18-agent-facing-artifact-contract-design.md) §5.1.

## Status

**Alpha — a working walking skeleton.** The deterministic core runs end to end with no
credentials: two built-in connectors (`local_files`, `git_commits`), canonicalization
with a stability law, a replay-safe mirror and diff, the §4.4 validator gate, and a
dry-run publisher, plus change detection, the no-op rule, and incremental sync via a
real cursor, all exercised by the test suite. Two credentialed publishers, GitHub and
GitLab, are also available (opt-in via `--publisher`, token from an env var). Synthesis ships in
two forms: a deterministic stub (the default, no LLM) and an opt-in grounded LLM
synthesizer (`--synthesizer llm`, via the `kbforge[llm]` extra).

Not built yet: a credentialed system-of-record connector. See
[`docs/architecture.md`](docs/architecture.md) for the full map.

## Quickstart

```bash
pip install kbforge
kbforge list                       # show available connectors

kbforge run \
  --connector local_files \
  --set path=./docs \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

Re-running with no source change is a no-op — no merge request is opened. Point
`--connector git_commits --set repo=.` at a git repository to sync commit history
incrementally instead. Config values are YAML-typed, so `--set max_commits=50` is an
integer and `--set 'ignore_globs=[drafts]'` is a list.

To synthesize real prose instead of the deterministic stub, install the LLM extra
and select the synthesizer (config values are YAML-typed; the API key comes from an
env var, never the CLI):

```bash
pip install "kbforge[llm]"
export OPENROUTER_API_KEY=...        # or point --llm-set api_base=... at a gateway
kbforge run --connector local_files --set path=./docs \
  --synthesizer llm --llm-set model=deepseek/deepseek-v4-flash \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

The synthesizer reaches models through a LiteLLM provider, so OpenRouter and a
self-hosted LiteLLM gateway share one config path.

## Publishing to GitHub or GitLab

The default publisher writes the proposal to a local directory. To open a real
pull request or merge request instead, select a forge publisher and give it a
repo. The token comes from an env var, never the CLI:

```bash
export GITHUB_TOKEN=...            # or GITLAB_TOKEN
kbforge run --connector local_files --set path=./docs \
  --publisher github --publish-set repo=acme/knowledge-base \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

Both publishers accept the same config: `repo` (required), `base` (default: the
repo's default branch), `base_path` (a subdirectory, default: repo root),
`branch` (default: `sync/<system>`), `title`, `api_base` (point it at GitHub
Enterprise or a self-managed GitLab), and `token_env`.

kbforge maintains **one long-lived sync branch and one open review request** per
source system: a later run force-updates that branch and edits the existing
PR/MR rather than opening a second one. Two consequences worth knowing:

- Manual commits pushed onto the sync branch are discarded by the next run.
- Concepts deleted from the source are not deleted from the target repo; files
  absent from a run are inherited from the base branch.

kbforge never merges. No publisher has a merge method.

## Design stance

The core ships **zero credentialed connectors and zero CI logic.** The two built-in
connectors need no credentials and serve as references; real systems of record are
plugins, discovered through the `kbforge.connectors` (and `kbforge.publishers`)
entry-point group without editing kbforge — deployments are separate, private
repositories. The interface is the product.

```toml
# in a third-party package's pyproject.toml — discovered automatically once installed
[project.entry-points."kbforge.connectors"]
myservice = "my_package:connector"
```

A complete worked example — a credentialed GitHub Issues connector (~160 lines) with
token auth, pagination, and a real incremental cursor — is in
[`examples/github-issues-connector/`](examples/github-issues-connector/).

The pipeline order — fetch → normalize → mirror → diff → scope → synthesize → validate →
publish — is deliberately **not** pluggable, and neither are the no-op rule or the
never-auto-merge rule. Those are the trust guarantees; making them pluggable would make
them optional. Plugins extend stages. They cannot reorder or remove them.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — package architecture, the Pluggy
  hookspecs, the connector protocol and its canonicalization laws, the fixed pipeline, and
  the conformance test kit.
- [`docs/context/knowledge-base-design.md`](docs/context/knowledge-base-design.md) — the
  system kbforge was extracted from: an OKF knowledge base for application managers served
  over MCP, including the security model and a literature review.
- [`docs/design/2026-07-18-agent-facing-artifact-contract-design.md`](docs/design/2026-07-18-agent-facing-artifact-contract-design.md)
  — why the artifact contract exists and how the four emit-side laws are enforced.
- [`docs/design/2026-07-19-agentic-ingest-design.md`](docs/design/2026-07-19-agentic-ingest-design.md)
  — the roadmap for agentic fetch, the refresh model, and KB bootstrap.
- [`docs/design/2026-07-18-datacontract-bridge-design.md`](docs/design/2026-07-18-datacontract-bridge-design.md)
  — how kbforge bridges to `agentic-data-contracts` via the OKF bundle (future, cross-project).
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

## Related projects

kbforge is one of three *contracts for agents*, split by seam:

- [**ai-agent-contracts**](https://github.com/flyersworder/agent-contracts) — the formal
  spine: resource, temporal, and lifecycle contracts (the seven-tuple kbforge maps onto).
- [**agentic-data-contracts**](https://github.com/flyersworder/agentic-data-contracts) —
  the *consumption* half for **structured** data: domain-driven governance enforced at
  query time. kbforge is the *production* half for **unstructured** knowledge; both
  independently converged on making freshness legible to the agent.

## Development

```bash
uv sync --all-extras --dev   # create the venv and install
prek install                 # ruff + ty on every commit
uv run pytest
```

The default suite never touches the network. Tests that call a real external
service are marked `live` and skipped unless you pass `--run-live`.

The forge publishers have a live suite because their offline tests can only
assert what we *meant* to send — a real forge is the only thing that can say
the intent was right. It needs a throwaway repo on each forge and the two CLIs
(`gh`, `glab`) authenticated; each run writes under a fresh `live/<run-id>/`
prefix, so nothing accumulates and no repo is ever deleted.

```bash
GITHUB_TOKEN=$(gh auth token) \
GITLAB_TOKEN=$(glab config get token --host gitlab.com) \
KBFORGE_LIVE_GITHUB_REPO=you/kbforge-live-test \
KBFORGE_LIVE_GITLAB_REPO=you/kbforge-live-test \
uv run pytest tests/test_forge_live.py --run-live
```

## License

MIT
