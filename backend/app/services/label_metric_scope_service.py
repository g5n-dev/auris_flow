from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_mapping import sha256_document
from app.models import (
    LabelFactSet,
    LabelFactSetHead,
    LabelMappingBundle,
    LabelMappingBundlePath,
    LabelVersion,
    LabelVersionItem,
    MetricResult,
    MetricResultLabelScope,
    RunRecord,
)
from app.schemas.label_metric_scopes import (
    LabelMetricResultMaterializeRequest,
    LabelMetricRunScopeRequest,
)
from app.services.audit_service import record_audit
from app.services.idempotency_service import replay_or_conflict, save_idempotency_result
from app.services.outbox_service import enqueue_event

MATERIALIZE_OPERATION = "label_metric_results.materialize"
_COMPARABILITY_SEVERITY = {
    "comparable": 0,
    "not-applicable": 1,
    "partial": 2,
    "structural-break": 3,
}


@dataclass(frozen=True, slots=True)
class _FactSetAnchor:
    fact_set_head_id: str
    generation: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _body_hash(ctx: RequestContext, request: LabelMetricResultMaterializeRequest) -> str:
    return sha256_document(
        {
            "actor_kind": ctx.actor_kind,
            "body": request.model_dump(mode="json", exclude_none=False),
            "operation": MATERIALIZE_OPERATION,
            "schema_version": "auris.label-metric-materialize-command/1",
            "user_id": ctx.user_id,
        }
    )


def _source_run(
    session: Session,
    ctx: RequestContext,
    request: LabelMetricResultMaterializeRequest,
) -> RunRecord:
    record = session.scalar(
        select(RunRecord).where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_id == request.source_run_id,
        )
    )
    if record is None:
        raise ApiError(
            "LABEL_METRIC_SOURCE_RUN_NOT_FOUND",
            "当前租户项目中不存在指标来源运行",
            404,
        )
    if record.run_type != "insight_metric_aggregation":
        raise ApiError(
            "LABEL_METRIC_SOURCE_RUN_INVALID",
            "标签指标只能绑定受治理的洞察聚合运行",
            409,
            details=[{"run_type": record.run_type}],
        )
    return record


