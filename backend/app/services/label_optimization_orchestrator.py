from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex, server_generated_public_id
from app.domain.label_optimization import (
    OptimizationMetrics,
    OptimizationScope,
    TriggerContext,
    evaluate_optimization_trigger,
)
from app.models import (
    EvalDatasetVersion,
    FeedbackExample,
    JsonResource,
    LabelAggregate,
    LabelAggregationPolicyVersion,
    LabelOptimizationSchedule,
    LabelVersion,
    PromptVersion,
    RunRecord,
)
from app.services.audit_service import record_audit
from app.services.execution_contract_registry import (
    preflight_production_execution_contract,
)
from app.services.outbox_service import enqueue_event
from app.services.public_run_projection_service import public_run_projection

TRIGGER_SCAN_COLLECTION = "label_optimization_trigger_scans"
METRIC_BASELINE_COLLECTION = "label_optimization_metric_baselines"
METRIC_SNAPSHOT_COLLECTION = "label_optimization_metric_snapshots"
METRIC_WINDOW = timedelta(hours=24)
TERMINAL_RUN_STATUSES = frozenset(
    {
        "blocked",
        "cancelled",
        "completed",
        "failed",
        "rolled-back",
        "rolled_back",
        "success",
        "succeeded",
    }
)
OVERRIDE_FEEDBACK_TYPES = frozenset(
    {"human-modified", "human_modified", "modified", "modified-accepted"}
)
LABEL_OPTIMIZATION_PUBLIC_FIELDS = frozenset(
    {
        "run_id",
        "scan_id",
        "status",
        "stage",
        "label_version_id",
        "prompt_version_id",
        "model_version",
        "aggregation_policy_version_id",
        "eval_dataset_version_id",
        "locked_versions",
        "trigger_kind",
        "trigger_reasons",
        "trigger_hash",
        "triggered_at",
        "metrics",
        "metrics_source",
        "metric_provenance",
        "budget",
        "blocked_reasons",
        "next_action",
        "next_actions",
        "affected_objects",
        "trace_id",
        "created_at",
        "updated_at",
    }
)


