from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    AssetLineageEdge,
    AssetMaterialization,
    AudioRecording,
    AuditLog,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
    StorageObject,
)
from app.services.audio_import_completion_service import (
    finalize_audio_import_batch_from_task_terminal,
)
from app.services.run_service import retry_payload_from_record
from app.workers.outbox_worker import process_aggregate_events

AUDIO_IMPORT_RESULT_SCHEMA = "auris-flow-audio-import-result-v1"
AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
AUDIO_IMPORT_ENVELOPE_SHA256 = "e" * 64


def _completion_result(run_id: str) -> dict:
    prefix = f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/audio-import/"
    return {
        "schema_version": AUDIO_IMPORT_RESULT_SCHEMA,
        "execution_contract": AUDIO_IMPORT_EXECUTION_CONTRACT,
        "execution_envelope_sha256": AUDIO_IMPORT_ENVELOPE_SHA256,
        "import_batch_id": "import_batch_completion_materialization",
        "batch_status": "partial",
        "manifest_storage_object_id": "sto_import_manifest_materialization",
        "manifest_sha256": "1" * 64,
        "next_cursor_candidate": "cursor-after-2",
        "items": [
            {
                "external_record_id": "platform-call-001",
                "status": "succeeded",
                "storage_object_id": "sto_import_raw_audio_001",
                "content_sha256": "2" * 64,
                "object_version": "object-version-001",
                "source": {
                    "started_at": "2026-07-27T10:00:00+08:00",
                    "duration_ms": 42_000,
                    "store_ref": "store-01",
                    "agent_ref": "agent-001",
                    "device_ref": "device-001",
                },
            },
            {
                "external_record_id": "platform-call-002",
                "status": "failed",
                "error_code": "AUDIO_DOWNLOAD_FAILED",
            },
        ],
        "storage_objects": [
            {
                "storage_object_id": "sto_import_manifest_materialization",
                "role": "manifest",
                "provider": "minio",
                "bucket": "auris-flow-local",
                "object_key": f"{prefix}manifest.json",
                "content_type": "application/json",
                "size_bytes": 2_048,
                "content_sha256": "1" * 64,
                "etag": "manifest-etag",
                "version_id": "manifest-version-001",
            },
            {
                "storage_object_id": "sto_import_raw_audio_001",
                "role": "raw_audio",
                "provider": "minio",
                "bucket": "auris-flow-local",
                "object_key": f"{prefix}recordings/platform-call-001.wav",
                "content_type": "audio/wav",
                "size_bytes": 84_044,
                "content_sha256": "2" * 64,
                "etag": "audio-etag",
                "version_id": "object-version-001",
            },
        ],
    }


def _seed_import_run(
    *,
    run_id: str,
    batch_id: str,
    root_trace_id: str,
    external_run_id: str,
    cursor_before: str | None,
) -> None:
    with SessionLocal.begin() as session:
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "connectors",
                JsonResource.resource_key == "connector_audio_import_completion",
            )
        )
        if connector is None:
            session.add(
                JsonResource(
                    collection="connectors",
                    resource_key="connector_audio_import_completion",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id=root_trace_id,
                    data={
                        "connector_id": "connector_audio_import_completion",
                        "connector_version": 1,
                        "status": "active",
                        **(
                            {
                                "sync_cursor": cursor_before,
                                "sync_cursor_connector_version": 1,
                            }
                            if cursor_before is not None
                            else {}
                        ),
                    },
                )
            )
        run = RunRecord(
            run_id=run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            run_type="task_run",
            status="submitted",
            run_key=f"audio-import-{run_id}",
            partition_key=None,
            trace_id=f"trace_{run_id}",
            payload={
                "run_id": run_id,
                "status": "submitted",
                "trace_id": f"trace_{run_id}",
                "root_trace_id": root_trace_id,
                "execution_contract": "auris-flow-audio-import-v1",
                "import_batch_id": batch_id,
                "task_version_id": "task_version_audio_import_completion",
                "connector_snapshot": {
                    "connector_id": "connector_audio_import_completion",
                    "connector_version": "1",
                    "platform_connection_id": "platform_connection_completion",
                    "platform_scope": {
                        "tenant_ref": "external-tenant-001",
                        "store_refs": ["store-01"],
                    },
                },
                "target": {
                    "storage_provider": "minio",
                    "bucket": "auris-flow-local",
                    "object_prefix": (
                        f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/audio-import/"
                    ),
                    "target_asset_key": "auris/audio/raw_recordings",
                    "dedupe_policy": "external_id_checksum",
                },
                "dispatch": {
                    "adapter": "dagster",
                    "operation": "run_request",
                    "status": "success",
                    "details": {
                        "external_run_id": external_run_id,
                        "execution_envelope_sha256": AUDIO_IMPORT_ENVELOPE_SHA256,
                        "dispatch_idempotency_key": f"dispatch-{run_id}",
                        "fencing_token": "1:1",
                    },
                },
                "business_completion_required": True,
            },
        )
        session.add(run)
        session.flush()
        session.add(
            ImportBatch(
                import_batch_id=batch_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                task_run_id=run_id,
                task_version_id="task_version_audio_import_completion",
                connector_id="connector_audio_import_completion",
                status="running",
                current_stage="listing",
                total_items=0,
                succeeded_items=0,
                skipped_items=0,
                failed_items=0,
                cursor_before=cursor_before,
                cursor_after=None,
                root_trace_id=root_trace_id,
                trace_id=run.trace_id,
                payload={},
            )
        )


