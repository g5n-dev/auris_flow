from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Never

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import AuditLog, OutboxDeliveryAttempt, OutboxEvent
from app.qdrant_rebuild import (
    POINT_ID_FIELDS,
    REBUILD_AGGREGATE_TYPE,
    QdrantRebuildError,
    _canonical,
    _point_id,
    build_plan,
    enqueue_plan,
    verify_plan,
)
from app.services.adapters import (
    AdapterRegistry,
    LocalQdrantIndexClient,
    _stable_uuid,
    dispatch_event,
    reconcile_event,
)
from app.services.outbox_service import enqueue_event


def _ctx(*, trace_id: str = "trace_original_qdrant") -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="usr_admin",
        roles=("system",),
        request_id="request_qdrant_source",
        trace_id=trace_id,
        idempotency_key="source-qdrant-event",
        actor_kind="service",
    )


def _source_payload(trace_id: str = "trace_original_qdrant") -> dict[str, object]:
    return {
        "status": "pending",
        "version": "kb-index-v3.2",
        "embedding_text": "authoritative knowledge input",
        "qdrant_payload": {
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": trace_id,
            "collection": "knowledge_chunks",
            "knowledge_index_id": "ki_sales_policy_v1",
            "knowledge_source_id": "ks_sales_policy",
            "source_id": "ks_sales_policy",
            "source_type": "sop",
            "asset_key": "auris/knowledge/ks_sales_policy",
            "version": "kb-index-v3.2",
            "embedding_text": "authoritative knowledge input",
            "business_ref": {
                "connector_id": "conn_platform_auth",
                "source_name": "private source name",
            },
        },
    }


def _confirm_source_event() -> int:
    with SessionLocal.begin() as session:
        event = enqueue_event(
            session,
            _ctx(),
            event_type="knowledge_index.build_requested",
            aggregate_type="knowledge_build",
            aggregate_id="knowledge_build_source",
            payload=_source_payload(),
        )
        point_id = _point_id(event.payload["qdrant_payload"])
        event.status = "processed"
        event.delivery_state = "confirmed"
        event.attempt_count = 1
        event.lease_generation = 1
        event.processed_at = datetime.now(UTC)
        session.add(
            OutboxDeliveryAttempt(
                attempt_id=f"source_attempt_{event.event_id}",
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                project_id=event.project_id,
                attempt_number=1,
                lease_generation=1,
                claimed_by="test-worker",
                claim_token_sha256="a" * 64,
                delivery_mode="dispatch",
                status="succeeded",
                dispatch_idempotency_key=event.dispatch_idempotency_key,
                request_sha256="b" * 64,
                adapter="qdrant",
                operation="upsert_payload",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                details={"dispatch_details": {"point_ids": [point_id]}},
            )
        )
        return event.event_id


def test_plan_is_scope_bound_and_contains_no_business_payload() -> None:
    source_event_id = _confirm_source_event()
    with SessionLocal() as session:
        plan, candidates = build_plan(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )

    assert plan["cutoff_event_id"] == source_event_id
    assert plan["scope"] == {"tenant_id": "aurora_auto", "project_id": "sales_qa"}
    assert plan["collections"] == {"knowledge_chunks": 1}
    assert plan["point_count"] == 1
    assert len(candidates) == 1
    assert b"private source name" not in _canonical(plan)
    assert set(plan["items"][0]) == {
        "collection",
        "embedding_inputs_sha256",
        "expected_point_id",
        "original_trace_id",
        "source_business_payload_sha256",
        "source_event_id",
        "source_event_type",
        "source_qdrant_payload_sha256",
    }


def test_point_id_algorithm_matches_the_real_qdrant_adapter() -> None:
    payload = _source_payload()["qdrant_payload"]
    assert isinstance(payload, dict)
    assert _point_id(payload) == _stable_uuid(payload, *POINT_ID_FIELDS)


def test_voiceprint_dispatch_never_calls_text_embedding_qdrant_adapter() -> None:
    class ForbiddenQdrant:
        def upsert_index_payload(self, _payload: dict[str, Any]) -> Never:
            raise AssertionError("voiceprint payload reached the text embedding adapter")

        def search_index_payload(
            self,
            _qdrant_payload: dict[str, Any],
            *,
            query: str,
            top_k: int,
        ) -> Never:
            raise AssertionError((query, top_k))

        def reconcile_index_payload(self, _payload: dict[str, Any]) -> Never:
            raise AssertionError("voiceprint payload reached Qdrant reconciliation")

    registry = AdapterRegistry(qdrant=ForbiddenQdrant())
    voiceprint_payload = {
        "status": "enrolled",
        "qdrant_payload": {"collection": "voiceprint_embeddings"},
    }
    results = [
        delivery(event_type, aggregate_type, voiceprint_payload, registry)
        for delivery in (dispatch_event, reconcile_event)
        for event_type, aggregate_type in (
            ("voiceprint_enrollments.upserted", "voiceprint_enrollments"),
            ("knowledge_index.build_requested", "knowledge_build"),
        )
    ]
    results.append(LocalQdrantIndexClient().upsert_index_payload(voiceprint_payload))
    for result in results:
        assert result.status == "failed"
        assert result.error_code == "VOICEPRINT_VECTOR_PROVIDER_UNSUPPORTED"
        assert result.retryable is False
        assert result.details["indexed"] is False
        assert result.details["text_embedding_forbidden"] is True


