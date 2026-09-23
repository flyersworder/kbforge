---
type: design-note
title: kbforge — describe synthesizer (verbatim body, one-sentence description, tags)
description: A third synthesizer that keeps the stub's byte-for-byte body, has a model write only a one-sentence OKF description, and tags each concept from a vocabulary by deterministic keyword match and optionally by model choice, with model output cached per content hash in the mirror and tags promoted to a kbforge-owned key bound across both carriers.
tags: [okf, synthesis, llm, description, tags, mirror, okfquery]
generated: { by: human:flyersworder, at: 2026-09-23T00:00:00Z }
status: proposed — issue #40
okf_version: "0.2"
---

# kbforge — describe synthesizer

**The short version.** `kbforge run --synthesizer describe` renders the stub's
body unchanged and asks a model for one thing: a one-sentence `description`,
which is what OKF §4.1 says index generators, snippets and previews read.
`tags` come from a configured vocabulary two ways: deterministic keyword
matching, and optionally the model choosing among the vocabulary's tags. What
the model wrote is cached in `mirror/_described/`, keyed by the document's
`content_hash`, so a re-render for any reason other than a source change makes
no model call. `tags` becomes a kbforge-owned key with a projection
counterpart, so the value the gate checks is the value that ships.

## 1. Context

Stub concepts keep their source's bytes, which is what tables, slide decks and
reports need. But their `description` is the title, so the front door
`okfquery index` builds (`* [Title](path) - description`, OKF §8) says nothing
about them. In one deployment (72 concepts, 63 stub), an agent needed grep or
find for 9 of 12 domain tasks and missed reports that were in the bundle. The
LLM synthesizer is no substitute: it rewrites the body, so exact content is
paraphrased or dropped.

OKF v0.2 recommends both fields (§4.1): `description` is "a single sentence
summarizing the concept", and `tags` is "a YAML list of short strings for
cross-cutting categorization". Neither is filled for stub concepts today.

## 2. Decisions, and why

| Decision | Why |
|---|---|
| A new synthesizer, `describe`, not a `--describe` flag on `stub` | One CLI choice and one settings namespace (`--llm-set`). The stub stays the offline, LLM-free baseline. |
| Body is the stub body, title is `doc.title` | Exactly the stub's frame. A test pins it byte-identical. (The declarative-links design adds a kbforge-owned `## Related` section to every synthesizer's body when a concept has links; that is frame, and applies to `stub` identically.) |
| `description` is one sentence | OKF §4.1. A paragraph makes a worse index line and a worse snippet. |
| `grounds = False` | The description is written from the document text only. Citing grounding documents would claim a provenance the concept does not have (architecture.md §7.1, same reason as the stub). |
| Tags by keyword **and** by model | Keywords are deterministic, free, explainable, and stable across runs. A model catches a document about "high-voltage architecture" that never writes "800 V". Both draw from one vocabulary, so tags stay filterable across sources. |
| Model output cached by `content_hash` only | The no-op rule already means "the source did not change". Editing `instructions` or switching the model does not re-describe unchanged concepts, matching how the `llm` synthesizer treats its prompt. |
| Shipped tags = source ∪ keyword ∪ model | A source that already publishes tags keeps them. |
| The vocabulary constrains model tags only | Source tags are the source's data; keyword tags are drawn from the vocabulary by construction. |
| Vocabulary and length enforced in the synthesizer, not as a §4.4 law | A law cannot depend on deployment config (CLAUDE.md: the laws are core and unconditional). |

## 3. Configuration

`DescribeConfig(LLMConfig)` adds four fields, all set through `--llm-set`
(YAML-typed values):

```
kbforge run ... --synthesizer describe \
  --llm-set model=deepseek/deepseek-v4-flash \
  --llm-set instructions="Name the event, its date, the companies and the technical topics." \
  --llm-set 'tags_vocabulary={800v: ["800 V", "800-volt"], sic: ["SiC", "silicon carbide"], packaging: []}' \
  --llm-set model_tags=true \
  --llm-set description_max_chars=240
```

- `instructions: str = ""` — appended to the fixed prompt; what makes a good
  description differs per source.
- `tags_vocabulary: dict[str, list[str]] | None = None` — each key is a tag;
  its value is the phrases that assign it by keyword. An empty list makes a tag
  the model may choose but no keyword assigns. When unset, no keyword or model
  tags are produced.
