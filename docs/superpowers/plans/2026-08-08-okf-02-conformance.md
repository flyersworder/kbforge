# OKF v0.2 Conformance Pass (kbforge 0.5.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move kbforge's emitted OKF frontmatter from v0.1 vocabulary (`resource`, `timestamp`) to v0.2 (`sources`, `generated`), so the §4.4 artifact laws are expressed in the standard's own field names rather than beside them.

**Architecture:** Three code changes, each isolated to one emit-side concern. `ConceptFrontmatter` (the §4.4 projection) renames `resources` → `sources` and `freshness` → `generated_at`, and gains `generated_by`. `synthesize._render` writes the two new frontmatter families. The strict-OKF validator's required-key tuple follows. The four laws keep their names, slugs, and meanings — only the fields they read are renamed. No pipeline stage, hook signature, or publisher changes.

**Tech Stack:** Python 3.12+, Pydantic v2, PyYAML, pytest, uv, ruff + ty via prek.

## Global Constraints

- **Spec authority:** OKF v0.2, `okf/SPEC.md` in `GoogleCloudPlatform/knowledge-catalog`. Section numbers below (§5.1, §5.2, §11) refer to it. kbforge's own section numbers are written as "kbforge §4.4".
- **Python floor:** 3.12+. Use `from __future__ import annotations` in every modified module (all already have it).
- **Formatting/typing:** `prek` runs `ruff check`, `ruff format`, and `ty` on every commit. Never bypass with `--no-verify`.
- **Network:** the default test suite must never touch the network. Do not add `live`-marked tests in this plan.
- **Producer strictness is intentional:** OKF §11 requires only a non-empty `type`. kbforge additionally requires `title`, `description`, and `generated`. This is deliberate producer-side strictness, not a misreading of conformance — keep it, and say so in the docstring.
- **Commit style:** Conventional Commits, and every commit message ends with the trailer `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- **Actor convention (§7):** `<producer>/<version>`. kbforge emits `kbforge/<pkg-version>` for the stub synthesizer and `kbforge/<model-id>` for the LLM synthesizer, following the spec's own `reference_agent/gemini-2.5-pro` example where the version slot carries the model.
- **Out of scope, by decision:** `verified`, `status`/`deprecated`, `stale_after`, and footnote attribution. Task 4 records why in a design note. Do not implement them.

---

## File Structure

| File | Change | Responsibility after this plan |
|---|---|---|
| `src/kbforge/models.py` | Modify (`ResourceAnchor` docstring, `ConceptFrontmatter` fields) | The §4.4 projection, in v0.2 vocabulary |
| `src/kbforge/synthesize.py` | Modify (`_render`, `assemble`, new `_generated`/`_source_entry`) | Renders v0.2 frontmatter; owns the actor string |
| `src/kbforge/llm_synthesizer.py` | Modify (one `assemble` call) | Passes the model-derived actor |
| `src/kbforge/validate.py` | Modify (`_check_anchor_presence`, `_check_freshness_legible`, `_STRICT_REQUIRED`) | Laws read the renamed fields |
| `src/kbforge/connectors/local_files.py` | Modify (`_RESERVED_KEYS`) | Source keys never collide with emit-side keys |
| `tests/test_synthesize.py` | Modify | Pins projection field names and rendered output |
| `tests/test_validate.py` | Modify | Pins law behaviour on renamed fields |
| `tests/test_strict_okf.py` | Modify | Pins `generated` as a required rendered key |
| `tests/test_local_files_connector.py` | Modify | Pins reserved-key round-tripping |
| `docs/design/2026-08-08-okf-02-deferred-decisions.md` | Create | Records the four deferred families and why |
| `README.md`, `docs/architecture.md`, `docs/design/2026-07-18-agent-facing-artifact-contract-design.md`, `CHANGELOG.md`, `pyproject.toml` | Modify | Docs and release metadata |

---

## Task 1: `resources` → `sources` (OKF §5.1)

Provenance moves off the `resource` key — which v0.1 and v0.2 both define as a **singular optional URI for the underlying asset**, not a list — and onto `sources`, where each entry has a REQUIRED `resource` field. kbforge has been emitting a list of dicts under `resource` since the beginning; this is the fix, not a new v0.2 requirement.

**Files:**
- Modify: `src/kbforge/models.py:16-41`
- Modify: `src/kbforge/synthesize.py:42-64`, `src/kbforge/synthesize.py:82-89`
- Modify: `src/kbforge/validate.py:79-88`
- Test: `tests/test_synthesize.py`, `tests/test_validate.py`

**Interfaces:**
- Consumes: `ResourceAnchor(system, native_id, url, retrieved_at, content_hash)` — unchanged shape.
- Produces: `ConceptFrontmatter.sources: list[ResourceAnchor]` (replaces `.resources`); module-private `synthesize._source_entry(a: ResourceAnchor) -> dict`. Task 2 adds fields to the same model; Task 3 reads the emitted key names.

- [ ] **Step 1: Write the failing tests**

In `tests/test_synthesize.py`, replace the `assert fm.resources == [doc.anchor]` line in `test_synthesizes_a_conformant_concept` with `assert fm.sources == [doc.anchor]`, then append:

```python
def test_rendered_frontmatter_uses_okf_02_sources():
    """OKF §5.1: provenance lives in `sources`, each entry carrying a REQUIRED
    `resource`. The bare `resource` key is a singular URI in both v0.1 and v0.2
    and must not be a list."""
    import yaml

    doc = _doc("local_files:apps/x.md")
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    rendered = change.files[concept_path("local_files:apps/x.md")]
    front = yaml.safe_load(rendered.split("---")[1])

    assert "resource" not in front
    assert front["sources"] == [
        {
            "id": "local_files:apps/x.md",
            "resource": "local_files:apps/x.md",
            "content_hash": "h",
        }
    ]


