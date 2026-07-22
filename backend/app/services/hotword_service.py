from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    AsrAnnotationCorrection,
    AssetMaterialization,
    Badcase,
    HotwordMetricSnapshot,
    HotwordPack,
    HotwordPackVersion,
    HotwordVersionItem,
    JsonResource,
    RunRecord,
)
from app.schemas.hotwords import (
    HotwordBadcaseCreateRequest,
    HotwordBadcaseDecisionRequest,
    HotwordBadcasePatchRequest,
    HotwordEvalMetrics,
    HotwordEvalRunRequest,
    HotwordItemCreateRequest,
    HotwordItemPatchRequest,
    HotwordPackCreateRequest,
    HotwordPackVersionCreateRequest,
    HotwordPackVersionPatchRequest,
    HotwordPublishRequest,
)
from app.services.audio_intelligence_service import validate_scoped_storage_object_reference
from app.services.audit_service import record_audit
from app.services.eval_dataset_service import locked_eval_dataset_snapshot
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource
from app.services.run_service import get_run

PACK_MANAGE_ROLES = frozenset({"project_admin", "model_engineer"})
HOTWORD_READ_ROLES = frozenset(
    {"project_admin", "model_engineer", "asset_manager", "review_arbitrator", "annotator"}
)
BADCASE_EVIDENCE_READ_ROLES = frozenset(
    {"project_admin", "model_engineer", "review_arbitrator", "annotator"}
)
BADCASE_WRITE_ROLES = frozenset(
    {"project_admin", "model_engineer", "review_arbitrator", "annotator"}
)
BADCASE_EVIDENCE_SOURCE_TYPES = frozenset(
    {"asr_hotword_evidence", "audio_intelligence", "hotword_analysis", "hotword_eval"}
)
VERSION_MUTABLE_STATUSES = frozenset({"draft", "validating", "gate_blocked"})
HOTWORD_ERROR_TYPES = frozenset(
    {"missing_term", "misrecognition", "alias_gap", "weight_issue", "false_boost"}
)
EVIDENCE_CONFIDENCE = {
    "gold": 1.0,
    "human-confirmed": 1.0,
    "business-master": 0.8,
    "discovery": 0.4,
}
TRUSTED_BADCASE_STATUSES = frozenset({"pending-backflow", "in-regression"})
PROVIDER_COMPILATION_LIMITS: dict[str, dict[str, int]] = {
    "auris-audio-stack": {"max_items": 5000, "max_aliases_per_item": 32},
    "ali-nls-prod": {"max_items": 1000, "max_aliases_per_item": 10},
    "volc-bigmodel-audio": {"max_items": 5000, "max_aliases_per_item": 20},
    "whisperx-pyannote": {"max_items": 5000, "max_aliases_per_item": 32},
}
PROVIDER_ALIASES = {"audio_intelligence_default": "auris-audio-stack"}
HOTWORD_BASELINE_MODE_PUBLISHED_VERSION = "published_version"
HOTWORD_BASELINE_MODE_NO_HOTWORD = "no_hotword"
HOTWORD_BASELINE_REF_NO_HOTWORD = "baseline:no-hotword"
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"validating", "archived"}),
    "validating": frozenset({"draft", "ready_for_eval", "archived"}),
    "ready_for_eval": frozenset({"evaluating", "archived"}),
    "evaluating": frozenset({"gate_blocked", "review_required"}),
    "gate_blocked": frozenset({"validating", "ready_for_eval", "archived"}),
    "review_required": frozenset({"approved", "gate_blocked", "archived"}),
    "approved": frozenset({"published", "ready_for_eval", "archived"}),
    "published": frozenset({"deprecated", "rolled_back"}),
    "deprecated": frozenset({"archived"}),
    "rolled_back": frozenset({"archived"}),
    "archived": frozenset(),
}

PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PLATE_PATTERN = re.compile(
    r"^[\u4e00-\u9fff][A-HJ-NP-Z][A-HJ-NP-Z0-9]{5,6}$",
    re.IGNORECASE,
)
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
PERSON_NAME_HINT_PATTERN = re.compile(
    r"^(?:客户|联系人)?[\u4e00-\u9fff]{1,4}(?:先生|女士|小姐|老师|总)$"
)
SENSITIVE_CATEGORIES = frozenset(
    {
        "customer_name",
        "customer-name",
        "person",
        "person_name",
        "phone",
        "mobile",
        "license_plate",
        "plate",
        "vin",
    }
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _snapshot_nonnegative_integer(raw: dict[str, Any], index: int, field: str) -> int:
    value = raw.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ApiError(
            "HOTWORD_METRIC_COUNT_INVALID",
            f"第 {index + 1} 个热词指标快照 {field} 无效",
            422,
        )
    return value


def normalize_hotword(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    left = 0
    right = len(normalized)
    while left < right and unicodedata.category(normalized[left])[0] in {"P", "Z"}:
        left += 1
    while right > left and unicodedata.category(normalized[right - 1])[0] in {"P", "Z"}:
        right -= 1
    normalized = normalized[left:right].strip()
    if not normalized:
        raise ApiError("HOTWORD_TERM_EMPTY", "热词规范化后不能为空", 422)
    return normalized.casefold()


def ensure_hotword_is_not_sensitive(term: str, category: str) -> None:
    compact = re.sub(r"[\s-]", "", unicodedata.normalize("NFKC", term))
    normalized_category = category.strip().casefold()
    is_sensitive = (
        normalized_category in SENSITIVE_CATEGORIES
        or PHONE_PATTERN.search(compact) is not None
        or PLATE_PATTERN.fullmatch(compact) is not None
        or VIN_PATTERN.fullmatch(compact) is not None
        or PERSON_NAME_HINT_PATTERN.fullmatch(compact) is not None
    )
    if is_sensitive:
        raise ApiError(
            "HOTWORD_SENSITIVE_TERM_FORBIDDEN",
            "客户姓名、手机号、车牌和 VIN 等敏感实体禁止进入热词包",
            422,
        )


def canonicalize_hotword_provider(provider: str | None) -> str:
    normalized = (provider or "").strip().casefold()
    if not normalized:
        raise ApiError("HOTWORD_PROVIDER_REQUIRED", "热词编译必须指定 provider", 422)
    return PROVIDER_ALIASES.get(normalized, normalized)


def validate_provider_compilation(
    provider: str, *, item_count: int, alias_counts: list[int]
) -> str:
    canonical_provider = canonicalize_hotword_provider(provider)
    limits = PROVIDER_COMPILATION_LIMITS.get(canonical_provider)
    if limits is None:
        raise ApiError(
            "HOTWORD_PROVIDER_UNSUPPORTED",
            f"不支持的热词编译 provider：{provider}",
            422,
            details=[{"supported_providers": sorted(PROVIDER_COMPILATION_LIMITS)}],
        )
    if item_count > limits["max_items"]:
        raise ApiError(
            "HOTWORD_PROVIDER_ITEM_LIMIT_EXCEEDED",
            "热词项数量超过 provider 编译上限",
            422,
            details=[
                {
                    "provider": canonical_provider,
                    "item_count": item_count,
                    "max_items": limits["max_items"],
                }
            ],
        )
    if alias_counts and max(alias_counts) > limits["max_aliases_per_item"]:
        raise ApiError(
            "HOTWORD_PROVIDER_ALIAS_LIMIT_EXCEEDED",
            "单个热词的显式别名数量超过 provider 编译上限",
            422,
            details=[
                {
                    "provider": canonical_provider,
                    "max_aliases_per_item": limits["max_aliases_per_item"],
                }
            ],
        )
    return canonical_provider


def calculate_hotword_metrics(
    *,
    expected_count: int,
    correct_count: int,
    weighted_error_count: float,
    false_insert_count: int,
    recognized_hotword_count: int,
) -> dict[str, float]:
    return {
        "recall_rate": correct_count / expected_count if expected_count else 0.0,
        "error_rate": weighted_error_count / expected_count if expected_count else 0.0,
        "false_boost_rate": (
            false_insert_count / recognized_hotword_count if recognized_hotword_count else 0.0
        ),
    }


def classify_hotword_candidate(
    *, expected_count: int, error_rate: float, manual_corrections: int
) -> str:
    if (expected_count >= 3 and error_rate >= 0.2) or manual_corrections >= 2:
        return "confirmed"
    return "suspected"


def priority_score(
    expected_count: int,
    error_rate: float,
    evidence_confidence: float,
    business_weight: float,
) -> float:
    raw = math.log1p(expected_count) * error_rate * evidence_confidence * business_weight * 100
    return round(max(0.0, min(100.0, raw)), 2)


def evaluate_release_gate(
    *, baseline: dict[str, float | int | None], candidate: dict[str, float | int | None]
) -> dict[str, Any]:
    def number(source: dict[str, float | int | None], key: str) -> float:
        value = source.get(key)
        return (
            float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
        )

    reasons: list[str] = []
    if number(candidate, "trusted_occurrences") < 30:
        reasons.append("minimum_sample_size")
    if number(candidate, "unique_terms") < 3:
        reasons.append("minimum_unique_terms")

    baseline_error = number(baseline, "error_rate")
    candidate_error = number(candidate, "error_rate")
    relative_error_improvement = (
        (baseline_error - candidate_error) / baseline_error if baseline_error else 0.0
    )
    recall_improvement = number(candidate, "recall_rate") - number(baseline, "recall_rate")
    if relative_error_improvement < 0.2 and recall_improvement < 0.03:
        reasons.append("hotword_improvement")
    if number(candidate, "false_boost_rate") - number(baseline, "false_boost_rate") > 0.005:
        reasons.append("false_boost_regression")
    if number(candidate, "cer") - number(baseline, "cer") > 0.002:
        reasons.append("cer_regression")
    if number(candidate, "wer") - number(baseline, "wer") > 0.002:
        reasons.append("wer_regression")
    if number(baseline, "downstream_f1") - number(candidate, "downstream_f1") > 0.005:
        reasons.append("downstream_f1_regression")
    baseline_latency = number(baseline, "p95_latency_ms")
    if baseline_latency and number(candidate, "p95_latency_ms") / baseline_latency > 1.05:
        reasons.append("latency_regression")
    baseline_cost = number(baseline, "cost_per_minute")
    if baseline_cost and number(candidate, "cost_per_minute") / baseline_cost > 1.05:
        reasons.append("cost_regression")
    return {
        "passed": not reasons,
        "blocked_reasons": reasons,
        "relative_error_improvement": round(relative_error_improvement, 6),
        "recall_improvement": round(recall_improvement, 6),
    }


def _assert_expected_version(expected: int, current: int, object_name: str) -> None:
    if expected == current:
        return
    raise ApiError(
        "RESOURCE_VERSION_CONFLICT",
        f"{object_name}已被其他请求更新，请刷新后重试",
        409,
        details=[{"expected_resource_version": expected, "current_resource_version": current}],
    )


def _get_pack(
    session: Session,
    ctx: RequestContext,
    pack_id: str,
    *,
    for_update: bool = False,
) -> HotwordPack:
    statement = select(HotwordPack).where(
        HotwordPack.pack_id == pack_id,
        HotwordPack.tenant_id == ctx.tenant_id,
        HotwordPack.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    pack = session.scalar(statement)
    if pack is None:
        raise ApiError("HOTWORD_PACK_NOT_FOUND", f"热词包不存在：{pack_id}", 404)
    return pack


def get_hotword_version(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    *,
    for_update: bool = False,
) -> HotwordPackVersion:
    statement = select(HotwordPackVersion).where(
        HotwordPackVersion.version_id == version_id,
        HotwordPackVersion.tenant_id == ctx.tenant_id,
        HotwordPackVersion.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    version = session.scalar(statement)
    if version is None:
        raise ApiError("HOTWORD_VERSION_NOT_FOUND", f"热词包版本不存在：{version_id}", 404)
    return version


def _get_item(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    item_id: str,
    *,
    for_update: bool = False,
) -> HotwordVersionItem:
    statement = select(HotwordVersionItem).where(
        HotwordVersionItem.item_id == item_id,
        HotwordVersionItem.version_id == version_id,
        HotwordVersionItem.tenant_id == ctx.tenant_id,
        HotwordVersionItem.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = session.scalar(statement)
    if item is None:
        raise ApiError("HOTWORD_ITEM_NOT_FOUND", f"热词项不存在：{item_id}", 404)
    return item


def _pack_data(pack: HotwordPack) -> dict[str, Any]:
    return {
        "id": pack.pack_id,
        "pack_id": pack.pack_id,
        "name": pack.name,
        "language": pack.language,
        "domain": pack.domain,
        "status": pack.status,
        "current_version_id": pack.current_version_id,
        "production_version_id": pack.production_version_id,
        "resource_version": pack.resource_version,
        "root_trace_id": pack.root_trace_id,
        "current_trace_id": pack.current_trace_id,
        "created_at": _iso(pack.created_at),
        "updated_at": _iso(pack.updated_at),
    }


def _item_data(item: HotwordVersionItem) -> dict[str, Any]:
    return {
        "id": item.item_id,
        "item_id": item.item_id,
        "version_id": item.version_id,
        "canonical_term": item.canonical_term,
        "normalized_term": item.normalized_term,
        "aliases": item.aliases,
        "category": item.category,
        "weight": item.weight,
        "source_badcase_id": item.source_badcase_id,
        "source_type": item.source_type,
        "resource_version": item.resource_version,
        "root_trace_id": item.root_trace_id,
        "current_trace_id": item.current_trace_id,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


def _version_data(
    session: Session, version: HotwordPackVersion, *, include_items: bool = False
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": version.version_id,
        "version_id": version.version_id,
        "pack_id": version.pack_id,
        "version": version.version,
        "baseline_version_id": version.baseline_version_id,
        "status": version.status,
        "content_sha256": version.content_sha256,
        "manifest_storage_object_id": version.manifest_storage_object_id,
        "eval_run_id": version.eval_run_id,
        "eval_locked": version.eval_locked,
        "model_approved_by": version.model_approved_by,
        "project_admin_confirmed_by": version.project_admin_confirmed_by,
        "provider_artifact_ref": version.provider_artifact_ref,
        "compiled_provider": version.compiled_provider,
        "build_run_id": (version.payload or {}).get("build_run_id"),
        "publish_run_id": (version.payload or {}).get("publish_run_id"),
        "task_version_id": (version.payload or {}).get("task_version_id"),
        "production_active": bool((version.payload or {}).get("production_active", False)),
        "production_task_version_id": (version.payload or {}).get("production_task_version_id"),
        "task_type_id": (version.payload or {}).get("task_type_id"),
        "production_activated_at": (version.payload or {}).get("production_activated_at"),
        "artifact_sha256": (version.payload or {}).get("artifact_sha256"),
        "inherited_item_count": int((version.payload or {}).get("inherited_item_count") or 0),
        "resource_version": version.resource_version,
        "root_trace_id": version.root_trace_id,
        "current_trace_id": version.current_trace_id,
        "published_at": _iso(version.published_at),
        "created_at": _iso(version.created_at),
        "updated_at": _iso(version.updated_at),
    }
    if include_items:
        items = session.scalars(
            select(HotwordVersionItem)
            .where(
                HotwordVersionItem.tenant_id == version.tenant_id,
                HotwordVersionItem.project_id == version.project_id,
                HotwordVersionItem.version_id == version.version_id,
            )
            .order_by(HotwordVersionItem.created_at, HotwordVersionItem.item_id)
        ).all()
        data["items"] = [_item_data(item) for item in items]
    return data


def _hotword_task_type_binding(
    session: Session,
    ctx: RequestContext,
    version: HotwordPackVersion,
    pack: HotwordPack,
    scene_binding: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    from app.schemas.scene_profiles import SceneProfileManifest

    scene_version = scene_binding.get("version")
    manifest_data = scene_version.get("manifest") if isinstance(scene_version, dict) else None
    manifest = SceneProfileManifest.model_validate(manifest_data)
    allowed_task_types = list(manifest.task_type_refs)
    version_payload = version.payload if isinstance(version.payload, dict) else {}
    explicit_task_type = str(version_payload.get("task_type_id") or "").strip()

    source_version_ids: list[str] = []
    if version.baseline_version_id:
        source_version_ids.append(version.baseline_version_id)
    if pack.production_version_id and pack.production_version_id not in source_version_ids:
        source_version_ids.append(pack.production_version_id)

    task_version_refs: list[dict[str, str]] = []
    for source_version_id in source_version_ids:
        source_version = get_hotword_version(session, ctx, source_version_id)
        source_payload = source_version.payload if isinstance(source_version.payload, dict) else {}
        for field in ("production_task_version_id", "task_version_id"):
            task_version_id = str(source_payload.get(field) or "").strip()
            if task_version_id and all(
                item["task_version_id"] != task_version_id for item in task_version_refs
            ):
                task_version_refs.append(
                    {
                        "source_hotword_pack_version_id": source_version_id,
                        "task_version_id": task_version_id,
                    }
                )

    inherited_bindings: list[dict[str, str]] = []
    for reference in task_version_refs:
        task_version = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == ctx.tenant_id,
                JsonResource.project_id == ctx.project_id,
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == reference["task_version_id"],
            )
        )
        task_data = task_version.data if task_version is not None else {}
        task_type_id = str(
            task_data.get("task_type_id") if isinstance(task_data, dict) else ""
        ).strip()
        if task_type_id:
            model_version = str(
                task_data.get("model_version") if isinstance(task_data, dict) else ""
            ).strip()
            inherited_bindings.append(
                {
                    **reference,
                    "task_type_id": task_type_id,
                    **({"model_version": model_version} if model_version else {}),
                }
            )

    inherited_task_types = sorted({binding["task_type_id"] for binding in inherited_bindings})
    if len(inherited_task_types) > 1:
        raise ApiError(
            "HOTWORD_TASK_TYPE_BINDING_AMBIGUOUS",
            "热词包基线关联了多个任务类型，必须先修复版本血缘",
            409,
            details=inherited_bindings,
        )
    inherited_task_type = inherited_task_types[0] if inherited_task_types else ""
    if explicit_task_type and inherited_task_type and explicit_task_type != inherited_task_type:
        raise ApiError(
            "HOTWORD_TASK_TYPE_REBIND_FORBIDDEN",
            "热词候选版本不能改变基线任务类型；请新建独立热词包",
            409,
            details=[
                {
                    "explicit_task_type_id": explicit_task_type,
                    "baseline_task_type_id": inherited_task_type,
                }
            ],
        )

    if explicit_task_type:
        task_type_id = explicit_task_type
        binding_source = "explicit_version_binding"
    elif inherited_task_type:
        task_type_id = inherited_task_type
        binding_source = "baseline_task_version"
    elif len(allowed_task_types) == 1:
        task_type_id = allowed_task_types[0]
        binding_source = "single_scene_task_type"
    else:
        raise ApiError(
            "HOTWORD_TASK_TYPE_BINDING_REQUIRED",
            "当前场景包含多个任务类型，热词候选版本必须显式绑定 task_type_id",
            409,
            details=[
                {
                    "scene_profile_version_id": scene_binding["scene_profile_version_id"],
                    "allowed_task_type_refs": allowed_task_types,
                    "task_version_refs": task_version_refs,
                }
            ],
        )
    if task_type_id not in set(allowed_task_types):
        raise ApiError(
            "HOTWORD_TASK_TYPE_NOT_IN_SCENE_PROFILE",
            "热词候选版本绑定的任务类型未被当前 SceneProfile 声明",
            409,
            details=[
                {
                    "task_type_id": task_type_id,
                    "scene_profile_version_id": scene_binding["scene_profile_version_id"],
                    "allowed_task_type_refs": allowed_task_types,
                }
            ],
        )
    inherited_model_versions = sorted(
        {binding["model_version"] for binding in inherited_bindings if binding.get("model_version")}
    )
    if len(inherited_model_versions) > 1:
        raise ApiError(
            "HOTWORD_TASK_MODEL_BINDING_AMBIGUOUS",
            "热词包基线关联了多个模型版本，必须先修复版本血缘",
            409,
            details=inherited_bindings,
        )
    return task_type_id, {
        "source": binding_source,
        "source_task_versions": inherited_bindings,
        "scene_profile_version_id": scene_binding["scene_profile_version_id"],
        "model_version": inherited_model_versions[0] if inherited_model_versions else None,
    }


def _badcase_data(record: Badcase, *, include_evidence: bool = True) -> dict[str, Any]:
    data = {
        "id": record.badcase_id,
        "badcase_id": record.badcase_id,
        "capability": record.capability,
        "standard_term": record.standard_term,
        "recognized_text": record.recognized_text if include_evidence else "[redacted]",
        "error_type": record.error_type,
        "evidence_ref": record.evidence_ref if include_evidence else "redacted://forbidden",
        "evidence_storage_object_id": (
            record.evidence_storage_object_id if include_evidence else None
        ),
        "evidence_level": record.evidence_level,
        "hotword_pack_version_id": record.hotword_pack_version_id,
        "expected_count": record.expected_count,
        "correct_count": record.correct_count,
        "weighted_error_count": record.weighted_error_count,
        "manual_correction_count": record.manual_correction_count,
        "candidate_state": record.candidate_state,
        "priority_score": record.priority_score,
        "root_cause": record.root_cause,
        "fix_suggestion": record.fix_suggestion,
        "downstream_impact": record.downstream_impact,
        "status": record.status,
        "resource_version": record.resource_version,
        "root_trace_id": record.root_trace_id,
        "current_trace_id": record.current_trace_id,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }
    if record.capability in {"labeling", "prompt-optimization"}:
        data.update(
            {
                "failure_reason": record.payload.get("failure_reason") or record.root_cause,
                "severity": record.payload.get("severity"),
                "source_ref": record.payload.get("source_ref"),
                "evidence_refs": record.payload.get("evidence_refs", []),
                "label_version_id": record.payload.get("label_version_id"),
                "prompt_version_id": record.payload.get("prompt_version_id"),
                "aggregate_id": record.payload.get("aggregate_id"),
                "review_decision_id": record.payload.get("review_decision_id"),
                "expected_value": record.payload.get("expected_value"),
                "actual_value": record.payload.get("actual_value"),
                "field_diff": record.payload.get("field_diff", {}),
            }
        )
    return data


def create_hotword_pack(
    session: Session, ctx: RequestContext, body: HotwordPackCreateRequest
) -> dict[str, Any]:
    existing = session.scalar(
        select(HotwordPack).where(
            HotwordPack.tenant_id == ctx.tenant_id,
            HotwordPack.project_id == ctx.project_id,
            HotwordPack.name == body.name,
            HotwordPack.language == body.language,
            HotwordPack.domain == body.domain,
        )
    )
    if existing is not None:
        raise ApiError("HOTWORD_PACK_ALREADY_EXISTS", "同名热词包已存在", 409)
    pack = HotwordPack(
        pack_id=body.pack_id or _new_id("hwp"),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        name=body.name,
        language=body.language,
        domain=body.domain,
        status="active",
        current_version_id=None,
        resource_version=1,
        root_trace_id=ctx.trace_id,
        current_trace_id=ctx.trace_id,
    )
    session.add(pack)
    session.flush()
    data = _pack_data(pack)
    record_audit(
        session,
        ctx,
        action="hotword_pack.create",
        object_type="hotword_pack",
        object_id=pack.pack_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack.created",
        aggregate_type="hotword_pack",
        aggregate_id=pack.pack_id,
        payload={
            "pack_id": pack.pack_id,
            "name": pack.name,
            "language": pack.language,
            "domain": pack.domain,
            "resource_version": pack.resource_version,
            "root_trace_id": pack.root_trace_id,
        },
    )
    return data


def list_hotword_packs(
    session: Session, ctx: RequestContext, *, status: str | None, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    filters = [
        HotwordPack.tenant_id == ctx.tenant_id,
        HotwordPack.project_id == ctx.project_id,
    ]
    if status:
        filters.append(HotwordPack.status == status)
    total = int(session.scalar(select(func.count()).select_from(HotwordPack).where(*filters)) or 0)
    records = session.scalars(
        select(HotwordPack)
        .where(*filters)
        .order_by(HotwordPack.updated_at.desc(), HotwordPack.pack_id)
        .offset(offset)
        .limit(limit)
    ).all()
    return [_pack_data(record) for record in records], total


def create_hotword_version(
    session: Session,
    ctx: RequestContext,
    pack_id: str,
    body: HotwordPackVersionCreateRequest,
) -> dict[str, Any]:
    pack = _get_pack(session, ctx, pack_id)
    if body.manifest_storage_object_id is not None:
        raise ApiError(
            "HOTWORD_BUILD_WORKFLOW_REQUIRED",
            "manifest 必须由受控热词构建运行完成回执固化，不能由客户端直接写入",
            409,
        )
    duplicate = session.scalar(
        select(HotwordPackVersion.version_id).where(
            HotwordPackVersion.tenant_id == ctx.tenant_id,
            HotwordPackVersion.project_id == ctx.project_id,
            HotwordPackVersion.pack_id == pack_id,
            HotwordPackVersion.version == body.version,
        )
    )
    if duplicate is not None:
        raise ApiError("HOTWORD_VERSION_ALREADY_EXISTS", "该词包版本号已存在", 409)
    baseline_version_id = body.baseline_version_id or pack.current_version_id
    baseline: HotwordPackVersion | None = None
    if baseline_version_id:
        baseline = get_hotword_version(session, ctx, baseline_version_id)
        if baseline.pack_id != pack_id:
            raise ApiError("HOTWORD_BASELINE_PACK_MISMATCH", "基线版本不属于当前热词包", 422)
        if pack.current_version_id and baseline.version_id != pack.current_version_id:
            raise ApiError(
                "HOTWORD_BASELINE_NOT_CURRENT",
                "候选版本必须从逻辑词包当前版本创建",
                409,
                details=[
                    {
                        "baseline_version_id": baseline.version_id,
                        "current_version_id": pack.current_version_id,
                    }
                ],
            )
        if baseline.status != "published":
            raise ApiError(
                "HOTWORD_BASELINE_NOT_PUBLISHED",
                "候选版本的基线必须是已发布不可变版本",
                409,
                details=[{"baseline_version_id": baseline.version_id, "status": baseline.status}],
            )
    version = HotwordPackVersion(
        version_id=body.version_id or _new_id("hwpv"),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        pack_id=pack_id,
        version=body.version,
        baseline_version_id=baseline_version_id,
        status="draft",
        content_sha256=None,
        manifest_storage_object_id=body.manifest_storage_object_id,
        eval_run_id=None,
        eval_locked=False,
        model_approved_by=None,
        project_admin_confirmed_by=None,
        provider_artifact_ref=None,
        compiled_provider=None,
        resource_version=1,
        root_trace_id=pack.root_trace_id,
        current_trace_id=ctx.trace_id,
        payload={
            "legacy_hotwords_ref": None,
            "inherited_item_count": 0,
            "task_type_id": body.task_type_id,
        },
    )
    session.add(version)
    session.flush()
    inherited_item_count = 0
    if baseline is not None:
        baseline_items = session.scalars(
            select(HotwordVersionItem)
            .where(
                HotwordVersionItem.tenant_id == ctx.tenant_id,
                HotwordVersionItem.project_id == ctx.project_id,
                HotwordVersionItem.version_id == baseline.version_id,
            )
            .order_by(HotwordVersionItem.created_at, HotwordVersionItem.item_id)
        ).all()
        for baseline_item in baseline_items:
            session.add(
                HotwordVersionItem(
                    item_id=_new_id("hwpi"),
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    version_id=version.version_id,
                    canonical_term=baseline_item.canonical_term,
                    normalized_term=baseline_item.normalized_term,
                    aliases=list(baseline_item.aliases),
                    category=baseline_item.category,
                    weight=baseline_item.weight,
                    source_badcase_id=baseline_item.source_badcase_id,
                    source_type=baseline_item.source_type,
                    resource_version=1,
                    root_trace_id=version.root_trace_id,
                    current_trace_id=ctx.trace_id,
                )
            )
        inherited_item_count = len(baseline_items)
        session.flush()
        _recalculate_content_hash(session, version)
    version.payload = {
        **(version.payload or {}),
        "inherited_item_count": inherited_item_count,
    }
    session.flush()
    data = _version_data(session, version, include_items=True)
    record_audit(
        session,
        ctx,
        action="hotword_version.create",
        object_type="hotword_pack_version",
        object_id=version.version_id,
        after=data,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.created",
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload={
            "version_id": version.version_id,
            "pack_id": pack_id,
            "version": version.version,
            "baseline_version_id": version.baseline_version_id,
            "inherited_item_count": inherited_item_count,
            "resource_version": version.resource_version,
            "root_trace_id": version.root_trace_id,
        },
    )
    return data


def list_hotword_versions(
    session: Session,
    ctx: RequestContext,
    pack_id: str,
    *,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    _get_pack(session, ctx, pack_id)
    filters = [
        HotwordPackVersion.tenant_id == ctx.tenant_id,
        HotwordPackVersion.project_id == ctx.project_id,
        HotwordPackVersion.pack_id == pack_id,
    ]
    if status:
        filters.append(HotwordPackVersion.status == status)
    total = int(
        session.scalar(select(func.count()).select_from(HotwordPackVersion).where(*filters)) or 0
    )
    records = session.scalars(
        select(HotwordPackVersion)
        .where(*filters)
        .order_by(HotwordPackVersion.updated_at.desc(), HotwordPackVersion.version_id)
        .offset(offset)
        .limit(limit)
    ).all()
    can_read_candidate_details = bool(
        set(ctx.roles) & (set(PACK_MANAGE_ROLES) | {"review_arbitrator"})
    )
    items: list[dict[str, Any]] = []
    for record in records:
        data = _version_data(session, record)
        if not can_read_candidate_details and record.status not in {"published", "deprecated"}:
            for field in (
                "manifest_storage_object_id",
                "provider_artifact_ref",
                "eval_run_id",
                "model_approved_by",
                "project_admin_confirmed_by",
            ):
                data[field] = None
        items.append(data)
    return items, total


def _recalculate_content_hash(session: Session, version: HotwordPackVersion) -> None:
    items = session.scalars(
        select(HotwordVersionItem)
        .where(
            HotwordVersionItem.tenant_id == version.tenant_id,
            HotwordVersionItem.project_id == version.project_id,
            HotwordVersionItem.version_id == version.version_id,
        )
        .order_by(HotwordVersionItem.normalized_term)
    ).all()
    manifest = [
        {
            "normalized_term": item.normalized_term,
            "normalized_aliases": sorted(normalize_hotword(alias) for alias in item.aliases),
            "category": item.category,
            "weight": item.weight,
            "source_type": item.source_type,
            "source_badcase_id": item.source_badcase_id,
        }
        for item in items
    ]
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    if version.content_sha256 and version.content_sha256 != content_sha256:
        version.manifest_storage_object_id = None
        version.provider_artifact_ref = None
        version.compiled_provider = None
        version.eval_run_id = None
        version.eval_locked = False
        version.model_approved_by = None
        version.project_admin_confirmed_by = None
        payload = dict(version.payload or {})
        for field in ("artifact_sha256", "build_run_id"):
            payload.pop(field, None)
        version.payload = payload
    version.content_sha256 = content_sha256


def _assert_item_terms_unique(
    session: Session,
    version: HotwordPackVersion,
    *,
    canonical_term: str,
    aliases: list[str],
    exclude_item_id: str | None = None,
) -> str:
    normalized_canonical = normalize_hotword(canonical_term)
    normalized_aliases = [normalize_hotword(alias) for alias in aliases]
    requested_terms = [normalized_canonical, *normalized_aliases]
    if len(set(requested_terms)) != len(requested_terms):
        raise ApiError(
            "HOTWORD_ITEM_ALIAS_DUPLICATE",
            "规范词与显式别名按 NFKC/边界清理/大小写折叠后不得重复",
            409,
        )
    statement = select(HotwordVersionItem).where(
        HotwordVersionItem.tenant_id == version.tenant_id,
        HotwordVersionItem.project_id == version.project_id,
        HotwordVersionItem.version_id == version.version_id,
    )
    if exclude_item_id:
        statement = statement.where(HotwordVersionItem.item_id != exclude_item_id)
    conflicts: list[dict[str, str]] = []
    requested_set = set(requested_terms)
    for existing in session.scalars(statement).all():
        existing_terms = {
            normalize_hotword(existing.canonical_term),
            *(normalize_hotword(alias) for alias in existing.aliases),
        }
        for term in sorted(requested_set & existing_terms):
            conflicts.append({"normalized_term": term, "item_id": existing.item_id})
    if conflicts:
        raise ApiError(
            "HOTWORD_ITEM_DUPLICATE",
            "规范词或别名与当前版本已有词项冲突",
            409,
            details=conflicts,
        )
    return normalized_canonical


def _assert_items_mutable(version: HotwordPackVersion) -> None:
    build_in_progress = version.status == "validating" and bool(
        (version.payload or {}).get("build_run_id")
    )
    if version.status in VERSION_MUTABLE_STATUSES and not build_in_progress:
        return
    raise ApiError(
        "HOTWORD_VERSION_IMMUTABLE",
        f"状态为 {version.status} 的热词包版本不可修改词项",
        409,
    )


def _knowledge_candidate_ids(session: Session, version: HotwordPackVersion) -> list[str]:
    return list(
        session.scalars(
            select(HotwordVersionItem.item_id).where(
                HotwordVersionItem.tenant_id == version.tenant_id,
                HotwordVersionItem.project_id == version.project_id,
                HotwordVersionItem.version_id == version.version_id,
                HotwordVersionItem.source_type == "knowledge_candidate",
            )
        ).all()
    )


def _assert_no_knowledge_candidates(session: Session, version: HotwordPackVersion) -> None:
    candidate_ids = _knowledge_candidate_ids(session, version)
    if candidate_ids:
        raise ApiError(
            "HOTWORD_KNOWLEDGE_CANDIDATE_UNCONFIRMED",
            "知识库候选词必须经人工确认并转为 manual 后才能评测或发布",
            409,
            details=[{"item_ids": candidate_ids}],
        )


def _validate_item_source(
    session: Session,
    ctx: RequestContext,
    *,
    source_type: str,
    source_badcase_id: str | None,
    canonical_term: str,
) -> None:
    if source_type == "badcase":
        if not source_badcase_id:
            raise ApiError(
                "HOTWORD_SOURCE_BADCASE_REQUIRED",
                "source_type=badcase 时必须提供 source_badcase_id",
                422,
            )
        badcase = _get_badcase(session, ctx, source_badcase_id)
        if (
            badcase.capability != "asr-hotword"
            or badcase.candidate_state != "confirmed"
            or badcase.status not in {"pending-backflow", "in-regression"}
        ):
            raise ApiError(
                "HOTWORD_SOURCE_BADCASE_NOT_CONFIRMED",
                "只有已人工确认且待回流的 ASR 热词 Badcase 可以进入词包",
                409,
                details=[
                    {
                        "badcase_id": source_badcase_id,
                        "capability": badcase.capability,
                        "candidate_state": badcase.candidate_state,
                        "status": badcase.status,
                    }
                ],
            )
        if not badcase.standard_term or normalize_hotword(canonical_term) != normalize_hotword(
            badcase.standard_term
        ):
            raise ApiError(
                "HOTWORD_SOURCE_BADCASE_TERM_MISMATCH",
                "热词规范词必须与来源 Badcase 的标准词一致",
                409,
                details=[
                    {
                        "badcase_id": source_badcase_id,
                        "standard_term": badcase.standard_term,
                    }
                ],
            )
        return
    if source_badcase_id is not None:
        raise ApiError(
            "HOTWORD_SOURCE_REFERENCE_INVALID",
            "仅 source_type=badcase 可以引用 source_badcase_id",
            422,
        )


def create_hotword_item(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    body: HotwordItemCreateRequest,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_items_mutable(version)
    ensure_hotword_is_not_sensitive(body.canonical_term, body.category)
    for alias in body.aliases:
        ensure_hotword_is_not_sensitive(alias, body.category)
    _validate_item_source(
        session,
        ctx,
        source_type=body.source_type,
        source_badcase_id=body.source_badcase_id,
        canonical_term=body.canonical_term,
    )
    normalized = _assert_item_terms_unique(
        session,
        version,
        canonical_term=body.canonical_term,
        aliases=body.aliases,
    )
    item = HotwordVersionItem(
        item_id=body.item_id or _new_id("hwpi"),
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        version_id=version_id,
        canonical_term=body.canonical_term,
        normalized_term=normalized,
        aliases=body.aliases,
        category=body.category,
        weight=body.weight,
        source_badcase_id=body.source_badcase_id,
        source_type=body.source_type,
        resource_version=1,
        root_trace_id=version.root_trace_id,
        current_trace_id=ctx.trace_id,
    )
    session.add(item)
    session.flush()
    _recalculate_content_hash(session, version)
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    data = _item_data(item)
    record_audit(
        session,
        ctx,
        action="hotword_item.create",
        object_type="hotword_version_item",
        object_id=item.item_id,
        after=data,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.item-upserted",
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload={
            "version_id": version.version_id,
            "item_id": item.item_id,
            "root_trace_id": version.root_trace_id,
        },
    )
    return data


def patch_hotword_item(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    item_id: str,
    body: HotwordItemPatchRequest,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_items_mutable(version)
    item = _get_item(session, ctx, version_id, item_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, item.resource_version, "热词项")
    before = _item_data(item)
    values = body.model_dump(exclude={"expected_resource_version"}, exclude_unset=True)
    if "source_badcase_id" in values and "source_type" not in values:
        values["source_type"] = "badcase" if values["source_badcase_id"] else "manual"
    if values.get("source_type") in {"manual", "knowledge_candidate"}:
        values["source_badcase_id"] = None
    canonical_term = str(values.get("canonical_term", item.canonical_term))
    source_type = str(values.get("source_type", item.source_type))
    source_badcase_id = values.get("source_badcase_id", item.source_badcase_id)
    _validate_item_source(
        session,
        ctx,
        source_type=source_type,
        source_badcase_id=(str(source_badcase_id) if isinstance(source_badcase_id, str) else None),
        canonical_term=canonical_term,
    )
    category = str(values.get("category", item.category))
    aliases = values.get("aliases", item.aliases)
    if not isinstance(aliases, list):
        aliases = item.aliases
    ensure_hotword_is_not_sensitive(canonical_term, category)
    for alias in aliases:
        ensure_hotword_is_not_sensitive(str(alias), category)
    normalized = _assert_item_terms_unique(
        session,
        version,
        canonical_term=canonical_term,
        aliases=[str(alias) for alias in aliases],
        exclude_item_id=item_id,
    )
    for field, value in values.items():
        setattr(item, field, value)
    item.normalized_term = normalized
    item.resource_version += 1
    item.current_trace_id = ctx.trace_id
    session.flush()
    _recalculate_content_hash(session, version)
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    data = _item_data(item)
    record_audit(
        session,
        ctx,
        action="hotword_item.update",
        object_type="hotword_version_item",
        object_id=item.item_id,
        before=before,
        after=data,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.item-upserted",
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload={
            "version_id": version.version_id,
            "item_id": item.item_id,
            "root_trace_id": version.root_trace_id,
        },
    )
    return data


def delete_hotword_item(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    item_id: str,
    *,
    expected_resource_version: int,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_items_mutable(version)
    item = _get_item(session, ctx, version_id, item_id, for_update=True)
    _assert_expected_version(expected_resource_version, item.resource_version, "热词项")
    before = _item_data(item)
    session.delete(item)
    session.flush()
    _recalculate_content_hash(session, version)
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    record_audit(
        session,
        ctx,
        action="hotword_item.delete",
        object_type="hotword_version_item",
        object_id=item_id,
        before=before,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.item-deleted",
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload={
            "version_id": version.version_id,
            "item_id": item_id,
            "root_trace_id": version.root_trace_id,
        },
    )
    return {"item_id": item_id, "version_id": version_id, "deleted": True}


def _hotword_item_eval_fingerprint(item: HotwordVersionItem) -> tuple[Any, ...]:
    return (
        item.canonical_term,
        item.normalized_term,
        tuple(sorted(item.aliases)),
        item.category,
        item.weight,
        item.source_badcase_id,
        item.source_type,
    )


def _evaluated_term_bindings(
    session: Session,
    version: HotwordPackVersion,
    version_items: Sequence[HotwordVersionItem],
    baseline: HotwordPackVersion | None,
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """Freeze only candidate repairs while global metrics still cover the fixed dataset."""

    candidate_by_term = {item.normalized_term: item for item in version_items}
    baseline_by_term: dict[str, HotwordVersionItem] = {}
    if baseline is not None:
        baseline_items = session.scalars(
            select(HotwordVersionItem).where(
                HotwordVersionItem.tenant_id == version.tenant_id,
                HotwordVersionItem.project_id == version.project_id,
                HotwordVersionItem.version_id == baseline.version_id,
            )
        ).all()
        baseline_by_term = {item.normalized_term: item for item in baseline_items}

    changes: list[dict[str, str]] = []
    for normalized_term in sorted(set(candidate_by_term) | set(baseline_by_term)):
        candidate_item = candidate_by_term.get(normalized_term)
        baseline_item = baseline_by_term.get(normalized_term)
        if baseline_item is None:
            assert candidate_item is not None
            change_type = "added"
            evaluated_item = candidate_item
        elif candidate_item is None:
            change_type = "removed"
            evaluated_item = baseline_item
        elif _hotword_item_eval_fingerprint(candidate_item) != _hotword_item_eval_fingerprint(
            baseline_item
        ):
            change_type = "modified"
            evaluated_item = candidate_item
        else:
            continue
        changes.append(
            {
                "term_id": evaluated_item.item_id,
                "canonical_term": evaluated_item.canonical_term,
                "normalized_term": normalized_term,
                "change_type": change_type,
            }
        )
    return (
        [change["term_id"] for change in changes],
        [change["canonical_term"] for change in changes],
        changes,
    )


def _verify_eval_run(
    session: Session, ctx: RequestContext, version: HotwordPackVersion, eval_run_id: str
) -> dict[str, Any]:
    # Release authorization must use the owned durable payload, not the public
    # Run projection. The public projection intentionally removes internal
    # artifact hashes and frozen baseline bindings before returning data to a
    # caller; reusing it here would make every valid approval fail closed and
    # would couple authorization semantics to response-shaping policy.
    record = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == eval_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "hotword_eval",
        )
    )
    if record is None:
        raise ApiError("HOTWORD_EVAL_RUN_NOT_FOUND", "热词评测运行不存在", 404)
    if not isinstance(record.payload, dict):
        raise ApiError(
            "HOTWORD_EVAL_GATE_NOT_PASSED",
            "发布要求成功、锁定且通过门禁的同版本 EvalRun",
            409,
        )
    eval_run = {
        **record.payload,
        "run_id": record.run_id,
        "run_type": record.run_type,
        "status": record.status,
        "trace_id": record.trace_id,
    }
    payload = eval_run
    gate = payload.get("gate")
    pack = _get_pack(session, ctx, version.pack_id)
    baseline_version_id = payload.get("baseline_version_id")
    baseline = (
        session.scalar(
            select(HotwordPackVersion).where(
                HotwordPackVersion.version_id == baseline_version_id,
                HotwordPackVersion.tenant_id == ctx.tenant_id,
                HotwordPackVersion.project_id == ctx.project_id,
                HotwordPackVersion.pack_id == version.pack_id,
            )
        )
        if isinstance(baseline_version_id, str) and baseline_version_id
        else None
    )
    baseline_mode = payload.get("baseline_mode")
    baseline_ref = payload.get("baseline_ref")
    uses_no_hotword_baseline = (
        baseline_mode == HOTWORD_BASELINE_MODE_NO_HOTWORD
        and baseline_ref == HOTWORD_BASELINE_REF_NO_HOTWORD
        and version.baseline_version_id is None
        and payload.get("baseline_version_id") is None
        and payload.get("prior_current_version_id") is None
        and pack.current_version_id is None
        and baseline is None
    )
    uses_published_baseline = (
        baseline_mode == HOTWORD_BASELINE_MODE_PUBLISHED_VERSION
        and isinstance(baseline_ref, str)
        and baseline_ref == version.baseline_version_id
        and payload.get("baseline_version_id") == version.baseline_version_id
        and payload.get("prior_current_version_id") == pack.current_version_id
        and payload.get("baseline_version_id") == payload.get("prior_current_version_id")
        and baseline is not None
        and baseline.status == "published"
    )
    if (
        eval_run.get("run_type") != "hotword_eval"
        or eval_run.get("status") != "success"
        or payload.get("hotword_pack_version_id") != version.version_id
        or payload.get("provider") != version.compiled_provider
        or payload.get("provider_artifact_ref") != version.provider_artifact_ref
        or payload.get("artifact_sha256") != (version.payload or {}).get("artifact_sha256")
        or payload.get("content_sha256") != version.content_sha256
        or payload.get("manifest_storage_object_id") != version.manifest_storage_object_id
        or payload.get("baseline_version_id") != version.baseline_version_id
        or not (uses_no_hotword_baseline or uses_published_baseline)
        or payload.get("locked") is not True
        or not isinstance(gate, dict)
        or gate.get("passed") is not True
    ):
        raise ApiError(
            "HOTWORD_EVAL_GATE_NOT_PASSED",
            "发布要求成功、锁定且通过门禁的同版本 EvalRun",
            409,
        )
    result_storage_object_ids = payload.get("result_storage_object_ids")
    result_storage_object_sha256 = payload.get("result_storage_object_sha256")
    if (
        not isinstance(result_storage_object_ids, list)
        or not result_storage_object_ids
        or any(
            not isinstance(value, str) or not value.strip() for value in result_storage_object_ids
        )
        or len(set(result_storage_object_ids)) != len(result_storage_object_ids)
        or not isinstance(result_storage_object_sha256, dict)
        or set(result_storage_object_sha256) != set(result_storage_object_ids)
    ):
        raise ApiError(
            "HOTWORD_EVAL_RESULT_STORAGE_INVALID",
            "EvalRun 缺少非空、去重且冻结哈希的结果对象存储引用",
            409,
        )
    for result_storage_object_id in result_storage_object_ids:
        expected_hash = result_storage_object_sha256.get(result_storage_object_id)
        if not isinstance(expected_hash, str):
            raise ApiError(
                "HOTWORD_EVAL_RESULT_STORAGE_INVALID",
                "EvalRun 结果对象存储引用缺少冻结内容哈希",
                409,
            )
        validate_scoped_storage_object_reference(
            session,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            storage_object_id=result_storage_object_id,
            purpose="热词评测结果复验",
            expected_content_sha256=expected_hash,
        )
    return eval_run


def _request_hotword_build(
    session: Session,
    ctx: RequestContext,
    version: HotwordPackVersion,
    *,
    provider: str | None,
) -> str:
    items = session.scalars(
        select(HotwordVersionItem).where(
            HotwordVersionItem.tenant_id == version.tenant_id,
            HotwordVersionItem.project_id == version.project_id,
            HotwordVersionItem.version_id == version.version_id,
        )
    ).all()
    compiled_provider = validate_provider_compilation(
        provider or version.compiled_provider or "auris-audio-stack",
        item_count=len(items),
        alias_counts=[len(item.aliases) for item in items],
    )
    if not version.content_sha256:
        raise ApiError("HOTWORD_CONTENT_HASH_REQUIRED", "构建前必须固化词包内容哈希", 409)
    run_id = _new_id("hwbuild")
    run_payload = {
        "run_id": run_id,
        "origin_run_id": run_id,
        "status": "pending",
        "hotword_pack_version_id": version.version_id,
        "version_id": version.version_id,
        "pack_id": version.pack_id,
        "version": version.version,
        "content_sha256": version.content_sha256,
        "provider": compiled_provider,
        "target_provider": compiled_provider,
        "item_count": len(items),
        "expected_resource_version": version.resource_version,
        "manifest_storage_object_id": None,
        "job_name": "hotword_pack_compile",
        "root_trace_id": version.root_trace_id,
        "affected_objects": [
            {"type": "hotword_pack", "id": version.pack_id},
            {"type": "hotword_pack_version", "id": version.version_id},
        ],
    }
    session.add(
        RunRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_build",
            status="pending",
            run_key=f"hotword-build:{version.version_id}:{version.content_sha256}",
            partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{version.version_id}",
            trace_id=ctx.trace_id,
            payload=run_payload,
        )
    )
    version.compiled_provider = compiled_provider
    version.manifest_storage_object_id = None
    version.provider_artifact_ref = None
    version.payload = {**(version.payload or {}), "build_run_id": run_id}
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.build-requested",
        aggregate_type="hotword_build",
        aggregate_id=run_id,
        payload=run_payload,
    )
    return run_id


def patch_hotword_version(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    body: HotwordPackVersionPatchRequest,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, version.resource_version, "热词包版本")
    before = _version_data(session, version)
    metadata_change = body.eval_run_id is not None and body.eval_run_id != version.eval_run_id
    if version.status not in VERSION_MUTABLE_STATUSES and metadata_change:
        raise ApiError("HOTWORD_VERSION_IMMUTABLE", "已发布或归档版本不可修改", 409)
    prior_status = version.status
    if body.status is not None:
        if body.status == "published":
            raise ApiError("HOTWORD_PUBLISH_ENDPOINT_REQUIRED", "请使用专用发布接口", 409)
        if body.status == "rolled_back":
            raise ApiError(
                "HOTWORD_ROLLBACK_WORKFLOW_REQUIRED",
                "回滚必须通过带原因、双审批和 RunRecord 的专用受控流程",
                409,
            )
        allowed = STATUS_TRANSITIONS.get(version.status, frozenset())
        if body.status not in allowed:
            raise ApiError(
                "HOTWORD_VERSION_TRANSITION_INVALID",
                f"不允许从 {version.status} 转移到 {body.status}",
                409,
            )
        if prior_status == "published" and body.status == "deprecated":
            pack = _get_pack(session, ctx, version.pack_id, for_update=True)
            if pack.current_version_id == version.version_id:
                raise ApiError(
                    "HOTWORD_CURRENT_VERSION_DEPRECATION_FORBIDDEN",
                    "逻辑词包当前生产版本不能通过通用 PATCH 废弃；请先发布替代版本或执行受控回滚",
                    409,
                    details=[
                        {
                            "pack_id": pack.pack_id,
                            "current_version_id": pack.current_version_id,
                        }
                    ],
                )
        if body.status == "ready_for_eval":
            if prior_status == "validating":
                raise ApiError(
                    "HOTWORD_BUILD_COMPLETION_REQUIRED",
                    "validating 版本只能由受信构建完成回执进入 ready_for_eval",
                    409,
                )
            _assert_no_knowledge_candidates(session, version)
            effective_manifest = version.manifest_storage_object_id
            if not effective_manifest:
                raise ApiError(
                    "HOTWORD_MANIFEST_REQUIRED",
                    "进入评测前必须固化对象存储 manifest",
                    409,
                )
            if not version.content_sha256:
                _recalculate_content_hash(session, version)
        if body.status == "approved":
            if "model_engineer" not in ctx.roles and "system" not in ctx.roles:
                raise ApiError("FORBIDDEN", "仅模型负责人可以批准热词版本", 403)
            eval_run_id = body.eval_run_id or version.eval_run_id
            if not eval_run_id:
                raise ApiError("HOTWORD_EVAL_RUN_REQUIRED", "批准前必须绑定 EvalRun", 409)
            _verify_eval_run(session, ctx, version, eval_run_id)
            version.eval_run_id = eval_run_id
            version.eval_locked = True
            version.model_approved_by = ctx.user_id
        version.status = body.status
    if body.eval_run_id is not None:
        version.eval_run_id = body.eval_run_id
    if version.status == "validating":
        _recalculate_content_hash(session, version)
        if prior_status != "validating":
            _request_hotword_build(session, ctx, version, provider=body.provider)
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    session.flush()
    data = _version_data(session, version, include_items=True)
    record_audit(
        session,
        ctx,
        action="hotword_version.update",
        object_type="hotword_pack_version",
        object_id=version.version_id,
        before=before,
        after=data,
        trace_id=version.root_trace_id,
    )
    event_type = (
        "hotword_pack_version.rolled-back"
        if version.status == "rolled_back" and prior_status != "rolled_back"
        else "hotword_pack_version.updated"
    )
    if event_type == "hotword_pack_version.rolled-back":
        pack = _get_pack(session, ctx, version.pack_id)
        if pack.current_version_id == version.version_id:
            pack.current_version_id = version.baseline_version_id
            pack.resource_version += 1
            pack.current_trace_id = ctx.trace_id
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload={
            "version_id": version.version_id,
            "status": version.status,
            "root_trace_id": version.root_trace_id,
        },
    )
    return data


def materialize_hotword_build_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "hotword_build":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError(
            "HOTWORD_BUILD_RESULT_REQUIRED",
            "热词构建完成回执必须包含 result_ref",
            422,
        )
    version_id = str(record.payload.get("hotword_pack_version_id") or "")
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    source_run_id = record.payload.get("retry_of_run_id")
    origin_run_id = record.payload.get("origin_run_id")
    current_build_run_id = (version.payload or {}).get("build_run_id")
    if current_build_run_id not in {record.run_id, source_run_id, origin_run_id}:
        raise ApiError("HOTWORD_BUILD_RUN_MISMATCH", "构建回执与版本当前构建运行不一致", 409)
    for field, expected in (
        ("hotword_pack_version_id", version_id),
        ("content_sha256", record.payload.get("content_sha256")),
        ("provider", record.payload.get("provider")),
    ):
        if result_ref.get(field) != expected:
            raise ApiError(
                "HOTWORD_BUILD_BINDING_MISMATCH",
                "构建回执未绑定发起时冻结的版本、内容哈希或 provider",
                409,
                details=[{"field": field, "expected": expected, "actual": result_ref.get(field)}],
            )
    if (
        version.status != "validating"
        or version.content_sha256 != record.payload.get("content_sha256")
        or version.compiled_provider != record.payload.get("provider")
    ):
        raise ApiError(
            "HOTWORD_BUILD_VERSION_CHANGED",
            "热词版本在构建期间已变化，拒绝固化产物",
            409,
        )
    manifest_storage_object_id = result_ref.get("manifest_storage_object_id")
    provider_artifact_ref = result_ref.get("provider_artifact_ref")
    artifact_sha256 = result_ref.get("artifact_sha256")
    if not isinstance(manifest_storage_object_id, str) or not manifest_storage_object_id.strip():
        raise ApiError("HOTWORD_MANIFEST_REQUIRED", "构建回执缺少 manifest 对象存储引用", 422)
    if not isinstance(provider_artifact_ref, str) or not provider_artifact_ref.strip():
        raise ApiError("HOTWORD_PROVIDER_ARTIFACT_REQUIRED", "构建回执缺少 provider 编译产物", 422)
    if not isinstance(artifact_sha256, str) or not re.fullmatch(
        r"[0-9a-fA-F]{64}", artifact_sha256
    ):
        raise ApiError("HOTWORD_ARTIFACT_HASH_REQUIRED", "构建回执缺少有效产物 SHA-256", 422)
    validate_scoped_storage_object_reference(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        storage_object_id=manifest_storage_object_id,
        purpose="热词 manifest",
        expected_content_sha256=str(record.payload.get("content_sha256") or ""),
    )
    validate_scoped_storage_object_reference(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        storage_object_id=provider_artifact_ref,
        purpose="Provider 热词编译产物",
        expected_content_sha256=artifact_sha256,
    )
    items = session.scalars(
        select(HotwordVersionItem).where(
            HotwordVersionItem.tenant_id == ctx.tenant_id,
            HotwordVersionItem.project_id == ctx.project_id,
            HotwordVersionItem.version_id == version_id,
        )
    ).all()
    _assert_no_knowledge_candidates(session, version)
    validate_provider_compilation(
        str(record.payload.get("provider")),
        item_count=len(items),
        alias_counts=[len(item.aliases) for item in items],
    )
    before = _version_data(session, version)
    version.manifest_storage_object_id = manifest_storage_object_id
    version.provider_artifact_ref = provider_artifact_ref
    version.payload = {
        **(version.payload or {}),
        "artifact_sha256": artifact_sha256.lower(),
    }
    version.status = "ready_for_eval"
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    completion = {
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "content_sha256": version.content_sha256,
        "provider": version.compiled_provider,
        "compiled_provider": version.compiled_provider,
        "manifest_storage_object_id": manifest_storage_object_id,
        "provider_artifact_ref": provider_artifact_ref,
        "artifact_sha256": artifact_sha256.lower(),
        "version_status": version.status,
        "root_trace_id": version.root_trace_id,
    }
    record.payload = {**record.payload, **completion}
    session.flush()
    record_audit(
        session,
        ctx,
        action="hotword_version.build_completed",
        object_type="hotword_pack_version",
        object_id=version_id,
        before=before,
        after=_version_data(session, version),
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.built",
        aggregate_type="hotword_pack_version",
        aggregate_id=version_id,
        payload={"run_id": record.run_id, **completion},
    )
    return completion


def create_hotword_eval_run(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    body: HotwordEvalRunRequest,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, version.resource_version, "热词包版本")
    if version.status != "ready_for_eval":
        raise ApiError(
            "HOTWORD_VERSION_NOT_READY_FOR_EVAL",
            "仅 ready_for_eval 版本可以发起影子评测",
            409,
        )
    dataset_snapshot = locked_eval_dataset_snapshot(
        session,
        ctx,
        body.eval_dataset_id,
        required_capability="asr_hotword",
    )
    _assert_no_knowledge_candidates(session, version)
    version_items = session.scalars(
        select(HotwordVersionItem).where(
            HotwordVersionItem.tenant_id == ctx.tenant_id,
            HotwordVersionItem.project_id == ctx.project_id,
            HotwordVersionItem.version_id == version_id,
        )
    ).all()
    compiled_provider = validate_provider_compilation(
        body.provider,
        item_count=len(version_items),
        alias_counts=[len(item.aliases) for item in version_items],
    )
    if compiled_provider != version.compiled_provider:
        raise ApiError(
            "HOTWORD_PROVIDER_MISMATCH",
            "评测 provider 必须与受信构建产物 provider 一致",
            409,
        )
    if not version.content_sha256:
        raise ApiError("HOTWORD_CONTENT_HASH_REQUIRED", "评测前必须固化词包内容哈希", 409)
    if not version.manifest_storage_object_id:
        raise ApiError("HOTWORD_MANIFEST_REQUIRED", "评测前必须固化对象存储 manifest", 409)
    if not version.provider_artifact_ref or not (version.payload or {}).get("artifact_sha256"):
        raise ApiError(
            "HOTWORD_PROVIDER_ARTIFACT_REQUIRED",
            "评测前必须固化带 SHA-256 的 provider 编译产物",
            409,
        )
    pack = _get_pack(session, ctx, version.pack_id, for_update=True)
    baseline: HotwordPackVersion | None = None
    if pack.current_version_id is None:
        if version.baseline_version_id is not None:
            raise ApiError(
                "HOTWORD_EVAL_BOOTSTRAP_BASELINE_INVALID",
                "首个词包版本只能使用显式无热词基线",
                409,
            )
        baseline_mode = HOTWORD_BASELINE_MODE_NO_HOTWORD
        baseline_ref = HOTWORD_BASELINE_REF_NO_HOTWORD
    else:
        if version.baseline_version_id != pack.current_version_id:
            raise ApiError(
                "HOTWORD_EVAL_BASELINE_NOT_CURRENT",
                "影子评测基线必须是逻辑词包当前已发布版本",
                409,
                details=[
                    {
                        "baseline_version_id": version.baseline_version_id,
                        "current_version_id": pack.current_version_id,
                    }
                ],
            )
        baseline = get_hotword_version(session, ctx, pack.current_version_id)
        if baseline.pack_id != pack.pack_id or baseline.status != "published":
            raise ApiError(
                "HOTWORD_EVAL_BASELINE_NOT_PUBLISHED",
                "影子评测基线必须是当前词包的已发布版本",
                409,
            )
        baseline_mode = HOTWORD_BASELINE_MODE_PUBLISHED_VERSION
        baseline_ref = baseline.version_id
    evaluated_term_ids, evaluated_terms, evaluated_term_changes = _evaluated_term_bindings(
        session,
        version,
        version_items,
        baseline,
    )
    run_id = _new_id("hweval")
    run_payload = {
        "run_id": run_id,
        "origin_run_id": run_id,
        "status": "pending",
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "baseline_version_id": version.baseline_version_id,
        "baseline_mode": baseline_mode,
        "baseline_ref": baseline_ref,
        "prior_current_version_id": pack.current_version_id,
        "eval_dataset_id": body.eval_dataset_id,
        "eval_dataset_version": dataset_snapshot["dataset_version"],
        "eval_dataset_manifest_storage_object_id": dataset_snapshot["manifest_storage_object_id"],
        "eval_dataset_manifest_sha256": dataset_snapshot["manifest_sha256"],
        "eval_dataset_snapshot_sha256": dataset_snapshot["snapshot_sha256"],
        "eval_dataset_sample_count": dataset_snapshot["sample_count"],
        "execution_mode": "shadow",
        "provider": compiled_provider,
        "compiled_provider": compiled_provider,
        "provider_artifact_ref": version.provider_artifact_ref,
        "artifact_sha256": (version.payload or {}).get("artifact_sha256"),
        "content_sha256": version.content_sha256,
        "manifest_storage_object_id": version.manifest_storage_object_id,
        "unique_terms": len(version_items),
        "evaluated_term_ids": evaluated_term_ids,
        "evaluated_terms": evaluated_terms,
        "evaluated_term_changes": evaluated_term_changes,
        "job_name": "hotword_pack_shadow_eval",
        "root_trace_id": version.root_trace_id,
        "affected_objects": [
            {"type": "hotword_pack_version", "id": version_id},
            {
                "type": "eval_dataset_version",
                "id": body.eval_dataset_id,
                "version": dataset_snapshot["dataset_version"],
            },
        ],
        "next_actions": [
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": f"traces/{version.root_trace_id}",
            },
            {"key": "wait_completion", "label": "等待影子评测完成"},
        ],
    }
    run = RunRecord(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type="hotword_eval",
        status="pending",
        run_key=(
            f"hotword-eval:{version_id}:{body.eval_dataset_id}:"
            f"{dataset_snapshot['snapshot_sha256']}"
        ),
        partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{version_id}",
        trace_id=ctx.trace_id,
        payload=run_payload,
    )
    session.add(run)
    version.status = "evaluating"
    version.eval_run_id = run_id
    version.eval_locked = False
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    session.flush()
    response = {
        "id": run_id,
        "run_id": run_id,
        "run_type": "hotword_eval",
        "status": "pending",
        "hotword_pack_version_id": version_id,
        "execution_mode": "shadow",
        "baseline_mode": baseline_mode,
        "baseline_ref": baseline_ref,
        "evaluated_term_ids": evaluated_term_ids,
        "evaluated_terms": evaluated_terms,
        "provider": compiled_provider,
        "locked": False,
        "gate": None,
        "root_trace_id": version.root_trace_id,
        "version_status": "evaluating",
    }
    record_audit(
        session,
        ctx,
        action="hotword_version.eval",
        object_type="hotword_pack_version",
        object_id=version_id,
        after=response,
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.eval-requested",
        aggregate_type="hotword_eval",
        aggregate_id=run_id,
        payload=run_payload,
    )
    return response


def _validated_eval_metrics(raw: Any, *, field: str) -> dict[str, float | int | None]:
    if not isinstance(raw, dict):
        raise ApiError(
            "HOTWORD_EVAL_METRICS_REQUIRED",
            f"评测完成回执必须包含 {field}",
            422,
        )
    try:
        return HotwordEvalMetrics.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ApiError(
            "HOTWORD_EVAL_METRICS_INVALID",
            f"评测完成回执的 {field} 无效",
            422,
            details=[
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "message": str(error["msg"]),
                    "code": str(error["type"]),
                }
                for error in exc.errors()
            ],
        ) from exc


def _per_term_occurrences_gate_blocked(
    raw: Any,
    *,
    unique_terms: int,
    expected_terms: set[str] | None = None,
    expected_total: int | None = None,
) -> bool:
    if raw is None:
        return True
    if not isinstance(raw, dict) or any(
        not isinstance(term, str)
        or not term.strip()
        or isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for term, value in raw.items()
    ):
        raise ApiError(
            "HOTWORD_EVAL_PER_TERM_COUNTS_INVALID",
            "per_term_trusted_occurrences 必须是规范词到非负整数的映射",
            422,
        )
    if len(raw) != unique_terms or any(value < 3 for value in raw.values()):
        return True
    if expected_terms is not None and set(raw) != expected_terms:
        return True
    return expected_total is not None and sum(raw.values()) != expected_total


def materialize_hotword_eval_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "hotword_eval":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError(
            "HOTWORD_EVAL_RESULT_REQUIRED",
            "热词评测完成回执必须包含 result_ref",
            422,
        )
    version_id = str(record.payload.get("hotword_pack_version_id") or "")
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    source_eval_run_id = record.payload.get("retry_of_run_id")
    origin_eval_run_id = record.payload.get("origin_run_id")
    if version.eval_run_id not in {
        record.run_id,
        source_eval_run_id,
        origin_eval_run_id,
    }:
        raise ApiError(
            "HOTWORD_EVAL_RUN_MISMATCH",
            "完成回执与热词版本当前 EvalRun 不一致",
            409,
        )
    expected_bindings = {
        "hotword_pack_version_id": version_id,
        "baseline_version_id": record.payload.get("baseline_version_id"),
        "baseline_mode": record.payload.get("baseline_mode"),
        "baseline_ref": record.payload.get("baseline_ref"),
        "eval_dataset_id": record.payload.get("eval_dataset_id"),
        "content_sha256": record.payload.get("content_sha256"),
        "manifest_storage_object_id": record.payload.get("manifest_storage_object_id"),
        "provider": record.payload.get("provider"),
        "provider_artifact_ref": record.payload.get("provider_artifact_ref"),
        "artifact_sha256": record.payload.get("artifact_sha256"),
        "evaluated_term_ids": record.payload.get("evaluated_term_ids"),
        "evaluated_terms": record.payload.get("evaluated_terms"),
    }
    current_dataset_snapshot = locked_eval_dataset_snapshot(
        session,
        ctx,
        str(expected_bindings["eval_dataset_id"] or ""),
        required_capability="asr_hotword",
    )
    expected_dataset_bindings = {
        "eval_dataset_version": record.payload.get("eval_dataset_version"),
        "eval_dataset_manifest_storage_object_id": record.payload.get(
            "eval_dataset_manifest_storage_object_id"
        ),
        "eval_dataset_manifest_sha256": record.payload.get("eval_dataset_manifest_sha256"),
        "eval_dataset_snapshot_sha256": record.payload.get("eval_dataset_snapshot_sha256"),
        "eval_dataset_sample_count": record.payload.get("eval_dataset_sample_count"),
    }
    actual_dataset_bindings = {
        "eval_dataset_version": current_dataset_snapshot["dataset_version"],
        "eval_dataset_manifest_storage_object_id": current_dataset_snapshot[
            "manifest_storage_object_id"
        ],
        "eval_dataset_manifest_sha256": current_dataset_snapshot["manifest_sha256"],
        "eval_dataset_snapshot_sha256": current_dataset_snapshot["snapshot_sha256"],
        "eval_dataset_sample_count": current_dataset_snapshot["sample_count"],
    }
    binding_errors = [
        {
            "field": field,
            "expected": expected,
            "actual": result_ref.get(field),
        }
        for field, expected in expected_bindings.items()
        if result_ref.get(field) != expected
    ]
    for required_binding in (
        "baseline_version_id",
        "baseline_mode",
        "baseline_ref",
        "evaluated_term_ids",
        "evaluated_terms",
    ):
        if required_binding not in result_ref:
            binding_errors.append(
                {
                    "field": required_binding,
                    "expected": expected_bindings[required_binding],
                    "actual": "<missing>",
                }
            )
    pack = _get_pack(session, ctx, version.pack_id)
    current_binding_errors = [
        field
        for field, expected, actual in (
            ("content_sha256", expected_bindings["content_sha256"], version.content_sha256),
            (
                "manifest_storage_object_id",
                expected_bindings["manifest_storage_object_id"],
                version.manifest_storage_object_id,
            ),
            (
                "provider_artifact_ref",
                expected_bindings["provider_artifact_ref"],
                version.provider_artifact_ref,
            ),
            (
                "artifact_sha256",
                expected_bindings["artifact_sha256"],
                (version.payload or {}).get("artifact_sha256"),
            ),
            ("provider", expected_bindings["provider"], version.compiled_provider),
            (
                "baseline_version_id",
                expected_bindings["baseline_version_id"],
                version.baseline_version_id,
            ),
            (
                "prior_current_version_id",
                record.payload.get("prior_current_version_id"),
                pack.current_version_id,
            ),
        )
        if expected != actual
    ]
    current_binding_errors.extend(
        field
        for field, expected in expected_dataset_bindings.items()
        if expected != actual_dataset_bindings[field]
    )
    if binding_errors or current_binding_errors:
        raise ApiError(
            "HOTWORD_EVAL_BINDING_MISMATCH",
            "评测完成回执未绑定发起时冻结的版本、数据集或编译产物",
            409,
            details=[
                *binding_errors,
                *(
                    {"field": field, "reason": "version_changed"}
                    for field in current_binding_errors
                ),
            ],
        )
    baseline = _validated_eval_metrics(result_ref.get("baseline_metrics"), field="baseline_metrics")
    candidate = _validated_eval_metrics(
        result_ref.get("candidate_metrics"), field="candidate_metrics"
    )
    version_items = session.scalars(
        select(HotwordVersionItem).where(
            HotwordVersionItem.tenant_id == ctx.tenant_id,
            HotwordVersionItem.project_id == ctx.project_id,
            HotwordVersionItem.version_id == version_id,
        )
    ).all()
    _assert_no_knowledge_candidates(session, version)
    validate_provider_compilation(
        str(expected_bindings["provider"]),
        item_count=len(version_items),
        alias_counts=[len(item.aliases) for item in version_items],
    )
    evaluated_unique_terms = candidate.get("unique_terms")
    if evaluated_unique_terms != len(version_items):
        raise ApiError(
            "HOTWORD_EVAL_UNIQUE_TERMS_INVALID",
            "评测词数必须等于热词包版本实际词项数",
            422,
            details=[
                {
                    "evaluated_unique_terms": evaluated_unique_terms,
                    "version_item_count": len(version_items),
                }
            ],
        )
    gate = evaluate_release_gate(baseline=baseline, candidate=candidate)
    raw_per_term = result_ref.get("per_term_trusted_occurrences")
    evaluated_terms = record.payload.get("evaluated_terms")
    if not isinstance(evaluated_terms, list) or any(
        not isinstance(term, str) or not term.strip() for term in evaluated_terms
    ):
        raise ApiError(
            "HOTWORD_EVAL_REPAIR_SET_INVALID",
            "评测运行缺少冻结的候选修复词集合",
            409,
        )
    if _per_term_occurrences_gate_blocked(
        raw_per_term,
        unique_terms=len(evaluated_terms),
        expected_terms=set(evaluated_terms),
    ):
        blocked_reasons = list(gate["blocked_reasons"])
        if "minimum_per_term_occurrences" not in blocked_reasons:
            blocked_reasons.append("minimum_per_term_occurrences")
        gate = {
            **gate,
            "passed": False,
            "blocked_reasons": blocked_reasons,
        }
    locked = result_ref.get("locked") is True
    if not locked:
        gate = {
            **gate,
            "passed": False,
            "blocked_reasons": [*gate["blocked_reasons"], "eval_result_not_locked"],
        }
    result_storage_object_ids = result_ref.get("result_storage_object_ids")
    if (
        not isinstance(result_storage_object_ids, list)
        or not result_storage_object_ids
        or any(
            not isinstance(value, str) or not value.strip() for value in result_storage_object_ids
        )
        or len(set(result_storage_object_ids)) != len(result_storage_object_ids)
    ):
        raise ApiError(
            "HOTWORD_EVAL_RESULT_STORAGE_REQUIRED",
            "评测完成回执必须提供结果对象存储引用",
            422,
        )
    result_storage_object_sha256: dict[str, str] = {}
    for result_storage_object_id in result_storage_object_ids:
        storage_object = validate_scoped_storage_object_reference(
            session,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            storage_object_id=result_storage_object_id,
            purpose="热词评测结果",
        )
        result_storage_object_sha256[result_storage_object_id] = str(
            storage_object.content_sha256
        ).lower()
    before = _version_data(session, version)
    version.status = "review_required" if gate["passed"] else "gate_blocked"
    version.eval_run_id = record.run_id
    version.eval_locked = locked
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    completion = {
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "eval_dataset_id": expected_bindings["eval_dataset_id"],
        **expected_dataset_bindings,
        "baseline_version_id": expected_bindings["baseline_version_id"],
        "baseline_mode": expected_bindings["baseline_mode"],
        "baseline_ref": expected_bindings["baseline_ref"],
        "evaluated_term_ids": expected_bindings["evaluated_term_ids"],
        "evaluated_terms": expected_bindings["evaluated_terms"],
        "evaluated_term_changes": record.payload.get("evaluated_term_changes"),
        "content_sha256": expected_bindings["content_sha256"],
        "manifest_storage_object_id": expected_bindings["manifest_storage_object_id"],
        "provider": expected_bindings["provider"],
        "compiled_provider": expected_bindings["provider"],
        "provider_artifact_ref": expected_bindings["provider_artifact_ref"],
        "baseline_metrics": baseline,
        "candidate_metrics": candidate,
        "locked": locked,
        "gate": gate,
        "result_storage_object_ids": result_storage_object_ids,
        "result_storage_object_sha256": result_storage_object_sha256,
        "version_status": version.status,
        "root_trace_id": version.root_trace_id,
    }
    record.payload = {**record.payload, **completion}
    session.flush()
    record_audit(
        session,
        ctx,
        action="hotword_version.eval_completed",
        object_type="hotword_pack_version",
        object_id=version_id,
        before=before,
        after=_version_data(session, version),
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.eval-completed",
        aggregate_type="hotword_pack_version",
        aggregate_id=version_id,
        payload={"run_id": record.run_id, **completion},
    )
    return completion


def publish_hotword_version(
    session: Session,
    ctx: RequestContext,
    version_id: str,
    body: HotwordPublishRequest,
) -> dict[str, Any]:
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, version.resource_version, "热词包版本")
    if version.status != "approved":
        raise ApiError("HOTWORD_VERSION_NOT_APPROVED", "仅已批准版本可以发布", 409)
    active_publish_run_id = (version.payload or {}).get("publish_run_id")
    if isinstance(active_publish_run_id, str) and active_publish_run_id:
        active_publish_run = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == active_publish_run_id,
                RunRecord.tenant_id == ctx.tenant_id,
                RunRecord.project_id == ctx.project_id,
                RunRecord.run_type == "hotword_publish",
            )
        )
        if active_publish_run is not None and active_publish_run.status in {
            "pending",
            "running",
            "submitted",
            "blocked",
        }:
            raise ApiError(
                "HOTWORD_PUBLISH_ALREADY_PENDING",
                "该热词版本已有进行中的发布运行",
                409,
                details=[
                    {
                        "run_id": active_publish_run.run_id,
                        "status": active_publish_run.status,
                    }
                ],
            )
    if version.eval_run_id != body.eval_run_id:
        raise ApiError("HOTWORD_EVAL_RUN_MISMATCH", "发布 EvalRun 与批准记录不一致", 409)
    _verify_eval_run(session, ctx, version, body.eval_run_id)
    _assert_no_knowledge_candidates(session, version)
    if not version.model_approved_by:
        raise ApiError("HOTWORD_MODEL_APPROVAL_REQUIRED", "缺少模型负责人审批", 409)
    if version.model_approved_by == ctx.user_id:
        raise ApiError(
            "HOTWORD_APPROVAL_SEPARATION_REQUIRED",
            "模型负责人审批与项目管理员发布确认必须由不同人员完成",
            409,
        )
    if not version.provider_artifact_ref:
        raise ApiError("HOTWORD_PROVIDER_ARTIFACT_REQUIRED", "缺少 provider 编译产物", 409)
    if not version.compiled_provider:
        raise ApiError("HOTWORD_COMPILED_PROVIDER_REQUIRED", "缺少 provider 编译目标", 409)
    artifact_sha256 = (version.payload or {}).get("artifact_sha256")
    if not isinstance(artifact_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise ApiError("HOTWORD_ARTIFACT_HASH_REQUIRED", "缺少 provider 编译产物哈希", 409)
    pack = _get_pack(session, ctx, version.pack_id, for_update=True)
    publish_run_id = _new_id("hwpublish")
    run_payload = {
        "run_id": publish_run_id,
        "origin_run_id": publish_run_id,
        "status": "pending",
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "pack_id": pack.pack_id,
        "prior_current_version_id": pack.current_version_id,
        "eval_run_id": body.eval_run_id,
        "expected_resource_version": body.expected_resource_version,
        "content_sha256": version.content_sha256,
        "manifest_storage_object_id": version.manifest_storage_object_id,
        "provider": version.compiled_provider,
        "compiled_provider": version.compiled_provider,
        "provider_artifact_ref": version.provider_artifact_ref,
        "artifact_sha256": artifact_sha256,
        "model_approved_by": version.model_approved_by,
        "project_admin_confirmed_by": ctx.user_id,
        "confirmation": body.confirmation,
        "job_name": "hotword_pack_publish",
        "root_trace_id": version.root_trace_id,
        "affected_objects": [
            {"type": "hotword_pack", "id": pack.pack_id},
            {"type": "hotword_pack_version", "id": version_id},
            {"type": "eval_run", "id": body.eval_run_id},
        ],
    }
    session.add(
        RunRecord(
            run_id=publish_run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="hotword_publish",
            status="pending",
            run_key=f"hotword-publish:{version_id}:{body.eval_run_id}",
            partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{version_id}",
            trace_id=ctx.trace_id,
            payload=run_payload,
        )
    )
    version.payload = {**(version.payload or {}), "publish_run_id": publish_run_id}
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    session.flush()
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.publish-requested",
        aggregate_type="hotword_publish",
        aggregate_id=publish_run_id,
        payload=run_payload,
    )
    response = {
        "id": publish_run_id,
        "run_id": publish_run_id,
        "run_type": "hotword_publish",
        "status": "pending",
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "eval_run_id": body.eval_run_id,
        "version_status": version.status,
        "resource_version": version.resource_version,
        "root_trace_id": version.root_trace_id,
    }
    record_audit(
        session,
        ctx,
        action="hotword_version.publish_requested",
        object_type="hotword_pack_version",
        object_id=version_id,
        after=response,
        trace_id=version.root_trace_id,
    )
    return response


def materialize_hotword_publish_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "hotword_publish":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError(
            "HOTWORD_PUBLISH_RESULT_REQUIRED",
            "热词发布完成回执必须包含 result_ref",
            422,
        )
    version_id = str(record.payload.get("hotword_pack_version_id") or "")
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    source_publish_run_id = record.payload.get("retry_of_run_id")
    origin_publish_run_id = record.payload.get("origin_run_id")
    current_publish_run_id = (version.payload or {}).get("publish_run_id")
    if current_publish_run_id not in {
        record.run_id,
        source_publish_run_id,
        origin_publish_run_id,
    }:
        raise ApiError(
            "HOTWORD_PUBLISH_RUN_MISMATCH",
            "完成回执与热词版本当前发布运行不一致",
            409,
        )
    expected_bindings = {
        "version_id": version_id,
        "pack_id": record.payload.get("pack_id"),
        "eval_run_id": record.payload.get("eval_run_id"),
        "content_sha256": record.payload.get("content_sha256"),
        "manifest_storage_object_id": record.payload.get("manifest_storage_object_id"),
        "compiled_provider": record.payload.get("compiled_provider"),
        "provider_artifact_ref": record.payload.get("provider_artifact_ref"),
        "artifact_sha256": record.payload.get("artifact_sha256"),
    }
    binding_errors = [
        {"field": field, "expected": expected, "actual": result_ref.get(field)}
        for field, expected in expected_bindings.items()
        if result_ref.get(field) != expected
    ]
    current_binding_errors = [
        field
        for field, expected, actual in (
            ("pack_id", expected_bindings["pack_id"], version.pack_id),
            ("eval_run_id", expected_bindings["eval_run_id"], version.eval_run_id),
            ("content_sha256", expected_bindings["content_sha256"], version.content_sha256),
            (
                "manifest_storage_object_id",
                expected_bindings["manifest_storage_object_id"],
                version.manifest_storage_object_id,
            ),
            (
                "compiled_provider",
                expected_bindings["compiled_provider"],
                version.compiled_provider,
            ),
            (
                "provider_artifact_ref",
                expected_bindings["provider_artifact_ref"],
                version.provider_artifact_ref,
            ),
            (
                "artifact_sha256",
                expected_bindings["artifact_sha256"],
                (version.payload or {}).get("artifact_sha256"),
            ),
            (
                "model_approved_by",
                record.payload.get("model_approved_by"),
                version.model_approved_by,
            ),
        )
        if expected != actual
    ]
    if binding_errors or current_binding_errors:
        raise ApiError(
            "HOTWORD_PUBLISH_BINDING_MISMATCH",
            "发布完成回执未绑定发起时冻结的版本、评测或编译产物",
            409,
            details=[
                *binding_errors,
                *(
                    {"field": field, "reason": "version_changed"}
                    for field in current_binding_errors
                ),
            ],
        )
    if version.status != "approved":
        raise ApiError("HOTWORD_VERSION_NOT_APPROVED", "仅已批准版本可以完成发布", 409)
    eval_run_id = str(record.payload.get("eval_run_id") or "")
    _verify_eval_run(session, ctx, version, eval_run_id)
    _assert_no_knowledge_candidates(session, version)
    model_approved_by = str(record.payload.get("model_approved_by") or "")
    project_admin_confirmed_by = str(record.payload.get("project_admin_confirmed_by") or "")
    if not model_approved_by:
        raise ApiError("HOTWORD_MODEL_APPROVAL_REQUIRED", "缺少模型负责人审批", 409)
    if not project_admin_confirmed_by:
        raise ApiError("HOTWORD_ADMIN_CONFIRMATION_REQUIRED", "缺少项目管理员确认", 409)
    if model_approved_by == project_admin_confirmed_by:
        raise ApiError(
            "HOTWORD_APPROVAL_SEPARATION_REQUIRED",
            "模型负责人审批与项目管理员发布确认必须由不同人员完成",
            409,
        )
    pack = _get_pack(session, ctx, version.pack_id, for_update=True)
    from app.services.scene_profile_service import get_active_scene_binding

    scene_binding = get_active_scene_binding(session, ctx)
    task_type_id, task_type_binding = _hotword_task_type_binding(
        session, ctx, version, pack, scene_binding
    )
    task_model_version = str(task_type_binding.get("model_version") or "").strip() or None
    prior_current_version_id = record.payload.get("prior_current_version_id")
    if pack.current_version_id not in {prior_current_version_id, version_id}:
        raise ApiError(
            "HOTWORD_PACK_CURRENT_VERSION_CHANGED",
            "逻辑词包当前版本已在发布期间变化，请重新评测并确认",
            409,
            details=[
                {
                    "expected_current_version_id": prior_current_version_id,
                    "actual_current_version_id": pack.current_version_id,
                }
            ],
        )
    before = _version_data(session, version)
    task_version_id = f"task_hotword_{version_id}_{record.run_id[-12:]}"
    task_data = {
        "id": task_version_id,
        "task_version_id": task_version_id,
        "task_type_id": task_type_id,
        "task_type_binding": task_type_binding,
        "status": "draft",
        "scene_profile_id": scene_binding["scene_profile_id"],
        "scene_profile_version_id": scene_binding["scene_profile_version_id"],
        "scene_profile_snapshot_sha256": scene_binding["manifest_sha256"],
        "execution_mode": "production",
        "provider": version.compiled_provider,
        **({"model_version": task_model_version} if task_model_version else {}),
        "hotword_pack_version_id": version_id,
        "language": pack.language,
        "audio_intelligence": {
            "execution_mode": "production",
            "provider": version.compiled_provider,
            **({"model_version": task_model_version} if task_model_version else {}),
            "hotword_pack_version_id": version_id,
            "language": pack.language,
        },
        "source": "hotword_pack_publish",
        "source_publish_run_id": record.run_id,
        "source_hotword_pack_version_id": version_id,
        "root_trace_id": version.root_trace_id,
        "trace_id": version.root_trace_id,
        "affected_objects": [
            {"type": "hotword_pack", "id": pack.pack_id},
            {"type": "hotword_pack_version", "id": version_id},
            {"type": "eval_run", "id": eval_run_id},
            {"type": "scene_profile_version", "id": scene_binding["scene_profile_version_id"]},
            {"type": "task_type", "id": task_type_id},
        ],
    }
    existing_task = session.scalar(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
            JsonResource.resource_key == task_version_id,
        )
        .with_for_update()
    )
    if existing_task is not None:
        existing_data = existing_task.data if isinstance(existing_task.data, dict) else {}
        if existing_task.status != "draft" or existing_data != task_data:
            raise ApiError(
                "HOTWORD_TASK_VERSION_ID_CONFLICT",
                "发布生成的 TaskVersion ID 已被非 draft 或非完整同源对象占用",
                409,
                details=[
                    {
                        "task_version_id": task_version_id,
                        "existing_status": existing_task.status,
                        "canonical_match": existing_data == task_data,
                    }
                ],
            )
    else:
        upsert_resource(
            session,
            ctx,
            "task_versions",
            task_version_id,
            task_data,
            status="draft",
            trace_id=version.root_trace_id,
            audit_action="task_versions.create_from_hotword",
        )
    now = datetime.now(UTC)
    version.status = "published"
    version.project_admin_confirmed_by = project_admin_confirmed_by
    version.published_at = now
    version.payload = {
        **(version.payload or {}),
        "publish_run_id": record.run_id,
        "task_version_id": task_version_id,
        "task_type_id": task_type_id,
        "task_type_binding": task_type_binding,
        "task_model_version": task_model_version,
        "production_active": False,
        "production_task_version_id": None,
    }
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    pack.current_version_id = version_id
    pack.resource_version += 1
    pack.current_trace_id = ctx.trace_id
    session.flush()
    completion = {
        "run_id": record.run_id,
        "pack_id": pack.pack_id,
        "hotword_pack_version_id": version_id,
        "version_id": version_id,
        "eval_run_id": eval_run_id,
        "project_admin_confirmed_by": project_admin_confirmed_by,
        "task_version_id": task_version_id,
        "task_type_id": task_type_id,
        "task_type_binding": task_type_binding,
        "model_version": task_model_version,
        "content_sha256": version.content_sha256,
        "compiled_provider": version.compiled_provider,
        "provider_artifact_ref": version.provider_artifact_ref,
        "version_status": version.status,
        "root_trace_id": version.root_trace_id,
    }
    record.payload = {**record.payload, **completion}
    record_audit(
        session,
        ctx,
        action="hotword_version.published",
        object_type="hotword_pack_version",
        object_id=version_id,
        before=before,
        after={**completion, "task_version": task_data},
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.published",
        aggregate_type="hotword_pack_version",
        aggregate_id=version_id,
        payload=completion,
    )
    return completion