def _validate_locked_versions(
    session: Session,
    ctx: RequestContext,
    request_data: dict[str, Any],
) -> None:
    label_version_id = str(request_data["label_version_id"])
    prompt_version_id = str(request_data["prompt_version_id"])
    policy_version_id = str(request_data["aggregation_policy_version_id"])
    dataset_version_id = str(request_data["eval_dataset_version_id"])
    model_version = str(request_data["model_version"])
    label = session.scalar(
        select(LabelVersion).where(
            LabelVersion.label_version_id == label_version_id,
            LabelVersion.tenant_id == ctx.tenant_id,
            LabelVersion.project_id == ctx.project_id,
        )
    )
    prompt = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == prompt_version_id,
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    policy = session.scalar(
        select(LabelAggregationPolicyVersion).where(
            LabelAggregationPolicyVersion.policy_version_id == policy_version_id,
            LabelAggregationPolicyVersion.tenant_id == ctx.tenant_id,
            LabelAggregationPolicyVersion.project_id == ctx.project_id,
        )
    )
    dataset = session.scalar(
        select(EvalDatasetVersion).where(
            EvalDatasetVersion.eval_dataset_id == dataset_version_id,
            EvalDatasetVersion.tenant_id == ctx.tenant_id,
            EvalDatasetVersion.project_id == ctx.project_id,
        )
    )
    missing = [
        name
        for name, value in (
            ("label_version_id", label),
            ("prompt_version_id", prompt),
            ("aggregation_policy_version_id", policy),
            ("eval_dataset_version_id", dataset),
        )
        if value is None
    ]
    if missing:
        raise ApiError(
            "LABEL_OPTIMIZATION_LOCKED_VERSION_NOT_FOUND",
            "自动优化扫描的锁定版本在当前租户项目中不存在",
            404,
            details=[{"fields": missing}],
        )
    assert label is not None
    assert prompt is not None
    assert policy is not None
    assert dataset is not None
    if prompt.label_version_id != label_version_id or prompt.model_version not in {
        None,
        model_version,
    }:
        raise ApiError(
            "LABEL_OPTIMIZATION_PROMPT_BINDING_MISMATCH",
            "PromptVersion 与锁定 LabelVersion 或模型版本不一致",
            409,
        )
    if policy.label_version_id != label_version_id or policy.status != "active":
        raise ApiError(
            "LABEL_OPTIMIZATION_POLICY_BINDING_MISMATCH",
            "聚合策略必须是锁定标签版本的 active 强版本",
            409,
        )
    if dataset.status != "locked" or dataset.capability != "labeling":
        raise ApiError(
            "LABEL_OPTIMIZATION_DATASET_NOT_LOCKED",
            "自动优化只能使用已锁定的 labeling 评测集版本",
            409,
        )


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    normalized = _aware_utc(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else None


def _bounded_ppm(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(1_000_000, parsed))


def _metric_data(metrics: OptimizationMetrics) -> dict[str, int]:
    return {
        "reviewed_sample_count": metrics.reviewed_sample_count,
        "human_override_rate_ppm": metrics.human_override_rate_ppm,
        "baseline_human_override_rate_ppm": metrics.baseline_human_override_rate_ppm,
        "conflict_rate_ppm": metrics.conflict_rate_ppm,
        "json_validity_ppm": metrics.json_validity_ppm,
        "critical_recall_ppm": metrics.critical_recall_ppm,
        "baseline_critical_recall_ppm": metrics.baseline_critical_recall_ppm,
        "largest_failure_cluster_count": metrics.largest_failure_cluster_count,
        "new_feedback_count": metrics.new_feedback_count,
    }


def _latest_resource_data(
    session: Session,
    ctx: RequestContext,
    *,
    collection: str,
    label_version_id: str,
) -> dict[str, Any]:
    records = session.scalars(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == collection,
        )
        .order_by(JsonResource.created_at.desc())
    ).all()
    for record in records:
        data = record.data if isinstance(record.data, dict) else {}
        if (
            record.resource_key == label_version_id
            or data.get("label_version_id") == label_version_id
        ):
            return data
    return {}


def _feedback_matches_label_version(
    feedback: FeedbackExample,
    *,
    aggregate_ids: set[str],
    label_version_id: str,
) -> bool:
    if feedback.target_type in {"label-aggregate", "label_aggregate"}:
        if feedback.target_id in aggregate_ids:
            return True
    before = feedback.before_json if isinstance(feedback.before_json, dict) else {}
    after = feedback.after_json if isinstance(feedback.after_json, dict) else {}
    return (
        before.get("label_version_id") == label_version_id
        or after.get("label_version_id") == label_version_id
    )


