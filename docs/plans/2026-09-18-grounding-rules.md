# Grounding Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the grounding config carry templated rules that ground existing concepts in matching documents from another system, ranked newest-first, so web articles refresh the concepts they're about.

**Architecture:** All rule logic is pure and lives in `src/kbforge/grounding.py`: rule models and validation, placeholder filling, matching, ordering, and a `resolve_all` that unions explicit grounding with rule matches. One small new piece of mirror state, first-seen records, sits beside the grounding sidecars. `pipeline.run` changes in three places: the scan gate, the `_resolved` closure, and the commit step. The existing drift sidecar does the staleness work unchanged.

**Tech Stack:** Python ≥3.12, pydantic v2, pytest; ruff + ty via prek.

**Spec:** `docs/design/2026-09-18-grounding-rules-design.md`. Read it before Task 1; every behaviour below argues from it.

## Global Constraints

- Only `src/kbforge/grounding.py`, `src/kbforge/pipeline.py`, `src/kbforge/__main__.py`, tests and docs change. No connector, synthesizer, validator or companion package changes.
- Rule selection is deterministic: no clock, randomness or network in `grounding.py` except the file-state functions. The synthesizer receives an already-resolved list.
- Explicit grounding (`grounded_by` + subject map) behaves exactly as today, including its `max_grounding_docs` cap. Every existing test in `tests/test_grounding.py` and `tests/test_pipeline.py` must keep passing unmodified.
- Placeholders use plain `{name}` substitution (`re.compile(r"\{([^{}]*)\}")`), never `str.format`.
- Phrase matching is case-insensitive, NFC-normalized, on word boundaries implemented as `(?<!\w)…(?!\w)` around `re.escape(phrase)`. That's `\b` semantics that still works when a phrase starts or ends with a non-word character.
- First-seen records: `mirror/_first_seen/<slot_key(doc_id)>.json`, written atomically, write-once, only in the commit step of a publishing run, deleted on tombstone. An unreadable record reads as absent.
- Rule-model config is `extra="forbid"` everywhere.
- Commit before any mutation check; mutate in place and restore with `git checkout --`. Assert on failure *messages* (CLAUDE.md, "Verifying a gate").
- Commit messages: subject, blank line, `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Run `uv run ruff check --fix && uv run ruff format` before each commit. E501 (88 cols) applies and the formatter doesn't split strings; wrap long strings by hand.

---

## File structure

| File | Change |
|---|---|
| `src/kbforge/grounding.py` | rule models, `problems_for` checks, `template_fields`, `fill`, `rule_matches`, `resolve_all`, first-seen store, a shared `_write_atomic` |
| `src/kbforge/pipeline.py` | scan gate, `_resolved` → `resolve_all`, first-seen load/record/delete |
| `src/kbforge/__main__.py` | one-line notice when rules are configured with the stub synthesizer |
| `tests/test_grounding_rules.py` | new: validation, matching, ordering, first-seen, resolution |
| `tests/test_pipeline.py` | `_doc` gains `structured=`; new end-to-end rule tests |
| `docs/architecture.md` §7.1, `CHANGELOG.md`, the design note | docs (Task 6) |

---

### Task 1: Rule models and validation

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding_rules.py` (create)

**Interfaces:**
- Produces: `RuleFor(type: str | None, system: str | None, doc: list[str] | None)`; `RuleFrom(system: str)`; `GroundingRule(for_: RuleFor [alias "for"], from_: RuleFrom [alias "from"], match: list[str], newest: int = 3, by: str | None = None)`; `GroundingConfig.rules: list[GroundingRule]` (default `[]`); `template_fields(template: str) -> list[str] | None`; new messages in `problems_for`.

- [ ] **Step 1: Write the failing tests**

`tests/test_grounding_rules.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_grounding_rules.py -q`
Expected: collection error, `ImportError: cannot import name 'template_fields'`.

- [ ] **Step 3: Implement**

In `src/kbforge/grounding.py`, add `import re` to the imports. Add these models above `GroundingConfig`:

```python
class RuleFor(BaseModel):
    """Which concepts a rule grounds. Keys present are AND-ed."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    system: str | None = None
    doc: list[str] | None = None


class RuleFrom(BaseModel):
    """Which documents may ground them."""

    model_config = ConfigDict(extra="forbid")

    system: str


class GroundingRule(BaseModel):
    """A templated grounding rule (design note 2026-09-18). `for`/`from` are
    Python keywords, hence the aliases."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    for_: RuleFor = Field(alias="for")
    from_: RuleFrom = Field(alias="from")
    match: list[str]
    newest: int = 3
    by: str | None = None
```

Add a field to `GroundingConfig`:

```python
    rules: list[GroundingRule] = Field(default_factory=list)
```

Add after `is_qualified`:

```python
# Plain `{name}` substitution, deliberately not str.format, which reads `{a.b}`
# as an attribute and `{a:{w}}` as a nested field (the kbforge-sql url_template
# lesson). One pattern serves validation and filling.
_FIELD = re.compile(r"\{([^{}]*)\}")


def template_fields(template: str) -> list[str] | None:
    """Placeholder names in a match phrase, or None if its braces don't pair
    up into `{name}` fields."""
    rest = _FIELD.sub("", template)
    if "{" in rest or "}" in rest:
        return None
    return _FIELD.findall(template)
```

At the end of `problems_for`, before `return problems`:

```python
    for i, rule in enumerate(cfg.rules, 1):
        where = f"grounding rule {i}"
        if not (rule.for_.type or rule.for_.system or rule.for_.doc):
            problems.append(
                f"{where}: 'for' needs at least one of 'type', 'system', 'doc'"
            )
        for doc_id in rule.for_.doc or []:
            if not is_qualified(doc_id):
                problems.append(
                    f"{where}: 'for.doc' entry {doc_id!r} must be a qualified "
                    "doc_id ('system:native_id')"
                )
        if not rule.match:
            problems.append(f"{where}: 'match' needs at least one phrase")
        for phrase in rule.match:
            if not phrase.strip():
                problems.append(f"{where}: a 'match' phrase is blank")
            elif template_fields(phrase) is None:
                problems.append(
                    f"{where}: 'match' phrase {phrase!r} has unpaired braces; "
                    "fields are {name}"
                )
        if rule.newest < 1:
            problems.append(f"{where}: 'newest' must be at least 1")
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_grounding_rules.py tests/test_grounding.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding_rules.py
git commit -m "feat(grounding): rule models and offline validation" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Matching and ordering

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding_rules.py`

**Interfaces:**
- Consumes: Task 1's models and `_FIELD`.
- Produces: `fill(template: str, owner: CanonicalDocument) -> str | None`; `rule_matches(owner, cfg, by_id, first_seen) -> tuple[list[tuple[str, str]], list[str]]`. The first element is `(doc_id, reason_note)` in rank order, deduplicated across rules. The second is the other notes: cap drops and unparseable `by` values.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_grounding_rules.py`:

```python
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


PRODUCT = _doc("sql:IMC300", "IMC300 motor controller", structured={"type": "product", "family": "MOTIX"})


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
    assert _ids(rule_matches(PRODUCT, cfg, _by_id(PRODUCT, hit, near), {})) == [
        "web:a"
    ]


def test_matching_reads_title_and_nfc_normalizes():
    owner = _doc("sql:X", structured={"type": "product", "name": "café"})
    cfg = _cfg(_rule(match=["{name}"]))
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_grounding_rules.py -q`
Expected: `ImportError: cannot import name 'fill'`.

- [ ] **Step 3: Implement**

Add `import unicodedata` and `from datetime import UTC, date, datetime` to `grounding.py`'s imports. Add after `template_fields`:

```python
def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _field(owner: CanonicalDocument, name: str) -> str | None:
    if name == "title":
        value: object = owner.title
    elif name == "native_id":
        value = owner.anchor.native_id
    else:
        value = owner.structured.get(name)
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value).strip()
    return text or None


def fill(template: str, owner: CanonicalDocument) -> str | None:
    """A match phrase with its `{fields}` filled from `owner`, or None when any
    field is missing or blank. Matching an empty field would match everything,
    so such a phrase is dropped for this owner only."""
    missing = False

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        value = _field(owner, m.group(1))
        if value is None:
            missing = True
            return ""
        return value

    out = _FIELD.sub(sub, template)
    return None if missing or not out.strip() else out


