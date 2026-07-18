from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    InsightReport,
    LabelFactSet,
    LabelFactSetHead,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    MetricResult,
    MetricResultLabelScope,
    OutboxEvent,
    RunRecord,
)
from app.schemas.label_metric_scopes import (
    LabelMetricResultMaterializeRequest,
    LabelMetricRunScopeRequest,
)
from app.services.insight_closure_service import (
    current_metric_payloads,
    report_detail_payload,
)
from app.services.insight_report_metric_binding_service import bind_insight_report_metrics
from app.services.label_metric_scope_service import (
    lock_label_metric_run_scope,
    materialize_label_metric_result,
)

TENANT_ID = "tenant_label_metric_scope"
PROJECT_ID = "project_label_metric_scope"
TAXONOMY_ID = "taxonomy_label_metric_scope"
SOURCE_VERSION_ID = "label_version_metric_source"
TARGET_VERSION_ID = "label_version_metric_target"
LABEL_ID = "purchase-intent"
FACT_NAMESPACE = "production"
FACT_AS_OF = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)


def _ctx(key: str, *, tenant_id: str = TENANT_ID) -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        project_id=PROJECT_ID,
        user_id="u_metric_admin",
        roles=("project_admin",),
        request_id=f"request-{key}",
        trace_id=f"action-{key}",
        idempotency_key=key,
        actor_kind="human",
    )


def _version(version_id: str, *, semver: str, content: str) -> LabelVersion:
    return LabelVersion(
        label_version_id=version_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="published",
        resource_version=1,
        taxonomy_id=TAXONOMY_ID,
        semantic_version=semver,
        artifact_status="published",
        content_sha256=content * 64,
        trace_id=f"trace-{version_id}",
        payload={},
    )


def _version_item(version_id: str, *, suffix: str) -> LabelVersionItem:
    return LabelVersionItem(
        label_version_item_id=f"lvi-metric-{suffix}",
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        label_version_id=version_id,
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
        trace_id=f"trace-item-{suffix}",
    )


def _fact_set(
    fact_set_id: str,
    *,
    target_version_id: str,
    manifest_character: str,
    fact_as_of: datetime = FACT_AS_OF,
) -> LabelFactSet:
    return LabelFactSet(
        fact_set_id=fact_set_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        fact_namespace=FACT_NAMESPACE,
        target_label_version_id=target_version_id,
        status="published",
        fact_as_of=fact_as_of,
        partition_manifest={"2026-07": {"row_count": 1}},
        partition_manifest_sha256="3" * 64,
        source_manifest_sha256="4" * 64,
        result_manifest_sha256="5" * 64,
        row_count=1,
        manifest_sha256=manifest_character * 64,
        approval_id=f"approval-{fact_set_id}",
        approved_by="u_metric_admin",
        approved_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
        root_trace_id="root-metric-run",
        action_trace_id="trace-fact-set-action",
        trace_id="root-metric-run",
        payload={},
    )


