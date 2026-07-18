from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models import (
    HumanReviewDecision,
    HumanReviewTask,
    InsightAction,
    InsightReport,
    JsonResource,
    MetricResult,
    OutboxEvent,
    RunRecord,
)


def _seed_action_review_task() -> tuple[str, str]:
    action_id = "insight_action_concurrent_review"
    review_task_id = "hrt_concurrent_action"
    action_payload = {
        "id": action_id,
        "insight_action_id": action_id,
        "status": "pending_review",
        "resource_version": 1,
    }
    task_payload = {
        "id": review_task_id,
        "status": "pending",
        "queue": "insight_action_review",
        "target_refs": [{"type": "work_item", "id": action_id}],
    }
    with SessionLocal() as session:
        session.add(
            RunRecord(
                run_id="insight_report_run_concurrency_probe",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="insight_report",
                status="success",
                run_key="insight_report_run_concurrency_probe",
                partition_key=None,
                trace_id="trace_concurrency_probe",
                payload={"status": "success"},
            )
        )
        session.add(
            MetricResult(
                metric_result_id="metric_concurrency_probe",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="materialized",
                trace_id="trace_concurrency_probe",
                payload={
                    "metric_key": "quote_consistency",
                    "snapshot_role": "aggregation",
                    "immutable": True,
                    "source_run_id": "metric_source_concurrency_probe",
                },
            )
        )
        session.flush()
        session.add(
            InsightReport(
                report_id="report_concurrency_probe",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_id="insight_report_run_concurrency_probe",
                status="generated",
                report_type="management_summary",
                trace_id="trace_concurrency_probe",
                payload={"id": "report_concurrency_probe", "status": "generated"},
            )
        )
        session.flush()
        session.add(
            InsightAction(
                action_id=action_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                report_id="report_concurrency_probe",
                baseline_metric_result_id="metric_concurrency_probe",
                action_type="create_training_action",
                branch="human_review",
                risk_level="high",
                status="pending_review",
                review_task_id=review_task_id,
                resource_version=1,
                trace_id="trace_concurrency_probe",
                payload=action_payload,
            )
        )
        session.add(
            JsonResource(
                collection="work_items",
                resource_key=action_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="pending_review",
                trace_id="trace_concurrency_probe",
                data=action_payload,
            )
        )
        session.add(
            HumanReviewTask(
                review_task_id=review_task_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="pending",
                trace_id="trace_concurrency_probe",
                payload=task_payload,
            )
        )
        session.add(
            JsonResource(
                collection="human_review_tasks",
                resource_key=review_task_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="pending",
                trace_id="trace_concurrency_probe",
                data=task_payload,
            )
        )
        session.commit()
    return action_id, review_task_id


def test_human_review_terminal_unique_constraint_is_database_enforced():
    with SessionLocal() as session:
        session.add(
            HumanReviewDecision(
                decision_id="hrd_constraint_first",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                review_task_id="hrt_constraint_probe",
                terminal_review_task_id="hrt_constraint_probe",
                status="success",
                trace_id="trace_constraint_first",
                payload={"decision": "accepted", "review_task_id": "hrt_constraint_probe"},
            )
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(IntegrityError):
        session.add(
            HumanReviewDecision(
                decision_id="hrd_constraint_second",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                review_task_id="hrt_constraint_probe",
                terminal_review_task_id="hrt_constraint_probe",
                status="blocked",
                trace_id="trace_constraint_second",
                payload={"decision": "rejected", "review_task_id": "hrt_constraint_probe"},
            )
        )
        session.commit()


def test_concurrent_same_idempotency_key_replays_one_human_review_decision(
    client,
    auth_headers,
):
    start = Barrier(2)
    headers = {**auth_headers, "Idempotency-Key": "human-review-concurrent-replay"}
    payload = {"decision": "accepted", "note": "并发同键只执行一次"}

    def decide():
        start.wait(timeout=5)
        return client.post(
            "/api/v1/human-review-tasks/hrt_amount_001/decisions",
            json=payload,
            headers=headers,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(decide), executor.submit(decide))
        responses = [future.result() for future in futures]

    assert [response.status_code for response in responses] == [200, 200]
    decision_ids = {response.json()["data"]["decision_id"] for response in responses}
    assert len(decision_ids) == 1
    with SessionLocal() as session:
        decisions = (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == "hrt_amount_001")
            .all()
        )
        assert len(decisions) == 1


def test_concurrent_human_review_decisions_converge_to_one_terminal_write(
    client,
    auth_headers,
):
    action_id, review_task_id = _seed_action_review_task()
    start = Barrier(2)

    def decide(key: str, decision: str):
        start.wait(timeout=5)
        return client.post(
            f"/api/v1/human-review-tasks/{review_task_id}/decisions",
            json={"decision": decision, "note": f"并发决策 {decision}"},
            headers={**auth_headers, "Idempotency-Key": key},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda args: decide(*args),
                [
                    ("human-review-concurrent-accept", "accepted"),
                    ("human-review-concurrent-reject", "rejected"),
                ],
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] == "HUMAN_REVIEW_TASK_ALREADY_DECIDED"

    with SessionLocal() as session:
        decisions = (
            session.query(HumanReviewDecision)
            .filter(HumanReviewDecision.review_task_id == review_task_id)
            .all()
        )
        runs = (
            session.query(RunRecord)
            .filter(
                RunRecord.run_type == "human_review_decision",
                RunRecord.payload["review_task_id"].as_string() == review_task_id,
            )
            .all()
        )
        events = (
            session.query(OutboxEvent)
            .filter(
                OutboxEvent.event_type == "human_review.decision.created",
                OutboxEvent.payload["review_task_id"].as_string() == review_task_id,
            )
            .all()
        )
        action = session.get(InsightAction, action_id)
        task = session.get(HumanReviewTask, review_task_id)
        assert action is not None
        assert task is not None
        assert len(decisions) == 1
        assert len(runs) == 1
        assert len(events) == 1
        assert action.resource_version == 2
        assert len(task.payload["decision_history"]) == 1
