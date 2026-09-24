# Editorial Links (#41) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Editorial links declared in a reviewed `links.yaml`, resolved by `doc_id` across systems, rendered in a pipeline-owned `## Related` body section (with connector `relations`), and kept current across runs by a `_links/` sidecar.

**Architecture:** Two new pure-ish modules. `related.py` renders and parses the `## Related` section; the validator uses it to bind the section to the projection's `links`. `links.py` loads and validates `links.yaml`, resolves a document's links against `by_id`, and reads/writes the `_links/` sidecar and its drift. The pipeline resolves links for every concept it synthesizes, hands the synthesizer a copy with resolved `relations`, adds the targets to `existing`, appends the section after synthesis, and runs a link-drift scan beside the grounding drift scan. The cross-system-relation abort is lifted.

**Tech Stack:** Python 3.12, pydantic v2, PyYAML, pytest. `uv run pytest`, `prek` (ruff + ty) on commit.

**Spec:** `docs/design/2026-09-23-declarative-links-design.md` (read §3–§7 and §10 before starting any task).

## Global Constraints

- The pipeline order stays fixed. Link resolution and link drift sit where grounding's do; the `## Related` render sits between synthesize and validate.
- The no-op rule gains exactly one clause: return `NoOp()` when `ChangeSet.is_noop` and there is no grounding drift **and no link drift**.
- Links reach **synthesis copies only**. `commit()` always receives the connector's own documents. The mirror must be byte-identical with and without `--links`.
- `normalize` stays pure. Nothing in this plan touches a connector.
- `kbforge never merges`: no `def .*merge` in `src/kbforge/publishers/`.
- The `## Related` section is a second carrier of `links`. `_check_related_section` binds it to the projection, next to `_check_carriers_agree`, under the law slug `okf-strict`.
- Section marker, verbatim: `<!-- kbforge:related -->`. Heading, verbatim: `## Related`. Line format: `- [Title](/concepts/…/overview.md)`, plus ` — note` (space, em dash U+2014, space) when a note exists.
- Paths in the section are bundle-absolute (a leading `/`). Frontmatter `links` keeps its current bundle-relative form.
- Sidecar directory: `_links`. Payload: `{"doc_id": …, "links": [[target_doc_id, note_or_null], …]}`, written through `grounding.write_atomic`.
- Every `links.yaml` key and `to` must be a qualified `doc_id` (`system:native_id`); `extra="forbid"` on both models.
- A reference to a document not in the mirror is **not** a config error. It is a review note: `<path>: link to <id> (links.yaml) was not found in the mirror or this fetch and was dropped`.
- Cross-system review note, verbatim form: `<path>: links to <target_id> (system <system>); merge that system's review request first, or the link dangles until it does`.
- Link-drift review note, verbatim: `<path>: re-synthesized because its links changed since it was last published; its own source is unchanged`.
- Test messages, not just slugs (CLAUDE.md, "Verifying a gate").
- Mutation checks mutate **in place** and restore with `git checkout -- <file>`. Commit before mutating. Run with `PYTHONDONTWRITEBYTECODE=1` so stale bytecode can't mask a mutation.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Deviation from the spec, settled here

Spec §6.1 says the sidecar is written "only when the managed set is non-empty". That strands a concept whose only managed link was unresolvable at first publish: there's no sidecar, no `--links` to trip the gate (for a cross-system *relation*), and so no rescan when the target arrives. This is the same trap grounding closed with an empty sidecar. **Write the sidecar whenever the concept *declares* a managed link, even if the resolved set is empty. Delete it when nothing managed is declared.** `LinkResolution.declares_managed` carries this.

## Review Focus

1. **Markdown-hostile titles and paths** (`]`, `(`, spaces, `%`, `@`, backticks). Expected: the section still round-trips through `section_targets` to exactly the projection's links. Test in Task 1.
2. **The marker text inside a source's frontmatter** (a facet value). Expected: it is not taken as a section; only the body is searched. Test in Task 1.
3. **Retitling a link target.** Expected: no rebuild of its referrers, since link drift compares targets and notes, not titles (spec §5, "Titles can go stale"). Test in Task 5.
4. **Dropping `--links` after using it.** Expected: the leftover sidecars trip the scan, each formerly linked concept is rebuilt once without its editorial links, its sidecar is deleted, and the next run is `NoOp`. Test in Task 5.
5. **A synthesizer that drops a document from its output.** Expected: that concept's `_links/` sidecar is left exactly as it was, matching the `_described/` and `_grounding/` rule. Test in Task 4.

---

### Task 1: The `## Related` section and its binding validator

**Files:**
- Create: `src/kbforge/related.py`
- Modify: `src/kbforge/validate.py`, where `_check_strict_okf` calls `_check_carriers_agree`, plus a new function after `_check_carriers_agree`
- Test: `tests/test_related.py` (new)

**Interfaces:**
- Produces:
  - `related.MARKER: str`
  - `related.render_section(links: list[str], titles: dict[str, str], notes: dict[str, str]) -> str`
  - `related.with_related(proposal: ProposedChange, titles: dict[str, str], notes: dict[str, dict[str, str]]) -> None`. It mutates `proposal.files`.
  - `related.section_targets(content: str) -> list[str] | None`
  - `validate._check_related_section(path: str, content: str, concept: ConceptFrontmatter) -> list[Failure]`
  - `titles` is keyed by concept path. `notes` maps a source concept path to a dict of target concept path to note.

- [ ] **Step 1: Write the failing tests**

`tests/test_related.py`:

```python
"""The `## Related` section (#41, architecture.md §7.4): rendering, parsing,
and the validator that binds it to the projection's `links`."""

from datetime import UTC, datetime

import pytest

from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor
from kbforge.related import MARKER, render_section, section_targets, with_related
from kbforge.synthesize import assemble, concept_path
from kbforge.validate import run_validators

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _doc(native: str, relations: tuple[str, ...] = ()) -> CanonicalDocument:
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system="s", native_id=native, retrieved_at=NOW, content_hash="h"
        ),
        doc_id=f"s:{native}",
        title=native.upper(),
        text="body",
        relations=list(relations),
    )


X, Y, Z = (concept_path(f"s:{n}") for n in "xyz")
EXISTING = frozenset({Y, Z})


def _linked():
    x = _doc("x", ("s:y", "s:z"))
    return assemble([(x, "X", "X", "body")], ChangeSet(added=["s:x"]), EXISTING)


def _messages(change) -> list[str]:
    return [f.message for f in run_validators(change, EXISTING)]


def test_render_lists_links_in_order_with_title_and_note():
    out = render_section(
        ["concepts/a/overview.md", "concepts/b/overview.md"],
        {"concepts/a/overview.md": "Alpha"},
        {"concepts/a/overview.md": "why they relate"},
    )
    assert out == (
        f"{MARKER}\n## Related\n\n"
        "- [Alpha](/concepts/a/overview.md) — why they relate\n"
        "- [concepts/b/overview.md](/concepts/b/overview.md)\n"
    )


@pytest.mark.parametrize(
    "path",
    [
        "concepts/a b/overview.md",
        "concepts/x(1)/overview.md",
        "concepts/100%/overview.md",
        "concepts/[x]/overview.md",
        "concepts/@en.wikipedia.org/overview.md",
    ],
)
def test_a_hostile_path_and_title_round_trip(path):
    out = render_section([path], {path: "T ] [ ` < \\ x"}, {})
    assert section_targets(f"# X\n\nbody\n\n{out}") == [path]


def test_an_at_sign_stays_readable():
    out = render_section(["concepts/@en/overview.md"], {}, {})
    assert "(/concepts/@en/overview.md)" in out


def test_no_marker_means_no_section():
    assert section_targets("---\ntype: concept\n---\n# X\n\nbody\n") is None


def test_only_the_last_marker_counts():
    fake = f"{MARKER}\n- [Fake](/concepts/fake/overview.md)\n"
    content = f"# X\n\n{fake}\n{render_section([Y], {}, {})}"
    assert section_targets(content) == [Y]


def test_a_marker_in_the_frontmatter_is_not_a_section():
    content = f"---\ntype: concept\nnote: '{MARKER}'\n---\n# X\n\nbody\n"
    assert section_targets(content) is None


def test_with_related_appends_only_to_linked_concepts():
    x = _doc("x", ("s:y",))
    y = _doc("y")
    change = assemble(
        [(x, "X", "X", "body"), (y, "Y", "Y", "body")],
        ChangeSet(added=["s:x", "s:y"]),
    )
    with_related(change, {Y: "Why"}, {X: {Y: "a note"}})
    assert change.files[X].endswith(
        f"body\n\n{MARKER}\n## Related\n\n- [Why](/{Y}) — a note\n"
    )
    assert MARKER not in change.files[Y]


def test_a_rendered_section_passes_the_gate():
    change = _linked()
    with_related(change, {}, {})
    assert run_validators(change, EXISTING) == []


def test_links_without_a_section_fail():
    assert any("no '## Related' section" in m for m in _messages(_linked()))


def test_a_link_only_in_the_section_fails():
    change = _linked()
    with_related(change, {}, {})
    change.files[X] = (
        change.files[X].rstrip("\n") + "\n- [Ghost](/concepts/ghost/overview.md)\n"
    )
    assert any(
        "that the projection's 'links' do not" in m
        and "concepts/ghost/overview.md" in m
        for m in _messages(change)
    )


