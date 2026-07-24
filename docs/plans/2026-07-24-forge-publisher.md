# Forge Publisher (GitHub & GitLab) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace kbforge's dry-run-only publish stage with a real publisher that opens (or updates) a pull request on GitHub and a merge request on GitLab.

**Architecture:** One forge-agnostic orchestration (`publish_to_forge`) drives a five-method `ForgeClient` protocol; two adapters implement it over the GitHub and GitLab REST APIs using stdlib `urllib.request`. Each adapter takes its transport as a constructor argument, so every test is offline. Never-auto-merge is enforced structurally — no merge method exists on the protocol or either adapter.

**Tech Stack:** Python 3.12+, stdlib only (`urllib.request`, `json`, `posixpath`), pluggy for plugin registration, pytest.

**Spec:** [`docs/design/2026-07-24-forge-publisher-design.md`](../design/2026-07-24-forge-publisher-design.md)

## Global Constraints

- **No new runtime dependencies.** kbforge's runtime deps stay exactly `pluggy>=1.5`, `pydantic>=2.0`, `pyyaml>=6`. Do not add `httpx`, `requests`, `PyGithub`, or `python-gitlab`.
- **Tokens come from environment variables only**, never from `--set` / `--publish-set` / any CLI flag.
- **No test may make a real network call** unless marked `@pytest.mark.live` (skipped unless `--run-live` is passed).
- **Never merge.** Do not add a merge method to `ForgeClient` or any adapter.
- **Python 3.12+**, `from __future__ import annotations` at the top of every new module (house style).
- Lint/format via ruff (`select = ["E", "F", "I", "UP"]`); pre-commit runs ruff + ty on commit.
- Run the full suite with `uv run pytest -q`. Run a single test with `uv run pytest tests/test_x.py::test_y -v`.
- Publisher `ConnectorInfo.version` is `"0.3.0"` for both new publishers (the release this ships in).

---

### Task 1: Extract `summary_md` into a shared module

Both new publishers need the MR-body renderer that currently lives private in `dry_run.py`. Extract it first so later tasks import a stable name.

**Files:**
- Create: `src/kbforge/publishers/summary.py`
- Modify: `src/kbforge/publishers/dry_run.py:13-26` (remove `_summary_md`, import instead)
- Test: `tests/test_summary.py`

**Interfaces:**
- Consumes: `kbforge.models.ChangeSummary`
- Produces: `kbforge.publishers.summary.summary_md(summary: ChangeSummary) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_summary.py`:

```python
from kbforge.models import ChangeSummary
from kbforge.publishers.summary import summary_md


def test_summary_md_renders_populated_sections():
    md = summary_md(
        ChangeSummary(
            claims_added=["concepts/x/overview.md"],
            claims_modified=["concepts/y/overview.md"],
        )
    )
    assert md.startswith("# Proposed change\n")
    assert "## Added" in md
    assert "- concepts/x/overview.md" in md
    assert "## Modified" in md


def test_summary_md_omits_empty_sections():
    md = summary_md(ChangeSummary(claims_added=["a.md"]))
    assert "## Added" in md
    assert "## Removed" not in md
    assert "## Conflicts" not in md


def test_summary_md_ends_with_single_newline():
    md = summary_md(ChangeSummary(claims_added=["a.md"]))
    assert md.endswith("\n")
    assert not md.endswith("\n\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_summary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kbforge.publishers.summary'`

- [ ] **Step 3: Create the module**

Create `src/kbforge/publishers/summary.py`:

```python
"""Renders a ChangeSummary as the review request's body. Shared by every
publisher: the dry-run publisher writes it to MR_BODY.md for want of anywhere
better, while a forge publisher uses it as the PR/MR description — which is the
shape it always had."""

from __future__ import annotations

from kbforge.models import ChangeSummary


def summary_md(summary: ChangeSummary) -> str:
    lines = ["# Proposed change", ""]
    for label, items in (
        ("Added", summary.claims_added),
        ("Modified", summary.claims_modified),
        ("Removed", summary.claims_removed),
        ("Conflicts", summary.conflicts_flagged),
        ("Gaps", summary.gaps_flagged),
    ):
        if items:
            lines.append(f"## {label}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Repoint `dry_run.py`**

In `src/kbforge/publishers/dry_run.py`, delete the entire `_summary_md` function (lines 13-26) and change the imports and its one call site:

```python
"""Dry-run publisher: writes the proposal to a local directory instead of opening
an MR. Ships in core (§5.2). Never merges — see the github/gitlab publishers for
the real thing."""

from __future__ import annotations

from pathlib import Path

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers.summary import summary_md


class DryRunPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="dry-run", version="0.1.0", source_system="local filesystem"
        )

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        branch = change.branch_hint.replace("/", "-")
        out_dir = Path(config["out_dir"]) / branch
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in change.files.items():
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, "utf-8")
        (out_dir / "MR_BODY.md").write_text(summary_md(change.summary), "utf-8")
        return str(out_dir)  # a path, not a merge — never merges
```

Note `ChangeSummary` is no longer imported here.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — all existing tests including `tests/test_dry_run_publisher.py` still green.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/publishers/summary.py src/kbforge/publishers/dry_run.py tests/test_summary.py
git commit -m "refactor: extract summary_md for reuse by forge publishers"
```

---

### Task 2: The stdlib HTTP helper

**Files:**
- Create: `src/kbforge/publishers/_http.py`
- Test: `tests/test_forge_http.py`

**Interfaces:**
- Produces:
  - `ForgeError(status: int, url: str, body: str)` — a `RuntimeError` with `.status`, `.url`, `.body`
  - `request(method: str, url: str, *, headers: dict[str, str], payload: dict | list | None = None) -> Any`
  - `Transport` — the callable type alias adapters accept for injection

- [ ] **Step 1: Write the failing test**

Create `tests/test_forge_http.py`:

```python
import io
import json
import urllib.error
import urllib.request

import pytest

from kbforge.publishers._http import ForgeError, request


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_request_sends_json_and_parses_response(monkeypatch):
    seen = {}

    def fake_urlopen(req):
        seen["method"] = req.method
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data)
        seen["auth"] = req.get_header("Authorization")
        seen["content_type"] = req.get_header("Content-type")
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = request(
        "POST",
        "https://api.example/things",
        headers={"Authorization": "Bearer t0ken"},
        payload={"a": 1},
    )

    assert out == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.example/things"
    assert seen["payload"] == {"a": 1}
    assert seen["auth"] == "Bearer t0ken"
    assert seen["content_type"] == "application/json"


def test_request_without_payload_sends_no_body(monkeypatch):
    seen = {}

    def fake_urlopen(req):
        seen["data"] = req.data
        seen["method"] = req.method
        return _Resp(b'{"default_branch": "main"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = request("GET", "https://api.example/repo", headers={})
    assert out == {"default_branch": "main"}
    assert seen["data"] is None
    assert seen["method"] == "GET"


def test_request_returns_none_for_empty_body(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: _Resp(b""))
    assert request("DELETE", "https://api.example/x", headers={}) is None


def test_http_error_becomes_forge_error_with_status_and_body(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.HTTPError(
            req.full_url, 422, "Unprocessable", {}, io.BytesIO(b'{"message": "nope"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request("PATCH", "https://api.example/x", headers={}, payload={})

    assert exc.value.status == 422
    assert exc.value.url == "https://api.example/x"
    assert "nope" in exc.value.body


def test_forge_error_never_exposes_the_token(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message": "bad creds"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request(
            "GET",
            "https://api.example/x",
            headers={"Authorization": "Bearer s3cret"},
        )

    assert "s3cret" not in str(exc.value)
    assert "s3cret" not in repr(exc.value)
    assert exc.value.__cause__ is None  # chained HTTPError suppressed


def test_url_error_becomes_forge_error_with_status_zero(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request("GET", "https://api.example/x", headers={})

    assert exc.value.status == 0
    assert "name resolution failed" in exc.value.body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_forge_http.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kbforge.publishers._http'`

