"""Project mode: `kbforge run` over a `kbforge.yaml` (project-file design §6.2)."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kbforge.__main__ import main
from kbforge.lock import lock_path
from kbforge.project import ProjectError, load_project
from kbforge.project_cli import resolve
from kbforge.registry import build_registry
from kbforge.synthesize import concept_path

DOC = "---\ntype: application\ntitle: {title}\n---\n{title} does things.\n"

NOTE = (
    "note: config changed since the last run; starting from a fresh cursor "
    "(kbforge reset {name} clears the old state)."
)

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={"PATH": os.environ.get("PATH", ""), **_GIT_ENV},
        capture_output=True,
        text=True,
        check=True,
    )


def _repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "file.txt").write_text("hello\n", "utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "Add the file")
    return repo


def _docs(root: Path, name: str, *stems: str) -> Path:
    d = root / name
    d.mkdir()
    for stem in stems:
        (d / f"{stem}.md").write_text(DOC.format(title=stem), "utf-8")
    return d


def _project(root: Path, sources: dict, **top) -> Path:
    yml = root / "kbforge.yaml"
    yml.write_text(yaml.safe_dump({**top, "sources": sources}, sort_keys=False))
    return yml


def _entries(root: Path) -> dict:
    path = root / ".kbforge" / "state" / "sources.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


@pytest.fixture
def at(tmp_path, monkeypatch):
    """Run from the project directory unless a test says otherwise; the CLI
    chdirs itself, this only keeps a failing test from leaking a cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_project_run_syncs_every_source_then_noops(at, capsys):
    _docs(at, "handbook", "app-x")
    _repo(at)
    yml = _project(
        at,
        {
            "handbook": {"connector": "local_files", "config": {"path": "./handbook"}},
            "commits": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )
    assert main(["run", "--project", str(yml)]) == 0
    out = _lines(capsys)
    assert len(out) == 2
    assert out[0].startswith("handbook  Published:")
    assert out[1].startswith("commits   Published:")

    entries = _entries(at)
    assert list(entries) == ["handbook", "commits"]
    assert entries["handbook"]["connector"] == "local_files"
    assert entries["handbook"]["systems"] == ["local_files"]
    assert entries["commits"]["systems"] == ["git_commits"]
    assert entries["handbook"]["last_published"].endswith("+00:00")

    assert main(["run", "--project", str(yml)]) == 0
    out = _lines(capsys)
    assert out == [
        "handbook  NoOp: no change detected; no MR opened.",
        "commits   NoOp: no change detected; no MR opened.",
    ]
    # A NoOp keeps the last publish time it already had.
    assert (
        _entries(at)["handbook"]["last_published"]
        == (entries["handbook"]["last_published"])
    )


def test_relative_connector_paths_resolve_against_the_project_file(
    tmp_path, monkeypatch, capsys
):
    _docs(tmp_path, "handbook", "app-x")
    yml = _project(
        tmp_path,
        {"handbook": {"connector": "local_files", "config": {"path": "./handbook"}}},
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert main(["run", "--project", str(yml)]) == 0
    assert Path.cwd() == elsewhere
    out = tmp_path / ".kbforge" / "out" / "sync-local_files"
    assert _lines(capsys) == [f"handbook  Published: {out}"]
    assert (out / "MR_BODY.md").exists()
    assert not (elsewhere / ".kbforge").exists()


def test_the_default_project_file_is_the_one_in_the_cwd(at, capsys):
    _docs(at, "handbook", "app-x")
    _project(
        at,
        {"handbook": {"connector": "local_files", "config": {"path": "./handbook"}}},
    )
    assert main(["run"]) == 0
    assert _lines(capsys)[0].startswith("handbook  Published:")


def test_a_missing_project_file_exits_2(at, capsys):
    assert main(["run"]) == 2
    assert _lines(capsys) == [f"kbforge.yaml: not found in {at.resolve()}"]


def test_one_misconfigured_source_runs_nothing(at, capsys):
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {
            "a": {"connector": "local_files", "config": {"path": "./handbook"}},
            "b": {"connector": "local_files", "config": {"path": "./nope"}},
            "c": {"connector": "nope"},
            "d": {
                "connector": "local_files",
                "config": {"path": "./handbook"},
                "synthesizer": {"name": "imaginary"},
            },
        },
        publisher={"name": "dry-run", "out_dir": ""},
    )
    assert main(["run", "--project", str(yml)]) == 2
    out = _lines(capsys)
    assert "b: config 'path' is not a readable directory: './nope'" in out
    assert any(
        line.startswith("c: unknown connector 'nope'; available: ") for line in out
    )
    assert "d: unknown synthesizer 'imaginary'; available: stub, llm, describe" in out
    assert "publisher: 'out_dir' is required" in out
    assert not any(line.startswith("a  ") for line in out)
    assert not (at / ".kbforge" / "mirror").exists()
    assert not (at / ".kbforge" / "state").exists()
    assert not (at / ".kbforge" / "out").exists()


def test_project_level_problems_are_reported_with_the_rest(at, capsys):
    yml = _project(
        at,
        {"a": {"connector": "local_files", "config": {"path": "./nope"}}},
        links="missing-links.yaml",
    )
    assert main(["run", "--project", str(yml)]) == 2
    out = _lines(capsys)
    assert "a: config 'path' is not a readable directory: './nope'" in out
    assert any(
        line.startswith(f"links config {at / 'missing-links.yaml'}: ") for line in out
    )


def test_llm_synthesizer_without_credentials_exits_2(at, capsys, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {"a": {"connector": "local_files", "config": {"path": "./handbook"}}},
        synthesizer={"name": "llm", "model": "openrouter/x"},
    )
    assert main(["run", "--project", str(yml)]) == 2
    out = _lines(capsys)
    assert len(out) == 1 and out[0].startswith("synthesizer: ")
    assert "OPENROUTER_API_KEY" in out[0]
    assert not (at / ".kbforge" / "mirror").exists()


def test_a_failing_source_does_not_stop_the_next(at, capsys):
    _docs(at, "one", "a")
    _docs(at, "two", "b")
    _repo(at)
    yml = _project(
        at,
        {
            "a": {"connector": "local_files", "config": {"path": "./one"}},
            "b": {"connector": "local_files", "config": {"path": "./two"}},
            "c": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )
    assert main(["run", "--project", str(yml)]) == 1
    out = _lines(capsys)
    assert out[0].startswith("a  Published:")
    assert out[1] == "b  Aborted: 1 validation failure(s):"
    assert out[2].startswith("b    [source-ownership] ")
    assert out[3].startswith("c  Published:")
    assert len(out) == 4
    # An aborted source writes nothing to sources.json.
    assert list(_entries(at)) == ["a", "c"]


def test_two_local_files_sources_second_aborts(at, capsys):
    _docs(at, "one", "a")
    _docs(at, "two", "b")
    yml = _project(
        at,
        {
            "a": {"connector": "local_files", "config": {"path": "./one"}},
            "b": {"connector": "local_files", "config": {"path": "./two"}},
        },
    )
    failure = (
        f"b    [source-ownership] {concept_path('local_files:b.md')}: "
        "system 'local_files' is already owned by source 'a'"
    )
    assert main(["run", "--project", str(yml)]) == 1
    out = _lines(capsys)
    assert out[1:] == ["b  Aborted: 1 validation failure(s):", failure]

    # A later invocation selecting only b knows a's claim from sources.json.
    assert main(["run", "--project", str(yml), "b"]) == 1
    assert _lines(capsys) == ["b  Aborted: 1 validation failure(s):", failure]


def test_renaming_a_source_keeps_its_cursor_and_ownership(at, capsys):
    _docs(at, "handbook", "app-x")
    config = {"connector": "local_files", "config": {"path": "./handbook"}}
    yml = _project(at, {"handbook": config})
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[0].startswith("handbook  Published:")

    _project(at, {"docs": config})
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["docs  NoOp: no change detected; no MR opened."]
    # The old name's entry stays (status lists it as stale; reset still works).
    assert set(_entries(at)) == {"handbook", "docs"}


def test_config_change_prints_the_note(at, capsys):
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {"handbook": {"connector": "local_files", "config": {"path": "./handbook"}}},
    )
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()

    _project(
        at,
        {
            "handbook": {
                "connector": "local_files",
                "config": {"path": "./handbook", "ignore_globs": ["drafts"]},
            }
        },
    )
    assert main(["run", "--project", str(yml)]) == 0
    out = _lines(capsys)
    assert out[0] == "handbook  " + NOTE.format(name="handbook")
    assert len(out) == 2 and out[1].startswith("handbook  ")

    assert main(["run", "--project", str(yml)]) == 0
    assert not any("note:" in line for line in _lines(capsys))


HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from kbforge.lock import mirror_lock\n"
    "with mirror_lock(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


def test_project_run_exits_3_while_a_flag_run_holds_the_lock(at, capsys):
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {"handbook": {"connector": "local_files", "config": {"path": "./handbook"}}},
    )
    mirror = at / ".kbforge" / "mirror"
    # What a flag-mode `run --mirror .kbforge/mirror` holds while it works.
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(mirror)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "held"
        code = main(["run", "--project", str(yml)])
    finally:
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    assert code == 3
    assert _lines(capsys) == [
        f"Locked: another kbforge run holds {lock_path(mirror)}; "
        "nothing was read or written."
    ]
    assert not mirror.exists()
    assert not (at / ".kbforge" / "state").exists()


