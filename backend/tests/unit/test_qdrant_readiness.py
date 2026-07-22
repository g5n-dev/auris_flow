from __future__ import annotations

import json
from http.client import BadStatusLine, IncompleteRead
from typing import Any

import pytest

from app.main import QDRANT_READINESS_MAX_BYTES, probe_qdrant_collections


class _Response:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self._body[:limit]


def _json_response(payload: Any, *, status: int = 200) -> _Response:
    return _Response(json.dumps(payload).encode("utf-8"), status=status)


def test_qdrant_readiness_requires_bounded_collections_shape(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def healthy_open(request, *, timeout: float):
        observed["url"] = request.full_url
        observed["api_key"] = request.headers.get("Api-key")
        observed["timeout"] = timeout
        return _json_response(
            {"result": {"collections": [{"name": "auris_assets"}]}, "status": "ok"}
        )

    monkeypatch.setattr("app.main.urlopen", healthy_open)

    assert probe_qdrant_collections("http://qdrant:6333", "scoped-key") == "ok"
    assert observed == {
        "url": "http://qdrant:6333/collections",
        "api_key": "scoped-key",
        "timeout": 0.25,
    }


@pytest.mark.parametrize(
    "response",
    (
        _Response(b"", status=200),
        _Response(b"<html>proxy</html>", status=200),
        _json_response([]),
        _json_response({"status": "ok"}),
        _json_response({"result": {"collections": "not-a-list"}, "status": "ok"}),
        _json_response({"result": {"collections": ["not-an-object"]}, "status": "ok"}),
        _json_response({"result": {"collections": [{}]}, "status": "ok"}),
        _json_response({"result": {"collections": [{"name": ""}]}, "status": "ok"}),
        _json_response(
            {
                "result": {"collections": [{"name": "duplicate"}, {"name": "duplicate"}]},
                "status": "ok",
            }
        ),
        _json_response({"result": {"collections": []}, "status": "error"}),
        _json_response({"result": {"collections": []}}, status=401),
        _Response(b"x" * (QDRANT_READINESS_MAX_BYTES + 1)),
    ),
)
def test_qdrant_readiness_rejects_non_authoritative_200_responses(
    monkeypatch,
    response: _Response,
) -> None:
    monkeypatch.setattr("app.main.urlopen", lambda *_args, **_kwargs: response)

    assert probe_qdrant_collections("http://qdrant:6333", "scoped-key") == "not_ready"


@pytest.mark.parametrize(
    "failure",
    (BadStatusLine("malformed qdrant status line"), IncompleteRead(b"partial", 42)),
)
def test_qdrant_readiness_maps_malformed_http_to_not_ready(monkeypatch, failure: Exception) -> None:
    def fail_probe(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr("app.main.urlopen", fail_probe)

    assert probe_qdrant_collections("http://qdrant:6333", "scoped-key") == "not_ready"
