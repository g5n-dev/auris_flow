from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_BEFORE = "0034_label_lifecycle_mapping_expand"
REVISION_EXPAND = "0035_label_fact_temporal_heads"

TEMPORAL_TABLES = {
    "label_fact_heads",
    "label_fact_sets",
    "label_fact_set_heads",
    "label_fact_set_head_events",
}

TEMPORAL_FACT_COLUMNS = {
    "fact_namespace",
    "logical_key_sha",
    "revision",
    "event_or_segment_id",
    "assertion_slot",
    "occurred_at",
    "recorded_at",
    "occurred_at_origin",
    "source_kind",
    "human_review_decision_id",
    "recompute_run_item_id",
    "fact_set_id",
    "content_sha256",
    "root_trace_id",
    "action_trace_id",
}


def _alembic(database_url: str, *arguments: str) -> None:
    environment = {**os.environ, "DATABASE_URL": database_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *arguments],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection: object, _record: object) -> None:
        if isinstance(connection, sqlite3.Connection):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _revision(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.scalar(text("SELECT version_num FROM alembic_version")))


def _insert_label_version(
    connection: Connection,
    *,
    tenant_id: str,
    project_id: str,
    label_version_id: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_versions "
            "(label_version_id, tenant_id, project_id, status, resource_version, "
            "trace_id, payload) VALUES "
            "(:label_version_id, :tenant_id, :project_id, 'published', 1, "
            ":trace_id, :payload)"
        ),
        {
            "label_version_id": label_version_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "trace_id": f"trace_{label_version_id}",
            "payload": json.dumps({"legacy": True}),
        },
    )


def _insert_aggregate(
    connection: Connection,
    *,
    tenant_id: str,
    project_id: str,
    aggregate_id: str,
    label_version_id: str,
    subject_key: str,
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_aggregates "
            "(aggregate_id, tenant_id, project_id, aggregation_run_id, label_version_id, "
            "policy_version_id, calibration_version_ids, subject_scope, subject_key, "
            "label_id, value_type, value_json, score, margin, risk_level, decision, status, "
            "reason_codes, explanation, bucket_sha256, deterministic_hash, review_task_id, "
            "trace_id) VALUES "
            "(:aggregate_id, :tenant_id, :project_id, :run_id, :label_version_id, "
            "'policy_temporal', :empty_list, 'audio-session', :subject_key, "
            "'label_temporal', 'boolean', :value_json, 0.99, 0.9, 'low', "
            "'auto_accept', 'accepted', :empty_list, :empty_object, :bucket_sha, "
            ":deterministic_sha, NULL, :trace_id)"
        ),
        {
            "aggregate_id": aggregate_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_id": f"run_{aggregate_id}",
            "label_version_id": label_version_id,
            "subject_key": subject_key,
            "empty_list": json.dumps([]),
            "empty_object": json.dumps({}),
            "value_json": json.dumps(True),
            "bucket_sha": "b" * 64,
            "deterministic_sha": "d" * 64,
            "trace_id": f"trace_{aggregate_id}",
        },
    )


def _insert_legacy_fact(
    connection: Connection,
    *,
    fact_id: str,
    tenant_id: str,
    project_id: str,
    aggregate_id: str,
    label_version_id: str,
    subject_key: str,
    status: str = "active",
    active_slot: str | None = "active",
) -> None:
    connection.execute(
        text(
            "INSERT INTO label_facts "
            "(fact_id, tenant_id, project_id, aggregate_id, supersedes_fact_id, "
            "label_version_id, subject_scope, subject_key, label_id, value_type, "
            "value_json, authority, status, active_slot, review_decision_id, trace_id, "
            "payload) VALUES "
            "(:fact_id, :tenant_id, :project_id, :aggregate_id, NULL, :label_version_id, "
            "'audio-session', :subject_key, 'label_temporal', 'boolean', :value_json, "
            "'l2-auto-accepted', :status, :active_slot, NULL, :trace_id, :payload)"
        ),
        {
            "fact_id": fact_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "aggregate_id": aggregate_id,
            "label_version_id": label_version_id,
            "subject_key": subject_key,
            "value_json": json.dumps(True),
            "status": status,
            "active_slot": active_slot,
            "trace_id": f"trace_{fact_id}",
            "payload": json.dumps({"legacy": True}),
        },
    )


def _expect_db_error(engine: Engine, statement: str, parameters: dict[str, object]) -> None:
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(text(statement), parameters)


