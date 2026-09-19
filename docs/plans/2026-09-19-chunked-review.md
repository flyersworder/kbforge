# Chunked Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A run whose change exceeds `max_concepts` publishes one reviewable chunk, waits for its review request to close before the next chunk, and `kbforge redo` re-proposes a rejected chunk.

**Architecture:** A new pure module `src/kbforge/chunking.py` owns admission (which changed documents go in this chunk) and the chunk record (what to restore on redo). `pipeline.run` narrows everything after `diff` to the admitted documents, holds the cursor while a backlog remains, and checks an optional new publisher hook `kbforge_open_request` before fetching. `pipeline.redo` restores the record. The CLI gains `--chunking` and a `redo` subcommand.

**Tech Stack:** Python 3.12+, Pydantic v2, pluggy, PyYAML, pytest, uv, ruff, ty.

**Spec:** `docs/design/2026-09-19-chunked-review-design.md` — read it before any task; section numbers below (§4.1 etc.) refer to it.

## Global Constraints

- Without `--chunking`, behaviour is byte-for-byte unchanged: no chunk record, same mirror writes, same cursor writes, same proposals. Every existing test must keep passing unmodified.
- Pipeline order is fixed. Admission is a narrowing between `diff` and `scope`, not a stage, and not pluggable.
- kbforge never merges: `grep -rn 'def .*merge' src/kbforge/publishers/` must stay empty. The new hook is read-only.
- §4.4 emit-side laws are untouched. Links to backlog concepts are dropped by law 2, never kept.
- `normalize` stays pure; nothing here touches connectors.
- Chunking config: `max_concepts` (int, ≥ 1, required), `group_by` (str, optional). `extra="forbid"`.
- Chunk record path: `<state>/chunk-<connector-name>-<_instance_key(config)>.json`.
- Tests never touch the network, except the one `--run-live` test in Task 8.
- Commands: `uv run pytest`, `uv run ruff check`, `uv run ruff format`, `uv run ty check`. `prek` runs ruff + ty on commit.
- Commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Structure

- Create `src/kbforge/chunking.py`: `ChunkingConfig`, `load_chunking`, `admit`, `ChunkRecord`, `owned_paths`, `snapshot`, `restore`, `read_record`, `write_record`. Pure except the record/snapshot file I/O.
- Modify `src/kbforge/hookspecs.py`: optional `kbforge_open_request` hookspec.
- Modify `src/kbforge/publishers/forge.py`: `open_request(client, branch_hint, cfg)`.
- Modify `src/kbforge/publishers/{dry_run,gitlab,github}.py`: implement the hook.
- Modify `src/kbforge/pipeline.py`: `chunking=` on `run`, `Waiting`, `_chunk_slot`, `redo`, `Redone`, `RedoRefused`.
- Modify `src/kbforge/__main__.py`: `--chunking`, `Waiting` output, `redo` subcommand.
- Create `tests/test_chunking.py`, `tests/test_pipeline_chunking.py`, `tests/test_cli_chunking.py`.
- Modify `tests/test_forge.py`, `tests/test_gitlab_publisher.py`, `tests/test_github_publisher.py`, `tests/test_dry_run_publisher.py`, `tests/test_forge_live.py`.
- Docs (Task 9): `docs/architecture.md`, `docs/design/2026-09-19-chunked-review-design.md`, `docs/design/2026-07-19-agentic-ingest-design.md`, `README.md`, `CHANGELOG.md`.

---

### Task 1: Chunking config and admission

**Files:**
- Create: `src/kbforge/chunking.py`
- Test: `tests/test_chunking.py`

**Interfaces:**
- Produces:
  - `class ChunkingConfig(BaseModel)`: `max_concepts: int` (≥1), `group_by: str | None = None`
  - `load_chunking(path: Path | None) -> ChunkingConfig | None`
  - `admit(docs: list[CanonicalDocument], cfg: ChunkingConfig, capacity: int) -> tuple[list[CanonicalDocument], bool]`: the admitted chunk (in admission order) and whether anything was left over.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chunking.py
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig, admit, load_chunking
from kbforge.models import CanonicalDocument, ResourceAnchor


def _doc(native_id: str, group: str | None = None) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system="sys",
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"sys:{native_id}",
        title=native_id,
        text=native_id,
        structured={} if group is None else {"category": group},
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


def _ids(docs: list[CanonicalDocument]) -> list[str]:
    return [d.doc_id for d in docs]


def test_under_the_cap_everything_is_admitted():
    chunk, left = admit([_doc("b"), _doc("a")], ChunkingConfig(max_concepts=5), 5)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is False


def test_without_group_by_the_chunk_is_the_lowest_doc_ids():
    docs = [_doc(n) for n in "edcba"]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=2), 2)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is True


def test_whole_groups_are_packed_in_key_order():
    docs = [
        _doc("a", "runbook"),
        _doc("b", "service"),
        _doc("c", "runbook"),
        _doc("d", "service"),
    ]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=3, group_by="category"), 3)
    # runbook (2) fits; service (2) would overflow 3, so it waits whole.
    assert _ids(chunk) == ["sys:a", "sys:c"]
    assert left is True


def test_a_group_larger_than_the_cap_is_split_by_doc_id():
    docs = [_doc(n, "big") for n in "cba"]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=2, group_by="category"), 2)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is True


def test_documents_without_the_group_key_come_last():
    docs = [_doc("a"), _doc("z", "runbook")]
    chunk, _ = admit(docs, ChunkingConfig(max_concepts=1, group_by="category"), 1)
    assert _ids(chunk) == ["sys:z"]


def test_zero_capacity_admits_nothing():
    chunk, left = admit([_doc("a")], ChunkingConfig(max_concepts=1), 0)
    assert chunk == []
    assert left is True


def test_admission_does_not_depend_on_input_order():
    docs = [_doc(n, g) for n, g in [("a", "x"), ("b", "y"), ("c", "x"), ("d", "y")]]
    cfg = ChunkingConfig(max_concepts=2, group_by="category")
    assert _ids(admit(docs, cfg, 2)[0]) == _ids(admit(docs[::-1], cfg, 2)[0])


def test_config_rejects_an_unknown_key(tmp_path: Path):
    path = tmp_path / "chunking.yaml"
    path.write_text("max_concepts: 3\nmax_concept: 4\n", "utf-8")
    with pytest.raises(ValidationError, match=r"max_concept\s+Extra inputs are not permitted"):
        load_chunking(path)


def test_config_rejects_a_cap_below_one():
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ChunkingConfig(max_concepts=0)