def _single_success_result(
    run_id: str,
    *,
    batch_id: str,
    suffix: str,
) -> dict:
    result = _completion_result(run_id)
    manifest_id = f"sto_import_manifest_{suffix}"
    audio_id = f"sto_import_raw_audio_{suffix}"
    object_version = f"object-version-{suffix}"
    result["import_batch_id"] = batch_id
    result["batch_status"] = "succeeded"
    result["manifest_storage_object_id"] = manifest_id
    result["items"] = [
        {
            **result["items"][0],
            "storage_object_id": audio_id,
            "object_version": object_version,
        }
    ]
    result["storage_objects"] = [
        {
            **result["storage_objects"][0],
            "storage_object_id": manifest_id,
            "object_key": (
                f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/audio-import/manifest.json"
            ),
            "version_id": f"manifest-version-{suffix}",
        },
        {
            **result["storage_objects"][1],
            "storage_object_id": audio_id,
            "object_key": (
                "tenants/aurora_auto/projects/sales_qa/"
                f"runs/{run_id}/audio-import/recordings/platform-call-001.wav"
            ),
            "version_id": object_version,
        },
    ]
    return result


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
def test_audio_import_task_terminal_sync_is_idempotent(terminal_status: str) -> None:
    run_id = f"task_run_import_terminal_{terminal_status}"
    batch_id = f"import_batch_terminal_{terminal_status}"
    root_trace_id = f"root_import_terminal_{terminal_status}"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id=root_trace_id,
        external_run_id=f"dagster_import_terminal_{terminal_status}",
        cursor_before="cursor-before-terminal",
    )

    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="system:test-audio-import-terminal",
        roles=("system",),
        request_id=f"request-{terminal_status}",
        trace_id=root_trace_id,
        idempotency_key=f"terminal-{terminal_status}",
        correlation_id=root_trace_id,
        actor_kind="service",
    )
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        batch = session.get(ImportBatch, batch_id)
        assert run is not None and batch is not None
        run.status = terminal_status
        run.terminal_reason = f"terminal-{terminal_status}-reason"
        batch.cursor_after = "must-not-survive-terminal"

        assert (
            finalize_audio_import_batch_from_task_terminal(
                session,
                ctx,
                run,
                reason=run.terminal_reason,
            )
            is True
        )
        assert (
            finalize_audio_import_batch_from_task_terminal(
                session,
                ctx,
                run,
                reason=run.terminal_reason,
            )
            is False
        )

    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == terminal_status
        assert batch.current_stage == "completed"
        assert batch.cursor_after is None
        assert batch.started_at is not None
        assert batch.finished_at is not None
        assert batch.payload["task_run_terminal_status"] == terminal_status
        assert batch.payload["task_run_terminal_reason"] == f"terminal-{terminal_status}-reason"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.object_id == batch_id,
                    AuditLog.action == f"audio_import.batch_{terminal_status}",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_id == batch_id,
                    OutboxEvent.event_type == f"audio_import.batch_{terminal_status}",
                )
            )
            == 1
        )


def test_audio_import_task_terminal_sync_does_not_overwrite_business_terminal_batch() -> None:
    run_id = "task_run_import_terminal_preserved"
    batch_id = "import_batch_terminal_preserved"
    root_trace_id = "root_import_terminal_preserved"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id=root_trace_id,
        external_run_id="dagster_import_terminal_preserved",
        cursor_before="cursor-before-preserved",
    )
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="system:test-audio-import-terminal",
        roles=("system",),
        request_id="request-preserved",
        trace_id=root_trace_id,
        idempotency_key="terminal-preserved",
        correlation_id=root_trace_id,
        actor_kind="service",
    )
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        batch = session.get(ImportBatch, batch_id)
        assert run is not None and batch is not None
        run.status = "failed"
        run.terminal_reason = "late-operational-failure"
        batch.status = "succeeded"
        batch.current_stage = "completed"
        batch.cursor_after = "cursor-after-success"

        assert (
            finalize_audio_import_batch_from_task_terminal(
                session,
                ctx,
                run,
                reason=run.terminal_reason,
            )
            is False
        )

    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "succeeded"
        assert batch.cursor_after == "cursor-after-success"


