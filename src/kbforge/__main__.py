"""`python -m kbforge ...` — the walking-skeleton entry point.

Connector selection and config are fully generic: the connector is resolved by
name from the registry (built-in or entry-point-discovered), and its config comes
from repeatable `--set KEY=VALUE` pairs. Nothing here knows a connector's config
shape, so a third-party plugin is usable with no change to this file."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pluggy
import yaml
from pydantic import ValidationError

from kbforge.assembly import (
    SetupError,
    build_synthesizer,
    check_publisher,
    execute,
    synthesizer_grounds,
)
from kbforge.chunking import load_chunking
from kbforge.grounding import load_grounding, problems_for
from kbforge.links import links_problems, load_links
from kbforge.lock import mirror_lock
from kbforge.pipeline import (
    ConnectorProtocol,
    PublisherProtocol,
    redo,
    run,
)
from kbforge.registry import build_registry


def _connectors(pm: pluggy.PluginManager) -> dict[str, ConnectorProtocol]:
    """name -> connector instance (a connector implements kbforge_fetch)."""
    return {
        p.kbforge_connector_info().name: cast(ConnectorProtocol, p)
        for p in pm.get_plugins()
        if hasattr(p, "kbforge_fetch")
    }


def _publishers(pm: pluggy.PluginManager) -> dict[str, PublisherProtocol]:
    """name -> publisher instance (a publisher implements kbforge_publish).

    Keyed by name rather than "first plugin found": with three publishers
    registered, positional lookup would make the destination depend on plugin
    registration order.
    """
    return {
        p.kbforge_publisher_info().name: cast(PublisherProtocol, p)
        for p in pm.get_plugins()
        if hasattr(p, "kbforge_publish")
    }


SYNTHESIZERS = {
    "stub": "deterministic, no LLM",
    "llm": "Pydantic AI (needs kbforge[llm])",
    "describe": "stub body, model-written description and tags (needs kbforge[llm])",
}
"""One table for `--synthesizer` choices and `kbforge list`: two hand-kept
lists let `describe` reach the first and not the second."""


def _parse_settings(pairs: list[str]) -> dict:
    """`KEY=VALUE` pairs into a config dict; VALUE is YAML-typed so `max_commits=5`
    is an int, `ref=HEAD` a str, and `ignore_globs=[a, b]` a list."""
    config: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        config[key] = yaml.safe_load(raw)
        if _drops_a_comment(raw):
            raise ValueError(
                f"{key}: YAML reads ' #' as a comment and would drop the rest of "
                f"{raw!r}; quote the value: {key}='\"...\"'"
            )
    return config


def _drops_a_comment(raw: str) -> bool:
    """True when YAML discards part of `raw` as a comment. Comments are the one
    thing the scanner emits no token for, so any non-blank character outside
    every token's span was dropped."""
    covered = [False] * len(raw)
    for token in yaml.scan(raw):
        for i in range(token.start_mark.index, token.end_mark.index):
            covered[i] = True
    return any(not c and not ch.isspace() for c, ch in zip(covered, raw, strict=True))


def _source_args(p: argparse.ArgumentParser) -> None:
    """What `run` and `redo` both need to find the connector instance's state
    and ask the publisher about open review requests."""
    p.add_argument("--connector", required=True)
    p.add_argument(
        "--set",
        action="append",
        default=[],
        dest="settings",
        metavar="KEY=VALUE",
        help="connector config (repeatable); values are YAML-typed",
    )
    p.add_argument(
        "--publisher",
        default="dry-run",
        help="publisher name (default: dry-run); see `kbforge list`",
    )
    p.add_argument(
        "--publish-set",
        action="append",
        default=[],
        dest="publish_settings",
        metavar="KEY=VALUE",
        help="publisher config (repeatable); values are YAML-typed",
    )
    p.add_argument("--mirror", required=True)
    p.add_argument("--state", required=True)


