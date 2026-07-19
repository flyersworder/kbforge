# Agent-Facing Artifact Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the emit-side data model and the four §4.4 agent-facing artifact validators, so kbforge's "agent-first" claim is mechanically enforced against a `ProposedChange` before any MR opens.

**Architecture:** Two new modules under `src/kbforge/`. `models.py` holds the emit-side Pydantic classes (`ResourceAnchor`, `ConceptFrontmatter`, `ChangeSummary`, `ProposedChange`). `validate.py` holds `run_artifact_validators(proposal, existing_paths)`, which runs the four §4.4 laws over `proposal.concepts` and returns a list of `Failure` records (empty == conformant). The validators are pure functions of their input — no network, no clock, no running MCP server, no LLM.

**Tech Stack:** Python 3.12+, Pydantic v2, pytest. (pluggy is a project dependency but is not used in this plan.)

## Global Constraints

- Python `>=3.12`; code must type-check clean under `ty` and lint clean under `ruff` (rules `E`, `F`, `I`, `UP`).
- Pydantic v2 (`pydantic>=2.0`) for all data models.
- Source lives under `src/kbforge/`; tests under `tests/`.
- `prek` runs `ruff-check --fix`, `ruff-format`, and `ty` on every commit; a commit fails if any hook fails.
- Conventional-commit messages; end every commit message with the trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **Accountability principle (load-bearing):** the validate stage is the *single* accountable gate for the §4.4 laws (architecture §7; spec §5: "checked inside `run_validators` as core validators"). No law may be silently enforced elsewhere — in particular, model fields are **permissive** so a law-violating concept can be *constructed and then reported*, never crash at construction. kbforge checks synthesis output; it does not trust it.

---

## Scope: this is Plan 1 of a series

The full architecture (docs/architecture.md) is far larger than one plan. This plan builds only the **agent-facing artifact contract** — the brainstormed spec `docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md`. It is the foundation and is independently shippable: given a `ProposedChange`, it returns the list of §4.4 law violations.

Deliberately **out of scope** here (each is its own later plan, each extends `models.py` with the classes it tests):

- **Canonicalization (§4.3):** `canonical.py`, hashing/stability, and the ingest models (`Cursor`, `RawRecord`, `FetchResult`, `CanonicalDocument`, `ConnectorInfo`).
- **Diff stage:** `ChangeSet` + `is_noop`.
- **Pluggy machinery:** `hookspecs.py`, `registry.py`.
- **Pipeline run loop:** `pipeline.py`, and the umbrella `run_validators` that calls this plan's `run_artifact_validators` alongside the strict-OKF-on-rendered-files check and the `kbforge_extra_validators` hook.
- **Strict 4-field OKF check on rendered markdown** (`title`, `description`, `timestamp`): needs a frontmatter parser + the synthesis renderer. This plan covers the two OKF-required properties that live in the projection — non-empty `type` (via the `okf-type` check) and `timestamp` presence (via law 4 on `freshness`) — and defers `title`/`description` to the synthesis plan.

`models.py` starts here with only the emit-side classes; later plans append the ingest/diff classes when they have tests that exercise them (no untested model code).

## File Structure

- Create `src/kbforge/models.py` — emit-side Pydantic data model. Responsibility: the shapes synthesis produces and validation consumes.
- Create `src/kbforge/validate.py` — the four §4.4 law validators + `Failure` + `run_artifact_validators`. Responsibility: mechanically decide whether an emitted artifact is agent-usable.
- Create `tests/test_models.py` — construction and default behavior of the models.
- Create `tests/test_validate.py` — one focused test per law (pass + fail) plus the §9 conformance capstone.
- Modify `docs/architecture.md` §3 and the spec §4 model block — sync the shown `ConceptFrontmatter` to the permissive fields this plan builds (Task 6).

---

### Task 1: Emit-side data model (`models.py`)

