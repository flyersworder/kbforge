"""Makes `packages/kbforge-mcp/tests` the importable package `tests`.

`test_client.py` (and Task 5's `test_connector.py`) do `from tests.fake_server
import mcp`. Without this file, pytest's default import mode collects modules
under this directory by their bare names (there is no `tests` package to import
from anywhere on `sys.path`), and that import fails with `ModuleNotFoundError:
No module named 'tests'`.

With this file present, `packages/kbforge-mcp` -- not `.../tests` itself -- is
what gets inserted onto `sys.path`, and every module here is collected dotted
under `tests.*`, which is what makes `tests.fake_server` resolve.

This resolves deterministically despite the repo-root `tests/` directory: that
one has no `__init__.py` of its own, and PEP 420 only synthesizes a namespace
package when no *regular* package by that name is found anywhere on the path --
a regular package (this file) wins.

Deliberately not a `conftest.py`: the repo-root `conftest.py` already
registers `--run-live` and the `live` marker -- and it must live at the repo
root, not in `tests/`, precisely so it is an ancestor of every collected
path, this one included. A second `pytest_addoption` for the same flag
raises `ValueError: option names {'--run-live'} already added`.
"""

from __future__ import annotations
