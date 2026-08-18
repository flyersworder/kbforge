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


def test_ids_that_are_only_an_extension_are_refused():
    # Extension-stripping ".md" leaves nothing. Falling back to the untouched
    # segment (as an earlier version of this function did) would let it
    # through as native_id=".md", which collides with the empty-id case on
    # concepts//overview.md -- the same root-level-concept collision
    # test_empty_and_content_free_ids_are_refused exists to prevent, arriving
    # through a different door. Only the final segment is checked, so a
    # genuine dotfile earlier in the path (`docs/.gitignore/notes.md`) is
    # unaffected -- see test_dotfile_directory_segments_are_left_alone.
    for raw in (".md", "/.md", "./.md", "https://x.com/.md", "docs/.md"):
        with pytest.raises(SlugError, match="no content beyond its extension"):
            native_id_for(raw)


def test_dotfile_directory_segments_are_left_alone():
    # Only the final segment is extension-stripped; `.gitignore` here is a
    # directory-like segment, not the document name, so it is untouched.
    assert native_id_for("docs/.gitignore/notes.md") == "docs/.gitignore/notes"


def test_percent_encoded_traversal_is_refused():
    # Neither git nor the filesystem decodes percent-encoding today, so a
    # literal "%2e%2e" segment isn't currently exploitable -- it would just
    # produce an oddly named directory rather than an escape. But this
    # function is the one thing standing between a server-controlled id and
    # a path escape, so it refuses the decoded form too. The native_id itself
    # is never built from the decoded segments (see native_id_for's
    # docstring/comment): only the literal "%2e%2e" would ever be returned,
    # and here it's refused before that can happen.
    with pytest.raises(SlugError, match="escapes the bundle"):
        native_id_for("%2e%2e/%2e%2e/etc/passwd")
