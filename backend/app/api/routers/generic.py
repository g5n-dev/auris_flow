from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import ContextDep, PaginationDep, SessionDep, SignedCompletionContextDep
from app.core.errors import ApiError
from app.core.project_membership import (
    conflicting_project_member_identities,
    duplicate_project_member_user_ids,
    project_member_user_id,
    user_has_project_membership,
)
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import Project, RunRecord, Tenant
from app.schemas import (
    ExternalCallbackRequest,
    KnowledgeBuildRequest,
    KnowledgeRecallRequest,
    RunCompletionReceiptRequest,
    RunReleaseDecisionRequest,
    TaskRunRetryRequest,
    parse_payload,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.knowledge_recall_service import recall_knowledge_index
from app.services.outbox_service import enqueue_event
from app.services.release_gate_service import (
    decide_release_gate,
    prepare_settings_publish,
)
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_data,
    list_resource_page,
    page_limit,
    patch_idempotent_json_resource,
    status_counts,
    upsert_idempotent_json_resource,
)
from app.services.run_service import complete_run_from_receipt, create_run, get_run, retry_run

router = APIRouter(tags=["generic"])


def _require_unique_project_members(members: object) -> None:
    conflicts = conflicting_project_member_identities(members)
    if conflicts:
        raise ApiError(
            "PROJECT_MEMBER_IDENTITY_CONFLICT",
            "项目成员 user_id 与 id 别名冲突",
            422,
            details=[{"conflict_count": len(conflicts)}],
        )
    duplicates = duplicate_project_member_user_ids(members)
    if duplicates:
        raise ApiError(
            "PROJECT_MEMBER_DUPLICATE",
            "项目成员 user_id 必须唯一",
            422,
            details=[{"duplicate_user_ids": list(duplicates)}],
        )


KNOWLEDGE_QDRANT_COLLECTION = "knowledge_chunks"
QDRANT_COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
QDRANT_AUTHORITY_FIELDS = frozenset(
    {
        "tenant_id",
        "project_id",
        "trace_id",
        "collection",
        "collection_name",
        "vector_collection",
        "qdrant_collection",
        "qdrant_payload",
        "knowledge_index_id",
        "index_id",
        "index_ref",
        "index_refs",
        "knowledge_source_id",
        "source_id",
        "source_ref",
        "source_refs",
        "source_type",
        "asset_key",
        "version",
        "connector_id",
        "business_ref",
        "affected_objects",
    }
)


def qdrant_caller_fields(payload: dict[str, Any]) -> dict[str, Any]:
    caller_fields = {
        field: value for field, value in payload.items() if field not in QDRANT_AUTHORITY_FIELDS
    }
    scope = caller_fields.get("scope")
    if isinstance(scope, dict):
        caller_fields["scope"] = {
            field: value for field, value in scope.items() if field not in QDRANT_AUTHORITY_FIELDS
        }
    return caller_fields


def knowledge_qdrant_collection(
    source: dict[str, Any], *, index: dict[str, Any] | None = None
) -> str:
    configured_collection = (
        index.get("vector_collection") if index is not None else source.get("vector_collection")
    )
    if configured_collection is None:
        return KNOWLEDGE_QDRANT_COLLECTION
    if not isinstance(configured_collection, str) or not QDRANT_COLLECTION_PATTERN.fullmatch(
        configured_collection
    ):
        raise ApiError(
            "QDRANT_COLLECTION_INVALID",
            "知识索引 collection 名称不符合服务端安全规则",
            422,
            details=[
                {
                    "field": "vector_collection",
                    "message": "collection 只能包含字母、数字、下划线或连字符",
                    "code": "invalid_collection_name",
                }
            ],
        )
    if configured_collection != KNOWLEDGE_QDRANT_COLLECTION:
        raise ApiError(
            "QDRANT_COLLECTION_FORBIDDEN",
            "知识索引不能切换到未授权的 Qdrant collection",
            422,
            details=[
                {
                    "field": "vector_collection",
                    "message": "collection 不在知识域服务端映射中",
                    "code": "collection_not_allowed",
                }
            ],
        )
    return KNOWLEDGE_QDRANT_COLLECTION


