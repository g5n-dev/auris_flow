from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.models import (
    AuditLog,
    IdempotencyRecord,
    JsonResource,
    OutboxEvent,
    RunRecord,
    TaskVersionReleaseHead,
)
from app.repositories.outbox_events import database_utc_now
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.resource_service import get_resource, upsert_resource
from app.services.run_service import public_run_response, run_payload, transition_run

CONTROL_PLANE_RELEASE_RUN_TYPES = frozenset({"task_version_publish", "settings_publish"})
RELEASE_RUN_TYPES = CONTROL_PLANE_RELEASE_RUN_TYPES | {"hotword_rollback"}
RELEASE_EVENT_TYPES = {
    "task_version_publish": "task_version.publish_requested",
    "settings_publish": "settings.publish_requested",
    "hotword_rollback": "hotword_pack_version.rollback-requested",
}
PROJECT_ADMIN_ROLE = "project_admin"
SEPARATION_OF_DUTIES_POLICY = "different_natural_person"
RELEASE_REQUEST_FIELDS = (
    "requested_by",
    "requested_at",
    "target",
    "required_roles",
    "separation_of_duties",
)


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_hash(*, status: str | None, data: dict[str, Any]) -> str:
    return _json_hash({"status": status, "data": data})


def _resource_hash(resource: JsonResource) -> str:
    return _canonical_hash(status=resource.status, data=resource.data)


def _optional_resource(
    session: Session,
    ctx: RequestContext,
    collection: str,
    resource_key: str,
    *,
    for_update: bool = False,
) -> JsonResource | None:
    statement = select(JsonResource).where(
        JsonResource.tenant_id == ctx.tenant_id,
        JsonResource.project_id == ctx.project_id,
        JsonResource.collection == collection,
        JsonResource.resource_key == resource_key,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _task_release_head(
    session: Session,
    ctx: RequestContext,
    task_type_id: str,
    release_channel: str,
    *,
    for_update: bool = False,
) -> TaskVersionReleaseHead | None:
    statement = select(TaskVersionReleaseHead).where(
        TaskVersionReleaseHead.tenant_id == ctx.tenant_id,
        TaskVersionReleaseHead.project_id == ctx.project_id,
        TaskVersionReleaseHead.task_type_id == task_type_id,
        TaskVersionReleaseHead.release_channel == release_channel,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _current_published_task_version(
    session: Session,
    ctx: RequestContext,
    task_type_id: str,
    *,
    exclude_task_version_id: str | None = None,
    for_update: bool = False,
) -> JsonResource | None:
    statement = (
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
            JsonResource.status == "published",
        )
        .order_by(JsonResource.updated_at.desc(), JsonResource.resource_key.desc())
    )
    if for_update:
        statement = statement.with_for_update()
    for resource in session.scalars(statement):
        if resource.resource_key == exclude_task_version_id:
            continue
        if str(resource.data.get("task_type_id") or "") == task_type_id:
            return resource
    return None


def _release_head_payload(head: TaskVersionReleaseHead) -> dict[str, Any]:
    return {
        "release_head_id": head.release_head_id,
        "task_type_id": head.task_type_id,
        "release_channel": head.release_channel,
        "active_task_version_id": head.active_task_version_id,
        "active_snapshot_sha256": head.active_snapshot_sha256,
        "previous_task_version_id": head.previous_task_version_id,
        "generation": head.generation,
        "status": head.status,
        "activated_by_run_id": head.activated_by_run_id,
        "trace_id": head.trace_id,
    }


def _release_gate(
    ctx: RequestContext,
    *,
    run_type: str,
    target: dict[str, Any],
) -> dict[str, Any]:
    gate = {
        "status": "awaiting_decision",
        "requested_by": ctx.user_id,
        "requested_at": datetime.now(UTC).isoformat(),
        "target": target,
        "required_roles": [PROJECT_ADMIN_ROLE],
        "separation_of_duties": SEPARATION_OF_DUTIES_POLICY,
    }
    gate["request_sha256"] = _release_request_sha256(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=run_type,
        gate=gate,
    )
    return gate


def _release_request_sha256(
    *,
    tenant_id: str,
    project_id: str,
    run_type: str,
    gate: dict[str, Any],
) -> str:
    return _json_hash(
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_type": run_type,
            **{field: gate.get(field) for field in RELEASE_REQUEST_FIELDS},
        }
    )


