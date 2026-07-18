from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    DataAsset,
    InsightReport,
    InsightReportMetricBinding,
    JsonResource,
    LabelAggregate,
    LabelFact,
    LabelFactSet,
    LabelFactSetHead,
    LabelMappingBundle,
    LabelMappingBundleSource,
    LabelObservation,
    LabelTaxonomy,
    LabelVersion,
    MetricResult,
    MetricResultLabelScope,
    OutboxEvent,
    ReleaseBundleHead,
    ReleaseBundleHeadEvent,
    ReleaseDeployment,
    RunRecord,
)
from app.schemas.label_lifecycle import (
    LabelVersionDeprecationPreflightRequest,
    LabelVersionTransitionRequest,
)
from app.services.label_lifecycle_service import (
    create_label_version_deprecation_preflight,
    enrich_label_version_lifecycle_views,
    transition_label_version,
)

TENANT_ID = "tenant_label_lifecycle_service"
PROJECT_ID = "project_label_lifecycle_service"
TAXONOMY_ID = "taxonomy_lifecycle_service"
SOURCE_VERSION_ID = "lv_lifecycle_source"
TARGET_VERSION_ID = "lv_lifecycle_target"
MAPPING_BUNDLE_ID = "lmb_lifecycle_source_target"


def _ctx(
    key: str,
    *,
    user_id: str = "u_lifecycle_admin",
    roles: tuple[str, ...] = ("project_admin",),
    actor_kind: str = "human",
) -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        user_id=user_id,
        roles=roles,
        request_id=f"request-{key}",
        trace_id=f"trace-{key}",
        idempotency_key=key,
        actor_kind=actor_kind,
    )


def _version_payload(
    version_id: str,
    *,
    taxonomy_id: str,
    semantic_version: str,
    status: str,
    resource_version: int,
    content_sha256: str,
) -> dict[str, object]:
    return {
        "id": version_id,
        "label_version_id": version_id,
        "taxonomy_id": taxonomy_id,
        "semantic_version": semantic_version,
        "status": status,
        "artifact_status": status,
        "resource_version": resource_version,
        "content_sha256": content_sha256,
    }


def _add_version(
    session,
    version_id: str,
    *,
    taxonomy_id: str = TAXONOMY_ID,
    semantic_version: str,
    status: str = "published",
    resource_version: int,
    content_sha256: str,
    with_projection: bool = True,
) -> LabelVersion:
    payload = _version_payload(
        version_id,
        taxonomy_id=taxonomy_id,
        semantic_version=semantic_version,
        status=status,
        resource_version=resource_version,
        content_sha256=content_sha256,
    )
    version = LabelVersion(
        label_version_id=version_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status=status,
        resource_version=resource_version,
        taxonomy_id=taxonomy_id,
        semantic_version=semantic_version,
        artifact_status=status,
        artifact_published_at=(
            datetime(2026, 7, 1, tzinfo=UTC) if status in {"published", "deprecated"} else None
        ),
        artifact_deprecated_at=(
            datetime(2026, 7, 15, tzinfo=UTC) if status == "deprecated" else None
        ),
        content_sha256=content_sha256,
        trace_id=f"trace-{version_id}",
        payload=payload,
    )
    session.add(version)
    if with_projection:
        session.add(
            JsonResource(
                collection="label_versions",
                resource_key=version_id,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status=status,
                trace_id=f"trace-{version_id}",
                data=dict(payload),
            )
        )
    return version


def _seed_versions(
    *,
    source_status: str = "published",
    with_mapping: bool = True,
    mapping_source_resource_version: int = 7,
) -> None:
    with SessionLocal() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="生命周期标签体系",
                description="服务测试",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-taxonomy-lifecycle",
                payload={"taxonomy_id": TAXONOMY_ID},
            )
        )
        _add_version(
            session,
            SOURCE_VERSION_ID,
            semantic_version="1.0.0",
            status=source_status,
            resource_version=7 if source_status == "published" else 8,
            content_sha256="a" * 64,
        )
        _add_version(
            session,
            TARGET_VERSION_ID,
            semantic_version="2.0.0",
            resource_version=3,
            content_sha256="b" * 64,
        )
        session.flush()
        if with_mapping:
            session.add(
                LabelMappingBundle(
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    target_label_version_id=TARGET_VERSION_ID,
                    source_label_version_ids=[SOURCE_VERSION_ID],
                    source_manifest_sha256="c" * 64,
                    compiler_version="label-mapping-compiler/1",
                    status="published",
                    resource_version=1,
                    canonical_manifest_sha256="d" * 64,
                    approval_id="approval-mapping-lifecycle",
                    approved_by="u_mapping_reviewer",
                    approved_at=datetime(2026, 7, 2, tzinfo=UTC),
                    published_at=datetime(2026, 7, 3, tzinfo=UTC),
                    root_trace_id="trace-root-mapping-lifecycle",
                    trace_id="trace-mapping-lifecycle",
                    payload={"mapping_bundle_id": MAPPING_BUNDLE_ID},
                )
            )
            session.flush()
            session.add(
                LabelMappingBundleSource(
                    bundle_source_id="lmbs_lifecycle_source",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    mapping_bundle_id=MAPPING_BUNDLE_ID,
                    source_label_version_id=SOURCE_VERSION_ID,
                    source_resource_version=mapping_source_resource_version,
                    source_order=0,
                    content_sha256="e" * 64,
                    trace_id="trace-mapping-source-lifecycle",
                    payload={},
                )
            )
        session.commit()


