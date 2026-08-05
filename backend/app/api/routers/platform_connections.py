from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy.orm import Session

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.request_identifiers import server_generated_public_id
from app.core.response import collection_envelope, envelope
from app.models import PlatformConnection
from app.services import platform_connection_service
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    api_error_result,
    raise_replayed_api_error,
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event

router = APIRouter(tags=["platform-connections"])
PLATFORM_CONNECTION_WRITE_ROLES = ("project_admin", "asset_manager")


def _create_platform_connection(
    session: Session,
    ctx: RequestContext,
    definition: platform_connection_service.PlatformConnectionCreate,
) -> PlatformConnection:
    platform_connection_id: str | None = None
    for _ in range(5):
        candidate = server_generated_public_id(
            "platform_connection",
            suffix_length=20,
        )
        if session.get(PlatformConnection, candidate) is None:
            platform_connection_id = candidate
            break
    if platform_connection_id is None:
        raise ApiError(
            "PLATFORM_CONNECTION_ID_ALLOCATION_FAILED",
            "平台连接 ID 暂时无法分配，请稍后重试",
            503,
            retryable=True,
        )
    connection = PlatformConnection(
        platform_connection_id=platform_connection_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        external_tenant_ref=definition.external_tenant_ref,
        name=definition.name,
        provider_type=definition.provider_type,
        auth_mode=definition.auth_mode,
        origin=definition.origin,
        credential_ref=definition.credential_ref,
        store_refs=definition.store_refs,
        test_path=definition.test_path,
        status="draft",
        resource_version=1,
        last_test_status=None,
        last_tested_at=None,
        root_trace_id=ctx.trace_id,
        current_trace_id=ctx.trace_id,
    )
    session.add(connection)
    session.flush()
    return connection


@router.post(
    "/platform-connections",
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": (
                        platform_connection_service.PlatformConnectionCreate.model_json_schema()
                    )
                }
            },
        }
    },
)
async def post_platform_connections(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        PLATFORM_CONNECTION_WRITE_ROLES,
        action="platform_connections.create",
    )
    body_hash = await request_hash(request)
    operation = "platform_connections.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        raise_replayed_api_error(replay)
        return replay
    definition = platform_connection_service.parse_platform_connection_create(await request.json())
    connection = _create_platform_connection(session, ctx, definition)
    payload = platform_connection_service.platform_connection_payload(connection)
    record_audit(
        session,
        ctx,
        action="platform_connection.create",
        object_type="platform_connection",
        object_id=connection.platform_connection_id,
        after=payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="platform_connection.created",
        aggregate_type="platform_connection",
        aggregate_id=connection.platform_connection_id,
        payload={
            "platform_connection_id": connection.platform_connection_id,
            "status": connection.status,
            "provider_type": connection.provider_type,
            "auth_mode": connection.auth_mode,
            "origin": connection.origin,
            "external_tenant_ref": connection.external_tenant_ref,
            "resource_version": connection.resource_version,
            "root_trace_id": connection.root_trace_id,
        },
    )
    response = envelope(payload, ctx)
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


@router.get("/platform-connections")
def get_platform_connections(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
) -> dict[str, Any]:
    items, total, next_cursor = platform_connection_service.list_platform_connections(
        session,
        ctx,
        cursor=page.get("cursor"),
        limit=int(page.get("limit") or 50),
    )
    return collection_envelope(
        [
            platform_connection_service.platform_connection_payload(connection)
            for connection in items
        ],
        ctx,
        total=total,
        limit=int(page.get("limit") or 50),
        next_cursor=next_cursor,
    )


