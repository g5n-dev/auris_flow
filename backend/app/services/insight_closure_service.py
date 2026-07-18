from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.response import envelope
from app.models import (
    InsightAction,
    InsightEffect,
    InsightExperiment,
    InsightReport,
    JsonResource,
    MetricResult,
    MetricResultLabelScope,
    ProjectSceneProfileBinding,
    RunRecord,
    SceneProfileVersion,
)
from app.schemas.insights import (
    InsightActionRequest,
    InsightExperimentRequest,
    InsightExperimentRetryAttemptRequest,
    InsightMetricAggregationResult,
    InsightMetricRunRequest,
    InsightReportArtifactResult,
    InsightReportDocument,
    InsightReportRequest,
)
from app.schemas.scene_profiles import SceneProfileManifest
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

BRANCH_GOVERNANCE_LEVEL = {"experiment": 0, "human_review": 1}
REPORT_SECTION_TITLES = {
    "north_star": "核心指标快照",
    "risk_root_cause": "风险与证据",
    "next_actions": "后续行动依据",
}


def _scene_metric_catalog(
    session: Session,
    ctx: RequestContext,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    binding = session.scalar(
        select(ProjectSceneProfileBinding).where(
            ProjectSceneProfileBinding.tenant_id == ctx.tenant_id,
            ProjectSceneProfileBinding.project_id == ctx.project_id,
            ProjectSceneProfileBinding.environment == "production",
            ProjectSceneProfileBinding.status == "active",
        )
    )
    if binding is None:
        raise ApiError(
            "SCENE_PROFILE_BINDING_REQUIRED",
            "洞察运行必须绑定已发布场景版本",
            409,
        )
    version = session.scalar(
        select(SceneProfileVersion).where(
            SceneProfileVersion.tenant_id == ctx.tenant_id,
            SceneProfileVersion.project_id == ctx.project_id,
            SceneProfileVersion.scene_profile_version_id == binding.scene_profile_version_id,
            SceneProfileVersion.status == "published",
        )
    )
    if version is None or version.manifest_sha256 != binding.manifest_sha256:
        raise ApiError("SCENE_PROFILE_BINDING_DRIFT", "项目场景绑定已漂移", 409)
    manifest = SceneProfileManifest.model_validate(version.manifest)
    catalog = {
        metric.metric_key: {
            "label": metric.display_name,
            "unit": metric.unit,
            "formula": metric.formula or metric.calculator_ref,
            "owner": metric.owner or manifest.display_name,
            "calculator_ref": metric.calculator_ref,
            "metric_family": metric.metric_family,
            "label_version_applicability": metric.label_version_applicability,
            "evidence_refs": list(metric.evidence_refs),
            "risk_level": metric.risk_level,
            "human_review_required": metric.human_review_required,
        }
        for metric in manifest.metrics
    }
    return catalog, {
        "scene_profile_id": binding.scene_profile_id,
        "scene_profile_version_id": binding.scene_profile_version_id,
        "scene_profile_snapshot_sha256": binding.manifest_sha256,
        "scene_key": manifest.scene_key,
    }


def _canonical_sha256(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


_SCENE_LOCK_FIELDS = (
    "scene_profile_id",
    "scene_profile_version_id",
    "scene_profile_snapshot_sha256",
)


def _scene_lock(payload: dict[str, Any], *, object_ref: str) -> dict[str, str]:
    lock = {field: str(payload.get(field) or "") for field in _SCENE_LOCK_FIELDS}
    missing = [field for field, value in lock.items() if not value]
    if missing:
        raise ApiError(
            "INSIGHT_SCENE_PROFILE_LOCK_MISSING",
            "洞察对象缺少冻结的场景 Profile 三元组",
            409,
            details=[{"object_ref": object_ref, "fields": missing}],
        )
    if len(lock["scene_profile_snapshot_sha256"]) != 64:
        raise ApiError(
            "INSIGHT_SCENE_PROFILE_LOCK_INVALID",
            "洞察对象的场景快照摘要无效",
            409,
            details=[{"object_ref": object_ref}],
        )
    return lock


def _require_same_scene_lock(
    expected: dict[str, str],
    actual: dict[str, str],
    *,
    object_ref: str,
) -> None:
    if expected != actual:
        raise ApiError(
            "INSIGHT_SCENE_PROFILE_MISMATCH",
            "洞察链路不能混用不同 SceneProfile 快照",
            409,
            details=[
                {
                    "object_ref": object_ref,
                    "expected": expected,
                    "actual": actual,
                }
            ],
        )


def _assert_scene_snapshot(
    session: Session,
    ctx: RequestContext,
    scene_lock: dict[str, str],
) -> None:
    # Local import avoids run_service -> insight -> scene -> run_service at module load.
    from app.services.scene_profile_service import assert_scene_profile_snapshot

    assert_scene_profile_snapshot(session, ctx, **scene_lock)


def rebind_report_document(
    raw_document: dict[str, Any],
    *,
    run_id: str,
    trace_id: str,
) -> dict[str, Any]:
    document = InsightReportDocument.model_validate(raw_document).model_copy(
        update={"run_id": run_id, "trace_id": trace_id}
    )
    return InsightReportDocument.model_validate(document).model_dump(mode="json")


def _scoped(
    session: Session,
    model: Any,
    identifier: str,
    id_field: Any,
    ctx: RequestContext,
) -> Any:
    item = session.scalar(
        select(model).where(
            id_field == identifier,
            model.tenant_id == ctx.tenant_id,
            model.project_id == ctx.project_id,
        )
    )
    if item is None:
        raise ApiError("NOT_FOUND", f"对象不存在：{identifier}", 404)
    return item


def _datetime_iso(value: datetime) -> str:
    normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _label_scope_payload(scope: MetricResultLabelScope) -> dict[str, Any]:
    return {
        "taxonomy_mode": scope.taxonomy_mode,
        "source_label_version_ids": list(scope.source_label_version_ids),
        "target_label_version_id": scope.target_label_version_id,
        "mapping_bundle_id": scope.mapping_bundle_id,
        "fact_set_generation": scope.fact_set_generation,
        "fact_as_of": _datetime_iso(scope.fact_as_of),
        "metric_definition_versions": dict(scope.metric_definition_versions),
        "timezone": scope.timezone,
        "period_boundary": scope.period_boundary,
        "denominator_definition": scope.denominator_definition,
    }


def _metric_label_scopes(
    session: Session,
    ctx: RequestContext,
    metric_result_ids: list[str],
) -> dict[str, MetricResultLabelScope]:
    if not metric_result_ids:
        return {}
    scopes = session.scalars(
        select(MetricResultLabelScope).where(
            MetricResultLabelScope.tenant_id == ctx.tenant_id,
            MetricResultLabelScope.project_id == ctx.project_id,
            MetricResultLabelScope.metric_result_id.in_(metric_result_ids),
        )
    ).all()
    return {scope.metric_result_id: scope for scope in scopes}


def metric_payload(
    metric: MetricResult,
    label_scope: MetricResultLabelScope | None = None,
) -> dict[str, Any]:
    payload = {
        **metric.payload,
        "id": metric.metric_result_id,
        "metric_result_id": metric.metric_result_id,
        "status": metric.status,
        "trace_id": metric.trace_id,
    }
    label_required = metric.payload.get("label_version_applicability") == "required"
    if label_required and label_scope is None:
        raise ApiError(
            "INSIGHT_METRIC_LABEL_SCOPE_MISSING",
            "标签派生 MetricResult 缺少强 LabelScope，禁止返回不完整统计口径",
            409,
            details=[{"metric_result_id": metric.metric_result_id}],
        )
    if label_scope is None:
        return payload
    expected_hashes = (
        label_scope.content_sha256,
        label_scope.scope_sha256,
        label_scope.source_manifest_sha256,
    )
    actual_hashes = (
        metric.content_sha256,
        metric.scope_sha256,
        metric.source_manifest_sha256,
    )
    if actual_hashes != expected_hashes:
        raise ApiError(
            "INSIGHT_METRIC_LABEL_SCOPE_DRIFT",
            "MetricResult 与强 LabelScope 哈希不一致，禁止返回漂移口径",
            409,
            details=[{"metric_result_id": metric.metric_result_id}],
        )
    scope_payload = _label_scope_payload(label_scope)
    raw_scope = payload.get("scope")
    metric_scope = dict(raw_scope) if isinstance(raw_scope, dict) else {}
    metric_scope.update(
        {
            "label_version_applicability": "required",
            "label_scope": scope_payload,
        }
    )
    return {
        **payload,
        "scope": metric_scope,
        "label_version_applicability": "required",
        "label_scope": scope_payload,
        "comparability_status": label_scope.comparability_status,
        "comparability_reason_codes": list(label_scope.comparability_reason_codes),
        "scope_sha256": label_scope.scope_sha256,
        "source_manifest_sha256": label_scope.source_manifest_sha256,
        "content_sha256": label_scope.content_sha256,
    }


def report_payload(report: InsightReport) -> dict[str, Any]:
    return {
        **report.payload,
        "id": report.report_id,
        "report_id": report.report_id,
        "run_id": report.run_id,
        "report_type": report.report_type,
        "status": report.status,
        "trace_id": report.trace_id,
    }


def report_detail_payload(
    session: Session, ctx: RequestContext, report: InsightReport
) -> dict[str, Any]:
    metric_result_ids = list(report.payload.get("metric_result_ids") or [])
    snapshots = session.scalars(
        select(MetricResult).where(
            MetricResult.tenant_id == ctx.tenant_id,
            MetricResult.project_id == ctx.project_id,
            MetricResult.metric_result_id.in_(metric_result_ids),
        )
    ).all()
    by_id = {item.metric_result_id: item for item in snapshots}
    missing = [item for item in metric_result_ids if item not in by_id]
    if missing:
        raise ApiError(
            "INSIGHT_METRIC_CAUSAL_CHAIN_BROKEN",
            "报告引用的指标结果已缺失，禁止静默返回不完整报告",
            409,
            details=[{"report_id": report.report_id, "metric_result_ids": missing}],
        )
    ordered = [by_id[item] for item in metric_result_ids]
    label_scopes = _metric_label_scopes(session, ctx, metric_result_ids)
    from app.services.insight_report_metric_binding_service import (
        verify_insight_report_metric_binding,
    )

    metric_binding = verify_insight_report_metric_binding(session, report, ordered)
    return {
        **report_payload(report),
        "metric_results": [
            metric_payload(item, label_scopes.get(item.metric_result_id)) for item in ordered
        ],
        "metric_scope_sha256": metric_binding["metric_scope_sha256"],
        "report_metric_binding_content_sha256": metric_binding["content_sha256"],
        "report_metric_binding_id": metric_binding["report_metric_binding_id"],
    }


def action_payload(action: InsightAction) -> dict[str, Any]:
    return {
        **action.payload,
        "id": action.action_id,
        "action_id": action.action_id,
        "report_id": action.report_id,
        "metric_result_id": action.baseline_metric_result_id,
        "action_type": action.action_type,
        "branch": action.branch,
        "risk_level": action.risk_level,
        "status": action.status,
        "review_task_id": action.review_task_id,
        "resource_version": action.resource_version,
        "trace_id": action.trace_id,
    }


def experiment_payload(experiment: InsightExperiment) -> dict[str, Any]:
    return {
        **experiment.payload,
        "id": experiment.experiment_id,
        "experiment_id": experiment.experiment_id,
        "action_id": experiment.action_id,
        "eval_run_id": experiment.eval_run_id,
        "baseline_metric_result_id": experiment.baseline_metric_result_id,
        "outcome_metric_result_id": experiment.outcome_metric_result_id,
        "status": experiment.status,
        "trace_id": experiment.trace_id,
    }


def effect_payload(effect: InsightEffect) -> dict[str, Any]:
    return {
        **effect.payload,
        "id": effect.effect_id,
        "effect_id": effect.effect_id,
        "action_id": effect.action_id,
        "experiment_id": effect.experiment_id,
        "baseline_metric_result_id": effect.baseline_metric_result_id,
        "outcome_metric_result_id": effect.outcome_metric_result_id,
        "metric_key": effect.metric_key,
        "delta": effect.delta,
        "confidence_low": effect.confidence_low,
        "confidence_high": effect.confidence_high,
        "status": effect.status,
        "trace_id": effect.trace_id,
    }


def _label_scope_matches_filters(
    payload: dict[str, Any],
    label_scope: MetricResultLabelScope | None,
    *,
    label_version_applicability: str | None,
    taxonomy_mode: str | None,
    source_label_version_ids: list[str] | None,
    target_label_version_id: str | None,
    mapping_bundle_id: str | None,
    fact_set_generation: int | None,
    fact_as_of: datetime | None,
) -> bool:
    actual_applicability = (
        "required"
        if label_scope is not None
        else str(payload.get("label_version_applicability") or "none")
    )
    if (
        label_version_applicability is not None
        and actual_applicability != label_version_applicability
    ):
        return False

    strong_scope_filter_requested = any(
        value is not None
        for value in (
            taxonomy_mode,
            source_label_version_ids,
            target_label_version_id,
            mapping_bundle_id,
            fact_set_generation,
            fact_as_of,
        )
    )
    if label_scope is None:
        return not strong_scope_filter_requested
    if taxonomy_mode is not None and label_scope.taxonomy_mode != taxonomy_mode:
        return False
    if source_label_version_ids is not None:
        expected_sources = sorted(
            {value.strip() for value in source_label_version_ids if value.strip()}
        )
        if sorted(set(label_scope.source_label_version_ids)) != expected_sources:
            return False
    if (
        target_label_version_id is not None
        and label_scope.target_label_version_id != target_label_version_id.strip()
    ):
        return False
    if mapping_bundle_id is not None and label_scope.mapping_bundle_id != mapping_bundle_id.strip():
        return False
    if fact_set_generation is not None and label_scope.fact_set_generation != fact_set_generation:
        return False
    return fact_as_of is None or _datetime_iso(label_scope.fact_as_of) == _datetime_iso(fact_as_of)


def current_metric_payloads(
    session: Session,
    ctx: RequestContext,
    *,
    time_range: str = "30d",
    store_id: str | None = None,
    label_version: str | None = None,
    label_version_applicability: str | None = None,
    taxonomy_mode: str | None = None,
    source_label_version_ids: list[str] | None = None,
    target_label_version_id: str | None = None,
    mapping_bundle_id: str | None = None,
    fact_set_generation: int | None = None,
    fact_as_of: datetime | None = None,
    model_version: str | None = None,
) -> list[dict[str, Any]]:
    materialized = session.scalars(
        select(MetricResult)
        .where(
            MetricResult.tenant_id == ctx.tenant_id,
            MetricResult.project_id == ctx.project_id,
            MetricResult.status == "materialized",
        )
        .order_by(MetricResult.created_at.desc())
    ).all()
    label_scopes = _metric_label_scopes(
        session,
        ctx,
        [item.metric_result_id for item in materialized],
    )
    items: list[dict[str, Any]] = []
    for result in materialized:
        payload = result.payload
        if payload.get("snapshot_role") != "aggregation":
            continue
        scope = _metric_scope(payload)
        stores = scope["store_ids"]
        if scope["time_range"] != time_range:
            continue
        if store_id and store_id not in stores:
            continue
        label_scope = label_scopes.get(result.metric_result_id)
        if not _label_scope_matches_filters(
            payload,
            label_scope,
            label_version_applicability=label_version_applicability,
            taxonomy_mode=taxonomy_mode,
            source_label_version_ids=source_label_version_ids,
            target_label_version_id=target_label_version_id,
            mapping_bundle_id=mapping_bundle_id,
            fact_set_generation=fact_set_generation,
            fact_as_of=fact_as_of,
        ):
            continue
        if label_version:
            if label_scope is not None:
                scoped_versions = set(label_scope.source_label_version_ids)
                if label_scope.target_label_version_id:
                    scoped_versions.add(label_scope.target_label_version_id)
                if label_version not in scoped_versions:
                    continue
            elif scope["label_version"] != label_version:
                continue
        if model_version and scope["model_version"] != model_version:
            continue
        items.append(metric_payload(result, label_scope))
    return items


def _normalized_scope(
    *,
    time_range: str,
    store_ids: list[str],
    model_version: str | None,
    label_version: str | None,
) -> dict[str, Any]:
    return {
        "time_range": time_range.strip(),
        "store_ids": sorted(set(store_ids)),
        "model_version": model_version or None,
        "label_version": label_version or None,
    }


def _metric_scope(payload: dict[str, Any]) -> dict[str, Any]:
    raw_scope = payload.get("scope")
    scope = raw_scope if isinstance(raw_scope, dict) else {}
    dimensions = payload.get("dimensions")
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    raw_stores = scope.get("store_ids", dimensions.get("store_ids", []))
    stores = [str(item) for item in raw_stores] if isinstance(raw_stores, list) else []
    return _normalized_scope(
        time_range=str(scope.get("time_range") or payload.get("time_range") or ""),
        store_ids=stores,
        model_version=scope.get("model_version") or payload.get("model_version"),
        label_version=scope.get("label_version") or payload.get("label_version"),
    )


def _request_scope(body: InsightMetricRunRequest | InsightReportRequest) -> dict[str, Any]:
    return _normalized_scope(
        time_range=str(body.time_range or ""),
        store_ids=body.store_ids,
        model_version=body.model_version,
        label_version=body.label_version,
    )


def _validate_metric_keys(
    metric_keys: list[str],
    catalog: dict[str, dict[str, Any]],
) -> None:
    unknown = [item for item in metric_keys if item not in catalog]
    if unknown:
        raise ApiError(
            "INSIGHT_METRIC_UNKNOWN",
            "请求包含未注册的洞察指标",
            422,
            details=[{"metric_keys": unknown}],
        )


async def create_insight_metric_run(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: InsightMetricRunRequest,
) -> dict[str, Any]:
    from app.services.run_service import create_run

    catalog, scene_snapshot = _scene_metric_catalog(session, ctx)
    _validate_metric_keys(body.metric_keys, catalog)
    scope = _request_scope(body)
    definitions = [
        {"metric_key": metric_key, **catalog[metric_key]} for metric_key in body.metric_keys
    ]
    label_definitions = [
        item for item in definitions if item["label_version_applicability"] == "required"
    ]
    if label_definitions and body.label_scope is None:
        raise ApiError(
            "INSIGHT_LABEL_SCOPE_REQUIRED",
            "标签派生指标必须提交显式 native/normalized/recomputed scope",
            422,
            details=[{"metric_keys": [str(item["metric_key"]) for item in label_definitions]}],
        )
    if body.label_scope is not None and not label_definitions:
        raise ApiError(
            "INSIGHT_LABEL_SCOPE_NOT_APPLICABLE",
            "当前指标目录未声明标签版本适用性，不能附加标签 scope",
            422,
        )
    label_scope_lock: dict[str, Any] | None = None
    if body.label_scope is not None:
        from app.services.label_metric_scope_service import lock_label_metric_run_scope

        label_scope_lock = lock_label_metric_run_scope(
            session,
            ctx,
            body.label_scope,
            label_definitions,
        )
    return await create_run(
        session,
        ctx,
        request,
        run_type="insight_metric_aggregation",
        event_type="insight_metric_aggregation.requested",
        payload={
            "metric_keys": body.metric_keys,
            "metric_definitions": definitions,
            "label_scope": (body.label_scope.model_dump(mode="json") if body.label_scope else None),
            "label_scope_lock": label_scope_lock,
            **scene_snapshot,
            "metric_scope": scope,
            "time_range": scope["time_range"],
            "store_ids": scope["store_ids"],
            "model_version": scope["model_version"],
            "label_version": scope["label_version"],
            "source": body.source,
            "job_name": "insight_metric_aggregation_job",
            "object_type": "metric_aggregation",
            "affected_objects": [
                {"type": "metric_definition", "id": item} for item in body.metric_keys
            ],
            "next_actions": [
                {"key": "view_run", "label": "查看聚合运行"},
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
            ],
        },
        status="pending",
        idempotency_operation="insights.metric-runs.create",
    )


def _resolve_evidence_refs(
    session: Session, ctx: RequestContext, refs: list[str]
) -> tuple[list[str], list[str]]:
    if not refs:
        return [], []
    resources = session.scalars(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection.in_(("evidence_packs", "documents")),
            JsonResource.resource_key.in_(refs),
        )
    ).all()
    found = {item.resource_key: item.collection for item in resources}
    missing = [item for item in refs if item not in found]
    if missing:
        raise ApiError(
            "INSIGHT_EVIDENCE_NOT_FOUND",
            "洞察报告或动作引用了不存在的项目证据",
            422,
            details=[{"evidence_refs": missing}],
        )
    return refs, [item for item in refs if found[item] == "evidence_packs"]


def _load_evidence_resources(
    session: Session,
    ctx: RequestContext,
    refs: list[str],
) -> list[JsonResource]:
    if not refs:
        return []
    resources = session.scalars(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection.in_(("evidence_packs", "documents")),
            JsonResource.resource_key.in_(refs),
        )
    ).all()
    by_ref = {item.resource_key: item for item in resources}
    return [by_ref[item] for item in refs]


def _evidence_summary(resource: JsonResource) -> str:
    data = resource.data
    title = str(data.get("title") or data.get("name") or resource.resource_key)
    if resource.collection == "evidence_packs":
        details = [f"证据包 {title}"]
        audio_session_id = data.get("audio_session_id")
        if audio_session_id:
            details.append(f"会话 {audio_session_id}")
        start_ms = data.get("window_start_ms")
        end_ms = data.get("window_end_ms")
        if isinstance(start_ms, int) and isinstance(end_ms, int):
            details.append(f"冻结窗口 {start_ms}-{end_ms}ms")
        status = resource.status or data.get("status")
        if status:
            details.append(f"状态 {status}")
        return "；".join(details)
    summary = data.get("summary") or data.get("description") or data.get("content")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:2000]
    return f"文档 {title}；已按项目内权威资源版本冻结"


def _build_report_document(
    *,
    ctx: RequestContext,
    run_id: str,
    report_id: str,
    body: InsightReportRequest,
    metric_results: list[MetricResult],
    evidence_resources: list[JsonResource],
) -> dict[str, Any]:
    metric_snapshots = []
    for metric in metric_results:
        payload = metric.payload
        metric_snapshots.append(
            {
                "metric_result_id": metric.metric_result_id,
                "metric_key": str(payload["metric_key"]),
                "label": str(payload.get("label") or payload["metric_key"]),
                "value": payload["value"],
                "unit": str(payload["unit"]),
                "sample_size": payload["sample_size"],
                "definition_version": str(payload["definition_version"]),
                "scope": _metric_scope(payload),
                "source_run_id": str(payload["source_run_id"]),
                "trace_id": str(metric.trace_id or payload.get("trace_id") or ctx.trace_id),
                "payload_sha256": _canonical_sha256(payload),
            }
        )

    evidence_snapshots = [
        {
            "evidence_ref": resource.resource_key,
            "source_collection": resource.collection,
            "title": str(
                resource.data.get("title") or resource.data.get("name") or resource.resource_key
            ),
            "status": resource.status or resource.data.get("status"),
            "summary": _evidence_summary(resource),
            "trace_id": resource.trace_id or resource.data.get("trace_id"),
            "source_sha256": _canonical_sha256(resource.data),
        }
        for resource in evidence_resources
    ]
    metric_ids = [item["metric_result_id"] for item in metric_snapshots]
    evidence_refs = [item["evidence_ref"] for item in evidence_snapshots]
    metric_text = "，".join(
        f"{item['label']}={item['value']} {item['unit']}（样本 {item['sample_size']}）"
        for item in metric_snapshots
    )
    evidence_text = "；".join(str(item["summary"]) for item in evidence_snapshots)
    requested_sections = body.report_sections or ["metric_snapshot"]
    sections = []
    for order, section_id in enumerate(requested_sections, start=1):
        title = REPORT_SECTION_TITLES.get(section_id, section_id.replace("_", " "))
        if section_id == "risk_root_cause" and evidence_text:
            summary = (
                f"本节仅固化可追溯证据，不推断未验证根因：{evidence_text}。"
                f"关联指标：{metric_text}。"
            )
        elif section_id == "next_actions":
            summary = f"后续动作必须以冻结指标 {metric_text} 为基线，并引用本报告的证据快照。"
        else:
            summary = f"冻结范围 {body.time_range} 的真实物化指标：{metric_text}。"
        sections.append(
            {
                "section_id": section_id,
                "section_version": 1,
                "order": order,
                "title": title,
                "summary": summary,
                "metric_result_ids": metric_ids,
                "evidence_refs": evidence_refs,
            }
        )

    document = {
        "schema_version": "auris.insight-report.v2",
        "document_version": 1,
        "artifact_state": "materialized",
        "report_id": report_id,
        "run_id": run_id,
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "trace_id": ctx.trace_id,
        "title": body.title,
        "report_type": body.report_type,
        "time_range": str(body.time_range),
        "owner": body.owner,
        "metric_results": metric_snapshots,
        "evidence": evidence_snapshots,
        "sections": sections,
        "generator_proof": {
            "proof_type": "bff-governed-snapshot",
            "generator_id": "auris-flow-bff",
            "generator_version": "insight-report-v2",
            "generation_mode": "deterministic-governed-snapshot",
            "metric_snapshot_sha256": _canonical_sha256(metric_snapshots),
            "evidence_snapshot_sha256": _canonical_sha256(evidence_snapshots),
            "section_manifest_sha256": _canonical_sha256(sections),
        },
    }
    return InsightReportDocument.model_validate(document).model_dump(mode="json")


def _load_report_metric_results(
    session: Session,
    ctx: RequestContext,
    *,
    body: InsightReportRequest,
) -> tuple[list[MetricResult], dict[str, str]]:
    rows = session.scalars(
        select(MetricResult).where(
            MetricResult.tenant_id == ctx.tenant_id,
            MetricResult.project_id == ctx.project_id,
            MetricResult.metric_result_id.in_(body.metric_result_ids),
        )
    ).all()
    by_id = {item.metric_result_id: item for item in rows}
    missing = [item for item in body.metric_result_ids if item not in by_id]
    if missing:
        raise ApiError(
            "INSIGHT_METRIC_RESULT_NOT_FOUND",
            "报告引用了不存在或不可访问的指标结果",
            422,
            details=[{"metric_result_ids": missing}],
        )
    ordered = [by_id[item] for item in body.metric_result_ids]
    expected_scope = _request_scope(body)
    source_run_ids: set[str] = set()
    metric_scene_locks: dict[str, dict[str, str]] = {}
    for metric in ordered:
        payload = metric.payload
        metric_scene_locks[metric.metric_result_id] = _scene_lock(
            payload,
            object_ref=f"metric_result:{metric.metric_result_id}",
        )
        source_run_id = payload.get("source_run_id")
        if (
            metric.status != "materialized"
            or payload.get("snapshot_role") != "aggregation"
            or payload.get("immutable") is not True
            or not isinstance(source_run_id, str)
        ):
            raise ApiError(
                "INSIGHT_METRIC_RESULT_NOT_MATERIALIZED",
                "报告只能引用已成功物化的聚合指标结果",
                409,
                details=[
                    {
                        "metric_result_id": metric.metric_result_id,
                        "status": metric.status,
                        "snapshot_role": payload.get("snapshot_role"),
                    }
                ],
            )
        actual_scope = _metric_scope(payload)
        if actual_scope != expected_scope:
            raise ApiError(
                "INSIGHT_METRIC_SCOPE_MISMATCH",
                "报告范围必须与所有指标结果的冻结范围完全一致",
                409,
                details=[
                    {
                        "metric_result_id": metric.metric_result_id,
                        "expected_scope": expected_scope,
                        "actual_scope": actual_scope,
                    }
                ],
            )
        source_run_ids.add(source_run_id)

    scene_lock = metric_scene_locks[ordered[0].metric_result_id]
    for metric_result_id, candidate_lock in metric_scene_locks.items():
        _require_same_scene_lock(
            scene_lock,
            candidate_lock,
            object_ref=f"metric_result:{metric_result_id}",
        )
    _assert_scene_snapshot(session, ctx, scene_lock)

    source_runs = session.scalars(
        select(RunRecord).where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_id.in_(source_run_ids),
        )
    ).all()
    source_runs_by_id = {item.run_id: item for item in source_runs}
    successful_runs = {
        item.run_id
        for item in source_runs
        if item.run_type == "insight_metric_aggregation" and item.status == "success"
    }
    invalid_sources = sorted(source_run_ids - successful_runs)
    if invalid_sources:
        raise ApiError(
            "INSIGHT_METRIC_SOURCE_RUN_INVALID",
            "指标结果的聚合来源运行不存在或未成功",
            409,
            details=[{"source_run_ids": invalid_sources}],
        )
    for metric in ordered:
        source_run_id = str(metric.payload["source_run_id"])
        source_run = source_runs_by_id[source_run_id]
        _require_same_scene_lock(
            metric_scene_locks[metric.metric_result_id],
            _scene_lock(
                source_run.payload,
                object_ref=f"run:{source_run_id}",
            ),
            object_ref=f"metric_result:{metric.metric_result_id}/source_run:{source_run_id}",
        )
    return ordered, scene_lock


async def create_insight_report(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: InsightReportRequest,
) -> dict[str, Any]:
    from app.services.run_service import create_run

    operation = "insights.reports.create"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    report_id = body.report_id or f"insight_report_{uuid.uuid4().hex[:12]}"
    evidence_refs, evidence_pack_ids = _resolve_evidence_refs(session, ctx, body.evidence_refs)
    evidence_resources = _load_evidence_resources(session, ctx, evidence_refs)
    metric_results, scene_lock = _load_report_metric_results(session, ctx, body=body)
    metric_result_ids = [item.metric_result_id for item in metric_results]
    metric_refs = [str(item.payload["metric_key"]) for item in metric_results]

    def prepare(record: RunRecord) -> None:
        if session.get(InsightReport, report_id) is not None:
            raise ApiError("INSIGHT_REPORT_ALREADY_EXISTS", "报告 ID 已存在", 409)
        report_document = _build_report_document(
            ctx=ctx,
            run_id=record.run_id,
            report_id=report_id,
            body=body,
            metric_results=metric_results,
            evidence_resources=evidence_resources,
        )
        data = {
            "id": report_id,
            "report_id": report_id,
            "run_id": record.run_id,
            "title": body.title,
            "report_type": body.report_type,
            "status": "generating",
            "time_range": body.time_range,
            "range": body.time_range,
            "owner": body.owner,
            "metric_refs": metric_refs,
            "metric_result_ids": metric_result_ids,
            "evidence_refs": evidence_refs,
            "evidence_pack_ids": evidence_pack_ids,
            "report_sections": body.report_sections,
            "store_ids": body.store_ids,
            "model_version": body.model_version,
            "label_version": body.label_version,
            "metric_scope": _request_scope(body),
            **scene_lock,
            "report_document": report_document,
            "asset_ref": record.payload.get("asset_ref"),
            "trace_id": ctx.trace_id,
        }
        report = InsightReport(
            report_id=report_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_id=record.run_id,
            status="generating",
            report_type=body.report_type,
            trace_id=ctx.trace_id,
            payload=data,
        )
        session.add(report)
        session.flush()
        from app.services.insight_report_metric_binding_service import (
            bind_insight_report_metrics,
        )

        metric_binding = bind_insight_report_metrics(
            session,
            ctx,
            report,
            metric_results,
        )
        data = {
            **data,
            "metric_scope_sha256": metric_binding["metric_scope_sha256"],
            "report_metric_binding_content_sha256": metric_binding["content_sha256"],
            "report_metric_binding_id": metric_binding["report_metric_binding_id"],
        }
        report.payload = data
        upsert_resource(
            session,
            ctx,
            "insight_reports",
            report_id,
            data,
            status="generating",
            trace_id=ctx.trace_id,
            audit_action="insight_report.create",
        )
        record.payload = {
            **record.payload,
            **scene_lock,
            "metric_refs": metric_refs,
            "metric_result_ids": metric_result_ids,
            "metric_scope_sha256": metric_binding["metric_scope_sha256"],
            "report_metric_binding_content_sha256": metric_binding["content_sha256"],
            "report_metric_binding_id": metric_binding["report_metric_binding_id"],
            "evidence_refs": evidence_refs,
            "evidence_pack_ids": evidence_pack_ids,
            "report_document": report_document,
            "affected_objects": [
                {"type": "insight_report", "id": report_id},
                *[{"type": "metric_result", "id": item} for item in metric_result_ids],
            ],
            "next_actions": [
                {
                    "key": "view_report",
                    "label": "查看报告",
                    "route": f"insights/reports/{report_id}",
                },
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
            ],
        }

    return await create_run(
        session,
        ctx,
        request,
        run_type="insight_report",
        event_type="export.requested",
        payload={
            **body.model_dump(exclude_none=True),
            **scene_lock,
            "report_id": report_id,
            "report_type": body.report_type,
            "asset_ref": f"auris/reports/{report_id}",
            "content_type": "application/json",
            "object_id": report_id,
            "object_type": "insight_report",
        },
        status="pending",
        idempotency_operation=operation,
        prepare_record=prepare,
    )


