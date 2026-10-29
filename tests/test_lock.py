import signal
import subprocess
import sys
from pathlib import Path

import pytest

from kbforge.__main__ import main
from kbforge.lock import MirrorLocked, lock_path, mirror_lock

DOC = "---\ntype: application\ntitle: App X\n---\nApp X.\n"

HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from kbforge.lock import mirror_lock\n"
    "with mirror_lock(Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(60)\n"
)


def _hold(mirror: Path) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(mirror)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "held"
    return proc


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


def test_second_holder_is_refused(tmp_path):
    m = tmp_path / "mirror"
    with mirror_lock(m):
        with pytest.raises(MirrorLocked) as exc:
            with mirror_lock(m):
                ...
    assert exc.value.path == lock_path(m)
    assert exc.value.path == tmp_path.resolve() / "run.lock"


def test_lock_is_reusable_after_release_and_file_kept(tmp_path):
    m = tmp_path / "mirror"
    with mirror_lock(m):
        ...
    assert lock_path(m).exists()
    with mirror_lock(m):
        ...


def test_lock_is_released_when_the_holder_dies(tmp_path):
    m = tmp_path / "mirror"
    proc = _hold(m)
    try:
        with pytest.raises(MirrorLocked):
            with mirror_lock(m):
                ...
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        with mirror_lock(m):
            ...
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


def test_flag_run_exits_3_while_the_mirror_is_locked(tmp_path, capsys):
    src = _source(tmp_path)
    proc = _hold(tmp_path / "mirror")
    try:
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
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    assert code == 3
    out = capsys.readouterr().out
    assert out.strip() == (
        f"Locked: another kbforge run holds {lock_path(tmp_path / 'mirror')}; "
        "nothing was read or written."
    )
    assert not (tmp_path / "mirror").exists() or not any(
        (tmp_path / "mirror").iterdir()
    )


def test_flag_redo_exits_3_while_the_mirror_is_locked(tmp_path, capsys):
    src = _source(tmp_path)
    proc = _hold(tmp_path / "mirror")
    try:
        code = main(
            [
                "redo",
                "--connector",
                "local_files",
                "--set",
                f"path={src}",
                "--mirror",
                str(tmp_path / "mirror"),
                "--state",
                str(tmp_path / "state"),
            ]
        )
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    assert code == 3
    assert capsys.readouterr().out.strip().startswith("Locked: another kbforge run")