def test_source_entry_prefers_a_real_url_when_the_anchor_has_one():
    """A followable URL is the better `resource`; the scope descriptor is only
    the fallback OKF §5.1 permits when no artifact URL exists."""
    import yaml

    doc = _doc("local_files:apps/x.md")
    doc.anchor.url = "https://wiki.acme/x"
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    rendered = change.files[concept_path("local_files:apps/x.md")]
    front = yaml.safe_load(rendered.split("---")[1])

    assert front["sources"][0]["resource"] == "https://wiki.acme/x"
```

In `tests/test_validate.py`, change every `ConceptFrontmatter(...)` keyword `resources=` to `sources=`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_synthesize.py tests/test_validate.py -q`
Expected: FAIL — `ConceptFrontmatter` has no field `sources` (pydantic raises on the unexpected keyword), and `front` has no key `sources`.

- [ ] **Step 3: Rename the model field**

In `src/kbforge/models.py`, update `ResourceAnchor`'s docstring and `ConceptFrontmatter`:

```python
class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Each anchor becomes one OKF v0.2 `sources` entry at emit time (§5.1), whose
    REQUIRED `resource` field takes this anchor's `url` — or, when there is none,
    the "system:native_id" scope descriptor §5.1 permits in its place."""

    system: str
    native_id: str
    url: str | None = None
    retrieved_at: datetime
    content_hash: str
```

and in `ConceptFrontmatter`, rename the field:

```python
    sources: list[ResourceAnchor] = Field(default_factory=list)  # law 3
```

- [ ] **Step 4: Emit the `sources` block**

In `src/kbforge/synthesize.py`, add the helper above `_render`:

```python
def _source_entry(anchor: ResourceAnchor) -> dict:
    """One OKF v0.2 `sources` entry (§5.1). `resource` is REQUIRED within an
    entry; when the anchor carries no URL, §5.1 permits a scope descriptor in its
    place, and "system:native_id" is the stable one kbforge already uses as a
    doc_id. `content_hash` is a producer extension key (§4.1 permits them): it is
    what makes a published concept auditable back to the canonical form it was
    synthesized from."""
    descriptor = f"{anchor.system}:{anchor.native_id}"
    return {
        "id": descriptor,
        "resource": anchor.url or descriptor,
        "content_hash": anchor.content_hash,
    }
```

Add `ResourceAnchor` to the `kbforge.models` import list at the top of the file. Then in `_render`, replace the `front["resource"] = [...]` block with:

```python
    front["sources"] = [_source_entry(a) for a in fm.sources]
```

and in `assemble`, rename the constructor keyword:

```python
            sources=[doc.anchor],
```

- [ ] **Step 5: Rename the field the law reads**

In `src/kbforge/validate.py`, update `_check_anchor_presence`:

```python
def _check_anchor_presence(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if not concept.sources:
        return [
            Failure(
                path,
                "anchor-presence",
                "concept carries no source anchor (§4.4 law 3)",
            )
        ]
    return []
```

