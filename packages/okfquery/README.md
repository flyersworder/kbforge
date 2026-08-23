# okfquery

SQL over an [OKF v0.2](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle, via DuckDB. `okfquery` loads a published bundle into an in-memory DuckDB
connection and hands the connection back — no daemon, no cache, no committed
artifact. It depends on no kbforge code at runtime, so it reads any OKF v0.2
bundle, not only ones a kbforge run produced.

See `docs/design/2026-08-23-okfquery-design.md` in the kbforge repository for
the design note this package implements.
