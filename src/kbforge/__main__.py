"""`python -m kbforge run ...` — the walking-skeleton entry point."""

from __future__ import annotations

import argparse

from kbforge.connectors.git_commits import GitCommitsConnector
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.pipeline import Aborted, NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbforge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the pipeline once over a local source")
    r.add_argument(
        "--connector",
        choices=["local_files", "git_commits"],
        default="local_files",
    )
    r.add_argument("--source", required=True, help="folder (local_files) or repo (git)")
    r.add_argument("--ref", default="HEAD", help="git ref to sync (git_commits only)")
    r.add_argument("--mirror", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--state", required=True)
    args = parser.parse_args(argv)

    if args.connector == "git_commits":
        connector = GitCommitsConnector()
        config = {"repo": args.source, "ref": args.ref}
    else:
        connector = LocalFilesConnector()
        config = {"path": args.source}

    result = run(
        connector,
        DryRunPublisher(),
        config=config,
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
