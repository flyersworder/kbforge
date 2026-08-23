"""argparse over `load()`. Four verbs, no query logic of its own.

There is deliberately no --format parquet and no --output: DuckDB writes parquet
from inside the SQL (`COPY (...) TO 'out.parquet'`), and a second export path
here would be a worse version of a feature already shipping."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

from okfquery.load import EmptyMirrorError, load
from okfquery.schema import SCHEMA_SQL


def _bundle_missing(bundle: str) -> bool:
    """True when `bundle` has no `concepts/` directory to scan.

    `load()` must keep answering an empty bundle (no .md files under an
    existing `concepts/`) rather than erroring -- that is a real, if boring,
    OKF bundle. A *missing* `concepts/` is different: it means the path is
    wrong, and `scan()` silently returning `[]` for it would make `check`
    report "no problems" and exit 0 against a bundle that was never loaded at
    all. That is a CLI-level concern, not `load()`'s, so it is caught here.
    """
    return not (Path(bundle) / "concepts").is_dir()


def _connect(args: argparse.Namespace) -> duckdb.DuckDBPyConnection:
    mirror = Path(args.mirror) if args.mirror else None
    return load(Path(args.bundle), mirror=mirror)


def _emit(con: duckdb.DuckDBPyConnection, sql: str, fmt: str) -> None:
    if fmt == "table":
        print(con.sql(sql))
        return
    cursor = con.execute(sql)
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    if fmt == "json":
        print(json.dumps([dict(zip(columns, r)) for r in rows], default=str))
        return
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)


def _shell(args: argparse.Namespace) -> int:
    if _bundle_missing(args.bundle):
        print(f"no concepts/ directory under {args.bundle}", file=sys.stderr)
        return 2
    # subprocess, NEVER exec. exec replaces this process, so the TemporaryDirectory
    # finalizer would never run and a database holding every concept body would
    # outlive the session -- quietly breaking the ephemeral guarantee the whole
    # design rests on.
    with tempfile.TemporaryDirectory(prefix="okfquery-") as tmp:
        database = Path(tmp) / "bundle.duckdb"
        mirror = Path(args.mirror) if args.mirror else None
        try:
            load(Path(args.bundle), mirror=mirror, database=str(database)).close()
        except EmptyMirrorError as exc:
            print(exc, file=sys.stderr)
            return 2
        try:
            return subprocess.run(["duckdb", str(database)], check=False).returncode
        except FileNotFoundError:
            print(
                "the `duckdb` CLI is not on PATH. Install it "
                "(https://duckdb.org/docs/installation/) and run:\n"
                f"  duckdb {database}\n"
                "note: that file is deleted when this command exits.",
                file=sys.stderr,
            )
            return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="okfquery", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def bundle_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--bundle", default=".", help="bundle root (default: .)")
        p.add_argument("--mirror", default=None, help="kbforge mirror to attach")

    query = sub.add_parser("query", help="run one SQL statement")
    query.add_argument("sql")
    query.add_argument("--format", choices=("table", "json", "csv"), default="table")
    bundle_args(query)

    shell = sub.add_parser("shell", help="open the duckdb CLI on this bundle")
    bundle_args(shell)

    sub.add_parser("schema", help="print the DDL")

    check = sub.add_parser("check", help="exit 1 if any file failed to parse")
    bundle_args(check)

    args = parser.parse_args(argv)

    if args.command == "schema":
        print(SCHEMA_SQL.strip())
        return 0
    if args.command == "shell":
        return _shell(args)

    if _bundle_missing(args.bundle):
        print(f"no concepts/ directory under {args.bundle}", file=sys.stderr)
        return 2

    try:
        con = _connect(args)
    except EmptyMirrorError as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.command == "check":
        rows = con.execute(
            "select path, kind, detail from problems order by path, kind"
        ).fetchall()
        if not rows:
            print("no problems")
            return 0
        for path, kind, detail in rows:
            print(f"{path}: {kind}: {detail}", file=sys.stderr)
        return 1

    try:
        _emit(con, args.sql, args.format)
    except duckdb.Error as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
