"""kbforge — agent-first knowledge bases, forged from your systems of record.

The production half of the Open Knowledge Format: connectors, canonicalization,
diff, provenance, and publish. See docs/architecture.md.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from installed package metadata rather than restating the number
    # here: pyproject.toml is the single source of truth, so the two cannot
    # drift (they did — this said 0.1.0 for the whole of the 0.2.0 release).
    __version__ = version("kbforge")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
