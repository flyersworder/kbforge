"""`plan()`: `run()`'s own body, stopped before synthesis (project-file design
§6.3). Helpers are local: tests/ is not a package."""

from datetime import UTC, datetime
from pathlib import Path

from kbforge.canonical import content_hash
from kbforge.chunking import ChunkingConfig
from kbforge.grounding import GroundingConfig
from kbforge.links import LinksConfig
from kbforge.models import (
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import (
    Aborted,
    NoOp,
    Planned,
    Published,
    Waiting,
    plan,
    run,
)
from kbforge.synthesize import assemble, concept_path


def _doc(
    native_id: str,
    *,
    system: str = "a",
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
        title=native_id,
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
    """`open` is what kbforge_open_request reports for every branch."""

    def __init__(self, open_request: str | None = None):
        self.open = open_request
        self.changes: list[ProposedChange] = []

    def kbforge_publisher_info(self):
        return ConnectorInfo(name="rec", version="0.1.0", source_system="t")

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


def _common(root: Path, docs, name, publisher, cap, kw):
    return {
        "connector": _Connector(docs, name),
        "publisher": publisher or _Publisher(),
        "config": {},
        "mirror": str(root / "mirror"),
        "state_dir": str(root / "state"),
        "publish_config": {},
        "chunking": None if cap is None else ChunkingConfig(max_concepts=cap),
        **kw,
    }


def _run(root, docs, *, name="a", publisher=None, cap=None, **kw):
    args = _common(root, docs, name, publisher, cap, kw)
    return run(args.pop("connector"), args.pop("publisher"), **args)


def _plan(root, docs, *, name="a", publisher=None, cap=None, grounds=False, **kw):
    args = _common(root, docs, name, publisher, cap, kw)
    return plan(args.pop("connector"), args.pop("publisher"), grounds=grounds, **args)


def _tree(root: Path) -> dict[Path, bytes]:
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _links(raw: dict) -> LinksConfig:
    return LinksConfig.model_validate({"links": raw})


RULES = GroundingConfig.model_validate(
    {
        "rules": [
            {"for": {"system": "a"}, "from": {"system": "b"}, "match": ["{native_id}"]}
        ]
    }
)


def test_plan_writes_nothing(tmp_path):
    synth = _GroundingSynth()
    kw = {"synthesizer": synth, "grounding_config": RULES}
    links = _links({"a:x": [{"to": "a:y", "note": "see also"}]})
    _run(tmp_path, [_doc("t", system="b", text="about x")], name="b", **kw)
    docs = [_doc("x"), _doc("y")]
    first = _run(tmp_path, docs, cap=1, links_config=links, **kw)
    second = _run(tmp_path, docs, cap=1, links_config=links, **kw)
    assert isinstance(first, Published) and isinstance(second, Published)
    before = _tree(tmp_path)
    # Every kind of state a run writes is present, so each could be (not) written.
    kinds = {p.parts[1] for p in before if p.parts[0] == "mirror" and len(p.parts) > 2}
    assert kinds == {"_grounding", "_links", "_first_seen"}
    assert {p.name.split("-")[0] for p in before if p.parts[0] == "state"} == {
        "chunk",
        "cursor",
    }
    edited = [_doc("x", text="x2"), _doc("y", text="y2"), _doc("z")]
    p = _plan(
        tmp_path,
        edited,
        cap=1,
        grounds=True,
        grounding_config=RULES,
        links_config=_links({"a:x": [{"to": "a:y", "note": "changed"}]}),
    )

    assert isinstance(p, Planned)
    assert p.modified == ["a:x"]
    assert p.deferred == 2
    assert _tree(tmp_path) == before


def test_plan_verdict_matches_run(tmp_path):
    # Unchanged world: both NoOp.
    world = tmp_path / "unchanged"
    _run(world, [_doc("x")])
    assert _plan(world, [_doc("x")]) == NoOp()
    assert _run(world, [_doc("x")]) == NoOp()

    # An edit: plan names it, the run right after publishes it.
    world = tmp_path / "edit"
    _run(world, [_doc("x"), _doc("y")])
    p = _plan(world, [_doc("x", text="x2"), _doc("y")])
    assert isinstance(p, Planned)
    assert (p.added, p.modified, p.removed) == ([], ["a:x"], [])
    assert p.systems == frozenset({"a"})
    assert isinstance(_run(world, [_doc("x", text="x2"), _doc("y")]), Published)

    # A pending chunk whose request is open: both wait on the same request.
    world = tmp_path / "waiting"
    pub = _Publisher()
    _run(world, [_doc("x"), _doc("y")], cap=1, publisher=pub)
    pub.open = "!12"
    p = _plan(world, [_doc("x"), _doc("y")], cap=1, publisher=pub)
    r = _run(world, [_doc("x"), _doc("y")], cap=1, publisher=pub)
    assert isinstance(p, Waiting) and p == r
    assert p.request == "!12"

    # A bundle-path collision: both abort with the same failures.
    world = tmp_path / "collision"
    _run(world, [_doc("readme", system="other")], name="other")
    p = _plan(world, [_doc("readme")])
    r = _run(world, [_doc("readme")])
    assert isinstance(p, Aborted) and isinstance(r, Aborted)
    assert p.failures == r.failures
    assert [f.law for f in p.failures] == ["bundle-path-collision"]
    assert p.failures[0].message.startswith("a:readme and other:readme are different")

    # An ownership violation: both abort with run's wording.
    world = tmp_path / "ownership"
    owned = {"foreign_systems": frozenset({"a"}), "owners": {"a": "wiki"}}
    p = _plan(world, [_doc("x")], **owned)
    r = _run(world, [_doc("x")], **owned)
    assert isinstance(p, Aborted) and p == r
    assert [f.message for f in p.failures] == [
        "system 'a' is already owned by source 'wiki'"
    ]


def test_plan_without_open_request_checks_does_not_wait(tmp_path):
    # The pre-fetch check: a pending chunk record and its request still open.
    world = tmp_path / "pending"
    pub = _Publisher()
    _run(world, [_doc("x"), _doc("y")], cap=1, publisher=pub)
    pub.open = "!12"
    docs = [_doc("x"), _doc("y")]
    assert isinstance(_plan(world, docs, cap=1, publisher=pub), Waiting)
    p = _plan(world, docs, cap=1, publisher=pub, check_open_requests=False)
    assert isinstance(p, Planned)
    assert p.added == ["a:y"]

    # The post-diff check: the last chunk finished (not pending) but its request
    # is still open, and a new change is too big for one chunk.
    world = tmp_path / "backlog"
    pub = _Publisher()
    assert isinstance(_run(world, [_doc("x")], cap=1, publisher=pub), Published)
    pub.open = "!13"
    docs = [_doc("x"), _doc("y"), _doc("z")]
    assert isinstance(_plan(world, docs, cap=1, publisher=pub), Waiting)
    p = _plan(world, docs, cap=1, publisher=pub, check_open_requests=False)
    assert isinstance(p, Planned)
    assert (p.added, p.deferred) == (["a:y"], 1)


def test_plan_reports_deferred_backlog(tmp_path):
    docs = [_doc("x"), _doc("y"), _doc("z")]
    p = _plan(tmp_path, docs, cap=1)
    assert p == Planned(
        added=["a:x"],
        modified=[],
        removed=[],
        grounding_drift=[],
        link_drift=[],
        rebuilt=[],
        deferred=2,
        systems=frozenset({"a"}),
    )
    pub = _Publisher()
    assert isinstance(_run(tmp_path, docs, cap=1, publisher=pub), Published)
    # The chunk plan names is the chunk run publishes.
    published = {concept_path(i) for i in ("a:x", "a:y", "a:z")} & set(
        pub.changes[0].files
    )
    assert published == {concept_path(i) for i in p.added}


def test_plan_reports_drift_removals_and_rebuilt_referrers(tmp_path):
    grounding = GroundingConfig(grounding={"a:x": ["b:t"]})
    kw = {"synthesizer": _GroundingSynth(), "grounding_config": grounding}
    _run(tmp_path, [_doc("t", system="b")], name="b", **kw)
    old = _links({"a:w": [{"to": "a:x", "note": "old"}]})
    docs = [_doc("x"), _doc("y", relations=["a:z"]), _doc("z"), _doc("w")]
    _run(tmp_path, docs, links_config=old, **kw)
    _run(tmp_path, [_doc("t", system="b", text="t2")], name="b", **kw)

    new = _links({"a:w": [{"to": "a:x", "note": "new"}]})
    now = [_doc("x"), _doc("y", relations=["a:z"]), _doc("z", deleted=True), _doc("w")]
    p = _plan(tmp_path, now, grounds=True, grounding_config=grounding, links_config=new)
    assert p == Planned(
        added=[],
        modified=[],
        removed=["a:z"],
        grounding_drift=["a:x"],
        link_drift=["a:w"],
        rebuilt=["a:y"],
        deferred=0,
        systems=frozenset({"a"}),
    )
    assert isinstance(_run(tmp_path, now, links_config=new, **kw), Published)