def test_no_path_means_no_chunking():
    assert load_chunking(None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_chunking.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.chunking'`

- [ ] **Step 3: Implement**

```python
# src/kbforge/chunking.py
"""Chunked review for oversized runs (design/2026-09-19-chunked-review-design.md).

Admission decides which changed documents a run publishes now; the chunk record
says what to put back if a reviewer asks for that chunk to be redone. Admission
is pure. The record functions are the only I/O here."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.models import CanonicalDocument


class ChunkingConfig(BaseModel):
    """`extra="forbid"` so a typo'd key is an error rather than no cap at all."""

    model_config = ConfigDict(extra="forbid")

    max_concepts: int = Field(ge=1)
    group_by: str | None = None


def load_chunking(path: Path | None) -> ChunkingConfig | None:
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return ChunkingConfig.model_validate(raw)


def _group_key(doc: CanonicalDocument, group_by: str | None) -> tuple[bool, str]:
    """Missing keys sort last (`True` after `False`), then by the value's text."""
    if group_by is None:
        return (False, "")
    value = doc.structured.get(group_by)
    return (value is None, "" if value is None else str(value))


def admit(
    docs: list[CanonicalDocument], cfg: ChunkingConfig, capacity: int
) -> tuple[list[CanonicalDocument], bool]:
    """The first chunk of `docs` that fits in `capacity`, and whether any were
    left over (§4).

    Whole groups are packed in key order while they fit; packing stops at the
    first group that does not, so a group is never split across chunks unless
    it cannot fit an empty one. A group that cannot is split by doc_id and
    fills the chunk by itself. Sorted throughout, so a re-run of the same
    change admits the same chunk."""
    groups: dict[tuple[bool, str], list[CanonicalDocument]] = {}
    for doc in docs:
        groups.setdefault(_group_key(doc, cfg.group_by), []).append(doc)
    admitted: list[CanonicalDocument] = []
    for key in sorted(groups):
        group = sorted(groups[key], key=lambda d: d.doc_id)
        room = capacity - len(admitted)
        if len(group) <= room:
            admitted += group
            continue
        if not admitted:
            admitted = group[: max(room, 0)]
        break
    return admitted, len(admitted) < len(docs)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_chunking.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/chunking.py tests/test_chunking.py
git commit -m "feat(chunking): admission cap with optional group key

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: The chunk record — snapshot and restore

**Files:**
- Modify: `src/kbforge/chunking.py`
- Test: `tests/test_chunking.py`

**Interfaces:**
- Consumes: `kbforge.mirror.slot_key`, `kbforge.grounding.SIDECAR_DIR`, `kbforge.grounding.FIRST_SEEN_DIR`
- Produces:
  - `class ChunkRecord(BaseModel)`: `branch_hints: list[str]`, `pending: bool`, `admitted: list[str]`, `mirror: dict[str, str | None]` (mirror-relative path → prior content, `None` = absent), `cursor: str | None`
  - `owned_paths(doc_id: str) -> list[str]`: `[slot, sidecar, first_seen]`, mirror-relative
  - `snapshot(mirror: Path, doc_ids: set[str]) -> dict[str, str | None]`
  - `restore(record: ChunkRecord, mirror: Path, cursor_slot: Path) -> None`
  - `read_record(path: Path) -> ChunkRecord | None`, `write_record(path: Path, record: ChunkRecord) -> None`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_chunking.py`, and extend its import to `from kbforge.chunking import ChunkingConfig, ChunkRecord, admit, load_chunking, owned_paths, read_record, restore, snapshot, write_record`)

```python
def test_owned_paths_are_where_the_three_writers_actually_write(tmp_path: Path):
    """Guards drift: if the mirror, the sidecar or the first-seen writer ever
    renames its file, redo would silently restore the wrong path."""
    from kbforge.grounding import record_first_seen, write_sidecar
    from kbforge.mirror import commit

    mirror = tmp_path / "mirror"
    doc = _doc("a")
    commit(mirror, [doc])
    write_sidecar(mirror, doc.doc_id, {})
    record_first_seen(mirror, [doc])
    written = {p.relative_to(mirror).as_posix() for p in mirror.rglob("*.json")}
    assert written == set(owned_paths(doc.doc_id))


def test_snapshot_keeps_present_files_verbatim_and_absent_ones_as_none(tmp_path):
    mirror = tmp_path / "mirror"
    slot, sidecar, first_seen = owned_paths("sys:a")
    mirror.mkdir()
    (mirror / slot).write_text("OLD", "utf-8")
    assert snapshot(mirror, {"sys:a"}) == {slot: "OLD", sidecar: None, first_seen: None}


def test_restore_puts_contents_back_and_removes_what_did_not_exist(tmp_path):
    mirror = tmp_path / "mirror"
    cursor = tmp_path / "state" / "cursor-fake-0.json"
    slot, sidecar, _ = owned_paths("sys:a")
    (mirror / "_grounding").mkdir(parents=True)
    (mirror / slot).write_text("NEW", "utf-8")
    (mirror / sidecar).write_text("NEW", "utf-8")
    cursor.parent.mkdir()
    cursor.write_text("C2", "utf-8")
    record = ChunkRecord(
        branch_hints=["sync/sys"],
        pending=False,
        admitted=["sys:a"],
        mirror={slot: "OLD", sidecar: None},
        cursor=None,
    )
    restore(record, mirror, cursor)
    assert (mirror / slot).read_text("utf-8") == "OLD"
    assert not (mirror / sidecar).exists()
    assert not cursor.exists()


def test_a_record_round_trips(tmp_path: Path):
    path = tmp_path / "state" / "chunk-fake-0.json"
    record = ChunkRecord(
        branch_hints=["sync/sys"],
        pending=True,
        admitted=["sys:a"],
        mirror={"k.json": None},
        cursor="{}",
    )
    write_record(path, record)
    assert read_record(path) == record


def test_no_record_reads_as_none(tmp_path: Path):
    assert read_record(tmp_path / "nope.json") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_chunking.py -q`
Expected: FAIL — `ImportError: cannot import name 'ChunkRecord'`

- [ ] **Step 3: Implement** (append to `src/kbforge/chunking.py`; add imports `from kbforge.grounding import FIRST_SEEN_DIR, SIDECAR_DIR` and `from kbforge.mirror import slot_key`)

```python
class ChunkRecord(BaseModel):
    """The last chunk a connector instance published (§5): enough to wait on
    its review request and to roll it back."""

    model_config = ConfigDict(extra="forbid")

    branch_hints: list[str]
    pending: bool
    """True when that publish left a backlog, so the next run must wait."""
    admitted: list[str]
    mirror: dict[str, str | None]
    """Mirror-relative path -> content before the chunk's commit; None = absent."""
    cursor: str | None
    """The cursor slot's content before the chunk; None = absent."""


def owned_paths(doc_id: str) -> list[str]:
    """Every mirror-relative file a run writes or deletes on behalf of `doc_id`:
    its slot, its grounding sidecar, its first-seen record."""
    name = f"{slot_key(doc_id)}.json"
    return [name, f"{SIDECAR_DIR}/{name}", f"{FIRST_SEEN_DIR}/{name}"]


def _read(path: Path) -> str | None:
    return path.read_text("utf-8") if path.exists() else None


def _put(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, "utf-8")


def snapshot(mirror: Path, doc_ids: set[str]) -> dict[str, str | None]:
    return {
        rel: _read(mirror / rel)
        for doc_id in sorted(doc_ids)
        for rel in owned_paths(doc_id)
    }


def restore(record: ChunkRecord, mirror: Path, cursor_slot: Path) -> None:
    for rel, content in sorted(record.mirror.items()):
        _put(mirror / rel, content)
    _put(cursor_slot, record.cursor)


def read_record(path: Path) -> ChunkRecord | None:
    if not path.exists():
        return None
    return ChunkRecord.model_validate_json(path.read_text("utf-8"))


def write_record(path: Path, record: ChunkRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(), "utf-8")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_chunking.py -q`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/chunking.py tests/test_chunking.py
git commit -m "feat(chunking): chunk record with snapshot and restore

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The `kbforge_open_request` publisher hook

