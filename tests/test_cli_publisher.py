from pathlib import Path

import pytest

from kbforge.__main__ import _publishers, main
from kbforge.publishers._http import ForgeError, TreeListingTruncatedError
from kbforge.publishers.forge import PathError
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror",
        str(tmp_path / "mirror"),
        "--out",
        str(tmp_path / "out"),
        "--state",
        str(tmp_path / "state"),
    ]


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "app-x.md").write_text(DOC, "utf-8")
    return src


def test_registry_exposes_all_three_publishers():
    names = set(_publishers(build_registry()))
    assert {"dry-run", "github", "gitlab"} <= names


def test_publisher_lookup_is_by_name_not_registration_order():
    publishers = _publishers(build_registry())
    assert publishers["github"].kbforge_publisher_info().name == "github"
    assert publishers["gitlab"].kbforge_publisher_info().name == "gitlab"


def test_list_shows_publishers(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "publishers:" in out
    assert "github" in out
    assert "gitlab" in out


def test_unknown_publisher_exits_2(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "bitbucket",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "unknown publisher" in capsys.readouterr().out


def test_default_publisher_is_dry_run(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 0
    assert "Published:" in capsys.readouterr().out
    assert (tmp_path / "out").exists()


def test_forge_publisher_config_is_validated_before_the_pipeline_runs(
    tmp_path: Path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "github",
            "--publish-set",
            "repo=acme/kb",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().out
    # Fail-fast: the pipeline never ran, so no mirror was written.
    assert not (tmp_path / "mirror").exists()


def test_publish_set_values_are_yaml_typed():
    from kbforge.__main__ import _parse_settings

    assert _parse_settings(["repo=acme/kb", "base_path=knowledge"]) == {
        "repo": "acme/kb",
        "base_path": "knowledge",
    }


def test_malformed_publish_set_exits_2(tmp_path: Path, capsys):
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            "github",
            "--publish-set",
            "norepo",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "KEY=VALUE" in capsys.readouterr().out


def _run_with_publish_error(tmp_path, monkeypatch, exc):
    """Drive the CLI to the publish step, which then raises `exc`."""

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr("kbforge.__main__.run", boom)
    return main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            *_plumbing(tmp_path),
        ]
    )


def test_forge_error_from_the_publish_step_exits_1(tmp_path: Path, capsys, monkeypatch):
    code = _run_with_publish_error(
        tmp_path, monkeypatch, ForgeError(500, "https://api.example/x", "boom")
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Publish failed" in out
    assert "500" in out


def test_tree_listing_truncated_from_the_publish_step_exits_1(
    tmp_path: Path, capsys, monkeypatch
):
    """Not a ForgeError, so naming publisher exception types one by one had let
    this escape as a traceback — losing remediation advice written for the user.
    Every PublishError subclass must reach the same handler."""
    code = _run_with_publish_error(
        tmp_path,
        monkeypatch,
        TreeListingTruncatedError("did not finish within _TREE_MAX_PAGES (1000) pages"),
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Publish failed" in out
    assert "_TREE_MAX_PAGES" in out


def test_path_error_from_the_publish_step_exits_1(tmp_path: Path, capsys, monkeypatch):
    """A connector emitting a traversing file key is the case safe_join()
    exists for; it must not surface as a traceback."""
    code = _run_with_publish_error(
        tmp_path, monkeypatch, PathError("'..' not allowed in path: '../../x.yml'")
    )

    assert code == 1
    out = capsys.readouterr().out
    assert "Publish failed" in out
    assert "not allowed in path" in out


@pytest.mark.parametrize("publisher", ["github", "gitlab"])
def test_forge_publishers_reject_unknown_config_keys(
    tmp_path: Path, capsys, monkeypatch, publisher
):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={_source(tmp_path)}",
            "--publisher",
            publisher,
            "--publish-set",
            "repo=acme/kb",
            "--publish-set",
            "reviewers=[a]",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    assert "reviewers" in capsys.readouterr().out
