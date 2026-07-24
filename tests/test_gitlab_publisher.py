from kbforge.publishers.forge import ForgeConfig
from kbforge.publishers.gitlab import DEFAULTS, GitLabClient, GitLabPublisher


class FakeTransport:
    """Returns a canned response per (method, url-suffix); records every call."""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, payload=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected call: {method} {url}")


API = "https://gitlab.com/api/v4"
PROJECT = "acme%2Fkb"


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


def _client(routes, **over):
    transport = FakeTransport(routes)
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
    client, transport = _client({("POST", "/repository/commits"): {"id": "abc"}})

    client.put_files("sync/local-files", "main", {"a.md": "A\n"}, "msg")

    assert len(transport.calls) == 1
    payload = transport.calls[0]["payload"]
    assert payload["branch"] == "sync/local-files"
    assert payload["start_branch"] == "main"
    assert payload["force"] is True
    assert payload["commit_message"] == "msg"
    assert payload["actions"] == [
        {"action": "create", "file_path": "a.md", "content": "A\n"}
    ]


def test_put_files_sorts_actions_for_determinism(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    client, transport = _client({("POST", "/repository/commits"): {"id": "abc"}})

    client.put_files("b", "main", {"z.md": "Z", "a.md": "A"}, "msg")

    paths = [a["file_path"] for a in transport.calls[0]["payload"]["actions"]]
    assert paths == ["a.md", "z.md"]


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
