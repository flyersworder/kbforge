from pathlib import Path

import pytest
from sql_testdb import execute, flat_cfg, make_db

from kbforge.models import Cursor
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge_sql.connector import CONNECTOR, TOMBSTONE
from kbforge_sql.errors import SqlSourceError


def _run(tmp_path: Path, cfg: dict):
    return run(
        CONNECTOR,
        DryRunPublisher(),
        config=cfg,
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )


def _concept(tmp_path: Path, native_id: str) -> Path:
    return tmp_path / "out" / "sync-products" / "concepts" / native_id / "overview.md"


def _prior(*ids: str) -> Cursor:
    return Cursor(connector="sql", payload={"ids": list(ids)})


def test_a_removed_row_is_tombstoned_and_its_concept_removed(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert _concept(tmp_path, "XDP1").exists()

    execute(db, "DELETE FROM product WHERE product_id = 'XDP1';")
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert not _concept(tmp_path, "XDP1").exists()
    assert _concept(tmp_path, "TLE9").exists()


def test_an_unchanged_table_is_a_noop(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    assert isinstance(_run(tmp_path, flat_cfg()), NoOp)


def test_the_manifest_round_trips_through_the_pipeline_under_sql(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    slots = list((tmp_path / "state").glob("cursor-sql-*.json"))
    assert len(slots) == 1  # saved under the same name run() loads it by
    assert Cursor.model_validate_json(slots[0].read_text()).payload == {
        "ids": ["IMC300", "TLE9", "XDP1"]
    }


def test_a_row_leaving_the_query_scope_is_tombstoned(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(
        flat_cfg(), _prior("IMC300", "TLE9", "XDP1", "GONE")
    )
    tombstones = [r for r in result.records if r.media_type == TOMBSTONE]
    assert [r.anchor_hint["native_id"] for r in tombstones] == ["GONE"]
    docs = CONNECTOR.kbforge_normalize(tombstones)
    assert docs[0].deleted is True
    assert docs[0].doc_id == "products:GONE"
    assert result.complete is True


def test_an_empty_result_never_deletes_everything(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "DELETE FROM product;")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(
            flat_cfg(max_removed_fraction=1.0), _prior("IMC300", "TLE9", "XDP1")
        )
    assert str(exc.value) == (
        "sql source 'products': the query returned no rows, but the last "
        "published run saw 3; refusing to delete every concept. If the source "
        "is meant to be empty, remove its config instead"
    )


def test_an_empty_first_run_is_not_an_error(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "DELETE FROM product;")
    assert CONNECTOR.kbforge_fetch(flat_cfg(), None).records == []


def test_the_deletion_ceiling_stops_a_mass_removal(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C", "D")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), prior)
    assert str(exc.value) == (
        "sql source 'products': 4 of 7 previously seen ids (57%) are missing, "
        "above max_removed_fraction=0.5; raise it for a deliberate cleanup"
    )


def test_the_ceiling_is_inclusive(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C")  # 3 of 6 = exactly 0.5
    result = CONNECTOR.kbforge_fetch(flat_cfg(), prior)
    assert len([r for r in result.records if r.media_type == TOMBSTONE]) == 3


def test_a_raised_ceiling_permits_the_cleanup(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    prior = _prior("IMC300", "TLE9", "XDP1", "A", "B", "C", "D")
    result = CONNECTOR.kbforge_fetch(flat_cfg(max_removed_fraction=1.0), prior)
    assert len([r for r in result.records if r.media_type == TOMBSTONE]) == 4


def test_an_aborted_run_re_emits_its_tombstone(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    _run(tmp_path, flat_cfg())
    execute(db, "DELETE FROM product WHERE product_id = 'XDP1';")

    class Boom(Exception):
        pass

    class FailingPublisher(DryRunPublisher):
        def kbforge_publish(self, change, config):
            raise Boom

    with pytest.raises(Boom):
        run(
            CONNECTOR,
            FailingPublisher(),
            config=flat_cfg(),
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={"out_dir": str(tmp_path / "out")},
        )
    # The cursor was not saved, so the manifest still holds XDP1 and the next
    # run tombstones it again (at-least-once, architecture §4.2).
    assert isinstance(_run(tmp_path, flat_cfg()), Published)
    assert not _concept(tmp_path, "XDP1").exists()
