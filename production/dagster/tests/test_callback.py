from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from auris_flow_dagster.callback import (
    CompletionCallbackClient,
    CompletionCallbackError,
    CompletionKeyring,
    canonical_signature_message,
)
from auris_flow_dagster.contracts import AurisRunContext

SIGNING_VALUE = "unit-only-dagster-signing-value-000000000001"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_callback_matches_bff_hmac_contract_and_never_sends_keyring(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 2.0
        captured.append(request)
        return FakeResponse({"data": {"status": "success"}})

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        timeout_seconds=2.0,
        opener=open_request,
        clock=lambda: datetime(2026, 7, 18, 10, 30, tzinfo=UTC),
        nonce_factory=lambda: "nonce-dagster-attempt-1",
    )
    response = client.post(
        scope,
        dagster_run_id="dg-run-001",
        status="success",
        result_ref={"execution_contract": "auris-flow-generic-v1"},
        metrics={"processed": 1},
    )

    assert response["data"]["status"] == "success"
    request = captured[0]
    body = request.data or b""
    payload = json.loads(body)
    assert payload["completion_receipt_id"] == "dagster:dg-run-001"
    assert payload["external_id"] == "dg-run-001"
    assert payload["source"] == "dagster"
    assert SIGNING_VALUE.encode() not in body
    assert request.get_header("X-tenant-id") == scope.tenant_id
    assert request.get_header("X-project-id") == scope.project_id
    assert request.get_header("Idempotency-key") == "dagster-completion:dg-run-001"

    expected_message = canonical_signature_message(
        method="POST",
        path=f"/api/v1/runs/{scope.run_id}/external-completion-receipts",
        query="",
        scope=scope,
        idempotency_key="dagster-completion:dg-run-001",
        timestamp="2026-07-18T10:30:00+00:00",
        nonce="nonce-dagster-attempt-1",
        key_id="dagster-2026-01",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
    expected_signature = hmac.new(
        SIGNING_VALUE.encode(),
        expected_message.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.get_header("X-auris-signature") == f"sha256={expected_signature}"


def test_callback_retry_keeps_business_idempotency_but_rotates_nonce(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []
    nonces = iter(("nonce-attempt-one", "nonce-attempt-two"))

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        captured.append(request)
        if len(captured) == 1:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return FakeResponse({"data": {"status": "success"}})

    sleeps: list[float] = []
    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        max_attempts=2,
        opener=open_request,
        nonce_factory=lambda: next(nonces),
        sleeper=sleeps.append,
    )
    client.post(scope, dagster_run_id="dg-run-retry", status="success")

    assert len(captured) == 2
    assert captured[0].data == captured[1].data
    assert captured[0].get_header("Idempotency-key") == captured[1].get_header("Idempotency-key")
    assert captured[0].get_header("X-auris-nonce") != captured[1].get_header("X-auris-nonce")
    assert sleeps == [0.25]


def test_key_rotation_requires_explicit_active_id_for_overlapping_keys(
    scope: AurisRunContext,
) -> None:
    mapping = {
        key_id: {
            "secret": f"unit-only-rotation-signing-value-{suffix:012d}",
            "allowed_sources": ["dagster"],
            "allowed_scopes": [{"tenant_id": scope.tenant_id, "project_id": scope.project_id}],
        }
        for suffix, key_id in enumerate(("dagster-old", "dagster-new"), start=1)
    }
    keyring = CompletionKeyring.from_mapping(mapping)
    with pytest.raises(CompletionCallbackError, match="active completion key id"):
        keyring.select(scope, active_key_id=None)
    assert keyring.select(scope, active_key_id="dagster-new").key_id == "dagster-new"


def test_keyring_ignores_valid_keys_owned_by_other_completion_sources(
    scope: AurisRunContext,
) -> None:
    mapping = {
        "dagster-active": {
            "secret": "unit-only-dagster-owned-value-00000000000001",
            "allowed_sources": ["dagster"],
            "allowed_scopes": [{"tenant_id": scope.tenant_id, "project_id": scope.project_id}],
        },
        "object-storage-only": {
            "secret": "unit-only-storage-owned-value-00000000000001",
            "allowed_sources": ["object_storage"],
            "allowed_scopes": [{"tenant_id": scope.tenant_id, "project_id": scope.project_id}],
        },
    }
    keyring = CompletionKeyring.from_mapping(mapping)
    assert keyring.select(scope, active_key_id=None).key_id == "dagster-active"


def test_keyring_and_http_errors_do_not_leak_secret_values(
    tmp_path: Path,
    scope: AurisRunContext,
) -> None:
    canary = "canary-sensitive-signing-material-000000000001"
    invalid = tmp_path / "invalid-keyring"
    invalid.write_text(f'{{"broken": "{canary}"', encoding="utf-8")
    with pytest.raises(CompletionCallbackError) as captured:
        CompletionKeyring.from_file(invalid)
    assert canary not in str(captured.value)

    valid = tmp_path / "valid-keyring"
    valid.write_text(
        json.dumps(
            {
                "dagster-canary": {
                    "secret": canary,
                    "allowed_sources": ["dagster"],
                    "allowed_scopes": [
                        {"tenant_id": scope.tenant_id, "project_id": scope.project_id}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    def rejected(request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        raise HTTPError(request.full_url, 401, canary, {}, None)

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=valid,
        opener=rejected,
        max_attempts=1,
    )
    with pytest.raises(CompletionCallbackError) as delivery:
        client.post(scope, dagster_run_id="dg-secret-check", status="success")
    assert canary not in str(delivery.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///run/secrets/key",
        "http://user:password@bff:8000",
        "http://external.example.com",
        "http://bff:8000?secret=query",
    ],
)
def test_production_callback_url_fails_closed(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    with pytest.raises(CompletionCallbackError):
        CompletionCallbackClient(base_url=url)
