from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import server_generated_public_id
from app.models import LabelAggregate
from app.schemas.label_closed_loop import HumanReviewDecisionBatchRequest
from app.services.audit_service import record_audit
from app.services.human_review_service import (
    apply_human_review_decision,
    get_human_review_task_for_update,
)
from app.services.outbox_service import enqueue_event


def _aggregate_target(task_data: dict[str, Any]) -> str | None:
    refs = [
        item
        for item in task_data.get("target_refs") or []
        if isinstance(item, dict)
        and str(item.get("type") or "") in {"label_aggregate", "label_aggregates"}
        and isinstance(item.get("id"), str)
    ]
    if len(refs) != 1:
        return None
    return str(refs[0]["id"])


def apply_human_review_decision_batch(
    session: Session,
    ctx: RequestContext,
    body: HumanReviewDecisionBatchRequest,
) -> dict[str, Any]:
    batch_id = server_generated_public_id("hrb", suffix_length=24)
    cohort: tuple[str, str, str] | None = None
    results: list[dict[str, Any]] = []

    for item in body.items:
        try:
            with session.begin_nested():
                task, projection = get_human_review_task_for_update(
                    session, ctx, item.review_task_id
                )
                aggregate_id = _aggregate_target(dict(task.data))
                if aggregate_id is None:
                    results.append(
                        {
                            "review_task_id": item.review_task_id,
                            "status": "skipped",
                            "reason_code": "BATCH_EXPLICIT_AGGREGATE_TARGET_REQUIRED",
                        }
                    )
                    continue
                aggregate = session.scalar(
                    select(LabelAggregate).where(
                        LabelAggregate.aggregate_id == aggregate_id,
                        LabelAggregate.tenant_id == ctx.tenant_id,
                        LabelAggregate.project_id == ctx.project_id,
                    )
                )
                if aggregate is None:
                    raise ApiError(
                        "LABEL_AGGREGATE_PROJECTION_MISSING",
                        "批量审核目标缺少 LabelAggregate 强表",
                        409,
                    )
                if aggregate.risk_level != "low":
                    results.append(
                        {
                            "review_task_id": item.review_task_id,
                            "aggregate_id": aggregate_id,
                            "status": "skipped",
                            "reason_code": "BATCH_ONLY_LOW_RISK_ALLOWED",
                        }
                    )
                    continue
                batch_review = (
                    aggregate.explanation.get("batch_review")
                    if isinstance(aggregate.explanation, dict)
                    else None
                )
                if not isinstance(batch_review, dict) or batch_review.get("eligible") is not True:
                    results.append(
                        {
                            "review_task_id": item.review_task_id,
                            "aggregate_id": aggregate_id,
                            "status": "skipped",
                            "reason_code": "BATCH_SERVER_ELIGIBILITY_REQUIRED",
                            "eligibility_reasons": (
                                batch_review.get("reason_codes", [])
                                if isinstance(batch_review, dict)
                                else ["ELIGIBILITY_NOT_MATERIALIZED"]
                            ),
                        }
                    )
                    continue
                item_cohort = (
                    aggregate.label_id,
                    aggregate.risk_level,
                    aggregate.policy_version_id,
                )
                if cohort is None:
                    cohort = item_cohort
                elif item_cohort != cohort:
                    results.append(
                        {
                            "review_task_id": item.review_task_id,
                            "aggregate_id": aggregate_id,
                            "status": "skipped",
                            "reason_code": "BATCH_COHORT_MISMATCH",
                        }
                    )
                    continue
                decision = apply_human_review_decision(
                    session,
                    ctx,
                    task=task,
                    task_projection=projection,
                    request_body={
                        "decision": item.decision,
                        "note": item.note,
                        "reason": item.note,
                        "changes": [],
                    },
                )
                results.append(
                    {
                        "review_task_id": item.review_task_id,
                        "aggregate_id": aggregate_id,
                        "status": "success",
                        "decision_id": decision["decision_id"],
                        "decision": decision["decision"],
                    }
                )
        except (ApiError, IntegrityError) as exc:
            code = exc.code if isinstance(exc, ApiError) else "CONCURRENT_TERMINAL_DECISION"
            results.append(
                {
                    "review_task_id": item.review_task_id,
                    "status": "failed",
                    "reason_code": code,
                }
            )

    counts = {
        state: sum(result["status"] == state for result in results)
        for state in ("success", "skipped", "failed")
    }
    status = (
        "completed"
        if counts["success"] == len(results)
        else "failed"
        if counts["failed"] == len(results)
        else "partial"
    )
    payload = {
        "batch_id": batch_id,
        "status": status,
        "cohort": (
            {
                "label_id": cohort[0],
                "risk_level": cohort[1],
                "policy_version_id": cohort[2],
            }
            if cohort
            else None
        ),
        "counts": counts,
        "results": results,
        "trace_id": ctx.trace_id,
    }
    record_audit(
        session,
        ctx,
        action="human_review_decision_batch.completed",
        object_type="human_review_decision_batch",
        object_id=batch_id,
        after=payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review_decision_batch.completed",
        aggregate_type="human_review_decision_batch",
        aggregate_id=batch_id,
        payload=payload,
    )
    return payload
