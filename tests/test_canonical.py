from datetime import UTC, datetime

import pytest

from kbforge.canonical import StabilityError, assert_stability, content_hash
from kbforge.models import CanonicalDocument, RawRecord, ResourceAnchor


def _doc(text="body", retrieved_at=datetime(2026, 7, 19, tzinfo=UTC)):
    anchor = ResourceAnchor(
        system="s", native_id="n", retrieved_at=retrieved_at, content_hash="ignored"
    )
    return CanonicalDocument(anchor=anchor, doc_id="s:n", title="T", text=text)


def test_content_hash_excludes_anchor_volatility():
    a = _doc(retrieved_at=datetime(2026, 7, 19, tzinfo=UTC))
    b = _doc(retrieved_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert content_hash(a) == content_hash(b)  # retrieved_at must not affect it


def test_content_hash_reacts_to_content():
    assert content_hash(_doc("one")) != content_hash(_doc("two"))


def test_assert_stability_passes_for_pure_normalize():
    def normalize(records):
        return [_doc()]

    assert assert_stability(normalize, [RawRecord(media_type="x", payload=b"")]) is None


def test_assert_stability_raises_for_unstable_normalize():
    calls = {"n": 0}

    def flaky(records):
        calls["n"] += 1
        return [_doc(text=f"body-{calls['n']}")]

    with pytest.raises(StabilityError):
        assert_stability(flaky, [RawRecord(media_type="x", payload=b"")])
