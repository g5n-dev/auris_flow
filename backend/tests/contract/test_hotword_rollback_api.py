from __future__ import annotations

from datetime import UTC, datetime

from app.core.database import SessionLocal
from app.models import AuditLog, HotwordPack, HotwordPackVersion, OutboxEvent, RunRecord

SOURCE_VERSION_ID = "hwpv-auto-sales-v1-8"
TARGET_VERSION_ID = "hwpv-auto-sales-v1-7-rollback-contract"


def _headers(
    auth_headers: dict[str, str],
    *,
    key: str,
    token: str,
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _seed_historical_published_target(*, version_id: str = TARGET_VERSION_ID) -> None:
    with SessionLocal() as session:
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        assert pack is not None and source is not None
        session.add(
            HotwordPackVersion(
                version_id=version_id,
                tenant_id=source.tenant_id,
                project_id=source.project_id,
                pack_id=source.pack_id,
                version="v1.7",
                baseline_version_id=None,
                status="published",
                content_sha256="7" * 64,
                manifest_storage_object_id="storage_hwpv_auto_sales_v1_7_manifest",
                eval_run_id="evalrun_hotword_v17_seed",
                eval_locked=True,
                model_approved_by="u_model_legacy",
                project_admin_confirmed_by="u_admin_legacy",
                provider_artifact_ref="storage_hwpv_auto_sales_v1_7_provider_artifact",
                compiled_provider="auris-audio-stack",
                resource_version=4,
                root_trace_id=pack.root_trace_id,
                current_trace_id="trace_hotword_pack_auto_sales_publish_v1_7",
                published_at=datetime(2025, 4, 1, tzinfo=UTC),
                payload={"artifact_sha256": "7" * 64},
            )
        )
        session.commit()


def test_hotword_rollback_request_is_model_engineer_only_idempotent_and_audited(
    client,
    auth_headers,
) -> None:
    _seed_historical_published_target()
    with SessionLocal() as session:
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        assert source is not None
        source_resource_version = source.resource_version

    payload = {
        "target_version_id": TARGET_VERSION_ID,
        "expected_resource_version": source_resource_version,
        "reason": "新版本出现误增强，恢复已验证的 v1.7",
    }
    forbidden = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json=payload,
        headers=_headers(auth_headers, key="rollback-admin-forbidden", token="dev-token"),
    )
    assert forbidden.status_code == 403

    requested = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json=payload,
        headers=_headers(auth_headers, key="rollback-contract", token="model-token"),
    )
    assert requested.status_code == 202, requested.text
    data = requested.json()["data"]
    assert data["run_type"] == "hotword_rollback"
    assert data["status"] == "pending"
    assert data["source_version_id"] == SOURCE_VERSION_ID
    assert data["target_version_id"] == TARGET_VERSION_ID
    assert data["release_gate"]["status"] == "awaiting_decision"
    assert data["root_trace_id"] == "trace_hotword_pack_auto_sales"
    assert data["source_resource_version"] == source_resource_version
    assert data["target_resource_version"] == 4
    assert data["pack_resource_version"] == 1

    replay = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json=payload,
        headers=_headers(auth_headers, key="rollback-contract", token="model-token"),
    )
    assert replay.status_code == 202
    assert replay.json() == requested.json()

    conflict = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json={**payload, "reason": "同一幂等键下的不同原因"},
        headers=_headers(auth_headers, key="rollback-contract", token="model-token"),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    with SessionLocal() as session:
        run = session.get(RunRecord, data["run_id"])
        assert run is not None
        assert run.trace_id == "trace_hotword_pack_auto_sales"
        assert run.payload["source_root_trace_id"] == run.payload["target_root_trace_id"]
        assert run.payload["target_root_trace_id"] == run.payload["pack_root_trace_id"]
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == data["run_id"]).one()
        assert event.event_type == "hotword_pack_version.rollback-requested"
        assert event.aggregate_type == "hotword_rollback"
        assert event.payload["trace_id"] == "trace_hotword_pack_auto_sales"
        audit = (
            session.query(AuditLog)
            .filter(
                AuditLog.object_id == data["run_id"],
                AuditLog.action == "hotword_rollback.create",
            )
            .one()
        )
        assert audit.trace_id == "trace_hotword_pack_auto_sales"


def test_hotword_rollback_rejects_wrong_version_and_non_historical_target(
    client,
    auth_headers,
) -> None:
    _seed_historical_published_target()
    wrong_version = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json={
            "target_version_id": TARGET_VERSION_ID,
            "expected_resource_version": 1,
            "reason": "stale request",
        },
        headers=_headers(auth_headers, key="rollback-stale", token="model-token"),
    )
    assert wrong_version.status_code == 409
    assert wrong_version.json()["error"]["code"] == "RESOURCE_VERSION_CONFLICT"

    current_as_target = client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json={
            "target_version_id": SOURCE_VERSION_ID,
            "expected_resource_version": 9,
            "reason": "cannot roll back to itself",
        },
        headers=_headers(auth_headers, key="rollback-self-target", token="model-token"),
    )
    assert current_as_target.status_code == 409
    assert current_as_target.json()["error"]["code"] == "HOTWORD_ROLLBACK_TARGET_INVALID"
