"""Live tests. Skipped unless --run-live.

AWS Documentation needs network but NO credentials, so it is the one live test in
this repo that can run unattended. GitHub needs GITHUB_TOKEN, and is here for its
tier-1 resource-block reader, which nothing else exercises.

**deepwiki was the original target and was dropped**, recorded here because a
rejected target is reasoning that appears nowhere in code. Measured against the
selector/reader split (architecture §4.1) it fails four ways: it exposes no
resources; `read_wiki_contents(repoName)` takes no page parameter, so granularity
is one document per repo rather than per page; `read_wiki_structure` returns a
prose outline rather than ids; and `ask_question` is an extractor, which
retriever-not-extractor forbids. Its content is also AI-generated from the
upstream repo, so kbforge would be synthesizing concepts out of another model's
synthesis and citing generated prose as provenance. A server can be popular,
stable, and credential-free and still be the wrong shape.
"""

from __future__ import annotations

import os

import pytest

from kbforge.canonical import assert_fetch_contract
from kbforge_mcp.connector import CONNECTOR

pytestmark = pytest.mark.live

AWS = {
    "system": "aws_docs",
    "transport": {
        "kind": "stdio",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
        "env": ["AWS_DOCUMENTATION_PARTITION"],
    },
    "select": {
        "tool": "search_documentation",
        "args": {"search_phrase": "S3 bucket naming rules", "limit": 3},
        "ids": {"list": "search_results", "id": "url", "title": "title"},
    },
    "read": {"tool": "read_documentation", "id_arg": "url"},
}


# Verified against the live server before this task was written:
#   * all five AWS tools report `read_only_hint = None` (never declared). This is
#     the case the client's refuse-only-on-explicit-False rule exists for; a
#     `if not hint: refuse` client could not talk to this server at all.
#   * `search_documentation` returns real tier-2 structuredContent, keyed
#     `search_results` (not `results`).
#   * `read_documentation` returns ONE text block plus a scalar-wrapped
#     `{'result': ...}` structuredContent. With `text_key` unset the reader
#     correctly takes tier 3 (the text blocks).
#   * that text begins with a preamble line, `AWS Documentation from <url>:`,
#     before the document's own `# Heading`. Report what you observe about it;
#     do NOT strip it in the connector -- a source's bytes are the source's bytes,
#     and the fix, if any is wanted, belongs in synthesis.


def test_aws_docs_select_then_read_yields_citable_documents():
    result = CONNECTOR.kbforge_fetch(AWS, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert docs, "expected at least one document"
    assert_fetch_contract(docs, complete=result.complete)
    for d in docs:
        # The slug is path-safe; the full URL survives as provenance.
        assert "://" not in d.anchor.native_id
        # The HOST is part of identity, carried on the `@` segment: the same
        # path served by two hosts is two documents, and a slug built from the
        # url path alone merged them into one doc_id and aborted the run. Every
        # id this selector returns is an aws.amazon.com url, so pin that.
        assert d.anchor.native_id.startswith("@docs.aws.amazon.com/")
        assert d.anchor.url and d.anchor.url.startswith("https://")
        # `d.text.strip()` alone would also be satisfied by a body truncated
        # to just the preamble line -- `assert_fetch_contract` deliberately
        # does not check verbatim content (core has no independent access to
        # the source), so this pair is the only thing standing between a
        # tier-3 truncation regression and a green test. Assert the known
        # preamble is present (pinning it as observed behaviour) AND that
        # real content -- a markdown heading -- follows it; a body cut down
        # to the preamble line satisfies the first and fails the second.
        lines = d.text.splitlines()
        assert lines[0].startswith("AWS Documentation from ")
        assert any(line.startswith("# ") for line in lines[1:])


def test_aws_docs_two_runs_agree_on_content_hashes():
    # The no-op rule depends on this: an unchanged source must hash identically.
    first = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(AWS, None).records)
    second = CONNECTOR.kbforge_normalize(CONNECTOR.kbforge_fetch(AWS, None).records)
    assert [d.anchor.content_hash for d in first] == [
        d.anchor.content_hash for d in second
    ]


# Verified against the live server before this task was written:
#   * both `search_code` and `get_file_contents` report `read_only_hint = True`.
#   * `get_file_contents` returns TWO blocks: a text preamble
#     ("successfully downloaded text file (SHA: ...)") AND an EmbeddedResource
#     whose `.text` is the file. Tier 1 therefore fires on the resource block and
#     the preamble is correctly ignored -- assert the preamble is NOT in the body.
#   * that resource's uri is `repo://owner/repo/sha/<commit-sha>/contents/<path>`.
#     Assert no commit sha reaches `native_id`: this is the live regression guard
#     for the one-to-one identity rule.
#   * `GITHUB_TOKEN` may be supplied from the `gh` CLI (`gh auth token`), which is
#     this repo's existing convention for live forge tests.
@pytest.mark.skipif(not os.environ.get("GITHUB_TOKEN"), reason="GITHUB_TOKEN not set")
def test_github_readonly_endpoint_yields_verbatim_files():
    cfg = {
        "system": "gh_docs",
        "transport": {
            "kind": "http",
            "url": "https://api.githubcopilot.com/mcp/x/repos/readonly",
            "auth_env": "GITHUB_TOKEN",
        },
        # VERIFIED against the live server: `search_code` returns its JSON inside
        # a TEXT block and declares NO structuredContent, so it is a tier-3
        # selector and unmappable by design -- see the note below. GitHub is here
        # for its tier-1 READER, which is the thing no other target exercises, so
        # the selector is the configured id list.
        "static_ids": ["SECURITY.md"],
        "read": {
            "tool": "get_file_contents",
            "id_arg": "path",
            "static_args": {"owner": "modelcontextprotocol", "repo": "servers"},
        },
    }
    result = CONNECTOR.kbforge_fetch(cfg, None)
    docs = CONNECTOR.kbforge_normalize(result.records)
    assert docs
    assert_fetch_contract(docs, complete=result.complete)
    assert "Security Policy" in docs[0].text
    # The tier-1 resource block carries the file, so the preamble text block is
    # not the body.
    assert "successfully downloaded" not in docs[0].text
    # The server's own uri embeds a commit sha; identity must not.
    assert "76d64c82" not in docs[0].anchor.native_id
    # Unchanged by the host-in-identity fix: `SECURITY.md` is a configured
    # repo path, not a url -- it has no scheme and no authority, so there is no
    # host to carry and no `@` segment. Only a real url gains one.
    assert docs[0].anchor.native_id == "SECURITY"
