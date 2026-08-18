"""The two things the in-process fixture skips: entry-point discovery, and the
stdio transport's real framing over a real subprocess.

Every other test in this package drives the fixture server through
`client._server_override`, an in-process `Client` -- real protocol
serialization, but no transport at all. `test_a_real_stdio_subprocess_round_trips`
is the only test that launches an actual child process and talks to it over
stdin/stdout, so it is the only test that would catch a framing bug in
`open_session`'s stdio branch.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from kbforge.registry import build_registry


def test_the_connector_is_discovered_through_the_entry_point():
    # No edit to registry.py: an installed distribution advertising
    # kbforge.connectors is discovered by load_setuptools_entrypoints.
    names = [i.name for i in build_registry().hook.kbforge_connector_info()]
    assert "mcp" in names


def test_kbforge_list_shows_the_connector():
    out = subprocess.run(
        [sys.executable, "-m", "kbforge", "list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "mcp" in out


def test_a_real_stdio_subprocess_round_trips(tmp_path):
    # The in-process Client skips transport framing entirely. Launch the fixture
    # server as a subprocess so the stdio branch is actually exercised.
    #
    # This subprocess is a plain interpreter, not a pytest worker -- it does not
    # inherit pytest's sys.path insertions, so a bare `from tests.fake_server
    # import mcp` would fail with ModuleNotFoundError. Rather than routing a
    # PYTHONPATH through the connector's env-passthrough mechanism (which would
    # make this test also depend on that unrelated feature working), the
    # generated script makes its own import path explicit with a `sys.path`
    # insert. `packages/kbforge-mcp` (this file's parent's parent) is what makes
    # `tests` resolve as a package -- see tests/__init__.py -- so that is what
    # gets inserted, not `tests` itself.
    pkg_root = Path(__file__).resolve().parent.parent
    server = tmp_path / "server.py"
    server.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(pkg_root)!r})\n"
        "from tests.fake_server import mcp\n"
        "if __name__ == '__main__':\n"
        "    mcp.run()\n"
    )
    from kbforge_mcp.connector import CONNECTOR

    cfg = {
        "system": "fixture",
        "transport": {
            "kind": "stdio",
            "command": sys.executable,
            "args": [str(server)],
        },
        "static_ids": ["docs/retention.md"],
        "read": {"tool": "read_doc", "id_arg": "path"},
    }
    # `open_session`'s stdio branch spawns the subprocess and tears it down in
    # a `finally` on every exit path (see client.py's __aenter__/__aexit__
    # guards), so a raise out of kbforge_fetch would still leave no orphan.
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert [d.doc_id for d in docs] == ["fixture:docs/retention"]
