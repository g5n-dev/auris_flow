from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, or_, select, tuple_
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.logging import get_logger, log_event
from app.models import RunRecord
from app.repositories.outbox_events import database_utc_now
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.run_service import transition_run
from app.services.task_run_control_service import (
    audit_task_run_transition,
    cancel_pending_task_run_dispatch,
    emit_task_run_terminal_event,
    task_run_external_id,
)

logger = get_logger("worker.task_run_monitor")

DEADLINE_STATUSES = frozenset(
    {"queued", "pending", "running", "submitted", "completion_pending", "blocked"}
)
STATUS_SYNC_STATUSES = frozenset({"running", "submitted", "completion_pending", "cancelling"})
ACTIVE_CONTROL_STATUSES = frozenset({"queued", "pending", "running", "submitted"})
CONTROL_DEFINITIONS: dict[str, tuple[str, str]] = {
    "cancel": ("task_run_cancellation", "task_run.cancel_requested"),
    "status_sync": ("task_run_status_sync", "task_run.status_sync_requested"),
}
DEADLINE_REASON = "task_run_deadline_exceeded"
MONITOR_LOCK_BATCH_SIZE = 10

_SourceKey = tuple[str, str, str]


def _database_timestamp(session: Session, value: datetime | None) -> datetime:
    if value is None:
        return database_utc_now(session)
    if value.tzinfo is None:
        return value
    normalized = value.astimezone(UTC)
    if session.get_bind().dialect.name in {"mysql", "sqlite"}:
        return normalized.replace(tzinfo=None)
    return normalized


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _safe_worker_id(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:-]", "_", value.strip())[:64]
    return normalized or "unknown"


def _monitor_context(source: RunRecord, *, worker_id: str, control_id: str) -> RequestContext:
    safe_worker_id = _safe_worker_id(worker_id)
    return RequestContext(
        tenant_id=source.tenant_id,
        project_id=source.project_id,
        user_id=f"system:task-run-monitor:{safe_worker_id}",
        roles=("system",),
        request_id=f"task-run-monitor:{control_id}",
        trace_id=source.trace_id,
        idempotency_key=f"task-run-monitor:{control_id}",
        parent_trace_id=source.trace_id,
        correlation_id=source.trace_id,
        actor_kind="service",
    )


def _control_id(
    source: RunRecord,
    *,
    action: Literal["cancel", "status_sync"],
    cycle: str,
) -> str:
    run_type, _ = CONTROL_DEFINITIONS[action]
    digest = hashlib.sha256(
        (f"v1|{source.tenant_id}|{source.project_id}|{source.run_id}|{action}|{cycle}").encode()
    ).hexdigest()[:24]
    return f"{run_type}_auto_{digest}"


def _source_key(source: RunRecord) -> _SourceKey:
    return source.tenant_id, source.project_id, source.run_id


def _deadline_is_due(source: RunRecord, *, now: datetime) -> bool:
    return (
        source.status in DEADLINE_STATUSES
        and source.deadline_at is not None
        and source.deadline_at <= now
    )


def _planned_control_id(source: RunRecord, *, now: datetime) -> str:
    if _deadline_is_due(source, now=now):
        return _deadline_control_id(source)
    generation = int(source.monitor_generation or 0) + 1
    return _control_id(
        source,
        action="status_sync",
        cycle=f"generation:{generation}",
    )


def _load_control_state(
    session: Session,
    candidates: list[RunRecord],
    *,
    now: datetime,
) -> tuple[dict[_SourceKey, RunRecord], dict[str, RunRecord]]:
    """Load active and deterministic controls in a constant number of queries."""

    source_keys = tuple(sorted({_source_key(source) for source in candidates}))
    planned_control_ids = tuple(
        sorted({_planned_control_id(source, now=now) for source in candidates})
    )
    existing_controls = {
        control.run_id: control
        for control in session.scalars(
            select(RunRecord).where(RunRecord.run_id.in_(planned_control_ids))
        )
    }
    active_controls: dict[_SourceKey, RunRecord] = {}
    active_statement = (
        select(RunRecord)
        .where(
            tuple_(
                RunRecord.tenant_id,
                RunRecord.project_id,
                RunRecord.run_key,
            ).in_(source_keys),
            RunRecord.run_type.in_(tuple(item[0] for item in CONTROL_DEFINITIONS.values())),
            RunRecord.status.in_(tuple(ACTIVE_CONTROL_STATUSES)),
        )
        .order_by(RunRecord.created_at.desc(), RunRecord.run_id.desc())
    )
    for control in session.scalars(active_statement):
        if control.run_key is None:
            continue
        key = (control.tenant_id, control.project_id, control.run_key)
        active_controls.setdefault(key, control)
    return active_controls, existing_controls


