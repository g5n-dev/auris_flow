from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models import (
    CalibrationRound,
    GoldSetVersion,
    LabelCalibrationVersion,
    LabelFact,
    LabelVersion,
    PromptAsset,
    PromptVersion,
    ReleaseBundleHead,
    ReleaseDeployment,
)

LABEL_VERSION_ID = "label_v1_9_0_rc2"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _headers(auth_headers: dict[str, str], key: str, *, token: str = "dev-token") -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _seed_l2_release_head(*, prompt_version_id: str, model_version: str, policy_id: str) -> None:
    with SessionLocal() as session:
        bundle_sha256 = _sha("l2-release-head-bundle")
        session.add(
            ReleaseDeployment(
                deployment_id="rd_l2_contract_head",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                environment="production",
                status="completed",
                stage="completed",
                label_version_id=LABEL_VERSION_ID,
                prompt_version_id=prompt_version_id,
                model_version=model_version,
                aggregation_policy_version_id=policy_id,
                eval_dataset_version_id="evalset-l2-contract",
                eval_run_id="evalrun-l2-contract",
                rollback_target_deployment_id=None,
                bundle_sha256=bundle_sha256,
                rollout_percentage=100,
                blocked_reasons=[],
                monitor_metrics={},
                approved_by="u_admin_001",
                trace_id="trace-l2-contract-head",
                payload={"root_trace_id": "trace-l2-contract-head"},
            )
        )
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id="rbh_l2_contract_head",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                environment="production",
                active_deployment_id="rd_l2_contract_head",
                active_bundle_sha256=bundle_sha256,
                prompt_asset_id="prompt-l2-contract-asset",
                prompt_version_id=prompt_version_id,
                label_version_id=LABEL_VERSION_ID,
                model_version=model_version,
                aggregation_policy_version_id=policy_id,
                eval_dataset_version_id="evalset-l2-contract",
                generation=1,
                status="active",
                bootstrapped=True,
                activated_by_command_id=None,
                trace_id="trace-l2-contract-head",
                payload={"root_trace_id": "trace-l2-contract-head"},
            )
        )
        session.commit()


def _seed_locked_gold(*, suffix: str, sample_count: int = 200) -> str:
    round_id = f"cal_round_{suffix}"
    gold_set_version_id = f"gold_label_{suffix}"
    now = datetime.now(UTC)
    with SessionLocal() as session:
        session.add(
            CalibrationRound(
                round_id=round_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                dataset_id=f"dataset_{suffix}",
                dataset_version="v1",
                label_version=LABEL_VERSION_ID,
                rubric_version="label-rubric-v1",
                sample_manifest_sha256=_sha(f"samples:{suffix}"),
                reviewer_a_id="reviewer-a",
                reviewer_b_id="reviewer-b",
                adjudicator_id="reviewer-c",
                status="published",
                resource_version=1,
                sample_count=sample_count,
                paired_submission_count=sample_count,
                agreed_count=sample_count,
                conflict_count=0,
                adjudication_count=0,
                excluded_count=0,
                observed_agreement_ppm=950_000,
                cohen_kappa_micros=800_000,
                cohen_kappa_defined=True,
                root_trace_id=f"trace_gold_{suffix}",
                current_trace_id=f"trace_gold_{suffix}",
                published_at=now,
            )
        )
        session.add(
            GoldSetVersion(
                gold_set_version_id=gold_set_version_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                round_id=round_id,
                gold_set_key=f"label-calibration-{suffix}",
                version_number=1,
                dataset_id=f"dataset_{suffix}",
                dataset_version="v1",
                label_version=LABEL_VERSION_ID,
                rubric_version="label-rubric-v1",
                sample_manifest_sha256=_sha(f"samples:{suffix}"),
                annotation_manifest_sha256=_sha(f"annotations:{suffix}"),
                status="published",
                resource_version=1,
                sample_count=sample_count,
                annotation_count=sample_count,
                excluded_count=0,
                observed_agreement_ppm=950_000,
                cohen_kappa_micros=800_000,
                cohen_kappa_defined=True,
                conflict_count=0,
                adjudication_count=0,
                published_by="reviewer-c",
                trace_id=f"trace_gold_{suffix}",
                published_at=now,
            )
        )
        session.commit()
    return gold_set_version_id


