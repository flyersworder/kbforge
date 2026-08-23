"""SQL over an OKF v0.2 bundle.

`load()` returns a DuckDB connection, deliberately unwrapped: an OKF bundle is
just a DuckDB database, and anything this package wrapped would be a worse
version of what DuckDB already offers."""

from __future__ import annotations

__version__ = "0.1.0"

from okfquery.load import EmptyMirrorError, load
from okfquery.schema import SCHEMA_SQL

__all__ = ["EmptyMirrorError", "SCHEMA_SQL", "__version__", "load"]
