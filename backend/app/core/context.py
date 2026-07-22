from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from urllib.parse import unquote

from fastapi import Depends, Header, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.auth import AuthActor, authenticate_bearer
from app.core.browser_session import (
    authenticate_browser_session,
    browser_session_default_scope,
    oidc_bearer_default_scope,
    resolve_oidc_bearer_actor,
)
from app.core.completion_signature import verify_completion_signature
from app.core.config import _csv_items, get_settings, is_production_environment
from app.core.database import get_session
from app.core.errors import ApiError
from app.core.project_membership import (
    project_member_role_binding,
    user_has_project_membership,
)
from app.models import AuthSession, IdempotencyRecord, Project, Tenant, TraceRef, User

ContextSessionDep = Annotated[Session, Depends(get_session)]


@dataclass(frozen=True)
class RequestContext:
    tenant_id: str
    project_id: str
    user_id: str
    roles: tuple[str, ...]
    request_id: str
    trace_id: str
    idempotency_key: str | None = None
    parent_trace_id: str | None = None
    correlation_id: str | None = None
    auth_session_id: str | None = None
    store_key: str | None = None
    business_date: str | None = None
    model_version: str | None = None
    label_version: str | None = None
    actor_kind: str = "human"


@dataclass(frozen=True)
class ServerTraceContext:
    root_trace_id: str
    parent_trace_id: str | None
    correlation_id: str


TRACE_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_CONTEXT_DIMENSION_LENGTH = 256


def resolve_actor(authorization: str | None) -> AuthActor:
    return authenticate_bearer(authorization)


def actor_allows_scope(actor: AuthActor, tenant_id: str, project_id: str) -> bool:
    tenant_allowed = (
        not actor.tenant_ids or "*" in actor.tenant_ids or tenant_id in actor.tenant_ids
    )
    project_allowed = (
        not actor.project_ids or "*" in actor.project_ids or project_id in actor.project_ids
    )
    return tenant_allowed and project_allowed


def _validated_trace_header(value: str | None, *, header: str) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip()
    if not TRACE_CONTEXT_PATTERN.fullmatch(normalized):
        raise ApiError(
            "INVALID_TRACE_CONTEXT",
            f"{header} 格式无效",
            400,
            details=[{"header": header, "max_length": 128}],
        )
    return normalized


def _validated_context_dimension(value: str | None, *, header: str) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = unquote(value.strip())
    if len(normalized) > MAX_CONTEXT_DIMENSION_LENGTH or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ApiError(
            "CONTEXT_INVALID_DIMENSION",
            f"{header} 格式无效",
            400,
            details=[{"header": header, "max_length": MAX_CONTEXT_DIMENSION_LENGTH}],
        )
    return normalized


def _validated_business_date(value: str | None) -> str | None:
    normalized = _validated_context_dimension(value, header="X-Business-Date")
    if normalized is None:
        return None
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError:
        raise ApiError(
            "CONTEXT_INVALID_BUSINESS_DATE",
            "X-Business-Date 必须是有效的 YYYY-MM-DD 日期",
            400,
        ) from None
    if parsed.isoformat() != normalized:
        raise ApiError(
            "CONTEXT_INVALID_BUSINESS_DATE",
            "X-Business-Date 必须是有效的 YYYY-MM-DD 日期",
            400,
        )
    return normalized


