"""`python -m kbforge ...` — the walking-skeleton entry point.

Connector selection and config are fully generic: the connector is resolved by
name from the registry (built-in or entry-point-discovered), and its config comes
from repeatable `--set KEY=VALUE` pairs. Nothing here knows a connector's config
shape, so a third-party plugin is usable with no change to this file."""

from __future__ import annotations

import argparse
from typing import cast

import pluggy
import yaml

from kbforge.pipeline import (
    Aborted,
    ConfigError,
    ConnectorProtocol,
    NoOp,
    Published,
    PublisherProtocol,
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


def _publisher(pm: pluggy.PluginManager) -> PublisherProtocol:
    for p in pm.get_plugins():
        if hasattr(p, "kbforge_publish"):
            return cast(PublisherProtocol, p)
    raise SystemExit("no publisher registered")


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
    r.add_argument("--mirror", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--state", required=True)
    args = parser.parse_args(argv)

    pm = build_registry()
    connectors = _connectors(pm)

    if args.cmd == "list":
        for name in sorted(connectors):
            info = connectors[name].kbforge_connector_info()
            print(f"{name}\t{info.source_system}")
        print("synthesizers:")
        print("  stub\tdeterministic, no LLM")
        print("  llm\tPydantic AI (needs kbforge[llm])")
        return 0

    if args.connector not in connectors:
        available = ", ".join(sorted(connectors)) or "(none)"
        print(f"unknown connector {args.connector!r}; available: {available}")
        return 2

    try:
        config = _parse_settings(args.settings)
    except ValueError as exc:
        print(str(exc))
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
        synthesizer = LLMSynthesizer(llm_cfg)
    else:
        synthesizer = None  # run() defaults to StubSynthesizer

    try:
        result = run(
            connectors[args.connector],
            _publisher(pm),
            config=config,
            mirror=args.mirror,
            state_dir=args.state,
            publish_config={"out_dir": args.out},
            synthesizer=synthesizer,
        )
    except ConfigError as exc:
        print(str(exc))
        return 2

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
