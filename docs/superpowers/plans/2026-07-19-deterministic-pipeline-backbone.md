# Deterministic Pipeline Backbone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A deterministic, end-to-end `kbforge` pipeline that ingests a folder of fixture markdown files, canonicalizes, mirrors, diffs, synthesizes OKF concepts (stub, no LLM), validates (strict OKF + §4.4 laws), and dry-run publishes — proving Bootstrap (`cursor=None`) and Refresh-lite in one runnable command.

**Architecture:** Follows the fixed pipeline of architecture.md §7 (`fetch → normalize → mirror → diff → scope → synthesize → validate → publish`). The connector and publisher interfaces are Pluggy hookspecs (§5); the pipeline depends on *bound* connector/publisher objects (dependency injection), so it is testable without the plugin-discovery layer, which arrives last. The mirror is a directory of canonical JSON; the diff is read-only, and mirror + cursor state are committed only after a run fully succeeds, preserving replay/at-least-once semantics (§4.2). "Scope" is represented by passing the `ChangeSet` to synthesis so only changed docs are processed — no separate module.

**Tech Stack:** Python ≥3.12, Pydantic v2, pluggy ≥1.5, PyYAML ≥6 (added in Task 4), pytest+cov, uv, prek (ruff + ty).

## Global Constraints

- Python ≥3.12; Pydantic v2 models; `from __future__ import annotations` at the top of every new module.
- ruff rules E/F/I/UP, line length **88** — keep every line ≤88 chars.
- `normalize()` is **pure**: no network, no clock (`datetime.now`/`utcnow`), no randomness. Time comes from the source (file mtime), set in `fetch` and carried through `RawRecord.anchor_hint`.
- The pipeline order is **not** pluggable; the no-op rule and the never-auto-merge rule are trust guarantees — the dry-run publisher writes files and **never merges**; a no-op `ChangeSet` returns before any publish.
- All datetimes are **timezone-aware UTC** (§4.4 law 4 rejects naive stamps).
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Frequent commits.
- Run tests with `uv run pytest`; the pre-commit hooks (`prek`) run ruff-check --fix, ruff-format, and ty on commit — if ruff-format reformats a file, `git add -A` and re-commit.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- Do **not** modify the existing §4.4 validators (`run_artifact_validators`, the `_check_*` helpers) or the emit-side models — extend, never rewrite.

## File Structure

- `src/kbforge/models.py` — **extend** with ingest-side models (Task 1).
- `src/kbforge/canonical.py` — content hashing + stability check (Task 2).
- `src/kbforge/mirror.py` — canonical mirror store + read-only diff (Task 3).
- `src/kbforge/hookspecs.py` — Pluggy `ConnectorSpec` / `PublisherSpec` (Task 4).
- `src/kbforge/connectors/__init__.py`, `src/kbforge/connectors/local_files.py` — the fixture connector (Task 4).
- `src/kbforge/synthesize.py` — stub synthesizer (Task 5).
- `src/kbforge/validate.py` — **extend** with strict-OKF checks + unified `run_validators` (Task 6).
- `src/kbforge/publishers/__init__.py`, `src/kbforge/publishers/dry_run.py` — dry-run publisher (Task 7).
- `src/kbforge/pipeline.py` — the `run()` loop (Task 8).
- `src/kbforge/registry.py`, `src/kbforge/__main__.py` — plugin discovery + CLI (Task 9).
- Tests mirror each under `tests/`.

---

### Task 1: Ingest-side models

**Files:**
- Modify: `src/kbforge/models.py` (append after `ProposedChange`)
- Test: `tests/test_ingest_models.py`

**Interfaces:**
- Consumes: existing `ResourceAnchor` (models.py).
- Produces: `Cursor`, `ConnectorInfo`, `RawRecord`, `FetchResult`, `CanonicalDocument`, `ChangeSet` — the ingest vocabulary every later task uses.

- [ ] **Step 1: Write the failing test** — `tests/test_ingest_models.py`

```python
from datetime import UTC, datetime

from kbforge.models import (
    CanonicalDocument,
    ChangeSet,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _anchor():
    return ResourceAnchor(
        system="local_files", native_id="apps/x", retrieved_at=NOW, content_hash="h"
    )


def test_changeset_is_noop_when_empty():
    assert ChangeSet().is_noop is True
    assert ChangeSet(unchanged_count=5).is_noop is True


def test_changeset_not_noop_with_any_change():
    assert ChangeSet(added=["a"]).is_noop is False
    assert ChangeSet(modified=["b"]).is_noop is False
    assert ChangeSet(removed=["c"]).is_noop is False


def test_fetchresult_defaults_complete_true():
    fr = FetchResult(cursor=Cursor(connector="local_files"))
    assert fr.complete is True
    assert fr.records == []


def test_canonical_document_defaults():
    doc = CanonicalDocument(
        anchor=_anchor(), doc_id="local_files:apps/x", title="X", text="body"
    )
    assert doc.deleted is False
    assert doc.structured == {}
    assert doc.relations == []


def test_connector_and_raw_record():
    info = ConnectorInfo(
        name="local_files", version="0.1.0", source_system="local filesystem"
    )
    assert info.info_types == []
    rec = RawRecord(media_type="text/markdown", payload=b"# X")
    assert rec.anchor_hint == {}
```

- [ ] **Step 2: Run it, verify it fails** — `uv run pytest tests/test_ingest_models.py -q` → ImportError on the new names.

