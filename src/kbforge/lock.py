"""One run at a time per mirror.

The mirror is the state a run advances; two runs interleaving over it would
each diff against a baseline the other is about to move. The lock sits beside
the mirror directory (not inside it, which the mirror code owns) and is an OS
advisory lock, so a killed process releases it with no stale file to clean up.
The file itself is never deleted: unlinking a lock file races with a waiter
that already opened the old inode.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":  # pragma: no cover
    import msvcrt
else:
    import fcntl


class MirrorLocked(RuntimeError):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is held by another kbforge run")
        self.path = path


def lock_path(mirror: Path) -> Path:
    return Path(mirror).resolve().parent / "run.lock"


@contextmanager
def mirror_lock(mirror: Path) -> Generator[None]:
    path = lock_path(mirror)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        try:
            if sys.platform == "win32":  # pragma: no cover
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise MirrorLocked(path) from exc
        try:
            yield
        finally:
            if sys.platform == "win32":  # pragma: no cover
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
