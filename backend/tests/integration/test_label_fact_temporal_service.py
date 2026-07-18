from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    LabelAggregate,
    LabelFact,
    LabelFactHead,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
)
from app.schemas.label_facts import LabelFactAsOfRequest, LabelFactRevisionCreate
from app.services import label_fact_temporal_service
from app.services.label_fact_temporal_service import (
    append_label_fact_revision,
    list_label_facts_as_of,
)

TENANT_ID = "tenant_fact_temporal_service"
PROJECT_ID = "project_fact_temporal_service"
TAXONOMY_ID = "taxonomy_fact_temporal_service"
LABEL_VERSION_ID = "lv_fact_temporal_service"
LABEL_ID = "label_purchase_intent"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _ctx(key: str, *, project_id: str = PROJECT_ID) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=project_id,
        user_id="u_fact_writer",
        roles=("project_admin",),
        request_id=f"request-{key}",
        trace_id=f"trace-{key}",
        idempotency_key=key,
        actor_kind="human",
    )


def _aggregate(aggregate_id: str, *, value: bool) -> LabelAggregate:
    return LabelAggregate(
        aggregate_id=aggregate_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        aggregation_run_id=f"aggregation-run-{aggregate_id}",
        label_version_id=LABEL_VERSION_ID,
        policy_version_id="policy-fact-temporal",
        calibration_version_ids=[],
        subject_scope="business-event",
        subject_key="customer-42",
        label_id=LABEL_ID,
        value_type="boolean",
        value_json=value,
        score=0.99,
        margin=0.9,
        risk_level="low",
        decision="auto_accept",
        status="accepted",
        reason_codes=[],
        explanation={"evidence_refs": [f"evidence://{aggregate_id}"]},
        bucket_sha256=("a" if value else "b") * 64,
        deterministic_hash=("c" if value else "d") * 64,
        review_task_id=None,
        trace_id=f"root-{aggregate_id}",
    )


def _seed_scope() -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="事实双时态标签体系",
                description="服务级 revision/as-of 测试",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-taxonomy-fact-temporal",
                payload={},
            )
        )
        session.add(
            LabelVersion(
                label_version_id=LABEL_VERSION_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="published",
                resource_version=1,
                taxonomy_id=TAXONOMY_ID,
                semantic_version="1.0.0",
                artifact_status="published",
                content_sha256="2" * 64,
                trace_id="trace-version-fact-temporal",
                payload={},
            )
        )
        session.flush()
        session.add(
            LabelVersionItem(
                label_version_item_id="lvi-fact-temporal",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                label_version_id=LABEL_VERSION_ID,
                label_id=LABEL_ID,
                canonical_name="购买意向",
                aliases=[],
                value_type="boolean",
                risk_level="low",
                mutual_exclusion_group=None,
                parent_ids=[],
                aggregation_rule={"mode": "presence"},
                status="active",
                definition_sha256=None,
                trace_id="trace-item-fact-temporal",
            )
        )
        session.add_all(
            [
                _aggregate("aggregate-fact-revision-1", value=True),
                _aggregate("aggregate-fact-revision-2", value=False),
                _aggregate("aggregate-fact-drift", value=True),
            ]
        )


def _request(
    aggregate_id: str,
    *,
    value: bool,
    expected_head_generation: int,
) -> LabelFactRevisionCreate:
    return LabelFactRevisionCreate.model_validate(
        {
            "aggregate_id": aggregate_id,
            "source_kind": "aggregate",
            "human_review_decision_id": None,
            "fact_set_id": None,
            "fact_namespace": f"native:{LABEL_VERSION_ID}",
            "subject_scope": "business-event",
            "subject_key": "customer-42",
            "event_or_segment_id": "sales-event-2026-06-30-001",
            "assertion_slot": "presence",
            "occurred_at": datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
            "occurred_at_origin": "source",
            "label_version_id": LABEL_VERSION_ID,
            "label_id": LABEL_ID,
            "value_type": "boolean",
            "value": value,
            "authority": "l2-auto-accepted",
            "expected_head_generation": expected_head_generation,
        }
    )