The law slug stays `anchor-presence` — the law is unchanged, only the field it reads is renamed.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, all tests. If `test_pipeline.py` fails on a `resources=` keyword, rename it there too.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/models.py src/kbforge/synthesize.py src/kbforge/validate.py tests/
git commit -m "$(cat <<'EOF'
feat!: emit provenance as OKF v0.2 `sources`

kbforge wrote its anchors as a list of dicts under the `resource` key. Both
v0.1 (spec line 154) and v0.2 (§4.1) define `resource` as a singular optional
URI for the underlying asset, so this was a divergence from the day it was
written, not something v0.2 broke. v0.2 gives provenance a correct home:
`sources`, where `resource` is REQUIRED per entry.

ResourceAnchor maps onto an entry directly — `url` becomes `resource`, falling
back to the "system:native_id" scope descriptor §5.1 permits when the anchor
has no URL, and `system:native_id` becomes the stable `id` that per-claim
footnote attribution will later join on. `content_hash` rides along as a
producer extension key (§4.1), which is what keeps a published concept
auditable back to the canonical form it came from.

Law 3 is unchanged in meaning and keeps its `anchor-presence` slug; only the
projection field it reads is renamed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `timestamp` → `generated` (OKF §5.2, §13.1)

**Files:**
- Modify: `src/kbforge/models.py:27-41`
- Modify: `src/kbforge/synthesize.py:42-64`, `src/kbforge/synthesize.py:67-89`
- Modify: `src/kbforge/llm_synthesizer.py:161`
- Modify: `src/kbforge/validate.py:91-109`, `src/kbforge/validate.py:185`
- Test: `tests/test_synthesize.py`, `tests/test_validate.py`, `tests/test_strict_okf.py`

**Interfaces:**
- Consumes: `ConceptFrontmatter.sources` from Task 1.
- Produces: `ConceptFrontmatter.generated_at: datetime | None` (replaces `.freshness`), `ConceptFrontmatter.generated_by: str`; `assemble(items, changeset, existing_paths, *, generated_by: str = ...)`. Task 3 reads the emitted `generated` key name.

- [ ] **Step 1: Write the failing tests**

In `tests/test_synthesize.py`, change `assert fm.freshness == NOW` to `assert fm.generated_at == NOW` and append:

```python
def test_rendered_frontmatter_uses_okf_02_generated():
    """§13.1: `timestamp` is superseded by `generated: {by, at}`."""
    import yaml

    from kbforge import __version__

    doc = _doc("local_files:apps/x.md")
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    rendered = change.files[concept_path("local_files:apps/x.md")]
    front = yaml.safe_load(rendered.split("---")[1])

    assert "timestamp" not in front
    assert front["generated"] == {
        "by": f"kbforge/{__version__}",
        "at": NOW.isoformat(),
    }


def test_generated_by_is_overridable_for_llm_synthesis():
    """OKF §7 actor convention: the version slot carries the model, following the
    spec's own `reference_agent/gemini-2.5-pro` example."""
    doc = _doc("local_files:apps/x.md")
    change = assemble(
        [(doc, "T", "D", "body")],
        ChangeSet(added=["local_files:apps/x.md"]),
        generated_by="kbforge/deepseek-v4-flash",
    )
    fm = change.concepts[concept_path("local_files:apps/x.md")]

    assert fm.generated_by == "kbforge/deepseek-v4-flash"
```

In `tests/test_validate.py`, rename every `freshness=` keyword to `generated_at=`.

In `tests/test_strict_okf.py`, replace the two `timestamp:` frontmatter lines with `generated: {by: kbforge/test, at: 2026-07-19T00:00:00+00:00}` and append:

```python
MISSING_GENERATED = """---
type: concept
title: X
description: X
---
# X
"""


def test_rendered_file_without_generated_is_reported():
    """kbforge requires `generated` even though OKF §11 requires only `type`:
    producer-side strictness, so law 4 can never be satisfied by a projection
    whose rendered file omits the stamp."""
    failures = run_validators(_proposal("concepts/x/overview.md", MISSING_GENERATED))
    assert any(f.law == "okf-strict" for f in failures)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_synthesize.py tests/test_validate.py tests/test_strict_okf.py -q`
Expected: FAIL — no field `generated_at`, no `generated` key in rendered frontmatter, and `assemble` rejects the `generated_by` keyword.

