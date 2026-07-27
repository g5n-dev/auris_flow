from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth_models import OidcAuthorizationState
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.oidc_state import (
    consume_authorization_state,
    delete_consumed_authorization_state,
    prune_authorization_states,
    store_authorization_state,
)

STATE = "state-" + ("s" * 48)
STATE_TWO = "state-two-" + ("t" * 48)
NONCE = "nonce-" + ("n" * 48)
VERIFIER = "v" * 64
TRANSACTION_SECRET = "transaction-" + ("a" * 48)
OTHER_TRANSACTION_SECRET = "transaction-" + ("b" * 48)


def test_authorization_state_is_hashed_one_time_and_deletable() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        store_authorization_state(
            session,
            state=STATE,
            nonce=NONCE,
            code_verifier=VERIFIER,
            transaction_secret=TRANSACTION_SECRET,
            ttl_seconds=300,
            return_path="/insights?range=7d",
            now=now,
        )
    with SessionLocal.begin() as session:
        stored = session.query(OidcAuthorizationState).one()
        assert stored.state_sha256 != STATE
        assert stored.transaction_sha256 != TRANSACTION_SECRET
        assert STATE not in repr(stored)
        assert TRANSACTION_SECRET not in repr(stored)
        consumed = consume_authorization_state(
            session,
            state=STATE,
            transaction_secret=TRANSACTION_SECRET,
            now=now + timedelta(seconds=1),
        )
        assert consumed.nonce == NONCE
        assert consumed.code_verifier == VERIFIER
        assert consumed.return_path == "/insights?range=7d"

        with pytest.raises(ApiError) as replay:
            consume_authorization_state(
                session,
                state=STATE,
                transaction_secret=TRANSACTION_SECRET,
                now=now + timedelta(seconds=2),
            )
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
            transaction_secret=TRANSACTION_SECRET,
            ttl_seconds=60,
            now=now,
        )
    with SessionLocal.begin() as session:
        with pytest.raises(ApiError) as expired:
            consume_authorization_state(
                session,
                state=STATE,
                transaction_secret=TRANSACTION_SECRET,
                now=now + timedelta(seconds=61),
            )
        assert expired.value.code == "OIDC_STATE_EXPIRED"
        assert session.query(OidcAuthorizationState).one().consumed_at is not None


def test_authorization_state_is_bound_to_the_initiating_browser_without_cross_client_dos() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        store_authorization_state(
            session,
            state=STATE,
            nonce=NONCE,
            code_verifier=VERIFIER,
            transaction_secret=TRANSACTION_SECRET,
            ttl_seconds=300,
            now=now,
        )

    with SessionLocal.begin() as session:
        with pytest.raises(ApiError) as mismatched:
            consume_authorization_state(
                session,
                state=STATE,
                transaction_secret=OTHER_TRANSACTION_SECRET,
                now=now + timedelta(seconds=1),
            )
        assert mismatched.value.code == "OIDC_STATE_INVALID"
        assert session.query(OidcAuthorizationState).one().consumed_at is None

        consumed = consume_authorization_state(
            session,
            state=STATE,
            transaction_secret=TRANSACTION_SECRET,
            now=now + timedelta(seconds=2),
        )
        assert consumed.state_sha256


def test_same_binding_has_at_most_one_pending_authorization_transaction() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        store_authorization_state(
            session,
            state=STATE,
            nonce=NONCE,
            code_verifier=VERIFIER,
            transaction_secret=TRANSACTION_SECRET,
            ttl_seconds=300,
            now=now,
        )
        store_authorization_state(
            session,
            state=STATE_TWO,
            nonce=NONCE,
            code_verifier=VERIFIER,
            transaction_secret=TRANSACTION_SECRET,
            ttl_seconds=300,
            now=now + timedelta(seconds=1),
        )
        assert session.query(OidcAuthorizationState).count() == 1

    with SessionLocal.begin() as session:
        with pytest.raises(ApiError) as replaced:
            consume_authorization_state(
                session,
                state=STATE,
                transaction_secret=TRANSACTION_SECRET,
                now=now + timedelta(seconds=2),
            )
        assert replaced.value.code == "OIDC_STATE_INVALID"
        consume_authorization_state(
            session,
            state=STATE_TWO,
            transaction_secret=TRANSACTION_SECRET,
            now=now + timedelta(seconds=2),
        )


def test_authorization_state_cleanup_is_bounded_and_preserves_active_rows() -> None:
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        for index in range(4):
            store_authorization_state(
                session,
                state=f"state-{index}-" + (str(index) * 48),
                nonce=NONCE,
                code_verifier=VERIFIER,
                transaction_secret=f"transaction-{index}-" + (str(index) * 48),
                ttl_seconds=300,
                now=now,
            )
        stale = (
            session.query(OidcAuthorizationState)
            .order_by(OidcAuthorizationState.state_sha256)
            .all()[:3]
        )
        stale[0].issued_at = now - timedelta(minutes=10)
        stale[0].expires_at = now - timedelta(minutes=5)
        stale[1].issued_at = now - timedelta(minutes=10)
        stale[1].expires_at = now - timedelta(minutes=5)
        stale[2].consumed_at = now

    with SessionLocal.begin() as session:
        assert prune_authorization_states(session, now=now, limit=2) == 2
        assert session.query(OidcAuthorizationState).count() == 2
        assert prune_authorization_states(session, now=now, limit=2) == 1
        remaining = session.query(OidcAuthorizationState).one()
        assert remaining.consumed_at is None
    remaining_expires_at = remaining.expires_at
    if remaining_expires_at.tzinfo is None:
        remaining_expires_at = remaining_expires_at.replace(tzinfo=UTC)
    assert remaining_expires_at > now


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
                transaction_secret=TRANSACTION_SECRET,
                ttl_seconds=300,
                return_path=return_path,
            )
        assert captured.value.code == "OIDC_RETURN_PATH_INVALID"
