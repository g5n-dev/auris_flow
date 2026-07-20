"""One-time server-side state for OIDC Authorization Code + PKCE."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from app.auth_models import OidcAuthorizationState
from app.core.errors import ApiError

_TRANSACTION_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_INVALID_TRANSACTION_SHA256 = "0" * 64
_DEFAULT_CLEANUP_LIMIT = 100


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


def _transaction_hash(transaction_secret: str) -> str:
    return hashlib.sha256(transaction_secret.encode("utf-8")).hexdigest()


def _validated_transaction_hash(transaction_secret: str) -> str:
    if not isinstance(transaction_secret, str) or not _TRANSACTION_SECRET_PATTERN.fullmatch(
        transaction_secret
    ):
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    return _transaction_hash(transaction_secret)


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
    transaction_secret: str,
    ttl_seconds: int,
    return_path: str | None = None,
    now: datetime | None = None,
) -> None:
    if not 60 <= ttl_seconds <= 900:
        raise ValueError("ttl_seconds must be between 60 and 900")
    if len(state) < 32 or len(nonce) < 32 or not 43 <= len(code_verifier) <= 128:
        raise ApiError("OIDC_STATE_INVALID", "OIDC 登录状态无效", 400)
    issued_at = _as_utc(now or datetime.now(UTC))
    transaction_sha256 = _validated_transaction_hash(transaction_secret)
    prune_authorization_states(session, now=issued_at, limit=_DEFAULT_CLEANUP_LIMIT)
    # A browser transaction may have only one outstanding state. Reusing the
    # short-lived HttpOnly transaction cookie replaces an older unfinished login.
    session.execute(
        delete(OidcAuthorizationState).where(
            OidcAuthorizationState.transaction_sha256 == transaction_sha256,
        )
    )
    session.add(
        OidcAuthorizationState(
            state_sha256=_state_hash(state),
            transaction_sha256=transaction_sha256,
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
    transaction_secret: str | None,
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
    transaction_is_valid = isinstance(transaction_secret, str) and bool(
        _TRANSACTION_SECRET_PATTERN.fullmatch(transaction_secret)
    )
    supplied_transaction_sha256 = (
        _transaction_hash(transaction_secret)
        if transaction_is_valid and isinstance(transaction_secret, str)
        else _INVALID_TRANSACTION_SHA256
    )
    stored_transaction_sha256 = (
        record.transaction_sha256 if record is not None else _INVALID_TRANSACTION_SHA256
    )
    transaction_matches = hmac.compare_digest(
        stored_transaction_sha256,
        supplied_transaction_sha256,
    )
    if (
        record is None
        or record.consumed_at is not None
        or not transaction_is_valid
        or not transaction_matches
    ):
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


def prune_authorization_states(
    session: Session,
    *,
    now: datetime | None = None,
    limit: int = _DEFAULT_CLEANUP_LIMIT,
) -> int:
    if isinstance(limit, bool) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    current = _as_utc(now or datetime.now(UTC))
    stale_state_hashes = tuple(
        session.scalars(
            select(OidcAuthorizationState.state_sha256)
            .where(
                or_(
                    OidcAuthorizationState.expires_at <= current,
                    OidcAuthorizationState.consumed_at.is_not(None),
                )
            )
            .order_by(
                OidcAuthorizationState.expires_at,
                OidcAuthorizationState.state_sha256,
            )
            .limit(limit)
        )
    )
    if not stale_state_hashes:
        return 0
    session.execute(
        delete(OidcAuthorizationState).where(
            OidcAuthorizationState.state_sha256.in_(stale_state_hashes)
        )
    )
    session.flush()
    return len(stale_state_hashes)


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
