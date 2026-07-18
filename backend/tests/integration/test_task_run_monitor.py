from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, func, select, update

from app.core.database import SessionLocal, engine
from app.models import AuditLog, OutboxEvent, RunRecord
from app.services import task_run_monitor_service
from app.services.adapters import DispatchResult
from app.services.run_service import transition_run
from app.services.task_run_monitor_service import monitor_task_runs_once
from app.workers import outbox_worker
from app.workers.outbox_worker import process_aggregate_events


def _create_task_run(client, auth_headers, *, key: str) -> str:
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": f"monitor/{key}",
        },
        headers={**auth_headers, "Idempotency-Key": f"monitor-create-{key}"},
    )
    assert response.status_code == 202, response.text
    return str(response.json()["data"]["run_id"])


def _control_rows(session, source_run_id: str, run_type: str) -> list[RunRecord]:
    return list(
        session.scalars(
            select(RunRecord).where(
                RunRecord.run_key == source_run_id,
                RunRecord.run_type == run_type,
            )
        )
    )


def test_expired_pending_run_is_atomically_cancelled_without_dagster_dispatch(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="pending-timeout")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.deadline_at = now - timedelta(seconds=1)
        source_trace_id = source.trace_id
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now) == 1
    assert monitor_task_runs_once(worker_id="monitor-b", now=now + timedelta(seconds=1)) == 0

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "cancelled"
        assert source.terminal_reason == "task_run_deadline_exceeded"
        assert source.cancel_requested_at is not None
        assert source.finished_at is not None
        requested = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.requested",
            )
        )
        assert requested is not None
        assert requested.status == "cancelled"
        controls = _control_rows(session, run_id, "task_run_cancellation")
        assert len(controls) == 1
        control = controls[0]
        assert control.trace_id == source_trace_id
        assert control.payload["engine_dispatch_required"] is False
        assert control.payload["monitor_kind"] == "deadline"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == control.run_id,
                    OutboxEvent.event_type == "task_run.cancel_requested",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type == "task_run.cancelled",
                )
            )
            == 1
        )
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.object_id == run_id,
                AuditLog.action == "task_run.deadline_cancelled",
            )
        )
        assert audit is not None
        assert audit.actor_id == "system:task-run-monitor:monitor-a"
        assert audit.trace_id == source_trace_id
        assert audit.tenant_id == source.tenant_id
        assert audit.project_id == source.project_id


def test_expired_bound_run_emits_one_fenced_cancel_control(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="bound-timeout")
    assert process_aggregate_events([run_id]) == 1
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "submitted"
        assert source.payload["dispatch"]["details"]["external_run_id"]
        source.deadline_at = now - timedelta(seconds=1)
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now) == 1
    assert monitor_task_runs_once(worker_id="monitor-b", now=now) == 0

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "cancelling"
        assert source.cancel_reason == "task_run_deadline_exceeded"
        controls = _control_rows(session, run_id, "task_run_cancellation")
        assert len(controls) == 1
        control = controls[0]
        assert control.payload["external_run_id"]
        assert control.payload["engine_dispatch_required"] is True
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == control.run_id,
                OutboxEvent.event_type == "task_run.cancel_requested",
            )
        )
        assert event is not None
        assert event.payload["source_status_version"] == source.status_version
        assert event.payload["monitor_control_id"] == control.run_id


def test_superseded_deadline_control_does_not_block_next_fenced_cycle(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="deadline-fence-reschedule")
    assert process_aggregate_events([run_id]) == 1
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.deadline_at = now - timedelta(seconds=1)
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now) == 1
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        controls = _control_rows(session, run_id, "task_run_cancellation")
        assert source is not None and len(controls) == 1
        first_control_id = controls[0].run_id
        transition_run(source, "completion_pending", reason="trusted_callback_race")
        source.engine_status = "CALLBACK_SUCCESS"
        source.engine_status_observed_at = now
        session.commit()

    assert process_aggregate_events([first_control_id]) == 1
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        first_control = session.get(RunRecord, first_control_id)
        assert source is not None and first_control is not None
        assert source.status == "completion_pending"
        assert source.engine_status == "CALLBACK_SUCCESS"
        assert first_control.payload["control_outcome"] == "superseded"

    later = now + timedelta(seconds=1)
    assert monitor_task_runs_once(worker_id="monitor-b", now=later) == 1
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        controls = _control_rows(session, run_id, "task_run_cancellation")
        assert source is not None
        assert source.status == "cancelling"
        assert len(controls) == 2
        assert len({control.run_id for control in controls}) == 2


def test_periodic_reconciliation_is_unique_and_engine_success_is_not_business_success(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="missed-callback")
    assert process_aggregate_events([run_id]) == 1
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.deadline_at = now + timedelta(hours=1)
        source.next_status_sync_at = now - timedelta(seconds=1)
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now) == 1
    assert monitor_task_runs_once(worker_id="monitor-b", now=now) == 0

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.monitor_generation == 1
        controls = _control_rows(session, run_id, "task_run_status_sync")
        assert len(controls) == 1
        first_control_id = controls[0].run_id
        assert controls[0].trace_id == source.trace_id
        assert controls[0].payload["monitor_generation"] == 1

    def dagster_success_status(
        event_type: str,
        aggregate_type: str,
        payload: dict,
        registry=None,
    ) -> DispatchResult:
        del event_type, aggregate_type, registry
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "external_run_id": payload["external_run_id"],
                "dagster_status": "SUCCESS",
                "mode": "test",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", dagster_success_status)
    assert process_aggregate_events([first_control_id]) == 1

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "completion_pending"
        assert source.status != "success"
        assert source.engine_status == "SUCCESS"
        assert source.next_status_sync_at is not None
        source.next_status_sync_at = now - timedelta(seconds=1)
        session.commit()

    later = now + timedelta(minutes=2)
    assert monitor_task_runs_once(worker_id="monitor-b", now=later) == 1
    assert monitor_task_runs_once(worker_id="monitor-c", now=later) == 0
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "completion_pending"
        assert source.monitor_generation == 2
        controls = _control_rows(session, run_id, "task_run_status_sync")
        assert len(controls) == 2
        assert len({control.run_id for control in controls}) == 2


def test_monitor_never_schedules_cross_scope_or_unbound_status_sync(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="unbound")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.status = "running"
        source.deadline_at = now + timedelta(hours=1)
        source.next_status_sync_at = now - timedelta(seconds=1)
        source.payload = {**source.payload, "dispatch": {"adapter": "dagster", "details": {}}}
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now) == 0
    with SessionLocal() as session:
        assert _control_rows(session, run_id, "task_run_status_sync") == []


def test_pre_0042_active_task_run_uses_null_sync_fallback_without_deadline_backfill(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="pre-0042-null-sync-fallback")
    assert process_aggregate_events([run_id]) == 1
    now = datetime.now(UTC)
    legacy_observed_at = now - timedelta(minutes=10)
    with SessionLocal() as session:
        session.execute(
            update(RunRecord)
            .where(RunRecord.run_id == run_id)
            .values(
                deadline_at=None,
                next_status_sync_at=None,
                submitted_at=None,
                started_at=None,
                engine_status_observed_at=None,
                created_at=legacy_observed_at,
                updated_at=legacy_observed_at,
            )
        )
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-upgrade", now=now) == 1
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "submitted"
        assert source.deadline_at is None
        assert source.next_status_sync_at is not None
        assert source.next_status_sync_at.replace(tzinfo=UTC) > now
        assert len(_control_rows(session, run_id, "task_run_status_sync")) == 1
        assert _control_rows(session, run_id, "task_run_cancellation") == []


def test_pre_0042_null_deadline_is_grandfathered_not_implicitly_cancelled(
    client,
    auth_headers,
) -> None:
    run_id = _create_task_run(client, auth_headers, key="pre-0042-deadline-grandfather")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.deadline_at = None
        source.next_status_sync_at = None
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-upgrade", now=now) == 0
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "pending"
        assert source.deadline_at is None
        assert _control_rows(session, run_id, "task_run_cancellation") == []


def test_expired_deadline_is_prioritized_over_reconciliation_when_batch_is_full(
    client,
    auth_headers,
) -> None:
    reconciliation_run_id = _create_task_run(client, auth_headers, key="sync-backlog")
    assert process_aggregate_events([reconciliation_run_id]) == 1
    expired_run_id = _create_task_run(client, auth_headers, key="deadline-priority")
    now = datetime.now(UTC)
    with SessionLocal() as session:
        reconciliation = session.get(RunRecord, reconciliation_run_id)
        expired = session.get(RunRecord, expired_run_id)
        assert reconciliation is not None
        assert expired is not None
        reconciliation.deadline_at = None
        reconciliation.next_status_sync_at = now - timedelta(minutes=5)
        expired.deadline_at = now - timedelta(seconds=1)
        session.commit()

    assert monitor_task_runs_once(worker_id="monitor-a", now=now, limit=1) == 1

    with SessionLocal() as session:
        reconciliation = session.get(RunRecord, reconciliation_run_id)
        expired = session.get(RunRecord, expired_run_id)
        assert reconciliation is not None
        assert expired is not None
        assert reconciliation.monitor_generation == 0
        assert _control_rows(session, reconciliation_run_id, "task_run_status_sync") == []
        assert expired.status == "cancelled"
        assert len(_control_rows(session, expired_run_id, "task_run_cancellation")) == 1


def test_monitor_bulk_loads_control_state_without_per_candidate_run_record_queries(
    client,
    auth_headers,
) -> None:
    now = datetime.now(UTC)
    run_ids = [
        _create_task_run(client, auth_headers, key=f"query-count-{position}")
        for position in range(4)
    ]
    with SessionLocal() as session:
        for run_id in run_ids:
            source = session.get(RunRecord, run_id)
            assert source is not None
            source.deadline_at = now - timedelta(seconds=1)
        session.commit()

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and " from run_records " in normalized:
            statements.append(normalized)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        assert monitor_task_runs_once(worker_id="monitor-query-count", now=now, limit=4) == 4
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    # One candidate read plus at most two bulk control-state reads. The count is
    # independent of the number of source rows in this lock batch.
    assert len(statements) <= 3, statements
    assert not any("run_records.run_key =" in statement for statement in statements)


def test_monitor_splits_large_sweep_into_small_lock_transactions(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    run_ids = [
        _create_task_run(client, auth_headers, key=f"lock-batch-{position}")
        for position in range(11)
    ]
    with SessionLocal() as session:
        for run_id in run_ids:
            source = session.get(RunRecord, run_id)
            assert source is not None
            source.deadline_at = now - timedelta(seconds=1)
        session.commit()

    opened_sessions = []

    def tracked_session_factory():
        session = SessionLocal()
        opened_sessions.append(session)
        return session

    monkeypatch.setattr(task_run_monitor_service, "SessionLocal", tracked_session_factory)

    assert monitor_task_runs_once(worker_id="monitor-lock-batch", now=now, limit=11) == 11
    assert len(opened_sessions) == 2
