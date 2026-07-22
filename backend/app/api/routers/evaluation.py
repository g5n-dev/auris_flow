from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import PromptVersionCandidate
from app.schemas import (
    EvalDatasetVersionCreateRequest,
    EvalDatasetVersionLockRequest,
    EvalFeedbackTaskRequest,
    EvalRunRequest,
    parse_payload,
)
from app.schemas.public_runs import (
    PublicPromptVersionCandidate,
    PublicRunCollectionEnvelope,
    PublicRunEnvelope,
)
from app.services.eval_binding_service import validate_labeling_eval_binding
from app.services.eval_dataset_service import (
    create_eval_dataset_version,
    eval_dataset_data,
    get_eval_dataset_version,
    lock_eval_dataset_version,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.public_run_projection_service import public_run_projection
from app.services.resource_service import (
    list_resource_page,
)
from app.services.run_service import create_run, get_run, list_run_page

router = APIRouter(tags=["evaluation"])
PROMPT_CANDIDATE_PUBLIC_FIELDS = frozenset(
    {
        "id",
        "candidate_id",
        "prompt_version_id",
        "prompt_asset_id",
        "parent_version_id",
        "label_version_id",
        "model_version",
        "schema_version",
        "status",
        "template",
        "output_schema",
        "generation_params",
        "structured_diff",
        "source_badcase_refs",
        "content_sha256",
        "source_run_id",
        "source_run_type",
        "agent_run_id",
        "eval_run_id",
        "feedback_task_id",
        "base_prompt_version",
        "target",
        "badcase_refs",
        "result_ref",
        "metrics",
        "change_set_id",
        "review_task_id",
        "review_gate",
        "write_policy",
        "affected_objects",
        "summary",
        "review_status",
        "review_submission_ids",
        "received_reviews",
        "review_decision_id",
        "review_resolution_source",
        "adjudication_id",
        "requested_field_diff",
        "reviewed_at",
        "source_trace_id",
        "action_trace_id",
        "revision_of_candidate_id",
        "source_review_decision_id",
        "child_prompt_version_id",
        "child_review_task_id",
        "trace_id",
    }
)


def _public_prompt_candidate(candidate: PromptVersionCandidate) -> dict:
    return public_run_projection(
        {
            **candidate.payload,
            "id": candidate.candidate_id,
            "candidate_id": candidate.candidate_id,
            "status": candidate.status,
            "trace_id": candidate.trace_id,
        },
        allowed_fields=PROMPT_CANDIDATE_PUBLIC_FIELDS,
        forbidden_fields={"provider"},
        field_name="prompt_version_candidate",
    )


@router.get("/eval-datasets")
def get_eval_datasets(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "eval_datasets", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/eval-datasets", status_code=201)
async def post_eval_datasets(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="eval_datasets.create",
    )
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation="eval_datasets.create",
        body_hash=body_hash,
    )
    if replay is not None:
        return replay
    body = parse_payload(
        EvalDatasetVersionCreateRequest,
        await request.json(),
    )
    response = envelope(create_eval_dataset_version(session, ctx, body), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation="eval_datasets.create",
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/eval-datasets/{id}")
def get_eval_datasets_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(eval_dataset_data(get_eval_dataset_version(session, ctx, id)), ctx)


@router.post("/eval-datasets/{id}/lock")
async def post_eval_datasets_by_id_lock(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="eval_datasets.lock",
    )
    body_hash = await request_hash(request)
    operation = f"eval_datasets.lock:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(EvalDatasetVersionLockRequest, await request.json())
    response = envelope(
        lock_eval_dataset_version(
            session,
            ctx,
            id,
            expected_resource_version=body.expected_resource_version,
        ),
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


@router.get("/eval-runs")
def get_eval_runs(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    run_page = list_run_page(session, ctx, page, run_type="eval_run")
    return collection_envelope(
        run_page.items,
        ctx,
        total=run_page.total,
        limit=run_page.limit,
        next_cursor=run_page.next_cursor,
    )


@router.post("/eval-runs", status_code=202)
async def post_eval_runs(request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(EvalRunRequest, await request.json()).model_dump(exclude_none=True)
    body = validate_labeling_eval_binding(session, ctx, body)
    return await create_run(
        session,
        ctx,
        request,
        run_type="eval_run",
        event_type="eval_run.requested",
        payload=body,
        status="queued" if body.get("capability") == "labeling" else "pending",
    )


@router.get("/eval-runs/{id}")
def get_eval_runs_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_run(session, ctx, id), ctx)


@router.get(
    "/prompt-version-candidates",
    response_model=PublicRunCollectionEnvelope[PublicPromptVersionCandidate],
    response_model_exclude_unset=True,
)
def get_prompt_version_candidates(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
):
    cursor = int(page.get("cursor") or 0)
    limit = int(page.get("limit") or 50)
    query = session.query(PromptVersionCandidate).filter(
        PromptVersionCandidate.tenant_id == ctx.tenant_id,
        PromptVersionCandidate.project_id == ctx.project_id,
    )
    if status:
        query = query.filter(PromptVersionCandidate.status == status)
    total = query.count()
    items = (
        query.order_by(PromptVersionCandidate.updated_at.desc()).limit(limit).offset(cursor).all()
    )
    return collection_envelope(
        [_public_prompt_candidate(candidate) for candidate in items],
        ctx,
        total=total,
        limit=limit,
        next_cursor=str(cursor + limit) if cursor + limit < total else None,
    )


@router.get(
    "/prompt-version-candidates/{id}",
    response_model=PublicRunEnvelope[PublicPromptVersionCandidate],
    response_model_exclude_unset=True,
)
def get_prompt_version_candidates_by_id(id: str, session: SessionDep, ctx: ContextDep):
    candidate = session.get(PromptVersionCandidate, id)
    if (
        not candidate
        or candidate.tenant_id != ctx.tenant_id
        or candidate.project_id != ctx.project_id
    ):
        raise ApiError("NOT_FOUND", f"Prompt 候选版本不存在：{id}", 404)
    return envelope(_public_prompt_candidate(candidate), ctx)


@router.post("/eval-runs/{id}/feedback-tasks", status_code=202)
async def post_eval_runs_by_id_feedback_tasks(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    raw_body = await request.json()
    target_run = get_run(session, ctx, id)
    if target_run.get("run_type") != "eval_run":
        raise ApiError(
            "INVALID_EVAL_RUN",
            "feedback task 只能挂在 eval_run 上",
            409,
            details=[{"field": "eval_run_id", "value": id}],
        )
    if not isinstance(raw_body.get("badcase_refs"), list) or not raw_body.get("badcase_refs"):
        raise ApiError(
            "BADCASE_REFS_REQUIRED",
            "评测反馈必须绑定至少一个 badcase 或证据样本",
            400,
            details=[{"field": "badcase_refs", "reason": "required_non_empty_list"}],
        )
    body = parse_payload(EvalFeedbackTaskRequest, raw_body).model_dump(exclude_none=True)
    badcase_refs = body["badcase_refs"]
    feedback_digest = hashlib.sha1(
        f"{id}|{body.get('target')}|{','.join(badcase_refs)}".encode()
    ).hexdigest()[:10]
    feedback_task_id = body.get("feedback_task_id") or f"feedback_{id}_{feedback_digest}"
    return await create_run(
        session,
        ctx,
        request,
        run_type="eval_feedback",
        event_type="agent_run.requested",
        payload={
            **body,
            "feedback_task_id": feedback_task_id,
            "eval_run_id": id,
            "eval_run_trace_id": target_run.get("trace_id"),
            "affected_objects": [
                {"type": "eval_run", "id": id},
                {"type": "feedback_task", "id": feedback_task_id},
                *[{"type": "badcase", "id": ref} for ref in badcase_refs],
            ],
        },
        status="pending",
    )