def test_audio_import_completion_materializes_partial_batch_and_playable_session(
    client,
    auth_headers,
) -> None:
    run_id = "task_run_import_completion_materialization"
    root_trace_id = "root_import_completion_materialization"
    with SessionLocal.begin() as session:
        run = RunRecord(
            run_id=run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            run_type="task_run",
            status="submitted",
            run_key="audio-import-completion",
            partition_key=None,
            trace_id="trace_import_completion_materialization",
            payload={
                "run_id": run_id,
                "status": "submitted",
                "trace_id": "trace_import_completion_materialization",
                "root_trace_id": root_trace_id,
                "execution_contract": "auris-flow-audio-import-v1",
                "import_batch_id": "import_batch_completion_materialization",
                "task_version_id": "task_version_audio_import_completion",
                "connector_snapshot": {
                    "connector_id": "connector_audio_import_completion",
                    "connector_version": "1",
                    "platform_connection_id": "platform_connection_completion",
                    "platform_scope": {
                        "tenant_ref": "external-tenant-001",
                        "store_refs": ["store-01"],
                    },
                },
                "target": {
                    "storage_provider": "minio",
                    "bucket": "auris-flow-local",
                    "object_prefix": (
                        f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/audio-import/"
                    ),
                    "target_asset_key": "auris/audio/raw_recordings",
                    "dedupe_policy": "external_id_checksum",
                },
                "dispatch": {
                    "adapter": "dagster",
                    "operation": "run_request",
                    "status": "success",
                    "details": {
                        "external_run_id": "dagster-import-materialization-001",
                        "execution_envelope_sha256": AUDIO_IMPORT_ENVELOPE_SHA256,
                        "dispatch_idempotency_key": "dispatch-import-materialization",
                        "fencing_token": "1:1",
                    },
                },
                "business_completion_required": True,
            },
        )
        session.add(run)
        session.flush()
        session.add(
            ImportBatch(
                import_batch_id="import_batch_completion_materialization",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                task_run_id=run_id,
                task_version_id="task_version_audio_import_completion",
                connector_id="connector_audio_import_completion",
                status="running",
                total_items=0,
                succeeded_items=0,
                skipped_items=0,
                failed_items=0,
                cursor_before="cursor-before-0",
                cursor_after=None,
                root_trace_id=root_trace_id,
                trace_id=run.trace_id,
                payload={},
            )
        )

    path = f"/api/v1/task-runs/{run_id}/completion-receipts"
    body = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "dagster_audio_import_materialization_001",
        "external_id": "dagster-import-materialization-001",
        "result_ref": _completion_result(run_id),
        "metrics": {
            "total": 2,
            "succeeded": 1,
            "skipped": 0,
            "failed": 1,
        },
    }
    headers = {
        **auth_headers,
        "Idempotency-Key": "complete-audio-import-materialization",
    }
    completed = client.post(path, json=body, headers=headers)
    replayed = client.post(path, json=body, headers=headers)
    replayed_with_new_key = client.post(
        path,
        json=body,
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-audio-import-materialization-replay",
        },
    )

    assert (
        completed.status_code == replayed.status_code == replayed_with_new_key.status_code == 202
    ), completed.text
    assert replayed.json()["data"]["status"] == "completion_pending"
    assert replayed.json()["data"]["receipt_state"] == "materializing"
    assert replayed_with_new_key.json()["data"]["receipt_state"] == "materializing"
    assert completed.json()["data"]["status"] == "completion_pending"
    assert completed.json()["data"]["business_status"] == "materializing"
    assert completed.json()["data"]["receipt_state"] == "materializing"
    assert completed.json()["data"]["import_batch_id"] == (
        "import_batch_completion_materialization"
    )
    assert (
        next(
            action
            for action in completed.json()["data"]["next_actions"]
            if action["key"] == "view_trace"
        )["route"]
        == f"traces/{root_trace_id}"
    )

    pending_run = client.get(f"/api/v1/task-runs/{run_id}", headers=auth_headers)
    assert pending_run.status_code == 200
    assert pending_run.json()["data"]["status"] == "completion_pending"
    assert pending_run.json()["data"]["business_status"] == "materializing"
    pending_batch = client.get(
        "/api/v1/import-batches/import_batch_completion_materialization",
        headers=auth_headers,
    )
    assert pending_batch.status_code == 200
    assert pending_batch.json()["data"]["status"] == "running"
    assert pending_batch.json()["data"]["current_stage"] == "materializing"
    pending_items = client.get(
        "/api/v1/import-batches/import_batch_completion_materialization/items",
        headers=auth_headers,
    )
    assert pending_items.status_code == 200
    assert pending_items.json()["data"]["items"] == []

    with SessionLocal() as session:
        materialization_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        assert materialization_event is not None
        assert materialization_event.status == "pending"
        assert (
            session.scalar(
                select(func.count())
                .select_from(StorageObject)
                .where(
                    StorageObject.source_type == "task_run",
                    StorageObject.source_id == run_id,
                )
            )
            == 0
        )

    assert process_aggregate_events([run_id]) == 1
    finalized_run = client.get(f"/api/v1/task-runs/{run_id}", headers=auth_headers)
    assert finalized_run.status_code == 200
    assert finalized_run.json()["data"]["status"] == "success"
    assert finalized_run.json()["data"]["import_batch"]["status"] == "partial"
    imported_audio_session_id = finalized_run.json()["data"]["import_batch"]["audio_session_ids"][0]
    # The import transaction ends at a playable session. Intelligence
    # scheduling consumes the durable materialization event in a separate
    # transaction, so a downstream failure can never roll back the import.
    assert process_aggregate_events([imported_audio_session_id]) == 1

    with SessionLocal() as session:
        batch = session.get(ImportBatch, "import_batch_completion_materialization")
        assert batch is not None
        assert batch.status == "partial"
        assert (
            batch.total_items,
            batch.succeeded_items,
            batch.skipped_items,
            batch.failed_items,
        ) == (2, 1, 0, 1)
        # A partial batch may not leap over the failed source record.
        assert batch.cursor_before == "cursor-before-0"
        assert batch.cursor_after is None

        items = list(
            session.scalars(
                select(ImportBatchItem).where(
                    ImportBatchItem.import_batch_id == batch.import_batch_id
                )
            )
        )
        assert len(items) == 2
        success = next(item for item in items if item.status == "succeeded")
        failed = next(item for item in items if item.status == "failed")
        assert success.object_version == "object-version-001"
        assert success.audio_session_id
        assert imported_audio_session_id == str(success.audio_session_id)
        assert failed.error_code == "AUDIO_DOWNLOAD_FAILED"
        assert failed.audio_session_id is None

        storage = session.get(StorageObject, "sto_import_raw_audio_001")
        assert storage is not None
        assert storage.status == "verified"
        assert storage.payload["object_version_id"] == "object-version-001"

        recordings = list(
            session.scalars(
                select(AudioRecording).where(
                    AudioRecording.tenant_id == "aurora_auto",
                    AudioRecording.project_id == "sales_qa",
                    AudioRecording.trace_id == root_trace_id,
                )
            )
        )
        assert len(recordings) == 1
        recording = recordings[0]
        assert recording.payload["storage_object_id"] == storage.storage_object_id
        assert recording.payload["external_record_id"] == "platform-call-001"
        assert recording.payload["platform_connection_id"] == "platform_connection_completion"

        audio_session = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "audio_sessions",
                JsonResource.resource_key == success.audio_session_id,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert audio_session is not None
        assert audio_session.data["recording_id"] == recording.recording_id
        assert audio_session.data["import_batch_id"] == batch.import_batch_id
        assert audio_session.data["root_trace_id"] == root_trace_id
        assert audio_session.data["platform_connection_id"] == "platform_connection_completion"

        materialized_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "audio_session.materialized",
                OutboxEvent.aggregate_id == success.audio_session_id,
                OutboxEvent.tenant_id == "aurora_auto",
                OutboxEvent.project_id == "sales_qa",
            )
        )
        assert materialized_event is not None
        assert materialized_event.payload["trace_id"] == root_trace_id
        intelligence_run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_type == "audio_intelligence",
                RunRecord.tenant_id == "aurora_auto",
                RunRecord.project_id == "sales_qa",
                RunRecord.payload["audio_session_id"].as_string() == success.audio_session_id,
            )
        )
        assert intelligence_run is not None
        assert intelligence_run.status == "pending"
        assert intelligence_run.payload["root_trace_id"] == root_trace_id
        assert intelligence_run.payload["trigger"] == "audio_session.materialized"
        assert intelligence_run.payload["scene_profile_version_id"] == (
            "scenev_auto_sales_quality_v1"
        )
        assert intelligence_run.payload["input_object"] == {
            "storage_object_id": storage.storage_object_id,
            "storage_provider": storage.provider,
            "bucket": storage.bucket,
            "object_key": storage.object_key,
            "version_id": "object-version-001",
            "content_sha256": storage.content_sha256,
            "content_length": storage.size_bytes,
            "content_type": storage.content_type,
        }
        intelligence_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "audio_intelligence.requested",
                OutboxEvent.aggregate_id == intelligence_run.run_id,
            )
        )
        assert intelligence_event is not None
        assert intelligence_event.payload["trace_id"] == root_trace_id

        assert (
            session.scalar(
                select(func.count())
                .select_from(AssetMaterialization)
                .where(AssetMaterialization.trace_id == root_trace_id)
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AssetLineageEdge)
                .where(AssetLineageEdge.trace_id == root_trace_id)
            )
            == 1
        )

    batch_detail = client.get(
        "/api/v1/import-batches/import_batch_completion_materialization",
        headers=auth_headers,
    )
    assert batch_detail.status_code == 200
    assert batch_detail.json()["data"]["status"] == "partial"
    batch_items = client.get(
        "/api/v1/import-batches/import_batch_completion_materialization/items",
        headers=auth_headers,
    )
    assert batch_items.status_code == 200
    assert len(batch_items.json()["data"]["items"]) == 2
    for item in batch_items.json()["data"]["items"]:
        assert item["retry_lineage"] == {
            "source_import_batch_id": None,
            "source_import_item_id": None,
            "root_import_batch_id": "import_batch_completion_materialization",
            "root_import_item_id": item["import_item_id"],
            "attempt": 1,
        }
    failed_item = next(
        item for item in batch_items.json()["data"]["items"] if item["status"] == "failed"
    )
    assert failed_item["error_code"] == "AUDIO_DOWNLOAD_FAILED"
    assert failed_item["retryable"] is True
    assert failed_item["recovery_suggestion"]

    audio_session = client.get(
        f"/api/v1/audio-sessions/{imported_audio_session_id}",
        headers=auth_headers,
    )
    assert audio_session.status_code == 200, audio_session.text
    assert (
        audio_session.json()["data"]["recording"]["storage_object"]["storage_object_id"]
        == "sto_import_raw_audio_001"
    )
    playback_grant = client.post(
        f"/api/v1/audio-sessions/{imported_audio_session_id}/playback-grants",
        json={},
        headers={
            **auth_headers,
            "Idempotency-Key": "grant-imported-audio-playback",
        },
    )
    assert playback_grant.status_code == 201, playback_grant.text

    root_trace = client.get(f"/api/v1/traces/{root_trace_id}", headers=auth_headers)
    assert root_trace.status_code == 200, root_trace.text
    spans = root_trace.json()["data"]["spans"]
    assert any(
        span["kind"] == "run" and span["run_id"] == run_id and span["status"] == "success"
        for span in spans
    )
    assert any(
        span["kind"] == "audit"
        and span["object_id"] == "import_batch_completion_materialization"
        and span["action"] == "audio_import.batch_materialized"
        for span in spans
    )
    assert any(
        span["kind"] == "resource"
        and span["collection"] == "audio_sessions"
        and span["object_id"] == imported_audio_session_id
        for span in spans
    )


