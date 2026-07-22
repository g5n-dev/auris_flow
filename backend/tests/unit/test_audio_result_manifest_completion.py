from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app.core.completion_signature import completion_signature_message
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    JsonResource,
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
    StorageObject,
)
from app.services import audio_intelligence_service
from app.services.adapters import DispatchResult
from app.services.audio_intelligence_service import (
    resolve_audio_intelligence_result,
    sanitize_audio_intelligence_result,
)
from app.services.run_completion_storage_service import (
    hydrate_staged_audio_result_ref,
    register_hotword_completion_storage_objects,
)
from app.workers import outbox_worker
from app.workers.outbox_worker import process_aggregate_events

TEST_COMPLETION_HMAC_VALUE = "auris-test-completion-secret-32chars-minimum"
TEST_COMPLETION_KEY_ID = "auris-test-completion"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _run() -> RunRecord:
    input_object = {
        "storage_object_id": "sto_audio_input_1",
        "storage_provider": "minio",
        "bucket": "auris-flow",
        "object_key": "tenants/tenant-a/projects/project-a/audio/input.wav",
        "version_id": "input-version-3",
        "content_sha256": "a" * 64,
        "content_length": 64,
        "content_type": "audio/wav",
    }
    return RunRecord(
        run_id="audio_run_1",
        tenant_id="tenant-a",
        project_id="project-a",
        run_type="audio_intelligence",
        status="submitted",
        trace_id="trace-audio-1",
        payload={
            "audio_session_id": "session-a",
            "recording_id": "recording-a",
            "execution_contract": "auris-flow-audio-intelligence-v1",
            "input_object": input_object,
            "provider": "audio_intelligence_default",
            "model_version": "audio-v2.3.1",
            "capabilities": ["vad", "asr", "diarization", "voiceprint", "quality"],
            "dispatch": {
                "adapter": "dagster",
                "details": {
                    "dispatch_idempotency_key": "dispatch-audio-1",
                    "fencing_token": "17:2",
                    "execution_envelope_sha256": "e" * 64,
                },
            },
        },
    )


def _completion_ready_run(run_id: str) -> RunRecord:
    run = _run()
    run.run_id = run_id
    run.tenant_id = "aurora_auto"
    run.project_id = "sales_qa"
    run.trace_id = f"trace_{run_id}"
    payload = deepcopy(run.payload)
    payload["input_object"] = {
        **payload["input_object"],
        "object_key": f"tenants/aurora_auto/projects/sales_qa/audio/{run_id}-input.wav",
    }
    payload["dispatch"] = {
        "adapter": "dagster",
        "operation": "run_request",
        "status": "success",
        "details": {
            **payload["dispatch"]["details"],
            "external_run_id": f"dagster-{run_id}",
        },
    }
    payload["business_completion_required"] = True
    run.payload = payload
    return run


def _manifest(run: RunRecord) -> dict[str, Any]:
    input_object = run.payload["input_object"]
    dispatch_details = run.payload["dispatch"]["details"]
    input_integrity = {
        "manifest_version": "auris-flow-audio-input-integrity-v1",
        "status": "verified",
        "execution_envelope_sha256": dispatch_details["execution_envelope_sha256"],
        "storage_object_id_sha256": hashlib.sha256(
            input_object["storage_object_id"].encode()
        ).hexdigest(),
        "object_version_id_sha256": hashlib.sha256(input_object["version_id"].encode()).hexdigest(),
        "expected_content_sha256": input_object["content_sha256"],
        "observed_content_sha256": input_object["content_sha256"],
        "content_length": input_object["content_length"],
    }
    provider_result = {
        "transcript": {
            "language": "zh-CN",
            "text": "hello world",
            "segments": [
                {
                    "start_ms": 10,
                    "end_ms": 110,
                    "speaker": "speaker-a",
                    "text": "hello world",
                    "confidence": 0.9,
                }
            ],
        },
        "analyses": [
            {"capability": capability, "summary": "validated", "score": 0.8, "labels": []}
            for capability in ("vad", "diarization", "voiceprint", "quality")
        ],
    }
    return {
        "schema_version": "auris-flow-audio-result-manifest-v1",
        "execution_contract": run.payload["execution_contract"],
        "execution_envelope_sha256": dispatch_details["execution_envelope_sha256"],
        "tenant_id": run.tenant_id,
        "project_id": run.project_id,
        "trace_id": run.trace_id,
        "run_id": run.run_id,
        "dispatch_idempotency_key": dispatch_details["dispatch_idempotency_key"],
        "outbox_fencing_token": dispatch_details["fencing_token"],
        "audio_session_id": run.payload["audio_session_id"],
        "recording_id": run.payload["recording_id"],
        "input_object": deepcopy(input_object),
        "inference": {
            "provider": run.payload["provider"],
            "model": run.payload["model_version"],
        },
        "capabilities": list(run.payload["capabilities"]),
        "input_integrity": input_integrity,
        "provider_request_sha256": "1" * 64,
        "provider_response_sha256": "2" * 64,
        "provider_result_sha256": hashlib.sha256(_canonical(provider_result)).hexdigest(),
        "provider_result": provider_result,
    }


