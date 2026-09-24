from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

from kbforge.llm_synthesizer import (  # noqa: E402
    _INSTRUCTIONS,
    LLMConfig,
    LLMSynthesizer,
    SynthesizedConcept,
    _strip_title_heading,
)
from kbforge.models import CanonicalDocument, ChangeSet, ResourceAnchor  # noqa: E402
from kbforge.synthesize import GroundingSynthesizer, concept_path  # noqa: E402
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


def _agent_returning(concept: SynthesizedConcept) -> Agent[object, SynthesizedConcept]:
    # For structured output (tool mode), the model must CALL the output tool with
    # the concept as args — not return free text. `info.output_tools[0].name` is the
    # output tool Pydantic AI registered for SynthesizedConcept.
    #
    # Both parameters are spelled out because `Agent`'s type vars carry PEP 696
    # defaults (`object`, `str`), so a bare `Agent` here would claim this returns a
    # plain-text agent rather than a SynthesizedConcept one.
    def fn(messages, info: AgentInfo):
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, concept.model_dump())]
        )

    return Agent(FunctionModel(fn), output_type=SynthesizedConcept)


def _synth(concept: SynthesizedConcept, **cfg) -> LLMSynthesizer:
    return LLMSynthesizer(LLMConfig(**cfg), agent=_agent_returning(concept))


def test_llm_synthesizer_stamps_the_model_derived_actor():
    """Binds the seam, not the helper. `actor_for` and `assemble(generated_by=)`
    were each covered alone, so reverting this call site to interpolate the model
    id whole — reintroducing the three-segment `kbforge/deepseek/deepseek-v4-flash`
    actor — failed no test. The live suite would not catch it either: the gate
    checks that `generated.by` is non-blank, never its §7 shape."""
    doc = _doc()
    concept = SynthesizedConcept(title="X", description="X.", body="X.")
    synth = _synth(concept, model="deepseek/deepseek-v4-flash")

    proposal = synth.synthesize([doc], ChangeSet(added=[doc.doc_id]))

    fm = proposal.concepts[concept_path(doc.doc_id)]
    assert fm.generated_by == "kbforge/deepseek-v4-flash"
    assert fm.generated_by.count("/") == 1  # §7 is <producer>/<version>


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


def test_h2_echoed_title_is_stripped_from_rendered_body():
    doc = _doc()
    title = "Payments API On-Call Runbook"
    concept = SynthesizedConcept(
        title=title,
        description="d",
        body=f"## {title}\n\nReal content about paging the on-call engineer.",
    )
    proposal = _synth(concept).synthesize([doc], ChangeSet(added=[doc.doc_id]))
    rendered = proposal.files[concept_path(doc.doc_id)]
    assert rendered.count(f"# {title}") == 1  # only kbforge's H1 remains
    assert f"## {title}" not in rendered
    assert "Real content about paging the on-call engineer." in rendered


def test_h1_echoed_title_is_stripped_from_rendered_body():
    doc = _doc()
    title = "Payments API On-Call Runbook"
    concept = SynthesizedConcept(
        title=title,
        description="d",
        body=f"# {title}\n\nReal content about paging the on-call engineer.",
    )
    proposal = _synth(concept).synthesize([doc], ChangeSet(added=[doc.doc_id]))
    rendered = proposal.files[concept_path(doc.doc_id)]
    assert rendered.count(f"# {title}") == 1  # only kbforge's H1 remains
    assert "Real content about paging the on-call engineer." in rendered


def test_non_matching_leading_heading_is_preserved():
    doc = _doc()
    title = "Payments API On-Call Runbook"
    concept = SynthesizedConcept(
        title=title,
        description="d",
        body="## Overview\n\nReal content about paging the on-call engineer.",
    )
    proposal = _synth(concept).synthesize([doc], ChangeSet(added=[doc.doc_id]))
    rendered = proposal.files[concept_path(doc.doc_id)]
    assert rendered.count(f"# {title}") == 1
    assert "## Overview" in rendered  # not a title match, so it survives
    assert "Real content about paging the on-call engineer." in rendered


def test_strip_title_heading_unit():
    title = "Payments API On-Call Runbook"
    assert _strip_title_heading(f"## {title}\n\nbody text", title) == "body text"
    assert _strip_title_heading(f"# {title}\n\nbody text", title) == "body text"
    assert (
        _strip_title_heading("## Overview\n\nbody text", title)
        == "## Overview\n\nbody text"
    )


