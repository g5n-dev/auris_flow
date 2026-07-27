from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.services.adapters import (
    AUDIO_IMPORT_EXECUTION_CONTRACT,
    AUDIO_IMPORT_JOB_NAME,
    AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    AUDIO_INTELLIGENCE_JOB_NAME,
    AdapterRegistry,
    LocalDagsterClient,
    RealDagsterClient,
    dispatch_event,
)


def _audio_payload() -> dict[str, Any]:
    return {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_audio_domain_001",
        "run_id": "audio_intelligence_001",
        "event_type": "audio_intelligence.requested",
        "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        "execution_deadline_at": "2099-07-21T12:00:00+00:00",
        "dispatch_idempotency_key": "outbox:audio-intelligence:001",
        "outbox_fencing_token": "821:3",
        "recording_id": "recording_001",
        "audio_session_id": "audio_session_001",
        "provider": "audio_intelligence_default",
        "model_version": "audio-v2.3.1",
        "capabilities": ["vad", "asr", "diarization"],
        "input_object": {
            "storage_object_id": "sto_audio_001",
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_key": "tenants/aurora_auto/projects/sales_qa/audio/recording.wav",
            "version_id": "3LgKx9-immutable-version",
            "content_sha256": "a" * 64,
            "content_length": 32_044,
            "content_type": "audio/wav",
        },
        # None of these caller-controlled values may alter engine selection/config.
        "job_name": "caller_selected_job",
        "dagster_run_draft": {"job_name": "draft_selected_job"},
        "run_config": {
            "execution_envelope": {"tenant_id": "forged_tenant"},
            "resources": {"unsafe": {"config": {"token": "must-not-forward"}}},
        },
    }


def _audio_import_payload() -> dict[str, Any]:
    run_id = "task_run_audio_import_001"
    return {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_audio_import_001",
        "root_trace_id": "root_audio_import_001",
        "run_id": run_id,
        "event_type": "task_run.requested",
        "execution_contract": AUDIO_IMPORT_EXECUTION_CONTRACT,
        "execution_deadline_at": "2099-07-21T12:00:00+00:00",
        "dispatch_idempotency_key": "outbox:audio-import:001",
        "outbox_fencing_token": "921:4",
        "import_batch_id": "import_batch_001",
        "connector_snapshot": {
            "connector_id": "connector_platform_audio_001",
            "connector_version": "1",
            "platform_connection_id": "platform_connection_001",
            "platform_scope": {"tenant_ref": "tenant-external-001", "store_refs": ["store-01"]},
            "source_type": "platform_audio_url_api",
            "base_url": "https://recordings.example.test",
            "request_path": "/v1/recordings",
            "credential_ref": "secret://platform/recordings-reader",
            "pagination": {
                "mode": "cursor",
                "page_size": 100,
                "cursor_param": "cursor",
                "next_cursor_path": "meta.next_cursor",
            },
            "field_mapping": {
                "external_record_id": "id",
                "audio_url": "recording_url",
                "started_at": "started_at",
                "duration_ms": "duration_ms",
                "store_ref": "store_id",
            },
            "cursor_policy": {
                "field": "updated_at",
                "initial_window_start": "2026-07-01T00:00:00+00:00",
            },
        },
        "target": {
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_prefix": (f"tenants/aurora_auto/projects/sales_qa/runs/{run_id}/audio-import/"),
            "target_asset_key": "auris/sources/platform_audio",
            "dedupe_policy": "external_id_checksum",
        },
        # Caller-selected engine configuration must never escape the BFF.
        "job_name": "caller_selected_import_job",
        "run_config": {
            "execution_envelope": {"tenant_id": "forged"},
            "resources": {"unsafe": {"config": {"token": "must-not-forward"}}},
        },
    }


class RecordingDagsterClient(RealDagsterClient):
    def __init__(self) -> None:
        super().__init__(
            graphql_url="http://dagster.example.test/graphql",
            repository_location_name="auris_defs",
            repository_name="auris_repo",
            default_job_name="auris_flow_generic_job",
        )
        self.requests: list[dict[str, Any]] = []

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(body)
        return {
            "data": {
                "launchPipelineExecution": {
                    "__typename": "LaunchRunSuccess",
                    "run": {"runId": "real_audio_run_001", "status": "STARTED"},
                }
            }
        }