def test_enqueue_requires_exact_plan_confirmation_and_preserves_point_trace() -> None:
    _confirm_source_event()
    with SessionLocal() as session:
        plan, _ = build_plan(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )

    with pytest.raises(QdrantRebuildError, match="confirmation"):
        with SessionLocal.begin() as session:
            enqueue_plan(session, plan, confirmation_sha256="0" * 64)

    plan_sha256 = hashlib.sha256(_canonical(plan)).hexdigest()
    with SessionLocal.begin() as session:
        result = enqueue_plan(
            session,
            plan,
            confirmation_sha256=plan_sha256,
        )
        rebuild = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_type == REBUILD_AGGREGATE_TYPE)
        )
        assert rebuild is not None
        assert rebuild.tenant_id == "aurora_auto"
        assert rebuild.project_id == "sales_qa"
        assert rebuild.payload["trace_id"] == "trace_original_qdrant"
        assert rebuild.payload["rebuild_trace_id"] == result["rebuild_trace_id"]
        assert rebuild.payload["qdrant_payload"]["trace_id"] == "trace_original_qdrant"
        assert re.fullmatch(r"qdrant_rebuild_[a-p]{16}_[a-p]+", rebuild.aggregate_id)
        assert re.fullmatch(r"trace_qdrant_rebuild_[a-p]{32}", result["rebuild_trace_id"])
        audit = session.scalar(select(AuditLog).where(AuditLog.action == "qdrant.rebuild.enqueued"))
        assert audit is not None
        assert audit.trace_id == result["rebuild_trace_id"]
        assert re.fullmatch(r"qdrant_rebuild_plan_[a-p]{32}", audit.object_id)


def test_verify_requires_confirmed_qdrant_receipt() -> None:
    _confirm_source_event()
    with SessionLocal.begin() as session:
        plan, _ = build_plan(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )
        plan_sha256 = hashlib.sha256(_canonical(plan)).hexdigest()
        enqueue_plan(session, plan, confirmation_sha256=plan_sha256)

    with SessionLocal() as session, pytest.raises(QdrantRebuildError, match="not confirmed"):
        verify_plan(session, plan)

    with SessionLocal.begin() as session:
        rebuild = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_type == REBUILD_AGGREGATE_TYPE)
        )
        assert rebuild is not None
        rebuild.status = "processed"
        rebuild.delivery_state = "confirmed"
        rebuild.attempt_count = 1
        rebuild.lease_generation = 1
        rebuild.processed_at = datetime.now(UTC)
        session.add(
            OutboxDeliveryAttempt(
                attempt_id=f"rebuild_attempt_{rebuild.event_id}",
                event_id=rebuild.event_id,
                tenant_id=rebuild.tenant_id,
                project_id=rebuild.project_id,
                attempt_number=1,
                lease_generation=1,
                claimed_by="test-worker",
                claim_token_sha256="c" * 64,
                delivery_mode="dispatch",
                status="succeeded",
                dispatch_idempotency_key=rebuild.dispatch_idempotency_key,
                request_sha256="d" * 64,
                adapter="qdrant",
                operation="upsert_payload",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                details={"dispatch_details": {"point_ids": [rebuild.payload["expected_point_id"]]}},
            )
        )

    with SessionLocal() as session:
        evidence = verify_plan(session, plan)
    assert evidence["status"] == "verified"
    assert evidence["plan_sha256"] == plan_sha256
    assert evidence["collections"] == {"knowledge_chunks": 1}
    assert evidence["next_gate"] == "production/scripts/finalize-restore.sh"


def test_plan_rejects_tampered_business_payload() -> None:
    _confirm_source_event()
    with SessionLocal.begin() as session:
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.aggregate_type == "knowledge_build")
        )
        assert event is not None
        event.payload = {
            **event.payload,
            "data": {
                **event.payload["data"],
                "qdrant_payload": {
                    **event.payload["data"]["qdrant_payload"],
                    "tenant_id": "other_tenant",
                },
            },
        }

    with (
        SessionLocal() as session,
        pytest.raises(QdrantRebuildError, match="digest does not match"),
    ):
        build_plan(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


@pytest.mark.parametrize(
    ("event_type", "collection"),
    [
        ("voiceprint_enrollments.upserted", "voiceprint_embeddings"),
        ("future_vector.indexed", "future_vectors"),
    ],
)
def test_plan_fails_closed_for_unregistered_qdrant_event_type(
    event_type: str,
    collection: str,
) -> None:
    with SessionLocal.begin() as session:
        payload = _source_payload()
        source_qdrant_payload = payload["qdrant_payload"]
        assert isinstance(source_qdrant_payload, dict)
        qdrant_payload = {
            **source_qdrant_payload,
            "collection": collection,
        }
        payload = {**payload, "qdrant_payload": qdrant_payload}
        event = enqueue_event(
            session,
            _ctx(trace_id="trace_future_vector"),
            event_type=event_type,
            aggregate_type="future_vector",
            aggregate_id="future_vector_source",
            payload=payload,
        )
        point_id = _point_id(
            {
                **qdrant_payload,
                "tenant_id": event.tenant_id,
                "project_id": event.project_id,
                "trace_id": event.payload["trace_id"],
            }
        )
        event.status = "processed"
        event.delivery_state = "confirmed"
        event.attempt_count = 1
        event.lease_generation = 1
        event.processed_at = datetime.now(UTC)
        session.add(
            OutboxDeliveryAttempt(
                attempt_id=f"unsupported_attempt_{event.event_id}",
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                project_id=event.project_id,
                attempt_number=1,
                lease_generation=1,
                claimed_by="test-worker",
                claim_token_sha256="e" * 64,
                delivery_mode="dispatch",
                status="succeeded",
                dispatch_idempotency_key=event.dispatch_idempotency_key,
                request_sha256="f" * 64,
                adapter="qdrant",
                operation="upsert_payload",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                details={"dispatch_details": {"point_ids": [point_id]}},
            )
        )

    with (
        SessionLocal() as session,
        pytest.raises(QdrantRebuildError, match="outside rebuild policy"),
    ):
        build_plan(
            session,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )
