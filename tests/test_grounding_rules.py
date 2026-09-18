from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.grounding import (
    GroundingConfig,
    load_grounding,
    problems_for,
    template_fields,
)
from kbforge.models import CanonicalDocument, ResourceAnchor


def _rule(**over):
    rule = {
        "for": {"type": "product"},
        "from": {"system": "web"},
        "match": ["{native_id}"],
    }
    rule.update(over)
    return rule


def _cfg(*rules, **top):
    return GroundingConfig.model_validate({"rules": list(rules), **top})


def test_rules_load_from_yaml(tmp_path: Path):
    path = tmp_path / "g.yaml"
    path.write_text(
        "rules:\n"
        "  - for: {type: product}\n"
        "    from: {system: web}\n"
        "    match: ['{native_id}']\n"
        "    newest: 2\n"
        "    by: published\n",
        "utf-8",
    )
    cfg = load_grounding(path)
    (rule,) = cfg.rules
    assert rule.for_.type == "product" and rule.from_.system == "web"
    assert rule.match == ["{native_id}"] and rule.newest == 2
    assert rule.by == "published"


def test_newest_defaults_to_three():
    assert _cfg(_rule()).rules[0].newest == 3


def test_a_valid_rule_has_no_problems():
    assert problems_for(_cfg(_rule())) == []


@pytest.mark.parametrize("key", ["newset", "matches"])
def test_an_unknown_rule_key_is_refused(key):
    with pytest.raises(ValidationError):
        _cfg(_rule(**{key: 1}))


def test_a_missing_from_is_refused():
    rule = _rule()
    del rule["from"]
    with pytest.raises(ValidationError):
        _cfg(rule)


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (
            _rule(**{"for": {}}),
            "grounding rule 1: 'for' needs at least one of 'type', 'system', 'doc'",
        ),
        (
            _rule(**{"for": {"doc": []}}),
            "grounding rule 1: 'for' needs at least one of 'type', 'system', 'doc'",
        ),
        (
            _rule(**{"for": {"doc": ["bare-id"]}}),
            "grounding rule 1: 'for.doc' entry 'bare-id' must be a qualified "
            "doc_id ('system:native_id')",
        ),
        (_rule(match=[]), "grounding rule 1: 'match' needs at least one phrase"),
        (_rule(match=["  "]), "grounding rule 1: a 'match' phrase is blank"),
        (
            _rule(match=["{native_id"]),
            "grounding rule 1: 'match' phrase '{native_id' has unpaired braces; "
            "fields are {name}",
        ),
        (_rule(newest=0), "grounding rule 1: 'newest' must be at least 1"),
    ],
)
def test_rule_problems_are_reported(rule, message):
    assert message in problems_for(_cfg(rule))


def test_problems_name_the_rule_by_position():
    problems = problems_for(_cfg(_rule(), _rule(newest=0)))
    assert problems == ["grounding rule 2: 'newest' must be at least 1"]


def test_template_fields():
    assert template_fields("{native_id} and {family}") == ["native_id", "family"]
    assert template_fields("traction inverter") == []
    assert template_fields("{a") is None
    assert template_fields("a}") is None


from kbforge.grounding import fill, rule_matches  # noqa: E402


def _doc(doc_id, title="T", text="", structured=None, deleted=False):
    system, _, native = doc_id.partition(":")
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native,
            retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash=f"h-{doc_id}",
        ),
        doc_id=doc_id,
        title=title,
        text=text,
        structured=structured or {},
        deleted=deleted,
    )


PRODUCT = _doc(
    "sql:IMC300",
    "IMC300 motor controller",
    structured={
        "type": "product",
        "family": "MOTIX",
    },
)


def _by_id(*docs):
    return {d.doc_id: d for d in docs}


def _ids(matches):
    return [doc_id for doc_id, _ in matches[0]]


def test_fill_uses_facets_and_reserved_names():
    assert fill("{native_id}", PRODUCT) == "IMC300"
    assert fill("{title}", PRODUCT) == "IMC300 motor controller"
    assert fill("{family} drivers", PRODUCT) == "MOTIX drivers"
    assert fill("traction inverter", PRODUCT) == "traction inverter"


@pytest.mark.parametrize("template", ["{missing}", "{blank}", "x {missing}"])
def test_a_phrase_with_a_missing_or_blank_field_is_dropped(template):
    owner = _doc("sql:A", structured={"type": "product", "blank": "  "})
    assert fill(template, owner) is None


def test_matching_is_case_insensitive_and_on_word_boundaries():
    cfg = _cfg(_rule())
    hit = _doc("web:a", text="Skyworks' driver rivals the imc300 in EVs.")
    near = _doc("web:b", text="The IMC3001 is unrelated.")
    assert _ids(rule_matches(PRODUCT, cfg, _by_id(PRODUCT, hit, near), {})) == ["web:a"]


def test_matching_reads_title_and_nfc_normalizes():
    owner = _doc("sql:X", structured={"type": "product", "name": "café"})
    cfg = _cfg(_rule(match=["{name}"]))
    # Decomposed café (NFD) in the candidate's title
    decomposed = _doc("web:a", title="Le café news", text="")
    assert _ids(rule_matches(owner, cfg, _by_id(owner, decomposed), {})) == ["web:a"]


