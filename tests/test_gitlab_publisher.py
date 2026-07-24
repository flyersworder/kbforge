import pytest

from kbforge.publishers._http import ForgeError
from kbforge.publishers.forge import ForgeConfig
from kbforge.publishers.gitlab import (
    _TREE_PAGE_SIZE,
    DEFAULTS,
    GitLabClient,
    GitLabPublisher,
)


class FakeTransport:
    """Returns a canned response per (method, url-suffix); records every call.
    A matching `errors` entry raises instead of returning."""

    def __init__(self, routes: dict, errors: dict | None = None) -> None:
        self.routes = routes
        self.errors = errors or {}
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, payload=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        for (m, suffix), exc in self.errors.items():
            if m == method and url.endswith(suffix):
                raise exc
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected call: {method} {url}")


API = "https://gitlab.com/api/v4"
PROJECT = "acme%2Fkb"

# The tree listing always ends with &page=N, which is what routes key off.
TREE_PAGE_1 = ("GET", "&page=1")
TREE_PAGE_2 = ("GET", "&page=2")
COMMITS = ("POST", "/repository/commits")


def _blobs(*paths):
    return [{"type": "blob", "path": p} for p in paths]


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


def _client(routes, errors=None, **over):
    transport = FakeTransport(routes, errors)
    return GitLabClient(_cfg(**over), transport=transport), transport


def test_default_branch_reads_the_project(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}}
    )

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/projects/{PROJECT}"


