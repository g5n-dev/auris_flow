from __future__ import annotations

import hashlib
import json

from app.core.database import SessionLocal
from app.models import (
    LabelAggregationPolicyVersion,
    LabelEvalResult,
    LabelEvalSuiteResult,
    LabelVersion,
    PromptAsset,
    PromptVersion,
    RunRecord,
    StorageObject,
)
from app.services.promptfoo_eval_adapter import (
    PROMPTFOO_EVAL_SUITES,
    PromptfooArtifactReference,
    PromptfooEvalRequest,
    PromptfooLockedBundle,
    PromptfooLockedVersions,
    PromptfooResultArtifactDocument,
    build_promptfoo_completion_payload,
    serialize_promptfoo_result_artifact,
)
from app.workers.outbox_worker import process_aggregate_events

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
EVAL_RUN_ID = "eval_promptfoo_integration"
DATASET_ID = "evalset_promptfoo_integration"
LABEL_VERSION_ID = "label_promptfoo_integration"
PROMPT_ASSET_ID = "prompt_asset_promptfoo_integration"
PROMPT_VERSION_ID = "prompt_promptfoo_integration_v1"
POLICY_VERSION_ID = "policy_promptfoo_integration_v1"
OPTIMIZATION_RUN_ID = "optimization_promptfoo_integration"
MODEL_VERSION = "provider/model-promptfoo-integration"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _headers(
    auth_headers: dict[str, str],
    idempotency_key: str,
    *,
    token: str = "dev-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idempotency_key,
    }


def _seed_manifest_storage_object() -> str:
    content_sha256 = _sha("promptfoo-integration-eval-manifest")
    object_key = (
        f"tenants/{TENANT_ID}/projects/{PROJECT_ID}/eval-datasets/{DATASET_ID}/manifest.jsonl"
    )
    with SessionLocal() as session:
        session.add(
            StorageObject(
                storage_object_id="sto_promptfoo_integration_manifest",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                provider="minio",
                bucket="auris-flow-local",
                object_key=object_key,
                object_key_sha256=_sha(object_key),
                source_type="eval_dataset_manifest",
                source_id=DATASET_ID,
                content_type="application/x-ndjson",
                size_bytes=2048,
                content_sha256=content_sha256,
                etag=f'"{content_sha256[:16]}"',
                status="verified",
                trace_id="trace-promptfoo-integration",
                payload={"verified": True},
            )
        )
        session.commit()
    return content_sha256


def _seed_eval_dependencies() -> None:
    with SessionLocal() as session:
        session.add_all(
            [
                LabelVersion(
                    label_version_id=LABEL_VERSION_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="published",
                    resource_version=1,
                    trace_id="trace-promptfoo-integration",
                    payload={},
                ),
                LabelAggregationPolicyVersion(
                    policy_version_id=POLICY_VERSION_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    label_version_id=LABEL_VERSION_ID,
                    policy_version="1.0.0",
                    mode="l1",
                    status="active",
                    source_weights={"llm": 1.0},
                    calibration_versions={},
                    thresholds={},
                    label_definitions=[{"label_id": "intent", "kind": "categorical"}],
                    canonical_sha256=_sha("promptfoo-integration-policy"),
                    trace_id="trace-promptfoo-integration",
                    payload={},
                ),
                PromptAsset(
                    prompt_asset_id=PROMPT_ASSET_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    name="Promptfoo 集成评测 Prompt",
                    capability="labeling",
                    label_version_id=LABEL_VERSION_ID,
                    status="active",
                    current_version_id=PROMPT_VERSION_ID,
                    trace_id="trace-promptfoo-integration",
                    payload={},
                ),
                PromptVersion(
                    prompt_version_id=PROMPT_VERSION_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    prompt_asset_id=PROMPT_ASSET_ID,
                    version="1.0.0",
                    parent_version_id=None,
                    label_version_id=LABEL_VERSION_ID,
                    schema_version="label-output-v1",
                    model_version=MODEL_VERSION,
                    status="approved",
                    template_json={"system": "只输出符合 Schema 的 JSON"},
                    output_schema={"type": "object"},
                    generation_params={"temperature": 0},
                    structured_diff={},
                    source_badcase_refs=[],
                    content_sha256=_sha("promptfoo-integration-prompt"),
                    trace_id="trace-promptfoo-integration",
                ),
                RunRecord(
                    run_id=OPTIMIZATION_RUN_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    run_type="label_optimization",
                    status="success",
                    run_key=f"label-opt:{OPTIMIZATION_RUN_ID}",
                    partition_key=f"{TENANT_ID}/{PROJECT_ID}/{LABEL_VERSION_ID}",
                    trace_id="trace-promptfoo-integration",
                    payload={
                        "label_version_id": LABEL_VERSION_ID,
                        "prompt_version_id": PROMPT_VERSION_ID,
                        "prompt_candidate_ids": [],
                        "model_version": MODEL_VERSION,
                        "aggregation_policy_version_id": POLICY_VERSION_ID,
                        "eval_dataset_version_id": DATASET_ID,
                        "trigger_hash": _sha("promptfoo-integration-trigger"),
                    },
                ),
            ]
        )
        session.commit()


