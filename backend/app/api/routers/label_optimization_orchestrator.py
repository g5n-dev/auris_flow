from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.domain.label_optimization import IterationBudget
from app.services.execution_contract_registry import (
    preflight_production_execution_contract,
)
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.label_optimization_orchestrator import (
    execute_trigger_scan,
    get_trigger_scan_or_run,
)
from app.services.label_optimization_runtime_service import (
    create_or_update_schedule,
    get_schedule,
    list_metric_snapshots,
    list_rounds,
    list_schedules,
    schedule_data,
)

router = APIRouter(tags=["label-optimization"])
WRITE_ROLES = ("project_admin", "model_engineer")
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$"


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OptimizationMetricsOverride(StrictBody):
    reviewed_sample_count: int = Field(ge=0)
    human_override_rate_ppm: int = Field(ge=0, le=1_000_000)
    baseline_human_override_rate_ppm: int = Field(ge=0, le=1_000_000)
    conflict_rate_ppm: int = Field(ge=0, le=1_000_000)
    json_validity_ppm: int = Field(ge=0, le=1_000_000)
    critical_recall_ppm: int = Field(ge=0, le=1_000_000)
    baseline_critical_recall_ppm: int = Field(ge=0, le=1_000_000)
    largest_failure_cluster_count: int = Field(ge=0)
    new_feedback_count: int = Field(ge=0)


class OptimizationBudgetBody(StrictBody):
    max_rounds: int = Field(default=3, ge=1, le=3)
    min_candidates_per_round: int = Field(default=2, ge=2, le=5)
    max_candidates_per_round: int = Field(default=5, ge=2, le=5)
    candidates_per_round: int = Field(default=5, ge=2, le=5)
    max_elapsed_seconds: int = Field(default=7200, ge=1, le=7200)
    max_cost_micros: int | None = Field(default=None, gt=0)
    min_meaningful_gain_ppm: int = Field(default=20_000, ge=0, le=1_000_000)
    max_consecutive_failed_rounds: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def validate_hard_limits(self) -> OptimizationBudgetBody:
        # Reuse the domain invariant instead of duplicating subtle candidate,
        # elapsed-time and stop-budget semantics in the transport layer.
        IterationBudget(
            max_rounds=self.max_rounds,
            min_candidates_per_round=self.min_candidates_per_round,
            max_candidates_per_round=self.max_candidates_per_round,
            max_elapsed=timedelta(seconds=self.max_elapsed_seconds),
            max_cost_micros=self.max_cost_micros,
            min_meaningful_gain_ppm=self.min_meaningful_gain_ppm,
            max_consecutive_failed_rounds=self.max_consecutive_failed_rounds,
        )
        if (
            not self.min_candidates_per_round
            <= self.candidates_per_round
            <= self.max_candidates_per_round
        ):
            raise ValueError("candidates_per_round must stay inside the locked candidate range")
        return self


class LabelOptimizationTriggerScanBody(StrictBody):
    label_version_id: str = Field(pattern=ID_PATTERN)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    aggregation_policy_version_id: str = Field(pattern=ID_PATTERN)
    eval_dataset_version_id: str = Field(pattern=ID_PATTERN)
    budget: OptimizationBudgetBody = Field(default_factory=OptimizationBudgetBody)
    metrics_override: OptimizationMetricsOverride | None = None


class LabelOptimizationScheduleBody(StrictBody):
    schedule_id: str | None = Field(default=None, pattern=ID_PATTERN)
    label_version_id: str = Field(pattern=ID_PATTERN)
    prompt_version_id: str = Field(pattern=ID_PATTERN)
    model_version: str = Field(min_length=1, max_length=128)
    aggregation_policy_version_id: str = Field(pattern=ID_PATTERN)
    eval_dataset_version_id: str = Field(pattern=ID_PATTERN)
    status: Literal["active", "paused"] = "active"
    schedule_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    daily_hour: int = Field(default=2, ge=0, le=23)
    weekly_day: int = Field(default=6, ge=0, le=6)
    start_immediately: bool = True
    expected_resource_version: int | None = Field(default=None, ge=1)
    budget: OptimizationBudgetBody = Field(default_factory=OptimizationBudgetBody)


@router.post("/label-optimization-schedules", status_code=201)
async def post_label_optimization_schedule(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    body: Annotated[LabelOptimizationScheduleBody, Body()],
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.schedule.write")
    body_hash = await request_hash(request)
    operation = "label_optimization.schedule.write"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    result = create_or_update_schedule(
        session,
        ctx,
        request_data=body.model_dump(mode="json", exclude_none=True),
    )
    response = envelope(result, ctx)
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


def _paged(items: list[dict], page: PaginationDep, ctx: ContextDep):
    cursor = int(page.get("cursor") or 0)
    limit = int(page.get("limit") or 50)
    selected = items[cursor : cursor + limit]
    return collection_envelope(
        selected,
        ctx,
        total=len(items),
        limit=limit,
        next_cursor=str(cursor + limit) if cursor + limit < len(items) else None,
    )


@router.get("/label-optimization-schedules")
def get_label_optimization_schedules(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.schedule.read")
    items = list_schedules(session, ctx)
    return _paged(items, page, ctx)


@router.get("/label-optimization-schedules/{schedule_id}")
def get_label_optimization_schedule(
    schedule_id: str,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.schedule.read")
    return envelope(
        schedule_data(get_schedule(session, ctx, schedule_id=schedule_id)),
        ctx,
    )


@router.get("/label-optimization-schedules/{schedule_id}/metric-snapshots")
def get_label_optimization_schedule_metric_snapshots(
    schedule_id: str,
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.schedule.read")
    items = list_metric_snapshots(session, ctx, schedule_id=schedule_id)
    return _paged(items, page, ctx)


@router.get("/label-optimization-schedules/{schedule_id}/rounds")
def get_label_optimization_schedule_rounds(
    schedule_id: str,
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.schedule.read")
    items = list_rounds(session, ctx, schedule_id=schedule_id)
    return _paged(items, page, ctx)


@router.post("/label-optimization-trigger-scans", status_code=201)
async def post_label_optimization_trigger_scan(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    body: Annotated[LabelOptimizationTriggerScanBody, Body()],
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.trigger_scan")
    preflight_production_execution_contract(
        event_type="agent_run.requested",
        run_type="label_optimization",
    )
    body_hash = await request_hash(request)
    operation = "label_optimization.trigger_scan"
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay

    result = execute_trigger_scan(
        session,
        ctx,
        request_data=body.model_dump(mode="json", exclude_none=True),
    )
    response = envelope(result, ctx)
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


@router.get("/label-optimization-trigger-scans/{run_or_scan_id}")
def get_label_optimization_trigger_scan(
    run_or_scan_id: str,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(ctx, WRITE_ROLES, "label_optimization.trigger_scan.read")
    return envelope(
        get_trigger_scan_or_run(
            session,
            ctx,
            run_or_scan_id=run_or_scan_id,
        ),
        ctx,
    )


__all__ = ["router"]
