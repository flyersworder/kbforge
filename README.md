# kbforge

[![PyPI](https://img.shields.io/pypi/v/kbforge.svg)](https://pypi.org/project/kbforge/)
[![CI](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml/badge.svg)](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Agent-first knowledge bases, forged from your systems of record.**

The [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
(OKF) v0.2 standardizes the *artifact at rest* — markdown concept files, frontmatter,
`index.md`, `log.md`. It says nothing about how those bundles get **produced**: how you
pull from a wiki or a CMDB, how you tell a real change from an export timestamp jittering,
how a claim stays traceable to its source, and how an update reaches `main` without a human
losing an afternoon to review.

`kbforge` is the missing half — the production protocol.

| Layer | Standardized by |
|---|---|
| Artifact format | OKF v0.2 |
| **Production protocol** — connectors, canonicalization, diff, provenance, publish | **kbforge** |
| Serving protocol | MCP — or any context database that ingests the bundle |

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
PR/MR rather than opening a second one. Three consequences worth knowing:

- Concepts deleted from the source are deleted from the target repo, provided the
  connector emits an explicit tombstone. Absence never implies a deletion.
- Manual commits on the sync branch are preserved **while its review request is
  open** — a later run builds on the branch rather than resetting it. Once no
  request is open, the next run rebuilds the branch from the default branch and
  those commits are gone. A hand edit to a concept kbforge later regenerates is
  overwritten by that regeneration either way.
- **Close a kbforge review request only by merging it.** The mirror advances on
  every successful publish, so the concepts a request carries are never
  re-proposed. Closing one unmerged discards its contents permanently: the
  target repo simply never gets them, and a published-then-abandoned deletion
  leaves the doc gone from the mirror, so no later run even sees it as a
  removal. To undo an abandoned request, reset **both** the mirror and the
  connector's cursor: delete the mirror directory and, in the state directory
  (`--state`), the connector's `cursor-<connector-name>.json`. Deleting the
  mirror alone does not work for an incremental connector — its cursor still
  points past the abandoned content, so the next `kbforge_fetch` returns only
  what changed since then, which can be little or nothing, and no re-proposal
  happens at all. Only once both are gone does a re-run re-propose everything
  from scratch.

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
- [`docs/design/2026-08-08-okf-02-deferred-decisions.md`](docs/design/2026-08-08-okf-02-deferred-decisions.md)
  — the OKF v0.2 families kbforge does not emit yet (`verified`, `status: deprecated`,
  `stale_after`, footnote attribution) and why each is a decision, not a backlog item.
- [`CHANGELOG.md`](CHANGELOG.md) — release history.

## Related projects

kbforge is one of three *contracts for agents*, split by seam:

- [**ai-agent-contracts**](https://github.com/flyersworder/agent-contracts) — the formal
  spine: resource, temporal, and lifecycle contracts (the seven-tuple kbforge maps onto).
- [**agentic-data-contracts**](https://github.com/flyersworder/agentic-data-contracts) —
  the *consumption* half for **structured** data: domain-driven governance enforced at
  query time. kbforge is the *production* half for **unstructured** knowledge; both
  independently converged on making freshness legible to the agent.

### Not a context database

[OpenViking](https://github.com/volcengine/OpenViking) and its kin sit in the
**serving** row of the table above, not the production row. They ingest documents
and expose them to an agent — OpenViking summarizes each one into retrieval tiers
on write and serves them over a filesystem API and MCP. kbforge produces the
documents such a system serves: an OKF bundle in a git repo is a valid input to
one, so these compose rather than compete.

The difference shows on the *second* pull from a source that mostly did not
change. A context database refreshes a watched resource by re-ingesting it
wholesale — no diff, no changed-set, no proposal a human ever sees. kbforge
[canonicalizes](docs/architecture.md#43-canonicalization-laws-the-load-bearing-part)
first, so an export whose timestamps and ordering jitter reduces to *no change at
all*: no LLM spend, no review request, no merge. Byte-level deduplication further
downstream cannot substitute, because a rendered system-of-record export is rarely
byte-identical across pulls even when nothing about it has meaningfully changed.

That one mechanism is why both the token bill and the review queue stay bounded on
a corpus where most documents are stable — and it is what makes the human gate
affordable rather than ceremonial. Reach for a context database when an agent needs
to *retrieve* from a corpus; reach for kbforge when a corpus has to stay honest to a
system of record that keeps changing, and someone has to be accountable for what it
says.

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