def export_job_payload(record: RunRecord) -> dict[str, Any]:
    payload = record.payload or {}
    dispatch = payload.get("dispatch") if isinstance(payload.get("dispatch"), dict) else None
    details = dispatch.get("details", {}) if isinstance(dispatch, dict) else {}
    storage_object_id = details.get("storage_object_id")
    object_uri = details.get("object_uri")
    content_type = details.get("content_type") or payload.get("content_type", "application/json")
    download_ref = None
    if dispatch and dispatch.get("adapter") == "object_storage":
        download_ref = {
            "kind": "object_storage_reference",
            "status": "ready" if record.status == "success" and object_uri else "reserved",
            "storage_object_id": storage_object_id,
            "object_uri": object_uri,
            "content_type": content_type,
            "expires_at": payload.get("expires_at"),
        }

    return {
        "id": record.run_id,
        "run_id": record.run_id,
        "export_job_id": record.run_id,
        "run_type": record.run_type,
        "status": record.status,
        "format": payload.get("format", "jsonl"),
        "target": payload.get("target"),
        "object_id": payload.get("object_id"),
        "scope": payload.get("scope")
        or {
            "target": payload.get("target"),
            "object_id": payload.get("object_id"),
            "module_key": payload.get("module_key"),
            "active_tab": payload.get("active_tab"),
            "filter": payload.get("filter"),
        },
        "storage_object_id": storage_object_id,
        "download_ref": download_ref,
        "trace_id": record.trace_id,
        "dispatch": dispatch,
        "next_actions": payload.get("next_actions", []),
    }


def knowledge_qdrant_payload(
    ctx: ContextDep,
    source: dict[str, Any],
    *,
    index: dict[str, Any] | None = None,
    source_id: str | None = None,
    index_id: str | None = None,
) -> dict[str, Any]:
    authoritative_source_id = (
        source_id or source.get("knowledge_source_id") or source.get("source_id")
    )
    authoritative_index_id = (index_id if index is not None else None) or (
        index.get("knowledge_index_id") if index else None
    )
    collection = knowledge_qdrant_collection(source, index=index)
    version = (
        index.get("version")
        if index
        else source.get("version") or source.get("freshness") or "source-current"
    )
    asset_key = source.get("asset_key") or f"auris/knowledge/{authoritative_source_id}"
    embedding_text = "\n".join(
        str(value).strip()
        for value in (
            source.get("name"),
            source.get("description"),
            source.get("source_type"),
            source.get("connector_id"),
        )
        if value is not None and str(value).strip()
    )
    return {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "trace_id": ctx.trace_id,
        "collection": collection,
        "knowledge_index_id": authoritative_index_id,
        "knowledge_source_id": authoritative_source_id,
        "source_id": authoritative_source_id,
        "source_type": source.get("source_type"),
        "asset_key": asset_key,
        "version": version,
        "embedding_text": embedding_text,
        "business_ref": {
            "connector_id": source.get("connector_id"),
            "source_name": source.get("name"),
            "index_name": index.get("name") if index else None,
            "recall_strategy": index.get("recall_strategy") if index else None,
        },
    }


@router.get("/tenants")
def get_tenants(session: SessionDep, ctx: ContextDep):
    if "system" not in ctx.roles:
        tenant = session.get(Tenant, ctx.tenant_id)
        return collection_envelope([tenant.data] if tenant else [], ctx)
    tenants = [
        tenant.data for tenant in session.scalars(select(Tenant).order_by(Tenant.created_at))
    ]
    return collection_envelope(tenants, ctx)


