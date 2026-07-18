from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    DataAsset,
    JsonResource,
    LabelMappingBundle,
    LabelMappingBundleSource,
    LabelTaxonomy,
    LabelVersion,
    OutboxEvent,
    ReleaseBundleHead,
    ReleaseDeployment,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
TAXONOMY_ID = "taxonomy_lifecycle_api"
SOURCE_VERSION_ID = "lv_lifecycle_api_source"
TARGET_VERSION_ID = "lv_lifecycle_api_target"
MAPPING_BUNDLE_ID = "lmb_lifecycle_api"


def _headers(
    auth_headers: dict[str, str],
    key: str,
    *,
    token: str = "dev-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _version_payload(
    version_id: str,
    *,
    semantic_version: str,
    resource_version: int,
) -> dict[str, object]:
    return {
        "id": version_id,
        "label_version_id": version_id,
        "taxonomy_id": TAXONOMY_ID,
        "semantic_version": semantic_version,
        "status": "published",
        "artifact_status": "published",
        "resource_version": resource_version,
        "content_sha256": ("a" if version_id == SOURCE_VERSION_ID else "b") * 64,
    }


def _add_version(
    session,
    version_id: str,
    *,
    semantic_version: str,
    resource_version: int,
) -> None:
    payload = _version_payload(
        version_id,
        semantic_version=semantic_version,
        resource_version=resource_version,
    )
    session.add(
        LabelVersion(
            label_version_id=version_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            status="published",
            resource_version=resource_version,
            taxonomy_id=TAXONOMY_ID,
            semantic_version=semantic_version,
            artifact_status="published",
            artifact_published_at=datetime(2026, 7, 1, tzinfo=UTC),
            content_sha256=str(payload["content_sha256"]),
            trace_id=f"trace-{version_id}",
            payload=payload,
        )
    )
    session.add(
        JsonResource(
            collection="label_versions",
            resource_key=version_id,
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            status="published",
            trace_id=f"trace-{version_id}",
            data=dict(payload),
        )
    )


def _seed_versions(*, with_mapping: bool = True) -> None:
    with SessionLocal() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="HTTP 生命周期标签体系",
                description="契约测试",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-taxonomy-lifecycle-api",
                payload={"taxonomy_id": TAXONOMY_ID},
            )
        )
        _add_version(
            session,
            SOURCE_VERSION_ID,
            semantic_version="1.0.0",
            resource_version=7,
        )
        _add_version(
            session,
            TARGET_VERSION_ID,
            semantic_version="2.0.0",
            resource_version=3,
        )
        session.flush()
        if with_mapping:
            session.add(
                LabelMappingBundle(
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    target_label_version_id=TARGET_VERSION_ID,
                    source_label_version_ids=[SOURCE_VERSION_ID],
                    source_manifest_sha256="c" * 64,
                    compiler_version="label-mapping-compiler/1",
                    status="published",
                    resource_version=1,
                    canonical_manifest_sha256="d" * 64,
                    approval_id="approval-lifecycle-api",
                    approved_by="u_admin_001",
                    approved_at=datetime(2026, 7, 2, tzinfo=UTC),
                    published_at=datetime(2026, 7, 3, tzinfo=UTC),
                    root_trace_id="trace-root-mapping-lifecycle-api",
                    trace_id="trace-mapping-lifecycle-api",
                    payload={"mapping_bundle_id": MAPPING_BUNDLE_ID},
                )
            )
            session.flush()
            session.add(
                LabelMappingBundleSource(
                    bundle_source_id="lmbs_lifecycle_api",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    source_label_version_id=SOURCE_VERSION_ID,
                    source_resource_version=7,
                    source_order=0,
                    content_sha256="e" * 64,
                    trace_id="trace-mapping-source-lifecycle-api",
                    payload={},
                )
            )
        session.commit()


def _preflight_body(*, reason: str = "升级到新版标签") -> dict[str, object]:
    return {
        "expected_resource_version": 7,
        "replacement_label_version_id": TARGET_VERSION_ID,
        "mapping_bundle_id": MAPPING_BUNDLE_ID,
        "reason": reason,
    }


def _transition_body() -> dict[str, object]:
    return {"action": "deprecate", **_preflight_body()}


