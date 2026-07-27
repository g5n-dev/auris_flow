from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import socket
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import Any, cast
from uuid import uuid4

from opentelemetry.context import Context
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings
from app.core.database import SessionLocal, engine
from app.core.logging import get_logger, log_event
from app.core.metrics import metrics
from app.core.observability import (
    annotate_current_span,
    configure_worker_observability,
    extract_remote_trace_context,
    internal_span,
)
from app.core.runtime_guards import failure_injection_enabled
from app.models import (
    ExternalCallbackReceipt,
    OutboxDeliveryAttempt,
    OutboxEvent,
    RunRecord,
)
from app.repositories.outbox_events import (
    OutboxClaim,
    claim_events,
    clear_claim,
    database_utc_now,
    lock_owned_claim,
    owned_claim_trace_carrier,
    renew_claim,
)
from app.services.adapters import (
    DAGSTER_RECONCILIATION_ABSENCE_PROOF,
    DAGSTER_RUN_REQUEST_EVENT_TYPES,
    DispatchResult,
    dispatch_event,
    reconcile_event,
)
from app.services.agentic_execution_service import record_agent_dispatch
from app.services.audit_service import record_audit
from app.services.run_service import transition_run

logger = get_logger("worker.outbox")
DEFAULT_MAX_ATTEMPTS = 3
BUSINESS_COMPLETION_REQUIRED_ADAPTERS = {"dagster", "external_callback", "object_storage"}
PROJECTION_ONLY_TERMINAL_EVENT_TYPES = frozenset(
    {
        "human_review.decision.created",
        "task_run.succeeded",
        "task_run.failed",
        "task_run.cancelled",
    }
)
TASK_RUN_CONTROL_EVENT_TYPES = frozenset(
    {
        "task_run.cancel_requested",
        "task_run.status_sync_requested",
    }
)
DEFAULT_MAX_RECONCILE_ATTEMPTS = 5
OUTCOME_UNKNOWN_ERROR_CODES = {
    "DAGSTER_RUN_REQUEST_FAILED",
    "OBJECT_STORAGE_WRITE_FAILED",
    "QDRANT_UPSERT_FAILED",
    "EXTERNAL_CALLBACK_HTTP_ERROR",
    "EXTERNAL_CALLBACK_SEND_FAILED",
}
TRANSIENT_RELEASE_GATE_REASONS = frozenset({"release_decision_audit_missing"})


@dataclass(frozen=True)
class PreparedDelivery:
    payload: dict[str, Any]
    request_sha256: str
    blocked: bool


@dataclass
class WorkerRuntimeState:
    worker_id: str
    pid: int
    status: str
    started_at: str
    heartbeat_at: str
    iteration_count: int = 0
    processed_total: int = 0
    consecutive_errors: int = 0
    consecutive_idle_polls: int = 0
    current_wait_seconds: float = 0.0
    last_processed_at: str | None = None
    last_successful_poll_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    monitor_enabled: bool = False
    monitor_status: str = "disabled"
    monitor_consecutive_errors: int = 0
    last_monitor_success_at: str | None = None
    last_monitor_error: str | None = None
    last_monitor_error_at: str | None = None
    shutdown_requested: bool = False

    def as_payload(self) -> dict[str, Any]:
        healthy = bool(
            self.status == "running"
            and self.consecutive_errors == 0
            and self.monitor_status in {"disabled", "healthy"}
        )
        return {
            "status": self.status,
            "healthy": healthy,
            "degraded": self.status == "running" and self.monitor_status == "degraded",
            "worker_id": self.worker_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "iteration_count": self.iteration_count,
            "processed_total": self.processed_total,
            "consecutive_errors": self.consecutive_errors,
            "consecutive_idle_polls": self.consecutive_idle_polls,
            "current_wait_seconds": self.current_wait_seconds,
            "last_processed_at": self.last_processed_at,
            "last_successful_poll_at": self.last_successful_poll_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "monitor_enabled": self.monitor_enabled,
            "monitor_status": self.monitor_status,
            "monitor_consecutive_errors": self.monitor_consecutive_errors,
            "last_monitor_success_at": self.last_monitor_success_at,
            "last_monitor_error": self.last_monitor_error,
            "last_monitor_error_at": self.last_monitor_error_at,
            "shutdown_requested": self.shutdown_requested,
        }


