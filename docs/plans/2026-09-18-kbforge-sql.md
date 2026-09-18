# kbforge-sql Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `kbforge-sql`, a workspace distribution that makes any database SQLAlchemy can reach a kbforge source through configuration alone.

**Architecture:** One scoped `SELECT` per source. `fetch` runs it in a rolled-back transaction, canonicalizes every value, groups rows into entities, and emits one JSON `RawRecord` per entity plus a tombstone record per id that vanished since the last published run (a manifest kept in the cursor). `normalize` is pure: it renders each payload into fixed-format markdown and builds the `CanonicalDocument`. No core (`src/kbforge/`) change.

**Tech Stack:** Python ≥3.12, SQLAlchemy ≥2 (Core only, `exec_driver_sql`), pydantic ≥2, pytest with SQLite as the offline engine, uv workspace, ruff + ty via prek.

**Spec:** `docs/design/2026-09-18-sql-source-connector-design.md` — read it before Task 1; every behaviour below argues from it.

## Global Constraints

- `src/kbforge/` is not modified. If a task seems to need a core change, stop and report it.
- `kbforge-sql` depends on `kbforge>=0.8.0`, `pydantic>=2.0`, `sqlalchemy>=2.0` and **no database driver**.
- Version `0.1.0`; kbforge's own version (`pyproject.toml` root, `0.9.0`) does not move.
- Connector entry-point name `sql`; `Cursor.connector` is always `"sql"` (spec §6.2).
- `normalize` never calls a clock, the network, or randomness. Only `fetch` stamps `retrieved_at`.
- No commit is ever issued on a database connection; the query runs via `exec_driver_sql` and the connection is rolled back.
- Credentials never appear in config values, CLI arguments, or error messages. Env var *names* only; errors never echo an env var's value.
- Every `SqlSourceError` that leaves `kbforge_fetch` starts with `sql source '<system>': `.
- Tests never touch the network. Live tests carry `pytestmark = pytest.mark.live`.
- Test files in `packages/kbforge-sql/tests/` are named `test_sql_*.py`, and the directory has **no** `__init__.py` and **no** `conftest.py`: `okfquery`'s tests use the same no-`__init__` layout, where module basenames must be unique across the whole run, and a second rootless `conftest.py` would shadow the root one.
- Commit before any mutation check, then mutate in place and restore with `git checkout --` (CLAUDE.md, "Verifying a gate").
- Run `uv run ruff check --fix && uv run ruff format` before each commit; the prek hook runs ruff and ty. Ruff selects `E501` (88 columns) and the formatter does not split strings, so wrap any long test string or parametrize row by hand — code in this plan is not pre-wrapped.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/kbforge-sql/pyproject.toml` | distribution metadata, entry point |
| `packages/kbforge-sql/README.md` | operator docs |
| `packages/kbforge-sql/src/kbforge_sql/__init__.py` | package marker, `__version__` |
| `…/errors.py` | `SqlSourceError` |
| `…/config.py` | `SqlSourceConfig`, `GroupSpec`, `problems_for`, `check_columns` |
| `…/values.py` | `canonical(value, column)` |
| `…/identity.py` | `native_id_for(values, columns)` |
| `…/render.py` | `render_text(lead, attributes, group)` |
| `…/connector.py` | the four hookimpls, query execution, retry, entities, manifest |
| `packages/kbforge-sql/tests/sql_testdb.py` | SQLite fixture helpers shared by tests |
| `packages/kbforge-sql/tests/test_sql_*.py` | tests per module |
| root `pyproject.toml`, `conftest.py`, `.github/workflows/ci.yml` | workspace wiring |
| `README.md`, `CHANGELOG.md`, `docs/architecture.md`, the design note | docs (Task 7) |

---

### Task 1: Package scaffold and config validation

**Files:**
- Create: `packages/kbforge-sql/pyproject.toml`, `packages/kbforge-sql/README.md`, `packages/kbforge-sql/src/kbforge_sql/__init__.py`, `packages/kbforge-sql/src/kbforge_sql/errors.py`, `packages/kbforge-sql/src/kbforge_sql/config.py`
- Modify: root `pyproject.toml` (testpaths, ruff `src`, dependency group, uv sources), `.github/workflows/ci.yml` (coverage), `uv.lock`
- Test: `packages/kbforge-sql/tests/test_sql_config.py`

**Interfaces:**
- Produces: `SqlSourceError(RuntimeError)`; `GroupSpec(children: list[str], order_by: list[str], heading: str | None)`; `SqlSourceConfig` with fields `system, url_env, password_env, query, id, title, text, facets, exclude, type, url_template, group, retries, max_removed_fraction` and method `configured_columns() -> list[str]`; `problems_for(config: dict) -> list[str]`; `check_columns(cfg: SqlSourceConfig, columns: Sequence[str]) -> None` (raises `SqlSourceError`).

**Do not add the entry point in this task.** kbforge discovers connectors eagerly; an entry point naming a `connector` module that does not exist yet breaks every `kbforge` command, not just `list`. Task 4 adds it.

- [ ] **Step 1: Create the distribution skeleton**

`packages/kbforge-sql/pyproject.toml`:

```toml
[project]
name = "kbforge-sql"
version = "0.1.0"
description = "SQL source connector for kbforge"
readme = "README.md"
requires-python = ">=3.12"
authors = [{ name = "Qing", email = "qingye779@gmail.com" }]
license = { text = "MIT" }
dependencies = [
    # 0.8.0: cursor slots keyed per config instance, so two `sql` sources never
    # share a manifest; `synthesize.OKF_OWNED` and `canonical.is_blank` exist.
    "kbforge>=0.8.0",
    "pydantic>=2.0",
    "sqlalchemy>=2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/kbforge_sql"]
```

`packages/kbforge-sql/README.md` (placeholder until Task 7 writes it):

```markdown
# kbforge-sql

