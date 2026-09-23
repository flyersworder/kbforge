# Describe Synthesizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `kbforge run --synthesizer describe` publishes stub-bodied concepts whose one-sentence `description` a model wrote and whose `tags` come from source ∪ keyword ∪ model, with model output cached per `content_hash` in `mirror/_described/`.

**Architecture:** A shared phrase matcher (`tagging.py`) serves keyword tags and grounding rules. `tags` becomes a kbforge-owned key with a projection field bound by the validators. `DescribeSynthesizer` (in `llm_synthesizer.py`) reads the `_described/` cache and returns what it used on `ProposedChange.described`; the pipeline persists it after a successful publish, exactly like grounding sidecars.

**Tech Stack:** Python 3.12+, Pydantic v2, pydantic-ai 2.13 (`FunctionModel` for offline tests, `output_validator` + `ModelRetry`), PyYAML, pytest, ruff, ty.

**Spec:** `docs/design/2026-09-23-describe-synthesizer-design.md`

## Global Constraints

- `description`: one sentence; enforced as non-blank, single line, `len <= description_max_chars` (default **240**). Never truncated silently.
- `DescribeConfig` fields, exactly: `instructions: str = ""`, `tags_vocabulary: dict[str, list[str]] | None = None`, `model_tags: bool = True`, `description_max_chars: int = 240`. All set via `--llm-set`.
- Shipped `tags` = `sorted(set(source_tags) | set(keyword_tags) | set(model_tags))`.
- The vocabulary constrains **model** tags only. Source tags are never vocabulary-checked.
- Sidecar path `mirror/_described/<slot_key(doc_id)>.json`, payload `{doc_id, content_hash, actor, description, tags}` where `tags` is the **model's** tags only.
- Cache hit ⇔ record exists, `record.content_hash == doc.anchor.content_hash`, and `set(record.tags) ⊆ allowed` where `allowed = set(vocabulary) if (vocabulary and model_tags) else set()`.
- `generated.by` = `actor_for(model)` on a miss, the record's `actor` on a hit.
- `DescribeSynthesizer.grounds = False`.
- `local_files._RESERVED_KEYS` must NOT gain `tags`.
- okfquery's `OKF_OWNED` must NOT gain `tags` (comment change only).
- Tests never touch the network (`uv run pytest`); the live check uses `--run-live` and `.env`.
- Mutation checks: mutate in place, run, restore with `git checkout --` **after** committing the real change. Assert on failure *messages*.
- Commit after every task; `prek` (ruff + ty) runs on commit.

## Review Focus

1. **Corrupt `_described/` sidecar** (torn write, hand edit, wrong shape) — must read as a cache miss and re-describe, never raise. → Task 3.
2. **A description with YAML-special characters** (`: `, `#`, quotes, a leading `---`) — the rendered file must still parse and `run_validators` must pass. → Task 4.
3. **A vocabulary phrase with regex metacharacters** (`C++`, `800 V (DC)`) — matched literally on word boundaries, never as a regex. → Task 1.
4. **Source `tags` as a scalar, with duplicates, blanks or non-strings** (`tags: sic`, `[sic, sic, "", 3]`) — normalized to a sorted list of non-blank strings, never a validation failure. → Task 2.
5. **A model returning a duplicate or case-variant tag** (`[sic, sic]`, `SiC` when the key is `sic`) — duplicates collapse; a case variant is outside the vocabulary and is retried, then named in the `SynthesisError`. → Task 4.

---

### Task 1: Shared phrase matcher and keyword tags

**Files:**
- Create: `src/kbforge/tagging.py`
- Modify: `src/kbforge/grounding.py` (lines ~105-106 `_nfc`, ~199-205 pattern compile, ~224 haystack)
- Test: `tests/test_tagging.py`

**Interfaces:**
- Produces: `tagging.nfc(text: str) -> str`, `tagging.phrase_pattern(phrase: str) -> re.Pattern[str]`, `tagging.keyword_tags(vocabulary: dict[str, list[str]] | None, title: str, text: str) -> list[str]` (sorted).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_tagging.py
from kbforge.tagging import keyword_tags, phrase_pattern

VOCAB = {"sic": ["SiC", "silicon carbide"], "800v": ["800 V"], "cpp": ["C++"],
         "dc": ["800 V (DC)"], "packaging": []}  # fmt: skip


def test_a_phrase_matches_on_word_boundaries_case_insensitively():
    assert phrase_pattern("SiC").search("New sic MOSFETs")
    assert not phrase_pattern("SiC").search("a basic design")


def test_regex_metacharacters_are_literal():
    assert phrase_pattern("C++").search("written in C++ today")
    assert not phrase_pattern("C++").search("written in C today")
    assert phrase_pattern("800 V (DC)").search("rated 800 V (DC) bus")
    assert not phrase_pattern("800 V (DC)").search("rated 800 V DC bus")


def test_keyword_tags_are_the_sorted_tags_whose_phrases_match():
    tags = keyword_tags(VOCAB, "Q3 report", "Silicon carbide at 800 V, in C++.")
    assert tags == ["800v", "cpp", "sic"]


def test_a_tag_with_no_phrases_is_never_assigned_by_keyword():
    assert keyword_tags(VOCAB, "packaging", "packaging packaging") == []


def test_no_vocabulary_means_no_keyword_tags():
    assert keyword_tags(None, "SiC", "SiC") == []


