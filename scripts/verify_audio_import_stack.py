#!/usr/bin/env python3
"""Fail-closed verifier for the real platform-audio import Compose gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SOURCE_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SCOPE = ("aurora_auto", "sales_qa")
PLATFORM_CONNECTION_ID = "conn_platform_auth"
TARGET_ASSET_KEY = "auris/audio/raw_recordings"
PLATFORM_ORIGIN = "https://recordings.audio-import-gate.test:8443"
PLATFORM_CREDENTIAL_REF = "secret://platform/audio-import-gate"
EXPECTED_JOB_NAME = "auris_flow_audio_import_v1"
EXPECTED_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
EXPECTED_EXTERNAL_IDS = [
    "audio-import-gate-001",
    "audio-import-gate-002",
    "audio-import-gate-003",
]
TERMINAL_RUN_STATUSES = frozenset({"success", "failed", "cancelled"})
TERMINAL_BATCH_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})
TERMINAL_DAGSTER_STATUSES = frozenset({"SUCCESS", "FAILURE", "CANCELED", "CANCELING"})


class GateFailure(RuntimeError):
    """A sanitized gate failure that does not include remote bodies or secrets."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateFailure(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateFailure(f"{label} is missing")
    return value


def _items(response: dict[str, Any], label: str) -> list[dict[str, Any]]:
    data = _mapping(response.get("data"), f"{label} data")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or any(
        not isinstance(item, dict) for item in raw_items
    ):
        raise GateFailure(f"{label} items are invalid")
    return raw_items


