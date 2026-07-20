from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select

from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.completion_signature import completion_signature_message
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import (
    AgentDecision,
    AgentRun,
    AsrAnnotationCorrection,
    AssetLineageEdge,
    AssetMaterialization,
    AssetPartition,
    AuditLog,
    ExternalCallbackReceipt,
    JsonResource,
    OutboxEvent,
    Project,
    PromptVersionCandidate,
    RunRecord,
    StorageObject,
    ToolCall,
    TraceRef,
    User,
    VoiceprintEnrollment,
)
from app.workers.outbox_worker import process_aggregate_events, process_once

TEST_COMPLETION_HMAC_VALUE = "auris-test-completion-secret-32chars-minimum"
TEST_COMPLETION_KEY_ID = "auris-test-completion"


def _record_real_qdrant_dispatch(
    run_id: str,
    *,
    point_seed: str | None = None,
) -> tuple[str, dict[str, object]]:
    from app.services.adapters import configured_real_qdrant_embedding_space_fingerprint

    point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"auris-test:{point_seed or run_id}"))
    embedding_space_fingerprint = configured_real_qdrant_embedding_space_fingerprint()
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        dispatch = run.payload.get("dispatch")
        assert isinstance(dispatch, dict)
        details = dispatch.get("details")
        assert isinstance(details, dict)
        qdrant_payload = details.get("qdrant_payload")
        assert isinstance(qdrant_payload, dict)
        recorded_payload = {
            **qdrant_payload,
            "embedding_space_fingerprint": embedding_space_fingerprint,
        }
        run.payload = {
            **run.payload,
            "dispatch": {
                **dispatch,
                "details": {
                    **details,
                    "mode": "real",
                    "point_ids": [point_id],
                    "embedding_space_fingerprint": embedding_space_fingerprint,
                    "qdrant_payload": recorded_payload,
                },
            },
        }
    return point_id, recorded_payload


def _release_second_admin_token() -> str:
    user_id = "u_annotator_001"
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        project = session.get(Project, "sales_qa")
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project.data = {
            **project.data,
            "members": [
                {
                    **member,
                    "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
                }
                if member.get("user_id") == user_id
                else member
                for member in project.data.get("members", [])
            ],
        }
    profile = DevAuthProfile(
        email="outbox-release-reviewer@auris.local",
        user_id=user_id,
        name="发布复核管理员",
        role_label="项目管理员",
        initials="复",
        roles=("annotator", "review_arbitrator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def test_outbox_worker_records_redacted_asr_annotation_correction_event(
    client, auth_headers
) -> None:
    storage_object_id = "sto-worker-asr-correction"
    object_key = f"tenants/aurora_auto/projects/sales_qa/tests/{storage_object_id}.json"
    with SessionLocal() as session:
        session.add(
            StorageObject(
                storage_object_id=storage_object_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                provider="minio",
                bucket="auris-flow-local",
                object_key=object_key,
                object_key_sha256=hashlib.sha256(object_key.encode()).hexdigest(),
                source_type="asr_hotword_evidence",
                source_id=storage_object_id,
                content_type="application/json",
                size_bytes=128,
                content_sha256="a" * 64,
                etag=f"etag-{storage_object_id}",
                status="registered",
                trace_id="trace-worker-asr-correction-source",
                payload={"status": "registered"},
            )
        )
        session.commit()

    created = client.post(
        "/api/v1/audio-sessions/S20250526-000128/annotations",
        json={
            "annotation_id": "worker-asr-correction",
            "annotation_kind": "asr-transcript-correction",
            "confirmation": "record_correction",
            "track": "asr",
            "audio_session_id": "S20250526-000128",
            "recognized_text": "摇控泊车",
            "corrected_text": "遥控泊车",
            "error_type": "misrecognition",
            "evidence_window": "12:30:10 - 12:30:14",
            "evidence_storage_object_id": storage_object_id,
            "hotword_pack_version_id": "hwpv-auto-sales-v1-8",
        },
        headers={
            **auth_headers,
            "Authorization": "Bearer annotator-token",
            "Idempotency-Key": "worker-asr-correction",
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    correction_id = data["correction_id"]
    trace_id = created.json()["meta"]["trace_id"]

    assert process_aggregate_events([correction_id]) == 1
    with SessionLocal() as session:
        correction = session.get(AsrAnnotationCorrection, correction_id)
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == correction_id,
                OutboxEvent.event_type == "asr_annotation.correction-recorded",
            )
        )
        assert correction is not None
        assert event is not None
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"]["adapter"] == "projection"
        assert event.payload["adapter_dispatch"]["operation"] == "record_event"
        serialized_payload = json.dumps(event.payload, ensure_ascii=False)
        assert "摇控泊车" not in serialized_payload
        assert "遥控泊车" not in serialized_payload

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    assert any(
        span.get("kind") == "outbox"
        and span.get("event_type") == "asr_annotation.correction-recorded"
        and span.get("status") == "processed"
        for span in trace.json()["data"]["spans"]
    )


def signed_completion_headers(
    *,
    method: str,
    path: str,
    payload: dict,
    idempotency_key: str,
    source: str,
    trace_id: str = "trace-test-completion",
    timestamp: str | None = None,
    nonce: str = "nonce-test-completion",
) -> dict[str, str]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signed_at = timestamp or datetime.now(UTC).isoformat()
    body_sha256 = hashlib.sha256(encoded).hexdigest()
    message = completion_signature_message(
        method=method,
        path=path,
        query="",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        idempotency_key=idempotency_key,
        timestamp=signed_at,
        nonce=nonce,
        key_id=TEST_COMPLETION_KEY_ID,
        source=source,
        body_sha256=body_sha256,
    )
    signature = hmac.new(
        TEST_COMPLETION_HMAC_VALUE.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Trace-Id": trace_id,
        "X-Request-Id": f"{trace_id}-request",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Auris-Key-Id": TEST_COMPLETION_KEY_ID,
        "X-Auris-Timestamp": signed_at,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": source,
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def test_outbox_worker_marks_task_run_submitted_after_dagster_dispatch(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-run"},
    )
    assert response.status_code == 202
    trace_id = response.json()["meta"]["trace_id"]
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        assert run.payload["status_history"] == [
            {"from": "pending", "to": "running", "reason": "outbox_dispatch_started"},
            {"from": "running", "to": "submitted", "reason": "outbox_dispatch_submitted"},
        ]
        assert run.payload["dispatch_state"] == "dispatched"
        assert run.payload["business_status"] == "awaiting_completion"
        assert run.payload["business_completion_required"] is True
        assert run.payload["completion_mode"] == "external_receipt_required"
        assert run.payload["dispatch"]["adapter"] == "dagster"
        assert run.payload["dispatch"]["operation"] == "run_request"
        assert run.payload["dispatch"]["details"]["external_run_id"].startswith("dg_run_")
        assert run.payload["dispatch"]["details"]["run_request_id"].startswith("dg_req_")
        events = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).all()
        assert events
        assert events[0].status == "processed"
        assert events[0].payload["adapter_dispatch"] == run.payload["dispatch"]

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    outbox_spans = [
        span
        for span in trace.json()["data"]["spans"]
        if span.get("kind") == "outbox" and span.get("event_type") == "task_run.requested"
    ]
    assert outbox_spans
    assert outbox_spans[0]["adapter_dispatch"]["details"]["external_run_id"].startswith("dg_run_")
    assert outbox_spans[0]["attempt_count"] == 1
    attempt_spans = [
        span
        for span in trace.json()["data"]["spans"]
        if span.get("kind") == "outbox_delivery_attempt"
        and span.get("event_id") == outbox_spans[0]["id"]
    ]
    assert len(attempt_spans) == 1
    assert attempt_spans[0]["status"] == "succeeded"
    assert attempt_spans[0]["delivery_mode"] == "dispatch"
    assert attempt_spans[0]["remote_id"].startswith("dg_run_")


def test_outbox_worker_can_process_specific_aggregate_without_draining_others(client, auth_headers):
    first = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/isolated-a",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-isolated-run-a"},
    )
    second = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/isolated-b",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-isolated-run-b"},
    )
    assert first.status_code == 202
    assert second.status_code == 202
    first_run_id = first.json()["data"]["run_id"]
    second_run_id = second.json()["data"]["run_id"]

    assert process_aggregate_events([first_run_id]) == 1
    with SessionLocal() as session:
        first_run = session.get(RunRecord, first_run_id)
        second_run = session.get(RunRecord, second_run_id)
        first_event = (
            session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == first_run_id).one()
        )
        second_event = (
            session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == second_run_id).one()
        )
        assert first_run is not None
        assert first_run.status == "submitted"
        assert first_event.status == "processed"
        assert second_run is not None
        assert second_run.status == "pending"
        assert second_event.status == "pending"
        assert second_event.attempt_count == 0


