import json

import pytest
from pydantic import ValidationError

from kbforge.sources_record import SourceEntry, read_sources, write_sources


def test_missing_file_reads_as_empty(tmp_path):
    assert read_sources(tmp_path / "state") == {}


def test_round_trip_keeps_order_and_values(tmp_path):
    state = tmp_path / "state"
    entries = {
        "products": SourceEntry(
            connector="sql",
            digest="0123abcd",
            systems=["products"],
            last_published="2026-10-29T12:00:00+00:00",
        ),
        "handbook": SourceEntry(
            connector="local_files", digest="feedbeef", systems=["local_files"]
        ),
    }
    write_sources(state, entries)
    assert read_sources(state) == entries
    assert list(read_sources(state)) == ["products", "handbook"]
    raw = json.loads((state / "sources.json").read_text("utf-8"))
    assert raw["handbook"]["last_published"] is None


def test_write_leaves_no_temp_file_behind(tmp_path):
    state = tmp_path / "state"
    write_sources(state, {"a": SourceEntry(connector="c", digest="d", systems=[])})
    assert [p.name for p in state.iterdir()] == ["sources.json"]


def test_unknown_keys_are_rejected(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "sources.json").write_text(
        json.dumps({"a": {"connector": "c", "digest": "d", "systems": [], "x": 1}}),
        "utf-8",
    )
    with pytest.raises(ValidationError, match="x"):
        read_sources(state)
