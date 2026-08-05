from __future__ import annotations

import hashlib
import hmac
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from auris_flow_dagster import callback as callback_module
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


def progress_conflict(request: Request, code: str) -> HTTPError:
    body = json.dumps({"error": {"code": code}}).encode("utf-8")
    return HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(body))


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


def test_completion_callback_accepts_materializing_ack_without_claiming_business_success(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        del request, timeout
        # urllib treats every 2xx response, including the BFF's 202
        # completion_pending acknowledgement, as a normal response.
        return FakeResponse(
            {
                "data": {
                    "status": "completion_pending",
                    "business_status": "materializing",
                    "receipt_state": "materializing",
                }
            }
        )

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=open_request,
    )
    response = client.post(
        scope,
        dagster_run_id="dg-run-materializing",
        status="success",
    )

    assert response["data"] == {
        "status": "completion_pending",
        "business_status": "materializing",
        "receipt_state": "materializing",
    }


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


def test_progress_callback_reuses_hmac_contract_with_stage_idempotency(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        assert timeout == 2
        captured.append(request)
        return FakeResponse({"data": {"current_stage": "downloading"}})

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        timeout_seconds=2,
        opener=open_request,
        clock=lambda: datetime(2026, 7, 18, 10, 31, tzinfo=UTC),
        nonce_factory=lambda: "nonce-progress-downloading",
    )

    response = client.post_progress(
        scope,
        dagster_run_id="dg-import-001",
        import_batch_id="import_batch_001",
        stage="downloading",
    )

    assert response["data"]["current_stage"] == "downloading"
    request = captured[0]
    body = request.data or b""
    assert request.full_url.endswith(f"/api/v1/runs/{scope.run_id}/external-progress-receipts")
    assert json.loads(body) == {
        "adapter": "dagster",
        "source": "dagster",
        "progress_receipt_id": "dagster:dg-import-001:downloading",
        "external_id": "dg-import-001",
        "tenant_id": scope.tenant_id,
        "project_id": scope.project_id,
        "task_run_id": scope.run_id,
        "import_batch_id": "import_batch_001",
        "stage": "downloading",
    }
    idempotency_key = "dagster-progress:dg-import-001:downloading"
    assert request.get_header("Idempotency-key") == idempotency_key
    expected_message = canonical_signature_message(
        method="POST",
        path=f"/api/v1/runs/{scope.run_id}/external-progress-receipts",
        query="",
        scope=scope,
        idempotency_key=idempotency_key,
        timestamp="2026-07-18T10:31:00+00:00",
        nonce="nonce-progress-downloading",
        key_id="dagster-2026-01",
        body_sha256=hashlib.sha256(body).hexdigest(),
    )
    expected_signature = hmac.new(
        SIGNING_VALUE.encode(),
        expected_message.encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.get_header("X-auris-signature") == f"sha256={expected_signature}"


def test_progress_callback_waits_through_dispatch_listing_race(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []
    sleeps: list[float] = []
    nonces = iter(f"nonce-progress-race-{attempt}" for attempt in range(1, 6))

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        captured.append(request)
        if len(captured) < 5:
            raise progress_conflict(
                request,
                "AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_MISSING",
            )
        return FakeResponse({"data": {"current_stage": "downloading"}})

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=open_request,
        nonce_factory=lambda: next(nonces),
        sleeper=sleeps.append,
    )

    client.post_progress(
        scope,
        dagster_run_id="dg-import-dispatch-race",
        import_batch_id="import_batch_001",
        stage="downloading",
    )

    assert len(captured) == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
    assert {request.data for request in captured} == {captured[0].data}
    assert {request.get_header("Idempotency-key") for request in captured} == {
        "dagster-progress:dg-import-dispatch-race:downloading"
    }
    assert len({request.get_header("X-auris-nonce") for request in captured}) == 5


def test_downloading_progress_uses_frozen_deadline_as_registration_barrier(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []
    sleeps: list[float] = []
    now = [datetime(2026, 7, 18, 10, 31, tzinfo=UTC)]

    def sleep_and_advance(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        captured.append(request)
        if len(captured) < 7:
            raise progress_conflict(
                request,
                "AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_MISSING",
            )
        return FakeResponse({"data": {"current_stage": "downloading"}})

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=open_request,
        clock=lambda: now[0],
        nonce_factory=lambda: f"nonce-progress-barrier-{len(captured) + 1}",
        sleeper=sleep_and_advance,
    )

    client.post_progress(
        scope,
        dagster_run_id="dg-import-registration-barrier",
        import_batch_id="import_batch_001",
        stage="downloading",
        deadline_at=datetime(2026, 7, 18, 10, 32, tzinfo=UTC),
    )

    assert len(captured) == 7
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 4.0, 4.0]
    assert {request.data for request in captured} == {captured[0].data}
    assert len({request.get_header("X-auris-nonce") for request in captured}) == 7


def test_progress_callback_fails_immediately_for_non_registration_conflict(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    captured: list[Request] = []
    sleeps: list[float] = []

    def open_request(request: Request, *, timeout: float) -> FakeResponse:
        del timeout
        captured.append(request)
        raise progress_conflict(request, "AUDIO_IMPORT_PROGRESS_OUT_OF_ORDER")

    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=open_request,
        clock=lambda: datetime(2026, 7, 18, 10, 31, tzinfo=UTC),
        sleeper=sleeps.append,
    )

    with pytest.raises(CompletionCallbackError, match=r"progress callback failed \(HTTP 409\)"):
        client.post_progress(
            scope,
            dagster_run_id="dg-import-out-of-order",
            import_batch_id="import_batch_001",
            stage="downloading",
            deadline_at=datetime(2026, 7, 18, 10, 32, tzinfo=UTC),
        )

    assert len(captured) == 1
    assert sleeps == []


def test_progress_callback_requires_exact_persisted_stage_acknowledgement(
    scope: AurisRunContext,
    keyring_file: Path,
) -> None:
    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=lambda *_args, **_kwargs: FakeResponse({"data": {"current_stage": "listing"}}),
    )

    with pytest.raises(CompletionCallbackError, match="acknowledgement"):
        client.post_progress(
            scope,
            dagster_run_id="dg-import-unpersisted-stage",
            import_batch_id="import_batch_001",
            stage="downloading",
        )


