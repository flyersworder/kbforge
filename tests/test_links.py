"""Editorial links (#41, architecture.md §7.4): config, resolution, sidecar."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.links import LinksConfig, expand, links_problems, load_links


def _cfg(links: dict) -> LinksConfig:
    return LinksConfig.model_validate({"links": links})


def test_a_plain_id_and_an_entry_both_load_and_expand():
    cfg = _cfg({"a:x": ["b:y", {"to": "b:z", "note": "why", "symmetric": True}]})
    assert links_problems(cfg) == []
    assert expand(cfg) == {
        "a:x": {"b:y": None, "b:z": "why"},
        "b:z": {"a:x": "why"},
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"link": {}},  # a typo'd top-level key
        {"links": {"a:x": [{"to": "b:y", "symetric": True}]}},  # a typo'd entry key
    ],
)
def test_an_unknown_key_is_rejected(raw):
    with pytest.raises(ValidationError):
        LinksConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("links", "message"),
    [
        (
            {"x": ["b:y"]},
            "links key 'x' must be a qualified doc_id ('system:native_id'); "
            "bare ids are not accepted",
        ),
        (
            {"a:x": ["y"]},
            "link 'y' under 'a:x' must be a qualified doc_id "
            "('system:native_id'); bare ids are not accepted",
        ),
        ({"a:x": ["a:x"]}, "link under 'a:x' points at itself"),
        (
            {"a:x": [{"to": "b:y", "note": "  "}]},
            "note on 'a:x' -> 'b:y' must be one non-blank line",
        ),
        (
            {"a:x": [{"to": "b:y", "note": "two\nlines"}]},
            "note on 'a:x' -> 'b:y' must be one non-blank line",
        ),
        ({"a:x": ["b:y", "b:y"]}, "'a:x' -> 'b:y' is declared more than once"),
        (
            {"a:x": [{"to": "b:y", "symmetric": True}], "b:y": ["a:x"]},
            "'b:y' -> 'a:x' is declared more than once",
        ),
    ],
)
def test_each_problem_is_reported_by_text(links, message):
    assert message in links_problems(_cfg(links))


def test_an_unresolvable_id_is_not_a_config_problem():
    # It may live in a system that has not synced yet (§3.1).
    assert links_problems(_cfg({"a:x": ["never:synced"]})) == []


def test_no_path_means_no_config():
    assert load_links(None) is None


def test_an_empty_file_is_an_empty_config(tmp_path: Path):
    path = tmp_path / "links.yaml"
    path.write_text("", "utf-8")
    assert load_links(path) == LinksConfig()
