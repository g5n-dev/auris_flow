from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import IntegrityError, OperationalError

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import (
    CalibrationAdjudicationClaimRequest,
    CalibrationAdjudicationRequest,
    CalibrationGoldReleaseRequest,
    CalibrationRoundCreateRequest,
    CalibrationSubmissionRequest,
    parse_payload,
)
from app.services.calibration_service import (
    adjudicate_calibration_item,
    claim_calibration_item,
    create_calibration_round,
    get_calibration_round_detail,
    get_gold_set_version,
    list_calibration_conflicts,
    list_calibration_rounds,
    list_gold_set_versions,
    list_my_calibration_assignments,
    release_calibration_gold,
    submit_calibration_assignment,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)

router = APIRouter(tags=["calibrations"])

ROUND_MANAGE_ROLES = ("project_admin", "review_arbitrator")
REVIEWER_ROLES = ("project_admin", "review_arbitrator", "annotator")
GOLD_READ_ROLES = ("project_admin", "system")


def _is_retryable_database_conflict(exc: OperationalError) -> bool:
    original = exc.orig
    error_code = getattr(original, "errno", None)
    if error_code in {1205, 1213}:
        return True
    args = getattr(original, "args", ())
    return bool(args and args[0] in {1205, 1213, "1205", "1213"})


@router.post("/calibration-rounds", status_code=201)
async def post_calibration_rounds(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ROUND_MANAGE_ROLES, action="calibration_rounds.create")
    body = parse_payload(CalibrationRoundCreateRequest, await request.json())
    body_hash = await request_hash(request)
    operation = "calibration_rounds.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    try:
        data = create_calibration_round(session, ctx, body)
        response = envelope(data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=201,
            response_json=response,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "CALIBRATION_ROUND_CREATE_CONFLICT",
            "校准轮次创建与其他请求冲突",
            409,
        ) from None
    return response


@router.get("/gold-set-versions")
def get_gold_set_versions(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    gold_set_key: str | None = Query(default=None, min_length=1, max_length=128),
) -> dict[str, Any]:
    require_any_role(ctx, GOLD_READ_ROLES, action="gold_set_versions.list")
    limit = int(page["limit"] or 50)
    items = list_gold_set_versions(
        session,
        ctx,
        gold_set_key=gold_set_key,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/gold-set-versions/{gold_set_version_id}")
def get_gold_set_versions_by_id(
    gold_set_version_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, GOLD_READ_ROLES, action="gold_set_versions.read")
    return envelope(get_gold_set_version(session, ctx, gold_set_version_id), ctx)


@router.get("/calibration-rounds")
def get_calibration_rounds(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
) -> dict[str, Any]:
    limit = int(page["limit"] or 50)
    items = list_calibration_rounds(session, ctx, status=status, limit=limit)
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/calibration-rounds/{round_id}")
def get_calibration_rounds_by_id(
    round_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return envelope(get_calibration_round_detail(session, ctx, round_id), ctx)


@router.get("/calibration-assignments")
def get_calibration_assignments(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    round_id: str = Query(min_length=1, max_length=128),
    mine: bool = Query(default=True),
) -> dict[str, Any]:
    require_any_role(ctx, REVIEWER_ROLES, action="calibration_assignments.list_mine")
    limit = int(page["limit"] or 50)
    items = list_my_calibration_assignments(
        session,
        ctx,
        round_id=round_id,
        mine=mine,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.post("/calibration-assignments/{assignment_id}/submissions", status_code=201)
async def post_calibration_assignment_submissions(
    assignment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, REVIEWER_ROLES, action="calibration_assignments.submit")
    body = parse_payload(CalibrationSubmissionRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"calibration_assignments.submit:{assignment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    try:
        response = envelope(
            submit_calibration_assignment(session, ctx, assignment_id, body),
            ctx,
        )
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=201,
            response_json=response,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "CALIBRATION_ASSIGNMENT_ALREADY_SUBMITTED",
            "该 reviewer 的提交已由其他请求密封",
            409,
        ) from None
    return response


@router.get("/calibration-rounds/{round_id}/conflicts")
def get_calibration_round_conflicts(
    round_id: str,
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
) -> dict[str, Any]:
    require_any_role(ctx, ROUND_MANAGE_ROLES, action="calibration_rounds.list_conflicts")
    limit = int(page["limit"] or 50)
    items = list_calibration_conflicts(session, ctx, round_id, limit=limit)
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.post("/calibration-items/{item_id}/adjudication-claims")
async def post_calibration_item_adjudication_claims(
    item_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ROUND_MANAGE_ROLES, action="calibration_items.claim")
    body = parse_payload(CalibrationAdjudicationClaimRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"calibration_items.claim:{item_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    response = envelope(claim_calibration_item(session, ctx, item_id, body), ctx)
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


@router.post("/calibration-items/{item_id}/adjudications", status_code=201)
async def post_calibration_item_adjudications(
    item_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ROUND_MANAGE_ROLES, action="calibration_items.adjudicate")
    body = parse_payload(CalibrationAdjudicationRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"calibration_items.adjudicate:{item_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    try:
        response = envelope(adjudicate_calibration_item(session, ctx, item_id, body), ctx)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=201,
            response_json=response,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "CALIBRATION_ITEM_ALREADY_RESOLVED",
            "该争议样本已由其他请求生成不可变裁决",
            409,
        ) from None
    return response


@router.post("/calibration-rounds/{round_id}/gold-releases", status_code=201)
async def post_calibration_round_gold_releases(
    round_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ROUND_MANAGE_ROLES, action="calibration_rounds.release_gold")
    body = parse_payload(CalibrationGoldReleaseRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"calibration_rounds.release_gold:{round_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    try:
        response = envelope(release_calibration_gold(session, ctx, round_id, body), ctx)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=201,
            response_json=response,
        )
        session.commit()
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "CALIBRATION_GOLD_RELEASE_CONFLICT",
            "该轮次或金标版本号已由其他发布请求占用",
            409,
        ) from None
    except OperationalError as exc:
        session.rollback()
        if not _is_retryable_database_conflict(exc):
            raise
        raise ApiError(
            "CALIBRATION_GOLD_RELEASE_RETRY",
            "金标版本序列正在被其他发布占用，请使用同一幂等键重试",
            409,
            details=[{"retryable": True}],
        ) from None
    return response