def collect_optimization_metrics(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
    now: datetime,
) -> tuple[OptimizationMetrics, dict[str, Any]]:
    """Aggregate a reproducible 24-hour trigger snapshot from authoritative tables."""
    utc_now = _aware_utc(now)
    if utc_now is None:  # pragma: no cover - the public caller always provides now
        raise ValueError("now is required")
    cutoff = utc_now - METRIC_WINDOW

    aggregate_rows = session.scalars(
        select(LabelAggregate).where(
            LabelAggregate.tenant_id == ctx.tenant_id,
            LabelAggregate.project_id == ctx.project_id,
            LabelAggregate.label_version_id == label_version_id,
        )
    ).all()
    aggregates = [
        row
        for row in aggregate_rows
        if (_aware_utc(row.created_at) or utc_now) >= cutoff
        and (_aware_utc(row.created_at) or utc_now) <= utc_now
    ]
    aggregate_ids = {row.aggregate_id for row in aggregates}

    feedback_rows = session.scalars(
        select(FeedbackExample).where(
            FeedbackExample.tenant_id == ctx.tenant_id,
            FeedbackExample.project_id == ctx.project_id,
        )
    ).all()
    feedback = [
        row
        for row in feedback_rows
        if (_aware_utc(row.created_at) or utc_now) >= cutoff
        and (_aware_utc(row.created_at) or utc_now) <= utc_now
        and _feedback_matches_label_version(
            row,
            aggregate_ids=aggregate_ids,
            label_version_id=label_version_id,
        )
    ]

    reviewed_count = len(feedback)
    override_count = sum(row.feedback_type in OVERRIDE_FEEDBACK_TYPES for row in feedback)
    override_rate = round(override_count * 1_000_000 / reviewed_count) if reviewed_count else 0

    conflict_count = 0
    invalid_json_count = 0
    for aggregate in aggregates:
        reason_codes = {
            str(reason).strip().lower()
            for reason in (aggregate.reason_codes or [])
            if str(reason).strip()
        }
        if any("conflict" in reason for reason in reason_codes):
            conflict_count += 1
        if any(
            reason in {"invalid-json", "invalid_json", "json-invalid"} for reason in reason_codes
        ):
            invalid_json_count += 1
    aggregate_count = len(aggregates)
    conflict_rate = round(conflict_count * 1_000_000 / aggregate_count) if aggregate_count else 0
    json_validity = (
        round((aggregate_count - invalid_json_count) * 1_000_000 / aggregate_count)
        if aggregate_count
        else 1_000_000
    )

    reason_clusters = Counter(
        str(row.reason_code).strip()
        for row in feedback
        if row.reason_code and str(row.reason_code).strip()
    )
    largest_failure_cluster = max(reason_clusters.values(), default=0)

    baseline = _latest_resource_data(
        session,
        ctx,
        collection=METRIC_BASELINE_COLLECTION,
        label_version_id=label_version_id,
    )
    snapshot = _latest_resource_data(
        session,
        ctx,
        collection=METRIC_SNAPSHOT_COLLECTION,
        label_version_id=label_version_id,
    )
    baseline_override = _bounded_ppm(
        baseline.get("human_override_rate_ppm"),
        default=override_rate,
    )
    baseline_recall = _bounded_ppm(
        baseline.get("critical_recall_ppm"),
        default=1_000_000,
    )
    critical_recall = _bounded_ppm(
        snapshot.get("critical_recall_ppm"),
        default=baseline_recall,
    )
    json_validity = _bounded_ppm(
        snapshot.get("json_validity_ppm"),
        default=json_validity,
    )

    metrics = OptimizationMetrics(
        reviewed_sample_count=reviewed_count,
        human_override_rate_ppm=override_rate,
        baseline_human_override_rate_ppm=baseline_override,
        conflict_rate_ppm=conflict_rate,
        json_validity_ppm=json_validity,
        critical_recall_ppm=critical_recall,
        baseline_critical_recall_ppm=baseline_recall,
        largest_failure_cluster_count=largest_failure_cluster,
        new_feedback_count=reviewed_count,
    )
    provenance = {
        "window_started_at": _iso(cutoff),
        "window_ended_at": _iso(utc_now),
        "aggregate_count": aggregate_count,
        "feedback_count": reviewed_count,
        "override_count": override_count,
        "conflict_count": conflict_count,
        "invalid_json_count": invalid_json_count,
        "baseline_source": METRIC_BASELINE_COLLECTION if baseline else "window_default",
        "snapshot_source": METRIC_SNAPSHOT_COLLECTION if snapshot else "strong_table_projection",
    }
    return metrics, provenance


def _scope_runs(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
) -> list[RunRecord]:
    # FOR UPDATE makes same-scope scans serialize on MySQL. SQLite serializes the
    # subsequent writes, while the canonical hash remains a second idempotency gate.
    records = session.scalars(
        select(RunRecord)
        .where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_optimization",
        )
        .with_for_update()
    ).all()
    return [
        record
        for record in records
        if isinstance(record.payload, dict)
        and record.payload.get("label_version_id") == label_version_id
    ]


def _last_datetime(records: list[RunRecord], *, trigger_kind: str | None = None) -> datetime | None:
    timestamps = [
        _aware_utc(record.created_at)
        for record in records
        if trigger_kind is None or record.payload.get("trigger_kind") == trigger_kind
    ]
    present = [value for value in timestamps if value is not None]
    return max(present, default=None)


