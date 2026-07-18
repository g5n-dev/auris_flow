from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response

from app.api.deps import ContextDep, SessionDep
from app.core.errors import ApiError
from app.core.response import envelope
from app.schemas.common import ApiErrorEnvelope
from app.schemas.label_recomputations import (
    LabelRecomputeMutationEnvelope,
    LabelRecomputeRunCreateRequest,
    LabelRecomputeRunItemCompletionRequest,
    LabelRecomputeRunItemMutationEnvelope,
    LabelRecomputeRunItemRetryRequest,
)
from app.services.label_recomputation_service import (
    complete_label_recompute_run_item,
    create_label_recompute_run,
    retry_label_recompute_run_item,
)


class LabelRecomputeContractRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await original(request)
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


router = APIRouter(tags=["label-recomputations"], route_class=LabelRecomputeContractRoute)

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorEnvelope, "description": "上下文或幂等键无效"},
    401: {"model": ApiErrorEnvelope, "description": "身份认证失败"},
    403: {"model": ApiErrorEnvelope, "description": "角色或租户项目范围不足"},
    404: {"model": ApiErrorEnvelope, "description": "运行、分区或冻结锚点不存在"},
    409: {"model": ApiErrorEnvelope, "description": "锚点、回执、状态或 manifest 冲突"},
    422: {"model": ApiErrorEnvelope, "description": "请求语义校验失败"},
}


@router.post(
    "/label-recompute-runs",
    operation_id="postLabelRecomputeRuns",
    status_code=201,
    response_model=LabelRecomputeMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
def post_label_recompute_runs(
    body: LabelRecomputeRunCreateRequest,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    data = create_label_recompute_run(session, ctx, body)
    response = envelope(data, ctx)
    session.commit()
    return response


@router.post(
    "/label-recompute-runs/{run_id}/items/{item_id}/completions",
    operation_id="postLabelRecomputeRunItemsByIdCompletions",
    response_model=LabelRecomputeRunItemMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
def post_label_recompute_run_item_completions(
    run_id: str,
    item_id: str,
    body: LabelRecomputeRunItemCompletionRequest,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    data = complete_label_recompute_run_item(session, ctx, run_id, item_id, body)
    response = envelope(data, ctx)
    session.commit()
    return response


@router.post(
    "/label-recompute-runs/{run_id}/items/{item_id}/retries",
    operation_id="postLabelRecomputeRunItemsByIdRetries",
    response_model=LabelRecomputeRunItemMutationEnvelope,
    response_model_exclude_unset=True,
    responses=ERROR_RESPONSES,
)
def post_label_recompute_run_item_retries(
    run_id: str,
    item_id: str,
    body: LabelRecomputeRunItemRetryRequest,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    data = retry_label_recompute_run_item(session, ctx, run_id, item_id, body)
    response = envelope(data, ctx)
    session.commit()
    return response
