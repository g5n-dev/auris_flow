#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
    StorageObject,
)
from app.services.run_service import RUN_EVENT_TYPES  # noqa: E402
from app.workers.outbox_worker import process_aggregate_events  # noqa: E402


EXTERNAL_ID_FIELD_BY_ADAPTER = {
    "dagster": "external_run_id",
    "external_callback": "callback_receipt_id",
    "object_storage": "storage_object_id",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_BUSINESS_STATUS_BY_RUN_TYPE: dict[str, dict[str, frozenset[str]]] = {
    run_type: {
        "success": frozenset(
            {"awaiting-review" if run_type == "label_optimization" else "completed"}
        ),
        "failed": frozenset({"failed"}),
        **(
            {"blocked": frozenset({"blocked"})}
            if run_type in {"eval_run", "release_command"}
            else {}
        ),
    }
    for run_type in RUN_EVENT_TYPES
}


def is_valid_terminal_business_state(
    run_type: object,
    run_status: object,
    business_status: object,
) -> bool:
    if (
        not isinstance(run_type, str)
        or not isinstance(run_status, str)
        or not isinstance(business_status, str)
    ):
        return False
    statuses_by_run_status = TERMINAL_BUSINESS_STATUS_BY_RUN_TYPE.get(run_type)
    if statuses_by_run_status is None:
        return False
    allowed_business_statuses = statuses_by_run_status.get(run_status)
    return bool(
        allowed_business_statuses is not None
        and business_status in allowed_business_statuses
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process one E2E run's outbox event without draining unrelated work."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--read-only",
        action="store_true",
        help="Read already-processed dispatch evidence without invoking a worker.",
    )
    mode.add_argument(
        "--read-completion",
        action="store_true",
        help="Read completed signed-receipt evidence without exposing internal secrets.",
    )
    parser.add_argument("--tenant-id")
    parser.add_argument("--project-id")
    parser.add_argument("--completion-receipt-id")
    parser.add_argument("--expected-adapter")
    parser.add_argument("--expected-external-id")
    parser.add_argument("--expected-signature-key-id")
    parser.add_argument("--expected-source")
    parser.add_argument("--expected-body-sha256")
    parser.add_argument("--expected-nonce")
    parser.add_argument("run_id")
    return parser.parse_args()


def read_run_dispatch_evidence(
    run_id: str,
    *,
    tenant_id: str,
    project_id: str,
) -> dict[str, Any]:
    with SessionLocal() as session:
        run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == run_id,
                RunRecord.tenant_id == tenant_id,
                RunRecord.project_id == project_id,
            )
        )
        if run is None:
            raise SystemExit(f"scoped run not found: {run_id}")
        payload = run.payload if isinstance(run.payload, dict) else {}
        if (
            run.status != "submitted"
            or payload.get("business_status") != "awaiting_completion"
            or payload.get("business_completion_required") is not True
        ):
            raise SystemExit(
                f"run has not reached the external dispatch boundary: {run_id}"
            )

        processed_event_id = payload.get("processed_event_id")
        if (
            not isinstance(processed_event_id, int)
            or isinstance(processed_event_id, bool)
            or processed_event_id <= 0
        ):
            raise SystemExit(f"run has no exact processed outbox event: {run_id}")
        event = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_id == processed_event_id,
                OutboxEvent.tenant_id == tenant_id,
                OutboxEvent.project_id == project_id,
                OutboxEvent.aggregate_id == run_id,
            )
        )
        if event is None:
            raise SystemExit(f"processed outbox event not found for run: {run_id}")
        if event.status != "processed" or event.attempt_count < 1:
            raise SystemExit(f"run outbox event is not processed: {run_id}")

        raw_dispatch = payload.get("dispatch")
        dispatch: dict[str, Any] = (
            raw_dispatch if isinstance(raw_dispatch, dict) else {}
        )
        event_payload = event.payload if isinstance(event.payload, dict) else {}
        if not dispatch or event_payload.get("adapter_dispatch") != dispatch:
            raise SystemExit(
                f"run dispatch does not match processed outbox evidence: {run_id}"
            )
        adapter = dispatch.get("adapter")
        details = dispatch.get("details")
        external_id_field = EXTERNAL_ID_FIELD_BY_ADAPTER.get(adapter)
        external_id = (
            details.get(external_id_field)
            if isinstance(details, dict) and external_id_field is not None
            else None
        )
        if (
            dispatch.get("status") != "success"
            or not isinstance(adapter, str)
            or external_id_field is None
            or not isinstance(external_id, str)
            or not external_id.strip()
            or external_id != external_id.strip()
        ):
            raise SystemExit(
                f"run dispatch has no trusted adapter external identity: {run_id}"
            )

        return {
            "run_id": run.run_id,
            "run_type": run.run_type,
            "run_status": run.status,
            "business_status": payload.get("business_status"),
            "business_completion_required": payload.get("business_completion_required"),
            "event_id": event.event_id,
            "event_status": event.status,
            "adapter": adapter,
            "external_id": external_id,
            "dispatch": dispatch,
        }


