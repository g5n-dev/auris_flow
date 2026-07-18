from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    LabelAggregate,
    LabelAggregateMember,
    LabelAggregationRun,
    LabelFact,
    LabelFactSet,
    LabelFactSetHead,
    LabelFactSetHeadEvent,
    LabelObservation,
    LabelRecomputeRun,
    LabelRecomputeRunItem,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
    RunCompletionReceipt,
    RunRecord,
)
from app.schemas.label_fact_sets import (
    LabelFactSetApproveRequest,
    LabelFactSetPromoteRequest,
    LabelFactSetValidateRequest,
)
from app.schemas.label_recomputations import (
    LabelRecomputeFactCandidate,
    LabelRecomputePartition,
    LabelRecomputeRunCreateRequest,
    LabelRecomputeRunItemCompletionRequest,
    LabelRecomputeRunItemRetryRequest,
)
from app.services.label_fact_set_service import (
    approve_label_fact_set,
    label_fact_set_head_event_content_sha256,
    promote_label_fact_set,
    validate_label_fact_set,
)
from app.services.label_recomputation_service import (
    complete_label_recompute_run_item,
    create_label_recompute_run,
    retry_label_recompute_run_item,
)

TENANT_ID = "tenant_recompute"
PROJECT_ID = "project_recompute"
SOURCE_VERSION_ID = "label_version_source"
TARGET_VERSION_ID = "label_version_target"


def _sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _ctx(
    key: str,
    *,
    roles: tuple[str, ...] = ("project_admin",),
    tenant_id: str = TENANT_ID,
    project_id: str = PROJECT_ID,
) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id="recompute-user",
        roles=roles,
        request_id=f"request-{key}",
        trace_id=f"trace-{key}",
        idempotency_key=key,
        actor_kind="human",
    )


