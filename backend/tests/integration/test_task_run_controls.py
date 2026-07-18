from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.completion_signature import completion_signature_message
from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    OutboxDeliveryAttempt,
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
)
from app.services.adapters import DispatchResult
from app.services.run_service import transition_run
from app.workers import outbox_worker
from app.workers.outbox_worker import process_aggregate_events

TEST_COMPLETION_SECRET = "auris-test-completion-secret-32chars-minimum"
TEST_COMPLETION_KEY_ID = "auris-test-completion"


def _create_submitted_task_run(client, auth_headers, *, key: str) -> tuple[str, str]:
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": f"controls/{key}",
        },
        headers={**auth_headers, "Idempotency-Key": f"create-{key}"},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["run_id"]
    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        external_run_id = run.payload["dispatch"]["details"]["external_run_id"]
    return run_id, external_run_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "caller-run-id"),
        ("task_run_id", "caller-task-run-id"),
        ("job_name", "untrusted_job"),
        ("dagster_run_draft", {"job_name": "untrusted_job"}),
        ("run_config", {"ops": {"unsafe": {"config": {"enabled": True}}}}),
        ("repository_name", "untrusted_repository"),
        ("repository_location_name", "untrusted_location"),
        ("pipeline_name", "untrusted_pipeline"),
        ("deadline_at", "2099-01-01T00:00:00Z"),
        ("next_status_sync_at", "2099-01-01T00:00:00Z"),
        ("monitor_generation", 999),
    ],
)
def test_task_run_rejects_caller_control_plane_fields(
    client,
    auth_headers,
    field: str,
    value: object,
) -> None:
    with SessionLocal() as session:
        run_count_before = session.scalar(select(func.count()).select_from(RunRecord))
        outbox_count_before = session.scalar(select(func.count()).select_from(OutboxEvent))

    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            field: value,
        },
        headers={**auth_headers, "Idempotency-Key": f"reject-{field}"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == run_count_before
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_count_before
        assert session.get(RunRecord, "caller-run-id") is None
        assert session.get(RunRecord, "caller-task-run-id") is None


def test_task_run_uses_server_generated_id_and_control_columns(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "controls/server-id",
        },
        headers={**auth_headers, "Idempotency-Key": "server-generated-run-id"},
    )

    assert response.status_code == 202, response.text
    data = response.json()["data"]
    run_id = data["run_id"]
    assert run_id.startswith("task_run_")
    assert data["status_version"] == 1
    assert data["submitted_at"] is None
    assert data["started_at"] is None
    assert data["finished_at"] is None
    assert data["deadline_at"] is not None
    deadline_at = datetime.fromisoformat(data["deadline_at"])
    assert 60 <= (deadline_at - datetime.now(UTC)).total_seconds() <= 7 * 24 * 60 * 60
    assert data["next_status_sync_at"] is None
    assert data["monitor_generation"] == 0
    assert data["engine_status"] is None
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status_version == 1
        assert run.submitted_at is None
        assert run.started_at is None
        assert run.finished_at is None
        assert run.deadline_at is not None
        assert run.next_status_sync_at is None
        assert run.monitor_generation == 0
        assert run.engine_status is None
        indexes = {index.name: index for index in RunRecord.__table__.indexes}
        assert "ix_run_records_status_deadline" in indexes
        assert "ix_run_records_status_sync_due" in indexes
        assert "ix_run_records_engine_status" in indexes
        assert tuple(
            column.name for column in indexes["ix_run_records_monitor_deadline"].columns
        ) == ("run_type", "status", "deadline_at")
        assert tuple(
            column.name for column in indexes["ix_run_records_monitor_sync_due"].columns
        ) == ("run_type", "status", "next_status_sync_at")
        active_control_index = indexes["ix_run_records_monitor_control_active"]
        assert tuple(column.name for column in active_control_index.columns) == (
            "tenant_id",
            "project_id",
            "run_key",
            "run_type",
            "status",
        )
        assert active_control_index.dialect_options["mysql"]["length"] == {"run_key": 128}


