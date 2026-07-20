from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0042_task_run_control_plane"
REVISION_BINDING = "0043_oidc_transaction_binding"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0043_invalidates_legacy_states_and_adds_non_null_transaction_binding(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'oidc-transaction-binding.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    now = datetime.now(UTC)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oidc_authorization_states ("
                    "state_sha256, nonce, code_verifier, return_path, issued_at, expires_at"
                    ") VALUES ("
                    ":state_sha256, :nonce, :code_verifier, '/', :issued_at, :expires_at"
                    ")"
                ),
                {
                    "state_sha256": "1" * 64,
                    "nonce": "nonce-" + ("n" * 48),
                    "code_verifier": "v" * 64,
                    "issued_at": now,
                    "expires_at": now + timedelta(minutes=5),
                },
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_BINDING)
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        columns = {
            column["name"]: column for column in inspector.get_columns("oidc_authorization_states")
        }
        indexes = {index["name"] for index in inspector.get_indexes("oidc_authorization_states")}
        unique_constraints = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("oidc_authorization_states")
        }
        assert columns["transaction_sha256"]["nullable"] is False
        assert "ix_oidc_authorization_states_transaction_pending" in indexes
        assert "uq_oidc_authorization_states_transaction" in unique_constraints
        with engine.connect() as connection:
            migrated = (
                connection.execute(
                    text(
                        "SELECT transaction_sha256, consumed_at "
                        "FROM oidc_authorization_states WHERE state_sha256 = :state_sha256"
                    ),
                    {"state_sha256": "1" * 64},
                )
                .mappings()
                .one()
            )
            assert migrated["transaction_sha256"] == "1" * 64
            assert migrated["consumed_at"] is not None
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                REVISION_BINDING
            )
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO oidc_authorization_states ("
                        "state_sha256, nonce, code_verifier, return_path, issued_at, expires_at"
                        ") VALUES ("
                        ":state_sha256, :nonce, :code_verifier, '/', :issued_at, :expires_at"
                        ")"
                    ),
                    {
                        "state_sha256": "2" * 64,
                        "nonce": "nonce-" + ("n" * 48),
                        "code_verifier": "v" * 64,
                        "issued_at": now,
                        "expires_at": now + timedelta(minutes=5),
                    },
                )
        except IntegrityError:
            pass
        else:
            raise AssertionError("0043 must reject an authorization state without binding hash")

        # A state created by 0043 is still browser-bound and pending before the
        # downgrade.  Rolling back to 0042 must invalidate it before removing
        # the binding column, otherwise the older runtime could consume it with
        # only the public OIDC state value.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO oidc_authorization_states ("
                    "state_sha256, transaction_sha256, nonce, code_verifier, "
                    "return_path, issued_at, expires_at"
                    ") VALUES ("
                    ":state_sha256, :transaction_sha256, :nonce, :code_verifier, "
                    "'/', :issued_at, :expires_at"
                    ")"
                ),
                {
                    "state_sha256": "3" * 64,
                    "transaction_sha256": "4" * 64,
                    "nonce": "nonce-" + ("p" * 48),
                    "code_verifier": "w" * 64,
                    "issued_at": now,
                    "expires_at": now + timedelta(minutes=5),
                },
            )
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        columns = {
            column["name"] for column in inspect(engine).get_columns("oidc_authorization_states")
        }
        assert "transaction_sha256" not in columns
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM oidc_authorization_states "
                        "WHERE state_sha256 = :state_sha256"
                    ),
                    {"state_sha256": "1" * 64},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT consumed_at FROM oidc_authorization_states "
                        "WHERE state_sha256 = :state_sha256"
                    ),
                    {"state_sha256": "3" * 64},
                )
                is not None
            )
    finally:
        engine.dispose()
