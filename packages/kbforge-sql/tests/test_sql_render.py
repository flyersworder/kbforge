from kbforge_sql.render import render_text


def test_flat_entity_with_lead():
    text = render_text(
        "Gate driver for EV inverters.",
        [["product_id", "IMC300"], ["family", "MOTIX"], ["status", None]],
        None,
    )
    assert text == (
        "Gate driver for EV inverters.\n"
        "\n"
        "## Attributes\n"
        "- **product_id:** IMC300\n"
        "- **family:** MOTIX\n"
        "- **status:** —"
    )


def test_no_lead_starts_with_attributes():
    assert render_text(None, [["id", 1]], None) == "## Attributes\n- **id:** 1"


def test_bools_render_lowercase():
    assert render_text(None, [["active", True]], None).endswith("- **active:** true")


def test_newlines_in_an_attribute_become_br():
    assert render_text(None, [["note", "a\nb"]], None).endswith("- **note:** a<br>b")


def test_grouped_entity_renders_a_child_table():
    text = render_text(
        None,
        [["app_id", "EV"]],
        {
            "heading": "Products",
            "columns": ["product_id", "status"],
            "rows": [["IMC300", "active"], ["IMC301", None]],
        },
    )
    assert text == (
        "## Attributes\n"
        "- **app_id:** EV\n"
        "\n"
        "## Products\n"
        "| product_id | status |\n"
        "|---|---|\n"
        "| IMC300 | active |\n"
        "| IMC301 | — |"
    )


def test_pipes_and_newlines_in_cells_cannot_break_the_table():
    text = render_text(
        None, [["k", 1]], {"heading": "H", "columns": ["a|b"], "rows": [["x|y\nz"]]}
    )
    assert text.endswith("| a\\|b |\n|---|\n| x\\|y<br>z |")


def test_a_group_with_no_rows_still_renders_its_header():
    text = render_text(None, [["k", 1]], {"heading": "H", "columns": ["a"], "rows": []})
    assert text.endswith("## H\n| a |\n|---|")