def test_task_run_cancellation_is_scoped_idempotent_and_audited(client, auth_headers) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="cancel-submitted"
    )
    path = f"/api/v1/task-runs/{run_id}/cancellations"
    headers = {**auth_headers, "Idempotency-Key": "cancel-submitted"}

    requested = client.post(path, json={"reason": "operator requested"}, headers=headers)
    replay = client.post(path, json={"reason": "operator requested"}, headers=headers)

    assert requested.status_code == 202, requested.text
    assert replay.status_code == 202, replay.text
    assert replay.json() == requested.json()
    control_id = requested.json()["data"]["run_id"]
    assert requested.json()["data"]["source_run_id"] == run_id
    assert requested.json()["data"]["control_action"] == "cancel"
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == "cancelling"
        assert source.cancel_reason == "operator requested"
        assert source.cancel_requested_at is not None
        assert control.status == "pending"
        assert control.payload["source_status_version"] == source.status_version
        assert control.payload["monitor_generation"] == source.monitor_generation
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == control_id,
                OutboxEvent.event_type == "task_run.cancel_requested",
            )
        )
        assert event is not None
        assert event.payload["external_run_id"] == external_run_id

    assert process_aggregate_events([control_id]) == 1
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == "cancelled"
        assert source.engine_status == "CANCELED"
        assert source.finished_at is not None
        assert source.terminal_reason == "operator requested"
        assert control.status == "success"
        assert control.payload["dispatch"]["operation"] == "cancel_run"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.object_id == run_id,
                    AuditLog.action == "task_run.cancelled",
                )
            )
            == 1
        )
        terminal_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.event_type == "task_run.cancelled",
            )
        )
        assert terminal_event is not None


def test_task_run_cancellation_idempotency_key_conflict(client, auth_headers) -> None:
    run_id, _ = _create_submitted_task_run(client, auth_headers, key="cancel-conflict")
    path = f"/api/v1/task-runs/{run_id}/cancellations"
    headers = {**auth_headers, "Idempotency-Key": "cancel-conflict"}

    first = client.post(path, json={"reason": "reason one"}, headers=headers)
    conflict = client.post(path, json={"reason": "reason two"}, headers=headers)

    assert first.status_code == 202, first.text
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_task_run_cancellation_requires_execution_role(client, auth_headers) -> None:
    run_id, _ = _create_submitted_task_run(client, auth_headers, key="cancel-role")

    response = client.post(
        f"/api/v1/task-runs/{run_id}/cancellations",
        json={"reason": "not allowed"},
        headers={
            **auth_headers,
            "Authorization": "Bearer annotator-token",
            "Idempotency-Key": "cancel-role-denied",
        },
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "FORBIDDEN"
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        assert source.status == "submitted"


def test_dagster_success_status_sync_never_fabricates_business_success(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(client, auth_headers, key="sync-success")
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/status-syncs",
        json={},
        headers={**auth_headers, "Idempotency-Key": "sync-success"},
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]

    def successful_status_sync(
        event_type: str,
        aggregate_type: str,
        payload: dict,
    ) -> DispatchResult:
        assert event_type == "task_run.status_sync_requested"
        assert aggregate_type == "task_run_status_sync"
        assert payload["external_run_id"] == external_run_id
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "SUCCESS",
                "can_terminate": False,
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", successful_status_sync)
    assert process_aggregate_events([control_id]) == 1

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == "completion_pending"
        assert source.engine_status == "SUCCESS"
        assert source.finished_at is None
        assert source.terminal_reason is None
        assert control.status == "success"
        assert control.payload["observed_engine_status"] == "SUCCESS"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type == "task_run.succeeded",
                )
            )
            == 0
        )


