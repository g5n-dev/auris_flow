from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.domain.label_optimization import IterationBudget, OptimizationMetrics
from app.models import (
    FeedbackExample,
    LabelEvalResult,
    LabelOptimizationMetricSnapshot,
    LabelOptimizationRound,
    LabelOptimizationSchedule,
    PromptVersion,
    RunRecord,
)
from app.services.audit_service import record_audit
from app.services.label_optimization_orchestrator import (
    METRIC_WINDOW,
    _validate_locked_versions,
    collect_optimization_metrics,
)
from app.services.outbox_service import enqueue_event

DEFAULT_BUDGET: dict[str, Any] = {
    "max_rounds": 3,
    "min_candidates_per_round": 2,
    "max_candidates_per_round": 5,
    "candidates_per_round": 5,
    "max_elapsed_seconds": 7200,
    "max_cost_micros": None,
    "min_meaningful_gain_ppm": 20_000,
    "max_consecutive_failed_rounds": 2,
}

# Only stable machine reason codes enter trigger clustering.  User notes and
# arbitrary model text are rejected into the append-only snapshot evidence.
CANONICAL_FAILURE_REASON_CODES = frozenset(
    {
        "false_positive",
        "false_negative",
        "wrong_value",
        "missing_evidence",
        "source_conflict",
        "taxonomy_unknown",
        "prompt_format_failure",
        "invalid_json_output",
        "critical_recall_regression",
        "human_modified",
        "adjudicated_correction",
    }
)
_REASON_ALIASES = {
    "modified": "human_modified",
    "human-modified": "human_modified",
    "modified-accepted": "human_modified",
    "json-invalid": "invalid_json_output",
    "invalid-json": "invalid_json_output",
    "invalid_json": "invalid_json_output",
    "schema_validation_failed": "invalid_json_output",
    "conflict": "source_conflict",
}
_INVALID_JSON_ERROR_CODES = frozenset(
    {
        "invalid_json",
        "invalid_json_output",
        "json_decode_error",
        "output_schema_invalid",
        "schema_validation_failed",
    }
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value).isoformat().replace("+00:00", "Z")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_reason_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    normalized = _REASON_ALIASES.get(normalized, normalized)
    return normalized if normalized in CANONICAL_FAILURE_REASON_CODES else None


def _validated_budget(raw: dict[str, Any] | None) -> dict[str, Any]:
    budget = {**DEFAULT_BUDGET, **(raw or {})}
    candidates_per_round = int(
        budget.get("candidates_per_round") or budget["max_candidates_per_round"]
    )
    budget["candidates_per_round"] = candidates_per_round
    if (
        not int(budget["min_candidates_per_round"])
        <= candidates_per_round
        <= int(budget["max_candidates_per_round"])
    ):
        raise ApiError(
            "LABEL_OPTIMIZATION_CANDIDATE_BUDGET_INVALID",
            "每轮候选数必须位于锁定的 2–5 范围内",
            422,
        )
    IterationBudget(
        max_rounds=int(budget["max_rounds"]),
        min_candidates_per_round=int(budget["min_candidates_per_round"]),
        max_candidates_per_round=int(budget["max_candidates_per_round"]),
        max_elapsed=timedelta(seconds=int(budget["max_elapsed_seconds"])),
        max_cost_micros=(
            int(budget["max_cost_micros"]) if budget.get("max_cost_micros") is not None else None
        ),
        min_meaningful_gain_ppm=int(budget["min_meaningful_gain_ppm"]),
        max_consecutive_failed_rounds=int(budget["max_consecutive_failed_rounds"]),
    )
    return budget