- `model_tags: bool = True` — whether the model may also choose tags from the
  vocabulary's keys. `false` gives keyword-only tagging.
- `description_max_chars: int = 240` — must be positive (`validate_env`).

Every other `LLMConfig` field (`model`, `api_base`, `max_tokens`,
`max_source_chars`, `output_retries`, …) keeps its meaning. An unknown key is a
config error, as today. A blank tag name or phrase is a config error.

The fixed prompt keeps the `llm` synthesizer's rule: write only from the
provided text, add no outside knowledge. It asks for one sentence and, when
`model_tags` is on, the applicable tags from the listed vocabulary.

## 4. Flow

For each document the pipeline hands it (changed, drifted, referrer, or
arrival):

1. **Keyword tags.** Every vocabulary tag with a phrase that matches the
   document's title or text, case-insensitively on word boundaries after NFC
   normalization. This is the matcher grounding rules already use
   (`grounding.rule_matches`); it moves to a small shared helper rather than
   being copied. Pure, so it runs on every render, with no cache.
2. **Model output, cache hit.** `mirror/_described/<slot_key>.json` exists, its
   `content_hash` equals `doc.anchor.content_hash`, and its model tags are a
   subset of the current vocabulary's keys (an empty set always is). Reuse its
   `description` and model tags. No model call.
3. **Model output, miss.** Call the model with the fixed prompt,
   `instructions`, the vocabulary's keys (when `model_tags`), and the document
   text truncated at `max_source_chars` (with a note, as `LLMSynthesizer`
   writes). Output model: `DescribedConcept{description: str, tags: list[str]}`.
4. Hand `(doc, doc.title, description, body)` plus keyword ∪ model tags to
   `assemble`.

**Provenance.** `generated.by` is the actor that wrote the description:
`actor_for(config.model)` on a miss, the sidecar's stored `actor` on a hit. So a
model switch does not relabel descriptions the previous model wrote. `assemble`
takes `generated_by` per proposal today, so it gains a per-item override.

**The sidecar.** `{doc_id, content_hash, actor, description, tags}`, where
`tags` is the **model's** tags only — not keyword or source tags, which are
recomputed, and a source tag outside the vocabulary would otherwise make every
lookup a miss. It lives under `mirror/_described/` beside `_grounding/` and
`_first_seen/` (a subdirectory because `load_all` globs `mirror/*.json`).
Rules shared with the grounding sidecar:

- written by the pipeline after a successful publish, through `_write_atomic`,
  and only for concepts that are in `proposal.files`;
- read tolerantly: unreadable is a miss, never an error that wedges later runs;
- deleted when the owning document is tombstoned;
- lives in the mirror so the `rm -rf` that resets the mirror resets it too;
- added to `chunking`'s per-document file list (today slot + `_grounding/` +
  `_first_seen/`), so `redo` restores it with the rest of a rolled-back chunk.

The synthesizer reads the cache but never writes it. It is constructed with the
mirror path (the CLI has `--mirror`) and returns every entry it used or
produced on a new `ProposedChange.described: dict[str, DescribedRecord]`, keyed
by concept path. After a successful publish, the pipeline writes a sidecar for
each key that is also in `proposal.files`, next to the grounding sidecars.
Other synthesizers leave `described` empty. A synthesizer that wrote state
before publish would leave a cache describing a bundle that never shipped.

**Why a vocabulary change is a miss.** Otherwise narrowing the vocabulary would
make every cached concept fail on its next re-render. Re-describing it is the
smallest correct response, and only happens when the concept is rendered
anyway. Keyword tags follow a vocabulary change on the next render for free.

## 5. `tags` as a kbforge-owned key

Today `tags` is a facet: `local_files` passes frontmatter `tags:` through
`structured`, and `_facets` merges it into the rendered frontmatter. With more
writers, the dual-carrier rule applies:

- `OKF_OWNED` gains `tags`, so `_facets` drops it.
- `ConceptFrontmatter` gains `tags: list[str]`.
- `assemble` sets it for **every** synthesizer to the sorted, deduplicated
  union of source tags and the synthesizer's tags. Source tags come from
  `structured["tags"]`: a string becomes a one-item list, non-string items are
  dropped. The synthesizer's tags are empty except under `describe`.
- `_render` writes `tags:` after `sources`/`links`, only when non-empty.
- `_check_carriers_agree` binds rendered `tags` to `concept.tags` (absent
  equals `[]`).
