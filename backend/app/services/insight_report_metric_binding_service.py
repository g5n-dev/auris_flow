from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.domain.label_mapping import sha256_document
from app.models import (
    InsightReport,
    InsightReportMetricBinding,
    MetricResult,
    MetricResultLabelScope,
)
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event


def _binding_hashes(
    report: InsightReport,
    entries: list[dict[str, Any]],
) -> tuple[str, str]:
    metric_scope_sha256 = sha256_document(
        {
            "metric_scopes": [
                {
                    "metric_result_id": item["metric_result_id"],
                    "scope_sha256": item["scope_sha256"],
                }
                for item in entries
            ],
            "schema_version": "auris.insight-report-metric-scope-set/1",
        }
    )
    content_sha256 = sha256_document(
        {
            "metric_result_bindings": entries,
            "metric_scope_sha256": metric_scope_sha256,
            "project_id": report.project_id,
            "report_id": report.report_id,
            "schema_version": "auris.insight-report-metric-binding/1",
            "tenant_id": report.tenant_id,
        }
    )
    return metric_scope_sha256, content_sha256


def _metric_binding_entry(
    metric: MetricResult,
    label_scope: MetricResultLabelScope | None,
) -> dict[str, Any]:
    if metric.status != "materialized" or metric.payload.get("immutable") is not True:
        raise ApiError(
            "INSIGHT_REPORT_METRIC_NOT_IMMUTABLE",
            "报告只能绑定已物化的不可变 MetricResult",
            409,
            details=[{"metric_result_id": metric.metric_result_id}],
        )
    label_required = metric.payload.get("label_version_applicability") == "required"
    if label_required and label_scope is None:
        raise ApiError(
            "INSIGHT_REPORT_LABEL_SCOPE_MISSING",
            "标签派生 MetricResult 缺少强 LabelScope，禁止生成报告",
            409,
            details=[{"metric_result_id": metric.metric_result_id}],
        )
    if label_scope is not None:
        expected = {
            "content_sha256": label_scope.content_sha256,
            "scope_sha256": label_scope.scope_sha256,
            "source_manifest_sha256": label_scope.source_manifest_sha256,
        }
        actual = {
            "content_sha256": metric.content_sha256,
            "scope_sha256": metric.scope_sha256,
            "source_manifest_sha256": metric.source_manifest_sha256,
        }
        if actual != expected or not all(isinstance(value, str) for value in actual.values()):
            raise ApiError(
                "INSIGHT_REPORT_LABEL_SCOPE_DRIFT",
                "MetricResult 与一对一 LabelScope 哈希不一致",
                409,
                details=[{"metric_result_id": metric.metric_result_id}],
            )
        return {
            "content_sha256": label_scope.content_sha256,
            "label_scope_id": label_scope.metric_scope_id,
            "metric_result_id": metric.metric_result_id,
            "scope_sha256": label_scope.scope_sha256,
            "source_manifest_sha256": label_scope.source_manifest_sha256,
        }

    # Legacy/non-label metrics still receive a frozen report binding. Their
    # generic scope is hashed exactly once here; label-derived metrics may not
    # use this compatibility path.
    return {
        "content_sha256": metric.content_sha256 or sha256_document(metric.payload),
        "label_scope_id": None,
        "metric_result_id": metric.metric_result_id,
        "scope_sha256": metric.scope_sha256 or sha256_document(metric.payload.get("scope") or {}),
        "source_manifest_sha256": metric.source_manifest_sha256
        or sha256_document(
            {
                "source_run_id": metric.payload.get("source_run_id"),
                "trace_id": metric.trace_id,
            }
        ),
    }


