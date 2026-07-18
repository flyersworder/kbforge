# kbforge

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
the agent connects over MCP, which kbforge doesn't own — but the artifact it emits is held
to four **agent-facing artifact laws** (facet survival, link resolvability, anchor
presence, freshness legibility) enforced in the pipeline, so the frontmatter, links, and
provenance an agent's serving layer needs are guaranteed to be there. See
[architecture.md §4.4](docs/architecture.md#44-agent-facing-artifact-laws-the-emit-side).

## Status

**Pre-alpha.** The architecture is specified; the implementation is not written yet. This
repository currently contains the design and the project scaffolding. Follow
[`docs/architecture.md`](docs/architecture.md) for what is being built.

## Design stance

The core ships **zero connectors, zero credentials, zero CI logic.** Connectors are
plugins, discovered through the `kbforge.connectors` entry-point group; deployments are
separate, private repositories. The interface is the product.

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
- [`docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md`](docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md)
  — why the artifact contract exists and how the four emit-side laws are enforced.
- [`docs/superpowers/specs/2026-07-18-datacontract-bridge-design.md`](docs/superpowers/specs/2026-07-18-datacontract-bridge-design.md)
  — how kbforge bridges to `agentic-data-contracts` via the OKF bundle (future, cross-project).

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