def test_0035_is_nullable_expand_and_keeps_legacy_writer_compatible(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_fact_expand.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_label_version(
                connection,
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                label_version_id="lv_legacy_fact",
            )
            _insert_aggregate(
                connection,
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                aggregate_id="aggregate_legacy_one",
                label_version_id="lv_legacy_fact",
                subject_key="session_legacy",
            )
            _insert_legacy_fact(
                connection,
                fact_id="fact_legacy_one",
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                aggregate_id="aggregate_legacy_one",
                label_version_id="lv_legacy_fact",
                subject_key="session_legacy",
            )
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_EXPAND
        inspector = inspect(engine)
        assert TEMPORAL_TABLES <= set(inspector.get_table_names())
        columns = {column["name"]: column for column in inspector.get_columns("label_facts")}
        assert TEMPORAL_FACT_COLUMNS <= set(columns)
        assert all(columns[name]["nullable"] is True for name in TEMPORAL_FACT_COLUMNS)
        assert columns["aggregate_id"]["nullable"] is False

        fact_triggers = {
            row[0]
            for row in engine.connect().execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = 'label_facts'"
                )
            )
        }
        assert "trg_label_facts_no_update" not in fact_triggers
        assert "trg_label_facts_no_delete" not in fact_triggers

        with engine.connect() as connection:
            legacy = connection.execute(
                text(
                    "SELECT status, active_slot, fact_namespace, logical_key_sha, revision "
                    "FROM label_facts WHERE fact_id = 'fact_legacy_one'"
                )
            ).one()
        assert legacy == ("active", "active", None, None, None)

        # The pre-0035 writer must still be able to retire its old active slot and
        # insert a replacement without supplying any temporal columns.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE label_facts SET status = 'superseded', active_slot = NULL "
                    "WHERE fact_id = 'fact_legacy_one'"
                )
            )
            _insert_aggregate(
                connection,
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                aggregate_id="aggregate_legacy_two",
                label_version_id="lv_legacy_fact",
                subject_key="session_legacy",
            )
            _insert_legacy_fact(
                connection,
                fact_id="fact_legacy_two",
                tenant_id="tenant_legacy",
                project_id="project_legacy",
                aggregate_id="aggregate_legacy_two",
                label_version_id="lv_legacy_fact",
                subject_key="session_legacy",
            )
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT fact_id, status, active_slot FROM label_facts ORDER BY fact_id")
            ).all()
        assert rows == [
            ("fact_legacy_one", "superseded", None),
            ("fact_legacy_two", "active", "active"),
        ]
    finally:
        engine.dispose()