def _seed_scope(*, normalized: bool = False) -> None:
    with SessionLocal.begin() as session:
        session.add(
            LabelTaxonomy(
                taxonomy_id=TAXONOMY_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                name="标签指标口径体系",
                description="不可变指标 scope 测试",
                status="active",
                resource_version=1,
                content_sha256="1" * 64,
                trace_id="trace-metric-taxonomy",
                payload={},
            )
        )
        session.add(_version(SOURCE_VERSION_ID, semver="1.0.0", content="a"))
        if normalized:
            session.add(_version(TARGET_VERSION_ID, semver="2.0.0", content="b"))
        session.flush()
        session.add(_version_item(SOURCE_VERSION_ID, suffix="source"))
        if normalized:
            session.add(_version_item(TARGET_VERSION_ID, suffix="target"))
        session.add(
            RunRecord(
                run_id="run-label-metric-scope",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="insight_metric_aggregation",
                status="submitted",
                run_key="metric-scope-run",
                partition_key=f"{TENANT_ID}/{PROJECT_ID}",
                trace_id="root-metric-run",
                payload={"metric_keys": ["purchase_intent_rate"]},
            )
        )
        target_version_id = TARGET_VERSION_ID if normalized else SOURCE_VERSION_ID
        fact_set = _fact_set(
            "fact-set-metric-v1",
            target_version_id=target_version_id,
            manifest_character="6",
        )
        session.add(fact_set)
        session.flush()
        session.add(
            LabelFactSetHead(
                fact_set_head_id="fact-set-head-metric",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
                fact_namespace=FACT_NAMESPACE,
                current_fact_set_id=fact_set.fact_set_id,
                current_manifest_sha256=fact_set.manifest_sha256,
                previous_fact_set_id=None,
                previous_manifest_sha256=None,
                generation=1,
                status="active",
                root_trace_id="root-metric-run",
                action_trace_id="trace-fact-set-action",
                trace_id="root-metric-run",
                payload={},
            )
        )
        if normalized:
            bundle = LabelMappingBundle(
                mapping_bundle_id="mapping-bundle-metric",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                target_label_version_id=TARGET_VERSION_ID,
                source_label_version_ids=[SOURCE_VERSION_ID],
                source_manifest_sha256="7" * 64,
                compiler_version="label-mapping-bundle-compiler/1",
                status="published",
                resource_version=1,
                canonical_manifest_sha256="8" * 64,
                approval_id="approval-mapping-metric",
                approved_by="u_metric_admin",
                approved_at=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
                published_at=datetime(2026, 7, 18, 8, 30, tzinfo=UTC),
                root_trace_id="root-mapping-metric",
                trace_id="root-mapping-metric",
                payload={},
            )
            session.add(bundle)
            session.flush()
            session.add(
                LabelMappingBundlePath(
                    bundle_path_id="mapping-path-metric",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    mapping_bundle_id=bundle.mapping_bundle_id,
                    source_label_version_id=SOURCE_VERSION_ID,
                    target_label_version_id=TARGET_VERSION_ID,
                    source_label_id=LABEL_ID,
                    target_label_id=None,
                    metric_family="rate",
                    relation_path=[{"relation": "split-recompute"}],
                    mapping_version_ids=["mapping-edge-metric"],
                    metric_grain="business-event",
                    lineage_key="event_or_segment_id",
                    reducer=None,
                    comparability_status="structural-break",
                    requires_recompute=True,
                    path_sha256="9" * 64,
                    trace_id="root-mapping-metric",
                    payload={},
                )
            )


def _request(
    *,
    metric_result_id: str = "metric-label-scope-v1",
    taxonomy_mode: str = "native",
    expected_generation: int = 1,
    legacy_label_version: str = SOURCE_VERSION_ID,
) -> LabelMetricResultMaterializeRequest:
    normalized = taxonomy_mode == "normalized"
    return LabelMetricResultMaterializeRequest.model_validate(
        {
            "metric_result_id": metric_result_id,
            "metric_key": "purchase_intent_rate",
            "metric_family": "rate",
            "value": 0.42,
            "unit": "ratio",
            "sample_size": 120,
            "source_run_id": "run-label-metric-scope",
            "taxonomy_mode": taxonomy_mode,
            "source_label_version_ids": [SOURCE_VERSION_ID],
            "target_label_version_id": TARGET_VERSION_ID if normalized else None,
            "mapping_bundle_id": "mapping-bundle-metric" if normalized else None,
            "fact_namespace": FACT_NAMESPACE,
            "fact_set_id": "fact-set-metric-v1",
            "expected_fact_set_generation": expected_generation,
            "fact_as_of": FACT_AS_OF,
            "metric_definition_versions": {"purchase_intent_rate": "metric-catalog/3"},
            "timezone": "Asia/Shanghai",
            "period_boundary": "calendar-month:[start,end)",
            "denominator_definition": "eligible business events in locked FactSet",
            "result_payload": {
                "label": "购买意向率",
                "source_external_id": "dagster-metric-001",
                "time_range": "30d",
                "dimensions": {"store_ids": []},
                "scope": {
                    "time_range": "30d",
                    "store_ids": [],
                    "model_version": "model-metric-v1",
                    "label_version": legacy_label_version,
                },
            },
        }
    )


