import pytest

from kbforge.publishers._http import ForgeError
from kbforge.publishers.forge import ForgeConfig
from kbforge.publishers.github import DEFAULTS, GitHubClient, GitHubPublisher


class FakeTransport:
    """Returns a canned response per (method, url-suffix); records every call."""

    def __init__(self, routes: dict, errors: dict | None = None) -> None:
        self.routes = routes
        self.errors = errors or {}
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, payload=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "payload": payload}
        )
        key = (method, url)
        if key in self.errors:
            raise self.errors[key]
        for (m, suffix), response in self.routes.items():
            if m == method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected call: {method} {url}")


API = "https://api.github.com"


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


def _client(routes, errors=None, **over):
    transport = FakeTransport(routes, errors)
    return GitHubClient(_cfg(**over), transport=transport), transport


def test_default_branch_reads_the_repo(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client({("GET", "/repos/acme/kb"): {"default_branch": "main"}})

    assert client.default_branch() == "main"
    assert transport.calls[0]["url"] == f"{API}/repos/acme/kb"


def test_requests_carry_bearer_auth_and_api_version(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")
    client, transport = _client({("GET", "/repos/acme/kb"): {"default_branch": "main"}})
    client.default_branch()

    headers = transport.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer s3cret"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_api_base_override_targets_github_enterprise(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {("GET", "/repos/acme/kb"): {"default_branch": "main"}},
        api_base="https://ghe.acme/api/v3",
    )
    client.default_branch()

    assert transport.calls[0]["url"] == "https://ghe.acme/api/v3/repos/acme/kb"


def test_put_files_walks_tree_commit_ref(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "base-sha",
            "commit": {"tree": {"sha": "base-tree"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "new-tree"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "new-commit"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/sync/local-files"): {"ref": "ok"},
    }
    client, transport = _client(routes)

    client.put_files("sync/local-files", "main", {"a.md": "A\n"}, [], "msg")

    assert [c["method"] for c in transport.calls] == ["GET", "POST", "POST", "PATCH"]

    tree = transport.calls[1]["payload"]
    assert tree["base_tree"] == "base-tree"
    assert tree["tree"] == [
        {"path": "a.md", "mode": "100644", "type": "blob", "content": "A\n"}
    ]

    commit = transport.calls[2]["payload"]
    assert commit == {"message": "msg", "tree": "new-tree", "parents": ["base-sha"]}

    ref = transport.calls[3]["payload"]
    assert ref == {"sha": "new-commit", "force": True}


def test_put_files_sorts_entries_for_determinism(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/b"): {},
    }
    client, transport = _client(routes)

    client.put_files("b", "main", {"z.md": "Z", "a.md": "A"}, [], "msg")

    paths = [e["path"] for e in transport.calls[1]["payload"]["tree"]]
    assert paths == ["a.md", "z.md"]


def test_put_files_creates_the_ref_when_patch_reports_it_missing(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("POST", "/repos/acme/kb/git/refs"): {"ref": "created"},
    }
    errors = {
        ("PATCH", f"{API}/repos/acme/kb/git/refs/heads/b"): ForgeError(
            422, "u", "Reference does not exist"
        )
    }
    client, transport = _client(routes, errors)

    client.put_files("b", "main", {"a.md": "A"}, [], "msg")

    assert transport.calls[-1]["method"] == "POST"
    assert transport.calls[-1]["url"].endswith("/git/refs")
    assert transport.calls[-1]["payload"] == {"ref": "refs/heads/b", "sha": "nc"}


def test_put_files_reraises_unexpected_ref_errors(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
    }
    errors = {
        ("PATCH", f"{API}/repos/acme/kb/git/refs/heads/b"): ForgeError(
            403, "u", "protected branch"
        )
    }
    client, _ = _client(routes, errors)

    with pytest.raises(ForgeError) as exc:
        client.put_files("b", "main", {"a.md": "A"}, [], "msg")
    assert exc.value.status == 403


def test_put_files_keeps_a_slashed_branch_intact_in_the_ref_path(monkeypatch):
    """A slash is structural in a ref name, so sync/local-files must reach the
    ref path unchanged."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/sync/local-files"): {},
    }
    client, transport = _client(routes)

    client.put_files("sync/local-files", "main", {"a.md": "A"}, [], "msg")

    assert transport.calls[-1]["url"] == (
        f"{API}/repos/acme/kb/git/refs/heads/sync/local-files"
    )


def test_put_files_encodes_a_traversing_branch_in_the_ref_path(monkeypatch):
    """branch defaults to the connector-supplied branch_hint, so a '..' segment
    must not reach the URL path as '..'. Note quote('..', safe='') is still
    '..' — urllib never escapes a dot — so the segment is escaped by hand."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "%2E%2E/%2E%2E/evil"): {},
    }
    client, transport = _client(routes)

    client.put_files("../../evil", "main", {"a.md": "A"}, [], "msg")

    url = transport.calls[-1]["url"]
    assert url == f"{API}/repos/acme/kb/git/refs/heads/%2E%2E/%2E%2E/evil"
    assert "/.." not in url


def test_created_ref_payload_carries_the_raw_branch_not_the_encoded_path(
    monkeypatch,
):
    """sync/local-files cannot discriminate here — its _ref_path()-encoded and
    raw forms are byte-identical, so it passes whether the POST body uses the
    encoded or the raw value. A branch with a character quote(safe="") escapes
    (here '+') is required: the PATCH URL must carry the encoded segment while
    the POST body carries the literal, unencoded ref name."""
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/main"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("POST", "/repos/acme/kb/git/refs"): {"ref": "created"},
    }
    errors = {
        ("PATCH", f"{API}/repos/acme/kb/git/refs/heads/sync/kb%2Bdocs"): ForgeError(
            422, "u", "Reference does not exist"
        )
    }
    client, transport = _client(routes, errors)

    client.put_files("sync/kb+docs", "main", {"a.md": "A"}, [], "msg")

    patch_call, post_call = transport.calls[-2], transport.calls[-1]
    assert patch_call["method"] == "PATCH"
    assert patch_call["url"] == f"{API}/repos/acme/kb/git/refs/heads/sync/kb%2Bdocs"
    assert post_call["method"] == "POST"
    assert post_call["payload"] == {"ref": "refs/heads/sync/kb+docs", "sha": "nc"}


def test_base_ref_is_encoded_without_breaking_slashes(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/repos/acme/kb/commits/release/1.0"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/b"): {},
    }
    client, transport = _client(routes)

    client.put_files("b", "release/1.0", {"a.md": "A"}, [], "msg")

    assert transport.calls[0]["url"] == f"{API}/repos/acme/kb/commits/release/1.0"


