from pathlib import Path

import pytest
from pydantic import ValidationError

from kbforge.grounding import (
    GroundingConfig,
    load_grounding,
    problems_for,
    template_fields,
)


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