def _run_scope_request() -> LabelMetricRunScopeRequest:
    return LabelMetricRunScopeRequest.model_validate(
        {
            "taxonomy_mode": "native",
            "source_label_version_ids": [SOURCE_VERSION_ID],
            "target_label_version_id": None,
            "mapping_bundle_id": None,
            "fact_namespace": FACT_NAMESPACE,
            "fact_set_id": "fact-set-metric-v1",
            "expected_fact_set_generation": 1,
            "fact_as_of": FACT_AS_OF,
            "timezone": "Asia/Shanghai",
            "period_boundary": "calendar-month:[start,end)",
            "denominator_definition": "eligible business events in locked FactSet",
        }
    )


def test_native_snapshot_freezes_scope_audit_outbox_and_idempotent_replay() -> None:
    _seed_scope()
    request = _request()
    ctx = _ctx("materialize-native-label-metric")
    with SessionLocal.begin() as session:
        created = materialize_label_metric_result(session, ctx, request)
    with SessionLocal.begin() as session:
        replay = materialize_label_metric_result(session, ctx, request)
        assert replay == created

        metric = session.get(MetricResult, request.metric_result_id)
        scope = session.scalar(
            select(MetricResultLabelScope).where(
                MetricResultLabelScope.metric_result_id == request.metric_result_id
            )
        )
        assert metric is not None
        assert scope is not None
        assert metric.content_sha256 == scope.content_sha256 == created["content_sha256"]
        assert metric.scope_sha256 == scope.scope_sha256 == created["scope_sha256"]
        assert metric.source_manifest_sha256 == scope.source_manifest_sha256
        assert metric.root_trace_id == scope.root_trace_id == "root-metric-run"
        assert metric.action_trace_id == scope.action_trace_id == ctx.trace_id
        assert scope.taxonomy_mode == "native"
        assert scope.target_label_version_id is None
        assert scope.mapping_bundle_id is None
        assert scope.fact_set_generation == 1
        assert scope.label_version_applicability == "required"
        assert scope.comparability_status == "comparable"
        assert scope.comparability_reason_codes == ["NATIVE_VERSION_PARTITIONED"]
        assert session.scalar(select(func.count()).select_from(MetricResult)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "insight_metric.materialized")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "insight_metric.materialized")
            )
            == 1
        )


def test_metric_read_projection_joins_the_authoritative_label_scope() -> None:
    _seed_scope()
    request = _request()
    with SessionLocal.begin() as session:
        materialize_label_metric_result(
            session,
            _ctx("materialize-readable-label-metric"),
            request,
        )

    with SessionLocal() as session:
        items = current_metric_payloads(
            session,
            _ctx("read-label-metric"),
            time_range="30d",
            label_version=SOURCE_VERSION_ID,
            label_version_applicability="required",
            taxonomy_mode="native",
            source_label_version_ids=[SOURCE_VERSION_ID],
            fact_set_generation=1,
            fact_as_of=FACT_AS_OF,
            model_version="model-metric-v1",
        )

    assert len(items) == 1
    item = items[0]
    assert item["metric_result_id"] == request.metric_result_id
    assert item["label_scope"] == {
        "taxonomy_mode": "native",
        "source_label_version_ids": [SOURCE_VERSION_ID],
        "target_label_version_id": None,
        "mapping_bundle_id": None,
        "fact_set_generation": 1,
        "fact_as_of": "2026-07-18T10:00:00Z",
        "metric_definition_versions": {"purchase_intent_rate": "metric-catalog/3"},
        "timezone": "Asia/Shanghai",
        "period_boundary": "calendar-month:[start,end)",
        "denominator_definition": "eligible business events in locked FactSet",
    }
    assert item["scope"]["label_scope"] == item["label_scope"]
    assert item["comparability_status"] == "comparable"
    assert item["comparability_reason_codes"] == ["NATIVE_VERSION_PARTITIONED"]
    assert item["scope_sha256"]
    assert item["source_manifest_sha256"]
    assert item["content_sha256"]