def test_outbox_worker_submits_conversation_boundary_sync_run(client, auth_headers):
    response = client.patch(
        "/api/v1/conversation-boundaries/boundary_s128_v1",
        json={
            "audio_session_id": "S20250526-000128",
            "start_ms": 42_000,
            "end_ms": 662_000,
            "decision": "manual_confirmed",
            "merged_slice_ids": ["W1", "W2", "W3"],
            "split_slice_ids": [],
            "extension_ids": ["prev_1", "next_1"],
        },
        headers={**auth_headers, "Idempotency-Key": "worker-boundary-sync"},
    )
    assert response.status_code == 200
    body = response.json()
    trace_id = body["meta"]["trace_id"]
    run_id = body["data"]["run_id"]
    assert body["data"]["run_type"] == "boundary_sync"

    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        assert run.payload["boundary_id"] == "boundary_s128_v1"
        assert run.payload["audio_session_id"] == "S20250526-000128"
        assert run.payload["dispatch"]["adapter"] == "dagster"
        assert (
            run.payload["dispatch"]["details"]["job_name"] == "conversation_boundary_sync_pipeline"
        )
        assert run.payload["dispatch"]["details"]["external_run_id"].startswith("dg_run_")
        event = session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "boundary_sync",
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "conversation_boundary.sync_requested",
            )
            .order_by(OutboxEvent.event_id.desc())
        )
        assert event is not None
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"] == run.payload["dispatch"]

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    spans = trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "outbox"
        and span.get("event_type") == "conversation_boundary.sync_requested"
        and span.get("adapter_dispatch", {}).get("adapter") == "dagster"
        for span in spans
    )


def test_audio_intelligence_completion_materializes_audio_tracks(client, auth_headers):
    response = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["vad", "asr", "diarization", "voiceprint", "quality"],
            "reason": "integration_audio_intelligence",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-audio-intelligence"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    trace_id = response.json()["meta"]["trace_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        assert run.run_type == "audio_intelligence"
        assert run.payload["dispatch"]["adapter"] == "dagster"
        assert run.payload["dispatch"]["details"]["job_name"] == "audio_intelligence_pipeline"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]

    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_audio_intelligence",
            "external_id": external_run_id,
            "result_ref": {
                "audio_session_id": "S20250526-000128",
                "recording_id": "A-1001_20250526_122300",
                "capability_statuses": {
                    "vad": {"status": "success"},
                    "asr": {"status": "success"},
                    "diarization": {"status": "success"},
                    "voiceprint": {"status": "success"},
                    "quality": {"status": "success"},
                },
                "vad_segments": [{"start_ms": 30_000, "end_ms": 300_000, "confidence": 0.96}],
                "speaker_turns": [
                    {
                        "speaker": "销售A",
                        "start_ms": 30_000,
                        "end_ms": 128_000,
                        "confidence": 0.92,
                    },
                    {
                        "speaker": "客户",
                        "start_ms": 128_000,
                        "end_ms": 186_000,
                        "confidence": 0.88,
                    },
                ],
                "asr_segments": [
                    {
                        "start_ms": 30_780,
                        "end_ms": 38_200,
                        "speaker": "销售A",
                        "text": "可以优惠 3.5 万，落地大概 28.19 万左右",
                        "confidence": 0.91,
                    }
                ],
                "speaker_ref": "sales_a",
                "voiceprint_quality_score": 88,
                "voiceprint_embedding_ref": {
                    "collection": "voiceprint_embeddings",
                    "point_id": "vp_sales_a_run_001",
                    "vector_dim": 512,
                },
                "snr_db": 23.8,
                "crosstalk_risk": "medium",
            },
        },
        headers={**auth_headers, "Idempotency-Key": "worker-audio-intelligence-complete"},
    )
    assert completion.status_code == 200
    completion_data = completion.json()["data"]
    assert completion_data["status"] == "success"
    assert completion_data["business_status"] == "completed"
    assert {item["collection"] for item in completion_data["materialized_outputs"]} == {
        "vad_segments",
        "speaker_turns",
        "asr_segments",
        "voiceprint_samples",
        "audio_quality_reports",
    }

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    assert any(
        span.get("kind") == "audit"
        and span.get("action") == "audio_intelligence.completion_received"
        and span.get("object_id") == run_id
        for span in trace.json()["data"]["spans"]
    )

    with SessionLocal() as session:
        collections = {
            row.collection
            for row in session.query(JsonResource).filter(JsonResource.trace_id == trace_id).all()
        }
        assert {
            "vad_segments",
            "speaker_turns",
            "asr_segments",
            "voiceprint_samples",
            "audio_quality_reports",
        } <= collections

    detail = client.get("/api/v1/audio-sessions/S20250526-000128", headers=auth_headers)
    assert detail.status_code == 200
    data = detail.json()["data"]
    for field in (
        "vad_segments",
        "speaker_turns",
        "asr_segments",
        "voiceprint_samples",
        "audio_quality_reports",
    ):
        assert any(item["source_run_id"] == run_id for item in data[field]), field


def test_audio_intelligence_rejects_missing_outputs_and_accepts_explicit_no_content(
    client,
    auth_headers,
):
    response = client.post(
        "/api/v1/audio-sessions/S20250526-000128/intelligence-runs",
        json={
            "recording_id": "A-1001_20250526_122300",
            "capabilities": ["vad", "asr", "diarization"],
            "reason": "validate_explicit_audio_outputs",
        },
        headers={**auth_headers, "Idempotency-Key": "audio-empty-output-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]

    invalid = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "audio-empty-output-invalid",
            "external_id": external_run_id,
            "result_ref": {
                "audio_session_id": "S20250526-000128",
                "recording_id": "A-1001_20250526_122300",
            },
        },
        headers={**auth_headers, "Idempotency-Key": "audio-empty-output-invalid"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "AUDIO_CAPABILITY_STATUSES_REQUIRED"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        assert "completion_receipt" not in run.payload

    no_content = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "audio-empty-output-explicit",
            "external_id": external_run_id,
            "result_ref": {
                "audio_session_id": "S20250526-000128",
                "recording_id": "A-1001_20250526_122300",
                "capability_statuses": {
                    "vad": {"status": "no_content", "reason": "no_speech_detected"},
                    "asr": {"status": "no_content", "reason": "no_speech_detected"},
                    "diarization": {
                        "status": "no_content",
                        "reason": "no_speaker_turns_detected",
                    },
                },
                "vad_segments": [],
                "asr_segments": [],
                "speaker_turns": [],
            },
        },
        headers={**auth_headers, "Idempotency-Key": "audio-empty-output-explicit"},
    )
    assert no_content.status_code == 200
    assert no_content.json()["data"]["status"] == "success"
    assert {
        item["collection"]: item["status"]
        for item in no_content.json()["data"]["materialized_outputs"]
    } == {
        "vad_segments": "no_content",
        "asr_segments": "no_content",
        "speaker_turns": "no_content",
    }


def test_outbox_worker_records_qdrant_receipt_for_knowledge_build(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "integration_build"},
        headers={**auth_headers, "Idempotency-Key": "worker-qdrant-build"},
    )
    assert response.status_code == 202
    trace_id = response.json()["meta"]["trace_id"]
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "success"
        assert run.payload["dispatch_state"] == "completed"
        assert run.payload["business_status"] == "completed"
        assert run.payload["business_completion_required"] is False
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "qdrant"
        assert dispatch["operation"] == "upsert_payload"
        assert dispatch["details"]["collection"] == "knowledge_chunks"
        assert dispatch["details"]["source_id"] == "ks_sales_policy"
        assert dispatch["details"]["point_count"] == 1
        assert dispatch["details"]["point_ids"][0].startswith("qdrant_point_")
        qdrant_payload = dispatch["details"]["qdrant_payload"]
        assert qdrant_payload["tenant_id"] == "aurora_auto"
        assert qdrant_payload["project_id"] == "sales_qa"
        assert qdrant_payload["trace_id"] == response.json()["meta"]["trace_id"]
        assert qdrant_payload["collection"] == "knowledge_chunks"
        assert qdrant_payload["knowledge_index_id"] == "ki_sales_policy_v1"
        assert qdrant_payload["knowledge_source_id"] == "ks_sales_policy"
        assert qdrant_payload["source_type"] == "sop_faq_product_docs"
        assert qdrant_payload["asset_key"] == "auris/knowledge/ks_sales_policy"
        assert qdrant_payload["version"] == "kb-index-v3.2"
        assert qdrant_payload["business_ref"]["connector_id"] == "conn_platform_auth"
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"] == dispatch

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    outbox_span = next(
        span
        for span in trace.json()["data"]["spans"]
        if span.get("kind") == "outbox"
        and span.get("event_type") == "knowledge_index.build_requested"
    )
    assert outbox_span["adapter_dispatch"]["adapter"] == "qdrant"
    assert outbox_span["adapter_dispatch"]["details"]["point_ids"][0].startswith("qdrant_point_")
    assert (
        outbox_span["adapter_dispatch"]["details"]["qdrant_payload"]["asset_key"]
        == "auris/knowledge/ks_sales_policy"
    )


def test_outbox_worker_records_qdrant_receipt_for_knowledge_source_sync(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-sources/ks_sales_policy/sync-runs",
        json={"reason": "integration_sync"},
        headers={**auth_headers, "Idempotency-Key": "worker-qdrant-sync"},
    )
    assert response.status_code == 202
    trace_id = response.json()["meta"]["trace_id"]
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "success"
        assert run.payload["business_status"] == "completed"
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "qdrant"
        qdrant_payload = dispatch["details"]["qdrant_payload"]
        assert qdrant_payload["tenant_id"] == "aurora_auto"
        assert qdrant_payload["project_id"] == "sales_qa"
        assert qdrant_payload["trace_id"] == trace_id
        assert qdrant_payload["collection"] == "knowledge_chunks"
        assert qdrant_payload["knowledge_index_id"] is None
        assert qdrant_payload["knowledge_source_id"] == "ks_sales_policy"
        assert qdrant_payload["source_type"] == "sop_faq_product_docs"
        assert qdrant_payload["asset_key"] == "auris/knowledge/ks_sales_policy"
        assert qdrant_payload["business_ref"]["connector_id"] == "conn_platform_auth"
        assert event.status == "processed"

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    outbox_span = next(
        span
        for span in trace.json()["data"]["spans"]
        if span.get("kind") == "outbox"
        and span.get("event_type") == "knowledge_source.sync_requested"
    )
    assert (
        outbox_span["adapter_dispatch"]["details"]["qdrant_payload"]["source_type"]
        == "sop_faq_product_docs"
    )


