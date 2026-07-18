from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    HumanReviewDecision,
    InsightReport,
    InsightReportMetricBinding,
    JsonResource,
    LabelAggregate,
    LabelFact,
    LabelFactSet,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelMappingBundleSource,
    LabelRecomputeRun,
    LabelRecomputeRunItem,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    MetricResult,
    MetricResultLabelScope,
    OutboxEvent,
    RunRecord,
)
from app.schemas.label_fact_sets import (
    LabelFactSetApproveRequest,
    LabelFactSetCreateRequest,
    LabelFactSetPromoteRequest,
    LabelFactSetValidateRequest,
)
from app.schemas.label_facts import LabelFactRevisionCreate
from app.schemas.label_lifecycle import (
    LabelVersionDeprecationPreflightRequest,
    LabelVersionTransitionRequest,
)
from app.schemas.label_metric_scopes import LabelMetricResultMaterializeRequest
from app.schemas.label_recomputations import (
    LabelRecomputePartition,
    LabelRecomputeRunCreateRequest,
    LabelRecomputeRunItemCompletionRequest,
)
from app.services.insight_closure_service import current_metric_payloads, report_detail_payload
from app.services.insight_report_metric_binding_service import (
    bind_insight_report_metrics,
    verify_insight_report_metric_binding,
)
from app.services.label_fact_set_service import (
    approve_label_fact_set,
    create_label_fact_set,
    promote_label_fact_set,
    strict_canonical_sha256,
    validate_label_fact_set,
    verify_label_fact_set_head_chain,
)
from app.services.label_fact_temporal_service import append_label_fact_revision
from app.services.label_lifecycle_service import (
    create_label_version_deprecation_preflight,
    transition_label_version,
)
from app.services.label_metric_scope_service import materialize_label_metric_result
from app.services.label_recomputation_service import (
    complete_label_recompute_run_item,
    create_label_recompute_run,
)
from tests.integration.test_label_recomputation_e2e import (
    _execution_success,
    _seed_candidate_lineage,
)

TENANT_ID = "tenant_recompute"
PROJECT_ID = "project_recompute"
TAXONOMY_ID = "taxonomy-governance-closed-loop"
SOURCE_VERSION_ID = "label_version_source"
TARGET_VERSION_ID = "label_version_target"
LABEL_ID = "intent"
MAPPING_BUNDLE_ID = "mapping-governance-source-target"
SOURCE_NAMESPACE = "native-governance-production"
RECOMPUTED_NAMESPACE = "recomputed-governance-production"
TRACE_ID = "trace-label-statistics-governance-closed-loop"
FACT_AS_OF = datetime(2026, 7, 18, 12, tzinfo=UTC)


def _sha(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _ctx(
    key: str,
    *,
    roles: tuple[str, ...] = ("project_admin",),
    project_id: str = PROJECT_ID,
) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=project_id,
        user_id="governance-admin",
        roles=roles,
        request_id=f"request-{key}",
        trace_id=TRACE_ID,
        idempotency_key=f"idem-{key}",
        actor_kind="human",
    )


def _version(version_id: str, *, semantic_version: str, resource_version: int) -> LabelVersion:
    return LabelVersion(
        label_version_id=version_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="published",
        resource_version=resource_version,
        taxonomy_id=TAXONOMY_ID,
        semantic_version=semantic_version,
        artifact_status="published",
        artifact_published_at=datetime(2026, 7, 1, tzinfo=UTC),
        content_sha256=_sha([version_id, semantic_version]),
        trace_id=TRACE_ID,
        payload={
            "label_version_id": version_id,
            "resource_version": resource_version,
            "semantic_version": semantic_version,
        },
    )


def _version_item(version_id: str, *, suffix: str) -> LabelVersionItem:
    return LabelVersionItem(
        label_version_item_id=f"label-version-item-{suffix}",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=version_id,
        label_id=LABEL_ID,
        canonical_name="Intent",
        aliases=[],
        value_type="categorical",
        risk_level="low",
        mutual_exclusion_group=None,
        parent_ids=[],
        aggregation_rule={"mode": "categorical"},
        status="active",
        definition_sha256=_sha([version_id, LABEL_ID]),
        trace_id=TRACE_ID,
    )


