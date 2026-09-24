"""The grounded LLM synthesizer (spec §4). Optional: requires kbforge[llm]. The
model writes only prose (title/description/body); kbforge owns all structure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from kbforge.described import read_described
from kbforge.models import CanonicalDocument, ChangeSet, DescribedRecord, ProposedChange
from kbforge.synthesize import assemble, concept_path
from kbforge.tagging import keyword_tags

if TYPE_CHECKING:
    # Only for static type-checking (ty/pyright); never imported at runtime, so
    # kbforge[llm] stays optional. `from __future__ import annotations` (above)
    # means these names are never evaluated outside a type checker.
    from pydantic_ai import Agent
    from pydantic_ai.messages import RetryPromptPart
    from pydantic_ai.models import Model


class SynthesisError(RuntimeError):
    """The model gave no usable concept for one document. Raised before anything
    is published, so the mirror and cursor stay put and the next run retries."""


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
    # Output tokens per concept. 1500 truncated the tool call inside `body` for
    # a long source (~1,800 needed at max_source_chars), and the provider still
    # reported finish_reason=tool_call, so nothing said why (#35).
    max_tokens: int = 4096
    temperature: float = 0.0
    max_source_chars: int = 24000
    output_mode: str = "tool"
    # Extra attempts after an output fails validation: for a genuinely flaky
    # model. They do not help a truncated one, which fails the same way again.
    output_retries: int = 2
    # Appended to the fixed prompt, never replacing it. Not part of any cache or
    # drift key: editing it alone re-synthesizes nothing (architecture.md §7.3).
    instructions: str = ""

    def validate_env(self) -> list[str]:
        problems: list[str] = []
        if not self.model:
            problems.append("llm 'model' must be non-empty")
        if not os.environ.get(self.api_key_env):
            problems.append(f"env var {self.api_key_env} is not set")
        if self.max_tokens <= 0 or self.max_source_chars <= 0:
            problems.append("max_tokens and max_source_chars must be positive")
        if self.output_retries < 0:
            problems.append("output_retries must be >= 0")
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


def _retry_reason(part: RetryPromptPart) -> str:
    """The last retry's problem, compact and never carrying `input` — a
    validation error's `input` echoes the model's own field value back
    (potentially the whole body it wrote), which `SynthesisError` must not
    repeat. `content` is either a `ModelRetry` message (a plain `str`, used
    as-is) or a list of pydantic `ErrorDetails` (reduced to `loc: msg`)."""
    if isinstance(part.content, str):
        return part.content
    return "; ".join(
        f"{'.'.join(str(x) for x in e.get('loc', ())) or '?'}: "
        f"{e.get('msg', '(no message)')}"
        for e in part.content
    )


def _run_agent(
    agent: Agent[Any, Any], config: LLMConfig, doc: CanonicalDocument, prompt: str
) -> Any:
    """Shared by both LLM synthesizers: run the agent once, and on exhausted
    retries name where synthesis failed and why (truncated vs. invalid), so a
    consumer reading `SynthesisError` knows which knob to turn."""
    from pydantic_ai import capture_run_messages
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart

    with capture_run_messages() as messages:
        try:
            return agent.run_sync(prompt).output
        except UnexpectedModelBehavior as exc:
            attempts = [m for m in messages if isinstance(m, ModelResponse)]
            where = f"{concept_path(doc.doc_id)} ({config.model})"
            last = attempts[-1].usage.output_tokens if attempts else 0
            # The provider reports finish_reason=tool_call even when it cut
            # the output off, so a response that used the whole budget is
            # the only reliable sign of truncation.
            if last >= config.max_tokens:
                reason = (
                    f"model output hit max_tokens={config.max_tokens} "
                    "and was cut off; raise it with "
                    f"--llm-set max_tokens={2 * config.max_tokens}"
                )
            else:
                retries = [
                    p
                    for m in messages
                    if isinstance(m, ModelRequest)
                    for p in m.parts
                    if isinstance(p, RetryPromptPart)
                ]
                # The last retry prompt says what was wrong (a tag outside the
                # vocabulary, a description over the cap); `exc` only says
                # retries ran out.
                why = f"; last problem: {_retry_reason(retries[-1])}" if retries else ""
                reason = (
                    f"model returned invalid output in {len(attempts)} "
                    f"attempts: {exc}{why}"
                )
            raise SynthesisError(f"{where}: {reason}") from exc


class LLMSynthesizer:
    def __init__(
        self, config: LLMConfig, *, agent: Agent[Any, Any] | None = None
    ) -> None:
        self.config = config
        self.agent: Agent[Any, Any] = (
            agent if agent is not None else self._build_agent(config)
        )

    @staticmethod
    def _build_agent(config: LLMConfig, model: Model | None = None) -> Agent[Any, Any]:
        """`model` is for tests: a scripted model exercises the real agent,
        retries included, without a network call."""
        try:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.litellm import LiteLLMProvider
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - guarded by the extra
            raise ImportError(
                "LLMSynthesizer requires the LLM extra: pip install 'kbforge[llm]'"
            ) from exc
        if model is None:
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
            retries=config.output_retries,
            instructions=_with_instructions(_INSTRUCTIONS, config),
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

    def _run(self, doc: CanonicalDocument, prompt: str) -> SynthesizedConcept:
        return _run_agent(self.agent, self.config, doc, prompt)

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
            c = self._run(doc, self._prompt(doc, text) + block)
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


def _with_instructions(fixed: str, config: LLMConfig) -> str:
    extra = config.instructions.strip()
    return f"{fixed}\n\n{extra}" if extra else fixed


_DESCRIBE_INSTRUCTIONS = (
    "You describe one source document for a knowledge-base index. Write ONLY "
    "from the provided text; add no outside knowledge and invent no facts. "
    "Return `description`: ONE sentence, on one line, saying what the document "
    "is and what it covers, so a reader can decide whether to open it."
)


@dataclass
class DescribeConfig(LLMConfig):
    tags_vocabulary: dict[str, list[str]] | None = None
    model_tags: bool = True
    description_max_chars: int = 240

    def validate_env(self) -> list[str]:
        """Reports problems, never raises: `--llm-set` values are YAML-typed
        (`kbforge.__main__._parse_settings`), so `tags_vocabulary` can arrive
        as anything -- a bare scalar (`tags_vocabulary=sic`), or a dict with a
        non-string key (`tags_vocabulary={2024: [x]}`). Either used to reach
        the model config unchecked and crash later: a scalar raised
        AttributeError right here (`.items()` on a `str`), and a non-string
        key passed this check silently and then blew up in
        `_describe_instructions`'s `", ".join(sorted(allowed_tags))` or
        `assemble`'s tag sort -- both require every tag to be a `str`."""
        problems = super().validate_env()
        if self.description_max_chars <= 0:
            problems.append("description_max_chars must be positive")
        vocab = self.tags_vocabulary
        if vocab is not None and not isinstance(vocab, dict):
            problems.append("tags_vocabulary must be a mapping of tag to phrase list")
        else:
            for tag, phrases in (vocab or {}).items():
                if not isinstance(tag, str) or not tag.strip():
                    problems.append(
                        f"tags_vocabulary has a non-string or blank tag: {tag!r}"
                    )
                    continue
                if not isinstance(phrases, list) or not all(
                    isinstance(p, str) and p.strip() for p in phrases
                ):
                    problems.append(
                        f"tags_vocabulary[{tag!r}] must be a list of non-blank phrases"
                    )
        return problems

    @property
    def allowed_tags(self) -> frozenset[str]:
        """The tags the MODEL may return; empty when it may return none."""
        if not (self.tags_vocabulary and self.model_tags):
            return frozenset()
        return frozenset(self.tags_vocabulary)


