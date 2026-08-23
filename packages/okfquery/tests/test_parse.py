from __future__ import annotations

from datetime import UTC, datetime

import pytest

from okfquery.parse import Problem, is_reserved, parse

GOOD = """---
type: concept
title: Bucket naming
description: How buckets are named.
generated:
  by: kbforge/0.8.0
  at: 2026-08-23T09:00:00+00:00
owner: platform
sources:
  - id: wiki:naming
    resource: https://wiki/naming
    content_hash: abc123
  - id: notes:naming
    resource: notes:naming
    content_hash: def456
links:
  - concepts/other/overview.md
---

# Bucket naming

Body text.
"""


def kinds(content: str) -> list[str]:
    return [p.kind for p in parse(content).problems]


def detail_for(content: str, kind: str) -> str:
    return next(p.detail for p in parse(content).problems if p.kind == kind)


def test_good_concept_has_no_problems():
    assert parse(GOOD).problems == []


def test_good_concept_fields():
    c = parse(GOOD)
    assert c.type == "concept"
    assert c.title == "Bucket naming"
    assert c.description == "How buckets are named."
    assert c.generated_by == "kbforge/0.8.0"
    assert c.generated_at == datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    assert c.body.startswith("# Bucket naming")


def test_facets_exclude_okf_owned_keys():
    # `owner` is a facet; `type`/`title`/`sources`/`links`/`generated` are not.
    assert parse(GOOD).facets == {"owner": "platform"}


def test_sources_keep_order_so_ordinal_survives():
    ids = [s["id"] for s in parse(GOOD).sources]
    assert ids == ["wiki:naming", "notes:naming"]


def test_no_frontmatter():
    assert kinds("# Just a heading\n") == ["no-frontmatter"]


def test_unterminated_frontmatter_is_distinct_from_invalid_yaml():
    # These two are the pair most likely to be conflated: both leave the parser
    # with nothing usable, and kbforge's own _parse_frontmatter collapses them.
    unterminated = detail_for("---\ntype: concept\n", "unterminated-frontmatter")
    invalid = detail_for("---\ntype: [unclosed\n---\n\nbody\n", "invalid-yaml")
    assert "closing '---'" in unterminated
    assert "YAML" in invalid
    assert unterminated != invalid


def test_frontmatter_not_mapping():
    assert "list" in detail_for(
        "---\n- a\n- b\n---\n\nbody\n", "frontmatter-not-mapping"
    )


def test_missing_required_names_every_absent_key():
    detail = detail_for("---\ntype: concept\n---\n\nbody\n", "missing-required")
    for key in ("title", "description", "generated", "sources"):
        assert key in detail
    assert "type" not in detail


def test_bad_generated_when_not_a_mapping():
    content = (
        "---\ntype: c\ntitle: t\ndescription: d\n"
        "generated: nope\nsources: []\n---\n\nb\n"
    )
    assert "mapping" in detail_for(content, "bad-generated")


def test_bad_timestamp_when_unparseable():
    content = (
        "---\ntype: c\ntitle: t\ndescription: d\n"
        "generated:\n  by: x/1\n  at: not-a-date\nsources: []\n---\n\nb\n"
    )
    assert "not-a-date" in detail_for(content, "bad-timestamp")
    assert parse(content).generated_at is None


def test_naive_timestamp_is_kept_but_flagged():
    content = (
        "---\ntype: c\ntitle: t\ndescription: d\n"
        "generated:\n  by: x/1\n  at: 2026-08-23T09:00:00\nsources: []\n---\n\nb\n"
    )
    c = parse(content)
    assert "naive-timestamp" in [p.kind for p in c.problems]
    # Kept and read as UTC so staleness queries degrade rather than vanish.
    assert c.generated_at == datetime(2026, 8, 23, 9, 0, tzinfo=UTC)


def test_bad_sources_names_the_ordinal():
    content = (
        "---\ntype: c\ntitle: t\ndescription: d\ngenerated:\n  by: x/1\n"
        "  at: 2026-08-23T09:00:00+00:00\nsources:\n  - id: a\n---\n\nb\n"
    )
    detail = detail_for(content, "bad-sources")
    assert "0" in detail and "resource" in detail
    # Additive: the entry is still carried, with a null resource.
    assert parse(content).sources[0]["resource"] is None


def test_bad_links():
    content = (
        "---\ntype: c\ntitle: t\ndescription: d\ngenerated:\n  by: x/1\n"
        "  at: 2026-08-23T09:00:00+00:00\nsources: []\nlinks:\n  - 3\n---\n\nb\n"
    )
    assert "3" in detail_for(content, "bad-links")
    assert parse(content).links == []


@pytest.mark.parametrize("name", ["index.md", "log.md"])
def test_reserved_without_fence_is_skipped(name):
    assert is_reserved(name, "# Directory listing\n") is True


@pytest.mark.parametrize("name", ["index.md", "log.md"])
def test_reserved_with_fence_is_a_concept(name):
    # The rule kbforge's validate._check_strict_okf applies: a reserved NAME
    # bearing a frontmatter fence is claiming to be a concept, and is one.
    assert is_reserved(name, "---\ntype: concept\n---\n\nb\n") is False


def test_non_reserved_name_is_never_skipped():
    assert is_reserved("overview.md", "# no fence here\n") is False


def test_problem_is_hashable_so_tests_can_use_sets():
    assert {Problem("a", "b")} == {Problem("a", "b")}
