# LLM Synthesizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the stub synthesizer's prose with a real, grounded Pydantic AI synthesizer, behind an injectable seam, without weakening any trust guarantee.

**Architecture:** `synthesize()` becomes a `Synthesizer` protocol with two implementations — `StubSynthesizer` (today's logic, still the default) and `LLMSynthesizer` (Pydantic AI + LiteLLMProvider). The LLM emits only `title`/`description`/`body` prose; kbforge assembles the entire structural frame (anchors, links, facets, type, timestamp) deterministically, so the §4.4 validators bite on structure the model never touches.

**Tech Stack:** Python ≥3.12, Pydantic v2, Pydantic AI (`pydantic-ai-slim[litellm]`) behind the optional `kbforge[llm]` extra, OpenRouter/self-hosted LiteLLM gateway.

**Spec:** [`../design/2026-07-19-llm-synthesizer-design.md`](../design/2026-07-19-llm-synthesizer-design.md)

## Global Constraints

- **Grounding boundary (spec §2):** the LLM output type carries ONLY `title`, `description`, `body`. Anchors, links, facets, `type`, `timestamp` are assembled deterministically by kbforge. The model never emits an anchor, link, or type.
- **Stub stays default:** `pipeline.run(...)` defaults to `StubSynthesizer`. Every existing test must keep passing unchanged.
- **Optional dependency:** Pydantic AI lives behind `kbforge[llm]`. Core, `StubSynthesizer`, and the existing test suite import nothing new. `LLMSynthesizer` imports `pydantic_ai` lazily and raises a clear "install kbforge[llm]" error if absent.
- **Tests never hit a real model:** `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` suite-wide; unit tests inject `FunctionModel`/`TestModel`. Exactly one opt-in live test, skipped by default.
- **Key handling:** the API key is ALWAYS read from the env var named by `api_key_env`; never a config value, never a CLI arg.
- **Default model:** `deepseek/deepseek-v4-flash` via `api_base=https://openrouter.ai/api/v1`, key from `OPENROUTER_API_KEY`.
- **The validator gate is absolute:** the assembled `ProposedChange` always goes through `run_validators`; a failure returns `Aborted`, no MR. No partial publish.
- **Tooling:** ruff `E/F/I/UP`, line length 88, `ty` clean (authoritative via prek). Commit messages end with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## File Structure

- `src/kbforge/synthesize.py` (modify) — `Synthesizer` protocol, `StubSynthesizer`, shared `assemble()`; `_render` gains explicit prose params. Keeps `concept_path`, `_facets`.
- `src/kbforge/llm_synthesizer.py` (create) — `SynthesizedConcept`, `LLMConfig`, `LLMSynthesizer`. Lazy `pydantic_ai` import. A flat module, not a plugin package (synthesis is a core stage, spec §10).
- `src/kbforge/pipeline.py` (modify) — `run(...)` gains `synthesizer` param.
- `src/kbforge/__main__.py` (modify) — `--synthesizer`, `--llm-set`, synthesizer construction, `list` shows synthesizers.
- `pyproject.toml` (modify) — `[project.optional-dependencies].llm`; exclude `docs/plans` from sdist.
- `tests/conftest.py` (create) — offline guard + `--run-live` option.
- `tests/test_synthesize.py` (modify), `tests/test_llm_synthesizer.py` (create), `tests/test_pipeline.py` (modify), `tests/test_cli.py` (modify), `tests/test_llm_live.py` (create).

---

## Task 1: Extract the `Synthesizer` seam (behavior-preserving refactor)

**Files:**
- Modify: `src/kbforge/synthesize.py`
- Test: `tests/test_synthesize.py`

**Interfaces:**
- Produces: `Synthesizer` (Protocol), `StubSynthesizer`, `assemble(items, changeset, existing_paths) -> ProposedChange` where `items: list[tuple[CanonicalDocument, str, str, str]]` is `(doc, title, description, body)`. `concept_path`, `_facets` unchanged. Module `synthesize()` retained, delegates to `StubSynthesizer`.

- [ ] **Step 1: Add a golden test pinning current stub output**

Add to `tests/test_synthesize.py`:

```python
from kbforge.synthesize import StubSynthesizer, synthesize


def test_stub_synthesizer_matches_module_function(_one_changed):
    docs, changeset, existing = _one_changed
    a = synthesize(docs, changeset, existing)
    b = StubSynthesizer().synthesize(docs, changeset, existing)
    assert a.model_dump() == b.model_dump()  # identical behavior
```

If `tests/test_synthesize.py` has no shared fixture producing `(changed_docs, changeset, existing_paths)`, add one named `_one_changed` built from an existing test's setup in that file (reuse the same `CanonicalDocument`/`ChangeSet` construction already present).

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_synthesize.py::test_stub_synthesizer_matches_module_function -v`
Expected: FAIL with `ImportError: cannot import name 'StubSynthesizer'`.

- [ ] **Step 3: Refactor `synthesize.py`**

Replace the `_render` and `synthesize` definitions. `_render` takes explicit prose; extract `assemble`; add the protocol, stub, and a delegating module function. Full new content from the `_SCALAR` line onward:

```python
from typing import Protocol

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
        k: v for k, v in structured.items() if v not in (None, "", [], {}) and ok(v)
    }


