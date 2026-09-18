"""Cross-source grounding: which documents ground which, and whether that has
changed since the concept was last built (design note 2026-08-20).

Everything here is pure except the sidecar functions (`write_sidecar`,
`read_sidecar`, `delete_sidecar`, `has_sidecars`) and the first-seen functions
(`record_first_seen`, `load_first_seen`, `delete_first_seen`). Resolution lives
on this side of the seam, never in a synthesizer: a synthesizer that chose its
own sources would be choosing its own provenance."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kbforge.mirror import slot_key
from kbforge.models import CanonicalDocument, resource_key
from kbforge.synthesize import concept_path

DEFAULT_MAX_GROUNDING_DOCS = 5


class RuleFor(BaseModel):
    """Which concepts a rule grounds. Keys present are AND-ed."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    system: str | None = None
    doc: list[str] | None = None


class RuleFrom(BaseModel):
    """Which documents may ground them."""

    model_config = ConfigDict(extra="forbid")

    system: str


class GroundingRule(BaseModel):
    """A templated grounding rule (design note 2026-09-18). `for`/`from` are
    Python keywords, hence the aliases."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    for_: RuleFor = Field(alias="for")
    from_: RuleFrom = Field(alias="from")
    match: list[str]
    newest: int = 3
    by: str | None = None


class GroundingConfig(BaseModel):
    """The operator subject map (§2.2). `extra="forbid"` so a typo'd key is an
    error rather than a silently empty map."""

    model_config = ConfigDict(extra="forbid")

    max_grounding_docs: int = DEFAULT_MAX_GROUNDING_DOCS
    grounding: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[GroundingRule] = Field(default_factory=list)


def load_grounding(path: Path | None) -> GroundingConfig:
    if path is None:
        return GroundingConfig()
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return GroundingConfig.model_validate(raw)


def is_qualified(value: str) -> bool:
    """A doc_id is `system:native_id` with both halves non-empty.

    Public because both declaration sites must agree on it: the subject map the
    CLI validates, and `grounded_by` as a connector reads it off a source
    document. Two copies of this rule would let the two sites accept different
    ids, and only one of them produces an operator-facing message."""
    system, sep, native = value.partition(":")
    return bool(sep and system and native)


# Plain `{name}` substitution, deliberately not str.format, which reads `{a.b}`
# as an attribute and `{a:{w}}` as a nested field (the kbforge-sql url_template
# lesson). One pattern serves validation and filling.
_FIELD = re.compile(r"\{([^{}]*)\}")


def template_fields(template: str) -> list[str] | None:
    """Placeholder names in a match phrase, or None if its braces don't pair
    up into `{name}` fields."""
    rest = _FIELD.sub("", template)
    if "{" in rest or "}" in rest:
        return None
    return _FIELD.findall(template)


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _field(owner: CanonicalDocument, name: str) -> str | None:
    if name == "title":
        value: object = owner.title
    elif name == "native_id":
        value = owner.anchor.native_id
    else:
        value = owner.structured.get(name)
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value).strip()
    return text or None


def fill(template: str, owner: CanonicalDocument) -> str | None:
    """A match phrase with its `{fields}` filled from `owner`, or None when any
    field is missing or blank. Matching an empty field would match everything,
    so such a phrase is dropped for this owner only."""
    missing = False

    def sub(m: re.Match[str]) -> str:
        nonlocal missing
        value = _field(owner, m.group(1))
        if value is None:
            missing = True
            return ""
        return value

    out = _FIELD.sub(sub, template)
    return None if missing or not out.strip() else out


def _applies(rule: GroundingRule, owner: CanonicalDocument) -> bool:
    scope = rule.for_
    kind = str(owner.structured.get("type") or "concept")
    if scope.type is not None and kind != scope.type:
        return False
    if scope.system is not None and owner.anchor.system != scope.system:
        return False
    return scope.doc is None or owner.doc_id in scope.doc


def _as_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    # Naive is taken as UTC: the one assumption needed to order it against an
    # aware value, and the convention kbforge-sql's canonical form documents.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def rule_matches(
    owner: CanonicalDocument,
    cfg: GroundingConfig,
    by_id: dict[str, CanonicalDocument],
    first_seen: dict[str, datetime],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Rule-selected grounding for `owner`: `(doc_id, reason)` in rank order,
    plus cap and unparseable-date notes. Pure and deterministic over `by_id`.

    Rank is newest first by the rule's `by` facet, else first-seen; undated
    candidates last; `doc_id` breaks every tie, so the order is total."""
    path = concept_path(owner.doc_id)
    matched: list[tuple[str, str]] = []
    listed: set[str] = set()
    notes: list[str] = []
    for i, rule in enumerate(cfg.rules, 1):
        if not _applies(rule, owner):
            continue
        phrases = [(t, p) for t in rule.match if (p := fill(t, owner)) is not None]
        patterns = [
            (
                t,
                p,
                re.compile(rf"(?<!\w){re.escape(_nfc(p))}(?!\w)", re.IGNORECASE),
            )
            for t, p in phrases
        ]
        if not patterns:
            continue
        ranked: list[tuple[datetime | None, str, str]] = []
        for doc in by_id.values():
            if (
                doc.deleted
                or doc.doc_id == owner.doc_id
                or doc.anchor.system != rule.from_.system
            ):
                continue
            haystack = _nfc(f"{doc.title}\n{doc.text}")
            hit = next(((t, p) for t, p, rx in patterns if rx.search(haystack)), None)
            if hit is None:
                continue
            when = None
            if rule.by is not None:
                raw = doc.structured.get(rule.by)
                when = _as_time(raw)
                if raw is not None and when is None:
                    notes.append(
                        f"{path}: rule {i}: {doc.doc_id} has an unparseable "
                        f"{rule.by!r} value {raw!r}; ranked by first-seen"
                    )
            if when is None:
                when = first_seen.get(doc.doc_id)
            reason = (
                f"{path}: grounded by rule {i} ({hit[0]!r} = {hit[1]!r}) "
                f"via {doc.doc_id}"
            )
            ranked.append((when, doc.doc_id, reason))
        ranked.sort(
            key=lambda r: (r[0] is None, -r[0].timestamp() if r[0] else 0.0, r[1])
        )
        kept, dropped = ranked[: rule.newest], ranked[rule.newest :]
        if dropped:
            notes.append(
                f"{path}: rule {i} capped at {rule.newest}; dropped "
                + ", ".join(doc_id for _, doc_id, _ in dropped)
            )
        for _, doc_id, reason in kept:
            if doc_id not in listed:
                listed.add(doc_id)
                matched.append((doc_id, reason))
    return matched, notes