**Files:**
- Create: `src/kbforge/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing (foundation).
- Produces:
  - `ResourceAnchor(system: str, native_id: str, url: str | None = None, retrieved_at: datetime, content_hash: str)`
  - `ConceptFrontmatter(type: str = "", facets: dict = {}, resources: list[ResourceAnchor] = [], links: list[str] = [], freshness: datetime | None = None)`
  - `ChangeSummary(...)` — seven `list[...]` fields, all defaulting empty.
  - `ProposedChange(branch_hint: str, files: dict[str, str] = {}, concepts: dict[str, ConceptFrontmatter] = {}, summary: ChangeSummary = ChangeSummary())`

- [ ] **Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
from datetime import datetime, timezone

from kbforge.models import (
    ChangeSummary,
    ConceptFrontmatter,
    ProposedChange,
    ResourceAnchor,
)

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def test_concept_frontmatter_defaults_are_permissive():
    c = ConceptFrontmatter()
    assert c.type == ""
    assert c.facets == {}
    assert c.resources == []
    assert c.links == []
    assert c.freshness is None


def test_proposed_change_holds_files_and_concepts():
    anchor = ResourceAnchor(
        system="confluence",
        native_id="123",
        retrieved_at=NOW,
        content_hash="abc",
    )
    concept = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a"},
        resources=[anchor],
        freshness=NOW,
    )
    change = ProposedChange(
        branch_hint="sync/app-x",
        files={"apps/x/overview.md": "# X"},
        concepts={"apps/x/overview.md": concept},
    )
    assert change.concepts["apps/x/overview.md"].facets["owner"] == "team-a"
    assert change.concepts["apps/x/overview.md"].resources[0].native_id == "123"
    assert isinstance(change.summary, ChangeSummary)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.models'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/kbforge/models.py`:

```python
"""Pydantic data model for kbforge. See docs/architecture.md §3.

This module starts with the emit-side classes the agent-facing artifact
contract (§4.4) validates. Ingest-side classes (Cursor, ConnectorInfo,
RawRecord, FetchResult, CanonicalDocument, ChangeSet) arrive with the plans
that build and test them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Each anchor becomes one OKF `resource` frontmatter entry at emit time."""

    system: str
    native_id: str
    url: str | None = None
    retrieved_at: datetime
    content_hash: str


class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF concept (§4.4).

    Fields are permissive so a law-violating concept can be represented and then
    reported by the validators — kbforge checks synthesis output, it does not
    trust it (spec §5). `type` and `freshness` serialize onto the OKF `type` and
    `timestamp` keys at write time; each `resources` entry becomes a `resource`
    entry. This is the §4.4 projection, not the whole frontmatter: title,
    description, and the rendered body live in the file the publisher writes."""

    type: str = ""  # OKF's one required field (checked non-empty by validate)
    facets: dict = Field(default_factory=dict)  # law 1
    resources: list[ResourceAnchor] = Field(default_factory=list)  # law 3
    links: list[str] = Field(default_factory=list)  # law 2
    freshness: datetime | None = None  # law 4


class ChangeSummary(BaseModel):
    """Producer-generated MR description, structured."""

    sources_changed: list[ResourceAnchor] = Field(default_factory=list)
    claims_added: list[str] = Field(default_factory=list)
    claims_modified: list[str] = Field(default_factory=list)
    claims_removed: list[str] = Field(default_factory=list)
    conflicts_flagged: list[str] = Field(default_factory=list)
    gaps_flagged: list[str] = Field(default_factory=list)
    grounding_notes: list[str] = Field(default_factory=list)


class ProposedChange(BaseModel):
    """What synthesis hands to a publisher: rendered files, the validated
    frontmatter projection, and a reviewable summary (§3, §4.4)."""

    branch_hint: str
    files: dict[str, str] = Field(default_factory=dict)
    concepts: dict[str, ConceptFrontmatter] = Field(default_factory=dict)
    summary: ChangeSummary = Field(default_factory=ChangeSummary)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/models.py tests/test_models.py
git commit -m "feat(models): add emit-side data model for the artifact contract

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Validators — type, anchor presence (law 3), freshness (law 4)

**Files:**
- Create: `src/kbforge/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `ConceptFrontmatter`, `ProposedChange` from `kbforge.models`.
- Produces:
  - `Failure(concept_path: str, law: str, message: str)` — frozen dataclass.
  - `run_artifact_validators(proposal: ProposedChange) -> list[Failure]` — runs the per-concept checks (`okf-type`, `anchor-presence`, `freshness-legibility`). Extended in Task 4 to take `existing_paths` and run law 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_validate.py`:

```python
from datetime import datetime, timezone

