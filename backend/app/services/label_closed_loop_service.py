from __future__ import annotations

import hashlib
import json
import math
import uuid
from bisect import bisect_right
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_aggregation import (
    AggregationMode,
    AggregationPolicy,
    LabelAggregationEngine,
    LabelDefinition,
    LabelKind,
    RiskLevel,
    SourceType,
    SourceWeight,
    TimeSpan,
)
from app.domain.label_aggregation import (
    LabelObservation as DomainObservation,
)
from app.models import (
    Badcase,
    FeedbackExample,
    GoldSetVersion,
    JsonResource,
    LabelAggregate,
    LabelAggregateMember,
    LabelAggregationPolicyVersion,
    LabelAggregationRun,
    LabelCalibrationVersion,
    LabelExtractionRun,
    LabelFact,
    LabelFactHead,
    LabelNode,
    LabelObservation,
    LabelTaxonomySuggestion,
    LabelVersion,
    LabelVersionItem,
    PromptAsset,
    PromptVersion,
    ReleaseBundleHead,
    ReleaseDeployment,
    RunRecord,
    StorageObject,
)
from app.schemas.label_closed_loop import (
    LabelAggregationPolicyCreateRequest,
    LabelAggregationRunCreateRequest,
    LabelBadcaseCreateRequest,
    LabelCalibrationVersionCreateRequest,
    LabelExtractionRunCreateRequest,
    LabelObservationCreateRequest,
)
from app.schemas.label_facts import LabelFactRevisionCreate
from app.services.audit_service import record_audit
from app.services.label_fact_temporal_service import (
    append_label_fact_revision,
    label_fact_logical_key_sha256,
)
from app.services.label_lifecycle_compat_service import label_version_item_definition_sha256
from app.services.outbox_service import enqueue_event
from app.services.public_run_projection_service import public_run_projection
from app.services.resource_service import upsert_resource

LABEL_EXTRACTION_PUBLIC_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "extraction_run_id",
        "tenant_id",
        "project_id",
        "label_version_id",
        "prompt_version_id",
        "model_version",
        "schema_version",
        "status",
        "subject_scope",
        "subject_refs",
        "input_sha256",
        "observation_count",
        "aggregation_policy_version_id",
        "source_bindings",
        "manifest_sha256",
        "release_head_lock",
        "aggregation_run_id",
        "aggregate_ids",
        "trace_id",
        "created_at",
        "next_actions",
    }
)
LABEL_EXTRACTION_PUBLIC_SOURCE_BINDING_FIELDS = frozenset(
    {"source_family", "source_type", "correlation_group_id"}
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _rooted_context(ctx: RequestContext, root_trace_id: str) -> RequestContext:
    if ctx.trace_id == root_trace_id:
        return ctx
    return replace(
        ctx,
        trace_id=root_trace_id,
        parent_trace_id=ctx.trace_id,
        correlation_id=ctx.correlation_id or root_trace_id,
    )


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _scoped_label_version(
    session: Session, ctx: RequestContext, label_version_id: str
) -> LabelVersion:
    version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.label_version_id == label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
    )
    if version is None:
        raise ApiError("LABEL_VERSION_NOT_FOUND", "当前租户项目中不存在指定标签版本", 404)
    return version


def _scoped_extraction_prompt(
    session: Session,
    ctx: RequestContext,
    *,
    prompt_version_id: str,
    label_version_id: str,
    model_version: str,
    schema_version: str,
    execution_mode: str,
) -> tuple[PromptAsset, PromptVersion]:
    prompt = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == prompt_version_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    if prompt is None:
        raise ApiError("PROMPT_VERSION_NOT_FOUND", "抽取运行锁定的 PromptVersion 不存在", 404)
    asset = session.scalar(
        select(PromptAsset).where(
            PromptAsset.prompt_asset_id == prompt.prompt_asset_id,
            PromptAsset.tenant_id == ctx.tenant_id,
            PromptAsset.project_id == ctx.project_id,
        )
    )
    if asset is None or asset.capability != "labeling":
        raise ApiError(
            "EXTRACTION_PROMPT_CAPABILITY_INVALID",
            "抽取运行只能锁定 labeling PromptAsset",
            409,
        )
    mismatches: list[dict[str, Any]] = [
        {"field": field, "expected": expected, "actual": actual}
        for field, expected, actual in (
            ("label_version_id", label_version_id, prompt.label_version_id),
            ("model_version", model_version, prompt.model_version),
            ("schema_version", schema_version, prompt.schema_version),
        )
        if actual != expected
    ]
    allowed_statuses = (
        {"approved", "published"}
        if execution_mode == "production"
        else {"draft", "approved", "published"}
    )
    if prompt.status not in allowed_statuses:
        mismatches.append(
            {
                "field": "prompt_status",
                "expected": sorted(allowed_statuses),
                "actual": prompt.status,
            }
        )
    if mismatches:
        raise ApiError(
            "EXTRACTION_PROMPT_BINDING_MISMATCH",
            "PromptVersion 与抽取运行锁定的标签、模型或 Schema 不一致",
            409,
            details=mismatches,
        )
    return asset, prompt


def _scoped_extraction_policy(
    session: Session,
    ctx: RequestContext,
    *,
    policy_version_id: str | None,
    label_version_id: str,
) -> LabelAggregationPolicyVersion | None:
    if policy_version_id is None:
        return None
    policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    if policy is None:
        raise ApiError(
            "AGGREGATION_POLICY_NOT_FOUND",
            "抽取运行锁定的聚合策略不存在",
            404,
        )
    if policy.label_version_id != label_version_id or policy.status != "active":
        raise ApiError(
            "EXTRACTION_POLICY_BINDING_MISMATCH",
            "抽取运行的聚合策略必须绑定同一标签版本且处于 active",
            409,
        )
    return policy


def _lock_production_release_head(
    session: Session,
    ctx: RequestContext,
    body: LabelExtractionRunCreateRequest,
) -> dict[str, Any] | None:
    if body.execution_mode != "production":
        return None
    head = session.scalar(
        select(ReleaseBundleHead)
        .where(
            ReleaseBundleHead.tenant_id == ctx.tenant_id,
            ReleaseBundleHead.project_id == ctx.project_id,
            ReleaseBundleHead.environment == "production",
        )
        .with_for_update()
    )
    if head is None or head.status != "active":
        raise ApiError(
            "EXTRACTION_RELEASE_HEAD_REQUIRED",
            "production 抽取必须在事务内锁定 active Bundle Head",
            409,
        )
    expected = {
        "label_version_id": body.label_version_id,
        "prompt_version_id": body.prompt_version_id,
        "model_version": body.model_version,
        "aggregation_policy_version_id": body.aggregation_policy_version_id,
    }
    mismatches = [
        {"field": field, "expected": value, "actual": getattr(head, field)}
        for field, value in expected.items()
        if getattr(head, field) != value
    ]
    if mismatches:
        raise ApiError(
            "EXTRACTION_RELEASE_HEAD_BINDING_MISMATCH",
            "production 抽取请求未绑定当前 active Bundle Head",
            409,
            details=mismatches,
        )
    deployment = session.scalar(
        select(ReleaseDeployment)
        .where(
            ReleaseDeployment.tenant_id == ctx.tenant_id,
            ReleaseDeployment.project_id == ctx.project_id,
            ReleaseDeployment.deployment_id == head.active_deployment_id,
        )
        .with_for_update()
    )
    deployment_mismatches: list[dict[str, Any]] = []
    if deployment is None:
        deployment_mismatches.append(
            {"field": "active_deployment_id", "expected": head.active_deployment_id, "actual": None}
        )
    else:
        expected_deployment = {
            "environment": head.environment,
            "bundle_sha256": head.active_bundle_sha256,
            "label_version_id": head.label_version_id,
            "prompt_version_id": head.prompt_version_id,
            "model_version": head.model_version,
            "aggregation_policy_version_id": head.aggregation_policy_version_id,
            "status": "completed",
            "rollout_percentage": 100,
        }
        deployment_mismatches = [
            {"field": field, "head": value, "deployment": getattr(deployment, field)}
            for field, value in expected_deployment.items()
            if getattr(deployment, field) != value
        ]
    if deployment_mismatches:
        raise ApiError(
            "EXTRACTION_RELEASE_HEAD_DRIFT",
            "active Bundle Head 与其部署投影不一致",
            409,
            details=deployment_mismatches,
        )
    return {
        "environment": head.environment,
        "generation": head.generation,
        "active_deployment_id": head.active_deployment_id,
        "active_bundle_sha256": head.active_bundle_sha256,
    }


