"""Placeholder for the `kbforge.connectors` entry point.

`pluggy.PluginManager.load_setuptools_entrypoints` (called from
`kbforge.registry.build_registry` on every run) resolves the
`kbforge.connectors = mcp = "kbforge_mcp.connector:CONNECTOR"` entry point
declared in `pyproject.toml` by importing this module and calling
`register()` on whatever it finds -- eagerly, on every invocation, whether or
not the mcp connector is the one in use. That means this module must exist
and be importable from the moment the entry point is declared, even though
the real connector (implementing `kbforge_fetch`, `kbforge_normalize`, etc.)
is a later task.

`CONNECTOR` implements none of the `kbforge.hookspecs.ConnectorSpec` hooks
yet, so pluggy registers it as an inert plugin: `kbforge.__main__._connectors`
filters candidates with `hasattr(p, "kbforge_fetch")`, which this object
fails, so it is invisible to `kbforge list` / `kbforge run` until the real
connector lands.
"""

from __future__ import annotations


class _NotYetImplementedConnector:
    """No hook implementations yet -- registers as a no-op pluggy plugin."""


CONNECTOR = _NotYetImplementedConnector()