def iteration_budget_from_data(raw: dict[str, Any]) -> IterationBudget:
    budget = _validated_budget(raw)
    return IterationBudget(
        max_rounds=int(budget["max_rounds"]),
        min_candidates_per_round=int(budget["min_candidates_per_round"]),
        max_candidates_per_round=int(budget["max_candidates_per_round"]),
        max_elapsed=timedelta(seconds=int(budget["max_elapsed_seconds"])),
        max_cost_micros=(
            int(budget["max_cost_micros"]) if budget.get("max_cost_micros") is not None else None
        ),
        min_meaningful_gain_ppm=int(budget["min_meaningful_gain_ppm"]),
        max_consecutive_failed_rounds=int(budget["max_consecutive_failed_rounds"]),
    )


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ApiError(
            "LABEL_OPTIMIZATION_TIMEZONE_INVALID",
            f"未知 IANA 时区：{name}",
            422,
        ) from exc


def next_daily_at(now: datetime, timezone_name: str, hour: int) -> datetime:
    local_now = _aware_utc(now).astimezone(_timezone(timezone_name))
    candidate = local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_weekly_at(
    now: datetime,
    timezone_name: str,
    weekday: int,
    hour: int,
) -> datetime:
    local_now = _aware_utc(now).astimezone(_timezone(timezone_name))
    days = (weekday - local_now.weekday()) % 7
    candidate = (local_now + timedelta(days=days)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def schedule_data(schedule: LabelOptimizationSchedule) -> dict[str, Any]:
    return {
        "schedule_id": schedule.schedule_id,
        "status": schedule.status,
        "label_version_id": schedule.label_version_id,
        "prompt_version_id": schedule.prompt_version_id,
        "model_version": schedule.model_version,
        "aggregation_policy_version_id": schedule.aggregation_policy_version_id,
        "eval_dataset_version_id": schedule.eval_dataset_version_id,
        "schedule_timezone": schedule.schedule_timezone,
        "threshold_interval_seconds": schedule.threshold_interval_seconds,
        "daily_hour": schedule.daily_hour,
        "weekly_day": schedule.weekly_day,
        "next_threshold_scan_at": _iso(schedule.next_threshold_scan_at),
        "next_daily_at": _iso(schedule.next_daily_at),
        "next_weekly_at": _iso(schedule.next_weekly_at),
        "last_threshold_scanned_at": _iso(schedule.last_threshold_scanned_at),
        "last_daily_started_at": _iso(schedule.last_daily_started_at),
        "last_weekly_started_at": _iso(schedule.last_weekly_started_at),
        "active_run_id": schedule.active_run_id,
        "baseline_snapshot_id": schedule.baseline_snapshot_id,
        "budget": schedule.budget,
        "resource_version": schedule.resource_version,
        "trace_id": schedule.trace_id,
        "created_at": _iso(schedule.created_at),
        "updated_at": _iso(schedule.updated_at),
    }


def metric_snapshot_data(snapshot: LabelOptimizationMetricSnapshot) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "schedule_id": snapshot.schedule_id,
        "snapshot_kind": snapshot.snapshot_kind,
        "label_version_id": snapshot.label_version_id,
        "prompt_version_id": snapshot.prompt_version_id,
        "model_version": snapshot.model_version,
        "aggregation_policy_version_id": snapshot.aggregation_policy_version_id,
        "eval_dataset_version_id": snapshot.eval_dataset_version_id,
        "window_started_at": _iso(snapshot.window_started_at),
        "window_ended_at": _iso(snapshot.window_ended_at),
        "metrics": snapshot.metrics,
        "reason_counts": snapshot.reason_counts,
        "rejection_count": snapshot.rejection_count,
        "rejected_records": snapshot.rejected_records,
        "provenance": snapshot.provenance,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "trace_id": snapshot.trace_id,
        "created_at": _iso(snapshot.created_at),
    }


