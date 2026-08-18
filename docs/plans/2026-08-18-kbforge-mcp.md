# kbforge-mcp (0.7.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kbforge-mcp`, a separate distribution that turns any MCP server
with a select tool and a read-by-id tool into a kbforge source through
configuration alone.

**Architecture:** A pluggy connector discovered through the `kbforge.connectors`
entry-point group, so core needs zero changes. Fetch is two jobs: a **selector**
turns a tool response into document ids, and a fixed **reader** fetches each id
verbatim. Response mapping is protocol-first — MCP's own content-block types are
the vocabulary, so the common case needs no config. Read-only is structural: the
callable set *is* the two configured tool names.

**Tech Stack:** Python ≥3.12, `mcp` Python SDK v2 (async `Client`), Pydantic v2,
pluggy, uv workspaces, pytest, ruff + ty via prek.

**Spec:** [`docs/design/2026-08-16-mcp-source-connector-design.md`](../design/2026-08-16-mcp-source-connector-design.md)

## Global Constraints

Every task's requirements implicitly include this section.

- **The package is a uv workspace member at `packages/kbforge-mcp/`.** Distribution
  name `kbforge-mcp`, import name `kbforge_mcp`. It is a separate *distribution*,
  not a separate repository — one CI, one live-credential set, one set of
  conventions. Splitting it out later is cheap; duplicating the tooling now is not.
- **Zero changes to `src/kbforge/`.** If a task appears to need one, that is a
  finding to report, not a change to make. The only root-level edits allowed are
  `pyproject.toml` (workspace wiring and `testpaths`).
- **The callable set is exactly `{select.tool, read.tool}`** (spec §2.4). No tool
  discovery loop, no allowlist config key, no third call site.
- **Never write to a source.** No tool outside the two configured names is ever
  called, and config SHOULD prefer a server's read-only endpoint where one exists.
- **Credentials come from environment variables named in config, never values in
  config and never on the command line** (`architecture.md:35`).
- **`normalize` is pure** — no network, no clock, no randomness. `retrieved_at` is
  stamped in `fetch` into `RawRecord.anchor_hint` (spec §10.3).
- **Python ≥3.12**, `from __future__ import annotations` at the top of every module,
  ruff lint set `E,F,I,UP`, line length 88 (ruff default).
- **Import ordering is whatever `ruff check --select I --fix` produces.** Both
  `kbforge` and `kbforge_mcp` are first-party (root `[tool.ruff] src` lists both
  package roots), so they group together *after* third-party `mcp`/`pytest`. The
  import blocks in this plan's code samples are illustrative on this point only —
  run ruff and take its ordering rather than preserving the sample's.
- **Run `uv run pytest` before every commit**, not after every edit.
- Tests must be pristine: no warnings, no stray output.

---

## File Structure

```
packages/kbforge-mcp/
├── pyproject.toml               distribution metadata, mcp dep, entry point
├── README.md                    what it is, one config example
└── src/kbforge_mcp/
    ├── __init__.py              version only
    ├── slug.py                  raw server id → path-safe native_id (pure)
    ├── config.py                config models + offline validation (pure)
    ├── mapping.py               DocRef + the three response tiers (pure)
    ├── client.py                McpClient: session, two-tool set, guards
    ├── selectors.py             StaticSelector, QuerySelector
    └── connector.py             the four hookimpls + CONNECTOR instance
└── tests/
    ├── fake_server.py           FastMCP fixture server emitting every tier
    ├── test_slug.py
    ├── test_config.py
    ├── test_mapping.py
    ├── test_client.py
    ├── test_connector.py
    ├── test_stdio.py
    └── test_live.py
```

Each module is pure where it can be: `slug`, `config`, and `mapping` have no I/O
at all and carry most of the test weight. `client` owns every await. `connector`
owns the one `asyncio.run`.

**No second `conftest.py`.** The repo-root `tests/conftest.py` already registers
`--run-live` and the `live` marker; pytest's rootdir is the repo root, so it
applies to these tests too. Adding `pytest_addoption` again raises
`ValueError: option names {'--run-live'} already added`.

---

## Task 1: Workspace scaffolding and the slug

**Files:**
- Create: `packages/kbforge-mcp/pyproject.toml`
- Create: `packages/kbforge-mcp/src/kbforge_mcp/__init__.py`
- Create: `packages/kbforge-mcp/src/kbforge_mcp/slug.py`
- Create: `packages/kbforge-mcp/tests/test_slug.py`
- Modify: `pyproject.toml` (root — workspace, dev dependency, `testpaths`)

**Interfaces:**
- Produces: `native_id_for(raw: str) -> str`, `SlugError(ValueError)` in
  `kbforge_mcp.slug`. Every later task uses these exact names.

- [ ] **Step 1: Create the distribution**

`packages/kbforge-mcp/pyproject.toml`:

```toml
[project]
name = "kbforge-mcp"
version = "0.1.0"
description = "MCP source connector for kbforge"
readme = "README.md"
requires-python = ">=3.12"
authors = [{ name = "Qing", email = "qingye779@gmail.com" }]
license = { text = "MIT" }
dependencies = [
    "kbforge>=0.6.0",
    "mcp>=2.0",
    "pydantic>=2.0",
]

[project.entry-points."kbforge.connectors"]
mcp = "kbforge_mcp.connector:CONNECTOR"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/kbforge_mcp"]
```

The entry-point value resolves to a **module-level instance**, not a class:
`registry.py` registers connectors as `pm.register(LocalFilesConnector())`, and
pluggy's `load_setuptools_entrypoints` calls `ep.load()` then `register()` on
whatever comes back. A class would register unbound methods.

`packages/kbforge-mcp/src/kbforge_mcp/__init__.py`:

```python
"""MCP source connector for kbforge. See the design note in docs/design/."""

__version__ = "0.1.0"
```

- [ ] **Step 2: Wire the workspace into the root `pyproject.toml`**

Append these two tables, and edit `testpaths`:

```toml
[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
kbforge-mcp = { workspace = true }
```

Add `"kbforge-mcp"` to `[project.optional-dependencies].dev` so `uv sync
--all-extras --dev` installs it editable, and change:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "packages/kbforge-mcp/tests"]
```

Then run `uv sync --all-extras --dev` and confirm it resolves.

- [ ] **Step 3: Write the failing slug tests**

`packages/kbforge-mcp/tests/test_slug.py`:

```python
from __future__ import annotations

import pytest

from kbforge_mcp.slug import SlugError, native_id_for


def test_url_reduces_to_its_path_without_scheme_host_or_extension():
    raw = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html"
    assert native_id_for(raw) == "AmazonS3/latest/userguide/bucketnamingrules"


def test_query_and_fragment_are_dropped():
    raw = "https://example.com/docs/guide.html?v=2#section"
    assert native_id_for(raw) == "docs/guide"


def test_plain_path_is_kept_and_normalized():
    assert native_id_for("docs/handbook/onboarding.md") == "docs/handbook/onboarding"
    assert native_id_for("/leading/slash.md") == "leading/slash"


def test_traversal_is_refused_at_fetch_time_not_at_publish_time():
    # `safe_join` would raise PathError during publish -- after synthesis, after
    # tokens. Refuse here instead.
    with pytest.raises(SlugError, match="escapes the bundle"):
        native_id_for("../../.github/workflows/release.yml")


def test_empty_and_content_free_ids_are_refused():
    # doc_id="" passes assert_fetch_contract's uniqueness check and
    # concept_path("") renders concepts//overview.md, which normalizes onto a
    # root-level concept. Refuse before it can collide.
    for raw in ("", "   ", "/", "///"):
        with pytest.raises(SlugError):
            native_id_for(raw)


