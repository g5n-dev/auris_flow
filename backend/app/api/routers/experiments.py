from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.schemas import (
    ExperimentAssignmentRequest,
    ExperimentCreateRequest,
    ExperimentDecisionRequest,
    ExperimentExposureRequest,
    ExperimentMetricSnapshotRequest,
    ExperimentOutcomeRequest,
    ExperimentStartRequest,
    parse_payload,
)
from app.services.experiment_service import (
    assign_experiment_subject,
    compute_experiment_metric_snapshot,
    create_experiment,
    get_experiment_detail,
    list_experiments,
    record_experiment_decision,
    record_experiment_exposure,
    record_experiment_outcome,
    start_experiment,
)

router = APIRouter(tags=["experiments"])


@router.get("/experiments")
def get_experiments(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
):
    return list_experiments(session, ctx, page, status=status)


@router.post("/experiments", status_code=201)
async def post_experiments(request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(ExperimentCreateRequest, await request.json())
    return await create_experiment(session, ctx, request, body)


@router.get("/experiments/{experiment_id}")
def get_experiments_by_id(
    experiment_id: str,
    session: SessionDep,
    ctx: ContextDep,
):
    return get_experiment_detail(session, ctx, experiment_id)


@router.post("/experiments/{experiment_id}/start")
async def post_experiment_start(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ExperimentStartRequest, await request.json())
    return await start_experiment(session, ctx, request, experiment_id, body)


@router.post("/experiments/{experiment_id}/assignments", status_code=201)
async def post_experiment_assignments(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ExperimentAssignmentRequest, await request.json())
    return await assign_experiment_subject(session, ctx, request, experiment_id, body)


@router.post("/experiments/{experiment_id}/exposures", status_code=201)
async def post_experiment_exposures(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ExperimentExposureRequest, await request.json())
    return await record_experiment_exposure(session, ctx, request, experiment_id, body)


@router.post("/experiments/{experiment_id}/outcomes", status_code=201)
async def post_experiment_outcomes(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ExperimentOutcomeRequest, await request.json())
    return await record_experiment_outcome(session, ctx, request, experiment_id, body)


@router.post("/experiments/{experiment_id}/metric-snapshots", status_code=201)
async def post_experiment_metric_snapshots(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    parse_payload(ExperimentMetricSnapshotRequest, await request.json())
    return await compute_experiment_metric_snapshot(session, ctx, request, experiment_id)


@router.post("/experiments/{experiment_id}/decisions", status_code=201)
async def post_experiment_decisions(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    body = parse_payload(ExperimentDecisionRequest, await request.json())
    return await record_experiment_decision(session, ctx, request, experiment_id, body)
