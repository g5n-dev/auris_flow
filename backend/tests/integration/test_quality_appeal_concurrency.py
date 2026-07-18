from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

from app.core.database import SessionLocal
from app.models import AuditLog, HumanReviewDecision, OutboxEvent, QualityAppeal


def _seed_source_decision() -> str:
    decision_id = "hrd_quality_appeal_concurrency"
    review_task_id = "hrt_quality_appeal_concurrency"
    with SessionLocal() as session:
        session.add(
            HumanReviewDecision(
                decision_id=decision_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                review_task_id=review_task_id,
                terminal_review_task_id=review_task_id,
                status="success",
                trace_id="trace_quality_appeal_source",
                payload={
                    "decision_id": decision_id,
                    "review_task_id": review_task_id,
                    "decision": "accepted",
                    "decided_by": "u_model_001",
                    "trace_id": "trace_quality_appeal_source",
                    "source_trace_id": "trace_quality_appeal_root",
                },
            )
        )
        session.commit()
    return decision_id


def _headers(
    auth_headers: dict[str, str],
    *,
    key: str,
    token: str = "dev-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def test_concurrent_quality_appeal_decisions_create_only_one_terminal_write(
    client,
    auth_headers,
):
    source_decision_id = _seed_source_decision()
    created = client.post(
        "/api/v1/quality-appeals",
        json={
            "source_decision_id": source_decision_id,
            "reason": "Concurrent decision probe.",
            "evidence_refs": ["evidence://concurrency/probe"],
        },
        headers=_headers(auth_headers, key="quality-appeal-concurrency-create"),
    )
    assert created.status_code == 201, created.text
    appeal_id = created.json()["data"]["appeal_id"]

    claimed = client.post(
        f"/api/v1/quality-appeals/{appeal_id}/claims",
        json={"expected_resource_version": 1},
        headers=_headers(
            auth_headers,
            key="quality-appeal-concurrency-claim",
            token="annotator-token",
        ),
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["resource_version"] == 2

    with SessionLocal() as session:
        original = session.get(HumanReviewDecision, source_decision_id)
        assert original is not None
        original_payload = deepcopy(original.payload)
        original_updated_at = original.updated_at

    start = Barrier(2)

    def decide(key: str, decision: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/quality-appeals/{appeal_id}/decisions",
            json={
                "decision": decision,
                "reason": f"Concurrent terminal result: {decision}",
                "expected_resource_version": 2,
            },
            headers=_headers(auth_headers, key=key, token="annotator-token"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda values: decide(*values),
                [
                    ("quality-appeal-concurrent-upheld", "original_upheld"),
                    ("quality-appeal-concurrent-remanded", "original_remanded"),
                ],
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] in {
        "QUALITY_APPEAL_INVALID_TRANSITION",
        "QUALITY_APPEAL_VERSION_CONFLICT",
    }

    with SessionLocal() as session:
        appeal = session.get(QualityAppeal, appeal_id)
        original = session.get(HumanReviewDecision, source_decision_id)
        assert appeal is not None
        assert original is not None
        assert appeal.status == "resolved"
        assert appeal.resource_version == 3
        assert appeal.decision in {"original_upheld", "original_remanded"}
        assert appeal.appeal_decision_id is not None
        assert original.payload == original_payload
        assert original.updated_at == original_updated_at
        appeal_decisions = (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == appeal.review_task_id)
            .all()
        )
        assert len(appeal_decisions) == 1
        assert appeal_decisions[0].decision_id == appeal.appeal_decision_id
        assert appeal_decisions[0].terminal_review_task_id == appeal.review_task_id
        assert appeal_decisions[0].payload["appeal_id"] == appeal_id
        assert appeal_decisions[0].payload["supersedes_source_decision_id"] == source_decision_id
        assert appeal_decisions[0].payload["decision"] == appeal.decision
        terminal_events = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.event_type == "quality_appeal.resolved",
                OutboxEvent.aggregate_id == appeal_id,
            )
            .all()
        )
        terminal_audits = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "quality_appeal.resolved",
                AuditLog.object_id == appeal_id,
            )
            .all()
        )
        assert len(terminal_events) == 1
        assert len(terminal_audits) == 1
