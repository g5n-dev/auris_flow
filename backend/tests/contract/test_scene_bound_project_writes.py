from __future__ import annotations

from urllib.parse import quote

import pytest
from sqlalchemy import delete

from app.core.database import SessionLocal
from app.models import ProjectSceneProfileBinding

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")


def _production_scene_lock(client, auth_headers: dict[str, str]) -> dict[str, str]:
    response = client.get(
        "/api/v1/projects/sales_qa/scene-profile?environment=production",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    binding = response.json()["data"]
    return {
        "scene_profile_id": binding["scene_profile_id"],
        "scene_profile_version_id": binding["scene_profile_version_id"],
        "scene_profile_snapshot_sha256": binding["manifest_sha256"],
    }


@pytest.mark.parametrize(
    ("path", "payload", "idempotency_key"),
    [
        (
            "/api/v1/connectors",
            {"connector_id": "scene_bound_connector", "name": "Scene 约束连接器"},
            "scene-bound-connector",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/backfills",
            {"reason": "scene lock contract"},
            "scene-bound-backfill",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/checks/retry",
            {"reason": "scene lock contract"},
            "scene-bound-check-retry",
        ),
        (
            "/api/v1/exports",
            {"target": "data_asset", "object_id": "auris/label/event_tags"},
            "scene-bound-export",
        ),
    ],
)
def test_project_writes_capture_canonical_active_scene_lock(
    client,
    auth_headers,
    path: str,
    payload: dict[str, str],
    idempotency_key: str,
):
    expected_lock = _production_scene_lock(client, auth_headers)

    response = client.post(
        path,
        json=payload,
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
    )

    assert response.status_code in {201, 202}, response.text
    data = response.json()["data"]
    assert {key: data[key] for key in expected_lock} == expected_lock


@pytest.mark.parametrize(
    ("path", "payload", "idempotency_key"),
    [
        (
            "/api/v1/connectors",
            {"connector_id": "forged_scene_connector", "name": "伪造 Scene 连接器"},
            "forged-scene-connector",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/backfills",
            {"reason": "forged scene lock"},
            "forged-scene-backfill",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/checks/retry",
            {"reason": "forged scene lock"},
            "forged-scene-check-retry",
        ),
        (
            "/api/v1/exports",
            {"target": "data_asset", "object_id": "auris/label/event_tags"},
            "forged-scene-export",
        ),
    ],
)
def test_project_writes_reject_forged_scene_snapshot(
    client,
    auth_headers,
    path: str,
    payload: dict[str, str],
    idempotency_key: str,
):
    response = client.post(
        path,
        json={**payload, "scene_profile_snapshot_sha256": "0" * 64},
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCENE_PROFILE_SNAPSHOT_MISMATCH"


@pytest.mark.parametrize(
    ("path", "payload", "idempotency_key"),
    [
        (
            "/api/v1/connectors",
            {"connector_id": "unbound_scene_connector", "name": "未绑定 Scene 连接器"},
            "unbound-scene-connector",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/backfills",
            {"reason": "unbound scene"},
            "unbound-scene-backfill",
        ),
        (
            f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/checks/retry",
            {"reason": "unbound scene"},
            "unbound-scene-check-retry",
        ),
        (
            "/api/v1/exports",
            {"target": "data_asset", "object_id": "auris/label/event_tags"},
            "unbound-scene-export",
        ),
    ],
)
def test_project_writes_fail_closed_without_active_scene_binding(
    client,
    auth_headers,
    path: str,
    payload: dict[str, str],
    idempotency_key: str,
):
    with SessionLocal() as session:
        session.execute(
            delete(ProjectSceneProfileBinding).where(
                ProjectSceneProfileBinding.tenant_id == "aurora_auto",
                ProjectSceneProfileBinding.project_id == "sales_qa",
                ProjectSceneProfileBinding.environment == "production",
            )
        )
        session.commit()

    response = client.post(
        path,
        json=payload,
        headers={**auth_headers, "Idempotency-Key": idempotency_key},
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SCENE_PROFILE_BINDING_REQUIRED"


def test_asset_check_retry_does_not_disclose_cross_project_asset(client, auth_headers):
    created_project = client.post(
        "/api/v1/projects",
        json={"project_id": "scene_scope_other_project", "name": "Scene 隔离项目"},
        headers={**auth_headers, "Idempotency-Key": "scene-scope-create-project"},
    )
    assert created_project.status_code == 201, created_project.text
    other_project_headers = {
        **auth_headers,
        "X-Project-Id": "scene_scope_other_project",
        "X-Request-Id": "scene-scope-other-project",
        "Idempotency-Key": "scene-scope-cross-project-retry",
    }

    response = client.post(
        f"/api/v1/data-assets/{quote('auris/label/event_tags', safe='')}/checks/retry",
        json={"reason": "cross-project probe"},
        headers=other_project_headers,
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_only_well_formed_governance_module_exports_are_scene_exempt(client, auth_headers):
    with SessionLocal() as session:
        session.execute(
            delete(ProjectSceneProfileBinding).where(
                ProjectSceneProfileBinding.tenant_id == "aurora_auto",
                ProjectSceneProfileBinding.project_id == "sales_qa",
                ProjectSceneProfileBinding.environment == "production",
            )
        )
        session.commit()

    governance_export = client.post(
        "/api/v1/exports",
        json={
            "target": "module_view",
            "module_key": "settings",
            "object_id": "settings:providers:all",
        },
        headers={**auth_headers, "Idempotency-Key": "scene-exempt-settings-export"},
    )
    assert governance_export.status_code == 202, governance_export.text
    assert "scene_profile_id" not in governance_export.json()["data"]

    forged_governance_scope = client.post(
        "/api/v1/exports",
        json={
            "target": "module_view",
            "module_key": "settings",
            "object_id": "assets:all:all",
        },
        headers={**auth_headers, "Idempotency-Key": "scene-exempt-forged-export"},
    )
    assert forged_governance_scope.status_code == 409, forged_governance_scope.text
    assert forged_governance_scope.json()["error"]["code"] == "SCENE_PROFILE_BINDING_REQUIRED"


def test_connector_patch_recaptures_scene_lock_and_rejects_stale_snapshot(client, auth_headers):
    expected_lock = _production_scene_lock(client, auth_headers)
    created = client.post(
        "/api/v1/connectors",
        json={"connector_id": "scene_patch_connector", "name": "Scene Patch"},
        headers={**auth_headers, "Idempotency-Key": "scene-patch-create"},
    )
    assert created.status_code == 201, created.text

    patched = client.patch(
        "/api/v1/connectors/scene_patch_connector",
        json={"name": "Scene Patch Updated"},
        headers={**auth_headers, "Idempotency-Key": "scene-patch-update"},
    )
    assert patched.status_code == 200, patched.text
    assert {key: patched.json()["data"][key] for key in expected_lock} == expected_lock

    stale = client.patch(
        "/api/v1/connectors/scene_patch_connector",
        json={"scene_profile_snapshot_sha256": "0" * 64},
        headers={**auth_headers, "Idempotency-Key": "scene-patch-stale"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "SCENE_PROFILE_SNAPSHOT_MISMATCH"


def test_connector_target_asset_must_exist_in_current_project(client, auth_headers):
    response = client.post(
        "/api/v1/connectors",
        json={
            "connector_id": "missing_target_connector",
            "name": "越界目标连接器",
            "target_asset_key": "auris/audio/not-registered",
        },
        headers={**auth_headers, "Idempotency-Key": "missing-target-connector"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_asset_check_retry_uses_only_authoritative_failed_check_scope(client, auth_headers):
    asset_key = quote("auris/label/event_tags", safe="")
    accepted = client.post(
        f"/api/v1/data-assets/{asset_key}/checks/retry",
        json={
            "reason": "retry authoritative failures",
            "failed_check_ids": ["check_label_document_consistency"],
            "failed_partitions": ["aurora_auto/BJ-AURORA-001/2025-05-26/12"],
        },
        headers={**auth_headers, "Idempotency-Key": "authoritative-check-retry"},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["data"]["failed_check_ids"] == ["check_label_document_consistency"]
    assert accepted.json()["data"]["failed_partitions"] == [
        "aurora_auto/BJ-AURORA-001/2025-05-26/12"
    ]

    forged = client.post(
        f"/api/v1/data-assets/{asset_key}/checks/retry",
        json={"failed_check_ids": ["check_not_in_asset"]},
        headers={**auth_headers, "Idempotency-Key": "forged-check-retry"},
    )
    assert forged.status_code == 409, forged.text
    assert forged.json()["error"]["code"] == "ASSET_CHECK_RETRY_SCOPE_INVALID"

    no_failure_asset = quote("auris/audio/raw_recordings", safe="")
    not_required = client.post(
        f"/api/v1/data-assets/{no_failure_asset}/checks/retry",
        json={"reason": "should not create"},
        headers={**auth_headers, "Idempotency-Key": "unneeded-check-retry"},
    )
    assert not_required.status_code == 409, not_required.text
    assert not_required.json()["error"]["code"] == "ASSET_CHECK_RETRY_NOT_REQUIRED"
