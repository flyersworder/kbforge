"""Editorial links (#41; architecture.md §7.4): declared in a reviewed
`links.yaml`, resolved by doc_id over the whole mirror, rendered in the
pipeline-owned `## Related` section, and kept current by a sidecar of what
each concept last published.

A pipeline flag, not connector config, for `--grounding`'s reason: a connector
must not know other systems exist (`normalize` is pure)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.grounding import is_qualified


class LinkEntry(BaseModel):
    """`extra="forbid"`, so `symetric: true` is an error, not a one-way link."""

    model_config = ConfigDict(extra="forbid")

    to: str
    note: str | None = None
    symmetric: bool = False


class LinksConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: dict[str, list[str | LinkEntry]] = Field(default_factory=dict)


def load_links(path: Path | None) -> LinksConfig | None:
    if path is None:
        return None
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return LinksConfig.model_validate(raw)


def _entries(cfg: LinksConfig) -> list[tuple[str, LinkEntry]]:
    """(source, entry) with plain ids lifted to entries, in a fixed order."""
    return [
        (source, LinkEntry(to=item) if isinstance(item, str) else item)
        for source in sorted(cfg.links)
        for item in cfg.links[source]
    ]


def _pairs(cfg: LinksConfig) -> list[tuple[str, str, str | None]]:
    """Every directed (source, target, note) the config declares, reverses of
    symmetric entries included."""
    out: list[tuple[str, str, str | None]] = []
    for source, entry in _entries(cfg):
        out.append((source, entry.to, entry.note))
        if entry.symmetric:
            out.append((entry.to, source, entry.note))
    return out


def links_problems(cfg: LinksConfig) -> list[str]:
    """Shape only ([] = ok). Whether an id *resolves* is not a shape question:
    it may live in a system that has not synced yet (§3.1)."""
    problems: list[str] = []
    for source in sorted(cfg.links):
        if not is_qualified(source):
            problems.append(
                f"links key {source!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
    for source, entry in _entries(cfg):
        if not is_qualified(entry.to):
            problems.append(
                f"link {entry.to!r} under {source!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
        if entry.to == source:
            problems.append(f"link under {source!r} points at itself")
        if entry.note is not None and (
            not entry.note.strip() or len(entry.note.strip().splitlines()) != 1
        ):
            problems.append(
                f"note on {source!r} -> {entry.to!r} must be one non-blank line"
            )
    seen: set[tuple[str, str]] = set()
    for source, target, _ in _pairs(cfg):
        if (source, target) in seen:
            problems.append(f"{source!r} -> {target!r} is declared more than once")
        seen.add((source, target))
    return problems


def expand(cfg: LinksConfig) -> dict[str, dict[str, str | None]]:
    """source doc_id -> {target doc_id: note}. On a duplicate the first
    declaration wins, but `links_problems` rejects duplicates before a run."""
    out: dict[str, dict[str, str | None]] = {}
    for source, target, note in _pairs(cfg):
        note = note.strip() if note else None
        out.setdefault(source, {}).setdefault(target, note)
    return out
