from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.canonical import FetchContractError, content_hash
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.grounding import GroundingConfig, has_sidecars
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
from kbforge.synthesize import StubSynthesizer, assemble, concept_path

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
        descriptor = f"{doc.anchor.system}:{doc.anchor.native_id}"
        # The rendered `at` must equal the projection's generated_at below —
        # the strict gate binds the two carriers, since law 4 reads one and
        # whats_stale reads the other.
        at = doc.anchor.retrieved_at.isoformat()
        fm_file = (
            "---\ntype: concept\ntitle: Injected\ndescription: Injected\n"
            f"generated:\n  by: kbforge/test\n  at: '{at}'\n"
            f"sources:\n- id: {descriptor}\n  resource: {descriptor}\n"
            f"  content_hash: {doc.anchor.content_hash}\n"
            "---\n\n# Injected\n\nInjected body.\n"
        )
        return ProposedChange(
            branch_hint="sync/injected",
            files={path: fm_file},
            concepts={
                path: ConceptFrontmatter(
                    type="concept",
                    generated_at=doc.anchor.retrieved_at,
                    sources=[doc.anchor],
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
    system: str = "sys",
    deleted: bool = False,
    relations: list[str] | None = None,
    grounded_by: list[str] | None = None,
    text: str | None = None,
) -> CanonicalDocument:
    """A fixed, clock-free CanonicalDocument keyed under `system` (default "sys")
    — deletions and referrer-relations require a fake source, since
    LocalFilesConnector derives docs from files that exist and can never emit a
    tombstone."""
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"{system}:{native_id}",
        title=title,
        text=text or title,
        relations=relations or [],
        grounded_by=grounded_by or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _FakeConnector:
    """Returns a fixed list of CanonicalDocuments, deterministically — satisfies
    assert_stability without a clock or any real I/O."""

    def __init__(self, docs: list[CanonicalDocument], complete: bool = True):
        self._docs = docs
        self._complete = complete

    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(name="fake", version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config: dict) -> list[str]:
        return []

    def kbforge_fetch(self, config: dict, cursor) -> FetchResult:
        return FetchResult(
            records=[], cursor=Cursor(connector="fake"), complete=self._complete
        )

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
    tmp_path: Path,
    docs: list[CanonicalDocument],
    synthesizer=None,
    grounding_config=None,
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
        grounding_config=grounding_config,
    )
    # Narrows last_change from `ProposedChange | None` for callers, and fails
    # loudly (rather than with a bare AttributeError) if a run unexpectedly
    # didn't publish.
    assert publisher.last_change is not None, f"pipeline did not publish: {result!r}"
    return publisher


def _run_result(tmp_path, docs, synthesizer=None, grounding_config=None):
    """The raw result, so a test can assert NoOp. `_run_once` asserts a publish
    happened and cannot express "nothing should have happened"."""
    publisher = _RecordingPublisher()
    result = run(
        _FakeConnector(docs),
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        synthesizer=synthesizer,
        grounding_config=grounding_config,
    )
    return result, publisher


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


def test_pipeline_rejects_a_duplicate_doc_id_before_it_reaches_the_mirror(tmp_path):
    """Without the law this run publishes happily and silently drops a document:
    diff appends the id to `added` twice, assemble collapses both onto one
    concept_path, and mirror and bundle agree afterwards."""
    docs = [_doc("a.md", "First"), _doc("a.md", "Second")]
    with pytest.raises(FetchContractError) as exc:
        run(
            _FakeConnector(docs),
            _RecordingPublisher(),
            config={},
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={},
        )
    assert str(exc.value) == "duplicate doc_id in fetch output: sys:a.md"
    assert not (tmp_path / "mirror").exists()


def test_pipeline_rejects_a_tombstone_from_an_incomplete_fetch(tmp_path):
    """The invariant CLAUDE.md states but nothing enforced: a rate-limited
    partial fetch must not be able to manufacture a removal."""
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    with pytest.raises(FetchContractError) as exc:
        run(
            _FakeConnector([_doc("gone.md", "Gone", deleted=True)], complete=False),
            _RecordingPublisher(),
            config={},
            mirror=str(tmp_path / "mirror"),
            state_dir=str(tmp_path / "state"),
            publish_config={},
        )
    assert str(exc.value) == "incomplete fetch cannot emit a tombstone: sys:gone.md"


def test_pipeline_still_accepts_a_tombstone_from_a_complete_fetch(tmp_path):
    """The guard must not break ordinary deletion propagation. Sets complete=True
    explicitly (rather than going through _run_once's default) so this test
    distinguishes 'complete is allowed' from '_run_once's default is allowed' —
    without that, this duplicates test_a_tombstone_reaches_the_publisher_as_a_removal
    exactly."""
    _run_once(tmp_path, [_doc("gone.md", "Gone")])
    publisher = _RecordingPublisher()
    result = run(
        _FakeConnector([_doc("gone.md", "Gone", deleted=True)], complete=True),
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
    )
    assert publisher.last_change is not None, f"pipeline did not publish: {result!r}"
    assert publisher.last_change.files_removed == ["concepts/gone/overview.md"]


class _GroundingSynth:
    """Records what grounding it was handed. `grounds = True`, so the pipeline
    both scans for drift and passes the keyword."""

    grounds = True

    def __init__(self):
        self.seen: dict = {}

    def synthesize(
        self, changed_docs, changeset, existing_paths=frozenset(), grounding=None
    ):
        self.seen = grounding or {}
        items = [(d, d.title, d.title, d.text) for d in changed_docs]
        return assemble(items, changeset, existing_paths, grounding=grounding)


def _cfg(**mapping):
    return GroundingConfig(grounding=dict(mapping))


def test_a_legacy_synthesizer_is_never_passed_grounding(tmp_path: Path):
    """`_FixedSynth` is duck-typed with no `grounding` parameter. Passing the
    keyword unconditionally raises TypeError for every synthesizer written
    before this change -- including both test doubles in this file."""
    pub = _run_once(tmp_path, [_doc("a", "A")], synthesizer=_FixedSynth())
    assert pub.last_change is not None


def test_grounding_reaches_the_synthesizer(tmp_path: Path):
    synth = _GroundingSynth()
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(
        tmp_path,
        docs,
        synthesizer=synth,
        grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}),
    )
    assert [d.doc_id for d in synth.seen["sys:a"]] == ["other:SVC1"]