def round_data(round_record: LabelOptimizationRound) -> dict[str, Any]:
    return {
        "round_id": round_record.round_id,
        "schedule_id": round_record.schedule_id,
        "optimization_run_id": round_record.optimization_run_id,
        "generation_run_id": round_record.generation_run_id,
        "round_number": round_record.round_number,
        "label_version_id": round_record.label_version_id,
        "prompt_version_id": round_record.prompt_version_id,
        "model_version": round_record.model_version,
        "aggregation_policy_version_id": round_record.aggregation_policy_version_id,
        "eval_dataset_version_id": round_record.eval_dataset_version_id,
        "status": round_record.status,
        "candidate_count": round_record.candidate_count,
        "candidate_ids": round_record.candidate_ids,
        "eval_run_ids": round_record.eval_run_ids,
        "selected_prompt_version_id": round_record.selected_prompt_version_id,
        "latest_gain_ppm": round_record.latest_gain_ppm,
        "critical_metric_regressed": round_record.critical_metric_regressed,
        "cost_spent_micros": round_record.cost_spent_micros,
        "consecutive_failed_rounds": round_record.consecutive_failed_rounds,
        "stop_reason_codes": round_record.stop_reason_codes,
        "started_at": _iso(round_record.started_at),
        "completed_at": _iso(round_record.completed_at),
        "trace_id": round_record.trace_id,
        "payload": round_record.payload,
    }


