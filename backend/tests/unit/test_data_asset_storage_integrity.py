from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import AssetMaterialization, RunRecord, StorageObject
from app.services.data_asset_materialization_service import (
    materialization_id_for,
    materialize_asset_completion,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
ASSET_KEY = "auris/label/event_tags"
PARTITION_KEY = "aurora_auto/BJ-AURORA-001/2025-05-26/storage-integrity"
TRACE_ID = "trace_asset_storage_integrity"


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="asset-storage-integrity",
        trace_id=TRACE_ID,
        idempotency_key=None,
    )


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        run_type="asset_backfill",
        status="success",
        run_key=ASSET_KEY,
        partition_key=PARTITION_KEY,
        trace_id=TRACE_ID,
        payload={
            "asset_key": ASSET_KEY,
            "partition_key": PARTITION_KEY,
        },
    )


def _storage_object(
    run_id: str,
    *,
    provider: str = "minio",
    status: str = "verified",
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
    source_id: str | None = None,
    object_key: str | None = None,
    content_sha256: str | None = "a" * 64,
    size_bytes: int | None = 128,
) -> StorageObject:
    resolved_key = object_key or (
        f"tenants/{tenant_id}/projects/{project_id}/runs/{run_id}/assets/backfill.jsonl"
    )
    storage_object_id = f"sto_{run_id}_{provider}_{tenant_id}"
    return StorageObject(
        storage_object_id=storage_object_id,
        tenant_id=tenant_id,
        project_id=project_id,
        provider=provider,
        bucket=f"{provider}-assets",
        object_key=resolved_key,
        object_key_sha256=hashlib.sha256(resolved_key.encode("utf-8")).hexdigest(),
        source_type="asset_backfill",
        source_id=source_id or run_id,
        content_type="application/x-ndjson",
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        etag="asset-etag-2",
        status=status,
        trace_id=TRACE_ID,
        payload={"run_id": source_id or run_id, "verification": {"status": "verified"}},
    )


def _receipt(**result_ref: Any) -> dict[str, Any]:
    return {
        "completion_receipt_id": "receipt_asset_storage_integrity",
        "trace_id": TRACE_ID,
        "result_ref": {
            "asset_key": ASSET_KEY,
            "partition_key": PARTITION_KEY,
            "record_count": 128,
            "error_count": 0,
            **result_ref,
        },
        "metrics": {"record_count": 128, "error_count": 0},
    }


@pytest.mark.parametrize("provider", ("minio", "s3", "obs", "oss"))
def test_asset_backfill_materializes_only_verified_run_scoped_storage_object(
    provider: str,
) -> None:
    run_id = f"asset_backfill_verified_{provider}"
    record = _record(run_id)
    storage_object = _storage_object(run_id, provider=provider)
    with SessionLocal() as session:
        session.add(storage_object)
        session.flush()

        materialized = materialize_asset_completion(
            session,
            _ctx(),
            record,
            _receipt(storage_object_id=storage_object.storage_object_id),
        )
        session.flush()

        assert len(materialized) == 1
        storage_refs = materialized[0]["storage_refs"]
        assert storage_refs == [
            {
                "kind": "storage_object",
                "storage_object_id": storage_object.storage_object_id,
                "provider": provider,
                "bucket": f"{provider}-assets",
                "object_key": storage_object.object_key,
                "content_type": "application/x-ndjson",
                "size_bytes": 128,
                "content_sha256": "a" * 64,
                "etag": "asset-etag-2",
                "status": "verified",
                "run_id": run_id,
            }
        ]
        row = session.get(
            AssetMaterialization,
            materialization_id_for(_ctx(), ASSET_KEY, PARTITION_KEY, run_id),
        )
        assert row is not None
        assert row.status == "success"
        assert row.payload["storage_refs"] == storage_refs
        session.rollback()


