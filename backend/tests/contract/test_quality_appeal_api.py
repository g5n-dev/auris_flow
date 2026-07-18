from __future__ import annotations

from copy import deepcopy

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    OutboxEvent,
    QualityAppeal,
)


def _seed_terminal_decision(
    *,
    decision_id: str,
    decided_by: str = "u_model_001",
    terminal: bool = True,
) -> None:
    review_task_id = f"hrt_{decision_id}"
    decision = "accepted" if terminal else "escalated"
    with SessionLocal() as session:
        session.add(
            HumanReviewTask(
                review_task_id=review_task_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="success" if terminal else "under_review",
                trace_id=f"trace_{decision_id}",
                payload={
                    "id": review_task_id,
                    "review_task_id": review_task_id,
                    "queue": "amount_conflict",
                    "status": "success" if terminal else "under_review",
                    "trace_id": f"trace_{decision_id}",
                },
            )
        )
        session.add(
            HumanReviewDecision(
                decision_id=decision_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                review_task_id=review_task_id,
                terminal_review_task_id=review_task_id if terminal else None,
                status="success" if terminal else "escalated",
                trace_id=f"trace_{decision_id}",
                payload={
                    "decision_id": decision_id,
                    "review_task_id": review_task_id,
                    "decision": decision,
                    "decided_by": decided_by,
                    "trace_id": f"trace_{decision_id}",
                    "source_trace_id": f"trace_root_{decision_id}",
                    "affected_objects": [{"type": "evidence_pack", "id": "AF-128"}],
                },
            )
        )
        session.commit()