**Files:**
- Modify: `src/kbforge/hookspecs.py` (in `PublisherSpec`, after `kbforge_publish`)
- Modify: `src/kbforge/publishers/forge.py` (after `publish_to_forge`)
- Modify: `src/kbforge/publishers/dry_run.py`, `gitlab.py`, `github.py`
- Test: `tests/test_forge.py`, `tests/test_dry_run_publisher.py`, `tests/test_gitlab_publisher.py`, `tests/test_github_publisher.py`

**Interfaces:**
- Produces: `kbforge_open_request(self, branch_hint: str, config: dict) -> str | None` on every in-tree publisher; `kbforge.publishers.forge.open_request(client: ForgeClient, branch_hint: str, cfg: ForgeConfig) -> str | None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_forge.py` (add `open_request` to the existing `from kbforge.publishers.forge import ...`):

```python
def test_open_request_asks_about_the_hinted_branch():
    client = FakeForgeClient(open_pr="7")
    assert open_request(client, "sync/sys", _cfg()) == "7"
    assert client.calls == [("find_open_pr", "sync/sys")]


def test_open_request_prefers_the_configured_branch_like_publish_does():
    client = FakeForgeClient()
    assert open_request(client, "sync/sys", _cfg(branch="kb/sync")) is None
    assert client.calls == [("find_open_pr", "kb/sync")]
```

In `tests/test_dry_run_publisher.py`:

```python
def test_dry_run_never_has_an_open_request(tmp_path):
    from kbforge.publishers.dry_run import DryRunPublisher

    publisher = DryRunPublisher()
    assert publisher.kbforge_open_request("sync/sys", {"out_dir": str(tmp_path)}) is None
```

In `tests/test_gitlab_publisher.py`:

```python
def test_publisher_open_request_resolves_the_branch_and_asks_the_client(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    seen: list[str] = []

    class _Client:
        def __init__(self, cfg):
            pass

        def find_open_pr(self, branch):
            seen.append(branch)
            return "9"

    monkeypatch.setattr(gitlab_module, "GitLabClient", _Client)
    config = {"repo": "acme/kb", "branch": "kb/sync"}
    assert GitLabPublisher().kbforge_open_request("sync/sys", config) == "9"
    assert seen == ["kb/sync"]
```

In `tests/test_github_publisher.py` (add `from kbforge.publishers import github as github_module` if absent):

```python
def test_publisher_open_request_resolves_the_branch_and_asks_the_client(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    seen: list[str] = []

    class _Client:
        def __init__(self, cfg):
            pass

        def find_open_pr(self, branch):
            seen.append(branch)
            return None

    monkeypatch.setattr(github_module, "GitHubClient", _Client)
    assert GitHubPublisher().kbforge_open_request("sync/sys", {"repo": "acme/kb"}) is None
    assert seen == ["sync/sys"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_forge.py tests/test_dry_run_publisher.py tests/test_gitlab_publisher.py tests/test_github_publisher.py -q`
Expected: FAIL — `ImportError: cannot import name 'open_request'` and `AttributeError: ... has no attribute 'kbforge_open_request'`

- [ ] **Step 3: Implement**

`src/kbforge/hookspecs.py`, inside `PublisherSpec` after `kbforge_publish` — deliberately **not** `@abstractmethod`, so third-party publishers keep working:

```python
    @hookspec
    def kbforge_open_request(self, branch_hint: str, config: dict) -> str | None:
        """The id of the review request open on `branch_hint`'s branch, or None.
        Read-only. Optional: only chunked runs (`--chunking`) and `kbforge redo`
        call it, and both refuse a publisher without it, because appending the
        next chunk to an open request is what chunking exists to prevent."""
```

`src/kbforge/publishers/forge.py`, after `publish_to_forge`:

```python
def open_request(client: ForgeClient, branch_hint: str, cfg: ForgeConfig) -> str | None:
    """The open review request for `branch_hint`, resolved to a branch exactly as
    `publish_to_forge` resolves it, so a configured `branch` wins here too."""
    return client.find_open_pr(cfg.branch or branch_hint)
```

`src/kbforge/publishers/dry_run.py`, in `DryRunPublisher`:

```python
    @hookimpl
    def kbforge_open_request(self, branch_hint: str, config: dict) -> str | None:
        # A directory is never "open": repeated dry runs step through the chunks.
        return None
```

`src/kbforge/publishers/gitlab.py` (import `open_request` from `kbforge.publishers.forge` alongside `publish_to_forge`), in `GitLabPublisher`:

```python
    @hookimpl
    def kbforge_open_request(self, branch_hint: str, config: dict) -> str | None:
        cfg = build_config(config, DEFAULTS)
        return open_request(GitLabClient(cfg), branch_hint, cfg)
```

`src/kbforge/publishers/github.py` likewise, in `GitHubPublisher`:

```python
    @hookimpl
    def kbforge_open_request(self, branch_hint: str, config: dict) -> str | None:
        cfg = build_config(config, DEFAULTS)
        return open_request(GitHubClient(cfg), branch_hint, cfg)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_forge.py tests/test_dry_run_publisher.py tests/test_gitlab_publisher.py tests/test_github_publisher.py tests/test_cli.py -q && grep -rn 'def .*merge' src/kbforge/publishers/`
Expected: all pass; grep prints nothing.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/hookspecs.py src/kbforge/publishers tests/test_forge.py tests/test_dry_run_publisher.py tests/test_gitlab_publisher.py tests/test_github_publisher.py
git commit -m "feat(publishers): optional read-only kbforge_open_request hook

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Admission in the pipeline

**Files:**
- Modify: `src/kbforge/pipeline.py`
- Test: `tests/test_pipeline_chunking.py` (create)

**Interfaces:**
- Consumes: `ChunkingConfig`, `ChunkRecord`, `admit`, `snapshot`, `write_record` (Tasks 1–2)
- Produces: `run(..., chunking: ChunkingConfig | None = None)`; `_chunk_slot(state_dir: Path, connector: str, config: dict) -> Path`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pipeline_chunking.py
"""Chunked review in the pipeline (design/2026-09-19-chunked-review-design.md).
Helpers are local rather than imported from test_pipeline: tests/ is not a
package, so cross-test imports depend on pytest's import mode."""

from datetime import UTC, datetime
from pathlib import Path

from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig, read_record
from kbforge.grounding import GroundingConfig
from kbforge.mirror import load_all
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import Published, _chunk_slot, _cursor_slot, run
from kbforge.synthesize import assemble, concept_path


def _doc(
    native_id: str,
    *,
    system: str = "sys",
    text: str | None = None,
    relations: list[str] | None = None,
    deleted: bool = False,
) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"{system}:{native_id}",
        title=native_id,
        text=text or native_id,
        relations=relations or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _Connector:
    """Fixed docs; records the cursor each fetch was handed, and returns a
    cursor that counts fetches so a held cursor is observable."""

    def __init__(self, docs, name: str = "fake"):
        self.docs = docs
        self.name = name
        self.cursors: list[Cursor | None] = []

    def kbforge_connector_info(self):
        return ConnectorInfo(name=self.name, version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        self.cursors.append(cursor)
        n = 0 if cursor is None else int(cursor.payload.get("n", 0))
        return FetchResult(
            records=[], cursor=Cursor(connector=self.name, payload={"n": n + 1})
        )

    def kbforge_normalize(self, records):
        return self.docs


class _Publisher:
    """Records every change; `open` is what kbforge_open_request reports."""

    def __init__(self, open_request: str | None = None):
        self.open = open_request
        self.changes: list[ProposedChange] = []

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="chunk-test", version="0.1.0", source_system="test")

    def kbforge_publish(self, change, config):
        self.changes.append(change)
        return f"recorded://{len(self.changes)}"

    def kbforge_open_request(self, branch_hint, config):
        return self.open


