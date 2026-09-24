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
        unquote(m.group(1)) for line in tail.splitlines() if (m := _LINE.match(line))
    ]
