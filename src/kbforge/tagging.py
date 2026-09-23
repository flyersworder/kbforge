"""Deterministic phrase matching: one rule shared by grounding rules and keyword
tags, so "this document mentions X" cannot mean two things.

Case-insensitive, on word boundaries, after NFC normalization, and always
literal: a phrase like `C++` or `800 V (DC)` is escaped, never read as a regex."""

from __future__ import annotations

import re
import unicodedata


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    # `(?<!\w)`/`(?!\w)` rather than `\b`: `\b` needs a word character on one
    # side, so it never matches around a phrase that starts or ends in `+`.
    return re.compile(rf"(?<!\w){re.escape(nfc(phrase))}(?!\w)", re.IGNORECASE)


def keyword_tags(
    vocabulary: dict[str, list[str]] | None, title: str, text: str
) -> list[str]:
    """The vocabulary tags with a phrase in `title` or `text`, sorted. Pure."""
    if not vocabulary:
        return []
    haystack = nfc(f"{title}\n{text}")
    return sorted(
        tag
        for tag, phrases in vocabulary.items()
        if any(phrase_pattern(p).search(haystack) for p in phrases)
    )