- [ ] **Step 3: Add the model fields**

In `src/kbforge/models.py`, update `ConceptFrontmatter`'s docstring and last field:

```python
class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF v0.2 concept (kbforge §4.4).

    Fields are permissive so a law-violating concept can be represented and then
    reported by the validators — kbforge checks synthesis output, it does not
    trust it (spec §5). `type` serializes onto the OKF `type` key; `generated_by`
    and `generated_at` onto `generated: {by, at}` (§5.2); each `sources` entry
    onto one OKF `sources` entry (§5.1). This is the §4.4 projection, not the
    whole frontmatter: title, description, and the rendered body live in the file
    the publisher writes."""

    type: str = ""  # OKF's one required field (checked non-empty by validate)
    facets: dict = Field(default_factory=dict)  # law 1
    sources: list[ResourceAnchor] = Field(default_factory=list)  # law 3
    links: list[str] = Field(default_factory=list)  # law 2
    generated_at: datetime | None = None  # law 4 — OKF `generated.at`
    generated_by: str = ""  # OKF `generated.by`, an §7 actor
```

- [ ] **Step 4: Emit the `generated` block and thread the actor**

In `src/kbforge/synthesize.py`, add near the top after the imports:

```python
from kbforge import __version__

_DEFAULT_ACTOR = f"kbforge/{__version__}"
```

Add the helper above `_render`:

```python
def _generated(fm: ConceptFrontmatter) -> dict:
    """The OKF v0.2 `generated` block (§5.2), which supersedes v0.1 `timestamp`.

    `at` is the anchor's `retrieved_at` — a fetch time standing in for "last
    meaningful change". kbforge earns that equivalence from the no-op rule: a
    concept is re-rendered only when its canonical form actually changed, so the
    run that rewrote it *is* its last meaningful change. A producer without
    canonicalization cannot make the same claim."""
    out: dict = {"by": fm.generated_by}
    if fm.generated_at is not None:
        out["at"] = fm.generated_at.isoformat()
    return out
```

In `_render`, replace the `"timestamp": ...` entry of the `front` dict with:

```python
        "generated": _generated(fm),
```

Change `assemble`'s signature to accept the actor, keyword-only with a default:

```python
def assemble(
    items: list[tuple[CanonicalDocument, str, str, str]],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
    *,
    generated_by: str = _DEFAULT_ACTOR,
) -> ProposedChange:
```

and in its `ConceptFrontmatter(...)` construction, replace `freshness=doc.anchor.retrieved_at` with:

```python
            generated_at=doc.anchor.retrieved_at,
            generated_by=generated_by,
```

- [ ] **Step 5: Pass the model-derived actor from the LLM synthesizer**

In `src/kbforge/llm_synthesizer.py:161`:

```python
        proposal = assemble(
            items, changeset, existing_paths, generated_by=f"kbforge/{config.model}"
        )
```

If the local variable holding the config is not named `config` at that point, use whatever name is in scope — the value needed is the same `model` string validated at `llm_synthesizer.py:61-62`.

- [ ] **Step 6: Update the validators**

In `src/kbforge/validate.py`, rename the field `_check_freshness_legible` reads (keep the function name and the `freshness-legibility` slug — the law is unchanged):

```python
def _check_freshness_legible(path: str, concept: ConceptFrontmatter) -> list[Failure]:
    if concept.generated_at is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept carries no freshness stamp (§4.4 law 4)",
            )
        ]
    if concept.generated_at.utcoffset() is None:
        return [
            Failure(
                path,
                "freshness-legibility",
                "concept freshness stamp is timezone-naive; whats_stale needs an "
                "aware datetime (§4.4 law 4)",
            )
        ]
    return []
```

and update the strict-OKF required tuple:

```python
# Stricter than OKF §11, which requires only a non-empty `type`. kbforge is a
# producer: it holds its own output to `title`/`description`/`generated` so a
# rendered file can never satisfy the §4.4 projection while omitting the stamp
# law 4 depends on. Consumers stay permissive; producers do not have to be.
_STRICT_REQUIRED = ("type", "title", "description", "generated")
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, all tests.

- [ ] **Step 8: Commit**

```bash
git add src/kbforge tests/
git commit -m "$(cat <<'EOF'
feat!: emit `generated` instead of the superseded `timestamp`