def get_insight_report(session: Session, ctx: RequestContext, report_id: str) -> InsightReport:
    return _scoped(session, InsightReport, report_id, InsightReport.report_id, ctx)


def get_insight_action(session: Session, ctx: RequestContext, action_id: str) -> InsightAction:
    return _scoped(session, InsightAction, action_id, InsightAction.action_id, ctx)


def _minimum_review_branch(
    *,
    risk_level: str,
    metric_definition: dict[str, Any],
) -> str:
    governed_risk = str(metric_definition.get("risk_level") or "low")
    human_review_required = metric_definition.get("human_review_required") is True
    if (
        risk_level in {"high", "critical"}
        or governed_risk in {"high", "critical"}
        or human_review_required
    ):
        return "human_review"
    return "experiment"


def _review_branch(
    body: InsightActionRequest,
    metric_definition: dict[str, Any],
) -> tuple[str, str]:
    minimum_branch = _minimum_review_branch(
        risk_level=body.risk_level,
        metric_definition=metric_definition,
    )
    requested_branch = minimum_branch if body.branch == "auto" else body.branch
    effective_branch = max(
        (minimum_branch, requested_branch),
        key=BRANCH_GOVERNANCE_LEVEL.__getitem__,
    )
    return effective_branch, minimum_branch


