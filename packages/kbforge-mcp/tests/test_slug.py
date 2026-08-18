from __future__ import annotations

import pytest

from kbforge.publishers.forge import safe_join
from kbforge.synthesize import concept_path
from kbforge_mcp.slug import SlugError, is_url, native_id_for


def test_a_url_keeps_its_host_and_loses_only_its_scheme_and_extension():
    # The host is identity: `docs/x.html` on two hosts is two documents, and
    # building the slug from `parts.path` alone merged them. `@` marks the
    # host segment and is escaped everywhere else, which keeps URL identities
    # disjoint from plain-path ones.
    raw = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules.html"
    assert (
        native_id_for(raw)
        == "@docs.aws.amazon.com/AmazonS3/latest/userguide/bucketnamingrules"
    )


def test_the_same_path_on_two_hosts_is_two_documents():
    assert native_id_for("https://a.com/docs/x.html") != native_id_for(
        "https://b.com/docs/x.html"
    )


def test_query_and_fragment_are_identity_carried_on_the_final_segment():
    # Dropping them merged `page?v=1` with `page?v=2`. `?` is escaped because
    # it is illegal in a Windows filename; `#` is legal on every platform.
    raw = "https://example.com/docs/guide.html?v=2#section"
    assert native_id_for(raw) == "@example.com/docs/guide%3Fv=2#section"
    assert native_id_for("https://example.com/p?v=1") != native_id_for(
        "https://example.com/p?v=2"
    )


def test_a_slash_inside_a_query_or_fragment_is_text_not_structure():
    # The tail is spliced onto the final path segment, so `/` has to be escaped
    # like every other character a segment cannot carry. Spliced in raw it was
    # STRUCTURE: it injected real path separators into the identity, with three
    # consequences this pins one by one.
    #
    # 1. `?x=/y`, `?x=//y` and `?x=/./y` stayed three distinct native_ids and
    #    three distinct doc_ids, and then normalized onto ONE published file --
    #    see test_distinct_ids_never_reduce_to_one_published_file, which is
    #    where that class is caught. The tail stays inside one segment now.
    assert native_id_for("https://a.com/p?x=/y") == "@a.com/p%3Fx=%2Fy"
    assert len(native_id_for("https://a.com/p?x=/y").split("/")) == 2
    # 2. The `..` refusal runs on segments split from the PATH, before the tail
    #    exists, so a traversal hidden in a tail walked straight past it and
    #    reached `safe_join` at publish time -- after synthesis, after tokens,
    #    which is the exact failure this module's docstring says it prevents.
    escaped = native_id_for("https://a.com/p?x=/../../secrets")
    assert ".." not in escaped.split("/")
    assert len(escaped.split("/")) == 2
    # 3. `_escape` checks only the FINAL character of a segment, so a tail `/`
    #    put every earlier piece out of the trailing-dot rule's reach and left a
    #    `p%3Fx=v.` directory -- a name Windows silently trims.
    assert native_id_for("https://a.com/p?x=v./y") == "@a.com/p%3Fx=v.%2Fy"


def test_a_slug_never_ends_in_md_for_concept_path_to_strip_again():
    # `concept_path` runs `native.removesuffix(".md")` on the whole native_id,
    # one stage after this module's own extension rule and outside its reach.
    # Without this, `docs/a.md.md` -> `docs/a.md` and `docs/a.md` -> `docs/a`
    # are two distinct doc_ids that publish to one file, silently.
    assert native_id_for("docs/a.md.md") == "docs/a%2Emd"
    assert native_id_for("docs/a.md") == "docs/a"
    # A query tail can produce the suffix too.
    assert native_id_for("https://a.com/p?x=a.md") == "@a.com/p%3Fx=a%2Emd"


def test_only_the_host_is_case_folded_not_the_whole_authority():
    # RFC 3986 §6.2.2 makes scheme and host case-insensitive. Userinfo is not,
    # so `netloc.lower()` merged two distinct authorities.
    assert native_id_for("https://A.COM/p") == native_id_for("https://a.com/p")
    assert native_id_for("https://USER:PASS@a.com/p") != native_id_for(
        "https://user:pass@a.com/p"
    )


def test_a_namespaced_non_url_id_keeps_its_namespace():
    # `urlsplit` reads any leading `word:` as a scheme, so `SPACE:page.md`
    # used to reduce to `page` and every space's copy of a page collided.
    # A URL needs an authority as well as a scheme; this has none, so the
    # whole string is the path. `:` is escaped rather than remapped: `:` -> `/`
    # would collide with a literal `SPACE/page.md` and `:` -> `-` with
    # `SPACE-page.md`.
    assert native_id_for("SPACE:page.md") == "SPACE%3Apage"
    assert native_id_for("ARCHIVE:page.md") == "ARCHIVE%3Apage"
    assert not is_url("SPACE:page.md")
    assert is_url("https://example.com/x")


