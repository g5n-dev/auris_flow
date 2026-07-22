from __future__ import annotations

import json
from typing import Any

import pytest

from app.main import OBSERVABILITY_READINESS_MAX_BYTES, probe_observability_status


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


def test_observability_readiness_requires_exact_status_and_trace_ack(monkeypatch) -> None:
    trace_id = "a" * 32
    responses = iter(
        (
            _json_response({"status": "ok", "probe_age_seconds": 0.1}),
            _json_response({"status": "ok", "trace_id": trace_id}),
        )
    )
    monkeypatch.setattr("app.main.urlopen", lambda *_args, **_kwargs: next(responses))

    assert probe_observability_status("http://observability-health:8080/ready") == "ok"
    assert (
        probe_observability_status(
            f"http://observability-health:8080/traces/{trace_id}",
            expected_trace_id=trace_id,
            timeout_seconds=0.75,
        )
        == "ok"
    )


@pytest.mark.parametrize(
    "response",
    (
        _Response(b"<html>catch-all proxy</html>"),
        _Response(b""),
        _json_response([]),
        _json_response({"status": "not_ready"}),
        _json_response({"status": "ok"}, status=503),
        _Response(b"x" * (OBSERVABILITY_READINESS_MAX_BYTES + 1)),
    ),
)
def test_observability_readiness_rejects_ambiguous_or_unbounded_200(
    monkeypatch,
    response: _Response,
) -> None:
    monkeypatch.setattr("app.main.urlopen", lambda *_args, **_kwargs: response)

    assert probe_observability_status("http://observability-health:8080/ready") == "not_ready"


def test_observability_trace_probe_rejects_wrong_or_missing_trace_id(monkeypatch) -> None:
    expected = "a" * 32
    responses = iter(
        (
            _json_response({"status": "ok"}),
            _json_response({"status": "ok", "trace_id": "b" * 32}),
        )
    )
    monkeypatch.setattr("app.main.urlopen", lambda *_args, **_kwargs: next(responses))

    for _ in range(2):
        assert (
            probe_observability_status(
                f"http://observability-health:8080/traces/{expected}",
                expected_trace_id=expected,
            )
            == "not_ready"
        )