class ClaimHeartbeat:
    def __init__(self, claim: OutboxClaim, *, lease_seconds: int) -> None:
        self.claim = claim
        self.lease_seconds = max(5, min(int(lease_seconds), 3600))
        self.interval_seconds = max(1.0, min(self.lease_seconds / 3, 15.0))
        self._stop = Event()
        self._thread = Thread(
            target=self._run,
            name=f"outbox-heartbeat-{claim.event_id}",
            daemon=True,
        )
        self.lost = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                with SessionLocal() as session:
                    renewed = renew_claim(
                        session,
                        self.claim,
                        lease_seconds=self.lease_seconds,
                    )
                    if renewed:
                        session.commit()
                    else:
                        session.rollback()
                if not renewed:
                    self.lost = True
                    return
            except Exception as exc:  # noqa: BLE001 - heartbeat failure fences finalize.
                self.lost = True
                log_event(
                    logger,
                    "outbox.heartbeat.failed",
                    level=40,
                    event_id=self.claim.event_id,
                    lease_generation=self.claim.lease_generation,
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                return


class AdapterDispatchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "ADAPTER_DISPATCH_FAILED",
        retryable: bool = True,
        retry_after_seconds: int | None = None,
        dispatch_payload: dict[str, Any] | None = None,
        remote_outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.dispatch_payload = dispatch_payload
        self.remote_outcome_unknown = remote_outcome_unknown


class PostDispatchFinalizeError(RuntimeError):
    def __init__(self, cause: Exception) -> None:
        super().__init__(f"remote dispatch succeeded but local finalization failed: {cause}")
        self.error_code = "OUTBOX_FINALIZE_AFTER_REMOTE_SUCCESS_FAILED"
        self.retryable = True
        self.retry_after_seconds = None
        self.remote_outcome_unknown = True


def _payload_int(payload: dict, key: str, default: int) -> int:
    value = payload.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _next_available_at(
    event: OutboxEvent,
    *,
    retry_after_seconds: int | None = None,
) -> datetime:
    settings = get_settings()
    requested_base = (
        retry_after_seconds
        if retry_after_seconds is not None
        else _payload_int(
            event.payload,
            "retry_after_seconds",
            settings.outbox_retry_base_seconds,
        )
    )
    base_seconds = max(0, min(requested_base, settings.outbox_retry_max_seconds))
    attempt_number = (
        event.reconcile_attempt_count
        if event.delivery_state in {"outcome_unknown", "reconciling"}
        else event.attempt_count
    )
    exponent = max(attempt_number - 1, 0)
    backoff_seconds = min(base_seconds * (2**exponent), settings.outbox_retry_max_seconds)
    jitter_window = max(0, min(settings.outbox_retry_jitter_seconds, 60))
    jitter_seconds = 0
    if backoff_seconds and jitter_window:
        jitter_seed = hashlib.sha256(
            f"{event.dispatch_idempotency_key}:{event.delivery_state}:{attempt_number}".encode()
        ).hexdigest()
        jitter_seconds = int(jitter_seed[:8], 16) % (jitter_window + 1)
    session = object_session(event)
    if session is None:
        raise RuntimeError("outbox retry scheduling requires an attached session")
    return database_utc_now(session) + timedelta(seconds=backoff_seconds + jitter_seconds)


def _effective_max_attempts(event: OutboxEvent) -> int:
    configured_cap = max(1, min(get_settings().outbox_max_attempts, 100))
    requested = _payload_int(event.payload, "max_attempts", DEFAULT_MAX_ATTEMPTS)
    return max(1, min(requested, configured_cap))


def _effective_max_reconcile_attempts(event: OutboxEvent) -> int:
    requested = _payload_int(
        event.payload,
        "reconcile_max_attempts",
        DEFAULT_MAX_RECONCILE_ATTEMPTS,
    )
    return max(1, min(requested, 20))


def _is_exact_dagster_absence_proof(
    event: OutboxEvent,
    error: Exception,
    *,
    require_reconcile_mode: bool,
) -> bool:
    if event.event_type not in DAGSTER_RUN_REQUEST_EVENT_TYPES:
        return False
    dispatch_payload = getattr(error, "dispatch_payload", None)
    if not isinstance(dispatch_payload, dict):
        return False
    details = dispatch_payload.get("details")
    return bool(
        (not require_reconcile_mode or event.delivery_state == "reconciling")
        and getattr(error, "error_code", None) == "DAGSTER_RECONCILIATION_ABSENT"
        and dispatch_payload.get("adapter") == "dagster"
        and dispatch_payload.get("operation") == "reconcile_run_request"
        and isinstance(details, dict)
        and details.get("reconciled") is False
        and details.get("run_key") == event.dispatch_idempotency_key
        and details.get("absence_proof") == DAGSTER_RECONCILIATION_ABSENCE_PROOF
    )


def _previous_attempt_is_exact_dagster_absence(
    event: OutboxEvent,
    claim: OutboxClaim,
) -> bool:
    session = object_session(event)
    if session is None:
        raise RuntimeError("Dagster absence proof requires an attached session")
    previous = session.scalar(
        select(OutboxDeliveryAttempt)
        .where(
            OutboxDeliveryAttempt.event_id == event.event_id,
            OutboxDeliveryAttempt.lease_generation < claim.lease_generation,
        )
        .order_by(OutboxDeliveryAttempt.lease_generation.desc())
        .limit(1)
    )
    if previous is None:
        return False
    failed_dispatch = previous.details.get("failed_dispatch")
    details = failed_dispatch.get("details") if isinstance(failed_dispatch, dict) else None
    return bool(
        previous.delivery_mode == "reconcile"
        and previous.status == "reconcile_retry_scheduled"
        and previous.error_code == "DAGSTER_RECONCILIATION_ABSENT"
        and previous.adapter == "dagster"
        and previous.operation == "reconcile_run_request"
        and isinstance(details, dict)
        and details.get("reconciled") is False
        and details.get("run_key") == event.dispatch_idempotency_key
        and details.get("absence_proof") == DAGSTER_RECONCILIATION_ABSENCE_PROOF
    )


def _run_for_event(event: OutboxEvent, *, lock: bool = False) -> RunRecord | None:
    # These events only replicate an already-committed terminal decision into the
    # event stream. Their same-ID RunRecord is an audit span, not an executable
    # aggregate, so projection delivery must not re-enter the Run state machine.
    if event.event_type in PROJECTION_ONLY_TERMINAL_EVENT_TYPES:
        return None
    session = object_session(event)
    if session is None:
        return None
    statement = select(RunRecord).where(RunRecord.run_id == event.aggregate_id)
    if lock:
        statement = statement.with_for_update()
    run = session.scalar(statement)
    if run is None:
        return None
    if (
        run.tenant_id != event.tenant_id
        or run.project_id != event.project_id
        or run.run_type != event.aggregate_type
    ):
        raise AdapterDispatchError(
            "outbox event scope or aggregate type does not match its run",
            error_code="OUTBOX_SCOPE_MISMATCH",
            retryable=False,
        )
    return run


def _record_external_callback_receipt(
    event: OutboxEvent,
    run: RunRecord | None,
    dispatch_payload: dict,
) -> None:
    if dispatch_payload.get("adapter") != "external_callback":
        return
    details = dispatch_payload.get("details")
    if not isinstance(details, dict):
        return
    receipt_id = details.get("callback_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return
    session = object_session(event)
    if session is None:
        return
    receipt = session.get(ExternalCallbackReceipt, receipt_id)
    payload = {
        "run_id": event.aggregate_id,
        "run_type": run.run_type if run else event.aggregate_type,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "attempt_count": event.attempt_count,
        "dispatch": dispatch_payload,
        "idempotency_key": details.get("idempotency_key") or event.payload.get("idempotency_key"),
        "remote_trace_id": details.get("remote_trace_id"),
        "request_sha256": details.get("request_sha256"),
        "response_sha256": details.get("response_sha256"),
        "target": details.get("target") or event.payload.get("target"),
    }
    if receipt:
        if receipt.tenant_id != event.tenant_id or receipt.project_id != event.project_id:
            raise AdapterDispatchError(
                "external callback receipt id already belongs to another scope",
                error_code="CALLBACK_RECEIPT_SCOPE_CONFLICT",
                retryable=False,
            )
        prior_dispatch_key = receipt.payload.get("dispatch_idempotency_key")
        if prior_dispatch_key and prior_dispatch_key != event.dispatch_idempotency_key:
            raise AdapterDispatchError(
                "external callback receipt id conflicts with another dispatch",
                error_code="CALLBACK_RECEIPT_DISPATCH_CONFLICT",
                retryable=False,
            )
        receipt.status = "success"
        receipt.trace_id = details.get("trace_id") or event.payload.get("trace_id")
        receipt.payload = {
            **payload,
            "dispatch_idempotency_key": event.dispatch_idempotency_key,
            "dispatch_request_sha256": event.dispatch_request_sha256,
        }
        return
    session.add(
        ExternalCallbackReceipt(
            callback_receipt_id=receipt_id,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            status="success",
            trace_id=details.get("trace_id") or event.payload.get("trace_id"),
            payload={
                **payload,
                "dispatch_idempotency_key": event.dispatch_idempotency_key,
                "dispatch_request_sha256": event.dispatch_request_sha256,
            },
        )
    )


def _dispatch_completion_state(
    dispatch_payload: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, Any]]]:
    adapter = str(dispatch_payload.get("adapter") or "")
    raw_details = dispatch_payload.get("details")
    details = raw_details if isinstance(raw_details, dict) else {}
    if adapter in BUSINESS_COMPLETION_REQUIRED_ADAPTERS:
        external_id = (
            details.get("external_run_id")
            or details.get("storage_object_id")
            or details.get("callback_receipt_id")
        )
        return (
            "submitted",
            "dispatched",
            "awaiting_completion",
            [
                {
                    "key": "view_trace",
                    "label": "查看 Trace",
                },
                {
                    "key": "wait_completion",
                    "label": "等待外部完成回执",
                    "external_id": external_id,
                },
            ],
        )
    return (
        "success",
        "completed",
        "completed",
        [
            {
                "key": "view_trace",
                "label": "查看 Trace",
            },
            {"key": "view_result", "label": "查看结果"},
        ],
    )


def _logical_dispatch_payload(event: OutboxEvent) -> dict[str, Any]:
    payload = dict(event.payload)
    payload.pop("adapter_dispatch", None)
    original_idempotency_key = payload.get("idempotency_key")
    payload.update(
        {
            "delivery_protocol_version": "2",
            "tenant_id": event.tenant_id,
            "project_id": event.project_id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "outbox_event_id": event.event_id,
            "dispatch_idempotency_key": event.dispatch_idempotency_key,
            "idempotency_key": event.dispatch_idempotency_key,
        }
    )
    if original_idempotency_key:
        payload["request_idempotency_key"] = original_idempotency_key
    return payload


def _dispatch_request_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attempt_id(event: OutboxEvent) -> str:
    return f"outbox_attempt_{event.event_id}_{event.lease_generation}"


def _delivery_attempt(
    event: OutboxEvent,
    claim: OutboxClaim,
) -> OutboxDeliveryAttempt:
    session = object_session(event)
    if session is None:
        raise RuntimeError("outbox delivery attempt requires an attached session")
    attempt_id = _attempt_id(event)
    attempt = session.get(OutboxDeliveryAttempt, attempt_id)
    if attempt is None:
        attempt = OutboxDeliveryAttempt(
            attempt_id=attempt_id,
            event_id=event.event_id,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            attempt_number=event.attempt_count,
            lease_generation=claim.lease_generation,
            claimed_by=claim.claimed_by,
            claim_token_sha256=hashlib.sha256(claim.claim_token.encode("utf-8")).hexdigest(),
            delivery_mode="reconcile" if claim.exhausted else "dispatch",
            status="claimed",
            dispatch_idempotency_key=event.dispatch_idempotency_key,
            details={},
        )
        session.add(attempt)
    return attempt


def _record_attempt_prepared(
    event: OutboxEvent,
    claim: OutboxClaim,
    *,
    request_sha256: str,
    blocked: bool,
) -> OutboxDeliveryAttempt:
    attempt = _delivery_attempt(event, claim)
    attempt.status = "blocked" if blocked else "prepared"
    attempt.request_sha256 = request_sha256
    attempt.details = {
        **attempt.details,
        "event_type": event.event_type,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "delivery_mode": attempt.delivery_mode,
    }
    return attempt


