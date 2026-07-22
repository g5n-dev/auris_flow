#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal  # noqa: E402
from app.models import OutboxEvent, RunRecord  # noqa: E402
from app.workers.outbox_worker import process_aggregate_events  # noqa: E402


EXTERNAL_ID_FIELD_BY_ADAPTER = {
    "dagster": "external_run_id",
    "external_callback": "callback_receipt_id",
    "object_storage": "storage_object_id",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process one E2E run's outbox event without draining unrelated work."
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Read already-processed dispatch evidence without invoking a worker.",
    )
    parser.add_argument("--tenant-id")
    parser.add_argument("--project-id")
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


def main() -> None:
    args = parse_args()
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
