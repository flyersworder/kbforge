from datetime import UTC, datetime
from pathlib import Path

from kbforge.canonical import content_hash
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.models import (
    CanonicalDocument,
    ConceptFrontmatter,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import NoOp, Published, run
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge.synthesize import StubSynthesizer, concept_path

DOC = """---
type: application
title: App X
owner: team-a
---
App X does things.
"""


def _dirs(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_text(DOC, "utf-8")
    return (
        {"path": str(src)},
        str(tmp_path / "mirror"),
        str(tmp_path / "state"),
        {"out_dir": str(tmp_path / "out")},
    )


def test_bootstrap_run_publishes(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(result, Published)
    assert (Path(result.url) / "concepts/x/overview.md").exists()


def test_second_identical_run_is_noop(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    first = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(first, Published)
    second = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(second, NoOp)  # mirror committed → no change → no MR


def test_link_to_unchanged_sibling_survives(tmp_path: Path):
    # A links to B; both bootstrapped. Then only A changes. The A→B link must
    # still resolve — B is unchanged-but-present — not be dropped (§4.4 law 2).
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text("---\ntype: application\ntitle: B\n---\nB.\n", "utf-8")
    a_body = "---\ntype: application\ntitle: {t}\nrelations:\n  - b.md\n---\n{x}\n"
    (src / "a.md").write_text(a_body.format(t="A", x="A one"), "utf-8")
    config = {"path": str(src)}
    mirror = str(tmp_path / "mirror")
    state = str(tmp_path / "state")
    pub = {"out_dir": str(tmp_path / "out")}
    run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )  # bootstrap A and B
    (src / "a.md").write_text(a_body.format(t="A2", x="A two"), "utf-8")
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )  # only A changed
    assert isinstance(result, Published)
    published_a = Path(result.url) / "concepts/a/overview.md"
    assert "concepts/b/overview.md" in published_a.read_text("utf-8")


def test_crlf_reencoding_is_a_noop(tmp_path: Path):
    # Re-saving a file with CRLF endings (mixed OS / git autocrlf) is byte-different
    # but content-identical — it must not register as a change (§4.3 law 1).
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.md").write_bytes(b"---\ntitle: X\n---\nLine one.\nLine two.\n")
    config = {"path": str(src)}
    mirror = str(tmp_path / "mirror")
    state = str(tmp_path / "state")
    pub = {"out_dir": str(tmp_path / "out")}
    first = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(first, Published)
    (src / "x.md").write_bytes(b"---\r\ntitle: X\r\n---\r\nLine one.\r\nLine two.\r\n")
    second = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
    )
    assert isinstance(second, NoOp)  # CRLF flip = same content = no change


class _FixedSynth:
    """A Synthesizer that ignores the LLM and returns a fixed conformant bundle."""

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        doc = changed_docs[0]
        path = concept_path(doc.doc_id)
        fm_file = (
            "---\ntype: concept\ntitle: Injected\ndescription: Injected\n"
            "timestamp: '2026-01-01T00:00:00+00:00'\nresource:\n"
            f"- system: {doc.anchor.system}\n  native_id: {doc.anchor.native_id}\n"
            "  url: null\n---\n\n# Injected\n\nInjected body.\n"
        )
        return ProposedChange(
            branch_hint="sync/injected",
            files={path: fm_file},
            concepts={
                path: ConceptFrontmatter(
                    type="concept",
                    freshness=doc.anchor.retrieved_at,
                    resources=[doc.anchor],
                )
            },
        )


def test_run_uses_injected_synthesizer(tmp_path: Path):
    config, mirror, state, pub = _dirs(tmp_path)
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config=config,
        mirror=mirror,
        state_dir=state,
        publish_config=pub,
        synthesizer=_FixedSynth(),
    )
    assert isinstance(result, Published)
    assert "Injected body." in (Path(result.url) / "concepts/x/overview.md").read_text()


def _doc(
    native_id: str,
    title: str,
    *,
    deleted: bool = False,
    relations: list[str] | None = None,
) -> CanonicalDocument:
    """A fixed, clock-free CanonicalDocument keyed under system "sys" — deletions
    and referrer-relations require a fake source, since LocalFilesConnector
    derives docs from files that exist and can never emit a tombstone."""
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system="sys",
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"sys:{native_id}",
        title=title,
        text=title,
        relations=relations or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _FakeConnector:
    """Returns a fixed list of CanonicalDocuments, deterministically — satisfies
    assert_stability without a clock or any real I/O."""

    def __init__(self, docs: list[CanonicalDocument]):
        self._docs = docs

    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="fake", version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config: dict) -> list[str]:
        return []

    def kbforge_fetch(self, config: dict, cursor) -> FetchResult:
        return FetchResult(records=[], cursor=Cursor(connector="fake"))

    def kbforge_normalize(self, records) -> list[CanonicalDocument]:
        return self._docs


class _RecordingPublisher:
    """Stores the last ProposedChange it was handed, for direct inspection."""

    def __init__(self):
        self.last_change: ProposedChange | None = None

    def kbforge_publisher_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="recording", version="0.1.0", source_system="test")

    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        self.last_change = change
        return "recorded://ok"


