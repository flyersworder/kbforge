from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.llm_synthesizer import (  # noqa: E402
    LLMConfig,
    LLMSynthesizer,
    SynthesizedConcept,
)
from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor  # noqa: E402
from kbforge.synthesize import concept_path  # noqa: E402
from kbforge.validate import run_validators  # noqa: E402


def _doc(doc_id="local_files:apps/x.md", text="X does things.", relations=None):
    anchor = ResourceAnchor(
        system="local_files",
        native_id=doc_id.split(":", 1)[1],
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="h",
    )
    return CanonicalDocument(
        anchor=anchor, doc_id=doc_id, title="X", text=text, relations=relations or []
    )


def _agent_returning(concept: SynthesizedConcept) -> Agent:
    # For structured output (tool mode), the model must CALL the output tool with
    # the concept as args — not return free text. `info.output_tools[0].name` is the
    # output tool Pydantic AI registered for SynthesizedConcept.
    def fn(messages, info: AgentInfo):
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, concept.model_dump())]
        )

    return Agent(FunctionModel(fn), output_type=SynthesizedConcept)


def _synth(concept: SynthesizedConcept, **cfg) -> LLMSynthesizer:
    return LLMSynthesizer(LLMConfig(**cfg), agent=_agent_returning(concept))


def test_llm_output_becomes_conformant_concept():
    doc = _doc()
    concept = SynthesizedConcept(
        title="Checkout", description="What checkout does.", body="A clean summary."
    )
    proposal = _synth(concept).synthesize([doc], ChangeSet(added=[doc.doc_id]))
    path = concept_path(doc.doc_id)
    assert "A clean summary." in proposal.files[path]
    assert "# Checkout" in proposal.files[path]
    existing = frozenset({path})
    assert run_validators(proposal, existing) == []  # passes the §4.4 gate


def test_llm_cannot_emit_links_or_anchors_only_kbforge_can():
    # The model 'claims' a link in prose; no structural link may appear — the frame
    # is kbforge-owned. The doc has a real relation, which DOES resolve structurally.
    doc = _doc(relations=["local_files:apps/y.md"])
    concept = SynthesizedConcept(
        title="X", description="d", body="See [Y](concepts/apps/z/overview.md)."
    )
    other = concept_path("local_files:apps/y.md")
    proposal = _synth(concept).synthesize(
        [doc],
        ChangeSet(added=[doc.doc_id]),
        frozenset({concept_path(doc.doc_id), other}),
    )
    fm = proposal.concepts[concept_path(doc.doc_id)]
    assert fm.links == [other]  # only the real relation, resolved by kbforge
    assert "concepts/apps/z/overview.md" not in fm.links  # prose claim ignored


def test_oversized_source_is_truncated_and_flagged():
    doc = _doc(text="x" * 5000)
    concept = SynthesizedConcept(title="X", description="d", body="b")
    synth = _synth(concept, max_source_chars=100)
    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))
    assert any("truncated" in n for n in proposal.summary.grounding_notes)


def test_empty_prose_is_rejected_by_schema():
    with pytest.raises(Exception):
        SynthesizedConcept(title="", description="d", body="b")


def test_whitespace_only_body_is_rejected_by_schema():
    # "   " passes min_length=1 but must still fail: title/description are
    # backstopped by strict-OKF downstream, but body is not in the frontmatter, so
    # a whitespace-only body would otherwise publish a near-empty concept.
    with pytest.raises(ValidationError):
        SynthesizedConcept(title="T", description="D", body="   ")


def test_validate_config_reports_missing_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    problems = LLMConfig().validate_env()
    assert problems and "OPENROUTER_API_KEY" in problems[0]
