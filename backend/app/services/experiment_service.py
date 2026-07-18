from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from statistics import mean, variance
from typing import Any

from fastapi import Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import (
    ControlledExperiment,
    ExperimentAssignment,
    ExperimentDecision,
    ExperimentExposure,
    ExperimentMetricSnapshot,
    ExperimentOutcome,
    JsonResource,
    RunRecord,
)
from app.schemas.experiments import (
    ExperimentAssignmentRequest,
    ExperimentCreateRequest,
    ExperimentDecisionRequest,
    ExperimentExposureRequest,
    ExperimentOutcomeRequest,
    ExperimentStartRequest,
)
from app.schemas.scene_profiles import SceneProfileManifest
from app.services.audit_service import record_audit
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.outbox_service import enqueue_event
from app.services.scene_profile_service import get_active_scene_binding
from app.services.task_version_bundle import (
    build_task_version_bundle,
    compare_task_version_bundles,
)

EXPERIMENT_WRITE_ROLES = ("project_admin", "model_engineer")
EXPERIMENT_EVENT_ROLES = ("project_admin", "model_engineer", "operator")
EXPERIMENT_SAMPLE_RATIO_ALPHA = 0.001
TASK_VERSION_EXPERIMENT_STATUSES = {"published", "validated", "experiment_ready"}
TASK_VERSION_HISTORICAL_STATUSES = {"deprecated"}
TASK_VERSION_LIFECYCLE_FIELDS = {
    "status",
    "published_at",
    "published_by",
    "publish_run_id",
    "deprecated_at",
    "deprecated_by",
    "replaced_by_task_version_id",
    "replacement_run_id",
    "trace_id",
}
EXPERIMENT_ASSIGNMENT_KEY_VERSION = "v1"
Z_CRITICAL = {0.9: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _hmac_sha256(value: str) -> str:
    secret = get_settings().experiment_assignment_secret.encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def encode_experiment_cursor(experiment: ControlledExperiment) -> str:
    created_at = experiment.created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(UTC).replace(tzinfo=None)
    token = f"controlled_experiment|{created_at.isoformat()}|{experiment.experiment_id}".encode()
    return urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode_experiment_cursor(cursor: str | int | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    try:
        cursor_text = str(cursor)
        padded = cursor_text + "=" * (-len(cursor_text) % 4)
        raw = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        prefix, created_at, experiment_id = raw.split("|", 2)
        if prefix != "controlled_experiment" or not created_at or not experiment_id:
            raise ValueError
        decoded_created_at = datetime.fromisoformat(created_at)
        if decoded_created_at.tzinfo is not None:
            decoded_created_at = decoded_created_at.astimezone(UTC).replace(tzinfo=None)
        return decoded_created_at, experiment_id
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None


def _human_only(ctx: RequestContext, *, action: str) -> None:
    if ctx.actor_kind != "human":
        raise ApiError(
            "HUMAN_ACTOR_REQUIRED",
            "实验启动、停止和晋级决策必须由实名人工执行",
            403,
            details=[{"action": action, "actor_kind": ctx.actor_kind}],
        )


def _experiment_query(ctx: RequestContext) -> Select[tuple[ControlledExperiment]]:
    return select(ControlledExperiment).where(
        ControlledExperiment.tenant_id == ctx.tenant_id,
        ControlledExperiment.project_id == ctx.project_id,
    )


def get_experiment(
    session: Session,
    ctx: RequestContext,
    experiment_id: str,
    *,
    for_update: bool = False,
) -> ControlledExperiment:
    statement = _experiment_query(ctx).where(ControlledExperiment.experiment_id == experiment_id)
    if for_update:
        statement = statement.with_for_update()
    experiment = session.scalar(statement)
    if experiment is None:
        raise ApiError("EXPERIMENT_NOT_FOUND", "实验不存在或不属于当前项目", 404)
    return experiment


def _task_version(
    session: Session,
    ctx: RequestContext,
    task_version_id: str,
    *,
    allow_historical: bool = False,
) -> JsonResource:
    resource = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == "task_versions",
            JsonResource.resource_key == task_version_id,
        )
    )
    if resource is None:
        raise ApiError(
            "EXPERIMENT_TASK_VERSION_NOT_FOUND",
            "实验引用的 TaskVersion 不存在",
            404,
            details=[{"task_version_id": task_version_id}],
        )
    status = str(resource.data.get("status") or resource.status or "")
    allowed_statuses = TASK_VERSION_EXPERIMENT_STATUSES | (
        TASK_VERSION_HISTORICAL_STATUSES if allow_historical else set()
    )
    if status not in allowed_statuses:
        raise ApiError(
            "EXPERIMENT_TASK_VERSION_NOT_IMMUTABLE",
            "实验只能引用已发布、已校验或已冻结为实验候选的 TaskVersion",
            409,
            details=[{"task_version_id": task_version_id, "status": status}],
        )
    return resource


def _task_version_snapshot_sha256(resource: JsonResource) -> str:
    executable_data = {
        key: value
        for key, value in resource.data.items()
        if key not in TASK_VERSION_LIFECYCLE_FIELDS
    }
    return _sha256(
        {
            "task_version_id": resource.resource_key,
            "executable_data": executable_data,
        }
    )


def _assert_frozen_task_versions(
    session: Session,
    ctx: RequestContext,
    experiment: ControlledExperiment,
) -> dict[str, JsonResource]:
    resources: dict[str, JsonResource] = {}
    for arm in experiment.payload.get("arms", []):
        arm_key = str(arm.get("arm_key") or "")
        task_version_id = str(arm.get("task_version_id") or "")
        expected_sha256 = str(arm.get("task_version_snapshot_sha256") or "")
        if not arm_key or not task_version_id or len(expected_sha256) != 64:
            raise ApiError(
                "EXPERIMENT_TASK_VERSION_LOCK_INVALID",
                "实验设计缺少不可变 TaskVersion 内容锁",
                409,
                details=[{"arm_key": arm_key, "task_version_id": task_version_id}],
            )
        resource = _task_version(
            session,
            ctx,
            task_version_id,
            allow_historical=experiment.status in {"running", "paused", "stopped", "decided"},
        )
        actual_sha256 = _task_version_snapshot_sha256(resource)
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ApiError(
                "EXPERIMENT_TASK_VERSION_DRIFT",
                "实验引用的 TaskVersion 内容已漂移，必须终止并重建实验",
                409,
                details=[
                    {
                        "arm_key": arm_key,
                        "task_version_id": task_version_id,
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    }
                ],
            )
        actual_bundle = build_task_version_bundle(resource.resource_key, resource.data)
        for digest_field in ("behavior_sha256", "binding_sha256"):
            expected_digest = str(arm.get(f"task_version_{digest_field}") or "")
            if len(expected_digest) != 64 or not hmac.compare_digest(
                actual_bundle[digest_field], expected_digest
            ):
                raise ApiError(
                    "EXPERIMENT_TASK_VERSION_BUNDLE_DRIFT",
                    "实验引用的 TaskVersion 执行 bundle 已漂移，必须终止并重建实验",
                    409,
                    details=[
                        {
                            "arm_key": arm_key,
                            "task_version_id": task_version_id,
                            "digest_field": digest_field,
                            "expected_sha256": expected_digest,
                            "actual_sha256": actual_bundle[digest_field],
                        }
                    ],
                )
        resources[arm_key] = resource
    if set(resources) != {"control", "candidate"}:
        raise ApiError("EXPERIMENT_ARMS_INVALID", "实验臂定义不完整", 409)
    return resources


def _counts(session: Session, experiment: ControlledExperiment) -> dict[str, int]:
    scope = (
        experiment.tenant_id,
        experiment.project_id,
        experiment.experiment_id,
    )

    def count(model: Any) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == scope[0],
                    model.project_id == scope[1],
                    model.experiment_id == scope[2],
                )
            )
            or 0
        )

    return {
        "assignments": count(ExperimentAssignment),
        "exposures": count(ExperimentExposure),
        "outcomes": count(ExperimentOutcome),
        "metric_snapshots": count(ExperimentMetricSnapshot),
        "decisions": count(ExperimentDecision),
    }


def experiment_payload(
    session: Session,
    experiment: ControlledExperiment,
    *,
    include_counts: bool = True,
) -> dict[str, Any]:
    payload = {
        "experiment_id": experiment.experiment_id,
        "tenant_id": experiment.tenant_id,
        "project_id": experiment.project_id,
        "name": experiment.name,
        "experiment_kind": experiment.experiment_kind,
        "task_type_id": experiment.task_type_id,
        "control_task_version_id": experiment.control_task_version_id,
        "candidate_task_version_id": experiment.candidate_task_version_id,
        "scene_profile_id": experiment.scene_profile_id,
        "scene_profile_version_id": experiment.scene_profile_version_id,
        "scene_profile_snapshot_sha256": experiment.scene_profile_snapshot_sha256,
        "design_sha256": experiment.design_sha256,
        "status": experiment.status,
        "resource_version": experiment.resource_version,
        "started_at": _iso(experiment.started_at),
        "ended_at": _iso(experiment.ended_at),
        "trace_id": experiment.trace_id,
        **experiment.payload,
        "created_at": _iso(experiment.created_at),
        "updated_at": _iso(experiment.updated_at),
    }
    if include_counts:
        payload["counts"] = _counts(session, experiment)
    return payload


