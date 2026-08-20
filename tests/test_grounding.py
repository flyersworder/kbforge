from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.grounding import (
    GroundingConfig,
    declared_ids,
    load_grounding,
    problems_for,
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
