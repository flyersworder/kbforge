---
type: design-note
title: kbforge — Library Architecture & Connector Protocol (Sketch)
description: Package architecture, Pluggy hookspecs, connector protocol, and conformance rules for a Python library that standardizes the production side of OKF knowledge bundles.
tags: [okf, pluggy, connectors, knowledge-base, producer, agent-governance]
timestamp: 2026-07-11T00:00:00Z
status: draft
okf_version: "0.1"
---

# kbforge — Library Architecture & Connector Protocol

**Status:** Draft v0.1 · **Companion to:** [`context/knowledge-base-design.md`](context/knowledge-base-design.md)
**Name:** `kbforge` — *agent-first knowledge bases, forged from your systems of record.*
**Repo:** `flyersworder/kbforge` · connectors: `kbforge-<system>` · entry points: `kbforge.connectors`

## 0. What we are standardizing

OKF v0.1 standardizes the **artifact at rest** — files, frontmatter, `index.md`/`log.md`.
It says nothing about how bundles are produced, grounded, kept fresh, or trusted.
This library is the reference implementation of that missing half:

| Layer | Standardized by | Status |
|---|---|---|
| Artifact format | OKF v0.1 (Google) | exists |
| Semantic vocabulary (`type` taxonomy) | us, per domain | our design doc §5.4 |
| **Production protocol** (connectors, canonicalization, diff, provenance, publish) | **this library** | this spec |
| Serving protocol | MCP | exists |

Design stance carried over from the main doc: **the core ships zero connectors,
zero credentials, zero CI logic.** Connectors are plugins; deployments are separate
repos. The interface is the product.

---

## 1. Package architecture

```
kbforge (core, PyPI)
├── kbforge/
│   ├── models.py          # Pydantic data model (§3)
│   ├── hookspecs.py       # Pluggy specs (§5)
│   ├── registry.py        # plugin discovery via entry points
│   ├── pipeline.py        # the sync algorithm (§7) — core, NOT pluggable
│   ├── canonical.py       # stability checker, hashing (§4.3)
│   ├── validate.py        # strict producer-side OKF checks (4 required fields)
│   └── testing/           # contract-test kit for connector authors (§9)
│
kbforge-confluence   (separate package, own release cycle)
kbforge-servicenow   (separate package)
kbforge-gitlab-repo  (separate package; in-house-apps case)
│
<deployment repo>          (private; config, credentials via CI vars, type vocab,
                            MR templates, schedule — everything org-specific)
```

Discovery: connectors register under the entry-point group **`kbforge.connectors`**.
`pip install kbforge-confluence` is the entire installation story.

```toml
# kbforge-confluence/pyproject.toml
[project.entry-points."kbforge.connectors"]
confluence = "kbforge_confluence.plugin"
```

**What is deliberately NOT pluggable:** the pipeline order (fetch → normalize →
mirror → diff → scope → synthesize → validate → publish), the no-op rule, and the
never-auto-merge rule. These are the trust guarantees of the standard; making them
pluggable would make them optional. Plugins extend *stages*; they cannot reorder or
remove them. (This is the same posture as ADR-2 in the main doc.)

---

## 2. Two plugin families

1. **Connectors** (`ConnectorSpec`, §5.1) — bring data *in* from a system of record.
   Fully specified in this sketch; this is what we build first.
2. **Publishers** (`PublisherSpec`, §5.2) — push proposals *out* (GitLab MR, GitHub PR,
   local dry-run). Thin; sketched here because the pilot needs exactly one (GitLab).

Synthesis (the LLM step) and validation are **core stages with narrow extension
hooks** (§5.3), not open plugin families — they carry the grounding contract and the
security posture, which we do not want third-party plugins silently weakening.

---

## 3. Data model (Pydantic)

