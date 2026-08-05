from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    LabelCandidate,
    LabelConflict,
)

LABEL_VERSION_ID = "label_v1_9_0_rc2"
CANDIDATE_ID = "cand_af128_amount_conflict"


def _policy_validation_payload() -> dict:
    return {
        "policy": {
            "dsl_version": "1.0",
            "policy_kind": "label-candidate",
            "policy_key": "contract-conflict-review-policy",
            "revision": 1,
            "fact_schema_version": "label-policy-facts/1",
            "thresholds": [],
            "rules": [
                {
                    "rule_id": "candidate-request-is-valid",
                    "priority": 100,
                    "when": {
                        "op": "eq",
                        "path": "request.action",
                        "value": "evaluate_candidate",
                    },
                    "effect": "pass",
                    "reason_code": "CANDIDATE_REQUEST_VALID",
                }
            ],
            "default_effect": "require_review",
        },
        "activate": True,
        "expected_label_resource_version": 1,
    }


def _create_policy_conflict(client, auth_headers, *, suffix: str) -> dict[str, str]:
    source_trace_id = f"trace_label_conflict_source_{suffix}"
    validation = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/policy/validate",
        json=_policy_validation_payload(),
        headers={
            **auth_headers,
            "Idempotency-Key": f"conflict-review-policy-{suffix}",
            "X-Trace-Id": source_trace_id,
        },
    )
    assert validation.status_code == 201, validation.text

    evaluation = client.post(
        "/api/v1/label-candidates/evaluate",
        json={
            "candidate_id": CANDIDATE_ID,
            "policy_version_id": validation.json()["data"]["policy_version_id"],
            "expected_candidate_resource_version": 1,
            "create_human_review": True,
            "facts": {
                "candidate": {
                    "source_type": "model_candidate",
                    "confidence_ppm": 950_000,
                    "version_matches": True,
                    "overwrites_human": False,
                    "business_document_conflict": False,
                },
                "evidence": {
                    "total_count": 1,
                    "valid_count": 1,
                    "pending_count": 0,
                    "cross_scope_count": 0,
                },
                "conflicts": {
                    "open_count": 0,
                    "high_risk_open_count": 0,
                    "human_disagreement_count": 0,
                    "equal_precedence_count": 0,
                },
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": f"conflict-review-evaluate-{suffix}",
            "X-Trace-Id": source_trace_id,
        },
    )
    assert evaluation.status_code == 201, evaluation.text
    data = evaluation.json()["data"]
    assert data["conflict_id"]
    assert data["review_task_id"]
    source_root_trace_id = evaluation.json()["meta"]["trace_id"]
    assert source_root_trace_id != source_trace_id

    with SessionLocal() as session:
        conflict = session.get(LabelConflict, data["conflict_id"])
        assert conflict is not None
        assert conflict.status == "detected"
        assert conflict.tenant_id == "aurora_auto"
        assert conflict.project_id == "sales_qa"
        assert conflict.trace_id == source_root_trace_id

    return {
        "conflict_id": data["conflict_id"],
        "review_task_id": data["review_task_id"],
        "source_trace_id": source_root_trace_id,
    }


def _keep_conflict_reference(
    review_task_id: str,
    *,
    reference_source: str,
) -> None:
    with SessionLocal() as session:
        resource = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == review_task_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        projection = session.get(HumanReviewTask, review_task_id)
        assert resource is not None
        assert projection is not None
        task_data = dict(resource.data)
        if reference_source == "target_refs":
            task_data.pop("label_conflict_id")
        elif reference_source == "label_conflict_id":
            task_data["target_refs"] = [
                target
                for target in task_data.get("target_refs") or []
                if target.get("type") != "label_conflict"
            ]
        resource.data = task_data
        projection.payload = task_data
        session.commit()


@pytest.mark.parametrize(
    ("terminal_decision", "reference_source"),
    [
        ("accepted", "target_refs"),
        ("modified", "label_conflict_id"),
        ("rejected", "both"),
    ],
)
def test_terminal_human_review_resolves_label_policy_conflict(
    client,
    auth_headers,
    terminal_decision,
    reference_source,
):
    created = _create_policy_conflict(client, auth_headers, suffix=terminal_decision)
    _keep_conflict_reference(
        created["review_task_id"],
        reference_source=reference_source,
    )
    decision_trace_id = f"trace_label_conflict_{terminal_decision}"
    body = {
        "decision": terminal_decision,
        "note": f"contract {terminal_decision}",
    }
    if terminal_decision == "modified":
        body["changes"] = [
            {
                "target_type": "label_candidate",
                "target_id": CANDIDATE_ID,
                "fields": {"value_or_action": "人工修正后的金额"},
            }
        ]

    response = client.post(
        f"/api/v1/human-review-tasks/{created['review_task_id']}/decisions",
        json=body,
        headers={
            **auth_headers,
            "Idempotency-Key": f"conflict-review-terminal-{terminal_decision}",
            "X-Trace-Id": decision_trace_id,
        },
    )
    assert response.status_code == 200, response.text
    response_data = response.json()["data"]
    decision_root_trace_id = response.json()["meta"]["trace_id"]
    assert decision_root_trace_id != decision_trace_id
    decision_id = response_data["decision_id"]
    conflict_receipt = next(
        item
        for item in response_data["affected_objects"]
        if item["type"] == "label_conflict" and item["id"] == created["conflict_id"]
    )
    assert conflict_receipt["readback_url"] == (
        f"/api/v1/human-review-decisions/{decision_id}/affected-objects/"
        f"label_conflict/{created['conflict_id']}"
    )

    with SessionLocal() as session:
        conflict = session.get(LabelConflict, created["conflict_id"])
        decision = session.get(HumanReviewDecision, decision_id)
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "human_review.label_conflict_writeback",
                AuditLog.object_id == created["conflict_id"],
            )
        )
        candidate = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "label_candidates",
                JsonResource.resource_key == CANDIDATE_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )

        assert conflict is not None
        assert conflict.status == "resolved"
        assert conflict.status not in {"detected", "reviewing"}
        assert conflict.trace_id == decision_root_trace_id
        assert conflict.payload["status"] == "resolved"
        assert conflict.payload["resolution"] == terminal_decision
        assert conflict.payload["resolution_note"] == body["note"]
        assert conflict.payload["review_decision_id"] == decision_id
        assert conflict.payload["review_task_id"] == created["review_task_id"]
        assert decision is not None
        assert conflict.payload["decided_by"] == decision.payload["decided_by"]
        assert conflict.payload["resolved_at"]
        assert conflict.payload["source_trace_id"] == created["source_trace_id"]

        before = decision.payload["before_json"]["label_conflicts"][created["conflict_id"]]
        after = decision.payload["after_json"]["label_conflicts"][created["conflict_id"]]
        assert before["status"] == "detected"
        assert before["trace_id"] == created["source_trace_id"]
        assert after["status"] == "resolved"
        assert after["payload"]["review_decision_id"] == decision_id
        assert any(
            item["type"] == "label_conflict" and item["id"] == created["conflict_id"]
            for item in decision.payload["affected_objects"]
        )

        assert audit is not None
        assert audit.before_json["status"] == "detected"
        assert audit.after_json["status"] == "resolved"
        assert candidate is not None
        assert candidate.data["review_decision_id"] == decision_id