def test_audio_import_materialization_failure_remains_recoverable_and_never_reports_success(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    from app.services import run_service

    run_id = "task_run_import_materialization_retry"
    batch_id = "import_batch_materialization_retry"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id="root_import_materialization_retry",
        external_run_id="dagster-import-materialization-retry",
        cursor_before="cursor-before-retry",
    )
    result = _single_success_result(
        run_id,
        batch_id=batch_id,
        suffix="materialization_retry",
    )
    accepted = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "receipt-import-materialization-retry",
            "external_id": "dagster-import-materialization-retry",
            "result_ref": result,
            "metrics": {"total": 1, "succeeded": 1, "skipped": 0, "failed": 0},
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-materialization-retry",
        },
    )
    assert accepted.status_code == 202

    def fail_materialization(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("simulated materializer failure")

    monkeypatch.setattr(
        run_service,
        "materialize_staged_execution_completion",
        fail_materialization,
    )
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        batch = session.get(ImportBatch, batch_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        assert run is not None and run.status == "completion_pending"
        assert run.payload["business_status"] == "materializing"
        assert batch is not None and batch.status == "running"
        assert batch.current_stage == "materializing"
        assert receipt is not None and receipt.processing_state == "materializing"
        assert event is not None and event.status == "pending"
        assert event.attempt_count == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(ImportBatchItem)
                .where(ImportBatchItem.import_batch_id == batch_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(StorageObject)
                .where(
                    StorageObject.source_type == "task_run",
                    StorageObject.source_id == run_id,
                )
            )
            == 0
        )


def test_audio_import_materialization_retry_exhaustion_atomically_fails_business_state(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    from app.services import run_service

    run_id = "task_run_import_materialization_dead_letter"
    batch_id = "import_batch_materialization_dead_letter"
    root_trace_id = "root_import_materialization_dead_letter"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id=root_trace_id,
        external_run_id="dagster-import-materialization-dead-letter",
        cursor_before="cursor-before-dead-letter",
    )
    result = _single_success_result(
        run_id,
        batch_id=batch_id,
        suffix="materialization_dead_letter",
    )
    accepted = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "receipt-import-materialization-dead-letter",
            "external_id": "dagster-import-materialization-dead-letter",
            "result_ref": result,
            "metrics": {"total": 1, "succeeded": 1, "skipped": 0, "failed": 0},
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-materialization-dead-letter",
        },
    )
    assert accepted.status_code == 202

    with SessionLocal.begin() as session:
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        assert event is not None
        event.payload = {**event.payload, "max_attempts": 2}

    def fail_materialization(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("permanent materializer failure")

    monkeypatch.setattr(
        run_service,
        "materialize_staged_execution_completion",
        fail_materialization,
    )
    assert process_aggregate_events([run_id]) == 1
    with SessionLocal.begin() as session:
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        assert event is not None and event.status == "pending"
        event.available_at = event.created_at
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        batch = session.get(ImportBatch, batch_id)
        receipt = session.scalar(
            select(RunCompletionReceipt).where(RunCompletionReceipt.run_id == run_id)
        )
        materialization_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        terminal_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "task_run.failed",
                OutboxEvent.aggregate_id == run_id,
            )
        )
        terminal_audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "task_run.failed",
                AuditLog.object_id == run_id,
            )
        )
        materialization_audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "task_run.materialization_failed",
                AuditLog.object_id == run_id,
            )
        )

        assert run is not None and run.status == "failed"
        assert run.terminal_reason == "EXECUTION_MATERIALIZATION_RETRIES_EXHAUSTED"
        assert run.payload["business_status"] == "failed"
        assert run.payload["materialization"]["status"] == "failed"
        assert run.payload["dead_letter_event_id"] == materialization_event.event_id
        assert receipt is not None and receipt.processing_state == "completed"
        assert receipt.completion_status == "failed"
        assert receipt.response_json["data"]["status"] == "failed"
        assert batch is not None and batch.status == "failed"
        assert batch.current_stage == "completed"
        assert batch.cursor_before == "cursor-before-dead-letter"
        assert batch.cursor_after is None
        assert batch.finished_at is not None
        assert materialization_event is not None
        assert materialization_event.status == "dead_letter"
        assert terminal_event is not None and terminal_event.status == "pending"
        assert terminal_audit is not None
        assert materialization_audit is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ImportBatchItem)
                .where(ImportBatchItem.import_batch_id == batch_id)
            )
            == 0
        )