@pytest.mark.parametrize(
    ("path_suffix", "body", "server_field"),
    [
        (
            "deprecation-preflights",
            _preflight_body(),
            {"ready_for_transition": True},
        ),
        (
            "transitions",
            _transition_body(),
            {"artifact_status": "deprecated"},
        ),
    ],
)
def test_lifecycle_endpoints_reject_forged_server_fields(
    client,
    auth_headers,
    path_suffix: str,
    body: dict[str, object],
    server_field: dict[str, object],
) -> None:
    response = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/{path_suffix}",
        json={**body, **server_field},
        headers=_headers(auth_headers, f"lifecycle-forged-{path_suffix}"),
    )

    assert response.status_code == 422
    assert "extra_forbidden" in response.text


def test_system_actor_cannot_request_a_lifecycle_transition(client, auth_headers) -> None:
    _seed_versions()

    response = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/deprecation-preflights",
        json=_preflight_body(),
        headers=_headers(auth_headers, "lifecycle-system", token="system-token"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ("AGENT_LABEL_LIFECYCLE_TRANSITION_FORBIDDEN")


def test_preflight_http_response_replays_exactly_and_conflicts_on_a_new_body(
    client,
    auth_headers,
) -> None:
    _seed_versions()
    headers = _headers(auth_headers, "lifecycle-preflight-replay")
    path = f"/api/v1/label-versions/{SOURCE_VERSION_ID}/deprecation-preflights"

    first = client.post(path, json=_preflight_body(), headers=headers)
    replay = client.post(path, json=_preflight_body(), headers=headers)
    conflict = client.post(
        path,
        json=_preflight_body(reason="同一幂等键的新请求"),
        headers=headers,
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["data"]["ready_for_transition"] is True
    assert first.json()["data"]["in_flight_run_references"] == []
    assert first.json()["meta"]["trace_id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.tenant_id == TENANT_ID,
                    AuditLog.project_id == PROJECT_ID,
                    AuditLog.action == "label_version.deprecation_preflight",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == TENANT_ID,
                    OutboxEvent.project_id == PROJECT_ID,
                    OutboxEvent.event_type == "label_version.deprecation_requested",
                )
            )
            == 1
        )


def test_preflight_http_exposes_bounded_scoped_downstream_impacts(
    client,
    auth_headers,
) -> None:
    _seed_versions()
    with SessionLocal() as session:
        session.add_all(
            [
                DataAsset(
                    data_asset_id="asset_lifecycle_api_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="archived",
                    trace_id="trace-lifecycle-api-impact",
                    payload={"label_version_id": SOURCE_VERSION_ID},
                ),
                DataAsset(
                    data_asset_id="asset_lifecycle_api_other_project",
                    tenant_id=TENANT_ID,
                    project_id="other-project",
                    status="published",
                    trace_id="trace-lifecycle-api-other",
                    payload={"label_version_id": SOURCE_VERSION_ID},
                ),
            ]
        )
        session.commit()

    response = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/deprecation-preflights",
        json={**_preflight_body(), "impact_limit": 1},
        headers=_headers(auth_headers, "lifecycle-impact-http"),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["downstream_impact_total"] == 1
    assert data["blocking_impact_total"] == 0
    assert data["migration_required_impact_total"] == 0
    assert data["historical_reference_total"] == 1
    assert data["impact_scan_complete"] is True
    assert data["migration_evidence_required"] is False
    assert data["migration_evidence_satisfied"] is True
    assert data["downstream_impacts"] == [
        {
            "impact_key": "data-asset:asset_lifecycle_api_impact",
            "impact_type": "data-asset",
            "resource_id": "asset_lifecycle_api_impact",
            "status": "archived",
            "reference_role": "payload-reference",
            "impact_disposition": "historical-reference",
            "details": {"trace_id": "trace-lifecycle-api-impact"},
        }
    ]


def test_transition_endpoint_commits_strong_and_json_projections(client, auth_headers) -> None:
    _seed_versions()

    response = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/transitions",
        json=_transition_body(),
        headers=_headers(auth_headers, "lifecycle-transition-success"),
    )

    assert response.status_code == 200
    assert response.json()["data"]["artifact_status"] == "deprecated"
    assert response.json()["data"]["resource_version"] == 8
    with SessionLocal() as session:
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == SOURCE_VERSION_ID,
            )
        )
        assert version is not None and version.artifact_status == "deprecated"
        assert projection is not None and projection.status == "deprecated"
        assert projection.data["resource_version"] == 8