def test_escalation_reviews_then_terminal_decision_resolves_conflict_once(
    client,
    auth_headers,
):
    created = _create_policy_conflict(client, auth_headers, suffix="escalation")
    escalated = client.post(
        f"/api/v1/human-review-tasks/{created['review_task_id']}/decisions",
        json={"decision": "escalated", "note": "升级到标签仲裁"},
        headers={
            **auth_headers,
            "Idempotency-Key": "conflict-review-escalated",
            "X-Trace-Id": "trace_label_conflict_escalated",
        },
    )
    assert escalated.status_code == 200, escalated.text
    escalated_id = escalated.json()["data"]["decision_id"]

    with SessionLocal() as session:
        conflict = session.get(LabelConflict, created["conflict_id"])
        decision = session.get(HumanReviewDecision, escalated_id)
        assert conflict is not None
        assert conflict.status == "reviewing"
        assert conflict.payload["resolution"] == "escalated"
        assert conflict.payload["resolved_at"] is None
        assert conflict.payload["source_trace_id"] == created["source_trace_id"]
        assert decision is not None
        assert (
            decision.payload["before_json"]["label_conflicts"][created["conflict_id"]]["status"]
            == "detected"
        )
        assert (
            decision.payload["after_json"]["label_conflicts"][created["conflict_id"]]["status"]
            == "reviewing"
        )

    terminal = client.post(
        f"/api/v1/human-review-tasks/{created['review_task_id']}/decisions",
        json={"decision": "accepted", "note": "仲裁完成"},
        headers={
            **auth_headers,
            "Idempotency-Key": "conflict-review-after-escalation",
            "X-Trace-Id": "trace_label_conflict_resolved",
        },
    )
    assert terminal.status_code == 200, terminal.text
    terminal_id = terminal.json()["data"]["decision_id"]

    duplicate = client.post(
        f"/api/v1/human-review-tasks/{created['review_task_id']}/decisions",
        json={"decision": "rejected", "note": "不能覆盖终态"},
        headers={
            **auth_headers,
            "Idempotency-Key": "conflict-review-duplicate-terminal",
            "X-Trace-Id": "trace_label_conflict_duplicate",
        },
    )
    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json()["error"]["code"] == "HUMAN_REVIEW_TASK_ALREADY_DECIDED"

    with SessionLocal() as session:
        conflict = session.get(LabelConflict, created["conflict_id"])
        decisions = list(
            session.scalars(
                select(HumanReviewDecision).where(
                    HumanReviewDecision.review_task_id == created["review_task_id"]
                )
            )
        )
        assert conflict is not None
        assert conflict.status == "resolved"
        assert conflict.payload["resolution"] == "accepted"
        assert conflict.payload["review_decision_id"] == terminal_id
        assert conflict.payload["source_trace_id"] == created["source_trace_id"]
        assert len(decisions) == 2


