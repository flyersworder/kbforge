"""Editorial links (#41, architecture.md §7.4): config, resolution, sidecar."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.links import (
    LINKS_DIR,
    LinksConfig,
    delete_links,
    expand,
    has_links_sidecars,
    links_drifted,
    links_problems,
    load_links,
    read_links,
    resolve_links,
    write_links,
)
from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument, ResourceAnchor

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _doc(doc_id: str, relations: tuple[str, ...] = ()) -> CanonicalDocument:
    system, _, native = doc_id.partition(":")
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=system, native_id=native, retrieved_at=NOW, content_hash="h"
        ),
        doc_id=doc_id,
        title=native,
        text=native,
        relations=list(relations),
    )


def _by_id(*docs: CanonicalDocument) -> dict[str, CanonicalDocument]:
    return {d.doc_id: d for d in docs}


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
    # It may live in a system that has not synced yet (architecture.md §7.4).
    assert links_problems(_cfg({"a:x": ["never:synced"]})) == []


def test_no_path_means_no_config():
    assert load_links(None) is None


def test_an_empty_file_is_an_empty_config(tmp_path: Path):
    path = tmp_path / "links.yaml"
    path.write_text("", "utf-8")
    assert load_links(path) == LinksConfig()


def test_relations_and_editorial_links_resolve_by_doc_id():
    x = _doc("a:x", ("a:y",))
    res = resolve_links(x, {"a:x": {"b:z": "why"}}, _by_id(x, _doc("a:y"), _doc("b:z")))
    assert res.links == [("a:y", None), ("b:z", "why")]
    assert res.managed == [("b:z", "why")]  # a same-system relation is not
    assert res.declares_managed
    assert res.notes == []


def test_a_cross_system_relation_is_managed():
    x = _doc("a:x", ("b:z",))
    res = resolve_links(x, {}, _by_id(x, _doc("b:z")))
    assert res.managed == [("b:z", None)]


def test_a_note_on_a_same_system_relation_makes_it_managed():
    x = _doc("a:x", ("a:y",))
    res = resolve_links(x, {"a:x": {"a:y": "why"}}, _by_id(x, _doc("a:y")))
    assert res.links == res.managed == [("a:y", "why")]


def test_a_missing_editorial_target_is_dropped_with_a_note():
    x = _doc("a:x")
    res = resolve_links(x, {"a:x": {"b:z": None}}, _by_id(x))
    assert res.links == res.managed == []
    assert res.declares_managed  # so the pipeline writes an EMPTY sidecar
    assert res.notes == [
        "concepts/x/overview.md: link to b:z (links.yaml) is not published yet "
        "and was dropped; it is added once its target is"
    ]


def test_a_missing_cross_system_relation_is_dropped_with_a_note():
    x = _doc("a:x", ("b:z",))
    res = resolve_links(x, {}, _by_id(x))
    assert res.links == res.managed == []
    assert res.declares_managed
    assert res.notes == [
        "concepts/x/overview.md: link to b:z (relation into system b) is not "
        "published yet and was dropped; it is added once its target is"
    ]


def test_a_missing_relation_is_dropped_silently():
    x = _doc("a:x", ("a:ghost",))
    res = resolve_links(x, {}, _by_id(x))
    assert (res.links, res.notes, res.declares_managed) == ([], [], False)


def test_a_self_link_is_ignored():
    x = _doc("a:x", ("a:x",))
    assert resolve_links(x, {"a:x": {"a:x": None}}, _by_id(x)).links == []


def test_a_tombstoned_target_is_dropped():
    x = _doc("a:x", ("b:z",))
    gone = _doc("b:z").model_copy(update={"deleted": True})
    assert resolve_links(x, {}, _by_id(x, gone)).links == []


def test_the_sidecar_round_trips(tmp_path: Path):
    managed: list[tuple[str, str | None]] = [("b:w", None), ("b:z", "why")]
    assert not has_links_sidecars(tmp_path)
    write_links(tmp_path, "a:x", managed)
    assert has_links_sidecars(tmp_path)
    assert read_links(tmp_path, "a:x") == managed
    delete_links(tmp_path, "a:x")
    assert read_links(tmp_path, "a:x") == []
    delete_links(tmp_path, "a:x")  # idempotent


def test_an_empty_sidecar_still_counts_for_the_gate(tmp_path: Path):
    write_links(tmp_path, "a:x", [])
    assert has_links_sidecars(tmp_path)


def test_an_unreadable_sidecar_reads_as_empty(tmp_path: Path):
    path = tmp_path / LINKS_DIR / f"{slot_key('a:x')}.json"
    path.parent.mkdir()
    path.write_text("{torn", "utf-8")
    assert read_links(tmp_path, "a:x") == []


def test_drift_compares_targets_and_notes(tmp_path: Path):
    write_links(tmp_path, "a:x", [("b:z", None)])
    write_links(tmp_path, "a:n", [("b:z", "old")])
    docs = [_doc(i) for i in ("a:x", "a:n", "a:y", "a:w")]
    current: dict[str, list[tuple[str, str | None]]] = {
        "a:x": [("b:z", None)],  # unchanged
        "a:n": [("b:z", "new")],  # only the note moved
        "a:y": [],  # no sidecar, nothing managed: settled
        "a:w": [("b:v", None)],  # no sidecar reads as empty, not exempt
    }
    assert links_drifted(tmp_path, docs, current) == ["a:n", "a:w"]