A relational database as a [kbforge](https://github.com/flyersworder/kbforge) source.
```

`packages/kbforge-sql/src/kbforge_sql/__init__.py`:

```python
"""A relational database as a kbforge source, through configuration alone."""

__version__ = "0.1.0"
```

`packages/kbforge-sql/src/kbforge_sql/errors.py`:

```python
"""The one exception kbforge-sql raises."""


class SqlSourceError(RuntimeError):
    """A SQL source failed in a way an operator must fix or retry.

    Raised unprefixed by the helpers; `kbforge_fetch` re-raises it once with
    `sql source '<system>': ` in front, so every message names its source
    exactly once."""
```

- [ ] **Step 2: Wire the workspace**

In the root `pyproject.toml`:

```toml
[dependency-groups]
dev = ["kbforge-mcp", "kbforge-okfquery", "kbforge-sql"]
```

```toml
[tool.pytest.ini_options]
testpaths = [
    "tests",
    "packages/kbforge-mcp/tests",
    "packages/okfquery/tests",
    "packages/kbforge-sql/tests",
]
```

```toml
[tool.ruff]
# All four workspace packages are first-party, so their imports group together
# and `kbforge_mcp`/`okfquery`/`kbforge_sql` are not sorted in with third-party
# dependencies.
src = [
    "src",
    "packages/kbforge-mcp/src",
    "packages/okfquery/src",
    "packages/kbforge-sql/src",
]
```

```toml
[tool.uv.sources]
kbforge = { workspace = true }
kbforge-mcp = { workspace = true }
kbforge-okfquery = { workspace = true }
kbforge-sql = { workspace = true }
```

In `.github/workflows/ci.yml`, change the test step and its comment:

```yaml
      # All four workspace packages: `--cov=kbforge` alone left the companions
      # measured by nothing.
      - run: uv run pytest --cov=kbforge --cov=kbforge_mcp --cov=okfquery --cov=kbforge_sql --cov-report=term-missing
```

Run: `uv lock && uv sync --all-extras --dev`
Expected: `kbforge-sql` and `sqlalchemy` appear in the sync output; `uv run python -c "import kbforge_sql, sqlalchemy"` exits 0.

- [ ] **Step 3: Write the failing config tests**

`packages/kbforge-sql/tests/test_sql_config.py`:

```python
import pytest

from kbforge_sql.config import SqlSourceConfig, check_columns, problems_for
from kbforge_sql.errors import SqlSourceError


def _cfg(**over):
    cfg = {
        "system": "products",
        "url_env": "KB_SQL_URL",
        "query": "SELECT 1",
        "id": ["product_id"],
        "title": "product_name",
    }
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KB_SQL_URL", "sqlite://")
    monkeypatch.setenv("KB_SQL_PASSWORD", "pw")


def test_a_minimal_config_is_valid():
    assert problems_for(_cfg()) == []


def test_an_unknown_key_is_an_error_not_ignored():
    assert any("qurey" in p for p in problems_for(_cfg(qurey="SELECT 2")))


@pytest.mark.parametrize("system", ["", "a:b", "a/b", "-x", "a b"])
def test_system_must_be_a_short_identifier(system):
    assert any("config 'system'" in p for p in problems_for(_cfg(system=system)))


def test_an_env_name_holding_a_value_is_rejected_without_echoing_it():
    problems = problems_for(_cfg(url_env="postgresql://u:s3cret@h/db"))
    assert any("config 'url_env' must name an environment variable" in p for p in problems)
    assert not any("s3cret" in p for p in problems)


def test_unset_env_vars_are_reported(monkeypatch):
    monkeypatch.delenv("KB_SQL_URL")
    problems = problems_for(_cfg(password_env="MISSING_PW"))
    assert "config 'url_env': environment variable KB_SQL_URL is not set" in problems
    assert "config 'password_env': environment variable MISSING_PW is not set" in problems


def test_a_set_password_env_is_accepted():
    assert problems_for(_cfg(password_env="KB_SQL_PASSWORD")) == []


def test_a_blank_query_is_rejected():
    assert "config 'query' is blank" in problems_for(_cfg(query="  \n"))


def test_a_blank_type_is_rejected():
    assert any("config 'type' is blank" in p for p in problems_for(_cfg(type=" ")))


def test_an_empty_id_list_is_rejected():
    assert any(p.startswith("config id:") for p in problems_for(_cfg(id=[])))


def test_used_columns_must_not_be_excluded():
    problems = problems_for(_cfg(facets=["family"], exclude=["family", "product_id"]))
    assert (
        "config 'exclude' names column(s) the source also uses: "
        "['family', 'product_id']"
    ) in problems


def test_a_facet_named_like_an_okf_key_is_rejected():
    problems = problems_for(_cfg(facets=["type", "status"]))
    assert any(
        p.startswith("config 'facets' must not use OKF-owned key(s) ['type']")
        for p in problems
    )


def test_group_children_must_not_include_id_columns():
    problems = problems_for(_cfg(group={"children": ["product_id", "x"]}))
    assert "config 'group.children' must not include id column(s) ['product_id']" in problems


def test_group_children_must_not_include_entity_columns():
    problems = problems_for(
        _cfg(text="description", facets=["family"], group={"children": ["family", "x"]})
    )
    assert any(
        p.startswith(
            "config 'group.children' must not include title, text or facet column(s) "
            "['family']"
        )
        for p in problems
    )


def test_group_order_by_must_be_a_subset_of_children():
    problems = problems_for(_cfg(group={"children": ["a"], "order_by": ["a", "b"]}))
    assert "config 'group.order_by' must be a subset of 'group.children': ['b']" in problems


def test_url_template_may_only_reference_id_columns():
    problems = problems_for(_cfg(url_template="https://p/{product_id}/{family}"))
    assert "config 'url_template' may only reference id column(s): ['family']" in problems


def test_a_malformed_url_template_is_reported():
    problems = problems_for(_cfg(url_template="https://p/{product_id"))
    assert "config 'url_template' is not a valid format string" in problems


@pytest.mark.parametrize(
    ("key", "value"),
    [("retries", -1), ("max_removed_fraction", 0), ("max_removed_fraction", 1.5)],
)
def test_numeric_bounds(key, value):
    assert any(p.startswith(f"config {key}:") for p in problems_for(_cfg(**{key: value})))


def test_configured_columns_are_deduplicated_in_order():
    cfg = SqlSourceConfig.model_validate(
        _cfg(text="d", facets=["f", "d"], exclude=["x"], group={"children": ["c"], "order_by": ["c"]})
    )
    assert cfg.configured_columns() == ["product_id", "product_name", "d", "f", "x", "c"]


def test_check_columns_lists_what_the_query_returned():
    cfg = SqlSourceConfig.model_validate(_cfg(facets=["famliy"]))
    with pytest.raises(SqlSourceError) as exc:
        check_columns(cfg, ["product_id", "product_name", "family"])
    assert str(exc.value) == (
        "configured column(s) ['famliy'] are not in the query result; "
        "the query returned: ['product_id', 'product_name', 'family']"
    )


def test_check_columns_passes_when_all_present():
    cfg = SqlSourceConfig.model_validate(_cfg())
    check_columns(cfg, ["product_id", "product_name"])
```

- [ ] **Step 4: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_config.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'kbforge_sql.config'`.

- [ ] **Step 5: Implement `config.py`**

`packages/kbforge-sql/src/kbforge_sql/config.py`:

```python
"""Config model for a SQL source, and the offline validation the CLI runs
before any connection (spec §3.1). `check_columns` is the one check that needs
the query result (§3.2)."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kbforge.canonical import is_blank
from kbforge.synthesize import OKF_OWNED
from kbforge_sql.errors import SqlSourceError

# Same shapes as kbforge-mcp's config: an ALL_CAPS env var name, and a `system`
# that is safe inside a doc_id and a `sync/<system>` branch name.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*\Z")
_SYSTEM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class _Strict(BaseModel):
    # A typo in a source config must be an error, not a silently ignored key.
    model_config = ConfigDict(extra="forbid")


class GroupSpec(_Strict):
    children: list[str] = Field(min_length=1)
    order_by: list[str] = Field(default_factory=list)
    heading: str | None = None


class SqlSourceConfig(_Strict):
    system: str
    url_env: str
    password_env: str | None = None
    query: str
    id: list[str] = Field(min_length=1)
    title: str
    text: str | None = None
    facets: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    type: str = "concept"
    url_template: str | None = None
    group: GroupSpec | None = None
    retries: int = Field(default=2, ge=0)
    max_removed_fraction: float = Field(default=0.5, gt=0, le=1)

    def configured_columns(self) -> list[str]:
        """Every column the config names, first mention first, no repeats."""
        cols = [*self.id, self.title]
        if self.text:
            cols.append(self.text)
        cols += [*self.facets, *self.exclude]
        if self.group is not None:
            cols += [*self.group.children, *self.group.order_by]
        return list(dict.fromkeys(cols))


def _template_fields(template: str) -> list[str] | None:
    """Field names in a str.format template, or None if it does not parse."""
    try:
        return [f for _, f, _, _ in Formatter().parse(template) if f is not None]
    except ValueError:
        return None


def problems_for(config: dict) -> list[str]:
    """Human-readable problems; `[]` means the config is usable. No connection."""
    try:
        cfg = SqlSourceConfig.model_validate(config)
    except ValidationError as exc:
        return [
            f"config {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()
        ]

    problems: list[str] = []
    if not _SYSTEM_NAME.match(cfg.system):
        problems.append(
            "config 'system' must be a short identifier -- letters, digits, '_' "
            "and '-', starting with a letter or digit -- because it is part of "
            f"every doc_id and the publish branch: {cfg.system!r}"
        )
    for key, name in (("url_env", cfg.url_env), ("password_env", cfg.password_env)):
        if name is None:
            continue
        if not _ENV_NAME.match(name):
            # The value is NOT echoed: a name that fails this shape is most
            # likely a pasted URL or password.
            problems.append(
                f"config '{key}' must name an environment variable (ALL_CAPS), "
                "not hold its value"
            )
        elif name not in os.environ:
            problems.append(f"config '{key}': environment variable {name} is not set")
    if is_blank(cfg.query):
        problems.append("config 'query' is blank")
    if is_blank(cfg.type):
        problems.append("config 'type' is blank; OKF requires a non-empty type")

    used = {*cfg.id, cfg.title, *cfg.facets}
    if cfg.text:
        used.add(cfg.text)
    if clash := used & set(cfg.exclude):
        problems.append(
            f"config 'exclude' names column(s) the source also uses: {sorted(clash)}"
        )
    if owned := sorted(set(cfg.facets) & OKF_OWNED):
        problems.append(
            f"config 'facets' must not use OKF-owned key(s) {owned}: the emitter "
            "drops them, so the facet would silently never appear"
        )

    if cfg.group is not None:
        children = set(cfg.group.children)
        if ids := sorted(children & set(cfg.id)):
            problems.append(f"config 'group.children' must not include id column(s) {ids}")
        entity = {cfg.title, *cfg.facets} | ({cfg.text} if cfg.text else set())
        if both := sorted(children & entity):
            problems.append(
                "config 'group.children' must not include title, text or facet "
                f"column(s) {both}: they describe the entity, not a child"
            )
        if extra := [c for c in cfg.group.order_by if c not in children]:
            problems.append(
                f"config 'group.order_by' must be a subset of 'group.children': {extra}"
            )

    if cfg.url_template is not None:
        fields = _template_fields(cfg.url_template)
        if fields is None:
            problems.append("config 'url_template' is not a valid format string")
        elif bad := sorted({f for f in fields if f not in cfg.id}):
            problems.append(
                f"config 'url_template' may only reference id column(s): {bad}"
            )
    return problems


def check_columns(cfg: SqlSourceConfig, columns: Sequence[str]) -> None:
    """Spec §3.2: every configured column must be in the result."""
    missing = [c for c in cfg.configured_columns() if c not in columns]
    if missing:
        raise SqlSourceError(
            f"configured column(s) {missing} are not in the query result; "
            f"the query returned: {list(columns)}"
        )
```

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_config.py -q`
Expected: all pass. The numeric-bounds and empty-id tests rely on pydantic's `loc` being the bare field name (`config retries: …`, `config id: …`); if pydantic words it differently, fix the *test's* prefix, not the formatter, which matches `kbforge-mcp`'s.

- [ ] **Step 7: Run the whole suite and commit**

Run: `uv run pytest -q`
Expected: all pass (nothing else changed behaviour).

```bash
git add packages/kbforge-sql pyproject.toml uv.lock .github/workflows/ci.yml
git commit -m "feat(sql): scaffold kbforge-sql with offline config validation"
```

---

### Task 2: Canonical values and identity

**Files:**
- Create: `packages/kbforge-sql/src/kbforge_sql/values.py`, `packages/kbforge-sql/src/kbforge_sql/identity.py`
- Test: `packages/kbforge-sql/tests/test_sql_values.py`, `packages/kbforge-sql/tests/test_sql_identity.py`

**Interfaces:**
- Consumes: `SqlSourceError` (Task 1).
- Produces: `Scalar = str | int | bool | None`; `canonical(value: object, column: str) -> Scalar`; `native_id_for(values: Sequence[Scalar], columns: Sequence[str]) -> str`.

- [ ] **Step 1: Write the failing tests**

`packages/kbforge-sql/tests/test_sql_values.py`:

```python
import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from kbforge_sql.errors import SqlSourceError
from kbforge_sql.values import canonical


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        (Decimal("1.50"), "1.5"),
        (Decimal("1.5"), "1.5"),
        (Decimal("100"), "100"),
        (Decimal("1E+2"), "100"),
        (Decimal("0.00"), "0"),
        (0.1, "0.1"),
        ("  café\r\nline two  \n", "  café\nline two"),
        ("café", "café"),
        (date(2026, 9, 18), "2026-09-18"),
        (datetime(2026, 9, 18, 12, 0), "2026-09-18T12:00:00"),
        (
            datetime(2026, 9, 18, 14, 0, tzinfo=timezone(timedelta(hours=2))),
            "2026-09-18T12:00:00+00:00",
        ),
        (datetime(2026, 9, 18, 12, 0, tzinfo=UTC), "2026-09-18T12:00:00+00:00"),
        (UUID("12345678-1234-5678-1234-567812345678"), "12345678-1234-5678-1234-567812345678"),
    ],
)
def test_canonical_forms(value, expected):
    assert canonical(value, "c") == expected


