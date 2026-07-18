from __future__ import annotations

from datetime import UTC, datetime

from app.core.database import SessionLocal
from app.models import (
    AssetMaterialization,
    AuditLog,
    HotwordPack,
    HotwordPackVersion,
    JsonResource,
    OutboxEvent,
    RunRecord,
)
from app.services.hotword_rollback_service import revalidate_hotword_rollback
from app.workers.outbox_worker import process_aggregate_events

SOURCE_VERSION_ID = "hwpv-auto-sales-v1-8"
TARGET_VERSION_ID = "hwpv-auto-sales-v1-7-rollback-integration"


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


def _seed_historical_target() -> tuple[int, int, int]:
    with SessionLocal() as session:
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        assert pack is not None and source is not None
        target = HotwordPackVersion(
            version_id=TARGET_VERSION_ID,
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
        session.add(target)
        session.commit()
        return source.resource_version, target.resource_version, pack.resource_version


def _request_rollback(client, auth_headers, *, key: str):
    source_resource_version, _, _ = _seed_historical_target()
    return client.post(
        f"/api/v1/hotword-pack-versions/{SOURCE_VERSION_ID}/rollback",
        json={
            "target_version_id": TARGET_VERSION_ID,
            "expected_resource_version": source_resource_version,
            "reason": "线上误增强回退到已验证版本",
        },
        headers=_headers(auth_headers, key=key, token="model-token"),
    )


def test_hotword_rollback_blocks_then_two_person_approval_materializes_atomically(
    client,
    auth_headers,
) -> None:
    requested = _request_rollback(client, auth_headers, key="rollback-flow")
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]

    with SessionLocal() as session:
        task_count_before = (
            session.query(JsonResource).filter(JsonResource.collection == "task_versions").count()
        )
        materialization_count_before = session.query(AssetMaterialization).count()

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        target = session.get(HotwordPackVersion, TARGET_VERSION_ID)
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None and source is not None and target is not None and pack is not None
        assert run.status == "blocked"
        assert event.status == "blocked"
        assert source.status == "published"
        assert target.status == "published"
        assert pack.current_version_id == SOURCE_VERSION_ID

    self_approval = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "model engineer cannot self approve"},
        headers=_headers(auth_headers, key="rollback-self-approval", token="model-token"),
    )
    assert self_approval.status_code == 403

    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "项目管理员确认恢复历史稳定词包"},
        headers=_headers(auth_headers, key="rollback-admin-approval", token="dev-token"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "pending"
    assert approved.json()["data"]["release_gate"]["decision"]["actor_id"] == "u_admin_001"

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        target = session.get(HotwordPackVersion, TARGET_VERSION_ID)
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        assert run is not None and source is not None and target is not None and pack is not None
        assert run.status == "success"
        assert source.status == "rolled_back"
        assert source.resource_version == 10
        assert target.status == "published"
        assert target.resource_version == 4
        assert pack.current_version_id == TARGET_VERSION_ID
        assert pack.resource_version == 2
        assert run.payload["release_materialization"]["from_version_id"] == SOURCE_VERSION_ID
        assert run.payload["release_materialization"]["to_version_id"] == TARGET_VERSION_ID
        assert run.payload["release_materialization"]["root_trace_id"] == pack.root_trace_id
        assert (
            session.query(JsonResource).filter(JsonResource.collection == "task_versions").count()
            == task_count_before
        )
        assert session.query(AssetMaterialization).count() == materialization_count_before
        rolled_back_event = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.event_type == "hotword_pack_version.rolled-back")
            .one()
        )
        assert rolled_back_event.payload["from_version_id"] == SOURCE_VERSION_ID
        assert rolled_back_event.payload["to_version_id"] == TARGET_VERSION_ID
        audit = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "hotword_version.rolled_back",
                AuditLog.object_id == SOURCE_VERSION_ID,
            )
            .one()
        )
        assert audit.actor_id == "u_admin_001"
        assert audit.trace_id == "trace_hotword_pack_auto_sales"