def _order_by_expected_external_identity(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    actual = [item.get("external_record_id") for item in items]
    if len(actual) != len(EXPECTED_EXTERNAL_IDS) or set(actual) != set(
        EXPECTED_EXTERNAL_IDS
    ):
        return None
    by_external_id = {str(item["external_record_id"]): item for item in items}
    return [by_external_id[external_id] for external_id in EXPECTED_EXTERNAL_IDS]


class BFFClient:
    def __init__(self, base_url: str, *, deadline: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.deadline = deadline

    def _timeout(self, label: str, maximum: float = 10.0) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GateFailure(f"gate deadline exhausted during {label}")
        return min(remaining, maximum)

    def json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expected_status: int = 200,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Authorization": "Bearer dev-token",
            "X-Tenant-Id": SCOPE[0],
            "X-Project-Id": SCOPE[1],
            "X-Request-Id": f"audio-import-gate-{secrets.token_hex(8)}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(
                request,
                timeout=self._timeout(f"{method} {path}"),
            ) as response:
                status = int(response.status)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = int(exc.code)
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, URLError) as exc:
            raise GateFailure(f"BFF request failed during {method} {path}") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GateFailure(f"BFF response exceeded limit during {method} {path}")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateFailure(
                f"BFF returned invalid JSON during {method} {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise GateFailure(f"BFF envelope is invalid during {method} {path}")
        if status != expected_status:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else "UNKNOWN"
            raise GateFailure(
                f"BFF returned HTTP {status} ({code}) during {method} {path}"
            )
        return payload

    def audio(
        self,
        playback_path: str,
        *,
        range_header: str | None = None,
        expected_status: int,
    ) -> tuple[dict[str, str], bytes]:
        if not playback_path.startswith("/api/v1/audio-playback?grant="):
            raise GateFailure("playback path is not a scoped BFF grant URL")
        headers = {
            "Accept": "audio/wav",
            "X-Request-Id": f"audio-import-playback-{secrets.token_hex(8)}",
        }
        if range_header is not None:
            headers["Range"] = range_header
        request = Request(
            f"{self.base_url}{playback_path}",
            method="GET",
            headers=headers,
        )
        try:
            with urlopen(
                request,
                timeout=self._timeout("audio playback"),
            ) as response:
                status = int(response.status)
                body = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {
                    str(name).casefold(): str(value)
                    for name, value in response.headers.items()
                }
        except HTTPError as exc:
            status = int(exc.code)
            body = exc.read(MAX_RESPONSE_BYTES + 1)
            response_headers = {
                str(name).casefold(): str(value) for name, value in exc.headers.items()
            }
        except (OSError, TimeoutError, URLError) as exc:
            raise GateFailure("BFF audio playback failed") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise GateFailure("BFF audio playback response exceeded limit")
        if status != expected_status:
            raise GateFailure(f"BFF audio playback returned HTTP {status}")
        return response_headers, body


def _graphql(
    *,
    query: str,
    variables: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(
        {"query": query, "variables": variables},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        "http://dagster-webserver:3000/graphql",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, OSError, TimeoutError, URLError) as exc:
        raise GateFailure("Dagster GraphQL request failed") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise GateFailure("Dagster GraphQL response exceeded limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GateFailure("Dagster GraphQL response is invalid") from exc
    if not isinstance(payload, dict) or payload.get("errors"):
        raise GateFailure("Dagster GraphQL returned errors")
    return payload


RUNS_BY_BUSINESS_RUN_QUERY = """
query AudioImportGateRuns($filter: RunsFilter!) {
  runsOrError(filter: $filter, limit: 2) {
    __typename
    ... on Runs {
      results {
        runId
        status
        pipelineName
        tags {
          key
          value
        }
      }
    }
    ... on PythonError {
      message
    }
  }
}
""".strip()


def _dagster_run_for_business_run(
    business_run_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    last_status = "missing"
    while time.monotonic() < deadline:
        payload = _graphql(
            query=RUNS_BY_BUSINESS_RUN_QUERY,
            variables={
                "filter": {
                    "tags": [{"key": "run_id", "value": business_run_id}],
                }
            },
            timeout_seconds=min(max(deadline - time.monotonic(), 0.1), 10.0),
        )
        data = _mapping(payload.get("data"), "Dagster GraphQL data")
        result = _mapping(data.get("runsOrError"), "Dagster runs result")
        runs = result.get("results")
        if result.get("__typename") != "Runs" or not isinstance(runs, list):
            raise GateFailure("Dagster runs query returned an invalid result")
        if len(runs) > 1:
            raise GateFailure("business TaskRun dispatched more than one Dagster run")
        if runs:
            run = _mapping(runs[0], "Dagster run")
            last_status = str(run.get("status") or "unknown")
            if last_status == "SUCCESS":
                return run
            if last_status in TERMINAL_DAGSTER_STATUSES:
                raise GateFailure(
                    f"audio import Dagster run reached terminal state {last_status}"
                )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise GateFailure(
        f"timed out waiting for successful Dagster run; last_status={last_status}"
    )


def _wait_for_batch(
    client: BFFClient,
    batch_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    last_status = "unknown"
    last_stage = "unknown"
    while time.monotonic() < deadline:
        response = client.json("GET", f"/api/v1/import-batches/{batch_id}")
        batch = _mapping(response.get("data"), "import batch")
        last_status = str(batch.get("status") or "unknown")
        last_stage = str(batch.get("current_stage") or "unknown")
        if last_status == "succeeded" and last_stage == "completed":
            return batch
        if last_status in TERMINAL_BATCH_STATUSES:
            items = _items(
                client.json("GET", f"/api/v1/import-batches/{batch_id}/items"),
                "terminal import batch",
            )
            error_codes = sorted(
                {
                    str(item["error_code"])
                    for item in items
                    if item.get("error_code") is not None
                }
            )
            raise GateFailure(
                f"import batch reached {last_status}/{last_stage}; "
                f"error_codes={error_codes}"
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise GateFailure(
        "timed out waiting for import batch; "
        f"last_status={last_status}, last_stage={last_stage}"
    )


def _wait_for_task_run(
    client: BFFClient,
    run_id: str,
    *,
    deadline: float,
) -> dict[str, Any]:
    last_status = "unknown"
    while time.monotonic() < deadline:
        response = client.json("GET", f"/api/v1/task-runs/{run_id}")
        run = _mapping(response.get("data"), "TaskRun")
        last_status = str(run.get("status") or "unknown")
        if last_status == "success":
            return run
        if last_status in TERMINAL_RUN_STATUSES:
            raise GateFailure(f"TaskRun reached terminal state {last_status}")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise GateFailure(f"timed out waiting for TaskRun; last_status={last_status}")


def _database_proof(
    *,
    run_id: str,
    root_trace_id: str,
    expected_item_versions: set[str],
) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import OutboxEvent, RunCompletionReceipt, RunRecord, StorageObject

    with SessionLocal() as session:
        run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == run_id,
                RunRecord.tenant_id == SCOPE[0],
                RunRecord.project_id == SCOPE[1],
            )
        )
        receipt = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.run_id == run_id,
                RunCompletionReceipt.tenant_id == SCOPE[0],
                RunCompletionReceipt.project_id == SCOPE[1],
            )
        )
        materialization_event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_type == "execution.materialization.requested",
                OutboxEvent.aggregate_id == run_id,
                OutboxEvent.tenant_id == SCOPE[0],
                OutboxEvent.project_id == SCOPE[1],
            )
        )
        objects = list(
            session.scalars(
                select(StorageObject)
                .where(
                    StorageObject.tenant_id == SCOPE[0],
                    StorageObject.project_id == SCOPE[1],
                    StorageObject.source_type == "task_run",
                    StorageObject.source_id == run_id,
                )
                .order_by(StorageObject.storage_object_id)
            )
        )
    if run is None or receipt is None or materialization_event is None:
        raise GateFailure(
            "TaskRun, signed completion receipt, or materialization boundary proof is missing"
        )
    payload = _mapping(run.payload, "persisted TaskRun payload")
    dispatch = _mapping(payload.get("dispatch"), "persisted Dagster dispatch")
    details = _mapping(dispatch.get("details"), "persisted Dagster dispatch details")
    dagster_run_id = _text(details.get("external_run_id"), "persisted Dagster run id")
    if (
        run.run_type != "task_run"
        or run.status != "success"
        or run.trace_id != root_trace_id
        or payload.get("execution_contract") != EXPECTED_EXECUTION_CONTRACT
        or dispatch.get("adapter") != "dagster"
        or dispatch.get("operation") != "run_request"
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
        or details.get("job_name") != EXPECTED_JOB_NAME
    ):
        raise GateFailure("persisted real Dagster dispatch proof is invalid")
    if (
        receipt.processing_state != "completed"
        or receipt.completion_status != "success"
        or receipt.adapter != "dagster"
        or receipt.source != "dagster"
        or receipt.authenticated_source != "dagster"
        or receipt.signature_key_id != "dagster-v1"
        or receipt.signature_mode != "hmac-sha256"
        or receipt.external_id != dagster_run_id
        or receipt.run_trace_id != root_trace_id
        or not receipt.signature_nonce
        or not SHA256_PATTERN.fullmatch(str(receipt.signature_body_hash or ""))
        or receipt.completed_at is None
    ):
        raise GateFailure("signed Dagster completion receipt proof is invalid")
    history = payload.get("status_history")
    transitions = {
        (str(item.get("from")), str(item.get("to")))
        for item in (history if isinstance(history, list) else [])
        if isinstance(item, dict)
    }
    materialization = _mapping(
        payload.get("materialization"),
        "persisted asynchronous materialization state",
    )
    if (
        ("submitted", "completion_pending") not in transitions
        and ("running", "completion_pending") not in transitions
    ) or (
        "completion_pending",
        "success",
    ) not in transitions:
        raise GateFailure(
            "TaskRun did not preserve the completion_pending business window"
        )
    if (
        payload.get("completion_mode") != "async_materialization"
        or materialization.get("status") != "completed"
        or materialization_event.status != "processed"
        or materialization_event.delivery_state != "confirmed"
        or materialization_event.processed_at is None
        or str(materialization_event.payload.get("completion_receipt_id"))
        != receipt.completion_receipt_id
    ):
        raise GateFailure("asynchronous materialization outbox proof is invalid")
    if len(objects) != 4:
        raise GateFailure(
            "audio import did not register three WAV objects and one manifest"
        )
    roles: dict[str, int] = {}
    raw_audio_versions: set[str] = set()
    object_evidence: list[dict[str, Any]] = []
    for storage_object in objects:
        object_payload = _mapping(storage_object.payload, "StorageObject payload")
        role = _text(object_payload.get("role"), "StorageObject role")
        version_id = _text(
            object_payload.get("object_version_id"),
            "StorageObject exact version",
        )
        roles[role] = roles.get(role, 0) + 1
        if (
            storage_object.provider != "minio"
            or storage_object.bucket != "auris-flow"
            or storage_object.status != "verified"
            or storage_object.trace_id != root_trace_id
            or version_id.casefold() == "null"
            or not storage_object.etag
            or not SHA256_PATTERN.fullmatch(str(storage_object.content_sha256 or ""))
        ):
            raise GateFailure("registered MinIO StorageObject proof is invalid")
        if role == "raw_audio":
            raw_audio_versions.add(version_id)
        object_evidence.append(
            {
                "storage_object_id": storage_object.storage_object_id,
                "role": role,
                "provider": storage_object.provider,
                "content_sha256": storage_object.content_sha256,
                "object_version_id": version_id,
                "etag": storage_object.etag,
            }
        )
    if roles != {"manifest": 1, "raw_audio": 3}:
        raise GateFailure("registered MinIO object roles are incomplete")
    if raw_audio_versions != expected_item_versions:
        raise GateFailure(
            "batch item versions do not bind the registered MinIO WAV versions"
        )
    return {
        "dagster_run_id": dagster_run_id,
        "signed_completion": {
            "receipt_id": receipt.completion_receipt_id,
            "source": receipt.authenticated_source,
            "signature_mode": receipt.signature_mode,
            "key_id": receipt.signature_key_id,
            "processing_state": receipt.processing_state,
        },
        "materialization_boundary": {
            "event_id": materialization_event.event_id,
            "event_type": materialization_event.event_type,
            "delivery_state": materialization_event.delivery_state,
            "status": materialization.get("status"),
            "status_transitions": [
                {"from": source, "to": target}
                for source, target in sorted(transitions)
                if target in {"completion_pending", "success"}
            ],
        },
        "storage_objects": object_evidence,
    }


