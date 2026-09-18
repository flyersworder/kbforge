import pytest

from kbforge.synthesize import concept_path
from kbforge_sql.errors import SqlSourceError
from kbforge_sql.identity import native_id_for


def test_a_plain_id_is_unchanged():
    assert native_id_for(["IMC300"], ["product_id"]) == "IMC300"


def test_an_int_id_is_stringified():
    assert native_id_for([42], ["id"]) == "42"


def test_composite_ids_join_with_a_slash():
    assert native_id_for(["EV", "IMC300"], ["app", "product"]) == "EV/IMC300"


def test_composites_containing_slashes_stay_distinct():
    a = native_id_for(["a/b", "c"], ["x", "y"])
    b = native_id_for(["a", "b/c"], ["x", "y"])
    assert a != b
    assert a == "a%2Fb/c"


def test_percent_is_escaped_so_the_escape_is_injective():
    assert native_id_for(["a%2Fb"], ["x"]) != native_id_for(["a/b"], ["x"])


def test_windows_unsafe_characters_are_escaped():
    assert (
        native_id_for(['a:b<c>"d|e?f*g\\h'], ["x"])
        == "a%3Ab%3Cc%3E%22d%7Ce%3Ff%2Ag%5Ch"
    )


def test_a_trailing_dot_or_space_is_escaped():
    assert native_id_for(["a."], ["x"]) == "a%2E"
    assert native_id_for(["a "], ["x"]) == "a%20"


def test_dot_dot_cannot_escape_the_bundle():
    assert native_id_for([".."], ["x"]) == ".%2E"


def test_a_md_suffix_survives_concept_path():
    a, b = native_id_for(["x.md"], ["k"]), native_id_for(["x"], ["k"])
    assert concept_path(f"s:{a}") != concept_path(f"s:{b}")


@pytest.mark.parametrize("value", [None, "", "  "])
def test_a_missing_id_value_is_an_error_naming_the_column(value):
    with pytest.raises(SqlSourceError, match="id column 'product_id' is empty"):
        native_id_for([value], ["product_id"])
