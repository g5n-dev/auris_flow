from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.models import OutboxEvent, RunRecord
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    api_error_result,
    raise_replayed_api_error,
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event
from app.services.run_service import public_run_response, run_payload, transition_run

TASK_RUN_CONTROL_ROLES = ("project_admin", "asset_manager", "model_engineer")
TERMINAL_RUN_STATUSES = {"success", "failed", "cancelled"}
CONTROL_ACTIONS = {
    "cancel": ("task_run_cancellation", "task_run.cancel_requested"),
    "status_sync": ("task_run_status_sync", "task_run.status_sync_requested"),
}
ACTIVE_CONTROL_STATUSES = ("queued", "pending", "running", "submitted")


def task_run_external_id(record: RunRecord) -> str | None:
    dispatch = record.payload.get("dispatch")
    details = dispatch.get("details") if isinstance(dispatch, dict) else None
    value = details.get("external_run_id") if isinstance(details, dict) else None
    return str(value) if value else None


def worker_request_context(event: OutboxEvent) -> RequestContext:
    return RequestContext(
        tenant_id=event.tenant_id,
        project_id=event.project_id,
        user_id="system:task-run-control-worker",
        roles=("system",),
        request_id=str(event.payload.get("request_id") or f"outbox-{event.event_id}"),
        trace_id=str(event.payload.get("trace_id") or f"outbox-{event.event_id}"),
        idempotency_key=str(event.payload.get("idempotency_key") or event.dispatch_idempotency_key),
        correlation_id=str(event.payload.get("correlation_id") or "") or None,
        actor_kind="service",
    )


def emit_task_run_terminal_event(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    reason: str,
) -> OutboxEvent | None:
    event_type = {
        "success": "task_run.succeeded",
        "failed": "task_run.failed",
        "cancelled": "task_run.cancelled",
    }.get(record.status)
    if event_type is None or record.run_type != "task_run":
        return None
    event_ctx = replace(
        ctx,
        trace_id=record.trace_id,
        parent_trace_id=(ctx.trace_id if ctx.trace_id != record.trace_id else ctx.parent_trace_id),
        correlation_id=record.trace_id,
    )
    return enqueue_event(
        session,
        event_ctx,
        event_type=event_type,
        aggregate_type="task_run",
        aggregate_id=record.run_id,
        payload={
            "run_id": record.run_id,
            "status": record.status,
            "reason": reason,
            "engine_status": record.engine_status,
            "resource_version": record.status_version,
            "trace_id": record.trace_id,
        },
    )


def audit_task_run_transition(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    action: str,
    before_status: str,
    reason: str,
) -> None:
    record_audit(
        session,
        ctx,
        action=action,
        object_type="task_run",
        object_id=record.run_id,
        result=record.status,
        before={"status": before_status},
        after={
            "status": record.status,
            "status_version": record.status_version,
            "engine_status": record.engine_status,
            "reason": reason,
        },
        trace_id=record.trace_id,
    )


def _source_task_run(
    session: Session,
    ctx: RequestContext,
    run_id: str,
) -> RunRecord:
    record = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "task_run",
        )
        .with_for_update()
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"任务运行不存在：{run_id}", 404)
    return record


def _active_status_sync_control(
    session: Session,
    source: RunRecord,
) -> RunRecord | None:
    return session.scalar(
        select(RunRecord)
        .where(
            RunRecord.tenant_id == source.tenant_id,
            RunRecord.project_id == source.project_id,
            RunRecord.run_type == "task_run_status_sync",
            RunRecord.run_key == source.run_id,
            RunRecord.status.in_(ACTIVE_CONTROL_STATUSES),
        )
        .order_by(RunRecord.created_at.desc(), RunRecord.run_id.desc())
    )


def cancel_pending_task_run_dispatch(
    session: Session,
    source: RunRecord,
    *,
    now: datetime,
    reason: str,
) -> bool:
    event = session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == source.tenant_id,
            OutboxEvent.project_id == source.project_id,
            OutboxEvent.aggregate_type == "task_run",
            OutboxEvent.aggregate_id == source.run_id,
            OutboxEvent.event_type == "task_run.requested",
            OutboxEvent.status == "pending",
        )
        .with_for_update()
    )
    if event is None:
        return False
    event.status = "cancelled"
    event.delivery_state = "confirmed"
    event.processed_at = now
    event.last_error = f"TASK_RUN_CANCELLED_BEFORE_DISPATCH: {reason}"
    return True