def test_metric_read_projection_never_uses_legacy_label_version_when_strong_scope_exists() -> None:
    _seed_scope()
    request = _request(legacy_label_version="legacy-payload-version")
    with SessionLocal.begin() as session:
        materialize_label_metric_result(
            session,
            _ctx("materialize-strong-over-legacy"),
            request,
        )

    with SessionLocal() as session:
        authoritative = current_metric_payloads(
            session,
            _ctx("read-authoritative-version"),
            label_version=SOURCE_VERSION_ID,
        )
        legacy_mismatch = current_metric_payloads(
            session,
            _ctx("read-legacy-version"),
            label_version="legacy-payload-version",
        )

    assert [item["metric_result_id"] for item in authoritative] == [request.metric_result_id]
    assert legacy_mismatch == []


def test_normalized_metric_read_projection_filters_every_authoritative_scope_field() -> None:
    _seed_scope(normalized=True)
    request = _request(taxonomy_mode="normalized")
    with SessionLocal.begin() as session:
        materialize_label_metric_result(
            session,
            _ctx("materialize-normalized-readable"),
            request,
        )

    filters = {
        "time_range": "30d",
        "model_version": "model-metric-v1",
        "label_version_applicability": "required",
        "taxonomy_mode": "normalized",
        "source_label_version_ids": [SOURCE_VERSION_ID],
        "target_label_version_id": TARGET_VERSION_ID,
        "mapping_bundle_id": "mapping-bundle-metric",
        "fact_set_generation": 1,
        "fact_as_of": FACT_AS_OF,
    }
    mismatches = [
        {"label_version_applicability": "none"},
        {"taxonomy_mode": "native"},
        {"source_label_version_ids": [SOURCE_VERSION_ID, TARGET_VERSION_ID]},
        {"target_label_version_id": SOURCE_VERSION_ID},
        {"mapping_bundle_id": "mapping-bundle-other"},
        {"fact_set_generation": 2},
        {"fact_as_of": FACT_AS_OF + timedelta(seconds=1)},
    ]
    with SessionLocal() as session:
        matched = current_metric_payloads(
            session,
            _ctx("read-normalized-exact"),
            **filters,
        )
        rejected = [
            current_metric_payloads(
                session,
                _ctx(f"read-normalized-mismatch-{index}"),
                **{**filters, **mismatch},
            )
            for index, mismatch in enumerate(mismatches)
        ]

    assert [item["metric_result_id"] for item in matched] == [request.metric_result_id]
    assert matched[0]["label_scope"]["taxonomy_mode"] == "normalized"
    assert matched[0]["label_scope"]["target_label_version_id"] == TARGET_VERSION_ID
    assert matched[0]["label_scope"]["mapping_bundle_id"] == "mapping-bundle-metric"
    assert rejected == [[] for _item in mismatches]


def test_metric_read_projection_fails_closed_when_required_scope_is_missing() -> None:
    with SessionLocal.begin() as session:
        session.add(
            MetricResult(
                metric_result_id="metric-required-without-scope",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                status="materialized",
                trace_id="root-missing-label-scope",
                payload={
                    "immutable": True,
                    "label_version_applicability": "required",
                    "metric_key": "purchase_intent_rate",
                    "snapshot_role": "aggregation",
                    "scope": {
                        "time_range": "30d",
                        "store_ids": [],
                        "model_version": "model-metric-v1",
                        "label_version": SOURCE_VERSION_ID,
                    },
                },
            )
        )

    with SessionLocal() as session, pytest.raises(ApiError) as missing:
        current_metric_payloads(session, _ctx("read-missing-label-scope"))

    assert missing.value.code == "INSIGHT_METRIC_LABEL_SCOPE_MISSING"


