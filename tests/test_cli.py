from pathlib import Path

from kbforge.__main__ import main
from kbforge.registry import build_registry

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"


def test_registry_exposes_connector_and_publisher():
    pm = build_registry()
    names = {p.__class__.__name__ for p in pm.get_plugins()}
    assert {"LocalFilesConnector", "DryRunPublisher"} <= names


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
    assert (tmp_path / "out" / "sync-local-files" / "concepts/x/overview.md").exists()
