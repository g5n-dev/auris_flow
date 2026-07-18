from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import AssetMaterialization, RunRecord, StorageObject
from app.services.data_asset_materialization_service import (
    materialization_id_for,
    materialize_asset_completion,
)

ASSET_KEY = "auris/model/asr_transcripts"
PARTITION_KEY = "aurora_auto/BJ-AURORA-001/2025-05-26/hotword-guard"
SOURCE_MATERIALIZATION_ID = "mat_hotword_guard_source"
ROOT_TRACE_ID = "trace_hotword_guard_root"


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="hotword-backfill-guard",
        trace_id="trace_hotword_guard_completion",
        idempotency_key=None,
    )


def _source_payload() -> dict[str, object]:
    return {
        "asset_key": ASSET_KEY,
        "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/source",
        "run_id": "source_asr_run",
        "marker": "immutable-source",
    }


def _seed_source(session) -> dict[str, object]:
    payload = _source_payload()
    session.add(
        AssetMaterialization(
            materialization_id=SOURCE_MATERIALIZATION_ID,
            tenant_id="aurora_auto",
            project_id="sales_qa",
            status="success",
            trace_id="trace_source_asr",
            payload=payload,
        )
    )
    session.commit()
    return deepcopy(payload)


def _record(*, run_id: str = "asset_backfill_hotword_guard") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="asset_backfill",
        status="success",
        run_key=ASSET_KEY,
        partition_key=PARTITION_KEY,
        trace_id=ROOT_TRACE_ID,
        payload={
            "asset_key": ASSET_KEY,
            "partition_key": PARTITION_KEY,
            "impact_scope": {
                "hotword_pack_version_id": "hwpv-hotword-guard",
                "eval_run_id": "hweval-hotword-guard",
                "task_version_id": "task-hotword-guard",
                "materialization_id": SOURCE_MATERIALIZATION_ID,
                "source_materialization_id": SOURCE_MATERIALIZATION_ID,
                "source_asset_key": ASSET_KEY,
                "root_trace_id": ROOT_TRACE_ID,
                "overwrite_history": False,
            },
        },
    )


def _receipt(**result_overrides: object) -> dict[str, object]:
    return {
        "completion_receipt_id": "receipt-hotword-guard",
        "trace_id": "trace_hotword_guard_completion",
        "result_ref": {
            "asset_key": ASSET_KEY,
            "partition_key": PARTITION_KEY,
            "record_count": 10,
            "error_count": 0,
            **result_overrides,
        },
        "metrics": {"record_count": 10, "error_count": 0},
    }


def _verified_run_storage_object(session, record: RunRecord) -> StorageObject:
    storage_object_id = f"sto_{record.run_id}"
    object_key = (
        f"tenants/{record.tenant_id}/projects/{record.project_id}/runs/"
        f"{record.run_id}/assets/hotword-backfill.jsonl"
    )
    storage_object = StorageObject(
        storage_object_id=storage_object_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        provider="minio",
        bucket="auris-flow-local",
        object_key=object_key,
        object_key_sha256=hashlib.sha256(object_key.encode("utf-8")).hexdigest(),
        source_type=record.run_type,
        source_id=record.run_id,
        content_type="application/x-ndjson",
        size_bytes=128,
        content_sha256=hashlib.sha256(b"hotword-backfill").hexdigest(),
        etag="hotword-backfill-etag",
        status="verified",
        trace_id=record.trace_id,
        payload={"run_id": record.run_id, "verification": {"status": "verified"}},
    )
    session.add(storage_object)
    session.flush()
    return storage_object


def _assert_source_unchanged(session, expected_payload: dict[str, object]) -> None:
    session.expire_all()
    source = session.get(AssetMaterialization, SOURCE_MATERIALIZATION_ID)
    assert source is not None
    assert source.tenant_id == "aurora_auto"
    assert source.project_id == "sales_qa"
    assert source.status == "success"
    assert source.trace_id == "trace_source_asr"
    assert source.payload == expected_payload


