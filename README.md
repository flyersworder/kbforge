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

**Alpha — a working walking skeleton.** The deterministic pipeline runs end to end with
no credentials and no LLM: two built-in connectors (`local_files`, `git_commits`),
canonicalization with a stability law, a replay-safe mirror and diff, a stub synthesizer,
the §4.4 validator gate, and a dry-run publisher. Change detection, the no-op rule, and
incremental sync via a real cursor are exercised by the test suite.

Not built yet: a real LLM synthesizer (the current one copies source text verbatim), a
credentialed system-of-record connector, and a GitHub-PR publisher. See
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

## License

MIT
