from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0043_oidc_transaction_binding"
REVISION_LOGOUT = "0044_oidc_backchannel_logout"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0044_adds_hash_only_sid_and_persistent_unique_jti_replay_guard(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'oidc-backchannel-logout.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_LOGOUT)
    engine = create_engine(database_url, future=True)
    now = datetime.now(UTC)
    try:
        inspector = inspect(engine)
        assert "oidc_logout_token_replays" in inspector.get_table_names()
        session_columns = {
            column["name"]: column
            for column in inspector.get_columns("browser_auth_sessions")
        }
        assert session_columns["oidc_session_id_sha256"]["nullable"] is True
        assert {"oidc_session_id", "sid"}.isdisjoint(session_columns)
        session_indexes = {
            index["name"] for index in inspector.get_indexes("browser_auth_sessions")
        }
        assert "ix_browser_auth_sessions_oidc_sid_active" in session_indexes

        replay_columns = {
            column["name"]
            for column in inspector.get_columns("oidc_logout_token_replays")
        }
        assert {
            "logout_event_sha256",
            "issuer_sha256",
            "jti_sha256",
            "issued_at",
            "expires_at",
            "created_at",
            "updated_at",
        } == replay_columns
        assert {"issuer", "jti", "logout_token"}.isdisjoint(replay_columns)
        replay_uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "oidc_logout_token_replays"
            )
        }
        assert "uq_oidc_logout_token_replays_issuer_jti" in replay_uniques

        values = {
            "logout_event_sha256": "a" * 64,
            "issuer_sha256": "b" * 64,
            "jti_sha256": "c" * 64,
            "issued_at": now,
            "expires_at": now + timedelta(minutes=2),
        }
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oidc_logout_token_replays ("
                    "logout_event_sha256, issuer_sha256, jti_sha256, issued_at, expires_at"
                    ") VALUES ("
                    ":logout_event_sha256, :issuer_sha256, :jti_sha256, :issued_at, :expires_at"
                    ")"
                ),
                values,
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO oidc_logout_token_replays ("
                        "logout_event_sha256, issuer_sha256, jti_sha256, issued_at, expires_at"
                        ") VALUES ("
                        ":logout_event_sha256, :issuer_sha256, :jti_sha256, :issued_at, :expires_at"
                        ")"
                    ),
                    {**values, "logout_event_sha256": "d" * 64},
                )

        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                REVISION_LOGOUT
            )
    finally:
        engine.dispose()


def test_0044_downgrade_removes_only_backchannel_logout_expansion(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'oidc-backchannel-logout-roundtrip.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_LOGOUT)
    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        assert "oidc_logout_token_replays" not in inspector.get_table_names()
        session_columns = {
            column["name"]
            for column in inspector.get_columns("browser_auth_sessions")
        }
        assert "oidc_session_id_sha256" not in session_columns
        assert "oidc_authorization_states" in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                REVISION_BEFORE
            )
    finally:
        engine.dispose()
