from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.models import MetricResult, MetricResultLabelScope

COMPARISON_SCHEMA_VERSION = "auris.metric-snapshot-comparison/1"


def _iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _canonical_dimensions(payload: dict[str, Any]) -> dict[str, Any]:
    raw_scope = payload.get("scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    raw_dimensions = payload.get("dimensions")
    dimensions = dict(raw_dimensions) if isinstance(raw_dimensions, dict) else {}
    raw_stores = scope.get("store_ids", dimensions.get("store_ids", []))
    dimensions["store_ids"] = (
        sorted({str(item) for item in raw_stores}) if isinstance(raw_stores, list) else []
    )
    dimensions["model_version"] = scope.get("model_version") or payload.get("model_version")
    dimensions["label_version"] = scope.get("label_version") or payload.get("label_version")
    return {key: dimensions[key] for key in sorted(dimensions)}


def _time_window_descriptor(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    if raw in {"today", "7d", "30d", "90d"}:
        return {
            "kind": "rolling-window",
            "rule": raw,
            "start": None,
            "end": None,
        }
    parts = raw.split("/")
    if len(parts) == 2:
        try:
            start = date.fromisoformat(parts[0])
            end = date.fromisoformat(parts[1])
        except ValueError:
            pass
        else:
            if end >= start:
                return {
                    "kind": "explicit-date-range",
                    "rule": {"duration_days": (end - start).days + 1},
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
    return {
        "kind": "unverified",
        "rule": raw,
        "start": None,
        "end": None,
    }


def _generic_anchor(metric: MetricResult) -> dict[str, Any]:
    payload = metric.payload
    raw_scope = payload.get("scope")
    scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
    time_range = scope.get("time_range") or payload.get("time_range")
    return {
        "content_sha256": metric.content_sha256 or sha256_document(metric.payload),
        "definition_version": payload.get("definition_version"),
        "dimensions": _canonical_dimensions(payload),
        "metric_definition_versions": {
            str(payload.get("metric_key") or ""): str(payload.get("definition_version") or "")
        },
        "metric_key": payload.get("metric_key"),
        "metric_result_id": metric.metric_result_id,
        "reason_codes": list(payload.get("reason_codes") or []),
        "result_status": str(payload.get("result_status") or "value"),
        "scope_sha256": metric.scope_sha256 or sha256_document(scope),
        "source_manifest_sha256": metric.source_manifest_sha256
        or sha256_document(
            {
                "source_run_id": metric.payload.get("source_run_id"),
                "trace_id": metric.trace_id,
            }
        ),
        "time_range": time_range,
        "time_window": _time_window_descriptor(time_range),
        "unit": payload.get("unit"),
    }


def _label_anchor(
    metric: MetricResult,
    scope: MetricResultLabelScope,
) -> dict[str, Any]:
    expected_hashes = (
        scope.content_sha256,
        scope.scope_sha256,
        scope.source_manifest_sha256,
    )
    actual_hashes = (
        metric.content_sha256,
        metric.scope_sha256,
        metric.source_manifest_sha256,
    )
    if actual_hashes != expected_hashes:
        raise ApiError(
            "INSIGHT_METRIC_LABEL_SCOPE_DRIFT",
            "MetricResult 与强 LabelScope 哈希不一致，禁止比较漂移口径",
            409,
            details=[{"metric_result_id": metric.metric_result_id}],
        )
    return {
        **_generic_anchor(metric),
        "comparability_reason_codes": list(scope.comparability_reason_codes),
        "comparability_status": scope.comparability_status,
        "denominator_definition": scope.denominator_definition,
        "fact_as_of": _iso(scope.fact_as_of),
        "fact_as_of_rule": "current-not-before-baseline",
        "fact_namespace": scope.fact_namespace,
        "fact_set_generation": scope.fact_set_generation,
        "fact_set_id": scope.fact_set_id,
        "fact_set_manifest_sha256": scope.fact_set_manifest_sha256,
        "mapping_bundle_id": scope.mapping_bundle_id,
        "mapping_bundle_sha256": scope.mapping_bundle_sha256,
        "metric_definition_versions": dict(scope.metric_definition_versions),
        "period_boundary": scope.period_boundary,
        "source_label_version_ids": sorted(set(scope.source_label_version_ids)),
        "target_label_version_id": scope.target_label_version_id,
        "taxonomy_mode": scope.taxonomy_mode,
        "timezone": scope.timezone,
    }


def _add_difference(
    reasons: list[str],
    baseline: dict[str, Any],
    current: dict[str, Any],
    field: str,
    code: str,
) -> None:
    if baseline.get(field) != current.get(field):
        reasons.append(code)


def compare_metric_snapshot_anchors(
    baseline: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Compare two already verified immutable snapshot anchors.

    A single snapshot's Mapping-path status is deliberately insufficient.  A
    continuous line is allowed only when every governed cross-snapshot anchor
    remains identical and the fact cutoff is monotonic.
    """

    reasons: list[str] = []
    comparisons = (
        ("metric_key", "METRIC_KEY_CHANGED"),
        ("definition_version", "METRIC_DEFINITION_VERSION_CHANGED"),
        ("metric_definition_versions", "METRIC_DEFINITION_SET_CHANGED"),
        ("unit", "METRIC_UNIT_CHANGED"),
        ("dimensions", "DIMENSIONS_CHANGED"),
        ("taxonomy_mode", "TAXONOMY_MODE_CHANGED"),
        ("source_label_version_ids", "SOURCE_LABEL_VERSION_SET_CHANGED"),
        ("target_label_version_id", "TARGET_LABEL_VERSION_CHANGED"),
        ("mapping_bundle_id", "MAPPING_BUNDLE_ID_CHANGED"),
        ("mapping_bundle_sha256", "MAPPING_BUNDLE_SHA256_CHANGED"),
        ("fact_namespace", "FACT_NAMESPACE_CHANGED"),
        ("fact_set_id", "FACT_SET_ID_CHANGED"),
        ("fact_set_generation", "FACT_SET_GENERATION_CHANGED"),
        ("fact_set_manifest_sha256", "FACT_SET_MANIFEST_CHANGED"),
        ("fact_as_of_rule", "FACT_AS_OF_RULE_CHANGED"),
        ("timezone", "TIMEZONE_CHANGED"),
        ("period_boundary", "PERIOD_BOUNDARY_CHANGED"),
        ("denominator_definition", "DENOMINATOR_DEFINITION_CHANGED"),
    )
    for field, code in comparisons:
        _add_difference(reasons, baseline, current, field, code)

    baseline_window = baseline.get("time_window")
    current_window = current.get("time_window")
    if not isinstance(baseline_window, dict) or not isinstance(current_window, dict):
        reasons.append("TIME_WINDOW_RULE_UNVERIFIABLE")
    else:
        if (
            baseline_window.get("kind") != current_window.get("kind")
            or baseline_window.get("rule") != current_window.get("rule")
            or baseline_window.get("kind") == "unverified"
        ):
            reasons.append("TIME_WINDOW_RULE_CHANGED")
        baseline_start = baseline_window.get("start")
        baseline_end = baseline_window.get("end")
        current_start = current_window.get("start")
        current_end = current_window.get("end")
        if (
            isinstance(baseline_start, str)
            and isinstance(baseline_end, str)
            and isinstance(current_start, str)
            and isinstance(current_end, str)
            and (current_start < baseline_start or current_end < baseline_end)
        ):
            reasons.append("TIME_WINDOW_NOT_FORWARD")

    baseline_cutoff = baseline.get("fact_as_of")
    current_cutoff = current.get("fact_as_of")
    if isinstance(baseline_cutoff, str) and isinstance(current_cutoff, str):
        if current_cutoff < baseline_cutoff:
            reasons.append("FACT_AS_OF_NOT_MONOTONIC")
    elif baseline_cutoff is not None or current_cutoff is not None:
        reasons.append("FACT_AS_OF_RULE_UNVERIFIABLE")

    for role, anchor in (("BASELINE", baseline), ("CURRENT", current)):
        if anchor.get("result_status", "value") != "value":
            reasons.append(f"{role}_RESULT_NOT_NUMERIC")
        if anchor.get("comparability_status") not in (None, "comparable"):
            reasons.append(f"{role}_SNAPSHOT_NOT_COMPARABLE")

    reason_codes = sorted(set(reasons))
    status = "comparable" if not reason_codes else "structural-break"
    frozen = {
        "baseline": baseline,
        "comparison_status": status,
        "current": current,
        "reason_codes": reason_codes,
        "schema_version": COMPARISON_SCHEMA_VERSION,
    }
    comparison_sha256 = sha256_document(frozen)
    return {
        **frozen,
        "comparison_sha256": comparison_sha256,
        "continuous_trend_allowed": status == "comparable",
    }


def missing_baseline_comparison(
    current: MetricResult,
    current_scope: MetricResultLabelScope | None = None,
) -> dict[str, Any]:
    current_anchor = (
        _label_anchor(current, current_scope)
        if current_scope is not None
        else _generic_anchor(current)
    )
    frozen = {
        "baseline": None,
        "comparison_status": "structural-break",
        "current": current_anchor,
        "reason_codes": ["BASELINE_SNAPSHOT_MISSING"],
        "schema_version": COMPARISON_SCHEMA_VERSION,
    }
    return {
        **frozen,
        "comparison_sha256": sha256_document(frozen),
        "continuous_trend_allowed": False,
    }


def compare_metric_snapshots(
    session: Session,
    ctx: RequestContext,
    baseline_metric_result_id: str,
    current_metric_result_id: str,
) -> dict[str, Any]:
    if baseline_metric_result_id == current_metric_result_id:
        raise ApiError(
            "INSIGHT_METRIC_COMPARISON_DISTINCT_SNAPSHOTS_REQUIRED",
            "双快照比较必须引用两个不同的 MetricResult",
            422,
        )
    metrics = list(
        session.scalars(
            select(MetricResult).where(
                MetricResult.tenant_id == ctx.tenant_id,
                MetricResult.project_id == ctx.project_id,
                MetricResult.metric_result_id.in_(
                    (baseline_metric_result_id, current_metric_result_id)
                ),
            )
        )
    )
    by_id = {item.metric_result_id: item for item in metrics}
    missing = [
        item for item in (baseline_metric_result_id, current_metric_result_id) if item not in by_id
    ]
    if missing:
        raise ApiError(
            "INSIGHT_METRIC_COMPARISON_SNAPSHOT_NOT_FOUND",
            "当前租户项目中不存在待比较的 MetricResult",
            404,
            details=[{"metric_result_ids": missing}],
        )
    for metric in metrics:
        if (
            metric.status != "materialized"
            or metric.payload.get("immutable") is not True
            or metric.payload.get("snapshot_role") != "aggregation"
        ):
            raise ApiError(
                "INSIGHT_METRIC_COMPARISON_SNAPSHOT_INVALID",
                "双快照比较只接受不可变的已物化聚合 MetricResult",
                409,
                details=[{"metric_result_id": metric.metric_result_id}],
            )

    scopes = list(
        session.scalars(
            select(MetricResultLabelScope).where(
                MetricResultLabelScope.tenant_id == ctx.tenant_id,
                MetricResultLabelScope.project_id == ctx.project_id,
                MetricResultLabelScope.metric_result_id.in_(
                    (baseline_metric_result_id, current_metric_result_id)
                ),
            )
        )
    )
    scopes_by_id = {item.metric_result_id: item for item in scopes}
    baseline_metric = by_id[baseline_metric_result_id]
    current_metric = by_id[current_metric_result_id]
    baseline_required = baseline_metric.payload.get("label_version_applicability") == "required"
    current_required = current_metric.payload.get("label_version_applicability") == "required"
    if baseline_required != current_required:
        raise ApiError(
            "INSIGHT_METRIC_COMPARISON_APPLICABILITY_MISMATCH",
            "标签指标与非标签指标不能组成连续比较",
            409,
        )
    if baseline_required and (
        baseline_metric_result_id not in scopes_by_id
        or current_metric_result_id not in scopes_by_id
    ):
        raise ApiError(
            "INSIGHT_METRIC_LABEL_SCOPE_MISSING",
            "标签派生双快照缺少强 LabelScope，禁止比较",
            409,
        )

    baseline_anchor = (
        _label_anchor(baseline_metric, scopes_by_id[baseline_metric_result_id])
        if baseline_required
        else _generic_anchor(baseline_metric)
    )
    current_anchor = (
        _label_anchor(current_metric, scopes_by_id[current_metric_result_id])
        if current_required
        else _generic_anchor(current_metric)
    )
    return compare_metric_snapshot_anchors(baseline_anchor, current_anchor)