def _add_historical_facts() -> None:
    with SessionLocal() as session:
        session.add(
            LabelObservation(
                observation_id="lo_lifecycle_history",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                extraction_run_id="ler_lifecycle_history",
                subject_scope="call",
                subject_key="call-history",
                evidence_ref={"type": "object", "id": "evidence-history"},
                evidence_sha256="f" * 64,
                label_version_id=SOURCE_VERSION_ID,
                raw_label="历史标签",
                label_id="label_history",
                value_type="boolean",
                value_json=True,
                source_family="model:lifecycle",
                source_type="model",
                model_version="model-lifecycle",
                prompt_version_id="prompt-lifecycle",
                schema_version="label-output-v1",
                calibration_version_id=None,
                raw_confidence=0.9,
                calibrated_confidence=0.85,
                input_sha256="2" * 64,
                output_sha256="3" * 64,
                status="materialized",
                trace_id="trace-observation-history",
                payload={"immutable_marker": "observation"},
            )
        )
        session.add(
            LabelAggregate(
                aggregate_id="la_lifecycle_history",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                aggregation_run_id="lar_lifecycle_history",
                label_version_id=SOURCE_VERSION_ID,
                policy_version_id="policy-lifecycle",
                calibration_version_ids=[],
                subject_scope="call",
                subject_key="call-history",
                label_id="label_history",
                value_type="boolean",
                value_json=True,
                score=0.85,
                margin=0.4,
                risk_level="low",
                decision="accept",
                status="materialized",
                reason_codes=[],
                explanation={"immutable_marker": "aggregate"},
                bucket_sha256="4" * 64,
                deterministic_hash="5" * 64,
                review_task_id=None,
                trace_id="trace-aggregate-history",
            )
        )
        session.add(
            LabelFact(
                fact_id="lf_lifecycle_history",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                aggregate_id="la_lifecycle_history",
                supersedes_fact_id=None,
                fact_namespace="production",
                logical_key_sha="6" * 64,
                revision=1,
                event_or_segment_id="call-history",
                assertion_slot="label_history",
                occurred_at=datetime(2026, 7, 10, tzinfo=UTC),
                recorded_at=datetime(2026, 7, 10, 0, 1, tzinfo=UTC),
                occurred_at_origin="source",
                source_kind="aggregate",
                content_sha256="7" * 64,
                root_trace_id="trace-root-fact-history",
                action_trace_id="trace-action-fact-history",
                label_version_id=SOURCE_VERSION_ID,
                subject_scope="call",
                subject_key="call-history",
                label_id="label_history",
                value_type="boolean",
                value_json=True,
                authority="aggregate",
                status="recorded",
                active_slot=None,
                review_decision_id=None,
                trace_id="trace-fact-history",
                payload={"immutable_marker": "fact"},
            )
        )
        session.commit()


