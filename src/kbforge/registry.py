"""Plugin registration. Real Pluggy, scoped to the walking skeleton's in-tree
connectors + publisher. Entry-point discovery and multi-connector
`subset_hook_caller` dispatch (architecture §5.4) are deferred."""

from __future__ import annotations

import pluggy

from kbforge.connectors.git_commits import GitCommitsConnector
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.hookspecs import (
    CONNECTOR_ENTRYPOINTS,
    PROJECT,
    PUBLISHER_ENTRYPOINTS,
    ConnectorSpec,
    PublisherSpec,
)
from kbforge.publishers.dry_run import DryRunPublisher


def build_registry() -> pluggy.PluginManager:
    pm = pluggy.PluginManager(PROJECT)
    pm.add_hookspecs(ConnectorSpec)
    pm.add_hookspecs(PublisherSpec)
    # In-tree built-ins are always available and registered explicitly.
    pm.register(LocalFilesConnector())
    pm.register(GitCommitsConnector())
    pm.register(DryRunPublisher())
    # Third-party plugins: any installed package advertising the kbforge.connectors
    # or kbforge.publishers entry-point group is discovered without editing this
    # file (§5.4).
    pm.load_setuptools_entrypoints(CONNECTOR_ENTRYPOINTS)
    pm.load_setuptools_entrypoints(PUBLISHER_ENTRYPOINTS)
    return pm
