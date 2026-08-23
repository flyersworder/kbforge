from __future__ import annotations

import json
from pathlib import Path

from okfquery.cli import main

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN = str(FIXTURES / "clean")
MESSY = str(FIXTURES / "messy")


def test_query_table_format(capsys):
    code = main(["query", "select title from concepts", "--bundle", CLEAN])
    assert code == 0
    assert "API overview" in capsys.readouterr().out


def test_query_json_format(capsys):
    code = main(
        ["query", "select title from concepts", "--bundle", CLEAN, "--format", "json"]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == [{"title": "API overview"}]


def test_query_csv_format(capsys):
    code = main(
        ["query", "select title from concepts", "--bundle", CLEAN, "--format", "csv"]
    )
    assert code == 0
    assert capsys.readouterr().out.splitlines() == ["title", "API overview"]


def test_query_reports_bad_sql_without_a_traceback(capsys):
    code = main(["query", "select * from nope", "--bundle", CLEAN])
    assert code == 2
    assert "nope" in capsys.readouterr().err


def test_schema_prints_the_ddl(capsys):
    assert main(["schema"]) == 0
    out = capsys.readouterr().out
    assert "CREATE TABLE concepts" in out
    assert "TIMESTAMPTZ" in out


def test_check_passes_on_a_clean_bundle(capsys):
    assert main(["check", "--bundle", CLEAN]) == 0
    assert "no problems" in capsys.readouterr().out


def test_check_fails_and_lists_problems(capsys):
    assert main(["check", "--bundle", MESSY]) == 1
    err = capsys.readouterr().err
    assert "badyaml/overview.md" in err
    assert "invalid-yaml" in err


def test_empty_mirror_is_a_message_not_a_traceback(capsys, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    code = main(["query", "select 1", "--bundle", CLEAN, "--mirror", str(mirror)])
    assert code == 2
    assert str(mirror) in capsys.readouterr().err


def test_missing_bundle_is_a_message_not_a_silent_success(capsys, tmp_path):
    # scan() returns [] for a bundle with no concepts/ dir, same as a genuinely
    # empty one -- so `check` must reject the missing case itself, or it prints
    # "no problems" and exits 0 for a bundle that was never loaded at all.
    missing = tmp_path / "nonexistent"
    code = main(["check", "--bundle", str(missing)])
    assert code == 2
    assert str(missing) in capsys.readouterr().err


def test_missing_bundle_rejected_for_query_too(capsys, tmp_path):
    missing = tmp_path / "nonexistent"
    code = main(["query", "select 1", "--bundle", str(missing)])
    assert code == 2
    assert str(missing) in capsys.readouterr().err


def test_shell_on_empty_mirror_is_a_message_not_a_traceback(capsys, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    code = main(["shell", "--bundle", CLEAN, "--mirror", str(mirror)])
    assert code == 2
    assert str(mirror) in capsys.readouterr().err


def test_shell_on_missing_bundle_is_a_message_not_a_traceback(capsys, tmp_path):
    missing = tmp_path / "nonexistent"
    code = main(["shell", "--bundle", str(missing)])
    assert code == 2
    assert str(missing) in capsys.readouterr().err
