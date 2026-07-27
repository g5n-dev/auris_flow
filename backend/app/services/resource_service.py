from __future__ import annotations

import hashlib
import json
import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.request_identifiers import server_generated_public_id
from app.core.response import envelope
from app.models import (
    AudioRecording,
    Badcase,
    EvalDatasetVersion,
    HotwordMetricSnapshot,
    HotwordPack,
    HotwordPackVersion,
    HotwordVersionItem,
    JsonResource,
    Project,
    ProjectSceneProfileBinding,
    RunRecord,
    SceneProfile,
    SceneProfileVersion,
    StorageObject,
    Tenant,
    User,
)
from app.repositories import JsonResourceRepository
from app.schemas.common import FlexiblePayload, parse_payload
from app.schemas.scene_profiles import SceneProfileManifest
from app.services.audit_service import record_audit
from app.services.data_asset_materialization_service import seed_asset_materialization_projection
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.knowledge_projection_service import sync_knowledge_projection
from app.services.label_review_projection_service import sync_label_review_projection
from app.services.outbox_service import enqueue_event
from app.services.read_policy_service import (
    can_read_human_review_task,
    require_resource_read,
    resource_read_scope,
)

ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = ROOT / "doc" / "backend-spec" / "seed-fixture-v0.1.json"

RESOURCE_WRITE_ROLE_POLICY: dict[str, tuple[str, ...]] = {
    "connectors": ("project_admin", "asset_manager"),
    "event_links": ("project_admin", "asset_manager", "review_arbitrator"),
    "eval_datasets": ("project_admin", "model_engineer"),
    "evidence_packs": ("project_admin", "asset_manager", "review_arbitrator"),
    "human_review_tasks": ("project_admin", "review_arbitrator"),
    "label_versions": ("project_admin", "model_engineer", "review_arbitrator"),
    "listening_annotations": ("project_admin", "asset_manager", "review_arbitrator"),
    "platform_sessions": ("project_admin", "asset_manager"),
    "data_aggregation_views": ("project_admin", "asset_manager", "review_arbitrator"),
    "conversation_boundaries": ("project_admin", "asset_manager", "review_arbitrator"),
    "settings": ("project_admin", "model_engineer"),
    "settings_drafts": ("project_admin", "model_engineer"),
    "task_versions": ("project_admin", "model_engineer"),
    "voiceprint_enrollments": ("project_admin", "asset_manager", "review_arbitrator"),
    "work_items": ("project_admin", "asset_manager", "review_arbitrator"),
}


@dataclass(frozen=True)
class ResourcePage:
    items: list[dict[str, Any]]
    total: int
    limit: int
    next_cursor: str | None


def encode_cursor(resource_id: int) -> str:
    token = f"json_resource:{resource_id}".encode()
    return urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | int | None) -> int:
    if cursor in (None, "", 0):
        return 0
    if isinstance(cursor, int):
        if cursor < 0:
            raise ApiError("INVALID_CURSOR", "cursor 不能为负数", 400)
        return cursor
    cursor_text = str(cursor)
    if cursor_text.isdigit():
        return int(cursor_text)
    try:
        padded = cursor_text + "=" * (-len(cursor_text) % 4)
        raw = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None
    prefix, _, value = raw.partition(":")
    if prefix != "json_resource" or not value.isdigit():
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400)
    return int(value)


def page_limit(page: dict[str, str | int | None]) -> int:
    return int(page.get("limit") or 50)