def _add_downstream_impacts(
    *,
    fact_set_status: str = "superseded",
    data_asset_status: str = "archived",
    current_fact_set_head: bool = False,
) -> None:
    fact_as_of = datetime(2026, 7, 17, 23, 59, tzinfo=UTC)
    with SessionLocal() as session:
        session.add_all(
            [
                LabelFactSet(
                    fact_set_id="lfs_lifecycle_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    fact_namespace="production",
                    target_label_version_id=SOURCE_VERSION_ID,
                    status=fact_set_status,
                    fact_as_of=fact_as_of,
                    partition_manifest={"partitions": ["2026-07-17"]},
                    partition_manifest_sha256="1" * 64,
                    source_manifest_sha256="2" * 64,
                    result_manifest_sha256="3" * 64,
                    row_count=12,
                    manifest_sha256="4" * 64,
                    approval_id=(
                        "approval-lifecycle-impact"
                        if fact_set_status in {"approved", "published"}
                        else None
                    ),
                    approved_by=(
                        "u_lifecycle_admin"
                        if fact_set_status in {"approved", "published"}
                        else None
                    ),
                    approved_at=(
                        datetime(2026, 7, 17, 23, tzinfo=UTC)
                        if fact_set_status in {"approved", "published"}
                        else None
                    ),
                    root_trace_id="trace-root-lifecycle-impact",
                    action_trace_id="trace-action-lifecycle-impact",
                    trace_id="trace-lifecycle-impact",
                    payload={},
                ),
                MetricResult(
                    metric_result_id="metric_lifecycle_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="snapshot",
                    content_sha256="5" * 64,
                    source_manifest_sha256="6" * 64,
                    scope_sha256="7" * 64,
                    root_trace_id="trace-root-lifecycle-impact",
                    action_trace_id="trace-action-lifecycle-impact",
                    trace_id="trace-lifecycle-impact",
                    payload={"metric_key": "label-impact-rate"},
                ),
                RunRecord(
                    run_id="run_lifecycle_report_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    run_type="insight_report",
                    status="success",
                    trace_id="trace-lifecycle-impact",
                    payload={},
                ),
                DataAsset(
                    data_asset_id="asset_lifecycle_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status=data_asset_status,
                    trace_id="trace-lifecycle-impact",
                    payload={"locked_versions": {"label_version_id": SOURCE_VERSION_ID}},
                ),
                DataAsset(
                    data_asset_id="asset_lifecycle_other_project",
                    tenant_id=TENANT_ID,
                    project_id="project-outside-lifecycle-scope",
                    status="published",
                    trace_id="trace-lifecycle-outside",
                    payload={"label_version_id": SOURCE_VERSION_ID},
                ),
            ]
        )
        session.flush()
        if current_fact_set_head:
            session.add(
                LabelFactSetHead(
                    fact_set_head_id="lfsh_lifecycle_impact",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    environment="production",
                    fact_namespace="production",
                    current_fact_set_id="lfs_lifecycle_impact",
                    current_manifest_sha256="4" * 64,
                    previous_fact_set_id=None,
                    previous_manifest_sha256=None,
                    generation=1,
                    status="active",
                    root_trace_id="trace-root-lifecycle-impact",
                    action_trace_id="trace-action-lifecycle-impact",
                    trace_id="trace-lifecycle-impact",
                    payload={},
                )
            )
        session.add(
            MetricResultLabelScope(
                metric_scope_id="metric_scope_lifecycle_impact",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                metric_result_id="metric_lifecycle_impact",
                taxonomy_mode="native",
                source_label_version_ids=[SOURCE_VERSION_ID],
                target_label_version_id=None,
                mapping_bundle_id=None,
                mapping_bundle_sha256=None,
                fact_namespace="production",
                fact_set_id="lfs_lifecycle_impact",
                fact_set_manifest_sha256="4" * 64,
                fact_set_generation=1,
                fact_as_of=fact_as_of,
                metric_definition_versions={"label-impact-rate": "1.0.0"},
                timezone="Asia/Shanghai",
                period_boundary="[2026-07-17,2026-07-18)",
                denominator_definition="frozen eligible events",
                label_version_applicability="required",
                comparability_status="comparable",
                comparability_reason_codes=[],
                scope_sha256="7" * 64,
                source_manifest_sha256="6" * 64,
                content_sha256="8" * 64,
                root_trace_id="trace-root-lifecycle-impact",
                action_trace_id="trace-action-lifecycle-impact",
                trace_id="trace-lifecycle-impact",
                payload={},
            )
        )
        session.add(
            InsightReport(
                report_id="report_lifecycle_impact",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_id="run_lifecycle_report_impact",
                status="ready",
                report_type="tags",
                trace_id="trace-lifecycle-impact",
                payload={
                    "report_document": {
                        "schema_version": "auris.insight-report.v2",
                        "document_version": 1,
                        "artifact_state": "materialized",
                    }
                },
            )
        )
        session.flush()
        session.add(
            InsightReportMetricBinding(
                report_metric_binding_id="report_binding_lifecycle_impact",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                report_id="report_lifecycle_impact",
                metric_result_ids=["metric_lifecycle_impact"],
                result_count=1,
                metric_scope_sha256="9" * 64,
                content_sha256="a" * 64,
                root_trace_id="trace-root-lifecycle-impact",
                action_trace_id="trace-action-lifecycle-impact",
                trace_id="trace-lifecycle-impact",
                payload={},
            )
        )
        session.commit()


