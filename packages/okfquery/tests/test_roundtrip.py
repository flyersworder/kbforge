"""okfquery against a bundle kbforge actually produced.

The unit tests prove the parser handles the shapes this package expects. This
one proves those are the shapes kbforge emits -- it is the only thing standing
between a change to synthesize._render, or to validate's reserved rule, and a
loader that silently reports wrong counts.

kbforge is a dev dependency only. Nothing under src/okfquery imports it, which is
what makes "reads any OKF v0.2 bundle" true rather than aspirational."""

from __future__ import annotations

from pathlib import Path

import pytest

from okfquery import load

pytest.importorskip("kbforge")


def _build_bundle(tmp_path: Path) -> Path:
    """Run kbforge end to end over a tiny local_files source; return the bundle root.

    DryRunPublisher never opens a review request, so this needs no credentials
    and no network."""
    from kbforge.connectors.local_files import LocalFilesConnector
    from kbforge.pipeline import Published, run
    from kbforge.publishers.dry_run import DryRunPublisher

    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text(
        "---\ntype: application\ntitle: B\nowner: platform\n---\nB body.\n", "utf-8"
    )
    (src / "a.md").write_text(
        "---\ntype: application\ntitle: A\nrelations:\n  - b.md\n---\nA body.\n",
        "utf-8",
    )
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )
    assert isinstance(result, Published), result
    return Path(result.url)


def test_every_rendered_concept_loads_without_problems(tmp_path):
    bundle = _build_bundle(tmp_path)
    con = load(bundle)
    rows = con.execute("select path, kind, detail from problems").fetchall()
    assert rows == [], f"kbforge-rendered concepts failed to parse: {rows}"


def test_rendered_concepts_are_all_present(tmp_path):
    bundle = _build_bundle(tmp_path)
    con = load(bundle)
    on_disk = {
        p.relative_to(bundle).as_posix() for p in (bundle / "concepts").rglob("*.md")
    }
    loaded = {p for (p,) in con.execute("select path from concepts").fetchall()}
    assert loaded == on_disk


def test_sources_ordinal_zero_is_the_owning_anchor(tmp_path):
    # synthesize.assemble puts the owning anchor first. If that convention ever
    # changes, "which system owns this concept" silently starts lying.
    bundle = _build_bundle(tmp_path)
    con = load(bundle)
    owners = con.execute(
        "select distinct split_part(id, ':', 1) from sources where ordinal = 0"
    ).fetchall()
    assert owners == [("local_files",)]


def test_generated_at_survives_as_an_aware_timestamp(tmp_path):
    bundle = _build_bundle(tmp_path)
    con = load(bundle)
    nulls = con.execute(
        "select count(*) from concepts where generated_at is null"
    ).fetchone()[0]
    assert nulls == 0