def test_llm_synthesizer_declares_that_it_grounds():
    assert LLMSynthesizer.grounds is True


def test_grounding_text_reaches_the_prompt_and_is_labelled_by_system():
    """The model must be able to tell whose text it is reading, or it cannot
    attribute a claim to the right system in prose."""
    doc = _doc()
    ground = _doc(doc_id="servicenow:SVC0042", text="Escalate to the payments queue.")
    seen: list[str] = []

    def fn(messages, info):
        seen.append(messages[-1].parts[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    SynthesizedConcept(
                        title="X", description="d", body="b"
                    ).model_dump(),
                )
            ]
        )

    synth = LLMSynthesizer(
        LLMConfig(), agent=Agent(FunctionModel(fn), output_type=SynthesizedConcept)
    )
    synth.synthesize(
        [doc], ChangeSet(added=[doc.doc_id]), grounding={doc.doc_id: [ground]}
    )
    assert "servicenow:SVC0042" in seen[0]
    assert "Escalate to the payments queue." in seen[0]


def test_a_grounding_document_is_truncated_like_a_source():
    doc = _doc()
    ground = _doc(doc_id="servicenow:SVC0042", text="x" * 5000)
    concept = SynthesizedConcept(title="X", description="d", body="b")
    synth = _synth(concept, max_source_chars=100)
    proposal = synth.synthesize(
        [doc], ChangeSet(added=[doc.doc_id]), grounding={doc.doc_id: [ground]}
    )
    assert any(
        "servicenow:SVC0042" in n and "truncated" in n
        for n in proposal.summary.grounding_notes
    )


def _takes_grounding_synthesizer(synth: GroundingSynthesizer) -> None:
    """Type-only sink: exists so `ty` fails if `LLMSynthesizer` ever drifts off
    the `GroundingSynthesizer` shape. Nothing declares conformance to that
    protocol, so this is the only check that would catch the drift."""


def test_llm_synthesizer_structurally_conforms_to_grounding_synthesizer():
    concept = SynthesizedConcept(title="X", description="d", body="b")
    _takes_grounding_synthesizer(_synth(concept))


def test_grounding_documents_share_one_budget():
    """`max_source_chars` is documented as the knob that governs prompt size.
    Applied per document it stopped doing that: a grounded prompt grew to
    (1 + max_grounding_docs) times an ungrounded one, 6x at the defaults."""
    doc = _doc(text="Q" * 5000)
    grounds = [_doc(doc_id=f"servicenow:SVC{i}", text="Z" * 5000) for i in range(1, 4)]
    seen: list[str] = []

    def fn(messages, info):
        seen.append(messages[-1].parts[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    SynthesizedConcept(
                        title="X", description="d", body="b"
                    ).model_dump(),
                )
            ]
        )

    synth = LLMSynthesizer(
        LLMConfig(max_source_chars=300),
        agent=Agent(FunctionModel(fn), output_type=SynthesizedConcept),
    )
    synth.synthesize(
        [doc], ChangeSet(added=[doc.doc_id]), grounding={doc.doc_id: grounds}
    )
    assert seen[0].count("Q") == 300  # the owning source keeps the full budget
    assert seen[0].count("Z") == 300  # the three grounding documents share one


# --- #35: a truncated or invalid model output --------------------------------

from pydantic_ai.usage import RequestUsage  # noqa: E402

from kbforge.llm_synthesizer import SynthesisError  # noqa: E402

_GOOD = {
    "title": "X",
    "description": "About X.",
    "body": "## Overview\n\nX does things.",
}
_EMPTY_BODY = {"title": "X", "description": "About X.", "body": ""}


def _scripted(outputs: list[tuple[dict, int]]) -> FunctionModel:
    """Answers each attempt with the next (tool args, output_tokens) pair."""
    calls = iter(outputs)

    def fn(messages, info: AgentInfo):
        args, tokens = next(calls)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)],
            usage=RequestUsage(output_tokens=tokens),
        )

    return FunctionModel(fn)


def _synth_with(outputs, **cfg) -> LLMSynthesizer:
    config = LLMConfig(**cfg)
    return LLMSynthesizer(
        config, agent=LLMSynthesizer._build_agent(config, model=_scripted(outputs))
    )


