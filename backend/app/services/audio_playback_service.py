from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.audio_playback import AudioPlaybackGrant, verify_audio_playback_grant
from app.core.config import Settings
from app.core.context import RequestContext, require_context_membership
from app.core.errors import ApiError
from app.core.rbac import require_any_role
from app.models import AuthSession, User

AUDIO_PLAYBACK_READ_ROLES = (
    "project_admin",
    "asset_manager",
    "review_arbitrator",
    "annotator",
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
        raise ApiError("AUDIO_PLAYBACK_GRANT_REVOKED", "音频播放授权已撤销", 403)
    if grant.auth_session_id:
        auth_session = session.get(AuthSession, grant.auth_session_id)
        now = datetime.now(UTC)
        if auth_session is not None:
            expires_at = auth_session.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
        if (
            auth_session is None
            or auth_session.user_id != grant.user_id
            or auth_session.tenant_id != grant.tenant_id
            or auth_session.revoked_at is not None
            or expires_at <= now
        ):
            raise ApiError("AUDIO_PLAYBACK_GRANT_REVOKED", "音频播放授权已撤销", 403)

    ctx = RequestContext(
        tenant_id=grant.tenant_id,
        project_id=grant.project_id,
        user_id=grant.user_id,
        roles=tuple(str(role) for role in user.roles),
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