def _receipt(
    run: RunRecord,
    body: bytes,
    *,
    provider: str = "minio",
    bucket: str = "auris-flow",
) -> dict[str, Any]:
    manifest_sha256 = hashlib.sha256(body).hexdigest()
    object_key = (
        f"tenants/{run.tenant_id}/projects/{run.project_id}/runs/{run.run_id}/"
        "audio-intelligence/manifest.json"
    )
    version_id = "result-version-7"
    storage_object_id = f"sto_audio_manifest_{manifest_sha256[:32]}"
    input_integrity = _manifest(run)["input_integrity"]
    return {
        "manifest_version": "auris-flow-audio-result-receipt-v1",
        "status": "materialized",
        "result_manifest_schema": "auris-flow-audio-result-manifest-v1",
        "result_manifest_sha256": manifest_sha256,
        "result_manifest_storage_object_id": storage_object_id,
        "result_manifest_object_key_sha256": hashlib.sha256(object_key.encode()).hexdigest(),
        "result_manifest_version_id_sha256": hashlib.sha256(version_id.encode()).hexdigest(),
        "provider_request_sha256": "1" * 64,
        "provider_response_sha256": "2" * 64,
        "provider_result_sha256": _manifest(run)["provider_result_sha256"],
        "execution_contract": run.payload["execution_contract"],
        "execution_envelope_sha256": run.payload["dispatch"]["details"][
            "execution_envelope_sha256"
        ],
        "input_integrity_manifest_sha256": hashlib.sha256(_canonical(input_integrity)).hexdigest(),
        "inference_binding_sha256": hashlib.sha256(
            f"{run.payload['provider']}\n{run.payload['model_version']}".encode()
        ).hexdigest(),
        "requested_capabilities": list(run.payload["capabilities"]),
        "storage_objects": [
            {
                "storage_object_id": storage_object_id,
                "role": "manifest",
                "provider": provider,
                "bucket": bucket,
                "object_key": object_key,
                "version_id": version_id,
                "content_type": "application/json",
                "size_bytes": len(body),
                "content_sha256": manifest_sha256,
            }
        ],
    }


