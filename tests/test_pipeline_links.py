"""Editorial links and the `## Related` section in the pipeline (#41,
architecture.md §7.4). Helpers are local: tests/ is not a package."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kbforge.canonical import content_hash
from kbforge.connectors.local_files import LocalFilesConnector
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
from kbforge.pipeline import Published, run
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
        f"{X}: link to b:y (links.yaml) was not found in the mirror or this "
        "fetch and was dropped"
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
