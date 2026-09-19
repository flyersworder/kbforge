"""Bundle on disk -> a DuckDB connection with the schema filled.

Ephemeral by design: nothing is cached and nothing is written, so the tables
cannot disagree with the files they came from. That is also why no query index is
committed into a bundle -- an index derived from `ProposedChange.concepts` would
be a THIRD carrier of the same concept, and kbforge already has two. The OKF
root `index.md` that `okfquery index` writes is not that: it is navigation,
rebuilt from the merged files, and `--check` fails CI when it drifts."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from okfquery.parse import is_reserved, parse
from okfquery.schema import SCHEMA_SQL


class EmptyMirrorError(Exception):
    """A --mirror path holding no document slots. Raised rather than passed
    through, because read_json_auto's own error on a zero-match glob names a
    pattern, not the directory the user typed."""


def scan(bundle: Path) -> list[Path]:
    """Concept files, sorted. Reserved OKF artifacts are excluded here and
    nowhere else, so the count invariant has exactly one definition.

    The glob is `concepts/**/*.md` because that is where kbforge's
    `synthesize.concept_path` puts everything -- which keeps a bundle repo's own
    README.md and docs/ out of the tables."""
    root = bundle / "concepts"
    if not root.is_dir():
        return []
    # errors="replace", not the bare default: a mis-encoded file is exactly the
    # pathology this tool exists to surface. Raising here would abort the whole
    # load over one file; U+FFFD replacement characters let it through to
    # `parse()`, which turns it into a `concepts` row (NULLs where the decode
    # broke the frontmatter) plus a `problems` row, same as any other bad file.
    return [
        p
        for p in sorted(root.rglob("*.md"))
        if not is_reserved(p.name, p.read_text("utf-8", errors="replace"))
    ]


def _mirror_view(con: duckdb.DuckDBPyConnection, mirror: Path) -> None:
    # Shallow, never `**/*.json`: grounding.SIDECAR_DIR keeps flat
    # {doc_id: content_hash} maps in <mirror>/_grounding/, and unioning those
    # into a CanonicalDocument view would wreck read_json_auto's inferred schema.
    slots = sorted(mirror.glob("*.json"))
    if not slots:
        raise EmptyMirrorError(
            f"mirror {mirror} holds no document slots (*.json); "
            "nothing to attach as the `mirror` view"
        )
    pattern = str(mirror / "*.json").replace("'", "''")
    con.execute(f"CREATE VIEW mirror AS SELECT * FROM read_json_auto('{pattern}')")


def load(
    bundle: Path,
    mirror: Path | None = None,
    *,
    database: str = ":memory:",
) -> duckdb.DuckDBPyConnection:
    """An OKF v0.2 bundle as a DuckDB connection.

    Returns the connection itself, unwrapped. An OKF bundle is just a DuckDB
    database: COPY ... TO 'x.parquet', ATTACH, joining against your own CSV, and
    INSTALL fts over `body` all work because the real object comes back.

    `database` exists for `okfquery shell`, which needs the same tables in a file
    the duckdb CLI can open. Everything else takes the default.
    """
    bundle = Path(bundle)
    con = duckdb.connect(database)
    # Set before anything is inserted: TIMESTAMPTZ values render in the session
    # timezone, and a bundle should read the same on every machine.
    con.execute("SET timezone = 'UTC'")
    con.execute(SCHEMA_SQL)

    concepts: list[tuple] = []
    sources: list[tuple] = []
    links: list[tuple] = []
    problems: list[tuple] = []

    for file in scan(bundle):
        path = file.relative_to(bundle).as_posix()
        # Same errors="replace" as scan(), and for the same reason: one
        # mis-encoded file must produce a row, not a traceback that aborts
        # every other concept in the bundle along with it.
        concept = parse(file.read_text("utf-8", errors="replace"))
        concepts.append(
            (
                path,
                concept.type,
                concept.title,
                concept.description,
                concept.generated_by,
                concept.generated_at,
                json.dumps(concept.facets) if concept.facets else None,
                concept.body,
            )
        )
        for ordinal, source in enumerate(concept.sources):
            sources.append(
                (
                    path,
                    ordinal,
                    source["id"],
                    source["resource"],
                    source["content_hash"],
                )
            )
        links.extend((path, target) for target in concept.links)
        problems.extend((path, p.kind, p.detail) for p in concept.problems)

    if concepts:
        con.executemany(
            "INSERT INTO concepts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", concepts
        )
    if sources:
        con.executemany("INSERT INTO sources VALUES (?, ?, ?, ?, ?)", sources)
    if links:
        con.executemany("INSERT INTO links VALUES (?, ?)", links)
    if problems:
        con.executemany("INSERT INTO problems VALUES (?, ?, ?)", problems)

    if mirror is not None:
        _mirror_view(con, Path(mirror))
    return con
