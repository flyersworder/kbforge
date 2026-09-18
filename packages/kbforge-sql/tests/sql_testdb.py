"""SQLite fixtures. SQLite through SQLAlchemy is a real SQL engine, so these
tests execute real queries rather than mocking a cursor."""

from __future__ import annotations

import sqlite3
from pathlib import Path

URL_ENV = "KB_SQL_URL"

_SCHEMA = """
CREATE TABLE product (
    product_id TEXT, product_name TEXT, family TEXT, status TEXT,
    description TEXT, last_refreshed TEXT
);
INSERT INTO product VALUES
    ('IMC300', 'IMC300 motor controller', 'MOTIX', 'active',
     'Motor controller for EV pumps.', '2026-09-18T01:00:00'),
    ('TLE9', 'TLE9 gate driver', 'MOTIX', 'active',
     'Gate driver.', '2026-09-18T01:00:00'),
    ('XDP1', 'XDP1 power stage', 'XDP', 'eol', NULL, '2026-09-18T01:00:00');

CREATE TABLE app_product (
    app_id TEXT, app_name TEXT, segment TEXT, app_description TEXT,
    product_id TEXT, product_name TEXT, product_status TEXT
);
INSERT INTO app_product VALUES
    ('EV', 'EV traction', 'Automotive', 'Traction inverters.',
     'TLE9', 'TLE9 gate driver', 'active'),
    ('EV', 'EV traction', 'Automotive', 'Traction inverters.',
     'IMC300', 'IMC300 motor controller', 'active'),
    ('HP', 'Heat pump', 'Industrial', 'Heat pumps.', NULL, NULL, NULL);
"""


def make_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "source.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(_SCHEMA)
    monkeypatch.setenv(URL_ENV, f"sqlite:///{db}")
    return db


def execute(db: Path, sql: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.executescript(sql)


def flat_cfg(**over) -> dict:
    cfg = {
        "system": "products",
        "url_env": URL_ENV,
        "query": (
            "SELECT product_id, product_name, family, status, description, "
            "last_refreshed FROM product"
        ),
        "id": ["product_id"],
        "title": "product_name",
        "text": "description",
        "facets": ["family", "status"],
        "exclude": ["last_refreshed"],
        "type": "product",
        "url_template": "https://portal.example/products/{product_id}",
    }
    cfg.update(over)
    return cfg


def grouped_cfg(**over) -> dict:
    cfg = {
        "system": "applications",
        "url_env": URL_ENV,
        "query": (
            "SELECT app_id, app_name, segment, app_description, product_id, "
            "product_name, product_status FROM app_product"
        ),
        "id": ["app_id"],
        "title": "app_name",
        "text": "app_description",
        "facets": ["segment"],
        "type": "application",
        "group": {
            "children": ["product_id", "product_name", "product_status"],
            "order_by": ["product_id"],
            "heading": "Products",
        },
    }
    cfg.update(over)
    return cfg
