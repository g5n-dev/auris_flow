from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    OutboxEvent,
    RunRecord,
)
from app.services import connector_import_service
from app.services.adapters import AUDIO_IMPORT_JOB_NAME, LocalDagsterClient
from app.services.task_run_monitor_service import monitor_task_runs_once
from app.workers.outbox_worker import process_aggregate_events


def _write_headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _connector_payload(*, connector_id: str = "connector_audio_import") -> dict[str, object]:
    return {
        "connector_id": connector_id,
        "name": "平台录音导入",
        "source_type": "platform_audio_url_api",
        "platform_connection_id": "conn_platform_auth",
        "credential_ref": "secret://platform/audio-reader",
        "base_url": "https://recordings.example.test",
        "request_path": "/v1/recordings",
        "field_mapping": {
            "external_record_id": "recording_id",
            "audio_url": "download_url",
            "started_at": "started_at",
            "agent_ref": "employee.badge",
            "store_ref": "store_id",
            "duration_ms": "duration_ms",
        },
        "pagination": {
            "mode": "cursor",
            "page_size": 100,
            "cursor_param": "cursor",
            "next_cursor_path": "next_cursor",
        },
        "cursor_policy": {
            "field": "updated_at",
            "initial_window_start": "2026-07-01T00:00:00+00:00",
        },
        "platform_scope": {
            "tenant_ref": "tenant-ext-001",
            "store_refs": ["BJ-AURORA-001"],
        },
        "target_asset_key": "auris/audio/raw_recordings",
        "dedupe_policy": "external_id_checksum",
    }


def _create_connector(client, auth_headers, *, connector_id: str = "connector_audio_import"):
    response = client.post(
        "/api/v1/connectors",
        json=_connector_payload(connector_id=connector_id),
        headers=_write_headers(auth_headers, f"create-{connector_id}"),
    )
    assert response.status_code == 201, response.text
    return response


def _create_import_task_version(
    client,
    auth_headers,
    *,
    connector_id: str,
    task_version_id: str,
):
    response = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": task_version_id,
            "task_type_id": "audio-platform-import",
            "version": "v1",
            "connector_id": connector_id,
        },
        headers=_write_headers(auth_headers, f"create-{task_version_id}"),
    )
    assert response.status_code == 201, response.text
    return response


def test_connector_create_requires_complete_platform_audio_contract_and_rejects_secrets(
    client,
    auth_headers,
) -> None:
    with SessionLocal() as session:
        connectors_before = session.scalar(
            select(func.count())
            .select_from(JsonResource)
            .where(JsonResource.collection == "connectors")
        )
        outbox_before = session.scalar(select(func.count()).select_from(OutboxEvent))

    missing_mapping = _connector_payload(connector_id="connector_missing_mapping")
    missing_mapping.pop("field_mapping")
    missing = client.post(
        "/api/v1/connectors",
        json=missing_mapping,
        headers=_write_headers(auth_headers, "connector-missing-mapping"),
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "CONNECTOR_CONFIGURATION_INVALID"

    raw_secret = {
        **_connector_payload(connector_id="connector_raw_secret"),
        "api_key": "must-never-be-persisted",
    }
    rejected_secret = client.post(
        "/api/v1/connectors",
        json=raw_secret,
        headers=_write_headers(auth_headers, "connector-raw-secret"),
    )
    assert rejected_secret.status_code == 422, rejected_secret.text
    assert rejected_secret.json()["error"]["code"] == "VALIDATION_ERROR"

    unsafe_url = {
        **_connector_payload(connector_id="connector-private-url"),
        "base_url": "http://127.0.0.1:8080",
    }
    rejected_url = client.post(
        "/api/v1/connectors",
        json=unsafe_url,
        headers=_write_headers(auth_headers, "connector-private-url"),
    )
    assert rejected_url.status_code == 422, rejected_url.text

    forged_cursor = _connector_payload(connector_id="connector-forged-cursor")
    forged_cursor["cursor_policy"] = {
        **forged_cursor["cursor_policy"],
        "cursor_value": "caller-controlled-cursor",
    }
    rejected_cursor = client.post(
        "/api/v1/connectors",
        json=forged_cursor,
        headers=_write_headers(auth_headers, "connector-forged-cursor"),
    )
    assert rejected_cursor.status_code == 422, rejected_cursor.text
    assert rejected_cursor.json()["error"]["code"] == "CONNECTOR_CURSOR_SERVER_OWNED"

    oversized_page = _connector_payload(connector_id="connector-oversized-page")
    oversized_page["pagination"] = {
        **dict(oversized_page["pagination"]),
        "page_size": 251,
    }
    rejected_page = client.post(
        "/api/v1/connectors",
        json=oversized_page,
        headers=_write_headers(auth_headers, "connector-oversized-page"),
    )
    assert rejected_page.status_code == 422, rejected_page.text

    unscoped_store_mapping = _connector_payload(
        connector_id="connector-missing-store-scope-mapping"
    )
    del unscoped_store_mapping["field_mapping"]["store_ref"]
    rejected_store_scope = client.post(
        "/api/v1/connectors",
        json=unscoped_store_mapping,
        headers=_write_headers(auth_headers, "connector-missing-store-scope-mapping"),
    )
    assert rejected_store_scope.status_code == 422, rejected_store_scope.text

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(JsonResource)
                .where(JsonResource.collection == "connectors")
            )
            == connectors_before
        )
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_before