def _write_headers(
    auth_headers: dict[str, str],
    *,
    key: str,
    token: str = "dev-token",
    trace_id: str | None = None,
) -> dict[str, str]:
    headers = {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    return headers


def _create_appeal(
    client,
    auth_headers: dict[str, str],
    *,
    decision_id: str,
    key: str,
    trace_id: str | None = None,
):
    return client.post(
        "/api/v1/quality-appeals",
        json={
            "source_decision_id": decision_id,
            "reason": "The source quality result omitted material evidence.",
            "evidence_refs": ["evidence://AF-128/transcript#L42-L51"],
        },
        headers=_write_headers(auth_headers, key=key, trace_id=trace_id),
    )


def test_quality_appeal_submission_freezes_source_and_is_idempotent(client, auth_headers):
    decision_id = "hrd_appeal_contract_source"
    _seed_terminal_decision(decision_id=decision_id)

    missing_key = client.post(
        "/api/v1/quality-appeals",
        json={
            "source_decision_id": decision_id,
            "reason": "Missing evidence.",
            "evidence_refs": ["evidence://AF-128/quote"],
        },
        headers=auth_headers,
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    first = _create_appeal(
        client,
        auth_headers,
        decision_id=decision_id,
        key="quality-appeal-submit-contract",
        trace_id="trace_quality_appeal_submit",
    )
    replay = _create_appeal(
        client,
        auth_headers,
        decision_id=decision_id,
        key="quality-appeal-submit-contract",
        trace_id="trace_quality_appeal_submit",
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()
    data = first.json()["data"]
    assert data["id"] == data["appeal_id"]
    assert data["source_decision_id"] == decision_id
    assert data["source_review_task_id"] == f"hrt_{decision_id}"
    assert data["review_task_id"].startswith("hrt_qap_")
    assert data["appeal_decision_id"] is None
    assert len(data["source_result_sha256"]) == 64
    assert data["source_trace_id"] == f"trace_{decision_id}"
    assert data["root_trace_id"] == f"trace_root_{decision_id}"
    assert data["current_trace_id"] == first.json()["meta"]["trace_id"]
    assert data["current_trace_id"] != "trace_quality_appeal_submit"
    assert data["appellant_id"] == "u_admin_001"
    assert data["evidence_refs"] == ["evidence://AF-128/transcript#L42-L51"]
    assert data["status"] == "submitted"
    assert data["resource_version"] == 1
    assert data["reviewer_id"] is None
    assert data["decision"] is None

    detail = client.get(f"/api/v1/quality-appeals/{data['appeal_id']}", headers=auth_headers)
    listing = client.get(
        "/api/v1/quality-appeals?status=submitted",
        headers=auth_headers,
    )
    assert detail.status_code == 200
    assert detail.json()["data"] == data
    assert listing.status_code == 200
    assert [item["appeal_id"] for item in listing.json()["data"]["items"]] == [data["appeal_id"]]

    with SessionLocal() as session:
        appeals = session.query(QualityAppeal).all()
        appeal_task = session.get(HumanReviewTask, data["review_task_id"])
        appeal_task_resource = (
            session.query(JsonResource)
            .filter(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == data["review_task_id"],
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
            .one_or_none()
        )
        events = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "quality_appeal.submitted")
            .all()
        )
        audits = session.query(AuditLog).filter(AuditLog.action == "quality_appeal.submitted").all()
        assert len(appeals) == 1
        assert appeal_task is not None
        assert appeal_task_resource is not None
        assert appeal_task.status == "submitted"
        assert appeal_task.payload["queue"] == "quality_appeal"
        assert appeal_task.payload["appeal_id"] == data["appeal_id"]
        assert appeal_task_resource.data["queue"] == "quality_appeal"
        assert appeal_task_resource.data["appeal_id"] == data["appeal_id"]
        assert len(events) == 1
        assert len(audits) == 1

    queue = client.get(
        "/api/v1/human-review-tasks?queue=quality_appeal",
        headers=auth_headers,
    )
    assert queue.status_code == 200
    assert [item["id"] for item in queue.json()["data"]["items"]] == [data["review_task_id"]]

    trace = client.get(
        f"/api/v1/traces/{data['current_trace_id']}",
        headers=auth_headers,
    )
    assert trace.status_code == 200, trace.text
    spans = trace.json()["data"]["spans"]
    assert any(
        span.get("kind") == "audit" and span.get("object_id") == data["appeal_id"] for span in spans
    )
    assert any(
        span.get("kind") == "outbox" and span.get("event_type") == "quality_appeal.submitted"
        for span in spans
    )


def test_generic_human_review_decision_rejects_quality_appeal_queue(client, auth_headers):
    source_decision_id = "hrd_appeal_generic_bypass_source"
    _seed_terminal_decision(decision_id=source_decision_id)
    created = _create_appeal(
        client,
        auth_headers,
        decision_id=source_decision_id,
        key="quality-appeal-generic-bypass-create",
    )
    review_task_id = created.json()["data"]["review_task_id"]

    bypass = client.post(
        f"/api/v1/human-review-tasks/{review_task_id}/decisions",
        json={"decision": "accepted", "note": "Attempt generic bypass."},
        headers=_write_headers(
            auth_headers,
            key="quality-appeal-generic-bypass-decision",
            token="annotator-token",
        ),
    )

    assert bypass.status_code == 409
    assert bypass.json()["error"]["code"] == "QUALITY_APPEAL_SPECIALIZED_DECISION_REQUIRED"
    with SessionLocal() as session:
        appeal = (
            session.query(QualityAppeal)
            .filter(
                QualityAppeal.review_task_id == review_task_id,
            )
            .one_or_none()
        )
        assert appeal is not None
        assert appeal.status == "submitted"
        assert appeal.appeal_decision_id is None
        assert (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == review_task_id)
            .count()
            == 0
        )


def test_quality_appeal_rejects_non_terminal_source(client, auth_headers):
    decision_id = "hrd_appeal_escalated_source"
    _seed_terminal_decision(decision_id=decision_id, terminal=False)

    response = _create_appeal(
        client,
        auth_headers,
        decision_id=decision_id,
        key="quality-appeal-non-terminal-source",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUALITY_APPEAL_SOURCE_NOT_TERMINAL"


def test_appellant_and_original_decider_cannot_claim_review(client, auth_headers):
    self_source = "hrd_appeal_self_review_source"
    _seed_terminal_decision(decision_id=self_source)
    created = _create_appeal(
        client,
        auth_headers,
        decision_id=self_source,
        key="quality-appeal-self-review-create",
    )
    appeal_id = created.json()["data"]["appeal_id"]

    self_claim = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/claims",
        json={"expected_resource_version": 1},
        headers=_write_headers(auth_headers, key="quality-appeal-self-review-claim"),
    )
    assert self_claim.status_code == 403
    assert self_claim.json()["error"]["code"] == "QUALITY_APPEAL_SELF_REVIEW_FORBIDDEN"

    original_reviewer_source = "hrd_appeal_original_reviewer_source"
    _seed_terminal_decision(
        decision_id=original_reviewer_source,
        decided_by="u_annotator_001",
    )
    second = _create_appeal(
        client,
        auth_headers,
        decision_id=original_reviewer_source,
        key="quality-appeal-original-reviewer-create",
    )
    original_claim = client.post(
        f"/api/v1/quality-appeals/{second.json()['data']['appeal_id']}/claims",
        json={"expected_resource_version": 1},
        headers=_write_headers(
            auth_headers,
            key="quality-appeal-original-reviewer-claim",
            token="annotator-token",
        ),
    )
    assert original_claim.status_code == 403
    assert original_claim.json()["error"]["code"] == (
        "QUALITY_APPEAL_ORIGINAL_DECIDER_REVIEW_FORBIDDEN"
    )


def test_quality_appeal_claim_and_decision_leave_original_decision_immutable(
    client,
    auth_headers,
):
    source_decision_id = "hrd_appeal_resolution_source"
    _seed_terminal_decision(decision_id=source_decision_id)
    with SessionLocal() as session:
        original = session.get(HumanReviewDecision, source_decision_id)
        assert original is not None
        original_snapshot = {
            "status": original.status,
            "trace_id": original.trace_id,
            "payload": deepcopy(original.payload),
            "updated_at": original.updated_at,
        }

    created = _create_appeal(
        client,
        auth_headers,
        decision_id=source_decision_id,
        key="quality-appeal-resolution-create",
    )
    appeal_id = created.json()["data"]["appeal_id"]
    reviewer_headers = _write_headers(
        auth_headers,
        key="quality-appeal-resolution-claim",
        token="annotator-token",
        trace_id="trace_quality_appeal_claim",
    )
    claimed = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/claims",
        json={"expected_resource_version": 1},
        headers=reviewer_headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["status"] == "under_review"
    assert claimed.json()["data"]["reviewer_id"] == "u_annotator_001"
    assert claimed.json()["data"]["resource_version"] == 2
    assert claimed.json()["data"]["current_trace_id"] == claimed.json()["meta"]["trace_id"]
    assert claimed.json()["data"]["current_trace_id"] != "trace_quality_appeal_claim"
    with SessionLocal() as session:
        appeal_task = session.get(
            HumanReviewTask,
            claimed.json()["data"]["review_task_id"],
        )
        assert appeal_task is not None
        assert appeal_task.status == "under_review"
        assert appeal_task.payload["reviewer_id"] == "u_annotator_001"

    stale = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/decisions",
        json={
            "decision": "original_overturned",
            "reason": "The frozen evidence changes the result.",
            "expected_resource_version": 1,
        },
        headers=_write_headers(
            auth_headers,
            key="quality-appeal-resolution-stale",
            token="annotator-token",
        ),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "QUALITY_APPEAL_VERSION_CONFLICT"

    resolved = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/decisions",
        json={
            "decision": "original_overturned",
            "reason": "The frozen evidence changes the result.",
            "expected_resource_version": 2,
        },
        headers=_write_headers(
            auth_headers,
            key="quality-appeal-resolution-decide",
            token="annotator-token",
            trace_id="trace_quality_appeal_resolved",
        ),
    )
    assert resolved.status_code == 200, resolved.text
    result = resolved.json()["data"]
    assert result["status"] == "resolved"
    assert result["decision"] == "original_overturned"
    assert result["decision_reason"] == "The frozen evidence changes the result."
    assert result["appeal_decision_id"].startswith("hrd_qap_")
    assert result["resource_version"] == 3
    assert result["current_trace_id"] == resolved.json()["meta"]["trace_id"]
    assert result["current_trace_id"] != "trace_quality_appeal_resolved"

    second_level = _create_appeal(
        client,
        auth_headers,
        decision_id=result["appeal_decision_id"],
        key="quality-appeal-second-level-rejected",
    )
    assert second_level.status_code == 422
    assert second_level.json()["error"]["code"] == "QUALITY_APPEAL_SOURCE_NOT_TERMINAL"

    with SessionLocal() as session:
        original = session.get(HumanReviewDecision, source_decision_id)
        assert original is not None
        assert original.status == original_snapshot["status"]
        assert original.trace_id == original_snapshot["trace_id"]
        assert original.payload == original_snapshot["payload"]
        assert original.updated_at == original_snapshot["updated_at"]
        appeal = session.get(QualityAppeal, appeal_id)
        appeal_decision = session.get(HumanReviewDecision, result["appeal_decision_id"])
        appeal_task = session.get(HumanReviewTask, result["review_task_id"])
        appeal_task_resource = (
            session.query(JsonResource)
            .filter(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == result["review_task_id"],
            )
            .one_or_none()
        )
        assert appeal is not None
        assert appeal_decision is not None
        assert appeal_task is not None
        assert appeal_task_resource is not None
        assert appeal.appeal_decision_id == appeal_decision.decision_id
        assert appeal_decision.review_task_id == appeal.review_task_id
        assert appeal_decision.terminal_review_task_id == appeal.review_task_id
        assert appeal_decision.payload["appeal_id"] == appeal_id
        assert appeal_decision.payload["decision"] == "original_overturned"
        assert appeal_decision.payload["supersedes_source_decision_id"] == source_decision_id
        assert appeal_decision.payload["decided_by"] == "u_annotator_001"
        assert appeal_task.status == "resolved"
        assert appeal_task.payload["appeal_decision_id"] == appeal_decision.decision_id
        assert appeal_task_resource.status == "resolved"
        assert appeal_task_resource.data["appeal_decision_id"] == appeal_decision.decision_id
        terminal_events = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.event_type == "quality_appeal.resolved",
                OutboxEvent.aggregate_id == appeal_id,
            )
            .all()
        )
        assert len(terminal_events) == 1