def _create_control(
    session: Session,
    source: RunRecord,
    *,
    action: Literal["cancel", "status_sync"],
    control_id: str,
    ctx: RequestContext,
    reason: str,
    external_run_id: str | None,
    engine_dispatch_required: bool,
    now: datetime,
    monitor_generation: int,
    existing_control: RunRecord | None,
) -> RunRecord:
    run_type, event_type = CONTROL_DEFINITIONS[action]
    existing = existing_control
    if existing is not None:
        if (
            existing.tenant_id != source.tenant_id
            or existing.project_id != source.project_id
            or existing.run_type != run_type
            or existing.run_key != source.run_id
        ):
            raise RuntimeError("task-run monitor control identity is bound to another scope")
        return existing

    payload = {
        "run_id": control_id,
        "status": "pending",
        "control_action": action,
        "source_run_id": source.run_id,
        "source_trace_id": source.trace_id,
        "source_status": source.status,
        "source_status_version": int(source.status_version or 1),
        "external_run_id": external_run_id,
        "reason": reason,
        "engine_dispatch_required": engine_dispatch_required,
        "monitor_kind": "deadline" if action == "cancel" else "missed_callback_reconcile",
        "monitor_control_id": control_id,
        "monitor_generation": monitor_generation,
        "monitor_scheduled_at": _iso(now),
        "deadline_at": _iso(source.deadline_at),
        "affected_objects": [{"type": "task_run", "id": source.run_id}],
        "next_actions": [
            {"key": "view_source_run", "label": "查看任务运行", "run_id": source.run_id},
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{source.trace_id}"},
        ],
        "trace_id": source.trace_id,
    }
    control = RunRecord(
        run_id=control_id,
        tenant_id=source.tenant_id,
        project_id=source.project_id,
        run_type=run_type,
        status="pending",
        run_key=source.run_id,
        partition_key=None,
        trace_id=source.trace_id,
        payload=payload,
    )
    session.add(control)
    session.flush()
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type=run_type,
        aggregate_id=control_id,
        payload=payload,
    )
    record_audit(
        session,
        ctx,
        action=f"{run_type}.monitor_create",
        object_type=run_type,
        object_id=control_id,
        after=payload,
        trace_id=source.trace_id,
    )
    return control


def _deadline_control_id(source: RunRecord) -> str:
    return _control_id(
        source,
        action="cancel",
        cycle=(
            f"{_iso(source.deadline_at) or 'missing-deadline'}|"
            f"status-version:{int(source.status_version or 1)}|"
            f"monitor-generation:{int(source.monitor_generation or 0)}"
        ),
    )