def _seed_anchors(session: Session) -> None:
    taxonomy = LabelTaxonomy(
        taxonomy_id="taxonomy-recompute",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        name="Recompute taxonomy",
        description="fixture",
        status="active",
        resource_version=1,
        content_sha256=_sha("taxonomy"),
        trace_id="trace-taxonomy",
        payload={},
    )
    source_version = LabelVersion(
        label_version_id=SOURCE_VERSION_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="published",
        resource_version=1,
        taxonomy_id=taxonomy.taxonomy_id,
        semantic_version="1.0.0",
        artifact_status="published",
        artifact_published_at=datetime(2026, 1, 1, tzinfo=UTC),
        content_sha256=_sha("source-version"),
        trace_id="trace-source-version",
        payload={},
    )
    target_version = LabelVersion(
        label_version_id=TARGET_VERSION_ID,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="published",
        resource_version=3,
        taxonomy_id=taxonomy.taxonomy_id,
        semantic_version="2.0.0",
        artifact_status="published",
        artifact_published_at=datetime(2026, 7, 18, tzinfo=UTC),
        content_sha256=_sha("target-version"),
        trace_id="trace-target-version",
        payload={},
    )
    target_item = LabelVersionItem(
        label_version_item_id="label-version-item-target",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=TARGET_VERSION_ID,
        label_id="intent",
        canonical_name="Intent",
        aliases=[],
        value_type="categorical",
        risk_level="low",
        mutual_exclusion_group=None,
        parent_ids=[],
        aggregation_rule={},
        status="active",
        definition_sha256=_sha("target-item"),
        trace_id="trace-target-item",
    )
    source_manifest = {
        "schema_version": "auris.label-fact-partitions/1",
        "partitions": [{"partition_id": "source", "row_count": 1}],
    }
    source_fact_set = LabelFactSet(
        fact_set_id="fact-set-source",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        fact_namespace="production",
        target_label_version_id=SOURCE_VERSION_ID,
        status="published",
        fact_as_of=datetime(2026, 7, 1, tzinfo=UTC),
        partition_manifest=source_manifest,
        partition_manifest_sha256=_sha(source_manifest),
        source_manifest_sha256=_sha("source-input"),
        result_manifest_sha256=_sha("source-result"),
        row_count=1,
        manifest_sha256=_sha("source-fact-set-manifest"),
        approval_id="approval-source",
        approved_by="source-admin",
        approved_at=datetime(2026, 7, 1, tzinfo=UTC),
        root_trace_id="trace-source-fact-set",
        action_trace_id="trace-source-fact-set",
        trace_id="trace-source-fact-set",
        payload={},
    )
    source_head = LabelFactSetHead(
        fact_set_head_id="fact-set-head-source",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        environment="production",
        fact_namespace="production",
        current_fact_set_id=source_fact_set.fact_set_id,
        current_manifest_sha256=source_fact_set.manifest_sha256,
        previous_fact_set_id=None,
        previous_manifest_sha256=None,
        generation=4,
        status="active",
        root_trace_id="trace-source-head",
        action_trace_id="trace-source-head",
        trace_id="trace-source-head",
        payload={},
    )
    prior_partition_manifest = {
        "schema_version": "auris.label-recompute-partitions/1",
        "partitions": [
            {
                "partition_id": "prior",
                "row_count": 1,
                "status": "succeeded",
                "content_sha256": _sha("prior-partition"),
                "source_manifest_sha256": _sha("prior-source-partition"),
                "result_manifest_sha256": _sha("prior-result-partition"),
            }
        ],
    }
    prior_partition_sha = _sha(prior_partition_manifest)
    prior_source_sha = _sha("prior-source")
    prior_result_sha = _sha("prior-result")
    prior_frozen_manifest = {
        "fact_as_of": datetime(2026, 7, 10, tzinfo=UTC).isoformat(),
        "fact_namespace": "candidate-recompute-001",
        "partition_manifest": prior_partition_manifest,
        "partition_manifest_sha256": prior_partition_sha,
        "project_id": PROJECT_ID,
        "result_manifest_sha256": prior_result_sha,
        "row_count": 1,
        "schema_version": "auris.label-fact-set-manifest/1",
        "source_manifest_sha256": prior_source_sha,
        "target_label_version": {
            "artifact_status": "published",
            "content_sha256": target_version.content_sha256,
            "label_version_id": TARGET_VERSION_ID,
            "resource_version": target_version.resource_version,
        },
        "tenant_id": TENANT_ID,
    }
    prior_fact_set = LabelFactSet(
        fact_set_id="fact-set-prior-recomputed",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        fact_namespace="candidate-recompute-001",
        target_label_version_id=TARGET_VERSION_ID,
        status="published",
        fact_as_of=datetime(2026, 7, 10, tzinfo=UTC),
        partition_manifest=prior_partition_manifest,
        partition_manifest_sha256=prior_partition_sha,
        source_manifest_sha256=prior_source_sha,
        result_manifest_sha256=prior_result_sha,
        row_count=1,
        manifest_sha256=_sha(prior_frozen_manifest),
        approval_id="approval-prior",
        approved_by="prior-admin",
        approved_at=datetime(2026, 7, 10, tzinfo=UTC),
        root_trace_id="trace-prior-fact-set",
        action_trace_id="trace-prior-fact-set",
        trace_id="trace-prior-fact-set",
        payload={
            "frozen_manifest": prior_frozen_manifest,
            "schema_version": "auris.label-fact-set/1",
            "trace_anchor": {
                "action_trace_id": "trace-prior-fact-set",
                "root_trace_id": "trace-prior-fact-set",
            },
        },
    )
    prior_head = LabelFactSetHead(
        fact_set_head_id="fact-set-head-prior-recomputed",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        environment="production",
        fact_namespace="candidate-recompute-001",
        current_fact_set_id=prior_fact_set.fact_set_id,
        current_manifest_sha256=prior_fact_set.manifest_sha256,
        previous_fact_set_id=None,
        previous_manifest_sha256=None,
        generation=1,
        status="active",
        root_trace_id="trace-prior-head",
        action_trace_id="trace-prior-head",
        trace_id="trace-prior-head",
        payload={},
    )
    prior_event = LabelFactSetHeadEvent(
        head_event_id="pending",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        environment="production",
        fact_namespace="candidate-recompute-001",
        generation=1,
        previous_generation=None,
        action="bootstrap",
        old_fact_set_id=None,
        old_manifest_sha256=None,
        new_fact_set_id=prior_fact_set.fact_set_id,
        new_manifest_sha256=prior_fact_set.manifest_sha256,
        approval_id=prior_fact_set.approval_id,
        effective_at=datetime(2026, 7, 10, tzinfo=UTC),
        content_sha256="0" * 64,
        actor_id="prior-admin",
        root_trace_id="trace-prior-head",
        action_trace_id="trace-prior-head",
        trace_id="trace-prior-head",
        payload={
            "fact_set_head_id": prior_head.fact_set_head_id,
            "previous_event_sha256": None,
            "schema_version": "auris.label-fact-set-head-event/1",
        },
    )
    prior_event.content_sha256 = label_fact_set_head_event_content_sha256(prior_event)
    prior_event.head_event_id = f"lfshe_{prior_event.content_sha256[:24]}"
    prior_head.payload = {"last_event_sha256": prior_event.content_sha256}
    session.add(taxonomy)
    session.flush()
    session.add_all([source_version, target_version])
    session.flush()
    session.add(target_item)
    session.flush()
    session.add(source_fact_set)
    session.flush()
    session.add(prior_fact_set)
    session.flush()
    session.add_all([source_head, prior_head])
    session.flush()
    session.add(prior_event)
    session.flush()