def _dispatch_remote_id(dispatch: DispatchResult) -> str | None:
    for key in (
        "external_run_id",
        "storage_object_id",
        "callback_receipt_id",
        "point_id",
        "operation_id",
    ):
        value = dispatch.details.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _complete_attempt(
    event: OutboxEvent,
    claim: OutboxClaim,
    *,
    status: str,
    dispatch: DispatchResult | None = None,
    error: Exception | None = None,
) -> None:
    session = object_session(event)
    if session is None:
        raise RuntimeError("outbox delivery attempt completion requires an attached session")
    attempt = _delivery_attempt(event, claim)
    attempt.status = status
    attempt.request_sha256 = event.dispatch_request_sha256
    attempt.completed_at = database_utc_now(session)
    if dispatch is not None:
        attempt.adapter = dispatch.adapter
        attempt.operation = dispatch.operation
        attempt.remote_id = _dispatch_remote_id(dispatch)
        attempt.details = {
            **attempt.details,
            "dispatch_status": dispatch.status,
            "dispatch_details": dispatch.details,
        }
    if error is not None:
        attempt.error_code = str(
            getattr(error, "error_code", error.__class__.__name__) or error.__class__.__name__
        )[:128]
        attempt.error_message = str(error)[:1024]
        failed_dispatch = getattr(error, "dispatch_payload", None)
        if isinstance(failed_dispatch, dict):
            attempt.adapter = str(failed_dispatch.get("adapter") or "")[:64] or None
            attempt.operation = str(failed_dispatch.get("operation") or "")[:128] or None
            attempt.details = {
                **attempt.details,
                "failed_dispatch": failed_dispatch,
            }


def _task_run_control_source(
    session: Session,
    event: OutboxEvent,
    control: RunRecord,
) -> RunRecord:
    source_run_id = str(control.payload.get("source_run_id") or "")
    source = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == source_run_id,
            RunRecord.tenant_id == event.tenant_id,
            RunRecord.project_id == event.project_id,
            RunRecord.run_type == "task_run",
        )
        .with_for_update()
    )
    if source is None:
        raise AdapterDispatchError(
            "task-run control source is missing or outside the event scope",
            error_code="TASK_RUN_CONTROL_SOURCE_NOT_FOUND",
            retryable=False,
        )
    return source


def _task_run_control_fence(
    event: OutboxEvent,
    control: RunRecord,
    source: RunRecord,
) -> tuple[bool, dict[str, Any]]:
    expected_status_version = control.payload.get("source_status_version")
    expected_monitor_generation = control.payload.get("monitor_generation")
    event_status_version = event.payload.get("source_status_version")
    event_monitor_generation = event.payload.get("monitor_generation")
    current_status_version = int(source.status_version or 1)
    current_monitor_generation = int(source.monitor_generation or 0)
    identity_matches = (
        control.run_key == source.run_id
        and control.payload.get("source_run_id") == source.run_id
        and event.payload.get("source_run_id") == source.run_id
    )
    fence_matches = (
        identity_matches
        and type(expected_status_version) is int
        and type(expected_monitor_generation) is int
        and type(event_status_version) is int
        and type(event_monitor_generation) is int
        and expected_status_version == event_status_version
        and expected_monitor_generation == event_monitor_generation
        and expected_status_version == current_status_version
        and expected_monitor_generation == current_monitor_generation
    )
    return fence_matches, {
        "expected_source_status_version": expected_status_version,
        "event_source_status_version": event_status_version,
        "current_source_status_version": current_status_version,
        "expected_monitor_generation": expected_monitor_generation,
        "event_monitor_generation": event_monitor_generation,
        "current_monitor_generation": current_monitor_generation,
        "identity_matches": identity_matches,
    }


def _supersede_task_run_control(
    event: OutboxEvent,
    control: RunRecord,
    source: RunRecord,
    *,
    phase: str,
    fence: dict[str, Any],
    dispatch_payload: dict[str, Any] | None = None,
) -> None:
    from app.services.task_run_control_service import worker_request_context

    session = cast(Session, object_session(event))
    ctx = worker_request_context(event)
    before_control_status = control.status
    if control.status in {"pending", "queued"}:
        transition_run(control, "running", reason="task_run_control_superseded")
    if control.status == "running":
        transition_run(control, "success", reason="task_run_control_superseded")
    completed_at = datetime.now(UTC).isoformat()
    control.payload = {
        **control.payload,
        "status": control.status,
        **({"dispatch": dispatch_payload} if dispatch_payload is not None else {}),
        "control_outcome": "superseded",
        "no_op": True,
        "superseded_reason": "source_fence_mismatch",
        "superseded_phase": phase,
        **fence,
        "source_status": source.status,
        "completed_at": completed_at,
    }
    record_audit(
        session,
        ctx,
        action=f"{control.run_type}.superseded",
        object_type=control.run_type,
        object_id=control.run_id,
        result="superseded",
        before={"status": before_control_status},
        after={
            "status": control.status,
            "source_run_id": source.run_id,
            "phase": phase,
            **fence,
            "no_op": True,
        },
        trace_id=source.trace_id,
    )
    log_event(
        logger,
        "task_run.control.superseded",
        event_id=event.event_id,
        control_id=control.run_id,
        source_run_id=source.run_id,
        phase=phase,
        **fence,
    )


def _confirm_superseded_task_run_control(
    event: OutboxEvent,
    claim: OutboxClaim,
    control: RunRecord,
    *,
    phase: str,
    fence: dict[str, Any],
) -> None:
    session = object_session(event)
    if session is None:
        raise RuntimeError("task-run control supersede requires an attached session")
    payload = _logical_dispatch_payload(event)
    request_sha256 = _dispatch_request_sha256(payload)
    if event.dispatch_request_sha256 and event.dispatch_request_sha256 != request_sha256:
        raise AdapterDispatchError(
            "outbox dispatch payload changed after its first delivery attempt",
            error_code="OUTBOX_DISPATCH_PAYLOAD_CONFLICT",
            retryable=False,
        )
    event.dispatch_request_sha256 = request_sha256
    attempt = _record_attempt_prepared(
        event,
        claim,
        request_sha256=request_sha256,
        blocked=False,
    )
    attempt.details = {
        **attempt.details,
        "control_id": control.run_id,
        "control_outcome": "superseded",
        "superseded_phase": phase,
        "remote_call_performed": False,
        **fence,
    }
    session.flush()
    event.status = "processed"
    event.delivery_state = "confirmed"
    event.last_error = None
    event.processed_at = database_utc_now(session)
    _complete_attempt(event, claim, status="superseded")
    clear_claim(event)
    metrics.record_worker_processing("success")
    log_event(
        logger,
        "outbox.process.control_superseded",
        event_id=event.event_id,
        event_type=event.event_type,
        aggregate_id=event.aggregate_id,
        control_id=control.run_id,
        phase=phase,
        remote_call_performed=False,
    )


