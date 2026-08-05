from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    Badcase,
    FeedbackExample,
    HumanReviewDecision,
    JsonResource,
    LabelConflict,
    LabelFact,
    RunRecord,
)
from app.services.audio_evidence_review_service import assemble_scoped_evidence_pack
from app.services.read_policy_service import can_read_human_review_task

JSON_COLLECTION_BY_AFFECTED_TYPE = {
    "human_review_task": "human_review_tasks",
    "label_candidate": "label_candidates",
    "label_aggregate": "label_aggregates",
    "prompt_version_candidate": "prompt_version_candidates",
    "taxonomy_suggestion": "label_taxonomy_suggestions",
    "event_link": "event_links",
    "conversation_boundary": "conversation_boundaries",
    "voiceprint_sample": "voiceprint_samples",
    "work_item": "work_items",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _scoped_decision(
    session: Session,
    ctx: RequestContext,
    decision_id: str,
) -> HumanReviewDecision:
    decision = session.scalar(
        select(HumanReviewDecision).where(
            HumanReviewDecision.decision_id == decision_id,
            HumanReviewDecision.tenant_id == ctx.tenant_id,
            HumanReviewDecision.project_id == ctx.project_id,
        )
    )
    if decision is None:
        raise ApiError(
            "HUMAN_REVIEW_DECISION_NOT_FOUND",
            "人审决定不存在或不属于当前租户项目",
            404,
        )
    task = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == "human_review_tasks",
            JsonResource.resource_key == decision.review_task_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    if task is None or not can_read_human_review_task(dict(task.data), ctx):
        raise ApiError(
            "HUMAN_REVIEW_DECISION_FORBIDDEN",
            "当前用户无权读取该人审决定",
            403,
        )
    return decision


def _json_resource(
    session: Session,
    ctx: RequestContext,
    collection: str,
    object_id: str,
) -> tuple[dict[str, Any], int | None]:
    resource = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == collection,
            JsonResource.resource_key == object_id,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    if resource is None:
        raise ApiError(
            "HUMAN_REVIEW_AFFECTED_OBJECT_READBACK_MISSING",
            "人审回执中的受影响对象无法从权威存储回读",
            409,
            details=[{"type": collection, "id": object_id}],
        )
    data = dict(resource.data)
    raw_version = data.get("resource_version")
    resource_version = (
        raw_version
        if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version > 0
        else None
    )
    return data, resource_version


def _decision_resource(decision: HumanReviewDecision) -> dict[str, Any]:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    return {
        "decision_id": decision.decision_id,
        "review_task_id": decision.review_task_id,
        "decision": payload.get("decision"),
        "status": decision.status,
        "root_trace_id": decision.trace_id,
        "current_trace_id": payload.get("action_trace_id"),
        "affected_objects": payload.get("affected_objects") or [],
        "created_at": _iso(decision.created_at),
        "updated_at": _iso(decision.updated_at),
    }


def _resolve_affected_resource(
    session: Session,
    ctx: RequestContext,
    *,
    object_type: str,
    object_id: str,
) -> tuple[dict[str, Any], int | None, str | None]:
    collection = JSON_COLLECTION_BY_AFFECTED_TYPE.get(object_type)
    if collection is not None:
        data, version = _json_resource(session, ctx, collection, object_id)
        binding = (
            data.get("decision_id")
            if object_type == "human_review_task"
            else data.get("review_decision_id")
        )
        return data, version, str(binding or "") or None

    if object_type == "evidence_pack":
        data = assemble_scoped_evidence_pack(session, ctx, object_id)
        version = data.get("resource_version")
        return (
            data,
            version if isinstance(version, int) and version > 0 else None,
            str(data.get("review_decision_id") or "") or None,
        )

    if object_type == "human_review_decision":
        decision = _scoped_decision(session, ctx, object_id)
        return _decision_resource(decision), None, decision.decision_id

    if object_type == "label_conflict":
        conflict = session.scalar(
            select(LabelConflict).where(
                LabelConflict.conflict_id == object_id,
                LabelConflict.tenant_id == ctx.tenant_id,
                LabelConflict.project_id == ctx.project_id,
            )
        )
        if conflict is not None:
            data = {
                "conflict_id": conflict.conflict_id,
                "status": conflict.status,
                "root_trace_id": conflict.trace_id,
                **dict(conflict.payload),
            }
            return data, None, str(data.get("review_decision_id") or "") or None

    if object_type == "feedback_example":
        feedback = session.scalar(
            select(FeedbackExample).where(
                FeedbackExample.feedback_example_id == object_id,
                FeedbackExample.tenant_id == ctx.tenant_id,
                FeedbackExample.project_id == ctx.project_id,
            )
        )
        if feedback is not None:
            return (
                {
                    "feedback_example_id": feedback.feedback_example_id,
                    "review_decision_id": feedback.review_decision_id,
                    "review_task_id": feedback.review_task_id,
                    "target_type": feedback.target_type,
                    "target_id": feedback.target_id,
                    "feedback_type": feedback.feedback_type,
                    "reason_code": feedback.reason_code,
                    "field_diff": feedback.field_diff,
                    "gold_status": feedback.gold_status,
                    "root_trace_id": feedback.trace_id,
                    "created_at": _iso(feedback.created_at),
                    "updated_at": _iso(feedback.updated_at),
                },
                None,
                feedback.review_decision_id,
            )

    if object_type == "label_fact":
        fact = session.scalar(
            select(LabelFact).where(
                LabelFact.fact_id == object_id,
                LabelFact.tenant_id == ctx.tenant_id,
                LabelFact.project_id == ctx.project_id,
            )
        )
        if fact is not None:
            version = (
                fact.revision if isinstance(fact.revision, int) and fact.revision > 0 else None
            )
            binding = fact.human_review_decision_id or fact.review_decision_id
            return (
                {
                    "fact_id": fact.fact_id,
                    "aggregate_id": fact.aggregate_id,
                    "fact_namespace": fact.fact_namespace,
                    "revision": fact.revision,
                    "subject_scope": fact.subject_scope,
                    "subject_key": fact.subject_key,
                    "label_id": fact.label_id,
                    "value_type": fact.value_type,
                    "value": fact.value_json,
                    "authority": fact.authority,
                    "status": fact.status,
                    "human_review_decision_id": fact.human_review_decision_id,
                    "review_decision_id": fact.review_decision_id,
                    "content_sha256": fact.content_sha256,
                    "root_trace_id": fact.root_trace_id or fact.trace_id,
                    "created_at": _iso(fact.created_at),
                    "updated_at": _iso(fact.updated_at),
                },
                version,
                binding,
            )

    if object_type == "badcase":
        badcase = session.scalar(
            select(Badcase).where(
                Badcase.badcase_id == object_id,
                Badcase.tenant_id == ctx.tenant_id,
                Badcase.project_id == ctx.project_id,
            )
        )
        if badcase is not None:
            binding = badcase.payload.get("review_decision_id")
            return (
                {
                    "badcase_id": badcase.badcase_id,
                    "status": badcase.status,
                    "resource_version": badcase.resource_version,
                    "review_decision_id": binding,
                    "capability": badcase.capability,
                    "failure_reason": badcase.payload.get("failure_reason") or badcase.root_cause,
                    "root_trace_id": badcase.root_trace_id,
                    "current_trace_id": badcase.current_trace_id,
                    "created_at": _iso(badcase.created_at),
                    "updated_at": _iso(badcase.updated_at),
                },
                badcase.resource_version,
                str(binding or "") or None,
            )

    if object_type == "platform_callback":
        run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == object_id,
                RunRecord.tenant_id == ctx.tenant_id,
                RunRecord.project_id == ctx.project_id,
                RunRecord.run_type == "external_callback",
            )
        )
        if run is not None:
            binding = run.payload.get("source_review_decision_id")
            return (
                {
                    "run_id": run.run_id,
                    "status": run.status,
                    "resource_version": run.status_version,
                    "source_review_task_id": run.payload.get("source_review_task_id"),
                    "source_review_decision_id": binding,
                    "output_sink_id": run.payload.get("output_sink_id"),
                    "target": run.payload.get("target"),
                    "business_status": run.payload.get("business_status"),
                    "retry_count": run.payload.get("retry_count", 0),
                    "root_trace_id": run.payload.get("root_trace_id") or run.trace_id,
                    "current_trace_id": run.trace_id,
                    "created_at": _iso(run.created_at),
                    "updated_at": _iso(run.updated_at),
                },
                run.status_version,
                str(binding or "") or None,
            )

    raise ApiError(
        "HUMAN_REVIEW_AFFECTED_OBJECT_READBACK_MISSING",
        "人审回执中的受影响对象无法从权威存储回读",
        409,
        details=[{"type": object_type, "id": object_id}],
    )