@router.post("/tenants", status_code=201)
async def post_tenants(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("system",), action="tenants.create")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="tenants.create", body_hash=body_hash)
    if replay is not None:
        return replay
    body = await request.json()
    tenant_id = (
        body.get("tenant_id") or body.get("tenant_code") or body.get("name", "tenant").lower()
    )
    before_tenant = session.get(Tenant, tenant_id)
    before = dict(before_tenant.data) if before_tenant else None
    data = {**body, "tenant_id": tenant_id, "trace_id": ctx.trace_id}
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=body.get("tenant_code", tenant_id),
        name=body.get("name", tenant_id),
        status=body.get("status", "active"),
        data=data,
    )
    session.merge(tenant)
    record_audit(
        session,
        ctx,
        action="tenants.create",
        object_type="tenant",
        object_id=tenant_id,
        before=before,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="tenant.created",
        aggregate_type="tenant",
        aggregate_id=tenant_id,
        payload=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation="tenants.create",
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/tenants/{id}")
def get_tenants_by_id(id: str, session: SessionDep, ctx: ContextDep):
    if id != ctx.tenant_id and "system" not in ctx.roles:
        raise ApiError("FORBIDDEN", "当前上下文无权访问目标租户", 403)
    tenant = session.get(Tenant, id)
    if not tenant:
        raise ApiError("NOT_FOUND", f"租户不存在：{id}", 404)
    return envelope(tenant.data, ctx)


@router.patch("/tenants/{id}")
async def patch_tenants_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("system",), action="tenants.patch")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="tenants.patch", body_hash=body_hash)
    if replay is not None:
        return replay
    tenant = session.get(Tenant, id)
    body = await request.json()
    if tenant:
        before = dict(tenant.data)
        tenant.data = {**tenant.data, **body, "trace_id": ctx.trace_id}
        tenant.status = tenant.data.get("status", tenant.status)
        record_audit(
            session,
            ctx,
            action="tenants.patch",
            object_type="tenant",
            object_id=id,
            before=before,
            after=tenant.data,
        )
        enqueue_event(
            session,
            ctx,
            event_type="tenant.patched",
            aggregate_type="tenant",
            aggregate_id=id,
            payload=tenant.data,
        )
        response = envelope(tenant.data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation="tenants.patch",
            body_hash=body_hash,
            status_code=200,
            response_json=response,
        )
        session.commit()
        return response
    raise ApiError("NOT_FOUND", f"租户不存在：{id}", 404)


@router.get("/projects")
def get_projects(session: SessionDep, ctx: ContextDep):
    projects = [
        project.data
        for project in session.scalars(select(Project).where(Project.tenant_id == ctx.tenant_id))
        if "system" in ctx.roles or user_has_project_membership(project, ctx.user_id)
    ]
    return collection_envelope(projects, ctx)


@router.post("/projects", status_code=201)
async def post_projects(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), action="projects.create")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="projects.create", body_hash=body_hash)
    if replay is not None:
        return replay
    body = await request.json()
    project_id = body.get("project_id") or body.get("name", "project").lower().replace(" ", "_")
    before_project = session.get(Project, project_id)
    if before_project is not None:
        if before_project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
            raise ApiError("FORBIDDEN", "当前上下文无权覆盖其他租户项目", 403)
        raise ApiError(
            "PROJECT_ALREADY_EXISTS",
            "项目 ID 已存在，请切换项目后通过编辑接口修改",
            409,
        )
    requested_member_ids = body.get("member_user_ids")
    member_user_ids = (
        [
            ctx.user_id,
            *(
                value
                for value in requested_member_ids
                if isinstance(value, str) and value != ctx.user_id
            ),
        ]
        if isinstance(requested_member_ids, list)
        else [ctx.user_id]
    )
    requested_members = body.get("members")
    members = (
        [dict(member) for member in requested_members if isinstance(member, dict)]
        if isinstance(requested_members, list)
        else []
    )
    _require_unique_project_members(members)
    creator_member = next(
        (member for member in members if project_member_user_id(member) == ctx.user_id),
        None,
    )
    if creator_member is None:
        members.append({"user_id": ctx.user_id, "roles": list(ctx.roles)})
    else:
        requested_roles = creator_member.get("roles")
        creator_member["roles"] = list(
            dict.fromkeys(
                [
                    *(requested_roles if isinstance(requested_roles, list) else []),
                    *ctx.roles,
                ]
            )
        )
    _require_unique_project_members(members)
    data = {
        **body,
        "project_id": project_id,
        "tenant_id": ctx.tenant_id,
        "member_user_ids": member_user_ids,
        "members": members,
        "trace_id": ctx.trace_id,
    }
    project = Project(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        name=body.get("name", project_id),
        status=body.get("status", "active"),
        data=data,
    )
    session.add(project)
    record_audit(
        session,
        ctx,
        action="projects.create",
        object_type="project",
        object_id=project_id,
        before=None,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="project.created",
        aggregate_type="project",
        aggregate_id=project_id,
        payload=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation="projects.create",
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/projects/{id}")
def get_projects_by_id(id: str, session: SessionDep, ctx: ContextDep):
    if id != ctx.project_id and "system" not in ctx.roles:
        raise ApiError(
            "PROJECT_CONTEXT_MISMATCH",
            "目标项目与 X-Project-Id 上下文不一致，请先切换项目",
            403,
        )
    project = session.get(Project, id)
    if not project:
        raise ApiError("NOT_FOUND", f"项目不存在：{id}", 404)
    if project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
        raise ApiError("FORBIDDEN", "当前上下文无权访问目标项目", 403)
    return envelope(project.data, ctx)


@router.patch("/projects/{id}")
async def patch_projects_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), action="projects.patch")
    if id != ctx.project_id and "system" not in ctx.roles:
        raise ApiError(
            "PROJECT_CONTEXT_MISMATCH",
            "目标项目与 X-Project-Id 上下文不一致，请先切换项目",
            403,
        )
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="projects.patch", body_hash=body_hash)
    if replay is not None:
        return replay
    project = session.get(Project, id)
    body = await request.json()
    if project:
        if project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
            raise ApiError("FORBIDDEN", "当前上下文无权修改目标项目", 403)
        if "members" in body:
            _require_unique_project_members(body["members"])
        before = dict(project.data)
        project.data = {**project.data, **body, "trace_id": ctx.trace_id}
        project.status = project.data.get("status", project.status)
        record_audit(
            session,
            ctx,
            action="projects.patch",
            object_type="project",
            object_id=id,
            before=before,
            after=project.data,
        )
        enqueue_event(
            session,
            ctx,
            event_type="project.patched",
            aggregate_type="project",
            aggregate_id=id,
            payload=project.data,
        )
        response = envelope(project.data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation="projects.patch",
            body_hash=body_hash,
            status_code=200,
            response_json=response,
        )
        session.commit()
        return response
    raise ApiError("NOT_FOUND", f"项目不存在：{id}", 404)