def _preflight_request(
    *,
    replacement_id: str | None = TARGET_VERSION_ID,
    mapping_bundle_id: str | None = MAPPING_BUNDLE_ID,
    expected_resource_version: int = 7,
    reason: str = "标签定义升级",
) -> LabelVersionDeprecationPreflightRequest:
    return LabelVersionDeprecationPreflightRequest(
        expected_resource_version=expected_resource_version,
        replacement_label_version_id=replacement_id,
        mapping_bundle_id=mapping_bundle_id,
        reason=reason,
    )


def _transition_request(
    *,
    action: Literal["deprecate", "archive"] = "deprecate",
    replacement_id: str | None = TARGET_VERSION_ID,
    mapping_bundle_id: str | None = MAPPING_BUNDLE_ID,
    expected_resource_version: int = 7,
    reason: str = "标签定义升级",
) -> LabelVersionTransitionRequest:
    return LabelVersionTransitionRequest(
        action=action,
        expected_resource_version=expected_resource_version,
        replacement_label_version_id=replacement_id,
        mapping_bundle_id=mapping_bundle_id,
        reason=reason,
    )


def test_preflight_and_deprecate_with_replacement_are_atomic_and_replay_exactly() -> None:
    _seed_versions()
    _add_historical_facts()

    preflight_ctx = _ctx("preflight-with-replacement")
    with SessionLocal() as session:
        preflight = create_label_version_deprecation_preflight(
            session,
            preflight_ctx,
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
        assert preflight["ready_for_transition"] is True
        assert preflight["safe_stop_required"] is False
        assert preflight["blockers"] == []
        session.commit()

    transition_ctx = _ctx("deprecate-with-replacement")
    with SessionLocal() as session:
        first = transition_label_version(
            session,
            transition_ctx,
            SOURCE_VERSION_ID,
            _transition_request(),
        )
        session.commit()
    with SessionLocal() as session:
        replay = transition_label_version(
            session,
            transition_ctx,
            SOURCE_VERSION_ID,
            _transition_request(),
        )
        session.commit()

        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == SOURCE_VERSION_ID,
            )
        )
        assert replay == first
        assert version is not None and version.artifact_status == "deprecated"
        assert version.status == "deprecated" and version.resource_version == 8
        assert version.replacement_label_version_id == TARGET_VERSION_ID
        assert version.deprecation_reason == "标签定义升级"
        assert projection is not None and projection.status == "deprecated"
        assert projection.data["resource_version"] == 8
        assert projection.data["mapping_bundle_id"] == MAPPING_BUNDLE_ID
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.tenant_id == TENANT_ID,
                    AuditLog.project_id == PROJECT_ID,
                    AuditLog.action == "label_version.deprecated",
                )
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == TENANT_ID,
                    OutboxEvent.project_id == PROJECT_ID,
                    OutboxEvent.event_type == "label_version.deprecated",
                )
            )
            == 1
        )
        assert session.get(LabelObservation, "lo_lifecycle_history").payload == {
            "immutable_marker": "observation"
        }
        assert session.get(LabelAggregate, "la_lifecycle_history").explanation == {
            "immutable_marker": "aggregate"
        }
        assert session.get(LabelFact, "lf_lifecycle_history").payload == {
            "immutable_marker": "fact"
        }


def test_no_replacement_active_production_head_requires_safe_stop_and_blocks_transition() -> None:
    _seed_versions(with_mapping=False)
    with SessionLocal() as session:
        deployment = ReleaseDeployment(
            deployment_id="rd_lifecycle_active",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            environment="production",
            status="completed",
            stage="completed",
            label_version_id=SOURCE_VERSION_ID,
            prompt_version_id="prompt-lifecycle",
            model_version="model-lifecycle",
            aggregation_policy_version_id="policy-lifecycle",
            eval_dataset_version_id="dataset-lifecycle",
            eval_run_id="eval-lifecycle",
            rollback_target_deployment_id=None,
            bundle_sha256="6" * 64,
            rollout_percentage=100,
            blocked_reasons=[],
            monitor_metrics={},
            approved_by="u_lifecycle_admin",
            trace_id="trace-active-deployment",
            payload={},
        )
        session.add(deployment)
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id="rbh_lifecycle_active",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
                active_deployment_id=deployment.deployment_id,
                active_bundle_sha256=deployment.bundle_sha256,
                prompt_asset_id="prompt-asset-lifecycle",
                prompt_version_id=deployment.prompt_version_id,
                label_version_id=SOURCE_VERSION_ID,
                model_version=deployment.model_version,
                aggregation_policy_version_id=deployment.aggregation_policy_version_id,
                eval_dataset_version_id=deployment.eval_dataset_version_id,
                generation=4,
                status="active",
                bootstrapped=False,
                activated_by_command_id=None,
                trace_id="trace-active-head",
                payload={},
            )
        )
        session.commit()

    body = _preflight_request(replacement_id=None, mapping_bundle_id=None)
    with SessionLocal() as session:
        result = create_label_version_deprecation_preflight(
            session,
            _ctx("preflight-safe-stop"),
            SOURCE_VERSION_ID,
            body,
        )
        session.commit()
    assert result["ready_for_transition"] is False
    assert result["safe_stop_required"] is True
    assert result["active_environment_references"] == [
        {
            "deployment_id": "rd_lifecycle_active",
            "environment": "production",
            "head_generation": 4,
            "reference_status": "active",
        }
    ]

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        transition_label_version(
            session,
            _ctx("deprecate-safe-stop"),
            SOURCE_VERSION_ID,
            _transition_request(replacement_id=None, mapping_bundle_id=None),
        )
    assert exc_info.value.code == "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE"
    assert exc_info.value.details[0]["safe_stop_required"] is True
    with SessionLocal() as session:
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        assert version is not None and version.artifact_status == "published"


