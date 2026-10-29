"""architecture.md §7.5: a system belongs to exactly one source."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kbforge.canonical import content_hash
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ResourceAnchor,
)
from kbforge.pipeline import Aborted, NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher

DOC = "---\ntype: application\ntitle: App X\n---\nApp X does things.\n"


def _kw(tmp_path: Path) -> dict:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "x.md").write_text(DOC, "utf-8")
    return dict(
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )


def _run(tmp_path: Path, **extra):
    return run(LocalFilesConnector(), DryRunPublisher(), **_kw(tmp_path), **extra)


def test_a_fetched_system_owned_by_another_source_aborts_before_diff(tmp_path):
    r = _run(
        tmp_path,
        foreign_systems=frozenset({"local_files"}),
        owners={"local_files": "handbook"},
    )
    assert isinstance(r, Aborted)
    [f] = r.failures
    assert f.law == "source-ownership"
    assert f.message == "system 'local_files' is already owned by source 'handbook'"
    assert not any((tmp_path / "mirror").glob("*.json"))
    assert not any((tmp_path / "state").glob("cursor-*"))


def test_ownership_is_checked_even_when_the_fetch_is_a_noop(tmp_path):
    assert isinstance(_run(tmp_path), Published)
    r = _run(tmp_path, foreign_systems=frozenset({"local_files"}))
    assert isinstance(r, Aborted)
    assert r.failures[0].message == (
        "system 'local_files' is already owned by source '?'"
    )


def test_an_unrelated_foreign_system_does_not_abort(tmp_path):
    assert isinstance(_run(tmp_path, foreign_systems=frozenset({"wiki"})), Published)


def test_noop_and_published_carry_the_fetched_systems(tmp_path):
    first = _run(tmp_path)
    assert isinstance(first, Published)
    assert first.systems == frozenset({"local_files"})
    second = _run(tmp_path)
    assert second == NoOp() and second.systems == frozenset({"local_files"})


class _Quiet:
    """Incremental: fetches `docs`, which a test empties to model a run with
    nothing new since the cursor."""

    def __init__(self, docs):
        self.docs = docs

    def kbforge_connector_info(self):
        return ConnectorInfo(name="quiet", version="0.1.0", source_system="t")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        return FetchResult(records=[], cursor=Cursor(connector="quiet"))

    def kbforge_normalize(self, records):
        return self.docs


def _quiet_doc() -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system="git_commits",
            native_id="abc",
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id="git_commits:abc",
        title="abc",
        text="abc",
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


def test_an_empty_fetch_is_checked_against_its_prior_cursors_systems(tmp_path):
    connector = _Quiet([_quiet_doc()])
    kw: dict[str, Any] = dict(
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )
    assert isinstance(run(connector, DryRunPublisher(), **kw), Published)

    connector.docs = []
    r = run(
        connector,
        DryRunPublisher(),
        **kw,
        foreign_systems=frozenset({"git_commits"}),
        owners={"git_commits": "c1"},
    )
    assert isinstance(r, Aborted)
    [f] = r.failures
    assert f.law == "source-ownership"
    assert f.concept_path == "git_commits"
    assert f.message == "system 'git_commits' is already owned by source 'c1'"
