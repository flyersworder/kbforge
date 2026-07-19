"""Pluggy hookspecs. The connector and publisher interfaces ARE the product (§5).
Kept minimal for the walking skeleton: one connector family, one publisher family."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import pluggy

from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    RawRecord,
)

PROJECT = "kbforge"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)


class ConnectorSpec(ABC):
    """One plugin object per system of record. Connectors never see the bundle,
    never call the LLM, never touch git (§4.1)."""

    @hookspec
    @abstractmethod
    def kbforge_connector_info(self) -> ConnectorInfo:
        """Static self-description."""

    @hookspec
    @abstractmethod
    def kbforge_validate_config(self, config: dict) -> list[str]:
        """Return human-readable problems ([] = ok). No network I/O."""

    @hookspec
    @abstractmethod
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        """Pull raw records (cursor=None = full backfill / bootstrap)."""

    @hookspec
    @abstractmethod
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        """Deterministic, volatile-free, clock-free (§4.3)."""


class PublisherSpec(ABC):
    """Where proposals go. MUST NOT merge (§5.2)."""

    @hookspec
    @abstractmethod
    def kbforge_publisher_info(self) -> ConnectorInfo:
        """Static self-description."""

    @hookspec
    @abstractmethod
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        """Open a review request; return its URL/path. Never merges."""