def _prepare_claim(claim: OutboxClaim, *, lease_seconds: int) -> PreparedDelivery | None:
    with SessionLocal() as session:
        event = lock_owned_claim(session, claim)
        if event is None:
            session.rollback()
            return None
        run = _run_for_event(event, lock=True)
        if run and run.status not in {"queued", "pending", "running", "blocked"}:
            raise AdapterDispatchError(
                f"run status {run.status} cannot be externally dispatched",
                error_code="OUTBOX_RUN_NOT_DISPATCHABLE",
                retryable=False,
            )
        if run and run.status in {"pending", "queued"}:
            transition_run(run, "running", reason="outbox_dispatch_started")
        if event.event_type in TASK_RUN_CONTROL_EVENT_TYPES:
            if run is None:
                raise AdapterDispatchError(
                    "task-run control record is missing",
                    error_code="TASK_RUN_CONTROL_NOT_FOUND",
                    retryable=False,
                )
            source = _task_run_control_source(session, event, run)
            fence_matches, fence = _task_run_control_fence(event, run, source)
            if not fence_matches:
                _supersede_task_run_control(
                    event,
                    run,
                    source,
                    phase="prepare",
                    fence=fence,
                )
                _confirm_superseded_task_run_control(
                    event,
                    claim,
                    run,
                    phase="prepare",
                    fence=fence,
                )
                session.commit()
                return None
        if run and event.event_type == "label_version.publish_requested":
            from app.services.label_policy_service import (
                revalidate_label_version_release_dispatch,
            )

            release_gate = revalidate_label_version_release_dispatch(session, run)
            persisted_gate = {
                key: value
                for key, value in release_gate.items()
                if key not in {"approval_ctx", "label_version"}
            }
            if release_gate.get("allowed") is not True:
                run.payload = {
                    **run.payload,
                    "dispatch_state": "release_gate_blocked",
                    "release_dispatch_gate": persisted_gate,
                }
                if run.status != "blocked":
                    transition_run(run, "blocked", reason="release_gate_revalidation_failed")
                event.payload = {**event.payload, "release_dispatch_gate": persisted_gate}
                payload = _logical_dispatch_payload(event)
                request_sha256 = _dispatch_request_sha256(payload)
                event.dispatch_request_sha256 = request_sha256
                _record_attempt_prepared(
                    event,
                    claim,
                    request_sha256=request_sha256,
                    blocked=True,
                )
                session.commit()
                return PreparedDelivery(
                    payload=payload,
                    request_sha256=request_sha256,
                    blocked=True,
                )
        if run and event.event_type in {
            "task_version.publish_requested",
            "settings.publish_requested",
            "hotword_pack_version.rollback-requested",
        }:
            from app.services.release_gate_service import (
                revalidate_control_plane_release,
            )

            release_gate = revalidate_control_plane_release(session, run)
            if release_gate.get("allowed") is not True:
                reason = str(release_gate.get("reason") or "release_gate_revalidation_failed")
                if reason in TRANSIENT_RELEASE_GATE_REASONS:
                    # The approval row, audit proof and outbox reset are committed atomically.
                    # A separate worker can still hold an older read snapshot briefly (notably
                    # with SQLite-based E2E and a fast polling loop). Never dispatch without the
                    # proof, but retry the read instead of turning a valid approval into a
                    # permanent business block. A forged approval has no proof and therefore
                    # exhausts the normal bounded outbox retries without being dispatched.
                    raise AdapterDispatchError(
                        "release decision audit proof is not visible yet",
                        error_code="RELEASE_DECISION_AUDIT_NOT_VISIBLE",
                        retryable=True,
                        retry_after_seconds=1,
                    )
                run.payload = {
                    **run.payload,
                    "dispatch_state": "release_gate_blocked",
                    "release_dispatch_gate": release_gate,
                }
                if run.status != "blocked":
                    transition_run(run, "blocked", reason="release_gate_revalidation_failed")
                event.payload = {**event.payload, "release_dispatch_gate": release_gate}
                payload = _logical_dispatch_payload(event)
                request_sha256 = _dispatch_request_sha256(payload)
                event.dispatch_request_sha256 = request_sha256
                _record_attempt_prepared(
                    event,
                    claim,
                    request_sha256=request_sha256,
                    blocked=True,
                )
                session.commit()
                return PreparedDelivery(
                    payload=payload,
                    request_sha256=request_sha256,
                    blocked=True,
                )
        if run and run.status == "running":
            run.payload = {
                **run.payload,
                "dispatch_state": "dispatching",
                "dispatch_event_id": event.event_id,
                "dispatch_lease_generation": claim.lease_generation,
            }
        payload = _logical_dispatch_payload(event)
        request_sha256 = _dispatch_request_sha256(payload)
        if event.dispatch_request_sha256 and event.dispatch_request_sha256 != request_sha256:
            raise AdapterDispatchError(
                "outbox dispatch payload changed after its first delivery attempt",
                error_code="OUTBOX_DISPATCH_PAYLOAD_CONFLICT",
                retryable=False,
            )
        event.dispatch_request_sha256 = request_sha256
        _record_attempt_prepared(
            event,
            claim,
            request_sha256=request_sha256,
            blocked=bool(run and run.status == "blocked"),
        )
        now = database_utc_now(session)
        event.lease_expires_at = now + timedelta(seconds=max(5, min(int(lease_seconds), 3600)))
        session.commit()
        return PreparedDelivery(
            payload={
                **payload,
                "outbox_fencing_token": f"{claim.event_id}:{claim.lease_generation}",
                "delivery_mode": "reconcile" if claim.exhausted else "dispatch",
            },
            request_sha256=request_sha256,
            blocked=bool(run and run.status == "blocked"),
        )


def _mark_remote_operation_started(claim: OutboxClaim) -> bool:
    with SessionLocal() as session:
        event = lock_owned_claim(session, claim)
        if event is None:
            session.rollback()
            return False
        if event.event_type in TASK_RUN_CONTROL_EVENT_TYPES:
            control = _run_for_event(event, lock=True)
            if control is None:
                raise AdapterDispatchError(
                    "task-run control record is missing",
                    error_code="TASK_RUN_CONTROL_NOT_FOUND",
                    retryable=False,
                )
            source = _task_run_control_source(session, event, control)
            fence_matches, fence = _task_run_control_fence(event, control, source)
            if not fence_matches:
                _supersede_task_run_control(
                    event,
                    control,
                    source,
                    phase="remote_start",
                    fence=fence,
                )
                _confirm_superseded_task_run_control(
                    event,
                    claim,
                    control,
                    phase="remote_start",
                    fence=fence,
                )
                session.commit()
                return False
        attempt = _delivery_attempt(event, claim)
        if claim.exhausted:
            event.delivery_state = "reconciling"
            attempt.status = "reconcile_started"
        else:
            # Commit before the remote call. A crash after this point must query
            # the remote receipt rather than repeat the external write.
            event.delivery_state = "outcome_unknown"
            attempt.status = "remote_call_started"
        session.commit()
        return True


def _dispatch_prepared(prepared: PreparedDelivery) -> DispatchResult:
    payload = prepared.payload
    if failure_injection_enabled() and (
        payload.get("force_worker_error") or payload.get("simulate_worker_failure")
    ):
        raise RuntimeError(str(payload.get("failure_reason", "simulated worker failure")))
    delivery = reconcile_event if payload.get("delivery_mode") == "reconcile" else dispatch_event
    with internal_span(
        "outbox.adapter.dispatch",
        attributes={
            "auris.event_type": str(payload["event_type"]),
            "auris.aggregate_type": str(payload["aggregate_type"]),
            "auris.delivery_mode": str(payload.get("delivery_mode") or "dispatch"),
            "auris.business_trace_id": str(payload.get("trace_id") or "unknown"),
        },
    ) as span:
        dispatch = delivery(
            str(payload["event_type"]),
            str(payload["aggregate_type"]),
            payload,
        )
        span.set_attribute("auris.adapter", dispatch.adapter[:64])
        span.set_attribute("auris.adapter_operation", dispatch.operation[:128])
        span.set_attribute("auris.adapter_status", dispatch.status[:32])
    if dispatch.status != "success":
        failed_dispatch = {
            "adapter": dispatch.adapter,
            "operation": dispatch.operation,
            "status": dispatch.status,
            "error_code": dispatch.error_code,
            "error_message": dispatch.error_message,
            "retryable": dispatch.retryable,
            "retry_after_seconds": dispatch.retry_after_seconds,
            "details": dispatch.details,
        }
        raise AdapterDispatchError(
            dispatch.error_message or "adapter dispatch failed",
            error_code=dispatch.error_code or "ADAPTER_DISPATCH_FAILED",
            retryable=dispatch.retryable,
            retry_after_seconds=dispatch.retry_after_seconds,
            dispatch_payload=failed_dispatch,
            remote_outcome_unknown=(
                bool(dispatch.details.get("outcome_unknown"))
                or str(dispatch.error_code or "") in OUTCOME_UNKNOWN_ERROR_CODES
            ),
        )
    return dispatch


def _mark_blocked(event: OutboxEvent, claim: OutboxClaim) -> None:
    run = _run_for_event(event)
    event.status = "blocked"
    event.delivery_state = "confirmed"
    event.last_error = "run is blocked by release gate or human confirmation"
    session = object_session(event)
    if session is None:
        raise RuntimeError("outbox blocking requires an attached session")
    event.processed_at = database_utc_now(session)
    _complete_attempt(event, claim, status="blocked")
    clear_claim(event)
    metrics.record_worker_processing("blocked")
    log_event(
        logger,
        "outbox.process.blocked",
        level=30,
        event_id=event.event_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        run_status=run.status if run else None,
    )


