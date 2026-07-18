from __future__ import annotations

from copy import deepcopy

from app.services.metric_snapshot_comparison_service import (
    compare_metric_snapshot_anchors,
)


def _anchor() -> dict[str, object]:
    return {
        "comparability_status": "comparable",
        "content_sha256": "1" * 64,
        "definition_version": "metric/3",
        "denominator_definition": "eligible events",
        "dimensions": {"store_ids": ["store-a"]},
        "fact_as_of": "2026-07-18T10:00:00Z",
        "fact_as_of_rule": "current-not-before-baseline",
        "fact_namespace": "production",
        "fact_set_generation": 7,
        "fact_set_id": "fact-set-7",
        "fact_set_manifest_sha256": "2" * 64,
        "mapping_bundle_id": "bundle-7",
        "mapping_bundle_sha256": "3" * 64,
        "metric_definition_versions": {"purchase_rate": "metric/3"},
        "metric_key": "purchase_rate",
        "metric_result_id": "metric-baseline",
        "period_boundary": "calendar-month:[start,end)",
        "scope_sha256": "4" * 64,
        "source_label_version_ids": ["label-v7"],
        "source_manifest_sha256": "5" * 64,
        "target_label_version_id": "label-v7",
        "taxonomy_mode": "normalized",
        "time_range": "30d",
        "time_window": {
            "kind": "rolling-window",
            "rule": "30d",
            "start": None,
            "end": None,
        },
        "timezone": "Asia/Shanghai",
        "unit": "percent",
    }


def test_identical_governed_anchors_are_comparable_and_hash_is_stable() -> None:
    baseline = _anchor()
    current = deepcopy(baseline)
    current["metric_result_id"] = "metric-current"
    current["content_sha256"] = "6" * 64
    current["fact_as_of"] = "2026-07-18T11:00:00Z"

    first = compare_metric_snapshot_anchors(baseline, current)
    second = compare_metric_snapshot_anchors(baseline, current)

    assert first["comparison_status"] == "comparable"
    assert first["reason_codes"] == []
    assert first["continuous_trend_allowed"] is True
    assert first["comparison_sha256"] == second["comparison_sha256"]
    assert len(first["comparison_sha256"]) == 64


def test_every_governed_drift_creates_a_structural_break() -> None:
    baseline = _anchor()
    current = deepcopy(baseline)
    current.update(
        {
            "comparability_status": "structural-break",
            "denominator_definition": "all events",
            "dimensions": {"store_ids": ["store-b"]},
            "fact_as_of": "2026-07-17T10:00:00Z",
            "fact_set_generation": 8,
            "fact_set_id": "fact-set-8",
            "fact_set_manifest_sha256": "8" * 64,
            "mapping_bundle_id": "bundle-8",
            "mapping_bundle_sha256": "9" * 64,
            "metric_definition_versions": {"purchase_rate": "metric/4"},
            "period_boundary": "calendar-week:[start,end)",
            "target_label_version_id": "label-v8",
            "taxonomy_mode": "recomputed",
            "timezone": "UTC",
        }
    )

    result = compare_metric_snapshot_anchors(baseline, current)

    assert result["comparison_status"] == "structural-break"
    assert result["continuous_trend_allowed"] is False
    assert {
        "CURRENT_SNAPSHOT_NOT_COMPARABLE",
        "DENOMINATOR_DEFINITION_CHANGED",
        "DIMENSIONS_CHANGED",
        "FACT_AS_OF_NOT_MONOTONIC",
        "FACT_SET_GENERATION_CHANGED",
        "FACT_SET_ID_CHANGED",
        "FACT_SET_MANIFEST_CHANGED",
        "MAPPING_BUNDLE_ID_CHANGED",
        "MAPPING_BUNDLE_SHA256_CHANGED",
        "METRIC_DEFINITION_SET_CHANGED",
        "PERIOD_BOUNDARY_CHANGED",
        "TARGET_LABEL_VERSION_CHANGED",
        "TAXONOMY_MODE_CHANGED",
        "TIMEZONE_CHANGED",
    }.issubset(result["reason_codes"])


def test_different_date_windows_with_same_duration_remain_comparable() -> None:
    baseline = _anchor()
    current = deepcopy(baseline)
    baseline["time_range"] = "2026-05-01/2026-05-31"
    baseline["fact_as_of"] = "2026-06-01T00:00:00Z"
    baseline["time_window"] = {
        "kind": "explicit-date-range",
        "rule": {"duration_days": 31},
        "start": "2026-05-01",
        "end": "2026-05-31",
    }
    current["time_range"] = "2026-06-01/2026-07-01"
    current["time_window"] = {
        "kind": "explicit-date-range",
        "rule": {"duration_days": 31},
        "start": "2026-06-01",
        "end": "2026-07-01",
    }
    current["metric_result_id"] = "metric-current"
    current["fact_as_of"] = "2026-07-02T00:00:00Z"

    result = compare_metric_snapshot_anchors(baseline, current)

    assert result["comparison_status"] == "comparable"
    assert result["reason_codes"] == []


def test_different_window_duration_fails_closed() -> None:
    baseline = _anchor()
    current = deepcopy(baseline)
    baseline["time_window"] = {
        "kind": "explicit-date-range",
        "rule": {"duration_days": 31},
        "start": "2026-05-01",
        "end": "2026-05-31",
    }
    current["time_window"] = {
        "kind": "explicit-date-range",
        "rule": {"duration_days": 30},
        "start": "2026-06-01",
        "end": "2026-06-30",
    }

    result = compare_metric_snapshot_anchors(baseline, current)

    assert result["comparison_status"] == "structural-break"
    assert "TIME_WINDOW_RULE_CHANGED" in result["reason_codes"]


def test_non_numeric_outcome_never_allows_a_continuous_comparison() -> None:
    baseline = _anchor()
    current = deepcopy(baseline)
    current["metric_result_id"] = "metric-current-na"
    current["result_status"] = "zero-denominator"
    current["reason_codes"] = ["ZERO_DENOMINATOR"]

    result = compare_metric_snapshot_anchors(baseline, current)

    assert result["comparison_status"] == "structural-break"
    assert result["continuous_trend_allowed"] is False
    assert "CURRENT_RESULT_NOT_NUMERIC" in result["reason_codes"]
    assert result["current"]["result_status"] == "zero-denominator"