def _body(
    *,
    calibration_id: str,
    gold_set_version_id: str,
    method: str = "isotonic",
    source_family: str = "model-a",
) -> dict:
    parameters: dict[str, object]
    if method == "isotonic":
        parameters = {"x": [0.0, 0.5, 1.0], "y": [0.05, 0.55, 0.98]}
    elif method == "platt":
        parameters = {"a": 1.1, "b": -0.05}
    else:
        parameters = {"shrink": 0.8, "cap": 0.95}
    return {
        "calibration_version_id": calibration_id,
        "label_version_id": LABEL_VERSION_ID,
        "label_id": "*",
        "source_family": source_family,
        "version": "1.0.0",
        "method": method,
        "status": "published",
        "gold_set_version_id": gold_set_version_id,
        "parameters": parameters,
        "metrics": {"ece": 0.018, "brier": 0.074},
    }


def test_published_calibration_is_scoped_idempotent_and_append_only(client, auth_headers):
    gold_id = _seed_locked_gold(suffix="contract")
    body = _body(calibration_id="lcal_contract_v1", gold_set_version_id=gold_id)
    headers = _headers(auth_headers, "create-label-calibration-contract")

    created = client.post("/api/v1/label-calibration-versions", json=body, headers=headers)
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["status"] == "published"
    assert data["sample_count"] == 200
    assert data["gold_set_version_id"] == gold_id
    assert data["training_manifest_sha256"] == _sha("annotations:contract")
    assert len(data["content_sha256"]) == 64

    replay = client.post("/api/v1/label-calibration-versions", json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == created.json()

    detail = client.get("/api/v1/label-calibration-versions/lcal_contract_v1", headers=auth_headers)
    assert detail.status_code == 200
    listed = client.get(
        "/api/v1/label-calibration-versions",
        params={"label_version_id": LABEL_VERSION_ID, "status": "published"},
        headers=auth_headers,
    )
    assert listed.status_code == 200
    assert [item["calibration_version_id"] for item in listed.json()["data"]["items"]] == [
        "lcal_contract_v1"
    ]

    with SessionLocal() as session:
        record = session.get(LabelCalibrationVersion, "lcal_contract_v1")
        assert record is not None
        record.status = "retired"
        with pytest.raises(IntegrityError, match="append-only calibration record"):
            session.commit()
        session.rollback()


def test_calibration_publish_requires_stable_gold_and_human_authority(client, auth_headers):
    gold_id = _seed_locked_gold(suffix="too_small", sample_count=49)
    body = _body(
        calibration_id="lcal_too_small_v1",
        gold_set_version_id=gold_id,
        method="global-conservative",
    )

    unstable = client.post(
        "/api/v1/label-calibration-versions",
        json=body,
        headers=_headers(auth_headers, "create-label-calibration-too-small"),
    )
    assert unstable.status_code == 409
    assert unstable.json()["error"]["code"] == "CALIBRATION_GOLD_NOT_STABLE"

    forbidden = client.post(
        "/api/v1/label-calibration-versions",
        json=body,
        headers=_headers(
            auth_headers,
            "system-publish-label-calibration",
            token="system-token",
        ),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "AGENT_CALIBRATION_PUBLISH_FORBIDDEN"


def test_server_calibrated_independent_sources_can_auto_accept_low_risk_l2_fact(
    client, auth_headers
):
    gold_id = _seed_locked_gold(suffix="l2_auto")
    calibration_ids = {"model-a": "lcal_l2_model_a", "model-b": "lcal_l2_model_b"}
    for source_family, calibration_id in calibration_ids.items():
        created = client.post(
            "/api/v1/label-calibration-versions",
            json=_body(
                calibration_id=calibration_id,
                gold_set_version_id=gold_id,
                source_family=source_family,
            ),
            headers=_headers(auth_headers, f"create-{calibration_id}"),
        )
        assert created.status_code == 201, created.text

    model_version = "label-model-l2-contract"
    schema_version = "label-observation/1"
    prompt_version_id = "prompt-l2-contract-v1"
    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert label_version is not None
        label_version.status = "published"
        session.add(
            PromptAsset(
                prompt_asset_id="prompt-l2-contract-asset",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                name="L2 contract labeling prompt",
                capability="labeling",
                label_version_id=LABEL_VERSION_ID,
                status="active",
                current_version_id=prompt_version_id,
                trace_id="trace-l2-contract",
                payload={},
            )
        )
        session.add(
            PromptVersion(
                prompt_version_id=prompt_version_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                prompt_asset_id="prompt-l2-contract-asset",
                version="1.0.0",
                parent_version_id=None,
                label_version_id=LABEL_VERSION_ID,
                schema_version=schema_version,
                model_version=model_version,
                status="published",
                template_json={"system": "Return a locked labeling JSON document."},
                output_schema={"type": "object"},
                generation_params={"temperature": 0},
                structured_diff={},
                source_badcase_refs=[],
                content_sha256=_sha("prompt-l2-contract-v1"),
                trace_id="trace-l2-contract",
            )
        )
        session.commit()

    policy_id = "lap_l2_server_calibrated"
    policy = client.post(
        "/api/v1/label-aggregation-policies",
        json={
            "policy_version_id": policy_id,
            "label_version_id": LABEL_VERSION_ID,
            "policy_version": "1.0.0",
            "mode": "l2",
            "status": "active",
            "source_weights": {"model-a": 1.0, "model-b": 1.0},
            "calibration_versions": calibration_ids,
            "thresholds": {
                "l2_accept_score": 0.95,
                "categorical_margin": 0.15,
                "temporal_iou": 0.6,
                "min_independent_sources": 2,
                "random_audit_rate": 0.05,
            },
            "label_definitions": [
                {
                    "label_id": "refund-request",
                    "canonical_name": "申请退款",
                    "aliases": ["退款申请"],
                    "kind": "boolean",
                    "risk_level": "low",
                    "parent_ids": [],
                }
            ],
        },
        headers=_headers(auth_headers, "create-l2-server-calibrated-policy"),
    )
    assert policy.status_code == 201, policy.text
    _seed_l2_release_head(
        prompt_version_id=prompt_version_id,
        model_version=model_version,
        policy_id=policy_id,
    )

    extraction_run_id = "lexr_l2_contract"
    subject_key = "session-l2-contract"
    evidence_id = "segment-l2-contract"
    input_sha256 = _sha(subject_key)
    extraction = client.post(
        "/api/v1/label-extraction-runs",
        json={
            "extraction_run_id": extraction_run_id,
            "label_version_id": LABEL_VERSION_ID,
            "prompt_version_id": prompt_version_id,
            "model_version": model_version,
            "schema_version": schema_version,
            "aggregation_policy_version_id": policy_id,
            "subject_scope": "audio-session",
            "subject_refs": [{"subject_key": subject_key, "evidence_ref": evidence_id}],
            "source_bindings": [
                {
                    "source_family": source_family,
                    "source_type": "model",
                    "provider": f"provider-{source_family}",
                    "adapter": "label-contract-adapter",
                }
                for source_family in calibration_ids
            ],
            "input_sha256": input_sha256,
            "execution_mode": "production",
        },
        headers=_headers(auth_headers, "create-l2-contract-extraction"),
    )
    assert extraction.status_code == 202, extraction.text

    observation_ids: list[str] = []
    for source_family, calibration_id in calibration_ids.items():
        observation_id = f"lob_l2_{source_family.replace('-', '_')}"
        observation_ids.append(observation_id)
        observation = client.post(
            "/api/v1/label-observations",
            json={
                "observation_id": observation_id,
                "extraction_run_id": extraction_run_id,
                "subject_scope": "audio-session",
                "subject_key": subject_key,
                "evidence_ref": {
                    "type": "audio-segment",
                    "id": evidence_id,
                    "sha256": _sha(evidence_id),
                },
                "label_version_id": LABEL_VERSION_ID,
                "raw_label": "退款申请",
                "label_id": "refund-request",
                "value": True,
                "value_type": "boolean",
                "source_family": source_family,
                "source_type": "model",
                "model_version": model_version,
                "prompt_version_id": prompt_version_id,
                "schema_version": schema_version,
                "calibration_version_id": calibration_id,
                "raw_confidence": 0.99,
                "input_sha256": input_sha256,
                "output_sha256": _sha(f"{observation_id}:true"),
            },
            headers=_headers(
                auth_headers,
                f"create-{observation_id}",
                token="system-token",
            ),
        )
        assert observation.status_code == 201, observation.text
        assert observation.json()["data"]["calibrated_confidence"] > 0.95
        assert observation.json()["data"]["source_lineage"]["server_locked"] is True

    aggregation = client.post(
        "/api/v1/label-aggregation-runs",
        json={
            "aggregation_run_id": "lagr_l2_contract",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version_id": policy_id,
            "observation_ids": observation_ids,
            "mode": "l2",
        },
        headers=_headers(auth_headers, "aggregate-l2-contract"),
    )
    assert aggregation.status_code == 202, aggregation.text
    aggregate_id = aggregation.json()["data"]["aggregate_ids"][0]
    aggregate = client.get(f"/api/v1/label-aggregates/{aggregate_id}", headers=auth_headers)
    assert aggregate.status_code == 200
    assert aggregate.json()["data"]["decision"] == "auto-accept"
    assert aggregate.json()["data"]["review_task_id"] is None

    with SessionLocal() as session:
        fact = session.query(LabelFact).filter_by(aggregate_id=aggregate_id).one()
        assert fact.authority == "l2-auto-accepted"
        assert fact.status == "active"
        assert fact.value_json is True
