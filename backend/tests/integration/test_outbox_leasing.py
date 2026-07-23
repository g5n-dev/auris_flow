from __future__ import annotations

from datetime import timedelta

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import OutboxEvent
from app.repositories.outbox_events import claim_events, database_utc_now, lock_owned_claim
from app.services.outbox_service import enqueue_event


def _context(*, trace_id: str = "trace_outbox_lease") -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="sample-operator",
        roles=("project_admin",),
        request_id="outbox-lease-test",
        trace_id=trace_id,
        idempotency_key="outbox-lease-test",
    )


def _enqueue_events(count: int) -> list[int]:
    event_ids: list[int] = []
    with SessionLocal() as session:
        for index in range(count):
            event = enqueue_event(
                session,
                _context(trace_id=f"trace_outbox_lease_{index}"),
                event_type="test.outbox.requested",
                aggregate_type="test_run",
                aggregate_id=f"outbox_lease_{index}",
                payload={"sequence": index},
            )
            session.flush()
            event_ids.append(event.event_id)
        session.commit()
    return event_ids


def test_claim_batches_are_disjoint_between_workers():
    expected_ids = set(_enqueue_events(4))

    with SessionLocal() as session:
        worker_a = claim_events(
            session,
            worker_id="worker-a",
            limit=2,
            lease_seconds=30,
            max_attempts_cap=5,
        )
        session.commit()

    with SessionLocal() as session:
        worker_b = claim_events(
            session,
            worker_id="worker-b",
            limit=2,
            lease_seconds=30,
            max_attempts_cap=5,
        )
        session.commit()

    worker_a_ids = {claim.event_id for claim in worker_a}
    worker_b_ids = {claim.event_id for claim in worker_b}
    assert worker_a_ids.isdisjoint(worker_b_ids)
    assert worker_a_ids | worker_b_ids == expected_ids


def test_expired_lease_reclaim_fences_previous_owner():
    event_id = _enqueue_events(1)[0]

    with SessionLocal() as session:
        first_claim = claim_events(
            session,
            worker_id="worker-old",
            limit=1,
            lease_seconds=30,
            max_attempts_cap=5,
        )[0]
        session.commit()

    with SessionLocal() as session:
        event = session.get(OutboxEvent, event_id)
        assert event is not None
        event.lease_expires_at = database_utc_now(session) - timedelta(seconds=1)
        session.commit()

    with SessionLocal() as session:
        assert lock_owned_claim(session, first_claim) is None

    with SessionLocal() as session:
        second_claim = claim_events(
            session,
            worker_id="worker-new",
            limit=1,
            lease_seconds=30,
            max_attempts_cap=5,
        )[0]
        session.commit()

    assert second_claim.event_id == first_claim.event_id
    assert second_claim.claim_token != first_claim.claim_token
    assert second_claim.lease_generation == first_claim.lease_generation + 1

    with SessionLocal() as session:
        assert lock_owned_claim(session, first_claim) is None
        assert lock_owned_claim(session, second_claim) is not None


def test_enqueue_event_overwrites_untrusted_delivery_scope_fields(monkeypatch):
    trusted_carrier = {"traceparent": "00-11111111111111111111111111111111-2222222222222222-01"}
    monkeypatch.setattr(
        "app.services.outbox_service.current_trace_carrier",
        lambda: trusted_carrier,
    )
    trusted = _context(trace_id="trace_trusted")
    with SessionLocal() as session:
        event = enqueue_event(
            session,
            trusted,
            event_type="test.scope.requested",
            aggregate_type="test_run",
            aggregate_id="scope-test",
            payload={
                "tenant_id": "attacker",
                "project_id": "other-project",
                "trace_id": "trace_attacker",
                "event_type": "attacker.event",
                "idempotency_key": "attacker-key",
                "otel_trace_context": {
                    "traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
                },
            },
        )
        session.flush()

        assert event.tenant_id == trusted.tenant_id
        assert event.project_id == trusted.project_id
        assert event.payload["tenant_id"] == trusted.tenant_id
        assert event.payload["project_id"] == trusted.project_id
        assert event.payload["trace_id"] == trusted.trace_id
        assert event.payload["event_type"] == "test.scope.requested"
        assert event.payload["idempotency_key"] == trusted.idempotency_key
        assert event.payload["dispatch_idempotency_key"] == event.dispatch_idempotency_key
        assert event.payload["otel_trace_context"] == trusted_carrier