def prepare_task_version_publish(
    session: Session,
    ctx: RequestContext,
    task_version_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    version = get_resource(session, ctx, "task_versions", task_version_id)
    version_status = str(version.data.get("status") or version.status or "")
    if version_status == "published":
        raise ApiError(
            "TASK_VERSION_ALREADY_PUBLISHED",
            "任务版本已经发布，不能重复创建发布门禁",
            409,
            details=[{"task_version_id": task_version_id}],
        )
    experiment_attestation = None
    if version_status in {"validated", "experiment_ready"}:
        from app.services.experiment_service import validate_experiment_release_attestation

        experiment_attestation = validate_experiment_release_attestation(
            session,
            ctx,
            task_version_id,
            payload,
        )
    elif version_status != "draft":
        raise ApiError(
            "TASK_VERSION_NOT_DRAFT",
            "只有草稿或已通过受控实验晋级的冻结任务版本可以创建发布门禁",
            409,
            details=[{"task_version_id": task_version_id, "status": version_status}],
        )
    from app.services.task_execution_policy import validate_task_version_publish_binding

    hotword_binding = validate_task_version_publish_binding(
        session,
        ctx,
        version.data,
        task_version_id=task_version_id,
    )
    task_type_id = str(
        (experiment_attestation or {}).get("task_type_id") or version.data.get("task_type_id") or ""
    ).strip()
    if not task_type_id:
        raise ApiError("TASK_TYPE_BINDING_REQUIRED", "发布版本必须声明 task_type_id", 409)
    release_channel = str(payload.get("release_channel") or "production").strip()
    head = _task_release_head(session, ctx, task_type_id, release_channel)
    current = (
        _optional_resource(
            session,
            ctx,
            "task_versions",
            head.active_task_version_id,
        )
        if head is not None
        else _optional_resource(
            session,
            ctx,
            "task_versions",
            str((experiment_attestation or {}).get("control_task_version_id") or ""),
        )
        if experiment_attestation
        else _current_published_task_version(
            session,
            ctx,
            task_type_id,
            exclude_task_version_id=task_version_id,
        )
    )
    target = {
        "collection": "task_versions",
        "resource_id": task_version_id,
        "snapshot_sha256": _resource_hash(version),
        "task_type_id": task_type_id,
        "release_channel": release_channel,
        "expected_head_task_version_id": (
            head.active_task_version_id
            if head is not None
            else current.resource_key
            if current
            else None
        ),
        "expected_head_generation": head.generation if head is not None else 0,
        "expected_head_snapshot_sha256": _resource_hash(current) if current else None,
    }
    gate = _release_gate(ctx, run_type="task_version_publish", target=target)
    return {
        **payload,
        "task_version_id": task_version_id,
        "hotword_binding": hotword_binding,
        "experiment_attestation": experiment_attestation,
        "requested_by": gate["requested_by"],
        "requested_at": gate["requested_at"],
        "release_gate": gate,
        "affected_objects": [
            {"type": "task_version", "id": task_version_id},
            {
                "type": "task_version_release_head",
                "id": f"{task_type_id}:{release_channel}",
            },
        ],
        "next_actions": [
            {"key": "approve_release", "label": "审批通过"},
            {"key": "reject_release", "label": "拒绝发布"},
            {"key": "view_trace", "label": "查看 Trace"},
        ],
    }


def prepare_settings_publish(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "").strip()
    if not draft_id:
        raise ApiError("SETTINGS_DRAFT_ID_REQUIRED", "发布设置必须指定 draft_id", 422)
    draft = get_resource(session, ctx, "settings_drafts", draft_id)
    if draft.status != "draft" and draft.data.get("status") != "draft":
        raise ApiError(
            "SETTINGS_DRAFT_NOT_PUBLISHABLE",
            "只有草稿状态的配置可以创建发布门禁",
            409,
            details=[{"draft_id": draft_id, "status": draft.status}],
        )
    setting_id = str(draft.data.get("setting_id") or "").strip()
    changes = draft.data.get("changes")
    if not setting_id or not isinstance(changes, dict) or not changes:
        raise ApiError(
            "SETTINGS_DRAFT_INVALID",
            "配置草稿必须包含 setting_id 和非空 changes",
            422,
            details=[{"draft_id": draft_id}],
        )
    live_setting = _optional_resource(session, ctx, "settings", setting_id)
    target = {
        "collection": "settings_drafts",
        "resource_id": draft_id,
        "snapshot_sha256": _resource_hash(draft),
        "live_collection": "settings",
        "live_resource_id": setting_id,
        "live_snapshot_sha256": _resource_hash(live_setting) if live_setting else None,
    }
    gate = _release_gate(ctx, run_type="settings_publish", target=target)
    return {
        **payload,
        "draft_id": draft_id,
        "setting_id": setting_id,
        "requested_by": gate["requested_by"],
        "requested_at": gate["requested_at"],
        "release_gate": gate,
        "affected_objects": [
            {"type": "settings_draft", "id": draft_id},
            {"type": "setting", "id": setting_id},
        ],
        "next_actions": [
            {"key": "approve_release", "label": "审批通过写入"},
            {"key": "reject_release", "label": "退回草稿"},
            {"key": "view_trace", "label": "查看 Trace"},
        ],
    }


def _release_context(record: RunRecord) -> RequestContext:
    gate = record.payload.get("release_gate")
    decision = gate.get("decision") if isinstance(gate, dict) else None
    actor_id = "system"
    if isinstance(decision, dict):
        decision_actor = decision.get("actor_id")
        if isinstance(decision_actor, str) and decision_actor:
            actor_id = decision_actor
    if actor_id == "system" and isinstance(gate, dict):
        requested_by = gate.get("requested_by")
        if isinstance(requested_by, str) and requested_by:
            actor_id = requested_by
    return RequestContext(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        user_id=actor_id,
        roles=("project_admin",),
        request_id=f"release-{record.run_id}",
        trace_id=record.trace_id,
        idempotency_key=None,
        correlation_id=record.trace_id,
    )


def _gate_target(record: RunRecord) -> dict[str, Any]:
    gate = record.payload.get("release_gate")
    target = gate.get("target") if isinstance(gate, dict) else None
    if not isinstance(target, dict):
        raise ApiError(
            "RELEASE_GATE_TARGET_MISSING",
            "发布门禁缺少冻结目标",
            409,
            details=[{"run_id": record.run_id}],
        )
    return target


def _release_request_snapshot(gate: dict[str, Any]) -> dict[str, Any]:
    return {field: gate.get(field) for field in RELEASE_REQUEST_FIELDS}


def _release_request_binding_is_valid(
    record: RunRecord,
    payload: dict[str, Any],
    gate: dict[str, Any],
) -> bool:
    requested_by = gate.get("requested_by")
    requested_at = gate.get("requested_at")
    target = gate.get("target")
    required_roles = gate.get("required_roles")
    request_sha256 = gate.get("request_sha256")
    if (
        not isinstance(requested_by, str)
        or not requested_by.strip()
        or not isinstance(requested_at, str)
        or not requested_at.strip()
        or not isinstance(target, dict)
        or not target
        or required_roles != [PROJECT_ADMIN_ROLE]
        or gate.get("separation_of_duties") != SEPARATION_OF_DUTIES_POLICY
        or payload.get("requested_by") != requested_by
        or payload.get("requested_at") != requested_at
        or not isinstance(request_sha256, str)
    ):
        return False
    expected_sha256 = _release_request_sha256(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        run_type=record.run_type,
        gate=gate,
    )
    return hmac.compare_digest(request_sha256, expected_sha256)


def _release_event(
    session: Session,
    record: RunRecord,
    *,
    for_update: bool = False,
) -> OutboxEvent | None:
    event_type = RELEASE_EVENT_TYPES.get(record.run_type)
    if event_type is None:
        return None
    statement = select(OutboxEvent).where(
        OutboxEvent.tenant_id == record.tenant_id,
        OutboxEvent.project_id == record.project_id,
        OutboxEvent.aggregate_id == record.run_id,
        OutboxEvent.aggregate_type == record.run_type,
        OutboxEvent.event_type == event_type,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalar(statement)


def _release_request_binding_reason(
    session: Session,
    record: RunRecord,
    gate: dict[str, Any],
) -> str | None:
    if not _release_request_binding_is_valid(record, record.payload, gate):
        return "release_request_binding_changed"

    event = _release_event(session, record)
    if event is None:
        return "release_gate_event_missing"
    event_data = event.payload.get("data")
    event_data_sha256 = event.payload.get("business_payload_sha256")
    if (
        not isinstance(event_data, dict)
        or not isinstance(event_data_sha256, str)
        or not hmac.compare_digest(_json_hash(event_data), event_data_sha256)
    ):
        return "release_request_binding_changed"
    frozen_gate = event_data.get("release_gate")
    if not isinstance(frozen_gate, dict) or not _release_request_binding_is_valid(
        record,
        event_data,
        frozen_gate,
    ):
        return "release_request_binding_changed"
    if _release_request_snapshot(frozen_gate) != _release_request_snapshot(gate):
        return "release_request_binding_changed"
    if frozen_gate.get("request_sha256") != gate.get("request_sha256"):
        return "release_request_binding_changed"

    retry_of_run_id = record.payload.get("retry_of_run_id")
    if isinstance(retry_of_run_id, str) and retry_of_run_id:
        source = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == retry_of_run_id,
                RunRecord.tenant_id == record.tenant_id,
                RunRecord.project_id == record.project_id,
                RunRecord.run_type == record.run_type,
            )
        )
        source_gate = source.payload.get("release_gate") if source is not None else None
        if (
            source is None
            or not isinstance(source_gate, dict)
            or not _release_request_binding_is_valid(source, source.payload, source_gate)
            or _release_request_snapshot(source_gate) != _release_request_snapshot(gate)
            or source_gate.get("request_sha256") != gate.get("request_sha256")
        ):
            return "release_request_binding_changed"
    return None


