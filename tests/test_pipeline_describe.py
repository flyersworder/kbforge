"""The pipeline persists, clears and tombstones `_described/` (#40). Helpers
below are copied verbatim from tests/test_pipeline.py: tests/ is not a
package, so cross-test imports depend on pytest's import mode."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.canonical import content_hash  # noqa: E402
from kbforge.described import read_described  # noqa: E402
from kbforge.llm_synthesizer import DescribeConfig, DescribeSynthesizer  # noqa: E402
from kbforge.models import (  # noqa: E402
    CanonicalDocument,
    ConnectorInfo,
    Cursor,
    FetchResult,
    ProposedChange,
    ResourceAnchor,
)
from kbforge.pipeline import NoOp, run  # noqa: E402
from kbforge.synthesize import StubSynthesizer  # noqa: E402


def _doc(
    native_id: str,
    title: str,
    *,
    system: str = "sys",
    deleted: bool = False,
    relations: list[str] | None = None,
    grounded_by: list[str] | None = None,
    text: str | None = None,
    structured: dict | None = None,
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
        structured=structured or {},
        relations=relations or [],
        grounded_by=grounded_by or [],
        deleted=deleted,
    )
    doc.anchor.content_hash = content_hash(doc)
    return doc


class _FakeConnector:
    """Returns a fixed list of CanonicalDocuments, deterministically — satisfies
    assert_stability without a clock or any real I/O."""

    def __init__(
        self,
        docs: list[CanonicalDocument],
        complete: bool = True,
        name: str = "fake",
    ):
        self._docs = docs
        self._complete = complete
        self._name = name

    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(name=self._name, version="0.1.0", source_system="sys")

    def kbforge_validate_config(self, config: dict) -> list[str]:
        return []

    def kbforge_fetch(self, config: dict, cursor) -> FetchResult:
        return FetchResult(
            records=[], cursor=Cursor(connector=self._name), complete=self._complete
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


def _run_result(
    tmp_path,
    docs,
    synthesizer=None,
    grounding_config=None,
    connector_name: str = "fake",
    config: dict | None = None,
):
    """The raw result, so a test can assert NoOp. `_run_once` asserts a publish
    happened and cannot express "nothing should have happened"."""
    publisher = _RecordingPublisher()
    result = run(
        _FakeConnector(docs, name=connector_name),
        publisher,
        config=config or {},
        mirror=str(tmp_path / "mirror"),
        state_dir=str(tmp_path / "state"),
        publish_config={},
        synthesizer=synthesizer,
        grounding_config=grounding_config,
    )
    return result, publisher


def _describer(mirror: Path, calls: list[str]) -> DescribeSynthesizer:
    def fn(messages, info: AgentInfo):
        calls.append("call")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"description": "One sentence.", "tags": []},
                )
            ]
        )

    config = DescribeConfig()
    agent = DescribeSynthesizer._build_agent(config, model=FunctionModel(fn))
    return DescribeSynthesizer(config, mirror=mirror, agent=agent)


def test_publish_writes_the_record_and_an_unchanged_rerun_is_a_free_noop(tmp_path):
    calls: list[str] = []
    docs = [_doc("a.md", "A")]
    _run_result(tmp_path, docs, synthesizer=_describer(tmp_path / "mirror", calls))
    record = read_described(tmp_path / "mirror", "sys:a.md")
    assert record is not None and record.description == "One sentence."
    result, _ = _run_result(
        tmp_path, docs, synthesizer=_describer(tmp_path / "mirror", calls)
    )
    assert isinstance(result, NoOp) and calls == ["call"]


def test_an_arrival_rebuild_reuses_the_record(tmp_path):
    """`ref` links to `later`, which does not exist yet -- law 2 drops the
    dangling link, so `ref` is republished once `later` arrives, purely to
    restore the link. Its own source is unchanged, so the describe cache
    must still hit: only `later` (new, never described) may call the model."""
    calls: list[str] = []
    mirror = tmp_path / "mirror"
    ref = _doc("ref.md", "Ref", relations=["sys:later.md"])
    _run_result(tmp_path, [ref], synthesizer=_describer(mirror, calls))
    assert calls == ["call"]
    _, pub = _run_result(tmp_path, [ref, _doc("later.md", "Later")],
                         synthesizer=_describer(mirror, calls))  # fmt: skip
    assert "concepts/ref/overview.md" in pub.last_change.files  # rebuilt (arrival)
    assert calls == ["call", "call"], "only `later` may call the model"


def test_a_tombstone_referrer_rebuild_reuses_the_record(tmp_path):
    """`keeper` links to `target`. Once `target` is tombstoned, law 2 requires
    `keeper` to be re-synthesized to drop the now-dangling link -- but
    `keeper`'s own source is unchanged, so the describe cache must still hit:
    zero model calls for the rebuild, the record reused as-is."""
    calls: list[str] = []
    mirror = tmp_path / "mirror"
    keeper = _doc("keeper.md", "Keeper", relations=["sys:target.md"])
    target = _doc("target.md", "Target")
    _run_result(tmp_path, [keeper, target], synthesizer=_describer(mirror, calls))
    assert calls == ["call", "call"]
    _, pub = _run_result(
        tmp_path,
        [_doc("target.md", "Target", deleted=True)],
        synthesizer=_describer(mirror, calls),
    )
    # rebuilt (tombstone referrer), link to the removed concept dropped
    assert "concepts/keeper/overview.md" in pub.last_change.files
    assert calls == ["call", "call"], "keeper's record is reused; zero new model calls"


def test_a_tombstone_deletes_the_record(tmp_path):
    mirror = tmp_path / "mirror"
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=_describer(mirror, []))
    _run_result(
        tmp_path, [_doc("a.md", "A", deleted=True)], synthesizer=_describer(mirror, [])
    )
    assert read_described(mirror, "sys:a.md") is None


def test_a_rebuild_without_a_record_clears_the_stale_one(tmp_path):
    mirror = tmp_path / "mirror"
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=_describer(mirror, []))
    _run_result(
        tmp_path, [_doc("a.md", "A", text="changed")], synthesizer=StubSynthesizer()
    )
    assert read_described(mirror, "sys:a.md") is None


def test_a_record_for_another_documents_path_is_not_written(tmp_path):
    class Forging(DescribeSynthesizer):
        def synthesize(self, *a, **k):
            change = super().synthesize(*a, **k)
            for rec in change.described.values():
                rec.doc_id = "sys:someone-else.md"
            return change

    mirror = tmp_path / "mirror"
    base = _describer(mirror, [])
    forging = Forging(base.config, mirror=mirror, agent=base.agent)
    _run_result(tmp_path, [_doc("a.md", "A")], synthesizer=forging)
    assert read_described(mirror, "sys:someone-else.md") is None
    assert read_described(mirror, "sys:a.md") is None


def test_a_failed_publish_writes_no_record(tmp_path):
    class Boom:
        def kbforge_publisher_info(self):
            from kbforge.models import ConnectorInfo

            return ConnectorInfo(name="boom", version="0", source_system="t")

        def kbforge_publish(self, change, config):
            raise RuntimeError("publish failed")

    mirror = tmp_path / "mirror"
    with pytest.raises(RuntimeError, match="publish failed"):
        run(_FakeConnector([_doc("a.md", "A")]), Boom(), config={}, mirror=str(mirror),
            state_dir=str(tmp_path / "state"), publish_config={},
            synthesizer=_describer(mirror, []))  # fmt: skip
    assert read_described(mirror, "sys:a.md") is None
