import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.forge import (
    ForgeConfig,
    PathError,
    build_config,
    publish_to_forge,
    safe_join,
)

DEFAULTS = {"api_base": "https://api.example", "token_env": "EXAMPLE_TOKEN"}


class FakeForgeClient:
    """Records calls so the orchestration can be asserted without a network."""

    def __init__(self, open_pr: str | None = None, default: str = "main") -> None:
        self.calls: list[tuple] = []
        self._open_pr = open_pr
        self._default = default

    def default_branch(self) -> str:
        self.calls.append(("default_branch",))
        return self._default

    def put_files(self, branch, base, files, message) -> None:
        self.calls.append(("put_files", branch, base, files, message))

    def find_open_pr(self, branch):
        self.calls.append(("find_open_pr", branch))
        return self._open_pr

    def create_pr(self, branch, base, title, body):
        self.calls.append(("create_pr", branch, base, title, body))
        return "https://forge.example/pr/1"

    def update_pr(self, pr_id, title, body):
        self.calls.append(("update_pr", pr_id, title, body))
        return "https://forge.example/pr/7"


def _change():
    return ProposedChange(
        branch_hint="sync/local-files",
        files={"concepts/x/overview.md": "# X\n"},
        summary=ChangeSummary(claims_added=["concepts/x/overview.md"]),
    )


def _cfg(**over) -> ForgeConfig:
    return ForgeConfig(repo="acme/kb", **{**DEFAULTS, **over})


# --- safe_join -------------------------------------------------------------


def test_safe_join_without_base_path_returns_rel():
    assert safe_join("", "concepts/x/overview.md") == "concepts/x/overview.md"


def test_safe_join_prefixes_base_path():
    assert safe_join("knowledge", "concepts/x.md") == "knowledge/concepts/x.md"


def test_safe_join_normalizes_redundant_separators():
    assert safe_join("knowledge/", "concepts//x.md") == "knowledge/concepts/x.md"


@pytest.mark.parametrize(
    "base_path, rel",
    [
        ("", "../../.github/workflows/deploy.yml"),
        ("..", "x.md"),
        ("/etc", "passwd"),
        ("", "/etc/passwd"),
        ("knowledge", "../../../x.md"),
    ],
)
def test_safe_join_rejects_traversal_and_absolute_paths(base_path, rel):
    with pytest.raises(PathError):
        safe_join(base_path, rel)


def test_safe_join_rejects_empty_rel():
    with pytest.raises(PathError):
        safe_join("knowledge", "")


# --- ForgeConfig -----------------------------------------------------------


def test_validate_config_accepts_a_good_config(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    assert _cfg().validate_config() == []


def test_validate_config_accepts_nested_gitlab_subgroups(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    cfg = ForgeConfig(repo="group/subgroup/project", **DEFAULTS)
    assert cfg.validate_config() == []


def test_validate_config_reports_missing_repo(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = ForgeConfig(repo="", **DEFAULTS).validate_config()
    assert any("repo" in p for p in problems)


def test_validate_config_reports_malformed_repo(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = ForgeConfig(repo="justname", **DEFAULTS).validate_config()
    assert any("owner/name" in p for p in problems)


def test_validate_config_reports_unset_token_env(monkeypatch):
    monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    problems = _cfg().validate_config()
    assert any("EXAMPLE_TOKEN" in p for p in problems)


def test_validate_config_reports_traversing_base_path(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "t")
    problems = _cfg(base_path="../outside").validate_config()
    assert any("base_path" in p for p in problems)


def test_token_reads_the_named_env_var(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "s3cret")
    assert _cfg().token() == "s3cret"


def test_build_config_applies_defaults_then_overrides():
    cfg = build_config({"repo": "acme/kb", "api_base": "https://ghe.acme"}, DEFAULTS)
    assert cfg.repo == "acme/kb"
    assert cfg.api_base == "https://ghe.acme"
    assert cfg.token_env == "EXAMPLE_TOKEN"


def test_build_config_rejects_unknown_keys():
    with pytest.raises(ValueError) as exc:
        build_config({"repo": "acme/kb", "reviewers": ["a"]}, DEFAULTS)
    assert "reviewers" in str(exc.value)


# --- publish_to_forge ------------------------------------------------------


def test_publish_creates_a_pr_when_none_is_open():
    client = FakeForgeClient(open_pr=None)
    url = publish_to_forge(client, _change(), _cfg(base="main"))

    assert url == "https://forge.example/pr/1"
    names = [c[0] for c in client.calls]
    assert names == ["put_files", "find_open_pr", "create_pr"]


def test_publish_updates_the_open_pr_when_one_exists():
    client = FakeForgeClient(open_pr="7")
    url = publish_to_forge(client, _change(), _cfg(base="main"))

    assert url == "https://forge.example/pr/7"
    names = [c[0] for c in client.calls]
    assert names == ["put_files", "find_open_pr", "update_pr"]
    assert client.calls[-1][1] == "7"


def test_publish_resolves_default_branch_when_base_is_unset():
    client = FakeForgeClient(default="trunk")
    publish_to_forge(client, _change(), _cfg())

    assert client.calls[0] == ("default_branch",)
    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[2] == "trunk"


def test_publish_uses_branch_hint_when_branch_is_unset():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[1] == "sync/local-files"


def test_publish_prefers_configured_branch_over_hint():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", branch="kb/sync"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[1] == "kb/sync"


def test_publish_prefixes_files_with_base_path():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", base_path="knowledge"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[3] == {"knowledge/concepts/x/overview.md": "# X\n"}


def test_publish_sends_the_summary_as_the_pr_body():
    client = FakeForgeClient(open_pr=None)
    publish_to_forge(client, _change(), _cfg(base="main"))

    create = next(c for c in client.calls if c[0] == "create_pr")
    assert create[4].startswith("# Proposed change")
    assert "concepts/x/overview.md" in create[4]


def test_publish_uses_the_title_as_the_commit_message():
    client = FakeForgeClient()
    publish_to_forge(client, _change(), _cfg(base="main", title="kbforge: sync"))

    put = next(c for c in client.calls if c[0] == "put_files")
    assert put[4] == "kbforge: sync"


def test_publish_rejects_a_traversing_file_key():
    client = FakeForgeClient()
    change = ProposedChange(
        branch_hint="sync/x",
        files={"../../.github/workflows/deploy.yml": "evil"},
    )
    with pytest.raises(PathError):
        publish_to_forge(client, change, _cfg(base="main"))
    assert client.calls == []  # nothing reached the forge


def test_forge_client_protocol_has_no_merge_method():
    from kbforge.publishers import forge

    assert not hasattr(forge.ForgeClient, "merge")
    assert not any("merge" in name for name in dir(forge.ForgeClient))