def bind_insight_report_metrics(
    session: Session,
    ctx: RequestContext,
    report: InsightReport,
    metric_results: list[MetricResult],
) -> dict[str, Any]:
    """Freeze the exact ordered MetricResult set consumed by one report."""

    if report.tenant_id != ctx.tenant_id or report.project_id != ctx.project_id:
        raise ApiError("INSIGHT_REPORT_SCOPE_FORBIDDEN", "报告不属于当前租户项目", 404)
    if not metric_results:
        raise ApiError(
            "INSIGHT_REPORT_METRICS_REQUIRED",
            "报告必须至少绑定一个不可变 MetricResult",
            422,
        )
    metric_result_ids = [item.metric_result_id for item in metric_results]
    if len(metric_result_ids) != len(set(metric_result_ids)):
        raise ApiError(
            "INSIGHT_REPORT_METRICS_DUPLICATE",
            "报告不能重复绑定同一 MetricResult",
            422,
        )
    outside_scope = [
        item.metric_result_id
        for item in metric_results
        if item.tenant_id != ctx.tenant_id or item.project_id != ctx.project_id
    ]
    if outside_scope:
        raise ApiError(
            "INSIGHT_REPORT_METRIC_SCOPE_FORBIDDEN",
            "报告引用了其他租户或项目的 MetricResult",
            404,
        )
    scopes = list(
        session.scalars(
            select(MetricResultLabelScope).where(
                MetricResultLabelScope.tenant_id == ctx.tenant_id,
                MetricResultLabelScope.project_id == ctx.project_id,
                MetricResultLabelScope.metric_result_id.in_(metric_result_ids),
            )
        )
    )
    scopes_by_result = {item.metric_result_id: item for item in scopes}
    entries = [
        _metric_binding_entry(metric, scopes_by_result.get(metric.metric_result_id))
        for metric in metric_results
    ]
    metric_scope_sha256, content_sha256 = _binding_hashes(report, entries)
    existing = session.scalar(
        select(InsightReportMetricBinding).where(
            InsightReportMetricBinding.tenant_id == ctx.tenant_id,
            InsightReportMetricBinding.project_id == ctx.project_id,
            InsightReportMetricBinding.report_id == report.report_id,
        )
    )
    if existing is not None:
        if existing.content_sha256 != content_sha256:
            raise ApiError(
                "INSIGHT_REPORT_METRIC_BINDING_DRIFT",
                "报告已绑定不同的不可变指标快照集合",
                409,
            )
        return {
            "content_sha256": existing.content_sha256,
            "deduplicated": True,
            "metric_result_ids": list(existing.metric_result_ids),
            "metric_scope_sha256": existing.metric_scope_sha256,
            "report_metric_binding_id": existing.report_metric_binding_id,
            "result_count": existing.result_count,
        }

    binding = InsightReportMetricBinding(
        report_metric_binding_id=public_id_from_hex(
            "irmb",
            content_sha256,
            suffix_length=24,
        ),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        report_id=report.report_id,
        metric_result_ids=metric_result_ids,
        result_count=len(metric_result_ids),
        metric_scope_sha256=metric_scope_sha256,
        content_sha256=content_sha256,
        root_trace_id=report.trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=report.trace_id,
        payload={
            "metric_result_bindings": entries,
            "schema_version": "auris.insight-report-metric-binding/1",
        },
    )
    session.add(binding)
    session.flush()
    summary = {
        "content_sha256": content_sha256,
        "metric_result_ids": metric_result_ids,
        "metric_scope_sha256": metric_scope_sha256,
        "report_id": report.report_id,
        "report_metric_binding_id": binding.report_metric_binding_id,
        "result_count": len(metric_result_ids),
    }
    audit = record_audit(
        session,
        ctx,
        action="insight_report.metric_binding.created",
        object_type="insight_report_metric_binding",
        object_id=binding.report_metric_binding_id,
        after=summary,
        trace_id=report.trace_id,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type="insight_report.metric_binding.created",
        aggregate_type="insight_report",
        aggregate_id=report.report_id,
        payload=summary,
    )
    session.flush()
    return {
        **summary,
        "audit_id": audit.audit_id,
        "deduplicated": False,
        "outbox_event_id": outbox.event_id,
    }


def verify_insight_report_metric_binding(
    session: Session,
    report: InsightReport,
    metric_results: list[MetricResult],
) -> dict[str, Any]:
    """Read-only verification that a report still references its frozen snapshots."""

    binding = session.scalar(
        select(InsightReportMetricBinding).where(
            InsightReportMetricBinding.tenant_id == report.tenant_id,
            InsightReportMetricBinding.project_id == report.project_id,
            InsightReportMetricBinding.report_id == report.report_id,
        )
    )
    if binding is None:
        raise ApiError(
            "INSIGHT_REPORT_METRIC_BINDING_MISSING",
            "报告缺少不可变 MetricResult 集合绑定",
            409,
            details=[{"report_id": report.report_id}],
        )
    ids = [item.metric_result_id for item in metric_results]
    scopes = list(
        session.scalars(
            select(MetricResultLabelScope).where(
                MetricResultLabelScope.tenant_id == report.tenant_id,
                MetricResultLabelScope.project_id == report.project_id,
                MetricResultLabelScope.metric_result_id.in_(ids),
            )
        )
    )
    scopes_by_result = {item.metric_result_id: item for item in scopes}
    entries = [
        _metric_binding_entry(metric, scopes_by_result.get(metric.metric_result_id))
        for metric in metric_results
    ]
    metric_scope_sha256, content_sha256 = _binding_hashes(report, entries)
    if (
        binding.metric_result_ids != ids
        or binding.result_count != len(ids)
        or binding.metric_scope_sha256 != metric_scope_sha256
        or binding.content_sha256 != content_sha256
        or binding.payload.get("metric_result_bindings") != entries
    ):
        raise ApiError(
            "INSIGHT_REPORT_METRIC_BINDING_DRIFT",
            "报告、MetricResult 与冻结 scope 集合不再一致",
            409,
            details=[{"report_id": report.report_id}],
        )
    return {
        "content_sha256": binding.content_sha256,
        "metric_result_ids": ids,
        "metric_scope_sha256": binding.metric_scope_sha256,
        "report_metric_binding_id": binding.report_metric_binding_id,
        "result_count": binding.result_count,
    }