def _completion_storage_evidence(
    session: Any,
    *,
    run: RunRecord,
    request_body: dict[str, Any],
) -> list[dict[str, Any]]:
    result_ref = request_body.get("result_ref")
    raw_descriptors = (
        result_ref.get("storage_objects") if isinstance(result_ref, dict) else []
    )
    if raw_descriptors is None:
        return []
    if not isinstance(raw_descriptors, list):
        raise SystemExit(f"completion storage descriptors are not a list: {run.run_id}")

    evidence: list[dict[str, Any]] = []
    seen_storage_object_ids: set[str] = set()
    expected_root_trace_id = str(run.payload.get("root_trace_id") or run.trace_id or "")
    if not expected_root_trace_id:
        raise SystemExit(f"completed run has no root trace identity: {run.run_id}")
    for index, raw_descriptor in enumerate(raw_descriptors):
        if not isinstance(raw_descriptor, dict):
            raise SystemExit(
                f"completion storage descriptor {index} is not an object: {run.run_id}"
            )
        storage_object_id = raw_descriptor.get("storage_object_id")
        if not isinstance(storage_object_id, str) or not storage_object_id.strip():
            raise SystemExit(
                f"completion storage descriptor {index} has no identity: {run.run_id}"
            )
        if storage_object_id in seen_storage_object_ids:
            raise SystemExit(
                f"completion storage descriptor identity is duplicated: {storage_object_id}"
            )
        seen_storage_object_ids.add(storage_object_id)
        storage_object = session.scalar(
            select(StorageObject).where(
                StorageObject.storage_object_id == storage_object_id,
                StorageObject.tenant_id == run.tenant_id,
                StorageObject.project_id == run.project_id,
            )
        )
        if storage_object is None:
            raise SystemExit(
                f"scoped completion storage object not found: {storage_object_id}"
            )
        expected_fields = {
            "provider": storage_object.provider,
            "bucket": storage_object.bucket,
            "object_key": storage_object.object_key,
            "content_type": storage_object.content_type,
            "size_bytes": storage_object.size_bytes,
            "content_sha256": storage_object.content_sha256,
            "etag": storage_object.etag,
        }
        if any(
            raw_descriptor.get(field) != value
            for field, value in expected_fields.items()
        ):
            raise SystemExit(
                f"completion storage descriptor does not match registered object: {storage_object_id}"
            )
        storage_payload = (
            storage_object.payload if isinstance(storage_object.payload, dict) else {}
        )
        role = raw_descriptor.get("role")
        if (
            not isinstance(role, str)
            or storage_payload.get("role") != role
            or storage_object.source_id != run.run_id
            or storage_object.source_type != run.run_type
            or storage_object.status != "verified"
            or storage_object.trace_id != expected_root_trace_id
            or storage_payload.get("root_trace_id") != expected_root_trace_id
            or storage_payload.get("run_id") != run.run_id
            or storage_payload.get("run_type") != run.run_type
            or storage_payload.get("object_version_id")
            != raw_descriptor.get("version_id")
        ):
            raise SystemExit(
                f"completion storage object lacks trusted registration evidence: {storage_object_id}"
            )
        evidence.append(
            {
                "ordinal": index,
                "role": role,
                "content_sha256": storage_object.content_sha256,
                "source_type": storage_object.source_type,
                "source_id": storage_object.source_id,
                "status": storage_object.status,
                "trace_id": storage_object.trace_id,
            }
        )
    return evidence


