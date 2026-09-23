"""`okfquery related`: links, backlinks, and shared-facet neighbours of one
concept, derived at query time (OKF §3.1) rather than stored in the bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from okfquery.cli import main
from okfquery.related import render_related


def _concept(bundle: Path, slug: str, front: str) -> str:
    path = bundle / "concepts" / slug / "overview.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\nBody of {slug}.\n", "utf-8")
    return f"concepts/{slug}/overview.md"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    _concept(
        tmp_path,
        "gap",
        "type: gap\ntitle: Redundant supply\n"
        "description: A missing backup rail.\ntags: [800v, sic]\n"
        # `gone` is a dangling target; the leading `/` is OKF §6.1's
        # bundle-absolute form, which must name the same concept.
        "links: [/concepts/req/overview.md, concepts/gone/overview.md]\n",
    )
    _concept(
        tmp_path,
        "req",
        "type: requirement\ntitle: Fail-mode requirement\ntags: [800v]\n",
    )
    _concept(
        tmp_path,
        "deck",
        "type: deck\ntitle: Roadmap deck\ndescription: The plan.\n"
        # A bundle-absolute backlink must still count as linking to `gap`.
        "links: [/concepts/gap/overview.md]\ntags: [800v]\n",
    )
    _concept(
        tmp_path,
        "both",
        "type: report\ntitle: Q3 report\ndescription: Both tags.\n"
        "tags: [sic, 800v]\nscenario: ev\n",
    )
    _concept(
        tmp_path, "one", "type: report\ntitle: Q2 report\ntags: [sic]\nscenario: ev\n"
    )
    _concept(tmp_path, "none", "type: report\ntitle: Untagged\n")
    return tmp_path


GAP = "concepts/gap/overview.md"


def test_three_sections_in_index_line_format(bundle):
    assert render_related(bundle, GAP) == (
        "# Links to\n"
        "\n"
        "* [concepts/gone/overview.md](concepts/gone/overview.md)\n"
        "* [Fail-mode requirement](concepts/req/overview.md)\n"
        "\n"
        "# Linked from\n"
        "\n"
        "* [Roadmap deck](concepts/deck/overview.md) - The plan.\n"
        "\n"
        "# Shares tags\n"
        "\n"
        "* [Q3 report](concepts/both/overview.md) - Both tags."
        " (shares tags: 800v, sic)\n"
        "* [Q2 report](concepts/one/overview.md) (shares tags: sic)\n"
    )


def test_already_linked_concepts_are_not_repeated_under_shares(bundle):
    # `req` and `deck` share 800v with `gap`, but a reader already sees them.
    shares = render_related(bundle, GAP).split("# Shares tags", 1)[1]
    assert "concepts/req/" not in shares
    assert "concepts/deck/" not in shares


def test_ranked_by_shared_value_count_then_path(bundle):
    text = render_related(bundle, "concepts/one/overview.md", by=["tags", "scenario"])
    shares = text.split("# Shares tags, scenario\n\n", 1)[1].splitlines()
    assert shares == [
        "* [Q3 report](concepts/both/overview.md) - Both tags."
        " (shares tags: sic; scenario: ev)",
        "* [Redundant supply](concepts/gap/overview.md) - A missing backup rail."
        " (shares tags: sic)",
    ]


def test_more_shared_values_outrank_an_earlier_path(tmp_path):
    a = _concept(tmp_path, "a", "type: x\ntags: [p, q]\n")
    _concept(tmp_path, "b-one", "type: x\ntitle: One\ntags: [p]\n")
    _concept(tmp_path, "z-two", "type: x\ntitle: Two\ntags: [p, q]\n")
    shares = render_related(tmp_path, a).split("# Shares tags\n\n", 1)[1]
    assert shares.splitlines() == [
        "* [Two](concepts/z-two/overview.md) (shares tags: p, q)",
        "* [One](concepts/b-one/overview.md) (shares tags: p)",
    ]


def test_limit_applies_to_shares_only(bundle):
    text = render_related(bundle, GAP, limit=1)
    assert "concepts/both/" in text
    assert "concepts/one/" not in text
    assert "concepts/gone/" in text and "concepts/deck/" in text


def test_empty_sections_say_none(bundle):
    assert render_related(bundle, "concepts/none/overview.md") == (
        "# Links to\n\n(none)\n\n# Linked from\n\n(none)\n\n# Shares tags\n\n(none)\n"
    )


def test_leading_slash_or_dot_slash_is_accepted(bundle):
    expected = render_related(bundle, GAP)
    assert render_related(bundle, "/" + GAP) == expected
    assert render_related(bundle, "./" + GAP) == expected


def test_unknown_path_is_an_error(bundle):
    with pytest.raises(ValueError, match="concepts/nope/overview.md is not a concept"):
        render_related(bundle, "concepts/nope/overview.md")


def test_an_okf_owned_key_is_not_a_facet(bundle):
    with pytest.raises(ValueError, match="'type' is not a facet: OKF owns it"):
        render_related(bundle, GAP, by=["type"])


def test_cli_prints_and_exits_zero(bundle, capsys):
    code = main(["related", GAP, "--bundle", str(bundle), "--by", "tags,scenario"])
    assert code == 0
    assert "# Shares tags, scenario" in capsys.readouterr().out


def test_cli_unknown_path_is_a_message_not_a_traceback(bundle, capsys):
    code = main(["related", "concepts/nope/overview.md", "--bundle", str(bundle)])
    assert code == 2
    assert "concepts/nope/overview.md is not a concept" in capsys.readouterr().err


def test_cli_missing_bundle(tmp_path, capsys):
    missing = tmp_path / "nonexistent"
    code = main(["related", GAP, "--bundle", str(missing)])
    assert code == 2
    assert str(missing) in capsys.readouterr().err