def create_or_update_schedule(
    session: Session,
    ctx: RequestContext,
    *,
    request_data: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    utc_now = _aware_utc(now or datetime.now(UTC))
    _validate_locked_versions(session, ctx, request_data)
    prompt = session.scalar(
        select(PromptVersion).where(
            PromptVersion.prompt_version_id == request_data["prompt_version_id"],
            PromptVersion.tenant_id == ctx.tenant_id,
            PromptVersion.project_id == ctx.project_id,
        )
    )
    if prompt is None or prompt.status not in {"approved", "published"}:
        raise ApiError(
            "LABEL_OPTIMIZATION_BASELINE_PROMPT_NOT_APPROVED",
            "自动优化计划的基线 PromptVersion 必须已批准",
            409,
            details=[{"status": prompt.status if prompt is not None else None}],
        )
    timezone_name = str(request_data.get("schedule_timezone") or "Asia/Shanghai")
    _timezone(timezone_name)
    daily_hour = int(request_data.get("daily_hour", 2))
    weekly_day = int(request_data.get("weekly_day", 6))
    if not 0 <= daily_hour <= 23 or not 0 <= weekly_day <= 6:
        raise ApiError(
            "LABEL_OPTIMIZATION_SCHEDULE_INVALID",
            "daily_hour 必须为 0–23，weekly_day 必须为 0–6（周一到周日）",
            422,
        )
    budget = _validated_budget(request_data.get("budget"))
    schedule = session.scalar(
        select(LabelOptimizationSchedule)
        .where(
            LabelOptimizationSchedule.tenant_id == ctx.tenant_id,
            LabelOptimizationSchedule.project_id == ctx.project_id,
            LabelOptimizationSchedule.label_version_id == request_data["label_version_id"],
        )
        .with_for_update()
    )
    before = schedule_data(schedule) if schedule is not None else None
    if schedule is not None and schedule.active_run_id:
        locked_fields = {
            "prompt_version_id",
            "model_version",
            "aggregation_policy_version_id",
            "eval_dataset_version_id",
        }
        changed = [
            field for field in locked_fields if getattr(schedule, field) != request_data[field]
        ]
        if changed:
            raise ApiError(
                "LABEL_OPTIMIZATION_SCHEDULE_ACTIVE",
                "活跃优化运行期间不能替换锁定 Bundle",
                409,
                details=[{"fields": sorted(changed), "active_run_id": schedule.active_run_id}],
            )
    start_immediately = bool(request_data.get("start_immediately", True))
    if schedule is None:
        schedule_id = str(
            request_data.get("schedule_id")
            or "los_"
            + _canonical_sha256([ctx.tenant_id, ctx.project_id, request_data["label_version_id"]])[
                :24
            ]
        )
        schedule = LabelOptimizationSchedule(
            schedule_id=schedule_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            label_version_id=str(request_data["label_version_id"]),
            prompt_version_id=str(request_data["prompt_version_id"]),
            model_version=str(request_data["model_version"]),
            aggregation_policy_version_id=str(request_data["aggregation_policy_version_id"]),
            eval_dataset_version_id=str(request_data["eval_dataset_version_id"]),
            status=str(request_data.get("status") or "active"),
            schedule_timezone=timezone_name,
            threshold_interval_seconds=900,
            daily_hour=daily_hour,
            weekly_day=weekly_day,
            next_threshold_scan_at=utc_now
            if start_immediately
            else utc_now + timedelta(minutes=15),
            next_daily_at=next_daily_at(utc_now, timezone_name, daily_hour),
            next_weekly_at=next_weekly_at(
                utc_now,
                timezone_name,
                weekly_day,
                daily_hour,
            ),
            budget=budget,
            resource_version=1,
            trace_id=ctx.trace_id,
        )
        session.add(schedule)
        action = "label_optimization_schedule.create"
    else:
        expected_resource_version = request_data.get("expected_resource_version")
        if expected_resource_version is None:
            raise ApiError(
                "RESOURCE_VERSION_REQUIRED",
                "更新优化计划必须提供 expected_resource_version",
                409,
                details=[{"actual": schedule.resource_version}],
            )
        if int(expected_resource_version) != int(schedule.resource_version):
            raise ApiError(
                "RESOURCE_VERSION_CONFLICT",
                "优化计划已被其他操作更新",
                409,
                details=[
                    {"expected": expected_resource_version, "actual": schedule.resource_version}
                ],
            )
        binding_changed = any(
            getattr(schedule, field) != request_data[field]
            for field in (
                "prompt_version_id",
                "model_version",
                "aggregation_policy_version_id",
                "eval_dataset_version_id",
            )
        )
        calendar_changed = (
            schedule.schedule_timezone != timezone_name
            or schedule.daily_hour != daily_hour
            or schedule.weekly_day != weekly_day
        )
        schedule.prompt_version_id = str(request_data["prompt_version_id"])
        schedule.model_version = str(request_data["model_version"])
        schedule.aggregation_policy_version_id = str(request_data["aggregation_policy_version_id"])
        schedule.eval_dataset_version_id = str(request_data["eval_dataset_version_id"])
        schedule.status = str(request_data.get("status") or schedule.status)
        schedule.schedule_timezone = timezone_name
        schedule.daily_hour = daily_hour
        schedule.weekly_day = weekly_day
        schedule.budget = budget
        schedule.trace_id = ctx.trace_id
        schedule.resource_version += 1
        if binding_changed:
            schedule.baseline_snapshot_id = None
            schedule.next_threshold_scan_at = utc_now
        if calendar_changed:
            schedule.next_daily_at = next_daily_at(utc_now, timezone_name, daily_hour)
            schedule.next_weekly_at = next_weekly_at(
                utc_now,
                timezone_name,
                weekly_day,
                daily_hour,
            )
        if start_immediately:
            schedule.next_threshold_scan_at = utc_now
        action = "label_optimization_schedule.update"
    session.flush()
    data = schedule_data(schedule)
    record_audit(
        session,
        ctx,
        action=action,
        object_type="label_optimization_schedule",
        object_id=schedule.schedule_id,
        before=before,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type=f"{action}d" if action.endswith(".create") else f"{action}d",
        aggregate_type="label_optimization_schedule",
        aggregate_id=schedule.schedule_id,
        payload=data,
    )
    return data


def get_schedule(
    session: Session,
    ctx: RequestContext,
    *,
    schedule_id: str,
) -> LabelOptimizationSchedule:
    schedule = session.scalar(
        select(LabelOptimizationSchedule).where(
            LabelOptimizationSchedule.schedule_id == schedule_id,
            LabelOptimizationSchedule.tenant_id == ctx.tenant_id,
            LabelOptimizationSchedule.project_id == ctx.project_id,
        )
    )
    if schedule is None:
        raise ApiError(
            "LABEL_OPTIMIZATION_SCHEDULE_NOT_FOUND",
            f"标签优化计划不存在：{schedule_id}",
            404,
        )
    return schedule


def list_schedules(session: Session, ctx: RequestContext) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(LabelOptimizationSchedule)
        .where(
            LabelOptimizationSchedule.tenant_id == ctx.tenant_id,
            LabelOptimizationSchedule.project_id == ctx.project_id,
        )
        .order_by(LabelOptimizationSchedule.updated_at.desc())
    ).all()
    return [schedule_data(row) for row in rows]