def _render(
    doc: CanonicalDocument,
    fm: ConceptFrontmatter,
    *,
    title: str,
    description: str,
    body: str,
) -> str:
    front: dict = {
        "type": fm.type,
        "title": title,
        "description": description,
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
    return f"---\n{head}\n---\n\n# {title}\n\n{body}\n"


def assemble(
    items: list[tuple[CanonicalDocument, str, str, str]],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
) -> ProposedChange:
    """Build the ProposedChange frame from per-doc prose (doc, title, description,
    body). Both synthesizers produce `items` differently and share this assembly, so
    the kbforge-owned structural frame is identical regardless of prose source."""
    known = {concept_path(doc.doc_id) for doc, *_ in items} | set(existing_paths)
    files: dict[str, str] = {}
    concepts: dict[str, ConceptFrontmatter] = {}
    summary = ChangeSummary()
    for doc, title, description, body in items:
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
        files[path] = _render(
            doc, fm, title=title, description=description, body=body
        )
        summary.sources_changed.append(doc.anchor)
    summary.claims_added = sorted(concept_path(x) for x in changeset.added)
    summary.claims_modified = sorted(concept_path(x) for x in changeset.modified)
    summary.claims_removed = sorted(changeset.removed)
    system = items[0][0].anchor.system if items else "source"
    return ProposedChange(
        branch_hint=f"sync/{system}",
        files=files,
        concepts=concepts,
        summary=summary,
    )


class Synthesizer(Protocol):
    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange: ...


class StubSynthesizer:
    """Deterministic, no LLM: title and description mirror the source; body is the
    canonical text verbatim. The default synthesizer and the test baseline."""

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items = [(doc, doc.title, doc.title, doc.text) for doc in changed_docs]
        return assemble(items, changeset, existing_paths)


def synthesize(
    changed_docs: list[CanonicalDocument],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
) -> ProposedChange:
    """Backwards-compatible module entry point; delegates to StubSynthesizer."""
    return StubSynthesizer().synthesize(changed_docs, changeset, existing_paths)
```

Keep the module docstring and the existing imports; add `from typing import Protocol` near the top (after `from __future__ import annotations`).

- [ ] **Step 4: Run the full synthesize suite**

Run: `uv run pytest tests/test_synthesize.py -v`
Expected: PASS (existing tests + the new golden test).

- [ ] **Step 5: Run the whole suite (nothing else regressed)**

Run: `uv run pytest -q`
Expected: all pass (was 77).

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/synthesize.py tests/test_synthesize.py
git commit -m "refactor(synthesize): extract Synthesizer seam + StubSynthesizer

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Inject the synthesizer into `pipeline.run`

**Files:**
- Modify: `src/kbforge/pipeline.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `Synthesizer`, `StubSynthesizer` from Task 1.
- Produces: `run(..., synthesizer: Synthesizer | None = None)` — defaults to `StubSynthesizer()`.

- [ ] **Step 1: Write the injection test**

Add to `tests/test_pipeline.py`:

```python
from kbforge.models import ProposedChange
from kbforge.synthesize import concept_path


class _FixedSynth:
    """A Synthesizer that ignores the LLM and returns a fixed conformant bundle."""

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        doc = changed_docs[0]
        path = concept_path(doc.doc_id)
        fm_file = (
            "---\ntype: concept\ntitle: Injected\ndescription: Injected\n"
            "timestamp: '2026-01-01T00:00:00+00:00'\nresource:\n"
            f"- system: {doc.anchor.system}\n  native_id: {doc.anchor.native_id}\n"
            "  url: null\n---\n\n# Injected\n\nInjected body.\n"
        )
        from kbforge.models import ConceptFrontmatter

        return ProposedChange(
            branch_hint="sync/injected",
            files={path: fm_file},
            concepts={path: ConceptFrontmatter(type="concept", freshness=doc.anchor.retrieved_at, resources=[doc.anchor])},
        )


def test_run_uses_injected_synthesizer(tmp_path):
    config, mirror, state, pub = _dirs(tmp_path)
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
        synthesizer=_FixedSynth(),
    )
    assert isinstance(result, Published)
    assert "Injected body." in (Path(result.url) / "concepts/x/overview.md").read_text()
```

(Reuse the file's existing `_dirs` helper and `DOC` fixture, which already produce a single `x.md` under `concepts/x/overview.md`.)

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_pipeline.py::test_run_uses_injected_synthesizer -v`
Expected: FAIL with `TypeError: run() got an unexpected keyword argument 'synthesizer'`.

- [ ] **Step 3: Add the parameter and use it**

In `src/kbforge/pipeline.py`:

Add to imports: `from kbforge.synthesize import StubSynthesizer, Synthesizer, concept_path` (extend the existing `from kbforge.synthesize import ...` line, which already imports `concept_path`; drop the now-unused `synthesize` import).

Change the `run` signature and the synthesize call:

```python
def run(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
    synthesizer: Synthesizer | None = None,
) -> NoOp | Aborted | Published:
```

Immediately after the `info = ...` line (before any I/O), resolve the default:

```python
    synthesizer = synthesizer or StubSynthesizer()
```

Replace the existing call:

```python
    proposal = synthesize(changed_docs, changeset, existing)
```

with:

```python
    proposal = synthesizer.synthesize(changed_docs, changeset, existing)
```

- [ ] **Step 4: Run the pipeline suite**

Run: `uv run pytest tests/test_pipeline.py -v`
Expected: PASS (existing + injection test).

- [ ] **Step 5: Full suite + prek**

Run: `uv run pytest -q && uv run prek run --files src/kbforge/pipeline.py src/kbforge/synthesize.py tests/test_pipeline.py`
Expected: all pass; ruff + ty clean.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline.py
git commit -m "feat(pipeline): inject Synthesizer into run (defaults to stub)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Add the `kbforge[llm]` extra + offline test guard

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`

**Interfaces:**
- Produces: installable `kbforge[llm]` extra; suite-wide `ALLOW_MODEL_REQUESTS=False`; a `--run-live` pytest option (used in Task 6); `docs/plans` excluded from sdist.

- [ ] **Step 1: Add the optional dependency**

Run: `uv add --optional llm "pydantic-ai-slim[litellm]"`
Expected: `pyproject.toml` gains `[project.optional-dependencies].llm = ["pydantic-ai-slim[litellm]>=..."]` and `uv.lock` updates. (uv chooses the constraint and pins the resolved version in the lock.)

- [ ] **Step 2: Exclude plans from the sdist**

In `pyproject.toml`, under `[tool.hatch.build.targets.sdist]`, add `docs/plans` to `exclude` (process docs, like `.claude`):

```toml
[tool.hatch.build.targets.sdist]
# Agent/editor config and process plans are not part of the distributed library.
exclude = [
    ".claude",
    "docs/plans",
]
```

- [ ] **Step 3: Create the conftest guard**

Create `tests/conftest.py`:

```python
import pytest

# Hard guarantee: no test may reach a real model provider. TestModel/FunctionModel
# are unaffected; any accidental real request raises instead of spending tokens.
try:
    import pydantic_ai.models

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
except ImportError:  # pragma: no cover - the [llm] extra is not installed
    pass


def pytest_addoption(parser):
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run the opt-in live LLM test (needs OPENROUTER_API_KEY)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live test; pass --run-live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: test that calls a real LLM provider")
```

- [ ] **Step 4: Verify the extra installs and the guard is active**

Run: `uv sync --all-extras --dev && uv run python -c "import pydantic_ai.models as m; print('litellm import ok')"`
Expected: prints `litellm import ok`.

Run: `uv run pytest -q`
Expected: all pass (count unchanged from Task 2 — conftest adds no test functions).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock tests/conftest.py
git commit -m "build: add optional kbforge[llm] extra + offline test guard

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `LLMSynthesizer` + `SynthesizedConcept`

**Files:**
- Create: `src/kbforge/llm_synthesizer.py`
- Test: `tests/test_llm_synthesizer.py`

**Interfaces:**
- Consumes: `assemble`, `concept_path` from `synthesize`; `run_validators` from `validate`; `CanonicalDocument`, `ChangeSet` from `models`.
- Produces: `SynthesizedConcept` (Pydantic model, `min_length=1` fields), `LLMConfig` (dataclass), `LLMSynthesizer(config, *, agent=None)` implementing the `Synthesizer` protocol. `agent` is injectable so tests bypass the real provider.

- [ ] **Step 1: Write the tests (injected FunctionModel)**

Create `tests/test_llm_synthesizer.py`:

```python
from datetime import UTC, datetime

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402
from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402

from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer, SynthesizedConcept  # noqa: E402
from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402
from kbforge.synthesize import concept_path  # noqa: E402


def _doc(doc_id="local_files:apps/x.md", text="X does things.", relations=None):
    anchor = ResourceAnchor(
        system="local_files",
        native_id=doc_id.split(":", 1)[1],
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="h",
    )
    return CanonicalDocument(
        anchor=anchor, doc_id=doc_id, title="X", text=text, relations=relations or []
    )


def _agent_returning(concept: SynthesizedConcept) -> Agent:
    # For structured output (tool mode), the model must CALL the output tool with
    # the concept as args — not return free text. `info.output_tools[0].name` is the
    # output tool Pydantic AI registered for SynthesizedConcept.
    def fn(messages, info: AgentInfo):
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, concept.model_dump())]
        )

    return Agent(FunctionModel(fn), output_type=SynthesizedConcept)


def _synth(concept: SynthesizedConcept, **cfg) -> LLMSynthesizer:
    return LLMSynthesizer(LLMConfig(**cfg), agent=_agent_returning(concept))


def test_llm_output_becomes_conformant_concept():
    doc = _doc()
    concept = SynthesizedConcept(
        title="Checkout", description="What checkout does.", body="A clean summary."
    )
    proposal = _synth(concept).synthesize([doc], ChangeSet(added=[doc.doc_id]))
    path = concept_path(doc.doc_id)
    assert "A clean summary." in proposal.files[path]
    assert "# Checkout" in proposal.files[path]
    existing = frozenset({path})
    assert run_validators(proposal, existing) == []  # passes the §4.4 gate


def test_llm_cannot_emit_links_or_anchors_only_kbforge_can():
    # The model 'claims' a link in prose; no structural link may appear — the frame
    # is kbforge-owned. The doc has a real relation, which DOES resolve structurally.
    doc = _doc(relations=["local_files:apps/y.md"])
    concept = SynthesizedConcept(
        title="X", description="d", body="See [Y](concepts/apps/z/overview.md)."
    )
    other = concept_path("local_files:apps/y.md")
    proposal = _synth(concept).synthesize(
        [doc], ChangeSet(added=[doc.doc_id]), frozenset({concept_path(doc.doc_id), other})
    )
    fm = proposal.concepts[concept_path(doc.doc_id)]
    assert fm.links == [other]  # only the real relation, resolved by kbforge
    assert "concepts/apps/z/overview.md" not in fm.links  # prose claim ignored


def test_oversized_source_is_truncated_and_flagged():
    doc = _doc(text="x" * 5000)
    concept = SynthesizedConcept(title="X", description="d", body="b")
    synth = _synth(concept, max_source_chars=100)
    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))
    assert any("truncated" in n for n in proposal.summary.grounding_notes)


def test_empty_prose_is_rejected_by_schema():
    with pytest.raises(Exception):
        SynthesizedConcept(title="", description="d", body="b")


def test_validate_config_reports_missing_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    problems = LLMConfig().validate_env()
    assert problems and "OPENROUTER_API_KEY" in problems[0]
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_llm_synthesizer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.llm_synthesizer'`.

- [ ] **Step 3: Implement `llm_synthesizer.py`**

Create `src/kbforge/llm_synthesizer.py`:

```python
"""The grounded LLM synthesizer (spec §4). Optional: requires kbforge[llm]. The
model writes only prose (title/description/body); kbforge owns all structure."""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import BaseModel, Field

from kbforge.models import CanonicalDocument, ChangeSet, ProposedChange
from kbforge.synthesize import assemble

_INSTRUCTIONS = (
    "You turn one source document into a knowledge-base concept. Write ONLY from "
    "the provided text; add no outside knowledge and invent no facts. Produce a "
    "concise title, a one-paragraph description, and a clear markdown body that "
    "faithfully summarizes the source. Do not fabricate links, owners, dates, or "
    "identifiers that are not in the text."
)


class SynthesizedConcept(BaseModel):
    """The ONLY thing the model produces. Everything structural is kbforge's."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)


@dataclass
class LLMConfig:
    model: str = "deepseek/deepseek-v4-flash"
    api_base: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    max_tokens: int = 1500
    temperature: float = 0.0
    max_source_chars: int = 24000
    output_mode: str = "tool"

    def validate_env(self) -> list[str]:
        problems: list[str] = []
        if not self.model:
            problems.append("llm 'model' must be non-empty")
        if not os.environ.get(self.api_key_env):
            problems.append(f"env var {self.api_key_env} is not set")
        if self.max_tokens <= 0 or self.max_source_chars <= 0:
            problems.append("max_tokens and max_source_chars must be positive")
        if self.output_mode not in ("tool", "native", "prompted"):
            problems.append("output_mode must be tool, native, or prompted")
        return problems


def _wrap_output(mode: str):
    from pydantic_ai import NativeOutput, PromptedOutput

    if mode == "native":
        return NativeOutput(SynthesizedConcept)
    if mode == "prompted":
        return PromptedOutput(SynthesizedConcept)
    return SynthesizedConcept  # tool mode (default)


class LLMSynthesizer:
    def __init__(self, config: LLMConfig, *, agent: object | None = None) -> None:
        self.config = config
        self.agent = agent if agent is not None else self._build_agent(config)

    @staticmethod
    def _build_agent(config: LLMConfig) -> object:
        try:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.litellm import LiteLLMProvider
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - guarded by the extra
            raise ImportError(
                "LLMSynthesizer requires the LLM extra: pip install 'kbforge[llm]'"
            ) from exc
        model = OpenAIChatModel(
            config.model,
            provider=LiteLLMProvider(
                api_base=config.api_base,
                api_key=os.environ.get(config.api_key_env),
            ),
        )
        return Agent(
            model,
            output_type=_wrap_output(config.output_mode),
            instructions=_INSTRUCTIONS,
            model_settings=ModelSettings(
                temperature=config.temperature, max_tokens=config.max_tokens
            ),
        )

    def _prompt(self, doc: CanonicalDocument, text: str) -> str:
        facets = "\n".join(f"{k}: {v}" for k, v in doc.structured.items())
        return (
            f"Source id: {doc.anchor.native_id}\n"
            f"Source title: {doc.title}\n"
            f"Source facets:\n{facets or '(none)'}\n\n"
            f"Source text:\n{text}"
        )

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        notes: list[str] = []
        for doc in changed_docs:
            text = doc.text
            if len(text) > self.config.max_source_chars:
                text = text[: self.config.max_source_chars]
                notes.append(
                    f"{doc.doc_id}: source truncated to "
                    f"{self.config.max_source_chars} chars before synthesis"
                )
            result = self.agent.run_sync(self._prompt(doc, text))
            c = result.output
            items.append((doc, c.title, c.description, c.body))
        proposal = assemble(items, changeset, existing_paths)
        proposal.summary.grounding_notes.extend(notes)
        return proposal
```

- [ ] **Step 4: Run the LLM synthesizer tests**

Run: `uv run pytest tests/test_llm_synthesizer.py -v`
Expected: PASS. Fallback if the output-tool lookup differs by Pydantic AI version: replace the `FunctionModel` helper with `TestModel(custom_output_args=concept.model_dump())` from `pydantic_ai.models.test`, keeping every assertion unchanged.

- [ ] **Step 5: Full suite + prek**

Run: `uv run pytest -q && uv run prek run --files src/kbforge/llm_synthesizer.py tests/test_llm_synthesizer.py`
Expected: all pass; ruff + ty clean.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/llm_synthesizer.py tests/test_llm_synthesizer.py
git commit -m "feat(synthesize): grounded LLMSynthesizer on Pydantic AI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: CLI integration

**Files:**
- Modify: `src/kbforge/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `LLMConfig`, `LLMSynthesizer`; `_parse_settings` (existing).
- Produces: `run` subcommand gains `--synthesizer {stub,llm}` (default `stub`) and repeatable `--llm-set KEY=VALUE`; `list` prints a synthesizers section.

- [ ] **Step 1: Write the CLI tests**

Add to `tests/test_cli.py`:

```python
def test_list_shows_synthesizers(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "stub" in out and "llm" in out


def test_run_stub_synthesizer_default(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        ["run", "--connector", "local_files", "--set", f"path={src}", *_plumbing(tmp_path)]
    )
    assert code == 0 and "Published" in capsys.readouterr().out


def test_run_llm_synthesizer_offline(tmp_path: Path, capsys, monkeypatch):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent
    from pydantic_ai.models.function import AgentInfo, FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from kbforge import llm_synthesizer
    from kbforge.llm_synthesizer import SynthesizedConcept

    def fake_agent(config):
        def fn(messages, info: AgentInfo):
            c = SynthesizedConcept(title="T", description="D", body="Body from LLM.")
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, c.model_dump())]
            )

        return Agent(FunctionModel(fn), output_type=SynthesizedConcept)

    monkeypatch.setattr(llm_synthesizer.LLMSynthesizer, "_build_agent", staticmethod(fake_agent))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--synthesizer",
            "llm",
            "--llm-set",
            "model=deepseek/deepseek-v4-flash",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0 and "Published" in capsys.readouterr().out
    assert "Body from LLM." in (
        tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md"
    ).read_text()