- [ ] **Step 3: Implement** — append to `src/kbforge/models.py`:

```python
class Cursor(BaseModel):
    """Opaque incremental-sync watermark. Core persists it; only the owning
    connector interprets its payload (§4.2)."""

    connector: str
    payload: dict = Field(default_factory=dict)


class ConnectorInfo(BaseModel):
    """Static self-description; used for registry listing (§3)."""

    name: str
    version: str
    source_system: str
    info_types: list[str] = Field(default_factory=list)


class RawRecord(BaseModel):
    """One record as fetched. `anchor_hint` carries what normalize needs to build
    a ResourceAnchor (native_id, url, retrieved_at) — set in fetch, so normalize
    stays clock-free (§4.3)."""

    anchor_hint: dict = Field(default_factory=dict)
    media_type: str
    payload: bytes


class FetchResult(BaseModel):
    records: list[RawRecord] = Field(default_factory=list)
    cursor: Cursor
    complete: bool = True


class CanonicalDocument(BaseModel):
    """The diff-stable unit the mirror stores (§3, §4.3)."""

    anchor: ResourceAnchor
    doc_id: str
    title: str
    text: str
    structured: dict = Field(default_factory=dict)
    relations: list[str] = Field(default_factory=list)
    deleted: bool = False


class ChangeSet(BaseModel):
    """Output of the diff stage; input to synthesis scoping (§3)."""

    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged_count: int = 0

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.modified or self.removed)
```

- [ ] **Step 4: Run, verify pass** — `uv run pytest tests/test_ingest_models.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(models): add ingest-side models"`

---

### Task 2: Canonical hashing and the stability check

**Files:**
- Create: `src/kbforge/canonical.py`
- Test: `tests/test_canonical.py`

**Interfaces:**
- Consumes: `CanonicalDocument`, `RawRecord` (Task 1).
- Produces: `content_hash(doc) -> str` (hashes canonical CONTENT, excluding the anchor); `assert_stability(normalize, records)` raising `StabilityError`.

- [ ] **Step 1: Write the failing test** — `tests/test_canonical.py`

```python
from datetime import UTC, datetime

import pytest

from kbforge.canonical import StabilityError, assert_stability, content_hash
from kbforge.models import CanonicalDocument, RawRecord, ResourceAnchor


def _doc(text="body", retrieved_at=datetime(2026, 7, 19, tzinfo=UTC)):
    anchor = ResourceAnchor(
        system="s", native_id="n", retrieved_at=retrieved_at, content_hash="ignored"
    )
    return CanonicalDocument(anchor=anchor, doc_id="s:n", title="T", text=text)


def test_content_hash_excludes_anchor_volatility():
    a = _doc(retrieved_at=datetime(2026, 7, 19, tzinfo=UTC))
    b = _doc(retrieved_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert content_hash(a) == content_hash(b)  # retrieved_at must not affect it


def test_content_hash_reacts_to_content():
    assert content_hash(_doc("one")) != content_hash(_doc("two"))


def test_assert_stability_passes_for_pure_normalize():
    def normalize(records):
        return [_doc()]

    assert_stability(normalize, [RawRecord(media_type="x", payload=b"")]) is None


def test_assert_stability_raises_for_unstable_normalize():
    calls = {"n": 0}

    def flaky(records):
        calls["n"] += 1
        return [_doc(text=f"body-{calls['n']}")]

    with pytest.raises(StabilityError):
        assert_stability(flaky, [RawRecord(media_type="x", payload=b"")])
```

- [ ] **Step 2: Run it, verify it fails** — import error.

- [ ] **Step 3: Implement** — `src/kbforge/canonical.py`

```python
"""Canonicalization: stable content hashing and the §4.3 law-1 stability check."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from kbforge.models import CanonicalDocument, RawRecord


class StabilityError(RuntimeError):
    """normalize() produced different canonical content for identical input."""


def content_hash(doc: CanonicalDocument) -> str:
    """SHA-256 over the canonical CONTENT — everything the diff must react to.
    The anchor is excluded: `retrieved_at` is volatile (§4.3 law 2) and the
    anchor's own content_hash would be circular."""
    payload = {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "text": doc.text,
        "structured": doc.structured,
        "relations": sorted(doc.relations),
        "deleted": doc.deleted,
    }
    blob = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_stability(
    normalize: Callable[[Sequence[RawRecord]], list[CanonicalDocument]],
    records: Sequence[RawRecord],
) -> None:
    """§4.3 law 1: normalize twice over identical input, require identical content
    hashes. A connector that fails is not deterministic and must be rejected."""
    first = [content_hash(d) for d in normalize(records)]
    second = [content_hash(d) for d in normalize(records)]
    if first != second:
        raise StabilityError("normalize() is not deterministic over identical input")
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(canonical): content hashing and stability check"`

---

### Task 3: The canonical mirror and read-only diff

**Files:**
- Create: `src/kbforge/mirror.py`
- Test: `tests/test_mirror.py`

**Interfaces:**
- Consumes: `CanonicalDocument`, `ChangeSet` (Task 1); `content_hash` is *not* used here — the diff compares stored vs incoming `anchor.content_hash`, which the connector sets in normalize (Task 4).
- Produces: `diff(mirror: Path, docs) -> ChangeSet` (read-only); `commit(mirror: Path, docs) -> None` (advance state, success-only).