def list_metric_snapshots(
    session: Session,
    ctx: RequestContext,
    *,
    schedule_id: str,
) -> list[dict[str, Any]]:
    get_schedule(session, ctx, schedule_id=schedule_id)
    rows = session.scalars(
        select(LabelOptimizationMetricSnapshot)
        .where(
            LabelOptimizationMetricSnapshot.schedule_id == schedule_id,
            LabelOptimizationMetricSnapshot.tenant_id == ctx.tenant_id,
            LabelOptimizationMetricSnapshot.project_id == ctx.project_id,
        )
        .order_by(LabelOptimizationMetricSnapshot.window_ended_at.desc())
    ).all()
    return [metric_snapshot_data(row) for row in rows]


def list_rounds(
    session: Session,
    ctx: RequestContext,
    *,
    schedule_id: str,
) -> list[dict[str, Any]]:
    get_schedule(session, ctx, schedule_id=schedule_id)
    rows = session.scalars(
        select(LabelOptimizationRound)
        .where(
            LabelOptimizationRound.schedule_id == schedule_id,
            LabelOptimizationRound.tenant_id == ctx.tenant_id,
            LabelOptimizationRound.project_id == ctx.project_id,
        )
        .order_by(
            LabelOptimizationRound.optimization_run_id,
            LabelOptimizationRound.round_number,
        )
    ).all()
    return [round_data(row) for row in rows]


def _failed_extraction_rejections(
    session: Session,
    schedule: LabelOptimizationSchedule,
    *,
    cutoff: datetime,
    now: datetime,
) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(RunRecord).where(
            RunRecord.tenant_id == schedule.tenant_id,
            RunRecord.project_id == schedule.project_id,
            RunRecord.run_type == "label_extraction",
            RunRecord.status.in_(("failed", "blocked")),
            RunRecord.created_at >= cutoff,
            RunRecord.created_at <= now,
        )
    ).all()
    rejected: list[dict[str, Any]] = []
    for row in rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("label_version_id") != schedule.label_version_id:
            continue
        completion = payload.get("completion_receipt")
        completion = completion if isinstance(completion, dict) else {}
        raw_code = str(payload.get("error_code") or completion.get("error_code") or "")
        normalized = raw_code.strip().lower().replace("-", "_")
        if normalized in _INVALID_JSON_ERROR_CODES:
            rejected.append(
                {
                    "record_type": "label_extraction_run",
                    "record_id": row.run_id,
                    "reason_code": "invalid_json_output",
                }
            )
    return rejected


