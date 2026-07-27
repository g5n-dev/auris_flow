from __future__ import annotations

import pytest
from dagster import DagsterInvalidConfigError, validate_run_config

from auris_flow_dagster.contracts import AurisContractError
from auris_flow_dagster.definitions import (
    auris_flow_audio_import_v1,
    auris_flow_audio_intelligence_v1,
    auris_flow_generic_job,
    defs,
    map_audio_import_run_config,
    map_audio_intelligence_run_config,
    map_auris_run_config,
)


def _audio_envelope(valid_context: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": "auris-flow-audio-intelligence-v1",
        "tenant_id": valid_context["tenant_id"],
        "project_id": valid_context["project_id"],
        "trace_id": valid_context["trace_id"],
        "run_id": valid_context["run_id"],
        "dispatch_idempotency_key": valid_context["dispatch_idempotency_key"],
        "outbox_fencing_token": valid_context["outbox_fencing_token"],
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "audio_session_id": "audio_session_001",
        "recording_id": "recording_001",
        "input_object": {
            "storage_object_id": "sto_audio_001",
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_key": "tenants/aurora_auto/projects/sales_qa/audio/recording.wav",
            "version_id": "immutable-version-3",
            "content_sha256": "a" * 64,
            "content_length": 64,
            "content_type": "audio/wav",
        },
        "inference": {"provider": "audio_intelligence_default", "model": "audio-v2.3.1"},
        "capabilities": ["vad", "asr"],
    }


def _audio_context(valid_context: dict[str, object]) -> dict[str, object]:
    return {**valid_context, "event_type": "audio_intelligence.requested"}


def _audio_import_envelope(valid_context: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": "auris-flow-audio-import-v1",
        "tenant_id": valid_context["tenant_id"],
        "project_id": valid_context["project_id"],
        "trace_id": valid_context["trace_id"],
        "root_trace_id": valid_context["trace_id"],
        "run_id": valid_context["run_id"],
        "dispatch_idempotency_key": valid_context["dispatch_idempotency_key"],
        "outbox_fencing_token": valid_context["outbox_fencing_token"],
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "import_batch_id": "import_batch_001",
        "connector": {
            "connector_id": "connector_001",
            "connector_version": "connector_version_001",
            "platform_connection_id": "platform_connection_001",
            "platform_scope": {
                "tenant_ref": "external_tenant_001",
                "store_refs": ["store-001"],
            },
            "source_type": "platform_audio_url_api",
            "base_url": "https://platform.example.test",
            "request_path": "/api/v1/recordings",
            "credential_ref": "platform_primary",
            "pagination": {
                "mode": "cursor",
                "page_size": 100,
                "cursor_param": "cursor",
                "next_cursor_path": "data.next_cursor",
            },
            "field_mapping": {
                "external_record_id": "recordingId",
                "audio_url": "audioUrl",
                "started_at": "startedAt",
                "store_ref": "storeId",
            },
            "cursor_policy": {
                "field": "updatedAt",
                "initial_window_start": "2026-07-20T00:00:00+00:00",
            },
        },
        "target": {
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_prefix": (
                "tenants/aurora_auto/projects/sales_qa/runs/run_task_001/audio-import/"
            ),
            "target_asset_key": "auris/sources/platform_audio",
            "dedupe_policy": "external_id_checksum",
        },
    }


def test_real_client_top_level_run_config_maps_to_domain_op(
    valid_context: dict[str, object],
) -> None:
    incoming = {
        "auris_context": valid_context,
        "execution": {"mode": "control-plane-acknowledgement"},
    }
    mapped = map_auris_run_config(incoming)
    op_config = mapped["ops"]["execute_auris_flow_domain_work"]["config"]
    assert op_config["auris_context"] == valid_context
    assert op_config["execution"] == incoming["execution"]
    assert validate_run_config(auris_flow_generic_job, run_config=incoming)["ops"]


def test_dagster_config_mapping_fails_closed_before_domain_work(
    valid_context: dict[str, object],
) -> None:
    invalid = {"auris_context": {**valid_context, "tenant_id": None}}
    with pytest.raises(AurisContractError, match="tenant_id"):
        map_auris_run_config(invalid)
    with pytest.raises(DagsterInvalidConfigError):
        validate_run_config(auris_flow_generic_job, run_config={})


def test_definitions_expose_only_domain_named_generic_job() -> None:
    repository = defs.get_repository_def()
    assert repository.name == "__repository__"
    assert repository.has_job("auris_flow_generic_job")
    assert repository.has_job("auris_flow_audio_intelligence_v1")
    assert repository.has_job("auris_flow_audio_import_v1")
    assert "dagster" not in auris_flow_generic_job.description.lower()
    assert "dagster" not in auris_flow_audio_intelligence_v1.description.lower()
    assert "dagster" not in auris_flow_audio_import_v1.description.lower()


def test_audio_job_maps_only_strict_execution_envelope(
    valid_context: dict[str, object],
) -> None:
    audio_context = _audio_context(valid_context)
    incoming = {
        "auris_context": audio_context,
        "execution_envelope": _audio_envelope(audio_context),
    }

    mapped = map_audio_intelligence_run_config(incoming)
    op_config = mapped["ops"]["execute_auris_flow_audio_intelligence_v1"]["config"]

    assert op_config == incoming
    assert validate_run_config(auris_flow_audio_intelligence_v1, run_config=incoming)["ops"]


def test_audio_import_job_maps_only_server_bound_execution_envelope(
    valid_context: dict[str, object],
) -> None:
    context = {**valid_context, "event_type": "task_run.requested"}
    incoming = {
        "auris_context": context,
        "execution_envelope": _audio_import_envelope(context),
    }

    mapped = map_audio_import_run_config(incoming)
    op_config = mapped["ops"]["execute_auris_flow_audio_import_v1"]["config"]

    assert op_config == incoming
    assert validate_run_config(auris_flow_audio_import_v1, run_config=incoming)["ops"]


def test_audio_import_job_rejects_frontend_job_name_and_run_config_override(
    valid_context: dict[str, object],
) -> None:
    context = {**valid_context, "event_type": "task_run.requested"}
    incoming = {
        "auris_context": context,
        "execution_envelope": {
            **_audio_import_envelope(context),
            "job_name": "frontend_supplied_job",
        },
    }

    with pytest.raises(AurisContractError, match="unexpected"):
        map_audio_import_run_config(incoming)