def test_appellant_can_withdraw_submitted_appeal(client, auth_headers):
    source_decision_id = "hrd_appeal_withdrawal_source"
    _seed_terminal_decision(decision_id=source_decision_id)
    created = _create_appeal(
        client,
        auth_headers,
        decision_id=source_decision_id,
        key="quality-appeal-withdraw-create",
    )
    appeal_id = created.json()["data"]["appeal_id"]

    withdrawn = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/withdrawals",
        json={
            "reason": "The appellant supplied the wrong evidence reference.",
            "expected_resource_version": 1,
        },
        headers=_write_headers(auth_headers, key="quality-appeal-withdraw"),
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["data"]["status"] == "withdrawn"
    assert withdrawn.json()["data"]["resource_version"] == 2
    assert withdrawn.json()["data"]["appeal_decision_id"] is None
    with SessionLocal() as session:
        appeal_task = session.get(
            HumanReviewTask,
            withdrawn.json()["data"]["review_task_id"],
        )
        assert appeal_task is not None
        assert appeal_task.status == "withdrawn"

    claim_after_withdrawal = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/claims",
        json={"expected_resource_version": 2},
        headers=_write_headers(
            auth_headers,
            key="quality-appeal-claim-after-withdrawal",
            token="annotator-token",
        ),
    )
    assert claim_after_withdrawal.status_code == 409
    assert claim_after_withdrawal.json()["error"]["code"] == ("QUALITY_APPEAL_INVALID_TRANSITION")
