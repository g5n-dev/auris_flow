#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal  # type: ignore[import-not-found]  # noqa: E402
from app.models import OutboxEvent, StorageObject  # type: ignore[import-not-found]  # noqa: E402
from app.services.adapters import RealObjectStorageClient  # type: ignore[import-not-found]  # noqa: E402


AUDIO_SESSION_ID = "S20250526-000128"
RECORDING_ID = "rec_A_1001_20250526_122300"
STORAGE_OBJECT_ID = f"sto_{RECORDING_ID}"
OBJECT_KEY = (
    "tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/"
    "A-1001_20250526_122300.wav"
)
EVIDENCE_STORAGE_OBJECT_ID = "storage_badcase_a_4107_evidence"
EVIDENCE_OBJECT_KEY = (
    "tenants/aurora_auto/projects/sales_qa/evidence/AF-131/hotword-diff.json"
)
EVIDENCE_BODY = (
    '{"audio_session_id":"S20250526-000131","badcase_id":"A-4107",'
    '"error_type":"misrecognition","evidence_span":{"end_ms":17120,'
    '"start_ms":16380},"recognized_text":"星月L","redacted":true,'
    '"schema_version":"auris.hotword-evidence.v1","standard_term":"星越L"}'
).encode("utf-8")
EVAL_DATASET_MANIFEST_STORAGE_OBJECT_ID = "storage_evalset_asr_hotword_v1_manifest"
EVAL_DATASET_MANIFEST_OBJECT_KEY = (
    "tenants/aurora_auto/projects/sales_qa/eval-datasets/"
    "evalset-asr-hotword-v1/manifest.jsonl"
)


