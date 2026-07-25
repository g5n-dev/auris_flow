from __future__ import annotations

import re

import pytest

from app.main import settings

SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@pytest.mark.parametrize(
    "idempotency_key",
    [
        "x" * 129,
        "invalid key with spaces",
        "-cannot-start-with-punctuation",
        "invalid/key",
    ],
)
def test_invalid_idempotency_key_is_rejected_without_reflection(
    client,
    auth_headers,
    idempotency_key: str,
) -> None:
    response = client.post(
        "/api/v1/connectors",
        json={"connector_id": "invalid_idempotency", "name": "非法幂等键"},
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["idempotency_key"] is None
    assert idempotency_key not in response.text


def test_idempotency_key_accepts_the_128_character_boundary(
    client,
    auth_headers,
) -> None:
    idempotency_key = "a" * 128
    response = client.post(
        "/api/v1/connectors",
        json={"connector_id": "boundary_idempotency", "name": "幂等键边界"},
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 201, response.text


def test_duplicate_idempotency_headers_fail_closed_without_reflection(client) -> None:
    response = client.get(
        "/healthz",
        headers=[
            ("Idempotency-Key", "first-valid-key"),
            ("Idempotency-Key", "second-valid-key"),
        ],
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["code"] == "header_duplicate"
    assert error["idempotency_key"] is None
    assert "first-valid-key" not in response.text
    assert "second-valid-key" not in response.text


@pytest.mark.parametrize(
    "caller_request_id",
    [
        "x" * 129,
        "request id with spaces",
        "request/id",
    ],
)
def test_unsafe_request_id_is_replaced_before_response_and_error_envelopes(
    client,
    caller_request_id: str,
) -> None:
    response = client.get(
        "/healthz",
        headers={"X-Request-Id": caller_request_id},
    )

    assert response.status_code == 200
    generated = response.headers["X-Request-Id"]
    assert generated != caller_request_id
    assert SAFE_REQUEST_ID.fullmatch(generated)
    assert caller_request_id not in response.text


def test_duplicate_request_ids_are_replaced_with_one_server_id(client) -> None:
    response = client.get(
        "/healthz",
        headers=[
            ("X-Request-Id", "first-request"),
            ("X-Request-Id", "second-request"),
        ],
    )

    assert response.status_code == 200
    generated = response.headers["X-Request-Id"]
    assert generated not in {"first-request", "second-request"}
    assert SAFE_REQUEST_ID.fullmatch(generated)


def test_declared_json_body_over_limit_returns_typed_413(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 64)
    response = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "operator@example.test", "password": "x" * 128},
        headers={"X-Request-Id": "bounded-request"},
    )

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "REQUEST_BODY_TOO_LARGE"
    assert error["status"] == 413
    assert error["details"] == [{"max_bytes": 64}]
    assert error["request_id"] == "bounded-request"
    assert response.headers["X-Request-Id"] == "bounded-request"


def test_chunked_body_cannot_bypass_streaming_limit(
    client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_request_body_bytes", 64)

    def chunks():
        yield b'{"email":"operator@example.test","password":"'
        yield b"x" * 128
        yield b'"}'

    response = client.post(
        "/api/v1/auth/dev-login",
        content=chunks(),
        headers={
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"


def test_ambiguous_request_framing_fails_closed(client) -> None:
    response = client.post(
        "/api/v1/auth/dev-login",
        content=b"{}",
        headers={
            "Content-Length": "2",
            "Transfer-Encoding": "chunked",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REQUEST_FRAMING_AMBIGUOUS"


def test_duplicate_content_length_fails_closed(client) -> None:
    response = client.post(
        "/api/v1/auth/dev-login",
        content=b"{}",
        headers=[
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Content-Length", "2"),
        ],
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "REQUEST_FRAMING_AMBIGUOUS"