def test_manual_status_sync_reuses_active_control_under_source_lock(
    client,
    auth_headers,
) -> None:
    run_id, _ = _create_submitted_task_run(client, auth_headers, key="sync-active-control-merge")
    path = f"/api/v1/task-runs/{run_id}/status-syncs"

    first = client.post(
        path,
        json={},
        headers={**auth_headers, "Idempotency-Key": "sync-active-control-first"},
    )
    second = client.post(
        path,
        json={},
        headers={**auth_headers, "Idempotency-Key": "sync-active-control-second"},
    )

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert second_data["run_id"] == first_data["run_id"]
    assert second_data["control_action"] == "status_sync"
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, first_data["run_id"])
        assert source is not None and control is not None
        assert control.payload["source_status_version"] == source.status_version
        assert control.payload["monitor_generation"] == source.monitor_generation
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunRecord)
                .where(
                    RunRecord.run_key == run_id,
                    RunRecord.run_type == "task_run_status_sync",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == control.run_id,
                    OutboxEvent.event_type == "task_run.status_sync_requested",
                )
            )
            == 1
        )


@pytest.mark.parametrize("changed_fence", ["source_status_version", "monitor_generation"])
def test_stale_status_sync_control_is_superseded_without_overwriting_engine_evidence(
    client,
    auth_headers,
    monkeypatch,
    changed_fence: str,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key=f"sync-stale-{changed_fence}"
    )
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/status-syncs",
        json={},
        headers={
            **auth_headers,
            "Idempotency-Key": f"sync-stale-{changed_fence}",
        },
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]
    evidence_at = datetime(2026, 7, 19, 1, 2, 3, tzinfo=UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        expected_version = control.payload["source_status_version"]
        expected_generation = control.payload["monitor_generation"]
        source.engine_status = "CALLBACK_STARTED"
        source.engine_status_observed_at = evidence_at
        source.payload = {
            **source.payload,
            "engine_status": "CALLBACK_STARTED",
            "engine_status_observed_at": evidence_at.isoformat(),
        }
        if changed_fence == "source_status_version":
            source.status_version = int(source.status_version or 1) + 1
        else:
            source.monitor_generation = int(source.monitor_generation or 0) + 1
        source_status = source.status
        next_status_sync_at = source.next_status_sync_at
        session.commit()

    adapter_calls: list[str] = []

    def forbidden_status_query(event_type: str, *_args, **_kwargs) -> DispatchResult:
        adapter_calls.append(event_type)
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "mode": "real",
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "FAILURE",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", forbidden_status_query)

    assert process_aggregate_events([control_id]) == 1
    assert adapter_calls == []
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == source_status
        assert source.engine_status == "CALLBACK_STARTED"
        assert source.engine_status_observed_at is not None
        assert source.engine_status_observed_at.replace(tzinfo=UTC) == evidence_at
        assert source.payload["engine_status"] == "CALLBACK_STARTED"
        assert source.next_status_sync_at == next_status_sync_at
        assert control.status == "success"
        assert control.payload["control_outcome"] == "superseded"
        assert control.payload["no_op"] is True
        assert control.payload["expected_source_status_version"] == expected_version
        assert control.payload["expected_monitor_generation"] == expected_generation
        assert "observed_engine_status" not in control.payload
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == control_id,
                OutboxEvent.event_type == "task_run.status_sync_requested",
            )
        )
        assert event is not None
        attempt = session.scalar(
            select(OutboxDeliveryAttempt).where(OutboxDeliveryAttempt.event_id == event.event_id)
        )
        assert event.status == "processed"
        assert event.delivery_state == "confirmed"
        assert event.claim_token is None
        assert attempt is not None
        assert attempt.status == "superseded"
        assert attempt.adapter is None
        assert attempt.completed_at is not None
        assert attempt.details["remote_call_performed"] is False


