"""Dry-run publisher: writes the proposal to a local directory instead of opening
an MR. Ships in core (§5.2). Never merges — see the github/gitlab publishers for
the real thing."""

from __future__ import annotations

from pathlib import Path

from kbforge.hookspecs import hookimpl
from kbforge.models import ConnectorInfo, ProposedChange
from kbforge.publishers.summary import summary_md


class DryRunPublisher:
    @hookimpl
    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="dry-run", version="0.1.0", source_system="local filesystem"
        )

    @hookimpl
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        branch = change.branch_hint.replace("/", "-")
        out_dir = Path(config["out_dir"]) / branch
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in change.files.items():
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, "utf-8")
        (out_dir / "MR_BODY.md").write_text(summary_md(change.summary), "utf-8")
        return str(out_dir)  # a path, not a merge — never merges
