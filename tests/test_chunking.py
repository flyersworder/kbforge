from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.canonical import content_hash
from kbforge.chunking import (
    ChunkingConfig,
    ChunkRecord,
    ChunkRecordError,
    admit,
    load_chunking,
    merge_records,
    owned_paths,
    read_record,
    restore,
    snapshot,
    write_record,
)
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


def test_owned_paths_are_where_the_three_writers_actually_write(tmp_path: Path):
    """Guards drift: if the mirror, the sidecar or the first-seen writer ever
    renames its file, redo would silently restore the wrong path."""
    from kbforge.grounding import record_first_seen, write_sidecar
    from kbforge.links import write_links
    from kbforge.mirror import commit

    mirror = tmp_path / "mirror"
    doc = _doc("a")
    commit(mirror, [doc])
    write_sidecar(mirror, doc.doc_id, {})
    record_first_seen(mirror, [doc])
    write_links(mirror, doc.doc_id, [])
    written = {p.relative_to(mirror).as_posix() for p in mirror.rglob("*.json")}
    # Written files should be a subset of owned_paths (described is added later).
    assert written <= set(owned_paths(doc.doc_id))


def test_snapshot_keeps_present_files_verbatim_and_absent_ones_as_none(tmp_path):
    mirror = tmp_path / "mirror"
    slot, sidecar, first_seen, described, links = owned_paths("sys:a")
    mirror.mkdir()
    (mirror / slot).write_text("OLD", "utf-8")
    assert snapshot(mirror, {"sys:a"}) == {
        slot: "OLD",
        sidecar: None,
        first_seen: None,
        described: None,
        links: None,
    }


def test_restore_puts_contents_back_and_removes_what_did_not_exist(tmp_path):
    mirror = tmp_path / "mirror"
    cursor = tmp_path / "state" / "cursor-fake-0.json"
    slot, sidecar, _, _, _ = owned_paths("sys:a")
    (mirror / "_grounding").mkdir(parents=True)
    (mirror / slot).write_text("NEW", "utf-8")
    (mirror / sidecar).write_text("NEW", "utf-8")
    cursor.parent.mkdir()
    cursor.write_text("C2", "utf-8")
    record = ChunkRecord(
        branch_hints=["sync/sys"],
        pending=False,
        admitted=["sys:a"],
        mirror={slot: "OLD", sidecar: None},
        cursor=None,
    )
    restore(record, mirror, cursor)
    assert (mirror / slot).read_text("utf-8") == "OLD"
    assert not (mirror / sidecar).exists()
    assert not cursor.exists()


def test_a_record_round_trips(tmp_path: Path):
    path = tmp_path / "state" / "chunk-fake-0.json"
    record = ChunkRecord(
        branch_hints=["sync/sys"],
        pending=True,
        admitted=["sys:a"],
        mirror={"k.json": None},
        cursor="{}",
    )
    write_record(path, record)
    assert read_record(path) == record


def test_no_record_reads_as_none(tmp_path: Path):
    assert read_record(tmp_path / "nope.json") is None


def _rec(**kw) -> ChunkRecord:
    base = dict(
        branch_hints=["sync/sys"], pending=False, admitted=[], mirror={}, cursor=None
    )
    return ChunkRecord.model_validate(base | kw)


def test_merging_keeps_the_earliest_prior_state_of_every_path():
    older = _rec(admitted=["sys:a"], mirror={"a.json": None, "c.json": "C0"})
    newer = _rec(admitted=["sys:b"], mirror={"a.json": "A1", "b.json": None})
    merged = merge_records(older, newer)
    assert merged.mirror == {"a.json": None, "b.json": None, "c.json": "C0"}, (
        "a path the older run touched must restore to its state before that run"
    )


def test_merging_keeps_the_older_cursor_and_the_newer_pending_flag():
    older = _rec(cursor="C0", pending=False)
    newer = _rec(cursor="C1", pending=True)
    merged = merge_records(older, newer)
    assert merged.cursor == "C0", "redo must rewind the cursor to before the first run"
    assert merged.pending is True, "whether a backlog remains is the newer run's"


def test_merging_unions_admitted_sorted_and_branch_hints_in_order():
    older = _rec(branch_hints=["sync/b", "sync/a"], admitted=["sys:c", "sys:a"])
    newer = _rec(branch_hints=["sync/a", "sync/z"], admitted=["sys:b", "sys:a"])
    merged = merge_records(older, newer)
    assert merged.admitted == ["sys:a", "sys:b", "sys:c"]
    assert merged.branch_hints == ["sync/b", "sync/a", "sync/z"]


@pytest.mark.parametrize(
    "garbage",
    [b'{"branch_hints": ["sync/', b'{"pending": true}', b"\xff\xfe"],
    ids=["torn", "wrong-shape", "not-utf8"],
)
def test_an_unreadable_record_names_its_path(tmp_path: Path, garbage: bytes):
    path = tmp_path / "chunk-fake-0.json"
    path.write_bytes(garbage)
    with pytest.raises(ChunkRecordError) as caught:
        read_record(path)
    assert caught.value.path == path
    assert str(caught.value).startswith(f"chunk record {path}: ")


def test_owned_paths_cover_the_described_sidecar():
    from kbforge.chunking import owned_paths
    from kbforge.mirror import slot_key

    assert f"_described/{slot_key('sys:x.md')}.json" in owned_paths("sys:x.md")
