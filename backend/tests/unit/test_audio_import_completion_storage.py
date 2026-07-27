from __future__ import annotations

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import RunRecord, StorageObject
from app.services.run_completion_storage_service import (
    register_hotword_completion_storage_objects,
)


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="system",
        roles=("system",),
        request_id="request-audio-import-storage",
        trace_id="trace-audio-import-completion",
    )


def _run() -> RunRecord:
    return RunRecord(
        run_id="task_run_audio_import_storage",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="task_run",
        status="submitted",
        trace_id="trace-audio-import-run",
        payload={
            "execution_contract": "auris-flow-audio-import-v1",
            "import_batch_id": "import_batch_storage",
            "root_trace_id": "root-audio-import-storage",
        },
    )


def _result_ref(run: RunRecord) -> dict:
    prefix = f"tenants/{run.tenant_id}/projects/{run.project_id}/runs/{run.run_id}/audio-import/"
    return {
        "import_batch_id": "import_batch_storage",
        "manifest_storage_object_id": "sto_audio_import_manifest",
        "manifest_sha256": "a" * 64,
        "next_cursor_candidate": "cursor-101",
        "items": [
            {
                "external_record_id": "external-call-100",
                "status": "succeeded",
                "storage_object_id": "sto_audio_import_wav_100",
                "content_sha256": "b" * 64,
                "object_version": "version-wav-100",
                "source": {
                    "started_at": "2026-07-21T10:00:00+00:00",
                    "duration_ms": 42_000,
                    "store_ref": "store-01",
                },
            },
            {
                "external_record_id": "external-call-101",
                "status": "failed",
                "error_code": "AUDIO_DOWNLOAD_FAILED",
            },
        ],
        "storage_objects": [
            {
                "storage_object_id": "sto_audio_import_manifest",
                "role": "manifest",
                "provider": "minio",
                "bucket": "auris-flow-local",
                "object_key": f"{prefix}manifest.json",
                "content_type": "application/json",
                "size_bytes": 1024,
                "content_sha256": "a" * 64,
                "etag": "manifest-etag",
                "version_id": "version-manifest-1",
            },
            {
                "storage_object_id": "sto_audio_import_wav_100",
                "role": "raw_audio",
                "provider": "minio",
                "bucket": "auris-flow-local",
                "object_key": f"{prefix}recordings/external-call-100.wav",
                "content_type": "audio/wav",
                "size_bytes": 84_044,
                "content_sha256": "b" * 64,
                "etag": "wav-etag",
                "version_id": "version-wav-100",
            },
        ],
    }


def test_audio_import_completion_registers_manifest_and_raw_audio_exact_versions() -> None:
    ctx = _ctx()
    run = _run()
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        registered = register_hotword_completion_storage_objects(
            session,
            ctx,
            run,
            _result_ref(run),
        )
        session.flush()

        assert {item["role"] for item in registered} == {"manifest", "raw_audio"}
        raw = session.get(StorageObject, "sto_audio_import_wav_100")
        assert raw is not None
        assert raw.status == "verified"
        assert raw.content_type == "audio/wav"
        assert raw.content_sha256 == "b" * 64
        assert raw.payload["object_version_id"] == "version-wav-100"
        assert raw.source_type == "task_run"
        assert raw.source_id == run.run_id


def test_audio_import_completion_rejects_item_descriptor_version_mismatch() -> None:
    ctx = _ctx()
    run = _run()
    result_ref = _result_ref(run)
    result_ref["storage_objects"][1]["version_id"] = "forged-version"
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        with pytest.raises(ApiError) as error:
            register_hotword_completion_storage_objects(session, ctx, run, result_ref)

        assert error.value.code == "RUN_COMPLETION_STORAGE_VERSION_MISMATCH"


def test_audio_import_completion_storage_rejects_executor_claimed_skipped_item() -> None:
    ctx = _ctx()
    run = _run()
    result_ref = _result_ref(run)
    result_ref["items"] = [
        {
            "external_record_id": "external-call-forged-skipped",
            "status": "skipped",
        }
    ]
    result_ref["storage_objects"] = result_ref["storage_objects"][:1]
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        with pytest.raises(ApiError) as error:
            register_hotword_completion_storage_objects(session, ctx, run, result_ref)

        assert error.value.code == "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID"


def test_audio_import_completion_rejects_unversioned_manifest_before_registration() -> None:
    ctx = _ctx()
    run = _run()
    result_ref = _result_ref(run)
    result_ref["storage_objects"][0].pop("version_id")
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        with pytest.raises(ApiError) as error:
            register_hotword_completion_storage_objects(session, ctx, run, result_ref)

        assert error.value.code == "RUN_COMPLETION_STORAGE_VERSION_REQUIRED"
        assert session.get(StorageObject, "sto_audio_import_manifest") is None
        assert session.get(StorageObject, "sto_audio_import_wav_100") is None


def test_audio_import_completion_rejects_raw_audio_without_playback_etag() -> None:
    ctx = _ctx()
    run = _run()
    result_ref = _result_ref(run)
    result_ref["storage_objects"][1].pop("etag")
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        with pytest.raises(ApiError) as error:
            register_hotword_completion_storage_objects(session, ctx, run, result_ref)

        assert error.value.code == "RUN_COMPLETION_STORAGE_ETAG_REQUIRED"


def test_audio_import_completion_accepts_empty_window_with_manifest_only() -> None:
    ctx = _ctx()
    run = _run()
    result_ref = _result_ref(run)
    result_ref["items"] = []
    result_ref["storage_objects"] = result_ref["storage_objects"][:1]
    with SessionLocal() as session:
        session.add(run)
        session.flush()

        registered = register_hotword_completion_storage_objects(
            session,
            ctx,
            run,
            result_ref,
        )

        assert [item["role"] for item in registered] == ["manifest"]