def _schedule_deadline_cancellation(
    session: Session,
    source: RunRecord,
    *,
    now: datetime,
    worker_id: str,
    sync_interval_seconds: int,
    active_control: RunRecord | None,
    existing_control: RunRecord | None,
) -> bool:
    control_id = _deadline_control_id(source)
    if existing_control is not None or active_control is not None:
        return False

    external_run_id = task_run_external_id(source)
    before_status = source.status
    local_cancel = False
    if not external_run_id and source.status in {"queued", "pending", "blocked"}:
        local_cancel = source.status == "blocked" or cancel_pending_task_run_dispatch(
            session,
            source,
            now=now,
            reason=DEADLINE_REASON,
        )
        if not local_cancel:
            # Dispatch may already own the outbox lease. Leave the source active;
            # the next sweep will see either the trusted engine binding or the
            # bounded outbox recovery/dead-letter result.
            return False
        transition_run(source, "cancelled", reason=DEADLINE_REASON)
        source.terminal_reason = DEADLINE_REASON
    elif not external_run_id:
        source.next_status_sync_at = now + timedelta(seconds=sync_interval_seconds)
        return False
    else:
        transition_run(source, "cancelling", reason=DEADLINE_REASON)
        source.next_status_sync_at = now + timedelta(seconds=sync_interval_seconds)

    source.cancel_requested_at = now
    source.cancel_reason = DEADLINE_REASON
    ctx = _monitor_context(source, worker_id=worker_id, control_id=control_id)
    action = (
        "task_run.deadline_cancelled"
        if source.status == "cancelled"
        else "task_run.deadline_cancellation_requested"
    )
    audit_task_run_transition(
        session,
        ctx,
        source,
        action=action,
        before_status=before_status,
        reason=DEADLINE_REASON,
    )
    if source.status == "cancelled":
        emit_task_run_terminal_event(session, ctx, source, reason=DEADLINE_REASON)

    control = _create_control(
        session,
        source,
        action="cancel",
        control_id=control_id,
        ctx=ctx,
        reason=DEADLINE_REASON,
        external_run_id=external_run_id,
        engine_dispatch_required=not local_cancel,
        now=now,
        monitor_generation=int(source.monitor_generation or 0),
        existing_control=existing_control,
    )
    source.payload = {
        **source.payload,
        "status": source.status,
        "deadline_monitor": {
            "control_id": control.run_id,
            "scheduled_at": _iso(now),
            "worker_id": _safe_worker_id(worker_id),
            "engine_dispatch_required": not local_cancel,
        },
    }
    return True


def _schedule_status_sync(
    session: Session,
    source: RunRecord,
    *,
    now: datetime,
    worker_id: str,
    sync_interval_seconds: int,
    active_control: RunRecord | None,
    existing_control: RunRecord | None,
) -> bool:
    if active_control is not None:
        source.next_status_sync_at = now + timedelta(seconds=sync_interval_seconds)
        return False
    external_run_id = task_run_external_id(source)
    if not external_run_id:
        source.next_status_sync_at = now + timedelta(seconds=sync_interval_seconds)
        ctx = _monitor_context(
            source,
            worker_id=worker_id,
            control_id=f"deferred:{source.run_id}",
        )
        record_audit(
            session,
            ctx,
            action="task_run.status_sync_deferred",
            object_type="task_run",
            object_id=source.run_id,
            result="deferred",
            before={"status": source.status},
            after={
                "status": source.status,
                "reason": "trusted_engine_binding_unavailable",
                "next_status_sync_at": _iso(source.next_status_sync_at),
            },
            trace_id=source.trace_id,
        )
        return False

    generation = int(source.monitor_generation or 0) + 1
    control_id = _control_id(
        source,
        action="status_sync",
        cycle=f"generation:{generation}",
    )
    ctx = _monitor_context(source, worker_id=worker_id, control_id=control_id)
    source.monitor_generation = generation
    source.next_status_sync_at = now + timedelta(seconds=sync_interval_seconds)
    control = _create_control(
        session,
        source,
        action="status_sync",
        control_id=control_id,
        ctx=ctx,
        reason="periodic_task_run_status_reconciliation",
        external_run_id=external_run_id,
        engine_dispatch_required=True,
        now=now,
        monitor_generation=generation,
        existing_control=existing_control,
    )
    source.payload = {
        **source.payload,
        "task_run_monitor": {
            "last_status_sync_control_id": control.run_id,
            "monitor_generation": generation,
            "scheduled_at": _iso(now),
            "next_status_sync_at": _iso(source.next_status_sync_at),
            "worker_id": _safe_worker_id(worker_id),
        },
    }
    record_audit(
        session,
        ctx,
        action="task_run.status_sync_scheduled",
        object_type="task_run",
        object_id=source.run_id,
        result="scheduled",
        before={"status": source.status, "monitor_generation": generation - 1},
        after={
            "status": source.status,
            "status_version": source.status_version,
            "monitor_generation": generation,
            "control_id": control.run_id,
            "next_status_sync_at": _iso(source.next_status_sync_at),
        },
        trace_id=source.trace_id,
    )
    return True


