from datetime import UTC, datetime

from kbforge.models import (
    ChangeSummary,
    ConceptFrontmatter,
    ProposedChange,
    ResourceAnchor,
)

NOW = datetime(2026, 7, 18, tzinfo=UTC)


def test_concept_frontmatter_defaults_are_permissive():
    c = ConceptFrontmatter()
    assert c.type == ""
    assert c.facets == {}
    assert c.resources == []
    assert c.links == []
    assert c.freshness is None


def test_proposed_change_holds_files_and_concepts():
    anchor = ResourceAnchor(
        system="confluence",
        native_id="123",
        retrieved_at=NOW,
        content_hash="abc",
    )
    concept = ConceptFrontmatter(
        type="application",
        facets={"owner": "team-a"},
        resources=[anchor],
        freshness=NOW,
    )
    change = ProposedChange(
        branch_hint="sync/app-x",
        files={"apps/x/overview.md": "# X"},
        concepts={"apps/x/overview.md": concept},
    )
    assert change.concepts["apps/x/overview.md"].facets["owner"] == "team-a"
    assert change.concepts["apps/x/overview.md"].resources[0].native_id == "123"
    assert isinstance(change.summary, ChangeSummary)