def test_the_default_output_budget_fits_a_long_source():
    """The root cause of #35: a 24,000-char source needed ~1,800 output tokens,
    and the 1,500 default truncated the tool call inside `body` every time."""
    assert LLMConfig().max_tokens >= 4096


def test_output_truncated_at_max_tokens_is_named_as_such():
    synth = _synth_with([(_EMPTY_BODY, 1500)] * 3, max_tokens=1500)
    with pytest.raises(SynthesisError) as err:
        synth.synthesize([_doc()], ChangeSet(added=["local_files:apps/x.md"]))
    message = str(err.value)
    assert concept_path("local_files:apps/x.md") in message
    assert "deepseek/deepseek-v4-flash" in message
    assert "max_tokens=1500" in message and "--llm-set max_tokens=" in message, message


def test_invalid_output_below_the_budget_says_invalid_not_truncated():
    synth = _synth_with([(_EMPTY_BODY, 200)] * 3, max_tokens=1500)
    with pytest.raises(SynthesisError) as err:
        synth.synthesize([_doc()], ChangeSet(added=["local_files:apps/x.md"]))
    message = str(err.value)
    assert "invalid output" in message and "3 attempts" in message, message
    assert "max_tokens" not in message


def test_invalid_output_names_the_field_without_echoing_large_values():
    """A pydantic `missing` error's `input` is the whole *remaining* args
    dict, not just the failing field — so a tool call missing `body` reports
    `input: {title: ..., description: ...}`. `RetryPromptPart.model_response()`
    would dump that whole dict into `SynthesisError`; the retry reason must
    name what failed (`body`) without repeating any of the model's own field
    values. A distinctive marker planted in `title` stands in for anything
    large or sensitive the model wrote that must not leak."""
    marker = "MARKER" * 50
    bad = {"title": marker, "description": "About X."}  # `body` missing
    synth = _synth_with([(bad, 200)] * 3, max_tokens=1500)
    with pytest.raises(SynthesisError) as err:
        synth.synthesize([_doc()], ChangeSet(added=["local_files:apps/x.md"]))
    message = str(err.value)
    assert marker not in message
    assert "body" in message


def test_a_bad_output_is_retried_within_the_budget():
    synth = _synth_with([(_EMPTY_BODY, 200), (_EMPTY_BODY, 200), (_GOOD, 200)])
    change = synth.synthesize([_doc()], ChangeSet(added=["local_files:apps/x.md"]))
    assert "X does things." in change.files[concept_path("local_files:apps/x.md")]


def test_output_retries_is_configurable_and_positive():
    assert LLMConfig().output_retries == 2
    assert "output_retries must be >= 0" in LLMConfig(output_retries=-1).validate_env()


def _prompt_seen(**cfg) -> str:
    """The instructions the model actually received through a built agent."""
    seen: list[str] = []

    def fn(messages, info: AgentInfo):
        seen.append(messages[-1].instructions or "")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, _GOOD)])

    config = LLMConfig(**cfg)
    synth = LLMSynthesizer(
        config, agent=LLMSynthesizer._build_agent(config, model=FunctionModel(fn))
    )
    synth.synthesize([_doc()], ChangeSet(added=["local_files:apps/x.md"]))
    return seen[0]


def test_instructions_are_appended_to_the_fixed_prompt():
    """`instructions` extends the prompt; it cannot replace the part that says
    to write only from the provided text (#44)."""
    prompt = _prompt_seen(instructions="  Cite every claim as [n].  ")
    assert prompt.startswith(_INSTRUCTIONS), prompt
    assert prompt.endswith("\n\nCite every claim as [n]."), prompt


def test_blank_instructions_leave_the_fixed_prompt_alone():
    assert _prompt_seen() == _INSTRUCTIONS
    assert _prompt_seen(instructions="   ") == _INSTRUCTIONS


def test_an_empty_instructions_value_means_none():
    """`--llm-set instructions=` is YAML `None`: a wrapper passing an unset
    `$EXTRA` must run with no extra guidance, not exit 2 (#44 review)."""
    assert LLMConfig(instructions=None).validate_env() == LLMConfig().validate_env()  # ty: ignore[invalid-argument-type]
    assert _prompt_seen(instructions=None) == _INSTRUCTIONS