def _fixture_wav(index: int) -> bytes:
    fixture_root = Path("/opt/auris-gate")
    if fixture_root.is_dir():
        sys.path.insert(0, str(fixture_root))
    try:
        from audio_import_platform import fixture_wav_bytes
    except ImportError as exc:
        raise GateFailure("shared audio fixture implementation is unavailable") from exc
    return fixture_wav_bytes(index)


def run_gate(
    *,
    base_url: str,
    source_commit: str,
    source_tree_dirty: bool,
    timeout_seconds: float,
    run_suffix: str,
    artifact: Path,
) -> dict[str, Any]:
    if not SOURCE_COMMIT_PATTERN.fullmatch(source_commit):
        raise GateFailure("source commit must be an exact lowercase Git SHA")
    deadline = time.monotonic() + timeout_seconds
    client = BFFClient(base_url, deadline=deadline)
    client.json("GET", "/readyz")

    suffix_digest = hashlib.sha256(run_suffix.encode("utf-8")).hexdigest()[:12]
    connector_id = f"audio_import_gate_{suffix_digest}"
    task_version_id = f"audio_import_gate_task_{suffix_digest}"
    connector_body = {
        "connector_id": connector_id,
        "name": "真实栈音频导入门禁",
        "source_type": "platform_audio_url_api",
        "platform_connection_id": PLATFORM_CONNECTION_ID,
        "credential_ref": PLATFORM_CREDENTIAL_REF,
        "base_url": PLATFORM_ORIGIN,
        "request_path": "/v1/recordings",
        "platform_scope": {
            "tenant_ref": "audio-import-gate-tenant",
            "store_refs": ["BJ-AURORA-001"],
        },
        "pagination": {
            "mode": "cursor",
            "page_size": 2,
            "cursor_param": "cursor",
            "next_cursor_path": "next_cursor",
        },
        "field_mapping": {
            "external_record_id": "recording_id",
            "audio_url": "download_url",
            "started_at": "started_at",
            "duration_ms": "duration_ms",
            "store_ref": "store_id",
            "agent_ref": "employee.badge",
            "device_ref": "device_id",
        },
        "cursor_policy": {
            "field": "updated_at",
            "initial_window_start": "2026-07-27T00:00:00+00:00",
        },
        "target_asset_key": TARGET_ASSET_KEY,
        "dedupe_policy": "external_id_checksum",
        "status": "active",
    }
    created_connector = _mapping(
        client.json(
            "POST",
            "/api/v1/connectors",
            body=connector_body,
            idempotency_key=f"audio-import-gate:{suffix_digest}:connector",
            expected_status=201,
        ).get("data"),
        "created connector",
    )
    if (
        created_connector.get("connector_id") != connector_id
        or created_connector.get("credential_ref") != PLATFORM_CREDENTIAL_REF
        or created_connector.get("connector_version") != 1
    ):
        raise GateFailure("created connector did not preserve the strong contract")

    connection_test = _mapping(
        client.json(
            "POST",
            f"/api/v1/connectors/{connector_id}/connection-tests",
            body={},
            idempotency_key=f"audio-import-gate:{suffix_digest}:connection-test",
        ).get("data"),
        "connection test",
    )
    if (
        connection_test.get("status") != "success"
        or connection_test.get("response_status") != 200
    ):
        raise GateFailure("real HTTPS connector test did not succeed")
    preview = _mapping(
        client.json(
            "POST",
            f"/api/v1/connectors/{connector_id}/record-previews",
            body={"limit": 3},
            idempotency_key=f"audio-import-gate:{suffix_digest}:preview",
        ).get("data"),
        "record preview",
    )
    if (
        preview.get("status") != "success"
        or preview.get("record_count") != 3
        or preview.get("mapping_valid") is not True
        or preview.get("mapping_errors") != []
    ):
        raise GateFailure("real HTTPS record preview did not validate three records")
    preview_records = preview.get("records")
    if (
        not isinstance(preview_records, list)
        or [
            record.get("recording_id") if isinstance(record, dict) else None
            for record in preview_records
        ]
        != EXPECTED_EXTERNAL_IDS
    ):
        raise GateFailure(
            "record preview did not return the expected platform identities"
        )
    if any(
        not isinstance(record, dict) or record.get("download_url") is not True
        for record in preview_records
    ):
        raise GateFailure(
            "record preview exposed or lost the redacted audio URL marker"
        )

    task_version = _mapping(
        client.json(
            "POST",
            "/api/v1/task-versions",
            body={
                "task_version_id": task_version_id,
                "task_type_id": "audio-platform-import",
                "version": "v1",
                "connector_id": connector_id,
            },
            idempotency_key=f"audio-import-gate:{suffix_digest}:task-version",
            expected_status=201,
        ).get("data"),
        "created TaskVersion",
    )
    if task_version.get("status") != "draft":
        raise GateFailure("audio import TaskVersion was not created as a draft")
    published = _mapping(
        client.json(
            "POST",
            f"/api/v1/task-versions/{task_version_id}/publish",
            body={"reason": "真实栈音频导入验收"},
            idempotency_key=f"audio-import-gate:{suffix_digest}:publish",
            expected_status=202,
        ).get("data"),
        "published TaskVersion",
    )
    connector_snapshot = _mapping(
        published.get("connector_snapshot"),
        "published connector snapshot",
    )
    if (
        published.get("status") != "published"
        or published.get("execution_contract") != EXPECTED_EXECUTION_CONTRACT
        or connector_snapshot.get("connector_id") != connector_id
        or connector_snapshot.get("connector_version") != "1"
        or connector_snapshot.get("base_url") != PLATFORM_ORIGIN
    ):
        raise GateFailure("published TaskVersion did not freeze the tested connector")

    run_idempotency_key = f"audio-import-gate:{suffix_digest}:run"
    run_response = client.json(
        "POST",
        "/api/v1/task-runs",
        body={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
            "partition_key": f"audio-import-gate/{suffix_digest}",
        },
        idempotency_key=run_idempotency_key,
        expected_status=202,
    )
    replay_response = client.json(
        "POST",
        "/api/v1/task-runs",
        body={
            "task_version_id": task_version_id,
            "trigger_type": "manual",
            "execution_mode": "production",
            "partition_key": f"audio-import-gate/{suffix_digest}",
        },
        idempotency_key=run_idempotency_key,
        expected_status=202,
    )
    if replay_response != run_response:
        raise GateFailure("production TaskRun idempotency replay changed the batch")
    run_created = _mapping(run_response.get("data"), "created TaskRun")
    run_id = _text(run_created.get("run_id"), "TaskRun id")
    batch_id = _text(run_created.get("import_batch_id"), "ImportBatch id")
    root_trace_id = _text(run_created.get("root_trace_id"), "root trace id")
    if run_created.get("execution_mode") != "production":
        raise GateFailure("audio import TaskRun did not use production mode")

    batch = _wait_for_batch(client, batch_id, deadline=deadline)
    task_run = _wait_for_task_run(client, run_id, deadline=deadline)
    if (
        batch.get("task_run_id") != run_id
        or batch.get("root_trace_id") != root_trace_id
        or batch.get("total_items") != 3
        or batch.get("succeeded_items") != 3
        or batch.get("skipped_items") != 0
        or batch.get("failed_items") != 0
        or not batch.get("cursor_after")
        or task_run.get("trace_id") != root_trace_id
    ):
        raise GateFailure(
            "materialized import batch statistics or trace binding is invalid"
        )

    batch_items = _items(
        client.json("GET", f"/api/v1/import-batches/{batch_id}/items"),
        "import batch",
    )
    ordered_batch_items = _order_by_expected_external_identity(batch_items)
    if ordered_batch_items is None:
        raise GateFailure(
            "ImportBatch items do not preserve external recording identities"
        )
    batch_items = ordered_batch_items
    if any(
        item.get("status") != "succeeded"
        or item.get("error_code") is not None
        or not item.get("audio_session_id")
        or not item.get("object_version")
        or str(item.get("object_version")).casefold() == "null"
        or item.get("root_trace_id") != root_trace_id
        for item in batch_items
    ):
        raise GateFailure("ImportBatch item materialization proof is invalid")
    item_versions = {str(item["object_version"]) for item in batch_items}
    if len(item_versions) != 3:
        raise GateFailure("each imported WAV must bind an exact MinIO object version")

    listed_sessions = _items(
        client.json("GET", "/api/v1/audio-sessions?limit=200"),
        "audio sessions",
    )
    listed_session_ids = {
        str(item.get("audio_session_id") or item.get("id") or "")
        for item in listed_sessions
    }
    expected_session_ids = {str(item["audio_session_id"]) for item in batch_items}
    if not expected_session_ids.issubset(listed_session_ids):
        raise GateFailure("new audio sessions are absent from the BFF session list")

    playback_evidence: list[dict[str, Any]] = []
    for index, item in enumerate(batch_items, start=1):
        session_id = str(item["audio_session_id"])
        session_data = _mapping(
            client.json("GET", f"/api/v1/audio-sessions/{session_id}").get("data"),
            "audio session",
        )
        if (
            session_data.get("audio_session_id") != session_id
            or session_data.get("external_record_id") != item["external_record_id"]
            or session_data.get("import_batch_id") != batch_id
            or session_data.get("platform_connection_id") != PLATFORM_CONNECTION_ID
            or session_data.get("root_trace_id") != root_trace_id
            or session_data.get("source") != "platform_audio_import"
        ):
            raise GateFailure("new audio session lineage is invalid")
        grant = _mapping(
            client.json(
                "POST",
                f"/api/v1/audio-sessions/{session_id}/playback-grants",
                body={},
                idempotency_key=(f"audio-import-gate:{suffix_digest}:playback:{index}"),
                expected_status=201,
            ).get("data"),
            "playback grant",
        )
        playback_path = _text(grant.get("playback_url"), "playback URL")
        full_headers, full_body = client.audio(
            playback_path,
            expected_status=200,
        )
        expected_body = _fixture_wav(index)
        if (
            full_body != expected_body
            or full_headers.get("content-type", "").split(";", 1)[0] != "audio/wav"
            or full_headers.get("x-storage-provider") != "minio"
            or full_headers.get("x-storage-object-id") != grant.get("storage_object_id")
            or not full_headers.get("etag")
        ):
            raise GateFailure("BFF did not replay the exact imported WAV from MinIO")
        range_headers, range_body = client.audio(
            playback_path,
            range_header="bytes=0-63",
            expected_status=206,
        )
        if (
            range_body != expected_body[:64]
            or range_headers.get("content-range") != f"bytes 0-63/{len(expected_body)}"
            or range_headers.get("accept-ranges") != "bytes"
        ):
            raise GateFailure(
                "exact-version MinIO playback Range semantics are invalid"
            )
        playback_evidence.append(
            {
                "audio_session_id": session_id,
                "external_record_id": item["external_record_id"],
                "storage_object_id": grant.get("storage_object_id"),
                "content_sha256": hashlib.sha256(full_body).hexdigest(),
                "content_length": len(full_body),
                "range_verified": True,
            }
        )

    database_proof = _database_proof(
        run_id=run_id,
        root_trace_id=root_trace_id,
        expected_item_versions=item_versions,
    )
    dagster_run = _dagster_run_for_business_run(run_id, deadline=deadline)
    dagster_tags = {
        str(tag.get("key")): str(tag.get("value"))
        for tag in dagster_run.get("tags", [])
        if isinstance(tag, dict)
    }
    if (
        dagster_run.get("runId") != database_proof["dagster_run_id"]
        or dagster_run.get("pipelineName") != EXPECTED_JOB_NAME
        or dagster_tags.get("run_id") != run_id
        or dagster_tags.get("tenant_id") != SCOPE[0]
        or dagster_tags.get("project_id") != SCOPE[1]
        or dagster_tags.get("auris/execution_contract") != EXPECTED_EXECUTION_CONTRACT
        or not SHA256_PATTERN.fullmatch(
            dagster_tags.get("auris/execution_envelope_sha256", "")
        )
    ):
        raise GateFailure("Dagster fixed-job execution or scope tags are invalid")

    evidence = {
        "schema_version": "auris.audio-import-real-stack-gate.v1",
        "status": "ok",
        "source_commit": source_commit,
        "source_tree_dirty": source_tree_dirty,
        "verified_at": datetime.now(UTC).isoformat(),
        "execution_environment": "compose",
        "scope": {"tenant_id": SCOPE[0], "project_id": SCOPE[1]},
        "adapters": {
            "dagster": "real",
            "object_storage": "real",
            "platform_source": "https",
        },
        "connector": {
            "connector_id": connector_id,
            "connector_version": "1",
            "connection_test": "success",
            "preview_count": 3,
            "mapping_valid": True,
        },
        "task_version": {
            "task_version_id": task_version_id,
            "status": "published",
            "execution_contract": EXPECTED_EXECUTION_CONTRACT,
        },
        "task_run": {
            "run_id": run_id,
            "status": task_run.get("status"),
            "execution_mode": "production",
            "root_trace_id": root_trace_id,
            "dagster_run_id": dagster_run.get("runId"),
            "dagster_job_name": dagster_run.get("pipelineName"),
            "dagster_status": dagster_run.get("status"),
        },
        "import_batch": {
            "import_batch_id": batch_id,
            "status": batch.get("status"),
            "current_stage": batch.get("current_stage"),
            "total": batch.get("total_items"),
            "succeeded": batch.get("succeeded_items"),
            "skipped": batch.get("skipped_items"),
            "failed": batch.get("failed_items"),
            "cursor_after": batch.get("cursor_after"),
            "items": [
                {
                    "external_record_id": item["external_record_id"],
                    "status": item["status"],
                    "object_version": item["object_version"],
                    "audio_session_id": item["audio_session_id"],
                }
                for item in batch_items
            ],
        },
        "signed_completion": database_proof["signed_completion"],
        "materialization_boundary": database_proof["materialization_boundary"],
        "minio_objects": database_proof["storage_objects"],
        "playback": playback_evidence,
    }
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact.with_suffix(f"{artifact.suffix}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(artifact)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument(
        "--source-tree-dirty",
        choices=("true", "false"),
        required=True,
    )
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--run-suffix", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        evidence = run_gate(
            base_url=args.base_url,
            source_commit=args.source_commit,
            source_tree_dirty=args.source_tree_dirty == "true",
            timeout_seconds=args.timeout_seconds,
            run_suffix=args.run_suffix,
            artifact=args.artifact,
        )
    except GateFailure as exc:
        print(f"audio import real-stack gate failed: {exc}", file=sys.stderr)
        return 1
    print(
        "audio import real-stack gate ok: "
        f"run_id={evidence['task_run']['run_id']}, "
        f"batch_id={evidence['import_batch']['import_batch_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