async def create_task_run_control(
    session: Session,
    ctx: RequestContext,
    request: Request,
    run_id: str,
    payload: dict[str, Any],
    *,
    action: Literal["cancel", "status_sync"],
) -> dict[str, Any]:
    require_any_role(ctx, TASK_RUN_CONTROL_ROLES, f"task_runs.{action}")
    body_hash = await request_hash(request)
    operation = f"task_run:{run_id}:{action}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        public_replay = public_run_response(replay, ctx)
        raise_replayed_api_error(public_replay)
        return public_replay

    source = _source_task_run(session, ctx, run_id)
    if source.status in TERMINAL_RUN_STATUSES:
        error = ApiError(
            "RUN_ALREADY_TERMINAL",
            f"任务运行已经是终态：{source.status}",
            409,
            details=[{"run_id": source.run_id, "status": source.status}],
        )
        record_audit(
            session,
            ctx,
            action=(
                "task_run.cancellation_rejected"
                if action == "cancel"
                else "task_run.status_sync_rejected"
            ),
            object_type="task_run",
            object_id=source.run_id,
            result="failed",
            before={"status": source.status},
            after={"code": error.code, "status": source.status},
            trace_id=source.trace_id,
        )
        error_response = api_error_result(ctx, error)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=error.status_code,
            response_json=error_response,
        )
        session.commit()
        raise error
    if action == "cancel" and source.status == "cancelling":
        raise ApiError(
            "RUN_CANCELLATION_ALREADY_REQUESTED",
            "任务运行已经收到取消请求",
            409,
            details=[{"run_id": source.run_id}],
        )
    if action == "status_sync":
        active_control = _active_status_sync_control(session, source)
        if active_control is not None:
            response = envelope(run_payload(active_control), ctx)
            record_audit(
                session,
                ctx,
                action="task_run_status_sync.reuse",
                object_type="task_run_status_sync",
                object_id=active_control.run_id,
                result="reused",
                before={"source_run_id": source.run_id},
                after={
                    "source_run_id": source.run_id,
                    "source_status_version": source.status_version,
                    "monitor_generation": int(source.monitor_generation or 0),
                },
                trace_id=source.trace_id,
            )
            save_idempotency_result(
                session,
                ctx,
                operation=operation,
                body_hash=body_hash,
                status_code=202,
                response_json=response,
            )
            session.commit()
            return response

    now = datetime.now(UTC)
    reason = str(payload.get("reason") or "status synchronization requested")
    external_run_id = task_run_external_id(source)
    local_cancel = False
    before_status = source.status
    if action == "cancel":
        source.cancel_requested_at = now
        source.cancel_reason = reason
        if source.status in {"pending", "queued", "blocked"}:
            local_cancel = cancel_pending_task_run_dispatch(session, source, now=now, reason=reason)
            if source.status == "blocked" or local_cancel:
                transition_run(source, "cancelled", reason="cancelled_before_engine_dispatch")
                source.terminal_reason = reason
            else:
                raise ApiError(
                    "RUN_CONTROL_NOT_READY",
                    "运行正在提交执行引擎，请稍后重试取消",
                    409,
                    retryable=True,
                )
        elif not external_run_id:
            raise ApiError(
                "RUN_ENGINE_BINDING_UNAVAILABLE",
                "运行尚未形成可信执行引擎绑定，请稍后重试",
                409,
                retryable=True,
            )
        else:
            transition_run(source, "cancelling", reason="cancellation_requested")
            source.next_status_sync_at = None
        audit_task_run_transition(
            session,
            ctx,
            source,
            action=(
                "task_run.cancelled"
                if source.status == "cancelled"
                else "task_run.cancellation_requested"
            ),
            before_status=before_status,
            reason=reason,
        )
        if source.status == "cancelled":
            emit_task_run_terminal_event(session, ctx, source, reason=reason)
    elif not external_run_id:
        raise ApiError(
            "RUN_ENGINE_BINDING_UNAVAILABLE",
            "运行尚未形成可信执行引擎绑定，不能同步状态",
            409,
            retryable=True,
        )

    run_type, event_type = CONTROL_ACTIONS[action]
    control_id = f"{run_type}_{uuid.uuid4().hex[:12]}"
    control = RunRecord(
        run_id=control_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=run_type,
        status="pending",
        run_key=source.run_id,
        partition_key=None,
        trace_id=ctx.trace_id,
        payload={
            "run_id": control_id,
            "status": "pending",
            "control_action": action,
            "source_run_id": source.run_id,
            "source_trace_id": source.trace_id,
            "source_status_version": int(source.status_version or 1),
            "monitor_generation": int(source.monitor_generation or 0),
            "external_run_id": external_run_id,
            "reason": reason,
            "engine_dispatch_required": not local_cancel,
            "affected_objects": [{"type": "task_run", "id": source.run_id}],
            "next_actions": [
                {"key": "view_source_run", "label": "查看任务运行", "run_id": source.run_id},
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
            ],
            "trace_id": ctx.trace_id,
        },
    )
    session.add(control)
    session.flush()
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type=run_type,
        aggregate_id=control_id,
        payload=control.payload,
    )
    record_audit(
        session,
        ctx,
        action=f"{run_type}.create",
        object_type=run_type,
        object_id=control_id,
        after=control.payload,
        trace_id=control.trace_id,
    )
    response = envelope(run_payload(control), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response_json=response,
    )
    session.commit()
    return response
