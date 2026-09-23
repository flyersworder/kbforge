---
type: design-note
title: kbforge — describe synthesizer (verbatim body, model-written description and tags)
description: A third synthesizer that keeps the stub's byte-for-byte body and has a model write only the frontmatter description and tags, cached per content hash in the mirror so re-renders are free and stable, with tags promoted to a kbforge-owned key bound across both carriers.
tags: [okf, synthesis, llm, description, tags, mirror, okfquery]
generated: { by: human:flyersworder, at: 2026-09-23T00:00:00Z }
status: proposed — issue #40
okf_version: "0.2"
---

# kbforge — describe synthesizer

**The short version.** `kbforge run --synthesizer describe` renders the stub's
body unchanged and asks a model for two frontmatter fields only: a
`description` and, optionally, `tags` drawn from a configured vocabulary. What
the model wrote is cached in `mirror/_described/`, keyed by the document's
`content_hash`, so a concept re-rendered for any reason other than a source
change reuses it without a model call. `tags` becomes a kbforge-owned key
with a projection counterpart, so the value the gate checks is the value that
ships.

## 1. Context

Stub concepts keep their source's bytes, which is what tables, slide decks and
reports need. But their `description` is the title, so the front door
`okfquery index` builds (`* [Title](path) - description`) says nothing about
them. In one deployment (72 concepts, 63 stub), an agent needed grep or find
for 9 of 12 domain tasks and missed reports that were in the bundle. The LLM
synthesizer is no substitute: it rewrites the body, so exact content is
paraphrased or dropped.

OKF §4.1 names `description` as what index generators, snippets and previews
read, and `tags` for cross-cutting categories. Neither is filled for stub
concepts today. #41 (declarative links) needs tags readable from the mirror for
its tag-matching rules, which is part of why the cache lives there (§4).

## 2. Decisions, and why

| Decision | Why |
|---|---|
| A new synthesizer, `describe`, not a `--describe` flag on `stub` | One CLI choice and one settings namespace (`--llm-set`). The stub stays the offline, LLM-free baseline. |
| Body is `doc.text`, title is `doc.title` | Exactly the stub's frame. A test pins the body byte-identical. |
| `grounds = False` | The description is written from the document text only, as the issue requires. Citing grounding documents would claim a provenance the concept does not have (§7.1 of architecture.md, same reason as the stub). |
| Cache keyed by `content_hash` only | The no-op rule already means "the source did not change". Editing `instructions` or switching the model does not re-describe unchanged concepts, matching how the `llm` synthesizer treats its prompt. |
| Shipped tags = source tags ∪ model tags | A source that already publishes tags keeps them. |
| Vocabulary applies to model tags only | Source tags are the source's data; a vocabulary constrains what the model may invent. |
| Vocabulary and length enforced in the synthesizer, not as a §4.4 law | A law cannot depend on deployment config (CLAUDE.md: the laws are core and unconditional). |

## 3. Configuration

`DescribeConfig(LLMConfig)` adds three fields, all set through `--llm-set`:

```
kbforge run ... --synthesizer describe \
  --llm-set model=deepseek/deepseek-v4-flash \
  --llm-set instructions="Name the event, its date, the companies and the technical topics." \
  --llm-set tags_vocabulary=[800v,sic,gan,packaging] \
  --llm-set description_max_chars=500
```

- `instructions: str = ""` — appended to the fixed prompt; what makes a good
  description differs per source.
- `tags_vocabulary: list[str] | None = None` — when set, the only values the
  model may emit. When unset, the model emits no tags.
- `description_max_chars: int = 500` — must be positive (`validate_env`).

Every other `LLMConfig` field (`model`, `api_base`, `max_tokens`,
`max_source_chars`, `output_retries`, …) keeps its meaning. An unknown key is a
config error, as today.

The fixed prompt keeps the `llm` synthesizer's rule: write only from the
provided text, add no outside knowledge. It asks for one paragraph.

## 4. Flow

For each document the pipeline hands it (changed, drifted-by-referrer, or
arrival):

1. **Cache hit.** `mirror/_described/<slot_key>.json` exists, its
   `content_hash` equals `doc.anchor.content_hash`, and its tags are a subset
   of the current `tags_vocabulary` (an empty set is always a subset). Reuse its
   `description` and `tags`. No model call.
2. **Miss.** Call the model with the fixed prompt, `instructions`, the
   vocabulary, and the document text truncated at `max_source_chars` (with a
   `grounding_notes` note, as `LLMSynthesizer` writes). Output model:
   `DescribedConcept{description: str, tags: list[str]}`.