def test_bool_is_not_collapsed_into_int():
    assert canonical(True, "c") is True


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_numbers_are_rejected_naming_the_column(value):
    with pytest.raises(SqlSourceError, match="column 'price'"):
        canonical(value, "price")


def test_bytes_are_rejected_with_the_remedy():
    with pytest.raises(SqlSourceError) as exc:
        canonical(b"\x00", "blob")
    assert str(exc.value) == (
        "column 'blob' holds a bytes value, which has no canonical text form; "
        "add it to 'exclude' or cast it in the query"
    )
```

`packages/kbforge-sql/tests/test_sql_identity.py`:

```python
import pytest

from kbforge.synthesize import concept_path
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.identity import native_id_for


def test_a_plain_id_is_unchanged():
    assert native_id_for(["IMC300"], ["product_id"]) == "IMC300"


def test_an_int_id_is_stringified():
    assert native_id_for([42], ["id"]) == "42"


def test_composite_ids_join_with_a_slash():
    assert native_id_for(["EV", "IMC300"], ["app", "product"]) == "EV/IMC300"


def test_composites_containing_slashes_stay_distinct():
    a = native_id_for(["a/b", "c"], ["x", "y"])
    b = native_id_for(["a", "b/c"], ["x", "y"])
    assert a != b
    assert a == "a%2Fb/c"


def test_percent_is_escaped_so_the_escape_is_injective():
    assert native_id_for(["a%2Fb"], ["x"]) != native_id_for(["a/b"], ["x"])


def test_windows_unsafe_characters_are_escaped():
    assert native_id_for(['a:b<c>"d|e?f*g\\h'], ["x"]) == "a%3Ab%3Cc%3E%22d%7Ce%3Ff%2Ag%5Ch"


def test_a_trailing_dot_or_space_is_escaped():
    assert native_id_for(["a."], ["x"]) == "a%2E"
    assert native_id_for(["a "], ["x"]) == "a%20"


def test_dot_dot_cannot_escape_the_bundle():
    assert native_id_for([".."], ["x"]) == ".%2E"


def test_a_md_suffix_survives_concept_path():
    a, b = native_id_for(["x.md"], ["k"]), native_id_for(["x"], ["k"])
    assert concept_path(f"s:{a}") != concept_path(f"s:{b}")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_missing_id_value_is_an_error_naming_the_column(value):
    with pytest.raises(SqlSourceError, match="id column 'product_id' is empty"):
        native_id_for([value], ["product_id"])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_values.py packages/kbforge-sql/tests/test_sql_identity.py -q`
Expected: collection errors, `No module named 'kbforge_sql.values'` / `'kbforge_sql.identity'`.

- [ ] **Step 3: Implement `values.py`**

```python
"""Database value -> canonical JSON-safe form (spec §4.3).

Everything the diff hashes passes through here, so this is where §4.3 law 1
(determinism) is won or lost for a SQL source. Unknown types are rejected
rather than str()-ed: a repr can embed memory addresses or driver-specific
formatting, and a silently dropped column is a fact synthesis never sees."""

from __future__ import annotations

import math
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from kbforge_sql.errors import SqlSourceError

Scalar = str | int | bool | None