def test_cross_scope_label_conflict_reference_fails_closed(client, auth_headers):
    created = _create_policy_conflict(client, auth_headers, suffix="cross-scope")
    with SessionLocal() as session:
        conflict = session.get(LabelConflict, created["conflict_id"])
        assert conflict is not None
        conflict.tenant_id = "foreign_tenant"
        conflict.project_id = "foreign_project"
        session.commit()

    response = client.post(
        f"/api/v1/human-review-tasks/{created['review_task_id']}/decisions",
        json={"decision": "accepted", "note": "不得跨 scope 关闭"},
        headers={
            **auth_headers,
            "Idempotency-Key": "conflict-review-cross-scope",
            "X-Trace-Id": "trace_label_conflict_cross_scope_attempt",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "HUMAN_REVIEW_LABEL_CONFLICT_NOT_FOUND"

    with SessionLocal() as session:
        conflict = session.get(LabelConflict, created["conflict_id"])
        task = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == created["review_task_id"],
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        decision_count = len(
            list(
                session.scalars(
                    select(HumanReviewDecision).where(
                        HumanReviewDecision.review_task_id == created["review_task_id"]
                    )
                )
            )
        )
        candidate = session.get(LabelCandidate, CANDIDATE_ID)

        assert conflict is not None
        assert conflict.status == "detected"
        assert "review_decision_id" not in conflict.payload
        assert task is not None
        assert task.status == "pending"
        assert "decision_id" not in task.data
        assert decision_count == 0
        assert candidate is not None
        assert candidate.status == "blocked"
