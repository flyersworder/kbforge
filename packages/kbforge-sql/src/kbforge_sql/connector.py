"""The kbforge connector: four hookimpls over one rolled-back query.

`kbforge_fetch` may use a clock and the network; `kbforge_normalize` may not
(architecture §4.3). `retrieved_at` is stamped in fetch, into `anchor_hint`,
and normalize only reads it back. Everything normalize needs travels in the
record -- it never sees config."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool

from kbforge.canonical import content_hash, is_blank
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)
from kbforge_sql.config import SqlSourceConfig, check_columns, problems_for
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.identity import native_id_for
from kbforge_sql.render import render_text
from kbforge_sql.values import Scalar, canonical

NAME = "sql"
TOMBSTONE = "application/vnd.kbforge.tombstone"

# Indirection so tests can observe backoff without waiting (Task 6).
_sleep = time.sleep


@dataclass(frozen=True)
class _Entity:
    native_id: str
    payload: dict
    url: str | None


def _query_once(url: URL, query: str) -> tuple[list[str], list[tuple]]:
    """Run the one configured statement and roll back. `exec_driver_sql`, not
    `text()`: text() parses `:name` as a bind parameter, which breaks `'10:30'`
    literals and Postgres `::` casts in an operator's query."""
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            try:
                result = conn.exec_driver_sql(query)
                if not result.returns_rows:
                    raise SqlSourceError(
                        "the query returned no result set; a source query must "
                        "be a SELECT"
                    )
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchall()]
            finally:
                # No commit exists anywhere in this package. Whatever the
                # statement did, this discards it (spec §7).
                conn.rollback()
    finally:
        engine.dispose()
    return columns, rows


def _query(cfg: SqlSourceConfig) -> tuple[list[str], list[tuple]]:
    """Task 6 replaces this with URL building, retry and error hygiene."""
    import os

    from sqlalchemy import make_url

    url = make_url(os.environ[cfg.url_env])
    if cfg.password_env:
        url = url.set(password=os.environ[cfg.password_env])
    return _query_once(url, cfg.query)


def _sort_key(value: Scalar) -> tuple:
    # Type name before value, so a column mixing ints and strings still sorts
    # instead of raising TypeError; NULLs last.
    return (value is None, type(value).__name__, 0 if value is None else value)


def _entities(
    cfg: SqlSourceConfig, columns: list[str], rows: list[tuple]
) -> list[_Entity]:
    index = {c: i for i, c in enumerate(columns)}
    keep = [c for c in columns if c not in cfg.exclude]
    children = list(cfg.group.children) if cfg.group else []

    by_id: dict[str, list[dict[str, Scalar]]] = {}
    for raw in rows:
        row = {c: canonical(raw[index[c]], c) for c in keep}
        nid = native_id_for([row[c] for c in cfg.id], cfg.id)
        by_id.setdefault(nid, []).append(row)

    entity_cols = [c for c in keep if c not in children]
    entities: list[_Entity] = []
    for nid in sorted(by_id):
        group_rows = by_id[nid]
        if cfg.group is None and len(group_rows) > 1:
            raise SqlSourceError(
                f"{len(group_rows)} rows share the id {nid!r}; configure 'group' "
                "to fold them, or make the id unique"
            )
        for c in entity_cols:
            if len({json.dumps(r[c]) for r in group_rows}) > 1:
                raise SqlSourceError(
                    f"rows for id {nid!r} disagree on column {c!r}; only "
                    "'group.children' columns may vary within an entity"
                )
        first = group_rows[0]

        title = first[cfg.title]
        lead = first[cfg.text] if cfg.text else None
        attributes = [[c, first[c]] for c in cfg.id] + [
            [c, first[c]]
            for c in entity_cols
            if c not in cfg.id and c != cfg.title and c != cfg.text
        ]
        group = None
        if cfg.group is not None:
            child_rows = [[r[c] for c in children] for r in group_rows]
            # A LEFT JOIN's all-NULL row means "no children", not a child.
            child_rows = [r for r in child_rows if any(v is not None for v in r)]
            order = [children.index(c) for c in cfg.group.order_by]
            child_rows.sort(
                key=lambda r: ([_sort_key(r[i]) for i in order], json.dumps(r))
            )
            group = {
                "heading": cfg.group.heading or cfg.system,
                "columns": children,
                "rows": child_rows,
            }
        url = None
        if cfg.url_template is not None:
            url = cfg.url_template.format(
                **{c: quote(str(first[c]), safe="") for c in cfg.id}
            )
        entities.append(
            _Entity(
                native_id=nid,
                url=url,
                payload={
                    "title": (
                        nid if title is None or is_blank(str(title)) else str(title)
                    ),
                    "lead": None if lead is None else str(lead),
                    "attributes": attributes,
                    "facets": {f: first[f] for f in cfg.facets if first[f] is not None},
                    "type": cfg.type,
                    "group": group,
                },
            )
        )
    return entities


def _removed(cfg: SqlSourceConfig, prior: list[str], current: list[str]) -> list[str]:
    """Task 5 implements deletions and their guards."""
    return []


class SqlConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=NAME,
            version="0.1.0",
            source_system="any database SQLAlchemy can reach",
            info_types=["entity"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        return problems_for(config)

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        cfg = SqlSourceConfig.model_validate(config)
        try:
            columns, rows = _query(cfg)
            check_columns(cfg, columns)
            entities = _entities(cfg, columns, rows)
            current = [e.native_id for e in entities]
            prior = list((cursor.payload.get("ids") if cursor else None) or [])
            removed = _removed(cfg, prior, current)
        except SqlSourceError as exc:
            raise SqlSourceError(f"sql source {cfg.system!r}: {exc}") from None

        stamped = datetime.now(tz=UTC).isoformat()

        def hint(native_id: str, url: str | None) -> dict:
            return {
                "system": cfg.system,
                "native_id": native_id,
                "url": url,
                "retrieved_at": stamped,
            }

        records = [
            RawRecord(
                anchor_hint=hint(e.native_id, e.url),
                media_type="application/json",
                payload=json.dumps(
                    e.payload, sort_keys=True, ensure_ascii=False
                ).encode(),
            )
            for e in entities
        ]
        records += [
            RawRecord(anchor_hint=hint(nid, None), media_type=TOMBSTONE, payload=b"")
            for nid in removed
        ]
        return FetchResult(
            records=records,
            cursor=Cursor(connector=NAME, payload={"ids": sorted(current)}),
            complete=True,
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            h = rec.anchor_hint
            system, native_id = h["system"], h["native_id"]
            anchor = ResourceAnchor(
                system=system,
                native_id=native_id,
                url=h.get("url"),
                retrieved_at=datetime.fromisoformat(h["retrieved_at"]),
                content_hash="",
            )
            doc_id = f"{system}:{native_id}"
            if rec.media_type == TOMBSTONE:
                doc = CanonicalDocument(
                    anchor=anchor,
                    doc_id=doc_id,
                    title=native_id,
                    text="",
                    deleted=True,
                )
            else:
                p = json.loads(rec.payload)
                doc = CanonicalDocument(
                    anchor=anchor,
                    doc_id=doc_id,
                    title=p["title"],
                    text=render_text(p["lead"], p["attributes"], p["group"]),
                    structured={**p["facets"], "type": p["type"]},
                )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs


CONNECTOR = SqlConnector()
