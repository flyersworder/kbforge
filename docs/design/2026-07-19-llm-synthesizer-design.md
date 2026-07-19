# kbforge — The LLM Synthesizer

**Status:** Draft v0.1 · **Amends:** [`../architecture.md`](../architecture.md) §7 (the
`synthesize` stage) · **Sub-project of:** Threshold 2

## 1. Problem

`synthesize` is the one pipeline stage that is meant to *think*, and today it does
not. The shipped `synthesize()` is a deterministic stub: it reshapes a
`CanonicalDocument` into an OKF concept, copies the body verbatim, and sets
`title`/`description` to the filename. It exists to prove the pipeline wiring and to
give the §4.4 validators real structure to check — nothing more.

This sub-project replaces the stub's *prose* with a real, grounded LLM synthesizer,
while leaving every trust guarantee and every deterministic seam around it intact.
It is one of three Threshold-2 sub-projects (the others — a GitHub-PR publisher and a
credentialed connector — are separate specs); it is fully in-core and independent of
them.

## 2. The load-bearing principle — the LLM writes prose inside a kbforge-owned frame

Grounding is not a prompt aspiration here; it is enforced by *what the model is
allowed to produce*. The synthesizer's LLM output type carries exactly three fields:

```
title        # a concise concept title
description  # one-paragraph summary
body         # the concept prose (markdown)
```

Everything structural — the `resource` anchors, the `links`, the surviving `facets`,
the `type`, the `timestamp` — is assembled **deterministically by kbforge**, from the
`CanonicalDocument`, exactly as the stub does today. The model never emits an anchor,
never invents a link, never chooses a type. Consequences:

- **The §4.4 laws stay fully enforced on structure the model cannot touch.** Anchor
  presence (law 3), link resolvability (law 2), facet well-formedness (law 1), and
  freshness (law 4) are all about kbforge-owned frame, so a hallucinating model
  cannot produce a structurally non-conformant concept.
- **The grounding contract is unchanged from the architecture** (§4.1, §4.4 law 3):
  synthesis reads only canonical docs — never raw payloads — and the prose is
  instructed to draw only on the provided canonical text. Prose faithfulness itself
  is *trusted*, not verified, in this version (a faithfulness judge is deferred — §11).
- **Non-conformant output is caught, not shipped.** The existing `validate` stage
  gates the assembled `ProposedChange`; a failure returns `Aborted`, and no MR opens.

## 3. The `Synthesizer` seam

`synthesize()` becomes an object behind a one-method protocol, so the stub and the
LLM implementation are interchangeable and the deterministic path survives for tests.

```python
class Synthesizer(Protocol):
    def synthesize(
        self,
        changed_docs: list[CanonicalDocument],
        changeset: ChangeSet,
        existing_paths: frozenset[str] = frozenset(),
    ) -> ProposedChange: ...
```

- `StubSynthesizer` — today's `synthesize()` logic verbatim, wrapped in the protocol.
  Remains the default, so every existing test and the credential-free walking
  skeleton keep working unchanged.
- `LLMSynthesizer` — the new implementation (§4).
- `pipeline.run(...)` gains a keyword param `synthesizer: Synthesizer =
  StubSynthesizer()`. Nothing else in `run` changes: it still gates on `is_noop`
  before calling the synthesizer, still passes `existing_paths`, still runs
  `run_validators` on the result, still commits the mirror and cursor only after a
  successful publish.

The shared assembly helpers (`concept_path`, `_facets`, the frame-building portion of
`_render`) are extracted so both synthesizers build the kbforge-owned frame
identically; only the prose source differs.

**LLM non-determinism is safe by construction.** Change detection runs *upstream* of
synthesis: `diff` compares against the mirror of *canonical* docs, and the mirror
stores canonical docs — never synthesized prose. Unchanged input is a `NoOp` before
the synthesizer is called, so a run only synthesizes docs that genuinely changed. A
non-deterministic synthesizer therefore never manufactures a spurious diff, and
`synthesize` carries no `assert_stability` obligation (that law binds the connector's
`normalize`, not this stage).

## 4. `LLMSynthesizer`

Built on **Pydantic AI**, which gives schema-validated output, automatic re-prompting
on validation failure, and a first-class offline testing story — and is idiomatic in
this Pydantic-v2 codebase.

```python
class SynthesizedConcept(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    body: str = Field(min_length=1)
```

