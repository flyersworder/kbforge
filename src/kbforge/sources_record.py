"""`<state>/sources.json`: what project mode remembers per source name.

The connector name and slot digest a source last ran with (so `reset` still
works for a name since removed from the file, and a config change is no longer
silent), the systems its last run fetched (ownership, architecture.md
§7.5), and when it last published. Written only by project mode; `Cursor` and
`ChunkRecord` keep their shapes, so a flag-mode kbforge still reads the state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, TypeAdapter

FILE = "sources.json"


class SourceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector: str
    digest: str
    systems: list[str]
    last_published: str | None = None
    """ISO 8601 with offset, seconds precision."""


_ENTRIES = TypeAdapter(dict[str, SourceEntry])


def read_sources(state: Path) -> dict[str, SourceEntry]:
    path = Path(state) / FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    return _ENTRIES.validate_json(raw)


def write_sources(state: Path, entries: dict[str, SourceEntry]) -> None:
    """Atomic: a temp file in the same directory, then `os.replace`, so a crash
    leaves the old record or the new one, never half of one."""
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    data = _ENTRIES.dump_json(entries, indent=2) + b"\n"
    fd, tmp = tempfile.mkstemp(dir=state, prefix=".sources-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, state / FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