def _release_decision_sha256(gate: dict[str, Any], decision: dict[str, Any]) -> str:
    return _json_hash(
        {
            "request_sha256": gate.get("request_sha256"),
            "value": decision.get("value"),
            "reason": decision.get("reason"),
            "actor_id": decision.get("actor_id"),
            "roles": decision.get("roles"),
            "decided_at": decision.get("decided_at"),
            "trace_id": decision.get("trace_id"),
        }
    )


def _release_decision_audit_proof(
    gate: dict[str, Any],
    decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "request_sha256": gate.get("request_sha256"),
        "status": gate.get("status"),
    }
    if decision is not None:
        proof.update(
            {
                "decision_sha256": decision.get("decision_sha256"),
                "decision_value": decision.get("value"),
                "actor_id": decision.get("actor_id"),
            }
        )
    return proof


def _release_decision_audit_matches(
    session: Session,
    record: RunRecord,
    gate: dict[str, Any],
    decision: dict[str, Any],
) -> bool:
    audit = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.tenant_id == record.tenant_id,
            AuditLog.project_id == record.project_id,
            AuditLog.action == f"{record.run_type}.release_gate_decided",
            AuditLog.object_type == record.run_type,
            AuditLog.object_id == record.run_id,
            AuditLog.actor_id == decision.get("actor_id"),
            AuditLog.result == "approved",
        )
        .order_by(AuditLog.audit_id.desc())
    )
    proof = (
        audit.after_json.get("release_gate_proof")
        if audit is not None and isinstance(audit.after_json, dict)
        else None
    )
    expected_decision_sha256 = _release_decision_sha256(gate, decision)
    return (
        isinstance(proof, dict)
        and proof.get("request_sha256") == gate.get("request_sha256")
        and proof.get("decision_sha256") == expected_decision_sha256
        and proof.get("decision_value") == decision.get("value")
        and proof.get("actor_id") == decision.get("actor_id")
    )


