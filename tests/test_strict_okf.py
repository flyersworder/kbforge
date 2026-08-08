from datetime import UTC, datetime

import pytest

from kbforge.models import ConceptFrontmatter, ProposedChange, ResourceAnchor
from kbforge.validate import run_validators

ANCHOR = ResourceAnchor(
    system="s",
    native_id="n",
    retrieved_at=datetime(2026, 7, 19, tzinfo=UTC),
    content_hash="h",
)

GOOD = """---
type: concept
title: X
description: X
generated: {by: kbforge/test, at: 2026-07-19T00:00:00+00:00}
---
# X
body
"""

MISSING_DESC = """---
type: concept
title: X
generated: {by: kbforge/test, at: 2026-07-19T00:00:00+00:00}
---
# X
"""


MISSING_GENERATED = """---
type: concept
title: X
description: X
sources:
- {id: 's:n', resource: 's:n'}
---
# X
"""

_GOOD_GENERATED = "generated: {by: kbforge/t, at: '2026-07-19T00:00:00+00:00'}"
_GOOD_SOURCES = "sources:\n- {id: 's:n', resource: 's:n'}"


def _concept(
    concept_type: str = "concept",
    links: list[str] | None = None,
) -> ConceptFrontmatter:
    return ConceptFrontmatter(
        type=concept_type,
        sources=[ANCHOR],
        links=links if links is not None else [],
        generated_at=datetime(2026, 7, 19, tzinfo=UTC),
        generated_by="kbforge/t",
    )


def _proposal(path, content, concept=None):
    return ProposedChange(
        branch_hint="b",
        files={path: content},
        concepts={path: concept} if concept else {},
    )


def _laws(content, concept=None) -> list[str]:
    """The exact failure slugs, so a test cannot pass on a *different* violation
    than the one it names — the way six of these did before."""
    return [f.law for f in run_validators(_proposal("c/x.md", content, concept))]


def test_rendered_file_missing_required_field_is_reported():
    failures = run_validators(_proposal("concepts/x/overview.md", MISSING_DESC))
    assert any(f.law == "okf-strict" for f in failures)


def test_rendered_file_without_generated_is_reported():
    """kbforge requires `generated` even though OKF §11 requires only `type`:
    producer-side strictness, so law 4 can never be satisfied by a projection
    whose rendered file omits the stamp. The fixture carries valid `sources` so
    this can only fail on the missing stamp."""
    failures = run_validators(
        _proposal("concepts/x/overview.md", MISSING_GENERATED, _concept())
    )
    messages = [f.message for f in failures if f.law == "okf-strict"]
    assert any("'generated'" in m for m in messages), messages


def _rendered(front: str) -> str:
    """Carries valid `sources` so a `generated`-focused case fails on `generated`
    and nothing else."""
    return (
        f"---\ntype: concept\ntitle: X\ndescription: X\n{front}\n"
        f"{_GOOD_SOURCES}\n---\n# X\n"
    )


@pytest.mark.parametrize(
    "front,why",
    [
        ("generated: {}", "empty mapping"),
        ("generated: {by: ''}", "blank actor"),
        ("generated: 2026-07-19", "scalar instead of mapping"),
        ("generated: {by: kbforge/t}", "no `at`"),
        ("generated: {at: 2026-07-19T00:00:00+00:00}", "no `by`"),
    ],
)
def test_malformed_generated_block_is_reported(front, why):
    """`generated` is a mapping, so a presence check cannot judge it — every
    shape here is non-None and useless. The last two matter most: OKF §5.2 makes
    `by` REQUIRED, and `whats_stale` reads the rendered file, so a projection
    carrying `generated_at` does not rescue a file that omits `at`."""
    failures = run_validators(
        _proposal("concepts/x/overview.md", _rendered(front), _concept())
    )
    messages = [f.message for f in failures if f.law == "okf-strict"]
    assert any("generated" in m for m in messages), (why, messages)


def test_rendered_at_disagreeing_with_the_projection_is_reported():
    """projection↔files coherence binds path sets, not field values. Law 4 reads
    the projection and `whats_stale` reads the file, so nothing else would catch
    the two carriers disagreeing about when a concept was generated."""
    concept = ConceptFrontmatter(
        type="concept",
        sources=[ANCHOR],
        generated_at=datetime(2026, 7, 19, tzinfo=UTC),
        generated_by="kbforge/t",
    )
    content = (
        "---\ntype: concept\ntitle: X\ndescription: X\n"
        "generated: {by: kbforge/t, at: '2020-01-01T00:00:00+00:00'}\n"
        "sources:\n- id: s:n\n  resource: s:n\n---\n# X\n"
    )
    failures = run_validators(
        _proposal("concepts/x/overview.md", content, concept=concept)
    )
    assert any(f.law == "okf-strict" for f in failures)


def test_rendered_file_without_sources_is_reported():
    """Law 3 checks the projection; provenance an agent can actually read has to
    survive into the file it ships in. Built without `_rendered`, which supplies
    valid sources."""
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n{_GOOD_GENERATED}\n---\n# X\n"
    )
    messages = [f.message for f in run_validators(_proposal("c/x.md", content))]
    assert any("sources" in m for m in messages), messages


