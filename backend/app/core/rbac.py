from __future__ import annotations

from collections.abc import Iterable

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.logging import get_logger, log_event

logger = get_logger("rbac")


def require_any_role(ctx: RequestContext, allowed_roles: Iterable[str], action: str) -> None:
    allowed = tuple(allowed_roles)
    if "system" in ctx.roles:
        return
    if set(ctx.roles).intersection(allowed):
        return
    log_event(
        logger,
        "rbac.denied",
        level=30,
        ctx=ctx,
        action=action,
        roles=list(ctx.roles),
        required_roles=list(allowed),
    )
    raise ApiError(
        "FORBIDDEN",
        f"当前角色无权执行：{action}",
        403,
        details=[
            {
                "action": action,
                "roles": list(ctx.roles),
                "required_roles": list(allowed),
            }
        ],
    )


def require_project_scope(
    ctx: RequestContext, *, tenant_id: str, project_id: str, action: str
) -> None:
    if ctx.tenant_id == tenant_id and ctx.project_id == project_id:
        return
    log_event(
        logger,
        "rbac.scope_denied",
        level=30,
        ctx=ctx,
        action=action,
        target_tenant_id=tenant_id,
        target_project_id=project_id,
    )
    raise ApiError(
        "FORBIDDEN",
        f"当前上下文无权访问目标资源：{action}",
        403,
        details=[
            {
                "action": action,
                "tenant_id": tenant_id,
                "project_id": project_id,
            }
        ],
    )
