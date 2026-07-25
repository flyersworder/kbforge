"""Live forge tests — the only tests that let a real GitHub/GitLab answer.

Every other publisher test drives the adapters through a FakeTransport, which
records what we *meant* to send and asserts we sent it. That has zero distance
between the writer and the checker, and it is exactly why the GitLab
create-vs-update bug survived 199 offline tests: the payload was faithfully
what the code intended, and the intent was wrong. Only the forge can say so.

So the rules here are:

* The calls under test go out through kbforge's own clients over urllib.
  `gh`/`glab` never make a call on the code's behalf.
* Every assertion about the resulting state reads back through `gh`/`glab` —
  an independent implementation that cannot collude with ours.

Isolation: each run picks a fresh `live/<run-id>` base_path inside one
persistent scratch repo, so runs never see each other's files and no repo has
to be created or deleted (which would need a `delete_repo` scope we don't ask
for).

Run with:

    GITHUB_TOKEN=$(gh auth token) \\
    GITLAB_TOKEN=$(glab config get token --host gitlab.com) \\
    KBFORGE_LIVE_GITHUB_REPO=owner/kbforge-live-test \\
    KBFORGE_LIVE_GITLAB_REPO=user/kbforge-live-test \\
    uv run pytest tests/test_forge_live.py --run-live
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from typing import Any
from urllib.parse import quote

import pytest

from kbforge.models import ChangeSummary, ProposedChange
from kbforge.publishers.github import GitHubPublisher
from kbforge.publishers.gitlab import GitLabPublisher

RUN_ID = os.environ.get("KBFORGE_LIVE_RUN_ID") or str(int(time.time()))


def _require(var: str) -> str:
    value = os.environ.get(var, "")
    if not value:
        pytest.skip(f"{var} not set")
    return value


def _cli(*args: str) -> str:
    """Run a forge CLI as the independent oracle. Never used to perform the
    operation under test — only to set up, merge, and read back."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _change(branch: str, body: str) -> ProposedChange:
    return ProposedChange(
        branch_hint=branch,
        files={"concepts/checkout.md": body},
        summary=ChangeSummary(claims_added=["checkout accepts a cart"]),
    )


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------


def _gh(repo: str, path: str) -> Any:
    return json.loads(_cli("gh", "api", f"repos/{repo}{path}"))


def _gh_file(repo: str, ref: str, path: str) -> str:
    blob = _gh(repo, f"/contents/{quote(path)}?ref={quote(ref, safe='')}")
    return base64.b64decode(blob["content"]).decode()


def _gh_open_prs(repo: str, branch: str) -> list[dict]:
    prs = _gh(repo, "/pulls?state=open&per_page=100")
    return [pr for pr in prs if pr["head"]["ref"] == branch]


@pytest.mark.live
def test_github_publish_update_and_republish_after_merge():
    repo = _require("KBFORGE_LIVE_GITHUB_REPO")
    _require("GITHUB_TOKEN")
    branch = f"sync/live-{RUN_ID}"
    base_path = f"live/{RUN_ID}"
    target = f"{base_path}/concepts/checkout.md"
    config = {"repo": repo, "base_path": base_path, "branch": branch}
    publisher = GitHubPublisher()

    # Phase 1 — fresh publish opens a branch and exactly one PR.
    url = publisher.kbforge_publish(_change(branch, "first"), config)
    assert repo in url
    assert _gh_file(repo, branch, target) == "first"
    prs = _gh_open_prs(repo, branch)
    assert len(prs) == 1, f"expected 1 open PR, got {len(prs)}"
    pr_number = prs[0]["number"]
    assert prs[0]["base"]["ref"] == _gh(repo, "")["default_branch"]

    # Phase 2 — republishing updates that PR instead of opening a second one.
    publisher.kbforge_publish(_change(branch, "second"), config)
    assert _gh_file(repo, branch, target) == "second"
    prs = _gh_open_prs(repo, branch)
    assert len(prs) == 1, f"republish opened a duplicate PR: {prs}"
    assert prs[0]["number"] == pr_number

    # Phase 3 — the regression case. Merge by hand so the files now exist on
    # base, then publish again: every path must go out as an update.
    _cli("gh", "pr", "merge", str(pr_number), "--squash", "--repo", repo)
    default = _gh(repo, "")["default_branch"]
    assert _gh_file(repo, default, target) == "second"

    publisher.kbforge_publish(_change(branch, "third"), config)
    assert _gh_file(repo, branch, target) == "third"
    prs = _gh_open_prs(repo, branch)
    assert len(prs) == 1, f"expected 1 open PR after re-publish, got {len(prs)}"
    assert prs[0]["number"] != pr_number


