from datetime import UTC, datetime

from kbforge.models import (
    CanonicalDocument,
    ChangeSet,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _anchor():
    return ResourceAnchor(
        system="local_files", native_id="apps/x", retrieved_at=NOW, content_hash="h"
    )


def test_changeset_is_noop_when_empty():
    assert ChangeSet().is_noop is True
    assert ChangeSet(unchanged_count=5).is_noop is True


def test_changeset_not_noop_with_any_change():
    assert ChangeSet(added=["a"]).is_noop is False
    assert ChangeSet(modified=["b"]).is_noop is False
    assert ChangeSet(removed=["c"]).is_noop is False


def test_fetchresult_defaults_complete_true():
    fr = FetchResult(cursor=Cursor(connector="local_files"))
    assert fr.complete is True
    assert fr.records == []


def test_canonical_document_defaults():
    doc = CanonicalDocument(
        anchor=_anchor(), doc_id="local_files:apps/x", title="X", text="body"
    )
    assert doc.deleted is False
    assert doc.structured == {}
    assert doc.relations == []


def test_connector_and_raw_record():
    info = ConnectorInfo(
        name="local_files", version="0.1.0", source_system="local filesystem"
    )
    assert info.info_types == []
    rec = RawRecord(media_type="text/markdown", payload=b"# X")
    assert rec.anchor_hint == {}