def _aggregate(
    aggregate_id: str,
    *,
    subject_key: str,
    value: str,
    review_task_id: str | None,
) -> LabelAggregate:
    return LabelAggregate(
        aggregate_id=aggregate_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        aggregation_run_id=f"aggregation-run-{aggregate_id}",
        label_version_id=SOURCE_VERSION_ID,
        policy_version_id="policy-governance-v1",
        calibration_version_ids=[],
        subject_scope="session",
        subject_key=subject_key,
        label_id=LABEL_ID,
        value_type="categorical",
        value_json=value,
        score=0.98,
        margin=0.8,
        risk_level="low",
        decision="accept",
        status="materialized",
        reason_codes=[],
        explanation={"evidence": aggregate_id},
        bucket_sha256=_sha([aggregate_id, "bucket"]),
        deterministic_hash=_sha([aggregate_id, value]),
        review_task_id=review_task_id,
        trace_id=TRACE_ID,
    )


def _seed_governance_anchors() -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="Governed label statistics",
                description="joint lifecycle and statistics integration",
                status="active",
                resource_version=1,
                content_sha256=_sha(TAXONOMY_ID),
                trace_id=TRACE_ID,
                payload={},
            )
        )
        source = _version(SOURCE_VERSION_ID, semantic_version="1.0.0", resource_version=1)
        target = _version(TARGET_VERSION_ID, semantic_version="2.0.0", resource_version=3)
        session.add_all([source, target])
        session.flush()
        session.add_all(
            [
                JsonResource(
                    collection="label_versions",
                    resource_key=version.label_version_id,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="published",
                    trace_id=TRACE_ID,
                    data={
                        "artifact_status": "published",
                        "content_sha256": version.content_sha256,
                        "id": version.label_version_id,
                        "label_version_id": version.label_version_id,
                        "resource_version": version.resource_version,
                        "semantic_version": version.semantic_version,
                        "status": "published",
                        "taxonomy_id": TAXONOMY_ID,
                    },
                )
                for version in (source, target)
            ]
        )
        session.add_all(
            [
                _version_item(SOURCE_VERSION_ID, suffix="source"),
                _version_item(TARGET_VERSION_ID, suffix="target"),
            ]
        )
        session.add(
            LabelMappingBundle(
                mapping_bundle_id=MAPPING_BUNDLE_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                target_label_version_id=TARGET_VERSION_ID,
                source_label_version_ids=[SOURCE_VERSION_ID],
                source_manifest_sha256=_sha([SOURCE_VERSION_ID, "source-manifest"]),
                compiler_version="label-mapping-bundle-compiler/1",
                status="published",
                resource_version=1,
                canonical_manifest_sha256=_sha([MAPPING_BUNDLE_ID, "canonical"]),
                approval_id="approval-governance-mapping",
                approved_by="governance-admin",
                approved_at=datetime(2026, 7, 2, tzinfo=UTC),
                published_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
                root_trace_id=TRACE_ID,
                trace_id=TRACE_ID,
                payload={},
            )
        )
        session.flush()
        session.add_all(
            [
                LabelMappingBundleSource(
                    bundle_source_id="mapping-source-governance-v1",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    source_label_version_id=SOURCE_VERSION_ID,
                    source_resource_version=1,
                    source_order=0,
                    content_sha256=_sha([MAPPING_BUNDLE_ID, SOURCE_VERSION_ID]),
                    trace_id=TRACE_ID,
                    payload={},
                ),
                LabelMappingBundlePath(
                    bundle_path_id="mapping-path-governance-intent",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    source_label_version_id=SOURCE_VERSION_ID,
                    target_label_version_id=TARGET_VERSION_ID,
                    source_label_id=LABEL_ID,
                    target_label_id=LABEL_ID,
                    metric_family="rate",
                    relation_path=[{"relation": "rename"}],
                    mapping_version_ids=["mapping-governance-v1"],
                    metric_grain="business-event",
                    lineage_key="event_or_segment_id",
                    reducer=None,
                    comparability_status="comparable",
                    requires_recompute=False,
                    path_sha256=_sha([MAPPING_BUNDLE_ID, LABEL_ID, "path"]),
                    trace_id=TRACE_ID,
                    payload={},
                ),
            ]
        )
        auto = _aggregate(
            "aggregate-governance-auto",
            subject_key="session-governance-auto",
            value="buy",
            review_task_id=None,
        )
        human = _aggregate(
            "aggregate-governance-human",
            subject_key="session-governance-human",
            value="uncertain",
            review_task_id="review-task-governance-human",
        )
        session.add_all([auto, human])
        session.add(
            HumanReviewDecision(
                decision_id="decision-governance-human",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                review_task_id="review-task-governance-human",
                terminal_review_task_id="review-task-governance-human",
                status="success",
                trace_id=TRACE_ID,
                payload={
                    "decision": "modified",
                    "affected_objects": [{"id": human.aggregate_id, "type": "label_aggregate"}],
                    "after_json": {
                        "targets": {f"label_aggregates:{human.aggregate_id}": {"value": "buy"}}
                    },
                },
            )
        )
        session.add_all(
            [
                RunRecord(
                    run_id=f"run-metric-{mode}",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    run_type="insight_metric_aggregation",
                    status="submitted",
                    run_key=f"metric-{mode}",
                    partition_key=f"{TENANT_ID}/{PROJECT_ID}",
                    trace_id=TRACE_ID,
                    payload={"metric_keys": ["intent_rate"]},
                )
                for mode in ("native", "normalized", "recomputed")
            ]
        )
        session.add(
            RunRecord(
                run_id="run-report-governance",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="insight_report",
                status="success",
                run_key="report-governance",
                partition_key=f"{TENANT_ID}/{PROJECT_ID}",
                trace_id=TRACE_ID,
                payload={},
            )
        )