def revalidate_control_plane_release(
    session: Session,
    record: RunRecord,
) -> dict[str, Any]:
    if record.run_type not in RELEASE_RUN_TYPES:
        return {"allowed": True}
    if record.run_type == "hotword_rollback":
        from app.services.hotword_rollback_service import revalidate_hotword_rollback

        return revalidate_hotword_rollback(session, record)
    gate = record.payload.get("release_gate")
    if not isinstance(gate, dict):
        return {"allowed": False, "reason": "release_gate_missing"}
    binding_reason = _release_request_binding_reason(session, record, gate)
    if binding_reason is not None:
        return {"allowed": False, "reason": binding_reason}
    ctx = _release_context(record)
    target = gate["target"]
    resource = _optional_resource(
        session,
        ctx,
        str(target.get("collection") or ""),
        str(target.get("resource_id") or ""),
    )
    if resource is None:
        return {"allowed": False, "reason": "release_target_missing"}
    if _resource_hash(resource) != target.get("snapshot_sha256"):
        return {"allowed": False, "reason": "release_target_changed"}
    live_collection = target.get("live_collection")
    live_resource_id = target.get("live_resource_id")
    if isinstance(live_collection, str) and isinstance(live_resource_id, str):
        live = _optional_resource(session, ctx, live_collection, live_resource_id)
        current_hash = _resource_hash(live) if live else None
        if current_hash != target.get("live_snapshot_sha256"):
            return {"allowed": False, "reason": "live_setting_changed"}
    gate_status = gate.get("status")
    if gate_status == "awaiting_decision" and "decision" not in gate:
        return {"allowed": False, "reason": "release_gate_not_approved"}
    if gate_status != "approved":
        return {"allowed": False, "reason": "release_gate_state_invalid"}
    decision = gate.get("decision")
    if not isinstance(decision, dict) or decision.get("value") != "approved":
        return {"allowed": False, "reason": "release_approval_missing"}
    approved_by = decision.get("actor_id")
    roles = decision.get("roles")
    if (
        not isinstance(approved_by, str)
        or not approved_by.strip()
        or not isinstance(roles, list)
        or PROJECT_ADMIN_ROLE not in roles
    ):
        return {"allowed": False, "reason": "release_admin_approval_missing"}
    if approved_by == gate["requested_by"]:
        return {"allowed": False, "reason": "release_approval_separation_failed"}
    decision_sha256 = decision.get("decision_sha256")
    if not isinstance(decision_sha256, str) or not hmac.compare_digest(
        decision_sha256,
        _release_decision_sha256(gate, decision),
    ):
        return {"allowed": False, "reason": "release_decision_binding_changed"}
    if not _release_decision_audit_matches(session, record, gate, decision):
        return {"allowed": False, "reason": "release_decision_audit_missing"}
    return {
        "allowed": True,
        "target": target,
        "decision": decision,
    }