OKF v0.2 §13.1 supersedes `timestamp` with `generated: {by, at}`. The rename is
the small half; the modelling is the point. v0.1's `timestamp` meant "last
meaningful change", and kbforge was filling it with the anchor's retrieved_at —
a fetch time. v0.2 splits the two ideas (`generated.at` for the concept,
`sources[].last_modified` for the source), and the no-op rule is what lets
kbforge keep using retrieved_at honestly for the first: a concept is re-rendered
only when its canonical form actually changed, so the run that rewrote it is its
last meaningful change.

`generated.by` follows the §7 actor convention — `kbforge/<version>` for the
stub synthesizer and `kbforge/<model>` for the LLM one, matching the spec's own
`reference_agent/gemini-2.5-pro` example where the version slot carries the
model. Both are non-`human:` actors, so a freshly produced concept sits in the
unverified tier until a human merges it, which is exactly true of kbforge.

_STRICT_REQUIRED now names `generated`. It stays stricter than §11 conformance
(which requires only `type`) on purpose, and the comment now says so rather than
leaving it to read as a misreading of the spec.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Reserve the new emit-side keys in the connector

`local_files` reads source markdown frontmatter into `structured`, which becomes facets. Any source key colliding with a key the synthesizer owns would be emitted twice with different meanings. The reserved set still names the retired v0.1 keys, so a v0.1-era source document cannot inject a superseded key into the rendered output.

**Files:**
- Modify: `src/kbforge/connectors/local_files.py:25-32`
- Test: `tests/test_local_files_connector.py`

**Interfaces:**
- Consumes: the emitted key names from Tasks 1 and 2 (`sources`, `generated`).
- Produces: nothing downstream depends on this beyond the connector's own output.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_local_files_connector.py`:

```python
def test_emit_side_okf_keys_never_become_facets(tmp_path):
    """A source doc carrying keys the synthesizer owns must not have them flow
    into `structured` — they would be rendered twice, with the source's meaning
    fighting the emit-side one. The retired v0.1 names stay reserved so a
    v0.1-era document cannot reintroduce a superseded key either."""
    _write(
        tmp_path,
        "a.md",
        "---\ntitle: A\ngenerated: nope\nsources: nope\n"
        "timestamp: nope\nresource: nope\n---\nbody\n",
    )

    docs = _normalize(tmp_path)

    for key in ("generated", "sources", "timestamp", "resource"):
        assert key not in docs[0].structured
```

If the file's existing helpers are not named `_write` / `_normalize`, reuse whatever the neighbouring tests at `tests/test_local_files_connector.py:111-116` use — that test does exactly this for `links` and `resource`.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_local_files_connector.py::test_emit_side_okf_keys_never_become_facets -q`
Expected: FAIL — `assert "generated" not in docs[0].structured` fails; `generated` and `sources` are not yet reserved.

- [ ] **Step 3: Extend the reserved set**

In `src/kbforge/connectors/local_files.py`:

```python
# Keys handled structurally, so they never leak into `structured` (hence facets):
# title → the concept title; relations → cross-links; type is dropped here because
# the OKF type comes from synthesis taxonomy (the stub emits "concept"); description,
# generated, sources, and links are emit-side OKF v0.2 fields the synthesizer owns —
# a source key of the same name must not collide with them in the rendered
# frontmatter. The retired v0.1 names (timestamp, resource) stay reserved so a
# v0.1-era source document cannot reintroduce a superseded key as a facet.
_RESERVED_KEYS = frozenset(
    {
        "type",
        "title",
        "relations",
        "description",
        "generated",
        "sources",
        "links",
        "timestamp",
        "resource",
    }
)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/connectors/local_files.py tests/test_local_files_connector.py
git commit -m "$(cat <<'EOF'
fix: reserve the v0.2 emit-side keys in local_files

`generated` and `sources` are now synthesizer-owned, so a source document
carrying either would have had it rendered twice with two different meanings.
The retired v0.1 names stay in the reserved set as well: a v0.1-era source doc
should not be able to reintroduce `timestamp` or a list-shaped `resource` into
output that no longer emits them.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Docs, deferred-decisions note, changelog, version bump

**Files:**
- Create: `docs/design/2026-08-08-okf-02-deferred-decisions.md`
- Modify: `README.md:10-11`, `README.md:21`
- Modify: `docs/architecture.md:8`, `:19`, `:25`, `:117`, `:200-209`, `:326-332`
- Modify: `docs/design/2026-07-18-agent-facing-artifact-contract-design.md:8`
- Modify: `CHANGELOG.md:8`
- Modify: `pyproject.toml:3`

**Interfaces:**
- Consumes: the final emitted field names from Tasks 1–3.
- Produces: nothing code-facing.

- [ ] **Step 1: Bump the version**

`pyproject.toml:3`: `version = "0.4.0"` → `version = "0.5.0"`.

- [ ] **Step 2: Update the OKF version references**

Read each hit individually — **do not** run a blind find-and-replace:

```bash
grep -rn 'v0\.1\|okf_version' README.md docs/
```

Change only the references that mean *OKF's* version:
- `README.md:11` — "(OKF) v0.1 standardizes" → "(OKF) v0.2 standardizes"
- `README.md:21` — table cell "OKF v0.1" → "OKF v0.2"
- `docs/architecture.md:8` — frontmatter `okf_version: "0.1"` → `"0.2"`
- `docs/architecture.md:19` — "OKF v0.1 standardizes" → "OKF v0.2 standardizes"
- `docs/architecture.md:25` — table cell "OKF v0.1 (Google)" → "OKF v0.2 (Google)"
- `docs/design/2026-07-18-agent-facing-artifact-contract-design.md:8` — frontmatter `okf_version` → `"0.2"`

**Leave alone** — these mean kbforge's own document or law-set version, not OKF's:
- `docs/architecture.md:13` "Status: Draft v0.1"
- `docs/architecture.md:355` "the complete v0.1 set"
- `docs/architecture.md:710` "out of scope for v0.1"
- every "Status: Draft v0.1" line in `docs/design/*`
- `docs/design/2026-07-18-agent-facing-artifact-contract-design.md:122`, `:232`, `:328`, `:332`

- [ ] **Step 3: Update the field names in prose**

In `docs/architecture.md`, update the model sketch and law text so they name v0.2 fields:
- `:117` — "Each anchor becomes one OKF `resource` frontmatter entry at emit time." → "Each anchor becomes one OKF v0.2 `sources` entry at emit time (§5.1), whose REQUIRED `resource` field takes this anchor's `url`, or the `system:native_id` scope descriptor §5.1 permits when there is none."
- `:200-209` — in the `ConceptFrontmatter` sketch, rename `resources` → `sources` and `freshness` → `generated_at`, add `generated_by: str`, and rewrite the docstring sentence "At write time `freshness` serializes to the OKF `timestamp` key … and each anchor in `resources` to a `resource` entry" to name `generated: {by, at}` and `sources`.
- `:326-332` — law 3 and law 4 text: "≥1 `resource` anchor in frontmatter" → "≥1 `sources` entry in frontmatter"; law 4's "machine-readable freshness stamp" now names `generated.at`.

Add a short paragraph at the end of kbforge §4.4 (after the four laws, before "What the runtime enforces"):

```markdown
**The laws now speak OKF's vocabulary.** v0.1 standardized only the artifact
shell, so laws 3 and 4 had to invent field names — `resource` for provenance,
`timestamp` for freshness. v0.2 makes provenance, trust, and lifecycle
first-class, and the laws now read its fields directly: law 3 checks `sources`
(§5.1), law 4 checks `generated.at` (§5.2). Nothing about what they enforce
changed; the encoding stopped being private. Two families v0.2 adds — `verified`
(§5.2) and `stale_after` (§5.5) — are deliberately not emitted yet; see
[`design/2026-08-08-okf-02-deferred-decisions.md`](design/2026-08-08-okf-02-deferred-decisions.md).
```

- [ ] **Step 4: Write the deferred-decisions design note**

Create `docs/design/2026-08-08-okf-02-deferred-decisions.md` with frontmatter matching the sibling design notes (`type: design-note`, `okf_version: "0.2"`, `status: draft`) and one section per deferred family. Each must record the decision *and* the reason it is not mechanical:

1. **`verified` (§5.2) and the human gate.** kbforge never auto-merges, so every concept reaching `main` passed human review — structurally the `human-reviewed` tier (§5.3), which no other producer in this space can claim by construction. The blocker: kbforge cannot stamp it at publish time, because review has not happened yet. The stamp belongs to the merge event, which kbforge deliberately does not own (the never-merge rule, §5.2 of architecture.md). Options to weigh: a post-merge `kbforge stamp-verified` run by the deployment's CI, versus leaving it entirely to the deployment. Note that emitting `verified` from the producer would be a lie the artifact laws could not catch.
2. **`status: deprecated` (§5.4) versus hard delete.** kbforge deletes on tombstone. `deprecated` means "kept for links and history; no longer current". Deleting is lossier than it looks: law 2 filters links only in concepts rendered *this run*, so a concept already on `main` that links to a deleted one and is out of scope keeps a dangling link. §6.1 says consumers MUST tolerate broken links, so this is not a conformance failure — but `deprecated` preserves the graph, and v0.2 now sanctions it. Changing this changes deletion semantics, so it needs its own decision.
3. **`stale_after` (§5.5).** An absolute date would turn law 4 from "here is when we synced, you decide" into a real freshness policy and give `whats_stale` a direct answer. Needs a config surface (a per-connector TTL) that does not exist yet.
4. **Footnote attribution (§5.1).** `[^id]` labels joining to `sources[].id` standardize per-claim grounding — the shape the deferred faithfulness judge (architecture.md §7) would check, and the path from law 3's *anchor presence* to *anchor validity* (artifact-contract spec §10). Record the open question: kbforge's `id` is `system:native_id`, which contains `:` and `/`, and whether those make well-behaved markdown footnote labels needs checking before committing to the format.

- [ ] **Step 5: Write the changelog entry**

Under `## [Unreleased]` in `CHANGELOG.md`, add a `## [0.5.0] - 2026-08-08` section with `### Changed` covering both breaking renames — stating plainly that `resource` was a divergence from v0.1 as well, not something v0.2 broke — and a `### Notes` line pointing at the deferred-decisions note. Match the prose density of the 0.4.0 entry above it.

- [ ] **Step 6: Verify docs and suite**

```bash
grep -rn 'timestamp\|\bresources\b' src/kbforge docs/architecture.md README.md | grep -v 'retrieved_at\|fromtimestamp\|last_modified'
uv run pytest -q
```
Expected: the grep returns only intentional hits (the §13.1 migration notes explaining what `timestamp` was); the suite passes.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: track OKF v0.2, record the deferred families, cut 0.5.0

README and architecture.md now say v0.2 and name the fields the code actually
emits. The §4.4 laws get a short paragraph on what changed: v0.1 standardized
only the artifact shell, so laws 3 and 4 had to invent `resource` and
`timestamp`; v0.2 makes provenance and lifecycle first-class, so they read
`sources` and `generated.at` instead. What the laws enforce is unchanged — the
encoding stopped being private.

The new design note records the four v0.2 families deliberately not emitted yet
and why each needs a decision rather than an implementation: `verified` (the
stamp belongs to the merge event kbforge does not own), `deprecated` versus hard
delete (changes deletion semantics), `stale_after` (needs a config surface), and
footnote attribution (rides with the faithfulness judge, and the `id` format
needs checking against markdown footnote labels first).

The v0.1 mentions that refer to kbforge's own doc and law-set versions are
deliberately untouched.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage.** §13.1's two breaking changes are Tasks 1 and 2. §5.1's entry shape and §7's actor convention are covered. §11 conformance is addressed by the `_STRICT_REQUIRED` comment. §5.2 `verified`, §5.4 `status`, §5.5 `stale_after`, and §5.1 footnotes are recorded as deferred in Task 4 Step 4 — the only spec sections with no code, deliberately. §10 Attested Computation is out of scope: kbforge produces knowledge, not sanctioned computations, and the plan does not mention it beyond this line. `okf_version` in a bundle-root `index.md` (§12) is *not* covered — kbforge emits no `index.md`, which is a real gap but a feature, not a conformance fix; it is not in this plan.

**Placeholder scan.** No TBDs. Every code step carries the literal code. Two steps hedge on local names (`llm_synthesizer.py:161`'s config variable, `test_local_files_connector.py`'s helpers) with an explicit instruction on what to look for — deliberate, since neither name is verifiable from the excerpt.

**Type consistency.** `ConceptFrontmatter.sources` (Task 1) is read by `_source_entry` and `_check_anchor_presence`; `generated_at`/`generated_by` (Task 2) by `_generated` and `_check_freshness_legible`. `assemble`'s new keyword `generated_by: str` matches its use in `llm_synthesizer.py` and in Task 2's test. `_DEFAULT_ACTOR` is defined once in `synthesize.py` and referenced in the test via `f"kbforge/{__version__}"`, which is the same value.