def _signed_completion_headers(
    *,
    path: str,
    encoded_body: bytes,
    idempotency_key: str,
    nonce: str,
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
        key_id=TEST_COMPLETION_KEY_ID,
        source="dagster",
        body_sha256=body_sha256,
    )
    signature = hmac.new(
        TEST_COMPLETION_HMAC_VALUE.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Tenant-Id": tenant_id,
        "X-Project-Id": project_id,
        "X-Request-Id": f"request-{nonce}",
        "X-Trace-Id": f"trace-{nonce}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Auris-Key-Id": TEST_COMPLETION_KEY_ID,
        "X-Auris-Timestamp": timestamp,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": "dagster",
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def _early_audio_run_and_binding(
    client: Any,
    auth_headers: dict[str, str],
    *,
    key: str,
    external_run_id: str,
    execution_envelope_sha256: str,
) -> tuple[RunRecord, OutboxEvent, dict[str, Any]]:
    created = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["vad", "asr", "diarization", "voiceprint", "quality"],
            "reason": f"forced_early_completion_{key}",
        },
        headers={**auth_headers, "Idempotency-Key": f"create-{key}"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    with SessionLocal() as session:
        persisted = session.get(RunRecord, run_id)
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "audio_intelligence.requested",
            )
        )
        assert persisted is not None and event is not None
        assert persisted.status == "pending"
        assert "dispatch" not in persisted.payload
        if "input_object" not in persisted.payload:
            input_object = {
                "storage_object_id": f"sto_input_{run_id}",
                "storage_provider": "minio",
                "bucket": "auris-flow",
                "object_key": (
                    f"tenants/{persisted.tenant_id}/projects/{persisted.project_id}/"
                    f"audio/{run_id}-input.wav"
                ),
                "version_id": "input-version-1",
                "content_sha256": "a" * 64,
                "content_length": 64,
                "content_type": "audio/wav",
            }
            persisted.payload = {**persisted.payload, "input_object": input_object}
            event.payload = {**event.payload, "input_object": input_object}
            session.commit()
        session.expunge(persisted)
        session.expunge(event)
    expected_fencing_token = f"{event.event_id}:{event.lease_generation + 1}"
    dispatch_details = {
        "external_run_id": external_run_id,
        "dagster_run_id": external_run_id,
        "job_name": "auris_flow_audio_intelligence_v1",
        "dispatch_idempotency_key": event.dispatch_idempotency_key,
        "fencing_token": expected_fencing_token,
        "execution_envelope_sha256": execution_envelope_sha256,
    }
    persisted.payload = {
        **persisted.payload,
        "dispatch": {
            "adapter": "dagster",
            "operation": "run_request",
            "status": "success",
            "details": dispatch_details,
        },
        "business_completion_required": True,
    }
    return persisted, event, dispatch_details


class _ExactVersionClient:
    def __init__(
        self,
        body: bytes,
        *,
        version_id: str = "result-version-7",
        bucket: str = "auris-flow",
    ) -> None:
        self.body = body
        self.version_id = version_id
        self.bucket = bucket
        self.calls: list[tuple[str, str, str, int]] = []

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == self.bucket

    def get_object_version(
        self,
        bucket: str,
        object_key: str,
        *,
        version_id: str,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        self.calls.append((bucket, object_key, version_id, max_response_bytes))
        return {
            "status": 200,
            "version_id": self.version_id,
            "content_length": str(len(self.body)),
            "content_type": "application/json",
            "body": self.body,
        }


def test_manifest_receipt_reads_exact_version_and_materializes_supported_domain_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    body = _canonical(_manifest(run))
    receipt = _receipt(run, body)
    client = _ExactVersionClient(body)
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda provider: client if provider == "minio" else pytest.fail(provider),
    )

    resolved = resolve_audio_intelligence_result(run, receipt)

    assert resolved["audio_session_id"] == "session-a"
    assert resolved["asr_segments"][0]["text"] == "hello world"
    assert resolved["vad_segments"] == [{"start_ms": 10, "end_ms": 110, "confidence": 0.9}]
    assert resolved["speaker_turns"][0]["speaker"] == "speaker-a"
    assert resolved["capability_statuses"]["voiceprint"] == {
        "status": "no_content",
        "reason": "provider_protocol_has_no_structured_voiceprint_output",
    }
    assert resolved["capability_statuses"]["quality"] == {
        "status": "no_content",
        "reason": "provider_protocol_has_no_structured_quality_output",
    }
    assert client.calls[0][2:] == ("result-version-7", 4 * 1024 * 1024)


@pytest.mark.parametrize("mutation", ["model", "version", "duplicate", "nan"])
def test_manifest_receipt_fails_closed_on_binding_version_or_json_tampering(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    run = _run()
    manifest = _manifest(run)
    if mutation == "model":
        manifest["inference"]["model"] = "other-approved-model"
        body = _canonical(manifest)
    elif mutation == "duplicate":
        body = b'{"schema_version":"duplicate",' + _canonical(manifest)[1:]
    elif mutation == "nan":
        body = _canonical(manifest).replace(b'"score":0.8', b'"score":NaN', 1)
    else:
        body = _canonical(manifest)
    receipt = _receipt(run, body)
    client = _ExactVersionClient(
        body,
        version_id="other-version" if mutation == "version" else "result-version-7",
    )
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda _provider: client,
    )

    with pytest.raises(ApiError) as raised:
        resolve_audio_intelligence_result(run, receipt)

    assert raised.value.code.startswith("AUDIO_RESULT_MANIFEST_")