class _GroundingSynth:
    grounds = True

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset(), grounding=None):
        items = [(d, d.title, d.title, d.text) for d in changed_docs]
        return assemble(items, changeset, existing_paths, grounding=grounding)


def _run(tmp_path, docs, *, cap=None, publisher=None, connector=None, **kw):
    publisher = publisher or _Publisher()
    connector = connector or _Connector(docs)
    connector.docs = docs
    result = run(
        connector,
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        chunking=None if cap is None else ChunkingConfig(max_concepts=cap),
        **kw,
    )
    return result, publisher, connector


def _record(tmp_path, name="fake"):
    return read_record(_chunk_slot(tmp_path / "state", name, {}))


def test_an_oversized_run_publishes_and_commits_only_the_first_chunk(tmp_path):
    result, pub, _ = _run(tmp_path, [_doc("a"), _doc("b"), _doc("c")], cap=2)
    assert isinstance(result, Published)
    assert set(pub.changes[0].files) == {concept_path("sys:a"), concept_path("sys:b")}
    assert pub.changes[0].summary.claims_added == sorted(
        [concept_path("sys:a"), concept_path("sys:b")]
    )
    assert [d.doc_id for d in load_all(tmp_path / "mirror")] == ["sys:a", "sys:b"]
    record = _record(tmp_path)
    assert record is not None and record.pending is True
    assert any("carries 2 of 3" in n for n in pub.changes[0].summary.grounding_notes)


def test_the_cursor_is_held_until_the_final_chunk(tmp_path):
    docs = [_doc("a"), _doc("b"), _doc("c")]
    connector = _Connector(docs)
    _run(tmp_path, docs, cap=2, connector=connector)
    assert not _cursor_slot(tmp_path / "state", "fake", {}).exists()

    _, pub, _ = _run(tmp_path, docs, cap=2, connector=connector)
    assert connector.cursors == [None, None], "the second chunk must re-fetch from the held cursor"
    assert set(pub.changes[0].files) == {concept_path("sys:c")}, "admitted docs were re-proposed"
    assert _cursor_slot(tmp_path / "state", "fake", {}).exists()
    record = _record(tmp_path)
    assert record is not None and record.pending is False


def test_a_link_to_a_backlog_concept_is_dropped_then_restored_on_arrival(tmp_path):
    docs = [_doc("a", relations=["sys:b"]), _doc("b")]
    _, pub1, _ = _run(tmp_path, docs, cap=1)
    assert pub1.changes[0].concepts[concept_path("sys:a")].links == [], (
        "a link to an unpublished backlog concept must not ship"
    )

    _, pub2, _ = _run(tmp_path, docs, cap=1)
    change = pub2.changes[0]
    assert concept_path("sys:a") in change.files, "the referrer was not rebuilt on arrival"
    assert change.concepts[concept_path("sys:a")].links == [concept_path("sys:b")]
    assert any(
        n.startswith(concept_path("sys:a")) and "restore a link" in n
        for n in change.summary.grounding_notes
    )


def test_a_backlog_referrer_of_a_deleted_concept_is_rebuilt_from_its_mirror_copy(tmp_path):
    _run(tmp_path, [_doc("x"), _doc("r", relations=["sys:x"]), _doc("a")])
    docs = [
        _doc("x", deleted=True),
        _doc("r", text="r2", relations=["sys:x"]),  # modified, but backlog
        _doc("a", text="a2"),  # modified, admitted first (a < r)
    ]
    _, pub, _ = _run(tmp_path, docs, cap=1)
    change = pub.changes[0]
    assert change.files_removed == [concept_path("sys:x")]
    path_r = concept_path("sys:r")
    assert path_r in change.files, "a dangling link to the deleted concept would ship"
    assert change.concepts[path_r].links == []
    assert "r2" not in change.files[path_r], "the backlog modification leaked into this chunk"


def test_drift_counts_toward_the_cap_and_waits_its_turn(tmp_path):
    grounding = GroundingConfig(grounding={"sys:a": ["other:t"]})
    synth = _GroundingSynth()
    a = _doc("a")
    _run(tmp_path, [a, _doc("t", system="other")], synthesizer=synth, grounding_config=grounding)
    _run(
        tmp_path,
        [_doc("t", system="other", text="t2")],
        connector=_Connector([], name="other"),
        synthesizer=synth,
        grounding_config=grounding,
    )

    b = _doc("b")
    _, pub1, _ = _run(tmp_path, [a, b], cap=1, synthesizer=synth, grounding_config=grounding)
    assert set(pub1.changes[0].files) == {concept_path("sys:b")}

    _, pub2, _ = _run(tmp_path, [a, b], cap=1, synthesizer=synth, grounding_config=grounding)
    assert set(pub2.changes[0].files) == {concept_path("sys:a")}
    assert any("grounding changed" in n for n in pub2.changes[0].summary.grounding_notes)