def problems_for(cfg: GroundingConfig) -> list[str]:
    """Shape only ([] = ok). Whether an id *resolves* is not a shape question and
    is not fatal -- §2.2, symmetric with the unresolvable-value rule in §3."""
    problems: list[str] = []
    if cfg.max_grounding_docs < 1:
        problems.append("grounding 'max_grounding_docs' must be at least 1")
    for key, values in sorted(cfg.grounding.items()):
        if not is_qualified(key):
            problems.append(
                f"grounding key {key!r} must be a qualified doc_id "
                "('system:native_id'); bare ids are not accepted"
            )
        for value in values:
            if not is_qualified(value):
                problems.append(
                    f"grounding value {value!r} under {key!r} must be a qualified "
                    "doc_id ('system:native_id'); bare ids are not accepted"
                )
    for i, rule in enumerate(cfg.rules, 1):
        where = f"grounding rule {i}"
        if not (rule.for_.type or rule.for_.system or rule.for_.doc):
            problems.append(
                f"{where}: 'for' needs at least one of 'type', 'system', 'doc'"
            )
        for doc_id in rule.for_.doc or []:
            if not is_qualified(doc_id):
                problems.append(
                    f"{where}: 'for.doc' entry {doc_id!r} must be a qualified "
                    "doc_id ('system:native_id')"
                )
        if not rule.match:
            problems.append(f"{where}: 'match' needs at least one phrase")
        for phrase in rule.match:
            if not phrase.strip():
                problems.append(f"{where}: a 'match' phrase is blank")
            elif template_fields(phrase) is None:
                problems.append(
                    f"{where}: 'match' phrase {phrase!r} has unpaired braces; "
                    "fields are {name}"
                )
        if rule.newest < 1:
            problems.append(f"{where}: 'newest' must be at least 1")
    return problems