- [ ] **Step 1: Write the failing test** — `tests/test_mirror.py`

```python
from datetime import UTC, datetime
from pathlib import Path

from kbforge.mirror import commit, diff
from kbforge.models import CanonicalDocument, ResourceAnchor

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _doc(doc_id="s:a", chash="h1", deleted=False):
    anchor = ResourceAnchor(
        system="s", native_id=doc_id.split(":")[1], retrieved_at=NOW, content_hash=chash
    )
    return CanonicalDocument(
        anchor=anchor, doc_id=doc_id, title="T", text="body", deleted=deleted
    )


def test_all_new_docs_are_added(tmp_path: Path):
    cs = diff(tmp_path, [_doc("s:a"), _doc("s:b")])
    assert cs.added == ["s:a", "s:b"]
    assert cs.is_noop is False


def test_diff_is_read_only(tmp_path: Path):
    diff(tmp_path, [_doc("s:a")])
    assert list(tmp_path.iterdir()) == []  # diff must NOT write


def test_committed_docs_are_unchanged_next_run(tmp_path: Path):
    docs = [_doc("s:a", "h1")]
    commit(tmp_path, docs)
    cs = diff(tmp_path, docs)
    assert cs.is_noop is True
    assert cs.unchanged_count == 1


def test_content_change_is_modified(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    cs = diff(tmp_path, [_doc("s:a", "h2")])
    assert cs.modified == ["s:a"]


def test_tombstone_removes_only_if_present(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    cs = diff(tmp_path, [_doc("s:a", deleted=True)])
    assert cs.removed == ["s:a"]
    # a tombstone for an unknown doc is not a removal
    cs2 = diff(tmp_path, [_doc("s:ghost", deleted=True)])
    assert cs2.removed == []


def test_commit_deletes_tombstoned(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    commit(tmp_path, [_doc("s:a", deleted=True)])
    assert diff(tmp_path, [_doc("s:a", "h1")]).added == ["s:a"]  # gone from mirror
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — `src/kbforge/mirror.py`

```python
"""The canonical mirror and the read-only diff (architecture §7's mirror_and_diff,
split into a pure `diff` and a success-only `commit`)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from kbforge.models import CanonicalDocument, ChangeSet


def _slot(mirror: Path, doc_id: str) -> Path:
    key = hashlib.sha256(doc_id.encode("utf-8")).hexdigest()
    return mirror / f"{key}.json"


def _load(mirror: Path, doc_id: str) -> CanonicalDocument | None:
    slot = _slot(mirror, doc_id)
    if not slot.exists():
        return None
    return CanonicalDocument.model_validate_json(slot.read_text("utf-8"))


def diff(mirror: Path, docs: list[CanonicalDocument]) -> ChangeSet:
    """Read-only comparison against the mirror. Deletions are explicit tombstones
    (`deleted=True`); absence never implies one (§4.2). Never mutates the mirror."""
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    unchanged = 0
    for doc in docs:
        prev = _load(mirror, doc.doc_id)
        if doc.deleted:
            if prev is not None:
                removed.append(doc.doc_id)
            continue
        if prev is None:
            added.append(doc.doc_id)
        elif prev.anchor.content_hash != doc.anchor.content_hash:
            modified.append(doc.doc_id)
        else:
            unchanged += 1
    return ChangeSet(
        added=sorted(added),
        modified=sorted(modified),
        removed=sorted(removed),
        unchanged_count=unchanged,
    )


def commit(mirror: Path, docs: list[CanonicalDocument]) -> None:
    """Advance the mirror to the fetched state. Called only after a run fully
    succeeds, so a failed publish never leaves the mirror ahead of the bundle."""
    mirror.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        slot = _slot(mirror, doc.doc_id)
        if doc.deleted:
            slot.unlink(missing_ok=True)
        else:
            slot.write_text(doc.model_dump_json(), "utf-8")
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(mirror): canonical mirror store and read-only diff"`

---

### Task 4: Hookspecs and the local-files connector

**Files:**
- Create: `src/kbforge/hookspecs.py`, `src/kbforge/connectors/__init__.py`, `src/kbforge/connectors/local_files.py`
- Modify: `pyproject.toml` (add `pyyaml>=6` to `dependencies`)
- Test: `tests/test_local_files_connector.py`

**Interfaces:**
- Consumes: `ConnectorInfo`, `Cursor`, `FetchResult`, `RawRecord`, `CanonicalDocument`, `ResourceAnchor` (Task 1); `content_hash` (Task 2).
- Produces: `ConnectorSpec`/`PublisherSpec` markers (`hookspec`, `hookimpl`, `PROJECT`); `LocalFilesConnector` with `kbforge_connector_info`, `kbforge_validate_config`, `kbforge_fetch`, `kbforge_normalize`. `doc_id` scheme: `f"local_files:{relative_path}"`. Concept-relevant frontmatter keys: `type`, `title`, `relations` are reserved; all other scalar/list frontmatter becomes `structured`.

- [ ] **Step 1: Add the dependency** — in `pyproject.toml`, add `"pyyaml>=6"` to the `dependencies` list. Run `uv sync --all-extras --dev`.

- [ ] **Step 2: Write the failing test** — `tests/test_local_files_connector.py`

```python
from pathlib import Path

from kbforge.canonical import assert_stability
from kbforge.connectors.local_files import LocalFilesConnector


def _write(dirp: Path, rel: str, body: str) -> None:
    dest = dirp / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, "utf-8")


DOC = """---
type: application
title: App X
owner: team-a
relations:
  - apps/y
