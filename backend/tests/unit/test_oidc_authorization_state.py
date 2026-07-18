from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth_models import OidcAuthorizationState
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.oidc_state import (
    consume_authorization_state,
    delete_consumed_authorization_state,
    store_authorization_state,
)

STATE = "state-" + ("s" * 48)
NONCE = "nonce-" + ("n" * 48)
VERIFIER = "v" * 64


def test_authorization_state_is_hashed_one_time_and_deletable() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        store_authorization_state(
            session,
            state=STATE,
            nonce=NONCE,
            code_verifier=VERIFIER,
            ttl_seconds=300,
            return_path="/insights?range=7d",
            now=now,
        )
    with SessionLocal.begin() as session:
        stored = session.query(OidcAuthorizationState).one()
        assert stored.state_sha256 != STATE
        assert STATE not in repr(stored)
        consumed = consume_authorization_state(session, state=STATE, now=now + timedelta(seconds=1))
        assert consumed.nonce == NONCE
        assert consumed.code_verifier == VERIFIER
        assert consumed.return_path == "/insights?range=7d"

        with pytest.raises(ApiError) as replay:
            consume_authorization_state(session, state=STATE, now=now + timedelta(seconds=2))
        assert replay.value.code == "OIDC_STATE_INVALID"

        delete_consumed_authorization_state(session, state_sha256=consumed.state_sha256)
        assert session.query(OidcAuthorizationState).count() == 0


def test_expired_state_fails_closed_and_is_marked_consumed() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        store_authorization_state(
            session,
            state=STATE,
            nonce=NONCE,
            code_verifier=VERIFIER,
            ttl_seconds=60,
            now=now,
        )
    with SessionLocal.begin() as session:
        with pytest.raises(ApiError) as expired:
            consume_authorization_state(session, state=STATE, now=now + timedelta(seconds=61))
        assert expired.value.code == "OIDC_STATE_EXPIRED"
        assert session.query(OidcAuthorizationState).one().consumed_at is not None


@pytest.mark.parametrize(
    "return_path",
    [
        "https://attacker.example/steal",
        "//attacker.example/steal",
        "/safe#https://attacker.example",
        "/safe\nLocation: https://attacker.example",
    ],
)
def test_authorization_state_rejects_open_redirects(return_path: str) -> None:
    with SessionLocal.begin() as session:
        with pytest.raises(ApiError) as captured:
            store_authorization_state(
                session,
                state=STATE,
                nonce=NONCE,
                code_verifier=VERIFIER,
                ttl_seconds=300,
                return_path=return_path,
            )
        assert captured.value.code == "OIDC_RETURN_PATH_INVALID"
