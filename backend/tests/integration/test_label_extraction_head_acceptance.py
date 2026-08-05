from __future__ import annotations

import hashlib
import json

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    JsonResource,
    LabelExtractionRun,
    LabelVersion,
    ReleaseBundleHead,
    ReleaseDeployment,
    RunRecord,
)
from app.services.label_lifecycle_service import _environment_references
from tests.contract.test_label_closed_loop_api import (
    LABEL_VERSION_ID,
    POLICY_VERSION_ID,
    _create_policy,
    _seed_extraction_prompt,
)

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")


def _sha(value: object) -> str:
    document = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _request_body(run_id: str) -> dict[str, object]:
    return {
        "extraction_run_id": run_id,
        "label_version_id": LABEL_VERSION_ID,
        "prompt_version_id": "prompt-contract-v1",
        "model_version": "label-model-contract-v1",
        "schema_version": "label-observation/1",
        "aggregation_policy_version_id": POLICY_VERSION_ID,
        "subject_scope": "audio-session",
        "subject_refs": [{"id": "session-head-lock", "evidence_ref": "segment-head-lock"}],
        "source_bindings": [
            {
                "source_family": "model-a",
                "source_type": "model",
                "provider": "contract-provider",
                "adapter": "contract-adapter",
            }
        ],
        "input_sha256": _sha({"subject": "session-head-lock"}),
        "execution_mode": "production",
    }


def _seed_production_head(*, aligned: bool) -> dict[str, object]:
    with SessionLocal() as session:
        bundle_sha256 = _sha({"deployment": "rd_extraction_head"})
        deployment = ReleaseDeployment(
            deployment_id="rd_extraction_head",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            environment="production",
            status="completed",
            stage="completed",
            label_version_id=LABEL_VERSION_ID if aligned else "label_other_version",
            prompt_version_id=("prompt-contract-v1" if aligned else "prompt-other-v1"),
            model_version=("label-model-contract-v1" if aligned else "label-model-other-v1"),
            aggregation_policy_version_id=(POLICY_VERSION_ID if aligned else "lap_other_v1"),
            eval_dataset_version_id="evalset-head-lock",
            eval_run_id="evalrun-head-lock",
            rollback_target_deployment_id=None,
            bundle_sha256=bundle_sha256,
            rollout_percentage=100,
            blocked_reasons=[],
            monitor_metrics={},
            approved_by="u_admin_001",
            trace_id="trace_extraction_head",
            payload={"root_trace_id": "trace_extraction_head"},
        )
        session.add(deployment)
        session.flush()
        head = ReleaseBundleHead(
            release_head_id="rbh_extraction_production",
            tenant_id="aurora_auto",
            project_id="sales_qa",
            environment="production",
            active_deployment_id="rd_extraction_head",
            active_bundle_sha256=bundle_sha256,
            prompt_asset_id=("prompt-contract-asset" if aligned else "prompt-other-asset"),
            prompt_version_id=("prompt-contract-v1" if aligned else "prompt-other-v1"),
            label_version_id=LABEL_VERSION_ID if aligned else "label_other_version",
            model_version=("label-model-contract-v1" if aligned else "label-model-other-v1"),
            aggregation_policy_version_id=(POLICY_VERSION_ID if aligned else "lap_other_v1"),
            eval_dataset_version_id="evalset-head-lock",
            generation=7,
            status="active",
            bootstrapped=False,
            activated_by_command_id=None,
            trace_id="trace_extraction_head",
            payload={"root_trace_id": "trace_extraction_head"},
        )
        session.add(head)
        session.commit()
        return {
            "environment": head.environment,
            "generation": head.generation,
            "active_deployment_id": head.active_deployment_id,
            "active_bundle_sha256": head.active_bundle_sha256,
        }


