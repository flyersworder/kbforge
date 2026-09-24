"""Editorial links and the `## Related` section in the pipeline (#41,
architecture.md §7.4). Helpers are local: tests/ is not a package."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge import pipeline
from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig
from kbforge.connectors.local_files import LocalFilesConnector
from kbforge.grounding import GroundingConfig
from kbforge.links import LINKS_DIR, LinksConfig, read_links
from kbforge.mirror import slot_key
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import NoOp, Published, redo, run
from kbforge.publishers.dry_run import DryRunPublisher
from kbforge.related import MARKER
from kbforge.synthesize import assemble, concept_path

X, Y, Z = (concept_path(f"a:{n}") for n in "xyz")


def _doc(
    native_id: str,
    *,
    system: str = "a",
    title: str | None = None,
    text: str | None = None,
    relations: list[str] | None = None,
    deleted: bool = False,
) -> CanonicalDocument:
    doc = CanonicalDocument(
        anchor=ResourceAnchor(
            system=system,
            native_id=native_id,
            retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
            content_hash="",
        ),
        doc_id=f"{system}:{native_id}",
        title=title or native_id,
        text=text or native_id,
        relations=relations or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _Connector:
    def __init__(self, docs, name):
        self.docs, self.name = docs, name

    def kbforge_connector_info(self):
        return ConnectorInfo(name=self.name, version="0.1.0", source_system="t")

    def kbforge_validate_config(self, config):
        return []

    def kbforge_fetch(self, config, cursor):
        return FetchResult(records=[], cursor=Cursor(connector=self.name))

    def kbforge_normalize(self, records):
        return self.docs


class _Publisher:
    def __init__(self):
        self.changes: list[ProposedChange] = []

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="rec", version="0.1.0", source_system="t")

    def kbforge_publish(self, change, config):
        self.changes.append(change)
        return f"recorded://{len(self.changes)}"

    def kbforge_open_request(self, branch_hint, config):
        return None  # every earlier request counts as merged


def _run(root: Path, docs, *, name="a", links=None, synthesizer=None, **kw):
    publisher = _Publisher()
    result = run(
        _Connector(docs, name),
        publisher,
        config={},
        mirror=str(root / "mirror"),
        state_dir=str(root / "state"),
        publish_config={},
        synthesizer=synthesizer,
        links_config=links,
        **kw,
    )
    return result, (publisher.changes[-1] if publisher.changes else None)


def _links(raw: dict) -> LinksConfig:
    return LinksConfig.model_validate({"links": raw})


def _sidecar(root: Path, doc_id: str) -> Path:
    return root / "mirror" / LINKS_DIR / f"{slot_key(doc_id)}.json"


def _section(content: str) -> str:
    return content[content.rindex(MARKER) :]


def test_a_one_way_link_renders_on_the_source_only(tmp_path):
    links = _links({"a:x": [{"to": "a:y", "note": "the requirement that closes it"}]})
    result, change = _run(
        tmp_path, [_doc("x"), _doc("y", title="Fail-mode requirement")], links=links
    )
    assert isinstance(result, Published)
    assert change.concepts[X].links == [Y]
    assert change.concepts[Y].links == []
    assert (
        f"- [Fail-mode requirement](/{Y}) — the requirement that closes it"
        in change.files[X]
    )
    assert MARKER not in change.files[Y]


def test_a_symmetric_link_lands_on_the_targets_own_run(tmp_path):
    links = _links({"a:x": [{"to": "b:y", "note": "same variant", "symmetric": True}]})
    _, first = _run(tmp_path, [_doc("x")], links=links)  # b has not synced yet
    assert first.concepts[X].links == []
    assert (
        f"{X}: link to b:y (links.yaml) was dropped: its target is not in the "
        "bundle (not synced yet, deferred to a later chunk, or deleted); the "
        "link returns if the target is published"
    ) in first.summary.grounding_notes

    result, second = _run(tmp_path, [_doc("y", system="b")], name="b", links=links)
    assert isinstance(result, Published)
    assert set(second.files) == {Y}  # B's run never touches A's concept
    assert second.concepts[Y].links == [X]
    assert f"- [x](/{X}) — same variant" in second.files[Y]
    assert (
        f"{Y}: links to a:x (system a); merge that system's review request "
        "first, or the link dangles until it does"
    ) in second.summary.grounding_notes


def test_a_cross_system_relation_now_links_instead_of_aborting(tmp_path):
    _run(tmp_path, [_doc("y", system="b")], name="b")
    result, change = _run(tmp_path, [_doc("x", relations=["b:y"])])
    assert isinstance(result, Published)
    assert change.concepts[X].links == [Y]
    assert any("links to b:y (system b)" in n for n in change.summary.grounding_notes)


class _Prose:
    """A third-party synthesizer writing its own body."""

    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        items = [(d, d.title, d.title, "Rewritten.") for d in changed_docs]
        return assemble(items, changeset, existing_paths)


def test_every_synthesizer_gets_the_same_section(tmp_path):
    docs = [_doc("x", relations=["a:y"]), _doc("y", title="Why")]
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _, stub = _run(tmp_path / "stub", docs, links=links)
    _, prose = _run(tmp_path / "prose", docs, links=links, synthesizer=_Prose())
    assert "Rewritten." in prose.files[X]
    assert _section(stub.files[X]) == _section(prose.files[X])


def test_the_describe_synthesizer_gets_the_same_section(tmp_path):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from kbforge.llm_synthesizer import DescribeConfig, DescribeSynthesizer

    def fn(messages, info: AgentInfo):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"description": "One sentence.", "tags": []},
                )
            ]
        )

    docs = [_doc("x", relations=["a:y"]), _doc("y")]
    _, stub = _run(tmp_path / "stub", docs)
    config = DescribeConfig()
    agent = DescribeSynthesizer._build_agent(config, model=FunctionModel(fn))
    describer = DescribeSynthesizer(
        config, mirror=tmp_path / "desc" / "mirror", agent=agent
    )
    _, desc = _run(tmp_path / "desc", docs, synthesizer=describer)
    assert _section(stub.files[X]) == _section(desc.files[X])


def test_a_local_files_relation_gets_a_section(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "b.md").write_text("---\ntitle: B\n---\nB.\n", "utf-8")
    (src / "a.md").write_text("---\ntitle: A\nrelations:\n  - b.md\n---\nA.\n", "utf-8")
    result = run(
        LocalFilesConnector(),
        DryRunPublisher(),
        config={"path": str(src)},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={"out_dir": str(tmp_path / "out")},
    )
    assert isinstance(result, Published)
    text = (Path(result.url) / "concepts/a/overview.md").read_text("utf-8")
    assert "- [B](/concepts/b/overview.md)" in text


def test_links_never_reach_the_mirror(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _, change = _run(tmp_path / "with", docs, links=_links({"a:x": ["a:y"]}))
    assert change.concepts[X].links == [Y]  # the links did apply
    _run(tmp_path / "without", docs)

    def slots(root: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in (root / "mirror").glob("*.json")}

    assert slots(tmp_path / "with") == slots(tmp_path / "without")


def test_a_managed_link_is_recorded_after_publish(tmp_path):
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _run(tmp_path, [_doc("x"), _doc("y")], links=links)
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", "n")]
    assert not _sidecar(tmp_path, "a:y").exists()  # nothing managed on y


def test_a_declared_but_unresolved_link_records_an_empty_sidecar(tmp_path):
    _run(tmp_path, [_doc("x")], links=_links({"a:x": ["b:y"]}))
    assert _sidecar(tmp_path, "a:x").exists()
    assert read_links(tmp_path / "mirror", "a:x") == []


def test_a_same_system_relation_writes_no_sidecar(tmp_path):
    _run(tmp_path, [_doc("x", relations=["a:y"]), _doc("y")])
    assert not (tmp_path / "mirror" / LINKS_DIR).exists()


def test_a_tombstone_deletes_its_sidecar(tmp_path):
    links = _links({"a:x": ["a:y"]})
    _run(tmp_path, [_doc("x"), _doc("y")], links=links)
    _run(tmp_path, [_doc("x", deleted=True), _doc("y")], links=links)
    assert not _sidecar(tmp_path, "a:x").exists()


class _DropX:
    def synthesize(self, changed_docs, changeset, existing_paths=frozenset()):
        items = [
            (d, d.title, d.title, d.text) for d in changed_docs if d.doc_id != "a:x"
        ]
        return assemble(items, changeset, existing_paths)


def test_a_document_the_synthesizer_dropped_keeps_its_sidecar(tmp_path):
    _run(
        tmp_path,
        [_doc("x"), _doc("y")],
        links=_links({"a:x": [{"to": "a:y", "note": "n"}]}),
    )
    _run(
        tmp_path,
        [_doc("x", text="x2"), _doc("y", text="y2")],
        links=_links({"a:x": ["a:y"]}),
        synthesizer=_DropX(),
    )
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", "n")]


def _chunked(root, docs, cap, **kw):
    return _run(root, docs, chunking=ChunkingConfig(max_concepts=cap), **kw)


def test_the_other_side_of_a_symmetric_link_is_picked_up_on_its_own_run(tmp_path):
    links = _links({"a:x": [{"to": "b:y", "note": "n", "symmetric": True}]})
    _run(tmp_path, [_doc("x")], links=links)
    _run(tmp_path, [_doc("y", system="b")], name="b", links=links)

    result, change = _run(tmp_path, [_doc("x")], links=links)
    assert isinstance(result, Published)
    assert set(change.files) == {X}
    assert change.concepts[X].links == [Y]
    assert (
        f"{X}: re-synthesized because its links changed since it was last "
        "published; its own source is unchanged"
    ) in change.summary.grounding_notes

    assert isinstance(_run(tmp_path, [_doc("x")], links=links)[0], NoOp)
    assert isinstance(
        _run(tmp_path, [_doc("y", system="b")], name="b", links=links)[0], NoOp
    )


def test_editing_only_a_note_rebuilds_only_that_concept(tmp_path):
    docs = [_doc("x"), _doc("y"), _doc("z")]
    before = {"a:x": [{"to": "a:y", "note": "old"}], "a:z": ["a:y"]}
    after = {"a:x": [{"to": "a:y", "note": "new"}], "a:z": ["a:y"]}
    _run(tmp_path, docs, links=_links(before))
    _, change = _run(tmp_path, docs, links=_links(after))
    assert set(change.files) == {X}
    assert change.files[X].rstrip().endswith("— new")


def test_a_new_entry_rebuilds_its_source(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _run(tmp_path, docs)
    _, change = _run(tmp_path, docs, links=_links({"a:x": ["a:y"]}))
    assert set(change.files) == {X}
    assert change.concepts[X].links == [Y]


def test_a_target_tombstoned_in_another_system_drops_the_link_next_run(tmp_path):
    links = _links({"a:x": ["b:y"]})
    _run(tmp_path, [_doc("y", system="b")], name="b", links=links)
    _, linked = _run(tmp_path, [_doc("x")], links=links)
    assert linked.concepts[X].links == [Y]

    _run(tmp_path, [_doc("y", system="b", deleted=True)], name="b", links=links)
    result, change = _run(tmp_path, [_doc("x")], links=links)
    assert isinstance(result, Published)
    assert change.concepts[X].links == []
    assert MARKER not in change.files[X]
    assert (
        f"{X}: re-synthesized because its links changed since it was last "
        "published; its own source is unchanged"
    ) in change.summary.grounding_notes
    assert isinstance(_run(tmp_path, [_doc("x")], links=links)[0], NoOp)


def test_an_unchanged_world_with_links_is_a_noop(tmp_path):
    docs = [_doc("x"), _doc("y")]
    links = _links({"a:x": [{"to": "a:y", "note": "n"}]})
    _run(tmp_path, docs, links=links)
    assert isinstance(_run(tmp_path, docs, links=links)[0], NoOp)


def test_retitling_a_target_does_not_rebuild_its_referrer(tmp_path):
    links = _links({"a:x": ["b:y"]})
    _run(tmp_path, [_doc("y", system="b", title="Old")], name="b", links=links)
    _run(tmp_path, [_doc("x")], links=links)
    _run(tmp_path, [_doc("y", system="b", title="New")], name="b", links=links)
    assert isinstance(_run(tmp_path, [_doc("x")], links=links)[0], NoOp)


def test_dropping_links_yaml_removes_editorial_links_once(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _run(tmp_path, docs, links=_links({"a:x": ["a:y"]}))
    result, change = _run(tmp_path, docs)  # no --links: the sidecar trips the scan
    assert isinstance(result, Published)
    assert change.concepts[X].links == []
    assert not _sidecar(tmp_path, "a:x").exists()
    assert isinstance(_run(tmp_path, docs)[0], NoOp)


def test_without_links_or_sidecars_the_mirror_is_never_loaded(tmp_path, monkeypatch):
    docs = [_doc("x", relations=["a:y"]), _doc("y")]
    _run(tmp_path, docs)

    def _never(mirror):
        raise AssertionError("load_all ran: the link-drift gate is not holding")

    monkeypatch.setattr(pipeline, "load_all", _never)
    assert isinstance(_run(tmp_path, docs)[0], NoOp)


def test_link_drift_counts_toward_the_cap_and_waits_its_turn(tmp_path):
    docs = [_doc("x"), _doc("y"), _doc("z")]
    _run(tmp_path, docs)
    links = _links({"a:x": ["a:y"], "a:z": ["a:y"]})
    _, first = _chunked(tmp_path, docs, 1, links=links)
    assert set(first.files) == {X}
    assert (
        "chunked review: this request carries 1 of 2 changed concepts; the rest "
        "follow once it is merged or closed"
    ) in first.summary.grounding_notes
    _, second = _chunked(tmp_path, docs, 1, links=links)
    assert set(second.files) == {Z}
    assert not any("chunked review" in n for n in second.summary.grounding_notes)
    assert isinstance(_chunked(tmp_path, docs, 1, links=links)[0], NoOp)


def test_redo_restores_the_links_sidecar(tmp_path):
    docs = [_doc("x"), _doc("y")]
    _chunked(tmp_path, docs, 5)
    _chunked(tmp_path, docs, 5, links=_links({"a:x": ["a:y"]}))
    assert read_links(tmp_path / "mirror", "a:x") == [("a:y", None)]
    redo(
        _Connector([], "a"),
        _Publisher(),
        config={},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
    )
    assert not _sidecar(tmp_path, "a:x").exists()


def test_a_chunk_that_tombstones_a_linked_target_rebuilds_its_referrer(tmp_path):
    # links.yaml, not a relation: `referrers` must still pull x in past the cap,
    # or the chunk removes y while x's published file keeps a link to it.
    links = _links({"a:x": ["a:y"]})
    _run(tmp_path, [_doc("x"), _doc("y"), _doc("z")], links=links)
    docs = [_doc("x"), _doc("y", deleted=True), _doc("z", text="z2")]
    result, change = _chunked(tmp_path, docs, 1, links=links)
    assert isinstance(result, Published)
    assert Z in change.files and Y in change.files_removed
    assert X in change.files
    assert change.concepts[X].links == []
    assert MARKER not in change.files[X]
    assert (
        f"{X}: re-synthesized to drop links to concepts removed in this run; "
        "its own source is unchanged"
    ) in change.summary.grounding_notes
    assert not any("chunked review" in n for n in change.summary.grounding_notes)
    assert isinstance(_chunked(tmp_path, docs, 1, links=links)[0], NoOp)


def test_deferred_link_drift_pulled_in_by_a_referrer_is_not_left_pending(tmp_path):
    # x's link drift (a note edit) is deferred by the cap, but x is a relation
    # referrer of the tombstoned y, so it rides this chunk regardless.
    docs = [_doc("x", relations=["a:y"]), _doc("y"), _doc("z"), _doc("w")]
    _run(tmp_path, docs, links=_links({"a:x": [{"to": "a:w", "note": "old"}]}))
    links = _links({"a:x": [{"to": "a:w", "note": "new"}]})
    now = [
        _doc("x", relations=["a:y"]),
        _doc("y", deleted=True),
        _doc("z", text="z2"),
        _doc("w"),
    ]
    result, change = _chunked(tmp_path, now, 1, links=links)
    assert isinstance(result, Published)
    assert X in change.files
    assert change.files[X].rstrip().endswith("— new")
    assert (
        f"{X}: re-synthesized because its links changed since it was last "
        "published; its own source is unchanged"
    ) in change.summary.grounding_notes
    assert not any("chunked review" in n for n in change.summary.grounding_notes)
    assert isinstance(_chunked(tmp_path, now, 1, links=links)[0], NoOp)


def test_a_title_holding_the_marker_publishes(tmp_path):
    evil = f"Evil {MARKER} title"
    docs = [_doc("x", title=evil, relations=["a:y"]), _doc("y", title=evil)]
    docs.append(_doc("z", title=evil, text=f"a line with {MARKER} inside it"))
    result, change = _run(tmp_path, docs)
    assert isinstance(result, Published)
    assert change.concepts[X].links == [Y]
    assert change.concepts[Z].links == []


def test_a_cross_system_relation_arrives_without_links_yaml(tmp_path):
    # No --links at all: the empty sidecar the relation left is what trips the
    # scan once b syncs.
    x = _doc("x", relations=["b:y"])
    _, first = _run(tmp_path, [x])
    assert first.concepts[X].links == []
    assert read_links(tmp_path / "mirror", "a:x") == []
    assert _sidecar(tmp_path, "a:x").exists()

    _run(tmp_path, [_doc("y", system="b")], name="b")
    result, change = _run(tmp_path, [x])
    assert isinstance(result, Published)
    assert set(change.files) == {X}
    assert change.concepts[X].links == [Y]
    assert read_links(tmp_path / "mirror", "a:x") == [("b:y", None)]
    assert isinstance(_run(tmp_path, [x])[0], NoOp)


class _GroundingSynth:
    grounds = True

    def synthesize(
        self, changed_docs, changeset, existing_paths=frozenset(), grounding=None
    ):
        items = [(d, d.title, d.title, d.text) for d in changed_docs]
        return assemble(items, changeset, existing_paths, grounding=grounding)


def test_grounding_and_link_drift_together_rebuild_once_with_the_grounding_note(
    tmp_path,
):
    grounding = GroundingConfig(grounding={"a:x": ["b:t"]})
    synth = _GroundingSynth()
    kw = {"synthesizer": synth, "grounding_config": grounding}
    _run(tmp_path, [_doc("t", system="b")], name="b", **kw)
    docs = [_doc("x"), _doc("y")]
    _run(tmp_path, docs, links=_links({"a:x": [{"to": "a:y", "note": "old"}]}), **kw)

    _run(tmp_path, [_doc("t", system="b", text="t2")], name="b", **kw)
    links = _links({"a:x": [{"to": "a:y", "note": "new"}]})
    result, change = _run(tmp_path, docs, links=links, **kw)
    assert isinstance(result, Published)
    assert set(change.files) == {X}
    assert change.files[X].rstrip().endswith("— new")
    notes = [n for n in change.summary.grounding_notes if n.startswith(f"{X}:")]
    assert notes == [
        f"{X}: re-synthesized because its grounding changed since it was last "
        "published; its own source is unchanged"
    ]
    assert isinstance(_run(tmp_path, docs, links=links, **kw)[0], NoOp)
