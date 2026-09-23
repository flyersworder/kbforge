"""The release guard: a tag publishes the distribution it names, and only that.

`uv build --all-packages` puts every workspace distribution in dist/. Before
this, the publish step uploaded all of them with skip-existing, so a kbforge
tag also published kbforge-sql 0.1.1 (2026-09-23) and the kbforge-sql tag's own
job then failed its guard. A companion bumped for a later release would have
gone out early the same way."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "check_release_target.py"
_spec = importlib.util.spec_from_file_location("check_release_target", _SCRIPT)
assert _spec is not None and _spec.loader is not None
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ARTIFACTS = [
    "kbforge-0.12.0-py3-none-any.whl",
    "kbforge-0.12.0.tar.gz",
    "kbforge_sql-0.1.1-py3-none-any.whl",
    "kbforge_sql-0.1.1.tar.gz",
    "kbforge_okfquery-0.3.0-py3-none-any.whl",
    "kbforge_okfquery-0.3.0.tar.gz",
    ".gitignore",  # uv build writes one into dist/
]


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    for name in ARTIFACTS:
        (d / name).write_text("x")
    return d


def _names(d: Path) -> set[str]:
    return {p.name for p in d.iterdir()}


def test_a_kbforge_tag_leaves_only_kbforge_to_publish(dist: Path):
    held = dist.parent / "held"
    guard.keep_only(dist, "kbforge", held)
    assert _names(dist) == {
        "kbforge-0.12.0-py3-none-any.whl",
        "kbforge-0.12.0.tar.gz",
        ".gitignore",
    }
    assert "kbforge_sql-0.1.1.tar.gz" in _names(held)


def test_a_companion_tag_leaves_only_the_companion(dist: Path):
    guard.keep_only(dist, "kbforge-sql", dist.parent / "held")
    assert _names(dist) == {
        "kbforge_sql-0.1.1-py3-none-any.whl",
        "kbforge_sql-0.1.1.tar.gz",
        ".gitignore",
    }


def test_kbforge_is_not_mistaken_for_a_prefix_of_a_companion(dist: Path):
    # `kbforge_sql-…` starts with `kbforge`; only the exact stem is kbforge.
    guard.keep_only(dist, "kbforge", dist.parent / "held")
    assert not any(n.startswith("kbforge_") for n in _names(dist))


def test_main_prunes_after_the_checks_pass(dist: Path, monkeypatch):
    monkeypatch.setattr(guard, "pypi_versions", lambda distribution: {"0.1.0"})
    assert guard.main(["x", "kbforge-sql-v0.1.1", str(dist)]) == 0
    assert _names(dist) == {
        "kbforge_sql-0.1.1-py3-none-any.whl",
        "kbforge_sql-0.1.1.tar.gz",
        ".gitignore",
    }


def test_a_failed_check_prunes_nothing(dist: Path, monkeypatch, capsys):
    monkeypatch.setattr(guard, "pypi_versions", lambda distribution: {"0.1.1"})
    with pytest.raises(SystemExit):
        guard.main(["x", "kbforge-sql-v0.1.1", str(dist)])
    assert "kbforge-sql 0.1.1 is already on PyPI" in capsys.readouterr().err
    assert _names(dist) == set(ARTIFACTS)
