from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.completion_signature import completion_signature_message
from app.core.database import SessionLocal
from app.models import AuditLog, ImportBatch, RunRecord

TEST_COMPLETION_SECRET = "auris-test-completion-secret-32chars-minimum"
TEST_COMPLETION_KEY_ID = "auris-test-completion"
TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
RUN_ID = "task_run_audio_progress"
BATCH_ID = "import_batch_audio_progress"
EXTERNAL_RUN_ID = "dagster_audio_progress"


def _seed_audio_import(
    *,
    stage: str = "listing",
    status: str = "running",
    run_status: str = "submitted",
    with_dispatch: bool = True,
) -> None:
    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(
            RunRecord(
                run_id=RUN_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="task_run",
                status=run_status,
                run_key="audio-progress",
                trace_id="trace_audio_progress",
                submitted_at=now,
                started_at=now,
                payload={
                    "execution_contract": "auris-flow-audio-import-v1",
                    "import_batch_id": BATCH_ID,
                    "root_trace_id": "trace_audio_progress",
                    "business_completion_required": True,
                    **(
                        {
                            "dispatch": {
                                "adapter": "dagster",
                                "details": {
                                    "external_run_id": EXTERNAL_RUN_ID,
                                    "dagster_run_id": EXTERNAL_RUN_ID,
                                },
                            }
                        }
                        if with_dispatch
                        else {}
                    ),
                },
            )
        )
        session.flush()
        session.add(
            ImportBatch(
                import_batch_id=BATCH_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                task_run_id=RUN_ID,
                task_version_id="task_version_audio_progress",
                connector_id="connector_audio_progress",
                status=status,
                current_stage=stage,
                root_trace_id="trace_audio_progress",
                trace_id="trace_audio_progress",
                started_at=now,
                payload={},
            )
        )
        session.commit()


def _payload(
    stage: str,
    *,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    task_run_id: str = RUN_ID,
    import_batch_id: str = BATCH_ID,
    external_id: str = EXTERNAL_RUN_ID,
    receipt_id: str | None = None,
) -> dict[str, str]:
    return {
        "progress_receipt_id": receipt_id or f"dagster:{external_id}:{stage}",
        "adapter": "dagster",
        "source": "dagster",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "task_run_id": task_run_id,
        "import_batch_id": import_batch_id,
        "external_id": external_id,
        "stage": stage,
    }


def _signed_headers(
    *,
    path: str,
    payload: dict[str, object],
    idempotency_key: str,
    nonce: str,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> dict[str, str]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = datetime.now(UTC).isoformat()
    body_sha256 = hashlib.sha256(body).hexdigest()
    message = completion_signature_message(
        method="POST",
        path=path,
        query="",
        tenant_id=tenant_id,
        project_id=project_id,
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
        "X-Tenant-Id": tenant_id,
        "X-Project-Id": project_id,
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


def _post_progress(
    client,
    stage: str,
    *,
    key: str,
    payload: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
):
    path = f"/api/v1/runs/{RUN_ID}/external-progress-receipts"
    body = payload or _payload(stage)
    signed = headers or _signed_headers(
        path=path,
        payload=body,
        idempotency_key=key,
        nonce=f"nonce-{key}",
    )
    return client.post(
        path,
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        headers=signed,
    )


def test_signed_audio_import_progress_advances_monotonically_and_replays_idempotently(
    client,
) -> None:
    _seed_audio_import()
    payload = _payload("downloading")
    path = f"/api/v1/runs/{RUN_ID}/external-progress-receipts"
    headers = _signed_headers(
        path=path,
        payload=payload,
        idempotency_key="progress-downloading",
        nonce="nonce-progress-downloading",
    )

    first = _post_progress(
        client,
        "downloading",
        key="progress-downloading",
        payload=payload,
        headers=headers,
    )
    replay = _post_progress(
        client,
        "downloading",
        key="progress-downloading",
        payload=payload,
        headers=headers,
    )
    verifying = _post_progress(client, "verifying", key="progress-verifying")

    assert first.status_code == 202, first.text
    assert first.json()["data"] == {
        "run_id": RUN_ID,
        "import_batch_id": BATCH_ID,
        "status": "running",
        "current_stage": "downloading",
        "applied": True,
        "root_trace_id": "trace_audio_progress",
    }
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()
    assert verifying.status_code == 202, verifying.text
    assert verifying.json()["data"]["current_stage"] == "verifying"
    assert verifying.json()["data"]["applied"] is True

    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.current_stage == "verifying"
        assert (
            batch.payload["external_progress"]["progress_receipt_id"]
            == f"dagster:{EXTERNAL_RUN_ID}:verifying"
        )
        assert batch.payload["external_progress"]["external_run_id"] == EXTERNAL_RUN_ID
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "audio_import.progress_received",
                    AuditLog.object_id == BATCH_ID,
                )
            )
            == 2
        )


