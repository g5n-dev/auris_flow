from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.schemas import (
    RunCompletionReceiptRequest,
    TaskRunCancellationRequest,
    TaskRunRequest,
    TaskRunRetryRequest,
    TaskRunStatusSyncRequest,
    TaskVersionRequest,
    parse_payload,
)
from app.services.experiment_service import bind_task_run_to_experiment
from app.services.release_gate_service import prepare_task_version_publish
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_page,
    patch_idempotent_json_resource,
)
from app.services.run_service import (
    complete_run_from_receipt,
    create_run,
    get_run,
    list_run_page,
    retry_run,
)
from app.services.task_execution_policy import (
    enforce_task_execution_policy,
    prepare_task_version_write,
)
from app.services.task_run_control_service import create_task_run_control

router = APIRouter(tags=["task-runs"])


@router.get("/task-types")
def get_task_types(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "task_types", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.get("/task-versions")
def get_task_versions(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "task_versions", page, status=status)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/task-versions", status_code=201)
async def post_task_versions(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "task_versions",
        key_prefix="task_version",
        status="draft",
        body_model=TaskVersionRequest,
        reject_existing=True,
        prepare_payload=lambda payload: prepare_task_version_write(session, ctx, payload),
    )


@router.get("/task-versions/{id}")
def get_task_versions_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "task_versions", id).data, ctx)


@router.patch("/task-versions/{id}")
async def patch_task_versions_by_id(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    return await patch_idempotent_json_resource(
        session,
        ctx,
        request,
        "task_versions",
        id,
        body_model=TaskVersionRequest,
        prepare_payload=lambda resource, payload: prepare_task_version_write(
            session,
            ctx,
            payload,
            current=resource.data,
        ),
        preserve_root_trace_id=True,
    )


@router.post("/task-versions/{id}/publish", status_code=202)
async def post_task_versions_by_id_publish(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    require_any_role(ctx, ("project_admin",), "task_versions.publish")
    body = await request.json()
    return await create_run(
        session,
        ctx,
        request,
        run_type="task_version_publish",
        event_type="task_version.publish_requested",
        payload={**body, "task_version_id": id},
        status="blocked",
        prepare_payload=lambda payload: prepare_task_version_publish(session, ctx, id, payload),
    )


@router.post("/task-runs", status_code=202)
async def post_task_runs(request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(TaskRunRequest, await request.json()).model_dump(exclude_none=True)
    return await create_run(
        session,
        ctx,
        request,
        run_type="task_run",
        event_type="task_run.requested",
        payload=body,
        status="pending",
        prepare_payload=lambda payload: enforce_task_execution_policy(
            session,
            ctx,
            bind_task_run_to_experiment(session, ctx, payload),
        ),
    )


@router.get("/task-runs")
def get_task_runs(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    run_page = list_run_page(session, ctx, page, run_type="task_run", status=status)
    return collection_envelope(
        run_page.items,
        ctx,
        total=run_page.total,
        limit=run_page.limit,
        next_cursor=run_page.next_cursor,
    )


@router.post("/task-runs/{id}/retries", status_code=202)
async def post_task_runs_by_id_retries(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(TaskRunRetryRequest, await request.json()).model_dump(exclude_none=True)
    return await retry_run(session, ctx, request, id, body)


@router.post("/task-runs/{id}/cancellations", status_code=202)
async def post_task_runs_by_id_cancellations(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(TaskRunCancellationRequest, await request.json()).model_dump()
    return await create_task_run_control(
        session,
        ctx,
        request,
        id,
        body,
        action="cancel",
    )


@router.post("/task-runs/{id}/status-syncs", status_code=202)
async def post_task_runs_by_id_status_syncs(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(TaskRunStatusSyncRequest, await request.json()).model_dump()
    return await create_task_run_control(
        session,
        ctx,
        request,
        id,
        body,
        action="status_sync",
    )


@router.post("/task-runs/{id}/completion-receipts")
async def post_task_runs_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(session, ctx, request, id, body)


@router.get("/task-runs/{id}")
def get_task_runs_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_run(session, ctx, id), ctx)
