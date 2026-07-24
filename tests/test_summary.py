from kbforge.models import ChangeSummary
from kbforge.publishers.summary import summary_md


def test_summary_md_renders_populated_sections():
    md = summary_md(
        ChangeSummary(
            claims_added=["concepts/x/overview.md"],
            claims_modified=["concepts/y/overview.md"],
        )
    )
    assert md.startswith("# Proposed change\n")
    assert "## Added" in md
    assert "- concepts/x/overview.md" in md
    assert "## Modified" in md


def test_summary_md_omits_empty_sections():
    md = summary_md(ChangeSummary(claims_added=["a.md"]))
    assert "## Added" in md
    assert "## Removed" not in md
    assert "## Conflicts" not in md


def test_summary_md_ends_with_single_newline():
    md = summary_md(ChangeSummary(claims_added=["a.md"]))
    assert md.endswith("\n")
    assert not md.endswith("\n\n")
