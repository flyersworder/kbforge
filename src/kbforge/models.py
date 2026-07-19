"""Pydantic data model for kbforge. See docs/architecture.md §3.

This module starts with the emit-side classes the agent-facing artifact
contract (§4.4) validates. Ingest-side classes (Cursor, ConnectorInfo,
RawRecord, FetchResult, CanonicalDocument, ChangeSet) arrive with the plans
that build and test them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Each anchor becomes one OKF `resource` frontmatter entry at emit time."""

    system: str
    native_id: str
    url: str | None = None
    retrieved_at: datetime
    content_hash: str


class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF concept (§4.4).

    Fields are permissive so a law-violating concept can be represented and then
    reported by the validators — kbforge checks synthesis output, it does not
    trust it (spec §5). `type` and `freshness` serialize onto the OKF `type` and
    `timestamp` keys at write time; each `resources` entry becomes a `resource`
    entry. This is the §4.4 projection, not the whole frontmatter: title,
    description, and the rendered body live in the file the publisher writes."""

    type: str = ""  # OKF's one required field (checked non-empty by validate)
    facets: dict = Field(default_factory=dict)  # law 1
    resources: list[ResourceAnchor] = Field(default_factory=list)  # law 3
    links: list[str] = Field(default_factory=list)  # law 2
    freshness: datetime | None = None  # law 4


class ChangeSummary(BaseModel):
    """Producer-generated MR description, structured."""

    sources_changed: list[ResourceAnchor] = Field(default_factory=list)
    claims_added: list[str] = Field(default_factory=list)
    claims_modified: list[str] = Field(default_factory=list)
    claims_removed: list[str] = Field(default_factory=list)
    conflicts_flagged: list[str] = Field(default_factory=list)
    gaps_flagged: list[str] = Field(default_factory=list)
    grounding_notes: list[str] = Field(default_factory=list)


class ProposedChange(BaseModel):
    """What synthesis hands to a publisher: rendered files, the validated
    frontmatter projection, and a reviewable summary (§3, §4.4)."""

    branch_hint: str
    files: dict[str, str] = Field(default_factory=dict)
    concepts: dict[str, ConceptFrontmatter] = Field(default_factory=dict)
    summary: ChangeSummary = Field(default_factory=ChangeSummary)


class Cursor(BaseModel):
    """Opaque incremental-sync watermark. Core persists it; only the owning
    connector interprets its payload (§4.2)."""

    connector: str
    payload: dict = Field(default_factory=dict)


class ConnectorInfo(BaseModel):
    """Static self-description; used for registry listing (§3)."""

    name: str
    version: str
    source_system: str
    info_types: list[str] = Field(default_factory=list)


class RawRecord(BaseModel):
    """One record as fetched. `anchor_hint` carries what normalize needs to build
    a ResourceAnchor (native_id, url, retrieved_at) — set in fetch, so normalize
    stays clock-free (§4.3)."""

    anchor_hint: dict = Field(default_factory=dict)
    media_type: str
    payload: bytes


class FetchResult(BaseModel):
    records: list[RawRecord] = Field(default_factory=list)
    cursor: Cursor
    complete: bool = True


class CanonicalDocument(BaseModel):
    """The diff-stable unit the mirror stores (§3, §4.3)."""

    anchor: ResourceAnchor
    doc_id: str
    title: str
    text: str
    structured: dict = Field(default_factory=dict)
    relations: list[str] = Field(default_factory=list)
    deleted: bool = False


class ChangeSet(BaseModel):
    """Output of the diff stage; input to synthesis scoping (§3)."""

    added: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    unchanged_count: int = 0

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.modified or self.removed)
