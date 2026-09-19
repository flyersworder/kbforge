from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig, admit, load_chunking
from kbforge.models import CanonicalDocument, ResourceAnchor


def _doc(native_id: str, group: str | None = None) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system="sys",
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"sys:{native_id}",
        title=native_id,
        text=native_id,
        structured={} if group is None else {"category": group},
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


def _ids(docs: list[CanonicalDocument]) -> list[str]:
    return [d.doc_id for d in docs]


def test_under_the_cap_everything_is_admitted():
    chunk, left = admit([_doc("b"), _doc("a")], ChunkingConfig(max_concepts=5), 5)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is False


def test_without_group_by_the_chunk_is_the_lowest_doc_ids():
    docs = [_doc(n) for n in "edcba"]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=2), 2)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is True


def test_whole_groups_are_packed_in_key_order():
    docs = [
        _doc("a", "runbook"),
        _doc("b", "service"),
        _doc("c", "runbook"),
        _doc("d", "service"),
    ]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=3, group_by="category"), 3)
    # runbook (2) fits; service (2) would overflow 3, so it waits whole.
    assert _ids(chunk) == ["sys:a", "sys:c"]
    assert left is True


def test_a_group_larger_than_the_cap_is_split_by_doc_id():
    docs = [_doc(n, "big") for n in "cba"]
    chunk, left = admit(docs, ChunkingConfig(max_concepts=2, group_by="category"), 2)
    assert _ids(chunk) == ["sys:a", "sys:b"]
    assert left is True


def test_documents_without_the_group_key_come_last():
    docs = [_doc("a"), _doc("z", "runbook")]
    chunk, _ = admit(docs, ChunkingConfig(max_concepts=1, group_by="category"), 1)
    assert _ids(chunk) == ["sys:z"]


def test_zero_capacity_admits_nothing():
    chunk, left = admit([_doc("a")], ChunkingConfig(max_concepts=1), 0)
    assert chunk == []
    assert left is True


def test_admission_does_not_depend_on_input_order():
    docs = [_doc(n, g) for n, g in [("a", "x"), ("b", "y"), ("c", "x"), ("d", "y")]]
    cfg = ChunkingConfig(max_concepts=2, group_by="category")
    assert _ids(admit(docs, cfg, 2)[0]) == _ids(admit(docs[::-1], cfg, 2)[0])


def test_config_rejects_an_unknown_key(tmp_path: Path):
    path = tmp_path / "chunking.yaml"
    path.write_text("max_concepts: 3\nmax_concept: 4\n", "utf-8")
    with pytest.raises(
        ValidationError, match=r"max_concept\s+Extra inputs are not permitted"
    ):
        load_chunking(path)


def test_config_rejects_a_cap_below_one():
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ChunkingConfig(max_concepts=0)


def test_no_path_means_no_chunking():
    assert load_chunking(None) is None