# --------------------------------------------------------------------------
# GitLab
# --------------------------------------------------------------------------


def _gl(repo: str, path: str) -> Any:
    project = quote(repo, safe="")
    return json.loads(_cli("glab", "api", f"projects/{project}{path}"))


def _gl_file(repo: str, ref: str, path: str) -> str:
    project = quote(repo, safe="")
    raw = _cli(
        "glab",
        "api",
        f"projects/{project}/repository/files/{quote(path, safe='')}"
        f"/raw?ref={quote(ref, safe='')}",
    )
    return raw.rstrip("\n")


def _gl_open_mrs(repo: str, branch: str) -> list[dict]:
    return _gl(
        repo, f"/merge_requests?state=opened&source_branch={quote(branch, safe='')}"
    )


def _gl_merge(repo: str, iid: int) -> None:
    """Merge by hand, waiting for GitLab to finish computing mergeability."""
    project = quote(repo, safe="")
    for _ in range(30):
        mr = _gl(repo, f"/merge_requests/{iid}")
        if mr["detailed_merge_status"] in ("mergeable", "not_approved"):
            break
        time.sleep(2)
    _cli("glab", "api", "-X", "PUT", f"projects/{project}/merge_requests/{iid}/merge")


@pytest.mark.live
def test_gitlab_publish_update_and_republish_after_merge():
    repo = _require("KBFORGE_LIVE_GITLAB_REPO")
    _require("GITLAB_TOKEN")
    branch = f"sync/live-{RUN_ID}"
    base_path = f"live/{RUN_ID}"
    target = f"{base_path}/concepts/checkout.md"
    config = {"repo": repo, "base_path": base_path, "branch": branch}
    publisher = GitLabPublisher()

    # Phase 1 — fresh publish opens a branch and exactly one MR.
    url = publisher.kbforge_publish(_change(branch, "first"), config)
    assert repo in url
    assert _gl_file(repo, branch, target) == "first"
    mrs = _gl_open_mrs(repo, branch)
    assert len(mrs) == 1, f"expected 1 open MR, got {len(mrs)}"
    mr_iid = mrs[0]["iid"]
    assert mrs[0]["target_branch"] == _gl(repo, "")["default_branch"]

    # Phase 2 — republishing updates that MR instead of opening a second one.
    publisher.kbforge_publish(_change(branch, "second"), config)
    assert _gl_file(repo, branch, target) == "second"
    mrs = _gl_open_mrs(repo, branch)
    assert len(mrs) == 1, f"republish opened a duplicate MR: {mrs}"
    assert mrs[0]["iid"] == mr_iid

    # Phase 3 — the case that broke. Before the create-vs-update fix, this
    # raised 400 "A file with this name already exists", permanently wedging
    # every sync after the first human merge.
    _gl_merge(repo, mr_iid)
    default = _gl(repo, "")["default_branch"]
    assert _gl_file(repo, default, target) == "second"

    publisher.kbforge_publish(_change(branch, "third"), config)
    assert _gl_file(repo, branch, target) == "third"
    mrs = _gl_open_mrs(repo, branch)
    assert len(mrs) == 1, f"expected 1 open MR after re-publish, got {len(mrs)}"
    assert mrs[0]["iid"] != mr_iid
