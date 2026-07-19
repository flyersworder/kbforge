from datetime import UTC, datetime
from pathlib import Path

from kbforge.mirror import commit, diff
from kbforge.models import CanonicalDocument, ResourceAnchor

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _doc(doc_id="s:a", chash="h1", deleted=False):
    anchor = ResourceAnchor(
        system="s", native_id=doc_id.split(":")[1], retrieved_at=NOW, content_hash=chash
    )
    return CanonicalDocument(
        anchor=anchor, doc_id=doc_id, title="T", text="body", deleted=deleted
    )


def test_all_new_docs_are_added(tmp_path: Path):
    cs = diff(tmp_path, [_doc("s:a"), _doc("s:b")])
    assert cs.added == ["s:a", "s:b"]
    assert cs.is_noop is False


def test_diff_is_read_only(tmp_path: Path):
    diff(tmp_path, [_doc("s:a")])
    assert list(tmp_path.iterdir()) == []  # diff must NOT write


def test_committed_docs_are_unchanged_next_run(tmp_path: Path):
    docs = [_doc("s:a", "h1")]
    commit(tmp_path, docs)
    cs = diff(tmp_path, docs)
    assert cs.is_noop is True
    assert cs.unchanged_count == 1


def test_content_change_is_modified(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    cs = diff(tmp_path, [_doc("s:a", "h2")])
    assert cs.modified == ["s:a"]


def test_tombstone_removes_only_if_present(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    cs = diff(tmp_path, [_doc("s:a", deleted=True)])
    assert cs.removed == ["s:a"]
    # a tombstone for an unknown doc is not a removal
    cs2 = diff(tmp_path, [_doc("s:ghost", deleted=True)])
    assert cs2.removed == []


def test_commit_deletes_tombstoned(tmp_path: Path):
    commit(tmp_path, [_doc("s:a", "h1")])
    commit(tmp_path, [_doc("s:a", deleted=True)])
    assert diff(tmp_path, [_doc("s:a", "h1")]).added == ["s:a"]  # gone from mirror
