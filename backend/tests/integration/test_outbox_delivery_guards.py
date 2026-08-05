from __future__ import annotations

import time
from datetime import timedelta
from threading import Event, Thread

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import OutboxDeliveryAttempt, OutboxEvent, RunRecord
from app.repositories.outbox_events import database_utc_now
from app.services.adapters import DispatchResult
from app.services.run_service import transition_run
from app.workers import outbox_worker

pytestmark = pytest.mark.usefixtures("configured_test_business_execution_contracts")


def _create_task_run(client, auth_headers, *, key: str, max_attempts: int = 3) -> str:
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": f"aurora_auto/BJ-AURORA-001/2025-05-26/{key}",
            "max_attempts": max_attempts,
        },
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202
    return response.json()["data"]["run_id"]


def test_retry_keeps_dispatch_request_hash_stable(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(client, auth_headers, key="stable-dispatch-hash")
    calls: list[str] = []

    def flaky_dispatch(event_type: str, aggregate_type: str, payload: dict):
        calls.append(str(payload["dispatch_idempotency_key"]))
        if len(calls) == 1:
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                error_code="TRANSIENT_DOWNSTREAM",
                error_message="temporary downstream failure",
                retryable=True,
                retry_after_seconds=0,
            )
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"external_run_id": "dg-run-stable-hash"},
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", flaky_dispatch)

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        first_event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        first_hash = first_event.dispatch_request_sha256
        assert first_event.status == "pending"
        assert first_hash

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        run = session.get(RunRecord, run_id)
        attempts = list(
            session.scalars(
                select(OutboxDeliveryAttempt)
                .where(OutboxDeliveryAttempt.event_id == event.event_id)
                .order_by(OutboxDeliveryAttempt.lease_generation)
            )
        )
        assert event.status == "processed"
        assert event.dispatch_request_sha256 == first_hash
        assert run is not None
        assert run.status == "submitted"
        assert [attempt.status for attempt in attempts] == ["retry_scheduled", "succeeded"]
        assert {attempt.dispatch_idempotency_key for attempt in attempts} == {calls[0]}
        assert attempts[1].remote_id == "dg-run-stable-hash"

    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_expired_last_attempt_reconciles_with_same_idempotency_key(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(
        client,
        auth_headers,
        key="reconcile-last-attempt",
        max_attempts=1,
    )
    dispatch_keys: list[str] = []
    reconcile_keys: list[str] = []
    original_finalize = outbox_worker._finalize_dispatch

    def successful_dispatch(event_type: str, aggregate_type: str, payload: dict):
        dispatch_keys.append(str(payload["dispatch_idempotency_key"]))
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"external_run_id": "dg-run-reconciled"},
        )

    def successful_reconcile(event_type: str, aggregate_type: str, payload: dict):
        reconcile_keys.append(str(payload["dispatch_idempotency_key"]))
        return DispatchResult(
            adapter="dagster",
            operation="reconcile_run_request",
            details={"external_run_id": "dg-run-reconciled", "reconciled": True},
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", successful_dispatch)
    monkeypatch.setattr(outbox_worker, "reconcile_event", successful_reconcile)
    monkeypatch.setattr(outbox_worker, "_finalize_dispatch", lambda *args: False)

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.status == "processing"
        event.lease_expires_at = database_utc_now(session) - timedelta(seconds=1)
        session.commit()

    monkeypatch.setattr(outbox_worker, "_finalize_dispatch", original_finalize)
    assert outbox_worker.process_once() == 1

    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        run = session.get(RunRecord, run_id)
        attempts = list(
            session.scalars(
                select(OutboxDeliveryAttempt)
                .where(OutboxDeliveryAttempt.event_id == event.event_id)
                .order_by(OutboxDeliveryAttempt.lease_generation)
            )
        )
        assert event.status == "processed"
        assert event.attempt_count == 1
        assert run is not None
        assert run.status == "submitted"
        assert [attempt.status for attempt in attempts] == ["lease_expired", "succeeded"]
        assert [attempt.delivery_mode for attempt in attempts] == ["dispatch", "reconcile"]
        assert attempts[0].dispatch_idempotency_key == attempts[1].dispatch_idempotency_key

    assert len(dispatch_keys) == 1
    assert len(reconcile_keys) == 1
    assert dispatch_keys[0] == reconcile_keys[0]


def test_finalize_exception_enters_reconcile_and_transient_query_recovers_without_redispatch(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(
        client,
        auth_headers,
        key="finalize-exception-reconcile",
        max_attempts=1,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_retry_base_seconds", 0)
    monkeypatch.setattr(settings, "outbox_retry_jitter_seconds", 0)
    dispatch_keys: list[str] = []
    reconcile_keys: list[str] = []
    original_finalize = outbox_worker._finalize_dispatch
    finalize_calls = 0

    def successful_dispatch(event_type: str, aggregate_type: str, payload: dict):
        dispatch_keys.append(str(payload["dispatch_idempotency_key"]))
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"external_run_id": "dg-run-finalize-exception"},
        )

    def transient_then_successful_reconcile(
        event_type: str,
        aggregate_type: str,
        payload: dict,
    ):
        reconcile_keys.append(str(payload["dispatch_idempotency_key"]))
        if len(reconcile_keys) == 1:
            return DispatchResult(
                adapter="dagster",
                operation="reconcile_run_request",
                status="failed",
                error_code="DAGSTER_RECONCILIATION_FAILED",
                error_message="temporary query timeout",
                retryable=True,
                retry_after_seconds=0,
            )
        return DispatchResult(
            adapter="dagster",
            operation="reconcile_run_request",
            details={
                "external_run_id": "dg-run-finalize-exception",
                "reconciled": True,
            },
        )

    def fail_first_finalize(*args, **kwargs):
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            raise RuntimeError("database commit failed after remote success")
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(outbox_worker, "dispatch_event", successful_dispatch)
    monkeypatch.setattr(
        outbox_worker,
        "reconcile_event",
        transient_then_successful_reconcile,
    )
    monkeypatch.setattr(outbox_worker, "_finalize_dispatch", fail_first_finalize)

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.status == "pending"
        assert event.delivery_state == "outcome_unknown"
        assert event.attempt_count == 1
        assert event.reconcile_attempt_count == 0

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.status == "pending"
        assert event.delivery_state == "outcome_unknown"
        assert event.reconcile_attempt_count == 1

    assert outbox_worker.process_once() == 1
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        run = session.get(RunRecord, run_id)
        attempts = list(
            session.scalars(
                select(OutboxDeliveryAttempt)
                .where(OutboxDeliveryAttempt.event_id == event.event_id)
                .order_by(OutboxDeliveryAttempt.lease_generation)
            )
        )
        assert event.status == "processed"
        assert event.delivery_state == "confirmed"
        assert event.attempt_count == 1
        assert event.reconcile_attempt_count == 2
        assert run is not None and run.status == "submitted"
        assert [attempt.delivery_mode for attempt in attempts] == [
            "dispatch",
            "reconcile",
            "reconcile",
        ]
        assert [attempt.status for attempt in attempts] == [
            "reconcile_retry_scheduled",
            "reconcile_retry_scheduled",
            "succeeded",
        ]

    assert len(dispatch_keys) == 1
    assert len(reconcile_keys) == 2
    assert {dispatch_keys[0], *reconcile_keys} == {dispatch_keys[0]}


def _dagster_absence(run_key: str) -> DispatchResult:
    return DispatchResult(
        adapter="dagster",
        operation="reconcile_run_request",
        status="failed",
        details={
            "reconciled": False,
            "run_key": run_key,
            "absence_proof": "dagster-exact-dispatch-tag-absent-v1",
        },
        error_code="DAGSTER_RECONCILIATION_ABSENT",
        error_message="Dagster exact dispatch tag is absent",
        retryable=True,
        retry_after_seconds=0,
    )


def _ambiguous_dagster_launch() -> DispatchResult:
    return DispatchResult(
        adapter="dagster",
        operation="run_request",
        status="failed",
        error_code="DAGSTER_RUN_REQUEST_FAILED",
        error_message="Dagster GraphQL request failed",
        retryable=True,
        retry_after_seconds=0,
    )


def test_two_consecutive_dagster_absence_proofs_safely_redispatch_with_same_key(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(
        client,
        auth_headers,
        key="dagster-outage-safe-redispatch",
        max_attempts=3,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_retry_base_seconds", 0)
    monkeypatch.setattr(settings, "outbox_retry_jitter_seconds", 0)
    calls: list[tuple[str, str]] = []

    def dispatch(event_type: str, aggregate_type: str, payload: dict):
        del event_type, aggregate_type
        run_key = str(payload["dispatch_idempotency_key"])
        calls.append(("dispatch", run_key))
        if sum(mode == "dispatch" for mode, _ in calls) == 1:
            return _ambiguous_dagster_launch()
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"external_run_id": "dagster-run-after-recovery"},
        )

    def reconcile(event_type: str, aggregate_type: str, payload: dict):
        del event_type, aggregate_type
        run_key = str(payload["dispatch_idempotency_key"])
        calls.append(("reconcile", run_key))
        return _dagster_absence(run_key)

    monkeypatch.setattr(outbox_worker, "dispatch_event", dispatch)
    monkeypatch.setattr(outbox_worker, "reconcile_event", reconcile)

    for _ in range(4):
        assert outbox_worker.process_once() == 1

    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        run = session.get(RunRecord, run_id)
        attempts = list(
            session.scalars(
                select(OutboxDeliveryAttempt)
                .where(OutboxDeliveryAttempt.event_id == event.event_id)
                .order_by(OutboxDeliveryAttempt.lease_generation)
            )
        )
        assert event.status == "processed"
        assert event.delivery_state == "confirmed"
        assert event.attempt_count == 2
        assert event.reconcile_attempt_count == 2
        assert run is not None and run.status == "submitted"
        assert [attempt.status for attempt in attempts] == [
            "reconcile_retry_scheduled",
            "reconcile_retry_scheduled",
            "dagster_redispatch_scheduled",
            "succeeded",
        ]
        assert not any("absence" in key for key in event.payload)
        assert not any("redispatch" in key for key in event.payload)
        assert len({attempt.dispatch_idempotency_key for attempt in attempts}) == 1

    assert [mode for mode, _ in calls] == [
        "dispatch",
        "reconcile",
        "reconcile",
        "dispatch",
    ]
    assert len({run_key for _, run_key in calls}) == 1


def test_dagster_absence_must_be_consecutive_before_redispatch(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(
        client,
        auth_headers,
        key="dagster-nonconsecutive-absence",
        max_attempts=3,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_retry_base_seconds", 0)
    monkeypatch.setattr(settings, "outbox_retry_jitter_seconds", 0)
    dispatch_count = 0
    reconcile_count = 0

    def ambiguous_dispatch(event_type: str, aggregate_type: str, payload: dict):
        nonlocal dispatch_count
        del event_type, aggregate_type, payload
        dispatch_count += 1
        return _ambiguous_dagster_launch()

    def nonconsecutive_reconcile(event_type: str, aggregate_type: str, payload: dict):
        nonlocal reconcile_count
        del event_type, aggregate_type
        reconcile_count += 1
        if reconcile_count == 2:
            return DispatchResult(
                adapter="dagster",
                operation="reconcile_run_request",
                status="failed",
                error_code="DAGSTER_RECONCILIATION_FAILED",
                error_message="temporary status query failure",
                retryable=True,
                retry_after_seconds=0,
            )
        return _dagster_absence(str(payload["dispatch_idempotency_key"]))

    monkeypatch.setattr(outbox_worker, "dispatch_event", ambiguous_dispatch)
    monkeypatch.setattr(outbox_worker, "reconcile_event", nonconsecutive_reconcile)

    # launch failure, absence, transient query failure, then one new absence proof
    for _ in range(4):
        assert outbox_worker.process_once() == 1

    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.status == "pending"
        assert event.delivery_state == "outcome_unknown"
        assert event.attempt_count == 1
        assert event.reconcile_attempt_count == 3
    assert dispatch_count == 1
    assert reconcile_count == 3


def test_dagster_safe_redispatch_respects_dispatch_attempt_cap(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(
        client,
        auth_headers,
        key="dagster-redispatch-attempt-cap",
        max_attempts=1,
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_retry_base_seconds", 0)
    monkeypatch.setattr(settings, "outbox_retry_jitter_seconds", 0)
    dispatch_count = 0

    def ambiguous_dispatch(event_type: str, aggregate_type: str, payload: dict):
        nonlocal dispatch_count
        del event_type, aggregate_type, payload
        dispatch_count += 1
        return _ambiguous_dagster_launch()

    def absent_reconcile(event_type: str, aggregate_type: str, payload: dict):
        del event_type, aggregate_type
        return _dagster_absence(str(payload["dispatch_idempotency_key"]))

    monkeypatch.setattr(outbox_worker, "dispatch_event", ambiguous_dispatch)
    monkeypatch.setattr(outbox_worker, "reconcile_event", absent_reconcile)

    for _ in range(3):
        assert outbox_worker.process_once() == 1

    with SessionLocal() as session:
        event = (
            session.query(OutboxEvent)
            .filter_by(aggregate_id=run_id, event_type="task_run.requested")
            .one()
        )
        run = session.get(RunRecord, run_id)
        assert event.status == "dead_letter"
        assert event.delivery_state == "unresolved"
        assert event.attempt_count == 1
        assert event.reconcile_attempt_count == 2
        assert event.last_error is not None
        assert "DAGSTER_REDISPATCH_ATTEMPTS_EXHAUSTED" in event.last_error
        assert run is not None and run.status == "failed"
    assert dispatch_count == 1


def test_heartbeat_prevents_takeover_during_slow_dispatch(
    client,
    auth_headers,
    monkeypatch,
):
    run_id = _create_task_run(client, auth_headers, key="slow-dispatch-heartbeat")
    settings = get_settings()
    monkeypatch.setattr(settings, "outbox_lease_seconds", 5)
    started = Event()
    release = Event()
    processed: list[int] = []

    def slow_dispatch(event_type: str, aggregate_type: str, payload: dict):
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test did not release slow dispatch")
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"external_run_id": "dg-run-slow-heartbeat"},
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", slow_dispatch)

    worker = Thread(
        target=lambda: processed.append(outbox_worker.process_once(worker_id="heartbeat-owner")),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=3)
    try:
        with SessionLocal() as session:
            run = session.get(RunRecord, run_id)
            assert run is not None
            with pytest.raises(ApiError) as error:
                transition_run(run, "blocked", reason="late_release_gate")
            assert error.value.code == "RUN_DISPATCH_IN_PROGRESS"
            session.rollback()
        time.sleep(5.2)
        assert outbox_worker.process_once(worker_id="takeover-worker") == 0
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert processed == [1]
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.status == "processed"
        assert event.attempt_count == 1