def _publish_initial_fact_set(
    *,
    key: str,
    fact_namespace: str,
    target_label_version_id: str,
    row_count: int,
) -> dict[str, object]:
    partition_manifest = {
        "partitions": [{"partition_id": key, "row_count": row_count}],
        "schema_version": "auris.label-fact-partitions/1",
    }
    request = LabelFactSetCreateRequest(
        fact_namespace=fact_namespace,
        target_label_version_id=target_label_version_id,
        fact_as_of=FACT_AS_OF,
        partition_manifest=partition_manifest,
        partition_manifest_sha256=strict_canonical_sha256(partition_manifest),
        source_manifest_sha256=_sha([key, "source"]),
        result_manifest_sha256=_sha([key, "result"]),
        row_count=row_count,
    )
    with SessionLocal.begin() as session:
        created = create_label_fact_set(session, _ctx(f"{key}-create"), request)
        fact_set_id = str(created["fact_set_id"])
        manifest_sha256 = str(created["manifest_sha256"])
        validate_label_fact_set(
            session,
            _ctx(f"{key}-validate", roles=("model_engineer",)),
            fact_set_id,
            LabelFactSetValidateRequest(expected_manifest_sha256=manifest_sha256),
        )
        approve_label_fact_set(
            session,
            _ctx(f"{key}-approve"),
            fact_set_id,
            LabelFactSetApproveRequest(
                expected_manifest_sha256=manifest_sha256,
                approval_id=f"approval-{key}",
                reason="joint closed-loop fixture",
            ),
        )
        published = promote_label_fact_set(
            session,
            _ctx(f"{key}-bootstrap"),
            fact_set_id,
            LabelFactSetPromoteRequest(
                environment="production",
                action="bootstrap",
                expected_generation=0,
                expected_current_fact_set_id=None,
                expected_current_manifest_sha256=None,
            ),
        )
    return {**created, "generation": published["generation"]}


def _fact_request(
    *,
    aggregate_id: str,
    fact_set_id: str,
    subject_key: str,
    event_id: str,
    source_kind: str,
    human_review_decision_id: str | None,
) -> LabelFactRevisionCreate:
    return LabelFactRevisionCreate.model_validate(
        {
            "aggregate_id": aggregate_id,
            "source_kind": source_kind,
            "human_review_decision_id": human_review_decision_id,
            "fact_set_id": fact_set_id,
            "fact_namespace": SOURCE_NAMESPACE,
            "subject_scope": "session",
            "subject_key": subject_key,
            "event_or_segment_id": event_id,
            "assertion_slot": "canonical",
            "occurred_at": datetime(2026, 7, 18, 10, tzinfo=UTC),
            "occurred_at_origin": "source",
            "label_version_id": SOURCE_VERSION_ID,
            "label_id": LABEL_ID,
            "value_type": "categorical",
            "value": "buy",
            "authority": (
                "human-confirmed" if source_kind == "human-decision" else "l2-auto-accepted"
            ),
            "expected_head_generation": 0,
        }
    )