from kbforge.models import ConceptFrontmatter, ProposedChange, ResourceAnchor
from kbforge.validate import run_artifact_validators

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)
ANCHOR = ResourceAnchor(
    system="confluence", native_id="123", retrieved_at=NOW, content_hash="abc"
)


def _proposal(concept, path="apps/x/overview.md"):
    return ProposedChange(
        branch_hint="b", files={path: "..."}, concepts={path: concept}
    )


def test_missing_anchor_is_reported():
    c = ConceptFrontmatter(type="application", freshness=NOW)  # no resources
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "anchor-presence" for f in failures)


def test_missing_freshness_is_reported():
    c = ConceptFrontmatter(type="application", resources=[ANCHOR])  # freshness None
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "freshness-legibility" for f in failures)


def test_empty_type_is_reported():
    c = ConceptFrontmatter(type="", resources=[ANCHOR], freshness=NOW)
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "okf-type" for f in failures)


def test_conformant_concept_passes_per_concept_checks():
    c = ConceptFrontmatter(type="application", resources=[ANCHOR], freshness=NOW)
    assert run_artifact_validators(_proposal(c)) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kbforge.validate'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/kbforge/validate.py`:

```python
"""Agent-facing artifact validators — the §4.4 laws, enforced core.

These run in the pipeline's validate stage (architecture §7) over a
ProposedChange's `concepts` projection. A non-empty result aborts the run; no
MR opens for a non-conformant artifact. kbforge checks synthesis output rather
than trusting it (spec §5), so every law is a runtime check that returns a
report — never a construction-time crash.
"""

from __future__ import annotations

from dataclasses import dataclass

from kbforge.models import ConceptFrontmatter, ProposedChange


@dataclass(frozen=True)
class Failure:
    """One law violation, collected into a report rather than raised."""

    concept_path: str
    law: str
    message: str


def _check_type(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if not concept.type or not concept.type.strip():
        return [
            Failure(
                path,
                "okf-type",
                "concept type is empty; OKF requires a non-empty type",
            )
        ]
    return []


def _check_anchor_presence(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if len(concept.resources) < 1:
        return [
            Failure(
                path,
                "anchor-presence",
                "concept carries no resource anchor (§4.4 law 3)",
            )
        ]
    return []


def _check_freshness_legible(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if concept.freshness is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept carries no freshness stamp (§4.4 law 4)",
            )
        ]
    return []


def run_artifact_validators(proposal: ProposedChange) -> list[Failure]:
    """Run the §4.4 laws over the proposal's concept projection.

    Empty result == conformant artifact."""
    failures: list[Failure] = []
    for path, concept in proposal.concepts.items():
        failures += _check_type(path, concept)
        failures += _check_anchor_presence(path, concept)
        failures += _check_freshness_legible(path, concept)
    return failures
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/validate.py tests/test_validate.py
git commit -m "feat(validate): add type, anchor (law 3), freshness (law 4) checks

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Validator — facet survival (law 1)

**Files:**
- Modify: `src/kbforge/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `ConceptFrontmatter`.
- Produces: `_check_facets_wellformed(path, concept) -> list[Failure]`, wired into `run_artifact_validators`. A facet value is valid iff it is a non-empty scalar (`str`/`int`/`float`/`bool`) or a flat list of scalars. Nested/empty values are reported as `facet-survival`. **Scope note:** the core check is *well-formedness of declared facets* (what keeps `list_concepts`/`search_knowledge` filters alive). The completeness direction — "synthesis didn't leave a used field only in prose" — is not decidable from the artifact alone and is verified on fixtures by the §9 conformance test (Task 5), not per-run.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_validate.py`:

```python
def test_empty_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application", facets={"owner": ""}, resources=[ANCHOR], freshness=NOW
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-survival" for f in failures)


