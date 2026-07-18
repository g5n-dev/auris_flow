from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import HotwordPack, HotwordPackVersion, RunRecord
from app.schemas.hotwords import HotwordRollbackRequest
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.run_service import run_payload

ROLLBACK_RUN_TYPE = "hotword_rollback"
ROLLBACK_REQUESTED_EVENT = "hotword_pack_version.rollback-requested"
ROLLBACK_COMPLETED_EVENT = "hotword_pack_version.rolled-back"


def _scoped_version(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    version_id: str,
    for_update: bool = False,
) -> HotwordPackVersion | None:
    statement = select(HotwordPackVersion).where(
        HotwordPackVersion.tenant_id == tenant_id,
        HotwordPackVersion.project_id == project_id,
        HotwordPackVersion.version_id == version_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def _scoped_pack(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    pack_id: str,
    for_update: bool = False,
) -> HotwordPack | None:
    statement = select(HotwordPack).where(
        HotwordPack.tenant_id == tenant_id,
        HotwordPack.project_id == project_id,
        HotwordPack.pack_id == pack_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def _version_snapshot(version: HotwordPackVersion) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "pack_id": version.pack_id,
        "status": version.status,
        "resource_version": version.resource_version,
        "root_trace_id": version.root_trace_id,
        "current_trace_id": version.current_trace_id,
    }


def _pack_snapshot(pack: HotwordPack) -> dict[str, Any]:
    return {
        "pack_id": pack.pack_id,
        "status": pack.status,
        "current_version_id": pack.current_version_id,
        "production_version_id": pack.production_version_id,
        "resource_version": pack.resource_version,
        "root_trace_id": pack.root_trace_id,
        "current_trace_id": pack.current_trace_id,
    }


def _assert_expected_resource_version(expected: int, actual: int) -> None:
    if expected == actual:
        return
    raise ApiError(
        "RESOURCE_VERSION_CONFLICT",
        "热词包版本已被其他请求更新，请刷新后重试",
        409,
        details=[
            {
                "expected_resource_version": expected,
                "current_resource_version": actual,
            }
        ],
    )


def _assert_no_active_rollback(
    session: Session,
    ctx: RequestContext,
    *,
    source_version_id: str,
) -> None:
    active_runs = session.scalars(
        select(RunRecord).where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == ROLLBACK_RUN_TYPE,
            RunRecord.status.in_(("pending", "running", "blocked", "submitted")),
        )
    ).all()
    for record in active_runs:
        if record.payload.get("source_version_id") == source_version_id:
            raise ApiError(
                "HOTWORD_ROLLBACK_ALREADY_PENDING",
                "当前热词版本已有进行中的受控回滚",
                409,
                details=[{"run_id": record.run_id, "status": record.status}],
            )


