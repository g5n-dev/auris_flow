from __future__ import annotations

import hmac
from datetime import UTC, datetime
from functools import lru_cache
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import RedirectResponse
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
from app.core.browser_session import (
    authenticate_browser_session,
    browser_session_default_scope,
    create_browser_session,
    find_oidc_identity,
    revoke_browser_session,
    rotate_browser_session_csrf,
)
from app.core.config import _csv_items, get_settings, is_production_environment
from app.core.errors import ApiError
from app.core.oidc import (
    OIDCConfigurationError,
    OIDCError,
    OIDCProviderConfig,
    OIDCProviderUnavailableError,
    OIDCTokenValidator,
)
from app.core.oidc_flow import (
    OIDCAuthorizationDeniedError,
    OIDCAuthorizationFlow,
    OIDCClientConfig,
)
from app.core.oidc_state import (
    consume_authorization_state,
    delete_consumed_authorization_state,
    store_authorization_state,
)
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


@lru_cache
def get_oidc_authorization_flow() -> OIDCAuthorizationFlow:
    settings = get_settings()
    provider = OIDCProviderConfig(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        discovery_url=settings.oidc_discovery_url or None,
        jwks_cache_ttl_seconds=settings.oidc_jwks_cache_ttl_seconds,
        clock_skew_seconds=settings.oidc_clock_skew_seconds,
        http_timeout_seconds=settings.oidc_http_timeout_seconds,
    )
    validator = OIDCTokenValidator(provider)
    return OIDCAuthorizationFlow(
        OIDCClientConfig(
            provider=provider,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret or None,
            redirect_uri=settings.oidc_redirect_uri,
            scopes=tuple(item for item in settings.oidc_scopes.split() if item != "openid"),
        ),
        token_validator=validator,
    )


def _translate_oidc_error(error: OIDCError) -> ApiError:
    if isinstance(error, OIDCProviderUnavailableError):
        return ApiError(
            "OIDC_PROVIDER_UNAVAILABLE",
            "OIDC 身份提供方暂不可用",
            503,
            retryable=True,
        )
    if isinstance(error, OIDCConfigurationError):
        return ApiError("OIDC_CONFIGURATION_INVALID", "OIDC 配置无效", 500)
    if isinstance(error, OIDCAuthorizationDeniedError):
        return ApiError("OIDC_AUTHORIZATION_DENIED", "OIDC 登录未获授权", 401)
    return ApiError(error.code, error.public_message, 400)


def _callback_state(raw_query: str) -> str:
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
    except (UnicodeError, ValueError):
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400) from None
    states = [value for key, value in pairs if key == "state"]
    if len(states) != 1 or len(states[0]) < 32:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    return states[0]