class DescribedConcept(BaseModel):
    """The ONLY thing the describing model produces."""

    description: str
    tags: list[str] = Field(default_factory=list)


def _description_problem(text: str, cap: int) -> str | None:
    """What is wrong with a description, or None. Shared by `_check` (a fresh
    model answer) and `_cached` (a stored one) so a description that would
    fail one fails the other — a cache hit ships a description no run of the
    model could actually produce, and lowering `description_max_chars` must
    retire descriptions written under a looser cap rather than keep shipping
    them unchecked."""
    if not text:
        return "`description` is empty; write one sentence."
    if len(text.splitlines()) > 1:
        return "`description` must be ONE sentence on one line."
    if len(text) > cap:
        return f"`description` is {len(text)} chars; keep it at most {cap}."
    return None


def _describe_instructions(config: DescribeConfig) -> str:
    parts = [_DESCRIBE_INSTRUCTIONS]
    if config.allowed_tags:
        parts.append(
            "Return `tags`: the tags from this list that apply, spelled exactly "
            f"as listed, or none: {', '.join(sorted(config.allowed_tags))}."
        )
    else:
        parts.append("Return `tags` as an empty list.")
    return _with_instructions("\n\n".join(parts), config)


class DescribeSynthesizer:
    """Stub body, model-written one-sentence description, keyword + model tags
    (architecture.md §7.3)."""

    grounds = False
    """The description is written from the document alone; citing grounding
    documents would claim a provenance the concept does not have (§7.1)."""

    def __init__(
        self,
        config: DescribeConfig,
        *,
        mirror: Path | None = None,
        agent: Agent[Any, Any] | None = None,
    ) -> None:
        """`mirror` must be the same path passed to `pipeline.run(mirror=...)`:
        it is where `_cached` reads `_described/` records from. With
        `mirror=None` this synthesizer never reads the cache -- every document
        is a miss -- but the pipeline still writes `_described/` regardless,
        silently, rather than erroring. The CLI wires the two together."""
        self.config = config
        self.mirror = mirror
        self.agent: Agent[Any, Any] = (
            agent if agent is not None else self._build_agent(config)
        )
        # Registered here, not in `_build_agent`, so an injected agent is held
        # to the same checks as a built one.
        self.agent.output_validator(self._check)

    @staticmethod
    def _build_agent(
        config: DescribeConfig, model: Model | None = None
    ) -> Agent[Any, Any]:
        try:
            from pydantic_ai import Agent
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.litellm import LiteLLMProvider
            from pydantic_ai.settings import ModelSettings
        except ImportError as exc:  # pragma: no cover - guarded by the extra
            raise ImportError(
                "DescribeSynthesizer requires the LLM extra: pip install 'kbforge[llm]'"
            ) from exc
        if model is None:
            model = OpenAIChatModel(
                config.model,
                provider=LiteLLMProvider(
                    api_base=config.api_base,
                    api_key=os.environ.get(config.api_key_env),
                ),
            )
        output: Any = DescribedConcept
        if config.output_mode != "tool":
            from pydantic_ai import NativeOutput, PromptedOutput

            wrap = NativeOutput if config.output_mode == "native" else PromptedOutput
            output = wrap(DescribedConcept)
        return Agent(
            model,
            output_type=output,
            retries=config.output_retries,
            instructions=_describe_instructions(config),
            model_settings=ModelSettings(
                temperature=config.temperature, max_tokens=config.max_tokens
            ),
        )

    def _check(self, out: DescribedConcept) -> DescribedConcept:
        from pydantic_ai import ModelRetry

        text = out.description.strip()
        problem = _description_problem(text, self.config.description_max_chars)
        if problem:
            raise ModelRetry(problem)
        allowed = self.config.allowed_tags
        bad = sorted({t for t in out.tags if t not in allowed})
        if bad:
            listed = ", ".join(sorted(allowed)) or "(none: return an empty list)"
            raise ModelRetry(f"tags {bad} are not allowed; choose only from: {listed}")
        return DescribedConcept(description=text, tags=sorted(set(out.tags)))

    def _cached(self, doc: CanonicalDocument) -> DescribedRecord | None:
        if self.mirror is None:
            return None
        record = read_described(self.mirror, doc.doc_id)
        if (
            record is None
            or record.content_hash != doc.anchor.content_hash
            or not set(record.tags) <= self.config.allowed_tags
            or _description_problem(
                record.description, self.config.description_max_chars
            )
            is not None
        ):
            return None
        return record

    def _prompt(self, doc: CanonicalDocument, notes: list[str]) -> str:
        text = doc.text
        if len(text) > self.config.max_source_chars:
            text = text[: self.config.max_source_chars]
            notes.append(
                f"{concept_path(doc.doc_id)}: source truncated to "
                f"{self.config.max_source_chars} chars before description"
            )
        return f"Source title: {doc.title}\n\nSource text:\n{text}"

    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange:
        items: list[tuple[CanonicalDocument, str, str, str]] = []
        tags: dict[str, list[str]] = {}
        actors: dict[str, str] = {}
        records: dict[str, DescribedRecord] = {}
        notes: list[str] = []
        vocabulary = self.config.tags_vocabulary
        for doc in changed_docs:
            record = self._cached(doc)
            if record is None:
                out = _run_agent(self.agent, self.config, doc, self._prompt(doc, notes))
                record = DescribedRecord(
                    doc_id=doc.doc_id,
                    content_hash=doc.anchor.content_hash,
                    actor=actor_for(self.config.model),
                    description=out.description,
                    tags=out.tags,
                )
            items.append((doc, doc.title, record.description, doc.text))
            tags[doc.doc_id] = sorted(
                set(record.tags) | set(keyword_tags(vocabulary, doc.title, doc.text))
            )
            actors[doc.doc_id] = record.actor
            records[concept_path(doc.doc_id)] = record
        proposal = assemble(items, changeset, existing_paths, tags=tags, actors=actors)
        proposal.described = records
        proposal.summary.grounding_notes.extend(notes)
        return proposal
