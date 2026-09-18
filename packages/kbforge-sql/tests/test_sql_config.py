import pytest

from kbforge_sql.config import SqlSourceConfig, check_columns, problems_for
from kbforge_sql.errors import SqlSourceError


def _cfg(**over):
    cfg = {
        "system": "products",
        "url_env": "KB_SQL_URL",
        "query": "SELECT 1",
        "id": ["product_id"],
        "title": "product_name",
    }
    cfg.update(over)
    return cfg


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KB_SQL_URL", "sqlite://")
    monkeypatch.setenv("KB_SQL_PASSWORD", "pw")


def test_a_minimal_config_is_valid():
    assert problems_for(_cfg()) == []


def test_an_unknown_key_is_an_error_not_ignored():
    assert any("qurey" in p for p in problems_for(_cfg(qurey="SELECT 2")))


@pytest.mark.parametrize("system", ["", "a:b", "a/b", "-x", "a b"])
def test_system_must_be_a_short_identifier(system):
    assert any("config 'system'" in p for p in problems_for(_cfg(system=system)))


def test_an_env_name_holding_a_value_is_rejected_without_echoing_it():
    problems = problems_for(_cfg(url_env="postgresql://u:s3cret@h/db"))
    assert any(
        "config 'url_env' must name an environment variable" in p for p in problems
    )
    assert not any("s3cret" in p for p in problems)


def test_unset_env_vars_are_reported(monkeypatch):
    monkeypatch.delenv("KB_SQL_URL")
    problems = problems_for(_cfg(password_env="MISSING_PW"))
    assert "config 'url_env': environment variable KB_SQL_URL is not set" in problems
    assert (
        "config 'password_env': environment variable MISSING_PW is not set" in problems
    )


def test_a_set_password_env_is_accepted():
    assert problems_for(_cfg(password_env="KB_SQL_PASSWORD")) == []


def test_a_blank_query_is_rejected():
    assert "config 'query' is blank" in problems_for(_cfg(query="  \n"))


def test_a_blank_type_is_rejected():
    assert any("config 'type' is blank" in p for p in problems_for(_cfg(type=" ")))


def test_a_blank_title_is_rejected():
    assert "config 'title' is blank" in problems_for(_cfg(title="  "))


def test_a_blank_id_column_is_rejected():
    problems = problems_for(_cfg(id=["product_id", " "]))
    assert "config 'id' has a blank column name" in problems


def test_an_empty_id_list_is_rejected():
    assert any(p.startswith("config id:") for p in problems_for(_cfg(id=[])))


def test_used_columns_must_not_be_excluded():
    problems = problems_for(_cfg(facets=["family"], exclude=["family", "product_id"]))
    assert (
        "config 'exclude' names column(s) the source also uses: "
        "['family', 'product_id']"
    ) in problems


def test_a_facet_named_like_an_okf_key_is_rejected():
    problems = problems_for(_cfg(facets=["type", "status"]))
    assert any(
        p.startswith("config 'facets' must not use OKF-owned key(s) ['type']")
        for p in problems
    )


def test_group_children_must_not_include_id_columns():
    problems = problems_for(_cfg(group={"children": ["product_id", "x"]}))
    assert (
        "config 'group.children' must not include id column(s) ['product_id']"
        in problems
    )


def test_group_children_must_not_include_entity_columns():
    problems = problems_for(
        _cfg(text="description", facets=["family"], group={"children": ["family", "x"]})
    )
    assert any(
        p.startswith(
            "config 'group.children' must not include title, text or facet column(s) "
            "['family']"
        )
        for p in problems
    )


def test_group_order_by_must_be_a_subset_of_children():
    problems = problems_for(_cfg(group={"children": ["a"], "order_by": ["a", "b"]}))
    assert (
        "config 'group.order_by' must be a subset of 'group.children': ['b']"
        in problems
    )


def test_url_template_may_only_reference_id_columns():
    problems = problems_for(_cfg(url_template="https://p/{product_id}/{family}"))
    assert (
        "config 'url_template' may only reference id column(s): ['family']" in problems
    )


def test_a_malformed_url_template_is_reported():
    problems = problems_for(_cfg(url_template="https://p/{product_id"))
    assert "config 'url_template' is not a valid format string" in problems


@pytest.mark.parametrize(
    ("key", "value"),
    [("retries", -1), ("max_removed_fraction", 0), ("max_removed_fraction", 1.5)],
)
def test_numeric_bounds(key, value):
    assert any(
        p.startswith(f"config {key}:") for p in problems_for(_cfg(**{key: value}))
    )


def test_configured_columns_are_deduplicated_in_order():
    cfg = SqlSourceConfig.model_validate(
        _cfg(
            text="d",
            facets=["f", "d"],
            exclude=["x"],
            group={"children": ["c"], "order_by": ["c"]},
        )
    )
    assert cfg.configured_columns() == [
        "product_id",
        "product_name",
        "d",
        "f",
        "x",
        "c",
    ]


def test_check_columns_lists_what_the_query_returned():
    cfg = SqlSourceConfig.model_validate(_cfg(facets=["famliy"]))
    with pytest.raises(SqlSourceError) as exc:
        check_columns(cfg, ["product_id", "product_name", "family"])
    assert str(exc.value) == (
        "configured column(s) ['famliy'] are not in the query result; "
        "the query returned: ['product_id', 'product_name', 'family']"
    )


def test_check_columns_passes_when_all_present():
    cfg = SqlSourceConfig.model_validate(_cfg())
    check_columns(cfg, ["product_id", "product_name"])