def assignment_payload(assignment: ExperimentAssignment) -> dict[str, Any]:
    return {
        "assignment_id": assignment.assignment_id,
        "experiment_id": assignment.experiment_id,
        "subject_key_sha256": assignment.subject_key_sha256,
        "arm_key": assignment.arm_key,
        "assignment_bucket": assignment.assignment_bucket,
        "design_sha256": assignment.design_sha256,
        "trace_id": assignment.trace_id,
        **assignment.payload,
        "created_at": _iso(assignment.created_at),
    }


def exposure_payload(exposure: ExperimentExposure) -> dict[str, Any]:
    return {
        "exposure_id": exposure.exposure_id,
        "experiment_id": exposure.experiment_id,
        "assignment_id": exposure.assignment_id,
        "arm_key": exposure.arm_key,
        "occurred_at": _iso(exposure.occurred_at),
        "trace_id": exposure.trace_id,
        **exposure.payload,
        "created_at": _iso(exposure.created_at),
    }


def snapshot_payload(snapshot: ExperimentMetricSnapshot) -> dict[str, Any]:
    return {
        "metric_snapshot_id": snapshot.metric_snapshot_id,
        "experiment_id": snapshot.experiment_id,
        "snapshot_version": snapshot.snapshot_version,
        "verdict": snapshot.verdict,
        "primary_metric_key": snapshot.primary_metric_key,
        "evidence_sha256": snapshot.evidence_sha256,
        "scene_profile_id": snapshot.scene_profile_id,
        "scene_profile_version_id": snapshot.scene_profile_version_id,
        "scene_profile_snapshot_sha256": snapshot.scene_profile_snapshot_sha256,
        "trace_id": snapshot.trace_id,
        **snapshot.payload,
        "created_at": _iso(snapshot.created_at),
    }


def list_experiments(
    session: Session,
    ctx: RequestContext,
    page: dict[str, str | int | None],
    *,
    status: str | None = None,
) -> dict[str, Any]:
    statement = _experiment_query(ctx)
    count_statement = (
        select(func.count())
        .select_from(ControlledExperiment)
        .where(
            ControlledExperiment.tenant_id == ctx.tenant_id,
            ControlledExperiment.project_id == ctx.project_id,
        )
    )
    if status:
        statement = statement.where(ControlledExperiment.status == status)
        count_statement = count_statement.where(ControlledExperiment.status == status)
    cursor_created_at, cursor_experiment_id = decode_experiment_cursor(page.get("cursor"))
    if cursor_created_at is not None and cursor_experiment_id is not None:
        created_at_sort: Any = ControlledExperiment.created_at
        cursor_sort: datetime | str = cursor_created_at
        if session.get_bind().dialect.name == "sqlite":
            # SQLite CURRENT_TIMESTAMP is second precision while bound datetimes include
            # microseconds. Normalise both sides so a cursor never repeats its last row.
            created_at_sort = func.strftime(
                "%Y-%m-%d %H:%M:%f",
                ControlledExperiment.created_at,
            )
            cursor_sort = cursor_created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        statement = statement.where(
            or_(
                created_at_sort < cursor_sort,
                and_(
                    created_at_sort == cursor_sort,
                    ControlledExperiment.experiment_id < cursor_experiment_id,
                ),
            )
        )
    limit = int(page.get("limit") or 50)
    experiments = list(
        session.scalars(
            statement.order_by(
                ControlledExperiment.created_at.desc(),
                ControlledExperiment.experiment_id.desc(),
            ).limit(limit + 1)
        )
    )
    visible = experiments[:limit]
    items = [experiment_payload(session, item) for item in visible]
    return collection_envelope(
        items,
        ctx,
        total=int(session.scalar(count_statement) or 0),
        limit=limit,
        next_cursor=(
            encode_experiment_cursor(visible[-1]) if len(experiments) > limit and visible else None
        ),
    )


def get_experiment_detail(
    session: Session,
    ctx: RequestContext,
    experiment_id: str,
) -> dict[str, Any]:
    experiment = get_experiment(session, ctx, experiment_id)
    latest_snapshot = session.scalar(
        select(ExperimentMetricSnapshot)
        .where(
            ExperimentMetricSnapshot.tenant_id == ctx.tenant_id,
            ExperimentMetricSnapshot.project_id == ctx.project_id,
            ExperimentMetricSnapshot.experiment_id == experiment_id,
        )
        .order_by(ExperimentMetricSnapshot.snapshot_version.desc())
        .limit(1)
    )
    decisions = list(
        session.scalars(
            select(ExperimentDecision)
            .where(
                ExperimentDecision.tenant_id == ctx.tenant_id,
                ExperimentDecision.project_id == ctx.project_id,
                ExperimentDecision.experiment_id == experiment_id,
            )
            .order_by(ExperimentDecision.created_at.desc())
        )
    )
    detail = experiment_payload(session, experiment)
    detail["latest_metric_snapshot"] = (
        snapshot_payload(latest_snapshot) if latest_snapshot else None
    )
    detail["decisions"] = [
        {
            "decision_id": decision.decision_id,
            "decision": decision.decision,
            "metric_snapshot_id": decision.metric_snapshot_id,
            "reason": decision.reason,
            "decided_by": decision.decided_by,
            "trace_id": decision.trace_id,
            **decision.payload,
            "created_at": _iso(decision.created_at),
        }
        for decision in decisions
    ]
    return envelope(detail, ctx)