```

- [ ] **Step 2: Run to confirm failure**

Run: `uv run pytest tests/test_cli.py -k "synthesizer" -v`
Expected: FAIL (`--synthesizer`/`--llm-set` unknown; `list` lacks synthesizers).

- [ ] **Step 3: Wire the CLI**

In `src/kbforge/__main__.py`:

Add args to the `run` subparser (after `--set`):

```python
    r.add_argument("--synthesizer", choices=["stub", "llm"], default="stub")
    r.add_argument(
        "--llm-set",
        action="append",
        default=[],
        dest="llm_settings",
        metavar="KEY=VALUE",
        help="LLM synthesizer config (repeatable); YAML-typed values",
    )
```

In the `list` branch, after printing connectors, add:

```python
        print("synthesizers:")
        print("  stub\tdeterministic, no LLM")
        print("  llm\tPydantic AI (needs kbforge[llm])")
```

Build the synthesizer before the `run(...)` call:

```python
    if args.synthesizer == "llm":
        from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer

        try:
            llm_cfg = LLMConfig(**_parse_settings(args.llm_settings))
        except (ValueError, TypeError) as exc:
            print(str(exc))
            return 2
        problems = llm_cfg.validate_env()
        if problems:
            print("; ".join(problems))
            return 2
        synthesizer = LLMSynthesizer(llm_cfg)
    else:
        synthesizer = None  # run() defaults to StubSynthesizer
```

Pass it into `run(...)`:

```python
        result = run(
            connectors[args.connector],
            _publisher(pm),
            config=config,
            mirror=args.mirror,
            state_dir=args.state,
            publish_config={"out_dir": args.out},
            synthesizer=synthesizer,
        )
```

- [ ] **Step 4: Run the CLI suite**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite + prek**

Run: `uv run pytest -q && uv run prek run --files src/kbforge/__main__.py tests/test_cli.py`
Expected: all pass; ruff + ty clean.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/__main__.py tests/test_cli.py
git commit -m "feat(cli): --synthesizer selector and --llm-set config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Opt-in live test + docs

**Files:**
- Create: `tests/test_llm_live.py`
- Modify: `README.md`, `docs/architecture.md`, `CHANGELOG.md`

**Interfaces:**
- Consumes: the `live` marker + `--run-live` option from Task 3's conftest.

- [ ] **Step 1: Write the opt-in live test**

Create `tests/test_llm_live.py`:

```python
import os

import pytest

pytest.importorskip("pydantic_ai")

from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer  # noqa: E402
from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor  # noqa: E402
from kbforge.synthesize import concept_path  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402
from datetime import UTC, datetime  # noqa: E402


@pytest.mark.live
def test_live_deepseek_produces_conformant_concept():
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    anchor = ResourceAnchor(
        system="local_files",
        native_id="apps/checkout.md",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="h",
    )
    doc = CanonicalDocument(
        anchor=anchor,
        doc_id="local_files:apps/checkout.md",
        title="Checkout",
        text="The checkout service accepts a cart and returns an order.",
    )
    synth = LLMSynthesizer(LLMConfig())  # default deepseek/deepseek-v4-flash
    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))
    path = concept_path(doc.doc_id)
    assert proposal.files[path].strip()
    assert run_validators(proposal, frozenset({path})) == []
