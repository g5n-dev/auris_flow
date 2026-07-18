from __future__ import annotations

import hashlib
import json
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from typing import Any, Literal, overload

from fastapi import Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.domain.calibration.rubrics import RUBRIC_PROFILES
from app.models import (
    HotwordPackVersion,
    JsonResource,
    Project,
    ProjectSceneProfileBinding,
    RunRecord,
    SceneProfile,
    SceneProfileVersion,
)
from app.schemas.scene_profiles import (
    ProjectSceneProfileBindingRequest,
    SceneProfileCreateRequest,
    SceneProfileGenerationRequest,
    SceneProfileManifest,
    SceneProfilePatchRequest,
    SceneProfilePublishRequest,
    SceneProfileReviewRequest,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event
from app.services.run_service import create_run

EDITABLE_VERSION_STATUSES = {"draft", "candidate", "blocked"}
PUBLISHABLE_DEPENDENCY_STATUSES: dict[str, set[str | None]] = {
    "data_contracts": {"active", "published", "locked"},
    "schemas": {"active", "published", "locked"},
    "task_types": {None, "active", "published", "success"},
    "label_versions": {"published"},
    "prompt_versions": {"published"},
    "knowledge_indexes": {"active", "published", "success"},
    "eval_datasets": {"locked", "published"},
    "metric_calculators": {"active", "published", "locked"},
    "retention_policies": {"active", "published", "locked"},
    "privacy_policies": {"active", "published", "locked"},
    "connectors": {"active", "online", "success", "published"},
    "model_services": {"active", "online", "success", "published"},
    "hotword_pack_versions": {"published"},
    "calibration_rubrics": {"published"},
    "output_sinks": {"active", "online", "success", "published"},
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _human_only(ctx: RequestContext, *, action: str) -> None:
    if ctx.actor_kind != "human":
        raise ApiError(
            "HUMAN_ACTOR_REQUIRED",
            "场景校验、复核、发布和绑定必须由实名人工账号执行",
            403,
            details=[{"action": action, "actor_kind": ctx.actor_kind}],
        )


def _profile_query(ctx: RequestContext):
    return select(SceneProfile).where(
        SceneProfile.tenant_id == ctx.tenant_id,
        SceneProfile.project_id == ctx.project_id,
    )


def _version_query(ctx: RequestContext):
    return select(SceneProfileVersion).where(
        SceneProfileVersion.tenant_id == ctx.tenant_id,
        SceneProfileVersion.project_id == ctx.project_id,
    )


def encode_scene_profile_cursor(profile: SceneProfile) -> str:
    created_at = profile.created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(UTC).replace(tzinfo=None)
    token = f"scene_profile|{created_at.isoformat()}|{profile.scene_profile_id}".encode()
    return urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode_scene_profile_cursor(cursor: str | int | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        cursor_text = str(cursor)
        padded = cursor_text + "=" * (-len(cursor_text) % 4)
        raw = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        prefix, created_at, scene_profile_id = raw.split("|", 2)
        if prefix != "scene_profile" or not created_at or not scene_profile_id:
            raise ValueError
        decoded_created_at = datetime.fromisoformat(created_at)
        if decoded_created_at.tzinfo is not None:
            decoded_created_at = decoded_created_at.astimezone(UTC).replace(tzinfo=None)
        return decoded_created_at, scene_profile_id
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None


def get_scene_profile(
    session: Session,
    ctx: RequestContext,
    scene_profile_id: str,
    *,
    for_update: bool = False,
) -> SceneProfile:
    statement = _profile_query(ctx).where(SceneProfile.scene_profile_id == scene_profile_id)
    if for_update:
        statement = statement.with_for_update()
    profile = session.scalar(statement)
    if profile is None:
        raise ApiError("SCENE_PROFILE_NOT_FOUND", "场景配置不存在", 404)
    return profile


def get_scene_profile_version(
    session: Session,
    ctx: RequestContext,
    scene_profile_version_id: str,
    *,
    for_update: bool = False,
) -> SceneProfileVersion:
    statement = _version_query(ctx).where(
        SceneProfileVersion.scene_profile_version_id == scene_profile_version_id
    )
    if for_update:
        statement = statement.with_for_update()
    version = session.scalar(statement)
    if version is None:
        raise ApiError("SCENE_PROFILE_VERSION_NOT_FOUND", "场景配置版本不存在", 404)
    return version


def scene_profile_payload(profile: SceneProfile) -> dict[str, Any]:
    return {
        "scene_profile_id": profile.scene_profile_id,
        "tenant_id": profile.tenant_id,
        "project_id": profile.project_id,
        "scene_key": profile.scene_key,
        "name": profile.name,
        "description": profile.description,
        "status": profile.status,
        "current_published_version_id": profile.current_published_version_id,
        "created_by": profile.created_by,
        "trace_id": profile.trace_id,
        "created_at": _iso_timestamp(profile.created_at),
        "updated_at": _iso_timestamp(profile.updated_at),
    }


def scene_profile_version_payload(version: SceneProfileVersion) -> dict[str, Any]:
    return {
        "scene_profile_version_id": version.scene_profile_version_id,
        "scene_profile_id": version.scene_profile_id,
        "tenant_id": version.tenant_id,
        "project_id": version.project_id,
        "version": version.version,
        "status": version.status,
        "source_type": version.source_type,
        "schema_version": version.schema_version,
        "parent_version_id": version.parent_version_id,
        "generated_by_run_id": version.generated_by_run_id,
        "requested_by": version.requested_by,
        "reviewed_by": version.reviewed_by,
        "published_by": version.published_by,
        "manifest": version.manifest,
        "manifest_sha256": version.manifest_sha256,
        "validation_report": version.validation_report,
        "review_record": version.review_record,
        "resource_version": version.resource_version,
        "trace_id": version.trace_id,
        "created_at": _iso_timestamp(version.created_at),
        "updated_at": _iso_timestamp(version.updated_at),
    }


def binding_payload(binding: ProjectSceneProfileBinding) -> dict[str, Any]:
    return {
        "binding_id": binding.binding_id,
        "tenant_id": binding.tenant_id,
        "project_id": binding.project_id,
        "environment": binding.environment,
        "scene_profile_id": binding.scene_profile_id,
        "scene_profile_version_id": binding.scene_profile_version_id,
        "manifest_sha256": binding.manifest_sha256,
        "status": binding.status,
        "bound_by": binding.bound_by,
        "resource_version": binding.resource_version,
        "trace_id": binding.trace_id,
        "created_at": _iso_timestamp(binding.created_at),
        "updated_at": _iso_timestamp(binding.updated_at),
    }


def list_scene_profiles(
    session: Session,
    ctx: RequestContext,
    page: dict[str, str | int | None],
) -> dict[str, Any]:
    statement = _profile_query(ctx)
    cursor_created_at, cursor_profile_id = decode_scene_profile_cursor(page.get("cursor"))
    if cursor_created_at is not None and cursor_profile_id is not None:
        created_at_sort: Any = SceneProfile.created_at
        cursor_sort: datetime | str = cursor_created_at
        if session.get_bind().dialect.name == "sqlite":
            created_at_sort = func.strftime("%Y-%m-%d %H:%M:%f", SceneProfile.created_at)
            cursor_sort = cursor_created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        statement = statement.where(
            or_(
                created_at_sort < cursor_sort,
                and_(
                    created_at_sort == cursor_sort,
                    SceneProfile.scene_profile_id < cursor_profile_id,
                ),
            )
        )
    limit = int(page.get("limit") or 50)
    profiles = list(
        session.scalars(
            statement.order_by(
                SceneProfile.created_at.desc(),
                SceneProfile.scene_profile_id.desc(),
            ).limit(limit + 1)
        )
    )
    visible = profiles[:limit]
    version_counts: dict[str, int] = (
        {
            str(profile_id): int(count)
            for profile_id, count in session.execute(
                select(
                    SceneProfileVersion.scene_profile_id,
                    func.count(SceneProfileVersion.scene_profile_version_id),
                )
                .where(
                    SceneProfileVersion.tenant_id == ctx.tenant_id,
                    SceneProfileVersion.project_id == ctx.project_id,
                    SceneProfileVersion.scene_profile_id.in_(
                        [profile.scene_profile_id for profile in visible]
                    ),
                )
                .group_by(SceneProfileVersion.scene_profile_id)
            ).all()
        }
        if visible
        else {}
    )
    items = [
        {
            **scene_profile_payload(profile),
            "version_count": int(version_counts.get(profile.scene_profile_id, 0)),
        }
        for profile in visible
    ]
    total = int(
        session.scalar(
            select(func.count())
            .select_from(SceneProfile)
            .where(
                SceneProfile.tenant_id == ctx.tenant_id,
                SceneProfile.project_id == ctx.project_id,
            )
        )
        or 0
    )
    return collection_envelope(
        items,
        ctx,
        total=total,
        limit=limit,
        next_cursor=(
            encode_scene_profile_cursor(visible[-1]) if len(profiles) > limit and visible else None
        ),
    )


def get_scene_profile_detail(
    session: Session,
    ctx: RequestContext,
    scene_profile_id: str,
) -> dict[str, Any]:
    profile = get_scene_profile(session, ctx, scene_profile_id)
    versions = list(
        session.scalars(
            _version_query(ctx)
            .where(SceneProfileVersion.scene_profile_id == scene_profile_id)
            .order_by(SceneProfileVersion.created_at.desc())
        )
    )
    return envelope(
        {
            **scene_profile_payload(profile),
            "versions": [scene_profile_version_payload(version) for version in versions],
        },
        ctx,
    )


def _profile_by_key(
    session: Session,
    ctx: RequestContext,
    scene_key: str,
    *,
    for_update: bool = False,
) -> SceneProfile | None:
    statement = _profile_query(ctx).where(SceneProfile.scene_key == scene_key)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _version_by_number(
    session: Session,
    ctx: RequestContext,
    scene_profile_id: str,
    version: str,
) -> SceneProfileVersion | None:
    return session.scalar(
        _version_query(ctx).where(
            SceneProfileVersion.scene_profile_id == scene_profile_id,
            SceneProfileVersion.version == version,
        )
    )


async def create_scene_profile(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: SceneProfileCreateRequest,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin", "model_engineer"), action="scene_profiles.create")
    _human_only(ctx, action="scene_profiles.create")
    body_hash = await request_hash(request)
    operation = "scene_profiles.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    profile = _profile_by_key(session, ctx, body.scene_key, for_update=True)
    if profile is None:
        created_at = datetime.now(UTC)
        profile = SceneProfile(
            scene_profile_id=body.scene_profile_id or f"scene_{uuid.uuid4().hex[:16]}",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            scene_key=body.scene_key,
            name=body.name,
            description=body.description,
            status="draft",
            created_by=ctx.user_id,
            trace_id=ctx.trace_id,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(profile)
        session.flush()
    elif body.scene_profile_id and body.scene_profile_id != profile.scene_profile_id:
        raise ApiError(
            "SCENE_PROFILE_KEY_CONFLICT",
            "scene_key 已绑定到其他场景配置",
            409,
            details=[{"scene_key": body.scene_key, "scene_profile_id": profile.scene_profile_id}],
        )
    if _version_by_number(session, ctx, profile.scene_profile_id, body.version):
        raise ApiError("SCENE_PROFILE_VERSION_EXISTS", "场景版本已经存在", 409)

    manifest = body.manifest.model_dump(mode="json")
    scene_profile_version_id = body.scene_profile_version_id or f"scenev_{uuid.uuid4().hex[:16]}"
    version = SceneProfileVersion(
        scene_profile_version_id=scene_profile_version_id,
        scene_profile_id=profile.scene_profile_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        version=body.version,
        status="draft",
        source_type=body.source_type,
        schema_version=body.manifest.schema_version,
        parent_version_id=body.parent_version_id,
        requested_by=ctx.user_id,
        manifest=manifest,
        manifest_sha256=_canonical_hash(manifest),
        validation_report={},
        review_record={},
        resource_version=1,
        trace_id=ctx.trace_id,
    )
    session.add(version)
    profile.status = "draft"
    profile.name = body.name
    profile.description = body.description
    profile.trace_id = ctx.trace_id
    session.flush()

    response = envelope(
        {
            "profile": scene_profile_payload(profile),
            "version": scene_profile_version_payload(version),
            "next_actions": ["validate", "review", "publish", "bind_project"],
        },
        ctx,
    )
    record_audit(
        session,
        ctx,
        action="scene_profile.create",
        object_type="scene_profile_version",
        object_id=scene_profile_version_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="scene_profile.draft-created",
        aggregate_type="scene_profile",
        aggregate_id=profile.scene_profile_id,
        payload=response["data"],
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


async def request_scene_profile_generation(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: SceneProfileGenerationRequest,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        action="scene_profile_generation.create",
    )
    _human_only(ctx, action="scene_profile_generation.create")
    profile = _profile_by_key(session, ctx, body.scene_key)
    profile_id = (
        profile.scene_profile_id
        if profile
        else (body.scene_profile_id or f"scene_{uuid.uuid4().hex[:16]}")
    )
    if body.scene_profile_id and profile and body.scene_profile_id != profile.scene_profile_id:
        raise ApiError("SCENE_PROFILE_KEY_CONFLICT", "scene_key 已绑定到其他场景配置", 409)

    def prepare_record(record: RunRecord) -> None:
        existing = _profile_by_key(session, ctx, body.scene_key, for_update=True)
        if existing is None:
            session.add(
                SceneProfile(
                    scene_profile_id=profile_id,
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    scene_key=body.scene_key,
                    name=body.name,
                    description=body.description,
                    status="generating",
                    created_by=ctx.user_id,
                    trace_id=record.trace_id,
                )
            )
        elif existing.status == "archived":
            raise ApiError("SCENE_PROFILE_ARCHIVED", "已归档场景不能生成新候选", 409)

    return await create_run(
        session,
        ctx,
        request,
        run_type="scene_profile_generation",
        event_type="scene_profile.generation-requested",
        payload={
            **body.model_dump(exclude_none=True),
            "scene_profile_id": profile_id,
            "requested_by": ctx.user_id,
            "request_actor_kind": ctx.actor_kind,
            "candidate_only": True,
            "publish_allowed": False,
            "affected_objects": [{"type": "scene_profile", "id": profile_id}],
            "next_actions": [
                {"key": "wait_candidate", "label": "等待候选场景"},
                {"key": "view_trace", "label": "查看 Trace"},
            ],
        },
        status="queued",
        prepare_record=prepare_record,
    )


def materialize_scene_profile_generation_completion(
    session: Session,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> SceneProfileVersion | None:
    if record.run_type != "scene_profile_generation":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError("SCENE_PROFILE_RESULT_MISSING", "场景生成结果缺少 result_ref", 422)
    manifest_data = result_ref.get("scene_profile_manifest")
    if not isinstance(manifest_data, dict):
        raise ApiError(
            "SCENE_PROFILE_MANIFEST_MISSING",
            "场景生成结果必须包含 scene_profile_manifest",
            422,
        )
    manifest = SceneProfileManifest.model_validate(manifest_data)
    expected_scene_key = str(record.payload.get("scene_key") or "")
    if manifest.scene_key != expected_scene_key:
        raise ApiError(
            "SCENE_PROFILE_IDENTITY_MISMATCH",
            "模型返回的 scene_key 与生成请求不一致",
            409,
        )
    profile_id = str(record.payload.get("scene_profile_id") or "")
    profile = session.scalar(
        select(SceneProfile)
        .where(
            SceneProfile.tenant_id == record.tenant_id,
            SceneProfile.project_id == record.project_id,
            SceneProfile.scene_profile_id == profile_id,
        )
        .with_for_update()
    )
    if profile is None:
        raise ApiError("SCENE_PROFILE_NOT_FOUND", "生成运行对应场景配置不存在", 409)
    version_number = str(record.payload.get("version") or "")
    existing = session.scalar(
        select(SceneProfileVersion).where(
            SceneProfileVersion.tenant_id == record.tenant_id,
            SceneProfileVersion.project_id == record.project_id,
            SceneProfileVersion.scene_profile_id == profile.scene_profile_id,
            SceneProfileVersion.version == version_number,
        )
    )
    if existing is not None:
        if existing.generated_by_run_id == record.run_id:
            return existing
        raise ApiError("SCENE_PROFILE_VERSION_EXISTS", "模型生成目标版本已经存在", 409)

    canonical_manifest = manifest.model_dump(mode="json")
    version = SceneProfileVersion(
        scene_profile_version_id=str(
            result_ref.get("scene_profile_version_id") or f"scenev_{uuid.uuid4().hex[:16]}"
        ),
        scene_profile_id=profile.scene_profile_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        version=version_number,
        status="candidate",
        source_type="model",
        schema_version=manifest.schema_version,
        parent_version_id=record.payload.get("parent_version_id"),
        generated_by_run_id=record.run_id,
        requested_by=str(record.payload.get("requested_by") or "unknown"),
        manifest=canonical_manifest,
        manifest_sha256=_canonical_hash(canonical_manifest),
        validation_report={},
        review_record={
            "generation": {
                "model_ref": record.payload.get("model_ref"),
                "input_refs": record.payload.get("input_refs", []),
                "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
                "agent_run_id": record.payload.get("agent_run_id"),
            }
        },
        resource_version=1,
        trace_id=record.trace_id,
    )
    session.add(version)
    profile.status = "candidate"
    profile.trace_id = record.trace_id
    completion_ctx = RequestContext(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        user_id="scene_profile_materializer",
        roles=("system",),
        request_id=f"materialize:{record.run_id}",
        trace_id=record.trace_id,
        actor_kind="service",
    )
    record_audit(
        session,
        completion_ctx,
        action="scene_profile.candidate_materialized",
        object_type="scene_profile_version",
        object_id=version.scene_profile_version_id,
        after=scene_profile_version_payload(version),
    )
    enqueue_event(
        session,
        completion_ctx,
        event_type="scene_profile.candidate-generated",
        aggregate_type="scene_profile_version",
        aggregate_id=version.scene_profile_version_id,
        payload=scene_profile_version_payload(version),
    )
    return version


def _resource_dependency_snapshot(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_id: str,
) -> dict[str, Any] | None:
    if collection == "calibration_rubrics":
        rubric = RUBRIC_PROFILES.get(resource_id)
        if rubric is None:
            return None
        rubric_data = {
            "version": rubric.version,
            "categories": sorted(rubric.categories),
            "reason_codes": sorted(rubric.reason_codes),
        }
        return {
            "collection": collection,
            "resource_id": resource_id,
            "status": "published",
            "content_sha256": _canonical_hash(rubric_data),
        }
    if collection == "hotword_pack_versions":
        version = session.scalar(
            select(HotwordPackVersion).where(
                HotwordPackVersion.tenant_id == ctx.tenant_id,
                HotwordPackVersion.project_id == ctx.project_id,
                HotwordPackVersion.version_id == resource_id,
            )
        )
        if version is None:
            return None
        return {
            "collection": collection,
            "resource_id": resource_id,
            "status": version.status,
            "content_sha256": _canonical_hash(
                {
                    "pack_id": version.pack_id,
                    "version_id": version.version_id,
                    "version": version.version,
                    "status": version.status,
                    "content_sha256": version.content_sha256,
                    "provider_artifact_ref": version.provider_artifact_ref,
                    "compiled_provider": version.compiled_provider,
                    "eval_run_id": version.eval_run_id,
                    "payload": version.payload,
                }
            ),
        }
    resource = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == collection,
            JsonResource.resource_key == resource_id,
        )
    )
    if resource is None:
        return None
    status = resource.status or resource.data.get("status")
    return {
        "collection": collection,
        "resource_id": resource_id,
        "status": status,
        "content_sha256": _canonical_hash(
            {
                "collection": collection,
                "resource_id": resource_id,
                "status": status,
                "data": resource.data,
            }
        ),
    }


def validate_manifest_dependencies(
    session: Session,
    ctx: RequestContext,
    manifest: SceneProfileManifest,
) -> dict[str, Any]:
    schema_refs = sorted(
        {
            item.schema_ref
            for item in (*manifest.entities, *manifest.events, *manifest.document_types)
            if item.schema_ref
        }
    )
    dependency_sets = {
        "data_contracts": manifest.data_contract_refs,
        "schemas": schema_refs,
        "task_types": manifest.task_type_refs,
        "label_versions": manifest.label_version_refs,
        "prompt_versions": manifest.prompt_version_refs,
        "knowledge_indexes": manifest.knowledge_index_refs,
        "eval_datasets": manifest.eval_dataset_version_refs,
        "connectors": manifest.connector_refs,
        "model_services": manifest.model_service_refs,
        "hotword_pack_versions": manifest.hotword_pack_version_refs,
        "calibration_rubrics": manifest.rubric_refs,
        "output_sinks": manifest.output_sink_refs,
        "metric_calculators": sorted({item.calculator_ref for item in manifest.metrics}),
        "retention_policies": [manifest.governance.retention_policy_ref],
        "privacy_policies": [manifest.governance.privacy_policy_ref],
    }
    blockers: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for collection, refs in dependency_sets.items():
        allowed_statuses = PUBLISHABLE_DEPENDENCY_STATUSES[collection]
        for resource_id in refs:
            snapshot = _resource_dependency_snapshot(session, ctx, collection, resource_id)
            if snapshot is None:
                blockers.append(
                    {
                        "code": "SCENE_DEPENDENCY_MISSING",
                        "collection": collection,
                        "resource_id": resource_id,
                    }
                )
            elif snapshot["status"] not in allowed_statuses:
                blockers.append(
                    {
                        "code": "SCENE_DEPENDENCY_NOT_RELEASED",
                        "collection": collection,
                        "resource_id": resource_id,
                        "status": snapshot["status"],
                        "allowed_statuses": sorted(
                            value for value in allowed_statuses if value is not None
                        ),
                    }
                )
            else:
                resolved.append(snapshot)
    resolved.sort(key=lambda item: (str(item["collection"]), str(item["resource_id"])))
    blockers.sort(
        key=lambda item: (
            str(item.get("collection") or ""),
            str(item.get("resource_id") or ""),
            str(item.get("code") or ""),
        )
    )
    return {
        "status": "pass" if not blockers else "blocked",
        "manifest_sha256": _canonical_hash(manifest.model_dump(mode="json")),
        "dependency_closure_sha256": _canonical_hash(resolved),
        "resolved_dependencies": resolved,
        "blockers": blockers,
    }


async def patch_scene_profile_version(
    session: Session,
    ctx: RequestContext,
    request: Request,
    scene_profile_version_id: str,
    body: SceneProfilePatchRequest,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin", "model_engineer"), action="scene_profiles.patch")
    _human_only(ctx, action="scene_profiles.patch")
    body_hash = await request_hash(request)
    operation = f"scene_profile_versions.patch:{scene_profile_version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    version = get_scene_profile_version(session, ctx, scene_profile_version_id, for_update=True)
    if version.status not in EDITABLE_VERSION_STATUSES:
        raise ApiError("SCENE_PROFILE_VERSION_IMMUTABLE", "当前场景版本已冻结，不能修改", 409)
    if version.resource_version != body.expected_resource_version:
        raise ApiError(
            "SCENE_PROFILE_VERSION_CONFLICT",
            "场景版本已被其他操作修改，请刷新后重试",
            409,
            details=[{"current_resource_version": version.resource_version}],
        )
    profile = get_scene_profile(session, ctx, version.scene_profile_id)
    if body.manifest.scene_key != profile.scene_key:
        raise ApiError("SCENE_PROFILE_IDENTITY_MISMATCH", "manifest.scene_key 不能修改", 409)
    before = scene_profile_version_payload(version)
    manifest = body.manifest.model_dump(mode="json")
    version.manifest = manifest
    version.manifest_sha256 = _canonical_hash(manifest)
    version.schema_version = body.manifest.schema_version
    version.status = "draft" if version.source_type != "model" else "candidate"
    version.validation_report = {}
    version.reviewed_by = None
    version.published_by = None
    version.resource_version += 1
    version.trace_id = ctx.trace_id
    response = envelope(scene_profile_version_payload(version), ctx)
    record_audit(
        session,
        ctx,
        action="scene_profile_version.patch",
        object_type="scene_profile_version",
        object_id=version.scene_profile_version_id,
        before=before,
        after=response["data"],
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


async def validate_scene_profile_version(
    session: Session,
    ctx: RequestContext,
    request: Request,
    scene_profile_version_id: str,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer", "review_arbitrator"),
        action="scene_profiles.validate",
    )
    _human_only(ctx, action="scene_profiles.validate")
    body_hash = await request_hash(request)
    operation = f"scene_profile_versions.validate:{scene_profile_version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    version = get_scene_profile_version(session, ctx, scene_profile_version_id, for_update=True)
    if version.status not in EDITABLE_VERSION_STATUSES | {"validated"}:
        raise ApiError("SCENE_PROFILE_VERSION_NOT_VALIDATABLE", "当前场景版本不能重新校验", 409)
    manifest = SceneProfileManifest.model_validate(version.manifest)
    report = validate_manifest_dependencies(session, ctx, manifest)
    report.update(
        {
            "validated_at": datetime.now(UTC).isoformat(),
            "validated_by": ctx.user_id,
            "actor_kind": ctx.actor_kind,
        }
    )
    version.validation_report = report
    version.status = "validated" if report["status"] == "pass" else "blocked"
    version.resource_version += 1
    version.trace_id = ctx.trace_id
    response = envelope(scene_profile_version_payload(version), ctx)
    record_audit(
        session,
        ctx,
        action="scene_profile_version.validate",
        object_type="scene_profile_version",
        object_id=version.scene_profile_version_id,
        after=response["data"],
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


async def review_scene_profile_version(
    session: Session,
    ctx: RequestContext,
    request: Request,
    scene_profile_version_id: str,
    body: SceneProfileReviewRequest,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "review_arbitrator"),
        action="scene_profiles.review",
    )
    _human_only(ctx, action="scene_profiles.review")
    body_hash = await request_hash(request)
    operation = f"scene_profile_versions.review:{scene_profile_version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    version = get_scene_profile_version(session, ctx, scene_profile_version_id, for_update=True)
    if version.status != "validated":
        raise ApiError("SCENE_PROFILE_NOT_VALIDATED", "场景版本必须先通过依赖校验", 409)
    if ctx.user_id == version.requested_by:
        raise ApiError(
            "SCENE_PROFILE_SEPARATION_OF_DUTIES",
            "场景创建或生成申请人与复核人必须是不同实名用户",
            409,
        )
    version.status = body.decision
    version.reviewed_by = ctx.user_id
    version.review_record = {
        **version.review_record,
        "decision": body.decision,
        "reason": body.reason,
        "reviewed_by": ctx.user_id,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "actor_kind": ctx.actor_kind,
    }
    version.resource_version += 1
    version.trace_id = ctx.trace_id
    response = envelope(scene_profile_version_payload(version), ctx)
    record_audit(
        session,
        ctx,
        action=f"scene_profile_version.{body.decision}",
        object_type="scene_profile_version",
        object_id=version.scene_profile_version_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type=f"scene_profile.{body.decision}",
        aggregate_type="scene_profile_version",
        aggregate_id=version.scene_profile_version_id,
        payload=response["data"],
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


async def publish_scene_profile_version(
    session: Session,
    ctx: RequestContext,
    request: Request,
    scene_profile_version_id: str,
    body: SceneProfilePublishRequest,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin",), action="scene_profiles.publish")
    _human_only(ctx, action="scene_profiles.publish")
    body_hash = await request_hash(request)
    operation = f"scene_profile_versions.publish:{scene_profile_version_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    version = get_scene_profile_version(session, ctx, scene_profile_version_id, for_update=True)
    if version.status != "approved":
        raise ApiError("SCENE_PROFILE_NOT_APPROVED", "场景版本必须先通过独立人工复核", 409)
    if version.validation_report.get("status") != "pass":
        raise ApiError("SCENE_PROFILE_VALIDATION_BLOCKED", "场景版本校验未通过", 409)
    if version.validation_report.get("manifest_sha256") != version.manifest_sha256:
        raise ApiError("SCENE_PROFILE_SNAPSHOT_DRIFT", "场景内容在校验后发生变化", 409)
    manifest = SceneProfileManifest.model_validate(version.manifest)
    current_validation = validate_manifest_dependencies(session, ctx, manifest)
    if current_validation["status"] != "pass":
        raise ApiError(
            "SCENE_PROFILE_DEPENDENCY_BLOCKED",
            "场景依赖在发布前已失效",
            409,
            details=current_validation["blockers"],
        )
    if current_validation["dependency_closure_sha256"] != version.validation_report.get(
        "dependency_closure_sha256"
    ):
        raise ApiError(
            "SCENE_PROFILE_DEPENDENCY_DRIFT",
            "场景依赖在校验后发生变化，请重新校验并复核",
            409,
        )
    profile = get_scene_profile(session, ctx, version.scene_profile_id, for_update=True)
    # Publishing a new immutable version must not invalidate a production
    # binding that still points at an older version. Promotion is a separate
    # CAS-protected binding operation; retirement requires an explicit command
    # after no active binding references the historical version.
    version.status = "published"
    version.published_by = ctx.user_id
    version.resource_version += 1
    version.trace_id = ctx.trace_id
    profile.status = "published"
    profile.current_published_version_id = version.scene_profile_version_id
    profile.trace_id = ctx.trace_id
    response = envelope(
        {
            "profile": scene_profile_payload(profile),
            "version": scene_profile_version_payload(version),
            "reason": body.reason,
        },
        ctx,
    )
    record_audit(
        session,
        ctx,
        action="scene_profile_version.publish",
        object_type="scene_profile_version",
        object_id=version.scene_profile_version_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="scene_profile.published",
        aggregate_type="scene_profile_version",
        aggregate_id=version.scene_profile_version_id,
        payload=response["data"],
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


def get_project_scene_binding(
    session: Session,
    ctx: RequestContext,
    environment: str,
    *,
    for_update: bool = False,
) -> ProjectSceneProfileBinding | None:
    statement = select(ProjectSceneProfileBinding).where(
        ProjectSceneProfileBinding.tenant_id == ctx.tenant_id,
        ProjectSceneProfileBinding.project_id == ctx.project_id,
        ProjectSceneProfileBinding.environment == environment,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


@overload
def get_active_scene_binding(
    session: Session,
    ctx: RequestContext,
    environment: str = "production",
    *,
    allow_missing: Literal[False] = False,
) -> dict[str, Any]: ...


@overload
def get_active_scene_binding(
    session: Session,
    ctx: RequestContext,
    environment: str = "production",
    *,
    allow_missing: Literal[True],
) -> dict[str, Any] | None: ...


@overload
def get_active_scene_binding(
    session: Session,
    ctx: RequestContext,
    environment: str = "production",
    *,
    allow_missing: bool,
) -> dict[str, Any] | None: ...


def get_active_scene_binding(
    session: Session,
    ctx: RequestContext,
    environment: str = "production",
    *,
    allow_missing: bool = False,
) -> dict[str, Any] | None:
    binding = get_project_scene_binding(session, ctx, environment)
    if binding is None or binding.status != "active":
        if allow_missing:
            return None
        raise ApiError(
            "SCENE_PROFILE_BINDING_MISSING",
            "当前项目没有已激活的场景配置",
            404,
            details=[{"environment": environment}],
        )
    version = get_scene_profile_version(session, ctx, binding.scene_profile_version_id)
    if (
        version.status != "published"
        or version.scene_profile_id != binding.scene_profile_id
        or version.manifest_sha256 != binding.manifest_sha256
    ):
        raise ApiError("SCENE_PROFILE_BINDING_DRIFT", "场景绑定已漂移或版本不再可用", 409)
    return {
        **binding_payload(binding),
        "version": scene_profile_version_payload(version),
    }


async def bind_project_scene_profile(
    session: Session,
    ctx: RequestContext,
    request: Request,
    project_id: str,
    body: ProjectSceneProfileBindingRequest,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin",), action="scene_profiles.bind")
    _human_only(ctx, action="scene_profiles.bind")
    if project_id != ctx.project_id:
        raise ApiError("PROJECT_CONTEXT_MISMATCH", "只能绑定当前上下文项目", 403)
    body_hash = await request_hash(request)
    operation = f"scene_profile_bindings.put:{body.environment}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    version = get_scene_profile_version(
        session,
        ctx,
        body.scene_profile_version_id,
        for_update=True,
    )
    if version.status != "published":
        raise ApiError("SCENE_PROFILE_NOT_PUBLISHED", "项目只能绑定已发布场景版本", 409)
    binding = get_project_scene_binding(session, ctx, body.environment, for_update=True)
    if binding is None:
        if body.expected_resource_version is not None:
            raise ApiError(
                "SCENE_PROFILE_BINDING_CONFLICT",
                "绑定尚不存在，不能使用 expected_resource_version",
                409,
            )
        binding = ProjectSceneProfileBinding(
            binding_id=f"sceneb_{uuid.uuid4().hex[:16]}",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            environment=body.environment,
            scene_profile_id=version.scene_profile_id,
            scene_profile_version_id=version.scene_profile_version_id,
            manifest_sha256=version.manifest_sha256,
            status="active",
            bound_by=ctx.user_id,
            resource_version=1,
            trace_id=ctx.trace_id,
        )
        session.add(binding)
    else:
        if (
            body.expected_resource_version is None
            or body.expected_resource_version != binding.resource_version
        ):
            raise ApiError(
                "SCENE_PROFILE_BINDING_CONFLICT",
                "场景绑定版本冲突，请刷新后使用最新 resource_version 重试",
                409,
                details=[{"current_resource_version": binding.resource_version}],
            )
        binding.scene_profile_id = version.scene_profile_id
        binding.scene_profile_version_id = version.scene_profile_version_id
        binding.manifest_sha256 = version.manifest_sha256
        binding.status = "active"
        binding.bound_by = ctx.user_id
        binding.resource_version += 1
        binding.trace_id = ctx.trace_id
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != ctx.tenant_id:
        raise ApiError("PROJECT_NOT_FOUND", "项目不存在或不属于当前租户", 404)
    project.data = {
        **project.data,
        "scene_profile_id": version.scene_profile_id,
        "scene_profile_version_id": version.scene_profile_version_id,
        "scene_profile_snapshot_sha256": version.manifest_sha256,
        "scene_profile_environment": body.environment,
        "trace_id": ctx.trace_id,
    }
    response = envelope(binding_payload(binding), ctx)
    record_audit(
        session,
        ctx,
        action="project_scene_profile.bind",
        object_type="project_scene_profile_binding",
        object_id=binding.binding_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="project.scene-profile-bound",
        aggregate_type="project",
        aggregate_id=project_id,
        payload=response["data"],
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


def assert_active_scene_profile_binding(
    session: Session,
    ctx: RequestContext,
    *,
    scene_profile_id: str | None = None,
    scene_profile_version_id: str | None = None,
    scene_profile_snapshot_sha256: str | None = None,
    environment: str = "production",
) -> ProjectSceneProfileBinding:
    binding = get_project_scene_binding(session, ctx, environment)
    if binding is None or binding.status != "active":
        raise ApiError(
            "SCENE_PROFILE_BINDING_REQUIRED",
            "生产运行必须绑定已发布场景版本",
            409,
        )
    version = get_scene_profile_version(session, ctx, binding.scene_profile_version_id)
    if (
        version.status != "published"
        or version.scene_profile_id != binding.scene_profile_id
        or version.manifest_sha256 != binding.manifest_sha256
    ):
        raise ApiError("SCENE_PROFILE_BINDING_DRIFT", "场景绑定已漂移或版本不再可发布", 409)
    if scene_profile_id and scene_profile_id != binding.scene_profile_id:
        raise ApiError("SCENE_PROFILE_ID_MISMATCH", "任务版本与项目场景配置不一致", 409)
    if scene_profile_version_id and scene_profile_version_id != binding.scene_profile_version_id:
        raise ApiError("SCENE_PROFILE_VERSION_MISMATCH", "任务版本与项目场景版本不一致", 409)
    if scene_profile_snapshot_sha256 and scene_profile_snapshot_sha256 != binding.manifest_sha256:
        raise ApiError("SCENE_PROFILE_SNAPSHOT_MISMATCH", "任务版本锁定的场景快照已漂移", 409)
    return binding


def assert_scene_profile_snapshot(
    session: Session,
    ctx: RequestContext,
    *,
    scene_profile_id: str,
    scene_profile_version_id: str,
    scene_profile_snapshot_sha256: str,
) -> SceneProfileVersion:
    """Resolve an immutable historical SceneProfile snapshot.

    In-flight runs keep using the snapshot captured at creation even after a
    project promotes a newer profile version.  The version may therefore be
    published or explicitly retired, but its profile identity and manifest
    digest must remain exact.
    """

    version = get_scene_profile_version(session, ctx, scene_profile_version_id)
    if version.status not in {"published", "deprecated"}:
        raise ApiError(
            "SCENE_PROFILE_SNAPSHOT_NOT_IMMUTABLE",
            "运行锁定的场景版本尚未发布，不能作为不可变快照",
            409,
            details=[{"status": version.status}],
        )
    if version.scene_profile_id != scene_profile_id:
        raise ApiError(
            "SCENE_PROFILE_ID_MISMATCH",
            "运行锁定的场景 Profile 与版本归属不一致",
            409,
        )
    if version.manifest_sha256 != scene_profile_snapshot_sha256:
        raise ApiError(
            "SCENE_PROFILE_SNAPSHOT_MISMATCH",
            "运行锁定的场景快照摘要已漂移",
            409,
        )
    return version