def activate_hotword_version_for_task_release(
    session: Session,
    ctx: RequestContext,
    *,
    task_version_id: str,
    task_data: dict[str, Any],
    task_publish_run_id: str,
    published_by: str,
) -> dict[str, Any] | None:
    """Atomically make a hotword version effective after its TaskVersion publishes."""

    if task_data.get("source") != "hotword_pack_publish":
        return None
    version_id = str(task_data.get("source_hotword_pack_version_id") or "").strip()
    if not version_id:
        raise ApiError(
            "HOTWORD_TASK_VERSION_SOURCE_MISSING",
            "热词来源 TaskVersion 缺少 source_hotword_pack_version_id",
            409,
        )
    version = get_hotword_version(session, ctx, version_id, for_update=True)
    pack = _get_pack(session, ctx, version.pack_id, for_update=True)
    version_payload = dict(version.payload or {})
    expected_bindings = {
        "task_version_id": version_payload.get("task_version_id"),
        "source_publish_run_id": version_payload.get("publish_run_id"),
        "hotword_pack_version_id": version.version_id,
        "provider": version.compiled_provider,
        "model_version": version_payload.get("task_model_version"),
        "language": pack.language,
        "execution_mode": "production",
    }
    actual_bindings = {
        "task_version_id": task_version_id,
        "source_publish_run_id": task_data.get("source_publish_run_id"),
        "hotword_pack_version_id": task_data.get("hotword_pack_version_id"),
        "provider": task_data.get("provider"),
        "model_version": task_data.get("model_version"),
        "language": task_data.get("language"),
        "execution_mode": task_data.get("execution_mode"),
    }
    mismatches = [
        {"field": field, "expected": expected, "actual": actual_bindings.get(field)}
        for field, expected in expected_bindings.items()
        if expected is None or actual_bindings.get(field) != expected
    ]
    if version.status != "published":
        mismatches.append(
            {"field": "version_status", "expected": "published", "actual": version.status}
        )
    if mismatches:
        raise ApiError(
            "HOTWORD_PRODUCTION_ACTIVATION_BINDING_INVALID",
            "TaskVersion 与热词发布冻结绑定不一致，拒绝切换生产版本",
            409,
            details=mismatches,
        )

    previous_version_id = pack.production_version_id
    previous_version: HotwordPackVersion | None = None
    if previous_version_id and previous_version_id != version.version_id:
        previous_version = get_hotword_version(
            session,
            ctx,
            previous_version_id,
            for_update=True,
        )
        previous_version.payload = {
            **(previous_version.payload or {}),
            "production_active": False,
            "production_deactivated_at": datetime.now(UTC).isoformat(),
            "production_deactivated_by_task_version_id": task_version_id,
        }
        previous_version.resource_version += 1
        previous_version.current_trace_id = ctx.trace_id

    before = {
        "pack": _pack_data(pack),
        "version": _version_data(session, version),
        "previous_version_id": previous_version_id,
    }
    activated_at = datetime.now(UTC).isoformat()
    version.payload = {
        **version_payload,
        "production_active": True,
        "production_task_version_id": task_version_id,
        "production_task_publish_run_id": task_publish_run_id,
        "production_activated_at": activated_at,
        "production_activated_by": published_by,
    }
    version.resource_version += 1
    version.current_trace_id = ctx.trace_id
    pack.production_version_id = version.version_id
    pack.resource_version += 1
    pack.current_trace_id = ctx.trace_id
    session.flush()

    activation = {
        "pack_id": pack.pack_id,
        "hotword_pack_version_id": version.version_id,
        "previous_production_version_id": previous_version_id,
        "production_version_id": pack.production_version_id,
        "task_version_id": task_version_id,
        "task_publish_run_id": task_publish_run_id,
        "activated_at": activated_at,
        "activated_by": published_by,
        "root_trace_id": version.root_trace_id,
    }
    record_audit(
        session,
        ctx,
        action="hotword_version.production_activated",
        object_type="hotword_pack",
        object_id=pack.pack_id,
        before=before,
        after={
            "pack": _pack_data(pack),
            "version": _version_data(session, version),
            **activation,
        },
        trace_id=version.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_pack_version.production-activated",
        aggregate_type="hotword_pack_version",
        aggregate_id=version.version_id,
        payload=activation,
    )
    return activation