def eval_dataset_manifest_bytes() -> bytes:
    lines = []
    terms = ("xingyue-l", "yinhe-e8", "lynk-08")
    for index in range(1, 41):
        lines.append(
            json.dumps(
                {
                    "audio_storage_object_id": STORAGE_OBJECT_ID,
                    "expected_terms": [terms[index % len(terms)]],
                    "sample_id": f"hotword-{index:03d}",
                    "synthetic": True,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def wav_fixture_bytes() -> bytes:
    sample_rate = 8000
    sample_count = sample_rate
    pcm = b"".join(
        struct.pack("<h", 1200 if (index // 80) % 2 == 0 else -1200)
        for index in range(sample_count)
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


def fail(message: str, details: dict | None = None) -> None:
    print(
        json.dumps(
            {"status": "failed", "message": message, "details": details or {}},
            ensure_ascii=False,
            indent=2,
        ),
        file=sys.stderr,
    )
    raise SystemExit(1)


def wait_for_worker_projection(
    *,
    expected_size: int,
    expected_checksum: str,
    expected_trace_id: str,
) -> tuple[StorageObject, int]:
    timeout_seconds = max(
        1.0,
        float(os.environ.get("AURIS_E2E_OUTBOX_TIMEOUT_SECONDS", "30")),
    )
    poll_seconds = max(
        0.05,
        float(os.environ.get("AURIS_E2E_OUTBOX_POLL_INTERVAL_SECONDS", "0.2")),
    )
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, object] = {}
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            storage_object = session.get(StorageObject, STORAGE_OBJECT_ID)
            processed_events = list(
                session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == STORAGE_OBJECT_ID,
                        OutboxEvent.event_type == "audio_recording.object_registered",
                        OutboxEvent.payload["trace_id"].as_string()
                        == expected_trace_id,
                        OutboxEvent.status == "processed",
                    )
                )
            )
            if (
                storage_object
                and storage_object.size_bytes == expected_size
                and storage_object.content_sha256 == expected_checksum
                and processed_events
            ):
                session.expunge(storage_object)
                return storage_object, len(processed_events)
            last_status = {
                "storage_object": bool(storage_object),
                "processed_event_count": len(processed_events),
            }
        time.sleep(poll_seconds)
    fail(
        "Managed outbox worker did not materialize real-stack audio metadata",
        {
            **last_status,
            "storage_object_id": STORAGE_OBJECT_ID,
            "trace_id": expected_trace_id,
            "timeout_seconds": timeout_seconds,
        },
    )
    raise AssertionError("unreachable")


def main() -> None:
    bff_url = os.environ.get("AURIS_E2E_BFF_URL", "").rstrip("/")
    if not bff_url or not os.environ.get("DATABASE_URL"):
        fail("AURIS_E2E_BFF_URL and DATABASE_URL are required")
    body = wav_fixture_bytes()
    client = RealObjectStorageClient()
    bucket = os.environ.get("OBJECT_STORAGE_BUCKET", "auris-flow-local")
    evidence_checksum = hashlib.sha256(EVIDENCE_BODY).hexdigest()
    eval_manifest_body = eval_dataset_manifest_bytes()
    eval_manifest_checksum = hashlib.sha256(eval_manifest_body).hexdigest()
    with SessionLocal() as session:
        evidence_descriptor = session.get(StorageObject, EVIDENCE_STORAGE_OBJECT_ID)
        if evidence_descriptor is None:
            fail(
                "Seeded hotword evidence descriptor is missing",
                {"storage_object_id": EVIDENCE_STORAGE_OBJECT_ID},
            )
        descriptor_mismatches = {
            field: {"expected": expected, "actual": actual}
            for field, expected, actual in (
                ("bucket", bucket, evidence_descriptor.bucket),
                ("object_key", EVIDENCE_OBJECT_KEY, evidence_descriptor.object_key),
                ("size_bytes", len(EVIDENCE_BODY), evidence_descriptor.size_bytes),
                (
                    "content_sha256",
                    evidence_checksum,
                    evidence_descriptor.content_sha256,
                ),
                ("status", "verified", evidence_descriptor.status),
            )
            if expected != actual
        }
        if descriptor_mismatches:
            fail(
                "Seeded hotword evidence descriptor does not match fixture bytes",
                descriptor_mismatches,
            )
        eval_manifest_descriptor = session.get(
            StorageObject,
            EVAL_DATASET_MANIFEST_STORAGE_OBJECT_ID,
        )
        if eval_manifest_descriptor is None:
            fail(
                "Seeded evaluation dataset manifest descriptor is missing",
                {"storage_object_id": EVAL_DATASET_MANIFEST_STORAGE_OBJECT_ID},
            )
        eval_manifest_mismatches = {
            field: {"expected": expected, "actual": actual}
            for field, expected, actual in (
                ("bucket", bucket, eval_manifest_descriptor.bucket),
                (
                    "object_key",
                    EVAL_DATASET_MANIFEST_OBJECT_KEY,
                    eval_manifest_descriptor.object_key,
                ),
                (
                    "size_bytes",
                    len(eval_manifest_body),
                    eval_manifest_descriptor.size_bytes,
                ),
                (
                    "content_sha256",
                    eval_manifest_checksum,
                    eval_manifest_descriptor.content_sha256,
                ),
                (
                    "content_type",
                    "application/x-ndjson",
                    eval_manifest_descriptor.content_type,
                ),
                ("status", "verified", eval_manifest_descriptor.status),
            )
            if expected != actual
        }
        if eval_manifest_mismatches:
            fail(
                "Seeded evaluation dataset manifest descriptor does not match fixture bytes",
                eval_manifest_mismatches,
            )
    try:
        client._ensure_bucket()
        put_result = client._request(
            "PUT",
            f"/{bucket}/{OBJECT_KEY}",
            body=body,
            content_type="audio/wav",
        )
        evidence_put_result = client._request(
            "PUT",
            f"/{bucket}/{EVIDENCE_OBJECT_KEY}",
            body=EVIDENCE_BODY,
            content_type="application/json",
        )
        eval_manifest_put_result = client._request(
            "PUT",
            f"/{bucket}/{EVAL_DATASET_MANIFEST_OBJECT_KEY}",
            body=eval_manifest_body,
            content_type="application/x-ndjson",
        )
        evidence_head = client.head_object(bucket, EVIDENCE_OBJECT_KEY)
        eval_manifest_head = client.head_object(
            bucket,
            EVAL_DATASET_MANIFEST_OBJECT_KEY,
        )
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        fail("Could not upload the real-stack storage fixtures", {"error": str(exc)})
    if int(str(evidence_head.get("content_length") or "-1")) != len(EVIDENCE_BODY):
        fail(
            "Real-stack hotword evidence HEAD size mismatch",
            {
                "expected": len(EVIDENCE_BODY),
                "actual": evidence_head.get("content_length"),
            },
        )
    if int(str(eval_manifest_head.get("content_length") or "-1")) != len(
        eval_manifest_body
    ):
        fail(
            "Real-stack evaluation dataset manifest HEAD size mismatch",
            {
                "expected": len(eval_manifest_body),
                "actual": eval_manifest_head.get("content_length"),
            },
        )
    expected_manifest_etag = hashlib.md5(
        eval_manifest_body,
        usedforsecurity=False,
    ).hexdigest()
    actual_manifest_etag = str(eval_manifest_head.get("etag") or "").strip('"')
    if actual_manifest_etag != expected_manifest_etag:
        fail(
            "Real-stack evaluation dataset manifest ETag mismatch",
            {"expected": expected_manifest_etag, "actual": actual_manifest_etag},
        )

    payload = {
        "storage_object_id": STORAGE_OBJECT_ID,
        "provider": "minio",
        "bucket": bucket,
        "object_key": OBJECT_KEY,
        "content_type": "audio/wav",
        "content_length": len(body),
        "checksum_sha256": hashlib.sha256(body).hexdigest(),
        "etag": put_result.get("etag"),
    }
    request = Request(
        f"{bff_url}/api/v1/audio-sessions/{AUDIO_SESSION_ID}/recording-object",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": "Bearer dev-token",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "X-Request-Id": "e2e-seed-audio-fixture",
            "X-Trace-Id": "trace_e2e_seed_audio_fixture",
            "Idempotency-Key": "e2e-seed-audio-fixture-v1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            registration = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        fail(
            "BFF rejected real-stack audio object registration",
            {"status": exc.code, "body": exc.read().decode("utf-8", errors="replace")},
        )
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        fail(
            "Could not register real-stack audio object through BFF",
            {"error": str(exc)},
        )

    registration_trace_id = registration.get("meta", {}).get("trace_id")
    if not isinstance(registration_trace_id, str) or not registration_trace_id:
        fail(
            "BFF audio object registration did not return a trace_id",
            {"registration": registration},
        )

    _storage_object, processed_event_count = wait_for_worker_projection(
        expected_size=len(body),
        expected_checksum=payload["checksum_sha256"],
        expected_trace_id=registration_trace_id,
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "audio_session_id": AUDIO_SESSION_ID,
                "storage_object_id": STORAGE_OBJECT_ID,
                "content_length": len(body),
                "checksum_sha256": payload["checksum_sha256"],
                "registration_trace_id": registration_trace_id,
                "processed_event_count": processed_event_count,
                "evidence_storage_object_id": EVIDENCE_STORAGE_OBJECT_ID,
                "evidence_content_length": len(EVIDENCE_BODY),
                "evidence_checksum_sha256": evidence_checksum,
                "evidence_etag": evidence_put_result.get("etag"),
                "eval_dataset_manifest_storage_object_id": (
                    EVAL_DATASET_MANIFEST_STORAGE_OBJECT_ID
                ),
                "eval_dataset_manifest_content_length": len(eval_manifest_body),
                "eval_dataset_manifest_checksum_sha256": eval_manifest_checksum,
                "eval_dataset_manifest_etag": eval_manifest_put_result.get("etag"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