def _create_request(
    partition_ids: tuple[str, ...] = ("p1", "p2"),
) -> LabelRecomputeRunCreateRequest:
    return LabelRecomputeRunCreateRequest(
        target_label_version_id=TARGET_VERSION_ID,
        mapping_bundle_id=None,
        mapping_bundle_sha256=None,
        source_environment="production",
        source_fact_namespace="production",
        source_head_generation=4,
        source_fact_set_id="fact-set-source",
        source_manifest_sha256=_sha("source-fact-set-manifest"),
        fact_namespace="candidate-recompute-001",
        fact_as_of=datetime(2026, 7, 18, 12, tzinfo=UTC),
        partitions=[
            LabelRecomputePartition(
                partition_id=partition_id,
                source_scope={"date": f"2026-07-{index + 1:02d}"},
            )
            for index, partition_id in enumerate(partition_ids)
        ],
        asset_scope={"asset_ids": ["audio-assets"]},
        coverage_policy={"mode": "all-partitions"},
        coverage_min=1.0,
        budget={"unit": "facts"},
        budget_units=100,
    )


def _execution_success(
    session: Session,
    item: LabelRecomputeRunItem,
    *,
    status: str = "success",
) -> str:
    execution = session.get(RunRecord, item.execution_run_id)
    assert execution is not None
    execution.status = status
    receipt_id = f"receipt-{item.partition_id}-{item.attempt_generation}"
    receipt = RunCompletionReceipt(
        tenant_id=item.tenant_id,
        project_id=item.project_id,
        completion_receipt_id=receipt_id,
        run_id=execution.run_id,
        receipt_hash=_sha([receipt_id, status]),
        processing_state="completed",
        processing_token=f"token-{receipt_id}",
        completion_status=status,
        status_code=200,
        adapter="dagster",
        source="dagster",
        external_id=execution.payload["dispatch"]["details"]["dagster_run_id"],
        request_body={},
        response_json={},
        signature_key_id="test-key",
        authenticated_source="dagster",
        signature_nonce=f"nonce-{receipt_id}",
        signature_request_hash=_sha([receipt_id, "request"]),
        signature_body_hash=_sha([receipt_id, "body"]),
        signature_mode="hmac-sha256",
        signed_at=datetime.now(UTC).isoformat(),
        request_id=f"request-{receipt_id}",
        request_trace_id=f"trace-{receipt_id}",
        run_trace_id=execution.trace_id,
        completed_at=datetime.now(UTC),
    )
    session.add(receipt)
    session.flush()
    return receipt_id