def test_hotword_backfill_rejects_source_materialization_as_completion_target() -> None:
    with SessionLocal() as session:
        source_payload = _seed_source(session)

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(),
                _receipt(materialization_id=SOURCE_MATERIALIZATION_ID),
            )

        assert exc_info.value.code == "HOTWORD_BACKFILL_TARGET_IS_SOURCE"
        _assert_source_unchanged(session, source_payload)


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [
        ("aurora_auto", "sales_qa"),
        ("other_tenant", "other_project"),
    ],
)
def test_hotword_backfill_rejects_any_existing_completion_target_across_scopes(
    tenant_id: str,
    project_id: str,
) -> None:
    ctx = _ctx()
    record = _record()
    existing_id = materialization_id_for(ctx, ASSET_KEY, PARTITION_KEY, record.run_id)
    existing_payload = {"asset_key": "foreign/or/existing", "marker": "must-survive"}
    with SessionLocal() as session:
        source_payload = _seed_source(session)
        session.add(
            AssetMaterialization(
                materialization_id=existing_id,
                tenant_id=tenant_id,
                project_id=project_id,
                status="success",
                trace_id="trace_existing_target",
                payload=existing_payload,
            )
        )
        session.commit()

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                ctx,
                record,
                _receipt(),
            )

        assert exc_info.value.code == "HOTWORD_BACKFILL_TARGET_ALREADY_EXISTS"
        _assert_source_unchanged(session, source_payload)
        session.expire_all()
        existing = session.get(AssetMaterialization, existing_id)
        assert existing is not None
        assert existing.tenant_id == tenant_id
        assert existing.project_id == project_id
        assert existing.trace_id == "trace_existing_target"
        assert existing.payload == existing_payload


def test_hotword_backfill_rejects_caller_chosen_new_materialization_id() -> None:
    caller_chosen_id = "mat_hotword_caller_chosen"
    with SessionLocal() as session:
        source_payload = _seed_source(session)

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(),
                _receipt(materialization_id=caller_chosen_id),
            )

        assert exc_info.value.code == "HOTWORD_BACKFILL_TARGET_ID_FORBIDDEN"
        assert session.get(AssetMaterialization, caller_chosen_id) is None
        _assert_source_unchanged(session, source_payload)


@pytest.mark.parametrize(
    ("result_overrides", "expected_code"),
    [
        (
            {"asset_key": "auris/model/another_asset"},
            "HOTWORD_BACKFILL_TARGET_ASSET_MISMATCH",
        ),
        (
            {"partition_key": f"{PARTITION_KEY}/tampered"},
            "HOTWORD_BACKFILL_TARGET_PARTITION_MISMATCH",
        ),
    ],
)
def test_hotword_backfill_freezes_target_asset_and_partition(
    result_overrides: dict[str, object],
    expected_code: str,
) -> None:
    with SessionLocal() as session:
        source_payload = _seed_source(session)

        with pytest.raises(ApiError) as exc_info:
            materialize_asset_completion(
                session,
                _ctx(),
                _record(),
                _receipt(**result_overrides),
            )

        assert exc_info.value.code == expected_code
        _assert_source_unchanged(session, source_payload)


def test_hotword_backfill_derives_stable_target_id_when_receipt_omits_it() -> None:
    ctx = _ctx()
    record = _record(run_id="asset_backfill_hotword_derived")
    expected_id = materialization_id_for(ctx, ASSET_KEY, PARTITION_KEY, record.run_id)
    with SessionLocal() as session:
        source_payload = _seed_source(session)
        storage_object = _verified_run_storage_object(session, record)

        materialized = materialize_asset_completion(
            session,
            ctx,
            record,
            _receipt(storage_object_id=storage_object.storage_object_id),
        )
        session.flush()

        assert materialized[0]["materialization_id"] == expected_id
        target = session.get(AssetMaterialization, expected_id)
        assert target is not None
        assert target.payload["source_materialization_id"] == SOURCE_MATERIALIZATION_ID
        assert target.payload["asset_key"] == ASSET_KEY
        assert target.payload["partition_key"] == PARTITION_KEY
        assert target.payload["storage_refs"][0]["storage_object_id"] == (
            storage_object.storage_object_id
        )
        _assert_source_unchanged(session, source_payload)
