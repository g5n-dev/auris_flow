from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from app.core.database import SessionLocal
from app.models import StorageObject

ROOT = Path(__file__).resolve().parents[3]


def _load_harness() -> ModuleType:
    path = ROOT / "scripts" / "verify_e2e_outbox_dispatch.py"
    spec = importlib.util.spec_from_file_location("auris_e2e_outbox_dispatch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_wav() -> bytes:
    sample_rate = 8000
    pcm = b"".join(
        struct.pack("<h", 1200 if (index // 80) % 2 == 0 else -1200) for index in range(sample_rate)
    )
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + len(pcm)),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
            b"data",
            struct.pack("<I", len(pcm)),
            pcm,
        ]
    )


def test_range_harness_reuses_registered_exact_object_version(monkeypatch) -> None:
    harness = _load_harness()
    body = _fixture_wav()
    object_key = (
        "tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/A-1001_20250526_122300.wav"
    )
    with SessionLocal.begin() as session:
        existing = session.get(StorageObject, "sto_rec_A_1001_20250526_122300")
        if existing is not None:
            session.delete(existing)
            session.flush()
        session.add(
            StorageObject(
                storage_object_id="sto_rec_A_1001_20250526_122300",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                provider="minio",
                bucket="auris-flow-local",
                object_key=object_key,
                object_key_sha256=hashlib.sha256(object_key.encode()).hexdigest(),
                source_type="audio_recording",
                source_id="rec_A_1001_20250526_122300",
                content_type="audio/wav",
                size_bytes=len(body),
                content_sha256=hashlib.sha256(body).hexdigest(),
                etag="registered-etag",
                status="verified",
                trace_id="trace-registered-audio",
                payload={"object_version_id": "registered-version-id"},
            )
        )

    class FakeStorageClient:
        def __init__(self) -> None:
            self.put_bodies: list[bytes] = []

        def _ensure_bucket(self) -> None:
            raise AssertionError("an already registered fixture must not be uploaded again")

        def head_object(
            self,
            bucket: str,
            key: str,
            *,
            version_id: str | None = None,
        ) -> dict[str, object]:
            assert bucket == "auris-flow-local"
            assert key == object_key
            assert version_id == "registered-version-id"
            return {
                "etag": "registered-etag",
                "content_length": len(body),
                "version_id": version_id,
            }

        def _request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            content_type: str,
        ) -> dict[str, str]:
            assert method == "PUT"
            assert path == f"/auris-flow-local/{object_key}"
            assert content_type == "audio/wav"
            self.put_bodies.append(body)
            return {"etag": "replacement-etag" if len(self.put_bodies) == 1 else "registered-etag"}

    storage_client = FakeStorageClient()
    monkeypatch.setenv("AURIS_OBJECT_STORAGE_ADAPTER", "real")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "auris-flow-local")
    monkeypatch.setattr(harness, "RealObjectStorageClient", lambda: storage_client)
    monkeypatch.setattr(
        harness,
        "wait_for_aggregate_outbox_event",
        lambda **_kwargs: SimpleNamespace(
            event_id=1,
            attempt_count=1,
            reconcile_attempt_count=0,
        ),
    )

    def bff_json_request(method: str, path: str, **_kwargs):
        assert method != "PUT", "the governed object must not be registered twice"
        assert method == "POST"
        assert path.endswith("/playback-grants")
        return {
            "data": {
                "playback_url": "/api/v1/audio-playback?grant=test-grant",
                "status": "issued",
            }
        }

    monkeypatch.setattr(harness, "bff_json_request", bff_json_request)
    whole_reads = 0

    def bff_binary_request(
        method: str,
        path: str,
        *,
        extra_headers: dict[str, str] | None = None,
        include_default_context: bool,
    ) -> dict[str, object]:
        nonlocal whole_reads
        assert method == "GET"
        assert path == "/api/v1/audio-playback?grant=test-grant"
        assert include_default_context is False
        requested_range = (extra_headers or {}).get("Range")
        if requested_range == "bytes=0-15":
            return {
                "status": 206,
                "headers": {
                    "accept-ranges": "bytes",
                    "content-range": f"bytes 0-15/{len(body)}",
                    "x-storage-object-id": "sto_rec_A_1001_20250526_122300",
                },
                "body": body[:16],
            }
        if requested_range == "bytes=999999-1000000":
            return {
                "status": 416,
                "headers": {"content-range": f"bytes */{len(body)}"},
                "body": b"",
            }
        whole_reads += 1
        if whole_reads == 1:
            return {
                "status": 200,
                "headers": {
                    "accept-ranges": "bytes",
                    "x-audio-source": "object_storage",
                    "x-storage-object-id": "sto_rec_A_1001_20250526_122300",
                },
                "body": body,
            }
        return {
            "status": 200,
            "headers": {
                "accept-ranges": "bytes",
                "x-audio-source": "object_storage",
                "x-storage-object-id": "sto_rec_A_1001_20250526_122300",
            },
            "body": body,
        }

    monkeypatch.setattr(harness, "bff_binary_request", bff_binary_request)

    result = harness.verify_audio_recording_range_stream()

    assert result["status"] == "ok"
    assert result["registration_event_processed"] == 1
    assert result["replacement_current_version_changed"] is True
    assert result["registered_version_continuity_status"] == 200
    assert result["registered_version_body_match"] is True
    assert len(storage_client.put_bodies) == 2
    assert storage_client.put_bodies[0] != body
    assert storage_client.put_bodies[1] == body