```python
# kbforge/models.py
from datetime import datetime
from pydantic import BaseModel, Field


class ConnectorInfo(BaseModel):
    """Static self-description; used for registry listing and docs."""
    name: str                      # "confluence" — unique, entry-point name
    version: str
    source_system: str             # human label: "Atlassian Confluence"
    info_types: list[str]          # which KB info types this SoR is authoritative
                                   # for, e.g. ["runbook", "architecture-notes"]
    config_schema: type[BaseModel] # connector-specific config model


class Cursor(BaseModel):
    """Opaque incremental-sync watermark. Core persists it; only the owning
    connector interprets it (timestamp, sys_updated_on, etag set, git SHA...)."""
    connector: str
    payload: dict = Field(default_factory=dict)


class ResourceAnchor(BaseModel):
    """Provenance. Every document and every downstream concept claim carries one.
    Maps 1:1 onto the OKF `resource` frontmatter field at emit time."""
    system: str                    # "servicenow"
    native_id: str                 # sys_id / page id / repo path
    url: str | None = None         # human-clickable deep link
    retrieved_at: datetime
    content_hash: str              # hash of the CANONICAL form (§4.3), not the raw


class RawRecord(BaseModel):
    """One record as fetched. Persisted to the mirror's raw side for audit;
    never diffed directly (raw exports are volatile — see §4.3)."""
    anchor_hint: dict              # enough to build a ResourceAnchor later
    media_type: str                # "application/json", "text/html", ...
    payload: bytes


class FetchResult(BaseModel):
    records: list[RawRecord]
    cursor: Cursor                 # new watermark; core persists on success
    complete: bool = True          # False => partial fetch (rate-limited); core
                                   # may continue but must not treat absent
                                   # records as deletions


class CanonicalDocument(BaseModel):
    """The diff-stable unit. This is what the mirror stores and what change
    detection runs on. The stability laws in §4.3 apply here."""
    anchor: ResourceAnchor
    doc_id: str                    # stable across syncs: f"{system}:{native_id}"
    title: str
    text: str                      # normalized plain text / markdown
    structured: dict = Field(default_factory=dict)   # typed fields (owner, env...)
    relations: list[str] = Field(default_factory=list)  # doc_ids this links to
    deleted: bool = False          # tombstone — deletions are explicit, never
                                   # inferred from absence (see FetchResult.complete)


class ChangeSet(BaseModel):
    """Output of the core diff stage; input to synthesis scoping."""
    added: list[str]
    modified: list[str]
    removed: list[str]             # tombstoned doc_ids
    unchanged_count: int

    @property
    def is_noop(self) -> bool:
        return not (self.added or self.modified or self.removed)


class ProposedChange(BaseModel):
    """What synthesis hands to a publisher: concept files + reviewable summary.
    The structured summary is what makes the MR a 90-second review, not
    archaeology (main doc §6 / reviewer-fatigue point)."""
    branch_hint: str
    files: dict[str, str]          # bundle-relative path -> full new content
    summary: "ChangeSummary"


class ChangeSummary(BaseModel):
    """Producer-generated MR description, structured."""
    sources_changed: list[ResourceAnchor]
    claims_added: list[str]
    claims_modified: list[str]
    claims_removed: list[str]
    conflicts_flagged: list[str]   # "CMDB says owner=A; Confluence says owner=B"
    gaps_flagged: list[str]        # "no DR runbook found for app X"
    grounding_notes: list[str]     # claims whose evidence weakened
```

---

## 4. The connector protocol (contract, not just interface)

### 4.1 Lifecycle

```
validate_config ──▶ fetch(cursor) ──▶ normalize(records) ──▶ [core takes over]
     once/run          incremental        deterministic         mirror, diff,
                                                                scope, synth,
                                                                validate, publish
```

A connector implements exactly this and nothing downstream. Connectors never see
the bundle, never call the LLM, never touch git. (Rule-of-Two posture from main
doc §7: the component holding SoR credentials performs no consequential external
action — publishing is a different plugin family running in a different stage.)

### 4.2 Incremental contract

- `fetch(config, cursor)` where `cursor=None` means full backfill.
- The connector returns a new `Cursor`; the core persists it **only after** the
  whole pipeline run succeeds (so failed runs re-fetch — at-least-once semantics;
  normalize determinism makes replays harmless).
- Deletions must be **explicit tombstones** (`CanonicalDocument.deleted=True`).
  Absence from an incremental fetch never implies deletion; `FetchResult.complete`
  exists so rate-limited partial fetches don't trigger false "removed" diffs.

### 4.3 Canonicalization laws (the load-bearing part)

