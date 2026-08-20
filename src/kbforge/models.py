"""Pydantic data model for kbforge. See docs/architecture.md §3.

This module starts with the emit-side classes the agent-facing artifact
contract (§4.4) validates. Ingest-side classes (Cursor, ConnectorInfo,
RawRecord, FetchResult, CanonicalDocument, ChangeSet) arrive with the plans
that build and test them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Each anchor becomes one OKF v0.2 `sources` entry at emit time (§5.1), whose
    REQUIRED `resource` field takes this anchor's `url` — or, when there is none,
    falls back to "system:native_id" (see `synthesize._source_entry` for why that
    fallback is honest but not spec-sanctioned)."""

    system: str
    native_id: str
    url: str | None = None
    retrieved_at: datetime
    content_hash: str


class ConceptFrontmatter(BaseModel):
    """The checkable head of an emitted OKF v0.2 concept (§4.4).

    Fields are permissive so a law-violating concept can be represented and then
    reported by the validators — kbforge checks synthesis output, it does not
    trust it (spec §5). `type` serializes onto the OKF `type` key; `generated_by`
    and `generated_at` onto `generated: {by, at}` (§5.2); each `sources` entry
    onto one OKF `sources` entry (§5.1). This is the §4.4 projection, not the
    whole frontmatter: title, description, and the rendered body live in the file
    the publisher writes."""

    # Pydantic ignores unknown keywords by default, which would silently swallow
    # a third-party synthesizer still passing v0.1's `resources=`/`freshness=`
    # and hand back a projection with no anchors and no stamp. The gate would
    # catch it, but three stages later and as a law violation rather than as the
    # migration error it is. Fail at construction instead.
    model_config = ConfigDict(extra="forbid")

    type: str = ""  # OKF's one required field (checked non-empty by validate)
    facets: dict = Field(default_factory=dict)  # law 1
    sources: list[ResourceAnchor] = Field(default_factory=list)  # law 3
    links: list[str] = Field(default_factory=list)  # law 2
    generated_at: datetime | None = None  # law 4 — OKF `generated.at`
    generated_by: str = ""  # OKF `generated.by`, an §7 actor


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
    files_removed: list[str] = Field(default_factory=list)
    """Bundle-relative paths to delete. Assigned by the pipeline, which overwrites
    whatever a synthesizer sets here — it is not merely unused by convention, it
    is discarded. Deletion is structure, not prose (§4.4 posture): a synthesizer
    must not be able to delete a file it dislikes."""
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
    grounded_by: list[str] = Field(default_factory=list)
    """Fully qualified doc_ids this document should be grounded in (§2.1).
    Qualified-only: a bare id would have to be told from a qualified one by
    looking for a colon, and a native_id may contain one."""
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