def test_connector_create_rejects_plaintext_credential_ref_before_persistence(
    client,
    auth_headers,
) -> None:
    plaintext_credential = "RawApiToken123"
    payload = _connector_payload(connector_id="connector_plaintext_credential")
    payload["credential_ref"] = plaintext_credential
    with SessionLocal() as session:
        outbox_before = session.scalar(select(func.count()).select_from(OutboxEvent))

    rejected = client.post(
        "/api/v1/connectors",
        json=payload,
        headers=_write_headers(auth_headers, "connector-plaintext-credential"),
    )

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "CONNECTOR_CONFIGURATION_INVALID"
    assert plaintext_credential not in rejected.text
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(JsonResource).where(
                    JsonResource.collection == "connectors",
                    JsonResource.resource_key == "connector_plaintext_credential",
                )
            )
            is None
        )
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_before


def test_connector_create_patch_and_read_keep_a_server_owned_version(
    client,
    auth_headers,
) -> None:
    created = _create_connector(client, auth_headers)
    data = created.json()["data"]
    assert data["source_type"] == "platform_audio_url_api"
    assert data["connector_version"] == 1
    assert data["credential_ref"] == "secret://platform/audio-reader"
    assert "api_key" not in data

    fetched = client.get("/api/v1/connectors/connector_audio_import", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["connector_version"] == 1

    patched = client.patch(
        "/api/v1/connectors/connector_audio_import",
        json={"request_path": "/v2/recordings"},
        headers=_write_headers(auth_headers, "patch-connector-audio-import"),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["request_path"] == "/v2/recordings"
    assert patched.json()["data"]["connector_version"] == 2
    assert patched.json()["data"]["field_mapping"]["audio_url"] == "download_url"

    before_id_change = client.get(
        "/api/v1/connectors/connector_audio_import",
        headers=auth_headers,
    ).json()["data"]
    rejected_id_change = client.patch(
        "/api/v1/connectors/connector_audio_import",
        json={"connector_id": "connector_audio_import_renamed"},
        headers=_write_headers(auth_headers, "patch-connector-audio-import-id"),
    )
    assert rejected_id_change.status_code == 409, rejected_id_change.text
    assert rejected_id_change.json()["error"]["code"] == "CONNECTOR_ID_IMMUTABLE"
    assert (
        client.get(
            "/api/v1/connectors/connector_audio_import",
            headers=auth_headers,
        ).json()["data"]
        == before_id_change
    )
    assert (
        client.get(
            "/api/v1/connectors/connector_audio_import_renamed",
            headers=auth_headers,
        ).status_code
        == 404
    )

    with SessionLocal() as session:
        audit = session.scalar(
            select(AuditLog)
            .where(
                AuditLog.object_type == "connectors",
                AuditLog.object_id == "connector_audio_import",
                AuditLog.action == "connectors.patch",
            )
            .order_by(AuditLog.audit_id.desc())
        )
        assert audit is not None
        assert "must-never-be-persisted" not in json.dumps(audit.after_json, ensure_ascii=False)


def test_connection_test_and_record_preview_are_idempotent_bounded_and_secret_safe(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    _create_connector(client, auth_headers, connector_id="connector_preview")
    with SessionLocal.begin() as session:
        persisted = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == "connector_preview",
            )
        )
        assert persisted is not None
        persisted.data = {
            **persisted.data,
            "sync_cursor": "2026-07-27T09:59:59+08:00",
        }
    signed_url_secret = "signed-url-secret-canary"
    authorization_secret = "authorization-secret-canary"
    calls: list[int] = []

    def fake_fetch(
        connector: dict[str, object],
        *,
        limit: int,
        tenant_id: str,
        project_id: str,
    ):
        assert connector["credential_ref"] == "secret://platform/audio-reader"
        assert connector["cursor_policy"]["cursor_value"] == "2026-07-27T09:59:59+08:00"
        assert tenant_id == "aurora_auto"
        assert project_id == "sales_qa"
        calls.append(limit)
        return 200, {
            "records": [
                {
                    "recording_id": "external-call-001",
                    "download_url": (
                        f"https://audio.example.test/call-001.wav?X-Signature={signed_url_secret}"
                    ),
                    "started_at": "2026-07-27T10:00:00+08:00",
                    "employee": {"badge": "A-1001"},
                    "store_id": "BJ-AURORA-001",
                    "duration_ms": 42_000,
                    "updated_at": "2026-07-27T10:00:01+08:00",
                    "Authorization": authorization_secret,
                }
            ],
            "next_cursor": "2026-07-27T10:00:01+08:00",
        }

    monkeypatch.setattr(
        "app.services.connector_import_service.fetch_connector_json",
        fake_fetch,
    )

    test_headers = _write_headers(auth_headers, "connector-preview-test")
    tested = client.post(
        "/api/v1/connectors/connector_preview/connection-tests",
        json={},
        headers=test_headers,
    )
    replay = client.post(
        "/api/v1/connectors/connector_preview/connection-tests",
        json={},
        headers=test_headers,
    )
    assert tested.status_code == replay.status_code == 200
    assert tested.json() == replay.json()
    assert tested.json()["data"]["status"] == "success"

    preview = client.post(
        "/api/v1/connectors/connector_preview/record-previews",
        json={"limit": 3},
        headers=_write_headers(auth_headers, "connector-record-preview"),
    )
    assert preview.status_code == 200, preview.text
    preview_data = preview.json()["data"]
    assert preview_data["record_count"] == 1
    assert preview_data["records"] == [
        {
            "recording_id": "external-call-001",
            "download_url": True,
            "started_at": "2026-07-27T10:00:00+08:00",
            "employee.badge": "A-1001",
            "store_id": "BJ-AURORA-001",
            "duration_ms": 42_000,
            "updated_at": "2026-07-27T10:00:01+08:00",
        }
    ]
    assert preview_data["fields"] == [
        "download_url",
        "duration_ms",
        "employee.badge",
        "recording_id",
        "started_at",
        "store_id",
        "updated_at",
    ]
    assert preview_data["mapping_valid"] is True
    assert preview_data["mapping_errors"] == []
    serialized = json.dumps(preview.json(), ensure_ascii=False)
    assert signed_url_secret not in serialized
    assert authorization_secret not in serialized
    assert "X-Signature" not in serialized
    assert calls == [1, 3]

    with SessionLocal() as session:
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == "connector_preview",
            )
        )
        assert connector is not None
        assert connector.data["last_connection_test"]["status"] == "success"
        assert connector.data["last_record_preview"]["record_count"] == 1
        assert connector.data["last_record_preview"]["mapping_valid"] is True
        assert signed_url_secret not in json.dumps(connector.data, ensure_ascii=False)


