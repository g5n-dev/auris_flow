from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import HumanReviewDecisionRequest, parse_payload
from app.schemas.label_closed_loop import HumanReviewDecisionBatchRequest
from app.services.human_review_batch_service import apply_human_review_decision_batch
from app.services.human_review_service import (
    apply_human_review_decision,
    get_human_review_task_for_update,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    require_idempotency,
    save_idempotency_result,
)
from app.services.read_policy_service import can_read_human_review_task
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_data,
    status_counts,
)

router = APIRouter(tags=["human-review"])

CLOSED_LOOP_TARGET_TYPES = frozenset(
    {
        "label_aggregate",
        "label_aggregates",
        "prompt_version_candidate",
        "prompt_version_candidates",
        "taxonomy_suggestion",
        "label_taxonomy_suggestion",
    }
)


def _closed_loop_target_types(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    targets = [
        *(payload.get("target_refs") or []),
        *(payload.get("affected_objects") or []),
    ]
    return {
        str(target.get("type"))
        for target in targets
        if isinstance(target, dict) and str(target.get("type") or "") in CLOSED_LOOP_TARGET_TYPES
    }


@router.post("/human-review-decision-batches")
async def post_human_review_decision_batches(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator", "annotator"),
        action="human_review_decision_batches.create",
    )
    require_idempotency(ctx)
    operation = "human_review_decision_batches.create"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(HumanReviewDecisionBatchRequest, await request.json())
    response = envelope(apply_human_review_decision_batch(session, ctx, body), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response


@router.post("/evidence-packs", status_code=201)
async def post_evidence_packs(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "evidence_packs",
        key_prefix="evidence_pack",
        status="pending",
    )


@router.get("/evidence-packs/{id}")
def get_evidence_packs_by_id(id: str, session: SessionDep, ctx: ContextDep) -> dict[str, Any]:
    data = dict(get_resource(session, ctx, "evidence_packs", id).data)
    candidates = [
        item
        for item in list_resource_data(session, ctx, "label_candidates")
        if item.get("evidence_pack_id") == id
    ]
    data["label_candidates"] = candidates
    return envelope(data, ctx)


@router.get("/human-review-tasks")
def get_human_review_tasks(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    queue: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    items = list_resource_data(
        session, ctx, "human_review_tasks", status=status, limit=int(page["limit"] or 50)
    )
    if queue:
        items = [item for item in items if item.get("queue") == queue]
    items = [item for item in items if can_read_human_review_task(item, ctx)]
    return collection_envelope(items, ctx, meta={"status_counts": status_counts(items)})


@router.post("/human-review-tasks", status_code=201)
async def post_human_review_tasks(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    payload = await request.json()
    closed_loop_types = sorted(_closed_loop_target_types(payload))
    if closed_loop_types:
        raise ApiError(
            "CLOSED_LOOP_SPECIALIZED_TASK_REQUIRED",
            "标签聚合、Taxonomy 与 Prompt 候选任务只能由闭环服务创建",
            409,
            details=[{"target_types": closed_loop_types}],
        )
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "human_review_tasks",
        key_prefix="hrt",
        status="pending",
        reject_existing=True,
    )


@router.get("/human-review-tasks/{id}")
def get_human_review_tasks_by_id(id: str, session: SessionDep, ctx: ContextDep) -> dict[str, Any]:
    task = dict(get_resource(session, ctx, "human_review_tasks", id).data)
    if not can_read_human_review_task(task, ctx):
        raise ApiError("HUMAN_REVIEW_TASK_FORBIDDEN", "当前用户无权读取该人审任务", 403)
    evidence_id = task.get("evidence_pack_id")
    if evidence_id:
        task["evidence_pack"] = get_resource(session, ctx, "evidence_packs", evidence_id).data
    return envelope(task, ctx)


@router.post("/human-review-tasks/{id}/decisions")
async def post_human_review_tasks_by_id_decisions(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator", "annotator"),
        action="human_review_tasks.decide",
    )
    require_idempotency(ctx)
    body_hash = await request_hash(request)
    operation = f"human_review_tasks.decide:{id}"
    body = parse_payload(HumanReviewDecisionRequest, await request.json()).model_dump(
        exclude_none=True
    )
    resource, projection = get_human_review_task_for_update(session, ctx, id)
    if not can_read_human_review_task(dict(resource.data), ctx):
        raise ApiError("HUMAN_REVIEW_TASK_FORBIDDEN", "当前用户无权处理该人审任务", 403)
    if resource.data.get("queue") == "quality_appeal":
        raise ApiError(
            "QUALITY_APPEAL_SPECIALIZED_DECISION_REQUIRED",
            "质检申诉必须通过 /quality-appeals/{id}/decisions 提交专用裁决",
            409,
        )
    if resource.data.get("queue") == "blind_calibration":
        raise ApiError(
            "BLIND_CALIBRATION_SPECIALIZED_SUBMISSION_REQUIRED",
            "盲审校准必须通过 /calibration-assignments/{id}/submissions 提交密封答案",
            409,
        )
    prompt_double_blind_target = any(
        isinstance(target, dict)
        and target.get("type")
        in {
            "prompt_version_candidate",
            "prompt_version_candidates",
        }
        for target in resource.data.get("target_refs") or []
    )
    if resource.data.get("review_mode") == "double-blind" and prompt_double_blind_target:
        raise ApiError(
            "PROMPT_DOUBLE_BLIND_SPECIALIZED_SUBMISSION_REQUIRED",
            "Prompt 双盲审核必须通过 "
            "/prompt-version-candidates/{id}/review-submissions 提交密封结论",
            409,
        )
    closed_loop_double_blind_target = any(
        isinstance(target, dict)
        and target.get("type")
        in {
            "label_aggregate",
            "label_aggregates",
            "taxonomy_suggestion",
            "label_taxonomy_suggestion",
        }
        for target in resource.data.get("target_refs") or []
    )
    if resource.data.get("review_mode") == "double-blind" and closed_loop_double_blind_target:
        raise ApiError(
            "CLOSED_LOOP_DOUBLE_BLIND_REQUIRED",
            "高风险标签聚合与 Taxonomy 必须通过专用双盲提交和仲裁接口处理",
            409,
        )
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    try:
        decision_result = apply_human_review_decision(
            session,
            ctx,
            task=resource,
            task_projection=projection,
            request_body=body,
        )
    except IntegrityError:
        session.rollback()
        replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
        if replay is not None:
            return replay
        raise ApiError(
            "HUMAN_REVIEW_TASK_ALREADY_DECIDED",
            "该人审任务已由其他处理者完成，不能重复落账",
            409,
        ) from None
    response = envelope(
        {
            "id": id,
            **decision_result,
        },
        ctx,
    )
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response
