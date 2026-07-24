---
type: design-note
title: kbforge — Forge Publisher (GitHub & GitLab)
description: A credential-carrying publisher that turns a ProposedChange into a real pull request or merge request on GitHub or GitLab, sharing one orchestration across two adapters, with never-auto-merge enforced by the interface rather than by discipline.
tags: [okf, publisher, github, gitlab, pull-request, merge-request, producer, trust]
timestamp: 2026-07-24T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge — Forge Publisher (GitHub & GitLab)

**Status:** Draft v0.1 · **Amends:** [`../architecture.md`](../architecture.md) §0 (design
stance) and §5.2 (publisher family)
**Related:** [`2026-07-18-agent-facing-artifact-contract-design.md`](2026-07-18-agent-facing-artifact-contract-design.md)
— the §4.4 validator gate runs *before* publish, so anything this note ships has already
passed the artifact laws. This note owns only the last hop.

## 1. Problem

The pipeline runs end to end, but `publish` is still a stub. `DryRunPublisher` writes the
proposal to a local directory and its own comment says what that means:
`# a path, not a merge — never merges`. The README's central promise — "how an update
reaches `main` without a human losing an afternoon to review" — currently terminates in a
folder on disk.

This note designs the real publisher, for **both GitHub and GitLab**, as the last piece of
the walking skeleton.

Two structural problems surface the moment a second publisher exists, and both must be
fixed here:

1. `_publisher()` (`src/kbforge/__main__.py:37-41`) returns **the first plugin carrying a
   `kbforge_publish` attribute**. With one publisher that is fine; with three it makes
   publisher selection depend on plugin registration order — a silent, non-deterministic
   choice of where someone's knowledge base gets written.
2. `publish_config={"out_dir": args.out}` (`__main__.py:136`) is hardcoded to the dry-run
   publisher's single need. A forge publisher needs repo, base branch, target path, and a
   token env var.

## 2. Decisions

Settled during brainstorming; recorded here so the implementation does not relitigate them.

| Question | Decision |
|---|---|
| Placement | **In core.** Publishing is delivery, not a system-of-record integration. |
| Dependencies | **None.** stdlib `urllib.request`; no extra is required. |
| Code sharing | **One orchestration, two adapters** behind a semantic `ForgeClient` protocol. |
| Re-run with an open PR | **Update it in place** — one long-lived sync PR per source system. |
| Target path | **Configurable `base_path`, default repo root.** |
| Merging | **Never.** No merge method exists on the protocol or either adapter. |

### 2.1 Why no extras, despite the placement decision

The brainstorm chose "in core, behind extras", by analogy with `kbforge[llm]`. Hand-rolling
~5 REST calls per forge over stdlib removes the premise: there is no dependency for an
extra to install. Declaring `kbforge[github] = []` would be ceremony that installs nothing
and implies a cost that isn't there.

The *intent* behind that choice — publishing lives in core, credentials never do — is
preserved exactly: the publisher ships in core, and the token is read from an environment
variable named by config, never accepted on the command line.

### 2.2 Amendment to the §0 design stance

`architecture.md` §0 currently reads: *"the core ships zero connectors, zero credentials,
zero CI logic."* Taken literally, this note violates it.

The stance is amended to **"zero credentialed _connectors_"**. What it protects is that
kbforge does not privilege any particular system of record — that connectors stay plugins
and the interface stays the product. A publisher is the delivery mechanism for the
producer's own output, not an integration with someone's wiki. Shipping zero working
publishers does not defend the stance; it only means nobody can use kbforge without first
writing one.

`dry_run.py`'s docstring (*"a real GitHub/GitLab publisher is a separate plugin"*) is
superseded and updated in the same change.

## 3. Architecture

Four new modules under `src/kbforge/publishers/`, plus one extraction.

| Module | Responsibility | Depends on |
|---|---|---|
| `_http.py` | `request(method, url, token, ...)` over `urllib.request`; JSON in/out; raises `ForgeError` on non-2xx | stdlib |
| `summary.py` | `summary_md(summary)` — extracted from `dry_run.py` | `models` |
| `forge.py` | `ForgeConfig`, the `ForgeClient` protocol, `publish_to_forge()` | `_http`, `summary`, `models` |
| `github.py` | `GitHubClient` + `GitHubPublisher` (name `github`) | `forge`, `_http` |
| `gitlab.py` | `GitLabClient` + `GitLabPublisher` (name `gitlab`) | `forge`, `_http` |

