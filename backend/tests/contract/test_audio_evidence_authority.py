from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import EvidencePack, HumanReviewDecision, HumanReviewTask, JsonResource


def test_evidence_pack_direct_write_is_forbidden(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/evidence-packs",
        json={
            "evidence_pack_id": "weak-client-evidence",
            "audio_session_id": "S20250526-000128",
            "evidence_sha256": "0" * 64,
            "status": "ready",
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "weak-client-evidence",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVIDENCE_PACK_MATERIALIZER_REQUIRED"
    with SessionLocal() as session:
        assert session.get(EvidencePack, "weak-client-evidence") is None
        assert (
            session.scalar(
                select(JsonResource).where(
                    JsonResource.collection == "evidence_packs",
                    JsonResource.resource_key == "weak-client-evidence",
                )
            )
            is None
        )


def test_evidence_readback_uses_strong_immutable_body_and_allowlisted_overlay(
    client,
    auth_headers,
) -> None:
    with SessionLocal() as session:
        strong = session.get(EvidencePack, "AF-128")
        assert strong is not None
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "evidence_packs",
                JsonResource.resource_key == "AF-128",
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert projection is not None
        projection.status = "ready"
        projection.data = {
            **projection.data,
            "audio_session_id": "forged-session",
            "recording_id": "forged-recording",
            "storage_object": {
                "storage_object_id": "forged-storage",
                "version_id": "forged-version",
                "content_sha256": "f" * 64,
            },
            "asr_result": {
                "asr_result_id": "forged-asr",
                "version": "forged-version",
                "segments_sha256": "f" * 64,
            },
            "time_window": {"start_ms": 1, "end_ms": 2},
            "evidence_sha256": "f" * 64,
            "resource_version": 999,
            "root_trace_id": "forged-root",
            "status": "ready",
            "review_state": "modified",
            "manual_decision": {"forged": True},
            "review_decision_id": "hrd-safe-overlay",
            "review_overrides": {
                "recording_disposition": "main",
                "low_confidence": True,
                "storage_object_version": "forged-overlay",
            },
        }
        session.commit()

    evidence_response = client.get("/api/v1/evidence-packs/AF-128", headers=auth_headers)
    task_response = client.get(
        "/api/v1/human-review-tasks/hrt_amount_001",
        headers=auth_headers,
    )

    assert evidence_response.status_code == 200
    assert task_response.status_code == 200
    evidence = evidence_response.json()["data"]
    embedded = task_response.json()["data"]["evidence_pack"]
    for readback in (evidence, embedded):
        assert readback["audio_session_id"] == strong.audio_session_id
        assert readback["recording_id"] == strong.recording_id
        assert readback["storage_object"]["storage_object_id"] == strong.storage_object_id
        assert readback["storage_object"]["version_id"] == strong.storage_object_version
        assert readback["storage_object"]["content_sha256"] == strong.audio_sha256
        assert readback["asr_result"]["asr_result_id"] == strong.asr_result_id
        assert readback["asr_result"]["version"] == strong.asr_result_version
        assert readback["time_window"] == {
            "start_ms": strong.window_start_ms,
            "end_ms": strong.window_end_ms,
        }
        assert readback["evidence_sha256"] == strong.evidence_sha256
        assert readback["resource_version"] == strong.resource_version
        assert readback["root_trace_id"] == strong.root_trace_id
        assert readback["status"] == strong.status
        assert readback["review_state"] == "modified"
        assert "manual_decision" not in readback
        assert readback["review_decision_id"] == "hrd-safe-overlay"
        assert readback["review_overrides"] == {
            "recording_disposition": "main",
            "low_confidence": True,
        }


def test_audio_review_cannot_decide_around_orphan_json_evidence(
    client,
    auth_headers,
) -> None:
    task_id = "hrt-orphan-json-evidence"
    evidence_id = "weak-json-evidence"
    task_payload = {
        "id": task_id,
        "review_task_id": task_id,
        "status": "pending",
        "queue": "audio_evidence_review",
        "evidence_pack_id": evidence_id,
        "audio_session_id": "S20250526-000128",
        "recording_id": "rec_A_1001_20250526_122300",
        "root_trace_id": "trace-orphan-json-evidence",
        "target_refs": [{"type": "evidence_pack", "id": evidence_id}],
    }
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="evidence_packs",
                resource_key=evidence_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="ready",
                trace_id="trace-orphan-json-evidence",
                data={
                    "id": evidence_id,
                    "evidence_pack_id": evidence_id,
                    "status": "ready",
                    "evidence_sha256": "a" * 64,
                },
            )
        )
        session.add(
            JsonResource(
                collection="human_review_tasks",
                resource_key=task_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="pending",
                trace_id="trace-orphan-json-evidence",
                data=task_payload,
            )
        )
        session.add(
            HumanReviewTask(
                review_task_id=task_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="pending",
                trace_id="trace-orphan-json-evidence",
                payload=task_payload,
            )
        )
        session.commit()

    response = client.post(
        f"/api/v1/human-review-tasks/{task_id}/decisions",
        json={"decision": "accepted"},
        headers={
            **auth_headers,
            "Idempotency-Key": "orphan-json-evidence-decision",
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIO_EVIDENCE_STRONG_BINDING_REQUIRED"


def test_client_cannot_forge_a_second_terminal_review_for_materialized_evidence(
    client,
    auth_headers,
) -> None:
    decided = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "确认唯一受控任务"},
        headers={
            **auth_headers,
            "Idempotency-Key": "audio-evidence-authoritative-decision",
        },
    )
    assert decided.status_code == 200, decided.text

    forged_payloads = [
        {
            "id": "hrt_forged_evidence_reference",
            "queue": "manual",
            "evidence_pack_id": "AF-128",
        },
        {
            "id": "hrt_forged_audio_target",
            "queue": "manual",
            "target_refs": [{"type": "evidence_pack", "id": "AF-128"}],
        },
        {
            "id": "hrt_forged_audio_queue",
            "queue": "audio_evidence_review",
        },
    ]
    for index, payload in enumerate(forged_payloads):
        forged = client.post(
            "/api/v1/human-review-tasks",
            json=payload,
            headers={
                **auth_headers,
                "Idempotency-Key": f"forged-audio-evidence-task-{index}",
            },
        )
        assert forged.status_code == 409, forged.text
        assert forged.json()["error"]["code"] == "AUDIO_EVIDENCE_MATERIALIZER_REQUIRED"

    with SessionLocal() as session:
        decisions = list(
            session.scalars(
                select(HumanReviewDecision).where(
                    HumanReviewDecision.payload["evidence_pack_id"].as_string() == "AF-128"
                )
            )
        )
        forged_tasks = list(
            session.scalars(
                select(HumanReviewTask).where(
                    HumanReviewTask.review_task_id.in_(
                        [
                            "hrt_forged_evidence_reference",
                            "hrt_forged_audio_target",
                            "hrt_forged_audio_queue",
                        ]
                    )
                )
            )
        )

    assert len(decisions) == 1
    assert decisions[0].review_task_id == "hrt_amount_001"
    assert forged_tasks == []