def test_without_chunking_no_record_is_written(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")])
    assert _record(tmp_path) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_pipeline_chunking.py -q`
Expected: FAIL — `ImportError: cannot import name '_chunk_slot'`

- [ ] **Step 3: Implement** — edits to `src/kbforge/pipeline.py`, in order:

(a) Imports, after the `kbforge.canonical` import:

```python
from kbforge.chunking import ChunkingConfig, ChunkRecord, admit, snapshot, write_record
```

(b) After `_cursor_slot`:

```python
def _chunk_slot(state_dir: Path, connector: str, config: dict) -> Path:
    """Keyed exactly like the cursor slot, for the same sibling-instance reason."""
    return state_dir / f"chunk-{connector}-{_instance_key(config)}.json"
```

(c) `run` signature: add `chunking: ChunkingConfig | None = None,` after `grounding_config`.

(d) Immediately after `mirror_docs = load_all(mirror_path)` (and its comment block), insert:

```python
    # Chunked review (design/2026-09-19): an oversized change publishes one
    # chunk now and leaves the rest as backlog, which stays visible to `diff`
    # because only admitted documents are committed. Everything below sees the
    # world as it will be once this chunk merges, so `admitted_docs` replaces
    # `docs` wherever a document could reach the bundle or the mirror. Without
    # --chunking the backlog is empty and `admitted_docs` is `docs`.
    changed = set(changeset.added) | set(changeset.modified)
    backlog: set[str] = set()
    if chunking is not None:
        chunk, _ = admit(
            [d for d in docs if d.doc_id in changed], chunking, chunking.max_concepts
        )
        backlog = changed - {d.doc_id for d in chunk}
        changed -= backlog
    admitted_docs = [d for d in docs if d.doc_id not in backlog]
```

(e) Replace `docs` with `admitted_docs` in exactly these places: the `with_first_seen(load_first_seen(mirror_path), docs)` call; `by_id.update({d.doc_id: d for d in docs if not d.deleted})`; the `for doc in docs: if doc.deleted: by_id.pop(...)` loop; the `| {concept_path(d.doc_id) for d in docs if not d.deleted}` term of `existing`; `commit(mirror_path, docs)`; `record_first_seen(mirror_path, docs)`. Leave `systems = {d.anchor.system for d in docs} ...` and `changed_docs = [d for d in docs if d.doc_id in changed]` as they are.

(f) Delete the old line `changed = set(changeset.added) | set(changeset.modified)` (it moved up in (d)); keep `removed_ids` and `changed_docs` lines.

(g) Replace the drift block (`drift: list[str] = []` through `changed_docs += [d for d in candidates if d.doc_id in drift_ids]`) with:

```python
    drift: list[str] = []
    deferred_drift: set[str] = set()
    if scan:
        # `changed | backlog`: a backlog document is rebuilt whole in its own
        # chunk, so rebuilding its stale mirror copy for drift now is waste.
        candidates = _drift_candidates(
            mirror_docs, by_id, systems, changed | backlog, removed_ids
        )
        drift = drifted(
            mirror_path,
            candidates,
            {d.doc_id: [g.doc_id for g in _resolved(d)[0]] for d in candidates},
            hashes,
        )
        # Hoisted: as a comprehension condition this was rebuilt once per
        # candidate, and `candidates` is O(mirror).
        drift_ids = set(drift)
        drifted_docs = [d for d in candidates if d.doc_id in drift_ids]
        if chunking is not None:
            # Drift counts toward the cap: each is a concept the reviewer
            # reads. A deferred one writes no sidecar, so the next run finds
            # the same drift again.
            room = max(chunking.max_concepts - len(changed), 0)
            drifted_docs, _ = admit(drifted_docs, chunking, room)
            deferred_drift = drift_ids - {d.doc_id for d in drifted_docs}
            drift = [x for x in drift if x not in deferred_drift]
        changed_docs += drifted_docs
```

(h) In the deletion-referrers comment block, append one sentence: `# Under chunking, `changed` is the admitted set, so a backlog document that links to a removed concept is rebuilt here from its mirror copy (§4.1).` Code unchanged.

(i) After the deletion-referrers `if removed_ids:` block, before the dedupe, insert:

```python
    # Arrival referrers (§4.1), chunked runs only: a published concept whose
    # relation names a document this chunk adds lost that link under law 2
    # when it was built, because the target was still backlog. Rebuilt so the
    # link comes back. Scoped and filtered exactly like `referrers`. Unchunked
    # runs keep today's behaviour (spec §10).
    arrivals: list[CanonicalDocument] = []
    if chunking is not None:
        arrived = set(changeset.added) - backlog
        if arrived:
            arrivals = [
                d
                for d in mirror_docs
                if d.anchor.system in systems
                and d.doc_id not in changed
                and d.doc_id not in removed_ids
                and arrived.intersection(d.relations)
            ]
            changed_docs += arrivals
```

(j) Before the `if grounds:` synthesize call, insert, and pass `chunk_changeset` instead of `changeset` to **both** `synthesize(...)` calls:

```python
    # Summary claims come from the changeset, so a chunk must not claim its
    # backlog. Identical to `changeset` when nothing is backlog.
    chunk_changeset = changeset.model_copy(
        update={
            "added": [x for x in changeset.added if x not in backlog],
            "modified": [x for x in changeset.modified if x not in backlog],
        }
    )
```

(k) After the `for doc_id in drift:` note loop, before `failures = run_validators(...)`:

```python
    for doc in arrivals:
        path = concept_path(doc.doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized to restore a link to a concept added in "
                "this chunk; its own source is unchanged"
            )

    pending = bool(backlog or deferred_drift)
    if pending:
        carried = len(changed) + len(drift)
        proposal.summary.grounding_notes.append(
            f"chunked review: this request carries {carried} of "
            f"{carried + len(backlog) + len(deferred_drift)} changed concepts; "
            "the rest follow once it is merged or closed"
        )
```

(l) Replace from `url = publisher.kbforge_publish(proposal, publish_config)` through `commit(...)` with:

```python
    url = publisher.kbforge_publish(proposal, publish_config)
    cursor_slot = _cursor_slot(state_path, info.name, config)
    touched: set[str] = set()
    prior_mirror: dict[str, str | None] = {}
    prior_cursor: str | None = None
    if chunking is not None:
        # Captured before the commit: everything redo must put back (§5).
        touched = (
            changed
            | removed_ids
            | set(drift)
            | {d.doc_id for d in referrers + arrivals}
        )
        prior_mirror = snapshot(mirror_path, touched)
        prior_cursor = cursor_slot.read_text("utf-8") if cursor_slot.exists() else None
    commit(mirror_path, admitted_docs)  # advance mirror ONLY after success
```

(m) Replace the final `_save_cursor(state_path, result.cursor, systems, config)` with:

```python
    # Held while a backlog remains: the next chunk re-fetches from the same
    # cursor, which §4.2's at-least-once replay makes harmless (§4.2 of the
    # design note).
    if not pending:
        _save_cursor(state_path, result.cursor, systems, config)
    if chunking is not None:
        write_record(
            _chunk_slot(state_path, info.name, config),
            ChunkRecord(
                branch_hints=[proposal.branch_hint],
                pending=pending,
                admitted=sorted(touched),
                mirror=prior_mirror,
                cursor=prior_cursor,
            ),
        )
```

- [ ] **Step 4: Run to verify pass, and that nothing else moved**

Run: `uv run pytest -q && uv run ruff check && uv run ty check`
Expected: all pass, including every pre-existing test.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline_chunking.py
git commit -m "feat(pipeline): admit one chunk of an oversized change

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Waiting between chunks

**Files:**
- Modify: `src/kbforge/pipeline.py`
- Test: `tests/test_pipeline_chunking.py`

**Interfaces:**
- Consumes: `read_record` (Task 2), `kbforge_open_request` (Task 3), `_chunk_slot` (Task 4)
- Produces: `@dataclass(frozen=True) class Waiting: request: str; branch_hint: str`; `run(...) -> NoOp | Aborted | Published | Waiting`; `ConfigError` for a hookless publisher under chunking.

- [ ] **Step 1: Write the failing tests** (append; extend imports with `import pytest` and `from kbforge.pipeline import ConfigError, Waiting`)

```python
def test_an_open_chunk_request_makes_the_next_run_wait_before_fetching(tmp_path):
    docs = [_doc("a"), _doc("b")]
    connector = _Connector(docs)
    publisher = _Publisher()
    _run(tmp_path, docs, cap=1, connector=connector, publisher=publisher)

    publisher.open = "7"
    result, _, _ = _run(tmp_path, docs, cap=1, connector=connector, publisher=publisher)
    assert result == Waiting(request="7", branch_hint="sync/sys")
    assert len(connector.cursors) == 1, "a waiting run must not fetch"
    assert len(publisher.changes) == 1, "a waiting run must not publish"


def test_a_merged_or_closed_request_releases_the_next_chunk(tmp_path):
    docs = [_doc("a"), _doc("b")]
    publisher = _Publisher()
    _run(tmp_path, docs, cap=1, publisher=publisher)
    publisher.open = None
    result, _, _ = _run(tmp_path, docs, cap=1, publisher=publisher)
    assert isinstance(result, Published)
    assert set(publisher.changes[1].files) == {concept_path("sys:b")}


def test_a_final_chunk_does_not_make_later_runs_wait(tmp_path):
    publisher = _Publisher()
    _run(tmp_path, [_doc("a")], cap=5, publisher=publisher)
    publisher.open = "7"
    result, _, _ = _run(tmp_path, [_doc("a", text="a2")], cap=5, publisher=publisher)
    assert isinstance(result, Published), "small follow-ups append as they do today"


def test_a_publisher_without_the_hook_is_refused_under_chunking(tmp_path):
    class _Hookless:
        def kbforge_publisher_info(self):
            return ConnectorInfo(name="hookless", version="0", source_system="t")

        def kbforge_publish(self, change, config):
            raise AssertionError("must be refused before publishing")

    with pytest.raises(ConfigError, match="hookless: --chunking needs a publisher that implements kbforge_open_request"):
        _run(tmp_path, [_doc("a")], cap=1, publisher=_Hookless())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_pipeline_chunking.py -q`
Expected: FAIL — `ImportError: cannot import name 'Waiting'`

- [ ] **Step 3: Implement**

Add `read_record` to the `kbforge.chunking` import. After `class Published`:

```python
@dataclass(frozen=True)
class Waiting:
    """The last chunk's review request is still open, so this run did nothing:
    nothing fetched, nothing synthesized, no review request touched."""

    request: str
    branch_hint: str
```

Change `run`'s return annotation to `-> NoOp | Aborted | Published | Waiting`. Immediately after the `if problems: raise ConfigError(...)` block, insert:

```python
    # Before the fetch, so a waiting run costs one read-only forge call (§6).
    open_request = getattr(publisher, "kbforge_open_request", None)
    if chunking is not None:
        if open_request is None:
            raise ConfigError(
                f"{publisher.kbforge_publisher_info().name}: --chunking needs a "
                "publisher that implements kbforge_open_request, to wait between "
                "chunks; this one does not"
            )
        record = read_record(_chunk_slot(Path(state_dir), info.name, config))
        if record is not None and record.pending:
            for hint in record.branch_hints:
                request = open_request(hint, publish_config)
                if request is not None:
                    return Waiting(request=request, branch_hint=hint)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q && uv run ty check`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline_chunking.py
git commit -m "feat(pipeline): wait for an open chunk request before the next chunk

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `redo`

**Files:**
- Modify: `src/kbforge/pipeline.py`
- Test: `tests/test_pipeline_chunking.py`

**Interfaces:**
- Consumes: `read_record`, `restore` (Task 2); `_chunk_slot`, `_cursor_slot`
- Produces: `class RedoRefused(RuntimeError)`; `@dataclass(frozen=True) class Redone: admitted: list[str]`; `redo(connector: ConnectorProtocol, publisher: PublisherProtocol, *, config: dict, mirror: str, state_dir: str, publish_config: dict) -> Redone`

- [ ] **Step 1: Write the failing tests** (append; extend imports with `RedoRefused, Redone, redo`)

```python
def _tree(*roots: Path) -> dict[str, bytes]:
    return {
        f"{root.name}/{p.relative_to(root).as_posix()}": p.read_bytes()
        for root in roots
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _redo(tmp_path, publisher=None, name="fake"):
    return redo(
        _Connector([], name=name),
        publisher or _Publisher(),
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
    )


def test_redo_restores_mirror_first_seen_and_cursor_byte_for_byte(tmp_path):
    connector = _Connector([])
    _run(tmp_path, [_doc("a")], connector=connector)  # unchunked baseline
    before = _tree(tmp_path / "mirror", tmp_path / "state")

    _run(tmp_path, [_doc("a", text="a2"), _doc("b")], cap=5, connector=connector)
    assert _tree(tmp_path / "mirror", tmp_path / "state") != before

    result = _redo(tmp_path)
    assert result == Redone(admitted=["sys:a", "sys:b"])
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before


def test_after_redo_the_next_run_proposes_the_same_chunk_again(tmp_path):
    docs = [_doc("a"), _doc("b")]
    _, pub1, _ = _run(tmp_path, docs, cap=1)
    _redo(tmp_path)
    _, pub2, _ = _run(tmp_path, docs, cap=1)
    assert set(pub2.changes[0].files) == set(pub1.changes[0].files)


def test_redo_refuses_without_a_record(tmp_path):
    with pytest.raises(RedoRefused, match="fake: no chunk to redo"):
        _redo(tmp_path)


def test_redo_refuses_while_the_request_is_open_and_touches_nothing(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")], cap=1)
    before = _tree(tmp_path / "mirror", tmp_path / "state")
    with pytest.raises(RedoRefused, match="review request 7 on sync/sys is still open; close it first"):
        _redo(tmp_path, publisher=_Publisher(open_request="7"))
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_pipeline_chunking.py -q`
Expected: FAIL — `ImportError: cannot import name 'RedoRefused'`

- [ ] **Step 3: Implement**

Add `restore` to the `kbforge.chunking` import. After `class ConfigError`:

```python
class RedoRefused(RuntimeError):
    """`kbforge redo` found nothing it may safely roll back."""


@dataclass(frozen=True)
class Redone:
    admitted: list[str]
```

At the end of `pipeline.py`:

```python
def redo(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
) -> Redone:
    """Roll the last chunk out of the mirror so the next run proposes it again
    (design/2026-09-19 §7). One level deep: waiting guarantees every earlier
    chunk was merged or closed before this one was synthesized. Closing a
    request still means discard; this is the only way to re-propose."""
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")
    open_request = getattr(publisher, "kbforge_open_request", None)
    if open_request is None:
        raise ConfigError(
            f"{publisher.kbforge_publisher_info().name}: redo needs a publisher "
            "that implements kbforge_open_request; this one does not"
        )
    state_path = Path(state_dir)
    slot = _chunk_slot(state_path, info.name, config)
    record = read_record(slot)
    if record is None:
        raise RedoRefused(
            f"{info.name}: no chunk to redo (no chunk record in {state_path}; "
            "only a run with --chunking writes one)"
        )
    for hint in record.branch_hints:
        request = open_request(hint, publish_config)
        if request is not None:
            raise RedoRefused(
                f"review request {request} on {hint} is still open; close it "
                "first, or the redone chunk would be appended to it"
            )
    restore(record, Path(mirror), _cursor_slot(state_path, info.name, config))
    slot.unlink()
    return Redone(admitted=record.admitted)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q && uv run ty check`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline_chunking.py
git commit -m "feat(pipeline): redo rolls the last chunk out of the mirror

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: CLI — `--chunking`, `Waiting`, `redo`

**Files:**
- Modify: `src/kbforge/__main__.py`
- Test: `tests/test_cli_chunking.py` (create)

**Interfaces:**
- Consumes: `load_chunking` (Task 1); `Waiting` (Task 5); `redo`, `Redone`, `RedoRefused` (Task 6)
- Produces: `kbforge run ... --chunking PATH`; `kbforge redo --connector ... [--set ...] [--publisher ...] [--publish-set ...] --mirror ... --out ... --state ...`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_chunking.py
from pathlib import Path

from kbforge.__main__ import main


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror", str(tmp_path / "mirror"),
        "--out", str(tmp_path / "out"),
        "--state", str(tmp_path / "state"),
    ]