def test_active_production_head_is_visible_in_preflight_and_blocks_transition(
    client,
    auth_headers,
) -> None:
    _seed_versions(with_mapping=False)
    with SessionLocal() as session:
        deployment = ReleaseDeployment(
            deployment_id="rd_lifecycle_api_active",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            environment="production",
            status="completed",
            stage="completed",
            label_version_id=SOURCE_VERSION_ID,
            prompt_version_id="prompt-lifecycle-api",
            model_version="model-lifecycle-api",
            aggregation_policy_version_id="policy-lifecycle-api",
            eval_dataset_version_id="dataset-lifecycle-api",
            eval_run_id="eval-lifecycle-api",
            rollback_target_deployment_id=None,
            bundle_sha256="6" * 64,
            rollout_percentage=100,
            blocked_reasons=[],
            monitor_metrics={},
            approved_by="u_admin_001",
            trace_id="trace-deployment-lifecycle-api",
            payload={},
        )
        session.add(deployment)
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id="rbh_lifecycle_api_active",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
                active_deployment_id=deployment.deployment_id,
                active_bundle_sha256=deployment.bundle_sha256,
                prompt_asset_id="prompt-asset-lifecycle-api",
                prompt_version_id=deployment.prompt_version_id,
                label_version_id=SOURCE_VERSION_ID,
                model_version=deployment.model_version,
                aggregation_policy_version_id=deployment.aggregation_policy_version_id,
                eval_dataset_version_id=deployment.eval_dataset_version_id,
                generation=2,
                status="active",
                bootstrapped=False,
                activated_by_command_id=None,
                trace_id="trace-head-lifecycle-api",
                payload={},
            )
        )
        session.commit()

    body = {
        "expected_resource_version": 7,
        "replacement_label_version_id": None,
        "mapping_bundle_id": None,
        "reason": "无替代版本退役",
    }
    preflight = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/deprecation-preflights",
        json=body,
        headers=_headers(auth_headers, "lifecycle-preflight-blocked"),
    )
    transition = client.post(
        f"/api/v1/label-versions/{SOURCE_VERSION_ID}/transitions",
        json={"action": "deprecate", **body},
        headers=_headers(auth_headers, "lifecycle-transition-blocked"),
    )

    assert preflight.status_code == 200
    assert preflight.json()["data"]["ready_for_transition"] is False
    assert preflight.json()["data"]["safe_stop_required"] is True
    assert transition.status_code == 409
    assert transition.json()["error"]["code"] == ("LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE")
    with SessionLocal() as session:
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        assert version is not None and version.artifact_status == "published"


def test_label_version_from_another_scope_is_not_visible(client, auth_headers) -> None:
    outsider_id = "lv_lifecycle_api_outside_scope"
    with SessionLocal() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id="taxonomy_lifecycle_api_outside",
                tenant_id="tenant_outside",
                project_id="project_outside",
                name="其他租户标签体系",
                description=None,
                status="active",
                resource_version=1,
                content_sha256="8" * 64,
                trace_id="trace-taxonomy-outside",
                payload={},
            )
        )
        session.add(
            LabelVersion(
                label_version_id=outsider_id,
                tenant_id="tenant_outside",
                project_id="project_outside",
                status="published",
                resource_version=1,
                taxonomy_id="taxonomy_lifecycle_api_outside",
                semantic_version="1.0.0",
                artifact_status="published",
                artifact_published_at=datetime(2026, 7, 1, tzinfo=UTC),
                content_sha256="9" * 64,
                trace_id="trace-version-outside",
                payload={},
            )
        )
        session.commit()

    response = client.post(
        f"/api/v1/label-versions/{outsider_id}/deprecation-preflights",
        json={
            "expected_resource_version": 1,
            "replacement_label_version_id": None,
            "mapping_bundle_id": None,
            "reason": "越权尝试",
        },
        headers=_headers(auth_headers, "lifecycle-outside-scope"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LABEL_VERSION_NOT_FOUND"
