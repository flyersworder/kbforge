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
    -- TIMESTAMPTZ, never TIMESTAMP. Not because a naive column would corrupt
    -- the instant on this package's load path -- `load` binds an aware Python
    -- datetime through executemany, so the instant survives a naive column
    -- fine. What a naive column loses is the type: values come back with no
    -- tzinfo, so every comparison against now() (itself TIMESTAMPTZ) needs a
    -- cast, and the column asserts UTC by convention with nothing recording
    -- that it does. §4.4 law 4 exists to force an *aware* stamp; a column that
    -- cannot hold one throws away exactly the property the law buys.
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