def test_manifest_descriptor_is_removed_from_receipts_but_registered_in_scoped_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()
    body = _canonical(_manifest(run))
    receipt = _receipt(run, body)
    settings = audio_intelligence_service.get_settings()
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_bucket", "auris-flow")
    monkeypatch.setattr(settings, "object_storage_allowed_buckets", "auris-flow")

    sanitized = sanitize_audio_intelligence_result(receipt)
    assert "storage_objects" not in sanitized
    assert receipt["storage_objects"][0]["object_key"] not in repr(sanitized)
    assert "result-version-7" not in repr(sanitized)

    with SessionLocal() as session:
        session.add(run)
        session.flush()
        registered = register_hotword_completion_storage_objects(
            session,
            # This helper binds the descriptor to the run; the context is only used
            # for audit/outbox identity and is built by the existing test fixture DB.
            audio_intelligence_service.RequestContext(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                user_id="ext:dagster",
                roles=("system",),
                request_id="request-audio-manifest",
                trace_id="trace-completion",
            ),
            run,
            receipt,
        )
        assert len(registered) == 1
        stored = session.get(StorageObject, receipt["result_manifest_storage_object_id"])
        assert stored is not None
        assert stored.payload["object_version_id"] == "result-version-7"
        assert stored.object_key == receipt["storage_objects"][0]["object_key"]
        session.rollback()


def test_manifest_completion_is_atomic_and_never_persists_raw_result_locator(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "audio_manifest_completion_atomic"
    staged_run = _completion_ready_run(run_id)
    with SessionLocal.begin() as session:
        session.add(staged_run)
    external_id = f"dagster-{run_id}"

    with SessionLocal() as session:
        persisted_run = session.get(RunRecord, run_id)
        assert persisted_run is not None
        session.expunge(persisted_run)
    manifest_body = _canonical(_manifest(persisted_run))
    settings = audio_intelligence_service.get_settings()
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_bucket", "auris-flow")
    monkeypatch.setattr(settings, "object_storage_allowed_buckets", "auris-flow")
    result_receipt = _receipt(persisted_run, manifest_body)
    result_object_key = result_receipt["storage_objects"][0]["object_key"]
    result_version_id = result_receipt["storage_objects"][0]["version_id"]
    exact_client = _ExactVersionClient(manifest_body)
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda provider: exact_client if provider == "minio" else pytest.fail(provider),
    )

    completed = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "audio-result-manifest-complete",
            "external_id": external_id,
            "result_ref": result_receipt,
        },
        headers={**auth_headers, "Idempotency-Key": "audio-result-manifest-complete"},
    )
    assert completed.status_code == 200, completed.text
    assert result_object_key not in completed.text
    assert result_version_id not in completed.text

    with SessionLocal() as session:
        stored_run = session.get(RunRecord, run_id)
        inbox = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == "audio-result-manifest-complete"
            )
        )
        stored_object = session.get(
            StorageObject,
            result_receipt["result_manifest_storage_object_id"],
        )
        assert stored_run is not None
        assert inbox is not None
        assert stored_object is not None
        assert result_object_key not in repr(stored_run.payload["result_ref"])
        assert result_version_id not in repr(stored_run.payload["completion_receipt"])
        assert result_object_key not in repr(inbox.request_body)
        assert result_version_id not in repr(inbox.request_body)
        assert stored_object.object_key == result_object_key
        assert stored_object.payload["object_version_id"] == result_version_id
        resources = session.query(JsonResource).filter(JsonResource.trace_id == stored_run.trace_id)
        assert {resource.collection for resource in resources} >= {
            "vad_segments",
            "speaker_turns",
            "asr_segments",
            "voiceprint_samples",
            "audio_quality_reports",
        }