def _seed_candidate_lineage(
    session: Session,
    item: LabelRecomputeRunItem,
) -> LabelRecomputeFactCandidate:
    execution = session.get(RunRecord, item.execution_run_id)
    assert execution is not None
    observation_id = f"observation-{item.partition_id}-{item.attempt_generation}"
    aggregate_id = f"aggregate-{item.partition_id}-{item.attempt_generation}"
    aggregation_run_id = f"aggregation-run-{item.partition_id}-{item.attempt_generation}"
    candidate = LabelRecomputeFactCandidate(
        aggregate_id=aggregate_id,
        observation_ids=[observation_id],
        subject_scope="session",
        subject_key=f"session-{item.partition_id}",
        event_or_segment_id=f"segment-{item.partition_id}",
        assertion_slot="canonical",
        occurred_at=datetime(2026, 7, 18, 10, tzinfo=UTC),
        label_id="intent",
        value_type="categorical",
        value="buy",
    )
    if session.get(LabelAggregate, aggregate_id) is not None:
        return candidate
    observation = LabelObservation(
        observation_id=observation_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        extraction_run_id=f"extract-{item.partition_id}",
        subject_scope="session",
        subject_key=f"session-{item.partition_id}",
        evidence_ref={"type": "segment", "id": item.partition_id},
        evidence_sha256=_sha(["evidence", item.partition_id]),
        label_version_id=TARGET_VERSION_ID,
        raw_label="intent",
        label_id="intent",
        value_type="categorical",
        value_json="buy",
        source_family="recompute",
        source_type="model",
        model_version="model-v2",
        prompt_version_id="prompt-v2",
        schema_version="v2",
        calibration_version_id=None,
        raw_confidence=0.99,
        calibrated_confidence=0.99,
        input_sha256=_sha(["input", item.partition_id]),
        output_sha256=_sha(["output", item.partition_id]),
        status="materialized",
        trace_id=execution.trace_id,
        payload={},
    )
    aggregation_run = LabelAggregationRun(
        aggregation_run_id=aggregation_run_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=TARGET_VERSION_ID,
        policy_version_id="policy-v2",
        mode="l1",
        status="success",
        observation_count=1,
        aggregate_count=1,
        input_sha256=_sha(["aggregate-input", item.partition_id]),
        result_sha256=_sha(["aggregate-result", item.partition_id]),
        trace_id=execution.trace_id,
        payload={},
    )
    aggregate = LabelAggregate(
        aggregate_id=aggregate_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        aggregation_run_id=aggregation_run_id,
        label_version_id=TARGET_VERSION_ID,
        policy_version_id="policy-v2",
        calibration_version_ids=[],
        subject_scope="session",
        subject_key=f"session-{item.partition_id}",
        label_id="intent",
        value_type="categorical",
        value_json="buy",
        score=0.99,
        margin=0.9,
        risk_level="low",
        decision="accept",
        status="materialized",
        reason_codes=[],
        explanation={},
        bucket_sha256=_sha(["bucket", item.partition_id]),
        deterministic_hash=_sha(["aggregate", item.partition_id]),
        review_task_id=None,
        trace_id=execution.trace_id,
    )
    member = LabelAggregateMember(
        aggregate_member_id=f"member-{item.partition_id}-{item.attempt_generation}",
        aggregate_id=aggregate_id,
        observation_id=observation_id,
        included=True,
        source_family="recompute",
        evidence_sha256=observation.evidence_sha256,
        calibrated_confidence=0.99,
        contribution_score=1.0,
        exclusion_reason=None,
        explanation={},
        trace_id=execution.trace_id,
    )
    session.add_all([observation, aggregation_run, aggregate, member])
    session.flush()
    return candidate


def _run_and_items(
    session: Session,
) -> tuple[dict[str, object], LabelRecomputeRun, list[LabelRecomputeRunItem]]:
    created = create_label_recompute_run(session, _ctx("create"), _create_request())
    run = session.get(LabelRecomputeRun, created["recompute_run_id"])
    assert run is not None
    items = list(
        session.scalars(
            select(LabelRecomputeRunItem)
            .where(LabelRecomputeRunItem.recompute_run_id == run.recompute_run_id)
            .order_by(LabelRecomputeRunItem.partition_id)
        )
    )
    return created, run, items


