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
