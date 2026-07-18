from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import Select, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    EvalDatasetVersion,
    HumanReviewDecision,
    JsonResource,
    LabelAggregationPolicyVersion,
    LabelEvalResult,
    LabelVersion,
    PromptAsset,
    PromptVersion,
    PromptVersionCandidate,
    ReleaseBundleHead,
    ReleaseBundleHeadEvent,
    ReleaseCommand,
    ReleaseDeployment,
    RunRecord,
)
from app.schemas.label_closed_loop import (
    LabelVersionEvaluationLockRequest,
    PromptAssetCreateRequest,
    PromptVersionCreateRequest,
    ReleaseDeploymentCreateRequest,
    ReleaseHeadBootstrapRequest,
    ReleaseMonitorSampleRequest,
    ReleaseTransitionRequest,
)
from app.services.audit_service import record_audit
from app.services.eval_binding_service import revalidate_labeling_eval_manifest
from app.services.eval_dataset_service import locked_eval_dataset_snapshot
from app.services.label_eval_result_service import label_eval_result_integrity_blockers
from app.services.label_lifecycle_compat_service import (
    LabelLifecycleDriftError,
    transition_label_version_artifact,
)
from app.services.outbox_service import enqueue_event

_Model = TypeVar("_Model")
_EVAL_SUCCESS_STATUSES = frozenset({"success", "completed"})
_LABEL_LOCKED_STATUSES = frozenset({"published", "approved", "locked", "validated"})
_POLICY_ACTIVE_STATUSES = frozenset({"active", "published", "approved"})
_ROLLBACK_TARGET_STATUSES = frozenset({"completed"})
_SCENE_LOCK_FIELDS = (
    "scene_profile_id",
    "scene_profile_version_id",
    "scene_profile_snapshot_sha256",
)


def _canonical_sha256(value: Any) -> str:
    document = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _strict_canonical_sha256(value: Any) -> str:
    document = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _eval_scene_lock(eval_run: RunRecord) -> dict[str, str] | None:
    payload = eval_run.payload or {}
    locked = payload.get("locked_versions")
    if not isinstance(locked, dict):
        return None
    lock = {field: str(locked.get(field) or "") for field in _SCENE_LOCK_FIELDS}
    if not all(lock.values()):
        return None
    if any(payload.get(field) != value for field, value in lock.items()):
        return None
    return lock


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _scoped(
    session: Session,
    model: type[_Model],
    identity_column: Any,
    identity: str,
    ctx: RequestContext,
    *,
    code: str,
    message: str,
    for_update: bool = False,
) -> _Model:
    statement: Select[Any] = select(model).where(
        identity_column == identity,
        model.tenant_id == ctx.tenant_id,  # type: ignore[attr-defined]
        model.project_id == ctx.project_id,  # type: ignore[attr-defined]
    )
    if for_update:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        # Scoped 404 deliberately avoids leaking that an ID exists in another project.
        raise ApiError(code, message, 404)
    return item


def _label_version(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    *,
    for_update: bool = False,
) -> LabelVersion:
    return _scoped(
        session,
        LabelVersion,
        LabelVersion.label_version_id,
        label_version_id,
        ctx,
        code="LABEL_VERSION_NOT_FOUND",
        message="标签版本不存在",
        for_update=for_update,
    )


def _prompt_asset(
    session: Session, ctx: RequestContext, prompt_asset_id: str, *, for_update: bool = False
) -> PromptAsset:
    return _scoped(
        session,
        PromptAsset,
        PromptAsset.prompt_asset_id,
        prompt_asset_id,
        ctx,
        code="PROMPT_ASSET_NOT_FOUND",
        message="Prompt 资产不存在",
        for_update=for_update,
    )


def _prompt_version(
    session: Session, ctx: RequestContext, prompt_version_id: str, *, for_update: bool = False
) -> PromptVersion:
    return _scoped(
        session,
        PromptVersion,
        PromptVersion.prompt_version_id,
        prompt_version_id,
        ctx,
        code="PROMPT_VERSION_NOT_FOUND",
        message="Prompt 版本不存在",
        for_update=for_update,
    )


def _deployment(
    session: Session, ctx: RequestContext, deployment_id: str, *, for_update: bool = False
) -> ReleaseDeployment:
    return _scoped(
        session,
        ReleaseDeployment,
        ReleaseDeployment.deployment_id,
        deployment_id,
        ctx,
        code="RELEASE_DEPLOYMENT_NOT_FOUND",
        message="发布部署不存在",
        for_update=for_update,
    )