def test_a_link_only_in_the_frontmatter_fails():
    change = _linked()
    with_related(change, {}, {})
    z_line = next(ln for ln in change.files[X].splitlines() if f"(/{Z})" in ln)
    change.files[X] = change.files[X].replace(z_line + "\n", "")
    assert any("omits" in m and Z in m for m in _messages(change))


def test_a_section_on_a_linkless_concept_fails():
    x = _doc("x")
    change = assemble(
        [(x, "X", "X", f"body\n\n{MARKER}\n## Related\n")], ChangeSet(added=["s:x"])
    )
    assert any("on a concept with no links" in m for m in _messages(change))


def test_links_out_of_order_fail():
    change = _linked()
    with_related(change, {}, {})
    lines = change.files[X].splitlines()
    i, j = [k for k, ln in enumerate(lines) if ln.startswith("- [")]
    lines[i], lines[j] = lines[j], lines[i]
    change.files[X] = "\n".join(lines) + "\n"
    assert any("out of order" in m for m in _messages(change))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_related.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'kbforge.related'`.

- [ ] **Step 3: Implement `src/kbforge/related.py`**

```python
"""The `## Related` body section: every link a concept carries, rendered where
OKF v0.2 puts links (§6.1), with the reason as prose after the link.

A second carrier of `links`, so `validate._check_related_section` binds it to
the projection. The pipeline renders it after synthesis, never a synthesizer:
it is frame, not prose, so every synthesizer gets the same one and none can
forge it (architecture.md §7.4)."""

from __future__ import annotations

import re
from urllib.parse import unquote

from kbforge.models import ProposedChange

MARKER = "<!-- kbforge:related -->"
"""Invisible when rendered. It lets the validator find the section even when a
source body has a `## Related` heading of its own; the last one wins."""

_UNSAFE = frozenset(' #?%()<>[]\\"`{}|^')
"""What would end, redirect or garble a CommonMark link destination; the same
set okfquery's index encodes. Everything else stays readable (`@`, unicode)."""

_LINE = re.compile(r"^- \[(?:\\.|[^\\\]])*\]\(/([^)\s]*)\)")


def _text(text: str) -> str:
    # Backslash first, so the escapes added after it are not doubled.
    for char in "\\[]`<":
        text = text.replace(char, "\\" + char)
    return text


def _target(path: str) -> str:
    return "".join(
        "".join(f"%{b:02X}" for b in c.encode("utf-8"))
        if c in _UNSAFE or ord(c) < 0x21 or ord(c) == 0x7F
        else c
        for c in path
    )


def render_section(
    links: list[str], titles: dict[str, str], notes: dict[str, str]
) -> str:
    """The section for `links`, in their order (the projection's, sorted)."""
    lines = [MARKER, "## Related", ""]
    for path in links:
        title = " ".join((titles.get(path) or "").split()) or path
        line = f"- [{_text(title)}](/{_target(path)})"
        note = " ".join((notes.get(path) or "").split())
        lines.append(f"{line} — {note}" if note else line)
    return "\n".join(lines) + "\n"


def with_related(
    proposal: ProposedChange,
    titles: dict[str, str],
    notes: dict[str, dict[str, str]],
) -> None:
    """Append the section to every rendered concept whose projection has links."""
    for path, concept in proposal.concepts.items():
        if not concept.links or path not in proposal.files:
            continue
        body = proposal.files[path].rstrip("\n")
        section = render_section(concept.links, titles, notes.get(path, {}))
        proposal.files[path] = f"{body}\n\n{section}"


def _body(content: str) -> str:
    """Everything after the frontmatter, so a facet value that happens to hold
    the marker is not read as a section."""
    if not content.startswith("---"):
        return content
    _, _, rest = content.partition("---")
    _, sep, body = rest.partition("\n---")
    return body if sep else content


def section_targets(content: str) -> list[str] | None:
    """Bundle-relative targets listed after the last marker, in order; None when
    the body has no marker."""
    _, sep, tail = _body(content).rpartition(MARKER)
    if not sep:
        return None
    return [
        unquote(m.group(1))
        for line in tail.splitlines()
        if (m := _LINE.match(line))
    ]
```

- [ ] **Step 4: Add the validator to `src/kbforge/validate.py`**

Add the import `from kbforge.related import section_targets`. Then add this function directly after `_check_carriers_agree`:

```python
def _check_related_section(
    path: str, content: str, concept: ConceptFrontmatter
) -> list[Failure]:
    """Bind the `## Related` body section to the projection's `links`.

    The section is a second carrier of `links` (architecture.md §7.4): OKF §6.1
    readers follow body links, while law 2 resolves the projection. If the two
    disagree, a reader follows a link the gate never checked, or never sees one
    it did. Read after the LAST marker, so a source body that carries the
    marker text fails here loudly rather than shipping ambiguous links."""
    listed = section_targets(content)
    if listed is None:
        if concept.links:
            return [
                Failure(
                    path,
                    "okf-strict",
                    "concept has links but no '## Related' section; an OKF "
                    "reader follows body links and would never see them",
                )
            ]
        return []
    if not concept.links:
        return [
            Failure(
                path,
                "okf-strict",
                "'## Related' section on a concept with no links; a body link "
                "the projection lacks is never checked by law 2",
            )
        ]
    failures: list[Failure] = []
    extra = sorted(set(listed) - set(concept.links))
    if extra:
        failures.append(
            Failure(
                path,
                "okf-strict",
                f"'## Related' lists {extra} that the projection's 'links' do "
                "not; law 2 resolves the projection, so these were never checked",
            )
        )
    missing = sorted(set(concept.links) - set(listed))
    if missing:
        failures.append(
            Failure(
                path,
                "okf-strict",
                f"'## Related' omits {missing} from the projection's 'links'; a "
                "reader of the body never sees them",
            )
        )
    if not failures and listed != concept.links:
        failures.append(
            Failure(
                path,
                "okf-strict",
                "'## Related' lists the projection's links out of order or "
                "more than once",
            )
        )
    return failures
```

In `_check_strict_okf`, extend the existing projection branch:

```python
        if concept is not None:
            failures += _check_carriers_agree(path, front, concept)
            failures += _check_related_section(path, content, concept)
```

- [ ] **Step 5: Wire the render into the pipeline**

Without this, every pipeline run that publishes a link aborts on the new validator. In `src/kbforge/pipeline.py`, import `from kbforge.related import with_related` and add this directly before `failures = run_validators(proposal, existing)`:

```python
    # Frame, not prose: rendered here so every synthesizer gets the same
    # section and none can forge it. Bound to the projection by
    # `validate._check_related_section` (architecture.md §7.4).
    with_related(proposal, {concept_path(i): d.title for i, d in by_id.items()}, {})
```

`by_id` is always built by this point, since the run is past the first no-op gate. Task 4 adds notes to this call.

- [ ] **Step 6: Run the tests, then the whole suite**

Run: `uv run pytest tests/test_related.py -q`, then `uv run pytest -q`.
Expected: all pass.

A test that fails with `no '## Related' section` is running `run_validators` over raw synthesizer output with links; only the pipeline appends the section. Fix each one by calling `with_related(change, {}, {})` before `run_validators`, with the comment `# the pipeline appends this after synthesis`. A test that asserts a published file's exact bytes may now see the section appended to a linked concept: update only that expectation. Name every test you touched in the report. Any other failure: stop and report.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/related.py src/kbforge/validate.py src/kbforge/pipeline.py tests/
git commit -m "feat: the ## Related section and its binding validator (#41)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 8: Mutation-check the validator**

Do each mutation in place in `src/kbforge/validate.py`. Run `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_related.py -q`, confirm that exactly the named test fails, then `git checkout -- src/kbforge/validate.py`:
- Delete the `failures += _check_related_section(...)` line. Expected: all five failure tests fail.
- Change `if extra:` to `if False:`. Expected: only `test_a_link_only_in_the_section_fails` fails.
- Change `if missing:` to `if False:`. Expected: only `test_a_link_only_in_the_frontmatter_fails` fails.
- Change `if not concept.links:` (the second one) to `if False:`. Expected: `test_a_section_on_a_linkless_concept_fails` fails.

Also in `src/kbforge/related.py`: change `rpartition(MARKER)` to `partition(MARKER)`. Expected: `test_only_the_last_marker_counts` fails. Restore it.

Record every result in the report.

---

### Task 2: `links.yaml`, its model and validation

**Files:**
- Create: `src/kbforge/links.py`
- Test: `tests/test_links.py` (new)

**Interfaces:**
- Consumes: `grounding.is_qualified(value: str) -> bool`
- Produces:
  - `links.LinkEntry(to: str, note: str | None = None, symmetric: bool = False)`
  - `links.LinksConfig(links: dict[str, list[str | LinkEntry]])`
  - `links.load_links(path: Path | None) -> LinksConfig | None`
  - `links.links_problems(cfg: LinksConfig) -> list[str]`
  - `links.expand(cfg: LinksConfig) -> dict[str, dict[str, str | None]]`: source doc_id to {target doc_id: note}, with symmetric reverses included.

- [ ] **Step 1: Write the failing tests**

`tests/test_links.py`:

```python
"""Editorial links (#41, architecture.md §7.4): config, resolution, sidecar."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.links import LinksConfig, expand, links_problems, load_links


def _cfg(links: dict) -> LinksConfig:
    return LinksConfig.model_validate({"links": links})


def test_a_plain_id_and_an_entry_both_load_and_expand():
    cfg = _cfg(
        {"a:x": ["b:y", {"to": "b:z", "note": "why", "symmetric": True}]}
    )
    assert links_problems(cfg) == []
    assert expand(cfg) == {
        "a:x": {"b:y": None, "b:z": "why"},
        "b:z": {"a:x": "why"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"link": {}},  # a typo'd top-level key
        {"links": {"a:x": [{"to": "b:y", "symetric": True}]}},  # a typo'd entry key
    ],
)
def test_an_unknown_key_is_rejected(raw):
    with pytest.raises(ValidationError):
        LinksConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("links", "message"),
    [
        (
            {"x": ["b:y"]},
            "links key 'x' must be a qualified doc_id ('system:native_id'); "
            "bare ids are not accepted",
        ),
        (
            {"a:x": ["y"]},
            "link 'y' under 'a:x' must be a qualified doc_id "
            "('system:native_id'); bare ids are not accepted",
        ),
        ({"a:x": ["a:x"]}, "link under 'a:x' points at itself"),
        (
            {"a:x": [{"to": "b:y", "note": "  "}]},
            "note on 'a:x' -> 'b:y' must be one non-blank line",
        ),
        (
            {"a:x": [{"to": "b:y", "note": "two\nlines"}]},
            "note on 'a:x' -> 'b:y' must be one non-blank line",
        ),
        ({"a:x": ["b:y", "b:y"]}, "'a:x' -> 'b:y' is declared more than once"),
        (
            {"a:x": [{"to": "b:y", "symmetric": True}], "b:y": ["a:x"]},
            "'b:y' -> 'a:x' is declared more than once",
        ),
    ],
)
def test_each_problem_is_reported_by_text(links, message):
    assert message in links_problems(_cfg(links))


def test_an_unresolvable_id_is_not_a_config_problem():
    # It may live in a system that has not synced yet (§3.1).
    assert links_problems(_cfg({"a:x": ["never:synced"]})) == []


def test_no_path_means_no_config():
    assert load_links(None) is None


def test_an_empty_file_is_an_empty_config(tmp_path: Path):
    path = tmp_path / "links.yaml"
    path.write_text("", "utf-8")
    assert load_links(path) == LinksConfig()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_links.py -q`
Expected: `ModuleNotFoundError: No module named 'kbforge.links'`.

- [ ] **Step 3: Implement the config half of `src/kbforge/links.py`**

```python
"""Editorial links (#41; architecture.md §7.4): declared in a reviewed
`links.yaml`, resolved by doc_id over the whole mirror, rendered in the
pipeline-owned `## Related` section, and kept current by a sidecar of what
each concept last published.

A pipeline flag, not connector config, for `--grounding`'s reason: a connector
must not know other systems exist (`normalize` is pure)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.grounding import is_qualified


class LinkEntry(BaseModel):
    """`extra="forbid"`, so `symetric: true` is an error, not a one-way link."""

    model_config = ConfigDict(extra="forbid")

    to: str
    note: str | None = None
    symmetric: bool = False


class LinksConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: dict[str, list[str | LinkEntry]] = Field(default_factory=dict)


def load_links(path: Path | None) -> LinksConfig | None:
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return LinksConfig.model_validate(raw)


def _entries(cfg: LinksConfig) -> list[tuple[str, LinkEntry]]:
    """(source, entry) with plain ids lifted to entries, in a fixed order."""
    return [
        (source, LinkEntry(to=item) if isinstance(item, str) else item)
        for source in sorted(cfg.links)
        for item in cfg.links[source]
    ]


def _pairs(cfg: LinksConfig) -> list[tuple[str, str, str | None]]:
    """Every directed (source, target, note) the config declares, reverses of
    symmetric entries included."""
    out: list[tuple[str, str, str | None]] = []
    for source, entry in _entries(cfg):
        out.append((source, entry.to, entry.note))
        if entry.symmetric:
            out.append((entry.to, source, entry.note))
    return out