def test_knowledge_recall_returns_business_hit_after_qdrant_build(client, auth_headers):
    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall_ready"},
        headers={**auth_headers, "Idempotency-Key": "worker-qdrant-recall-build"},
    )
    assert build.status_code == 202
    assert process_once() == 1

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["knowledge_index_id"] == "ki_sales_policy_v1"
    assert data["mode"] == "local_dispatch_receipts"
    assert data["hit_count"] >= 1
    hit = data["hits"][0]
    assert "vector" not in hit
    assert hit["collection"] == "knowledge_chunks"
    assert hit["knowledge_index_id"] == "ki_sales_policy_v1"
    assert hit["knowledge_source_id"] == "ks_sales_policy"
    assert hit["source_type"] == "sop_faq_product_docs"
    assert hit["asset_key"] == "auris/knowledge/ks_sales_policy"
    assert hit["business_ref"]["connector_id"] == "conn_platform_auth"
    assert hit["evidence_ref"]["asset_key"] == "auris/knowledge/ks_sales_policy"
    assert data["filter"] == {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "knowledge_index_id": "ki_sales_policy_v1",
    }


def test_knowledge_recall_rejects_scope_override(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "scope": {"project_id": "other_project"}},
        headers=auth_headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "KNOWLEDGE_RECALL_SCOPE_FORBIDDEN"


def test_real_knowledge_recall_ignores_local_receipts_and_short_circuits(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-local-receipt-is-not-authority"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-local-receipt"},
    )
    assert build.status_code == 202
    assert process_once() == 1

    def unexpected_real_recall(*_args, **_kwargs):
        raise AssertionError("empty real authority must not call Qdrant")

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        unexpected_real_recall,
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "real_qdrant_authority_empty"
    assert data["hit_count"] == 0
    assert data["filter"]["authorized_point_count"] == 0
    assert "has_id" not in data["filter"]


def test_real_knowledge_recall_rejects_cross_project_point(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-cross-project-defense"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-cross-project"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    _record_real_qdrant_dispatch(build.json()["data"]["run_id"])

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        lambda *_args, **_kwargs: {
            "mode": "real_qdrant",
            "collection": "knowledge_chunks",
            "filter": {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "knowledge_index_id": "ki_sales_policy_v1",
            },
            "points": [
                {
                    "id": "99999999-9999-4999-8999-999999999999",
                    "score": 0.99,
                    "payload": {
                        "tenant_id": "aurora_auto",
                        "project_id": "other_project",
                        "knowledge_index_id": "ki_sales_policy_v1",
                        "collection": "knowledge_chunks",
                        "asset_key": "secret/cross-project-asset",
                    },
                }
            ],
        },
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "KNOWLEDGE_RECALL_SCOPE_VIOLATION"
    assert "secret/cross-project-asset" not in response.text


def test_real_knowledge_recall_uses_mysql_dispatch_as_evidence_authority(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-integrity"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-integrity"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    run_id = build.json()["data"]["run_id"]
    point_id, recorded_payload = _record_real_qdrant_dispatch(run_id)

    tampered_payload = {
        **recorded_payload,
        "asset_key": "secret/forged-asset",
        "business_ref": {"connector_id": "forged-connector"},
    }
    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        lambda *_args, **_kwargs: {
            "mode": "real_qdrant",
            "collection": "knowledge_chunks",
            "filter": {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "knowledge_index_id": "ki_sales_policy_v1",
            },
            "points": [{"id": point_id, "score": 0.99, "payload": tampered_payload}],
        },
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "KNOWLEDGE_RECALL_SCOPE_VIOLATION"
    assert response.json()["error"]["details"][0]["code"] == "POINT_PAYLOAD_TAMPERED"
    assert response.json()["error"]["details"][0]["fields"] == [
        "asset_key",
        "business_ref",
    ]
    assert "secret/forged-asset" not in response.text
    assert "forged-connector" not in response.text


def test_real_knowledge_recall_limits_search_to_mysql_dispatch_point_ids(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-authority-filter"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-authority-filter"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    point_id, recorded_payload = _record_real_qdrant_dispatch(build.json()["data"]["run_id"])

    with SessionLocal.begin() as session:
        session.add_all(
            [
                RunRecord(
                    run_id=f"zz_qdrant_other_index_{index:03d}",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    run_type="knowledge_build",
                    status="success",
                    trace_id=f"trace_qdrant_other_index_{index:03d}",
                    payload={
                        "dispatch": {
                            "adapter": "qdrant",
                            "details": {
                                "mode": "real",
                                "point_ids": [
                                    str(
                                        uuid.uuid5(
                                            uuid.NAMESPACE_URL,
                                            f"auris-test:other-index:{index}",
                                        )
                                    )
                                ],
                                "qdrant_payload": {
                                    **recorded_payload,
                                    "knowledge_index_id": f"ki_other_{index:03d}",
                                },
                            },
                        }
                    },
                )
                for index in range(501)
            ]
        )
        session.add(
            RunRecord(
                run_id="zz_qdrant_invalid_real_point",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="knowledge_build",
                status="success",
                trace_id="trace_qdrant_invalid_real_point",
                payload={
                    "dispatch": {
                        "adapter": "qdrant",
                        "details": {
                            "mode": "real",
                            "point_ids": ["qdrant_point_local_receipt"],
                            "qdrant_payload": recorded_payload,
                        },
                    }
                },
            )
        )

    observed: dict[str, object] = {}

    def fake_recall(qdrant_payload, *, query, top_k):
        observed.update(payload=qdrant_payload, query=query, top_k=top_k)
        return {
            "mode": "real_qdrant",
            "collection": "knowledge_chunks",
            "filter": {"has_id": qdrant_payload.get("_authorized_point_ids")},
            "points": [{"id": point_id, "score": 0.99, "payload": recorded_payload}],
        }

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        fake_recall,
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert observed["payload"]["_authorized_point_ids"] == [point_id]
    assert observed["query"] == "报价金额冲突处理 SOP"
    assert observed["top_k"] == 3


def test_real_knowledge_recall_authorizes_only_the_latest_valid_dispatch_point_set(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-latest-authority"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-latest-authority"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    stale_point_id, recorded_payload = _record_real_qdrant_dispatch(build.json()["data"]["run_id"])
    latest_point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "auris-test:latest-authority"))
    latest_payload = {**recorded_payload, "trace_id": "trace_qdrant_latest_authority"}
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id="zz_qdrant_latest_authority",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="knowledge_sync",
                status="success",
                trace_id="trace_qdrant_latest_authority",
                updated_at=datetime.now(UTC) + timedelta(seconds=5),
                payload={
                    "dispatch": {
                        "adapter": "qdrant",
                        "details": {
                            "mode": "real",
                            "point_ids": [latest_point_id, latest_point_id],
                            "embedding_space_fingerprint": latest_payload[
                                "embedding_space_fingerprint"
                            ],
                            "qdrant_payload": latest_payload,
                        },
                    }
                },
            )
        )

    observed: dict[str, object] = {}

    def fake_recall(qdrant_payload, *, query, top_k):
        observed.update(payload=qdrant_payload, query=query, top_k=top_k)
        return {
            "mode": "real_qdrant",
            "collection": "knowledge_chunks",
            "filter": {
                "has_id": [latest_point_id, stale_point_id],
                "tenant_id": "aurora_auto",
            },
            "points": [
                {
                    "id": latest_point_id,
                    "score": 0.99,
                    "payload": latest_payload,
                }
            ],
        }

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        fake_recall,
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert observed["payload"]["_authorized_point_ids"] == [latest_point_id]
    assert data["hits"][0]["point_id"] == latest_point_id
    assert data["filter"]["authorized_point_count"] == 1
    assert latest_point_id not in json.dumps(data["filter"])
    assert stale_point_id not in json.dumps(data["filter"])