`summary_md` moves out of `dry_run.py` because both publishers need it, and its role
changes: dry-run writes it to `MR_BODY.md` for lack of anywhere better; on a real forge it
*is* the PR description, which is the shape it always had. `dry_run.py` imports it and
otherwise stays as-is.

### 3.1 The `ForgeClient` protocol

```python
class ForgeClient(Protocol):
    def default_branch(self) -> str: ...
    def put_files(self, branch: str, base: str, files: dict[str, str], message: str) -> None: ...
    def find_open_pr(self, branch: str) -> str | None: ...          # PR/MR id, or None
    def create_pr(self, branch: str, base: str, title: str, body: str) -> str: ...  # -> URL
    def update_pr(self, pr_id: str, title: str, body: str) -> str: ...             # -> URL
```

Every method names an intention, not a REST endpoint. That is load-bearing rather than
stylistic: the two forges decompose "commit these files" completely differently — GitLab
does it in one call, GitHub in four or five — so an abstraction pitched at git plumbing
(`create_blob`, `get_ref`) would fit GitHub and be meaningless for GitLab.

**`put_files` contract:** *reset `branch` to `base`, then apply exactly `files` as a single
commit.* Both forges support this natively. Files present in `base` but absent from `files`
are **inherited**, not deleted (see §8).

This force-reset semantics is what makes "update the open PR" nearly free: the branch always
holds exactly one kbforge commit on top of base, so creating and updating are the same code
path, with no incremental-diff state to reason about.

PR identifiers are opaque to `forge.py` and typed `str`: each adapter stringifies its native
id (GitHub's numeric `number`, GitLab's `iid`) on the way out of `find_open_pr` and
interpolates it back into a URL in `update_pr`. The orchestration never parses one.

### 3.2 Publisher registration

`GitHubPublisher` and `GitLabPublisher` each implement `kbforge_publisher_info` and
`kbforge_publish`, construct their client from `ForgeConfig`, and delegate to
`publish_to_forge`. Both register in `registry.py` alongside `DryRunPublisher`.

## 4. Configuration

`ForgeConfig` is a frozen dataclass, following the `LLMConfig` shape
(`llm_synthesizer.py:50-64`) that already governs credentialed config in this codebase.

| Field | Default | Meaning |
|---|---|---|
| `repo` | *(required)* | `owner/name` (GitHub) or `group/project` (GitLab) |
| `base` | `""` | Base branch; `""` resolves the repo default via API |
| `base_path` | `""` | Subdirectory prefix in the target repo; `""` = repo root |
| `branch` | `""` | Sync branch; `""` uses `change.branch_hint` (`sync/<system>`) |
| `title` | `"kbforge: knowledge base sync"` | PR/MR title and commit message |
| `api_base` | per-forge | `https://api.github.com` / `https://gitlab.com/api/v4`; override for GitHub Enterprise or self-managed GitLab |
| `token_env` | per-forge | `GITHUB_TOKEN` / `GITLAB_TOKEN` |

`branch_hint` is already `sync/{system}` (`synthesize.py:97`) — stable across runs, with no
timestamp or hash — which is exactly the durable branch identity the one-long-lived-PR model
needs. `branch` exists to override it when two kbforge deployments target one repo.

**`validate_config() -> list[str]`** returns problems in the `LLMConfig.validate_env` idiom,
and the CLI checks it *before* the pipeline starts, so a bad config fails in under a second
rather than after a full fetch and synthesize. It rejects:

Reaching it from the CLI requires one addition to `PublisherSpec`:
`kbforge_validate_publish_config(config) -> list[str]`, mirroring the connector family's
existing `kbforge_validate_config`. The CLI calls it through `getattr` with a `[]` fallback,
so third-party publishers that predate the hook keep working and simply skip
pre-validation.

- `repo` empty, or fewer than two non-empty `/`-separated segments. *At least* two, not
  exactly two: GitLab projects nest inside subgroups (`group/subgroup/project`), while
  GitHub is always `owner/name`. GitHub's `{owner}` is the first segment either way.
- `base_path` absolute, or containing a `..` component
- the env var named by `token_env` unset or empty

The token is read from the environment at request time and **never** accepted via `--set`
or `--publish-set`.

### 4.1 Path safety

