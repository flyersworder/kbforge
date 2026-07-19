from datetime import UTC, datetime

from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor
from kbforge.synthesize import concept_path, synthesize

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _doc(doc_id, structured=None, relations=None):
    native = doc_id.split(":", 1)[1]
    anchor = ResourceAnchor(
        system="local_files", native_id=native, retrieved_at=NOW, content_hash="h"
    )
    return CanonicalDocument(
        anchor=anchor,
        doc_id=doc_id,
        title=native,
        text="body",
        structured=structured or {},
        relations=relations or [],
    )


def test_synthesizes_a_conformant_concept():
    doc = _doc("local_files:apps/x.md", structured={"owner": "team-a"})
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    path = concept_path("local_files:apps/x.md")
    assert path in change.files and path in change.concepts
    fm = change.concepts[path]
    assert fm.type == "concept"
    assert fm.facets == {"owner": "team-a"}
    assert fm.resources == [doc.anchor]
    assert fm.freshness == NOW
    assert fm.links == []  # no relations declared → no links
    assert change.files[path].startswith("---\n")  # rendered with YAML frontmatter
    # full strict-OKF + §4.4 conformance of synthesized output is proven end-to-end
    # by the pipeline test (Task 8): a Published result means run_validators == [].


def test_dangling_relations_are_dropped():
    doc = _doc("local_files:apps/x.md", relations=["local_files:apps/ghost.md"])
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == []  # ghost not in the bundle → dropped, not dangling


def test_resolvable_sibling_link_survives():
    x = _doc("local_files:apps/x.md", relations=["local_files:apps/y.md"])
    y = _doc("local_files:apps/y.md")
    change = synthesize(
        [x, y], ChangeSet(added=["local_files:apps/x.md", "local_files:apps/y.md"])
    )
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == [concept_path("local_files:apps/y.md")]


def test_nested_structured_value_is_not_a_facet():
    doc = _doc(
        "local_files:apps/x.md", structured={"owner": {"team": "a"}, "env": "prod"}
    )
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.facets == {"env": "prod"}  # nested dropped → law 1 stays well-formed