def _fact_set_anchor(
    session: Session,
    ctx: RequestContext,
    request: LabelMetricResultMaterializeRequest,
    *,
    accepted_scope_lock: dict[str, Any] | None = None,
) -> tuple[LabelFactSet, _FactSetAnchor]:
    if accepted_scope_lock is not None:
        lock_document = {
            key: value for key, value in accepted_scope_lock.items() if key != "scope_lock_sha256"
        }
        if accepted_scope_lock.get("scope_lock_sha256") != sha256_document(lock_document):
            raise ApiError(
                "INSIGHT_LABEL_SCOPE_LOCK_DRIFT",
                "受理时标签 scope lock 内容哈希已漂移",
                409,
            )
        expected_lock = {
            "fact_as_of": _iso(request.fact_as_of),
            "fact_namespace": request.fact_namespace,
            "fact_set_generation": request.expected_fact_set_generation,
            "fact_set_id": request.fact_set_id,
            "mapping_bundle_id": request.mapping_bundle_id,
            "source_label_version_ids": list(request.source_label_version_ids),
            "target_label_version_id": request.target_label_version_id,
            "taxonomy_mode": request.taxonomy_mode,
        }
        actual_lock = {key: accepted_scope_lock.get(key) for key in expected_lock}
        if actual_lock != expected_lock:
            raise ApiError(
                "INSIGHT_LABEL_SCOPE_LOCK_DRIFT",
                "物化请求与受理时标签 scope lock 不一致",
                409,
                details=[{"actual": actual_lock, "expected": expected_lock}],
            )
        fact_set = session.scalar(
            select(LabelFactSet).where(
                LabelFactSet.tenant_id == ctx.tenant_id,
                LabelFactSet.project_id == ctx.project_id,
                LabelFactSet.fact_set_id == request.fact_set_id,
            )
        )
        if fact_set is None:
            raise ApiError(
                "LABEL_METRIC_FACT_SET_NOT_FOUND",
                "受理时冻结的 FactSet 不再可见",
                404,
            )
        if fact_set.status not in {"published", "superseded", "archived"}:
            raise ApiError(
                "LABEL_METRIC_FACT_SET_NOT_PUBLISHED",
                "受理时冻结的 FactSet 从未形成可消费发布态",
                409,
                details=[{"status": fact_set.status}],
            )
        if (
            fact_set.fact_namespace != request.fact_namespace
            or fact_set.manifest_sha256 != accepted_scope_lock.get("fact_set_manifest_sha256")
            or fact_set.source_manifest_sha256
            != accepted_scope_lock.get("fact_set_source_manifest_sha256")
            or _as_utc(fact_set.fact_as_of) != _as_utc(request.fact_as_of)
        ):
            raise ApiError(
                "LABEL_METRIC_FACT_SET_DRIFT",
                "受理时冻结的 FactSet 内容锚点已漂移",
                409,
            )
        head_id = accepted_scope_lock.get("fact_set_head_id")
        if not isinstance(head_id, str) or not head_id:
            raise ApiError(
                "INSIGHT_LABEL_SCOPE_LOCK_DRIFT",
                "受理时 scope lock 缺少 FactSet Head ID",
                409,
            )
        return fact_set, _FactSetAnchor(
            fact_set_head_id=head_id,
            generation=request.expected_fact_set_generation,
        )

    head = session.scalar(
        select(LabelFactSetHead)
        .where(
            LabelFactSetHead.tenant_id == ctx.tenant_id,
            LabelFactSetHead.project_id == ctx.project_id,
            LabelFactSetHead.environment == "production",
            LabelFactSetHead.fact_namespace == request.fact_namespace,
        )
        .with_for_update()
    )
    if head is None:
        raise ApiError(
            "LABEL_METRIC_FACT_SET_HEAD_NOT_FOUND",
            "生产环境尚未发布该事实 namespace 的 FactSet Head",
            409,
        )
    expected = {
        "fact_set_id": request.fact_set_id,
        "generation": request.expected_fact_set_generation,
    }
    actual = {
        "fact_set_id": head.current_fact_set_id,
        "generation": head.generation,
    }
    if actual != expected:
        raise ApiError(
            "LABEL_METRIC_FACT_SET_HEAD_CONFLICT",
            "FactSet Head 已变化，拒绝用漂移口径物化指标",
            409,
            details=[{"actual": actual, "expected": expected}],
        )
    fact_set = session.scalar(
        select(LabelFactSet).where(
            LabelFactSet.tenant_id == ctx.tenant_id,
            LabelFactSet.project_id == ctx.project_id,
            LabelFactSet.fact_set_id == request.fact_set_id,
        )
    )
    if fact_set is None:
        raise ApiError("LABEL_METRIC_FACT_SET_NOT_FOUND", "指标引用的 FactSet 不存在", 404)
    if fact_set.status != "published":
        raise ApiError(
            "LABEL_METRIC_FACT_SET_NOT_PUBLISHED",
            "指标只能消费已发布的完整 FactSet",
            409,
            details=[{"status": fact_set.status}],
        )
    if (
        fact_set.fact_namespace != request.fact_namespace
        or head.current_manifest_sha256 != fact_set.manifest_sha256
        or _as_utc(fact_set.fact_as_of) != _as_utc(request.fact_as_of)
    ):
        raise ApiError(
            "LABEL_METRIC_FACT_SET_DRIFT",
            "FactSet、Head 与 fact_as_of 冻结锚点不一致",
            409,
        )
    return fact_set, _FactSetAnchor(
        fact_set_head_id=head.fact_set_head_id,
        generation=head.generation,
    )