def test_requests_carry_private_token_header(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "s3cret")
    client, transport = _client(
        {("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}}
    )
    client.default_branch()

    assert transport.calls[0]["headers"]["PRIVATE-TOKEN"] == "s3cret"


def test_nested_subgroup_path_is_url_encoded(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    transport = FakeTransport(
        {("GET", "/projects/group%2Fsub%2Fkb"): {"default_branch": "main"}}
    )
    client = GitLabClient(
        ForgeConfig(repo="group/sub/kb", **DEFAULTS), transport=transport
    )

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/projects/group%2Fsub%2Fkb"


def test_api_base_override_targets_self_managed(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {("GET", f"/projects/{PROJECT}"): {"default_branch": "main"}},
        api_base="https://gitlab.acme/api/v4",
    )
    client.default_branch()

    assert transport.calls[0]["url"] == f"https://gitlab.acme/api/v4/projects/{PROJECT}"


def test_put_files_commits_in_one_forced_call(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({TREE_PAGE_1: [], COMMITS: {"id": "abc"}})

    client.put_files("sync/local-files", "main", {"a.md": "A\n"}, "msg")

    # One listing of base, then exactly one commit.
    assert [c["method"] for c in transport.calls] == ["GET", "POST"]
    payload = transport.calls[-1]["payload"]
    assert payload["branch"] == "sync/local-files"
    assert payload["start_branch"] == "main"
    assert payload["force"] is True
    assert payload["commit_message"] == "msg"
    assert payload["actions"] == [
        {"action": "create", "file_path": "a.md", "content": "A\n"}
    ]


def test_put_files_sorts_actions_for_determinism(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({TREE_PAGE_1: [], COMMITS: {"id": "abc"}})

    client.put_files("b", "main", {"z.md": "Z", "a.md": "A"}, "msg")

    paths = [a["file_path"] for a in transport.calls[-1]["payload"]["actions"]]
    assert paths == ["a.md", "z.md"]


def test_put_files_updates_a_file_that_already_exists_on_base(monkeypatch):
    """action=create against a path already on base fails with 400 "A file with
    this name already exists" — the commit is built from start_branch, so
    force=true does not empty the tree."""
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {TREE_PAGE_1: _blobs("a.md"), COMMITS: {"id": "abc"}},
    )

    client.put_files("b", "main", {"a.md": "A\n"}, "msg")

    assert transport.calls[-1]["payload"]["actions"] == [
        {"action": "update", "file_path": "a.md", "content": "A\n"}
    ]


def test_put_files_creates_a_file_absent_from_base(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {TREE_PAGE_1: _blobs("other.md"), COMMITS: {"id": "abc"}},
    )

    client.put_files("b", "main", {"a.md": "A\n"}, "msg")

    assert transport.calls[-1]["payload"]["actions"] == [
        {"action": "create", "file_path": "a.md", "content": "A\n"}
    ]


def test_put_files_picks_the_verb_per_path_and_stays_sorted(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {TREE_PAGE_1: _blobs("b.md", "z.md", "stale.md"), COMMITS: {"id": "abc"}},
    )

    client.put_files("br", "main", {"z.md": "Z", "a.md": "A", "b.md": "B"}, "msg")

    assert transport.calls[-1]["payload"]["actions"] == [
        {"action": "create", "file_path": "a.md", "content": "A"},
        {"action": "update", "file_path": "b.md", "content": "B"},
        {"action": "update", "file_path": "z.md", "content": "Z"},
    ]


def test_tree_listing_paginates_until_a_short_page(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    first = _blobs(*[f"p{i:03d}.md" for i in range(_TREE_PAGE_SIZE)])
    client, transport = _client(
        {
            TREE_PAGE_1: first,
            TREE_PAGE_2: _blobs("late.md"),
            COMMITS: {"id": "abc"},
        },
    )

    client.put_files("b", "main", {"p000.md": "0", "late.md": "L"}, "msg")

    pages = [c["url"] for c in transport.calls if c["method"] == "GET"]
    assert len(pages) == 2
    assert f"per_page={_TREE_PAGE_SIZE}" in pages[0]
    assert "recursive=true" in pages[0]
    # Both pages were considered: page 1's path and page 2's path both update.
    assert [a["action"] for a in transport.calls[-1]["payload"]["actions"]] == [
        "update",
        "update",
    ]


def test_tree_listing_ignores_non_blob_entries(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {
            TREE_PAGE_1: [{"type": "tree", "path": "docs"}],
            COMMITS: {"id": "abc"},
        },
    )

    client.put_files("b", "main", {"docs": "D"}, "msg")

    assert transport.calls[-1]["payload"]["actions"][0]["action"] == "create"


def test_tree_listing_404_means_nothing_exists_yet(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {COMMITS: {"id": "abc"}},
        errors={("GET", "&page=1"): ForgeError(404, "u", "404 Tree Not Found")},
    )

    client.put_files("b", "main", {"a.md": "A", "z.md": "Z"}, "msg")

    assert [a["action"] for a in transport.calls[-1]["payload"]["actions"]] == [
        "create",
        "create",
    ]


def test_tree_listing_propagates_other_errors(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, _ = _client(
        {COMMITS: {"id": "abc"}},
        errors={("GET", "&page=1"): ForgeError(403, "u", "forbidden")},
    )

    with pytest.raises(ForgeError) as exc:
        client.put_files("b", "main", {"a.md": "A"}, "msg")
    assert exc.value.status == 403


def test_tree_listing_is_scoped_by_base_path(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {TREE_PAGE_1: [], COMMITS: {"id": "abc"}},
        base_path="knowledge",
    )

    client.put_files("b", "main", {"knowledge/a.md": "A"}, "msg")

    assert "path=knowledge" in transport.calls[0]["url"]
    assert "ref=main" in transport.calls[0]["url"]


def test_find_open_pr_returns_stringified_iid(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("GET", "&state=opened"): [{"iid": 7}]})

    assert client.find_open_pr("sync/local-files") == "7"
    assert "source_branch=sync%2Flocal-files" in transport.calls[0]["url"]
    assert "state=opened" in transport.calls[0]["url"]


def test_find_open_pr_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, _ = _client({("GET", "&state=opened"): []})
    assert client.find_open_pr("b") is None


def test_create_pr_returns_web_url(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {
            ("POST", "/merge_requests"): {
                "web_url": "https://gitlab.com/acme/kb/-/merge_requests/1"
            }
        }
    )

    url = client.create_pr("b", "main", "T", "B")

    assert url == "https://gitlab.com/acme/kb/-/merge_requests/1"
    assert transport.calls[0]["payload"] == {
        "source_branch": "b",
        "target_branch": "main",
        "title": "T",
        "description": "B",
    }


def test_update_pr_puts_by_iid(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client(
        {
            ("PUT", "/merge_requests/7"): {
                "web_url": "https://gitlab.com/acme/kb/-/merge_requests/7"
            }
        }
    )

    url = client.update_pr("7", "T", "B")

    assert url == "https://gitlab.com/acme/kb/-/merge_requests/7"
    assert transport.calls[0]["payload"] == {"title": "T", "description": "B"}


def test_publisher_info_names_it_gitlab():
    info = GitLabPublisher().kbforge_publisher_info()
    assert info.name == "gitlab"
    assert "GitLab" in info.source_system


def test_publisher_validates_config(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    problems = GitLabPublisher().kbforge_validate_publish_config({"repo": "acme/kb"})
    assert any("GITLAB_TOKEN" in p for p in problems)


def test_publisher_validate_accepts_good_config(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    assert GitLabPublisher().kbforge_validate_publish_config({"repo": "acme/kb"}) == []


def test_publisher_has_no_merge_method():
    assert not hasattr(GitLabPublisher(), "merge")
    assert not hasattr(GitLabClient, "merge")