def links_problems(cfg: LinksConfig) -> list[str]:
    """Shape only ([] = ok). Whether an id *resolves* is not a shape question:
    it may live in a system that has not synced yet (§3.1)."""
    problems: list[str] = []
    for source in sorted(cfg.links):
        if not is_qualified(source):
            problems.append(
                f"links key {source!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
    for source, entry in _entries(cfg):
        if not is_qualified(entry.to):
            problems.append(
                f"link {entry.to!r} under {source!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
        if entry.to == source:
            problems.append(f"link under {source!r} points at itself")
        if entry.note is not None and (
            not entry.note.strip() or len(entry.note.strip().splitlines()) != 1
        ):
            problems.append(
                f"note on {source!r} -> {entry.to!r} must be one non-blank line"
            )
    seen: set[tuple[str, str]] = set()
    for source, target, _ in _pairs(cfg):
        if (source, target) in seen:
            problems.append(f"{source!r} -> {target!r} is declared more than once")
        seen.add((source, target))
    return problems


def expand(cfg: LinksConfig) -> dict[str, dict[str, str | None]]:
    """source doc_id -> {target doc_id: note}. On a duplicate the first
    declaration wins, but `links_problems` rejects duplicates before a run."""
    out: dict[str, dict[str, str | None]] = {}
    for source, target, note in _pairs(cfg):
        note = note.strip() if note else None
        out.setdefault(source, {}).setdefault(target, note)
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_links.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/links.py tests/test_links.py
git commit -m "feat: links.yaml config and validation (#41)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Link resolution, the `_links/` sidecar, and drift

**Files:**
- Modify: `src/kbforge/links.py` (append)
- Modify: `src/kbforge/chunking.py`, in `owned_paths` and its imports
- Test: `tests/test_links.py` (append), `tests/test_chunking.py` (update the two tests that enumerate `owned_paths`)

**Interfaces:**
- Consumes: `grounding.write_atomic(path, payload)`, `mirror.slot_key(doc_id)`, `synthesize.concept_path(doc_id)`, `models.CanonicalDocument`
- Produces:
  - `links.LINKS_DIR = "_links"`
  - `links.LinkResolution`, a frozen dataclass:
    - `links: list[tuple[str, str | None]]`
    - `managed: list[tuple[str, str | None]]`
    - `declares_managed: bool`
    - `notes: list[str]`
  - `links.resolve_links(doc: CanonicalDocument, expanded: dict[str, dict[str, str | None]], by_id: dict[str, CanonicalDocument]) -> LinkResolution`
  - `links.read_links(mirror: Path, doc_id: str) -> list[tuple[str, str | None]]`
  - `links.write_links(mirror: Path, doc_id: str, managed: list[tuple[str, str | None]]) -> None`
  - `links.delete_links(mirror: Path, doc_id: str) -> None`
  - `links.has_links_sidecars(mirror: Path) -> bool`
  - `links.links_drifted(mirror: Path, candidates: list[CanonicalDocument], current: dict[str, list[tuple[str, str | None]]]) -> list[str]`
  - `chunking.owned_paths(doc_id)` now returns five paths. The fifth is `f"{LINKS_DIR}/{name}"`.

"Managed" means a resolved link that does **not** come from a same-system connector relation: an editorial link (including a note on a relation) or a cross-system one. Same-system relations keep today's upkeep through referrers and arrivals, so a deployment without `--links` or cross-system relations writes no sidecar.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_links.py`; move the imports below into the file's top import block, since ruff rejects mid-file imports with E402)

```python
from datetime import UTC, datetime

from kbforge.links import (
    LINKS_DIR,
    delete_links,
    has_links_sidecars,
    links_drifted,
    read_links,
    resolve_links,
    write_links,
)
from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument, ResourceAnchor

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _doc(doc_id: str, relations: tuple[str, ...] = ()) -> CanonicalDocument:
    system, _, native = doc_id.partition(":")
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=system, native_id=native, retrieved_at=NOW, content_hash="h"
        ),
        doc_id=doc_id,
        title=native,
        text=native,
        relations=list(relations),
    )


def _by_id(*docs: CanonicalDocument) -> dict[str, CanonicalDocument]:
    return {d.doc_id: d for d in docs}


def test_relations_and_editorial_links_resolve_by_doc_id():
    x = _doc("a:x", ("a:y",))
    res = resolve_links(
        x, {"a:x": {"b:z": "why"}}, _by_id(x, _doc("a:y"), _doc("b:z"))
    )
    assert res.links == [("a:y", None), ("b:z", "why")]
    assert res.managed == [("b:z", "why")]  # a same-system relation is not
    assert res.declares_managed
    assert res.notes == []


def test_a_cross_system_relation_is_managed():
    x = _doc("a:x", ("b:z",))
    res = resolve_links(x, {}, _by_id(x, _doc("b:z")))
    assert res.managed == [("b:z", None)]


def test_a_note_on_a_same_system_relation_makes_it_managed():
    x = _doc("a:x", ("a:y",))
    res = resolve_links(x, {"a:x": {"a:y": "why"}}, _by_id(x, _doc("a:y")))
    assert res.links == res.managed == [("a:y", "why")]


def test_a_missing_editorial_target_is_dropped_with_a_note():
    x = _doc("a:x")
    res = resolve_links(x, {"a:x": {"b:z": None}}, _by_id(x))
    assert res.links == res.managed == []
    assert res.declares_managed  # so the pipeline writes an EMPTY sidecar
    assert res.notes == [
        "concepts/x/overview.md: link to b:z (links.yaml) was not found in the "
        "mirror or this fetch and was dropped"
    ]


def test_a_missing_relation_is_dropped_silently():
    x = _doc("a:x", ("a:ghost",))
    res = resolve_links(x, {}, _by_id(x))
    assert (res.links, res.notes, res.declares_managed) == ([], [], False)


def test_a_self_link_is_ignored():
    x = _doc("a:x", ("a:x",))
    assert resolve_links(x, {"a:x": {"a:x": None}}, _by_id(x)).links == []


def test_a_tombstoned_target_is_dropped():
    x = _doc("a:x", ("b:z",))
    gone = _doc("b:z").model_copy(update={"deleted": True})
    assert resolve_links(x, {}, _by_id(x, gone)).links == []


def test_the_sidecar_round_trips(tmp_path: Path):
    managed = [("b:w", None), ("b:z", "why")]
    assert not has_links_sidecars(tmp_path)
    write_links(tmp_path, "a:x", managed)
    assert has_links_sidecars(tmp_path)
    assert read_links(tmp_path, "a:x") == managed
    delete_links(tmp_path, "a:x")
    assert read_links(tmp_path, "a:x") == []
    delete_links(tmp_path, "a:x")  # idempotent


def test_an_empty_sidecar_still_counts_for_the_gate(tmp_path: Path):
    write_links(tmp_path, "a:x", [])
    assert has_links_sidecars(tmp_path)


def test_an_unreadable_sidecar_reads_as_empty(tmp_path: Path):
    path = tmp_path / LINKS_DIR / f"{slot_key('a:x')}.json"
    path.parent.mkdir()
    path.write_text("{torn", "utf-8")
    assert read_links(tmp_path, "a:x") == []


def test_drift_compares_targets_and_notes(tmp_path: Path):
    write_links(tmp_path, "a:x", [("b:z", None)])
    write_links(tmp_path, "a:n", [("b:z", "old")])
    docs = [_doc(i) for i in ("a:x", "a:n", "a:y", "a:w")]
    current = {
        "a:x": [("b:z", None)],  # unchanged
        "a:n": [("b:z", "new")],  # only the note moved
        "a:y": [],  # no sidecar, nothing managed: settled
        "a:w": [("b:v", None)],  # no sidecar reads as empty, not exempt
    }
    assert links_drifted(tmp_path, docs, current) == ["a:n", "a:w"]
```

Update `tests/test_chunking.py`:
- In `test_owned_paths_are_where_the_three_writers_actually_write`, add `write_links(mirror, doc.doc_id, [])` next to `write_sidecar`, importing `from kbforge.links import write_links`.
- In `test_snapshot_keeps_present_files_verbatim_and_absent_ones_as_none`, unpack five names (`slot, sidecar, first_seen, described, links`) and add `links: None` to the expected dict.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_links.py tests/test_chunking.py -q`
Expected: an `ImportError` on `LINKS_DIR`/`resolve_links`, and the chunking tests fail on unpacking.

- [ ] **Step 3: Implement** (append to `src/kbforge/links.py`, and merge the imports into the file's import block)

```python
import json
from dataclasses import dataclass

from kbforge.grounding import write_atomic
from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument
from kbforge.synthesize import concept_path

LINKS_DIR = "_links"
"""A subdirectory: `load_all` globs `mirror/*.json`."""


def _system(doc_id: str) -> str:
    return doc_id.partition(":")[0]


@dataclass(frozen=True)
class LinkResolution:
    links: list[tuple[str, str | None]]
    """Resolved (target doc_id, note), sorted by doc_id: every link it ships."""
    managed: list[tuple[str, str | None]]
    """The subset the `_links/` sidecar records. A same-system connector
    relation is left out: referrers and arrivals already keep it current."""
    declares_managed: bool
    """Anything managed was declared, resolved or not. The pipeline then
    writes a sidecar even when it is empty, or a concept whose only link was
    unresolvable at publish would never be rescanned (grounding's rule)."""
    notes: list[str]


def resolve_links(
    doc: CanonicalDocument,
    expanded: dict[str, dict[str, str | None]],
    by_id: dict[str, CanonicalDocument],
) -> LinkResolution:
    """`doc`'s connector relations plus its editorial links, resolved by doc_id
    against `by_id` (the whole mirror overlaid with this run, tombstones out).

    By doc_id, never by path: `bundle-path-collision` guarantees one doc_id per
    bundle path, which is what lets this look across systems (spec §4)."""
    editorial = expanded.get(doc.doc_id, {})
    declared: dict[str, str | None] = dict.fromkeys(doc.relations)
    declared.update(editorial)
    path = concept_path(doc.doc_id)
    own = _system(doc.doc_id)
    links: list[tuple[str, str | None]] = []
    managed: list[tuple[str, str | None]] = []
    notes: list[str] = []
    declares = False
    for target in sorted(declared):
        if target == doc.doc_id:
            continue
        is_managed = target in editorial or _system(target) != own
        declares = declares or is_managed
        found = by_id.get(target)
        if found is None or found.deleted:
            if target in editorial:
                notes.append(
                    f"{path}: link to {target} (links.yaml) was not found in the "
                    "mirror or this fetch and was dropped"
                )
            continue
        entry = (target, declared[target])
        links.append(entry)
        if is_managed:
            managed.append(entry)
    return LinkResolution(links, managed, declares, notes)


def _sidecar(mirror: Path, doc_id: str) -> Path:
    return mirror / LINKS_DIR / f"{slot_key(doc_id)}.json"


def read_links(mirror: Path, doc_id: str) -> list[tuple[str, str | None]]:
    """What the concept's managed links were at its last publish. A missing or
    unreadable sidecar is empty, not exempt: an empty record against a current
    link is drift, which is exactly the repair."""
    try:
        payload = json.loads(_sidecar(mirror, doc_id).read_text("utf-8"))
        return [
            (str(target), None if note is None else str(note))
            for target, note in payload["links"]
        ]
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return []


def write_links(
    mirror: Path, doc_id: str, managed: list[tuple[str, str | None]]
) -> None:
    write_atomic(
        _sidecar(mirror, doc_id),
        {"doc_id": doc_id, "links": [[t, n] for t, n in managed]},
    )


def delete_links(mirror: Path, doc_id: str) -> None:
    """Idempotent. A stale sidecar would drift its concept on every run."""
    _sidecar(mirror, doc_id).unlink(missing_ok=True)


def has_links_sidecars(mirror: Path) -> bool:
    """Cheap gate for the link-drift scan: a directory listing, not a load."""
    directory = mirror / LINKS_DIR
    return directory.is_dir() and any(directory.glob("*.json"))


def links_drifted(
    mirror: Path,
    candidates: list[CanonicalDocument],
    current: dict[str, list[tuple[str, str | None]]],
) -> list[str]:
    """Candidates whose managed links (targets and notes) differ from what their
    sidecar recorded. Titles are not compared: a retitled target does not
    rebuild its referrers (spec §5)."""
    return sorted(
        d.doc_id
        for d in candidates
        if read_links(mirror, d.doc_id) != current.get(d.doc_id, [])
    )
```

`ValueError` covers `json.JSONDecodeError` and a wrong-length unpack.

In `src/kbforge/chunking.py`, import `from kbforge.links import LINKS_DIR`. Add `f"{LINKS_DIR}/{name}"` as the last entry of `owned_paths`, and append "its links sidecar" to the docstring.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_links.py tests/test_chunking.py -q`
Expected: all pass. If the import between `links` → `grounding` → `synthesize` is circular, it shows up here; none of those modules imports `links` or `chunking`.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/links.py src/kbforge/chunking.py tests/test_links.py tests/test_chunking.py
git commit -m "feat: link resolution, the _links sidecar and drift (#41)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 6: Mutation-check.** Each mutation is in place in `src/kbforge/links.py`, run with `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_links.py -q`, then restored with `git checkout -- src/kbforge/links.py`:
- Change `is_managed = target in editorial or _system(target) != own` to `is_managed = target in editorial`. Expected: `test_a_cross_system_relation_is_managed` fails.
- Replace `declares = declares or is_managed` with `pass`. Expected: `test_a_missing_editorial_target_is_dropped_with_a_note` fails on `res.declares_managed`.

Record the results.

---

### Task 4: The pipeline resolves links, renders `## Related`, and records sidecars

**Files:**
- Modify: `src/kbforge/pipeline.py`:
  - imports
  - the `run` signature
  - `_scope_failures`
  - the block that builds `existing`
  - the synthesis call
  - the notes loops
  - the post-publish per-document loop
  - the tombstone loop
- Modify: `tests/test_pipeline.py`. Replace `test_a_cross_system_relation_aborts_instead_of_vanishing`, and reword the seeding comment in `test_another_systems_referrer_is_never_pulled_into_scope`.
- Create: `tests/test_pipeline_links.py`

**Interfaces:**
- Consumes:
  - `links.LinksConfig`
  - `links.expand`
  - `links.resolve_links`
  - `links.LinkResolution`
  - `links.write_links`
  - `links.delete_links`
  - `related.with_related`
- Produces: `pipeline.run(..., links_config: LinksConfig | None = None)`. `_scope_failures(by_id)` no longer takes `changed_docs`.

This task does **not** add the drift scan or change the no-op gate; that's Task 5. Editorial links therefore take effect here only on concepts that are already being rebuilt.

- [ ] **Step 1: Write the failing tests**

`tests/test_pipeline_links.py`:

```python
"""Editorial links and the `## Related` section in the pipeline (#41,
architecture.md §7.4). Helpers are local: tests/ is not a package."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.canonical import content_hash
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.links import LINKS_DIR, LinksConfig, read_links
from kbforge.mirror import slot_key
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import Published, run
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge.related import MARKER
from kbforge.synthesize import assemble, concept_path