def canonical(value: object, column: str) -> Scalar:
    if value is None:
        return None
    # bool before int: bool is an int subclass, and True must not become 1.
    if isinstance(value, bool | int):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SqlSourceError(f"column {column!r} holds a non-finite decimal {value}")
        # normalize() drops trailing zeros (1.50 -> 1.5); format "f" undoes the
        # exponent normalize() may introduce (100 -> 1E+2 -> "100").
        return format(value.normalize(), "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SqlSourceError(f"column {column!r} holds a non-finite float {value}")
        return repr(value)
    if isinstance(value, str):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", text).rstrip()
    # datetime before date: datetime is a date subclass.
    if isinstance(value, datetime):
        if value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat()
        # Naive stays naive: guessing a timezone would invent a fact.
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise SqlSourceError(
        f"column {column!r} holds a {type(value).__name__} value, which has no "
        "canonical text form; add it to 'exclude' or cast it in the query"
    )
```

- [ ] **Step 4: Implement `identity.py`**

```python
"""Id column values -> a path-safe, injective native_id (spec §4.2).

The escape set is kbforge-mcp's (`kbforge_mcp/slug.py`, which documents each
character). It is copied rather than imported: kbforge-sql must not depend on
kbforge-mcp, and a shared helper belongs in core, which this release does not
touch. Each id value is ONE path segment, so none of slug.py's URL and path
handling applies -- only the per-segment escape and the two tail rules."""

from __future__ import annotations

import re
from collections.abc import Sequence

from kbforge.canonical import is_blank
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.values import Scalar

# `%` makes the escape injective; `/` keeps a value from becoming structure;
# the rest are illegal in an NTFS filename.
_ESCAPE = re.compile(r'[\x00-\x1f\x7f%/<>:"|?*\\]')
_UNSAFE_TAIL = (".", " ")


def _escape(part: str) -> str:
    escaped = _ESCAPE.sub(lambda m: f"%{ord(m.group()):02X}", part)
    # Windows refuses, and sometimes silently trims, a trailing dot or space.
    if escaped[-1:] in _UNSAFE_TAIL:
        escaped = f"{escaped[:-1]}%{ord(escaped[-1]):02X}"
    return escaped


def native_id_for(values: Sequence[Scalar], columns: Sequence[str]) -> str:
    parts: list[str] = []
    for column, value in zip(columns, values, strict=True):
        if value is None or (isinstance(value, str) and is_blank(value)):
            raise SqlSourceError(
                f"id column {column!r} is empty in a returned row; every row "
                "needs an id"
            )
        parts.append(_escape(str(value)))
    slug = "/".join(parts)
    # `concept_path` strips a `.md` suffix a second time downstream, which would
    # publish `x.md` and `x` to one file. Escaping the dot ends it.
    if slug.endswith(".md"):
        slug = f"{slug[:-3]}%2Emd"
    return slug
```

- [ ] **Step 5: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_values.py packages/kbforge-sql/tests/test_sql_identity.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add packages/kbforge-sql
git commit -m "feat(sql): canonical value forms and injective native_ids"
```

---

### Task 3: Deterministic markdown rendering

**Files:**
- Create: `packages/kbforge-sql/src/kbforge_sql/render.py`
- Test: `packages/kbforge-sql/tests/test_sql_render.py`

**Interfaces:**
- Consumes: `Scalar` (Task 2).
- Produces: `render_text(lead: str | None, attributes: Sequence[Sequence], group: dict | None) -> str`, where `attributes` is a list of `[column, value]` pairs and `group` is `{"heading": str, "columns": list[str], "rows": list[list[Scalar]]}` or `None`. This is exactly the JSON payload shape Task 4 writes, so `normalize` passes parsed JSON straight in.

- [ ] **Step 1: Write the failing tests**

`packages/kbforge-sql/tests/test_sql_render.py`:

```python
from kbforge_sql.render import render_text


def test_flat_entity_with_lead():
    text = render_text(
        "Gate driver for EV inverters.",
        [["product_id", "IMC300"], ["family", "MOTIX"], ["status", None]],
        None,
    )
    assert text == (
        "Gate driver for EV inverters.\n"
        "\n"
        "## Attributes\n"
        "- **product_id:** IMC300\n"
        "- **family:** MOTIX\n"
        "- **status:** —"
    )


def test_no_lead_starts_with_attributes():
    assert render_text(None, [["id", 1]], None) == "## Attributes\n- **id:** 1"


def test_bools_render_lowercase():
    assert render_text(None, [["active", True]], None).endswith("- **active:** true")


def test_newlines_in_an_attribute_become_br():
    assert render_text(None, [["note", "a\nb"]], None).endswith("- **note:** a<br>b")


def test_grouped_entity_renders_a_child_table():
    text = render_text(
        None,
        [["app_id", "EV"]],
        {
            "heading": "Products",
            "columns": ["product_id", "status"],
            "rows": [["IMC300", "active"], ["IMC301", None]],
        },
    )
    assert text == (
        "## Attributes\n"
        "- **app_id:** EV\n"
        "\n"
        "## Products\n"
        "| product_id | status |\n"
        "|---|---|\n"
        "| IMC300 | active |\n"
        "| IMC301 | — |"
    )


def test_pipes_and_newlines_in_cells_cannot_break_the_table():
    text = render_text(
        None, [["k", 1]], {"heading": "H", "columns": ["a|b"], "rows": [["x|y\nz"]]}
    )
    assert text.endswith("| a\\|b |\n|---|\n| x\\|y<br>z |")


def test_a_group_with_no_rows_still_renders_its_header():
    text = render_text(None, [["k", 1]], {"heading": "H", "columns": ["a"], "rows": []})
    assert text.endswith("## H\n| a |\n|---|")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_render.py -q`
Expected: `No module named 'kbforge_sql.render'`.

- [ ] **Step 3: Implement `render.py`**

```python
"""Canonical entity -> fixed-format markdown (spec §4.5).

No templating: wording is the synthesizer's job. This only has to present the
row faithfully and byte-stably, because this text is ALL a grounding reader
sees of a row (`llm_synthesizer._grounding_block` passes `text`, not
`structured`)."""

from __future__ import annotations

from collections.abc import Sequence

from kbforge_sql.values import Scalar

_NULL = "—"


def _value(v: Scalar) -> str:
    if v is None:
        return _NULL
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).replace("\n", "<br>")


def _cell(v: Scalar) -> str:
    return _value(v).replace("|", "\\|")


def render_text(
    lead: str | None, attributes: Sequence[Sequence], group: dict | None
) -> str:
    blocks: list[str] = []
    if lead:
        blocks.append(lead)
    blocks.append(
        "\n".join(["## Attributes", *(f"- **{k}:** {_value(v)}" for k, v in attributes)])
    )
    if group is not None:
        columns = group["columns"]
        lines = [
            f"## {group['heading']}",
            "| " + " | ".join(_cell(c) for c in columns) + " |",
            "|" + "---|" * len(columns),
            *("| " + " | ".join(_cell(v) for v in row) + " |" for row in group["rows"]),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_render.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add packages/kbforge-sql
git commit -m "feat(sql): deterministic markdown rendering of entities"
```

---

### Task 4: The connector — query, entities, normalize (no deletions yet)

**Files:**
- Create: `packages/kbforge-sql/src/kbforge_sql/connector.py`, `packages/kbforge-sql/tests/sql_testdb.py`
- Modify: `packages/kbforge-sql/pyproject.toml` (entry point)
- Test: `packages/kbforge-sql/tests/test_sql_connector.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `CONNECTOR` (a `SqlConnector` instance) with the four hookimpls; module constants `NAME = "sql"`, `TOMBSTONE = "application/vnd.kbforge.tombstone"`; module attribute `_sleep` (Task 6 patches it); helpers `_query(cfg) -> tuple[list[str], list[tuple]]`, `_query_once(url: URL, query: str) -> tuple[list[str], list[tuple]]`, `_entities(cfg, columns, rows) -> list[_Entity]`, and `_removed(cfg, prior: list[str], current: list[str]) -> list[str]` (a stub returning `[]` in this task; Task 5 implements it).
- Test helpers in `sql_testdb.py`: `make_db(tmp_path, monkeypatch) -> Path`, `execute(db: Path, sql: str) -> None`, `flat_cfg(**over) -> dict`, `grouped_cfg(**over) -> dict`.

- [ ] **Step 1: Write the test helpers**

`packages/kbforge-sql/tests/sql_testdb.py`:

```python
"""SQLite fixtures. SQLite through SQLAlchemy is a real SQL engine, so these
tests execute real queries rather than mocking a cursor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

URL_ENV = "KB_SQL_URL"

_SCHEMA = """
CREATE TABLE product (
    product_id TEXT, product_name TEXT, family TEXT, status TEXT,
    description TEXT, last_refreshed TEXT
);
INSERT INTO product VALUES
    ('IMC300', 'IMC300 motor controller', 'MOTIX', 'active',
     'Motor controller for EV pumps.', '2026-09-18T01:00:00'),
    ('TLE9', 'TLE9 gate driver', 'MOTIX', 'active',
     'Gate driver.', '2026-09-18T01:00:00'),
    ('XDP1', 'XDP1 power stage', 'XDP', 'eol', NULL, '2026-09-18T01:00:00');

CREATE TABLE app_product (
    app_id TEXT, app_name TEXT, segment TEXT, app_description TEXT,
    product_id TEXT, product_name TEXT, product_status TEXT
);
INSERT INTO app_product VALUES
    ('EV', 'EV traction', 'Automotive', 'Traction inverters.', 'TLE9', 'TLE9 gate driver', 'active'),
    ('EV', 'EV traction', 'Automotive', 'Traction inverters.', 'IMC300', 'IMC300 motor controller', 'active'),
    ('HP', 'Heat pump', 'Industrial', 'Heat pumps.', NULL, NULL, NULL);
"""


def make_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "source.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(_SCHEMA)
    monkeypatch.setenv(URL_ENV, f"sqlite:///{db}")
    return db


def execute(db: Path, sql: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(sql)


def flat_cfg(**over) -> dict:
    cfg = {
        "system": "products",
        "url_env": URL_ENV,
        "query": (
            "SELECT product_id, product_name, family, status, description, "
            "last_refreshed FROM product"
        ),
        "id": ["product_id"],
        "title": "product_name",
        "text": "description",
        "facets": ["family", "status"],
        "exclude": ["last_refreshed"],
        "type": "product",
        "url_template": "https://portal.example/products/{product_id}",
    }
    cfg.update(over)
    return cfg


def grouped_cfg(**over) -> dict:
    cfg = {
        "system": "applications",
        "url_env": URL_ENV,
        "query": (
            "SELECT app_id, app_name, segment, app_description, product_id, "
            "product_name, product_status FROM app_product"
        ),
        "id": ["app_id"],
        "title": "app_name",
        "text": "app_description",
        "facets": ["segment"],
        "type": "application",
        "group": {
            "children": ["product_id", "product_name", "product_status"],
            "order_by": ["product_id"],
            "heading": "Products",
        },
    }
    cfg.update(over)
    return cfg
```

- [ ] **Step 2: Write the failing connector tests**

`packages/kbforge-sql/tests/test_sql_connector.py`:

```python
from datetime import datetime

import pytest
from sql_testdb import execute, flat_cfg, grouped_cfg, make_db

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge.registry import build_registry
from kbforge_sql.connector import CONNECTOR, TOMBSTONE
from kbforge_sql.errors import SqlSourceError


def _docs(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    return result, {d.doc_id: d for d in CONNECTOR.kbforge_normalize(result.records)}


def test_the_connector_is_discovered_as_sql():
    # pluggy registers an entry-point plugin under the entry point's name.
    assert build_registry().get_plugin("sql") is CONNECTOR


def test_a_flat_source_yields_one_document_per_row(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result, docs = _docs(flat_cfg())
    assert result.complete is True
    assert sorted(docs) == ["products:IMC300", "products:TLE9", "products:XDP1"]
    doc = docs["products:IMC300"]
    assert doc.title == "IMC300 motor controller"
    assert doc.structured == {"family": "MOTIX", "status": "active", "type": "product"}
    assert doc.anchor.url == "https://portal.example/products/IMC300"
    assert doc.text == (
        "Motor controller for EV pumps.\n"
        "\n"
        "## Attributes\n"
        "- **product_id:** IMC300\n"
        "- **family:** MOTIX\n"
        "- **status:** active"
    )
    assert "last_refreshed" not in doc.text
    assert_fetch_contract(list(docs.values()), complete=True)


def test_a_null_text_column_leaves_no_lead(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(flat_cfg())
    assert docs["products:XDP1"].text.startswith("## Attributes\n")


def test_a_grouped_source_folds_rows_into_one_document(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(grouped_cfg())
    assert sorted(docs) == ["applications:EV", "applications:HP"]
    assert docs["applications:EV"].text == (
        "Traction inverters.\n"
        "\n"
        "## Attributes\n"
        "- **app_id:** EV\n"
        "- **segment:** Automotive\n"
        "\n"
        "## Products\n"
        "| product_id | product_name | product_status |\n"
        "|---|---|---|\n"
        "| IMC300 | IMC300 motor controller | active |\n"
        "| TLE9 | TLE9 gate driver | active |"
    )


def test_a_left_join_with_no_children_renders_an_empty_table(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(grouped_cfg())
    assert docs["applications:HP"].text.endswith(
        "| product_id | product_name | product_status |\n|---|---|---|"
    )


def test_child_order_does_not_depend_on_row_order(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, forward = _docs(grouped_cfg())
    _, backward = _docs(
        grouped_cfg(query=grouped_cfg()["query"] + " ORDER BY product_id DESC")
    )
    assert forward["applications:EV"].text == backward["applications:EV"].text


def test_a_duplicate_id_without_group_is_an_error(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "INSERT INTO product VALUES ('TLE9', 'dup', 'MOTIX', 'active', NULL, 'x');")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        "sql source 'products': 2 rows share the id 'TLE9'; configure 'group' "
        "to fold them, or make the id unique"
    )


def test_grouped_rows_that_disagree_on_an_entity_column_are_an_error(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "UPDATE app_product SET segment = 'Other' WHERE product_id = 'TLE9';")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(grouped_cfg(), None)
    assert str(exc.value) == (
        "sql source 'applications': rows for id 'EV' disagree on column "
        "'segment'; only 'group.children' columns may vary within an entity"
    )


def test_a_misnamed_column_fails_listing_the_real_ones(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(facets=["famliy"]), None)
    assert str(exc.value).startswith(
        "sql source 'products': configured column(s) ['famliy'] are not in the "
        "query result; the query returned: ['product_id', 'product_name', "
    )


def test_a_value_with_no_canonical_form_names_its_column(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(query=flat_cfg()["query"].replace("last_refreshed", "X'00' AS blob"), exclude=[])
    with pytest.raises(SqlSourceError, match="column 'blob' holds a bytes value"):
        CONNECTOR.kbforge_fetch(cfg, None)


def test_an_excluded_column_is_never_canonicalized(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query=flat_cfg()["query"].replace("last_refreshed", "X'00' AS blob"),
        exclude=["blob"],
    )
    CONNECTOR.kbforge_fetch(cfg, None)  # does not raise


def test_normalize_is_stable_and_clock_free(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(grouped_cfg(), None)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)
    first = CONNECTOR.kbforge_normalize(result.records)

    import kbforge_sql.connector as mod

    class NoClock:
        # normalize must not call now(); it must still parse retrieved_at, so
        # fromisoformat delegates. `tz` matches fetch's call shape, so a copied
        # clock call fails on the assertion, not on a TypeError.
        @staticmethod
        def now(tz=None):
            raise AssertionError("normalize called the clock (architecture 4.3)")

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(mod, "datetime", NoClock)
    second = CONNECTOR.kbforge_normalize(result.records)
    assert [d.anchor for d in first] == [d.anchor for d in second]


def test_one_retrieved_at_for_the_whole_run(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert len({r.anchor_hint["retrieved_at"] for r in result.records}) == 1


def test_the_query_cannot_write(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query=(
            "INSERT INTO product VALUES ('NEW', 'n', 'f', 's', 'd', 'x') "
            "RETURNING product_id, product_name, family, status, description, "
            "last_refreshed"
        )
    )
    CONNECTOR.kbforge_fetch(cfg, None)
    _, docs = _docs(flat_cfg())
    assert "products:NEW" not in docs  # rolled back, never committed


def test_a_statement_without_a_result_set_is_rejected(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with pytest.raises(SqlSourceError, match="returned no result set"):
        CONNECTOR.kbforge_fetch(flat_cfg(query="DELETE FROM product"), None)
    _, docs = _docs(flat_cfg())
    assert len(docs) == 3  # the DELETE was rolled back


def test_the_cursor_carries_the_manifest_under_sql(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert result.cursor.connector == "sql"
    assert result.cursor.payload == {"ids": ["IMC300", "TLE9", "XDP1"]}
    assert all(r.media_type != TOMBSTONE for r in result.records)
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_connector.py -q`
Expected: `No module named 'kbforge_sql.connector'`.

- [ ] **Step 4: Implement `connector.py`**

```python
"""The kbforge connector: four hookimpls over one rolled-back query.

`kbforge_fetch` may use a clock and the network; `kbforge_normalize` may not
(architecture §4.3). `retrieved_at` is stamped in fetch, into `anchor_hint`,
and normalize only reads it back. Everything normalize needs travels in the
record -- it never sees config."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool

from kbforge.canonical import content_hash, is_blank
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)
from kbforge_sql.config import SqlSourceConfig, check_columns, problems_for
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.identity import native_id_for
from kbforge_sql.render import render_text
from kbforge_sql.values import Scalar, canonical