def test_audio_domain_event_selects_allowlisted_job_and_complete_immutable_envelope() -> None:
    client = RecordingDagsterClient()
    payload = _audio_payload()

    dispatch = dispatch_event(
        "audio_intelligence.requested",
        "audio_intelligence",
        payload,
        registry=AdapterRegistry(dagster=client),
    )

    assert dispatch.status == "success"
    assert dispatch.details["job_name"] == AUDIO_INTELLIGENCE_JOB_NAME
    assert "input_object" not in dispatch.details
    request = client.requests[0]
    params = request["variables"]["executionParams"]
    assert params["selector"]["pipelineName"] == AUDIO_INTELLIGENCE_JOB_NAME
    assert set(params["runConfigData"]) == {"auris_context", "execution_envelope"}
    envelope = params["runConfigData"]["execution_envelope"]
    assert envelope == {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_audio_domain_001",
        "run_id": "audio_intelligence_001",
        "dispatch_idempotency_key": "outbox:audio-intelligence:001",
        "outbox_fencing_token": "821:3",
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "audio_session_id": "audio_session_001",
        "recording_id": "recording_001",
        "input_object": payload["input_object"],
        "inference": {
            "provider": "audio_intelligence_default",
            "model": "audio-v2.3.1",
        },
        "capabilities": ["vad", "asr", "diarization"],
    }
    assert len(dispatch.details["execution_envelope_sha256"]) == 64
    tags = {item["key"]: item["value"] for item in params["executionMetadata"]["tags"]}
    assert tags["auris/execution_contract"] == AUDIO_INTELLIGENCE_EXECUTION_CONTRACT
    assert tags["auris/execution_envelope_sha256"] == dispatch.details["execution_envelope_sha256"]
    serialized_tags = repr(tags)
    assert payload["input_object"]["object_key"] not in serialized_tags
    assert payload["input_object"]["content_sha256"] not in serialized_tags
    assert "must-not-forward" not in repr(request)


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        (lambda payload: payload.pop("event_type"), "event_type"),
        (lambda payload: payload.__setitem__("event_type", "unknown.requested"), "event_type"),
        (lambda payload: payload.pop("execution_contract"), "execution_contract"),
        (lambda payload: payload["input_object"].pop("version_id"), "version_id"),
        (lambda payload: payload["input_object"].pop("content_sha256"), "content_sha256"),
        (lambda payload: payload.pop("execution_deadline_at"), "deadline"),
        (lambda payload: payload.pop("outbox_fencing_token"), "outbox_fencing_token"),
    ],
)
def test_audio_domain_mapping_and_envelope_fail_closed_before_graphql(
    mutation: Any,
    expected_field: str,
) -> None:
    client = RecordingDagsterClient()
    payload = deepcopy(_audio_payload())
    mutation(payload)

    dispatch = client.submit_run_request(payload)

    assert dispatch.status == "failed"
    assert dispatch.error_code == "DAGSTER_EXECUTION_CONTRACT_INVALID"
    assert dispatch.retryable is False
    assert expected_field in str(dispatch.details.get("invalid_field") or "")
    assert client.requests == []
    assert "must-not-forward" not in repr(dispatch)


def test_known_control_plane_event_retains_generic_job_for_ci_and_control_tests() -> None:
    client = RecordingDagsterClient()
    payload = {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_control_001",
        "run_id": "task_run_001",
        "event_type": "task_run.requested",
        "dispatch_idempotency_key": "outbox:task:001",
        "outbox_fencing_token": "900:1",
        "job_name": "caller_must_not_select_this",
    }

    dispatch = client.submit_run_request(payload)

    assert dispatch.status == "success"
    request = client.requests[0]
    params = request["variables"]["executionParams"]
    assert params["selector"]["pipelineName"] == "auris_flow_generic_job"
    assert params["runConfigData"]["execution"] == {"mode": "control-plane-acknowledgement"}


def test_audio_import_task_run_selects_allowlisted_job_and_server_envelope() -> None:
    client = RecordingDagsterClient()
    payload = _audio_import_payload()

    dispatch = client.submit_run_request(payload)

    assert dispatch.status == "success"
    assert dispatch.details["job_name"] == AUDIO_IMPORT_JOB_NAME
    request = client.requests[0]
    params = request["variables"]["executionParams"]
    assert params["selector"]["pipelineName"] == AUDIO_IMPORT_JOB_NAME
    assert set(params["runConfigData"]) == {"auris_context", "execution_envelope"}
    envelope = params["runConfigData"]["execution_envelope"]
    assert envelope["execution_contract"] == AUDIO_IMPORT_EXECUTION_CONTRACT
    assert envelope["root_trace_id"] == "root_audio_import_001"
    assert envelope["import_batch_id"] == "import_batch_001"
    assert envelope["connector"] == payload["connector_snapshot"]
    assert envelope["target"] == payload["target"]
    assert "must-not-forward" not in repr(request)
    assert "caller_selected_import_job" not in repr(request)


def test_local_audio_import_dispatch_reports_the_same_allowlisted_job() -> None:
    payload = _audio_import_payload()

    dispatch = LocalDagsterClient().submit_run_request(payload)

    assert dispatch.status == "success"
    assert dispatch.details["job_name"] == AUDIO_IMPORT_JOB_NAME


@pytest.mark.parametrize(
    ("mutation", "expected_field"),
    [
        (lambda payload: payload.pop("import_batch_id"), "import_batch_id"),
        (lambda payload: payload.pop("root_trace_id"), "root_trace_id"),
        (
            lambda payload: payload["connector_snapshot"].__setitem__(
                "base_url", "http://127.0.0.1/internal"
            ),
            "connector.base_url",
        ),
        (
            lambda payload: payload["target"].__setitem__(
                "object_prefix", "tenants/other/projects/sales_qa/runs/forged/audio-import/"
            ),
            "target.object_prefix",
        ),
        (
            lambda payload: payload["connector_snapshot"]["field_mapping"].pop("audio_url"),
            "connector.field_mapping.audio_url",
        ),
    ],
)
def test_audio_import_execution_contract_fails_closed_before_graphql(
    mutation: Any,
    expected_field: str,
) -> None:
    client = RecordingDagsterClient()
    payload = deepcopy(_audio_import_payload())
    mutation(payload)

    dispatch = client.submit_run_request(payload)

    assert dispatch.status == "failed"
    assert dispatch.error_code == "DAGSTER_EXECUTION_CONTRACT_INVALID"
    assert dispatch.retryable is False
    assert expected_field in str(dispatch.details.get("invalid_field") or "")
    assert client.requests == []
