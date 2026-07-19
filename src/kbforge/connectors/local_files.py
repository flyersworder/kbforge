"""A fixture connector: a folder of markdown-with-frontmatter → canonical docs.
No credentials, no network — the deterministic source for the walking skeleton."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

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

_SYSTEM = "local_files"
# Keys handled structurally, so they never leak into `structured` (hence facets):
# title → the concept title; relations → cross-links; type is dropped here because
# the OKF type comes from synthesis taxonomy (the stub emits "concept"); description,
# timestamp, resource, and links are emit-side OKF fields the synthesizer owns — a
# source key of the same name must not collide with them in the rendered frontmatter.
_RESERVED_KEYS = frozenset(
    {"type", "title", "relations", "description", "timestamp", "resource", "links"}
)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    _, _, rest = text.partition("---")
    front_raw, sep, body = rest.partition("\n---")
    if not sep:
        return {}, text
    try:
        data = yaml.safe_load(front_raw) or {}
    except yaml.YAMLError:
        # Malformed frontmatter (e.g. an unquoted colon) must not crash the whole
        # sync: drop the unparseable frontmatter, keep the clean body.
        data = {}
    front = data if isinstance(data, dict) else {}
    return front, body.lstrip("\n")


class LocalFilesConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_SYSTEM,
            version="0.1.0",
            source_system="local filesystem (fixture)",
            info_types=["fixture"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        path = config.get("path")
        if not path or not Path(path).is_dir():
            return [f"config 'path' is not a readable directory: {path!r}"]
        return []

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        # cursor is unused: this feed-less source always re-scans. The mirror diff
        # detects adds and modifies; it does NOT derive deletions — a full-scan
        # source can't tell a deleted file from an absent one, so a removed file
        # leaves a stale concept until a tombstone / `complete`-aware diff lands (a
        # later increment; `FetchResult.complete` is defined but not yet consumed).
        # retrieved_at is stamped here (fetch may use a clock; normalize may not)
        # from file mtime, keeping runs reproducible.
        root = Path(config["path"])
        records: list[RawRecord] = []
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            records.append(
                RawRecord(
                    anchor_hint={
                        "native_id": rel,
                        "url": None,
                        "retrieved_at": mtime.isoformat(),
                    },
                    media_type="text/markdown",
                    payload=path.read_bytes(),
                )
            )
        return FetchResult(records=records, cursor=Cursor(connector=_SYSTEM))

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            # utf-8-sig strips a BOM (Windows editors) that would otherwise defeat
            # the `startswith("---")` check; line-ending normalization keeps CRLF
            # and LF copies of the same content hashing identically (§4.3 law 1).
            text = (
                rec.payload.decode("utf-8-sig")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            front, body = _split_frontmatter(text)
            native_id = rec.anchor_hint["native_id"]
            doc_id = f"{_SYSTEM}:{native_id}"
            relations = sorted(
                f"{_SYSTEM}:{r}"
                for r in front.get("relations", [])
                if isinstance(r, str)
            )
            structured = {k: v for k, v in front.items() if k not in _RESERVED_KEYS}
            anchor = ResourceAnchor(
                system=_SYSTEM,
                native_id=native_id,
                url=rec.anchor_hint.get("url"),
                retrieved_at=datetime.fromisoformat(rec.anchor_hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=doc_id,
                title=str(front.get("title") or native_id),
                text=body.strip(),
                structured=structured,
                relations=relations,
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs
