import os
import subprocess
from pathlib import Path

import pluggy
import pytest

from kbforge.__main__ import _parse_settings, main
from kbforge.hookspecs import CONNECTOR_ENTRYPOINTS, PUBLISHER_ENTRYPOINTS
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


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror",
        str(tmp_path / "mirror"),
        "--out",
        str(tmp_path / "out"),
        "--state",
        str(tmp_path / "state"),
    ]


def test_registry_exposes_connectors_and_publisher():
    pm = build_registry()
    names = {p.__class__.__name__ for p in pm.get_plugins()}
    assert {"LocalFilesConnector", "GitCommitsConnector", "DryRunPublisher"} <= names


def test_registry_loads_setuptools_entrypoints(monkeypatch):
    # The drop-in seam: build_registry must ask pluggy to discover third-party
    # plugins advertised under the connector and publisher entry-point groups.
    seen: list[str] = []

    def spy(self, group, name=None):
        seen.append(group)
        return 0

    monkeypatch.setattr(pluggy.PluginManager, "load_setuptools_entrypoints", spy)
    build_registry()
    assert seen == [CONNECTOR_ENTRYPOINTS, PUBLISHER_ENTRYPOINTS]


def test_parse_settings_yaml_types_values():
    cfg = _parse_settings(["path=/docs", "max_commits=5", "ignore_globs=[drafts, x]"])
    assert cfg["path"] == "/docs"  # str
    assert cfg["max_commits"] == 5  # int, not "5"
    assert cfg["ignore_globs"] == ["drafts", "x"]  # list


def test_parse_settings_rejects_missing_equals():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        _parse_settings(["justakey"])


def test_list_command_shows_connectors(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "local_files" in out
    assert "git_commits" in out


def test_cli_run_local_files_via_generic_config(tmp_path: Path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert (tmp_path / "out" / "sync-local_files" / "concepts/x/overview.md").exists()


def test_cli_run_git_commits_via_generic_config(tmp_path: Path, capsys):
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
            "--set",
            f"repo={repo}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published" in capsys.readouterr().out
    assert len(list((tmp_path / "out" / "sync-git_commits").rglob("overview.md"))) == 1


def test_cli_unknown_connector_lists_available(tmp_path: Path, capsys):
    code = main(["run", "--connector", "jira", "--set", "x=1", *_plumbing(tmp_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "unknown connector" in out
    assert "local_files" in out and "git_commits" in out  # available list shown


def test_cli_config_error_surfaces_nonzero(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={tmp_path / 'nope'}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "path" in capsys.readouterr().out  # the connector's config problem
