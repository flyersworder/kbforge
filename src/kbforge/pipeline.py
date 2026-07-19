"""The fixed-order pipeline (architecture §7). The order is NOT pluggable; the
no-op and never-auto-merge rules are trust guarantees enforced here."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kbforge.canonical import assert_stability
from kbforge.mirror import commit, diff
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    RawRecord,
)
from kbforge.synthesize import concept_path, synthesize
from kbforge.validate import Failure, run_validators


class ConnectorProtocol(Protocol):
    """Duck-typed connector interface (hookspec-based)."""

    def kbforge_connector_info(self) -> ConnectorInfo: ...

    def kbforge_validate_config(self, config: dict) -> list[str]: ...

    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult: ...

    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]: ...


class PublisherProtocol(Protocol):
    """Duck-typed publisher interface (hookspec-based)."""

    def kbforge_publish(self, change: ProposedChange, config: dict) -> str: ...


@dataclass(frozen=True)
class NoOp:
    """No change detected — no MR opened. Ever."""


@dataclass(frozen=True)
class Aborted:
    """Validation failed — the artifact is non-conformant, so no MR opened."""

    failures: list[Failure]


@dataclass(frozen=True)
class Published:
    url: str


class ConfigError(RuntimeError):
    """A connector rejected its config before any I/O."""


def _cursor_slot(state_dir: Path, connector: str) -> Path:
    return state_dir / f"cursor-{connector}.json"


def _load_cursor(state_dir: Path, connector: str) -> Cursor | None:
    slot = _cursor_slot(state_dir, connector)
    if not slot.exists():
        return None
    return Cursor.model_validate_json(slot.read_text("utf-8"))


def _save_cursor(state_dir: Path, cursor: Cursor) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    slot = _cursor_slot(state_dir, cursor.connector)
    slot.write_text(cursor.model_dump_json(), "utf-8")


def run(
    connector: ConnectorProtocol,
    publisher: PublisherProtocol,
    *,
    config: dict,
    mirror: str,
    state_dir: str,
    publish_config: dict,
) -> NoOp | Aborted | Published:
    info = connector.kbforge_connector_info()
    problems = connector.kbforge_validate_config(config)
    if problems:
        raise ConfigError(f"{info.name}: {'; '.join(problems)}")

    mirror_path = Path(mirror)
    state_path = Path(state_dir)

    result = connector.kbforge_fetch(config, _load_cursor(state_path, info.name))
    docs = connector.kbforge_normalize(result.records)
    assert_stability(connector.kbforge_normalize, result.records)  # §4.3 law 1

    changeset = diff(mirror_path, docs)
    if changeset.is_noop:
        return NoOp()

    changed = set(changeset.added) | set(changeset.modified)
    changed_docs = [d for d in docs if d.doc_id in changed]  # "scope"
    # Existing bundle paths = every fetched doc's concept path, so a link from a
    # changed concept to an unchanged-but-present sibling still resolves (§4.4 law 2)
    # instead of being dropped. (Feed-less full-fetch connector: `docs` is complete.)
    existing = frozenset(concept_path(d.doc_id) for d in docs)
    proposal = synthesize(changed_docs, changeset, existing)

    failures = run_validators(proposal, existing)
    if failures:
        return Aborted(failures=failures)

    url = publisher.kbforge_publish(proposal, publish_config)
    commit(mirror_path, docs)  # advance mirror ONLY after success
    _save_cursor(state_path, result.cursor)
    return Published(url=url)