def _metric_request(
    *,
    mode: str,
    metric_result_id: str,
    fact_set: LabelFactSet,
    generation: int,
    value: float,
) -> LabelMetricResultMaterializeRequest:
    normalized = mode == "normalized"
    target_version_id = TARGET_VERSION_ID if mode != "native" else None
    fact_as_of = fact_set.fact_as_of
    if fact_as_of.tzinfo is None or fact_as_of.utcoffset() is None:
        fact_as_of = fact_as_of.replace(tzinfo=UTC)
    return LabelMetricResultMaterializeRequest.model_validate(
        {
            "metric_result_id": metric_result_id,
            "metric_key": "intent_rate",
            "metric_family": "rate",
            "value": value,
            "unit": "ratio",
            "sample_size": 2,
            "source_run_id": f"run-metric-{mode}",
            "taxonomy_mode": mode,
            "source_label_version_ids": [SOURCE_VERSION_ID],
            "target_label_version_id": target_version_id,
            "mapping_bundle_id": MAPPING_BUNDLE_ID if normalized else None,
            "fact_namespace": fact_set.fact_namespace,
            "fact_set_id": fact_set.fact_set_id,
            "expected_fact_set_generation": generation,
            "fact_as_of": fact_as_of,
            "metric_definition_versions": {"intent_rate": "metric-catalog/3"},
            "timezone": "Asia/Shanghai",
            "period_boundary": "calendar-day:[start,end)",
            "denominator_definition": "eligible events in the frozen FactSet",
            "result_payload": {
                "label": f"Intent rate ({mode})",
                "time_range": "2026-07-18/2026-07-18",
                "dimensions": {"store_ids": []},
                "scope": {
                    "time_range": "2026-07-18/2026-07-18",
                    "store_ids": [],
                    "model_version": "model-governance-v2",
                    "label_version": SOURCE_VERSION_ID,
                },
            },
        }
    )


def _immutable_fact_snapshot(fact: LabelFact) -> dict[str, object]:
    return {
        "action_trace_id": fact.action_trace_id,
        "aggregate_id": fact.aggregate_id,
        "content_sha256": fact.content_sha256,
        "fact_set_id": fact.fact_set_id,
        "human_review_decision_id": fact.human_review_decision_id,
        "label_version_id": fact.label_version_id,
        "payload": fact.payload,
        "recorded_at": fact.recorded_at,
        "recompute_run_item_id": fact.recompute_run_item_id,
        "root_trace_id": fact.root_trace_id,
        "source_kind": fact.source_kind,
        "status": fact.status,
        "updated_at": fact.updated_at,
        "value_json": fact.value_json,
    }


