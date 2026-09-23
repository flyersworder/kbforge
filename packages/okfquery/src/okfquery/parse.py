"""Pure frontmatter parsing for OKF v0.2 concepts.

No filesystem, no clock, no DuckDB: text in, a `Concept` and its `Problem`s out.
That purity is why every row of the problem ladder is testable from a string
literal.

This module deliberately does NOT reuse kbforge's `validate._parse_frontmatter`,
and not only because okfquery imports no kbforge. That function collapses
no-fence, unterminated-fence, and broken-YAML alike to `{}` -- correct for a gate,
which needs only "no usable frontmatter" and fails either way. Here, *which* of
the three occurred is the entire finding: a missing fence is a renderer bug,
broken YAML is a hand-edit, an unterminated fence is a truncated write. Do not
"fix" this duplication by importing kbforge's version."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import yaml

RESERVED = frozenset({"index.md", "log.md"})
"""OKF §8 directory listings and change logs. See `is_reserved` for the rule."""

# The keys OKF owns at the head of a concept. Everything else in the frontmatter
# is a facet. Mirrors kbforge's synthesize.OKF_OWNED except for `tags`: kbforge
# owns `tags` so only it writes the key, but to a reader `tags` is a filterable
# list like any facet (`okfquery index --group-by tags`, `okfquery related`).
OKF_OWNED = frozenset({"type", "title", "description", "generated", "sources", "links"})

_REQUIRED = ("type", "title", "description", "generated", "sources")


@dataclass(frozen=True)
class Problem:
    kind: str
    detail: str


@dataclass
class Concept:
    type: str | None = None
    title: str | None = None
    description: str | None = None
    generated_by: str | None = None
    generated_at: datetime | None = None
    facets: dict = field(default_factory=dict)
    body: str = ""
    sources: list[dict] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)


def is_reserved(basename: str, content: str) -> bool:
    """Whether a file under `concepts/` is a non-concept OKF artifact.

    Copied from kbforge's `validate._check_strict_okf`, deliberately and with
    this note: a file is exempt only if its basename is reserved AND it does not
    open a frontmatter fence, because an `index.md` bearing frontmatter is
    claiming to be a concept and exempting it would skip every check. Keyed on
    the raw fence, not on a parse result -- "no frontmatter" and "broken
    frontmatter" must not look alike."""
    return basename in RESERVED and not content.lstrip().startswith("---")


def _split(content: str) -> tuple[str | None, str, Problem | None]:
    """(raw frontmatter, body, problem). Raw is None when there is none to parse."""
    text = content.lstrip()
    if not text.startswith("---"):
        return (
            None,
            content,
            Problem("no-frontmatter", "file does not open a '---' frontmatter fence"),
        )
    _, _, rest = text.partition("---")
    raw, sep, body = rest.partition("\n---")
    if not sep:
        return (
            None,
            content,
            Problem(
                "unterminated-frontmatter",
                "frontmatter fence is opened but never closed by a closing '---'",
            ),
        )
    return raw, body.lstrip("\n"), None


def _generated(front: dict, out: Concept) -> None:
    block = front.get("generated")
    if not isinstance(block, dict):
        out.problems.append(
            Problem(
                "bad-generated", f"'generated' is {type(block).__name__}, not a mapping"
            )
        )
        return
    by = block.get("by")
    out.generated_by = by if isinstance(by, str) and by else None
    if out.generated_by is None:
        out.problems.append(
            Problem("bad-generated", "'generated.by' is missing or empty")
        )
    at = block.get("at")
    if at is None:
        out.problems.append(Problem("bad-generated", "'generated.at' is missing"))
        return
    parsed = _timestamp(at, out)
    if parsed is not None and parsed.utcoffset() is None:
        out.problems.append(
            Problem(
                "naive-timestamp",
                f"'generated.at' {at!r} carries no UTC offset (§4.4 law 4 requires "
                "an aware stamp); read as UTC",
            )
        )
        parsed = parsed.replace(tzinfo=UTC)
    out.generated_at = parsed


def _timestamp(at: object, out: Concept) -> datetime | None:
    if isinstance(at, datetime):
        return at
    try:
        return datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        out.problems.append(
            Problem(
                "bad-timestamp", f"'generated.at' {at!r} is not an ISO-8601 datetime"
            )
        )
        return None


def _sources(front: dict, out: Concept) -> None:
    raw = front.get("sources")
    if not isinstance(raw, list):
        out.problems.append(
            Problem("bad-sources", f"'sources' is {type(raw).__name__}, not a list")
        )
        return
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            out.problems.append(
                Problem(
                    "bad-sources",
                    f"'sources' entry {i} is {type(entry).__name__}, not a mapping",
                )
            )
            continue
        resource = entry.get("resource")
        if not isinstance(resource, str) or not resource:
            out.problems.append(
                Problem(
                    "bad-sources",
                    f"'sources' entry {i} has no 'resource' (§5.1 requires one)",
                )
            )
            resource = None
        # Carried regardless: problems are additive, and dropping the entry
        # would silently renumber every ordinal after it.
        out.sources.append(
            {
                "id": entry.get("id") if isinstance(entry.get("id"), str) else None,
                "resource": resource,
                "content_hash": entry.get("content_hash")
                if isinstance(entry.get("content_hash"), str)
                else None,
            }
        )


def _links(front: dict, out: Concept) -> None:
    raw = front.get("links")
    if raw is None:
        return
    if not isinstance(raw, list):
        out.problems.append(
            Problem("bad-links", f"'links' is {type(raw).__name__}, not a list")
        )
        return
    for entry in raw:
        if isinstance(entry, str):
            out.links.append(entry)
        else:
            out.problems.append(
                Problem("bad-links", f"'links' entry {entry!r} is not a string")
            )


def _facets(front: dict) -> dict:
    """Frontmatter keys OKF does not own. Mirrors synthesize._facets on the emit
    side: scalars and scalar lists only, so a nested mapping never lands in a
    column typed for values."""
    scalar = (str, int, float, bool)

    def ok(v: object) -> bool:
        if isinstance(v, scalar):
            return True
        return isinstance(v, list) and all(isinstance(i, scalar) for i in v)

    return {k: v for k, v in front.items() if k not in OKF_OWNED and ok(v)}


def parse(content: str) -> Concept:
    """One concept file's text into a `Concept` plus the problems it carries.

    Never raises. A file this cannot read still yields a Concept -- with NULLs
    for what was unreadable -- because dropping it would make an audit tool hide
    exactly the files most likely to be wrong."""
    out = Concept()
    raw, body, problem = _split(content)
    out.body = body
    if problem is not None:
        out.problems.append(problem)
        return out
    try:
        front = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        out.problems.append(
            Problem(
                "invalid-yaml",
                f"frontmatter is not valid YAML: {exc.__class__.__name__}",
            )
        )
        return out
    if not isinstance(front, dict):
        out.problems.append(
            Problem(
                "frontmatter-not-mapping",
                f"frontmatter parses to {type(front).__name__}, not a mapping",
            )
        )
        return out

    missing = [k for k in _REQUIRED if front.get(k) is None]
    if missing:
        out.problems.append(
            Problem(
                "missing-required", f"missing required OKF keys: {', '.join(missing)}"
            )
        )

    for key in ("type", "title", "description"):
        value = front.get(key)
        setattr(out, key, value if isinstance(value, str) else None)
    out.facets = _facets(front)
    if front.get("generated") is not None:
        _generated(front, out)
    if front.get("sources") is not None:
        _sources(front, out)
    _links(front, out)
    return out