def create_hotword_rollback(
    session: Session,
    ctx: RequestContext,
    source_version_id: str,
    body: HotwordRollbackRequest,
) -> dict[str, Any]:
    source = _scoped_version(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        version_id=source_version_id,
        for_update=True,
    )
    if source is None:
        raise ApiError(
            "HOTWORD_VERSION_NOT_FOUND",
            f"热词包版本不存在：{source_version_id}",
            404,
        )
    _assert_expected_resource_version(body.expected_resource_version, source.resource_version)
    pack = _scoped_pack(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        pack_id=source.pack_id,
        for_update=True,
    )
    if pack is None:
        raise ApiError("HOTWORD_PACK_NOT_FOUND", f"热词包不存在：{source.pack_id}", 404)
    if source.status != "published" or pack.current_version_id != source.version_id:
        raise ApiError(
            "HOTWORD_ROLLBACK_SOURCE_NOT_CURRENT",
            "只能回滚逻辑词包当前已发布版本",
            409,
            details=[
                {
                    "source_status": source.status,
                    "current_version_id": pack.current_version_id,
                }
            ],
        )
    target = _scoped_version(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        version_id=body.target_version_id,
        for_update=True,
    )
    if target is None:
        raise ApiError(
            "HOTWORD_VERSION_NOT_FOUND",
            f"回滚目标版本不存在：{body.target_version_id}",
            404,
        )
    if (
        target.version_id == source.version_id
        or target.pack_id != source.pack_id
        or target.status != "published"
    ):
        raise ApiError(
            "HOTWORD_ROLLBACK_TARGET_INVALID",
            "回滚目标必须是同一词包中已发布的历史版本",
            409,
            details=[
                {
                    "source_version_id": source.version_id,
                    "target_version_id": target.version_id,
                    "target_pack_id": target.pack_id,
                    "target_status": target.status,
                }
            ],
        )
    if not (source.root_trace_id == target.root_trace_id == pack.root_trace_id):
        raise ApiError(
            "HOTWORD_ROLLBACK_ROOT_TRACE_MISMATCH",
            "源版本、目标版本与逻辑词包必须属于同一治理根 Trace",
            409,
        )
    _assert_no_active_rollback(session, ctx, source_version_id=source.version_id)

    run_id = f"hotword_rollback_{uuid.uuid4().hex[:12]}"
    requested_at = datetime.now(UTC).isoformat()
    frozen_target = {
        "pack_id": pack.pack_id,
        "pack_resource_version": pack.resource_version,
        "pack_root_trace_id": pack.root_trace_id,
        "source_version_id": source.version_id,
        "source_resource_version": source.resource_version,
        "source_root_trace_id": source.root_trace_id,
        "target_version_id": target.version_id,
        "target_resource_version": target.resource_version,
        "target_root_trace_id": target.root_trace_id,
    }
    release_gate = {
        "status": "awaiting_decision",
        "requested_by": ctx.user_id,
        "requested_at": requested_at,
        "required_roles": ["project_admin"],
        "separation_of_duties": "different_natural_person",
        "target": frozen_target,
    }
    payload = {
        "run_id": run_id,
        "run_type": ROLLBACK_RUN_TYPE,
        "status": "pending",
        "hotword_pack_version_id": source.version_id,
        **frozen_target,
        "expected_resource_version": body.expected_resource_version,
        "reason": body.reason,
        "requested_by": ctx.user_id,
        "requested_at": requested_at,
        "root_trace_id": source.root_trace_id,
        "release_gate": release_gate,
        "affected_objects": [
            {"type": "hotword_pack", "id": pack.pack_id},
            {"type": "hotword_pack_version", "id": source.version_id},
            {"type": "hotword_pack_version", "id": target.version_id},
        ],
        "next_actions": [
            {"key": "approve_release", "label": "项目管理员批准回滚"},
            {"key": "reject_release", "label": "拒绝回滚"},
            {"key": "view_trace", "label": "查看 Trace"},
        ],
    }
    record = RunRecord(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=ROLLBACK_RUN_TYPE,
        status="pending",
        run_key=f"hotword-rollback:{source.version_id}:{target.version_id}",
        partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{pack.pack_id}",
        trace_id=source.root_trace_id,
        payload=payload,
    )
    session.add(record)
    session.flush()
    lineage_ctx = replace(
        ctx,
        trace_id=source.root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=source.root_trace_id,
    )
    enqueue_event(
        session,
        lineage_ctx,
        event_type=ROLLBACK_REQUESTED_EVENT,
        aggregate_type=ROLLBACK_RUN_TYPE,
        aggregate_id=run_id,
        payload=payload,
    )
    record_audit(
        session,
        ctx,
        action="hotword_rollback.create",
        object_type=ROLLBACK_RUN_TYPE,
        object_id=run_id,
        after=payload,
        trace_id=source.root_trace_id,
    )
    return run_payload(record)


def _frozen_binding(record: RunRecord, field: str) -> Any:
    target = record.payload.get("release_gate", {}).get("target", {})
    if isinstance(target, dict) and field in target:
        return target[field]
    return record.payload.get(field)


