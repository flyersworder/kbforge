# kbforge

[![kbforge](https://img.shields.io/pypi/v/kbforge.svg?label=kbforge)](https://pypi.org/project/kbforge/)
[![kbforge-mcp](https://img.shields.io/pypi/v/kbforge-mcp.svg?label=kbforge-mcp)](https://pypi.org/project/kbforge-mcp/)
[![kbforge-okfquery](https://img.shields.io/pypi/v/kbforge-okfquery.svg?label=kbforge-okfquery)](https://pypi.org/project/kbforge-okfquery/)
[![kbforge-sql](https://img.shields.io/pypi/v/kbforge-sql.svg?label=kbforge-sql)](https://pypi.org/project/kbforge-sql/)
[![CI](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml/badge.svg)](https://github.com/flyersworder/kbforge/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Agent-first knowledge bases, forged from your systems of record.**

kbforge reads your systems of record (a folder of markdown, a git history, a
database view, any MCP server) and proposes an
[Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format/blob/main/SPEC.md)
(OKF) v0.2 bundle as a pull request: one markdown concept per source document,
carrying the frontmatter, links and provenance an agent needs to cite it. You
review and merge; kbforge never does. A re-run when nothing changed costs one
fetch: no model call, no pull request.

## Quickstart

`pip install kbforge`; then, beside a `docs/` of markdown (here two files):

```console
$ kbforge init
wrote kbforge.yaml
next: kbforge plan
keep .kbforge/ between runs: it holds the mirror that makes re-runs free
$ kbforge plan
docs  2 added, 0 modified, 0 removed, 0 drifted, 0 rebuilt → would propose 2 concepts (validation runs at publish)
docs    + local_files:onboarding.md  added
docs    + local_files:refunds.md  added
$ kbforge run
docs  Published: …/.kbforge/out/sync-local_files
$ kbforge run
docs  NoOp: no change detected; no MR opened.
$ kbforge status
docs  last published 2026-10-29 16:32 UTC · no open request
```

The starter file needs no credentials: `stub` copies each body verbatim, and
`dry-run` writes the proposal (concepts plus `MR_BODY.md`, the review note)
under `.kbforge/out/`. `plan` is `run` stopped before synthesis: it fetches and
diffs, writes nothing, and needs only the sources' credentials.

| Command | Does |
|---|---|
| `kbforge init [PATH]` | writes a starter `kbforge.yaml` for the documents in `PATH` (default `./docs`); never overwrites one |
| `kbforge plan [SOURCE...]` | what `run` would propose, and why; writes nothing, calls no model |
| `kbforge run [SOURCE...]` | runs the sources (default: all) in file order |
| `kbforge status` | per source: last publish, open review request, pending chunks |
| `kbforge redo SOURCE` | rolls back the last chunk so the next run proposes it again ([Large first runs](#large-first-runs)) |
| `kbforge reset NAME [--yes]` | forgets one source's local state; its next run proposes everything, as a first run would |
| `kbforge list` | the installed connectors, synthesizers and publishers |

Each reads `./kbforge.yaml` or `--project PATH`. `run` exits 1 if any source
failed (the others still run), 2 if the project is invalid (every problem is
printed and nothing runs), 3 if another run holds the lock, and 0 otherwise.

## The project file

```yaml
# kbforge.yaml: one knowledge base
synthesizer:                        # default: stub (no model)
  name: llm
  model: deepseek/deepseek-v4-flash
  instructions: |
    Write for application managers. Prefer the product's own terms.

publisher:                          # default: dry-run (a local directory)
  name: github
  repo: acme/ev-traction-kb
  token_env: GITHUB_TOKEN

links: links.yaml                   # a path, or the mapping inline
grounding: grounding.yaml

sources:                            # run in this order
  products:
    connector: sql                  # pip install kbforge-sql
    config:
      system: products
      url_env: DENODO_URL
      query: SELECT product_id, product_name, description FROM iv_product
      id: [product_id]
      title: product_name
      text: description
  handbook:
    connector: local_files
    config: { path: ./handbook }
    chunking: { max_concepts: 40 }
    synthesizer: { name: describe, model: deepseek/deepseek-v4-flash }
```

- **Paths are relative to the file.** State lives under `state_dir` (default
  `.kbforge/`) and must persist between runs: in CI, a cache or a committed
  directory. Secrets stay in env vars that `*_env` keys name, and an unknown
  key is an error naming its YAML path.
- **Per-source `synthesizer` and `publisher` replace** the top-level one whole.
  `links` and `grounding` exist only at the top, so every source sees the same.
  Inline, each takes the whole file's contents, top-level key included:
  `links: { links: { "products:p1": [handbook:p1.md] } }`, and `grounding: {
  grounding: {...}, rules: [...] }`.
- **Put a source others ground on first**, so their rebuilds land in the same
  run rather than the next.
- **A system belongs to one source.** `local_files` and `git_commits` each emit
  one fixed system, so point one `local_files` source at a common parent; a
  second aborts with `[source-ownership] ... system 'local_files' is already
  owned by source 'docs'`.
- **One run at a time per mirror.** `run`, `plan`, `redo` and `reset` hold
  `run.lock`; a second exits 3: `Locked: another kbforge run holds
  …/.kbforge/run.lock; nothing was read or written.`
- **Editing a source's config starts it on a fresh cursor**, and `run` prints
  `note: config changed since the last run; starting from a fresh cursor (the
  old one stays in state/, unused).`

**Writing with a model** needs `pip install "kbforge[llm]"` and
`OPENROUTER_API_KEY` (or `api_base:` for a LiteLLM gateway). `llm` writes the
body; editing its `instructions` alone re-synthesizes nothing. `describe` keeps
the body byte-for-byte and has the model write only `description` and `tags`
(optionally from a `tags_vocabulary`), cached per document.

## Publishing to GitHub or GitLab

Set `publisher` to `github` or `gitlab`. Both take `repo` (required), `base`
(default: the repo's default branch), `base_path` (default: repo root),
`branch` (default: `sync/<system>`), `title`, `api_base` (GitHub Enterprise or
self-managed GitLab) and `token_env` (default `GITHUB_TOKEN` / `GITLAB_TOKEN`).

kbforge keeps **one long-lived sync branch and one open review request** per
source system: a later run force-updates the branch and edits the open PR/MR
rather than opening a second. Three consequences:

- A concept deleted from the source is deleted from the target repo, provided
  the connector emits an explicit tombstone. Absence never implies a deletion.
- Manual commits on the sync branch survive **while its request is open**. Once
  none is open, the next run rebuilds the branch from the default branch and
  they are gone; a hand edit to a regenerated concept is overwritten either way.
- **Close a kbforge review request only by merging it.** The mirror advances on
  every publish, so a request's concepts are never re-proposed: closing it
  unmerged discards them. `kbforge reset SOURCE` undoes that, and the next run
  proposes everything from the source again, except a deletion whose request
  was closed unmerged: it is gone from the mirror *and* the source, so that
  concept stays in the bundle until you remove it by hand.

### Large first runs

A cold start or a bulk upstream edit can put hundreds of concepts in one
review request. A source's `chunking: {max_concepts: 40, group_by: <field>}`
publishes one chunk at a time and waits for each request to be merged or closed
before the next. `group_by` can only name a structured field the connector
keeps (`local_files` drops `type`, which comes from synthesis); a field no
document carries groups nothing. To redo a chunk, close its request and run
`kbforge redo SOURCE` before the next `kbforge run`: closing alone discards the
chunk, and a run after closing moves on. See `docs/architecture.md` §7.2.

### Links no source carries

Two taxonomies with no join key, or a requirement that closes a gap on another
slide: declare them in a reviewed `links.yaml`.

```yaml
links:
  planning_deck:gaps/redundant-supply:
    - to: db_apps:applications/777
      note: the application this gap is about
      symmetric: true
    - planning_deck:requirements/diagnostics   # one-way, no note
```

Links resolve by `doc_id`, across systems too, and render in a `## Related`
section (with the note saying why) that an OKF reader follows; connector
`relations` get the same section. A cross-system link lands on each side on
that side's run, and the review note names the other system so its request can
be merged first. For concepts that merely share tags or facets, use `okfquery
related` instead. See `docs/architecture.md` §7.4.

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

## Why you can trust it

- **No change, no proposal.** Sources are canonicalized and diffed against
  the mirror, so a jittering export is no change at all: the run stops before
  synthesis, with no tokens spent and no review request
  ([below](#not-a-context-database)).
- **Nothing ships unvalidated.** Every publish is gated on four agent-facing
  artifact laws (facet well-formedness, link resolvability, anchor presence,
  freshness legibility) and a projection↔files coherence check
  ([§4.4](docs/architecture.md#44-agent-facing-artifact-laws-the-emit-side)).
- **A human merges.** No publisher has a merge method.
- **The order is fixed.** fetch → normalize → mirror → diff → scope →
  synthesize → validate → publish is **not** pluggable, nor are the no-op and
  never-merge rules: pluggable would mean optional. Plugins extend stages, and
  the project file has no key that reaches them.

## Where kbforge fits

OKF standardizes the *artifact at rest*. It says nothing about how a bundle is
**produced**: pulling from a wiki or a CMDB, telling a real change from an
export's jitter, keeping a claim traceable to its source, and getting an update
to `main` without a human losing an afternoon to review. kbforge is that half.

| Layer | Standardized by |
|---|---|
| Artifact format | OKF v0.2 |
| **Production protocol**: connectors, canonicalization, diff, provenance, publish | **kbforge** |
| Serving protocol | MCP, or any context database that ingests the bundle (see [`packages/okfquery`](packages/okfquery) for a DuckDB reader) |

**Status: beta**, still 0.x. Every seam only a real service can check has a
live suite (`--run-live`), not a mock. The largest gap is a first-party
connector for a specific system of record: `kbforge-mcp` gives the capability,
but nothing ships preconfigured for Confluence, ServiceNow, or their kin.

**Companion distributions**, versioned independently; none is required:

| Distribution | Import | What it does |
|---|---|---|
| [`kbforge`](https://pypi.org/project/kbforge/) | `kbforge` | the production protocol: this README |
| [`kbforge-mcp`](https://pypi.org/project/kbforge-mcp/) ([src](packages/kbforge-mcp)) | `kbforge_mcp` | makes any MCP server with a select tool and a read-by-id tool a kbforge **source**, through configuration alone |
| [`kbforge-sql`](https://pypi.org/project/kbforge-sql/) ([src](packages/kbforge-sql)) | `kbforge_sql` | makes any database SQLAlchemy can reach a kbforge **source**, one scoped query per source |
| [`kbforge-okfquery`](https://pypi.org/project/kbforge-okfquery/) ([src](packages/okfquery)) | `okfquery` | reads a published bundle **back**: `okfquery query "SELECT ..."` over concepts, sources, links, and parse problems |

**Plugins, not credentials.** The core ships zero credentialed connectors and
zero CI logic. Real systems of record are plugins, discovered through the
`kbforge.connectors` (and `kbforge.publishers`) entry-point group; deployments
are separate, private repositories. A credentialed GitHub Issues connector with
a real incremental cursor is worked through in
[`examples/github-issues-connector/`](examples/github-issues-connector/).

## Running one source without a project file

`kbforge run --connector NAME` runs one source from flags, under the same
mirror lock. `--set`, `--publish-set` and `--llm-set` take connector, publisher
and synthesizer config as YAML-typed `KEY=VALUE` (`--set max_commits=50` is an
integer), so quote text containing `: ` or ` #` (otherwise exit 2).

```bash
kbforge run --connector local_files --set path=./docs \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state

kbforge run --connector git_commits --set repo=. \
  --synthesizer llm --llm-set model=deepseek/deepseek-v4-flash \
  --llm-set 'instructions="Write for application managers."' \
  --publisher github --publish-set repo=acme/knowledge-base \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

`--synthesizer`, `--chunking FILE`, `--links FILE` and `--grounding FILE`
mirror the project file's keys. `kbforge redo` takes the run's `--connector`,
`--set`, `--publisher`, `--mirror` and `--state`. `plan`, `status` and `reset`
need a project file.

**Moving to a project file.** Write one source per invocation with its
connector config exactly as you pass it (`path: ./docs` is not `path: docs`:
config is hashed into the cursor's name), and set `state_dir` to the directory
holding your mirror and state as `mirror/` and `state/` (or move them there).
Then `kbforge plan`: every source that was up to date reports `no change →
NoOp`, which is the check that it found its own cursor.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — the spec: hookspecs, the
  connector protocol and its laws, the fixed pipeline, the project layer, the
  build sequence. Sections headed **not built** are specification.
- [`docs/context/knowledge-base-design.md`](docs/context/knowledge-base-design.md)
  — the system kbforge was extracted from, with its security model.
- [`docs/design/`](docs/design/) — design notes: the agentic-ingest roadmap, the
  data-contract bridge, the OKF v0.2 keys kbforge does not emit yet, and more.
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
