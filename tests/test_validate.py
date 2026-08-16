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
    c = ConceptFrontmatter(type="application", generated_at=NOW)  # no sources
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "anchor-presence" for f in failures)


def test_missing_freshness_is_reported():
    c = ConceptFrontmatter(type="application", sources=[ANCHOR])  # generated_at None
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "freshness-legibility" for f in failures)


def test_empty_type_is_reported():
    c = ConceptFrontmatter(type="", sources=[ANCHOR], generated_at=NOW)
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "okf-type" for f in failures)


def test_conformant_concept_passes_per_concept_checks():
    c = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
    assert run_artifact_validators(_proposal(c)) == []


def test_empty_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application", facets={"owner": ""}, sources=[ANCHOR], generated_at=NOW
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-wellformedness" for f in failures)


def test_nested_facet_value_is_reported():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": {"team": "a"}},
        sources=[ANCHOR],
        generated_at=NOW,
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "facet-wellformedness" for f in failures)


def test_scalar_and_flat_list_facets_pass():
    c = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a", "tags": ["prod", "db"], "replicas": 3},
        sources=[ANCHOR],
        generated_at=NOW,
    )
    failures = run_artifact_validators(_proposal(c))
    facet_failures = [f for f in failures if f.law == "facet-wellformedness"]
    assert facet_failures == []


def test_dangling_link_is_reported():
    c = ConceptFrontmatter(
        type="application",
        sources=[ANCHOR],
        generated_at=NOW,
        links=["apps/y/overview.md"],  # y not in the bundle
    )
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "link-resolvability" for f in failures)


def test_link_to_sibling_in_same_change_resolves():
    x = ConceptFrontmatter(
        type="application",
        sources=[ANCHOR],
        generated_at=NOW,
        links=["apps/y/overview.md"],
    )
    y = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
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
        sources=[ANCHOR],
        generated_at=NOW,
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
        sources=[ANCHOR],
        links=["apps/y/overview.md"],
        generated_at=NOW,
    )
    sibling = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
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
    no_anchor.concepts["apps/x/overview.md"].sources = []
    assert any(f.law == "anchor-presence" for f in run_artifact_validators(no_anchor))

    no_freshness = base.model_copy(deep=True)
    no_freshness.concepts["apps/x/overview.md"].generated_at = None
    assert any(
        f.law == "freshness-legibility" for f in run_artifact_validators(no_freshness)
    )

    bad_facet = base.model_copy(deep=True)
    bad_facet.concepts["apps/x/overview.md"].facets = {"owner": ""}
    assert any(
        f.law == "facet-wellformedness" for f in run_artifact_validators(bad_facet)
    )

    dangling = base.model_copy(deep=True)
    dangling.concepts["apps/x/overview.md"].links = ["apps/ghost/overview.md"]
    assert any(f.law == "link-resolvability" for f in run_artifact_validators(dangling))


def test_empty_proposal_has_no_failures():
    assert run_artifact_validators(ProposedChange(branch_hint="b")) == []


def test_file_without_projection_is_reported():
    # A rendered concept file with no ConceptFrontmatter would ship unvalidated —
    # the gate must catch the omission, not greenlight it (red-team finding #1).
    change = ProposedChange(
        branch_hint="b",
        files={"apps/x/overview.md": "...unvalidated garbage..."},
        concepts={},
    )
    failures = run_artifact_validators(change)
    assert any(f.law == "projection-coherence" for f in failures)


def test_projection_without_file_is_reported():
    c = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
    change = ProposedChange(
        branch_hint="b", files={}, concepts={"apps/x/overview.md": c}
    )
    failures = run_artifact_validators(change)
    assert any(f.law == "projection-coherence" for f in failures)


def test_reserved_files_need_no_projection():
    # index.md (listing) and log.md (history) carry no frontmatter — exempt.
    c = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
    change = ProposedChange(
        branch_hint="b",
        files={
            "apps/x/overview.md": "# X",
            "apps/index.md": "listing",
            "apps/x/log.md": "history",
        },
        concepts={"apps/x/overview.md": c},
    )
    coherence = [
        f for f in run_artifact_validators(change) if f.law == "projection-coherence"
    ]
    assert coherence == []


def test_naive_freshness_is_reported():
    # A timezone-naive stamp is "present" but crashes whats_stale's
    # aware-minus-naive subtraction — law 4 must reject it (red-team finding #4).
    naive = datetime(2026, 7, 18)  # deliberately naive (no tzinfo)
    c = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=naive)
    failures = run_artifact_validators(_proposal(c))
    assert any(f.law == "freshness-legibility" for f in failures)


def test_a_path_both_written_and_removed_is_a_failure():
    """A proposal that adds and deletes one path is self-contradictory, and
    nothing caught it: _check_projection_coherence bound files<->concepts and
    never inspected files_removed, so it returned [] on this proposal."""
    path = "apps/x/overview.md"
    concept = ConceptFrontmatter(type="application", sources=[ANCHOR], generated_at=NOW)
    change = ProposedChange(
        branch_hint="b",
        files={path: "# X"},
        files_removed=[path],
        concepts={path: concept},
    )
    failures = run_artifact_validators(change)
    coherence = [f for f in failures if f.law == "projection-coherence"]
    assert len(coherence) == 1
    assert coherence[0].concept_path == path
    assert (
        coherence[0].message
        == "path is both written and removed in one proposal (§4.4 gate)"
    )
