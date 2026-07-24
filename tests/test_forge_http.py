import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from kbforge.publishers._http import ForgeError, request


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_request_sends_json_and_parses_response(monkeypatch):
    seen = {}

    def fake_urlopen(req):
        seen["method"] = req.method
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data)
        seen["auth"] = req.get_header("Authorization")
        seen["content_type"] = req.get_header("Content-type")
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = request(
        "POST",
        "https://api.example/things",
        headers={"Authorization": "Bearer t0ken"},
        payload={"a": 1},
    )

    assert out == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.example/things"
    assert seen["payload"] == {"a": 1}
    assert seen["auth"] == "Bearer t0ken"
    assert seen["content_type"] == "application/json"


def test_request_without_payload_sends_no_body(monkeypatch):
    seen = {}

    def fake_urlopen(req):
        seen["data"] = req.data
        seen["method"] = req.method
        return _Resp(b'{"default_branch": "main"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    out = request("GET", "https://api.example/repo", headers={})
    assert out == {"default_branch": "main"}
    assert seen["data"] is None
    assert seen["method"] == "GET"


def test_request_returns_none_for_empty_body(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req: _Resp(b""))
    assert request("DELETE", "https://api.example/x", headers={}) is None


def test_http_error_becomes_forge_error_with_status_and_body(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.HTTPError(
            req.full_url,
            422,
            "Unprocessable",
            Message(),
            io.BytesIO(b'{"message": "nope"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request("PATCH", "https://api.example/x", headers={}, payload={})

    assert exc.value.status == 422
    assert exc.value.url == "https://api.example/x"
    assert "nope" in exc.value.body


def test_forge_error_never_exposes_the_token(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.HTTPError(
            req.full_url,
            401,
            "Unauthorized",
            Message(),
            io.BytesIO(b'{"message": "bad creds"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request(
            "GET",
            "https://api.example/x",
            headers={"Authorization": "Bearer s3cret"},
        )

    assert "s3cret" not in str(exc.value)
    assert "s3cret" not in repr(exc.value)
    assert exc.value.__cause__ is None  # chained HTTPError suppressed


def test_url_error_becomes_forge_error_with_status_zero(monkeypatch):
    def fake_urlopen(req):
        raise urllib.error.URLError("name resolution failed")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ForgeError) as exc:
        request("GET", "https://api.example/x", headers={})

    assert exc.value.status == 0
    assert "name resolution failed" in exc.value.body
