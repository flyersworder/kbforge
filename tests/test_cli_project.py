"""Project mode: `kbforge run` over a `kbforge.yaml` (architecture.md §7.5)."""

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


def _snapshot(root: Path) -> dict[str, bytes]:
    """Names and bytes of every file, but the empty lock file taking the
    lock leaves behind."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != "run.lock"
    }


def test_plan_then_run_agree(at, capsys):
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {"local_files": {"connector": "local_files", "config": {"path": "./handbook"}}},
    )
    assert main(["plan", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "local_files  1 added, 0 modified, 0 removed, 0 drifted, 0 rebuilt "
        "→ would propose 1 concepts (validation runs at publish)",
        "local_files    + local_files:a.md  added",
    ]
    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[0].startswith("local_files  Published:")
    assert main(["plan", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["local_files  no change → NoOp"]


def test_plan_writes_nothing_under_state_dir(at, capsys):
    _docs(at, "handbook", "a", "b", "c")
    yml = _project(
        at,
        {
            "local_files": {
                "connector": "local_files",
                "config": {"path": "./handbook"},
                "chunking": {"max_concepts": 1},
            }
        },
        links={"links": {"local_files:a.md": ["local_files:b.md"]}},
    )
    state = at / ".kbforge" / "state"
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    # The fixture must keep exercising what a stray write would hide in.
    assert list(state.glob("chunk-*.json"))
    assert list((at / ".kbforge" / "mirror").rglob("_links/*"))

    (at / "handbook" / "a.md").write_text(DOC.format(title="a edited"), "utf-8")
    before = _snapshot(at / ".kbforge")
    assert main(["plan", "--project", str(yml)]) == 0
    out = _lines(capsys)
    assert _snapshot(at / ".kbforge") == before
    assert out == [
        "local_files  0 added, 1 modified, 0 removed, 0 drifted, 0 rebuilt "
        "→ would propose 1 concepts; 2 more follow in later chunks "
        "(validation runs at publish)",
        "local_files    ~ local_files:a.md  modified",
    ]


def test_plan_needs_no_model_credentials(at, monkeypatch, capsys):
    import builtins

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("pydantic_ai"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "kbforge.llm_synthesizer", raising=False)
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {"h": {"connector": "local_files", "config": {"path": "./handbook"}}},
        synthesizer={"name": "llm", "model": "x"},
    )
    assert main(["plan", "--project", str(yml)]) == 0
    assert _lines(capsys)[0].startswith("h  1 added, 0 modified")


def test_plan_with_forge_publisher_and_no_token(at, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {
            "h": {
                "connector": "local_files",
                "config": {"path": "./handbook"},
                "chunking": {"max_concepts": 5},
            }
        },
        publisher={"name": "github", "repo": "acme/kb"},
    )
    assert main(["plan", "--project", str(yml)]) == 0
    out = _lines(capsys)
    assert out[0].startswith("h  note: open request unknown (")
    assert "env var GITHUB_TOKEN is not set" in out[0]
    assert out[0].endswith("); a run may wait instead")
    assert out[1].startswith("h  1 added")


def test_migration_from_flag_mode_plans_noop(at, capsys):
    docs = _docs(at, "handbook", "a")
    x = at / "X"
    out = at / "out"
    assert (
        main(
            [
                "run",
                "--connector",
                "local_files",
                "--set",
                f"path={docs}",
                "--mirror",
                str(x / "mirror"),
                "--state",
                str(x / "state"),
                "--publisher",
                "dry-run",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    capsys.readouterr()
    yml = _project(
        at,
        {"h": {"connector": "local_files", "config": {"path": str(docs)}}},
        state_dir="X",
    )
    # state_dir X puts the mirror at X/mirror and state at X/state.
    assert main(["plan", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["h  no change → NoOp"]


def test_plan_skips_the_open_request_check_when_the_publisher_is_unusable(
    at, monkeypatch, capsys
):
    import kbforge.project_cli as pc

    seen = []
    real = pc.plan

    def spy(*a, **k):
        seen.append(k["check_open_requests"])
        return real(*a, **k)

    monkeypatch.setattr(pc, "plan", spy)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {"h": {"connector": "local_files", "config": {"path": "./handbook"}}},
        publisher={"name": "github", "repo": "acme/kb"},
    )
    assert main(["plan", "--project", str(yml)]) == 0
    capsys.readouterr()
    assert seen == [False]


_PUBLISHED = r"last published \d{4}-\d\d-\d\d \d\d:\d\d UTC"


def test_status_before_and_after_a_run(at, capsys):
    import re

    _docs(at, "handbook", "a")
    yml = _project(
        at, {"h": {"connector": "local_files", "config": {"path": "./handbook"}}}
    )
    assert main(["status", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["h  never run"]

    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    assert main(["status", "--project", str(yml)]) == 0
    [line] = _lines(capsys)
    assert re.fullmatch(f"h  {_PUBLISHED} · no open request", line), line


def test_status_shows_a_pending_chunk(at, monkeypatch, capsys):
    import re

    from kbforge.publishers.dry_run import DryRunPublisher

    _docs(at, "handbook", "a", "b")
    _repo(at)
    yml = _project(
        at,
        {
            "docs": {
                "connector": "local_files",
                "config": {"path": "./handbook"},
                "chunking": {"max_concepts": 1},
            },
            "other": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )
    assert main(["run", "--project", str(yml), "docs"]) == 0
    capsys.readouterr()

    assert main(["status", "--project", str(yml)]) == 0
    docs_line, other_line = _lines(capsys)
    assert re.fullmatch(
        f"docs   {_PUBLISHED} · no open request · next chunk proposes on the next run",
        docs_line,
    ), docs_line
    assert other_line == "other  never run"

    monkeypatch.setattr(
        DryRunPublisher, "kbforge_open_request", lambda self, hint, config: "!12"
    )
    assert main(["status", "--project", str(yml)]) == 0
    docs_line, _ = _lines(capsys)
    assert re.fullmatch(
        f"docs   {_PUBLISHED} · review request !12 open · "
        "chunk of 1 open as !12, more to follow",
        docs_line,
    ), docs_line


def test_status_lists_a_removed_source_as_stale(at, capsys):
    import re

    _docs(at, "handbook", "a")
    _repo(at)
    yml = _project(
        at,
        {
            "h": {"connector": "local_files", "config": {"path": "./handbook"}},
            "gone": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    _project(at, {"h": {"connector": "local_files", "config": {"path": "./handbook"}}})
    assert main(["status", "--project", str(yml)]) == 0
    h_line, gone_line = _lines(capsys)
    assert re.fullmatch(f"h     {_PUBLISHED} · no open request", h_line), h_line
    assert re.fullmatch(
        f"gone  {_PUBLISHED} · "
        r"not in kbforge\.yaml \(kbforge reset gone clears its state\)",
        gone_line,
    ), gone_line


def test_status_exits_2_on_a_bad_project_and_notes_publisher_problems(
    at, monkeypatch, capsys
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {"h": {"connector": "local_files", "config": {"path": "./handbook"}}},
        publisher={"name": "github", "repo": "acme/kb"},
    )
    assert main(["status", "--project", str(yml)]) == 0
    assert _lines(capsys) == ["h  never run"]

    from kbforge.pipeline import instance_key

    state = at / ".kbforge" / "state"
    state.mkdir(parents=True)
    (state / "sources.json").write_text(
        json.dumps(
            {
                "h": {
                    "connector": "local_files",
                    "digest": instance_key({"path": "./handbook"}),
                    "systems": ["local_files"],
                    "last_published": "2026-10-28T16:02:00+02:00",
                }
            }
        )
    )
    assert main(["status", "--project", str(yml)]) == 0
    [line] = _lines(capsys)
    assert line.startswith(
        "h  last published 2026-10-28 14:02 UTC · open request unknown ("
    )
    assert "env var GITHUB_TOKEN is not set" in line

    yml.write_text("sources: {}\nbogus: 1\n")
    assert main(["status", "--project", str(yml)]) == 2


def test_status_reports_a_forge_failure_as_unknown_and_exits_0(at, monkeypatch, capsys):
    from kbforge.publishers._http import ForgeError
    from kbforge.publishers.dry_run import DryRunPublisher

    _docs(at, "handbook", "a")
    yml = _project(
        at, {"h": {"connector": "local_files", "config": {"path": "./handbook"}}}
    )
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()

    def boom(self, hint, config):
        raise ForgeError(0, "https://forge.example/api", "timed out")

    monkeypatch.setattr(DryRunPublisher, "kbforge_open_request", boom)
    assert main(["status", "--project", str(yml)]) == 0
    [line] = _lines(capsys)
    assert line.endswith(
        " · open request unknown (0 from https://forge.example/api: timed out)"
    ), line
    assert line.startswith("h  last published ")


def test_project_redo_reproposes_the_last_chunk(at, capsys):
    _docs(at, "handbook", "a", "b")
    yml = _project(
        at,
        {
            "docs": {
                "connector": "local_files",
                "config": {"path": "./handbook"},
                "chunking": {"max_concepts": 1},
            }
        },
    )
    mirror = at / ".kbforge" / "mirror"
    assert main(["run", "--project", str(yml)]) == 0
    capsys.readouterr()
    assert len(list(mirror.glob("*.json"))) == 1

    assert main(["redo", "docs", "--project", str(yml)]) == 0
    assert _lines(capsys) == [
        "docs  Redone: 1 document(s) will be proposed again on the next run."
    ]
    assert list(mirror.glob("*.json")) == []

    assert main(["run", "--project", str(yml)]) == 0
    assert _lines(capsys)[0].startswith("docs  Published:")
    assert len(list(mirror.glob("*.json"))) == 1

    assert main(["redo", "docs", "--project", str(yml)]) == 0
    capsys.readouterr()
    assert main(["redo", "docs", "--project", str(yml)]) == 1
    assert _lines(capsys) == [
        "docs  Redo refused: local_files: no chunk to redo (no chunk record in "
        f"{at / '.kbforge' / 'state'}; only a run with --chunking writes one)"
    ]


def test_project_redo_takes_one_known_source(at, capsys):
    _docs(at, "handbook", "a")
    yml = _project(
        at, {"docs": {"connector": "local_files", "config": {"path": "./handbook"}}}
    )
    assert main(["redo", "nope", "--project", str(yml)]) == 2
    assert _lines(capsys) == ["unknown source 'nope'; known: docs"]
    assert main(["redo", "--project", str(yml)]) == 2
    assert _lines(capsys) == ["kbforge redo takes exactly one SOURCE"]
    assert main(["redo", "docs", "docs", "--project", str(yml)]) == 2
    assert _lines(capsys) == ["kbforge redo takes exactly one SOURCE"]


def test_project_redo_needs_a_usable_publisher(at, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _docs(at, "handbook", "a")
    yml = _project(
        at,
        {"docs": {"connector": "local_files", "config": {"path": "./handbook"}}},
        publisher={"name": "github", "repo": "acme/kb"},
    )
    assert main(["redo", "docs", "--project", str(yml)]) == 2
    [line] = _lines(capsys)
    assert line.startswith("docs: "), line
    assert "env var GITHUB_TOKEN is not set" in line


INIT_LINES = [
    "wrote kbforge.yaml",
    "next: kbforge plan",
    "keep .kbforge/ between runs: it holds the mirror that makes re-runs free",
]


def test_init_writes_a_project_that_plans_and_runs(at, capsys):
    _docs(at, "docs", "a")
    assert main(["init"]) == 0
    assert _lines(capsys) == INIT_LINES
    assert main(["plan"]) == 0
    assert "docs    + local_files:a.md  added" in _lines(capsys)
    assert main(["run"]) == 0
    assert _lines(capsys)[0].startswith("docs  Published:")


def test_init_takes_the_docs_path_verbatim_and_a_project_target(at, capsys):
    (at / "sub").mkdir()
    assert (
        main(["init", "../kb/handbook", "--project", str(at / "sub" / "p.yaml")]) == 0
    )
    written = yaml.safe_load((at / "sub" / "p.yaml").read_text("utf-8"))
    assert written["sources"]["docs"]["config"] == {"path": "../kb/handbook"}


def test_init_never_overwrites(at, capsys):
    (at / "kbforge.yaml").write_text("mine: true\n", "utf-8")
    assert main(["init"]) == 2
    assert _lines(capsys) == ["kbforge.yaml already exists; not overwritten"]
    assert (at / "kbforge.yaml").read_text("utf-8") == "mine: true\n"


def test_init_template_uncommented_blocks_validate(at, capsys):
    """Uncomment exactly the example blocks (a `# synthesizer:` / `# publisher:`
    line and the `#   ` lines under it) in place of the live defaults."""
    from kbforge.project_cli import INIT_TEMPLATE

    out, block = [], False
    for line in INIT_TEMPLATE.format(docs_path="./docs").splitlines():
        if line.startswith(("synthesizer:", "publisher:")):
            continue  # the live default the example replaces
        if line in ("# synthesizer:", "# publisher:") or (
            block and line.startswith("#   ")
        ):
            block = True
            out.append(line[2:])
        else:
            block = False
            out.append(line)
    (at / "kbforge.yaml").write_text("\n".join(out), "utf-8")
    project = load_project(at / "kbforge.yaml")
    assert project.synthesizer.name == "llm"
    assert (project.synthesizer.model_extra or {}).get("model")
    assert project.publisher.name == "github"
    assert project.publisher.model_extra == {
        "repo": "acme/knowledge-base",
        "token_env": "GITHUB_TOKEN",
    }


class SqlSourceError(Exception):
    """Stands in for a third-party connector's own exception type."""


