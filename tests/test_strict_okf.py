from kbforge.models import ProposedChange
from kbforge.validate import run_validators

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


def test_reserved_files_are_exempt_from_strict_checks():
    failures = run_validators(_proposal("apps/index.md", "listing, no frontmatter"))
    assert [f for f in failures if f.law == "okf-strict"] == []


def test_run_validators_also_runs_artifact_laws():
    # a file present but no concept projection → §4.4 coherence still fires
    failures = run_validators(_proposal("concepts/x/overview.md", GOOD))
    assert any(f.law == "projection-coherence" for f in failures)
