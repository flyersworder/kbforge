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
from collections.abc import Callable
from dataclasses import dataclass
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


def _change_files(
    branch: str, files: dict[str, str], removed: list[str] | None = None
) -> ProposedChange:
    """Widened sibling of `_change`, for scenarios that touch several files and/or
    remove some, in one run.

    claims_removed is populated alongside files_removed so the review body a
    real forge renders carries the '## Removed' heading — the spec asked for
    that section and nothing was confirming a forge ever displays it."""
    return ProposedChange(
        branch_hint=branch,
        files=files,
        files_removed=removed or [],
        summary=ChangeSummary(
            claims_added=sorted(files), claims_removed=sorted(removed or [])
        ),
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


def _gh_tree(repo: str, ref: str) -> set[str]:
    """Every blob path on `ref`. A *positive* observation: absence of a path is
    read off a listing that succeeded, so a CLI failure makes this raise rather
    than silently satisfy an "it's gone" assertion."""
    head = _gh(repo, f"/commits/{ref}")
    tree = _gh(repo, f"/git/trees/{head['commit']['tree']['sha']}?recursive=1")
    assert not tree.get("truncated"), "tree listing truncated; assertion unsound"
    return {e["path"] for e in tree["tree"] if e["type"] == "blob"}


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


def _gl_tree(repo: str, ref: str) -> set[str]:
    """Every blob path on `ref` — see _gh_tree for why this is a listing rather
    than a failed read."""
    paths: set[str] = set()
    for page in range(1, 21):
        entries = _gl(
            repo,
            f"/repository/tree?ref={quote(ref, safe='')}&recursive=true"
            f"&per_page=100&page={page}",
        )
        paths.update(e["path"] for e in entries if e["type"] == "blob")
        if len(entries) < 100:
            return paths
    raise AssertionError("tree listing did not finish; assertion unsound")


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


# --------------------------------------------------------------------------
# Accumulation and deletion, across both forges
# --------------------------------------------------------------------------
#
# Both defects this scenario guards live in the *steady state* between runs —
# never in any single run — so the offline suite (which drives each adapter
# through a FakeTransport, one call at a time) cannot see either one. Only a
# real forge, driven across several runs against one never-merged branch, can:
#
#   run 1: publish A and B
#   run 2: touch only B — A must survive (rebuilding the branch from base here
#           is the shipped 0.3.0 data-loss bug)
#   run 3: delete A — A must be gone
#   run 4: touch only B again — A must stay deleted (under the old model this
#           reset the branch to base, where A still exists unmerged)
#
# all four runs sharing exactly one open review request throughout.
#
# GitHub and GitLab run the identical scenario above; only the forge-specific
# plumbing (env vars, publisher class, how to read a file back, how to list
# open review requests) differs, so that plumbing is the only thing that
# varies per forge — captured as data in _ForgeUnderTest below — while the
# scenario itself is written once and parametrized over both.


@dataclass(frozen=True)
class _ForgeUnderTest:
    label: str
    repo_env: str
    token_env: str
    branch: str
    make_publisher: Callable[[], Any]
    read_file: Callable[[str, str, str], str]
    open_requests: Callable[[str, str], list[dict]]
    list_paths: Callable[[str, str], set[str]]
    body_key: str  # what the forge calls a review request's description


_FORGES = [
    _ForgeUnderTest(
        label="github",
        repo_env="KBFORGE_LIVE_GITHUB_REPO",
        token_env="GITHUB_TOKEN",
        branch=f"sync/accum-gh-{RUN_ID}",
        make_publisher=GitHubPublisher,
        read_file=_gh_file,
        open_requests=_gh_open_prs,
        list_paths=_gh_tree,
        body_key="body",
    ),
    _ForgeUnderTest(
        label="gitlab",
        repo_env="KBFORGE_LIVE_GITLAB_REPO",
        token_env="GITLAB_TOKEN",
        branch=f"sync/accum-{RUN_ID}",
        make_publisher=GitLabPublisher,
        read_file=_gl_file,
        open_requests=_gl_open_mrs,
        list_paths=_gl_tree,
        body_key="description",
    ),
]


@pytest.mark.live
@pytest.mark.parametrize("forge", _FORGES, ids=lambda f: f.label)
def test_accumulates_across_runs_and_deletes_without_resurrection(
    forge: _ForgeUnderTest,
) -> None:
    repo = _require(forge.repo_env)
    _require(forge.token_env)
    branch = forge.branch
    base_path = f"live/{RUN_ID}-accum-{forge.label}"
    config = {"repo": repo, "base_path": base_path, "branch": branch}
    publisher = forge.make_publisher()
    alpha, beta = "concepts/alpha.md", "concepts/beta.md"

    # Run 1 — two concepts.
    publisher.kbforge_publish(_change_files(branch, {alpha: "A1", beta: "B1"}), config)
    assert forge.read_file(repo, branch, f"{base_path}/{alpha}") == "A1"

    # Run 2 — touch only beta, do not merge. Alpha must survive: rebuilding the
    # branch from base here is the 0.3.0 data-loss bug.
    publisher.kbforge_publish(_change_files(branch, {beta: "B2"}), config)
    assert forge.read_file(repo, branch, f"{base_path}/{alpha}") == "A1", (
        "alpha was rebuilt away"
    )
    assert forge.read_file(repo, branch, f"{base_path}/{beta}") == "B2"

    # Run 3 — delete alpha. Asserted by *listing* the branch, not by failing to
    # read the file: `pytest.raises(AssertionError)` around a read cannot tell
    # "the file is gone" from "the CLI errored", since _cli raises
    # AssertionError on any non-zero exit — an expired token or a transient 5xx
    # would satisfy it. A listing that succeeds and does not contain the path is
    # a positive observation; a CLI failure makes it raise instead.
    publisher.kbforge_publish(
        _change_files(branch, {beta: "B3"}, removed=[alpha]), config
    )
    paths = forge.list_paths(repo, branch)
    assert f"{base_path}/{beta}" in paths, "listing did not see the branch's own files"
    assert f"{base_path}/{alpha}" not in paths, "alpha was not deleted"

    # The review body must actually carry the '## Removed' section the spec
    # asks for — rendered by a real forge, read back through its own CLI.
    open_reqs = forge.open_requests(repo, branch)
    assert len(open_reqs) == 1, f"expected one review request, got {len(open_reqs)}"
    body = open_reqs[0][forge.body_key] or ""
    assert "## Removed" in body, f"review body has no Removed section: {body!r}"
    assert alpha in body, f"review body does not name the removed path: {body!r}"

    # Run 4 — an unrelated change must not resurrect alpha. Under the 0.3.0
    # model this reset the branch to base, where alpha still exists unmerged.
    publisher.kbforge_publish(_change_files(branch, {beta: "B4"}), config)
    paths = forge.list_paths(repo, branch)
    assert f"{base_path}/{alpha}" not in paths, "alpha was resurrected"
    assert forge.read_file(repo, branch, f"{base_path}/{beta}") == "B4"

    open_reqs = forge.open_requests(repo, branch)
    assert len(open_reqs) == 1, (
        f"four runs must share one review request, got {len(open_reqs)}"
    )