def _set_session_cookie(response: Response, value: str, *, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.browser_session_cookie_name,
        value=value,
        max_age=max_age,
        path="/",
        secure=is_production_environment(settings.app_env),
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/dev-login")
def dev_login(
    body: DevLoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> dict[str, object]:
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
    _set_session_cookie(
        response,
        access_token,
        max_age=max(0, expires_at - int(datetime.now(UTC).timestamp())),
    )
    return {
        "data": {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_at": datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
            "user": _profile_payload(profile),
        },
        "meta": _request_meta(request),
    }


@router.get("/oidc/login")
def oidc_login(
    request: Request,
    session: SessionDep,
    return_path: str | None = None,
) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_provider.strip().lower() != "oidc":
        raise ApiError("OIDC_LOGIN_DISABLED", "当前环境未启用 OIDC 登录", 404)
    try:
        authorization = get_oidc_authorization_flow().create_authorization_request()
    except OIDCError as error:
        raise _translate_oidc_error(error) from None
    store_authorization_state(
        session,
        state=authorization.state,
        nonce=authorization.nonce,
        code_verifier=authorization.code_verifier,
        ttl_seconds=settings.oidc_authorization_state_ttl_seconds,
        return_path=return_path,
    )
    session.commit()
    response = RedirectResponse(authorization.authorization_url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


@router.get("/oidc/callback")
def oidc_callback(request: Request, session: SessionDep) -> RedirectResponse:
    settings = get_settings()
    if settings.auth_provider.strip().lower() != "oidc":
        raise ApiError("OIDC_LOGIN_DISABLED", "当前环境未启用 OIDC 登录", 404)
    try:
        raw_query = request.scope.get("query_string", b"").decode("ascii")
    except UnicodeDecodeError:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400) from None
    state = _callback_state(raw_query)
    consumed = consume_authorization_state(session, state=state)
    # Consume before any network request so a code/state pair can never be retried.
    session.commit()
    try:
        callback = get_oidc_authorization_flow().parse_authorization_response(
            raw_query,
            expected_state=state,
        )
        token_set = get_oidc_authorization_flow().exchange_code(
            callback.code,
            code_verifier=consumed.code_verifier,
            expected_nonce=consumed.nonce,
        )
    except OIDCError as error:
        raise _translate_oidc_error(error) from None
    identity = find_oidc_identity(
        session,
        issuer=token_set.claims.issuer,
        subject=token_set.claims.subject,
    )
    issued = create_browser_session(
        session,
        identity_id=identity.identity_id,
        ttl_seconds=settings.oidc_session_ttl_seconds,
    )
    delete_consumed_authorization_state(session, state_sha256=consumed.state_sha256)
    session.commit()
    response = RedirectResponse(consumed.return_path, status_code=303)
    _set_session_cookie(response, issued.raw_token, max_age=settings.oidc_session_ttl_seconds)
    return response


@router.get("/session")
def get_session(
    request: Request,
    response: Response,
    session: SessionDep,
    ctx: ContextDep,
) -> dict[str, object]:
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
    is_browser_session = bool(ctx.auth_session_id and ctx.auth_session_id.startswith("browser_"))
    csrf_token: str | None = None
    if is_browser_session:
        raw_token = request.cookies.get(get_settings().browser_session_cookie_name)
        if not raw_token:
            raise ApiError("AUTH_SESSION_INVALID", "浏览器会话无效", 401)
        csrf_token = rotate_browser_session_csrf(
            session,
            raw_token=raw_token,
            session_id=ctx.auth_session_id or "",
        )
        session.commit()
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
        "provider": "oidc_session"
        if is_browser_session
        else "dev_session"
        if dev_auth_enabled(get_settings())
        else "configured",
    }
    if csrf_token:
        data["csrf_token"] = csrf_token
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return envelope(data, ctx, meta=_request_meta(request))


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_project_id: str | None = Header(default=None, alias="X-Project-Id"),
    x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> dict[str, object]:
    settings = get_settings()
    raw_browser_session = request.cookies.get(settings.browser_session_cookie_name)
    if raw_browser_session and not raw_browser_session.startswith("auris.v1."):
        scope = browser_session_default_scope(session, raw_token=raw_browser_session)
        authenticate_browser_session(
            session,
            raw_token=raw_browser_session,
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            method=request.method,
            csrf_token=x_csrf_token,
            origin=request.headers.get("Origin"),
            allowed_origins=_csv_items(settings.cors_allowed_origins),
        )
        receipt = revoke_browser_session(session, raw_token=raw_browser_session)
        session.commit()
        response.delete_cookie(
            settings.browser_session_cookie_name,
            path="/",
            secure=is_production_environment(settings.app_env),
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "data": {
                "status": "revoked",
                "session_id": receipt.session_id,
                "revoked_at": receipt.revoked_at.isoformat(),
            },
            "meta": _request_meta(request),
        }
    if not dev_auth_enabled(settings):
        raise ApiError("DEV_LOGOUT_DISABLED", "当前环境未启用本地开发会话注销", 404)
    if not x_tenant_id:
        raise ApiError("CONTEXT_MISSING_TENANT", "缺少 X-Tenant-Id", 400)
    if not x_project_id:
        raise ApiError("CONTEXT_MISSING_PROJECT", "缺少 X-Project-Id", 400)

    actor = authenticate_bearer(
        authorization or (f"Bearer {raw_browser_session}" if raw_browser_session else None)
    )
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
