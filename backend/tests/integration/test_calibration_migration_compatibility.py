from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

VERIFY_MIGRATIONS = Path(__file__).resolve().parents[2] / "scripts" / "verify_migrations.py"


def _run_migration_verification(database_url: str) -> None:
    environment = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        [
            sys.executable,
            str(VERIFY_MIGRATIONS),
            "--database-url",
            database_url,
        ],
        check=True,
        env=environment,
        cwd=VERIFY_MIGRATIONS.parents[2],
    )


def test_unsupported_legacy_submission_fails_before_schema_expansion(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'calibration_preflight.sqlite'}"
    environment = {**os.environ, "DATABASE_URL": database_url}
    probe = """
import json
import os
import sys
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect, text

sys.path.insert(0, str(Path.cwd() / "backend"))
from scripts.verify_migrations import (
    alembic_config,
    insert_legacy_calibration_history,
)

database_url = os.environ["DATABASE_URL"]
config = alembic_config()
command.upgrade(config, "0018_blind_calibration_gold_loop")
insert_legacy_calibration_history(database_url)
engine = create_engine(database_url, future=True)
with engine.begin() as connection:
    connection.execute(
        text(
            "UPDATE calibration_submissions SET value_json = :value_json "
            "WHERE submission_id = 'sub_cal_legacy_nonzero_0_a'"
        ),
        {"value_json": json.dumps({"decision": "unknown"})},
    )
try:
    command.upgrade(config, "0019_calibration_hardening")
except RuntimeError:
    pass
else:
    raise AssertionError("unsupported legacy submission unexpectedly migrated")
columns = {column["name"] for column in inspect(engine).get_columns("calibration_rounds")}
if "cohen_kappa_defined" in columns:
    raise AssertionError("0019 expanded the schema before legacy preflight completed")
with engine.connect() as connection:
    version = connection.scalar(text("SELECT version_num FROM alembic_version"))
if version != "0018_blind_calibration_gold_loop":
    raise AssertionError(f"unexpected Alembic version after preflight rejection: {version}")
engine.dispose()
"""
    subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        cwd=VERIFY_MIGRATIONS.parents[2],
        env=environment,
    )


def test_calibration_0018_history_upgrades_on_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'calibration_migration.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    _run_migration_verification(database_url)


@pytest.mark.skipif(
    not os.getenv("MIGRATION_DATABASE_URL"),
    reason="MIGRATION_DATABASE_URL is not configured for the disposable MySQL migration test",
)
def test_calibration_0018_history_upgrades_on_mysql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ["MIGRATION_DATABASE_URL"]
    database_name = make_url(database_url).database or ""
    assert any(marker in database_name.lower() for marker in ("test", "migration", "e2e")), (
        "MIGRATION_DATABASE_URL must target a disposable test/migration/e2e database"
    )
    assert database_url != os.getenv("DATABASE_URL"), (
        "MIGRATION_DATABASE_URL must not be the application DATABASE_URL"
    )
    monkeypatch.setenv("DATABASE_URL", database_url)

    _run_migration_verification(database_url)