- A shape check next to facet well-formedness: `tags` is a list of non-blank
  strings.

`local_files._RESERVED_KEYS` must **not** gain `tags`: it has to reach
`structured` for `assemble` to read it.

**okfquery is unchanged in behaviour.** It reads files, and to a consumer
`tags` is still a top-level list it can filter and group on
(`okfquery index --group-by tags`). Its own `OKF_OWNED` stays as is; only its
comment ("Mirrors kbforge's synthesize.OKF_OWNED") changes to say why `tags`
differs.

**Visible change to existing bundles.** A concept whose source carries `tags`
renders the key in a new position the next time it is rendered. The value is
the same, deduplicated and sorted. Nothing is rewritten until the concept is
re-rendered for its own reasons.

## 6. Enforcement and errors

- **Model tags.** An output validator raises `ModelRetry` for any tag outside
  the vocabulary's keys, naming the allowed values, so `output_retries`
  applies. Exhausted retries raise `SynthesisError` naming the concept and the
  offending tags.
- **Description.** Blank, containing a newline, or over
  `description_max_chars` is a retry, then `SynthesisError`. Never truncated
  silently. "One sentence" is asked for in the prompt; the length cap and
  single-line check are what is enforced, since sentence boundaries are not
  reliably checkable.
- `SynthesisError` is already handled by the CLI: nothing published, mirror and
  cursor unchanged, the next run retries.
- The model's output is only `description` and `tags`. It cannot reach `type`,
  `links`, `sources`, `generated` or the body.
- The CLI's "grounding rules are validated but inactive" warning now covers
  `describe` as well as `stub`.

## 7. Invariants (CLAUDE.md)

- **Pipeline order and the no-op rule:** unchanged. `describe` runs only where
  `stub` would.
- **Dual carrier:** `tags` is bound in both directions (§5); the path-set
  binding is untouched.
- **§4.4 laws stay core:** the vocabulary is enforced by the synthesizer, not
  added as a law.
- **`normalize` is pure:** nothing here touches a connector.
- **Emit-side vocabulary:** `generated.by` stays two segments via `actor_for`.

## 8. Testing

Offline, with a scripted model (the `LLMSynthesizer._build_agent(config,
model=…)` pattern):

1. For a concept without links, the rendered body is byte-identical to plain
   `stub` output for the same document.
2. Description is the model's, one line, within the cap; `generated.by` is
   `kbforge/<model>`. A multi-line or over-long description retries, then
   raises `SynthesisError`.
3. Tags:
   - keyword tags: exactly the vocabulary tags whose phrases match on word
     boundaries (`SiC` matches "SiC MOSFET", not "basic");
   - model tags outside the vocabulary retry; exhausted retries raise
     `SynthesisError` naming the concept and the tag;
   - `model_tags=false` makes no tag request of the model;
   - a source tag outside the vocabulary still ships;
   - shipped tags are the sorted union.
4. Sidecar:
   - unchanged second run is `NoOp` with zero model calls;
   - a referrer or arrival re-render reuses the cache, zero model calls;
   - a changed `content_hash` re-describes;
   - narrowing the vocabulary past a cached model tag re-describes;
   - a tombstone deletes the sidecar; `redo` restores it;
   - after a model switch, a cache hit keeps the stored actor;
   - a failed publish writes no sidecar.
5. Tags carrier: `tags` no longer appears as a facet. Mutation checks, in place
   and restored with `git checkout --`: a `tags` value only in the file, and a
   malformed `tags`, each fail with their own message.
6. `okfquery index` lines for describe concepts carry the new description.

Live (`--run-live`, key from `.env`): a real model describes two verbatim
sources under a vocabulary with both keyword and model tags, then a real CLI
run on a scratch mirror, run twice, ends in `NoOp`.

## 9. Deferred

- **Keyword tags for every synthesizer.** They need no model, so `stub` and
  `llm` could use them too; that means a pipeline-level vocabulary rather than
  an `--llm-set` key. Worth doing if keyword tagging proves useful on its own.
- `instructions` for the `llm` synthesizer (listed as related in #40); the
  field is shaped so it can move to `LLMConfig` unchanged.
- Re-describing on an `instructions` or model change: a config fingerprint in
  the sidecar and a drift rule like grounding rule 3.
- kbforge-sql's id-column option and multi-document concepts (both listed as
  related in #40) are separate issues.
