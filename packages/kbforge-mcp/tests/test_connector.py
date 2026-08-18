from __future__ import annotations

from datetime import datetime

import pytest

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge_mcp.connector import CONNECTOR
from tests.fake_server import mcp as fixture_server

CONFIG = {
    "system": "fixture",
    "transport": {"kind": "stdio", "command": "unused"},
    "select": {
        "tool": "search_docs",
        "args": {"query": "*"},
        "ids": {"list": "results", "id": "path", "title": "title"},
    },
    "read": {"tool": "read_doc", "id_arg": "path"},
}


@pytest.fixture
def cfg(monkeypatch):
    """Point the connector at the in-process fixture server."""
    from kbforge_mcp import client as mod

    monkeypatch.setattr(mod, "_server_override", fixture_server)
    return dict(CONFIG)


def test_fetch_then_normalize_produces_citable_documents(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == [
        "fixture:docs/onboarding",
        "fixture:docs/retention",
    ]
    assert "How to get started" in docs[0].text
    assert docs[0].anchor.system == "fixture"
    assert_fetch_contract(docs, complete=result.complete)


def test_normalize_is_stable_and_clock_free(cfg, monkeypatch):
    # assert_stability CANNOT catch a clock in normalize: content_hash excludes
    # the anchor by design, so both passes hash identically. Compare the anchors.
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)

    first = CONNECTOR.kbforge_normalize(result.records)
    import kbforge_mcp.connector as mod

    class FrozenElsewhere:
        # normalize must not call now(); it MUST still call fromisoformat, so the
        # shim delegates that one. A shim without it fails on AttributeError and
        # would pass for the wrong reason. `tz` is accepted, never used, only so
        # this matches `_fetch`'s real call shape (`datetime.now(tz=UTC)`) --
        # without it, a regression that copies fetch's clock call into normalize
        # would raise TypeError on the keyword argument instead of the intended
        # AssertionError, which would still fail the test but for the wrong reason.
        @staticmethod
        def now(tz=None):
            raise AssertionError("normalize called the clock (architecture 4.3)")

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(mod, "datetime", FrozenElsewhere)
    second = CONNECTOR.kbforge_normalize(result.records)
    assert [d.anchor.retrieved_at for d in first] == [
        d.anchor.retrieved_at for d in second
    ]


def test_retrieved_at_is_stamped_in_fetch_not_normalize(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert all("retrieved_at" in r.anchor_hint for r in result.records)


def test_a_failed_read_degrades_complete_rather_than_dropping_silently(cfg):
    # Skipping a document while still claiming complete=True would, once the
    # 0.8.0 manifest lands, manufacture a deletion out of a transient error.
    # `docs/missing.md` makes the fixture's read_doc raise -> ToolCallFailed.
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/missing.md"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]
    # static_ids is complete by construction -- the failed read is what
    # downgrades it, and that downgrade is the whole point.
    assert result.complete is False


def test_a_prose_only_selector_fails_closed(cfg):
    cfg["select"] = {"tool": "outline", "args": {"query": "*"}}
    with pytest.raises(Exception, match="static_ids"):
        CONNECTOR.kbforge_fetch(cfg, None)


def test_a_query_selector_never_reports_complete(cfg):
    # This is what makes an empty select result safe: `refs_from_select` may
    # legally return [], and zero documents with complete=True would manufacture
    # a corpus-wide deletion once the 0.8.0 manifest lands. A query selector saw
    # only what the server chose to return, so it can never claim completeness --
    # and `assert_fetch_contract` refuses a tombstone under complete=False.
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert result.complete is False


def test_static_ids_need_no_select_call(cfg):
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]


def test_slug_collision_is_caught_by_the_0_6_0_fetch_side_law(cfg):
    # Stripping the extension turns `policy` and `policy.md` into one slug, which
    # makes them one doc_id -- converting a silent concept_path collapse into a
    # loud FetchContractError. The law already catches this; prove it does.
    from kbforge.canonical import FetchContractError

    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/retention"]
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    with pytest.raises(FetchContractError, match="duplicate doc_id"):
        assert_fetch_contract(docs, complete=result.complete)