def test_production_extraction_rejects_request_not_bound_to_active_head(client, auth_headers):
    _seed_extraction_prompt()
    _create_policy(client, auth_headers)
    _seed_production_head(aligned=False)
    response = client.post(
        "/api/v1/label-extraction-runs",
        json=_request_body("lexr_head_mismatch"),
        headers={**auth_headers, "Idempotency-Key": "extraction-head-mismatch"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EXTRACTION_RELEASE_HEAD_BINDING_MISMATCH"
    with SessionLocal() as session:
        assert session.get(RunRecord, "lexr_head_mismatch") is None
        assert session.get(LabelExtractionRun, "lexr_head_mismatch") is None


def test_production_extraction_freezes_active_head_generation_in_manifest(client, auth_headers):
    _seed_extraction_prompt()
    _create_policy(client, auth_headers)
    expected_lock = _seed_production_head(aligned=True)
    response = client.post(
        "/api/v1/label-extraction-runs",
        json=_request_body("lexr_head_locked"),
        headers={**auth_headers, "Idempotency-Key": "extraction-head-locked"},
    )
    assert response.status_code == 202, response.text
    with SessionLocal() as session:
        record = session.get(RunRecord, "lexr_head_locked")
        projection = session.get(LabelExtractionRun, "lexr_head_locked")
        assert record is not None and projection is not None
        assert record.payload["release_head_lock"] == expected_lock
        assert projection.payload["release_head_lock"] == expected_lock
        assert projection.payload["manifest"]["release_head_lock"] == expected_lock


def test_production_extraction_requires_an_active_head(client, auth_headers):
    _seed_extraction_prompt()
    _create_policy(client, auth_headers)
    response = client.post(
        "/api/v1/label-extraction-runs",
        json=_request_body("lexr_head_missing"),
        headers={**auth_headers, "Idempotency-Key": "extraction-head-missing"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EXTRACTION_RELEASE_HEAD_REQUIRED"


def test_accepted_production_run_is_a_deprecation_reference(client, auth_headers):
    _seed_extraction_prompt()
    _create_policy(client, auth_headers)
    _seed_production_head(aligned=True)
    response = client.post(
        "/api/v1/label-extraction-runs",
        json=_request_body("lexr_deprecation_reference"),
        headers={**auth_headers, "Idempotency-Key": "extraction-deprecation-reference"},
    )
    assert response.status_code == 202, response.text
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        roles=("project_admin",),
        request_id="deprecation-reference",
        trace_id="trace_deprecation_reference",
        idempotency_key="deprecation-reference",
    )
    with SessionLocal() as session:
        references = _environment_references(
            session,
            ctx,
            label_version_id=LABEL_VERSION_ID,
            replacement_label_version_id=None,
        )
        assert references.in_flight == [
            {
                "run_id": "lexr_deprecation_reference",
                "run_status": "queued",
                "environment": "production",
                "head_generation": 7,
                "active_deployment_id": "rd_extraction_head",
                "active_bundle_sha256": _sha({"deployment": "rd_extraction_head"}),
            }
        ]
        assert any(blocker["reference_type"] == "in-flight-run" for blocker in references.blockers)


def test_label_version_detail_separates_artifact_lifecycle_from_activation(client, auth_headers):
    _seed_extraction_prompt()
    _seed_production_head(aligned=True)
    response = client.get(f"/api/v1/label-versions/{LABEL_VERSION_ID}", headers=auth_headers)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["artifact_lifecycle"]["status"] == "published"
    assert data["artifact_lifecycle"]["resource_version"] >= 1
    assert data["activation_summary"] == {
        "active_environment_count": 1,
        "active_environments": ["production"],
        "latest_generation": 7,
    }
    assert data["environment_activations"][0]["active_deployment_id"] == ("rd_extraction_head")
    assert data["activation_timeline"] == []


def test_deprecated_label_version_rejects_generic_patch(client, auth_headers):
    with SessionLocal() as session:
        version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert version is not None
        version.status = "deprecated"
        version.artifact_status = "deprecated"
        projection = (
            session.query(JsonResource)
            .filter_by(
                tenant_id="aurora_auto",
                project_id="sales_qa",
                collection="label_versions",
                resource_key=LABEL_VERSION_ID,
            )
            .one()
        )
        projection.status = "deprecated"
        projection.data = {
            **projection.data,
            "status": "deprecated",
            "artifact_status": "deprecated",
        }
        session.commit()
    response = client.patch(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}",
        json={"description": "不得修改终态版本"},
        headers={**auth_headers, "Idempotency-Key": "deprecated-label-patch"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TERMINAL_LABEL_VERSION_IMMUTABLE"