`normalize()` must satisfy three laws. These are what defuse the noisy-diff risk
(main doc review: "SoR exports embed volatile fields; without normalization,
no-op detection fails and MR economics collapse").

1. **Determinism.** Same raw payload → byte-identical `CanonicalDocument`
   (stable key order, stable list order, normalized whitespace/encoding).
2. **Volatility exclusion.** Fields that change without meaning changing —
   export timestamps, view counters, `sys_mod_count`, ad-banner HTML — must not
   survive into the canonical form. `retrieved_at` lives on the anchor, which is
   excluded from the diff hash.
3. **Semantic sufficiency.** Everything synthesis is allowed to claim must be
   present in the canonical form — synthesis never reaches back to raw payloads.
   (Keeps the grounding contract checkable: claims trace to canonical docs,
   canonical docs trace to anchors.)

The core **enforces** law 1 mechanically: the test kit (§9) and an optional
runtime check normalize twice and compare hashes; a connector that fails is
rejected at registration in strict mode.

---

## 5. Pluggy hookspecs

```python
# kbforge/hookspecs.py
import pluggy
from kbforge.models import (
    ConnectorInfo, Cursor, FetchResult, RawRecord,
    CanonicalDocument, ProposedChange,
)

PROJECT = "kbforge"
hookspec = pluggy.HookspecMarker(PROJECT)
hookimpl = pluggy.HookimplMarker(PROJECT)
```

### 5.1 `ConnectorSpec`

```python
class ConnectorSpec:
    """One plugin class per system of record."""

    @hookspec
    def kbforge_connector_info(self) -> ConnectorInfo:
        """Static self-description. Called at registration."""

    @hookspec
    def kbforge_validate_config(self, config: dict) -> list[str]:
        """Return human-readable problems ([] = ok). Called once per run,
        before any network I/O. Credential *presence* is checked here;
        credential *values* come from env/CI vars, never from the bundle."""

    @hookspec
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        """Pull raw records changed since `cursor` (None = full backfill).
        Must respect rate limits internally; may return complete=False."""

    @hookspec
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        """Deterministic, volatile-free, semantically sufficient (§4.3).
        Pure function of its input: no network, no clock, no randomness."""
```

### 5.2 `PublisherSpec`

```python
class PublisherSpec:
    """Where proposals go. Pilot ships gitlab-mr; dry-run ships in core."""

    @hookspec
    def kbforge_publisher_info(self) -> ConnectorInfo: ...

    @hookspec
    def kbforge_publish(self, change: ProposedChange, config: dict) -> str:
        """Open a review request (MR/PR). Returns its URL.
        MUST NOT merge. Must be idempotent per (branch_hint, content-hash):
        re-running a failed pipeline updates the same MR, never opens twins."""
```

### 5.3 Core-stage extension hooks (narrow, additive-only)

```python
class PipelineHooks:
    """Observability + additive checks. Cannot veto-free the pipeline's own
    gates; a hook can only ADD failures, never remove them."""

    @hookspec
    def kbforge_extra_validators(self) -> list["Validator"]:
        """Contribute bundle validators run in CI stage: secret scan (gitleaks),
        PII scan (GDPR / contact-type concepts), link check, vocab conformance."""

    @hookspec
    def kbforge_run_observer(self, event: str, payload: dict) -> None:
        """Telemetry: stage timings, token spend (LiteLLM budget), diff sizes,
        no-op rate. Feeds the silent-staleness alerting from main-doc review."""
```

### 5.4 Registration and dispatch

Multiple connectors coexist; hooks are dispatched **per connector**, not
broadcast — the registry keeps one `PluginManager` but drives each connector
through a `subset_hook_caller`, so `fetch` on Confluence never fans out to
ServiceNow:

```python
# kbforge/registry.py (sketch)
import pluggy
from kbforge import hookspecs

def build_registry() -> dict[str, "BoundConnector"]:
    pm = pluggy.PluginManager(hookspecs.PROJECT)
    pm.add_hookspecs(hookspecs.ConnectorSpec)
    pm.add_hookspecs(hookspecs.PublisherSpec)
    pm.add_hookspecs(hookspecs.PipelineHooks)
    pm.load_setuptools_entrypoints("kbforge.connectors")

    registry = {}
    for plugin in pm.get_plugins():
        caller = pm.subset_hook_caller  # bind hooks to this plugin only
        info = plugin.kbforge_connector_info()
        registry[info.name] = BoundConnector(info=info, plugin=plugin)
    return registry
```

---

## 6. Example connector skeleton

```python
# kbforge_confluence/plugin.py
from kbforge.hookspecs import hookimpl
from kbforge.models import *


class ConfluenceConfig(BaseModel):
    base_url: str
    space_keys: list[str]
    token_env_var: str = "CONFLUENCE_TOKEN"   # name of the env var, never the value


class ConfluenceConnector:

    @hookimpl
    def kbforge_connector_info(self) -> ConnectorInfo:
        return ConnectorInfo(
            name="confluence",
            version="0.1.0",
            source_system="Atlassian Confluence",
            info_types=["runbook", "architecture-notes"],
            config_schema=ConfluenceConfig,
        )

    @hookimpl
    def kbforge_validate_config(self, config: dict) -> list[str]:
        problems = []
        cfg = ConfluenceConfig.model_validate(config)
        if not os.environ.get(cfg.token_env_var):
            problems.append(f"env var {cfg.token_env_var} not set")
        return problems

    @hookimpl
    def kbforge_fetch(self, config: dict, cursor: Cursor | None) -> FetchResult:
        cfg = ConfluenceConfig.model_validate(config)
        since = (cursor.payload.get("last_sync") if cursor else None)
        pages, watermark = _cql_pages_modified_since(cfg, since)   # handles paging
        return FetchResult(
            records=[_to_raw(p) for p in pages],
            cursor=Cursor(connector="confluence", payload={"last_sync": watermark}),
        )

    @hookimpl
    def kbforge_normalize(self, records: list[RawRecord]) -> list[CanonicalDocument]:
        docs = []
        for r in records:
            page = json.loads(r.payload)
            docs.append(CanonicalDocument(
                doc_id=f"confluence:{page['id']}",
                title=page["title"].strip(),
                text=_storage_format_to_markdown(page["body"]),  # strips macros,
                structured={"space": page["space"]["key"],       # view counters,
                            "labels": sorted(page["labels"])},   # volatile HTML
                relations=sorted(_extract_page_links(page)),
                anchor=_anchor(page, r),
            ))
        return docs
```

---

## 7. The core pipeline (fixed order — this IS the standard)

```python
# kbforge/pipeline.py (sketch of the run loop)
def run(bundle: Path, mirror: Path, registry, publisher, synthesizer, cfg):
    for name, conn in registry.items():
        problems = conn.validate_config(cfg.connectors[name])
        if problems: abort(name, problems)                    # fail fast, no I/O

    changesets = {}
    for name, conn in registry.items():
        result = conn.fetch(cfg.connectors[name], load_cursor(name))
        docs = conn.normalize(result.records)
        assert_stability(conn, result.records, docs)          # §4.3 law 1
        changesets[name] = mirror_and_diff(mirror, docs, result.complete)

    total = merge(changesets)
    if total.is_noop:
        return NoOp()                                         # no MR. ever.

    proposal = synthesizer.synthesize(                        # LLM stage; grounding
        bundle, mirror, total,                                # contract lives here,
        budget=cfg.token_budget,                              # scoped to changed
    )                                                         # concepts only

    failures = run_validators(bundle, proposal)               # strict OKF + extras
    if failures: abort_with_report(failures)

    url = publisher.publish(proposal, cfg.publisher)          # opens MR; never merges
    persist_cursors(changesets)                               # only on full success
    return Published(url=url)
```

Everything main-doc §5.3 requires falls out of the seams: change-scoped updates
(diff drives synthesis scope), no-op detection (`is_noop` gate), grounding
(synthesis reads only canonical docs, emits `resource` = anchors), reviewability
(`ChangeSummary` becomes the MR body), and the security split (fetch stage holds
credentials but no external action; publish stage acts but holds no SoR access).

---

## 8. Connection to the agent-contracts framework

Each connector is a bounded execution unit and maps cleanly onto the seven-tuple:
**I** = (config, cursor); **O** = (canonical docs, cursor′); **S** = the SoR named
in `ConnectorInfo`; **R** = read-only, rate-limited; **T** = per-run invocation;
**Φ** = the canonicalization laws (§4.3) as checkable postconditions;
**Ψ** = the stability/tombstone invariants the test kit verifies. The pipeline is
then a *composition* of contracted units with the trust properties (no-op, human
gate) provable at the composition level — a small production instance of the
Paper 2 conservation-under-composition argument, worth a footnote there.

---

## 9. Conformance & the contract-test kit

`kbforge.testing` ships a reusable suite any connector repo runs in its CI:

- **Stability test:** normalize the same fixtures twice → identical hashes (law 1).
- **Volatility test:** author provides two raw exports of the *same unchanged
  content taken at different times* (the "export twice a week apart" spike from
  the main-doc review, turned into a permanent fixture) → identical canonical
  docs (law 2).
- **Tombstone test:** deletions surface as explicit tombstones; partial fetches
  never produce `removed` entries.
- **Purity test:** `normalize` runs with network access blocked and a frozen clock.
- **Anchor test:** every doc carries a resolvable `ResourceAnchor`.

A connector passing the kit may claim **"kbforge conformant"** — this badge,
not the core code, is what makes the connector ecosystem trustworthy, and it is
the operational meaning of the "standard" we are materializing.

## 10. Build sequence (extraction, not upfront design)

1. **Now:** monorepo pilot; `kbforge/` as an importable package with these
   hookspecs; Confluence + ServiceNow (or GitLab-repo, pending main-doc §9.1)
   connectors written in-tree against the protocol.
2. **After connector #2 forces interface honesty:** freeze hookspec v0.1, split
   connectors into their own packages, publish core to PyPI.
3. **Then:** publish the contract-test kit + a `cookiecutter-kbforge-connector`
   template; that's the moment this becomes a standard others can implement
   rather than a library we happen to own.

Open items for v0.2 of this sketch: attachment/binary handling in the mirror
(object store vs git, per main-doc review), a `SynthesizerSpec` decision (keep
closed vs open cautiously), and async fetch (likely `anyio` from the start —
cheap now, painful to retrofit).