def test_a_phrase_ending_in_punctuation_still_matches():
    owner = _doc("sql:X", structured={"type": "product"})
    cfg = _cfg(_rule(match=["SiC-MOSFET (1200V)"]))
    hit = _doc("web:a", text="A new SiC-MOSFET (1200V) part.")
    assert _ids(rule_matches(owner, cfg, _by_id(owner, hit), {})) == ["web:a"]


def test_for_and_from_scope_the_rule():
    cfg = _cfg(_rule())
    other_type = _doc("sql:IMC300b", structured={"type": "application"})
    wrong_system = _doc("news:a", text="IMC300")
    by_id = _by_id(PRODUCT, other_type, wrong_system)
    assert _ids(rule_matches(PRODUCT, cfg, by_id, {})) == []
    assert _ids(rule_matches(other_type, cfg, by_id, {})) == []


def test_for_doc_and_for_system_select_owners():
    app = _doc("local:apps/ev.md", structured={"type": "concept"})
    cfg = _cfg(_rule(**{"for": {"doc": ["local:apps/ev.md"]}, "match": ["SiC"]}))
    hit = _doc("web:a", text="SiC demand grows")
    assert _ids(rule_matches(app, cfg, _by_id(app, hit), {})) == ["web:a"]
    cfg2 = _cfg(_rule(**{"for": {"system": "local"}, "match": ["SiC"]}))
    assert _ids(rule_matches(app, cfg2, _by_id(app, hit), {})) == ["web:a"]


def test_a_document_never_grounds_itself_and_tombstones_are_skipped():
    cfg = _cfg(_rule(**{"from": {"system": "sql"}}))
    dead = _doc("sql:old", text="IMC300", deleted=True)
    assert _ids(rule_matches(PRODUCT, cfg, _by_id(PRODUCT, dead), {})) == []


def test_newest_first_by_first_seen_then_doc_id_and_capped():
    cfg = _cfg(_rule(newest=2))
    a, b, c = (_doc(f"web:{n}", text="IMC300") for n in "abc")
    seen = {
        "web:a": datetime(2026, 1, 1, tzinfo=UTC),
        "web:b": datetime(2026, 3, 1, tzinfo=UTC),
        "web:c": datetime(2026, 3, 1, tzinfo=UTC),
    }
    matches, notes = rule_matches(PRODUCT, cfg, _by_id(PRODUCT, a, b, c), seen)
    assert [d for d, _ in matches] == ["web:b", "web:c"]
    assert notes == ["concepts/IMC300/overview.md: rule 1 capped at 2; dropped web:a"]


def test_a_by_facet_outranks_first_seen_and_falls_back_when_absent():
    cfg = _cfg(_rule(by="published"))
    old_pub = _doc("web:a", text="IMC300", structured={"published": "2025-01-01"})
    new_pub = _doc("web:b", text="IMC300", structured={"published": date(2026, 5, 1)})
    no_pub = _doc("web:c", text="IMC300")
    seen = {"web:c": datetime(2026, 2, 1, tzinfo=UTC)}
    by_id = _by_id(PRODUCT, old_pub, new_pub, no_pub)
    assert _ids(rule_matches(PRODUCT, cfg, by_id, seen)) == ["web:b", "web:c", "web:a"]


def test_an_unparseable_by_value_falls_back_with_a_note():
    cfg = _cfg(_rule(by="published"))
    odd = _doc("web:a", text="IMC300", structured={"published": "last Tuesday"})
    matches, notes = rule_matches(PRODUCT, cfg, _by_id(PRODUCT, odd), {})
    assert [d for d, _ in matches] == ["web:a"]
    assert notes == [
        "concepts/IMC300/overview.md: rule 1: web:a has an unparseable "
        "'published' value 'last Tuesday'; ranked by first-seen"
    ]


def test_undated_candidates_rank_last_by_doc_id():
    cfg = _cfg(_rule())
    a, b = _doc("web:b", text="IMC300"), _doc("web:a", text="IMC300")
    dated = _doc("web:z", text="IMC300")
    seen = {"web:z": datetime(2026, 1, 1, tzinfo=UTC)}
    assert _ids(rule_matches(PRODUCT, cfg, _by_id(PRODUCT, a, b, dated), seen)) == [
        "web:z",
        "web:a",
        "web:b",
    ]


def test_the_reason_note_names_rule_phrase_and_document():
    cfg = _cfg(_rule())
    hit = _doc("web:a", text="IMC300")
    ((doc_id, reason),), _ = rule_matches(PRODUCT, cfg, _by_id(PRODUCT, hit), {})
    assert reason == (
        "concepts/IMC300/overview.md: grounded by rule 1 "
        "('{native_id}' = 'IMC300') via web:a"
    )


def test_a_document_matched_by_two_rules_is_listed_once():
    cfg = _cfg(_rule(), _rule(match=["{title}"]))
    hit = _doc("web:a", text="IMC300 motor controller")
    assert _ids(rule_matches(PRODUCT, cfg, _by_id(PRODUCT, hit), {})) == ["web:a"]