def _release_head(
    session: Session,
    ctx: RequestContext,
    environment: str,
    *,
    for_update: bool = False,
) -> ReleaseBundleHead | None:
    statement = select(ReleaseBundleHead).where(
        ReleaseBundleHead.tenant_id == ctx.tenant_id,
        ReleaseBundleHead.project_id == ctx.project_id,
        ReleaseBundleHead.environment == environment,
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _active_release_command(
    session: Session,
    ctx: RequestContext,
    deployment_id: str,
    *,
    for_update: bool = False,
) -> ReleaseCommand | None:
    statement = select(ReleaseCommand).where(
        ReleaseCommand.tenant_id == ctx.tenant_id,
        ReleaseCommand.project_id == ctx.project_id,
        ReleaseCommand.deployment_id == deployment_id,
        ReleaseCommand.active_slot == "active",
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def prompt_asset_data(asset: PromptAsset) -> dict[str, Any]:
    return {
        "prompt_asset_id": asset.prompt_asset_id,
        "name": asset.name,
        "capability": asset.capability,
        "label_version_id": asset.label_version_id,
        "status": asset.status,
        "current_version_id": asset.current_version_id,
        "trace_id": asset.trace_id,
        "payload": asset.payload or {},
        "created_at": _iso(asset.created_at),
        "updated_at": _iso(asset.updated_at),
    }


def prompt_version_data(version: PromptVersion) -> dict[str, Any]:
    return {
        "prompt_version_id": version.prompt_version_id,
        "prompt_asset_id": version.prompt_asset_id,
        "version": version.version,
        "parent_version_id": version.parent_version_id,
        "label_version_id": version.label_version_id,
        "schema_version": version.schema_version,
        "model_version": version.model_version,
        "status": version.status,
        "template": version.template_json,
        "output_schema": version.output_schema,
        "generation_params": version.generation_params or {},
        "structured_diff": version.structured_diff or {},
        "source_badcase_refs": version.source_badcase_refs or [],
        "content_sha256": version.content_sha256,
        "trace_id": version.trace_id,
        "created_at": _iso(version.created_at),
        "updated_at": _iso(version.updated_at),
    }


def release_deployment_data(deployment: ReleaseDeployment) -> dict[str, Any]:
    payload = deployment.payload or {}
    return {
        "deployment_id": deployment.deployment_id,
        "environment": deployment.environment,
        "status": deployment.status,
        "stage": deployment.stage,
        "label_version_id": deployment.label_version_id,
        "prompt_version_id": deployment.prompt_version_id,
        "model_version": deployment.model_version,
        "aggregation_policy_version_id": deployment.aggregation_policy_version_id,
        "eval_dataset_version_id": deployment.eval_dataset_version_id,
        "eval_run_id": deployment.eval_run_id,
        "rollback_target_deployment_id": deployment.rollback_target_deployment_id,
        "bundle_sha256": deployment.bundle_sha256,
        "rollout_percentage": deployment.rollout_percentage,
        "blocked_reasons": deployment.blocked_reasons or [],
        "monitor_metrics": deployment.monitor_metrics or {},
        "approved_by": deployment.approved_by,
        "trace_id": deployment.trace_id,
        "pending_command_id": payload.get("pending_command_id"),
        "pending_run_id": payload.get("pending_run_id"),
        "pending_action": payload.get("pending_action"),
        "payload": payload,
        "created_at": _iso(deployment.created_at),
        "updated_at": _iso(deployment.updated_at),
    }


def release_command_data(command: ReleaseCommand) -> dict[str, Any]:
    return {
        "command_id": command.command_id,
        "deployment_id": command.deployment_id,
        "target_deployment_id": command.target_deployment_id,
        "environment": command.environment,
        "action": command.action,
        "status": command.status,
        "run_id": command.run_id,
        "expected_deployment_status": command.expected_deployment_status,
        "expected_head_generation": command.expected_head_generation,
        "expected_head_deployment_id": command.expected_head_deployment_id,
        "expected_head_bundle_sha256": command.expected_head_bundle_sha256,
        "command_sha256": command.command_sha256,
        "requested_by": command.requested_by,
        "completed_by_source": command.completed_by_source,
        "completion_receipt_id": command.completion_receipt_id,
        "trace_id": command.trace_id,
        "payload": command.payload or {},
        "created_at": _iso(command.created_at),
        "updated_at": _iso(command.updated_at),
    }


def release_head_data(head: ReleaseBundleHead) -> dict[str, Any]:
    return {
        "release_head_id": head.release_head_id,
        "environment": head.environment,
        "active_deployment_id": head.active_deployment_id,
        "active_bundle_sha256": head.active_bundle_sha256,
        "prompt_asset_id": head.prompt_asset_id,
        "prompt_version_id": head.prompt_version_id,
        "label_version_id": head.label_version_id,
        "model_version": head.model_version,
        "aggregation_policy_version_id": head.aggregation_policy_version_id,
        "eval_dataset_version_id": head.eval_dataset_version_id,
        "generation": head.generation,
        "status": head.status,
        "bootstrapped": head.bootstrapped,
        "activated_by_command_id": head.activated_by_command_id,
        "trace_id": head.trace_id,
        "payload": head.payload or {},
        "created_at": _iso(head.created_at),
        "updated_at": _iso(head.updated_at),
    }


def release_head_event_data(event: ReleaseBundleHeadEvent) -> dict[str, Any]:
    return {
        "head_event_id": event.head_event_id,
        "environment": event.environment,
        "generation": event.generation,
        "previous_generation": event.previous_generation,
        "action": event.action,
        "activation_status": event.activation_status,
        "old_deployment_id": event.old_deployment_id,
        "new_deployment_id": event.new_deployment_id,
        "old_label_version_id": event.old_label_version_id,
        "new_label_version_id": event.new_label_version_id,
        "old_bundle_sha256": event.old_bundle_sha256,
        "new_bundle_sha256": event.new_bundle_sha256,
        "effective_from": _iso(event.effective_from),
        "effective_to": _iso(event.effective_to),
        "command_id": event.command_id,
        "completion_receipt_id": event.completion_receipt_id,
        "approval_id": event.approval_id,
        "content_sha256": event.content_sha256,
        "actor_id": event.actor_id,
        "root_trace_id": event.root_trace_id,
        "trace_id": event.trace_id,
        "payload": event.payload or {},
        "created_at": _iso(event.created_at),
    }


def release_head_event_hash_document(event: ReleaseBundleHeadEvent) -> dict[str, Any]:
    payload = event.payload or {}
    effective_from = str(payload.get("canonical_effective_from") or _iso(event.effective_from))
    return {
        "tenant_id": event.tenant_id,
        "project_id": event.project_id,
        "environment": event.environment,
        "generation": event.generation,
        "previous_generation": event.previous_generation,
        "action": event.action,
        "activation_status": event.activation_status,
        "old_deployment_id": event.old_deployment_id,
        "new_deployment_id": event.new_deployment_id,
        "old_label_version_id": event.old_label_version_id,
        "new_label_version_id": event.new_label_version_id,
        "old_bundle_sha256": event.old_bundle_sha256,
        "new_bundle_sha256": event.new_bundle_sha256,
        "effective_from": effective_from,
        "effective_to": _iso(event.effective_to),
        "command_id": event.command_id,
        "completion_receipt_id": event.completion_receipt_id,
        "approval_id": event.approval_id,
        "actor_id": event.actor_id,
        "root_trace_id": event.root_trace_id,
        "trace_id": event.trace_id,
        "payload": payload,
    }


def release_head_event_content_sha256(event: ReleaseBundleHeadEvent) -> str:
    return _strict_canonical_sha256(release_head_event_hash_document(event))


def get_release_bundle_head(
    session: Session,
    ctx: RequestContext,
    environment: str,
) -> dict[str, Any]:
    head = _release_head(session, ctx, environment)
    if head is None:
        raise ApiError(
            "RELEASE_HEAD_NOT_FOUND",
            "当前环境尚未建立 active Bundle head",
            404,
        )
    events = list(
        session.scalars(
            select(ReleaseBundleHeadEvent)
            .where(
                ReleaseBundleHeadEvent.tenant_id == ctx.tenant_id,
                ReleaseBundleHeadEvent.project_id == ctx.project_id,
                ReleaseBundleHeadEvent.environment == environment,
            )
            .order_by(ReleaseBundleHeadEvent.generation)
        )
    )
    data = release_head_data(head)
    last = events[-1] if events else None
    ledger_consistent = bool(
        last is not None
        and last.generation == head.generation
        and last.new_deployment_id == head.active_deployment_id
        and last.new_bundle_sha256 == head.active_bundle_sha256
        and last.new_label_version_id == head.label_version_id
    )
    data["activation_timeline"] = [release_head_event_data(event) for event in events]
    data["ledger_health"] = {
        "status": "consistent" if ledger_consistent else "drift",
        "head_generation": head.generation,
        "last_event_generation": last.generation if last is not None else None,
    }
    return data


def lock_label_version_for_evaluation(
    session: Session,
    ctx: RequestContext,
    label_version_id: str,
    body: LabelVersionEvaluationLockRequest,
) -> dict[str, Any]:
    """Freeze the complete labeling bundle before an EvalRun can be created."""

    label = _label_version(session, ctx, label_version_id, for_update=True)
    if label.resource_version != body.expected_resource_version:
        raise ApiError(
            "LABEL_VERSION_CONFLICT",
            "标签版本已被其他操作更新，请刷新后重试",
            409,
            details=[
                {
                    "expected_resource_version": body.expected_resource_version,
                    "actual_resource_version": label.resource_version,
                }
            ],
        )

    prompt = _prompt_version(session, ctx, body.prompt_version_id, for_update=True)
    if prompt.label_version_id != label_version_id or prompt.model_version not in {
        None,
        body.model_version,
    }:
        raise ApiError(
            "LABEL_EVALUATION_PROMPT_BINDING_MISMATCH",
            "PromptVersion 与待锁定标签或模型版本不一致",
            409,
        )
    approval_blockers = _prompt_approval_evidence_blockers(session, prompt)
    if approval_blockers:
        raise ApiError(
            "LABEL_EVALUATION_PROMPT_NOT_APPROVED",
            "标签版本锁定前必须完成 Prompt 双盲审批",
            409,
            details=approval_blockers,
        )

    policy = _scoped(
        session,
        LabelAggregationPolicyVersion,
        LabelAggregationPolicyVersion.policy_version_id,
        body.aggregation_policy_version_id,
        ctx,
        code="LABEL_AGGREGATION_POLICY_NOT_FOUND",
        message="标签聚合策略不存在",
    )
    if policy.label_version_id != label_version_id or policy.status not in _POLICY_ACTIVE_STATUSES:
        raise ApiError(
            "LABEL_EVALUATION_POLICY_NOT_ACTIVE",
            "标签版本只能绑定当前版本的 active 聚合策略",
            409,
            details=[{"status": policy.status, "label_version_id": policy.label_version_id}],
        )

    dataset = _scoped(
        session,
        EvalDatasetVersion,
        EvalDatasetVersion.eval_dataset_id,
        body.eval_dataset_version_id,
        ctx,
        code="EVAL_DATASET_NOT_FOUND",
        message="评测集版本不存在",
    )
    if dataset.status != "locked" or dataset.capability != "labeling":
        raise ApiError(
            "LABEL_EVALUATION_DATASET_NOT_LOCKED",
            "标签版本只能绑定已锁定的 labeling 评测集",
            409,
            details=[{"status": dataset.status, "capability": dataset.capability}],
        )
    dataset_snapshot = locked_eval_dataset_snapshot(
        session,
        ctx,
        dataset.eval_dataset_id,
        required_capability="labeling",
    )

    optimization_run = _scoped(
        session,
        RunRecord,
        RunRecord.run_id,
        body.optimization_run_id,
        ctx,
        code="LABEL_OPTIMIZATION_RUN_NOT_FOUND",
        message="标签优化运行不存在",
    )
    optimization_payload = optimization_run.payload or {}
    expected_bindings = {
        "label_version_id": label_version_id,
        "model_version": body.model_version,
        "aggregation_policy_version_id": body.aggregation_policy_version_id,
        "eval_dataset_version_id": body.eval_dataset_version_id,
    }
    prompt_ids = {
        str(optimization_payload.get("prompt_version_id") or ""),
        *{str(item) for item in optimization_payload.get("prompt_candidate_ids", []) if item},
    }
    mismatches = [
        field
        for field, expected in expected_bindings.items()
        if optimization_payload.get(field) != expected
    ]
    if body.prompt_version_id not in prompt_ids:
        mismatches.append("prompt_version_id")
    if optimization_run.run_type != "label_optimization" or optimization_run.status != "success":
        mismatches.append("optimization_run.status")
    if mismatches:
        raise ApiError(
            "LABEL_EVALUATION_OPTIMIZATION_BINDING_MISMATCH",
            "标签版本锁定 Bundle 与已物化优化运行不一致",
            409,
            details=[{"fields": sorted(set(mismatches))}],
        )

    current_lock = (label.payload or {}).get("evaluation_lock")
    lock_source_resource_version = label.resource_version
    if label.status == "locked":
        if not isinstance(current_lock, dict) or not isinstance(
            current_lock.get("label_resource_version"), int
        ):
            raise ApiError(
                "LABEL_EVALUATION_LOCK_DRIFT",
                "标签版本已锁定但缺少可验证的评测 Bundle，必须创建新版本",
                409,
            )
        lock_source_resource_version = current_lock["label_resource_version"]

    lock_document = {
        "schema_version": "label-evaluation-lock/v1",
        "label_version_id": label_version_id,
        "label_resource_version": lock_source_resource_version,
        "prompt_version_id": prompt.prompt_version_id,
        "prompt_content_sha256": prompt.content_sha256,
        "model_version": body.model_version,
        "aggregation_policy_version_id": policy.policy_version_id,
        "aggregation_policy_sha256": policy.canonical_sha256,
        "eval_dataset_version_id": dataset.eval_dataset_id,
        "eval_dataset_manifest_sha256": dataset.manifest_sha256,
        "eval_dataset_snapshot_sha256": dataset_snapshot["snapshot_sha256"],
        "optimization_run_id": optimization_run.run_id,
        "optimization_lock_sha256": _canonical_sha256(
            {
                "label_version_id": optimization_payload.get("label_version_id"),
                "prompt_version_id": optimization_payload.get("prompt_version_id"),
                "prompt_candidate_ids": sorted(
                    str(item)
                    for item in optimization_payload.get("prompt_candidate_ids", [])
                    if item
                ),
                "model_version": optimization_payload.get("model_version"),
                "aggregation_policy_version_id": optimization_payload.get(
                    "aggregation_policy_version_id"
                ),
                "eval_dataset_version_id": optimization_payload.get("eval_dataset_version_id"),
                "trigger_hash": optimization_payload.get("trigger_hash"),
            }
        ),
    }
    snapshot_sha256 = _canonical_sha256(lock_document)
    if label.status == "locked":
        if (
            not isinstance(current_lock, dict)
            or current_lock.get("snapshot_sha256") != snapshot_sha256
        ):
            raise ApiError(
                "LABEL_EVALUATION_LOCK_DRIFT",
                "标签版本已锁定到不同评测 Bundle，必须创建新版本",
                409,
            )
        try:
            transition_label_version_artifact(label, "locked")
        except LabelLifecycleDriftError as exc:
            raise ApiError("LABEL_VERSION_STRONG_FIELD_DRIFT", str(exc), 409) from exc
        return {
            "label_version_id": label.label_version_id,
            "status": label.status,
            "resource_version": label.resource_version,
            **current_lock,
            "materialized": False,
            "trace_id": label.trace_id,
            "next_action": "create-eval-run",
        }
    if label.status not in {"draft", "candidate", "validated"}:
        raise ApiError(
            "LABEL_EVALUATION_LOCK_STATE_INVALID",
            "当前标签版本状态不能锁定待评测",
            409,
            details=[{"status": label.status}],
        )

    before = {"status": label.status, "resource_version": label.resource_version}
    now = datetime.now(UTC).isoformat()
    try:
        transition_label_version_artifact(label, "locked")
    except LabelLifecycleDriftError as exc:
        raise ApiError("LABEL_VERSION_STRONG_FIELD_DRIFT", str(exc), 409) from exc
    label.status = "locked"
    label.resource_version += 1
    label.trace_id = ctx.trace_id
    evaluation_lock = {
        **lock_document,
        "snapshot_sha256": snapshot_sha256,
        "locked_at": now,
        "locked_by": ctx.user_id,
    }
    label.payload = {
        **(label.payload or {}),
        "status": "locked",
        "artifact_status": "locked",
        "resource_version": label.resource_version,
        "evaluation_lock": evaluation_lock,
        "trace_id": ctx.trace_id,
    }
    projection = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "label_versions",
            JsonResource.resource_key == label_version_id,
        )
    )
    if projection is None:
        raise ApiError(
            "LABEL_VERSION_PROJECTION_MISSING",
            "标签版本强表与业务投影不一致",
            409,
        )
    projection.data = dict(label.payload)
    projection.status = "locked"
    projection.trace_id = ctx.trace_id
    record_audit(
        session,
        ctx,
        action="label_version.evaluation_locked",
        object_type="label_version",
        object_id=label_version_id,
        before=before,
        after={
            "status": "locked",
            "resource_version": label.resource_version,
            "snapshot_sha256": snapshot_sha256,
            "prompt_version_id": prompt.prompt_version_id,
            "optimization_run_id": optimization_run.run_id,
        },
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_version.evaluation_locked",
        aggregate_type="label_version",
        aggregate_id=label_version_id,
        payload={
            "label_version_id": label_version_id,
            "status": "locked",
            "resource_version": label.resource_version,
            "evaluation_lock": evaluation_lock,
        },
    )
    return {
        "label_version_id": label.label_version_id,
        "status": label.status,
        "resource_version": label.resource_version,
        **evaluation_lock,
        "materialized": True,
        "trace_id": ctx.trace_id,
        "next_action": "create-eval-run",
    }


def create_prompt_asset(
    session: Session, ctx: RequestContext, body: PromptAssetCreateRequest
) -> dict[str, Any]:
    if body.label_version_id is not None:
        _label_version(session, ctx, body.label_version_id)
    duplicate_id = session.scalar(
        select(PromptAsset.prompt_asset_id).where(
            PromptAsset.prompt_asset_id == body.prompt_asset_id
        )
    )
    if duplicate_id is not None:
        raise ApiError("PROMPT_ASSET_ID_CONFLICT", "Prompt 资产 ID 已存在", 409)
    duplicate_name = session.scalar(
        select(PromptAsset.prompt_asset_id).where(
            PromptAsset.tenant_id == ctx.tenant_id,
            PromptAsset.project_id == ctx.project_id,
            PromptAsset.capability == body.capability,
            PromptAsset.name == body.name,
        )
    )
    if duplicate_name is not None:
        raise ApiError("PROMPT_ASSET_NAME_CONFLICT", "同能力下 Prompt 资产名称已存在", 409)

    asset = PromptAsset(
        prompt_asset_id=body.prompt_asset_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        name=body.name.strip(),
        capability=body.capability,
        label_version_id=body.label_version_id,
        status="active",
        current_version_id=None,
        trace_id=ctx.trace_id,
        payload={"created_by": ctx.user_id},
    )
    session.add(asset)
    session.flush()
    data = prompt_asset_data(asset)
    record_audit(
        session,
        ctx,
        action="prompt_asset.create",
        object_type="prompt_asset",
        object_id=asset.prompt_asset_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="prompt_asset.created",
        aggregate_type="prompt_asset",
        aggregate_id=asset.prompt_asset_id,
        payload=data,
    )
    return data


def get_prompt_asset(session: Session, ctx: RequestContext, prompt_asset_id: str) -> dict[str, Any]:
    return prompt_asset_data(_prompt_asset(session, ctx, prompt_asset_id))


