from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.responses import Response

from app.api.deps import ContextDep, SessionDep
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.models import IdempotencyRecord
from app.schemas.common import ApiErrorEnvelope
from app.schemas.label_fact_sets import (
    LabelFactSetApproveRequest,
    LabelFactSetCreateRequest,
    LabelFactSetMutationEnvelope,
    LabelFactSetPromotionEnvelope,
    LabelFactSetPublishPromoteRequest,
    LabelFactSetRollbackRequest,
    LabelFactSetValidateRequest,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    require_idempotency,
    save_idempotency_result,
)
from app.services.label_fact_set_service import (
    approve_label_fact_set,
    create_label_fact_set,
    promote_label_fact_set,
    validate_label_fact_set,
)


class LabelFactSetContractRoute(APIRoute):
    """Keep request validation on the shared ApiError envelope contract."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await original_route_handler(request)
            except RequestValidationError as exc:
                raise ApiError(
                    "VALIDATION_ERROR",
                    "请求参数校验失败",
                    422,
                    details=[
                        {
                            "field": ".".join(str(part) for part in error["loc"]),
                            "message": str(error["msg"]),
                            "code": str(error["type"]),
                        }
                        for error in exc.errors()
                    ],
                ) from exc

        return route_handler


router = APIRouter(tags=["label-fact-sets"], route_class=LabelFactSetContractRoute)

FACT_SET_WRITER_ROLES = ("project_admin", "model_engineer")
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorEnvelope, "description": "上下文或幂等键无效"},
    401: {"model": ApiErrorEnvelope, "description": "身份认证失败"},
    403: {"model": ApiErrorEnvelope, "description": "角色或自然人审批权限不足"},
    404: {"model": ApiErrorEnvelope, "description": "FactSet 或标签版本不存在"},
    409: {"model": ApiErrorEnvelope, "description": "幂等、状态、Manifest 或 Head CAS 冲突"},
    422: {"model": ApiErrorEnvelope, "description": "请求或 Manifest 语义校验失败"},
    429: {"model": ApiErrorEnvelope, "description": "请求频率超过限额"},
    503: {"model": ApiErrorEnvelope, "description": "依赖暂时不可用"},
}

_CREATE_HTTP_OPERATION = "http.label_fact_sets.create"
_VALIDATE_HTTP_OPERATION = "http.label_fact_sets.validate"
_APPROVE_HTTP_OPERATION = "http.label_fact_sets.approve"
_PROMOTE_HTTP_OPERATION = "http.label_fact_sets.promote"
_ROLLBACK_HTTP_OPERATION = "http.label_fact_sets.rollback"


def _authorize_writer(
    ctx: RequestContext,
    *,
    roles: Iterable[str],
    action: str,
) -> None:
    require_idempotency(ctx)
    require_any_role(ctx, roles, action=action)


def _authorize_human_project_admin(
    ctx: RequestContext,
    *,
    action: str,
    agent_error_code: str,
) -> None:
    require_idempotency(ctx)
    if ctx.actor_kind != "human" or ctx.user_id == "system" or "system" in ctx.roles:
        raise ApiError(
            agent_error_code,
            "FactSet 审批与生产 Head 切换只能由人工项目管理员执行",
            403,
        )
    require_any_role(ctx, ("project_admin",), action=action)


def _guard_http_idempotency_actor(
    session: SessionDep,
    ctx: RequestContext,
    *,
    operation: str,
) -> None:
    key = ctx.idempotency_key or ""
    existing = session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.project_id == ctx.project_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None and existing.user_id != ctx.user_id:
        raise ApiError(
            "LABEL_FACT_SET_IDEMPOTENCY_ACTOR_CONFLICT",
            "该 FactSet HTTP 幂等键已由另一操作人使用",
            409,
        )


async def _begin_http_operation(
    request: Request,
    session: SessionDep,
    ctx: RequestContext,
    *,
    operation: str,
) -> tuple[str, dict[str, Any] | None]:
    _guard_http_idempotency_actor(session, ctx, operation=operation)
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        session.commit()
    return body_hash, replay


def _complete_http_operation(
    session: SessionDep,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    response = envelope(data, ctx)
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
    "/label-fact-sets",
    operation_id="postLabelFactSets",
    status_code=201,
    response_model=LabelFactSetMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_fact_sets(
    body: LabelFactSetCreateRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_writer(
        ctx,
        roles=FACT_SET_WRITER_ROLES,
        action="label-fact-sets.create",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_CREATE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = create_label_fact_set(session, ctx, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_CREATE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=201,
        data=data,
    )


@router.post(
    "/label-fact-sets/{id}/validations",
    operation_id="postLabelFactSetsByIdValidations",
    response_model=LabelFactSetMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_fact_set_validations(
    id: str,
    body: LabelFactSetValidateRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_writer(
        ctx,
        roles=FACT_SET_WRITER_ROLES,
        action="label-fact-sets.validate",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_VALIDATE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = validate_label_fact_set(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_VALIDATE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-fact-sets/{id}/approvals",
    operation_id="postLabelFactSetsByIdApprovals",
    response_model=LabelFactSetMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_fact_set_approvals(
    id: str,
    body: LabelFactSetApproveRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_human_project_admin(
        ctx,
        action="label-fact-sets.approve",
        agent_error_code="AGENT_LABEL_FACT_SET_APPROVAL_FORBIDDEN",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_APPROVE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = approve_label_fact_set(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_APPROVE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-fact-sets/{id}/promotions",
    operation_id="postLabelFactSetsByIdPromotions",
    response_model=LabelFactSetPromotionEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_fact_set_promotions(
    id: str,
    body: LabelFactSetPublishPromoteRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_human_project_admin(
        ctx,
        action="label-fact-sets.promote",
        agent_error_code="AGENT_LABEL_FACT_SET_PROMOTION_FORBIDDEN",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_PROMOTE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = promote_label_fact_set(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_PROMOTE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )


@router.post(
    "/label-fact-sets/{id}/rollbacks",
    operation_id="postLabelFactSetsByIdRollbacks",
    response_model=LabelFactSetPromotionEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
async def post_label_fact_set_rollbacks(
    id: str,
    body: LabelFactSetRollbackRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    _authorize_human_project_admin(
        ctx,
        action="label-fact-sets.rollback",
        agent_error_code="AGENT_LABEL_FACT_SET_PROMOTION_FORBIDDEN",
    )
    body_hash, replay = await _begin_http_operation(
        request,
        session,
        ctx,
        operation=_ROLLBACK_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = promote_label_fact_set(session, ctx, id, body)
    return _complete_http_operation(
        session,
        ctx,
        operation=_ROLLBACK_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=200,
        data=data,
    )
