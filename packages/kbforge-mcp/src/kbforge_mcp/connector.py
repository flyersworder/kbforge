"""The kbforge connector: four hookimpls, one asyncio.run, one session.

`kbforge_fetch` may use a clock; `kbforge_normalize` may not (architecture §4.3).
`retrieved_at` is therefore stamped here, into `anchor_hint`, and normalize only
reads it back. `assert_stability` cannot catch a violation of that -- content_hash
excludes the anchor by design -- so the guard is a test, not a convention.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from kbforge.canonical import content_hash
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)
from kbforge_mcp.client import ToolCallFailed, open_session
from kbforge_mcp.config import McpSourceConfig, problems_for
from kbforge_mcp.mapping import MappingError, records_from_read
from kbforge_mcp.selectors import select_refs

_NAME = "mcp"


def _unwrap(exc: BaseException) -> BaseException:
    """Anyio's task groups wrap any exception that escapes the session's
    `async with` in a `BaseExceptionGroup` -- even a single one, and often
    nested once per context-manager layer the in-process transport crosses.
    A caller of `kbforge_fetch` wants the domain error (a `MappingError`'s
    "configure static_ids instead", a `ToolCallFailed`), not anyio plumbing,
    so unwrap down to the sole leaf when the group carries exactly one."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


async def _fetch(cfg: McpSourceConfig) -> tuple[list[RawRecord], bool]:
    async with open_session(cfg) as client:
        refs, complete = await select_refs(client, cfg)
        records: list[RawRecord] = []
        stamped = datetime.now(tz=UTC).isoformat()
        # `system` reaches normalize ONLY through anchor_hint: normalize receives
        # records, never config.
        for ref in refs:
            args = {cfg.read.id_arg: ref.raw_id, **cfg.read.static_args}
            try:
                result = await client.call(cfg.read.tool, args)
                got = records_from_read(result, ref, cfg.read, cfg.media_type)
            except (ToolCallFailed, MappingError):
                # A per-document failure degrades the run; it never silently
                # drops a document while still claiming complete coverage.
                complete = False
                continue
            for rec in got:
                rec.anchor_hint["retrieved_at"] = stamped
                rec.anchor_hint["system"] = cfg.system
            records.extend(got)
        return records, complete


class McpConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_NAME,
            version="0.1.0",
            source_system="any MCP server with a select tool and a read-by-id tool",
            info_types=["document"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        return problems_for(config)

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        # cursor is unused in 0.7.0: like local_files, this re-selects every run
        # and lets the mirror diff do the work. The manifest lands in 0.8.0.
        cfg = McpSourceConfig.model_validate(config)
        try:
            records, complete = asyncio.run(_fetch(cfg))
        except BaseExceptionGroup as eg:
            raise _unwrap(eg) from eg
        return FetchResult(
            records=records,
            cursor=Cursor(connector=_NAME),
            complete=complete,
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            hint = rec.anchor_hint
            native_id = hint["native_id"]
            system = hint["system"]
            text = (
                rec.payload.decode("utf-8-sig")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            anchor = ResourceAnchor(
                system=system,
                native_id=native_id,
                url=hint.get("url"),
                retrieved_at=datetime.fromisoformat(hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=f"{system}:{native_id}",
                title=str(hint.get("title") or native_id.rsplit("/", 1)[-1]),
                text=text.strip(),
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs


CONNECTOR = McpConnector()