The `min_length` constraints turn empty prose into a Pydantic AI *validation* failure
— which re-prompts the model — instead of a late §4.4 `Aborted` (the strict-OKF check
already requires non-empty `title` and `description`). Catch it at the cheap layer.

Per changed doc (1 canonical doc → 1 concept):

1. Build a prompt from that one `CanonicalDocument`: its `title`, `text` (truncated to
   `max_source_chars` if needed — §7), the `structured` facets, and the source
   `native_id`. The system `instructions` constrain the model: write only from the
   provided text, add no outside knowledge, no citations the frame does not carry.
2. `agent.run_sync(prompt)` → a validated `SynthesizedConcept`.
3. Assemble the `ProposedChange` frame deterministically (anchors, resolved links via
   `existing_paths`, surviving facets, `type`, `timestamp`) using the LLM's
   `title`/`description`/`body` for prose only.
4. Populate `ChangeSummary` (added/modified/removed) and any `grounding_notes` /
   `gaps_flagged` raised during the run (e.g. a truncated source).

The `Agent` is constructed once per `LLMSynthesizer` with `output_type=
SynthesizedConcept` (wrapped per the configured `output_mode` — §5) and the grounding
`instructions`.

## 5. Model & provider configuration

The model is reached through Pydantic AI's `LiteLLMProvider`, so a self-hosted LiteLLM
gateway and OpenRouter are the **same code path** — an OpenAI-compatible base URL plus
a key.

| Config key | Meaning | Default |
|---|---|---|
| `model` | LiteLLM/OpenRouter model slug | `deepseek/deepseek-v4-flash` |
| `api_base` | gateway/OpenRouter base URL | `https://openrouter.ai/api/v1` |
| `api_key_env` | name of the env var holding the key (never the value) | `OPENROUTER_API_KEY` |
| `max_tokens` | per-concept output cap | (a sane default, e.g. 1500) |
| `temperature` | sampling temperature | `0` |
| `max_source_chars` | truncate oversized canonical text | (e.g. 24000) |
| `output_mode` | Pydantic AI structured-output strategy: `tool` / `native` / `prompted` | `tool` |

