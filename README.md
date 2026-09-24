# kbforge

[![kbforge](https://img.shields.io/pypi/v/kbforge.svg?label=kbforge)](https://pypi.org/project/kbforge/)
[![kbforge-mcp](https://img.shields.io/pypi/v/kbforge-mcp.svg?label=kbforge-mcp)](https://pypi.org/project/kbforge-mcp/)
[![kbforge-okfquery](https://img.shields.io/pypi/v/kbforge-okfquery.svg?label=kbforge-okfquery)](https://pypi.org/project/kbforge-okfquery/)
[![kbforge-sql](https://img.shields.io/pypi/v/kbforge-sql.svg?label=kbforge-sql)](https://pypi.org/project/kbforge-sql/)
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
| Serving protocol | MCP — or any context database that ingests the bundle (see [`packages/okfquery`](packages/okfquery) for a DuckDB reader) |

"Agent-first" is a *checkable* claim, not a downstream hope. kbforge stays a producer —
the agent connects over MCP, which kbforge doesn't own — but every publish is gated on
four **agent-facing artifact laws** (facet well-formedness, link resolvability, anchor
presence, freshness legibility), plus a projection↔files coherence check so nothing
ships unvalidated. That gate is what puts the frontmatter, links, and provenance an
agent's serving layer needs into the artifact. What each law enforces at full versus
reduced strength (and the paths to full strength) is spelled out honestly in
[architecture.md §4.4](docs/architecture.md#44-agent-facing-artifact-laws-the-emit-side).

## Status

**Beta.** The full pipeline runs end to end, and every seam that can only be
checked against a real service has a live suite (`--run-live`) rather than a mock.

- **Sources** — `local_files` and `git_commits` built in, no credentials. Any MCP
  server with a select tool and a read-by-id tool becomes a source through
  configuration alone via [`kbforge-mcp`](packages/kbforge-mcp), live-tested
  against AWS Documentation and GitHub. Any database SQLAlchemy can reach
  becomes a source through configuration alone via
  [`kbforge-sql`](packages/kbforge-sql), one scoped query per source.
  Third-party connectors are discovered through an entry point with no change
  to kbforge.
- **Core** — canonicalization under a stability law, a replay-safe mirror and diff,
  change detection, the no-op rule, incremental sync via a real cursor, and
  cross-source grounding: one owning document per concept, cited alongside
  grounding documents from other systems, and editorial links across systems
  (`--links`).
- **Gate** — the §4.4 emit-side laws plus a projection↔files coherence check, so
  nothing ships unvalidated.
- **Synthesis** — a deterministic stub (default, no LLM), an opt-in grounded LLM
  synthesizer (`--synthesizer llm`, via the `kbforge[llm]` extra), or
  `--synthesizer describe`: the stub's verbatim body with a model-written
  one-sentence description and tags from a vocabulary.
- **Publishers** — `dry-run` built in; GitHub and GitLab opt-in via `--publisher`
  with the token from an env var. kbforge opens review requests and never merges.
- **Reading a bundle back** — [`kbforge-okfquery`](packages/okfquery) loads a
  published bundle into DuckDB.

Still 0.x, and the API can still move: `docs/design/` holds specs for work that is
designed but unbuilt, and sections of [`docs/architecture.md`](docs/architecture.md)
headed **not built** are specification rather than shipped code. The largest known
gap is a first-party connector for a specific system of record — the *capability*
exists through `kbforge-mcp`, but nothing ships preconfigured for Confluence,
ServiceNow, or their kin.

## Companion distributions

This repo is a uv workspace and ships four distributions, versioned independently.
No companion is required to use kbforge, and none is installed with it.

| Distribution | Import | What it does |
|---|---|---|
| [`kbforge`](https://pypi.org/project/kbforge/) | `kbforge` | the production protocol — this README |
| [`kbforge-mcp`](https://pypi.org/project/kbforge-mcp/) ([src](packages/kbforge-mcp)) | `kbforge_mcp` | makes any MCP server with a select tool and a read-by-id tool a kbforge **source**, through configuration alone |
| [`kbforge-sql`](https://pypi.org/project/kbforge-sql/) ([src](packages/kbforge-sql)) | `kbforge_sql` | makes any database SQLAlchemy can reach a kbforge **source**, one scoped query per source |
| [`kbforge-okfquery`](https://pypi.org/project/kbforge-okfquery/) ([src](packages/okfquery)) | `okfquery` | reads a published bundle **back**: `okfquery query "SELECT ..."` over concepts, sources, links, and parse problems |

They sit on opposite sides of the pipeline. `kbforge-mcp` is an ingest-side plugin
that registers a connector entry point; `okfquery` registers nothing and imports no
kbforge code at runtime, so it reads any OKF v0.2 bundle, kbforge-produced or not.

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
`--llm-set instructions="..."` appends deployment-specific guidance to the
fixed prompt. Changing it alone re-synthesizes nothing; it reaches a concept the
next time that concept is rebuilt.

For a stub concept that also carries a real description and tags — without
rewriting its body — use `--synthesizer describe` instead:

```bash
kbforge run --connector local_files --set path=./docs \
  --synthesizer describe --llm-set model=deepseek/deepseek-v4-flash \
  --llm-set 'tags_vocabulary={sic: [SiC], 800v: ["800 V"]}' \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

The body ships byte-for-byte; only `description` and `tags` are model-written,
and the model's output is cached per document so an unchanged source makes no
model call even when its concept is re-rendered.

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

**Large first runs.** A cold start, a new source, or a bulk upstream edit can
produce hundreds of concepts in one review request. `--chunking chunking.yaml`
(`max_concepts: 40`, optional `group_by: <field>`) publishes one chunk at a
time and waits for each request to be merged or closed before the next.
`group_by` reads the connector's structured fields, so it can only name a
field the connector keeps: `local_files`, for one, drops `type` (the OKF type
comes from synthesis), and a field no document carries groups nothing. To
redo a chunk after fixing the taxonomy or exemplars, close its request and run
`kbforge redo` with the same `--connector`, `--set`, `--publisher`,
`--mirror` and `--state`, before the next `kbforge run`: closing alone
discards the chunk, and a run after closing moves on to the next one. Keep a
connector instance either always chunked or never; redo after an unchunked run
would roll back a stale record. See `docs/architecture.md` §7.2.

**Links no source carries.** Two taxonomies with no join key, or a requirement
that closes a gap on another slide: declare them in a reviewed `links.yaml` and
pass `--links links.yaml`.

```yaml
links:
  planning_deck:gaps/redundant-supply:
    - to: db_apps:applications/777
      note: the application this gap is about
      symmetric: true
    - planning_deck:requirements/diagnostics   # one-way, no note
```

Links resolve by `doc_id`, across systems too. They render in a `## Related`
section at the end of each concept, which an OKF reader follows and which
holds the note that says why the two relate; connector `relations` get the
same section. Each system's run rebuilds only its own concepts, so a
cross-system link lands on each side on that side's run. The review note names
the other system, so its request can be merged first. Every system's run that
shares a mirror must pass the same `--links`: a run without it strips that
system's editorial links on its next run. For concepts that share tags or
facets, use `okfquery related` rather than declaring links. See
`docs/architecture.md` §7.4.

## Consuming a bundle

A merged bundle needs no server: OKF is markdown with frontmatter, meant to be
read by agents without an SDK. Point the agent at the bundle repo's **default
branch** (it holds only reviewed, merged concepts), through whichever access it
has:

| Agent | Access |
|---|---|
| has a shell (Claude Code, an Agent SDK app) | a checkout; its own read, glob and grep tools |
| has no filesystem | the GitHub or GitLab MCP server, reading files on the default branch |
| needs aggregate answers (who owns what, what is stale, what is built on source X) | [`okfquery query`](packages/okfquery) over a checkout |

Give it a front door: run [`okfquery index`](packages/okfquery#index-a-front-door-for-agents)
after each merge, so the root `index.md` lists every concept in one line
(`* [Title](path) - description`) and the agent opens only what it needs. The
okfquery README has the CI job. Then tell the agent how to read it, for example:

> Answer from the knowledge base at `<path or repo>`. Start at `index.md`, open
> only the concepts you need, and follow their links. Cite each claim with the
> concept path and its `sources` ids, and check `generated.at`: say when a
> concept is old. If the knowledge base does not cover something, say so rather
> than guessing.

In a trial on a five-concept bundle, a small model given only that prompt read
`index.md` and four concepts, answered with owners and freshness, and said
plainly that the refund procedure it was asked about was not in the bundle,
which was true: that concept's review request had not merged.

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
  hookspecs, the connector protocol and its canonicalization laws, the fixed pipeline, the
  §4.4 emit-side laws, and the build sequence. Sections headed **not built** are
  specification rather than shipped code.
- [`docs/context/knowledge-base-design.md`](docs/context/knowledge-base-design.md) — the
  system kbforge was extracted from: an OKF knowledge base for application managers served
  over MCP, including the security model and a literature review.
- [`docs/design/2026-07-19-agentic-ingest-design.md`](docs/design/2026-07-19-agentic-ingest-design.md)
  — the roadmap for agentic fetch, the refresh model, and KB bootstrap.
- [`docs/design/2026-07-18-datacontract-bridge-design.md`](docs/design/2026-07-18-datacontract-bridge-design.md)
  — how kbforge bridges to `agentic-data-contracts` via the OKF bundle (future, cross-project).
- [`docs/design/2026-08-08-okf-02-deferred-decisions.md`](docs/design/2026-08-08-okf-02-deferred-decisions.md)
  — the OKF v0.2 families kbforge does not emit yet (`verified`, `status: deprecated`,
  `stale_after`, footnote attribution) and why each is a decision, not a backlog item.
- [`docs/design/2026-08-23-okfquery-design.md`](docs/design/2026-08-23-okfquery-design.md)
  — why `okfquery` is a separate distribution rather than a `kbforge query` subcommand,
  its schema, and the two lossy mirror joins a reader has to know about.
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

That suite covers two levels. Most tests hand-build a `ProposedChange` and pin
the publisher adapters. One drives `kbforge.pipeline.run` end to end against a
real forge and pins the *composition* — that an unchanged source is still a
no-op, and that a later run accumulates into the same review request instead of
rebuilding the branch and dropping concepts an earlier run put there. Those three
pieces of state (mirror, cursor, open-request detection) can each be correct
while the whole is not, which is exactly how the 0.3.0 data-loss bug passed a
clean offline suite.

```bash
GITHUB_TOKEN=$(gh auth token) \
GITLAB_TOKEN=$(glab config get token --host gitlab.com) \
KBFORGE_LIVE_GITHUB_REPO=you/kbforge-live-test \
KBFORGE_LIVE_GITLAB_REPO=you/kbforge-live-test \
uv run pytest tests/test_forge_live.py --run-live
```

## License

MIT