def test_stale_cancellation_is_superseded_before_dagster_cancel(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="cancel-stale-preflight"
    )
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/cancellations",
        json={"reason": "operator requested"},
        headers={**auth_headers, "Idempotency-Key": "cancel-stale-preflight"},
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]
    evidence_at = datetime(2026, 7, 19, 3, 4, 5, tzinfo=UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        transition_run(source, "completion_pending", reason="trusted_callback_won_race")
        source.engine_status = "CALLBACK_SUCCESS"
        source.engine_status_observed_at = evidence_at
        source.payload = {
            **source.payload,
            "engine_status": "CALLBACK_SUCCESS",
            "engine_status_observed_at": evidence_at.isoformat(),
        }
        session.commit()

    adapter_calls: list[str] = []

    def forbidden_cancel(event_type: str, *_args, **_kwargs) -> DispatchResult:
        adapter_calls.append(event_type)
        return DispatchResult(
            adapter="dagster",
            operation="cancel_run",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "CANCELED",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", forbidden_cancel)

    assert process_aggregate_events([control_id]) == 1
    assert adapter_calls == []
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == "completion_pending"
        assert source.engine_status == "CALLBACK_SUCCESS"
        assert source.engine_status_observed_at is not None
        assert source.engine_status_observed_at.replace(tzinfo=UTC) == evidence_at
        assert control.status == "success"
        assert control.payload["control_outcome"] == "superseded"
        assert control.payload["no_op"] is True
        assert control.payload["superseded_phase"] == "prepare"


def test_source_change_after_remote_start_is_still_fenced_during_finalize(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="sync-finalize-second-fence"
    )
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/status-syncs",
        json={},
        headers={**auth_headers, "Idempotency-Key": "sync-finalize-second-fence"},
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]
    claims = outbox_worker._claim_batch(
        limit=1,
        worker_id="finalize-second-fence-worker",
        aggregate_ids=[control_id],
    )
    assert len(claims) == 1
    claim = claims[0]
    prepared = outbox_worker._prepare_claim(claim, lease_seconds=60)
    assert prepared is not None and prepared.blocked is False
    assert outbox_worker._mark_remote_operation_started(claim) is True

    evidence_at = datetime(2026, 7, 19, 4, 5, 6, tzinfo=UTC)
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.status_version = int(source.status_version or 1) + 1
        source.engine_status = "CALLBACK_STARTED"
        source.engine_status_observed_at = evidence_at
        source.payload = {
            **source.payload,
            "engine_status": "CALLBACK_STARTED",
            "engine_status_observed_at": evidence_at.isoformat(),
        }
        session.commit()

    adapter_calls: list[str] = []

    def completed_status_query(event_type: str, *_args, **_kwargs) -> DispatchResult:
        adapter_calls.append(event_type)
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "FAILURE",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", completed_status_query)
    dispatch = outbox_worker._dispatch_prepared(prepared)
    assert outbox_worker._finalize_dispatch(claim, prepared, dispatch) is True
    assert adapter_calls == ["task_run.status_sync_requested"]

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        assert source is not None and control is not None
        assert source.status == "submitted"
        assert source.engine_status == "CALLBACK_STARTED"
        assert source.engine_status_observed_at is not None
        assert source.engine_status_observed_at.replace(tzinfo=UTC) == evidence_at
        assert control.status == "success"
        assert control.payload["control_outcome"] == "superseded"
        assert control.payload["superseded_phase"] == "finalize"


def test_source_change_after_prepare_is_superseded_before_remote_start(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id, _ = _create_submitted_task_run(client, auth_headers, key="sync-remote-start-fence")
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/status-syncs",
        json={},
        headers={**auth_headers, "Idempotency-Key": "sync-remote-start-fence"},
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]
    claims = outbox_worker._claim_batch(
        limit=1,
        worker_id="remote-start-fence-worker",
        aggregate_ids=[control_id],
    )
    assert len(claims) == 1
    claim = claims[0]
    prepared = outbox_worker._prepare_claim(claim, lease_seconds=60)
    assert prepared is not None and prepared.blocked is False
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        assert source is not None
        source.monitor_generation = int(source.monitor_generation or 0) + 1
        session.commit()

    adapter_calls: list[str] = []
    monkeypatch.setattr(
        outbox_worker,
        "dispatch_event",
        lambda event_type, *_args, **_kwargs: adapter_calls.append(event_type),
    )

    assert outbox_worker._mark_remote_operation_started(claim) is False
    assert adapter_calls == []
    with SessionLocal() as session:
        event = session.get(OutboxEvent, claim.event_id)
        control = session.get(RunRecord, control_id)
        assert event is not None and control is not None
        attempt = session.scalar(
            select(OutboxDeliveryAttempt).where(OutboxDeliveryAttempt.event_id == event.event_id)
        )
        assert event.status == "processed"
        assert event.delivery_state == "confirmed"
        assert control.status == "success"
        assert control.payload["superseded_phase"] == "remote_start"
        assert attempt is not None
        assert attempt.status == "superseded"
        assert attempt.details["remote_call_performed"] is False


def test_signed_completion_can_finalize_a_run_observed_as_started(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="sync-started-before-completion"
    )
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/status-syncs",
        json={},
        headers={
            **auth_headers,
            "Idempotency-Key": "sync-started-before-completion",
        },
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]

    monkeypatch.setattr(
        outbox_worker,
        "dispatch_event",
        lambda _event_type, _aggregate_type, _payload: DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "mode": "real",
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "STARTED",
                "can_terminate": True,
            },
        ),
    )
    assert process_aggregate_events([control_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "running"

    completed, _ = _post_signed_task_completion(
        client,
        run_id=run_id,
        external_run_id=external_run_id,
        key="sync-started-before-completion",
    )

    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["status"] == "success"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "success"
        assert run.engine_status == "STARTED"


def _signed_completion_headers(
    *,
    path: str,
    payload: dict,
    idempotency_key: str,
    nonce: str,
) -> dict[str, str]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = datetime.now(UTC).isoformat()
    body_sha256 = hashlib.sha256(body).hexdigest()
    message = completion_signature_message(
        method="POST",
        path=path,
        query="",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        idempotency_key=idempotency_key,
        timestamp=timestamp,
        nonce=nonce,
        key_id=TEST_COMPLETION_KEY_ID,
        source="dagster",
        body_sha256=body_sha256,
    )
    signature = hmac.new(
        TEST_COMPLETION_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": f"request-{nonce}",
        "X-Trace-Id": f"trace-{nonce}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Auris-Key-Id": TEST_COMPLETION_KEY_ID,
        "X-Auris-Timestamp": timestamp,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": "dagster",
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def _post_signed_task_completion(
    client,
    *,
    run_id: str,
    external_run_id: str,
    key: str,
):
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": f"dagster:{external_run_id}:{key}",
        "external_id": external_run_id,
        "result_ref": {
            "object_type": "task_result",
            "object_id": f"result-{key}",
        },
        "metrics": {"processed": 1},
    }
    headers = _signed_completion_headers(
        path=path,
        payload=payload,
        idempotency_key=f"dagster-completion:{external_run_id}:{key}",
        nonce=f"completion-{key}",
    )
    response = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        headers=headers,
    )
    return response, payload


@pytest.mark.parametrize(
    ("observed_status", "expected_run_status", "expected_receipt_state", "terminal_event_type"),
    [
        ("SUCCESS", "success", "completed", "task_run.succeeded"),
        ("CANCELED", "cancelled", "rejected", "task_run.cancelled"),
    ],
)
def test_signed_success_receipt_racing_with_cancellation_is_durably_resolved(
    client,
    auth_headers,
    monkeypatch,
    observed_status: str,
    expected_run_status: str,
    expected_receipt_state: str,
    terminal_event_type: str,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client,
        auth_headers,
        key=f"cancel-completion-race-{observed_status.lower()}",
    )
    requested = client.post(
        f"/api/v1/task-runs/{run_id}/cancellations",
        json={"reason": "operator raced with Dagster completion"},
        headers={
            **auth_headers,
            "Idempotency-Key": f"cancel-completion-race-{observed_status.lower()}",
        },
    )
    assert requested.status_code == 202, requested.text
    control_id = requested.json()["data"]["run_id"]

    staged, completion_payload = _post_signed_task_completion(
        client,
        run_id=run_id,
        external_run_id=external_run_id,
        key=f"cancel-race-{observed_status.lower()}",
    )
    assert staged.status_code == 202, staged.text
    assert staged.json()["data"]["receipt_state"] == "pending_cancellation_resolution"
    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        assert source is not None and receipt is not None
        assert source.status == "cancelling"
        assert receipt.processing_state == "pending_cancel"
        assert receipt.external_id == external_run_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type.in_(
                        ["task_run.succeeded", "task_run.failed", "task_run.cancelled"]
                    ),
                )
            )
            == 0
        )

    def resolve_cancel_race(
        event_type: str,
        aggregate_type: str,
        payload: dict,
    ) -> DispatchResult:
        assert event_type == "task_run.cancel_requested"
        assert aggregate_type == "task_run_cancellation"
        assert payload["external_run_id"] == external_run_id
        return DispatchResult(
            adapter="dagster",
            operation="cancel_run",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": observed_status,
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", resolve_cancel_race)
    assert process_aggregate_events([control_id]) == 1

    with SessionLocal() as session:
        source = session.get(RunRecord, run_id)
        control = session.get(RunRecord, control_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        terminal_events = list(
            session.scalars(
                select(OutboxEvent).where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type.in_(
                        ["task_run.succeeded", "task_run.failed", "task_run.cancelled"]
                    ),
                )
            )
        )
        assert source is not None and control is not None and receipt is not None
        assert source.status == expected_run_status
        assert source.engine_status == observed_status
        assert control.status == "success"
        assert control.payload["source_status"] == expected_run_status
        assert receipt.processing_state == expected_receipt_state
        assert receipt.external_id == completion_payload["external_id"]
        assert [event.event_type for event in terminal_events] == [terminal_event_type]
        if observed_status == "SUCCESS":
            assert receipt.completion_status == "success"
            assert (
                source.payload["completion_receipt"]["completion_receipt_id"]
                == receipt.completion_receipt_id
            )
        else:
            assert receipt.completion_status == "rejected"
            assert receipt.response_json["error"]["code"] == "RUN_COMPLETION_CANCELLED_BEFORE_APPLY"


def test_signed_dagster_completion_is_durable_when_it_arrives_before_launch_finalize(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "controls/early-receipt",
        },
        headers={**auth_headers, "Idempotency-Key": "create-early-receipt"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    external_run_id = "dagster-early-receipt-run"
    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    payload = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": f"dagster:{external_run_id}",
        "external_id": external_run_id,
        "result_ref": {"object_type": "task_result", "object_id": "early-result"},
        "metrics": {"processed": 1},
    }
    headers = _signed_completion_headers(
        path=path,
        payload=payload,
        idempotency_key=f"dagster-completion:{external_run_id}",
        nonce="early-receipt-nonce",
    )

    staged = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        headers=headers,
    )

    assert staged.status_code == 202, staged.text
    assert staged.json()["data"]["receipt_state"] == "pending_binding"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        assert run is not None and receipt is not None
        assert run.status == "pending"
        assert receipt.processing_state == "pending_binding"
        assert receipt.external_id == external_run_id

    def launch_with_matching_external_id(
        event_type: str,
        aggregate_type: str,
        payload: dict,
    ) -> DispatchResult:
        assert event_type == "task_run.requested"
        assert aggregate_type == "task_run"
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "job_name": "auris_flow_generic_job",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", launch_with_matching_external_id)
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        assert run is not None and receipt is not None
        assert run.status == "success"
        assert (
            run.payload["completion_receipt"]["completion_receipt_id"]
            == payload["completion_receipt_id"]
        )
        assert receipt.processing_state == "completed"
        assert receipt.completion_status == "success"
        assert receipt.completed_at is not None