def _extraction_manifest_document(
    *,
    label: LabelVersion,
    prompt: PromptVersion,
    policy: LabelAggregationPolicyVersion | None,
    model_version: str,
    schema_version: str,
    subject_scope: str,
    subject_refs: list[dict[str, Any]],
    source_bindings: list[dict[str, Any]],
    input_sha256: str,
    execution_mode: str,
    release_head_lock: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_subjects = sorted(
        (
            {
                "subject_key": str(item["subject_key"]),
                **(
                    {"evidence_ref": str(item["evidence_ref"])}
                    if item.get("evidence_ref") is not None
                    else {}
                ),
                **(
                    {"data_range": str(item["data_range"])}
                    if item.get("data_range") is not None
                    else {}
                ),
            }
            for item in subject_refs
        ),
        key=lambda item: item["subject_key"],
    )
    return {
        "manifest_version": "label-extraction-manifest/1",
        "label": {
            "label_version_id": label.label_version_id,
            "resource_version": label.resource_version,
        },
        "prompt": {
            "prompt_version_id": prompt.prompt_version_id,
            "content_sha256": prompt.content_sha256,
        },
        "model_version": model_version,
        "schema": {
            "schema_version": schema_version,
            "output_schema_sha256": canonical_sha256(prompt.output_schema),
        },
        "aggregation_policy": (
            {
                "policy_version_id": policy.policy_version_id,
                "canonical_sha256": policy.canonical_sha256,
            }
            if policy is not None
            else None
        ),
        "subject_scope": subject_scope,
        "subject_refs": normalized_subjects,
        "source_bindings": sorted(source_bindings, key=lambda item: str(item["source_family"])),
        "input_sha256": input_sha256,
        "execution_mode": execution_mode,
        "release_head_lock": release_head_lock,
    }


def observation_data(record: LabelObservation) -> dict[str, Any]:
    return {
        "observation_id": record.observation_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "extraction_run_id": record.extraction_run_id,
        "subject_scope": record.subject_scope,
        "subject_key": record.subject_key,
        "evidence_ref": record.evidence_ref,
        "label_version_id": record.label_version_id,
        "raw_label": record.raw_label,
        "label_id": record.label_id,
        "value": record.value_json,
        "value_type": record.value_type,
        "source_family": record.source_family,
        "source_type": record.source_type,
        "model_version": record.model_version,
        "prompt_version_id": record.prompt_version_id,
        "schema_version": record.schema_version,
        "calibration_version_id": record.calibration_version_id,
        "raw_confidence": record.raw_confidence,
        "calibrated_confidence": record.calibrated_confidence,
        "source_lineage": record.payload.get("source_lineage", {}),
        "evidence_verification": record.payload.get("evidence_verification", {}),
        "input_sha256": record.input_sha256,
        "output_sha256": record.output_sha256,
        "status": record.status,
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
    }


def calibration_version_data(record: LabelCalibrationVersion) -> dict[str, Any]:
    return {
        "calibration_version_id": record.calibration_version_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "label_version_id": record.label_version_id,
        "label_id": record.label_id,
        "source_family": record.source_family,
        "version": record.version,
        "method": record.method,
        "status": record.status,
        "gold_set_version_id": record.gold_set_version_id,
        "sample_count": record.sample_count,
        "parameters": record.parameters,
        "metrics": record.metrics,
        "training_manifest_sha256": record.training_manifest_sha256,
        "content_sha256": record.content_sha256,
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
    }


def create_label_calibration_version(
    session: Session,
    ctx: RequestContext,
    body: LabelCalibrationVersionCreateRequest,
) -> dict[str, Any]:
    _scoped_label_version(session, ctx, body.label_version_id)
    if body.label_id != "*":
        item = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.tenant_id == ctx.tenant_id,
                LabelVersionItem.project_id == ctx.project_id,
                LabelVersionItem.label_version_id == body.label_version_id,
                LabelVersionItem.label_id == body.label_id,
                LabelVersionItem.status == "active",
            )
        )
        if item is None:
            raise ApiError(
                "CALIBRATION_LABEL_NOT_IN_VERSION",
                "校准器 label_id 必须属于锁定 LabelVersion，或显式使用 * 全局回退",
                409,
            )
    gold = session.scalar(
        select(GoldSetVersion).where(
            GoldSetVersion.gold_set_version_id == body.gold_set_version_id,
            GoldSetVersion.tenant_id == ctx.tenant_id,
            GoldSetVersion.project_id == ctx.project_id,
        )
    )
    if gold is None:
        raise ApiError("CALIBRATION_GOLD_NOT_FOUND", "当前租户项目中不存在锁定 Gold 版本", 404)
    if gold.label_version != body.label_version_id:
        raise ApiError(
            "CALIBRATION_GOLD_LABEL_VERSION_MISMATCH",
            "校准器与 Gold 必须绑定同一 LabelVersion",
            409,
        )
    minimum_samples = {"isotonic": 200, "platt": 100, "global-conservative": 50}[body.method]
    stable = (
        gold.status == "published"
        and gold.annotation_count >= minimum_samples
        and gold.cohen_kappa_defined
        and gold.observed_agreement_ppm >= 800_000
        and gold.cohen_kappa_micros >= 600_000
    )
    if body.status == "published" and not stable:
        raise ApiError(
            "CALIBRATION_GOLD_NOT_STABLE",
            "发布校准器所需 Gold 样本量或一致性不足",
            409,
            details=[
                {
                    "method": body.method,
                    "minimum_samples": minimum_samples,
                    "actual_samples": gold.annotation_count,
                    "observed_agreement_ppm": gold.observed_agreement_ppm,
                    "cohen_kappa_micros": gold.cohen_kappa_micros,
                }
            ],
        )
    if session.get(LabelCalibrationVersion, body.calibration_version_id) is not None:
        raise ApiError("CALIBRATION_VERSION_ALREADY_EXISTS", "校准器版本 ID 已存在", 409)
    content_document = {
        "label_version_id": body.label_version_id,
        "label_id": body.label_id,
        "source_family": body.source_family,
        "version": body.version,
        "method": body.method,
        "gold_set_version_id": gold.gold_set_version_id,
        "sample_count": gold.annotation_count,
        "parameters": body.parameters,
        "metrics": body.metrics,
        "training_manifest_sha256": gold.annotation_manifest_sha256,
    }
    content_sha256 = canonical_sha256(content_document)
    duplicate = session.scalar(
        select(LabelCalibrationVersion.calibration_version_id).where(
            LabelCalibrationVersion.tenant_id == ctx.tenant_id,
            LabelCalibrationVersion.project_id == ctx.project_id,
            LabelCalibrationVersion.content_sha256 == content_sha256,
        )
    )
    if duplicate is not None:
        raise ApiError(
            "CALIBRATION_VERSION_CONTENT_EXISTS",
            "相同校准器内容已物化",
            409,
            details=[{"calibration_version_id": duplicate}],
        )
    record = LabelCalibrationVersion(
        calibration_version_id=body.calibration_version_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        label_version_id=body.label_version_id,
        label_id=body.label_id,
        source_family=body.source_family,
        version=body.version,
        method=body.method,
        status=body.status,
        gold_set_version_id=gold.gold_set_version_id,
        sample_count=gold.annotation_count,
        parameters=body.parameters,
        metrics=body.metrics,
        training_manifest_sha256=gold.annotation_manifest_sha256,
        content_sha256=content_sha256,
        trace_id=ctx.trace_id,
        payload={
            "gold_sample_manifest_sha256": gold.sample_manifest_sha256,
            "gold_observed_agreement_ppm": gold.observed_agreement_ppm,
            "gold_cohen_kappa_micros": gold.cohen_kappa_micros,
            "minimum_samples": minimum_samples,
            "server_locked": True,
        },
    )
    session.add(record)
    session.flush()
    data = calibration_version_data(record)
    record_audit(
        session,
        ctx,
        action="label_calibration_version.created",
        object_type="label_calibration_version",
        object_id=record.calibration_version_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_calibration_version.created",
        aggregate_type="label_calibration_version",
        aggregate_id=record.calibration_version_id,
        payload=data,
    )
    return data


def list_label_calibration_versions(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str | None = None,
    source_family: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(LabelCalibrationVersion).where(
        LabelCalibrationVersion.tenant_id == ctx.tenant_id,
        LabelCalibrationVersion.project_id == ctx.project_id,
    )
    if label_version_id:
        statement = statement.where(LabelCalibrationVersion.label_version_id == label_version_id)
    if source_family:
        statement = statement.where(LabelCalibrationVersion.source_family == source_family)
    if status:
        statement = statement.where(LabelCalibrationVersion.status == status)
    records = session.scalars(
        statement.order_by(LabelCalibrationVersion.created_at.desc()).limit(limit)
    )
    return [calibration_version_data(record) for record in records]


def get_label_calibration_version(
    session: Session, ctx: RequestContext, calibration_version_id: str
) -> dict[str, Any]:
    record = session.scalar(
        select(LabelCalibrationVersion).where(
            LabelCalibrationVersion.calibration_version_id == calibration_version_id,
            LabelCalibrationVersion.tenant_id == ctx.tenant_id,
            LabelCalibrationVersion.project_id == ctx.project_id,
        )
    )
    if record is None:
        raise ApiError("CALIBRATION_VERSION_NOT_FOUND", "当前租户项目中不存在校准器版本", 404)
    return calibration_version_data(record)


def _calibrated_probability(record: LabelCalibrationVersion, raw_confidence: float) -> float:
    probability = min(max(float(raw_confidence), 1e-12), 1 - 1e-12)
    if record.method == "isotonic":
        xs = [float(item) for item in record.parameters["x"]]
        ys = [float(item) for item in record.parameters["y"]]
        right = bisect_right(xs, probability)
        if right == 0:
            result = ys[0]
        elif right >= len(xs):
            result = ys[-1]
        else:
            left = right - 1
            width = xs[right] - xs[left]
            ratio = (probability - xs[left]) / width
            result = ys[left] + ratio * (ys[right] - ys[left])
    elif record.method == "platt":
        raw_logit = math.log(probability / (1 - probability))
        score = float(record.parameters["a"]) * raw_logit + float(record.parameters["b"])
        result = 1 / (1 + math.exp(-max(min(score, 40), -40)))
    else:
        shrink = float(record.parameters["shrink"])
        cap = float(record.parameters.get("cap", 0.95))
        result = 0.5 + (probability - 0.5) * shrink
        result = min(max(result, 1 - cap), cap)
    return round(min(max(result, 0.0), 1.0), 15)


def _locked_source_binding(
    extraction_run: LabelExtractionRun,
    body: LabelObservationCreateRequest,
) -> dict[str, Any] | None:
    bindings = (extraction_run.payload or {}).get("source_bindings")
    if not isinstance(bindings, list) or not bindings:
        return None
    matches = [
        item
        for item in bindings
        if isinstance(item, dict) and item.get("source_family") == body.source_family
    ]
    if len(matches) != 1:
        raise ApiError(
            "LABEL_OBSERVATION_SOURCE_NOT_LOCKED",
            "Observation source_family 不属于抽取运行锁定的来源谱系",
            409,
            details=[
                {
                    "actual_source_family": body.source_family,
                    "allowed_source_families": sorted(
                        str(item.get("source_family"))
                        for item in bindings
                        if isinstance(item, dict) and item.get("source_family")
                    ),
                }
            ],
        )
    binding = matches[0]
    if binding.get("source_type") != body.source_type:
        raise ApiError(
            "LABEL_OBSERVATION_SOURCE_TYPE_MISMATCH",
            "Observation source_type 与抽取运行锁定来源不一致",
            409,
        )
    return binding


def _verify_observation_evidence(
    session: Session,
    ctx: RequestContext,
    extraction_run: LabelExtractionRun,
    body: LabelObservationCreateRequest,
) -> dict[str, Any]:
    evidence_type = body.evidence_ref.type.strip().replace("_", "-").casefold()
    subject_refs = [
        item
        for item in extraction_run.subject_refs
        if isinstance(item, dict)
        and str(item.get("subject_key") or item.get("id") or "") == body.subject_key
    ]
    expected_refs = sorted(
        {str(item["evidence_ref"]) for item in subject_refs if item.get("evidence_ref") is not None}
    )
    if expected_refs and body.evidence_ref.id not in expected_refs:
        raise ApiError(
            "LABEL_OBSERVATION_EVIDENCE_SCOPE_MISMATCH",
            "Observation evidence_ref 不属于运行锁定的 subject 证据",
            409,
            details=[
                {
                    "subject_key": body.subject_key,
                    "expected_evidence_refs": expected_refs,
                    "actual_evidence_ref": body.evidence_ref.id,
                }
            ],
        )
    if evidence_type in {"storage-object", "object-storage"}:
        storage_object = session.scalar(
            select(StorageObject).where(
                StorageObject.storage_object_id == body.evidence_ref.id,
                StorageObject.tenant_id == ctx.tenant_id,
                StorageObject.project_id == ctx.project_id,
            )
        )
        if storage_object is None:
            raise ApiError(
                "LABEL_OBSERVATION_EVIDENCE_NOT_FOUND",
                "证据对象不属于当前租户项目",
                404,
            )
        if (
            storage_object.content_sha256 is None
            or storage_object.content_sha256 != body.evidence_ref.sha256
            or storage_object.status in {"failed", "deleted", "quarantined"}
        ):
            raise ApiError(
                "LABEL_OBSERVATION_EVIDENCE_HASH_MISMATCH",
                "证据对象内容哈希或状态不满足事实物化要求",
                409,
            )
        return {
            "verified": True,
            "method": "scoped-storage-object-content-sha256",
            "storage_object_id": storage_object.storage_object_id,
        }
    return {
        "verified": bool(expected_refs),
        "method": "locked-subject-evidence-ref" if expected_refs else "unverified-ref",
        "expected_evidence_refs": expected_refs,
    }


def _resolve_observation_calibration(
    session: Session,
    ctx: RequestContext,
    body: LabelObservationCreateRequest,
    *,
    strong_manifest: bool,
) -> tuple[float | None, dict[str, Any]]:
    if not body.calibration_version_id:
        if strong_manifest and body.calibrated_confidence is not None:
            raise ApiError(
                "CLIENT_CALIBRATED_CONFIDENCE_FORBIDDEN",
                "强 Manifest Observation 的 calibrated_confidence 只能由服务端校准器计算",
                422,
            )
        return (
            body.calibrated_confidence,
            {"authority": "legacy-unverified" if not strong_manifest else "none"},
        )
    calibration = session.scalar(
        select(LabelCalibrationVersion).where(
            LabelCalibrationVersion.calibration_version_id == body.calibration_version_id,
            LabelCalibrationVersion.tenant_id == ctx.tenant_id,
            LabelCalibrationVersion.project_id == ctx.project_id,
        )
    )
    if calibration is None:
        if strong_manifest:
            raise ApiError(
                "LABEL_CALIBRATION_VERSION_NOT_FOUND",
                "Observation 引用的服务端校准器不存在",
                409,
            )
        return body.calibrated_confidence, {"authority": "legacy-unverified"}
    mismatches: list[dict[str, Any]] = []
    expected_values = {
        "status": "published",
        "label_version_id": body.label_version_id,
        "source_family": body.source_family,
    }
    for field, expected in expected_values.items():
        actual = getattr(calibration, field)
        if actual != expected:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    if calibration.label_id not in {"*", str(body.label_id or "")}:
        mismatches.append(
            {
                "field": "label_id",
                "expected": ["*", body.label_id],
                "actual": calibration.label_id,
            }
        )
    if mismatches:
        raise ApiError(
            "LABEL_CALIBRATION_BINDING_MISMATCH",
            "Observation 与校准器锁定范围不一致",
            409,
            details=mismatches,
        )
    calibrated = _calibrated_probability(calibration, body.raw_confidence)
    if (
        body.calibrated_confidence is not None
        and abs(body.calibrated_confidence - calibrated) > 1e-12
    ):
        raise ApiError(
            "CLIENT_CALIBRATION_RESULT_MISMATCH",
            "客户端声明的 calibrated_confidence 与服务端确定性结果不一致",
            409,
        )
    return calibrated, {
        "authority": "server-locked",
        "calibration_version_id": calibration.calibration_version_id,
        "content_sha256": calibration.content_sha256,
        "method": calibration.method,
    }


def create_label_observation(
    session: Session,
    ctx: RequestContext,
    body: LabelObservationCreateRequest,
) -> dict[str, Any]:
    if body.source_type == "human-confirmed":
        raise ApiError(
            "LABEL_OBSERVATION_HUMAN_SOURCE_FORBIDDEN",
            "人工确认必须进入 FeedbackExample/LabelFact，不能伪装为原始 Observation",
            422,
        )
    extraction_run = session.scalar(
        select(LabelExtractionRun).where(
            LabelExtractionRun.extraction_run_id == body.extraction_run_id,
            LabelExtractionRun.tenant_id == ctx.tenant_id,
            LabelExtractionRun.project_id == ctx.project_id,
        )
    )
    if extraction_run is None:
        raise ApiError(
            "LABEL_EXTRACTION_RUN_NOT_FOUND",
            "当前租户项目中不存在 Observation 绑定的抽取运行",
            404,
        )
    _revalidate_extraction_manifest(session, ctx, extraction_run)
    fact_ctx = _rooted_context(ctx, extraction_run.trace_id)
    _validate_observation_run_binding(extraction_run, body)
    source_binding = _locked_source_binding(extraction_run, body)
    evidence_verification = _verify_observation_evidence(session, fact_ctx, extraction_run, body)
    calibrated_confidence, calibration_provenance = _resolve_observation_calibration(
        session,
        fact_ctx,
        body,
        strong_manifest=source_binding is not None,
    )
    policy_version_id = str(
        (extraction_run.payload or {}).get("aggregation_policy_version_id") or ""
    )
    if policy_version_id:
        locked_policy = get_aggregation_policy(session, fact_ctx, policy_version_id)
        if locked_policy.mode == "l2":
            expected_calibration_id = _policy_calibration_version_id(
                locked_policy,
                label_id=body.label_id,
                source_family=body.source_family,
            )
            l2_blockers = []
            if body.calibration_version_id != expected_calibration_id:
                l2_blockers.append("CALIBRATION_VERSION_MISMATCH")
            if (
                calibrated_confidence is None
                or calibration_provenance.get("authority") != "server-locked"
            ):
                l2_blockers.append("SERVER_CALIBRATION_REQUIRED")
            if source_binding is None:
                l2_blockers.append("SERVER_SOURCE_LINEAGE_REQUIRED")
            if not evidence_verification.get("verified"):
                l2_blockers.append("VERIFIED_EVIDENCE_REQUIRED")
            if l2_blockers:
                raise ApiError(
                    "L2_OBSERVATION_TRUST_CHAIN_INCOMPLETE",
                    "L2 Observation 的校准、来源或证据强事实链不完整",
                    409,
                    details=[{"reason_codes": l2_blockers}],
                )
    _scoped_label_version(session, fact_ctx, extraction_run.label_version_id)
    if body.label_id is not None:
        version_item = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.tenant_id == fact_ctx.tenant_id,
                LabelVersionItem.project_id == fact_ctx.project_id,
                LabelVersionItem.label_version_id == extraction_run.label_version_id,
                LabelVersionItem.label_id == body.label_id,
                LabelVersionItem.status == "active",
            )
        )
        if version_item is None:
            raise ApiError(
                "LABEL_OBSERVATION_LABEL_OUT_OF_VERSION",
                "Observation 的 label_id 不属于抽取运行锁定的标签版本",
                409,
            )
    existing = session.get(LabelObservation, body.observation_id)
    if existing is not None:
        raise ApiError(
            "LABEL_OBSERVATION_IMMUTABLE",
            "LabelObservation 为不可变事实，不能覆盖已有 observation_id",
            409,
        )
    payload = body.model_dump(mode="json", exclude_none=True)
    record = LabelObservation(
        observation_id=body.observation_id,
        tenant_id=fact_ctx.tenant_id,
        project_id=fact_ctx.project_id,
        extraction_run_id=body.extraction_run_id,
        subject_scope=body.subject_scope,
        subject_key=body.subject_key,
        evidence_ref=body.evidence_ref.model_dump(mode="json", exclude_none=True),
        evidence_sha256=body.evidence_ref.sha256,
        label_version_id=body.label_version_id,
        raw_label=body.raw_label,
        label_id=body.label_id,
        value_type=body.value_type,
        value_json=body.value,
        source_family=body.source_family,
        source_type=body.source_type,
        model_version=body.model_version,
        prompt_version_id=body.prompt_version_id,
        schema_version=body.schema_version,
        calibration_version_id=body.calibration_version_id,
        raw_confidence=body.raw_confidence,
        calibrated_confidence=calibrated_confidence,
        input_sha256=body.input_sha256,
        output_sha256=body.output_sha256,
        status="materialized",
        trace_id=fact_ctx.trace_id,
        payload={
            **payload,
            "root_trace_id": fact_ctx.trace_id,
            "materialization_trace_id": ctx.trace_id,
            "server_payload_sha256": canonical_sha256(payload),
            "source_lineage": (
                {
                    **source_binding,
                    "server_locked": True,
                }
                if source_binding is not None
                else {
                    "source_family": body.source_family,
                    "source_type": body.source_type,
                    "server_locked": False,
                    "legacy_compatibility": True,
                }
            ),
            "evidence_verification": evidence_verification,
            "calibration_provenance": calibration_provenance,
        },
    )
    session.add(record)
    session.flush()
    data = observation_data(record)
    record_audit(
        session,
        fact_ctx,
        action="label_observation.created",
        object_type="label_observation",
        object_id=record.observation_id,
        after=data,
    )
    enqueue_event(
        session,
        fact_ctx,
        event_type="label_observation.created",
        aggregate_type="label_observation",
        aggregate_id=record.observation_id,
        payload=data,
    )
    return data