def _run_once(
    tmp_path: Path, docs: list[CanonicalDocument], synthesizer=None
) -> _RecordingPublisher:
    """Runs the pipeline against a fake connector returning `docs`, publishing via
    a recording publisher. Reuses tmp_path's mirror/state across calls in a test,
    so a second call diffs against the first."""
    publisher = _RecordingPublisher()
    result = run(
        _FakeConnector(docs),
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        synthesizer=synthesizer,
    )
    # Narrows last_change from `ProposedChange | None` for callers, and fails
    # loudly (rather than with a bare AttributeError) if a run unexpectedly
    # didn't publish.
    assert publisher.last_change is not None, f"pipeline did not publish: {result!r}"
    return publisher


def test_a_tombstone_reaches_the_publisher_as_a_removal(tmp_path):
    """The review body already advertised '## Removed'; the change must honour it."""
    # First run establishes the concept in the mirror.
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])
    assert publisher.last_change is not None

    change = publisher.last_change
    assert change.files_removed == ["concepts/gone/overview.md"]


def test_a_synthesizer_cannot_decide_deletions(tmp_path):
    """Deletion is structure, not prose: whatever a synthesizer returns in
    files_removed is discarded, so an LLM cannot delete a file it dislikes."""

    class Meddling:
        def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
            change = StubSynthesizer().synthesize(
                changed_docs, changeset, existing_paths
            )
            change.files_removed = ["concepts/victim/overview.md"]
            return change

    _run_once(tmp_path, [_doc("a.md", "A")])
    publisher = _run_once(tmp_path, [_doc("a.md", "A2")], synthesizer=Meddling())
    assert publisher.last_change is not None

    assert "concepts/victim/overview.md" not in publisher.last_change.files_removed


def test_a_tombstoned_concept_is_not_treated_as_an_existing_link_target(tmp_path):
    """existing feeds law 2; counting a concept this run deletes would let a
    dangling link ship."""
    _run_once(tmp_path, [_doc("gone.md", "Gone"), _doc("keep.md", "Keep")])
    publisher = _run_once(
        tmp_path,
        [
            _doc("gone.md", "Gone", deleted=True),
            _doc("keep.md", "Keep2", relations=["sys:gone.md"]),
        ],
    )
    assert publisher.last_change is not None

    concept = publisher.last_change.concepts["concepts/keep/overview.md"]
    assert "concepts/gone/overview.md" not in concept.links


def test_an_incremental_tombstone_keeps_a_referrers_other_links(tmp_path):
    """The regression: `existing` built from `docs` alone.

    An incremental connector's second fetch carries only the tombstone, so
    `docs` holds no live document at all. The referrer is pulled in from the
    mirror and re-synthesized — but assemble() drops every link not in
    `existing`, so an empty `existing` strips the referrer's link to 'other',
    which still exists and is still published. §4.4 law 2 only fails on links
    that do not resolve, never on links that went missing, so it ships silently.
    """
    _run_once(
        tmp_path,
        [
            _doc("gone.md", "Gone"),
            _doc("other.md", "Other"),
            _doc("ref.md", "Ref", relations=["sys:gone.md", "sys:other.md"]),
        ],
    )

    # Incremental fetch: the tombstone and nothing else.
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])
    assert publisher.last_change is not None

    change = publisher.last_change
    ref = "concepts/ref/overview.md"
    assert ref in change.files, "the referrer must be re-synthesized"
    assert change.concepts[ref].links == ["concepts/other/overview.md"], (
        "the link to the still-published 'other' must survive; only the link "
        "to the deleted 'gone' may be dropped"
    )
    assert "concepts/other/overview.md" in change.files[ref]


def test_a_referrer_pulled_into_scope_is_explained_in_the_summary(tmp_path):
    """It lands in change.files and therefore in the diff, but in none of
    claims_added/modified/removed — an unexplained file change otherwise."""
    _run_once(
        tmp_path,
        [_doc("gone.md", "Gone"), _doc("ref.md", "Ref", relations=["sys:gone.md"])],
    )
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])
    assert publisher.last_change is not None

    notes = publisher.last_change.summary.grounding_notes
    assert any("concepts/ref/overview.md" in n for n in notes), notes


def test_a_concept_linking_to_a_deleted_one_is_pulled_into_scope(tmp_path):
    """referrer is unchanged, so nothing would otherwise re-synthesize it and its
    link to the deleted concept would survive in the bundle."""
    _run_once(
        tmp_path,
        [
            _doc("gone.md", "Gone"),
            _doc("referrer.md", "Ref", relations=["sys:gone.md"]),
        ],
    )
    publisher = _run_once(tmp_path, [_doc("gone.md", "Gone", deleted=True)])
    assert publisher.last_change is not None

    change = publisher.last_change
    assert "concepts/referrer/overview.md" in change.files
    assert change.concepts["concepts/referrer/overview.md"].links == []