def validate_hotword_execution(
    session: Session,
    ctx: RequestContext,
    *,
    version_id: str | None,
    execution_mode: str,
    provider: str | None,
    language: str,
    require_production_active: bool = False,
) -> str | None:
    if version_id is None:
        return None
    version = get_hotword_version(session, ctx, version_id)
    pack = _get_pack(session, ctx, version.pack_id)
    if language != pack.language:
        raise ApiError(
            "HOTWORD_LANGUAGE_MISMATCH",
            "运行语言与热词包语言不一致",
            409,
            details=[{"requested": language, "pack_language": pack.language}],
        )
    requested_provider = canonicalize_hotword_provider(provider)
    if execution_mode in {"production", "diagnostic"} and version.status != "published":
        raise ApiError(
            "HOTWORD_VERSION_NOT_PUBLISHED",
            "生产或诊断运行只能绑定已发布热词包版本；候选版本仅可用于影子运行",
            409,
            details=[{"version_id": version_id, "status": version.status}],
        )
    if (
        execution_mode == "production"
        and require_production_active
        and pack.production_version_id != version.version_id
    ):
        raise ApiError(
            "HOTWORD_VERSION_NOT_PRODUCTION_ACTIVE",
            "TaskVersion 绑定的热词版本尚未完成生产激活或已被替换",
            409,
            details=[
                {
                    "requested_version_id": version.version_id,
                    "production_version_id": pack.production_version_id,
                    "task_version_id": (version.payload or {}).get("production_task_version_id"),
                }
            ],
        )
    if requested_provider != version.compiled_provider:
        raise ApiError(
            "HOTWORD_PROVIDER_MISMATCH",
            "运行 provider 必须与热词版本编译 provider 一致",
            409,
            details=[
                {
                    "requested_provider": requested_provider,
                    "compiled_provider": version.compiled_provider,
                }
            ],
        )
    if execution_mode == "shadow" and version.status not in {
        "ready_for_eval",
        "evaluating",
        "gate_blocked",
        "review_required",
        "approved",
        "published",
    }:
        raise ApiError(
            "HOTWORD_VERSION_NOT_SHADOW_READY",
            "影子运行要求候选版本已进入评测阶段",
            409,
        )
    if execution_mode == "shadow" and not version.provider_artifact_ref:
        raise ApiError(
            "HOTWORD_PROVIDER_ARTIFACT_REQUIRED",
            "影子运行要求热词版本已有 provider 编译产物",
            409,
        )
    return requested_provider


