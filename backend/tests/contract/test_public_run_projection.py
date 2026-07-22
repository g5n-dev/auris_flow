from __future__ import annotations

import json
import re
from collections import UserDict, deque
from collections.abc import Mapping, Sequence
from typing import Any

import yaml  # type: ignore[import-untyped]
from sqlalchemy import select
from yaml.nodes import MappingNode  # type: ignore[import-untyped]

from app.core.database import SessionLocal
from app.core.json_keys import json_key_fingerprint
from app.models import AuditLog, IdempotencyRecord, OutboxEvent, RunRecord
from app.services.run_service import (
    PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS,
    PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS,
    run_payload,
)

FORBIDDEN_FIELD_FINGERPRINTS = {
    json_key_fingerprint(field)
    for field in (
        "adapter",
        "adapter_dispatch",
        "adapter_mode",
        "artifact_uri",
        "access_token",
        "api_key",
        "dagster_run_id",
        "details",
        "dispatch",
        "dispatch_idempotency_key",
        "dispatch_request_sha256",
        "dispatch_state",
        "engine_status",
        "engine_status_observed_at",
        "execution_contract",
        "execution_deadline_at",
        "execution_envelope",
        "external_id",
        "external_run_id",
        "endpoint",
        "graphql_url",
        "job_name",
        "monitor_generation",
        "next_status_sync_at",
        "observed_engine_status",
        "processed_event_id",
        "failed_event_id",
        "dead_letter_event_id",
        "provider_evidence",
        "provider_run_id",
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
        "execution_envelope_sha256",
        "result_manifest_object_key_sha256",
        "result_manifest_version_id_sha256",
        "storage_provider",
        "object_version_id",
        "etag",
        "input_object",
        "repository_location_name",
        "repository_name",
        "signature",
        "signature_body_hash",
        "signature_key_id",
        "signature_mode",
        "signature_nonce",
        "signature_request_hash",
        "bucket",
        "object_key",
        "object_uri",
        "partial_artifact_uri",
        "storage_object_id",
        "uri",
        "url",
    )
}
FORBIDDEN_FIELD_FRAGMENTS = (
    "adapter",
    "dagster",
    "dispatch",
    "endpoint",
    "engine",
    "graphql",
    "internal",
    "protocol",
    "remote",
    "signature",
    "bucket",
    "storage",
    "secret",
    "token",
)
FORBIDDEN_VALUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"dagster",
        r"graphql",
        r"\badapter\b",
        r"\b[a-z0-9.-]+_(?:job|pipeline)\b",
        r"engine-run-proof-canary",
        r"signature-proof-canary",
    )
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise AssertionError(f"duplicate OpenAPI key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _assert_engine_neutral(value: Any, *, path: str = "data") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            fingerprint = json_key_fingerprint(str(key))
            assert fingerprint not in FORBIDDEN_FIELD_FINGERPRINTS, f"{path}.{key}"
            if fingerprint == "storageobjects":
                assert isinstance(child, list), f"{path}.{key}"
                assert all(
                    isinstance(item, Mapping)
                    and set(item) == {"role", "content_sha256"}
                    and isinstance(item["role"], str)
                    and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", item["role"])
                    and isinstance(item["content_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", item["content_sha256"])
                    for item in child
                ), f"{path}.{key}"
                continue
            assert not any(fragment in fingerprint for fragment in FORBIDDEN_FIELD_FRAGMENTS), (
                f"{path}.{key}"
            )
            _assert_engine_neutral(child, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_engine_neutral(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            assert pattern.search(value) is None, f"{path}: {value!r}"


def _unsafe_internal_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **payload,
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "dispatch_state": "submitted",
        "processed_event_id": 13800138000,
        "failed_event_id": 13800138001,
        "dead_letter_event_id": 13800138002,
        "provider_artifact_ref": "provider-artifact-internal-canary",
        "result_storage_object_ids": ["storage-object-list-internal-canary"],
        "result_storage_object_sha256": {
            "storage-object-list-internal-canary": "c" * 64,
        },
        "accessToken": "legacy-access-token-canary",
        "api-key": "legacy-api-key-canary",
        "remote_run_id": "remote-run-proof-canary",
        "protocol_receipt": {
            "mode": "real",
            "response_typename": "LaunchRunSuccess",
        },
        "secret\u200bmaterial": "zero-width-secret-canary",
        "dagster\u200bmetadata": "zero-width-engine-canary",
        "engine\u200bbinding": "zero-width-binding-canary",
        "dispatch": {
            "adapter": "dagster",
            "operation": "run_request",
            "status": "success",
            "details": {
                "mode": "real",
                "externalRunId": "engine-run-proof-canary",
                "dagster_run_id": "engine-run-proof-canary",
                "jobName": "auris_flow_generic_job",
                "graphql_url": "http://dagster-webserver:3000/graphql",
                "response_typename": "LaunchRunSuccess",
            },
        },
        "completion_receipt": {
            "completion_receipt_id": "receipt-domain-summary-001",
            "receipt_hash": "receipt-internal-proof-canary",
            "adapter": "dagster",
            "external_id": "engine-run-proof-canary",
            "source": "dagster",
            "status": "success",
            "result_ref": {
                "type": "storage_object",
                "id": "result_13800138000",
                "provider_evidence": {
                    "provider": "promptfoo",
                    "provider_run_id": "provider-run-internal-canary",
                    "config_artifact": {
                        "object_type": "storage_object",
                        "object_id": "storage-object-config-internal-canary",
                    },
                    "result_artifact": {
                        "object_type": "storage_object",
                        "object_id": "storage-object-result-internal-canary",
                    },
                },
                "provider_request_sha256": "d" * 64,
                "provider_response_sha256": "e" * 64,
                "provider_result_sha256": "f" * 64,
                "execution_envelope_sha256": "1" * 64,
                "result_manifest_object_key_sha256": "2" * 64,
                "result_manifest_version_id_sha256": "3" * 64,
                "storage_provider": "minio",
                "version_id": "storage-version-internal-canary",
                "object_version_id": "storage-version-internal-canary",
                "etag": "storage-etag-internal-canary",
                "checks": {
                    "url": "http://metadata.internal/admin",
                    "uri": "s3://internal-bucket-canary/nested-result.json",
                    "artifact_uri": "s3://internal-bucket-canary/artifact.json",
                    "artifact_url": "http://metadata.internal/artifact",
                    "manifest_uri": "s3://internal-bucket-canary/manifest.json",
                    "artifactUrl": "s3://internal-bucket-canary/camel.json",
                    "safe_check": "passed",
                },
                "artifact_id": "s3://internal-bucket-canary/private-artifact.json",
                "action_id": "storage.private.lan",
                "draft_ref": "tenant/secret.json",
                "asset_key": "auris/task/login_risk_review_task_v3",
                "content_type": "application/json",
                "cloud_locator_evidence": "unknown-locator-internal-canary",
                "storage_objects": [
                    {
                        "storage_object_id": "storage-object-descriptor-internal-canary",
                        "role": "manifest",
                        "provider": "minio",
                        "bucket": "internal-bucket-canary",
                        "object_key": "tenants/internal/manifest.json",
                        "version_id": "storage-version-internal-canary",
                        "etag": "storage-etag-internal-canary",
                        "content_sha256": "5" * 64,
                    },
                    {"role": "Dagster artifact", "content_sha256": "6" * 64},
                    {"role": "manifest", "content_sha256": "not-a-sha256"},
                ],
            },
            "metrics": {
                "materialized_partitions": 1,
                "unknown_provider_locator": "metric-internal-canary",
            },
            "note": "领域结果已确认",
            "error_code": None,
            "retryable": False,
            "received_at": "2026-07-21T12:00:00Z",
            "trace_id": "callback-trace-internal-canary",
            "run_trace_id": "run-trace-internal-canary",
            "root_trace_id": "root-trace-internal-canary",
            "request_sha256": "request-internal-proof-canary",
            "body_sha256": "body-internal-proof-canary",
            "internal_metadata": {"worker_host": "worker-internal-canary"},
            "auth": {
                "signatureKeyId": "signature-proof-canary",
                "signature_nonce": "signature-proof-canary",
                "signature_body_hash": "a" * 64,
                "secret": "signature-proof-canary",
            },
        },
        "nested_engine_metadata": {
            "repositoryLocationName": "auris_internal_repository",
            "job_name": "audio_intelligence_pipeline",
        },
        "failure_details": {
            "endpoint": "http://dagster-webserver:3000/graphql",
            "bucket": "internal-bucket-canary",
            "object_key": "tenants/internal/export.jsonl",
            "object_uri": "s3://internal-bucket-canary/export.jsonl",
            "storage_object_id": "storage-object-internal-canary",
        },
        "execution_contract": "auris.execution.v1",
        "execution_deadline_at": "2026-07-21T12:00:00Z",
        "execution_envelope": {
            "input_object": {
                "bucket": "auris-internal",
                "object_key": "tenant/project/source.wav",
                "version_id": "storage-version-canary",
                "content_sha256": "b" * 64,
            }
        },
        "status_history": [
            {"from": "pending", "to": "running", "reason": "outbox_dispatch_started"},
            {
                "from": "running",
                "to": "submitted",
                "reason": "dagster_status_reconciled",
            },
        ],
        "next_actions": [
            {
                "key": "view_result",
                "label": "Open Dagster GraphQL adapter job auris_flow_generic_job",
                "href": "/runs/obj_13800138000",
            }
        ],
        "result_ref": {
            "type": "storage_object",
            "id": "obj_13800138000",
            "provider_evidence": {
                "provider": "promptfoo",
                "provider_run_id": "provider-run-top-level-internal-canary",
            },
            "execution_envelope_sha256": "4" * 64,
            "version_id": "storage-version-top-level-internal-canary",
            "etag": "storage-etag-top-level-internal-canary",
        },
        "content_sha256": "a13800138000b" + "c" * 51,
        "display_note": "Dag\u200bster Graph\u200bQL",
        "error": "Dagster GraphQL adapter failed in audio_intelligence_pipeline",
        "error_code": "DAGSTER_GRAPHQL_ADAPTER_FAILURE",
    }


def test_public_run_endpoints_recursively_hide_execution_engine_evidence(
    client, auth_headers
) -> None:
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "partition_key": "engine-neutral/contract",
        },
        headers={**auth_headers, "Idempotency-Key": "engine-neutral-run-contract"},
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["data"]["run_id"]

    with SessionLocal() as session:
        record = session.get(RunRecord, run_id)
        assert record is not None
        unsafe = _unsafe_internal_evidence(record.payload)
        record.payload = unsafe
        record.status = "submitted"
        record.engine_status = "STARTED"

        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        event.payload = unsafe
        audit = session.query(AuditLog).filter_by(object_id=run_id).one()
        audit.after_json = unsafe
        session.commit()

    detail = client.get(f"/api/v1/task-runs/{run_id}", headers=auth_headers)
    generic = client.get(f"/api/v1/runs/{run_id}", headers=auth_headers)
    listing = client.get("/api/v1/task-runs?limit=100", headers=auth_headers)

    assert detail.status_code == 200, detail.text
    assert generic.status_code == 200, generic.text
    assert listing.status_code == 200, listing.text
    listed = next(item for item in listing.json()["data"]["items"] if item["run_id"] == run_id)
    projections = (detail.json()["data"], generic.json()["data"], listed)
    for projection in projections:
        _assert_engine_neutral(projection)
        assert projection["run_id"] == run_id
        assert projection["trace_id"]
        assert projection["status"] == "submitted"
        assert projection["tenant_id"] == "aurora_auto"
        assert projection["project_id"] == "sales_qa"
        assert projection["status_history"] == [
            {"from": "pending", "to": "running", "reason": "execution_started"},
            {
                "from": "running",
                "to": "submitted",
                "reason": "status_reconciled",
            },
        ]
        assert projection["next_actions"] == [
            {
                "key": "view_result",
                "label": "Open execution engine protocol integration workflow",
            }
        ]
        assert projection["result_ref"] == {
            "type": "storage_object",
            "id": "obj_[REDACTED_PHONE]",
        }
        assert projection["content_sha256"] == "a13800138000b" + "c" * 51
        assert "provider_artifact_ref" not in projection
        assert "result_storage_object_ids" not in projection
        assert "result_storage_object_sha256" not in projection
        assert "failed_event_id" not in projection
        assert "dead_letter_event_id" not in projection
        assert projection["display_note"] == "execution engine protocol"
        assert projection["completion_receipt"] == {
            "completion_receipt_id": "receipt-domain-summary-001",
            "status": "success",
            "result_ref": {
                "type": "storage_object",
                "id": "result_[REDACTED_PHONE]",
                "asset_key": "auris/task/login_risk_review_task_v3",
                "content_type": "application/json",
                "storage_objects": [{"role": "manifest", "content_sha256": "5" * 64}],
            },
            "metrics": {"materialized_partitions": 1},
            "error_code": None,
            "retryable": False,
            "received_at": "2026-07-21T12:00:00Z",
        }
        assert projection["completion_receipt"]["metrics"] == {"materialized_partitions": 1}
        encoded_projection = json.dumps(projection, sort_keys=True)
        assert "provider-run-internal-canary" not in encoded_projection
        assert "storage-object-config-internal-canary" not in encoded_projection
        assert "storage-version-internal-canary" not in encoded_projection
        assert "storage-etag-internal-canary" not in encoded_projection
        assert "metadata.internal" not in encoded_projection
        assert "internal-bucket-canary/nested-result" not in encoded_projection
        assert "internal-bucket-canary/manifest" not in encoded_projection
        assert "internal-bucket-canary/camel" not in encoded_projection
        assert "storage.private.lan" not in encoded_projection
        assert "tenant/secret.json" not in encoded_projection
        assert not any("\u200b" in str(key) for key in projection)

    with SessionLocal() as session:
        record = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        audit = session.query(AuditLog).filter_by(object_id=run_id).one()
        assert record is not None
        for internal in (record.payload, event.payload, audit.after_json):
            encoded = json.dumps(internal, sort_keys=True)
            assert "engine-run-proof-canary" in encoded
            assert "signature-proof-canary" in encoded
            assert "auris_flow_generic_job" in encoded


def test_run_payload_is_engine_neutral_without_mutating_persisted_evidence() -> None:
    record = RunRecord(
        run_id="task_run_projection_unit",
        tenant_id="tenant_projection_unit",
        project_id="project_projection_unit",
        run_type="task_run",
        status="failed",
        status_version=4,
        trace_id="trace_projection_unit",
        engine_status="FAILURE",
        terminal_reason="DAGSTER_GRAPHQL_ADAPTER_FAILURE",
        payload=_unsafe_internal_evidence(
            {
                "affected_objects": [{"type": "task_run", "id": "task_13800138000"}],
                "next_actions": [],
            }
        ),
    )
    original = json.loads(json.dumps(record.payload))

    projection = run_payload(record)

    _assert_engine_neutral(projection)
    assert projection["tenant_id"] == "tenant_projection_unit"
    assert projection["project_id"] == "project_projection_unit"
    assert projection["terminal_reason"] == "EXECUTION_PROTOCOL_INTEGRATION_FAILURE"
    assert projection["affected_objects"] == [{"type": "task_run", "id": "task_[REDACTED_PHONE]"}]
    assert record.payload == original
    assert record.engine_status == "FAILURE"


def test_run_payload_omits_non_json_containers_and_noncanonical_unicode_keys() -> None:
    record = RunRecord(
        run_id="task_run_projection_container_guard",
        tenant_id="tenant_projection_container_guard",
        project_id="project_projection_container_guard",
        run_type="task_run",
        status="pending",
        status_version=1,
        trace_id="trace_projection_container_guard",
        payload={
            "stable_domain_ref": "domain_13800138000",
            "unknown_mapping": UserDict(
                {"adapter": "dagster", "secret_ref": "container-secret-canary"}
            ),
            "unknown_sequence": deque(
                [{"dispatch": {"external_run_id": "container-engine-canary"}}]
            ),
            "unknown_set": {"dagster", "container-engine-canary"},
            "ｓｔａｔｕｓ": "failed",
            "safe\u200bfield": "must-not-cross-boundary",
        },
    )

    projection = run_payload(record)

    assert projection["stable_domain_ref"] == "domain_[REDACTED_PHONE]"
    assert "unknown_mapping" not in projection
    assert "unknown_sequence" not in projection
    assert "unknown_set" not in projection
    assert "ｓｔａｔｕｓ" not in projection
    assert "safe\u200bfield" not in projection
    assert projection["status"] == "pending"


def test_legacy_idempotency_replay_is_projected_before_returning(client, auth_headers) -> None:
    request_body = {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        "partition_key": "engine-neutral/legacy-replay",
    }
    headers = {**auth_headers, "Idempotency-Key": "legacy-engine-run-replay"}
    created = client.post("/api/v1/task-runs", json=request_body, headers=headers)
    assert created.status_code == 202, created.text

    with SessionLocal() as session:
        stored = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == "aurora_auto",
                IdempotencyRecord.project_id == "sales_qa",
                IdempotencyRecord.operation == "create:task_run",
                IdempotencyRecord.idempotency_key == "legacy-engine-run-replay",
            )
        )
        assert stored is not None
        legacy_data = _unsafe_internal_evidence(stored.response_json["data"])
        legacy_data.pop("tenant_id", None)
        legacy_data.pop("project_id", None)
        stored.response_json = {
            **stored.response_json,
            "data": legacy_data,
        }
        session.commit()

    replayed = client.post("/api/v1/task-runs", json=request_body, headers=headers)
    assert replayed.status_code == 202, replayed.text
    _assert_engine_neutral(replayed.json()["data"])
    assert replayed.json()["data"]["run_id"] == created.json()["data"]["run_id"]
    assert replayed.json()["data"]["tenant_id"] == "aurora_auto"
    assert replayed.json()["data"]["project_id"] == "sales_qa"


def test_openapi_run_projection_is_explicitly_engine_neutral() -> None:
    document = yaml.load(
        (
            __import__("pathlib").Path(__file__).resolve().parents[3]
            / "doc"
            / "backend-spec"
            / "openapi-v0.1.yaml"
        ).read_text(encoding="utf-8"),
        Loader=_UniqueKeySafeLoader,
    )
    schemas = document["components"]["schemas"]
    for schema_name in ("RunAction", "RunSummary", "RunDetail"):
        schema = schemas[schema_name]
        recursively_omitted = {
            json_key_fingerprint(field) for field in schema["x-auris-recursively-omitted-fields"]
        }
        assert recursively_omitted == PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS
        assert set(schema["x-auris-recursively-omitted-key-parts"]) == set(
            PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS
        )
        properties = schema["properties"]
        for field in (
            "adapter",
            "dispatch",
            "engine_status",
            "engine_status_observed_at",
            "external_run_id",
            "job_name",
            "monitor_generation",
            "next_status_sync_at",
        ):
            assert field not in properties

    for path in ("/task-runs", "/task-runs/{id}", "/runs/{id}"):
        operation = document["paths"][path]["get" if "{id}" in path else "post"]
        assert "执行引擎中立" in (operation.get("description") or "")
