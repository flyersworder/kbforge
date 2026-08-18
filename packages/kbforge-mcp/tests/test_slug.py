from __future__ import annotations

import pytest
from kbforge_mcp.slug import SlugError, native_id_for


def test_url_reduces_to_its_path_without_scheme_host_or_extension():
    raw = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html"
    assert native_id_for(raw) == "AmazonS3/latest/userguide/bucketnamingrules"


def test_query_and_fragment_are_dropped():
    raw = "https://example.com/docs/guide.html?v=2#section"
    assert native_id_for(raw) == "docs/guide"


def test_plain_path_is_kept_and_normalized():
    assert native_id_for("docs/handbook/onboarding.md") == "docs/handbook/onboarding"
    assert native_id_for("/leading/slash.md") == "leading/slash"


def test_traversal_is_refused_at_fetch_time_not_at_publish_time():
    # `safe_join` would raise PathError during publish -- after synthesis, after
    # tokens. Refuse here instead.
    with pytest.raises(SlugError, match="escapes the bundle"):
        native_id_for("../../.github/workflows/release.yml")


def test_empty_and_content_free_ids_are_refused():
    # doc_id="" passes assert_fetch_contract's uniqueness check and
    # concept_path("") renders concepts//overview.md, which normalizes onto a
    # root-level concept. Refuse before it can collide.
    for raw in ("", "   ", "/", "///"):
        with pytest.raises(SlugError):
            native_id_for(raw)


def test_only_a_short_alphanumeric_extension_is_stripped():
    assert (
        native_id_for("reports/2024.annual.summary.pdf")
        == "reports/2024.annual.summary"
    )
    assert native_id_for("api/v1.2/reference") == "api/v1.2/reference"
