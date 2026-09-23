from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

pytest.importorskip("pydantic_ai")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.described import write_described  # noqa: E402
from kbforge.llm_synthesizer import (  # noqa: E402
    DescribeConfig,
    DescribeSynthesizer,
    SynthesisError,
)
from kbforge.models import (  # noqa: E402
    CanonicalDocument,
    ChangeSet,
    DescribedRecord,
    ResourceAnchor,
)
from kbforge.synthesize import StubSynthesizer, concept_path  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402

VOCAB = {"sic": ["SiC"], "800v": ["800 V"], "gan": []}
DOC_ID = "sys:q3.md"
PATH = concept_path(DOC_ID)


def _doc(text="SiC MOSFETs at 800 V.", structured=None, h="h1"):
    return CanonicalDocument(
        anchor=ResourceAnchor(system="sys", native_id="q3.md",
                              retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                              content_hash=h),
        doc_id=DOC_ID, title="Q3 report", text=text, structured=structured or {},
    )  # fmt: skip


def _model(outputs: list[dict], calls: list[int]) -> FunctionModel:
    it = iter(outputs)

    def fn(messages, info: AgentInfo):
        calls.append(1)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, next(it))])

    return FunctionModel(fn)


def _synth(outputs, calls, mirror=None, **cfg) -> DescribeSynthesizer:
    cfg.setdefault("tags_vocabulary", VOCAB)
    config = DescribeConfig(**cfg)
    agent = DescribeSynthesizer._build_agent(config, model=_model(outputs, calls))
    return DescribeSynthesizer(config, mirror=mirror, agent=agent)


def _front(text: str) -> dict:
    return yaml.safe_load(text.split("---\n")[1])


def _run(synth, doc=None):
    doc = doc or _doc()
    return synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))


GOOD = {
    "description": "A Q3 report on SiC MOSFETs for 800 V platforms.",
    "tags": ["gan"],
}


def test_body_is_byte_identical_to_the_stub():
    doc = _doc()
    stub = StubSynthesizer().synthesize([doc], ChangeSet(added=[DOC_ID])).files[PATH]
    got = _run(_synth([GOOD], []), doc).files[PATH]
    assert got.split("\n---\n", 1)[1] == stub.split("\n---\n", 1)[1]


def test_description_actor_and_tag_union():
    change = _run(_synth([GOOD], []), _doc(structured={"tags": ["roadmap"]}))
    front = _front(change.files[PATH])
    assert front["description"] == GOOD["description"]
    assert front["generated"]["by"] == "kbforge/deepseek-v4-flash"
    # source ∪ keyword (sic, 800v) ∪ model (gan)
    assert front["tags"] == ["800v", "gan", "roadmap", "sic"]
    assert change.described[PATH].tags == ["gan"]  # model tags only
    assert run_validators(change) == []


def test_a_tag_outside_the_vocabulary_retries_then_names_the_tag():
    bad = {"description": "Fine.", "tags": ["SiC"]}  # case variant of `sic`
    with pytest.raises(SynthesisError) as err:
        _run(_synth([bad] * 3, []))
    assert PATH in str(err.value) and "'SiC'" in str(err.value), str(err.value)


def test_a_bad_tag_then_a_good_answer_succeeds():
    calls: list[int] = []
    change = _run(_synth([{"description": "Fine.", "tags": ["nope"]}, GOOD], calls))
    assert len(calls) == 2 and change.described[PATH].description == GOOD["description"]


def test_duplicate_model_tags_collapse():
    change = _run(_synth([{"description": "Fine.", "tags": ["gan", "gan"]}], []))
    assert change.described[PATH].tags == ["gan"]


@pytest.mark.parametrize(
    "description, why, expect",
    [
        ("", "blank", "empty"),
        ("Two\nlines.", "multi-line", "ONE sentence on one line"),
        (
            "Two\rlines.",
            "carriage return is a line break too",
            "ONE sentence on one line",
        ),
        ("x" * 241, "over the cap", "241 chars"),
    ],
)
def test_a_bad_description_retries_then_fails(description, why, expect):
    with pytest.raises(SynthesisError) as err:
        _run(_synth([{"description": description, "tags": []}] * 3, []))
    assert PATH in str(err.value) and expect in str(err.value), why


def test_a_description_at_exactly_the_cap_is_accepted():
    at_cap = "x" * 240
    change = _run(_synth([{"description": at_cap, "tags": []}], []))
    assert _front(change.files[PATH])["description"] == at_cap


def test_model_tags_off_means_no_tag_request_and_keyword_tags_still_apply():
    change = _run(_synth([{"description": "Fine.", "tags": []}], [], model_tags=False))
    assert _front(change.files[PATH])["tags"] == ["800v", "sic"]


def test_yaml_special_description_still_validates():
    tricky = {
        "description": "Key: value # not a comment, 'quoted' --- still one line.",
        "tags": [],
    }
    change = _run(_synth([tricky], []))
    assert _front(change.files[PATH])["description"] == tricky["description"]
    assert run_validators(change) == []


def test_a_cache_hit_makes_no_model_call_and_keeps_the_stored_actor(tmp_path: Path):
    write_described(tmp_path, DescribedRecord(doc_id=DOC_ID, content_hash="h1",
                    actor="kbforge/old-model", description="Cached.", tags=["gan"]))  # fmt: skip  # noqa: E501
    calls: list[int] = []
    change = _run(_synth([], calls, mirror=tmp_path))
    front = _front(change.files[PATH])
    assert calls == []
    assert (
        front["description"] == "Cached."
        and front["generated"]["by"] == "kbforge/old-model"
    )


@pytest.mark.parametrize(
    "record_hash, record_tags, record_description, why",
    [
        ("h0", ["gan"], "Old.", "the source changed"),
        ("h1", ["dropped"], "Old.", "vocabulary narrowed"),
        ("h1", ["gan"], "x" * 241, "the cached description is over the cap"),
    ],
)
def test_a_stale_cache_is_a_miss(
    tmp_path: Path, record_hash, record_tags, record_description, why
):
    write_described(tmp_path, DescribedRecord(doc_id=DOC_ID, content_hash=record_hash,
                    actor="kbforge/m", description=record_description, tags=record_tags))  # fmt: skip  # noqa: E501
    calls: list[int] = []
    change = _run(_synth([GOOD], calls, mirror=tmp_path))
    assert calls == [1], why
    assert change.described[PATH].description == GOOD["description"]


def test_describe_does_not_ground():
    assert DescribeSynthesizer.grounds is False