async def create_insight_action(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: InsightActionRequest,
) -> dict[str, Any]:
    body_hash = await request_hash(request)
    operation = "insights.actions.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    report = get_insight_report(session, ctx, body.report_id)
    if report.status != "generated":
        raise ApiError(
            "INSIGHT_REPORT_NOT_GENERATED",
            "只有已生成并冻结结果的报告才能创建动作",
            409,
            details=[{"report_id": report.report_id, "status": report.status}],
        )
    if body.metric_result_id not in set(report.payload.get("metric_result_ids") or []):
        raise ApiError(
            "INSIGHT_METRIC_NOT_IN_REPORT",
            "动作必须引用报告中固化的指标快照",
            409,
            details=[{"report_id": report.report_id, "metric_result_id": body.metric_result_id}],
        )
    metric = _scoped(
        session,
        MetricResult,
        body.metric_result_id,
        MetricResult.metric_result_id,
        ctx,
    )
    metric_key = str(metric.payload.get("metric_key") or "")
    if body.metric_key and body.metric_key != metric_key:
        raise ApiError("INSIGHT_METRIC_KEY_MISMATCH", "指标 key 与快照不一致", 409)
    report_scene_lock = _scene_lock(
        report.payload,
        object_ref=f"insight_report:{report.report_id}",
    )
    _require_same_scene_lock(
        report_scene_lock,
        _scene_lock(metric.payload, object_ref=f"metric_result:{metric.metric_result_id}"),
        object_ref=f"insight_action:{body.action_id or 'new'}",
    )
    _assert_scene_snapshot(session, ctx, report_scene_lock)

    report_evidence = set(report.payload.get("evidence_refs") or [])
    evidence_refs = body.evidence_refs or list(report_evidence)
    if not set(evidence_refs).issubset(report_evidence):
        raise ApiError(
            "INSIGHT_EVIDENCE_NOT_IN_REPORT",
            "动作只能引用报告已固化的证据",
            409,
        )
    _resolve_evidence_refs(session, ctx, evidence_refs)

    action_id = body.action_id or f"insight_action_{uuid.uuid4().hex[:12]}"
    if session.get(InsightAction, action_id) is not None:
        raise ApiError("INSIGHT_ACTION_ALREADY_EXISTS", "动作 ID 已存在", 409)
    branch, minimum_branch = _review_branch(body, metric.payload)
    status = "pending_review" if branch == "human_review" else "experiment_ready"
    review_task_id = f"review_{action_id}" if branch == "human_review" else None
    data = {
        "id": action_id,
        "action_id": action_id,
        "report_id": report.report_id,
        "metric_result_id": metric.metric_result_id,
        "metric_key": metric_key,
        "action_type": body.action_type,
        "branch": branch,
        "requested_branch": body.branch,
        "minimum_governance_branch": minimum_branch,
        "branch_escalated": body.branch not in {"auto", branch},
        "risk_level": body.risk_level,
        "status": status,
        "review_task_id": review_task_id,
        "owner": body.owner,
        "hypothesis": body.hypothesis,
        "target_value": body.target_value,
        "evidence_refs": evidence_refs,
        "source": body.source,
        **report_scene_lock,
        "trace_id": ctx.trace_id,
        "resource_version": 1,
        "affected_objects": [
            {"type": "insight_report", "id": report.report_id},
            {"type": "metric_result", "id": metric.metric_result_id},
            *[
                {"type": "evidence_pack", "id": item}
                for item in evidence_refs
                if item.startswith("AF-")
            ],
        ],
        "next_actions": [
            {
                "key": "review_action" if branch == "human_review" else "start_experiment",
                "label": "进入人工复核" if branch == "human_review" else "创建效果实验",
                "route": f"human-review-tasks/{review_task_id}"
                if review_task_id
                else f"insights/actions/{action_id}/experiments",
            },
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
        ],
    }
    action = InsightAction(
        action_id=action_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        report_id=report.report_id,
        baseline_metric_result_id=metric.metric_result_id,
        action_type=body.action_type,
        branch=branch,
        risk_level=body.risk_level,
        status=status,
        review_task_id=review_task_id,
        resource_version=1,
        trace_id=ctx.trace_id,
        payload=data,
    )
    session.add(action)
    if review_task_id:
        review_data = {
            "id": review_task_id,
            "review_task_id": review_task_id,
            "title": f"洞察动作复核：{metric.payload.get('label') or metric_key}",
            "status": "pending",
            "review_status": "pending",
            "risk_level": body.risk_level,
            "evidence_refs": evidence_refs,
            "target_refs": [{"type": "work_item", "id": action_id}],
            "affected_objects": [{"type": "work_item", "id": action_id}],
            "source_report_id": report.report_id,
            "source_metric_result_id": metric.metric_result_id,
            **report_scene_lock,
            "trace_id": ctx.trace_id,
        }
        upsert_resource(
            session,
            ctx,
            "human_review_tasks",
            review_task_id,
            review_data,
            status="pending",
            trace_id=ctx.trace_id,
            audit_action="insight_action.review_requested",
        )
        data["affected_objects"].append({"type": "human_review_task", "id": review_task_id})
        action.payload = data
    upsert_resource(
        session,
        ctx,
        "work_items",
        action_id,
        {**data, "insight_action_id": action_id},
        status=status,
        trace_id=ctx.trace_id,
        audit_action="insight_action.create",
    )
    enqueue_event(
        session,
        ctx,
        event_type="insight_action.created",
        aggregate_type="insight_action",
        aggregate_id=action_id,
        payload=data,
    )
    record_audit(
        session,
        ctx,
        action="insight_action.create",
        object_type="insight_action",
        object_id=action_id,
        after=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


async def create_insight_experiment(
    session: Session,
    ctx: RequestContext,
    request: Request,
    action_id: str,
    body: InsightExperimentRequest,
) -> dict[str, Any]:
    from app.services.run_service import create_run

    operation = f"insights.actions.{action_id}.experiments.create"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    action = session.scalar(
        select(InsightAction)
        .where(
            InsightAction.action_id == action_id,
            InsightAction.tenant_id == ctx.tenant_id,
            InsightAction.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if action is None:
        raise ApiError("NOT_FOUND", f"对象不存在：{action_id}", 404)
    if action.status == "pending_review":
        raise ApiError(
            "INSIGHT_ACTION_REVIEW_REQUIRED",
            "高风险洞察动作必须先完成人工复核",
            409,
            details=[{"action_id": action_id, "review_task_id": action.review_task_id}],
        )
    if action.status not in {"experiment_ready", "approved"}:
        raise ApiError("INSIGHT_ACTION_NOT_EXPERIMENT_READY", "动作当前状态不能创建实验", 409)
    metric = _scoped(
        session,
        MetricResult,
        action.baseline_metric_result_id,
        MetricResult.metric_result_id,
        ctx,
    )
    metric_key = str(metric.payload.get("metric_key") or "")
    if body.primary_metric_key and body.primary_metric_key != metric_key:
        raise ApiError("INSIGHT_EXPERIMENT_METRIC_MISMATCH", "实验主指标必须与动作基线一致", 409)
    action_scene_lock = _scene_lock(
        action.payload,
        object_ref=f"insight_action:{action.action_id}",
    )
    _require_same_scene_lock(
        action_scene_lock,
        _scene_lock(metric.payload, object_ref=f"metric_result:{metric.metric_result_id}"),
        object_ref=f"insight_experiment:{body.experiment_id or 'new'}",
    )
    _assert_scene_snapshot(session, ctx, action_scene_lock)
    experiment_id = body.experiment_id or f"insight_experiment_{uuid.uuid4().hex[:12]}"

    def prepare(record: RunRecord) -> None:
        if session.get(InsightExperiment, experiment_id) is not None:
            raise ApiError("INSIGHT_EXPERIMENT_ALREADY_EXISTS", "实验 ID 已存在", 409)
        data = {
            "id": experiment_id,
            "experiment_id": experiment_id,
            "action_id": action.action_id,
            "report_id": action.report_id,
            "eval_run_id": record.run_id,
            "baseline_metric_result_id": metric.metric_result_id,
            "primary_metric_key": metric_key,
            "allocation_percent": body.allocation_percent,
            "duration_days": body.duration_days,
            "min_sample_size": body.min_sample_size,
            "hypothesis": body.hypothesis,
            "candidate": body.candidate,
            "control": body.control,
            "guardrails": body.guardrails,
            "status": "running",
            **action_scene_lock,
            "trace_id": ctx.trace_id,
        }
        experiment = InsightExperiment(
            experiment_id=experiment_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            action_id=action.action_id,
            eval_run_id=record.run_id,
            baseline_metric_result_id=metric.metric_result_id,
            status="running",
            trace_id=ctx.trace_id,
            payload=data,
        )
        session.add(experiment)
        record_audit(
            session,
            ctx,
            action="insight_experiment.create",
            object_type="insight_experiment",
            object_id=experiment_id,
            after=data,
        )
        action.status = "experiment_running"
        action.resource_version += 1
        action.payload = {
            **action.payload,
            "status": action.status,
            "experiment_id": experiment_id,
            "eval_run_id": record.run_id,
            "resource_version": action.resource_version,
            "trace_id": ctx.trace_id,
        }
        upsert_resource(
            session,
            ctx,
            "work_items",
            action.action_id,
            action_payload(action),
            status=action.status,
            trace_id=ctx.trace_id,
            audit_action="insight_action.experiment_started",
        )
        record.payload = {
            **record.payload,
            **action_scene_lock,
            "insight_experiment_id": experiment_id,
            "experiment_id": experiment_id,
            "source_action_id": action.action_id,
            "action_id": action.action_id,
            "source_report_id": action.report_id,
            "report_id": action.report_id,
            "eval_run_id": record.run_id,
            "baseline_metric_result_id": metric.metric_result_id,
            "primary_metric_key": metric_key,
            "affected_objects": [
                {"type": "insight_action", "id": action.action_id},
                {"type": "insight_experiment", "id": experiment_id},
                {"type": "metric_result", "id": metric.metric_result_id},
            ],
            "next_actions": [
                {
                    "key": "view_experiment",
                    "label": "查看实验",
                    "route": f"insights/experiments/{experiment_id}",
                },
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
            ],
        }

    return await create_run(
        session,
        ctx,
        request,
        run_type="eval_run",
        event_type="eval_run.requested",
        payload={
            **body.model_dump(exclude_none=True),
            **action_scene_lock,
            "dataset_id": f"insight_action:{action.action_id}",
            "insight_experiment_id": experiment_id,
            "source_action_id": action.action_id,
            "source_report_id": action.report_id,
            "baseline_metric_result_id": metric.metric_result_id,
            "primary_metric_key": metric_key,
        },
        status="pending",
        idempotency_operation=operation,
        prepare_record=prepare,
    )


async def create_insight_experiment_retry_attempt(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: InsightExperimentRetryAttemptRequest,
) -> dict[str, Any]:
    from app.services.run_service import create_run

    operation = f"insights.experiments.{experiment_id}.retry-attempts.create"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    current_experiment = session.scalar(
        select(InsightExperiment).where(
            InsightExperiment.experiment_id == experiment_id,
            InsightExperiment.tenant_id == ctx.tenant_id,
            InsightExperiment.project_id == ctx.project_id,
        )
    )
    if current_experiment is None:
        raise ApiError("NOT_FOUND", f"洞察实验不存在：{experiment_id}", 404)

    source_run_id = current_experiment.eval_run_id
    source_record = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == source_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    experiment = session.scalar(
        select(InsightExperiment)
        .where(
            InsightExperiment.experiment_id == experiment_id,
            InsightExperiment.tenant_id == ctx.tenant_id,
            InsightExperiment.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if experiment is None:
        raise ApiError("NOT_FOUND", f"洞察实验不存在：{experiment_id}", 404)
    if experiment.eval_run_id != source_run_id:
        raise ApiError(
            "INSIGHT_EXPERIMENT_NOT_RETRYABLE",
            "实验已经绑定到新的评测运行，当前失败 attempt 不能重复重试",
            409,
            details=[
                {
                    "experiment_id": experiment_id,
                    "expected_run_id": source_run_id,
                    "current_run_id": experiment.eval_run_id,
                }
            ],
        )
    if source_record is None:
        raise ApiError(
            "INSIGHT_EXPERIMENT_RETRY_CAUSAL_CHAIN_BROKEN",
            "实验当前绑定的评测运行不存在",
            409,
            details=[{"experiment_id": experiment_id, "eval_run_id": source_run_id}],
        )

    completion_receipt = source_record.payload.get("completion_receipt")
    receipt_payload = completion_receipt if isinstance(completion_receipt, dict) else {}
    receipt_retryable = (
        receipt_payload.get("status") == "failed" and receipt_payload.get("retryable") is True
    )
    source_matches = (
        source_record.run_type == "eval_run"
        and source_record.payload.get("insight_experiment_id") == experiment_id
    )
    if not (
        experiment.status == "failed"
        and source_record.status == "failed"
        and source_record.payload.get("retryable") is True
        and receipt_retryable
        and source_matches
    ):
        raise ApiError(
            "INSIGHT_EXPERIMENT_NOT_RETRYABLE",
            "只有具备可重试失败回执的洞察实验才能创建 retry attempt",
            409,
            details=[
                {
                    "experiment_id": experiment_id,
                    "experiment_status": experiment.status,
                    "eval_run_id": source_record.run_id,
                    "run_status": source_record.status,
                    "run_type": source_record.run_type,
                    "retryable": receipt_retryable,
                }
            ],
        )

    action = session.scalar(
        select(InsightAction)
        .where(
            InsightAction.action_id == experiment.action_id,
            InsightAction.tenant_id == ctx.tenant_id,
            InsightAction.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if action is None:
        raise ApiError(
            "INSIGHT_EXPERIMENT_RETRY_CAUSAL_CHAIN_BROKEN",
            "实验关联的洞察动作不存在",
            409,
            details=[{"experiment_id": experiment_id, "action_id": experiment.action_id}],
        )
    if action.status != "experiment_failed":
        raise ApiError(
            "INSIGHT_EXPERIMENT_NOT_RETRYABLE",
            "只有失败状态一致的实验和动作才能创建 retry attempt",
            409,
            details=[
                {
                    "experiment_id": experiment_id,
                    "experiment_status": experiment.status,
                    "action_id": action.action_id,
                    "action_status": action.status,
                }
            ],
        )
    report = get_insight_report(session, ctx, action.report_id)
    if report.status != "generated":
        raise ApiError(
            "INSIGHT_EXPERIMENT_NOT_RETRYABLE",
            "上游报告不再可用，实验不能创建 retry attempt",
            409,
            details=[{"report_id": report.report_id, "status": report.status}],
        )

    causal_values = {
        "action_id": action.action_id,
        "report_id": report.report_id,
        "baseline_metric_result_id": experiment.baseline_metric_result_id,
    }
    mismatches = [
        {
            "field": field,
            "expected": expected,
            "actual": source_record.payload.get(field)
            or source_record.payload.get(f"source_{field}"),
        }
        for field, expected in causal_values.items()
        if (source_record.payload.get(field) or source_record.payload.get(f"source_{field}"))
        != expected
    ]
    if mismatches:
        raise ApiError(
            "INSIGHT_EXPERIMENT_RETRY_CAUSAL_CHAIN_BROKEN",
            "失败运行与实验因果投影不一致",
            409,
            details=mismatches,
        )

    raw_history = experiment.payload.get("retry_history")
    retry_history = list(raw_history) if isinstance(raw_history, list) else []
    retry_attempt = (
        max(
            int(source_record.payload.get("retry_attempt") or 0),
            len(retry_history),
        )
        + 1
    )
    attempt_id = f"{experiment_id}:retry:{retry_attempt}"
    root_run_id = str(source_record.payload.get("root_run_id") or source_record.run_id)
    root_trace_id = str(source_record.payload.get("root_trace_id") or source_record.trace_id)
    failure = {
        "completion_receipt_id": receipt_payload.get("completion_receipt_id"),
        "error_code": receipt_payload.get("error_code") or source_record.payload.get("error_code"),
        "message": receipt_payload.get("note") or source_record.payload.get("error"),
        "retryable": True,
    }
    design_fields = (
        "allocation_percent",
        "duration_days",
        "min_sample_size",
        "primary_metric_key",
        "hypothesis",
        "candidate",
        "control",
        "guardrails",
    )
    retry_payload = {
        **{
            field: experiment.payload.get(field, source_record.payload.get(field))
            for field in design_fields
        },
        "dataset_id": source_record.payload.get("dataset_id")
        or f"insight_action:{action.action_id}",
        "insight_experiment_id": experiment.experiment_id,
        "experiment_id": experiment.experiment_id,
        "source_action_id": action.action_id,
        "action_id": action.action_id,
        "source_report_id": report.report_id,
        "report_id": report.report_id,
        "baseline_metric_result_id": experiment.baseline_metric_result_id,
        "parent_run_id": source_record.run_id,
        "retry_of": source_record.run_id,
        "retry_of_run_id": source_record.run_id,
        "root_run_id": root_run_id,
        "parent_trace_id": source_record.trace_id,
        "retry_of_trace_id": source_record.trace_id,
        "root_trace_id": root_trace_id,
        "retry_attempt": retry_attempt,
        "attempt_id": attempt_id,
        "retry_reason": body.reason,
        "retry_source": body.source,
        "trigger_type": "insight_experiment_retry",
        "run_key": f"insight_experiment:{experiment_id}:attempt:{retry_attempt}",
        "affected_objects": [
            {"type": "insight_report", "id": report.report_id},
            {"type": "insight_action", "id": action.action_id},
            {"type": "insight_experiment", "id": experiment.experiment_id},
            {
                "type": "metric_result",
                "id": experiment.baseline_metric_result_id,
            },
            {"type": "run_record", "id": source_record.run_id, "status": "failed"},
        ],
        "next_actions": [
            {
                "key": "view_experiment",
                "label": "查看实验",
                "route": f"insights/experiments/{experiment_id}",
            },
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
        ],
    }

    def prepare_retry_attempt(new_record: RunRecord) -> None:
        requested_at = datetime.now(UTC).isoformat()
        attempt = {
            "attempt_id": attempt_id,
            "attempt": retry_attempt,
            "from_run_id": source_record.run_id,
            "to_run_id": new_record.run_id,
            "reason": body.reason,
            "source": body.source,
            "failure": failure,
            "requested_at": requested_at,
            "trace_id": new_record.trace_id,
            "parent_trace_id": source_record.trace_id,
        }
        updated_history = [*retry_history, attempt]
        before_experiment = experiment_payload(experiment)
        clean_experiment_payload = {
            key: value
            for key, value in experiment.payload.items()
            if key not in {"error", "effect_id", "outcome_metric_result_id"}
        }
        experiment.eval_run_id = new_record.run_id
        experiment.outcome_metric_result_id = None
        experiment.status = "running"
        experiment.trace_id = new_record.trace_id
        experiment.payload = {
            **clean_experiment_payload,
            "eval_run_id": new_record.run_id,
            "status": "running",
            "trace_id": new_record.trace_id,
            "parent_run_id": source_record.run_id,
            "retry_of": source_record.run_id,
            "retry_of_run_id": source_record.run_id,
            "root_run_id": root_run_id,
            "parent_trace_id": source_record.trace_id,
            "retry_of_trace_id": source_record.trace_id,
            "root_trace_id": root_trace_id,
            "retry_attempt": retry_attempt,
            "attempt_id": attempt_id,
            "retry_reason": body.reason,
            "retry_source": body.source,
            "retry_history": updated_history,
        }

        clean_action_payload = {
            key: value
            for key, value in action.payload.items()
            if key not in {"error", "effect_id", "outcome_metric_result_id"}
        }
        action.status = "experiment_running"
        action.resource_version += 1
        action.trace_id = new_record.trace_id
        action.payload = {
            **clean_action_payload,
            "status": action.status,
            "experiment_id": experiment.experiment_id,
            "eval_run_id": new_record.run_id,
            "retry_attempt": retry_attempt,
            "attempt_id": attempt_id,
            "parent_run_id": source_record.run_id,
            "retry_of_run_id": source_record.run_id,
            "retry_history": updated_history,
            "resource_version": action.resource_version,
            "trace_id": new_record.trace_id,
        }
        new_record.payload = {
            **new_record.payload,
            "eval_run_id": new_record.run_id,
            "retry_history": updated_history,
        }
        upsert_resource(
            session,
            ctx,
            "work_items",
            action.action_id,
            action_payload(action),
            status=action.status,
            trace_id=new_record.trace_id,
            audit_action="insight_action.experiment_retry_started",
        )
        record_audit(
            session,
            ctx,
            action="insight_experiment.retry_attempt.create",
            object_type="insight_experiment",
            object_id=experiment.experiment_id,
            before=before_experiment,
            after=experiment_payload(experiment),
            trace_id=new_record.trace_id,
        )

    return await create_run(
        session,
        ctx,
        request,
        run_type="eval_run",
        event_type="eval_run.requested",
        payload=retry_payload,
        status="pending",
        idempotency_operation=operation,
        prepare_record=prepare_retry_attempt,
    )


def _numeric_metric(receipt: dict[str, Any], metric_key: str) -> float:
    metrics = receipt.get("metrics") or {}
    raw = metrics.get(metric_key, metrics.get("value"))
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw)):
        raise ApiError(
            "INSIGHT_EFFECT_METRIC_REQUIRED",
            f"实验完成回执必须提供数值指标：{metric_key}",
            422,
        )
    return float(raw)


def _confidence_interval(receipt: dict[str, Any]) -> tuple[float, float]:
    metrics = receipt.get("metrics") or {}
    interval = metrics.get("confidence_interval") or {}
    low = interval.get("low") if isinstance(interval, dict) else None
    high = interval.get("high") if isinstance(interval, dict) else None
    if (
        isinstance(low, bool)
        or isinstance(high, bool)
        or not isinstance(low, int | float)
        or not isinstance(high, int | float)
        or not math.isfinite(float(low))
        or not math.isfinite(float(high))
    ):
        raise ApiError("INSIGHT_EFFECT_CONFIDENCE_INVALID", "置信区间必须是数值", 422)
    if float(low) > float(high):
        raise ApiError("INSIGHT_EFFECT_CONFIDENCE_INVALID", "置信区间下界不能大于上界", 422)
    return float(low), float(high)


def _validated_effect_evidence(
    receipt: dict[str, Any],
    *,
    experiment: InsightExperiment,
    action: InsightAction,
    outcome_value: float,
) -> tuple[int, float, float, bool]:
    result_ref = receipt.get("result_ref") or {}
    receipt_experiment_id = result_ref.get("experiment_id")
    if receipt_experiment_id != experiment.experiment_id:
        raise ApiError(
            "INSIGHT_EFFECT_EXPERIMENT_MISMATCH",
            "实验完成回执必须携带与运行一致的 experiment_id",
            422,
            details=[
                {
                    "expected_experiment_id": experiment.experiment_id,
                    "received_experiment_id": receipt_experiment_id,
                }
            ],
        )
    receipt_action_id = result_ref.get("action_id")
    if receipt_action_id is not None and receipt_action_id != action.action_id:
        raise ApiError("INSIGHT_EFFECT_ACTION_MISMATCH", "实验回执 action_id 与运行不一致", 422)

    metrics = receipt.get("metrics") or {}
    sample_size = metrics.get("sample_size")
    minimum_sample_size = int(experiment.payload.get("min_sample_size") or 0)
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise ApiError("INSIGHT_EFFECT_SAMPLE_SIZE_INVALID", "实验回执必须提供整数样本量", 422)
    if sample_size < minimum_sample_size:
        raise ApiError(
            "INSIGHT_EFFECT_SAMPLE_SIZE_INSUFFICIENT",
            "实验样本量未达到预设下限",
            422,
            details=[{"minimum": minimum_sample_size, "actual": sample_size}],
        )

    confidence_low, confidence_high = _confidence_interval(receipt)
    if not confidence_low <= outcome_value <= confidence_high:
        raise ApiError(
            "INSIGHT_EFFECT_CONFIDENCE_INVALID",
            "实验结果值必须位于置信区间内",
            422,
        )
    statistically_significant = metrics.get("statistically_significant")
    if not isinstance(statistically_significant, bool):
        raise ApiError(
            "INSIGHT_EFFECT_SIGNIFICANCE_REQUIRED",
            "实验回执必须明确 statistically_significant",
            422,
        )
    return sample_size, confidence_low, confidence_high, statistically_significant


def _validated_aggregation_results(
    record: RunRecord,
    receipt: dict[str, Any],
) -> list[InsightMetricAggregationResult]:
    requested = record.payload.get("metric_keys")
    if (
        not isinstance(requested, list)
        or not requested
        or not all(isinstance(item, str) for item in requested)
    ):
        raise ApiError(
            "INSIGHT_METRIC_RUN_INVALID",
            "指标聚合运行缺少受治理的 metric_keys",
            409,
        )
    raw_definitions = record.payload.get("metric_definitions")
    if not isinstance(raw_definitions, list):
        raise ApiError(
            "INSIGHT_METRIC_RUN_INVALID",
            "指标聚合运行缺少冻结的 SceneProfile 指标定义",
            409,
        )
    definitions = {
        str(item.get("metric_key")): item
        for item in raw_definitions
        if isinstance(item, dict) and isinstance(item.get("metric_key"), str)
    }
    if set(definitions) != set(requested):
        raise ApiError(
            "INSIGHT_METRIC_DEFINITION_DRIFT",
            "冻结指标定义与请求指标集合不一致",
            409,
        )
    result_ref = receipt.get("result_ref") or {}
    raw_results = result_ref.get("metric_results") if isinstance(result_ref, dict) else None
    if not isinstance(raw_results, list) or not raw_results:
        raise ApiError(
            "INSIGHT_METRIC_RESULTS_REQUIRED",
            "指标聚合完成回执必须在 result_ref.metric_results 提供结果列表",
            422,
        )
    results: list[InsightMetricAggregationResult] = []
    for index, raw in enumerate(raw_results):
        try:
            results.append(InsightMetricAggregationResult.model_validate(raw))
        except ValidationError as exc:
            raise ApiError(
                "INSIGHT_METRIC_RESULT_INVALID",
                "指标聚合结果必须包含合法的 metric_key、value、unit 和 sample_size",
                422,
                details=[{"index": index, "errors": exc.errors(include_url=False)}],
            ) from exc

    actual_keys = [item.metric_key for item in results]
    duplicates = sorted({item for item in actual_keys if actual_keys.count(item) > 1})
    missing = [item for item in requested if item not in actual_keys]
    unexpected = [item for item in actual_keys if item not in requested]
    if duplicates or missing or unexpected:
        raise ApiError(
            "INSIGHT_METRIC_RESULT_SET_MISMATCH",
            "聚合回执必须且只能为每个请求指标提供一条结果",
            422,
            details=[
                {
                    "requested_metric_keys": requested,
                    "missing_metric_keys": missing,
                    "unexpected_metric_keys": unexpected,
                    "duplicate_metric_keys": duplicates,
                }
            ],
        )
    by_key = {item.metric_key: item for item in results}
    ordered = [by_key[item] for item in requested]
    for item in ordered:
        expected_unit = str(definitions[item.metric_key].get("unit") or "")
        if item.unit != expected_unit:
            raise ApiError(
                "INSIGHT_METRIC_UNIT_MISMATCH",
                "聚合回执的指标单位与治理定义不一致",
                422,
                details=[
                    {
                        "metric_key": item.metric_key,
                        "expected_unit": expected_unit,
                        "actual_unit": item.unit,
                    }
                ],
            )
    return ordered


def _materialize_metric_aggregation(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    if receipt.get("status") != "success":
        return {"source_run_id": record.run_id, "status": "failed", "metric_result_ids": []}
    results = _validated_aggregation_results(record, receipt)
    definitions = {
        str(item["metric_key"]): item
        for item in record.payload.get("metric_definitions", [])
        if isinstance(item, dict) and isinstance(item.get("metric_key"), str)
    }
    raw_scope = record.payload.get("metric_scope")
    if not isinstance(raw_scope, dict):
        raise ApiError("INSIGHT_METRIC_RUN_INVALID", "指标聚合运行缺少冻结 scope", 409)
    scope = _normalized_scope(
        time_range=str(raw_scope.get("time_range") or ""),
        store_ids=[str(item) for item in raw_scope.get("store_ids") or []],
        model_version=raw_scope.get("model_version"),
        label_version=raw_scope.get("label_version"),
    )
    metric_result_ids: list[str] = []
    for result in results:
        digest = uuid.uuid5(
            uuid.NAMESPACE_URL,
            (
                f"auris-flow:metric-result:{record.tenant_id}:{record.project_id}:"
                f"{record.run_id}:{result.metric_key}"
            ),
        ).hex[:24]
        metric_result_id = f"metric_{digest}"
        if session.get(MetricResult, metric_result_id) is not None:
            raise ApiError(
                "INSIGHT_METRIC_RESULT_ALREADY_EXISTS",
                "聚合运行试图覆盖不可变指标结果",
                409,
                details=[{"metric_result_id": metric_result_id}],
            )
        definition = definitions[result.metric_key]
        payload = {
            "id": metric_result_id,
            "metric_result_id": metric_result_id,
            "metric_key": result.metric_key,
            **definition,
            "value": result.value,
            "unit": result.unit,
            "sample_size": result.sample_size,
            "definition_version": "insight-metric-v1",
            "scene_profile_id": record.payload.get("scene_profile_id"),
            "scene_profile_version_id": record.payload.get("scene_profile_version_id"),
            "scene_profile_snapshot_sha256": record.payload.get("scene_profile_snapshot_sha256"),
            "scope": scope,
            "time_range": scope["time_range"],
            "dimensions": {"store_ids": scope["store_ids"]},
            "model_version": scope["model_version"],
            "label_version": scope["label_version"],
            "source_run_id": record.run_id,
            "source_external_id": receipt.get("external_id"),
            "completion_receipt_id": receipt.get("completion_receipt_id"),
            "captured_at": receipt.get("received_at"),
            "snapshot_role": "aggregation",
            "data_source": "dagster_aggregation",
            "immutable": True,
            "status": "materialized",
            "trace_id": record.trace_id,
        }
        if definition.get("label_version_applicability") == "required":
            raw_label_scope = record.payload.get("label_scope")
            label_scope_lock = record.payload.get("label_scope_lock")
            if not isinstance(raw_label_scope, dict) or not isinstance(label_scope_lock, dict):
                raise ApiError(
                    "INSIGHT_LABEL_SCOPE_LOCK_MISSING",
                    "标签指标运行缺少受理时冻结的 scope/Head 锁",
                    409,
                )
            metric_lock = (label_scope_lock.get("metric_locks") or {}).get(result.metric_key)
            expected_definition_version = str(definition.get("calculator_ref") or "")
            if not isinstance(metric_lock, dict) or (
                metric_lock.get("definition_version") != expected_definition_version
                or metric_lock.get("metric_family") != definition.get("metric_family")
            ):
                raise ApiError(
                    "INSIGHT_LABEL_SCOPE_LOCK_DRIFT",
                    "标签指标定义与受理时 scope lock 不一致",
                    409,
                    details=[{"metric_key": result.metric_key}],
                )
            from app.schemas.label_metric_scopes import (
                LabelMetricResultMaterializeRequest,
            )
            from app.services.label_metric_scope_service import (
                materialize_label_metric_result,
            )

            materialize_request = LabelMetricResultMaterializeRequest.model_validate(
                {
                    "metric_result_id": metric_result_id,
                    "metric_key": result.metric_key,
                    "metric_family": definition["metric_family"],
                    "value": result.value,
                    "unit": result.unit,
                    "sample_size": result.sample_size,
                    "source_run_id": record.run_id,
                    **raw_label_scope,
                    "metric_definition_versions": {result.metric_key: expected_definition_version},
                    "result_payload": payload,
                }
            )
            materialize_label_metric_result(
                session,
                ctx,
                materialize_request,
                accepted_scope_lock=label_scope_lock,
            )
            metric_result_ids.append(metric_result_id)
            continue
        metric = MetricResult(
            metric_result_id=metric_result_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            status="materialized",
            trace_id=record.trace_id,
            payload=payload,
        )
        session.add(metric)
        metric_result_ids.append(metric_result_id)
        record_audit(
            session,
            ctx,
            action="metric_result.materialize",
            object_type="metric_result",
            object_id=metric_result_id,
            after=payload,
        )
    affected_objects = list(record.payload.get("affected_objects") or [])
    affected_objects.extend({"type": "metric_result", "id": item} for item in metric_result_ids)
    record.payload = {
        **record.payload,
        "metric_result_ids": metric_result_ids,
        "affected_objects": affected_objects,
    }
    return {
        "source_run_id": record.run_id,
        "status": "materialized",
        "metric_result_ids": metric_result_ids,
        "metric_count": len(metric_result_ids),
        "scope": scope,
    }


def _validated_governed_report_document(
    session: Session,
    record: RunRecord,
    report: InsightReport,
) -> tuple[dict[str, Any], str]:
    raw_document = record.payload.get("report_document")
    try:
        document = InsightReportDocument.model_validate(raw_document)
    except ValidationError as exc:
        raise ApiError(
            "INSIGHT_REPORT_DOCUMENT_INVALID",
            "报告运行缺少合法的受治理文档",
            409,
            details=[{"run_id": record.run_id, "errors": exc.errors(include_url=False)}],
            retryable=True,
        ) from exc

    payload = document.model_dump(mode="json")
    expected_binding = {
        "report_id": report.report_id,
        "run_id": record.run_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "trace_id": record.trace_id,
    }
    binding_mismatches = [
        {"field": key, "expected": expected, "actual": payload.get(key)}
        for key, expected in expected_binding.items()
        if payload.get(key) != expected
    ]
    if binding_mismatches:
        raise ApiError(
            "INSIGHT_REPORT_DOCUMENT_BINDING_MISMATCH",
            "受治理报告文档与当前报告运行范围不一致",
            409,
            details=[{"mismatches": binding_mismatches}],
            retryable=True,
        )

    metric_snapshots = payload["metric_results"]
    metric_ids = [item["metric_result_id"] for item in metric_snapshots]
    expected_metric_ids = list(report.payload.get("metric_result_ids") or [])
    if metric_ids != expected_metric_ids or metric_ids != list(
        record.payload.get("metric_result_ids") or []
    ):
        raise ApiError(
            "INSIGHT_REPORT_DOCUMENT_METRIC_IDS_MISMATCH",
            "报告文档中的指标快照与报告运行固化指标不一致",
            409,
            details=[{"expected": expected_metric_ids, "actual": metric_ids}],
            retryable=True,
        )

    metrics = session.scalars(
        select(MetricResult).where(
            MetricResult.tenant_id == record.tenant_id,
            MetricResult.project_id == record.project_id,
            MetricResult.metric_result_id.in_(metric_ids),
        )
    ).all()
    by_id = {item.metric_result_id: item for item in metrics}
    missing_metric_ids = [item for item in metric_ids if item not in by_id]
    if missing_metric_ids:
        raise ApiError(
            "INSIGHT_REPORT_METRIC_CAUSAL_CHAIN_BROKEN",
            "报告冻结的 MetricResult 已缺失",
            409,
            details=[{"metric_result_ids": missing_metric_ids}],
            retryable=True,
        )
    from app.services.insight_report_metric_binding_service import (
        verify_insight_report_metric_binding,
    )

    metric_binding = verify_insight_report_metric_binding(
        session,
        report,
        [by_id[item] for item in metric_ids],
    )
    for object_ref, container in (
        (f"report:{report.report_id}", report.payload),
        (f"run:{record.run_id}", record.payload),
    ):
        if (
            container.get("report_metric_binding_id") != metric_binding["report_metric_binding_id"]
            or container.get("metric_scope_sha256") != metric_binding["metric_scope_sha256"]
            or container.get("report_metric_binding_content_sha256")
            != metric_binding["content_sha256"]
        ):
            raise ApiError(
                "INSIGHT_REPORT_METRIC_BINDING_DRIFT",
                "报告或运行投影中的指标绑定锚点已漂移",
                409,
                details=[{"object_ref": object_ref}],
                retryable=True,
            )
    metric_hash_mismatches = []
    for snapshot in metric_snapshots:
        metric_id = snapshot["metric_result_id"]
        metric = by_id.get(metric_id)
        actual_hash = _canonical_sha256(metric.payload) if metric is not None else None
        field_mismatches: list[dict[str, Any]] = []
        if metric is not None:
            expected_snapshot_fields = {
                "metric_key": str(metric.payload.get("metric_key") or ""),
                "label": str(metric.payload.get("label") or metric.payload.get("metric_key") or ""),
                "value": metric.payload.get("value"),
                "unit": str(metric.payload.get("unit") or ""),
                "sample_size": metric.payload.get("sample_size"),
                "definition_version": str(metric.payload.get("definition_version") or ""),
                "scope": _metric_scope(metric.payload),
                "source_run_id": str(metric.payload.get("source_run_id") or ""),
                "trace_id": str(metric.trace_id or metric.payload.get("trace_id") or ""),
            }
            field_mismatches = [
                {"field": field, "expected": expected, "actual": snapshot.get(field)}
                for field, expected in expected_snapshot_fields.items()
                if snapshot.get(field) != expected
            ]
        if actual_hash != snapshot["payload_sha256"] or field_mismatches:
            metric_hash_mismatches.append(
                {
                    "metric_result_id": metric_id,
                    "expected": snapshot["payload_sha256"],
                    "actual": actual_hash,
                    "field_mismatches": field_mismatches,
                }
            )
    proof = payload["generator_proof"]
    proof_mismatches = []
    for field, value in (
        ("metric_snapshot_sha256", metric_snapshots),
        ("evidence_snapshot_sha256", payload["evidence"]),
        ("section_manifest_sha256", payload["sections"]),
    ):
        actual_hash = _canonical_sha256(value)
        if proof[field] != actual_hash:
            proof_mismatches.append(
                {"field": field, "expected": proof[field], "actual": actual_hash}
            )
    if metric_hash_mismatches or proof_mismatches:
        raise ApiError(
            "INSIGHT_REPORT_DOCUMENT_HASH_MISMATCH",
            "报告文档的冻结指标或生成器证明校验失败",
            409,
            details=[
                {
                    "metric_hash_mismatches": metric_hash_mismatches,
                    "proof_mismatches": proof_mismatches,
                }
            ],
            retryable=True,
        )
    return payload, _canonical_sha256(payload)


def _validated_report_artifact(
    session: Session,
    record: RunRecord,
    report: InsightReport,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    report_document, document_sha256 = _validated_governed_report_document(
        session,
        record,
        report,
    )
    raw_result_ref = receipt.get("result_ref")
    try:
        artifact = InsightReportArtifactResult.model_validate(raw_result_ref)
    except ValidationError as exc:
        raise ApiError(
            "INSIGHT_REPORT_ARTIFACT_INVALID",
            "报告成功回执必须提供完整、合法且受控的对象存储产物引用",
            422,
            details=[
                {
                    "run_id": record.run_id,
                    "errors": exc.errors(include_url=False),
                }
            ],
            retryable=True,
        ) from exc

    dispatch = record.payload.get("dispatch")
    if (
        not isinstance(dispatch, dict)
        or dispatch.get("adapter") != "object_storage"
        or dispatch.get("operation") not in {"reserve_object", "reconcile_object"}
        or dispatch.get("status") != "success"
    ):
        raise ApiError(
            "INSIGHT_REPORT_RESERVATION_INVALID",
            "报告运行缺少可验证的对象存储 reservation",
            409,
            details=[{"run_id": record.run_id, "dispatch": dispatch}],
            retryable=True,
        )
    reservation = dispatch.get("details")
    if not isinstance(reservation, dict):
        raise ApiError(
            "INSIGHT_REPORT_RESERVATION_INVALID",
            "报告运行的对象存储 reservation 缺少详情",
            409,
            details=[{"run_id": record.run_id}],
            retryable=True,
        )

    artifact_payload = artifact.model_dump(mode="json")
    governed_fields = ("storage_object_id", "object_uri", "content_type")
    unavailable = [
        field
        for field in governed_fields
        if not isinstance(reservation.get(field), str) or not reservation[field].strip()
    ]
    if unavailable:
        raise ApiError(
            "INSIGHT_REPORT_RESERVATION_INVALID",
            "报告运行的对象存储 reservation 不完整",
            409,
            details=[{"run_id": record.run_id, "missing_fields": unavailable}],
            retryable=True,
        )

    if reservation.get("artifact_state") != "materialized":
        raise ApiError(
            "INSIGHT_REPORT_ARTIFACT_NOT_MATERIALIZED",
            "对象存储中仍是占位引用，不能把报告标记为已生成",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "artifact_state": reservation.get("artifact_state"),
                }
            ],
            retryable=True,
        )
    reserved_sha256 = reservation.get("content_sha256")
    if (
        not isinstance(reserved_sha256, str)
        or artifact_payload["content_sha256"] != reserved_sha256
        or document_sha256 != reserved_sha256
    ):
        raise ApiError(
            "INSIGHT_REPORT_ARTIFACT_HASH_MISMATCH",
            "报告完成回执的内容摘要与已写入对象不一致",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "expected": reserved_sha256,
                    "actual": artifact_payload["content_sha256"],
                    "document_sha256": document_sha256,
                }
            ],
            retryable=True,
        )

    mismatches = [
        {
            "field": field,
            "expected": reservation[field],
            "actual": artifact_payload[field],
        }
        for field in governed_fields
        if artifact_payload[field] != reservation[field]
    ]
    if mismatches:
        raise ApiError(
            "INSIGHT_REPORT_RESERVATION_MISMATCH",
            "报告产物引用与对象存储 reservation 不一致",
            409,
            details=[{"run_id": record.run_id, "mismatches": mismatches}],
            retryable=True,
        )
    return {
        **artifact_payload,
        "artifact_state": "materialized",
        "content_length": reservation.get("content_length"),
        "verified": reservation.get("verified"),
        "report_document_sha256": document_sha256,
        "schema_version": report_document["schema_version"],
    }


def _invalidate_failed_report_downstream(
    session: Session,
    ctx: RequestContext,
    report: InsightReport,
    *,
    trace_id: str,
) -> dict[str, int]:
    actions = session.scalars(
        select(InsightAction).where(
            InsightAction.tenant_id == report.tenant_id,
            InsightAction.project_id == report.project_id,
            InsightAction.report_id == report.report_id,
        )
    ).all()
    invalidated_actions = 0
    invalidated_experiments = 0
    cancelled_runs = 0
    terminal_action_statuses = {"measured", "blocked_upstream_failed", "cancelled"}
    terminal_experiment_statuses = {"measured", "failed", "invalidated", "cancelled"}

    for action in actions:
        if action.status in terminal_action_statuses:
            continue
        experiments = session.scalars(
            select(InsightExperiment).where(
                InsightExperiment.tenant_id == report.tenant_id,
                InsightExperiment.project_id == report.project_id,
                InsightExperiment.action_id == action.action_id,
            )
        ).all()
        for experiment in experiments:
            if experiment.status in terminal_experiment_statuses:
                continue
            experiment.status = "invalidated"
            experiment.payload = {
                **experiment.payload,
                "status": "invalidated",
                "invalidated_reason": "upstream_report_failed",
                "source_report_status": "failed",
            }
            invalidated_experiments += 1
            run = session.get(RunRecord, experiment.eval_run_id)
            if run is not None and run.status not in {"success", "failed", "cancelled"}:
                # Local import avoids the run-service/insight-materializer import cycle.
                from app.services.run_service import transition_run

                transition_run(
                    run,
                    "cancelled",
                    reason="upstream_report_failed",
                )
                run.payload = {
                    **run.payload,
                    "cancelled_reason": "upstream_report_failed",
                    "source_report_id": report.report_id,
                }
                cancelled_runs += 1
            record_audit(
                session,
                ctx,
                action="insight_experiment.invalidated",
                object_type="insight_experiment",
                object_id=experiment.experiment_id,
                after=experiment_payload(experiment),
            )

        action.status = "blocked_upstream_failed"
        action.resource_version += 1
        action.payload = {
            **action.payload,
            "status": action.status,
            "blocked_reason": "upstream_report_failed",
            "source_report_status": "failed",
            "resource_version": action.resource_version,
        }
        upsert_resource(
            session,
            ctx,
            "work_items",
            action.action_id,
            action_payload(action),
            status=action.status,
            trace_id=trace_id,
            audit_action="insight_action.blocked_upstream_failed",
        )
        invalidated_actions += 1

    return {
        "invalidated_actions": invalidated_actions,
        "invalidated_experiments": invalidated_experiments,
        "cancelled_runs": cancelled_runs,
    }


def materialize_insight_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type == "insight_metric_aggregation":
        return _materialize_metric_aggregation(session, ctx, record, receipt)
    if record.run_type == "insight_report":
        report = session.scalar(
            select(InsightReport).where(
                InsightReport.tenant_id == record.tenant_id,
                InsightReport.project_id == record.project_id,
                InsightReport.run_id == record.run_id,
            )
        )
        if report is None:
            raise ApiError("INSIGHT_REPORT_PROJECTION_MISSING", "报告运行缺少强投影", 409)
        succeeded = receipt.get("status") == "success"
        result_ref = (
            _validated_report_artifact(session, record, report, receipt)
            if succeeded
            else dict(receipt.get("result_ref") or {})
        )
        if succeeded:
            receipt["result_ref"] = result_ref
            record.payload = {**record.payload, "result_ref": result_ref}
        target_status = "generated" if succeeded else "failed"
        report.status = target_status
        report.payload = {
            **report.payload,
            "status": target_status,
            "result_ref": result_ref,
            "metrics": receipt.get("metrics") or {},
            "completion_receipt_id": receipt.get("completion_receipt_id"),
            "completed_at": receipt.get("received_at"),
            "failure": {
                "error_code": receipt.get("error_code") or "INSIGHT_REPORT_FAILED",
                "message": receipt.get("note"),
                "retryable": bool(receipt.get("retryable", True)),
            }
            if target_status == "failed"
            else None,
        }
        upsert_resource(
            session,
            ctx,
            "insight_reports",
            report.report_id,
            report_payload(report),
            status=target_status,
            trace_id=record.trace_id,
            audit_action="insight_report.completed",
        )
        downstream = (
            _invalidate_failed_report_downstream(
                session,
                ctx,
                report,
                trace_id=record.trace_id,
            )
            if target_status == "failed"
            else {"invalidated_actions": 0, "invalidated_experiments": 0, "cancelled_runs": 0}
        )
        return {"report_id": report.report_id, "status": target_status, **downstream}

    experiment_id = record.payload.get("insight_experiment_id")
    if record.run_type != "eval_run" or not isinstance(experiment_id, str):
        return None
    experiment = _scoped(
        session,
        InsightExperiment,
        experiment_id,
        InsightExperiment.experiment_id,
        ctx,
    )
    action = _scoped(session, InsightAction, experiment.action_id, InsightAction.action_id, ctx)
    if experiment.status != "running" or action.status != "experiment_running":
        raise ApiError(
            "INSIGHT_EFFECT_STATE_INVALID",
            "只有运行中的实验和动作可以物化效果",
            409,
            details=[
                {
                    "experiment_status": experiment.status,
                    "action_status": action.status,
                }
            ],
        )
    report = get_insight_report(session, ctx, action.report_id)
    if report.status != "generated":
        raise ApiError(
            "INSIGHT_EFFECT_UPSTREAM_REPORT_INVALID",
            "上游报告不是 generated，禁止物化实验效果",
            409,
            details=[{"report_id": report.report_id, "status": report.status}],
        )
    if experiment.eval_run_id != record.run_id:
        raise ApiError("INSIGHT_EFFECT_RUN_MISMATCH", "实验与评测运行不一致", 409)
    if receipt.get("status") != "success":
        experiment.status = "failed"
        experiment.payload = {
            **experiment.payload,
            "status": "failed",
            "error": receipt.get("note"),
        }
        action.status = "experiment_failed"
        action.resource_version += 1
        action.payload = {
            **action.payload,
            "status": action.status,
            "resource_version": action.resource_version,
            "error": receipt.get("note"),
        }
        upsert_resource(
            session,
            ctx,
            "work_items",
            action.action_id,
            action_payload(action),
            status=action.status,
            trace_id=record.trace_id,
            audit_action="insight_action.experiment_failed",
        )
        return {"experiment_id": experiment.experiment_id, "status": "failed"}

    baseline = _scoped(
        session,
        MetricResult,
        experiment.baseline_metric_result_id,
        MetricResult.metric_result_id,
        ctx,
    )
    metric_key = str(
        experiment.payload.get("primary_metric_key") or baseline.payload.get("metric_key")
    )
    outcome_value = _numeric_metric(receipt, metric_key)
    baseline_value = float(baseline.payload.get("value") or 0)
    sample_size, confidence_low, confidence_high, statistically_significant = (
        _validated_effect_evidence(
            receipt,
            experiment=experiment,
            action=action,
            outcome_value=outcome_value,
        )
    )
    outcome_metric_result_id = f"metric_outcome_{experiment.experiment_id}"
    outcome_payload = {
        **baseline.payload,
        "id": outcome_metric_result_id,
        "metric_result_id": outcome_metric_result_id,
        "value": outcome_value,
        "source_report_id": action.report_id,
        "source_action_id": action.action_id,
        "source_experiment_id": experiment.experiment_id,
        "source_eval_run_id": record.run_id,
        "snapshot_role": "outcome",
        "captured_at": receipt.get("received_at"),
        "completion_receipt_id": receipt.get("completion_receipt_id"),
        "trace_id": record.trace_id,
    }
    outcome = session.get(MetricResult, outcome_metric_result_id)
    if outcome is None:
        outcome = MetricResult(
            metric_result_id=outcome_metric_result_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status="snapshot",
            trace_id=record.trace_id,
            payload=outcome_payload,
        )
        session.add(outcome)
        # InsightEffect and InsightExperiment reference this immutable result
        # through immediate composite foreign keys. Persist the parent first so
        # MySQL/SQLite cannot order the dependent writes ahead of it.
        session.flush()
    effect_id = f"insight_effect_{experiment.experiment_id}"
    delta = round(outcome_value - baseline_value, 6)
    effect_data = {
        "id": effect_id,
        "effect_id": effect_id,
        "action_id": action.action_id,
        "report_id": action.report_id,
        "experiment_id": experiment.experiment_id,
        "baseline_metric_result_id": baseline.metric_result_id,
        "outcome_metric_result_id": outcome_metric_result_id,
        "metric_key": metric_key,
        "baseline_value": baseline_value,
        "outcome_value": outcome_value,
        "delta": delta,
        "confidence_interval": {"low": confidence_low, "high": confidence_high},
        "sample_size": sample_size,
        "statistically_significant": statistically_significant,
        "status": "measured",
        "trace_id": record.trace_id,
    }
    effect = session.get(InsightEffect, effect_id)
    if effect is None:
        effect = InsightEffect(
            effect_id=effect_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            action_id=action.action_id,
            experiment_id=experiment.experiment_id,
            baseline_metric_result_id=baseline.metric_result_id,
            outcome_metric_result_id=outcome_metric_result_id,
            metric_key=metric_key,
            delta=delta,
            confidence_low=confidence_low,
            confidence_high=confidence_high,
            status="measured",
            trace_id=record.trace_id,
            payload=effect_data,
        )
        session.add(effect)
    experiment.status = "measured"
    experiment.outcome_metric_result_id = outcome_metric_result_id
    experiment.payload = {
        **experiment.payload,
        "status": "measured",
        "outcome_metric_result_id": outcome_metric_result_id,
        "effect_id": effect_id,
    }
    action.status = "measured"
    action.resource_version += 1
    action.payload = {
        **action.payload,
        "status": "measured",
        "experiment_id": experiment.experiment_id,
        "effect_id": effect_id,
        "outcome_metric_result_id": outcome_metric_result_id,
        "delta": delta,
        "resource_version": action.resource_version,
    }
    effect_refs = list(report.payload.get("effect_refs") or [])
    if effect_id not in effect_refs:
        effect_refs.append(effect_id)
    report.payload = {**report.payload, "effect_refs": effect_refs}
    upsert_resource(
        session,
        ctx,
        "work_items",
        action.action_id,
        action_payload(action),
        status="measured",
        trace_id=record.trace_id,
        audit_action="insight_action.effect_measured",
    )
    upsert_resource(
        session,
        ctx,
        "insight_reports",
        report.report_id,
        report_payload(report),
        status=report.status,
        trace_id=report.trace_id,
    )
    upsert_resource(
        session,
        ctx,
        "insight_effects",
        effect_id,
        effect_data,
        status="measured",
        trace_id=record.trace_id,
        audit_action="insight_effect.materialized",
    )
    record_audit(
        session,
        ctx,
        action="insight_experiment.measured",
        object_type="insight_experiment",
        object_id=experiment.experiment_id,
        after=experiment_payload(experiment),
    )
    return {
        "experiment_id": experiment.experiment_id,
        "effect_id": effect_id,
        "outcome_metric_result_id": outcome_metric_result_id,
        "delta": delta,
        "status": "measured",
    }
