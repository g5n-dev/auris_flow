from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0037_label_fact_logical_active_heads"
REVISION_INTERVALS = "0038_release_head_interval_closure"


def _alembic(database_url: str, *arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
        capture_output=True,
        text=True,
    )


def _event_values(generation: int, *, effective_from: str) -> dict[str, object]:
    previous = generation - 1 if generation > 1 else None
    return {
        "head_event_id": f"rbhe_interval_{generation}",
        "generation": generation,
        "previous_generation": previous,
        "old_deployment_id": f"deployment-{previous}" if previous else None,
        "new_deployment_id": f"deployment-{generation}",
        "old_label_version_id": f"label-version-{previous}" if previous else None,
        "new_label_version_id": f"label-version-{generation}",
        "old_bundle_sha256": str(previous) * 64 if previous else None,
        "new_bundle_sha256": str(generation) * 64,
        "effective_from": effective_from,
        "content_sha256": f"{generation + 3}" * 64,
        "payload": json.dumps({"head_event_schema": "release-bundle-head-event/v2"}),
    }


def _insert_event(connection: object, values: dict[str, object]) -> None:
    connection.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO release_bundle_head_events ("
            "head_event_id, tenant_id, project_id, environment, generation, "
            "previous_generation, action, activation_status, old_deployment_id, "
            "new_deployment_id, old_label_version_id, new_label_version_id, "
            "old_bundle_sha256, new_bundle_sha256, effective_from, effective_to, "
            "command_id, completion_receipt_id, approval_id, content_sha256, actor_id, "
            "root_trace_id, trace_id, payload) VALUES ("
            ":head_event_id, 'tenant-interval', 'project-interval', 'production', "
            ":generation, :previous_generation, 'promote', 'active', "
            ":old_deployment_id, :new_deployment_id, :old_label_version_id, "
            ":new_label_version_id, :old_bundle_sha256, :new_bundle_sha256, "
            ":effective_from, NULL, NULL, NULL, NULL, :content_sha256, "
            "'admin-interval', 'trace-root-interval', 'trace-interval', :payload)"
        ),
        values,
    )


def test_0038_allows_only_write_once_contiguous_interval_closure(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'release-interval.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_INTERVALS)
    engine = create_engine(database_url, future=True)
    boundary = "2026-07-18 10:00:00.000000"
    try:
        with engine.begin() as connection:
            _insert_event(
                connection,
                _event_values(1, effective_from="2026-07-18 09:00:00.000000"),
            )
            connection.execute(
                text(
                    "UPDATE release_bundle_head_events SET effective_to = :boundary "
                    "WHERE head_event_id = 'rbhe_interval_1'"
                ),
                {"boundary": boundary},
            )
            _insert_event(connection, _event_values(2, effective_from=boundary))

        with pytest.raises(DBAPIError, match="one effective_to closure only"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE release_bundle_head_events SET effective_to = "
                        "'2026-07-18 11:00:00.000000' "
                        "WHERE head_event_id = 'rbhe_interval_1'"
                    )
                )
        with pytest.raises(DBAPIError, match="one effective_to closure only"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE release_bundle_head_events SET action = 'rollback' "
                        "WHERE head_event_id = 'rbhe_interval_2'"
                    )
                )
        with pytest.raises(DBAPIError, match="continuous"):
            with engine.begin() as connection:
                _insert_event(
                    connection,
                    _event_values(3, effective_from="2026-07-18 12:00:00.000000"),
                )
    finally:
        engine.dispose()


def test_0038_downgrade_restores_fully_append_only_update_guard(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'release-interval-down.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_INTERVALS)
    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = create_engine(database_url, future=True)
    try:
        with engine.begin() as connection:
            _insert_event(
                connection,
                _event_values(1, effective_from="2026-07-18 09:00:00.000000"),
            )
        with pytest.raises(DBAPIError, match="append-only release_bundle_head_events"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE release_bundle_head_events SET effective_to = "
                        "'2026-07-18 10:00:00.000000' "
                        "WHERE head_event_id = 'rbhe_interval_1'"
                    )
                )
    finally:
        engine.dispose()
