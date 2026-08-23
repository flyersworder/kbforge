from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from okfquery import EmptyMirrorError, load

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN = FIXTURES / "clean"
MESSY = FIXTURES / "messy"


def test_clean_bundle_loads_one_concept():
    con = load(CLEAN)
    assert con.execute("select count(*) from concepts").fetchone()[0] == 1


def test_empty_bundle_answers_instead_of_erroring(tmp_path):
    # Explicit DDL, not an inferred schema: an empty bundle must still have
    # queryable tables.
    (tmp_path / "concepts").mkdir()
    con = load(tmp_path)
    for table in ("concepts", "sources", "links", "problems"):
        assert con.execute(f"select count(*) from {table}").fetchone()[0] == 0


def test_generated_at_is_timestamptz_and_keeps_the_offset():
    # The messy bundle's ok/overview.md stamps +09:00. `load` binds an aware
    # Python datetime through executemany, so the instant survives a naive
    # TIMESTAMP column fine -- what TIMESTAMPTZ buys here is the *type*: a
    # naive column would come back with no tzinfo and assert UTC by
    # convention, with nothing recording that it does.
    con = load(MESSY)
    got = con.execute(
        "select generated_at from concepts where path = 'concepts/ok/overview.md'"
    ).fetchone()[0]
    assert got == datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


def test_generated_at_column_type_is_timestamptz():
    con = load(CLEAN)
    typ = con.execute(
        "select data_type from information_schema.columns "
        "where table_name = 'concepts' and column_name = 'generated_at'"
    ).fetchone()[0]
    assert typ == "TIMESTAMP WITH TIME ZONE"


def test_no_silent_drops_invariant():
    # Nine .md files, two reserved-and-fenceless. Stating this over ALL
    # scanned files would make it false and the mutation check meaningless.
    files = list((MESSY / "concepts").rglob("*.md"))
    assert len(files) == 9
    con = load(MESSY)
    assert con.execute("select count(*) from concepts").fetchone()[0] == 7


def test_fenced_reserved_name_is_loaded_as_a_concept():
    # The drift guard. If the reserved rule ever exempts index.md
    # unconditionally, this row disappears and every count is quietly wrong.
    con = load(MESSY)
    assert (
        con.execute(
            "select count(*) from concepts where path = 'concepts/claims/index.md'"
        ).fetchone()[0]
        == 1
    )


def test_fenceless_reserved_files_are_absent():
    con = load(MESSY)
    rows = con.execute(
        "select count(*) from concepts where path in "
        "('concepts/index.md', 'concepts/log.md')"
    ).fetchone()[0]
    assert rows == 0


def test_broken_file_still_gets_a_concept_row_with_nulls():
    con = load(MESSY)
    row = con.execute(
        "select type, title, generated_at from concepts "
        "where path = 'concepts/badyaml/overview.md'"
    ).fetchone()
    assert row == (None, None, None)


def test_ordinal_preserves_owner_then_grounding():
    con = load(MESSY)
    rows = con.execute(
        "select ordinal, id from sources where path = 'concepts/ok/overview.md' "
        "order by ordinal"
    ).fetchall()
    assert rows == [(0, "wiki:ok"), (1, "notes:ok")]


def test_links_are_long_form():
    con = load(MESSY)
    assert con.execute(
        "select target from links where path = 'concepts/ok/overview.md'"
    ).fetchall() == [("concepts/ok/overview.md",)]


def test_facets_are_json_and_exclude_okf_keys():
    con = load(MESSY)
    raw = con.execute(
        "select facets from concepts where path = 'concepts/ok/overview.md'"
    ).fetchone()[0]
    assert json.loads(raw) == {"owner": "platform"}


def test_facet_keys_are_discoverable():
    con = load(MESSY)
    keys = con.execute(
        "select distinct unnest(json_keys(facets)) from concepts "
        "where facets is not null"
    ).fetchall()
    assert ("owner",) in keys


