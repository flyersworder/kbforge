from pathlib import Path

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.dry_run import DryRunPublisher


def _change():
    return ProposedChange(
        branch_hint="sync/local-files",
        files={"concepts/x/overview.md": "# X\n"},
        summary=ChangeSummary(claims_added=["concepts/x/overview.md"]),
    )


def test_publish_writes_files_and_body(tmp_path: Path):
    out = DryRunPublisher().kbforge_publish(_change(), {"out_dir": str(tmp_path)})
    out_dir = Path(out)
    assert (out_dir / "concepts/x/overview.md").read_text("utf-8") == "# X\n"
    assert (out_dir / "MR_BODY.md").exists()


def test_publish_is_idempotent(tmp_path: Path):
    cfg = {"out_dir": str(tmp_path)}
    a = DryRunPublisher().kbforge_publish(_change(), cfg)
    b = DryRunPublisher().kbforge_publish(_change(), cfg)
    assert a == b  # same branch → same dir, overwritten not duplicated


def test_removed_paths_are_deleted_from_the_output_directory(tmp_path):
    publisher = DryRunPublisher()
    config = {"out_dir": str(tmp_path)}
    publisher.kbforge_publish(
        ProposedChange(branch_hint="sync/x", files={"a.md": "A", "b.md": "B"}), config
    )

    publisher.kbforge_publish(
        ProposedChange(
            branch_hint="sync/x", files={"b.md": "B2"}, files_removed=["a.md"]
        ),
        config,
    )

    out = tmp_path / "sync-x"
    assert not (out / "a.md").exists()
    assert (out / "b.md").read_text() == "B2"