def test_nested_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": {"team": "a"}},
        resources=[ANCHOR],
        freshness=NOW,
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-survival" for f in failures)


def test_scalar_and_flat_list_facets_pass():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a", "tags": ["prod", "db"], "replicas": 3},
        resources=[ANCHOR],
        freshness=NOW,
    )
    facet_failures = [
        f for f in run_artifact_validators(_proposal(c)) if f.law == "facet-survival"
    ]
    assert facet_failures == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate.py -k facet -v`
Expected: FAIL — `test_empty_facet_value_is_reported` and `test_nested_facet_value_is_reported` fail (no `facet-survival` failures produced yet).

- [ ] **Step 3: Write minimal implementation**

In `src/kbforge/validate.py`, add the scalar tuple constant just below the imports:

```python
_SCALAR = (str, int, float, bool)
```

Add these two functions above `run_artifact_validators`:

```python
def _is_filterable(value: object) -> bool:
    if isinstance(value, _SCALAR):
        return True
    if isinstance(value, list):
        return all(isinstance(v, _SCALAR) for v in value)
    return False


def _check_facets_wellformed(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    failures: list[Failure] = []
    for key, value in concept.facets.items():
        if value in (None, "", [], {}):
            failures.append(
                Failure(
                    path,
                    "facet-survival",
                    f"facet {key!r} is empty; a filterable facet must carry a "
                    "value (§4.4 law 1)",
                )
            )
        elif not _is_filterable(value):
            failures.append(
                Failure(
                    path,
                    "facet-survival",
                    f"facet {key!r} must be a scalar or flat list to be "
                    "filterable (§4.4 law 1)",
                )
            )
    return failures
```

Wire it into the per-concept loop in `run_artifact_validators`, after `_check_type`:

```python
    for path, concept in proposal.concepts.items():
        failures += _check_type(path, concept)
        failures += _check_facets_wellformed(path, concept)
        failures += _check_anchor_presence(path, concept)
        failures += _check_freshness_legible(path, concept)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/validate.py tests/test_validate.py
git commit -m "feat(validate): add facet survival (law 1) well-formedness check

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Validator — link resolvability (law 2)

**Files:**
- Modify: `src/kbforge/validate.py`
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: `ProposedChange`.
- Produces: `_check_links_resolve(proposal, existing_paths) -> list[Failure]`; `run_artifact_validators` gains a second parameter `existing_paths: frozenset[str] = frozenset()`. Links are stored **bundle-root-relative and already normalized** (synthesis normalizes relative markdown links before emit); the validator does set membership against `proposal.files` ∪ `proposal.concepts` ∪ `existing_paths`. A link resolving to none of those is reported as `link-resolvability`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_validate.py`:

```python
def test_dangling_link_is_reported():
    c = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/y/overview.md"],  # y not in the bundle
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "link-resolvability" for f in failures)


def test_link_to_sibling_in_same_change_resolves():
    x = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/y/overview.md"],
    )
    y = ConceptFrontmatter(type="application", resources=[ANCHOR], freshness=NOW)
    change = ProposedChange(
        branch_hint="b",
        files={"apps/x/overview.md": "...", "apps/y/overview.md": "..."},
        concepts={"apps/x/overview.md": x, "apps/y/overview.md": y},
    )
    link_failures = [
        f for f in run_artifact_validators(change) if f.law == "link-resolvability"
    ]
    assert link_failures == []


def test_link_to_existing_bundle_path_resolves():
    c = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/z/overview.md"],
    )
    link_failures = [
        f
        for f in run_artifact_validators(
            _proposal(c), existing_paths=frozenset({"apps/z/overview.md"})
        )
        if f.law == "link-resolvability"
    ]
    assert link_failures == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validate.py -k link -v`
Expected: FAIL — `test_link_to_existing_bundle_path_resolves` raises `TypeError: run_artifact_validators() got an unexpected keyword argument 'existing_paths'` (and `test_dangling_link_is_reported` fails: no link check yet).

- [ ] **Step 3: Write minimal implementation**

In `src/kbforge/validate.py`, add this function above `run_artifact_validators`:

```python
def _check_links_resolve(
    proposal: ProposedChange, existing_paths: frozenset[str]
) -> list[Failure]:
    known = set(proposal.files) | set(proposal.concepts) | set(existing_paths)
    failures: list[Failure] = []
    for path, concept in proposal.concepts.items():
        for link in concept.links:
            if link not in known:
                failures.append(
                    Failure(
                        path,
                        "link-resolvability",
                        f"link {link!r} resolves to no concept in the bundle "
                        "(§4.4 law 2)",
                    )
                )
    return failures
