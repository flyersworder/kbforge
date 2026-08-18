from __future__ import annotations

import pytest

from kbforge_mcp.config import McpSourceConfig, problems_for

STDIO = {
    "system": "aws_docs",
    "transport": {
        "kind": "stdio",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
    },
    "select": {
        "tool": "search_documentation",
        "args": {"search_phrase": "S3 bucket naming", "limit": 20},
        "ids": {"list": "results", "id": "url", "title": "title"},
    },
    "read": {"tool": "read_documentation", "id_arg": "url"},
}


def test_a_valid_stdio_source_has_no_problems():
    assert problems_for(STDIO) == []


def test_a_valid_http_source_has_no_problems():
    cfg = dict(STDIO)
    cfg["transport"] = {
        "kind": "http",
        "url": "https://api.githubcopilot.com/mcp/x/repos/readonly",
        "auth_env": "GITHUB_TOKEN",
    }
    assert problems_for(cfg) == []


def test_the_callable_set_is_exactly_the_two_configured_tools():
    cfg = McpSourceConfig.model_validate(STDIO)
    assert cfg.tool_names == frozenset({"search_documentation", "read_documentation"})


def test_an_unknown_transport_kind_is_rejected_offline():
    cfg = dict(STDIO)
    cfg["transport"] = {"kind": "carrier-pigeon", "url": "https://x"}
    assert any("transport" in p for p in problems_for(cfg))


def test_a_transport_without_a_kind_is_rejected_rather_than_sniffed():
    # v0.2 carried both transports in one `server:` string with no discriminator.
    # There is no sniffing: absent `kind` is a config error.
    cfg = dict(STDIO)
    cfg["transport"] = {"url": "https://example.com/mcp"}
    assert any("kind" in p for p in problems_for(cfg))


def test_a_static_selector_needs_no_select_tool_but_needs_ids():
    cfg = {k: v for k, v in STDIO.items() if k != "select"}
    cfg["static_ids"] = ["docs/a.md", "docs/b.md"]
    assert problems_for(cfg) == []
    cfg_bad = {k: v for k, v in STDIO.items() if k != "select"}
    assert any("static_ids" in p for p in problems_for(cfg_bad))


def test_select_and_static_ids_are_mutually_exclusive():
    cfg = dict(STDIO)
    cfg["static_ids"] = ["docs/a.md"]
    assert any("mutually exclusive" in p for p in problems_for(cfg))


def test_auth_env_names_a_variable_and_never_carries_a_value():
    cfg = dict(STDIO)
    cfg["transport"] = {
        "kind": "http",
        "url": "https://example.com/mcp",
        "auth_env": "ghp_realtokenvalue",
    }
    assert any("must name an environment variable" in p for p in problems_for(cfg))


def test_stdio_env_entries_must_be_variable_names_not_values():
    cfg = dict(STDIO)
    cfg["transport"] = {
        "kind": "stdio",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
        "env": ["AWS_PROFILE", "ghp_realtokenvalue"],
    }
    problems = problems_for(cfg)
    assert any("transport.env" in p and "ghp_realtokenvalue" in p for p in problems)


def test_problems_are_returned_not_raised():
    assert isinstance(problems_for({}), list)
    assert problems_for({}) != []


@pytest.mark.parametrize(
    "system",
    [
        "",
        " ",
        "aws docs",
        "aws/docs",
        "aws:docs",
        "-aws",
        "../escape",
        "docs\n",
    ],
)
def test_an_unusable_system_name_is_rejected_offline(system):
    # `system` is interpolated into every `doc_id` (`f"{system}:{native_id}"`)
    # and into the publish branch (`f"sync/{system}"`). `system: ""` used to
    # validate and produce `doc_id=":docs/a"` plus a bare `sync/` branch git
    # refuses -- discovered at publish time, after synthesis, after tokens.
    # That is the exact failure class `slug.py` exists to prevent for
    # server-supplied ids; the operator-supplied half gets the same treatment.
    cfg = dict(STDIO)
    cfg["system"] = system
    assert any("'system'" in p for p in problems_for(cfg))


@pytest.mark.parametrize("system", ["aws_docs", "github", "servicenow2", "gh-docs"])
def test_an_ordinary_system_name_is_accepted(system):
    cfg = dict(STDIO)
    cfg["system"] = system
    assert problems_for(cfg) == []


@pytest.mark.parametrize(
    "url",
    ["", "example.com/mcp", "ftp://example.com/mcp", "file:///etc/passwd", "https://"],
)
def test_a_non_http_transport_url_is_rejected_offline(url):
    cfg = dict(STDIO)
    cfg["transport"] = {"kind": "http", "url": url}
    assert any("http(s) URL" in p for p in problems_for(cfg))


def test_a_static_arg_may_not_shadow_the_id_arg():
    # `_fetch` merges `{id_arg: ref.raw_id, **static_args}`, so a static_args key
    # equal to id_arg wins and EVERY read returns the same document -- while each
    # record keeps its own native_id, so the run produces N distinct concepts all
    # carrying one file's text and nothing raises anywhere. It is a config
    # mistake, so it is caught here rather than papered over by reordering the
    # merge.
    cfg = dict(STDIO)
    cfg.pop("select")
    cfg["static_ids"] = ["docs/a.md", "docs/b.md"]
    cfg["read"] = {
        "tool": "get_file_contents",
        "id_arg": "path",
        "static_args": {"owner": "o", "repo": "r", "path": "README.md"},
    }
    problems = problems_for(cfg)
    assert any("static_args" in p and "'path'" in p for p in problems), problems


def test_a_static_id_that_cannot_be_slugged_is_caught_offline():
    # Without this the id reports no problems and then raises out of
    # `select_refs` at fetch -- outside `_fetch`'s per-document catch, so one bad
    # configured id aborts the whole run instead of being reported by
    # `kbforge validate`.
    cfg = dict(STDIO)
    cfg.pop("select")
    cfg["static_ids"] = ["../../secrets.md"]
    problems = problems_for(cfg)
    assert any("static_ids" in p and "escapes the bundle" in p for p in problems), (
        problems
    )


def test_two_static_ids_that_slug_to_one_native_id_are_caught_offline():
    # They would become one doc_id and abort the run on "duplicate doc_id". A
    # configured list is the one selector whose ids are all known offline.
    cfg = dict(STDIO)
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/retention"]
    problems = problems_for(cfg)
    assert any("same native_id" in p for p in problems), problems


def test_a_valid_static_id_list_has_no_problems():
    cfg = dict(STDIO)
    cfg.pop("select")
    cfg["static_ids"] = ["docs/retention.md", "docs/policy.md"]
    assert problems_for(cfg) == []
