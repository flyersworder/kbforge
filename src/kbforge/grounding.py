"""Cross-source grounding: which documents ground which, and whether that has
changed since the concept was last built (design note 2026-08-20).

Everything here is pure except the four sidecar functions. Resolution lives on
this side of the seam, never in a synthesizer: a synthesizer that chose its own
sources would be choosing its own provenance."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument, resource_key

DEFAULT_MAX_GROUNDING_DOCS = 5


class GroundingConfig(BaseModel):
    """The operator subject map (§2.2). `extra="forbid"` so a typo'd key is an
    error rather than a silently empty map."""

    model_config = ConfigDict(extra="forbid")

    max_grounding_docs: int = DEFAULT_MAX_GROUNDING_DOCS
    grounding: dict[str, list[str]] = Field(default_factory=dict)


def load_grounding(path: Path | None) -> GroundingConfig:
    if path is None:
        return GroundingConfig()
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return GroundingConfig.model_validate(raw)


def _qualified(value: str) -> bool:
    """A doc_id is `system:native_id` with both halves non-empty."""
    system, sep, native = value.partition(":")
    return bool(sep and system and native)


def problems_for(cfg: GroundingConfig) -> list[str]:
    """Shape only ([] = ok). Whether an id *resolves* is not a shape question and
    is not fatal -- §2.2, symmetric with the unresolvable-value rule in §3."""
    problems: list[str] = []
    if cfg.max_grounding_docs < 1:
        problems.append("grounding 'max_grounding_docs' must be at least 1")
    for key, values in sorted(cfg.grounding.items()):
        if not _qualified(key):
            problems.append(
                f"grounding key {key!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
        for value in values:
            if not _qualified(value):
                problems.append(
                    f"grounding value {value!r} under {key!r} must be a qualified "
                    "doc_id ('system:native_id'); bare ids are not accepted"
                )
    return problems


def declared_ids(doc: CanonicalDocument, cfg: GroundingConfig) -> list[str]:
    """Both declaration sites, unioned and sorted. Sorted because everything
    downstream -- the cap, the sidecar, the diff -- must be deterministic."""
    return sorted(set(doc.grounded_by) | set(cfg.grounding.get(doc.doc_id, [])))


def resolve(
    owner: CanonicalDocument,
    ids: list[str],
    by_id: dict[str, CanonicalDocument],
    *,
    max_docs: int,
) -> tuple[list[CanonicalDocument], list[str]]:
    """Declared ids -> the documents that will actually be cited, plus notes.

    Nothing here raises: an unresolvable or tombstoned target is a fact about
    another system's sync state, not an error in this run (§3)."""
    notes: list[str] = []
    seen = {resource_key(owner.anchor)}
    kept: list[CanonicalDocument] = []

    for gid in sorted(set(ids)):
        if gid == owner.doc_id:
            continue  # self-reference: silent, not a note
        doc = by_id.get(gid)
        if doc is None:
            notes.append(
                f"{owner.doc_id}: grounding {gid} was not found in the mirror or "
                "this fetch and was dropped"
            )
            continue
        if doc.deleted:
            notes.append(
                f"{owner.doc_id}: grounding {gid} is tombstoned upstream and "
                "was dropped"
            )
            continue
        key = resource_key(doc.anchor)
        if key in seen:
            continue  # same artifact, cited once
        seen.add(key)
        kept.append(doc)

    if len(kept) > max_docs:
        dropped = ", ".join(d.doc_id for d in kept[max_docs:])
        notes.append(
            f"{owner.doc_id}: grounding capped at {max_docs}; dropped {dropped}"
        )
        kept = kept[:max_docs]
    return kept, notes


SIDECAR_DIR = "_grounding"
"""A subdirectory, deliberately: `load_all` globs `mirror/*.json`, so a sidecar
at the root would be parsed as a CanonicalDocument on every run."""


def _sidecar(mirror: Path, doc_id: str) -> Path:
    return mirror / SIDECAR_DIR / f"{slot_key(doc_id)}.json"


def read_sidecar(mirror: Path, doc_id: str) -> dict[str, str] | None:
    """The grounding hashes recorded when this concept was last published, or
    None if it has never been grounded."""
    path = _sidecar(mirror, doc_id)
    if not path.exists():
        return None
    return dict(json.loads(path.read_text("utf-8"))["grounding"])


def write_sidecar(mirror: Path, doc_id: str, recorded: dict[str, str]) -> None:
    path = _sidecar(mirror, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"doc_id": doc_id, "grounding": dict(sorted(recorded.items()))}
    path.write_text(json.dumps(payload, sort_keys=True), "utf-8")


def delete_sidecar(mirror: Path, doc_id: str) -> None:
    """Idempotent. Called when a grounding set empties and when an owner is
    tombstoned -- NOT writing a file does not remove the one already there, and
    a stale sidecar re-synthesizes its document on every run forever (§4)."""
    _sidecar(mirror, doc_id).unlink(missing_ok=True)


def has_sidecars(mirror: Path) -> bool:
    """Cheap gate for the drift scan: a directory listing, not a mirror load."""
    directory = mirror / SIDECAR_DIR
    return directory.is_dir() and any(directory.glob("*.json"))


def drifted(
    mirror: Path,
    candidates: list[CanonicalDocument],
    resolved: dict[str, list[str]],
    hashes: dict[str, str],
) -> list[str]:
    """Owning doc_ids whose grounding moved since they were last published.

    `resolved` must be POST-resolution, matching what `write_sidecar` recorded.
    Comparing declared ids instead leaves an unresolvable id permanently present
    on one side and absent on the other, re-synthesizing forever (§4)."""
    out: list[str] = []
    for doc in candidates:
        # A missing sidecar is an EMPTY recorded set, not "exempt from drift".
        # Declared grounding that was unresolvable at first publish resolves to
        # nothing, so the pipeline deletes rather than writes the sidecar --
        # and on any fresh multi-system deployment the first system to run has
        # none of the others in the mirror. Skipping here would strand every
        # concept it published, ungrounded forever, once the others synced.
        # This cannot loop: a document declaring nothing has
        # `current == set() == recorded`, and `any(...)` over `{}` is False.
        recorded = read_sidecar(mirror, doc.doc_id) or {}
        current = set(resolved.get(doc.doc_id, []))
        if current != set(recorded):  # rule 3, and rule 2 by construction
            out.append(doc.doc_id)
            continue
        if any(hashes.get(gid) != h for gid, h in recorded.items()):  # rule 1
            out.append(doc.doc_id)
    return sorted(out)
