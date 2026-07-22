#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -n "${PYTHON:-}" ]; then
  PYTHON_BIN="${PYTHON}"
elif [ -x "${ROOT}/backend/.venv/bin/python" ]; then
  PYTHON_BIN="${ROOT}/backend/.venv/bin/python"
else
  PYTHON_BIN="$(command -v python3)"
fi

PLATFORM_RESULT="${AURIS_REAL_STACK_PLATFORM_RESULT:-${ROOT}/prototype/auris-flow-ui/e2e/artifacts/platform-bff-result.json}"
OUTBOX_RESULT="${AURIS_REAL_STACK_OUTBOX_RESULT:-${ROOT}/prototype/auris-flow-ui/e2e/artifacts/outbox-dispatch-result.json}"
VERIFICATION_RESULT="${AURIS_REAL_STACK_VERIFICATION_RESULT:-${ROOT}/build/release-evidence/real-stack-gate.json}"

"${PYTHON_BIN}" - "${PLATFORM_RESULT}" "${OUTBOX_RESULT}" "${VERIFICATION_RESULT}" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


platform_path = Path(sys.argv[1]).resolve()
outbox_path = Path(sys.argv[2]).resolve()
verification_path = Path(sys.argv[3]).resolve()


def fail(message: str, detail: Any | None = None) -> None:
    payload: dict[str, Any] = {"status": "failed", "message": message}
    if detail is not None:
        payload["detail"] = detail
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        fail(f"{label} artifact does not exist", {"path": str(path)})
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"{label} artifact is not valid JSON", {"path": str(path), "error": str(exc)})
    if not isinstance(value, dict):
        fail(f"{label} artifact must be a JSON object", {"path": str(path)})
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        fail(f"{label} is missing")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        fail(f"{label} is not an ISO-8601 timestamp", {"value": value})


platform = load_json(platform_path, "UI/BFF")
outbox = load_json(outbox_path, "outbox")
source_commit = os.environ.get("AURIS_REAL_STACK_SOURCE_COMMIT", "").strip().lower()
if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_commit) is None:
    fail("real-stack gate requires an exact source commit")
if platform.get("status") != "ok" or outbox.get("status") != "ok":
    fail(
        "real-stack source artifacts are not successful",
        {"ui_bff_status": platform.get("status"), "outbox_status": outbox.get("status")},
    )

platform_started = parse_time(platform.get("startedAt"), "UI/BFF startedAt")
platform_completed = parse_time(platform.get("completedAt"), "UI/BFF completedAt")
outbox_started = parse_time(outbox.get("artifact_started_at"), "outbox artifact_started_at")
outbox_completed = parse_time(outbox.get("completed_at"), "outbox completed_at")
if platform_completed < platform_started or outbox_completed < outbox_started:
    fail("real-stack artifact timestamps are out of order")
if platform_started != outbox_started:
    fail(
        "UI/BFF and outbox artifacts do not describe the same run",
        {"ui_bff_started_at": platform_started.isoformat(), "outbox_started_at": outbox_started.isoformat()},
    )
platform_run_id = platform.get("runId")
outbox_run_id = outbox.get("e2e_run_id")
if not isinstance(platform_run_id, str) or not platform_run_id:
    fail("UI/BFF artifact is missing runId")
if outbox_run_id != platform_run_id:
    fail(
        "UI/BFF and outbox artifacts do not describe the same run ID",
        {"ui_bff_run_id": platform_run_id, "outbox_run_id": outbox_run_id},
    )
run_started_at = os.environ.get("AURIS_REAL_STACK_STARTED_AT")
if run_started_at:
    try:
        minimum_started_at = float(run_started_at)
    except ValueError:
        fail("AURIS_REAL_STACK_STARTED_AT must be a Unix timestamp")
    if platform_started.timestamp() < minimum_started_at:
        fail(
            "real-stack artifact is stale",
            {"artifact_started_at": platform_started.isoformat(), "minimum_unix_time": minimum_started_at},
        )