def test_a_rule_with_only_missing_fields_matches_nothing():
    owner = _doc("sql:A", structured={"type": "product"})
    cfg = _cfg(_rule(match=["{missing}"]))
    hit = _doc("web:a", text="whatever will match everything")
    assert _ids(rule_matches(owner, cfg, _by_id(owner, hit), {})) == []


from kbforge.grounding import (  # noqa: E402
    FIRST_SEEN_DIR,
    delete_first_seen,
    load_first_seen,
    record_first_seen,
)
from kbforge.mirror import load_all, slot_key  # noqa: E402


def _stamped(doc_id, when):
    doc = _doc(doc_id)
    doc.anchor.retrieved_at = when
    return doc


def test_first_seen_is_written_once_and_kept(tmp_path: Path):
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    record_first_seen(tmp_path, [_stamped("web:a", t1)])
    record_first_seen(tmp_path, [_stamped("web:a", t2), _stamped("web:b", t2)])
    assert load_first_seen(tmp_path) == {"web:a": t1, "web:b": t2}


def test_tombstones_are_not_recorded_and_delete_is_idempotent(tmp_path: Path):
    dead = _doc("web:a", deleted=True)
    record_first_seen(tmp_path, [dead])
    assert load_first_seen(tmp_path) == {}
    record_first_seen(tmp_path, [_doc("web:b")])
    delete_first_seen(tmp_path, "web:b")
    delete_first_seen(tmp_path, "web:b")
    assert load_first_seen(tmp_path) == {}


def test_a_naive_retrieved_at_is_recorded_as_utc(tmp_path: Path):
    record_first_seen(tmp_path, [_stamped("web:a", datetime(2026, 1, 1))])
    assert load_first_seen(tmp_path)["web:a"] == datetime(2026, 1, 1, tzinfo=UTC)


def test_an_unreadable_record_reads_as_absent_and_is_rewritten(tmp_path: Path):
    path = tmp_path / FIRST_SEEN_DIR / f"{slot_key('web:a')}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", "utf-8")
    assert load_first_seen(tmp_path) == {}
    when = datetime(2026, 2, 2, tzinfo=UTC)
    record_first_seen(tmp_path, [_stamped("web:a", when)])
    assert load_first_seen(tmp_path) == {"web:a": when}


def test_first_seen_is_invisible_to_load_all(tmp_path: Path):
    record_first_seen(tmp_path, [_doc("web:a")])
    assert load_all(tmp_path) == []


def test_first_seen_writes_leave_no_temp_files(tmp_path: Path):
    record_first_seen(tmp_path, [_doc("web:a")])
    assert [p.suffix for p in (tmp_path / FIRST_SEEN_DIR).iterdir()] == [".json"]


from kbforge.grounding import resolve_all  # noqa: E402


def test_without_rules_resolve_all_is_resolve():
    owner = _doc("sql:A")
    target = _doc("web:x")
    cfg = GroundingConfig(grounding={"sql:A": ["web:x"]})
    docs, notes = resolve_all(owner, cfg, _by_id(owner, target), {})
    assert [d.doc_id for d in docs] == ["web:x"] and notes == []


def test_explicit_grounding_comes_first_and_keeps_its_own_cap():
    owner = _doc("sql:IMC300", structured={"type": "product"})
    picked = [_doc(f"web:p{i}") for i in range(3)]
    news = [_doc(f"web:n{i}", text="IMC300") for i in range(2)]
    cfg = GroundingConfig.model_validate(
        {
            "max_grounding_docs": 2,
            "grounding": {"sql:IMC300": [d.doc_id for d in picked]},
            "rules": [_rule()],
        }
    )
    docs, notes = resolve_all(owner, cfg, _by_id(owner, *picked, *news), {})
    assert [d.doc_id for d in docs] == ["web:p0", "web:p1", "web:n0", "web:n1"]
    assert any("grounding capped at 2" in n for n in notes)


def test_a_rule_match_already_cited_explicitly_is_cited_once_without_a_reason():
    owner = _doc("sql:IMC300", structured={"type": "product"})
    both = _doc("web:a", text="IMC300")
    cfg = GroundingConfig.model_validate(
        {"grounding": {"sql:IMC300": ["web:a"]}, "rules": [_rule()]}
    )
    docs, notes = resolve_all(owner, cfg, _by_id(owner, both), {})
    assert [d.doc_id for d in docs] == ["web:a"]
    assert not any("grounded by rule" in n for n in notes)


def test_rule_documents_carry_their_reason_notes():
    owner = _doc("sql:IMC300", structured={"type": "product"})
    hit = _doc("web:a", text="IMC300")
    docs, notes = resolve_all(owner, _cfg(_rule()), _by_id(owner, hit), {})
    assert [d.doc_id for d in docs] == ["web:a"]
    assert notes == [
        "concepts/IMC300/overview.md: grounded by rule 1 "
        "('{native_id}' = 'IMC300') via web:a"
    ]
