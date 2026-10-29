"""What flag mode and project mode share: building the pieces of a run and
turning its result (or failure) into the lines and exit code the CLI prints.

Lifted out of `__main__.main()` so the two modes cannot drift: a message fixed
here is fixed for both."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pluggy

from kbforge.canonical import FetchContractError, StabilityError
from kbforge.chunking import ChunkRecordError
from kbforge.grounding import GroundingConfig, load_grounding
from kbforge.links import LinksConfig, load_links
from kbforge.llm_synthesizer import SynthesisError
from kbforge.lock import MirrorLocked
from kbforge.pipeline import (
    Aborted,
    ConfigError,
    ConnectorProtocol,
    NoOp,
    Planned,
    Published,
    PublisherProtocol,
    Redone,
    RedoRefused,
    Waiting,
)
from kbforge.publishers._http import PublishError
from kbforge.publishers.forge import PathError
from kbforge.synthesize import Synthesizer


def connectors_of(pm: pluggy.PluginManager) -> dict[str, ConnectorProtocol]:
    """name -> connector instance (a connector implements kbforge_fetch)."""
    return {
        p.kbforge_connector_info().name: cast(ConnectorProtocol, p)
        for p in pm.get_plugins()
        if hasattr(p, "kbforge_fetch")
    }


def publishers_of(pm: pluggy.PluginManager) -> dict[str, PublisherProtocol]:
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
"""One table for `--synthesizer` choices, `kbforge list` and the project file:
two hand-kept lists let `describe` reach the first and not the second."""


class SetupError(Exception):
    """A configuration mistake found before any run; `str(exc)` is the exact
    line the CLI prints (exit 2)."""


# Which synthesizers read grounding documents. A table rather than an import of
# `kbforge.llm_synthesizer` classes so asking needs no `kbforge[llm]`; a test
# pins it to the classes' own `grounds` attributes.
_GROUNDS = {"stub": False, "llm": True, "describe": False}


def synthesizer_grounds(name: str) -> bool:
    return _GROUNDS.get(name, False)


def build_synthesizer(name: str, settings: dict, mirror: Path) -> Synthesizer | None:
    """None for `stub`: `run()` defaults to StubSynthesizer."""
    if name not in ("llm", "describe"):
        return None
    from kbforge.llm_synthesizer import (
        DescribeConfig,
        DescribeSynthesizer,
        LLMConfig,
        LLMSynthesizer,
    )

    config_cls = DescribeConfig if name == "describe" else LLMConfig
    try:
        llm_cfg = config_cls(**settings)
    except (ValueError, TypeError) as exc:
        raise SetupError(str(exc)) from None
    problems = llm_cfg.validate_env()
    if problems:
        raise SetupError("; ".join(problems))
    try:
        if isinstance(llm_cfg, DescribeConfig):
            return DescribeSynthesizer(llm_cfg, mirror=mirror)
        return LLMSynthesizer(llm_cfg)
    except ImportError as exc:
        raise SetupError(str(exc)) from None


def check_publisher(publisher: Any, name: str, config: dict) -> list[str]:
    """Fail fast: a bad publisher config should cost a second, not a full
    fetch+synthesize. Third-party publishers predating the hook skip this."""
    validate = getattr(publisher, "kbforge_validate_publish_config", None)
    return validate(config) if validate else []


def load_links_value(v: Path | dict | None) -> LinksConfig | None:
    """A path (resolved by `load_project`) or the mapping inline."""
    if isinstance(v, dict):
        return LinksConfig.model_validate(v)
    return load_links(v)


def load_grounding_value(v: Path | dict | None) -> GroundingConfig:
    if isinstance(v, dict):
        return GroundingConfig.model_validate(v)
    return load_grounding(v)


@dataclass(frozen=True)
class Outcome:
    code: int
    lines: list[str] = field(default_factory=list)
    result: object | None = None
    """The pipeline result on success; Aborted and Waiting included."""


def execute(
    call: Callable[[], NoOp | Aborted | Published | Waiting | Redone | Planned],
    connector_name: str,
) -> Outcome:
    """Run `call` (a `run(...)` or a `redo(...)`, under the mirror lock) and
    map every way it can end to the lines and exit code the CLI reports."""
    try:
        result = call()
    except MirrorLocked as exc:
        return Outcome(
            3,
            [
                f"Locked: another kbforge run holds {exc.path}; "
                "nothing was read or written."
            ],
        )
    except (ConfigError, ChunkRecordError) as exc:
        return Outcome(2, [str(exc)])
    except RedoRefused as exc:
        return Outcome(1, [f"Redo refused: {exc}"])
    except (FetchContractError, StabilityError) as exc:
        # A connector-contract violation, not an operator mistake — but the
        # operator is who sees it, and a traceback tells them nothing about
        # which plugin to report it against. StabilityError is caught here too:
        # it has always escaped as a traceback, and fixing the surfacing only
        # for the newer law would leave the older one worse for no reason.
        return Outcome(2, [f"Connector contract violation ({connector_name}): {exc}"])
    except SynthesisError as exc:
        # Raised before the publish, so the mirror and cursor never moved.
        return Outcome(
            1,
            [f"Synthesis failed: {exc} (nothing was published; the next run retries)"],
        )
    except (PublishError, PathError) as exc:
        # The mirror never advanced, so the next run retries this same change.
        # Catching the PublishError base rather than ForgeError specifically:
        # TreeListingTruncatedError is a publish failure with carefully worded
        # remediation advice, and naming subclasses one by one had already let
        # it escape as a traceback. PathError lands here too — a connector
        # emitting a traversing file key is the case safe_join() exists for,
        # and it deserves a message rather than a traceback.
        return Outcome(1, [f"Publish failed: {exc}"])

    if isinstance(result, Redone):
        return Outcome(
            0,
            [
                f"Redone: {len(result.admitted)} document(s) will be proposed again "
                "on the next run."
            ],
            result,
        )
    if isinstance(result, Published):
        return Outcome(0, [f"Published: {result.url}"], result)
    if isinstance(result, NoOp):
        return Outcome(0, ["NoOp: no change detected; no MR opened."], result)
    if isinstance(result, Aborted):
        lines = [f"Aborted: {len(result.failures)} validation failure(s):"]
        lines += [f"  [{f.law}] {f.concept_path}: {f.message}" for f in result.failures]
        return Outcome(1, lines, result)
    if isinstance(result, Waiting):
        return Outcome(
            0,
            [
                # The request, not `branch_hint`: the hint is the synthesizer's
                # `sync/<system>`, and a configured `branch` override puts the
                # request somewhere else.
                f"Waiting: review request {result.request} is still open; the next "
                "chunk follows once it is merged or closed."
            ],
            result,
        )
    return Outcome(2, [], result)