def test_report_detail_projects_the_same_authoritative_label_scope() -> None:
    _seed_scope()
    request = _request()
    with SessionLocal.begin() as session:
        materialize_label_metric_result(
            session,
            _ctx("materialize-report-label-metric"),
            request,
        )
        session.add(
            RunRecord(
                run_id="run-label-metric-report",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="insight_report",
                status="pending",
                run_key="label-metric-report",
                partition_key=f"{TENANT_ID}/{PROJECT_ID}",
                trace_id="root-label-metric-report",
                payload={},
            )
        )
        session.flush()
        report = InsightReport(
            report_id="report-label-metric-scope",
            tenant_id=TENANT_ID,
            project_id=PROJECT_ID,
            run_id="run-label-metric-report",
            status="generating",
            report_type="management_summary",
            trace_id="root-label-metric-report",
            payload={"metric_result_ids": [request.metric_result_id]},
        )
        session.add(report)
        session.flush()
        metric = session.get(MetricResult, request.metric_result_id)
        assert metric is not None
        bind_insight_report_metrics(session, _ctx("bind-report-label-metric"), report, [metric])

    with SessionLocal() as session:
        report = session.get(InsightReport, "report-label-metric-scope")
        assert report is not None
        detail = report_detail_payload(session, _ctx("read-report-label-metric"), report)

    assert detail["metric_result_ids"] == [request.metric_result_id]
    assert detail["metric_results"][0]["label_scope"]["source_label_version_ids"] == [
        SOURCE_VERSION_ID
    ]
    assert (
        detail["metric_results"][0]["scope"]["label_scope"]
        == detail["metric_results"][0]["label_scope"]
    )
    assert detail["metric_scope_sha256"]


def test_head_advancement_does_not_mutate_old_snapshot_and_stale_anchor_has_no_partial() -> None:
    _seed_scope()
    with SessionLocal.begin() as session:
        created = materialize_label_metric_result(
            session,
            _ctx("materialize-before-head-advance"),
            _request(),
        )

    with SessionLocal.begin() as session:
        next_set = _fact_set(
            "fact-set-metric-v2",
            target_version_id=SOURCE_VERSION_ID,
            manifest_character="c",
            fact_as_of=datetime(2026, 7, 18, 11, 0, tzinfo=UTC),
        )
        session.add(next_set)
        session.flush()
        head = session.get(LabelFactSetHead, "fact-set-head-metric")
        assert head is not None
        head.previous_fact_set_id = head.current_fact_set_id
        head.previous_manifest_sha256 = head.current_manifest_sha256
        head.current_fact_set_id = next_set.fact_set_id
        head.current_manifest_sha256 = next_set.manifest_sha256
        head.generation = 2

    with SessionLocal() as session, pytest.raises(ApiError) as stale:
        materialize_label_metric_result(
            session,
            _ctx("materialize-stale-head"),
            _request(metric_result_id="metric-label-scope-stale", expected_generation=1),
        )
    assert stale.value.code == "LABEL_METRIC_FACT_SET_HEAD_CONFLICT"

    with SessionLocal() as session:
        old_metric = session.get(MetricResult, "metric-label-scope-v1")
        assert old_metric is not None
        assert old_metric.content_sha256 == created["content_sha256"]
        assert old_metric.scope_sha256 == created["scope_sha256"]
        assert session.get(MetricResult, "metric-label-scope-stale") is None


