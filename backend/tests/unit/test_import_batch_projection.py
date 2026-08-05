from __future__ import annotations

import json

from app.models import ImportBatch, ImportBatchItem
from app.services.import_batch_service import (
    import_batch_item_payload,
    import_batch_payload,
)


def _batch(*, status: str, payload: dict[str, object]) -> ImportBatch:
    return ImportBatch(
        import_batch_id=f"import_batch_{status}",
        tenant_id="tenant_projection",
        project_id="project_projection",
        task_run_id=f"task_run_{status}",
        task_version_id="task_version_audio_import",
        connector_id="connector_audio_import",
        status=status,
        current_stage="completed",
        total_items=0,
        succeeded_items=0,
        skipped_items=0,
        failed_items=0,
        root_trace_id=f"trace_{status}",
        trace_id=f"trace_{status}",
        payload=payload,
    )


def test_partial_batch_without_items_has_public_retry_reason() -> None:
    projected = import_batch_payload(_batch(status="partial", payload={}))

    assert projected["error_code"] == "AUDIO_IMPORT_BATCH_PARTIAL"
    assert projected["reason"] == "导入批次部分失败；成功项已保留，可重试失败项。"
    assert projected["retryable"] is True
    assert projected["recovery_suggestion"]
    assert projected["retry_lineage"] == {
        "source_task_run_id": None,
        "source_import_batch_id": None,
        "root_task_run_id": "task_run_partial",
        "root_import_batch_id": "import_batch_partial",
        "attempt": 1,
    }


def test_batch_failure_projection_redacts_url_and_credential_material() -> None:
    projected = import_batch_payload(
        _batch(
            status="failed",
            payload={
                "error_code": "SECRET_PROJECTION_CANARY",
                "reason": (
                    "https://private.example.test/audio?id=secret "
                    "Authorization: Bearer projection-secret-canary"
                ),
            },
        )
    )

    assert projected["error_code"] == "AUDIO_IMPORT_BATCH_FAILED"
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "SECRET_PROJECTION_CANARY" not in serialized
    assert "private.example.test" not in serialized
    assert "projection-secret-canary" not in serialized
    assert "Authorization" not in serialized
    assert "Bearer" not in serialized


def test_import_item_failure_projection_uses_safe_code_recovery_and_lineage() -> None:
    item = ImportBatchItem(
        import_item_id="import_item_retry_002",
        tenant_id="tenant_projection",
        project_id="project_projection",
        import_batch_id="import_batch_retry_002",
        external_record_id="recording-safe-002",
        status="failed",
        error_code="INTERNAL_S3_EXCEPTION_WITH_BUCKET",
        root_trace_id="trace_retry_002",
        trace_id="trace_retry_002",
        payload={
            "retry_lineage": {
                "source_import_batch_id": "import_batch_original",
                "source_import_item_id": "import_item_original",
                "root_import_batch_id": "import_batch_original",
                "root_import_item_id": "import_item_original",
                "attempt": 2,
            }
        },
    )

    projected = import_batch_item_payload(item)

    assert projected["error_code"] == "AUDIO_IMPORT_ITEM_FAILED"
    assert projected["retryable"] is False
    assert projected["recovery_suggestion"]
    assert projected["retry_lineage"] == {
        "source_import_batch_id": "import_batch_original",
        "source_import_item_id": "import_item_original",
        "root_import_batch_id": "import_batch_original",
        "root_import_item_id": "import_item_original",
        "attempt": 2,
    }
    assert "INTERNAL_S3" not in json.dumps(projected)