def test_real_knowledge_recall_rejects_dispatch_from_a_stale_embedding_space(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-stale-embedding-space"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-stale-embedding-space"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    run_id = build.json()["data"]["run_id"]
    _record_real_qdrant_dispatch(run_id)
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        dispatch = run.payload["dispatch"]
        details = dispatch["details"]
        run.payload = {
            **run.payload,
            "dispatch": {
                **dispatch,
                "details": {
                    **details,
                    "embedding_space_fingerprint": "0" * 64,
                    "qdrant_payload": {
                        **details["qdrant_payload"],
                        "embedding_space_fingerprint": "0" * 64,
                    },
                },
            },
        }

    def unexpected_real_recall(*_args, **_kwargs):
        raise AssertionError("stale embedding authority must not call Qdrant")

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        unexpected_real_recall,
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "real_qdrant_authority_empty"
    assert data["hit_count"] == 0
    assert data["filter"]["authorized_point_count"] == 0
    assert "has_id" not in data["filter"]


def test_real_knowledge_recall_filters_stale_dispatch_when_current_version_exists(
    client,
    auth_headers,
    monkeypatch,
):
    from app.services import knowledge_recall_service

    build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-stale-version"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-stale-version"},
    )
    assert build.status_code == 202
    assert process_once() == 1
    stale_point_id, _stale_payload = _record_real_qdrant_dispatch(build.json()["data"]["run_id"])
    with SessionLocal.begin() as session:
        index = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "knowledge_indexes",
                JsonResource.resource_key == "ki_sales_policy_v1",
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert index is not None
        index.data = {**index.data, "version": "kb-index-v3.3"}

    current_build = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "recall-current-version"},
        headers={**auth_headers, "Idempotency-Key": "qdrant-recall-current-version"},
    )
    assert current_build.status_code == 202
    assert process_once() == 1
    current_point_id, current_payload = _record_real_qdrant_dispatch(
        current_build.json()["data"]["run_id"]
    )
    assert current_point_id != stale_point_id
    assert current_payload["version"] == "kb-index-v3.3"

    observed: dict[str, object] = {}

    def fake_recall(qdrant_payload, *, query, top_k):
        observed.update(payload=qdrant_payload, query=query, top_k=top_k)
        return {
            "mode": "real_qdrant",
            "collection": "knowledge_chunks",
            "filter": {"has_id": qdrant_payload.get("_authorized_point_ids")},
            "points": [
                {
                    "id": current_point_id,
                    "score": 0.99,
                    "payload": current_payload,
                }
            ],
        }

    monkeypatch.setenv("AURIS_QDRANT_ADAPTER", "real")
    monkeypatch.setattr(
        knowledge_recall_service,
        "recall_from_real_qdrant",
        fake_recall,
    )

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={"query": "报价金额冲突处理 SOP", "top_k": 3},
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["hit_count"] == 1
    assert data["hits"][0]["point_id"] == current_point_id
    assert observed["payload"]["_authorized_point_ids"] == [current_point_id]
    assert stale_point_id not in observed["payload"]["_authorized_point_ids"]


def test_outbox_worker_indexes_approved_voiceprint_enrollment(client, auth_headers):
    response = client.post(
        "/api/v1/voiceprint-enrollments",
        json={
            "enrollment_id": "vp_worker_qdrant_enrollment",
            "voiceprint_id": "VP-WORKER-QDRANT",
            "employee_ref": "销售A / A-1001",
            "speaker_id": "spk_worker_qdrant",
            "audio_session_id": "S20250526-000128",
            "recording_id": "A-1001_20250526_122300",
            "asset_key": "auris/audio/raw_recordings",
            "voice_asset_key": "auris/voiceprint/enrollment_templates",
            "quality": {
                "overall": 91,
                "duration": 94,
                "snr": 88,
                "purity": 90,
                "stability": 92,
            },
            "consistency": {"ab": 0.91, "ac": 0.89, "bc": 0.9},
            "samples": [
                {"sample_id": "A", "window": "12:23:42-12:24:12"},
                {"sample_id": "B", "window": "12:24:48-12:25:18"},
            ],
        },
        headers={
            **auth_headers,
            "Authorization": "Bearer annotator-token",
            "Idempotency-Key": "worker-voiceprint-qdrant",
        },
    )
    assert response.status_code == 201
    trace_id = response.json()["meta"]["trace_id"]
    assert response.json()["data"]["status"] == "enrolled"
    assert response.json()["data"]["embedding_ref"]["status"] == "pending_qdrant_upsert"
    assert process_once() == 1

    with SessionLocal() as session:
        projection = session.get(VoiceprintEnrollment, "vp_worker_qdrant_enrollment")
        assert projection is not None
        assert projection.voiceprint_id == "VP-WORKER-QDRANT"
        assert projection.status == "enrolled"
        event = session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "voiceprint_enrollments",
                OutboxEvent.aggregate_id == "vp_worker_qdrant_enrollment",
                OutboxEvent.event_type == "voiceprint_enrollments.upserted",
            )
            .order_by(OutboxEvent.event_id.desc())
        )
        assert event is not None
        assert event.status == "processed"
        dispatch = event.payload["adapter_dispatch"]
        assert dispatch["adapter"] == "qdrant"
        details = dispatch["details"]
        assert details["collection"] == "voiceprint_embeddings"
        assert details["point_ids"][0].startswith("qdrant_point_")
        qdrant_payload = details["qdrant_payload"]
        assert qdrant_payload["tenant_id"] == "aurora_auto"
        assert qdrant_payload["project_id"] == "sales_qa"
        assert qdrant_payload["trace_id"] == trace_id
        assert qdrant_payload["voiceprint_id"] == "VP-WORKER-QDRANT"
        assert qdrant_payload["source_type"] == "voiceprint_enrollment"
        assert qdrant_payload["business_ref"]["sample_count"] == 2

    trace = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    spans = trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "voiceprint_enrollment"
        and span.get("status") == "enrolled"
        and span.get("voiceprint_id") == "VP-WORKER-QDRANT"
        for span in spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("adapter_dispatch", {}).get("adapter") == "qdrant"
        for span in spans
    )


