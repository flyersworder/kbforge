"""Renders a ChangeSummary as the review request's body. Shared by every
publisher: the dry-run publisher writes it to MR_BODY.md for want of anywhere
better, while a forge publisher uses it as the PR/MR description — which is the
shape it always had."""

from __future__ import annotations

from kbforge.models import ChangeSummary

_SECTIONS = (
    ("Added", "claims_added"),
    ("Modified", "claims_modified"),
    ("Removed", "claims_removed"),
    ("Conflicts", "conflicts_flagged"),
    ("Gaps", "gaps_flagged"),
    # Rendered so a file in the diff that no claims_* list accounts for — a
    # referrer pulled into scope to drop its links to a removed concept — is
    # explained rather than left as an unexplained change.
    ("Notes", "grounding_notes"),
)
_CLAIMS = ("claims_added", "claims_modified", "claims_removed")
_NOTES = ("conflicts_flagged", "gaps_flagged", "grounding_notes")


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
    for label, field in _SECTIONS:
        items = getattr(summary, field)
        if items:
            lines.append(f"## {label}")
            lines += [f"- {i}" for i in items]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_summary_md(text: str) -> ChangeSummary:
    """The inverse of `summary_md` for its own output. Anything it did not
    render -- a hand-written paragraph, a heading of its own -- is ignored."""
    fields = dict(_SECTIONS)
    out = ChangeSummary()
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            current = fields.get(line.removeprefix("## ").strip())
        elif current is not None and line.startswith("- "):
            getattr(out, current).append(line[2:])
    return out


# (earlier kind, this run's kind) -> the kind relative to the base branch, which
# is what the accumulated branch's diff shows. None: the path is not in the diff.
_ACROSS_RUNS: dict[tuple[str, str], str | None] = {
    ("claims_added", "claims_added"): "claims_added",
    ("claims_added", "claims_modified"): "claims_added",
    ("claims_added", "claims_removed"): None,
    ("claims_modified", "claims_added"): "claims_modified",
    ("claims_modified", "claims_modified"): "claims_modified",
    ("claims_modified", "claims_removed"): "claims_removed",
    ("claims_removed", "claims_added"): "claims_modified",
    ("claims_removed", "claims_modified"): "claims_modified",
    ("claims_removed", "claims_removed"): "claims_removed",
}


def _note_path(note: str) -> str | None:
    head, sep, _ = note.partition(": ")
    return head if sep and head.endswith(".md") else None


def merge_summaries(
    prior: ChangeSummary, new: ChangeSummary, touched: set[str]
) -> ChangeSummary:
    """One summary for a review request that several runs published into (#43).

    Claims are per path and relative to the base branch, because the branch
    accumulates: a concept added by one run and modified by the next is still
    an addition. Notes about a path this run `touched` are replaced by this
    run's, since they described a file this run rewrote; notes about other
    paths still describe the diff and are kept. A note naming no path (the
    chunked-review count) is this run's alone."""
    kinds: dict[str, str | None] = {}
    for field in _CLAIMS:
        for path in getattr(prior, field):
            kinds[path] = field
    for field in _CLAIMS:
        for path in getattr(new, field):
            earlier = kinds.get(path)
            kinds[path] = field if earlier is None else _ACROSS_RUNS[(earlier, field)]
    merged = ChangeSummary(sources_changed=list(new.sources_changed))
    for field in _CLAIMS:
        setattr(merged, field, sorted(p for p, k in kinds.items() if k == field))
    for field in _NOTES:
        kept = [
            n
            for n in getattr(prior, field)
            if (path := _note_path(n)) is not None and path not in touched
        ]
        setattr(merged, field, list(dict.fromkeys(kept + getattr(new, field))))
    return merged