def test_select_sources_by_name_and_unknown_name_exits_2(at, capsys):
    _docs(at, "handbook", "app-x")
    _repo(at)
    yml = _project(
        at,
        {
            "a": {"connector": "local_files", "config": {"path": "./handbook"}},
            "c": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )
    assert main(["run", "--project", str(yml), "c"]) == 0
    out = _lines(capsys)
    assert len(out) == 1 and out[0].startswith("c  Published:")
    assert list(_entries(at)) == ["c"]

    assert main(["run", "--project", str(yml), "zzz", "c"]) == 2
    assert _lines(capsys) == ["unknown source 'zzz'; known: a, c"]


@pytest.mark.parametrize(
    "extra, flag",
    [
        (["--set", "path=x"], "--set"),
        (["--publish-set", "out_dir=x"], "--publish-set"),
        (["--llm-set", "model=x"], "--llm-set"),
        (["--synthesizer", "stub"], "--synthesizer"),
        (["--publisher", "dry-run"], "--publisher"),
        (["--grounding", "g.yaml"], "--grounding"),
        (["--chunking", "c.yaml"], "--chunking"),
        (["--links", "l.yaml"], "--links"),
        (["--mirror", "m"], "--mirror"),
        (["--state", "s"], "--state"),
        (["--out", "o"], "--out"),
    ],
)
def test_flag_options_without_connector_exit_2(at, capsys, extra, flag):
    assert main(["run", *extra]) == 2
    assert _lines(capsys) == [
        f"{flag} needs --connector; a project's sources are configured in kbforge.yaml"
    ]


def test_flag_mode_still_requires_its_plumbing(at, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--connector", "local_files", "--set", "path=x"])
    assert exc.value.code == 2
    assert "the following arguments are required: --mirror, --state, --out" in (
        capsys.readouterr().err
    )
    with pytest.raises(SystemExit) as exc:
        main(["redo", "--connector", "local_files", "--mirror", "m"])
    assert exc.value.code == 2
    assert "the following arguments are required: --state" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [["--project", "kbforge.yaml"], ["handbook"]],
)
def test_flag_mode_refuses_project_arguments(at, capsys, extra):
    plumbing = ["--mirror", "m", "--state", "s", "--out", "o"]
    with pytest.raises(SystemExit) as exc:
        main(["run", "--connector", "local_files", *plumbing, *extra])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert ("--project" in err or "handbook" in err) and "--connector" in err


def test_resolve_for_plan_needs_no_credentials(at, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {"a": {"connector": "local_files", "config": {"path": "./handbook"}}},
        synthesizer={"name": "llm", "model": "openrouter/x"},
        publisher={"name": "dry-run", "out_dir": ""},
    )
    project = load_project(yml)
    [rs] = resolve(project, build_registry(), [], for_plan=True)
    assert rs.synthesizer is None and rs.grounds is True
    assert rs.publisher_problems == ["'out_dir' is required"]

    with pytest.raises(ProjectError) as exc:
        resolve(project, build_registry(), [], for_plan=False)
    assert exc.value.problems == [
        "publisher: 'out_dir' is required",
        "synthesizer: env var OPENROUTER_API_KEY is not set",
    ]


def test_resolve_for_plan_still_checks_the_synthesizer_shape(at):
    _docs(at, "handbook", "app-x")
    yml = _project(
        at,
        {"a": {"connector": "local_files", "config": {"path": "./handbook"}}},
        synthesizer={"name": "llm", "no_such_setting": 1},
    )
    with pytest.raises(ProjectError) as exc:
        resolve(load_project(yml), build_registry(), [], for_plan=True)
    [problem] = exc.value.problems
    assert problem.startswith("synthesizer: ") and "no_such_setting" in problem
