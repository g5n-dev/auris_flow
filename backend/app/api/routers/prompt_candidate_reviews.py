from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.schemas.common import parse_payload
from app.schemas.prompt_candidate_review import (
    PromptReviewAdjudicationRequest,
    PromptReviewSubmissionRequest,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    require_idempotency,
    save_idempotency_result,
)
from app.services.prompt_candidate_review_service import (
    adjudicate_prompt_candidate_review,
    submit_prompt_candidate_review,
)

router = APIRouter(tags=["prompt-candidate-reviews"])


@router.post(
    "/prompt-version-candidates/{candidate_id}/review-submissions",
    status_code=201,
)
async def post_prompt_candidate_review_submission(
    candidate_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator", "annotator"),
        action="prompt_version_candidates.review_submit",
    )
    if "system" in ctx.roles:
        raise ApiError(
            "FORBIDDEN",
            "系统账号不能代替人工提交 Prompt 盲审结论",
            403,
        )
    require_idempotency(ctx)
    operation = f"prompt_version_candidates.review_submit:{candidate_id}"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(PromptReviewSubmissionRequest, await request.json())
    response = envelope(
        submit_prompt_candidate_review(session, ctx, candidate_id, body),
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
    return response


@router.post(
    "/prompt-version-candidates/{candidate_id}/adjudications",
    status_code=201,
)
async def post_prompt_candidate_adjudication(
    candidate_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("review_arbitrator",),
        action="prompt_version_candidates.adjudicate",
    )
    if "review_arbitrator" not in ctx.roles:
        raise ApiError(
            "FORBIDDEN",
            "只有 review_arbitrator 可以裁决 Prompt 双盲分歧",
            403,
        )
    require_idempotency(ctx)
    operation = f"prompt_version_candidates.adjudicate:{candidate_id}"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(PromptReviewAdjudicationRequest, await request.json())
    response = envelope(
        adjudicate_prompt_candidate_review(session, ctx, candidate_id, body),
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
    return response