def test_early_completion_external_id_mismatch_is_rejected_after_launch_binding(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "controls/early-receipt-mismatch",
        },
        headers={**auth_headers, "Idempotency-Key": "create-early-receipt-mismatch"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]
    staged_external_id = "dagster-staged-untrusted-id"
    staged, _ = _post_signed_task_completion(
        client,
        run_id=run_id,
        external_run_id=staged_external_id,
        key="early-mismatch",
    )
    assert staged.status_code == 202, staged.text

    trusted_external_id = "dagster-launch-trusted-id"

    def launch_with_different_external_id(
        event_type: str,
        aggregate_type: str,
        payload: dict,
    ) -> DispatchResult:
        assert event_type == "task_run.requested"
        assert aggregate_type == "task_run"
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "external_run_id": trusted_external_id,
                "dagster_run_id": trusted_external_id,
                "job_name": "auris_flow_generic_job",
            },
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", launch_with_different_external_id)
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        assert run is not None and receipt is not None
        assert run.status == "submitted"
        assert run.payload["dispatch"]["details"]["external_run_id"] == trusted_external_id
        assert "completion_receipt" not in run.payload
        assert receipt.processing_state == "rejected"
        assert receipt.completion_status == "rejected"
        assert receipt.status_code == 409
        assert receipt.response_json["error"]["code"] == "RUN_COMPLETION_EXTERNAL_ID_MISMATCH"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.object_id == run_id,
                    AuditLog.action == "task_run.completion_rejected",
                    AuditLog.result == "failed",
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
                    OutboxEvent.event_type.in_(
                        ["task_run.succeeded", "task_run.failed", "task_run.cancelled"]
                    ),
                )
            )
            == 0
        )


