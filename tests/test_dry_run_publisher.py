from pathlib import Path

import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge.publishers.forge import PathError


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


def test_a_traversing_removal_cannot_unlink_outside_the_output_directory(tmp_path):
    """files_removed entries come from concept_path(doc_id), which sanitises
    nothing beyond .strip('/') — so a connector-supplied native_id of
    '../../../etc/foo' would unlink outside out_dir. The forge publishers guard
    exactly this with safe_join."""
    victim = tmp_path / "victim.md"
    victim.write_text("do not delete", "utf-8")
    out_dir = tmp_path / "out"

    with pytest.raises(PathError):
        DryRunPublisher().kbforge_publish(
            ProposedChange(branch_hint="sync/x", files_removed=["../../victim.md"]),
            {"out_dir": str(out_dir)},
        )

    assert victim.read_text("utf-8") == "do not delete"


def test_a_traversing_file_key_cannot_write_outside_the_output_directory(tmp_path):
    """Pre-existing gap, closed alongside the removal one: guarding deletes but
    not writes would be an unjustifiable asymmetry."""
    out_dir = tmp_path / "out"

    with pytest.raises(PathError):
        DryRunPublisher().kbforge_publish(
            ProposedChange(branch_hint="sync/x", files={"../../pwned.md": "evil"}),
            {"out_dir": str(out_dir)},
        )

    assert not (tmp_path / "pwned.md").exists()