async def create_experiment(
    session: Session,
    ctx: RequestContext,
    request: Request,
    body: ExperimentCreateRequest,
) -> dict[str, Any]:
    require_any_role(ctx, EXPERIMENT_WRITE_ROLES, action="experiments.create")
    body_hash = await request_hash(request)
    operation = "experiments.create"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay

    experiment_id = body.experiment_id or f"exp_{uuid.uuid4().hex[:20]}"
    existing = session.get(ControlledExperiment, experiment_id)
    if existing is not None:
        raise ApiError("EXPERIMENT_ALREADY_EXISTS", "实验 ID 已存在", 409)

    active_binding = get_active_scene_binding(session, ctx, "production")
    scene_version = active_binding["version"]
    manifest = SceneProfileManifest.model_validate(scene_version["manifest"])
    if body.task_type_id not in set(manifest.task_type_refs):
        raise ApiError(
            "EXPERIMENT_TASK_TYPE_NOT_IN_SCENE",
            "实验任务类型未被当前 SceneProfile 声明",
            409,
            details=[{"task_type_id": body.task_type_id}],
        )
    metric_by_key = {metric.metric_key: metric for metric in manifest.metrics}
    requested_metric_keys = {
        body.primary_metric.metric_key,
        *(guardrail.metric_key for guardrail in body.guardrails),
    }
    unknown_metrics = sorted(requested_metric_keys - set(metric_by_key))
    if unknown_metrics:
        raise ApiError(
            "EXPERIMENT_METRIC_NOT_IN_SCENE",
            "实验指标未被当前 SceneProfile 声明",
            409,
            details=[{"metric_keys": unknown_metrics}],
        )

    task_versions = {
        arm.arm_key: _task_version(session, ctx, arm.task_version_id) for arm in body.arms
    }
    for arm_key, task_version in task_versions.items():
        data = task_version.data
        mismatches = {
            "task_type_id": (data.get("task_type_id"), body.task_type_id),
            "scene_profile_id": (
                data.get("scene_profile_id"),
                active_binding["scene_profile_id"],
            ),
            "scene_profile_version_id": (
                data.get("scene_profile_version_id"),
                active_binding["scene_profile_version_id"],
            ),
            "scene_profile_snapshot_sha256": (
                data.get("scene_profile_snapshot_sha256"),
                active_binding["manifest_sha256"],
            ),
        }
        invalid = [key for key, (actual, expected) in mismatches.items() if actual != expected]
        if invalid:
            raise ApiError(
                "EXPERIMENT_TASK_VERSION_SCENE_DRIFT",
                "实验 TaskVersion 与当前场景锁不一致",
                409,
                details=[
                    {
                        "arm_key": arm_key,
                        "task_version_id": task_version.resource_key,
                        "fields": invalid,
                    }
                ],
            )

    task_version_bundles = {
        arm_key: build_task_version_bundle(task_version.resource_key, task_version.data)
        for arm_key, task_version in task_versions.items()
    }
    bundle_diff = compare_task_version_bundles(
        task_version_bundles["control"], task_version_bundles["candidate"]
    )
    changed_dimensions = bundle_diff["changed_dimensions"]
    if not changed_dimensions:
        raise ApiError(
            "EXPERIMENT_NO_EFFECTIVE_TREATMENT",
            "对照与候选 TaskVersion 的执行语义完全相同，不能创建空实验",
            409,
        )
    if body.variant_dimension != "bundle" and changed_dimensions != [body.variant_dimension]:
        raise ApiError(
            "EXPERIMENT_VARIANT_DIMENSION_MISMATCH",
            "TaskVersion 实际差异与声明的实验变量不一致，已阻断混杂实验",
            409,
            details=[
                {
                    "declared_variant_dimension": body.variant_dimension,
                    "actual_changed_dimensions": changed_dimensions,
                    "diff_sha256": bundle_diff["diff_sha256"],
                }
            ],
        )

    arms = [
        {
            **arm.model_dump(mode="json"),
            "task_version_status": str(
                task_versions[arm.arm_key].data.get("status")
                or task_versions[arm.arm_key].status
                or ""
            ),
            "task_version_snapshot_sha256": _task_version_snapshot_sha256(
                task_versions[arm.arm_key]
            ),
            "task_version_behavior_sha256": task_version_bundles[arm.arm_key]["behavior_sha256"],
            "task_version_binding_sha256": task_version_bundles[arm.arm_key]["binding_sha256"],
            "component_fingerprints": task_version_bundles[arm.arm_key]["component_fingerprints"],
        }
        for arm in body.arms
    ]
    primary_metric = {
        **body.primary_metric.model_dump(mode="json"),
        "display_name": metric_by_key[body.primary_metric.metric_key].display_name,
        "unit": metric_by_key[body.primary_metric.metric_key].unit,
        "calculator_ref": metric_by_key[body.primary_metric.metric_key].calculator_ref,
    }
    guardrails = [
        {
            **guardrail.model_dump(mode="json"),
            "display_name": metric_by_key[guardrail.metric_key].display_name,
            "unit": metric_by_key[guardrail.metric_key].unit,
            "calculator_ref": metric_by_key[guardrail.metric_key].calculator_ref,
        }
        for guardrail in body.guardrails
    ]
    frozen_design = {
        "experiment_kind": body.experiment_kind,
        "variant_dimension": body.variant_dimension,
        "actual_changed_dimensions": changed_dimensions,
        "variant_diff_sha256": bundle_diff["diff_sha256"],
        "task_type_id": body.task_type_id,
        "hypothesis": body.hypothesis,
        "allocation_unit": body.allocation_unit,
        "arms": arms,
        "primary_metric": primary_metric,
        "guardrails": guardrails,
        "min_sample_size_per_arm": body.min_sample_size_per_arm,
        "confidence_level": body.confidence_level,
        "scene_profile_id": active_binding["scene_profile_id"],
        "scene_profile_version_id": active_binding["scene_profile_version_id"],
        "scene_profile_snapshot_sha256": active_binding["manifest_sha256"],
        "assignment_key_version": EXPERIMENT_ASSIGNMENT_KEY_VERSION,
    }
    design_sha256 = _sha256(frozen_design)
    created_at = datetime.now(UTC)
    control_arm = next(arm for arm in body.arms if arm.arm_key == "control")
    candidate_arm = next(arm for arm in body.arms if arm.arm_key == "candidate")
    experiment = ControlledExperiment(
        experiment_id=experiment_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        name=body.name,
        experiment_kind=body.experiment_kind,
        task_type_id=body.task_type_id,
        control_task_version_id=control_arm.task_version_id,
        candidate_task_version_id=candidate_arm.task_version_id,
        scene_profile_id=active_binding["scene_profile_id"],
        scene_profile_version_id=active_binding["scene_profile_version_id"],
        scene_profile_snapshot_sha256=active_binding["manifest_sha256"],
        design_sha256=design_sha256,
        status="draft",
        resource_version=1,
        trace_id=ctx.trace_id,
        payload={**frozen_design, "created_by": ctx.user_id},
        created_at=created_at,
        updated_at=created_at,
    )
    session.add(experiment)
    session.flush()
    response = envelope(experiment_payload(session, experiment), ctx)
    record_audit(
        session,
        ctx,
        action="experiment.create",
        object_type="controlled_experiment",
        object_id=experiment_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="experiment.created",
        aggregate_type="controlled_experiment",
        aggregate_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "design_sha256": design_sha256,
            "resource_version": experiment.resource_version,
        },
    )
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


async def start_experiment(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: ExperimentStartRequest,
) -> dict[str, Any]:
    require_any_role(ctx, EXPERIMENT_WRITE_ROLES, action="experiments.start")
    _human_only(ctx, action="experiments.start")
    body_hash = await request_hash(request)
    operation = f"experiments.start:{experiment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    experiment = get_experiment(session, ctx, experiment_id, for_update=True)
    if experiment.status != "draft":
        raise ApiError("EXPERIMENT_NOT_STARTABLE", "只有草稿实验可以启动", 409)
    if experiment.resource_version != body.expected_resource_version:
        raise ApiError(
            "EXPERIMENT_VERSION_CONFLICT",
            "实验版本冲突，请刷新后重试",
            409,
            details=[{"current_resource_version": experiment.resource_version}],
        )
    active = get_active_scene_binding(session, ctx, "production")
    if (
        active["scene_profile_id"] != experiment.scene_profile_id
        or active["scene_profile_version_id"] != experiment.scene_profile_version_id
        or active["manifest_sha256"] != experiment.scene_profile_snapshot_sha256
    ):
        raise ApiError("EXPERIMENT_SCENE_PROFILE_DRIFT", "实验冻结场景与当前生产绑定已漂移", 409)
    _assert_frozen_task_versions(session, ctx, experiment)
    experiment.status = "running"
    experiment.started_at = datetime.now(UTC)
    experiment.resource_version += 1
    experiment.trace_id = ctx.trace_id
    response = envelope(experiment_payload(session, experiment), ctx)
    record_audit(
        session,
        ctx,
        action="experiment.start",
        object_type="controlled_experiment",
        object_id=experiment_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="experiment.started",
        aggregate_type="controlled_experiment",
        aggregate_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "design_sha256": experiment.design_sha256,
            "resource_version": experiment.resource_version,
        },
    )
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    return response


def _arm_for_bucket(experiment: ControlledExperiment, bucket: int) -> str:
    cumulative = 0
    for arm in experiment.payload["arms"]:
        cumulative += int(arm["allocation_ppm"])
        if bucket < cumulative:
            return str(arm["arm_key"])
    raise ApiError("EXPERIMENT_ALLOCATION_INVALID", "实验分流区间不完整", 500)


def resolve_experiment_assignment(
    session: Session,
    ctx: RequestContext,
    experiment: ControlledExperiment,
    subject_key: str,
) -> ExperimentAssignment:
    if experiment.status != "running":
        raise ApiError("EXPERIMENT_NOT_RUNNING", "实验未运行，不能分配新主体", 409)
    subject_key_sha256 = _hmac_sha256(
        f"{ctx.tenant_id}:{ctx.project_id}:{experiment.experiment_id}:{subject_key}"
    )
    existing = session.scalar(
        select(ExperimentAssignment).where(
            ExperimentAssignment.tenant_id == ctx.tenant_id,
            ExperimentAssignment.project_id == ctx.project_id,
            ExperimentAssignment.experiment_id == experiment.experiment_id,
            ExperimentAssignment.subject_key_sha256 == subject_key_sha256,
        )
    )
    if existing is not None:
        return existing
    assignment_digest = _hmac_sha256(f"{experiment.design_sha256}:{subject_key_sha256}:assignment")
    bucket = int(assignment_digest[:16], 16) % 1_000_000
    assignment = ExperimentAssignment(
        assignment_id=f"assign_{assignment_digest[:24]}",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        experiment_id=experiment.experiment_id,
        subject_key_sha256=subject_key_sha256,
        arm_key=_arm_for_bucket(experiment, bucket),
        assignment_bucket=bucket,
        design_sha256=experiment.design_sha256,
        trace_id=ctx.trace_id,
        payload={"allocation_unit": experiment.payload["allocation_unit"]},
    )
    session.add(assignment)
    session.flush()
    record_audit(
        session,
        ctx,
        action="experiment.assignment.create",
        object_type="experiment_assignment",
        object_id=assignment.assignment_id,
        after=assignment_payload(assignment),
    )
    return assignment