@router.get("/platform-connections/{platform_connection_id}")
def get_platform_connections_by_id(
    platform_connection_id: str,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    return envelope(
        platform_connection_service.platform_connection_payload(
            platform_connection_service.get_platform_connection(
                session,
                ctx,
                platform_connection_id,
            )
        ),
        ctx,
    )


@router.post(
    "/platform-connections/{platform_connection_id}/connection-tests",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": (
                        platform_connection_service.PlatformConnectionTestRequest.model_json_schema()
                    )
                }
            },
        }
    },
)
async def post_platform_connection_tests(
    platform_connection_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        PLATFORM_CONNECTION_WRITE_ROLES,
        action="platform_connections.connection_test",
    )
    body_hash = await request_hash(request)
    operation = f"platform_connections.connection_test:{platform_connection_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        raise_replayed_api_error(replay)
        return replay
    body = await request.json()
    if not isinstance(body, dict) or body:
        raise ApiError(
            "VALIDATION_ERROR",
            "平台连接测试不接受客户端运行参数",
            422,
        )

    connection = platform_connection_service.get_platform_connection(
        session,
        ctx,
        platform_connection_id,
    )
    if connection.status == "disabled":
        raise ApiError(
            "PLATFORM_CONNECTION_DISABLED",
            "平台连接已停用，不能执行连通性测试",
            409,
        )
    expected_resource_version = connection.resource_version
    try:
        probe_result = platform_connection_service.probe_platform_connection(connection)
        probe_response_status = probe_result.get("response_status")
        if not isinstance(probe_response_status, int):
            raise ApiError(
                "PLATFORM_CONNECTION_PROBE_RESPONSE_INVALID",
                "平台连接测试返回了无效的 HTTP 状态",
                502,
            )
    except ApiError as error:
        locked = platform_connection_service.get_platform_connection(
            session,
            ctx,
            platform_connection_id,
            for_update=True,
        )
        before = platform_connection_service.platform_connection_payload(locked)
        if locked.resource_version != expected_resource_version:
            raise ApiError(
                "PLATFORM_CONNECTION_CHANGED_DURING_TEST",
                "平台连接在测试期间已被修改，请重新执行",
                409,
            ) from error
        platform_connection_service.mark_connection_test_failure(
            locked,
            ctx,
            tested_at=platform_connection_service.utc_now(),
        )
        after = platform_connection_service.platform_connection_payload(locked)
        record_audit(
            session,
            ctx,
            action="platform_connection.connection_test",
            object_type="platform_connection",
            object_id=platform_connection_id,
            result="failed",
            before=before,
            after=after,
        )
        enqueue_event(
            session,
            ctx,
            event_type="platform_connection.connection_test_failed",
            aggregate_type="platform_connection",
            aggregate_id=platform_connection_id,
            payload={
                "platform_connection_id": platform_connection_id,
                "status": locked.status,
                "resource_version": locked.resource_version,
                "root_trace_id": locked.root_trace_id,
            },
        )
        failure = api_error_result(ctx, error)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=error.status_code,
            response_json=failure,
        )
        session.commit()
        raise error

    locked = platform_connection_service.get_platform_connection(
        session,
        ctx,
        platform_connection_id,
        for_update=True,
    )
    if locked.resource_version != expected_resource_version:
        raise ApiError(
            "PLATFORM_CONNECTION_CHANGED_DURING_TEST",
            "平台连接在测试期间已被修改，请重新执行",
            409,
        )
    before = platform_connection_service.platform_connection_payload(locked)
    tested_at = platform_connection_service.utc_now()
    platform_connection_service.mark_connection_test_success(
        locked,
        ctx,
        tested_at=tested_at,
    )
    after = platform_connection_service.platform_connection_payload(locked)
    record_audit(
        session,
        ctx,
        action="platform_connection.connection_test",
        object_type="platform_connection",
        object_id=platform_connection_id,
        before=before,
        after=after,
    )
    enqueue_event(
        session,
        ctx,
        event_type="platform_connection.connection_tested",
        aggregate_type="platform_connection",
        aggregate_id=platform_connection_id,
        payload={
            "platform_connection_id": platform_connection_id,
            "status": locked.status,
            "resource_version": locked.resource_version,
            "root_trace_id": locked.root_trace_id,
        },
    )
    response = envelope(
        {
            "resource_type": "platform_connection",
            "resource_id": platform_connection_id,
            "status": "success",
            "response_status": probe_response_status,
            "root_trace_id": locked.root_trace_id,
            "current_trace_id": ctx.trace_id,
            "readback_url": (f"/api/v1/platform-connections/{platform_connection_id}"),
            "affected_objects": [
                {
                    "type": "platform_connection",
                    "id": platform_connection_id,
                    "resource_version": locked.resource_version,
                }
            ],
            "next_actions": [
                {
                    "action": "create_audio_import_connector",
                    "label": "新建音频导入配置",
                }
            ],
            "tested_at": tested_at.isoformat(),
        },
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


@router.post(
    "/platform-connections/{platform_connection_id}/session",
    status_code=410,
)
def post_deprecated_platform_connection_session(
    platform_connection_id: str,
) -> None:
    raise ApiError(
        "PLATFORM_SESSION_ENDPOINT_DEPRECATED",
        "平台会话不再由浏览器创建；请配置 credential_ref 并执行连接测试",
        410,
        details=[
            {
                "platform_connection_id": platform_connection_id,
                "replacement": (
                    f"/api/v1/platform-connections/{platform_connection_id}/connection-tests"
                ),
            }
        ],
    )