class _BoomConnector:
    """A connector whose fetch raises an exception kbforge does not know."""

    def kbforge_connector_info(self):
        from kbforge.models import ConnectorInfo

        return ConnectorInfo(name="boom", version="0.1.0", source_system="t")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        raise SqlSourceError("unable to open database file")

    def kbforge_normalize(self, records):
        return []


def _registry_with(monkeypatch, *plugins):
    import kbforge.project_cli as pc

    def build():
        pm = build_registry()
        for plugin in plugins:
            pm.register(plugin)
        return pm

    monkeypatch.setattr(pc, "build_registry", build)


def _boom_project(at: Path) -> Path:
    _docs(at, "one", "a")
    _repo(at)
    return _project(
        at,
        {
            "a": {"connector": "local_files", "config": {"path": "./one"}},
            "b": {"connector": "boom"},
            "c": {"connector": "git_commits", "config": {"repo": "./repo"}},
        },
    )


BOOM = "b  error: SqlSourceError: unable to open database file"


def test_a_connector_exception_does_not_stop_later_sources_in_run(
    at, monkeypatch, capsys
):
    _registry_with(monkeypatch, _BoomConnector())
    yml = _boom_project(at)
    assert main(["run", "--project", str(yml)]) == 1
    out = _lines(capsys)
    assert out[0].startswith("a  Published:")
    assert out[1] == BOOM
    assert out[2].startswith("c  Published:")
    assert len(out) == 3
    assert list(_entries(at)) == ["a", "c"]


def test_a_connector_exception_does_not_stop_later_sources_in_plan(
    at, monkeypatch, capsys
):
    _registry_with(monkeypatch, _BoomConnector())
    yml = _boom_project(at)
    assert main(["plan", "--project", str(yml)]) == 1
    out = _lines(capsys)
    assert out[0].startswith("a  1 added, 0 modified")
    assert out[1] == "a    + local_files:a.md  added"
    assert out[2] == BOOM
    assert out[3].startswith("c  1 added, 0 modified")
    assert not (at / ".kbforge" / "state").exists()