---
App X does things.
"""


def test_fetch_then_normalize_builds_canonical_doc(tmp_path: Path):
    _write(tmp_path, "apps/x.md", DOC)
    conn = LocalFilesConnector()
    cfg = {"path": str(tmp_path)}
    assert conn.kbforge_validate_config(cfg) == []
    result = conn.kbforge_fetch(cfg, None)
    docs = conn.kbforge_normalize(result.records)
    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == "local_files:apps/x.md"
    assert doc.title == "App X"
    assert doc.structured == {"owner": "team-a"}  # type/title/relations reserved
    assert doc.relations == ["local_files:apps/y"]
    assert "App X does things." in doc.text
    assert doc.anchor.retrieved_at.utcoffset() is not None  # tz-aware
    assert doc.anchor.content_hash  # set during normalize


def test_normalize_is_stable(tmp_path: Path):
    _write(tmp_path, "apps/x.md", DOC)
    conn = LocalFilesConnector()
    result = conn.kbforge_fetch({"path": str(tmp_path)}, None)
    assert_stability(conn.kbforge_normalize, result.records)  # must not raise


def test_validate_config_reports_missing_path(tmp_path: Path):
    conn = LocalFilesConnector()
    problems = conn.kbforge_validate_config({"path": str(tmp_path / "nope")})
    assert problems and "path" in problems[0]
```

- [ ] **Step 3: Run it, verify it fails.**

- [ ] **Step 4: Implement hookspecs** — `src/kbforge/hookspecs.py`

```python
"""Pluggy hookspecs. The connector and publisher interfaces ARE the product (§5).
Kept minimal for the walking skeleton: one connector family, one publisher family."""

from __future__ import annotations

import pluggy

from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    RawRecord,
)

PROJECT = "kbforge"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)


class ConnectorSpec:
    """One plugin object per system of record. Connectors never see the bundle,
    never call the LLM, never touch git (§4.1)."""

    @hookspec
    def kbforge_connector_info(self) -> ConnectorInfo:
        """Static self-description."""

    @hookspec
    def kbforge_validate_config(self, config: dict) -> list[str]:
        """Return human-readable problems ([] = ok). No network I/O."""

    @hookspec
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        """Pull raw records (cursor=None = full backfill / bootstrap)."""

    @hookspec
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        """Deterministic, volatile-free, clock-free (§4.3)."""


class PublisherSpec:
    """Where proposals go. MUST NOT merge (§5.2)."""

    @hookspec
    def kbforge_publisher_info(self) -> ConnectorInfo:
        """Static self-description."""

    @hookspec
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        """Open a review request; return its URL/path. Never merges."""
```

- [ ] **Step 5: Implement the connector** — `src/kbforge/connectors/__init__.py` (empty) and `src/kbforge/connectors/local_files.py`

```python
"""A fixture connector: a folder of markdown-with-frontmatter → canonical docs.
No credentials, no network — the deterministic source for the walking skeleton."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

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

_SYSTEM = "local_files"
# Keys handled structurally, so they never leak into `structured` (hence facets):
# title → the concept title; relations → cross-links; type is dropped here because
# the OKF type comes from synthesis taxonomy (the stub emits "concept"); description
# and timestamp are emit-side OKF fields the synthesizer generates, not source data.
_RESERVED_KEYS = frozenset(
    {"type", "title", "relations", "description", "timestamp"}
)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    front_raw, sep, body = rest.partition("\n---")
    if not sep:
        return {}, text
    data = yaml.safe_load(front_raw) or {}
    front = data if isinstance(data, dict) else {}
    return front, body.lstrip("\n")


class LocalFilesConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_SYSTEM,
            version="0.1.0",
            source_system="local filesystem (fixture)",
            info_types=["fixture"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        path = config.get("path")
        if not path or not Path(path).is_dir():
            return [f"config 'path' is not a readable directory: {path!r}"]
        return []

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        # cursor is unused: this feed-less source always re-scans; the mirror diff
        # provides incrementality. retrieved_at is stamped here (fetch may use a
        # clock; normalize may not) from file mtime, keeping runs reproducible.
        root = Path(config["path"])
        records: list[RawRecord] = []
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            records.append(
                RawRecord(
                    anchor_hint={
                        "native_id": rel,
                        "url": None,
                        "retrieved_at": mtime.isoformat(),
                    },
                    media_type="text/markdown",
                    payload=path.read_bytes(),
                )
            )
        return FetchResult(records=records, cursor=Cursor(connector=_SYSTEM))

    @hookimpl
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            front, body = _split_frontmatter(rec.payload.decode("utf-8"))
            native_id = rec.anchor_hint["native_id"]
            doc_id = f"{_SYSTEM}:{native_id}"
            relations = sorted(
                f"{_SYSTEM}:{r}"
                for r in front.get("relations", [])
                if isinstance(r, str)
            )
            structured = {
                k: v for k, v in front.items() if k not in _RESERVED_KEYS
            }
            anchor = ResourceAnchor(
                system=_SYSTEM,
                native_id=native_id,
                url=rec.anchor_hint.get("url"),
                retrieved_at=datetime.fromisoformat(rec.anchor_hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=doc_id,
                title=str(front.get("title") or native_id),
                text=body.strip(),
                structured=structured,
                relations=relations,
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs
```

- [ ] **Step 6: Run, verify pass.**
- [ ] **Step 7: Commit** — `git commit -m "feat(connectors): hookspecs and local-files fixture connector"`

---

### Task 5: Stub synthesizer

**Files:**
- Create: `src/kbforge/synthesize.py`
- Test: `tests/test_synthesize.py`

**Interfaces:**
- Consumes: `CanonicalDocument`, `ChangeSet`, `ChangeSummary`, `ConceptFrontmatter`, `ProposedChange` (models).
- Produces: `synthesize(changed_docs, changeset, existing_paths=frozenset()) -> ProposedChange`; `concept_path(doc_id) -> str` (bundle path, shared with the pipeline). Path scheme: `doc_id "system:native"` → `f"concepts/{native without .md}/overview.md"`. Facets keep only scalar / flat-list `structured` values. Links = resolvable `concept_path` of each relation, dangling ones dropped (§4.4 law 2). `type` defaults to `"concept"`. `freshness = doc.anchor.retrieved_at`.

- [ ] **Step 1: Write the failing test** — `tests/test_synthesize.py`

```python
from datetime import UTC, datetime

from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor
from kbforge.synthesize import concept_path, synthesize

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _doc(doc_id, structured=None, relations=None):
    native = doc_id.split(":", 1)[1]
    anchor = ResourceAnchor(
        system="local_files", native_id=native, retrieved_at=NOW, content_hash="h"
    )
    return CanonicalDocument(
        anchor=anchor,
        doc_id=doc_id,
        title=native,
        text="body",
        structured=structured or {},
        relations=relations or [],
    )


def test_synthesizes_a_conformant_concept():
    doc = _doc("local_files:apps/x.md", structured={"owner": "team-a"})
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    path = concept_path("local_files:apps/x.md")
    assert path in change.files and path in change.concepts
    fm = change.concepts[path]
    assert fm.type == "concept"
    assert fm.facets == {"owner": "team-a"}
    assert fm.resources == [doc.anchor]
    assert fm.freshness == NOW
    assert fm.links == []  # no relations declared → no links
    assert change.files[path].startswith("---\n")  # rendered with YAML frontmatter
    # full strict-OKF + §4.4 conformance of synthesized output is proven end-to-end
    # by the pipeline test (Task 8): a Published result means run_validators == [].


def test_dangling_relations_are_dropped():
    doc = _doc("local_files:apps/x.md", relations=["local_files:apps/ghost.md"])
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == []  # ghost not in the bundle → dropped, not dangling


def test_resolvable_sibling_link_survives():
    x = _doc("local_files:apps/x.md", relations=["local_files:apps/y.md"])
    y = _doc("local_files:apps/y.md")
    change = synthesize(
        [x, y], ChangeSet(added=["local_files:apps/x.md", "local_files:apps/y.md"])
    )
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == [concept_path("local_files:apps/y.md")]


def test_nested_structured_value_is_not_a_facet():
    doc = _doc(
        "local_files:apps/x.md", structured={"owner": {"team": "a"}, "env": "prod"}
    )
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.facets == {"env": "prod"}  # nested dropped → law 1 stays well-formed
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — `src/kbforge/synthesize.py`

```python
"""Stub synthesizer: a deterministic CanonicalDocument → ProposedChange map.

No LLM. Real synthesis (grounding contract, token budget) is a later increment;
this stub proves the pipeline wiring and gives the validators real structure to
check. kbforge checks synthesis output either way (spec §5)."""

from __future__ import annotations

import yaml

from kbforge.models import (
    CanonicalDocument,
    ChangeSet,
    ChangeSummary,
    ConceptFrontmatter,
    ProposedChange,
)

_SCALAR = (str, int, float, bool)


def concept_path(doc_id: str) -> str:
    """Deterministic bundle path from a doc_id ("system:native_id")."""
    _, _, native = doc_id.partition(":")
    stem = native.removesuffix(".md").strip("/")
    return f"concepts/{stem}/overview.md"


def _facets(structured: dict) -> dict:
    def ok(v: object) -> bool:
        if isinstance(v, _SCALAR):
            return True
        return isinstance(v, list) and all(isinstance(i, _SCALAR) for i in v)

    return {
        k: v
        for k, v in structured.items()
        if v not in (None, "", [], {}) and ok(v)
    }


def _render(doc: CanonicalDocument, fm: ConceptFrontmatter) -> str:
    front: dict = {
        "type": fm.type,
        "title": doc.title,
        "description": doc.title,  # skeleton: description mirrors title
        "timestamp": fm.freshness.isoformat() if fm.freshness else None,
    }
    front.update(fm.facets)
    front["resource"] = [
        {"system": a.system, "native_id": a.native_id, "url": a.url}
        for a in fm.resources
    ]
    if fm.links:
        front["links"] = fm.links
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n# {doc.title}\n\n{doc.text}\n"


def synthesize(
    changed_docs: list[CanonicalDocument],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
) -> ProposedChange:
    known = {concept_path(d.doc_id) for d in changed_docs} | set(existing_paths)
    files: dict[str, str] = {}
    concepts: dict[str, ConceptFrontmatter] = {}
    summary = ChangeSummary()
    for doc in changed_docs:
        path = concept_path(doc.doc_id)
        links = [concept_path(r) for r in doc.relations]
        fm = ConceptFrontmatter(
            type=str(doc.structured.get("type") or "concept"),
            facets=_facets(doc.structured),
            resources=[doc.anchor],
            links=sorted(p for p in links if p in known),  # drop dangling (law 2)
            freshness=doc.anchor.retrieved_at,
        )
        concepts[path] = fm
        files[path] = _render(doc, fm)
        summary.sources_changed.append(doc.anchor)
    summary.claims_added = sorted(concept_path(x) for x in changeset.added)
    summary.claims_modified = sorted(concept_path(x) for x in changeset.modified)
    summary.claims_removed = sorted(changeset.removed)
    return ProposedChange(
        branch_hint="sync/local-files",
        files=files,
        concepts=concepts,
        summary=summary,
    )
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(synthesize): deterministic stub synthesizer"`

---

### Task 6: Strict-OKF validators and unified `run_validators`

**Files:**
- Modify: `src/kbforge/validate.py` (add; do not touch existing `run_artifact_validators` or `_check_*`)
- Test: `tests/test_strict_okf.py`

**Interfaces:**
- Consumes: `ProposedChange`; existing `run_artifact_validators`, `Failure`, `_basename`, `_RESERVED`.
- Produces: `run_validators(proposal, existing_paths=frozenset()) -> list[Failure]` = strict-OKF over rendered `files` + the four §4.4 laws over `concepts`. Strict required frontmatter keys: `type`, `title`, `description`, `timestamp`.

- [ ] **Step 1: Write the failing test** — `tests/test_strict_okf.py`

```python
from kbforge.models import ConceptFrontmatter, ProposedChange
from kbforge.validate import run_validators

GOOD = """---
type: concept
title: X
description: X
timestamp: 2026-07-19T00:00:00+00:00
---
# X
body
"""

MISSING_DESC = """---
type: concept
title: X
timestamp: 2026-07-19T00:00:00+00:00
---
# X
"""


def _proposal(path, content, concept=None):
    return ProposedChange(
        branch_hint="b",
        files={path: content},
        concepts={path: concept} if concept else {},
    )


def test_rendered_file_missing_required_field_is_reported():
    failures = run_validators(_proposal("concepts/x/overview.md", MISSING_DESC))
    assert any(f.law == "okf-strict" for f in failures)


def test_reserved_files_are_exempt_from_strict_checks():
    failures = run_validators(_proposal("apps/index.md", "listing, no frontmatter"))
    assert [f for f in failures if f.law == "okf-strict"] == []


def test_run_validators_also_runs_artifact_laws():
    # a file present but no concept projection → §4.4 coherence still fires
    failures = run_validators(_proposal("concepts/x/overview.md", GOOD))
    assert any(f.law == "projection-coherence" for f in failures)
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — add to `src/kbforge/validate.py` (new import at top: `import yaml`):

```python
_STRICT_REQUIRED = ("type", "title", "description", "timestamp")


def _parse_frontmatter(content: str) -> dict:
    if not content.startswith("---"):
        return {}
    _, _, rest = content.partition("---")
    front_raw, sep, _ = rest.partition("\n---")
    if not sep:
        return {}
    try:
        data = yaml.safe_load(front_raw)
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _check_strict_okf(proposal: ProposedChange) -> list[Failure]:
    failures: list[Failure] = []
    for path, content in proposal.files.items():
        if _basename(path) in _RESERVED:
            continue
        front = _parse_frontmatter(content)
        for key in _STRICT_REQUIRED:
            value = front.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                failures.append(
                    Failure(
                        path,
                        "okf-strict",
                        f"rendered concept is missing required OKF field {key!r}",
                    )
                )
    return failures


def run_validators(
    proposal: ProposedChange,
    existing_paths: frozenset[str] = frozenset(),
) -> list[Failure]:
    """The full validate stage (§7): strict-OKF checks over the rendered `files`
    plus the four §4.4 agent-facing laws over the `concepts` projection."""
    return _check_strict_okf(proposal) + run_artifact_validators(
        proposal, existing_paths
    )
```

- [ ] **Step 4: Run, verify pass** — run `tests/test_strict_okf.py`, `tests/test_synthesize.py`, and the existing `tests/test_validate.py` (must stay green).
- [ ] **Step 5: Commit** — `git commit -m "feat(validate): strict-OKF checks and unified run_validators"`

---

### Task 7: Dry-run publisher

**Files:**
- Create: `src/kbforge/publishers/__init__.py`, `src/kbforge/publishers/dry_run.py`
- Test: `tests/test_dry_run_publisher.py`

**Interfaces:**
- Consumes: `ProposedChange`, `ConnectorInfo`, `ChangeSummary`; `hookimpl` (hookspecs).
- Produces: `DryRunPublisher` with `kbforge_publisher_info`, `kbforge_publish(change, config) -> str`. `config["out_dir"]` is the output root; writes `change.files` under `<out_dir>/<branch>/`, a `MR_BODY.md`, returns the branch dir path. **Never merges. Idempotent** per branch_hint (re-run overwrites the same dir).

- [ ] **Step 1: Write the failing test** — `tests/test_dry_run_publisher.py`

```python
from pathlib import Path

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.dry_run import DryRunPublisher


def _change():
    return ProposedChange(
        branch_hint="sync/local-files",
        files={"concepts/x/overview.md": "# X\n"},
        summary=ChangeSummary(claims_added=["concepts/x/overview.md"]),
    )


def test_publish_writes_files_and_body(tmp_path: Path):
    out = DryRunPublisher().kbforge_publish(_change(), {"out_dir": str(tmp_path)})
    out_dir = Path(out)
    assert (out_dir / "concepts/x/overview.md").read_text("utf-8") == "# X\n"
    assert (out_dir / "MR_BODY.md").exists()


def test_publish_is_idempotent(tmp_path: Path):
    cfg = {"out_dir": str(tmp_path)}
    a = DryRunPublisher().kbforge_publish(_change(), cfg)
    b = DryRunPublisher().kbforge_publish(_change(), cfg)
    assert a == b  # same branch → same dir, overwritten not duplicated
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — `src/kbforge/publishers/__init__.py` (empty) and `src/kbforge/publishers/dry_run.py`

```python
"""Dry-run publisher: writes the proposal to a local directory instead of opening
an MR. Ships in core (§5.2). Never merges — a real GitHub/GitLab publisher is a
separate plugin."""

from __future__ import annotations

from pathlib import Path

from kbforge.hookspecs import hookimpl
from kbforge.models import ChangeSummary, ConnectorInfo, ProposedChange


def _summary_md(summary: ChangeSummary) -> str:
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
        (out_dir / "MR_BODY.md").write_text(_summary_md(change.summary), "utf-8")
        return str(out_dir)  # a path, not a merge — never merges
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(publishers): dry-run publisher"`

---

### Task 8: The pipeline `run()` loop

**Files:**
- Create: `src/kbforge/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `assert_stability` (canonical); `diff`, `commit` (mirror); `synthesize` and `concept_path` (synthesize); `run_validators` (validate); a bound connector and publisher (duck-typed via the hookimpl method names). `state_dir` holds `cursor-<connector>.json`.
- Produces: `run(connector, publisher, *, config, mirror, state_dir, publish_config) -> NoOp | Aborted | Published`. Result dataclasses `NoOp`, `Aborted(failures)`, `Published(url)`.

- [ ] **Step 1: Write the failing test** — `tests/test_pipeline.py`

```python
from pathlib import Path

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher

DOC = """---
type: application
title: App X
owner: team-a
---
App X does things.
"""


def _dirs(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    return (
        {"path": str(src)},
        str(tmp_path / "mirror"),
        str(tmp_path / "state"),
        {"out_dir": str(tmp_path / "out")},
    )


def test_bootstrap_run_publishes(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(result, Published)
    assert (Path(result.url) / "concepts/x/overview.md").exists()


def test_second_identical_run_is_noop(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    kwargs = dict(
        config=config, mirror=mirror, state_dir=state, publish_config=pub
    )
    first = run(LocalFilesConnector(), DryRunPublisher(), **kwargs)
    assert isinstance(first, Published)
    second = run(LocalFilesConnector(), DryRunPublisher(), **kwargs)
    assert isinstance(second, NoOp)  # mirror committed → no change → no MR


def test_link_to_unchanged_sibling_survives(tmp_path: Path):
    # A links to B; both bootstrapped. Then only A changes. The A→B link must
    # still resolve — B is unchanged-but-present — not be dropped (§4.4 law 2).
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text("---\ntype: application\ntitle: B\n---\nB.\n", "utf-8")
    a_body = "---\ntype: application\ntitle: {t}\nrelations:\n  - b.md\n---\n{x}\n"
    (src / "a.md").write_text(a_body.format(t="A", x="A one"), "utf-8")
    kwargs = dict(
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )
    run(LocalFilesConnector(), DryRunPublisher(), **kwargs)  # bootstrap A and B
    (src / "a.md").write_text(a_body.format(t="A2", x="A two"), "utf-8")
    result = run(LocalFilesConnector(), DryRunPublisher(), **kwargs)  # only A changed
    assert isinstance(result, Published)
    published_a = Path(result.url) / "concepts/a/overview.md"
    assert "concepts/b/overview.md" in published_a.read_text("utf-8")
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement** — `src/kbforge/pipeline.py`

```python
"""The fixed-order pipeline (architecture §7). The order is NOT pluggable; the
no-op and never-auto-merge rules are trust guarantees enforced here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kbforge.canonical import assert_stability
from kbforge.mirror import commit, diff
from kbforge.models import Cursor
from kbforge.synthesize import concept_path, synthesize
from kbforge.validate import Failure, run_validators


@dataclass(frozen=True)
class NoOp:
    """No change detected — no MR opened. Ever."""


@dataclass(frozen=True)
class Aborted:
    """Validation failed — the artifact is non-conformant, so no MR opened."""

    failures: list[Failure]


@dataclass(frozen=True)
class Published:
    url: str


class ConfigError(RuntimeError):
    """A connector rejected its config before any I/O."""


def _cursor_slot(state_dir: Path, connector: str) -> Path:
    return state_dir / f"cursor-{connector}.json"


def _load_cursor(state_dir: Path, connector: str) -> Cursor | None:
    slot = _cursor_slot(state_dir, connector)
    if not slot.exists():
        return None
    return Cursor.model_validate_json(slot.read_text("utf-8"))


def _save_cursor(state_dir: Path, cursor: Cursor) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    slot = _cursor_slot(state_dir, cursor.connector)
    slot.write_text(cursor.model_dump_json(), "utf-8")


def run(
    connector: object,
    publisher: object,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
) -> NoOp | Aborted | Published:
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")

    mirror_path = Path(mirror)
    state_path = Path(state_dir)

    result = connector.kbforge_fetch(config, _load_cursor(state_path, info.name))
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1

    changeset = diff(mirror_path, docs)
    if changeset.is_noop:
        return NoOp()

    changed = set(changeset.added) | set(changeset.modified)
    changed_docs = [d for d in docs if d.doc_id in changed]  # "scope"
    # Existing bundle paths = every fetched doc's concept path, so a link from a
    # changed concept to an unchanged-but-present sibling still resolves (§4.4 law 2)
    # instead of being dropped. (Feed-less full-fetch connector: `docs` is complete.)
    existing = frozenset(concept_path(d.doc_id) for d in docs)
    proposal = synthesize(changed_docs, changeset, existing)

    failures = run_validators(proposal, existing)
    if failures:
        return Aborted(failures=failures)

    url = publisher.kbforge_publish(proposal, publish_config)
    commit(mirror_path, docs)  # advance mirror ONLY after success
    _save_cursor(state_path, result.cursor)
    return Published(url=url)
```

- [ ] **Step 4: Run, verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(pipeline): fixed-order run loop with no-op gate"`

---

### Task 9: Registry and CLI

**Files:**
- Create: `src/kbforge/registry.py`, `src/kbforge/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `pluggy`, `PROJECT`, `ConnectorSpec`, `PublisherSpec` (hookspecs); `LocalFilesConnector`, `DryRunPublisher`; `run` and result types (pipeline).
- Produces: `build_registry() -> pluggy.PluginManager` (registers the in-tree connector + publisher — real Pluggy, one of each; multi-connector `subset_hook_caller` dispatch is deferred). `main(argv=None) -> int` for `python -m kbforge run --source DIR --mirror DIR --out DIR --state DIR`.

- [ ] **Step 1: Write the failing test** — `tests/test_cli.py`

```python
from pathlib import Path

from kbforge.__main__ import main
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


def test_registry_exposes_connector_and_publisher():
    pm = build_registry()
    names = {p.__class__.__name__ for p in pm.get_plugins()}
    assert {"LocalFilesConnector", "DryRunPublisher"} <= names


def test_cli_run_bootstrap(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--source", str(src),
            "--mirror", str(tmp_path / "mirror"),
            "--out", str(tmp_path / "out"),
            "--state", str(tmp_path / "state"),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert (tmp_path / "out" / "sync-local-files" / "concepts/x/overview.md").exists()
```

- [ ] **Step 2: Run it, verify it fails.**

- [ ] **Step 3: Implement registry** — `src/kbforge/registry.py`

```python
"""Plugin registration. Real Pluggy, but scoped to the walking skeleton's single
in-tree connector + publisher. Entry-point discovery and multi-connector
`subset_hook_caller` dispatch (architecture §5.4) are deferred."""

from __future__ import annotations

import pluggy

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.hookspecs import PROJECT, ConnectorSpec, PublisherSpec
from kbforge.publishers.dry_run import DryRunPublisher


def build_registry() -> pluggy.PluginManager:
    pm = pluggy.PluginManager(PROJECT)
    pm.add_hookspecs(ConnectorSpec)
    pm.add_hookspecs(PublisherSpec)
    pm.register(LocalFilesConnector())
    pm.register(DryRunPublisher())
    return pm
```

- [ ] **Step 4: Implement CLI** — `src/kbforge/__main__.py`

```python
"""`python -m kbforge run ...` — the walking-skeleton entry point."""

from __future__ import annotations

import argparse

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import Aborted, NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the pipeline once over a local folder")
    r.add_argument("--source", required=True)
    r.add_argument("--mirror", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--state", required=True)
    args = parser.parse_args(argv)

    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config={"path": args.source},
        mirror=args.mirror,
        state_dir=args.state,
        publish_config={"out_dir": args.out},
    )
    if isinstance(result, Published):
        print(f"Published: {result.url}")
        return 0
    if isinstance(result, NoOp):
        print("NoOp: no change detected; no MR opened.")
        return 0
    if isinstance(result, Aborted):
        print(f"Aborted: {len(result.failures)} validation failure(s):")
        for f in result.failures:
            print(f"  [{f.law}] {f.concept_path}: {f.message}")
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 5: Run, verify pass.**
- [ ] **Step 6: Full-suite check** — `uv run pytest -q` → all green, then confirm the manual smoke path in the test is covered.
- [ ] **Step 7: Commit** — `git commit -m "feat(cli): plugin registry and python -m kbforge run"`

---

## Post-plan verification

After Task 9: `uv run pytest -q` (all tests green, coverage on the new modules), and a manual smoke run:

```bash
mkdir -p /tmp/kbf/src && printf -- '---\ntype: application\ntitle: App X\n---\nApp X.\n' > /tmp/kbf/src/x.md
uv run python -m kbforge run --source /tmp/kbf/src --mirror /tmp/kbf/mirror --out /tmp/kbf/out --state /tmp/kbf/state
# → "Published: .../sync-local-files"; re-run → "NoOp"
```

This proves Bootstrap (first run, `cursor=None`) and Refresh-lite (second run → no-op) end to end, deterministically, with no credentials and no LLM — the walking skeleton of architecture.md §7.