def test_outbox_worker_records_object_storage_receipt_for_export(client, auth_headers):
    response = client.post(
        "/api/v1/exports",
        json={
            "target": "evidence_pack",
            "object_id": "AF-128",
            "content_type": "application/json",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-storage-export"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    pending_detail = client.get(f"/api/v1/exports/{run_id}", headers=auth_headers)
    assert pending_detail.status_code == 200
    pending_data = pending_detail.json()["data"]
    assert pending_data["export_job_id"] == run_id
    assert pending_data["status"] == "pending"
    assert pending_data["download_ref"] is None
    assert pending_data["storage_object_id"] is None

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "submitted"
        assert run.payload["dispatch_state"] == "dispatched"
        assert run.payload["business_status"] == "awaiting_completion"
        assert run.payload["business_completion_required"] is True
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "object_storage"
        assert dispatch["operation"] == "reserve_object"
        assert dispatch["details"]["storage_object_id"].startswith("obj_")
        assert dispatch["details"]["object_uri"].startswith("mock://object-storage/obj_")
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"] == dispatch

    export_detail = client.get(f"/api/v1/exports/{run_id}", headers=auth_headers)
    assert export_detail.status_code == 200
    export_data = export_detail.json()["data"]
    assert export_data["export_job_id"] == run_id
    assert export_data["status"] == "submitted"
    assert export_data["format"] == "jsonl"
    assert export_data["target"] == "evidence_pack"
    assert export_data["object_id"] == "AF-128"
    assert export_data["storage_object_id"].startswith("obj_")
    assert export_data["download_ref"]["kind"] == "object_storage_reference"
    assert export_data["download_ref"]["status"] == "reserved"
    assert export_data["download_ref"]["storage_object_id"] == export_data["storage_object_id"]
    assert export_data["download_ref"]["object_uri"].startswith("mock://object-storage/obj_")


def test_outbox_worker_records_callback_receipt_for_external_callback(client, auth_headers):
    response = client.post(
        "/api/v1/output-sinks/platform-callbacks",
        json={
            "target": "crm_reception_order",
            "payload_template": {"evidence_pack_id": "AF-128"},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-platform-callback"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "submitted"
        assert run.payload["dispatch_state"] == "dispatched"
        assert run.payload["business_status"] == "awaiting_completion"
        assert run.payload["business_completion_required"] is True
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "external_callback"
        assert dispatch["operation"] == "send_signed_callback"
        assert dispatch["details"]["callback_receipt_id"].startswith("callback_receipt_")
        assert dispatch["details"]["signature_id"].startswith("sig_")
        assert dispatch["details"]["signature_mode"] == "mock-hmac-sha256"
        receipt = session.get(ExternalCallbackReceipt, dispatch["details"]["callback_receipt_id"])
        assert receipt is not None
        assert receipt.status == "success"
        assert receipt.trace_id == run.trace_id
        assert receipt.payload["run_id"] == run_id
        assert receipt.payload["event_id"] == event.event_id
        assert receipt.payload["dispatch"] == dispatch
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"] == dispatch


def test_task_run_completion_receipt_moves_submitted_run_to_success(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/completion",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-task-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
        assert run.status == "submitted"

    completion_headers = {**auth_headers, "Idempotency-Key": "worker-completion-task-run-done"}
    completion = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_task_run",
            "external_id": external_run_id,
            "result_ref": {"asset_key": "auris/task/login_risk_review_task_v3"},
            "metrics": {"materialized_partitions": 1},
        },
        headers=completion_headers,
    )
    replay = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_task_run",
            "external_id": external_run_id,
            "result_ref": {"asset_key": "auris/task/login_risk_review_task_v3"},
            "metrics": {"materialized_partitions": 1},
        },
        headers=completion_headers,
    )
    assert completion.status_code == 200
    assert replay.status_code == 200
    assert completion.json()["data"]["run_id"] == replay.json()["data"]["run_id"]
    assert completion.json()["data"]["status"] == "success"
    assert completion.json()["data"]["business_status"] == "completed"
    assert completion.json()["data"]["business_completion_required"] is False
    assert completion.json()["data"]["completion_receipt"]["external_id"] == external_run_id

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run.status == "success"
        assert run.payload["status_history"] == [
            {"from": "pending", "to": "running", "reason": "outbox_dispatch_started"},
            {"from": "running", "to": "submitted", "reason": "outbox_dispatch_submitted"},
            {"from": "submitted", "to": "success", "reason": "dagster_completion_received"},
        ]


def test_signed_external_completion_receipt_moves_submitted_run_to_success(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/signed-completion",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-signed-completion-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
        assert run.status == "submitted"

    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "signed_dagster_complete_task_run",
        "external_id": external_run_id,
        "result_ref": {"asset_key": "auris/task/login_risk_review_task_v3"},
        "metrics": {"materialized_partitions": 1},
    }
    headers = signed_completion_headers(
        method="POST",
        path=path,
        payload=payload,
        idempotency_key="worker-signed-completion-done",
        source="dagster",
        nonce="nonce-worker-signed-completion-done",
    )
    completion = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    replay = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    assert completion.status_code == 200
    assert replay.status_code == 200
    data = completion.json()["data"]
    assert data["status"] == "success"
    assert data["completion_receipt"]["external_id"] == external_run_id
    assert data["completion_receipt"]["auth"]["auth_mode"] == "signed_external_completion"
    assert data["completion_receipt"]["auth"]["authenticated_source"] == "dagster"
    assert data["completion_receipt"]["auth"]["signature_key_id"] == TEST_COMPLETION_KEY_ID


def test_signed_external_completion_receipt_rejects_missing_signature(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/missing-signature",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-missing-signature-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1
    with SessionLocal() as session:
        external_run_id = session.get(RunRecord, run_id).payload["dispatch"]["details"][
            "external_run_id"
        ]

    completion = client.post(
        f"/api/v1/runs/{run_id}/external-completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "missing_signature_complete",
            "external_id": external_run_id,
        },
        headers={
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "Idempotency-Key": "worker-missing-signature-done",
        },
    )
    assert completion.status_code == 401
    assert completion.json()["error"]["code"] == "COMPLETION_SIGNATURE_REQUIRED"
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"


def test_signed_external_completion_receipt_rejects_tampered_body_and_missing_external_id(
    client, auth_headers
):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/tampered-signature",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-tampered-signature-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    signed_payload = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "tampered_signature_complete",
        "external_id": "signed-but-will-be-tampered",
    }
    tampered_payload = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "tampered_signature_complete",
    }
    headers = signed_completion_headers(
        method="POST",
        path=path,
        payload=signed_payload,
        idempotency_key="worker-tampered-signature-done",
        source="dagster",
        nonce="nonce-worker-tampered-signature-done",
    )
    tampered = client.post(
        path,
        content=json.dumps(tampered_payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    assert tampered.status_code == 401
    assert tampered.json()["error"]["code"] == "COMPLETION_SIGNATURE_INVALID"

    missing_external_id_headers = signed_completion_headers(
        method="POST",
        path=path,
        payload=tampered_payload,
        idempotency_key="worker-signed-missing-external-id",
        source="dagster",
        nonce="nonce-worker-signed-missing-external-id",
    )
    missing_external_id = client.post(
        path,
        content=json.dumps(tampered_payload, ensure_ascii=False).encode("utf-8"),
        headers=missing_external_id_headers,
    )
    assert missing_external_id.status_code == 400
    assert missing_external_id.json()["error"]["code"] == "RUN_COMPLETION_EXTERNAL_ID_REQUIRED"
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"


def test_signed_external_completion_receipt_rejects_expired_timestamp(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/expired-signature",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-expired-signature-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1
    with SessionLocal() as session:
        external_run_id = session.get(RunRecord, run_id).payload["dispatch"]["details"][
            "external_run_id"
        ]
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "expired_signature_complete",
        "external_id": external_run_id,
    }
    headers = signed_completion_headers(
        method="POST",
        path=path,
        payload=payload,
        idempotency_key="worker-expired-signature-done",
        source="dagster",
        timestamp=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
        nonce="nonce-worker-expired-signature-done",
    )
    completion = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    assert completion.status_code == 401
    assert completion.json()["error"]["code"] == "COMPLETION_SIGNATURE_EXPIRED"
    with SessionLocal() as session:
        assert session.get(RunRecord, run_id).status == "submitted"


def test_completion_receipt_rejects_wrong_external_id(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/completion-mismatch",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-mismatch-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1

    completion = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_wrong",
            "external_id": "wrong_dagster_run_id",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-mismatch"},
    )
    assert completion.status_code == 409
    assert completion.json()["error"]["code"] == "RUN_COMPLETION_EXTERNAL_ID_MISMATCH"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run.status == "submitted"
        assert "completion_receipt" not in run.payload


def test_export_completion_receipt_makes_download_ready(client, auth_headers):
    response = client.post(
        "/api/v1/exports",
        json={
            "target": "evidence_pack",
            "object_id": "AF-129",
            "content_type": "application/json",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-export"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1

    reserved = client.get(f"/api/v1/exports/{run_id}", headers=auth_headers)
    assert reserved.status_code == 200
    assert reserved.json()["data"]["status"] == "submitted"
    assert reserved.json()["data"]["download_ref"]["status"] == "reserved"
    storage_object_id = reserved.json()["data"]["storage_object_id"]

    completion = client.post(
        f"/api/v1/exports/{run_id}/completion-receipts",
        json={
            "adapter": "object_storage",
            "status": "success",
            "completion_receipt_id": "export_ready_AF_129",
            "external_id": storage_object_id,
            "result_ref": {"download_url": reserved.json()["data"]["download_ref"]["object_uri"]},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-export-ready"},
    )
    assert completion.status_code == 200
    assert completion.json()["data"]["status"] == "success"
    assert completion.json()["data"]["download_ref"]["status"] == "ready"
    assert completion.json()["data"]["download_ref"]["storage_object_id"] == storage_object_id


def test_asset_backfill_completion_materializes_asset_lineage(client, auth_headers):
    asset_key = "auris/label/event_tags"
    encoded_asset_key = quote(asset_key, safe="")
    partition_key = "aurora_auto/BJ-AURORA-001/2025-05-26/backfill"
    response = client.post(
        f"/api/v1/data-assets/{encoded_asset_key}/backfills",
        json={
            "reason": "补齐金额冲突标签",
            "partition_key": partition_key,
            "recompute_downstream": True,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-asset-backfill-completion"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    run_trace_id = response.json()["meta"]["trace_id"]
    assert process_once() == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
        assert run.status == "submitted"

    materialization_id = "mat_backfill_contract_001"
    storage_object_id = f"sto_{run_id}_materialization"
    storage_object_key = (
        f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/assets/event-tags-backfill.jsonl"
    )
    storage_content_sha256 = hashlib.sha256(b"event-tags-backfill").hexdigest()
    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_asset_backfill",
            "external_id": external_run_id,
            "result_ref": {
                "asset_key": asset_key,
                "partition_key": partition_key,
                "materialization_id": materialization_id,
                "storage_object_id": storage_object_id,
                "storage_objects": [
                    {
                        "storage_object_id": storage_object_id,
                        "role": "asset_materialization",
                        "provider": "minio",
                        "bucket": "auris-flow-local",
                        "object_key": storage_object_key,
                        "content_type": "application/x-ndjson",
                        "size_bytes": 128,
                        "content_sha256": storage_content_sha256,
                        "etag": "event-tags-backfill-etag",
                    }
                ],
                "upstream_asset_keys": ["auris/audio/transcript_asset"],
                "downstream_asset_keys": ["auris/report/quality_daily"],
                "record_count": 128,
                "error_count": 0,
                "checks": [{"name": "schema", "status": "passed"}],
            },
            "metrics": {"record_count": 128, "error_count": 0},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-asset-backfill-complete"},
    )
    assert completion.status_code == 200
    assert completion.json()["data"]["status"] == "success"
    assert completion.json()["data"]["registered_storage_objects"] == [
        {
            "storage_object_id": storage_object_id,
            "source_type": "asset_backfill",
            "source_id": run_id,
            "status": "verified",
            "trace_id": run_trace_id,
        }
    ]
    assert completion.json()["data"]["materialized_assets"][0]["asset_key"] == asset_key
    assert (
        completion.json()["data"]["materialized_assets"][0]["materialization_id"]
        == materialization_id
    )

    with SessionLocal() as session:
        materialization = session.get(AssetMaterialization, materialization_id)
        assert materialization is not None
        assert materialization.trace_id == run_trace_id
        assert materialization.payload["run_id"] == run_id
        storage_object = session.get(StorageObject, storage_object_id)
        assert storage_object is not None
        assert storage_object.status == "verified"
        assert storage_object.source_type == "asset_backfill"
        assert storage_object.source_id == run_id
        assert storage_object.trace_id == run_trace_id
        partition = (
            session.query(AssetPartition)
            .filter(AssetPartition.payload["materialization_id"].as_string() == materialization_id)
            .one()
        )
        assert partition.payload["partition_key"] == partition_key
        asset_projection = (
            session.query(JsonResource)
            .filter(
                JsonResource.collection == "data_assets",
                JsonResource.resource_key == asset_key,
            )
            .one()
        )
        assert asset_projection.data["latest_materialization_id"] == materialization_id
        assert asset_projection.data["latest_run_id"] == run_id
        lineage_edges = (
            session.query(AssetLineageEdge)
            .filter(
                AssetLineageEdge.payload["materialization_id"].as_string() == materialization_id
            )
            .all()
        )
        assert {edge.payload["source_asset_key"] for edge in lineage_edges} == {
            "auris/audio/transcript_asset",
            asset_key,
        }
        assert {edge.payload["target_asset_key"] for edge in lineage_edges} == {
            asset_key,
            "auris/report/quality_daily",
        }

    materializations = client.get(
        f"/api/v1/data-assets/{encoded_asset_key}/materializations", headers=auth_headers
    )
    assert materializations.status_code == 200
    items = materializations.json()["data"]["items"]
    assert items[0]["materialization_id"] == materialization_id
    assert items[0]["asset_key"] == asset_key
    assert items[0]["partition_key"] == partition_key

    partitions = client.get(
        f"/api/v1/data-assets/{encoded_asset_key}/partitions", headers=auth_headers
    )
    assert partitions.status_code == 200
    assert any(
        item["materialization_id"] == materialization_id
        for item in partitions.json()["data"]["items"]
    )

    lineage = client.get(f"/api/v1/data-assets/{encoded_asset_key}/lineage", headers=auth_headers)
    assert lineage.status_code == 200
    lineage_data = lineage.json()["data"]
    assert any(
        edge["from"] == "auris/audio/transcript_asset"
        and edge["to"] == asset_key
        and edge["materialization_id"] == materialization_id
        for edge in lineage_data["edges"]
    )
    assert any(
        edge["from"] == asset_key
        and edge["to"] == "auris/report/quality_daily"
        and edge["materialization_id"] == materialization_id
        for edge in lineage_data["edges"]
    )
    assert any(
        node["node_type"] == "materialization" and node["asset_key"] == materialization_id
        for node in lineage_data["nodes"]
    )

    trace = client.get(f"/api/v1/traces/{run_trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    assert {
        "kind": "materialization",
        "id": materialization_id,
        "materialization_id": materialization_id,
        "status": "success",
        "asset_key": asset_key,
        "partition_key": partition_key,
        "run_id": run_id,
        "storage_refs": [
            {
                "kind": "storage_object",
                "storage_object_id": storage_object_id,
                "provider": "minio",
                "bucket": "auris-flow-local",
                "object_key": storage_object_key,
                "content_type": "application/x-ndjson",
                "size_bytes": 128,
                "content_sha256": storage_content_sha256,
                "etag": "event-tags-backfill-etag",
                "status": "verified",
                "run_id": run_id,
            }
        ],
    } in trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "asset_lineage_edge"
        and span.get("materialization_id") == materialization_id
        and span.get("run_id") == run_id
        for span in trace.json()["data"]["spans"]
    )


def test_asset_backfill_completion_rejects_forged_storage_descriptor(client, auth_headers):
    asset_key = "auris/label/event_tags"
    partition_key = "aurora_auto/BJ-AURORA-001/2025-05-26/forged-storage"
    response = client.post(
        f"/api/v1/data-assets/{quote(asset_key, safe='')}/backfills",
        json={"reason": "验证伪造对象描述符被拒绝", "partition_key": partition_key},
        headers={**auth_headers, "Idempotency-Key": "worker-asset-backfill-forged-storage"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None and run.status == "submitted"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]

    storage_object_id = f"sto_{run_id}_forged"
    completion = client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_reject_forged_asset_storage",
            "external_id": external_run_id,
            "result_ref": {
                "asset_key": asset_key,
                "partition_key": partition_key,
                "storage_object_id": storage_object_id,
                "storage_objects": [
                    {
                        "storage_object_id": storage_object_id,
                        "role": "asset_materialization",
                        "provider": "minio",
                        "bucket": "auris-flow-local",
                        "object_key": (
                            "tenants/aurora_auto/projects/sales_qa/runs/another_run/forged.jsonl"
                        ),
                        "content_type": "application/x-ndjson",
                        "size_bytes": 128,
                        "content_sha256": hashlib.sha256(b"forged").hexdigest(),
                        "etag": "forged-etag",
                    }
                ],
            },
        },
        headers={**auth_headers, "Idempotency-Key": "worker-reject-forged-asset-storage"},
    )
    assert completion.status_code == 422
    assert completion.json()["error"]["code"] == "RUN_COMPLETION_STORAGE_LOCATOR_INVALID"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None and run.status == "submitted"
        assert session.get(StorageObject, storage_object_id) is None
        assert (
            session.query(AssetMaterialization)
            .filter(AssetMaterialization.payload["run_id"].as_string() == run_id)
            .count()
            == 0
        )


def test_eval_feedback_agent_run_records_tools_refs_and_decision(client, auth_headers):
    eval_response = client.post(
        "/api/v1/eval-runs",
        json={
            "dataset_id": "eval_quote_guard_v12",
            "model_version": "prod-v5",
            "label_version": "v1.9.0-rc2",
            "source": "agentic_integration",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-agentic-eval-source"},
    )
    assert eval_response.status_code == 202
    eval_run_id = eval_response.json()["data"]["run_id"]
    assert process_aggregate_events([eval_run_id]) == 1

    feedback = client.post(
        f"/api/v1/eval-runs/{eval_run_id}/feedback-tasks",
        json={
            "badcase_refs": ["B-2031", "LC-quote-002"],
            "target": "标签规则 / Prompt 优化 / 打标黄金集",
            "source": "agentic_integration",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-agentic-feedback"},
    )
    assert feedback.status_code == 202
    feedback_data = feedback.json()["data"]
    feedback_run_id = feedback_data["run_id"]
    feedback_trace_id = feedback.json()["meta"]["trace_id"]
    agent_run_id = feedback_data["agent_run_id"]
    assert agent_run_id == feedback_run_id
    assert feedback_data["agent_policy"]["forbidden_writes"] == [
        "online_label",
        "production_prompt",
        "source_asset",
    ]
    assert {item["key"] for item in feedback_data["agent_tool_plan"]} == {
        "retrieve_badcases",
        "diagnose_prompt_gap",
        "write_feedback_draft",
    }
    assert {"type": "badcase", "id": "B-2031", "source_field": "badcase_refs"} in feedback_data[
        "agent_input_refs"
    ]

    with SessionLocal() as session:
        agent = session.get(AgentRun, agent_run_id)
        assert agent is not None
        assert agent.status == "pending"
        assert agent.trace_id == feedback_trace_id
        planned_tools = (
            session.query(ToolCall)
            .filter(ToolCall.payload["agent_run_id"].as_string() == agent_run_id)
            .all()
        )
        assert {tool.payload["key"] for tool in planned_tools} == {
            "retrieve_badcases",
            "diagnose_prompt_gap",
            "write_feedback_draft",
        }
        input_refs = (
            session.query(TraceRef)
            .filter(
                TraceRef.payload["agent_run_id"].as_string() == agent_run_id,
                TraceRef.payload["ref_role"].as_string() == "input",
            )
            .all()
        )
        assert {ref.payload["id"] for ref in input_refs} >= {
            eval_run_id,
            feedback_data["feedback_task_id"],
            "B-2031",
            "LC-quote-002",
        }

    assert process_aggregate_events([feedback_run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, feedback_run_id)
        assert run is not None
        assert run.status == "submitted"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
        agent = session.get(AgentRun, agent_run_id)
        assert agent is not None
        assert agent.status == "submitted"
        assert agent.payload["dispatch"]["details"]["external_run_id"] == external_run_id
        dispatch_tool = (
            session.query(ToolCall)
            .filter(
                ToolCall.payload["agent_run_id"].as_string() == agent_run_id,
                ToolCall.payload["key"].as_string() == "dispatch_agent_run",
            )
            .one()
        )
        assert dispatch_tool.status == "success"

    completion = client.post(
        f"/api/v1/runs/{feedback_run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "dagster_complete_agentic_feedback",
            "external_id": external_run_id,
            "result_ref": {
                "draft_ref": "prompt_candidate_v7",
                "object_uri": "mock://object-storage/agent/eval_feedback.json",
                "change_set_id": "changeset_eval_feedback_001",
            },
            "metrics": {"candidate_count": 2, "human_review_tasks": 1},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-agentic-feedback-complete"},
    )
    assert completion.status_code == 200
    assert completion.json()["data"]["status"] == "success"

    with SessionLocal() as session:
        agent = session.get(AgentRun, agent_run_id)
        assert agent is not None
        assert agent.status == "success"
        decision = (
            session.query(AgentDecision)
            .filter(AgentDecision.payload["agent_run_id"].as_string() == agent_run_id)
            .one()
        )
        assert decision.status == "success"
        assert decision.payload["result_ref"]["draft_ref"] == "prompt_candidate_v7"
        prompt_candidate = session.get(PromptVersionCandidate, "prompt_candidate_v7")
        assert prompt_candidate is not None
        assert prompt_candidate.status == "candidate"
        assert prompt_candidate.trace_id == feedback_trace_id
        assert prompt_candidate.payload["source_run_id"] == feedback_run_id
        assert prompt_candidate.payload["eval_run_id"] == eval_run_id
        assert prompt_candidate.payload["review_gate"]["required"] is True
        assert "production_prompt" in prompt_candidate.payload["write_policy"]["forbidden_writes"]
        result_ref = (
            session.query(TraceRef)
            .filter(
                TraceRef.payload["agent_run_id"].as_string() == agent_run_id,
                TraceRef.payload["ref_role"].as_string() == "result",
            )
            .one()
        )
        assert result_ref.status == "success"
        assert result_ref.payload["result_ref"]["change_set_id"] == "changeset_eval_feedback_001"

    trace = client.get(f"/api/v1/traces/{feedback_trace_id}", headers=auth_headers)
    assert trace.status_code == 200
    spans = trace.json()["data"]["spans"]
    assert {
        "agent_run",
        "tool_call",
        "agent_decision",
        "trace_ref",
    } <= {span.get("kind") for span in spans}
    assert any(
        span.get("kind") == "agent_run"
        and span.get("agent_run_id") == agent_run_id
        and span.get("status") == "success"
        for span in spans
    )
    assert any(
        span.get("kind") == "prompt_version_candidate"
        and span.get("candidate_id") == "prompt_candidate_v7"
        and span.get("status") == "candidate"
        for span in spans
    )
    prompt_detail = client.get(
        "/api/v1/prompt-version-candidates/prompt_candidate_v7",
        headers=auth_headers,
    )
    assert prompt_detail.status_code == 200
    assert prompt_detail.json()["data"]["source_run_id"] == feedback_run_id
    prompt_list = client.get("/api/v1/prompt-version-candidates", headers=auth_headers)
    assert prompt_list.status_code == 200
    assert any(
        item["candidate_id"] == "prompt_candidate_v7"
        for item in prompt_list.json()["data"]["items"]
    )


def test_external_callback_completion_ack_is_persisted(client, auth_headers):
    response = client.post(
        "/api/v1/output-sinks/platform-callbacks",
        json={
            "target": "crm_reception_order",
            "payload_template": {"evidence_pack_id": "AF-130"},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-callback"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]
    assert process_once() == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        callback_receipt_id = run.payload["dispatch"]["details"]["callback_receipt_id"]
        assert session.get(ExternalCallbackReceipt, callback_receipt_id) is not None

    completion = client.post(
        f"/api/v1/output-sinks/platform-callbacks/{run_id}/completion-receipts",
        json={
            "adapter": "external_callback",
            "status": "success",
            "completion_receipt_id": "remote_callback_ack_AF_130",
            "external_id": callback_receipt_id,
            "result_ref": {"remote_ticket_id": "CRM-130"},
        },
        headers={**auth_headers, "Idempotency-Key": "worker-completion-callback-ack"},
    )
    assert completion.status_code == 200
    assert completion.json()["data"]["status"] == "success"
    assert completion.json()["data"]["completion_receipt"]["external_id"] == callback_receipt_id

    with SessionLocal() as session:
        receipt = session.get(ExternalCallbackReceipt, callback_receipt_id)
        assert receipt.payload["completion_ack"]["run_id"] == run_id
        assert (
            receipt.payload["completion_ack"]["completion_receipt_id"]
            == "remote_callback_ack_AF_130"
        )


def test_outbox_worker_schedules_retry_on_failure(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "force_worker_error": True,
            "failure_reason": "temporary downstream timeout",
            "retry_after_seconds": 0,
            "max_attempts": 3,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-retry-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "running"
        assert run.payload["dispatch_state"] == "retry_wait"
        assert event.status == "pending"
        assert event.attempt_count == 1
        assert event.last_error == "RuntimeError: temporary downstream timeout"
        assert event.available_at is not None
        assert run.payload["retryable"] is True
        assert run.payload["retry_count"] == 1
        assert run.payload["next_retry_at"].startswith(event.available_at.isoformat())
        assert run.payload["failed_event_id"] == event.event_id
        assert run.payload["error_code"] == "RuntimeError"
        assert run.payload["error"] == event.last_error
        assert run.payload["next_actions"][0]["key"] == "retry_scheduled"
        assert run.payload["next_actions"][0]["available_at"].startswith(
            event.available_at.isoformat()
        )

    trace = client.get(
        f"/api/v1/traces/{response.json()['meta']['trace_id']}", headers=auth_headers
    )
    assert trace.status_code == 200
    outbox_span = next(
        span for span in trace.json()["data"]["spans"] if span.get("kind") == "outbox"
    )
    assert outbox_span["status"] == "pending"
    assert outbox_span["retryable"] is True
    assert outbox_span["retry_after_seconds"] == 0
    assert outbox_span["available_at"] is not None
    assert outbox_span["error_code"] == "RuntimeError"
    assert outbox_span["next_actions"][0]["key"] == "retry_scheduled"


def test_outbox_worker_dead_letters_after_max_attempts(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "force_worker_error": True,
            "failure_reason": "permanent callback failure",
            "retry_after_seconds": 0,
            "max_attempts": 1,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-dead-letter-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.requested",
            )
            .one()
        )
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == event.tenant_id,
                AuditLog.project_id == event.project_id,
                AuditLog.object_id == run_id,
                AuditLog.action == "task_run.failed",
            )
        )
        terminal_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.tenant_id == event.tenant_id,
                OutboxEvent.project_id == event.project_id,
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.failed",
            )
        )
        assert run is not None
        assert run.status == "failed"
        assert run.payload["error"] == "RuntimeError: permanent callback failure"
        assert event.status == "dead_letter"
        assert event.attempt_count == 1
        assert event.processed_at is not None
        assert run.payload["retry_count"] == 1
        assert run.payload["next_retry_at"] is None
        assert run.payload["dead_letter_event_id"] == event.event_id
        assert run.payload["next_actions"][0]["key"] == "retry"
        assert run.payload["status_history"] == [
            {"from": "pending", "to": "running", "reason": "outbox_dispatch_started"},
            {"from": "running", "to": "failed", "reason": "outbox_dispatch_dead_letter"},
        ]
        assert audit is not None
        assert audit.trace_id == run.trace_id == event.payload["trace_id"]
        assert audit.idempotency_key == event.payload["idempotency_key"]
        assert audit.before_json == {"status": "running"}
        assert audit.after_json["status"] == "failed"
        assert audit.after_json["reason"] == "outbox_dispatch_dead_letter"
        assert terminal_event is not None
        assert terminal_event.status == "pending"
        assert terminal_event.payload["trace_id"] == run.trace_id
        assert terminal_event.payload["idempotency_key"] == event.payload["idempotency_key"]
        assert terminal_event.payload["reason"] == "outbox_dispatch_dead_letter"

    # The terminal event is a projection only: delivering it cannot re-enter or
    # mutate the failed source TaskRun state machine.
    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        terminal_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.failed",
            )
        )
        assert run is not None and run.status == "failed"
        assert terminal_event is not None and terminal_event.status == "processed"

    trace = client.get(
        f"/api/v1/traces/{response.json()['meta']['trace_id']}", headers=auth_headers
    )
    assert trace.status_code == 200
    outbox_span = next(
        span for span in trace.json()["data"]["spans"] if span.get("kind") == "outbox"
    )
    assert outbox_span["status"] == "dead_letter"
    assert outbox_span["retryable"] is False
    assert outbox_span["error_code"] == "RuntimeError"
    assert outbox_span["processed_at"] is not None
    assert outbox_span["next_actions"][0]["key"] == "retry"


def test_dead_letter_task_run_can_create_retry_run(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "force_worker_error": True,
            "failure_reason": "permanent callback failure",
            "retry_after_seconds": 0,
            "max_attempts": 1,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-dead-letter-retry-source"},
    )
    assert response.status_code == 202
    failed_run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        failed_run = session.get(RunRecord, failed_run_id)
        failed_event = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.aggregate_id == failed_run_id,
                OutboxEvent.event_type == "task_run.requested",
            )
            .one()
        )
        assert failed_run is not None
        assert failed_run.status == "failed"
        assert failed_event.status == "dead_letter"

    semantic_override = client.post(
        f"/api/v1/task-runs/{failed_run_id}/retries",
        json={
            "reason": "attempt to replace frozen task and scene semantics",
            "payload_overrides": {
                "task_version_id": "task_version_other",
                "scene_profile_id": "scene_other",
                "scene_profile_version_id": "scenev_other",
                "scene_profile_snapshot_sha256": "0" * 64,
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "worker-dead-letter-retry-semantic-override",
        },
    )
    assert semantic_override.status_code == 409, semantic_override.text
    assert semantic_override.json()["error"]["code"] == "RUN_RETRY_SEMANTIC_OVERRIDE_FORBIDDEN"

    retry = client.post(
        f"/api/v1/task-runs/{failed_run_id}/retries",
        json={
            "reason": "operator fixed downstream callback",
            "payload_overrides": {
                "force_worker_error": True,
                "run_id": failed_run_id,
                "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/retry",
            },
        },
        headers={**auth_headers, "Idempotency-Key": "worker-dead-letter-retry-new-run"},
    )
    assert retry.status_code == 202
    retry_body = retry.json()
    retry_run_id = retry_body["data"]["run_id"]
    assert retry_run_id != failed_run_id
    assert retry_body["data"]["status"] == "pending"
    assert retry_body["data"]["retry_of_run_id"] == failed_run_id
    assert retry_body["data"]["retry_of_trace_id"] == response.json()["data"]["trace_id"]
    assert retry_body["data"]["retry_reason"] == "operator fixed downstream callback"
    assert retry_body["data"]["trigger_type"] == "retry"
    assert retry_body["data"]["partition_key"] == "aurora_auto/BJ-AURORA-001/2025-05-26/retry"
    assert retry_body["data"]["trace_id"] == response.json()["data"]["trace_id"]
    assert retry_body["data"]["trace_id"] != retry_body["meta"]["trace_id"]
    assert "force_worker_error" not in retry_body["data"]
    assert retry_body["data"]["next_actions"][0]["key"] == "view_trace"

    replay = client.post(
        f"/api/v1/task-runs/{failed_run_id}/retries",
        json={
            "reason": "operator fixed downstream callback",
            "payload_overrides": {
                "force_worker_error": True,
                "run_id": failed_run_id,
                "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/retry",
            },
        },
        headers={**auth_headers, "Idempotency-Key": "worker-dead-letter-retry-new-run"},
    )
    assert replay.status_code == 202
    assert replay.json()["data"]["run_id"] == retry_run_id

    with SessionLocal() as session:
        failed_run = session.get(RunRecord, failed_run_id)
        retry_run = session.get(RunRecord, retry_run_id)
        retry_event = (
            session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == retry_run_id).one()
        )
        assert failed_run is not None
        assert failed_run.status == "failed"
        assert retry_run is not None
        assert retry_run.status == "pending"
        assert retry_event.status == "pending"
        assert retry_event.payload["retry_of_run_id"] == failed_run_id
        assert retry_event.payload["retry_of_event_id"] == failed_event.event_id
        assert retry_event.payload["retry_of_trace_id"] == failed_run.trace_id

    retry_trace = client.get(
        f"/api/v1/traces/{retry_body['data']['trace_id']}", headers=auth_headers
    )
    assert retry_trace.status_code == 200
    spans = retry_trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "run"
        and span.get("run_id") == retry_run_id
        and span.get("status") == "pending"
        for span in spans
    )
    assert any(
        span.get("kind") == "outbox"
        and span.get("event_type") == "task_run.requested"
        and span.get("aggregate_id") == retry_run_id
        for span in spans
    )


