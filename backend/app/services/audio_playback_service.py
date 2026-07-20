from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypeAlias

from sqlalchemy.orm import Session

from app.auth_models import BrowserAuthSession, OidcIdentity, UserSecurityState
from app.core.audio_playback import AudioPlaybackGrant, verify_audio_playback_grant
from app.core.config import Settings
from app.core.context import RequestContext, require_context_membership
from app.core.errors import ApiError
from app.core.project_membership import project_member_role_binding
from app.core.rbac import require_any_role
from app.models import AuthSession, Project, Tenant, User

AUDIO_PLAYBACK_READ_ROLES = (
    "project_admin",
    "asset_manager",
    "review_arbitrator",
    "annotator",
)

PlaybackSessionRecord: TypeAlias = AuthSession | BrowserAuthSession


@dataclass(frozen=True)
class PlaybackSessionResolution:
    record: PlaybackSessionRecord
    effective_roles: tuple[str, ...]


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _playback_grant_revoked() -> ApiError:
    return ApiError("AUDIO_PLAYBACK_GRANT_REVOKED", "音频播放授权已撤销", 403)


def _resolve_legacy_auth_session(
    auth_session: AuthSession,
    grant: AudioPlaybackGrant,
    *,
    user: User,
    now: datetime,
) -> PlaybackSessionResolution:
    if (
        auth_session.user_id != grant.user_id
        or auth_session.tenant_id != grant.tenant_id
        or auth_session.revoked_at is not None
        or _as_utc(auth_session.expires_at) <= now
    ):
        raise _playback_grant_revoked()
    current_roles = tuple(
        str(role) for role in (user.roles or []) if isinstance(role, str) and role
    )
    return PlaybackSessionResolution(record=auth_session, effective_roles=current_roles)


def _resolve_browser_auth_session(
    session: Session,
    browser_session: BrowserAuthSession,
    grant: AudioPlaybackGrant,
    *,
    user: User,
    now: datetime,
) -> PlaybackSessionResolution:
    identity = session.get(OidcIdentity, browser_session.oidc_identity_id)
    security = session.get(UserSecurityState, browser_session.user_id)
    tenant = session.get(Tenant, grant.tenant_id)
    project = session.get(Project, grant.project_id)
    issuer_sha256 = (
        hashlib.sha256(identity.issuer.encode("utf-8")).hexdigest() if identity else None
    )
    if (
        browser_session.provider != "oidc_session"
        or browser_session.user_id != grant.user_id
        or browser_session.tenant_id != grant.tenant_id
        or browser_session.project_id != grant.project_id
        or browser_session.revoked_at is not None
        or _as_utc(browser_session.expires_at) <= now
        or identity is None
        or identity.status != "active"
        or identity.issuer_sha256 != issuer_sha256
        or identity.user_id != browser_session.user_id
        or identity.tenant_id != browser_session.tenant_id
        or identity.project_id != browser_session.project_id
        or security is None
        or security.status != "active"
        or security.authz_version <= 0
        or user.tenant_id != browser_session.tenant_id
        or tenant is None
        or tenant.status != "active"
        or project is None
        or project.tenant_id != browser_session.tenant_id
        or project.status != "active"
    ):
        raise _playback_grant_revoked()

    role_binding = project_member_role_binding(project, browser_session.user_id)
    current_user_roles = {role for role in (user.roles or []) if isinstance(role, str) and role}
    effective_roles = tuple(sorted(current_user_roles.intersection(role_binding.roles)))
    if (
        role_binding.duplicate
        or not role_binding.configured
        or not role_binding.roles
        or not effective_roles
        or not set(effective_roles).intersection(AUDIO_PLAYBACK_READ_ROLES)
    ):
        raise _playback_grant_revoked()
    return PlaybackSessionResolution(
        record=browser_session,
        effective_roles=effective_roles,
    )


def resolve_playback_issuing_session(
    session: Session,
    grant: AudioPlaybackGrant,
    *,
    user: User,
    now: datetime | None = None,
) -> PlaybackSessionResolution | None:
    """Resolve the signed parent-session reference without type confusion.

    Legacy development sessions and production OIDC browser sessions live in
    separate tables. A colliding identifier is ambiguous and therefore denied.
    Browser sessions additionally re-evaluate the identity, user security state,
    tenant/project status, and current effective project roles for every stream.
    """

    if not grant.auth_session_id:
        return None
    legacy = session.get(AuthSession, grant.auth_session_id)
    browser = session.get(BrowserAuthSession, grant.auth_session_id)
    if (legacy is None) == (browser is None):
        raise _playback_grant_revoked()
    current = now or datetime.now(UTC)
    if legacy is not None:
        return _resolve_legacy_auth_session(legacy, grant, user=user, now=current)
    assert browser is not None
    return _resolve_browser_auth_session(
        session,
        browser,
        grant,
        user=user,
        now=current,
    )


def authorize_audio_playback_grant(
    session: Session,
    settings: Settings,
    token: str | None,
    *,
    request_id: str,
    trace_id: str,
    expected_audio_session_id: str | None = None,
) -> tuple[AudioPlaybackGrant, RequestContext]:
    if not token:
        raise ApiError(
            "AUDIO_PLAYBACK_GRANT_REQUIRED",
            "音频取流必须提供短期播放授权",
            403,
        )

    grant = verify_audio_playback_grant(settings, token)
    if (
        expected_audio_session_id is not None
        and grant.audio_session_id != expected_audio_session_id
    ):
        raise ApiError(
            "AUDIO_PLAYBACK_GRANT_SCOPE_MISMATCH",
            "音频播放授权与请求会话不匹配",
            403,
        )

    user = session.get(User, grant.user_id)
    if not user:
        raise _playback_grant_revoked()
    issuing_session = resolve_playback_issuing_session(
        session,
        grant,
        user=user,
    )
    roles = (
        issuing_session.effective_roles
        if issuing_session is not None
        else tuple(str(role) for role in (user.roles or []))
    )

    ctx = RequestContext(
        tenant_id=grant.tenant_id,
        project_id=grant.project_id,
        user_id=grant.user_id,
        roles=roles,
        request_id=request_id,
        trace_id=trace_id,
        auth_session_id=grant.auth_session_id,
    )
    require_context_membership(session, ctx)
    require_any_role(
        ctx,
        AUDIO_PLAYBACK_READ_ROLES,
        action="audio_recordings.stream",
    )
    return grant, ctx
