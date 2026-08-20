"""Cross-source grounding: which documents ground which, and whether that has
changed since the concept was last built (design note 2026-08-20).

Everything here is pure except the four sidecar functions. Resolution lives on
this side of the seam, never in a synthesizer: a synthesizer that chose its own
sources would be choosing its own provenance."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.models import CanonicalDocument

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