@pytest.mark.parametrize(
    "sources,why",
    [
        ('sources: "see the wiki"', "bare string, not a list"),
        ("sources: true", "boolean"),
        ("sources:\n- {id: 'p:r1'}", "entry with no REQUIRED `resource` (§5.1)"),
        ("sources:\n- {}", "entry with no fields at all"),
        ("sources: {resource: x}", "mapping instead of a list"),
    ],
)
def test_malformed_rendered_sources_is_reported(sources, why):
    """Law 3 checks the projection. On the file that ships, `sources` was only
    tested for truthiness — so a string, a boolean, or an entry missing the
    REQUIRED `resource` all counted as provenance."""
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n"
        f"{_GOOD_GENERATED}\n{sources}\n---\n# X\n"
    )
    assert "okf-strict" in _laws(content, _concept()), why


def test_rendered_sources_citing_a_different_resource_is_reported():
    """The dangerous shape: well-formed provenance that is not the provenance the
    gate validated. An agent decides whether to trust a claim by following this."""
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n{_GOOD_GENERATED}\n"
        "sources:\n- {id: 'evil', resource: 'https://evil.test/fake'}\n---\n# X\n"
    )
    assert "okf-strict" in _laws(content, _concept())


@pytest.mark.parametrize(
    "key,value",
    [("type", "false"), ("title", "0"), ("description", "[]")],
)
def test_non_string_okf_field_in_the_rendered_file_is_reported(key, value):
    """The presence loop rejected only None and blank strings, so `type: false`
    — a boolean — counted as a present OKF type."""
    lines = {
        "type": "concept",
        "title": "X",
        "description": "X",
        key: value,
    }
    front = "\n".join(f"{k}: {v}" for k, v in lines.items())
    content = f"---\n{front}\n{_GOOD_GENERATED}\n{_GOOD_SOURCES}\n---\n# X\n"
    assert "okf-strict" in _laws(content, _concept())


def test_rendered_type_disagreeing_with_the_projection_is_reported():
    content = (
        f"---\ntype: something-else\ntitle: X\ndescription: X\n{_GOOD_GENERATED}\n"
        f"{_GOOD_SOURCES}\n---\n# X\n"
    )
    assert "okf-strict" in _laws(content, _concept())


def test_rendered_links_not_in_the_projection_is_reported():
    """Law 2 resolves the projection's links. A link present only in the file is
    invisible to it — which is how a dangling link reaches the bundle."""
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n{_GOOD_GENERATED}\n"
        f"{_GOOD_SOURCES}\nlinks:\n- concepts/ghost/overview.md\n---\n# X\n"
    )
    assert "okf-strict" in _laws(content, _concept(links=[]))


@pytest.mark.parametrize(
    "at,why",
    [
        ("'2026-07-19T00:00:00Z'", "quoted Z — the spelling OKF's own examples use"),
        ("'2026-07-19T02:00:00+02:00'", "same instant, different offset"),
        ("2026-07-19T00:00:00Z", "bare Z"),
    ],
)
def test_equivalent_instants_are_accepted_however_spelled(at, why):
    """The binding must compare instants, not serialized text. Rejecting a
    correct artifact over YAML quoting blocks a legitimate publish."""
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n"
        f"generated: {{by: kbforge/t, at: {at}}}\n{_GOOD_SOURCES}\n---\n# X\n"
    )
    assert _laws(content, _concept()) == [], why


def test_genuinely_different_instant_is_still_reported():
    content = (
        f"---\ntype: concept\ntitle: X\ndescription: X\n"
        f"generated: {{by: kbforge/t, at: '2020-01-01T00:00:00+00:00'}}\n"
        f"{_GOOD_SOURCES}\n---\n# X\n"
    )
    assert "okf-strict" in _laws(content, _concept())


@pytest.mark.parametrize("field", ["type", "by"])
def test_zero_width_characters_do_not_count_as_content(field):
    """str.strip() removes NBSP but not U+200B (category Cf), so a zero-width
    space passed every blankness test. Pasted-from-a-browser text carries them."""
    zwsp = "​"
    if field == "type":
        concept = _concept(concept_type=zwsp)
        content = (
            f"---\ntype: '{zwsp}'\ntitle: X\ndescription: X\n{_GOOD_GENERATED}\n"
            f"{_GOOD_SOURCES}\n---\n# X\n"
        )
        assert "okf-type" in _laws(content, concept)
    else:
        content = (
            f"---\ntype: concept\ntitle: X\ndescription: X\n"
            f"generated: {{by: '{zwsp}', at: '2026-07-19T00:00:00+00:00'}}\n"
            f"{_GOOD_SOURCES}\n---\n# X\n"
        )
        assert "okf-strict" in _laws(content, _concept())


def test_a_reserved_name_carrying_concept_frontmatter_is_not_exempt():
    """`index.md` is exempt because a directory listing has no frontmatter. A
    file with full concept frontmatter under that name is a smuggled concept —
    exempting it skips strict checks AND projection coherence."""
    failures = run_validators(
        _proposal("concepts/deep/index.md", "---\ngarbage: [\n---\nnope\n")
    )
    assert failures != []


def test_a_real_directory_listing_stays_exempt():
    """The exemption must survive for what it is actually for."""
    listing = "# Concepts\n\n* [X](x/overview.md) - a concept\n"
    assert run_validators(_proposal("concepts/index.md", listing)) == []


def test_reserved_files_are_exempt_from_strict_checks():
    failures = run_validators(_proposal("apps/index.md", "listing, no frontmatter"))
    assert [f for f in failures if f.law == "okf-strict"] == []


def test_run_validators_also_runs_artifact_laws():
    # a file present but no concept projection → §4.4 coherence still fires
    failures = run_validators(_proposal("concepts/x/overview.md", GOOD))
    assert any(f.law == "projection-coherence" for f in failures)
