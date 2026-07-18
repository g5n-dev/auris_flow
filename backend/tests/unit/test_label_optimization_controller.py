from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.label_optimization import (
    IterationBudget,
    IterationState,
    OptimizationMetrics,
    OptimizationScope,
    StopReason,
    TriggerBlockReason,
    TriggerContext,
    TriggerKind,
    TriggerReason,
    canonical_trigger_hash,
    evaluate_iteration_budget,
    evaluate_optimization_trigger,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI)
SCOPE = OptimizationScope("tenant-a", "project-a", "label-version-7")


def metrics(**overrides: int) -> OptimizationMetrics:
    values = {
        "reviewed_sample_count": 200,
        "human_override_rate_ppm": 100_000,
        "baseline_human_override_rate_ppm": 100_000,
        "conflict_rate_ppm": 10_000,
        "json_validity_ppm": 1_000_000,
        "critical_recall_ppm": 900_000,
        "baseline_critical_recall_ppm": 900_000,
        "largest_failure_cluster_count": 0,
        "new_feedback_count": 0,
    }
    values.update(overrides)
    return OptimizationMetrics(**values)


def context(**overrides: object) -> TriggerContext:
    values: dict[str, object] = {
        "scope": SCOPE,
        "now": NOW,
        "metrics": metrics(),
        "last_threshold_scan_at": NOW - timedelta(minutes=15),
        "last_daily_trigger_at": datetime(2026, 7, 15, 2, 0, tzinfo=SHANGHAI),
        "last_weekly_trigger_at": datetime(2026, 7, 12, 2, 0, tzinfo=SHANGHAI),
    }
    values.update(overrides)
    return TriggerContext(**values)


def iteration_state(**overrides: object) -> IterationState:
    values: dict[str, object] = {
        "started_at": NOW - timedelta(minutes=10),
        "now": NOW,
        "completed_rounds": 0,
        "requested_candidate_count": 2,
    }
    values.update(overrides)
    return IterationState(**values)


def test_threshold_scan_runs_no_more_often_than_every_fifteen_minutes() -> None:
    breached = metrics(conflict_rate_ppm=50_001)

    too_early = evaluate_optimization_trigger(
        context(
            metrics=breached,
            last_threshold_scan_at=NOW - timedelta(minutes=14, seconds=59),
        )
    )
    on_boundary = evaluate_optimization_trigger(
        context(metrics=breached, last_threshold_scan_at=NOW - timedelta(minutes=15))
    )

    assert too_early.threshold_scan_due is False
    assert too_early.should_trigger is False
    assert on_boundary.threshold_scan_due is True
    assert on_boundary.should_trigger is True
    assert on_boundary.kind is TriggerKind.THRESHOLD


def test_threshold_trigger_requires_two_hundred_reviewed_samples() -> None:
    below_minimum = evaluate_optimization_trigger(
        context(metrics=metrics(reviewed_sample_count=199, conflict_rate_ppm=50_001))
    )
    at_minimum = evaluate_optimization_trigger(
        context(metrics=metrics(reviewed_sample_count=200, conflict_rate_ppm=50_001))
    )

    assert below_minimum.should_trigger is False
    assert below_minimum.blocked_reasons == (TriggerBlockReason.INSUFFICIENT_REVIEWED_SAMPLES,)
    assert at_minimum.should_trigger is True


@pytest.mark.parametrize(
    ("metric_overrides", "reason"),
    [
        (
            {
                "human_override_rate_ppm": 130_000,
                "baseline_human_override_rate_ppm": 100_000,
            },
            TriggerReason.HUMAN_OVERRIDE_RATE_INCREASED,
        ),
        ({"conflict_rate_ppm": 50_001}, TriggerReason.CONFLICT_RATE_HIGH),
        ({"json_validity_ppm": 994_999}, TriggerReason.JSON_VALIDITY_LOW),
        (
            {"critical_recall_ppm": 879_999, "baseline_critical_recall_ppm": 900_000},
            TriggerReason.CRITICAL_RECALL_DROPPED,
        ),
        ({"largest_failure_cluster_count": 20}, TriggerReason.FAILURE_CLUSTER_GROWN),
    ],
)
def test_each_threshold_condition_triggers(
    metric_overrides: dict[str, int],
    reason: TriggerReason,
) -> None:
    decision = evaluate_optimization_trigger(context(metrics=metrics(**metric_overrides)))

    assert decision.should_trigger is True
    assert decision.kind is TriggerKind.THRESHOLD
    assert reason in decision.reason_codes


