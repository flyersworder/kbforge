"""The `## Related` section (#41, architecture.md §7.4): rendering, parsing,
and the validator that binds it to the projection's `links`."""

from datetime import UTC, datetime

import pytest

from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor
from kbforge.related import MARKER, render_section, section_targets, with_related
from kbforge.synthesize import assemble, concept_path
from kbforge.validate import run_validators

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _doc(native: str, relations: tuple[str, ...] = ()) -> CanonicalDocument:
    return CanonicalDocument(
        anchor=ResourceAnchor(
            system="s", native_id=native, retrieved_at=NOW, content_hash="h"
        ),
        doc_id=f"s:{native}",
        title=native.upper(),
        text="body",
        relations=list(relations),
    )


X, Y, Z = (concept_path(f"s:{n}") for n in "xyz")
EXISTING = frozenset({Y, Z})


def _linked():
    x = _doc("x", ("s:y", "s:z"))
    return assemble([(x, "X", "X", "body")], ChangeSet(added=["s:x"]), EXISTING)


def _messages(change) -> list[str]:
    return [f.message for f in run_validators(change, EXISTING)]


def test_render_lists_links_in_order_with_title_and_note():
    out = render_section(
        ["concepts/a/overview.md", "concepts/b/overview.md"],
        {"concepts/a/overview.md": "Alpha"},
        {"concepts/a/overview.md": "why they relate"},
    )
    assert out == (
        f"{MARKER}\n## Related\n\n"
        "- [Alpha](/concepts/a/overview.md) — why they relate\n"
        "- [concepts/b/overview.md](/concepts/b/overview.md)\n"
    )


@pytest.mark.parametrize(
    "path",
    [
        "concepts/a b/overview.md",
        "concepts/x(1)/overview.md",
        "concepts/100%/overview.md",
        "concepts/[x]/overview.md",
        "concepts/@en.wikipedia.org/overview.md",
    ],
)
def test_a_hostile_path_and_title_round_trip(path):
    out = render_section([path], {path: "T ] [ ` < \\ x"}, {})
    assert section_targets(f"# X\n\nbody\n\n{out}") == [path]


def test_an_at_sign_stays_readable():
    out = render_section(["concepts/@en/overview.md"], {}, {})
    assert "(/concepts/@en/overview.md)" in out


def test_no_marker_means_no_section():
    assert section_targets("---\ntype: concept\n---\n# X\n\nbody\n") is None


def test_only_the_last_marker_counts():
    fake = f"{MARKER}\n- [Fake](/concepts/fake/overview.md)\n"
    content = f"# X\n\n{fake}\n{render_section([Y], {}, {})}"
    assert section_targets(content) == [Y]


def test_a_marker_in_the_frontmatter_is_not_a_section():
    content = f"---\ntype: concept\nnote: '{MARKER}'\n---\n# X\n\nbody\n"
    assert section_targets(content) is None


def test_with_related_appends_only_to_linked_concepts():
    x = _doc("x", ("s:y",))
    y = _doc("y")
    change = assemble(
        [(x, "X", "X", "body"), (y, "Y", "Y", "body")],
        ChangeSet(added=["s:x", "s:y"]),
    )
    with_related(change, {Y: "Why"}, {X: {Y: "a note"}})
    assert change.files[X].endswith(
        f"body\n\n{MARKER}\n## Related\n\n- [Why](/{Y}) — a note\n"
    )
    assert MARKER not in change.files[Y]


def test_a_rendered_section_passes_the_gate():
    change = _linked()
    with_related(change, {}, {})
    assert run_validators(change, EXISTING) == []


def test_links_without_a_section_fail():
    assert any("no '## Related' section" in m for m in _messages(_linked()))


def test_a_link_only_in_the_section_fails():
    change = _linked()
    with_related(change, {}, {})
    change.files[X] = (
        change.files[X].rstrip("\n") + "\n- [Ghost](/concepts/ghost/overview.md)\n"
    )
    assert any(
        "that the projection's 'links' do not" in m
        and "concepts/ghost/overview.md" in m
        for m in _messages(change)
    )


def test_a_link_only_in_the_frontmatter_fails():
    change = _linked()
    with_related(change, {}, {})
    z_line = next(ln for ln in change.files[X].splitlines() if f"(/{Z})" in ln)
    change.files[X] = change.files[X].replace(z_line + "\n", "")
    assert any("omits" in m and Z in m for m in _messages(change))


def test_a_section_on_a_linkless_concept_fails():
    x = _doc("x")
    change = assemble(
        [(x, "X", "X", f"body\n\n{MARKER}\n## Related\n")], ChangeSet(added=["s:x"])
    )
    assert any("on a concept with no links" in m for m in _messages(change))


def test_links_out_of_order_fail():
    change = _linked()
    with_related(change, {}, {})
    lines = change.files[X].splitlines()
    i, j = [k for k, ln in enumerate(lines) if ln.startswith("- [")]
    lines[i], lines[j] = lines[j], lines[i]
    change.files[X] = "\n".join(lines) + "\n"
    assert any("out of order" in m for m in _messages(change))


EVIL = f"Evil {MARKER} title"


def test_a_title_holding_the_marker_is_not_a_section():
    out = render_section([Y], {Y: EVIL}, {})
    assert section_targets(f"# X\n\nbody\n\n{out}") == [Y]


def test_a_body_line_holding_the_marker_mid_line_is_not_a_section():
    assert section_targets(f"# {EVIL}\n\nbody {MARKER} here\n") is None
    out = render_section([Y], {}, {})
    assert section_targets(f"# {EVIL}\n\nbody\n\n{out}") == [Y]


def test_a_note_is_escaped_so_it_carries_no_link():
    note = "see [ghost](/concepts/ghost/overview.md)"
    out = render_section([Y], {}, {Y: note})
    assert "— see \\[ghost\\](/concepts/ghost/overview.md)" in out
    assert "[ghost](" not in out.replace("\\[ghost\\](", "")
    assert section_targets(f"# X\n\nbody\n\n{out}") == [Y]