def declared_ids(doc: CanonicalDocument, cfg: GroundingConfig) -> list[str]:
    """Both declaration sites, unioned and sorted. Sorted because everything
    downstream -- the cap, the sidecar, the diff -- must be deterministic."""
    return sorted(set(doc.grounded_by) | set(cfg.grounding.get(doc.doc_id, [])))


def resolve(
    owner: CanonicalDocument,
    ids: list[str],
    by_id: dict[str, CanonicalDocument],
    *,
    max_docs: int,
) -> tuple[list[CanonicalDocument], list[str]]:
    """Declared ids -> the documents that will actually be cited, plus notes.

    Nothing here raises: an unresolvable or tombstoned target is a fact about
    another system's sync state, not an error in this run (§3)."""
    notes: list[str] = []
    seen = {resource_key(owner.anchor)}
    kept: list[CanonicalDocument] = []

    for gid in sorted(set(ids)):
        if gid == owner.doc_id:
            continue  # self-reference: silent, not a note
        doc = by_id.get(gid)
        if doc is None:
            notes.append(
                f"{concept_path(owner.doc_id)}: grounding {gid} was not found in "
                "the mirror or this fetch and was dropped"
            )
            continue
        if doc.deleted:
            notes.append(
                f"{concept_path(owner.doc_id)}: grounding {gid} is tombstoned "
                "upstream and was dropped"
            )
            continue
        key = resource_key(doc.anchor)
        if key in seen:
            continue  # same artifact, cited once
        seen.add(key)
        kept.append(doc)

    if len(kept) > max_docs:
        dropped = ", ".join(d.doc_id for d in kept[max_docs:])
        notes.append(
            f"{concept_path(owner.doc_id)}: grounding capped at {max_docs}; "
            f"dropped {dropped}"
        )
        kept = kept[:max_docs]
    return kept, notes


SIDECAR_DIR = "_grounding"
"""A subdirectory, deliberately: `load_all` globs `mirror/*.json`, so a sidecar
at the root would be parsed as a CanonicalDocument on every run."""


def _sidecar(mirror: Path, doc_id: str) -> Path:
    return mirror / SIDECAR_DIR / f"{slot_key(doc_id)}.json"


def read_sidecar(mirror: Path, doc_id: str) -> dict[str, str] | None:
    """The grounding hashes recorded when this concept was last published, or
    None if it has never been grounded.

    An unreadable sidecar reads as never-grounded rather than raising. This is
    recoverable state, not a corrupt mirror: it records what a past run did, and
    the repair -- republish the concept and rewrite it -- is exactly what the
    never-grounded answer produces. Raising instead wedges every later run on the
    shared mirror, permanently, because nothing on the failing path ever reaches
    `delete_sidecar`."""
    path = _sidecar(mirror, doc_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text("utf-8"))
        return {str(k): str(v) for k, v in payload["grounding"].items()}
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return None