@pytest.mark.parametrize(
    ("probe_path", "probe_body", "observation_field"),
    [
        ("connection-tests", {}, "last_connection_test"),
        ("record-previews", {"limit": 3}, "last_record_preview"),
    ],
)
def test_connector_probe_rejects_concurrent_semantic_change_without_lost_update(
    client,
    auth_headers,
    monkeypatch,
    probe_path: str,
    probe_body: dict[str, object],
    observation_field: str,
) -> None:
    connector_id = f"connector_probe_cas_{probe_path.replace('-', '_')}"
    _create_connector(client, auth_headers, connector_id=connector_id)

    def fetch_before_concurrent_change(
        _connector: dict[str, object],
        *,
        limit: int,
        tenant_id: str,
        project_id: str,
    ):
        assert limit in {1, 3}
        assert tenant_id == "aurora_auto"
        assert project_id == "sales_qa"
        return 200, {"records": []}

    original_lock = connector_import_service.lock_connector_after_probe

    def mutate_before_lock(
        session,
        ctx,
        locked_connector_id: str,
        *,
        expected_connector_version: int,
        expected_semantic_sha256: str,
    ):
        persisted = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection == "connectors",
                JsonResource.resource_key == locked_connector_id,
            )
        )
        assert persisted is not None
        persisted.data = {
            **persisted.data,
            "request_path": "/v2/recordings",
            "connector_version": 2,
        }
        # Deterministically model a semantic write committed between the
        # external response and the router's post-probe locking read.
        session.commit()
        return original_lock(
            session,
            ctx,
            locked_connector_id,
            expected_connector_version=expected_connector_version,
            expected_semantic_sha256=expected_semantic_sha256,
        )

    monkeypatch.setattr(
        "app.services.connector_import_service.fetch_connector_json",
        fetch_before_concurrent_change,
    )
    monkeypatch.setattr(
        "app.api.routers.imports.lock_connector_after_probe",
        mutate_before_lock,
    )
    rejected = client.post(
        f"/api/v1/connectors/{connector_id}/{probe_path}",
        json=probe_body,
        headers=_write_headers(auth_headers, f"probe-cas-{probe_path}"),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "CONNECTOR_CHANGED_DURING_PROBE"

    persisted = client.get(
        f"/api/v1/connectors/{connector_id}",
        headers=auth_headers,
    )
    assert persisted.status_code == 200, persisted.text
    persisted_data = persisted.json()["data"]
    assert persisted_data["request_path"] == "/v2/recordings"
    assert persisted_data["connector_version"] == 2
    assert observation_field not in persisted_data


def test_task_publish_freezes_connector_snapshot_and_production_run_creates_one_batch(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    connector_id = "connector_frozen_import"
    task_version_id = "task_version_audio_import_v1"
    _create_connector(client, auth_headers, connector_id=connector_id)
    _create_import_task_version(
        client,
        auth_headers,
        connector_id=connector_id,
        task_version_id=task_version_id,
    )

    unverified_publish = client.post(
        f"/api/v1/task-versions/{task_version_id}/publish",
        json={"reason": "不应绕过真实测试与预览"},
        headers=_write_headers(auth_headers, "publish-unverified-audio-import-v1"),
    )
    assert unverified_publish.status_code == 409, unverified_publish.text
    assert unverified_publish.json()["error"]["code"] == "TASK_IMPORT_CONNECTOR_NOT_VERIFIED"

    def fake_fetch(
        _connector: dict[str, object],
        *,
        limit: int,
        tenant_id: str,
        project_id: str,
    ):
        assert tenant_id == "aurora_auto"
        assert project_id == "sales_qa"
        return 200, {
            "records": [
                {
                    "recording_id": "verify-recording-001",
                    "download_url": "https://media.example.test/verify-recording-001.wav",
                    "started_at": "2026-07-27T10:00:00+08:00",
                    "employee": {"badge": "A-1001"},
                    "store_id": "BJ-AURORA-001",
                    "duration_ms": 42_000,
                    "updated_at": "2026-07-27T10:00:01+08:00",
                }
            ][:limit],
            "next_cursor": "2026-07-27T10:00:01+08:00",
        }

    monkeypatch.setattr(
        "app.services.connector_import_service.fetch_connector_json",
        fake_fetch,
    )
    tested = client.post(
        f"/api/v1/connectors/{connector_id}/connection-tests",
        json={},
        headers=_write_headers(auth_headers, "test-frozen-audio-import"),
    )
    previewed = client.post(
        f"/api/v1/connectors/{connector_id}/record-previews",
        json={"limit": 3},
        headers=_write_headers(auth_headers, "preview-frozen-audio-import"),
    )
    assert tested.status_code == 200, tested.text
    assert previewed.status_code == 200, previewed.text

    requested_publish = client.post(
        f"/api/v1/task-versions/{task_version_id}/publish",
        json={"reason": "冻结平台录音导入配置"},
        headers=_write_headers(auth_headers, "publish-audio-import-v1"),
    )
    replayed_publish = client.post(
        f"/api/v1/task-versions/{task_version_id}/publish",
        json={"reason": "冻结平台录音导入配置"},
        headers=_write_headers(auth_headers, "publish-audio-import-v1"),
    )
    assert requested_publish.status_code == replayed_publish.status_code == 202
    assert requested_publish.json() == replayed_publish.json()
    assert requested_publish.json()["data"]["task_version_id"] == task_version_id
    assert requested_publish.json()["data"]["status"] == "published"

    frozen = client.get(f"/api/v1/task-versions/{task_version_id}", headers=auth_headers)
    assert frozen.status_code == 200, frozen.text
    frozen_data = frozen.json()["data"]
    assert frozen_data["status"] == "published"
    assert frozen_data["connector_snapshot"]["connector_id"] == connector_id
    assert frozen_data["connector_snapshot"]["connector_version"] == "1"
    assert frozen_data["connector_snapshot"]["request_path"] == "/v1/recordings"
    assert "cursor_value" not in frozen_data["connector_snapshot"]["cursor_policy"]
    assert len(frozen_data["connector_snapshot_sha256"]) == 64
    assert frozen_data["execution_contract"] == "auris-flow-audio-import-v1"

    connector_before_duplicate_create = client.get(
        f"/api/v1/connectors/{connector_id}",
        headers=auth_headers,
    ).json()["data"]
    duplicate_payload = _connector_payload(connector_id=connector_id)
    duplicate_payload["request_path"] = "/v2/recordings"
    rejected_duplicate = client.post(
        "/api/v1/connectors",
        json=duplicate_payload,
        headers=_write_headers(auth_headers, "duplicate-frozen-audio-import-connector"),
    )
    assert rejected_duplicate.status_code == 409, rejected_duplicate.text
    assert rejected_duplicate.json()["error"]["code"] == "RESOURCE_ALREADY_EXISTS"
    assert (
        client.get(
            f"/api/v1/connectors/{connector_id}",
            headers=auth_headers,
        ).json()["data"]
        == connector_before_duplicate_create
    )

    patched = client.patch(
        f"/api/v1/connectors/{connector_id}",
        json={"request_path": "/v2/recordings"},
        headers=_write_headers(auth_headers, "patch-frozen-connector"),
    )
    assert patched.status_code == 409, patched.text
    assert patched.json()["error"]["code"] == "CONNECTOR_PUBLISHED_SEMANTICS_IMMUTABLE"
    assert patched.json()["error"]["details"] == [
        {
            "connector_id": connector_id,
            "fields": ["request_path"],
            "task_version_ids": [task_version_id],
        }
    ]

    frozen_again = client.get(f"/api/v1/task-versions/{task_version_id}", headers=auth_headers)
    assert frozen_again.json()["data"]["connector_snapshot"]["request_path"] == "/v1/recordings"

    diagnostic = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "diagnostic",
        },
        headers=_write_headers(auth_headers, "diagnostic-audio-import-v1"),
    )
    assert diagnostic.status_code == 409, diagnostic.text
    assert diagnostic.json()["error"]["code"] == "TASK_IMPORT_PRODUCTION_MODE_REQUIRED"

    # A historic published snapshot must never consume a cursor produced by a
    # different connector version.  Simulate pre-fix/corrupt persisted state:
    # the API itself no longer permits this semantic mutation.
    with SessionLocal.begin() as session:
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == connector_id,
            )
        )
        assert connector is not None
        connector.data = {
            **connector.data,
            "request_path": "/v2/recordings",
            "connector_version": 2,
            "sync_cursor": "cursor-live-7",
            "sync_cursor_import_batch_id": "import_batch_previous",
            "sync_cursor_trace_id": "trace_previous_import",
            "sync_cursor_connector_version": 2,
        }

    stale_version_run = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "run-stale-audio-import-version"),
    )
    assert stale_version_run.status_code == 409, stale_version_run.text
    assert stale_version_run.json()["error"]["code"] == "TASK_IMPORT_CONNECTOR_VERSION_MISMATCH"

    # Even with the live connector restored to the frozen version, an
    # explicitly cross-version cursor must fail closed.
    with SessionLocal.begin() as session:
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == connector_id,
            )
        )
        assert connector is not None
        connector.data = {
            **connector.data,
            "request_path": "/v1/recordings",
            "connector_version": 1,
            "sync_cursor_connector_version": 2,
        }

    stale_cursor_run = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "run-stale-audio-import-cursor"),
    )
    assert stale_cursor_run.status_code == 409, stale_cursor_run.text
    assert stale_cursor_run.json()["error"]["code"] == "CONNECTOR_CURSOR_VERSION_MISMATCH"

    with SessionLocal.begin() as session:
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == connector_id,
            )
        )
        assert connector is not None
        connector.data = {
            **connector.data,
            "sync_cursor_connector_version": 1,
        }

    # Display metadata remains editable and must not create a new extraction
    # version or invalidate the cursor/version relationship.
    patched_live_connector = client.patch(
        f"/api/v1/connectors/{connector_id}",
        json={"description": "保留服务端同步游标"},
        headers=_write_headers(auth_headers, "patch-live-cursor-connector"),
    )
    assert patched_live_connector.status_code == 200, patched_live_connector.text
    assert patched_live_connector.json()["data"]["connector_version"] == 1
    assert patched_live_connector.json()["data"]["sync_cursor"] == "cursor-live-7"
    assert (
        patched_live_connector.json()["data"]["sync_cursor_import_batch_id"]
        == "import_batch_previous"
    )
    assert patched_live_connector.json()["data"]["sync_cursor_trace_id"] == "trace_previous_import"
    assert patched_live_connector.json()["data"]["sync_cursor_connector_version"] == 1

    run_headers = _write_headers(auth_headers, "run-audio-import-v1")
    created_run = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
        },
        headers=run_headers,
    )
    replayed_run = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
        },
        headers=run_headers,
    )
    assert created_run.status_code == replayed_run.status_code == 202
    assert created_run.json() == replayed_run.json()
    run_data = created_run.json()["data"]
    assert run_data["import_batch_id"].startswith("import_batch_")
    assert run_data["root_trace_id"]

    concurrent = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "run-audio-import-v1-concurrent"),
    )
    assert concurrent.status_code == 409, concurrent.text
    assert concurrent.json()["error"]["code"] == "CONNECTOR_IMPORT_ALREADY_ACTIVE"

    blocked_patch = client.patch(
        f"/api/v1/connectors/{connector_id}",
        json={"request_path": "/v3/recordings"},
        headers=_write_headers(auth_headers, "patch-active-import-connector"),
    )
    assert blocked_patch.status_code == 409, blocked_patch.text
    assert blocked_patch.json()["error"]["code"] == "CONNECTOR_IMPORT_ALREADY_ACTIVE"

    with SessionLocal() as session:
        batches = list(
            session.scalars(
                select(ImportBatch).where(
                    ImportBatch.task_run_id == run_data["run_id"],
                    ImportBatch.tenant_id == "aurora_auto",
                    ImportBatch.project_id == "sales_qa",
                )
            )
        )
        assert len(batches) == 1
        batch = batches[0]
        assert batch.import_batch_id == run_data["import_batch_id"]
        assert batch.status == "queued"
        assert batch.current_stage == "queued"
        assert batch.connector_id == connector_id
        assert batch.cursor_before == "cursor-live-7"
        assert batch.root_trace_id == run_data["root_trace_id"]

        record = session.get(RunRecord, run_data["run_id"])
        assert record is not None
        assert record.payload["execution_contract"] == "auris-flow-audio-import-v1"
        assert record.payload["connector_snapshot"]["connector_version"] == "1"
        assert record.payload["connector_snapshot"]["request_path"] == "/v1/recordings"
        assert (
            record.payload["connector_snapshot"]["cursor_policy"]["cursor_value"] == "cursor-live-7"
        )
        assert record.payload["target"]["target_asset_key"] == "auris/audio/raw_recordings"
        assert record.payload["target"]["object_prefix"].endswith(
            f"/runs/{run_data['run_id']}/audio-import/"
        )
        assert record.payload["target"]["dedupe_policy"] == "external_id_checksum"
        assert record.payload["execution_deadline_at"]

        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_type == "task_run",
                OutboxEvent.aggregate_id == run_data["run_id"],
            )
        )
        assert event is not None
        assert event.event_type == "task_run.requested"
        assert event.payload["data"]["import_batch_id"] == batch.import_batch_id
        dispatched = LocalDagsterClient().submit_run_request(
            {**event.payload, "outbox_fencing_token": "1:1"}
        )
        assert dispatched.status == "success"
        assert dispatched.details["job_name"] == AUDIO_IMPORT_JOB_NAME
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.object_type == "import_batch",
                AuditLog.object_id == batch.import_batch_id,
            )
        )
        assert audit is not None
        connector = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "connectors",
                JsonResource.resource_key == connector_id,
            )
        )
        assert connector is not None
        assert connector.data["request_path"] == "/v1/recordings"
        assert connector.data["sync_cursor"] == "cursor-live-7"
        assert connector.data["sync_cursor_connector_version"] == 1

    batch_detail = client.get(
        f"/api/v1/import-batches/{run_data['import_batch_id']}",
        headers=auth_headers,
    )
    assert batch_detail.status_code == 200, batch_detail.text
    assert batch_detail.json()["data"]["status"] == "queued"
    assert batch_detail.json()["data"]["current_stage"] == "queued"
    assert batch_detail.json()["data"]["total_items"] == 0

    batch_items = client.get(
        f"/api/v1/import-batches/{run_data['import_batch_id']}/items",
        headers=auth_headers,
    )
    assert batch_items.status_code == 200, batch_items.text
    assert batch_items.json()["data"]["items"] == []
    assert batch_items.json()["meta"]["total"] == 0

    # “重试失败项”必须创建新的 production TaskRun 与 ImportBatch。部分批次
    # 不推进游标，因此新批次会重读同一窗口并由去重策略跳过既有成功项。
    with SessionLocal.begin() as session:
        failed_record = session.get(RunRecord, run_data["run_id"])
        failed_batch = session.get(ImportBatch, run_data["import_batch_id"])
        assert failed_record is not None and failed_batch is not None
        failed_record.status = "success"
        failed_record.payload = {
            **failed_record.payload,
            "status": "success",
            "business_status": "success",
        }
        failed_batch.status = "partial"
        failed_batch.current_stage = "completed"
        failed_batch.total_items = 0
        failed_batch.failed_items = 0
        failed_batch.payload = {
            **failed_batch.payload,
            "error_code": "PLATFORM_CREDENTIAL_INVALID",
            "reason": (
                "GET https://recordings.example.test/v1/recordings failed; "
                "Authorization: Bearer must-never-reach-browser"
            ),
        }

    batch_level_failure = client.get(
        f"/api/v1/import-batches/{run_data['import_batch_id']}",
        headers=auth_headers,
    )
    assert batch_level_failure.status_code == 200, batch_level_failure.text
    public_failure = batch_level_failure.json()["data"]
    assert public_failure["status"] == "partial"
    assert public_failure["total_items"] == 0
    assert public_failure["failed_items"] == 0
    assert public_failure["error_code"] == "PLATFORM_CREDENTIAL_INVALID"
    assert public_failure["reason"]
    serialized_failure = json.dumps(public_failure, ensure_ascii=False)
    assert "recordings.example.test" not in serialized_failure
    assert "must-never-reach-browser" not in serialized_failure
    assert "Authorization" not in serialized_failure
    assert "Bearer" not in serialized_failure

    retried = client.post(
        f"/api/v1/task-runs/{run_data['run_id']}/retries",
        json={
            "reason": "重试导入批次失败项",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "retry-audio-import-v1"),
    )
    assert retried.status_code == 202, retried.text
    retry_data = retried.json()["data"]
    assert retry_data["run_id"] != run_data["run_id"]
    assert retry_data["import_batch_id"] != run_data["import_batch_id"]
    assert retry_data["execution_mode"] == "production"
    assert retry_data["retry_of_run_id"] == run_data["run_id"]

    with SessionLocal() as session:
        retry_batch = session.get(ImportBatch, retry_data["import_batch_id"])
        assert retry_batch is not None
        assert retry_batch.task_run_id == retry_data["run_id"]
        assert retry_batch.status == "queued"
        assert retry_batch.cursor_before == "cursor-live-7"
        retry_record = session.get(RunRecord, retry_data["run_id"])
        assert retry_record is not None
        assert retry_record.payload["connector_snapshot"]["request_path"] == "/v1/recordings"
        assert retry_record.payload["target"]["object_prefix"].endswith(
            f"/runs/{retry_data['run_id']}/audio-import/"
        )

    cancelled = client.post(
        f"/api/v1/task-runs/{retry_data['run_id']}/cancellations",
        json={"reason": "取消尚未分发的重试批次"},
        headers=_write_headers(auth_headers, "cancel-retried-audio-import-v1"),
    )
    assert cancelled.status_code == 202, cancelled.text
    with SessionLocal() as session:
        cancelled_batch = session.get(ImportBatch, retry_data["import_batch_id"])
        assert cancelled_batch is not None
        assert cancelled_batch.status == "cancelled"
        assert cancelled_batch.current_stage == "completed"
        assert cancelled_batch.cursor_after is None

    engine_retry = client.post(
        f"/api/v1/task-runs/{run_data['run_id']}/retries",
        json={
            "reason": "验证执行引擎确认取消",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "retry-audio-import-engine-cancel"),
    )
    assert engine_retry.status_code == 202, engine_retry.text
    engine_retry_data = engine_retry.json()["data"]
    assert process_aggregate_events([engine_retry_data["run_id"]]) == 1
    engine_cancel = client.post(
        f"/api/v1/task-runs/{engine_retry_data['run_id']}/cancellations",
        json={"reason": "引擎确认取消导入"},
        headers=_write_headers(auth_headers, "cancel-audio-import-after-dispatch"),
    )
    assert engine_cancel.status_code == 202, engine_cancel.text
    engine_control_id = engine_cancel.json()["data"]["run_id"]
    assert process_aggregate_events([engine_control_id]) == 1
    with SessionLocal() as session:
        engine_cancelled_batch = session.get(
            ImportBatch,
            engine_retry_data["import_batch_id"],
        )
        assert engine_cancelled_batch is not None
        assert engine_cancelled_batch.status == "cancelled"
        assert engine_cancelled_batch.current_stage == "completed"
        assert engine_cancelled_batch.cursor_after is None

    deadline_retry = client.post(
        f"/api/v1/task-runs/{run_data['run_id']}/retries",
        json={
            "reason": "验证 deadline 本地取消",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "retry-audio-import-deadline"),
    )
    assert deadline_retry.status_code == 202, deadline_retry.text
    deadline_retry_data = deadline_retry.json()["data"]
    deadline_now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        deadline_run = session.get(RunRecord, deadline_retry_data["run_id"])
        assert deadline_run is not None
        deadline_run.deadline_at = deadline_now - timedelta(seconds=1)
    assert (
        monitor_task_runs_once(
            worker_id="audio-import-deadline-test",
            now=deadline_now,
        )
        == 1
    )
    with SessionLocal() as session:
        deadline_batch = session.get(
            ImportBatch,
            deadline_retry_data["import_batch_id"],
        )
        assert deadline_batch is not None
        assert deadline_batch.status == "cancelled"
        assert deadline_batch.current_stage == "completed"
        assert deadline_batch.cursor_after is None

    dead_letter_retry = client.post(
        f"/api/v1/task-runs/{run_data['run_id']}/retries",
        json={
            "reason": "验证分发 dead-letter",
            "execution_mode": "production",
        },
        headers=_write_headers(auth_headers, "retry-audio-import-dead-letter"),
    )
    assert dead_letter_retry.status_code == 202, dead_letter_retry.text
    dead_letter_retry_data = dead_letter_retry.json()["data"]
    with SessionLocal.begin() as session:
        requested_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_type == "task_run",
                OutboxEvent.aggregate_id == dead_letter_retry_data["run_id"],
                OutboxEvent.event_type == "task_run.requested",
            )
        )
        assert requested_event is not None
        requested_event.payload = {
            **requested_event.payload,
            "force_worker_error": True,
            "failure_reason": "audio import dispatch failed permanently",
            "max_attempts": 1,
        }
    assert process_aggregate_events([dead_letter_retry_data["run_id"]]) == 1
    with SessionLocal() as session:
        dead_letter_batch = session.get(
            ImportBatch,
            dead_letter_retry_data["import_batch_id"],
        )
        assert dead_letter_batch is not None
        assert dead_letter_batch.status == "failed"
        assert dead_letter_batch.current_stage == "completed"
        assert dead_letter_batch.cursor_after is None