def enrich_human_review_affected_objects(
    session: Session,
    ctx: RequestContext,
    *,
    decision_id: str,
    affected_objects: list[dict[str, str]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in affected_objects:
        object_type = str(item.get("type") or "").strip()
        object_id = str(item.get("id") or "").strip()
        key = (object_type, object_id)
        if not object_type or not object_id or key in seen:
            raise ApiError(
                "HUMAN_REVIEW_AFFECTED_OBJECT_INVALID",
                "人审决定生成了空标识或重复的受影响对象",
                409,
                details=[{"type": object_type or None, "id": object_id or None}],
            )
        seen.add(key)
        _resource, resource_version, binding = _resolve_affected_resource(
            session,
            ctx,
            object_type=object_type,
            object_id=object_id,
        )
        if binding != decision_id:
            raise ApiError(
                "HUMAN_REVIEW_AFFECTED_OBJECT_BINDING_MISMATCH",
                "受影响对象未绑定当前人审决定，事务已阻断",
                409,
                details=[
                    {
                        "type": object_type,
                        "id": object_id,
                        "expected_review_decision_id": decision_id,
                        "actual_review_decision_id": binding,
                    }
                ],
            )
        ref: dict[str, Any] = {
            "type": object_type,
            "id": object_id,
            "readback_url": (
                f"/api/v1/human-review-decisions/{quote(decision_id, safe='')}"
                f"/affected-objects/{quote(object_type, safe='')}/{quote(object_id, safe='')}"
            ),
        }
        if resource_version is not None:
            ref["resource_version"] = resource_version
        enriched.append(ref)
    return enriched


def get_human_review_decision_readback(
    session: Session,
    ctx: RequestContext,
    decision_id: str,
) -> dict[str, Any]:
    return _decision_resource(_scoped_decision(session, ctx, decision_id))


def get_human_review_affected_object_readback(
    session: Session,
    ctx: RequestContext,
    *,
    decision_id: str,
    object_type: str,
    object_id: str,
) -> dict[str, Any]:
    decision = _scoped_decision(session, ctx, decision_id)
    affected_objects = (
        decision.payload.get("affected_objects") if isinstance(decision.payload, dict) else None
    )
    receipt_ref = next(
        (
            item
            for item in affected_objects or []
            if isinstance(item, dict)
            and item.get("type") == object_type
            and item.get("id") == object_id
        ),
        None,
    )
    if receipt_ref is None:
        raise ApiError(
            "HUMAN_REVIEW_AFFECTED_OBJECT_NOT_FOUND",
            "该对象不属于当前人审决定的受影响对象清单",
            404,
        )
    resource, resource_version, binding = _resolve_affected_resource(
        session,
        ctx,
        object_type=object_type,
        object_id=object_id,
    )
    if binding != decision_id:
        raise ApiError(
            "HUMAN_REVIEW_AFFECTED_OBJECT_READBACK_MISMATCH",
            "受影响对象当前状态与人审决定绑定不一致",
            409,
        )
    result: dict[str, Any] = {
        "type": object_type,
        "id": object_id,
        "review_decision_id": decision_id,
        "root_trace_id": decision.trace_id,
        "resource": resource,
    }
    if resource_version is not None:
        result["resource_version"] = resource_version
    return result