def _locked_call(
    args: argparse.Namespace, call: Callable[[], Any]
) -> Callable[[], Any]:
    """`call` under the mirror lock; `execute` turns MirrorLocked into exit 3."""

    def locked() -> Any:
        with mirror_lock(Path(args.mirror)):
            return call()

    return locked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbforge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list available connectors")

    r = sub.add_parser("run", help="run the pipeline once")
    _source_args(r)
    r.add_argument("--out", required=True)
    r.add_argument(
        "--synthesizer",
        choices=list(SYNTHESIZERS),
        default="stub",
        help="stub (default), llm, or describe (stub body + model-written "
        "description and tags)",
    )
    r.add_argument(
        "--llm-set",
        action="append",
        default=[],
        dest="llm_settings",
        metavar="KEY=VALUE",
        help="LLM synthesizer config (repeatable); YAML-typed values",
    )
    r.add_argument(
        "--grounding",
        default=None,
        metavar="PATH",
        help="grounding subject map (YAML); see docs/architecture.md §7.1",
    )
    r.add_argument(
        "--chunking",
        default=None,
        metavar="PATH",
        help="chunked review config (YAML: max_concepts, group_by); "
        "see docs/architecture.md §7.2",
    )
    r.add_argument(
        "--links",
        default=None,
        metavar="PATH",
        help="editorial links (YAML); see docs/architecture.md §7.4",
    )
    rd = sub.add_parser(
        "redo", help="roll the last chunk back so the next run proposes it again"
    )
    _source_args(rd)
    # Accepted so a `run` command line can be reused as is, but never needed:
    # redo reads and writes state and the mirror, and never publishes.
    rd.add_argument("--out", default=None)
    args = parser.parse_args(argv)

    pm = build_registry()
    connectors = _connectors(pm)
    publishers = _publishers(pm)

    if args.cmd == "list":
        for name in sorted(connectors):
            info = connectors[name].kbforge_connector_info()
            print(f"{name}\t{info.source_system}")
        print("synthesizers:")
        for name, summary in SYNTHESIZERS.items():
            print(f"  {name}\t{summary}")
        print("publishers:")
        for name in sorted(publishers):
            info = publishers[name].kbforge_publisher_info()
            print(f"  {name}\t{info.source_system}")
        return 0

    if args.connector not in connectors:
        available = ", ".join(sorted(connectors)) or "(none)"
        print(f"unknown connector {args.connector!r}; available: {available}")
        return 2

    if args.publisher not in publishers:
        available = ", ".join(sorted(publishers)) or "(none)"
        print(f"unknown publisher {args.publisher!r}; available: {available}")
        return 2

    try:
        config = _parse_settings(args.settings)
    except ValueError as exc:
        print(str(exc))
        return 2

    try:
        publish_config = _parse_settings(args.publish_settings)
    except ValueError as exc:
        print(str(exc))
        return 2
    # The built-in dry-run publisher is wired to --out; forge publishers take
    # their whole config from --publish-set.
    if args.publisher == "dry-run" and args.out is not None:
        publish_config.setdefault("out_dir", args.out)

    # Fail fast: a bad publisher config should cost a second, not a full
    # fetch+synthesize. Third-party publishers predating the hook skip this.
    # So does redo under dry-run: dry-run's config is only where to write, and
    # redo never publishes (a forge publisher's config is still checked, since
    # redo asks the forge whether a request is open).
    skip_check = args.cmd == "redo" and args.publisher == "dry-run"
    publish_problems = (
        []
        if skip_check
        else check_publisher(publishers[args.publisher], args.publisher, publish_config)
    )
    if publish_problems:
        print("; ".join(publish_problems))
        return 2

    if args.cmd == "redo":
        outcome = execute(
            _locked_call(
                args,
                lambda: redo(
                    connectors[args.connector],
                    publishers[args.publisher],
                    config=config,
                    mirror=args.mirror,
                    state_dir=args.state,
                    publish_config=publish_config,
                ),
            ),
            args.connector,
        )
        print(*outcome.lines, sep="\n")
        return outcome.code

    try:
        synthesizer = build_synthesizer(
            args.synthesizer,
            # `stub` takes no settings, so a stray --llm-set is not parsed for it.
            _parse_settings(args.llm_settings) if args.synthesizer != "stub" else {},
            Path(args.mirror),
        )
    except (ValueError, SetupError) as exc:
        print(str(exc))
        return 2

    try:
        grounding_config = load_grounding(
            Path(args.grounding) if args.grounding else None
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        # A missing file, non-UTF-8 bytes, unparseable YAML and a rejected shape
        # are all operator mistakes about one named path, so they get the
        # surrounding style — a sentence naming the file and exit 2 — rather than
        # a traceback. The shape problems `problems_for` reports below are already
        # handled that way; these four reached the terminal raw.
        # `UnicodeDecodeError` is listed explicitly because it is a `ValueError`,
        # not an `OSError`, so `read_text("utf-8")` slips past the other three.
        print(f"grounding config {args.grounding}: {exc}")
        return 2
    problems = problems_for(grounding_config)
    if problems:
        print(f"grounding config: {'; '.join(problems)}")
        return 2

    if grounding_config.rules and not synthesizer_grounds(args.synthesizer):
        print(
            "grounding rules are validated but inactive: the "
            f"{args.synthesizer} synthesizer does not ground; use --synthesizer llm"
        )

    try:
        chunking = load_chunking(Path(args.chunking) if args.chunking else None)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        # Same four operator mistakes, same handling, as --grounding above.
        print(f"chunking config {args.chunking}: {exc}")
        return 2

    try:
        links_config = load_links(Path(args.links) if args.links else None)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        # Same four operator mistakes, same handling, as --grounding above.
        print(f"links config {args.links}: {exc}")
        return 2
    if links_config is not None:
        problems = links_problems(links_config)
        if problems:
            print(f"links config: {'; '.join(problems)}")
            return 2

    outcome = execute(
        _locked_call(
            args,
            lambda: run(
                connectors[args.connector],
                publishers[args.publisher],
                config=config,
                mirror=args.mirror,
                state_dir=args.state,
                publish_config=publish_config,
                synthesizer=synthesizer,
                grounding_config=grounding_config,
                chunking=chunking,
                links_config=links_config,
            ),
        ),
        args.connector,
    )
    print(*outcome.lines, sep="\n")
    return outcome.code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