```

Change the `run_artifact_validators` signature and add the link check after the per-concept loop:

```python
def run_artifact_validators(
    proposal: ProposedChange,
    existing_paths: frozenset[str] = frozenset(),
) -> list[Failure]:
    """Run all four §4.4 laws over the proposal's concept projection.

    Empty result == conformant artifact. `existing_paths` are bundle-root-relative
    paths already on `main`, so law 2 resolves links to concepts this change does
    not itself carry."""
    failures: list[Failure] = []
    for path, concept in proposal.concepts.items():
        failures += _check_type(path, concept)
        failures += _check_facets_wellformed(path, concept)
        failures += _check_anchor_presence(path, concept)
        failures += _check_freshness_legible(path, concept)
    failures += _check_links_resolve(proposal, existing_paths)
    return failures
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validate.py -v`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/validate.py tests/test_validate.py
git commit -m "feat(validate): add link resolvability (law 2) check

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: The §9 conformance capstone — agent-facing artifact test

**Files:**
- Test: `tests/test_validate.py`

**Interfaces:**
- Consumes: everything above. Produces no new code — this is the acceptance test named in architecture §9 and spec §6: a fully-conformant multi-concept `ProposedChange` yields zero failures, and a single deliberate violation of each law is caught. It is the concrete meaning of "kbforge conformant" for the emit side.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_validate.py`:

```python
def _conformant_change():
    concept = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a", "criticality": "high"},
        resources=[ANCHOR],
        links=["apps/y/overview.md"],
        freshness=NOW,
    )
    sibling = ConceptFrontmatter(
        type="application", resources=[ANCHOR], freshness=NOW
    )
    return ProposedChange(
        branch_hint="sync/app-x",
        files={"apps/x/overview.md": "# X", "apps/y/overview.md": "# Y"},
        concepts={"apps/x/overview.md": concept, "apps/y/overview.md": sibling},
    )


def test_agent_facing_artifact_conformance():
    # §9 conformance capstone: a conformant bundle passes all four laws.
    assert run_artifact_validators(_conformant_change()) == []


def test_each_law_catches_its_own_violation():
    # One targeted break per law, asserting the specific law fires.
    base = _conformant_change()

    no_anchor = base.model_copy(deep=True)
    no_anchor.concepts["apps/x/overview.md"].resources = []
    assert any(
        f.law == "anchor-presence"
        for f in run_artifact_validators(no_anchor)
    )

    no_freshness = base.model_copy(deep=True)
    no_freshness.concepts["apps/x/overview.md"].freshness = None
    assert any(
        f.law == "freshness-legibility"
        for f in run_artifact_validators(no_freshness)
    )

    bad_facet = base.model_copy(deep=True)
    bad_facet.concepts["apps/x/overview.md"].facets = {"owner": ""}
    assert any(
        f.law == "facet-survival" for f in run_artifact_validators(bad_facet)
    )

    dangling = base.model_copy(deep=True)
    dangling.concepts["apps/x/overview.md"].links = ["apps/ghost/overview.md"]
    assert any(
        f.law == "link-resolvability"
        for f in run_artifact_validators(dangling)
    )
```

- [ ] **Step 2: Run the capstone tests to verify they pass**

Run: `uv run pytest tests/test_validate.py -k "conformance or each_law" -v`
Expected: PASS (the validators from Tasks 2–4 already satisfy these). This task adds tests over existing behavior — there is no red→green step; the capstone is an acceptance test that locks the integrated contract. If either test fails, the failure names the law whose validator regressed — fix `validate.py`, do not weaken the test.

- [ ] **Step 3: Run the full suite with coverage**