def test_append_revisions_freezes_logical_key_head_audit_outbox_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_scope()
    first_recorded_at = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    monkeypatch.setattr(label_fact_temporal_service, "_utcnow", lambda: first_recorded_at)
    first_ctx = _ctx("append-fact-revision-1")
    first_request = _request(
        "aggregate-fact-revision-1",
        value=True,
        expected_head_generation=0,
    )

    with SessionLocal.begin() as session:
        first = append_label_fact_revision(session, first_ctx, first_request)
    with SessionLocal.begin() as session:
        replay = append_label_fact_revision(session, first_ctx, first_request)
        assert replay == first

        first_fact = session.get(LabelFact, first["fact_id"])
        head = session.scalar(
            select(LabelFactHead).where(
                LabelFactHead.tenant_id == TENANT_ID,
                LabelFactHead.project_id == PROJECT_ID,
                LabelFactHead.logical_key_sha == first["logical_key_sha256"],
            )
        )
        assert first_fact is not None
        assert head is not None
        assert first_fact.revision == 1
        assert _as_utc(first_fact.recorded_at) == first_recorded_at
        assert _as_utc(first_fact.occurred_at) == datetime(2026, 6, 30, 12, 0, tzinfo=UTC)
        assert first_fact.content_sha256 == first["content_sha256"]
        assert head.current_fact_id == first_fact.fact_id
        assert head.current_revision == 1
        assert head.generation == 1
        assert session.scalar(select(func.count()).select_from(LabelFact)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "label_fact.created")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "label_fact.created")
            )
            == 1
        )


def test_as_of_uses_recorded_at_revision_and_occurred_at_business_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_scope()
    first_time = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    second_time = datetime(2026, 7, 18, 11, 0, tzinfo=UTC)
    monkeypatch.setattr(label_fact_temporal_service, "_utcnow", lambda: first_time)
    with SessionLocal.begin() as session:
        first = append_label_fact_revision(
            session,
            _ctx("append-as-of-first"),
            _request(
                "aggregate-fact-revision-1",
                value=True,
                expected_head_generation=0,
            ),
        )
    with SessionLocal() as session:
        frozen_first = session.get(LabelFact, first["fact_id"])
        assert frozen_first is not None
        first_row_snapshot = {
            "active_slot": frozen_first.active_slot,
            "content_sha256": frozen_first.content_sha256,
            "recorded_at": frozen_first.recorded_at,
            "status": frozen_first.status,
            "updated_at": frozen_first.updated_at,
            "value_json": frozen_first.value_json,
        }

    monkeypatch.setattr(label_fact_temporal_service, "_utcnow", lambda: second_time)
    with SessionLocal.begin() as session:
        second = append_label_fact_revision(
            session,
            _ctx("append-as-of-second"),
            _request(
                "aggregate-fact-revision-2",
                value=False,
                expected_head_generation=1,
            ),
        )

    with SessionLocal() as session:
        old_snapshot = list_label_facts_as_of(
            session,
            _ctx("query-old-as-of"),
            LabelFactAsOfRequest.model_validate(
                {
                    "fact_namespace": f"native:{LABEL_VERSION_ID}",
                    "fact_as_of": datetime(2026, 7, 18, 10, 30, tzinfo=UTC),
                    "occurred_from": datetime(2026, 6, 1, tzinfo=UTC),
                    "occurred_to": datetime(2026, 7, 1, tzinfo=UTC),
                    "label_version_ids": [LABEL_VERSION_ID],
                }
            ),
        )
        new_snapshot = list_label_facts_as_of(
            session,
            _ctx("query-new-as-of"),
            LabelFactAsOfRequest.model_validate(
                {
                    "fact_namespace": f"native:{LABEL_VERSION_ID}",
                    "fact_as_of": datetime(2026, 7, 18, 11, 30, tzinfo=UTC),
                    "occurred_from": datetime(2026, 6, 1, tzinfo=UTC),
                    "occurred_to": datetime(2026, 7, 1, tzinfo=UTC),
                    "label_version_ids": [LABEL_VERSION_ID],
                }
            ),
        )
        before_recording = list_label_facts_as_of(
            session,
            _ctx("query-before-recording"),
            LabelFactAsOfRequest.model_validate(
                {
                    "fact_namespace": f"native:{LABEL_VERSION_ID}",
                    "fact_as_of": datetime(2026, 7, 18, 9, 59, 59, tzinfo=UTC),
                    "occurred_from": datetime(2026, 6, 1, tzinfo=UTC),
                    "occurred_to": datetime(2026, 7, 1, tzinfo=UTC),
                    "label_version_ids": [LABEL_VERSION_ID],
                }
            ),
        )

        assert [item["fact_id"] for item in old_snapshot["facts"]] == [first["fact_id"]]
        assert [item["fact_id"] for item in new_snapshot["facts"]] == [second["fact_id"]]
        assert before_recording["facts"] == []
        assert old_snapshot["fact_as_of"] == "2026-07-18T10:30:00Z"
        assert old_snapshot["source_manifest_sha256"] != new_snapshot["source_manifest_sha256"]

        first_fact = session.get(LabelFact, first["fact_id"])
        assert first_fact is not None
        assert {
            "active_slot": first_fact.active_slot,
            "content_sha256": first_fact.content_sha256,
            "recorded_at": first_fact.recorded_at,
            "status": first_fact.status,
            "updated_at": first_fact.updated_at,
            "value_json": first_fact.value_json,
        } == first_row_snapshot
        assert first_fact.status == "recorded"
        assert first_fact.active_slot is None
        assert first_fact.value_json is True
        assert _as_utc(first_fact.recorded_at) == first_time


def test_stale_head_generation_and_head_drift_fail_without_partial_fact() -> None:
    _seed_scope()
    with SessionLocal.begin() as session:
        first = append_label_fact_revision(
            session,
            _ctx("append-before-stale"),
            _request(
                "aggregate-fact-revision-1",
                value=True,
                expected_head_generation=0,
            ),
        )

    with SessionLocal() as session, pytest.raises(ApiError) as stale:
        append_label_fact_revision(
            session,
            _ctx("append-stale-generation"),
            _request(
                "aggregate-fact-revision-2",
                value=False,
                expected_head_generation=0,
            ),
        )
    assert stale.value.code == "LABEL_FACT_HEAD_GENERATION_CONFLICT"

    with SessionLocal.begin() as session:
        head = session.scalar(
            select(LabelFactHead).where(
                LabelFactHead.logical_key_sha == first["logical_key_sha256"]
            )
        )
        assert head is not None
        head.payload = {**head.payload, "current_content_sha256": "9" * 64}

    with SessionLocal() as session, pytest.raises(ApiError) as drift:
        append_label_fact_revision(
            session,
            _ctx("append-head-drift"),
            _request(
                "aggregate-fact-drift",
                value=True,
                expected_head_generation=1,
            ),
        )
    assert drift.value.code == "LABEL_FACT_HEAD_DRIFT"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(LabelFact)) == 1


def test_source_scope_or_payload_mismatch_is_rejected() -> None:
    _seed_scope()
    forged = _request(
        "aggregate-fact-revision-1",
        value=False,
        expected_head_generation=0,
    )
    with SessionLocal() as session, pytest.raises(ApiError) as mismatch:
        append_label_fact_revision(session, _ctx("append-forged-value"), forged)
    assert mismatch.value.code == "LABEL_FACT_SOURCE_MISMATCH"

    with SessionLocal() as session, pytest.raises(ApiError) as outside_scope:
        append_label_fact_revision(
            session,
            _ctx("append-outside-scope", project_id="other-project"),
            _request(
                "aggregate-fact-revision-1",
                value=True,
                expected_head_generation=0,
            ),
        )
    assert outside_scope.value.code == "LABEL_FACT_SOURCE_NOT_FOUND"