def test_a_grounded_concept_cites_the_owning_system_first(tmp_path: Path):
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    pub = _run_once(
        tmp_path,
        docs,
        synthesizer=_GroundingSynth(),
        grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}),
    )
    assert pub.last_change is not None
    fm = pub.last_change.concepts[concept_path("sys:a")]
    assert [a.system for a in fm.sources] == ["sys", "other"]


def test_drift_in_another_system_reopens_the_owner_on_its_next_run(tmp_path: Path):
    """The whole point. System B's run must not touch A's concepts, and A's next
    run must rebuild what B moved."""
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    a = _doc("a", "A")
    ticket_v1 = _doc("SVC1", "Ticket", system="other")
    _run_once(
        tmp_path, [a, ticket_v1], synthesizer=_GroundingSynth(), grounding_config=cfg
    )

    # B's run alone, with B's document changed.
    ticket_v2 = _doc("SVC1", "Ticket reassigned", system="other")
    pub_b = _run_once(
        tmp_path, [ticket_v2], synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert pub_b.last_change is not None
    assert concept_path("sys:a") not in pub_b.last_change.files  # branch-per-system

    # A's next run, A's own source unchanged.
    pub_a = _run_once(
        tmp_path, [a], synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert pub_a.last_change is not None
    assert concept_path("sys:a") in pub_a.last_change.files
    assert any("another system" in n for n in pub_a.last_change.summary.grounding_notes)


def test_an_unchanged_grounded_run_is_still_a_noop(tmp_path: Path):
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    result, _ = _run_result(
        tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert isinstance(result, NoOp)


def test_emptying_the_grounding_set_settles_after_one_rebuild(tmp_path: Path):
    """The sidecar must be DELETED, not skipped. Skipping leaves rule 3 firing on
    every later run: three would rebuild, and four, and five."""
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(
        tmp_path,
        docs,
        synthesizer=_GroundingSynth(),
        grounding_config=_cfg(**{"sys:a": ["other:SVC1"]}),
    )

    rebuild, _ = _run_result(
        tmp_path,
        docs,
        synthesizer=_GroundingSynth(),
        grounding_config=GroundingConfig(),
    )
    assert isinstance(rebuild, Published)  # the map went away: rebuild once

    settled, _ = _run_result(
        tmp_path,
        docs,
        synthesizer=_GroundingSynth(),
        grounding_config=GroundingConfig(),
    )
    assert isinstance(settled, NoOp)  # and then stop


def test_an_unresolvable_grounding_id_does_not_loop(tmp_path: Path):
    """Declared but never resolvable: rule 3 compares post-resolution sets, so
    this must settle rather than rebuild forever."""
    cfg = _cfg(**{"sys:a": ["nowhere:X"]})
    docs = [_doc("a", "A")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    result, _ = _run_result(
        tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert isinstance(result, NoOp)


def test_an_unresolvable_id_beside_a_resolvable_one_settles(tmp_path: Path):
    """`test_an_unresolvable_grounding_id_does_not_loop` above declares only an
    unresolvable id, so its grounding set is empty and no sidecar is ever
    written -- the comparison in `drifted` never runs. This scenario forces
    that comparison: `a` grounds in one real document and one that never
    resolves, so the sidecar is non-empty and rule 3 must compare against the
    POST-resolution set, or the unresolvable id (present in `declared_ids`,
    permanently absent from the recorded sidecar) drifts every run forever."""
    cfg = _cfg(**{"sys:a": ["other:SVC1", "nowhere:X"]})
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    result, _ = _run_result(
        tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert isinstance(result, NoOp)


def test_a_tombstoned_owner_leaves_no_sidecar(tmp_path: Path):
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    docs = [_doc("a", "A"), _doc("SVC1", "Ticket", system="other")]
    _run_once(tmp_path, docs, synthesizer=_GroundingSynth(), grounding_config=cfg)
    assert has_sidecars(tmp_path / "mirror") is True

    _run_once(
        tmp_path,
        [_doc("a", "A", deleted=True)],
        synthesizer=_GroundingSynth(),
        grounding_config=cfg,
    )
    assert has_sidecars(tmp_path / "mirror") is False


def test_another_systems_drift_is_never_pulled_into_scope(tmp_path: Path):
    """Scoping is by {d.anchor.system for d in docs}, not connector name --
    kbforge-mcp is named `mcp` and carries a configured `system`, so a
    name-based scope would be wrong for exactly the connector that needs this."""
    cfg = _cfg(**{"other:SVC1": ["sys:a"]})  # the OTHER system is grounded
    a_v1 = _doc("a", "A")
    ticket = _doc("SVC1", "Ticket", system="other")
    _run_once(
        tmp_path, [a_v1, ticket], synthesizer=_GroundingSynth(), grounding_config=cfg
    )

    a_v2 = _doc("a", "A rewritten")
    pub = _run_once(
        tmp_path, [a_v2], synthesizer=_GroundingSynth(), grounding_config=cfg
    )
    assert pub.last_change is not None
    assert concept_path("other:SVC1") not in pub.last_change.files


def test_a_document_selected_by_both_drift_and_referrers_renders_once(tmp_path: Path):
    """`referrers` filters on `d.doc_id not in changed`, which knows nothing about
    drift, so the same document can arrive twice."""
    cfg = _cfg(**{"sys:a": ["other:SVC1"]})
    a = _doc("a", "A", relations=["sys:gone"])
    gone = _doc("gone", "Gone")
    ticket_v1 = _doc("SVC1", "Ticket", system="other")
    _run_once(
        tmp_path,
        [a, gone, ticket_v1],
        synthesizer=_GroundingSynth(),
        grounding_config=cfg,
    )

    ticket_v2 = _doc("SVC1", "Ticket reassigned", system="other")
    pub = _run_once(
        tmp_path,
        [_doc("gone", "Gone", deleted=True), ticket_v2],
        synthesizer=_GroundingSynth(),
        grounding_config=cfg,
    )
    assert pub.last_change is not None
    anchors = [a.native_id for a in pub.last_change.summary.sources_changed]
    assert anchors.count("a") == 1