def materialize_control_plane_release(
    session: Session,
    record: RunRecord,
) -> dict[str, Any]:
    if record.run_type == "hotword_rollback":
        from app.services.hotword_rollback_service import materialize_hotword_rollback

        return materialize_hotword_rollback(session, record)
    gate_result = revalidate_control_plane_release(session, record)
    if gate_result.get("allowed") is not True:
        return gate_result
    ctx = _release_context(record)
    target = _gate_target(record)
    now = datetime.now(UTC).isoformat()
    decision = record.payload.get("release_gate", {}).get("decision", {})
    published_by = decision.get("actor_id") or ctx.user_id

    if record.run_type == "task_version_publish":
        version_id = str(target["resource_id"])
        version = get_resource(session, ctx, "task_versions", version_id)
        experiment_attestation = record.payload.get("experiment_attestation")
        if isinstance(experiment_attestation, dict):
            from app.services.experiment_service import validate_experiment_release_attestation

            try:
                experiment_attestation = validate_experiment_release_attestation(
                    session,
                    ctx,
                    version_id,
                    experiment_attestation,
                )
            except ApiError as exc:
                return {
                    "allowed": False,
                    "reason": "experiment_attestation_changed",
                    "error_code": exc.code,
                    "details": exc.details,
                }
        from app.services.task_execution_policy import validate_task_version_publish_binding

        try:
            validate_task_version_publish_binding(
                session,
                ctx,
                version.data,
                task_version_id=version_id,
            )
        except ApiError as exc:
            return {
                "allowed": False,
                "reason": "hotword_binding_changed",
                "error_code": exc.code,
                "details": exc.details,
            }
        task_type_id = str(target.get("task_type_id") or version.data.get("task_type_id") or "")
        release_channel = str(target.get("release_channel") or "production")
        expected_head_id = target.get("expected_head_task_version_id")
        expected_generation = int(target.get("expected_head_generation") or 0)
        expected_head_sha256 = target.get("expected_head_snapshot_sha256")
        head = _task_release_head(
            session,
            ctx,
            task_type_id,
            release_channel,
            for_update=True,
        )
        current_id: str | None
        if head is not None:
            current_id = head.active_task_version_id
            current_generation = head.generation
            current = _optional_resource(
                session,
                ctx,
                "task_versions",
                current_id,
                for_update=True,
            )
        else:
            current_generation = 0
            current = _current_published_task_version(
                session,
                ctx,
                task_type_id,
                exclude_task_version_id=version_id,
                for_update=True,
            )
            current_id = current.resource_key if current else None
        current_sha256 = _resource_hash(current) if current else None
        if (
            current_id != expected_head_id
            or current_generation != expected_generation
            or current_sha256 != expected_head_sha256
        ):
            return {
                "allowed": False,
                "reason": "task_version_release_head_changed",
                "current_head_task_version_id": current_id,
                "current_head_generation": current_generation,
            }
        published = {
            **version.data,
            "status": "published",
            "published_at": now,
            "published_by": published_by,
            "publish_run_id": record.run_id,
            "trace_id": record.trace_id,
        }
        published_resource = upsert_resource(
            session,
            ctx,
            "task_versions",
            version_id,
            published,
            status="published",
            trace_id=record.trace_id,
            audit_action="task_version.release_materialized",
        )
        if current is not None and current.resource_key != version_id:
            deprecated = {
                **current.data,
                "status": "deprecated",
                "deprecated_at": now,
                "deprecated_by": published_by,
                "replaced_by_task_version_id": version_id,
                "replacement_run_id": record.run_id,
                "trace_id": record.trace_id,
            }
            upsert_resource(
                session,
                ctx,
                "task_versions",
                current.resource_key,
                deprecated,
                status="deprecated",
                trace_id=record.trace_id,
                audit_action="task_version.deprecated_by_release_head",
            )
        active_snapshot_sha256 = _resource_hash(published_resource)
        release_head_payload = {
            "experiment_attestation": experiment_attestation,
            "release_request_sha256": record.payload.get("release_gate", {}).get("request_sha256"),
            "approved_by": published_by,
        }
        if head is None:
            release_head_scope_sha256 = _json_hash(
                {
                    "tenant_id": ctx.tenant_id,
                    "project_id": ctx.project_id,
                    "task_type_id": task_type_id,
                    "release_channel": release_channel,
                }
            )
            head = TaskVersionReleaseHead(
                release_head_id=f"tvrh_{release_head_scope_sha256[:24]}",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                task_type_id=task_type_id,
                release_channel=release_channel,
                active_task_version_id=version_id,
                active_snapshot_sha256=active_snapshot_sha256,
                previous_task_version_id=current_id,
                generation=1,
                status="active",
                activated_by_run_id=record.run_id,
                trace_id=record.trace_id,
                payload=release_head_payload,
            )
            session.add(head)
        else:
            head.previous_task_version_id = current_id
            head.active_task_version_id = version_id
            head.active_snapshot_sha256 = active_snapshot_sha256
            head.generation += 1
            head.activated_by_run_id = record.run_id
            head.trace_id = record.trace_id
            head.payload = release_head_payload
        session.flush()
        record_audit(
            session,
            ctx,
            action="task_version_release_head.activate",
            object_type="task_version_release_head",
            object_id=head.release_head_id,
            before={
                "active_task_version_id": current_id,
                "generation": current_generation,
            },
            after=_release_head_payload(head),
            trace_id=record.trace_id,
        )
        from app.services.hotword_service import activate_hotword_version_for_task_release

        hotword_activation = activate_hotword_version_for_task_release(
            session,
            ctx,
            task_version_id=version_id,
            task_data=published,
            task_publish_run_id=record.run_id,
            published_by=published_by,
        )
        return {
            "allowed": True,
            "collection": "task_versions",
            "resource_id": version_id,
            "status": "published",
            "hotword_activation": hotword_activation,
            "experiment_attestation": experiment_attestation,
            "task_version_release_head": _release_head_payload(head),
        }

    draft_id = str(target["resource_id"])
    setting_id = str(target["live_resource_id"])
    draft = get_resource(session, ctx, "settings_drafts", draft_id)
    changes = dict(draft.data.get("changes") or {})
    live = _optional_resource(session, ctx, "settings", setting_id)
    live_data = dict(live.data) if live else {"id": setting_id, "setting_id": setting_id}
    published_setting = {
        **live_data,
        **changes,
        "id": setting_id,
        "setting_id": setting_id,
        "status": "active",
        "published_at": now,
        "published_by": published_by,
        "publish_run_id": record.run_id,
        "source_draft_id": draft_id,
        "trace_id": record.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "settings",
        setting_id,
        published_setting,
        status="active",
        trace_id=record.trace_id,
        audit_action="settings.release_materialized",
    )
    published_draft = {
        **draft.data,
        "status": "published",
        "published_at": now,
        "published_by": published_by,
        "publish_run_id": record.run_id,
        "trace_id": record.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "settings_drafts",
        draft_id,
        published_draft,
        status="published",
        trace_id=record.trace_id,
        audit_action="settings_draft.release_materialized",
    )
    return {
        "allowed": True,
        "collection": "settings",
        "resource_id": setting_id,
        "source_draft_id": draft_id,
        "status": "published",
    }