def _last_scan_at(
    session: Session,
    ctx: RequestContext,
    *,
    label_version_id: str,
) -> datetime | None:
    records = session.scalars(
        select(JsonResource)
        .where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == TRIGGER_SCAN_COLLECTION,
        )
        .order_by(JsonResource.created_at.desc())
    ).all()
    for record in records:
        data = record.data if isinstance(record.data, dict) else {}
        # A caller may probe the endpoint more often than the 15-minute clock.
        # Only a scan that was actually due advances that clock; otherwise every
        # blocked probe could postpone the next real threshold evaluation forever.
        if (
            data.get("label_version_id") == label_version_id
            and data.get("threshold_scan_due") is True
        ):
            return _aware_utc(record.created_at)
    return None


def _next_action(*, triggered: bool, blocked_reasons: list[str]) -> dict[str, str]:
    if triggered:
        return {
            "code": "generate-prompt-candidates",
            "label": "生成 2–5 个 Prompt 候选并进入离线评测",
        }
    if blocked_reasons:
        return {
            "code": "wait-for-safety-gate",
            "label": "等待单活运行结束、冷却期到期或补足审核样本",
        }
    return {
        "code": "wait-for-next-scan",
        "label": "当前未达到触发阈值，等待下一次 15 分钟扫描",
    }


def _serialize_run(record: RunRecord) -> dict[str, Any]:
    payload = dict(record.payload or {})
    projection = {
        **payload,
        "run_id": record.run_id,
        "status": record.status,
        "stage": payload.get("stage") or record.status,
        "trace_id": record.trace_id,
        "created_at": _iso(record.created_at),
        "updated_at": _iso(record.updated_at),
    }
    return public_run_projection(
        projection,
        allowed_fields=LABEL_OPTIMIZATION_PUBLIC_FIELDS,
        forbidden_fields={"provider"},
        field_name="label_optimization_run",
    )


