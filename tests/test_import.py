import tomllib
from pathlib import Path

import pytest

import kbforge

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_package_imports_with_version():
    assert kbforge.__version__


def test_version_matches_pyproject():
    """The old `__version__ = "0.1.0"` literal sat one release behind
    pyproject.toml, and asserting mere truthiness could not see it."""
    if not PYPROJECT.exists():  # pragma: no cover - installed without the sdist
        pytest.skip("pyproject.toml not available")
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["version"]
    assert kbforge.__version__ == declared
