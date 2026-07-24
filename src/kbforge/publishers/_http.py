"""Minimal JSON-over-HTTP for forge publishers, on stdlib urllib. Deliberately
not httpx/requests: the forge adapters make fewer than ten calls against known
endpoints, so kbforge's runtime dependency list stays pluggy/pydantic/pyyaml."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# What an adapter accepts for injection so its tests never touch the network.
Transport = Callable[..., Any]


class ForgeError(RuntimeError):
    """A forge API call failed.

    Carries status, URL and *response* body only. Request headers are never
    captured, so the token cannot reach a log, a traceback, or CI output.
    """

    def __init__(self, status: int, url: str, body: str) -> None:
        super().__init__(f"{status} from {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict | list | None = None,
) -> Any:
    """Perform one JSON request. Returns the decoded body, or None if empty."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # `from None` so the chained HTTPError — which holds the Request — can
        # never surface a header in a traceback.
        raise ForgeError(exc.code, url, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ForgeError(0, url, str(exc.reason)) from None
    return json.loads(raw) if raw else None
