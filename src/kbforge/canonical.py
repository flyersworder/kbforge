"""Canonicalization: stable content hashing, the §4.3 law-1 stability check, the
§4.2 fetch-side contract law, and the blankness helper both this module and
`validate` use to judge whether a string carries visible content."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Callable, Sequence

from kbforge.models import CanonicalDocument, RawRecord


class StabilityError(RuntimeError):
    """normalize() produced different canonical content for identical input."""


def is_blank(value: object) -> bool:
    """True when `value` is not a string, or is a string carrying no content.

    `str.strip()` removes NBSP (it is `Zs`) but not U+200B and friends, which are
    `Cf` — invisible, zero-width, and routinely present in text pasted out of a
    browser. A concept whose `type` is a zero-width space is untyped in every way
    that matters, so blankness has to be judged on visible content."""
    if not isinstance(value, str):
        return True
    return not "".join(
        ch for ch in value if unicodedata.category(ch) not in ("Cf", "Zs", "Cc")
    ).strip()


def content_hash(doc: CanonicalDocument) -> str:
    """SHA-256 over the canonical CONTENT — everything the diff must react to.
    The anchor is excluded: `retrieved_at` is volatile (§4.3 law 2) and the
    anchor's own content_hash would be circular."""
    payload = {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "text": doc.text,
        "structured": doc.structured,
        "relations": sorted(doc.relations),
        "deleted": doc.deleted,
    }
    # default=str keeps determinism while tolerating YAML-parsed date/datetime
    # values (PyYAML turns bare `2024-05-01` into a date, which json can't dump).
    blob = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def assert_stability(
    normalize: Callable[[Sequence[RawRecord]], list[CanonicalDocument]],
    records: Sequence[RawRecord],
) -> None:
    """§4.3 law 1: normalize twice over identical input, require identical output.

    Compared as `(content_hash, sorted(grounded_by))` rather than the hash alone.
    `grounded_by` is deliberately NOT in `content_hash`'s payload — that payload
    is an explicit allowlist, and adding a key would change every document's hash
    and re-synthesize the whole mirror on first upgrade (§7.1) — but cross-source
    grounding makes it a re-synthesis driver via drift rule 3. A connector whose
    `normalize` emitted it nondeterministically would pass this law while
    re-synthesizing its documents forever, so the law covers it beside the hash
    instead of inside it.

    A connector that fails is not deterministic and must be rejected."""

    def fingerprint(docs: list[CanonicalDocument]) -> list[tuple[str, list[str]]]:
        return [(content_hash(d), sorted(d.grounded_by)) for d in docs]

    if fingerprint(normalize(records)) != fingerprint(normalize(records)):
        raise StabilityError("normalize() is not deterministic over identical input")


class FetchContractError(RuntimeError):
    """A connector's fetch output violates the fetch-side contract."""


def assert_fetch_contract(docs: Sequence[CanonicalDocument], *, complete: bool) -> None:
    """Fetch-side law: what a connector hands the mirror must be identifiable,
    and honest about its own coverage.

    Runs on normalize output rather than on RawRecords for two reasons: `doc_id`
    is what the mirror keys on, and tombstones only exist post-normalize —
    RawRecord has no `deleted` field.

    - **Unique `doc_id`.** Two records sharing an id both land in
      `ChangeSet.added` (diff never mutates, so `prev is None` twice), and
      `synthesize.assemble` then collapses them onto one `concept_path` with
      last-write-wins. Mirror and bundle agree afterwards, so nothing looks
      broken: one document is simply absent from the knowledge base.
    - **Non-blank `native_id`,** or the document cannot be cited — the fetch-side
      mirror of the §4.4 anchor-presence law.
    - **No tombstone from an incomplete fetch.** `complete=False` means the
      connector saw a partial slice, so absence is not evidence of deletion.

    Deliberately NOT checked: that content is verbatim. Core has no independent
    access to the source, so it cannot tell a returned document from an agent's
    summary of one. This closes the identity half of retriever-not-extractor;
    the verbatim half stays contract.

    Also not checked: that `normalize` is clock-free. `assert_stability` compares
    `content_hash`, which excludes the anchor by design, so a `datetime.now()`
    inside normalize hashes identically on both passes and passes that gate."""
    seen: set[str] = set()
    for doc in docs:
        if doc.doc_id in seen:
            raise FetchContractError(f"duplicate doc_id in fetch output: {doc.doc_id}")
        seen.add(doc.doc_id)
        if is_blank(doc.anchor.native_id):
            raise FetchContractError(f"record has no native_id: doc_id={doc.doc_id}")
        if not complete and doc.deleted:
            raise FetchContractError(
                f"incomplete fetch cannot emit a tombstone: {doc.doc_id}"
            )