def _reset_outbox_for_approval(
    event: OutboxEvent,
    gate: dict[str, Any],
    *,
    available_at: datetime,
) -> None:
    event.status = "pending"
    event.delivery_state = "ready"
    event.available_at = available_at
    event.processed_at = None
    event.last_error = None
    event.dispatch_request_sha256 = None
    event.claim_token = None
    event.claimed_by = None
    event.claimed_at = None
    event.lease_expires_at = None
    clean_payload = {
        key: value
        for key, value in event.payload.items()
        if key not in {"release_dispatch_gate", "adapter_dispatch"}
    }
    event.payload = {**clean_payload, "release_gate": gate}


def _assert_decision_idempotency_actor(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
) -> None:
    if not ctx.idempotency_key:
        return
    existing = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.project_id == ctx.project_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.idempotency_key == ctx.idempotency_key,
        )
    )
    if existing is not None and existing.user_id != ctx.user_id:
        raise ApiError(
            "RELEASE_DECISION_IDEMPOTENCY_ACTOR_CONFLICT",
            "发布审批幂等键已绑定其他决策人，不能跨身份重放",
            409,
            details=[{"operation": operation}],
        )


async def decide_release_gate(
    session: Session,
    ctx: RequestContext,
    request: Request,
    run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # A release decision is a natural-person control, not a general privileged
    # write. The global RBAC helper intentionally lets system actors perform
    # maintenance operations, so enforce the human project-admin boundary
    # before idempotency reservation, row locks, audit, or state mutation.
    if (
        ctx.actor_kind != "human"
        or ctx.user_id == "system"
        or "system" in ctx.roles
        or PROJECT_ADMIN_ROLE not in ctx.roles
    ):
        raise ApiError(
            "RELEASE_APPROVAL_HUMAN_ADMIN_REQUIRED",
            "发布审批必须由当前项目的自然人管理员完成",
            403,
            details=[
                {
                    "actor_kind": ctx.actor_kind,
                    "required_role": PROJECT_ADMIN_ROLE,
                }
            ],
        )
    require_any_role(ctx, (PROJECT_ADMIN_ROLE,), "runs.release_gate_decide")
    body_hash = await request_hash(request)
    operation = f"run.release_gate_decision:{run_id}"
    _assert_decision_idempotency_actor(session, ctx, operation=operation)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    _assert_decision_idempotency_actor(session, ctx, operation=operation)
    if replay is not None:
        return public_run_response(replay, ctx)
    # Match the worker's event -> run lock order. Locking every scoped event for
    # this run first avoids the run -> event inversion that can deadlock on
    # MySQL. A release run has only its request event before the decision.
    locked_events = list(
        session.scalars(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.tenant_id == ctx.tenant_id,
                OutboxEvent.project_id == ctx.project_id,
            )
            .order_by(OutboxEvent.event_id)
            .with_for_update()
        )
    )
    record = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"运行不存在：{run_id}", 404)
    if record.run_type not in RELEASE_RUN_TYPES:
        raise ApiError(
            "RUN_RELEASE_DECISION_UNSUPPORTED",
            "该运行不是可审批的发布门禁",
            409,
            details=[{"run_id": run_id, "run_type": record.run_type}],
        )
    if record.status != "blocked":
        raise ApiError(
            "RUN_RELEASE_DECISION_NOT_ALLOWED",
            "只有 blocked 状态的发布门禁可以审批",
            409,
            details=[{"run_id": run_id, "status": record.status}],
        )
    expected_event_type = RELEASE_EVENT_TYPES[record.run_type]
    event = next(
        (
            candidate
            for candidate in locked_events
            if candidate.aggregate_type == record.run_type
            and candidate.event_type == expected_event_type
        ),
        None,
    )
    if event is None:
        raise ApiError(
            "RELEASE_GATE_EVENT_MISSING",
            "发布门禁缺少可恢复的 outbox 事件",
            409,
            details=[{"run_id": run_id}],
        )
    if event.status == "processing" or any(
        value is not None
        for value in (
            event.claim_token,
            event.claimed_by,
            event.claimed_at,
            event.lease_expires_at,
        )
    ):
        raise ApiError(
            "RELEASE_GATE_EVENT_IN_FLIGHT",
            "发布门禁事件仍由 Worker 处理，请稍后重试审批",
            409,
            details=[{"run_id": run_id}],
            retryable=True,
        )
    if event.status != "blocked" or event.delivery_state != "confirmed":
        raise ApiError(
            "RELEASE_GATE_EVENT_NOT_SETTLED",
            "发布门禁事件尚未稳定进入待审批状态，请稍后重试",
            409,
            details=[{"run_id": run_id}],
            retryable=True,
        )
    decision = str(payload.get("decision") or "")
    if record.run_type == "hotword_rollback" and decision == "approved":
        gate = record.payload.get("release_gate")
        requested_by = gate.get("requested_by") if isinstance(gate, dict) else None
        if requested_by == ctx.user_id:
            raise ApiError(
                "HOTWORD_ROLLBACK_APPROVAL_SEPARATION_REQUIRED",
                "热词回滚发起人与项目管理员审批人必须是不同自然人",
                409,
                details=[{"run_id": run_id, "requested_by": requested_by}],
            )
    gate_check = revalidate_control_plane_release(session, record)
    # Before the decision, not-approved is expected; target drift is not.
    if gate_check.get("reason") == "release_gate_event_missing":
        raise ApiError(
            "RELEASE_GATE_EVENT_MISSING",
            "发布门禁缺少可恢复的 outbox 事件",
            409,
            details=[{"run_id": run_id}],
        )
    if gate_check.get("reason") not in {"release_gate_not_approved", None}:
        raise ApiError(
            "RELEASE_GATE_STALE",
            "发布目标在审批前已变化，请重新创建发布门禁",
            409,
            details=[{"run_id": run_id, "reason": gate_check.get("reason")}],
        )
    gate = dict(record.payload.get("release_gate") or {})
    before_gate = dict(gate)
    if (
        record.run_type in CONTROL_PLANE_RELEASE_RUN_TYPES
        and gate.get("requested_by") == ctx.user_id
    ):
        raise ApiError(
            "RELEASE_APPROVAL_SEPARATION_REQUIRED",
            "发布门禁发起人与项目管理员决策人必须是不同自然人",
            409,
            details=[{"run_id": run_id, "requested_by": gate.get("requested_by")}],
        )
    release_decision = {
        "value": decision,
        "reason": payload.get("reason"),
        "actor_id": ctx.user_id,
        "roles": list(ctx.roles),
        "decided_at": datetime.now(UTC).isoformat(),
        "trace_id": ctx.trace_id,
    }
    if record.run_type in CONTROL_PLANE_RELEASE_RUN_TYPES:
        release_decision["decision_sha256"] = _release_decision_sha256(
            gate,
            release_decision,
        )
    gate["decision"] = release_decision
    gate["status"] = "approved" if decision == "approved" else "rejected"
    if decision == "approved":
        transition_run(record, "pending", reason="release_gate_approved")
        _reset_outbox_for_approval(
            event,
            gate,
            available_at=database_utc_now(session),
        )
        next_actions = [
            {"key": "view_trace", "label": "查看 Trace"},
            {"key": "wait_dispatch", "label": "等待发布执行"},
        ]
    else:
        transition_run(record, "cancelled", reason="release_gate_rejected")
        event.status = "cancelled"
        event.delivery_state = "confirmed"
        event.processed_at = datetime.now(UTC)
        event.last_error = "release gate rejected"
        event.claim_token = None
        event.claimed_by = None
        event.claimed_at = None
        event.lease_expires_at = None
        event.payload = {**event.payload, "release_gate": gate}
        next_actions = [
            {"key": "revise_draft", "label": "修改草稿"},
            {"key": "view_trace", "label": "查看 Trace"},
        ]
    clean_run_payload = {
        key: value
        for key, value in record.payload.items()
        if key not in {"release_dispatch_gate", "dispatch_state"}
    }
    record.payload = {
        **clean_run_payload,
        "status": record.status,
        "release_gate": gate,
        "next_actions": next_actions,
    }
    record_audit(
        session,
        ctx,
        action=f"{record.run_type}.release_gate_decided",
        object_type=record.run_type,
        object_id=record.run_id,
        result=gate["status"],
        before={"release_gate_proof": _release_decision_audit_proof(before_gate)},
        after={
            "release_gate_proof": _release_decision_audit_proof(
                gate,
                release_decision,
            )
        },
        trace_id=record.trace_id,
    )
    response = envelope(run_payload(record), ctx)
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
