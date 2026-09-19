"""Chunked review in the pipeline (design/2026-09-19-chunked-review-design.md).
Helpers are local rather than imported from test_pipeline: tests/ is not a
package, so cross-test imports depend on pytest's import mode."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig, owned_paths, read_record
from kbforge.grounding import GroundingConfig
from kbforge.hookspecs import PublisherSpec
from kbforge.mirror import load_all
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import (
    ConfigError,
    Published,
    Redone,
    RedoRefused,
    Waiting,
    _chunk_slot,
    _cursor_slot,
    redo,
    run,
)
from kbforge.synthesize import assemble, concept_path


def _doc(
    native_id: str,
    *,
    system: str = "sys",
    text: str | None = None,
    relations: list[str] | None = None,
    deleted: bool = False,
) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            url=None,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"{system}:{native_id}",
        title=native_id,
        text=text or native_id,
        relations=relations or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _Connector:
    """Fixed docs; records the cursor each fetch was handed, and returns a
    cursor that counts fetches so a held cursor is observable."""

    def __init__(self, docs, name: str = "fake"):
        self.docs = docs
        self.name = name
        self.cursors: list[Cursor | None] = []

    def kbforge_connector_info(self):
        return ConnectorInfo(name=self.name, version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        self.cursors.append(cursor)
        n = 0 if cursor is None else int(cursor.payload.get("n", 0))
        return FetchResult(
            records=[], cursor=Cursor(connector=self.name, payload={"n": n + 1})
        )

    def kbforge_normalize(self, records):
        return self.docs


class _Publisher:
    """Records every change; `open` is what kbforge_open_request reports."""

    def __init__(self, open_request: str | None = None):
        self.open = open_request
        self.changes: list[ProposedChange] = []

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="chunk-test", version="0.1.0", source_system="test")

    def kbforge_publish(self, change, config):
        self.changes.append(change)
        return f"recorded://{len(self.changes)}"

    def kbforge_open_request(self, branch_hint, config):
        return self.open


class _GroundingSynth:
    grounds = True

    def synthesize(
        self, changed_docs, changeset, existing_paths=frozenset(), grounding=None
    ):
        items = [(d, d.title, d.title, d.text) for d in changed_docs]
        return assemble(items, changeset, existing_paths, grounding=grounding)


def _run(tmp_path, docs, *, cap=None, publisher=None, connector=None, **kw):
    publisher = publisher or _Publisher()
    connector = connector or _Connector(docs)
    connector.docs = docs
    result = run(
        connector,
        publisher,
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        chunking=None if cap is None else ChunkingConfig(max_concepts=cap),
        **kw,
    )
    return result, publisher, connector


def _record(tmp_path, name="fake"):
    return read_record(_chunk_slot(tmp_path / "state", name, {}))


def test_an_oversized_run_publishes_and_commits_only_the_first_chunk(tmp_path):
    result, pub, _ = _run(tmp_path, [_doc("a"), _doc("b"), _doc("c")], cap=2)
    assert isinstance(result, Published)
    assert set(pub.changes[0].files) == {concept_path("sys:a"), concept_path("sys:b")}
    assert pub.changes[0].summary.claims_added == sorted(
        [concept_path("sys:a"), concept_path("sys:b")]
    )
    assert [d.doc_id for d in load_all(tmp_path / "mirror")] == ["sys:a", "sys:b"]
    record = _record(tmp_path)
    assert record is not None and record.pending is True
    assert any("carries 2 of 3" in n for n in pub.changes[0].summary.grounding_notes)
    assert record.branch_hints == ["sync/sys"]
    assert record.admitted == ["sys:a", "sys:b"]
    assert record.cursor is None
    assert all(v is None for v in record.mirror.values())
    assert set(record.mirror) == set(owned_paths("sys:a")) | set(owned_paths("sys:b"))


def test_the_chunk_record_captures_the_pre_run_slot_of_a_modified_document(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")])
    slot_path = tmp_path / "mirror" / owned_paths("sys:a")[0]
    pre_run_text = slot_path.read_text("utf-8")

    _, _, _ = _run(tmp_path, [_doc("a", text="a2"), _doc("b")], cap=1)
    record = _record(tmp_path)
    assert record is not None
    assert record.mirror[owned_paths("sys:a")[0]] == pre_run_text


def test_the_cursor_is_held_until_the_final_chunk(tmp_path):
    docs = [_doc("a"), _doc("b"), _doc("c")]
    connector = _Connector(docs)
    _run(tmp_path, docs, cap=2, connector=connector)
    assert not _cursor_slot(tmp_path / "state", "fake", {}).exists()

    _, pub, _ = _run(tmp_path, docs, cap=2, connector=connector)
    assert connector.cursors == [None, None], (
        "the second chunk must re-fetch from the held cursor"
    )
    assert set(pub.changes[0].files) == {concept_path("sys:c")}, (
        "admitted docs were re-proposed"
    )
    assert _cursor_slot(tmp_path / "state", "fake", {}).exists()
    record = _record(tmp_path)
    assert record is not None and record.pending is False


def test_a_link_to_a_backlog_concept_is_dropped_then_restored_on_arrival(tmp_path):
    docs = [_doc("a", relations=["sys:b"]), _doc("b")]
    _, pub1, _ = _run(tmp_path, docs, cap=1)
    assert pub1.changes[0].concepts[concept_path("sys:a")].links == [], (
        "a link to an unpublished backlog concept must not ship"
    )

    _, pub2, _ = _run(tmp_path, docs, cap=1)
    change = pub2.changes[0]
    assert concept_path("sys:a") in change.files, (
        "the referrer was not rebuilt on arrival"
    )
    assert change.concepts[concept_path("sys:a")].links == [concept_path("sys:b")]
    assert any(
        n.startswith(concept_path("sys:a")) and "restore a link" in n
        for n in change.summary.grounding_notes
    )


def test_a_backlog_referrer_of_a_deleted_concept_is_rebuilt_from_its_mirror_copy(
    tmp_path,
):
    _run(tmp_path, [_doc("x"), _doc("r", relations=["sys:x"]), _doc("a")])
    docs = [
        _doc("x", deleted=True),
        _doc("r", text="r2", relations=["sys:x"]),  # modified, but backlog
        _doc("a", text="a2"),  # modified, admitted first (a < r)
    ]
    _, pub, _ = _run(tmp_path, docs, cap=1)
    change = pub.changes[0]
    assert change.files_removed == [concept_path("sys:x")]
    path_r = concept_path("sys:r")
    assert path_r in change.files, "a dangling link to the deleted concept would ship"
    assert change.concepts[path_r].links == []
    assert "r2" not in change.files[path_r], (
        "the backlog modification leaked into this chunk"
    )


def test_drift_counts_toward_the_cap_and_waits_its_turn(tmp_path):
    grounding = GroundingConfig(grounding={"sys:a": ["other:t"]})
    synth = _GroundingSynth()
    a = _doc("a")
    _run(
        tmp_path,
        [a, _doc("t", system="other")],
        synthesizer=synth,
        grounding_config=grounding,
    )
    _run(
        tmp_path,
        [_doc("t", system="other", text="t2")],
        connector=_Connector([], name="other"),
        synthesizer=synth,
        grounding_config=grounding,
    )

    b = _doc("b")
    _, pub1, _ = _run(
        tmp_path, [a, b], cap=1, synthesizer=synth, grounding_config=grounding
    )
    assert set(pub1.changes[0].files) == {concept_path("sys:b")}

    _, pub2, _ = _run(
        tmp_path, [a, b], cap=1, synthesizer=synth, grounding_config=grounding
    )
    assert set(pub2.changes[0].files) == {concept_path("sys:a")}
    assert any(
        "grounding changed" in n for n in pub2.changes[0].summary.grounding_notes
    )


def test_a_deferred_drift_document_rebuilt_as_a_referrer_is_not_left_pending(
    tmp_path,
):
    """A doc that both drifted (grounding changed) and links to a concept this
    chunk removes gets rebuilt via the referrer path regardless of the cap, so
    it must not also count as deferred: it ships with fresh grounding in THIS
    chunk, so `pending` must be False and it must get the "grounding changed"
    note, not be silently dropped from the accounting."""
    grounding = GroundingConfig(grounding={"sys:d": ["other:t"]})
    synth = _GroundingSynth()
    d = _doc("d", relations=["sys:x"])
    x = _doc("x")
    _run(
        tmp_path,
        [x, d, _doc("t", system="other")],
        synthesizer=synth,
        grounding_config=grounding,
    )
    _run(
        tmp_path,
        [_doc("t", system="other", text="t2")],
        connector=_Connector([], name="other"),
        synthesizer=synth,
        grounding_config=grounding,
    )

    docs = [_doc("x", deleted=True), _doc("b")]
    _, pub, _ = _run(
        tmp_path, docs, cap=1, synthesizer=synth, grounding_config=grounding
    )
    change = pub.changes[0]
    path_d = concept_path("sys:d")
    assert path_d in change.files, "the drifted referrer was not rebuilt"
    assert any(
        n.startswith(path_d) and "grounding changed" in n
        for n in change.summary.grounding_notes
    ), "a doc rebuilt via the referrer path must still get its drift note"
    record = _record(tmp_path)
    assert record is not None and record.pending is False
    assert not any("carries" in n for n in change.summary.grounding_notes), (
        "nothing is actually left in backlog, so no carry note should be emitted"
    )


def test_without_chunking_no_record_is_written(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")])
    assert _record(tmp_path) is None


def test_an_open_chunk_request_makes_the_next_run_wait_before_fetching(tmp_path):
    docs = [_doc("a"), _doc("b")]
    connector = _Connector(docs)
    publisher = _Publisher()
    _run(tmp_path, docs, cap=1, connector=connector, publisher=publisher)

    publisher.open = "7"
    result, _, _ = _run(tmp_path, docs, cap=1, connector=connector, publisher=publisher)
    assert result == Waiting(request="7", branch_hint="sync/sys")
    assert len(connector.cursors) == 1, "a waiting run must not fetch"
    assert len(publisher.changes) == 1, "a waiting run must not publish"


def test_a_merged_or_closed_request_releases_the_next_chunk(tmp_path):
    docs = [_doc("a"), _doc("b")]
    publisher = _Publisher()
    _run(tmp_path, docs, cap=1, publisher=publisher)
    publisher.open = None
    result, _, _ = _run(tmp_path, docs, cap=1, publisher=publisher)
    assert isinstance(result, Published)
    assert set(publisher.changes[1].files) == {concept_path("sys:b")}


def test_a_final_chunk_does_not_make_later_runs_wait(tmp_path):
    publisher = _Publisher()
    _run(tmp_path, [_doc("a")], cap=5, publisher=publisher)
    publisher.open = "7"
    result, _, _ = _run(tmp_path, [_doc("a", text="a2")], cap=5, publisher=publisher)
    assert isinstance(result, Published), "small follow-ups append as they do today"


def test_a_publisher_without_the_hook_is_refused_under_chunking(tmp_path):
    class _Hookless:
        def kbforge_publisher_info(self):
            return ConnectorInfo(name="hookless", version="0", source_system="t")

        def kbforge_publish(self, change, config):
            raise AssertionError("must be refused before publishing")

    with pytest.raises(
        ConfigError,
        match="hookless: --chunking needs a publisher that implements "
        "kbforge_open_request",
    ):
        _run(tmp_path, [_doc("a")], cap=1, publisher=_Hookless())


def _tree(*roots: Path) -> dict[str, bytes]:
    return {
        f"{root.name}/{p.relative_to(root).as_posix()}": p.read_bytes()
        for root in roots
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _redo(tmp_path, publisher=None, name="fake"):
    return redo(
        _Connector([], name=name),
        publisher or _Publisher(),
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
    )


def test_redo_restores_mirror_first_seen_and_cursor_byte_for_byte(tmp_path):
    connector = _Connector([])
    _run(tmp_path, [_doc("a")], connector=connector)  # unchunked baseline
    before = _tree(tmp_path / "mirror", tmp_path / "state")

    _run(tmp_path, [_doc("a", text="a2"), _doc("b")], cap=5, connector=connector)
    assert _tree(tmp_path / "mirror", tmp_path / "state") != before

    result = _redo(tmp_path)
    assert result == Redone(admitted=["sys:a", "sys:b"])
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before


def test_after_redo_the_next_run_proposes_the_same_chunk_again(tmp_path):
    docs = [_doc("a"), _doc("b")]
    _, pub1, _ = _run(tmp_path, docs, cap=1)
    _redo(tmp_path)
    _, pub2, _ = _run(tmp_path, docs, cap=1)
    assert set(pub2.changes[0].files) == set(pub1.changes[0].files)


def test_redo_refuses_without_a_record(tmp_path):
    with pytest.raises(RedoRefused, match="fake: no chunk to redo"):
        _redo(tmp_path)


def test_redo_refuses_while_the_request_is_open_and_touches_nothing(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")], cap=1)
    before = _tree(tmp_path / "mirror", tmp_path / "state")
    with pytest.raises(
        RedoRefused, match="review request 7 is still open; close it first"
    ):
        _redo(tmp_path, publisher=_Publisher(open_request="7"))
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before


def test_redo_rolls_back_every_run_published_into_the_still_open_request(tmp_path):
    """Run A's final chunk leaves its request open; run B appends to it. Closing
    that request discards both, so redo must roll back both, not just B."""
    connector = _Connector([])
    publisher = _Publisher()
    _run(tmp_path, [_doc("z")], connector=connector)  # unchunked baseline cursor
    before = _tree(tmp_path / "mirror", tmp_path / "state")

    _run(
        tmp_path,
        [_doc("z"), _doc("a")],
        cap=5,
        connector=connector,
        publisher=publisher,
    )
    publisher.open = "7"
    result, _, _ = _run(
        tmp_path,
        [_doc("z"), _doc("a"), _doc("b")],
        cap=5,
        connector=connector,
        publisher=publisher,
    )
    assert isinstance(result, Published), "a small follow-up appends"

    publisher.open = None  # the reviewer closes the request
    assert _redo(tmp_path, publisher=publisher) == Redone(admitted=["sys:a", "sys:b"])
    ids = {d.doc_id for d in load_all(tmp_path / "mirror")}
    assert "sys:a" not in ids, "run A's concept was discarded, not rolled back"
    assert "sys:b" not in ids, "run B's concept was not rolled back"
    assert _record(tmp_path) is None, "redo must delete the record"
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before, (
        "redo must restore the mirror and the cursor to before run A"
    )


def test_a_small_follow_up_into_an_open_final_chunk_merges_the_record(tmp_path):
    publisher = _Publisher()
    _run(tmp_path, [_doc("a")], cap=5, publisher=publisher)
    first = _record(tmp_path)
    assert first is not None
    publisher.open = "7"
    result, _, _ = _run(
        tmp_path, [_doc("a", text="a2"), _doc("b")], cap=5, publisher=publisher
    )
    assert isinstance(result, Published)
    record = _record(tmp_path)
    assert record is not None
    assert record.admitted == ["sys:a", "sys:b"], "the record was replaced, not merged"
    assert record.mirror[owned_paths("sys:a")[0]] is None, (
        "sys:a must restore to its state before the first run, not after it"
    )
    assert record.cursor == first.cursor
    assert record.pending is False


def test_a_follow_up_after_the_request_closed_replaces_the_record(tmp_path):
    publisher = _Publisher()
    _run(tmp_path, [_doc("a")], cap=5, publisher=publisher)
    result, _, _ = _run(tmp_path, [_doc("a"), _doc("b")], cap=5, publisher=publisher)
    assert isinstance(result, Published)
    record = _record(tmp_path)
    assert record is not None
    assert record.admitted == ["sys:b"], (
        "a request that was merged or closed must not be merged into"
    )


def test_an_oversized_change_waits_while_a_final_chunk_request_is_open(tmp_path):
    publisher = _Publisher()
    _run(tmp_path, [_doc("a")], cap=2, publisher=publisher)
    publisher.open = "7"
    docs = [_doc("a"), _doc("b"), _doc("c"), _doc("d")]
    before = _tree(tmp_path / "mirror", tmp_path / "state")
    result, _, _ = _run(tmp_path, docs, cap=2, publisher=publisher)
    assert result == Waiting(request="7", branch_hint="sync/sys"), (
        "an oversized change must not start chunking into an open request"
    )
    assert len(publisher.changes) == 1, "a waiting run must not publish"
    assert _tree(tmp_path / "mirror", tmp_path / "state") == before, (
        "a waiting run must not touch the mirror, the cursor or the record"
    )


class _InheritsTheSpec(PublisherSpec):
    """A third-party publisher written against PublisherSpec that predates the
    hook: it inherits the spec's docstring-only default, which returns None."""

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="inherits", version="0", source_system="t")

    def kbforge_validate_publish_config(self, config):
        return []

    def kbforge_publish(self, change, config):
        raise AssertionError("must be refused before publishing")


def test_the_inherited_spec_default_counts_as_no_hook_under_chunking(tmp_path):
    with pytest.raises(
        ConfigError,
        match="inherits: --chunking needs a publisher that implements "
        "kbforge_open_request",
    ):
        _run(tmp_path, [_doc("a")], cap=1, publisher=_InheritsTheSpec())


def test_the_inherited_spec_default_counts_as_no_hook_for_redo(tmp_path):
    _run(tmp_path, [_doc("a"), _doc("b")], cap=1)
    with pytest.raises(
        ConfigError,
        match="inherits: redo needs a publisher that implements kbforge_open_request",
    ):
        _redo(tmp_path, publisher=_InheritsTheSpec())
