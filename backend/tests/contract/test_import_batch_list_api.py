from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.database import SessionLocal
from app.models import ImportBatch, ImportBatchItem, RunRecord


def _seed_batch(
    *,
    batch_id: str,
    created_at: datetime,
    connector_id: str,
    task_version_id: str,
    target_asset_key: str,
    status: str,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
) -> None:
    run_id = f"run_{batch_id}"
    with SessionLocal() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                run_type="task_run",
                status="success",
                run_key=f"import-list:{batch_id}",
                trace_id=f"trace_{batch_id}",
                payload={"root_trace_id": f"trace_{batch_id}"},
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.flush()
        session.add(
            ImportBatch(
                import_batch_id=batch_id,
                tenant_id=tenant_id,
                project_id=project_id,
                task_run_id=run_id,
                task_version_id=task_version_id,
                connector_id=connector_id,
                status=status,
                current_stage="completed"
                if status in {"succeeded", "partial", "failed"}
                else "queued",
                total_items=1,
                succeeded_items=1 if status == "succeeded" else 0,
                skipped_items=0,
                failed_items=1 if status in {"partial", "failed"} else 0,
                root_trace_id=f"trace_{batch_id}",
                trace_id=f"trace_{batch_id}",
                payload={"target_asset_key": target_asset_key},
                created_at=created_at,
                updated_at=created_at,
            )
        )
        session.commit()


def test_import_batch_list_is_server_authoritative_filtered_and_newest_first(
    client,
    auth_headers,
) -> None:
    now = datetime.now(UTC)
    _seed_batch(
        batch_id="batch_list_old",
        created_at=now - timedelta(minutes=2),
        connector_id="connector_list",
        task_version_id="task_version_list",
        target_asset_key="auris/audio/raw_recordings",
        status="succeeded",
    )
    _seed_batch(
        batch_id="batch_list_new",
        created_at=now - timedelta(minutes=1),
        connector_id="connector_list",
        task_version_id="task_version_list",
        target_asset_key="auris/audio/raw_recordings",
        status="partial",
    )
    _seed_batch(
        batch_id="batch_list_unrelated",
        created_at=now,
        connector_id="connector_other",
        task_version_id="task_version_other",
        target_asset_key="auris/audio/other",
        status="succeeded",
    )
    _seed_batch(
        batch_id="batch_list_cross_tenant",
        created_at=now + timedelta(minutes=1),
        connector_id="connector_list",
        task_version_id="task_version_list",
        target_asset_key="auris/audio/raw_recordings",
        status="succeeded",
        tenant_id="tenant_other",
        project_id="project_other",
    )

    first = client.get(
        "/api/v1/import-batches",
        params={
            "connector_id": "connector_list",
            "task_version_id": "task_version_list",
            "target_asset_key": "auris/audio/raw_recordings",
            "limit": 1,
        },
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["total"] == 2
    assert first.json()["data"]["items"][0]["import_batch_id"] == "batch_list_new"
    assert first.json()["meta"]["next_cursor"]

    second = client.get(
        "/api/v1/import-batches",
        params={
            "connector_id": "connector_list",
            "task_version_id": "task_version_list",
            "target_asset_key": "auris/audio/raw_recordings",
            "limit": 1,
            "cursor": first.json()["meta"]["next_cursor"],
        },
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["items"][0]["import_batch_id"] == "batch_list_old"
    assert second.json()["meta"]["next_cursor"] is None

    partial = client.get(
        "/api/v1/import-batches",
        params={
            "connector_id": "connector_list",
            "status": "partial",
        },
        headers=auth_headers,
    )
    assert partial.status_code == 200
    assert [item["import_batch_id"] for item in partial.json()["data"]["items"]] == [
        "batch_list_new"
    ]


def test_import_batch_items_support_status_filter_and_pagination(
    client,
    auth_headers,
) -> None:
    now = datetime.now(UTC)
    _seed_batch(
        batch_id="batch_items_list",
        created_at=now,
        connector_id="connector_items",
        task_version_id="task_version_items",
        target_asset_key="auris/audio/raw_recordings",
        status="partial",
    )
    with SessionLocal() as session:
        for index, status in enumerate(("failed", "failed", "succeeded"), start=1):
            session.add(
                ImportBatchItem(
                    import_item_id=f"batch_item_{index}",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    import_batch_id="batch_items_list",
                    external_record_id=f"external-{index}",
                    status=status,
                    error_code=(
                        "INTERNAL_S3_EXCEPTION_WITH_BUCKET"
                        if index == 1
                        else "AUDIO_DOWNLOAD_FAILED"
                        if status == "failed"
                        else None
                    ),
                    root_trace_id="trace_batch_items_list",
                    trace_id=f"trace_batch_item_{index}",
                    payload={
                        "retry_lineage": {
                            "source_import_batch_id": "batch_items_source",
                            "source_import_item_id": f"source_item_{index}",
                            "root_import_batch_id": "batch_items_root",
                            "root_import_item_id": f"root_item_{index}",
                            "attempt": 3,
                        }
                    },
                    created_at=now + timedelta(seconds=index),
                    updated_at=now + timedelta(seconds=index),
                )
            )
        session.commit()

    first = client.get(
        "/api/v1/import-batches/batch_items_list/items",
        params={"status": "failed", "limit": 1},
        headers=auth_headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["meta"]["total"] == 2
    assert len(first.json()["data"]["items"]) == 1
    assert first.json()["meta"]["next_cursor"]
    first_item = first.json()["data"]["items"][0]
    assert first_item["retryable"] in {True, False}
    assert first_item["recovery_suggestion"]
    assert first_item["retry_lineage"]["attempt"] == 3
    assert first_item["retry_lineage"]["source_import_batch_id"] == "batch_items_source"

    second = client.get(
        "/api/v1/import-batches/batch_items_list/items",
        params={
            "status": "failed",
            "limit": 1,
            "cursor": first.json()["meta"]["next_cursor"],
        },
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["meta"]["total"] == 2
    assert len(second.json()["data"]["items"]) == 1
    assert (
        second.json()["data"]["items"][0]["import_item_id"]
        != first.json()["data"]["items"][0]["import_item_id"]
    )
    assert second.json()["meta"]["next_cursor"] is None
    serialized = str(first.json()) + str(second.json())
    assert "INTERNAL_S3_EXCEPTION_WITH_BUCKET" not in serialized
    assert "AUDIO_IMPORT_ITEM_FAILED" in serialized
