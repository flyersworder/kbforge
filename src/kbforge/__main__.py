"""`python -m kbforge ...` — the walking-skeleton entry point.

Connector selection and config are fully generic: the connector is resolved by
name from the registry (built-in or entry-point-discovered), and its config comes
from repeatable `--set KEY=VALUE` pairs. Nothing here knows a connector's config
shape, so a third-party plugin is usable with no change to this file."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

import pluggy
import yaml
from pydantic import ValidationError

from kbforge.canonical import FetchContractError, StabilityError
from kbforge.grounding import load_grounding, problems_for
from kbforge.pipeline import (
    Aborted,
    ConfigError,
    ConnectorProtocol,
    NoOp,
    Published,
    PublisherProtocol,
    run,
)
from kbforge.publishers._http import PublishError
from kbforge.publishers.forge import PathError
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


def _parse_settings(pairs: list[str]) -> dict:
    """`KEY=VALUE` pairs into a config dict; VALUE is YAML-typed so `max_commits=5`
    is an int, `ref=HEAD` a str, and `ignore_globs=[a, b]` a list."""
    config: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"--set expects KEY=VALUE, got {pair!r}")
        config[key] = yaml.safe_load(raw)
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbforge")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list available connectors")

    r = sub.add_parser("run", help="run the pipeline once")
    r.add_argument("--connector", required=True)
    r.add_argument(
        "--set",
        action="append",
        default=[],
        dest="settings",
        metavar="KEY=VALUE",
        help="connector config (repeatable); values are YAML-typed",
    )
    r.add_argument("--synthesizer", choices=["stub", "llm"], default="stub")
    r.add_argument(
        "--llm-set",
        action="append",
        default=[],
        dest="llm_settings",
        metavar="KEY=VALUE",
        help="LLM synthesizer config (repeatable); YAML-typed values",
    )
    r.add_argument(
        "--publisher",
        default="dry-run",
        help="publisher name (default: dry-run); see `kbforge list`",
    )
    r.add_argument(
        "--publish-set",
        action="append",
        default=[],
        dest="publish_settings",
        metavar="KEY=VALUE",
        help="publisher config (repeatable); values are YAML-typed",
    )
    r.add_argument("--mirror", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--state", required=True)
    r.add_argument(
        "--grounding",
        default=None,
        metavar="PATH",
        help="grounding subject map (YAML); see docs/architecture.md §7.1",
    )
    args = parser.parse_args(argv)

    pm = build_registry()
    connectors = _connectors(pm)
    publishers = _publishers(pm)

    if args.cmd == "list":
        for name in sorted(connectors):
            info = connectors[name].kbforge_connector_info()
            print(f"{name}\t{info.source_system}")
        print("synthesizers:")
        print("  stub\tdeterministic, no LLM")
        print("  llm\tPydantic AI (needs kbforge[llm])")
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
    if args.publisher == "dry-run":
        publish_config.setdefault("out_dir", args.out)

    # Fail fast: a bad publisher config should cost a second, not a full
    # fetch+synthesize. Third-party publishers predating the hook skip this.
    validate = getattr(
        publishers[args.publisher], "kbforge_validate_publish_config", None
    )
    publish_problems = validate(publish_config) if validate else []
    if publish_problems:
        print("; ".join(publish_problems))
        return 2

    if args.synthesizer == "llm":
        from kbforge.llm_synthesizer import LLMConfig, LLMSynthesizer

        try:
            llm_cfg = LLMConfig(**_parse_settings(args.llm_settings))
        except (ValueError, TypeError) as exc:
            print(str(exc))
            return 2
        problems = llm_cfg.validate_env()
        if problems:
            print("; ".join(problems))
            return 2
        try:
            synthesizer = LLMSynthesizer(llm_cfg)
        except ImportError as exc:
            print(str(exc))
            return 2
    else:
        synthesizer = None  # run() defaults to StubSynthesizer

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

    try:
        result = run(
            connectors[args.connector],
            publishers[args.publisher],
            config=config,
            mirror=args.mirror,
            state_dir=args.state,
            publish_config=publish_config,
            synthesizer=synthesizer,
            grounding_config=grounding_config,
        )
    except ConfigError as exc:
        print(str(exc))
        return 2
    except (FetchContractError, StabilityError) as exc:
        # A connector-contract violation, not an operator mistake — but the
        # operator is who sees it, and a traceback tells them nothing about
        # which plugin to report it against. StabilityError is caught here too:
        # it has always escaped as a traceback, and fixing the surfacing only
        # for the newer law would leave the older one worse for no reason.
        print(f"Connector contract violation ({args.connector}): {exc}")
        return 2
    except (PublishError, PathError) as exc:
        # The mirror never advanced, so the next run retries this same change.
        # Catching the PublishError base rather than ForgeError specifically:
        # TreeListingTruncatedError is a publish failure with carefully worded
        # remediation advice, and naming subclasses one by one had already let
        # it escape as a traceback. PathError lands here too — a connector
        # emitting a traversing file key is the case safe_join() exists for,
        # and it deserves a message rather than a traceback.
        print(f"Publish failed: {exc}")
        return 1

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