def test_non_failed_task_run_retry_is_rejected(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-retry-not-failed"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    retry = client.post(
        f"/api/v1/task-runs/{run_id}/retries",
        json={"reason": "should not retry a healthy run"},
        headers={**auth_headers, "Idempotency-Key": "worker-retry-not-failed-attempt"},
    )
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "RUN_NOT_RETRYABLE"


def test_outbox_worker_retries_structured_adapter_failure(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "simulate_adapter_failure": True,
            "adapter_error_code": "DAGSTER_TIMEOUT",
            "adapter_error_message": "Dagster did not accept the run in time",
            "adapter_retryable": True,
            "retry_after_seconds": 0,
            "max_attempts": 3,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-adapter-retry-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "running"
        assert event.status == "pending"
        assert event.last_error == "DAGSTER_TIMEOUT: Dagster did not accept the run in time"
        assert event.payload["adapter_dispatch"]["adapter"] == "dagster"
        assert event.payload["adapter_dispatch"]["retryable"] is True
        assert run.payload["retryable"] is True
        assert run.payload["retry_count"] == 1
        assert run.payload["next_retry_at"].startswith(event.available_at.isoformat())
        assert run.payload["error_code"] == "DAGSTER_TIMEOUT"
        assert run.payload["dispatch"]["adapter"] == "dagster"

    trace = client.get(
        f"/api/v1/traces/{response.json()['meta']['trace_id']}", headers=auth_headers
    )
    assert trace.status_code == 200
    outbox_span = next(
        span for span in trace.json()["data"]["spans"] if span.get("kind") == "outbox"
    )
    assert outbox_span["status"] == "pending"
    assert outbox_span["retryable"] is True
    assert outbox_span["error_code"] == "DAGSTER_TIMEOUT"
    assert outbox_span["adapter_dispatch"]["retryable"] is True


def test_outbox_worker_dead_letters_terminal_adapter_failure(client, auth_headers):
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/12",
            "simulate_adapter_failure": True,
            "adapter_error_code": "CALLBACK_SIGNATURE_INVALID",
            "adapter_error_message": "signature check failed",
            "adapter_retryable": False,
            "max_attempts": 3,
        },
        headers={**auth_headers, "Idempotency-Key": "worker-adapter-terminal-run"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_once() == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.requested",
            )
            .one()
        )
        assert run is not None
        assert run.status == "failed"
        assert event.status == "dead_letter"
        assert event.attempt_count == 1
        assert run.payload["error_code"] == "CALLBACK_SIGNATURE_INVALID"
        assert run.payload["retryable"] is False
        assert run.payload["retry_count"] == 1
        assert run.payload["dead_letter_event_id"] == event.event_id
        assert run.payload["dispatch"]["retryable"] is False

    trace = client.get(
        f"/api/v1/traces/{response.json()['meta']['trace_id']}", headers=auth_headers
    )
    assert trace.status_code == 200
    outbox_span = next(
        span for span in trace.json()["data"]["spans"] if span.get("kind") == "outbox"
    )
    assert outbox_span["status"] == "dead_letter"
    assert outbox_span["retryable"] is False
    assert outbox_span["error_code"] == "CALLBACK_SIGNATURE_INVALID"
    assert outbox_span["next_actions"][0]["key"] == "retry"


