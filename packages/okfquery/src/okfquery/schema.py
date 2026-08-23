"""The DDL, defined once.

Explicit CREATE TABLE rather than a dataframe round-trip, for two reasons: an
empty bundle still answers queries instead of erroring on a missing table, and
the column types are pinned rather than inferred."""

from __future__ import annotations

SCHEMA_SQL = """
CREATE TABLE concepts (
    path          VARCHAR NOT NULL,
    type          VARCHAR,
    title         VARCHAR,
    description   VARCHAR,
    generated_by  VARCHAR,
    -- TIMESTAMPTZ, never TIMESTAMP. §4.4 law 4 requires an *aware* stamp, not a
    -- UTC one, so a connector may legally emit +09:00 -- and a naive TIMESTAMP
    -- would drop that offset, making every staleness query wrong by up to a day.
    generated_at  TIMESTAMPTZ,
    facets        JSON,
    body          VARCHAR
);

CREATE TABLE sources (
    path          VARCHAR NOT NULL,
    -- A real column. kbforge's synthesize.assemble puts the OWNING anchor first
    -- and grounding anchors after it, and that ordering is the only thing
    -- telling owner from ground -- OKF has no field for it. Discarding ordinal
    -- during unnest would destroy the signal.
    ordinal       INTEGER NOT NULL,
    id            VARCHAR,
    resource      VARCHAR,
    content_hash  VARCHAR
);

CREATE TABLE links (
    path          VARCHAR NOT NULL,
    target        VARCHAR NOT NULL
);

CREATE TABLE problems (
    path          VARCHAR NOT NULL,
    kind          VARCHAR NOT NULL,
    detail        VARCHAR NOT NULL
);
"""
