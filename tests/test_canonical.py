from datetime import UTC, datetime

import pytest

from kbforge.canonical import (
    FetchContractError,
    StabilityError,
    assert_fetch_contract,
    assert_stability,
    content_hash,
)
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


def _fdoc(doc_id="sys:a.md", native_id="a.md", deleted=False):
    """A minimal CanonicalDocument for fetch-contract tests."""
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system="sys",
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="h",
        ),
        doc_id=doc_id,
        title="A",
        text="A",
        deleted=deleted,
    )


def test_fetch_contract_accepts_a_well_formed_complete_fetch():
    assert_fetch_contract(
        [_fdoc("sys:a.md", "a.md"), _fdoc("sys:b.md", "b.md")], complete=True
    )


def test_fetch_contract_rejects_a_duplicate_doc_id():
    """Two records sharing an id both land in ChangeSet.added, then assemble
    collapses them onto one concept_path with last-write-wins: one document is
    silently absent from the KB and nothing downstream looks broken."""
    docs = [_fdoc("sys:a.md", "a.md"), _fdoc("sys:a.md", "a.md")]
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract(docs, complete=True)
    assert str(exc.value) == "duplicate doc_id in fetch output: sys:a.md"


def test_fetch_contract_rejects_a_blank_native_id():
    """The fetch-side mirror of the §4.4 anchor-presence law: a record with no
    native_id cannot be cited, so a reviewer cannot follow it to its source."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "")], complete=True)
    assert str(exc.value) == "record has no native_id: doc_id=sys:a.md"


def test_fetch_contract_rejects_a_zero_width_native_id():
    """Blankness is judged on visible content, not str.strip(): U+200B is `Cf`,
    survives strip(), and is no more citable than an empty string."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "​")], complete=True)
    assert str(exc.value) == "record has no native_id: doc_id=sys:a.md"


def test_fetch_contract_rejects_a_tombstone_from_an_incomplete_fetch():
    """complete=False means the connector saw a partial slice of the source, so
    absence is not evidence of deletion. This is the check that makes
    FetchResult.complete load-bearing rather than decorative."""
    with pytest.raises(FetchContractError) as exc:
        assert_fetch_contract([_fdoc("sys:a.md", "a.md", deleted=True)], complete=False)
    assert str(exc.value) == "incomplete fetch cannot emit a tombstone: sys:a.md"


def test_fetch_contract_allows_a_tombstone_from_a_complete_fetch():
    assert_fetch_contract([_fdoc("sys:a.md", "a.md", deleted=True)], complete=True)