def test_draining_deployment_blocks_deprecation_even_with_replacement() -> None:
    _seed_versions()
    with SessionLocal() as session:
        session.add(
            ReleaseDeployment(
                deployment_id="rd_lifecycle_draining",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="staging",
                status="draining",
                stage="draining",
                label_version_id=SOURCE_VERSION_ID,
                prompt_version_id="prompt-lifecycle",
                model_version="model-lifecycle",
                aggregation_policy_version_id="policy-lifecycle",
                eval_dataset_version_id="dataset-lifecycle",
                eval_run_id="eval-lifecycle",
                rollback_target_deployment_id=None,
                bundle_sha256="7" * 64,
                rollout_percentage=0,
                blocked_reasons=[],
                monitor_metrics={},
                approved_by="u_lifecycle_admin",
                trace_id="trace-draining-deployment",
                payload={},
            )
        )
        session.commit()

    with SessionLocal() as session:
        preflight = create_label_version_deprecation_preflight(
            session,
            _ctx("preflight-draining"),
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
        session.commit()
    assert preflight["ready_for_transition"] is False
    assert preflight["draining_environment_references"][0]["deployment_id"] == (
        "rd_lifecycle_draining"
    )

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        transition_label_version(
            session,
            _ctx("deprecate-draining"),
            SOURCE_VERSION_ID,
            _transition_request(),
        )
    assert exc_info.value.code == "LABEL_VERSION_ACTIVE_ENVIRONMENT_REFERENCE"


def test_deprecate_without_replacement_succeeds_when_no_environment_references_remain() -> None:
    _seed_versions(with_mapping=False)
    with SessionLocal() as session:
        result = transition_label_version(
            session,
            _ctx("deprecate-retire"),
            SOURCE_VERSION_ID,
            _transition_request(replacement_id=None, mapping_bundle_id=None),
        )
        session.commit()
    assert result["artifact_status"] == "deprecated"
    assert result["replacement_label_version_id"] is None
    assert result["mapping_bundle_id"] is None
    assert result["safe_stop_required"] is False
    assert result["normalized_disposition"] == "coverage-gap"


def test_preflight_pages_historical_impacts_without_blocking_retire_or_mutating_snapshots() -> None:
    _seed_versions(with_mapping=False)
    _add_downstream_impacts()

    first_request = _preflight_request(
        replacement_id=None,
        mapping_bundle_id=None,
    ).model_copy(update={"impact_limit": 2})
    with SessionLocal() as session:
        first = create_label_version_deprecation_preflight(
            session,
            _ctx("preflight-impact-page-one"),
            SOURCE_VERSION_ID,
            first_request,
        )
        session.commit()

    assert first["downstream_impact_total"] == 4
    assert first["blocking_impact_total"] == 0
    assert first["migration_required_impact_total"] == 0
    assert first["historical_reference_total"] == 4
    assert len(first["downstream_impacts"]) == 2
    assert first["impact_next_cursor"]
    assert first["impact_scan_complete"] is True
    assert first["migration_evidence_required"] is False
    assert first["migration_evidence_satisfied"] is True
    assert first["ready_for_transition"] is True
    assert first["blockers"] == []

    second_request = first_request.model_copy(update={"impact_cursor": first["impact_next_cursor"]})
    with SessionLocal() as session:
        second = create_label_version_deprecation_preflight(
            session,
            _ctx("preflight-impact-page-two"),
            SOURCE_VERSION_ID,
            second_request,
        )
        session.commit()
    impact_types = {
        impact["impact_type"]
        for impact in [*first["downstream_impacts"], *second["downstream_impacts"]]
    }
    assert impact_types == {
        "data-asset",
        "fact-set",
        "metric-result",
        "report-document",
    }
    assert all(
        impact["resource_id"] != "asset_lifecycle_other_project"
        for impact in [*first["downstream_impacts"], *second["downstream_impacts"]]
    )
    assert all(
        impact["impact_disposition"] == "historical-reference"
        for impact in [*first["downstream_impacts"], *second["downstream_impacts"]]
    )

    with SessionLocal() as session:
        result = transition_label_version(
            session,
            _ctx("deprecate-impact-without-migration"),
            SOURCE_VERSION_ID,
            _transition_request(replacement_id=None, mapping_bundle_id=None),
        )
        session.commit()
        metric = session.get(MetricResult, "metric_lifecycle_impact")
        report = session.get(InsightReport, "report_lifecycle_impact")
    assert result["artifact_status"] == "deprecated"
    assert metric is not None and metric.content_sha256 == "5" * 64
    assert report is not None and report.payload["report_document"] == {
        "schema_version": "auris.insight-report.v2",
        "document_version": 1,
        "artifact_state": "materialized",
    }


def test_replacement_mapping_does_not_bypass_current_fact_set_head_blocker() -> None:
    _seed_versions()
    _add_downstream_impacts(
        fact_set_status="published",
        current_fact_set_head=True,
    )

    with SessionLocal() as session:
        preflight = create_label_version_deprecation_preflight(
            session,
            _ctx("preflight-impact-with-mapping"),
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
        session.commit()
    assert preflight["downstream_impact_total"] == 4
    assert preflight["blocking_impact_total"] == 1
    assert preflight["historical_reference_total"] == 3
    assert preflight["migration_evidence_required"] is False
    assert preflight["migration_evidence_satisfied"] is True
    assert preflight["ready_for_transition"] is False
    assert {blocker["code"] for blocker in preflight["blockers"]} == {
        "LABEL_VERSION_ACTIVE_DOWNSTREAM_REFERENCE"
    }

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        transition_label_version(
            session,
            _ctx("deprecate-impact-with-mapping"),
            SOURCE_VERSION_ID,
            _transition_request(),
        )
    assert exc_info.value.code == "LABEL_VERSION_ACTIVE_DOWNSTREAM_REFERENCE"
    with SessionLocal() as session:
        fact_set = session.get(LabelFactSet, "lfs_lifecycle_impact")
        metric = session.get(MetricResult, "metric_lifecycle_impact")
        report = session.get(InsightReport, "report_lifecycle_impact")
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
    assert fact_set is not None and fact_set.status == "published"
    assert metric is not None and metric.content_sha256 == "5" * 64
    assert report is not None and report.status == "ready"
    assert version is not None and version.artifact_status == "published"


def test_lifecycle_projection_assigns_each_closed_interval_only_to_activated_version() -> None:
    _seed_versions()
    boundary = datetime(2026, 7, 18, 10, tzinfo=UTC)
    with SessionLocal() as session:
        source_deployment = ReleaseDeployment(
            deployment_id="rd_lifecycle_interval_source",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            environment="staging",
            status="superseded",
            stage="completed",
            label_version_id=SOURCE_VERSION_ID,
            prompt_version_id="prompt-lifecycle-source",
            model_version="model-lifecycle",
            aggregation_policy_version_id="policy-lifecycle",
            eval_dataset_version_id="dataset-lifecycle",
            eval_run_id="eval-lifecycle-source",
            rollback_target_deployment_id=None,
            bundle_sha256="b" * 64,
            rollout_percentage=0,
            blocked_reasons=[],
            monitor_metrics={},
            approved_by="u_lifecycle_admin",
            trace_id="trace-lifecycle-source-deployment",
            payload={},
        )
        target_deployment = ReleaseDeployment(
            deployment_id="rd_lifecycle_interval_target",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            environment="staging",
            status="completed",
            stage="completed",
            label_version_id=TARGET_VERSION_ID,
            prompt_version_id="prompt-lifecycle-target",
            model_version="model-lifecycle",
            aggregation_policy_version_id="policy-lifecycle",
            eval_dataset_version_id="dataset-lifecycle",
            eval_run_id="eval-lifecycle-target",
            rollback_target_deployment_id=None,
            bundle_sha256="c" * 64,
            rollout_percentage=100,
            blocked_reasons=[],
            monitor_metrics={},
            approved_by="u_lifecycle_admin",
            trace_id="trace-lifecycle-target-deployment",
            payload={},
        )
        session.add_all([source_deployment, target_deployment])
        session.flush()
        session.add(
            ReleaseBundleHead(
                release_head_id="rbh_lifecycle_interval",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="staging",
                active_deployment_id=target_deployment.deployment_id,
                active_bundle_sha256=target_deployment.bundle_sha256,
                prompt_asset_id="prompt-asset-lifecycle",
                prompt_version_id=target_deployment.prompt_version_id,
                label_version_id=TARGET_VERSION_ID,
                model_version=target_deployment.model_version,
                aggregation_policy_version_id=target_deployment.aggregation_policy_version_id,
                eval_dataset_version_id=target_deployment.eval_dataset_version_id,
                generation=2,
                status="active",
                bootstrapped=False,
                activated_by_command_id=None,
                trace_id="trace-lifecycle-interval-head",
                payload={},
            )
        )
        source_event = ReleaseBundleHeadEvent(
            head_event_id="rbhe_lifecycle_interval_source",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            environment="staging",
            generation=1,
            previous_generation=None,
            action="activate",
            activation_status="active",
            old_deployment_id=None,
            new_deployment_id=source_deployment.deployment_id,
            old_label_version_id=None,
            new_label_version_id=SOURCE_VERSION_ID,
            old_bundle_sha256=None,
            new_bundle_sha256=source_deployment.bundle_sha256,
            effective_from=datetime(2026, 7, 18, 9, tzinfo=UTC),
            effective_to=None,
            command_id=None,
            completion_receipt_id=None,
            approval_id=None,
            content_sha256="d" * 64,
            actor_id="u_lifecycle_admin",
            root_trace_id="trace-root-lifecycle-interval",
            trace_id="trace-lifecycle-interval-source",
            payload={"canonical_effective_to": None},
        )
        session.add(source_event)
        session.flush()
        source_event.effective_to = boundary
        session.flush()
        session.add(
            ReleaseBundleHeadEvent(
                head_event_id="rbhe_lifecycle_interval_target",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="staging",
                generation=2,
                previous_generation=1,
                action="promote",
                activation_status="active",
                old_deployment_id=source_deployment.deployment_id,
                new_deployment_id=target_deployment.deployment_id,
                old_label_version_id=SOURCE_VERSION_ID,
                new_label_version_id=TARGET_VERSION_ID,
                old_bundle_sha256=source_deployment.bundle_sha256,
                new_bundle_sha256=target_deployment.bundle_sha256,
                effective_from=boundary,
                effective_to=None,
                command_id=None,
                completion_receipt_id=None,
                approval_id=None,
                content_sha256="e" * 64,
                actor_id="u_lifecycle_admin",
                root_trace_id="trace-root-lifecycle-interval",
                trace_id="trace-lifecycle-interval-target",
                payload={"canonical_effective_to": None},
            )
        )
        session.commit()

    with SessionLocal() as session:
        projected = enrich_label_version_lifecycle_views(
            session,
            _ctx("lifecycle-interval-read"),
            [{"id": SOURCE_VERSION_ID}, {"id": TARGET_VERSION_ID}],
            include_timeline=True,
        )
    by_id = {item["id"]: item for item in projected}
    source_timeline = by_id[SOURCE_VERSION_ID]["activation_timeline"]
    target_timeline = by_id[TARGET_VERSION_ID]["activation_timeline"]
    assert [item["generation"] for item in source_timeline] == [1]
    assert source_timeline[0]["effective_to"] == boundary.isoformat()
    assert [item["generation"] for item in target_timeline] == [2]
    assert target_timeline[0]["effective_from"] == boundary.isoformat()
    assert target_timeline[0]["effective_to"] is None


def test_archive_preserves_deprecation_binding_and_updates_projection() -> None:
    _seed_versions(source_status="deprecated", with_mapping=False)
    with SessionLocal() as session:
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == SOURCE_VERSION_ID,
            )
        )
        assert version is not None and projection is not None
        version.replacement_label_version_id = TARGET_VERSION_ID
        version.deprecation_reason = "先前废弃原因"
        version.payload = {
            **version.payload,
            "replacement_label_version_id": TARGET_VERSION_ID,
            "deprecation_reason": "先前废弃原因",
            "mapping_bundle_id": MAPPING_BUNDLE_ID,
        }
        projection.data = dict(version.payload)
        session.commit()

    body = _transition_request(
        action="archive",
        replacement_id=None,
        mapping_bundle_id=None,
        expected_resource_version=8,
        reason="达到归档保留期限",
    )
    with SessionLocal() as session:
        result = transition_label_version(
            session,
            _ctx("archive-version"),
            SOURCE_VERSION_ID,
            body,
        )
        session.commit()
        version = session.get(LabelVersion, SOURCE_VERSION_ID)
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == SOURCE_VERSION_ID,
            )
        )
        assert result["artifact_status"] == "archived"
        assert version is not None and version.resource_version == 9
        assert version.replacement_label_version_id == TARGET_VERSION_ID
        assert version.deprecation_reason == "先前废弃原因"
        assert projection is not None and projection.status == "archived"
        assert projection.data["mapping_bundle_id"] == MAPPING_BUNDLE_ID


