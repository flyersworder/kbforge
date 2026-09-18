"""The kbforge connector: four hookimpls over one rolled-back query.

`kbforge_fetch` may use a clock and the network; `kbforge_normalize` may not
(architecture §4.3). `retrieved_at` is stamped in fetch, into `anchor_hint`,
and normalize only reads it back. Everything normalize needs travels in the
record -- it never sees config."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import URL, create_engine, make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, InterfaceError, OperationalError
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
                # no_parameters: a driver's paramstyle (psycopg/psycopg2/pymysql,
                # and Denodo's dialect, are all pyformat-family) otherwise treats
                # `%` in the statement as a parameter marker, and a bare `LIKE
                # 'EV-%'` breaks with an empty parameter collection to fill it.
                result = conn.exec_driver_sql(
                    query, execution_options={"no_parameters": True}
                )
                if not result.returns_rows:
                    raise SqlSourceError(
                        "the query returned no result set; a source query must "
                        "be a SELECT"
                    )
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchall()]
            finally:
                # No commit exists anywhere in this package. Whatever the
                # statement did, this discards it (spec §7). SQLAlchemy also
                # rolls back on close, so this line is belt-and-braces; no
                # test can observe it, and the property tests pin is that
                # nothing is ever committed.
                conn.rollback()
    finally:
        engine.dispose()
    return columns, rows


# Retried: the connection failed, not the statement. Re-running a SELECT is
# safe. Everything else -- bad SQL, a missing view, no permission -- fails at
# once, because retrying a typo only delays the message. The classification is
# the driver's: sqlite3 and pymysql raise OperationalError for some statement
# errors too, so on those a bad query is retried before it fails. That costs
# time, never correctness; `retries: 0` opts out.
_TRANSIENT = (OperationalError, InterfaceError)


def _url(cfg: SqlSourceConfig) -> URL:
    try:
        url = make_url(os.environ[cfg.url_env])
    except ArgumentError:
        # Never echo the value: it may carry a password.
        raise SqlSourceError(
            f"the value of {cfg.url_env} is not a SQLAlchemy URL"
        ) from None
    if cfg.password_env:
        # URL.set takes the raw password: no percent-escaping for `@:/%`.
        url = url.set(password=os.environ[cfg.password_env])
    return url


def _redact(message: str, url: URL) -> str:
    secret = url.password
    return message.replace(str(secret), "***") if secret else message


def _query(cfg: SqlSourceConfig) -> tuple[list[str], list[tuple]]:
    url = _url(cfg)
    attempts = cfg.retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return _query_once(url, cfg.query)
        except _TRANSIENT as exc:
            if attempt == attempts:
                raise SqlSourceError(
                    _redact(
                        f"{type(exc).__name__} after {attempts} attempt(s): {exc.orig}",
                        url,
                    )
                ) from None
            _sleep(min(2 ** (attempt - 1), 30))
        except DBAPIError as exc:
            raise SqlSourceError(
                _redact(f"{type(exc).__name__}: {exc.orig}", url)
            ) from None
        except (ArgumentError, ImportError) as exc:
            # NoSuchModuleError is an ArgumentError; a dialect whose DBAPI
            # module is absent raises ImportError from create_engine.
            raise SqlSourceError(
                _redact(
                    "no SQLAlchemy dialect or driver for this URL is installed "
                    f"({exc}); install the driver for your database",
                    url,
                )
            ) from None
    raise AssertionError("unreachable: the loop returns or raises")


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


_ALLOW_REMOVALS_ENV = "KBFORGE_SQL_ALLOW_REMOVALS"


def _removals_allowed(system: str) -> bool:
    """`KBFORGE_SQL_ALLOW_REMOVALS` is a comma-separated list of source
    `system` names, out of band from the config on purpose:
    `pipeline._instance_key` hashes the *whole* connector config into the
    cursor slot name, so raising `max_removed_fraction` -- or editing any
    other config key -- makes the next run find no prior manifest at all
    (no tombstones, a silent NoOp) rather than performing the cleanup. This
    is read here, at fetch time; `normalize` never reads the environment."""
    raw = os.environ.get(_ALLOW_REMOVALS_ENV, "")
    return system in {name.strip() for name in raw.split(",") if name.strip()}


def _removed(cfg: SqlSourceConfig, prior: list[str], current: list[str]) -> list[str]:
    """Ids seen at the last published run and missing now (spec §6).

    The empty-result guard always applies. The deletion ceiling is skipped
    when the source's `system` is listed in `KBFORGE_SQL_ALLOW_REMOVALS`,
    the deliberate-cleanup override (spec §6.3). An empty result is refused
    even at max_removed_fraction=1.0 or with the override set: a view
    mid-refresh returns zero rows without an error, and 'delete the
    knowledge base' must never be the default reading of that."""
    if not prior:
        return []
    if not current:
        raise SqlSourceError(
            "the query returned no rows, but the last published run saw "
            f"{len(prior)}; refusing to delete every concept. If the source "
            "is meant to be empty, remove its config instead"
        )
    gone = sorted(set(prior) - set(current))
    if _removals_allowed(cfg.system):
        return gone
    fraction = len(gone) / len(prior)
    if fraction > cfg.max_removed_fraction:
        raise SqlSourceError(
            f"{len(gone)} of {len(prior)} previously seen ids ({fraction:.1%}) "
            f"are missing, above max_removed_fraction={cfg.max_removed_fraction}; "
            f"for a deliberate cleanup, rerun with {_ALLOW_REMOVALS_ENV}={cfg.system} "
            "(editing the config resets deletion memory instead)"
        )
    return gone


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