```

- [ ] **Step 2: Confirm it is skipped by default**

Run: `uv run pytest tests/test_llm_live.py -v`
Expected: SKIPPED ("live test; pass --run-live to enable").

- [ ] **Step 3: Update the README Quickstart**

In `README.md`, after the `local_files` Quickstart block, add an LLM note:

```markdown
To synthesize real prose instead of the deterministic stub, install the LLM extra
and select the synthesizer (config values are YAML-typed; the API key comes from an
env var, never the CLI):

```bash
pip install "kbforge[llm]"
export OPENROUTER_API_KEY=...        # or point --llm-set api_base=... at a gateway
kbforge run --connector local_files --set path=./docs \
  --synthesizer llm --llm-set model=deepseek/deepseek-v4-flash \
  --mirror .kbforge/mirror --out .kbforge/out --state .kbforge/state
```

The synthesizer reaches models through a LiteLLM provider, so OpenRouter and a
self-hosted LiteLLM gateway share one config path.
```

- [ ] **Step 4: Amend architecture.md (spec §12)**

In `docs/architecture.md` §7, update the `synthesize` description to note it is a stage backed by a `Synthesizer` object injected into `run` (`StubSynthesizer` default, `LLMSynthesizer` optional), and that the LLM writes prose inside a kbforge-owned frame. Update the `run(...)` signature note to include the `synthesizer` parameter. (Keep edits to the two relevant sentences; do not restructure the doc.)

- [ ] **Step 5: Add a CHANGELOG entry**

In `CHANGELOG.md`, under `## [Unreleased]`, add:

