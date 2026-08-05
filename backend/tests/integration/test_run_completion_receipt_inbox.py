from __future__ import annotations

import hashlib
import hmac
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.completion_signature import completion_signature_message
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.main import app
from app.models import AuditLog, RunCompletionReceipt, RunRecord, TraceRef

TEST_COMPLETION_KEY_ID = "auris-test-completion"
TEST_DAGSTER_KEY_ID = "auris-test-dagster-completion"
TEST_CALLBACK_KEY_ID = "auris-test-callback-completion"
TEST_DAGSTER_HMAC_VALUE = "auris-test-dagster-hmac-value-32-chars"
TEST_CALLBACK_HMAC_VALUE = "auris-test-callback-hmac-value-32-chars"


def _configure_key_bindings(
    monkeypatch,
    *,
    dagster_sources: tuple[str, ...] = ("dagster",),
    dagster_scope: tuple[str, str] = ("aurora_auto", "sales_qa"),
) -> None:
    tenant_id, project_id = dagster_scope
    bindings = {
        TEST_DAGSTER_KEY_ID: {
            "secret": TEST_DAGSTER_HMAC_VALUE,
            "allowed_sources": list(dagster_sources),
            "allowed_scopes": [{"tenant_id": tenant_id, "project_id": project_id}],
        },
        TEST_CALLBACK_KEY_ID: {
            "secret": TEST_CALLBACK_HMAC_VALUE,
            "allowed_sources": ["external_callback"],
            "allowed_scopes": [{"tenant_id": "aurora_auto", "project_id": "sales_qa"}],
        },
    }
    monkeypatch.setattr(
        get_settings(),
        "completion_receipt_key_bindings",
        json.dumps(bindings, separators=(",", ":")),
    )


def _add_submitted_run(
    run_id: str,
    *,
    trace_id: str,
    execution_mode: str = "diagnostic",
) -> str:
    external_run_id = f"external-{run_id}"
    with SessionLocal() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="provider_test",
                status="submitted",
                run_key=run_id,
                partition_key=None,
                trace_id=trace_id,
                payload={
                    "run_id": run_id,
                    "status": "submitted",
                    "trace_id": trace_id,
                    "execution_mode": execution_mode,
                    "business_completion_required": True,
                    "dispatch_state": "submitted",
                    "dispatch": {
                        "adapter": "dagster",
                        "details": {"external_run_id": external_run_id},
                    },
                    "affected_objects": [],
                    "next_actions": [],
                },
            )
        )
        session.commit()
    return external_run_id


def _receipt_payload(*, completion_receipt_id: str, external_run_id: str) -> dict[str, object]:
    return {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": completion_receipt_id,
        "external_id": external_run_id,
        "result_ref": {
            "object_type": "run_result",
            "object_id": f"result-{external_run_id}",
        },
        "metrics": {"processed": 1},
    }


def _signed_headers(
    *,
    path: str,
    encoded_body: bytes,
    idempotency_key: str,
    nonce: str,
    trace_id: str,
    key_id: str = TEST_COMPLETION_KEY_ID,
    source: str = "dagster",
    hmac_value: str | None = None,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
) -> dict[str, str]:
    timestamp = datetime.now(UTC).isoformat()
    body_sha256 = hashlib.sha256(encoded_body).hexdigest()
    message = completion_signature_message(
        method="POST",
        path=path,
        query="",
        tenant_id=tenant_id,
        project_id=project_id,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
        nonce=nonce,
        key_id=key_id,
        source=source,
        body_sha256=body_sha256,
    )
    signature = hmac.new(
        (hmac_value or os.environ["COMPLETION_RECEIPT_SECRET"]).encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Tenant-Id": tenant_id,
        "X-Project-Id": project_id,
        "X-Request-Id": f"request-{trace_id}",
        "X-Trace-Id": trace_id,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Auris-Key-Id": key_id,
        "X-Auris-Timestamp": timestamp,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": source,
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def test_completion_receipt_replays_once_with_a_different_idempotency_key(
    client,
    auth_headers,
) -> None:
    run_id = "receipt-inbox-replay-run"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-original-replay-run")
    payload = _receipt_payload(
        completion_receipt_id="receipt-inbox-replay",
        external_run_id=external_run_id,
    )
    path = f"/api/v1/runs/{run_id}/completion-receipts"

    first = client.post(
        path,
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "receipt-replay-request-a"},
    )
    replay = client.post(
        path,
        json=payload,
        headers={
            **auth_headers,
            "X-Trace-Id": "trace-receipt-replay-second-request",
            "Idempotency-Key": "receipt-replay-request-b",
        },
    )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    with SessionLocal() as session:
        receipt_count = session.scalar(select(func.count()).select_from(RunCompletionReceipt))
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "provider_test.completion_received",
                AuditLog.object_id == run_id,
            )
        )
        run = session.get(RunRecord, run_id)
        assert receipt_count == 1
        assert audit_count == 1
        assert run is not None
        assert run.payload["status_history"] == [
            {
                "from": "submitted",
                "to": "success",
                "reason": "dagster_completion_received",
            }
        ]