@pytest.mark.parametrize(
    ("override", "expected_code"),
    [
        ({"tenant_id": "other_tenant"}, "AUDIO_IMPORT_PROGRESS_SCOPE_MISMATCH"),
        ({"project_id": "other_project"}, "AUDIO_IMPORT_PROGRESS_SCOPE_MISMATCH"),
        ({"task_run_id": "other_run"}, "AUDIO_IMPORT_PROGRESS_RUN_MISMATCH"),
        ({"import_batch_id": "other_batch"}, "AUDIO_IMPORT_PROGRESS_BATCH_MISMATCH"),
        ({"external_id": "other_dagster_run"}, "AUDIO_IMPORT_PROGRESS_EXTERNAL_ID_MISMATCH"),
        (
            {"receipt_id": "dagster:other_dagster_run:downloading"},
            "AUDIO_IMPORT_PROGRESS_RECEIPT_ID_MISMATCH",
        ),
    ],
)
def test_audio_import_progress_rejects_cross_binding_receipts(
    client,
    override: dict[str, str],
    expected_code: str,
) -> None:
    _seed_audio_import()
    payload = _payload("downloading", **override)

    response = _post_progress(
        client,
        "downloading",
        key=f"reject-{next(iter(override))}",
        payload=payload,
    )

    assert response.status_code in {403, 409}, response.text
    assert response.json()["error"]["code"] == expected_code
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.current_stage == "listing"


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [
        ("other_tenant", PROJECT_ID),
        (TENANT_ID, "other_project"),
    ],
)
def test_audio_import_progress_rejects_cross_scope_headers(
    client,
    tenant_id: str,
    project_id: str,
) -> None:
    _seed_audio_import()
    payload = _payload("downloading")
    path = f"/api/v1/runs/{RUN_ID}/external-progress-receipts"
    headers = _signed_headers(
        path=path,
        payload=payload,
        idempotency_key=f"cross-scope-{tenant_id}-{project_id}",
        nonce=f"nonce-cross-scope-{tenant_id}-{project_id}",
        tenant_id=tenant_id,
        project_id=project_id,
    )

    response = _post_progress(
        client,
        "downloading",
        key="ignored",
        payload=payload,
        headers=headers,
    )

    assert response.status_code in {403, 404}, response.text
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.current_stage == "listing"


def test_audio_import_progress_rejects_out_of_order_stage_without_mutation(client) -> None:
    _seed_audio_import()

    response = _post_progress(client, "verifying", key="out-of-order-verifying")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_IMPORT_PROGRESS_OUT_OF_ORDER"
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.current_stage == "listing"


def test_audio_import_progress_cannot_mutate_terminal_batch(client) -> None:
    _seed_audio_import(stage="completed", status="succeeded")

    response = _post_progress(client, "downloading", key="terminal-progress")

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "AUDIO_IMPORT_PROGRESS_TERMINAL"
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.status == "succeeded"
        assert batch.current_stage == "completed"


def test_audio_import_progress_schema_is_strict_and_signature_is_required(client) -> None:
    _seed_audio_import()
    payload = {**_payload("downloading"), "unexpected": "forbidden"}

    unsigned = client.post(
        f"/api/v1/runs/{RUN_ID}/external-progress-receipts",
        json=_payload("downloading"),
        headers={
            "X-Tenant-Id": TENANT_ID,
            "X-Project-Id": PROJECT_ID,
            "Idempotency-Key": "unsigned-progress",
        },
    )
    strict = _post_progress(
        client,
        "downloading",
        key="strict-progress",
        payload=payload,
    )

    assert unsigned.status_code == 401, unsigned.text
    assert unsigned.json()["error"]["code"] == "COMPLETION_SIGNATURE_REQUIRED"
    assert strict.status_code == 422, strict.text
    assert strict.json()["error"]["code"] == "VALIDATION_ERROR"


def test_signed_completion_staging_exposes_materializing_before_business_materialization(
    client,
) -> None:
    _seed_audio_import(
        stage="queued",
        status="queued",
        run_status="pending",
        with_dispatch=False,
    )
    path = f"/api/v1/runs/{RUN_ID}/external-completion-receipts"
    payload: dict[str, object] = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": "dagster:audio-progress:completion",
        "external_id": EXTERNAL_RUN_ID,
        "result_ref": {
            "import_batch_id": BATCH_ID,
            "manifest_storage_object_id": "storage_manifest_progress",
            "manifest_sha256": "a" * 64,
            "items": [],
            "storage_objects": [],
        },
        "metrics": {},
    }
    headers = _signed_headers(
        path=path,
        payload=payload,
        idempotency_key="stage-audio-completion",
        nonce="nonce-stage-audio-completion",
    )

    staged = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        headers=headers,
    )

    assert staged.status_code == 202, staged.text
    assert staged.json()["data"]["receipt_state"] == "pending_binding"
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.status == "running"
        assert batch.current_stage == "materializing"
        assert batch.payload["materialization"]["completion_receipt_id"] == (
            "dagster:audio-progress:completion"
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "audio_import.materialization_started",
                    AuditLog.object_id == BATCH_ID,
                )
            )
            == 1
        )


def test_signed_completion_staging_rejects_cross_batch_materialization(
    client,
) -> None:
    _seed_audio_import(
        stage="queued",
        status="queued",
        run_status="pending",
        with_dispatch=False,
    )
    path = f"/api/v1/runs/{RUN_ID}/external-completion-receipts"
    payload: dict[str, object] = {
        "adapter": "dagster",
        "source": "dagster",
        "status": "success",
        "completion_receipt_id": "dagster:audio-progress:wrong-batch",
        "external_id": EXTERNAL_RUN_ID,
        "result_ref": {
            "import_batch_id": "import_batch_other_project",
            "manifest_storage_object_id": "storage_manifest_progress",
            "manifest_sha256": "a" * 64,
            "items": [],
            "storage_objects": [],
        },
        "metrics": {},
    }
    headers = _signed_headers(
        path=path,
        payload=payload,
        idempotency_key="stage-audio-completion-wrong-batch",
        nonce="nonce-stage-audio-completion-wrong-batch",
    )

    rejected = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        headers=headers,
    )

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "AUDIO_IMPORT_COMPLETION_BATCH_MISMATCH"
    with SessionLocal() as session:
        batch = session.get(ImportBatch, BATCH_ID)
        assert batch is not None
        assert batch.status == "queued"
        assert batch.current_stage == "queued"