X, Y, Z = (concept_path(f"a:{n}") for n in "xyz")


def _doc(
    native_id: str,
    *,
    system: str = "a",
    title: str | None = None,
    text: str | None = None,
    relations: list[str] | None = None,
    deleted: bool = False,
) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"{system}:{native_id}",
        title=title or native_id,
        text=text or native_id,
        relations=relations or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _Connector:
    def __init__(self, docs, name):
        self.docs, self.name = docs, name

    def kbforge_connector_info(self):
        return ConnectorInfo(name=self.name, version="0.1.0", source_system="t")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        return FetchResult(records=[], cursor=Cursor(connector=self.name))

    def kbforge_normalize(self, records):
        return self.docs


class _Publisher:
    def __init__(self):
        self.changes: list[ProposedChange] = []

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="rec", version="0.1.0", source_system="t")

    def kbforge_publish(self, change, config):
        self.changes.append(change)
        return f"recorded://{len(self.changes)}"

    def kbforge_open_request(self, branch_hint, config):
        return None  # every earlier request counts as merged


def _run(root: Path, docs, *, name="a", links=None, synthesizer=None, **kw):
    publisher = _Publisher()
    result = run(
        _Connector(docs, name),
        publisher,
        config={},
        mirror=str(root / "mirror"),
        state_dir=str(root / "state"),
        publish_config={},
        synthesizer=synthesizer,
        links_config=links,
        **kw,
    )
    return result, (publisher.changes[-1] if publisher.changes else None)


def _links(raw: dict) -> LinksConfig:
    return LinksConfig.model_validate({"links": raw})


def _sidecar(root: Path, doc_id: str) -> Path:
    return root / "mirror" / LINKS_DIR / f"{slot_key(doc_id)}.json"


def _section(content: str) -> str:
    return content[content.rindex(MARKER) :]


def test_a_one_way_link_renders_on_the_source_only(tmp_path):
    links = _links({"a:x": [{"to": "a:y", "note": "the requirement that closes it"}]})
    result, change = _run(
        tmp_path, [_doc("x"), _doc("y", title="Fail-mode requirement")], links=links
    )
    assert isinstance(result, Published)
    assert change.concepts[X].links == [Y]
    assert change.concepts[Y].links == []
    assert (
        f"- [Fail-mode requirement](/{Y}) — the requirement that closes it"
        in change.files[X]
    )
    assert MARKER not in change.files[Y]


def test_a_symmetric_link_lands_on_the_targets_own_run(tmp_path):
    links = _links({"a:x": [{"to": "b:y", "note": "same variant", "symmetric": True}]})
    _, first = _run(tmp_path, [_doc("x")], links=links)  # b has not synced yet
    assert first.concepts[X].links == []
    assert (
        f"{X}: link to b:y (links.yaml) was not found in the mirror or this "
        "fetch and was dropped"
    ) in first.summary.grounding_notes

    result, second = _run(tmp_path, [_doc("y", system="b")], name="b", links=links)
    assert isinstance(result, Published)
    assert set(second.files) == {Y}  # B's run never touches A's concept
    assert second.concepts[Y].links == [X]
    assert f"- [x](/{X}) — same variant" in second.files[Y]
    assert (
        f"{Y}: links to a:x (system a); merge that system's review request "
        "first, or the link dangles until it does"
    ) in second.summary.grounding_notes


def test_a_cross_system_relation_now_links_instead_of_aborting(tmp_path):
    _run(tmp_path, [_doc("y", system="b")], name="b")
    result, change = _run(tmp_path, [_doc("x", relations=["b:y"])])
    assert isinstance(result, Published)
    assert change.concepts[X].links == [Y]
    assert any("links to b:y (system b)" in n for n in change.summary.grounding_notes)


class _Prose:
    """A third-party synthesizer writing its own body."""

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        items = [(d, d.title, d.title, "Rewritten.") for d in changed_docs]
        return assemble(items, changeset, existing_paths)


def test_every_synthesizer_gets_the_same_section(tmp_path):
    docs = [_doc("x", relations=["a:y"]), _doc("y", title="Why")]
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _, stub = _run(tmp_path / "stub", docs, links=links)
    _, prose = _run(tmp_path / "prose", docs, links=links, synthesizer=_Prose())
    assert "Rewritten." in prose.files[X]
    assert _section(stub.files[X]) == _section(prose.files[X])


def test_the_describe_synthesizer_gets_the_same_section(tmp_path):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kbforge.llm_synthesizer import DescribeConfig, DescribeSynthesizer

    def fn(messages, info: AgentInfo):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"description": "One sentence.", "tags": []},
                )
            ]
        )

    docs = [_doc("x", relations=["a:y"]), _doc("y")]
    _, stub = _run(tmp_path / "stub", docs)
    config = DescribeConfig()
    agent = DescribeSynthesizer._build_agent(config, model=FunctionModel(fn))
    describer = DescribeSynthesizer(
        config, mirror=tmp_path / "desc" / "mirror", agent=agent
    )
    _, desc = _run(tmp_path / "desc", docs, synthesizer=describer)
    assert _section(stub.files[X]) == _section(desc.files[X])


def test_a_local_files_relation_gets_a_section(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text("---\ntitle: B\n---\nB.\n", "utf-8")
    (src / "a.md").write_text("---\ntitle: A\nrelations:\n  - b.md\n---\nA.\n", "utf-8")
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )
    assert isinstance(result, Published)
    text = (Path(result.url) / "concepts/a/overview.md").read_text("utf-8")
    assert "- [B](/concepts/b/overview.md)" in text


