from pathlib import Path

from kbforge.__main__ import main


def _plumbing(tmp_path: Path) -> list[str]:
    return [
        "--mirror",
        str(tmp_path / "mirror"),
        "--out",
        str(tmp_path / "out"),
        "--state",
        str(tmp_path / "state"),
    ]


def _source(tmp_path: Path, names: list[str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for n in names:
        (src / f"{n}.md").write_text(
            f"---\ntype: application\ntitle: {n}\n---\n{n}.\n", "utf-8"
        )
    return src


def _concepts(tmp_path: Path) -> list[Path]:
    return [p for p in (tmp_path / "out").rglob("*.md") if p.name != "MR_BODY.md"]


def _chunking(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "chunking.yaml"
    path.write_text(text, "utf-8")
    return path


def test_run_with_chunking_publishes_one_chunk_per_run(tmp_path, capsys):
    src = _source(tmp_path, ["a", "b"])
    cfg = _chunking(tmp_path, "max_concepts: 1\n")
    args = [
        "run",
        "--connector",
        "local_files",
        "--set",
        f"path={src}",
        "--chunking",
        str(cfg),
        *_plumbing(tmp_path),
    ]
    assert main(args) == 0
    assert len(_concepts(tmp_path)) == 1
    assert main(args) == 0
    assert len(_concepts(tmp_path)) == 2
    assert main(args) == 0
    assert "NoOp" in capsys.readouterr().out


def test_a_bad_chunking_file_exits_2(tmp_path, capsys):
    src = _source(tmp_path, ["a"])
    cfg = _chunking(tmp_path, "max_concepts: 0\n")
    code = main(
        [
            "run",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            "--chunking",
            str(cfg),
            *_plumbing(tmp_path),
        ]
    )
    assert code == 2
    out = capsys.readouterr().out
    assert f"chunking config {cfg}:" in out
    assert "greater than or equal to 1" in out


def test_redo_reproposes_the_last_chunk(tmp_path, capsys):
    src = _source(tmp_path, ["a", "b"])
    cfg = _chunking(tmp_path, "max_concepts: 1\n")
    run_args = [
        "run",
        "--connector",
        "local_files",
        "--set",
        f"path={src}",
        "--chunking",
        str(cfg),
        *_plumbing(tmp_path),
    ]
    mirror = tmp_path / "mirror"
    assert main(run_args) == 0
    assert len(list(mirror.glob("*.json"))) == 1

    redo_args = [
        "redo",
        "--connector",
        "local_files",
        "--set",
        f"path={src}",
        *_plumbing(tmp_path),
    ]
    assert main(redo_args) == 0
    assert (
        "Redone: 1 document(s) will be proposed again on the next run."
        in capsys.readouterr().out
    )
    assert list(mirror.glob("*.json")) == [], (
        "redo must roll the chunk out of the mirror"
    )

    assert main(run_args) == 0
    assert "Published" in capsys.readouterr().out
    assert len(list(mirror.glob("*.json"))) == 1


def test_redo_without_a_record_exits_1(tmp_path, capsys):
    src = _source(tmp_path, ["a"])
    code = main(
        [
            "redo",
            "--connector",
            "local_files",
            "--set",
            f"path={src}",
            *_plumbing(tmp_path),
        ]
    )
    assert code == 1
    assert "Redo refused: local_files: no chunk to redo" in capsys.readouterr().out