def test_manifest_registration_and_run_completion_roll_back_with_materialization_failure(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "audio_manifest_completion_rollback"
    staged_run = _completion_ready_run(run_id)
    with SessionLocal.begin() as session:
        session.add(staged_run)
    with SessionLocal() as session:
        persisted_run = session.get(RunRecord, run_id)
        assert persisted_run is not None
        session.expunge(persisted_run)

    manifest_body = _canonical(_manifest(persisted_run))
    result_receipt = _receipt(persisted_run, manifest_body)
    storage_object_id = result_receipt["result_manifest_storage_object_id"]
    settings = audio_intelligence_service.get_settings()
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_bucket", "auris-flow")
    monkeypatch.setattr(settings, "object_storage_allowed_buckets", "auris-flow")
    exact_client = _ExactVersionClient(manifest_body)
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda provider: exact_client if provider == "minio" else pytest.fail(provider),
    )

    def fail_materialization(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ApiError(
            "AUDIO_TEST_MATERIALIZATION_FAILED",
            "forced materialization failure",
            500,
        )

    monkeypatch.setattr(
        audio_intelligence_service,
        "_upsert_audio_resource",
        fail_materialization,
    )
    completion_receipt_id = "audio-result-manifest-rollback"
    failed = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": completion_receipt_id,
            "external_id": f"dagster-{run_id}",
            "result_ref": result_receipt,
        },
        headers={**auth_headers, "Idempotency-Key": completion_receipt_id},
    )
    assert failed.status_code == 500, failed.text
    assert failed.json()["error"]["code"] == "AUDIO_TEST_MATERIALIZATION_FAILED"

    with SessionLocal() as session:
        rolled_back_run = session.get(RunRecord, run_id)
        assert rolled_back_run is not None
        assert rolled_back_run.status == "submitted"
        assert "completion_receipt" not in rolled_back_run.payload
        assert session.get(StorageObject, storage_object_id) is None
        assert (
            session.scalar(
                select(RunCompletionReceipt).where(
                    RunCompletionReceipt.completion_receipt_id == completion_receipt_id
                )
            )
            is None
        )


