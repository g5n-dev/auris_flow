from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import OutboxEvent
from app.services.adapters import DispatchResult
from app.services.outbox_service import OutboxEventConflictError, enqueue_event
from app.workers import outbox_worker


def _context(suffix: str) -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="frank",
        roles=("project_admin",),
        request_id=f"outbox-business-{suffix}",
        trace_id=f"trace-outbox-business-{suffix}",
        idempotency_key=f"outbox-business-{suffix}",
    )


def _enqueue(*, suffix: str, resource_version: int, value: str = "same") -> tuple[int, str]:
    with SessionLocal() as session:
        event = enqueue_event(
            session,
            _context(suffix),
            event_type="test.resource.published",
            aggregate_type="test_resource",
            aggregate_id="resource-42",
            payload={"resource_version": resource_version, "value": value},
        )
        session.commit()
        return event.event_id, str(event.payload["event_id"])


def test_equivalent_event_returns_existing_row_across_requests() -> None:
    first_database_id, first_business_event_id = _enqueue(suffix="first", resource_version=7)
    second_database_id, second_business_event_id = _enqueue(suffix="second", resource_version=7)

    assert second_database_id == first_database_id
    assert second_business_event_id == first_business_event_id
    with SessionLocal() as session:
        events = session.query(OutboxEvent).all()
        assert len(events) == 1
        assert events[0].payload["trace_id"] == "trace-outbox-business-first"
        assert events[0].payload["resource_version"] == 7
        assert events[0].dispatch_idempotency_key.startswith("outbox_v1_")


def test_different_resource_versions_create_distinct_events() -> None:
    first_database_id, first_business_event_id = _enqueue(suffix="v7", resource_version=7)
    second_database_id, second_business_event_id = _enqueue(suffix="v8", resource_version=8)

    assert second_database_id != first_database_id
    assert second_business_event_id != first_business_event_id
    with SessionLocal() as session:
        assert session.query(OutboxEvent).count() == 2


def test_same_resource_version_with_different_business_payload_is_rejected() -> None:
    _enqueue(suffix="original", resource_version=7, value="original")

    with SessionLocal() as session:
        with pytest.raises(OutboxEventConflictError, match="resource version"):
            enqueue_event(
                session,
                _context("conflict"),
                event_type="test.resource.published",
                aggregate_type="test_resource",
                aggregate_id="resource-42",
                payload={"resource_version": 7, "value": "changed"},
            )

    with SessionLocal() as session:
        assert session.query(OutboxEvent).count() == 1


def test_concurrent_equivalent_enqueues_converge_on_one_event() -> None:
    barrier = Barrier(2)

    def enqueue_concurrently(suffix: str) -> tuple[int, str]:
        with SessionLocal() as session:
            barrier.wait(timeout=5)
            event = enqueue_event(
                session,
                _context(suffix),
                event_type="test.resource.published",
                aggregate_type="test_resource",
                aggregate_id="resource-42",
                payload={"resource_version": 7, "value": "same"},
            )
            session.commit()
            return event.event_id, str(event.payload["event_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(enqueue_concurrently, ("worker-a", "worker-b")))

    assert len({database_id for database_id, _ in results}) == 1
    assert len({business_id for _, business_id in results}) == 1
    with SessionLocal() as session:
        assert session.query(OutboxEvent).count() == 1


def test_duplicate_enqueue_does_not_revive_dead_letter_event() -> None:
    database_id, _ = _enqueue(suffix="dead-letter-original", resource_version=7)
    with SessionLocal() as session:
        event = session.get(OutboxEvent, database_id)
        assert event is not None
        event.status = "dead_letter"
        event.delivery_state = "failed"
        event.attempt_count = 3
        event.last_error = "TEST_FAILURE: exhausted"
        session.commit()

    duplicate_id, _ = _enqueue(suffix="dead-letter-duplicate", resource_version=7)

    assert duplicate_id == database_id
    with SessionLocal() as session:
        event = session.get(OutboxEvent, database_id)
        assert event is not None
        assert event.status == "dead_letter"
        assert event.delivery_state == "failed"
        assert event.attempt_count == 3
        assert event.last_error == "TEST_FAILURE: exhausted"
        assert session.query(OutboxEvent).count() == 1


def test_duplicate_enqueue_produces_one_external_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    first_database_id, first_business_event_id = _enqueue(
        suffix="dispatch-first",
        resource_version=7,
    )
    duplicate_database_id, _ = _enqueue(
        suffix="dispatch-duplicate",
        resource_version=7,
    )
    calls: list[dict[str, object]] = []

    def dispatch_once(
        event_type: str,
        aggregate_type: str,
        payload: dict[str, object],
    ) -> DispatchResult:
        calls.append(payload)
        return DispatchResult(
            adapter="test",
            operation=f"{aggregate_type}:{event_type}",
            details={"remote_id": "remote-once"},
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", dispatch_once)

    assert duplicate_database_id == first_database_id
    assert outbox_worker.process_once() == 1
    assert outbox_worker.process_once() == 0
    assert len(calls) == 1
    assert calls[0]["event_id"] == first_business_event_id
    assert calls[0]["resource_version"] == 7

    with SessionLocal() as session:
        event = session.get(OutboxEvent, first_database_id)
        assert event is not None
        assert event.status == "processed"
        assert event.attempt_count == 1