def test_only_a_short_alphanumeric_extension_is_stripped():
    assert native_id_for("reports/2024.annual.summary.pdf") == "reports/2024.annual.summary"
    assert native_id_for("api/v1.2/reference") == "api/v1.2/reference"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest packages/kbforge-mcp/tests/test_slug.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge_mcp.slug'`

- [ ] **Step 5: Implement `slug.py`**

```python
"""Reduce a server-supplied document id to a path-safe `native_id`.

Server ids are frequently URLs. `synthesize.concept_path` builds a bundle path
straight from `native_id` (`concepts/{stem}/overview.md`), so a raw URL renders
to `concepts/https:/docs.aws.amazon.com/...`, and a `../..` id reaches
`safe_join` and dies as a PathError at publish time -- after synthesis, after
tokens. Both are refused here instead.

Identity and provenance are different things and `ResourceAnchor` has a field
for each: this produces `native_id`, while the untouched original becomes `url`.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A trailing extension only when it is short and alphanumeric, so `guide.html`
# loses its suffix but `api/v1.2/reference` keeps its version segment.
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,5}$")


class SlugError(ValueError):
    """A server-supplied id cannot be reduced to a path-safe native_id."""


def native_id_for(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SlugError(f"document id is empty: {raw!r}")
    text = raw.strip()
    parts = urlsplit(text)
    # urlsplit already discards query and fragment into their own fields.
    path = parts.path if parts.scheme else text.split("?", 1)[0].split("#", 1)[0]

    segments = [s for s in path.split("/") if s not in ("", ".")]
    if ".." in segments:
        raise SlugError(f"document id escapes the bundle: {raw!r}")
    if not segments:
        raise SlugError(f"document id has no path content: {raw!r}")

    stripped = _EXTENSION.sub("", segments[-1])
    segments[-1] = stripped or segments[-1]
    return "/".join(segments)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest packages/kbforge-mcp/tests/test_slug.py -v`
Expected: PASS, 6 passed

- [ ] **Step 7: Confirm the whole suite still collects and passes**

Run: `uv run pytest`
Expected: the pre-existing 286 pass, plus the 6 new ones.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml packages/kbforge-mcp
git commit -m "feat(mcp): scaffold kbforge-mcp and the path-safe native_id slug"
```

---

## Task 2: Config models and offline validation

**Files:**
- Create: `packages/kbforge-mcp/src/kbforge_mcp/config.py`
- Create: `packages/kbforge-mcp/tests/test_config.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces, in `kbforge_mcp.config`:
  - `StdioTransport`, `HttpTransport`, `IdsMapping`, `SelectSpec`, `ReadSpec`,
    `McpSourceConfig` (all Pydantic `BaseModel`)
  - `McpSourceConfig.tool_names -> frozenset[str]` — the two-tool callable set
  - `problems_for(config: dict) -> list[str]` — offline, no network, `[]` means ok

- [ ] **Step 1: Write the failing config tests**

`packages/kbforge-mcp/tests/test_config.py`:

```python
from __future__ import annotations

from kbforge_mcp.config import McpSourceConfig, problems_for

STDIO = {
    "system": "aws_docs",
    "transport": {
        "kind": "stdio",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
    },
    "select": {
        "tool": "search_documentation",
        "args": {"search_phrase": "S3 bucket naming", "limit": 20},
        "ids": {"list": "results", "id": "url", "title": "title"},
    },
    "read": {"tool": "read_documentation", "id_arg": "url"},
}


def test_a_valid_stdio_source_has_no_problems():
    assert problems_for(STDIO) == []


def test_a_valid_http_source_has_no_problems():
    cfg = dict(STDIO)
    cfg["transport"] = {
        "kind": "http",
        "url": "https://api.githubcopilot.com/mcp/x/repos/readonly",
        "auth_env": "GITHUB_TOKEN",
    }
    assert problems_for(cfg) == []


def test_the_callable_set_is_exactly_the_two_configured_tools():
    cfg = McpSourceConfig.model_validate(STDIO)
    assert cfg.tool_names == frozenset({"search_documentation", "read_documentation"})


def test_an_unknown_transport_kind_is_rejected_offline():
    cfg = dict(STDIO)
    cfg["transport"] = {"kind": "carrier-pigeon", "url": "https://x"}
    assert any("transport" in p for p in problems_for(cfg))


def test_a_transport_without_a_kind_is_rejected_rather_than_sniffed():
    # v0.2 carried both transports in one `server:` string with no discriminator.
    # There is no sniffing: absent `kind` is a config error.
    cfg = dict(STDIO)
    cfg["transport"] = {"url": "https://example.com/mcp"}
    assert any("kind" in p for p in problems_for(cfg))


def test_a_static_selector_needs_no_select_tool_but_needs_ids():
    cfg = {k: v for k, v in STDIO.items() if k != "select"}
    cfg["static_ids"] = ["docs/a.md", "docs/b.md"]
    assert problems_for(cfg) == []
    cfg_bad = {k: v for k, v in STDIO.items() if k != "select"}
    assert any("static_ids" in p for p in problems_for(cfg_bad))


def test_select_and_static_ids_are_mutually_exclusive():
    cfg = dict(STDIO)
    cfg["static_ids"] = ["docs/a.md"]
    assert any("mutually exclusive" in p for p in problems_for(cfg))


def test_auth_env_names_a_variable_and_never_carries_a_value():
    cfg = dict(STDIO)
    cfg["transport"] = {
        "kind": "http",
        "url": "https://example.com/mcp",
        "auth_env": "ghp_realtokenvalue",
    }
    assert any("looks like a value" in p for p in problems_for(cfg))


def test_problems_are_returned_not_raised():
    assert isinstance(problems_for({}), list)
    assert problems_for({}) != []
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest packages/kbforge-mcp/tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge_mcp.config'`

- [ ] **Step 3: Implement `config.py`**

```python
"""Config models for an MCP source, and the offline validation the CLI runs
before any network I/O.

`transport.kind` is an explicit discriminator. v0.2 of the design note carried
`server: https://...  # or a stdio command` -- two incompatible transports in one
string that `kbforge_validate_config` was expected to classify offline. There is
no sniffing here: the kind is declared or the config is invalid.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# A conservative shape for "this is an env var NAME, not a token VALUE".
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class _Strict(BaseModel):
    # A typo in a source config must be an error, not a silently ignored key.
    model_config = ConfigDict(extra="forbid")