@pytest.mark.parametrize(
    ("ctx", "expected_code"),
    [
        (
            _ctx("system-forbidden", roles=("system",), actor_kind="system"),
            "AGENT_LABEL_LIFECYCLE_TRANSITION_FORBIDDEN",
        ),
        (_ctx("agent-forbidden", actor_kind="agent"), "AGENT_LABEL_LIFECYCLE_TRANSITION_FORBIDDEN"),
        (_ctx("role-forbidden", roles=("model_engineer",)), "FORBIDDEN"),
    ],
)
def test_lifecycle_writes_require_a_human_project_admin(
    ctx: RequestContext,
    expected_code: str,
) -> None:
    _seed_versions()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            ctx,
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
    assert exc_info.value.code == expected_code


def test_resource_version_and_projection_drift_fail_closed() -> None:
    _seed_versions()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        transition_label_version(
            session,
            _ctx("cas-conflict"),
            SOURCE_VERSION_ID,
            _transition_request(expected_resource_version=6),
        )
    assert exc_info.value.code == "RESOURCE_VERSION_CONFLICT"

    with SessionLocal() as session:
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == TENANT_ID,
                JsonResource.project_id == PROJECT_ID,
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == SOURCE_VERSION_ID,
            )
        )
        assert projection is not None
        projection.data = {**projection.data, "resource_version": 6}
        session.commit()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        transition_label_version(
            session,
            _ctx("projection-conflict"),
            SOURCE_VERSION_ID,
            _transition_request(),
        )
    assert exc_info.value.code == "LABEL_VERSION_PROJECTION_DRIFT"