def read_completion_receipt_evidence(
    run_id: str,
    *,
    tenant_id: str,
    project_id: str,
    completion_receipt_id: str,
    expected_adapter: str,
    expected_external_id: str,
    expected_signature_key_id: str,
    expected_source: str,
    expected_body_sha256: str,
    expected_nonce: str,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(expected_body_sha256):
        raise SystemExit("expected completion body hash is not canonical sha256")
    with SessionLocal() as session:
        run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == run_id,
                RunRecord.tenant_id == tenant_id,
                RunRecord.project_id == project_id,
            )
        )
        if run is None:
            raise SystemExit(f"scoped completed run not found: {run_id}")
        payload = run.payload if isinstance(run.payload, dict) else {}
        internal_receipt = payload.get("completion_receipt")
        business_status = payload.get("business_status")
        if (
            run.status not in {"success", "failed", "blocked"}
            or payload.get("business_completion_required") is not False
            or not isinstance(business_status, str)
            or not business_status.strip()
            or business_status != business_status.strip()
            or business_status in {"awaiting_completion", "dispatching", "queued"}
            or not isinstance(internal_receipt, dict)
            or internal_receipt.get("completion_receipt_id") != completion_receipt_id
            or internal_receipt.get("status") != run.status
        ):
            raise SystemExit(f"run has no completed receipt boundary: {run_id}")
        if not is_valid_terminal_business_state(
            run.run_type,
            run.status,
            business_status,
        ):
            raise SystemExit(f"run has invalid terminal business state: {run_id}")

        receipt = session.scalar(
            select(RunCompletionReceipt).where(
                RunCompletionReceipt.run_id == run_id,
                RunCompletionReceipt.tenant_id == tenant_id,
                RunCompletionReceipt.project_id == project_id,
                RunCompletionReceipt.completion_receipt_id == completion_receipt_id,
            )
        )
        if receipt is None:
            raise SystemExit(
                f"scoped completion receipt not found: {completion_receipt_id}"
            )
        if (
            receipt.processing_state != "completed"
            or receipt.status_code != 200
            or receipt.completed_at is None
            or receipt.completion_status != run.status
        ):
            raise SystemExit(
                f"completion receipt is not finalized: {completion_receipt_id}"
            )

        request_body = (
            receipt.request_body if isinstance(receipt.request_body, dict) else {}
        )
        requested_status = str(request_body.get("status") or "success")
        expected_terminal_statuses = (
            {"success", "blocked"} if requested_status == "success" else {"failed"}
        )
        if (
            receipt.adapter != expected_adapter
            or receipt.source != expected_source
            or receipt.external_id != expected_external_id
            or request_body.get("adapter") != expected_adapter
            or request_body.get("external_id") != expected_external_id
            or request_body.get("completion_receipt_id") != completion_receipt_id
            or requested_status not in {"success", "failed"}
            or run.status not in expected_terminal_statuses
            or internal_receipt.get("status") != run.status
            or internal_receipt.get("adapter") != expected_adapter
            or internal_receipt.get("source") != expected_source
            or internal_receipt.get("external_id") != expected_external_id
        ):
            raise SystemExit(
                f"completion receipt identity drift: {completion_receipt_id}"
            )

        auth = internal_receipt.get("auth")
        if not isinstance(auth, dict):
            raise SystemExit(
                f"completion receipt has no internal auth: {completion_receipt_id}"
            )
        if (
            auth.get("auth_mode") != "signed_external_completion"
            or auth.get("signature_binding_mode") != "scoped_key_map"
            or auth.get("signature_key_id") != expected_signature_key_id
            or auth.get("authenticated_source") != expected_source
            or auth.get("authenticated_tenant_id") != tenant_id
            or auth.get("authenticated_project_id") != project_id
            or auth.get("body_sha256") != expected_body_sha256
            or auth.get("nonce") != expected_nonce
            or auth.get("signature_mode") != "hmac-sha256"
            or receipt.signature_key_id != expected_signature_key_id
            or receipt.authenticated_source != expected_source
            or receipt.signature_body_hash != expected_body_sha256
            or receipt.signature_nonce != expected_nonce
            or receipt.signature_mode != "hmac-sha256"
            or receipt.signature_request_hash != auth.get("request_sha256")
            or receipt.signed_at != auth.get("signed_at")
            or not isinstance(receipt.signature_request_hash, str)
            or not SHA256_PATTERN.fullmatch(receipt.signature_request_hash)
            or not isinstance(receipt.receipt_hash, str)
            or not SHA256_PATTERN.fullmatch(receipt.receipt_hash)
            or internal_receipt.get("receipt_hash") != receipt.receipt_hash
        ):
            raise SystemExit(
                f"completion receipt signed evidence drift: {completion_receipt_id}"
            )

        response_json = (
            receipt.response_json if isinstance(receipt.response_json, dict) else {}
        )
        response_data = response_json.get("data")
        response_summary = (
            response_data.get("completion_receipt")
            if isinstance(response_data, dict)
            else None
        )
        if (
            not isinstance(response_data, dict)
            or response_data.get("run_id") != run_id
            or response_data.get("tenant_id") != tenant_id
            or response_data.get("project_id") != project_id
            or response_data.get("run_type") != run.run_type
            or response_data.get("status") != run.status
            or response_data.get("business_status") != business_status
            or response_data.get("business_completion_required") is not False
            or not isinstance(response_summary, dict)
            or response_summary.get("completion_receipt_id") != completion_receipt_id
            or response_summary.get("status") != run.status
        ):
            raise SystemExit(
                f"completion receipt public response drift: {completion_receipt_id}"
            )

        storage_evidence = _completion_storage_evidence(
            session,
            run=run,
            request_body=request_body,
        )
        return {
            "verified": True,
            "run_id": run.run_id,
            "run_type": run.run_type,
            "run_status": run.status,
            "business_status": business_status,
            "business_completion_required": payload.get("business_completion_required"),
            "completion_receipt_id": completion_receipt_id,
            "completion_status": receipt.completion_status,
            "receipt_state": receipt.processing_state,
            "status_code": receipt.status_code,
            "auth": {
                "auth_mode": auth.get("auth_mode"),
                "binding_mode": auth.get("signature_binding_mode"),
                "signature_mode": receipt.signature_mode,
                "key_id": receipt.signature_key_id,
                "source": receipt.authenticated_source,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "body_sha256": receipt.signature_body_hash,
            },
            "storage_objects": storage_evidence,
        }