@pytest.mark.parametrize(
    ("field", "tampered_value", "expected_status", "expected_code"),
    [
        (
            "schema_version",
            "auris-flow-audio-import-result-v0",
            422,
            "AUDIO_IMPORT_COMPLETION_CONTRACT_INVALID",
        ),
        (
            "execution_contract",
            "auris-flow-generic-v1",
            409,
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
        ),
        (
            "execution_envelope_sha256",
            "f" * 64,
            409,
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
        ),
        (
            "batch_status",
            "succeeded",
            422,
            "AUDIO_IMPORT_COMPLETION_CONTRACT_INVALID",
        ),
    ],
)
def test_audio_import_completion_rejects_tampered_frozen_contract_without_mutation(
    client,
    auth_headers,
    field: str,
    tampered_value: str,
    expected_status: int,
    expected_code: str,
) -> None:
    suffix = field.replace("_", "-")
    run_id = f"task_run_import_tampered_{suffix}"
    batch_id = f"import_batch_tampered_{suffix}"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id=f"root_import_tampered_{suffix}",
        external_run_id=f"dagster-import-tampered-{suffix}",
        cursor_before="cursor-before-tamper",
    )
    result = _completion_result(run_id)
    result["import_batch_id"] = batch_id
    result[field] = tampered_value

    rejected = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": f"receipt-import-tampered-{suffix}",
            "external_id": f"dagster-import-tampered-{suffix}",
            "result_ref": result,
            "metrics": {
                "total": 2,
                "succeeded": 1,
                "skipped": 0,
                "failed": 1,
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": f"complete-import-tampered-{suffix}",
        },
    )

    assert rejected.status_code == expected_status, rejected.text
    assert rejected.json()["error"]["code"] == expected_code
    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "running"
        assert batch.cursor_after is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ImportBatchItem)
                .where(ImportBatchItem.import_batch_id == batch_id)
            )
            == 0
        )


