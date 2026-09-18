from datetime import datetime

import pytest
from sql_testdb import execute, flat_cfg, grouped_cfg, make_db
from sqlalchemy import event
from sqlalchemy.engine import Engine

from kbforge.canonical import assert_fetch_contract, assert_stability
from kbforge.registry import build_registry
from kbforge_sql.connector import CONNECTOR, TOMBSTONE
from kbforge_sql.errors import SqlSourceError


def _docs(cfg):
    result = CONNECTOR.kbforge_fetch(cfg, None)
    return result, {d.doc_id: d for d in CONNECTOR.kbforge_normalize(result.records)}


def test_the_connector_is_discovered_as_sql():
    # pluggy registers an entry-point plugin under the entry point's name.
    assert build_registry().get_plugin("sql") is CONNECTOR


def test_a_flat_source_yields_one_document_per_row(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result, docs = _docs(flat_cfg())
    assert result.complete is True
    assert sorted(docs) == ["products:IMC300", "products:TLE9", "products:XDP1"]
    doc = docs["products:IMC300"]
    assert doc.title == "IMC300 motor controller"
    assert doc.structured == {"family": "MOTIX", "status": "active", "type": "product"}
    assert doc.anchor.url == "https://portal.example/products/IMC300"
    assert doc.text == (
        "Motor controller for EV pumps.\n"
        "\n"
        "## Attributes\n"
        "- **product_id:** IMC300\n"
        "- **family:** MOTIX\n"
        "- **status:** active"
    )
    assert "last_refreshed" not in doc.text
    assert_fetch_contract(list(docs.values()), complete=True)


def test_a_null_text_column_leaves_no_lead(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(flat_cfg())
    assert docs["products:XDP1"].text.startswith("## Attributes\n")


def test_a_null_title_falls_back_to_the_native_id(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "UPDATE product SET product_name = NULL WHERE product_id = 'XDP1';")
    _, docs = _docs(flat_cfg())
    assert docs["products:XDP1"].title == "XDP1"


def test_url_template_percent_quotes_special_characters_in_the_id(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path, monkeypatch)
    execute(
        db,
        "INSERT INTO product VALUES ('A/B%C', 'weird id product', 'MOTIX', "
        "'active', NULL, 'x');",
    )
    _, docs = _docs(flat_cfg())
    doc = docs["products:A%2FB%25C"]
    assert doc.anchor.url == "https://portal.example/products/A%2FB%25C"


def test_a_grouped_source_folds_rows_into_one_document(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(grouped_cfg())
    assert sorted(docs) == ["applications:EV", "applications:HP"]
    assert docs["applications:EV"].text == (
        "Traction inverters.\n"
        "\n"
        "## Attributes\n"
        "- **app_id:** EV\n"
        "- **segment:** Automotive\n"
        "\n"
        "## Products\n"
        "| product_id | product_name | product_status |\n"
        "|---|---|---|\n"
        "| IMC300 | IMC300 motor controller | active |\n"
        "| TLE9 | TLE9 gate driver | active |"
    )


def test_a_left_join_with_no_children_renders_an_empty_table(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, docs = _docs(grouped_cfg())
    assert docs["applications:HP"].text.endswith(
        "| product_id | product_name | product_status |\n|---|---|---|"
    )


def test_child_order_does_not_depend_on_row_order(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    _, forward = _docs(grouped_cfg())
    _, backward = _docs(
        grouped_cfg(query=grouped_cfg()["query"] + " ORDER BY product_id DESC")
    )
    assert forward["applications:EV"].text == backward["applications:EV"].text


def test_a_duplicate_id_without_group_is_an_error(tmp_path, monkeypatch):
    db = make_db(tmp_path, monkeypatch)
    execute(
        db, "INSERT INTO product VALUES ('TLE9', 'dup', 'MOTIX', 'active', NULL, 'x');"
    )
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        "sql source 'products': 2 rows share the id 'TLE9'; configure 'group' "
        "to fold them, or make the id unique"
    )


def test_byte_identical_duplicate_rows_still_trip_the_duplicate_id_check(
    tmp_path, monkeypatch
):
    # Two rows that agree on every column could slip past an entity-column
    # disagreement check (nothing to disagree about) if that check ran
    # instead of the duplicate-id count. Only the count check catches this.
    db = make_db(tmp_path, monkeypatch)
    execute(
        db,
        "INSERT INTO product VALUES ('IMC300', 'IMC300 motor controller', "
        "'MOTIX', 'active', 'Motor controller for EV pumps.', "
        "'2026-09-18T01:00:00');",
    )
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert str(exc.value) == (
        "sql source 'products': 2 rows share the id 'IMC300'; configure 'group' "
        "to fold them, or make the id unique"
    )


def test_a_query_with_duplicate_column_names_is_rejected(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query="SELECT product_id, product_name, product_name FROM product",
        text=None,
        facets=[],
        exclude=[],
        url_template=None,
    )
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(cfg, None)
    assert str(exc.value) == (
        "sql source 'products': the query returned duplicate column name(s) "
        "['product_name']; alias them apart"
    )


def test_grouped_rows_that_disagree_on_an_entity_column_are_an_error(
    tmp_path, monkeypatch
):
    db = make_db(tmp_path, monkeypatch)
    execute(db, "UPDATE app_product SET segment = 'Other' WHERE product_id = 'TLE9';")
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(grouped_cfg(), None)
    assert str(exc.value) == (
        "sql source 'applications': rows for id 'EV' disagree on column "
        "'segment'; only 'group.children' columns may vary within an entity"
    )


def test_a_misnamed_column_fails_listing_the_real_ones(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with pytest.raises(SqlSourceError) as exc:
        CONNECTOR.kbforge_fetch(flat_cfg(facets=["famliy"]), None)
    assert str(exc.value).startswith(
        "sql source 'products': configured column(s) ['famliy'] are not in the "
        "query result; the query returned: ['product_id', 'product_name', "
    )


def test_a_value_with_no_canonical_form_names_its_column(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query=flat_cfg()["query"].replace("last_refreshed", "X'00' AS blob"),
        exclude=[],
    )
    with pytest.raises(SqlSourceError, match="column 'blob' holds a bytes value"):
        CONNECTOR.kbforge_fetch(cfg, None)


def test_an_excluded_column_is_never_canonicalized(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query=flat_cfg()["query"].replace("last_refreshed", "X'00' AS blob"),
        exclude=["blob"],
    )
    CONNECTOR.kbforge_fetch(cfg, None)  # does not raise


def test_normalize_is_stable_and_clock_free(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(grouped_cfg(), None)
    assert_stability(CONNECTOR.kbforge_normalize, result.records)
    first = CONNECTOR.kbforge_normalize(result.records)

    import kbforge_sql.connector as mod

    class NoClock:
        # normalize must not call now(); it must still parse retrieved_at, so
        # fromisoformat delegates. `tz` matches fetch's call shape, so a copied
        # clock call fails on the assertion, not on a TypeError.
        @staticmethod
        def now(tz=None):
            raise AssertionError("normalize called the clock (architecture 4.3)")

        @staticmethod
        def fromisoformat(value):
            return datetime.fromisoformat(value)

    monkeypatch.setattr(mod, "datetime", NoClock)
    second = CONNECTOR.kbforge_normalize(result.records)
    assert [d.anchor for d in first] == [d.anchor for d in second]


def test_one_retrieved_at_for_the_whole_run(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert len({r.anchor_hint["retrieved_at"] for r in result.records}) == 1


def test_the_query_cannot_write(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query=(
            "INSERT INTO product VALUES ('NEW', 'n', 'f', 's', 'd', 'x') "
            "RETURNING product_id, product_name, family, status, description, "
            "last_refreshed"
        )
    )
    CONNECTOR.kbforge_fetch(cfg, None)
    _, docs = _docs(flat_cfg())
    assert "products:NEW" not in docs  # rolled back, never committed


def test_a_statement_without_a_result_set_is_rejected(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    with pytest.raises(SqlSourceError, match="returned no result set"):
        CONNECTOR.kbforge_fetch(flat_cfg(query="DELETE FROM product"), None)
    _, docs = _docs(flat_cfg())
    assert len(docs) == 3  # the DELETE was rolled back


def test_a_percent_sign_in_the_query_is_not_treated_as_a_bind_parameter(
    tmp_path, monkeypatch
):
    # exec_driver_sql hands the driver an empty parameter collection; a
    # pyformat-family driver (psycopg/psycopg2/pymysql, and Denodo's dialect)
    # then interpolates `%` against it, so a bare `LIKE 'IMC%'` breaks unless
    # the execution is marked no_parameters.
    make_db(tmp_path, monkeypatch)
    seen: list[bool] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(context.no_parameters)

    event.listen(Engine, "before_cursor_execute", listener)
    try:
        cfg = flat_cfg(query=flat_cfg()["query"] + " WHERE product_id LIKE 'IMC%'")
        _, docs = _docs(cfg)
    finally:
        event.remove(Engine, "before_cursor_execute", listener)

    assert seen and all(seen)
    assert sorted(docs) == ["products:IMC300"]


def test_the_cursor_carries_the_manifest_under_sql(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    result = CONNECTOR.kbforge_fetch(flat_cfg(), None)
    assert result.cursor.connector == "sql"
    assert result.cursor.payload == {"ids": ["IMC300", "TLE9", "XDP1"]}
    assert all(r.media_type != TOMBSTONE for r in result.records)


def test_numeric_order_by_sorts_by_value_not_text():
    # Canonical form turns a Decimal into text, and "10" < "100" < "9.5" as
    # text. Children must sort on the database value.
    from decimal import Decimal

    from kbforge_sql.config import SqlSourceConfig
    from kbforge_sql.connector import _entities

    cfg = SqlSourceConfig.model_validate(
        grouped_cfg(
            group={"children": ["sku", "price"], "order_by": ["price"]},
            facets=[],
            text=None,
        )
    )
    columns = ["app_id", "app_name", "segment", "sku", "price"]
    rows = [
        ("EV", "EV traction", "Automotive", "C", Decimal("100")),
        ("EV", "EV traction", "Automotive", "A", Decimal("9.5")),
        ("EV", "EV traction", "Automotive", "B", Decimal("10")),
        ("EV", "EV traction", "Automotive", "D", None),
    ]
    (entity,) = _entities(cfg, columns, rows)
    assert entity.payload["group"]["rows"] == [
        ["A", "9.5"],
        ["B", "10"],
        ["C", "100"],
        ["D", None],
    ]


def test_a_url_template_column_with_a_dot_renders(tmp_path, monkeypatch):
    make_db(tmp_path, monkeypatch)
    cfg = flat_cfg(
        query='SELECT product_id AS "p.id", product_name FROM product',
        id=["p.id"],
        text=None,
        facets=[],
        exclude=[],
        url_template="https://portal.example/products/{p.id}",
    )
    _, docs = _docs(cfg)
    assert docs["products:IMC300"].anchor.url == (
        "https://portal.example/products/IMC300"
    )
