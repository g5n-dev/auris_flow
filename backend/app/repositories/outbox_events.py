from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import OutboxDeliveryAttempt, OutboxEvent


@dataclass(frozen=True)
class OutboxClaim:
    event_id: int
    claim_token: str
    lease_generation: int
    claimed_by: str
    lease_expires_at: datetime
    exhausted: bool = False


def insert_or_get_event(
    session: Session,
    event: OutboxEvent,
) -> tuple[OutboxEvent, bool]:
    """Insert an outbox event or return the row that won the unique-key race.

    The savepoint keeps a duplicate-key race from invalidating the caller's outer
    transaction.  The locking read is intentional: under MySQL's default repeatable
    read isolation it observes the row committed by the concurrent winner.
    """

    savepoint = session.begin_nested()
    try:
        session.add(event)
        session.flush([event])
    except IntegrityError:
        savepoint.rollback()
        existing = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.dispatch_idempotency_key == event.dispatch_idempotency_key)
            .with_for_update()
        )
        if existing is None:
            raise
        return existing, False
    else:
        savepoint.commit()
        return event, True


def _prepare_claim_transaction(session: Session) -> None:
    if session.get_bind().dialect.name != "mysql":
        return
    if session.in_transaction():
        raise RuntimeError("MySQL outbox claims require a fresh READ COMMITTED transaction")
    session.connection(execution_options={"isolation_level": "READ COMMITTED"})


def database_utc_now(session: Session) -> datetime:
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        value = session.scalar(select(func.utc_timestamp(6)))
    else:
        value = session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database did not return a timestamp for outbox leasing")
    return value


def claim_events(
    session: Session,
    *,
    worker_id: str,
    limit: int,
    lease_seconds: int,
    max_attempts_cap: int,
    aggregate_ids: list[str] | None = None,
) -> list[OutboxClaim]:
    if limit <= 0:
        return []
    _prepare_claim_transaction(session)
    bounded_lease_seconds = max(5, min(int(lease_seconds), 3600))
    now = database_utc_now(session)
    reclaim_statement = select(OutboxEvent).where(
        OutboxEvent.status == "processing",
        OutboxEvent.lease_expires_at.is_not(None),
        OutboxEvent.lease_expires_at <= now,
    )
    if aggregate_ids:
        reclaim_statement = reclaim_statement.where(OutboxEvent.aggregate_id.in_(aggregate_ids))
    reclaim_statement = (
        reclaim_statement.order_by(
            OutboxEvent.lease_expires_at,
            OutboxEvent.event_id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    events = list(session.scalars(reclaim_statement))

    remaining = limit - len(events)
    if remaining:
        pending_statement = select(OutboxEvent).where(
            OutboxEvent.status == "pending",
            OutboxEvent.available_at <= now,
        )
        if aggregate_ids:
            pending_statement = pending_statement.where(OutboxEvent.aggregate_id.in_(aggregate_ids))
        pending_statement = (
            pending_statement.order_by(
                OutboxEvent.available_at,
                OutboxEvent.event_id,
            )
            .limit(remaining)
            .with_for_update(skip_locked=True)
        )
        events.extend(session.scalars(pending_statement))
    lease_expires_at = now + timedelta(seconds=bounded_lease_seconds)
    claims: list[OutboxClaim] = []
    for event in events:
        configured_max_attempts = event.payload.get("max_attempts", max_attempts_cap)
        try:
            requested_max_attempts = int(configured_max_attempts)
        except (TypeError, ValueError):
            requested_max_attempts = max_attempts_cap
        effective_max_attempts = max(1, min(requested_max_attempts, max_attempts_cap))
        needs_reconciliation = event.delivery_state in {"outcome_unknown", "reconciling"} or (
            event.status == "processing" and event.attempt_count >= effective_max_attempts
        )
        if event.status == "processing":
            expired_attempt = session.scalar(
                select(OutboxDeliveryAttempt).where(
                    OutboxDeliveryAttempt.event_id == event.event_id,
                    OutboxDeliveryAttempt.lease_generation == event.lease_generation,
                )
            )
            if expired_attempt is not None and expired_attempt.status in {
                "claimed",
                "prepared",
                "remote_call_started",
                "reconcile_started",
            }:
                expired_attempt.status = "lease_expired"
                expired_attempt.error_code = "OUTBOX_LEASE_EXPIRED"
                expired_attempt.error_message = (
                    "previous owner did not finalize before the database lease expired"
                )
                expired_attempt.completed_at = now
            event.last_error = (
                "LEASE_EXPIRED: previous owner did not finalize before the database lease expired"
            )
        claim_token = uuid4().hex
        event.status = "processing"
        event.claim_token = claim_token
        event.claimed_by = worker_id
        event.claimed_at = now
        event.lease_generation += 1
        event.lease_expires_at = lease_expires_at
        if needs_reconciliation:
            event.reconcile_attempt_count += 1
            event.delivery_state = "reconciling"
        else:
            event.attempt_count += 1
            event.delivery_state = "dispatching"
        claims.append(
            OutboxClaim(
                event_id=event.event_id,
                claim_token=claim_token,
                lease_generation=event.lease_generation,
                claimed_by=worker_id,
                lease_expires_at=lease_expires_at,
                exhausted=needs_reconciliation,
            )
        )
    return claims


def lock_owned_claim(session: Session, claim: OutboxClaim) -> OutboxEvent | None:
    now = database_utc_now(session)
    return session.scalar(
        select(OutboxEvent)
        .where(
            OutboxEvent.event_id == claim.event_id,
            OutboxEvent.status == "processing",
            OutboxEvent.claim_token == claim.claim_token,
            OutboxEvent.lease_generation == claim.lease_generation,
            OutboxEvent.lease_expires_at.is_not(None),
            OutboxEvent.lease_expires_at > now,
        )
        .with_for_update()
    )


def renew_claim(session: Session, claim: OutboxClaim, *, lease_seconds: int) -> bool:
    bounded_lease_seconds = max(5, min(int(lease_seconds), 3600))
    now = database_utc_now(session)
    result = cast(
        CursorResult[Any],
        session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.event_id == claim.event_id,
                OutboxEvent.status == "processing",
                OutboxEvent.claim_token == claim.claim_token,
                OutboxEvent.lease_generation == claim.lease_generation,
                OutboxEvent.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=bounded_lease_seconds))
        ),
    )
    return result.rowcount == 1


def clear_claim(event: OutboxEvent) -> None:
    event.claim_token = None
    event.claimed_by = None
    event.claimed_at = None
    event.lease_expires_at = None
