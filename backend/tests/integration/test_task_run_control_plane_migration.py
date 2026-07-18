from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0041_oidc_browser_sessions"
REVISION_CONTROL_PLANE = "0042_task_run_control_plane"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def test_0042_preserves_pre_rc_active_task_run_without_implicit_deadline_backfill(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'task-run-control-plane.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO run_records (run_id, tenant_id, project_id, run_type, "
                    "status, run_key, partition_key, trace_id, payload) VALUES ("
                    ":run_id, :tenant_id, :project_id, 'task_run', 'submitted', "
                    ":run_key, NULL, :trace_id, :payload)"
                ),
                {
                    "run_id": "task_run_pre_0042_rc",
                    "tenant_id": "tenant-upgrade",
                    "project_id": "project-upgrade",
                    "run_key": "pre-0042-rc",
                    "trace_id": "trace-pre-0042-rc",
                    "payload": json.dumps({"status": "submitted", "adapter": "dagster"}),
                },
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_CONTROL_PLANE)
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT status, deadline_at, next_status_sync_at, status_version, "
                    "monitor_generation FROM run_records WHERE run_id = :run_id"
                ),
                {"run_id": "task_run_pre_0042_rc"},
            ).one()
            assert row.status == "submitted"
            assert row.deadline_at is None
            assert row.next_status_sync_at is None
            assert row.status_version == 1
            assert row.monitor_generation == 0
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == REVISION_CONTROL_PLANE
            )
    finally:
        engine.dispose()