def test_full_recompute_requires_all_actual_partitions_and_replays_completion() -> None:
    with SessionLocal() as session:
        _seed_anchors(session)
        created, run, items = _run_and_items(session)
        assert created["status"] == "requested"
        production_head = session.scalar(
            select(LabelFactSetHead).where(
                LabelFactSetHead.tenant_id == TENANT_ID,
                LabelFactSetHead.project_id == PROJECT_ID,
                LabelFactSetHead.environment == "production",
                LabelFactSetHead.fact_namespace == "candidate-recompute-001",
            )
        )
        assert production_head is not None
        assert production_head.current_fact_set_id == "fact-set-prior-recomputed"
        assert production_head.current_fact_set_id != run.candidate_fact_set_id
        fact_set = session.get(LabelFactSet, run.candidate_fact_set_id)
        assert fact_set is not None
        with pytest.raises(ApiError, match="尚未完成"):
            validate_label_fact_set(
                session,
                _ctx("early-validate", roles=("model_engineer",)),
                fact_set.fact_set_id,
                LabelFactSetValidateRequest(expected_manifest_sha256=fact_set.manifest_sha256),
            )
        session.rollback()

        # Reload after rollback; the create transaction intentionally remains open.
        _seed_anchors(session)
        _created, run, items = _run_and_items(session)
        production_head = session.scalar(
            select(LabelFactSetHead).where(
                LabelFactSetHead.fact_set_head_id == "fact-set-head-prior-recomputed"
            )
        )
        assert production_head is not None
        worker = _ctx("complete-p1", roles=("model_engineer",))
        receipt = _execution_success(session, items[0])
        candidate = _seed_candidate_lineage(session, items[0])
        request = LabelRecomputeRunItemCompletionRequest(
            attempt_generation=1,
            completion_receipt_id=receipt,
            status="success",
            facts=[candidate],
        )
        first = complete_label_recompute_run_item(
            session, worker, run.recompute_run_id, items[0].recompute_run_item_id, request
        )
        assert first["run_status"] == "running"
        assert first["row_count"] == 1
        session.commit()
        replay = complete_label_recompute_run_item(
            session, worker, run.recompute_run_id, items[0].recompute_run_item_id, request
        )
        assert replay == first
        fact_set = session.get(LabelFactSet, run.candidate_fact_set_id)
        assert fact_set is not None
        with pytest.raises(ApiError, match="尚未完成"):
            validate_label_fact_set(
                session,
                _ctx("half-validate", roles=("model_engineer",)),
                fact_set.fact_set_id,
                LabelFactSetValidateRequest(expected_manifest_sha256=fact_set.manifest_sha256),
            )

        receipt = _execution_success(session, items[1])
        candidate = _seed_candidate_lineage(session, items[1])
        second = complete_label_recompute_run_item(
            session,
            _ctx("complete-p2", roles=("model_engineer",)),
            run.recompute_run_id,
            items[1].recompute_run_item_id,
            LabelRecomputeRunItemCompletionRequest(
                attempt_generation=1,
                completion_receipt_id=receipt,
                status="success",
                facts=[candidate],
            ),
        )
        assert second["run_status"] == "candidate-complete"
        assert second["candidate_manifest_sha256"]
        fact_set = session.get(LabelFactSet, run.candidate_fact_set_id)
        assert fact_set is not None and fact_set.row_count == 2
        validated = validate_label_fact_set(
            session,
            _ctx("final-validate", roles=("model_engineer",)),
            fact_set.fact_set_id,
            LabelFactSetValidateRequest(expected_manifest_sha256=fact_set.manifest_sha256),
        )
        assert validated["status"] == "validated"
        approved = approve_label_fact_set(
            session,
            _ctx("final-approve"),
            fact_set.fact_set_id,
            LabelFactSetApproveRequest(
                expected_manifest_sha256=fact_set.manifest_sha256,
                approval_id="approval-recompute",
                reason="all actual partitions and lineage hashes verified",
            ),
        )
        assert approved["status"] == "approved"
        before_promotion_generation = production_head.generation
        promoted = promote_label_fact_set(
            session,
            _ctx("final-promote"),
            fact_set.fact_set_id,
            LabelFactSetPromoteRequest(
                environment="production",
                action="promote",
                expected_generation=before_promotion_generation,
                expected_current_fact_set_id="fact-set-prior-recomputed",
                expected_current_manifest_sha256=production_head.current_manifest_sha256,
            ),
        )
        assert promoted["generation"] == before_promotion_generation + 1
        assert promoted["current_fact_set_id"] == fact_set.fact_set_id
        assert production_head.current_fact_set_id == fact_set.fact_set_id
        rollback = promote_label_fact_set(
            session,
            _ctx("final-rollback"),
            "fact-set-prior-recomputed",
            LabelFactSetPromoteRequest(
                environment="production",
                action="rollback",
                expected_generation=promoted["generation"],
                expected_current_fact_set_id=fact_set.fact_set_id,
                expected_current_manifest_sha256=fact_set.manifest_sha256,
            ),
        )
        assert rollback["generation"] == promoted["generation"] + 1
        assert rollback["current_fact_set_id"] == "fact-set-prior-recomputed"
        facts = list(
            session.scalars(select(LabelFact).where(LabelFact.fact_set_id == fact_set.fact_set_id))
        )
        assert len(facts) == 2
        assert all(
            fact.source_kind == "recompute-run-item"
            and fact.aggregate_id is None
            and fact.human_review_decision_id is None
            and fact.recompute_run_item_id is not None
            and fact.status == "recorded"
            and fact.active_slot is None
            for fact in facts
        )
        assert (
            session.scalar(
                select(AuditLog).where(
                    AuditLog.tenant_id == TENANT_ID,
                    AuditLog.project_id == PROJECT_ID,
                    AuditLog.action == "label_fact_set_head.rollback",
                    AuditLog.object_id == production_head.fact_set_head_id,
                )
            )
            is not None
        )
        assert (
            session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == TENANT_ID,
                    OutboxEvent.project_id == PROJECT_ID,
                    OutboxEvent.event_type == "label_fact_set.promoted",
                )
            )
            is not None
        )
        assert all(fact.root_trace_id == run.root_trace_id for fact in facts)