NAME = "sql"
TOMBSTONE = "application/vnd.kbforge.tombstone"

# Indirection so tests can observe backoff without waiting (Task 6).
_sleep = time.sleep


@dataclass(frozen=True)
class _Entity:
    native_id: str
    payload: dict
    url: str | None


def _query_once(url: URL, query: str) -> tuple[list[str], list[tuple]]:
    """Run the one configured statement and roll back. `exec_driver_sql`, not
    `text()`: text() parses `:name` as a bind parameter, which breaks `'10:30'`
    literals and Postgres `::` casts in an operator's query."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            try:
                result = conn.exec_driver_sql(query)
                if not result.returns_rows:
                    raise SqlSourceError(
                        "the query returned no result set; a source query must "
                        "be a SELECT"
                    )
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchall()]
            finally:
                # No commit exists anywhere in this package. Whatever the
                # statement did, this discards it (spec §7).
                conn.rollback()
    finally:
        engine.dispose()
    return columns, rows


def _query(cfg: SqlSourceConfig) -> tuple[list[str], list[tuple]]:
    """Task 6 replaces this with URL building, retry and error hygiene."""
    import os

    from sqlalchemy import make_url

    url = make_url(os.environ[cfg.url_env])
    if cfg.password_env:
        url = url.set(password=os.environ[cfg.password_env])
    return _query_once(url, cfg.query)


def _sort_key(value: Scalar) -> tuple:
    # Type name before value, so a column mixing ints and strings still sorts
    # instead of raising TypeError; NULLs last.
    return (value is None, type(value).__name__, 0 if value is None else value)


def _entities(
    cfg: SqlSourceConfig, columns: list[str], rows: list[tuple]
) -> list[_Entity]:
    index = {c: i for i, c in enumerate(columns)}
    keep = [c for c in columns if c not in cfg.exclude]
    children = list(cfg.group.children) if cfg.group else []

    by_id: dict[str, list[dict[str, Scalar]]] = {}
    for raw in rows:
        row = {c: canonical(raw[index[c]], c) for c in keep}
        nid = native_id_for([row[c] for c in cfg.id], cfg.id)
        by_id.setdefault(nid, []).append(row)

    entity_cols = [c for c in keep if c not in children]
    entities: list[_Entity] = []
    for nid in sorted(by_id):
        group_rows = by_id[nid]
        if cfg.group is None and len(group_rows) > 1:
            raise SqlSourceError(
                f"{len(group_rows)} rows share the id {nid!r}; configure 'group' "
                "to fold them, or make the id unique"
            )
        for c in entity_cols:
            if len({json.dumps(r[c]) for r in group_rows}) > 1:
                raise SqlSourceError(
                    f"rows for id {nid!r} disagree on column {c!r}; only "
                    "'group.children' columns may vary within an entity"
                )
        first = group_rows[0]

        title = first[cfg.title]
        lead = first[cfg.text] if cfg.text else None
        attributes = [[c, first[c]] for c in cfg.id] + [
            [c, first[c]]
            for c in entity_cols
            if c not in cfg.id and c != cfg.title and c != cfg.text
        ]
        group = None
        if cfg.group is not None:
            child_rows = [[r[c] for c in children] for r in group_rows]
            # A LEFT JOIN's all-NULL row means "no children", not a child.
            child_rows = [r for r in child_rows if any(v is not None for v in r)]
            order = [children.index(c) for c in cfg.group.order_by]
            child_rows.sort(
                key=lambda r: ([_sort_key(r[i]) for i in order], json.dumps(r))
            )
            group = {
                "heading": cfg.group.heading or cfg.system,
                "columns": children,
                "rows": child_rows,
            }
        url = None
        if cfg.url_template is not None:
            url = cfg.url_template.format(
                **{c: quote(str(first[c]), safe="") for c in cfg.id}
            )
        entities.append(
            _Entity(
                native_id=nid,
                url=url,
                payload={
                    "title": nid if title is None or is_blank(str(title)) else str(title),
                    "lead": None if lead is None else str(lead),
                    "attributes": attributes,
                    "facets": {f: first[f] for f in cfg.facets if first[f] is not None},
                    "type": cfg.type,
                    "group": group,
                },
            )
        )
    return entities


def _removed(cfg: SqlSourceConfig, prior: list[str], current: list[str]) -> list[str]:
    """Task 5 implements deletions and their guards."""
    return []


class SqlConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=NAME,
            version="0.1.0",
            source_system="any database SQLAlchemy can reach",
            info_types=["entity"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        return problems_for(config)

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        cfg = SqlSourceConfig.model_validate(config)
        try:
            columns, rows = _query(cfg)
            check_columns(cfg, columns)
            entities = _entities(cfg, columns, rows)
            current = [e.native_id for e in entities]
            prior = list((cursor.payload.get("ids") if cursor else None) or [])
            removed = _removed(cfg, prior, current)
        except SqlSourceError as exc:
            raise SqlSourceError(f"sql source {cfg.system!r}: {exc}") from None

        stamped = datetime.now(tz=UTC).isoformat()

        def hint(native_id: str, url: str | None) -> dict:
            return {
                "system": cfg.system,
                "native_id": native_id,
                "url": url,
                "retrieved_at": stamped,
            }

        records = [
            RawRecord(
                anchor_hint=hint(e.native_id, e.url),
                media_type="application/json",
                payload=json.dumps(e.payload, sort_keys=True, ensure_ascii=False).encode(),
            )
            for e in entities
        ]
        records += [
            RawRecord(anchor_hint=hint(nid, None), media_type=TOMBSTONE, payload=b"")
            for nid in removed
        ]
        return FetchResult(
            records=records,
            cursor=Cursor(connector=NAME, payload={"ids": sorted(current)}),
            complete=True,
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            h = rec.anchor_hint
            system, native_id = h["system"], h["native_id"]
            anchor = ResourceAnchor(
                system=system,
                native_id=native_id,
                url=h.get("url"),
                retrieved_at=datetime.fromisoformat(h["retrieved_at"]),
                content_hash="",
            )
            doc_id = f"{system}:{native_id}"
            if rec.media_type == TOMBSTONE:
                doc = CanonicalDocument(
                    anchor=anchor, doc_id=doc_id, title=native_id, text="", deleted=True
                )
            else:
                p = json.loads(rec.payload)
                doc = CanonicalDocument(
                    anchor=anchor,
                    doc_id=doc_id,
                    title=p["title"],
                    text=render_text(p["lead"], p["attributes"], p["group"]),
                    structured={**p["facets"], "type": p["type"]},
                )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs


CONNECTOR = SqlConnector()
```

- [ ] **Step 5: Add the entry point**

Append to `packages/kbforge-sql/pyproject.toml`, after `dependencies`:

```toml
[project.entry-points."kbforge.connectors"]
sql = "kbforge_sql.connector:CONNECTOR"
```

Run: `uv sync --all-extras --dev && uv run kbforge list`
Expected: the list includes `sql`.

- [ ] **Step 6: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_connector.py -q`
Expected: all pass. If `test_the_connector_is_discovered_as_sql` fails, the entry point is not installed: re-run `uv sync --all-extras --dev`. If SQLite's `X'00'` literal comes back as `bytes` under another name, the assertion on the message still holds — the column alias is `blob`.

- [ ] **Step 7: Run the whole suite and commit**

Run: `uv run pytest -q`
Expected: all pass.

```bash
git add packages/kbforge-sql uv.lock
git commit -m "feat(sql): query, group and render entities; register the sql connector"
```

---

### Task 5: Deletions, the manifest, and the guards

**Files:**
- Modify: `packages/kbforge-sql/src/kbforge_sql/connector.py` (`_removed`)
- Test: `packages/kbforge-sql/tests/test_sql_deletions.py`

**Interfaces:**
- Consumes: `CONNECTOR`, `TOMBSTONE`, `_removed` (Task 4); `kbforge.pipeline.run`, `Published`, `NoOp`; `kbforge.publishers.dry_run.DryRunPublisher`.
- Produces: `_removed(cfg, prior, current) -> list[str]` (sorted ids to tombstone; raises `SqlSourceError` for the empty-result guard and the ceiling).

- [ ] **Step 1: Write the failing tests**

`packages/kbforge-sql/tests/test_sql_deletions.py`:

```python
from pathlib import Path

import pytest
from sql_testdb import execute, flat_cfg, make_db

from kbforge.models import Cursor
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge_sql.connector import CONNECTOR, TOMBSTONE
from kbforge_sql.errors import SqlSourceError


def _run(tmp_path: Path, cfg: dict):
    return run(
        CONNECTOR,
        DryRunPublisher(),
        config=cfg,
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )


def _concept(tmp_path: Path, native_id: str) -> Path:
    return tmp_path / "out" / "sync-products" / "concepts" / native_id / "overview.md"


def _prior(*ids: str) -> Cursor:
    return Cursor(connector="sql", payload={"ids": list(ids)})


def test_a_removed_row_is_tombstoned_and_its_concept_removed(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert _concept(tmp_path, "XDP1").exists()

    execute(db, "DELETE FROM product WHERE product_id = 'XDP1';")
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert not _concept(tmp_path, "XDP1").exists()
    assert _concept(tmp_path, "TLE9").exists()


def test_an_unchanged_table_is_a_noop(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    assert isinstance(_run(tmp_path, flat_cfg()), NoOp)


def test_the_manifest_round_trips_through_the_pipeline_under_sql(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    slots = list((tmp_path / "state").glob("cursor-sql-*.json"))
    assert len(slots) == 1  # saved under the same name run() loads it by
    assert Cursor.model_validate_json(slots[0].read_text()).payload == {
        "ids": ["IMC300", "TLE9", "XDP1"]
    }


def test_a_row_leaving_the_query_scope_is_tombstoned(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(
        flat_cfg(), _prior("IMC300", "TLE9", "XDP1", "GONE")
    )
    tombstones = [r for r in result.records if r.media_type == TOMBSTONE]
    assert [r.anchor_hint["native_id"] for r in tombstones] == ["GONE"]
    docs = CONNECTOR.kbforge_normalize(tombstones)
    assert docs[0].deleted is True
    assert docs[0].doc_id == "products:GONE"
    assert result.complete is True


def test_an_empty_result_never_deletes_everything(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "DELETE FROM product;")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(
            flat_cfg(max_removed_fraction=1.0), _prior("IMC300", "TLE9", "XDP1")
        )
    assert str(exc.value) == (
        "sql source 'products': the query returned no rows, but the last "
        "published run saw 3; refusing to delete every concept. If the source "
        "is meant to be empty, remove its config instead"
    )


def test_an_empty_first_run_is_not_an_error(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "DELETE FROM product;")
    assert CONNECTOR.kbforge_fetch(flat_cfg(), None).records == []


def test_the_deletion_ceiling_stops_a_mass_removal(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C", "D")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), prior)
    assert str(exc.value) == (
        "sql source 'products': 4 of 7 previously seen ids (57%) are missing, "
        "above max_removed_fraction=0.5; raise it for a deliberate cleanup"
    )


def test_the_ceiling_is_inclusive(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C")  # 3 of 6 = exactly 0.5
    result = CONNECTOR.kbforge_fetch(flat_cfg(), prior)
    assert len([r for r in result.records if r.media_type == TOMBSTONE]) == 3


def test_a_raised_ceiling_permits_the_cleanup(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C", "D")
    result = CONNECTOR.kbforge_fetch(flat_cfg(max_removed_fraction=1.0), prior)
    assert len([r for r in result.records if r.media_type == TOMBSTONE]) == 4


def test_an_aborted_run_re_emits_its_tombstone(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    execute(db, "DELETE FROM product WHERE product_id = 'XDP1';")

    class Boom(Exception):
        pass

    class FailingPublisher(DryRunPublisher):
        def kbforge_publish(self, change, config):
            raise Boom

    with pytest.raises(Boom):
        run(
            CONNECTOR,
            FailingPublisher(),
            config=flat_cfg(),
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={"out_dir": str(tmp_path / "out")},
        )
    # The cursor was not saved, so the manifest still holds XDP1 and the next
    # run tombstones it again (at-least-once, architecture §4.2).
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert not _concept(tmp_path, "XDP1").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_deletions.py -q`
Expected: the tombstone, empty-result and ceiling tests fail (the stub returns `[]`); the no-op and manifest tests may already pass. If `_concept`'s path is wrong (the dry-run branch directory is `branch_hint` with `/` → `-`, and `branch_hint` is `sync/<system>`), fix the helper, not the connector.

- [ ] **Step 3: Implement `_removed`**

Replace the stub in `connector.py`:

```python
def _removed(cfg: SqlSourceConfig, prior: list[str], current: list[str]) -> list[str]:
    """Ids seen at the last published run and missing now (spec §6).

    Both guards run before any tombstone exists. An empty result is refused
    even at max_removed_fraction=1.0: a view mid-refresh returns zero rows
    without an error, and 'delete the knowledge base' must never be the
    default reading of that."""
    if not prior:
        return []
    if not current:
        raise SqlSourceError(
            f"the query returned no rows, but the last published run saw "
            f"{len(prior)}; refusing to delete every concept. If the source is "
            "meant to be empty, remove its config instead"
        )
    gone = sorted(set(prior) - set(current))
    fraction = len(gone) / len(prior)
    if fraction > cfg.max_removed_fraction:
        raise SqlSourceError(
            f"{len(gone)} of {len(prior)} previously seen ids ({fraction:.0%}) are "
            f"missing, above max_removed_fraction={cfg.max_removed_fraction}; "
            "raise it for a deliberate cleanup"
        )
    return gone
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_deletions.py -q`
Expected: all pass. If a pipeline run returns `Aborted`, print `result.failures` — a real validation failure on a rendered concept is a defect in Task 3 or 4's output and must be fixed there, not by weakening a test.

- [ ] **Step 5: Commit**

```bash
git add packages/kbforge-sql
git commit -m "feat(sql): tombstones from the cursor manifest, with empty-result and ceiling guards"
```

---

### Task 6: Retry, driver errors, and secret hygiene

**Files:**
- Modify: `packages/kbforge-sql/src/kbforge_sql/connector.py` (`_query`, new `_url`)
- Test: `packages/kbforge-sql/tests/test_sql_errors.py`

**Interfaces:**
- Consumes: `_query_once`, `_sleep`, `CONNECTOR` (Task 4).
- Produces: `_url(cfg) -> URL`; `_query(cfg)` with retry; errors formatted as below.

Note: SQLite reports a missing table and a syntax error as `OperationalError`, which this design treats as transient, so against SQLite those are retried before failing. Postgres raises `ProgrammingError` and fails at once. The tests therefore drive the classification through a patched `_query_once` rather than through SQLite's own error classes.

- [ ] **Step 1: Write the failing tests**

`packages/kbforge-sql/tests/test_sql_errors.py`:

```python
import pytest
from sql_testdb import URL_ENV, flat_cfg, make_db
from sqlalchemy.exc import OperationalError, ProgrammingError

import kbforge_sql.connector as mod
from kbforge_sql.connector import CONNECTOR
from kbforge_sql.errors import SqlSourceError


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(mod, "_sleep", calls.append)
    return calls


def _operational(message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(message))


def test_a_transient_error_is_retried_then_succeeds(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    real = mod._query_once
    failures = iter([_operational("server closed the connection")])

    def flaky(url, query):
        for exc in failures:
            raise exc
        return real(url, query)

    monkeypatch.setattr(mod, "_query_once", flaky)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert len(result.records) == 3
    assert sleeps == [1]


def test_transient_errors_exhaust_the_retries(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)

    def down(url, query):
        raise _operational("connection refused")

    monkeypatch.setattr(mod, "_query_once", down)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(retries=2), None)
    assert str(exc.value) == (
        "sql source 'products': OperationalError after 3 attempt(s): "
        "connection refused"
    )
    assert sleeps == [1, 2]


def test_a_programming_error_is_not_retried(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)

    def bad_sql(url, query):
        raise ProgrammingError("SELECT", {}, Exception('relation "prodcut" does not exist'))

    monkeypatch.setattr(mod, "_query_once", bad_sql)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        "sql source 'products': ProgrammingError: relation \"prodcut\" does not exist"
    )
    assert sleeps == []


def test_the_password_never_appears_in_an_error(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_SQL_PASSWORD", "s3cret-pw")

    def leaky(url, query):
        raise _operational("login failed for password s3cret-pw")

    monkeypatch.setattr(mod, "_query_once", leaky)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(password_env="KB_SQL_PASSWORD", retries=0), None)
    assert "s3cret-pw" not in str(exc.value)
    assert "***" in str(exc.value)


def test_a_password_inside_the_url_is_redacted_too(tmp_path, monkeypatch, sleeps):
    make_db(tmp_path, monkeypatch)
    monkeypatch.setenv(URL_ENV, "postgresql://kb:inline-pw@db.example/x")

    def leaky(url, query):
        raise _operational("auth failed: inline-pw")

    monkeypatch.setattr(mod, "_query_once", leaky)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(retries=0), None)
    assert "inline-pw" not in str(exc.value)


def test_an_unparseable_url_is_reported_without_its_value(tmp_path, monkeypatch):
    monkeypatch.setenv(URL_ENV, "not a url s3cret")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        f"sql source 'products': the value of {URL_ENV} is not a SQLAlchemy URL"
    )


def test_a_missing_driver_names_the_remedy(tmp_path, monkeypatch):
    monkeypatch.setenv(URL_ENV, "nosuchdialect://kb@db.example/x")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value).startswith(
        "sql source 'products': no SQLAlchemy dialect or driver for this URL "
        "is installed ("
    )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_errors.py -q`
Expected: failures — the Task 4 `_query` has no retry and lets SQLAlchemy exceptions escape.

- [ ] **Step 3: Replace `_query` and add `_url`**

In `connector.py`, add to the imports:

```python
import os

from sqlalchemy import URL, create_engine, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, InterfaceError, OperationalError
```

and remove the function-local imports from the old `_query`. Then replace `_query`:

```python
# Retried: the connection failed, not the statement. Re-running a SELECT is
# safe. Everything else -- bad SQL, a missing view, no permission -- fails at
# once, because retrying a typo only delays the message.
_TRANSIENT = (OperationalError, InterfaceError)


def _url(cfg: SqlSourceConfig) -> URL:
    try:
        url = make_url(os.environ[cfg.url_env])
    except ArgumentError:
        # Never echo the value: it may carry a password.
        raise SqlSourceError(
            f"the value of {cfg.url_env} is not a SQLAlchemy URL"
        ) from None
    if cfg.password_env:
        # URL.set takes the raw password: no percent-escaping for `@:/%`.
        url = url.set(password=os.environ[cfg.password_env])
    return url


def _redact(message: str, url: URL) -> str:
    secret = url.password
    return message.replace(str(secret), "***") if secret else message


def _query(cfg: SqlSourceConfig) -> tuple[list[str], list[tuple]]:
    url = _url(cfg)
    attempts = cfg.retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return _query_once(url, cfg.query)
        except _TRANSIENT as exc:
            if attempt == attempts:
                raise SqlSourceError(
                    _redact(
                        f"{type(exc).__name__} after {attempts} attempt(s): {exc.orig}",
                        url,
                    )
                ) from None
            _sleep(2 ** (attempt - 1))
        except DBAPIError as exc:
            raise SqlSourceError(
                _redact(f"{type(exc).__name__}: {exc.orig}", url)
            ) from None
        except (ArgumentError, ImportError) as exc:
            # NoSuchModuleError is an ArgumentError; a dialect whose DBAPI
            # module is absent raises ImportError from create_engine.
            raise SqlSourceError(
                _redact(
                    "no SQLAlchemy dialect or driver for this URL is installed "
                    f"({exc}); install the driver for your database",
                    url,
                )
            ) from None
    raise AssertionError("unreachable: the loop returns or raises")
```

`from None` is deliberate: a chained SQLAlchemy exception prints the statement *and its bind parameters*, and the chain would reintroduce what `_redact` removed.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest packages/kbforge-sql/tests -q`
Expected: all pass (Tasks 1–6).

- [ ] **Step 5: Commit**

```bash
git add packages/kbforge-sql
git commit -m "feat(sql): retry transient errors only, and keep credentials out of messages"
```

---

### Task 7: Live test and documentation

**Files:**
- Create: `packages/kbforge-sql/tests/test_sql_live.py`
- Modify: `packages/kbforge-sql/README.md`, root `README.md`, `CHANGELOG.md`, `conftest.py` (help text), `docs/architecture.md` §4.1, `docs/design/2026-09-18-sql-source-connector-design.md`

**Interfaces:**
- Consumes: `CONNECTOR`, `flat_cfg`-style config.
- Produces: docs only, plus one live test.

- [ ] **Step 1: Write the live test**

`packages/kbforge-sql/tests/test_sql_live.py`:

```python
"""Live test against a real network database. Skipped unless --run-live.