def resolve_experiment_exposure(
    session: Session,
    ctx: RequestContext,
    experiment: ControlledExperiment,
    assignment: ExperimentAssignment,
    *,
    exposure_key: str,
    occurred_at: datetime,
    context_refs: dict[str, str],
) -> ExperimentExposure:
    if experiment.status != "running":
        raise ApiError("EXPERIMENT_NOT_RUNNING", "实验未运行，不能记录曝光", 409)
    exposure_key_sha256 = _hmac_sha256(
        ":".join(
            (
                ctx.tenant_id,
                ctx.project_id,
                experiment.experiment_id,
                assignment.assignment_id,
                experiment.design_sha256,
                exposure_key,
                "exposure",
            )
        )
    )
    existing = session.scalar(
        select(ExperimentExposure).where(
            ExperimentExposure.tenant_id == ctx.tenant_id,
            ExperimentExposure.project_id == ctx.project_id,
            ExperimentExposure.experiment_id == experiment.experiment_id,
            ExperimentExposure.exposure_key_sha256 == exposure_key_sha256,
        )
    )
    if existing is not None:
        if existing.assignment_id != assignment.assignment_id:
            raise ApiError("EXPERIMENT_EXPOSURE_CONFLICT", "曝光键已绑定其他实验主体", 409)
        return existing
    exposure = ExperimentExposure(
        exposure_id=f"exposure_{exposure_key_sha256[:24]}",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        experiment_id=experiment.experiment_id,
        assignment_id=assignment.assignment_id,
        exposure_key_sha256=exposure_key_sha256,
        arm_key=assignment.arm_key,
        occurred_at=occurred_at,
        trace_id=ctx.trace_id,
        payload={"context_refs": context_refs},
    )
    session.add(exposure)
    session.flush()
    record_audit(
        session,
        ctx,
        action="experiment.exposure.create",
        object_type="experiment_exposure",
        object_id=exposure.exposure_id,
        after=exposure_payload(exposure),
    )
    return exposure