def test_replacement_taxonomy_and_cycle_are_validated() -> None:
    _seed_versions()
    with SessionLocal() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id="taxonomy_other",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="其他标签体系",
                description=None,
                status="active",
                resource_version=1,
                content_sha256="8" * 64,
                trace_id="trace-taxonomy-other",
                payload={},
            )
        )
        _add_version(
            session,
            "lv_other_taxonomy",
            taxonomy_id="taxonomy_other",
            semantic_version="1.0.0",
            resource_version=1,
            content_sha256="9" * 64,
        )
        session.commit()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            _ctx("taxonomy-mismatch"),
            SOURCE_VERSION_ID,
            _preflight_request(
                replacement_id="lv_other_taxonomy",
                mapping_bundle_id=MAPPING_BUNDLE_ID,
            ),
        )
    assert exc_info.value.code == "LABEL_VERSION_REPLACEMENT_TAXONOMY_MISMATCH"

    with SessionLocal() as session:
        target = session.get(LabelVersion, TARGET_VERSION_ID)
        assert target is not None
        target.replacement_label_version_id = SOURCE_VERSION_ID
        session.commit()
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            _ctx("replacement-cycle"),
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
    assert exc_info.value.code == "LABEL_VERSION_REPLACEMENT_CYCLE"


def test_mapping_source_resource_version_is_validated() -> None:
    _seed_versions(mapping_source_resource_version=6)
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            _ctx("mapping-source-drift"),
            SOURCE_VERSION_ID,
            _preflight_request(),
        )
    assert exc_info.value.code == "LABEL_MAPPING_BUNDLE_SOURCE_VERSION_CONFLICT"


def test_idempotency_key_rejects_a_different_body_and_another_actor() -> None:
    _seed_versions()
    ctx = _ctx("preflight-idempotency")
    with SessionLocal() as session:
        create_label_version_deprecation_preflight(
            session,
            ctx,
            SOURCE_VERSION_ID,
            _preflight_request(reason="第一次预检"),
        )
        session.commit()

    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            ctx,
            SOURCE_VERSION_ID,
            _preflight_request(reason="相同键不同请求"),
        )
    assert exc_info.value.code == "IDEMPOTENCY_KEY_CONFLICT"

    other_actor = replace(ctx, user_id="u_other_lifecycle_admin")
    with SessionLocal() as session, pytest.raises(ApiError) as exc_info:
        create_label_version_deprecation_preflight(
            session,
            other_actor,
            SOURCE_VERSION_ID,
            _preflight_request(reason="第一次预检"),
        )
    assert exc_info.value.code == "LABEL_LIFECYCLE_IDEMPOTENCY_ACTOR_CONFLICT"