def test_the_title_counts():
    assert keyword_tags(VOCAB, "SiC roadmap", "") == ["sic"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_tagging.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.tagging'`

- [ ] **Step 3: Implement `tagging.py`**

```python
"""Deterministic phrase matching: one rule shared by grounding rules and keyword
tags, so "this document mentions X" cannot mean two things.

Case-insensitive, on word boundaries, after NFC normalization, and always
literal: a phrase like `C++` or `800 V (DC)` is escaped, never read as a regex."""

from __future__ import annotations

import re
import unicodedata


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    # `(?<!\w)`/`(?!\w)` rather than `\b`: `\b` needs a word character on one
    # side, so it never matches around a phrase that starts or ends in `+`.
    return re.compile(rf"(?<!\w){re.escape(nfc(phrase))}(?!\w)", re.IGNORECASE)


def keyword_tags(
    vocabulary: dict[str, list[str]] | None, title: str, text: str
) -> list[str]:
    """The vocabulary tags with a phrase in `title` or `text`, sorted. Pure."""
    if not vocabulary:
        return []
    haystack = nfc(f"{title}\n{text}")
    return sorted(
        tag
        for tag, phrases in vocabulary.items()
        if any(phrase_pattern(p).search(haystack) for p in phrases)
    )
```

- [ ] **Step 4: Point grounding at it**

In `src/kbforge/grounding.py`: delete `_nfc` and `import unicodedata`; add `from kbforge.tagging import nfc, phrase_pattern`; replace the pattern build in `rule_matches` with

```python
        patterns = [(t, p, phrase_pattern(p)) for t, p in phrases]
```

and the haystack line with `haystack = nfc(f"{doc.title}\n{doc.text}")`. Keep `import re` (still used by `_FIELD`).

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_tagging.py tests/test_grounding_rules.py tests/test_grounding.py -q`
Expected: all PASS (grounding behaviour unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/tagging.py src/kbforge/grounding.py tests/test_tagging.py
git commit -m "refactor: one literal phrase matcher for grounding rules and keyword tags (#40)"
```

---

### Task 2: `tags` as a kbforge-owned key, bound across both carriers

**Files:**
- Modify: `src/kbforge/models.py` (`ConceptFrontmatter`)
- Modify: `src/kbforge/synthesize.py` (`OKF_OWNED`, new `_source_tags`, `assemble`, `_render`)
- Modify: `src/kbforge/validate.py` (`_check_strict_okf`, `_check_carriers_agree`)
- Modify: `packages/okfquery/src/okfquery/parse.py` (comment above `OKF_OWNED` only)
- Test: `tests/test_synthesize.py`, `tests/test_strict_okf.py`

**Interfaces:**
- Produces: `ConceptFrontmatter.tags: list[str]` (default `[]`); `synthesize._source_tags(structured: dict) -> list[str]`; `assemble(..., tags: dict[str, list[str]] | None = None)` keyword — extra tags per `doc_id`, unioned with source tags.

- [ ] **Step 1: Write the failing synthesize tests**

Append to `tests/test_synthesize.py` (reuse that file's existing doc-building helper; if it has none, build a `CanonicalDocument` inline as in `tests/test_pipeline.py::_doc`):

```python
import yaml

from kbforge.models import ChangeSet
from kbforge.synthesize import _facets, _source_tags, assemble, concept_path


def _front(text: str) -> dict:
    return yaml.safe_load(text.split("---\n")[1])


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("sic", ["sic"]),
        (["sic", "sic", "", "  ", 3, "800v"], ["800v", "sic"]),
        ([" gan "], ["gan"]),
        (None, []),
        ({"a": 1}, []),
    ],
)
def test_source_tags_are_normalized(raw, expected):
    assert _source_tags({"tags": raw}) == expected


def test_tags_are_no_longer_a_facet():
    assert "tags" not in _facets({"tags": ["sic"], "owner": "x"})


def test_assemble_ships_source_union_extra_tags_in_both_carriers():
    doc = _doc_with(structured={"tags": ["sic", "gan"]})  # helper below
    change = assemble(
        [(doc, doc.title, doc.title, doc.text)],
        ChangeSet(added=[doc.doc_id]),
        tags={doc.doc_id: ["800v", "sic"]},
    )
    path = concept_path(doc.doc_id)
    assert change.concepts[path].tags == ["800v", "gan", "sic"]
    assert _front(change.files[path])["tags"] == ["800v", "gan", "sic"]


def test_no_tags_renders_no_tags_key():
    doc = _doc_with(structured={})
    change = assemble([(doc, doc.title, doc.title, doc.text)], ChangeSet(added=[doc.doc_id]))
    assert "tags" not in _front(change.files[concept_path(doc.doc_id)])
```

with a local helper (skip if the file already has an equivalent):

```python
def _doc_with(structured: dict) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(system="sys", native_id="x.md",
                              retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                              content_hash="h"),
        doc_id="sys:x.md", title="X", text="X text.", structured=structured,
    )  # fmt: skip
    return doc
```

- [ ] **Step 2: Write the failing validator tests**

Append to `tests/test_strict_okf.py`:

```python
def _tagged(tags_line: str) -> str:
    return (
        "---\ntype: concept\ntitle: X\ndescription: X\n"
        f"{_GOOD_GENERATED}\n{_GOOD_SOURCES}\n{tags_line}---\n# X\n"
    )


def _with_tags(tags: list[str]) -> ConceptFrontmatter:
    concept = _concept()
    concept.tags = tags
    return concept


def test_rendered_tags_not_in_the_projection_are_reported():
    change = ProposedChange(
        branch_hint="b",
        files={"c.md": _tagged("tags: [sic]\n")},
        concepts={"c.md": _with_tags([])},
    )
    messages = [f.message for f in run_validators(change)]
    assert any("rendered 'tags' disagree with the projection's" in m for m in messages), messages


@pytest.mark.parametrize(
    "line, why",
    [("tags: sic\n", "a string"), ("tags: [sic, '']\n", "a blank tag"),
     ("tags: [sic, 3]\n", "a number")],
)  # fmt: skip
def test_malformed_rendered_tags_are_reported(line, why):
    change = ProposedChange(
        branch_hint="b", files={"c.md": _tagged(line)}, concepts={"c.md": _with_tags(["sic"])}
    )
    messages = [f.message for f in run_validators(change)]
    assert any("rendered 'tags' must be a list of non-blank strings" in m for m in messages), (why, messages)


def test_matching_tags_pass():
    change = ProposedChange(
        branch_hint="b", files={"c.md": _tagged("tags: [sic]\n")}, concepts={"c.md": _with_tags(["sic"])}
    )
    assert run_validators(change) == []
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_synthesize.py tests/test_strict_okf.py -q`
Expected: FAIL — `ImportError: cannot import name '_source_tags'`, and `ConceptFrontmatter` rejects `tags` (`extra="forbid"`).

- [ ] **Step 4: Implement the projection field**

`src/kbforge/models.py`, in `ConceptFrontmatter` after `links`:

```python
    tags: list[str] = Field(default_factory=list)  # OKF §4.1; bound by validate
```

- [ ] **Step 5: Implement the emit side**

`src/kbforge/synthesize.py`:

```python
OKF_OWNED = frozenset(
    {"type", "title", "description", "generated", "sources", "links", "tags"}
)
```

Update the comment above it: `tags` joins because more than one writer (the source and a synthesizer) now produces it, so a facet copy would shadow the bound value.

Add below `_facets`:

```python
def _source_tags(structured: dict) -> list[str]:
    """A source's own `tags`, normalized: a string is one tag, non-strings and
    blanks are dropped. Normalized rather than rejected -- a source's tags are
    its data, and one odd value must not fail the whole concept."""
    raw = structured.get("tags")
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})
```

In `assemble`, add the keyword parameter `tags: dict[str, list[str]] | None = None` and set, in the `ConceptFrontmatter(...)` call:

```python
            tags=sorted(
                set(_source_tags(doc.structured)) | set((tags or {}).get(doc.doc_id, []))
            ),
```

In `_render`, after the `links` block:

```python
    if fm.tags:
        front["tags"] = fm.tags
```

- [ ] **Step 6: Implement the gate side**

`src/kbforge/validate.py` — in `_check_strict_okf`, after `failures += _check_sources_shape(...)`:

```python
        failures += _check_tags_shape(path, front)
```

New function:

```python
def _check_tags_shape(path: str, front: dict) -> list[Failure]:
    """OKF §4.1: `tags` is a list of short strings. The file is what ships, so
    the file is what is checked; `_check_carriers_agree` binds it to the
    projection."""
    tags = front.get("tags")
    if tags is None:
        return []
    if not isinstance(tags, list) or not all(
        isinstance(t, str) and t.strip() for t in tags
    ):
        return [
            Failure(
                path,
                "okf-strict",
                "rendered 'tags' must be a list of non-blank strings (OKF §4.1)",
            )
        ]
    return []
```

In `_check_carriers_agree`, after the `links` check:

```python
    if front.get("tags", []) != concept.tags:
        failures.append(
            Failure(
                path,
                "okf-strict",
                "rendered 'tags' disagree with the projection's; a vocabulary or "
                "filter check reads the projection, so a tag only in the file is "
                "never checked",
            )
        )
```

- [ ] **Step 7: okfquery comment**

`packages/okfquery/src/okfquery/parse.py`, replace the comment above `OKF_OWNED` with:

```python
# The keys OKF owns at the head of a concept. Everything else in the frontmatter
# is a facet. Mirrors kbforge's synthesize.OKF_OWNED except for `tags`: kbforge
# owns `tags` so only it writes the key, but to a reader `tags` is a filterable
# list like any facet (`okfquery index --group-by tags`, `okfquery related`).
```

- [ ] **Step 8: Run everything**

Run: `uv run pytest -q`
Expected: PASS. If an existing test asserted a `tags` facet (grep `tests/` for `"tags"`), update it to the new top-level key and say so in the commit message.

- [ ] **Step 9: Commit, then mutation-check**

```bash
git add -A src/kbforge tests packages/okfquery/src/okfquery/parse.py
git commit -m "feat: tags is a kbforge-owned key, bound across file and projection (#40)"
```

Then, each on its own, mutate `src/kbforge/validate.py` in place, run `uv run pytest tests/test_strict_okf.py -q`, and restore with `git checkout -- src/kbforge/validate.py`:
1. delete the `front.get("tags", []) != concept.tags` block → `test_rendered_tags_not_in_the_projection_are_reported` must FAIL;
2. make `_check_tags_shape` `return []` → all three `test_malformed_rendered_tags_are_reported` cases must FAIL.

---

### Task 3: The `_described/` record, its sidecar, and the redo snapshot

**Files:**
- Modify: `src/kbforge/models.py` (new `DescribedRecord`, `ProposedChange.described`)
- Create: `src/kbforge/described.py`
- Modify: `src/kbforge/grounding.py` (rename `_write_atomic` → `write_atomic`, 3 call sites)
- Modify: `src/kbforge/chunking.py` (`owned_paths`)
- Test: `tests/test_described.py`, `tests/test_chunking.py`

**Interfaces:**
- Produces: `models.DescribedRecord(doc_id: str, content_hash: str, actor: str, description: str, tags: list[str] = [])`; `ProposedChange.described: dict[str, DescribedRecord]` (keyed by concept path, default `{}`); `described.DESCRIBED_DIR = "_described"`; `described.read_described(mirror: Path, doc_id: str) -> DescribedRecord | None`; `described.write_described(mirror: Path, record: DescribedRecord) -> None`; `described.delete_described(mirror: Path, doc_id: str) -> None`; `grounding.write_atomic(path: Path, payload: dict) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_described.py
from pathlib import Path

import pytest

from kbforge.described import DESCRIBED_DIR, delete_described, read_described, write_described
from kbforge.mirror import slot_key
from kbforge.models import DescribedRecord

REC = DescribedRecord(doc_id="sys:x.md", content_hash="h1", actor="kbforge/m",
                      description="One sentence.", tags=["sic"])  # fmt: skip


def test_round_trip(tmp_path: Path):
    write_described(tmp_path, REC)
    assert read_described(tmp_path, "sys:x.md") == REC
    assert (tmp_path / DESCRIBED_DIR / f"{slot_key('sys:x.md')}.json").exists()


def test_absent_is_none(tmp_path: Path):
    assert read_described(tmp_path / "no-mirror-yet", "sys:x.md") is None


@pytest.mark.parametrize(
    "content",
    ["{not json", '{"doc_id": "sys:x.md"}', "[]", '{"doc_id": 1, "tags": "x"}', "\udcff"],
)
def test_an_unreadable_record_is_a_miss_not_an_error(tmp_path: Path, content: str):
    path = tmp_path / DESCRIBED_DIR / f"{slot_key('sys:x.md')}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("utf-8", "surrogateescape"))
    assert read_described(tmp_path, "sys:x.md") is None


def test_delete_is_idempotent(tmp_path: Path):
    write_described(tmp_path, REC)
    delete_described(tmp_path, "sys:x.md")
    delete_described(tmp_path, "sys:x.md")
    assert read_described(tmp_path, "sys:x.md") is None
```

Append to `tests/test_chunking.py`:

```python
def test_owned_paths_cover_the_described_sidecar():
    from kbforge.chunking import owned_paths
    from kbforge.mirror import slot_key

    assert f"_described/{slot_key('sys:x.md')}.json" in owned_paths("sys:x.md")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_described.py tests/test_chunking.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.described'`.

- [ ] **Step 3: Implement the model**

`src/kbforge/models.py`, above `ProposedChange`:

```python
class DescribedRecord(BaseModel):
    """What a describing synthesizer's model wrote for one document, cached by
    the pipeline in `mirror/_described/` after a successful publish. `tags` are
    the MODEL's tags only: source and keyword tags are recomputed each render."""

    doc_id: str
    content_hash: str
    actor: str
    description: str
    tags: list[str] = Field(default_factory=list)
```

and in `ProposedChange` after `concepts`:

```python
    described: dict[str, DescribedRecord] = Field(default_factory=dict)
    """Keyed by concept path. Persisted by the pipeline after publish, only for
    paths in `files` whose record names the document that path belongs to; a
    synthesizer never writes mirror state itself."""
```

- [ ] **Step 4: Implement the sidecar**

In `src/kbforge/grounding.py` rename `_write_atomic` to `write_atomic` (definition, its docstring mention in `write_sidecar`, and the two calls). Then:

```python
# src/kbforge/described.py
"""The `_described/` cache: what a model wrote for a document, keyed by its
content hash, so a concept re-rendered without a source change (a referrer, an
arrival) reuses it rather than calling the model and churning the frontmatter.

Same rules as the grounding sidecar: written by the pipeline after a successful
publish, atomically; read tolerantly, because an unreadable record is a cache
miss whose repair -- describe again -- is exactly what a miss does."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from kbforge.grounding import write_atomic
from kbforge.mirror import slot_key
from kbforge.models import DescribedRecord

DESCRIBED_DIR = "_described"
"""A subdirectory: `load_all` globs `mirror/*.json`."""


def _path(mirror: Path, doc_id: str) -> Path:
    return mirror / DESCRIBED_DIR / f"{slot_key(doc_id)}.json"


def read_described(mirror: Path, doc_id: str) -> DescribedRecord | None:
    path = _path(mirror, doc_id)
    if not path.exists():
        return None
    try:
        return DescribedRecord.model_validate(json.loads(path.read_text("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        return None


def write_described(mirror: Path, record: DescribedRecord) -> None:
    write_atomic(_path(mirror, record.doc_id), record.model_dump())


def delete_described(mirror: Path, doc_id: str) -> None:
    _path(mirror, doc_id).unlink(missing_ok=True)
```

`src/kbforge/chunking.py` — import `DESCRIBED_DIR` from `kbforge.described` and extend `owned_paths`:

```python
    """Every mirror-relative file a run writes or deletes on behalf of `doc_id`:
    its slot, its grounding sidecar, its first-seen record, its described record."""
    name = f"{slot_key(doc_id)}.json"
    return [
        name,
        f"{SIDECAR_DIR}/{name}",
        f"{FIRST_SEEN_DIR}/{name}",
        f"{DESCRIBED_DIR}/{name}",
    ]
```

(If importing `kbforge.described` from `chunking` creates a cycle, move `DESCRIBED_DIR` next to `SIDECAR_DIR` in `grounding.py` and import it from there in both modules.)

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A src/kbforge tests
git commit -m "feat: _described/ sidecar record, tolerant reads, covered by redo (#40)"
```

---

### Task 4: `DescribeSynthesizer`

**Files:**
- Modify: `src/kbforge/llm_synthesizer.py`
- Modify: `src/kbforge/synthesize.py` (`assemble` gains `actors=`)
- Test: `tests/test_describe_synthesizer.py`

**Interfaces:**
- Consumes: `tagging.keyword_tags`, `described.read_described`, `models.DescribedRecord`, `assemble(..., tags=...)` (Tasks 1-3).
- Produces: `DescribeConfig(LLMConfig)`; `DescribedConcept(description: str, tags: list[str])`; `DescribeSynthesizer(config: DescribeConfig, *, mirror: Path | None = None, agent=None)` with `grounds = False`, `_build_agent(config, model=None)` (static), `synthesize(changed_docs, changeset, existing_paths=frozenset()) -> ProposedChange`; `assemble(..., actors: dict[str, str] | None = None)` — per-`doc_id` override of `generated_by`; module function `_run_agent(agent, config, doc, prompt)` shared by both LLM synthesizers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_describe_synthesizer.py
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.described import write_described  # noqa: E402
from kbforge.llm_synthesizer import (  # noqa: E402
    DescribeConfig,
    DescribeSynthesizer,
    SynthesisError,
)
from kbforge.models import CanonicalDocument, ChangeSet, DescribedRecord, ResourceAnchor  # noqa: E402
from kbforge.synthesize import StubSynthesizer, concept_path  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402

VOCAB = {"sic": ["SiC"], "800v": ["800 V"], "gan": []}
DOC_ID = "sys:q3.md"
PATH = concept_path(DOC_ID)


def _doc(text="SiC MOSFETs at 800 V.", structured=None, h="h1"):
    return CanonicalDocument(
        anchor=ResourceAnchor(system="sys", native_id="q3.md",
                              retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                              content_hash=h),
        doc_id=DOC_ID, title="Q3 report", text=text, structured=structured or {},
    )  # fmt: skip


def _model(outputs: list[dict], calls: list[int]) -> FunctionModel:
    it = iter(outputs)

    def fn(messages, info: AgentInfo):
        calls.append(1)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(it))])

    return FunctionModel(fn)


def _synth(outputs, calls, mirror=None, **cfg) -> DescribeSynthesizer:
    config = DescribeConfig(**{"tags_vocabulary": VOCAB, **cfg})
    agent = DescribeSynthesizer._build_agent(config, model=_model(outputs, calls))
    return DescribeSynthesizer(config, mirror=mirror, agent=agent)


def _front(text: str) -> dict:
    return yaml.safe_load(text.split("---\n")[1])


def _run(synth, doc=None):
    doc = doc or _doc()
    return synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))


GOOD = {"description": "A Q3 report on SiC MOSFETs for 800 V platforms.", "tags": ["gan"]}


def test_body_is_byte_identical_to_the_stub():
    doc = _doc()
    stub = StubSynthesizer().synthesize([doc], ChangeSet(added=[DOC_ID])).files[PATH]
    got = _run(_synth([GOOD], []), doc).files[PATH]
    assert got.split("\n---\n", 1)[1] == stub.split("\n---\n", 1)[1]


def test_description_actor_and_tag_union():
    change = _run(_synth([GOOD], []), _doc(structured={"tags": ["roadmap"]}))
    front = _front(change.files[PATH])
    assert front["description"] == GOOD["description"]
    assert front["generated"]["by"] == "kbforge/deepseek-v4-flash"
    # source ∪ keyword (sic, 800v) ∪ model (gan)
    assert front["tags"] == ["800v", "gan", "roadmap", "sic"]
    assert change.described[PATH].tags == ["gan"]  # model tags only
    assert run_validators(change) == []


def test_a_tag_outside_the_vocabulary_retries_then_names_the_tag():
    bad = {"description": "Fine.", "tags": ["SiC"]}  # case variant of `sic`
    with pytest.raises(SynthesisError) as err:
        _run(_synth([bad] * 3, []))
    assert PATH in str(err.value) and "'SiC'" in str(err.value), str(err.value)


def test_a_bad_tag_then_a_good_answer_succeeds():
    calls: list[int] = []
    change = _run(_synth([{"description": "Fine.", "tags": ["nope"]}, GOOD], calls))
    assert len(calls) == 2 and change.described[PATH].description == GOOD["description"]


def test_duplicate_model_tags_collapse():
    change = _run(_synth([{"description": "Fine.", "tags": ["gan", "gan"]}], []))
    assert change.described[PATH].tags == ["gan"]


@pytest.mark.parametrize(
    "description, why",
    [("", "blank"), ("Two\nlines.", "multi-line"), ("x" * 241, "over the cap")],
)
def test_a_bad_description_retries_then_fails(description, why):
    with pytest.raises(SynthesisError) as err:
        _run(_synth([{"description": description, "tags": []}] * 3, []))
    assert PATH in str(err.value), why


def test_model_tags_off_means_no_tag_request_and_keyword_tags_still_apply():
    change = _run(_synth([{"description": "Fine.", "tags": []}], [], model_tags=False))
    assert _front(change.files[PATH])["tags"] == ["800v", "sic"]


def test_yaml_special_description_still_validates():
    tricky = {"description": "Key: value # not a comment, 'quoted' --- still one line.", "tags": []}
    change = _run(_synth([tricky], []))
    assert _front(change.files[PATH])["description"] == tricky["description"]
    assert run_validators(change) == []


def test_a_cache_hit_makes_no_model_call_and_keeps_the_stored_actor(tmp_path: Path):
    write_described(tmp_path, DescribedRecord(doc_id=DOC_ID, content_hash="h1",
                    actor="kbforge/old-model", description="Cached.", tags=["gan"]))  # fmt: skip
    calls: list[int] = []
    change = _run(_synth([], calls, mirror=tmp_path))
    front = _front(change.files[PATH])
    assert calls == []
    assert front["description"] == "Cached." and front["generated"]["by"] == "kbforge/old-model"


@pytest.mark.parametrize(
    "record_hash, record_tags, why",
    [("h0", ["gan"], "the source changed"), ("h1", ["dropped"], "vocabulary narrowed")],
)
def test_a_stale_cache_is_a_miss(tmp_path: Path, record_hash, record_tags, why):
    write_described(tmp_path, DescribedRecord(doc_id=DOC_ID, content_hash=record_hash,
                    actor="kbforge/m", description="Old.", tags=record_tags))  # fmt: skip
    calls: list[int] = []
    change = _run(_synth([GOOD], calls, mirror=tmp_path))
    assert calls == [1], why
    assert change.described[PATH].description == GOOD["description"]


def test_describe_does_not_ground():
    assert DescribeSynthesizer.grounds is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_describe_synthesizer.py -q`
Expected: FAIL — `ImportError: cannot import name 'DescribeConfig'`.

- [ ] **Step 3: `assemble` gains per-document actors**

`src/kbforge/synthesize.py`, `assemble` signature: add `actors: dict[str, str] | None = None` (keyword). In the `ConceptFrontmatter(...)` call:

```python
            generated_by=(actors or {}).get(doc.doc_id, generated_by),
```

- [ ] **Step 4: Extract the shared run helper, with the retry reason**

In `src/kbforge/llm_synthesizer.py`, turn `LLMSynthesizer._run` into a module function and make `LLMSynthesizer._run` call it:

```python
def _run_agent(
    agent: Agent[Any, Any], config: LLMConfig, doc: CanonicalDocument, prompt: str
) -> Any:
    from pydantic_ai import capture_run_messages
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart

    with capture_run_messages() as messages:
        try:
            return agent.run_sync(prompt).output
        except UnexpectedModelBehavior as exc:
            attempts = [m for m in messages if isinstance(m, ModelResponse)]
            where = f"{concept_path(doc.doc_id)} ({config.model})"
            last = attempts[-1].usage.output_tokens if attempts else 0
            if last >= config.max_tokens:
                reason = (
                    f"model output hit max_tokens={config.max_tokens} "
                    "and was cut off; raise it with "
                    f"--llm-set max_tokens={2 * config.max_tokens}"
                )
            else:
                retries = [
                    p
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, RetryPromptPart)
                ]
                # The last retry prompt says what was wrong (a tag outside the
                # vocabulary, a description over the cap); `exc` only says
                # retries ran out.
                why = f"; last problem: {retries[-1].model_response()}" if retries else ""
                reason = (
                    f"model returned invalid output in {len(attempts)} "
                    f"attempts: {exc}{why}"
                )
            raise SynthesisError(f"{where}: {reason}") from exc
```

Keep the existing comment about `finish_reason=tool_call` on the `last >= ...` branch. `LLMSynthesizer._run(self, doc, prompt)` becomes `return _run_agent(self.agent, self.config, doc, prompt)`. The existing `tests/test_llm_synthesizer.py` truncation/invalid tests must still pass unchanged.

- [ ] **Step 5: Implement config, output model and synthesizer**

Append to `src/kbforge/llm_synthesizer.py` (add `from pathlib import Path` and imports of `keyword_tags`, `read_described`, `DescribedRecord`):

```python
_DESCRIBE_INSTRUCTIONS = (
    "You describe one source document for a knowledge-base index. Write ONLY "
    "from the provided text; add no outside knowledge and invent no facts. "
    "Return `description`: ONE sentence, on one line, saying what the document "
    "is and what it covers, so a reader can decide whether to open it."
)


@dataclass
class DescribeConfig(LLMConfig):
    instructions: str = ""
    tags_vocabulary: dict[str, list[str]] | None = None
    model_tags: bool = True
    description_max_chars: int = 240

    def validate_env(self) -> list[str]:
        problems = super().validate_env()
        if self.description_max_chars <= 0:
            problems.append("description_max_chars must be positive")
        for tag, phrases in (self.tags_vocabulary or {}).items():
            if not str(tag).strip():
                problems.append("tags_vocabulary has a blank tag")
            if not isinstance(phrases, list) or not all(
                isinstance(p, str) and p.strip() for p in phrases
            ):
                problems.append(
                    f"tags_vocabulary[{tag!r}] must be a list of non-blank phrases"
                )
        return problems

    @property
    def allowed_tags(self) -> frozenset[str]:
        """The tags the MODEL may return; empty when it may return none."""
        if not (self.tags_vocabulary and self.model_tags):
            return frozenset()
        return frozenset(self.tags_vocabulary)


class DescribedConcept(BaseModel):
    """The ONLY thing the describing model produces."""

    description: str
    tags: list[str] = Field(default_factory=list)


def _describe_instructions(config: DescribeConfig) -> str:
    parts = [_DESCRIBE_INSTRUCTIONS]
    if config.allowed_tags:
        parts.append(
            "Return `tags`: the tags from this list that apply, spelled exactly "
            f"as listed, or none: {', '.join(sorted(config.allowed_tags))}."
        )
    else:
        parts.append("Return `tags` as an empty list.")
    if config.instructions.strip():
        parts.append(config.instructions.strip())
    return "\n\n".join(parts)


class DescribeSynthesizer:
    """Stub body, model-written one-sentence description, keyword + model tags
    (design/2026-09-23-describe-synthesizer-design.md)."""

    grounds = False
    """The description is written from the document alone; citing grounding
    documents would claim a provenance the concept does not have (§7.1)."""

    def __init__(
        self,
        config: DescribeConfig,
        *,
        mirror: Path | None = None,
        agent: Agent[Any, Any] | None = None,
    ) -> None:
        self.config = config
        self.mirror = mirror
        self.agent: Agent[Any, Any] = (
            agent if agent is not None else self._build_agent(config)
        )
        # Registered here, not in `_build_agent`, so an injected agent is held
        # to the same checks as a built one.
        self.agent.output_validator(self._check)

    @staticmethod
    def _build_agent(config: DescribeConfig, model: Model | None = None) -> Agent[Any, Any]:
        try:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.litellm import LiteLLMProvider
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - guarded by the extra
            raise ImportError(
                "DescribeSynthesizer requires the LLM extra: pip install 'kbforge[llm]'"
            ) from exc
        if model is None:
            model = OpenAIChatModel(
                config.model,
                provider=LiteLLMProvider(
                    api_base=config.api_base,
                    api_key=os.environ.get(config.api_key_env),
                ),
            )
        output = DescribedConcept
        if config.output_mode != "tool":
            from pydantic_ai import NativeOutput, PromptedOutput

            wrap = NativeOutput if config.output_mode == "native" else PromptedOutput
            output = wrap(DescribedConcept)
        return Agent(
            model,
            output_type=output,
            retries=config.output_retries,
            instructions=_describe_instructions(config),
            model_settings=ModelSettings(
                temperature=config.temperature, max_tokens=config.max_tokens
            ),
        )

    def _check(self, out: DescribedConcept) -> DescribedConcept:
        from pydantic_ai import ModelRetry

        text = out.description.strip()
        cap = self.config.description_max_chars
        if not text:
            raise ModelRetry("`description` is empty; write one sentence.")
        if "\n" in text:
            raise ModelRetry("`description` must be ONE sentence on one line.")
        if len(text) > cap:
            raise ModelRetry(f"`description` is {len(text)} chars; keep it under {cap}.")
        allowed = self.config.allowed_tags
        bad = sorted({t for t in out.tags if t not in allowed})
        if bad:
            listed = ", ".join(sorted(allowed)) or "(none: return an empty list)"
            raise ModelRetry(f"tags {bad} are not allowed; choose only from: {listed}")
        return DescribedConcept(description=text, tags=sorted(set(out.tags)))

    def _cached(self, doc: CanonicalDocument) -> DescribedRecord | None:
        if self.mirror is None:
            return None
        record = read_described(self.mirror, doc.doc_id)
        if (
            record is None
            or record.content_hash != doc.anchor.content_hash
            or not set(record.tags) <= self.config.allowed_tags
        ):
            return None
        return record

    def _prompt(self, doc: CanonicalDocument, notes: list[str]) -> str:
        text = doc.text
        if len(text) > self.config.max_source_chars:
            text = text[: self.config.max_source_chars]
            notes.append(
                f"{concept_path(doc.doc_id)}: source truncated to "
                f"{self.config.max_source_chars} chars before description"
            )
        return f"Source title: {doc.title}\n\nSource text:\n{text}"

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        tags: dict[str, list[str]] = {}
        actors: dict[str, str] = {}
        records: dict[str, DescribedRecord] = {}
        notes: list[str] = []
        vocabulary = self.config.tags_vocabulary
        for doc in changed_docs:
            record = self._cached(doc)
            if record is None:
                out = _run_agent(self.agent, self.config, doc, self._prompt(doc, notes))
                record = DescribedRecord(
                    doc_id=doc.doc_id,
                    content_hash=doc.anchor.content_hash,
                    actor=actor_for(self.config.model),
                    description=out.description,
                    tags=out.tags,
                )
            items.append((doc, doc.title, record.description, doc.text))
            tags[doc.doc_id] = sorted(
                set(record.tags) | set(keyword_tags(vocabulary, doc.title, doc.text))
            )
            actors[doc.doc_id] = record.actor
            records[concept_path(doc.doc_id)] = record
        proposal = assemble(items, changeset, existing_paths, tags=tags, actors=actors)
        proposal.described = records
        proposal.summary.grounding_notes.extend(notes)
        return proposal
```

Note: the body passed is `doc.text`, exactly what `StubSynthesizer` passes, which is what makes the byte-identity test hold.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_describe_synthesizer.py tests/test_llm_synthesizer.py tests/test_synthesize.py -q`
Expected: PASS. If `test_a_tag_outside_the_vocabulary_retries_then_names_the_tag` fails because `RetryPromptPart.model_response()` renders differently in pydantic-ai 2.13, print `str(err.value)`, adjust `_run_agent` to use `retries[-1].content` when it is a `str`, and keep the assertion on the tag name.

- [ ] **Step 7: Commit, then mutation-check**

```bash
git add -A src/kbforge tests
git commit -m "feat: DescribeSynthesizer — stub body, one-sentence description, keyword + model tags (#40)"
```

Each alone, in place, restored with `git checkout -- src/kbforge/llm_synthesizer.py`, running `uv run pytest tests/test_describe_synthesizer.py -q`:
1. `_cached`: drop the `content_hash` comparison → `test_a_stale_cache_is_a_miss[h0-...]` FAILS;
2. `_cached`: drop the `allowed_tags` subset check → `test_a_stale_cache_is_a_miss[h1-...]` FAILS;
3. `_check`: remove the `bad` check → the vocabulary test FAILS;
4. `synthesize`: pass `actors={}` → the cache-hit actor test FAILS.

---

### Task 5: The pipeline persists, clears and tombstones `_described/`

**Files:**
- Modify: `src/kbforge/pipeline.py` (post-publish block, ~lines 685-722)
- Test: `tests/test_pipeline_describe.py`

**Interfaces:**
- Consumes: `ProposedChange.described`, `write_described`, `delete_described`, `read_described` (Task 3), `DescribeSynthesizer` (Task 4).
- Produces: after a successful publish, for every doc in `changed_docs` whose path is in `proposal.files`: a record is written iff `proposal.described[path].doc_id == doc.doc_id`, else any existing record is deleted; every `changeset.removed` id's record is deleted.

- [ ] **Step 1: Write the failing tests**

Reuse `tests/test_pipeline.py`'s `_doc`, `_FakeConnector`, `_RecordingPublisher`, `_run_result` by importing them (`from tests.test_pipeline import ...` if `tests/` is a package; otherwise copy the four helpers verbatim into this file).

```python
# tests/test_pipeline_describe.py
from pathlib import Path

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.described import read_described  # noqa: E402
from kbforge.llm_synthesizer import DescribeConfig, DescribeSynthesizer  # noqa: E402
from kbforge.pipeline import NoOp, run  # noqa: E402
from kbforge.synthesize import StubSynthesizer  # noqa: E402

# helpers: _doc, _FakeConnector, _RecordingPublisher, _run_result (see note above)


def _describer(mirror: Path, calls: list[str]) -> DescribeSynthesizer:
    def fn(messages, info: AgentInfo):
        calls.append("call")
        return ModelResponse(parts=[ToolCallPart(
            info.output_tools[0].name, {"description": "One sentence.", "tags": []})])

    config = DescribeConfig()
    agent = DescribeSynthesizer._build_agent(config, model=FunctionModel(fn))
    return DescribeSynthesizer(config, mirror=mirror, agent=agent)


def test_publish_writes_the_record_and_an_unchanged_rerun_is_a_free_noop(tmp_path):
    calls: list[str] = []
    docs = [_doc("a.md", "A")]
    _run_result(tmp_path, docs, synthesizer=_describer(tmp_path / "mirror", calls))
    assert read_described(tmp_path / "mirror", "sys:a.md").description == "One sentence."
    result, _ = _run_result(tmp_path, docs, synthesizer=_describer(tmp_path / "mirror", calls))
    assert isinstance(result, NoOp) and calls == ["call"]


def test_a_referrer_rebuild_reuses_the_record(tmp_path):
    calls: list[str] = []
    mirror = tmp_path / "mirror"
    ref = _doc("ref.md", "Ref", relations=["sys:later.md"])
    _run_result(tmp_path, [ref], synthesizer=_describer(mirror, calls))
    assert calls == ["call"]
    _, pub = _run_result(tmp_path, [ref, _doc("later.md", "Later")],
                         synthesizer=_describer(mirror, calls))  # fmt: skip
    assert "concepts/ref/overview.md" in pub.last_change.files  # rebuilt (arrival)
    assert calls == ["call", "call"], "only `later` may call the model"


def test_a_tombstone_deletes_the_record(tmp_path):
    mirror = tmp_path / "mirror"
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=_describer(mirror, []))
    _run_result(tmp_path, [_doc("a.md", "A", deleted=True)], synthesizer=_describer(mirror, []))
    assert read_described(mirror, "sys:a.md") is None


def test_a_rebuild_without_a_record_clears_the_stale_one(tmp_path):
    mirror = tmp_path / "mirror"
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=_describer(mirror, []))
    _run_result(tmp_path, [_doc("a.md", "A", text="changed")], synthesizer=StubSynthesizer())
    assert read_described(mirror, "sys:a.md") is None


def test_a_record_for_another_documents_path_is_not_written(tmp_path):
    class Forging(DescribeSynthesizer):
        def synthesize(self, *a, **k):
            change = super().synthesize(*a, **k)
            for rec in change.described.values():
                rec.doc_id = "sys:someone-else.md"
            return change

    mirror = tmp_path / "mirror"
    base = _describer(mirror, [])
    forging = Forging(base.config, mirror=mirror, agent=base.agent)
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=forging)
    assert read_described(mirror, "sys:someone-else.md") is None
    assert read_described(mirror, "sys:a.md") is None


def test_a_failed_publish_writes_no_record(tmp_path):
    class Boom:
        def kbforge_publisher_info(self):
            from kbforge.models import ConnectorInfo
            return ConnectorInfo(name="boom", version="0", source_system="t")

        def kbforge_publish(self, change, config):
            raise RuntimeError("publish failed")

    mirror = tmp_path / "mirror"
    with pytest.raises(RuntimeError):
        run(_FakeConnector([_doc("a.md", "A")]), Boom(), config={}, mirror=str(mirror),
            state_dir=str(tmp_path / "state"), publish_config={},
            synthesizer=_describer(mirror, []))  # fmt: skip
    assert read_described(mirror, "sys:a.md") is None
```

Also add a redo test to `tests/test_pipeline_chunking.py` following that file's existing redo-test pattern: publish one chunk with `_describer`, run `redo`, assert `read_described(...)` is back to its pre-chunk state (`None` for a first chunk).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_pipeline_describe.py -q`
Expected: FAIL — no record is written (`AttributeError: 'NoneType' object has no attribute 'description'`).

- [ ] **Step 3: Implement persistence**

In `src/kbforge/pipeline.py` import `delete_described, write_described` from `kbforge.described`. Inside the existing `for doc in changed_docs:` loop after `commit(...)`, right after the `continue` for dropped documents and before the grounding-sidecar branch:

```python
        record = proposal.described.get(concept_path(doc.doc_id))
        if record is not None and record.doc_id == doc.doc_id:
            write_described(mirror_path, record)
        else:
            # Delete, not skip, for the grounding sidecar's reason: a record
            # left behind describes a concept that no longer ships its text,
            # and #41's tag reads would trust it. A record naming another
            # document is not written anywhere -- a synthesizer does not get
            # to write another concept's mirror state.
            delete_described(mirror_path, doc.doc_id)
```

In the `for doc_id in changeset.removed:` loop add `delete_described(mirror_path, doc_id)`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit, then mutation-check**

```bash
git add -A src/kbforge tests
git commit -m "feat: pipeline persists _described/ after publish, clears it on rebuild and tombstone (#40)"
```

In place, restored with `git checkout -- src/kbforge/pipeline.py`:
1. replace the `else:` delete with `pass` → `test_a_rebuild_without_a_record_clears_the_stale_one` FAILS;
2. drop `and record.doc_id == doc.doc_id` → `test_a_record_for_another_documents_path_is_not_written` FAILS.

---

### Task 6: CLI `--synthesizer describe`

**Files:**
- Modify: `src/kbforge/__main__.py` (~lines 112, 226-244, 265-269)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `DescribeConfig`, `DescribeSynthesizer` (Task 4).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (it already defines `DOC` and `_plumbing`):

```python
def test_run_describe_synthesizer_offline(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kbforge import llm_synthesizer

    real = llm_synthesizer.DescribeSynthesizer._build_agent

    def fake_agent(config, model=None):
        def fn(messages, info: AgentInfo):
            return ModelResponse(parts=[ToolCallPart(
                info.output_tools[0].name, {"description": "Says what X is.", "tags": []})])

        return real(config, model=FunctionModel(fn))

    monkeypatch.setattr(
        llm_synthesizer.DescribeSynthesizer, "_build_agent", staticmethod(fake_agent)
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(["run", "--connector", "local_files", "--set", f"path={src}",
                 "--synthesizer", "describe",
                 "--llm-set", "tags_vocabulary={x: [App X]}",
                 *_plumbing(tmp_path)])  # fmt: skip
    assert code == 0 and "Published" in capsys.readouterr().out
    text = (tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md").read_text()
    assert "description: Says what X is." in text and "- x" in text


def test_describe_rejects_a_bad_vocabulary(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    code = main(["run", "--connector", "local_files", "--set", f"path={tmp_path}",
                 "--synthesizer", "describe", "--llm-set", "tags_vocabulary={x: [' ']}",
                 *_plumbing(tmp_path)])  # fmt: skip
    assert code == 2
    assert "tags_vocabulary['x'] must be a list of non-blank phrases" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -q -k describe`
Expected: FAIL — argparse `invalid choice: 'describe'` (exit 2 via `SystemExit`).

- [ ] **Step 3: Implement**

`--synthesizer` choices become `["stub", "llm", "describe"]`, help `"stub (default), llm, or describe (stub body + model-written description and tags)"`. Replace the `if args.synthesizer == "llm":` block with:

```python
    if args.synthesizer in ("llm", "describe"):
        from kbforge.llm_synthesizer import (
            DescribeConfig,
            DescribeSynthesizer,
            LLMConfig,
            LLMSynthesizer,
        )

        config_cls = DescribeConfig if args.synthesizer == "describe" else LLMConfig
        try:
            llm_cfg = config_cls(**_parse_settings(args.llm_settings))
        except (ValueError, TypeError) as exc:
            print(str(exc))
            return 2
        problems = llm_cfg.validate_env()
        if problems:
            print("; ".join(problems))
            return 2
        try:
            if isinstance(llm_cfg, DescribeConfig):
                synthesizer = DescribeSynthesizer(llm_cfg, mirror=Path(args.mirror))
            else:
                synthesizer = LLMSynthesizer(llm_cfg)
        except ImportError as exc:
            print(str(exc))
            return 2
    else:
        synthesizer = None  # run() defaults to StubSynthesizer
```

and the rules warning:

```python
    if grounding_config.rules and args.synthesizer in ("stub", "describe"):
        print(
            "grounding rules are validated but inactive: the "
            f"{args.synthesizer} synthesizer does not ground; use --synthesizer llm"
        )
```

Update any existing test asserting the old warning text (`grep -rn "stub synthesizer does not ground" tests/`) to the new wording.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src/kbforge tests
git commit -m "feat(cli): --synthesizer describe (#40)"
```

---

### Task 7: okfquery sees the description; docs; live check

**Files:**
- Test: `packages/okfquery/tests/test_roundtrip.py` (or a new test beside it)
- Modify: `docs/architecture.md` (§4.4 dual-carrier notes, §7 synthesizer paragraph ~line 975, new short subsection after §7.2)
- Modify: `CLAUDE.md` ("The dual-carrier rule" key list)
- Modify: `README.md` (line ~57 synthesis bullet, Quickstart)
- Modify: `CHANGELOG.md` (`[Unreleased]`)
- Delete: `docs/design/2026-09-23-describe-synthesizer-design.md` after folding its rationale

- [ ] **Step 1: Write the failing okfquery test**

In `packages/okfquery/tests/test_roundtrip.py`, following its existing pattern of building a real kbforge bundle, add a test that builds one concept with `DescribeSynthesizer` (scripted `FunctionModel` returning `{"description": "Says what X is.", "tags": []}`) through `kbforge.pipeline.run` with the dry-run publisher, then asserts:

```python
    from okfquery.index import render_bundle

    assert "- Says what X is." in render_bundle(bundle)
```

Run: `uv run pytest packages/okfquery/tests/test_roundtrip.py -q` — it should PASS immediately (the feature is built); if it fails, the index is not reading `description` and that is a bug to fix before continuing.

- [ ] **Step 2: Fold the spec into architecture.md**

Add a subsection `### 7.3 The describe synthesizer` after §7.2, carrying from the spec only what the code does not say: why a separate synthesizer; why one sentence (OKF §4.1); why keyword + model tags; the `content_hash`-only cache and its two deliberate non-triggers (instructions, model); why the vocabulary constrains model tags only and lives outside the §4.4 laws. Update the §7 paragraph at ~line 975 to name `DescribeSynthesizer` beside `StubSynthesizer`/`LLMSynthesizer`. In CLAUDE.md's dual-carrier section change "(`type`, `links`, `generated.at`, `sources`)" to "(`type`, `links`, `tags`, `generated.at`, `sources`)". Delete the design doc.

- [ ] **Step 3: README and CHANGELOG**

README synthesis bullet: add "or `--synthesizer describe`: the stub's verbatim body with a model-written one-sentence description and tags from a vocabulary". CHANGELOG `[Unreleased]`:

```markdown
### Added

- `--synthesizer describe` (#40): keeps the stub's body byte-for-byte and has a
  model write only a one-sentence `description` (OKF §4.1), so `okfquery index`
  lines say what verbatim sources are. `tags` come from the source, from a
  `tags_vocabulary` by keyword match, and optionally from the model choosing
  among the vocabulary's tags; a model tag outside it is retried, then fails the
  run. Model output is cached in `mirror/_described/` by content hash, so an
  unchanged source makes no model call, even when its concept is re-rendered.

### Changed

- `tags` is now a kbforge-owned frontmatter key, bound to the projection like
  `links`. A source's `tags` still ship, normalized (a string becomes a list;
  blanks and non-strings are dropped), and render after `sources`/`links`.
```

- [ ] **Step 4: Full verification**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all PASS.

- [ ] **Step 5: Live check (real model)**

```bash
set -a; . ./.env; set +a
S=$(mktemp -d); mkdir $S/src
printf -- '---\ntitle: Q3 SiC report\n---\n| Device | Vds |\n|---|---|\n| SiC MOSFET | 1200 V |\n\nSiC devices for 800 V platforms.\n' > $S/src/q3.md
printf -- '---\ntitle: GaN brief\ntags: [brief]\n---\nGaN transistors for chargers.\n' > $S/src/gan.md
uv run kbforge run --connector local_files --set path=$S/src --synthesizer describe \
  --llm-set 'tags_vocabulary={sic: [SiC], 800v: ["800 V"], gan: [GaN], chargers: []}' \
  --mirror $S/mirror --state $S/state --out $S/out --publish-set out_dir=$S/out
cat $S/out/sync-local_files/concepts/q3/overview.md
uv run kbforge run --connector local_files --set path=$S/src --synthesizer describe \
  --llm-set 'tags_vocabulary={sic: [SiC], 800v: ["800 V"], gan: [GaN], chargers: []}' \
  --mirror $S/mirror --state $S/state --out $S/out --publish-set out_dir=$S/out
uv run okfquery index --bundle $S/out/sync-local_files && cat $S/out/sync-local_files/index.md
```

Expected: the first run publishes; `q3` keeps its table byte-for-byte, has a one-sentence description and tags including `800v` and `sic`; `gan` carries `brief` and `gan`; the second run prints `NoOp`; the index lines carry the descriptions. Report the actual output.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: describe synthesizer folded into architecture.md §7.3; README, CHANGELOG (#40)"
```