def test_versioned_fact_lifecycle_statistics_report_and_fact_set_rollback_are_one_chain() -> None:
    _seed_governance_anchors()
    source_fact_set = _publish_initial_fact_set(
        key="source-native",
        fact_namespace=SOURCE_NAMESPACE,
        target_label_version_id=SOURCE_VERSION_ID,
        row_count=2,
    )
    prior_recomputed = _publish_initial_fact_set(
        key="prior-recomputed",
        fact_namespace=RECOMPUTED_NAMESPACE,
        target_label_version_id=TARGET_VERSION_ID,
        row_count=0,
    )

    auto_request = _fact_request(
        aggregate_id="aggregate-governance-auto",
        fact_set_id=str(source_fact_set["fact_set_id"]),
        subject_key="session-governance-auto",
        event_id="segment-governance-auto",
        source_kind="aggregate",
        human_review_decision_id=None,
    )
    human_request = _fact_request(
        aggregate_id="aggregate-governance-human",
        fact_set_id=str(source_fact_set["fact_set_id"]),
        subject_key="session-governance-human",
        event_id="segment-governance-human",
        source_kind="human-decision",
        human_review_decision_id="decision-governance-human",
    )
    auto_ctx = _ctx("append-auto-fact")
    with SessionLocal.begin() as session:
        auto_fact = append_label_fact_revision(session, auto_ctx, auto_request)
        human_fact = append_label_fact_revision(
            session,
            _ctx("append-human-fact"),
            human_request,
        )
    with SessionLocal.begin() as session:
        assert append_label_fact_revision(session, auto_ctx, auto_request) == auto_fact

    with SessionLocal() as session:
        facts = [
            session.get(LabelFact, str(auto_fact["fact_id"])),
            session.get(LabelFact, str(human_fact["fact_id"])),
        ]
        assert all(fact is not None for fact in facts)
        auto_row, human_row = facts
        assert auto_row is not None and human_row is not None
        assert auto_row.source_kind == "aggregate" and auto_row.aggregate_id is not None
        assert auto_row.human_review_decision_id is None
        assert human_row.source_kind == "human-decision" and human_row.aggregate_id is None
        assert human_row.human_review_decision_id == "decision-governance-human"
        assert all(
            fact.label_version_id == SOURCE_VERSION_ID
            and fact.status == "recorded"
            and fact.active_slot is None
            for fact in (auto_row, human_row)
        )
        frozen_source_facts = {
            fact.fact_id: _immutable_fact_snapshot(fact) for fact in (auto_row, human_row)
        }

    with SessionLocal() as session, pytest.raises(ApiError) as outside_scope:
        append_label_fact_revision(
            session,
            _ctx("append-cross-project", project_id="project-outside-governance"),
            auto_request,
        )
    assert outside_scope.value.code == "LABEL_FACT_SOURCE_NOT_FOUND"

    with SessionLocal.begin() as session:
        source_set = session.get(LabelFactSet, str(source_fact_set["fact_set_id"]))
        assert source_set is not None
        native_request = _metric_request(
            mode="native",
            metric_result_id="metric-governance-native",
            fact_set=source_set,
            generation=1,
            value=0.5,
        )
        native_metric = materialize_label_metric_result(
            session,
            _ctx("metric-native"),
            native_request,
        )

    recompute_request = LabelRecomputeRunCreateRequest(
        target_label_version_id=TARGET_VERSION_ID,
        mapping_bundle_id=None,
        mapping_bundle_sha256=None,
        source_environment="production",
        source_fact_namespace=SOURCE_NAMESPACE,
        source_head_generation=1,
        source_fact_set_id=str(source_fact_set["fact_set_id"]),
        source_manifest_sha256=str(source_fact_set["manifest_sha256"]),
        fact_namespace=RECOMPUTED_NAMESPACE,
        fact_as_of=FACT_AS_OF,
        partitions=[
            LabelRecomputePartition(
                partition_id=partition_id,
                source_scope={"date": f"2026-07-{index + 17:02d}"},
            )
            for index, partition_id in enumerate(("partition-a", "partition-b"))
        ],
        asset_scope={"asset_ids": ["audio-governance"]},
        coverage_policy={"mode": "all-partitions"},
        coverage_min=1.0,
        budget={"unit": "facts"},
        budget_units=100,
    )
    recompute_ctx = _ctx("recompute-create")
    with SessionLocal.begin() as session:
        recompute = create_label_recompute_run(session, recompute_ctx, recompute_request)
    with SessionLocal.begin() as session:
        assert create_label_recompute_run(session, recompute_ctx, recompute_request) == recompute

    with SessionLocal.begin() as session:
        run = session.get(LabelRecomputeRun, str(recompute["recompute_run_id"]))
        assert run is not None
        items = list(
            session.scalars(
                select(LabelRecomputeRunItem)
                .where(LabelRecomputeRunItem.recompute_run_id == run.recompute_run_id)
                .order_by(LabelRecomputeRunItem.partition_id)
            )
        )
        assert len(items) == 2
        for index, item in enumerate(items):
            receipt_id = _execution_success(session, item)
            candidate = _seed_candidate_lineage(session, item)
            completed = complete_label_recompute_run_item(
                session,
                _ctx(f"recompute-complete-{index}", roles=("model_engineer",)),
                run.recompute_run_id,
                item.recompute_run_item_id,
                LabelRecomputeRunItemCompletionRequest(
                    attempt_generation=1,
                    completion_receipt_id=receipt_id,
                    status="success",
                    facts=[candidate],
                ),
            )
        assert completed["run_status"] == "candidate-complete"
        candidate_set = session.get(LabelFactSet, run.candidate_fact_set_id)
        assert candidate_set is not None and candidate_set.row_count == 2
        validate_label_fact_set(
            session,
            _ctx("recompute-validate", roles=("model_engineer",)),
            candidate_set.fact_set_id,
            LabelFactSetValidateRequest(expected_manifest_sha256=candidate_set.manifest_sha256),
        )
        approve_label_fact_set(
            session,
            _ctx("recompute-approve"),
            candidate_set.fact_set_id,
            LabelFactSetApproveRequest(
                expected_manifest_sha256=candidate_set.manifest_sha256,
                approval_id="approval-governance-recompute",
                reason="all actual recompute facts are verified",
            ),
        )
        promoted = promote_label_fact_set(
            session,
            _ctx("recompute-promote"),
            candidate_set.fact_set_id,
            LabelFactSetPromoteRequest(
                environment="production",
                action="promote",
                expected_generation=1,
                expected_current_fact_set_id=str(prior_recomputed["fact_set_id"]),
                expected_current_manifest_sha256=str(prior_recomputed["manifest_sha256"]),
            ),
        )
        assert promoted["generation"] == 2
        candidate_fact_set_id = candidate_set.fact_set_id
        candidate_manifest_sha256 = candidate_set.manifest_sha256

    with SessionLocal.begin() as session:
        candidate_set = session.get(LabelFactSet, candidate_fact_set_id)
        assert candidate_set is not None
        normalized_request = _metric_request(
            mode="normalized",
            metric_result_id="metric-governance-normalized",
            fact_set=candidate_set,
            generation=2,
            value=0.5,
        )
        recomputed_request = _metric_request(
            mode="recomputed",
            metric_result_id="metric-governance-recomputed",
            fact_set=candidate_set,
            generation=2,
            value=0.5,
        )
        normalized_metric = materialize_label_metric_result(
            session,
            _ctx("metric-normalized"),
            normalized_request,
        )
        recomputed_metric = materialize_label_metric_result(
            session,
            _ctx("metric-recomputed"),
            recomputed_request,
        )

    with SessionLocal.begin() as session:
        metrics = [
            session.get(MetricResult, metric_id)
            for metric_id in (
                "metric-governance-native",
                "metric-governance-normalized",
                "metric-governance-recomputed",
            )
        ]
        assert all(metric is not None for metric in metrics)
        concrete_metrics = [metric for metric in metrics if metric is not None]
        report_document = {
            "artifact_state": "materialized",
            "document_version": 1,
            "metric_result_ids": [metric.metric_result_id for metric in concrete_metrics],
            "schema_version": "auris.insight-report.v2",
        }
        report = InsightReport(
            report_id="report-governance-closed-loop",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            run_id="run-report-governance",
            status="ready",
            report_type="tags",
            trace_id=TRACE_ID,
            payload={
                "metric_result_ids": report_document["metric_result_ids"],
                "report_document": report_document,
                "report_document_sha256": _sha(report_document),
            },
        )
        session.add(report)
        session.flush()
        binding = bind_insight_report_metrics(
            session,
            _ctx("report-bind"),
            report,
            concrete_metrics,
        )
        replay = bind_insight_report_metrics(
            session,
            _ctx("report-bind"),
            report,
            concrete_metrics,
        )
        assert replay["deduplicated"] is True
        assert replay["content_sha256"] == binding["content_sha256"]
        verify_insight_report_metric_binding(session, report, concrete_metrics)
        frozen_report_payload = json.loads(json.dumps(report.payload, sort_keys=True))
        frozen_binding_sha256 = str(binding["content_sha256"])
        frozen_metric_hashes = {
            metric.metric_result_id: (
                metric.content_sha256,
                metric.scope_sha256,
                metric.source_manifest_sha256,
            )
            for metric in concrete_metrics
        }

    # Publish a target-version FactSet into the old source namespace so the source
    # version is no longer a current downstream reference.
    partition_manifest = {
        "partitions": [{"partition_id": "replacement", "row_count": 2}],
        "schema_version": "auris.label-fact-partitions/1",
    }
    replacement_request = LabelFactSetCreateRequest(
        fact_namespace=SOURCE_NAMESPACE,
        target_label_version_id=TARGET_VERSION_ID,
        fact_as_of=FACT_AS_OF,
        partition_manifest=partition_manifest,
        partition_manifest_sha256=strict_canonical_sha256(partition_manifest),
        source_manifest_sha256=candidate_manifest_sha256,
        result_manifest_sha256=candidate_manifest_sha256,
        row_count=2,
    )
    with SessionLocal.begin() as session:
        created = create_label_fact_set(
            session,
            _ctx("replacement-source-namespace-create"),
            replacement_request,
        )
        replacement_id = str(created["fact_set_id"])
        replacement_manifest = str(created["manifest_sha256"])
        validate_label_fact_set(
            session,
            _ctx("replacement-source-namespace-validate", roles=("model_engineer",)),
            replacement_id,
            LabelFactSetValidateRequest(expected_manifest_sha256=replacement_manifest),
        )
        approve_label_fact_set(
            session,
            _ctx("replacement-source-namespace-approve"),
            replacement_id,
            LabelFactSetApproveRequest(
                expected_manifest_sha256=replacement_manifest,
                approval_id="approval-replacement-source-namespace",
                reason="replacement target is verified",
            ),
        )
        source_head_advanced = promote_label_fact_set(
            session,
            _ctx("replacement-source-namespace-promote"),
            replacement_id,
            LabelFactSetPromoteRequest(
                environment="production",
                action="promote",
                expected_generation=1,
                expected_current_fact_set_id=str(source_fact_set["fact_set_id"]),
                expected_current_manifest_sha256=str(source_fact_set["manifest_sha256"]),
            ),
        )
        assert source_head_advanced["generation"] == 2

    transition_request = LabelVersionTransitionRequest(
        action="deprecate",
        expected_resource_version=1,
        replacement_label_version_id=TARGET_VERSION_ID,
        mapping_bundle_id=MAPPING_BUNDLE_ID,
        reason="replace source taxonomy while preserving immutable statistics",
    )
    with SessionLocal.begin() as session:
        preflight = create_label_version_deprecation_preflight(
            session,
            _ctx("deprecate-source-preflight"),
            SOURCE_VERSION_ID,
            LabelVersionDeprecationPreflightRequest(
                expected_resource_version=1,
                replacement_label_version_id=TARGET_VERSION_ID,
                mapping_bundle_id=MAPPING_BUNDLE_ID,
                reason=transition_request.reason,
                impact_limit=100,
            ),
        )
        assert preflight["ready_for_transition"] is True
        assert preflight["blocking_impact_total"] == 0
        assert {item["impact_type"] for item in preflight["downstream_impacts"]} >= {
            "fact-set",
            "metric-result",
            "report-document",
        }
    transition_ctx = _ctx("deprecate-source-version")
    with SessionLocal.begin() as session:
        transition = transition_label_version(
            session,
            transition_ctx,
            SOURCE_VERSION_ID,
            transition_request,
        )
    with SessionLocal.begin() as session:
        assert (
            transition_label_version(
                session,
                transition_ctx,
                SOURCE_VERSION_ID,
                transition_request,
            )
            == transition
        )
        assert transition["artifact_status"] == "deprecated"
        assert transition["replacement_label_version_id"] == TARGET_VERSION_ID
        assert transition["mapping_bundle_id"] == MAPPING_BUNDLE_ID

    with SessionLocal.begin() as session:
        rolled_back = promote_label_fact_set(
            session,
            _ctx("recompute-rollback"),
            str(prior_recomputed["fact_set_id"]),
            LabelFactSetPromoteRequest(
                environment="production",
                action="rollback",
                expected_generation=2,
                expected_current_fact_set_id=candidate_fact_set_id,
                expected_current_manifest_sha256=candidate_manifest_sha256,
            ),
        )
        assert rolled_back["generation"] == 3
        assert rolled_back["current_fact_set_id"] == prior_recomputed["fact_set_id"]
        assert (
            verify_label_fact_set_head_chain(
                session,
                _ctx("verify-recomputed-head"),
                environment="production",
                fact_namespace=RECOMPUTED_NAMESPACE,
            )["generation"]
            == 3
        )

    with SessionLocal() as session:
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        assert version is not None
        assert version.artifact_status == version.status == "deprecated"
        assert version.replacement_label_version_id == TARGET_VERSION_ID
        assert version.resource_version == 2

        for fact_id, expected in frozen_source_facts.items():
            fact = session.get(LabelFact, fact_id)
            assert fact is not None
            assert _immutable_fact_snapshot(fact) == expected

        recomputed_facts = list(
            session.scalars(
                select(LabelFact)
                .where(LabelFact.fact_set_id == candidate_fact_set_id)
                .order_by(LabelFact.fact_id)
            )
        )
        assert len(recomputed_facts) == 2
        assert all(
            fact.source_kind == "recompute-run-item"
            and fact.aggregate_id is None
            and fact.human_review_decision_id is None
            and fact.recompute_run_item_id is not None
            and fact.status == "recorded"
            and fact.active_slot is None
            and fact.content_sha256
            for fact in recomputed_facts
        )

        scopes = list(
            session.scalars(
                select(MetricResultLabelScope).where(
                    MetricResultLabelScope.metric_result_id.in_(frozen_metric_hashes)
                )
            )
        )
        assert {scope.taxonomy_mode for scope in scopes} == {
            "native",
            "normalized",
            "recomputed",
        }
        for metric_id, expected_hashes in frozen_metric_hashes.items():
            metric = session.get(MetricResult, metric_id)
            assert metric is not None
            assert (
                metric.content_sha256,
                metric.scope_sha256,
                metric.source_manifest_sha256,
            ) == expected_hashes

        report = session.get(InsightReport, "report-governance-closed-loop")
        assert report is not None and report.payload == frozen_report_payload
        report_binding = session.scalar(
            select(InsightReportMetricBinding).where(
                InsightReportMetricBinding.report_id == report.report_id
            )
        )
        assert report_binding is not None
        assert report_binding.content_sha256 == frozen_binding_sha256
        report_detail = report_detail_payload(session, _ctx("report-read"), report)
        assert [item["taxonomy_mode"] for item in report_detail["metric_results"]] == [
            "native",
            "normalized",
            "recomputed",
        ]

        projected_metrics = [
            current_metric_payloads(
                session,
                _ctx(f"metric-read-{mode}"),
                metric_keys=["intent_rate"],
                taxonomy_mode=mode,
                time_range="2026-07-18/2026-07-18",
            )[0]
            for mode in ("native", "normalized", "recomputed")
        ]
        assert {item["taxonomy_mode"] for item in projected_metrics} == {
            "native",
            "normalized",
            "recomputed",
        }
        assert (
            current_metric_payloads(
                session,
                _ctx("metric-read-outside", project_id="project-outside-governance"),
                taxonomy_mode="native",
                time_range="2026-07-18/2026-07-18",
            )
            == []
        )

        required_actions = {
            "insight_metric.materialized",
            "insight_report.metric_binding.created",
            "label_fact.created",
            "label_fact_set_head.rollback",
            "label_version.deprecated",
        }
        required_events = {
            "insight_metric.materialized",
            "insight_report.metric_binding.created",
            "label_fact.created",
            "label_fact_set.promoted",
            "label_version.deprecated",
        }
        audit_actions = set(
            session.scalars(
                select(AuditLog.action).where(
                    AuditLog.tenant_id == TENANT_ID,
                    AuditLog.project_id == PROJECT_ID,
                )
            )
        )
        outbox_types = set(
            session.scalars(
                select(OutboxEvent.event_type).where(
                    OutboxEvent.tenant_id == TENANT_ID,
                    OutboxEvent.project_id == PROJECT_ID,
                )
            )
        )
        assert required_actions <= audit_actions
        assert required_events <= outbox_types
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "label_version.deprecated")
            )
            == 1
        )
        assert all(
            trace_id == TRACE_ID
            for trace_id in session.scalars(
                select(MetricResult.root_trace_id).where(
                    MetricResult.metric_result_id.in_(frozen_metric_hashes)
                )
            )
        )
        assert native_metric["taxonomy_mode"] == "native"
        assert normalized_metric["taxonomy_mode"] == "normalized"
        assert recomputed_metric["taxonomy_mode"] == "recomputed"
