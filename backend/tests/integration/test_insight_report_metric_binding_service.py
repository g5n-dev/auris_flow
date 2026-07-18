from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AuditLog,
    InsightReport,
    InsightReportMetricBinding,
    MetricResult,
    OutboxEvent,
    RunRecord,
)
from app.services.insight_report_metric_binding_service import bind_insight_report_metrics

TENANT_ID = "tenant_report_metric_binding"
PROJECT_ID = "project_report_metric_binding"
REPORT_ID = "report-metric-binding"


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        user_id="u_report_owner",
        roles=("project_admin",),
        request_id="request-report-metric-binding",
        trace_id="action-report-metric-binding",
        idempotency_key="idem-report-metric-binding",
        actor_kind="human",
    )


def _metric(metric_result_id: str, metric_key: str) -> MetricResult:
    return MetricResult(
        metric_result_id=metric_result_id,
        tenant_id=TENANT_ID,
        project_id=PROJECT_ID,
        status="materialized",
        trace_id="root-report-metric",
        payload={
            "immutable": True,
            "metric_key": metric_key,
            "scope": {
                "time_range": "2026-07",
                "store_ids": ["store-1"],
                "model_version": "model-v1",
                "label_version": None,
            },
            "source_run_id": "run-report-metrics",
        },
    )


def _seed_report(*, label_required_without_scope: bool = False) -> None:
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id="run-report-generation",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_type="insight_report",
                status="pending",
                run_key="report-binding",
                partition_key=f"{TENANT_ID}/{PROJECT_ID}",
                trace_id="root-report-metric",
                payload={},
            )
        )
        session.flush()
        session.add(
            InsightReport(
                report_id=REPORT_ID,
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_id="run-report-generation",
                status="generating",
                report_type="management_summary",
                trace_id="root-report-metric",
                payload={},
            )
        )
        first = _metric("metric-report-binding-1", "metric_one")
        second = _metric("metric-report-binding-2", "metric_two")
        if label_required_without_scope:
            first.payload = {
                **first.payload,
                "label_version_applicability": "required",
            }
        session.add_all([first, second])


def test_report_binding_freezes_ordered_metric_ids_and_scope_set_with_audit_outbox() -> None:
    _seed_report()
    ctx = _ctx()
    with SessionLocal.begin() as session:
        report = session.get(InsightReport, REPORT_ID)
        assert report is not None
        metrics = list(
            session.scalars(
                select(MetricResult)
                .where(
                    MetricResult.metric_result_id.in_(
                        ["metric-report-binding-1", "metric-report-binding-2"]
                    )
                )
                .order_by(MetricResult.metric_result_id)
            )
        )
        created = bind_insight_report_metrics(session, ctx, report, metrics)
        replay = bind_insight_report_metrics(session, ctx, report, metrics)
        assert replay["deduplicated"] is True
        assert replay["content_sha256"] == created["content_sha256"]

    with SessionLocal() as session:
        binding = session.scalar(select(InsightReportMetricBinding))
        assert binding is not None
        assert binding.metric_result_ids == [
            "metric-report-binding-1",
            "metric-report-binding-2",
        ]
        assert binding.result_count == 2
        assert len(binding.metric_scope_sha256) == 64
        assert len(binding.content_sha256) == 64
        assert [
            item["metric_result_id"] for item in binding.payload["metric_result_bindings"]
        ] == binding.metric_result_ids
        assert session.scalar(select(func.count()).select_from(InsightReportMetricBinding)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "insight_report.metric_binding.created")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.event_type == "insight_report.metric_binding.created")
            )
            == 1
        )


def test_label_required_metric_without_strong_scope_fails_without_partial_binding() -> None:
    _seed_report(label_required_without_scope=True)
    with SessionLocal() as session:
        report = session.get(InsightReport, REPORT_ID)
        assert report is not None
        metrics = list(
            session.scalars(select(MetricResult).order_by(MetricResult.metric_result_id))
        )
        with pytest.raises(ApiError) as missing:
            bind_insight_report_metrics(session, _ctx(), report, metrics)
    assert missing.value.code == "INSIGHT_REPORT_LABEL_SCOPE_MISSING"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(InsightReportMetricBinding)) == 0