def bind_task_run_to_experiment(
    session: Session,
    ctx: RequestContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    prepared = dict(payload)
    experiment_id = str(prepared.get("experiment_id") or "").strip()
    subject_key = str(prepared.pop("experiment_subject_key", "") or "").strip()
    if not experiment_id and not subject_key:
        return prepared
    if not experiment_id or not subject_key:
        raise ApiError(
            "EXPERIMENT_TASK_RUN_BINDING_INCOMPLETE",
            "实验任务运行必须同时提供 experiment_id 和临时分流主体键",
            422,
        )
    if str(prepared.get("execution_mode") or "") != "experiment":
        raise ApiError(
            "EXPERIMENT_TASK_RUN_MODE_REQUIRED",
            "实验任务运行必须使用 execution_mode=experiment",
            409,
        )
    experiment = get_experiment(session, ctx, experiment_id)
    if experiment.experiment_kind != "task_version":
        raise ApiError(
            "EXPERIMENT_TASK_RUN_KIND_INVALID",
            "只有 task_version 实验可以直接分流 TaskRun",
            409,
        )
    task_versions = _assert_frozen_task_versions(session, ctx, experiment)
    requested_task_version_id = str(prepared.get("task_version_id") or "").strip()
    arm_versions = {
        str(arm["arm_key"]): str(arm["task_version_id"]) for arm in experiment.payload["arms"]
    }
    if requested_task_version_id not in set(arm_versions.values()):
        raise ApiError(
            "EXPERIMENT_TASK_VERSION_NOT_IN_ARMS",
            "TaskRun 请求版本不属于当前实验对照或候选版本",
            409,
        )
    assignment = resolve_experiment_assignment(session, ctx, experiment, subject_key)
    selected_task_version = task_versions[assignment.arm_key]
    selected_arm = next(
        arm for arm in experiment.payload["arms"] if arm["arm_key"] == assignment.arm_key
    )
    execution = selected_task_version.data.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    authoritative_job_name = str(execution.get("job_name") or selected_task_version.resource_key)
    authoritative_run_config = execution.get("run_config")
    authoritative_run_config = (
        dict(authoritative_run_config) if isinstance(authoritative_run_config, dict) else {}
    )
    run_key = str(prepared.get("run_key") or ctx.trace_id)
    exposure = resolve_experiment_exposure(
        session,
        ctx,
        experiment,
        assignment,
        exposure_key=f"task-run:{run_key}",
        occurred_at=datetime.now(UTC),
        context_refs={
            "run_key": run_key,
            "task_type_id": experiment.task_type_id,
            "partition_key": str(prepared.get("partition_key") or "unpartitioned"),
        },
    )
    for field in (
        "job_name",
        "run_config",
        "dagster_run_draft",
        "asset_selection",
        "canvas_variant",
    ):
        prepared.pop(field, None)
    return {
        **prepared,
        "task_version_id": arm_versions[assignment.arm_key],
        "requested_task_version_id": requested_task_version_id,
        "experiment_id": experiment.experiment_id,
        "experiment_assignment_id": assignment.assignment_id,
        "experiment_exposure_id": exposure.exposure_id,
        "experiment_arm": assignment.arm_key,
        "experiment_design_sha256": experiment.design_sha256,
        "experiment_subject_key_sha256": assignment.subject_key_sha256,
        "experiment_variant_dimension": experiment.payload["variant_dimension"],
        "experiment_variant_diff_sha256": experiment.payload["variant_diff_sha256"],
        "task_version_behavior_sha256": selected_arm["task_version_behavior_sha256"],
        "task_version_binding_sha256": selected_arm["task_version_binding_sha256"],
        "expected_executed_bundle_sha256": selected_arm["task_version_binding_sha256"],
        "job_name": authoritative_job_name,
        "run_config": {
            **authoritative_run_config,
            "auris_task_version": {
                "task_version_id": selected_task_version.resource_key,
                "behavior_sha256": selected_arm["task_version_behavior_sha256"],
                "binding_sha256": selected_arm["task_version_binding_sha256"],
            },
        },
        "scene_profile_id": experiment.scene_profile_id,
        "scene_profile_version_id": experiment.scene_profile_version_id,
        "scene_profile_snapshot_sha256": experiment.scene_profile_snapshot_sha256,
    }


def resolve_experiment_outcomes(
    session: Session,
    ctx: RequestContext,
    experiment: ControlledExperiment,
    exposure: ExperimentExposure,
    *,
    metric_values: dict[str, float],
    occurred_at: datetime,
    evidence_refs: list[str],
    source_fact: dict[str, Any],
) -> list[dict[str, Any]]:
    if experiment.status not in {"running", "paused", "stopped"}:
        raise ApiError("EXPERIMENT_OUTCOME_NOT_ACCEPTED", "当前实验状态不能接收结果", 409)
    allowed_metrics = {
        experiment.payload["primary_metric"]["metric_key"],
        *(item["metric_key"] for item in experiment.payload.get("guardrails", [])),
    }
    unknown = sorted(set(metric_values) - allowed_metrics)
    if unknown:
        raise ApiError(
            "EXPERIMENT_OUTCOME_METRIC_NOT_DECLARED",
            "结果包含实验设计外的指标",
            409,
            details=[{"metric_keys": unknown}],
        )
    evidence_document = {
        "experiment_id": experiment.experiment_id,
        "design_sha256": experiment.design_sha256,
        "scene_profile_id": experiment.scene_profile_id,
        "scene_profile_version_id": experiment.scene_profile_version_id,
        "scene_profile_snapshot_sha256": experiment.scene_profile_snapshot_sha256,
        "assignment_id": exposure.assignment_id,
        "exposure_id": exposure.exposure_id,
        "arm_key": exposure.arm_key,
        "metric_values": dict(sorted(metric_values.items())),
        "evidence_refs": sorted(evidence_refs),
        "source_fact": source_fact,
    }
    evidence_sha256 = _sha256(evidence_document)
    items: list[dict[str, Any]] = []
    for metric_key, value in sorted(metric_values.items()):
        existing = session.scalar(
            select(ExperimentOutcome).where(
                ExperimentOutcome.tenant_id == ctx.tenant_id,
                ExperimentOutcome.project_id == ctx.project_id,
                ExperimentOutcome.experiment_id == experiment.experiment_id,
                ExperimentOutcome.exposure_id == exposure.exposure_id,
                ExperimentOutcome.metric_key == metric_key,
            )
        )
        if existing is not None:
            if existing.value != value or existing.evidence_sha256 != evidence_sha256:
                raise ApiError("EXPERIMENT_OUTCOME_CONFLICT", "同一曝光指标已有不同结果", 409)
            outcome = existing
        else:
            outcome_digest = _sha256(
                {
                    "experiment_id": experiment.experiment_id,
                    "exposure_id": exposure.exposure_id,
                    "metric_key": metric_key,
                }
            )
            outcome = ExperimentOutcome(
                outcome_id=f"outcome_{outcome_digest[:24]}",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                experiment_id=experiment.experiment_id,
                exposure_id=exposure.exposure_id,
                arm_key=exposure.arm_key,
                metric_key=metric_key,
                value=value,
                occurred_at=occurred_at,
                evidence_sha256=evidence_sha256,
                trace_id=ctx.trace_id,
                payload={
                    "evidence_refs": evidence_refs,
                    "source_fact": source_fact,
                    "evidence_document_sha256": evidence_sha256,
                },
            )
            session.add(outcome)
        items.append(
            {
                "outcome_id": outcome.outcome_id,
                "exposure_id": outcome.exposure_id,
                "arm_key": outcome.arm_key,
                "metric_key": outcome.metric_key,
                "value": outcome.value,
                "evidence_sha256": outcome.evidence_sha256,
                "trace_id": outcome.trace_id,
                "source_kind": (outcome.payload.get("source_fact") or {}).get("source_kind"),
                "source_run_id": (outcome.payload.get("source_fact") or {}).get("run_id"),
                "completion_receipt_id": (outcome.payload.get("source_fact") or {}).get(
                    "completion_receipt_id"
                ),
            }
        )
    session.flush()
    return items


def materialize_task_experiment_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "task_run" or not record.payload.get("experiment_id"):
        return None
    completion_auth = completion_receipt.get("auth") or {}
    signed_external = (
        ctx.actor_kind == "service"
        and completion_auth.get("auth_mode") == "signed_external_completion"
        and completion_auth.get("request_sha256")
        and completion_auth.get("signature_key_id")
    )
    local_system = ctx.actor_kind == "system" and "system" in ctx.roles
    if not signed_external and not local_system:
        raise ApiError(
            "EXPERIMENT_SIGNED_COMPLETION_REQUIRED",
            "实验指标只能由签名 TaskRun 完成回执物化，人工或模型不能直接提交结果",
            403,
        )
    experiment_id = str(record.payload["experiment_id"])
    experiment = get_experiment(session, ctx, experiment_id)
    frozen_versions = _assert_frozen_task_versions(session, ctx, experiment)
    exposure_id = str(record.payload.get("experiment_exposure_id") or "")
    exposure = session.scalar(
        select(ExperimentExposure).where(
            ExperimentExposure.tenant_id == ctx.tenant_id,
            ExperimentExposure.project_id == ctx.project_id,
            ExperimentExposure.experiment_id == experiment_id,
            ExperimentExposure.exposure_id == exposure_id,
        )
    )
    if exposure is None:
        raise ApiError(
            "EXPERIMENT_EXPOSURE_NOT_FOUND",
            "实验 TaskRun 的曝光事实不存在，完成回执已阻断",
            409,
        )
    arm_key = str(record.payload.get("experiment_arm") or "")
    expected_task_version_id = str(
        next(
            (
                arm["task_version_id"]
                for arm in experiment.payload["arms"]
                if arm["arm_key"] == arm_key
            ),
            "",
        )
    )
    expected_task_version = frozen_versions.get(arm_key)
    lineage_mismatch = (
        exposure.arm_key != arm_key
        or str(record.payload.get("experiment_exposure_id") or "") != exposure.exposure_id
        or str(record.payload.get("task_version_id") or "") != expected_task_version_id
        or expected_task_version is None
        or record.payload.get("scene_profile_id") != experiment.scene_profile_id
        or record.payload.get("scene_profile_version_id") != experiment.scene_profile_version_id
        or record.payload.get("scene_profile_snapshot_sha256")
        != experiment.scene_profile_snapshot_sha256
    )
    if lineage_mismatch:
        raise ApiError(
            "EXPERIMENT_COMPLETION_LINEAGE_INVALID",
            "实验完成回执与冻结分流、任务版本或场景快照不一致",
            409,
            details=[{"run_id": record.run_id, "exposure_id": exposure.exposure_id}],
        )
    assert expected_task_version is not None
    declared_metrics = {
        experiment.payload["primary_metric"]["metric_key"],
        *(item["metric_key"] for item in experiment.payload.get("guardrails", [])),
    }
    raw_metrics = completion_receipt.get("metrics") or {}
    missing = sorted(declared_metrics - set(raw_metrics))
    if missing:
        raise ApiError(
            "EXPERIMENT_COMPLETION_METRICS_REQUIRED",
            "实验 TaskRun 完成回执缺少声明指标",
            422,
            details=[{"metric_keys": missing}],
        )
    metric_values: dict[str, float] = {}
    for metric_key in sorted(declared_metrics):
        value = raw_metrics[metric_key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ApiError(
                "EXPERIMENT_COMPLETION_METRIC_INVALID",
                "实验指标必须是有限数值",
                422,
                details=[{"metric_key": metric_key}],
            )
        metric_values[metric_key] = float(value)
    result_ref = completion_receipt.get("result_ref") or {}
    raw_evidence_refs = result_ref.get("evidence_refs") if isinstance(result_ref, dict) else None
    evidence_refs = (
        [str(item) for item in raw_evidence_refs if str(item).strip()]
        if isinstance(raw_evidence_refs, list)
        else []
    )
    if not evidence_refs:
        evidence_refs = [
            f"run:{record.run_id}",
            f"completion:{completion_receipt['completion_receipt_id']}",
        ]
    source_fact = {
        "source_kind": "signed_task_run_completion"
        if signed_external
        else "local_system_completion",
        "run_id": record.run_id,
        "run_trace_id": record.trace_id,
        "completion_receipt_id": completion_receipt["completion_receipt_id"],
        "completion_receipt_sha256": completion_receipt["receipt_hash"],
        "adapter": completion_receipt.get("adapter"),
        "external_id": completion_receipt.get("external_id"),
        "task_version_id": expected_task_version_id,
        "task_version_snapshot_sha256": _task_version_snapshot_sha256(expected_task_version),
        "signature_key_id": completion_auth.get("signature_key_id"),
        "signature_request_sha256": completion_auth.get("request_sha256"),
        "signature_nonce": completion_auth.get("nonce"),
    }
    outcomes = resolve_experiment_outcomes(
        session,
        ctx,
        experiment,
        exposure,
        metric_values=metric_values,
        occurred_at=datetime.now(UTC),
        evidence_refs=evidence_refs,
        source_fact=source_fact,
    )
    result = {
        "experiment_id": experiment_id,
        "assignment_id": record.payload.get("experiment_assignment_id"),
        "exposure_id": exposure.exposure_id,
        "arm_key": exposure.arm_key,
        "outcome_ids": [item["outcome_id"] for item in outcomes],
        "metric_keys": sorted(metric_values),
        "design_sha256": experiment.design_sha256,
        "source_kind": source_fact["source_kind"],
        "completion_receipt_id": source_fact["completion_receipt_id"],
        "completion_receipt_sha256": source_fact["completion_receipt_sha256"],
    }
    record_audit(
        session,
        ctx,
        action="experiment.task_run.completed",
        object_type="controlled_experiment",
        object_id=experiment_id,
        after=result,
        trace_id=record.trace_id,
    )
    return result


async def assign_experiment_subject(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: ExperimentAssignmentRequest,
) -> dict[str, Any]:
    require_any_role(ctx, EXPERIMENT_EVENT_ROLES, action="experiments.assign")
    body_hash = await request_hash(request)
    operation = f"experiments.assign:{experiment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    experiment = get_experiment(session, ctx, experiment_id)
    assignment = resolve_experiment_assignment(session, ctx, experiment, body.subject_key)
    response = envelope(assignment_payload(assignment), ctx)
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


async def record_experiment_exposure(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: ExperimentExposureRequest,
) -> dict[str, Any]:
    require_any_role(ctx, EXPERIMENT_EVENT_ROLES, action="experiments.expose")
    body_hash = await request_hash(request)
    operation = f"experiments.expose:{experiment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    experiment = get_experiment(session, ctx, experiment_id)
    assignment = session.scalar(
        select(ExperimentAssignment).where(
            ExperimentAssignment.tenant_id == ctx.tenant_id,
            ExperimentAssignment.project_id == ctx.project_id,
            ExperimentAssignment.experiment_id == experiment_id,
            ExperimentAssignment.assignment_id == body.assignment_id,
        )
    )
    if assignment is None:
        raise ApiError("EXPERIMENT_ASSIGNMENT_NOT_FOUND", "实验分配记录不存在", 404)
    exposure = resolve_experiment_exposure(
        session,
        ctx,
        experiment,
        assignment,
        exposure_key=body.exposure_key,
        occurred_at=body.occurred_at,
        context_refs=dict(body.context_refs),
    )
    response = envelope(exposure_payload(exposure), ctx)
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


async def record_experiment_outcome(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: ExperimentOutcomeRequest,
) -> dict[str, Any]:
    del session, ctx, request, experiment_id, body
    raise ApiError(
        "EXPERIMENT_OUTCOME_DIRECT_WRITE_FORBIDDEN",
        "实验结果不能通过业务 API 直接写入，请提交绑定 TaskRun 的签名完成回执",
        403,
    )


def _metric_statistics(
    control_values: list[float],
    candidate_values: list[float],
    *,
    confidence_level: float,
) -> dict[str, Any]:
    control_value = mean(control_values) if control_values else None
    candidate_value = mean(candidate_values) if candidate_values else None
    if control_value is None or candidate_value is None:
        return {
            "control_value": control_value,
            "candidate_value": candidate_value,
            "delta": None,
            "confidence_low": None,
            "confidence_high": None,
            "p_value": None,
        }
    delta = candidate_value - control_value
    control_variance = variance(control_values) if len(control_values) > 1 else 0.0
    candidate_variance = variance(candidate_values) if len(candidate_values) > 1 else 0.0
    standard_error = math.sqrt(
        control_variance / len(control_values) + candidate_variance / len(candidate_values)
    )
    if standard_error == 0:
        p_value = 1.0 if delta == 0 else 0.0
        confidence_low = confidence_high = delta
    else:
        z_score = delta / standard_error
        p_value = math.erfc(abs(z_score) / math.sqrt(2))
        margin = Z_CRITICAL[confidence_level] * standard_error
        confidence_low = delta - margin
        confidence_high = delta + margin
    return {
        "control_value": control_value,
        "candidate_value": candidate_value,
        "delta": delta,
        "confidence_low": confidence_low,
        "confidence_high": confidence_high,
        "p_value": p_value,
    }


def _sample_ratio_diagnostic(
    counts: dict[str, int],
    arms: list[dict[str, Any]],
    *,
    alpha: float = EXPERIMENT_SAMPLE_RATIO_ALPHA,
) -> dict[str, Any]:
    total = sum(int(counts.get(arm_key, 0)) for arm_key in ("control", "candidate"))
    expected_ppm = {str(arm["arm_key"]): int(arm["allocation_ppm"]) for arm in arms}
    observed_ppm = {
        arm_key: (int(counts.get(arm_key, 0)) * 1_000_000 / total if total else None)
        for arm_key in ("control", "candidate")
    }
    if total == 0:
        return {
            "total": 0,
            "counts": {
                arm_key: int(counts.get(arm_key, 0)) for arm_key in ("control", "candidate")
            },
            "expected_ppm": expected_ppm,
            "observed_ppm": observed_ppm,
            "chi_square": None,
            "p_value": None,
            "alpha": alpha,
            "detected": False,
        }
    chi_square = 0.0
    for arm_key in ("control", "candidate"):
        expected_count = total * expected_ppm[arm_key] / 1_000_000
        if expected_count <= 0:
            raise ApiError(
                "EXPERIMENT_ALLOCATION_INVALID",
                "实验分流比例无法用于样本比例诊断",
                409,
            )
        chi_square += (int(counts.get(arm_key, 0)) - expected_count) ** 2 / expected_count
    p_value = math.erfc(math.sqrt(chi_square / 2))
    return {
        "total": total,
        "counts": {arm_key: int(counts.get(arm_key, 0)) for arm_key in ("control", "candidate")},
        "expected_ppm": expected_ppm,
        "observed_ppm": observed_ppm,
        "chi_square": chi_square,
        "p_value": p_value,
        "alpha": alpha,
        "detected": p_value < alpha,
    }


def _experiment_outcomes(
    session: Session,
    ctx: RequestContext,
    experiment_id: str,
    *,
    metric_keys: list[str] | None = None,
) -> list[ExperimentOutcome]:
    statement = select(ExperimentOutcome).where(
        ExperimentOutcome.tenant_id == ctx.tenant_id,
        ExperimentOutcome.project_id == ctx.project_id,
        ExperimentOutcome.experiment_id == experiment_id,
    )
    if metric_keys:
        statement = statement.where(ExperimentOutcome.metric_key.in_(metric_keys))
    return list(session.scalars(statement))


def _experiment_evidence_sha256(
    experiment: ControlledExperiment,
    outcomes: list[ExperimentOutcome],
) -> str:
    evidence_document = [
        {
            "outcome_id": outcome.outcome_id,
            "exposure_id": outcome.exposure_id,
            "arm_key": outcome.arm_key,
            "metric_key": outcome.metric_key,
            "value": outcome.value,
            "evidence_sha256": outcome.evidence_sha256,
        }
        for outcome in sorted(outcomes, key=lambda item: item.outcome_id)
    ]
    return _sha256(
        {
            "design_sha256": experiment.design_sha256,
            "outcomes": evidence_document,
        }
    )


def _metric_values_by_assignment(
    session: Session,
    ctx: RequestContext,
    experiment: ControlledExperiment,
    outcomes: list[ExperimentOutcome],
    metric_keys: list[str],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, int]]:
    """Aggregate repeated exposures before statistical analysis.

    The experiment allocation unit is represented by one immutable assignment. A
    subject may generate multiple runs or retries, but those observations cannot
    be counted as independent samples. We average repeated observations for the
    same assignment and metric, then run statistics over unique assignments.
    """

    exposures = list(
        session.scalars(
            select(ExperimentExposure).where(
                ExperimentExposure.tenant_id == ctx.tenant_id,
                ExperimentExposure.project_id == ctx.project_id,
                ExperimentExposure.experiment_id == experiment.experiment_id,
            )
        )
    )
    exposure_assignments = {
        exposure.exposure_id: (exposure.assignment_id, exposure.arm_key) for exposure in exposures
    }
    grouped: dict[str, dict[str, dict[str, list[float]]]] = {
        metric_key: {"control": {}, "candidate": {}} for metric_key in metric_keys
    }
    for outcome in outcomes:
        assignment = exposure_assignments.get(outcome.exposure_id)
        if assignment is None or outcome.metric_key not in grouped:
            continue
        assignment_id, exposure_arm = assignment
        if exposure_arm != outcome.arm_key or exposure_arm not in grouped[outcome.metric_key]:
            raise ApiError(
                "EXPERIMENT_OUTCOME_LINEAGE_INVALID",
                "实验结果与曝光分流血缘不一致",
                409,
                details=[{"outcome_id": outcome.outcome_id}],
            )
        grouped[outcome.metric_key][exposure_arm].setdefault(assignment_id, []).append(
            outcome.value
        )
    values = {
        metric_key: {
            arm_key: [mean(items) for items in assignments.values()]
            for arm_key, assignments in arms.items()
        }
        for metric_key, arms in grouped.items()
    }
    assignment_ids = {
        assignment_id
        for arms in grouped.values()
        for assignments in arms.values()
        for assignment_id in assignments
    }
    return values, {
        "distinct_assignments": len(assignment_ids),
        "outcome_count": len(outcomes),
    }


async def compute_experiment_metric_snapshot(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
) -> dict[str, Any]:
    require_any_role(ctx, EXPERIMENT_WRITE_ROLES, action="experiments.compute_metrics")
    body_hash = await request_hash(request)
    operation = f"experiments.metric_snapshot:{experiment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    experiment = get_experiment(session, ctx, experiment_id)
    if experiment.status not in {"running", "paused", "stopped"}:
        raise ApiError("EXPERIMENT_NOT_MEASURABLE", "当前实验状态不能计算指标", 409)
    metric_keys = [
        experiment.payload["primary_metric"]["metric_key"],
        *(item["metric_key"] for item in experiment.payload.get("guardrails", [])),
    ]
    _assert_frozen_task_versions(session, ctx, experiment)
    outcomes = _experiment_outcomes(
        session,
        ctx,
        experiment_id,
        metric_keys=metric_keys,
    )
    untrusted_outcomes = [
        outcome.outcome_id
        for outcome in outcomes
        if (outcome.payload.get("source_fact") or {}).get("source_kind")
        not in {"signed_task_run_completion", "local_system_completion"}
    ]
    if untrusted_outcomes:
        raise ApiError(
            "EXPERIMENT_OUTCOME_PROVENANCE_INVALID",
            "实验指标包含无法回放的非受信结果，已阻断统计",
            409,
            details=[{"outcome_ids": sorted(untrusted_outcomes)}],
        )
    source_facts = [outcome.payload.get("source_fact") or {} for outcome in outcomes]
    source_run_ids = sorted(
        {
            str(fact.get("run_id"))
            for fact in source_facts
            if isinstance(fact.get("run_id"), str) and fact.get("run_id")
        }
    )
    completion_receipt_ids = sorted(
        {
            str(fact.get("completion_receipt_id"))
            for fact in source_facts
            if isinstance(fact.get("completion_receipt_id"), str)
            and fact.get("completion_receipt_id")
        }
    )
    source_kinds = sorted(
        {
            str(fact.get("source_kind"))
            for fact in source_facts
            if isinstance(fact.get("source_kind"), str) and fact.get("source_kind")
        }
    )
    fact_source = (
        source_kinds[0]
        if len(source_kinds) == 1
        else "mixed_trusted_completion"
        if source_kinds
        else "no_observations"
    )
    values, aggregation = _metric_values_by_assignment(
        session,
        ctx,
        experiment,
        outcomes,
        metric_keys,
    )
    confidence_level = float(experiment.payload["confidence_level"])
    statistics = {
        metric_key: _metric_statistics(
            values[metric_key]["control"],
            values[metric_key]["candidate"],
            confidence_level=confidence_level,
        )
        for metric_key in metric_keys
    }
    primary_design = experiment.payload["primary_metric"]
    primary_stats = statistics[primary_design["metric_key"]]
    sample_sizes = {
        "control": len(values[primary_design["metric_key"]]["control"]),
        "candidate": len(values[primary_design["metric_key"]]["candidate"]),
    }
    assignment_rows = list(
        session.scalars(
            select(ExperimentAssignment).where(
                ExperimentAssignment.tenant_id == ctx.tenant_id,
                ExperimentAssignment.project_id == ctx.project_id,
                ExperimentAssignment.experiment_id == experiment_id,
            )
        )
    )
    assignment_counts = {
        arm_key: sum(1 for assignment in assignment_rows if assignment.arm_key == arm_key)
        for arm_key in ("control", "candidate")
    }
    assignment_ratio = _sample_ratio_diagnostic(
        assignment_counts,
        experiment.payload["arms"],
    )
    analysis_sample_ratio = _sample_ratio_diagnostic(
        sample_sizes,
        experiment.payload["arms"],
    )
    sample_ratio_mismatch = bool(assignment_ratio["detected"] or analysis_sample_ratio["detected"])
    completion_rates = {
        arm_key: (
            sample_sizes[arm_key] / assignment_counts[arm_key]
            if assignment_counts[arm_key]
            else None
        )
        for arm_key in ("control", "candidate")
    }
    minimum_samples = int(experiment.payload["min_sample_size_per_arm"])
    primary_enough = min(sample_sizes.values()) >= minimum_samples
    delta = primary_stats["delta"]
    if delta is None:
        primary_pass = False
    elif primary_design["direction"] == "increase":
        primary_pass = primary_stats["confidence_low"] is not None and primary_stats[
            "confidence_low"
        ] >= float(primary_design["minimum_effect"])
    else:
        primary_pass = primary_stats["confidence_high"] is not None and primary_stats[
            "confidence_high"
        ] <= -float(primary_design["minimum_effect"])
    guardrail_results: list[dict[str, Any]] = []
    guardrails_enough = True
    guardrails_pass = True
    for design in experiment.payload.get("guardrails", []):
        stats = statistics[design["metric_key"]]
        sizes = {
            "control": len(values[design["metric_key"]]["control"]),
            "candidate": len(values[design["metric_key"]]["candidate"]),
        }
        enough = min(sizes.values()) >= minimum_samples
        guardrails_enough = guardrails_enough and enough
        guard_delta = stats["delta"]
        if guard_delta is None:
            passed = False
        elif design["direction"] == "increase":
            passed = stats["confidence_low"] is not None and stats["confidence_low"] >= -float(
                design["maximum_regression"]
            )
        else:
            passed = stats["confidence_high"] is not None and stats["confidence_high"] <= float(
                design["maximum_regression"]
            )
        guardrails_pass = guardrails_pass and passed
        guardrail_results.append(
            {
                **design,
                **stats,
                "sample_sizes": sizes,
                "status": "insufficient_sample" if not enough else "pass" if passed else "fail",
            }
        )
    if not primary_enough or not guardrails_enough:
        verdict = "insufficient_sample"
    elif sample_ratio_mismatch:
        verdict = "blocked_sample_ratio"
    elif not guardrails_pass:
        verdict = "blocked_guardrail"
    elif primary_pass:
        verdict = "promote"
    else:
        verdict = "hold"

    current_version = int(
        session.scalar(
            select(func.max(ExperimentMetricSnapshot.snapshot_version)).where(
                ExperimentMetricSnapshot.tenant_id == ctx.tenant_id,
                ExperimentMetricSnapshot.project_id == ctx.project_id,
                ExperimentMetricSnapshot.experiment_id == experiment_id,
            )
        )
        or 0
    )
    snapshot_version = current_version + 1
    evidence_sha256 = _experiment_evidence_sha256(experiment, outcomes)
    snapshot = ExperimentMetricSnapshot(
        metric_snapshot_id=f"expm_{uuid.uuid4().hex[:20]}",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        experiment_id=experiment_id,
        snapshot_version=snapshot_version,
        verdict=verdict,
        primary_metric_key=primary_design["metric_key"],
        evidence_sha256=evidence_sha256,
        scene_profile_id=experiment.scene_profile_id,
        scene_profile_version_id=experiment.scene_profile_version_id,
        scene_profile_snapshot_sha256=experiment.scene_profile_snapshot_sha256,
        trace_id=ctx.trace_id,
        payload={
            "design_sha256": experiment.design_sha256,
            "experiment_resource_version": experiment.resource_version,
            "sample_sizes": sample_sizes,
            "assignment_counts": assignment_counts,
            "completion_rates": completion_rates,
            "sample_ratio_diagnostic": {
                "status": "blocked" if sample_ratio_mismatch else "pass",
                "detected": sample_ratio_mismatch,
                "assignment": assignment_ratio,
                "analysis_sample": analysis_sample_ratio,
            },
            "analysis_unit": experiment.payload["allocation_unit"],
            **aggregation,
            "fact_source": fact_source,
            "source_kinds": source_kinds,
            "source_run_count": len(source_run_ids),
            "completion_receipt_count": len(completion_receipt_ids),
            "source_run_ids_sha256": _sha256(source_run_ids),
            "completion_receipt_ids_sha256": _sha256(completion_receipt_ids),
            "min_sample_size_per_arm": minimum_samples,
            "confidence_level": confidence_level,
            "primary_metric": {
                **primary_design,
                **primary_stats,
                "status": (
                    "insufficient_sample"
                    if not primary_enough
                    else "pass"
                    if primary_pass
                    else "hold"
                ),
            },
            "guardrails": guardrail_results,
            "calculator_refs": {
                metric_key: (
                    experiment.payload["primary_metric"].get("calculator_ref")
                    if metric_key == primary_design["metric_key"]
                    else next(
                        (
                            item.get("calculator_ref")
                            for item in experiment.payload.get("guardrails", [])
                            if item["metric_key"] == metric_key
                        ),
                        None,
                    )
                )
                for metric_key in metric_keys
            },
            "calculator_engine": "auris.experiment.metric-engine/v2",
            "computed_by": ctx.user_id,
            "computed_at": datetime.now(UTC).isoformat(),
        },
    )
    session.add(snapshot)
    session.flush()
    response = envelope(snapshot_payload(snapshot), ctx)
    record_audit(
        session,
        ctx,
        action="experiment.metric_snapshot.create",
        object_type="experiment_metric_snapshot",
        object_id=snapshot.metric_snapshot_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="experiment.metric-snapshot-created",
        aggregate_type="controlled_experiment",
        aggregate_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "metric_snapshot_id": snapshot.metric_snapshot_id,
            "snapshot_version": snapshot.snapshot_version,
            "verdict": verdict,
            "evidence_sha256": evidence_sha256,
        },
    )
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


