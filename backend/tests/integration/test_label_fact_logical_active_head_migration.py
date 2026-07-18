from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0036_label_metric_snapshot_scopes"
REVISION_LOGICAL_HEAD = "0037_label_fact_logical_active_heads"
INDEX_NAME = "uq_label_facts_active_head"
LOGICAL_INDEX_COLUMNS = [
    "tenant_id",
    "project_id",
    "fact_namespace",
    "logical_key_sha",
    "active_slot",
]
LEGACY_INDEX_COLUMNS = [
    "tenant_id",
    "project_id",
    "subject_scope",
    "subject_key",
    "label_id",
    "active_slot",
]


def _alembic(
    database_url: str,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=check,
        capture_output=True,
        text=True,
    )


def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def _active_index_columns(engine: Engine) -> list[str]:
    index = next(
        item for item in inspect(engine).get_indexes("label_facts") if item["name"] == INDEX_NAME
    )
    assert index["unique"] == 1
    return list(index["column_names"])


def _insert_temporal_compatibility_fact(
    connection: Connection,
    *,
    fact_id: str,
    logical_key_sha: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_facts "
            "(fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
            "fact_namespace, logical_key_sha, revision, label_version_id, subject_scope, "
            "subject_key, label_id, value_type, value_json, authority, status, active_slot, "
            "review_decision_id, trace_id, payload) VALUES "
            "(:fact_id, 'tenant_logical_head', 'project_logical_head', :aggregate_id, NULL, "
            "'production', :logical_key_sha, 1, 'label-version-logical-head', "
            "'business-event', 'customer-42', 'purchase-intent', 'boolean', :value_json, "
            "'human-confirmed', 'active', 'active', NULL, :trace_id, :payload)"
        ),
        {
            "aggregate_id": f"aggregate-{fact_id}",
            "fact_id": fact_id,
            "logical_key_sha": logical_key_sha,
            "payload": json.dumps({"fixture": True}),
            "trace_id": f"trace-{fact_id}",
            "value_json": json.dumps(True),
        },
    )


def test_0037_allows_distinct_event_heads_and_rejects_duplicate_logical_head(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'logical-head.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_LOGICAL_HEAD)
    engine = _engine(database_url)
    try:
        assert _revision(engine) == REVISION_LOGICAL_HEAD
        assert _active_index_columns(engine) == LOGICAL_INDEX_COLUMNS
        with engine.begin() as connection:
            _insert_temporal_compatibility_fact(
                connection,
                fact_id="fact-event-17",
                logical_key_sha="1" * 64,
            )
            _insert_temporal_compatibility_fact(
                connection,
                fact_id="fact-event-18",
                logical_key_sha="2" * 64,
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                _insert_temporal_compatibility_fact(
                    connection,
                    fact_id="fact-event-17-duplicate",
                    logical_key_sha="1" * 64,
                )
    finally:
        engine.dispose()


def test_0037_downgrade_is_safe_only_before_distinct_event_heads_exist(
    tmp_path: Path,
) -> None:
    empty_database_url = f"sqlite:///{tmp_path / 'logical-head-empty.sqlite'}"
    _alembic(empty_database_url, "upgrade", REVISION_LOGICAL_HEAD)
    _alembic(empty_database_url, "downgrade", REVISION_BEFORE)
    empty_engine = _engine(empty_database_url)
    try:
        assert _revision(empty_engine) == REVISION_BEFORE
        assert _active_index_columns(empty_engine) == LEGACY_INDEX_COLUMNS
    finally:
        empty_engine.dispose()

    populated_database_url = f"sqlite:///{tmp_path / 'logical-head-populated.sqlite'}"
    _alembic(populated_database_url, "upgrade", REVISION_LOGICAL_HEAD)
    populated_engine = _engine(populated_database_url)
    try:
        with populated_engine.begin() as connection:
            _insert_temporal_compatibility_fact(
                connection,
                fact_id="fact-populated-event-17",
                logical_key_sha="3" * 64,
            )
            _insert_temporal_compatibility_fact(
                connection,
                fact_id="fact-populated-event-18",
                logical_key_sha="4" * 64,
            )
    finally:
        populated_engine.dispose()

    downgrade = _alembic(
        populated_database_url,
        "downgrade",
        REVISION_BEFORE,
        check=False,
    )
    assert downgrade.returncode != 0
    assert "cannot restore legacy LabelFact active index" in (downgrade.stderr + downgrade.stdout)
    populated_engine = _engine(populated_database_url)
    try:
        assert _revision(populated_engine) == REVISION_LOGICAL_HEAD
        assert _active_index_columns(populated_engine) == LOGICAL_INDEX_COLUMNS
    finally:
        populated_engine.dispose()
