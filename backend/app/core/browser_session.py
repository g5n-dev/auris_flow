"""Opaque browser-session issuance, CSRF enforcement, and live authorization."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth_models import BrowserAuthSession, OidcIdentity, UserSecurityState
from app.core.auth import AuthActor
from app.core.errors import ApiError
from app.models import Project, Tenant, User

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MIN_OPAQUE_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class IssuedBrowserSession:
    session_id: str
    raw_token: str
    csrf_token: str
    issued_at: datetime
    expires_at: datetime
    tenant_id: str
    project_id: str
    user_id: str


@dataclass(frozen=True)
class BrowserSessionRevocation:
    session_id: str
    revoked_at: datetime


@dataclass(frozen=True)
class BrowserSessionScope:
    tenant_id: str
    project_id: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _secret_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_invalid_session() -> ApiError:
    return ApiError("AUTH_SESSION_INVALID", "浏览器会话无效", 401)


def _member_roles(project: Project, user_id: str) -> tuple[str, ...] | None:
    members = (project.data or {}).get("members")
    if not isinstance(members, list):
        return None
    for member in members:
        if not isinstance(member, dict):
            continue
        if (member.get("user_id") or member.get("id")) != user_id:
            continue
        roles = member.get("roles")
        if not isinstance(roles, list):
            return None
        return tuple(sorted({role for role in roles if isinstance(role, str) and role}))
    return None


def _require_live_subject(
    session: Session,
    record: BrowserAuthSession,
    *,
    tenant_id: str,
    project_id: str,
) -> tuple[User, tuple[str, ...]]:
    identity = session.get(OidcIdentity, record.oidc_identity_id)
    security = session.get(UserSecurityState, record.user_id)
    user = session.get(User, record.user_id)
    tenant = session.get(Tenant, tenant_id)
    project = session.get(Project, project_id)
    if (
        identity is None
        or identity.status != "active"
        or identity.user_id != record.user_id
        or identity.tenant_id != record.tenant_id
        or security is None
        or security.status != "active"
        or user is None
        or user.tenant_id != record.tenant_id
    ):
        raise ApiError("AUTH_SUBJECT_DISABLED", "认证主体不可用", 401)
    if (
        tenant_id != record.tenant_id
        or tenant is None
        or tenant.status != "active"
        or project is None
        or project.tenant_id != tenant_id
        or project.status != "active"
    ):
        raise ApiError("AUTH_SCOPE_REJECTED", "请求资源不可用", 404)
    bound_roles = _member_roles(project, record.user_id)
    if not bound_roles:
        raise ApiError("AUTH_SCOPE_REJECTED", "请求资源不可用", 404)
    user_roles = {role for role in (user.roles or []) if isinstance(role, str) and role}
    effective_roles = tuple(sorted(user_roles.intersection(bound_roles)))
    if not effective_roles:
        raise ApiError("AUTHORIZATION_REJECTED", "当前操作未获授权", 403)
    return user, effective_roles


def resolve_oidc_bearer_actor(
    session: Session,
    actor: AuthActor,
    *,
    tenant_id: str,
    project_id: str,
) -> AuthActor:
    """Map a validated external subject to current internal authorization state."""

    if actor.provider != "oidc_bearer" or not actor.oidc_issuer or not actor.oidc_subject:
        raise _reject_invalid_session()
    identity = find_oidc_identity(
        session,
        issuer=actor.oidc_issuer,
        subject=actor.oidc_subject,
    )
    provisional = BrowserAuthSession(
        browser_session_id="bearer-validation-only",
        token_sha256="0" * 64,
        csrf_sha256="0" * 64,
        oidc_identity_id=identity.identity_id,
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        provider="oidc_bearer",
        issued_at=datetime.fromtimestamp(actor.issued_at or 0, tz=UTC),
        expires_at=datetime.fromtimestamp(actor.expires_at or 1, tz=UTC),
        last_seen_at=datetime.fromtimestamp(actor.issued_at or 0, tz=UTC),
    )
    _user, roles = _require_live_subject(
        session,
        provisional,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    return AuthActor(
        user_id=identity.user_id,
        roles=roles,
        provider="oidc_bearer",
        tenant_ids=(identity.tenant_id,),
        project_ids=(project_id,),
        actor_kind="human",
        issued_at=actor.issued_at,
        expires_at=actor.expires_at,
        oidc_issuer=actor.oidc_issuer,
        oidc_subject=actor.oidc_subject,
    )


def create_browser_session(
    session: Session,
    *,
    identity_id: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedBrowserSession:
    if not 300 <= ttl_seconds <= 2_592_000:
        raise ValueError("ttl_seconds must be between 300 and 2592000")
    identity = session.execute(
        select(OidcIdentity).where(OidcIdentity.identity_id == identity_id).with_for_update()
    ).scalar_one_or_none()
    if identity is None or identity.status != "active":
        raise ApiError("OIDC_IDENTITY_NOT_PROVISIONED", "OIDC 身份尚未获准访问", 403)

    issued_at = _as_utc(now or _utc_now())
    provisional = BrowserAuthSession(
        browser_session_id=f"browser_{secrets.token_hex(24)}",
        token_sha256="0" * 64,
        csrf_sha256="0" * 64,
        oidc_identity_id=identity.identity_id,
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        provider="oidc_session",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
        last_seen_at=issued_at,
    )
    _require_live_subject(
        session,
        provisional,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
    )
    raw_token = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(48)
    provisional.token_sha256 = _secret_sha256(raw_token)
    provisional.csrf_sha256 = _secret_sha256(csrf_token)
    identity.last_login_at = issued_at
    session.add(provisional)
    session.flush()
    return IssuedBrowserSession(
        session_id=provisional.browser_session_id,
        raw_token=raw_token,
        csrf_token=csrf_token,
        issued_at=issued_at,
        expires_at=provisional.expires_at,
        tenant_id=identity.tenant_id,
        project_id=identity.project_id,
        user_id=identity.user_id,
    )


def _load_session_by_token(session: Session, raw_token: str) -> BrowserAuthSession:
    if not isinstance(raw_token, str) or len(raw_token) < MIN_OPAQUE_TOKEN_LENGTH:
        raise _reject_invalid_session()
    record = session.scalar(
        select(BrowserAuthSession).where(
            BrowserAuthSession.token_sha256 == _secret_sha256(raw_token)
        )
    )
    if record is None:
        raise _reject_invalid_session()
    return record


def browser_session_default_scope(session: Session, *, raw_token: str) -> BrowserSessionScope:
    record = _load_session_by_token(session, raw_token)
    current = _utc_now()
    if record.revoked_at is not None:
        raise ApiError("AUTH_SESSION_REVOKED", "浏览器会话已撤销", 401)
    if current >= _as_utc(record.expires_at):
        raise ApiError("AUTH_SESSION_EXPIRED", "浏览器会话已过期", 401)
    return BrowserSessionScope(tenant_id=record.tenant_id, project_id=record.project_id)


def rotate_browser_session_csrf(
    session: Session,
    *,
    raw_token: str,
    session_id: str,
) -> str:
    record = _load_session_by_token(session, raw_token)
    if record.browser_session_id != session_id or record.revoked_at is not None:
        raise _reject_invalid_session()
    if _utc_now() >= _as_utc(record.expires_at):
        raise ApiError("AUTH_SESSION_EXPIRED", "浏览器会话已过期", 401)
    csrf_token = secrets.token_urlsafe(48)
    record.csrf_sha256 = _secret_sha256(csrf_token)
    session.flush()
    return csrf_token


def find_oidc_identity(
    session: Session,
    *,
    issuer: str,
    subject: str,
) -> OidcIdentity:
    identity = session.scalar(
        select(OidcIdentity).where(
            OidcIdentity.issuer_sha256 == _secret_sha256(issuer),
            OidcIdentity.subject_sha256 == _secret_sha256(subject),
        )
    )
    if identity is None or not identity.matches(issuer=issuer, subject=subject):
        raise ApiError("OIDC_IDENTITY_NOT_PROVISIONED", "OIDC 身份尚未获准访问", 403)
    if identity.status != "active":
        raise ApiError("AUTH_SUBJECT_DISABLED", "认证主体不可用", 401)
    return identity


def _require_csrf(
    record: BrowserAuthSession,
    *,
    method: str,
    csrf_token: str | None,
    origin: str | None,
    allowed_origins: tuple[str, ...],
) -> None:
    if method.upper() in SAFE_METHODS:
        return
    if not origin:
        raise ApiError("CSRF_ORIGIN_REQUIRED", "Cookie 写请求缺少 Origin", 403)
    if origin not in allowed_origins:
        raise ApiError("CSRF_ORIGIN_REJECTED", "Cookie 写请求来源未获准", 403)
    if not csrf_token:
        raise ApiError("CSRF_TOKEN_REQUIRED", "Cookie 写请求缺少 CSRF token", 403)
    supplied_hash = _secret_sha256(csrf_token)
    if not hmac.compare_digest(record.csrf_sha256, supplied_hash):
        raise ApiError("CSRF_TOKEN_INVALID", "CSRF token 无效", 403)


def authenticate_browser_session(
    session: Session,
    *,
    raw_token: str,
    tenant_id: str,
    project_id: str,
    method: str,
    csrf_token: str | None,
    origin: str | None,
    allowed_origins: tuple[str, ...],
    now: datetime | None = None,
) -> AuthActor:
    record = _load_session_by_token(session, raw_token)
    current = _as_utc(now or _utc_now())
    if record.revoked_at is not None:
        raise ApiError("AUTH_SESSION_REVOKED", "浏览器会话已撤销", 401)
    if current >= _as_utc(record.expires_at):
        raise ApiError("AUTH_SESSION_EXPIRED", "浏览器会话已过期", 401)
    _require_csrf(
        record,
        method=method,
        csrf_token=csrf_token,
        origin=origin,
        allowed_origins=allowed_origins,
    )
    _user, roles = _require_live_subject(
        session,
        record,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if current - _as_utc(record.last_seen_at) >= timedelta(seconds=60):
        record.last_seen_at = current
        session.flush()
    return AuthActor(
        user_id=record.user_id,
        roles=roles,
        provider="oidc_session",
        tenant_ids=(record.tenant_id,),
        project_ids=(project_id,),
        actor_kind="human",
        session_id=record.browser_session_id,
        issued_at=int(_as_utc(record.issued_at).timestamp()),
        expires_at=int(_as_utc(record.expires_at).timestamp()),
    )


def revoke_browser_session(
    session: Session,
    *,
    raw_token: str,
    now: datetime | None = None,
) -> BrowserSessionRevocation:
    record = session.scalar(
        select(BrowserAuthSession)
        .where(BrowserAuthSession.token_sha256 == _secret_sha256(raw_token))
        .with_for_update()
    )
    if record is None:
        raise _reject_invalid_session()
    if record.revoked_at is None:
        record.revoked_at = _as_utc(now or _utc_now())
        session.flush()
    return BrowserSessionRevocation(
        session_id=record.browser_session_id,
        revoked_at=_as_utc(record.revoked_at),
    )
