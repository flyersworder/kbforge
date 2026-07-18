from datetime import UTC, datetime

from kbforge.models import ConceptFrontmatter, ProposedChange, ResourceAnchor
from kbforge.validate import run_artifact_validators

NOW = datetime(2026, 7, 18, tzinfo=UTC)
ANCHOR = ResourceAnchor(
    system="confluence", native_id="123", retrieved_at=NOW, content_hash="abc"
)


def _proposal(concept, path="apps/x/overview.md"):
    return ProposedChange(
        branch_hint="b", files={path: "..."}, concepts={path: concept}
    )


def test_missing_anchor_is_reported():
    c = ConceptFrontmatter(type="application", freshness=NOW)  # no resources
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "anchor-presence" for f in failures)


def test_missing_freshness_is_reported():
    c = ConceptFrontmatter(type="application", resources=[ANCHOR])  # freshness None
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "freshness-legibility" for f in failures)


def test_empty_type_is_reported():
    c = ConceptFrontmatter(type="", resources=[ANCHOR], freshness=NOW)
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "okf-type" for f in failures)


def test_conformant_concept_passes_per_concept_checks():
    c = ConceptFrontmatter(type="application", resources=[ANCHOR], freshness=NOW)
    assert run_artifact_validators(_proposal(c)) == []


def test_empty_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application", facets={"owner": ""}, resources=[ANCHOR], freshness=NOW
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-survival" for f in failures)


def test_nested_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": {"team": "a"}},
        resources=[ANCHOR],
        freshness=NOW,
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-survival" for f in failures)


def test_scalar_and_flat_list_facets_pass():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a", "tags": ["prod", "db"], "replicas": 3},
        resources=[ANCHOR],
        freshness=NOW,
    )
    facet_failures = [
        f for f in run_artifact_validators(_proposal(c)) if f.law == "facet-survival"
    ]
    assert facet_failures == []


def test_dangling_link_is_reported():
    c = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/y/overview.md"],  # y not in the bundle
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "link-resolvability" for f in failures)


def test_link_to_sibling_in_same_change_resolves():
    x = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/y/overview.md"],
    )
    y = ConceptFrontmatter(type="application", resources=[ANCHOR], freshness=NOW)
    change = ProposedChange(
        branch_hint="b",
        files={"apps/x/overview.md": "...", "apps/y/overview.md": "..."},
        concepts={"apps/x/overview.md": x, "apps/y/overview.md": y},
    )
    link_failures = [
        f for f in run_artifact_validators(change) if f.law == "link-resolvability"
    ]
    assert link_failures == []


def test_link_to_existing_bundle_path_resolves():
    c = ConceptFrontmatter(
        type="application",
        resources=[ANCHOR],
        freshness=NOW,
        links=["apps/z/overview.md"],
    )
    link_failures = [
        f
        for f in run_artifact_validators(
            _proposal(c), existing_paths=frozenset({"apps/z/overview.md"})
        )
        if f.law == "link-resolvability"
    ]
    assert link_failures == []


def _conformant_change():
    concept = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a", "criticality": "high"},
        resources=[ANCHOR],
        links=["apps/y/overview.md"],
        freshness=NOW,
    )
    sibling = ConceptFrontmatter(type="application", resources=[ANCHOR], freshness=NOW)
    return ProposedChange(
        branch_hint="sync/app-x",
        files={"apps/x/overview.md": "# X", "apps/y/overview.md": "# Y"},
        concepts={"apps/x/overview.md": concept, "apps/y/overview.md": sibling},
    )


def test_agent_facing_artifact_conformance():
    # §9 conformance capstone: a conformant bundle passes all four laws.
    assert run_artifact_validators(_conformant_change()) == []


def test_each_law_catches_its_own_violation():
    # One targeted break per law, asserting the specific law fires.
    base = _conformant_change()

    no_anchor = base.model_copy(deep=True)
    no_anchor.concepts["apps/x/overview.md"].resources = []
    assert any(f.law == "anchor-presence" for f in run_artifact_validators(no_anchor))

    no_freshness = base.model_copy(deep=True)
    no_freshness.concepts["apps/x/overview.md"].freshness = None
    assert any(
        f.law == "freshness-legibility" for f in run_artifact_validators(no_freshness)
    )

    bad_facet = base.model_copy(deep=True)
    bad_facet.concepts["apps/x/overview.md"].facets = {"owner": ""}
    assert any(f.law == "facet-survival" for f in run_artifact_validators(bad_facet))

    dangling = base.model_copy(deep=True)
    dangling.concepts["apps/x/overview.md"].links = ["apps/ghost/overview.md"]
    assert any(f.law == "link-resolvability" for f in run_artifact_validators(dangling))


def test_empty_proposal_has_no_failures():
    assert run_artifact_validators(ProposedChange(branch_hint="b")) == []