def test_local_generic_ack_cannot_complete_a_production_business_run(
    client,
    auth_headers,
) -> None:
    run_id = "receipt-inbox-production-generic-run"
    external_run_id = _add_submitted_run(
        run_id,
        trace_id="trace-production-generic-run",
        execution_mode="production",
    )

    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json=_receipt_payload(
            completion_receipt_id="receipt-production-generic-rejected",
            external_run_id=external_run_id,
        ),
        headers={
            **auth_headers,
            "Idempotency-Key": "receipt-production-generic-rejected",
        },
    )

    assert completion.status_code == 409, completion.text
    assert completion.json()["error"]["code"] == "EXECUTION_CONTRACT_NOT_CONFIGURED"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        assert "completion_receipt" not in run.payload


def test_concurrent_completion_receipt_is_materialized_once(auth_headers) -> None:
    run_id = "receipt-inbox-concurrent-run"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-original-concurrent-run")
    payload = _receipt_payload(
        completion_receipt_id="receipt-inbox-concurrent",
        external_run_id=external_run_id,
    )
    path = f"/api/v1/runs/{run_id}/completion-receipts"
    start = Barrier(2)

    def submit(index: int) -> tuple[int, dict[str, object]]:
        local_client = TestClient(app)
        try:
            start.wait(timeout=5)
            response = local_client.post(
                path,
                json=payload,
                headers={
                    **auth_headers,
                    "X-Request-Id": f"receipt-concurrent-request-{index}",
                    "X-Trace-Id": f"trace-receipt-concurrent-{index}",
                    "Idempotency-Key": f"receipt-concurrent-key-{index}",
                },
            )
            return response.status_code, response.json()
        finally:
            local_client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))

    assert [status for status, _ in responses] == [200, 200]
    assert responses[0][1] == responses[1][1]
    with SessionLocal() as session:
        receipt_count = session.scalar(select(func.count()).select_from(RunCompletionReceipt))
        audit_count = session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.action == "provider_test.completion_received",
                AuditLog.object_id == run_id,
            )
        )
        assert receipt_count == 1
        assert audit_count == 1


def test_completion_receipt_id_cannot_bind_to_another_run_or_body(
    client,
    auth_headers,
) -> None:
    first_run_id = "receipt-inbox-binding-run-a"
    second_run_id = "receipt-inbox-binding-run-b"
    first_external_id = _add_submitted_run(first_run_id, trace_id="trace-binding-run-a")
    second_external_id = _add_submitted_run(second_run_id, trace_id="trace-binding-run-b")
    receipt_id = "receipt-inbox-single-binding"

    first = client.post(
        f"/api/v1/runs/{first_run_id}/completion-receipts",
        json=_receipt_payload(
            completion_receipt_id=receipt_id,
            external_run_id=first_external_id,
        ),
        headers={**auth_headers, "Idempotency-Key": "receipt-binding-request-a"},
    )
    changed_body = _receipt_payload(
        completion_receipt_id=receipt_id,
        external_run_id=first_external_id,
    )
    changed_body["metrics"] = {"processed": 2}
    body_conflict = client.post(
        f"/api/v1/runs/{first_run_id}/completion-receipts",
        json=changed_body,
        headers={**auth_headers, "Idempotency-Key": "receipt-binding-request-body-change"},
    )
    conflict = client.post(
        f"/api/v1/runs/{second_run_id}/completion-receipts",
        json=_receipt_payload(
            completion_receipt_id=receipt_id,
            external_run_id=second_external_id,
        ),
        headers={**auth_headers, "Idempotency-Key": "receipt-binding-request-b"},
    )

    assert first.status_code == 200, first.text
    assert body_conflict.status_code == 409, body_conflict.text
    assert body_conflict.json()["error"]["code"] == "RUN_COMPLETION_RECEIPT_CONFLICT"
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "RUN_COMPLETION_RECEIPT_CONFLICT"
    with SessionLocal() as session:
        second_run = session.get(RunRecord, second_run_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == receipt_id
            )
        )
        assert second_run is not None
        assert second_run.status == "submitted"
        assert receipt is not None
        assert receipt.run_id == first_run_id