def main() -> None:
    args = parse_args()
    if args.read_completion:
        required = {
            "--tenant-id": args.tenant_id,
            "--project-id": args.project_id,
            "--completion-receipt-id": args.completion_receipt_id,
            "--expected-adapter": args.expected_adapter,
            "--expected-external-id": args.expected_external_id,
            "--expected-signature-key-id": args.expected_signature_key_id,
            "--expected-source": args.expected_source,
            "--expected-body-sha256": args.expected_body_sha256,
            "--expected-nonce": args.expected_nonce,
        }
        missing = [flag for flag, value in required.items() if not value]
        if missing:
            raise SystemExit("--read-completion requires " + ", ".join(sorted(missing)))
        result = read_completion_receipt_evidence(
            args.run_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            completion_receipt_id=args.completion_receipt_id,
            expected_adapter=args.expected_adapter,
            expected_external_id=args.expected_external_id,
            expected_signature_key_id=args.expected_signature_key_id,
            expected_source=args.expected_source,
            expected_body_sha256=args.expected_body_sha256,
            expected_nonce=args.expected_nonce,
        )
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.read_only:
        if not args.tenant_id or not args.project_id:
            raise SystemExit("--read-only requires --tenant-id and --project-id")
        result = read_run_dispatch_evidence(
            args.run_id,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
        )
        print(json.dumps(result, ensure_ascii=False))
        return
    worker_id = f"e2e-inline-{os.getpid()}"
    processed = process_aggregate_events([args.run_id], limit=20, worker_id=worker_id)
    with SessionLocal() as session:
        run = session.get(RunRecord, args.run_id)
        if run is None:
            raise SystemExit(f"run not found after outbox processing: {args.run_id}")
        event = (
            session.query(OutboxEvent)
            .filter(OutboxEvent.aggregate_id == args.run_id)
            .order_by(OutboxEvent.created_at.desc())
            .first()
        )
        payload = run.payload if isinstance(run.payload, dict) else {}
        raw_dispatch = payload.get("dispatch")
        dispatch: dict[str, Any] = (
            raw_dispatch if isinstance(raw_dispatch, dict) else {}
        )
        result = {
            "run_id": run.run_id,
            "run_type": run.run_type,
            "run_status": run.status,
            "business_status": payload.get("business_status"),
            "processed": processed,
            "event_id": event.event_id if event else None,
            "event_status": event.status if event else None,
            "adapter": dispatch.get("adapter"),
            "dispatch": dispatch,
        }
    if result["run_status"] != "submitted" or not result["adapter"]:
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(
            "E2E run did not reach submitted state with an adapter receipt"
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