@pytest.mark.parametrize(
    "unsafe_result_ref",
    (
        {"object_uri": "mock://object-storage/backfill.jsonl"},
        {"object_uri": "local://fixtures/backfill.jsonl"},
        {"storage_refs": [{"kind": "object_uri", "uri": "mock://backfill.jsonl"}]},
        {"storage_refs": [{"kind": "object_uri", "uri": "local://backfill.jsonl"}]},
    ),
)
def test_asset_backfill_rejects_uri_only_or_pseudo_storage_references(
    unsafe_result_ref: dict[str, Any],
) -> None:
    run_id = "asset_backfill_reject_pseudo_uri"
    with SessionLocal() as session:
        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(run_id),
                _receipt(**unsafe_result_ref),
            )

        assert exc_info.value.code == "ASSET_MATERIALIZATION_STORAGE_REFERENCE_UNSAFE"
        assert exc_info.value.details[0]["run_id"] == run_id
        assert exc_info.value.details[0]["trace_id"] == TRACE_ID
        assert (
            session.get(
                AssetMaterialization,
                materialization_id_for(_ctx(), ASSET_KEY, PARTITION_KEY, run_id),
            )
            is None
        )


@pytest.mark.parametrize(
    ("storage_kwargs", "expected_code"),
    (
        ({"tenant_id": "other_tenant"}, "ASSET_STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ({"source_id": "another_run"}, "ASSET_STORAGE_OBJECT_RUN_MISMATCH"),
        ({"status": "registered"}, "ASSET_STORAGE_OBJECT_NOT_VERIFIED"),
        ({"status": "ready"}, "ASSET_STORAGE_OBJECT_NOT_VERIFIED"),
        ({"status": "available"}, "ASSET_STORAGE_OBJECT_NOT_VERIFIED"),
        ({"status": "completed"}, "ASSET_STORAGE_OBJECT_NOT_VERIFIED"),
        ({"provider": "local"}, "ASSET_STORAGE_OBJECT_PROVIDER_INVALID"),
        ({"content_sha256": None}, "ASSET_STORAGE_OBJECT_METADATA_INCOMPLETE"),
        ({"size_bytes": 0}, "ASSET_STORAGE_OBJECT_METADATA_INCOMPLETE"),
        (
            {"object_key": "tenants/aurora_auto/projects/sales_qa/assets/backfill.jsonl"},
            "ASSET_STORAGE_OBJECT_RUN_MISMATCH",
        ),
    ),
)
def test_asset_backfill_rejects_untrusted_storage_object_without_success_write(
    storage_kwargs: dict[str, Any],
    expected_code: str,
) -> None:
    run_id = f"asset_backfill_reject_{expected_code.lower()}"
    storage_object = _storage_object(run_id, **storage_kwargs)
    with SessionLocal() as session:
        session.add(storage_object)
        session.flush()

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(run_id),
                _receipt(storage_object_id=storage_object.storage_object_id),
            )

        assert exc_info.value.code == expected_code
        assert exc_info.value.details[0]["run_id"] == run_id
        assert exc_info.value.details[0]["storage_object_id"] == storage_object.storage_object_id
        assert (
            session.get(
                AssetMaterialization,
                materialization_id_for(_ctx(), ASSET_KEY, PARTITION_KEY, run_id),
            )
            is None
        )
        session.rollback()


def test_asset_backfill_rejects_tampered_object_locator_hash() -> None:
    run_id = "asset_backfill_reject_locator_hash"
    storage_object = _storage_object(run_id)
    storage_object.object_key_sha256 = "0" * 64
    with SessionLocal() as session:
        session.add(storage_object)
        session.flush()

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(run_id),
                _receipt(storage_object_id=storage_object.storage_object_id),
            )

        assert exc_info.value.code == "ASSET_STORAGE_OBJECT_LOCATOR_INVALID"
        assert (
            session.get(
                AssetMaterialization,
                materialization_id_for(_ctx(), ASSET_KEY, PARTITION_KEY, run_id),
            )
            is None
        )
        session.rollback()