def test_signed_completion_receipt_persists_auth_and_trace_evidence(
    client,
    monkeypatch,
) -> None:
    _configure_key_bindings(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    run_id = "receipt-inbox-signed-evidence-run"
    run_trace_id = "trace-original-signed-run"
    request_trace_id = "trace-signed-receipt-request"
    nonce = "nonce-signed-receipt-evidence"
    external_run_id = _add_submitted_run(run_id, trace_id=run_trace_id)
    payload = _receipt_payload(
        completion_receipt_id="receipt-inbox-signed-evidence",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-signed-evidence-request",
            nonce=nonce,
            trace_id=request_trace_id,
            key_id=TEST_DAGSTER_KEY_ID,
            hmac_value=TEST_DAGSTER_HMAC_VALUE,
        ),
    )

    assert response.status_code == 200, response.text
    request_root_trace_id = response.json()["meta"]["trace_id"]
    assert request_root_trace_id != request_trace_id
    with SessionLocal() as session:
        receipt = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == "receipt-inbox-signed-evidence"
            )
        )
        assert receipt is not None
        assert receipt.run_id == run_id
        assert receipt.request_body == payload
        assert receipt.response_json == response.json()
        assert receipt.signature_key_id == TEST_DAGSTER_KEY_ID
        assert receipt.authenticated_source == "dagster"
        assert receipt.signature_nonce == nonce
        assert receipt.request_trace_id == request_root_trace_id
        assert receipt.run_trace_id == run_trace_id
        assert receipt.signature_request_hash
        assert receipt.signature_body_hash == hashlib.sha256(encoded).hexdigest()
        trace_link = session.scalar(
            select(TraceRef).where(TraceRef.trace_id == request_root_trace_id)
        )
        assert trace_link is not None
        assert trace_link.payload["parent_trace_id"] == request_trace_id
        assert trace_link.payload["correlation_id"] == request_trace_id