def _source(tmp_path: Path, names: list[str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for n in names:
        (src / f"{n}.md").write_text(f"---\ntype: application\ntitle: {n}\n---\n{n}.\n", "utf-8")
    return src


def _concepts(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "out").rglob("*.md") if p.name != "MR_BODY.md"]


def _chunking(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chunking.yaml"
    path.write_text(text, "utf-8")
    return path


def test_run_with_chunking_publishes_one_chunk_per_run(tmp_path, capsys):
    src = _source(tmp_path, ["a", "b"])
    cfg = _chunking(tmp_path, "max_concepts: 1\n")
    args = ["run", "--connector", "local_files", "--set", f"path={src}",
            "--chunking", str(cfg), *_plumbing(tmp_path)]
    assert main(args) == 0
    assert len(_concepts(tmp_path)) == 1
    assert main(args) == 0
    assert len(_concepts(tmp_path)) == 2
    assert main(args) == 0
    assert "NoOp" in capsys.readouterr().out


def test_a_bad_chunking_file_exits_2(tmp_path, capsys):
    src = _source(tmp_path, ["a"])
    cfg = _chunking(tmp_path, "max_concepts: 0\n")
    code = main(["run", "--connector", "local_files", "--set", f"path={src}",
                 "--chunking", str(cfg), *_plumbing(tmp_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert f"chunking config {cfg}:" in out
    assert "greater than or equal to 1" in out


def test_redo_reproposes_the_last_chunk(tmp_path, capsys):
    src = _source(tmp_path, ["a", "b"])
    cfg = _chunking(tmp_path, "max_concepts: 1\n")
    run_args = ["run", "--connector", "local_files", "--set", f"path={src}",
                "--chunking", str(cfg), *_plumbing(tmp_path)]
    mirror = tmp_path / "mirror"
    assert main(run_args) == 0
    assert len(list(mirror.glob("*.json"))) == 1

    redo_args = ["redo", "--connector", "local_files", "--set", f"path={src}", *_plumbing(tmp_path)]
    assert main(redo_args) == 0
    assert "Redone: 1 document(s) will be proposed again on the next run." in capsys.readouterr().out
    assert list(mirror.glob("*.json")) == [], "redo must roll the chunk out of the mirror"

    assert main(run_args) == 0
    assert "Published" in capsys.readouterr().out
    assert len(list(mirror.glob("*.json"))) == 1


def test_redo_without_a_record_exits_1(tmp_path, capsys):
    src = _source(tmp_path, ["a"])
    code = main(["redo", "--connector", "local_files", "--set", f"path={src}", *_plumbing(tmp_path)])
    assert code == 1
    assert "Redo refused: local_files: no chunk to redo" in capsys.readouterr().out
```

Check before writing Step 3: `uv run python -c "from kbforge.connectors.local_files import LocalFilesConnector as C; print(C().kbforge_connector_info().name)"` must print `local_files`; if not, use the printed name in the last assertion.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli_chunking.py -q`
Expected: FAIL — argparse `unrecognized arguments: --chunking` / `invalid choice: 'redo'` (SystemExit 2).

- [ ] **Step 3: Implement** — edits to `src/kbforge/__main__.py`:

(a) Imports: add `from kbforge.chunking import load_chunking`; extend the `kbforge.pipeline` import with `RedoRefused, Waiting, redo`.

(b) Shared arguments. Add above `main`:

```python
def _source_args(p: argparse.ArgumentParser) -> None:
    """What `run` and `redo` both need to find the connector instance's state
    and ask the publisher about open review requests."""
    p.add_argument("--connector", required=True)
    p.add_argument(
        "--set",
        action="append",
        default=[],
        dest="settings",
        metavar="KEY=VALUE",
        help="connector config (repeatable); values are YAML-typed",
    )
    p.add_argument(
        "--publisher",
        default="dry-run",
        help="publisher name (default: dry-run); see `kbforge list`",
    )
    p.add_argument(
        "--publish-set",
        action="append",
        default=[],
        dest="publish_settings",
        metavar="KEY=VALUE",
        help="publisher config (repeatable); values are YAML-typed",
    )
    p.add_argument("--mirror", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--state", required=True)
```

In `main`, replace the `run` parser's `--connector`, `--set`, `--publisher`, `--publish-set`, `--mirror`, `--out`, `--state` arguments with one `_source_args(r)` call (keep `--synthesizer`, `--llm-set`, `--grounding`), and add after `--grounding`:

```python
    r.add_argument(
        "--chunking",
        default=None,
        metavar="PATH",
        help="chunked review config (YAML: max_concepts, group_by); "
        "see docs/architecture.md §7.2",
    )
    rd = sub.add_parser(
        "redo", help="roll the last chunk back so the next run proposes it again"
    )
    _source_args(rd)
```

(c) Directly after the `publish_problems` check (`return 2`), insert:

```python
    if args.cmd == "redo":
        try:
            redone = redo(
                connectors[args.connector],
                publishers[args.publisher],
                config=config,
                mirror=args.mirror,
                state_dir=args.state,
                publish_config=publish_config,
            )
        except ConfigError as exc:
            print(str(exc))
            return 2
        except RedoRefused as exc:
            print(f"Redo refused: {exc}")
            return 1
        except PublishError as exc:
            print(f"Publish failed: {exc}")
            return 1
        print(
            f"Redone: {len(redone.admitted)} document(s) will be proposed again "
            "on the next run."
        )
        return 0
```

(d) After the grounding-rules-inactive warning, before `try: result = run(...)`:

```python
    try:
        chunking = load_chunking(Path(args.chunking) if args.chunking else None)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        # Same four operator mistakes, same handling, as --grounding above.
        print(f"chunking config {args.chunking}: {exc}")
        return 2
```

Pass `chunking=chunking,` in the `run(...)` call.

(e) In the result dispatch, before `return 2`:

```python
    if isinstance(result, Waiting):
        print(
            f"Waiting: review request {result.request} on {result.branch_hint} is "
            "still open; the next chunk follows once it is merged or closed."
        )
        return 0
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q && uv run ruff check && uv run ty check`
Expected: all pass (including `tests/test_cli.py` and `tests/test_cli_publisher.py` unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/__main__.py tests/test_cli_chunking.py
git commit -m "feat(cli): --chunking and kbforge redo

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Verify the gates — mutation checks and a live forge run

**Files:**
- Modify: `tests/test_forge_live.py`
- No source changes survive this task except the live test.

- [ ] **Step 1: Mutation-check every gate.** For each row: apply the mutation **in place** in `src/kbforge/pipeline.py` (or `chunking.py`), run the named test, confirm it FAILS with the named message, then `git checkout -- src/`. Never copy the repo to mutate it (CLAUDE.md: a copy's `.venv` resolves to the original source).

| Mutation | Test that must fail | Message it must fail with |
|---|---|---|
| (m): always call `_save_cursor` | `test_the_cursor_is_held_until_the_final_chunk` | `must re-fetch from the held cursor` |
| (l): `commit(mirror_path, docs)` | `test_an_oversized_run_publishes_and_commits_only_the_first_chunk` | mirror list mismatch `['sys:a', 'sys:b', 'sys:c']` |
| (e): `existing` built from `docs` | `test_a_link_to_a_backlog_concept_is_dropped_then_restored_on_arrival` | `must not ship` |
| (i): delete the arrivals block | same test | `was not rebuilt on arrival` |
| (d): `changed` not narrowed (drop `changed -= backlog`) | `test_a_backlog_referrer_of_a_deleted_concept_is_rebuilt_from_its_mirror_copy` | `dangling link to the deleted concept would ship` or `leaked into this chunk` |
| (g): no drift admission | `test_drift_counts_toward_the_cap_and_waits_its_turn` | files mismatch in the first chunk |
| Task 5: delete the `Waiting` loop | `test_an_open_chunk_request_makes_the_next_run_wait_before_fetching` | `Waiting(...)` mismatch |
| Task 6: `restore` skips the cursor | `test_redo_restores_mirror_first_seen_and_cursor_byte_for_byte` | tree mismatch naming `state/cursor-` |
| `admit`: `sorted(groups)` → `groups` | `test_admission_does_not_depend_on_input_order` | list mismatch |

If a test passes under its mutation, the test is wrong: fix the test, not the mutation, and re-run the row.

- [ ] **Step 2: Write the live test.** Append to `tests/test_forge_live.py` (GitLab only — the scratch repo in `KBFORGE_LIVE_GITLAB_REPO`; reuse `_require`, `_gl_open_mrs`, `_gl_merge`, `_gl_tree`, `_cli`, `RUN_ID`):

```python
@pytest.mark.live
def test_gitlab_chunks_wait_for_merge_and_redo_reproposes(tmp_path):
    """The whole chunk loop against a real forge: chunk 1 opens a request, the
    next run waits, merging releases chunk 2, closing plus redo re-proposes it."""
    from kbforge.chunking import ChunkingConfig
    from kbforge.connectors.local_files import LocalFilesConnector
    from kbforge.pipeline import Published, Waiting, redo, run

    repo = _require("KBFORGE_LIVE_GITLAB_REPO")
    _require("GITLAB_TOKEN")
    branch = f"sync/live-{RUN_ID}-chunks"
    base_path = f"live/{RUN_ID}-chunks"
    src = tmp_path / "src"
    src.mkdir()
    for n in ["a", "b", "c", "d"]:
        (src / f"{n}.md").write_text(f"---\ntype: application\ntitle: {n}\n---\n{n}.\n", "utf-8")
    common = dict(
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"repo": repo, "base_path": base_path, "branch": branch},
    )
    connector, publisher = LocalFilesConnector(), GitLabPublisher()
    chunking = ChunkingConfig(max_concepts=2)

    def concept_files() -> set[str]:
        prefix = f"{base_path}/concepts/"
        return {p for p in _gl_tree(repo, branch) if p.startswith(prefix)}

    assert isinstance(run(connector, publisher, chunking=chunking, **common), Published)
    assert len(concept_files()) == 2
    [mr] = _gl_open_mrs(repo, branch)

    waiting = run(connector, publisher, chunking=chunking, **common)
    assert isinstance(waiting, Waiting), f"expected Waiting, got {waiting!r}"
    assert waiting.request == str(mr["iid"])

    _gl_merge(repo, mr["iid"])
    assert isinstance(run(connector, publisher, chunking=chunking, **common), Published)
    [mr2] = _gl_open_mrs(repo, branch)
    chunk2 = concept_files()

    project = quote(repo, safe="")
    _cli("glab", "api", "-X", "PUT",
         f"projects/{project}/merge_requests/{mr2['iid']}", "-f", "state_event=close")
    redo(connector, publisher, **common)
    assert isinstance(run(connector, publisher, chunking=chunking, **common), Published)
    [mr3] = _gl_open_mrs(repo, branch)
    assert mr3["iid"] != mr2["iid"], "redo must open a fresh request"
    assert concept_files() == chunk2, "the redone chunk must carry the same concepts"
```

- [ ] **Step 3: Run it live** (credentials per memory `kbforge-live-forge-testing`: `GITLAB_TOKEN` is the PAT from `.env`, never `glab config get token`):

Run: `set -a; source .env; set +a; uv run pytest tests/test_forge_live.py -k chunks --run-live -v`
Expected: PASS. If it fails, debug the pipeline, not the test (superpowers:systematic-debugging).

- [ ] **Step 4: Commit**

```bash
git add tests/test_forge_live.py
git commit -m "test(live): chunk loop against a real GitLab

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Docs and changelog

**Files:**
- Modify: `docs/architecture.md` (new `### 7.2 Chunked review` after §7.1, before `## 8.`; one sentence in §5.2 after the "Closing a kbforge review request without merging therefore discards its contents for good." paragraph)
- Modify: `docs/design/2026-09-19-chunked-review-design.md` (frontmatter `status`)
- Modify: `docs/design/2026-07-19-agentic-ingest-design.md` §9 (two bullets)
- Modify: `README.md` (under `## Quickstart`, after the existing run examples)
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

- [ ] **Step 1: architecture.md §7.2** — write this section (it states what the code does; rationale stays in the design note):

```markdown
### 7.2 Chunked review

`kbforge run --chunking <file>` (`max_concepts`, optional `group_by`) caps how
many concepts one review request carries. When a run's added, modified and
drifted documents exceed the cap, `run` admits one chunk (whole `group_by`
groups in key order, split by `doc_id` only when one group exceeds the cap),
synthesizes and publishes only that, and commits only that to the mirror; the
rest stays visible to `diff`. Removals are always admitted. Everything past
admission sees the world as it will be once the chunk merges: a link to a
backlog concept is dropped under §4.4 law 2, and restored when its target is
admitted by rebuilding the published referrer. The cursor is held until the
final chunk. While a non-final chunk's request is open, `run` returns
`Waiting` before fetching, using the publisher's optional read-only
`kbforge_open_request` hook; a publisher without it is refused under
`--chunking`. `<state>/chunk-<connector>-<digest>.json` records the last chunk,
and `kbforge redo` restores the mirror files and cursor it recorded, so a
closed request can be re-proposed. Rationale:
[`design/2026-09-19-chunked-review-design.md`](design/2026-09-19-chunked-review-design.md).
```

In §5.2 append: `` `kbforge redo` (§7.2) is the one exception: it rolls the last chunk of a chunked run back so the next run re-proposes it. ``

- [ ] **Step 2: Design notes.** In the chunked-review note set `status: shipped — unreleased; folded into architecture.md §7.2; this note keeps the rationale and §10`. In the agentic-ingest note §9, append to the **Partition function for chunking** and **Bootstrap review posture as a first-class flow** bullets: ` Resolved by [2026-09-19-chunked-review-design.md](2026-09-19-chunked-review-design.md).`

- [ ] **Step 3: README.** Add after the Quickstart run examples:

```markdown
**Large first runs.** A cold start, a new source, or a bulk upstream edit can
produce hundreds of concepts in one review request. `--chunking chunking.yaml`
(`max_concepts: 40`, optional `group_by: <field>`) publishes one chunk at a
time and waits for each request to be merged or closed before the next. To
redo a chunk after fixing the taxonomy or exemplars, close its request and run
`kbforge redo` with the same `--connector`, `--set`, `--publisher`,
`--mirror` and `--state`. See `docs/architecture.md` §7.2.
```

- [ ] **Step 4: CHANGELOG** under `## [Unreleased]`:

```markdown
### Added

- Chunked review: `kbforge run --chunking <file>` splits a change larger than
  `max_concepts` into one review request per chunk (optional `group_by` keeps
  groups together), holds the cursor until the final chunk, and returns
  `Waiting` while a chunk's request is open. `kbforge redo` rolls the last chunk
  back so a closed request can be re-proposed. Publishers gain an optional
  read-only `kbforge_open_request` hook (implemented by `dry-run`, `github`,
  `gitlab`); third-party publishers without it keep working, but cannot be used
  with `--chunking`.
```

- [ ] **Step 5: Full verification and commit**

Run: `uv run pytest -q && uv run ruff check && uv run ruff format --check && uv run ty check && grep -rn 'def .*merge' src/kbforge/publishers/`
Expected: all pass; grep prints nothing.

```bash
git add docs README.md CHANGELOG.md
git commit -m "docs: chunked review in architecture §7.2, README and changelog

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```
