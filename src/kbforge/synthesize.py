"""Stub synthesizer: a deterministic CanonicalDocument → ProposedChange map.

No LLM. Real synthesis (grounding contract, token budget) is a later increment;
this stub proves the pipeline wiring and gives the validators real structure to
check. kbforge checks synthesis output either way (spec §5)."""

from __future__ import annotations

from typing import Protocol

import yaml

from kbforge.models import (
    CanonicalDocument,
    ChangeSet,
    ChangeSummary,
    ConceptFrontmatter,
    ProposedChange,
)

_SCALAR = (str, int, float, bool)


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
        "timestamp": fm.freshness.isoformat() if fm.freshness else None,
    }
    front.update(fm.facets)
    front["resource"] = [
        {"system": a.system, "native_id": a.native_id, "url": a.url}
        for a in fm.resources
    ]
    if fm.links:
        front["links"] = fm.links
    head = yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{head}\n---\n\n# {title}\n\n{body}\n"


def assemble(
    items: list[tuple[CanonicalDocument, str, str, str]],
    changeset: ChangeSet,
    existing_paths: frozenset[str] = frozenset(),
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
            resources=[doc.anchor],
            links=sorted(p for p in links if p in known),  # drop dangling (law 2)
            freshness=doc.anchor.retrieved_at,
        )
        concepts[path] = fm
        files[path] = _render(doc, fm, title=title, description=description, body=body)
        summary.sources_changed.append(doc.anchor)
    summary.claims_added = sorted(concept_path(x) for x in changeset.added)
    summary.claims_modified = sorted(concept_path(x) for x in changeset.modified)
    summary.claims_removed = sorted(changeset.removed)
    system = items[0][0].anchor.system if items else "source"
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