Set KBFORGE_SQL_LIVE_URL to a SQLAlchemy URL (PostgreSQL by default, with its
driver installed: `uv pip install psycopg[binary]`) and, optionally,
KBFORGE_SQL_LIVE_PASSWORD. KBFORGE_SQL_LIVE_QUERY, _ID and _TITLE point the test
at an existing read-only view instead -- that is how Denodo is live-tested from
inside a network that reaches it."""

from __future__ import annotations

import os

import pytest

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge_sql.connector import CONNECTOR

pytestmark = pytest.mark.live


@pytest.fixture
def cfg():
    if not os.environ.get("KBFORGE_SQL_LIVE_URL"):
        pytest.skip("KBFORGE_SQL_LIVE_URL is not set")
    cfg = {
        "system": "live",
        "url_env": "KBFORGE_SQL_LIVE_URL",
        "query": os.environ.get(
            "KBFORGE_SQL_LIVE_QUERY",
            "SELECT table_schema || '.' || table_name AS id, table_name AS title, "
            "table_type FROM information_schema.tables "
            "WHERE table_schema = 'information_schema'",
        ),
        "id": [os.environ.get("KBFORGE_SQL_LIVE_ID", "id")],
        "title": os.environ.get("KBFORGE_SQL_LIVE_TITLE", "title"),
    }
    if os.environ.get("KBFORGE_SQL_LIVE_PASSWORD"):
        cfg["password_env"] = "KBFORGE_SQL_LIVE_PASSWORD"
    return cfg


def test_a_real_database_round_trips(cfg):
    assert CONNECTOR.kbforge_validate_config(cfg) == []
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert result.records, "the live query returned no rows"
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)
    assert_fetch_contract(docs, complete=True)


def test_two_fetches_hash_identically(cfg):
    # The real §4.3 law 1 test for a live source: nothing volatile leaks in.
    a = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(cfg, None).records)
    b = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(cfg, None).records)
    assert [d.anchor.content_hash for d in a] == [d.anchor.content_hash for d in b]
```

Run: `uv run pytest packages/kbforge-sql/tests/test_sql_live.py -q`
Expected: 2 skipped.

In the root `conftest.py`, extend the `--run-live` help string: after `GITHUB_TOKEN for GitHub)` add `, and the SQL source connector (KBFORGE_SQL_LIVE_URL)` inside the parenthesised list.

- [ ] **Step 2: Write the package README**

Replace `packages/kbforge-sql/README.md` with operator docs covering, in this order: what it is (one paragraph); install (`pip install kbforge-sql` plus a driver, with `denodo-sqlalchemy`, `psycopg`, `oracledb`, `pyodbc` as examples); the flat and grouped config examples copied verbatim from spec §3; the `kbforge run --connector sql --set …` form (one `--set` per key, as `kbforge-mcp`'s README shows); **first run with `dry-run` and a throwaway mirror** (spec §5); credentials (`url_env`, `password_env`, a dedicated read-only account); what "deleted" means and the two guards (spec §6.3); the two known limits verbatim in substance (spec §6.4 query edits, §6.5 id prefixes); and a link to architecture.md §4.1.

- [ ] **Step 3: Update the root README, CHANGELOG, architecture and the design note**

- Root `README.md`, **Status** → **Sources** bullet: add that any database SQLAlchemy can reach becomes a source through configuration via `kbforge-sql`. **Companion distributions** table: add a `kbforge-sql` row (`kbforge_sql`, "makes any database SQLAlchemy can reach a kbforge **source**, one scoped query per source"). Add its PyPI badge next to the other two.
- `CHANGELOG.md`: under `## [Unreleased]` → `### Added`, one bullet: "`kbforge-sql` 0.1.0, a separate distribution that makes any database SQLAlchemy can reach a source through configuration. One scoped query per source defines the corpus; every run is a snapshot, so a row leaving the result becomes a tombstone, guarded against an empty result and a mass removal." At release, this moves under a heading keyed by the tag `kbforge-sql-v0.1.0` (CLAUDE.md, "Releasing").
- `docs/architecture.md` §4.1: after the `kbforge-mcp` block, add a short paragraph "**The SQL source connector, `kbforge-sql` (shipped, not core).**" stating: one scoped query per source defines the corpus; a full snapshot makes tombstones derivable from a cursor manifest, which makes it the first connector to emit them; read-only is a rolled-back transaction plus the account's grants, and kbforge cannot prove a SQL string side-effect free. Link the design note for rationale and deferrals.
- The design note's frontmatter `status:` becomes `shipped in kbforge-sql 0.1.0 — §9 still deferred`.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: all pass.

```bash
git add packages/kbforge-sql conftest.py README.md CHANGELOG.md docs
git commit -m "docs(sql): operator README, live test, and architecture §4.1"
```

---

### Task 8: Verify the gates and the real thing

**Files:** none changed in the end; every mutation is restored.

- [ ] **Step 1: Confirm a clean tree**

Run: `git status --short`
Expected: empty. Mutations must start from a committed state.

- [ ] **Step 2: Mutation-check each gate**

For each row: apply the mutation in place, run the named test, confirm it fails **with the stated message fragment**, then `git checkout -- packages/kbforge-sql/src`.

| Mutation in `connector.py` / `config.py` | Test that must fail | Message fragment |
|---|---|---|
| `_removed`: delete the `if not current:` block | `test_an_empty_result_never_deletes_everything` | `refusing to delete every concept` missing (DID NOT RAISE or ceiling message instead) |
| `_removed`: `>` → `>=` in the ceiling | `test_the_ceiling_is_inclusive` | `3 of 6 previously seen ids` |
| `_query_once`: replace `conn.rollback()` with `conn.commit()` | `test_the_query_cannot_write` | `assert 'products:NEW' not in` |
| `_entities`: delete the duplicate-id `raise` | `test_a_duplicate_id_without_group_is_an_error` | `DID NOT RAISE` |
| `_entities`: delete the disagreeing-column loop | `test_grouped_rows_that_disagree_on_an_entity_column_are_an_error` | `DID NOT RAISE` |
| `check_columns`: `return` on its first line | `test_a_misnamed_column_fails_listing_the_real_ones` | the error is a `KeyError`, not the column list |
| `_query`: add `DBAPIError` to `_TRANSIENT` | `test_a_programming_error_is_not_retried` | `sleeps == []` |
| `_redact`: `return message` | `test_the_password_never_appears_in_an_error` | `s3cret-pw` |
| `normalize`: `datetime.now(tz=UTC)` for `retrieved_at` | `test_normalize_is_stable_and_clock_free` | `normalize called the clock` |
| `identity._ESCAPE`: drop `%` | `test_percent_is_escaped_so_the_escape_is_injective` | `assert 'a%2Fb' != 'a%2Fb'` |

Run after the last restore: `git status --short && uv run pytest packages/kbforge-sql -q`
Expected: empty status; all pass.

- [ ] **Step 3: Run the real CLI end to end**

```bash
S=$(mktemp -d)
sqlite3 "$S/src.db" "CREATE TABLE product(product_id TEXT, product_name TEXT, family TEXT);
INSERT INTO product VALUES ('IMC300','IMC300 motor controller','MOTIX'),('TLE9','TLE9 gate driver','MOTIX');"
export KB_SQL_URL="sqlite:///$S/src.db"
uv run kbforge run --connector sql \
  --set system=products --set url_env=KB_SQL_URL \
  --set 'query=SELECT product_id, product_name, family FROM product' \
  --set 'id=[product_id]' --set title=product_name --set 'facets=[family]' --set type=product \
  --mirror "$S/mirror" --state "$S/state" --out "$S/out"
cat "$S"/out/sync-products/concepts/IMC300/overview.md
```

Expected: the run reports a publish to `$S/out`; the concept has frontmatter `type: product`, `family: MOTIX`, a `sources` entry, and the `## Attributes` block. Run the same command again: expect a no-op. Then `sqlite3 "$S/src.db" "DELETE FROM product WHERE product_id='TLE9'"` and run again: expect a publish whose output no longer contains `concepts/TLE9/`.

Repeat once with `--synthesizer llm` and the `.env` gateway credentials if they are available (`uv run --env-file .env …`), and read one generated concept to confirm the prose is grounded in the attributes. Record the result in the PR description either way.

- [ ] **Step 4: Live suite, if a database is reachable**

Run: `KBFORGE_SQL_LIVE_URL=postgresql+psycopg://… uv run pytest packages/kbforge-sql/tests/test_sql_live.py --run-live -q`
Expected: 2 passed. If no Postgres is available, say so in the PR; the Denodo run happens inside the operator's network using the recipe in the live test's docstring.

- [ ] **Step 5: Full verification**

Run: `uv run pytest -q && uvx prek run --all-files && grep -rn 'def .*merge' src/kbforge/publishers/ packages/kbforge-sql/ ; git diff main --stat -- src/kbforge`
Expected: tests and hooks pass; the grep prints nothing; the diff over `src/kbforge` is empty.

Release (tag `kbforge-sql-v0.1.0`, PyPI trusted-publisher registration for the new project name) is a separate, user-approved step and is not part of this plan.