def test_import_batch_reads_are_project_scoped_and_items_use_public_contract(
    client,
    auth_headers,
) -> None:
    with SessionLocal.begin() as session:
        run = RunRecord(
            run_id="task_run_import_read_contract",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            run_type="task_run",
            status="pending",
            run_key="read-contract",
            partition_key=None,
            trace_id="trace-import-read-contract",
            payload={},
        )
        session.add(run)
        session.flush()
        batch = ImportBatch(
            import_batch_id="import_batch_read_contract",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            task_run_id=run.run_id,
            task_version_id="task_version_audio_import_v1",
            connector_id="connector_audio_import",
            status="partial",
            current_stage="completed",
            total_items=2,
            succeeded_items=1,
            skipped_items=0,
            failed_items=1,
            cursor_before="cursor-1",
            cursor_after=None,
            root_trace_id="trace-import-read-contract",
            trace_id="trace-import-read-contract",
            payload={"private_manifest_url": "https://must-not-leak.example/manifest"},
        )
        session.add(batch)
        session.flush()
        session.add_all(
            [
                ImportBatchItem(
                    import_item_id="import_item_success",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    import_batch_id=batch.import_batch_id,
                    external_record_id="external-001",
                    status="succeeded",
                    error_code=None,
                    object_version="version-001",
                    audio_session_id="audio-session-001",
                    root_trace_id=batch.root_trace_id,
                    trace_id=batch.trace_id,
                    payload={"signed_url": "https://must-not-leak.example/audio"},
                ),
                ImportBatchItem(
                    import_item_id="import_item_failed",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    import_batch_id=batch.import_batch_id,
                    external_record_id="external-002",
                    status="failed",
                    error_code="SOURCE_URL_EXPIRED",
                    object_version=None,
                    audio_session_id=None,
                    root_trace_id=batch.root_trace_id,
                    trace_id=batch.trace_id,
                    payload={"internal_exception": "secret detail"},
                ),
            ]
        )

    detail = client.get("/api/v1/import-batches/import_batch_read_contract", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["current_stage"] == "completed"
    assert "private_manifest_url" not in detail.json()["data"]

    items = client.get(
        "/api/v1/import-batches/import_batch_read_contract/items",
        headers=auth_headers,
    )
    assert items.status_code == 200, items.text
    assert items.json()["data"]["items"] == [
        {
            "import_item_id": "import_item_success",
            "import_batch_id": "import_batch_read_contract",
            "external_record_id": "external-001",
            "status": "succeeded",
            "error_code": None,
            "object_version": "version-001",
            "audio_session_id": "audio-session-001",
            "root_trace_id": "trace-import-read-contract",
            "trace_id": "trace-import-read-contract",
        },
        {
            "import_item_id": "import_item_failed",
            "import_batch_id": "import_batch_read_contract",
            "external_record_id": "external-002",
            "status": "failed",
            "error_code": "SOURCE_URL_EXPIRED",
            "object_version": None,
            "audio_session_id": None,
            "root_trace_id": "trace-import-read-contract",
            "trace_id": "trace-import-read-contract",
        },
    ]
    serialized = json.dumps(items.json(), ensure_ascii=False)
    assert "signed_url" not in serialized
    assert "internal_exception" not in serialized

    missing_scope_headers = deepcopy(auth_headers)
    missing_scope_headers["X-Project-Id"] = "missing-project"
    hidden = client.get(
        "/api/v1/import-batches/import_batch_read_contract",
        headers=missing_scope_headers,
    )
    assert hidden.status_code in {403, 404}