def _feedback_reason_evidence(
    session: Session,
    schedule: LabelOptimizationSchedule,
    *,
    cutoff: datetime,
    now: datetime,
) -> tuple[Counter[str], list[dict[str, Any]]]:
    rows = session.scalars(
        select(FeedbackExample).where(
            FeedbackExample.tenant_id == schedule.tenant_id,
            FeedbackExample.project_id == schedule.project_id,
            FeedbackExample.created_at >= cutoff,
            FeedbackExample.created_at <= now,
        )
    ).all()
    reasons: Counter[str] = Counter()
    rejected: list[dict[str, Any]] = []
    for row in rows:
        before = row.before_json if isinstance(row.before_json, dict) else {}
        after = row.after_json if isinstance(row.after_json, dict) else {}
        if schedule.label_version_id not in {
            before.get("label_version_id"),
            after.get("label_version_id"),
        }:
            continue
        canonical = _normalize_reason_code(row.reason_code)
        if canonical is not None:
            reasons[canonical] += 1
        elif row.reason_code:
            rejected.append(
                {
                    "record_type": "feedback_example",
                    "record_id": row.feedback_example_id,
                    "reason_code": "invalid_reason_code",
                    "value_sha256": _canonical_sha256(str(row.reason_code)),
                }
            )
    return reasons, rejected


def _latest_strong_critical_delta_ppm(
    session: Session,
    schedule: LabelOptimizationSchedule,
) -> int:
    eval_runs = session.scalars(
        select(RunRecord).where(
            RunRecord.tenant_id == schedule.tenant_id,
            RunRecord.project_id == schedule.project_id,
            RunRecord.run_type == "eval_run",
            RunRecord.status.in_(("success", "blocked")),
        )
    ).all()
    matching_ids = {
        row.run_id
        for row in eval_runs
        if isinstance(row.payload, dict)
        and row.payload.get("label_version_id") == schedule.label_version_id
        and row.payload.get("prompt_version_id") == schedule.prompt_version_id
        and row.payload.get("model_version") == schedule.model_version
        and row.payload.get("aggregation_policy_version_id")
        == schedule.aggregation_policy_version_id
        and row.payload.get("eval_dataset_version_id") == schedule.eval_dataset_version_id
    }
    if not matching_ids:
        return 0
    result = session.scalar(
        select(LabelEvalResult)
        .where(
            LabelEvalResult.tenant_id == schedule.tenant_id,
            LabelEvalResult.project_id == schedule.project_id,
            LabelEvalResult.eval_run_id.in_(matching_ids),
        )
        .order_by(LabelEvalResult.created_at.desc())
    )
    if result is None:
        return 0
    raw_delta = (result.overall_metrics or {}).get("critical_recall_delta_pp")
    if raw_delta is None or isinstance(raw_delta, bool):
        return 0
    try:
        return round(float(raw_delta) * 10_000)
    except (TypeError, ValueError):
        return 0


