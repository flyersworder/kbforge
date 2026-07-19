# Example: a GitHub Issues connector

A complete, credentialed kbforge connector in ~135 lines — the worked example for
writing your own. It syncs a repository's issues into OKF concepts, and it lives in
its **own package**, discovered by kbforge purely through an entry point. Nothing in
kbforge core changes.

This is the shape every connector follows. Copy it, rename it, swap the `fetch`
body for your system of record.

## How kbforge finds it

A connector is any object that implements the connector hooks, advertised under the
`kbforge.connectors` entry-point group in your package's `pyproject.toml`:

```toml
[project.entry-points."kbforge.connectors"]
github_issues = "kbforge_github_issues:connector"
```

Once your package is installed alongside kbforge, `kbforge list` shows it and
`kbforge run --connector github_issues …` uses it. No fork, no registration call.

## The four hooks (the whole contract)

| hook | job |
|---|---|
| `kbforge_connector_info` | static self-description (name, version, source) |
| `kbforge_validate_config` | return a list of problems (`[]` = ok); **no I/O** |
| `kbforge_fetch(config, cursor)` | pull raw records; `cursor=None` = full backfill |
| `kbforge_normalize(records)` | raw records → `CanonicalDocument`s; **pure & clock-free** |

That's it. Everything downstream — canonicalization, the replay-safe mirror, diff,
the no-op rule, incremental scoping, synthesis (stub or LLM), the §4.4 validators,
and publish — you get for free. A connector only teaches kbforge how to *read* your
source.

## What this connector shows

**`fetch` — the real-world work.** It reads a token from the `GITHUB_TOKEN`
environment variable (never from config), pages through the REST API, filters out
pull requests (GitHub's issues endpoint returns them too), and returns each issue as
a `RawRecord` carrying the issue JSON plus an `anchor_hint` (stable id, URL,
timestamp). The **cursor** is the maximum `updated_at` seen, so the next run passes
`since=<watermark>` and fetches only issues changed since — real incremental sync,
just like the built-in `git_commits` connector.

**`normalize` — deterministic by law.** It is a *pure* parse of the stored JSON into
a `CanonicalDocument`: no network, no clock (the timestamp came from `fetch`). kbforge
enforces this — `assert_stability` normalizes twice and rejects a connector whose
output isn't identical. One design choice worth copying: `updated_at` and the comment
count are deliberately kept **out** of the concept, so a comment-only edit produces an
identical concept and stays a no-op, while a real body/label/state change is detected.

**The rules a connector must respect:**
- **Deterministic, clock-free `normalize`** (the §4.3 stability law).
- **Retriever, not extractor** — return the source content verbatim with anchors;
  interpretation is the synthesizer's job, not the connector's.
- **Credentials only from the environment**, never from config values.
- **Stable document identity** — a `doc_id` that survives across syncs, so change
  detection works (here: `github_issues:owner/repo/issues/<number>`).

## Run it

```bash
# from this directory
uv sync --extra dev
uv run pytest                      # 6 offline tests (deterministic, no network)

export GITHUB_TOKEN=$(gh auth token)
uv run kbforge run \
  --connector github_issues \
  --set repo=owner/name \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

Add `--synthesizer llm --llm-set model=deepseek/deepseek-v4-flash` (with
`kbforge[llm]` and `OPENROUTER_API_KEY`) to turn each issue into a synthesized concept
instead of the verbatim stub.

## Deliberately out of scope

Issue **comments** are not folded into the concept body (one extra API call per
issue). Deletions aren't derived from absence yet (a kbforge-wide gap — needs
tombstones). Both are natural next enhancements if you extend this.
