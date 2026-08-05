from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import (
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    OutboxEvent,
    RunRecord,
)
from app.workers.outbox_worker import process_once


def _bind_review_output_sink(*, missing: bool = False) -> None:
    output_sink_id = "output_sink_missing" if missing else "output_sink_review_crm"
    with SessionLocal() as session:
        task = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == "hrt_amount_001",
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        strong_task = session.scalar(
            select(HumanReviewTask).where(
                HumanReviewTask.review_task_id == "hrt_amount_001",
                HumanReviewTask.tenant_id == "aurora_auto",
                HumanReviewTask.project_id == "sales_qa",
            )
        )
        assert task is not None and strong_task is not None
        task.data = {**task.data, "output_sink_refs": [output_sink_id]}
        strong_task.payload = {**strong_task.payload, "output_sink_refs": [output_sink_id]}
        if not missing:
            session.add(
                JsonResource(
                    collection="output_sinks",
                    resource_key=output_sink_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id="trace_output_sink_review_crm",
                    data={
                        "id": output_sink_id,
                        "output_sink_id": output_sink_id,
                        "type": "platform_callback",
                        "target": "crm_reception_order",
                        "status": "active",
                    },
                )
            )
        session.commit()


def test_terminal_review_atomically_creates_real_platform_callback_and_readback(
    client,
    auth_headers,
) -> None:
    initially_empty = client.get(
        "/api/v1/output-sinks/platform-callbacks",
        headers=auth_headers,
    )
    assert initially_empty.status_code == 200
    assert initially_empty.json()["data"]["items"] == []

    _bind_review_output_sink()
    decision = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "确认后回写 CRM"},
        headers={
            **auth_headers,
            "Idempotency-Key": "human-review-platform-callback",
        },
    )
    assert decision.status_code == 200, decision.text
    receipt = decision.json()["data"]
    callback_ref = next(
        item for item in receipt["affected_objects"] if item["type"] == "platform_callback"
    )
    callback_run_id = callback_ref["id"]
    assert callback_ref["readback_url"].endswith(f"/platform_callback/{callback_run_id}")
    assert callback_ref["resource_version"] == 1
    callback_readback = client.get(
        callback_ref["readback_url"],
        headers=auth_headers,
    )
    assert callback_readback.status_code == 200, callback_readback.text
    callback_data = callback_readback.json()["data"]
    assert callback_data["type"] == "platform_callback"
    assert callback_data["id"] == callback_run_id
    assert callback_data["resource_version"] == 1
    assert callback_data["review_decision_id"] == receipt["decision_id"]
    assert callback_data["resource"]["source_review_decision_id"] == receipt["decision_id"]
    assert callback_data["resource"]["status"] == "pending"

    with SessionLocal() as session:
        callback = session.get(RunRecord, callback_run_id)
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == callback_run_id,
                OutboxEvent.event_type == "external_callback.requested",
            )
        )
        assert callback is not None and event is not None
        assert callback.status == "pending"
        assert callback.payload["source_review_decision_id"] == receipt["decision_id"]
        assert callback.payload["source_review_task_id"] == "hrt_amount_001"
        assert callback.payload["root_trace_id"] == receipt["root_trace_id"]
        assert callback.trace_id == event.payload["trace_id"] == receipt["root_trace_id"]

    callback_list = client.get(
        "/api/v1/output-sinks/platform-callbacks",
        params={"status": "pending"},
        headers=auth_headers,
    )
    assert callback_list.status_code == 200
    listed = callback_list.json()["data"]["items"]
    assert [item["run_id"] for item in listed] == [callback_run_id]
    assert listed[0]["target"] == "crm_reception_order"

    # The decision projection event is handled first; the callback then uses
    # the same normal worker/retry/dead-letter path as manually created runs.
    assert process_once() == 1
    assert process_once() == 1
    with SessionLocal() as session:
        callback = session.get(RunRecord, callback_run_id)
        assert callback is not None
        assert callback.status == "submitted"
        assert callback.payload["business_status"] == "awaiting_completion"
    advanced_readback = client.get(
        callback_ref["readback_url"],
        headers=auth_headers,
    )
    assert advanced_readback.status_code == 200, advanced_readback.text
    advanced_data = advanced_readback.json()["data"]
    assert advanced_data["resource_version"] >= callback_ref["resource_version"]
    assert advanced_data["review_decision_id"] == receipt["decision_id"]
    assert advanced_data["resource"]["status"] == "submitted"


def test_missing_frozen_output_sink_rolls_back_terminal_review(
    client,
    auth_headers,
) -> None:
    _bind_review_output_sink(missing=True)
    rejected = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "目标已失效"},
        headers={
            **auth_headers,
            "Idempotency-Key": "human-review-missing-platform-callback",
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "REVIEW_CALLBACK_BINDING_NOT_FOUND"

    task = client.get(
        "/api/v1/human-review-tasks/hrt_amount_001",
        headers=auth_headers,
    )
    assert task.status_code == 200
    assert task.json()["data"]["status"] == "pending"
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(HumanReviewDecision).where(
                    HumanReviewDecision.review_task_id == "hrt_amount_001"
                )
            )
            is None
        )
        assert (
            session.scalar(select(RunRecord).where(RunRecord.run_type == "external_callback"))
            is None
        )
