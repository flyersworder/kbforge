"""Renders a ChangeSummary as the review request's body. Shared by every
publisher: the dry-run publisher writes it to MR_BODY.md for want of anywhere
better, while a forge publisher uses it as the PR/MR description — which is the
shape it always had."""

from __future__ import annotations

from kbforge.models import ChangeSummary


def summary_md(summary: ChangeSummary) -> str:
    lines = ["# Proposed change", ""]
    for label, items in (
        ("Added", summary.claims_added),
        ("Modified", summary.claims_modified),
        ("Removed", summary.claims_removed),
        ("Conflicts", summary.conflicts_flagged),
        ("Gaps", summary.gaps_flagged),
    ):
        if items:
            lines.append(f"## {label}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
