from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

PPM_SCALE = 1_000_000
THRESHOLD_SCAN_INTERVAL = timedelta(minutes=15)
TRIGGER_COOLDOWN = timedelta(hours=24)
MIN_REVIEWED_SAMPLES = 200
MIN_DAILY_FEEDBACK = 50
MAX_ROUNDS = 3
MIN_CANDIDATES_PER_ROUND = 2
MAX_CANDIDATES_PER_ROUND = 5
MAX_ITERATION_ELAPSED = timedelta(hours=2)

HUMAN_OVERRIDE_DELTA_THRESHOLD_PPM = 30_000
CONFLICT_RATE_THRESHOLD_PPM = 50_000
JSON_VALIDITY_THRESHOLD_PPM = 995_000
CRITICAL_RECALL_DROP_THRESHOLD_PPM = 20_000
FAILURE_CLUSTER_THRESHOLD = 20


class TriggerKind(StrEnum):
    THRESHOLD = "threshold"
    DAILY_INCREMENTAL = "daily_incremental"
    WEEKLY_FULL = "weekly_full"


class TriggerReason(StrEnum):
    HUMAN_OVERRIDE_RATE_INCREASED = "human_override_rate_increased"
    CONFLICT_RATE_HIGH = "conflict_rate_high"
    JSON_VALIDITY_LOW = "json_validity_low"
    CRITICAL_RECALL_DROPPED = "critical_recall_dropped"
    FAILURE_CLUSTER_GROWN = "failure_cluster_grown"
    DAILY_SCHEDULE_DUE = "daily_schedule_due"
    WEEKLY_SCHEDULE_DUE = "weekly_schedule_due"


class TriggerBlockReason(StrEnum):
    INSUFFICIENT_REVIEWED_SAMPLES = "insufficient_reviewed_samples"
    INSUFFICIENT_DAILY_FEEDBACK = "insufficient_daily_feedback"
    ACTIVE_RUN_EXISTS = "active_run_exists"
    COOLDOWN_ACTIVE = "cooldown_active"


class StopReason(StrEnum):
    CANDIDATE_COUNT_OUT_OF_RANGE = "candidate_count_out_of_range"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    TIME_BUDGET_EXCEEDED = "time_budget_exceeded"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    NO_MEANINGFUL_GAIN = "no_meaningful_gain"
    CRITICAL_METRIC_REGRESSION = "critical_metric_regression"
    UNRESOLVED_HIGH_RISK = "unresolved_high_risk"
    REPEATED_FAILURE = "repeated_failure"


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _as_utc(value: datetime) -> datetime:
    _require_aware(value, field_name="datetime")
    return value.astimezone(UTC)


def _elapsed(later: datetime, earlier: datetime) -> timedelta:
    return _as_utc(later) - _as_utc(earlier)


@dataclass(frozen=True, slots=True)
class OptimizationScope:
    tenant_id: str
    project_id: str
    label_version_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "project_id", "label_version_id"):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class OptimizationMetrics:
    reviewed_sample_count: int
    human_override_rate_ppm: int
    baseline_human_override_rate_ppm: int
    conflict_rate_ppm: int
    json_validity_ppm: int
    critical_recall_ppm: int
    baseline_critical_recall_ppm: int
    largest_failure_cluster_count: int
    new_feedback_count: int

    def __post_init__(self) -> None:
        count_fields = (
            "reviewed_sample_count",
            "largest_failure_cluster_count",
            "new_feedback_count",
        )
        if any(getattr(self, name) < 0 for name in count_fields):
            raise ValueError("metric counts must be non-negative")

        ppm_fields = (
            "human_override_rate_ppm",
            "baseline_human_override_rate_ppm",
            "conflict_rate_ppm",
            "json_validity_ppm",
            "critical_recall_ppm",
            "baseline_critical_recall_ppm",
        )
        if any(not 0 <= getattr(self, name) <= PPM_SCALE for name in ppm_fields):
            raise ValueError("ppm metrics must be between 0 and 1,000,000")