def _due_candidates(
    session: Session,
    *,
    now: datetime,
    sync_interval_seconds: int,
    limit: int,
    excluded_run_ids: set[str],
) -> list[RunRecord]:
    sync_cutoff = now - timedelta(seconds=sync_interval_seconds)
    fallback_observation = func.coalesce(
        RunRecord.engine_status_observed_at,
        RunRecord.submitted_at,
        RunRecord.started_at,
        RunRecord.updated_at,
        RunRecord.created_at,
    )
    deadline_due = and_(
        RunRecord.status.in_(tuple(DEADLINE_STATUSES)),
        RunRecord.deadline_at.is_not(None),
        RunRecord.deadline_at <= now,
    )
    status_sync_due = and_(
        RunRecord.status.in_(tuple(STATUS_SYNC_STATUSES)),
        or_(
            RunRecord.next_status_sync_at <= now,
            and_(
                RunRecord.next_status_sync_at.is_(None),
                fallback_observation <= sync_cutoff,
            ),
        ),
    )
    statement = (
        select(RunRecord)
        .where(
            RunRecord.run_type == "task_run",
            or_(deadline_due, status_sync_due),
        )
        .order_by(
            case((deadline_due, 0), else_=1),
            RunRecord.deadline_at.asc(),
            RunRecord.next_status_sync_at.asc(),
            RunRecord.run_id.asc(),
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    if excluded_run_ids:
        statement = statement.where(RunRecord.run_id.not_in(sorted(excluded_run_ids)))
    return list(session.scalars(statement))


def monitor_task_runs_once(
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> int:
    """Create deadline/status controls in bounded database transactions.

    At most ``MONITOR_LOCK_BATCH_SIZE`` candidate rows are locked with
    ``SKIP LOCKED`` in each transaction. Active and deterministic controls are
    loaded in bulk for that small batch. Each source state change, control,
    audit rows and outbox event still commits atomically; a second worker either
    skips the row or observes the committed monitor generation.
    """

    runtime_settings = settings or get_settings()
    if not runtime_settings.task_run_monitor_enabled:
        return 0
    batch_limit = min(
        max(int(limit or runtime_settings.task_run_monitor_batch_size), 1),
        runtime_settings.task_run_monitor_batch_size,
    )
    scheduled = 0
    candidate_count = 0
    examined_run_ids: set[str] = set()
    remaining = batch_limit
    while remaining > 0:
        lock_batch_limit = min(remaining, MONITOR_LOCK_BATCH_SIZE)
        with SessionLocal() as session:
            effective_now = _database_timestamp(session, now)
            candidates = _due_candidates(
                session,
                now=effective_now,
                sync_interval_seconds=(runtime_settings.task_run_status_sync_interval_seconds),
                limit=lock_batch_limit,
                excluded_run_ids=examined_run_ids,
            )
            if not candidates:
                break
            active_controls, existing_controls = _load_control_state(
                session,
                candidates,
                now=effective_now,
            )
            for source in candidates:
                source_key = _source_key(source)
                planned_control_id = _planned_control_id(source, now=effective_now)
                if _deadline_is_due(source, now=effective_now):
                    changed = _schedule_deadline_cancellation(
                        session,
                        source,
                        now=effective_now,
                        worker_id=worker_id,
                        sync_interval_seconds=(
                            runtime_settings.task_run_status_sync_interval_seconds
                        ),
                        active_control=active_controls.get(source_key),
                        existing_control=existing_controls.get(planned_control_id),
                    )
                else:
                    changed = _schedule_status_sync(
                        session,
                        source,
                        now=effective_now,
                        worker_id=worker_id,
                        sync_interval_seconds=(
                            runtime_settings.task_run_status_sync_interval_seconds
                        ),
                        active_control=active_controls.get(source_key),
                        existing_control=existing_controls.get(planned_control_id),
                    )
                scheduled += int(changed)
            session.commit()

        current_count = len(candidates)
        candidate_count += current_count
        remaining -= current_count
        examined_run_ids.update(source.run_id for source in candidates)
        if current_count < lock_batch_limit:
            break

    if candidate_count:
        log_event(
            logger,
            "task_run.monitor.batch",
            worker_id=_safe_worker_id(worker_id),
            candidates=candidate_count,
            scheduled=scheduled,
            batch_limit=batch_limit,
            lock_batch_size=MONITOR_LOCK_BATCH_SIZE,
        )
    return scheduled
