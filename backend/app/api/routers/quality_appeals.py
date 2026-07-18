from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import (
    QualityAppealClaimRequest,
    QualityAppealCreateRequest,
    QualityAppealDecisionRequest,
    QualityAppealWithdrawalRequest,
    parse_payload,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.quality_appeal_service import (
    claim_quality_appeal,
    create_quality_appeal,
    decide_quality_appeal,
    get_quality_appeal,
    list_quality_appeals,
    quality_appeal_data,
    withdraw_quality_appeal,
)

router = APIRouter(tags=["quality-appeals"])

APPEAL_SUBMIT_ROLES = (
    "project_admin",
    "review_arbitrator",
    "annotator",
    "business_operator",
)
APPEAL_REVIEW_ROLES = ("project_admin", "review_arbitrator")


@router.post("/quality-appeals", status_code=201)
async def post_quality_appeals(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, APPEAL_SUBMIT_ROLES, action="quality_appeals.submit")
    body_hash = await request_hash(request)
    operation = "quality_appeals.submit"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(QualityAppealCreateRequest, await request.json()).model_dump()
    try:
        data = create_quality_appeal(session, ctx, body)
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "QUALITY_APPEAL_ALREADY_EXISTS",
            "该终态人审裁决已经存在申诉",
            409,
        ) from None
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
    return response


@router.get("/quality-appeals")
def get_quality_appeals(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
    source_decision_id: str | None = None,
    appellant_id: str | None = None,
) -> dict[str, Any]:
    limit = int(page["limit"] or 50)
    items = list_quality_appeals(
        session,
        ctx,
        status=status,
        source_decision_id=source_decision_id,
        appellant_id=appellant_id,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/quality-appeals/{id}")
def get_quality_appeals_by_id(
    id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return envelope(quality_appeal_data(get_quality_appeal(session, ctx, id)), ctx)


@router.post("/quality-appeals/{id}/claims")
async def post_quality_appeals_by_id_claims(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, APPEAL_REVIEW_ROLES, action="quality_appeals.claim")
    body_hash = await request_hash(request)
    operation = f"quality_appeals.claim:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(QualityAppealClaimRequest, await request.json())
    response = envelope(
        claim_quality_appeal(
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


@router.post("/quality-appeals/{id}/decisions")
async def post_quality_appeals_by_id_decisions(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, APPEAL_REVIEW_ROLES, action="quality_appeals.decide")
    body_hash = await request_hash(request)
    operation = f"quality_appeals.decide:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(QualityAppealDecisionRequest, await request.json())
    try:
        data = decide_quality_appeal(
            session,
            ctx,
            id,
            decision=body.decision,
            reason=body.reason,
            expected_resource_version=body.expected_resource_version,
        )
    except IntegrityError:
        session.rollback()
        raise ApiError(
            "QUALITY_APPEAL_ALREADY_RESOLVED",
            "申诉已由其他请求生成终态裁决",
            409,
        ) from None
    response = envelope(data, ctx)
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


@router.post("/quality-appeals/{id}/withdrawals")
async def post_quality_appeals_by_id_withdrawals(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, APPEAL_SUBMIT_ROLES, action="quality_appeals.withdraw")
    body_hash = await request_hash(request)
    operation = f"quality_appeals.withdraw:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(QualityAppealWithdrawalRequest, await request.json())
    response = envelope(
        withdraw_quality_appeal(
            session,
            ctx,
            id,
            reason=body.reason,
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