def test_audio_import_completion_rejects_executor_claimed_skipped_item(
    client,
    auth_headers,
) -> None:
    run_id = "task_run_import_forged_skipped"
    batch_id = "import_batch_forged_skipped"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id="root_import_forged_skipped",
        external_run_id="dagster-import-forged-skipped",
        cursor_before="cursor-before-forged-skipped",
    )
    result = _single_success_result(
        run_id,
        batch_id=batch_id,
        suffix="forged_skipped",
    )
    result["items"] = [
        {
            "external_record_id": "platform-call-forged-skipped",
            "status": "skipped",
        }
    ]
    result["storage_objects"] = result["storage_objects"][:1]

    rejected = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "receipt-import-forged-skipped",
            "external_id": "dagster-import-forged-skipped",
            "result_ref": result,
            "metrics": {
                "total": 1,
                "succeeded": 0,
                "skipped": 1,
                "failed": 0,
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-forged-skipped",
        },
    )

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID"
    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "running"
        assert batch.cursor_after is None


def test_audio_import_deduplicates_same_external_id_and_checksum_across_batches(
    client,
    auth_headers,
) -> None:
    first_run_id = "task_run_import_dedupe_first"
    second_run_id = "task_run_import_dedupe_second"
    first_batch_id = "import_batch_dedupe_first"
    second_batch_id = "import_batch_dedupe_second"
    with SessionLocal() as session:
        recordings_before = int(
            session.scalar(
                select(func.count())
                .select_from(AudioRecording)
                .where(
                    AudioRecording.tenant_id == "aurora_auto",
                    AudioRecording.project_id == "sales_qa",
                )
            )
            or 0
        )
        sessions_before = int(
            session.scalar(
                select(func.count())
                .select_from(JsonResource)
                .where(
                    JsonResource.tenant_id == "aurora_auto",
                    JsonResource.project_id == "sales_qa",
                    JsonResource.collection == "audio_sessions",
                )
            )
            or 0
        )
    _seed_import_run(
        run_id=first_run_id,
        batch_id=first_batch_id,
        root_trace_id="root_import_dedupe_first",
        external_run_id="dagster-import-dedupe-first",
        cursor_before="cursor-before",
    )

    def complete(
        run_id: str,
        batch_id: str,
        external_run_id: str,
        suffix: str,
    ):
        return client.post(
            f"/api/v1/task-runs/{run_id}/completion-receipts",
            json={
                "adapter": "dagster",
                "status": "success",
                "completion_receipt_id": f"receipt-{suffix}",
                "external_id": external_run_id,
                "result_ref": _single_success_result(
                    run_id,
                    batch_id=batch_id,
                    suffix=suffix,
                ),
                "metrics": {
                    "total": 1,
                    "succeeded": 1,
                    "skipped": 0,
                    "failed": 0,
                },
            },
            headers={
                **auth_headers,
                "Idempotency-Key": f"complete-import-{suffix}",
            },
        )

    first = complete(
        first_run_id,
        first_batch_id,
        "dagster-import-dedupe-first",
        "dedupe_first",
    )
    assert first.status_code == 202, first.text
    assert process_aggregate_events([first_run_id]) == 1

    _seed_import_run(
        run_id=second_run_id,
        batch_id=second_batch_id,
        root_trace_id="root_import_dedupe_second",
        external_run_id="dagster-import-dedupe-second",
        cursor_before="cursor-after-2",
    )
    second = complete(
        second_run_id,
        second_batch_id,
        "dagster-import-dedupe-second",
        "dedupe_second",
    )
    assert second.status_code == 202, second.text
    assert process_aggregate_events([second_run_id]) == 1

    with SessionLocal() as session:
        second_batch = session.get(ImportBatch, second_batch_id)
        assert second_batch is not None
        assert second_batch.status == "succeeded"
        assert (
            second_batch.total_items,
            second_batch.succeeded_items,
            second_batch.skipped_items,
            second_batch.failed_items,
        ) == (1, 0, 1, 0)
        second_item = session.scalar(
            select(ImportBatchItem).where(ImportBatchItem.import_batch_id == second_batch_id)
        )
        assert second_item is not None
        assert second_item.status == "skipped"
        assert second_item.audio_session_id
        assert (
            session.scalar(
                select(func.count())
                .select_from(AudioRecording)
                .where(
                    AudioRecording.tenant_id == "aurora_auto",
                    AudioRecording.project_id == "sales_qa",
                )
            )
            == recordings_before + 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(JsonResource)
                .where(
                    JsonResource.tenant_id == "aurora_auto",
                    JsonResource.project_id == "sales_qa",
                    JsonResource.collection == "audio_sessions",
                )
            )
            == sessions_before + 1
        )


