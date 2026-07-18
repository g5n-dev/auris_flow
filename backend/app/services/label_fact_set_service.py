from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.models import (
    AuditLog,
    IdempotencyRecord,
    LabelFactSet,
    LabelFactSetHead,
    LabelFactSetHeadEvent,
    LabelVersion,
    OutboxEvent,
)
from app.schemas.label_fact_sets import (
    LabelFactSetApproveRequest,
    LabelFactSetCreateRequest,
    LabelFactSetHeadChainVerification,
    LabelFactSetMutationResponse,
    LabelFactSetPromoteRequest,
    LabelFactSetPromotionResponse,
    LabelFactSetValidateRequest,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    require_idempotency,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event

CREATE_OPERATION = "label-fact-sets.create"
VALIDATE_OPERATION = "label-fact-sets.validate"
APPROVE_OPERATION = "label-fact-sets.approve"
PROMOTE_OPERATION = "label-fact-sets.promote"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WRITER_ROLES = ("project_admin", "model_engineer")
_TARGET_ARTIFACT_STATUSES = frozenset({"approved", "published"})
_FACT_SET_STATUSES = frozenset(
    {"candidate", "validated", "approved", "published", "superseded", "archived"}
)


def _strict_canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ApiError(
            "LABEL_FACT_SET_MANIFEST_NOT_CANONICAL",
            "FactSet manifest 必须是严格 JSON，且不能包含 NaN/Infinity",
            422,
        ) from error


def strict_canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_strict_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_clone(value: Any) -> Any:
    return json.loads(_strict_canonical_json(value))


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _request_hash(
    ctx: RequestContext,
    *,
    operation: str,
    resource_id: str | None,
    body: dict[str, Any],
) -> str:
    return strict_canonical_sha256(
        {
            "actor": {
                "actor_kind": ctx.actor_kind,
                "user_id": ctx.user_id,
            },
            "body": body,
            "operation": operation,
            "resource_id": resource_id,
        }
    )


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
            "LABEL_FACT_SET_IDEMPOTENCY_ACTOR_CONFLICT",
            "该 FactSet 幂等键已由另一操作人使用",
            409,
        )
    return replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )


def _save_idempotency(
    session: Session,
    ctx: RequestContext,
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
    return response


def _require_writer(ctx: RequestContext, *, action: str) -> None:
    require_any_role(ctx, _WRITER_ROLES, action=action)


def _require_human_project_admin(
    ctx: RequestContext,
    *,
    action: str,
    agent_error_code: str,
) -> None:
    if ctx.actor_kind != "human" or ctx.user_id == "system" or "system" in ctx.roles:
        raise ApiError(
            agent_error_code,
            "FactSet 审批与生产 Head 切换只能由人工项目管理员执行",
            403,
        )
    if "project_admin" not in ctx.roles:
        raise ApiError(
            "FORBIDDEN",
            f"仅项目管理员可以执行：{action}",
            403,
            details=[
                {
                    "action": action,
                    "required_roles": ["project_admin"],
                    "roles": list(ctx.roles),
                }
            ],
        )


def _target_version(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    *,
    for_update: bool,
    missing_code: str,
) -> LabelVersion:
    statement = select(LabelVersion).where(
        LabelVersion.tenant_id == ctx.tenant_id,
        LabelVersion.project_id == ctx.project_id,
        LabelVersion.label_version_id == label_version_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    version = session.scalar(statement)
    if version is None:
        raise ApiError(missing_code, "FactSet 目标标签版本锚点不存在", 404)
    return version


def _target_version_anchor(version: LabelVersion) -> dict[str, Any]:
    invalid_fields: list[str] = []
    if not isinstance(version.resource_version, int) or version.resource_version <= 0:
        invalid_fields.append("resource_version")
    if not _valid_sha256(version.content_sha256):
        invalid_fields.append("content_sha256")
    if version.artifact_status not in _TARGET_ARTIFACT_STATUSES:
        invalid_fields.append("artifact_status")
    if invalid_fields:
        raise ApiError(
            "LABEL_FACT_SET_TARGET_ANCHOR_INCOMPLETE",
            "FactSet 目标标签版本尚未形成可冻结强锚点",
            409,
            details=[
                {
                    "artifact_status": version.artifact_status,
                    "label_version_id": version.label_version_id,
                    "missing_or_invalid_fields": invalid_fields,
                }
            ],
        )
    assert version.content_sha256 is not None
    return {
        "artifact_status": version.artifact_status,
        "content_sha256": version.content_sha256,
        "label_version_id": version.label_version_id,
        "resource_version": version.resource_version,
    }


def _manifest_document(
    *,
    tenant_id: str,
    project_id: str,
    fact_namespace: str,
    target_version_anchor: dict[str, Any],
    fact_as_of: datetime,
    partition_manifest: dict[str, Any],
    partition_manifest_sha256: str,
    source_manifest_sha256: str,
    result_manifest_sha256: str,
    row_count: int,
) -> dict[str, Any]:
    return {
        "fact_as_of": _iso_utc(fact_as_of),
        "fact_namespace": fact_namespace,
        "partition_manifest": _canonical_clone(partition_manifest),
        "partition_manifest_sha256": partition_manifest_sha256,
        "project_id": project_id,
        "result_manifest_sha256": result_manifest_sha256,
        "row_count": row_count,
        "schema_version": "auris.label-fact-set-manifest/1",
        "source_manifest_sha256": source_manifest_sha256,
        "target_label_version": _canonical_clone(target_version_anchor),
        "tenant_id": tenant_id,
    }


def _fact_set_id(ctx: RequestContext, manifest_sha256: str) -> str:
    digest = strict_canonical_sha256(
        {
            "manifest_sha256": manifest_sha256,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        }
    )
    return f"lfs_{digest[:24]}"


def _head_id(ctx: RequestContext, environment: str, fact_namespace: str) -> str:
    digest = strict_canonical_sha256(
        {
            "environment": environment,
            "fact_namespace": fact_namespace,
            "project_id": ctx.project_id,
            "tenant_id": ctx.tenant_id,
        }
    )
    return f"lfsh_{digest[:24]}"


def _validate_partition_anchor(request: LabelFactSetCreateRequest) -> None:
    actual = strict_canonical_sha256(request.partition_manifest)
    if actual != request.partition_manifest_sha256:
        raise ApiError(
            "LABEL_FACT_SET_PARTITION_HASH_MISMATCH",
            "partition_manifest 与其 SHA-256 锚点不一致",
            409,
            details=[
                {
                    "actual_partition_manifest_sha256": actual,
                    "expected_partition_manifest_sha256": request.partition_manifest_sha256,
                }
            ],
        )


def _load_fact_set(
    session: Session,
    ctx: RequestContext,
    fact_set_id: str,
    *,
    for_update: bool,
) -> LabelFactSet:
    statement = select(LabelFactSet).where(
        LabelFactSet.tenant_id == ctx.tenant_id,
        LabelFactSet.project_id == ctx.project_id,
        LabelFactSet.fact_set_id == fact_set_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    fact_set = session.scalar(statement)
    if fact_set is None:
        raise ApiError(
            "LABEL_FACT_SET_NOT_FOUND",
            "FactSet 不存在于当前租户项目范围",
            404,
        )
    return fact_set


def _assert_fact_set_integrity(
    session: Session,
    ctx: RequestContext,
    fact_set: LabelFactSet,
    *,
    for_update: bool,
) -> None:
    missing_fields: list[str] = []
    if not isinstance(fact_set.fact_namespace, str) or not fact_set.fact_namespace:
        missing_fields.append("fact_namespace")
    if not isinstance(fact_set.partition_manifest, dict) or not fact_set.partition_manifest:
        missing_fields.append("partition_manifest")
    for field_name in (
        "partition_manifest_sha256",
        "source_manifest_sha256",
        "result_manifest_sha256",
        "manifest_sha256",
    ):
        if not _valid_sha256(getattr(fact_set, field_name)):
            missing_fields.append(field_name)
    if not isinstance(fact_set.row_count, int) or fact_set.row_count < 0:
        missing_fields.append("row_count")
    if fact_set.status not in _FACT_SET_STATUSES:
        missing_fields.append("status")
    if not isinstance(fact_set.root_trace_id, str) or not fact_set.root_trace_id:
        missing_fields.append("root_trace_id")
    if not isinstance(fact_set.action_trace_id, str) or not fact_set.action_trace_id:
        missing_fields.append("action_trace_id")
    if not isinstance(fact_set.trace_id, str) or not fact_set.trace_id:
        missing_fields.append("trace_id")
    if missing_fields:
        raise ApiError(
            "LABEL_FACT_SET_MANIFEST_INCOMPLETE",
            "FactSet manifest、行数或 Trace 锚点不完整",
            409,
            details=[
                {
                    "fact_set_id": fact_set.fact_set_id,
                    "missing_or_invalid_fields": missing_fields,
                }
            ],
        )

    payload = fact_set.payload
    if not isinstance(payload, dict):
        raise ApiError(
            "LABEL_FACT_SET_CONTENT_DRIFT",
            "FactSet 缺少冻结 manifest payload",
            409,
        )
    frozen_manifest = payload.get("frozen_manifest")
    trace_anchor = payload.get("trace_anchor")
    if not isinstance(frozen_manifest, dict) or trace_anchor != {
        "action_trace_id": fact_set.action_trace_id,
        "root_trace_id": fact_set.root_trace_id,
    }:
        raise ApiError(
            "LABEL_FACT_SET_CONTENT_DRIFT",
            "FactSet 冻结 manifest 或 Trace 锚点已漂移",
            409,
            details=[{"fact_set_id": fact_set.fact_set_id}],
        )
    frozen_target = frozen_manifest.get("target_label_version")
    if not isinstance(frozen_target, dict):
        raise ApiError(
            "LABEL_FACT_SET_CONTENT_DRIFT",
            "FactSet 缺少冻结目标标签版本锚点",
            409,
        )

    version = _target_version(
        session,
        ctx,
        fact_set.target_label_version_id,
        for_update=for_update,
        missing_code="LABEL_FACT_SET_TARGET_ANCHOR_MISSING",
    )
    current_target = _target_version_anchor(version)
    if current_target != frozen_target:
        raise ApiError(
            "LABEL_FACT_SET_TARGET_ANCHOR_DRIFT",
            "FactSet 目标标签版本强锚点已变化",
            409,
            details=[
                {
                    "actual_target_anchor": current_target,
                    "expected_target_anchor": frozen_target,
                    "fact_set_id": fact_set.fact_set_id,
                }
            ],
        )

    actual_partition_sha256 = strict_canonical_sha256(fact_set.partition_manifest)
    if actual_partition_sha256 != fact_set.partition_manifest_sha256:
        raise ApiError(
            "LABEL_FACT_SET_CONTENT_DRIFT",
            "FactSet partition manifest 已漂移",
            409,
            details=[{"fact_set_id": fact_set.fact_set_id}],
        )
    expected_manifest = _manifest_document(
        tenant_id=fact_set.tenant_id,
        project_id=fact_set.project_id,
        fact_namespace=fact_set.fact_namespace,
        target_version_anchor=frozen_target,
        fact_as_of=fact_set.fact_as_of,
        partition_manifest=fact_set.partition_manifest,
        partition_manifest_sha256=fact_set.partition_manifest_sha256,
        source_manifest_sha256=fact_set.source_manifest_sha256,
        result_manifest_sha256=fact_set.result_manifest_sha256,
        row_count=fact_set.row_count,
    )
    if frozen_manifest != expected_manifest or (
        strict_canonical_sha256(expected_manifest) != fact_set.manifest_sha256
    ):
        raise ApiError(
            "LABEL_FACT_SET_CONTENT_DRIFT",
            "FactSet 行字段与服务端冻结 manifest 不一致",
            409,
            details=[{"fact_set_id": fact_set.fact_set_id}],
        )
    if fact_set.status in {"approved", "published"} and (
        not fact_set.approval_id
        or not fact_set.approved_by
        or fact_set.approved_by == "system"
        or fact_set.approved_at is None
    ):
        raise ApiError(
            "LABEL_FACT_SET_APPROVAL_ANCHOR_MISSING",
            "已审批或发布的 FactSet 缺少人工审批锚点",
            409,
            details=[{"fact_set_id": fact_set.fact_set_id}],
        )
    if isinstance(fact_set.payload.get("recompute_run_id"), str):
        # Import lazily to keep the FactSet primitive reusable while the
        # recomputation service itself uses its canonical manifest helpers.
        from app.services.label_recomputation_service import (
            assert_recompute_fact_set_materialized,
        )

        assert_recompute_fact_set_materialized(session, ctx, fact_set)


def _assert_expected_manifest(
    fact_set: LabelFactSet,
    expected_manifest_sha256: str,
) -> None:
    if fact_set.manifest_sha256 == expected_manifest_sha256:
        return
    raise ApiError(
        "LABEL_FACT_SET_MANIFEST_CONFLICT",
        "FactSet manifest SHA 已变化",
        409,
        details=[
            {
                "actual_manifest_sha256": fact_set.manifest_sha256,
                "expected_manifest_sha256": expected_manifest_sha256,
                "fact_set_id": fact_set.fact_set_id,
            }
        ],
    )


def _fact_set_summary(fact_set: LabelFactSet) -> dict[str, Any]:
    return {
        "action_trace_id": fact_set.action_trace_id,
        "approval_id": fact_set.approval_id,
        "approved_at": (
            _iso_utc(fact_set.approved_at) if fact_set.approved_at is not None else None
        ),
        "approved_by": fact_set.approved_by,
        "fact_as_of": _iso_utc(fact_set.fact_as_of),
        "fact_namespace": fact_set.fact_namespace,
        "fact_set_id": fact_set.fact_set_id,
        "manifest_sha256": fact_set.manifest_sha256,
        "partition_manifest_sha256": fact_set.partition_manifest_sha256,
        "result_manifest_sha256": fact_set.result_manifest_sha256,
        "root_trace_id": fact_set.root_trace_id,
        "row_count": fact_set.row_count,
        "source_manifest_sha256": fact_set.source_manifest_sha256,
        "status": fact_set.status,
        "target_label_version_id": fact_set.target_label_version_id,
    }


def _mutation_response(
    fact_set: LabelFactSet,
    ctx: RequestContext,
    *,
    audit_id: int,
    outbox_event_id: int,
) -> dict[str, Any]:
    return LabelFactSetMutationResponse(
        fact_set_id=fact_set.fact_set_id,
        fact_namespace=fact_set.fact_namespace,
        target_label_version_id=fact_set.target_label_version_id,
        status=cast(
            Literal["candidate", "validated", "approved", "published"],
            fact_set.status,
        ),
        manifest_sha256=fact_set.manifest_sha256,
        row_count=fact_set.row_count,
        audit_id=audit_id,
        outbox_event_id=outbox_event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")


def _record_fact_set_change(
    session: Session,
    ctx: RequestContext,
    fact_set: LabelFactSet,
    *,
    action: Literal["created", "validated", "approved"],
    before: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> tuple[AuditLog, OutboxEvent]:
    after = {**_fact_set_summary(fact_set), **(extra or {})}
    audit = record_audit(
        session,
        ctx,
        action=f"label_fact_set.{action}",
        object_type="label_fact_set",
        object_id=fact_set.fact_set_id,
        before=before,
        after=after,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type=f"label_fact_set.{action}",
        aggregate_type="label_fact_set",
        aggregate_id=fact_set.fact_set_id,
        payload=after,
    )
    session.flush()
    return audit, outbox_event


def _creation_refs(
    session: Session,
    ctx: RequestContext,
    fact_set_id: str,
) -> tuple[int, int]:
    audit = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == ctx.tenant_id,
            AuditLog.project_id == ctx.project_id,
            AuditLog.action == "label_fact_set.created",
            AuditLog.object_id == fact_set_id,
        )
        .order_by(AuditLog.audit_id)
        .limit(1)
    )
    outbox_event = session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == ctx.tenant_id,
            OutboxEvent.project_id == ctx.project_id,
            OutboxEvent.event_type == "label_fact_set.created",
            OutboxEvent.aggregate_id == fact_set_id,
        )
        .order_by(OutboxEvent.event_id)
        .limit(1)
    )
    if audit is None or outbox_event is None:
        raise ApiError(
            "LABEL_FACT_SET_CREATION_ANCHOR_MISSING",
            "FactSet 缺少创建 Audit/Outbox 锚点",
            409,
        )
    return audit.audit_id, outbox_event.event_id


def create_label_fact_set(
    session: Session,
    ctx: RequestContext,
    request: LabelFactSetCreateRequest,
) -> dict[str, Any]:
    """Freeze a scoped candidate FactSet without making it production-visible."""

    _require_writer(ctx, action="label-fact-sets.create")
    body = request.model_dump(mode="json")
    body_hash = _request_hash(
        ctx,
        operation=CREATE_OPERATION,
        resource_id=None,
        body=body,
    )
    replay = _idempotency_replay(
        session,
        ctx,
        operation=CREATE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    _validate_partition_anchor(request)
    version = _target_version(
        session,
        ctx,
        request.target_label_version_id,
        for_update=True,
        missing_code="LABEL_FACT_SET_TARGET_VERSION_NOT_FOUND",
    )
    target_anchor = _target_version_anchor(version)
    manifest_document = _manifest_document(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        fact_namespace=request.fact_namespace,
        target_version_anchor=target_anchor,
        fact_as_of=request.fact_as_of,
        partition_manifest=request.partition_manifest,
        partition_manifest_sha256=request.partition_manifest_sha256,
        source_manifest_sha256=request.source_manifest_sha256,
        result_manifest_sha256=request.result_manifest_sha256,
        row_count=request.row_count,
    )
    manifest_sha256 = strict_canonical_sha256(manifest_document)
    existing = session.scalar(
        select(LabelFactSet)
        .where(
            LabelFactSet.tenant_id == ctx.tenant_id,
            LabelFactSet.project_id == ctx.project_id,
            LabelFactSet.manifest_sha256 == manifest_sha256,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        _assert_fact_set_integrity(session, ctx, existing, for_update=True)
        audit_id, outbox_event_id = _creation_refs(
            session,
            ctx,
            existing.fact_set_id,
        )
        response = _mutation_response(
            existing,
            ctx,
            audit_id=audit_id,
            outbox_event_id=outbox_event_id,
        )
        return _save_idempotency(
            session,
            ctx,
            operation=CREATE_OPERATION,
            body_hash=body_hash,
            status_code=201,
            response=response,
        )

    fact_set = LabelFactSet(
        fact_set_id=_fact_set_id(ctx, manifest_sha256),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        fact_namespace=request.fact_namespace,
        target_label_version_id=request.target_label_version_id,
        status="candidate",
        fact_as_of=request.fact_as_of,
        partition_manifest=_canonical_clone(request.partition_manifest),
        partition_manifest_sha256=request.partition_manifest_sha256,
        source_manifest_sha256=request.source_manifest_sha256,
        result_manifest_sha256=request.result_manifest_sha256,
        row_count=request.row_count,
        manifest_sha256=manifest_sha256,
        approval_id=None,
        approved_by=None,
        approved_at=None,
        root_trace_id=ctx.trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload={
            "frozen_manifest": manifest_document,
            "schema_version": "auris.label-fact-set/1",
            "trace_anchor": {
                "action_trace_id": ctx.trace_id,
                "root_trace_id": ctx.trace_id,
            },
        },
    )
    session.add(fact_set)
    try:
        session.flush()
    except IntegrityError as error:
        raise ApiError(
            "LABEL_FACT_SET_CREATE_CONFLICT",
            "同内容 FactSet 已被并发创建",
            409,
            retryable=True,
        ) from error
    audit, outbox_event = _record_fact_set_change(
        session,
        ctx,
        fact_set,
        action="created",
        before=None,
    )
    response = _mutation_response(
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
    )
    return _save_idempotency(
        session,
        ctx,
        operation=CREATE_OPERATION,
        body_hash=body_hash,
        status_code=201,
        response=response,
    )


def validate_label_fact_set(
    session: Session,
    ctx: RequestContext,
    fact_set_id: str,
    request: LabelFactSetValidateRequest,
) -> dict[str, Any]:
    _require_writer(ctx, action="label-fact-sets.validate")
    body_hash = _request_hash(
        ctx,
        operation=VALIDATE_OPERATION,
        resource_id=fact_set_id,
        body=request.model_dump(mode="json"),
    )
    replay = _idempotency_replay(
        session,
        ctx,
        operation=VALIDATE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    fact_set = _load_fact_set(session, ctx, fact_set_id, for_update=True)
    _assert_expected_manifest(fact_set, request.expected_manifest_sha256)
    _assert_fact_set_integrity(session, ctx, fact_set, for_update=True)
    if fact_set.status != "candidate":
        raise ApiError(
            "LABEL_FACT_SET_STATE_CONFLICT",
            "只有 candidate FactSet 可以进入 validated",
            409,
            details=[{"fact_set_id": fact_set_id, "status": fact_set.status}],
        )
    before = _fact_set_summary(fact_set)
    fact_set.status = "validated"
    session.flush()
    audit, outbox_event = _record_fact_set_change(
        session,
        ctx,
        fact_set,
        action="validated",
        before=before,
    )
    response = _mutation_response(
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
    )
    return _save_idempotency(
        session,
        ctx,
        operation=VALIDATE_OPERATION,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


def approve_label_fact_set(
    session: Session,
    ctx: RequestContext,
    fact_set_id: str,
    request: LabelFactSetApproveRequest,
) -> dict[str, Any]:
    _require_human_project_admin(
        ctx,
        action="label-fact-sets.approve",
        agent_error_code="AGENT_LABEL_FACT_SET_APPROVAL_FORBIDDEN",
    )
    body_hash = _request_hash(
        ctx,
        operation=APPROVE_OPERATION,
        resource_id=fact_set_id,
        body=request.model_dump(mode="json"),
    )
    replay = _idempotency_replay(
        session,
        ctx,
        operation=APPROVE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    fact_set = _load_fact_set(session, ctx, fact_set_id, for_update=True)
    _assert_expected_manifest(fact_set, request.expected_manifest_sha256)
    _assert_fact_set_integrity(session, ctx, fact_set, for_update=True)
    if fact_set.status != "validated":
        raise ApiError(
            "LABEL_FACT_SET_STATE_CONFLICT",
            "只有 validated FactSet 可以由人工项目管理员审批",
            409,
            details=[{"fact_set_id": fact_set_id, "status": fact_set.status}],
        )
    before = _fact_set_summary(fact_set)
    fact_set.status = "approved"
    fact_set.approval_id = request.approval_id
    fact_set.approved_by = ctx.user_id
    fact_set.approved_at = _now()
    session.flush()
    audit, outbox_event = _record_fact_set_change(
        session,
        ctx,
        fact_set,
        action="approved",
        before=before,
        extra={"approval_reason_sha256": strict_canonical_sha256({"reason": request.reason})},
    )
    response = _mutation_response(
        fact_set,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
    )
    return _save_idempotency(
        session,
        ctx,
        operation=APPROVE_OPERATION,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )


def label_fact_set_head_event_hash_document(
    event: LabelFactSetHeadEvent,
) -> dict[str, Any]:
    return {
        "action": event.action,
        "action_trace_id": event.action_trace_id,
        "actor_id": event.actor_id,
        "approval_id": event.approval_id,
        "effective_at": _iso_utc(event.effective_at),
        "environment": event.environment,
        "fact_namespace": event.fact_namespace,
        "generation": event.generation,
        "new_fact_set_id": event.new_fact_set_id,
        "new_manifest_sha256": event.new_manifest_sha256,
        "old_fact_set_id": event.old_fact_set_id,
        "old_manifest_sha256": event.old_manifest_sha256,
        "payload": event.payload,
        "previous_generation": event.previous_generation,
        "project_id": event.project_id,
        "root_trace_id": event.root_trace_id,
        "tenant_id": event.tenant_id,
        "trace_id": event.trace_id,
    }


def label_fact_set_head_event_content_sha256(event: LabelFactSetHeadEvent) -> str:
    return strict_canonical_sha256(label_fact_set_head_event_hash_document(event))


def _load_head(
    session: Session,
    ctx: RequestContext,
    *,
    environment: str,
    fact_namespace: str,
    for_update: bool,
) -> LabelFactSetHead | None:
    statement = select(LabelFactSetHead).where(
        LabelFactSetHead.tenant_id == ctx.tenant_id,
        LabelFactSetHead.project_id == ctx.project_id,
        LabelFactSetHead.environment == environment,
        LabelFactSetHead.fact_namespace == fact_namespace,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def _load_head_events(
    session: Session,
    ctx: RequestContext,
    *,
    environment: str,
    fact_namespace: str,
    for_update: bool,
) -> list[LabelFactSetHeadEvent]:
    statement = (
        select(LabelFactSetHeadEvent)
        .where(
            LabelFactSetHeadEvent.tenant_id == ctx.tenant_id,
            LabelFactSetHeadEvent.project_id == ctx.project_id,
            LabelFactSetHeadEvent.environment == environment,
            LabelFactSetHeadEvent.fact_namespace == fact_namespace,
        )
        .order_by(LabelFactSetHeadEvent.generation)
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return list(session.scalars(statement))


def _verify_head_chain(
    head: LabelFactSetHead,
    events: list[LabelFactSetHeadEvent],
) -> dict[str, Any]:
    if not events:
        raise ApiError(
            "LABEL_FACT_SET_HEAD_LEDGER_MISSING",
            "FactSet Head 缺少 append-only ledger 锚点",
            409,
            details=[{"fact_set_head_id": head.fact_set_head_id}],
        )
    previous: LabelFactSetHeadEvent | None = None
    for expected_generation, event in enumerate(events, start=1):
        if event.generation != expected_generation:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
                "FactSet Head ledger generation 不连续",
                409,
            )
        previous_hash = previous.content_sha256 if previous is not None else None
        payload_previous_hash = (
            event.payload.get("previous_event_sha256") if isinstance(event.payload, dict) else None
        )
        if payload_previous_hash != previous_hash:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
                "FactSet Head ledger previous hash 不连续",
                409,
            )
        if previous is None:
            predecessor_matches = (
                event.previous_generation is None
                and event.old_fact_set_id is None
                and event.old_manifest_sha256 is None
                and event.action == "bootstrap"
            )
        else:
            predecessor_matches = (
                event.previous_generation == previous.generation
                and event.old_fact_set_id == previous.new_fact_set_id
                and event.old_manifest_sha256 == previous.new_manifest_sha256
            )
        if not predecessor_matches:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
                "FactSet Head ledger pointer 不连续",
                409,
            )
        if label_fact_set_head_event_content_sha256(event) != event.content_sha256:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_EVENT_HASH_DRIFT",
                "FactSet Head event content hash 不可验证",
                409,
                details=[{"head_event_id": event.head_event_id}],
            )
        previous = event

    latest = events[-1]
    latest_hash = latest.content_sha256
    head_payload_hash = (
        head.payload.get("last_event_sha256") if isinstance(head.payload, dict) else None
    )
    head_matches = (
        head.generation == latest.generation
        and head.current_fact_set_id == latest.new_fact_set_id
        and head.current_manifest_sha256 == latest.new_manifest_sha256
        and head.previous_fact_set_id == latest.old_fact_set_id
        and head.previous_manifest_sha256 == latest.old_manifest_sha256
        and head_payload_hash == latest_hash
    )
    if not head_matches:
        raise ApiError(
            "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
            "FactSet Head 与 append-only ledger 最新锚点不一致",
            409,
            details=[{"fact_set_head_id": head.fact_set_head_id}],
        )
    return LabelFactSetHeadChainVerification(
        event_count=len(events),
        generation=latest.generation,
        head_event_sha256=latest_hash,
        head_id=head.fact_set_head_id,
    ).model_dump(mode="json")


def verify_label_fact_set_head_chain(
    session: Session,
    ctx: RequestContext,
    *,
    environment: str,
    fact_namespace: str,
) -> dict[str, Any]:
    head = _load_head(
        session,
        ctx,
        environment=environment,
        fact_namespace=fact_namespace,
        for_update=False,
    )
    if head is None:
        raise ApiError(
            "LABEL_FACT_SET_HEAD_NOT_FOUND",
            "FactSet Head 不存在于当前租户项目范围",
            404,
        )
    events = _load_head_events(
        session,
        ctx,
        environment=environment,
        fact_namespace=fact_namespace,
        for_update=False,
    )
    return _verify_head_chain(head, events)


def _new_head_event(
    ctx: RequestContext,
    *,
    fact_set_head_id: str,
    environment: str,
    fact_namespace: str,
    generation: int,
    previous_generation: int | None,
    action: Literal["bootstrap", "promote", "rollback"],
    old_fact_set_id: str | None,
    old_manifest_sha256: str | None,
    target: LabelFactSet,
    previous_event_sha256: str | None,
    root_trace_id: str,
) -> LabelFactSetHeadEvent:
    effective_at = _now()
    payload = {
        "fact_set_head_id": fact_set_head_id,
        "previous_event_sha256": previous_event_sha256,
        "schema_version": "auris.label-fact-set-head-event/1",
    }
    event = LabelFactSetHeadEvent(
        head_event_id="pending",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        environment=environment,
        fact_namespace=fact_namespace,
        generation=generation,
        previous_generation=previous_generation,
        action=action,
        old_fact_set_id=old_fact_set_id,
        old_manifest_sha256=old_manifest_sha256,
        new_fact_set_id=target.fact_set_id,
        new_manifest_sha256=target.manifest_sha256,
        approval_id=target.approval_id,
        effective_at=effective_at,
        content_sha256="0" * 64,
        actor_id=ctx.user_id,
        root_trace_id=root_trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=ctx.trace_id,
        payload=payload,
    )
    content_sha256 = label_fact_set_head_event_content_sha256(event)
    event.head_event_id = f"lfshe_{content_sha256[:24]}"
    event.content_sha256 = content_sha256
    return event


def _head_snapshot(head: LabelFactSetHead) -> dict[str, Any]:
    return {
        "current_fact_set_id": head.current_fact_set_id,
        "current_manifest_sha256": head.current_manifest_sha256,
        "environment": head.environment,
        "fact_namespace": head.fact_namespace,
        "fact_set_head_id": head.fact_set_head_id,
        "generation": head.generation,
        "previous_fact_set_id": head.previous_fact_set_id,
        "previous_manifest_sha256": head.previous_manifest_sha256,
        "status": head.status,
    }


def _promotion_summary(
    head: LabelFactSetHead,
    event: LabelFactSetHeadEvent,
    target: LabelFactSet,
) -> dict[str, Any]:
    return {
        **_head_snapshot(head),
        "action": event.action,
        "action_trace_id": event.action_trace_id,
        "approval_id": target.approval_id,
        "approved_at": (_iso_utc(target.approved_at) if target.approved_at is not None else None),
        "approved_by": target.approved_by,
        "fact_as_of": _iso_utc(target.fact_as_of),
        "fact_set_status": target.status,
        "head_event_id": event.head_event_id,
        "head_event_sha256": event.content_sha256,
        "manifest_sha256": target.manifest_sha256,
        "partition_manifest_sha256": target.partition_manifest_sha256,
        "previous_generation": event.previous_generation,
        "resource_version": event.generation,
        "result_manifest_sha256": target.result_manifest_sha256,
        "root_trace_id": target.root_trace_id,
        "row_count": target.row_count,
        "source_manifest_sha256": target.source_manifest_sha256,
        "target_label_version_id": target.target_label_version_id,
        "trace_id": event.trace_id,
    }


def _promotion_response(
    head: LabelFactSetHead,
    event: LabelFactSetHeadEvent,
    ctx: RequestContext,
    *,
    audit_id: int,
    outbox_event_id: int,
) -> dict[str, Any]:
    return LabelFactSetPromotionResponse(
        fact_set_head_id=head.fact_set_head_id,
        head_event_id=event.head_event_id,
        environment=head.environment,
        fact_namespace=head.fact_namespace,
        action=cast(Literal["bootstrap", "promote", "rollback"], event.action),
        generation=head.generation,
        previous_generation=event.previous_generation,
        current_fact_set_id=head.current_fact_set_id,
        current_manifest_sha256=head.current_manifest_sha256,
        previous_fact_set_id=head.previous_fact_set_id,
        previous_manifest_sha256=head.previous_manifest_sha256,
        head_event_sha256=event.content_sha256,
        audit_id=audit_id,
        outbox_event_id=outbox_event_id,
        trace_id=ctx.trace_id,
    ).model_dump(mode="json")


def _record_promotion(
    session: Session,
    ctx: RequestContext,
    *,
    head: LabelFactSetHead,
    event: LabelFactSetHeadEvent,
    target: LabelFactSet,
    before: dict[str, Any] | None,
) -> tuple[AuditLog, OutboxEvent]:
    after = _promotion_summary(head, event, target)
    audit = record_audit(
        session,
        ctx,
        action=f"label_fact_set_head.{event.action}",
        object_type="label_fact_set_head",
        object_id=head.fact_set_head_id,
        before=before,
        after=after,
    )
    outbox_event = enqueue_event(
        session,
        ctx,
        event_type="label_fact_set.promoted",
        aggregate_type="label_fact_set_head_event",
        aggregate_id=event.head_event_id,
        payload=after,
    )
    session.flush()
    return audit, outbox_event


def _assert_promotable_target(
    target: LabelFactSet,
    *,
    action: Literal["bootstrap", "promote", "rollback"],
) -> None:
    expected_status = "published" if action == "rollback" else "approved"
    if target.status != expected_status:
        raise ApiError(
            "LABEL_FACT_SET_STATE_CONFLICT",
            f"{action} 目标 FactSet 必须处于 {expected_status} 状态",
            409,
            details=[
                {
                    "action": action,
                    "fact_set_id": target.fact_set_id,
                    "status": target.status,
                }
            ],
        )


def promote_label_fact_set(
    session: Session,
    ctx: RequestContext,
    fact_set_id: str,
    request: LabelFactSetPromoteRequest,
) -> dict[str, Any]:
    """CAS-switch one complete FactSet Head and append its ledger event atomically."""

    _require_human_project_admin(
        ctx,
        action="label-fact-sets.promote",
        agent_error_code="AGENT_LABEL_FACT_SET_PROMOTION_FORBIDDEN",
    )
    body_hash = _request_hash(
        ctx,
        operation=PROMOTE_OPERATION,
        resource_id=fact_set_id,
        body=request.model_dump(mode="json"),
    )
    replay = _idempotency_replay(
        session,
        ctx,
        operation=PROMOTE_OPERATION,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    target = _load_fact_set(session, ctx, fact_set_id, for_update=True)
    _assert_fact_set_integrity(session, ctx, target, for_update=True)
    _assert_promotable_target(target, action=request.action)
    head = _load_head(
        session,
        ctx,
        environment=request.environment,
        fact_namespace=target.fact_namespace,
        for_update=True,
    )

    if head is None:
        orphan_events = _load_head_events(
            session,
            ctx,
            environment=request.environment,
            fact_namespace=target.fact_namespace,
            for_update=True,
        )
        if orphan_events:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
                "FactSet ledger 存在但 current Head 缺失",
                409,
            )
        if request.action != "bootstrap" or request.expected_generation != 0:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT",
                "FactSet Head 尚不存在，必须以 generation=0 bootstrap",
                409,
            )
        head = LabelFactSetHead(
            fact_set_head_id=_head_id(ctx, request.environment, target.fact_namespace),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            environment=request.environment,
            fact_namespace=target.fact_namespace,
            current_fact_set_id=target.fact_set_id,
            current_manifest_sha256=target.manifest_sha256,
            previous_fact_set_id=None,
            previous_manifest_sha256=None,
            generation=1,
            status="active",
            root_trace_id=target.root_trace_id,
            action_trace_id=ctx.trace_id,
            trace_id=ctx.trace_id,
            payload={},
        )
        event = _new_head_event(
            ctx,
            fact_set_head_id=head.fact_set_head_id,
            environment=request.environment,
            fact_namespace=target.fact_namespace,
            generation=1,
            previous_generation=None,
            action="bootstrap",
            old_fact_set_id=None,
            old_manifest_sha256=None,
            target=target,
            previous_event_sha256=None,
            root_trace_id=head.root_trace_id,
        )
        head.payload = {
            "last_event_sha256": event.content_sha256,
            "schema_version": "auris.label-fact-set-head/1",
        }
        try:
            with session.begin_nested():
                session.add(head)
                session.flush([head])
        except IntegrityError as error:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT",
                "FactSet Head 已被并发 bootstrap",
                409,
                retryable=True,
            ) from error
        target.status = "published"
        session.add(event)
        session.flush()
        before = None
    else:
        if request.action == "bootstrap":
            raise ApiError(
                "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT",
                "FactSet Head 已存在，不能再次 bootstrap",
                409,
            )
        events = _load_head_events(
            session,
            ctx,
            environment=request.environment,
            fact_namespace=target.fact_namespace,
            for_update=True,
        )
        _verify_head_chain(head, events)
        if head.generation != request.expected_generation:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT",
                "FactSet Head generation 已变化",
                409,
                details=[
                    {
                        "actual_generation": head.generation,
                        "expected_generation": request.expected_generation,
                    }
                ],
            )
        if (
            head.current_fact_set_id != request.expected_current_fact_set_id
            or head.current_manifest_sha256 != request.expected_current_manifest_sha256
        ):
            raise ApiError(
                "LABEL_FACT_SET_HEAD_ANCHOR_CONFLICT",
                "FactSet Head current pointer 已变化",
                409,
            )
        current = _load_fact_set(
            session,
            ctx,
            head.current_fact_set_id,
            for_update=True,
        )
        _assert_fact_set_integrity(session, ctx, current, for_update=True)
        if current.status != "published":
            raise ApiError(
                "LABEL_FACT_SET_HEAD_LEDGER_DRIFT",
                "FactSet Head current pointer 未指向 published FactSet",
                409,
            )
        if request.action == "promote" and target.fact_set_id == current.fact_set_id:
            raise ApiError(
                "LABEL_FACT_SET_STATE_CONFLICT",
                "promote 目标已经是 current FactSet",
                409,
            )
        if request.action == "rollback" and (
            target.fact_set_id != head.previous_fact_set_id
            or target.manifest_sha256 != head.previous_manifest_sha256
        ):
            raise ApiError(
                "LABEL_FACT_SET_ROLLBACK_TARGET_CONFLICT",
                "rollback 目标必须是 Head 冻结的 previous FactSet",
                409,
            )
        before = _head_snapshot(head)
        previous_event_sha256 = events[-1].content_sha256
        next_generation = head.generation + 1
        event = _new_head_event(
            ctx,
            fact_set_head_id=head.fact_set_head_id,
            environment=request.environment,
            fact_namespace=target.fact_namespace,
            generation=next_generation,
            previous_generation=head.generation,
            action=request.action,
            old_fact_set_id=head.current_fact_set_id,
            old_manifest_sha256=head.current_manifest_sha256,
            target=target,
            previous_event_sha256=previous_event_sha256,
            root_trace_id=head.root_trace_id,
        )
        result = session.execute(
            update(LabelFactSetHead)
            .where(
                LabelFactSetHead.fact_set_head_id == head.fact_set_head_id,
                LabelFactSetHead.tenant_id == ctx.tenant_id,
                LabelFactSetHead.project_id == ctx.project_id,
                LabelFactSetHead.environment == request.environment,
                LabelFactSetHead.fact_namespace == target.fact_namespace,
                LabelFactSetHead.generation == request.expected_generation,
                LabelFactSetHead.current_fact_set_id == request.expected_current_fact_set_id,
                LabelFactSetHead.current_manifest_sha256
                == request.expected_current_manifest_sha256,
            )
            .values(
                current_fact_set_id=target.fact_set_id,
                current_manifest_sha256=target.manifest_sha256,
                previous_fact_set_id=head.current_fact_set_id,
                previous_manifest_sha256=head.current_manifest_sha256,
                generation=next_generation,
                action_trace_id=ctx.trace_id,
                trace_id=ctx.trace_id,
                payload={
                    "last_event_sha256": event.content_sha256,
                    "schema_version": "auris.label-fact-set-head/1",
                },
            )
        )
        if getattr(result, "rowcount", None) != 1:
            raise ApiError(
                "LABEL_FACT_SET_HEAD_GENERATION_CONFLICT",
                "FactSet Head CAS 更新失败",
                409,
                retryable=True,
            )
        session.expire(head)
        session.refresh(head)
        if request.action == "promote":
            target.status = "published"
        session.add(event)
        session.flush()

    audit, outbox_event = _record_promotion(
        session,
        ctx,
        head=head,
        event=event,
        target=target,
        before=before,
    )
    response = _promotion_response(
        head,
        event,
        ctx,
        audit_id=audit.audit_id,
        outbox_event_id=outbox_event.event_id,
    )
    return _save_idempotency(
        session,
        ctx,
        operation=PROMOTE_OPERATION,
        body_hash=body_hash,
        status_code=200,
        response=response,
    )