@pytest.mark.parametrize(
    "metric_overrides",
    [
        {
            "human_override_rate_ppm": 129_999,
            "baseline_human_override_rate_ppm": 100_000,
        },
        {"conflict_rate_ppm": 50_000},
        {"json_validity_ppm": 995_000},
        {"critical_recall_ppm": 880_000, "baseline_critical_recall_ppm": 900_000},
        {"largest_failure_cluster_count": 19},
    ],
)
def test_threshold_boundaries_match_the_goal(metric_overrides: dict[str, int]) -> None:
    decision = evaluate_optimization_trigger(context(metrics=metrics(**metric_overrides)))

    assert decision.should_trigger is False
    assert decision.reason_codes == ()


def test_daily_incremental_fallback_requires_fifty_new_feedback_items() -> None:
    daily_now = datetime(2026, 7, 15, 2, 0, tzinfo=SHANGHAI)
    last_daily = datetime(2026, 7, 14, 2, 0, tzinfo=SHANGHAI)

    below_minimum = evaluate_optimization_trigger(
        context(
            now=daily_now,
            metrics=metrics(reviewed_sample_count=0, new_feedback_count=49),
            last_threshold_scan_at=daily_now,
            last_daily_trigger_at=last_daily,
        )
    )
    at_minimum = evaluate_optimization_trigger(
        context(
            now=daily_now,
            metrics=metrics(reviewed_sample_count=0, new_feedback_count=50),
            last_threshold_scan_at=daily_now,
            last_daily_trigger_at=last_daily,
        )
    )

    assert below_minimum.should_trigger is False
    assert below_minimum.blocked_reasons == (TriggerBlockReason.INSUFFICIENT_DAILY_FEEDBACK,)
    assert at_minimum.should_trigger is True
    assert at_minimum.kind is TriggerKind.DAILY_INCREMENTAL
    assert at_minimum.reason_codes == (TriggerReason.DAILY_SCHEDULE_DUE,)


def test_weekly_full_fallback_catches_up_after_sunday_boundary() -> None:
    monday = datetime(2026, 7, 20, 10, 0, tzinfo=SHANGHAI)
    previous_week = datetime(2026, 7, 12, 2, 0, tzinfo=SHANGHAI)

    decision = evaluate_optimization_trigger(
        context(
            now=monday,
            metrics=metrics(reviewed_sample_count=0),
            last_threshold_scan_at=monday,
            last_weekly_trigger_at=previous_week,
        )
    )

    assert decision.should_trigger is True
    assert decision.kind is TriggerKind.WEEKLY_FULL
    assert decision.reason_codes == (TriggerReason.WEEKLY_SCHEDULE_DUE,)


def test_weekly_full_has_priority_over_threshold_and_daily_work() -> None:
    sunday = datetime(2026, 7, 19, 2, 0, tzinfo=SHANGHAI)

    decision = evaluate_optimization_trigger(
        context(
            now=sunday,
            metrics=metrics(conflict_rate_ppm=50_001, new_feedback_count=50),
            last_threshold_scan_at=sunday - timedelta(minutes=15),
            last_daily_trigger_at=sunday - timedelta(days=1),
            last_weekly_trigger_at=sunday - timedelta(days=7),
        )
    )

    assert decision.kind is TriggerKind.WEEKLY_FULL
    assert decision.reason_codes == (TriggerReason.WEEKLY_SCHEDULE_DUE,)


def test_trigger_hash_is_canonical_within_scan_bucket_and_reason_order() -> None:
    period_start = datetime(2026, 7, 15, 10, 0, tzinfo=SHANGHAI)
    reasons = [TriggerReason.CONFLICT_RATE_HIGH, TriggerReason.JSON_VALIDITY_LOW]

    first = canonical_trigger_hash(
        scope=SCOPE,
        trigger_kind=TriggerKind.THRESHOLD,
        reasons=reasons,
        period_start_at=period_start,
    )
    reordered = canonical_trigger_hash(
        scope=SCOPE,
        trigger_kind=TriggerKind.THRESHOLD,
        reasons=reversed(reasons),
        period_start_at=period_start,
    )
    another_scope = canonical_trigger_hash(
        scope=OptimizationScope("tenant-a", "project-b", "label-version-7"),
        trigger_kind=TriggerKind.THRESHOLD,
        reasons=reasons,
        period_start_at=period_start,
    )

    assert first == reordered
    assert first != another_scope
    assert len(first) == 64


def test_trigger_hash_requires_at_least_one_reason() -> None:
    with pytest.raises(ValueError, match="at least one"):
        canonical_trigger_hash(
            scope=SCOPE,
            trigger_kind=TriggerKind.THRESHOLD,
            reasons=[],
            period_start_at=NOW,
        )


def test_same_scope_active_run_blocks_but_another_scope_does_not() -> None:
    breached = metrics(conflict_rate_ppm=50_001)
    other_scope = OptimizationScope("tenant-a", "project-b", "label-version-7")

    same_scope = evaluate_optimization_trigger(
        context(metrics=breached, active_run_scopes=frozenset({SCOPE, other_scope}))
    )
    different_scope = evaluate_optimization_trigger(
        context(metrics=breached, active_run_scopes=frozenset({other_scope}))
    )

    assert same_scope.should_trigger is False
    assert same_scope.blocked_reasons == (TriggerBlockReason.ACTIVE_RUN_EXISTS,)
    assert same_scope.canonical_hash is not None
    assert different_scope.should_trigger is True


def test_scope_cooldown_is_twenty_four_hours_with_exact_boundary_allowed() -> None:
    breached = metrics(conflict_rate_ppm=50_001)

    cooling_down = evaluate_optimization_trigger(
        context(metrics=breached, last_scope_triggered_at=NOW - timedelta(hours=23, minutes=59))
    )
    boundary = evaluate_optimization_trigger(
        context(metrics=breached, last_scope_triggered_at=NOW - timedelta(hours=24))
    )

    assert cooling_down.should_trigger is False
    assert cooling_down.blocked_reasons == (TriggerBlockReason.COOLDOWN_ACTIVE,)
    assert boundary.should_trigger is True


def test_schedule_boundaries_do_not_run_before_local_two_am() -> None:
    before_daily = datetime(2026, 7, 15, 1, 0, tzinfo=SHANGHAI)
    before_weekly = datetime(2026, 7, 19, 1, 0, tzinfo=SHANGHAI)

    daily = evaluate_optimization_trigger(
        context(
            now=before_daily,
            last_threshold_scan_at=before_daily,
            last_daily_trigger_at=datetime(2026, 7, 14, 2, 0, tzinfo=SHANGHAI),
            last_weekly_trigger_at=datetime(2026, 7, 12, 2, 0, tzinfo=SHANGHAI),
        )
    )
    weekly = evaluate_optimization_trigger(
        context(
            now=before_weekly,
            last_threshold_scan_at=before_weekly,
            last_daily_trigger_at=datetime(2026, 7, 18, 2, 0, tzinfo=SHANGHAI),
            last_weekly_trigger_at=datetime(2026, 7, 12, 2, 0, tzinfo=SHANGHAI),
        )
    )

    assert daily.should_trigger is False
    assert weekly.should_trigger is False


@pytest.mark.parametrize("candidate_count", [2, 3, 4, 5])
def test_iteration_allows_two_to_five_candidates(candidate_count: int) -> None:
    decision = evaluate_iteration_budget(iteration_state(requested_candidate_count=candidate_count))

    assert decision.should_stop is False
    assert decision.can_start_next_round is True
    assert decision.stop_reasons == ()


@pytest.mark.parametrize("candidate_count", [1, 6])
def test_iteration_rejects_candidate_count_outside_two_to_five(candidate_count: int) -> None:
    decision = evaluate_iteration_budget(iteration_state(requested_candidate_count=candidate_count))

    assert decision.should_stop is True
    assert decision.stop_reasons == (StopReason.CANDIDATE_COUNT_OUT_OF_RANGE,)


def test_iteration_stops_at_three_rounds_and_two_hours() -> None:
    round_limit = evaluate_iteration_budget(iteration_state(completed_rounds=3))
    time_limit = evaluate_iteration_budget(iteration_state(started_at=NOW - timedelta(hours=2)))

    assert round_limit.stop_reasons == (StopReason.MAX_ROUNDS_REACHED,)
    assert round_limit.remaining_rounds == 0
    assert time_limit.stop_reasons == (StopReason.TIME_BUDGET_EXCEEDED,)
    assert time_limit.remaining_seconds == 0


def test_iteration_supports_project_cost_budget() -> None:
    budget = IterationBudget(max_cost_micros=1_000)

    below = evaluate_iteration_budget(
        iteration_state(cost_spent_micros=999),
        budget=budget,
    )
    at_limit = evaluate_iteration_budget(
        iteration_state(cost_spent_micros=1_000),
        budget=budget,
    )

    assert below.should_stop is False
    assert at_limit.stop_reasons == (StopReason.COST_BUDGET_EXCEEDED,)


def test_iteration_budget_cannot_expand_the_hard_candidate_range() -> None:
    with pytest.raises(ValueError, match="hard 2-5 range"):
        IterationBudget(min_candidates_per_round=1)
    with pytest.raises(ValueError, match="hard 2-5 range"):
        IterationBudget(max_candidates_per_round=6)


@pytest.mark.parametrize(
    ("budget_overrides", "message"),
    [
        ({"max_rounds": 4}, "max_rounds"),
        ({"max_elapsed": timedelta(0)}, "max_elapsed"),
        ({"max_cost_micros": 0}, "max_cost_micros"),
        ({"min_meaningful_gain_ppm": -1}, "min_meaningful_gain_ppm"),
        ({"max_consecutive_failed_rounds": 0}, "max_consecutive_failed_rounds"),
    ],
)
def test_iteration_budget_rejects_unsafe_configuration(
    budget_overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        IterationBudget(**budget_overrides)


def test_iteration_reports_all_quality_and_safety_stop_reasons() -> None:
    decision = evaluate_iteration_budget(
        iteration_state(
            completed_rounds=1,
            latest_gain_ppm=19_999,
            critical_metric_regressed=True,
            unresolved_high_risk_count=1,
            consecutive_failed_rounds=2,
        )
    )

    assert decision.should_stop is True
    assert decision.can_start_next_round is False
    assert decision.stop_reasons == (
        StopReason.NO_MEANINGFUL_GAIN,
        StopReason.CRITICAL_METRIC_REGRESSION,
        StopReason.UNRESOLVED_HIGH_RISK,
        StopReason.REPEATED_FAILURE,
    )


def test_datetime_inputs_must_be_timezone_aware() -> None:
    naive = datetime(2026, 7, 15, 10, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        context(now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        iteration_state(now=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_trigger_hash(
            scope=SCOPE,
            trigger_kind=TriggerKind.THRESHOLD,
            reasons=[TriggerReason.CONFLICT_RATE_HIGH],
            period_start_at=naive,
        )


def test_domain_inputs_reject_impossible_values() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        OptimizationScope("", "project-a", "label-version-7")
    with pytest.raises(ValueError, match="counts"):
        metrics(reviewed_sample_count=-1)
    with pytest.raises(ValueError, match="ppm"):
        metrics(conflict_rate_ppm=1_000_001)
    with pytest.raises(ValueError, match="later than now"):
        context(last_threshold_scan_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="OptimizationScope"):
        context(active_run_scopes=frozenset({"not-a-scope"}))
    with pytest.raises(ValueError, match="completed_rounds"):
        iteration_state(completed_rounds=-1)
    with pytest.raises(ValueError, match="cost_spent_micros"):
        iteration_state(cost_spent_micros=-1)
    with pytest.raises(ValueError, match="unresolved_high_risk_count"):
        iteration_state(unresolved_high_risk_count=-1)
    with pytest.raises(ValueError, match="consecutive_failed_rounds"):
        iteration_state(consecutive_failed_rounds=-1)
    with pytest.raises(ValueError, match="cannot be earlier"):
        iteration_state(started_at=NOW + timedelta(seconds=1))