@dataclass(frozen=True, slots=True)
class TriggerContext:
    scope: OptimizationScope
    now: datetime
    metrics: OptimizationMetrics
    last_threshold_scan_at: datetime | None = None
    last_scope_triggered_at: datetime | None = None
    last_daily_trigger_at: datetime | None = None
    last_weekly_trigger_at: datetime | None = None
    active_run_scopes: frozenset[OptimizationScope] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require_aware(self.now, field_name="now")
        timestamp_fields = (
            "last_threshold_scan_at",
            "last_scope_triggered_at",
            "last_daily_trigger_at",
            "last_weekly_trigger_at",
        )
        for field_name in timestamp_fields:
            value = getattr(self, field_name)
            if value is None:
                continue
            _require_aware(value, field_name=field_name)
            if _as_utc(value) > _as_utc(self.now):
                raise ValueError(f"{field_name} cannot be later than now")
        if any(not isinstance(scope, OptimizationScope) for scope in self.active_run_scopes):
            raise ValueError("active_run_scopes must contain OptimizationScope values")


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    should_trigger: bool
    kind: TriggerKind | None
    reason_codes: tuple[TriggerReason, ...]
    canonical_hash: str | None
    threshold_scan_due: bool
    blocked_reasons: tuple[TriggerBlockReason, ...] = ()


def canonical_trigger_hash(
    *,
    scope: OptimizationScope,
    trigger_kind: TriggerKind,
    reasons: Iterable[TriggerReason],
    period_start_at: datetime,
) -> str:
    """Hash semantic trigger identity independent of reason iteration order."""
    _require_aware(period_start_at, field_name="period_start_at")
    reason_values = sorted({TriggerReason(reason).value for reason in reasons})
    if not reason_values:
        raise ValueError("at least one trigger reason is required")
    period = _as_utc(period_start_at).isoformat(timespec="seconds").replace("+00:00", "Z")
    payload = {
        "period_start_at": period,
        "reasons": reason_values,
        "scope": {
            "label_version_id": scope.label_version_id,
            "project_id": scope.project_id,
            "tenant_id": scope.tenant_id,
        },
        "trigger_kind": TriggerKind(trigger_kind).value,
        "version": "label-optimization-trigger/1",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _threshold_reasons(metrics: OptimizationMetrics) -> tuple[TriggerReason, ...]:
    reasons: list[TriggerReason] = []
    if (
        metrics.human_override_rate_ppm - metrics.baseline_human_override_rate_ppm
        >= HUMAN_OVERRIDE_DELTA_THRESHOLD_PPM
    ):
        reasons.append(TriggerReason.HUMAN_OVERRIDE_RATE_INCREASED)
    if metrics.conflict_rate_ppm > CONFLICT_RATE_THRESHOLD_PPM:
        reasons.append(TriggerReason.CONFLICT_RATE_HIGH)
    if metrics.json_validity_ppm < JSON_VALIDITY_THRESHOLD_PPM:
        reasons.append(TriggerReason.JSON_VALIDITY_LOW)
    if (
        metrics.baseline_critical_recall_ppm - metrics.critical_recall_ppm
        > CRITICAL_RECALL_DROP_THRESHOLD_PPM
    ):
        reasons.append(TriggerReason.CRITICAL_RECALL_DROPPED)
    if metrics.largest_failure_cluster_count >= FAILURE_CLUSTER_THRESHOLD:
        reasons.append(TriggerReason.FAILURE_CLUSTER_GROWN)
    return tuple(reasons)


def _threshold_period_start(now: datetime) -> datetime:
    utc_now = _as_utc(now)
    minute = utc_now.minute - utc_now.minute % 15
    return utc_now.replace(minute=minute, second=0, microsecond=0)


def _most_recent_daily_boundary(now: datetime) -> datetime:
    boundary = now.replace(hour=2, minute=0, second=0, microsecond=0)
    if _as_utc(boundary) > _as_utc(now):
        boundary -= timedelta(days=1)
    return boundary


def _most_recent_weekly_boundary(now: datetime) -> datetime:
    boundary = now.replace(hour=2, minute=0, second=0, microsecond=0)
    days_since_sunday = (boundary.weekday() + 1) % 7
    boundary -= timedelta(days=days_since_sunday)
    if _as_utc(boundary) > _as_utc(now):
        boundary -= timedelta(days=7)
    return boundary


def _not_run_since(last_run_at: datetime | None, boundary: datetime) -> bool:
    return last_run_at is None or _as_utc(last_run_at) < _as_utc(boundary)


def evaluate_optimization_trigger(context: TriggerContext) -> TriggerDecision:
    """Select one due trigger and apply same-scope single-active and cooldown gates."""
    scan_due = (
        context.last_threshold_scan_at is None
        or _elapsed(
            context.now,
            context.last_threshold_scan_at,
        )
        >= THRESHOLD_SCAN_INTERVAL
    )
    raw_threshold_reasons = _threshold_reasons(context.metrics) if scan_due else ()
    threshold_reasons = (
        raw_threshold_reasons
        if context.metrics.reviewed_sample_count >= MIN_REVIEWED_SAMPLES
        else ()
    )

    daily_boundary = _most_recent_daily_boundary(context.now)
    weekly_boundary = _most_recent_weekly_boundary(context.now)
    daily_due = _not_run_since(context.last_daily_trigger_at, daily_boundary)
    weekly_due = _not_run_since(context.last_weekly_trigger_at, weekly_boundary)

    kind: TriggerKind | None = None
    reasons: tuple[TriggerReason, ...] = ()
    period_start_at: datetime | None = None
    if weekly_due:
        kind = TriggerKind.WEEKLY_FULL
        reasons = (TriggerReason.WEEKLY_SCHEDULE_DUE,)
        period_start_at = weekly_boundary
    elif threshold_reasons:
        kind = TriggerKind.THRESHOLD
        reasons = threshold_reasons
        period_start_at = _threshold_period_start(context.now)
    elif daily_due and context.metrics.new_feedback_count >= MIN_DAILY_FEEDBACK:
        kind = TriggerKind.DAILY_INCREMENTAL
        reasons = (TriggerReason.DAILY_SCHEDULE_DUE,)
        period_start_at = daily_boundary

    if kind is None or period_start_at is None:
        blockers: list[TriggerBlockReason] = []
        if raw_threshold_reasons and context.metrics.reviewed_sample_count < MIN_REVIEWED_SAMPLES:
            blockers.append(TriggerBlockReason.INSUFFICIENT_REVIEWED_SAMPLES)
        if daily_due and context.metrics.new_feedback_count < MIN_DAILY_FEEDBACK:
            blockers.append(TriggerBlockReason.INSUFFICIENT_DAILY_FEEDBACK)
        return TriggerDecision(
            should_trigger=False,
            kind=None,
            reason_codes=(),
            canonical_hash=None,
            threshold_scan_due=scan_due,
            blocked_reasons=tuple(blockers),
        )

    trigger_hash = canonical_trigger_hash(
        scope=context.scope,
        trigger_kind=kind,
        reasons=reasons,
        period_start_at=period_start_at,
    )
    safety_blockers: list[TriggerBlockReason] = []
    if context.scope in context.active_run_scopes:
        safety_blockers.append(TriggerBlockReason.ACTIVE_RUN_EXISTS)
    if (
        context.last_scope_triggered_at is not None
        and _elapsed(
            context.now,
            context.last_scope_triggered_at,
        )
        < TRIGGER_COOLDOWN
    ):
        safety_blockers.append(TriggerBlockReason.COOLDOWN_ACTIVE)

    return TriggerDecision(
        should_trigger=not safety_blockers,
        kind=kind,
        reason_codes=reasons,
        canonical_hash=trigger_hash,
        threshold_scan_due=scan_due,
        blocked_reasons=tuple(safety_blockers),
    )


@dataclass(frozen=True, slots=True)
class IterationBudget:
    max_rounds: int = MAX_ROUNDS
    min_candidates_per_round: int = MIN_CANDIDATES_PER_ROUND
    max_candidates_per_round: int = MAX_CANDIDATES_PER_ROUND
    max_elapsed: timedelta = MAX_ITERATION_ELAPSED
    max_cost_micros: int | None = None
    min_meaningful_gain_ppm: int = 20_000
    max_consecutive_failed_rounds: int = 2

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= MAX_ROUNDS:
            raise ValueError(f"max_rounds must be between 1 and {MAX_ROUNDS}")
        if not (
            MIN_CANDIDATES_PER_ROUND
            <= self.min_candidates_per_round
            <= self.max_candidates_per_round
            <= MAX_CANDIDATES_PER_ROUND
        ):
            raise ValueError("candidate budget must stay within the hard 2-5 range")
        if not timedelta(0) < self.max_elapsed <= MAX_ITERATION_ELAPSED:
            raise ValueError("max_elapsed must be positive and cannot exceed 2 hours")
        if self.max_cost_micros is not None and self.max_cost_micros <= 0:
            raise ValueError("max_cost_micros must be positive when configured")
        if not 0 <= self.min_meaningful_gain_ppm <= PPM_SCALE:
            raise ValueError("min_meaningful_gain_ppm must be a valid ppm value")
        if self.max_consecutive_failed_rounds < 1:
            raise ValueError("max_consecutive_failed_rounds must be positive")


@dataclass(frozen=True, slots=True)
class IterationState:
    started_at: datetime
    now: datetime
    completed_rounds: int
    requested_candidate_count: int
    cost_spent_micros: int = 0
    latest_gain_ppm: int | None = None
    critical_metric_regressed: bool = False
    unresolved_high_risk_count: int = 0
    consecutive_failed_rounds: int = 0

    def __post_init__(self) -> None:
        _require_aware(self.started_at, field_name="started_at")
        _require_aware(self.now, field_name="now")
        if _as_utc(self.now) < _as_utc(self.started_at):
            raise ValueError("now cannot be earlier than started_at")
        if self.completed_rounds < 0:
            raise ValueError("completed_rounds must be non-negative")
        if self.cost_spent_micros < 0:
            raise ValueError("cost_spent_micros must be non-negative")
        if self.unresolved_high_risk_count < 0:
            raise ValueError("unresolved_high_risk_count must be non-negative")
        if self.consecutive_failed_rounds < 0:
            raise ValueError("consecutive_failed_rounds must be non-negative")


@dataclass(frozen=True, slots=True)
class IterationBudgetDecision:
    should_stop: bool
    can_start_next_round: bool
    stop_reasons: tuple[StopReason, ...]
    remaining_rounds: int
    remaining_seconds: int


def evaluate_iteration_budget(
    state: IterationState,
    *,
    budget: IterationBudget | None = None,
) -> IterationBudgetDecision:
    """Evaluate hard resource budgets and fail-safe quality stop conditions."""
    effective_budget = budget or IterationBudget()
    elapsed = _elapsed(state.now, state.started_at)
    reasons: list[StopReason] = []

    if not (
        effective_budget.min_candidates_per_round
        <= state.requested_candidate_count
        <= effective_budget.max_candidates_per_round
    ):
        reasons.append(StopReason.CANDIDATE_COUNT_OUT_OF_RANGE)
    if state.completed_rounds >= effective_budget.max_rounds:
        reasons.append(StopReason.MAX_ROUNDS_REACHED)
    if elapsed >= effective_budget.max_elapsed:
        reasons.append(StopReason.TIME_BUDGET_EXCEEDED)
    if (
        effective_budget.max_cost_micros is not None
        and state.cost_spent_micros >= effective_budget.max_cost_micros
    ):
        reasons.append(StopReason.COST_BUDGET_EXCEEDED)
    if (
        state.completed_rounds > 0
        and state.latest_gain_ppm is not None
        and state.latest_gain_ppm < effective_budget.min_meaningful_gain_ppm
    ):
        reasons.append(StopReason.NO_MEANINGFUL_GAIN)
    if state.critical_metric_regressed:
        reasons.append(StopReason.CRITICAL_METRIC_REGRESSION)
    if state.unresolved_high_risk_count > 0:
        reasons.append(StopReason.UNRESOLVED_HIGH_RISK)
    if state.consecutive_failed_rounds >= effective_budget.max_consecutive_failed_rounds:
        reasons.append(StopReason.REPEATED_FAILURE)

    remaining = max(timedelta(0), effective_budget.max_elapsed - elapsed)
    return IterationBudgetDecision(
        should_stop=bool(reasons),
        can_start_next_round=not reasons,
        stop_reasons=tuple(reasons),
        remaining_rounds=max(0, effective_budget.max_rounds - state.completed_rounds),
        remaining_seconds=int(remaining.total_seconds()),
    )


__all__ = [
    "IterationBudget",
    "IterationBudgetDecision",
    "IterationState",
    "OptimizationMetrics",
    "OptimizationScope",
    "StopReason",
    "TriggerBlockReason",
    "TriggerContext",
    "TriggerDecision",
    "TriggerKind",
    "TriggerReason",
    "canonical_trigger_hash",
    "evaluate_iteration_budget",
    "evaluate_optimization_trigger",
]
