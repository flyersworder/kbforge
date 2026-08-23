"""The one okfquery claim no offline test can reach: mirror/bundle skew.

okfquery's README tells a reader to join the mirror on `sources.id` and then
*compare* `content_hash`, rather than equi-joining on the hash. The reason is a
window in kbforge's own composition: `pipeline.run` calls `commit(mirror, docs)`
immediately after `kbforge_publish`, and publishing only *opens a review
request* — kbforge never merges. So between publish and merge the mirror already
holds the new hash while the published bundle still serves the previous one, and
an equi-join on the hash silently returns nothing for exactly the concepts under
review: the ones an audit most wants to see.

Every offline test in this package is blind to that. The round-trip test uses
`DryRunPublisher`, which writes a directory and never opens a request, so the
window it would need to observe does not exist there. Only a real forge, with a
real review request left open, puts the mirror and the bundle out of step — which
is why this claim lived in prose until now, asserted from reading `pipeline.py`.

The rules from `tests/test_forge_live.py` apply here too: kbforge's own client
performs every operation under test, and `gh` is only ever the independent
oracle — it merges, and it clones the bundle back so the assertions read a tree
kbforge did not hand us.

Run with:

    GITHUB_TOKEN=$(gh auth token) \\
    KBFORGE_LIVE_GITHUB_REPO=owner/kbforge-live-test \\
    uv run pytest packages/okfquery/tests/test_okfquery_live.py --run-live
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from okfquery import load

RUN_ID = os.environ.get("KBFORGE_LIVE_RUN_ID") or str(int(time.time()))


def _require(var: str) -> str:
    value = os.environ.get(var, "")
    if not value:
        pytest.skip(f"{var} not set")
    return value


def _cli(*args: str) -> str:
    """`gh` as the independent oracle. Never performs the operation under test."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(f"{args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _gh(repo: str, path: str) -> object:
    return json.loads(_cli("gh", "api", f"repos/{repo}{path}"))


def _open_pr_number(repo: str, branch: str) -> int:
    prs = _gh(repo, "/pulls?state=open&per_page=100")
    assert isinstance(prs, list)
    mine = [pr for pr in prs if pr["head"]["ref"] == branch]
    assert len(mine) == 1, f"expected exactly one open PR on {branch}, got {len(mine)}"
    return int(mine[0]["number"])


@pytest.mark.live
def test_open_review_makes_the_hash_equijoin_lie(tmp_path: Path):
    """Publish, merge, edit one concept, publish again — then query across the
    still-unmerged gap.

    The bundle is cloned rather than reassembled file by file: okfquery's unit of
    work is a bundle *directory*, and a directory this test rebuilt from paths it
    already knew would only confirm its own expectations."""
    from kbforge.connectors.local_files import LocalFilesConnector
    from kbforge.pipeline import Published
    from kbforge.pipeline import run as pipeline_run
    from kbforge.publishers.github import GitHubPublisher

    repo = _require("KBFORGE_LIVE_GITHUB_REPO")
    _require("GITHUB_TOKEN")

    branch = f"live-okfquery/{RUN_ID}"
    base_path = f"live/{RUN_ID}-okfquery"
    mirror = tmp_path / "mirror"
    src = tmp_path / "src"
    src.mkdir()
    (src / "orders.md").write_text(
        "---\ntitle: Orders\nowner: team-a\n---\n\nOwns checkout.\n", "utf-8"
    )
    (src / "billing.md").write_text(
        "---\ntitle: Billing\nowner: team-b\n---\n\nIssues invoices.\n", "utf-8"
    )

    def _run():
        return pipeline_run(
            LocalFilesConnector(),
            GitHubPublisher(),
            config={"path": str(src)},
            mirror=str(mirror),
            state_dir=str(tmp_path / "state"),
            publish_config={"repo": repo, "base_path": base_path, "branch": branch},
        )

    # Phase 1 — bootstrap both concepts, then merge by hand. After this the
    # bundle on the default branch and the mirror agree, which is the baseline
    # the skew is measured against.
    first = _run()
    assert isinstance(first, Published), f"bootstrap did not publish: {first}"
    _cli(
        "gh",
        "pr",
        "merge",
        str(_open_pr_number(repo, branch)),
        "--squash",
        "--repo",
        repo,
    )

    # Phase 2 — edit ONE concept. This publishes to the branch and opens a second
    # request, which is deliberately left open: the mirror now carries the new
    # hash for orders while the default branch still serves the old one. Billing
    # is the control — untouched, so it must stay consistent on both joins.
    (src / "orders.md").write_text(
        "---\ntitle: Orders\nowner: team-c\n---\n\nOwns checkout and refunds.\n",
        "utf-8",
    )
    second = _run()
    assert isinstance(second, Published), f"edited source did not publish: {second}"
    _open_pr_number(repo, branch)  # the request is open; nothing merges it

    # Phase 3 — clone the DEFAULT branch. This is the bundle a consumer reads,
    # and it does not yet contain the edit sitting in review.
    default = _gh(repo, "")["default_branch"]
    clone = tmp_path / "clone"
    _cli(
        "gh",
        "repo",
        "clone",
        repo,
        str(clone),
        "--",
        "--depth",
        "1",
        "--branch",
        str(default),
    )
    bundle = clone / base_path
    assert (bundle / "concepts/orders/overview.md").is_file(), (
        f"cloned bundle has no orders concept under {base_path}"
    )
    assert "team-c" not in (bundle / "concepts/orders/overview.md").read_text(
        "utf-8"
    ), (
        "the default branch already carries the edit; the review must stay open "
        "for this test to observe anything"
    )

    con = load(bundle, mirror=mirror)
    orders = "concepts/orders/overview.md"
    billing = "concepts/billing/overview.md"

    # The failure mode the README warns about, demonstrated rather than asserted.
    matched = {
        path
        for (path,) in con.execute(
            """
            SELECT c.path
            FROM concepts c
            JOIN sources s USING (path)
            JOIN mirror m ON s.content_hash = m.anchor.content_hash
            WHERE s.ordinal = 0
            """
        ).fetchall()
    }
    assert orders not in matched, (
        "the hash equi-join returned the concept under review; if this passes "
        "the skew never opened and the rest of the test proves nothing"
    )
    # The control. Without it, an empty join would satisfy the assertion above
    # for entirely the wrong reason.
    assert billing in matched, (
        f"the hash equi-join lost the untouched concept too: {matched}"
    )

    # The form the README actually recommends: join on identity, compare hashes.
    current = dict(
        con.execute(
            """
            SELECT c.path, s.content_hash = m.anchor.content_hash AS is_current
            FROM concepts c
            JOIN sources s USING (path)
            LEFT JOIN mirror m ON s.id = m.doc_id
            WHERE s.ordinal = 0
            """
        ).fetchall()
    )
    assert current.get(orders) is False, (
        f"join-on-id should report the reviewed concept as stale, got {current}"
    )
    assert current.get(billing) is True, (
        f"join-on-id should report the untouched concept as current, got {current}"
    )