def validate_hotword_backfill_binding(
    session: Session,
    ctx: RequestContext,
    *,
    asset_key: str,
    impact_scope: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate and canonicalize immutable lineage for an ASR hotword backfill."""

    required_fields = (
        "hotword_pack_version_id",
        "eval_run_id",
        "task_version_id",
        "materialization_id",
    )
    missing = [
        field
        for field in required_fields
        if not isinstance(impact_scope.get(field), str) or not impact_scope[field].strip()
    ]
    if missing:
        raise ApiError(
            "HOTWORD_BACKFILL_BINDING_REQUIRED",
            "ASR 热词回填必须绑定词包版本、EvalRun、TaskVersion 和原物化记录",
            422,
            details=[{"missing_fields": missing}],
        )
    if impact_scope.get("overwrite_history") is not False:
        raise ApiError(
            "HOTWORD_BACKFILL_HISTORY_IMMUTABLE",
            "ASR 热词回填只能生成新资产，禁止覆盖历史转写",
            409,
        )

    version_id = str(impact_scope["hotword_pack_version_id"])
    eval_run_id = str(impact_scope["eval_run_id"])
    task_version_id = str(impact_scope["task_version_id"])
    materialization_id = str(impact_scope["materialization_id"])
    materialization = session.get(AssetMaterialization, materialization_id)
    if materialization is None:
        raise ApiError(
            "HOTWORD_BACKFILL_MATERIALIZATION_NOT_FOUND",
            "受控回填绑定的原 ASR 物化记录不存在",
            404,
            details=[{"materialization_id": materialization_id}],
        )
    if materialization.tenant_id != ctx.tenant_id or materialization.project_id != ctx.project_id:
        raise ApiError(
            "HOTWORD_BACKFILL_MATERIALIZATION_SCOPE_FORBIDDEN",
            "不能引用其他租户或项目的 ASR 物化记录",
            403,
            details=[{"materialization_id": materialization_id}],
        )
    materialization_payload = (
        materialization.payload if isinstance(materialization.payload, dict) else {}
    )
    if materialization_payload.get("asset_key") != asset_key:
        raise ApiError(
            "HOTWORD_BACKFILL_MATERIALIZATION_ASSET_MISMATCH",
            "原物化记录不属于当前 ASR 转写资产",
            409,
            details=[
                {
                    "materialization_id": materialization_id,
                    "expected_asset_key": asset_key,
                    "actual_asset_key": materialization_payload.get("asset_key"),
                }
            ],
        )
    if materialization.status != "success":
        raise ApiError(
            "HOTWORD_BACKFILL_MATERIALIZATION_NOT_READY",
            "只有成功的原 ASR 物化记录可以发起受控回填",
            409,
            details=[
                {
                    "materialization_id": materialization_id,
                    "status": materialization.status,
                }
            ],
        )
    version = get_hotword_version(session, ctx, version_id)
    if version.status != "published":
        raise ApiError(
            "HOTWORD_BACKFILL_VERSION_NOT_PUBLISHED",
            "受控回填只能使用已发布热词包版本",
            409,
            details=[{"version_id": version_id, "status": version.status}],
        )
    if version.eval_run_id != eval_run_id or version.eval_locked is not True:
        raise ApiError(
            "HOTWORD_BACKFILL_EVAL_MISMATCH",
            "受控回填必须绑定该词包版本成功且锁定的 EvalRun",
            409,
        )
    eval_run = get_run(session, ctx, eval_run_id)
    gate = eval_run.get("gate")
    if (
        eval_run.get("run_type") != "hotword_eval"
        or eval_run.get("status") != "success"
        or eval_run.get("hotword_pack_version_id") != version_id
        or eval_run.get("locked") is not True
        or not isinstance(gate, dict)
        or gate.get("passed") is not True
    ):
        raise ApiError(
            "HOTWORD_BACKFILL_EVAL_NOT_ELIGIBLE",
            "EvalRun 尚未成功、锁定并通过发布门禁",
            409,
        )

    expected_task_version_id = (version.payload or {}).get("task_version_id")
    if expected_task_version_id != task_version_id:
        raise ApiError(
            "HOTWORD_BACKFILL_TASK_MISMATCH",
            "TaskVersion 不属于该热词包发布记录",
            409,
            details=[
                {
                    "expected_task_version_id": expected_task_version_id,
                    "actual_task_version_id": task_version_id,
                }
            ],
        )
    task_version = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
            JsonResource.resource_key == task_version_id,
        )
    )
    task_data = task_version.data if task_version and isinstance(task_version.data, dict) else {}
    task_status = str(task_data.get("status") or (task_version.status if task_version else ""))
    legacy_import = bool((version.payload or {}).get("legacy_import"))
    if not legacy_import and task_data.get("source") != "hotword_pack_publish":
        raise ApiError(
            "HOTWORD_BACKFILL_TASK_SOURCE_INVALID",
            "新发布热词版本的回填 TaskVersion 必须来自 hotword_pack_publish",
            409,
            details=[
                {
                    "task_version_id": task_version_id,
                    "source": task_data.get("source"),
                }
            ],
        )
    from app.services.task_execution_policy import validate_task_version_publish_binding

    validate_task_version_publish_binding(
        session,
        ctx,
        task_data,
        task_version_id=task_version_id,
    )
    if (
        task_version is None
        or task_data.get("hotword_pack_version_id") != version_id
        or task_status != "published"
    ):
        raise ApiError(
            "HOTWORD_BACKFILL_TASK_NOT_PUBLISHED",
            "绑定热词包版本的 TaskVersion 必须先通过现有任务发布流程",
            409,
            details=[{"task_version_id": task_version_id, "status": task_status or None}],
        )

    supplied_root_trace_id = impact_scope.get("root_trace_id")
    if supplied_root_trace_id is not None and supplied_root_trace_id != version.root_trace_id:
        raise ApiError(
            "HOTWORD_BACKFILL_TRACE_MISMATCH",
            "回填根 Trace 必须由已发布词包版本恢复",
            409,
        )
    normalized_scope = {
        **impact_scope,
        "scope": "current_project",
        "source_asset_key": asset_key,
        "hotword_pack_version_id": version_id,
        "eval_run_id": eval_run_id,
        "task_version_id": task_version_id,
        "materialization_id": materialization_id,
        "source_materialization_trace_id": materialization.trace_id,
        "root_trace_id": version.root_trace_id,
        "overwrite_history": False,
    }
    affected_objects = [
        {"type": "data_asset", "id": asset_key},
        {"type": "asset_materialization", "id": materialization_id},
        {"type": "hotword_pack_version", "id": version_id},
        {"type": "eval_run", "id": eval_run_id},
        {"type": "task_version", "id": task_version_id},
    ]
    return normalized_scope, affected_objects


def create_badcase(
    session: Session,
    ctx: RequestContext,
    body: HotwordBadcaseCreateRequest,
    *,
    trusted_evidence: bool = False,
) -> dict[str, Any]:
    badcase_id = body.badcase_id or _new_id("badcase")
    existing = session.scalar(
        select(Badcase.badcase_id).where(
            Badcase.badcase_id == badcase_id,
            Badcase.tenant_id == ctx.tenant_id,
            Badcase.project_id == ctx.project_id,
        )
    )
    if existing is not None:
        raise ApiError("BADCASE_ALREADY_EXISTS", "Badcase 已存在", 409)
    if not trusted_evidence and (
        body.evidence_level != "discovery" or body.manual_correction_count != 0
    ):
        raise ApiError(
            "HOTWORD_BADCASE_EVIDENCE_UNTRUSTED",
            (
                "公开创建只能提交 discovery 证据且人工修正次数必须为 0；"
                "可信等级只能由受信分析或人工决策晋级"
            ),
            422,
        )
    linked_version = get_hotword_version(session, ctx, body.hotword_pack_version_id)
    evidence_storage_object = validate_scoped_storage_object_reference(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        storage_object_id=body.evidence_storage_object_id,
        purpose="ASR 热词 Badcase 证据",
    )
    if evidence_storage_object.source_type not in BADCASE_EVIDENCE_SOURCE_TYPES:
        raise ApiError(
            "HOTWORD_BADCASE_EVIDENCE_SOURCE_INVALID",
            "StorageObject 不是允许的 ASR 热词词级证据类型",
            422,
            details=[
                {
                    "storage_object_id": evidence_storage_object.storage_object_id,
                    "source_type": evidence_storage_object.source_type,
                    "allowed_source_types": sorted(BADCASE_EVIDENCE_SOURCE_TYPES),
                }
            ],
        )
    if not str(evidence_storage_object.trace_id or "").strip():
        raise ApiError(
            "HOTWORD_BADCASE_EVIDENCE_TRACE_REQUIRED",
            "ASR 热词词级证据必须绑定可追踪的 trace",
            409,
        )
    bound_badcase_id = session.scalar(
        select(Badcase.badcase_id).where(
            Badcase.tenant_id == ctx.tenant_id,
            Badcase.project_id == ctx.project_id,
            Badcase.evidence_storage_object_id == evidence_storage_object.storage_object_id,
            Badcase.badcase_id != badcase_id,
        )
    )
    if bound_badcase_id is not None:
        raise ApiError(
            "HOTWORD_BADCASE_EVIDENCE_ALREADY_BOUND",
            "该证据 StorageObject 已绑定其他 Badcase",
            409,
            details=[{"badcase_id": bound_badcase_id}],
        )
    root_trace_id = linked_version.root_trace_id
    metrics = calculate_hotword_metrics(
        expected_count=body.expected_count,
        correct_count=body.correct_count,
        weighted_error_count=body.weighted_error_count,
        false_insert_count=0,
        recognized_hotword_count=0,
    )
    candidate_state = classify_hotword_candidate(
        expected_count=body.expected_count,
        error_rate=metrics["error_rate"],
        manual_corrections=body.manual_correction_count,
    )
    if body.evidence_level == "discovery":
        candidate_state = "suspected"
    confidence = EVIDENCE_CONFIDENCE[body.evidence_level]
    record = Badcase(
        badcase_id=badcase_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status="pending-attribution",
        trace_id=ctx.trace_id,
        payload={
            "metrics": metrics,
            "business_weight": body.business_weight,
            "decision_history": [],
            "evidence_display_ref": body.evidence_ref,
        },
        capability=body.capability,
        error_type=body.error_type,
        standard_term=body.standard_term,
        recognized_text=body.recognized_text,
        evidence_ref=f"storage-object:{evidence_storage_object.storage_object_id}",
        evidence_storage_object_id=evidence_storage_object.storage_object_id,
        evidence_level=body.evidence_level,
        hotword_pack_version_id=body.hotword_pack_version_id,
        expected_count=body.expected_count,
        correct_count=body.correct_count,
        weighted_error_count=body.weighted_error_count,
        manual_correction_count=body.manual_correction_count,
        priority_score=priority_score(
            body.expected_count, metrics["error_rate"], confidence, body.business_weight
        ),
        candidate_state=candidate_state,
        root_cause=body.root_cause,
        fix_suggestion=body.fix_suggestion,
        downstream_impact=body.downstream_impact,
        resource_version=1,
        root_trace_id=root_trace_id,
        current_trace_id=ctx.trace_id,
    )
    session.add(record)
    session.flush()
    data = _badcase_data(record)
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
        payload={"badcase_id": badcase_id, "root_trace_id": record.root_trace_id},
    )
    return data


def _get_badcase(
    session: Session, ctx: RequestContext, badcase_id: str, *, for_update: bool = False
) -> Badcase:
    statement = select(Badcase).where(
        Badcase.badcase_id == badcase_id,
        Badcase.tenant_id == ctx.tenant_id,
        Badcase.project_id == ctx.project_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    record = session.scalar(statement)
    if record is None:
        raise ApiError("BADCASE_NOT_FOUND", f"Badcase 不存在：{badcase_id}", 404)
    if record.capability != "asr-hotword":
        raise ApiError(
            "BADCASE_CAPABILITY_NOT_ASR_HOTWORD",
            "该接口仅允许修改或引用 ASR 热词 Badcase",
            409,
        )
    return record


def list_badcases(
    session: Session,
    ctx: RequestContext,
    *,
    capability: str | None,
    error_type: str | None,
    status: str | None,
    hotword_pack_version_id: str | None,
    offset: int,
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    filters = [Badcase.tenant_id == ctx.tenant_id, Badcase.project_id == ctx.project_id]
    filters.append(Badcase.capability == (capability or "asr-hotword"))
    if error_type:
        if error_type not in HOTWORD_ERROR_TYPES:
            raise ApiError("HOTWORD_ERROR_TYPE_INVALID", "易错类型无效", 422)
        filters.append(Badcase.error_type == error_type)
    if status:
        filters.append(Badcase.status == status)
    if hotword_pack_version_id:
        get_hotword_version(session, ctx, hotword_pack_version_id)
        filters.append(Badcase.hotword_pack_version_id == hotword_pack_version_id)
    total = int(session.scalar(select(func.count()).select_from(Badcase).where(*filters)) or 0)
    records = session.scalars(
        select(Badcase)
        .where(*filters)
        .order_by(Badcase.priority_score.desc(), Badcase.updated_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    include_evidence = bool(set(ctx.roles) & BADCASE_EVIDENCE_READ_ROLES)
    return [_badcase_data(record, include_evidence=include_evidence) for record in records], total


def patch_badcase(
    session: Session,
    ctx: RequestContext,
    badcase_id: str,
    body: HotwordBadcasePatchRequest,
) -> dict[str, Any]:
    record = _get_badcase(session, ctx, badcase_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, record.resource_version, "Badcase")
    before = _badcase_data(record)
    for field, value in body.model_dump(
        exclude={"expected_resource_version"}, exclude_unset=True
    ).items():
        setattr(record, field, value)
    record.resource_version += 1
    record.current_trace_id = ctx.trace_id
    record.trace_id = ctx.trace_id
    session.flush()
    data = _badcase_data(record)
    record_audit(
        session,
        ctx,
        action="badcase.update",
        object_type="badcase",
        object_id=badcase_id,
        before=before,
        after=data,
        trace_id=record.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="badcase.updated",
        aggregate_type="badcase",
        aggregate_id=badcase_id,
        payload={
            "badcase_id": badcase_id,
            "resource_version": record.resource_version,
            "root_trace_id": record.root_trace_id,
        },
    )
    return data


def decide_badcase(
    session: Session,
    ctx: RequestContext,
    badcase_id: str,
    body: HotwordBadcaseDecisionRequest,
) -> dict[str, Any]:
    record = _get_badcase(session, ctx, badcase_id, for_update=True)
    _assert_expected_version(body.expected_resource_version, record.resource_version, "Badcase")
    before = _badcase_data(record)
    status_by_decision = {
        "confirmed": "pending-backflow",
        "rejected": "rejected",
        "needs-evidence": "pending-review",
    }
    record.status = status_by_decision[body.decision]
    payload = dict(record.payload or {})
    if body.decision == "confirmed":
        record.candidate_state = "confirmed"
        if record.evidence_level != "gold":
            record.evidence_level = "human-confirmed"
        metrics = calculate_hotword_metrics(
            expected_count=record.expected_count,
            correct_count=record.correct_count,
            weighted_error_count=record.weighted_error_count,
            false_insert_count=0,
            recognized_hotword_count=0,
        )
        raw_business_weight = payload.get("business_weight", 1.0)
        business_weight = (
            float(raw_business_weight)
            if isinstance(raw_business_weight, int | float)
            and not isinstance(raw_business_weight, bool)
            else 1.0
        )
        record.priority_score = priority_score(
            record.expected_count,
            metrics["error_rate"],
            1.0,
            business_weight,
        )
        payload["metrics"] = metrics
    else:
        # 否决/待补证是新的权威决策；不能继续沿用旧的人工确认晋级。
        record.candidate_state = "suspected"
        if record.evidence_level == "human-confirmed":
            record.evidence_level = "discovery"
        metrics = calculate_hotword_metrics(
            expected_count=record.expected_count,
            correct_count=record.correct_count,
            weighted_error_count=record.weighted_error_count,
            false_insert_count=0,
            recognized_hotword_count=0,
        )
        raw_business_weight = payload.get("business_weight", 1.0)
        business_weight = (
            float(raw_business_weight)
            if isinstance(raw_business_weight, int | float)
            and not isinstance(raw_business_weight, bool)
            else 1.0
        )
        record.priority_score = priority_score(
            record.expected_count,
            metrics["error_rate"],
            EVIDENCE_CONFIDENCE.get(record.evidence_level or "discovery", 0.4),
            business_weight,
        )
    history = list(payload.get("decision_history") or [])
    history.append(
        {
            "decision": body.decision,
            "reason": body.reason,
            "decided_by": ctx.user_id,
            "trace_id": ctx.trace_id,
            "decided_at": datetime.now(UTC).isoformat(),
        }
    )
    payload["decision_history"] = history
    record.payload = payload
    record.resource_version += 1
    record.current_trace_id = ctx.trace_id
    record.trace_id = ctx.trace_id
    session.flush()
    data = _badcase_data(record)
    record_audit(
        session,
        ctx,
        action="badcase.decide",
        object_type="badcase",
        object_id=badcase_id,
        before=before,
        after=data,
        trace_id=record.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="badcase.decision-recorded",
        aggregate_type="badcase",
        aggregate_id=badcase_id,
        payload={
            "badcase_id": badcase_id,
            "decision": body.decision,
            "root_trace_id": record.root_trace_id,
        },
    )
    return data


def create_hotword_analysis_run(
    session: Session, ctx: RequestContext, payload: dict[str, Any]
) -> dict[str, Any]:
    version_id = payload.get("hotword_pack_version_id")
    root_trace_id = ctx.trace_id
    if isinstance(version_id, str) and version_id:
        root_trace_id = get_hotword_version(session, ctx, version_id).root_trace_id
    run_id = _new_id("hwanalysis")
    run = RunRecord(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type="hotword_analysis",
        status="pending",
        run_key=f"hotword-analysis:{ctx.project_id}:{run_id}",
        partition_key=f"{ctx.tenant_id}/{ctx.project_id}",
        trace_id=ctx.trace_id,
        payload={**payload, "root_trace_id": root_trace_id},
    )
    session.add(run)
    response = {
        "id": run_id,
        "run_id": run_id,
        "run_type": "hotword_analysis",
        "status": "pending",
        "root_trace_id": root_trace_id,
    }
    record_audit(
        session,
        ctx,
        action="hotword_analysis.request",
        object_type="run_record",
        object_id=run_id,
        after=response,
    )
    enqueue_event(
        session,
        ctx,
        event_type="hotword_analysis.requested",
        aggregate_type="hotword_analysis",
        aggregate_id=run_id,
        payload={**payload, "run_id": run_id, "root_trace_id": root_trace_id},
    )
    return response


def _validate_analysis_result_scope(
    record: RunRecord,
    raw: dict[str, Any],
    *,
    index: int,
    object_type: str,
    bucket_start: datetime | None = None,
    bucket_end: datetime | None = None,
) -> None:
    mismatches: list[dict[str, Any]] = []
    for field in ("store_id", "provider", "model_version", "hotword_pack_version_id"):
        expected = record.payload.get(field)
        actual = raw.get(field)
        if expected is not None and actual != expected:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    date_from = record.payload.get("date_from")
    date_to = record.payload.get("date_to")
    if bucket_start is not None and isinstance(date_from, str):
        if bucket_start.date().isoformat() < date_from:
            mismatches.append(
                {"field": "date_from", "expected": date_from, "actual": bucket_start.isoformat()}
            )
    if bucket_end is not None and isinstance(date_to, str):
        last_instant = bucket_end - timedelta(microseconds=1)
        if last_instant.date().isoformat() > date_to:
            mismatches.append(
                {"field": "date_to", "expected": date_to, "actual": bucket_end.isoformat()}
            )
    if bucket_start is None and bucket_end is None:
        for field in ("date_from", "date_to"):
            expected = record.payload.get(field)
            actual = raw.get(field)
            if expected is not None and actual != expected:
                mismatches.append({"field": field, "expected": expected, "actual": actual})
    if mismatches:
        raise ApiError(
            "HOTWORD_ANALYSIS_SCOPE_MISMATCH",
            "热词分析完成结果超出请求时冻结的筛选范围",
            409,
            details=[{"object_type": object_type, "index": index, "mismatches": mismatches}],
        )


def _analysis_snapshot_evidence_provenance(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    raw: dict[str, Any],
    *,
    index: int,
    version_id: str | None,
) -> tuple[float, str, list[str]]:
    """Derive confidence from governed Badcases; never trust a worker-supplied float."""

    declared_confidence = raw.get("evidence_confidence")
    if declared_confidence is not None and (
        isinstance(declared_confidence, bool)
        or not isinstance(declared_confidence, int | float)
        or not 0 <= declared_confidence <= 1
    ):
        raise ApiError(
            "HOTWORD_METRIC_VALUE_INVALID",
            f"第 {index + 1} 个热词指标快照证据可信度无效",
            422,
        )
    source = str(raw.get("ground_truth_source") or "discovery")
    if source not in EVIDENCE_CONFIDENCE:
        raise ApiError(
            "HOTWORD_GROUND_TRUTH_SOURCE_INVALID",
            f"第 {index + 1} 个热词指标快照 ground_truth_source 无效",
            422,
        )
    raw_badcase_ids = raw.get("source_badcase_ids", [])
    if source == "discovery":
        if raw_badcase_ids not in (None, []):
            raise ApiError(
                "HOTWORD_GROUND_TRUTH_REFERENCE_INVALID",
                "discovery 快照不得伪装可信 Badcase 引用",
                422,
            )
        return EVIDENCE_CONFIDENCE[source], source, []
    if (
        not isinstance(raw_badcase_ids, list)
        or not raw_badcase_ids
        or len(raw_badcase_ids) > 1000
        or any(not isinstance(item, str) or not item.strip() for item in raw_badcase_ids)
    ):
        raise ApiError(
            "HOTWORD_GROUND_TRUTH_REFERENCE_REQUIRED",
            f"{source} 快照必须引用已治理的 source_badcase_ids",
            422,
        )
    badcase_ids = [str(item).strip() for item in raw_badcase_ids]
    if len(set(badcase_ids)) != len(badcase_ids):
        raise ApiError(
            "HOTWORD_GROUND_TRUTH_REFERENCE_INVALID",
            "source_badcase_ids 不得重复",
            422,
        )
    standard_term = raw.get("standard_term")
    run_root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id)
    invalid_refs: list[dict[str, Any]] = []
    for badcase_id in badcase_ids:
        badcase = _get_badcase(session, ctx, badcase_id)
        if (
            badcase.candidate_state != "confirmed"
            or badcase.status not in TRUSTED_BADCASE_STATUSES
            or badcase.evidence_level != source
            or (version_id is not None and badcase.hotword_pack_version_id != version_id)
            or badcase.root_trace_id != run_root_trace_id
            or (
                isinstance(standard_term, str)
                and standard_term
                and (
                    not badcase.standard_term
                    or normalize_hotword(badcase.standard_term) != normalize_hotword(standard_term)
                )
            )
        ):
            invalid_refs.append(
                {
                    "badcase_id": badcase_id,
                    "status": badcase.status,
                    "candidate_state": badcase.candidate_state,
                    "evidence_level": badcase.evidence_level,
                    "hotword_pack_version_id": badcase.hotword_pack_version_id,
                    "root_trace_id": badcase.root_trace_id,
                }
            )
    if invalid_refs:
        raise ApiError(
            "HOTWORD_GROUND_TRUTH_REFERENCE_INVALID",
            "指标快照引用了未确认、已否决或跨版本/跨 Trace 的 Badcase",
            409,
            details=invalid_refs,
        )
    return EVIDENCE_CONFIDENCE[source], source, badcase_ids


def _validate_analysis_storage_object(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    storage_object_id: str,
    role: str,
    purpose: str,
) -> None:
    storage_object = validate_scoped_storage_object_reference(
        session,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        storage_object_id=storage_object_id,
        purpose=purpose,
    )
    root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id)
    payload = storage_object.payload if isinstance(storage_object.payload, dict) else {}
    if (
        storage_object.source_type != "hotword_analysis"
        or storage_object.source_id != record.run_id
        or storage_object.trace_id != root_trace_id
        or payload.get("role") != role
    ):
        raise ApiError(
            "HOTWORD_ANALYSIS_STORAGE_BINDING_MISMATCH",
            "热词分析诊断/证据对象未绑定当前运行、角色或根 Trace",
            409,
            details=[
                {
                    "storage_object_id": storage_object_id,
                    "expected_source_id": record.run_id,
                    "expected_role": role,
                    "expected_root_trace_id": root_trace_id,
                }
            ],
        )


def materialize_hotword_analysis_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "hotword_analysis":
        return None
    result_ref = completion_receipt.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError(
            "HOTWORD_ANALYSIS_RESULT_REQUIRED",
            "热词分析完成回执必须包含 result_ref",
            422,
        )
    raw_snapshots = result_ref.get("metric_snapshots")
    if not isinstance(raw_snapshots, list):
        raise ApiError(
            "HOTWORD_METRIC_SNAPSHOTS_REQUIRED",
            "热词分析完成回执必须包含 metric_snapshots 数组",
            422,
        )
    raw_badcase_candidates = result_ref.get("badcase_candidates", [])
    if not isinstance(raw_badcase_candidates, list):
        raise ApiError(
            "HOTWORD_BADCASE_CANDIDATES_INVALID",
            "热词分析完成回执的 badcase_candidates 必须是数组",
            422,
        )
    run_root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id)
    materialized_ids: list[str] = []
    source_storage_object_ids: set[str] = set()
    for index, raw in enumerate(raw_snapshots):
        if not isinstance(raw, dict):
            raise ApiError(
                "HOTWORD_METRIC_SNAPSHOT_INVALID",
                f"第 {index + 1} 个热词指标快照必须是对象",
                422,
            )
        try:
            bucket_start = datetime.fromisoformat(str(raw["bucket_start"]).replace("Z", "+00:00"))
            bucket_end = datetime.fromisoformat(str(raw["bucket_end"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            raise ApiError(
                "HOTWORD_METRIC_BUCKET_INVALID",
                f"第 {index + 1} 个热词指标快照时间桶无效",
                422,
            ) from None
        if bucket_end <= bucket_start:
            raise ApiError(
                "HOTWORD_METRIC_BUCKET_INVALID",
                f"第 {index + 1} 个热词指标快照结束时间必须晚于开始时间",
                422,
            )
        _validate_analysis_result_scope(
            record,
            raw,
            index=index,
            object_type="metric_snapshot",
            bucket_start=bucket_start,
            bucket_end=bucket_end,
        )

        weighted_error_count = raw.get("weighted_error_count", 0.0)
        if (
            isinstance(weighted_error_count, bool)
            or not isinstance(weighted_error_count, int | float)
            or weighted_error_count < 0
        ):
            raise ApiError(
                "HOTWORD_METRIC_VALUE_INVALID",
                f"第 {index + 1} 个热词指标快照加权错误无效",
                422,
            )
        version_id = raw.get("hotword_pack_version_id") or record.payload.get(
            "hotword_pack_version_id"
        )
        if version_id is not None:
            if not isinstance(version_id, str) or not version_id:
                raise ApiError("HOTWORD_VERSION_REFERENCE_INVALID", "热词版本引用无效", 422)
            get_hotword_version(session, ctx, version_id)
        evidence_confidence, ground_truth_source, source_badcase_ids = (
            _analysis_snapshot_evidence_provenance(
                session,
                ctx,
                record,
                raw,
                index=index,
                version_id=version_id,
            )
        )
        snapshot_id = str(raw.get("snapshot_id") or _new_id("hwmetric"))
        expected_count = _snapshot_nonnegative_integer(raw, index, "expected_count")
        correct_count = _snapshot_nonnegative_integer(raw, index, "correct_count")
        if correct_count > expected_count:
            raise ApiError(
                "HOTWORD_METRIC_COUNT_INVALID",
                f"第 {index + 1} 个热词指标快照 correct_count 不能超过 expected_count",
                422,
            )
        if (
            not isinstance(raw.get("diagnostics_storage_object_id"), str)
            or not str(raw.get("diagnostics_storage_object_id")).strip()
        ):
            raise ApiError(
                "HOTWORD_METRIC_DIAGNOSTICS_REQUIRED",
                f"第 {index + 1} 个热词指标快照必须绑定诊断 StorageObject",
                422,
            )
        is_term_level = bool(raw.get("standard_term")) or any(
            field in raw
            for field in (
                "expected_count",
                "correct_count",
                "weighted_error_count",
                "recognized_hotword_count",
                "false_insert_count",
            )
        )
        if is_term_level and (
            not isinstance(raw.get("word_timestamps_storage_object_id"), str)
            or not str(raw.get("word_timestamps_storage_object_id")).strip()
        ):
            raise ApiError(
                "HOTWORD_METRIC_WORD_TIMESTAMPS_REQUIRED",
                f"第 {index + 1} 个词级热词指标快照必须绑定词级时间戳 StorageObject",
                422,
            )
        storage_references: dict[str, str | None] = {}
        for field, role, purpose in (
            (
                "word_timestamps_storage_object_id",
                "word_timestamps",
                "热词指标词级时间戳",
            ),
            ("diagnostics_storage_object_id", "diagnostics", "热词指标诊断"),
        ):
            raw_storage_object_id = raw.get(field)
            if raw_storage_object_id is None:
                storage_references[field] = None
                continue
            if not isinstance(raw_storage_object_id, str) or not raw_storage_object_id.strip():
                raise ApiError(
                    "STORAGE_OBJECT_REFERENCE_INVALID",
                    f"第 {index + 1} 个热词指标快照 {purpose} 引用无效",
                    422,
                    details=[{"field": field, "index": index}],
                )
            _validate_analysis_storage_object(
                session,
                ctx,
                record,
                storage_object_id=raw_storage_object_id,
                role=role,
                purpose=purpose,
            )
            normalized_storage_object_id = raw_storage_object_id.strip()
            storage_references[field] = normalized_storage_object_id
            source_storage_object_ids.add(normalized_storage_object_id)
        snapshot = HotwordMetricSnapshot(
            snapshot_id=snapshot_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            store_id=str(raw["store_id"]) if raw.get("store_id") else None,
            provider=str(raw["provider"]) if raw.get("provider") else None,
            model_version=str(raw["model_version"]) if raw.get("model_version") else None,
            hotword_pack_version_id=version_id,
            standard_term=str(raw["standard_term"]) if raw.get("standard_term") else None,
            expected_count=expected_count,
            correct_count=correct_count,
            weighted_error_count=float(weighted_error_count),
            false_insert_count=_snapshot_nonnegative_integer(raw, index, "false_insert_count"),
            recognized_hotword_count=_snapshot_nonnegative_integer(
                raw, index, "recognized_hotword_count"
            ),
            impacted_session_count=_snapshot_nonnegative_integer(
                raw, index, "impacted_session_count"
            ),
            evidence_confidence=float(evidence_confidence),
            root_trace_id=run_root_trace_id,
            payload={
                "source_run_id": record.run_id,
                "ground_truth_source": ground_truth_source,
                "source_badcase_ids": source_badcase_ids,
                **storage_references,
            },
        )
        session.add(snapshot)
        materialized_ids.append(snapshot_id)
    materialized_badcase_ids: list[str] = []
    materialization_ctx = replace(
        ctx,
        trace_id=record.trace_id,
        parent_trace_id=ctx.trace_id,
    )
    default_version_id = record.payload.get("hotword_pack_version_id")
    for index, raw_candidate in enumerate(raw_badcase_candidates):
        if not isinstance(raw_candidate, dict):
            raise ApiError(
                "HOTWORD_BADCASE_CANDIDATE_INVALID",
                f"第 {index + 1} 个热词 Badcase 候选必须是对象",
                422,
            )
        _validate_analysis_result_scope(
            record,
            raw_candidate,
            index=index,
            object_type="badcase_candidate",
        )
        candidate_payload = {
            key: value
            for key, value in raw_candidate.items()
            if key not in {"date_from", "date_to", "store_id", "provider", "model_version"}
        }
        if default_version_id and not candidate_payload.get("hotword_pack_version_id"):
            candidate_payload["hotword_pack_version_id"] = default_version_id
        if (
            candidate_payload.get("evidence_level") != "discovery"
            or candidate_payload.get("manual_correction_count", 0) != 0
        ):
            raise ApiError(
                "HOTWORD_ANALYSIS_BADCASE_TRUST_FORBIDDEN",
                "热词分析 worker 只能生成 discovery Badcase；可信等级必须由人工决策晋级",
                422,
                details=[{"index": index}],
            )
        raw_evidence_storage_object_id = candidate_payload.get("evidence_storage_object_id")
        if (
            not isinstance(raw_evidence_storage_object_id, str)
            or not raw_evidence_storage_object_id
        ):
            raise ApiError(
                "HOTWORD_BADCASE_EVIDENCE_REQUIRED",
                "热词分析 Badcase 候选必须引用词级证据对象",
                422,
            )
        _validate_analysis_storage_object(
            session,
            ctx,
            record,
            storage_object_id=raw_evidence_storage_object_id,
            role="badcase_evidence",
            purpose="热词分析 Badcase 词级证据",
        )
        try:
            candidate = HotwordBadcaseCreateRequest.model_validate(candidate_payload)
        except ValidationError as exc:
            raise ApiError(
                "HOTWORD_BADCASE_CANDIDATE_INVALID",
                f"第 {index + 1} 个热词 Badcase 候选无效",
                422,
                details=[
                    {
                        "field": ".".join(str(part) for part in error["loc"]),
                        "message": str(error["msg"]),
                        "code": str(error["type"]),
                    }
                    for error in exc.errors()
                ],
            ) from exc
        materialized = create_badcase(
            session,
            materialization_ctx,
            candidate,
            trusted_evidence=False,
        )
        materialized_badcase_ids.append(str(materialized["badcase_id"]))
    response = {
        "source_run_id": record.run_id,
        "metric_definition_version": "v1",
        "source_storage_object_ids": sorted(source_storage_object_ids),
        "snapshot_ids": materialized_ids,
        "snapshot_count": len(materialized_ids),
        "badcase_ids": materialized_badcase_ids,
        "badcase_count": len(materialized_badcase_ids),
        "root_trace_id": run_root_trace_id,
    }
    enqueue_event(
        session,
        ctx,
        event_type="hotword_metrics.materialized",
        aggregate_type="hotword_metrics",
        aggregate_id=f"metrics:{record.run_id}",
        payload=response,
    )
    return response


def hotword_statistics(
    session: Session,
    ctx: RequestContext,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    store_id: str | None,
    provider: str | None,
    model_version: str | None,
    hotword_pack_version_id: str | None,
) -> dict[str, Any]:
    filters = [
        HotwordMetricSnapshot.tenant_id == ctx.tenant_id,
        HotwordMetricSnapshot.project_id == ctx.project_id,
    ]
    if date_from is not None:
        filters.append(HotwordMetricSnapshot.bucket_start >= date_from)
    if date_to is not None:
        filters.append(HotwordMetricSnapshot.bucket_end <= date_to)
    if store_id:
        filters.append(HotwordMetricSnapshot.store_id == store_id)
    if provider:
        filters.append(HotwordMetricSnapshot.provider == provider)
    if model_version:
        filters.append(HotwordMetricSnapshot.model_version == model_version)
    if hotword_pack_version_id:
        get_hotword_version(session, ctx, hotword_pack_version_id)
        filters.append(HotwordMetricSnapshot.hotword_pack_version_id == hotword_pack_version_id)
    # 发现级证据只用于候选发现，不能进入可信易错分母或 Top 易错词。
    filters.append(HotwordMetricSnapshot.evidence_confidence >= 0.8)
    records = session.scalars(select(HotwordMetricSnapshot).where(*filters)).all()

    correction_filters = [
        AsrAnnotationCorrection.tenant_id == ctx.tenant_id,
        AsrAnnotationCorrection.project_id == ctx.project_id,
        AsrAnnotationCorrection.status == "submitted",
    ]
    if date_from is not None:
        correction_filters.append(AsrAnnotationCorrection.observed_at >= date_from)
    if date_to is not None:
        correction_filters.append(AsrAnnotationCorrection.observed_at < date_to)
    if store_id:
        correction_filters.append(AsrAnnotationCorrection.store_id == store_id)
    if provider:
        correction_filters.append(AsrAnnotationCorrection.provider == provider)
    if model_version:
        correction_filters.append(AsrAnnotationCorrection.model_version == model_version)
    if hotword_pack_version_id:
        correction_filters.append(
            AsrAnnotationCorrection.hotword_pack_version_id == hotword_pack_version_id
        )
    correction_records = session.scalars(
        select(AsrAnnotationCorrection)
        .where(*correction_filters)
        .order_by(AsrAnnotationCorrection.created_at, AsrAnnotationCorrection.correction_id)
    ).all()
    expected = sum(record.expected_count for record in records)
    correct = sum(record.correct_count for record in records)
    weighted_errors = sum(record.weighted_error_count for record in records)
    false_inserts = sum(record.false_insert_count for record in records)
    recognized = sum(record.recognized_hotword_count for record in records)
    impacted = sum(record.impacted_session_count for record in records)
    metrics = calculate_hotword_metrics(
        expected_count=expected,
        correct_count=correct,
        weighted_error_count=weighted_errors,
        false_insert_count=false_inserts,
        recognized_hotword_count=recognized,
    )
    by_term: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.standard_term:
            continue
        entry = by_term.setdefault(
            record.standard_term,
            {
                "standard_term": record.standard_term,
                "expected_count": 0,
                "weighted_error_count": 0.0,
                "impacted_session_count": 0,
                "evidence_confidence": 0.0,
                "root_trace_id": record.root_trace_id,
            },
        )
        entry["expected_count"] = int(entry["expected_count"]) + record.expected_count
        entry["weighted_error_count"] = (
            float(entry["weighted_error_count"]) + record.weighted_error_count
        )
        entry["impacted_session_count"] = (
            int(entry["impacted_session_count"]) + record.impacted_session_count
        )
        entry["evidence_confidence"] = max(
            float(entry["evidence_confidence"]), record.evidence_confidence
        )

    term_cases: dict[str, list[Badcase]] = {}
    include_badcase_evidence = bool(set(ctx.roles) & BADCASE_EVIDENCE_READ_ROLES)
    if by_term:
        case_filters = [
            Badcase.tenant_id == ctx.tenant_id,
            Badcase.project_id == ctx.project_id,
            Badcase.capability == "asr-hotword",
            Badcase.standard_term.in_(list(by_term)),
            Badcase.status.in_(TRUSTED_BADCASE_STATUSES),
            Badcase.candidate_state == "confirmed",
        ]
        if hotword_pack_version_id:
            case_filters.append(Badcase.hotword_pack_version_id == hotword_pack_version_id)
        for badcase in session.scalars(select(Badcase).where(*case_filters)).all():
            if badcase.standard_term:
                term_cases.setdefault(badcase.standard_term, []).append(badcase)

    items: list[dict[str, Any]] = []
    for entry in by_term.values():
        term_expected = int(entry["expected_count"])
        # false-boost 仍进入汇总 KPI；无应出现次数的词不伪装成 0% 易错词。
        if term_expected <= 0:
            continue
        term_errors = float(entry["weighted_error_count"])
        term_error_rate = min(term_errors / term_expected, 1.0) if term_expected else 0.0
        cases = term_cases.get(str(entry["standard_term"]), [])
        cases.sort(key=lambda case: case.priority_score, reverse=True)
        evidence_level = cases[0].evidence_level if cases else "discovery"
        if evidence_level not in EVIDENCE_CONFIDENCE:
            evidence_level = "discovery"
        evidence_confidence = max(
            (EVIDENCE_CONFIDENCE.get(case.evidence_level or "discovery", 0.4) for case in cases),
            default=EVIDENCE_CONFIDENCE["discovery"],
        )
        human_corrections = sum(case.manual_correction_count for case in cases)
        business_weight = max(
            (
                float(case.payload.get("business_weight", 1.0))
                if isinstance(case.payload, dict)
                else 1.0
                for case in cases
            ),
            default=1.0,
        )
        candidate_state = classify_hotword_candidate(
            expected_count=term_expected,
            error_rate=term_error_rate,
            manual_corrections=human_corrections,
        )
        error_type = cases[0].error_type if cases else "misrecognition"
        if error_type not in HOTWORD_ERROR_TYPES:
            error_type = "misrecognition"
        calculated_priority = priority_score(
            term_expected,
            term_error_rate,
            evidence_confidence,
            business_weight,
        )
        items.append(
            {
                "standard_term": entry["standard_term"],
                "recognized_forms": sorted(
                    {
                        case.recognized_text
                        for case in cases
                        if isinstance(case.recognized_text, str) and case.recognized_text
                    }
                )
                if include_badcase_evidence
                else [],
                "error_type": error_type,
                "expected_count": term_expected,
                "human_correction_count": human_corrections,
                "error_rate": term_error_rate,
                "evidence_level": evidence_level,
                "evidence_confidence": evidence_confidence,
                "business_weight": business_weight,
                "priority": int(
                    round(max([calculated_priority, *[c.priority_score for c in cases]]))
                ),
                "suspected": (
                    not cases or candidate_state == "suspected" or evidence_level == "discovery"
                ),
                "impacted_session_count": int(entry["impacted_session_count"]),
                "badcase_ids": [case.badcase_id for case in cases],
                "root_trace_id": entry["root_trace_id"],
            }
        )

    correction_by_term: dict[str, dict[str, Any]] = {}
    for correction in correction_records:
        entry = correction_by_term.setdefault(
            correction.normalized_term,
            {
                "standard_term": correction.standard_term,
                "recognized_forms": set(),
                "error_types": [],
                "audio_session_ids": set(),
                "badcase_ids": set(),
                "correction_ids": [],
                "root_trace_id": correction.root_trace_id,
            },
        )
        if include_badcase_evidence and correction.recognized_text:
            entry["recognized_forms"].add(correction.recognized_text)
        entry["error_types"].append(correction.error_type)
        entry["audio_session_ids"].add(correction.audio_session_id)
        entry["badcase_ids"].add(correction.source_badcase_id)
        entry["correction_ids"].append(correction.correction_id)

    discovery_items: list[dict[str, Any]] = []
    for normalized_term, entry in correction_by_term.items():
        correction_count = len(entry["correction_ids"])
        threshold_met = correction_count >= 2
        discovery_items.append(
            {
                "standard_term": entry["standard_term"],
                "normalized_term": normalized_term,
                "recognized_forms": sorted(entry["recognized_forms"]),
                "error_type": entry["error_types"][-1],
                "annotation_correction_count": correction_count,
                "human_correction_count": correction_count,
                "impacted_session_count": len(entry["audio_session_ids"]),
                "evidence_level": "discovery",
                "evidence_confidence": EVIDENCE_CONFIDENCE["discovery"],
                "candidate_state": "suspected",
                "threshold_met": threshold_met,
                "suspected": True,
                "eligible_for_release_gate": False,
                "priority": int(
                    round(
                        priority_score(
                            correction_count,
                            1.0,
                            EVIDENCE_CONFIDENCE["discovery"],
                            1.0,
                        )
                    )
                ),
                "source_counts": {
                    "listening_annotation": correction_count,
                    "metric_snapshot": 0,
                },
                "badcase_ids": sorted(entry["badcase_ids"]),
                "correction_ids": sorted(entry["correction_ids"]),
                "root_trace_id": entry["root_trace_id"],
            }
        )

    correction_counts_by_normalized = {
        normalized_term: len(entry["correction_ids"])
        for normalized_term, entry in correction_by_term.items()
    }
    for item in items:
        normalized_term = normalize_hotword(str(item["standard_term"]))
        annotation_count = correction_counts_by_normalized.get(normalized_term, 0)
        item["annotation_correction_count"] = annotation_count
        item["source_counts"] = {
            "metric_snapshot": int(item["expected_count"]),
            "listening_annotation": annotation_count,
        }
    discovery_items.sort(
        key=lambda item: (
            int(item["priority"]),
            int(item["annotation_correction_count"]),
            str(item["standard_term"]),
        ),
        reverse=True,
    )
    items.sort(
        key=lambda item: (
            int(item["priority"]),
            float(item["error_rate"]),
            int(item["expected_count"]),
        ),
        reverse=True,
    )
    return {
        "summary": {
            # 分母为零代表“无可计算样本”，不是 0% 的好结果。
            "coverage_rate": min(recognized / expected, 1.0) if expected else None,
            "recall_rate": min(metrics["recall_rate"], 1.0) if expected else None,
            "error_rate": min(metrics["error_rate"], 1.0) if expected else None,
            "false_boost_rate": (min(metrics["false_boost_rate"], 1.0) if recognized else None),
            "impacted_session_count": impacted,
            "trusted_expected_count": expected,
            "correct_hit_count": correct,
            "weighted_error_count": weighted_errors,
            "recognized_hotword_count": recognized,
            "false_insertion_count": false_inserts,
        },
        "discovery_summary": {
            "annotation_correction_count": len(correction_records),
            "unique_terms": len(correction_by_term),
            "impacted_session_count": len(
                {record.audio_session_id for record in correction_records}
            ),
            "threshold_met_term_count": sum(1 for item in discovery_items if item["threshold_met"]),
            "evidence_level": "discovery",
            "eligible_for_release_gate": False,
        },
        "items": items[:20],
        "discovery_items": discovery_items[:20],
        "dimensions": {
            "date_from": _iso(date_from),
            "date_to": _iso(date_to),
            "store_id": store_id,
            "provider": provider,
            "model_version": model_version,
            "hotword_pack_version_id": hotword_pack_version_id,
        },
    }