def test_signed_audio_completion_can_arrive_before_launch_and_materialize_exact_version(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_run_id = "dagster-audio-early-success"
    envelope_sha256 = "e" * 64
    bound_run, event, dispatch_details = _early_audio_run_and_binding(
        client,
        auth_headers,
        key="audio-early-success",
        external_run_id=external_run_id,
        execution_envelope_sha256=envelope_sha256,
    )
    manifest_body = _canonical(_manifest(bound_run))
    result_receipt = _receipt(
        bound_run,
        manifest_body,
        bucket="auris-results",
    )
    descriptor = result_receipt["storage_objects"][0]
    result_object_key = descriptor["object_key"]
    result_version_id = descriptor["version_id"]
    completion_receipt_id = "audio-early-result-completion"
    path = f"/api/v1/runs/{bound_run.run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": completion_receipt_id,
        "external_id": external_run_id,
        "result_ref": result_receipt,
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    headers = _signed_completion_headers(
        path=path,
        encoded_body=encoded,
        idempotency_key="audio-early-result-completion",
        nonce="audio-early-result-completion",
    )
    settings = audio_intelligence_service.get_settings()
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_bucket", "auris-flow")
    monkeypatch.setattr(settings, "object_storage_allowed_buckets", "auris-flow,auris-results")

    staged = client.post(path, content=encoded, headers=headers)
    replayed_while_pending = client.post(path, content=encoded, headers=headers)

    assert staged.status_code == 202, staged.text
    assert replayed_while_pending.status_code == 200, replayed_while_pending.text
    assert staged.json()["data"]["receipt_state"] == "pending_binding"
    assert replayed_while_pending.json()["data"]["receipt_state"] == "pending_binding"
    assert result_object_key not in staged.text
    assert result_version_id not in staged.text
    with SessionLocal() as session:
        stored_run = session.get(RunRecord, bound_run.run_id)
        inbox = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == completion_receipt_id
            )
        )
        pending = session.get(StorageObject, result_receipt["result_manifest_storage_object_id"])
        assert stored_run is not None and inbox is not None and pending is not None
        assert stored_run.status == "pending"
        assert pending.status == "pending_completion_binding"
        assert pending.tenant_id == stored_run.tenant_id
        assert pending.project_id == stored_run.project_id
        assert pending.source_id == stored_run.run_id
        assert pending.object_key == result_object_key
        assert pending.payload["object_version_id"] == result_version_id
        assert result_object_key not in repr(inbox.request_body)
        assert result_version_id not in repr(inbox.request_body)
        assert result_object_key not in repr(inbox.response_json)
        assert result_version_id not in repr(inbox.response_json)
        assert result_object_key not in repr(stored_run.payload)
        assert result_version_id not in repr(stored_run.payload)
        assert (
            session.query(RunCompletionReceipt)
            .filter(RunCompletionReceipt.run_id == bound_run.run_id)
            .count()
            == 1
        )
        foreign_run = deepcopy(bound_run)
        foreign_run.tenant_id = "other-tenant"
        with pytest.raises(ApiError) as cross_scope:
            hydrate_staged_audio_result_ref(
                session,
                foreign_run,
                inbox.request_body["result_ref"],
                completion_receipt_id=completion_receipt_id,
            )
        assert cross_scope.value.code == "AUDIO_RESULT_MANIFEST_PENDING_OBJECT_MISMATCH"

    exact_client = _ExactVersionClient(manifest_body, bucket="auris-results")
    monkeypatch.setattr(
        audio_intelligence_service,
        "object_storage_client_for_provider",
        lambda provider: exact_client if provider == "minio" else pytest.fail(provider),
    )

    def launch_after_callback(
        event_type: str,
        aggregate_type: str,
        dispatch_payload: dict[str, Any],
    ) -> DispatchResult:
        assert event_type == "audio_intelligence.requested"
        assert aggregate_type == "audio_intelligence"
        assert dispatch_payload["dispatch_idempotency_key"] == event.dispatch_idempotency_key
        assert dispatch_payload["outbox_fencing_token"] == dispatch_details["fencing_token"]
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "job_name": "auris_flow_audio_intelligence_v1",
                "execution_envelope_sha256": envelope_sha256,
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", launch_after_callback)
    assert process_aggregate_events([bound_run.run_id]) == 1

    with SessionLocal() as session:
        stored_run = session.get(RunRecord, bound_run.run_id)
        inbox = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == completion_receipt_id
            )
        )
        verified = session.get(
            StorageObject,
            result_receipt["result_manifest_storage_object_id"],
        )
        assert stored_run is not None and inbox is not None and verified is not None
        assert stored_run.status == "success"
        assert inbox.processing_state == "completed"
        assert verified.status == "verified"
        assert verified.payload["verified_completion_receipt_id"] == completion_receipt_id
        assert exact_client.calls[0][2] == result_version_id
        assert result_object_key not in repr(stored_run.payload)
        assert result_version_id not in repr(stored_run.payload)
        assert result_object_key not in repr(inbox.request_body)
        assert result_version_id not in repr(inbox.response_json)
        scoped_audits = session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == bound_run.tenant_id,
                AuditLog.project_id == bound_run.project_id,
            )
        )
        for audit in scoped_audits:
            serialized = repr(audit.after_json)
            assert result_object_key not in serialized
            assert result_version_id not in serialized
            assert "auris-results" not in serialized
        scoped_events = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == bound_run.tenant_id,
                OutboxEvent.project_id == bound_run.project_id,
            )
        )
        for stored_event in scoped_events:
            serialized = repr(stored_event.payload)
            assert result_object_key not in serialized
            assert result_version_id not in serialized
            assert "auris-results" not in serialized

    public_run = client.get(f"/api/v1/runs/{bound_run.run_id}", headers=auth_headers)
    public_trace = client.get(f"/api/v1/traces/{bound_run.trace_id}", headers=auth_headers)
    assert public_run.status_code == 200, public_run.text
    assert public_trace.status_code == 200, public_trace.text
    for public_response in (public_run, public_trace):
        assert result_object_key not in public_response.text
        assert result_version_id not in public_response.text
        assert "auris-results" not in public_response.text