def test_characters_a_git_checkout_cannot_carry_are_escaped_not_dropped():
    # `<>:"|?*\` and the control characters are illegal in an NTFS filename, so
    # a bundle containing one could not be checked out on Windows at all. They
    # are percent-escaped, and `%` is escaped too -- that is what keeps the
    # escape injective, since otherwise a literal `%3A` would be
    # indistinguishable from an escaped `:`.
    assert native_id_for('docs/a<b>c"d|e*f.md') == "docs/a%3Cb%3Ec%22d%7Ce%2Af"
    assert native_id_for("docs/a%3Ab.md") == "docs/a%253Ab"
    assert native_id_for("docs/a%3Ab.md") != native_id_for("docs/a:b.md")
    # Windows also refuses a name ending in a dot or a space, and trims both
    # silently on some paths -- which would merge two distinct segments.
    # (Whitespace around the id as a whole is stripped before any of this: that
    # is the fourth deliberate equivalence, and it is why the space case has to
    # be tested on a segment that is not the last one.)
    assert native_id_for("docs/trailing.") == "docs/trailing%2E"
    assert native_id_for("docs/sub /file.md") == "docs/sub%20/file"


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


def test_only_a_known_document_extension_is_stripped():
    # The rule this replaced was `\.[A-Za-z0-9]{1,5}\Z` over the FINAL segment,
    # which ate any short alphanumeric tail -- a trailing version or date
    # included. The old test asserted `api/v1.2/reference` keeps its version and
    # read as "versions survive", but that version is not the last segment and
    # so was the one place the regex never touched. Pin the version and the date
    # where they actually are: at the end.
    assert native_id_for("docs/spec-1.2") == "docs/spec-1.2"
    assert native_id_for("api/reference/v1.2") == "api/reference/v1.2"
    assert native_id_for("reports/2024.10") == "reports/2024.10"
    # `.pdf` is not a text document format this pipeline can synthesize from --
    # a binary payload is refused at the mapping seam -- so it is identity here
    # rather than an extension to normalise.
    assert (
        native_id_for("reports/2024.annual.summary.pdf")
        == "reports/2024.annual.summary.pdf"
    )
    # What the strip is FOR: the document extension a server happens to serve.
    assert native_id_for("docs/guide.HTML") == "docs/guide"
    assert native_id_for("docs/guide.md") == native_id_for("docs/guide")


def test_distinct_ids_never_reduce_to_one_published_file():
    # The property, not an example of it -- and asserted at the level where it
    # is actually true. Injectivity over slugs alone is not enough: two DISTINCT
    # native_ids still become one file if `concept_path` and `safe_join`
    # normalize them together downstream, and every core gate passes on the way
    # (three doc_ids, three `change.files` keys, `assert_fetch_contract` and
    # `_check_projection_coherence` both green) before
    # `{safe_join(base, rel): body ...}` collapses them onto one key and the
    # last write wins. Running each id through the whole chain subsumes the
    # slug-level property and catches that class too.
    #
    # Every id here is distinct from every other by something other than the
    # four deliberate equivalences in `native_id_for`'s docstring (document
    # extension, URL scheme and host case, POSIX path normalisation, surrounding
    # whitespace), so every published path must be distinct too. A future lossy
    # change fails HERE, on the property, rather than on whichever example
    # someone happened to pick.
    ids = [
        # versions and dates in the final segment -- the old regex ate these
        "docs/spec-1.2",
        "docs/spec-1.3",
        "docs/spec-1",
        "reports/2024.10",
        "reports/2024.11",
        "reports/2024",
        # the same path on different hosts -- identity was built from the path
        "https://a.com/docs/x.html",
        "https://b.com/docs/x.html",
        "https://a.com/docs/y.html",
        # ...and the plain path that used to be what both of those became
        "docs/x",
        # namespaced non-URL ids -- `urlsplit` called the namespace a scheme
        "SPACE:page.md",
        "ARCHIVE:page.md",
        "page",
        # ...and the remappings that would reintroduce the collision
        "SPACE/page",
        "SPACE-page",
        # query and fragment
        "https://a.com/p?v=1",
        "https://a.com/p?v=2",
        "https://a.com/p#one",
        "https://a.com/p#two",
        "https://a.com/p",
        # a `/` inside a query or fragment is TEXT, not structure. Spliced in
        # raw it became a real path separator, and these four then normalized
        # onto one published file at `safe_join` -- with distinct doc_ids, so
        # nothing upstream could see it. Hash-routed docs and redirect-style
        # queries make these ordinary rather than exotic.
        "https://a.com/p?x=/y",
        "https://a.com/p?x=//y",
        "https://a.com/p?x=/./y",
        "https://a.com/p?x=/../y",
        "https://a.com/p#/guide/intro",
        # `concept_path` strips `.md` from the whole native_id one stage later,
        # so a slug that ends in one would be stripped a second time
        "docs/a.md.md",
        "docs/a.md",
        "https://a.com/p?x=a.md",
        "https://a.com/p?x=a",
        # userinfo is case-SENSITIVE; only the host is folded
        "https://USER:PASS@a.com/p",
        "https://user:pass@a.com/p",
        # port and escape-character handling
        "https://a.com:8080/p",
        "docs/a%3Ab.md",
        "docs/a:b.md",
        "docs/a%253Ab",
        # trailing characters Windows trims
        "docs/trailing.",
        "docs/sub /file",
        "docs/sub/file",
        "docs/trailing",
    ]
    # The real chain: slug -> doc_id -> concept_path -> safe_join, which is the
    # key `forge.py` writes the file under.
    published = [
        safe_join("kb", concept_path(f"fixture:{native_id_for(raw)}")) for raw in ids
    ]
    collisions = {
        path: [raw for raw, p in zip(ids, published, strict=True) if p == path]
        for path in published
        if published.count(path) > 1
    }
    assert not collisions, f"distinct ids share one published file: {collisions}"


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