def execute_trigger_scan(
    session: Session,
    ctx: RequestContext,
    *,
    request_data: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    preflight_production_execution_contract(
        event_type="agent_run.requested",
        run_type="label_optimization",
    )
    utc_now = _aware_utc(now or datetime.now(UTC))
    if utc_now is None:  # pragma: no cover
        raise ValueError("now is required")
    _validate_locked_versions(session, ctx, request_data)
    label_version_id = str(request_data["label_version_id"])
    scope = OptimizationScope(ctx.tenant_id, ctx.project_id, label_version_id)

    metrics_override = request_data.get("metrics_override")
    if isinstance(metrics_override, dict):
        metrics = OptimizationMetrics(**metrics_override)
        metric_snapshot = request_data.get("_metric_snapshot")
        if isinstance(metric_snapshot, dict) and metric_snapshot.get("snapshot_id"):
            metrics_source = "authoritative_metric_snapshot"
            metric_provenance = {
                **dict(metric_snapshot.get("provenance") or {}),
                "snapshot_id": metric_snapshot["snapshot_id"],
                "snapshot_sha256": metric_snapshot.get("snapshot_sha256"),
                "window_started_at": metric_snapshot.get("window_started_at"),
                "window_ended_at": metric_snapshot.get("window_ended_at"),
            }
        else:
            metrics_source = "request_override"
            metric_provenance = {
                "window_started_at": _iso(utc_now - METRIC_WINDOW),
                "window_ended_at": _iso(utc_now),
                "override_actor_id": ctx.user_id,
            }
    else:
        metrics, metric_provenance = collect_optimization_metrics(
            session,
            ctx,
            label_version_id=label_version_id,
            now=utc_now,
        )
        metrics_source = "authoritative_24h_window"

    runs = _scope_runs(session, ctx, label_version_id=label_version_id)
    schedule_session_active = session.scalar(
        select(LabelOptimizationSchedule.schedule_id).where(
            LabelOptimizationSchedule.tenant_id == ctx.tenant_id,
            LabelOptimizationSchedule.project_id == ctx.project_id,
            LabelOptimizationSchedule.label_version_id == label_version_id,
            LabelOptimizationSchedule.active_run_id.is_not(None),
        )
    )
    active = (
        any(record.status not in TERMINAL_RUN_STATUSES for record in runs)
        or schedule_session_active is not None
    )
    scheduler_due_kinds = {
        str(item)
        for item in request_data.get("_scheduler_due_kinds", [])
        if str(item) in {"threshold", "daily_incremental", "weekly_full"}
    }
    last_threshold_scan_at = _last_scan_at(
        session,
        ctx,
        label_version_id=label_version_id,
    )
    last_daily_trigger_at = _last_datetime(runs, trigger_kind="daily_incremental")
    last_weekly_trigger_at = _last_datetime(runs, trigger_kind="weekly_full")
    if scheduler_due_kinds:
        if "threshold" not in scheduler_due_kinds:
            last_threshold_scan_at = utc_now
        if "daily_incremental" not in scheduler_due_kinds:
            last_daily_trigger_at = utc_now
        if "weekly_full" not in scheduler_due_kinds:
            last_weekly_trigger_at = utc_now
    decision = evaluate_optimization_trigger(
        TriggerContext(
            scope=scope,
            now=utc_now,
            metrics=metrics,
            last_threshold_scan_at=last_threshold_scan_at,
            last_scope_triggered_at=_last_datetime(runs),
            last_daily_trigger_at=last_daily_trigger_at,
            last_weekly_trigger_at=last_weekly_trigger_at,
            active_run_scopes=frozenset({scope}) if active else frozenset(),
        )
    )
    blocked_reasons = [reason.value for reason in decision.blocked_reasons]
    duplicate_hash = bool(
        decision.canonical_hash
        and any(record.payload.get("trigger_hash") == decision.canonical_hash for record in runs)
    )
    if duplicate_hash:
        blocked_reasons.append("duplicate_trigger_hash")
    if not decision.should_trigger and not blocked_reasons:
        blocked_reasons.append(
            "threshold_scan_interval_not_elapsed"
            if not decision.threshold_scan_due
            else "trigger_conditions_not_met"
        )

    triggered = decision.should_trigger and not duplicate_hash
    scan_id = server_generated_public_id("label_opt_scan", suffix_length=20)
    run_id = (
        public_id_from_hex(
            "label_optimization",
            decision.canonical_hash,
            suffix_length=24,
        )
        if triggered and decision.canonical_hash
        else None
    )
    locked_versions = {
        "label_version_id": label_version_id,
        "prompt_version_id": request_data["prompt_version_id"],
        "model_version": request_data["model_version"],
        "aggregation_policy_version_id": request_data["aggregation_policy_version_id"],
        "eval_dataset_version_id": request_data["eval_dataset_version_id"],
    }
    budget = dict(request_data["budget"])
    reason_codes = [reason.value for reason in decision.reason_codes]
    status = "queued" if triggered else "blocked"
    next_action = _next_action(triggered=triggered, blocked_reasons=blocked_reasons)
    scan_data: dict[str, Any] = {
        "scan_id": scan_id,
        "run_id": run_id,
        "label_version_id": label_version_id,
        "triggered": triggered,
        "status": status,
        "stage": status,
        "trigger_kind": decision.kind.value if decision.kind else None,
        "trigger_reasons": reason_codes,
        "trigger_hash": decision.canonical_hash,
        "threshold_scan_due": decision.threshold_scan_due,
        "metrics": _metric_data(metrics),
        "metrics_source": metrics_source,
        "metric_provenance": metric_provenance,
        "blocked_reasons": blocked_reasons,
        "next_action": next_action,
        "locked_versions": locked_versions,
        "budget": budget,
        "trace_id": ctx.trace_id,
        "created_at": _iso(utc_now),
        "updated_at": _iso(utc_now),
    }

    if run_id is not None:
        run_payload = {
            "run_id": run_id,
            "scan_id": scan_id,
            "status": "queued",
            "stage": "queued",
            "label_version_id": label_version_id,
            "prompt_version_id": locked_versions["prompt_version_id"],
            "model_version": locked_versions["model_version"],
            "aggregation_policy_version_id": locked_versions["aggregation_policy_version_id"],
            "eval_dataset_version_id": locked_versions["eval_dataset_version_id"],
            "locked_versions": locked_versions,
            "trigger_kind": decision.kind.value if decision.kind else None,
            "trigger_reasons": reason_codes,
            "trigger_hash": decision.canonical_hash,
            "triggered_at": _iso(utc_now),
            "metrics": _metric_data(metrics),
            "metrics_source": metrics_source,
            "metric_provenance": metric_provenance,
            "budget": budget,
            "blocked_reasons": [],
            "next_action": next_action,
            "next_actions": [next_action],
            "affected_objects": [
                {"type": "label_version", "id": label_version_id},
                {"type": "prompt_version", "id": locked_versions["prompt_version_id"]},
                {
                    "type": "aggregation_policy_version",
                    "id": locked_versions["aggregation_policy_version_id"],
                },
                {
                    "type": "eval_dataset_version",
                    "id": locked_versions["eval_dataset_version_id"],
                },
            ],
            "trace_id": ctx.trace_id,
        }
        record = RunRecord(
            run_id=run_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type="label_optimization",
            status="queued",
            run_key=f"label-optimization:{label_version_id}",
            partition_key=label_version_id,
            trace_id=ctx.trace_id,
            payload=run_payload,
            created_at=utc_now,
            updated_at=utc_now,
        )
        session.add(record)
        session.flush()
        enqueue_event(
            session,
            ctx,
            event_type="agent_run.requested",
            aggregate_type="label_optimization",
            aggregate_id=run_id,
            payload=run_payload,
        )
        record_audit(
            session,
            ctx,
            action="label_optimization.create",
            object_type="label_optimization",
            object_id=run_id,
            after=run_payload,
        )

    scan = JsonResource(
        collection=TRIGGER_SCAN_COLLECTION,
        resource_key=scan_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        status=status,
        trace_id=ctx.trace_id,
        data=scan_data,
        created_at=utc_now,
        updated_at=utc_now,
    )
    session.add(scan)
    session.flush()
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.trigger_scan.completed",
        aggregate_type="label_optimization_trigger_scan",
        aggregate_id=scan_id,
        payload={
            "scan_id": scan_id,
            "run_id": run_id,
            "status": status,
            "triggered": triggered,
            "trigger_hash": decision.canonical_hash,
            "blocked_reasons": blocked_reasons,
            "resource_version": 1,
        },
    )
    record_audit(
        session,
        ctx,
        action="label_optimization.trigger_scan",
        object_type="label_optimization_trigger_scan",
        object_id=scan_id,
        result="success" if triggered else "blocked",
        after=scan_data,
    )
    return scan_data