def _metrics() -> dict[str, object]:
    return {
        "macro_f1": 0.92,
        "macro_f1_gain_pp": 2.5,
        "critical_recall_delta_pp": 0.0,
        "json_valid_rate": 0.999,
        "coverage_rate": 0.98,
        "conflict_rate": 0.02,
        "cost_ratio": 1.02,
        "latency_ratio": 1.05,
        "quality_passed": True,
        "security_passed": True,
        "format_passed": True,
        "cost_passed": True,
        "latency_passed": True,
        "observability_passed": True,
    }


def _result_document(
    *,
    bundle: PromptfooLockedBundle,
) -> PromptfooResultArtifactDocument:
    suites = [
        {
            "suite": suite,
            "sample_count": 10,
            "sample_manifest_sha256": _sha(f"promptfoo-integration:{suite}"),
            "metrics": _metrics(),
        }
        for suite in PROMPTFOO_EVAL_SUITES
    ]
    sample_manifest = [
        {
            "suite": item["suite"],
            "sample_count": item["sample_count"],
            "sample_manifest_sha256": item["sample_manifest_sha256"],
        }
        for item in sorted(suites, key=lambda item: str(item["suite"]))
    ]
    locked = bundle.locked_versions
    return PromptfooResultArtifactDocument.model_validate(
        {
            "schema_version": "auris.promptfoo-eval-result.v1",
            "eval_run_id": EVAL_RUN_ID,
            "binding_sha256": bundle.binding_sha256,
            "provider_run_id": "promptfoo-provider-run-not-dispatch-id",
            "labeling_eval_result": {
                "binding_sha256": bundle.binding_sha256,
                "dataset_manifest_sha256": locked.eval_dataset_manifest_sha256,
                "dataset_snapshot_sha256": locked.eval_dataset_snapshot_sha256,
                "sample_manifest_sha256": _canonical_sha256(sample_manifest),
                "hidden_holdout_used": True,
                "dev_set_used": False,
                "suites": suites,
                "overall": _metrics(),
                "paired_bootstrap": {
                    "method": "paired-bootstrap-v1",
                    "confidence_level": 0.95,
                    "resample_count": 10_000,
                    "random_seed": 20260715,
                    "paired_sample_count": 60,
                    "macro_f1_gain_lower_pp": 1.2,
                    "macro_f1_gain_upper_pp": 3.7,
                    "critical_recall_delta_lower_pp": -0.2,
                    "critical_recall_delta_upper_pp": 0.3,
                },
            },
            "provider_metadata": {
                "provider": "promptfoo",
                "provider_version": "integration-test",
            },
        }
    )


def _artifact_object(
    *,
    storage_object_id: str,
    source_type: str,
    content_type: str,
    content_sha256: str,
    binding_sha256: str,
) -> StorageObject:
    object_key = (
        f"tenants/{TENANT_ID}/projects/{PROJECT_ID}/promptfoo/{EVAL_RUN_ID}/{storage_object_id}"
    )
    return StorageObject(
        storage_object_id=storage_object_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        provider="minio",
        bucket="auris-flow-local",
        object_key=object_key,
        object_key_sha256=_sha(object_key),
        source_type=source_type,
        source_id=EVAL_RUN_ID,
        content_type=content_type,
        size_bytes=1024,
        content_sha256=content_sha256,
        etag=f'"{content_sha256[:16]}"',
        status="verified",
        trace_id="trace-promptfoo-integration",
        payload={
            "authority": "artifact-only",
            "binding_sha256": binding_sha256,
        },
    )


