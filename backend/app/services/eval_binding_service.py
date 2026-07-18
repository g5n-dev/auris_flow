from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    EvalDatasetVersion,
    LabelAggregationPolicyVersion,
    LabelVersion,
    PromptVersion,
    RunRecord,
)
from app.services.eval_dataset_service import locked_eval_dataset_snapshot
from app.services.scene_profile_service import (
    assert_active_scene_profile_binding,
    assert_scene_profile_snapshot,
)

_LABEL_LOCKED_STATUSES = frozenset({"published", "approved", "locked", "validated"})
_PROMPT_EVALUABLE_STATUSES = frozenset(
    {"candidate", "in-review", "awaiting-adjudication", "approved", "published"}
)
_POLICY_ACTIVE_STATUSES = frozenset({"active", "published", "approved"})
_LABEL_EVAL_SUITES = (
    "golden",
    "boundary",
    "adversarial",
    "fresh",
    "canary",
    "regression",
)
_SCENE_LOCK_FIELDS = (
    "scene_profile_id",
    "scene_profile_version_id",
    "scene_profile_snapshot_sha256",
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_error(object_type: str, object_id: str) -> ApiError:
    return ApiError(
        "EVAL_BINDING_NOT_FOUND",
        f"评测锁定对象不存在或不属于当前租户项目：{object_type}/{object_id}",
        404,
        details=[{"object_type": object_type, "object_id": object_id}],
    )


def _optimization_lock_document(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "label_version_id": payload.get("label_version_id"),
        "prompt_version_id": payload.get("prompt_version_id"),
        "prompt_candidate_ids": sorted(
            str(item) for item in payload.get("prompt_candidate_ids", []) if item
        ),
        "model_version": payload.get("model_version"),
        "aggregation_policy_version_id": payload.get("aggregation_policy_version_id"),
        "eval_dataset_version_id": payload.get("eval_dataset_version_id"),
        "trigger_hash": payload.get("trigger_hash"),
    }


def _manifest_drift(field: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"field": field, "expected": expected, "actual": actual}


def _active_scene_lock(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, str]:
    binding = assert_active_scene_profile_binding(
        session,
        ctx,
        scene_profile_id=payload.get("scene_profile_id"),
        scene_profile_version_id=payload.get("scene_profile_version_id"),
        scene_profile_snapshot_sha256=payload.get("scene_profile_snapshot_sha256"),
    )
    return {
        "scene_profile_id": binding.scene_profile_id,
        "scene_profile_version_id": binding.scene_profile_version_id,
        "scene_profile_snapshot_sha256": binding.manifest_sha256,
    }


def revalidate_labeling_eval_manifest(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Re-read every frozen labeling artifact and reject any manifest drift.

    This is intentionally called at run creation, completion receipt ingestion and
    release construction.  A previously valid ``binding_sha256`` is not sufficient:
    all strong versions and the object-storage manifest must still resolve to the
    exact fingerprints captured by the EvalRun.
    """

    if payload.get("capability") != "labeling":
        return payload
    locked = payload.get("locked_versions")
    if not isinstance(locked, dict) or not locked:
        raise ApiError(
            "LABEL_EVAL_MANIFEST_MISSING",
            "标签 EvalRun 缺少锁定 manifest",
            409,
        )

    dataset_id = str(locked.get("eval_dataset_version_id") or "")
    label_version_id = str(locked.get("label_version_id") or "")
    prompt_version_id = str(locked.get("prompt_version_id") or "")
    policy_version_id = str(locked.get("aggregation_policy_version_id") or "")
    optimization_run_id = str(locked.get("optimization_run_id") or "")
    model_version = str(locked.get("model_version") or "")
    scene_profile_id = str(locked.get("scene_profile_id") or "")
    scene_profile_version_id = str(locked.get("scene_profile_version_id") or "")
    scene_profile_snapshot_sha256 = str(locked.get("scene_profile_snapshot_sha256") or "")
    if not all(
        (
            dataset_id,
            label_version_id,
            prompt_version_id,
            policy_version_id,
            optimization_run_id,
            model_version,
            scene_profile_id,
            scene_profile_version_id,
            scene_profile_snapshot_sha256,
        )
    ):
        raise ApiError(
            "LABEL_EVAL_MANIFEST_INCOMPLETE",
            "标签 EvalRun 锁定 manifest 不完整",
            409,
        )

    assert_scene_profile_snapshot(
        session,
        ctx,
        scene_profile_id=scene_profile_id,
        scene_profile_version_id=scene_profile_version_id,
        scene_profile_snapshot_sha256=scene_profile_snapshot_sha256,
    )

    dataset = session.scalar(
        select(EvalDatasetVersion).where(
            EvalDatasetVersion.eval_dataset_id == dataset_id,
            EvalDatasetVersion.tenant_id == ctx.tenant_id,
            EvalDatasetVersion.project_id == ctx.project_id,
        )
    )
    if dataset is None:
        raise _scope_error("eval_dataset_version", dataset_id)
    current_dataset_snapshot = locked_eval_dataset_snapshot(
        session,
        ctx,
        dataset_id,
        required_capability="labeling",
    )

    label_version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.label_version_id == label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
    )
    if label_version is None:
        raise _scope_error("label_version", label_version_id)
    if label_version.status not in _LABEL_LOCKED_STATUSES:
        raise ApiError(
            "EVAL_LABEL_VERSION_NOT_LOCKED",
            "标签评测只能绑定已锁定的标签版本",
            409,
            details=[{"label_version_id": label_version_id, "status": label_version.status}],
        )

    prompt_version = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == prompt_version_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    if prompt_version is None:
        raise _scope_error("prompt_version", prompt_version_id)
    if prompt_version.status not in _PROMPT_EVALUABLE_STATUSES:
        raise ApiError(
            "EVAL_PROMPT_VERSION_NOT_EVALUABLE",
            "PromptVersion 当前状态不能进入锁定评测",
            409,
            details=[{"prompt_version_id": prompt_version_id, "status": prompt_version.status}],
        )

    policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    if policy is None:
        raise _scope_error("label_aggregation_policy", policy_version_id)

    optimization_run = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == optimization_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_optimization",
        )
    )
    if optimization_run is None:
        raise _scope_error("label_optimization_run", optimization_run_id)

    current_optimization_lock_sha256 = _canonical_sha256(
        _optimization_lock_document(optimization_run.payload or {})
    )
    actual_suites = payload.get("evaluation_suites") or []
    mismatches: list[dict[str, Any]] = []
    current_values = {
        "eval_dataset_manifest_sha256": dataset.manifest_sha256,
        "eval_dataset_manifest_storage_object_id": dataset.manifest_storage_object_id,
        "eval_dataset_snapshot_sha256": current_dataset_snapshot.get("snapshot_sha256"),
        "eval_dataset_resource_version": dataset.resource_version,
        "label_resource_version": label_version.resource_version,
        "prompt_content_sha256": prompt_version.content_sha256,
        "aggregation_policy_sha256": policy.canonical_sha256,
        "optimization_run_lock_sha256": current_optimization_lock_sha256,
        "evaluation_suites": list(_LABEL_EVAL_SUITES),
    }
    for field, actual in current_values.items():
        expected = locked.get(field)
        if expected != actual:
            mismatches.append(_manifest_drift(field, expected, actual))
    for field in _SCENE_LOCK_FIELDS:
        if payload.get(field) != locked.get(field):
            mismatches.append(_manifest_drift(field, locked.get(field), payload.get(field)))

    expected_binding = _canonical_sha256(locked)
    if payload.get("binding_sha256") != expected_binding:
        mismatches.append(
            _manifest_drift(
                "binding_sha256",
                expected_binding,
                payload.get("binding_sha256"),
            )
        )
    if list(actual_suites) != list(_LABEL_EVAL_SUITES):
        mismatches.append(
            _manifest_drift("evaluation_suites", list(_LABEL_EVAL_SUITES), actual_suites)
        )
    if label_version.status not in _LABEL_LOCKED_STATUSES:
        mismatches.append(
            _manifest_drift(
                "label_version.status", sorted(_LABEL_LOCKED_STATUSES), label_version.status
            )
        )
    if prompt_version.status not in _PROMPT_EVALUABLE_STATUSES:
        mismatches.append(
            _manifest_drift(
                "prompt_version.status",
                sorted(_PROMPT_EVALUABLE_STATUSES),
                prompt_version.status,
            )
        )
    if prompt_version.label_version_id != label_version_id or prompt_version.model_version not in {
        None,
        model_version,
    }:
        mismatches.append(
            _manifest_drift(
                "prompt_version.binding",
                {"label_version_id": label_version_id, "model_version": model_version},
                {
                    "label_version_id": prompt_version.label_version_id,
                    "model_version": prompt_version.model_version,
                },
            )
        )
    if policy.status not in _POLICY_ACTIVE_STATUSES or policy.label_version_id != label_version_id:
        mismatches.append(
            _manifest_drift(
                "aggregation_policy.binding",
                {"label_version_id": label_version_id, "status": sorted(_POLICY_ACTIVE_STATUSES)},
                {"label_version_id": policy.label_version_id, "status": policy.status},
            )
        )
    if optimization_run.status != "success":
        mismatches.append(
            _manifest_drift("optimization_run.status", "success", optimization_run.status)
        )
    optimization_payload = optimization_run.payload or {}
    expected_optimization_bindings = {
        "label_version_id": label_version_id,
        "model_version": model_version,
        "aggregation_policy_version_id": policy_version_id,
        "eval_dataset_version_id": dataset_id,
    }
    for field, expected in expected_optimization_bindings.items():
        if optimization_payload.get(field) != expected:
            mismatches.append(
                _manifest_drift(
                    f"optimization_run.{field}", expected, optimization_payload.get(field)
                )
            )
    allowed_prompt_ids = {
        str(optimization_payload.get("prompt_version_id") or ""),
        *{str(item) for item in optimization_payload.get("prompt_candidate_ids", []) if item},
    }
    if prompt_version_id not in allowed_prompt_ids:
        mismatches.append(
            _manifest_drift(
                "optimization_run.prompt_version_id",
                sorted(allowed_prompt_ids),
                prompt_version_id,
            )
        )

    if mismatches:
        raise ApiError(
            "LABEL_EVAL_MANIFEST_DRIFT",
            "标签 EvalRun 锁定 manifest 已漂移，禁止完成或发布",
            409,
            details=mismatches,
        )
    return {
        **payload,
        "locked_versions": locked,
        "binding_sha256": expected_binding,
    }


def validate_labeling_eval_binding(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate and enrich an immutable labeling evaluation bundle."""

    scene_lock = _active_scene_lock(session, ctx, payload)
    payload = {**payload, **scene_lock}
    if payload.get("capability") != "labeling":
        locked_versions = {**scene_lock}
        return {
            **payload,
            "locked_versions": locked_versions,
            "binding_sha256": _canonical_sha256(locked_versions),
        }

    dataset_id = str(payload["eval_dataset_version_id"])
    label_version_id = str(payload["label_version_id"])
    prompt_version_id = str(payload["prompt_version_id"])
    model_version = str(payload["model_version"])
    policy_version_id = str(payload["aggregation_policy_version_id"])
    optimization_run_id = str(payload["optimization_run_id"])

    dataset = session.scalar(
        select(EvalDatasetVersion).where(
            EvalDatasetVersion.eval_dataset_id == dataset_id,
            EvalDatasetVersion.tenant_id == ctx.tenant_id,
            EvalDatasetVersion.project_id == ctx.project_id,
        )
    )
    if dataset is None:
        raise _scope_error("eval_dataset_version", dataset_id)
    if dataset.status != "locked" or dataset.capability != "labeling":
        raise ApiError(
            "EVAL_DATASET_NOT_LOCKED",
            "标签评测只能读取已锁定的 labeling 数据集版本",
            409,
            details=[
                {
                    "eval_dataset_version_id": dataset_id,
                    "status": dataset.status,
                    "capability": dataset.capability,
                }
            ],
        )
    dataset_snapshot = locked_eval_dataset_snapshot(
        session,
        ctx,
        dataset_id,
        required_capability="labeling",
    )

    label_version = session.scalar(
        select(LabelVersion).where(
            LabelVersion.label_version_id == label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
    )
    if label_version is None:
        raise _scope_error("label_version", label_version_id)
    label_payload = label_version.payload or {}
    label_root_trace_id = str(
        label_payload.get("root_trace_id")
        or label_version.trace_id
        or ctx.correlation_id
        or ctx.trace_id
    )

    prompt_version = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == prompt_version_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    if prompt_version is None:
        raise _scope_error("prompt_version", prompt_version_id)
    if prompt_version.label_version_id != label_version_id or prompt_version.model_version not in {
        None,
        model_version,
    }:
        raise ApiError(
            "EVAL_PROMPT_BINDING_MISMATCH",
            "PromptVersion 与锁定标签或模型版本不一致",
            409,
        )

    policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    if policy is None:
        raise _scope_error("label_aggregation_policy", policy_version_id)
    if policy.label_version_id != label_version_id or policy.status != "active":
        raise ApiError(
            "EVAL_AGGREGATION_POLICY_BINDING_MISMATCH",
            "聚合策略必须是该标签版本的 active 强版本",
            409,
        )

    optimization_run = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == optimization_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_optimization",
        )
    )
    if optimization_run is None:
        raise _scope_error("label_optimization_run", optimization_run_id)
    if optimization_run.status != "success":
        raise ApiError(
            "EVAL_OPTIMIZATION_NOT_MATERIALIZED",
            "优化运行必须成功物化候选后才能启动锁定评测",
            409,
            details=[{"status": optimization_run.status}],
        )
    optimization_payload = optimization_run.payload or {}
    expected_locks = {
        "label_version_id": label_version_id,
        "model_version": model_version,
        "aggregation_policy_version_id": policy_version_id,
        "eval_dataset_version_id": dataset_id,
    }
    mismatches = [
        field
        for field, expected in expected_locks.items()
        if optimization_payload.get(field) != expected
    ]
    allowed_prompt_ids = {
        str(optimization_payload.get("prompt_version_id") or ""),
        *{str(item) for item in optimization_payload.get("prompt_candidate_ids", []) if item},
    }
    if prompt_version_id not in allowed_prompt_ids:
        mismatches.append("prompt_version_id")
    if mismatches:
        raise ApiError(
            "EVAL_OPTIMIZATION_BINDING_MISMATCH",
            "EvalRun 与优化运行锁定 Bundle 不一致",
            409,
            details=[{"fields": sorted(set(mismatches))}],
        )

    evaluation_locked_versions: dict[str, Any] = {
        **scene_lock,
        "eval_dataset_version_id": dataset_id,
        "eval_dataset_manifest_sha256": dataset.manifest_sha256,
        "eval_dataset_snapshot_sha256": dataset_snapshot["snapshot_sha256"],
        "eval_dataset_manifest_storage_object_id": dataset.manifest_storage_object_id,
        "eval_dataset_resource_version": dataset.resource_version,
        "label_version_id": label_version_id,
        "label_resource_version": label_version.resource_version,
        "prompt_version_id": prompt_version_id,
        "prompt_content_sha256": prompt_version.content_sha256,
        "model_version": model_version,
        "aggregation_policy_version_id": policy_version_id,
        "aggregation_policy_sha256": policy.canonical_sha256,
        "optimization_run_id": optimization_run_id,
        "optimization_run_lock_sha256": _canonical_sha256(
            _optimization_lock_document(optimization_payload)
        ),
        "evaluation_suites": list(_LABEL_EVAL_SUITES),
    }
    enriched = {
        **payload,
        # The evaluation request keeps its own action trace. This immutable
        # domain root lets completion receipts and result materializations link
        # back to the LabelVersion without trusting a client-supplied trace.
        "root_trace_id": label_root_trace_id,
        "dataset_id": dataset_id,
        "eval_dataset_version_id": dataset_id,
        "evaluation_suites": list(_LABEL_EVAL_SUITES),
        "locked_versions": evaluation_locked_versions,
        "binding_sha256": _canonical_sha256(evaluation_locked_versions),
        "stage": "evaluating",
        "business_status": "evaluating",
        "release_gate": {
            "hidden_holdout_only": True,
            "paired_bootstrap_confidence": 0.95,
            "min_macro_f1_gain_pp": 2.0,
            "critical_recall_noninferiority_pp": 0.5,
            "json_valid_rate_min": 0.995,
            "coverage_min": 0.95,
            "conflict_rate_max": 0.05,
            "cost_ratio_max": 1.10,
        },
    }
    return revalidate_labeling_eval_manifest(session, ctx, enriched)