- The key is **always** read from the named env var, never stored in config
  (architecture's `token_env_var` pattern). `.env` is gitignored; `.env.example`
  documents `OPENROUTER_API_KEY`.
- A **self-hosted LiteLLM gateway** is configured by overriding `api_base` (to the
  gateway URL) and `api_key_env` (to the var holding the master key), and naming a
  `model` the gateway serves. No code difference from the OpenRouter default.
- **Why `LiteLLMProvider`, not plain `OpenAIProvider`** (both speak OpenAI-compatible
  endpoints): it honors the architecture's LiteLLM commitment, maps model-name
  prefixes to per-provider profiles, and keeps a path open to non-OpenAI-compatible
  providers later without reworking the seam.
- **`output_mode` exists because tool-calling is uneven on cheap models.** Pydantic AI
  defaults to tool-call structured output; a small model like `deepseek-v4-flash` may
  do better with `native` (JSON-schema `response_format`) or `prompted` (schema in the
  instructions). Making it config avoids hard-coding a strategy the default model
  might handle poorly — to be pinned during the first live runs (§13).
- `validate_config`-style checks: `model` non-empty; the env named by `api_key_env`
  is set; `max_tokens`/`max_source_chars` positive ints; `output_mode` one of the three.

## 6. Dependency hygiene

Pydantic AI (with LiteLLM) is a heavy dependency and must not burden the core install
or the stub path.

- Add an **optional extra**: `kbforge[llm]` → `pydantic-ai-slim[openai]`.
- `LLMSynthesizer` imports `pydantic_ai` **lazily** (inside `__init__`), raising a
  clear "install kbforge[llm]" error if absent. `kbforge` core, `StubSynthesizer`,
  and the whole existing test suite import nothing new.
- Bump the `pydantic` floor if Pydantic AI requires a newer minimum; kbforge is
  already Pydantic v2, so this is a floor bump, not a migration.

## 7. Budget, scope, oversized sources

- **1 doc → 1 concept.** No multi-doc merge or split (deferred — §11). This matches
  the existing `concept_path`/diff/scope model, so the pipeline is unchanged.
- **Per-concept budget**, not a hard per-run budget: `max_tokens` caps each call. The
  run synthesizes every changed concept; total spend is observable via the run
  observer hook (§5.3) but is not a run-level gate. This avoids the partial-publish
  dilemma (a per-run cap could leave a KB update half-synthesized yet published as if
  complete — forbidden by the all-or-nothing MR semantics).
- **Oversized source:** truncate `text` to `max_source_chars`, and record a
  `grounding_notes` entry naming the truncated doc. Recursive chunking / map-reduce is
  deferred (§11). Truncation is visible in the MR, never silent.

## 8. Failure handling

- **Malformed model output** (fails `SynthesizedConcept` validation): Pydantic AI
  re-prompts up to its retry budget. If it still fails, the run raises → the whole run
  fails; nothing is published.
- **Provider/transport errors** (timeout, 5xx, rate limit): bounded retry with
  backoff, then raise → run fails. At-least-once run semantics already make a failed
  run safe to re-run (mirror/cursor advance only on success).
- **Structurally non-conformant assembled concept** (should be impossible given §2,
  but the gate is absolute): `run_validators` returns failures → `run` returns
  `Aborted`; no MR. The validator gate is never bypassed for LLM output.
- No partial publish, ever: a run either produces one conformant `ProposedChange` for
  all changed concepts or it fails/aborts as a whole.

## 9. Testing (deterministic, offline)

The suite must never hit a real model.

- Set `pydantic_ai.models.ALLOW_MODEL_REQUESTS = False` suite-wide (a conftest guard)
  so any accidental real request raises.
- Use `FunctionModel` / `TestModel` via `agent.override(model=...)` to return canned
  `SynthesizedConcept`s. Tests exercise the real prompt-building, response handling,
  frame assembly, and the full §4.4 validator gate — deterministically.
- **Adversarial fakes** prove the frame holds regardless of model behavior:
  - a model returning prose with markdown that would break frontmatter → assembled
    concept still parses and passes the gate (frame is kbforge-owned);
  - a model "claiming" a link/anchor in prose → no structural link/anchor appears
    (the model cannot emit them);
  - an oversized source → `grounding_notes` records the truncation.
- One **opt-in live test** (skipped unless `OPENROUTER_API_KEY` is set and a
  `--run-live` marker is passed) runs the real `deepseek/deepseek-v4-flash` path end
  to end, mirroring the manual smoke test. Never part of CI's default run.

## 10. CLI integration

`kbforge run` gains synthesizer selection, mirroring the connector pattern:

- `--synthesizer {stub,llm}` (default `stub`, preserving today's behavior).
- LLM config via a repeatable `--llm-set KEY=VALUE` (same YAML-typed parsing as
  connector `--set`), keeping connector and synthesizer config namespaces separate.
  The key itself is never a CLI arg — it comes from the env var named by `api_key_env`.
- `kbforge list` gains a synthesizers line for discoverability. (Synthesis is a core
  stage, not a plugin family — no entry-point discovery; the two built-ins are named
  explicitly.)

## 11. Out of scope (named, deferred)

- **Faithfulness judge** — a second pass verifying each prose claim is supported by
  the source, dropping/flagging unsupported ones into `grounding_notes`.
- **Multi-doc merge / split** — one concept synthesized from several related canonical
  docs, or one doc fanned into several concepts.
- **Recursive chunking / map-reduce** for sources larger than the context window.
- **Hard per-run token budget** with graceful degradation.
- **Streaming, response caching, prompt-cache reuse.**

## 12. Amendments to `architecture.md`

- §7: `synthesize` is described as a stage backed by a `Synthesizer` object injected
  into `run`, with `StubSynthesizer` (default) and `LLMSynthesizer` implementations;
  the grounding boundary in §2 (LLM writes prose inside a kbforge-owned frame) is
  stated normatively.
- The `run(...)` signature note gains the `synthesizer` parameter.

## 13. Open items

- Exact `pydantic-ai-slim` version floor and the resulting `pydantic` floor bump.
- Default `max_tokens` / `max_source_chars` values (tune against real docs).
- Which `output_mode` `deepseek-v4-flash` actually handles best (`tool` vs `native` vs
  `prompted`) — pin the default from the first live runs; keep it configurable.
- Whether `SynthesizedConcept` should also carry an optional model-suggested
  `summary`/facets that kbforge *validates against* the source rather than trusts —
  a stepping stone toward the faithfulness judge, but not in v0.1.