def initialize_server_trace(request: Request) -> ServerTraceContext:
    root_trace_id = (
        getattr(request.state, "trace_id", None)
        if getattr(request.state, "server_trace_initialized", False)
        else None
    ) or f"trace_{uuid.uuid4().hex}"
    request.state.trace_id = root_trace_id
    request.state.server_trace_initialized = True
    parent_trace_id = _validated_trace_header(
        request.headers.get("X-Trace-Id"),
        header="X-Trace-Id",
    )
    correlation_id = (
        _validated_trace_header(
            request.headers.get("X-Correlation-Id"),
            header="X-Correlation-Id",
        )
        or parent_trace_id
        or root_trace_id
    )
    request.state.parent_trace_id = parent_trace_id
    request.state.correlation_id = correlation_id
    return ServerTraceContext(
        root_trace_id=root_trace_id,
        parent_trace_id=parent_trace_id,
        correlation_id=correlation_id,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def require_active_dev_session(session: Session, actor: AuthActor) -> None:
    if actor.provider != "dev_session":
        return
    if actor.session_id is None or actor.issued_at is None or actor.expires_at is None:
        raise ApiError("AUTH_SESSION_INVALID", "开发会话缺少服务端绑定", 401)

    record = session.execute(
        select(AuthSession).where(AuthSession.session_id == actor.session_id)
    ).scalar_one_or_none()
    if record is None:
        raise ApiError("AUTH_SESSION_NOT_FOUND", "开发会话不存在", 401)

    issued_at = _as_utc(record.issued_at)
    expires_at = _as_utc(record.expires_at)
    subject_matches = (
        record.user_id == actor.user_id
        and record.provider == actor.provider
        and actor.tenant_ids == (record.tenant_id,)
        and int(issued_at.timestamp()) == actor.issued_at
        and int(expires_at.timestamp()) == actor.expires_at
    )
    if not subject_matches:
        raise ApiError("AUTH_SESSION_SUBJECT_MISMATCH", "开发会话主体不匹配", 401)
    if record.revoked_at is not None:
        raise ApiError("AUTH_SESSION_REVOKED", "开发会话已撤销", 401)

    now = datetime.now(UTC)
    if now >= expires_at:
        raise ApiError("AUTH_SESSION_EXPIRED", "开发会话已过期", 401)

    interval = get_settings().auth_session_last_seen_interval_seconds
    cutoff = now - timedelta(seconds=interval)
    if _as_utc(record.last_seen_at) > cutoff:
        return

    result = session.execute(
        update(AuthSession)
        .where(
            AuthSession.session_id == actor.session_id,
            AuthSession.user_id == actor.user_id,
            AuthSession.tenant_id == record.tenant_id,
            AuthSession.provider == actor.provider,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            AuthSession.last_seen_at <= cutoff,
        )
        .values(last_seen_at=now)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0):
        session.commit()


def require_context_membership(
    session: Session,
    ctx: RequestContext,
    *,
    enforce_project_roles: bool = True,
) -> None:
    tenant = session.get(Tenant, ctx.tenant_id)
    project = session.get(Project, ctx.project_id)
    user = session.get(User, ctx.user_id)
    if not tenant:
        raise ApiError("TENANT_NOT_FOUND", f"租户不存在：{ctx.tenant_id}", 403)
    if not project or project.tenant_id != ctx.tenant_id:
        raise ApiError("PROJECT_NOT_FOUND", f"项目不存在或不属于当前租户：{ctx.project_id}", 403)
    if "system" in ctx.roles:
        return
    if not user or user.tenant_id != ctx.tenant_id:
        raise ApiError("MEMBERSHIP_REQUIRED", "当前用户不属于请求租户", 403)
    if not user_has_project_membership(project, ctx.user_id):
        raise ApiError("PROJECT_MEMBERSHIP_REQUIRED", "当前用户不属于请求项目", 403)
    user_roles = {role for role in (user.roles or []) if isinstance(role, str)}
    unbound_roles = sorted(set(ctx.roles) - user_roles)
    if unbound_roles:
        raise ApiError(
            "TOKEN_ROLE_MISMATCH",
            "token 角色与当前用户授权不一致",
            403,
            details=[{"unbound_roles": unbound_roles}],
        )
    role_binding = project_member_role_binding(project, ctx.user_id)
    if enforce_project_roles and role_binding.duplicate:
        raise ApiError(
            "PROJECT_ROLE_BINDING_DUPLICATE",
            "项目成员存在重复角色绑定，服务端拒绝授权",
            403,
            details=[{"user_id": ctx.user_id, "roles_state": "duplicate"}],
        )
    if enforce_project_roles and not role_binding.configured:
        if is_production_environment(get_settings().app_env):
            raise ApiError(
                "PROJECT_ROLE_BINDING_REQUIRED",
                "项目成员缺少显式 roles 绑定，release/prod 环境拒绝兼容回退",
                403,
                details=[{"user_id": ctx.user_id, "roles_state": "missing"}],
            )
        # Legacy member rows without a roles key fall back to tenant-level user.roles
        # only outside release/prod. An explicit roles: [] never takes this branch.
        return
    if enforce_project_roles and not role_binding.roles:
        raise ApiError(
            "PROJECT_ROLE_BINDING_EMPTY",
            "项目成员显式 roles 为空，服务端会话拒绝授权",
            403,
            details=[{"user_id": ctx.user_id, "roles_state": "explicit_empty"}],
        )
    if enforce_project_roles:
        member_roles = set(role_binding.roles)
        unbound_project_roles = sorted(
            role for role in set(ctx.roles) - {"system"} if role not in member_roles
        )
        if unbound_project_roles:
            raise ApiError(
                "TOKEN_PROJECT_ROLE_MISMATCH",
                "token 角色与当前项目成员授权不一致",
                403,
                details=[{"unbound_roles": unbound_project_roles}],
            )


def record_request_trace_link(
    session: Session,
    ctx: RequestContext,
    request: Request,
) -> None:
    if request.method.upper() not in WRITE_METHODS:
        return
    parent_trace_id = ctx.parent_trace_id
    correlation_id = ctx.correlation_id or ctx.trace_id
    has_client_correlation = correlation_id != ctx.trace_id
    source_field = (
        "X-Trace-Id" if parent_trace_id else "X-Correlation-Id" if has_client_correlation else None
    )
    session.add(
        TraceRef(
            trace_ref_id=f"trace_ref_request_{ctx.trace_id.removeprefix('trace_')}",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status="active",
            trace_id=ctx.trace_id,
            payload={
                "root_trace_id": ctx.trace_id,
                "parent_trace_id": parent_trace_id,
                "correlation_id": correlation_id,
                "request_id": ctx.request_id,
                "ref_role": (
                    "request_parent"
                    if parent_trace_id
                    else "request_correlation"
                    if has_client_correlation
                    else "request_root"
                ),
                "type": "trace_context",
                "id": parent_trace_id or correlation_id,
                "source": (
                    "client_header" if parent_trace_id or has_client_correlation else "server"
                ),
                "source_field": source_field,
                "context_dimensions": {
                    "store_key": ctx.store_key,
                    "business_date": ctx.business_date,
                    "model_version": ctx.model_version,
                    "label_version": ctx.label_version,
                },
            },
        )
    )


async def request_context(
    request: Request,
    session: ContextSessionDep,
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    x_store_key: str | None = Header(default=None, alias="X-Store-Key"),
    x_business_date: str | None = Header(default=None, alias="X-Business-Date"),
    x_model_version: str | None = Header(default=None, alias="X-Model-Version"),
    x_label_version: str | None = Header(default=None, alias="X-Label-Version"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> RequestContext:
    trace = initialize_server_trace(request)
    settings = get_settings()
    raw_browser_session = request.cookies.get(settings.browser_session_cookie_name)
    # Bearer credentials are authoritative when both mechanisms are present.
    # Select the mechanism before deriving scope so an unrelated/stale cookie can
    # neither block a valid bearer nor silently downgrade a cookie write around CSRF.
    authentication_mode = "bearer" if authorization else "cookie" if raw_browser_session else "none"
    is_session_restore = request.url.path == f"{settings.api_prefix}/auth/session"
    resolved_actor: AuthActor | None = None
    if is_session_restore and authentication_mode == "none":
        raise ApiError("AUTH_REQUIRED", "需要有效的登录会话", 401)
    if (
        is_session_restore
        and authentication_mode == "bearer"
        and (not x_tenant_id or not x_project_id)
    ):
        resolved_actor = resolve_actor(authorization)
        if resolved_actor.provider == "oidc_bearer":
            default_scope = oidc_bearer_default_scope(session, resolved_actor)
            x_tenant_id = x_tenant_id or default_scope.tenant_id
            x_project_id = x_project_id or default_scope.project_id
    if (
        is_session_restore
        and authentication_mode == "cookie"
        and raw_browser_session
        and (not x_tenant_id or not x_project_id)
    ):
        if raw_browser_session.startswith("auris.v1.") and settings.allow_dev_auth:
            dev_actor = resolve_actor(f"Bearer {raw_browser_session}")
            x_tenant_id = x_tenant_id or next(iter(dev_actor.tenant_ids), "aurora_auto")
            x_project_id = x_project_id or next(
                (item for item in dev_actor.project_ids if item != "*"),
                "sales_qa",
            )
        else:
            default_scope = browser_session_default_scope(session, raw_token=raw_browser_session)
            x_tenant_id = x_tenant_id or default_scope.tenant_id
            x_project_id = x_project_id or default_scope.project_id
    if request.url.path.startswith("/api/v1") and not x_tenant_id:
        raise ApiError("CONTEXT_MISSING_TENANT", "缺少 X-Tenant-Id", 400)
    if request.url.path.startswith("/api/v1") and not x_project_id:
        raise ApiError("CONTEXT_MISSING_PROJECT", "缺少 X-Project-Id", 400)

    if authentication_mode == "bearer":
        actor = resolved_actor or resolve_actor(authorization)
    elif authentication_mode == "cookie" and raw_browser_session:
        if raw_browser_session.startswith("auris.v1.") and settings.allow_dev_auth:
            actor = resolve_actor(f"Bearer {raw_browser_session}")
        else:
            actor = authenticate_browser_session(
                session,
                raw_token=raw_browser_session,
                tenant_id=x_tenant_id or "aurora_auto",
                project_id=x_project_id or "sales_qa",
                method=request.method,
                csrf_token=x_csrf_token,
                origin=request.headers.get("Origin"),
                allowed_origins=_csv_items(settings.cors_allowed_origins),
            )
    else:
        actor = resolve_actor(None)
    if actor.provider == "oidc_bearer":
        actor = resolve_oidc_bearer_actor(
            session,
            actor,
            tenant_id=x_tenant_id or "aurora_auto",
            project_id=x_project_id or "sales_qa",
        )
    require_active_dev_session(session, actor)
    if not actor_allows_scope(actor, x_tenant_id or "aurora_auto", x_project_id or "sales_qa"):
        raise ApiError("TOKEN_SCOPE_MISMATCH", "token 不允许访问当前租户或项目", 403)
    ctx = RequestContext(
        tenant_id=x_tenant_id or "aurora_auto",
        project_id=x_project_id or "sales_qa",
        user_id=actor.user_id,
        roles=actor.roles,
        request_id=x_request_id or getattr(request.state, "request_id", None) or str(uuid.uuid4()),
        trace_id=trace.root_trace_id,
        idempotency_key=idempotency_key,
        parent_trace_id=trace.parent_trace_id,
        correlation_id=trace.correlation_id,
        auth_session_id=actor.session_id,
        store_key=_validated_context_dimension(x_store_key, header="X-Store-Key"),
        business_date=_validated_business_date(x_business_date),
        model_version=_validated_context_dimension(x_model_version, header="X-Model-Version"),
        label_version=_validated_context_dimension(x_label_version, header="X-Label-Version"),
        actor_kind=actor.actor_kind,
    )
    # Compatibility static tokens exist only in local/test/ci and preserve legacy fixtures.
    # Server-issued dev sessions and configured production providers always enforce project roles.
    require_context_membership(session, ctx, enforce_project_roles=actor.provider != "dev")
    record_request_trace_link(session, ctx, request)
    return ctx


def _external_completion_user_id(key_id: str) -> str:
    return f"ext_completion_{uuid.uuid5(uuid.NAMESPACE_URL, key_id).hex[:16]}"


async def signed_completion_context(
    request: Request,
    session: ContextSessionDep,
    x_tenant_id: str = Header(alias="X-Tenant-Id"),
    x_project_id: str = Header(alias="X-Project-Id"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> RequestContext:
    trace = initialize_server_trace(request)
    if not x_tenant_id:
        raise ApiError("CONTEXT_MISSING_TENANT", "缺少 X-Tenant-Id", 400)
    if not x_project_id:
        raise ApiError("CONTEXT_MISSING_PROJECT", "缺少 X-Project-Id", 400)
    if not idempotency_key:
        raise ApiError("IDEMPOTENCY_KEY_REQUIRED", "写操作必须提供 Idempotency-Key", 400)
    verification = await verify_completion_signature(
        request,
        tenant_id=x_tenant_id,
        project_id=x_project_id,
        idempotency_key=idempotency_key,
    )
    user_id = _external_completion_user_id(verification.key_id)
    ctx = RequestContext(
        tenant_id=x_tenant_id,
        project_id=x_project_id,
        user_id=user_id,
        roles=("system", "external_completion_client"),
        request_id=x_request_id or getattr(request.state, "request_id", None) or str(uuid.uuid4()),
        trace_id=trace.root_trace_id,
        idempotency_key=idempotency_key,
        parent_trace_id=trace.parent_trace_id,
        correlation_id=trace.correlation_id,
        actor_kind="service",
    )
    require_context_membership(session, ctx)
    record_request_trace_link(session, ctx, request)
    nonce_operation = f"signed_completion_nonce:{verification.key_id}"
    existing_nonce = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == ctx.tenant_id,
            IdempotencyRecord.project_id == ctx.project_id,
            IdempotencyRecord.user_id == ctx.user_id,
            IdempotencyRecord.operation == nonce_operation,
            IdempotencyRecord.idempotency_key == verification.nonce,
        )
    )
    if existing_nonce and existing_nonce.request_hash != verification.request_sha256:
        raise ApiError("COMPLETION_SIGNATURE_REPLAY", "完成回执 nonce 已被不同请求使用", 409)
    if not existing_nonce:
        session.add(
            IdempotencyRecord(
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                user_id=ctx.user_id,
                operation=nonce_operation,
                idempotency_key=verification.nonce,
                request_hash=verification.request_sha256,
                status_code=202,
                response_json={"data": {"nonce": verification.nonce, "status": "accepted"}},
            )
        )
    request.state.completion_signature = {
        "auth_mode": "signed_external_completion",
        "signature_key_id": verification.key_id,
        "authenticated_source": verification.source,
        "authenticated_tenant_id": verification.tenant_id,
        "authenticated_project_id": verification.project_id,
        "signature_binding_mode": verification.binding_mode,
        "signature_mode": verification.signature_mode,
        "nonce": verification.nonce,
        "request_sha256": verification.request_sha256,
        "body_sha256": verification.body_sha256,
        "signed_at": verification.timestamp,
    }
    return ctx