Run: `uv run pytest --cov=kbforge --cov-report=term-missing`
Expected: PASS (all tests); `models.py` and `validate.py` at or near 100% — investigate any uncovered line before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/test_validate.py
git commit -m "test(validate): add §9 agent-facing artifact conformance capstone

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Sync the docs to the built model

**Files:**
- Modify: `docs/architecture.md` (§3 `ConceptFrontmatter`)
- Modify: `docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md` (§4 model block)

**Interfaces:** none (documentation). This closes the one gap the plan opened: the sketches show `resources`/`freshness` as required, but the accountability principle made them permissive (validator-reported, not construction-crashed). The docs must match the code.

- [ ] **Step 1: Update `architecture.md` §3**

In the `ConceptFrontmatter` block, replace the field lines so they read:

```python
    type: str = ""                 # OKF's required field; validate checks non-empty
    facets: dict = Field(default_factory=dict)   # law 1: filterable keys
    resources: list[ResourceAnchor] = Field(default_factory=list)  # law 3: >=1,
                                   # enforced by validate (permissive so a
                                   # violation is reported, not a construct error)
    links: list[str] = Field(default_factory=list)   # law 2: must resolve
    freshness: datetime | None = None            # law 4: retrieved_at; validate
                                   # requires presence
```

Then, in the paragraph after the model that begins "The §4.4 artifact laws are checked inside `run_validators`", add one sentence: *"Fields are permissive by design — the validate stage is the single accountable gate for the laws, so a violating concept is constructed and reported, never rejected at construction."*

- [ ] **Step 2: Update the spec §4 model block**

In `2026-07-18-agent-facing-artifact-contract-design.md` §4, apply the same field changes to the shown `ConceptFrontmatter` (`type: str = ""`, `resources` defaulted, `freshness: datetime | None = None`) and, in the "Notes" list, add a bullet: *"Fields are permissive so every law is a runtime validator that reports (spec §5), not a construction constraint — the validate stage is the single accountable gate."*

- [ ] **Step 3: Verify cross-references still resolve**

Run: `grep -n "datetime" docs/architecture.md docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md`
Expected: the `freshness` lines now show `datetime | None`; no stray `freshness: datetime` (required) remains in either file.

- [ ] **Step 4: Commit**

```bash
git add docs/architecture.md docs/superpowers/specs/2026-07-18-agent-facing-artifact-contract-design.md
git commit -m "docs: sync ConceptFrontmatter to the permissive, validator-gated model

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage** (against `2026-07-18-agent-facing-artifact-contract-design.md`):
- §2 serving contract (frontmatter / links / anchors) → the four validators map 1:1 to these affordances (Tasks 2–4). ✓
- §3 the four laws → law 1 Task 3, law 2 Task 4, law 3 Task 2, law 4 Task 2. ✓
- §3.1 freshness-vs-gate → law 4's freshness stamp is emitted and checked (Task 2); the caveat behavior is a serving-layer concern, correctly out of scope. ✓
- §4 `ConceptFrontmatter` → Task 1, with the permissive refinement documented in Task 6. ✓
- §5 enforcement as core validators returning a report → `run_artifact_validators` returns `list[Failure]` (Tasks 2–4); pipeline wiring into `run_validators` is explicitly a later plan. ✓
- §6 §9 conformance test → Task 5. ✓
- §7 positioning → no code; the enforced laws are what back the claim. ✓
- §10 open items: facet completeness is explicitly a fixture-level check, not per-run (Task 3 scope note); carrier is `ProposedChange.concepts` (Task 1). ✓

**Placeholder scan:** no TBD/TODO; every code step shows complete code; every command shows expected output. ✓

**Type consistency:** `run_artifact_validators` is `(proposal)` in Task 2, extended to `(proposal, existing_paths=frozenset())` in Task 4 (additive default, Task 2 callers unaffected). `Failure` fields (`concept_path`, `law`, `message`) and the `law` slugs (`okf-type`, `facet-survival`, `anchor-presence`, `freshness-legibility`, `link-resolvability`) are identical across all tasks and tests. `ConceptFrontmatter` field names match between `models.py`, every test, and Task 6's doc edits. ✓