def load_seed_file(path: Path = SEED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def decode_asset_key(asset_key: str) -> str:
    return unquote(asset_key)


def status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or item.get("human_state") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def request_trace_fields(ctx: RequestContext) -> dict[str, str]:
    fields = {
        "trace_id": ctx.trace_id,
        "root_trace_id": ctx.trace_id,
        "correlation_id": ctx.correlation_id or ctx.trace_id,
    }
    if ctx.parent_trace_id:
        fields["parent_trace_id"] = ctx.parent_trace_id
    return fields


def list_resources(
    session: Session,
    ctx: RequestContext,
    collection: str,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: int | str | None = 0,
) -> list[JsonResource]:
    require_resource_read(ctx, collection)
    read_scope = resource_read_scope(ctx, collection)
    return JsonResourceRepository(session).list(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection=collection,
        status=status,
        cursor=decode_cursor(cursor),
        limit=limit,
        read_scope=read_scope,
    )


def list_resource_data(
    session: Session,
    ctx: RequestContext,
    collection: str,
    *,
    status: str | None = None,
    limit: int = 50,
    cursor: int | str | None = 0,
) -> list[dict[str, Any]]:
    return [
        resource.data
        for resource in list_resources(
            session, ctx, collection, status=status, limit=limit, cursor=cursor
        )
    ]


def list_resource_page(
    session: Session,
    ctx: RequestContext,
    collection: str,
    page: dict[str, str | int | None],
    *,
    status: str | None = None,
) -> ResourcePage:
    require_resource_read(ctx, collection)
    read_scope = resource_read_scope(ctx, collection)
    limit = page_limit(page)
    cursor_id = decode_cursor(page.get("cursor"))
    repo = JsonResourceRepository(session)
    resources = repo.list(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection=collection,
        status=status,
        cursor=cursor_id,
        limit=limit + 1,
        read_scope=read_scope,
    )
    visible = resources[:limit]
    next_cursor = encode_cursor(visible[-1].id) if len(resources) > limit and visible else None
    return ResourcePage(
        items=[resource.data for resource in visible],
        total=repo.count(
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            collection=collection,
            status=status,
            read_scope=read_scope,
        ),
        limit=limit,
        next_cursor=next_cursor,
    )


def get_resource(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
) -> JsonResource:
    require_resource_read(ctx, collection)
    key = decode_asset_key(resource_key) if collection == "data_assets" else resource_key
    resource = JsonResourceRepository(session).find(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection=collection,
        resource_key=key,
    )
    if not resource:
        raise ApiError("NOT_FOUND", f"{collection} 不存在：{key}", 404)
    if collection == "human_review_tasks" and not can_read_human_review_task(resource.data, ctx):
        raise ApiError(
            "HUMAN_REVIEW_TASK_FORBIDDEN",
            "当前用户无权读取该人审任务",
            403,
        )
    return resource


def upsert_resource(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
    data: dict[str, Any],
    *,
    status: str | None = None,
    trace_id: str | None = None,
    audit_action: str | None = None,
) -> JsonResource:
    current = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == collection,
            JsonResource.resource_key == resource_key,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    if current:
        before = dict(current.data)
        current.data = data
        current.status = status or data.get("status") or current.status
        current.trace_id = trace_id or data.get("trace_id") or current.trace_id
        after = data
        target = current
    else:
        before = None
        target = JsonResource(
            collection=collection,
            resource_key=resource_key,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status=status or data.get("status"),
            trace_id=trace_id or data.get("trace_id"),
            data=data,
        )
        session.add(target)
        after = data
    sync_label_review_projection(
        session,
        ctx,
        collection,
        resource_key,
        data,
        status=target.status,
        trace_id=target.trace_id,
    )
    sync_knowledge_projection(
        session,
        ctx,
        collection,
        resource_key,
        data,
        status=target.status,
        trace_id=target.trace_id,
    )
    if audit_action:
        record_audit(
            session,
            ctx,
            action=audit_action,
            object_type=collection,
            object_id=resource_key,
            before=before,
            after=after,
        )
    return target


def create_json_resource(
    session: Session,
    ctx: RequestContext,
    collection: str,
    payload: dict[str, Any],
    *,
    key_prefix: str,
    status: str = "pending",
) -> dict[str, Any]:
    resource_key = (
        payload.get("id")
        or payload.get(f"{key_prefix}_id")
        or server_generated_public_id(key_prefix, suffix_length=12)
    )
    data = {
        "id": resource_key,
        **payload,
        "status": payload.get("status", status),
        **request_trace_fields(ctx),
    }
    upsert_resource(
        session,
        ctx,
        collection,
        resource_key,
        data,
        status=data["status"],
        trace_id=ctx.trace_id,
        audit_action=f"{collection}.create",
    )
    enqueue_event(
        session,
        ctx,
        event_type=f"{collection}.created",
        aggregate_type=collection,
        aggregate_id=resource_key,
        payload=data,
    )
    return data


async def create_idempotent_json_resource(
    session: Session,
    ctx: RequestContext,
    request: Request,
    collection: str,
    *,
    key_prefix: str,
    status: str = "pending",
    operation: str | None = None,
    status_code: int = 201,
    body_model: type[FlexiblePayload] | None = None,
    reject_existing: bool = False,
    prepare_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        RESOURCE_WRITE_ROLE_POLICY.get(collection, ("project_admin",)),
        action=f"{collection}.create",
    )
    body_hash = await request_hash(request)
    operation_key = operation or f"{collection}.create"
    replay = replay_or_conflict(session, ctx, operation=operation_key, body_hash=body_hash)
    if replay is not None:
        return replay

    raw_body = await request.json()
    body = (
        parse_payload(body_model, raw_body).model_dump(exclude_none=True)
        if body_model
        else raw_body
    )
    if prepare_payload is not None:
        body = prepare_payload(body)
    requested_key = body.get("id") or body.get(f"{key_prefix}_id")
    if reject_existing and requested_key:
        existing = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection == collection,
                JsonResource.resource_key == str(requested_key),
            )
        )
        if existing is not None:
            raise ApiError(
                "RESOURCE_ALREADY_EXISTS",
                f"{collection} ID 已存在，不能通过创建接口覆盖",
                409,
            )
    data = create_json_resource(
        session, ctx, collection, body, key_prefix=key_prefix, status=status
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation_key,
        body_hash=body_hash,
        status_code=status_code,
        response_json=response,
    )
    session.commit()
    return response