def test_completed_task_run_rejects_cancellation_with_one_terminal_event(
    client,
    auth_headers,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="complete-before-cancel"
    )
    completed, _ = _post_signed_task_completion(
        client,
        run_id=run_id,
        external_run_id=external_run_id,
        key="complete-before-cancel",
    )
    assert completed.status_code == 200, completed.text

    rejected = client.post(
        f"/api/v1/task-runs/{run_id}/cancellations",
        json={"reason": "too late"},
        headers={**auth_headers, "Idempotency-Key": "cancel-after-complete"},
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "RUN_ALREADY_TERMINAL"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "success"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type.in_(
                        ["task_run.succeeded", "task_run.failed", "task_run.cancelled"]
                    ),
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.object_id == run_id,
                    AuditLog.action == "task_run.cancellation_rejected",
                    AuditLog.result == "failed",
                )
            )
            == 1
        )


def test_cancelled_task_run_rejects_completion_with_one_terminal_event_and_audit(
    client,
    auth_headers,
) -> None:
    run_id, external_run_id = _create_submitted_task_run(
        client, auth_headers, key="cancel-before-complete"
    )
    cancellation = client.post(
        f"/api/v1/task-runs/{run_id}/cancellations",
        json={"reason": "operator cancelled first"},
        headers={**auth_headers, "Idempotency-Key": "cancel-before-complete"},
    )
    assert cancellation.status_code == 202, cancellation.text
    control_id = cancellation.json()["data"]["run_id"]
    assert process_aggregate_events([control_id]) == 1

    rejected, _ = _post_signed_task_completion(
        client,
        run_id=run_id,
        external_run_id=external_run_id,
        key="cancel-before-complete",
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "RUN_COMPLETION_NOT_ALLOWED"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "cancelled"
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == run_id,
                    OutboxEvent.event_type.in_(
                        ["task_run.succeeded", "task_run.failed", "task_run.cancelled"]
                    ),
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.object_id == run_id,
                    AuditLog.action == "task_run.completion_rejected",
                    AuditLog.result == "failed",
                )
            )
            == 1
        )
