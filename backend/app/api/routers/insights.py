from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from sqlalchemy import select

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import InsightAction, InsightEffect, InsightExperiment
from app.schemas import (
    InsightActionRequest,
    InsightExperimentRequest,
    InsightMetricRunRequest,
    InsightReportRequest,
)
from app.schemas.common import parse_payload
from app.schemas.insights import InsightExperimentRetryAttemptRequest
from app.services.insight_closure_service import (
    action_payload,
    create_insight_action,
    create_insight_experiment,
    create_insight_experiment_retry_attempt,
    create_insight_metric_run,
    create_insight_report,
    current_metric_payloads,
    effect_payload,
    experiment_payload,
    get_insight_action,
    get_insight_report,
    report_detail_payload,
)
from app.services.resource_service import list_resource_data

router = APIRouter(tags=["insights"])


@router.post("/insights/metric-runs", status_code=202)
async def post_insight_metric_run(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "asset_manager", "model_engineer"),
        "insights.metric-runs.create",
    )
    body = parse_payload(InsightMetricRunRequest, await request.json())
    return await create_insight_metric_run(session, ctx, request, body)


@router.get("/insights/metrics")
def get_insights_metrics(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    time_range: str = "30d",
    store_id: str | None = None,
    label_version: str | None = None,
    model_version: str | None = None,
) -> dict[str, Any]:
    all_items = current_metric_payloads(
        session,
        ctx,
        time_range=time_range,
        store_id=store_id,
        label_version=label_version,
        model_version=model_version,
    )
    raw_cursor = str(page.get("cursor") or "0")
    offset = int(raw_cursor) if raw_cursor.isdigit() else 0
    limit = int(page["limit"] or 50)
    items = all_items[offset : offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(all_items) else None
    return collection_envelope(
        items,
        ctx,
        total=len(all_items),
        limit=limit,
        next_cursor=next_cursor,
    )


@router.get("/insights/funnels")
def get_insights_funnels(
    session: SessionDep,
    ctx: ContextDep,
    dimension: str | None = None,
) -> dict[str, Any]:
    projections = list_resource_data(session, ctx, "insight_funnels", limit=1)
    data = (
        {
            **projections[0],
            "dimension": dimension or projections[0].get("dimension") or "scene",
            "data_source": "mysql_projection",
            "trace_id": ctx.trace_id,
        }
        if projections
        else {
            "dimension": dimension or "scene",
            "nodes": [],
            "links": [],
            "status": "empty",
            "empty_reason": "当前 SceneProfile 尚未物化漏斗投影",
            "data_source": "mysql_projection",
            "trace_id": ctx.trace_id,
        }
    )
    return envelope(data, ctx)


@router.get("/insights/reports")
def get_insights_reports(
    session: SessionDep, ctx: ContextDep, page: PaginationDep
) -> dict[str, Any]:
    projections = list_resource_data(
        session,
        ctx,
        "insight_reports",
        limit=int(page["limit"] or 50),
        cursor=page.get("cursor"),
    )
    return collection_envelope(projections, ctx)


@router.get("/insights/reports/{report_id}")
def get_insights_report_by_id(
    report_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    report = get_insight_report(session, ctx, report_id)
    return envelope(report_detail_payload(session, ctx, report), ctx)


@router.post("/insights/reports", status_code=202)
async def post_insights_reports(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin", "asset_manager"), "insights.reports.create")
    body = parse_payload(InsightReportRequest, await request.json())
    return await create_insight_report(session, ctx, request, body)


@router.get("/insights/actions")
def get_insights_actions(
    session: SessionDep, ctx: ContextDep, page: PaginationDep
) -> dict[str, Any]:
    limit = int(page["limit"] or 50)
    items = session.scalars(
        select(InsightAction)
        .where(
            InsightAction.tenant_id == ctx.tenant_id,
            InsightAction.project_id == ctx.project_id,
        )
        .order_by(InsightAction.created_at.desc())
        .limit(limit)
    ).all()
    return collection_envelope([action_payload(item) for item in items], ctx)


@router.get("/insights/actions/{action_id}")
def get_insights_action_by_id(
    action_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    action = get_insight_action(session, ctx, action_id)
    experiments = session.scalars(
        select(InsightExperiment).where(
            InsightExperiment.tenant_id == ctx.tenant_id,
            InsightExperiment.project_id == ctx.project_id,
            InsightExperiment.action_id == action_id,
        )
    ).all()
    effects = session.scalars(
        select(InsightEffect).where(
            InsightEffect.tenant_id == ctx.tenant_id,
            InsightEffect.project_id == ctx.project_id,
            InsightEffect.action_id == action_id,
        )
    ).all()
    return envelope(
        {
            **action_payload(action),
            "experiments": [experiment_payload(item) for item in experiments],
            "effects": [effect_payload(item) for item in effects],
        },
        ctx,
    )


@router.post("/insights/actions", status_code=201)
async def post_insights_actions(
    request: Request, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "asset_manager", "review_arbitrator"),
        "insights.actions.create",
    )
    body = parse_payload(InsightActionRequest, await request.json())
    return await create_insight_action(session, ctx, request, body)


@router.post("/insights/actions/{action_id}/experiments", status_code=202)
async def post_insight_action_experiment(
    action_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(ctx, ("project_admin", "model_engineer"), "insights.experiments.create")
    body = parse_payload(InsightExperimentRequest, await request.json())
    return await create_insight_experiment(session, ctx, request, action_id, body)


@router.get("/insights/experiments/{experiment_id}")
def get_insight_experiment(
    experiment_id: str, session: SessionDep, ctx: ContextDep
) -> dict[str, Any]:
    experiment = session.scalar(
        select(InsightExperiment).where(
            InsightExperiment.experiment_id == experiment_id,
            InsightExperiment.tenant_id == ctx.tenant_id,
            InsightExperiment.project_id == ctx.project_id,
        )
    )
    if experiment is None:
        from app.core.errors import ApiError

        raise ApiError("NOT_FOUND", f"洞察实验不存在：{experiment_id}", 404)
    return envelope(experiment_payload(experiment), ctx)


@router.post(
    "/insights/experiments/{experiment_id}/retry-attempts",
    status_code=202,
)
async def post_insight_experiment_retry_attempt(
    experiment_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, Any]:
    require_any_role(
        ctx,
        ("project_admin", "model_engineer"),
        "insights.experiments.retry-attempts.create",
    )
    body = parse_payload(InsightExperimentRetryAttemptRequest, await request.json())
    return await create_insight_experiment_retry_attempt(
        session,
        ctx,
        request,
        experiment_id,
        body,
    )


@router.get("/insights/effects")
def get_insight_effects(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    action_id: str | None = None,
) -> dict[str, Any]:
    limit = int(page["limit"] or 50)
    query = select(InsightEffect).where(
        InsightEffect.tenant_id == ctx.tenant_id,
        InsightEffect.project_id == ctx.project_id,
    )
    if action_id:
        query = query.where(InsightEffect.action_id == action_id)
    effects = session.scalars(query.order_by(InsightEffect.created_at.desc()).limit(limit)).all()
    return collection_envelope([effect_payload(item) for item in effects], ctx)
