from __future__ import annotations

import json
import unicodedata
from typing import Any

from app.api.routers.generic import export_job_payload
from app.core.database import SessionLocal
from app.core.json_keys import json_key_fingerprint
from app.models import LabelExtractionRun, PromptVersionCandidate, RunRecord
from app.services.label_closed_loop_service import extraction_run_data
from app.services.label_optimization_orchestrator import _serialize_run

FORBIDDEN_FIELDS = frozenset(
    json_key_fingerprint(field)
    for field in (
        "dispatch",
        "endpoint",
        "bucket",
        "object_key",
        "object_uri",
        "storage_object_id",
        "external_run_id",
        "details",
        "provider",
        "adapter",
    )
)
FORBIDDEN_VALUE_CANARIES = (
    "dagster",
    "graphql",
    "internal-bucket-canary",
    "internal-object-key-canary",
    "storage-object-internal-canary",
    "external-run-internal-canary",
    "secret-bearer-canary",
    "url-password-canary",
)


def _security_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return "".join(char for char in normalized if unicodedata.category(char) != "Cf").casefold()


def _assert_closed_public_projection(value: Any, *, path: str = "data") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            assert json_key_fingerprint(str(key)) not in FORBIDDEN_FIELDS, f"{path}.{key}"
            _assert_closed_public_projection(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_closed_public_projection(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        normalized = _security_text(value)
        for canary in FORBIDDEN_VALUE_CANARIES:
            assert canary not in normalized, f"{path}: {value!r}"


def _unsafe_dispatch() -> dict[str, Any]:
    return {
        "adapter": "Ｄａｇｓｔｅｒ",
        "operation": "Graph\u200bQL launch",
        "details": {
            "endpoint": "https://user:url-password-canary@internal.example/graphql",
            "bucket": "internal-bucket-canary",
            "object_key": "internal-object-key-canary",
            "object_uri": "s3://internal-bucket-canary/internal-object-key-canary",
            "storage_object_id": "storage-object-internal-canary",
            "external_run_id": "external-run-internal-canary",
        },
    }


def test_export_and_label_run_derivatives_share_closed_public_projection() -> None:
    run = RunRecord(
        run_id="run_derived_projection",
        tenant_id="tenant_derived_projection",
        project_id="project_derived_projection",
        run_type="export",
        status="success",
        trace_id="trace_derived_projection",
        payload={
            "format": "jsonl",
            "target": "evidence_pack",
            "object_id": "evidence_001",
            "dispatch": {
                **_unsafe_dispatch(),
                "adapter": "object_storage",
                "details": {
                    **_unsafe_dispatch()["details"],
                    "provider": "minio",
                    "bucket": "auris-flow-local",
                    "object_key": (
                        "tenants/tenant_derived_projection/projects/"
                        "project_derived_projection/exports/run_derived_projection.jsonl"
                    ),
                    "etag": "export-etag-v1",
                    "content_length": 128,
                    "content_type": "application/jsonl",
                },
            },
            "next_actions": [
                {
                    "key": "view_result",
                    "label": "Graph\u200bQL Ｄａｇｓｔｅｒ Bearer secret-bearer-canary",
                }
            ],
        },
    )
    extraction = LabelExtractionRun(
        extraction_run_id=run.run_id,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        label_version_id="label_v1",
        prompt_version_id="prompt_v1",
        model_version="model_v1",
        schema_version="schema_v1",
        status="submitted",
        subject_scope="audio-session",
        subject_refs=[{"id": "audio_001"}],
        input_sha256="a" * 64,
        observation_count=0,
        trace_id=run.trace_id,
        payload={
            "aggregation_policy_version_id": "policy_v1",
            "source_bindings": [
                {
                    "source_family": "model-v1",
                    "source_type": "model",
                    "provider": "internal-provider-canary",
                    "adapter": "internal-adapter-canary",
                    "correlation_group_id": "correlation-public-001",
                    "endpoint": "https://user:url-password-canary@internal.example/graphql",
                }
            ],
        },
    )

    export_projection = export_job_payload(run)
    extraction_projection = extraction_run_data(extraction, run)

    _assert_closed_public_projection(export_projection)
    _assert_closed_public_projection(extraction_projection)
    assert export_projection["download_ref"]["href"] == (
        "/api/v1/exports/run_derived_projection/download"
    )
    assert set(export_projection["download_ref"]) == {
        "kind",
        "status",
        "href",
        "content_type",
        "expires_at",
    }
    assert "dispatch" not in extraction_projection
    assert extraction_projection["source_bindings"] == [
        {
            "source_family": "model-v1",
            "source_type": "model",
            "correlation_group_id": "correlation-public-001",
        }
    ]


def test_label_optimization_run_serializer_does_not_expand_internal_ledger() -> None:
    run = RunRecord(
        run_id="run_label_optimization_projection",
        tenant_id="tenant_label_optimization_projection",
        project_id="project_label_optimization_projection",
        run_type="label_optimization",
        status="running",
        trace_id="trace_label_optimization_projection",
        payload={
            "label_version_id": "label_v1",
            "stage": "candidate-generation",
            "metric_provenance": {
                "snapshot_id": "metric-snapshot-public-001",
                "provider": "internal-provider-canary",
            },
            "dispatch": _unsafe_dispatch(),
            "error": "Ｄａｇｓｔｅｒ Graph\u200bQL Bearer secret-bearer-canary",
        },
    )

    projection = _serialize_run(run)

    _assert_closed_public_projection(projection)
    assert projection["run_id"] == run.run_id
    assert projection["label_version_id"] == "label_v1"
    assert projection["stage"] == "candidate-generation"
    assert projection["metric_provenance"] == {"snapshot_id": "metric-snapshot-public-001"}


def test_prompt_candidate_read_projection_removes_run_result_locators(client, auth_headers) -> None:
    candidate_id = "candidate_public_projection_locator_guard"
    unsafe_payload = {
        "candidate_id": candidate_id,
        "source_run_id": "run_candidate_projection",
        "result_ref": {
            "provider": "internal-provider-canary",
            "adapter": "internal-adapter-canary",
            "object_uri": "s3://internal-bucket-canary/internal-object-key-canary",
            "storage_object_id": "storage-object-internal-canary",
            "details": {"external_run_id": "external-run-internal-canary"},
            "candidate_artifact_id": "artifact_public_001",
        },
        "review_status": "in-review",
        "review_submission_ids": ["review-public-001"],
        "received_reviews": 1,
        "summary": "Graph\u200bQL Ｄａｇｓｔｅｒ Bearer secret-bearer-canary",
    }
    with SessionLocal.begin() as session:
        session.add(
            PromptVersionCandidate(
                candidate_id=candidate_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="candidate",
                trace_id="trace_candidate_projection",
                payload=unsafe_payload,
            )
        )

    detail = client.get(f"/api/v1/prompt-version-candidates/{candidate_id}", headers=auth_headers)
    listing = client.get("/api/v1/prompt-version-candidates?limit=100", headers=auth_headers)

    assert detail.status_code == 200, detail.text
    assert listing.status_code == 200, listing.text
    listed = next(
        item for item in listing.json()["data"]["items"] if item["candidate_id"] == candidate_id
    )
    for projection in (detail.json()["data"], listed):
        _assert_closed_public_projection(projection)
        assert projection["result_ref"] == {"candidate_artifact_id": "artifact_public_001"}
        assert projection["review_status"] == "in-review"
        assert projection["review_submission_ids"] == ["review-public-001"]
        assert projection["received_reviews"] == 1

    with SessionLocal() as session:
        persisted = session.get(PromptVersionCandidate, candidate_id)
        assert persisted is not None
        assert json.dumps(persisted.payload, sort_keys=True) == json.dumps(
            unsafe_payload, sort_keys=True
        )