@pytest.mark.parametrize("stage", ["queued", "listing", "materializing", "completed", "failed"])
def test_progress_callback_only_allows_executor_owned_stages(
    scope: AurisRunContext,
    keyring_file: Path,
    stage: str,
) -> None:
    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        opener=lambda *_args, **_kwargs: pytest.fail("invalid stage must not be sent"),
    )

    with pytest.raises(CompletionCallbackError, match="progress stage"):
        client.post_progress(
            scope,
            dagster_run_id="dg-import-invalid-stage",
            import_batch_id="import_batch_001",
            stage=stage,
        )


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


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_default_callback_opener_rejects_redirect_before_replaying_signed_request(
    scope: AurisRunContext,
    keyring_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    requests: list[Request] = []
    installed_handlers: list[object] = []

    class RedirectingDirector:
        def open(self, request: Request, *, timeout: float) -> Any:
            del timeout
            requests.append(request)
            handler = next(
                item
                for item in installed_handlers
                if isinstance(item, callback_module._RejectRedirectHandler)
            )
            redirect = getattr(handler, f"http_error_{status}")
            redirect(
                request,
                None,
                status,
                "redirect",
                {"Location": "http://attacker.invalid/replay"},
            )
            pytest.fail("redirect handler must raise before issuing a second request")

    def fake_build_opener(*handlers: object) -> RedirectingDirector:
        installed_handlers.extend(handlers)
        return RedirectingDirector()

    monkeypatch.setattr(callback_module, "build_opener", fake_build_opener)
    client = CompletionCallbackClient(
        base_url="http://bff:8000",
        keyring_path=keyring_file,
        max_attempts=1,
    )

    with pytest.raises(CompletionCallbackError):
        client.post(scope, dagster_run_id="dg-no-redirect", status="success")

    assert len(requests) == 1
    assert requests[0].get_header("X-auris-signature", "").startswith("sha256=")