3. Hand `(doc, doc.title, description, doc.text)` plus the model tags to
   `assemble`.

**Provenance.** `generated.by` is the actor that wrote the description:
`actor_for(config.model)` on a miss, the sidecar's stored `actor` on a hit. So a
model switch does not relabel descriptions the previous model wrote.
`assemble` takes `generated_by` per proposal today, so it gains a per-item
override for this.

**The sidecar.** `{doc_id, content_hash, actor, description, tags}`, under
`mirror/_described/` beside `_grounding/` and `_first_seen/` (a subdirectory
because `load_all` globs `mirror/*.json`). Rules shared with the grounding
sidecar:

- written by the pipeline after a successful publish, through `_write_atomic`,
  and only for concepts that are in `proposal.files`;
- read tolerantly: unreadable is a miss, never an error that wedges later runs;
- deleted when the owning document is tombstoned;
- lives in the mirror so the `rm -rf` that resets the mirror resets it too.

The synthesizer reads the cache but never writes it. It is constructed with the
mirror path (the CLI has `--mirror`) and returns every entry it used or
produced on a new `ProposedChange.described: dict[str, DescribedRecord]`, keyed
by concept path. After a successful publish, the pipeline writes a sidecar for
each key that is also in `proposal.files`, next to the grounding sidecars. The
record stores the **model's** tags, not the shipped union: a source tag outside
the vocabulary would otherwise make every later lookup a miss. Other
synthesizers leave `described` empty. A synthesizer that wrote state before
publish would leave a cache describing a bundle that never shipped.

**Why a vocabulary change is a miss.** Otherwise narrowing the vocabulary would
make every cached concept fail on its next re-render. Re-describing it instead
is the smallest correct response, and it only happens when the concept is
rendered anyway.

## 5. `tags` as a kbforge-owned key

Today `tags` is a facet: `local_files` passes frontmatter `tags:` through
`structured`, and `_facets` merges it into the rendered frontmatter. With a
second writer (the model), the dual-carrier rule applies:

- `OKF_OWNED` gains `tags`, so `_facets` drops it.
- `ConceptFrontmatter` gains `tags: list[str]`.
- `assemble` sets it for **every** synthesizer to `sorted(set(source_tags) |
  set(model_tags))`. Source tags come from `structured["tags"]`: a string
  becomes a one-item list, non-string items are dropped. Model tags are empty
  except under `describe`.
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

- **Vocabulary.** An output validator raises `ModelRetry` for any model tag
  outside `tags_vocabulary`, naming the allowed values, so `output_retries`
  applies. Exhausted retries raise `SynthesisError` naming the concept and the
  offending tags.
- **Length.** A description over `description_max_chars`, or blank, is a retry,
  then `SynthesisError`. Never truncated silently.
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

Offline, with a scripted model (`LLMSynthesizer._build_agent(config, model=…)`
pattern):

1. Body byte-identical to plain `stub` output for the same document.
2. Description is the model's and non-empty; `generated.by` is
   `kbforge/<model>`.
3. A model tag outside the vocabulary retries; exhausted retries raise
   `SynthesisError` whose message names the concept and the tag. A source tag
   outside the vocabulary still ships.
4. Sidecar:
   - unchanged second run is `NoOp` with zero model calls;
   - a referrer or arrival re-render reuses the cache, zero model calls;
   - a changed `content_hash` re-describes;
   - narrowing the vocabulary past a cached tag re-describes;
   - a tombstone deletes the sidecar;
   - after a model switch, a cache hit keeps the stored actor;
   - a failed publish writes no sidecar.
5. Tags carrier: source ∪ model tags; `tags` no longer appears as a facet.
   Mutation checks, in place and restored with `git checkout --`: a `tags`
   value only in the file, and a malformed `tags`, each fail with their own
   message.
6. `okfquery index` lines for describe concepts carry the new description.

Live (`--run-live`, key from `.env`): a real model describes two verbatim
sources under a vocabulary, then a real CLI run on a scratch mirror, run
twice, ends in `NoOp`.

## 9. Deferred

- `instructions` for the `llm` synthesizer (listed as related in #40); the
  field is shaped so it can move to `LLMConfig` unchanged.
- Re-describing on an `instructions` or model change. If wanted, it is a
  config fingerprint in the sidecar and a drift rule like grounding rule 3.
- kbforge-sql's id-column option and multi-document concepts (both listed as
  related in #40) are separate issues.
