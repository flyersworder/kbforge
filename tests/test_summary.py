import pytest

from kbforge.models import ChangeSummary
from kbforge.publishers.summary import merge_summaries, parse_summary_md, summary_md


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


# --- accumulating into an open review request (#43) -------------------------

A, B, C = (f"concepts/{n}/overview.md" for n in "abc")


def test_parse_is_the_inverse_of_render():
    summary = ChangeSummary(
        claims_added=[A],
        claims_modified=[B],
        claims_removed=[C],
        conflicts_flagged=["x conflict"],
        gaps_flagged=["y gap"],
        grounding_notes=[f"{A}: a note: with a colon", "chunked review: 1 of 2"],
    )
    assert parse_summary_md(summary_md(summary)) == summary


def test_parse_ignores_what_it_did_not_render():
    text = "Hand-written intro.\n\n## Added\n- " + A + "\n\n## Reviewer notes\n- mine\n"
    assert parse_summary_md(text) == ChangeSummary(claims_added=[A])


@pytest.mark.parametrize(
    ("prior", "new", "expected"),
    [
        ("added", "modified", "added"),
        ("added", "removed", None),  # never on the base: the diff has nothing
        ("modified", "removed", "removed"),
        ("removed", "added", "modified"),
        ("modified", "modified", "modified"),
        ("added", None, "added"),  # a later run that did not touch it
        (None, "removed", "removed"),
    ],
)
def test_claims_are_relative_to_the_base_across_runs(prior, new, expected):
    def summary(kind):
        return ChangeSummary(**({f"claims_{kind}": [A]} if kind else {}))

    touched = {A} if new else set()
    merged = merge_summaries(summary(prior), summary(new), touched)
    got = [
        kind
        for kind in ("added", "modified", "removed")
        if A in getattr(merged, f"claims_{kind}")
    ]
    assert got == ([expected] if expected else [])


def test_a_touched_paths_notes_are_replaced_and_others_kept():
    prior = ChangeSummary(
        claims_added=[A, B],
        grounding_notes=[
            f"{A}: link to z was dropped",
            f"{B}: links to other:y (system other); merge that first",
            "chunked review: this request carries 1 of 3",
        ],
    )
    new = ChangeSummary(
        grounding_notes=[f"{A}: re-synthesized because its links changed"]
    )
    merged = merge_summaries(prior, new, {A})
    assert merged.grounding_notes == [
        f"{B}: links to other:y (system other); merge that first",
        f"{A}: re-synthesized because its links changed",
    ]
    assert merged.claims_added == [A, B]


def test_the_live_repro_keeps_run_ones_claims():
    """PR #90 on the scratch repo: run 3 rebuilt one concept for link drift and
    its description replaced run 1's four Added claims."""
    run1 = ChangeSummary(claims_added=[A, B, C])
    run3 = ChangeSummary(
        grounding_notes=[f"{A}: re-synthesized because its links changed"]
    )
    merged = merge_summaries(parse_summary_md(summary_md(run1)), run3, {A})
    assert merged.claims_added == [A, B, C]
    assert merged.grounding_notes == run3.grounding_notes
