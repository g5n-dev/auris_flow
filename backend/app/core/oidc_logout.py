"""Durable, scope-hiding OIDC Back-Channel Logout processing."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth_models import BrowserAuthSession, OidcIdentity, OidcLogoutTokenReplay
from app.core.oidc import OIDCBackChannelLogoutClaims, OIDCTokenValidationError

_REPLAY_RETENTION_AFTER_RECEIPT = timedelta(minutes=10)
_EXPIRED_REPLAY_CLEANUP_BATCH_SIZE = 256


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_sha256(issuer: str, token_id: str) -> str:
    return _sha256(f"{len(issuer)}:{issuer}\x00{token_id}")


def _numeric_date(value: int | float) -> datetime:
    try:
        converted = datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, TypeError, ValueError):
        raise OIDCTokenValidationError from None
    return converted


def process_backchannel_logout(
    session: Session,
    claims: OIDCBackChannelLogoutClaims,
    *,
    now: datetime | None = None,
) -> None:
    """Persist the replay tombstone and revoke matching live sessions atomically."""

    current = now or datetime.now(UTC)
    issued_at = _numeric_date(claims.issued_at)
    token_expires_at = _numeric_date(claims.expires_at)
    if token_expires_at <= issued_at:
        raise OIDCTokenValidationError
    replay_expires_at = max(
        token_expires_at,
        current + _REPLAY_RETENTION_AFTER_RECEIPT,
    )

    issuer_sha256 = _sha256(claims.issuer)
    token_id_sha256 = _sha256(claims.token_id)
    session.add(
        OidcLogoutTokenReplay(
            logout_event_sha256=_event_sha256(claims.issuer, claims.token_id),
            issuer_sha256=issuer_sha256,
            jti_sha256=token_id_sha256,
            issued_at=issued_at,
            expires_at=replay_expires_at,
        )
    )
    # The unique (issuer,jti) constraint is the cross-process replay boundary.
    # Flushing it before taking session locks makes a duplicate fail before any
    # logout mutation; the caller commits both replay and revocations together.
    session.flush()

    expired_replay_ids = tuple(
        session.scalars(
            select(OidcLogoutTokenReplay.logout_event_sha256)
            .where(OidcLogoutTokenReplay.expires_at <= current)
            .order_by(
                OidcLogoutTokenReplay.expires_at,
                OidcLogoutTokenReplay.logout_event_sha256,
            )
            .limit(_EXPIRED_REPLAY_CLEANUP_BATCH_SIZE)
        )
    )
    if expired_replay_ids:
        session.execute(
            delete(OidcLogoutTokenReplay).where(
                OidcLogoutTokenReplay.logout_event_sha256.in_(expired_replay_ids)
            )
        )

    statement = (
        select(BrowserAuthSession)
        .join(
            OidcIdentity,
            OidcIdentity.identity_id == BrowserAuthSession.oidc_identity_id,
        )
        .where(
            OidcIdentity.issuer == claims.issuer,
            OidcIdentity.issuer_sha256 == issuer_sha256,
            BrowserAuthSession.revoked_at.is_(None),
            BrowserAuthSession.expires_at > current,
        )
        .with_for_update()
    )
    if claims.session_id is not None:
        statement = statement.where(
            BrowserAuthSession.oidc_session_id_sha256 == _sha256(claims.session_id)
        )
    if claims.subject is not None:
        statement = statement.where(
            OidcIdentity.subject_sha256 == _sha256(claims.subject)
        )

    for record in session.scalars(statement):
        record.revoked_at = current
    session.flush()
