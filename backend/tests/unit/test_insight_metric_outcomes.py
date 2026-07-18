from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.context import RequestContext
from app.models import MetricResult
from app.schemas.insights import (
    InsightMetricAggregationResult,
    InsightReportMetricSnapshot,
    InsightReportRequest,
)
from app.services.insight_closure_service import _build_report_document


def test_worker_outcome_distinguishes_numeric_zero_from_governed_na() -> None:
    numeric_zero = InsightMetricAggregationResult.model_validate(
        {
            "metric_key": "purchase_rate",
            "value": 0,
            "unit": "percent",
            "sample_size": 12,
            "result_status": "value",
        }
    )
    unavailable = InsightMetricAggregationResult.model_validate(
        {
            "metric_key": "purchase_rate",
            "value": None,
            "unit": "percent",
            "sample_size": 0,
            "result_status": "zero-denominator",
            "reason_codes": ["ZERO_DENOMINATOR"],
        }
    )

    assert numeric_zero.value == 0
    assert unavailable.value is None
    with pytest.raises(ValidationError):
        InsightMetricAggregationResult.model_validate(
            {
                "metric_key": "purchase_rate",
                "value": None,
                "unit": "percent",
                "sample_size": 0,
                "result_status": "zero-denominator",
                "reason_codes": [],
            }
        )
    with pytest.raises(ValidationError):
        InsightMetricAggregationResult.model_validate(
            {
                "metric_key": "purchase_rate",
                "value": None,
                "unit": "percent",
                "sample_size": 0,
                "result_status": "zero-denominator",
                "reason_codes": ["FREE_FORM_REASON"],
            }
        )


def test_report_document_freezes_na_outcome_and_reason_without_coercing_zero() -> None:
    metric = MetricResult(
        metric_result_id="metric-na",
        tenant_id="tenant-a",
        project_id="project-a",
        status="materialized",
        trace_id="trace-metric-na",
        payload={
            "comparability_reason_codes": ["ZERO_DENOMINATOR"],
            "comparability_status": "not-applicable",
            "definition_version": "metric/3",
            "immutable": True,
            "label": "购买率",
            "metric_key": "purchase_rate",
            "reason_codes": ["ZERO_DENOMINATOR"],
            "result_status": "zero-denominator",
            "sample_size": 0,
            "scope": {
                "time_range": "30d",
                "store_ids": [],
                "model_version": "model-v1",
                "label_version": "label-v2",
            },
            "snapshot_role": "aggregation",
            "source_run_id": "run-metric-na",
            "unit": "percent",
            "value": None,
        },
    )
    body = InsightReportRequest.model_validate(
        {
            "title": "N/A 冻结报告",
            "time_range": "30d",
            "metric_result_ids": ["metric-na"],
            "report_sections": ["metric_snapshot"],
        }
    )
    document = _build_report_document(
        ctx=RequestContext(
            tenant_id="tenant-a",
            project_id="project-a",
            user_id="user-a",
            roles=("project_admin",),
            request_id="request-a",
            trace_id="trace-report-a",
            idempotency_key="report-a",
            actor_kind="human",
        ),
        run_id="run-report-a",
        report_id="report-a",
        body=body,
        metric_results=[metric],
        evidence_resources=[],
    )

    frozen = document["metric_results"][0]
    assert frozen["value"] is None
    assert frozen["sample_size"] == 0
    assert frozen["result_status"] == "zero-denominator"
    assert frozen["reason_codes"] == ["ZERO_DENOMINATOR"]
    assert "N/A（ZERO_DENOMINATOR）" in document["sections"][0]["summary"]


def test_report_snapshot_rejects_null_without_non_comparable_reasons() -> None:
    with pytest.raises(ValidationError):
        InsightReportMetricSnapshot.model_validate(
            {
                "metric_result_id": "metric-na",
                "metric_key": "purchase_rate",
                "label": "购买率",
                "value": None,
                "unit": "percent",
                "sample_size": 0,
                "result_status": "zero-denominator",
                "reason_codes": ["ZERO_DENOMINATOR"],
                "comparability_status": "comparable",
                "comparability_reason_codes": [],
                "definition_version": "metric/3",
                "scope": {},
                "source_run_id": "run-a",
                "trace_id": "trace-a",
                "payload_sha256": "a" * 64,
            }
        )