artifact_ref = outbox.get("artifact")
outbox_ref = outbox.get("outbox_result_path")
if not isinstance(artifact_ref, str) or Path(artifact_ref).resolve() != platform_path:
    fail("outbox artifact points at a different UI/BFF artifact", {"artifact": artifact_ref})
if not isinstance(outbox_ref, str) or Path(outbox_ref).resolve() != outbox_path:
    fail("outbox artifact path does not match the validated file", {"outbox_result_path": outbox_ref})

serialized_outbox = json.dumps(outbox, ensure_ascii=True).lower()
fallback_markers = (
    "sqlite",
    "mock://object-storage",
    "mock-range-stream",
    "local_dispatch_receipts",
    "local_qdrant_projection",
)
found_fallbacks = [marker for marker in fallback_markers if marker in serialized_outbox]
if found_fallbacks:
    fail("real-stack artifact contains fallback evidence", {"markers": found_fallbacks})

database_ref = outbox.get("database_url")
expected_database_ref = os.environ.get("AURIS_REAL_STACK_EXPECTED_DATABASE_REF")
if not isinstance(database_ref, str) or not re.fullmatch(
    r"(?:127\.0\.0\.1|localhost):3306/auris_flow_e2e_[0-9]+_[0-9]+", database_ref
):
    fail("outbox artifact does not identify the isolated MySQL database", {"database_url": database_ref})
if not expected_database_ref or database_ref != expected_database_ref:
    fail(
        "outbox artifact database does not match the script-managed MySQL database",
        {"expected": expected_database_ref, "actual": database_ref},
    )
try:
    mysql_run_count = int(os.environ.get("AURIS_REAL_STACK_MYSQL_RUN_COUNT", "0"))
except ValueError:
    fail("AURIS_REAL_STACK_MYSQL_RUN_COUNT must be an integer")
if mysql_run_count <= 0:
    fail("script-managed MySQL database contains no run_records", {"count": mysql_run_count})
try:
    mysql_storage_object_count = int(
        os.environ.get("AURIS_REAL_STACK_MYSQL_STORAGE_OBJECT_COUNT", "0")
    )
except ValueError:
    fail("AURIS_REAL_STACK_MYSQL_STORAGE_OBJECT_COUNT must be an integer")
if mysql_storage_object_count <= 0:
    fail(
        "script-managed MySQL database contains no registered storage_objects",
        {"count": mysql_storage_object_count},
    )

dispatches = outbox.get("checked_dispatches")
if not isinstance(dispatches, list) or not dispatches:
    fail("outbox artifact contains no checked dispatches")
qdrant_dispatches = [item for item in dispatches if isinstance(item, dict) and item.get("adapter") == "qdrant"]
if not qdrant_dispatches:
    fail("outbox artifact contains no Qdrant dispatch")
required_qdrant_receipt_keys = {
    "mode",
    "qdrant_url",
    "collection",
    "point_ids",
    "qdrant_payload",
    "operation_id",
}
for dispatch in qdrant_dispatches:
    receipt_keys = set(dispatch.get("receipt_keys") or [])
    missing = sorted(required_qdrant_receipt_keys - receipt_keys)
    if missing:
        fail("Qdrant dispatch is not a real adapter receipt", {"run_id": dispatch.get("run_id"), "missing": missing})

recalls = outbox.get("checked_qdrant_recall")
if not isinstance(recalls, list) or not recalls:
    fail("outbox artifact contains no Qdrant recall proof")
invalid_recall_modes = [
    item.get("mode") if isinstance(item, dict) else type(item).__name__
    for item in recalls
    if not isinstance(item, dict) or item.get("mode") != "real_qdrant"
]
if invalid_recall_modes:
    fail("Qdrant recall did not use real_qdrant", {"modes": invalid_recall_modes})

object_dispatches = [
    item for item in dispatches if isinstance(item, dict) and item.get("adapter") == "object_storage"
]
if not object_dispatches:
    fail("outbox artifact contains no object-storage dispatch")
