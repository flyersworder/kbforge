"""Dry-run publisher: writes the proposal to a local directory instead of opening
an MR. Ships in core (§5.2). Never merges — see the github/gitlab publishers for
the real thing."""

from __future__ import annotations

from pathlib import Path

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers.forge import safe_join
from kbforge.publishers.summary import summary_md


class DryRunPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="dry-run", version="0.1.0", source_system="local filesystem"
        )

    @hookimpl
    def kbforge_validate_publish_config(self, config: dict) -> list[str]:
        return [] if config.get("out_dir") else ["'out_dir' is required"]

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        branch = change.branch_hint.replace("/", "-")
        out_dir = Path(config["out_dir"]) / branch
        out_dir.mkdir(parents=True, exist_ok=True)
        # safe_join on both loops, exactly as the forge publishers apply it:
        # every path here is connector/synthesizer output, so a native_id of
        # '../../../etc/foo' would otherwise write or unlink outside out_dir.
        # Deleting outside the output directory is the sharper edge, but the
        # asymmetry of guarding one loop and not the other is unjustifiable.
        for rel, content in change.files.items():
            dest = out_dir / safe_join("", rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, "utf-8")
        for rel in change.files_removed:
            # missing_ok: the dry-run directory may be fresh, and deletion must
            # be idempotent across re-runs exactly as it is on a forge.
            (out_dir / safe_join("", rel)).unlink(missing_ok=True)
        (out_dir / "MR_BODY.md").write_text(summary_md(change.summary), "utf-8")
        return str(out_dir)  # a path, not a merge — never merges
