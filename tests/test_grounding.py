from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    load_grounding,
    problems_for,
    resolve,
)
from kbforge.models import CanonicalDocument, ResourceAnchor


def _doc(doc_id="confluence:payments", grounded_by=None):
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=doc_id.partition(":")[0],
            native_id=doc_id.partition(":")[2],
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash="h",
        ),
        doc_id=doc_id,
        title="T",
        text="body",
        grounded_by=grounded_by or [],
    )


def test_absent_path_is_an_empty_config():
    cfg = load_grounding(None)
    assert cfg.grounding == {} and cfg.max_grounding_docs == 5


def test_map_is_loaded(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text(
        "max_grounding_docs: 2\n"
        "grounding:\n"
        "  confluence:payments:\n"
        "    - servicenow:SVC0042\n",
        "utf-8",
    )
    cfg = load_grounding(p)
    assert cfg.max_grounding_docs == 2
    assert cfg.grounding == {"confluence:payments": ["servicenow:SVC0042"]}


def test_unknown_key_is_refused(tmp_path: Path):
    p = tmp_path / "g.yaml"
    p.write_text("groundings: {}\n", "utf-8")  # typo'd key
    with pytest.raises(Exception):
        load_grounding(p)


@pytest.mark.parametrize(
    "cfg, fragment",
    [
        (GroundingConfig(grounding={"payments": ["servicenow:SVC0042"]}), "key"),
        (GroundingConfig(grounding={"confluence:payments": ["SVC0042"]}), "value"),
        (GroundingConfig(grounding={"confluence:payments": [":SVC0042"]}), "value"),
        (GroundingConfig(max_grounding_docs=0), "max_grounding_docs"),
    ],
)
def test_shape_problems_are_reported(cfg, fragment):
    problems = problems_for(cfg)
    assert problems and any(fragment in p for p in problems)


def test_a_valid_config_has_no_problems():
    cfg = GroundingConfig(grounding={"confluence:payments": ["servicenow:SVC0042"]})
    assert problems_for(cfg) == []


def test_declared_ids_unions_both_sites_and_dedupes():
    """Spec §2: two declaration sites, one consumption path."""
    doc = _doc(grounded_by=["servicenow:SVC0042", "mcp-aws:x"])
    cfg = GroundingConfig(
        grounding={"confluence:payments": ["servicenow:SVC0042", "drive:D1"]}
    )
    assert declared_ids(doc, cfg) == ["drive:D1", "mcp-aws:x", "servicenow:SVC0042"]


def _by_id(*docs):
    return {d.doc_id: d for d in docs}


def test_resolution_keeps_declared_documents_sorted():
    owner = _doc("confluence:payments")
    a, b = _doc("servicenow:SVC0042"), _doc("drive:D1")
    got, notes = resolve(
        owner, ["servicenow:SVC0042", "drive:D1"], _by_id(a, b), max_docs=5
    )
    assert [d.doc_id for d in got] == ["drive:D1", "servicenow:SVC0042"]
    assert notes == []


def test_self_reference_is_dropped_silently():
    owner = _doc("confluence:payments")
    got, notes = resolve(owner, ["confluence:payments"], _by_id(owner), max_docs=5)
    assert got == [] and notes == []


def test_unresolvable_id_is_dropped_with_a_note_not_an_error():
    """A grounding target may live in a system that has not synced yet. Failing
    would make one source's sync depend on another's."""
    owner = _doc("confluence:payments")
    got, notes = resolve(owner, ["servicenow:SVC0042"], _by_id(owner), max_docs=5)
    assert got == []
    assert notes and "servicenow:SVC0042" in notes[0]


def test_tombstoned_target_is_dropped():
    owner = _doc("confluence:payments")
    dead = _doc("servicenow:SVC0042")
    dead.deleted = True
    got, notes = resolve(owner, ["servicenow:SVC0042"], _by_id(owner, dead), max_docs=5)
    assert got == [] and notes


def test_duplicate_resource_collapses_even_across_different_doc_ids():
    """Dedup keys on the resource, not the doc_id: two systems can carry the same
    url, and `sources` is compared as a set of resources."""
    owner = _doc("confluence:payments")
    a, b = _doc("servicenow:SVC0042"), _doc("drive:D1")
    a.anchor.url = b.anchor.url = "https://example.test/same"
    got, _ = resolve(
        owner, ["servicenow:SVC0042", "drive:D1"], _by_id(a, b), max_docs=5
    )
    assert len(got) == 1


def test_a_grounding_doc_sharing_the_owners_resource_is_dropped():
    owner = _doc("confluence:payments")
    twin = _doc("drive:D1")
    owner.anchor.url = twin.anchor.url = "https://example.test/same"
    got, _ = resolve(owner, ["drive:D1"], _by_id(owner, twin), max_docs=5)
    assert got == []


def test_cap_truncates_deterministically_and_notes_what_it_dropped():
    owner = _doc("confluence:payments")
    docs = [_doc(f"servicenow:SVC{i}") for i in range(4)]
    got, notes = resolve(owner, [d.doc_id for d in docs], _by_id(*docs), max_docs=2)
    assert [d.doc_id for d in got] == ["servicenow:SVC0", "servicenow:SVC1"]
    assert notes and "servicenow:SVC2" in notes[0] and "servicenow:SVC3" in notes[0]