def _validate_observation_run_binding(
    extraction_run: LabelExtractionRun,
    body: LabelObservationCreateRequest,
) -> None:
    expected = {
        "label_version_id": extraction_run.label_version_id,
        "prompt_version_id": extraction_run.prompt_version_id,
        "model_version": extraction_run.model_version,
        "schema_version": extraction_run.schema_version,
        "subject_scope": extraction_run.subject_scope,
        "input_sha256": extraction_run.input_sha256,
    }
    actual = {
        "label_version_id": body.label_version_id,
        "prompt_version_id": body.prompt_version_id,
        "model_version": body.model_version,
        "schema_version": body.schema_version,
        "subject_scope": body.subject_scope,
        "input_sha256": body.input_sha256,
    }
    mismatches: list[dict[str, Any]] = [
        {"field": field, "expected": expected_value, "actual": actual[field]}
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    ]
    allowed_subject_keys = {
        str(item.get("subject_key") or item.get("id") or "")
        for item in extraction_run.subject_refs
        if isinstance(item, dict)
    }
    allowed_subject_keys.discard("")
    if not allowed_subject_keys or body.subject_key not in allowed_subject_keys:
        mismatches.append(
            {
                "field": "subject_key",
                "expected": sorted(allowed_subject_keys),
                "actual": body.subject_key,
            }
        )
    if mismatches:
        raise ApiError(
            "LABEL_OBSERVATION_RUN_BINDING_MISMATCH",
            "Observation 与抽取运行锁定的版本、输入或 subject 范围不一致",
            409,
            details=mismatches,
        )


def _server_locked_l2_calibrations(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    calibration_versions: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    version_ids = {str(value) for value in calibration_versions.values() if str(value)}
    records = list(
        session.scalars(
            select(LabelCalibrationVersion).where(
                LabelCalibrationVersion.tenant_id == ctx.tenant_id,
                LabelCalibrationVersion.project_id == ctx.project_id,
                LabelCalibrationVersion.calibration_version_id.in_(version_ids),
            )
        )
    )
    by_id = {record.calibration_version_id: record for record in records}
    locks: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, Any]] = []
    for calibration_key, raw_version_id in sorted(calibration_versions.items()):
        version_id = str(raw_version_id)
        record = by_id.get(version_id)
        if record is None:
            invalid.append(
                {
                    "calibration_key": calibration_key,
                    "calibration_version_id": version_id,
                    "reason": "SERVER_LOCK_NOT_FOUND",
                }
            )
            continue
        key_label_id, _, key_source_family = calibration_key.partition("::")
        expected_label_id = key_label_id if key_source_family else "*"
        expected_source_family = key_source_family or calibration_key
        stable = (
            record.status == "published"
            and record.label_version_id == label_version_id
            and record.label_id in {"*", expected_label_id}
            and record.source_family == expected_source_family
            and record.sample_count >= 50
            and bool(record.payload.get("server_locked"))
        )
        if not stable:
            invalid.append(
                {
                    "calibration_key": calibration_key,
                    "calibration_version_id": version_id,
                    "reason": "CALIBRATION_NOT_STABLE",
                }
            )
            continue
        locks[calibration_key] = {
            "calibration_version_id": record.calibration_version_id,
            "content_sha256": record.content_sha256,
            "gold_set_version_id": record.gold_set_version_id,
            "training_manifest_sha256": record.training_manifest_sha256,
            "sample_count": record.sample_count,
            "method": record.method,
            "label_id": record.label_id,
            "source_family": record.source_family,
        }
    if invalid:
        raise ApiError(
            "L2_CALIBRATION_NOT_SERVER_LOCKED",
            "L2 只能使用服务端已发布且稳定的校准版本",
            409,
            details=invalid,
        )
    return locks


def _policy_calibration_version_id(
    policy: LabelAggregationPolicyVersion,
    *,
    label_id: str | None,
    source_family: str,
) -> str | None:
    mappings = policy.calibration_versions or {}
    specific_key = f"{label_id}::{source_family}" if label_id else None
    value = mappings.get(specific_key) if specific_key else None
    return str(value or mappings.get(source_family) or "") or None


