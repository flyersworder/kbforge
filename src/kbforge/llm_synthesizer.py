"""The grounded LLM synthesizer (spec §4). Optional: requires kbforge[llm]. The
model writes only prose (title/description/body); kbforge owns all structure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from kbforge.models import CanonicalDocument, ChangeSet, ProposedChange
from kbforge.synthesize import assemble, concept_path

if TYPE_CHECKING:
    # Only for static type-checking (ty/pyright); never imported at runtime, so
    # kbforge[llm] stays optional. `from __future__ import annotations` (above)
    # means these names are never evaluated outside a type checker.
    from pydantic_ai import Agent

_INSTRUCTIONS = (
    "You turn one source document into a knowledge-base concept. Write ONLY from "
    "the provided text; add no outside knowledge and invent no facts. Produce a "
    "concise title, a one-paragraph description, and a clear markdown body that "
    "faithfully summarizes the source. Do not fabricate links, owners, dates, or "
    "identifiers that are not in the text. The body must NOT restate the title as "
    "a heading — kbforge renders the title separately as the document's top-level "
    "heading. The body should begin with the content itself, or with a "
    "section heading (e.g. '## Overview'), but never repeat the document title as "
    "a heading."
)


class SynthesizedConcept(BaseModel):
    """The ONLY thing the model produces. Everything structural is kbforge's."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)

    @field_validator("title", "description", "body")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be non-empty after stripping")
        return v


def actor_for(model: str) -> str:
    """The OKF §7 actor for a model-written concept.

    §7 fixes a two-part `<producer>/<version>` form, and the spec's own example
    (`reference_agent/gemini-2.5-pro`) puts the model in the version slot. Model
    ids are routinely provider-qualified — the default below is
    `deepseek/deepseek-v4-flash` — so interpolating one whole yields a
    three-segment actor that a consumer splitting on `/` reads as the producer
    "kbforge/deepseek". Only the last segment names the model, so only the last
    segment goes in the version slot."""
    return f"kbforge/{model.rsplit('/', 1)[-1]}"


@dataclass
class LLMConfig:
    model: str = "deepseek/deepseek-v4-flash"
    api_base: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    max_tokens: int = 1500
    temperature: float = 0.0
    max_source_chars: int = 24000
    output_mode: str = "tool"

    def validate_env(self) -> list[str]:
        problems: list[str] = []
        if not self.model:
            problems.append("llm 'model' must be non-empty")
        if not os.environ.get(self.api_key_env):
            problems.append(f"env var {self.api_key_env} is not set")
        if self.max_tokens <= 0 or self.max_source_chars <= 0:
            problems.append("max_tokens and max_source_chars must be positive")
        if self.output_mode not in ("tool", "native", "prompted"):
            problems.append("output_mode must be tool, native, or prompted")
        return problems


def _strip_title_heading(body: str, title: str) -> str:
    """Drop a leading markdown heading line whose text equals the concept title —
    kbforge renders the title as the body's H1, so the model echoing it would
    double the heading. Only strips an exact (case-insensitive) title match."""
    stripped = body.lstrip()
    lines = stripped.split("\n", 1)
    first = lines[0].strip()
    if first.startswith("#"):
        heading_text = first.lstrip("#").strip()
        if heading_text.casefold() == title.strip().casefold():
            return lines[1].lstrip() if len(lines) > 1 else ""
    return body


def _wrap_output(mode: str):
    from pydantic_ai import NativeOutput, PromptedOutput

    if mode == "native":
        return NativeOutput(SynthesizedConcept)
    if mode == "prompted":
        return PromptedOutput(SynthesizedConcept)
    return SynthesizedConcept  # tool mode (default)


class LLMSynthesizer:
    def __init__(
        self, config: LLMConfig, *, agent: Agent[Any, Any] | None = None
    ) -> None:
        self.config = config
        self.agent: Agent[Any, Any] = (
            agent if agent is not None else self._build_agent(config)
        )

    @staticmethod
    def _build_agent(config: LLMConfig) -> Agent[Any, Any]:
        try:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.litellm import LiteLLMProvider
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - guarded by the extra
            raise ImportError(
                "LLMSynthesizer requires the LLM extra: pip install 'kbforge[llm]'"
            ) from exc
        model = OpenAIChatModel(
            config.model,
            provider=LiteLLMProvider(
                api_base=config.api_base,
                api_key=os.environ.get(config.api_key_env),
            ),
        )
        return Agent(
            model,
            output_type=_wrap_output(config.output_mode),
            instructions=_INSTRUCTIONS,
            model_settings=ModelSettings(
                temperature=config.temperature, max_tokens=config.max_tokens
            ),
        )

    grounds = True
    """Reads grounding documents and writes a body informed by them (§7)."""

    def _prompt(self, doc: CanonicalDocument, text: str) -> str:
        facets = "\n".join(f"{k}: {v}" for k, v in doc.structured.items())
        return (
            f"Source id: {doc.anchor.native_id}\n"
            f"Source title: {doc.title}\n"
            f"Source facets:\n{facets or '(none)'}\n\n"
            f"Source text:\n{text}"
        )

    def _grounding_block(
        self, docs: list[CanonicalDocument], notes: list[str], owner_id: str
    ) -> str:
        """Related documents from other systems, each labelled with its doc_id so
        the model can attribute a claim to the system it came from."""
        if not docs:
            return ""
        # ONE budget for the whole block, split evenly. `max_source_chars` is
        # documented as the knob that governs prompt size; applied per document
        # it stopped doing that, letting a grounded prompt reach
        # (1 + max_grounding_docs) x max_source_chars — 6x at the defaults, past
        # the context window the knob exists to stay inside. The prompt is now
        # bounded by 2 x max_source_chars however many documents ground it.
        share = max(1, self.config.max_source_chars // len(docs))
        parts = []
        for g in docs:
            text = g.text
            if len(text) > share:
                text = text[:share]
                budget = self.config.max_source_chars
                notes.append(
                    f"{concept_path(owner_id)}: grounding {g.doc_id} truncated to "
                    f"{share} chars before synthesis (a {budget}-char grounding "
                    f"budget shared across {len(docs)} documents)"
                )
            parts.append(f"--- {g.doc_id} ---\n{text}")
        joined = "\n\n".join(parts)
        return (
            "\n\nRelated documents from other systems. Use them for context and "
            "corroboration. Do not treat them as this concept's subject:\n\n"
            f"{joined}"
        )

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
        grounding: dict[str, list[CanonicalDocument]] | None = None,
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        notes: list[str] = []
        grounding = grounding or {}
        for doc in changed_docs:
            text = doc.text
            if len(text) > self.config.max_source_chars:
                text = text[: self.config.max_source_chars]
                notes.append(
                    f"{concept_path(doc.doc_id)}: source truncated to "
                    f"{self.config.max_source_chars} chars before synthesis"
                )
            block = self._grounding_block(
                grounding.get(doc.doc_id, []), notes, doc.doc_id
            )
            result = self.agent.run_sync(self._prompt(doc, text) + block)
            c = result.output
            body = _strip_title_heading(c.body, c.title)
            items.append((doc, c.title, c.description, body))
        proposal = assemble(
            items,
            changeset,
            existing_paths,
            generated_by=actor_for(self.config.model),
            grounding=grounding,
        )
        proposal.summary.grounding_notes.extend(notes)
        return proposal
