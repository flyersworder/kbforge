from kbforge.models import ProposedChange
from kbforge.validate import run_validators

GOOD = """---
type: concept
title: X
description: X
timestamp: 2026-07-19T00:00:00+00:00
---
# X
body
"""

MISSING_DESC = """---
type: concept
title: X
timestamp: 2026-07-19T00:00:00+00:00
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


def test_reserved_files_are_exempt_from_strict_checks():
    failures = run_validators(_proposal("apps/index.md", "listing, no frontmatter"))
    assert [f for f in failures if f.law == "okf-strict"] == []


def test_run_validators_also_runs_artifact_laws():
    # a file present but no concept projection → §4.4 coherence still fires
    failures = run_validators(_proposal("concepts/x/overview.md", GOOD))
    assert any(f.law == "projection-coherence" for f in failures)