def create_label_extraction_projection(
    session: Session,
    ctx: RequestContext,
    body: LabelExtractionRunCreateRequest,
    record: RunRecord,
) -> None:
    label = _scoped_label_version(session, ctx, body.label_version_id)
    artifact_status = label.artifact_status or label.status
    if body.execution_mode == "production" and artifact_status != "published":
        raise ApiError(
            "EXTRACTION_LABEL_VERSION_NOT_PUBLISHED",
            "production 抽取只能锁定 published LabelVersion",
            409,
        )
    _, prompt = _scoped_extraction_prompt(
        session,
        ctx,
        prompt_version_id=body.prompt_version_id,
        label_version_id=body.label_version_id,
        model_version=body.model_version,
        schema_version=body.schema_version,
        execution_mode=body.execution_mode,
    )
    policy = _scoped_extraction_policy(
        session,
        ctx,
        policy_version_id=body.aggregation_policy_version_id,
        label_version_id=body.label_version_id,
    )
    release_head_lock = _lock_production_release_head(session, ctx, body)
    if session.get(LabelExtractionRun, body.extraction_run_id) is not None:
        raise ApiError("LABEL_EXTRACTION_RUN_ALREADY_EXISTS", "标签抽取运行 ID 已存在", 409)
    subject_refs = [item.model_dump(mode="json", exclude_none=True) for item in body.subject_refs]
    if policy is not None and policy.mode == "l2":
        missing_evidence_subjects = sorted(
            str(item["subject_key"]) for item in subject_refs if not item.get("evidence_ref")
        )
        if missing_evidence_subjects:
            raise ApiError(
                "L2_EXTRACTION_EVIDENCE_MANIFEST_REQUIRED",
                "L2 抽取必须为每个 subject 锁定 evidence_ref",
                409,
                details=[{"subject_keys": missing_evidence_subjects}],
            )
    source_bindings = [
        {
            **item.model_dump(mode="json"),
            "correlation_group_id": (
                "lcg_"
                + canonical_sha256(
                    [
                        body.extraction_run_id,
                        item.source_family,
                        item.source_type,
                        item.provider,
                        item.adapter,
                        body.model_version,
                        body.prompt_version_id,
                    ]
                )[:24]
            ),
        }
        for item in body.source_bindings
    ]
    manifest = _extraction_manifest_document(
        label=label,
        prompt=prompt,
        policy=policy,
        model_version=body.model_version,
        schema_version=body.schema_version,
        subject_scope=body.subject_scope,
        subject_refs=subject_refs,
        source_bindings=source_bindings,
        input_sha256=body.input_sha256,
        execution_mode=body.execution_mode,
        release_head_lock=release_head_lock,
    )
    record.payload = {**(record.payload or {}), "release_head_lock": release_head_lock}
    projection = LabelExtractionRun(
        extraction_run_id=body.extraction_run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        label_version_id=body.label_version_id,
        prompt_version_id=body.prompt_version_id,
        model_version=body.model_version,
        schema_version=body.schema_version,
        status="queued",
        subject_scope=body.subject_scope,
        subject_refs=subject_refs,
        input_sha256=body.input_sha256,
        observation_count=0,
        trace_id=record.trace_id,
        payload={
            "execution_mode": body.execution_mode,
            "aggregation_policy_version_id": body.aggregation_policy_version_id,
            "source_bindings": source_bindings,
            "manifest": manifest,
            "manifest_sha256": canonical_sha256(manifest),
            "release_head_lock": release_head_lock,
            "root_trace_id": record.trace_id,
        },
    )
    session.add(projection)


def extraction_run_data(
    record: LabelExtractionRun, run_record: RunRecord | None = None
) -> dict[str, Any]:
    status = run_record.status if run_record is not None else record.status
    if status == "success" and record.status == "materialized":
        status = "materialized"
    raw_source_bindings = record.payload.get("source_bindings", [])
    public_source_bindings = (
        [
            {
                field: binding[field]
                for field in LABEL_EXTRACTION_PUBLIC_SOURCE_BINDING_FIELDS
                if field in binding
            }
            for binding in raw_source_bindings
            if isinstance(binding, dict) and isinstance(binding.get("source_family"), str)
        ]
        if isinstance(raw_source_bindings, list)
        else []
    )
    projection = {
        "extraction_run_id": record.extraction_run_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "label_version_id": record.label_version_id,
        "prompt_version_id": record.prompt_version_id,
        "model_version": record.model_version,
        "schema_version": record.schema_version,
        "status": status,
        "subject_scope": record.subject_scope,
        "subject_refs": record.subject_refs,
        "input_sha256": record.input_sha256,
        "observation_count": record.observation_count,
        "aggregation_policy_version_id": record.payload.get("aggregation_policy_version_id"),
        "source_bindings": public_source_bindings,
        "manifest_sha256": record.payload.get("manifest_sha256"),
        "release_head_lock": record.payload.get("release_head_lock"),
        "aggregation_run_id": record.payload.get("aggregation_run_id"),
        "aggregate_ids": record.payload.get("aggregate_ids", []),
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
        "next_actions": (run_record.payload.get("next_actions", []) if run_record else []),
    }
    return public_run_projection(
        projection,
        allowed_fields=LABEL_EXTRACTION_PUBLIC_FIELDS,
        field_name="label_extraction_run",
    )


def _revalidate_extraction_manifest(
    session: Session,
    ctx: RequestContext,
    projection: LabelExtractionRun,
) -> None:
    stored_manifest = (projection.payload or {}).get("manifest")
    stored_sha256 = (projection.payload or {}).get("manifest_sha256")
    # Rows created before the strong-manifest API existed are readable for
    # migration compatibility.  No public create path can produce one now.
    if not isinstance(stored_manifest, dict) or not isinstance(stored_sha256, str):
        return
    if canonical_sha256(stored_manifest) != stored_sha256:
        raise ApiError(
            "EXTRACTION_MANIFEST_HASH_DRIFT",
            "抽取运行保存的强 Manifest 哈希不一致",
            409,
        )
    execution_mode = str((projection.payload or {}).get("execution_mode") or "production")
    label = _scoped_label_version(session, ctx, projection.label_version_id)
    if execution_mode == "production" and (label.artifact_status or label.status) != "published":
        raise ApiError(
            "EXTRACTION_LABEL_VERSION_NOT_PUBLISHED",
            "抽取完成时 LabelVersion 已不再满足 production 锁定条件",
            409,
        )
    _, prompt = _scoped_extraction_prompt(
        session,
        ctx,
        prompt_version_id=projection.prompt_version_id,
        label_version_id=projection.label_version_id,
        model_version=projection.model_version,
        schema_version=projection.schema_version,
        execution_mode=execution_mode,
    )
    policy = _scoped_extraction_policy(
        session,
        ctx,
        policy_version_id=(projection.payload or {}).get("aggregation_policy_version_id"),
        label_version_id=projection.label_version_id,
    )
    if not isinstance(projection.subject_refs, list) or not projection.subject_refs:
        raise ApiError(
            "EXTRACTION_SUBJECT_MANIFEST_INVALID",
            "抽取运行必须锁定至少一个规范化 subject",
            409,
        )
    current_manifest = _extraction_manifest_document(
        label=label,
        prompt=prompt,
        policy=policy,
        model_version=projection.model_version,
        schema_version=projection.schema_version,
        subject_scope=projection.subject_scope,
        subject_refs=projection.subject_refs,
        source_bindings=list((projection.payload or {}).get("source_bindings") or []),
        input_sha256=projection.input_sha256,
        execution_mode=execution_mode,
        release_head_lock=(
            stored_manifest.get("release_head_lock")
            if isinstance(stored_manifest.get("release_head_lock"), dict)
            else None
        ),
    )
    current_sha256 = canonical_sha256(current_manifest)
    if current_sha256 != stored_sha256:
        raise ApiError(
            "EXTRACTION_MANIFEST_BINDING_DRIFT",
            "抽取运行锁定的 Label/Prompt/Schema/模型/策略或 subject 已漂移",
            409,
            details=[
                {
                    "stored_manifest_sha256": stored_sha256,
                    "current_manifest_sha256": current_sha256,
                }
            ],
        )


def get_label_extraction_run(
    session: Session, ctx: RequestContext, extraction_run_id: str
) -> dict[str, Any]:
    projection = session.scalar(
        select(LabelExtractionRun).where(
            LabelExtractionRun.extraction_run_id == extraction_run_id,
            LabelExtractionRun.tenant_id == ctx.tenant_id,
            LabelExtractionRun.project_id == ctx.project_id,
        )
    )
    if projection is None:
        raise ApiError("NOT_FOUND", f"label_extraction_runs 不存在：{extraction_run_id}", 404)
    run = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == extraction_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_extraction",
        )
    )
    return extraction_run_data(projection, run)