def test_forged_receipt_cross_project_and_manifest_drift_are_blocked() -> None:
    with SessionLocal() as session:
        _seed_anchors(session)
        _created, run, items = _run_and_items(session)
        candidate = _seed_candidate_lineage(session, items[0])
        with pytest.raises(ApiError, match="可信终态"):
            complete_label_recompute_run_item(
                session,
                _ctx("forged", roles=("model_engineer",)),
                run.recompute_run_id,
                items[0].recompute_run_item_id,
                LabelRecomputeRunItemCompletionRequest(
                    attempt_generation=1,
                    completion_receipt_id="receipt-forged",
                    status="success",
                    facts=[candidate],
                ),
            )
        with pytest.raises(ApiError) as cross_project:
            complete_label_recompute_run_item(
                session,
                _ctx(
                    "cross-project",
                    roles=("model_engineer",),
                    project_id="other-project",
                ),
                run.recompute_run_id,
                items[0].recompute_run_item_id,
                LabelRecomputeRunItemCompletionRequest(
                    attempt_generation=1,
                    completion_receipt_id="receipt-forged",
                    status="success",
                    facts=[candidate],
                ),
            )
        assert cross_project.value.status_code == 404

        # Complete both partitions, then prove validation re-queries actual items.
        for index, item in enumerate(items):
            receipt = _execution_success(session, item)
            candidate = _seed_candidate_lineage(session, item)
            complete_label_recompute_run_item(
                session,
                _ctx(f"complete-{index}", roles=("model_engineer",)),
                run.recompute_run_id,
                item.recompute_run_item_id,
                LabelRecomputeRunItemCompletionRequest(
                    attempt_generation=1,
                    completion_receipt_id=receipt,
                    status="success",
                    facts=[candidate],
                ),
            )
        fact_set = session.get(LabelFactSet, run.candidate_fact_set_id)
        assert fact_set is not None
        items[0].row_count = 999
        session.flush()
        with pytest.raises(ApiError, match="实际 append-only"):
            validate_label_fact_set(
                session,
                _ctx("drift-validate", roles=("model_engineer",)),
                fact_set.fact_set_id,
                LabelFactSetValidateRequest(expected_manifest_sha256=fact_set.manifest_sha256),
            )


def test_failed_partition_retry_is_generation_cas_and_does_not_duplicate_facts() -> None:
    with SessionLocal() as session:
        _seed_anchors(session)
        _created, run, items = _run_and_items(session)
        failed_receipt = _execution_success(session, items[0], status="failed")
        failed = complete_label_recompute_run_item(
            session,
            _ctx("failed", roles=("model_engineer",)),
            run.recompute_run_id,
            items[0].recompute_run_item_id,
            LabelRecomputeRunItemCompletionRequest(
                attempt_generation=1,
                completion_receipt_id=failed_receipt,
                status="failed",
                facts=[],
                error_code="TRANSIENT_SOURCE_READ",
                retryable=True,
            ),
        )
        assert failed["status"] == "failed"
        retried = retry_label_recompute_run_item(
            session,
            _ctx("retry", roles=("model_engineer",)),
            run.recompute_run_id,
            items[0].recompute_run_item_id,
            LabelRecomputeRunItemRetryRequest(expected_attempt_generation=1),
        )
        assert retried["attempt_generation"] == 2
        session.commit()
        replay = retry_label_recompute_run_item(
            session,
            _ctx("retry", roles=("model_engineer",)),
            run.recompute_run_id,
            items[0].recompute_run_item_id,
            LabelRecomputeRunItemRetryRequest(expected_attempt_generation=1),
        )
        assert replay == retried
        with pytest.raises(ApiError, match="仅可重试"):
            retry_label_recompute_run_item(
                session,
                replace(_ctx("stale-retry"), idempotency_key="stale-retry"),
                run.recompute_run_id,
                items[0].recompute_run_item_id,
                LabelRecomputeRunItemRetryRequest(expected_attempt_generation=1),
            )
        assert (
            session.scalar(
                select(LabelFact).where(
                    LabelFact.recompute_run_item_id == items[0].recompute_run_item_id
                )
            )
            is None
        )