def test_every_problem_kind_reaches_the_table():
    con = load(MESSY)
    kinds = {k for (k,) in con.execute("select distinct kind from problems").fetchall()}
    assert kinds == {
        "no-frontmatter",
        "unterminated-frontmatter",
        "invalid-yaml",
        "frontmatter-not-mapping",
        "missing-required",
        "bad-timestamp",
        "bad-sources",
        "bad-links",
    }


def test_paths_are_bundle_relative_and_posix():
    con = load(CLEAN)
    assert con.execute("select path from concepts").fetchone()[0] == (
        "concepts/api/overview.md"
    )


def test_mirror_view_absent_unless_requested():
    con = load(CLEAN)
    with pytest.raises(duckdb.CatalogException, match="mirror"):
        con.execute("select * from mirror")


def test_mirror_view_reads_canonical_documents(tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "deadbeef.json").write_text(
        json.dumps(
            {
                "doc_id": "wiki:api",
                "title": "API",
                "text": "body",
                "structured": {},
                "relations": [],
                "grounded_by": [],
                "deleted": False,
                "anchor": {
                    "system": "wiki",
                    "native_id": "api",
                    "url": "https://wiki/api",
                    "retrieved_at": "2026-08-23T09:00:00+00:00",
                    "content_hash": "aaa",
                },
            }
        )
    )
    con = load(CLEAN, mirror=mirror)
    assert con.execute("select doc_id from mirror").fetchone()[0] == "wiki:api"
    # The nested anchor is a struct, reachable without a parser.
    assert con.execute("select anchor.content_hash from mirror").fetchone()[0] == "aaa"


def test_mirror_glob_is_shallow_so_grounding_sidecars_are_missed(tmp_path):
    # grounding.SIDECAR_DIR puts flat {doc_id: content_hash} maps in
    # <mirror>/_grounding/. They are not CanonicalDocuments; a **/*.json glob
    # would union them in and wreck the inferred schema.
    mirror = tmp_path / "mirror"
    (mirror / "_grounding").mkdir(parents=True)
    (mirror / "deadbeef.json").write_text(
        json.dumps(
            {
                "doc_id": "wiki:api",
                "title": "API",
                "text": "b",
                "structured": {},
                "relations": [],
                "grounded_by": [],
                "deleted": False,
                "anchor": {
                    "system": "wiki",
                    "native_id": "api",
                    "url": None,
                    "retrieved_at": "2026-08-23T09:00:00+00:00",
                    "content_hash": "aaa",
                },
            }
        )
    )
    (mirror / "_grounding" / "deadbeef.json").write_text(json.dumps({"notes:x": "h"}))
    con = load(CLEAN, mirror=mirror)
    assert con.execute("select count(*) from mirror").fetchone()[0] == 1


def test_empty_mirror_raises_a_named_error(tmp_path):
    # read_json_auto raises IOException on a zero-match glob. Turn that into a
    # message naming the directory, rather than leaking a DuckDB IO error.
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    with pytest.raises(EmptyMirrorError, match=str(mirror)):
        load(CLEAN, mirror=mirror)


def test_misencoded_file_gets_a_row_instead_of_aborting_the_load(tmp_path):
    # One mis-encoded file must not raise UnicodeDecodeError and take the rest
    # of the bundle down with it -- that is exactly the pathology this tool
    # exists to surface, not crash on.
    concepts = tmp_path / "concepts"
    concepts.mkdir()
    (concepts / "bad.md").write_bytes(b"\xff\xfe not valid utf-8 garbage")
    con = load(tmp_path)
    assert (
        con.execute(
            "select count(*) from concepts where path = 'concepts/bad.md'"
        ).fetchone()[0]
        == 1
    )
    # The replacement characters land before any '---' fence, so this is
    # "no-frontmatter" -- an existing kind, not a new one invented for this.
    kind = con.execute(
        "select kind from problems where path = 'concepts/bad.md'"
    ).fetchone()[0]
    assert kind == "no-frontmatter"