def materialize_label_extraction_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    if record.run_type != "label_extraction" or record.status != "success":
        return []
    projection = session.scalar(
        select(LabelExtractionRun)
        .where(
            LabelExtractionRun.extraction_run_id == record.run_id,
            LabelExtractionRun.tenant_id == ctx.tenant_id,
            LabelExtractionRun.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if projection is None:
        raise ApiError(
            "LABEL_EXTRACTION_PROJECTION_MISSING",
            "抽取完成回执缺少强类型运行投影",
            409,
        )
    _revalidate_extraction_manifest(session, ctx, projection)
    fact_ctx = _rooted_context(ctx, projection.trace_id)
    result_ref = completion_receipt.get("result_ref")
    raw_observations = result_ref.get("observations") if isinstance(result_ref, dict) else None
    if not isinstance(raw_observations, list) or not raw_observations:
        raise ApiError(
            "LABEL_EXTRACTION_RESULT_EMPTY",
            "成功回执必须包含至少一个可物化 Observation",
            422,
        )
    if len(raw_observations) > 5000:
        raise ApiError("LABEL_EXTRACTION_RESULT_TOO_LARGE", "单次最多物化 5000 条 Observation", 422)

    allowed_subject_keys = {
        str(item.get("subject_key") or item.get("id") or "")
        for item in projection.subject_refs
        if isinstance(item, dict)
    }
    allowed_subject_keys.discard("")
    materialized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    locked_source_bindings = list((projection.payload or {}).get("source_bindings") or [])
    default_source_binding = locked_source_bindings[0] if len(locked_source_bindings) == 1 else None
    for index, raw in enumerate(raw_observations):
        if not isinstance(raw, dict):
            raise ApiError(
                "LABEL_EXTRACTION_RESULT_INVALID",
                "Observation 结果必须为对象",
                422,
                details=[{"index": index}],
            )
        subject_key = str(raw.get("subject_key") or "")
        if allowed_subject_keys and subject_key not in allowed_subject_keys:
            raise ApiError(
                "LABEL_EXTRACTION_SUBJECT_OUT_OF_SCOPE",
                "模型结果引用了运行锁定范围之外的 subject",
                409,
                details=[{"index": index, "subject_key": subject_key}],
            )
        merged = {
            **raw,
            "extraction_run_id": projection.extraction_run_id,
            "subject_scope": projection.subject_scope,
            "label_version_id": projection.label_version_id,
            "model_version": projection.model_version,
            "prompt_version_id": projection.prompt_version_id,
            "schema_version": projection.schema_version,
        }
        if isinstance(default_source_binding, dict):
            merged.setdefault("source_family", default_source_binding.get("source_family"))
            merged.setdefault("source_type", default_source_binding.get("source_type"))
        try:
            body = LabelObservationCreateRequest.model_validate(merged)
        except ValidationError as exc:
            raise ApiError(
                "LABEL_EXTRACTION_RESULT_INVALID",
                "模型 Observation 结果未通过强 Schema 校验",
                422,
                details=[
                    {
                        "index": index,
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in exc.errors()
                ],
            ) from exc
        if body.observation_id in seen_ids:
            raise ApiError(
                "LABEL_EXTRACTION_DUPLICATE_OBSERVATION",
                "同一回执不能包含重复 observation_id",
                422,
            )
        seen_ids.add(body.observation_id)
        materialized.append(create_label_observation(session, fact_ctx, body))

    projection.status = "materialized"
    projection.observation_count = len(materialized)
    projection.trace_id = record.trace_id
    policy_version_id = str((projection.payload or {}).get("aggregation_policy_version_id") or "")
    if not policy_version_id:
        raise ApiError(
            "EXTRACTION_AGGREGATION_POLICY_REQUIRED",
            "抽取完成后必须有锁定聚合策略才能形成候选闭环",
            409,
        )
    policy = get_aggregation_policy(session, fact_ctx, policy_version_id)
    if policy.mode not in {"l1", "l2"}:
        raise ApiError(
            "AGGREGATION_POLICY_MODE_INVALID",
            "锁定聚合策略包含不受支持的运行模式",
            409,
            details=[{"policy_version_id": policy_version_id, "mode": policy.mode}],
        )
    policy_mode = cast(Literal["l1", "l2"], policy.mode)
    aggregation_run_id = (
        "lagr_"
        + canonical_sha256(
            {
                "extraction_run_id": projection.extraction_run_id,
                "policy_version_id": policy_version_id,
                "observation_ids": sorted(item["observation_id"] for item in materialized),
            }
        )[:24]
    )
    existing_aggregation = session.get(LabelAggregationRun, aggregation_run_id)
    if existing_aggregation is None:
        aggregation_data = create_aggregation_run(
            session,
            fact_ctx,
            LabelAggregationRunCreateRequest(
                aggregation_run_id=aggregation_run_id,
                label_version_id=projection.label_version_id,
                policy_version_id=policy_version_id,
                observation_ids=[item["observation_id"] for item in materialized],
                mode=policy_mode,
            ),
        )
    else:
        if (
            existing_aggregation.tenant_id != fact_ctx.tenant_id
            or existing_aggregation.project_id != fact_ctx.project_id
        ):
            raise ApiError(
                "AGGREGATION_RUN_SCOPE_CONFLICT",
                "确定性聚合运行 ID 已属于其他租户项目",
                409,
            )
        aggregation_data = aggregation_run_data(existing_aggregation)
    projection.payload = {
        **projection.payload,
        "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
        "observation_ids": [item["observation_id"] for item in materialized],
        "aggregation_run_id": aggregation_run_id,
        "aggregate_ids": aggregation_data.get("aggregate_ids", []),
        "review_task_ids": aggregation_data.get("review_task_ids", []),
        "materialized_at": datetime.now(UTC).isoformat(),
    }
    enqueue_event(
        session,
        fact_ctx,
        event_type="label_extraction_run.materialized",
        aggregate_type="label_extraction_run",
        aggregate_id=projection.extraction_run_id,
        payload={
            "extraction_run_id": projection.extraction_run_id,
            "observation_ids": projection.payload["observation_ids"],
            "observation_count": projection.observation_count,
            "aggregation_run_id": aggregation_run_id,
            "aggregate_ids": projection.payload["aggregate_ids"],
            "review_task_ids": projection.payload["review_task_ids"],
            "status": projection.status,
        },
    )
    return materialized


def get_label_observation(
    session: Session, ctx: RequestContext, observation_id: str
) -> dict[str, Any]:
    record = session.scalar(
        select(LabelObservation).where(
            LabelObservation.observation_id == observation_id,
            LabelObservation.tenant_id == ctx.tenant_id,
            LabelObservation.project_id == ctx.project_id,
        )
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"label_observations 不存在：{observation_id}", 404)
    return observation_data(record)


def list_label_observations(
    session: Session,
    ctx: RequestContext,
    *,
    subject_scope: str | None = None,
    subject_key: str | None = None,
    label_version_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(LabelObservation).where(
        LabelObservation.tenant_id == ctx.tenant_id,
        LabelObservation.project_id == ctx.project_id,
    )
    if subject_scope:
        statement = statement.where(LabelObservation.subject_scope == subject_scope)
    if subject_key:
        statement = statement.where(LabelObservation.subject_key == subject_key)
    if label_version_id:
        statement = statement.where(LabelObservation.label_version_id == label_version_id)
    records = session.scalars(statement.order_by(LabelObservation.created_at.desc()).limit(limit))
    return [observation_data(record) for record in records]


def policy_data(record: LabelAggregationPolicyVersion) -> dict[str, Any]:
    return {
        "policy_version_id": record.policy_version_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "label_version_id": record.label_version_id,
        "policy_version": record.policy_version,
        "mode": record.mode,
        "status": record.status,
        "source_weights": record.source_weights,
        "calibration_versions": record.calibration_versions,
        "thresholds": record.thresholds,
        "label_definitions": record.label_definitions,
        "canonical_sha256": record.canonical_sha256,
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
    }


def create_aggregation_policy(
    session: Session,
    ctx: RequestContext,
    body: LabelAggregationPolicyCreateRequest,
) -> dict[str, Any]:
    _scoped_label_version(session, ctx, body.label_version_id)
    if session.get(LabelAggregationPolicyVersion, body.policy_version_id) is not None:
        raise ApiError("AGGREGATION_POLICY_ALREADY_EXISTS", "聚合策略版本 ID 已存在", 409)
    calibration_lock: dict[str, dict[str, Any]] = {}
    if body.mode == "l2" and body.status == "active":
        calibration_lock = _server_locked_l2_calibrations(
            session,
            ctx,
            label_version_id=body.label_version_id,
            calibration_versions=body.calibration_versions,
        )
    canonical = body.model_dump(mode="json", exclude={"policy_version_id", "status"})
    canonical_hash = canonical_sha256(canonical)
    record = LabelAggregationPolicyVersion(
        policy_version_id=body.policy_version_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        label_version_id=body.label_version_id,
        policy_version=body.policy_version,
        mode=body.mode,
        status=body.status,
        source_weights=body.source_weights,
        calibration_versions=body.calibration_versions,
        thresholds=body.thresholds.model_dump(mode="json"),
        label_definitions=[item.model_dump(mode="json") for item in body.label_definitions],
        canonical_sha256=canonical_hash,
        trace_id=ctx.trace_id,
        payload={
            "root_trace_id": ctx.trace_id,
            "server_calibration_lock": calibration_lock,
            "server_calibration_lock_sha256": (
                canonical_sha256(calibration_lock) if calibration_lock else None
            ),
        },
    )
    session.add(record)
    _materialize_policy_taxonomy(session, ctx, body)
    session.flush()
    data = policy_data(record)
    record_audit(
        session,
        ctx,
        action="label_aggregation_policy.created",
        object_type="label_aggregation_policy",
        object_id=record.policy_version_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_aggregation_policy.created",
        aggregate_type="label_aggregation_policy",
        aggregate_id=record.policy_version_id,
        payload=data,
    )
    return data


def _materialize_policy_taxonomy(
    session: Session,
    ctx: RequestContext,
    body: LabelAggregationPolicyCreateRequest,
) -> None:
    for definition in body.label_definitions:
        node = session.scalar(
            select(LabelNode).where(
                LabelNode.tenant_id == ctx.tenant_id,
                LabelNode.project_id == ctx.project_id,
                LabelNode.label_id == definition.label_id,
            )
        )
        if node is None:
            session.add(
                LabelNode(
                    node_id=(
                        "ln_"
                        + canonical_sha256([ctx.tenant_id, ctx.project_id, definition.label_id])[
                            :24
                        ]
                    ),
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    label_id=definition.label_id,
                    canonical_name=definition.canonical_name,
                    status="active",
                    trace_id=ctx.trace_id,
                    payload={"root_trace_id": ctx.trace_id},
                )
            )
        version_item = session.scalar(
            select(LabelVersionItem).where(
                LabelVersionItem.tenant_id == ctx.tenant_id,
                LabelVersionItem.project_id == ctx.project_id,
                LabelVersionItem.label_version_id == body.label_version_id,
                LabelVersionItem.label_id == definition.label_id,
            )
        )
        expected_item = LabelVersionItem(
            label_version_item_id=(
                "lvi_"
                + canonical_sha256(
                    [
                        ctx.tenant_id,
                        ctx.project_id,
                        body.label_version_id,
                        definition.label_id,
                    ]
                )[:24]
            ),
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            label_version_id=body.label_version_id,
            label_id=definition.label_id,
            canonical_name=definition.canonical_name,
            aliases=definition.aliases,
            value_type=definition.kind,
            risk_level=definition.risk_level,
            mutual_exclusion_group=definition.mutual_exclusion_group,
            parent_ids=definition.parent_ids,
            aggregation_rule={"numeric_tolerance": definition.numeric_tolerance},
            status="active",
            trace_id=ctx.trace_id,
        )
        expected_sha256 = label_version_item_definition_sha256(expected_item)
        if version_item is None:
            expected_item.definition_sha256 = expected_sha256
            session.add(expected_item)
        elif version_item.definition_sha256 is None:
            version_item.definition_sha256 = expected_sha256
        elif version_item.definition_sha256 != expected_sha256:
            raise ApiError(
                "LABEL_VERSION_ITEM_DEFINITION_DRIFT",
                f"标签定义与已物化版本不一致：{definition.label_id}",
                409,
            )


def get_aggregation_policy(
    session: Session, ctx: RequestContext, policy_version_id: str
) -> LabelAggregationPolicyVersion:
    record = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"label_aggregation_policies 不存在：{policy_version_id}", 404)
    return record


def _domain_policy(record: LabelAggregationPolicyVersion, mode: str) -> AggregationPolicy:
    thresholds = record.thresholds or {}
    return AggregationPolicy(
        mode=AggregationMode(mode),
        source_weights=tuple(
            SourceWeight(source_family=family, weight=float(weight))
            for family, weight in sorted((record.source_weights or {}).items())
        ),
        l2_accept_threshold=float(thresholds.get("l2_accept_score", 0.95)),
        categorical_margin=float(thresholds.get("categorical_margin", 0.15)),
        temporal_iou_threshold=float(thresholds.get("temporal_iou", 0.6)),
        min_independent_sources=int(thresholds.get("min_independent_sources", 2)),
        random_audit_rate=float(thresholds.get("random_audit_rate", 0.05)),
    )


def _domain_definitions(record: LabelAggregationPolicyVersion) -> tuple[LabelDefinition, ...]:
    definitions: list[LabelDefinition] = []
    for item in record.label_definitions:
        kind = "hierarchy" if item.get("kind") == "hierarchical" else item.get("kind")
        definitions.append(
            LabelDefinition(
                label_id=str(item["label_id"]),
                canonical_name=str(item["canonical_name"]),
                aliases=tuple(item.get("aliases") or ()),
                kind=LabelKind(str(kind)),
                risk_level=RiskLevel(str(item.get("risk_level") or "low")),
                parent_ids=tuple(item.get("parent_ids") or ()),
                mutex_group=item.get("mutual_exclusion_group"),
                numeric_tolerance=float(item.get("numeric_tolerance") or 0),
            )
        )
    return tuple(definitions)


def _domain_value(record: LabelObservation) -> object:
    if record.value_type == "temporal" and isinstance(record.value_json, dict):
        start = record.value_json.get("start", record.value_json.get("start_ms"))
        end = record.value_json.get("end", record.value_json.get("end_ms"))
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
        ):
            raise ApiError(
                "LABEL_TEMPORAL_VALUE_INVALID",
                "时间区间标签必须包含数值 start/end 或 start_ms/end_ms",
                422,
                details=[{"observation_id": record.observation_id}],
            )
        return TimeSpan(float(start), float(end))
    if record.value_type == "multi" and isinstance(record.value_json, list):
        return tuple(record.value_json)
    return record.value_json


def _domain_observation(record: LabelObservation) -> DomainObservation:
    evidence_verification = record.payload.get("evidence_verification") or {}
    source_lineage = record.payload.get("source_lineage") or {}
    evidence_start = record.evidence_ref.get("start_ms")
    evidence_end = record.evidence_ref.get("end_ms")
    return DomainObservation(
        observation_id=record.observation_id,
        subject_scope=record.subject_scope,
        subject_key=record.subject_key,
        raw_label=record.raw_label,
        value=_domain_value(record),
        source_family=record.source_family,
        source_type=SourceType(record.source_type.replace("-", "_")),
        raw_confidence=record.raw_confidence,
        calibrated_confidence=record.calibrated_confidence,
        evidence_hash=record.evidence_sha256,
        trace_id=record.trace_id,
        evidence_valid=(
            bool(evidence_verification.get("verified"))
            if evidence_verification
            else bool(record.evidence_ref)
        ),
        novel=bool(record.payload.get("novel", False)),
        correlation_group_id=(
            str(source_lineage.get("correlation_group_id"))
            if source_lineage.get("correlation_group_id")
            else None
        ),
        extraction_run_id=record.extraction_run_id,
        model_version=record.model_version,
        prompt_version_id=record.prompt_version_id,
        evidence_ref_id=str(record.evidence_ref.get("id") or "") or None,
        evidence_start=float(evidence_start) if evidence_start is not None else None,
        evidence_end=float(evidence_end) if evidence_end is not None else None,
    )


def _risk_by_label(record: LabelAggregationPolicyVersion) -> dict[str, str]:
    return {
        str(item["label_id"]): str(item.get("risk_level") or "low")
        for item in record.label_definitions
    }


def _aggregate_data(
    record: LabelAggregate, members: list[LabelAggregateMember] | None = None
) -> dict[str, Any]:
    return {
        "aggregate_id": record.aggregate_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "aggregation_run_id": record.aggregation_run_id,
        "label_version_id": record.label_version_id,
        "policy_version_id": record.policy_version_id,
        "calibration_version_ids": record.calibration_version_ids,
        "subject_scope": record.subject_scope,
        "subject_key": record.subject_key,
        "label_id": record.label_id,
        "value_type": record.value_type,
        "value": record.value_json,
        "score": record.score,
        "margin": record.margin,
        "risk_level": record.risk_level,
        "decision": record.decision.replace("_", "-"),
        "status": record.status,
        "reason_codes": record.reason_codes,
        "explanation": record.explanation,
        "deterministic_hash": record.deterministic_hash,
        "review_task_id": record.review_task_id,
        "trace_id": record.trace_id,
        "members": [
            {
                "aggregate_member_id": member.aggregate_member_id,
                "observation_id": member.observation_id,
                "included": member.included,
                "source_family": member.source_family,
                "evidence_sha256": member.evidence_sha256,
                "calibrated_confidence": member.calibrated_confidence,
                "contribution_score": member.contribution_score,
                "exclusion_reason": member.exclusion_reason,
                "explanation": member.explanation,
            }
            for member in (members or [])
        ],
    }


def create_aggregation_run(
    session: Session,
    ctx: RequestContext,
    body: LabelAggregationRunCreateRequest,
) -> dict[str, Any]:
    _scoped_label_version(session, ctx, body.label_version_id)
    if session.get(LabelAggregationRun, body.aggregation_run_id) is not None:
        raise ApiError("AGGREGATION_RUN_ALREADY_EXISTS", "聚合运行 ID 已存在", 409)
    policy_record = get_aggregation_policy(session, ctx, body.policy_version_id)
    if policy_record.label_version_id != body.label_version_id:
        raise ApiError("AGGREGATION_VERSION_MISMATCH", "聚合策略与标签版本不匹配", 409)
    if policy_record.status != "active":
        raise ApiError("AGGREGATION_POLICY_NOT_ACTIVE", "仅 active 聚合策略可运行", 409)
    if policy_record.mode != body.mode:
        raise ApiError("AGGREGATION_MODE_MISMATCH", "运行模式必须与锁定策略一致", 409)

    server_calibration_lock: dict[str, dict[str, Any]] = {}
    if body.mode == "l2":
        thresholds = policy_record.thresholds or {}
        if int(thresholds.get("min_independent_sources", 0)) < 2:
            raise ApiError(
                "L2_INDEPENDENT_SOURCE_POLICY_INVALID",
                "L2 聚合策略必须锁定至少两个独立来源",
                409,
            )
        server_calibration_lock = _server_locked_l2_calibrations(
            session,
            ctx,
            label_version_id=body.label_version_id,
            calibration_versions=policy_record.calibration_versions or {},
        )
        if canonical_sha256(server_calibration_lock) != policy_record.payload.get(
            "server_calibration_lock_sha256"
        ):
            raise ApiError(
                "L2_CALIBRATION_LOCK_DRIFT",
                "聚合策略中的服务端校准锁与当前权威版本不一致",
                409,
            )

    observations = list(
        session.scalars(
            select(LabelObservation).where(
                LabelObservation.tenant_id == ctx.tenant_id,
                LabelObservation.project_id == ctx.project_id,
                LabelObservation.observation_id.in_(body.observation_ids),
            )
        )
    )
    by_id = {record.observation_id: record for record in observations}
    missing = sorted(set(body.observation_ids) - set(by_id))
    if missing:
        raise ApiError(
            "LABEL_OBSERVATIONS_NOT_FOUND",
            "部分 Observation 在当前租户项目中不存在",
            404,
            details=[{"observation_ids": missing}],
        )
    mismatched = sorted(
        record.observation_id
        for record in observations
        if record.label_version_id != body.label_version_id
    )
    if mismatched:
        raise ApiError(
            "OBSERVATION_LABEL_VERSION_MISMATCH",
            "Observation 必须全部绑定运行锁定的标签版本",
            409,
            details=[{"observation_ids": mismatched}],
        )

    if body.mode == "l2":
        calibration_mismatches = [
            {
                "observation_id": record.observation_id,
                "source_family": record.source_family,
                "expected_calibration_version_id": _policy_calibration_version_id(
                    policy_record,
                    label_id=record.label_id,
                    source_family=record.source_family,
                ),
                "actual_calibration_version_id": record.calibration_version_id,
            }
            for record in observations
            if record.calibrated_confidence is None
            or record.calibration_version_id
            != _policy_calibration_version_id(
                policy_record,
                label_id=record.label_id,
                source_family=record.source_family,
            )
            or not bool(
                (record.payload.get("calibration_provenance") or {}).get("authority")
                == "server-locked"
            )
            or not bool((record.payload.get("source_lineage") or {}).get("server_locked"))
            or not bool((record.payload.get("evidence_verification") or {}).get("verified"))
        ]
        if calibration_mismatches:
            raise ApiError(
                "L2_OBSERVATION_CALIBRATION_MISMATCH",
                "L2 Observation 必须使用策略服务端锁定的稳定校准版本",
                409,
                details=calibration_mismatches,
            )

    ordered = [by_id[observation_id] for observation_id in body.observation_ids]
    root_trace_ids = {record.trace_id for record in ordered}
    if len(root_trace_ids) != 1:
        raise ApiError(
            "OBSERVATION_ROOT_TRACE_MISMATCH",
            "一次聚合只能消费同一根 Trace 下的 Observation",
            409,
            details=[{"trace_ids": sorted(root_trace_ids)}],
        )
    ctx = _rooted_context(ctx, next(iter(root_trace_ids)))
    input_hash = canonical_sha256(
        {
            "policy_version_id": body.policy_version_id,
            "policy_sha256": policy_record.canonical_sha256,
            "observation_ids": sorted(body.observation_ids),
            "observation_output_sha256": sorted(item.output_sha256 for item in ordered),
        }
    )
    run = LabelAggregationRun(
        aggregation_run_id=body.aggregation_run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        label_version_id=body.label_version_id,
        policy_version_id=body.policy_version_id,
        mode=body.mode,
        status="materializing",
        observation_count=len(ordered),
        aggregate_count=0,
        input_sha256=input_hash,
        result_sha256=None,
        trace_id=ctx.trace_id,
        payload={"root_trace_id": ctx.trace_id},
    )
    session.add(run)
    session.flush()

    engine = LabelAggregationEngine(
        _domain_definitions(policy_record),
        _domain_policy(policy_record, body.mode),
    )
    batch = engine.aggregate(_domain_observation(record) for record in ordered)
    aggregate_ids: list[str] = []
    suggestion_ids: list[str] = []
    review_task_ids: list[str] = []
    risk_map = _risk_by_label(policy_record)
    definition_kind = {
        str(item["label_id"]): str(item.get("kind") or "boolean")
        for item in policy_record.label_definitions
    }

    for item in batch.aggregates:
        item_data = item.to_dict()
        aggregate_id = f"{item.aggregate_id}_{canonical_sha256(body.aggregation_run_id)[:8]}"
        bucket_hash = canonical_sha256(
            [item.subject_scope, item.subject_key, item.label_id, item_data["value"]]
        )
        decision = item.decision.value
        status = (
            "awaiting-review"
            if decision == "require_review"
            else ("accepted" if decision == "auto_accept" else "abstained")
        )
        reason_codes = list(item.reason_codes)
        batch_eligible = (
            risk_map.get(item.label_id, "high") == "low"
            and decision == "require_review"
            and set(reason_codes) == {"L1_HUMAN_REVIEW_REQUIRED"}
        )
        explanation = {
            **item.explanation.to_dict(),
            "batch_review": {
                "eligible": batch_eligible,
                "reason_codes": ([] if batch_eligible else reason_codes or ["NOT_REVIEW_PENDING"]),
            },
        }
        aggregate = LabelAggregate(
            aggregate_id=aggregate_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            aggregation_run_id=run.aggregation_run_id,
            label_version_id=run.label_version_id,
            policy_version_id=run.policy_version_id,
            calibration_version_ids=sorted(
                {item.calibration_version_id for item in ordered if item.calibration_version_id}
            ),
            subject_scope=item.subject_scope,
            subject_key=item.subject_key,
            label_id=item.label_id,
            value_type=definition_kind.get(item.label_id, "boolean"),
            value_json=item_data["value"],
            score=item.score,
            margin=item.margin,
            risk_level=risk_map.get(item.label_id, "high"),
            decision=decision,
            status=status,
            reason_codes=reason_codes,
            explanation=explanation,
            bucket_sha256=bucket_hash,
            deterministic_hash=item.canonical_hash,
            review_task_id=None,
            trace_id=ctx.trace_id,
        )
        session.add(aggregate)
        session.flush()
        contributions = {entry.observation_id: entry for entry in item.explanation.contributions}
        member_records: list[LabelAggregateMember] = []
        for observation_id, contribution in sorted(contributions.items()):
            source = by_id[observation_id]
            member = LabelAggregateMember(
                aggregate_member_id=(
                    "lam_" + canonical_sha256([aggregate_id, observation_id])[:24]
                ),
                aggregate_id=aggregate_id,
                observation_id=observation_id,
                included=contribution.included,
                source_family=contribution.source_family,
                evidence_sha256=source.evidence_sha256,
                calibrated_confidence=source.calibrated_confidence,
                contribution_score=contribution.weighted_log_odds,
                exclusion_reason=contribution.exclusion_reason,
                explanation=contribution.to_dict(),
                trace_id=ctx.trace_id,
            )
            session.add(member)
            member_records.append(member)

        if decision == "require_review":
            review_task_id = _create_aggregate_review_task(session, ctx, aggregate)
            aggregate.review_task_id = review_task_id
            review_task_ids.append(review_task_id)
        elif decision == "auto_accept":
            active_fact = _create_label_fact(
                session,
                ctx,
                aggregate,
                authority="l2-auto-accepted",
                review_decision_id=None,
            )
            if active_fact.aggregate_id != aggregate.aggregate_id:
                same_value = canonical_sha256(active_fact.value_json) == canonical_sha256(
                    aggregate.value_json
                )
                aggregate.reason_codes = [
                    *aggregate.reason_codes,
                    (
                        "higher-authority-fact-already-active"
                        if same_value
                        else "higher-authority-fact-conflict"
                    ),
                ]
                aggregate.explanation = {
                    **aggregate.explanation,
                    "fact_resolution": {
                        "retained_fact_id": active_fact.fact_id,
                        "retained_authority": active_fact.authority,
                        "same_value": same_value,
                    },
                }
                if not same_value:
                    aggregate.decision = "abstain"
                    aggregate.status = "abstained"
        aggregate_payload = _aggregate_data(aggregate, member_records)
        upsert_resource(
            session,
            ctx,
            "label_aggregates",
            aggregate_id,
            aggregate_payload,
            status=aggregate.status,
            trace_id=ctx.trace_id,
            audit_action="label_aggregate.projection_created",
        )
        record_audit(
            session,
            ctx,
            action="label_aggregate.created",
            object_type="label_aggregate",
            object_id=aggregate_id,
            after=aggregate_payload,
        )
        enqueue_event(
            session,
            ctx,
            event_type="label_aggregate.created",
            aggregate_type="label_aggregate",
            aggregate_id=aggregate_id,
            payload=aggregate_payload,
        )
        aggregate_ids.append(aggregate_id)

    for suggestion in batch.unknown_suggestions:
        suggestion_id, taxonomy_review_task_id = _create_taxonomy_suggestion(
            session,
            ctx,
            label_version_id=body.label_version_id,
            suggestion=suggestion.to_dict(),
        )
        suggestion_ids.append(suggestion_id)
        if taxonomy_review_task_id is not None:
            review_task_ids.append(taxonomy_review_task_id)

    awaiting_review = bool(review_task_ids)
    run.status = "awaiting-review" if awaiting_review else "completed"
    run.aggregate_count = len(aggregate_ids)
    run.result_sha256 = batch.canonical_hash
    run.payload = {
        **run.payload,
        "status": run.status,
        "aggregate_ids": aggregate_ids,
        "taxonomy_suggestion_ids": suggestion_ids,
        "review_task_ids": review_task_ids,
        "engine_version": batch.engine_version,
        "policy_hash": batch.policy_hash,
        "label_set_hash": batch.label_set_hash,
        "result_sha256": batch.canonical_hash,
    }
    data = aggregation_run_data(run)
    record_audit(
        session,
        ctx,
        action="label_aggregation_run.materialized",
        object_type="label_aggregation_run",
        object_id=run.aggregation_run_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_aggregation_run.materialized",
        aggregate_type="label_aggregation_run",
        aggregate_id=run.aggregation_run_id,
        payload=data,
    )
    return data


def _create_aggregate_review_task(
    session: Session, ctx: RequestContext, aggregate: LabelAggregate
) -> str:
    review_task_id = f"hrt_{canonical_sha256(['label-aggregate', aggregate.aggregate_id])[:24]}"
    payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "status": "pending",
        "review_status": "pending",
        "queue": _review_queue(aggregate),
        "risk_level": aggregate.risk_level,
        "review_mode": "double-blind" if aggregate.risk_level == "high" else "single",
        "required_reviews": 2 if aggregate.risk_level == "high" else 1,
        "reason_codes": aggregate.reason_codes,
        "target_refs": [{"type": "label_aggregate", "id": aggregate.aggregate_id}],
        "source_trace_id": aggregate.trace_id,
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
        payload,
        status="pending",
        trace_id=ctx.trace_id,
        audit_action="human_review_task.aggregate_created",
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review_task.created",
        aggregate_type="human_review_task",
        aggregate_id=review_task_id,
        payload=payload,
    )
    return review_task_id


def _review_queue(aggregate: LabelAggregate) -> str:
    if aggregate.risk_level == "high":
        return "high_risk"
    if any("CONFLICT" in reason for reason in aggregate.reason_codes):
        return "conflict"
    if any("NOVEL" in reason for reason in aggregate.reason_codes):
        return "novelty"
    if any("RANDOM_AUDIT" in reason for reason in aggregate.reason_codes):
        return "random_audit"
    return "uncertain"


def _create_taxonomy_suggestion(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    suggestion: dict[str, Any],
) -> tuple[str, str | None]:
    suggestion_id = (
        "lts_"
        + canonical_sha256(
            [ctx.tenant_id, ctx.project_id, label_version_id, suggestion["normalized_label"]]
        )[:24]
    )
    existing = session.get(LabelTaxonomySuggestion, suggestion_id)
    if existing is not None:
        if existing.status in {"accepted", "rejected"}:
            return suggestion_id, None
        if not existing.review_task_id:
            raise ApiError(
                "TAXONOMY_SUGGESTION_REVIEW_BINDING_MISSING",
                "待处理 Taxonomy suggestion 缺少审核任务绑定",
                409,
            )
        return suggestion_id, existing.review_task_id
    review_task_id = f"hrt_{canonical_sha256(['taxonomy', suggestion_id])[:24]}"
    record = LabelTaxonomySuggestion(
        suggestion_id=suggestion_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        label_version_id=label_version_id,
        normalized_label=suggestion["normalized_label"],
        raw_labels=suggestion["raw_labels"],
        observation_ids=suggestion["observation_ids"],
        proposed_action="review",
        canonical_target_label_id=None,
        status="pending",
        review_task_id=review_task_id,
        trace_id=ctx.trace_id,
        payload={"reason_code": suggestion["reason_code"]},
    )
    session.add(record)
    payload = taxonomy_suggestion_data(record)
    upsert_resource(
        session,
        ctx,
        "label_taxonomy_suggestions",
        suggestion_id,
        payload,
        status="pending",
        trace_id=ctx.trace_id,
        audit_action="label_taxonomy_suggestion.created",
    )
    task_payload = {
        "id": review_task_id,
        "review_task_id": review_task_id,
        "status": "pending",
        "review_status": "pending",
        "queue": "taxonomy",
        "risk_level": "high",
        "review_mode": "double-blind",
        "required_reviews": 2,
        "target_refs": [{"type": "taxonomy_suggestion", "id": suggestion_id}],
        "trace_id": ctx.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "human_review_tasks",
        review_task_id,
        task_payload,
        status="pending",
        trace_id=ctx.trace_id,
        audit_action="human_review_task.taxonomy_created",
    )
    enqueue_event(
        session,
        ctx,
        event_type="human_review_task.created",
        aggregate_type="human_review_task",
        aggregate_id=review_task_id,
        payload=task_payload,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_taxonomy_suggestion.created",
        aggregate_type="label_taxonomy_suggestion",
        aggregate_id=suggestion_id,
        payload=payload,
    )
    return suggestion_id, review_task_id


def aggregation_run_data(record: LabelAggregationRun) -> dict[str, Any]:
    return {
        "aggregation_run_id": record.aggregation_run_id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "label_version_id": record.label_version_id,
        "policy_version_id": record.policy_version_id,
        "mode": record.mode,
        "status": record.status,
        "observation_count": record.observation_count,
        "aggregate_count": record.aggregate_count,
        "input_sha256": record.input_sha256,
        "result_sha256": record.result_sha256,
        "aggregate_ids": record.payload.get("aggregate_ids", []),
        "taxonomy_suggestion_ids": record.payload.get("taxonomy_suggestion_ids", []),
        "review_task_ids": record.payload.get("review_task_ids", []),
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
    }


def get_aggregation_run(
    session: Session, ctx: RequestContext, aggregation_run_id: str
) -> dict[str, Any]:
    record = session.scalar(
        select(LabelAggregationRun).where(
            LabelAggregationRun.aggregation_run_id == aggregation_run_id,
            LabelAggregationRun.tenant_id == ctx.tenant_id,
            LabelAggregationRun.project_id == ctx.project_id,
        )
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"label_aggregation_runs 不存在：{aggregation_run_id}", 404)
    return aggregation_run_data(record)


def get_label_aggregate(session: Session, ctx: RequestContext, aggregate_id: str) -> dict[str, Any]:
    record = session.scalar(
        select(LabelAggregate).where(
            LabelAggregate.aggregate_id == aggregate_id,
            LabelAggregate.tenant_id == ctx.tenant_id,
            LabelAggregate.project_id == ctx.project_id,
        )
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"label_aggregates 不存在：{aggregate_id}", 404)
    members = list(
        session.scalars(
            select(LabelAggregateMember)
            .where(LabelAggregateMember.aggregate_id == aggregate_id)
            .order_by(LabelAggregateMember.observation_id)
        )
    )
    return _aggregate_data(record, members)


def list_label_aggregates(
    session: Session,
    ctx: RequestContext,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(LabelAggregate).where(
        LabelAggregate.tenant_id == ctx.tenant_id,
        LabelAggregate.project_id == ctx.project_id,
    )
    if status:
        statement = statement.where(LabelAggregate.status == status)
    records = list(
        session.scalars(statement.order_by(LabelAggregate.created_at.desc()).limit(limit))
    )
    return [_aggregate_data(record) for record in records]


def taxonomy_suggestion_data(record: LabelTaxonomySuggestion) -> dict[str, Any]:
    data = {
        "suggestion_id": record.suggestion_id,
        "label_version_id": record.label_version_id,
        "normalized_label": record.normalized_label,
        "raw_labels": record.raw_labels,
        "observation_ids": record.observation_ids,
        "proposed_action": record.proposed_action,
        "canonical_target_label_id": record.canonical_target_label_id,
        "status": record.status,
        "review_task_id": record.review_task_id,
        "trace_id": record.trace_id,
    }
    for key in (
        "candidate_label_version_id",
        "candidate_manifest_sha256",
        "created_label_id",
    ):
        if record.payload.get(key):
            data[key] = record.payload[key]
    return data


def _create_label_fact(
    session: Session,
    ctx: RequestContext,
    aggregate: LabelAggregate,
    *,
    authority: str,
    review_decision_id: str | None,
    value: Any | None = None,
) -> LabelFact:
    authority_rank = {
        "l2-auto-accepted": 10,
        "human-confirmed": 100,
    }
    if authority not in authority_rank:
        raise ApiError(
            "LABEL_FACT_AUTHORITY_INVALID",
            "LabelFact authority 不在服务端允许集合中",
            422,
        )
    legacy_previous = session.scalar(
        select(LabelFact)
        .where(
            LabelFact.tenant_id == ctx.tenant_id,
            LabelFact.project_id == ctx.project_id,
            LabelFact.subject_scope == aggregate.subject_scope,
            LabelFact.subject_key == aggregate.subject_key,
            LabelFact.label_id == aggregate.label_id,
            LabelFact.status == "active",
            LabelFact.source_kind.is_(None),
        )
        .with_for_update()
    )
    if legacy_previous is not None:
        raise ApiError(
            "LABEL_FACT_BACKFILL_REQUIRED",
            "现有 LabelFact 尚未完成双时态回填，拒绝覆盖以避免事实链断裂",
            409,
            details=[{"fact_id": legacy_previous.fact_id}],
        )

    occurred_at_origin = "legacy-recorded-fallback"
    occurred_at = aggregate.created_at or datetime.now(UTC)
    source_occurred_at = aggregate.explanation.get("occurred_at")
    if isinstance(source_occurred_at, datetime):
        if source_occurred_at.tzinfo is not None and source_occurred_at.utcoffset() is not None:
            occurred_at = source_occurred_at
            occurred_at_origin = "source"
    elif isinstance(source_occurred_at, str):
        try:
            parsed_occurred_at = datetime.fromisoformat(source_occurred_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_occurred_at = None
        if (
            parsed_occurred_at is not None
            and parsed_occurred_at.tzinfo is not None
            and parsed_occurred_at.utcoffset() is not None
        ):
            occurred_at = parsed_occurred_at
            occurred_at_origin = "source"
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    else:
        occurred_at = occurred_at.astimezone(UTC)
    fact_value = aggregate.value_json if value is None else value
    request = LabelFactRevisionCreate(
        aggregate_id=aggregate.aggregate_id,
        source_kind=("human-decision" if review_decision_id else "aggregate"),
        human_review_decision_id=review_decision_id,
        fact_set_id=None,
        fact_namespace="production",
        subject_scope=aggregate.subject_scope,
        subject_key=aggregate.subject_key,
        event_or_segment_id=str(
            aggregate.explanation.get("event_or_segment_id") or aggregate.subject_key
        ),
        assertion_slot=str(aggregate.explanation.get("assertion_slot") or "canonical"),
        occurred_at=occurred_at,
        occurred_at_origin=cast(Any, occurred_at_origin),
        label_version_id=aggregate.label_version_id,
        label_id=aggregate.label_id,
        value_type=cast(Any, aggregate.value_type),
        value=fact_value,
        authority=cast(Any, authority),
        expected_head_generation=0,
    )
    logical_key_sha256 = label_fact_logical_key_sha256(ctx, request)
    head = session.scalar(
        select(LabelFactHead)
        .where(
            LabelFactHead.tenant_id == ctx.tenant_id,
            LabelFactHead.project_id == ctx.project_id,
            LabelFactHead.fact_namespace == request.fact_namespace,
            LabelFactHead.logical_key_sha == logical_key_sha256,
        )
        .with_for_update()
    )
    if head is not None:
        current = session.scalar(
            select(LabelFact).where(
                LabelFact.tenant_id == ctx.tenant_id,
                LabelFact.project_id == ctx.project_id,
                LabelFact.fact_id == head.current_fact_id,
            )
        )
        if (
            current is None
            or current.fact_namespace != request.fact_namespace
            or current.logical_key_sha != logical_key_sha256
            or current.revision != head.current_revision
            or current.content_sha256 != head.payload.get("current_content_sha256")
        ):
            raise ApiError(
                "LABEL_FACT_HEAD_DRIFT",
                "LabelFact Head 与当前 revision 不一致",
                409,
                details=[{"fact_head_id": head.fact_head_id}],
            )
        current_rank = authority_rank.get(current.authority)
        if current_rank is None:
            raise ApiError(
                "LABEL_FACT_AUTHORITY_UNKNOWN",
                "现有 LabelFact authority 无法安全比较，拒绝覆盖",
                409,
            )
        if current_rank > authority_rank[authority]:
            return current
    request = request.model_copy(
        update={"expected_head_generation": head.generation if head is not None else 0}
    )
    response = append_label_fact_revision(session, ctx, request)
    fact = session.get(LabelFact, response["fact_id"])
    if fact is None:
        raise ApiError(
            "LABEL_FACT_PROJECTION_MISSING",
            "LabelFact append 成功但强事实投影不可见",
            409,
        )
    return fact


def materialize_human_review_feedback(
    session: Session,
    ctx: RequestContext,
    *,
    decision_id: str,
    review_task_id: str,
    decision: str,
    note: str | None,
    target_resources: list[JsonResource],
    target_befores: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    if decision == "escalated":
        return []
    affected: list[dict[str, str]] = []
    feedback_type = {
        "accepted": "human-confirmed",
        "modified": "human-modified",
        "rejected": "rejected-badcase",
    }[decision]
    for target in target_resources:
        if target.collection not in {
            "label_aggregates",
            "label_taxonomy_suggestions",
            "prompt_version_candidates",
        }:
            continue
        target_type = {
            "label_aggregates": "label-aggregate",
            "label_taxonomy_suggestions": "taxonomy-suggestion",
            "prompt_version_candidates": "prompt-version-candidate",
        }[target.collection]
        target_id = str(target.resource_key)
        feedback_id = f"fb_{canonical_sha256([decision_id, target_type, target_id])[:24]}"
        before = target_befores.get(f"{target.collection}:{target_id}", {})
        after = dict(target.data)
        feedback = FeedbackExample(
            feedback_example_id=feedback_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            review_decision_id=decision_id,
            review_task_id=review_task_id,
            target_type=target_type,
            target_id=target_id,
            feedback_type=feedback_type,
            reason_code=note,
            field_diff=_field_diff(before, after),
            before_json=before,
            after_json=after,
            gold_status="candidate",
            trace_id=ctx.trace_id,
        )
        session.add(feedback)
        affected.append({"type": "feedback_example", "id": feedback_id})
        if target.collection == "label_aggregates":
            aggregate = session.scalar(
                select(LabelAggregate).where(
                    LabelAggregate.aggregate_id == target_id,
                    LabelAggregate.tenant_id == ctx.tenant_id,
                    LabelAggregate.project_id == ctx.project_id,
                )
            )
            if aggregate is None:
                raise ApiError(
                    "LABEL_AGGREGATE_PROJECTION_MISSING",
                    "人审目标缺少 LabelAggregate 强表",
                    409,
                )
            aggregate.status = {
                "accepted": "accepted",
                "modified": "accepted",
                "rejected": "rejected",
            }[decision]
            aggregate.trace_id = ctx.trace_id
            if decision in {"accepted", "modified"}:
                fact = _create_label_fact(
                    session,
                    ctx,
                    aggregate,
                    authority="human-confirmed",
                    review_decision_id=decision_id,
                    value=target.data.get("value", aggregate.value_json),
                )
                session.flush()
                affected.append({"type": "label_fact", "id": fact.fact_id})
            if decision in {"modified", "rejected"}:
                badcase = create_label_badcase(
                    session,
                    ctx,
                    LabelBadcaseCreateRequest(
                        capability="labeling",
                        failure_reason=(
                            "human-modified" if decision == "modified" else "human-rejected"
                        ),
                        severity=("high" if aggregate.risk_level == "high" else "medium"),
                        source_ref={"type": "label-aggregate", "id": aggregate.aggregate_id},
                        evidence_refs=[],
                        label_version_id=aggregate.label_version_id,
                        aggregate_id=aggregate.aggregate_id,
                        review_decision_id=decision_id,
                        expected_value=(
                            target.data.get("value") if decision == "modified" else None
                        ),
                        actual_value=before.get("value", aggregate.value_json),
                        field_diff=_field_diff(before, after),
                    ),
                )
                affected.append({"type": "badcase", "id": badcase["badcase_id"]})
            if aggregate.risk_level != "high":
                _finalize_single_review_aggregate(
                    session,
                    ctx,
                    aggregate=aggregate,
                    target=target,
                    review_task_id=review_task_id,
                    review_decision_id=decision_id,
                )
        session.flush()
        record_audit(
            session,
            ctx,
            action="feedback_example.created",
            object_type="feedback_example",
            object_id=feedback_id,
            after={
                "feedback_example_id": feedback_id,
                "feedback_type": feedback_type,
                "target_type": target_type,
                "target_id": target_id,
                "gold_status": "candidate",
            },
        )
        enqueue_event(
            session,
            ctx,
            event_type="feedback_example.created",
            aggregate_type="feedback_example",
            aggregate_id=feedback_id,
            payload={
                "feedback_example_id": feedback_id,
                "review_decision_id": decision_id,
                "target_type": target_type,
                "target_id": target_id,
                "feedback_type": feedback_type,
                "gold_status": "candidate",
            },
        )
    return affected


def _finalize_single_review_aggregate(
    session: Session,
    ctx: RequestContext,
    *,
    aggregate: LabelAggregate,
    target: JsonResource,
    review_task_id: str,
    review_decision_id: str,
) -> None:
    if aggregate.review_task_id != review_task_id:
        raise ApiError(
            "LABEL_AGGREGATE_REVIEW_TASK_BINDING_MISMATCH",
            "LabelAggregate 强表 review_task_id 与终态决策不一致",
            409,
        )
    runs = list(
        session.scalars(
            select(LabelAggregationRun)
            .where(
                LabelAggregationRun.tenant_id == ctx.tenant_id,
                LabelAggregationRun.project_id == ctx.project_id,
            )
            .with_for_update()
        )
    )
    matching = [run for run in runs if review_task_id in (run.payload.get("review_task_ids") or [])]
    if len(matching) != 1 or matching[0].aggregation_run_id != aggregate.aggregation_run_id:
        raise ApiError(
            "LABEL_AGGREGATION_RUN_REVIEW_BINDING_INVALID",
            "审核任务必须且只能绑定 LabelAggregate 所属的聚合运行",
            409,
        )
    run = matching[0]
    aggregate_before = {
        "status": aggregate.status,
        "review_task_id": aggregate.review_task_id,
    }
    run_before = {
        "status": run.status,
        "review_task_ids": list(run.payload.get("review_task_ids") or []),
    }
    aggregate.review_task_id = None
    target.data = {
        **target.data,
        "status": aggregate.status,
        "review_task_id": None,
        "review_decision_id": review_decision_id,
        "trace_id": ctx.trace_id,
    }
    target.status = aggregate.status
    target.trace_id = ctx.trace_id
    remaining = [item for item in run_before["review_task_ids"] if item != review_task_id]
    run.status = "awaiting-review" if remaining else "completed"
    run.trace_id = ctx.trace_id
    run.payload = {
        **run.payload,
        "status": run.status,
        "review_task_ids": remaining,
        "completed_at": datetime.now(UTC).isoformat() if not remaining else None,
    }
    record_audit(
        session,
        ctx,
        action="label_aggregate.review_completed",
        object_type="label_aggregate",
        object_id=aggregate.aggregate_id,
        before=aggregate_before,
        after={
            "status": aggregate.status,
            "review_task_id": None,
            "review_decision_id": review_decision_id,
        },
    )
    record_audit(
        session,
        ctx,
        action="label_aggregation_run.review_progressed",
        object_type="label_aggregation_run",
        object_id=run.aggregation_run_id,
        before=run_before,
        after={"status": run.status, "review_task_ids": remaining},
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_aggregate.review_completed",
        aggregate_type="label_aggregate",
        aggregate_id=aggregate.aggregate_id,
        payload={
            "aggregate_id": aggregate.aggregate_id,
            "review_decision_id": review_decision_id,
            "status": aggregate.status,
            "review_task_id": None,
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type=(
            "label_aggregation_run.completed"
            if not remaining
            else "label_aggregation_run.review_progressed"
        ),
        aggregate_type="label_aggregation_run",
        aggregate_id=run.aggregation_run_id,
        payload={"status": run.status, "review_task_ids": remaining},
    )


def _field_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(before) | set(after))
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in keys
        if before.get(key) != after.get(key)
    }


def create_label_badcase(
    session: Session,
    ctx: RequestContext,
    body: LabelBadcaseCreateRequest,
) -> dict[str, Any]:
    badcase_id = body.badcase_id or f"badcase_{uuid.uuid4().hex[:20]}"
    if session.get(Badcase, badcase_id) is not None:
        raise ApiError("BADCASE_ALREADY_EXISTS", "Badcase 已存在", 409)
    payload = body.model_dump(mode="json", exclude_none=True)
    severity_score = {"low": 10.0, "medium": 30.0, "high": 70.0, "critical": 100.0}[body.severity]
    record = Badcase(
        badcase_id=badcase_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status="pending-attribution",
        trace_id=ctx.trace_id,
        payload=payload,
        capability=body.capability,
        error_type=None,
        standard_term=body.label_version_id,
        recognized_text=None,
        evidence_ref=f"{body.source_ref['type']}:{body.source_ref['id']}",
        evidence_storage_object_id=None,
        evidence_level="discovery",
        hotword_pack_version_id=None,
        expected_count=1,
        correct_count=0,
        weighted_error_count=1.0,
        manual_correction_count=0,
        priority_score=severity_score,
        candidate_state="suspected",
        root_cause=body.failure_reason,
        fix_suggestion=None,
        downstream_impact={
            "aggregate_id": body.aggregate_id,
            "prompt_version_id": body.prompt_version_id,
            "review_decision_id": body.review_decision_id,
        },
        resource_version=1,
        root_trace_id=ctx.trace_id,
        current_trace_id=ctx.trace_id,
    )
    session.add(record)
    session.flush()
    data = label_badcase_data(record)
    record_audit(
        session,
        ctx,
        action="badcase.create",
        object_type="badcase",
        object_id=badcase_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="badcase.created",
        aggregate_type="badcase",
        aggregate_id=badcase_id,
        payload={
            "badcase_id": badcase_id,
            "capability": record.capability,
            "failure_reason": body.failure_reason,
            "root_trace_id": record.root_trace_id,
        },
    )
    return data


def label_badcase_data(record: Badcase) -> dict[str, Any]:
    return {
        "id": record.badcase_id,
        "badcase_id": record.badcase_id,
        "capability": record.capability,
        "failure_reason": record.payload.get("failure_reason") or record.root_cause,
        "severity": record.payload.get("severity"),
        "source_ref": record.payload.get("source_ref"),
        "evidence_refs": record.payload.get("evidence_refs", []),
        "label_version_id": record.payload.get("label_version_id"),
        "prompt_version_id": record.payload.get("prompt_version_id"),
        "hotword_pack_version_id": record.hotword_pack_version_id,
        "aggregate_id": record.payload.get("aggregate_id"),
        "review_decision_id": record.payload.get("review_decision_id"),
        "expected_value": record.payload.get("expected_value"),
        "actual_value": record.payload.get("actual_value"),
        "field_diff": record.payload.get("field_diff", {}),
        "status": record.status,
        "resource_version": record.resource_version,
        "root_trace_id": record.root_trace_id,
        "current_trace_id": record.current_trace_id,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }
