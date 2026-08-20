# Cross-Source Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let one concept keep exactly one owning document and one path while being grounded in, and citing, related documents from other systems of record.

**Architecture:** A new `kbforge/grounding.py` owns every grounding concern — config, resolution, the mirror sidecar, and drift detection — as pure functions over data the pipeline already has. The pipeline resolves grounding and hands *resolved documents* to the synthesizer, which therefore never decides what counts as a source. Staleness is derived by comparing a sidecar's recorded hashes against the shared mirror's current ones, so no run ever writes into another connector's state.

**Tech Stack:** Python 3.12+, Pydantic v2, pytest, ruff + ty via prek, uv.

**Spec:** `docs/design/2026-08-20-cross-source-grounding-design.md` — read it before Task 1. The plan argues from it; where they disagree, the spec wins and the disagreement is a plan defect to report.

## Global Constraints

- **Every grounding id is a fully qualified `doc_id`** (`system:native_id`), in `grounded_by` and in the map alike. No bare form is accepted anywhere. Spec §2.1.
- **Resolution happens in the pipeline, never in a synthesizer.** A synthesizer receives resolved `CanonicalDocument`s. Spec §6.
- **Unresolvable is never fatal** — neither a map key nor a value. Drop it and append a grounding note. Only *shape* errors (malformed YAML, unqualified id, non-positive cap) exit 2. Spec §2.2, §3.
- **Drift rule 3 compares sets AFTER resolution.** Comparing declared sets makes an unresolvable id re-synthesize forever. Spec §4.
- **The sidecar is deleted, not skipped**, when the grounding set is empty and when the owner is tombstoned. Spec §4.
- **`grounds` is read as `getattr(synthesizer, "grounds", False)`** and also gates whether `grounding=` is passed at all — the test doubles in `tests/test_pipeline.py` are duck-typed and have no such parameter. Spec §7.
- **The owning anchor is `sources[0]`.** Spec §6.
- **Deduplicate by resource string** (`anchor.url or f"{system}:{native_id}"`), including against the owning anchor. Spec §3.
- **`normalize` stays pure** — no connector learns that another system exists. The map is applied in the pipeline and never merged into `docs`.
- Default cap `max_grounding_docs = 5`.
- `uv run pytest` must never touch the network.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/kbforge/grounding.py` **(new)** | Config model + loader, shape validation, declared-id collection, resolution, sidecar read/write/delete, drift detection. All pure except the sidecar's four filesystem calls. |
| `src/kbforge/models.py` | `CanonicalDocument.grounded_by`; a shared `resource_key()` helper. |
| `src/kbforge/mirror.py` | Expose `slot_key()` so the sidecar hashes `doc_id` exactly as the mirror does. |
| `src/kbforge/validate.py` | `_expected_resources` calls the shared `resource_key()` instead of inlining it. |
| `src/kbforge/connectors/local_files.py` | Read a `grounded_by:` frontmatter list; reserve the key. |
| `src/kbforge/synthesize.py` | `assemble(..., grounding=)` emits multi-source; protocol gains `grounds` and `grounding=`. |
| `src/kbforge/llm_synthesizer.py` | `grounds = True`; grounding text in the prompt; pass through to `assemble`. |
| `src/kbforge/pipeline.py` | Wiring: scan gate, drift, dedup, resolution, sidecar lifecycle. |
| `src/kbforge/__main__.py` | `--grounding PATH`. |
| `tests/test_grounding.py` **(new)** | Units for resolution, sidecar, drift. |

---

### Task 1: `grounded_by` on the document and in `local_files`

**Files:**
- Modify: `src/kbforge/models.py:124`
- Modify: `src/kbforge/connectors/local_files.py:32-44` and `:164-168`
- Test: `tests/test_models.py`, `tests/test_local_files_connector.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `CanonicalDocument.grounded_by: list[str]` — fully qualified `doc_id`s, default `[]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_local_files_connector.py`:

```python
def test_grounded_by_is_read_verbatim_and_never_prefixed(tmp_path):
    """Qualified-only: the connector must not prefix its own system, or a
    cross-system id becomes `local_files:servicenow:SVC0042`."""
    (tmp_path / "a.md").write_text(
        "---\ngrounded_by:\n  - servicenow:SVC0042\n  - local_files:b.md\n---\nbody\n",
        "utf-8",
    )
    docs = _normalize(tmp_path)          # existing helper in this file
    assert docs[0].grounded_by == ["local_files:b.md", "servicenow:SVC0042"]


def test_grounded_by_never_becomes_a_facet(tmp_path):
    """Unreserved keys land in `structured` and then in rendered frontmatter."""
    (tmp_path / "a.md").write_text(
        "---\ngrounded_by:\n  - servicenow:SVC0042\n---\nbody\n", "utf-8"
    )
    docs = _normalize(tmp_path)
    assert "grounded_by" not in docs[0].structured


def test_a_bare_grounded_by_id_is_dropped_with_no_prefixing(tmp_path):
    """Spec §2.1 accepts no bare form. Dropping is safer than guessing a system."""
    (tmp_path / "a.md").write_text(
        "---\ngrounded_by:\n  - b.md\n---\nbody\n", "utf-8"
    )
    assert _normalize(tmp_path)[0].grounded_by == []
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_local_files_connector.py -k grounded_by -v`
Expected: FAIL — `AttributeError: 'CanonicalDocument' object has no attribute 'grounded_by'`.

- [ ] **Step 3: Add the field**

`src/kbforge/models.py`, in `CanonicalDocument` after `relations`:

```python
    grounded_by: list[str] = Field(default_factory=list)
    """Fully qualified doc_ids this document should be grounded in (§2.1).
    Qualified-only: a bare id would have to be told from a qualified one by
    looking for a colon, and a native_id may contain one."""
```

- [ ] **Step 4: Populate it in `local_files`**

Add `"grounded_by"` to `_RESERVED_KEYS`, and after the `relations = sorted(...)` block:

```python
            # No `_SYSTEM` prefix: §2.1 ids are qualified already. An entry with
            # no system is dropped rather than guessed at.
            grounded_by = sorted(
                r
                for r in front.get("grounded_by", [])
                if isinstance(r, str) and ":" in r and all(r.partition(":")[::2])
            )
```

and pass `grounded_by=grounded_by` to the `CanonicalDocument(...)` call.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_local_files_connector.py tests/test_models.py -v`
Expected: PASS, no warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/models.py src/kbforge/connectors/local_files.py tests/
git commit -m "feat: carry grounded_by from source frontmatter to the canonical document"
```

---

### Task 2: the shared `resource_key`, and `slot_key` on the mirror

**Files:**
- Modify: `src/kbforge/models.py`, `src/kbforge/mirror.py:12-14`, `src/kbforge/validate.py:305-308`
- Test: `tests/test_validate.py`, `tests/test_mirror.py`

**Interfaces:**
- Produces: `models.resource_key(anchor: ResourceAnchor) -> str`; `mirror.slot_key(doc_id: str) -> str`.

Why this is its own task: the dedup in Task 4 must key on exactly what `_check_sources_shape` compares. Two copies of that expression is a live bug waiting for one of them to change.

- [ ] **Step 1: Write the failing test**

In `tests/test_validate.py`:

```python
def test_expected_resources_uses_the_shared_key_function():
    """Dedup (grounding) and the law's set-compare must key identically. If this
    import breaks, the two have diverged."""
    from kbforge.models import ResourceAnchor, resource_key
    from kbforge.validate import _expected_resources
    from kbforge.models import ConceptFrontmatter
    from datetime import UTC, datetime

    a = ResourceAnchor(
        system="s", native_id="n", retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="h", url="https://example.test/x",
    )
    fm = ConceptFrontmatter(type="concept", sources=[a],
                            generated_at=a.retrieved_at, generated_by="kbforge/0")
    assert _expected_resources(fm) == {resource_key(a)} == {"https://example.test/x"}
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `uv run pytest tests/test_validate.py -k shared_key -v`
Expected: FAIL — `ImportError: cannot import name 'resource_key'`.

- [ ] **Step 3: Add both helpers**

`src/kbforge/models.py`, after `ResourceAnchor`:

```python
def resource_key(anchor: ResourceAnchor) -> str:
    """The identity a `sources` entry is compared by (§4.4 law 3, §5.1).

    One definition, because two consumers must agree exactly: `validate`
    compares rendered against projected `sources` as sets of this key, and
    grounding deduplicates candidates by it."""
    return anchor.url or f"{anchor.system}:{anchor.native_id}"
```

`src/kbforge/mirror.py`, replacing `_slot`:

```python
def slot_key(doc_id: str) -> str:
    """The on-disk name for a doc_id. Public so the grounding sidecar names its
    files identically -- two hashing schemes would silently orphan sidecars."""
    return hashlib.sha256(doc_id.encode("utf-8")).hexdigest()


def _slot(mirror: Path, doc_id: str) -> Path:
    return mirror / f"{slot_key(doc_id)}.json"
```

`src/kbforge/validate.py`, replacing the body of `_expected_resources`:

```python
    return {resource_key(a) for a in concept.sources}
```

with `from kbforge.models import resource_key` added to its imports.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: 387 passed, 9 skipped — this is a pure refactor and must move no other number.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/models.py src/kbforge/mirror.py src/kbforge/validate.py tests/
git commit -m "refactor: one definition of a sources entry's identity, and of a slot name"
```

---

### Task 3: grounding config — model, loader, shape validation

**Files:**
- Create: `src/kbforge/grounding.py`
- Test: `tests/test_grounding.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DEFAULT_MAX_GROUNDING_DOCS = 5`
  - `class GroundingConfig(BaseModel)` with `max_grounding_docs: int`, `grounding: dict[str, list[str]]`
  - `load_grounding(path: Path | None) -> GroundingConfig`
  - `problems_for(cfg: GroundingConfig) -> list[str]`
  - `declared_ids(doc: CanonicalDocument, cfg: GroundingConfig) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_grounding.py`:

```python
from pathlib import Path

import pytest

from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    load_grounding,
    problems_for,
)
from kbforge.models import CanonicalDocument, ResourceAnchor
from datetime import UTC, datetime


def _doc(doc_id="confluence:payments", grounded_by=None):
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=doc_id.partition(":")[0],
            native_id=doc_id.partition(":")[2],
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash="h",
        ),
        doc_id=doc_id,
        title="T",
        text="body",
        grounded_by=grounded_by or [],
    )


def test_absent_path_is_an_empty_config():
    cfg = load_grounding(None)
    assert cfg.grounding == {} and cfg.max_grounding_docs == 5


def test_map_is_loaded(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text(
        "max_grounding_docs: 2\n"
        "grounding:\n"
        "  confluence:payments:\n"
        "    - servicenow:SVC0042\n",
        "utf-8",
    )
    cfg = load_grounding(p)
    assert cfg.max_grounding_docs == 2
    assert cfg.grounding == {"confluence:payments": ["servicenow:SVC0042"]}


def test_unknown_key_is_refused(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("groundings: {}\n", "utf-8")   # typo'd key
    with pytest.raises(Exception):
        load_grounding(p)


@pytest.mark.parametrize(
    "cfg, fragment",
    [
        (GroundingConfig(grounding={"payments": ["servicenow:SVC0042"]}), "key"),
        (GroundingConfig(grounding={"confluence:payments": ["SVC0042"]}), "value"),
        (GroundingConfig(grounding={"confluence:payments": [":SVC0042"]}), "value"),
        (GroundingConfig(max_grounding_docs=0), "max_grounding_docs"),
    ],
)
def test_shape_problems_are_reported(cfg, fragment):
    problems = problems_for(cfg)
    assert problems and any(fragment in p for p in problems)


def test_a_valid_config_has_no_problems():
    cfg = GroundingConfig(grounding={"confluence:payments": ["servicenow:SVC0042"]})
    assert problems_for(cfg) == []


def test_declared_ids_unions_both_sites_and_dedupes():
    """Spec §2: two declaration sites, one consumption path."""
    doc = _doc(grounded_by=["servicenow:SVC0042", "mcp-aws:x"])
    cfg = GroundingConfig(
        grounding={"confluence:payments": ["servicenow:SVC0042", "drive:D1"]}
    )
    assert declared_ids(doc, cfg) == ["drive:D1", "mcp-aws:x", "servicenow:SVC0042"]
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.grounding'`.

- [ ] **Step 3: Write the module**

Create `src/kbforge/grounding.py`:

```python
"""Cross-source grounding: which documents ground which, and whether that has
changed since the concept was last built (design note 2026-08-20).

Everything here is pure except the four sidecar functions. Resolution lives on
this side of the seam, never in a synthesizer: a synthesizer that chose its own
sources would be choosing its own provenance."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.models import CanonicalDocument

DEFAULT_MAX_GROUNDING_DOCS = 5


class GroundingConfig(BaseModel):
    """The operator subject map (§2.2). `extra="forbid"` so a typo'd key is an
    error rather than a silently empty map."""

    model_config = ConfigDict(extra="forbid")

    max_grounding_docs: int = DEFAULT_MAX_GROUNDING_DOCS
    grounding: dict[str, list[str]] = Field(default_factory=dict)


def load_grounding(path: Path | None) -> GroundingConfig:
    if path is None:
        return GroundingConfig()
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return GroundingConfig.model_validate(raw)


def _qualified(value: str) -> bool:
    """A doc_id is `system:native_id` with both halves non-empty."""
    system, sep, native = value.partition(":")
    return bool(sep and system and native)


def problems_for(cfg: GroundingConfig) -> list[str]:
    """Shape only ([] = ok). Whether an id *resolves* is not a shape question and
    is not fatal -- §2.2, symmetric with the unresolvable-value rule in §3."""
    problems: list[str] = []
    if cfg.max_grounding_docs < 1:
        problems.append("grounding 'max_grounding_docs' must be at least 1")
    for key, values in sorted(cfg.grounding.items()):
        if not _qualified(key):
            problems.append(
                f"grounding key {key!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
        for value in values:
            if not _qualified(value):
                problems.append(
                    f"grounding value {value!r} under {key!r} must be a qualified "
                    "doc_id ('system:native_id'); bare ids are not accepted"
                )
    return problems


def declared_ids(doc: CanonicalDocument, cfg: GroundingConfig) -> list[str]:
    """Both declaration sites, unioned and sorted. Sorted because everything
    downstream -- the cap, the sidecar, the diff -- must be deterministic."""
    return sorted(set(doc.grounded_by) | set(cfg.grounding.get(doc.doc_id, [])))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding.py
git commit -m "feat: the grounding subject map, its shape validation, and declared ids"
```

---

### Task 4: resolution

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Consumes: `declared_ids`, `models.resource_key`.
- Produces: `resolve(owner, ids, by_id, *, max_docs) -> tuple[list[CanonicalDocument], list[str]]` — `(grounding documents, notes)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grounding.py`:

```python
from kbforge.grounding import resolve


def _by_id(*docs):
    return {d.doc_id: d for d in docs}


def test_resolution_keeps_declared_documents_sorted():
    owner = _doc("confluence:payments")
    a, b = _doc("servicenow:SVC0042"), _doc("drive:D1")
    got, notes = resolve(
        owner, ["servicenow:SVC0042", "drive:D1"], _by_id(a, b), max_docs=5
    )
    assert [d.doc_id for d in got] == ["drive:D1", "servicenow:SVC0042"]
    assert notes == []


def test_self_reference_is_dropped_silently():
    owner = _doc("confluence:payments")
    got, notes = resolve(
        owner, ["confluence:payments"], _by_id(owner), max_docs=5
    )
    assert got == [] and notes == []


def test_unresolvable_id_is_dropped_with_a_note_not_an_error():
    """A grounding target may live in a system that has not synced yet. Failing
    would make one source's sync depend on another's."""
    owner = _doc("confluence:payments")
    got, notes = resolve(owner, ["servicenow:SVC0042"], _by_id(owner), max_docs=5)
    assert got == []
    assert notes and "servicenow:SVC0042" in notes[0]


def test_tombstoned_target_is_dropped():
    owner = _doc("confluence:payments")
    dead = _doc("servicenow:SVC0042")
    dead.deleted = True
    got, notes = resolve(
        owner, ["servicenow:SVC0042"], _by_id(owner, dead), max_docs=5
    )
    assert got == [] and notes


def test_duplicate_resource_collapses_even_across_different_doc_ids():
    """Dedup keys on the resource, not the doc_id: two systems can carry the same
    url, and `sources` is compared as a set of resources."""
    owner = _doc("confluence:payments")
    a, b = _doc("servicenow:SVC0042"), _doc("drive:D1")
    a.anchor.url = b.anchor.url = "https://example.test/same"
    got, _ = resolve(
        owner, ["servicenow:SVC0042", "drive:D1"], _by_id(a, b), max_docs=5
    )
    assert len(got) == 1


def test_a_grounding_doc_sharing_the_owners_resource_is_dropped():
    owner = _doc("confluence:payments")
    twin = _doc("drive:D1")
    owner.anchor.url = twin.anchor.url = "https://example.test/same"
    got, _ = resolve(owner, ["drive:D1"], _by_id(owner, twin), max_docs=5)
    assert got == []


def test_cap_truncates_deterministically_and_notes_what_it_dropped():
    owner = _doc("confluence:payments")
    docs = [_doc(f"servicenow:SVC{i}") for i in range(4)]
    got, notes = resolve(
        owner, [d.doc_id for d in docs], _by_id(*docs), max_docs=2
    )
    assert [d.doc_id for d in got] == ["servicenow:SVC0", "servicenow:SVC1"]
    assert notes and "servicenow:SVC2" in notes[0] and "servicenow:SVC3" in notes[0]
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_grounding.py -k resolve -v`
Expected: FAIL — `ImportError: cannot import name 'resolve'`.

- [ ] **Step 3: Implement**

Append to `src/kbforge/grounding.py` (add `from kbforge.models import resource_key` to imports):

```python
def resolve(
    owner: CanonicalDocument,
    ids: list[str],
    by_id: dict[str, CanonicalDocument],
    *,
    max_docs: int,
) -> tuple[list[CanonicalDocument], list[str]]:
    """Declared ids -> the documents that will actually be cited, plus notes.

    Nothing here raises: an unresolvable or tombstoned target is a fact about
    another system's sync state, not an error in this run (§3)."""
    notes: list[str] = []
    seen = {resource_key(owner.anchor)}
    kept: list[CanonicalDocument] = []

    for gid in sorted(set(ids)):
        if gid == owner.doc_id:
            continue                      # self-reference: silent, not a note
        doc = by_id.get(gid)
        if doc is None or doc.deleted:
            notes.append(
                f"{owner.doc_id}: grounding {gid} was not found in the mirror or "
                "this fetch and was dropped"
            )
            continue
        key = resource_key(doc.anchor)
        if key in seen:
            continue                      # same artifact, cited once
        seen.add(key)
        kept.append(doc)

    if len(kept) > max_docs:
        dropped = ", ".join(d.doc_id for d in kept[max_docs:])
        notes.append(
            f"{owner.doc_id}: grounding capped at {max_docs}; dropped {dropped}"
        )
        kept = kept[:max_docs]
    return kept, notes
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding.py
git commit -m "feat: resolve declared grounding ids to the documents that get cited"
```

---

### Task 5: the sidecar and its full lifecycle

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Consumes: `mirror.slot_key`.
- Produces: `SIDECAR_DIR = "_grounding"`, `read_sidecar(mirror, doc_id) -> dict[str, str] | None`, `write_sidecar(mirror, doc_id, recorded) -> None`, `delete_sidecar(mirror, doc_id) -> None`, `has_sidecars(mirror) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grounding.py`:

```python
from kbforge.grounding import (
    SIDECAR_DIR,
    delete_sidecar,
    has_sidecars,
    read_sidecar,
    write_sidecar,
)
from kbforge.mirror import load_all


def test_sidecar_round_trips(tmp_path: Path):
    write_sidecar(tmp_path, "confluence:payments", {"servicenow:SVC0042": "h1"})
    assert read_sidecar(tmp_path, "confluence:payments") == {
        "servicenow:SVC0042": "h1"
    }


def test_absent_sidecar_reads_as_none(tmp_path: Path):
    assert read_sidecar(tmp_path, "confluence:payments") is None


def test_delete_is_idempotent(tmp_path: Path):
    delete_sidecar(tmp_path, "confluence:payments")     # must not raise
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    delete_sidecar(tmp_path, "confluence:payments")
    assert read_sidecar(tmp_path, "confluence:payments") is None


def test_load_all_never_sees_a_sidecar(tmp_path: Path):
    """`load_all` globs `mirror/*.json`; the sidecar must stay in a subdirectory
    or every run would try to parse one as a CanonicalDocument."""
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    assert load_all(tmp_path) == []
    assert (tmp_path / SIDECAR_DIR).is_dir()


def test_has_sidecars_gates_the_scan(tmp_path: Path):
    assert has_sidecars(tmp_path) is False
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    assert has_sidecars(tmp_path) is True
    delete_sidecar(tmp_path, "confluence:payments")
    assert has_sidecars(tmp_path) is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_grounding.py -k sidecar -v`
Expected: FAIL — `ImportError: cannot import name 'SIDECAR_DIR'`.

- [ ] **Step 3: Implement**

Append to `src/kbforge/grounding.py` (add `import json` and `from kbforge.mirror import slot_key`):

```python
SIDECAR_DIR = "_grounding"
"""A subdirectory, deliberately: `load_all` globs `mirror/*.json`, so a sidecar
at the root would be parsed as a CanonicalDocument on every run."""


def _sidecar(mirror: Path, doc_id: str) -> Path:
    return mirror / SIDECAR_DIR / f"{slot_key(doc_id)}.json"


def read_sidecar(mirror: Path, doc_id: str) -> dict[str, str] | None:
    """The grounding hashes recorded when this concept was last published, or
    None if it has never been grounded."""
    path = _sidecar(mirror, doc_id)
    if not path.exists():
        return None
    return dict(json.loads(path.read_text("utf-8"))["grounding"])


def write_sidecar(mirror: Path, doc_id: str, recorded: dict[str, str]) -> None:
    path = _sidecar(mirror, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"doc_id": doc_id, "grounding": dict(sorted(recorded.items()))}
    path.write_text(json.dumps(payload, sort_keys=True), "utf-8")


def delete_sidecar(mirror: Path, doc_id: str) -> None:
    """Idempotent. Called when a grounding set empties and when an owner is
    tombstoned -- NOT writing a file does not remove the one already there, and
    a stale sidecar re-synthesizes its document on every run forever (§4)."""
    _sidecar(mirror, doc_id).unlink(missing_ok=True)


def has_sidecars(mirror: Path) -> bool:
    """Cheap gate for the drift scan: a directory listing, not a mirror load."""
    directory = mirror / SIDECAR_DIR
    return directory.is_dir() and any(directory.glob("*.json"))
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding.py
git commit -m "feat: the grounding sidecar, with deletes as first-class as writes"
```

---

### Task 6: drift detection

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding.py`

**Interfaces:**
- Consumes: `read_sidecar`.
- Produces: `drifted(mirror, candidates, resolved, hashes) -> list[str]` — owning `doc_id`s needing re-synthesis, sorted.
  - `candidates: list[CanonicalDocument]` — this run's systems' mirror documents, minus anything already changed or removed.
  - `resolved: dict[str, list[str]]` — owner `doc_id` → **post-resolution** grounding `doc_id`s.
  - `hashes: dict[str, str]` — `doc_id` → current `content_hash`, from mirror ∪ this run's docs.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grounding.py`:

```python
from kbforge.grounding import drifted


def test_unchanged_grounding_does_not_drift(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path, [owner],
        {"confluence:payments": ["servicenow:SVC0042"]},
        {"servicenow:SVC0042": "h1"},
    ) == []


def test_a_changed_grounding_hash_drifts(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path, [owner],
        {"confluence:payments": ["servicenow:SVC0042"]},
        {"servicenow:SVC0042": "h2"},      # the other system's run moved it
    ) == ["confluence:payments"]


def test_a_vanished_grounding_document_drifts(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path, [owner], {"confluence:payments": []}, {}
    ) == ["confluence:payments"]


def test_an_edited_map_drifts_via_the_set_comparison(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path, [owner],
        {"confluence:payments": ["servicenow:SVC0042", "drive:D1"]},
        {"servicenow:SVC0042": "h1", "drive:D1": "h9"},
    ) == ["confluence:payments"]


def test_a_document_that_never_grounded_does_not_drift(tmp_path: Path):
    assert drifted(tmp_path, [_doc("confluence:payments")], {}, {}) == []


def test_an_unresolvable_id_does_not_drift_forever(tmp_path: Path):
    """`resolved` is POST-resolution, so an id that never resolves is absent from
    both sides. Comparing DECLARED ids would leave it permanently present on one
    side and absent on the other -- re-synthesizing every run, forever."""
    owner = _doc("confluence:payments", grounded_by=["servicenow:NEVER"])
    write_sidecar(tmp_path, owner.doc_id, {"drive:D1": "h1"})
    assert drifted(
        tmp_path, [owner], {"confluence:payments": ["drive:D1"]}, {"drive:D1": "h1"}
    ) == []
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_grounding.py -k drift -v`
Expected: FAIL — `ImportError: cannot import name 'drifted'`.

- [ ] **Step 3: Implement**

Append to `src/kbforge/grounding.py`:

```python
def drifted(
    mirror: Path,
    candidates: list[CanonicalDocument],
    resolved: dict[str, list[str]],
    hashes: dict[str, str],
) -> list[str]:
    """Owning doc_ids whose grounding moved since they were last published.

    `resolved` must be POST-resolution, matching what `write_sidecar` recorded.
    Comparing declared ids instead leaves an unresolvable id permanently present
    on one side and absent on the other, re-synthesizing forever (§4)."""
    out: list[str] = []
    for doc in candidates:
        recorded = read_sidecar(mirror, doc.doc_id)
        if recorded is None:
            continue                       # never grounded: nothing to drift from
        current = set(resolved.get(doc.doc_id, []))
        if current != set(recorded):       # rule 3, and rule 2 by construction
            out.append(doc.doc_id)
            continue
        if any(hashes.get(gid) != h for gid, h in recorded.items()):   # rule 1
            out.append(doc.doc_id)
    return sorted(out)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_grounding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding.py
git commit -m "feat: derive grounding drift from the shared mirror"
```

---

### Task 7: multi-source emission and the synthesizer protocol

**Files:**
- Modify: `src/kbforge/synthesize.py:141-168`, `:196-225`
- Test: `tests/test_synthesize.py`, `tests/test_strict_okf.py`

**Interfaces:**
- Consumes: nothing from Tasks 3-6 (takes resolved documents as an argument).
- Produces:
  - `assemble(items, changeset, existing_paths=frozenset(), *, generated_by=..., grounding: dict[str, list[CanonicalDocument]] | None = None)`
  - `Synthesizer` protocol gains `grounds: bool = False` and a trailing `grounding: dict[str, list[CanonicalDocument]] | None = None` parameter.
  - `StubSynthesizer.grounds = False`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_synthesize.py`:

```python
def test_grounding_documents_are_cited_after_the_owning_anchor():
    """Owning anchor first is kbforge convention, not a validator rule --
    `_check_sources_shape` compares resources as sets. It is what makes
    "primary" legible in the artifact without adding a field to OKF."""
    owner = _doc("local_files:a.md")          # existing helper
    ground = _doc("servicenow:SVC0042")
    proposal = assemble(
        [(owner, "T", "D", "body")],
        ChangeSet(added=[owner.doc_id]),
        grounding={owner.doc_id: [ground]},
    )
    fm = proposal.concepts[concept_path(owner.doc_id)]
    assert [a.native_id for a in fm.sources] == ["a.md", "SVC0042"]


def test_a_grounded_concept_still_passes_the_laws():
    owner = _doc("local_files:a.md")
    ground = _doc("servicenow:SVC0042")
    proposal = assemble(
        [(owner, "T", "D", "body")],
        ChangeSet(added=[owner.doc_id]),
        grounding={owner.doc_id: [ground]},
    )
    path = concept_path(owner.doc_id)
    assert run_validators(proposal, frozenset({path})) == []


def test_grounding_for_another_document_does_not_leak():
    owner = _doc("local_files:a.md")
    other = _doc("local_files:b.md")
    ground = _doc("servicenow:SVC0042")
    proposal = assemble(
        [(owner, "T", "D", "body"), (other, "T2", "D2", "body2")],
        ChangeSet(added=[owner.doc_id, other.doc_id]),
        grounding={owner.doc_id: [ground]},
    )
    assert len(proposal.concepts[concept_path(other.doc_id)].sources) == 1


def test_the_stub_declares_that_it_does_not_ground():
    assert StubSynthesizer.grounds is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_synthesize.py -k ground -v`
Expected: FAIL — `TypeError: assemble() got an unexpected keyword argument 'grounding'`.

- [ ] **Step 3: Implement**

In `assemble`, add the parameter and use it:

```python
def assemble(
    items: list[tuple[CanonicalDocument, str, str, str]],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
    *,
    generated_by: str = _DEFAULT_ACTOR,
    grounding: dict[str, list[CanonicalDocument]] | None = None,
) -> ProposedChange:
```

and inside the loop replace `sources=[doc.anchor],` with:

```python
            # Owning anchor first (§6): the validator compares resources as sets,
            # so this ordering is convention, and it is what tells a reader which
            # system owns the concept without a new OKF field.
            sources=[doc.anchor, *(g.anchor for g in (grounding or {}).get(doc.doc_id, []))],
```

Then the protocol and stub:

```python
class Synthesizer(Protocol):
    grounds: bool = False
    """Whether this synthesizer reads grounding documents. The pipeline reads it
    with getattr and uses it twice: to skip the drift scan, and to decide whether
    to pass `grounding=` at all -- a duck-typed synthesizer predating this
    parameter must keep working."""

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
        grounding: dict[str, list[CanonicalDocument]] | None = None,
    ) -> ProposedChange: ...


class StubSynthesizer:
    """Deterministic, no LLM: title and description mirror the source; body is the
    canonical text verbatim. The default synthesizer and the test baseline."""

    grounds = False
    """The body is the source verbatim, so citing a grounding document would claim
    a provenance the artifact does not have (§7)."""

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
        grounding: dict[str, list[CanonicalDocument]] | None = None,
    ) -> ProposedChange:
        items = [(doc, doc.title, doc.title, doc.text) for doc in changed_docs]
        return assemble(items, changeset, existing_paths)
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q`
Expected: all pass — no existing caller passes `grounding`, so behaviour is unchanged for them.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/synthesize.py tests/test_synthesize.py
git commit -m "feat: emit a multi-source concept, owning anchor first"
```

---

### Task 8: the LLM synthesizer grounds

**Files:**
- Modify: `src/kbforge/llm_synthesizer.py:120-178`
- Test: `tests/test_llm_synthesizer.py`

**Interfaces:**
- Consumes: `assemble(..., grounding=)` from Task 7.
- Produces: `LLMSynthesizer.grounds = True`; grounding text reaches the prompt.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_llm_synthesizer.py`:

```python
def test_llm_synthesizer_declares_that_it_grounds():
    assert LLMSynthesizer.grounds is True


def test_grounding_text_reaches_the_prompt_and_is_labelled_by_system():
    """The model must be able to tell whose text it is reading, or it cannot
    attribute a claim to the right system in prose."""
    doc = _doc()
    ground = _doc(doc_id="servicenow:SVC0042", text="Escalate to the payments queue.")
    seen: list[str] = []

    def fn(messages, info):
        seen.append(messages[-1].parts[-1].content)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name,
                                SynthesizedConcept(title="X", description="d",
                                                   body="b").model_dump())]
        )

    synth = LLMSynthesizer(LLMConfig(), agent=Agent(FunctionModel(fn),
                                                    output_type=SynthesizedConcept))
    synth.synthesize([doc], ChangeSet(added=[doc.doc_id]),
                     grounding={doc.doc_id: [ground]})
    assert "servicenow:SVC0042" in seen[0]
    assert "Escalate to the payments queue." in seen[0]


def test_a_grounding_document_is_truncated_like_a_source():
    doc = _doc()
    ground = _doc(doc_id="servicenow:SVC0042", text="x" * 5000)
    concept = SynthesizedConcept(title="X", description="d", body="b")
    synth = _synth(concept, max_source_chars=100)
    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]),
                                grounding={doc.doc_id: [ground]})
    assert any("servicenow:SVC0042" in n and "truncated" in n
               for n in proposal.summary.grounding_notes)
```

The `_doc` helper in this file takes `doc_id` already; add a `text=` pass-through if it is missing.

- [ ] **Step 2: Run and confirm failure**

Run: `uv run pytest tests/test_llm_synthesizer.py -k ground -v`
Expected: FAIL — `AttributeError: type object 'LLMSynthesizer' has no attribute 'grounds'`.

- [ ] **Step 3: Implement**

Add the class attribute and extend `_prompt` and `synthesize`:

```python
    grounds = True
    """Reads grounding documents and writes a body informed by them (§7)."""

    def _grounding_block(
        self, docs: list[CanonicalDocument], notes: list[str], owner_id: str
    ) -> str:
        """Related documents from other systems, each labelled with its doc_id so
        the model can attribute a claim to the system it came from."""
        if not docs:
            return ""
        parts = []
        for g in docs:
            text = g.text
            if len(text) > self.config.max_source_chars:
                text = text[: self.config.max_source_chars]
                notes.append(
                    f"{concept_path(owner_id)}: grounding {g.doc_id} truncated to "
                    f"{self.config.max_source_chars} chars before synthesis"
                )
            parts.append(f"--- {g.doc_id} ---\n{text}")
        joined = "\n\n".join(parts)
        return (
            "\n\nRelated documents from other systems. Use them for context and "
            "corroboration. Do not treat them as this concept's subject:\n\n"
            f"{joined}"
        )
```

In `synthesize`, add the parameter, build the block, and pass grounding to `assemble`:

```python
    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
        grounding: dict[str, list[CanonicalDocument]] | None = None,
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        notes: list[str] = []
        grounding = grounding or {}
        for doc in changed_docs:
            text = doc.text
            if len(text) > self.config.max_source_chars:
                text = text[: self.config.max_source_chars]
                notes.append(
                    f"{concept_path(doc.doc_id)}: source truncated to "
                    f"{self.config.max_source_chars} chars before synthesis"
                )
            block = self._grounding_block(
                grounding.get(doc.doc_id, []), notes, doc.doc_id
            )
            result = self.agent.run_sync(self._prompt(doc, text) + block)
            c = result.output
            body = _strip_title_heading(c.body, c.title)
            items.append((doc, c.title, c.description, body))
        proposal = assemble(
            items,
            changeset,
            existing_paths,
            generated_by=actor_for(self.config.model),
            grounding=grounding,
        )
        proposal.summary.grounding_notes.extend(notes)
        return proposal
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_llm_synthesizer.py -v`
Expected: PASS, output pristine.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/llm_synthesizer.py tests/test_llm_synthesizer.py
git commit -m "feat: ground the LLM synthesizer in related documents from other systems"
```

---

### Task 9: pipeline wiring, the CLI flag, and the docs

**Files:**
- Modify: `src/kbforge/pipeline.py:86-195`, `src/kbforge/__main__.py:72-195`
- Modify: `docs/architecture.md`, `CHANGELOG.md`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 3-8.
- Produces: `run(..., grounding_config: GroundingConfig | None = None)`; `kbforge run --grounding PATH`.

- [ ] **Step 1: Extend the test helpers**

`tests/test_pipeline.py`'s `_doc` hardcodes `system="sys"`, and `_run_once`
asserts the run published — neither works for a two-system or no-op test. Extend
both, and add a result-returning variant:

```python
def _doc(
    native_id: str,
    title: str,
    *,
    system: str = "sys",
    deleted: bool = False,
    relations: list[str] | None = None,
    grounded_by: list[str] | None = None,
    text: str | None = None,
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
        title=title,
        text=text or title,
        relations=relations or [],
        grounded_by=grounded_by or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


def _run_result(tmp_path, docs, synthesizer=None, grounding_config=None):
    """The raw result, so a test can assert NoOp. `_run_once` asserts a publish
    happened and cannot express "nothing should have happened"."""
    publisher = _RecordingPublisher()
    result = run(
        _FakeConnector(docs),
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        synthesizer=synthesizer,
        grounding_config=grounding_config,
    )
    return result, publisher
```

and thread `grounding_config=None` through `_run_once` the same way.

**Do not add `grounded_by` to `canonical.content_hash`.** It is tempting — it is
declared content — but that payload is an explicit allowlist
(`canonical.py:37-44`), and adding a key changes the hash of *every* document.
The first run after upgrade would mark the entire mirror modified and
re-synthesize it, which for the LLM synthesizer is an unbounded token bill for no
change in any source. Drift rule 3 exists to catch a `grounded_by` edit precisely
so the hash does not have to.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_pipeline.py`:

```python
from kbforge.grounding import GroundingConfig, has_sidecars
from kbforge.synthesize import assemble


class _GroundingSynth:
    """Records what grounding it was handed. `grounds = True`, so the pipeline
    both scans for drift and passes the keyword."""

    grounds = True

    def __init__(self):
        self.seen: dict = {}

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset(),
                   grounding=None):
        self.seen = grounding or {}
        items = [(d, d.title, d.title, d.text) for d in changed_docs]
        return assemble(items, changeset, existing_paths, grounding=grounding)


def _cfg(**mapping):
    return GroundingConfig(grounding=dict(mapping))


def test_a_legacy_synthesizer_is_never_passed_grounding(tmp_path: Path):
    """`_FixedSynth` is duck-typed with no `grounding` parameter. Passing the
    keyword unconditionally raises TypeError for every synthesizer written
    before this change -- including both test doubles in this file."""
    pub = _run_once(tmp_path, [_doc("a", "A")], synthesizer=_FixedSynth())
    assert pub.last_change is not None


def test_grounding_reaches_the_synthesizer(tmp_path: Path):
    synth = _GroundingSynth()
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(
        tmp_path, docs, synthesizer=synth,
        grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}),
    )
    assert [d.doc_id for d in synth.seen["sys:a"]] == ["other:SVC1"]


def test_a_grounded_concept_cites_the_owning_system_first(tmp_path: Path):
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    pub = _run_once(
        tmp_path, docs, synthesizer=_GroundingSynth(),
        grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}),
    )
    fm = pub.last_change.concepts[concept_path("sys:a")]
    assert [a.system for a in fm.sources] == ["sys", "other"]


def test_drift_in_another_system_reopens_the_owner_on_its_next_run(tmp_path: Path):
    """The whole point. System B's run must not touch A's concepts, and A's next
    run must rebuild what B moved."""
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    a = _doc("a", "A")
    ticket_v1 = _doc("SVC1", "Ticket", system="other")
    _run_once(tmp_path, [a, ticket_v1], synthesizer=_GroundingSynth(),
              grounding_config=cfg)

    # B's run alone, with B's document changed.
    ticket_v2 = _doc("SVC1", "Ticket reassigned", system="other")
    pub_b = _run_once(tmp_path, [ticket_v2], synthesizer=_GroundingSynth(),
                      grounding_config=cfg)
    assert concept_path("sys:a") not in pub_b.last_change.files   # branch-per-system

    # A's next run, A's own source unchanged.
    pub_a = _run_once(tmp_path, [a], synthesizer=_GroundingSynth(),
                      grounding_config=cfg)
    assert concept_path("sys:a") in pub_a.last_change.files
    assert any("another system" in n
               for n in pub_a.last_change.summary.grounding_notes)


def test_an_unchanged_grounded_run_is_still_a_noop(tmp_path: Path):
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    result, _ = _run_result(tmp_path, docs, synthesizer=_GroundingSynth(),
                            grounding_config=cfg)
    assert isinstance(result, NoOp)


def test_emptying_the_grounding_set_settles_after_one_rebuild(tmp_path: Path):
    """The sidecar must be DELETED, not skipped. Skipping leaves rule 3 firing on
    every later run: three would rebuild, and four, and five."""
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(),
              grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}))

    rebuild, _ = _run_result(tmp_path, docs, synthesizer=_GroundingSynth(),
                             grounding_config=GroundingConfig())
    assert isinstance(rebuild, Published)          # the map went away: rebuild once

    settled, _ = _run_result(tmp_path, docs, synthesizer=_GroundingSynth(),
                             grounding_config=GroundingConfig())
    assert isinstance(settled, NoOp)               # and then stop


def test_an_unresolvable_grounding_id_does_not_loop(tmp_path: Path):
    """Declared but never resolvable: rule 3 compares post-resolution sets, so
    this must settle rather than rebuild forever."""
    cfg = _cfg(**{"sys:a": ["nowhere:X"]})
    docs = [_doc("a", "A")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    result, _ = _run_result(tmp_path, docs, synthesizer=_GroundingSynth(),
                            grounding_config=cfg)
    assert isinstance(result, NoOp)


def test_a_tombstoned_owner_leaves_no_sidecar(tmp_path: Path):
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    assert has_sidecars(tmp_path / "mirror") is True

    _run_once(tmp_path, [_doc("a", "A", deleted=True)],
              synthesizer=_GroundingSynth(), grounding_config=cfg)
    assert has_sidecars(tmp_path / "mirror") is False


def test_another_systems_drift_is_never_pulled_into_scope(tmp_path: Path):
    """Scoping is by {d.anchor.system for d in docs}, not connector name --
    kbforge-mcp is named `mcp` and carries a configured `system`, so a
    name-based scope would be wrong for exactly the connector that needs this."""
    cfg = _cfg(**{"other:SVC1": ["sys:a"]})       # the OTHER system is grounded
    a_v1 = _doc("a", "A")
    ticket = _doc("SVC1", "Ticket", system="other")
    _run_once(tmp_path, [a_v1, ticket], synthesizer=_GroundingSynth(),
              grounding_config=cfg)

    a_v2 = _doc("a", "A rewritten")
    pub = _run_once(tmp_path, [a_v2], synthesizer=_GroundingSynth(),
                    grounding_config=cfg)
    assert concept_path("other:SVC1") not in pub.last_change.files


def test_a_document_selected_by_both_drift_and_referrers_renders_once(tmp_path: Path):
    """`referrers` filters on `d.doc_id not in changed`, which knows nothing about
    drift, so the same document can arrive twice."""
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    a = _doc("a", "A", relations=["sys:gone"])
    gone = _doc("gone", "Gone")
    ticket_v1 = _doc("SVC1", "Ticket", system="other")
    _run_once(tmp_path, [a, gone, ticket_v1], synthesizer=_GroundingSynth(),
              grounding_config=cfg)

    ticket_v2 = _doc("SVC1", "Ticket reassigned", system="other")
    pub = _run_once(
        tmp_path,
        [_doc("gone", "Gone", deleted=True), ticket_v2],
        synthesizer=_GroundingSynth(),
        grounding_config=cfg,
    )
    anchors = [a.native_id for a in pub.last_change.summary.sources_changed]
    assert anchors.count("a") == 1
```

- [ ] **Step 3: Run and confirm failure**

Run: `uv run pytest tests/test_pipeline.py -k ground -v`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'grounding_config'`.

- [ ] **Step 4: Wire the pipeline**

Add the parameter, then replace the block from `changeset = diff(...)` through the `synthesizer.synthesize(...)` call:

```python
    grounds = getattr(synthesizer, "grounds", False)
    grounding_cfg = grounding_config or GroundingConfig()

    changeset = diff(mirror_path, docs)

    # The scan is gated three ways so a deployment that declares no grounding
    # keeps today's cheap no-op: the synthesizer must ground, and there must be
    # either something declared now or a sidecar from before (§5).
    scan = grounds and bool(
        grounding_cfg.grounding
        or any(d.grounded_by for d in docs)
        or has_sidecars(mirror_path)
    )
    if changeset.is_noop and not scan:
        return NoOp()

    mirror_docs = load_all(mirror_path)
    by_id = {d.doc_id: d for d in mirror_docs}
    by_id.update({d.doc_id: d for d in docs if not d.deleted})
    hashes = {k: v.anchor.content_hash for k, v in by_id.items()}

    changed = set(changeset.added) | set(changeset.modified)
    removed_ids = set(changeset.removed)
    changed_docs = [d for d in docs if d.doc_id in changed]

    def _resolved(doc: CanonicalDocument) -> tuple[list[CanonicalDocument], list[str]]:
        return resolve(
            doc,
            declared_ids(doc, grounding_cfg),
            by_id,
            max_docs=grounding_cfg.max_grounding_docs,
        )

    drift: list[str] = []
    if scan:
        # Scope by this run's own output. Connector identity will not do:
        # `kbforge_connector_info()` is static while a generic connector's
        # `system` is per-instance (design note §4).
        systems = {d.anchor.system for d in docs}
        candidates = [
            d
            for d in mirror_docs
            if d.anchor.system in systems
            and d.doc_id not in changed
            and d.doc_id not in removed_ids
        ]
        drift = drifted(
            mirror_path,
            candidates,
            {d.doc_id: [g.doc_id for g in _resolved(d)[0]] for d in candidates},
            hashes,
        )
        changed_docs += [d for d in candidates if d.doc_id in set(drift)]

    if changeset.is_noop and not drift:
        return NoOp()
```

Keep the existing `referrers` block, then dedup before use:

```python
    seen_ids: set[str] = set()
    deduped: list[CanonicalDocument] = []
    for d in changed_docs:
        if d.doc_id in seen_ids:
            continue      # drift and referrers can select the same document
        seen_ids.add(d.doc_id)
        deduped.append(d)
    changed_docs = deduped
```

Then resolve for the final scope and call the synthesizer:

```python
    grounding_map: dict[str, list[CanonicalDocument]] = {}
    grounding_notes: list[str] = []
    if grounds:
        for doc in changed_docs:
            docs_for, notes_for = _resolved(doc)
            if docs_for:
                grounding_map[doc.doc_id] = docs_for
            grounding_notes += notes_for

    if grounds:
        proposal = synthesizer.synthesize(
            changed_docs, changeset, existing, grounding=grounding_map
        )
    else:
        proposal = synthesizer.synthesize(changed_docs, changeset, existing)
    proposal.summary.grounding_notes.extend(grounding_notes)
```

Add a drift note beside the existing referrer note:

```python
    for doc_id in drift:
        path = concept_path(doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized because a document it is grounded in "
                "changed in another system; its own source is unchanged"
            )
```

And after `commit(mirror_path, docs)`:

```python
    if grounds:
        for doc in changed_docs:
            docs_for = grounding_map.get(doc.doc_id)
            if docs_for:
                write_sidecar(
                    mirror_path,
                    doc.doc_id,
                    {g.doc_id: g.anchor.content_hash for g in docs_for},
                )
            else:
                # A delete, not a skip: a stale sidecar fires rule 3 forever.
                delete_sidecar(mirror_path, doc.doc_id)
    for doc_id in changeset.removed:
        delete_sidecar(mirror_path, doc_id)
```

with imports:

```python
from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    delete_sidecar,
    drifted,
    has_sidecars,
    resolve,
    write_sidecar,
)
```

- [ ] **Step 5: Wire the CLI**

In `src/kbforge/__main__.py`, on the `run` subparser:

```python
    r.add_argument(
        "--grounding",
        default=None,
        metavar="PATH",
        help="grounding subject map (YAML); see docs/design/2026-08-20-"
        "cross-source-grounding-design.md",
    )
```

and before calling `run(...)`:

```python
    grounding_config = load_grounding(Path(args.grounding) if args.grounding else None)
    problems = problems_for(grounding_config)
    if problems:
        print(f"grounding config: {'; '.join(problems)}")
        return 2
```

passing `grounding_config=grounding_config` into `run(...)`.

Add a CLI test in `tests/test_cli.py`:

```python
def test_a_malformed_grounding_map_exits_2_before_fetching(tmp_path, capsys):
    p = tmp_path / "g.yaml"
    p.write_text("grounding:\n  payments:\n    - servicenow:SVC0042\n", "utf-8")
    code = main(["run", "--connector", "local_files", "--grounding", str(p),
                 "--set", f"root={tmp_path}", "--mirror", str(tmp_path / "m"),
                 "--state", str(tmp_path / "s")])
    assert code == 2
    assert "qualified doc_id" in capsys.readouterr().out
```

- [ ] **Step 6: Run everything**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: all pass; ruff, ruff-format and ty clean.

- [ ] **Step 7: Verify the gates by breaking them**

Per CLAUDE.md, mutate **in place** and restore with `git checkout --` (commit first):

1. Change `delete_sidecar(mirror_path, doc.doc_id)` to `pass` → `test_emptying_the_grounding_set_settles_after_one_rebuild` must fail.
2. Change `drifted`'s `resolved` argument to declared ids → `test_an_unresolvable_id_does_not_drift_forever` must fail.
3. Change the scan scope to `d.anchor.system in {info.name}` → `test_another_systems_drift_is_never_pulled_into_scope` must fail.
4. Pass `grounding=` unconditionally → `test_a_legacy_synthesizer_is_never_passed_grounding` must fail with `TypeError`.

Record the four failure **messages** in the task report, not just that they failed.

- [ ] **Step 8: Update the docs**

`docs/architecture.md`: §4.4 law 3 gains the multi-source case and the owning-anchor-first convention; §7's "one connector per run" paragraph gains a sentence that grounding does not weaken it (a run still fetches one system and publishes one system's concepts); §7's no-op paragraph gains the restatement from spec §5.

`CHANGELOG.md`: a `## [0.8.0]` entry under Added, Changed, and Known limits, carrying spec §11's limits verbatim.

Move the spec's shipped sections into `architecture.md` and reduce
`docs/design/2026-08-20-cross-source-grounding-design.md` to deferred-only
content with a fold table, per CLAUDE.md's docs layout rule.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: cross-source grounding, wired end to end"
```

---

## Plan Self-Review

**Spec coverage.** §1 vocabulary → Tasks 3-6 naming. §2.1 → Task 1. §2.2 → Task 3. §3 → Task 4. §4 sidecar + drift → Tasks 5, 6, and 9's lifecycle. §5 no-op → Task 9's two gates. §6 emission → Task 7. §7 stub/grounds → Tasks 7, 8. §8 untrusted content → no code; the bound it names (kbforge assigns `sources` from resolved anchors) is enforced by Task 7 taking documents rather than ids, and by Task 9 resolving before the call. §9 validators → Task 7's law test. §10 testing → distributed. §11 limits → Task 9 Step 7.

**Placeholders.** None. The first draft elided Task 9's ten pipeline tests as `...`; they are written out in full, against the real `_doc` / `_FakeConnector` / `_run_once` helpers, along with the two helper extensions they need. Task 9 also closes a trap the spec does not mention: adding `grounded_by` to `canonical.content_hash` would change every document's hash and re-synthesize the whole mirror on first upgrade.

**Type consistency.** `resolve` returns `tuple[list[CanonicalDocument], list[str]]` in Tasks 4, 9. `drifted` takes `resolved: dict[str, list[str]]` (doc_ids, post-resolution) in Tasks 6, 9. `grounding` is `dict[str, list[CanonicalDocument]]` keyed by owning doc_id in Tasks 7, 8, 9. `read_sidecar` returns `dict[str, str] | None` in Tasks 5, 6. Verified consistent.