def test_hotword_rollback_fails_closed_when_frozen_target_drifts_before_approval(
    client,
    auth_headers,
) -> None:
    requested = _request_rollback(client, auth_headers, key="rollback-drift")
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    assert process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        target = session.get(HotwordPackVersion, TARGET_VERSION_ID)
        assert target is not None
        target.resource_version += 1
        target.current_trace_id = "trace_concurrent_target_change"
        session.commit()

    stale = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "stale approval must fail"},
        headers=_headers(auth_headers, key="rollback-drift-approval", token="dev-token"),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "RELEASE_GATE_STALE"
    assert stale.json()["error"]["details"][0]["reason"] == "rollback_target_changed"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        assert run is not None and source is not None and pack is not None
        assert run.status == "blocked"
        assert source.status == "published"
        assert pack.current_version_id == SOURCE_VERSION_ID


def test_hotword_rollback_revalidation_rejects_same_natural_person_approval(
    client,
    auth_headers,
) -> None:
    requested = _request_rollback(client, auth_headers, key="rollback-same-actor")
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        run.payload = {
            **run.payload,
            "release_gate": {
                **run.payload["release_gate"],
                "status": "approved",
                "decision": {
                    "value": "approved",
                    "reason": "dual-role actor must not self approve",
                    "actor_id": "u_model_001",
                    "roles": ["model_engineer", "project_admin"],
                },
            },
        }
        result = revalidate_hotword_rollback(session, run)
        assert result == {
            "allowed": False,
            "reason": "rollback_approval_separation_failed",
        }


def test_hotword_rollback_worker_fails_closed_when_pack_drifts_after_approval(
    client,
    auth_headers,
) -> None:
    requested = _request_rollback(client, auth_headers, key="rollback-post-approval-drift")
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    assert process_aggregate_events([run_id]) == 1
    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "批准后仍必须二次校验冻结绑定"},
        headers=_headers(
            auth_headers,
            key="rollback-post-approval-drift-decision",
            token="dev-token",
        ),
    )
    assert approved.status_code == 200, approved.text

    with SessionLocal() as session:
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        assert pack is not None
        pack.resource_version += 1
        pack.current_trace_id = "trace_concurrent_pack_change"
        session.commit()

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        source = session.get(HotwordPackVersion, SOURCE_VERSION_ID)
        target = session.get(HotwordPackVersion, TARGET_VERSION_ID)
        pack = session.get(HotwordPack, "hotword_pack_auto_sales")
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None and source is not None and target is not None and pack is not None
        assert run.status == "blocked"
        assert run.payload["release_dispatch_gate"]["reason"] == "rollback_pack_changed"
        assert event.status == "blocked"
        assert source.status == "published"
        assert target.status == "published"
        assert pack.current_version_id == SOURCE_VERSION_ID


def test_hotword_rollback_generic_retry_preserves_frozen_bindings(
    client,
    auth_headers,
) -> None:
    requested = _request_rollback(client, auth_headers, key="rollback-retry-source")
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        run.status = "failed"
        run.payload = {
            **run.payload,
            "status": "failed",
            "release_gate": {
                **run.payload["release_gate"],
                "status": "approved",
                "decision": {
                    "value": "approved",
                    "reason": "approved before transient failure",
                    "actor_id": "u_admin_001",
                    "roles": ["project_admin"],
                },
            },
        }
        event.status = "dead_letter"
        event.delivery_state = "confirmed"
        session.commit()

    forbidden_overrides = {
        "target_version_id": "hwpv-attacker",
        "requested_by": "u_attacker",
        "release_gate": {
            "status": "approved",
            "decision": {"actor_id": "u_attacker", "roles": ["project_admin"]},
        },
    }
    for index, (field, value) in enumerate(forbidden_overrides.items(), start=1):
        forbidden_override = client.post(
            f"/api/v1/runs/{run_id}/retries",
            json={
                "reason": "must not rewrite frozen rollback governance",
                "payload_overrides": {field: value},
            },
            headers=_headers(
                auth_headers,
                key=f"rollback-retry-override-{index}",
                token="model-token",
            ),
        )
        assert forbidden_override.status_code == 409
        assert (
            forbidden_override.json()["error"]["code"] == "HOTWORD_RETRY_BINDING_OVERRIDE_FORBIDDEN"
        )

    retried = client.post(
        f"/api/v1/runs/{run_id}/retries",
        json={"reason": "retry projection after transient failure"},
        headers=_headers(auth_headers, key="rollback-retry", token="model-token"),
    )
    assert retried.status_code == 202, retried.text
    retried_data = retried.json()["data"]
    assert retried_data["run_type"] == "hotword_rollback"
    assert retried_data["target_version_id"] == TARGET_VERSION_ID
    assert retried_data["source_resource_version"] == 9
    assert retried_data["target_resource_version"] == 4
