#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal  # noqa: E402
from app.models import OutboxEvent, RunRecord  # noqa: E402
from app.workers.outbox_worker import process_aggregate_events  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process one E2E run's outbox event without draining unrelated work."
    )
    parser.add_argument("run_id")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
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