`safe_join(base_path, rel)` is the only way a path reaches a forge:

1. Reject if either argument is absolute or contains a `..` path component.
2. Join with `/` and normalize (POSIX semantics; forge APIs take POSIX paths regardless of
   the host OS).
3. Reject if the result still escapes the prefix.

Both `base_path` (user config) and every key of `change.files` (connector/synthesizer
output) pass through it. Validating config at load time is not sufficient on its own: file
keys are produced downstream and are equally capable of naming
`../../.github/workflows/deploy.yml`.

## 5. The publish sequence

The whole of `forge.py`'s logic:

```
base   = cfg.base   or client.default_branch()
branch = cfg.branch or change.branch_hint
files  = {safe_join(cfg.base_path, rel): body for rel, body in change.files.items()}
body   = summary_md(change.summary)

client.put_files(branch, base, files, message=cfg.title)
pr = client.find_open_pr(branch)
return client.update_pr(pr, cfg.title, body) if pr else client.create_pr(branch, base, cfg.title, body)
```

**Never-auto-merge is enforced structurally.** The protocol has no merge method and neither
adapter implements one. The guarantee cannot be violated without deliberately widening the
interface — it does not depend on anyone remembering it.

### 5.1 GitHub adapter

Auth: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`.

| Operation | Calls |
|---|---|
| `default_branch` | `GET /repos/{repo}` → `.default_branch` |
| `put_files` | `GET /repos/{repo}/commits/{base}` → `.sha`, `.commit.tree.sha`; `POST /repos/{repo}/git/trees` `{base_tree, tree:[{path, mode:"100644", type:"blob", content}]}`; `POST /repos/{repo}/git/commits` `{message, tree, parents:[base_sha]}`; `PATCH /repos/{repo}/git/refs/heads/{branch}` `{sha, force:true}`, falling back to `POST /repos/{repo}/git/refs` `{ref:"refs/heads/{branch}", sha}` on 422 (ref does not exist yet) |
| `find_open_pr` | `GET /repos/{repo}/pulls?head={owner}:{branch}&state=open` → first `.number` or `None` |
| `create_pr` | `POST /repos/{repo}/pulls` `{title, head:branch, base, body}` → `.html_url` |
| `update_pr` | `PATCH /repos/{repo}/pulls/{number}` `{title, body}` → `.html_url` |

`{owner}` in the `find_open_pr` query is the first segment of `repo` — kbforge always pushes
the sync branch to the target repo itself, never to a fork, so the head owner and the repo
owner are the same by construction.

Fetching `/repos/{repo}/commits/{base}` yields both the commit SHA and its tree SHA in one
call, avoiding a separate ref lookup. File contents go inline in the tree entries, so no
blob calls are needed.

### 5.2 GitLab adapter

Auth: `PRIVATE-TOKEN: <token>`. The project id is the URL-encoded path
(`group/project` → `group%2Fproject`).

| Operation | Calls |
|---|---|
| `default_branch` | `GET /projects/{id}` → `.default_branch` |
| `put_files` | `POST /projects/{id}/repository/commits` `{branch, start_branch:base, force:true, commit_message, actions:[{action:"create", file_path, content}]}` |
| `find_open_pr` | `GET /projects/{id}/merge_requests?source_branch={branch}&state=opened` → first `.iid` or `None` |
| `create_pr` | `POST /projects/{id}/merge_requests` `{source_branch, target_branch, title, description}` → `.web_url` |
| `update_pr` | `PUT /projects/{id}/merge_requests/{iid}` `{title, description}` → `.web_url` |

`force: true` bases the commit on `start_branch` and overwrites the target branch, which is
why `action: "create"` is always correct — the branch is reset first, so no file exists to
collide with. Content is sent as UTF-8 text (`encoding` defaults to `text`); OKF bundles are
markdown, so base64 is not needed.

## 6. Error handling

`_http.request` raises `ForgeError(status, url, body)` on any non-2xx response. The
publisher does not catch it.

**Retry-safety falls out of existing pipeline ordering.** `pipeline.py:123-125` calls
`kbforge_publish` *before* `commit(mirror_path, docs)`. If publishing raises, the mirror
never advances, the cursor is never saved, and the next run recomputes the identical change
and retries. Had the publisher swallowed the error and returned a placeholder URL, the
mirror would advance and the change would be lost silently and permanently.

- **Partial failure.** `put_files` succeeds, `create_pr` fails: the branch exists with
  content but no PR. The next run force-resets the same branch and retries the PR,
  converging. The operation is *idempotent, not atomic* — and the idempotency is what makes
  the non-atomicity safe. No compensating cleanup is needed or attempted.
- **Token leakage.** `ForgeError` carries status, URL, and response body only. Request
  headers are never captured, so the token cannot reach a log, a traceback, or CI output.
- **Exit codes.** Config problems return `2`, matching the existing `ConfigError` path. A
  forge API failure returns `1`, matching `Aborted` — the run happened, nothing shipped.

## 7. CLI and registry changes

- `--publisher NAME`, defaulting to **`dry-run`** so every existing invocation keeps working
  unchanged. Unknown names print the available list and return `2`, mirroring the existing
  `--connector` handling.
- `--publish-set KEY=VALUE` (repeatable, YAML-typed), exactly parallel to `--llm-set`.
  Replaces the hardcoded `publish_config={"out_dir": args.out}`; for `dry-run`, `out_dir`
  defaults to `--out` when not set explicitly.
- `_publisher(pm)` becomes `_publishers(pm)` — a name→plugin dict keyed on
  `kbforge_publisher_info().name` — removing the registration-order dependency.
- `kbforge list` gains a publishers section beside connectors and synthesizers.

## 8. Non-goals and known limitations

Both are consequences of one choice — `put_files` inherits from base rather than replacing
the tree wholesale — and are recorded together so the tradeoff reads as a single decision
rather than as two bugs filed later.

1. **Concept deletions do not propagate.** `ProposedChange.files` is write-only;
   `summary.claims_removed` is prose with no machine-readable file list. A concept removed
   from the source stays in the target repo. Fixing this requires a `files_removed` field on
   the model — deliberately out of scope here.
2. **Force-push discards human commits** on the sync branch. Guarding this (detecting
   non-kbforge commits and diverting to a fresh PR) was considered and declined in favour of
   the simpler one-PR model. Documented in the README so it is a known property, not a
   surprise.

Also out of scope: labels, reviewers, draft PRs, auto-merge (permanently), pagination
(`find_open_pr` reads only the first page — a branch has at most one open PR), and rate-limit
backoff (a run makes fewer than ten calls).

## 9. Testing

Each adapter takes its transport as a constructor argument defaulting to `_http.request`,
which keeps every test below offline and deterministic.

| Layer | What it pins |
|---|---|
| `publish_to_forge` + recording `FakeForgeClient` | call sequence, `base_path` prefixing, branch selection, create-vs-update branching |
| Each client + fake transport | exact URLs, methods, payloads, response parsing — GitHub's tree/commit/ref sequence including the 422→POST ref fallback, GitLab's single `actions[]` POST |
| `safe_join` | `..`, absolute paths, traversal via `base_path` *and* via `change.files` keys |
| `ForgeConfig.validate_config` | malformed `repo`, unset token env, bad `base_path` |
| CLI | `--publisher` selection, unknown-name exit code, `--publish-set` parsing, `list` output |
| `ForgeError` propagation | mirror does **not** advance when publish raises (via `run()` with a failing publisher) |
| `@pytest.mark.live` | opens a real PR/MR against a scratch repo on each forge; skipped unless `--run-live` |

Injecting the transport means tests assert on *the request we intended to make* — a URL and
a dict — rather than on stdlib internals. When a forge changes an endpoint, exactly one test
fails and it names the endpoint.

The `live` marker and `--run-live` flag already exist in `tests/conftest.py`; only the
marker description needs broadening beyond "calls a real LLM provider".

## 10. Build sequence

Each step leaves the suite green.

1. Extract `summary_md` into `publishers/summary.py`; repoint `dry_run.py`.
2. `_http.py` — `request` + `ForgeError`, with fake-transport tests.
3. `forge.py` — `ForgeConfig`, `safe_join`, protocol, `publish_to_forge`; tested against
   `FakeForgeClient`.
4. `github.py` + adapter tests.
5. `gitlab.py` + adapter tests.
6. Registry and CLI wiring (`--publisher`, `--publish-set`, `list`), plus CLI tests.
7. Docs: README status and a publish section, `architecture.md` §0 and §5.2 amendments,
   CHANGELOG.
8. Live tests against real scratch repos on both forges.