def test_outbox_worker_keeps_blocked_publish_behind_gate(client, auth_headers):
    version_id = "task_version_worker_blocked"
    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": version_id,
            "task_type_id": "task_sales_quality",
            "version": "v3.2.2-rc1",
        },
        headers={**auth_headers, "Idempotency-Key": "worker-blocked-version"},
    )
    assert created.status_code == 201, created.text
    response = client.post(
        f"/api/v1/task-versions/{version_id}/publish",
        json={"decision": "publish", "gate": "compatibility"},
        headers={**auth_headers, "Idempotency-Key": "worker-blocked-publish"},
    )
    assert response.status_code == 202
    run_id = response.json()["data"]["run_id"]

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "blocked"
        assert "dispatch" not in run.payload
        assert event.status == "blocked"
        assert event.last_error == "run is blocked by release gate or human confirmation"


def test_task_version_publish_gate_approval_requeues_and_materializes_version(client, auth_headers):
    version_id = "task_version_release_gate_approved"
    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": version_id,
            "task_type_id": "task_sales_quality",
            "version": "v3.3.0-rc1",
            "canvas_variant": "stable-v3",
            "label_version": "label_v1_8_4",
        },
        headers={**auth_headers, "Idempotency-Key": "task-release-gate-version"},
    )
    assert created.status_code == 201, created.text

    requested = client.post(
        f"/api/v1/task-versions/{version_id}/publish",
        json={"reason": "release candidate passed compatibility checks"},
        headers={**auth_headers, "Idempotency-Key": "task-release-gate-request"},
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    assert requested.json()["data"]["status"] == "blocked"
    assert requested.json()["data"]["release_gate"]["status"] == "awaiting_decision"

    assert process_aggregate_events([run_id]) == 1
    second_admin_token = _release_second_admin_token()
    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "兼容性、资产契约和回滚点均通过"},
        headers={
            **auth_headers,
            "Authorization": f"Bearer {second_admin_token}",
            "Idempotency-Key": "task-release-gate-approve",
        },
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "pending"
    assert approved.json()["data"]["release_gate"]["status"] == "approved"

    assert process_aggregate_events([run_id]) == 1
    detail = client.get(f"/api/v1/task-versions/{version_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "published"
    assert detail.json()["data"]["publish_run_id"] == run_id
    assert detail.json()["data"]["published_by"] == "u_annotator_001"

    run = client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)
    assert run.status_code == 200
    assert run.json()["data"]["status"] == "success"
    assert run.json()["data"]["release_materialization"]["resource_id"] == version_id


