from datetime import UTC, datetime

import pytest
import yaml

from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor
from kbforge.synthesize import assemble, concept_path, synthesize

NOW = datetime(2026, 7, 19, tzinfo=UTC)


def _frontmatter(rendered: str) -> dict:
    """Parse the YAML head of a rendered concept, so the emit-side assertions
    check what actually ships rather than the projection that fed it. Splits the
    way `validate._parse_frontmatter` does — on the closing "\\n---" rather than
    on every "---" — so a title or body containing a rule cannot mis-parse."""
    _, _, rest = rendered.partition("---")
    front, _, _ = rest.partition("\n---")
    return yaml.safe_load(front)


def _doc(doc_id, structured=None, relations=None):
    native = doc_id.split(":", 1)[1]
    anchor = ResourceAnchor(
        system="local_files", native_id=native, retrieved_at=NOW, content_hash="h"
    )
    return CanonicalDocument(
        anchor=anchor,
        doc_id=doc_id,
        title=native,
        text="body",
        structured=structured or {},
        relations=relations or [],
    )


@pytest.fixture
def _one_changed():
    doc = _doc("local_files:apps/x.md", structured={"owner": "team-a"})
    changeset = ChangeSet(added=["local_files:apps/x.md"])
    existing = frozenset()
    return [doc], changeset, existing


def test_synthesizes_a_conformant_concept():
    doc = _doc("local_files:apps/x.md", structured={"owner": "team-a"})
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    path = concept_path("local_files:apps/x.md")
    assert path in change.files and path in change.concepts
    fm = change.concepts[path]
    assert fm.type == "concept"
    assert fm.facets == {"owner": "team-a"}
    assert fm.sources == [doc.anchor]
    assert fm.generated_at == NOW
    assert fm.links == []  # no relations declared → no links
    assert change.files[path].startswith("---\n")  # rendered with YAML frontmatter
    # full strict-OKF + §4.4 conformance of synthesized output is proven end-to-end
    # by the pipeline test (Task 8): a Published result means run_validators == [].


def test_rendered_frontmatter_uses_okf_02_sources():
    """OKF §5.1: provenance lives in `sources`, each entry carrying a REQUIRED
    `resource`. The bare `resource` key is a singular URI in both v0.1 and v0.2
    and must not be a list."""
    doc = _doc("local_files:apps/x.md")
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    front = _frontmatter(change.files[concept_path("local_files:apps/x.md")])

    assert "resource" not in front
    assert front["sources"] == [
        {
            "id": "local_files:apps/x.md",
            "resource": "local_files:apps/x.md",
            "content_hash": "h",
        }
    ]


def test_source_entry_prefers_a_real_url_when_the_anchor_has_one():
    """A followable URL is the better `resource`; the scope descriptor is only the
    fallback OKF §5.1 permits when no artifact URL exists."""
    doc = _doc("local_files:apps/x.md")
    doc.anchor.url = "https://wiki.acme/x"
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    front = _frontmatter(change.files[concept_path("local_files:apps/x.md")])

    assert front["sources"][0]["resource"] == "https://wiki.acme/x"


def test_rendered_frontmatter_uses_okf_02_generated():
    """§13.1: `timestamp` is superseded by `generated: {by, at}`."""
    from kbforge import __version__

    doc = _doc("local_files:apps/x.md")
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    front = _frontmatter(change.files[concept_path("local_files:apps/x.md")])

    assert "timestamp" not in front
    assert front["generated"] == {
        "by": f"kbforge/{__version__}",
        "at": NOW.isoformat(),
    }


def test_generated_by_is_overridable_for_llm_synthesis():
    """OKF §7 actor convention: the version slot carries the model, following the
    spec's own `reference_agent/gemini-2.5-pro` example."""
    doc = _doc("local_files:apps/x.md")
    change = assemble(
        [(doc, "T", "D", "body")],
        ChangeSet(added=["local_files:apps/x.md"]),
        generated_by="kbforge/deepseek-v4-flash",
    )
    fm = change.concepts[concept_path("local_files:apps/x.md")]

    assert fm.generated_by == "kbforge/deepseek-v4-flash"


def test_llm_actor_is_two_segment_for_a_provider_qualified_model():
    """§7 fixes `<producer>/<version>`. The default model id is itself
    provider-qualified, so interpolating it whole would emit a three-segment
    actor a consumer reads as producer "kbforge/deepseek". Lives here rather
    than in test_llm_synthesizer.py, which is importorskip-guarded on
    pydantic_ai — this needs no LLM and must always run."""
    from kbforge.llm_synthesizer import LLMConfig, actor_for

    assert actor_for("deepseek/deepseek-v4-flash") == "kbforge/deepseek-v4-flash"
    assert actor_for("gpt-5") == "kbforge/gpt-5"
    assert actor_for(LLMConfig().model).count("/") == 1  # the shipped default


def test_dangling_relations_are_dropped():
    doc = _doc("local_files:apps/x.md", relations=["local_files:apps/ghost.md"])
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == []  # ghost not in the bundle → dropped, not dangling


def test_resolvable_sibling_link_survives():
    x = _doc("local_files:apps/x.md", relations=["local_files:apps/y.md"])
    y = _doc("local_files:apps/y.md")
    change = synthesize(
        [x, y], ChangeSet(added=["local_files:apps/x.md", "local_files:apps/y.md"])
    )
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.links == [concept_path("local_files:apps/y.md")]


def test_nested_structured_value_is_not_a_facet():
    doc = _doc(
        "local_files:apps/x.md", structured={"owner": {"team": "a"}, "env": "prod"}
    )
    change = synthesize([doc], ChangeSet(added=["local_files:apps/x.md"]))
    fm = change.concepts[concept_path("local_files:apps/x.md")]
    assert fm.facets == {"env": "prod"}  # nested dropped → law 1 stays well-formed


def test_stub_synthesizer_matches_module_function(_one_changed):
    from kbforge.synthesize import StubSynthesizer

    docs, changeset, existing = _one_changed
    a = synthesize(docs, changeset, existing)
    b = StubSynthesizer().synthesize(docs, changeset, existing)
    assert a.model_dump() == b.model_dump()  # identical behavior


def test_proposed_change_defaults_to_no_removals():
    change = assemble(
        [], ChangeSet(added=[], modified=[], removed=[], unchanged_count=0)
    )

    assert change.files_removed == []


def test_claims_removed_are_bundle_paths_like_added_and_modified():
    """The review body must not mix doc_ids with paths."""
    changeset = ChangeSet(
        added=[], modified=[], removed=["sys:gone"], unchanged_count=0
    )

    change = assemble([], changeset)

    assert change.summary.claims_removed == ["concepts/gone/overview.md"]


def test_branch_hint_survives_a_deletion_only_run():
    """No items means no doc to read the system from; the removed doc_ids carry
    it. Falling back to 'source' would publish to a different branch and open a
    second review request."""
    changeset = ChangeSet(
        added=[], modified=[], removed=["local_files:gone.md"], unchanged_count=0
    )

    change = assemble([], changeset)

    assert change.branch_hint == "sync/local_files"
