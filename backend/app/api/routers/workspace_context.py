from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.deps import ContextDep, SessionDep
from app.core.response import envelope
from app.services.workspace_context_service import get_workspace_context_options

router = APIRouter(tags=["workspace-context"])


@router.get("/workspace-context-options")
def workspace_context_options(
    request: Request,
    response: Response,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, object]:
    data = get_workspace_context_options(session, ctx)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return envelope(
        data,
        ctx,
        meta={
            "trace_id": getattr(request.state, "trace_id", ctx.trace_id),
            "request_id": getattr(request.state, "request_id", ctx.request_id),
        },
    )
