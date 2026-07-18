from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.response import envelope
from app.schemas import (
    ProjectSceneProfileBindingRequest,
    SceneProfileCreateRequest,
    SceneProfileGenerationRequest,
    SceneProfilePatchRequest,
    SceneProfilePublishRequest,
    SceneProfileReviewRequest,
    parse_payload,
)
from app.services.scene_profile_service import (
    bind_project_scene_profile,
    create_scene_profile,
    get_active_scene_binding,
    get_scene_profile_detail,
    list_scene_profiles,
    patch_scene_profile_version,
    publish_scene_profile_version,
    request_scene_profile_generation,
    review_scene_profile_version,
    validate_scene_profile_version,
)

router = APIRouter(tags=["scene-profiles"])


@router.get("/scene-profiles")
def get_scene_profiles(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    return list_scene_profiles(session, ctx, page)


@router.post("/scene-profiles", status_code=201)
async def post_scene_profiles(request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(SceneProfileCreateRequest, await request.json())
    return await create_scene_profile(session, ctx, request, body)


@router.get("/scene-profiles/{scene_profile_id}")
def get_scene_profiles_by_id(
    scene_profile_id: str,
    session: SessionDep,
    ctx: ContextDep,
):
    return get_scene_profile_detail(session, ctx, scene_profile_id)


@router.post("/scene-profile-generation-runs", status_code=202)
async def post_scene_profile_generation_runs(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(SceneProfileGenerationRequest, await request.json())
    return await request_scene_profile_generation(session, ctx, request, body)


@router.patch("/scene-profile-versions/{scene_profile_version_id}")
async def patch_scene_profile_versions_by_id(
    scene_profile_version_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(SceneProfilePatchRequest, await request.json())
    return await patch_scene_profile_version(
        session,
        ctx,
        request,
        scene_profile_version_id,
        body,
    )


@router.post("/scene-profile-versions/{scene_profile_version_id}/validations")
async def post_scene_profile_version_validations(
    scene_profile_version_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    return await validate_scene_profile_version(
        session,
        ctx,
        request,
        scene_profile_version_id,
    )


@router.post("/scene-profile-versions/{scene_profile_version_id}/reviews")
async def post_scene_profile_version_reviews(
    scene_profile_version_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(SceneProfileReviewRequest, await request.json())
    return await review_scene_profile_version(
        session,
        ctx,
        request,
        scene_profile_version_id,
        body,
    )


@router.post("/scene-profile-versions/{scene_profile_version_id}/publish")
async def post_scene_profile_version_publish(
    scene_profile_version_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(SceneProfilePublishRequest, await request.json())
    return await publish_scene_profile_version(
        session,
        ctx,
        request,
        scene_profile_version_id,
        body,
    )


@router.get("/projects/{project_id}/scene-profile")
def get_project_scene_profile(
    project_id: str,
    session: SessionDep,
    ctx: ContextDep,
    environment: str = Query(default="production", pattern="^(development|staging|production)$"),
    allow_missing: bool = Query(default=False),
):
    if project_id != ctx.project_id:
        from app.core.errors import ApiError

        raise ApiError("PROJECT_CONTEXT_MISMATCH", "只能读取当前上下文项目场景", 403)
    return envelope(
        get_active_scene_binding(
            session,
            ctx,
            environment,
            allow_missing=allow_missing,
        ),
        ctx,
    )


@router.put("/projects/{project_id}/scene-profile")
async def put_project_scene_profile(
    project_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ProjectSceneProfileBindingRequest, await request.json())
    return await bind_project_scene_profile(session, ctx, request, project_id, body)