def test_early_audio_completion_wrong_external_id_rejects_and_cannot_reuse_object(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged_external_id = "dagster-audio-staged-untrusted"
    trusted_external_id = "dagster-audio-launch-trusted"
    envelope_sha256 = "f" * 64
    bound_run, _event, _dispatch_details = _early_audio_run_and_binding(
        client,
        auth_headers,
        key="audio-early-mismatch",
        external_run_id=staged_external_id,
        execution_envelope_sha256=envelope_sha256,
    )
    manifest_body = _canonical(_manifest(bound_run))
    result_receipt = _receipt(bound_run, manifest_body, bucket="auris-results")
    completion_receipt_id = "audio-early-mismatch-completion"
    path = f"/api/v1/runs/{bound_run.run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": completion_receipt_id,
        "external_id": staged_external_id,
        "result_ref": result_receipt,
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    settings = audio_intelligence_service.get_settings()
    monkeypatch.setattr(settings, "object_storage_provider", "minio")
    monkeypatch.setattr(settings, "object_storage_bucket", "auris-flow")
    monkeypatch.setattr(settings, "object_storage_allowed_buckets", "auris-flow,auris-results")
    staged = client.post(
        path,
        content=encoded,
        headers=_signed_completion_headers(
            path=path,
            encoded_body=encoded,
            idempotency_key="audio-early-mismatch-completion",
            nonce="audio-early-mismatch-completion",
        ),
    )
    assert staged.status_code == 202, staged.text

    monkeypatch.setattr(
        outbox_worker,
        "dispatch_event",
        lambda *_args, **_kwargs: DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "external_run_id": trusted_external_id,
                "dagster_run_id": trusted_external_id,
                "job_name": "auris_flow_audio_intelligence_v1",
                "execution_envelope_sha256": envelope_sha256,
            },
        ),
    )
    assert process_aggregate_events([bound_run.run_id]) == 1
    storage_object_id = result_receipt["result_manifest_storage_object_id"]
    with SessionLocal() as session:
        rejected_run = session.get(RunRecord, bound_run.run_id)
        rejected_receipt = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.completion_receipt_id == completion_receipt_id
            )
        )
        rejected_object = session.get(StorageObject, storage_object_id)
        assert rejected_run is not None and rejected_receipt is not None
        assert rejected_object is not None
        assert rejected_run.status == "submitted"
        assert rejected_receipt.processing_state == "rejected"
        assert rejected_receipt.response_json["error"]["code"] == (
            "RUN_COMPLETION_EXTERNAL_ID_MISMATCH"
        )
        assert rejected_object.status == "rejected"
        assert rejected_object.payload["rejection_code"] == ("RUN_COMPLETION_EXTERNAL_ID_MISMATCH")

    replay_headers = _signed_completion_headers(
        path=path,
        encoded_body=encoded,
        idempotency_key="audio-early-mismatch-replay",
        nonce="audio-early-mismatch-replay",
    )
    rejected_replay = client.post(path, content=encoded, headers=replay_headers)
    assert rejected_replay.status_code == 409, rejected_replay.text
    assert rejected_replay.json()["error"]["code"] == "RUN_COMPLETION_RECEIPT_REJECTED"

    second_run, _second_event, _second_dispatch = _early_audio_run_and_binding(
        client,
        auth_headers,
        key="audio-early-reuse",
        external_run_id="dagster-audio-reuse",
        execution_envelope_sha256="1" * 64,
    )
    reused_receipt = deepcopy(result_receipt)
    reused_key = (
        f"tenants/{second_run.tenant_id}/projects/{second_run.project_id}/runs/"
        f"{second_run.run_id}/audio-intelligence/manifest.json"
    )
    reused_receipt["result_manifest_object_key_sha256"] = hashlib.sha256(
        reused_key.encode()
    ).hexdigest()
    reused_receipt["storage_objects"][0]["object_key"] = reused_key
    reuse_path = f"/api/v1/runs/{second_run.run_id}/external-completion-receipts"
    reuse_payload = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": "audio-early-reuse-completion",
        "external_id": "dagster-audio-reuse",
        "result_ref": reused_receipt,
    }
    reuse_encoded = json.dumps(reuse_payload, ensure_ascii=False).encode()
    reuse_attempt = client.post(
        reuse_path,
        content=reuse_encoded,
        headers=_signed_completion_headers(
            path=reuse_path,
            encoded_body=reuse_encoded,
            idempotency_key="audio-early-reuse-completion",
            nonce="audio-early-reuse-completion",
        ),
    )
    assert reuse_attempt.status_code == 409, reuse_attempt.text
    assert reuse_attempt.json()["error"]["code"] == "RUN_COMPLETION_STORAGE_COLLISION"
    with SessionLocal() as session:
        assert session.get(StorageObject, storage_object_id).source_id == bound_run.run_id
