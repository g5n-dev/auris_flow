from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import ContextDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import LabelAggregationPolicyVersion, LabelTaxonomySuggestion
from app.schemas.common import parse_payload
from app.schemas.label_closed_loop import (
    ClosedLoopReviewAdjudicationRequest,
    ClosedLoopReviewSubmissionRequest,
    LabelAggregationPolicyCreateRequest,
    LabelAggregationRunCreateRequest,
    LabelCalibrationVersionCreateRequest,
    LabelExtractionRunCreateRequest,
    LabelObservationCreateRequest,
)
from app.schemas.public_runs import LabelExtractionRunPublic, PublicRunEnvelope
from app.services.closed_loop_review_service import (
    adjudicate_closed_loop_review,
    submit_closed_loop_review,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.label_closed_loop_service import (
    create_aggregation_policy,
    create_aggregation_run,
    create_label_calibration_version,
    create_label_extraction_projection,
    create_label_observation,
    get_aggregation_policy,
    get_aggregation_run,
    get_label_aggregate,
    get_label_calibration_version,
    get_label_extraction_run,
    get_label_observation,
    list_label_aggregates,
    list_label_calibration_versions,
    list_label_observations,
    policy_data,
    taxonomy_suggestion_data,
)
from app.services.run_service import create_run

router = APIRouter(tags=["label-closed-loop"])


async def _idempotent_body(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    *,
    operation: str,
    model: type[Any],
) -> tuple[Any, str, dict[str, Any] | None]:
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return None, body_hash, replay
    body = parse_payload(model, await request.json())
    return body, body_hash, None


def _commit_idempotent(
    session: SessionDep,
    ctx: ContextDep,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    response: dict[str, Any],
) -> dict[str, Any]:
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=status_code,
        response_json=response,
    )
    session.commit()
    return response