async def record_experiment_decision(
    session: Session,
    ctx: RequestContext,
    request: Request,
    experiment_id: str,
    body: ExperimentDecisionRequest,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin",), action="experiments.decide")
    _human_only(ctx, action="experiments.decide")
    body_hash = await request_hash(request)
    operation = f"experiments.decision:{experiment_id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    experiment = get_experiment(session, ctx, experiment_id, for_update=True)
    if experiment.resource_version != body.expected_resource_version:
        raise ApiError(
            "EXPERIMENT_VERSION_CONFLICT",
            "实验版本冲突，请刷新后重试",
            409,
            details=[{"current_resource_version": experiment.resource_version}],
        )
    snapshot = None
    if body.metric_snapshot_id:
        snapshot = session.scalar(
            select(ExperimentMetricSnapshot).where(
                ExperimentMetricSnapshot.tenant_id == ctx.tenant_id,
                ExperimentMetricSnapshot.project_id == ctx.project_id,
                ExperimentMetricSnapshot.experiment_id == experiment_id,
                ExperimentMetricSnapshot.metric_snapshot_id == body.metric_snapshot_id,
            )
        )
        if snapshot is None:
            raise ApiError("EXPERIMENT_METRIC_SNAPSHOT_NOT_FOUND", "实验指标快照不存在", 404)

    allowed_states = {
        "pause": {"running"},
        "resume": {"paused"},
        "stop": {"running", "paused"},
        "promote_candidate": {"running", "paused", "stopped"},
        "reject_candidate": {"running", "paused", "stopped"},
    }
    if experiment.status not in allowed_states[body.decision]:
        raise ApiError("EXPERIMENT_DECISION_INVALID_STATE", "当前实验状态不允许该决策", 409)
    if body.decision in {"promote_candidate", "reject_candidate"}:
        if snapshot is None:
            raise ApiError(
                "EXPERIMENT_METRIC_SNAPSHOT_REQUIRED",
                "终态实验决策必须引用最新指标快照",
                409,
            )
        latest_snapshot = session.scalar(
            select(ExperimentMetricSnapshot)
            .where(
                ExperimentMetricSnapshot.tenant_id == ctx.tenant_id,
                ExperimentMetricSnapshot.project_id == ctx.project_id,
                ExperimentMetricSnapshot.experiment_id == experiment_id,
            )
            .order_by(ExperimentMetricSnapshot.snapshot_version.desc())
            .limit(1)
        )
        current_evidence_sha256 = _experiment_evidence_sha256(
            experiment,
            _experiment_outcomes(session, ctx, experiment_id),
        )
        snapshot_is_current = (
            latest_snapshot is not None
            and latest_snapshot.metric_snapshot_id == snapshot.metric_snapshot_id
            and snapshot.evidence_sha256 == current_evidence_sha256
            and snapshot.payload.get("experiment_resource_version") == experiment.resource_version
            and snapshot.payload.get("design_sha256") == experiment.design_sha256
        )
        if not snapshot_is_current:
            raise ApiError(
                "EXPERIMENT_METRIC_SNAPSHOT_STALE",
                "实验指标或状态已变化，必须重新计算指标后再决策",
                409,
                details=[
                    {
                        "metric_snapshot_id": snapshot.metric_snapshot_id,
                        "latest_metric_snapshot_id": (
                            latest_snapshot.metric_snapshot_id if latest_snapshot else None
                        ),
                        "current_evidence_sha256": current_evidence_sha256,
                    }
                ],
            )
        _assert_frozen_task_versions(session, ctx, experiment)
    if body.decision == "promote_candidate" and (snapshot is None or snapshot.verdict != "promote"):
        raise ApiError(
            "EXPERIMENT_GATE_NOT_PASSED",
            "主指标、样本量或守护指标尚未通过，不能晋级候选版本",
            409,
        )

    status_by_decision = {
        "pause": "paused",
        "resume": "running",
        "stop": "stopped",
        "promote_candidate": "decided",
        "reject_candidate": "decided",
    }
    experiment.status = status_by_decision[body.decision]
    experiment.resource_version += 1
    experiment.trace_id = ctx.trace_id
    if body.decision in {"stop", "promote_candidate", "reject_candidate"}:
        experiment.ended_at = datetime.now(UTC)
    next_action = (
        {
            "type": "task_version_release",
            "task_version_id": experiment.candidate_task_version_id,
            "status": "eligible",
        }
        if body.decision == "promote_candidate"
        else {
            "type": "retain_control",
            "task_version_id": experiment.control_task_version_id,
            "status": "active",
        }
        if body.decision == "reject_candidate"
        else {"type": "experiment_state_change", "status": experiment.status}
    )
    decision = ExperimentDecision(
        decision_id=f"expd_{uuid.uuid4().hex[:20]}",
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        experiment_id=experiment_id,
        metric_snapshot_id=body.metric_snapshot_id,
        decision=body.decision,
        reason=body.reason,
        decided_by=ctx.user_id,
        trace_id=ctx.trace_id,
        payload={
            "experiment_status": experiment.status,
            "experiment_resource_version": experiment.resource_version,
            "next_action": next_action,
            "design_sha256": experiment.design_sha256,
        },
    )
    session.add(decision)
    session.flush()
    response = envelope(
        {
            "decision_id": decision.decision_id,
            "experiment_id": experiment_id,
            "metric_snapshot_id": decision.metric_snapshot_id,
            "decision": decision.decision,
            "reason": decision.reason,
            "decided_by": decision.decided_by,
            "trace_id": decision.trace_id,
            **decision.payload,
            "created_at": _iso(decision.created_at),
        },
        ctx,
    )
    record_audit(
        session,
        ctx,
        action=f"experiment.decision.{body.decision}",
        object_type="controlled_experiment",
        object_id=experiment_id,
        after=response["data"],
    )
    enqueue_event(
        session,
        ctx,
        event_type="experiment.decision-created",
        aggregate_type="controlled_experiment",
        aggregate_id=experiment_id,
        payload={
            "experiment_id": experiment_id,
            "decision_id": decision.decision_id,
            "decision": body.decision,
            "metric_snapshot_id": body.metric_snapshot_id,
            "resource_version": experiment.resource_version,
            "next_action": next_action,
        },
    )
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


def validate_experiment_release_attestation(
    session: Session,
    ctx: RequestContext,
    task_version_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Rebuild the promotion proof before a candidate enters or leaves a release gate."""

    if str(payload.get("source") or "") != "controlled_experiment":
        raise ApiError(
            "EXPERIMENT_RELEASE_ATTESTATION_REQUIRED",
            "实验候选版本必须携带受控实验晋级鉴证",
            409,
        )
    experiment_id = str(payload.get("experiment_id") or "").strip()
    metric_snapshot_id = str(payload.get("metric_snapshot_id") or "").strip()
    supplied_design_sha256 = str(payload.get("design_sha256") or "").strip()
    if not experiment_id or not metric_snapshot_id or len(supplied_design_sha256) != 64:
        raise ApiError(
            "EXPERIMENT_RELEASE_ATTESTATION_INVALID",
            "实验发布鉴证缺少 experiment_id、metric_snapshot_id 或 design_sha256",
            422,
        )
    experiment = get_experiment(session, ctx, experiment_id)
    if experiment.status != "decided":
        raise ApiError("EXPERIMENT_NOT_DECIDED", "实验尚未形成终态决策", 409)
    if experiment.candidate_task_version_id != task_version_id:
        raise ApiError(
            "EXPERIMENT_RELEASE_TARGET_MISMATCH",
            "发布目标不是实验冻结的候选 TaskVersion",
            409,
        )
    if not hmac.compare_digest(experiment.design_sha256, supplied_design_sha256):
        raise ApiError("EXPERIMENT_DESIGN_DRIFT", "实验设计摘要与发布请求不一致", 409)
    frozen_versions = _assert_frozen_task_versions(session, ctx, experiment)
    latest_snapshot = session.scalar(
        select(ExperimentMetricSnapshot)
        .where(
            ExperimentMetricSnapshot.tenant_id == ctx.tenant_id,
            ExperimentMetricSnapshot.project_id == ctx.project_id,
            ExperimentMetricSnapshot.experiment_id == experiment_id,
        )
        .order_by(ExperimentMetricSnapshot.snapshot_version.desc())
        .limit(1)
    )
    decision = session.scalar(
        select(ExperimentDecision)
        .where(
            ExperimentDecision.tenant_id == ctx.tenant_id,
            ExperimentDecision.project_id == ctx.project_id,
            ExperimentDecision.experiment_id == experiment_id,
        )
        .order_by(ExperimentDecision.created_at.desc(), ExperimentDecision.decision_id.desc())
        .limit(1)
    )
    current_evidence_sha256 = _experiment_evidence_sha256(
        experiment,
        _experiment_outcomes(session, ctx, experiment_id),
    )
    if (
        latest_snapshot is None
        or latest_snapshot.metric_snapshot_id != metric_snapshot_id
        or latest_snapshot.verdict != "promote"
        or not hmac.compare_digest(latest_snapshot.evidence_sha256, current_evidence_sha256)
    ):
        raise ApiError(
            "EXPERIMENT_RELEASE_SNAPSHOT_STALE",
            "实验指标快照已失效或未达到晋级条件",
            409,
        )
    if (
        decision is None
        or decision.decision != "promote_candidate"
        or decision.metric_snapshot_id != metric_snapshot_id
        or decision.payload.get("design_sha256") != experiment.design_sha256
    ):
        raise ApiError(
            "EXPERIMENT_PROMOTION_DECISION_MISSING",
            "实验缺少与指标快照一致的人工晋级决策",
            409,
        )
    candidate = frozen_versions["candidate"]
    control = frozen_versions["control"]
    return {
        "source": "controlled_experiment",
        "experiment_id": experiment_id,
        "decision_id": decision.decision_id,
        "metric_snapshot_id": latest_snapshot.metric_snapshot_id,
        "metric_snapshot_version": latest_snapshot.snapshot_version,
        "metric_evidence_sha256": latest_snapshot.evidence_sha256,
        "design_sha256": experiment.design_sha256,
        "task_type_id": experiment.task_type_id,
        "control_task_version_id": experiment.control_task_version_id,
        "control_task_version_snapshot_sha256": _task_version_snapshot_sha256(control),
        "candidate_task_version_id": experiment.candidate_task_version_id,
        "candidate_task_version_snapshot_sha256": _task_version_snapshot_sha256(candidate),
        "scene_profile_id": experiment.scene_profile_id,
        "scene_profile_version_id": experiment.scene_profile_version_id,
        "scene_profile_snapshot_sha256": experiment.scene_profile_snapshot_sha256,
        "decision_trace_id": decision.trace_id,
    }
