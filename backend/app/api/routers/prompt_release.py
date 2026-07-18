from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from app.api.deps import ContextDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas.common import parse_payload
from app.schemas.label_closed_loop import (
    PromptAssetCreateRequest,
    PromptVersionCreateRequest,
    ReleaseDeploymentCreateRequest,
    ReleaseHeadBootstrapRequest,
    ReleaseMonitorSampleRequest,
    ReleaseTransitionRequest,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.prompt_release_service import (
    bootstrap_release_bundle_head,
    create_prompt_asset,
    create_prompt_version,
    create_release_deployment,
    get_prompt_asset,
    get_prompt_version,
    get_release_bundle_head,
    get_release_deployment,
    ingest_release_monitor_sample,
    list_prompt_assets,
    list_prompt_versions,
    list_release_deployments,
    transition_release_deployment,
)

router = APIRouter(tags=["prompt-release"])


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


@router.post("/prompt-assets", status_code=201)
async def post_prompt_asset(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="prompt_assets.create",
    )
    operation = "prompt_assets.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=PromptAssetCreateRequest,
    )
    if replay is not None:
        return replay
    response = envelope(create_prompt_asset(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/prompt-assets")
def get_prompt_assets(
    session: SessionDep,
    ctx: ContextDep,
    capability: str | None = None,
    label_version_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_prompt_assets(
        session,
        ctx,
        capability=capability,
        label_version_id=label_version_id,
        status=status,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/prompt-assets/{prompt_asset_id}")
def get_prompt_asset_by_id(
    prompt_asset_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_prompt_asset(session, ctx, prompt_asset_id), ctx)


@router.post("/prompt-versions", status_code=201)
async def post_prompt_version(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="prompt_versions.create",
    )
    operation = "prompt_versions.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=PromptVersionCreateRequest,
    )
    if replay is not None:
        return replay
    response = envelope(create_prompt_version(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/prompt-versions")
def get_prompt_versions(
    session: SessionDep,
    ctx: ContextDep,
    prompt_asset_id: str | None = None,
    label_version_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_prompt_versions(
        session,
        ctx,
        prompt_asset_id=prompt_asset_id,
        label_version_id=label_version_id,
        status=status,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/prompt-versions/{prompt_version_id}")
def get_prompt_version_by_id(
    prompt_version_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_prompt_version(session, ctx, prompt_version_id), ctx)


@router.post("/release-deployments", status_code=201)
async def post_release_deployment(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="release_deployments.create",
    )
    operation = "release_deployments.create"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ReleaseDeploymentCreateRequest,
    )
    if replay is not None:
        return replay
    response = envelope(create_release_deployment(session, ctx, body), ctx)
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


@router.get("/release-deployments")
def get_release_deployments(
    session: SessionDep,
    ctx: ContextDep,
    environment: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = list_release_deployments(
        session,
        ctx,
        environment=environment,
        status=status,
        limit=limit,
    )
    return collection_envelope(items, ctx, total=len(items), limit=limit)


@router.get("/release-deployments/{deployment_id}")
def get_release_deployment_by_id(
    deployment_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    return envelope(get_release_deployment(session, ctx, deployment_id), ctx)


@router.get("/release-bundle-heads/{environment}")
def get_active_release_bundle_head(
    environment: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return envelope(get_release_bundle_head(session, ctx, environment), ctx)


@router.post("/release-deployments/{deployment_id}/monitor-samples")
async def post_release_monitor_sample(
    deployment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ("system",), action="release_deployments.monitor")
    operation = f"release_deployments.monitor:{deployment_id}"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ReleaseMonitorSampleRequest,
    )
    if replay is not None:
        return replay
    response = envelope(
        ingest_release_monitor_sample(session, ctx, deployment_id, body),
        ctx,
    )
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


@router.post("/release-deployments/{deployment_id}/bootstrap-active-head")
async def post_release_active_head_bootstrap(
    deployment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin",),
        action="release_deployments.bootstrap_active_head",
    )
    if "system" in ctx.roles:
        raise ApiError(
            "SYSTEM_RELEASE_BOOTSTRAP_FORBIDDEN",
            "system 身份不能代替项目管理员确认初始 LKG",
            403,
        )
    operation = f"release_deployments.bootstrap_active_head:{deployment_id}"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ReleaseHeadBootstrapRequest,
    )
    if replay is not None:
        return replay
    response = envelope(
        bootstrap_release_bundle_head(session, ctx, deployment_id, body),
        ctx,
    )
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


@router.post("/release-deployments/{deployment_id}/transitions", status_code=202)
async def post_release_transition(
    deployment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    operation = f"release_deployments.transition:{deployment_id}"
    body, body_hash, replay = await _idempotent_body(
        request,
        session,
        ctx,
        operation=operation,
        model=ReleaseTransitionRequest,
    )
    if replay is not None:
        return replay
    require_any_role(ctx, ("project_admin",), action=f"release_deployments.{body.action}")
    if "system" in ctx.roles and body.action in {"approve-gray", "promote"}:
        raise ApiError(
            "SYSTEM_RELEASE_APPROVAL_FORBIDDEN",
            "系统身份不能代替人工批准灰度或正式发布",
            403,
        )
    response = envelope(
        transition_release_deployment(session, ctx, deployment_id, body),
        ctx,
    )
    return _commit_idempotent(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response=response,
    )