async def patch_idempotent_json_resource(
    session: Session,
    ctx: RequestContext,
    request: Request,
    collection: str,
    resource_key: str,
    *,
    status: str | None = None,
    operation: str | None = None,
    body_model: type[FlexiblePayload] | None = None,
    prepare_payload: (Callable[[JsonResource, dict[str, Any]], dict[str, Any]] | None) = None,
    preserve_root_trace_id: bool = False,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        RESOURCE_WRITE_ROLE_POLICY.get(collection, ("project_admin",)),
        action=f"{collection}.patch",
    )
    body_hash = await request_hash(request)
    operation_key = operation or f"{collection}.patch"
    replay = replay_or_conflict(session, ctx, operation=operation_key, body_hash=body_hash)
    if replay is not None:
        return replay

    resource = get_resource(session, ctx, collection, resource_key)
    before = dict(resource.data)
    raw_body = await request.json()
    body = (
        parse_payload(body_model, raw_body).model_dump(exclude_none=True)
        if body_model
        else raw_body
    )
    if prepare_payload is not None:
        body = prepare_payload(resource, body)
    trace_fields = request_trace_fields(ctx)
    existing_root_trace_id = resource.data.get("root_trace_id")
    if preserve_root_trace_id and isinstance(existing_root_trace_id, str):
        trace_fields["root_trace_id"] = existing_root_trace_id
    resource.data = {**resource.data, **body, **trace_fields}
    if status:
        resource.data["status"] = status
    resource.status = resource.data.get("status", resource.status)
    sync_label_review_projection(
        session,
        ctx,
        collection,
        resource_key,
        resource.data,
        status=resource.status,
        trace_id=ctx.trace_id,
    )
    record_audit(
        session,
        ctx,
        action=f"{collection}.patch",
        object_type=collection,
        object_id=resource_key,
        before=before,
        after=resource.data,
    )
    enqueue_event(
        session,
        ctx,
        event_type=f"{collection}.patched",
        aggregate_type=collection,
        aggregate_id=resource_key,
        payload=resource.data,
    )
    response = envelope(resource.data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation_key,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response


async def upsert_idempotent_json_resource(
    session: Session,
    ctx: RequestContext,
    request: Request,
    collection: str,
    resource_key: str,
    *,
    status: str | None = None,
    operation: str | None = None,
    status_code: int = 200,
    extra_data: dict[str, Any] | None = None,
    after_upsert: Callable[[dict[str, Any]], object] | None = None,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        RESOURCE_WRITE_ROLE_POLICY.get(collection, ("project_admin",)),
        action=f"{collection}.upsert",
    )
    body_hash = await request_hash(request)
    operation_key = operation or f"{collection}.upsert"
    replay = replay_or_conflict(session, ctx, operation=operation_key, body_hash=body_hash)
    if replay is not None:
        return replay

    current = session.scalar(
        select(JsonResource).where(
            JsonResource.collection == collection,
            JsonResource.resource_key == resource_key,
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
        )
    )
    body = await request.json()
    data = {
        "id": resource_key,
        **(current.data if current else {}),
        **body,
        **(extra_data or {}),
        "status": status or body.get("status") or (current.status if current else "success"),
        **request_trace_fields(ctx),
    }
    upsert_resource(
        session,
        ctx,
        collection,
        resource_key,
        data,
        status=data["status"],
        trace_id=ctx.trace_id,
        audit_action=f"{collection}.upsert",
    )
    if after_upsert:
        after_upsert(data)
    enqueue_event(
        session,
        ctx,
        event_type=f"{collection}.upserted",
        aggregate_type=collection,
        aggregate_id=resource_key,
        payload=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation_key,
        body_hash=body_hash,
        status_code=status_code,
        response_json=response,
    )
    session.commit()
    return response


def seed_database(session: Session, seed: dict[str, Any]) -> None:
    tenant = seed["context"]["tenant"]
    project = seed["context"]["project"]
    users = seed["context"]["users"]
    project_data = {
        **project,
        "member_user_ids": [user["user_id"] for user in users],
        "members": [
            {
                "user_id": user["user_id"],
                "roles": user.get("roles", []),
            }
            for user in users
        ],
    }
    session.merge(
        Tenant(
            tenant_id=tenant["tenant_id"],
            tenant_code=tenant["tenant_code"],
            name=tenant["name"],
            status=tenant["status"],
            data=tenant,
        )
    )
    session.merge(
        Project(
            project_id=project["project_id"],
            tenant_id=project["tenant_id"],
            name=project["name"],
            status=project["status"],
            data=project_data,
        )
    )
    for user in users:
        session.merge(
            User(
                user_id=user["user_id"],
                tenant_id=tenant["tenant_id"],
                email=user["email"],
                name=user["name"],
                roles=user["roles"],
                data=user,
            )
        )

    ctx = RequestContext(
        tenant_id=tenant["tenant_id"],
        project_id=project["project_id"],
        user_id="seed",
        roles=("system",),
        request_id="seed",
        trace_id="trace_seed",
    )

    scene_seed = seed.get("scene_profiles") or {}
    scene_manifests: dict[str, str] = {}
    for item in scene_seed.get("profiles", []):
        session.merge(
            SceneProfile(
                scene_profile_id=item["scene_profile_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                scene_key=item["scene_key"],
                name=item["name"],
                description=item["description"],
                status=item.get("status", "draft"),
                current_published_version_id=item.get("current_published_version_id"),
                created_by=item.get("created_by", "seed"),
                trace_id=item.get("trace_id", ctx.trace_id),
            )
        )
    session.flush()
    for item in scene_seed.get("versions", []):
        manifest = SceneProfileManifest.model_validate(item["manifest"]).model_dump(mode="json")
        manifest_sha256 = hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        scene_manifests[item["scene_profile_version_id"]] = manifest_sha256
        session.merge(
            SceneProfileVersion(
                scene_profile_version_id=item["scene_profile_version_id"],
                scene_profile_id=item["scene_profile_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                version=item["version"],
                status=item.get("status", "draft"),
                source_type=item.get("source_type", "human"),
                schema_version=manifest["schema_version"],
                parent_version_id=item.get("parent_version_id"),
                generated_by_run_id=item.get("generated_by_run_id"),
                requested_by=item.get("requested_by", "seed"),
                reviewed_by=item.get("reviewed_by"),
                published_by=item.get("published_by"),
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                validation_report={
                    "status": "pass",
                    "manifest_sha256": manifest_sha256,
                    "seeded": True,
                },
                review_record={"seeded": True},
                resource_version=int(item.get("resource_version", 1)),
                trace_id=item.get("trace_id", ctx.trace_id),
            )
        )
    session.flush()
    for item in scene_seed.get("bindings", []):
        manifest_sha256 = scene_manifests[item["scene_profile_version_id"]]
        session.merge(
            ProjectSceneProfileBinding(
                binding_id=item["binding_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                environment=item.get("environment", "production"),
                scene_profile_id=item["scene_profile_id"],
                scene_profile_version_id=item["scene_profile_version_id"],
                manifest_sha256=manifest_sha256,
                status=item.get("status", "active"),
                bound_by=item.get("bound_by", "seed"),
                resource_version=int(item.get("resource_version", 1)),
                trace_id=item.get("trace_id", ctx.trace_id),
            )
        )
        if item.get("environment", "production") == "production":
            project_row = session.get(Project, ctx.project_id)
            if project_row is not None:
                project_row.data = {
                    **project_row.data,
                    "scene_profile_id": item["scene_profile_id"],
                    "scene_profile_version_id": item["scene_profile_version_id"],
                    "scene_profile_snapshot_sha256": manifest_sha256,
                    "scene_profile_environment": "production",
                }
    session.flush()

    hotword_seed = seed.get("hotword_governance") or {}
    for item in hotword_seed.get("hotword_packs", []):
        session.merge(
            HotwordPack(
                pack_id=item["pack_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                name=item["name"],
                language=item["language"],
                domain=item["domain"],
                status=item.get("status", "active"),
                current_version_id=item.get("current_version_id"),
                production_version_id=item.get("production_version_id"),
                resource_version=int(item.get("resource_version", 1)),
                root_trace_id=item["root_trace_id"],
                current_trace_id=item.get("current_trace_id") or item["root_trace_id"],
            )
        )
    session.flush()
    for item in hotword_seed.get("storage_objects", []):
        object_key = str(item["object_key"])
        session.merge(
            StorageObject(
                storage_object_id=item["storage_object_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                provider=item["provider"],
                bucket=item["bucket"],
                object_key=object_key,
                object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                source_type=item["source_type"],
                source_id=item["source_id"],
                content_type=item["content_type"],
                size_bytes=int(item["size_bytes"]),
                content_sha256=item["content_sha256"],
                etag=item.get("etag"),
                status=item.get("status", "verified"),
                trace_id=item.get("trace_id") or ctx.trace_id,
                payload={"seeded": True},
            )
        )
    session.flush()
    for item in hotword_seed.get("hotword_pack_versions", []):
        session.merge(
            HotwordPackVersion(
                version_id=item["version_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                pack_id=item["pack_id"],
                version=item["version"],
                baseline_version_id=item.get("baseline_version_id"),
                status=item.get("status", "draft"),
                content_sha256=item.get("content_sha256"),
                manifest_storage_object_id=item.get("manifest_storage_object_id"),
                eval_run_id=item.get("eval_run_id"),
                eval_locked=bool(item.get("eval_locked", False)),
                model_approved_by=item.get("model_approved_by"),
                project_admin_confirmed_by=item.get("project_admin_confirmed_by"),
                provider_artifact_ref=item.get("provider_artifact_ref"),
                compiled_provider=item.get("compiled_provider"),
                resource_version=int(item.get("resource_version", 1)),
                root_trace_id=item["root_trace_id"],
                current_trace_id=item.get("current_trace_id") or item["root_trace_id"],
                published_at=(
                    datetime.fromisoformat(item["published_at"])
                    if item.get("published_at")
                    else None
                ),
                payload={
                    "seeded": True,
                    "legacy_import": bool(item.get("legacy_import", True)),
                    "artifact_sha256": item.get("artifact_sha256"),
                    "task_version_id": item.get("task_version_id"),
                    "production_active": bool(item.get("production_active", False)),
                    "production_task_version_id": item.get("task_version_id")
                    if item.get("production_active")
                    else None,
                },
            )
        )
    session.flush()
    for item in seed.get("review_and_feedback", {}).get("badcases", []):
        if item.get("capability") != "asr-hotword":
            continue
        root_trace_id = item.get("root_trace_id") or item.get("trace_id") or ctx.trace_id
        session.merge(
            Badcase(
                badcase_id=item["badcase_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status=item.get("status", "pending-attribution"),
                trace_id=item.get("current_trace_id") or root_trace_id,
                payload={
                    "source_evidence_pack_id": item.get("source_evidence_pack_id"),
                    "business_weight": item.get("business_weight", 1.0),
                    "decision_history": [],
                },
                capability=item.get("capability"),
                error_type=item.get("error_type"),
                standard_term=item.get("standard_term"),
                recognized_text=item.get("recognized_text"),
                evidence_ref=item.get("evidence_ref"),
                evidence_storage_object_id=item.get("evidence_storage_object_id"),
                evidence_level=item.get("evidence_level"),
                hotword_pack_version_id=item["hotword_pack_version_id"],
                expected_count=int(item.get("expected_count", 0)),
                correct_count=int(item.get("correct_count", 0)),
                weighted_error_count=float(item.get("weighted_error_count", 0)),
                manual_correction_count=int(item.get("manual_correction_count", 0)),
                priority_score=float(item.get("priority_score", 0)),
                candidate_state=item.get("candidate_state", "suspected"),
                root_cause=item.get("root_cause"),
                fix_suggestion=item.get("fix_suggestion"),
                downstream_impact=item.get("downstream_impact") or {},
                resource_version=int(item.get("resource_version", 1)),
                root_trace_id=root_trace_id,
                current_trace_id=item.get("current_trace_id") or root_trace_id,
            )
        )
    session.flush()
    for item in hotword_seed.get("hotword_version_items", []):
        session.merge(
            HotwordVersionItem(
                item_id=item["item_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                version_id=item["version_id"],
                canonical_term=item["canonical_term"],
                normalized_term=item["normalized_term"],
                aliases=item.get("aliases") or [],
                category=item["category"],
                weight=int(item["weight"]),
                source_badcase_id=item.get("source_badcase_id"),
                source_type=item.get("source_type", "manual"),
                resource_version=int(item.get("resource_version", 1)),
                root_trace_id=item["root_trace_id"],
                current_trace_id=item.get("current_trace_id") or item["root_trace_id"],
            )
        )
    session.flush()
    for item in hotword_seed.get("hotword_metric_snapshots", []):
        session.merge(
            HotwordMetricSnapshot(
                snapshot_id=item["snapshot_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                bucket_start=datetime.fromisoformat(item["bucket_start"]),
                bucket_end=datetime.fromisoformat(item["bucket_end"]),
                store_id=item.get("store_id"),
                provider=item.get("provider"),
                model_version=item.get("model_version"),
                hotword_pack_version_id=item.get("hotword_pack_version_id"),
                standard_term=item.get("standard_term"),
                expected_count=int(item.get("expected_count", 0)),
                correct_count=int(item.get("correct_count", 0)),
                weighted_error_count=float(item.get("weighted_error_count", 0)),
                false_insert_count=int(item.get("false_insert_count", 0)),
                recognized_hotword_count=int(item.get("recognized_hotword_count", 0)),
                impacted_session_count=int(item.get("impacted_session_count", 0)),
                evidence_confidence=float(item.get("evidence_confidence", 0)),
                root_trace_id=item["root_trace_id"],
                payload={
                    "source_run_id": item.get("source_run_id"),
                    "priority_score": item.get("priority_score"),
                    "suspected": item.get("suspected"),
                },
            )
        )
    session.flush()
    for run in hotword_seed.get("analysis_runs", []):
        session.merge(
            RunRecord(
                run_id=run["run_id"],
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                run_type=run.get("run_type", "hotword_analysis"),
                status=run.get("status", "success"),
                run_key=run.get("run_key") or f"hotword-analysis:seed:{run['run_id']}",
                partition_key=f"{ctx.tenant_id}/{ctx.project_id}",
                trace_id=run.get("root_trace_id") or ctx.trace_id,
                payload={**run, "seeded": True},
            )
        )
    session.flush()

    seeded_recordings: list[dict[str, Any]] = []
    for source_recording in seed["audio_evidence"]["recordings"]:
        recording = dict(source_recording)
        recording_id = str(recording["recording_id"])
        object_key = str(
            recording.get("object_key")
            or recording.get("audio_url_ref")
            or recording.get("source_url_ref")
            or ""
        ).strip("/")
        storage_object_id = str(recording.get("storage_object_id") or f"sto_{recording_id}")
        storage_payload = {
            "storage_object_id": storage_object_id,
            "provider": str(os.environ.get("OBJECT_STORAGE_PROVIDER") or "minio"),
            "bucket": str(os.environ.get("OBJECT_STORAGE_BUCKET") or "auris-flow-local"),
            "object_key": object_key,
            "content_type": str(recording.get("content_type") or "audio/wav"),
            "content_length": recording.get("content_length"),
            "checksum_sha256": recording.get("checksum_sha256"),
            "etag": recording.get("etag"),
            "status": "reference",
            "source_type": "audio_recording",
            "source_id": recording_id,
        }
        recording["storage_object_id"] = storage_object_id
        recording["storage_object"] = storage_payload
        seeded_recordings.append(recording)
        session.merge(
            AudioRecording(
                recording_id=recording_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status=str(recording.get("status") or "registered"),
                trace_id=recording.get("trace_id") or ctx.trace_id,
                payload=recording,
            )
        )
        if object_key:
            session.merge(
                StorageObject(
                    storage_object_id=storage_object_id,
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    provider=storage_payload["provider"],
                    bucket=storage_payload["bucket"],
                    object_key=object_key,
                    object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
                    source_type="audio_recording",
                    source_id=recording_id,
                    content_type=storage_payload["content_type"],
                    size_bytes=storage_payload["content_length"],
                    content_sha256=storage_payload["checksum_sha256"],
                    etag=storage_payload["etag"],
                    status="reference",
                    trace_id=recording.get("trace_id") or ctx.trace_id,
                    payload=storage_payload,
                )
            )

    collection_specs: list[tuple[str, str, list[dict[str, Any]]]] = [
        ("stores", "store_id", seed["context"]["stores"]),
        ("connectors", "connector_id", seed.get("connectors", [])),
        ("task_types", "task_type_id", seed["tasking"]["task_types"]),
        ("task_versions", "task_version_id", seed["tasking"]["task_versions"]),
        ("audio_sessions", "audio_session_id", seed["audio_evidence"]["audio_sessions"]),
        ("recordings", "recording_id", seeded_recordings),
        ("conversation_boundaries", "boundary_id", seed["audio_evidence"]["boundaries"]),
        ("evidence_packs", "evidence_pack_id", seed["audio_evidence"]["evidence_packs"]),
        ("asr_segments", "segment_id", seed["audio_evidence"]["asr_segments"]),
        ("documents", "document_id", seed["business_events"]["documents"]),
        ("event_links", "id", seed["business_events"]["event_links"]),
        ("taxonomies", "taxonomy_id", seed["labeling"]["taxonomies"]),
        ("label_versions", "label_version_id", seed["labeling"]["label_versions"]),
        ("prompt_versions", "prompt_version_id", seed["labeling"]["prompt_versions"]),
        ("label_candidates", "candidate_id", seed["labeling"]["label_candidates"]),
        (
            "label_optimization_runs",
            "optimization_run_id",
            seed["labeling"]["label_optimization_runs"],
        ),
        ("human_review_tasks", "id", seed["review_and_feedback"]["human_review_tasks"]),
        ("badcases", "badcase_id", seed["review_and_feedback"]["badcases"]),
        ("eval_datasets", "dataset_id", seed["evaluation"]["eval_datasets"]),
        ("eval_runs", "eval_run_id", seed["evaluation"]["eval_runs"]),
        ("data_assets", "asset_key", seed["data_assets"]),
        ("knowledge_sources", "knowledge_source_id", seed["knowledge"]["sources"]),
        ("knowledge_indexes", "knowledge_index_id", seed["knowledge"]["indexes"]),
        ("knowledge_quality_gates", "gate_id", seed["knowledge"]["quality_gates"]),
        ("knowledge_effects", "effect_id", seed["knowledge"]["effects"]),
        ("settings", "setting_id", seed["settings"]),
        ("insight_reports", "report_id", seed["insights"]["reports"]),
    ]
    for collection, key_field, items in collection_specs:
        for item in items:
            resource_item = dict(item)
            if collection == "task_versions":
                project_row = session.get(Project, ctx.project_id)
                if project_row is not None:
                    resource_item.update(
                        {
                            "scene_profile_id": project_row.data.get("scene_profile_id"),
                            "scene_profile_version_id": project_row.data.get(
                                "scene_profile_version_id"
                            ),
                            "scene_profile_snapshot_sha256": project_row.data.get(
                                "scene_profile_snapshot_sha256"
                            ),
                        }
                    )
            upsert_resource(
                session,
                ctx,
                collection,
                resource_item[key_field],
                resource_item,
                status=resource_item.get("status") or resource_item.get("human_state"),
                trace_id=resource_item.get("trace_id"),
            )
            if collection == "data_assets":
                seed_asset_materialization_projection(session, ctx, resource_item)
            if collection == "eval_datasets" and resource_item.get("manifest_storage_object_id"):
                snapshot_document = {
                    "eval_dataset_id": resource_item[key_field],
                    "name": resource_item["name"],
                    "capability": resource_item.get("capability") or resource_item.get("type"),
                    "dataset_version": resource_item["dataset_version"],
                    "manifest_storage_object_id": resource_item["manifest_storage_object_id"],
                    "manifest_sha256": resource_item["manifest_sha256"],
                    "sample_count": int(resource_item["sample_count"]),
                }
                snapshot_sha256 = hashlib.sha256(
                    json.dumps(
                        snapshot_document,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                session.merge(
                    EvalDatasetVersion(
                        eval_dataset_id=item[key_field],
                        tenant_id=ctx.tenant_id,
                        project_id=ctx.project_id,
                        name=item["name"],
                        capability=snapshot_document["capability"],
                        dataset_version=item["dataset_version"],
                        status=item.get("status", "draft"),
                        manifest_storage_object_id=item["manifest_storage_object_id"],
                        manifest_sha256=item["manifest_sha256"],
                        sample_count=int(item["sample_count"]),
                        resource_version=int(item.get("resource_version", 1)),
                        root_trace_id=item.get("root_trace_id") or ctx.trace_id,
                        current_trace_id=item.get("trace_id")
                        or item.get("root_trace_id")
                        or ctx.trace_id,
                        locked_at=(datetime.now(UTC) if item.get("status") == "locked" else None),
                        payload={
                            "seeded": True,
                            "snapshot_sha256": snapshot_sha256,
                            "metadata": {
                                "model_version": item.get("model_version"),
                                "label_version": item.get("label_version"),
                            },
                        },
                    )
                )

    for run in seed["tasking"]["task_runs"]:
        session.merge(
            RunRecord(
                run_id=run["task_run_id"],
                tenant_id=tenant["tenant_id"],
                project_id=project["project_id"],
                run_type="task_run",
                status=run["status"],
                run_key=run.get("run_key"),
                partition_key=run.get("partition_key"),
                trace_id=run["trace_id"],
                payload=run,
            )
        )
    for run in seed["labeling"]["label_optimization_runs"]:
        session.merge(
            RunRecord(
                run_id=run["optimization_run_id"],
                tenant_id=tenant["tenant_id"],
                project_id=project["project_id"],
                run_type="label_optimization",
                status=run["status"],
                run_key=run.get("candidate_version"),
                partition_key=run.get("sample_set"),
                trace_id=run["trace_id"],
                payload=run,
            )
        )
    for run in seed["evaluation"]["eval_runs"]:
        session.merge(
            RunRecord(
                run_id=run["eval_run_id"],
                tenant_id=tenant["tenant_id"],
                project_id=project["project_id"],
                run_type=run.get("run_type", "eval_run"),
                status=run["status"],
                run_key=run.get("dataset_id"),
                partition_key=run.get("hotword_pack_version_id") or run.get("candidate_version"),
                trace_id=(
                    run.get("root_trace_id") or run.get("trace_id") or f"trace_{run['eval_run_id']}"
                ),
                payload=run,
            )
        )
    session.commit()
