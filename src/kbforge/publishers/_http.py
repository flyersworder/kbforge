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


# urlopen() without this blocks on socket.getdefaulttimeout(), which is None:
# a black-holed connection would wedge the pipeline forever rather than fail.
# Generous enough for a slow forge, finite enough that a scheduled sync cannot
# hang indefinitely.
TIMEOUT_SECONDS = 30


class PublishError(RuntimeError):
    """Base for publish failures the CLI reports as a message, not a traceback.

    Subclassed rather than caught by type in the CLI so a new failure mode
    cannot be added without inheriting the user-facing treatment — the way
    TreeListingTruncatedError originally was.
    """


class ForgeError(PublishError):
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
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # `from None` so the chained HTTPError — which holds the Request — can
        # never surface a header in a traceback.
        raise ForgeError(exc.code, url, exc.read().decode("utf-8", "replace")) from None
    except urllib.error.URLError as exc:
        raise ForgeError(0, url, str(exc.reason)) from None
    except TimeoutError:
        # A *connect* timeout arrives wrapped as URLError above, but a read
        # timeout is raised bare from resp.read(); without this it would escape
        # as an unhandled exception rather than a publish failure.
        raise ForgeError(0, url, f"timed out after {TIMEOUT_SECONDS}s") from None
    return json.loads(raw) if raw else None
