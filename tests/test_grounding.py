from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.grounding import (
    SIDECAR_DIR,
    GroundingConfig,
    declared_ids,
    delete_sidecar,
    drifted,
    has_sidecars,
    load_grounding,
    problems_for,
    read_sidecar,
    resolve,
    write_sidecar,
)
from kbforge.mirror import load_all
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
    assert "not found" in notes[0] and "tombstoned" not in notes[0]


def test_tombstoned_target_is_dropped():
    owner = _doc("confluence:payments")
    dead = _doc("servicenow:SVC0042")
    dead.deleted = True
    got, notes = resolve(owner, ["servicenow:SVC0042"], _by_id(owner, dead), max_docs=5)
    assert got == [] and notes
    assert "servicenow:SVC0042" in notes[0] and "tombstoned" in notes[0]


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


def test_sidecar_round_trips(tmp_path: Path):
    write_sidecar(tmp_path, "confluence:payments", {"servicenow:SVC0042": "h1"})
    assert read_sidecar(tmp_path, "confluence:payments") == {"servicenow:SVC0042": "h1"}


def test_absent_sidecar_reads_as_none(tmp_path: Path):
    assert read_sidecar(tmp_path, "confluence:payments") is None


def test_delete_is_idempotent(tmp_path: Path):
    delete_sidecar(tmp_path, "confluence:payments")  # must not raise
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    delete_sidecar(tmp_path, "confluence:payments")
    assert read_sidecar(tmp_path, "confluence:payments") is None


def test_load_all_never_sees_a_sidecar(tmp_path: Path):
    """`load_all` globs `mirror/*.json`; the sidecar must stay in a subdirectory
    or every run would try to parse one as a CanonicalDocument."""
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    assert load_all(tmp_path) == []
    assert (tmp_path / SIDECAR_DIR).is_dir()


def test_has_sidecars_gates_the_scan(tmp_path: Path):
    assert has_sidecars(tmp_path) is False
    write_sidecar(tmp_path, "confluence:payments", {"a:b": "h"})
    assert has_sidecars(tmp_path) is True
    delete_sidecar(tmp_path, "confluence:payments")
    assert has_sidecars(tmp_path) is False


def test_unchanged_grounding_does_not_drift(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert (
        drifted(
            tmp_path,
            [owner],
            {"confluence:payments": ["servicenow:SVC0042"]},
            {"servicenow:SVC0042": "h1"},
        )
        == []
    )


def test_a_changed_grounding_hash_drifts(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path,
        [owner],
        {"confluence:payments": ["servicenow:SVC0042"]},
        {"servicenow:SVC0042": "h2"},  # the other system's run moved it
    ) == ["confluence:payments"]


def test_a_vanished_grounding_document_drifts(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(tmp_path, [owner], {"confluence:payments": []}, {}) == [
        "confluence:payments"
    ]


def test_an_edited_map_drifts_via_the_set_comparison(tmp_path: Path):
    owner = _doc("confluence:payments")
    write_sidecar(tmp_path, owner.doc_id, {"servicenow:SVC0042": "h1"})
    assert drifted(
        tmp_path,
        [owner],
        {"confluence:payments": ["servicenow:SVC0042", "drive:D1"]},
        {"servicenow:SVC0042": "h1", "drive:D1": "h9"},
    ) == ["confluence:payments"]


def test_a_document_that_never_grounded_does_not_drift(tmp_path: Path):
    assert drifted(tmp_path, [_doc("confluence:payments")], {}, {}) == []


def test_an_unresolvable_id_does_not_drift_forever(tmp_path: Path):
    """`resolved` is POST-resolution, so an id that never resolves is absent from
    both sides. Comparing DECLARED ids would leave it permanently present on one
    side and absent on the other -- re-synthesizing every run, forever."""
    owner = _doc("confluence:payments", grounded_by=["servicenow:NEVER"])
    write_sidecar(tmp_path, owner.doc_id, {"drive:D1": "h1"})
    assert (
        drifted(
            tmp_path, [owner], {"confluence:payments": ["drive:D1"]}, {"drive:D1": "h1"}
        )
        == []
    )
