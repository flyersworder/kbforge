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


def test_rendered_file_without_generated_is_reported():
    """kbforge requires `generated` even though OKF §11 requires only `type`:
    producer-side strictness, so law 4 can never be satisfied by a projection
    whose rendered file omits the stamp."""
    failures = run_validators(_proposal("concepts/x/overview.md", MISSING_GENERATED))
    assert any(f.law == "okf-strict" for f in failures)


def _rendered(front: str) -> str:
    return f"---\ntype: concept\ntitle: X\ndescription: X\n{front}\n---\n# X\n"


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
    failures = run_validators(_proposal("concepts/x/overview.md", _rendered(front)))
    assert any(f.law == "okf-strict" for f in failures), why


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
    survive into the file it ships in."""
    content = _rendered("generated: {by: kbforge/t, at: '2026-07-19T00:00:00+00:00'}")
    failures = run_validators(_proposal("concepts/x/overview.md", content))
    assert any(f.law == "okf-strict" for f in failures)


def test_reserved_files_are_exempt_from_strict_checks():
    failures = run_validators(_proposal("apps/index.md", "listing, no frontmatter"))
    assert [f for f in failures if f.law == "okf-strict"] == []


def test_run_validators_also_runs_artifact_laws():
    # a file present but no concept projection → §4.4 coherence still fires
    failures = run_validators(_proposal("concepts/x/overview.md", GOOD))
    assert any(f.law == "projection-coherence" for f in failures)
