"""The two rules the web source applies, as pure functions: which URLs a search
may return, and how a scraped page is made byte-stable.

Neither rule summarizes or edits what a page *says*. `allowed` only decides what
is read; `clean_markdown` only strips link targets that change on every request,
which is volatility exclusion (kbforge architecture §4.3 law 2) applied where the
HTML-to-markdown conversion happens, not a rewrite of the page.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Hosts whose link targets carry a per-request token. Measured, not guessed: a
# page behind Cloudflare Turnstile embeds `[Refresh](https://challenges.
# cloudflare.com/cdn-cgi/challenge-platform/.../<token>)` links whose path
# changes on every fetch, so the same unchanged article hashes differently each
# run and every sync shows it as modified. Add a host here only with evidence.
VOLATILE_LINK_HOSTS = frozenset({"challenges.cloudflare.com"})

# `[text](url)`, also matching the tail of an image `![alt](url)`. The target
# stops at whitespace or `)`, which is how the scraper renders links.
_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def domains_from(value: str) -> tuple[str, ...]:
    """Parse a comma-separated allowlist. Empty is an error, not "everything":
    a search source with no allowlist would read the whole web. `*` is the
    explicit way to ask for that."""
    domains = tuple(
        d.strip().lower().lstrip(".") for d in value.split(",") if d.strip()
    )
    if not domains:
        raise ValueError(
            "WEB_SOURCE_ALLOWED_DOMAINS is empty; list the domains a search may "
            "return (comma-separated), or '*' to allow any"
        )
    return domains


def allowed(url: str, domains: tuple[str, ...]) -> bool:
    """True when `url` is http(s) and its host is one of `domains` or a
    subdomain of one. Matching is on whole labels, so `ti.com` admits
    `news.ti.com` but not `evilti.com` or `ti.com.attacker.net`."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    if "*" in domains:
        return True
    host = parts.hostname.lower()
    return any(host == d or host.endswith(f".{d}") for d in domains)


def clean_markdown(markdown: str) -> str:
    """Replace links to a volatile host with their text; leave all else as is."""

    def unlink(m: re.Match[str]) -> str:
        host = (urlsplit(m.group(2)).hostname or "").lower()
        return m.group(1) if host in VOLATILE_LINK_HOSTS else m.group(0)

    return _LINK.sub(unlink, markdown)