def produce_metric_snapshot(
    session: Session,
    ctx: RequestContext,
    schedule: LabelOptimizationSchedule,
    *,
    now: datetime | None = None,
) -> LabelOptimizationMetricSnapshot:
    utc_now = _aware_utc(now or datetime.now(UTC))
    cutoff = utc_now - METRIC_WINDOW
    metrics, provenance = collect_optimization_metrics(
        session,
        ctx,
        label_version_id=schedule.label_version_id,
        now=utc_now,
    )
    reason_counts, rejected = _feedback_reason_evidence(
        session,
        schedule,
        cutoff=cutoff,
        now=utc_now,
    )
    invalid_json_rejections = _failed_extraction_rejections(
        session,
        schedule,
        cutoff=cutoff,
        now=utc_now,
    )
    rejected.extend(invalid_json_rejections)
    reason_counts["invalid_json_output"] += len(invalid_json_rejections)

    baseline = (
        session.get(LabelOptimizationMetricSnapshot, schedule.baseline_snapshot_id)
        if schedule.baseline_snapshot_id
        else None
    )
    baseline_metrics = baseline.metrics if baseline is not None else {}
    baseline_critical_recall_ppm = int(baseline_metrics.get("critical_recall_ppm", 1_000_000))
    critical_recall_ppm = max(
        0,
        min(
            1_000_000,
            baseline_critical_recall_ppm + _latest_strong_critical_delta_ppm(session, schedule),
        ),
    )
    aggregate_count = int(provenance.get("aggregate_count") or 0)
    invalid_count = int(provenance.get("invalid_json_count") or 0) + len(invalid_json_rejections)
    json_denominator = aggregate_count + len(invalid_json_rejections)
    json_validity_ppm = (
        round((json_denominator - invalid_count) * 1_000_000 / json_denominator)
        if json_denominator
        else metrics.json_validity_ppm
    )
    strongest_cluster = max(reason_counts.values(), default=0)
    normalized_metrics = OptimizationMetrics(
        reviewed_sample_count=metrics.reviewed_sample_count,
        human_override_rate_ppm=metrics.human_override_rate_ppm,
        baseline_human_override_rate_ppm=int(
            baseline_metrics.get(
                "human_override_rate_ppm",
                metrics.human_override_rate_ppm,
            )
        ),
        conflict_rate_ppm=metrics.conflict_rate_ppm,
        json_validity_ppm=max(0, min(1_000_000, json_validity_ppm)),
        critical_recall_ppm=critical_recall_ppm,
        baseline_critical_recall_ppm=baseline_critical_recall_ppm,
        largest_failure_cluster_count=strongest_cluster,
        new_feedback_count=metrics.new_feedback_count,
    )
    metric_document = {
        "reviewed_sample_count": normalized_metrics.reviewed_sample_count,
        "human_override_rate_ppm": normalized_metrics.human_override_rate_ppm,
        "baseline_human_override_rate_ppm": normalized_metrics.baseline_human_override_rate_ppm,
        "conflict_rate_ppm": normalized_metrics.conflict_rate_ppm,
        "json_validity_ppm": normalized_metrics.json_validity_ppm,
        "critical_recall_ppm": normalized_metrics.critical_recall_ppm,
        "baseline_critical_recall_ppm": normalized_metrics.baseline_critical_recall_ppm,
        "largest_failure_cluster_count": normalized_metrics.largest_failure_cluster_count,
        "new_feedback_count": normalized_metrics.new_feedback_count,
    }
    snapshot_kind = "window" if baseline is not None else "baseline"
    snapshot_document = {
        "version": "label-optimization-metric-snapshot/v1",
        "schedule_id": schedule.schedule_id,
        "locked_versions": {
            "label_version_id": schedule.label_version_id,
            "prompt_version_id": schedule.prompt_version_id,
            "model_version": schedule.model_version,
            "aggregation_policy_version_id": schedule.aggregation_policy_version_id,
            "eval_dataset_version_id": schedule.eval_dataset_version_id,
        },
        "snapshot_kind": snapshot_kind,
        "window_started_at": _iso(cutoff),
        "window_ended_at": _iso(utc_now),
        "metrics": metric_document,
        "reason_counts": dict(sorted(reason_counts.items())),
        "rejected_records": sorted(
            rejected, key=lambda item: (item["record_type"], item["record_id"])
        ),
    }
    snapshot_sha256 = _canonical_sha256(snapshot_document)
    snapshot = LabelOptimizationMetricSnapshot(
        snapshot_id=f"loms_{snapshot_sha256[:24]}",
        schedule_id=schedule.schedule_id,
        tenant_id=schedule.tenant_id,
        project_id=schedule.project_id,
        label_version_id=schedule.label_version_id,
        prompt_version_id=schedule.prompt_version_id,
        model_version=schedule.model_version,
        aggregation_policy_version_id=schedule.aggregation_policy_version_id,
        eval_dataset_version_id=schedule.eval_dataset_version_id,
        snapshot_kind=snapshot_kind,
        window_started_at=cutoff,
        window_ended_at=utc_now,
        metrics=metric_document,
        reason_counts=dict(sorted(reason_counts.items())),
        rejection_count=len(rejected),
        rejected_records=snapshot_document["rejected_records"],
        provenance={
            **provenance,
            "producer": "label_optimization_scheduler",
            "canonical_reason_registry": "v1",
            "baseline_source": (
                "label_optimization_metric_snapshots"
                if baseline is not None
                else "first_strong_window"
            ),
            "critical_recall_source": "label_eval_results",
        },
        snapshot_sha256=snapshot_sha256,
        trace_id=ctx.trace_id,
    )
    session.add(snapshot)
    session.flush()
    if schedule.baseline_snapshot_id is None:
        schedule.baseline_snapshot_id = snapshot.snapshot_id
    data = metric_snapshot_data(snapshot)
    record_audit(
        session,
        ctx,
        action="label_optimization_metric_snapshot.create",
        object_type="label_optimization_metric_snapshot",
        object_id=snapshot.snapshot_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.metric_snapshot.created",
        aggregate_type="label_optimization_metric_snapshot",
        aggregate_id=snapshot.snapshot_id,
        payload={
            "snapshot_id": snapshot.snapshot_id,
            "schedule_id": schedule.schedule_id,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "snapshot_kind": snapshot.snapshot_kind,
            "rejection_count": snapshot.rejection_count,
        },
    )
    return snapshot


