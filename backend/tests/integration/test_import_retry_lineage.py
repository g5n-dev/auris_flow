from __future__ import annotations

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import ImportBatchItem, RunRecord
from app.services.import_batch_service import (
    create_import_batch_for_task_run,
    import_item_retry_lineage,
)


def _context(*, trace_id: str) -> RequestContext:
    return RequestContext(
        tenant_id="tenant_import_lineage",
        project_id="project_import_lineage",
        user_id="lineage_admin",
        roles=("project_admin",),
        request_id=f"request_{trace_id}",
        trace_id=trace_id,
    )


def _run(
    *,
    run_id: str,
    batch_id: str,
    trace_id: str,
    retry_of_run_id: str | None = None,
    retry_attempt: int | None = None,
    tenant_id: str = "tenant_import_lineage",
    project_id: str = "project_import_lineage",
) -> RunRecord:
    payload = {
        "execution_contract": "auris-flow-audio-import-v1",
        "import_batch_id": batch_id,
        "task_version_id": "task_version_import_lineage",
        "connector_snapshot": {
            "connector_id": "connector_import_lineage",
            "cursor_policy": {"cursor_value": "cursor-lineage"},
        },
        "root_trace_id": trace_id,
        "target": {"target_asset_key": "auris/audio/raw_recordings"},
    }
    if retry_of_run_id:
        payload["retry_of_run_id"] = retry_of_run_id
    if retry_attempt is not None:
        payload["retry_attempt"] = retry_attempt
    return RunRecord(
        run_id=run_id,
        tenant_id=tenant_id,
        project_id=project_id,
        run_type="task_run",
        status="pending",
        run_key=f"run-key:{run_id}",
        trace_id=trace_id,
        payload=payload,
    )


def test_retry_batch_and_item_lineage_follow_server_run_chain() -> None:
    with SessionLocal() as session:
        source_record = _run(
            run_id="task_run_lineage_source",
            batch_id="import_batch_lineage_source",
            trace_id="trace_lineage_source",
        )
        session.add(source_record)
        session.flush()
        source_batch = create_import_batch_for_task_run(
            session,
            _context(trace_id="trace_lineage_source"),
            source_record,
        )
        assert source_batch is not None
        source_item = ImportBatchItem(
            import_item_id="import_item_lineage_source",
            tenant_id=source_record.tenant_id,
            project_id=source_record.project_id,
            import_batch_id=source_batch.import_batch_id,
            external_record_id="recording-lineage-001",
            status="failed",
            error_code="AUDIO_DOWNLOAD_FAILED",
            root_trace_id=source_batch.root_trace_id,
            trace_id=source_record.trace_id,
            payload={
                "retry_lineage": {
                    "source_import_batch_id": None,
                    "source_import_item_id": None,
                    "root_import_batch_id": source_batch.import_batch_id,
                    "root_import_item_id": "import_item_lineage_source",
                    "attempt": 1,
                }
            },
        )
        session.add(source_item)
        session.flush()

        retry_record = _run(
            run_id="task_run_lineage_retry",
            batch_id="import_batch_lineage_retry",
            trace_id="trace_lineage_retry",
            retry_of_run_id=source_record.run_id,
            retry_attempt=1,
        )
        session.add(retry_record)
        session.flush()
        retry_batch = create_import_batch_for_task_run(
            session,
            _context(trace_id="trace_lineage_retry"),
            retry_record,
        )
        assert retry_batch is not None
        assert retry_batch.payload["retry_lineage"] == {
            "source_task_run_id": source_record.run_id,
            "source_import_batch_id": source_batch.import_batch_id,
            "root_task_run_id": source_record.run_id,
            "root_import_batch_id": source_batch.import_batch_id,
            "attempt": 2,
        }

        item_lineage = import_item_retry_lineage(
            session,
            record=retry_record,
            batch=retry_batch,
            import_item_id="import_item_lineage_retry",
            external_record_id=source_item.external_record_id,
        )
        assert item_lineage == {
            "source_import_batch_id": source_batch.import_batch_id,
            "source_import_item_id": source_item.import_item_id,
            "root_import_batch_id": source_batch.import_batch_id,
            "root_import_item_id": source_item.import_item_id,
            "attempt": 2,
        }


def test_retry_lineage_rejects_cross_scope_source_run() -> None:
    with SessionLocal() as session:
        foreign_record = _run(
            run_id="task_run_lineage_foreign",
            batch_id="import_batch_lineage_foreign",
            trace_id="trace_lineage_foreign",
            tenant_id="tenant_other",
            project_id="project_other",
        )
        session.add(foreign_record)
        session.flush()
        retry_record = _run(
            run_id="task_run_lineage_forged_retry",
            batch_id="import_batch_lineage_forged_retry",
            trace_id="trace_lineage_forged_retry",
            retry_of_run_id=foreign_record.run_id,
            retry_attempt=1,
        )
        session.add(retry_record)
        session.flush()

        with pytest.raises(ApiError) as error:
            create_import_batch_for_task_run(
                session,
                _context(trace_id="trace_lineage_forged_retry"),
                retry_record,
            )

        assert error.value.code == "IMPORT_BATCH_RETRY_LINEAGE_INVALID"
