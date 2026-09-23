from pathlib import Path

import pytest

from kbforge.described import (
    DESCRIBED_DIR,
    delete_described,
    read_described,
    write_described,
)
from kbforge.mirror import slot_key
from kbforge.models import DescribedRecord

REC = DescribedRecord(doc_id="sys:x.md", content_hash="h1", actor="kbforge/m",
                      description="One sentence.", tags=["sic"])  # fmt: skip


def test_round_trip(tmp_path: Path):
    write_described(tmp_path, REC)
    assert read_described(tmp_path, "sys:x.md") == REC
    assert (tmp_path / DESCRIBED_DIR / f"{slot_key('sys:x.md')}.json").exists()


def test_absent_is_none(tmp_path: Path):
    assert read_described(tmp_path / "no-mirror-yet", "sys:x.md") is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"doc_id": "sys:x.md"}',
        "[]",
        '{"doc_id": 1, "tags": "x"}',
        "\udcff",
    ],
)
def test_an_unreadable_record_is_a_miss_not_an_error(tmp_path: Path, content: str):
    path = tmp_path / DESCRIBED_DIR / f"{slot_key('sys:x.md')}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("utf-8", "surrogateescape"))
    assert read_described(tmp_path, "sys:x.md") is None


def test_delete_is_idempotent(tmp_path: Path):
    write_described(tmp_path, REC)
    delete_described(tmp_path, "sys:x.md")
    delete_described(tmp_path, "sys:x.md")
    assert read_described(tmp_path, "sys:x.md") is None