def test_promptfoo_artifact_completes_real_dispatched_eval_and_materializes_fact(
    client,
    auth_headers,
) -> None:
    manifest_sha256 = _seed_manifest_storage_object()
    created_dataset = client.post(
        "/api/v1/eval-datasets",
        json={
            "eval_dataset_id": DATASET_ID,
            "name": "Promptfoo 完整链路锁定集",
            "capability": "labeling",
            "dataset_version": "1.0.0",
            "manifest_storage_object_id": "sto_promptfoo_integration_manifest",
            "manifest_sha256": manifest_sha256,
            "sample_count": 60,
            "source": "integration-test",
        },
        headers=_headers(auth_headers, "promptfoo-create-eval-dataset"),
    )
    assert created_dataset.status_code == 201, created_dataset.text
    locked_dataset = client.post(
        f"/api/v1/eval-datasets/{DATASET_ID}/lock",
        json={"expected_resource_version": 1, "confirmation": "lock"},
        headers=_headers(auth_headers, "promptfoo-lock-eval-dataset"),
    )
    assert locked_dataset.status_code == 200, locked_dataset.text
    _seed_eval_dependencies()

    created_run = client.post(
        "/api/v1/eval-runs",
        json={
            "run_id": EVAL_RUN_ID,
            "capability": "labeling",
            "eval_dataset_version_id": DATASET_ID,
            "label_version_id": LABEL_VERSION_ID,
            "prompt_version_id": PROMPT_VERSION_ID,
            "model_version": MODEL_VERSION,
            "aggregation_policy_version_id": POLICY_VERSION_ID,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "evaluation_suites": list(PROMPTFOO_EVAL_SUITES),
        },
        headers=_headers(
            auth_headers,
            "promptfoo-create-locked-eval",
            token="model-token",
        ),
    )
    assert created_run.status_code == 202, created_run.text
    assert process_aggregate_events([EVAL_RUN_ID]) == 1

    with SessionLocal() as session:
        eval_run = session.get(RunRecord, EVAL_RUN_ID)
        assert eval_run is not None and eval_run.status == "submitted"
        dispatch = eval_run.payload["dispatch"]
        assert dispatch["adapter"] == "dagster"
        dispatch_external_id = dispatch["details"]["external_run_id"]
        locked_versions = PromptfooLockedVersions.model_validate(
            eval_run.payload["locked_versions"]
        )
        bundle = PromptfooLockedBundle(
            binding_sha256=eval_run.payload["binding_sha256"],
            locked_versions=locked_versions,
        )

    config_sha256 = _sha("promptfoo-integration-config")
    request = PromptfooEvalRequest(
        eval_run_id=EVAL_RUN_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        dispatch_adapter="dagster",
        dispatch_external_id=dispatch_external_id,
        bundle=bundle,
        config_artifact=PromptfooArtifactReference(
            storage_object_id="sto_promptfoo_integration_config",
            content_sha256=config_sha256,
        ),
    )
    result_document = _result_document(bundle=bundle)
    result_sha256 = hashlib.sha256(serialize_promptfoo_result_artifact(result_document)).hexdigest()
    with SessionLocal() as session:
        session.add_all(
            [
                _artifact_object(
                    storage_object_id="sto_promptfoo_integration_config",
                    source_type="promptfoo_eval_config",
                    content_type="application/yaml",
                    content_sha256=config_sha256,
                    binding_sha256=bundle.binding_sha256,
                ),
                _artifact_object(
                    storage_object_id="sto_promptfoo_integration_result",
                    source_type="promptfoo_eval_result",
                    content_type="application/json",
                    content_sha256=result_sha256,
                    binding_sha256=bundle.binding_sha256,
                ),
            ]
        )
        session.commit()
        completion_payload = build_promptfoo_completion_payload(
            session,
            request=request,
            result_artifact=PromptfooArtifactReference(
                storage_object_id="sto_promptfoo_integration_result",
                content_sha256=result_sha256,
            ),
            result_document=result_document.model_dump(mode="json"),
        )

    assert completion_payload["adapter"] == "dagster"
    assert completion_payload["source"] == "dagster"
    assert completion_payload["external_id"] == dispatch_external_id
    assert completion_payload["external_id"] != result_document.provider_run_id
    assert (
        completion_payload["result_ref"]["provider_evidence"]["provider_run_id"]
        == result_document.provider_run_id
    )
    assert completion_payload["result_ref"]["provider_evidence"]["authoritative"] is False

    completed = client.post(
        f"/api/v1/runs/{EVAL_RUN_ID}/completion-receipts",
        json=completion_payload,
        headers=_headers(auth_headers, "promptfoo-complete-eval-run"),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["data"]["status"] == "success"
    assert completed.json()["data"]["label_eval_result"]["status"] == "passed"

    with SessionLocal() as session:
        fact = session.query(LabelEvalResult).filter_by(eval_run_id=EVAL_RUN_ID).one()
        suites = session.query(LabelEvalSuiteResult).filter_by(eval_result_id=fact.eval_result_id)
        eval_run = session.get(RunRecord, EVAL_RUN_ID)
        assert fact.status == "passed"
        assert suites.count() == 6
        assert eval_run is not None
        receipt = eval_run.payload["completion_receipt"]
        assert receipt["external_id"] == dispatch_external_id
        assert receipt["source"] == "dagster"
        provider_evidence = receipt["result_ref"]["provider_evidence"]
        assert provider_evidence["provider_run_id"] == result_document.provider_run_id
        assert provider_evidence["authoritative"] is False