def test_settings_publish_gate_rejects_without_mutating_target_and_approval_materializes(
    client, auth_headers
):
    second_admin_token = _release_second_admin_token()
    rejected_draft_id = "settings_draft_rejected"
    rejected_draft = client.post(
        "/api/v1/settings/drafts",
        json={
            "settings_draft_id": rejected_draft_id,
            "setting_id": "model-chain",
            "changes": {"provider": "provider_rejected"},
            "reason": "negative path",
        },
        headers={**auth_headers, "Idempotency-Key": "settings-draft-rejected"},
    )
    assert rejected_draft.status_code == 201, rejected_draft.text
    rejected_request = client.post(
        "/api/v1/settings/publish-requests",
        json={"draft_id": rejected_draft_id},
        headers={**auth_headers, "Idempotency-Key": "settings-publish-rejected"},
    )
    assert rejected_request.status_code == 202, rejected_request.text
    rejected_run_id = rejected_request.json()["data"]["run_id"]
    assert process_aggregate_events([rejected_run_id]) == 1
    rejected = client.post(
        f"/api/v1/runs/{rejected_run_id}/decisions",
        json={"decision": "rejected", "reason": "影子评测未达到门槛"},
        headers={
            **auth_headers,
            "Authorization": f"Bearer {second_admin_token}",
            "Idempotency-Key": "settings-gate-reject",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["status"] == "cancelled"
    assert (
        client.get("/api/v1/settings/model-chain", headers=auth_headers).json()["data"]["provider"]
        == "self_hosted_asr"
    )

    approved_draft_id = "settings_draft_approved"
    approved_draft = client.post(
        "/api/v1/settings/drafts",
        json={
            "settings_draft_id": approved_draft_id,
            "setting_id": "model-chain",
            "changes": {
                "provider": "qwen_asr_shadow",
                "owner": "音频算法组",
            },
            "reason": "provider contract passed",
        },
        headers={**auth_headers, "Idempotency-Key": "settings-draft-approved"},
    )
    assert approved_draft.status_code == 201, approved_draft.text
    approved_request = client.post(
        "/api/v1/settings/publish-requests",
        json={"draft_id": approved_draft_id},
        headers={**auth_headers, "Idempotency-Key": "settings-publish-approved"},
    )
    assert approved_request.status_code == 202, approved_request.text
    approved_run_id = approved_request.json()["data"]["run_id"]
    approved = client.post(
        f"/api/v1/runs/{approved_run_id}/decisions",
        json={"decision": "approved", "reason": "审批与回滚点已确认"},
        headers={
            **auth_headers,
            "Authorization": f"Bearer {second_admin_token}",
            "Idempotency-Key": "settings-gate-approve",
        },
    )
    assert approved.status_code == 200, approved.text
    assert process_aggregate_events([approved_run_id]) == 1

    target = client.get("/api/v1/settings/model-chain", headers=auth_headers)
    assert target.status_code == 200, target.text
    assert target.json()["data"]["provider"] == "qwen_asr_shadow"
    assert target.json()["data"]["owner"] == "音频算法组"
    assert target.json()["data"]["publish_run_id"] == approved_run_id
    draft = client.get(f"/api/v1/settings/drafts/{approved_draft_id}", headers=auth_headers)
    assert draft.status_code == 200, draft.text
    assert draft.json()["data"]["status"] == "published"
