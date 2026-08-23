"""okfquery against a bundle kbforge actually produced.

The unit tests prove the parser handles the shapes this package expects. This
one proves those are the shapes kbforge emits -- it is the only thing standing
between a change to synthesize._render, or to validate's reserved rule, and a
loader that silently reports wrong counts. It also guards `parse.RESERVED` and
`parse.OKF_OWNED`, the two constants okfquery copies from kbforge rather than
import: `test_replicated_constants_have_not_drifted` checks the copies still
equal the originals, and the facets assertion in
`test_every_rendered_concept_loads_without_problems`'s bundle checks the values
those constants gate, not just their identity.

kbforge is a dev dependency only. Nothing under src/okfquery imports it, which is
what makes "reads any OKF v0.2 bundle" true rather than aspirational."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from okfquery import load
from okfquery.parse import is_reserved

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
    # Subtract fenceless reserved names rather than comparing against every
    # concepts/**/*.md: if kbforge ever emits an OKF §8 directory listing, that
    # file is correctly excluded from `concepts`, and this test must not fail
    # against a correct loader for saying so.
    on_disk = {
        p.relative_to(bundle).as_posix()
        for p in (bundle / "concepts").rglob("*.md")
        if not is_reserved(p.name, p.read_text("utf-8"))
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


def test_facets_are_exactly_the_source_fields_okf_does_not_own(tmp_path):
    # b.md's only non-OKF frontmatter key is `owner`. If kbforge's OKF_OWNED
    # ever stops covering a key it should, that key leaks into facets here and
    # this catches it -- the same failure mode the dual-carrier rule exists to
    # prevent, one layer down.
    bundle = _build_bundle(tmp_path)
    con = load(bundle)
    raw = con.execute(
        "select facets from concepts where path = 'concepts/b/overview.md'"
    ).fetchone()[0]
    assert json.loads(raw) == {"owner": "platform"}


def test_replicated_constants_have_not_drifted():
    """The duplication in parse.py is justified by this test existing.

    okfquery cannot import kbforge at runtime (that is the whole boundary), so it
    copies two constants. A copy with no equality check is just a stale value
    waiting to happen -- the same defect the dual-carrier rule exists to prevent.
    """
    from kbforge.synthesize import OKF_OWNED as KB_OWNED
    from kbforge.validate import _RESERVED as KB_RESERVED
    from okfquery import parse

    assert parse.RESERVED == KB_RESERVED
    assert parse.OKF_OWNED == KB_OWNED