def _write_atomic(path: Path, payload: dict) -> None:
    """Through a unique temp file in the same directory, so a process killed
    mid-write leaves the old file or the new one, never a truncated one, and two
    writers on the shared mirror never `os.replace` each other's half-written
    file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name, not `<slot>.json.tmp`: a fixed one is the same path for
    # every writer, so two runs on the shared mirror can `os.replace` each
    # other's half-written file into the live slot — defeating the atomicity the
    # temp file is here for. The finally-unlink covers the crash-between case,
    # which would otherwise leave an orphan invisible to glob queries like
    # `*.json` used by `has_sidecars`, `load_first_seen`, and `delete_sidecar`.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_sidecar(mirror: Path, doc_id: str, recorded: dict[str, str]) -> None:
    """Written atomically (`_write_atomic`); `read_sidecar` tolerates a torn file
    anyway, and this keeps them from being made."""
    payload = {"doc_id": doc_id, "grounding": dict(sorted(recorded.items()))}
    _write_atomic(_sidecar(mirror, doc_id), payload)


def delete_sidecar(mirror: Path, doc_id: str) -> None:
    """Idempotent. Called when a grounding set empties and when an owner is
    tombstoned -- NOT writing a file does not remove the one already there, and
    a stale sidecar re-synthesizes its document on every run forever (§4)."""
    _sidecar(mirror, doc_id).unlink(missing_ok=True)


def has_sidecars(mirror: Path) -> bool:
    """Cheap gate for the drift scan: a directory listing, not a mirror load."""
    directory = mirror / SIDECAR_DIR
    return directory.is_dir() and any(directory.glob("*.json"))


def drifted(
    mirror: Path,
    candidates: list[CanonicalDocument],
    resolved: dict[str, list[str]],
    hashes: dict[str, str],
) -> list[str]:
    """Owning doc_ids whose grounding moved since they were last published.

    `resolved` must be POST-resolution, matching what `write_sidecar` recorded.
    Comparing declared ids instead leaves an unresolvable id permanently present
    on one side and absent on the other, re-synthesizing forever (§4)."""
    out: list[str] = []
    for doc in candidates:
        # A missing sidecar is an EMPTY recorded set, not "exempt from drift".
        # Declared grounding that was unresolvable at first publish resolves to
        # nothing, so the pipeline deletes rather than writes the sidecar --
        # and on any fresh multi-system deployment the first system to run has
        # none of the others in the mirror. Skipping here would strand every
        # concept it published, ungrounded forever, once the others synced.
        # This cannot loop: a document declaring nothing has
        # `current == set() == recorded`, and `any(...)` over `{}` is False.
        recorded = read_sidecar(mirror, doc.doc_id) or {}
        current = set(resolved.get(doc.doc_id, []))
        if current != set(recorded):  # rule 3, and rule 2 by construction
            out.append(doc.doc_id)
            continue
        if any(hashes.get(gid) != h for gid, h in recorded.items()):  # rule 1
            out.append(doc.doc_id)
    return sorted(out)


FIRST_SEEN_DIR = "_first_seen"
"""When a document first entered the mirror: the recency fallback for rules
whose date facet is absent (design note 2026-09-18 §5). A subdirectory for the
same reason as SIDECAR_DIR."""


def _first_seen_path(mirror: Path, doc_id: str) -> Path:
    return mirror / FIRST_SEEN_DIR / f"{slot_key(doc_id)}.json"


def _read_first_seen(path: Path) -> tuple[str, datetime] | None:
    """Tolerant like `read_sidecar`: an unreadable record reads as absent and is
    rewritten on the document's next commit, rather than wedging every run."""
    try:
        payload = json.loads(path.read_text("utf-8"))
        moment = datetime.fromisoformat(payload["first_seen"])
        return str(payload["doc_id"]), (
            moment if moment.tzinfo else moment.replace(tzinfo=UTC)
        )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
        return None


def record_first_seen(mirror: Path, docs: list[CanonicalDocument]) -> None:
    """Write-once, for documents a publishing run commits. Each run records only
    its own documents, so no run writes another connector's state."""
    for doc in docs:
        if doc.deleted:
            continue
        path = _first_seen_path(mirror, doc.doc_id)
        if _read_first_seen(path) is not None:
            continue
        when = doc.anchor.retrieved_at
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        _write_atomic(path, {"doc_id": doc.doc_id, "first_seen": when.isoformat()})


def load_first_seen(mirror: Path) -> dict[str, datetime]:
    directory = mirror / FIRST_SEEN_DIR
    if not directory.is_dir():
        return {}
    records = (_read_first_seen(p) for p in sorted(directory.glob("*.json")))
    return dict(r for r in records if r is not None)


def delete_first_seen(mirror: Path, doc_id: str) -> None:
    """Idempotent; called when a document is tombstoned."""
    _first_seen_path(mirror, doc_id).unlink(missing_ok=True)
