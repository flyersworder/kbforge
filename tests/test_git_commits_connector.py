import os
import subprocess
from pathlib import Path

from kbforge.canonical import assert_stability
from kbforge.connectors.git_commits import GitCommitsConnector
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher

# Hermetic git: identity via env, no global/system config, fixed dates so nothing
# depends on the developer's git setup or the wall clock.
_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@example.com",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@example.com",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", ""), **_ENV}
    out = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _commit(repo: Path, content: str, message: str, *more: str) -> None:
    (repo / "file.txt").write_text(content)
    _git(repo, "add", "file.txt")
    extra = []
    for m in more:
        extra += ["-m", m]
    _git(repo, "commit", "-q", "-m", message, *extra)


def test_backfill_fetches_all_commits(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "first")
    _commit(repo, "2", "second")
    _commit(repo, "3", "third")
    conn = GitCommitsConnector()
    result = conn.kbforge_fetch({"repo": str(repo)}, None)
    assert len(result.records) == 3
    assert result.cursor.connector == "git_commits"
    assert result.cursor.payload["last_sha"] == _git(repo, "rev-parse", "HEAD")


def test_normalize_builds_canonical_doc(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "add feature X")
    conn = GitCommitsConnector()
    result = conn.kbforge_fetch({"repo": str(repo)}, None)
    docs = conn.kbforge_normalize(result.records)
    assert len(docs) == 1
    doc = docs[0]
    sha = _git(repo, "rev-parse", "HEAD")
    assert doc.doc_id == f"git_commits:{sha}"
    assert doc.title == "add feature X"
    assert doc.structured["author"] == "Tester"
    assert doc.structured["author_email"] == "tester@example.com"
    assert doc.anchor.native_id == sha
    assert doc.anchor.retrieved_at.utcoffset() is not None  # tz-aware
    assert doc.anchor.content_hash  # set during normalize
    assert_stability(conn.kbforge_normalize, result.records)  # must not raise


def test_incremental_fetch_returns_only_new_commits(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "first")
    _commit(repo, "2", "second")
    conn = GitCommitsConnector()
    first = conn.kbforge_fetch({"repo": str(repo)}, None)
    assert len(first.records) == 2
    _commit(repo, "3", "third")
    second = conn.kbforge_fetch({"repo": str(repo)}, first.cursor)
    assert len(second.records) == 1  # only the new commit, not a re-scan
    docs = conn.kbforge_normalize(second.records)
    assert docs[0].title == "third"
    assert second.cursor.payload["last_sha"] == _git(repo, "rev-parse", "HEAD")


def test_no_new_commits_yields_empty_fetch(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "first")
    conn = GitCommitsConnector()
    first = conn.kbforge_fetch({"repo": str(repo)}, None)
    again = conn.kbforge_fetch({"repo": str(repo)}, first.cursor)
    assert again.records == []  # tip..tip is empty


def test_max_commits_bounds_backfill(tmp_path: Path):
    repo = _repo(tmp_path)
    for i in range(5):
        _commit(repo, str(i), f"c{i}")
    conn = GitCommitsConnector()
    result = conn.kbforge_fetch({"repo": str(repo), "max_commits": 2}, None)
    assert len(result.records) == 2  # newest 2 only


def test_multiline_commit_body_is_preserved(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "subject line", "body line one\nbody line two")
    conn = GitCommitsConnector()
    result = conn.kbforge_fetch({"repo": str(repo)}, None)
    docs = conn.kbforge_normalize(result.records)
    assert docs[0].title == "subject line"  # subject only
    assert "body line one" in docs[0].text
    assert "body line two" in docs[0].text


def test_validate_config_rejects_non_repo(tmp_path: Path):
    conn = GitCommitsConnector()
    problems = conn.kbforge_validate_config({"repo": str(tmp_path)})  # no .git
    assert problems and "repo" in problems[0]


def test_validate_config_rejects_bad_max_commits(tmp_path: Path):
    repo = _repo(tmp_path)
    _commit(repo, "1", "first")
    conn = GitCommitsConnector()
    problems = conn.kbforge_validate_config({"repo": str(repo), "max_commits": "ten"})
    assert problems and "max_commits" in problems[0]


def test_pipeline_incremental_over_git_repo(tmp_path: Path):
    # The money test: bootstrap publishes every commit, a later run publishes ONLY
    # the new commit (cursor advanced), and a run with nothing new is a NoOp.
    repo = _repo(tmp_path)
    _commit(repo, "1", "first")
    _commit(repo, "2", "second")
    mirror = str(tmp_path / "mirror")
    state = str(tmp_path / "state")
    pub = {"out_dir": str(tmp_path / "out")}

    r1 = run(
        GitCommitsConnector(),
        DryRunPublisher(),
        config={"repo": str(repo)},
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(r1, Published)
    published = list((Path(r1.url) / "concepts").rglob("overview.md"))
    assert len(published) == 2  # bootstrap: both commits

    _commit(repo, "3", "third")
    r2 = run(
        GitCommitsConnector(),
        DryRunPublisher(),
        config={"repo": str(repo)},
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(r2, Published)
    body = (Path(r2.url) / "MR_BODY.md").read_text("utf-8")
    assert "## Added" in body
    assert "## Modified" not in body  # commits are immutable
    assert body.count("/overview.md") == 1  # only the new commit proposed

    r3 = run(
        GitCommitsConnector(),
        DryRunPublisher(),
        config={"repo": str(repo)},
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(r3, NoOp)  # nothing new → no MR
