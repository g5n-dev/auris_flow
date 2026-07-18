from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import (
    HotwordAnalysisRunRequest,
    HotwordBadcaseCreateRequest,
    HotwordBadcaseDecisionRequest,
    HotwordBadcasePatchRequest,
    HotwordEvalRunRequest,
    HotwordItemCreateRequest,
    HotwordItemPatchRequest,
    HotwordPackCreateRequest,
    HotwordPackVersionCreateRequest,
    HotwordPackVersionPatchRequest,
    HotwordPublishRequest,
    HotwordRollbackRequest,
    parse_payload,
)
from app.schemas.label_closed_loop import LabelBadcaseCreateRequest
from app.services.hotword_rollback_service import create_hotword_rollback
from app.services.hotword_service import (
    BADCASE_WRITE_ROLES,
    HOTWORD_READ_ROLES,
    PACK_MANAGE_ROLES,
    create_badcase,
    create_hotword_analysis_run,
    create_hotword_eval_run,
    create_hotword_item,
    create_hotword_pack,
    create_hotword_version,
    decide_badcase,
    delete_hotword_item,
    get_hotword_version,
    hotword_statistics,
    list_badcases,
    list_hotword_packs,
    list_hotword_versions,
    patch_badcase,
    patch_hotword_item,
    patch_hotword_version,
    publish_hotword_version,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.label_closed_loop_service import create_label_badcase

router = APIRouter(tags=["hotwords"])


def _offset(page: dict[str, str | int | None]) -> int:
    raw = page.get("cursor")
    if raw in (None, ""):
        return 0
    try:
        value = int(str(raw))
    except ValueError:
        raise ApiError("INVALID_CURSOR", "cursor 必须是非负整数", 400) from None
    if value < 0:
        raise ApiError("INVALID_CURSOR", "cursor 必须是非负整数", 400)
    return value


def _next_cursor(offset: int, limit: int, total: int) -> str | None:
    return str(offset + limit) if offset + limit < total else None


def _parse_date(value: str | None, *, end: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ApiError("HOTWORD_DATE_FILTER_INVALID", "日期筛选必须使用 YYYY-MM-DD", 422) from None
    boundary = datetime.combine(parsed, time.min, tzinfo=UTC)
    return boundary + timedelta(days=1) if end else boundary


def _finish_write(
    session: SessionDep,
    ctx: ContextDep,
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


@router.get("/hotword-statistics")
def get_hotword_statistics(
    session: SessionDep,
    ctx: ContextDep,
    date_from: str | None = None,
    date_to: str | None = None,
    store_id: str | None = None,
    provider: str | None = None,
    model_version: str | None = None,
    hotword_pack_version_id: str | None = None,
) -> dict[str, Any]:
    require_any_role(ctx, HOTWORD_READ_ROLES, action="hotword_statistics.read")
    start = _parse_date(date_from)
    end = _parse_date(date_to, end=True)
    if start is not None and end is not None and start >= end:
        raise ApiError("HOTWORD_DATE_RANGE_INVALID", "开始日期不能晚于结束日期", 422)
    return envelope(
        hotword_statistics(
            session,
            ctx,
            date_from=start,
            date_to=end,
            store_id=store_id,
            provider=provider,
            model_version=model_version,
            hotword_pack_version_id=hotword_pack_version_id,
        ),
        ctx,
    )


@router.get("/badcases")
def get_badcases(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    capability: str | None = None,
    error_type: str | None = None,
    status: str | None = None,
    hotword_pack_version_id: str | None = None,
) -> dict[str, Any]:
    require_any_role(ctx, HOTWORD_READ_ROLES, action="badcases.read")
    offset = _offset(page)
    limit = int(page.get("limit") or 50)
    items, total = list_badcases(
        session,
        ctx,
        capability=capability,
        error_type=error_type,
        status=status,
        hotword_pack_version_id=hotword_pack_version_id,
        offset=offset,
        limit=limit,
    )
    return collection_envelope(
        items,
        ctx,
        total=total,
        limit=limit,
        next_cursor=_next_cursor(offset, limit, total),
    )


@router.post("/badcases", status_code=201)
async def post_badcases(request: Request, session: SessionDep, ctx: ContextDep) -> dict[str, Any]:
    require_any_role(ctx, BADCASE_WRITE_ROLES, action="badcases.create")
    raw_body = await request.json()
    capability = str(raw_body.get("capability") or "asr-hotword")
    body = (
        parse_payload(LabelBadcaseCreateRequest, raw_body)
        if capability in {"labeling", "prompt-optimization"}
        else parse_payload(HotwordBadcaseCreateRequest, raw_body)
    )
    body_hash = await request_hash(request)
    operation = "badcases.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        data=(
            create_label_badcase(session, ctx, body)
            if isinstance(body, LabelBadcaseCreateRequest)
            else create_badcase(session, ctx, body)
        ),
    )


@router.patch("/badcases/{badcase_id}")
async def patch_badcases(
    badcase_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, BADCASE_WRITE_ROLES, action="badcases.update")
    body = parse_payload(HotwordBadcasePatchRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"badcases.update:{badcase_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        data=patch_badcase(session, ctx, badcase_id, body),
    )


@router.post("/badcases/{badcase_id}/decisions", status_code=201)
async def post_badcase_decisions(
    badcase_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, BADCASE_WRITE_ROLES, action="badcases.decide")
    body = parse_payload(HotwordBadcaseDecisionRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"badcases.decide:{badcase_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        data=decide_badcase(session, ctx, badcase_id, body),
    )


@router.get("/hotword-packs")
def get_hotword_packs(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
) -> dict[str, Any]:
    require_any_role(ctx, HOTWORD_READ_ROLES, action="hotword_packs.read")
    offset = _offset(page)
    limit = int(page.get("limit") or 50)
    items, total = list_hotword_packs(session, ctx, status=status, offset=offset, limit=limit)
    return collection_envelope(
        items,
        ctx,
        total=total,
        limit=limit,
        next_cursor=_next_cursor(offset, limit, total),
    )


@router.post("/hotword-packs", status_code=201)
async def post_hotword_packs(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_packs.create")
    body = parse_payload(HotwordPackCreateRequest, await request.json())
    body_hash = await request_hash(request)
    operation = "hotword_packs.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        data=create_hotword_pack(session, ctx, body),
    )


@router.get("/hotword-packs/{pack_id}/versions")
def get_hotword_pack_versions(
    pack_id: str,
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
) -> dict[str, Any]:
    require_any_role(ctx, HOTWORD_READ_ROLES, action="hotword_pack_versions.read")
    offset = _offset(page)
    limit = int(page.get("limit") or 50)
    items, total = list_hotword_versions(
        session,
        ctx,
        pack_id,
        status=status,
        offset=offset,
        limit=limit,
    )
    return collection_envelope(
        items,
        ctx,
        total=total,
        limit=limit,
        next_cursor=_next_cursor(offset, limit, total),
    )


@router.post("/hotword-packs/{pack_id}/versions", status_code=201)
async def post_hotword_pack_versions(
    pack_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_pack_versions.create")
    body = parse_payload(HotwordPackVersionCreateRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_pack_versions.create:{pack_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        data=create_hotword_version(session, ctx, pack_id, body),
    )


@router.get("/hotword-pack-versions/{version_id}")
def get_hotword_pack_version(
    version_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    from app.services.hotword_service import _version_data

    require_any_role(ctx, HOTWORD_READ_ROLES, action="hotword_pack_versions.read")
    version = get_hotword_version(session, ctx, version_id)
    if version.status not in {"published", "deprecated"} and not (
        set(ctx.roles) & (set(PACK_MANAGE_ROLES) | {"review_arbitrator"})
    ):
        raise ApiError(
            "FORBIDDEN",
            "未发布热词版本详情仅对模型负责人、项目管理员和复核仲裁员可见",
            403,
        )
    return envelope(
        _version_data(session, version, include_items=True),
        ctx,
    )


@router.patch("/hotword-pack-versions/{version_id}")
async def patch_hotword_pack_version(
    version_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_pack_versions.update")
    body = parse_payload(HotwordPackVersionPatchRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_pack_versions.update:{version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        data=patch_hotword_version(session, ctx, version_id, body),
    )


@router.post("/hotword-pack-versions/{version_id}/items", status_code=201)
async def post_hotword_version_items(
    version_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_version_items.create")
    body = parse_payload(HotwordItemCreateRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_version_items.create:{version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        data=create_hotword_item(session, ctx, version_id, body),
    )


@router.patch("/hotword-pack-versions/{version_id}/items/{item_id}")
async def patch_hotword_version_items(
    version_id: str,
    item_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_version_items.update")
    body = parse_payload(HotwordItemPatchRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_version_items.update:{version_id}:{item_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        data=patch_hotword_item(session, ctx, version_id, item_id, body),
    )


@router.delete("/hotword-pack-versions/{version_id}/items/{item_id}")
async def delete_hotword_version_items(
    version_id: str,
    item_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    expected_resource_version: int = Query(ge=1),
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_version_items.delete")
    body_hash = await request_hash(request)
    operation = f"hotword_version_items.delete:{version_id}:{item_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        data=delete_hotword_item(
            session,
            ctx,
            version_id,
            item_id,
            expected_resource_version=expected_resource_version,
        ),
    )


@router.post("/hotword-analysis-runs", status_code=202)
async def post_hotword_analysis_runs(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_analysis.request")
    body = parse_payload(HotwordAnalysisRunRequest, await request.json())
    body_hash = await request_hash(request)
    operation = "hotword_analysis_runs.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        data=create_hotword_analysis_run(session, ctx, body.model_dump(exclude_none=True)),
    )


@router.post("/hotword-pack-versions/{version_id}/eval-runs", status_code=202)
async def post_hotword_eval_runs(
    version_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, PACK_MANAGE_ROLES, action="hotword_pack_versions.evaluate")
    body = parse_payload(HotwordEvalRunRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_pack_versions.evaluate:{version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        data=create_hotword_eval_run(session, ctx, version_id, body),
    )


@router.post("/hotword-pack-versions/{version_id}/publish", status_code=202)
async def post_hotword_publish(
    version_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, {"project_admin"}, action="hotword_pack_versions.publish")
    body = parse_payload(HotwordPublishRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_pack_versions.publish:{version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        data=publish_hotword_version(session, ctx, version_id, body),
    )


@router.post("/hotword-pack-versions/{version_id}/rollback", status_code=202)
async def post_hotword_rollback(
    version_id: str, request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, {"model_engineer"}, action="hotword_pack_versions.rollback")
    body = parse_payload(HotwordRollbackRequest, await request.json())
    body_hash = await request_hash(request)
    operation = f"hotword_pack_versions.rollback:{version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    return _finish_write(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        data=create_hotword_rollback(session, ctx, version_id, body),
    )
