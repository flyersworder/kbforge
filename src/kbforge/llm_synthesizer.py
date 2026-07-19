"""The grounded LLM synthesizer (spec §4). Optional: requires kbforge[llm]. The
model writes only prose (title/description/body); kbforge owns all structure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from kbforge.models import CanonicalDocument, ChangeSet, ProposedChange
from kbforge.synthesize import assemble

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
    "identifiers that are not in the text."
)


class SynthesizedConcept(BaseModel):
    """The ONLY thing the model produces. Everything structural is kbforge's."""

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)


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

    def _prompt(self, doc: CanonicalDocument, text: str) -> str:
        facets = "\n".join(f"{k}: {v}" for k, v in doc.structured.items())
        return (
            f"Source id: {doc.anchor.native_id}\n"
            f"Source title: {doc.title}\n"
            f"Source facets:\n{facets or '(none)'}\n\n"
            f"Source text:\n{text}"
        )

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        notes: list[str] = []
        for doc in changed_docs:
            text = doc.text
            if len(text) > self.config.max_source_chars:
                text = text[: self.config.max_source_chars]
                notes.append(
                    f"{doc.doc_id}: source truncated to "
                    f"{self.config.max_source_chars} chars before synthesis"
                )
            result = self.agent.run_sync(self._prompt(doc, text))
            c = result.output
            items.append((doc, c.title, c.description, c.body))
        proposal = assemble(items, changeset, existing_paths)
        proposal.summary.grounding_notes.extend(notes)
        return proposal