@router.get("/connectors")
def get_connectors(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "connectors", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/connectors", status_code=201)
async def post_connectors(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "connectors",
        key_prefix="connector",
        status="draft",
    )


@router.patch("/connectors/{id}")
async def patch_connectors_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(session, ctx, request, "connectors", id)


@router.post("/platform-connections/{connection_id}/session", status_code=201)
async def post_platform_connection_session(
    connection_id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = await request.json()
    session_ref = f"platform_session:{connection_id}:{ctx.request_id}"
    return await upsert_idempotent_json_resource(
        session,
        ctx,
        request,
        "platform_sessions",
        session_ref,
        status="success",
        operation="platform_sessions.create",
        status_code=201,
        extra_data={
            "connection_id": connection_id,
            "session_ref": session_ref,
            "expires_in": 3600,
            "scope": body.get("scope", "current_project"),
        },
    )


@router.get("/data-sources/{source_id}/records")
def get_data_source_records(source_id: str, session: SessionDep, ctx: ContextDep):
    records = [
        item
        for item in list_resource_data(session, ctx, "data_source_records", limit=500)
        if item.get("source_id") == source_id
    ]
    return collection_envelope(records, ctx, total=len(records), limit=len(records))


@router.post("/audio-ingest/recordings", status_code=202)
async def post_audio_ingest_recordings(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="audio_ingest",
        event_type="audio_ingest.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/authenticated-events")
def get_authenticated_events(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "documents", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/platform-sync-jobs", status_code=202)
async def post_platform_sync_jobs(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="platform_sync",
        event_type="platform_sync.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/knowledge-sources")
def get_knowledge_sources(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "knowledge_sources", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.get("/knowledge-sources/{id}")
def get_knowledge_sources_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "knowledge_sources", id).data, ctx)


@router.post("/knowledge-sources/{id}/sync-runs", status_code=202)
async def post_knowledge_sources_by_id_sync_runs(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    source = get_resource(session, ctx, "knowledge_sources", id).data
    qdrant_payload = knowledge_qdrant_payload(ctx, source, source_id=id)
    body = qdrant_caller_fields(await request.json())
    return await create_run(
        session,
        ctx,
        request,
        run_type="knowledge_sync",
        event_type="knowledge_source.sync_requested",
        payload={
            **body,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "trace_id": ctx.trace_id,
            "knowledge_source_id": id,
            "source_id": id,
            "source_type": source.get("source_type"),
            "asset_key": qdrant_payload["asset_key"],
            "version": qdrant_payload["version"],
            "vector_collection": qdrant_payload["collection"],
            "qdrant_payload": qdrant_payload,
            "affected_objects": [{"type": "knowledge_source", "id": id}],
        },
        status="pending",
    )


@router.get("/knowledge-indexes")
def get_knowledge_indexes(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "knowledge_indexes", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.get("/knowledge-indexes/{id}")
def get_knowledge_indexes_by_id(id: str, session: SessionDep, ctx: ContextDep):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    gates = [
        item
        for item in list_resource_data(session, ctx, "knowledge_quality_gates", limit=200)
        if item.get("knowledge_index_id") == id
    ]
    effect = next(
        (
            item
            for item in list_resource_data(session, ctx, "knowledge_effects", limit=200)
            if item.get("knowledge_index_id") == id
        ),
        None,
    )
    return envelope({**index, "quality_gates": gates, "effect": effect}, ctx)


@router.post("/knowledge-indexes/{id}/build-runs", status_code=202)
async def post_knowledge_indexes_by_id_build_runs(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    source_id = str(index.get("source_id"))
    source = get_resource(session, ctx, "knowledge_sources", str(source_id)).data
    qdrant_payload = knowledge_qdrant_payload(
        ctx,
        source,
        index=index,
        source_id=source_id,
        index_id=id,
    )
    body = qdrant_caller_fields(
        parse_payload(KnowledgeBuildRequest, await request.json()).model_dump(exclude_none=True)
    )
    return await create_run(
        session,
        ctx,
        request,
        run_type="knowledge_build",
        event_type="knowledge_index.build_requested",
        payload={
            **body,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "trace_id": ctx.trace_id,
            "knowledge_index_id": id,
            "source_id": source_id,
            "knowledge_source_id": source_id,
            "source_type": source.get("source_type"),
            "asset_key": qdrant_payload["asset_key"],
            "version": qdrant_payload["version"],
            "vector_collection": qdrant_payload["collection"],
            "qdrant_payload": qdrant_payload,
            "affected_objects": [{"type": "knowledge_index", "id": id}],
        },
        status="pending",
    )


@router.post("/knowledge-indexes/{id}/recall")
async def post_knowledge_indexes_by_id_recall(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    source_id = str(index.get("source_id"))
    source = get_resource(session, ctx, "knowledge_sources", str(source_id)).data
    qdrant_payload = knowledge_qdrant_payload(
        ctx,
        source,
        index=index,
        source_id=source_id,
        index_id=id,
    )
    body = parse_payload(KnowledgeRecallRequest, await request.json())
    for field_name, expected in (("tenant_id", ctx.tenant_id), ("project_id", ctx.project_id)):
        requested = body.scope.get(field_name)
        if requested is not None and requested != expected:
            raise ApiError(
                "KNOWLEDGE_RECALL_SCOPE_FORBIDDEN",
                f"知识召回不能覆盖当前 {field_name}",
                403,
            )
    result = recall_knowledge_index(
        session,
        ctx,
        knowledge_index_id=id,
        qdrant_payload=qdrant_payload,
        query=body.query,
        top_k=body.top_k,
    )
    return envelope(result, ctx)


@router.get("/knowledge-indexes/{id}/quality-gates")
def get_knowledge_indexes_by_id_quality_gates(
    id: str, session: SessionDep, ctx: ContextDep, page: PaginationDep
):
    get_resource(session, ctx, "knowledge_indexes", id)
    items = [
        item
        for item in list_resource_data(session, ctx, "knowledge_quality_gates", limit=200)
        if item.get("knowledge_index_id") == id
    ]
    return collection_envelope(
        items[: page_limit(page)],
        ctx,
        limit=page_limit(page),
        meta={"status_counts": status_counts(items)},
    )


@router.get("/knowledge-indexes/{id}/effects")
def get_knowledge_indexes_by_id_effects(id: str, session: SessionDep, ctx: ContextDep):
    get_resource(session, ctx, "knowledge_indexes", id)
    effect = next(
        (
            item
            for item in list_resource_data(session, ctx, "knowledge_effects", limit=200)
            if item.get("knowledge_index_id") == id
        ),
        None,
    )
    return envelope(effect or {"knowledge_index_id": id, "status": "empty"}, ctx)


@router.get("/settings")
def get_settings_list(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "settings", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.get("/settings/{id}")
def get_settings_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "settings", id).data, ctx)


@router.patch("/settings/{id}")
async def patch_settings_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(
        session,
        ctx,
        request,
        "settings",
        id,
        status="draft",
        operation=f"settings.patch:{id}",
    )


@router.post("/settings/drafts", status_code=201)
async def post_settings_drafts(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "settings_drafts",
        key_prefix="settings_draft",
        status="draft",
    )


@router.get("/settings/drafts/{id}")
def get_settings_drafts_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "settings_drafts", id).data, ctx)


@router.post("/settings/publish-requests", status_code=202)
async def post_settings_publish_requests(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), "settings.publish")
    body = await request.json()
    return await create_run(
        session,
        ctx,
        request,
        run_type="settings_publish",
        event_type="settings.publish_requested",
        payload=body,
        status="blocked",
        prepare_payload=lambda payload: prepare_settings_publish(session, ctx, payload),
    )


@router.post("/settings/provider-tests", status_code=202)
async def post_settings_provider_tests(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="provider_test",
        event_type="provider_test.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/output-sinks/platform-callbacks")
def get_platform_callbacks(ctx: ContextDep):
    return collection_envelope(
        [
            {
                "id": "callback_001",
                "status": "pending",
                "target": "crm_reception_order",
                "trace_id": ctx.trace_id,
            }
        ],
        ctx,
    )


@router.post("/output-sinks/platform-callbacks", status_code=202)
async def post_platform_callbacks(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(
        ctx, ("project_admin", "asset_manager"), "output_sinks.platform_callbacks.create"
    )
    body = parse_payload(ExternalCallbackRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await create_run(
        session,
        ctx,
        request,
        run_type="external_callback",
        event_type="external_callback.requested",
        payload=body,
        status="pending",
    )


@router.post("/output-sinks/platform-callbacks/{id}/completion-receipts")
async def post_platform_callbacks_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(session, ctx, request, id, body)


@router.post("/runs/{id}/completion-receipts")
async def post_runs_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(session, ctx, request, id, body)


@router.get("/runs/{id}")
def get_runs_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_run(session, ctx, id), ctx)


@router.post("/runs/{id}/decisions")
async def post_runs_by_id_decisions(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunReleaseDecisionRequest, await request.json()).model_dump()
    return await decide_release_gate(session, ctx, request, id, body)


@router.post("/runs/{id}/retries", status_code=202)
async def post_runs_by_id_retries(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(TaskRunRetryRequest, await request.json()).model_dump(exclude_none=True)
    return await retry_run(session, ctx, request, id, body)


@router.post("/runs/{id}/external-completion-receipts")
async def post_runs_by_id_external_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: SignedCompletionContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(
        session,
        ctx,
        request,
        id,
        body,
        strict_external_receipt=True,
        completion_auth=getattr(request.state, "completion_signature", None),
    )


@router.get("/work-items")
def get_work_items(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "work_items", page, status=status)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/work-items", status_code=201)
async def post_work_items(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "work_items",
        key_prefix="work_item",
        status="draft",
    )


@router.get("/work-items/{id}")
def get_work_items_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "work_items", id).data, ctx)


@router.patch("/work-items/{id}")
async def patch_work_items_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(session, ctx, request, "work_items", id)


@router.post("/exports", status_code=202)
async def post_exports(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), "exports.create")
    return await create_run(
        session,
        ctx,
        request,
        run_type="export",
        event_type="export.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/exports/{id}")
def get_exports_by_id(id: str, session: SessionDep, ctx: ContextDep):
    record = session.get(RunRecord, id)
    if (
        not record
        or record.run_type != "export"
        or record.tenant_id != ctx.tenant_id
        or record.project_id != ctx.project_id
    ):
        raise ApiError("NOT_FOUND", f"导出任务不存在：{id}", 404)
    return envelope(export_job_payload(record), ctx)


@router.post("/exports/{id}/completion-receipts")
async def post_exports_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(
        session, ctx, request, id, body, response_data=export_job_payload
    )
