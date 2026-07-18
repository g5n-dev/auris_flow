from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    IdempotencyRecord,
    JsonResource,
    LabelMappingBundle,
    LabelMappingBundleSource,
    LabelVersion,
    LabelVersionItem,
    ReleaseBundleHead,
    ReleaseBundleHeadEvent,
    ReleaseDeployment,
    RunRecord,
)
from app.schemas.label_lifecycle import (
    LabelVersionDeprecationPreflightRequest,
    LabelVersionDeprecationPreflightResponse,
    LabelVersionEnvironmentReference,
    LabelVersionInFlightRunReference,
    LabelVersionLifecycleBlocker,
    LabelVersionTransitionRequest,
    LabelVersionTransitionResponse,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    require_idempotency,
    save_idempotency_result,
)
from app.services.label_lifecycle_compat_service import (
    LabelLifecycleDriftError,
    transition_label_version_artifact,
)
from app.services.outbox_service import enqueue_event

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})
_MAX_REPLACEMENT_CHAIN_DEPTH = 64
_TERMINAL_RUN_STATUSES = frozenset(
    {"blocked", "cancelled", "completed", "failed", "rolled-back", "success"}
)


def list_label_version_item_views(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Return immutable label definitions for one version inside the caller scope."""

    version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == label_version_id,
        )
    )
    if version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "标签版本不存在", 404)

    filters = [
        LabelVersionItem.tenant_id == ctx.tenant_id,
        LabelVersionItem.project_id == ctx.project_id,
        LabelVersionItem.label_version_id == label_version_id,
    ]
    if status is not None:
        filters.append(LabelVersionItem.status == status)
    total = int(
        session.scalar(select(func.count()).select_from(LabelVersionItem).where(*filters)) or 0
    )
    statement = select(LabelVersionItem).where(*filters)
    if cursor is not None:
        statement = statement.where(LabelVersionItem.label_id > cursor)
    records = session.scalars(statement.order_by(LabelVersionItem.label_id).limit(limit + 1)).all()
    has_more = len(records) > limit
    items = records[:limit]
    views = [
        {
            "label_version_item_id": item.label_version_item_id,
            "label_version_id": item.label_version_id,
            "label_id": item.label_id,
            "canonical_name": item.canonical_name,
            "aliases": list(item.aliases),
            "value_type": item.value_type,
            "risk_level": item.risk_level,
            "mutual_exclusion_group": item.mutual_exclusion_group,
            "parent_ids": list(item.parent_ids),
            "aggregation_rule": dict(item.aggregation_rule),
            "status": item.status,
            "definition_sha256": item.definition_sha256,
            "trace_id": item.trace_id,
        }
        for item in items
    ]
    next_cursor = items[-1].label_id if has_more and items else None
    return views, total, next_cursor


@dataclass(frozen=True)
class _LockedLabelVersion:
    version: LabelVersion
    projection: JsonResource


@dataclass(frozen=True)
class _EnvironmentReferences:
    active: list[dict[str, Any]]
    draining: list[dict[str, Any]]
    in_flight: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    safe_stop_required: bool


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _request_hash(
    label_version_id: str,
    request: LabelVersionDeprecationPreflightRequest,
) -> str:
    return _sha256(
        {
            "label_version_id": label_version_id,
            "body": request.model_dump(mode="json"),
        }
    )


def _require_human_project_admin(ctx: RequestContext) -> None:
    if ctx.actor_kind != "human" or ctx.user_id == "system" or "system" in ctx.roles:
        raise ApiError(
            "AGENT_LABEL_LIFECYCLE_TRANSITION_FORBIDDEN",
            "标签版本废弃与归档只能由人工项目管理员执行",
            403,
        )
    if "project_admin" not in ctx.roles:
        raise ApiError("FORBIDDEN", "仅项目管理员可以变更标签版本生命周期", 403)


def _idempotency_replay(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    body_hash: str,
) -> dict[str, Any] | None:
    require_idempotency(ctx)
    key = ctx.idempotency_key or ""
    existing = session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.project_id == ctx.project_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == key,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None and existing.user_id != ctx.user_id:
        raise ApiError(
            "LABEL_LIFECYCLE_IDEMPOTENCY_ACTOR_CONFLICT",
            "该标签生命周期幂等键已由另一操作人使用",
            409,
        )
    return replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )


def _lock_label_version(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
) -> _LockedLabelVersion:
    version = session.scalar(
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == label_version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "标签版本不存在", 404)
    projection = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "label_versions",
            JsonResource.resource_key == label_version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if projection is None:
        raise ApiError(
            "LABEL_VERSION_PROJECTION_MISSING",
            "标签版本 JSON 投影不存在，生命周期变更已拒绝",
            409,
        )
    _assert_strong_and_projection_consistent(version, projection)
    return _LockedLabelVersion(version=version, projection=projection)


def _assert_strong_and_projection_consistent(
    version: LabelVersion,
    projection: JsonResource,
) -> None:
    missing_fields: list[str] = []
    if not isinstance(version.taxonomy_id, str) or not version.taxonomy_id.strip():
        missing_fields.append("taxonomy_id")
    if not isinstance(version.semantic_version, str) or not version.semantic_version.strip():
        missing_fields.append("semantic_version")
    if version.artifact_status not in {"published", "deprecated", "archived"}:
        missing_fields.append("artifact_status")
    if (
        not isinstance(version.content_sha256, str)
        or _SHA256_PATTERN.fullmatch(version.content_sha256) is None
    ):
        missing_fields.append("content_sha256")
    if version.resource_version < 1:
        missing_fields.append("resource_version")
    if missing_fields:
        raise ApiError(
            "LABEL_VERSION_STRONG_FIELDS_INCOMPLETE",
            "标签版本强字段不完整，生命周期变更已拒绝",
            409,
            details=[{"missing_fields": missing_fields}],
        )

    data = projection.data if isinstance(projection.data, dict) else {}
    expected = {
        "taxonomy_id": version.taxonomy_id,
        "semantic_version": version.semantic_version,
        "artifact_status": version.artifact_status,
        "status": version.status,
        "resource_version": version.resource_version,
        "content_sha256": version.content_sha256,
    }
    mismatches = [
        {
            "field": field_name,
            "strong_value": expected_value,
            "projection_value": data.get(field_name),
        }
        for field_name, expected_value in expected.items()
        if data.get(field_name) != expected_value
    ]
    projected_id = data.get("label_version_id", data.get("id"))
    if projected_id != version.label_version_id:
        mismatches.append(
            {
                "field": "label_version_id",
                "strong_value": version.label_version_id,
                "projection_value": projected_id,
            }
        )
    if projection.status != version.status:
        mismatches.append(
            {
                "field": "projection.status",
                "strong_value": version.status,
                "projection_value": projection.status,
            }
        )
    if version.status != version.artifact_status:
        mismatches.append(
            {
                "field": "status/artifact_status",
                "strong_value": version.artifact_status,
                "projection_value": version.status,
            }
        )
    if mismatches:
        raise ApiError(
            "LABEL_VERSION_PROJECTION_DRIFT",
            "标签版本强表与 JSON 投影不一致，生命周期变更已拒绝",
            409,
            details=mismatches,
        )


def _assert_expected_resource_version(
    version: LabelVersion,
    expected_resource_version: int,
) -> None:
    if version.resource_version == expected_resource_version:
        return
    raise ApiError(
        "RESOURCE_VERSION_CONFLICT",
        "标签版本资源版本已变化，请刷新后重试",
        409,
        details=[
            {
                "expected_resource_version": expected_resource_version,
                "actual_resource_version": version.resource_version,
            }
        ],
    )


def _lock_scoped_version(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
) -> LabelVersion | None:
    return session.scalar(
        select(LabelVersion)
        .where(
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
            LabelVersion.label_version_id == label_version_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _assert_no_replacement_cycle(
    session: Session,
    ctx: RequestContext,
    *,
    source_label_version_id: str,
    replacement: LabelVersion,
) -> None:
    visited = {source_label_version_id}
    current = replacement
    for _ in range(_MAX_REPLACEMENT_CHAIN_DEPTH):
        if current.label_version_id in visited:
            raise ApiError(
                "LABEL_VERSION_REPLACEMENT_CYCLE",
                "替代标签版本形成循环引用",
                409,
            )
        visited.add(current.label_version_id)
        next_id = current.replacement_label_version_id
        if next_id is None:
            return
        next_version = _lock_scoped_version(session, ctx, next_id)
        if next_version is None:
            raise ApiError(
                "LABEL_VERSION_REPLACEMENT_CHAIN_BROKEN",
                "替代标签版本链包含不可访问的版本",
                409,
                details=[{"label_version_id": next_id}],
            )
        current = next_version
    raise ApiError(
        "LABEL_VERSION_REPLACEMENT_CHAIN_TOO_DEEP",
        "替代标签版本链超过允许深度",
        409,
        details=[{"max_depth": _MAX_REPLACEMENT_CHAIN_DEPTH}],
    )


def _validate_replacement_binding(
    session: Session,
    ctx: RequestContext,
    *,
    source: LabelVersion,
    replacement_label_version_id: str | None,
    mapping_bundle_id: str | None,
) -> None:
    if replacement_label_version_id is None:
        if mapping_bundle_id is not None:
            raise ApiError(
                "LABEL_MAPPING_BUNDLE_WITHOUT_REPLACEMENT",
                "未指定替代标签版本时不能绑定映射包",
                400,
            )
        return
    if mapping_bundle_id is None:
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_REQUIRED",
            "指定替代标签版本时必须绑定已发布映射包",
            400,
        )
    if replacement_label_version_id == source.label_version_id:
        raise ApiError(
            "LABEL_VERSION_REPLACEMENT_SELF_REFERENCE",
            "标签版本不能替代自身",
            409,
        )

    replacement = _lock_scoped_version(session, ctx, replacement_label_version_id)
    if replacement is None:
        raise ApiError("LABEL_VERSION_REPLACEMENT_NOT_FOUND", "替代标签版本不存在", 404)
    if replacement.taxonomy_id != source.taxonomy_id:
        raise ApiError(
            "LABEL_VERSION_REPLACEMENT_TAXONOMY_MISMATCH",
            "替代标签版本必须属于同一标签体系",
            409,
        )
    if replacement.artifact_status != "published" or replacement.status != "published":
        raise ApiError(
            "LABEL_VERSION_REPLACEMENT_NOT_PUBLISHED",
            "替代标签版本必须处于已发布状态",
            409,
        )
    _assert_no_replacement_cycle(
        session,
        ctx,
        source_label_version_id=source.label_version_id,
        replacement=replacement,
    )

    bundle = session.scalar(
        select(LabelMappingBundle)
        .where(
            LabelMappingBundle.tenant_id == ctx.tenant_id,
            LabelMappingBundle.project_id == ctx.project_id,
            LabelMappingBundle.mapping_bundle_id == mapping_bundle_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if bundle is None:
        raise ApiError("LABEL_MAPPING_BUNDLE_NOT_FOUND", "标签映射包不存在", 404)
    if bundle.status != "published":
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_NOT_PUBLISHED",
            "标签映射包必须处于已发布状态",
            409,
        )
    if bundle.target_label_version_id != replacement_label_version_id:
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_TARGET_MISMATCH",
            "标签映射包目标版本与替代标签版本不一致",
            409,
        )

    normalized_sources = list(
        session.scalars(
            select(LabelMappingBundleSource)
            .where(
                LabelMappingBundleSource.tenant_id == ctx.tenant_id,
                LabelMappingBundleSource.project_id == ctx.project_id,
                LabelMappingBundleSource.mapping_bundle_id == mapping_bundle_id,
            )
            .order_by(LabelMappingBundleSource.source_order)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    manifest_sources = bundle.source_label_version_ids
    normalized_source_ids = [row.source_label_version_id for row in normalized_sources]
    if (
        not isinstance(manifest_sources, list)
        or any(not isinstance(item, str) for item in manifest_sources)
        or manifest_sources != normalized_source_ids
        or len(normalized_source_ids) != len(set(normalized_source_ids))
    ):
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_SOURCE_SET_DRIFT",
            "标签映射包清单与规范化来源不一致",
            409,
        )
    source_row = next(
        (
            row
            for row in normalized_sources
            if row.source_label_version_id == source.label_version_id
        ),
        None,
    )
    if source_row is None:
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_SOURCE_MISSING",
            "标签映射包不包含待废弃版本",
            409,
        )
    if source_row.source_resource_version != source.resource_version:
        raise ApiError(
            "LABEL_MAPPING_BUNDLE_SOURCE_VERSION_CONFLICT",
            "标签映射包绑定的来源资源版本已过期",
            409,
            details=[
                {
                    "expected_source_resource_version": source.resource_version,
                    "actual_source_resource_version": source_row.source_resource_version,
                }
            ],
        )


def _environment_references(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    replacement_label_version_id: str | None,
) -> _EnvironmentReferences:
    heads = list(
        session.scalars(
            select(ReleaseBundleHead)
            .where(
                ReleaseBundleHead.tenant_id == ctx.tenant_id,
                ReleaseBundleHead.project_id == ctx.project_id,
                ReleaseBundleHead.label_version_id == label_version_id,
                ReleaseBundleHead.status == "active",
            )
            .order_by(ReleaseBundleHead.environment, ReleaseBundleHead.release_head_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    draining_deployments = list(
        session.scalars(
            select(ReleaseDeployment)
            .where(
                ReleaseDeployment.tenant_id == ctx.tenant_id,
                ReleaseDeployment.project_id == ctx.project_id,
                ReleaseDeployment.label_version_id == label_version_id,
                ReleaseDeployment.status == "draining",
            )
            .order_by(ReleaseDeployment.environment, ReleaseDeployment.deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    candidate_runs = list(
        session.scalars(
            select(RunRecord)
            .where(
                RunRecord.tenant_id == ctx.tenant_id,
                RunRecord.project_id == ctx.project_id,
                RunRecord.run_type == "label_extraction",
                RunRecord.status.not_in(_TERMINAL_RUN_STATUSES),
            )
            .order_by(RunRecord.run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked_runs: list[RunRecord] = []
    for run in candidate_runs:
        payload = run.payload or {}
        locked_versions = payload.get("locked_versions")
        locked_versions = locked_versions if isinstance(locked_versions, dict) else {}
        if (
            str(locked_versions.get("label_version_id") or payload.get("label_version_id") or "")
            == label_version_id
        ):
            locked_runs.append(run)
    active: list[dict[str, Any]] = [
        {
            "deployment_id": head.active_deployment_id,
            "environment": head.environment,
            "head_generation": head.generation,
            "reference_status": "active",
        }
        for head in heads
    ]
    draining: list[dict[str, Any]] = [
        {
            "deployment_id": deployment.deployment_id,
            "environment": deployment.environment,
            "head_generation": None,
            "reference_status": "draining",
        }
        for deployment in draining_deployments
    ]
    in_flight: list[dict[str, Any]] = []
    for run in locked_runs:
        head_lock = (run.payload or {}).get("release_head_lock")
        head_lock = head_lock if isinstance(head_lock, dict) else {}
        in_flight.append(
            {
                "run_id": run.run_id,
                "run_status": run.status,
                "environment": str(head_lock.get("environment") or "unknown"),
                "head_generation": head_lock.get("generation"),
                "active_deployment_id": head_lock.get("active_deployment_id"),
                "active_bundle_sha256": head_lock.get("active_bundle_sha256"),
            }
        )
    blockers = (
        [
            {
                "code": "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE",
                "reference_type": "active-head",
                "deployment_id": reference["deployment_id"],
                "environment": reference["environment"],
            }
            for reference in active
        ]
        + [
            {
                "code": "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE",
                "reference_type": "draining-deployment",
                "deployment_id": reference["deployment_id"],
                "environment": reference["environment"],
            }
            for reference in draining
        ]
        + [
            {
                "code": "LABEL_VERSION_IN_FLIGHT_RUN_REFERENCE",
                "reference_type": "in-flight-run",
                "run_id": reference["run_id"],
                "environment": reference["environment"],
            }
            for reference in in_flight
        ]
    )
    safe_stop_required = replacement_label_version_id is None and any(
        str(reference["environment"]).lower() in _PRODUCTION_ENVIRONMENTS
        for reference in [*active, *draining, *in_flight]
    )
    return _EnvironmentReferences(
        active=active,
        draining=draining,
        in_flight=in_flight,
        blockers=blockers,
        safe_stop_required=safe_stop_required,
    )


def _preflight_id(
    ctx: RequestContext,
    *,
    label_version_id: str,
    expected_resource_version: int,
) -> str:
    digest = _sha256(
        {
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "label_version_id": label_version_id,
            "expected_resource_version": expected_resource_version,
            "idempotency_key": ctx.idempotency_key,
        }
    )
    return f"ldp_{digest}"


def create_label_version_deprecation_preflight(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    request: LabelVersionDeprecationPreflightRequest,
) -> dict[str, Any]:
    """Validate a deprecation against the current atomic database snapshot."""

    _require_human_project_admin(ctx)
    operation = f"label_version.deprecation_preflight:{label_version_id}"
    body_hash = _request_hash(label_version_id, request)
    replay = _idempotency_replay(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    locked = _lock_label_version(session, ctx, label_version_id)
    version = locked.version
    _assert_expected_resource_version(version, request.expected_resource_version)
    if version.artifact_status != "published" or version.status != "published":
        raise ApiError(
            "LABEL_VERSION_NOT_PUBLISHED",
            "只有已发布标签版本可以发起废弃预检",
            409,
        )
    _validate_replacement_binding(
        session,
        ctx,
        source=version,
        replacement_label_version_id=request.replacement_label_version_id,
        mapping_bundle_id=request.mapping_bundle_id,
    )
    references = _environment_references(
        session,
        ctx,
        label_version_id=label_version_id,
        replacement_label_version_id=request.replacement_label_version_id,
    )
    preflight_id = _preflight_id(
        ctx,
        label_version_id=label_version_id,
        expected_resource_version=request.expected_resource_version,
    )
    summary = {
        "preflight_id": preflight_id,
        "label_version_id": label_version_id,
        "expected_resource_version": request.expected_resource_version,
        "replacement_label_version_id": request.replacement_label_version_id,
        "mapping_bundle_id": request.mapping_bundle_id,
        "reason": request.reason,
        "active_environment_references": references.active,
        "draining_environment_references": references.draining,
        "in_flight_run_references": references.in_flight,
        "blockers": references.blockers,
        "ready_for_transition": not references.blockers,
        "safe_stop_required": references.safe_stop_required,
    }
    audit = record_audit(
        session,
        ctx,
        action="label_version.deprecation_preflight",
        object_type="label_version",
        object_id=label_version_id,
        result="success" if not references.blockers else "blocked",
        after=summary,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_version.deprecation_requested",
        aggregate_type="label_version_deprecation_preflight",
        aggregate_id=preflight_id,
        payload=summary,
    )
    session.flush()
    result = LabelVersionDeprecationPreflightResponse(
        preflight_id=preflight_id,
        label_version_id=label_version_id,
        expected_resource_version=request.expected_resource_version,
        replacement_label_version_id=request.replacement_label_version_id,
        mapping_bundle_id=request.mapping_bundle_id,
        active_environment_references=[
            LabelVersionEnvironmentReference.model_validate(reference)
            for reference in references.active
        ],
        draining_environment_references=[
            LabelVersionEnvironmentReference.model_validate(reference)
            for reference in references.draining
        ],
        in_flight_run_references=[
            LabelVersionInFlightRunReference.model_validate(reference)
            for reference in references.in_flight
        ],
        blockers=[
            LabelVersionLifecycleBlocker.model_validate(blocker) for blocker in references.blockers
        ],
        ready_for_transition=not references.blockers,
        safe_stop_required=references.safe_stop_required,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=result,
    )
    return result


def _raise_environment_reference_conflict(
    references: _EnvironmentReferences,
) -> None:
    if not references.blockers:
        return
    raise ApiError(
        "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE",
        "标签版本仍被生效 Head、draining 部署或在途运行引用",
        409,
        details=[
            {
                "safe_stop_required": references.safe_stop_required,
                "active_environment_references": references.active,
                "draining_environment_references": references.draining,
                "in_flight_run_references": references.in_flight,
                "blockers": references.blockers,
            }
        ],
    )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def enrich_label_version_lifecycle_views(
    session: Session,
    ctx: RequestContext,
    resources: list[dict[str, Any]],
    *,
    include_timeline: bool = False,
) -> list[dict[str, Any]]:
    """Separate artifact lifecycle from environment activation on read models."""

    resource_ids = {
        str(candidate)
        for resource in resources
        for candidate in (resource.get("label_version_id"), resource.get("id"))
        if isinstance(candidate, str) and candidate
    }
    if not resource_ids:
        return [dict(resource) for resource in resources]
    versions = list(
        session.scalars(
            select(LabelVersion).where(
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
                LabelVersion.label_version_id.in_(resource_ids),
            )
        )
    )
    heads = list(
        session.scalars(
            select(ReleaseBundleHead)
            .where(
                ReleaseBundleHead.tenant_id == ctx.tenant_id,
                ReleaseBundleHead.project_id == ctx.project_id,
                ReleaseBundleHead.label_version_id.in_(resource_ids),
                ReleaseBundleHead.status == "active",
            )
            .order_by(ReleaseBundleHead.environment, ReleaseBundleHead.generation)
        )
    )
    events: list[ReleaseBundleHeadEvent] = []
    if include_timeline:
        events = list(
            session.scalars(
                select(ReleaseBundleHeadEvent)
                .where(
                    ReleaseBundleHeadEvent.tenant_id == ctx.tenant_id,
                    ReleaseBundleHeadEvent.project_id == ctx.project_id,
                    (
                        ReleaseBundleHeadEvent.old_label_version_id.in_(resource_ids)
                        | ReleaseBundleHeadEvent.new_label_version_id.in_(resource_ids)
                    ),
                )
                .order_by(
                    ReleaseBundleHeadEvent.environment,
                    ReleaseBundleHeadEvent.generation,
                )
            )
        )
    version_by_id = {version.label_version_id: version for version in versions}
    heads_by_id: dict[str, list[ReleaseBundleHead]] = {item: [] for item in resource_ids}
    for head in heads:
        heads_by_id.setdefault(head.label_version_id, []).append(head)
    events_by_id: dict[str, list[ReleaseBundleHeadEvent]] = {item: [] for item in resource_ids}
    for event in events:
        for label_version_id in {event.old_label_version_id, event.new_label_version_id}:
            if label_version_id in resource_ids:
                events_by_id.setdefault(label_version_id, []).append(event)

    result: list[dict[str, Any]] = []
    for resource in resources:
        data = dict(resource)
        raw_id = data.get("label_version_id") or data.get("id")
        version = version_by_id.get(str(raw_id)) if raw_id is not None else None
        if version is None:
            result.append(data)
            continue
        version_heads = heads_by_id.get(version.label_version_id, [])
        activations = [
            {
                "environment": head.environment,
                "status": head.status,
                "generation": head.generation,
                "active_deployment_id": head.active_deployment_id,
                "active_bundle_sha256": head.active_bundle_sha256,
                "activated_by_command_id": head.activated_by_command_id,
                "trace_id": head.trace_id,
            }
            for head in version_heads
        ]
        artifact_status = version.artifact_status or version.status
        mapping_values = [
            version.payload.get("mapping_bundle_id"),
            data.get("mapping_bundle_id"),
        ]
        mapping_ids = {value for value in mapping_values if isinstance(value, str) and value}
        mapping_bundle_id = next(iter(mapping_ids), None) if len(mapping_ids) <= 1 else None
        data["artifact_lifecycle"] = {
            "status": artifact_status,
            "resource_version": version.resource_version,
            "published_at": _isoformat(version.artifact_published_at),
            "deprecated_at": _isoformat(version.artifact_deprecated_at),
            "archived_at": None,
            "deprecation_reason": version.deprecation_reason,
        }
        data["replacement"] = (
            {
                "label_version_id": version.replacement_label_version_id,
                "mapping_bundle_id": mapping_bundle_id,
            }
            if version.replacement_label_version_id is not None
            else None
        )
        data["environment_activations"] = activations
        data["activation_summary"] = {
            "active_environment_count": len(activations),
            "active_environments": sorted(head.environment for head in version_heads),
            "latest_generation": max(
                (head.generation for head in version_heads),
                default=None,
            ),
        }
        if include_timeline:
            data["activation_timeline"] = [
                {
                    "head_event_id": event.head_event_id,
                    "environment": event.environment,
                    "generation": event.generation,
                    "previous_generation": event.previous_generation,
                    "action": event.action,
                    "activation_status": event.activation_status,
                    "old_deployment_id": event.old_deployment_id,
                    "new_deployment_id": event.new_deployment_id,
                    "old_label_version_id": event.old_label_version_id,
                    "new_label_version_id": event.new_label_version_id,
                    "effective_from": _isoformat(event.effective_from),
                    "effective_to": _isoformat(event.effective_to),
                    "content_sha256": event.content_sha256,
                    "trace_id": event.trace_id,
                }
                for event in events_by_id.get(version.label_version_id, [])
            ]
        data["next_actions"] = (
            [{"key": "deprecation-preflight", "label": "执行废弃影响预检"}]
            if artifact_status == "published"
            else (
                [{"key": "archive", "label": "归档已废弃版本"}]
                if artifact_status == "deprecated"
                else []
            )
        )
        result.append(data)
    return result


def _existing_mapping_bundle_id(
    version: LabelVersion,
    projection: JsonResource,
) -> str | None:
    values = [
        version.payload.get("mapping_bundle_id"),
        projection.data.get("mapping_bundle_id"),
    ]
    normalized = {value for value in values if isinstance(value, str) and value}
    has_invalid = any(value is not None and not isinstance(value, str) for value in values)
    if has_invalid or len(normalized) > 1:
        raise ApiError(
            "LABEL_VERSION_DEPRECATION_BINDING_DRIFT",
            "已废弃标签版本的映射绑定不一致，归档已拒绝",
            409,
        )
    mapping_bundle_id = next(iter(normalized), None)
    if (version.replacement_label_version_id is None) != (mapping_bundle_id is None):
        raise ApiError(
            "LABEL_VERSION_DEPRECATION_BINDING_DRIFT",
            "已废弃标签版本的替代版本与映射包绑定不完整",
            409,
        )
    return mapping_bundle_id


def _materialize_lifecycle_payload(
    version: LabelVersion,
    projection: JsonResource,
    *,
    mapping_bundle_id: str | None,
    action: str,
    reason: str,
    occurred_at: datetime,
) -> dict[str, Any]:
    payload = {**projection.data, **version.payload}
    payload.update(
        {
            "id": version.label_version_id,
            "label_version_id": version.label_version_id,
            "taxonomy_id": version.taxonomy_id,
            "semantic_version": version.semantic_version,
            "status": version.status,
            "artifact_status": version.artifact_status,
            "resource_version": version.resource_version,
            "content_sha256": version.content_sha256,
            "artifact_published_at": _isoformat(version.artifact_published_at),
            "artifact_deprecated_at": _isoformat(version.artifact_deprecated_at),
            "deprecation_reason": version.deprecation_reason,
            "replacement_label_version_id": version.replacement_label_version_id,
            "mapping_bundle_id": mapping_bundle_id,
            "trace_id": version.trace_id,
        }
    )
    if action == "archive":
        payload.update(
            {
                "archive_reason": reason,
                "archived_at": occurred_at.isoformat(),
            }
        )
    return payload


def transition_label_version(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    request: LabelVersionTransitionRequest,
) -> dict[str, Any]:
    """Deprecate or archive a label version without rewriting historical facts."""

    _require_human_project_admin(ctx)
    operation = f"label_version.transition:{label_version_id}"
    body_hash = _request_hash(label_version_id, request)
    replay = _idempotency_replay(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    locked = _lock_label_version(session, ctx, label_version_id)
    version = locked.version
    projection = locked.projection
    _assert_expected_resource_version(version, request.expected_resource_version)

    if request.action == "deprecate":
        if version.artifact_status != "published" or version.status != "published":
            raise ApiError(
                "LABEL_VERSION_NOT_PUBLISHED",
                "只有已发布标签版本可以废弃",
                409,
            )
        _validate_replacement_binding(
            session,
            ctx,
            source=version,
            replacement_label_version_id=request.replacement_label_version_id,
            mapping_bundle_id=request.mapping_bundle_id,
        )
        mapping_bundle_id = request.mapping_bundle_id
        replacement_label_version_id = request.replacement_label_version_id
    else:
        if version.artifact_status != "deprecated" or version.status != "deprecated":
            raise ApiError(
                "LABEL_VERSION_NOT_DEPRECATED",
                "只有已废弃标签版本可以归档",
                409,
            )
        mapping_bundle_id = _existing_mapping_bundle_id(version, projection)
        replacement_label_version_id = version.replacement_label_version_id

    references = _environment_references(
        session,
        ctx,
        label_version_id=label_version_id,
        replacement_label_version_id=replacement_label_version_id,
    )
    _raise_environment_reference_conflict(references)

    before = {
        "status": version.status,
        "artifact_status": version.artifact_status,
        "resource_version": version.resource_version,
        "replacement_label_version_id": version.replacement_label_version_id,
        "mapping_bundle_id": version.payload.get("mapping_bundle_id"),
        "deprecation_reason": version.deprecation_reason,
    }
    occurred_at = datetime.now(UTC)
    target_status: Literal["deprecated", "archived"] = (
        "deprecated" if request.action == "deprecate" else "archived"
    )
    try:
        transition_label_version_artifact(
            version,
            target_status,
            occurred_at=occurred_at,
        )
    except LabelLifecycleDriftError as exc:
        raise ApiError(
            "LABEL_VERSION_LIFECYCLE_TRANSITION_CONFLICT",
            str(exc),
            409,
        ) from exc
    version.status = target_status
    if request.action == "deprecate":
        version.replacement_label_version_id = replacement_label_version_id
        version.deprecation_reason = request.reason
    version.resource_version += 1
    version.trace_id = ctx.trace_id
    payload = _materialize_lifecycle_payload(
        version,
        projection,
        mapping_bundle_id=mapping_bundle_id,
        action=request.action,
        reason=request.reason,
        occurred_at=occurred_at,
    )
    version.payload = payload
    projection.status = target_status
    projection.trace_id = ctx.trace_id
    projection.data = dict(payload)

    normalized_disposition: Literal["mapped-replacement", "coverage-gap"] = (
        "mapped-replacement" if replacement_label_version_id is not None else "coverage-gap"
    )
    after = {
        "status": target_status,
        "artifact_status": target_status,
        "resource_version": version.resource_version,
        "replacement_label_version_id": replacement_label_version_id,
        "mapping_bundle_id": mapping_bundle_id,
        "deprecation_reason": version.deprecation_reason,
        "normalized_disposition": normalized_disposition,
    }
    event_type = f"label_version.{target_status}"
    audit = record_audit(
        session,
        ctx,
        action=event_type,
        object_type="label_version",
        object_id=label_version_id,
        before=before,
        after=after,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type="label_version",
        aggregate_id=label_version_id,
        payload={
            **after,
            "label_version_id": label_version_id,
            "reason": request.reason,
        },
    )
    session.flush()
    result = LabelVersionTransitionResponse(
        label_version_id=label_version_id,
        action=request.action,
        status=target_status,
        artifact_status=target_status,
        resource_version=version.resource_version,
        replacement_label_version_id=replacement_label_version_id,
        mapping_bundle_id=mapping_bundle_id,
        normalized_disposition=normalized_disposition,
        safe_stop_required=False,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=result,
    )
    return result