- [ ] **Step 3: Write the implementation**

Create `src/kbforge/publishers/_http.py`:

```python
"""Minimal JSON-over-HTTP for forge publishers, on stdlib urllib. Deliberately
not httpx/requests: the forge adapters make fewer than ten calls against known
endpoints, so kbforge's runtime dependency list stays pluggy/pydantic/pyyaml."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# What an adapter accepts for injection so its tests never touch the network.
Transport = Callable[..., Any]


class ForgeError(RuntimeError):
    """A forge API call failed.

    Carries status, URL and *response* body only. Request headers are never
    captured, so the token cannot reach a log, a traceback, or CI output.
    """

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"{status} from {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict | list | None = None,
) -> Any:
    """Perform one JSON request. Returns the decoded body, or None if empty."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # `from None` so the chained HTTPError — which holds the Request — can
        # never surface a header in a traceback.
        raise ForgeError(exc.code, url, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ForgeError(0, url, str(exc.reason)) from None
    return json.loads(raw) if raw else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_forge_http.py -v`
Expected: PASS — 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/_http.py tests/test_forge_http.py
git commit -m "feat: add stdlib JSON-over-HTTP helper for forge publishers"
```

---

### Task 3: Forge-agnostic orchestration, config, and path safety

The heart of the feature: everything that is *not* GitHub- or GitLab-specific.

**Files:**
- Create: `src/kbforge/publishers/forge.py`
- Modify: `src/kbforge/hookspecs.py` (add `kbforge_validate_publish_config` to `PublisherSpec`)
- Test: `tests/test_forge.py`

**Interfaces:**
- Consumes: `kbforge.publishers.summary.summary_md`, `kbforge.models.ProposedChange`
- Produces:
  - `PathError(ValueError)`
  - `safe_join(base_path: str, rel: str) -> str`
  - `ForgeConfig` dataclass with fields `repo, base, base_path, branch, title, api_base, token_env`, plus `validate_config() -> list[str]` and `token() -> str`
  - `build_config(config: dict, defaults: dict) -> ForgeConfig`
  - `ForgeClient` Protocol: `default_branch()`, `put_files(branch, base, files, message)`, `find_open_pr(branch)`, `create_pr(branch, base, title, body)`, `update_pr(pr_id, title, body)`
  - `publish_to_forge(client: ForgeClient, change: ProposedChange, cfg: ForgeConfig) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_forge.py`:

```python
import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.forge import (
    ForgeConfig,
    PathError,
    build_config,
    publish_to_forge,
    safe_join,
)

DEFAULTS = {"api_base": "https://api.example", "token_env": "EXAMPLE_TOKEN"}


class FakeForgeClient:
    """Records calls so the orchestration can be asserted without a network."""

    def __init__(self, open_pr: str | None = None, default: str = "main") -> None:
        self.calls: list[tuple] = []
        self._open_pr = open_pr
        self._default = default

    def default_branch(self) -> str:
        self.calls.append(("default_branch",))
        return self._default

    def put_files(self, branch, base, files, message) -> None:
        self.calls.append(("put_files", branch, base, files, message))

    def find_open_pr(self, branch):
        self.calls.append(("find_open_pr", branch))
        return self._open_pr

    def create_pr(self, branch, base, title, body):
        self.calls.append(("create_pr", branch, base, title, body))
        return "https://forge.example/pr/1"

    def update_pr(self, pr_id, title, body):
        self.calls.append(("update_pr", pr_id, title, body))
        return "https://forge.example/pr/7"


def _change():
    return ProposedChange(
        branch_hint="sync/local-files",
        files={"concepts/x/overview.md": "# X\n"},
        summary=ChangeSummary(claims_added=["concepts/x/overview.md"]),
    )


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


# --- safe_join -------------------------------------------------------------


def test_safe_join_without_base_path_returns_rel():
    assert safe_join("", "concepts/x/overview.md") == "concepts/x/overview.md"


def test_safe_join_prefixes_base_path():
    assert safe_join("knowledge", "concepts/x.md") == "knowledge/concepts/x.md"


def test_safe_join_normalizes_redundant_separators():
    assert safe_join("knowledge/", "concepts//x.md") == "knowledge/concepts/x.md"


@pytest.mark.parametrize(
    "base_path, rel",
    [
        ("", "../../.github/workflows/deploy.yml"),
        ("..", "x.md"),
        ("/etc", "passwd"),
        ("", "/etc/passwd"),
        ("knowledge", "../../../x.md"),
    ],
)
def test_safe_join_rejects_traversal_and_absolute_paths(base_path, rel):
    with pytest.raises(PathError):
        safe_join(base_path, rel)


def test_safe_join_rejects_empty_rel():
    with pytest.raises(PathError):
        safe_join("knowledge", "")


# --- ForgeConfig -----------------------------------------------------------