def test_0035_scopes_heads_fact_sets_and_makes_head_events_append_only(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_fact_constraints.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        with engine.begin() as connection:
            for tenant_id, project_id, suffix in (
                ("tenant_a", "project_a", "a"),
                ("tenant_b", "project_b", "b"),
            ):
                _insert_label_version(
                    connection,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    label_version_id=f"lv_fact_{suffix}",
                )
                _insert_aggregate(
                    connection,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    aggregate_id=f"aggregate_fact_{suffix}",
                    label_version_id=f"lv_fact_{suffix}",
                    subject_key=f"session_{suffix}",
                )
                _insert_legacy_fact(
                    connection,
                    fact_id=f"fact_temporal_{suffix}",
                    tenant_id=tenant_id,
                    project_id=project_id,
                    aggregate_id=f"aggregate_fact_{suffix}",
                    label_version_id=f"lv_fact_{suffix}",
                    subject_key=f"session_{suffix}",
                )
                connection.execute(
                    text(
                        "UPDATE label_facts SET fact_namespace = 'production', "
                        "logical_key_sha = :logical_sha, revision = 1, "
                        "event_or_segment_id = :event_id, assertion_slot = 'primary', "
                        "occurred_at = CURRENT_TIMESTAMP, recorded_at = CURRENT_TIMESTAMP, "
                        "occurred_at_origin = 'source', source_kind = 'aggregate', "
                        "content_sha256 = :content_sha, root_trace_id = :root_trace_id, "
                        "action_trace_id = :action_trace_id WHERE fact_id = :fact_id"
                    ),
                    {
                        "logical_sha": suffix * 64,
                        "event_id": f"segment_{suffix}",
                        "content_sha": ("c" if suffix == "a" else "e") * 64,
                        "root_trace_id": f"root_trace_{suffix}",
                        "action_trace_id": f"action_trace_{suffix}",
                        "fact_id": f"fact_temporal_{suffix}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO label_fact_sets "
                        "(fact_set_id, tenant_id, project_id, fact_namespace, "
                        "target_label_version_id, status, fact_as_of, partition_manifest, "
                        "partition_manifest_sha256, source_manifest_sha256, "
                        "result_manifest_sha256, row_count, manifest_sha256, approval_id, "
                        "approved_by, approved_at, root_trace_id, action_trace_id, trace_id, "
                        "payload) VALUES "
                        "(:fact_set_id, :tenant_id, :project_id, 'production', "
                        ":label_version_id, 'published', CURRENT_TIMESTAMP, :manifest, "
                        ":partition_sha, :source_sha, :result_sha, 1, :manifest_sha, "
                        ":approval_id, 'user_admin', CURRENT_TIMESTAMP, :root_trace_id, "
                        ":action_trace_id, :trace_id, :payload)"
                    ),
                    {
                        "fact_set_id": f"fact_set_{suffix}",
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "label_version_id": f"lv_fact_{suffix}",
                        "manifest": json.dumps([f"partition_{suffix}"]),
                        "partition_sha": ("1" if suffix == "a" else "5") * 64,
                        "source_sha": ("2" if suffix == "a" else "6") * 64,
                        "result_sha": ("3" if suffix == "a" else "7") * 64,
                        "manifest_sha": ("4" if suffix == "a" else "8") * 64,
                        "approval_id": f"approval_set_{suffix}",
                        "root_trace_id": f"root_trace_set_{suffix}",
                        "action_trace_id": f"action_trace_set_{suffix}",
                        "trace_id": f"trace_set_{suffix}",
                        "payload": json.dumps({}),
                    },
                )

            connection.execute(
                text(
                    "INSERT INTO label_fact_heads "
                    "(fact_head_id, tenant_id, project_id, fact_namespace, logical_key_sha, "
                    "current_fact_id, current_revision, generation, root_trace_id, "
                    "action_trace_id, trace_id, payload) VALUES "
                    "('fact_head_a', 'tenant_a', 'project_a', 'production', :logical_sha, "
                    "'fact_temporal_a', 1, 1, 'root_head_a', 'action_head_a', "
                    "'trace_head_a', :payload)"
                ),
                {"logical_sha": "a" * 64, "payload": json.dumps({})},
            )
            connection.execute(
                text(
                    "INSERT INTO label_fact_set_heads "
                    "(fact_set_head_id, tenant_id, project_id, environment, fact_namespace, "
                    "current_fact_set_id, current_manifest_sha256, previous_fact_set_id, "
                    "previous_manifest_sha256, generation, status, root_trace_id, "
                    "action_trace_id, trace_id, payload) VALUES "
                    "('fact_set_head_a', 'tenant_a', 'project_a', 'production', "
                    "'production', 'fact_set_a', :manifest_sha, NULL, NULL, 1, 'active', "
                    "'root_set_head_a', 'action_set_head_a', 'trace_set_head_a', :payload)"
                ),
                {"manifest_sha": "4" * 64, "payload": json.dumps({})},
            )
            connection.execute(
                text(
                    "INSERT INTO label_fact_set_head_events "
                    "(head_event_id, tenant_id, project_id, environment, fact_namespace, "
                    "generation, previous_generation, action, old_fact_set_id, "
                    "old_manifest_sha256, new_fact_set_id, new_manifest_sha256, "
                    "effective_at, content_sha256, actor_id, root_trace_id, action_trace_id, "
                    "trace_id, payload) VALUES "
                    "('fact_set_event_a', 'tenant_a', 'project_a', 'production', "
                    "'production', 1, NULL, 'bootstrap', NULL, NULL, 'fact_set_a', "
                    ":manifest_sha, CURRENT_TIMESTAMP, :content_sha, 'user_admin', "
                    "'root_set_event_a', 'action_set_event_a', 'trace_set_event_a', :payload)"
                ),
                {
                    "manifest_sha": "4" * 64,
                    "content_sha": "9" * 64,
                    "payload": json.dumps({}),
                },
            )

        _expect_db_error(
            engine,
            "INSERT INTO label_fact_heads "
            "(fact_head_id, tenant_id, project_id, fact_namespace, logical_key_sha, "
            "current_fact_id, current_revision, generation, root_trace_id, "
            "action_trace_id, trace_id, payload) VALUES "
            "('fact_head_cross_scope', 'tenant_b', 'project_b', 'production', :logical_sha, "
            "'fact_temporal_a', 1, 1, 'root_cross', 'action_cross', 'trace_cross', :payload)",
            {"logical_sha": "a" * 64, "payload": json.dumps({})},
        )
        _expect_db_error(
            engine,
            "INSERT INTO label_fact_set_heads "
            "(fact_set_head_id, tenant_id, project_id, environment, fact_namespace, "
            "current_fact_set_id, current_manifest_sha256, generation, status, "
            "root_trace_id, action_trace_id, trace_id, payload) VALUES "
            "('fact_set_head_cross', 'tenant_b', 'project_b', 'production', 'production', "
            "'fact_set_a', :manifest_sha, 1, 'active', 'root_cross', 'action_cross', "
            "'trace_cross', :payload)",
            {"manifest_sha": "4" * 64, "payload": json.dumps({})},
        )
        _expect_db_error(
            engine,
            "UPDATE label_fact_set_head_events SET actor_id = 'tampered' "
            "WHERE head_event_id = 'fact_set_event_a'",
            {},
        )
        _expect_db_error(
            engine,
            "DELETE FROM label_fact_set_head_events WHERE head_event_id = 'fact_set_event_a'",
            {},
        )
        _expect_db_error(
            engine,
            "UPDATE label_facts SET revision = 0 WHERE fact_id = 'fact_temporal_a'",
            {},
        )
    finally:
        engine.dispose()


def test_0035_empty_schema_downgrade_is_reversible(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_fact_downgrade.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_EXPAND)
    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_BEFORE
        inspector = inspect(engine)
        assert not (TEMPORAL_TABLES & set(inspector.get_table_names()))
        fact_columns = {column["name"] for column in inspector.get_columns("label_facts")}
        assert not (TEMPORAL_FACT_COLUMNS & fact_columns)
        assert "active_slot" in fact_columns
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_EXPAND
        assert TEMPORAL_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