def test_links_never_reach_the_mirror(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _, change = _run(tmp_path / "with", docs, links=_links({"a:x": ["a:y"]}))
    assert change.concepts[X].links == [Y]  # the links did apply
    _run(tmp_path / "without", docs)

    def slots(root: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in (root / "mirror").glob("*.json")}

    assert slots(tmp_path / "with") == slots(tmp_path / "without")


def test_a_managed_link_is_recorded_after_publish(tmp_path):
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _run(tmp_path, [_doc("x"), _doc("y")], links=links)
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", "n")]
    assert not _sidecar(tmp_path, "a:y").exists()  # nothing managed on y


def test_a_declared_but_unresolved_link_records_an_empty_sidecar(tmp_path):
    _run(tmp_path, [_doc("x")], links=_links({"a:x": ["b:y"]}))
    assert _sidecar(tmp_path, "a:x").exists()
    assert read_links(tmp_path / "mirror", "a:x") == []


def test_a_same_system_relation_writes_no_sidecar(tmp_path):
    _run(tmp_path, [_doc("x", relations=["a:y"]), _doc("y")])
    assert not (tmp_path / "mirror" / LINKS_DIR).exists()


def test_a_tombstone_deletes_its_sidecar(tmp_path):
    links = _links({"a:x": ["a:y"]})
    _run(tmp_path, [_doc("x"), _doc("y")], links=links)
    _run(tmp_path, [_doc("x", deleted=True), _doc("y")], links=links)
    assert not _sidecar(tmp_path, "a:x").exists()


class _DropX:
    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        items = [(d, d.title, d.title, d.text) for d in changed_docs if d.doc_id != "a:x"]
        return assemble(items, changeset, existing_paths)


def test_a_document_the_synthesizer_dropped_keeps_its_sidecar(tmp_path):
    _run(tmp_path, [_doc("x"), _doc("y")], links=_links({"a:x": [{"to": "a:y", "note": "n"}]}))
    _run(
        tmp_path,
        [_doc("x", text="x2"), _doc("y", text="y2")],
        links=_links({"a:x": ["a:y"]}),
        synthesizer=_DropX(),
    )
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", "n")]
```

In `tests/test_pipeline.py`:
- Delete `test_a_cross_system_relation_aborts_instead_of_vanishing`. Its replacement is `test_a_cross_system_relation_now_links_instead_of_aborting` above.
- In `test_another_systems_referrer_is_never_pulled_into_scope`, replace the comment "Seeded straight into the mirror: a cross-system relation is now rejected at the run boundary, so the only way one exists is a mirror written before that rule -- …" with: `# Seeded straight into the mirror: another system's document naming this system's doc_id, which is exactly what this scope has to survive.` Leave the test body as it is.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pipeline_links.py -q`
Expected: `TypeError: run() got an unexpected keyword argument 'links_config'`.

- [ ] **Step 3: Implement in `src/kbforge/pipeline.py`**

Imports:

```python
from kbforge.links import (
    LinkResolution,
    LinksConfig,
    delete_links,
    expand,
    resolve_links,
    write_links,
)
from kbforge.related import with_related
```

`run` signature: add `links_config: LinksConfig | None = None` after `chunking`.

Directly after `grounding_cfg = grounding_config or GroundingConfig()`:

```python
    # Expanded once: symmetric reverses are what let B's run find A's entry.
    expanded = expand(links_config) if links_config is not None else {}
```

Directly after the `_resolved` memo definition, add a second memo:

```python
    # Links, memoised for the same reason as grounding: the drift scan and the
    # synthesis copy below resolve the same document from the same `by_id`.
    _link_resolutions: dict[str, LinkResolution] = {}

    def _links_of(doc: CanonicalDocument) -> LinkResolution:
        cached = _link_resolutions.get(doc.doc_id)
        if cached is None:
            cached = resolve_links(doc, expanded, by_id)
            _link_resolutions[doc.doc_id] = cached
        return cached
```

Replace `_scope_failures` with the collision half only. Its signature becomes `_scope_failures(by_id: dict[str, CanonicalDocument]) -> list[Failure]`. Delete the `for doc in changed_docs:` cross-system loop. Rewrite the docstring:

```python
    """A path collision on the shared mirror, reported rather than published
    into.

    `concept_path` drops the system prefix, so `wiki:readme` and `notes:readme`
    render one file on two sync branches; whichever merges second overwrites the
    other with no validator, no conflict, and no note. This check is also what
    makes resolving links by doc_id unambiguous across systems (#41): one doc_id
    per bundle path. System-qualified bundle paths would remove the collision at
    its root, but that rewrites every published path, so it is its own release
    (#42)."""
```

Update the call site to `scope_failures = _scope_failures(by_id)`.

After the dedupe block and before the `existing` comment, resolve links for everything being synthesized:

```python
    # Every concept this run renders gets its links resolved by doc_id over the
    # whole mirror (§7.4): connector relations and editorial links alike, across
    # systems. The synthesizer receives a COPY whose `relations` are the
    # resolved targets, never the original: `commit()` below writes `docs`, and
    # config-dependent content must not reach the mirror (§7.1's rule).
    link_notes: list[str] = []
    link_targets: set[str] = set()
    synth_docs: list[CanonicalDocument] = []
    for doc in changed_docs:
        res = _links_of(doc)
        link_notes += res.notes
        link_targets |= {target for target, _ in res.links}
        synth_docs.append(
            doc.model_copy(update={"relations": [t for t, _ in res.links]})
        )
```

Change `existing` so resolved targets count as present. They were resolved by doc_id, so the path-collision rescue that the scoping guards against cannot happen through them:

```python
    existing = (
        frozenset(
            {concept_path(d.doc_id) for d in mirror_docs if d.anchor.system in systems}
            | {concept_path(d.doc_id) for d in admitted_docs if not d.deleted}
            | {concept_path(t) for t in link_targets}
        )
        - tombstoned
    )
```

Add one sentence to the end of the long `existing` comment: `Resolved link targets are unioned in on top: they were resolved by doc_id, not by path, so they cannot be rescued by another system's document.`

Pass `synth_docs` instead of `changed_docs` to **both** synthesize calls, the grounding one and the plain one. Grounding still resolves from `changed_docs` (`_resolved(doc)`), which is keyed by doc_id, so it's unaffected.

After `proposal.summary.grounding_notes.extend(grounding_notes)`, add:

```python
    proposal.summary.grounding_notes.extend(link_notes)
```

Delete the Task 1 `with_related(...)` call just before `run_validators`. Its replacement goes after the arrivals note loop, before `pending = ...`, together with the cross-system disclosure. Both are still before validation:

```python
    # The merge-order window (§7.4): the mirror advances on publish, not merge,
    # so a cross-system link dangles on `main` until its target's request
    # merges. kbforge never merges; it tells the reviewer instead.
    for doc in changed_docs:
        path = concept_path(doc.doc_id)
        concept = proposal.concepts.get(path)
        if path not in proposal.files or concept is None:
            continue
        own = doc.doc_id.partition(":")[0]
        for target, _ in _links_of(doc).links:
            other = target.partition(":")[0]
            if other != own and concept_path(target) in concept.links:
                proposal.summary.grounding_notes.append(
                    f"{path}: links to {target} (system {other}); merge that "
                    "system's review request first, or the link dangles until "
                    "it does"
                )

    # Frame, not prose: rendered here so every synthesizer gets the same
    # section and none can forge it. Bound to the projection by
    # `validate._check_related_section` (architecture.md §7.4).
    with_related(
        proposal,
        {concept_path(i): d.title for i, d in by_id.items()},
        {
            concept_path(d.doc_id): {
                concept_path(t): note for t, note in _links_of(d).links if note
            }
            for d in changed_docs
        },
    )
```

In the post-publish per-document loop, after the `_described` write/delete block and before `docs_for = ...`, add:

```python
        resolution = _links_of(doc)
        if resolution.declares_managed:
            # Written even when empty, for the grounding sidecar's reason: a
            # declared link unresolvable today must still be rescanned when its
            # target arrives, and the sidecar is what trips the scan.
            write_links(mirror_path, doc.doc_id, resolution.managed)
        else:
            delete_links(mirror_path, doc.doc_id)
```

In the tombstone loop, add `delete_links(mirror_path, doc_id)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pipeline_links.py tests/test_pipeline.py -q`
Expected: all pass.

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: all pass, including the pipeline tests Task 1 listed as expected failures. If a test asserts a published file's exact bytes and a linked concept now gains the section, update only that expectation and name it in the report. Any other failure: stop and report.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline_links.py tests/test_pipeline.py
git commit -m "feat: the pipeline resolves links by doc_id and renders ## Related (#41)

Lifts the cross-system-relation abort: links resolve by doc_id, and the
bundle-path-collision check is what keeps that unambiguous.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Mutation-check.** For each mutation below, change `src/kbforge/pipeline.py` in place, run `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_pipeline_links.py -q`, then restore with `git checkout -- src/kbforge/pipeline.py`:
- Delete the `| {concept_path(t) for t in link_targets}` line. Expected: `test_a_cross_system_relation_now_links_instead_of_aborting` and `test_a_symmetric_link_lands_on_the_targets_own_run` fail on their `links ==` assertions, because `assemble` drops the target as unknown.
- Pass `changed_docs` instead of `synth_docs` to the plain synthesize call. Expected: `test_a_one_way_link_renders_on_the_source_only` fails.
- Replace `write_links(mirror_path, doc.doc_id, resolution.managed)` with `delete_links(mirror_path, doc.doc_id)`. Expected: both sidecar-recording tests fail.
- Delete the `with_related(...)` call. Expected: the run returns `Aborted` with `no '## Related' section`, so several tests fail.

Record the results.

---

### Task 5: Link drift, the no-op clause, and chunking

**Files:**
- Modify: `src/kbforge/pipeline.py`:
  - imports
  - the first no-op gate
  - after the grounding drift block
  - the second no-op gate
  - `rebuilt_deferred`
  - the notes
  - `pending`/`carried`
  - `touched`
- Test: `tests/test_pipeline_links.py` (append)

**Interfaces:**
- Consumes:
  - `links.has_links_sidecars`
  - `links.links_drifted`
  - `_links_of` (Task 4)
  - `_drift_candidates`
  - `chunking.admit`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_pipeline_links.py`; merge the imports below into the file's top import block, since ruff rejects mid-file imports with E402)

```python
from kbforge import pipeline
from kbforge.chunking import ChunkingConfig
from kbforge.pipeline import NoOp, redo


def _chunked(root, docs, cap, **kw):
    return _run(root, docs, chunking=ChunkingConfig(max_concepts=cap), **kw)


def test_the_other_side_of_a_symmetric_link_is_picked_up_on_its_own_run(tmp_path):
    links = _links({"a:x": [{"to": "b:y", "note": "n", "symmetric": True}]})
    _run(tmp_path, [_doc("x")], links=links)
    _run(tmp_path, [_doc("y", system="b")], name="b", links=links)

    result, change = _run(tmp_path, [_doc("x")], links=links)
    assert isinstance(result, Published)
    assert set(change.files) == {X}
    assert change.concepts[X].links == [Y]
    assert (
        f"{X}: re-synthesized because its links changed since it was last "
        "published; its own source is unchanged"
    ) in change.summary.grounding_notes

    assert isinstance(_run(tmp_path, [_doc("x")], links=links)[0], NoOp)
    assert isinstance(
        _run(tmp_path, [_doc("y", system="b")], name="b", links=links)[0], NoOp
    )


def test_editing_only_a_note_rebuilds_only_that_concept(tmp_path):
    docs = [_doc("x"), _doc("y"), _doc("z")]
    before = {"a:x": [{"to": "a:y", "note": "old"}], "a:z": ["a:y"]}
    after = {"a:x": [{"to": "a:y", "note": "new"}], "a:z": ["a:y"]}
    _run(tmp_path, docs, links=_links(before))
    _, change = _run(tmp_path, docs, links=_links(after))
    assert set(change.files) == {X}
    assert change.files[X].rstrip().endswith("— new")


def test_a_new_entry_rebuilds_its_source(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _run(tmp_path, docs)
    _, change = _run(tmp_path, docs, links=_links({"a:x": ["a:y"]}))
    assert set(change.files) == {X}
    assert change.concepts[X].links == [Y]


def test_a_target_tombstoned_in_another_system_drops_the_link_next_run(tmp_path):
    links = _links({"a:x": ["b:y"]})
    _run(tmp_path, [_doc("y", system="b")], name="b", links=links)
    _, linked = _run(tmp_path, [_doc("x")], links=links)
    assert linked.concepts[X].links == [Y]

    _run(tmp_path, [_doc("y", system="b", deleted=True)], name="b", links=links)
    result, change = _run(tmp_path, [_doc("x")], links=links)
    assert isinstance(result, Published)
    assert change.concepts[X].links == []
    assert MARKER not in change.files[X]


def test_an_unchanged_world_with_links_is_a_noop(tmp_path):
    docs = [_doc("x"), _doc("y")]
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _run(tmp_path, docs, links=links)
    assert isinstance(_run(tmp_path, docs, links=links)[0], NoOp)


def test_retitling_a_target_does_not_rebuild_its_referrer(tmp_path):
    links = _links({"a:x": ["b:y"]})
    _run(tmp_path, [_doc("y", system="b", title="Old")], name="b", links=links)
    _run(tmp_path, [_doc("x")], links=links)
    _run(tmp_path, [_doc("y", system="b", title="New")], name="b", links=links)
    assert isinstance(_run(tmp_path, [_doc("x")], links=links)[0], NoOp)


def test_dropping_links_yaml_removes_editorial_links_once(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _run(tmp_path, docs, links=_links({"a:x": ["a:y"]}))
    result, change = _run(tmp_path, docs)  # no --links: the sidecar trips the scan
    assert isinstance(result, Published)
    assert change.concepts[X].links == []
    assert not _sidecar(tmp_path, "a:x").exists()
    assert isinstance(_run(tmp_path, docs)[0], NoOp)


def test_without_links_or_sidecars_the_mirror_is_never_loaded(tmp_path, monkeypatch):
    docs = [_doc("x", relations=["a:y"]), _doc("y")]
    _run(tmp_path, docs)

    def _never(mirror):
        raise AssertionError("load_all ran: the link-drift gate is not holding")

    monkeypatch.setattr(pipeline, "load_all", _never)
    assert isinstance(_run(tmp_path, docs)[0], NoOp)


def test_link_drift_counts_toward_the_cap_and_waits_its_turn(tmp_path):
    docs = [_doc("x"), _doc("y"), _doc("z")]
    _run(tmp_path, docs)
    links = _links({"a:x": ["a:y"], "a:z": ["a:y"]})
    _, first = _chunked(tmp_path, docs, 1, links=links)
    assert set(first.files) == {X}
    assert (
        "chunked review: this request carries 1 of 2 changed concepts; the rest "
        "follow once it is merged or closed"
    ) in first.summary.grounding_notes
    _, second = _chunked(tmp_path, docs, 1, links=links)
    assert set(second.files) == {Z}


def test_redo_restores_the_links_sidecar(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _chunked(tmp_path, docs, 5)
    _chunked(tmp_path, docs, 5, links=_links({"a:x": ["a:y"]}))
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", None)]
    redo(
        _Connector([], "a"),
        _Publisher(),
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
    )
    assert not _sidecar(tmp_path, "a:x").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pipeline_links.py -q`
Expected: the new drift tests fail. For example, the symmetric pick-up run returns `NoOp`, and `test_dropping_links_yaml…` returns `NoOp` on its second run. The Task 4 tests still pass.

- [ ] **Step 3: Implement in `src/kbforge/pipeline.py`**

Add `has_links_sidecars` and `links_drifted` to the `kbforge.links` import.

Replace the first no-op gate with:

```python
    # Link drift (§7.4) is gated like grounding drift: `--links` given, or a
    # sidecar from before. Unlike grounding it runs under every synthesizer,
    # because links are frame, not prose.
    link_scan = links_config is not None or has_links_sidecars(mirror_path)
    if changeset.is_noop and not scan and not link_scan:
        return NoOp()
```

Hoist the candidate list so both scans share one computation. Replace the start of the `if scan:` block with:

```python
    # `changed | backlog`: a backlog document is rebuilt whole in its own
    # chunk, so rebuilding its stale mirror copy for drift now is waste.
    candidates = (
        _drift_candidates(mirror_docs, by_id, systems, changed | backlog, removed_ids)
        if scan or link_scan
        else []
    )
    drift: list[str] = []
    deferred_drift: set[str] = set()
    if scan:
        drift = drifted(
```

The rest of the `if scan:` block stays as it is, minus its own `candidates = ...` assignment and the comment that moved. Then add after it:

```python
    # A concept whose managed links (targets or notes) moved since its last
    # publish: a links.yaml edit, or a target added or tombstoned by another
    # system's run. Rebuilt on its own system's run, never the other's. Never
    # changes `relations`, the mirror or links.yaml, so it converges.
    link_drift: list[str] = []
    deferred_link_drift: set[str] = set()
    if link_scan:
        already = set(drift) | deferred_drift
        link_drift = [
            x
            for x in links_drifted(
                mirror_path,
                candidates,
                {d.doc_id: _links_of(d).managed for d in candidates},
            )
            if x not in already
        ]
        link_ids = set(link_drift)
        link_docs = [d for d in candidates if d.doc_id in link_ids]
        if chunking is not None:
            # After changed documents and grounding drift, into what remains.
            room = max(chunking.max_concepts - len(changed) - len(drift), 0)
            link_docs, _ = admit(link_docs, chunking, room)
            deferred_link_drift = link_ids - {d.doc_id for d in link_docs}
            link_drift = [x for x in link_drift if x not in deferred_link_drift]
        changed_docs += link_docs

    if changeset.is_noop and not drift and not link_drift:
        return NoOp()
```

`_links_of` is defined earlier in `run` than this block. If it isn't, move the memo definition above the drift blocks; it only needs `expanded` and `by_id`.

Extend `rebuilt_deferred`, leaving the grounding lines as they are:

```python
    rebuilt = {d.doc_id for d in referrers + arrivals}
    rebuilt_deferred = deferred_drift & rebuilt
    if rebuilt_deferred:
        drift += sorted(rebuilt_deferred)
        deferred_drift -= rebuilt_deferred
    rebuilt_link = deferred_link_drift & rebuilt
    if rebuilt_link:
        link_drift += sorted(rebuilt_link)
        deferred_link_drift -= rebuilt_link
```

After the grounding-drift note loop, add:

```python
    for doc_id in link_drift:
        path = concept_path(doc_id)
        if path in proposal.files:
            proposal.summary.grounding_notes.append(
                f"{path}: re-synthesized because its links changed since it was "
                "last published; its own source is unchanged"
            )
```

Update `pending`/`carried`:

```python
    pending = bool(backlog or deferred_drift or deferred_link_drift)
    if pending:
        carried = len(changed) + len(drift) + len(link_drift)
        waiting = len(backlog) + len(deferred_drift) + len(deferred_link_drift)
        proposal.summary.grounding_notes.append(
            f"chunked review: this request carries {carried} of "
            f"{carried + waiting} changed concepts; "
            "the rest follow once it is merged or closed"
        )
```

Add `| set(link_drift)` to `touched`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pipeline_links.py tests/test_pipeline.py tests/test_pipeline_chunking.py -q`
Expected: all pass.

- [ ] **Step 5: Run the whole suite and hooks**

Run: `uv run pytest -q && prek run --all-files`
Expected: all pass, and the hooks pass clean.

- [ ] **Step 6: Commit**

```bash
git add src/kbforge/pipeline.py tests/test_pipeline_links.py
git commit -m "feat: link drift keeps editorial and cross-system links current (#41)

The no-op rule gains one clause: no link drift.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: Mutation-check.** For each mutation below, change `src/kbforge/pipeline.py` in place, run `PYTHONDONTWRITEBYTECODE=1 uv run pytest tests/test_pipeline_links.py -q`, then restore with `git checkout -- src/kbforge/pipeline.py`:
- `link_scan = links_config is not None` (drop the sidecar clause). Expected: `test_dropping_links_yaml_removes_editorial_links_once` fails.
- `link_scan = True`. Expected: `test_without_links_or_sidecars_the_mirror_is_never_loaded` fails.
- Remove `and not link_drift` from the second no-op gate. Expected: the pick-up and new-entry tests fail.
- Remove `| set(link_drift)` from `touched`. Expected: `test_redo_restores_the_links_sidecar` fails.
- Delete the `link_docs, _ = admit(link_docs, chunking, room)` line. Expected: `test_link_drift_counts_toward_the_cap_and_waits_its_turn` fails.

Record the results.

---

### Task 6: `--links` on the CLI

**Files:**
- Modify: `src/kbforge/__main__.py`
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `links.load_links`, `links.links_problems`, and `run(..., links_config=)`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cli.py`)

```python
def _two_docs(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("---\ntitle: A\n---\nA.\n", "utf-8")
    (src / "b.md").write_text("---\ntitle: B\n---\nB.\n", "utf-8")
    return src


def test_links_flag_renders_a_related_section(tmp_path: Path, capsys):
    src = _two_docs(tmp_path)
    links = tmp_path / "links.yaml"
    links.write_text(
        "links:\n  local_files:a.md:\n    - to: local_files:b.md\n"
        "      note: why they relate\n",
        "utf-8",
    )
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0, capsys.readouterr().out
    page = tmp_path / "out" / "sync-local_files" / "concepts" / "a" / "overview.md"
    assert "- [B](/concepts/b/overview.md) — why they relate" in page.read_text(
        "utf-8"
    )


def test_a_malformed_links_file_exits_2_before_fetching(tmp_path: Path, capsys):
    src = _two_docs(tmp_path)
    links = tmp_path / "links.yaml"
    links.write_text("links:\n  a.md:\n    - local_files:b.md\n", "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert "links config: links key 'a.md' must be a qualified doc_id" in out
    assert not (tmp_path / "mirror").exists()


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("missing.yaml", None),
        ("bad.yaml", "links: [unclosed\n"),
        ("wrong.yaml", "link: {}\n"),
    ],
)
def test_an_unreadable_links_file_exits_2_with_a_message(
    tmp_path: Path, capsys, name: str, body: str | None
):
    src = _two_docs(tmp_path)
    links = tmp_path / name
    if body is not None:
        links.write_text(body, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--links",
            str(links),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert f"links config {links}" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -q -k links`
Expected: argparse exits 2 with `unrecognized arguments: --links`. The first test fails on `code == 0`, and the unreadable-file tests fail on the message.

- [ ] **Step 3: Implement in `src/kbforge/__main__.py`**

Import `from kbforge.links import links_problems, load_links`.

After the `--chunking` argument:

```python
    r.add_argument(
        "--links",
        default=None,
        metavar="PATH",
        help="editorial links (YAML); see docs/architecture.md §7.4",
    )
```

After the chunking config load and its error handling, before `run(...)`:

```python
    try:
        links_config = load_links(Path(args.links) if args.links else None)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        # Same four operator mistakes, same handling, as --grounding above.
        print(f"links config {args.links}: {exc}")
        return 2
    if links_config is not None:
        problems = links_problems(links_config)
        if problems:
            print(f"links config: {'; '.join(problems)}")
            return 2
```

Pass `links_config=links_config` to `run(...)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/__main__.py tests/test_cli.py
git commit -m "feat(cli): --links (#41)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Docs: fold the spec into architecture.md, CLAUDE.md, README, CHANGELOG

**Files:**
- Modify: `docs/architecture.md`:
  - add a new §7.4 after §7.3, before `---` / `## 8.`
  - §5.4, the paragraph starting "The shared mirror has one cost…" (~line 830)
  - §4.4 caveat "Law 2's confidence is contingent on normalization…" (~line 595)
- Modify: `CLAUDE.md`:
  - the no-op invariant
  - "One bundle path, one owner"
  - the dual-carrier section
- Modify: `README.md`, adding a paragraph after the "Large first runs" paragraph
- Modify: `CHANGELOG.md`, under `## [Unreleased]`
- Delete: `docs/design/2026-09-23-declarative-links-design.md` (CLAUDE.md, "Docs layout": fold a spec once it ships, keeping only the rationale)

No version bump here; that happens at release.

- [ ] **Step 1: architecture.md §7.4.** Add `### 7.4 Editorial links`. Keep the spec's rationale and the rules the code does not state, in this order. Use prose paragraphs, and match the density of §7.3:
  1. Why a pipeline flag and not connector config (`normalize` is pure). Why tag- and facet-implied relations are `okfquery related`'s job (OKF §3.1), not the producer's.
  2. The `links.yaml` shape: a plain id or `{to, note?, symmetric?}`, `extra="forbid"`, qualified ids only. A reference to an unsynced document is a review note, not an error.
  3. Resolution by doc_id over `by_id`. Why that is unambiguous: `bundle-path-collision`. The cross-system-relation abort is lifted for relations and `links.yaml` alike, under one rule. Links reach synthesis copies only, never the mirror.
  4. `## Related`: rendered by the pipeline after synthesis, bundle-absolute paths, the marker, last-marker-wins, and bound by `_check_related_section`. Titles can go stale by design. Existing bundles gain the section on their next render.
  5. Upkeep: "managed" links, the `_links/` sidecar, and why it is written even when empty (the deviation above). The drift gate. Retitles don't drift. Link drift runs under every synthesizer: `describe` reuses `_described/`, while `llm` re-synthesizes the body.
  6. The no-op clause and chunking (counts toward the cap, admitted after grounding drift, restored by `redo`).
  7. The merge-order window, and the review note that discloses it.
  8. **Deferred**: the spec's §11 list, minus `okfquery related`, which shipped.

- [ ] **Step 2: architecture.md §5.4.** In the "shared mirror has one cost" paragraph:
  - Delete "and likewise on a relation that crosses out of its own system, which `existing`'s scoping would otherwise drop silently under §4.4 law 2".
  - Change "Both are reported" to "It is reported".
  - Change "would remove the collision at its root and let cross-system links resolve" to "would remove the collision at its root".
  - Add: "Cross-system links resolve by `doc_id` (§7.4); this collision check is what keeps that unambiguous."

- [ ] **Step 3: architecture.md §4.4.** At the end of the "Law 2's confidence…" bullet, add: "Part of that has since landed: the `## Related` section (§7.4) is a body carrier of `links` that `_check_related_section` binds to the projection. Other body links in a concept's prose are still unchecked."

- [ ] **Step 4: CLAUDE.md.**
  - No-op bullet: after "grounding drift (§7.1)", add "and no link drift (§7.4)". After the "grounding added a second thing…" sentence, add "links.yaml and other systems' documents added a third".
  - "One bundle path, one owner": replace "so the pipeline aborts on a collision and on a cross-system relation rather than letting one system's concept overwrite another's on merge" with "so the pipeline aborts on a collision rather than letting one system's concept overwrite another's on merge. Cross-system links resolve by `doc_id` (architecture.md §7.4), which that abort keeps unambiguous; the merge-order window they open is disclosed in the review note, not solved by merging."
  - Dual-carrier section: add a third bullet under "Two mechanisms" and change "Two" to "Three": "`_check_related_section` binds the `## Related` body section to `links`. The pipeline renders it after synthesis, so a synthesizer can't forge it." Also add `related.render_section` to the "If you touch either side, re-read…" sentence.

- [ ] **Step 5: README.md.** After the "Large first runs" paragraph, add:

~~~markdown
**Links no source carries.** Two taxonomies with no join key, or a requirement
that closes a gap on another slide: declare them in a reviewed `links.yaml` and
pass `--links links.yaml`.

```yaml
links:
  planning_deck:gaps/redundant-supply:
    - to: db_apps:applications/777
      note: the application this gap is about
      symmetric: true
    - planning_deck:requirements/diagnostics   # one-way, no note
```

Links resolve by `doc_id`, across systems too. They render in a `## Related`
section at the end of each concept, which an OKF reader follows and which
holds the note that says why the two relate; connector `relations` get the
same section. Each system's run rebuilds only its own concepts, so a
cross-system link lands on each side on that side's run. The review note names
the other system, so its request can be merged first. For concepts that share
tags or facets, use `okfquery related` rather than declaring links. See
`docs/architecture.md` §7.4.
~~~

Also change the **Core** bullet's "cross-source grounding: …" sentence so it ends "…, and editorial links across systems (`--links`)."

- [ ] **Step 6: CHANGELOG.md.** Under `## [Unreleased]`:

```markdown
### Added

- `--links links.yaml` (#41): editorial links no source carries, as a plain
  `doc_id` or `{to, note, symmetric}`, resolved by `doc_id` across systems. A
  reference to a system that has not synced yet is a review note, not an error,
  and the link appears once its target does.
- Every concept with links gets a kbforge-owned `## Related` section at the end
  of its body (OKF §6.1), with the target's title and the link's note. It is
  bound to the `links` frontmatter by a new validator, so the two can't
  disagree. This covers connector `relations` too, so existing bundles gain the
  section as each linked concept is next rendered.

### Changed

- A relation into another system no longer aborts the run: it resolves by
  `doc_id` like an editorial link. The bundle-path-collision abort is unchanged.
- The no-op rule has one more clause: a run with no source change, no grounding
  drift and no link drift is still `NoOp`. Link drift (a `links.yaml` edit, or a
  target added or removed by another system's run) rebuilds only the concepts
  whose links moved, and only on their own system's run.
- `mirror/_links/` holds each concept's editorial and cross-system links as last
  published. `kbforge redo` restores it.
```

- [ ] **Step 7: Delete the spec and check for stale references**

```bash
git rm docs/design/2026-09-23-declarative-links-design.md
grep -rn "declarative-links-design\|cross-system-relation" --include=*.py --include=*.md . | grep -v "^./docs/superpowers/plans/"
```

Expected: no output. Fix any hit: a docstring pointing at the deleted spec should point at `architecture.md §7.4` instead.

- [ ] **Step 8: Verify and commit**

```bash
uv run pytest -q && prek run --all-files
git add -A docs CLAUDE.md README.md CHANGELOG.md
git commit -m "docs: editorial links in architecture §7.4; fold and remove the #41 spec

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
