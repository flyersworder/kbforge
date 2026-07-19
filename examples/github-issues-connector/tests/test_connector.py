import json

import httpx
from kbforge_github_issues import GitHubIssuesConnector

from kbforge.canonical import assert_stability, content_hash
from kbforge.models import RawRecord

_ISSUE = {
    "number": 42,
    "title": "Login flow drops the session on refresh",
    "body": "Steps: 1. log in 2. refresh -> session gone.",
    "state": "open",
    "user": {"login": "alice"},
    "labels": [{"name": "bug"}, {"name": "auth"}],
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-02T12:00:00Z",
    "html_url": "https://github.com/acme/app/issues/42",
    "comments": 3,
}


def _record(issue: dict) -> RawRecord:
    return RawRecord(
        anchor_hint={
            "native_id": f"acme/app/issues/{issue['number']}",
            "url": issue["html_url"],
            "retrieved_at": issue["updated_at"],
        },
        media_type="application/vnd.github.issue+json",
        payload=json.dumps(issue, sort_keys=True).encode("utf-8"),
    )


def test_normalize_builds_canonical_doc():
    docs = GitHubIssuesConnector().kbforge_normalize([_record(_ISSUE)])
    assert len(docs) == 1
    doc = docs[0]
    assert doc.doc_id == "github_issues:acme/app/issues/42"
    assert doc.title == "Login flow drops the session on refresh"
    assert "session gone" in doc.text
    assert doc.structured["state"] == "open"
    assert doc.structured["author"] == "alice"
    assert doc.structured["labels"] == ["auth", "bug"]  # sorted
    assert doc.anchor.url == "https://github.com/acme/app/issues/42"
    assert doc.anchor.retrieved_at.utcoffset() is not None  # tz-aware
    assert doc.anchor.content_hash


def test_normalize_is_stable():
    conn = GitHubIssuesConnector()
    assert_stability(conn.kbforge_normalize, [_record(_ISSUE)])  # must not raise


def test_comment_only_update_is_a_noop():
    # A new comment bumps updated_at and the comment count but changes nothing in the
    # body-only concept — the content hash must be identical (design invariant).
    later = {**_ISSUE, "updated_at": "2026-06-09T09:00:00Z", "comments": 7}
    conn = GitHubIssuesConnector()
    before = conn.kbforge_normalize([_record(_ISSUE)])[0]
    after = conn.kbforge_normalize([_record(later)])[0]
    assert content_hash(before) == content_hash(after)


def test_state_change_is_detected():
    closed = {**_ISSUE, "state": "closed"}
    conn = GitHubIssuesConnector()
    a = conn.kbforge_normalize([_record(_ISSUE)])[0]
    b = conn.kbforge_normalize([_record(closed)])[0]
    assert content_hash(a) != content_hash(b)  # state is a real change


def test_validate_config_rejects_bad_input(monkeypatch):
    conn = GitHubIssuesConnector()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    problems = conn.kbforge_validate_config({"repo": "not-a-repo"})
    assert any("repo" in p for p in problems)
    assert any("GITHUB_TOKEN" in p for p in problems)


def test_validate_config_accepts_good_input(monkeypatch):
    conn = GitHubIssuesConnector()
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    assert conn.kbforge_validate_config({"repo": "acme/app", "state": "all"}) == []


def _mock_github(monkeypatch, handler):
    """Route the connector's httpx.Client through a MockTransport — no network."""
    real_client = httpx.Client

    def fake_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


def _issue_json(number: int, updated: str, is_pr: bool = False) -> dict:
    issue = {**_ISSUE, "number": number, "updated_at": updated}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/.../pulls/1"}
    return issue


def test_fetch_paginates_filters_prs_and_advances_cursor(monkeypatch):
    # A full first page (100 items, one of them a PR) must trigger a second request;
    # the PR is dropped; the cursor becomes the max updated_at seen.
    page1 = [
        _issue_json(n, "2026-06-10T00:00:00Z", is_pr=(n == 50)) for n in range(1, 101)
    ]
    seen_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        page = request.url.params.get("page")
        seen_pages.append(page)
        return httpx.Response(200, json=page1 if page == "1" else [])

    _mock_github(monkeypatch, handler)
    result = GitHubIssuesConnector().kbforge_fetch({"repo": "acme/app"}, None)

    assert len(result.records) == 99  # 100 fetched minus the one PR
    assert seen_pages == ["1", "2"]  # paged past the full first page
    assert result.cursor.payload["since"] == "2026-06-10T00:00:00Z"


def test_fetch_incremental_sends_since_watermark(monkeypatch):
    from kbforge.models import Cursor

    sent_params: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent_params.update(request.url.params)
        return httpx.Response(200, json=[])

    _mock_github(monkeypatch, handler)
    cursor = Cursor(
        connector="github_issues", payload={"since": "2026-06-01T00:00:00Z"}
    )
    GitHubIssuesConnector().kbforge_fetch({"repo": "acme/app"}, cursor)

    assert sent_params.get("since") == "2026-06-01T00:00:00Z"  # only fetch since last