def _applies(rule: GroundingRule, owner: CanonicalDocument) -> bool:
    scope = rule.for_
    kind = str(owner.structured.get("type") or "concept")
    if scope.type is not None and kind != scope.type:
        return False
    if scope.system is not None and owner.anchor.system != scope.system:
        return False
    return scope.doc is None or owner.doc_id in scope.doc


def _as_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    # Naive is taken as UTC: the one assumption needed to order it against an
    # aware value, and the convention kbforge-sql's canonical form documents.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def rule_matches(
    owner: CanonicalDocument,
    cfg: GroundingConfig,
    by_id: dict[str, CanonicalDocument],
    first_seen: dict[str, datetime],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Rule-selected grounding for `owner`: `(doc_id, reason)` in rank order,
    plus cap and unparseable-date notes. Pure and deterministic over `by_id`.

    Rank is newest first by the rule's `by` facet, else first-seen; undated
    candidates last; `doc_id` breaks every tie, so the order is total."""
    path = concept_path(owner.doc_id)
    matched: list[tuple[str, str]] = []
    listed: set[str] = set()
    notes: list[str] = []
    for i, rule in enumerate(cfg.rules, 1):
        if not _applies(rule, owner):
            continue
        phrases = [(t, p) for t in rule.match if (p := fill(t, owner)) is not None]
        patterns = [
            (t, p, re.compile(rf"(?<!\w){re.escape(_nfc(p))}(?!\w)", re.IGNORECASE))
            for t, p in phrases
        ]
        if not patterns:
            continue
        ranked: list[tuple[datetime | None, str, str]] = []
        for doc in by_id.values():
            if (
                doc.deleted
                or doc.doc_id == owner.doc_id
                or doc.anchor.system != rule.from_.system
            ):
                continue
            haystack = _nfc(f"{doc.title}\n{doc.text}")
            hit = next(((t, p) for t, p, rx in patterns if rx.search(haystack)), None)
            if hit is None:
                continue
            when = None
            if rule.by is not None:
                raw = doc.structured.get(rule.by)
                when = _as_time(raw)
                if raw is not None and when is None:
                    notes.append(
                        f"{path}: rule {i}: {doc.doc_id} has an unparseable "
                        f"{rule.by!r} value {raw!r}; ranked by first-seen"
                    )
            if when is None:
                when = first_seen.get(doc.doc_id)
            reason = (
                f"{path}: grounded by rule {i} ({hit[0]!r} = {hit[1]!r}) "
                f"via {doc.doc_id}"
            )
            ranked.append((when, doc.doc_id, reason))
        ranked.sort(
            key=lambda r: (r[0] is None, -r[0].timestamp() if r[0] else 0.0, r[1])
        )
        kept, dropped = ranked[: rule.newest], ranked[rule.newest :]
        if dropped:
            notes.append(
                f"{path}: rule {i} capped at {rule.newest}; dropped "
                + ", ".join(doc_id for _, doc_id, _ in dropped)
            )
        for _, doc_id, reason in kept:
            if doc_id not in listed:
                listed.add(doc_id)
                matched.append((doc_id, reason))
    return matched, notes
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_grounding_rules.py -q`
Expected: all pass. If `test_a_by_facet_outranks_first_seen…` fails on ordering, check that `date(2026, 5, 1)` converts to an aware datetime before comparison. Every compared value must be aware.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding_rules.py
git commit -m "feat(grounding): rule matching and newest-first ordering" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: First-seen records

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding_rules.py`

**Interfaces:**
- Produces: `FIRST_SEEN_DIR = "_first_seen"`; `record_first_seen(mirror: Path, docs: list[CanonicalDocument]) -> None`; `load_first_seen(mirror: Path) -> dict[str, datetime]`; `delete_first_seen(mirror: Path, doc_id: str) -> None`; a private `_write_atomic(path: Path, payload: dict) -> None`, which `write_sidecar` now also uses.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_grounding_rules.py -q`
Expected: `ImportError: cannot import name 'FIRST_SEEN_DIR'`.

- [ ] **Step 3: Implement**

In `grounding.py`, extract the body of `write_sidecar` into a helper, and make `write_sidecar` call it:

```python
def _write_atomic(path: Path, payload: dict) -> None:
    """Through a unique temp file in the same directory, so a process killed
    mid-write leaves the old file or the new one, never a truncated one, and two
    writers on the shared mirror never `os.replace` each other's half-written
    file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_sidecar(mirror: Path, doc_id: str, recorded: dict[str, str]) -> None:
    """Written atomically (`_write_atomic`); `read_sidecar` tolerates a torn file
    anyway, and this keeps them from being made."""
    payload = {"doc_id": doc_id, "grounding": dict(sorted(recorded.items()))}
    _write_atomic(_sidecar(mirror, doc_id), payload)
```

Keep the existing comment about unique temp names inside `_write_atomic`. Then add, after `drifted`:

```python
FIRST_SEEN_DIR = "_first_seen"
"""When a document first entered the mirror: the recency fallback for rules
whose date facet is absent (design note 2026-09-18 §5). A subdirectory for the
same reason as SIDECAR_DIR."""


def _first_seen_path(mirror: Path, doc_id: str) -> Path:
    return mirror / FIRST_SEEN_DIR / f"{slot_key(doc_id)}.json"


def _read_first_seen(path: Path) -> tuple[str, datetime] | None:
    """Tolerant like `read_sidecar`: an unreadable record reads as absent and is
    rewritten on the document's next commit, rather than wedging every run."""
    try:
        payload = json.loads(path.read_text("utf-8"))
        moment = datetime.fromisoformat(payload["first_seen"])
        return str(payload["doc_id"]), (
            moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return None


def record_first_seen(mirror: Path, docs: list[CanonicalDocument]) -> None:
    """Write-once, for documents a publishing run commits. Each run records only
    its own documents, so no run writes another connector's state."""
    for doc in docs:
        if doc.deleted:
            continue
        path = _first_seen_path(mirror, doc.doc_id)
        if _read_first_seen(path) is not None:
            continue
        when = doc.anchor.retrieved_at
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        _write_atomic(path, {"doc_id": doc.doc_id, "first_seen": when.isoformat()})


def load_first_seen(mirror: Path) -> dict[str, datetime]:
    directory = mirror / FIRST_SEEN_DIR
    if not directory.is_dir():
        return {}
    records = (_read_first_seen(p) for p in sorted(directory.glob("*.json")))
    return dict(r for r in records if r is not None)


def delete_first_seen(mirror: Path, doc_id: str) -> None:
    """Idempotent; called when a document is tombstoned."""
    _first_seen_path(mirror, doc_id).unlink(missing_ok=True)
```

`json.JSONDecodeError` is a `ValueError`, so the except tuple covers it.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_grounding_rules.py tests/test_grounding.py -q`
Expected: all pass, including the existing sidecar tests that now go through `_write_atomic`.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding_rules.py
git commit -m "feat(grounding): write-once first-seen records beside the sidecars" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Resolution — explicit first, then rules

**Files:**
- Modify: `src/kbforge/grounding.py`
- Test: `tests/test_grounding_rules.py`

**Interfaces:**
- Consumes: `resolve`, `declared_ids`, `rule_matches`.
- Produces: `resolve_all(owner, cfg, by_id, first_seen) -> tuple[list[CanonicalDocument], list[str]]`. Explicit documents come first, in `resolve`'s order and cap; rule documents follow in rank order, skipping anything already cited. Notes are `resolve`'s notes, then each *added* rule document's reason, then the rule-level notes.

- [ ] **Step 1: Write the failing tests**

Append:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_grounding_rules.py -q`
Expected: `ImportError: cannot import name 'resolve_all'`.

- [ ] **Step 3: Implement**

Add after `rule_matches` in `grounding.py`:

```python
def resolve_all(
    owner: CanonicalDocument,
    cfg: GroundingConfig,
    by_id: dict[str, CanonicalDocument],
    first_seen: dict[str, datetime],
) -> tuple[list[CanonicalDocument], list[str]]:
    """Everything that grounds `owner`: explicit grounding first, resolved and
    capped exactly as before, then rule matches on top. A hand-picked source is
    never crowded out by news, and one artifact is cited once."""
    kept, notes = resolve(
        owner, declared_ids(owner, cfg), by_id, max_docs=cfg.max_grounding_docs
    )
    if not cfg.rules:
        return kept, notes
    matched, rule_notes = rule_matches(owner, cfg, by_id, first_seen)
    seen = {resource_key(owner.anchor)} | {resource_key(d.anchor) for d in kept}
    reasons: list[str] = []
    for doc_id, reason in matched:
        doc = by_id.get(doc_id)
        if doc is None or doc.deleted:
            continue
        key = resource_key(doc.anchor)
        if key in seen:
            continue
        seen.add(key)
        kept.append(doc)
        reasons.append(reason)
    return kept, notes + reasons + rule_notes
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_grounding_rules.py tests/test_grounding.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/kbforge/grounding.py tests/test_grounding_rules.py
git commit -m "feat(grounding): resolve explicit grounding first, rule matches on top" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Pipeline integration and the CLI notice

**Files:**
- Modify: `src/kbforge/pipeline.py`, `src/kbforge/__main__.py`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `resolve_all`, `load_first_seen`, `record_first_seen`, `delete_first_seen`, `GroundingConfig.rules`.
- Produces: no new public API.

- [ ] **Step 1: Extend the `_doc` test helper**

In `tests/test_pipeline.py`, add a keyword argument `structured: dict | None = None` to `_doc` and pass `structured=structured or {},` to the `CanonicalDocument(...)` call (before `relations=`). Existing callers are unaffected.

- [ ] **Step 2: Write the failing pipeline tests**

Append to `tests/test_pipeline.py`:

```python
from kbforge.grounding import load_first_seen  # noqa: E402


def _rules_cfg():
    return GroundingConfig.model_validate(
        {
            "rules": [
                {
                    "for": {"type": "product"},
                    "from": {"system": "web"},
                    "match": ["{native_id}"],
                }
            ]
        }
    )


def _product():
    return _doc(
        "IMC300", "IMC300 motor controller", system="sql",
        structured={"type": "product"},
    )


def test_a_new_matching_article_regrounds_the_product_on_its_own_run(tmp_path):
    cfg = _rules_cfg()
    _run_once(tmp_path, [_product()], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="sql")

    article = _doc("skyworks", "Skyworks gate driver", system="web",
                   text="Skyworks unveils a driver that rivals the IMC300.")
    pub_web = _run_once(tmp_path, [article], synthesizer=_GroundingSynth(),
                        grounding_config=cfg, connector_name="web")
    # The web run never touches the product concept.
    assert concept_path("sql:IMC300") not in pub_web.last_change.files
    assert "web:skyworks" in load_first_seen(tmp_path / "mirror")

    synth = _GroundingSynth()
    pub = _run_once(tmp_path, [_product()], synthesizer=synth,
                    grounding_config=cfg, connector_name="sql")
    assert [d.doc_id for d in synth.seen["sql:IMC300"]] == ["web:skyworks"]
    notes = pub.last_change.summary.grounding_notes
    assert (
        "concepts/IMC300/overview.md: grounded by rule 1 "
        "('{native_id}' = 'IMC300') via web:skyworks"
    ) in notes

    result, _ = _run_result(tmp_path, [_product()], synthesizer=_GroundingSynth(),
                            grounding_config=cfg, connector_name="sql")
    assert isinstance(result, NoOp)


def test_a_non_matching_article_leaves_the_product_a_noop(tmp_path):
    cfg = _rules_cfg()
    _run_once(tmp_path, [_product()], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="sql")
    other = _doc("other", "Other news", system="web", text="Nothing relevant.")
    _run_once(tmp_path, [other], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="web")
    result, _ = _run_result(tmp_path, [_product()], synthesizer=_GroundingSynth(),
                            grounding_config=cfg, connector_name="sql")
    assert isinstance(result, NoOp)


def test_an_edited_matched_article_drifts_the_product(tmp_path):
    cfg = _rules_cfg()
    v1 = _doc("skyworks", "Skyworks", system="web", text="IMC300 rival, v1")
    _run_once(tmp_path, [_product()], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="sql")
    _run_once(tmp_path, [v1], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="web")
    _run_once(tmp_path, [_product()], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="sql")
    v2 = _doc("skyworks", "Skyworks", system="web", text="IMC300 rival, v2")
    _run_once(tmp_path, [v2], synthesizer=_GroundingSynth(),
              grounding_config=cfg, connector_name="web")
    pub = _run_once(tmp_path, [_product()], synthesizer=_GroundingSynth(),
                    grounding_config=cfg, connector_name="sql")
    assert concept_path("sql:IMC300") in pub.last_change.files


def test_first_seen_is_deleted_with_its_tombstone(tmp_path):
    a = _doc("a", "A", system="web")
    _run_once(tmp_path, [a], connector_name="web")
    assert "web:a" in load_first_seen(tmp_path / "mirror")
    _run_once(tmp_path, [_doc("a", "A", system="web", deleted=True)],
              connector_name="web")
    assert "web:a" not in load_first_seen(tmp_path / "mirror")


def test_a_noop_run_writes_no_first_seen(tmp_path):
    a = _doc("a", "A", system="web")
    _run_once(tmp_path, [a], connector_name="web")
    directory = tmp_path / "mirror" / "_first_seen"
    before = sorted(p.name for p in directory.iterdir())
    result, _ = _run_result(tmp_path, [a], connector_name="web")
    assert isinstance(result, NoOp)
    assert sorted(p.name for p in directory.iterdir()) == before
```

The formatter will rewrap these calls; keep each within 88 columns.

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_pipeline.py -q -k "matching_article or non_matching or edited_matched or first_seen or noop_run_writes"`
Expected: failures. There's no drift yet (the product stays a no-op), and no first-seen records exist.

- [ ] **Step 4: Implement in `pipeline.py`**

1. Imports from `kbforge.grounding`: add `delete_first_seen`, `load_first_seen`, `record_first_seen`, `resolve_all`. Remove `resolve` if it becomes unused (ruff will say).
2. The scan gate, where `scan = grounds and bool(...)`:

```python
    scan = grounds and bool(
        grounding_cfg.grounding
        or grounding_cfg.rules
        or any(d.grounded_by for d in docs)
        or has_sidecars(mirror_path)
    )
```

   Extend the comment above it with one line: rules make the scan unconditional, since a concept with no grounding yet must still be able to pick up its first matching document.

3. Right after `mirror_docs = load_all(mirror_path)`:

```python
    # Recency fallback for grounding rules; loaded once, only when rules exist.
    first_seen = load_first_seen(mirror_path) if grounding_cfg.rules else {}
```

4. In `_resolved`, replace the `resolve(...)` call with:

```python
            cached = resolve_all(doc, grounding_cfg, by_id, first_seen)
```

5. After `commit(mirror_path, docs)`:

```python
    record_first_seen(mirror_path, docs)
```

6. In the final tombstone loop:

```python
    for doc_id in changeset.removed:
        delete_sidecar(mirror_path, doc_id)
        delete_first_seen(mirror_path, doc_id)
```

- [ ] **Step 5: The CLI notice**

In `src/kbforge/__main__.py`, after the `problems_for(grounding_config)` check and before `run(...)`:

```python
    if grounding_config.rules and args.synthesizer == "stub":
        print(
            "grounding rules are validated but inactive: the stub synthesizer "
            "does not ground; use --synthesizer llm"
        )
```

Add a test to `tests/test_cli.py` that follows that file's existing pattern for invoking `main` with `--grounding`. It writes a grounding YAML with one valid rule, runs `kbforge run --connector local_files` on a tiny fixture dir with the default stub, and asserts this exact line is in the captured stdout. Read `tests/test_cli.py` first and reuse its fixture-directory helper rather than inventing one.

- [ ] **Step 6: Run to verify everything passes**

Run: `uv run pytest -q`
Expected: every test passes, including all pre-existing grounding and pipeline tests.

- [ ] **Step 7: Commit**

```bash
git add src/kbforge/pipeline.py src/kbforge/__main__.py tests/test_pipeline.py tests/test_cli.py
git commit -m "feat(pipeline): grounding rules drive drift; first-seen recorded on commit" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/architecture.md` §7.1, `CHANGELOG.md`, `docs/design/2026-09-18-grounding-rules-design.md`

- [ ] **Step 1: architecture.md §7.1**

Add a subsection at the end of §7.1, "**Grounding rules.**", of two or three paragraphs in the section's voice:
- The config shape, with the product and application example from the design note's §3.
- That rules are declared by the concept and evaluated in core over the mirror, for the reasons in the note's §2: the search-side and system-side alternatives both fail on one URL found by several selectors.
- Explicit grounding first; rules on top with their own `newest`.
- Recency: the `by` facet, else first-seen (`mirror/_first_seen/`).
- The drift sidecar does the staleness work unchanged.
- Link the design note for the rationale and deferrals. Don't restate the full semantics; CLAUDE.md says a doc drifts when it restates what the code owns.

- [ ] **Step 2: CHANGELOG.md**

Under `## [Unreleased]` → `### Added` (create the heading if absent), one entry:

> Grounding rules: `rules:` in the `--grounding` config ground existing concepts in matching documents from another system (`for` / `from` / `match` with `{field}` placeholders / `newest` / optional `by` date facet), ranked newest-first and capped. Explicit grounding is unchanged and outranks rules. Every rule-added citation is explained in the review summary. Takes effect with `--synthesizer llm`.

Under `### Known limits` (create it if absent):

> First-seen records start with this release: after upgrading, documents a source republishes all look equally new once, until the next genuinely new document arrives.

- [ ] **Step 3: The design note**

Change its frontmatter `status:` to `shipped — unreleased; folded into architecture.md §7.1; this note keeps the rationale and §9`.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest -q && uvx prek run --all-files`
Expected: all pass.

```bash
git add docs CHANGELOG.md
git commit -m "docs: grounding rules in architecture §7.1 and the changelog" -m "Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Verify the gates and the real thing

**Files:** none changed in the end; every mutation is restored.

- [ ] **Step 1: Clean tree**

Run: `git status --short`. Expected: empty.

- [ ] **Step 2: Mutation checks**

Apply each mutation in place, run the named test, confirm it fails **with the stated fragment**, and restore with `git checkout -- src/kbforge`.

| Mutation | Test that must fail | Fragment |
|---|---|---|
| `pipeline.py`: drop `or grounding_cfg.rules` from the scan gate | `test_a_new_matching_article_regrounds_the_product_on_its_own_run` | the product stays unpublished (`pipeline did not publish` / NoOp) |
| `grounding.py`: `(?<!\w)`/`(?!\w)` removed from the pattern | `test_matching_is_case_insensitive_and_on_word_boundaries` | `web:b` appears in the matches |
| `fill`: return `out` even when `missing` | `test_a_phrase_with_a_missing_or_blank_field_is_dropped` | a string instead of `None` |
| `resolve_all`: append rule docs **before** explicit ones | `test_explicit_grounding_comes_first_and_keeps_its_own_cap` | list order mismatch |
| `record_first_seen`: drop the "already recorded" check | `test_first_seen_is_written_once_and_kept` | `web:a` has `t2` |
| `rule_matches`: sort ascending instead of newest first | `test_newest_first_by_first_seen_then_doc_id_and_capped` | `['web:a', …]` |

Afterwards: `git status --short` (empty) and `uv run pytest -q` (all pass).

- [ ] **Step 3: Live run**

In a scratch directory:
1. Start a local PostgreSQL container with a `product` table containing `IMC300`, as in the kbforge-sql live test. Configure a `kbforge-sql` source `system=sql`, `type=product`.
2. Run the Firecrawl web source from `examples/web-source-mcp` as `system=web`, with a Watch list holding one real page that names a product in the table. If none of the scratch table's names appear on real pages, name a product after a real part the page mentions.
3. Use one shared `--mirror` and a grounding YAML with the product rule. Run in order: `sql` (publishes products), `web` (publishes the page), then `sql` again with `--synthesizer llm` (credentials from `.env`).
4. Expected: the product concept is re-synthesized, its `sources` cite the web page, and the review summary carries the `grounded by rule 1` note. A further `sql` run is a NoOp.

Record the commands and the relevant output in the PR description. If Docker or credits aren't available, say so explicitly rather than skipping silently.

- [ ] **Step 4: Full verification**

Run: `uv run pytest -q && uvx prek run --all-files && git diff main --stat`
Expected: tests and hooks pass. The diff touches only the files in this plan's file table.
