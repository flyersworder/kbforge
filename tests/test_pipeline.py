from pathlib import Path

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher

DOC = """---
type: application
title: App X
owner: team-a
---
App X does things.
"""


def _dirs(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    return (
        {"path": str(src)},
        str(tmp_path / "mirror"),
        str(tmp_path / "state"),
        {"out_dir": str(tmp_path / "out")},
    )


def test_bootstrap_run_publishes(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(result, Published)
    assert (Path(result.url) / "concepts/x/overview.md").exists()


def test_second_identical_run_is_noop(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    first = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(first, Published)
    second = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(second, NoOp)  # mirror committed → no change → no MR


def test_link_to_unchanged_sibling_survives(tmp_path: Path):
    # A links to B; both bootstrapped. Then only A changes. The A→B link must
    # still resolve — B is unchanged-but-present — not be dropped (§4.4 law 2).
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text("---\ntype: application\ntitle: B\n---\nB.\n", "utf-8")
    a_body = "---\ntype: application\ntitle: {t}\nrelations:\n  - b.md\n---\n{x}\n"
    (src / "a.md").write_text(a_body.format(t="A", x="A one"), "utf-8")
    config = {"path": str(src)}
    mirror = str(tmp_path / "mirror")
    state = str(tmp_path / "state")
    pub = {"out_dir": str(tmp_path / "out")}
    run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )  # bootstrap A and B
    (src / "a.md").write_text(a_body.format(t="A2", x="A two"), "utf-8")
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )  # only A changed
    assert isinstance(result, Published)
    published_a = Path(result.url) / "concepts/a/overview.md"
    assert "concepts/b/overview.md" in published_a.read_text("utf-8")