def test_audio_import_empty_window_completes_without_creating_sessions(
    client,
    auth_headers,
) -> None:
    run_id = "task_run_import_empty_window"
    batch_id = "import_batch_empty_window"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id="root_import_empty_window",
        external_run_id="dagster-import-empty-window",
        cursor_before="cursor-before-empty",
    )
    result = _single_success_result(
        run_id,
        batch_id=batch_id,
        suffix="empty_window",
    )
    result["items"] = []
    result["storage_objects"] = result["storage_objects"][:1]
    result["next_cursor_candidate"] = "cursor-after-empty"

    completed = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": "receipt-empty-window",
            "external_id": "dagster-import-empty-window",
            "result_ref": result,
            "metrics": {
                "total": 0,
                "succeeded": 0,
                "skipped": 0,
                "failed": 0,
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-empty-window",
        },
    )

    assert completed.status_code == 202, completed.text
    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "succeeded"
        assert batch.current_stage == "completed"
        assert batch.total_items == 0
        assert batch.cursor_after == "cursor-after-empty"
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == "connector_audio_import_completion",
            )
        )
        assert connector is not None
        assert connector.data["sync_cursor"] == "cursor-after-empty"
        assert connector.data["sync_cursor_connector_version"] == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(ImportBatchItem)
                .where(ImportBatchItem.import_batch_id == batch_id)
            )
            == 0
        )