def revalidate_hotword_rollback(
    session: Session,
    record: RunRecord,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    if record.run_type != ROLLBACK_RUN_TYPE:
        return {"allowed": True}
    source_version_id = str(_frozen_binding(record, "source_version_id") or "")
    target_version_id = str(_frozen_binding(record, "target_version_id") or "")
    pack_id = str(_frozen_binding(record, "pack_id") or "")
    if not source_version_id or not target_version_id or not pack_id:
        return {"allowed": False, "reason": "rollback_binding_missing"}
    source = _scoped_version(
        session,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        version_id=source_version_id,
        for_update=for_update,
    )
    target = _scoped_version(
        session,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        version_id=target_version_id,
        for_update=for_update,
    )
    pack = _scoped_pack(
        session,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        pack_id=pack_id,
        for_update=for_update,
    )
    if source is None:
        return {"allowed": False, "reason": "rollback_source_missing"}
    if target is None:
        return {"allowed": False, "reason": "rollback_target_missing"}
    if pack is None:
        return {"allowed": False, "reason": "rollback_pack_missing"}
    if source.pack_id != pack.pack_id or target.pack_id != pack.pack_id:
        return {"allowed": False, "reason": "rollback_pack_binding_changed"}
    if source.resource_version != _frozen_binding(record, "source_resource_version"):
        return {"allowed": False, "reason": "rollback_source_changed"}
    if target.resource_version != _frozen_binding(record, "target_resource_version"):
        return {"allowed": False, "reason": "rollback_target_changed"}
    if pack.resource_version != _frozen_binding(record, "pack_resource_version"):
        return {"allowed": False, "reason": "rollback_pack_changed"}
    if source.root_trace_id != _frozen_binding(record, "source_root_trace_id"):
        return {"allowed": False, "reason": "rollback_source_root_trace_changed"}
    if target.root_trace_id != _frozen_binding(record, "target_root_trace_id"):
        return {"allowed": False, "reason": "rollback_target_root_trace_changed"}
    if pack.root_trace_id != _frozen_binding(record, "pack_root_trace_id"):
        return {"allowed": False, "reason": "rollback_pack_root_trace_changed"}
    if not (source.root_trace_id == target.root_trace_id == pack.root_trace_id):
        return {"allowed": False, "reason": "rollback_root_trace_mismatch"}
    if source.status != "published" or pack.current_version_id != source.version_id:
        return {"allowed": False, "reason": "rollback_source_not_current"}
    if target.status != "published" or target.version_id == source.version_id:
        return {"allowed": False, "reason": "rollback_target_not_historical_published"}

    gate = record.payload.get("release_gate")
    if not isinstance(gate, dict) or gate.get("status") != "approved":
        return {"allowed": False, "reason": "release_gate_not_approved"}
    decision = gate.get("decision")
    if not isinstance(decision, dict) or decision.get("value") != "approved":
        return {"allowed": False, "reason": "release_gate_not_approved"}
    requested_by = str(gate.get("requested_by") or record.payload.get("requested_by") or "")
    approved_by = str(decision.get("actor_id") or "")
    roles = decision.get("roles")
    if not approved_by or not isinstance(roles, list) or "project_admin" not in roles:
        return {"allowed": False, "reason": "rollback_admin_approval_missing"}
    if requested_by == approved_by:
        return {"allowed": False, "reason": "rollback_approval_separation_failed"}
    return {
        "allowed": True,
        "source": source,
        "target": target,
        "pack": pack,
        "decision": decision,
        "approval_id": f"approval:{record.run_id}:{approved_by}",
    }


def materialize_hotword_rollback(
    session: Session,
    record: RunRecord,
) -> dict[str, Any]:
    checked = revalidate_hotword_rollback(session, record, for_update=True)
    if checked.get("allowed") is not True:
        return checked
    source = checked["source"]
    target = checked["target"]
    pack = checked["pack"]
    decision = checked["decision"]
    assert isinstance(source, HotwordPackVersion)
    assert isinstance(target, HotwordPackVersion)
    assert isinstance(pack, HotwordPack)
    assert isinstance(decision, dict)
    before = {
        "source": _version_snapshot(source),
        "target": _version_snapshot(target),
        "pack": _pack_snapshot(pack),
    }
    approved_by = str(decision["actor_id"])
    decision_trace_id = str(decision.get("trace_id") or record.trace_id)
    source.status = "rolled_back"
    source.resource_version += 1
    source.current_trace_id = decision_trace_id
    pack.current_version_id = target.version_id
    pack.resource_version += 1
    pack.current_trace_id = decision_trace_id
    session.flush()

    completion = {
        "allowed": True,
        "run_id": record.run_id,
        "pack_id": pack.pack_id,
        "from_version_id": source.version_id,
        "to_version_id": target.version_id,
        "reason": record.payload.get("reason"),
        "approval_id": checked["approval_id"],
        "requested_by": record.payload.get("requested_by"),
        "approved_by": approved_by,
        "source_resource_version": source.resource_version,
        "target_resource_version": target.resource_version,
        "pack_resource_version": pack.resource_version,
        "root_trace_id": source.root_trace_id,
        "task_version_changed": False,
        "historical_assets_overwritten": False,
    }
    approval_ctx = RequestContext(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        user_id=approved_by,
        roles=("project_admin",),
        request_id=f"rollback-materialize-{record.run_id}",
        trace_id=source.root_trace_id,
        parent_trace_id=decision_trace_id,
        correlation_id=source.root_trace_id,
    )
    record_audit(
        session,
        approval_ctx,
        action="hotword_version.rolled_back",
        object_type="hotword_pack_version",
        object_id=source.version_id,
        before=before,
        after={
            "source": _version_snapshot(source),
            "target": _version_snapshot(target),
            "pack": _pack_snapshot(pack),
            **completion,
        },
        trace_id=source.root_trace_id,
    )
    enqueue_event(
        session,
        approval_ctx,
        event_type=ROLLBACK_COMPLETED_EVENT,
        aggregate_type="hotword_pack_version",
        aggregate_id=source.version_id,
        payload=completion,
    )
    return completion
