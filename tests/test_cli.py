import os
import subprocess
from pathlib import Path

from kbforge.__main__ import main
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"

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


def test_registry_exposes_connectors_and_publisher():
    pm = build_registry()
    names = {p.__class__.__name__ for p in pm.get_plugins()}
    assert {"LocalFilesConnector", "GitCommitsConnector", "DryRunPublisher"} <= names


def test_cli_run_bootstrap(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--source",
            str(src),
            "--mirror",
            str(tmp_path / "mirror"),
            "--out",
            str(tmp_path / "out"),
            "--state",
            str(tmp_path / "state"),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert (tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md").exists()


def test_cli_run_git_commits_connector(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("1")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "initial commit")
    code = main(
        [
            "run",
            "--connector",
            "git_commits",
            "--source",
            str(repo),
            "--mirror",
            str(tmp_path / "mirror"),
            "--out",
            str(tmp_path / "out"),
            "--state",
            str(tmp_path / "state"),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    concepts = list((tmp_path / "out" / "sync-git_commits").rglob("overview.md"))
    assert len(concepts) == 1
