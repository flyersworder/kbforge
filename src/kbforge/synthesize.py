"""Stub synthesizer: a deterministic CanonicalDocument → ProposedChange map.

No LLM. Real synthesis (grounding contract, token budget) is a later increment;
this stub proves the pipeline wiring and gives the validators real structure to
check. kbforge checks synthesis output either way (spec §5)."""

from __future__ import annotations

from typing import Protocol

import yaml

from kbforge import __version__
from kbforge.models import (
    CanonicalDocument,
    ChangeSet,
    ChangeSummary,
    ConceptFrontmatter,
    ProposedChange,
    ResourceAnchor,
)

_SCALAR = (str, int, float, bool)

# OKF §7 actor for the stub synthesizer. The LLM synthesizer overrides it with
# the model, matching the spec's own `reference_agent/gemini-2.5-pro` example
# where the version slot carries the model rather than the tool's release.
_DEFAULT_ACTOR = f"kbforge/{__version__}"


def concept_path(doc_id: str) -> str:
    """Deterministic bundle path from a doc_id ("system:native_id")."""
    _, _, native = doc_id.partition(":")
    stem = native.removesuffix(".md").strip("/")
    return f"concepts/{stem}/overview.md"


def _facets(structured: dict) -> dict:
    def ok(v: object) -> bool:
        if isinstance(v, _SCALAR):
            return True
        return isinstance(v, list) and all(isinstance(i, _SCALAR) for i in v)

    return {
        k: v for k, v in structured.items() if v not in (None, "", [], {}) and ok(v)
    }


def _generated(fm: ConceptFrontmatter) -> dict:
    """The OKF v0.2 `generated` block (§5.2), which supersedes v0.1 `timestamp`.

    `at` is the anchor's `retrieved_at` — a fetch time standing in for "last
    meaningful change". kbforge earns that equivalence from the no-op rule: a
    concept is re-rendered only when its canonical form actually changed, so the
    run that rewrote it *is* its last meaningful change. A producer without
    canonicalization cannot make the same claim."""
    out: dict = {"by": fm.generated_by}
    if fm.generated_at is not None:
        out["at"] = fm.generated_at.isoformat()
    return out


def _source_entry(anchor: ResourceAnchor) -> dict:
    """One OKF v0.2 `sources` entry (§5.1). `resource` is REQUIRED within an entry;
    when the anchor carries no URL, §5.1 permits a scope descriptor in its place,
    and "system:native_id" is the stable one kbforge already uses as a doc_id.
    `content_hash` is a producer extension key (§4.1 permits them): it is what
    makes a published concept auditable back to the canonical form it was
    synthesized from."""
    descriptor = f"{anchor.system}:{anchor.native_id}"
    return {
        "id": descriptor,
        "resource": anchor.url or descriptor,
        "content_hash": anchor.content_hash,
    }


def _render(
    doc: CanonicalDocument,
    fm: ConceptFrontmatter,
    *,
    title: str,
    description: str,
    body: str,
) -> str:
    front: dict = {
        "type": fm.type,
        "title": title,
        "description": description,
        "generated": _generated(fm),
    }
    front.update(fm.facets)
    front["sources"] = [_source_entry(a) for a in fm.sources]
    if fm.links:
        front["links"] = fm.links
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n# {title}\n\n{body}\n"


def assemble(
    items: list[tuple[CanonicalDocument, str, str, str]],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
    *,
    generated_by: str = _DEFAULT_ACTOR,
) -> ProposedChange:
    """Build the ProposedChange frame from per-doc prose (doc, title, description,
    body). Both synthesizers produce `items` differently and share this assembly, so
    the kbforge-owned structural frame is identical regardless of prose source."""
    known = {concept_path(doc.doc_id) for doc, *_ in items} | set(existing_paths)
    files: dict[str, str] = {}
    concepts: dict[str, ConceptFrontmatter] = {}
    summary = ChangeSummary()
    for doc, title, description, body in items:
        path = concept_path(doc.doc_id)
        links = [concept_path(r) for r in doc.relations]
        fm = ConceptFrontmatter(
            type=str(doc.structured.get("type") or "concept"),
            facets=_facets(doc.structured),
            sources=[doc.anchor],
            links=sorted(p for p in links if p in known),  # drop dangling (law 2)
            generated_at=doc.anchor.retrieved_at,
            generated_by=generated_by,
        )
        concepts[path] = fm
        files[path] = _render(doc, fm, title=title, description=description, body=body)
        summary.sources_changed.append(doc.anchor)
    summary.claims_added = sorted(concept_path(x) for x in changeset.added)
    summary.claims_modified = sorted(concept_path(x) for x in changeset.modified)
    # Paths, not doc_ids: the review body must speak one identifier format.
    summary.claims_removed = sorted(concept_path(x) for x in changeset.removed)
    # A deletion-only run has no items, so the system has to come from the
    # removed doc_ids. Falling back to a literal would publish to a different
    # branch and open a second review request — and so would deriving the two
    # cases from two different fields, which is why both read the doc_id's
    # "system:native_id" prefix rather than one reading doc_id and the other
    # anchor.system. Nothing enforces that a connector keeps those two in
    # agreement (every shipped one does), and a plugin where they diverged
    # would otherwise get a second branch and a second review request on its
    # deletion-only runs.
    if items:
        system = items[0][0].doc_id.partition(":")[0]
    elif changeset.removed:
        system = changeset.removed[0].partition(":")[0]
    else:
        system = "source"
    return ProposedChange(
        branch_hint=f"sync/{system}",
        files=files,
        concepts=concepts,
        summary=summary,
    )


class Synthesizer(Protocol):
    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange: ...


class StubSynthesizer:
    """Deterministic, no LLM: title and description mirror the source; body is the
    canonical text verbatim. The default synthesizer and the test baseline."""

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items = [(doc, doc.title, doc.title, doc.text) for doc in changed_docs]
        return assemble(items, changeset, existing_paths)


def synthesize(
    changed_docs: list[CanonicalDocument],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
) -> ProposedChange:
    """Backwards-compatible module entry point; delegates to StubSynthesizer."""
    return StubSynthesizer().synthesize(changed_docs, changeset, existing_paths)
