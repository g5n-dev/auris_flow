from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import RunRecord, StorageObject
from app.schemas.requests import RunCompletionReceiptRequest
from app.services.promptfoo_eval_adapter import (
    PROMPTFOO_EVAL_SUITES,
    PromptfooAdapterConfig,
    PromptfooArtifactReference,
    PromptfooCliAdapter,
    PromptfooEvalRequest,
    PromptfooLockedBundle,
    PromptfooLockedVersions,
    PromptfooMaterializedPaths,
    PromptfooResultArtifactDocument,
    build_promptfoo_completion_payload,
    canonical_json_bytes,
    serialize_promptfoo_result_artifact,
)

TENANT_ID = "aurora_auto"
PROJECT_ID = "sales_qa"
EVAL_RUN_ID = "eval_promptfoo_contract"
DISPATCH_EXTERNAL_ID = "dagster-eval-promptfoo-contract"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _locked_versions() -> PromptfooLockedVersions:
    return PromptfooLockedVersions(
        scene_profile_id="scene_profile_demo",
        scene_profile_version_id="scene_profile_demo_v1",
        scene_profile_snapshot_sha256="0" * 64,
        eval_dataset_version_id="eval-dataset-v1",
        eval_dataset_manifest_sha256="1" * 64,
        eval_dataset_snapshot_sha256="2" * 64,
        eval_dataset_manifest_storage_object_id="sto_eval_manifest",
        eval_dataset_resource_version=3,
        label_version_id="label-v1",
        label_resource_version=7,
        prompt_version_id="prompt-v2",
        prompt_content_sha256="3" * 64,
        model_version="provider/model-v1",
        aggregation_policy_version_id="policy-v1",
        aggregation_policy_sha256="4" * 64,
        optimization_run_id="opt-v1",
        optimization_run_lock_sha256="5" * 64,
        evaluation_suites=list(PROMPTFOO_EVAL_SUITES),
    )


def _bundle() -> PromptfooLockedBundle:
    locked = _locked_versions()
    return PromptfooLockedBundle(
        binding_sha256=_sha(canonical_json_bytes(locked.model_dump(mode="json")).decode("ascii")),
        locked_versions=locked,
    )


def _request(config_sha256: str = "6" * 64) -> PromptfooEvalRequest:
    return PromptfooEvalRequest(
        eval_run_id=EVAL_RUN_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        dispatch_adapter="dagster",
        dispatch_external_id=DISPATCH_EXTERNAL_ID,
        bundle=_bundle(),
        config_artifact=PromptfooArtifactReference(
            storage_object_id="sto_promptfoo_config",
            content_sha256=config_sha256,
        ),
    )


def _eval_run() -> RunRecord:
    return RunRecord(
        run_id=EVAL_RUN_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        run_type="eval_run",
        status="submitted",
        run_key=f"eval:{EVAL_RUN_ID}",
        partition_key=f"{TENANT_ID}/{PROJECT_ID}",
        trace_id="trace-promptfoo",
        payload={
            "capability": "labeling",
            "binding_sha256": _bundle().binding_sha256,
            "locked_versions": _locked_versions().model_dump(mode="json"),
            "business_completion_required": True,
            "dispatch": {
                "adapter": "dagster",
                "status": "success",
                "details": {
                    "external_run_id": DISPATCH_EXTERNAL_ID,
                    "dagster_run_id": DISPATCH_EXTERNAL_ID,
                },
            },
        },
    )