@router.post(
    "/label-extraction-runs",
    status_code=202,
    response_model=PublicRunEnvelope[LabelExtractionRunPublic],
    response_model_exclude_unset=True,
)
async def post_label_extraction_runs(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    body = parse_payload(LabelExtractionRunCreateRequest, await request.json())
    payload = body.model_dump(mode="json", exclude_none=True)
    payload.update(
        {
            "run_id": body.extraction_run_id,
            "extraction_run_id": body.extraction_run_id,
            "locked_versions": {
                "label_version_id": body.label_version_id,
                "prompt_version_id": body.prompt_version_id,
                "model_version": body.model_version,
                "schema_version": body.schema_version,
                "aggregation_policy_version_id": body.aggregation_policy_version_id,
            },
            "next_actions": [
                {"key": "view_trace", "label": "查看 Trace"},
                {"key": "wait_for_materialization", "label": "等待模型回执与物化"},
            ],
        }
    )
    response = await create_run(
        session,
        ctx,
        request,
        run_type="label_extraction",
        event_type="agent_run.requested",
        payload=payload,
        status="queued",
        prepare_record=lambda record: create_label_extraction_projection(
            session, ctx, body, record
        ),
    )
    return {
        **response,
        "data": get_label_extraction_run(session, ctx, body.extraction_run_id),
    }


@router.get(
    "/label-extraction-runs/{extraction_run_id}",
    response_model=PublicRunEnvelope[LabelExtractionRunPublic],
    response_model_exclude_unset=True,
)
def get_label_extraction_run_by_id(
    extraction_run_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_label_extraction_run(session, ctx, extraction_run_id), ctx)


@router.post("/label-observations", status_code=201)
async def post_label_observations(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    if not {"system", "external_completion_client"}.intersection(ctx.roles):
        raise ApiError(
            "LABEL_OBSERVATION_TRUSTED_WRITER_REQUIRED",
            "LabelObservation 只能由系统或已认证的可信模型适配器物化",
            403,
        )
    operation = "label_observations.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=LabelObservationCreateRequest,
    )
    if replay is not None:
        return replay
    response = envelope(create_label_observation(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/label-observations")
def get_label_observations(
    session: SessionDep,
    ctx: ContextDep,
    subject_scope: str | None = None,
    subject_key: str | None = None,
    label_version_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_label_observations(
        session,
        ctx,
        subject_scope=subject_scope,
        subject_key=subject_key,
        label_version_id=label_version_id,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/label-observations/{observation_id}")
def get_label_observation_by_id(
    observation_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_label_observation(session, ctx, observation_id), ctx)


@router.post("/label-calibration-versions", status_code=201)
async def post_label_calibration_versions(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        "label_calibration_versions.create",
    )
    operation = "label_calibration_versions.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=LabelCalibrationVersionCreateRequest,
    )
    if replay is not None:
        return replay
    if body.status == "published" and "system" in ctx.roles:
        raise ApiError(
            "AGENT_CALIBRATION_PUBLISH_FORBIDDEN",
            "系统身份不能批准发布标签校准器",
            403,
        )
    response = envelope(create_label_calibration_version(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/label-calibration-versions")
def get_label_calibration_versions(
    session: SessionDep,
    ctx: ContextDep,
    label_version_id: str | None = None,
    source_family: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_label_calibration_versions(
        session,
        ctx,
        label_version_id=label_version_id,
        source_family=source_family,
        status=status,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/label-calibration-versions/{calibration_version_id}")
def get_label_calibration_version_by_id(
    calibration_version_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_label_calibration_version(session, ctx, calibration_version_id), ctx)


@router.post("/label-aggregation-policies", status_code=201)
async def post_label_aggregation_policies(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        "label_aggregation_policies.create",
    )
    operation = "label_aggregation_policies.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=LabelAggregationPolicyCreateRequest,
    )
    if replay is not None:
        return replay
    if body.status == "active" and "system" in ctx.roles:
        raise ApiError(
            "AGENT_AGGREGATION_POLICY_ACTIVATION_FORBIDDEN",
            "系统身份不能批准聚合策略激活",
            403,
        )
    response = envelope(create_aggregation_policy(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/label-aggregation-policies")
def get_label_aggregation_policies(
    session: SessionDep,
    ctx: ContextDep,
    label_version_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    statement = select(LabelAggregationPolicyVersion).where(
        LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
        LabelAggregationPolicyVersion.project_id == ctx.project_id,
    )
    if label_version_id:
        statement = statement.where(
            LabelAggregationPolicyVersion.label_version_id == label_version_id
        )
    if status:
        statement = statement.where(LabelAggregationPolicyVersion.status == status)
    records = list(
        session.scalars(
            statement.order_by(LabelAggregationPolicyVersion.created_at.desc()).limit(limit)
        )
    )
    return collection_envelope(
        [policy_data(record) for record in records], ctx, total=len(records), limit=limit
    )


@router.get("/label-aggregation-policies/{policy_version_id}")
def get_label_aggregation_policy_by_id(
    policy_version_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(policy_data(get_aggregation_policy(session, ctx, policy_version_id)), ctx)


@router.post("/label-aggregation-runs", status_code=202)
async def post_label_aggregation_runs(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        "label_aggregation_runs.create",
    )
    operation = "label_aggregation_runs.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=LabelAggregationRunCreateRequest,
    )
    if replay is not None:
        return replay
    response = envelope(create_aggregation_run(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response=response,
    )


@router.get("/label-aggregation-runs/{aggregation_run_id}")
def get_label_aggregation_run_by_id(
    aggregation_run_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_aggregation_run(session, ctx, aggregation_run_id), ctx)


@router.get("/label-aggregates")
def get_label_aggregates(
    session: SessionDep,
    ctx: ContextDep,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_label_aggregates(session, ctx, status=status, limit=limit)
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/label-aggregates/{aggregate_id}")
def get_label_aggregate_by_id(
    aggregate_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_label_aggregate(session, ctx, aggregate_id), ctx)


@router.get("/label-taxonomy-suggestions")
def get_label_taxonomy_suggestions(
    session: SessionDep,
    ctx: ContextDep,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    statement = select(LabelTaxonomySuggestion).where(
        LabelTaxonomySuggestion.tenant_id == ctx.tenant_id,
        LabelTaxonomySuggestion.project_id == ctx.project_id,
    )
    if status:
        statement = statement.where(LabelTaxonomySuggestion.status == status)
    records = list(
        session.scalars(statement.order_by(LabelTaxonomySuggestion.created_at.desc()).limit(limit))
    )
    return collection_envelope(
        [taxonomy_suggestion_data(record) for record in records],
        ctx,
        total=len(records),
        limit=limit,
    )


async def _post_closed_loop_review_submission(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    *,
    kind: Literal["aggregate", "taxonomy"],
    target_id: str,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator", "annotator"),
        "closed_loop_reviews.submit",
    )
    if "system" in ctx.roles:
        raise ApiError("FORBIDDEN", "系统账号不能代替人工提交闭环双盲结论", 403)
    operation = f"closed_loop_reviews.submit:{kind}:{target_id}"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ClosedLoopReviewSubmissionRequest,
    )
    if replay is not None:
        return replay
    response = envelope(
        submit_closed_loop_review(
            session,
            ctx,
            kind=kind,
            target_id=target_id,
            body=body,
        ),
        ctx,
    )
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


async def _post_closed_loop_review_adjudication(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    *,
    kind: Literal["aggregate", "taxonomy"],
    target_id: str,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator"),
        "closed_loop_reviews.adjudicate",
    )
    if "system" in ctx.roles or not {
        "project_admin",
        "review_arbitrator",
    }.intersection(ctx.roles):
        raise ApiError("FORBIDDEN", "只有人工仲裁角色可以处理闭环审核分歧", 403)
    operation = f"closed_loop_reviews.adjudicate:{kind}:{target_id}"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ClosedLoopReviewAdjudicationRequest,
    )
    if replay is not None:
        return replay
    response = envelope(
        adjudicate_closed_loop_review(
            session,
            ctx,
            kind=kind,
            target_id=target_id,
            body=body,
        ),
        ctx,
    )
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.post("/label-aggregates/{aggregate_id}/review-submissions", status_code=201)
async def post_label_aggregate_review_submission(
    aggregate_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _post_closed_loop_review_submission(
        request,
        session,
        ctx,
        kind="aggregate",
        target_id=aggregate_id,
    )


@router.post("/label-aggregates/{aggregate_id}/adjudications", status_code=201)
async def post_label_aggregate_review_adjudication(
    aggregate_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _post_closed_loop_review_adjudication(
        request,
        session,
        ctx,
        kind="aggregate",
        target_id=aggregate_id,
    )


@router.post(
    "/label-taxonomy-suggestions/{suggestion_id}/review-submissions",
    status_code=201,
)
async def post_label_taxonomy_review_submission(
    suggestion_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _post_closed_loop_review_submission(
        request,
        session,
        ctx,
        kind="taxonomy",
        target_id=suggestion_id,
    )


@router.post(
    "/label-taxonomy-suggestions/{suggestion_id}/adjudications",
    status_code=201,
)
async def post_label_taxonomy_review_adjudication(
    suggestion_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return await _post_closed_loop_review_adjudication(
        request,
        session,
        ctx,
        kind="taxonomy",
        target_id=suggestion_id,
    )
