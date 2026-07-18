from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, SessionDep
from app.core.response import envelope
from app.schemas.common import ApiEnvelope
from app.schemas.label_lifecycle import (
    LabelVersionDeprecationPreflightRequest,
    LabelVersionDeprecationPreflightResponse,
    LabelVersionTransitionRequest,
    LabelVersionTransitionResponse,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.label_lifecycle_service import (
    create_label_version_deprecation_preflight,
    transition_label_version,
)

router = APIRouter(tags=["label-lifecycle"])

_PREFLIGHT_HTTP_OPERATION = "http.label_version.deprecation_preflight"
_TRANSITION_HTTP_OPERATION = "http.label_version.transition"


def _commit_envelope(
    session: SessionDep,
    ctx: ContextDep,
    *,
    operation: str,
    body_hash: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay
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


@router.post(
    "/label-versions/{id}/deprecation-preflights",
    response_model=ApiEnvelope[LabelVersionDeprecationPreflightResponse],
)
async def post_label_version_deprecation_preflight(
    id: str,
    body: LabelVersionDeprecationPreflightRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    body_hash = await request_hash(request)
    data = create_label_version_deprecation_preflight(session, ctx, id, body)
    return _commit_envelope(
        session,
        ctx,
        operation=_PREFLIGHT_HTTP_OPERATION,
        body_hash=body_hash,
        data=data,
    )


@router.post(
    "/label-versions/{id}/transitions",
    response_model=ApiEnvelope[LabelVersionTransitionResponse],
)
async def post_label_version_transition(
    id: str,
    body: LabelVersionTransitionRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    body_hash = await request_hash(request)
    data = transition_label_version(session, ctx, id, body)
    return _commit_envelope(
        session,
        ctx,
        operation=_TRANSITION_HTTP_OPERATION,
        body_hash=body_hash,
        data=data,
    )