def test_validate_config_accepts_a_good_config(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    assert _cfg().validate_config() == []


def test_validate_config_accepts_nested_gitlab_subgroups(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    cfg = ForgeConfig(repo="group/subgroup/project", **DEFAULTS)
    assert cfg.validate_config() == []


def test_validate_config_reports_missing_repo(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = ForgeConfig(repo="", **DEFAULTS).validate_config()
    assert any("repo" in p for p in problems)


def test_validate_config_reports_malformed_repo(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = ForgeConfig(repo="justname", **DEFAULTS).validate_config()
    assert any("owner/name" in p for p in problems)


def test_validate_config_reports_unset_token_env(monkeypatch):
    monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    problems = _cfg().validate_config()
    assert any("EXAMPLE_TOKEN" in p for p in problems)


def test_validate_config_reports_traversing_base_path(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = _cfg(base_path="../outside").validate_config()
    assert any("base_path" in p for p in problems)


def test_token_reads_the_named_env_var(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "s3cret")
    assert _cfg().token() == "s3cret"


def test_build_config_applies_defaults_then_overrides():
    cfg = build_config({"repo": "acme/kb", "api_base": "https://ghe.acme"}, DEFAULTS)
    assert cfg.repo == "acme/kb"
    assert cfg.api_base == "https://ghe.acme"
    assert cfg.token_env == "EXAMPLE_TOKEN"


def test_build_config_rejects_unknown_keys():
    with pytest.raises(ValueError) as exc:
        build_config({"repo": "acme/kb", "reviewers": ["a"]}, DEFAULTS)
    assert "reviewers" in str(exc.value)


# --- publish_to_forge ------------------------------------------------------


def test_publish_creates_a_pr_when_none_is_open():
    client = FakeForgeClient(open_pr=None)
    url = publish_to_forge(client, _change(), _cfg(base="main"))

    assert url == "https://forge.example/pr/1"
    names = [c[0] for c in client.calls]
    assert names == ["put_files", "find_open_pr", "create_pr"]


def test_publish_updates_the_open_pr_when_one_exists():
    client = FakeForgeClient(open_pr="7")
    url = publish_to_forge(client, _change(), _cfg(base="main"))

    assert url == "https://forge.example/pr/7"
    names = [c[0] for c in client.calls]
    assert names == ["put_files", "find_open_pr", "update_pr"]
    assert client.calls[-1][1] == "7"


def test_publish_resolves_default_branch_when_base_is_unset():
    client = FakeForgeClient(default="trunk")
    publish_to_forge(client, _change(), _cfg())

    assert client.calls[0] == ("default_branch",)
    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[2] == "trunk"


def test_publish_uses_branch_hint_when_branch_is_unset():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[1] == "sync/local-files"


def test_publish_prefers_configured_branch_over_hint():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", branch="kb/sync"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[1] == "kb/sync"


def test_publish_prefixes_files_with_base_path():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", base_path="knowledge"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[3] == {"knowledge/concepts/x/overview.md": "# X\n"}


def test_publish_sends_the_summary_as_the_pr_body():
    client = FakeForgeClient(open_pr=None)
    publish_to_forge(client, _change(), _cfg(base="main"))

    create = next(c for c in client.calls if c[0] == "create_pr")
    assert create[4].startswith("# Proposed change")
    assert "concepts/x/overview.md" in create[4]


def test_publish_uses_the_title_as_the_commit_message():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", title="kbforge: sync"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[4] == "kbforge: sync"


def test_publish_rejects_a_traversing_file_key():
    client = FakeForgeClient()
    change = ProposedChange(
        branch_hint="sync/x",
        files={"../../.github/workflows/deploy.yml": "evil"},
    )
    with pytest.raises(PathError):
        publish_to_forge(client, change, _cfg(base="main"))
    assert client.calls == []  # nothing reached the forge


def test_forge_client_protocol_has_no_merge_method():
    from kbforge.publishers import forge

    assert not hasattr(forge.ForgeClient, "merge")
    assert not any("merge" in name for name in dir(forge.ForgeClient))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_forge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kbforge.publishers.forge'`

- [ ] **Step 3: Write the implementation**

Create `src/kbforge/publishers/forge.py`:

```python
"""Forge-agnostic publish orchestration.

Knows the *sequence* — reset a branch to base, put the files on it, open or
update exactly one review request — and nothing about GitHub or GitLab. The two
forges decompose "commit these files" completely differently (GitLab in one
call, GitHub in four), so the ForgeClient protocol is pitched at intentions,
not at REST endpoints.

MUST NOT merge (§5.2). There is no merge method here or on any adapter, so the
guarantee cannot be violated without deliberately widening the interface.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, fields
from typing import Protocol

from kbforge.models import ProposedChange
from kbforge.publishers.summary import summary_md


class PathError(ValueError):
    """A configured or generated path escapes the target repository."""


def safe_join(base_path: str, rel: str) -> str:
    """Join a config prefix to a bundle-relative path, refusing anything that
    escapes the repo. Applied to *both* base_path (user config) and every key of
    change.files (connector/synthesizer output) — file keys are produced
    downstream and are equally capable of naming ../../.github/workflows/x.yml.
    """
    if not rel:
        raise PathError("empty file path")
    for part in (base_path, rel):
        if part.startswith("/"):
            raise PathError(f"absolute path not allowed: {part!r}")
        if ".." in part.split("/"):
            raise PathError(f"'..' not allowed in path: {part!r}")
    joined = posixpath.join(base_path, rel) if base_path else rel
    normalized = posixpath.normpath(joined)
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise PathError(f"path escapes the repository: {joined!r}")
    return normalized


@dataclass
class ForgeConfig:
    """Publisher config. Mirrors LLMConfig's shape: plain dataclass, a
    validate_*() returning human-readable problems, and the credential named by
    an env var rather than carried as a value."""

    repo: str = ""
    base: str = ""
    base_path: str = ""
    branch: str = ""
    title: str = "kbforge: knowledge base sync"
    api_base: str = ""
    token_env: str = ""

    def validate_config(self) -> list[str]:
        problems: list[str] = []
        if not self.repo:
            problems.append("'repo' is required (owner/name)")
        else:
            segments = self.repo.split("/")
            # At least two, not exactly two: GitLab projects nest in subgroups
            # (group/subgroup/project); GitHub is always owner/name.
            if len(segments) < 2 or not all(segments):
                problems.append(f"'repo' must be owner/name, got {self.repo!r}")
        if self.base_path:
            try:
                safe_join(self.base_path, "probe.md")
            except PathError as exc:
                problems.append(f"'base_path' invalid: {exc}")
        if not self.token_env:
            problems.append("'token_env' must be non-empty")
        elif not os.environ.get(self.token_env):
            problems.append(f"env var {self.token_env} is not set")
        return problems

    def token(self) -> str:
        return os.environ.get(self.token_env, "")


def build_config(config: dict, defaults: dict) -> ForgeConfig:
    """Merge per-forge defaults under the user's --publish-set values."""
    merged = {**defaults, **config}
    known = {f.name for f in fields(ForgeConfig)}
    unknown = sorted(set(merged) - known)
    if unknown:
        raise ValueError(
            f"unknown publisher config key(s): {', '.join(unknown)}; "
            f"known keys: {', '.join(sorted(known))}"
        )
    return ForgeConfig(**merged)


class ForgeClient(Protocol):
    """Every method names an intention, never a REST endpoint."""

    def default_branch(self) -> str: ...

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
        """Reset `branch` to `base`, then apply exactly `files` as one commit.

        Files present in `base` but absent from `files` are inherited, not
        deleted — concept deletions do not propagate (spec §8).
        """
        ...

    def find_open_pr(self, branch: str) -> str | None:
        """The open PR/MR id for `branch` as an opaque string, or None."""
        ...

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str: ...

    def update_pr(self, pr_id: str, title: str, body: str) -> str: ...


def publish_to_forge(
    client: ForgeClient, change: ProposedChange, cfg: ForgeConfig
) -> str:
    """Open or update one review request; return its URL. Never merges."""
    base = cfg.base or client.default_branch()
    branch = cfg.branch or change.branch_hint
    files = {
        safe_join(cfg.base_path, rel): body for rel, body in change.files.items()
    }
    body = summary_md(change.summary)

    client.put_files(branch, base, files, cfg.title)
    pr_id = client.find_open_pr(branch)
    if pr_id is not None:
        return client.update_pr(pr_id, cfg.title, body)
    return client.create_pr(branch, base, cfg.title, body)
```

- [ ] **Step 4: Add the validation hookspec**

In `src/kbforge/hookspecs.py`, add one method to `PublisherSpec`, after `kbforge_publisher_info`:

```python
    @hookspec
    @abstractmethod
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        """Return human-readable problems ([] = ok). No network I/O.

        Mirrors the connector family's kbforge_validate_config so the CLI can
        fail on a bad publisher config in under a second, rather than after a
        full fetch and synthesize."""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_forge.py -v`
Expected: PASS — 28 passed (9 `safe_join`, 9 `ForgeConfig`/`build_config`, 10 `publish_to_forge`).

Then run the full suite to confirm the hookspec addition broke nothing:

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/publishers/forge.py src/kbforge/hookspecs.py tests/test_forge.py
git commit -m "feat: add forge-agnostic publish orchestration, config and path safety"
```

---

### Task 4: GitHub adapter

**Files:**
- Create: `src/kbforge/publishers/github.py`
- Test: `tests/test_github_publisher.py`

**Interfaces:**
- Consumes: `ForgeConfig`, `build_config`, `publish_to_forge`, `ForgeClient` (Task 3); `request`, `ForgeError`, `Transport` (Task 2)
- Produces: `GitHubClient(cfg: ForgeConfig, transport: Transport = request)`, `GitHubPublisher`, `DEFAULTS: dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_github_publisher.py`:

```python
import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers._http import ForgeError
from kbforge.publishers.forge import ForgeConfig
from kbforge.publishers.github import DEFAULTS, GitHubClient, GitHubPublisher


class FakeTransport:
    """Returns a canned response per (method, url-suffix); records every call."""

    def __init__(self, routes: dict, errors: dict | None = None) -> None:
        self.routes = routes
        self.errors = errors or {}
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, payload=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        key = (method, url)
        if key in self.errors:
            raise self.errors[key]
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected call: {method} {url}")


API = "https://api.github.com"


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


def _client(routes, errors=None, **over):
    transport = FakeTransport(routes, errors)
    return GitHubClient(_cfg(**over), transport=transport), transport


def test_default_branch_reads_the_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client({("GET", "/repos/acme/kb"): {"default_branch": "main"}})

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/repos/acme/kb"


def test_requests_carry_bearer_auth_and_api_version(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    client, transport = _client({("GET", "/repos/acme/kb"): {"default_branch": "main"}})
    client.default_branch()

    headers = transport.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer s3cret"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_api_base_override_targets_github_enterprise(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {("GET", "/repos/acme/kb"): {"default_branch": "main"}},
        api_base="https://ghe.acme/api/v3",
    )
    client.default_branch()

    assert transport.calls[0]["url"] == "https://ghe.acme/api/v3/repos/acme/kb"


def test_put_files_walks_tree_commit_ref(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "base-sha",
            "commit": {"tree": {"sha": "base-tree"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "new-tree"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "new-commit"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/sync/local-files"): {"ref": "ok"},
    }
    client, transport = _client(routes)

    client.put_files("sync/local-files", "main", {"a.md": "A\n"}, "msg")

    assert [c["method"] for c in transport.calls] == ["GET", "POST", "POST", "PATCH"]

    tree = transport.calls[1]["payload"]
    assert tree["base_tree"] == "base-tree"
    assert tree["tree"] == [
        {"path": "a.md", "mode": "100644", "type": "blob", "content": "A\n"}
    ]

    commit = transport.calls[2]["payload"]
    assert commit == {"message": "msg", "tree": "new-tree", "parents": ["base-sha"]}

    ref = transport.calls[3]["payload"]
    assert ref == {"sha": "new-commit", "force": True}


def test_put_files_sorts_entries_for_determinism(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/b"): {},
    }
    client, transport = _client(routes)

    client.put_files("b", "main", {"z.md": "Z", "a.md": "A"}, "msg")

    paths = [e["path"] for e in transport.calls[1]["payload"]["tree"]]
    assert paths == ["a.md", "z.md"]


def test_put_files_creates_the_ref_when_patch_reports_it_missing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("POST", "/repos/acme/kb/git/refs"): {"ref": "created"},
    }
    errors = {
        ("PATCH", f"{API}/repos/acme/kb/git/refs/heads/b"): ForgeError(
            422, "u", "Reference does not exist"
        )
    }
    client, transport = _client(routes, errors)

    client.put_files("b", "main", {"a.md": "A"}, "msg")

    assert transport.calls[-1]["method"] == "POST"
    assert transport.calls[-1]["url"].endswith("/git/refs")
    assert transport.calls[-1]["payload"] == {"ref": "refs/heads/b", "sha": "nc"}


def test_put_files_reraises_unexpected_ref_errors(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
    }
    errors = {
        ("PATCH", f"{API}/repos/acme/kb/git/refs/heads/b"): ForgeError(
            403, "u", "protected branch"
        )
    }
    client, _ = _client(routes, errors)

    with pytest.raises(ForgeError) as exc:
        client.put_files("b", "main", {"a.md": "A"}, "msg")
    assert exc.value.status == 403


def test_find_open_pr_returns_stringified_number(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client({("GET", "&state=open"): [{"number": 42}]})

    assert client.find_open_pr("sync/local-files") == "42"
    assert "head=acme%3Async%2Flocal-files" in transport.calls[0]["url"]
    assert "state=open" in transport.calls[0]["url"]


def test_find_open_pr_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, _ = _client({("GET", "&state=open"): []})
    assert client.find_open_pr("b") is None


def test_create_pr_returns_html_url(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {("POST", "/repos/acme/kb/pulls"): {"html_url": "https://github.com/acme/kb/pull/1"}}
    )

    url = client.create_pr("b", "main", "T", "B")

    assert url == "https://github.com/acme/kb/pull/1"
    assert transport.calls[0]["payload"] == {
        "title": "T",
        "head": "b",
        "base": "main",
        "body": "B",
    }


def test_update_pr_patches_by_number(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {("PATCH", "/repos/acme/kb/pulls/42"): {"html_url": "https://github.com/acme/kb/pull/42"}}
    )

    url = client.update_pr("42", "T", "B")

    assert url == "https://github.com/acme/kb/pull/42"
    assert transport.calls[0]["payload"] == {"title": "T", "body": "B"}


def test_publisher_info_names_it_github():
    info = GitHubPublisher().kbforge_publisher_info()
    assert info.name == "github"
    assert "GitHub" in info.source_system


def test_publisher_validates_config(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    problems = GitHubPublisher().kbforge_validate_publish_config({"repo": "acme/kb"})
    assert any("GITHUB_TOKEN" in p for p in problems)


def test_publisher_validate_accepts_good_config(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert GitHubPublisher().kbforge_validate_publish_config({"repo": "acme/kb"}) == []


def test_publisher_validate_reports_unknown_keys(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    problems = GitHubPublisher().kbforge_validate_publish_config(
        {"repo": "acme/kb", "reviewers": ["a"]}
    )
    assert any("reviewers" in p for p in problems)


def test_publisher_has_no_merge_method():
    assert not hasattr(GitHubPublisher(), "merge")
    assert not hasattr(GitHubClient, "merge")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_github_publisher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kbforge.publishers.github'`

- [ ] **Step 3: Write the implementation**

Create `src/kbforge/publishers/github.py`:

```python
"""GitHub pull-request publisher. Ships in core, credential from the environment
only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import ForgeError, Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://api.github.com", "token_env": "GITHUB_TOKEN"}

# PATCH on a missing ref answers 422; 404 is accepted too so a future API
# tightening does not turn "branch not created yet" into a hard failure.
_REF_MISSING = (404, 422)


class GitHubClient:
    def __init__(self, cfg: ForgeConfig, transport: Transport = request) -> None:
        self._cfg = cfg
        self._transport = transport
        self._api = cfg.api_base.rstrip("/")
        self._repo = cfg.repo

    def _call(self, method: str, path: str, payload: dict | list | None = None):
        return self._transport(
            method,
            f"{self._api}{path}",
            headers={
                "Authorization": f"Bearer {self._cfg.token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            payload=payload,
        )

    def default_branch(self) -> str:
        return self._call("GET", f"/repos/{self._repo}")["default_branch"]

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
        # One call yields both the base commit SHA and its tree SHA, so no
        # separate ref lookup is needed. Contents go inline in the tree entries,
        # so no blob calls are needed either.
        head = self._call("GET", f"/repos/{self._repo}/commits/{quote(base)}")
        tree = self._call(
            "POST",
            f"/repos/{self._repo}/git/trees",
            {
                "base_tree": head["commit"]["tree"]["sha"],
                "tree": [
                    {"path": path, "mode": "100644", "type": "blob", "content": body}
                    for path, body in sorted(files.items())  # deterministic
                ],
            },
        )
        commit = self._call(
            "POST",
            f"/repos/{self._repo}/git/commits",
            {"message": message, "tree": tree["sha"], "parents": [head["sha"]]},
        )
        try:
            self._call(
                "PATCH",
                f"/repos/{self._repo}/git/refs/heads/{branch}",
                {"sha": commit["sha"], "force": True},
            )
        except ForgeError as exc:
            if exc.status not in _REF_MISSING:
                raise
            self._call(
                "POST",
                f"/repos/{self._repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )

    def find_open_pr(self, branch: str) -> str | None:
        # kbforge always pushes to the target repo itself, never a fork, so the
        # head owner is the first segment of repo by construction.
        owner = self._repo.split("/")[0]
        head = quote(f"{owner}:{branch}", safe="")
        prs = self._call("GET", f"/repos/{self._repo}/pulls?head={head}&state=open")
        return str(prs[0]["number"]) if prs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        pr = self._call(
            "POST",
            f"/repos/{self._repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        return pr["html_url"]

    def update_pr(self, pr_id: str, title: str, body: str) -> str:
        pr = self._call(
            "PATCH",
            f"/repos/{self._repo}/pulls/{pr_id}",
            {"title": title, "body": body},
        )
        return pr["html_url"]


class GitHubPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="github", version="0.3.0", source_system="GitHub pull requests"
        )

    @hookimpl
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        try:
            return build_config(config, DEFAULTS).validate_config()
        except ValueError as exc:
            return [str(exc)]

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        cfg = build_config(config, DEFAULTS)
        return publish_to_forge(GitHubClient(cfg), change, cfg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_github_publisher.py -v`
Expected: PASS — 16 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/github.py tests/test_github_publisher.py
git commit -m "feat: add GitHub pull-request publisher"
```

---

### Task 5: GitLab adapter

**Files:**
- Create: `src/kbforge/publishers/gitlab.py`
- Test: `tests/test_gitlab_publisher.py`

**Interfaces:**
- Consumes: `ForgeConfig`, `build_config`, `publish_to_forge` (Task 3); `request`, `Transport` (Task 2)
- Produces: `GitLabClient(cfg: ForgeConfig, transport: Transport = request)`, `GitLabPublisher`, `DEFAULTS: dict`

- [ ] **Step 1: Write the failing test**

Create `tests/test_gitlab_publisher.py`:

```python
from kbforge.publishers.forge import ForgeConfig
from kbforge.publishers.gitlab import DEFAULTS, GitLabClient, GitLabPublisher


class FakeTransport:
    """Returns a canned response per (method, url-suffix); records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, payload=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected call: {method} {url}")


API = "https://gitlab.com/api/v4"
PROJECT = "acme%2Fkb"


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


def _client(routes, **over):
    transport = FakeTransport(routes)
    return GitLabClient(_cfg(**over), transport=transport), transport


def test_default_branch_reads_the_project(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}})

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/projects/{PROJECT}"


def test_requests_carry_private_token_header(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "s3cret")
    client, transport = _client({("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}})
    client.default_branch()

    assert transport.calls[0]["headers"]["PRIVATE-TOKEN"] == "s3cret"


def test_nested_subgroup_path_is_url_encoded(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    transport = FakeTransport({("GET", "/projects/group%2Fsub%2Fkb"): {"default_branch": "main"}})
    client = GitLabClient(ForgeConfig(repo="group/sub/kb", **DEFAULTS), transport=transport)

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/projects/group%2Fsub%2Fkb"


def test_api_base_override_targets_self_managed(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}},
        api_base="https://gitlab.acme/api/v4",
    )
    client.default_branch()

    assert transport.calls[0]["url"] == f"https://gitlab.acme/api/v4/projects/{PROJECT}"


def test_put_files_commits_in_one_forced_call(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("POST", "/repository/commits"): {"id": "abc"}})

    client.put_files("sync/local-files", "main", {"a.md": "A\n"}, "msg")

    assert len(transport.calls) == 1
    payload = transport.calls[0]["payload"]
    assert payload["branch"] == "sync/local-files"
    assert payload["start_branch"] == "main"
    assert payload["force"] is True
    assert payload["commit_message"] == "msg"
    assert payload["actions"] == [
        {"action": "create", "file_path": "a.md", "content": "A\n"}
    ]


def test_put_files_sorts_actions_for_determinism(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("POST", "/repository/commits"): {"id": "abc"}})

    client.put_files("b", "main", {"z.md": "Z", "a.md": "A"}, "msg")

    paths = [a["file_path"] for a in transport.calls[0]["payload"]["actions"]]
    assert paths == ["a.md", "z.md"]


def test_find_open_pr_returns_stringified_iid(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("GET", "&state=opened"): [{"iid": 7}]})

    assert client.find_open_pr("sync/local-files") == "7"
    assert "source_branch=sync%2Flocal-files" in transport.calls[0]["url"]
    assert "state=opened" in transport.calls[0]["url"]


def test_find_open_pr_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, _ = _client({("GET", "&state=opened"): []})
    assert client.find_open_pr("b") is None


def test_create_pr_returns_web_url(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {("POST", "/merge_requests"): {"web_url": "https://gitlab.com/acme/kb/-/merge_requests/1"}}
    )

    url = client.create_pr("b", "main", "T", "B")

    assert url == "https://gitlab.com/acme/kb/-/merge_requests/1"
    assert transport.calls[0]["payload"] == {
        "source_branch": "b",
        "target_branch": "main",
        "title": "T",
        "description": "B",
    }


def test_update_pr_puts_by_iid(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {("PUT", "/merge_requests/7"): {"web_url": "https://gitlab.com/acme/kb/-/merge_requests/7"}}
    )

    url = client.update_pr("7", "T", "B")

    assert url == "https://gitlab.com/acme/kb/-/merge_requests/7"
    assert transport.calls[0]["payload"] == {"title": "T", "description": "B"}


def test_publisher_info_names_it_gitlab():
    info = GitLabPublisher().kbforge_publisher_info()
    assert info.name == "gitlab"
    assert "GitLab" in info.source_system


def test_publisher_validates_config(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    problems = GitLabPublisher().kbforge_validate_publish_config({"repo": "acme/kb"})
    assert any("GITLAB_TOKEN" in p for p in problems)


def test_publisher_validate_accepts_good_config(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    assert GitLabPublisher().kbforge_validate_publish_config({"repo": "acme/kb"}) == []


def test_publisher_has_no_merge_method():
    assert not hasattr(GitLabPublisher(), "merge")
    assert not hasattr(GitLabClient, "merge")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_gitlab_publisher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'kbforge.publishers.gitlab'`

- [ ] **Step 3: Write the implementation**

Create `src/kbforge/publishers/gitlab.py`:

```python
"""GitLab merge-request publisher. Ships in core, credential from the
environment only. Never merges (§5.2)."""

from __future__ import annotations

from urllib.parse import quote

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers._http import Transport, request
from kbforge.publishers.forge import ForgeConfig, build_config, publish_to_forge

DEFAULTS = {"api_base": "https://gitlab.com/api/v4", "token_env": "GITLAB_TOKEN"}


class GitLabClient:
    def __init__(self, cfg: ForgeConfig, transport: Transport = request) -> None:
        self._cfg = cfg
        self._transport = transport
        self._api = cfg.api_base.rstrip("/")
        # Projects are addressed by URL-encoded path, which is also how nested
        # subgroups (group/subgroup/project) are expressed.
        self._project = quote(cfg.repo, safe="")

    def _call(self, method: str, path: str, payload: dict | list | None = None):
        return self._transport(
            method,
            f"{self._api}{path}",
            headers={"PRIVATE-TOKEN": self._cfg.token()},
            payload=payload,
        )

    def default_branch(self) -> str:
        return self._call("GET", f"/projects/{self._project}")["default_branch"]

    def put_files(
        self, branch: str, base: str, files: dict[str, str], message: str
    ) -> None:
        # force=true bases the commit on start_branch and overwrites the target
        # branch, which is why action="create" is always right: the branch is
        # reset first, so no file exists to collide with.
        self._call(
            "POST",
            f"/projects/{self._project}/repository/commits",
            {
                "branch": branch,
                "start_branch": base,
                "force": True,
                "commit_message": message,
                "actions": [
                    {"action": "create", "file_path": path, "content": body}
                    for path, body in sorted(files.items())  # deterministic
                ],
            },
        )

    def find_open_pr(self, branch: str) -> str | None:
        source = quote(branch, safe="")
        mrs = self._call(
            "GET",
            f"/projects/{self._project}/merge_requests"
            f"?source_branch={source}&state=opened",
        )
        return str(mrs[0]["iid"]) if mrs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> str:
        mr = self._call(
            "POST",
            f"/projects/{self._project}/merge_requests",
            {
                "source_branch": branch,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        return mr["web_url"]

    def update_pr(self, pr_id: str, title: str, body: str) -> str:
        mr = self._call(
            "PUT",
            f"/projects/{self._project}/merge_requests/{pr_id}",
            {"title": title, "description": body},
        )
        return mr["web_url"]


class GitLabPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="gitlab", version="0.3.0", source_system="GitLab merge requests"
        )

    @hookimpl
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        try:
            return build_config(config, DEFAULTS).validate_config()
        except ValueError as exc:
            return [str(exc)]

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        cfg = build_config(config, DEFAULTS)
        return publish_to_forge(GitLabClient(cfg), change, cfg)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gitlab_publisher.py -v`
Expected: PASS — 14 passed.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/publishers/gitlab.py tests/test_gitlab_publisher.py
git commit -m "feat: add GitLab merge-request publisher"
```

---

### Task 6: Registry and CLI wiring

Removes the order-dependent publisher lookup and makes the new publishers reachable.

**Files:**
- Modify: `src/kbforge/registry.py:9-33`
- Modify: `src/kbforge/__main__.py:37-41` (`_publisher` → `_publishers`), `:56-96` (args + `list`), `:129-141` (`run` call)
- Modify: `src/kbforge/publishers/dry_run.py` (add `kbforge_validate_publish_config`)
- Test: `tests/test_cli_publisher.py`

**Interfaces:**
- Consumes: `GitHubPublisher` (Task 4), `GitLabPublisher` (Task 5), `kbforge_validate_publish_config` hookspec (Task 3)
- Produces: `kbforge.__main__._publishers(pm) -> dict[str, PublisherProtocol]`; CLI flags `--publisher NAME`, `--publish-set KEY=VALUE`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_publisher.py`:

```python
from pathlib import Path

import pytest

from kbforge.__main__ import _publishers, main
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror",
        str(tmp_path / "mirror"),
        "--out",
        str(tmp_path / "out"),
        "--state",
        str(tmp_path / "state"),
    ]


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app-x.md").write_text(DOC, "utf-8")
    return src


def test_registry_exposes_all_three_publishers():
    names = set(_publishers(build_registry()))
    assert {"dry-run", "github", "gitlab"} <= names


def test_publisher_lookup_is_by_name_not_registration_order():
    publishers = _publishers(build_registry())
    assert publishers["github"].kbforge_publisher_info().name == "github"
    assert publishers["gitlab"].kbforge_publisher_info().name == "gitlab"


def test_list_shows_publishers(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "publishers:" in out
    assert "github" in out
    assert "gitlab" in out


def test_unknown_publisher_exits_2(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "bitbucket",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "unknown publisher" in capsys.readouterr().out


def test_default_publisher_is_dry_run(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published:" in capsys.readouterr().out
    assert (tmp_path / "out").exists()


def test_forge_publisher_config_is_validated_before_the_pipeline_runs(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "github",
            "--publish-set",
            "repo=acme/kb",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().out
    # Fail-fast: the pipeline never ran, so no mirror was written.
    assert not (tmp_path / "mirror").exists()


def test_publish_set_values_are_yaml_typed():
    from kbforge.__main__ import _parse_settings

    assert _parse_settings(["repo=acme/kb", "base_path=knowledge"]) == {
        "repo": "acme/kb",
        "base_path": "knowledge",
    }


def test_malformed_publish_set_exits_2(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "github",
            "--publish-set",
            "norepo",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "KEY=VALUE" in capsys.readouterr().out


@pytest.mark.parametrize("publisher", ["github", "gitlab"])
def test_forge_publishers_reject_unknown_config_keys(
    tmp_path: Path, capsys, monkeypatch, publisher
):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            publisher,
            "--publish-set",
            "repo=acme/kb",
            "--publish-set",
            "reviewers=[a]",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "reviewers" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_publisher.py -v`
Expected: FAIL — `ImportError: cannot import name '_publishers' from 'kbforge.__main__'`

- [ ] **Step 3: Register the new publishers**

In `src/kbforge/registry.py`, add the imports and registrations:

```python
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge.publishers.github import GitHubPublisher
from kbforge.publishers.gitlab import GitLabPublisher
```

and inside `build_registry()`, after `pm.register(DryRunPublisher())`:

```python
    pm.register(GitHubPublisher())
    pm.register(GitLabPublisher())
```

- [ ] **Step 4: Give the dry-run publisher a validator**

In `src/kbforge/publishers/dry_run.py`, add this method to `DryRunPublisher`, after `kbforge_publisher_info`:

```python
    @hookimpl
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        return [] if config.get("out_dir") else ["'out_dir' is required"]
```

- [ ] **Step 5: Replace `_publisher` with `_publishers`**

In `src/kbforge/__main__.py`, replace the `_publisher` function (lines 37-41) with:

```python
def _publishers(pm: pluggy.PluginManager) -> dict[str, PublisherProtocol]:
    """name -> publisher instance (a publisher implements kbforge_publish).

    Keyed by name rather than "first plugin found": with three publishers
    registered, positional lookup would make the destination depend on plugin
    registration order.
    """
    return {
        p.kbforge_publisher_info().name: cast(PublisherProtocol, p)
        for p in pm.get_plugins()
        if hasattr(p, "kbforge_publish")
    }
```

- [ ] **Step 6: Add the CLI flags**

In `src/kbforge/__main__.py`, after the `--synthesizer` argument (line 72), add:

```python
    r.add_argument(
        "--publisher",
        default="dry-run",
        help="publisher name (default: dry-run); see `kbforge list`",
    )
    r.add_argument(
        "--publish-set",
        action="append",
        default=[],
        dest="publish_settings",
        metavar="KEY=VALUE",
        help="publisher config (repeatable); values are YAML-typed",
    )
```

- [ ] **Step 7: Resolve publishers and show them in `list`**

In `main()`, after `connectors = _connectors(pm)` (line 87), add:

```python
    publishers = _publishers(pm)
```

Then in the `list` branch, after the synthesizers block (line 95), add:

```python
        print("publishers:")
        for name in sorted(publishers):
            info = publishers[name].kbforge_publisher_info()
            print(f"  {name}\t{info.source_system}")
```

- [ ] **Step 8: Validate the publisher and its config before running**

In `main()`, after the connector validation block (line 101), add:

```python
    if args.publisher not in publishers:
        available = ", ".join(sorted(publishers)) or "(none)"
        print(f"unknown publisher {args.publisher!r}; available: {available}")
        return 2
```

Then after `config = _parse_settings(args.settings)` succeeds (line 107), add:

```python
    try:
        publish_config = _parse_settings(args.publish_settings)
    except ValueError as exc:
        print(str(exc))
        return 2
    # The built-in dry-run publisher is wired to --out; forge publishers take
    # their whole config from --publish-set.
    if args.publisher == "dry-run":
        publish_config.setdefault("out_dir", args.out)

    # Fail fast: a bad publisher config should cost a second, not a full
    # fetch+synthesize. Third-party publishers predating the hook skip this.
    validate = getattr(
        publishers[args.publisher], "kbforge_validate_publish_config", None
    )
    publish_problems = validate(publish_config) if validate else []
    if publish_problems:
        print("; ".join(publish_problems))
        return 2
```

- [ ] **Step 9: Use them in the `run` call**

Change the `run(...)` call (lines 130-138) to pass the selected publisher and config:

```python
        result = run(
            connectors[args.connector],
            publishers[args.publisher],
            config=config,
            mirror=args.mirror,
            state_dir=args.state,
            publish_config=publish_config,
            synthesizer=synthesizer,
        )
```

- [ ] **Step 10: Handle forge failures at the top level**

Add the import near the other `kbforge` imports:

```python
from kbforge.publishers._http import ForgeError
```

and wrap the failure case by extending the existing `except ConfigError` block (line 139):

```python
    except ConfigError as exc:
        print(str(exc))
        return 2
    except ForgeError as exc:
        # The mirror never advanced, so the next run retries this same change.
        print(f"Publish failed: {exc}")
        return 1
```

- [ ] **Step 11: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_publisher.py -v`
Expected: PASS — 10 passed.

Then the full suite:

Run: `uv run pytest -q`
Expected: PASS — everything green, including the existing `tests/test_cli.py`.

- [ ] **Step 12: Commit**

```bash
git add src/kbforge/registry.py src/kbforge/__main__.py src/kbforge/publishers/dry_run.py tests/test_cli_publisher.py
git commit -m "feat: select publishers by name and wire the forge publishers into the CLI"
```

---

### Task 7: Pipeline-level retry-safety test

The spec's central safety claim — a failed publish must leave the mirror unadvanced so the next run retries — is currently only an argument. Pin it.

**Files:**
- Test: `tests/test_publish_failure.py`

**Interfaces:**
- Consumes: `kbforge.pipeline.run`, `kbforge.publishers._http.ForgeError`

- [ ] **Step 1: Write the failing test**

Create `tests/test_publish_failure.py`:

```python
from pathlib import Path

import pytest

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import run
from kbforge.publishers._http import ForgeError

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


class ExplodingPublisher:
    def kbforge_publisher_info(self):  # pragma: no cover - not exercised
        raise NotImplementedError

    def kbforge_publish(self, change, config) -> str:
        raise ForgeError(500, "https://api.example/x", "boom")


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list = []

    def kbforge_publish(self, change, config) -> str:
        self.published.append(change)
        return "https://forge.example/pr/1"


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app-x.md").write_text(DOC, "utf-8")
    return src


def test_failed_publish_does_not_advance_the_mirror(tmp_path: Path):
    src = _source(tmp_path)
    mirror = tmp_path / "mirror"
    state = tmp_path / "state"

    with pytest.raises(ForgeError):
        run(
            LocalFilesConnector(),
            ExplodingPublisher(),
            config={"path": str(src)},
            mirror=str(mirror),
            state_dir=str(state),
            publish_config={},
        )

    # Nothing was committed, so a retry still sees the change.
    recorder = RecordingPublisher()
    result = run(
        LocalFilesConnector(),
        recorder,
        config={"path": str(src)},
        mirror=str(mirror),
        state_dir=str(state),
        publish_config={},
    )

    assert result.url == "https://forge.example/pr/1"
    assert len(recorder.published) == 1
    assert recorder.published[0].files  # the change survived the failure


def test_successful_publish_advances_the_mirror_so_a_rerun_is_a_noop(tmp_path: Path):
    from kbforge.pipeline import NoOp

    src = _source(tmp_path)
    mirror = tmp_path / "mirror"
    state = tmp_path / "state"
    kwargs = {
        "config": {"path": str(src)},
        "mirror": str(mirror),
        "state_dir": str(state),
        "publish_config": {},
    }

    run(LocalFilesConnector(), RecordingPublisher(), **kwargs)
    second = run(LocalFilesConnector(), RecordingPublisher(), **kwargs)

    assert isinstance(second, NoOp)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_publish_failure.py -v`
Expected: PASS — 2 passed. No production code changes are needed; this test documents behaviour that already falls out of `pipeline.py:123-125` ordering. If it fails, the ordering has regressed and that is the bug.

- [ ] **Step 3: Commit**

```bash
git add tests/test_publish_failure.py
git commit -m "test: pin that a failed publish leaves the mirror unadvanced"
```

---

### Task 8: Documentation

**Files:**
- Modify: `README.md` (Status section, plus a new publish section after the LLM quickstart)
- Modify: `docs/architecture.md` (§0 stance, §5.2 publisher family)
- Modify: `CHANGELOG.md`

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Update the README Status section**

Replace the "Not built yet" paragraph in `README.md`:

```markdown
Not built yet: a credentialed system-of-record connector. See
[`docs/architecture.md`](docs/architecture.md) for the full map.
```

and in the paragraph above it, change "and a dry-run publisher" to:

```markdown
and three publishers — dry-run, GitHub, and GitLab
```

- [ ] **Step 2: Add a publish section to the README**

After the LLM synthesizer quickstart block, add:

```markdown
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
```

- [ ] **Step 3: Amend the architecture doc**

In `docs/architecture.md` §0, change the design-stance sentence:

```markdown
Design stance carried over from the main doc: **the core ships zero credentialed
connectors, zero CI logic.** Connectors are plugins; deployments are separate
repos. The interface is the product. Publishers are the exception that proves
it: publishing is the producer's own delivery mechanism, not an integration with
someone's system of record, so `github` and `gitlab` ship in core — reading their
credentials from the environment, never from config. See
[`design/2026-07-24-forge-publisher-design.md`](design/2026-07-24-forge-publisher-design.md).
```

Then find §5.2 (the publisher family) and append:

```markdown
Three publishers ship in core: `dry-run` (default; writes to a directory),
`github` (pull requests) and `gitlab` (merge requests). All three implement
`kbforge_publish` and `kbforge_validate_publish_config`; none implements a merge
method, which is how §5.2's never-merge rule is enforced structurally rather
than by convention.
```

- [ ] **Step 4: Update the changelog**

At the top of `CHANGELOG.md`, under a new `## [Unreleased]` heading (or the existing one if present):

```markdown
### Added
- GitHub (`--publisher github`) and GitLab (`--publisher gitlab`) publishers that
  open or update a real pull/merge request from a `ProposedChange`. No new
  runtime dependencies — both run on stdlib `urllib`. Tokens are read from
  `GITHUB_TOKEN` / `GITLAB_TOKEN` (configurable via `token_env`), never the CLI.
- `--publisher NAME` and `--publish-set KEY=VALUE` CLI flags; `kbforge list` now
  lists publishers.
- `kbforge_validate_publish_config` hookspec so publisher config is checked
  before the pipeline runs.

### Changed
- Publishers are now resolved by name. Previously the first registered plugin
  implementing `kbforge_publish` won, which became order-dependent with more
  than one publisher installed.
- The core design stance is now "zero credentialed *connectors*" — publishing is
  delivery, not a system-of-record integration.
```

- [ ] **Step 5: Verify the docs match reality**

Run: `uv run kbforge list`
Expected: output includes a `publishers:` section listing `dry-run`, `github`, `gitlab`.

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/architecture.md CHANGELOG.md
git commit -m "docs: document the GitHub and GitLab publishers"
```

---

### Task 9: Live tests against real forges

Per the project's convention of live-testing the real thing. These are skipped by default.

**Files:**
- Modify: `tests/conftest.py` (broaden the `live` marker description and `--run-live` help)
- Test: `tests/test_forge_live.py`

**Interfaces:**
- Consumes: `GitHubPublisher` (Task 4), `GitLabPublisher` (Task 5)

**Prerequisites (the human running this must set these up):**
- A scratch GitHub repo and a scratch GitLab project, each with at least one commit on the default branch.
- `GITHUB_TOKEN` with `contents:write` + `pull_requests:write` on that repo; `GITLAB_TOKEN` with `api` scope on that project.
- `KBFORGE_LIVE_GITHUB_REPO=owner/name` and `KBFORGE_LIVE_GITLAB_REPO=group/project`.
- These belong in `.env` alongside the existing live-LLM settings.

- [ ] **Step 1: Broaden the marker description**

In `tests/conftest.py`, update the two strings that assume the live marker is LLM-only:

```python
        help="run opt-in live tests against real providers (LLM, GitHub, GitLab)",
```

and

```python
    config.addinivalue_line(
        "markers", "live: test that calls a real external provider"
    )
```

- [ ] **Step 2: Write the live test**

Create `tests/test_forge_live.py`:

```python
"""Opt-in live tests: these open real pull/merge requests. Run with
`uv run pytest --run-live tests/test_forge_live.py -v` and delete the resulting
PR/MR afterwards (kbforge never merges, so nothing lands on its own)."""

import os

import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.github import GitHubPublisher
from kbforge.publishers.gitlab import GitLabPublisher

pytestmark = pytest.mark.live


def _change(marker: str) -> ProposedChange:
    return ProposedChange(
        branch_hint="sync/kbforge-live-test",
        files={
            "concepts/live-test/overview.md": (
                f"---\ntype: application\ntitle: Live Test\n---\n\n"
                f"Written by kbforge's live test ({marker}).\n"
            )
        },
        summary=ChangeSummary(claims_added=["concepts/live-test/overview.md"]),
    )


def test_github_opens_a_real_pull_request():
    repo = os.environ.get("KBFORGE_LIVE_GITHUB_REPO")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("set KBFORGE_LIVE_GITHUB_REPO and GITHUB_TOKEN")

    config = {"repo": repo, "base_path": "kbforge-live"}
    assert GitHubPublisher().kbforge_validate_publish_config(config) == []

    url = GitHubPublisher().kbforge_publish(_change("github"), config)
    assert url.startswith("http")
    print(f"\nGitHub PR: {url}")


def test_github_rerun_updates_the_same_pull_request():
    repo = os.environ.get("KBFORGE_LIVE_GITHUB_REPO")
    if not repo or not os.environ.get("GITHUB_TOKEN"):
        pytest.skip("set KBFORGE_LIVE_GITHUB_REPO and GITHUB_TOKEN")

    config = {"repo": repo, "base_path": "kbforge-live"}
    first = GitHubPublisher().kbforge_publish(_change("run-1"), config)
    second = GitHubPublisher().kbforge_publish(_change("run-2"), config)
    assert first == second  # same PR updated, not a second one opened


def test_gitlab_opens_a_real_merge_request():
    repo = os.environ.get("KBFORGE_LIVE_GITLAB_REPO")
    if not repo or not os.environ.get("GITLAB_TOKEN"):
        pytest.skip("set KBFORGE_LIVE_GITLAB_REPO and GITLAB_TOKEN")

    config = {"repo": repo, "base_path": "kbforge-live"}
    assert GitLabPublisher().kbforge_validate_publish_config(config) == []

    url = GitLabPublisher().kbforge_publish(_change("gitlab"), config)
    assert url.startswith("http")
    print(f"\nGitLab MR: {url}")


def test_gitlab_rerun_updates_the_same_merge_request():
    repo = os.environ.get("KBFORGE_LIVE_GITLAB_REPO")
    if not repo or not os.environ.get("GITLAB_TOKEN"):
        pytest.skip("set KBFORGE_LIVE_GITLAB_REPO and GITLAB_TOKEN")

    config = {"repo": repo, "base_path": "kbforge-live"}
    first = GitLabPublisher().kbforge_publish(_change("run-1"), config)
    second = GitLabPublisher().kbforge_publish(_change("run-2"), config)
    assert first == second
```

- [ ] **Step 3: Verify they skip by default**

Run: `uv run pytest tests/test_forge_live.py -v`
Expected: 4 skipped, with reason "live test; pass --run-live to enable".

- [ ] **Step 4: Run them for real**

Run: `uv run pytest --run-live tests/test_forge_live.py -v -s`
Expected: PASS, printing a GitHub PR URL and a GitLab MR URL. Open both and confirm: the files landed under `kbforge-live/concepts/live-test/`, the description renders the change summary, and the rerun tests updated the same PR/MR rather than opening a second.

- [ ] **Step 5: Update `.env.example`**

Add to `.env.example`:

```bash
# Live forge publisher tests (pytest --run-live)
GITHUB_TOKEN=
GITLAB_TOKEN=
KBFORGE_LIVE_GITHUB_REPO=owner/scratch-repo
KBFORGE_LIVE_GITLAB_REPO=group/scratch-project
```

- [ ] **Step 6: Commit**

```bash
git add tests/test_forge_live.py tests/conftest.py .env.example
git commit -m "test: add opt-in live tests against real GitHub and GitLab"
```

---

## Verification checklist

Before calling this done:

- [ ] `uv run pytest -q` — full suite green
- [ ] `uv run pytest --run-live -q` — live LLM and live forge tests pass
- [ ] `uv run ruff check src tests && uv run ruff format --check src tests` — clean
- [ ] `uv run kbforge list` — shows `publishers:` with all three
- [ ] `grep -r "httpx\|requests\|PyGithub\|python-gitlab" pyproject.toml` — no matches (dependency constraint held)
- [ ] A real PR and a real MR were opened, inspected, and closed by hand
- [ ] Rerunning against an unchanged source still prints `NoOp` and opens nothing