def _finalize_task_run_control(
    event: OutboxEvent,
    control: RunRecord,
    dispatch_payload: dict[str, Any],
) -> bool:
    if event.event_type not in TASK_RUN_CONTROL_EVENT_TYPES:
        return False
    session = cast(Session, object_session(event))
    source = _task_run_control_source(session, event, control)

    from app.services.task_run_control_service import (
        audit_task_run_transition,
        emit_task_run_terminal_event,
        worker_request_context,
    )

    ctx = worker_request_context(event)
    fence_matches, fence = _task_run_control_fence(event, control, source)
    if not fence_matches:
        _supersede_task_run_control(
            event,
            control,
            source,
            phase="finalize",
            fence=fence,
            dispatch_payload=dispatch_payload,
        )
        return True

    details = dispatch_payload.get("details")
    details = details if isinstance(details, dict) else {}
    observed = str(
        details.get("dagster_status") or details.get("engine_status") or "UNKNOWN"
    ).upper()
    source.engine_status = observed
    observed_at = datetime.now(UTC)
    source.engine_status_observed_at = observed_at
    before_status = source.status
    reason = str(control.payload.get("reason") or "task-run control")

    if event.event_type == "task_run.cancel_requested":
        if source.status not in {"success", "failed", "cancelled"}:
            if observed in {"CANCELED", "CANCELLED"}:
                transition_run(source, "cancelled", reason="dagster_cancellation_confirmed")
                source.terminal_reason = source.cancel_reason or reason
            elif observed == "FAILURE":
                transition_run(source, "failed", reason="dagster_failed_during_cancellation")
                source.terminal_reason = "dagster_failed_during_cancellation"
            elif observed == "SUCCESS":
                transition_run(
                    source, "completion_pending", reason="dagster_completed_before_cancel"
                )
        action = (
            f"task_run.{source.status}"
            if source.status in {"failed", "cancelled"}
            else "task_run.cancellation_observed"
        )
    else:
        if source.status not in {"success", "failed", "cancelled"}:
            if observed in {"NOT_STARTED", "QUEUED", "STARTING"}:
                if source.status == "running":
                    transition_run(source, "submitted", reason="dagster_status_reconciled")
            elif observed == "STARTED" and source.status == "submitted":
                transition_run(source, "running", reason="dagster_status_reconciled")
            elif observed == "SUCCESS" and source.status in {
                "submitted",
                "running",
                "cancelling",
            }:
                # Engine success is not trusted business success. A bound signed
                # completion receipt must still materialize the business result.
                transition_run(source, "completion_pending", reason="dagster_success_observed")
            elif observed == "FAILURE":
                transition_run(source, "failed", reason="dagster_failure_observed")
                source.terminal_reason = "dagster_failure_observed"
            elif observed in {"CANCELED", "CANCELLED"}:
                transition_run(source, "cancelled", reason="dagster_cancellation_observed")
                source.terminal_reason = source.cancel_reason or "dagster_cancellation_observed"
        action = (
            f"task_run.{source.status}"
            if source.status in {"failed", "cancelled"}
            else "task_run.engine_status_observed"
        )

    source.payload = {
        **source.payload,
        "status": source.status,
        "engine_status": observed,
        "engine_status_observed_at": source.engine_status_observed_at.isoformat(),
    }
    source.next_status_sync_at = (
        None
        if source.status in {"success", "failed", "cancelled"}
        else observed_at + timedelta(seconds=get_settings().task_run_status_sync_interval_seconds)
    )
    audit_task_run_transition(
        session,
        ctx,
        source,
        action=action,
        before_status=before_status,
        reason=reason,
    )
    if source.status in {"failed", "cancelled"} and source.status != before_status:
        emit_task_run_terminal_event(session, ctx, source, reason=source.terminal_reason or reason)

    # A signed Dagster success callback can arrive after the source entered
    # `cancelling` but before this fenced engine observation. Resolve that
    # durable receipt only against the authoritative status seen by this
    # control attempt; cancellation/failure wins reject it, SUCCESS applies it.
    from app.services.run_service import resolve_cancellation_race_dagster_completion

    resolve_cancellation_race_dagster_completion(
        session,
        source,
        observed_status=observed,
    )

    if control.status == "running":
        transition_run(control, "success", reason="task_run_control_completed")
    control.payload = {
        **control.payload,
        "status": control.status,
        "dispatch": dispatch_payload,
        "observed_engine_status": observed,
        "source_status": source.status,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    return True


def _finalize_success(
    event: OutboxEvent,
    dispatch: DispatchResult,
    prepared: PreparedDelivery,
    claim: OutboxClaim,
) -> None:
    run = _run_for_event(event)
    if run and run.status == "blocked":
        raise AdapterDispatchError(
            "run became blocked after dispatch preparation",
            error_code="OUTBOX_RUN_BLOCKED_AFTER_PREPARE",
            retryable=False,
        )
    release_materialization: dict[str, Any] | None = None
    if run and event.event_type == "label_version.publish_requested":
        from app.services.label_policy_service import materialize_label_version_release

        release_gate = materialize_label_version_release(
            cast(Session, object_session(event)),
            run,
        )
        release_materialization = {
            key: value
            for key, value in release_gate.items()
            if key not in {"approval_ctx", "label_version"}
        }
        if release_gate.get("allowed") is not True:
            run.payload = {
                **run.payload,
                "dispatch_state": "release_gate_blocked",
                "release_dispatch_gate": release_materialization,
            }
            if run.status != "blocked":
                transition_run(run, "blocked", reason="release_gate_changed_before_commit")
            event.payload = {
                **event.payload,
                "release_dispatch_gate": release_materialization,
            }
            _mark_blocked(event, claim)
            return
    if run and event.event_type in {
        "task_version.publish_requested",
        "settings.publish_requested",
        "hotword_pack_version.rollback-requested",
    }:
        from app.services.release_gate_service import (
            materialize_control_plane_release,
        )

        release_materialization = materialize_control_plane_release(
            cast(Session, object_session(event)),
            run,
        )
        if release_materialization.get("allowed") is not True:
            run.payload = {
                **run.payload,
                "dispatch_state": "release_gate_blocked",
                "release_dispatch_gate": release_materialization,
            }
            if run.status != "blocked":
                transition_run(run, "blocked", reason="release_target_changed_before_commit")
            event.payload = {
                **event.payload,
                "release_dispatch_gate": release_materialization,
            }
            _mark_blocked(event, claim)
            return

    details = {
        **dispatch.details,
        "dispatch_idempotency_key": event.dispatch_idempotency_key,
        "dispatch_request_sha256": prepared.request_sha256,
        "fencing_token": f"{claim.event_id}:{claim.lease_generation}",
        **(
            {"label_release": release_materialization}
            if release_materialization is not None
            else {}
        ),
    }
    dispatch_payload = {
        "adapter": dispatch.adapter,
        "operation": dispatch.operation,
        "status": dispatch.status,
        "details": details,
    }
    event.payload = {**event.payload, "adapter_dispatch": dispatch_payload}
    if run and _finalize_task_run_control(event, run, dispatch_payload):
        pass
    if run and event.event_type == "release_deployment.command-requested":
        from app.services.prompt_release_service import mark_release_command_dispatched

        mark_release_command_dispatched(
            cast(Session, object_session(event)),
            run,
            dispatch_payload,
        )
    _record_external_callback_receipt(event, run, dispatch_payload)
    if run and run.status in {"pending", "queued"}:
        transition_run(run, "running", reason="outbox_dispatch_started")
    if run and run.status == "running":
        target_status, dispatch_state, business_status, next_actions = _dispatch_completion_state(
            dispatch_payload
        )
        transition_run(
            run,
            target_status,
            reason=(
                "outbox_dispatch_submitted"
                if target_status == "submitted"
                else "outbox_dispatch_completed"
            ),
        )
        run.payload = {
            **run.payload,
            "status": target_status,
            "processed_event_id": event.event_id,
            "dispatch": dispatch_payload,
            "dispatch_state": dispatch_state,
            "business_status": business_status,
            "business_completion_required": target_status == "submitted",
            "completion_mode": (
                "external_receipt_required" if target_status == "submitted" else "dispatch_is_final"
            ),
            **(
                {"release_materialization": release_materialization}
                if release_materialization is not None
                else {}
            ),
            "next_actions": [
                {
                    **action,
                    **(
                        {"route": f"traces/{run.trace_id}"}
                        if action.get("key") == "view_trace"
                        else {}
                    ),
                }
                for action in next_actions
            ],
        }
        if (
            event.event_type == "task_run.requested"
            and run.run_type == "task_run"
            and run.payload.get("execution_contract") == "auris-flow-audio-import-v1"
        ):
            from app.services.audio_import_completion_service import (
                mark_audio_import_batch_running,
            )

            active_session = object_session(run)
            if active_session is None:
                raise RuntimeError("audio import dispatch requires an attached session")
            mark_audio_import_batch_running(active_session, run)
        if event.event_type in {
            "task_run.requested",
            "audio_intelligence.requested",
        } and run.run_type in {"task_run", "audio_intelligence"}:
            from app.services.run_service import apply_staged_dagster_completion

            apply_staged_dagster_completion(
                cast(Session, object_session(event)),
                run,
            )
        session = object_session(run)
        if session is not None:
            record_agent_dispatch(session, run, dispatch_payload)
    event.status = "processed"
    event.delivery_state = "confirmed"
    event.last_error = None
    session = object_session(event)
    if session is None:
        raise RuntimeError("outbox finalization requires an attached session")
    event.processed_at = database_utc_now(session)
    _complete_attempt(event, claim, status="succeeded", dispatch=dispatch)
    clear_claim(event)
    metrics.record_worker_processing("success")
    if dispatch.adapter == "external_callback":
        metrics.record_callback_outcome("success")
    log_event(
        logger,
        "outbox.process.success",
        event_id=event.event_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        run_status=run.status if run else None,
        adapter=dispatch.adapter,
        operation=dispatch.operation,
        dispatch_idempotency_key=event.dispatch_idempotency_key,
        lease_generation=claim.lease_generation,
    )


def _mark_retry_or_dead_letter(
    event: OutboxEvent,
    error: Exception,
    claim: OutboxClaim,
) -> None:
    try:
        run = _run_for_event(event)
    except AdapterDispatchError:
        run = None
    error_code = getattr(error, "error_code", error.__class__.__name__)
    retryable = bool(getattr(error, "retryable", True))
    retry_after_seconds = getattr(error, "retry_after_seconds", None)
    requires_reconciliation = claim.exhausted or bool(
        getattr(error, "remote_outcome_unknown", False)
    )
    attempts_used = (
        event.reconcile_attempt_count if requires_reconciliation else event.attempt_count
    )
    max_attempts = (
        _effective_max_reconcile_attempts(event)
        if requires_reconciliation
        else _effective_max_attempts(event)
    )
    failed_dispatch = getattr(error, "dispatch_payload", None)
    if isinstance(failed_dispatch, dict):
        event.payload = {**event.payload, "adapter_dispatch": failed_dispatch}

    consecutive_dagster_absence = bool(
        claim.exhausted
        and _is_exact_dagster_absence_proof(
            event,
            error,
            require_reconcile_mode=True,
        )
        and _previous_attempt_is_exact_dagster_absence(event, claim)
    )
    if consecutive_dagster_absence and event.attempt_count < _effective_max_attempts(event):
        # The only safe automatic re-dispatch is a Dagster launch request for
        # which two consecutive exact-tag queries proved that no remote run
        # exists. The proof count is derived from immutable delivery attempts;
        # it is intentionally absent from caller-controlled business payloads.
        event.last_error = f"{error_code}: {error}" if error_code else str(error)
        event.status = "pending"
        event.delivery_state = "ready"
        event.available_at = _next_available_at(
            event,
            retry_after_seconds=retry_after_seconds,
        )
        event.processed_at = None
        _complete_attempt(
            event,
            claim,
            status="dagster_redispatch_scheduled",
            error=error,
        )
        clear_claim(event)
        metrics.record_worker_processing("retry")
        if run:
            run.payload = {
                **run.payload,
                "status": run.status,
                "retryable": True,
                "retry_count": event.attempt_count,
                "retry_mode": "dagster_safe_redispatch",
                "next_retry_at": event.available_at.isoformat(),
                "failed_event_id": event.event_id,
                "error_code": error_code,
                "error": event.last_error,
                "dispatch": event.payload.get("adapter_dispatch"),
                "dispatch_state": "dagster_redispatch_wait",
                "next_actions": [
                    {
                        "key": "dagster_redispatch_scheduled",
                        "label": "等待 Dagster 安全重派",
                        "available_at": event.available_at.isoformat(),
                    },
                    {
                        "key": "view_trace",
                        "label": "查看 Trace",
                        "route": f"traces/{run.trace_id}",
                    },
                ],
            }
        log_event(
            logger,
            "outbox.dagster.redispatch_scheduled",
            level=30,
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            attempt_count=event.attempt_count,
            reconcile_attempt_count=event.reconcile_attempt_count,
            dispatch_idempotency_key=event.dispatch_idempotency_key,
            available_at=event.available_at.isoformat(),
        )
        return

    if consecutive_dagster_absence:
        error = AdapterDispatchError(
            "Dagster safe re-dispatch limit is exhausted",
            error_code="DAGSTER_REDISPATCH_ATTEMPTS_EXHAUSTED",
            retryable=False,
            dispatch_payload=failed_dispatch if isinstance(failed_dispatch, dict) else None,
            remote_outcome_unknown=True,
        )
        error_code = error.error_code
        retryable = error.retryable
        retry_after_seconds = error.retry_after_seconds
        requires_reconciliation = True
        attempts_used = event.reconcile_attempt_count
        max_attempts = _effective_max_reconcile_attempts(event)
    event.last_error = f"{error_code}: {error}" if error_code else str(error)
    if run and run.status in {"pending", "queued"} and isinstance(error, AdapterDispatchError):
        transition_run(run, "running", reason="outbox_dispatch_started")
    if not retryable or attempts_used >= max_attempts:
        event.status = "dead_letter"
        event.delivery_state = "unresolved" if requires_reconciliation else "failed"
        session = object_session(event)
        if session is None:
            raise RuntimeError("outbox dead-lettering requires an attached session")
        event.processed_at = database_utc_now(session)
        _complete_attempt(event, claim, status="dead_letter", error=error)
        clear_claim(event)
        metrics.record_worker_processing("dead_letter")
        if event.event_type == "external_callback.requested":
            metrics.record_callback_outcome("dead_letter")
        if run and run.status in {"pending", "queued", "running"}:
            if run.status in {"pending", "queued"}:
                transition_run(run, "running", reason="outbox_dispatch_started")
            before_terminal_status = run.status
            transition_run(run, "failed", reason="outbox_dispatch_dead_letter")
            run.terminal_reason = "outbox_dispatch_dead_letter"
            run.payload = {
                **run.payload,
                "status": "failed",
                "failed_event_id": event.event_id,
                "error_code": error_code,
                "error": event.last_error,
                "retryable": retryable,
                "retry_count": attempts_used,
                "retry_mode": "reconcile" if requires_reconciliation else "dispatch",
                "next_retry_at": None,
                "dead_letter_event_id": event.event_id,
                "dispatch": event.payload.get("adapter_dispatch"),
                "dispatch_state": "dead_letter",
                "next_actions": [
                    {"key": "retry", "label": "重试运行"},
                    {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{run.trace_id}"},
                ],
            }
            if event.event_type == "task_run.requested" and run.run_type == "task_run":
                # The original TaskRun launch has reached a terminal local
                # decision. Persist the audit transition and terminal business
                # event atomically with the dead-letter state so observers never
                # see a failed run without its durable evidence.
                from app.services.task_run_control_service import (
                    audit_task_run_transition,
                    emit_task_run_terminal_event,
                    worker_request_context,
                )

                ctx = worker_request_context(event)
                audit_task_run_transition(
                    session,
                    ctx,
                    run,
                    action="task_run.failed",
                    before_status=before_terminal_status,
                    reason="outbox_dispatch_dead_letter",
                )
                emit_task_run_terminal_event(
                    session,
                    ctx,
                    run,
                    reason="outbox_dispatch_dead_letter",
                )
        log_event(
            logger,
            "outbox.process.dead_letter",
            level=40,
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            attempt_count=event.attempt_count,
            reconcile_attempt_count=event.reconcile_attempt_count,
            delivery_state=event.delivery_state,
            error=event.last_error,
        )
        return

    event.status = "pending"
    event.delivery_state = "outcome_unknown" if requires_reconciliation else "ready"
    event.available_at = _next_available_at(
        event,
        retry_after_seconds=retry_after_seconds,
    )
    event.processed_at = None
    _complete_attempt(
        event,
        claim,
        status="reconcile_retry_scheduled" if requires_reconciliation else "retry_scheduled",
        error=error,
    )
    clear_claim(event)
    metrics.record_worker_processing("retry")
    if event.event_type == "external_callback.requested":
        metrics.record_callback_outcome("retry")
    if run:
        run.payload = {
            **run.payload,
            "status": run.status,
            "retryable": True,
            "retry_count": attempts_used,
            "retry_mode": "reconcile" if requires_reconciliation else "dispatch",
            "next_retry_at": event.available_at.isoformat(),
            "failed_event_id": event.event_id,
            "error_code": error_code,
            "error": event.last_error,
            "dispatch": event.payload.get("adapter_dispatch"),
            "dispatch_state": "reconcile_wait" if requires_reconciliation else "retry_wait",
            "next_actions": [
                {
                    "key": "reconcile_scheduled" if requires_reconciliation else "retry_scheduled",
                    "label": "等待远端状态核对" if requires_reconciliation else "等待自动重试",
                    "available_at": event.available_at.isoformat(),
                },
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{run.trace_id}"},
            ],
        }
    log_event(
        logger,
        "outbox.process.retry_scheduled",
        level=30,
        event_id=event.event_id,
        event_type=event.event_type,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        attempt_count=event.attempt_count,
        reconcile_attempt_count=event.reconcile_attempt_count,
        delivery_state=event.delivery_state,
        error_code=error_code,
        retryable=retryable,
        available_at=event.available_at.isoformat(),
        error=event.last_error,
    )


def _worker_identity(explicit_worker_id: str | None = None) -> str:
    if explicit_worker_id:
        return explicit_worker_id[:128]
    configured = os.environ.get("AURIS_OUTBOX_WORKER_ID", "").strip()
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"[:128]


def _claim_batch(
    *,
    limit: int,
    worker_id: str,
    aggregate_ids: list[str] | None = None,
) -> list[OutboxClaim]:
    settings = get_settings()
    bounded_limit = min(max(limit, 0), max(1, settings.outbox_claim_batch_size))
    max_retries = max(0, min(settings.outbox_claim_retries, 10))
    retry_base_seconds = max(0, min(settings.outbox_claim_retry_base_ms, 1000)) / 1000
    for attempt in range(max_retries + 1):
        try:
            with SessionLocal() as session:
                claims = claim_events(
                    session,
                    worker_id=worker_id,
                    limit=bounded_limit,
                    lease_seconds=settings.outbox_lease_seconds,
                    max_attempts_cap=max(1, settings.outbox_max_attempts),
                    aggregate_ids=aggregate_ids,
                )
                session.commit()
            return claims
        except OperationalError as exc:
            original_error = exc.orig
            mysql_error_code = (
                original_error.args[0]
                if original_error is not None and len(original_error.args) > 0
                else None
            )
            if mysql_error_code not in {1205, 1213} or attempt >= max_retries:
                raise
            time.sleep(retry_base_seconds * (attempt + 1))
    return []


def _finalize_failure(claim: OutboxClaim, error: Exception) -> bool:
    with SessionLocal() as session:
        event = lock_owned_claim(session, claim)
        if event is None:
            session.rollback()
            log_event(
                logger,
                "outbox.finalize.fenced",
                level=30,
                event_id=claim.event_id,
                lease_generation=claim.lease_generation,
                outcome="failure",
            )
            return False
        _mark_retry_or_dead_letter(event, error, claim)
        session.commit()
        return True


def _finalize_blocked(claim: OutboxClaim) -> bool:
    with SessionLocal() as session:
        event = lock_owned_claim(session, claim)
        if event is None:
            session.rollback()
            return False
        _mark_blocked(event, claim)
        session.commit()
        return True


def _finalize_dispatch(
    claim: OutboxClaim,
    prepared: PreparedDelivery,
    dispatch: DispatchResult,
) -> bool:
    with SessionLocal() as session:
        event = lock_owned_claim(session, claim)
        if event is None:
            session.rollback()
            log_event(
                logger,
                "outbox.finalize.fenced",
                level=30,
                event_id=claim.event_id,
                lease_generation=claim.lease_generation,
                outcome="success",
            )
            return False
        _finalize_success(event, dispatch, prepared, claim)
        session.commit()
        return True


def _process_claim_traced(claim: OutboxClaim) -> None:
    settings = get_settings()
    log_event(
        logger,
        "outbox.process.start",
        event_id=claim.event_id,
        claimed_by=claim.claimed_by,
        lease_generation=claim.lease_generation,
        lease_expires_at=claim.lease_expires_at.isoformat(),
    )
    try:
        prepared = _prepare_claim(claim, lease_seconds=settings.outbox_lease_seconds)
    except Exception as exc:  # noqa: BLE001 - claim must finish through the fenced path.
        _finalize_failure(claim, exc)
        return
    if prepared is None:
        return
    annotate_current_span(
        business_trace_id=str(prepared.payload.get("trace_id") or "unknown"),
        request_id=str(prepared.payload.get("request_id") or f"outbox-{claim.event_id}"),
    )
    if prepared.blocked:
        _finalize_blocked(claim)
        return
    if not _mark_remote_operation_started(claim):
        log_event(
            logger,
            "outbox.remote_operation.fenced_before_start",
            level=30,
            event_id=claim.event_id,
            lease_generation=claim.lease_generation,
        )
        return
    heartbeat = ClaimHeartbeat(claim, lease_seconds=settings.outbox_lease_seconds)
    heartbeat.start()
    try:
        try:
            dispatch = _dispatch_prepared(prepared)
        except Exception as exc:  # noqa: BLE001 - adapter errors become retry/dead-letter state.
            _finalize_failure(claim, exc)
            return
    finally:
        heartbeat.stop()
    if heartbeat.lost:
        log_event(
            logger,
            "outbox.heartbeat.lost_before_finalize",
            level=30,
            event_id=claim.event_id,
            lease_generation=claim.lease_generation,
            dispatch_idempotency_key=prepared.payload.get("dispatch_idempotency_key"),
        )
    try:
        finalized = _finalize_dispatch(claim, prepared, dispatch)
        if heartbeat.lost and not finalized:
            log_event(
                logger,
                "outbox.dispatch.awaiting_reconcile",
                level=30,
                event_id=claim.event_id,
                lease_generation=claim.lease_generation,
                dispatch_idempotency_key=prepared.payload.get("dispatch_idempotency_key"),
            )
    except Exception as exc:  # noqa: BLE001 - preserve retryability after a remote success.
        _finalize_failure(claim, PostDispatchFinalizeError(exc))


def _claim_parent_context(claim: OutboxClaim) -> Context | None:
    try:
        with SessionLocal() as session:
            carrier = owned_claim_trace_carrier(session, claim)
    except Exception as exc:  # noqa: BLE001 - telemetry cannot stop business delivery.
        log_event(
            logger,
            "outbox.trace_carrier.lookup_failed",
            level=30,
            event_id=claim.event_id,
            lease_generation=claim.lease_generation,
            error_code=exc.__class__.__name__,
        )
        return None
    return extract_remote_trace_context(carrier)


def _process_claim(claim: OutboxClaim) -> None:
    with internal_span(
        "outbox.process",
        parent_context=_claim_parent_context(claim),
        attributes={
            "messaging.system": "auris-outbox",
            "messaging.operation.name": "process",
            "auris.outbox_event_id": claim.event_id,
            "auris.outbox_lease_generation": claim.lease_generation,
        },
    ):
        _process_claim_traced(claim)


def _process_claims(claims: list[OutboxClaim]) -> int:
    for claim in claims:
        try:
            _process_claim(claim)
        except Exception as exc:  # noqa: BLE001 - one event cannot stop the worker batch.
            metrics.record_worker_processing("failure")
            log_event(
                logger,
                "outbox.process.unhandled",
                level=40,
                event_id=claim.event_id,
                lease_generation=claim.lease_generation,
                error=f"{exc.__class__.__name__}: {exc}",
            )
    return len(claims)


def process_once(limit: int = 50, *, worker_id: str | None = None) -> int:
    claims = _claim_batch(limit=limit, worker_id=_worker_identity(worker_id))
    return _process_claims(claims)


def process_aggregate_events(
    aggregate_ids: Iterable[str],
    limit: int = 50,
    *,
    worker_id: str | None = None,
) -> int:
    unique_ids = [aggregate_id for aggregate_id in dict.fromkeys(aggregate_ids) if aggregate_id]
    if not unique_ids:
        return 0

    claims = _claim_batch(
        limit=limit,
        worker_id=_worker_identity(worker_id),
        aggregate_ids=unique_ids,
    )
    return _process_claims(claims)


def monitor_task_runs_once(*, worker_id: str | None = None) -> int:
    settings = get_settings()
    if not settings.task_run_monitor_enabled:
        return 0
    from app.services.task_run_monitor_service import monitor_task_runs_once as monitor_once

    return monitor_once(
        worker_id=_worker_identity(worker_id),
        limit=settings.task_run_monitor_batch_size,
        settings=settings,
    )


def _monitor_task_runs_isolated(
    *,
    worker_id: str,
    state: WorkerRuntimeState | None = None,
) -> int:
    try:
        processed = monitor_task_runs_once(worker_id=worker_id)
    except Exception as exc:  # noqa: BLE001 - monitor failure must not stop outbox delivery.
        error = f"{exc.__class__.__name__}: {exc}"
        if state is not None:
            state.monitor_status = "degraded"
            state.monitor_consecutive_errors += 1
            state.last_monitor_error = error
            state.last_monitor_error_at = _utc_now_iso()
            state.last_error = error
            state.last_error_at = state.last_monitor_error_at
        log_event(
            logger,
            "task_run.monitor.failed",
            level=40,
            worker_id=worker_id,
            error=error,
        )
        return 0
    if state is not None:
        state.monitor_status = "healthy"
        state.monitor_consecutive_errors = 0
        state.last_monitor_success_at = _utc_now_iso()
    return processed


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_float(value: float, *, minimum: float, maximum: float) -> float:
    if not math.isfinite(value):
        return minimum
    return max(minimum, min(value, maximum))


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _environment_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _write_worker_health(path: Path, state: WorkerRuntimeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{state.pid}.tmp")
    try:
        temporary.write_text(
            json.dumps(state.as_payload(), ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_worker_health(
    state: WorkerRuntimeState,
    health_path: Path | None,
) -> None:
    if health_path is None:
        return
    try:
        _write_worker_health(health_path, state)
    except Exception as exc:  # noqa: BLE001 - observability failure cannot stop dispatch.
        log_event(
            logger,
            "outbox.worker.health_write_failed",
            level=40,
            worker_id=state.worker_id,
            health_path=str(health_path),
            error=f"{exc.__class__.__name__}: {exc}",
        )


@contextmanager
def graceful_shutdown_signals(stop_event: Event) -> Iterator[None]:
    watched_signals = (signal.SIGINT, signal.SIGTERM)
    previous_handlers = {signum: signal.getsignal(signum) for signum in watched_signals}

    def request_shutdown(signum: int, _frame: object) -> None:
        if not stop_event.is_set():
            log_event(
                logger,
                "outbox.worker.shutdown_requested",
                signal=signal.Signals(signum).name,
            )
        stop_event.set()

    try:
        for signum in watched_signals:
            signal.signal(signum, request_shutdown)
        yield
    finally:
        for signum in watched_signals:
            signal.signal(signum, previous_handlers[signum])


def run_forever(
    limit: int = 50,
    *,
    worker_id: str | None = None,
    stop_event: Event | None = None,
    poll_interval_seconds: float = 0.5,
    max_idle_wait_seconds: float = 5.0,
    error_backoff_base_seconds: float = 1.0,
    error_backoff_max_seconds: float = 30.0,
    heartbeat_interval_seconds: float = 15.0,
    health_path: str | Path | None = None,
) -> WorkerRuntimeState:
    bounded_limit = max(1, min(int(limit), 1000))
    poll_interval = _bounded_float(float(poll_interval_seconds), minimum=0.01, maximum=60.0)
    max_idle_wait = _bounded_float(
        float(max_idle_wait_seconds), minimum=poll_interval, maximum=300.0
    )
    error_backoff_base = _bounded_float(
        float(error_backoff_base_seconds), minimum=0.01, maximum=300.0
    )
    error_backoff_max = _bounded_float(
        float(error_backoff_max_seconds), minimum=error_backoff_base, maximum=3600.0
    )
    heartbeat_interval = _bounded_float(
        float(heartbeat_interval_seconds), minimum=0.1, maximum=300.0
    )
    resolved_worker_id = _worker_identity(worker_id)
    resolved_health_path = Path(health_path).expanduser() if health_path else None
    shutdown = stop_event or Event()
    started_at = _utc_now_iso()
    settings = get_settings()
    monitor_enabled = bool(settings.task_run_monitor_enabled)
    state = WorkerRuntimeState(
        worker_id=resolved_worker_id,
        pid=os.getpid(),
        status="running",
        started_at=started_at,
        heartbeat_at=started_at,
        monitor_enabled=monitor_enabled,
        monitor_status="pending" if monitor_enabled else "disabled",
    )
    last_heartbeat = time.monotonic()
    next_task_run_monitor_at = 0.0
    _publish_worker_health(state, resolved_health_path)
    log_event(
        logger,
        "outbox.worker.started",
        worker_id=resolved_worker_id,
        pid=state.pid,
        limit=bounded_limit,
        poll_interval_seconds=poll_interval,
        max_idle_wait_seconds=max_idle_wait,
        error_backoff_base_seconds=error_backoff_base,
        error_backoff_max_seconds=error_backoff_max,
        heartbeat_interval_seconds=heartbeat_interval,
        health_path=str(resolved_health_path) if resolved_health_path else None,
    )

    while not shutdown.is_set():
        state.iteration_count += 1
        processed = 0
        try:
            monotonic_now = time.monotonic()
            monitored = 0
            if monitor_enabled and monotonic_now >= next_task_run_monitor_at:
                monitored = _monitor_task_runs_isolated(
                    worker_id=resolved_worker_id,
                    state=state,
                )
                next_task_run_monitor_at = monotonic_now + settings.task_run_monitor_poll_seconds
            processed = monitored + process_once(bounded_limit, worker_id=resolved_worker_id)
            now = _utc_now_iso()
            state.last_successful_poll_at = now
            state.consecutive_errors = 0
            state.processed_total += processed
            if processed:
                state.consecutive_idle_polls = 0
                state.last_processed_at = now
                wait_seconds = 0.0
            else:
                state.consecutive_idle_polls += 1
                idle_exponent = min(state.consecutive_idle_polls - 1, 20)
                wait_seconds = min(
                    poll_interval * (2**idle_exponent),
                    max_idle_wait,
                )
        except Exception as exc:  # noqa: BLE001 - daemon must survive transient failures.
            state.consecutive_errors += 1
            state.consecutive_idle_polls = 0
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            state.last_error_at = _utc_now_iso()
            error_exponent = min(state.consecutive_errors - 1, 20)
            wait_seconds = min(
                error_backoff_base * (2**error_exponent),
                error_backoff_max,
            )
            log_event(
                logger,
                "outbox.worker.iteration_failed",
                level=40,
                worker_id=resolved_worker_id,
                iteration_count=state.iteration_count,
                consecutive_errors=state.consecutive_errors,
                retry_in_seconds=wait_seconds,
                error=state.last_error,
            )

        state.current_wait_seconds = wait_seconds
        state.heartbeat_at = _utc_now_iso()
        _publish_worker_health(state, resolved_health_path)
        monotonic_now = time.monotonic()
        if monotonic_now - last_heartbeat >= heartbeat_interval:
            log_event(
                logger,
                "outbox.worker.heartbeat",
                worker_id=resolved_worker_id,
                pid=state.pid,
                iteration_count=state.iteration_count,
                processed_total=state.processed_total,
                processed=processed,
                consecutive_errors=state.consecutive_errors,
                monitor_status=state.monitor_status,
                monitor_consecutive_errors=state.monitor_consecutive_errors,
                consecutive_idle_polls=state.consecutive_idle_polls,
                next_poll_in_seconds=wait_seconds,
            )
            last_heartbeat = monotonic_now

        if wait_seconds > 0 and shutdown.wait(wait_seconds):
            break

    state.shutdown_requested = shutdown.is_set()
    state.status = "stopping"
    state.current_wait_seconds = 0.0
    state.heartbeat_at = _utc_now_iso()
    _publish_worker_health(state, resolved_health_path)
    log_event(
        logger,
        "outbox.worker.stopping",
        worker_id=resolved_worker_id,
        pid=state.pid,
        iteration_count=state.iteration_count,
        processed_total=state.processed_total,
    )
    state.status = "stopped"
    state.heartbeat_at = _utc_now_iso()
    _publish_worker_health(state, resolved_health_path)
    log_event(
        logger,
        "outbox.worker.stopped",
        worker_id=resolved_worker_id,
        pid=state.pid,
        iteration_count=state.iteration_count,
        processed_total=state.processed_total,
    )
    return state


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch Auris Flow outbox events.")
    parser.add_argument(
        "--once",
        action="store_true",
        default=os.environ.get("AURIS_OUTBOX_RUN_ONCE", "").lower() in {"1", "true", "yes"},
        help="Process one claim batch and exit.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_environment_int("AURIS_OUTBOX_WORKER_LIMIT", 50),
    )
    parser.add_argument("--worker-id", default=os.environ.get("AURIS_OUTBOX_WORKER_ID"))
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=_environment_float("AURIS_OUTBOX_POLL_INTERVAL_SECONDS", 0.5),
    )
    parser.add_argument(
        "--max-idle-wait",
        type=float,
        default=_environment_float("AURIS_OUTBOX_MAX_IDLE_WAIT_SECONDS", 5.0),
    )
    parser.add_argument(
        "--error-backoff-base",
        type=float,
        default=_environment_float("AURIS_OUTBOX_ERROR_BACKOFF_BASE_SECONDS", 1.0),
    )
    parser.add_argument(
        "--error-backoff-max",
        type=float,
        default=_environment_float("AURIS_OUTBOX_ERROR_BACKOFF_MAX_SECONDS", 30.0),
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=_environment_float("AURIS_OUTBOX_HEARTBEAT_INTERVAL_SECONDS", 15.0),
    )
    parser.add_argument(
        "--health-file",
        default=os.environ.get("AURIS_OUTBOX_HEALTH_PATH") or None,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    worker_id = _worker_identity(args.worker_id)
    observability = configure_worker_observability(get_settings(), engine=engine)
    try:
        if args.once:
            count = _monitor_task_runs_isolated(worker_id=worker_id) + process_once(
                args.limit, worker_id=worker_id
            )
            log_event(
                logger,
                "outbox.process.complete",
                worker_id=worker_id,
                processed=count,
                mode="once",
            )
            return

        stop_event = Event()
        with graceful_shutdown_signals(stop_event):
            run_forever(
                limit=args.limit,
                worker_id=worker_id,
                stop_event=stop_event,
                poll_interval_seconds=args.poll_interval,
                max_idle_wait_seconds=args.max_idle_wait,
                error_backoff_base_seconds=args.error_backoff_base,
                error_backoff_max_seconds=args.error_backoff_max,
                heartbeat_interval_seconds=args.heartbeat_interval,
                health_path=args.health_file,
            )
    finally:
        observability.shutdown()


if __name__ == "__main__":
    main()