def test_audio_import_all_item_failures_fail_task_run_and_remain_retryable(
    client,
    auth_headers,
) -> None:
    run_id = "task_run_import_all_items_failed"
    batch_id = "import_batch_all_items_failed"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id="root_import_all_items_failed",
        external_run_id="dagster-import-all-items-failed",
        cursor_before="cursor-before-all-items-failed",
    )
    result = _completion_result(run_id)
    result["import_batch_id"] = batch_id
    result["batch_status"] = "failed"
    result["manifest_storage_object_id"] = "sto_import_manifest_all_items_failed"
    result["items"] = [
        {
            "external_record_id": "platform-call-all-items-failed",
            "status": "failed",
            "error_code": "AUDIO_DOWNLOAD_FAILED",
        }
    ]
    result["storage_objects"] = [
        {
            **result["storage_objects"][0],
            "storage_object_id": "sto_import_manifest_all_items_failed",
        }
    ]

    completed = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            # Dagster completed the import job, but the BFF is authoritative for
            # whether any business item could be materialized.
            "status": "success",
            "completion_receipt_id": "receipt-import-all-items-failed",
            "external_id": "dagster-import-all-items-failed",
            "result_ref": result,
            "metrics": {
                "total": 1,
                "succeeded": 0,
                "skipped": 0,
                "failed": 1,
            },
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-all-items-failed",
        },
    )

    assert completed.status_code == 202, completed.text
    assert process_aggregate_events([run_id]) == 1
    finalized = client.get(f"/api/v1/task-runs/{run_id}", headers=auth_headers)
    assert finalized.status_code == 200
    data = finalized.json()["data"]
    assert data["status"] == "failed"
    assert data["business_status"] == "failed"
    assert data["error_code"] == "AUDIO_IMPORT_ALL_ITEMS_FAILED"
    assert data["retryable"] is True
    assert data["completion_receipt"]["status"] == "success"
    assert data["import_batch"]["status"] == "failed"
    assert {action["key"] for action in data["next_actions"]} >= {
        "view_import_batch",
        "retry",
        "view_trace",
    }

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        batch = session.get(ImportBatch, batch_id)
        item = session.scalar(
            select(ImportBatchItem).where(
                ImportBatchItem.import_batch_id == batch_id,
                ImportBatchItem.external_record_id == "platform-call-all-items-failed",
            )
        )
        assert run is not None
        assert run.status == "failed"
        assert run.payload["import_batch"]["import_batch_id"] == batch_id
        assert run.payload["registered_storage_objects"]
        retry_payload = retry_payload_from_record(
            run,
            RequestContext(
                tenant_id=run.tenant_id,
                project_id=run.project_id,
                user_id="u_admin_001",
                roles=("project_admin",),
                request_id="retry-all-items-failed",
                trace_id="trace-retry-all-items-failed",
                idempotency_key="retry-all-items-failed",
            ),
            {"reason": "重试全部失败的音频导入项"},
        )
        assert "import_batch" not in retry_payload
        assert "registered_storage_objects" not in retry_payload
        assert "experiment_completion" not in retry_payload
        assert batch is not None
        assert batch.status == "failed"
        assert batch.cursor_before == "cursor-before-all-items-failed"
        assert batch.cursor_after is None
        assert item is not None
        assert item.status == "failed"
        assert item.error_code == "AUDIO_DOWNLOAD_FAILED"


def test_audio_import_operational_failure_closes_the_batch_stage(
    client,
    auth_headers,
) -> None:
    run_id = "task_run_import_operational_failure"
    batch_id = "import_batch_operational_failure"
    _seed_import_run(
        run_id=run_id,
        batch_id=batch_id,
        root_trace_id="root_import_operational_failure",
        external_run_id="dagster-import-operational-failure",
        cursor_before="cursor-before-failure",
    )

    failed = client.post(
        f"/api/v1/task-runs/{run_id}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "failed",
            "completion_receipt_id": "receipt-import-operational-failure",
            "external_id": "dagster-import-operational-failure",
            "result_ref": {
                "schema_version": AUDIO_IMPORT_RESULT_SCHEMA,
                "execution_contract": AUDIO_IMPORT_EXECUTION_CONTRACT,
                "execution_envelope_sha256": AUDIO_IMPORT_ENVELOPE_SHA256,
                "import_batch_id": batch_id,
                "batch_status": "failed",
            },
            "error_code": "AUDIO_IMPORT_SOURCE_LIST_FAILED",
            "retryable": True,
        },
        headers={
            **auth_headers,
            "Idempotency-Key": "complete-import-operational-failure",
        },
    )

    assert failed.status_code == 200, failed.text
    with SessionLocal() as session:
        batch = session.get(ImportBatch, batch_id)
        assert batch is not None
        assert batch.status == "failed"
        assert batch.current_stage == "completed"
        assert batch.cursor_after is None