def create_round(
    session: Session,
    ctx: RequestContext,
    schedule: LabelOptimizationSchedule,
    *,
    optimization_run_id: str,
    generation_run_id: str,
    round_number: int,
    prompt_version_id: str,
    now: datetime,
    consecutive_failed_rounds: int = 0,
) -> LabelOptimizationRound:
    round_id = f"lor_{_canonical_sha256([optimization_run_id, round_number])[:24]}"
    existing = session.get(LabelOptimizationRound, round_id)
    if existing is not None:
        return existing
    row = LabelOptimizationRound(
        round_id=round_id,
        schedule_id=schedule.schedule_id,
        tenant_id=schedule.tenant_id,
        project_id=schedule.project_id,
        optimization_run_id=optimization_run_id,
        generation_run_id=generation_run_id,
        round_number=round_number,
        label_version_id=schedule.label_version_id,
        prompt_version_id=prompt_version_id,
        model_version=schedule.model_version,
        aggregation_policy_version_id=schedule.aggregation_policy_version_id,
        eval_dataset_version_id=schedule.eval_dataset_version_id,
        status="generating-candidates",
        candidate_count=0,
        candidate_ids=[],
        eval_run_ids=[],
        critical_metric_regressed=False,
        cost_spent_micros=0,
        consecutive_failed_rounds=consecutive_failed_rounds,
        stop_reason_codes=[],
        started_at=_aware_utc(now),
        trace_id=ctx.trace_id,
        payload={
            "budget": schedule.budget,
            "next_action": "await-candidate-materialization",
        },
    )
    session.add(row)
    session.flush()
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.round.created",
        aggregate_type="label_optimization_round",
        aggregate_id=row.round_id,
        payload=round_data(row),
    )
    record_audit(
        session,
        ctx,
        action="label_optimization_round.create",
        object_type="label_optimization_round",
        object_id=row.round_id,
        after=round_data(row),
    )
    return row


def scheduler_context(schedule: LabelOptimizationSchedule, *, trace_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=schedule.tenant_id,
        project_id=schedule.project_id,
        user_id="label-optimization-scheduler",
        roles=("project_admin", "model_engineer"),
        request_id=f"scheduler-{uuid.uuid4().hex[:20]}",
        trace_id=trace_id,
        idempotency_key=f"scheduler:{schedule.schedule_id}:{trace_id}",
        correlation_id=trace_id,
    )


__all__ = [
    "CANONICAL_FAILURE_REASON_CODES",
    "DEFAULT_BUDGET",
    "create_or_update_schedule",
    "create_round",
    "get_schedule",
    "iteration_budget_from_data",
    "list_metric_snapshots",
    "list_rounds",
    "list_schedules",
    "metric_snapshot_data",
    "next_daily_at",
    "next_weekly_at",
    "produce_metric_snapshot",
    "round_data",
    "schedule_data",
    "scheduler_context",
]