def get_trigger_scan_or_run(
    session: Session,
    ctx: RequestContext,
    *,
    run_or_scan_id: str,
) -> dict[str, Any]:
    run = session.scalar(
        select(RunRecord).where(
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_optimization",
            RunRecord.run_id == run_or_scan_id,
        )
    )
    if run is not None:
        return _serialize_run(run)

    scan = session.scalar(
        select(JsonResource).where(
            JsonResource.tenant_id == ctx.tenant_id,
            JsonResource.project_id == ctx.project_id,
            JsonResource.collection == TRIGGER_SCAN_COLLECTION,
            JsonResource.resource_key == run_or_scan_id,
        )
    )
    if scan is not None:
        return {
            **dict(scan.data or {}),
            "status": scan.status or (scan.data or {}).get("status"),
            "trace_id": scan.trace_id or (scan.data or {}).get("trace_id"),
            "created_at": _iso(scan.created_at),
            "updated_at": _iso(scan.updated_at),
        }
    raise ApiError(
        "LABEL_OPTIMIZATION_TRIGGER_SCAN_NOT_FOUND",
        f"标签优化触发扫描或运行不存在：{run_or_scan_id}",
        404,
    )


__all__ = [
    "collect_optimization_metrics",
    "execute_trigger_scan",
    "get_trigger_scan_or_run",
]
