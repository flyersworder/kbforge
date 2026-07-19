"""A credentialed kbforge connector for GitHub issues.

Lives outside kbforge core (the core ships zero credentialed connectors) and is
discovered purely via the `kbforge.connectors` entry-point group. Each issue
becomes one canonical document; the cursor is the max `updated_at` watermark, so a
re-run fetches only issues changed since the last sync. `updated_at` and comment
counts are deliberately kept OUT of the concept, so a comment-only update yields an
identical concept — a no-op — while a real body/label/state change is detected."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import datetime

import httpx

from kbforge.canonical import content_hash
from kbforge.hookspecs import hookimpl
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    RawRecord,
    ResourceAnchor,
)

_SYSTEM = "github_issues"
_API = "https://api.github.com"
_PER_PAGE = 100


class GitHubIssuesConnector:
    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name=_SYSTEM,
            version="0.1.0",
            source_system="GitHub issues (REST API)",
            info_types=["issue"],
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        problems: list[str] = []
        repo = config.get("repo", "")
        if (
            not isinstance(repo, str)
            or repo.count("/") != 1
            or not all(repo.split("/"))
        ):
            problems.append(f"config 'repo' must be 'owner/name': {repo!r}")
        token_env = config.get("token_env", "GITHUB_TOKEN")
        if not os.environ.get(token_env):
            problems.append(f"env var {token_env} is not set")
        state = config.get("state", "all")
        if state not in ("all", "open", "closed"):
            problems.append(f"config 'state' must be all/open/closed: {state!r}")
        return problems

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        owner, repo = config["repo"].split("/", 1)
        token = os.environ[config.get("token_env", "GITHUB_TOKEN")]
        state = config.get("state", "all")
        since = cursor.payload.get("since") if cursor else None

        records: list[RawRecord] = []
        max_updated = since
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with httpx.Client(base_url=_API, headers=headers, timeout=30.0) as client:
            page = 1
            while True:
                params: dict = {
                    "state": state,
                    "per_page": _PER_PAGE,
                    "page": page,
                    "sort": "updated",
                    "direction": "asc",
                }
                if since:
                    params["since"] = since
                resp = client.get(f"/repos/{owner}/{repo}/issues", params=params)
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                for issue in batch:
                    if "pull_request" in issue:  # the issues endpoint also returns PRs
                        continue
                    updated = issue["updated_at"]
                    native_id = f"{owner}/{repo}/issues/{issue['number']}"
                    records.append(
                        RawRecord(
                            anchor_hint={
                                "native_id": native_id,
                                "url": issue.get("html_url"),
                                "retrieved_at": updated,
                            },
                            media_type="application/vnd.github.issue+json",
                            payload=json.dumps(issue, sort_keys=True).encode("utf-8"),
                        )
                    )
                    if max_updated is None or updated > max_updated:
                        max_updated = updated
                if len(batch) < _PER_PAGE:
                    break
                page += 1

        payload = {"since": max_updated} if max_updated else {}
        return FetchResult(
            records=records, cursor=Cursor(connector=_SYSTEM, payload=payload)
        )

    @hookimpl
    def kbforge_normalize(
        self, records: Sequence[RawRecord]
    ) -> list[CanonicalDocument]:
        docs: list[CanonicalDocument] = []
        for rec in records:
            issue = json.loads(rec.payload.decode("utf-8"))
            native_id = rec.anchor_hint["native_id"]
            anchor = ResourceAnchor(
                system=_SYSTEM,
                native_id=native_id,
                url=rec.anchor_hint.get("url"),
                retrieved_at=datetime.fromisoformat(rec.anchor_hint["retrieved_at"]),
                content_hash="",
            )
            doc = CanonicalDocument(
                anchor=anchor,
                doc_id=f"{_SYSTEM}:{native_id}",
                title=str(issue.get("title") or native_id),
                text=(issue.get("body") or "").strip(),
                # updated_at / comment count are intentionally excluded — see module
                # docstring: a comment-only update must stay a no-op.
                structured={
                    "state": issue.get("state"),
                    "author": (issue.get("user") or {}).get("login"),
                    "labels": sorted(
                        label["name"] for label in issue.get("labels", [])
                    ),
                    "created_at": issue.get("created_at"),
                },
                relations=[],
            )
            doc.anchor.content_hash = content_hash(doc)
            docs.append(doc)
        return docs


connector = GitHubIssuesConnector()  # the entry point references this instance
