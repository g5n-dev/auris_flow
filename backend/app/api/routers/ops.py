from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps import ContextDep, SessionDep
from app.core.project_membership import user_has_project_membership
from app.core.response import envelope
from app.models import JsonResource, Project, RunRecord
from app.repositories.json_resources import JsonResourceRepository
from app.services.read_policy_service import require_resource_read, resource_read_scope
from app.services.resource_service import list_resource_data

router = APIRouter(tags=["ops"])

ACTIONABLE_REVIEW_STATUSES = frozenset({"draft", "pending", "claimed", "in_review", "reviewing"})
ANOMALY_ASSET_STATUSES = frozenset({"warning", "risk", "failed", "error", "blocked"})
SUCCESS_AUDIO_STATUSES = frozenset({"success", "completed", "processed", "passed"})
FAILED_RUN_STATUSES = frozenset({"failed", "error", "dead_letter", "blocked"})


def _resource_status_counts(
    session: SessionDep,
    ctx: ContextDep,
    collection: str,
    *,
    predicates: tuple[ColumnElement[bool], ...] = (),
) -> dict[str, int]:
    require_resource_read(ctx, collection)
    return JsonResourceRepository(session).status_counts(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection=collection,
        read_scope=resource_read_scope(ctx, collection),
        predicates=predicates,
    )


def _sum_statuses(counts: dict[str, int], statuses: frozenset[str]) -> int:
    return sum(count for status, count in counts.items() if status in statuses)


def _visible_project_count(session: SessionDep, ctx: ContextDep) -> int:
    projects = session.scalars(select(Project).where(Project.tenant_id == ctx.tenant_id))
    return sum(
        1
        for project in projects
        if project.status == "active"
        and ("system" in ctx.roles or user_has_project_membership(project, ctx.user_id))
    )


def _recent_audio_sessions(
    session: SessionDep,
    ctx: ContextDep,
    *,
    predicates: tuple[ColumnElement[bool], ...],
) -> list[dict[str, object]]:
    require_resource_read(ctx, "audio_sessions")
    rows = JsonResourceRepository(session).list(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        collection="audio_sessions",
        limit=5,
        read_scope=resource_read_scope(ctx, "audio_sessions"),
        predicates=predicates,
    )
    return [row.data for row in rows]


@router.get("/insights/ops-summary")
def get_insights_ops_summary(
    session: SessionDep,
    ctx: ContextDep,
    time_range: str | None = None,
):
    audio_predicates: tuple[ColumnElement[bool], ...] = ()
    if ctx.business_date:
        audio_predicates = (
            JsonResource.data["started_at"].as_string().like(f"{ctx.business_date}%"),
        )

    asset_counts = _resource_status_counts(session, ctx, "data_assets")
    review_counts = _resource_status_counts(session, ctx, "human_review_tasks")
    audio_counts = _resource_status_counts(
        session,
        ctx,
        "audio_sessions",
        predicates=audio_predicates,
    )
    project_count = _visible_project_count(session, ctx)
    audio_count = sum(audio_counts.values())
    processed_audio_count = _sum_statuses(audio_counts, SUCCESS_AUDIO_STATUSES)
    auto_pass_rate = round(processed_audio_count * 100 / audio_count, 1) if audio_count else 0.0
    pending_count = _sum_statuses(review_counts, ACTIONABLE_REVIEW_STATUSES)
    anomaly_count = _sum_statuses(asset_counts, ANOMALY_ASSET_STATUSES)
    failed_run_count = int(
        session.scalar(
            select(func.count())
            .select_from(RunRecord)
            .where(
                RunRecord.tenant_id == ctx.tenant_id,
                RunRecord.project_id == ctx.project_id,
                RunRecord.status.in_(FAILED_RUN_STATUSES),
            )
        )
        or 0
    )

    recent_assets = list_resource_data(session, ctx, "data_assets", limit=5)
    reports = list_resource_data(session, ctx, "insight_reports", limit=5)
    sessions = _recent_audio_sessions(
        session,
        ctx,
        predicates=audio_predicates,
    )
    metrics = [
        {
            "metric_key": "projects",
            "label": "项目数",
            "value": project_count,
            "delta": "当前用户可见且运行中",
        },
        {
            "metric_key": "today_audio",
            "label": "今日音频",
            "value": audio_count,
            "delta": ctx.business_date or time_range or "当前项目",
        },
        {
            "metric_key": "auto_pass_rate",
            "label": "自动通过率",
            "value": auto_pass_rate,
            "delta": f"{processed_audio_count}/{audio_count} 已通过",
        },
        {
            "metric_key": "human_review",
            "label": "待人工复核",
            "value": pending_count,
            "delta": "当前用户可见队列",
        },
        {
            "metric_key": "asset_risk",
            "label": "异常资产",
            "value": anomaly_count,
            "delta": "warning / risk / failed",
        },
        {
            "metric_key": "model_anomaly",
            "label": "模型异常",
            "value": failed_run_count,
            "delta": "失败、阻断或死信运行",
        },
    ]
    data = {
        "time_range": time_range or "today",
        "context": {
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "store_key": ctx.store_key,
            "business_date": ctx.business_date,
            "model_version": ctx.model_version,
            "label_version": ctx.label_version,
        },
        # Compatibility scalar fields keep existing prototype clients functional while
        # the typed metrics array remains the canonical projection contract.
        "project_count": project_count,
        "audio_count": audio_count,
        "auto_pass_rate": auto_pass_rate,
        "pending_count": pending_count,
        "anomaly_count": anomaly_count,
        "model_anomaly_count": failed_run_count,
        "metrics": metrics,
        "pipeline": [
            {"stage": "接入", "value": audio_count, "status": "success"},
            {"stage": "处理", "value": processed_audio_count, "status": "success"},
            {"stage": "人工网关", "value": pending_count, "status": "blocked"},
            {
                "stage": "资产生成",
                "value": f"{asset_counts.get('success', 0)}/{sum(asset_counts.values())}",
                "status": "risk" if anomaly_count else "success",
            },
        ],
        "recent_assets": recent_assets,
        "review_counts": review_counts,
        "reports": reports,
        "sessions": sessions,
    }
    return envelope(data, ctx)