class StdioTransport(_Strict):
    kind: Literal["stdio"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    """Names of environment variables to pass through. Names, never values."""


class HttpTransport(_Strict):
    kind: Literal["http"]
    url: str
    auth_env: str | None = None
    """Name of the env var holding the bearer token. Never the token itself."""


class IdsMapping(_Strict):
    list: str
    """Key in `structuredContent` holding the array of result records."""
    id: str
    title: str | None = None


class SelectSpec(_Strict):
    tool: str
    args: dict = Field(default_factory=dict)
    ids: IdsMapping | None = None
    """Omitted only when the select tool returns tier-1 resource links."""


class ReadSpec(_Strict):
    tool: str
    id_arg: str
    """The argument name the reader takes the id under. It is not `id`: AWS says
    `url` and GitHub says `path`, which is why no default could be right."""
    static_args: dict = Field(default_factory=dict)
    """Constant arguments alongside the id -- GitHub's reader needs owner+repo."""
    text_key: str | None = None
    """Tier-2 only: the `structuredContent` key holding the document body."""


class McpSourceConfig(_Strict):
    system: str
    transport: StdioTransport | HttpTransport = Field(discriminator="kind")
    read: ReadSpec
    select: SelectSpec | None = None
    static_ids: list[str] | None = None
    media_type: str = "text/markdown"

    @property
    def tool_names(self) -> frozenset[str]:
        """The entire callable set. There is no third entry and no config key
        that could add one (design note §2.4)."""
        names = {self.read.tool}
        if self.select is not None:
            names.add(self.select.tool)
        return frozenset(names)


def problems_for(config: dict) -> list[str]:
    """Human-readable problems; `[]` means the config is usable. No network I/O."""
    try:
        cfg = McpSourceConfig.model_validate(config)
    except ValidationError as exc:
        return [
            f"config {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()
        ]

    problems: list[str] = []
    if cfg.select is not None and cfg.static_ids is not None:
        problems.append(
            "config 'select' and 'static_ids' are mutually exclusive: a source "
            "has one selector"
        )
    if cfg.select is None and not cfg.static_ids:
        problems.append(
            "config needs either 'select' (a select tool) or 'static_ids' (a "
            "configured id list); a server whose select tool returns only prose "
            "is supported through 'static_ids'"
        )
    auth_env = getattr(cfg.transport, "auth_env", None)
    if auth_env and not _ENV_NAME.match(auth_env):
        problems.append(
            f"config 'transport.auth_env' looks like a value, not an environment "
            f"variable name: {auth_env!r}"
        )
    return problems
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `uv run pytest packages/kbforge-mcp/tests/test_config.py -v`
Expected: PASS, 9 passed

- [ ] **Step 5: Commit**

```bash
git add packages/kbforge-mcp
git commit -m "feat(mcp): config models with an explicit transport discriminator"
```

---

## Task 3: Response mapping — the three tiers

**Files:**
- Create: `packages/kbforge-mcp/src/kbforge_mcp/mapping.py`
- Create: `packages/kbforge-mcp/tests/test_mapping.py`

**Interfaces:**
- Consumes: `native_id_for`, `SlugError` (Task 1); `IdsMapping`, `ReadSpec` (Task 2).
- Produces, in `kbforge_mcp.mapping`:
  - `DocRef` — frozen dataclass `(raw_id: str, native_id: str, url: str | None, title: str | None)`
  - `MappingError(RuntimeError)`
  - `refs_from_select(result, ids: IdsMapping | None) -> list[DocRef]`
  - `records_from_read(result, ref: DocRef, spec: ReadSpec, media_type: str) -> list[RawRecord]`

**`DocRef` carries the id twice on purpose.** `raw_id` is what the reader must be
passed (`https://docs.aws.amazon.com/...html`); `native_id` is the slug identity
is built from. Passing the slug back to the reader is the obvious bug here.

- [ ] **Step 1: The SDK symbols, already verified**

These were checked against the installed `mcp` package; use them as given.

| Symbol | Fields you need |
|---|---|
| `CallToolResult` | `content`, `structured_content`, `is_error` |
| `TextContent` | `type` (`"text"`), `text` |
| `EmbeddedResource` | `type` (`"resource"`), `resource` |
| `ResourceLink` | `type`, `uri`, `name`, `title`, `mime_type` |
| `TextResourceContents` | `uri`, `text`, `mime_type` |
| `BlobResourceContents` | `uri`, `blob`, `mime_type` |

**The SDK uses snake_case `mime_type`, not `mimeType`.** Reading `mimeType` returns
`None` silently and every document falls back to the configured default — a bug no
test in this task would catch, because the fixture server returns plain text.

- [ ] **Step 2: Write the failing mapping tests**

`packages/kbforge-mcp/tests/test_mapping.py`:

```python
from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent

from kbforge_mcp.config import IdsMapping, ReadSpec
from kbforge_mcp.mapping import DocRef, MappingError, records_from_read, refs_from_select


def _text(body: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=body)])


def test_tier2_select_extracts_refs_from_structured_content():
    result = CallToolResult(
        content=[TextContent(type="text", text="ignored prose")],
        structured_content={
            "results": [
                {"url": "https://docs.aws.amazon.com/s3/naming.html", "title": "Naming"},
                {"url": "https://docs.aws.amazon.com/s3/limits.html", "title": "Limits"},
            ]
        },
    )
    refs = refs_from_select(result, IdsMapping(list="results", id="url", title="title"))
    assert [r.native_id for r in refs] == ["s3/naming", "s3/limits"]
    # The reader must receive the ORIGINAL id, never the slug.
    assert refs[0].raw_id == "https://docs.aws.amazon.com/s3/naming.html"
    assert refs[0].url == "https://docs.aws.amazon.com/s3/naming.html"
    assert refs[0].title == "Naming"


def test_a_non_url_id_gets_no_url_only_an_identity():
    result = CallToolResult(
        content=[], structured_content={"items": [{"path": "docs/onboarding.md"}]}
    )
    refs = refs_from_select(result, IdsMapping(list="items", id="path"))
    assert refs[0].native_id == "docs/onboarding"
    assert refs[0].raw_id == "docs/onboarding.md"
    assert refs[0].url is None


def test_tier3_select_fails_closed_with_a_message_naming_the_remedy():
    # No prose heuristics. A bare-text select response is unsupported, and the
    # error must point at static_ids rather than guess.
    with pytest.raises(MappingError, match="static_ids"):
        refs_from_select(_text("- 1 Overview\n- 2 Architecture"), None)


def test_a_missing_list_key_is_an_error_not_an_empty_result():
    result = CallToolResult(content=[], structured_content={"data": []})
    with pytest.raises(MappingError, match="results"):
        refs_from_select(result, IdsMapping(list="results", id="url"))


def test_tier3_read_is_complete_because_identity_came_from_the_ref():
    # The reader is called with an id we already have, so its response only has
    # to supply bytes. Concatenating text blocks is deterministic, not a guess.
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(
        content=[
            TextContent(type="text", text="first half"),
            TextContent(type="text", text="second half"),
        ]
    )
    records = records_from_read(
        result, ref, ReadSpec(tool="read", id_arg="path"), "text/markdown"
    )
    assert len(records) == 1
    assert records[0].payload.decode() == "first half\n\nsecond half"
    assert records[0].anchor_hint["native_id"] == "docs/a"
    assert records[0].media_type == "text/markdown"


def test_tier2_read_takes_the_configured_text_key():
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    result = CallToolResult(content=[], structured_content={"body": "the content"})
    spec = ReadSpec(tool="read", id_arg="path", text_key="body")
    records = records_from_read(result, ref, spec, "text/markdown")
    assert records[0].payload.decode() == "the content"


def test_an_empty_read_response_is_an_error_not_an_empty_document():
    ref = DocRef(raw_id="docs/a.md", native_id="docs/a", url=None, title="A")
    with pytest.raises(MappingError, match="no content"):
        records_from_read(
            CallToolResult(content=[]), ref, ReadSpec(tool="r", id_arg="p"), "text/markdown"
        )
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest packages/kbforge-mcp/tests/test_mapping.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge_mcp.mapping'`

- [ ] **Step 4: Implement `mapping.py`**

```python
"""Turn MCP tool results into DocRefs and RawRecords.

Protocol-first: MCP's own content-block types are the mapping vocabulary, so the
common case needs no configuration. Tiers are tried in order and the first that
applies wins.

The two stages carry very different burdens, because **identity is an input to
the reader, not an output of it**. A selector must produce ids it does not
already know, so a bare-prose response is unmappable and fails closed. A reader
is called with an id we already hold, so its response only has to supply bytes --
which makes "concatenate the text blocks" deterministic rather than a heuristic.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from kbforge.models import RawRecord
from mcp.types import CallToolResult

from kbforge_mcp.config import IdsMapping, ReadSpec
from kbforge_mcp.slug import SlugError, native_id_for


class MappingError(RuntimeError):
    """A tool result cannot be mapped onto the fields kbforge needs."""


@dataclass(frozen=True)
class DocRef:
    """One selected document.

    `raw_id` is what the reader must be passed; `native_id` is the path-safe slug
    identity is built from. They differ whenever the server's id is a URL, and
    passing the slug back to the reader is the mistake this split exists to make
    impossible to write by accident.
    """

    raw_id: str
    native_id: str
    url: str | None
    title: str | None


def _resource_blocks(result: CallToolResult) -> list:
    return [b for b in result.content if getattr(b, "type", "") in ("resource", "resource_link")]


def _text_blocks(result: CallToolResult) -> list[str]:
    return [b.text for b in result.content if getattr(b, "type", "") == "text"]


def _ref_for(raw_id: str, title: str | None) -> DocRef:
    try:
        native = native_id_for(raw_id)
    except SlugError as exc:
        raise MappingError(f"unusable document id from server: {exc}") from exc
    return DocRef(
        raw_id=raw_id,
        native_id=native,
        url=raw_id if "://" in raw_id else None,
        title=title,
    )


def refs_from_select(result: CallToolResult, ids: IdsMapping | None) -> list[DocRef]:
    blocks = _resource_blocks(result)
    if blocks:  # tier 1 -- the protocol already carries the identity
        refs = []
        for b in blocks:
            uri = getattr(b, "uri", None) or getattr(getattr(b, "resource", None), "uri", None)
            if uri is None:
                raise MappingError("resource block carries no uri")
            # ResourceLink carries both; `title` is the human-facing one.
            refs.append(
                _ref_for(str(uri), getattr(b, "title", None) or getattr(b, "name", None))
            )
        return refs

    if result.structured_content is not None and ids is not None:  # tier 2
        rows = result.structured_content.get(ids.list)
        if rows is None:
            raise MappingError(
                f"select response has no {ids.list!r} key; keys are "
                f"{sorted(result.structured_content)}"
            )
        if not isinstance(rows, list):
            raise MappingError(f"select response key {ids.list!r} is not a list")
        refs = []
        for row in rows:
            raw = row.get(ids.id) if isinstance(row, dict) else None
            if raw is None:
                raise MappingError(f"select result row has no {ids.id!r} key: {row!r}")
            refs.append(_ref_for(str(raw), row.get(ids.title) if ids.title else None))
        return refs

    # tier 3 -- fails closed. No "first text block", no regex over an outline.
    raise MappingError(
        "select response carries neither resource blocks nor structuredContent; "
        "a prose-only select tool is not mappable -- configure 'static_ids' instead"
    )


def records_from_read(
    result: CallToolResult,
    ref: DocRef,
    spec: ReadSpec,
    media_type: str,
) -> list[RawRecord]:
    def record(payload: bytes, native_id: str, url: str | None, mtype: str) -> RawRecord:
        return RawRecord(
            anchor_hint={"native_id": native_id, "url": url, "title": ref.title},
            media_type=mtype,
            payload=payload,
        )

    blocks = _resource_blocks(result)
    if blocks:  # tier 1 -- one call may legitimately yield many documents
        carried = []
        for b in blocks:
            res = getattr(b, "resource", b)
            uri = str(getattr(res, "uri", ref.raw_id))
            text, blob = getattr(res, "text", None), getattr(res, "blob", None)
            if text is not None:
                payload = text.encode("utf-8")
            elif blob is not None:
                payload = base64.b64decode(blob)
            else:
                continue  # a bare link with no content is not a document
            carried.append((uri, payload, getattr(res, "mime_type", None)))

        # One document in, one document out: the identity we ASKED for wins. A
        # server's own uri may encode volatile state -- GitHub returns
        # `repo://owner/repo/sha/<commit-sha>/contents/<path>`, and slugging that
        # would put a commit sha inside every native_id, so identity would churn
        # on every commit and nothing would ever diff as `modified`.
        # Only a one-to-many read (a "read this folder" tool) needs new
        # identities, and then the uris are the only source for them.
        if len(carried) == 1:
            uri, payload, mime = carried[0]
            return [record(payload, ref.native_id, ref.url, mime or media_type)]
        if carried:
            return [
                record(payload, _ref_for(uri, None).native_id,
                       uri if "://" in uri else ref.url, mime or media_type)
                for uri, payload, mime in carried
            ]

    if spec.text_key and result.structured_content is not None:  # tier 2
        body = result.structured_content.get(spec.text_key)
        if body is None:
            raise MappingError(
                f"read response has no {spec.text_key!r} key for {ref.native_id}"
            )
        return [record(str(body).encode("utf-8"), ref.native_id, ref.url, media_type)]

    texts = _text_blocks(result)  # tier 3 -- complete, because identity is an input
    if texts:
        return [record("\n\n".join(texts).encode("utf-8"), ref.native_id, ref.url, media_type)]

    raise MappingError(f"read response for {ref.native_id} carried no content")
```

- [ ] **Step 5: Run to verify the tests pass**

Run: `uv run pytest packages/kbforge-mcp/tests/test_mapping.py -v`
Expected: PASS, 7 passed

- [ ] **Step 6: Commit**

```bash
git add packages/kbforge-mcp
git commit -m "feat(mcp): protocol-first response mapping across the three tiers"
```

---

## Task 4: The client — the two-tool set and the guards

**Files:**
- Create: `packages/kbforge-mcp/src/kbforge_mcp/client.py`
- Create: `packages/kbforge-mcp/tests/fake_server.py`
- Create: `packages/kbforge-mcp/tests/test_client.py`

**Interfaces:**
- Consumes: `McpSourceConfig`, `StdioTransport`, `HttpTransport` (Task 2).
- Produces, in `kbforge_mcp.client`:
  - `ToolNotAllowed(RuntimeError)`, `ToolCallFailed(RuntimeError)`
  - `McpClient` with `async def prepare() -> None` and
    `async def call(name: str, args: dict) -> CallToolResult`
  - `open_session(cfg: McpSourceConfig)` — an async context manager yielding `McpClient`

The fixture server is **a real `FastMCP` server driven by a real `Client`**, not a
fake. No single live server produces every tier, an `is_error` result, *and* a
tool that declares itself mutating — which is exactly why the control is authored.

- [ ] **Step 1: Write the fixture server**

`packages/kbforge-mcp/tests/fake_server.py`:

```python
"""A real MCP server covering every response shape the mapping must handle.

Driven in-process by a real Client, so these tests exercise real protocol
serialization with no network and no fakes.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

mcp = MCPServer("kbforge-mcp-fixture")

DOCS = {
    "docs/onboarding.md": "# Onboarding\n\nHow to get started.",
    "docs/retention.md": "# Retention\n\nHow long we keep things.",
}


# The return annotation must be PRECISE or the SDK emits no structuredContent at
# all. A bare `-> dict` yields `structured_content=None` silently (the tier-2
# selector test would then fail closed into tier 3), and `structured_output=True`
# on a bare `dict` raises InvalidSignature at registration. A parameterized dict
# or a Pydantic model works; both were verified against the installed SDK.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def search_docs(query: str) -> dict[str, list[dict[str, str]]]:
    """Tier-2 selector: ids in structuredContent."""
    return {"results": [{"path": p, "title": p} for p in sorted(DOCS)]}


# NOTE: a `-> str` tool ALSO gets structuredContent, wrapped as {"result": ...},
# alongside the text block. That is why `records_from_read`'s tier-2 branch is
# gated on `spec.text_key` being configured rather than on structured_content
# merely being present -- do not "simplify" that gate away, or every tier-3
# reader would silently take the tier-2 path.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def read_doc(path: str) -> str:
    """Tier-3 reader: bare markdown. Identity came in as `path`."""
    if path not in DOCS:
        raise ValueError(f"no such document: {path}")
    return DOCS[path]


# `-> str` gives this one structuredContent too ({"result": "..."}), but the
# tier-2 selector branch also requires `ids` to be configured, and the prose-only
# source configures none -- so it still lands in tier 3 and fails closed.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
def outline(query: str) -> str:
    """Tier-3 selector: prose only. Must fail closed."""
    return "- 1 Onboarding\n- 2 Retention"


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False))
def delete_doc(path: str) -> str:
    """Declares itself mutating. Must never be called."""
    DOCS.pop(path, None)
    return "deleted"
```

`MCPServer` is verified. The whole seam was proved end to end against the installed
SDK before this task: `Client(mcp)` connects in-process, `list_tools()` round-trips
`read_only_hint` as `True/True/True/False` for these four tools, and a tool that
raises returns `is_error=True` with `"Error executing tool read_doc: no such
document: ..."` in a text block.

- [ ] **Step 2: Write the failing client tests**

`packages/kbforge-mcp/tests/test_client.py`:

```python
from __future__ import annotations

import pytest

from kbforge_mcp.client import McpClient, ToolCallFailed, ToolNotAllowed
from tests.fake_server import mcp as fixture_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _client(allowed: set[str]) -> McpClient:
    return McpClient(server=fixture_server, allowed=frozenset(allowed))


async def test_a_configured_tool_is_callable():
    async with await _client({"search_docs", "read_doc"}) as c:
        result = await c.call("read_doc", {"path": "docs/onboarding.md"})
        assert "Onboarding" in result.content[0].text


async def test_a_tool_outside_the_two_configured_names_is_never_called():
    # The callable set is structural: there is no config key that could widen it.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="delete_doc"):
            await c.call("delete_doc", {"path": "docs/onboarding.md"})
    assert "docs/onboarding.md" in __import__(
        "tests.fake_server", fromlist=["DOCS"]
    ).DOCS


async def test_a_tool_declaring_read_only_hint_false_is_refused_even_if_configured():
    async with await _client({"search_docs", "delete_doc"}) as c:
        with pytest.raises(ToolNotAllowed, match="declares itself mutating"):
            await c.call("delete_doc", {"path": "docs/onboarding.md"})


async def test_an_unset_read_only_hint_is_permitted():
    # Spec default is false, SDK sentinel for "not declared" is None. Refusing on
    # `not read_only_hint` would conflate them and reject every server that never
    # set the annotation -- which is most of them, including both live targets.
    async with await _client({"search_docs", "read_doc"}) as c:
        c._read_only[  # noqa: SLF001 - deliberately simulating an unannotated server
            "read_doc"
        ] = None
        assert await c.call("read_doc", {"path": "docs/retention.md"})


async def test_is_error_becomes_an_exception_and_never_document_content():
    # An errored call still returns content -- populated WITH THE ERROR MESSAGE.
    # Mapping it would ship the error text as a concept body.
    async with await _client({"search_docs", "read_doc"}) as c:
        with pytest.raises(ToolCallFailed, match="no such document"):
            await c.call("read_doc", {"path": "docs/missing.md"})
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest packages/kbforge-mcp/tests/test_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge_mcp.client'`

You will also need `anyio` for async tests; add `"anyio>=4"` to the root
`[project.optional-dependencies].dev` and re-sync.

- [ ] **Step 4: Implement `client.py`**

```python
"""The MCP session, and the two guards that stand between a sync run and a write.

Read-only is enforced structurally: the callable set IS the two configured tool
names, so there is no allowlist to misconfigure and no discovery loop that could
widen it. `read_only_hint` is defence in depth on top -- the SDK is explicit that
annotations are hints and "should never" drive tool decisions for untrusted
servers, so it is a guard against honest misconfiguration, never a boundary.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult

from kbforge_mcp.config import HttpTransport, McpSourceConfig


# Set by tests to point `open_session` at an in-process fixture server. It lives
# here, not in connector.py: connector imports client, so the reverse import
# would cycle. Never set in production.
_server_override = None


class ToolNotAllowed(RuntimeError):
    """A tool outside the configured pair, or one declaring itself mutating."""


class ToolCallFailed(RuntimeError):
    """The server reported the call as an error (`is_error`)."""


class McpClient:
    def __init__(self, *, server, allowed=frozenset()):
        # `server` is anything mcp.Client accepts: an in-process server object or
        # a Transport. There is no `url` parameter, because a URL with auth has to
        # become a Transport first (see `_http_transport`).
        self._target = server
        self._allowed = allowed
        self._client: Client | None = None
        self._read_only: dict[str, bool | None] = {}

    async def __aenter__(self) -> McpClient:
        self._client = await Client(self._target).__aenter__()
        listed = await self._client.list_tools()
        self._read_only = {
            t.name: getattr(getattr(t, "annotations", None), "read_only_hint", None)
            for t in listed.tools
        }
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def call(self, name: str, args: dict) -> CallToolResult:
        if name not in self._allowed:
            raise ToolNotAllowed(
                f"tool {name!r} is not one of the configured tools "
                f"{sorted(self._allowed)}; the callable set is the config"
            )
        if self._read_only.get(name) is False:
            raise ToolNotAllowed(
                f"tool {name!r} declares itself mutating (read_only_hint=false); "
                f"refusing to call it from a source connector"
            )
        assert self._client is not None, "McpClient used outside its context manager"
        result = await self._client.call_tool(name, args)
        if result.is_error:
            text = " ".join(
                b.text for b in result.content if getattr(b, "type", "") == "text"
            )
            raise ToolCallFailed(f"{name} failed: {text}")
        return result


@asynccontextmanager
async def _http_transport(url: str, headers: dict[str, str]):
    """`Client` has no `headers` parameter, so a bearer token rides on the httpx
    client the streamable-HTTP transport is built from. `Transport` is a Protocol
    -- an async context manager yielding TransportStreams -- so this qualifies.

    This is used for EVERY http source, authenticated or not. `StreamableHTTPTransport`
    looks like the natural no-auth shortcut and is not one: it does not implement the
    async context manager protocol, so `Client(StreamableHTTPTransport(url))` raises
    `TypeError: ... does not support the asynchronous context manager protocol`.
    Verified against a live public server."""
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            yield streams


@asynccontextmanager
async def open_session(cfg: McpSourceConfig):
    """One session per fetch: select and every read share it."""
    if _server_override is not None:
        async with McpClient(server=_server_override, allowed=cfg.tool_names) as c:
            yield c
        return
    if isinstance(cfg.transport, HttpTransport):
        headers = {}
        if cfg.transport.auth_env:
            token = os.environ.get(cfg.transport.auth_env)
            if not token:
                raise RuntimeError(
                    f"environment variable {cfg.transport.auth_env} is not set"
                )
            headers["Authorization"] = f"Bearer {token}"
        # One shape for authenticated and unauthenticated alike -- see the
        # docstring on `_http_transport` for why there is no no-auth shortcut.
        client = McpClient(
            server=_http_transport(cfg.transport.url, headers), allowed=cfg.tool_names
        )
    else:
        params = StdioServerParameters(
            command=cfg.transport.command,
            args=cfg.transport.args,
            env={k: os.environ[k] for k in cfg.transport.env if k in os.environ},
        )
        # stdio_client is an @asynccontextmanager yielding (read, write) streams,
        # which satisfies the Transport protocol.
        client = McpClient(server=stdio_client(params), allowed=cfg.tool_names)
    async with client as c:
        yield c
```

Every SDK name and both transports above were verified by running them:

- `Client(stdio_client(StdioServerParameters(...)))` was driven against a real
  subprocess server — tools listed, a document read, and a raising tool surfaced as
  `is_error=True`. The helper is `stdio_client`, an `@asynccontextmanager`; there is
  no `stdio_transport`.
- `Client(_http_transport(url, headers))` was connected to a live public MCP server
  over HTTP and listed its tools.
- `Client(StreamableHTTPTransport(url))` **fails** with `TypeError: ... does not
  support the asynchronous context manager protocol`. Do not reintroduce it.
- `mcp.Client.__init__` accepts `Server | MCPServer | Transport | str` and **no
  `headers` keyword** — passing one raises `TypeError`.

- [ ] **Step 5: Run to verify the tests pass**

Run: `uv run pytest packages/kbforge-mcp/tests/test_client.py -v`
Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add packages/kbforge-mcp pyproject.toml
git commit -m "feat(mcp): session client with the structural two-tool set"
```

---

## Task 5: Selectors and the four hookimpls

**Files:**
- Create: `packages/kbforge-mcp/src/kbforge_mcp/selectors.py`
- Create: `packages/kbforge-mcp/src/kbforge_mcp/connector.py`
- Create: `packages/kbforge-mcp/tests/test_connector.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  - `kbforge_mcp.selectors.select_refs(client, cfg) -> tuple[list[DocRef], bool]`
    — the bool is `complete`
  - `kbforge_mcp.connector.McpConnector` with the four hookimpls
  - `kbforge_mcp.connector.CONNECTOR` — the module-level instance the entry point
    resolves to

- [ ] **Step 1: Write the failing connector tests**

`packages/kbforge-mcp/tests/test_connector.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kbforge.canonical import assert_fetch_contract, assert_stability

from kbforge_mcp.connector import CONNECTOR
from tests.fake_server import mcp as fixture_server

CONFIG = {
    "system": "fixture",
    "transport": {"kind": "stdio", "command": "unused"},
    "select": {
        "tool": "search_docs",
        "args": {"query": "*"},
        "ids": {"list": "results", "id": "path", "title": "title"},
    },
    "read": {"tool": "read_doc", "id_arg": "path"},
}


@pytest.fixture
def cfg(monkeypatch):
    """Point the connector at the in-process fixture server."""
    from kbforge_mcp import client as mod

    monkeypatch.setattr(mod, "_server_override", fixture_server)
    return dict(CONFIG)


def test_fetch_then_normalize_produces_citable_documents(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == [
        "fixture:docs/onboarding",
        "fixture:docs/retention",
    ]
    assert "How to get started" in docs[0].text
    assert docs[0].anchor.system == "fixture"
    assert_fetch_contract(docs, complete=result.complete)


def test_normalize_is_stable_and_clock_free(cfg, monkeypatch):
    # assert_stability CANNOT catch a clock in normalize: content_hash excludes
    # the anchor by design, so both passes hash identically. Compare the anchors.
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)

    first = CONNECTOR.kbforge_normalize(result.records)
    import kbforge_mcp.connector as mod

    class FrozenElsewhere:
        # normalize must not call now(); it MUST still call fromisoformat, so the
        # shim delegates that one. A shim without it fails on AttributeError and
        # would pass for the wrong reason.
        @staticmethod
        def now(tz=None):
            raise AssertionError("normalize called the clock (architecture 4.3)")

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(mod, "datetime", FrozenElsewhere)
    second = CONNECTOR.kbforge_normalize(result.records)
    assert [d.anchor.retrieved_at for d in first] == [
        d.anchor.retrieved_at for d in second
    ]


def test_retrieved_at_is_stamped_in_fetch_not_normalize(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert all("retrieved_at" in r.anchor_hint for r in result.records)


def test_a_failed_read_degrades_complete_rather_than_dropping_silently(cfg):
    # Skipping a document while still claiming complete=True would, once the
    # 0.8.0 manifest lands, manufacture a deletion out of a transient error.
    # `docs/missing.md` makes the fixture's read_doc raise -> ToolCallFailed.
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/missing.md"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]
    # static_ids is complete by construction -- the failed read is what
    # downgrades it, and that downgrade is the whole point.
    assert result.complete is False


def test_a_prose_only_selector_fails_closed(cfg):
    cfg["select"] = {"tool": "outline", "args": {"query": "*"}}
    with pytest.raises(Exception, match="static_ids"):
        CONNECTOR.kbforge_fetch(cfg, None)


def test_a_query_selector_never_reports_complete(cfg):
    # This is what makes an empty select result safe: `refs_from_select` may
    # legally return [], and zero documents with complete=True would manufacture
    # a corpus-wide deletion once the 0.8.0 manifest lands. A query selector saw
    # only what the server chose to return, so it can never claim completeness --
    # and `assert_fetch_contract` refuses a tombstone under complete=False.
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert result.complete is False


def test_static_ids_need_no_select_call(cfg):
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]


def test_slug_collision_is_caught_by_the_0_6_0_fetch_side_law(cfg):
    # Stripping the extension turns `policy` and `policy.md` into one slug, which
    # makes them one doc_id -- converting a silent concept_path collapse into a
    # loud FetchContractError. The law already catches this; prove it does.
    from kbforge.canonical import FetchContractError

    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/retention"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    with pytest.raises(FetchContractError, match="duplicate doc_id"):
        assert_fetch_contract(docs, complete=result.complete)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest packages/kbforge-mcp/tests/test_connector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge_mcp.connector'`

- [ ] **Step 3: Implement `selectors.py`**

```python
"""Which documents to read. The reader is fixed; only this half varies."""

from __future__ import annotations

from kbforge_mcp.client import McpClient
from kbforge_mcp.config import McpSourceConfig
from kbforge_mcp.mapping import DocRef, refs_from_select
from kbforge_mcp.slug import SlugError, native_id_for


async def select_refs(client: McpClient, cfg: McpSourceConfig) -> tuple[list[DocRef], bool]:
    """Return the selected refs and whether the selection was complete.

    `static_ids` is complete by construction -- the configured list IS the scope,
    which is what would later license tombstones. A query selector never is: it
    saw whatever the server chose to return.
    """
    if cfg.static_ids is not None:
        refs = []
        for raw in cfg.static_ids:
            try:
                refs.append(
                    DocRef(
                        raw_id=raw,
                        native_id=native_id_for(raw),
                        url=raw if "://" in raw else None,
                        title=None,
                    )
                )
            except SlugError as exc:
                raise RuntimeError(f"config 'static_ids' entry unusable: {exc}") from exc
        return refs, True

    assert cfg.select is not None, "config validation guarantees one selector"
    result = await client.call(cfg.select.tool, cfg.select.args)
    return refs_from_select(result, cfg.select.ids), False
```

- [ ] **Step 4: Implement `connector.py`**

```python
"""The kbforge connector: four hookimpls, one asyncio.run, one session.

`kbforge_fetch` may use a clock; `kbforge_normalize` may not (architecture §4.3).
`retrieved_at` is therefore stamped here, into `anchor_hint`, and normalize only
reads it back. `assert_stability` cannot catch a violation of that -- content_hash
excludes the anchor by design -- so the guard is a test, not a convention.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from kbforge.canonical import content_hash
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)

from kbforge_mcp.client import ToolCallFailed, open_session
from kbforge_mcp.config import McpSourceConfig, problems_for
from kbforge_mcp.mapping import MappingError, records_from_read
from kbforge_mcp.selectors import select_refs

_NAME = "mcp"

async def _fetch(cfg: McpSourceConfig) -> tuple[list[RawRecord], bool]:
    async with open_session(cfg) as client:
        refs, complete = await select_refs(client, cfg)
        records: list[RawRecord] = []
        stamped = datetime.now(tz=UTC).isoformat()
        # `system` reaches normalize ONLY through anchor_hint: normalize receives
        # records, never config.
        for ref in refs:
            args = {cfg.read.id_arg: ref.raw_id, **cfg.read.static_args}
            try:
                result = await client.call(cfg.read.tool, args)
                got = records_from_read(result, ref, cfg.read, cfg.media_type)
            except (ToolCallFailed, MappingError):
                # A per-document failure degrades the run; it never silently
                # drops a document while still claiming complete coverage.
                complete = False
                continue
            for rec in got:
                rec.anchor_hint["retrieved_at"] = stamped
                rec.anchor_hint["system"] = cfg.system
            records.extend(got)
        return records, complete


class McpConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_NAME,
            version="0.1.0",
            source_system="any MCP server with a select tool and a read-by-id tool",
            info_types=["document"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        return problems_for(config)

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        # cursor is unused in 0.7.0: like local_files, this re-selects every run
        # and lets the mirror diff do the work. The manifest lands in 0.8.0.
        cfg = McpSourceConfig.model_validate(config)
        records, complete = asyncio.run(_fetch(cfg))
        return FetchResult(
            records=records,
            cursor=Cursor(connector=_NAME),
            complete=complete,
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            hint = rec.anchor_hint
            native_id = hint["native_id"]
            system = hint["system"]
            text = rec.payload.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
            anchor = ResourceAnchor(
                system=system,
                native_id=native_id,
                url=hint.get("url"),
                retrieved_at=datetime.fromisoformat(hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=f"{system}:{native_id}",
                title=str(hint.get("title") or native_id.rsplit("/", 1)[-1]),
                text=text.strip(),
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs


CONNECTOR = McpConnector()
```

`_server_override` is honoured inside `open_session` so the fixture server goes
through the same code path the real transports use. A parallel path would be a
path the tests do not test.

- [ ] **Step 5: Run the connector tests and iterate until green**

Run: `uv run pytest packages/kbforge-mcp/tests/ -v`
Expected: PASS. The fixture-server wiring in the test's `cfg` fixture is the
fiddly part — `open_session` must honour `connector._server_override` when it is
set. Add that branch to `open_session` rather than to the test.

- [ ] **Step 6: Run the whole suite**

Run: `uv run pytest`
Expected: all pre-existing tests plus the new package's, no warnings.

- [ ] **Step 7: Commit**

```bash
git add packages/kbforge-mcp
git commit -m "feat(mcp): selectors and the four connector hookimpls"
```

---

## Task 6: Registration and the stdio transport

**Files:**
- Create: `packages/kbforge-mcp/tests/test_stdio.py`

**Interfaces:**
- Consumes: `CONNECTOR` (Task 5), `fake_server.mcp` (Task 4).

- [ ] **Step 1: Write the registration and stdio tests**

```python
from __future__ import annotations

import subprocess
import sys

from kbforge.registry import build_registry


def test_the_connector_is_discovered_through_the_entry_point():
    # No edit to registry.py: an installed distribution advertising
    # kbforge.connectors is discovered by load_setuptools_entrypoints.
    names = [i.name for i in build_registry().hook.kbforge_connector_info()]
    assert "mcp" in names


def test_kbforge_list_shows_the_connector():
    out = subprocess.run(
        [sys.executable, "-m", "kbforge", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "mcp" in out


def test_a_real_stdio_subprocess_round_trips(tmp_path):
    # The in-process Client skips transport framing entirely. Launch the fixture
    # server as a subprocess so the stdio branch is actually exercised.
    server = tmp_path / "server.py"
    server.write_text(
        "from tests.fake_server import mcp\n"
        "if __name__ == '__main__':\n    mcp.run()\n"
    )
    from kbforge_mcp.connector import CONNECTOR

    cfg = {
        "system": "fixture",
        "transport": {
            "kind": "stdio",
            "command": sys.executable,
            "args": [str(server)],
        },
        "static_ids": ["docs/retention.md"],
        "read": {"tool": "read_doc", "id_arg": "path"},
    }
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]
```

`MCPServer.run()` is verified to exist (alongside `run_stdio_async()`), as is the
`@mcp.tool(annotations=ToolAnnotations(...))` decorator the fixture uses.

- [ ] **Step 2: Run and iterate**

Run: `uv run pytest packages/kbforge-mcp/tests/test_stdio.py -v`
Expected: PASS, 3 passed. The subprocess needs the repo on `PYTHONPATH` to import
`tests.fake_server`; set it in the `subprocess`/transport `env` rather than
copying the fixture into `tmp_path`.

- [ ] **Step 3: Commit**

```bash
git add packages/kbforge-mcp
git commit -m "test(mcp): entry-point discovery and a real stdio round trip"
```

---

## Task 7: Live tests against AWS documentation and GitHub

**Files:**
- Create: `packages/kbforge-mcp/tests/test_live.py`
- Modify: `tests/conftest.py:12-19` (extend the `--run-live` help text only)

**Interfaces:**
- Consumes: `CONNECTOR` (Task 5).

- [ ] **Step 1: Write the live tests**

```python
"""Live tests. Skipped unless --run-live.

AWS Documentation needs network but NO credentials, so it is the one live test in
this repo that can run unattended. GitHub needs GITHUB_TOKEN.
"""

from __future__ import annotations

import os

import pytest
from kbforge.canonical import assert_fetch_contract

from kbforge_mcp.connector import CONNECTOR

pytestmark = pytest.mark.live

AWS = {
    "system": "aws_docs",
    "transport": {
        "kind": "stdio",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
        "env": ["AWS_DOCUMENTATION_PARTITION"],
    },
    "select": {
        "tool": "search_documentation",
        "args": {"search_phrase": "S3 bucket naming rules", "limit": 3},
        "ids": {"list": "results", "id": "url", "title": "title"},
    },
    "read": {"tool": "read_documentation", "id_arg": "url"},
}


def test_aws_docs_select_then_read_yields_citable_documents():
    result = CONNECTOR.kbforge_fetch(AWS, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert docs, "expected at least one document"
    assert_fetch_contract(docs, complete=result.complete)
    for d in docs:
        # The slug is path-safe; the full URL survives as provenance.
        assert "://" not in d.anchor.native_id
        assert d.anchor.url and d.anchor.url.startswith("https://")
        assert d.text.strip()


def test_aws_docs_two_runs_agree_on_content_hashes():
    # The no-op rule depends on this: an unchanged source must hash identically.
    first = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(AWS, None).records)
    second = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(AWS, None).records)
    assert [d.anchor.content_hash for d in first] == [
        d.anchor.content_hash for d in second
    ]


@pytest.mark.skipif(not os.environ.get("GITHUB_TOKEN"), reason="GITHUB_TOKEN not set")
def test_github_readonly_endpoint_yields_verbatim_files():
    cfg = {
        "system": "gh_docs",
        "transport": {
            "kind": "http",
            "url": "https://api.githubcopilot.com/mcp/x/repos/readonly",
            "auth_env": "GITHUB_TOKEN",
        },
        "select": {
            "tool": "search_code",
            "args": {"query": "repo:modelcontextprotocol/servers filename:SECURITY.md"},
            "ids": {"list": "items", "id": "path"},
        },
        "read": {
            "tool": "get_file_contents",
            "id_arg": "path",
            "static_args": {"owner": "modelcontextprotocol", "repo": "servers"},
        },
    }
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert docs
    assert_fetch_contract(docs, complete=result.complete)
    assert "Security Policy" in docs[0].text
```

- [ ] **Step 2: Run them**

Run: `uv run pytest packages/kbforge-mcp/tests/test_live.py --run-live -v`
Expected: the two AWS tests pass; the GitHub test passes if `GITHUB_TOKEN` is set.

Check one shape question against real GitHub output before trusting the result:
`get_file_contents` appears to return a prose preamble text block alongside the
resource. If the resource block carries no text/blob, tier 1 yields nothing and the
mapping falls through to tier 3, which would capture the preamble
(`"successfully downloaded text file (SHA: ...)"`) as the document body. Assert on
real content (`"Security Policy" in docs[0].text`) so that failure cannot pass, and
report what the response actually contained.

If the AWS response shape differs from the config above, **fix the config, not the
mapping** — that is the config-only promise being tested. If the mapping genuinely
cannot express it, that is a finding worth reporting.

- [ ] **Step 3: Confirm they are skipped by default**

Run: `uv run pytest packages/kbforge-mcp/tests/test_live.py -v`
Expected: 3 skipped.

- [ ] **Step 4: Commit**

```bash
git add packages/kbforge-mcp tests/conftest.py
git commit -m "test(mcp): live coverage against AWS documentation and GitHub"
```

---

## Task 8: Mutation tests and documentation

**Files:**
- Create: `packages/kbforge-mcp/README.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/architecture.md:253-258` and `docs/architecture.md:280`
- Modify: `docs/design/2026-08-16-mcp-source-connector-design.md`

**Interfaces:** none — this task ships no new code.

- [ ] **Step 1: Verify each gate by breaking it**

CLAUDE.md: a test over a gate is worth what it catches. For each row, make the
edit **in place**, run the named test, confirm it fails **with the expected
message**, then restore with `git checkout -- <path>`.

Never copy the repo to mutate it: a `cp -R` keeps a `.venv` that resolves
`kbforge_mcp` to the original source, so nothing is actually mutated and
everything passes.

| Break | File | Test that must fail | Expected message |
|---|---|---|---|
| Delete the `name not in self._allowed` check | `client.py` | `test_a_tool_outside_the_two_configured_names_is_never_called` | `not one of the configured tools` |
| Change `is False` to `not` in the hint check | `client.py` | `test_an_unset_read_only_hint_is_permitted` | refuses an unannotated tool |
| Delete the `if result.is_error` branch | `client.py` | `test_is_error_becomes_an_exception_and_never_document_content` | error text arrives as content |
| Change `complete = False` to `pass` in the read loop | `connector.py` | `test_a_failed_read_degrades_complete_rather_than_dropping_silently` | `assert result.complete is False` |
| Return `raw` instead of the slug from `native_id_for` | `slug.py` | `test_url_reduces_to_its_path_without_scheme_host_or_extension` | full URL in `native_id` |
| Make tier-3 select return `[]` instead of raising | `mapping.py` | `test_tier3_select_fails_closed_with_a_message_naming_the_remedy` | no `MappingError` raised |

Record the actual output of each in your report. A row you cannot make fail is a
finding: the gate is not doing what its test claims.

- [ ] **Step 2: Write `packages/kbforge-mcp/README.md`**

Cover: what it is (one paragraph), the AWS config from Task 7 as the worked
example, the two-tool rule stated as a guarantee, and the pointer to the design
note. Do not restate the mapping tiers — the design note owns them, and a README
that restates them is drift.

- [ ] **Step 3: Add the CHANGELOG entry**

Follow the existing format exactly, including **a blank line after every `###`
header** — every prior entry has one.

```markdown
## [0.7.0] - 2026-08-18   <!-- set to the day it actually ships -->

### Added

- `kbforge-mcp`, a separate distribution that turns any MCP server with a select
  tool and a read-by-id tool into a kbforge source through configuration.
  Response mapping is protocol-first: MCP's own content-block types are the
  vocabulary, so the common case needs no config at all.
- Read-only is structural — the callable tool set *is* the two configured tool
  names — with a `read_only_hint` refusal as defence in depth.

### Changed

- `pyproject.toml` declares a uv workspace; `testpaths` now covers
  `packages/kbforge-mcp/tests`.
```

- [ ] **Step 4: Apply the `architecture.md` amendments from design note §12**

Replace the "Future convenience (not core)" note at `docs/architecture.md:253-258`
with the shipped package. State the selector/reader split as the general form of
retriever-not-extractor, and record that read-only is enforced as a **structural
two-tool set**, not a config allowlist, because side-effect-freedom is not
introspectable.

At `docs/architecture.md:280`, **edit** the existing sentence rather than adding
one: if the manifest is keyed on `native_id` rather than `doc_id`, say why
(`native_id` is the fetch-side identity; `doc_id` only exists post-normalize).

- [ ] **Step 5: Fold the shipped parts of the design note into `architecture.md`**

CLAUDE.md: `docs/design/` holds specs for **unbuilt** work only. Sections §1-§8 are
now built. Move what the code does not already say into `architecture.md` and
reduce the design note to §9-§12 — the deferred manifest, cursor, and collision
work — keeping the rationale, not the mechanics.

- [ ] **Step 6: Run everything and commit**

```bash
uv run pytest
git add -A
git commit -m "docs(mcp): fold the shipped design into architecture.md and verify the gates"
```

---

## Self-Review

**Spec coverage.** §2.3 asymmetry → Task 3. §2.4 two-tool set + hint asymmetry →
Task 4, verified in Task 8. §5.1 tiers → Task 3. §5.2 config incl. `id_arg` and
`static_args` → Tasks 2 and 7. §5.3 slug → Task 1. §6 discriminator and one
`asyncio.run` → Tasks 2 and 5. §7 `is_error` and the complete-downgrade → Tasks 4
and 5. §8.1 targets → Task 7. §8.2 three layers → Tasks 4, 6, 7. §8.3 mutation
tests → Task 8. §10.3 clock guard → Task 5. §12 amendments → Task 8.

**Two spec items deliberately not implemented**, because 0.7.0 does not need them
(§9, §11): the cursor manifest and tombstones. `kbforge_fetch` returns
`Cursor(connector="mcp")` with an empty payload, forward-compatible with either
§10.1 resolution.

**Known gaps, stated rather than hidden.**

1. Three SDK symbols could not be verified without the package installed: the
   content-block class names (Task 3 Step 1), the stdio transport constructor
   (Task 4 Step 4), and the server's stdio run entry point (Task 6 Step 1). Each
   has an explicit verification step and instruction to prefer the real name.
2. `ids.list` is a single top-level key, not a dotted path. Both live targets need
   only that. The first nested response will force the question; widening later is
   cheaper than shipping a path language now.
3. Task 5's fixture wiring (`_server_override`) is the one piece of test-only
   surface in production code. It is the price of driving an in-process server
   through the same `open_session` the real transports use; the alternative is a
   parallel code path that the tests would then not be testing.
