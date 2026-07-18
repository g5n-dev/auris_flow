from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0040_label_recomputation_fact_sets"
REVISION_OIDC = "0041_oidc_browser_sessions"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0041_creates_strong_oidc_and_opaque_session_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'oidc-session.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_OIDC)
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        assert {
            "user_security_states",
            "oidc_identities",
            "oidc_authorization_states",
            "browser_auth_sessions",
        }.issubset(inspector.get_table_names())
        assert {item["name"] for item in inspector.get_unique_constraints("oidc_identities")} == {
            "uq_oidc_identities_issuer_subject"
        }
        assert {
            item["name"] for item in inspector.get_unique_constraints("browser_auth_sessions")
        } == {"uq_browser_auth_sessions_token_sha256"}
        with engine.connect() as connection:
            users = connection.scalar(text("SELECT COUNT(*) FROM users"))
            security_states = connection.scalar(
                text("SELECT COUNT(*) FROM user_security_states WHERE status = 'active'")
            )
            assert users == security_states
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION_OIDC
            )
    finally:
        engine.dispose()


def test_0041_roundtrip_preserves_authoritative_users(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'oidc-session-roundtrip.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_OIDC)
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            user_count = connection.scalar(text("SELECT COUNT(*) FROM users"))
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        assert "browser_auth_sessions" not in inspector.get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM users")) == user_count
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_OIDC)
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM users")) == user_count
            assert (
                connection.scalar(text("SELECT COUNT(*) FROM user_security_states")) == user_count
            )
    finally:
        engine.dispose()