def _metrics() -> dict[str, Any]:
    return {
        "macro_f1": 0.92,
        "macro_f1_gain_pp": 2.5,
        "critical_recall_delta_pp": 0,
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


def _result_document() -> PromptfooResultArtifactDocument:
    suite_manifests = [
        {
            "suite": suite,
            "sample_count": 10,
            "sample_manifest_sha256": _sha(f"manifest:{suite}"),
        }
        for suite in sorted(PROMPTFOO_EVAL_SUITES)
    ]
    sample_manifest_sha256 = _sha(
        json.dumps(
            suite_manifests,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    result = {
        "binding_sha256": _bundle().binding_sha256,
        "dataset_manifest_sha256": "1" * 64,
        "dataset_snapshot_sha256": "2" * 64,
        "sample_manifest_sha256": sample_manifest_sha256,
        "hidden_holdout_used": True,
        "dev_set_used": False,
        "suites": [
            {
                "suite": suite,
                "sample_count": 10,
                "sample_manifest_sha256": _sha(f"manifest:{suite}"),
                "metrics": _metrics(),
            }
            for suite in PROMPTFOO_EVAL_SUITES
        ],
        "overall": _metrics(),
        "paired_bootstrap": {
            "method": "paired-bootstrap-v1",
            "confidence_level": 0.95,
            "resample_count": 10_000,
            "random_seed": 42,
            "paired_sample_count": 60,
            "macro_f1_gain_lower_pp": 1.2,
            "macro_f1_gain_upper_pp": 3.7,
            "critical_recall_delta_lower_pp": -0.2,
            "critical_recall_delta_upper_pp": 0.3,
        },
    }
    return PromptfooResultArtifactDocument.model_validate(
        {
            "schema_version": "auris.promptfoo-eval-result.v1",
            "eval_run_id": EVAL_RUN_ID,
            "binding_sha256": _bundle().binding_sha256,
            "provider_run_id": "promptfoo-ci-42",
            "labeling_eval_result": result,
            "provider_metadata": {
                "provider": "promptfoo",
                "provider_version": "0.100.0",
            },
        }
    )


def _storage_object(
    *,
    storage_object_id: str,
    source_type: str,
    source_id: str,
    content_type: str,
    content_sha256: str,
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> StorageObject:
    object_key = (
        f"tenants/{tenant_id}/projects/{project_id}/promptfoo/"
        f"{EVAL_RUN_ID}/{storage_object_id}.json"
    )
    return StorageObject(
        storage_object_id=storage_object_id,
        tenant_id=tenant_id,
        project_id=project_id,
        provider="minio",
        bucket="auris-flow-local",
        object_key=object_key,
        object_key_sha256=_sha(object_key),
        source_type=source_type,
        source_id=source_id,
        content_type=content_type,
        size_bytes=256,
        content_sha256=content_sha256,
        etag="promptfoo-etag",
        status="verified",
        trace_id="trace-promptfoo",
        payload={
            "authority": "artifact-only",
            "binding_sha256": _bundle().binding_sha256,
        },
    )


def test_disabled_and_optional_unavailable_modes_are_non_blocking(tmp_path: Path) -> None:
    config_path = tmp_path / "promptfooconfig.yaml"
    config_path.write_text("prompts: []", encoding="utf-8")
    paths = PromptfooMaterializedPaths(
        sandbox_root=tmp_path,
        config_path=config_path,
        result_path=tmp_path / "result.json",
    )

    disabled = PromptfooCliAdapter(
        PromptfooAdapterConfig(mode="disabled"), executable_resolver=lambda _: "/bin/false"
    ).plan(_request(), paths)
    unavailable = PromptfooCliAdapter(
        PromptfooAdapterConfig(mode="optional"), executable_resolver=lambda _: None
    ).plan(_request(), paths)

    assert disabled.status == "disabled"
    assert disabled.argv == ()
    assert unavailable.status == "unavailable"
    assert unavailable.argv == ()
    assert unavailable.required is False


def test_adapter_config_reads_only_disabled_or_optional_modes() -> None:
    config = PromptfooAdapterConfig.from_env(
        {
            "AURIS_PROMPTFOO_ADAPTER": "optional",
            "PROMPTFOO_EXECUTABLE": "/opt/promptfoo/bin/promptfoo",
            "PROMPTFOO_TIMEOUT_SECONDS": "300",
        }
    )

    assert config.mode == "optional"
    assert config.timeout_seconds == 300
    with pytest.raises(ValueError, match="disabled or optional"):
        PromptfooAdapterConfig.from_env({"AURIS_PROMPTFOO_ADAPTER": "required"})


def test_optional_adapter_builds_shell_free_argv_inside_sandbox(tmp_path: Path) -> None:
    config_path = tmp_path / "config;touch-NOT-EXECUTED.yaml"
    config_path.write_text("prompts: []", encoding="utf-8")
    result_path = tmp_path / "result $(also-not-executed).json"
    plan = PromptfooCliAdapter(
        PromptfooAdapterConfig(mode="optional", timeout_seconds=120),
        executable_resolver=lambda _: "/opt/promptfoo/bin/promptfoo",
    ).plan(
        _request(),
        PromptfooMaterializedPaths(
            sandbox_root=tmp_path,
            config_path=config_path,
            result_path=result_path,
        ),
    )

    assert plan.status == "ready"
    assert plan.argv == (
        "/opt/promptfoo/bin/promptfoo",
        "eval",
        "--config",
        str(config_path.resolve()),
        "--output",
        str(result_path.resolve()),
        "--no-progress-bar",
    )
    assert plan.shell is False
    assert plan.timeout_seconds == 120
    assert plan.config_artifact.storage_object_id == "sto_promptfoo_config"


def test_adapter_rejects_materialized_paths_outside_sandbox(tmp_path: Path) -> None:
    config_path = tmp_path / "promptfoo.yaml"
    config_path.write_text("prompts: []", encoding="utf-8")

    with pytest.raises(ValueError, match="sandbox_root"):
        PromptfooCliAdapter(
            PromptfooAdapterConfig(mode="optional"),
            executable_resolver=lambda _: "/opt/promptfoo/bin/promptfoo",
        ).plan(
            _request(),
            PromptfooMaterializedPaths(
                sandbox_root=tmp_path,
                config_path=config_path,
                result_path=tmp_path.parent / "escaped-result.json",
            ),
        )


def test_locked_bundle_rejects_binding_hash_drift() -> None:
    with pytest.raises(ValidationError, match="binding_sha256"):
        PromptfooLockedBundle(
            binding_sha256="f" * 64,
            locked_versions=_locked_versions(),
        )


def test_completion_payload_validates_artifacts_and_matches_internal_eval_schema() -> None:
    document = _result_document()
    document_bytes = serialize_promptfoo_result_artifact(document)
    result_sha256 = hashlib.sha256(document_bytes).hexdigest()
    request = _request()

    with SessionLocal() as session:
        session.add_all(
            [
                _eval_run(),
                _storage_object(
                    storage_object_id=request.config_artifact.storage_object_id,
                    source_type="promptfoo_eval_config",
                    source_id=EVAL_RUN_ID,
                    content_type="application/yaml",
                    content_sha256=request.config_artifact.content_sha256,
                ),
                _storage_object(
                    storage_object_id="sto_promptfoo_result",
                    source_type="promptfoo_eval_result",
                    source_id=EVAL_RUN_ID,
                    content_type="application/json",
                    content_sha256=result_sha256,
                ),
            ]
        )
        session.flush()

        payload = build_promptfoo_completion_payload(
            session,
            request=request,
            result_artifact=PromptfooArtifactReference(
                storage_object_id="sto_promptfoo_result",
                content_sha256=result_sha256,
            ),
            result_document=document.model_dump(mode="json"),
            completion_adapter="dagster",
        )

    validated = RunCompletionReceiptRequest.model_validate(payload)
    assert validated.status == "success"
    assert validated.adapter == "dagster"
    assert validated.source == "dagster"
    assert validated.external_id == DISPATCH_EXTERNAL_ID
    assert validated.external_id != document.provider_run_id
    assert validated.completion_receipt_id == public_id_from_hex(
        "promptfoo",
        result_sha256,
        suffix_length=24,
    )
    assert validated.result_ref["labeling_eval_result"]["binding_sha256"] == (
        request.bundle.binding_sha256
    )
    provider_evidence = validated.result_ref["provider_evidence"]
    assert provider_evidence["authoritative"] is False
    assert provider_evidence["provider_run_id"] == document.provider_run_id
    assert provider_evidence["fact_source"] == "internal-label-eval-result"
    assert provider_evidence["result_artifact"]["object_id"] == "sto_promptfoo_result"


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("wrong_scope", "STORAGE_OBJECT_SCOPE_FORBIDDEN"),
        ("wrong_hash", "STORAGE_OBJECT_CONTENT_HASH_MISMATCH"),
        ("wrong_source", "PROMPTFOO_RESULT_ARTIFACT_BINDING_MISMATCH"),
    ),
)
def test_completion_rejects_untrusted_result_artifact(
    mutation: str,
    expected_code: str,
) -> None:
    document = _result_document()
    result_sha256 = hashlib.sha256(serialize_promptfoo_result_artifact(document)).hexdigest()
    request = _request()
    tenant_id = "other_tenant" if mutation == "wrong_scope" else TENANT_ID
    stored_hash = "9" * 64 if mutation == "wrong_hash" else result_sha256
    source_id = "another_eval" if mutation == "wrong_source" else EVAL_RUN_ID

    with SessionLocal() as session:
        session.add_all(
            [
                _eval_run(),
                _storage_object(
                    storage_object_id=request.config_artifact.storage_object_id,
                    source_type="promptfoo_eval_config",
                    source_id=EVAL_RUN_ID,
                    content_type="application/yaml",
                    content_sha256=request.config_artifact.content_sha256,
                ),
                _storage_object(
                    storage_object_id="sto_promptfoo_result",
                    source_type="promptfoo_eval_result",
                    source_id=source_id,
                    content_type="application/json",
                    content_sha256=stored_hash,
                    tenant_id=tenant_id,
                ),
            ]
        )
        session.flush()

        with pytest.raises(ApiError) as exc_info:
            build_promptfoo_completion_payload(
                session,
                request=request,
                result_artifact=PromptfooArtifactReference(
                    storage_object_id="sto_promptfoo_result",
                    content_sha256=result_sha256,
                ),
                result_document=document.model_dump(mode="json"),
                completion_adapter="dagster",
            )

    assert exc_info.value.code == expected_code


def test_completion_rejects_adapter_or_external_id_drift_from_live_dispatch() -> None:
    document = _result_document()
    result_sha256 = hashlib.sha256(serialize_promptfoo_result_artifact(document)).hexdigest()
    request = _request()

    with SessionLocal() as session:
        run = _eval_run()
        run.payload = {
            **run.payload,
            "dispatch": {
                "adapter": "dagster",
                "status": "success",
                "details": {"external_run_id": "dagster-other-run"},
            },
        }
        session.add_all(
            [
                run,
                _storage_object(
                    storage_object_id=request.config_artifact.storage_object_id,
                    source_type="promptfoo_eval_config",
                    source_id=EVAL_RUN_ID,
                    content_type="application/yaml",
                    content_sha256=request.config_artifact.content_sha256,
                ),
                _storage_object(
                    storage_object_id="sto_promptfoo_result",
                    source_type="promptfoo_eval_result",
                    source_id=EVAL_RUN_ID,
                    content_type="application/json",
                    content_sha256=result_sha256,
                ),
            ]
        )
        session.flush()

        with pytest.raises(ApiError) as exc_info:
            build_promptfoo_completion_payload(
                session,
                request=request,
                result_artifact=PromptfooArtifactReference(
                    storage_object_id="sto_promptfoo_result",
                    content_sha256=result_sha256,
                ),
                result_document=document.model_dump(mode="json"),
                completion_adapter="dagster",
            )

    assert exc_info.value.code == "PROMPTFOO_EVAL_DISPATCH_BINDING_MISMATCH"


def test_result_document_binding_must_match_locked_bundle() -> None:
    document = _result_document().model_copy(update={"binding_sha256": "f" * 64})

    with SessionLocal() as session:
        with pytest.raises(ApiError) as exc_info:
            build_promptfoo_completion_payload(
                session,
                request=_request(),
                result_artifact=PromptfooArtifactReference(
                    storage_object_id="sto_promptfoo_result",
                    content_sha256="f" * 64,
                ),
                result_document=document.model_dump(mode="json"),
                completion_adapter="dagster",
            )

    assert exc_info.value.code == "PROMPTFOO_RESULT_BUNDLE_MISMATCH"
