from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import get_logger, log_event
from app.domain.label_optimization import IterationState, StopReason, evaluate_iteration_budget
from app.models import (
    LabelEvalResult,
    LabelOptimizationRound,
    LabelOptimizationSchedule,
    RunRecord,
)
from app.services.audit_service import record_audit
from app.services.eval_binding_service import validate_labeling_eval_binding
from app.services.label_optimization_orchestrator import execute_trigger_scan
from app.services.label_optimization_runtime_service import (
    create_round,
    iteration_budget_from_data,
    metric_snapshot_data,
    next_daily_at,
    next_weekly_at,
    produce_metric_snapshot,
    round_data,
    scheduler_context,
)
from app.services.outbox_service import enqueue_event

logger = get_logger("worker.label_optimization")
TERMINAL_EVAL_STATUSES = frozenset({"success", "blocked", "failed", "cancelled"})
RESOURCE_STOP_REASONS = frozenset(
    {
        StopReason.TIME_BUDGET_EXCEEDED.value,
        StopReason.COST_BUDGET_EXCEEDED.value,
        StopReason.CRITICAL_METRIC_REGRESSION.value,
        StopReason.UNRESOLVED_HIGH_RISK.value,
    }
)
PROMPT_VERSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _due_kinds(schedule: LabelOptimizationSchedule, now: datetime) -> set[str]:
    utc_now = _aware_utc(now)
    due: set[str] = set()
    if _aware_utc(schedule.next_threshold_scan_at) <= utc_now:
        due.add("threshold")
    if _aware_utc(schedule.next_daily_at) <= utc_now:
        due.add("daily_incremental")
    if _aware_utc(schedule.next_weekly_at) <= utc_now:
        due.add("weekly_full")
    return due


def _advance_interval(value: datetime, now: datetime, delta: timedelta) -> datetime:
    advanced = _aware_utc(value)
    utc_now = _aware_utc(now)
    while advanced <= utc_now:
        advanced += delta
    return advanced


def _advance_due_clocks(
    schedule: LabelOptimizationSchedule,
    *,
    due_kinds: set[str],
    now: datetime,
    triggered_kind: str | None,
) -> None:
    if "threshold" in due_kinds:
        schedule.last_threshold_scanned_at = _aware_utc(now)
        schedule.next_threshold_scan_at = _advance_interval(
            schedule.next_threshold_scan_at,
            now,
            timedelta(seconds=schedule.threshold_interval_seconds),
        )
    if "daily_incremental" in due_kinds:
        schedule.next_daily_at = next_daily_at(
            now,
            schedule.schedule_timezone,
            schedule.daily_hour,
        )
        if triggered_kind == "daily_incremental":
            schedule.last_daily_started_at = _aware_utc(now)
    if "weekly_full" in due_kinds:
        schedule.next_weekly_at = next_weekly_at(
            now,
            schedule.schedule_timezone,
            schedule.weekly_day,
            schedule.daily_hour,
        )
        if triggered_kind == "weekly_full":
            schedule.last_weekly_started_at = _aware_utc(now)


def _mark_session_stage(
    session: Session,
    ctx: Any,
    *,
    root_run_id: str,
    stage: str,
    reason_codes: list[str],
    selected_prompt_version_id: str | None = None,
) -> None:
    root = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == root_run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type == "label_optimization",
        )
    )
    if root is None:
        return
    root.payload = {
        **(root.payload or {}),
        "optimization_session_id": root_run_id,
        "stage": stage,
        "business_status": stage,
        "stop_reason_codes": reason_codes,
        "selected_prompt_version_id": selected_prompt_version_id,
        "next_action": {
            "code": "review-prompt-candidate" if stage == "awaiting-review" else "inspect-blockers",
            "label": "人工审核离线评测候选"
            if stage == "awaiting-review"
            else "检查自动优化阻断原因",
        },
    }
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.session.stage_changed",
        aggregate_type="label_optimization",
        aggregate_id=root_run_id,
        payload={
            "optimization_run_id": root_run_id,
            "stage": stage,
            "stop_reason_codes": reason_codes,
            "selected_prompt_version_id": selected_prompt_version_id,
        },
    )


def _cost_micros(payload: dict[str, Any]) -> int | None:
    candidates: list[Any] = []
    for container in (
        payload.get("metrics"),
        (payload.get("completion_receipt") or {}).get("metrics")
        if isinstance(payload.get("completion_receipt"), dict)
        else None,
        payload.get("result_ref"),
    ):
        if isinstance(container, dict):
            candidates.extend([container.get("cost_micros"), container.get("total_cost_micros")])
    for raw in candidates:
        if isinstance(raw, bool):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _create_eval_runs(
    session: Session,
    ctx: Any,
    schedule: LabelOptimizationSchedule,
    round_record: LabelOptimizationRound,
    generation: RunRecord,
    *,
    now: datetime,
) -> list[str]:
    raw_candidate_ids = (generation.payload or {}).get("prompt_candidate_ids", [])
    candidate_ids = (
        list(raw_candidate_ids)
        if isinstance(raw_candidate_ids, list)
        and all(
            isinstance(item, str) and PROMPT_VERSION_ID_PATTERN.fullmatch(item)
            for item in raw_candidate_ids
        )
        else []
    )
    budget = iteration_budget_from_data(schedule.budget)
    malformed_candidates = not isinstance(raw_candidate_ids, list) or (
        bool(raw_candidate_ids) and not candidate_ids
    )
    duplicate_candidates = len(set(candidate_ids)) != len(candidate_ids)
    observed_candidate_count = len(raw_candidate_ids) if isinstance(raw_candidate_ids, list) else 0
    if (
        malformed_candidates
        or duplicate_candidates
        or not budget.min_candidates_per_round
        <= observed_candidate_count
        <= budget.max_candidates_per_round
    ):
        round_record.status = "blocked"
        # candidate_count describes an accepted 2–5 materialization and therefore
        # stays zero for malformed/out-of-range output; preserve the observed
        # count in payload without violating the strong-table hard constraint.
        round_record.candidate_count = 0
        round_record.candidate_ids = candidate_ids
        if malformed_candidates:
            reason_code = "prompt_candidate_ids_invalid"
        elif duplicate_candidates:
            reason_code = "prompt_candidate_ids_not_unique"
        else:
            reason_code = StopReason.CANDIDATE_COUNT_OUT_OF_RANGE.value
        round_record.stop_reason_codes = [reason_code]
        round_record.completed_at = _aware_utc(now)
        round_record.payload = {
            **(round_record.payload or {}),
            "observed_candidate_count": observed_candidate_count,
            "next_action": "inspect-candidate-materialization",
        }
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="blocked",
            reason_codes=round_record.stop_reason_codes,
        )
        return []

    generation_cost = _cost_micros(generation.payload or {})
    if schedule.budget.get("max_cost_micros") is not None and generation_cost is None:
        round_record.status = "blocked"
        round_record.candidate_count = len(candidate_ids)
        round_record.candidate_ids = candidate_ids
        round_record.stop_reason_codes = ["cost_metric_missing"]
        round_record.completed_at = _aware_utc(now)
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="blocked",
            reason_codes=round_record.stop_reason_codes,
        )
        return []

    eval_run_ids: list[str] = []
    for candidate_id in candidate_ids:
        digest = _canonical_sha256(
            [round_record.optimization_run_id, round_record.round_number, candidate_id]
        )
        eval_run_id = f"eval_label_opt_{digest[:24]}"
        existing = session.scalar(
            select(RunRecord).where(
                RunRecord.run_id == eval_run_id,
                RunRecord.tenant_id == schedule.tenant_id,
                RunRecord.project_id == schedule.project_id,
                RunRecord.run_type == "eval_run",
            )
        )
        if existing is None:
            payload = validate_labeling_eval_binding(
                session,
                ctx,
                {
                    "run_id": eval_run_id,
                    "dataset_id": schedule.eval_dataset_version_id,
                    "eval_dataset_version_id": schedule.eval_dataset_version_id,
                    "capability": "labeling",
                    "label_version_id": schedule.label_version_id,
                    "prompt_version_id": candidate_id,
                    "model_version": schedule.model_version,
                    "aggregation_policy_version_id": schedule.aggregation_policy_version_id,
                    "optimization_run_id": generation.run_id,
                    "evaluation_suites": [
                        "golden",
                        "boundary",
                        "adversarial",
                        "fresh",
                        "canary",
                        "regression",
                    ],
                },
            )
            record_payload = {
                **payload,
                "run_id": eval_run_id,
                "status": "queued",
                "business_status": "evaluating",
                "optimization_session_id": round_record.optimization_run_id,
                "optimization_round_id": round_record.round_id,
                "trace_id": round_record.trace_id,
                "affected_objects": [
                    {"type": "prompt_version", "id": candidate_id},
                    {
                        "type": "eval_dataset_version",
                        "id": schedule.eval_dataset_version_id,
                    },
                ],
                "next_actions": [{"key": "await_eval", "label": "等待锁定离线评测回执"}],
            }
            existing = RunRecord(
                run_id=eval_run_id,
                tenant_id=schedule.tenant_id,
                project_id=schedule.project_id,
                run_type="eval_run",
                status="queued",
                run_key=f"label-optimization-eval:{round_record.round_id}",
                partition_key=candidate_id,
                trace_id=round_record.trace_id,
                payload=record_payload,
            )
            session.add(existing)
            session.flush()
            enqueue_event(
                session,
                ctx,
                event_type="eval_run.requested",
                aggregate_type="eval_run",
                aggregate_id=eval_run_id,
                payload=record_payload,
            )
            record_audit(
                session,
                ctx,
                action="label_optimization.eval_run.create",
                object_type="eval_run",
                object_id=eval_run_id,
                after=record_payload,
            )
        eval_run_ids.append(eval_run_id)

    round_record.candidate_count = len(candidate_ids)
    round_record.candidate_ids = candidate_ids
    round_record.eval_run_ids = eval_run_ids
    round_record.cost_spent_micros = generation_cost or 0
    round_record.status = "evaluating"
    round_record.payload = {
        **(round_record.payload or {}),
        "next_action": "await-locked-eval-results",
    }
    generation.payload = {
        **(generation.payload or {}),
        "stage": "evaluating",
        "business_status": "evaluating",
        "optimization_round_id": round_record.round_id,
        "eval_run_ids": eval_run_ids,
    }
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.round.evaluation_requested",
        aggregate_type="label_optimization_round",
        aggregate_id=round_record.round_id,
        payload={
            "round_id": round_record.round_id,
            "candidate_ids": candidate_ids,
            "eval_run_ids": eval_run_ids,
        },
    )
    return eval_run_ids


def _create_next_generation(
    session: Session,
    ctx: Any,
    schedule: LabelOptimizationSchedule,
    current: LabelOptimizationRound,
    *,
    prompt_version_id: str,
    now: datetime,
    consecutive_failed_rounds: int,
) -> LabelOptimizationRound:
    next_number = current.round_number + 1
    digest = _canonical_sha256([current.optimization_run_id, "generation", next_number])
    run_id = f"label_optimization_{digest[:24]}"
    generation = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == schedule.tenant_id,
            RunRecord.project_id == schedule.project_id,
            RunRecord.run_type == "label_optimization",
        )
    )
    if generation is None:
        locked_versions = {
            "label_version_id": schedule.label_version_id,
            "prompt_version_id": prompt_version_id,
            "model_version": schedule.model_version,
            "aggregation_policy_version_id": schedule.aggregation_policy_version_id,
            "eval_dataset_version_id": schedule.eval_dataset_version_id,
        }
        payload = {
            **locked_versions,
            "locked_versions": locked_versions,
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "optimization_session_id": current.optimization_run_id,
            "optimization_round_number": next_number,
            "parent_round_id": current.round_id,
            "budget": schedule.budget,
            "trigger_kind": "iteration_followup",
            "trigger_reasons": ["evaluation_gate_refinement"],
            "trigger_hash": digest,
            "trace_id": current.trace_id,
            "affected_objects": [
                {"type": "prompt_version", "id": prompt_version_id},
                {"type": "label_version", "id": schedule.label_version_id},
            ],
            "next_actions": [
                {"code": "generate-prompt-candidates", "label": "生成下一轮 2–5 个 Prompt 候选"}
            ],
        }
        generation = RunRecord(
            run_id=run_id,
            tenant_id=schedule.tenant_id,
            project_id=schedule.project_id,
            run_type="label_optimization",
            status="queued",
            run_key=f"label-optimization:{schedule.label_version_id}",
            partition_key=schedule.label_version_id,
            trace_id=current.trace_id,
            payload=payload,
            created_at=_aware_utc(now),
            updated_at=_aware_utc(now),
        )
        session.add(generation)
        session.flush()
        enqueue_event(
            session,
            ctx,
            event_type="agent_run.requested",
            aggregate_type="label_optimization",
            aggregate_id=run_id,
            payload=payload,
        )
        record_audit(
            session,
            ctx,
            action="label_optimization.iteration.create",
            object_type="label_optimization",
            object_id=run_id,
            after=payload,
        )
    return create_round(
        session,
        ctx,
        schedule,
        optimization_run_id=current.optimization_run_id,
        generation_run_id=run_id,
        round_number=next_number,
        prompt_version_id=prompt_version_id,
        now=now,
        consecutive_failed_rounds=consecutive_failed_rounds,
    )


def _handle_generation_failure(
    session: Session,
    ctx: Any,
    schedule: LabelOptimizationSchedule,
    round_record: LabelOptimizationRound,
    generation: RunRecord,
    *,
    now: datetime,
) -> None:
    budget = iteration_budget_from_data(schedule.budget)
    session_rounds = session.scalars(
        select(LabelOptimizationRound).where(
            LabelOptimizationRound.tenant_id == schedule.tenant_id,
            LabelOptimizationRound.project_id == schedule.project_id,
            LabelOptimizationRound.optimization_run_id == round_record.optimization_run_id,
        )
    ).all()
    session_started_at = min(
        (_aware_utc(item.started_at) for item in session_rounds),
        default=_aware_utc(round_record.started_at),
    )
    failures = round_record.consecutive_failed_rounds + 1
    generation_cost = _cost_micros(generation.payload or {})
    cost_metric_missing = schedule.budget.get("max_cost_micros") is not None and (
        generation_cost is None
    )
    round_record.cost_spent_micros += generation_cost or 0
    total_cost = sum(item.cost_spent_micros for item in session_rounds)
    decision = evaluate_iteration_budget(
        IterationState(
            started_at=session_started_at,
            now=_aware_utc(now),
            completed_rounds=round_record.round_number,
            # Candidate generation failed before a materialized count existed;
            # use the locked minimum so the failure is classified by the actual
            # time/cost/round/retry budget rather than a synthetic count error.
            requested_candidate_count=budget.min_candidates_per_round,
            cost_spent_micros=total_cost,
            consecutive_failed_rounds=failures,
        ),
        budget=budget,
    )
    stop_codes = [reason.value for reason in decision.stop_reasons]
    if cost_metric_missing:
        stop_codes.append("cost_metric_missing")
    round_record.consecutive_failed_rounds = failures
    round_record.status = "failed"
    round_record.stop_reason_codes = ["candidate_generation_failed"]
    round_record.completed_at = _aware_utc(now)
    if decision.should_stop or cost_metric_missing:
        round_record.status = "blocked"
        round_record.stop_reason_codes = stop_codes
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="blocked",
            reason_codes=round_record.stop_reason_codes,
        )
        return
    _create_next_generation(
        session,
        ctx,
        schedule,
        round_record,
        prompt_version_id=round_record.prompt_version_id,
        now=now,
        consecutive_failed_rounds=failures,
    )
    generation.payload = {
        **(generation.payload or {}),
        "optimization_iteration_outcome": "retrying",
    }


def _evaluate_round(
    session: Session,
    ctx: Any,
    schedule: LabelOptimizationSchedule,
    round_record: LabelOptimizationRound,
    *,
    now: datetime,
) -> None:
    eval_runs = session.scalars(
        select(RunRecord).where(
            RunRecord.tenant_id == schedule.tenant_id,
            RunRecord.project_id == schedule.project_id,
            RunRecord.run_id.in_(round_record.eval_run_ids),
            RunRecord.run_type == "eval_run",
        )
    ).all()
    if len(eval_runs) != len(round_record.eval_run_ids):
        return
    if any(run.status not in TERMINAL_EVAL_STATUSES for run in eval_runs):
        return
    results = session.scalars(
        select(LabelEvalResult).where(
            LabelEvalResult.tenant_id == schedule.tenant_id,
            LabelEvalResult.project_id == schedule.project_id,
            LabelEvalResult.eval_run_id.in_(round_record.eval_run_ids),
        )
    ).all()
    result_by_run = {row.eval_run_id: row for row in results}
    ranked: list[tuple[float, RunRecord, LabelEvalResult]] = []
    for eval_run in eval_runs:
        result = result_by_run.get(eval_run.run_id)
        if result is None:
            continue
        gain = float((result.overall_metrics or {}).get("macro_f1_gain_pp") or 0.0)
        ranked.append((gain, eval_run, result))
    ranked.sort(key=lambda item: (item[0], item[1].run_id), reverse=True)
    passed_ranked = [item for item in ranked if item[2].status == "passed"]
    # A blocked candidate may report a larger headline gain while failing a
    # safety/quality gate.  It must never hide a lower-gain candidate whose full
    # locked EvalRun passed every gate.
    best = passed_ranked[0] if passed_ranked else (ranked[0] if ranked else None)
    selected_prompt = (
        str((best[1].payload or {}).get("prompt_version_id")) if best is not None else None
    )
    latest_gain_ppm = round(best[0] * 10_000) if best is not None else None
    critical_regressed = False
    if best is not None:
        critical_lower = float(
            (best[2].bootstrap_ci or {}).get("critical_recall_delta_lower_pp") or 0.0
        )
        critical_regressed = critical_lower < -0.5
    passed = bool(passed_ranked)
    infrastructure_failed = not ranked

    eval_costs = [_cost_micros(run.payload or {}) for run in eval_runs]
    cost_metric_missing = schedule.budget.get("max_cost_micros") is not None and any(
        value is None for value in eval_costs
    )
    round_record.cost_spent_micros += sum(value or 0 for value in eval_costs)
    all_rounds = session.scalars(
        select(LabelOptimizationRound)
        .where(
            LabelOptimizationRound.tenant_id == schedule.tenant_id,
            LabelOptimizationRound.project_id == schedule.project_id,
            LabelOptimizationRound.optimization_run_id == round_record.optimization_run_id,
        )
        .order_by(LabelOptimizationRound.round_number)
    ).all()
    total_cost = sum(item.cost_spent_micros for item in all_rounds)
    failures = round_record.consecutive_failed_rounds + (1 if infrastructure_failed else 0)
    first_started = min(_aware_utc(item.started_at) for item in all_rounds)
    decision = evaluate_iteration_budget(
        IterationState(
            started_at=first_started,
            now=_aware_utc(now),
            completed_rounds=round_record.round_number,
            requested_candidate_count=round_record.candidate_count,
            cost_spent_micros=total_cost,
            latest_gain_ppm=latest_gain_ppm,
            critical_metric_regressed=critical_regressed,
            unresolved_high_risk_count=0,
            consecutive_failed_rounds=failures,
        ),
        budget=iteration_budget_from_data(schedule.budget),
    )
    stop_codes = [reason.value for reason in decision.stop_reasons]
    if cost_metric_missing:
        stop_codes.append("cost_metric_missing")
    round_record.selected_prompt_version_id = selected_prompt
    round_record.latest_gain_ppm = latest_gain_ppm
    round_record.critical_metric_regressed = critical_regressed
    round_record.consecutive_failed_rounds = failures
    round_record.completed_at = _aware_utc(now)
    round_record.payload = {
        **(round_record.payload or {}),
        "eval_result_ids": [row.eval_result_id for row in results],
        "passed_candidate_count": sum(row.status == "passed" for row in results),
        "budget_decision": {
            "should_stop": decision.should_stop or cost_metric_missing,
            "remaining_rounds": decision.remaining_rounds,
            "remaining_seconds": decision.remaining_seconds,
            "stop_reason_codes": stop_codes,
        },
    }

    # A passing candidate is handed to humans, never published.  Reaching the
    # third round is not itself a reason to discard a valid candidate; resource
    # and safety overruns still win over the quality pass.
    hard_stop_codes = sorted(
        (set(stop_codes) & RESOURCE_STOP_REASONS)
        | ({"cost_metric_missing"} if cost_metric_missing else set())
    )
    if passed and not hard_stop_codes:
        round_record.status = "awaiting-review"
        round_record.stop_reason_codes = []
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="awaiting-review",
            reason_codes=[],
            selected_prompt_version_id=selected_prompt,
        )
    elif decision.should_stop or cost_metric_missing:
        round_record.status = "blocked"
        round_record.stop_reason_codes = stop_codes
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="blocked",
            reason_codes=stop_codes,
            selected_prompt_version_id=selected_prompt,
        )
    else:
        round_record.status = "completed"
        round_record.stop_reason_codes = []
        _create_next_generation(
            session,
            ctx,
            schedule,
            round_record,
            prompt_version_id=selected_prompt or round_record.prompt_version_id,
            now=now,
            consecutive_failed_rounds=failures,
        )
    enqueue_event(
        session,
        ctx,
        event_type="label_optimization.round.evaluated",
        aggregate_type="label_optimization_round",
        aggregate_id=round_record.round_id,
        payload=round_data(round_record),
    )
    record_audit(
        session,
        ctx,
        action="label_optimization_round.evaluate",
        object_type="label_optimization_round",
        object_id=round_record.round_id,
        after=round_data(round_record),
    )


def _reconcile_active_session(
    session: Session,
    ctx: Any,
    schedule: LabelOptimizationSchedule,
    *,
    now: datetime,
) -> None:
    if not schedule.active_run_id:
        return
    round_record = session.scalar(
        select(LabelOptimizationRound)
        .where(
            LabelOptimizationRound.tenant_id == schedule.tenant_id,
            LabelOptimizationRound.project_id == schedule.project_id,
            LabelOptimizationRound.optimization_run_id == schedule.active_run_id,
            LabelOptimizationRound.status.in_(("generating-candidates", "evaluating")),
        )
        .order_by(LabelOptimizationRound.round_number.desc())
    )
    if round_record is None:
        return
    session_rounds = session.scalars(
        select(LabelOptimizationRound).where(
            LabelOptimizationRound.tenant_id == schedule.tenant_id,
            LabelOptimizationRound.project_id == schedule.project_id,
            LabelOptimizationRound.optimization_run_id == schedule.active_run_id,
        )
    ).all()
    first_started_at = min(
        (_aware_utc(item.started_at) for item in session_rounds),
        default=_aware_utc(round_record.started_at),
    )
    elapsed = _aware_utc(now) - first_started_at
    budget = iteration_budget_from_data(schedule.budget)
    if elapsed >= budget.max_elapsed:
        reason_code = StopReason.TIME_BUDGET_EXCEEDED.value
        round_record.status = "blocked"
        round_record.stop_reason_codes = [reason_code]
        round_record.completed_at = _aware_utc(now)
        round_record.payload = {
            **(round_record.payload or {}),
            "budget_decision": {
                "should_stop": True,
                "remaining_seconds": 0,
                "stop_reason_codes": [reason_code],
            },
            "next_action": "inspect-time-budget-blocker",
        }
        pending_run_ids = {round_record.generation_run_id, *round_record.eval_run_ids}
        pending_runs = session.scalars(
            select(RunRecord).where(
                RunRecord.tenant_id == schedule.tenant_id,
                RunRecord.project_id == schedule.project_id,
                RunRecord.run_id.in_(pending_run_ids),
                RunRecord.run_type.in_(("label_optimization", "eval_run")),
            )
        ).all()
        for pending_run in pending_runs:
            if pending_run.status in TERMINAL_EVAL_STATUSES:
                continue
            pending_run.status = "blocked"
            pending_run.payload = {
                **(pending_run.payload or {}),
                "status": "blocked",
                "business_status": "blocked",
                "error_code": reason_code,
                "blocked_at": _aware_utc(now).isoformat(),
            }
        schedule.active_run_id = None
        _mark_session_stage(
            session,
            ctx,
            root_run_id=round_record.optimization_run_id,
            stage="blocked",
            reason_codes=[reason_code],
        )
        event_payload = {
            **round_data(round_record),
            "elapsed_seconds": int(elapsed.total_seconds()),
        }
        enqueue_event(
            session,
            ctx,
            event_type="label_optimization.round.hard_stopped",
            aggregate_type="label_optimization_round",
            aggregate_id=round_record.round_id,
            payload=event_payload,
        )
        record_audit(
            session,
            ctx,
            action="label_optimization_round.hard_stop",
            object_type="label_optimization_round",
            object_id=round_record.round_id,
            after=event_payload,
        )
        return
    if round_record.status == "evaluating":
        _evaluate_round(session, ctx, schedule, round_record, now=now)
        return
    generation = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == round_record.generation_run_id,
            RunRecord.tenant_id == schedule.tenant_id,
            RunRecord.project_id == schedule.project_id,
            RunRecord.run_type == "label_optimization",
        )
    )
    if generation is None:
        return
    if generation.status == "success":
        _create_eval_runs(
            session,
            ctx,
            schedule,
            round_record,
            generation,
            now=now,
        )
    elif generation.status in {"failed", "blocked", "cancelled"}:
        _handle_generation_failure(
            session,
            ctx,
            schedule,
            round_record,
            generation,
            now=now,
        )


def process_schedule_once(
    session: Session,
    schedule: LabelOptimizationSchedule,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    utc_now = _aware_utc(now or datetime.now(UTC))
    trace_id = f"trace_label_opt_scheduler_{uuid.uuid4().hex[:20]}"
    ctx = scheduler_context(schedule, trace_id=trace_id)
    _reconcile_active_session(session, ctx, schedule, now=utc_now)
    due_kinds = _due_kinds(schedule, utc_now)
    scan_result: dict[str, Any] | None = None
    if due_kinds:
        snapshot = produce_metric_snapshot(session, ctx, schedule, now=utc_now)
        scan_request = {
            "label_version_id": schedule.label_version_id,
            "prompt_version_id": schedule.prompt_version_id,
            "model_version": schedule.model_version,
            "aggregation_policy_version_id": schedule.aggregation_policy_version_id,
            "eval_dataset_version_id": schedule.eval_dataset_version_id,
            "budget": schedule.budget,
            "metrics_override": snapshot.metrics,
            "_metric_snapshot": metric_snapshot_data(snapshot),
            "_scheduler_due_kinds": sorted(due_kinds),
        }
        scan_result = execute_trigger_scan(
            session,
            ctx,
            request_data=scan_request,
            now=utc_now,
        )
        triggered_kind = scan_result.get("trigger_kind") if scan_result.get("triggered") else None
        _advance_due_clocks(
            schedule,
            due_kinds=due_kinds,
            now=utc_now,
            triggered_kind=str(triggered_kind) if triggered_kind else None,
        )
        if scan_result.get("triggered") and scan_result.get("run_id"):
            root_run_id = str(scan_result["run_id"])
            schedule.active_run_id = root_run_id
            generation = session.scalar(
                select(RunRecord).where(
                    RunRecord.run_id == root_run_id,
                    RunRecord.tenant_id == schedule.tenant_id,
                    RunRecord.project_id == schedule.project_id,
                    RunRecord.run_type == "label_optimization",
                )
            )
            if generation is not None:
                generation.payload = {
                    **(generation.payload or {}),
                    "optimization_session_id": root_run_id,
                    "optimization_round_number": 1,
                    "schedule_id": schedule.schedule_id,
                    "metric_snapshot_id": snapshot.snapshot_id,
                }
            create_round(
                session,
                ctx,
                schedule,
                optimization_run_id=root_run_id,
                generation_run_id=root_run_id,
                round_number=1,
                prompt_version_id=schedule.prompt_version_id,
                now=utc_now,
            )
    schedule.trace_id = ctx.trace_id
    return {
        "schedule_id": schedule.schedule_id,
        "due_kinds": sorted(due_kinds),
        "scan_result": scan_result,
        "active_run_id": schedule.active_run_id,
        "trace_id": ctx.trace_id,
    }


def _candidate_schedule_ids(session: Session, *, now: datetime, limit: int) -> list[str]:
    utc_now = _aware_utc(now)
    return list(
        session.scalars(
            select(LabelOptimizationSchedule.schedule_id)
            .where(
                LabelOptimizationSchedule.status == "active",
                or_(
                    LabelOptimizationSchedule.active_run_id.is_not(None),
                    LabelOptimizationSchedule.next_threshold_scan_at <= utc_now,
                    LabelOptimizationSchedule.next_daily_at <= utc_now,
                    LabelOptimizationSchedule.next_weekly_at <= utc_now,
                ),
            )
            .order_by(LabelOptimizationSchedule.next_threshold_scan_at)
            .limit(limit)
        ).all()
    )


def _claim_schedule(
    session: Session,
    *,
    schedule_id: str,
    now: datetime,
    claim_token: str,
    lease_seconds: int,
) -> LabelOptimizationSchedule | None:
    utc_now = _aware_utc(now)
    stale_before = utc_now - timedelta(seconds=lease_seconds)
    claimed = session.execute(
        update(LabelOptimizationSchedule)
        .where(
            LabelOptimizationSchedule.schedule_id == schedule_id,
            LabelOptimizationSchedule.status == "active",
            or_(
                LabelOptimizationSchedule.scan_claim_token.is_(None),
                LabelOptimizationSchedule.scan_claimed_at < stale_before,
            ),
            or_(
                LabelOptimizationSchedule.active_run_id.is_not(None),
                LabelOptimizationSchedule.next_threshold_scan_at <= utc_now,
                LabelOptimizationSchedule.next_daily_at <= utc_now,
                LabelOptimizationSchedule.next_weekly_at <= utc_now,
            ),
        )
        .values(scan_claim_token=claim_token, scan_claimed_at=utc_now)
    )
    if getattr(claimed, "rowcount", 0) != 1:
        return None
    schedule = session.scalar(
        select(LabelOptimizationSchedule).where(
            LabelOptimizationSchedule.schedule_id == schedule_id,
            LabelOptimizationSchedule.scan_claim_token == claim_token,
        )
    )
    return schedule


def run_once(
    *,
    now: datetime | None = None,
    session_factory: sessionmaker[Session] = SessionLocal,
    limit: int | None = None,
    worker_id: str | None = None,
) -> dict[str, Any]:
    """Run one durable scheduler/reconciler tick.

    Dagster schedules and a standalone process call the exact same function.
    The conditional schedule UPDATE is the database scope mutex; clocks, rounds,
    RunRecords and the claim release commit atomically.
    """

    settings = get_settings()
    utc_now = _aware_utc(now or datetime.now(UTC))
    batch_limit = max(1, min(limit or settings.label_optimization_scheduler_batch_size, 100))
    resolved_worker = (worker_id or settings.label_optimization_scheduler_worker_id)[:64]
    with session_factory() as discovery:
        schedule_ids = _candidate_schedule_ids(discovery, now=utc_now, limit=batch_limit)
    processed: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for schedule_id in schedule_ids:
        claim_token = uuid.uuid4().hex
        try:
            with session_factory() as session:
                schedule = _claim_schedule(
                    session,
                    schedule_id=schedule_id,
                    now=utc_now,
                    claim_token=claim_token,
                    lease_seconds=settings.label_optimization_scheduler_claim_lease_seconds,
                )
                if schedule is None:
                    session.rollback()
                    continue
                result = process_schedule_once(session, schedule, now=utc_now)
                schedule.scan_claim_token = None
                schedule.scan_claimed_at = None
                session.commit()
                processed.append(result)
        except Exception as exc:  # noqa: BLE001 - one scope must not stop other scopes.
            failures.append({"schedule_id": schedule_id, "error_code": type(exc).__name__})
            log_event(
                logger,
                "label_optimization_scheduler.scope_failed",
                worker_id=resolved_worker,
                schedule_id=schedule_id,
                error_code=type(exc).__name__,
            )
    return {
        "worker_id": resolved_worker,
        "observed_at": utc_now.isoformat(),
        "candidate_count": len(schedule_ids),
        "processed_count": len(processed),
        "failure_count": len(failures),
        "processed": processed,
        "failures": failures,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Auris Flow 标签自动优化调度 worker")
    parser.add_argument("--once", action="store_true", help="执行一次 due 扫描和 round 对账")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("LABEL_OPTIMIZATION_SCHEDULER_WORKER_ID"),
    )
    args = parser.parse_args()
    settings = get_settings()
    if args.once:
        print(json.dumps(run_once(limit=args.limit, worker_id=args.worker_id), ensure_ascii=False))
        return 0
    if not settings.label_optimization_scheduler_enabled:
        print("LABEL_OPTIMIZATION_SCHEDULER_ENABLED=false; worker 未启动")
        return 2
    stopped = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    while not stopped:
        result = run_once(limit=args.limit, worker_id=args.worker_id)
        log_event(
            logger,
            "label_optimization_scheduler.tick",
            worker_id=result["worker_id"],
            processed_count=result["processed_count"],
            failure_count=result["failure_count"],
        )
        time.sleep(max(5, min(settings.label_optimization_scheduler_poll_seconds, 60)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["process_schedule_once", "run_once"]