def _validate_source_versions(
    session: Session,
    ctx: RequestContext,
    request: LabelMetricResultMaterializeRequest,
) -> None:
    visible = set(
        session.scalars(
            select(LabelVersion.label_version_id).where(
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
                LabelVersion.label_version_id.in_(request.source_label_version_ids),
            )
        )
    )
    missing = sorted(set(request.source_label_version_ids) - visible)
    if missing:
        raise ApiError(
            "LABEL_METRIC_SOURCE_VERSION_NOT_FOUND",
            "指标 scope 包含不存在或不可访问的源标签版本",
            404,
            details=[{"label_version_ids": missing}],
        )


def _mapping_comparability(
    session: Session,
    ctx: RequestContext,
    request: LabelMetricResultMaterializeRequest,
) -> tuple[LabelMappingBundle | None, str, list[str], list[str]]:
    if request.mapping_bundle_id is None:
        if request.taxonomy_mode == "native":
            return None, "comparable", ["NATIVE_VERSION_PARTITIONED"], []
        return None, "comparable", ["RECOMPUTED_FACT_SET"], []

    bundle = session.scalar(
        select(LabelMappingBundle).where(
            LabelMappingBundle.tenant_id == ctx.tenant_id,
            LabelMappingBundle.project_id == ctx.project_id,
            LabelMappingBundle.mapping_bundle_id == request.mapping_bundle_id,
        )
    )
    if bundle is None:
        raise ApiError("LABEL_METRIC_MAPPING_BUNDLE_NOT_FOUND", "映射 Bundle 不存在", 404)
    expected_sources = list(request.source_label_version_ids)
    if (
        bundle.status != "published"
        or sorted(bundle.source_label_version_ids) != expected_sources
        or bundle.target_label_version_id != request.target_label_version_id
    ):
        raise ApiError(
            "LABEL_METRIC_MAPPING_BUNDLE_MISMATCH",
            "已发布 Mapping Bundle 与指标 source/target scope 不一致",
            409,
            details=[
                {
                    "bundle_status": bundle.status,
                    "bundle_source_label_version_ids": sorted(bundle.source_label_version_ids),
                    "bundle_target_label_version_id": bundle.target_label_version_id,
                }
            ],
        )

    paths = list(
        session.scalars(
            select(LabelMappingBundlePath).where(
                LabelMappingBundlePath.tenant_id == ctx.tenant_id,
                LabelMappingBundlePath.project_id == ctx.project_id,
                LabelMappingBundlePath.mapping_bundle_id == bundle.mapping_bundle_id,
                LabelMappingBundlePath.metric_family.in_((request.metric_family, "*")),
            )
        )
    )
    active_items = list(
        session.execute(
            select(LabelVersionItem.label_version_id, LabelVersionItem.label_id).where(
                LabelVersionItem.tenant_id == ctx.tenant_id,
                LabelVersionItem.project_id == ctx.project_id,
                LabelVersionItem.label_version_id.in_(request.source_label_version_ids),
                LabelVersionItem.status == "active",
            )
        )
    )
    covered = {(path.source_label_version_id, path.source_label_id) for path in paths}
    missing_paths = sorted(set(active_items) - covered)
    reason_codes: set[str] = set()
    if missing_paths:
        reason_codes.add("MAPPING_COVERAGE_GAP")

    statuses = [path.comparability_status for path in paths]
    if any(path.requires_recompute for path in paths):
        statuses.append("structural-break")
        reason_codes.add("MAPPING_RECOMPUTE_REQUIRED")
    if missing_paths or not paths:
        statuses.append("structural-break")
    if not paths:
        reason_codes.add("MAPPING_PATHS_MISSING")
    for status in statuses:
        reason_codes.add(f"MAPPING_{status.upper().replace('-', '_')}")

    if statuses and set(statuses) == {"not-applicable"}:
        comparability = "not-applicable"
    elif "not-applicable" in statuses and any(item == "comparable" for item in statuses):
        comparability = "partial"
        reason_codes.add("MAPPING_PARTIAL_APPLICABILITY")
    else:
        comparability = max(
            statuses or ["structural-break"],
            key=lambda item: _COMPARABILITY_SEVERITY[item],
        )
    return (
        bundle,
        comparability,
        sorted(reason_codes),
        sorted(path.path_sha256 for path in paths),
    )


def _validate_mode_fact_set_binding(
    request: LabelMetricResultMaterializeRequest,
    fact_set: LabelFactSet,
) -> None:
    if request.taxonomy_mode == "native":
        if fact_set.target_label_version_id not in request.source_label_version_ids:
            raise ApiError(
                "LABEL_METRIC_FACT_SET_VERSION_MISMATCH",
                "native FactSet 的版本锚点必须属于 source_label_version_ids",
                409,
            )
        return
    if fact_set.target_label_version_id != request.target_label_version_id:
        raise ApiError(
            "LABEL_METRIC_FACT_SET_VERSION_MISMATCH",
            "normalized/recomputed FactSet 必须绑定请求的目标标签版本",
            409,
        )


def _validate_metric_definition(request: LabelMetricResultMaterializeRequest) -> None:
    if request.metric_key not in request.metric_definition_versions:
        raise ApiError(
            "LABEL_METRIC_DEFINITION_VERSION_REQUIRED",
            "标签指标必须冻结自身 metric definition version",
            422,
            details=[{"metric_key": request.metric_key}],
        )
    try:
        ZoneInfo(request.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ApiError("LABEL_METRIC_TIMEZONE_INVALID", "指标时区不是有效 IANA 时区", 422) from exc


def lock_label_metric_run_scope(
    session: Session,
    ctx: RequestContext,
    scope_request: LabelMetricRunScopeRequest,
    metric_definitions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve a client scope against current authoritative Heads at run acceptance."""

    if not metric_definitions:
        raise ApiError(
            "LABEL_METRIC_DEFINITIONS_REQUIRED",
            "标签指标运行必须至少冻结一个服务端指标定义",
            422,
        )
    metric_locks: dict[str, dict[str, Any]] = {}
    fact_set: LabelFactSet | None = None
    head: _FactSetAnchor | None = None
    bundle_sha256: str | None = None
    for definition in metric_definitions:
        metric_key = str(definition.get("metric_key") or "")
        metric_family = str(definition.get("metric_family") or "")
        definition_version = str(definition.get("calculator_ref") or "")
        unit = str(definition.get("unit") or "")
        if not all((metric_key, metric_family, definition_version, unit)):
            raise ApiError(
                "LABEL_METRIC_DEFINITION_INVALID",
                "服务端标签指标定义缺少 key、family、unit 或 version",
                409,
                details=[{"metric_key": metric_key or None}],
            )
        materialize_request = LabelMetricResultMaterializeRequest(
            metric_result_id=f"scope-lock-{sha256_document([metric_key])[:24]}",
            metric_key=metric_key,
            metric_family=metric_family,
            value=0,
            unit=unit,
            sample_size=1,
            source_run_id="scope-lock-placeholder",
            taxonomy_mode=scope_request.taxonomy_mode,
            source_label_version_ids=list(scope_request.source_label_version_ids),
            target_label_version_id=scope_request.target_label_version_id,
            mapping_bundle_id=scope_request.mapping_bundle_id,
            fact_namespace=scope_request.fact_namespace,
            fact_set_id=scope_request.fact_set_id,
            expected_fact_set_generation=scope_request.expected_fact_set_generation,
            fact_as_of=scope_request.fact_as_of,
            metric_definition_versions={metric_key: definition_version},
            timezone=scope_request.timezone,
            period_boundary=scope_request.period_boundary,
            denominator_definition=scope_request.denominator_definition,
            result_payload={},
        )
        _validate_source_versions(session, ctx, materialize_request)
        _validate_metric_definition(materialize_request)
        candidate_fact_set, candidate_head = _fact_set_anchor(
            session,
            ctx,
            materialize_request,
        )
        _validate_mode_fact_set_binding(materialize_request, candidate_fact_set)
        bundle, comparability, reason_codes, path_hashes = _mapping_comparability(
            session,
            ctx,
            materialize_request,
        )
        fact_set = candidate_fact_set
        head = candidate_head
        candidate_bundle_sha = bundle.canonical_manifest_sha256 if bundle else None
        if bundle_sha256 is not None and bundle_sha256 != candidate_bundle_sha:
            raise ApiError(
                "LABEL_METRIC_MAPPING_BUNDLE_DRIFT",
                "同一运行的标签指标解析到不同 Mapping Bundle",
                409,
            )
        bundle_sha256 = candidate_bundle_sha
        metric_locks[metric_key] = {
            "comparability_reason_codes": reason_codes,
            "comparability_status": comparability,
            "definition_version": definition_version,
            "metric_family": metric_family,
            "mapping_path_sha256s": path_hashes,
            "unit": unit,
        }

    assert fact_set is not None and head is not None
    lock_document = {
        "fact_as_of": _iso(fact_set.fact_as_of),
        "fact_namespace": fact_set.fact_namespace,
        "fact_set_generation": head.generation,
        "fact_set_head_id": head.fact_set_head_id,
        "fact_set_id": fact_set.fact_set_id,
        "fact_set_manifest_sha256": fact_set.manifest_sha256,
        "fact_set_source_manifest_sha256": fact_set.source_manifest_sha256,
        "mapping_bundle_id": scope_request.mapping_bundle_id,
        "mapping_bundle_sha256": bundle_sha256,
        "metric_locks": metric_locks,
        "schema_version": "auris.label-metric-run-scope-lock/1",
        "source_label_version_ids": list(scope_request.source_label_version_ids),
        "target_label_version_id": scope_request.target_label_version_id,
        "taxonomy_mode": scope_request.taxonomy_mode,
    }
    return {
        **lock_document,
        "scope_lock_sha256": sha256_document(lock_document),
    }


def materialize_label_metric_result(
    session: Session,
    ctx: RequestContext,
    request: LabelMetricResultMaterializeRequest,
    *,
    accepted_scope_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically freeze one label-derived MetricResult and its exact scope."""

    operation = f"{MATERIALIZE_OPERATION}:{request.metric_result_id}"
    body_hash = _body_hash(ctx, request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    if session.get(MetricResult, request.metric_result_id) is not None:
        raise ApiError(
            "LABEL_METRIC_RESULT_ALREADY_EXISTS",
            "MetricResult 是不可变快照，不能覆盖已有结果",
            409,
        )
    source_run = _source_run(session, ctx, request)
    _validate_source_versions(session, ctx, request)
    _validate_metric_definition(request)
    fact_set, head = _fact_set_anchor(
        session,
        ctx,
        request,
        accepted_scope_lock=accepted_scope_lock,
    )
    _validate_mode_fact_set_binding(request, fact_set)
    bundle, comparability, reason_codes, path_hashes = _mapping_comparability(
        session,
        ctx,
        request,
    )
    if accepted_scope_lock is not None:
        metric_locks = accepted_scope_lock.get("metric_locks") or {}
        accepted_metric_lock = metric_locks.get(request.metric_key)
        expected_metric_lock = {
            "comparability_reason_codes": reason_codes,
            "comparability_status": comparability,
            "definition_version": request.metric_definition_versions[request.metric_key],
            "metric_family": request.metric_family,
            "mapping_path_sha256s": path_hashes,
            "unit": request.unit,
        }
        actual_bundle_sha = bundle.canonical_manifest_sha256 if bundle else None
        if (
            accepted_metric_lock != expected_metric_lock
            or accepted_scope_lock.get("mapping_bundle_sha256") != actual_bundle_sha
        ):
            raise ApiError(
                "INSIGHT_LABEL_SCOPE_LOCK_DRIFT",
                "指标定义、Mapping 路径或可比性与受理时 scope lock 不一致",
                409,
                details=[{"metric_key": request.metric_key}],
            )

    if request.result_status != "value":
        outcome_comparability = {
            "coverage-gap": "partial",
            "recompute-required": "structural-break",
            "not-applicable": "not-applicable",
            "zero-denominator": "not-applicable",
        }[request.result_status]
        comparability = max(
            (comparability, outcome_comparability),
            key=lambda item: _COMPARABILITY_SEVERITY[item],
        )
        reason_codes = list(dict.fromkeys([*reason_codes, *request.reason_codes]))

    source_manifest = {
        "fact_as_of": _iso(fact_set.fact_as_of),
        "fact_namespace": fact_set.fact_namespace,
        "fact_set_generation": head.generation,
        "fact_set_id": fact_set.fact_set_id,
        "fact_set_manifest_sha256": fact_set.manifest_sha256,
        "fact_set_source_manifest_sha256": fact_set.source_manifest_sha256,
        "mapping_bundle_id": bundle.mapping_bundle_id if bundle else None,
        "mapping_bundle_sha256": bundle.canonical_manifest_sha256 if bundle else None,
        "mapping_path_sha256s": path_hashes,
        "schema_version": "auris.label-metric-source-manifest/1",
        "source_label_version_ids": list(request.source_label_version_ids),
        "target_label_version_id": request.target_label_version_id,
    }
    source_manifest_sha256 = sha256_document(source_manifest)
    scope_document = {
        "comparability_reason_codes": reason_codes,
        "comparability_status": comparability,
        "definition_version": request.metric_definition_versions[request.metric_key],
        "denominator_definition": request.denominator_definition,
        "fact_as_of": _iso(request.fact_as_of),
        "fact_namespace": request.fact_namespace,
        "fact_set_generation": head.generation,
        "fact_set_id": fact_set.fact_set_id,
        "fact_set_manifest_sha256": fact_set.manifest_sha256,
        "label_version_applicability": "required",
        "mapping_bundle_id": bundle.mapping_bundle_id if bundle else None,
        "mapping_bundle_sha256": bundle.canonical_manifest_sha256 if bundle else None,
        "metric_definition_versions": dict(request.metric_definition_versions),
        "metric_family": request.metric_family,
        "metric_key": request.metric_key,
        "period_boundary": request.period_boundary,
        "schema_version": "auris.label-metric-scope/1",
        "source_label_version_ids": list(request.source_label_version_ids),
        "source_manifest_sha256": source_manifest_sha256,
        "target_label_version_id": request.target_label_version_id,
        "taxonomy_mode": request.taxonomy_mode,
        "timezone": request.timezone,
    }
    scope_sha256 = sha256_document(scope_document)
    existing_scope = session.scalar(
        select(MetricResultLabelScope.metric_result_id).where(
            MetricResultLabelScope.tenant_id == ctx.tenant_id,
            MetricResultLabelScope.project_id == ctx.project_id,
            MetricResultLabelScope.scope_sha256 == scope_sha256,
        )
    )
    if existing_scope is not None:
        raise ApiError(
            "LABEL_METRIC_SCOPE_ALREADY_MATERIALIZED",
            "相同事实截止点与指标口径已存在不可变快照",
            409,
            details=[{"metric_result_id": existing_scope}],
        )

    metric_payload = {
        **request.result_payload,
        "comparability_reason_codes": reason_codes,
        "comparability_status": comparability,
        "definition_version": request.metric_definition_versions[request.metric_key],
        "immutable": True,
        "label_scope_sha256": scope_sha256,
        "label_version_applicability": "required",
        "metric_family": request.metric_family,
        "metric_key": request.metric_key,
        "metric_result_id": request.metric_result_id,
        "sample_size": request.sample_size,
        "result_status": request.result_status,
        "reason_codes": list(request.reason_codes),
        "snapshot_role": "aggregation",
        "source_run_id": request.source_run_id,
        "status": "materialized",
        "taxonomy_mode": request.taxonomy_mode,
        "unit": request.unit,
        "value": request.value,
    }
    root_trace_id = source_run.trace_id
    content_document = {
        "metric_payload": metric_payload,
        "metric_result_id": request.metric_result_id,
        "project_id": ctx.project_id,
        "root_trace_id": root_trace_id,
        "schema_version": "auris.label-metric-result/1",
        "scope_sha256": scope_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "tenant_id": ctx.tenant_id,
    }
    content_sha256 = sha256_document(content_document)
    metric = MetricResult(
        metric_result_id=request.metric_result_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status="materialized",
        content_sha256=content_sha256,
        source_manifest_sha256=source_manifest_sha256,
        scope_sha256=scope_sha256,
        root_trace_id=root_trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=root_trace_id,
        payload=metric_payload,
    )
    session.add(metric)
    session.flush()
    metric_scope_id = f"lms_{scope_sha256[:24]}"
    scope = MetricResultLabelScope(
        metric_scope_id=metric_scope_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        metric_result_id=metric.metric_result_id,
        taxonomy_mode=request.taxonomy_mode,
        source_label_version_ids=list(request.source_label_version_ids),
        target_label_version_id=request.target_label_version_id,
        mapping_bundle_id=bundle.mapping_bundle_id if bundle else None,
        mapping_bundle_sha256=bundle.canonical_manifest_sha256 if bundle else None,
        fact_namespace=fact_set.fact_namespace,
        fact_set_id=fact_set.fact_set_id,
        fact_set_manifest_sha256=fact_set.manifest_sha256,
        fact_set_generation=head.generation,
        fact_as_of=fact_set.fact_as_of,
        metric_definition_versions=dict(request.metric_definition_versions),
        timezone=request.timezone,
        period_boundary=request.period_boundary,
        denominator_definition=request.denominator_definition,
        label_version_applicability="required",
        comparability_status=comparability,
        comparability_reason_codes=reason_codes,
        scope_sha256=scope_sha256,
        source_manifest_sha256=source_manifest_sha256,
        content_sha256=content_sha256,
        root_trace_id=root_trace_id,
        action_trace_id=ctx.trace_id,
        trace_id=root_trace_id,
        payload={
            "fact_set_head_id": head.fact_set_head_id,
            "mapping_path_sha256s": path_hashes,
            "reason_codes": list(request.reason_codes),
            "result_status": request.result_status,
            "schema_version": "auris.metric-result-label-scope/1",
            "source_manifest": source_manifest,
        },
    )
    session.add(scope)
    session.flush()

    summary = {
        "comparability_reason_codes": reason_codes,
        "comparability_status": comparability,
        "content_sha256": content_sha256,
        "fact_as_of": _iso(fact_set.fact_as_of),
        "fact_set_generation": head.generation,
        "fact_set_id": fact_set.fact_set_id,
        "mapping_bundle_id": bundle.mapping_bundle_id if bundle else None,
        "metric_result_id": metric.metric_result_id,
        "metric_scope_id": scope.metric_scope_id,
        "scope_sha256": scope_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "taxonomy_mode": request.taxonomy_mode,
    }
    audit = record_audit(
        session,
        ctx,
        action="insight_metric.materialized",
        object_type="metric_result",
        object_id=metric.metric_result_id,
        after=summary,
        trace_id=root_trace_id,
    )
    outbox = enqueue_event(
        session,
        ctx,
        event_type="insight_metric.materialized",
        aggregate_type="metric_result",
        aggregate_id=metric.metric_result_id,
        payload=summary,
    )
    session.flush()
    response = {
        **summary,
        "audit_id": audit.audit_id,
        "outbox_event_id": outbox.event_id,
        "status": metric.status,
        "trace_id": root_trace_id,
    }
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    return response