def list_prompt_assets(
    session: Session,
    ctx: RequestContext,
    *,
    capability: str | None = None,
    label_version_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(PromptAsset).where(
        PromptAsset.tenant_id == ctx.tenant_id,
        PromptAsset.project_id == ctx.project_id,
    )
    if capability is not None:
        statement = statement.where(PromptAsset.capability == capability)
    if label_version_id is not None:
        statement = statement.where(PromptAsset.label_version_id == label_version_id)
    if status is not None:
        statement = statement.where(PromptAsset.status == status)
    rows = session.scalars(
        statement.order_by(PromptAsset.created_at.desc(), PromptAsset.prompt_asset_id).limit(limit)
    )
    return [prompt_asset_data(item) for item in rows]


def create_prompt_version(
    session: Session, ctx: RequestContext, body: PromptVersionCreateRequest
) -> dict[str, Any]:
    asset = _prompt_asset(session, ctx, body.prompt_asset_id)
    effective_label_version_id = body.label_version_id or asset.label_version_id
    if asset.label_version_id is not None and effective_label_version_id != asset.label_version_id:
        raise ApiError(
            "PROMPT_LABEL_SCOPE_MISMATCH",
            "Prompt 版本标签范围必须与资产一致",
            409,
        )
    if effective_label_version_id is not None:
        _label_version(session, ctx, effective_label_version_id)

    parent: PromptVersion | None = None
    if body.parent_version_id is not None:
        parent = _prompt_version(session, ctx, body.parent_version_id)
        if (
            parent.prompt_asset_id != asset.prompt_asset_id
            or parent.label_version_id != effective_label_version_id
        ):
            raise ApiError(
                "PROMPT_PARENT_SCOPE_MISMATCH",
                "父 Prompt 版本必须属于同一资产和标签范围",
                409,
            )

    duplicate_id = session.scalar(
        select(PromptVersion.prompt_version_id).where(
            PromptVersion.prompt_version_id == body.prompt_version_id
        )
    )
    if duplicate_id is not None:
        raise ApiError("PROMPT_VERSION_ID_CONFLICT", "Prompt 版本 ID 已存在", 409)
    duplicate_version = session.scalar(
        select(PromptVersion.prompt_version_id).where(
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
            PromptVersion.prompt_asset_id == asset.prompt_asset_id,
            PromptVersion.version == body.version,
        )
    )
    if duplicate_version is not None:
        raise ApiError("PROMPT_VERSION_CONFLICT", "Prompt 版本号已存在", 409)

    content_document = {
        "prompt_asset_id": asset.prompt_asset_id,
        "parent_version_id": body.parent_version_id,
        "label_version_id": effective_label_version_id,
        "schema_version": body.schema_version,
        "model_version": body.model_version,
        "template": body.template,
        "output_schema": body.output_schema,
        "generation_params": body.generation_params,
    }
    content_sha256 = _canonical_sha256(content_document)
    duplicate_content = session.scalar(
        select(PromptVersion.prompt_version_id).where(
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
            PromptVersion.content_sha256 == content_sha256,
        )
    )
    if duplicate_content is not None:
        raise ApiError(
            "PROMPT_CONTENT_CONFLICT",
            "相同 Prompt 内容已存在，请复用已有强版本",
            409,
            details=[{"prompt_version_id": duplicate_content}],
        )

    version = PromptVersion(
        prompt_version_id=body.prompt_version_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        prompt_asset_id=asset.prompt_asset_id,
        version=body.version.strip(),
        parent_version_id=parent.prompt_version_id if parent is not None else None,
        label_version_id=effective_label_version_id,
        schema_version=body.schema_version.strip(),
        model_version=body.model_version,
        status="draft",
        template_json=body.template,
        output_schema=body.output_schema,
        generation_params=body.generation_params,
        structured_diff=body.structured_diff,
        source_badcase_refs=body.source_badcase_refs,
        content_sha256=content_sha256,
        trace_id=ctx.trace_id,
    )
    session.add(version)
    session.flush()
    data = prompt_version_data(version)
    record_audit(
        session,
        ctx,
        action="prompt_version.create",
        object_type="prompt_version",
        object_id=version.prompt_version_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="prompt_version.created",
        aggregate_type="prompt_version",
        aggregate_id=version.prompt_version_id,
        payload=data,
    )
    return data


def get_prompt_version(
    session: Session, ctx: RequestContext, prompt_version_id: str
) -> dict[str, Any]:
    return prompt_version_data(_prompt_version(session, ctx, prompt_version_id))


def list_prompt_versions(
    session: Session,
    ctx: RequestContext,
    *,
    prompt_asset_id: str | None = None,
    label_version_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(PromptVersion).where(
        PromptVersion.tenant_id == ctx.tenant_id,
        PromptVersion.project_id == ctx.project_id,
    )
    if prompt_asset_id is not None:
        statement = statement.where(PromptVersion.prompt_asset_id == prompt_asset_id)
    if label_version_id is not None:
        statement = statement.where(PromptVersion.label_version_id == label_version_id)
    if status is not None:
        statement = statement.where(PromptVersion.status == status)
    rows = session.scalars(
        statement.order_by(PromptVersion.created_at.desc(), PromptVersion.prompt_version_id).limit(
            limit
        )
    )
    return [prompt_version_data(item) for item in rows]


def _scoped_policy(
    session: Session, ctx: RequestContext, policy_version_id: str
) -> LabelAggregationPolicyVersion:
    return _scoped(
        session,
        LabelAggregationPolicyVersion,
        LabelAggregationPolicyVersion.policy_version_id,
        policy_version_id,
        ctx,
        code="LABEL_AGGREGATION_POLICY_NOT_FOUND",
        message="标签聚合策略版本不存在",
    )


def _scoped_dataset(
    session: Session, ctx: RequestContext, dataset_version_id: str
) -> EvalDatasetVersion:
    return _scoped(
        session,
        EvalDatasetVersion,
        EvalDatasetVersion.eval_dataset_id,
        dataset_version_id,
        ctx,
        code="EVAL_DATASET_VERSION_NOT_FOUND",
        message="评测集版本不存在",
    )


def _scoped_run(session: Session, ctx: RequestContext, run_id: str) -> RunRecord:
    return _scoped(
        session,
        RunRecord,
        RunRecord.run_id,
        run_id,
        ctx,
        code="EVAL_RUN_NOT_FOUND",
        message="评测运行不存在",
    )


def _binding(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _blocked(code: str, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if details:
        result["details"] = details
    return result


def _json_resource(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    collection: str,
    resource_key: str,
) -> JsonResource | None:
    return session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == tenant_id,
            JsonResource.project_id == project_id,
            JsonResource.collection == collection,
            JsonResource.resource_key == resource_key,
        )
    )


def _prompt_approval_evidence_blockers(
    session: Session,
    prompt: PromptVersion,
) -> list[dict[str, Any]]:
    if prompt.status not in {"approved", "published"}:
        return [
            _blocked(
                "PROMPT_DOUBLE_BLIND_APPROVAL_REQUIRED",
                "Prompt 必须先完成双盲一致或独立仲裁，发布状态机不能代替审批",
                status=prompt.status,
            )
        ]

    candidate = session.scalar(
        select(PromptVersionCandidate).where(
            PromptVersionCandidate.candidate_id == prompt.prompt_version_id,
            PromptVersionCandidate.tenant_id == prompt.tenant_id,
            PromptVersionCandidate.project_id == prompt.project_id,
        )
    )
    if candidate is None or candidate.status != "approved":
        return [
            _blocked(
                "PROMPT_APPROVAL_EVIDENCE_MISSING",
                "Prompt approved/published 状态缺少 approved 候选强投影",
            )
        ]
    payload = candidate.payload or {}
    submission_ids = payload.get("review_submission_ids")
    review_task_id = payload.get("review_task_id")
    decision_id = payload.get("review_decision_id")
    resolution_source = payload.get("review_resolution_source")
    if (
        payload.get("received_reviews") != 2
        or not isinstance(submission_ids, list)
        or len(submission_ids) != 2
        or len(set(submission_ids)) != 2
        or not isinstance(review_task_id, str)
        or not review_task_id
        or not isinstance(decision_id, str)
        or not decision_id
        or resolution_source not in {"reviewer-consensus", "adjudication"}
    ):
        return [
            _blocked(
                "PROMPT_APPROVAL_EVIDENCE_MISSING",
                "Prompt 审批缺少两份密封审核、终态决策或解析来源",
            )
        ]

    submissions: list[dict[str, Any]] = []
    for submission_id in submission_ids:
        if not isinstance(submission_id, str) or not submission_id:
            continue
        resource = _json_resource(
            session,
            tenant_id=prompt.tenant_id,
            project_id=prompt.project_id,
            collection="prompt_review_submissions",
            resource_key=submission_id,
        )
        if resource is None:
            continue
        submission = resource.data or {}
        if (
            resource.status != "sealed"
            or submission.get("status") != "sealed"
            or submission.get("candidate_id") != prompt.prompt_version_id
            or submission.get("submission_id") != submission_id
        ):
            continue
        submissions.append(submission)
    reviewer_ids = {str(item.get("reviewer_id") or "") for item in submissions}
    if len(submissions) != 2 or "" in reviewer_ids or len(reviewer_ids) != 2:
        return [
            _blocked(
                "PROMPT_APPROVAL_EVIDENCE_INVALID",
                "Prompt 双盲证据必须来自两个不同审核人且均为密封记录",
            )
        ]

    decision = session.scalar(
        select(HumanReviewDecision).where(
            HumanReviewDecision.decision_id == decision_id,
            HumanReviewDecision.tenant_id == prompt.tenant_id,
            HumanReviewDecision.project_id == prompt.project_id,
            HumanReviewDecision.review_task_id == review_task_id,
        )
    )
    decision_payload = decision.payload if decision is not None else {}
    if (
        decision is None
        or decision.status != "success"
        or decision_payload.get("decision") != "accepted"
        or decision_payload.get("source") != resolution_source
        or decision_payload.get("submission_ids") != submission_ids
    ):
        return [
            _blocked(
                "PROMPT_APPROVAL_DECISION_INVALID",
                "Prompt 双盲审批缺少与密封提交一致的 accepted 终态决策",
            )
        ]

    if resolution_source == "reviewer-consensus":
        if any(item.get("decision") != "accepted" for item in submissions):
            return [
                _blocked(
                    "PROMPT_APPROVAL_CONSENSUS_INVALID",
                    "双盲一致审批要求两名审核人均独立接受候选",
                )
            ]
        if payload.get("adjudication_id") is not None:
            return [
                _blocked(
                    "PROMPT_APPROVAL_CONSENSUS_INVALID",
                    "双盲一致审批不能同时引用仲裁记录",
                )
            ]
    else:
        adjudication_id = payload.get("adjudication_id")
        if not isinstance(adjudication_id, str) or not adjudication_id:
            return [
                _blocked(
                    "PROMPT_APPROVAL_ADJUDICATION_MISSING",
                    "存在分歧的 Prompt 审批必须绑定独立仲裁",
                )
            ]
        adjudication = _json_resource(
            session,
            tenant_id=prompt.tenant_id,
            project_id=prompt.project_id,
            collection="prompt_review_adjudications",
            resource_key=adjudication_id,
        )
        adjudication_payload = adjudication.data if adjudication is not None else {}
        adjudicator_id = str(adjudication_payload.get("adjudicator_id") or "")
        if (
            adjudication is None
            or adjudication.status != "resolved"
            or adjudication_payload.get("candidate_id") != prompt.prompt_version_id
            or adjudication_payload.get("submission_ids") != submission_ids
            or adjudication_payload.get("decision") != "accepted"
            or not adjudicator_id
            or adjudicator_id in reviewer_ids
            or decision_payload.get("adjudication_id") != adjudication_id
        ):
            return [
                _blocked(
                    "PROMPT_APPROVAL_ADJUDICATION_INVALID",
                    "Prompt 仲裁必须独立、终态为 accepted 且绑定同一双盲提交",
                )
            ]
    return []


def _rollback_target_blockers(
    target: ReleaseDeployment | None,
    *,
    environment: str,
    allow_superseded: bool = False,
) -> list[dict[str, Any]]:
    if target is None:
        return []
    target_bundle = (target.payload or {}).get("bundle")
    target_bundle_sha256 = (
        _canonical_sha256(target_bundle) if isinstance(target_bundle, dict) else None
    )
    stable_status = target.status == "completed" and target.rollout_percentage == 100
    superseded_status = (
        allow_superseded and target.status == "superseded" and target.rollout_percentage == 0
    )
    if (
        target.environment != environment
        or not (stable_status or superseded_status)
        or target.stage not in {"completed", "superseded"}
        or not isinstance(target_bundle, dict)
        or target_bundle_sha256 != target.bundle_sha256
        or len(target.bundle_sha256 or "") != 64
    ):
        return [
            _blocked(
                "ROLLBACK_TARGET_NOT_STABLE",
                "回滚目标必须是同环境 completed、100% 流量且 Bundle 哈希可重算的稳定部署",
                status=target.status,
                stage=target.stage,
                environment=target.environment,
                rollout_percentage=target.rollout_percentage,
                bundle_sha256=target.bundle_sha256,
                recomputed_bundle_sha256=target_bundle_sha256,
            )
        ]
    return []


def _release_blockers(
    *,
    session: Session,
    ctx: RequestContext,
    body: ReleaseDeploymentCreateRequest,
    label: LabelVersion,
    prompt: PromptVersion,
    policy: LabelAggregationPolicyVersion,
    dataset: EvalDatasetVersion,
    eval_run: RunRecord,
    eval_result: LabelEvalResult | None,
    rollback_target: ReleaseDeployment | None,
    active_head: ReleaseBundleHead | None,
    require_production_rollback: bool = True,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if label.status not in _LABEL_LOCKED_STATUSES:
        blockers.append(
            _blocked("LABEL_VERSION_NOT_LOCKED", "标签版本尚未锁定", status=label.status)
        )
    if prompt.label_version_id != body.label_version_id:
        blockers.append(
            _blocked(
                "PROMPT_LABEL_BINDING_MISMATCH",
                "Prompt 版本未绑定目标标签版本",
                actual=prompt.label_version_id,
                expected=body.label_version_id,
            )
        )
    if prompt.model_version is not None and prompt.model_version != body.model_version:
        blockers.append(
            _blocked(
                "PROMPT_MODEL_BINDING_MISMATCH",
                "Prompt 版本模型绑定不一致",
                actual=prompt.model_version,
                expected=body.model_version,
            )
        )
    blockers.extend(_prompt_approval_evidence_blockers(session, prompt))
    if policy.label_version_id != body.label_version_id:
        blockers.append(
            _blocked(
                "AGGREGATION_POLICY_LABEL_BINDING_MISMATCH",
                "聚合策略未绑定目标标签版本",
                actual=policy.label_version_id,
                expected=body.label_version_id,
            )
        )
    if policy.status not in _POLICY_ACTIVE_STATUSES:
        blockers.append(
            _blocked(
                "AGGREGATION_POLICY_NOT_ACTIVE",
                "聚合策略尚未激活",
                status=policy.status,
            )
        )
    if dataset.status != "locked":
        blockers.append(
            _blocked("EVAL_DATASET_NOT_LOCKED", "评测集版本尚未锁定", status=dataset.status)
        )
    if eval_run.run_type != "eval_run":
        blockers.append(
            _blocked(
                "EVAL_RUN_TYPE_INVALID", "发布 Bundle 必须绑定 EvalRun", run_type=eval_run.run_type
            )
        )
    if eval_run.status not in _EVAL_SUCCESS_STATUSES:
        blockers.append(
            _blocked("EVAL_RUN_NOT_COMPLETED", "评测运行尚未成功完成", status=eval_run.status)
        )
    run_payload = eval_run.payload or {}
    try:
        revalidate_labeling_eval_manifest(session, ctx, run_payload)
    except ApiError as exc:
        blockers.append(
            _blocked(
                "LABEL_EVAL_MANIFEST_DRIFT",
                "EvalRun 锁定 manifest 在发布前重验失败",
                source_error=exc.code,
                drift=exc.details,
            )
        )
    scene_lock = _eval_scene_lock(eval_run)
    if scene_lock is None:
        blockers.append(
            _blocked(
                "SCENE_PROFILE_LOCK_MISSING",
                "EvalRun 缺少一致的 SceneProfile 版本和快照锁",
            )
        )
    else:
        try:
            from app.services.scene_profile_service import (
                assert_active_scene_profile_binding,
            )

            assert_active_scene_profile_binding(session, ctx, **scene_lock)
        except ApiError as exc:
            blockers.append(
                _blocked(
                    "SCENE_PROFILE_RELEASE_MISMATCH",
                    "发布 Bundle 与项目当前激活场景不一致",
                    source_error=exc.code,
                    drift=exc.details,
                )
            )
    if eval_result is None:
        blockers.append(
            _blocked(
                "LABEL_EVAL_RESULT_REQUIRED",
                "发布 Bundle 必须绑定已物化的强类型标签评测结果",
            )
        )
    elif eval_result.status != "passed":
        blockers.append(
            _blocked(
                "LABEL_EVAL_GATES_BLOCKED",
                "离线评测门禁未通过",
                eval_result_id=eval_result.eval_result_id,
                failed_gates=[
                    gate.get("code") for gate in eval_result.gate_results if not gate.get("passed")
                ],
            )
        )
    elif integrity_blockers := label_eval_result_integrity_blockers(session, eval_result):
        blockers.append(
            _blocked(
                "LABEL_EVAL_RESULT_INTEGRITY_BLOCKED",
                "离线评测强结果证据不完整或不可重算",
                failed_checks=[item.get("code") for item in integrity_blockers],
                checks=integrity_blockers,
            )
        )
    elif (
        eval_result.binding_sha256 != run_payload.get("binding_sha256")
        or (run_payload.get("label_eval_result") or {}).get("result_sha256")
        != eval_result.result_sha256
    ):
        blockers.append(
            _blocked(
                "LABEL_EVAL_RESULT_BINDING_MISMATCH",
                "评测强结果与 EvalRun 回执绑定不一致",
            )
        )

    expected_bindings = {
        "eval_dataset_version_id": (
            body.eval_dataset_version_id,
            _binding(run_payload, "eval_dataset_version_id", "eval_dataset_id", "dataset_id"),
        ),
        "label_version_id": (
            body.label_version_id,
            _binding(run_payload, "label_version_id", "label_version"),
        ),
        "prompt_version_id": (
            body.prompt_version_id,
            _binding(run_payload, "prompt_version_id", "prompt_version"),
        ),
        "aggregation_policy_version_id": (
            body.aggregation_policy_version_id,
            _binding(
                run_payload,
                "aggregation_policy_version_id",
                "policy_version_id",
                "aggregation_policy_id",
            ),
        ),
        "model_version": (body.model_version, _binding(run_payload, "model_version")),
    }
    for field, (expected, actual) in expected_bindings.items():
        if actual is None:
            blockers.append(
                _blocked(
                    "EVAL_RUN_BINDING_MISSING",
                    "评测运行缺少强版本绑定",
                    field=field,
                    expected=expected,
                )
            )
        elif str(actual) != str(expected):
            blockers.append(
                _blocked(
                    "EVAL_RUN_BINDING_MISMATCH",
                    "评测运行强版本绑定不一致",
                    field=field,
                    actual=actual,
                    expected=expected,
                )
            )
    if body.environment == "production" and require_production_rollback and rollback_target is None:
        blockers.append(
            _blocked(
                "ROLLBACK_TARGET_REQUIRED",
                "生产发布 Bundle 必须锁定 last-known-good 回滚部署；缺失时只能保持 shadow",
            )
        )
    blockers.extend(_rollback_target_blockers(rollback_target, environment=body.environment))
    if body.environment == "production" and require_production_rollback:
        if active_head is None:
            blockers.append(
                _blocked(
                    "RELEASE_ACTIVE_HEAD_REQUIRED",
                    "生产发布必须先通过受审计 bootstrap 建立唯一 active Bundle head",
                )
            )
        elif rollback_target is not None and (
            active_head.active_deployment_id != rollback_target.deployment_id
            or active_head.active_bundle_sha256 != rollback_target.bundle_sha256
        ):
            blockers.append(
                _blocked(
                    "ROLLBACK_TARGET_NOT_ACTIVE_HEAD",
                    "生产发布回滚目标必须等于当前 active Bundle head",
                    active_deployment_id=active_head.active_deployment_id,
                    rollback_target_deployment_id=rollback_target.deployment_id,
                )
            )
    return blockers


def _release_bundle_document(
    *,
    body: ReleaseDeploymentCreateRequest,
    label: LabelVersion,
    prompt: PromptVersion,
    policy: LabelAggregationPolicyVersion,
    dataset: EvalDatasetVersion,
    dataset_snapshot: dict[str, Any] | None,
    eval_run: RunRecord,
    eval_result: LabelEvalResult | None,
    rollback_target: ReleaseDeployment | None,
) -> dict[str, Any]:
    scene_lock = _eval_scene_lock(eval_run)
    return {
        "environment": body.environment,
        "scene_profile": (
            {
                "id": scene_lock["scene_profile_id"],
                "version_id": scene_lock["scene_profile_version_id"],
                "snapshot_sha256": scene_lock["scene_profile_snapshot_sha256"],
            }
            if scene_lock is not None
            else None
        ),
        "label": {"id": label.label_version_id, "resource_version": label.resource_version},
        "prompt": {"id": prompt.prompt_version_id, "sha256": prompt.content_sha256},
        "model_version": body.model_version,
        "aggregation_policy": {
            "id": policy.policy_version_id,
            "sha256": policy.canonical_sha256,
        },
        "eval_dataset": {
            "id": dataset.eval_dataset_id,
            "manifest_sha256": dataset.manifest_sha256,
            "resource_version": dataset.resource_version,
            "snapshot_sha256": (
                dataset_snapshot["snapshot_sha256"] if dataset_snapshot is not None else None
            ),
        },
        "eval_run": {
            "id": eval_run.run_id,
            "binding_sha256": (eval_run.payload or {}).get("binding_sha256"),
        },
        "eval_result": (
            {
                "id": eval_result.eval_result_id,
                "sha256": eval_result.result_sha256,
                "status": eval_result.status,
            }
            if eval_result is not None
            else None
        ),
        "rollback_target": (
            {
                "deployment_id": rollback_target.deployment_id,
                "bundle_sha256": rollback_target.bundle_sha256,
            }
            if rollback_target is not None
            else None
        ),
    }


def _release_request_from_deployment(
    deployment: ReleaseDeployment,
) -> ReleaseDeploymentCreateRequest:
    return ReleaseDeploymentCreateRequest.model_validate(
        {
            "deployment_id": deployment.deployment_id,
            "environment": deployment.environment,
            "label_version_id": deployment.label_version_id,
            "prompt_version_id": deployment.prompt_version_id,
            "model_version": deployment.model_version,
            "aggregation_policy_version_id": deployment.aggregation_policy_version_id,
            "eval_dataset_version_id": deployment.eval_dataset_version_id,
            "eval_run_id": deployment.eval_run_id,
            "rollback_target_deployment_id": deployment.rollback_target_deployment_id,
        }
    )


def _revalidate_release_deployment(
    session: Session,
    ctx: RequestContext,
    deployment: ReleaseDeployment,
    *,
    require_production_rollback: bool = True,
    verify_current_bundle_hash: bool = True,
) -> None:
    body = _release_request_from_deployment(deployment)
    label = _label_version(session, ctx, body.label_version_id)
    prompt = _prompt_version(session, ctx, body.prompt_version_id)
    policy = _scoped_policy(session, ctx, body.aggregation_policy_version_id)
    dataset = _scoped_dataset(session, ctx, body.eval_dataset_version_id)
    eval_run = _scoped_run(session, ctx, body.eval_run_id)
    eval_result = session.scalar(
        select(LabelEvalResult).where(
            LabelEvalResult.eval_run_id == body.eval_run_id,
            LabelEvalResult.tenant_id == ctx.tenant_id,
            LabelEvalResult.project_id == ctx.project_id,
        )
    )
    rollback_target = (
        _deployment(session, ctx, body.rollback_target_deployment_id)
        if body.rollback_target_deployment_id is not None
        else None
    )
    active_head = _release_head(session, ctx, body.environment, for_update=True)
    dataset_snapshot = (
        locked_eval_dataset_snapshot(
            session,
            ctx,
            body.eval_dataset_version_id,
            required_capability="labeling",
        )
        if dataset.status == "locked"
        else None
    )
    blockers = _release_blockers(
        session=session,
        ctx=ctx,
        body=body,
        label=label,
        prompt=prompt,
        policy=policy,
        dataset=dataset,
        eval_run=eval_run,
        eval_result=eval_result,
        rollback_target=rollback_target,
        active_head=active_head,
        require_production_rollback=require_production_rollback,
    )
    if blockers:
        raise ApiError(
            "RELEASE_BUNDLE_REVALIDATION_BLOCKED",
            "发布 Bundle 的锁定事实已失效，禁止继续推进",
            409,
            details=blockers,
        )
    current_bundle = _release_bundle_document(
        body=body,
        label=label,
        prompt=prompt,
        policy=policy,
        dataset=dataset,
        dataset_snapshot=dataset_snapshot,
        eval_run=eval_run,
        eval_result=eval_result,
        rollback_target=rollback_target,
    )
    stored_bundle = (deployment.payload or {}).get("bundle")
    current_sha256 = _canonical_sha256(current_bundle)
    stored_sha256 = _canonical_sha256(stored_bundle) if isinstance(stored_bundle, dict) else None
    if (
        not isinstance(stored_bundle, dict)
        or stored_sha256 != deployment.bundle_sha256
        or (verify_current_bundle_hash and current_sha256 != deployment.bundle_sha256)
    ):
        raise ApiError(
            "RELEASE_BUNDLE_HASH_DRIFT",
            "发布 Bundle 哈希或其锁定对象已漂移，禁止继续推进",
            409,
            details=[
                {
                    "stored_bundle_sha256": stored_sha256,
                    "current_bundle_sha256": current_sha256,
                    "deployment_bundle_sha256": deployment.bundle_sha256,
                }
            ],
        )


def _create_release_command(
    session: Session,
    ctx: RequestContext,
    deployment: ReleaseDeployment,
    *,
    action: str,
    reason: str,
    expected_deployment_status: str,
    target: ReleaseDeployment | None = None,
    command_id: str | None = None,
) -> ReleaseCommand:
    existing = _active_release_command(
        session,
        ctx,
        deployment.deployment_id,
        for_update=True,
    )
    if existing is not None:
        raise ApiError(
            "RELEASE_COMMAND_ALREADY_ACTIVE",
            "该发布部署已有待执行命令",
            409,
            details=[release_command_data(existing)],
        )
    head = _release_head(session, ctx, deployment.environment, for_update=True)
    resolved_command_id = command_id or f"rc_{uuid.uuid4().hex[:24]}"
    run_id = f"release_command_{resolved_command_id}"
    deployment_payload = deployment.payload or {}
    root_trace_id = str(
        deployment_payload.get("root_trace_id") or deployment.trace_id or ctx.trace_id
    )
    command_document = {
        "command_id": resolved_command_id,
        "deployment_id": deployment.deployment_id,
        "target_deployment_id": target.deployment_id if target is not None else None,
        "environment": deployment.environment,
        "action": action,
        "bundle_sha256": deployment.bundle_sha256,
        "target_bundle_sha256": target.bundle_sha256 if target is not None else None,
        "expected_deployment_status": expected_deployment_status,
        "expected_head_generation": head.generation if head is not None else None,
        "expected_head_deployment_id": (head.active_deployment_id if head is not None else None),
        "expected_head_bundle_sha256": (head.active_bundle_sha256 if head is not None else None),
        "requested_by": ctx.user_id,
        "trace_id": ctx.trace_id,
        "root_trace_id": root_trace_id,
    }
    command_sha256 = _canonical_sha256(command_document)
    run_payload = {
        **command_document,
        "command_sha256": command_sha256,
        "reason": reason,
        "prior_status": deployment.status,
        "prior_stage": deployment.stage,
        "prior_rollout_percentage": deployment.rollout_percentage,
        "affected_objects": [
            {"type": "release_deployment", "id": deployment.deployment_id},
            *(
                [{"type": "release_deployment", "id": target.deployment_id}]
                if target is not None
                else []
            ),
        ],
        "next_actions": [
            {"key": "wait_completion", "label": "等待可信执行回执"},
            {"key": "view_trace", "label": "查看 Trace"},
        ],
    }
    record = RunRecord(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type="release_command",
        status="pending",
        run_key=(
            f"release:{ctx.tenant_id}:{ctx.project_id}:{deployment.environment}:"
            f"{deployment.deployment_id}:{action}"
        ),
        partition_key=f"{ctx.tenant_id}/{ctx.project_id}/{deployment.environment}",
        trace_id=ctx.trace_id,
        payload={**run_payload, "run_id": run_id, "status": "pending"},
    )
    session.add(record)
    session.flush()
    command = ReleaseCommand(
        command_id=resolved_command_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        environment=deployment.environment,
        deployment_id=deployment.deployment_id,
        target_deployment_id=target.deployment_id if target is not None else None,
        action=action,
        status="pending",
        active_slot="active",
        run_id=run_id,
        expected_deployment_status=expected_deployment_status,
        expected_head_generation=head.generation if head is not None else None,
        expected_head_deployment_id=head.active_deployment_id if head is not None else None,
        expected_head_bundle_sha256=(head.active_bundle_sha256 if head is not None else None),
        command_sha256=command_sha256,
        requested_by=ctx.user_id,
        completed_by_source=None,
        completion_receipt_id=None,
        trace_id=ctx.trace_id,
        payload={**run_payload, "bundle_sha256": deployment.bundle_sha256},
    )
    session.add(command)
    payload = dict(deployment.payload or {})
    payload.update(
        {
            "pending_command_id": command.command_id,
            "pending_run_id": command.run_id,
            "pending_action": action,
            "pre_command_status": deployment.status,
            "pre_command_stage": deployment.stage,
            "pre_command_rollout_percentage": deployment.rollout_percentage,
        }
    )
    deployment.status = "pending" if action == "publish" else "materializing"
    if action == "publish":
        deployment.stage = "queued"
    else:
        deployment.stage = "materializing"
    if action == "rollback":
        deployment.rollout_percentage = 0
    deployment.payload = payload
    enqueue_event(
        session,
        ctx,
        event_type="release_deployment.command-requested",
        aggregate_type="release_command",
        aggregate_id=run_id,
        payload=record.payload,
    )
    record_audit(
        session,
        ctx,
        action=f"release_deployment.{action}.requested",
        object_type="release_command",
        object_id=command.command_id,
        after=release_command_data(command),
    )
    session.flush()
    return command


def mark_release_command_dispatched(
    session: Session,
    run: RunRecord,
    dispatch_payload: dict[str, Any],
) -> None:
    if run.run_type != "release_command":
        return
    command = session.scalar(
        select(ReleaseCommand)
        .where(
            ReleaseCommand.run_id == run.run_id,
            ReleaseCommand.tenant_id == run.tenant_id,
            ReleaseCommand.project_id == run.project_id,
        )
        .with_for_update()
    )
    if command is None:
        raise ApiError(
            "RELEASE_COMMAND_PROJECTION_MISSING",
            "发布命令缺少强表投影",
            409,
        )
    if command.status == "pending":
        command.status = "materializing"
        command.payload = {**(command.payload or {}), "dispatch": dispatch_payload}
    deployment = session.scalar(
        select(ReleaseDeployment)
        .where(
            ReleaseDeployment.deployment_id == command.deployment_id,
            ReleaseDeployment.tenant_id == command.tenant_id,
            ReleaseDeployment.project_id == command.project_id,
        )
        .with_for_update()
    )
    if deployment is None:
        raise ApiError(
            "RELEASE_DEPLOYMENT_NOT_FOUND",
            "发布命令绑定的部署不存在",
            409,
        )
    deployment.status = "materializing"
    deployment.payload = {
        **(deployment.payload or {}),
        "command_dispatch": dispatch_payload,
    }


def create_release_deployment(
    session: Session, ctx: RequestContext, body: ReleaseDeploymentCreateRequest
) -> dict[str, Any]:
    duplicate_id = session.scalar(
        select(ReleaseDeployment.deployment_id).where(
            ReleaseDeployment.deployment_id == body.deployment_id
        )
    )
    if duplicate_id is not None:
        raise ApiError("RELEASE_DEPLOYMENT_ID_CONFLICT", "发布部署 ID 已存在", 409)

    label = _label_version(session, ctx, body.label_version_id)
    prompt = _prompt_version(session, ctx, body.prompt_version_id)
    policy = _scoped_policy(session, ctx, body.aggregation_policy_version_id)
    dataset = _scoped_dataset(session, ctx, body.eval_dataset_version_id)
    eval_run = _scoped_run(session, ctx, body.eval_run_id)
    eval_run_payload = eval_run.payload or {}
    label_payload = label.payload or {}
    release_root_trace_id = str(
        eval_run_payload.get("root_trace_id")
        or label_payload.get("root_trace_id")
        or label.trace_id
        or eval_run.trace_id
        or ctx.trace_id
    )
    eval_result = session.scalar(
        select(LabelEvalResult).where(
            LabelEvalResult.eval_run_id == body.eval_run_id,
            LabelEvalResult.tenant_id == ctx.tenant_id,
            LabelEvalResult.project_id == ctx.project_id,
        )
    )
    rollback_target = (
        _deployment(session, ctx, body.rollback_target_deployment_id)
        if body.rollback_target_deployment_id is not None
        else None
    )
    active_head = _release_head(session, ctx, body.environment, for_update=True)
    dataset_snapshot = (
        locked_eval_dataset_snapshot(
            session,
            ctx,
            body.eval_dataset_version_id,
            required_capability="labeling",
        )
        if dataset.status == "locked"
        else None
    )

    bundle_document = _release_bundle_document(
        body=body,
        label=label,
        prompt=prompt,
        policy=policy,
        dataset=dataset,
        dataset_snapshot=dataset_snapshot,
        eval_run=eval_run,
        eval_result=eval_result,
        rollback_target=rollback_target,
    )
    blockers = _release_blockers(
        session=session,
        ctx=ctx,
        body=body,
        label=label,
        prompt=prompt,
        policy=policy,
        dataset=dataset,
        eval_run=eval_run,
        eval_result=eval_result,
        rollback_target=rollback_target,
        active_head=active_head,
    )
    status = "blocked" if blockers else "pending"
    stage = "blocked" if blockers else "queued"
    deployment = ReleaseDeployment(
        deployment_id=body.deployment_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        environment=body.environment,
        status=status,
        stage=stage,
        label_version_id=label.label_version_id,
        prompt_version_id=prompt.prompt_version_id,
        model_version=body.model_version,
        aggregation_policy_version_id=policy.policy_version_id,
        eval_dataset_version_id=dataset.eval_dataset_id,
        eval_run_id=eval_run.run_id,
        rollback_target_deployment_id=(
            rollback_target.deployment_id if rollback_target is not None else None
        ),
        bundle_sha256=_canonical_sha256(bundle_document),
        rollout_percentage=0,
        blocked_reasons=blockers,
        monitor_metrics={},
        approved_by=None,
        trace_id=ctx.trace_id,
        payload={
            "bundle": bundle_document,
            "root_trace_id": release_root_trace_id,
            "status_history": [{"status": status}],
        },
    )
    session.add(deployment)
    session.flush()
    if not blockers:
        _create_release_command(
            session,
            ctx,
            deployment,
            action="publish",
            reason="创建发布 Bundle 并请求执行 Shadow 发布",
            expected_deployment_status="pending",
        )
    data = release_deployment_data(deployment)
    record_audit(
        session,
        ctx,
        action="release_deployment.create",
        object_type="release_deployment",
        object_id=deployment.deployment_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_deployment.created",
        aggregate_type="release_deployment",
        aggregate_id=deployment.deployment_id,
        payload=data,
    )
    return data


def get_release_deployment(
    session: Session, ctx: RequestContext, deployment_id: str
) -> dict[str, Any]:
    return release_deployment_data(_deployment(session, ctx, deployment_id))


def list_release_deployments(
    session: Session,
    ctx: RequestContext,
    *,
    environment: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    statement = select(ReleaseDeployment).where(
        ReleaseDeployment.tenant_id == ctx.tenant_id,
        ReleaseDeployment.project_id == ctx.project_id,
    )
    if environment is not None:
        statement = statement.where(ReleaseDeployment.environment == environment)
    if status is not None:
        statement = statement.where(ReleaseDeployment.status == status)
    rows = session.scalars(
        statement.order_by(
            ReleaseDeployment.created_at.desc(), ReleaseDeployment.deployment_id
        ).limit(limit)
    )
    return [release_deployment_data(item) for item in rows]


def _clear_pending_command(deployment: ReleaseDeployment) -> None:
    payload = dict(deployment.payload or {})
    for key in (
        "pending_command_id",
        "pending_run_id",
        "pending_action",
        "pre_command_status",
        "pre_command_stage",
        "pre_command_rollout_percentage",
    ):
        payload.pop(key, None)
    deployment.payload = payload


def _bundle_pointer_blockers(
    deployment: ReleaseDeployment,
    prompt: PromptVersion,
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    bundle = (deployment.payload or {}).get("bundle")
    if not isinstance(bundle, dict) or _canonical_sha256(bundle) != deployment.bundle_sha256:
        blockers.append(
            _blocked(
                "RELEASE_BUNDLE_HASH_DRIFT",
                "发布 Bundle 内容无法重算为持久化哈希",
            )
        )
        return blockers
    expected = {
        "environment": deployment.environment,
        "label_version_id": deployment.label_version_id,
        "prompt_version_id": deployment.prompt_version_id,
        "model_version": deployment.model_version,
        "aggregation_policy_version_id": deployment.aggregation_policy_version_id,
        "eval_dataset_version_id": deployment.eval_dataset_version_id,
    }
    label_pointer = bundle.get("label")
    label_pointer = label_pointer if isinstance(label_pointer, dict) else {}
    prompt_pointer = bundle.get("prompt")
    prompt_pointer = prompt_pointer if isinstance(prompt_pointer, dict) else {}
    policy_pointer = bundle.get("aggregation_policy")
    policy_pointer = policy_pointer if isinstance(policy_pointer, dict) else {}
    dataset_pointer = bundle.get("eval_dataset")
    dataset_pointer = dataset_pointer if isinstance(dataset_pointer, dict) else {}
    actual = {
        "environment": bundle.get("environment"),
        "label_version_id": label_pointer.get("id"),
        "prompt_version_id": prompt_pointer.get("id"),
        "model_version": bundle.get("model_version"),
        "aggregation_policy_version_id": policy_pointer.get("id"),
        "eval_dataset_version_id": dataset_pointer.get("id"),
    }
    for field, expected_value in expected.items():
        if actual.get(field) != expected_value:
            blockers.append(
                _blocked(
                    "RELEASE_BUNDLE_POINTER_MISMATCH",
                    "发布 Bundle 的有效指针与部署强字段不一致",
                    field=field,
                    expected=expected_value,
                    actual=actual.get(field),
                )
            )
    if (
        prompt.prompt_version_id != deployment.prompt_version_id
        or prompt.label_version_id != deployment.label_version_id
        or (prompt.model_version is not None and prompt.model_version != deployment.model_version)
        or prompt_pointer.get("sha256") != prompt.content_sha256
    ):
        blockers.append(
            _blocked(
                "RELEASE_PROMPT_POINTER_MISMATCH",
                "Prompt 强版本与发布 Bundle 指针不一致",
            )
        )
    return blockers


def _head_cas_blockers(
    command: ReleaseCommand,
    head: ReleaseBundleHead | None,
) -> list[dict[str, Any]]:
    if command.expected_head_generation is None:
        if head is None:
            return []
    elif head is not None and (
        head.generation == command.expected_head_generation
        and head.active_deployment_id == command.expected_head_deployment_id
        and head.active_bundle_sha256 == command.expected_head_bundle_sha256
    ):
        return []
    return [
        _blocked(
            "RELEASE_HEAD_CAS_CONFLICT",
            "active Bundle head 已变化，旧命令不得覆盖新发布事实",
            expected_generation=command.expected_head_generation,
            actual_generation=head.generation if head is not None else None,
            expected_deployment_id=command.expected_head_deployment_id,
            actual_deployment_id=head.active_deployment_id if head is not None else None,
        )
    ]


def _block_release_command(
    session: Session,
    ctx: RequestContext,
    command: ReleaseCommand,
    deployment: ReleaseDeployment,
    blockers: list[dict[str, Any]],
    *,
    completion_receipt: dict[str, Any],
) -> dict[str, Any]:
    command.status = "blocked"
    command.active_slot = None
    command.completed_by_source = str(completion_receipt.get("source") or "system")
    command.completion_receipt_id = (
        str(completion_receipt.get("completion_receipt_id") or "") or None
    )
    command.payload = {
        **(command.payload or {}),
        "blocked_reasons": blockers,
        "completion_receipt_id": command.completion_receipt_id,
    }
    deployment.status = "blocked"
    deployment.stage = "blocked"
    deployment.rollout_percentage = 0
    deployment.blocked_reasons = blockers
    _clear_pending_command(deployment)
    record_audit(
        session,
        ctx,
        action="release_command.completion_blocked",
        object_type="release_command",
        object_id=command.command_id,
        after=release_command_data(command),
        trace_id=command.trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_deployment.command-blocked",
        aggregate_type="release_command",
        aggregate_id=command.command_id,
        payload={
            "command_id": command.command_id,
            "deployment_id": deployment.deployment_id,
            "action": command.action,
            "status": "blocked",
            "blocked_reasons": blockers,
        },
    )
    return {
        "command_id": command.command_id,
        "deployment_id": deployment.deployment_id,
        "action": command.action,
        "status": "blocked",
        "blocked_reasons": blockers,
    }


def _head_values(
    deployment: ReleaseDeployment,
    prompt: PromptVersion,
    *,
    generation: int,
    command_id: str | None,
    bootstrapped: bool,
    trace_id: str,
) -> dict[str, Any]:
    return {
        "active_deployment_id": deployment.deployment_id,
        "active_bundle_sha256": deployment.bundle_sha256,
        "prompt_asset_id": prompt.prompt_asset_id,
        "prompt_version_id": deployment.prompt_version_id,
        "label_version_id": deployment.label_version_id,
        "model_version": deployment.model_version,
        "aggregation_policy_version_id": deployment.aggregation_policy_version_id,
        "eval_dataset_version_id": deployment.eval_dataset_version_id,
        "generation": generation,
        "status": "active",
        "bootstrapped": bootstrapped,
        "activated_by_command_id": command_id,
        "trace_id": trace_id,
        "payload": {
            "bundle_sha256": deployment.bundle_sha256,
            "eval_run_id": deployment.eval_run_id,
            "root_trace_id": str(
                (deployment.payload or {}).get("root_trace_id") or deployment.trace_id or trace_id
            ),
            "activated_at": datetime.now(UTC).isoformat(),
        },
    }


def _set_prompt_active_pointer(
    session: Session,
    ctx: RequestContext,
    deployment: ReleaseDeployment,
) -> PromptVersion:
    prompt = _prompt_version(session, ctx, deployment.prompt_version_id, for_update=True)
    asset = _prompt_asset(session, ctx, prompt.prompt_asset_id, for_update=True)
    pointer_blockers = _bundle_pointer_blockers(deployment, prompt)
    if pointer_blockers:
        raise ApiError(
            "RELEASE_BUNDLE_POINTER_INVALID",
            "发布 Bundle 的强版本指针不一致",
            409,
            details=pointer_blockers,
        )
    prompt.status = "published"
    asset.current_version_id = prompt.prompt_version_id
    asset.trace_id = ctx.trace_id
    return prompt


def _release_head_pointer(head: ReleaseBundleHead) -> dict[str, Any]:
    return {
        "deployment_id": head.active_deployment_id,
        "label_version_id": head.label_version_id,
        "bundle_sha256": head.active_bundle_sha256,
    }


def _append_release_head_event(
    session: Session,
    ctx: RequestContext,
    head: ReleaseBundleHead,
    *,
    action: str,
    previous_generation: int | None,
    old_pointer: dict[str, Any] | None,
    command: ReleaseCommand | None,
    completion_receipt_id: str | None,
    effective_from: datetime,
    legacy_anchor_backfill: bool = False,
) -> ReleaseBundleHeadEvent:
    new_pointer = _release_head_pointer(head)
    root_trace_id = str(
        (head.payload or {}).get("root_trace_id")
        or head.trace_id
        or (command.trace_id if command is not None else None)
        or ctx.trace_id
    )
    actor_id = (
        "system-ledger-backfill"
        if legacy_anchor_backfill
        else (command.requested_by if command is not None else ctx.user_id)
    )
    trace_id = str(command.trace_id if command is not None else head.trace_id or ctx.trace_id)
    payload = {
        "head_event_schema": "release-bundle-head-event/v1",
        "release_head_id": head.release_head_id,
        "bootstrapped": head.bootstrapped,
        "legacy_anchor_backfill": legacy_anchor_backfill,
        "canonical_effective_from": effective_from.isoformat(),
    }
    document = {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "environment": head.environment,
        "generation": head.generation,
        "previous_generation": previous_generation,
        "action": action,
        "activation_status": "active",
        "old_deployment_id": old_pointer.get("deployment_id") if old_pointer else None,
        "new_deployment_id": new_pointer["deployment_id"],
        "old_label_version_id": old_pointer.get("label_version_id") if old_pointer else None,
        "new_label_version_id": new_pointer["label_version_id"],
        "old_bundle_sha256": old_pointer.get("bundle_sha256") if old_pointer else None,
        "new_bundle_sha256": new_pointer["bundle_sha256"],
        "effective_from": effective_from.isoformat(),
        "effective_to": None,
        "command_id": command.command_id if command is not None else None,
        "completion_receipt_id": completion_receipt_id,
        "approval_id": None,
        "actor_id": actor_id,
        "root_trace_id": root_trace_id,
        "trace_id": trace_id,
        "payload": payload,
    }
    content_sha256 = _strict_canonical_sha256(document)
    event = ReleaseBundleHeadEvent(
        head_event_id=f"rbhe_{content_sha256[:24]}",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        environment=head.environment,
        generation=head.generation,
        previous_generation=previous_generation,
        action=action,
        activation_status="active",
        old_deployment_id=document["old_deployment_id"],
        new_deployment_id=document["new_deployment_id"],
        old_label_version_id=document["old_label_version_id"],
        new_label_version_id=document["new_label_version_id"],
        old_bundle_sha256=document["old_bundle_sha256"],
        new_bundle_sha256=document["new_bundle_sha256"],
        effective_from=effective_from,
        effective_to=None,
        command_id=document["command_id"],
        completion_receipt_id=completion_receipt_id,
        approval_id=None,
        content_sha256=content_sha256,
        actor_id=actor_id,
        root_trace_id=root_trace_id,
        trace_id=trace_id,
        payload=payload,
    )
    session.add(event)
    session.flush()
    event_data = release_head_event_data(event)
    record_audit(
        session,
        ctx,
        action=f"release_bundle_head.{action}.recorded",
        object_type="release_bundle_head_event",
        object_id=event.head_event_id,
        after=event_data,
        trace_id=trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_bundle_head.activation-recorded",
        aggregate_type="release_bundle_head_event",
        aggregate_id=event.head_event_id,
        payload=event_data,
    )
    return event


def _ensure_release_head_ledger_anchor(
    session: Session,
    ctx: RequestContext,
    head: ReleaseBundleHead,
) -> ReleaseBundleHeadEvent:
    latest = session.scalar(
        select(ReleaseBundleHeadEvent)
        .where(
            ReleaseBundleHeadEvent.tenant_id == ctx.tenant_id,
            ReleaseBundleHeadEvent.project_id == ctx.project_id,
            ReleaseBundleHeadEvent.environment == head.environment,
        )
        .order_by(ReleaseBundleHeadEvent.generation.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is None:
        if head.generation != 1:
            raise ApiError(
                "RELEASE_ACTIVATION_LEDGER_DRIFT",
                "active Head 缺少可核验的 activation ledger 历史",
                409,
                details=[{"head_generation": head.generation}],
            )
        effective_from = head.created_at or datetime.now(UTC)
        return _append_release_head_event(
            session,
            ctx,
            head,
            action="bootstrap" if head.bootstrapped else "activate",
            previous_generation=None,
            old_pointer=None,
            command=None,
            completion_receipt_id=None,
            effective_from=effective_from,
            legacy_anchor_backfill=True,
        )
    mismatches = {
        "generation": (latest.generation, head.generation),
        "deployment_id": (latest.new_deployment_id, head.active_deployment_id),
        "label_version_id": (latest.new_label_version_id, head.label_version_id),
        "bundle_sha256": (latest.new_bundle_sha256, head.active_bundle_sha256),
    }
    drift = {
        key: {"ledger": pair[0], "head": pair[1]}
        for key, pair in mismatches.items()
        if pair[0] != pair[1]
    }
    if drift:
        raise ApiError(
            "RELEASE_ACTIVATION_LEDGER_DRIFT",
            "active Head 与 activation ledger 不一致",
            409,
            details=[{"mismatches": drift}],
        )
    return latest


def _activate_release_head(
    session: Session,
    ctx: RequestContext,
    deployment: ReleaseDeployment,
    *,
    command: ReleaseCommand | None,
    bootstrapped: bool,
    completion_receipt_id: str | None = None,
) -> ReleaseBundleHead:
    prompt = _set_prompt_active_pointer(session, ctx, deployment)
    head = _release_head(session, ctx, deployment.environment, for_update=True)
    if head is None:
        head_id = (
            "rbh_"
            + hashlib.sha256(
                f"{ctx.tenant_id}:{ctx.project_id}:{deployment.environment}".encode()
            ).hexdigest()[:24]
        )
        head = ReleaseBundleHead(
            release_head_id=head_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            environment=deployment.environment,
            **_head_values(
                deployment,
                prompt,
                generation=1,
                command_id=command.command_id if command is not None else None,
                bootstrapped=bootstrapped,
                trace_id=ctx.trace_id,
            ),
        )
        try:
            with session.begin_nested():
                session.add(head)
                session.flush()
        except IntegrityError as exc:
            raise ApiError(
                "RELEASE_HEAD_CAS_CONFLICT",
                "active Bundle head 已被并发创建",
                409,
            ) from exc
        _append_release_head_event(
            session,
            ctx,
            head,
            action="bootstrap" if bootstrapped else "activate",
            previous_generation=None,
            old_pointer=None,
            command=command,
            completion_receipt_id=completion_receipt_id,
            effective_from=datetime.now(UTC),
        )
        return head

    _ensure_release_head_ledger_anchor(session, ctx, head)
    if command is None or command.expected_head_generation is None:
        raise ApiError(
            "RELEASE_HEAD_CAS_CONFLICT",
            "active Bundle head 已存在，不能使用无头快照覆盖",
            409,
        )
    expected_generation = command.expected_head_generation
    next_generation = expected_generation + 1
    old_pointer = _release_head_pointer(head)
    values = _head_values(
        deployment,
        prompt,
        generation=next_generation,
        command_id=command.command_id,
        bootstrapped=False,
        trace_id=ctx.trace_id,
    )
    result = session.execute(
        update(ReleaseBundleHead)
        .where(
            ReleaseBundleHead.release_head_id == head.release_head_id,
            ReleaseBundleHead.tenant_id == ctx.tenant_id,
            ReleaseBundleHead.project_id == ctx.project_id,
            ReleaseBundleHead.environment == deployment.environment,
            ReleaseBundleHead.generation == expected_generation,
            ReleaseBundleHead.active_deployment_id == command.expected_head_deployment_id,
            ReleaseBundleHead.active_bundle_sha256 == command.expected_head_bundle_sha256,
        )
        .values(**values)
    )
    if getattr(result, "rowcount", None) != 1:
        raise ApiError(
            "RELEASE_HEAD_CAS_CONFLICT",
            "active Bundle head CAS 更新失败",
            409,
        )
    session.expire(head)
    session.refresh(head)
    _append_release_head_event(
        session,
        ctx,
        head,
        action="rollback" if command.action == "rollback" else "promote",
        previous_generation=expected_generation,
        old_pointer=old_pointer,
        command=command,
        completion_receipt_id=completion_receipt_id,
        effective_from=datetime.now(UTC),
    )
    return head


def materialize_release_command_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any]:
    if record.run_type != "release_command":
        raise ApiError("RELEASE_COMMAND_RUN_TYPE_INVALID", "运行不是发布命令", 409)
    command = session.scalar(
        select(ReleaseCommand)
        .where(
            ReleaseCommand.run_id == record.run_id,
            ReleaseCommand.tenant_id == ctx.tenant_id,
            ReleaseCommand.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if command is None:
        raise ApiError("RELEASE_COMMAND_NOT_FOUND", "发布命令不存在", 404)
    if command.status not in {"pending", "materializing"} or command.active_slot != "active":
        raise ApiError(
            "RELEASE_COMMAND_NOT_OPEN",
            "发布命令不再接受完成回执",
            409,
            details=[{"command_id": command.command_id, "status": command.status}],
        )
    deployment = _deployment(session, ctx, command.deployment_id, for_update=True)
    result_ref = completion_receipt.get("result_ref")
    result_ref = result_ref if isinstance(result_ref, dict) else {}
    expected_ack = {
        "release_command_id": command.command_id,
        "command_sha256": command.command_sha256,
        "deployment_id": command.deployment_id,
        "environment": command.environment,
        "action": command.action,
        "bundle_sha256": deployment.bundle_sha256,
        "applied": True,
    }
    mismatches = {
        key: {"expected": expected, "actual": result_ref.get(key)}
        for key, expected in expected_ack.items()
        if result_ref.get(key) != expected
    }
    if mismatches:
        raise ApiError(
            "RELEASE_COMMAND_ACK_BINDING_MISMATCH",
            "发布执行 ACK 与冻结命令不一致",
            409,
            details=[{"mismatches": mismatches}],
        )
    head = _release_head(session, ctx, command.environment, for_update=True)
    cas_blockers = _head_cas_blockers(command, head)
    if cas_blockers:
        return _block_release_command(
            session,
            ctx,
            command,
            deployment,
            cas_blockers,
            completion_receipt=completion_receipt,
        )

    if command.action in {"publish", "approve-gray", "promote"}:
        try:
            _revalidate_release_deployment(session, ctx, deployment)
        except ApiError as exc:
            return _block_release_command(
                session,
                ctx,
                command,
                deployment,
                [
                    _blocked(
                        "RELEASE_BUNDLE_REVALIDATION_BLOCKED",
                        "发布执行 ACK 到达时锁定事实已漂移",
                        source_error=exc.code,
                        source_details=exc.details,
                    )
                ],
                completion_receipt=completion_receipt,
            )

    before = release_deployment_data(deployment)
    if command.action == "publish":
        deployment.status = "shadowing"
        deployment.stage = "shadowing"
        deployment.rollout_percentage = 0
    elif command.action == "approve-gray":
        deployment.status = "gray-releasing"
        deployment.stage = "gray-releasing"
        deployment.rollout_percentage = 10
        deployment.approved_by = command.requested_by
    elif command.action == "promote":
        if head is not None and head.active_deployment_id != deployment.deployment_id:
            previous = _deployment(session, ctx, head.active_deployment_id, for_update=True)
            previous.status = "superseded"
            previous.stage = "superseded"
            previous.rollout_percentage = 0
            session.flush()
        deployment.status = "completed"
        deployment.stage = "completed"
        deployment.rollout_percentage = 100
        deployment.approved_by = command.requested_by
        _activate_release_head(
            session,
            ctx,
            deployment,
            command=command,
            bootstrapped=False,
            completion_receipt_id=str(completion_receipt.get("completion_receipt_id") or "")
            or None,
        )
    else:
        if command.target_deployment_id is None:
            raise ApiError("ROLLBACK_TARGET_REQUIRED", "回滚命令缺少目标部署", 409)
        target = _deployment(session, ctx, command.target_deployment_id, for_update=True)
        target_blockers = _rollback_target_blockers(
            target,
            environment=deployment.environment,
            allow_superseded=True,
        )
        target_prompt = _prompt_version(session, ctx, target.prompt_version_id, for_update=True)
        target_blockers.extend(_bundle_pointer_blockers(target, target_prompt))
        if target_blockers:
            return _block_release_command(
                session,
                ctx,
                command,
                deployment,
                target_blockers,
                completion_receipt=completion_receipt,
            )
        if head is not None and head.active_deployment_id != target.deployment_id:
            previous = _deployment(session, ctx, head.active_deployment_id, for_update=True)
            previous.status = (
                "rolled-back"
                if previous.deployment_id == deployment.deployment_id
                else "superseded"
            )
            previous.stage = previous.status
            previous.rollout_percentage = 0
            session.flush()
        deployment.status = "rolled-back"
        deployment.stage = "rolled-back"
        deployment.rollout_percentage = 0
        target.status = "completed"
        target.stage = "completed"
        target.rollout_percentage = 100
        session.flush()
        _activate_release_head(
            session,
            ctx,
            target,
            command=command,
            bootstrapped=False,
            completion_receipt_id=str(completion_receipt.get("completion_receipt_id") or "")
            or None,
        )
        deployment.payload = {
            **(deployment.payload or {}),
            "rolled_back_to": target.deployment_id,
        }

    command.status = "completed"
    command.active_slot = None
    command.completed_by_source = str(completion_receipt.get("source") or "system")
    command.completion_receipt_id = (
        str(completion_receipt.get("completion_receipt_id") or "") or None
    )
    command.payload = {
        **(command.payload or {}),
        "completion_receipt_id": command.completion_receipt_id,
        "completed_at": datetime.now(UTC).isoformat(),
    }
    _clear_pending_command(deployment)
    payload = dict(deployment.payload or {})
    history = list(payload.get("status_history") or [])
    history.append(
        {
            "status": deployment.status,
            "action": command.action,
            "command_id": command.command_id,
            "completion_receipt_id": command.completion_receipt_id,
            "trace_id": ctx.trace_id,
        }
    )
    payload["status_history"] = history
    deployment.payload = payload
    session.flush()
    after = release_deployment_data(deployment)
    record_audit(
        session,
        ctx,
        action=f"release_deployment.{command.action}.acknowledged",
        object_type="release_deployment",
        object_id=deployment.deployment_id,
        before=before,
        after=after,
        trace_id=command.trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_deployment.command-acknowledged",
        aggregate_type="release_command",
        aggregate_id=command.command_id,
        payload={
            "command_id": command.command_id,
            "deployment_id": deployment.deployment_id,
            "action": command.action,
            "status": "completed",
            "deployment_status": deployment.status,
        },
    )
    return {
        "command_id": command.command_id,
        "deployment_id": deployment.deployment_id,
        "action": command.action,
        "status": "completed",
        "deployment_status": deployment.status,
    }


def materialize_release_command_failure(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Close a failed execution attempt without changing the effective head."""

    command = session.scalar(
        select(ReleaseCommand)
        .where(
            ReleaseCommand.run_id == record.run_id,
            ReleaseCommand.tenant_id == ctx.tenant_id,
            ReleaseCommand.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if command is None:
        raise ApiError("RELEASE_COMMAND_NOT_FOUND", "发布命令不存在", 404)
    if command.status not in {"pending", "materializing"} or command.active_slot != "active":
        raise ApiError("RELEASE_COMMAND_NOT_OPEN", "发布命令不再接受失败回执", 409)
    deployment = _deployment(session, ctx, command.deployment_id, for_update=True)
    blocker = _blocked(
        "RELEASE_COMMAND_EXECUTION_FAILED",
        "执行器确认发布命令失败；active Bundle head 未改变",
        error_code=completion_receipt.get("error_code"),
        note=completion_receipt.get("note"),
    )
    command.status = "failed"
    command.active_slot = None
    command.completed_by_source = str(completion_receipt.get("source") or "system")
    command.completion_receipt_id = (
        str(completion_receipt.get("completion_receipt_id") or "") or None
    )
    command.payload = {
        **(command.payload or {}),
        "failed_reason": blocker,
        "completion_receipt_id": command.completion_receipt_id,
    }
    deployment.status = "blocked"
    deployment.stage = "blocked"
    deployment.rollout_percentage = 0
    deployment.blocked_reasons = [blocker]
    _clear_pending_command(deployment)
    record_audit(
        session,
        ctx,
        action="release_command.execution_failed",
        object_type="release_command",
        object_id=command.command_id,
        after=release_command_data(command),
        trace_id=command.trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_deployment.command-failed",
        aggregate_type="release_command",
        aggregate_id=command.command_id,
        payload={
            "command_id": command.command_id,
            "deployment_id": deployment.deployment_id,
            "action": command.action,
            "status": "failed",
            "blocked_reasons": [blocker],
        },
    )
    return {
        "command_id": command.command_id,
        "deployment_id": deployment.deployment_id,
        "action": command.action,
        "status": "failed",
        "blocked_reasons": [blocker],
    }


def bootstrap_release_bundle_head(
    session: Session,
    ctx: RequestContext,
    deployment_id: str,
    body: ReleaseHeadBootstrapRequest,
) -> dict[str, Any]:
    deployment = _deployment(session, ctx, deployment_id, for_update=True)
    if deployment.environment != "production":
        raise ApiError(
            "RELEASE_BOOTSTRAP_PRODUCTION_ONLY",
            "LKG bootstrap 只用于首次建立 production active head",
            409,
        )
    if _release_head(session, ctx, deployment.environment, for_update=True) is not None:
        raise ApiError("RELEASE_HEAD_ALREADY_EXISTS", "active Bundle head 已存在", 409)
    if _active_release_command(session, ctx, deployment.deployment_id, for_update=True) is not None:
        raise ApiError(
            "RELEASE_BOOTSTRAP_ACTIVE_COMMAND",
            "存在待执行发布命令，不能 bootstrap",
            409,
        )
    if deployment.status != "blocked" or {
        item.get("code") for item in deployment.blocked_reasons or []
    } - {"ROLLBACK_TARGET_REQUIRED", "RELEASE_ACTIVE_HEAD_REQUIRED"}:
        raise ApiError(
            "RELEASE_BOOTSTRAP_NOT_ELIGIBLE",
            "只有因缺少初始 LKG/head 而阻断的强 Bundle 可 bootstrap",
            409,
            details=deployment.blocked_reasons or [],
        )
    _revalidate_release_deployment(
        session,
        ctx,
        deployment,
        require_production_rollback=False,
        verify_current_bundle_hash=False,
    )
    conflicting = session.scalar(
        select(ReleaseDeployment.deployment_id).where(
            ReleaseDeployment.tenant_id == ctx.tenant_id,
            ReleaseDeployment.project_id == ctx.project_id,
            ReleaseDeployment.environment == deployment.environment,
            ReleaseDeployment.deployment_id != deployment.deployment_id,
            ReleaseDeployment.status == "completed",
            ReleaseDeployment.rollout_percentage == 100,
        )
    )
    if conflicting is not None:
        raise ApiError(
            "RELEASE_BOOTSTRAP_AMBIGUOUS_LKG",
            "已有 completed 100% 部署但未建立 head，必须先人工消歧",
            409,
            details=[{"deployment_id": conflicting}],
        )
    before = release_deployment_data(deployment)
    deployment.status = "completed"
    deployment.stage = "completed"
    deployment.rollout_percentage = 100
    deployment.blocked_reasons = []
    deployment.approved_by = ctx.user_id
    head = _activate_release_head(
        session,
        ctx,
        deployment,
        command=None,
        bootstrapped=True,
    )
    payload = dict(deployment.payload or {})
    history = list(payload.get("status_history") or [])
    history.append(
        {
            "status": "completed",
            "action": "bootstrap-last-known-good",
            "reason": body.reason,
            "actor_id": ctx.user_id,
            "trace_id": ctx.trace_id,
        }
    )
    payload["status_history"] = history
    payload["bootstrap_confirmation"] = body.confirmation
    deployment.payload = payload
    session.flush()
    data = release_deployment_data(deployment)
    record_audit(
        session,
        ctx,
        action="release_bundle_head.bootstrap",
        object_type="release_bundle_head",
        object_id=head.release_head_id,
        before=before,
        after={"deployment": data, "head": release_head_data(head), "reason": body.reason},
    )
    enqueue_event(
        session,
        ctx,
        event_type="release_bundle_head.bootstrapped",
        aggregate_type="release_bundle_head",
        aggregate_id=head.release_head_id,
        payload=release_head_data(head),
    )
    return data


def _monitor_blockers(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    required = {
        "json_valid_rate": 0.995,
        "conflict_rate": 0.05,
        "critical_recall_delta_pp": -0.5,
        "cost_ratio": 1.10,
    }
    if metrics.get("stable_window_complete") is not True:
        blockers.append(_blocked("STABLE_WINDOW_INCOMPLETE", "灰度稳定窗口尚未完成"))

    def number(name: str) -> float | None:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            blockers.append(
                _blocked("MONITOR_METRIC_MISSING", "监控门禁指标缺失或无效", metric=name)
            )
            return None
        return float(value)

    json_rate = number("json_valid_rate")
    if json_rate is not None and json_rate < required["json_valid_rate"]:
        blockers.append(
            _blocked(
                "JSON_VALID_RATE_REGRESSION",
                "JSON 合法率低于发布门禁",
                actual=json_rate,
                threshold=required["json_valid_rate"],
            )
        )
    conflict_rate = number("conflict_rate")
    if conflict_rate is not None and conflict_rate >= required["conflict_rate"]:
        blockers.append(
            _blocked(
                "CONFLICT_RATE_REGRESSION",
                "聚合冲突率达到发布阻断阈值",
                actual=conflict_rate,
                threshold=required["conflict_rate"],
            )
        )
    recall_delta = number("critical_recall_delta_pp")
    if recall_delta is not None and recall_delta < required["critical_recall_delta_pp"]:
        blockers.append(
            _blocked(
                "CRITICAL_RECALL_REGRESSION",
                "关键标签 recall 超出非劣界",
                actual=recall_delta,
                threshold=required["critical_recall_delta_pp"],
            )
        )
    cost_ratio = number("cost_ratio")
    if cost_ratio is not None and cost_ratio > required["cost_ratio"]:
        blockers.append(
            _blocked(
                "COST_REGRESSION",
                "单位成本超过基线 110%",
                actual=cost_ratio,
                threshold=required["cost_ratio"],
            )
        )
    return blockers


def _hard_monitor_violations(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Hard online safety thresholds that require an immediate safe stop."""

    violations: list[dict[str, Any]] = []
    checks = (
        ("json_valid_rate", "lt", 0.995, "JSON_VALID_RATE_HARD_REGRESSION"),
        ("conflict_rate", "gt", 0.05, "CONFLICT_RATE_HARD_REGRESSION"),
        ("critical_recall_delta_pp", "lt", -2.0, "CRITICAL_RECALL_HARD_REGRESSION"),
        ("human_override_delta_pp", "ge", 3.0, "HUMAN_OVERRIDE_HARD_REGRESSION"),
        ("cost_ratio", "gt", 1.10, "COST_HARD_REGRESSION"),
        ("latency_ratio", "gt", 1.20, "LATENCY_HARD_REGRESSION"),
    )
    for metric, operator, threshold, code in checks:
        actual = float(metrics[metric])
        breached = (
            (operator == "lt" and actual < threshold)
            or (operator == "gt" and actual > threshold)
            or (operator == "ge" and actual >= threshold)
        )
        if breached:
            violations.append(
                _blocked(
                    code,
                    "在线监控指标触发自动回滚硬阈值",
                    metric=metric,
                    actual=actual,
                    threshold=threshold,
                    operator=operator,
                )
            )
    return violations


def ingest_release_monitor_sample(
    session: Session,
    ctx: RequestContext,
    deployment_id: str,
    body: ReleaseMonitorSampleRequest,
) -> dict[str, Any]:
    """Persist one online sample and automatically roll back on hard regression."""

    deployment = _deployment(session, ctx, deployment_id, for_update=True)
    if deployment.status != body.expected_status:
        raise ApiError(
            "RELEASE_STATUS_CONFLICT",
            "发布状态已变化，请刷新后重试",
            409,
            details=[{"expected_status": body.expected_status, "actual_status": deployment.status}],
        )
    if deployment.status not in {"shadowing", "gray-releasing", "monitoring"}:
        raise ApiError("RELEASE_MONITOR_STATE_INVALID", "当前发布状态不能接收在线指标", 409)

    sample_document = body.model_dump(mode="json")
    sample_sha256 = _canonical_sha256(sample_document)
    payload = dict(deployment.payload or {})
    samples = list(payload.get("monitor_samples") or [])
    existing = next(
        (sample for sample in samples if sample.get("sample_id") == body.sample_id),
        None,
    )
    if existing is not None:
        if existing.get("sample_sha256") != sample_sha256:
            raise ApiError(
                "RELEASE_MONITOR_SAMPLE_CONFLICT",
                "同一监控样本 ID 的内容不一致",
                409,
            )
        return release_deployment_data(deployment)

    before = release_deployment_data(deployment)
    metrics = {
        **body.metrics.model_dump(mode="json", exclude_none=True),
        "stable_window_complete": body.stable_window_complete,
    }
    violations = _hard_monitor_violations(metrics)
    sample = {
        **sample_document,
        "sample_sha256": sample_sha256,
        "received_trace_id": ctx.trace_id,
        "violations": violations,
    }
    samples.append(sample)
    payload["monitor_samples"] = samples[-100:]
    payload["last_monitor_sample_id"] = body.sample_id
    payload["last_monitor_sample_sha256"] = sample_sha256
    deployment.monitor_metrics = {**(deployment.monitor_metrics or {}), **metrics}
    # Persist the sample into the in-session projection before a rollback command
    # snapshots and extends the same payload.
    deployment.payload = payload

    event_type = "release_deployment.monitor-sample-recorded"
    automatic_action = "continue-monitoring"
    if violations:
        deployment.rollout_percentage = 0
        deployment.blocked_reasons = violations
        if deployment.rollback_target_deployment_id is not None:
            target = _deployment(
                session,
                ctx,
                deployment.rollback_target_deployment_id,
                for_update=True,
            )
            target_blockers = _rollback_target_blockers(
                target,
                environment=deployment.environment,
                allow_superseded=True,
            )
            target_prompt = _prompt_version(
                session,
                ctx,
                target.prompt_version_id,
                for_update=True,
            )
            target_blockers.extend(_bundle_pointer_blockers(target, target_prompt))
            if target_blockers:
                violations.extend(target_blockers)
                deployment.status = "blocked"
                deployment.stage = "blocked"
                automatic_action = "safe-stop-blocked"
                event_type = "release_deployment.auto-rollback-blocked"
            else:
                command = _create_release_command(
                    session,
                    ctx,
                    deployment,
                    action="rollback",
                    reason="在线硬阈值触发自动安全回滚",
                    expected_deployment_status=body.expected_status,
                    target=target,
                    command_id=f"rc_auto_{sample_sha256[:20]}",
                )
                automatic_action = "auto-rollback-requested"
                event_type = "release_deployment.auto-rollback-requested"
                payload = {
                    **(deployment.payload or {}),
                    "automatic_rollback_command_id": command.command_id,
                    "automatic_rollback_run_id": command.run_id,
                }
        else:
            violations.append(
                _blocked(
                    "ROLLBACK_TARGET_REQUIRED",
                    "硬阈值已触发，但发布 Bundle 未锁定回滚目标；已立即停止流量",
                )
            )
            deployment.status = "blocked"
            deployment.stage = "blocked"
            automatic_action = "safe-stop-blocked"
            event_type = "release_deployment.auto-rollback-blocked"
        deployment.blocked_reasons = violations
    else:
        deployment.status = "monitoring"
        deployment.stage = "monitoring"

    payload = {**(deployment.payload or {}), **payload}
    history = list(payload.get("status_history") or [])
    history.append(
        {
            "status": deployment.status,
            "action": automatic_action,
            "sample_id": body.sample_id,
            "actor_id": ctx.user_id,
            "trace_id": ctx.trace_id,
        }
    )
    payload["status_history"] = history
    payload["last_automatic_action"] = automatic_action
    deployment.payload = payload
    session.flush()
    data = release_deployment_data(deployment)
    record_audit(
        session,
        ctx,
        action=f"release_deployment.{automatic_action}",
        object_type="release_deployment",
        object_id=deployment.deployment_id,
        before=before,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type=event_type,
        aggregate_type="release_deployment",
        aggregate_id=deployment.deployment_id,
        payload={**data, "monitor_sample_id": body.sample_id},
    )
    return data


def transition_release_deployment(
    session: Session,
    ctx: RequestContext,
    deployment_id: str,
    body: ReleaseTransitionRequest,
) -> dict[str, Any]:
    deployment = _deployment(session, ctx, deployment_id, for_update=True)
    if deployment.status != body.expected_status:
        raise ApiError(
            "RELEASE_STATUS_CONFLICT",
            "发布状态已变化，请刷新后重试",
            409,
            details=[{"expected_status": body.expected_status, "actual_status": deployment.status}],
        )
    if body.monitor_metrics:
        raise ApiError(
            "RELEASE_MONITOR_METRICS_SYSTEM_OWNED",
            "发布状态流转不能写入在线监控指标；请由系统监控使用 monitor-samples 接口上报",
            422,
            details=[
                {
                    "field": "monitor_metrics",
                    "write_endpoint": (
                        f"/api/v1/release-deployments/{deployment.deployment_id}/monitor-samples"
                    ),
                }
            ],
        )
    if body.action in {"approve-gray", "promote"} and deployment.status == "blocked":
        raise ApiError(
            "RELEASE_DEPLOYMENT_BLOCKED",
            "发布存在阻断项，不能继续推进",
            409,
            details=deployment.blocked_reasons or [],
        )
    if body.action in {"approve-gray", "promote"}:
        _revalidate_release_deployment(session, ctx, deployment)

    before = release_deployment_data(deployment)
    target: ReleaseDeployment | None = None
    if body.action == "approve-gray":
        if deployment.status not in {"shadowing", "monitoring"}:
            raise ApiError("RELEASE_TRANSITION_INVALID", "当前状态不能批准灰度", 409)
    elif body.action == "promote":
        if deployment.status not in {"gray-releasing", "monitoring"}:
            raise ApiError("RELEASE_TRANSITION_INVALID", "当前状态不能晋级正式发布", 409)
        monitor_metrics = dict(deployment.monitor_metrics or {})
        monitor_blockers = _monitor_blockers(monitor_metrics)
        if monitor_blockers:
            raise ApiError(
                "RELEASE_MONITOR_GATE_BLOCKED",
                "在线监控门禁未通过，禁止晋级",
                409,
                details=monitor_blockers,
            )
    else:
        if deployment.status == "rolled-back":
            raise ApiError("RELEASE_TRANSITION_INVALID", "发布已经回滚", 409)
        if deployment.rollback_target_deployment_id is None:
            raise ApiError("ROLLBACK_TARGET_REQUIRED", "发布未锁定回滚目标", 409)
        target = _deployment(
            session,
            ctx,
            deployment.rollback_target_deployment_id,
            for_update=True,
        )
        target_blockers = _rollback_target_blockers(
            target,
            environment=deployment.environment,
            allow_superseded=True,
        )
        target_prompt = _prompt_version(session, ctx, target.prompt_version_id, for_update=True)
        target_blockers.extend(_bundle_pointer_blockers(target, target_prompt))
        if target_blockers:
            raise ApiError(
                "ROLLBACK_TARGET_NOT_STABLE",
                "回滚目标不是可校验的稳定发布版本",
                409,
                details=target_blockers,
            )
    command = _create_release_command(
        session,
        ctx,
        deployment,
        action=body.action,
        reason=body.reason,
        expected_deployment_status=body.expected_status,
        target=target,
    )
    payload = dict(deployment.payload or {})
    history = list(payload.get("status_history") or [])
    history.append(
        {
            "status": deployment.status,
            "action": f"{body.action}-requested",
            "reason": body.reason,
            "actor_id": ctx.user_id,
            "command_id": command.command_id,
            "run_id": command.run_id,
        }
    )
    payload["status_history"] = history
    deployment.payload = payload
    session.flush()
    data = release_deployment_data(deployment)
    record_audit(
        session,
        ctx,
        action=f"release_deployment.{body.action}.command_created",
        object_type="release_deployment",
        object_id=deployment.deployment_id,
        before=before,
        after={
            **data,
            "transition_reason": body.reason,
            "command": release_command_data(command),
        },
    )
    return data
