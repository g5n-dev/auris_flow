from __future__ import annotations

import hmac
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import ContextDep, SessionDep
from app.core.auth import (
    DevAuthProfile,
    authenticate_bearer,
    dev_auth_enabled,
    get_dev_auth_profile,
    issue_dev_auth_token,
)
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.response import envelope
from app.models import AuthSession, User

router = APIRouter(prefix="/auth", tags=["auth"])


class DevLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


def _profile_payload(profile: DevAuthProfile) -> dict[str, object]:
    return {
        "user_id": profile.user_id,
        "name": profile.name,
        "email": profile.email,
        "role": profile.role_label,
        "roles": list(profile.roles),
        "initials": profile.initials,
        "tenant_id": profile.tenant_id,
        "tenant_name": profile.tenant_name,
        "project_id": profile.project_id,
        "project_name": profile.project_name,
    }


def _request_meta(request: Request) -> dict[str, str]:
    return {
        "trace_id": getattr(request.state, "trace_id", "trace_auth"),
        "request_id": getattr(request.state, "request_id", "auth-request"),
    }


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@router.post("/dev-login")
def dev_login(body: DevLoginRequest, request: Request, session: SessionDep) -> dict[str, object]:
    settings = get_settings()
    if not dev_auth_enabled(settings):
        raise ApiError("DEV_LOGIN_DISABLED", "当前环境未启用本地开发登录", 404)

    profile = get_dev_auth_profile(body.email)
    password_matches = hmac.compare_digest(body.password, settings.dev_auth_password)
    if profile is None or not password_matches:
        raise ApiError("INVALID_CREDENTIALS", "邮箱或密码错误", 401)

    if session.get(User, profile.user_id) is None:
        raise ApiError("DEV_USER_NOT_PROVISIONED", "开发账户尚未写入本地种子数据", 409)

    access_token, expires_at = issue_dev_auth_token(profile, settings, session=session)
    return {
        "data": {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_at": datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
            "user": _profile_payload(profile),
        },
        "meta": _request_meta(request),
    }


@router.get("/session")
def get_session(request: Request, session: SessionDep, ctx: ContextDep) -> dict[str, object]:
    user = session.get(User, ctx.user_id)
    if user is None:
        raise ApiError("AUTH_USER_NOT_FOUND", "当前认证用户不存在", 401)
    profile = next(
        (
            item
            for item in map(
                get_dev_auth_profile,
                (
                    "demo.operator@auris.local",
                    "admin@auris.local",
                    "release.approver@auris.local",
                    "annotator@auris.local",
                    "annotator.b@auris.local",
                    "model@auris.local",
                ),
            )
            if item and item.user_id == ctx.user_id
        ),
        None,
    )
    role_label = profile.role_label if profile else " / ".join(ctx.roles)
    data = {
        "user_id": ctx.user_id,
        "name": str(user.name),
        "email": str(user.email),
        "role": role_label,
        "roles": list(ctx.roles),
        "initials": profile.initials if profile else str(user.name)[:2].upper(),
        "tenant_id": ctx.tenant_id,
        "tenant_name": profile.tenant_name if profile else ctx.tenant_id,
        "project_id": ctx.project_id,
        "project_name": profile.project_name if profile else ctx.project_id,
        "provider": "dev_session" if dev_auth_enabled(get_settings()) else "configured",
    }
    return envelope(data, ctx, meta=_request_meta(request))


@router.post("/logout")
def logout(
    request: Request,
    session: SessionDep,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
) -> dict[str, object]:
    settings = get_settings()
    if not dev_auth_enabled(settings):
        raise ApiError("DEV_LOGOUT_DISABLED", "当前环境未启用本地开发会话注销", 404)
    if not x_tenant_id:
        raise ApiError("CONTEXT_MISSING_TENANT", "缺少 X-Tenant-Id", 400)
    if not x_project_id:
        raise ApiError("CONTEXT_MISSING_PROJECT", "缺少 X-Project-Id", 400)

    actor = authenticate_bearer(authorization)
    if (
        actor.provider != "dev_session"
        or actor.session_id is None
        or actor.issued_at is None
        or actor.expires_at is None
    ):
        raise ApiError("DEV_SESSION_REQUIRED", "注销仅支持服务端签发的开发会话", 401)

    record = session.execute(
        select(AuthSession).where(AuthSession.session_id == actor.session_id).with_for_update()
    ).scalar_one_or_none()
    if record is None:
        raise ApiError("AUTH_SESSION_NOT_FOUND", "开发会话不存在", 401)

    issued_at = _as_utc(record.issued_at)
    expires_at = _as_utc(record.expires_at)
    if (
        record.user_id != actor.user_id
        or record.provider != actor.provider
        or actor.tenant_ids != (record.tenant_id,)
        or int(issued_at.timestamp()) != actor.issued_at
        or int(expires_at.timestamp()) != actor.expires_at
    ):
        raise ApiError("AUTH_SESSION_SUBJECT_MISMATCH", "开发会话主体不匹配", 401)
    tenant_allowed = actor.tenant_ids == (record.tenant_id,) and x_tenant_id == record.tenant_id
    project_allowed = "*" in actor.project_ids or x_project_id in actor.project_ids
    if not tenant_allowed or not project_allowed:
        raise ApiError("TOKEN_SCOPE_MISMATCH", "token 不允许访问当前租户或项目", 403)

    now = datetime.now(UTC)
    if now >= expires_at:
        raise ApiError("AUTH_SESSION_EXPIRED", "开发会话已过期", 401)
    if record.revoked_at is None:
        record.revoked_at = now
        session.commit()
        revoked_at = now
    else:
        revoked_at = _as_utc(record.revoked_at)

    return {
        "data": {
            "status": "revoked",
            "session_id": record.session_id,
            "revoked_at": revoked_at.isoformat(),
        },
        "meta": _request_meta(request),
    }
