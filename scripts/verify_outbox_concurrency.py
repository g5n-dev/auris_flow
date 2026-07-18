from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.database import SessionLocal  # noqa: E402
from app.models import OutboxEvent  # noqa: E402
from app.repositories.outbox_events import (  # noqa: E402
    OutboxClaim,
    claim_events,
    database_utc_now,
    lock_owned_claim,
)


WORKER_COUNT = 8
EVENT_COUNT = 16
CLAIM_LIMIT = EVENT_COUNT // WORKER_COUNT


def _claim(
    barrier: threading.Barrier, worker_id: str, aggregate_prefix: str
) -> list[OutboxClaim]:
    with SessionLocal() as session:
        barrier.wait(timeout=10)
        claims = claim_events(
            session,
            worker_id=worker_id,
            limit=CLAIM_LIMIT,
            lease_seconds=30,
            max_attempts_cap=5,
            aggregate_ids=[
                f"{aggregate_prefix}-{index}" for index in range(EVENT_COUNT)
            ],
        )
        # Keep row locks open long enough to prove SKIP LOCKED under overlapping transactions.
        time.sleep(0.2)
        session.commit()
        return claims


def main() -> None:
    run_id = uuid.uuid4().hex[:12]
    aggregate_prefix = f"outbox-concurrency-{run_id}"
    dispatch_prefix = f"outbox_concurrency_{run_id}"
    event_ids: list[int] = []
    try:
        with SessionLocal() as session:
            for index in range(EVENT_COUNT):
                event = OutboxEvent(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    event_type="verification.outbox.requested",
                    aggregate_type="verification_run",
                    aggregate_id=f"{aggregate_prefix}-{index}",
                    status="pending",
                    payload={"verification_run_id": run_id, "sequence": index},
                    dispatch_idempotency_key=f"{dispatch_prefix}_{index}",
                )
                session.add(event)
                session.flush()
                event_ids.append(event.event_id)
            session.commit()

        barrier = threading.Barrier(WORKER_COUNT)
        with ThreadPoolExecutor(max_workers=WORKER_COUNT) as pool:
            batches = list(
                pool.map(
                    lambda index: _claim(
                        barrier, f"mysql-worker-{index}", aggregate_prefix
                    ),
                    range(WORKER_COUNT),
                )
            )
        claims = [claim for batch in batches for claim in batch]
        claimed_ids = [claim.event_id for claim in claims]
        if len(claims) != EVENT_COUNT or len(set(claimed_ids)) != EVENT_COUNT:
            raise RuntimeError(
                f"outbox claims were not exclusive: claims={len(claims)} unique={len(set(claimed_ids))}"
            )
        if set(claimed_ids) != set(event_ids):
            raise RuntimeError(
                "workers did not claim the complete verification event set"
            )

        first_claim = next(claim for claim in claims if claim.event_id == event_ids[0])
        with SessionLocal() as session:
            claimed_event = session.get(OutboxEvent, event_ids[0])
            if claimed_event is None:
                raise RuntimeError(
                    "verification event disappeared before lease takeover"
                )
            claimed_event.lease_expires_at = database_utc_now(session) - timedelta(
                seconds=1
            )
            session.commit()
        with SessionLocal() as session:
            takeover = claim_events(
                session,
                worker_id="mysql-takeover-worker",
                limit=1,
                lease_seconds=30,
                max_attempts_cap=5,
                aggregate_ids=[f"{aggregate_prefix}-0"],
            )[0]
            session.commit()

        if takeover.lease_generation != first_claim.lease_generation + 1:
            raise RuntimeError(
                "lease takeover did not increment the fencing generation"
            )
        with SessionLocal() as session:
            if lock_owned_claim(session, first_claim) is not None:
                raise RuntimeError("stale worker still owns the reclaimed outbox event")
            if lock_owned_claim(session, takeover) is None:
                raise RuntimeError(
                    "takeover worker does not own the reclaimed outbox event"
                )

        with SessionLocal() as session:
            status_rows = list(
                session.execute(
                    select(
                        OutboxEvent.status,
                        OutboxEvent.claimed_by,
                        OutboxEvent.lease_generation,
                    ).where(OutboxEvent.event_id.in_(event_ids))
                )
            )
        print(
            json.dumps(
                {
                    "status": "success",
                    "workers": WORKER_COUNT,
                    "events": EVENT_COUNT,
                    "unique_claims": len(set(claimed_ids)),
                    "takeover_generation": takeover.lease_generation,
                    "rows": len(status_rows),
                },
                sort_keys=True,
            )
        )
    finally:
        if event_ids:
            with SessionLocal() as session:
                session.execute(
                    delete(OutboxEvent).where(OutboxEvent.event_id.in_(event_ids))
                )
                session.commit()


if __name__ == "__main__":
    main()
