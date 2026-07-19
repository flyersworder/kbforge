"""`python -m kbforge run ...` — the walking-skeleton entry point."""

from __future__ import annotations

import argparse

from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import Aborted, NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the pipeline once over a local folder")
    r.add_argument("--source", required=True)
    r.add_argument("--mirror", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--state", required=True)
    args = parser.parse_args(argv)

    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config={"path": args.source},
        mirror=args.mirror,
        state_dir=args.state,
        publish_config={"out_dir": args.out},
    )
    if isinstance(result, Published):
        print(f"Published: {result.url}")
        return 0
    if isinstance(result, NoOp):
        print("NoOp: no change detected; no MR opened.")
        return 0
    if isinstance(result, Aborted):
        print(f"Aborted: {len(result.failures)} validation failure(s):")
        for f in result.failures:
            print(f"  [{f.law}] {f.concept_path}: {f.message}")
        return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