required_object_receipt_keys = {
    "mode",
    "endpoint",
    "provider",
    "protocol",
    "verified",
    "bucket",
    "object_key",
    "object_uri",
    "storage_object_id",
    "content_sha256",
    "content_length",
}
for dispatch in object_dispatches:
    receipt_keys = set(dispatch.get("receipt_keys") or [])
    missing = sorted(required_object_receipt_keys - receipt_keys)
    if missing:
        fail(
            "object-storage dispatch is not a real MinIO receipt",
            {"run_id": dispatch.get("run_id"), "missing": missing},
        )

range_check = outbox.get("checked_audio_range_stream")
if not isinstance(range_check, dict):
    fail("outbox artifact contains no audio Range proof")
expected_range_fields = {
    "status": "ok",
    "source": "object-storage",
    "range_status": 206,
    "invalid_range_status": 416,
    "metadata_registered": True,
    "registration_event_processed": 1,
    "replacement_current_version_changed": True,
    "registered_version_continuity_status": 200,
    "registered_version_body_match": True,
}
range_mismatches = {
    key: {"expected": expected, "actual": range_check.get(key)}
    for key, expected in expected_range_fields.items()
    if range_check.get(key) != expected
}
if range_mismatches or not isinstance(range_check.get("content_length"), int) or range_check["content_length"] <= 16:
    fail("audio Range proof is not backed by real object storage and HTTP 206", {"mismatches": range_mismatches, "proof": range_check})
storage_object_id = range_check.get("storage_object_id")
if not isinstance(storage_object_id, str) or not storage_object_id.startswith("sto_"):
    fail("audio Range proof is not linked to governed MySQL storage metadata", range_check)

coverage = outbox.get("coverage")
if not isinstance(coverage, dict):
    fail("outbox artifact is missing coverage")
if coverage.get("qdrant_recall_count") != len(recalls) or coverage.get("audio_range_stream") != 1:
    fail("outbox artifact coverage does not match real-stack proofs", {"coverage": coverage})

verification = {
    "schema_version": "auris.real-stack-gate.v1",
    "status": "ok",
    "source_commit": source_commit,
    "execution_environment": "compose-dependencies",
    "validated_at": datetime.now(UTC).isoformat(),
    "run_id": platform.get("runId"),
    "source_artifacts": {
        "ui_bff_sha256": sha256_file(platform_path),
        "outbox_sha256": sha256_file(outbox_path),
    },
    "database": {
        "backend": "mysql",
        "artifact_ref": database_ref,
        "run_record_count": mysql_run_count,
        "registered_storage_object_count": mysql_storage_object_count,
    },
    "qdrant": {
        "mode": "real_qdrant",
        "dispatch_count": len(qdrant_dispatches),
        "recall_count": len(recalls),
    },
    "object_storage": {
        "mode": "real",
        "provider": "minio",
        "dispatch_count": len(object_dispatches),
        "range_source": range_check.get("source"),
        "storage_object_id": storage_object_id,
        "metadata_registered": range_check.get("metadata_registered"),
    },
    "http_range": {
        "status": range_check.get("range_status"),
        "invalid_range_status": range_check.get("invalid_range_status"),
        "replacement_current_version_changed": range_check.get(
            "replacement_current_version_changed"
        ),
        "registered_version_continuity_status": range_check.get(
            "registered_version_continuity_status"
        ),
        "registered_version_body_match": range_check.get(
            "registered_version_body_match"
        ),
        "content_length": range_check.get("content_length"),
    },
    "rejected_fallback_markers": list(fallback_markers),
}
verification_path.parent.mkdir(parents=True, exist_ok=True)
temporary_path = verification_path.with_name(f".{verification_path.name}.{os.getpid()}.tmp")
temporary_path.write_text(json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
temporary_path.replace(verification_path)
print(json.dumps(verification, ensure_ascii=False, indent=2))
PY