def test_accepted_run_scope_lock_survives_later_fact_set_head_advancement() -> None:
    _seed_scope()
    ctx = _ctx("accept-label-metric-run")
    with SessionLocal.begin() as session:
        accepted_lock = lock_label_metric_run_scope(
            session,
            ctx,
            _run_scope_request(),
            [
                {
                    "metric_key": "purchase_intent_rate",
                    "metric_family": "rate",
                    "calculator_ref": "metric-catalog/3",
                    "unit": "ratio",
                }
            ],
        )

    with SessionLocal.begin() as session:
        next_set = _fact_set(
            "fact-set-metric-v2",
            target_version_id=SOURCE_VERSION_ID,
            manifest_character="d",
            fact_as_of=datetime(2026, 7, 18, 11, 0, tzinfo=UTC),
        )
        session.add(next_set)
        session.flush()
        head = session.get(LabelFactSetHead, "fact-set-head-metric")
        assert head is not None
        head.previous_fact_set_id = head.current_fact_set_id
        head.previous_manifest_sha256 = head.current_manifest_sha256
        head.current_fact_set_id = next_set.fact_set_id
        head.current_manifest_sha256 = next_set.manifest_sha256
        head.generation = 2

    with SessionLocal.begin() as session:
        created = materialize_label_metric_result(
            session,
            _ctx("complete-accepted-label-metric-run"),
            _request(metric_result_id="metric-label-scope-accepted"),
            accepted_scope_lock=accepted_lock,
        )
    assert created["fact_set_generation"] == 1
    with SessionLocal() as session:
        scope = session.scalar(
            select(MetricResultLabelScope).where(
                MetricResultLabelScope.metric_result_id == "metric-label-scope-accepted"
            )
        )
        assert scope is not None
        assert scope.fact_set_id == "fact-set-metric-v1"
        assert scope.fact_set_generation == 1


def test_normalized_structural_break_is_server_derived_from_published_bundle_paths() -> None:
    _seed_scope(normalized=True)
    with SessionLocal.begin() as session:
        created = materialize_label_metric_result(
            session,
            _ctx("materialize-normalized-label-metric"),
            _request(taxonomy_mode="normalized"),
        )
    with SessionLocal() as session:
        scope = session.scalar(select(MetricResultLabelScope))
        assert scope is not None
        assert created["comparability_status"] == "structural-break"
        assert scope.mapping_bundle_id == "mapping-bundle-metric"
        assert scope.mapping_bundle_sha256 == "8" * 64
        assert scope.target_label_version_id == TARGET_VERSION_ID
        assert "MAPPING_RECOMPUTE_REQUIRED" in scope.comparability_reason_codes
        assert "MAPPING_STRUCTURAL_BREAK" in scope.comparability_reason_codes
        assert scope.payload["mapping_path_sha256s"] == ["9" * 64]


def test_cross_scope_source_run_and_missing_definition_fail_without_rows() -> None:
    _seed_scope()
    with SessionLocal() as session, pytest.raises(ApiError) as outside_scope:
        materialize_label_metric_result(
            session,
            _ctx("materialize-outside-scope", tenant_id="other-tenant"),
            _request(metric_result_id="metric-label-scope-outside"),
        )
    assert outside_scope.value.code == "LABEL_METRIC_SOURCE_RUN_NOT_FOUND"

    invalid = _request(metric_result_id="metric-label-scope-invalid").model_copy(
        update={"metric_definition_versions": {"another_metric": "metric-catalog/3"}}
    )
    with SessionLocal() as session, pytest.raises(ApiError) as missing_definition:
        materialize_label_metric_result(
            session,
            _ctx("materialize-missing-definition"),
            invalid,
        )
    assert missing_definition.value.code == "LABEL_METRIC_DEFINITION_VERSION_REQUIRED"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(MetricResult)) == 0
        assert session.scalar(select(func.count()).select_from(MetricResultLabelScope)) == 0
