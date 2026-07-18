from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    HumanReviewDecision,
    LabelAggregate,
    LabelFact,
    LabelFactHead,
    OutboxEvent,
)
from app.services.label_fact_backfill_service import backfill_legacy_label_facts

TENANT_ID = "tenant_label_fact_backfill"
PROJECT_ID = "project_label_fact_backfill"
OTHER_PROJECT_ID = "project_label_fact_backfill_other"
VERSION_ID = "lv_label_fact_backfill"
LABEL_ID = "label_purchase_intent"


def _ctx(key: str, *, trace_id: str | None = None) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        user_id="u_label_fact_backfill",
        roles=("project_admin",),
        request_id=f"request-{key}",
        trace_id=trace_id or f"trace-{key}",
        idempotency_key=key,
        actor_kind="human",
    )


def _aggregate(
    aggregate_id: str,
    *,
    subject_key: str,
    value: bool,
    trace_id: str,
    review_task_id: str | None = None,
    project_id: str = PROJECT_ID,
) -> LabelAggregate:
    return LabelAggregate(
        aggregate_id=aggregate_id,
        tenant_id=TENANT_ID,
        project_id=project_id,
        aggregation_run_id=f"run-{aggregate_id}",
        label_version_id=VERSION_ID,
        policy_version_id="policy-label-fact-backfill",
        calibration_version_ids=[],
        subject_scope="business-event",
        subject_key=subject_key,
        label_id=LABEL_ID,
        value_type="boolean",
        value_json=value,
        score=0.99,
        margin=0.9,
        risk_level="low",
        decision="auto_accept",
        status="accepted",
        reason_codes=[],
        explanation={},
        bucket_sha256=("a" if value else "b") * 64,
        deterministic_hash=("c" if value else "d") * 64,
        review_task_id=review_task_id,
        trace_id=trace_id,
    )


def _legacy_fact(
    fact_id: str,
    aggregate_id: str,
    *,
    subject_key: str,
    value: bool,
    authority: str,
    created_at: datetime,
    supersedes_fact_id: str | None = None,
    active: bool = True,
    review_decision_id: str | None = None,
    trace_id: str,
    project_id: str = PROJECT_ID,
) -> LabelFact:
    return LabelFact(
        fact_id=fact_id,
        tenant_id=TENANT_ID,
        project_id=project_id,
        aggregate_id=aggregate_id,
        supersedes_fact_id=supersedes_fact_id,
        label_version_id=VERSION_ID,
        subject_scope="business-event",
        subject_key=subject_key,
        label_id=LABEL_ID,
        value_type="boolean",
        value_json=value,
        authority=authority,
        status="active" if active else "superseded",
        active_slot="active" if active else None,
        review_decision_id=review_decision_id,
        trace_id=trace_id,
        payload={"legacy_marker": fact_id},
        created_at=created_at,
        updated_at=created_at,
    )


def _seed_valid_legacy_chains() -> None:
    # This suite exercises the pre-0039 Expand backfill window.  The current
    # schema correctly rejects legacy inserts and every Fact mutation, so the
    # fixture explicitly removes only those Contract triggers to reproduce the
    # older deployment state.  The autouse database reset restores them before
    # every test.
    with SessionLocal.begin() as session:
        for trigger_name in (
            "trg_label_facts_contract_insert",
            "trg_label_facts_no_update",
            "trg_label_facts_no_delete",
        ):
            session.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    first_time = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, 8, 0, tzinfo=UTC)
    human_time = datetime(2026, 6, 3, 8, 0, tzinfo=UTC)
    with SessionLocal.begin() as session:
        session.add_all(
            [
                _aggregate(
                    "aggregate-backfill-auto-1",
                    subject_key="event-auto",
                    value=True,
                    trace_id="root-trace-auto-1",
                ),
                _aggregate(
                    "aggregate-backfill-auto-2",
                    subject_key="event-auto",
                    value=False,
                    trace_id="root-trace-auto-2",
                ),
                _aggregate(
                    "aggregate-backfill-human",
                    subject_key="event-human",
                    value=True,
                    trace_id="root-trace-human",
                    review_task_id="review-task-backfill-human",
                ),
                _aggregate(
                    "aggregate-backfill-other-scope",
                    subject_key="event-other-scope",
                    value=True,
                    trace_id="root-trace-other-scope",
                    project_id=OTHER_PROJECT_ID,
                ),
            ]
        )
        session.add(
            HumanReviewDecision(
                decision_id="decision-backfill-human",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                review_task_id="review-task-backfill-human",
                terminal_review_task_id="review-task-backfill-human",
                status="success",
                trace_id="root-trace-human",
                payload={
                    "decision": "accepted",
                    "affected_objects": [
                        {"type": "label_aggregate", "id": "aggregate-backfill-human"}
                    ],
                    "after_json": {
                        "targets": {"label_aggregates:aggregate-backfill-human": {"value": True}}
                    },
                    "note": "customer-secret-note-must-not-leak",
                },
            )
        )
        first_fact = _legacy_fact(
            "lf-backfill-auto-1",
            "aggregate-backfill-auto-1",
            subject_key="event-auto",
            value=True,
            authority="l2-auto-accepted",
            created_at=first_time,
            trace_id="root-trace-auto-1",
        )
        session.add(first_fact)
        session.flush()
        # ``active_slot`` has a legacy insert default; superseding is an UPDATE
        # in the real writer, so reproduce that order here.
        first_fact.status = "superseded"
        first_fact.active_slot = None
        session.flush()
        session.add_all(
            [
                _legacy_fact(
                    "lf-backfill-auto-2",
                    "aggregate-backfill-auto-2",
                    subject_key="event-auto",
                    value=False,
                    authority="l2-auto-accepted",
                    created_at=second_time,
                    supersedes_fact_id="lf-backfill-auto-1",
                    trace_id="root-trace-auto-2",
                ),
                _legacy_fact(
                    "lf-backfill-human",
                    "aggregate-backfill-human",
                    subject_key="event-human",
                    value=True,
                    authority="human-confirmed",
                    created_at=human_time,
                    review_decision_id="decision-backfill-human",
                    trace_id="root-trace-human",
                ),
                _legacy_fact(
                    "lf-backfill-other-scope",
                    "aggregate-backfill-other-scope",
                    subject_key="event-other-scope",
                    value=True,
                    authority="l2-auto-accepted",
                    created_at=human_time,
                    trace_id="root-trace-other-scope",
                    project_id=OTHER_PROJECT_ID,
                ),
            ]
        )


def _assert_no_backfill_side_effects() -> None:
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LabelFactHead)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "label_fact.temporal_backfilled")
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "label_fact.temporal_backfilled")
            )
            == 0
        )


def test_backfill_legacy_facts_builds_temporal_revisions_heads_and_is_reentrant() -> None:
    _seed_valid_legacy_chains()

    with SessionLocal.begin() as session:
        first = backfill_legacy_label_facts(session, _ctx("backfill-first"))

    assert first["status"] == "success"
    assert first["scanned_count"] == 3
    assert first["updated_count"] == 3
    assert first["created_head_count"] == 2
    assert first["fact_namespace"] == "production"
    assert first["conflict_count"] == 0

    with SessionLocal.begin() as session:
        auto_facts = list(
            session.scalars(
                select(LabelFact)
                .where(
                    LabelFact.tenant_id == TENANT_ID,
                    LabelFact.project_id == PROJECT_ID,
                    LabelFact.subject_key == "event-auto",
                )
                .order_by(LabelFact.created_at, LabelFact.fact_id)
            )
        )
        human_fact = session.get(LabelFact, "lf-backfill-human")
        other_scope = session.get(LabelFact, "lf-backfill-other-scope")
        heads = list(
            session.scalars(
                select(LabelFactHead).where(
                    LabelFactHead.tenant_id == TENANT_ID,
                    LabelFactHead.project_id == PROJECT_ID,
                )
            )
        )

        assert [fact.revision for fact in auto_facts] == [1, 2]
        assert {fact.fact_namespace for fact in auto_facts} == {"production"}
        assert {fact.event_or_segment_id for fact in auto_facts} == {"event-auto"}
        assert {fact.assertion_slot for fact in auto_facts} == {"canonical"}
        assert {fact.occurred_at_origin for fact in auto_facts} == {"legacy-recorded-fallback"}
        assert {fact.source_kind for fact in auto_facts} == {"aggregate"}
        assert all(fact.human_review_decision_id is None for fact in auto_facts)
        assert all(fact.occurred_at == fact.created_at for fact in auto_facts)
        assert all(fact.recorded_at == fact.created_at for fact in auto_facts)
        assert all(isinstance(fact.content_sha256, str) for fact in auto_facts)
        assert all(len(fact.content_sha256 or "") == 64 for fact in auto_facts)
        assert auto_facts[0].root_trace_id == "root-trace-auto-1"
        assert auto_facts[1].root_trace_id == "root-trace-auto-2"
        assert {fact.action_trace_id for fact in auto_facts} == {"trace-backfill-first"}

        assert human_fact is not None
        assert human_fact.revision == 1
        assert human_fact.source_kind == "human-decision"
        assert human_fact.human_review_decision_id == "decision-backfill-human"
        assert human_fact.review_decision_id == "decision-backfill-human"

        assert len(heads) == 2
        auto_head = next(head for head in heads if head.current_fact_id == "lf-backfill-auto-2")
        assert auto_head.current_revision == 2
        assert auto_head.generation == 2
        assert auto_head.payload["current_content_sha256"] == auto_facts[1].content_sha256
        assert auto_head.payload["previous_fact_id"] == "lf-backfill-auto-1"

        assert other_scope is not None
        assert other_scope.fact_namespace is None
        assert other_scope.revision is None
        assert other_scope.source_kind is None

        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "label_fact.temporal_backfilled")
        )
        event = session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "label_fact.temporal_backfilled")
        )
        assert audit is not None
        assert event is not None
        serialized = json.dumps(
            {"audit": audit.after_json, "outbox": event.payload}, ensure_ascii=False
        )
        assert "customer-secret-note-must-not-leak" not in serialized
        assert '"value": true' not in serialized.lower()

    # Re-entrancy is independent from request/idempotency/trace identity.
    with SessionLocal.begin() as session:
        replay = backfill_legacy_label_facts(
            session,
            _ctx("backfill-second", trace_id="trace-backfill-second"),
        )
        assert replay["status"] == "success"
        assert replay["scanned_count"] == 3
        assert replay["updated_count"] == 0
        assert replay["created_head_count"] == 0
        assert replay["conflict_count"] == 0

    with SessionLocal() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "label_fact.temporal_backfilled")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "label_fact.temporal_backfilled")
            )
            == 1
        )


@pytest.mark.parametrize(
    ("mutation", "expected_code", "expected_reason"),
    [
        ("bad_chain", "LABEL_FACT_BACKFILL_CHAIN_CONFLICT", "NON_CONTIGUOUS_CHAIN"),
        ("partial_temporal", "LABEL_FACT_BACKFILL_DRIFT", "PARTIAL_TEMPORAL_STATE"),
        (
            "unknown_authority",
            "LABEL_FACT_BACKFILL_AUTHORITY_UNKNOWN",
            "UNKNOWN_AUTHORITY",
        ),
    ],
)
def test_backfill_conflicts_are_explicit_and_leave_no_partial_writes(
    mutation: str,
    expected_code: str,
    expected_reason: str,
) -> None:
    _seed_valid_legacy_chains()
    with SessionLocal.begin() as session:
        target = session.get(LabelFact, "lf-backfill-auto-2")
        assert target is not None
        if mutation == "bad_chain":
            target.supersedes_fact_id = None
        elif mutation == "partial_temporal":
            target.fact_namespace = "production"
        else:
            target.authority = "legacy-free-text-do-not-expose"

    with pytest.raises(ApiError) as raised:
        with SessionLocal.begin() as session:
            backfill_legacy_label_facts(session, _ctx(f"backfill-{mutation}"))

    assert raised.value.code == expected_code
    assert raised.value.status_code == 409
    assert raised.value.details
    assert raised.value.details[0]["reason_code"] == expected_reason
    assert "legacy-free-text-do-not-expose" not in json.dumps(raised.value.details)
    _assert_no_backfill_side_effects()
    with SessionLocal() as session:
        facts = list(
            session.scalars(
                select(LabelFact).where(
                    LabelFact.tenant_id == TENANT_ID,
                    LabelFact.project_id == PROJECT_ID,
                )
            )
        )
        assert all(fact.logical_key_sha is None for fact in facts)
        assert all(fact.revision is None for fact in facts)
        assert all(fact.content_sha256 is None for fact in facts)


def test_human_authority_requires_a_valid_scope_bound_terminal_decision() -> None:
    _seed_valid_legacy_chains()
    with SessionLocal.begin() as session:
        decision = session.get(HumanReviewDecision, "decision-backfill-human")
        assert decision is not None
        decision.status = "draft"

    with pytest.raises(ApiError) as raised:
        with SessionLocal.begin() as session:
            backfill_legacy_label_facts(session, _ctx("backfill-invalid-human"))

    assert raised.value.code == "LABEL_FACT_BACKFILL_HUMAN_SOURCE_CONFLICT"
    assert raised.value.status_code == 409
    assert raised.value.details == [
        {
            "fact_id": "lf-backfill-human",
            "reason_code": "HUMAN_DECISION_NOT_AUTHORITATIVE",
        }
    ]
    _assert_no_backfill_side_effects()