def test_traversing_base_ref_is_encoded(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    routes = {
        ("GET", "/commits/%2E%2E/%2E%2E/etc"): {
            "sha": "s",
            "commit": {"tree": {"sha": "t"}},
        },
        ("POST", "/repos/acme/kb/git/trees"): {"sha": "nt"},
        ("POST", "/repos/acme/kb/git/commits"): {"sha": "nc"},
        ("PATCH", "/repos/acme/kb/git/refs/heads/b"): {},
    }
    client, transport = _client(routes)

    client.put_files("b", "../../etc", {"a.md": "A"}, [], "msg")

    assert "/.." not in transport.calls[0]["url"]


def test_find_open_pr_returns_stringified_number(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client({("GET", "&state=open"): [{"number": 42}]})

    assert client.find_open_pr("sync/local-files") == "42"
    assert "head=acme%3Async%2Flocal-files" in transport.calls[0]["url"]
    assert "state=open" in transport.calls[0]["url"]


def test_find_open_pr_returns_none_when_empty(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, _ = _client({("GET", "&state=open"): []})
    assert client.find_open_pr("b") is None


def test_create_pr_returns_html_url(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {
            ("POST", "/repos/acme/kb/pulls"): {
                "html_url": "https://github.com/acme/kb/pull/1"
            }
        }
    )

    url = client.create_pr("b", "main", "T", "B")

    assert url == "https://github.com/acme/kb/pull/1"
    assert transport.calls[0]["payload"] == {
        "title": "T",
        "head": "b",
        "base": "main",
        "body": "B",
    }


def test_update_pr_patches_by_number(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    client, transport = _client(
        {
            ("PATCH", "/repos/acme/kb/pulls/42"): {
                "html_url": "https://github.com/acme/kb/pull/42"
            }
        }
    )

    url = client.update_pr("42", "T", "B")

    assert url == "https://github.com/acme/kb/pull/42"
    assert transport.calls[0]["payload"] == {"title": "T", "body": "B"}


def test_publisher_info_names_it_github():
    info = GitHubPublisher().kbforge_publisher_info()
    assert info.name == "github"
    assert "GitHub" in info.source_system


def test_publisher_validates_config(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    problems = GitHubPublisher().kbforge_validate_publish_config({"repo": "acme/kb"})
    assert any("GITHUB_TOKEN" in p for p in problems)


def test_publisher_validate_accepts_good_config(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    assert GitHubPublisher().kbforge_validate_publish_config({"repo": "acme/kb"}) == []


def test_publisher_validate_reports_unknown_keys(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    problems = GitHubPublisher().kbforge_validate_publish_config(
        {"repo": "acme/kb", "reviewers": ["a"]}
    )
    assert any("reviewers" in p for p in problems)


def test_publisher_has_no_merge_method():
    assert not hasattr(GitHubPublisher(), "merge")
    assert not hasattr(GitHubClient, "merge")