```markdown
### Added

- Grounded LLM synthesizer (`--synthesizer llm`, optional `kbforge[llm]` extra):
  the model writes only concept prose inside a kbforge-owned structural frame,
  reached through a LiteLLM provider (OpenRouter or a self-hosted gateway). The
  deterministic stub remains the default.
```

- [ ] **Step 6: Full suite + prek + build check**

Run: `uv run pytest -q && uv run prek run --all-files`
Expected: all pass; ruff + ty clean. Live test skipped.

Run: `uv build && uvx twine check dist/*`
Expected: both artifacts PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_llm_live.py README.md docs/architecture.md CHANGELOG.md
git commit -m "test+docs: opt-in live LLM test and synthesizer docs

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** seam (§3)→T1/T2; provider+config (§4,§5)→T4; dependency (§6)→T3; budget/scope (§7)→T4; failure (§8)→T4 (schema retry) + existing gate (T2); testing (§9)→T3 conftest + T4 fakes + T6 live; CLI (§10)→T5; amendments (§12)→T6. All covered.
- **Type consistency:** `assemble(items, changeset, existing_paths)` with `items: list[tuple[CanonicalDocument, str, str, str]]` is produced in T1 and consumed by `LLMSynthesizer` in T4; `Synthesizer` protocol signature matches `StubSynthesizer`, `LLMSynthesizer`, and the pipeline call.
- **Known risk (flagged, not a blocker):** Pydantic AI structured output via `FunctionModel` may require returning a tool-call part rather than text (T4 Step 4 notes the adjustment). The `output_mode` config exists precisely so a weak-tool-calling model can fall back to `native`/`prompted` — to be pinned from the T6 live run.
