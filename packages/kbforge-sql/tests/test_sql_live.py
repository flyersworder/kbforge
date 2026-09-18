"""Live test against a real network database. Skipped unless --run-live.

Set KBFORGE_SQL_LIVE_URL to a SQLAlchemy URL (PostgreSQL by default, with its
driver installed: `uv pip install psycopg[binary]`) and, optionally,
KBFORGE_SQL_LIVE_PASSWORD. KBFORGE_SQL_LIVE_QUERY, _ID and _TITLE point the test
at an existing read-only view instead -- that is how Denodo is live-tested from
inside a network that reaches it."""

from __future__ import annotations

import os

import pytest

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge_sql.connector import CONNECTOR

pytestmark = pytest.mark.live


@pytest.fixture
def cfg():
    if not os.environ.get("KBFORGE_SQL_LIVE_URL"):
        pytest.skip("KBFORGE_SQL_LIVE_URL is not set")
    cfg = {
        "system": "live",
        "url_env": "KBFORGE_SQL_LIVE_URL",
        "query": os.environ.get(
            "KBFORGE_SQL_LIVE_QUERY",
            # The LIKE '%s%' exercises the no_parameters fix (Important 2):
            # a pyformat driver (psycopg/psycopg2/pymysql) would otherwise try
            # to interpolate this literal `%` as a bind parameter.
            "SELECT table_schema || '.' || table_name AS id, table_name AS title, "
            "table_type FROM information_schema.tables "
            "WHERE table_schema = 'information_schema' AND table_name LIKE '%s%'",
        ),
        "id": [os.environ.get("KBFORGE_SQL_LIVE_ID", "id")],
        "title": os.environ.get("KBFORGE_SQL_LIVE_TITLE", "title"),
    }
    if os.environ.get("KBFORGE_SQL_LIVE_PASSWORD"):
        cfg["password_env"] = "KBFORGE_SQL_LIVE_PASSWORD"
    return cfg


def test_a_real_database_round_trips(cfg):
    assert CONNECTOR.kbforge_validate_config(cfg) == []
    result = CONNECTOR.kbforge_fetch(cfg, None)
    assert result.records, "the live query returned no rows"
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)
    assert_fetch_contract(docs, complete=True)


def test_two_fetches_hash_identically(cfg):
    # The real §4.3 law 1 test for a live source: nothing volatile leaks in.
    a = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(cfg, None).records)
    b = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(cfg, None).records)
    assert [d.anchor.content_hash for d in a] == [d.anchor.content_hash for d in b]
