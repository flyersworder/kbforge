"""Renders a ChangeSummary as the review request's body. Shared by every
publisher: the dry-run publisher writes it to MR_BODY.md for want of anywhere
better, while a forge publisher uses it as the PR/MR description — which is the
shape it always had."""

from __future__ import annotations

from kbforge.models import ChangeSummary


def summary_md(summary: ChangeSummary) -> str:
    # Residual gap, deliberately not papered over here: "Removed" renders the
    # changeset's view, but an adapter intersects removals with what is actually
    # on the base tree before committing (GitLab 400s / GitHub 422s on deleting
    # an absent path). So a path already gone from base is still advertised
    # under "## Removed" while the diff performs no deletion for it. The body
    # describes the run's intent, not the resulting diff; closing the gap would
    # mean the summary could not be rendered until after the commit, and every
    # publisher would need the adapter's filtered set handed back to it.
    lines = ["# Proposed change", ""]
    for label, items in (
        ("Added", summary.claims_added),
        ("Modified", summary.claims_modified),
        ("Removed", summary.claims_removed),
        ("Conflicts", summary.conflicts_flagged),
        ("Gaps", summary.gaps_flagged),
        # Rendered so a file in the diff that no claims_* list accounts for —
        # a referrer pulled into scope to drop its links to a removed concept —
        # is explained rather than left as an unexplained change.
        ("Notes", summary.grounding_notes),
    ):
        if items:
            lines.append(f"## {label}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