def test_signed_completion_receipt_validation_keeps_the_stable_error_envelope(
    client,
    monkeypatch,
) -> None:
    _configure_key_bindings(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_env", "prod")
    run_id = "receipt-inbox-signed-validation-run"
    external_run_id = _add_submitted_run(
        run_id,
        trace_id="trace-original-signed-validation-run",
    )
    payload = {
        **_receipt_payload(
            completion_receipt_id="receipt-inbox-signed-validation",
            external_run_id=external_run_id,
        ),
        "unexpected_internal_field": "must-be-rejected",
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-signed-validation-request",
            nonce="nonce-signed-validation-request",
            trace_id="trace-signed-validation-request",
            key_id=TEST_DAGSTER_KEY_ID,
            hmac_value=TEST_DAGSTER_HMAC_VALUE,
        ),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert response.json()["error"]["trace_id"]
    assert response.json()["error"]["request_id"] == ("request-trace-signed-validation-request")
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"
        assert session.scalar(select(func.count()).select_from(RunCompletionReceipt)) == 0


def test_external_callback_key_cannot_complete_dagster_run(client, monkeypatch) -> None:
    _configure_key_bindings(monkeypatch)
    run_id = "receipt-inbox-callback-key-dagster-run"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-callback-key-dagster-run")
    payload = _receipt_payload(
        completion_receipt_id="receipt-callback-key-dagster-run",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-callback-key-dagster-request",
            nonce="nonce-callback-key-dagster-run",
            trace_id="trace-callback-key-dagster-request",
            key_id=TEST_CALLBACK_KEY_ID,
            source="external_callback",
            hmac_value=TEST_CALLBACK_HMAC_VALUE,
        ),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "RUN_COMPLETION_AUTH_SOURCE_MISMATCH"
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"
        assert session.scalar(select(func.count()).select_from(RunCompletionReceipt)) == 0


def test_completion_key_rejects_a_source_outside_its_binding(client, monkeypatch) -> None:
    _configure_key_bindings(monkeypatch)
    run_id = "receipt-inbox-callback-key-source-denied"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-callback-source-denied")
    payload = _receipt_payload(
        completion_receipt_id="receipt-callback-source-denied",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-callback-source-denied-request",
            nonce="nonce-callback-source-denied",
            trace_id="trace-callback-source-denied-request",
            key_id=TEST_CALLBACK_KEY_ID,
            source="dagster",
            hmac_value=TEST_CALLBACK_HMAC_VALUE,
        ),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "COMPLETION_SIGNATURE_SOURCE_DENIED"


def test_signed_completion_rejects_tampered_source(client, monkeypatch) -> None:
    _configure_key_bindings(
        monkeypatch,
        dagster_sources=("dagster", "external_callback"),
    )
    run_id = "receipt-inbox-tampered-source"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-tampered-source")
    payload = _receipt_payload(
        completion_receipt_id="receipt-tampered-source",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    headers = _signed_headers(
        path=path,
        encoded_body=encoded,
        idempotency_key="receipt-tampered-source-request",
        nonce="nonce-tampered-source",
        trace_id="trace-tampered-source-request",
        key_id=TEST_DAGSTER_KEY_ID,
        source="dagster",
        hmac_value=TEST_DAGSTER_HMAC_VALUE,
    )
    headers["X-Auris-Source"] = "external_callback"

    response = client.post(path, content=encoded, headers=headers)

    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "COMPLETION_SIGNATURE_INVALID"


def test_signed_completion_rejects_payload_adapter_dispatch_conflict(
    client,
    monkeypatch,
) -> None:
    _configure_key_bindings(monkeypatch)
    run_id = "receipt-inbox-callback-dispatch-conflict"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-callback-dispatch-conflict")
    payload = _receipt_payload(
        completion_receipt_id="receipt-callback-dispatch-conflict",
        external_run_id=external_run_id,
    )
    payload["adapter"] = "external_callback"
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-callback-dispatch-conflict-request",
            nonce="nonce-callback-dispatch-conflict",
            trace_id="trace-callback-dispatch-conflict-request",
            key_id=TEST_CALLBACK_KEY_ID,
            source="external_callback",
            hmac_value=TEST_CALLBACK_HMAC_VALUE,
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RUN_COMPLETION_ADAPTER_MISMATCH"


def test_system_completion_identity_cannot_bypass_key_scope(client, monkeypatch) -> None:
    _configure_key_bindings(
        monkeypatch,
        dagster_scope=("aurora_auto", "another_project"),
    )
    run_id = "receipt-inbox-key-scope-denied"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-key-scope-denied")
    payload = _receipt_payload(
        completion_receipt_id="receipt-key-scope-denied",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-key-scope-denied-request",
            nonce="nonce-key-scope-denied",
            trace_id="trace-key-scope-denied-request",
            key_id=TEST_DAGSTER_KEY_ID,
            hmac_value=TEST_DAGSTER_HMAC_VALUE,
        ),
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "COMPLETION_SIGNATURE_SCOPE_DENIED"
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"
        assert session.scalar(select(func.count()).select_from(RunCompletionReceipt)) == 0


def test_production_rejects_legacy_single_completion_key(client, monkeypatch) -> None:
    active_settings = get_settings()
    monkeypatch.setattr(active_settings, "app_env", "prod")
    monkeypatch.setattr(active_settings, "completion_receipt_key_bindings", "")
    run_id = "receipt-inbox-production-legacy-key"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-production-legacy-key")
    payload = _receipt_payload(
        completion_receipt_id="receipt-production-legacy-key",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"

    response = client.post(
        path,
        content=encoded,
        headers=_signed_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="receipt-production-legacy-key-request",
            nonce="nonce-production-legacy-key",
            trace_id="trace-production-legacy-key-request",
        ),
    )

    assert response.status_code == 500, response.text
    assert response.json()["error"]["code"] == "COMPLETION_SIGNATURE_KEY_BINDINGS_REQUIRED"


def test_production_completion_key_bindings_reject_wildcards(client, monkeypatch) -> None:
    active_settings = get_settings()
    monkeypatch.setattr(active_settings, "app_env", "prod")
    run_id = "receipt-inbox-production-wildcard-key"
    external_run_id = _add_submitted_run(run_id, trace_id="trace-production-wildcard-key")
    payload = _receipt_payload(
        completion_receipt_id="receipt-production-wildcard-key",
        external_run_id=external_run_id,
    )
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    headers = _signed_headers(
        path=path,
        encoded_body=encoded,
        idempotency_key="receipt-production-wildcard-key-request",
        nonce="nonce-production-wildcard-key",
        trace_id="trace-production-wildcard-key-request",
        key_id=TEST_DAGSTER_KEY_ID,
        hmac_value=TEST_DAGSTER_HMAC_VALUE,
    )
    invalid_bindings = (
        {
            "secret": TEST_DAGSTER_HMAC_VALUE,
            "allowed_sources": ["*"],
            "allowed_scopes": [{"tenant_id": "aurora_auto", "project_id": "sales_qa"}],
        },
        {
            "secret": TEST_DAGSTER_HMAC_VALUE,
            "allowed_sources": ["dagster"],
            "allowed_scopes": [{"tenant_id": "*", "project_id": "sales_qa"}],
        },
    )

    for binding in invalid_bindings:
        monkeypatch.setattr(
            active_settings,
            "completion_receipt_key_bindings",
            json.dumps({TEST_DAGSTER_KEY_ID: binding}, separators=(",", ":")),
        )
        response = client.post(path, content=encoded, headers=headers)
        assert response.status_code == 500, response.text
        assert response.json()["error"]["code"] == "COMPLETION_SIGNATURE_KEY_BINDINGS_INVALID"


def test_concurrent_signed_requests_cannot_rebind_a_nonce() -> None:
    nonce = "nonce-concurrent-single-binding"
    run_ids = ("signed-nonce-run-a", "signed-nonce-run-b")
    external_ids = tuple(
        _add_submitted_run(run_id, trace_id=f"trace-original-{run_id}") for run_id in run_ids
    )
    paths = tuple(f"/api/v1/runs/{run_id}/external-completion-receipts" for run_id in run_ids)
    payloads = tuple(
        _receipt_payload(
            completion_receipt_id=f"receipt-{run_id}",
            external_run_id=external_id,
        )
        for run_id, external_id in zip(run_ids, external_ids, strict=True)
    )
    encoded_bodies = tuple(json.dumps(payload, ensure_ascii=False).encode() for payload in payloads)
    headers = tuple(
        _signed_headers(
            path=path,
            encoded_body=encoded_body,
            idempotency_key=f"signed-nonce-request-{index}",
            nonce=nonce,
            trace_id=f"trace-signed-nonce-request-{index}",
        )
        for index, (path, encoded_body) in enumerate(zip(paths, encoded_bodies, strict=True))
    )
    start = Barrier(2)

    def submit(index: int) -> tuple[int, dict[str, object]]:
        local_client = TestClient(app)
        try:
            start.wait(timeout=5)
            response = local_client.post(
                paths[index],
                content=encoded_bodies[index],
                headers=headers[index],
            )
            return response.status_code, response.json()
        finally:
            local_client.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(submit, range(2)))

    assert sorted(status for status, _ in responses) == [200, 409]
    rejected = next(body for status, body in responses if status == 409)
    assert rejected["error"]["code"] == "COMPLETION_SIGNATURE_REPLAY"  # type: ignore[index]
    with SessionLocal() as session:
        runs = [session.get(RunRecord, run_id) for run_id in run_ids]
        assert sorted(run.status for run in runs if run is not None) == ["submitted", "success"]
        assert session.scalar(select(func.count()).select_from(RunCompletionReceipt)) == 1
