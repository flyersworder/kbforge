"""Editorial links (#41; architecture.md §7.4): declared in a reviewed
`links.yaml`, resolved by doc_id over the whole mirror, rendered in the
pipeline-owned `## Related` section, and kept current by a sidecar of what
each concept last published.

A pipeline flag, not connector config, for `--grounding`'s reason: a connector
must not know other systems exist (`normalize` is pure)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.grounding import is_qualified, write_atomic
from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument
from kbforge.synthesize import concept_path


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
    it may live in a system that has not synced yet (architecture.md §7.4)."""
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


LINKS_DIR = "_links"
"""A subdirectory: `load_all` globs `mirror/*.json`."""


def _system(doc_id: str) -> str:
    return doc_id.partition(":")[0]


@dataclass(frozen=True)
class LinkResolution:
    links: list[tuple[str, str | None]]
    """Resolved (target doc_id, note), sorted by doc_id: every link it ships."""
    managed: list[tuple[str, str | None]]
    """The subset the `_links/` sidecar records. A same-system connector
    relation is left out: referrers and arrivals already keep it current."""
    declares_managed: bool
    """Anything managed was declared, resolved or not. The pipeline then
    writes a sidecar even when it is empty, or a concept whose only link was
    unresolvable at publish would never be rescanned (grounding's rule)."""
    notes: list[str]


def resolve_links(
    doc: CanonicalDocument,
    expanded: dict[str, dict[str, str | None]],
    by_id: dict[str, CanonicalDocument],
) -> LinkResolution:
    """`doc`'s connector relations plus its editorial links, resolved by doc_id
    against `by_id` (the whole mirror overlaid with this run, tombstones out).

    By doc_id, never by path: `bundle-path-collision` guarantees one doc_id per
    bundle path, which is what lets this look across systems (architecture.md §7.4)."""
    editorial = expanded.get(doc.doc_id, {})
    declared: dict[str, str | None] = dict.fromkeys(doc.relations)
    declared.update(editorial)
    path = concept_path(doc.doc_id)
    own = _system(doc.doc_id)
    links: list[tuple[str, str | None]] = []
    managed: list[tuple[str, str | None]] = []
    notes: list[str] = []
    declares = False
    for target in sorted(declared):
        if target == doc.doc_id:
            continue
        is_managed = target in editorial or _system(target) != own
        declares = declares or is_managed
        found = by_id.get(target)
        if found is None or found.deleted:
            if target in editorial:
                notes.append(
                    f"{path}: link to {target} (links.yaml) was not found in the "
                    "mirror or this fetch and was dropped"
                )
            continue
        entry = (target, declared[target])
        links.append(entry)
        if is_managed:
            managed.append(entry)
    return LinkResolution(links, managed, declares, notes)


def _sidecar(mirror: Path, doc_id: str) -> Path:
    return mirror / LINKS_DIR / f"{slot_key(doc_id)}.json"


def read_links(mirror: Path, doc_id: str) -> list[tuple[str, str | None]]:
    """What the concept's managed links were at its last publish. A missing or
    unreadable sidecar is empty, not exempt: an empty record against a current
    link is drift, which is exactly the repair."""
    try:
        payload = json.loads(_sidecar(mirror, doc_id).read_text("utf-8"))
        return [
            (str(target), None if note is None else str(note))
            for target, note in payload["links"]
        ]
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return []


def write_links(
    mirror: Path, doc_id: str, managed: list[tuple[str, str | None]]
) -> None:
    write_atomic(
        _sidecar(mirror, doc_id),
        {"doc_id": doc_id, "links": [[t, n] for t, n in managed]},
    )


def delete_links(mirror: Path, doc_id: str) -> None:
    """Idempotent. A stale sidecar would drift its concept on every run."""
    _sidecar(mirror, doc_id).unlink(missing_ok=True)


def has_links_sidecars(mirror: Path) -> bool:
    """Cheap gate for the link-drift scan: a directory listing, not a load."""
    directory = mirror / LINKS_DIR
    return directory.is_dir() and any(directory.glob("*.json"))


def links_drifted(
    mirror: Path,
    candidates: list[CanonicalDocument],
    current: dict[str, list[tuple[str, str | None]]],
) -> list[str]:
    """Candidates whose managed links (targets and notes) differ from what their
    sidecar recorded. Titles are not compared: a retitled target does not
    rebuild its referrers (architecture.md §7.4)."""
    return sorted(
        d.doc_id
        for d in candidates
        if read_links(mirror, d.doc_id) != current.get(d.doc_id, [])
    )
