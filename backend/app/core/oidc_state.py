"""One-time server-side state for OIDC Authorization Code + PKCE."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.auth_models import OidcAuthorizationState
from app.core.errors import ApiError


@dataclass(frozen=True)
class ConsumedAuthorizationState:
    state_sha256: str
    nonce: str
    code_verifier: str
    return_path: str


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _safe_return_path(value: str | None) -> str:
    path = value or "/"
    if (
        not path.startswith("/")
        or path.startswith("//")
        or len(path) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ApiError("OIDC_RETURN_PATH_INVALID", "登录返回路径无效", 400)
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ApiError("OIDC_RETURN_PATH_INVALID", "登录返回路径无效", 400)
    return path


def store_authorization_state(
    session: Session,
    *,
    state: str,
    nonce: str,
    code_verifier: str,
    ttl_seconds: int,
    return_path: str | None = None,
    now: datetime | None = None,
) -> None:
    if not 60 <= ttl_seconds <= 900:
        raise ValueError("ttl_seconds must be between 60 and 900")
    if len(state) < 32 or len(nonce) < 32 or not 43 <= len(code_verifier) <= 128:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    issued_at = _as_utc(now or datetime.now(UTC))
    session.add(
        OidcAuthorizationState(
            state_sha256=_state_hash(state),
            nonce=nonce,
            code_verifier=code_verifier,
            return_path=_safe_return_path(return_path),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
    )
    session.flush()


def consume_authorization_state(
    session: Session,
    *,
    state: str,
    now: datetime | None = None,
) -> ConsumedAuthorizationState:
    if not isinstance(state, str) or len(state) < 32:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    state_sha256 = _state_hash(state)
    record = session.scalar(
        select(OidcAuthorizationState)
        .where(OidcAuthorizationState.state_sha256 == state_sha256)
        .with_for_update()
    )
    current = _as_utc(now or datetime.now(UTC))
    if record is None or record.consumed_at is not None:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    if current >= _as_utc(record.expires_at):
        record.consumed_at = current
        session.flush()
        raise ApiError("OIDC_STATE_EXPIRED", "OIDC 登录状态已过期", 400)
    record.consumed_at = current
    session.flush()
    return ConsumedAuthorizationState(
        state_sha256=state_sha256,
        nonce=record.nonce,
        code_verifier=record.code_verifier,
        return_path=record.return_path,
    )


def delete_consumed_authorization_state(
    session: Session,
    *,
    state_sha256: str,
) -> None:
    session.execute(
        delete(OidcAuthorizationState).where(
            OidcAuthorizationState.state_sha256 == state_sha256,
            OidcAuthorizationState.consumed_at.is_not(None),
        )
    )
    session.flush()
