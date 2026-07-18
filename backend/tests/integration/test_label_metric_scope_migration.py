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
REVISION_BEFORE = "0035_label_fact_temporal_heads"
REVISION_EXPAND = "0036_label_metric_snapshot_scopes"

METRIC_RESULT_EXPAND_COLUMNS = {
    "content_sha256",
    "source_manifest_sha256",
    "scope_sha256",
    "root_trace_id",
    "action_trace_id",
}
SCOPE_TABLES = {
    "metric_result_label_scopes",
    "insight_report_metric_bindings",
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


def _insert_metric_result(connection: Connection, metric_result_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO metric_results "
            "(metric_result_id, tenant_id, project_id, status, trace_id, payload) VALUES "
            "(:metric_result_id, 'tenant_scope', 'project_scope', 'materialized', "
            ":trace_id, :payload)"
        ),
        {
            "metric_result_id": metric_result_id,
            "trace_id": f"trace_{metric_result_id}",
            "payload": json.dumps({"legacy_writer": True}),
        },
    )


def _insert_label_version(connection: Connection, label_version_id: str) -> None:
    connection.execute(
        text(
            "INSERT INTO label_versions "
            "(label_version_id, tenant_id, project_id, status, resource_version, "
            "trace_id, payload) VALUES "
            "(:label_version_id, 'tenant_scope', 'project_scope', 'published', 1, "
            ":trace_id, :payload)"
        ),
        {
            "label_version_id": label_version_id,
            "trace_id": f"trace_{label_version_id}",
            "payload": json.dumps({}),
        },
    )


def _insert_dependencies(connection: Connection) -> None:
    _insert_label_version(connection, "lv_source")
    _insert_label_version(connection, "lv_target")
    connection.execute(
        text(
            "INSERT INTO label_mapping_bundles "
            "(mapping_bundle_id, tenant_id, project_id, target_label_version_id, "
            "source_label_version_ids, source_manifest_sha256, compiler_version, status, "
            "resource_version, canonical_manifest_sha256, root_trace_id, trace_id, payload) "
            "VALUES ('bundle_scope', 'tenant_scope', 'project_scope', 'lv_target', "
            ":sources, :source_sha, 'mapping-compiler/1', 'published', 1, :bundle_sha, "
            "'root_bundle', 'trace_bundle', :payload)"
        ),
        {
            "sources": json.dumps(["lv_source"]),
            "source_sha": "1" * 64,
            "bundle_sha": "2" * 64,
            "payload": json.dumps({}),
        },
    )
    connection.execute(
        text(
            "INSERT INTO label_fact_sets "
            "(fact_set_id, tenant_id, project_id, fact_namespace, target_label_version_id, "
            "status, fact_as_of, partition_manifest, partition_manifest_sha256, "
            "source_manifest_sha256, result_manifest_sha256, row_count, manifest_sha256, "
            "root_trace_id, action_trace_id, trace_id, payload) VALUES "
            "('fact_set_scope', 'tenant_scope', 'project_scope', 'insight-metrics', "
            "'lv_target', 'candidate', CURRENT_TIMESTAMP, :partition_manifest, :partition_sha, "
            ":source_sha, :result_sha, 1, :manifest_sha, 'root_fact_set', "
            "'action_fact_set', 'trace_fact_set', :payload)"
        ),
        {
            "partition_manifest": json.dumps({"partitions": ["2026-07"]}),
            "partition_sha": "3" * 64,
            "source_sha": "4" * 64,
            "result_sha": "5" * 64,
            "manifest_sha": "6" * 64,
            "payload": json.dumps({}),
        },
    )
    connection.execute(
        text(
            "INSERT INTO run_records "
            "(run_id, tenant_id, project_id, run_type, status, trace_id, payload) VALUES "
            "('run_scope_report', 'tenant_scope', 'project_scope', 'insight-report', "
            "'completed', 'trace_run_scope_report', :payload)"
        ),
        {"payload": json.dumps({})},
    )
    connection.execute(
        text(
            "INSERT INTO insight_reports "
            "(report_id, tenant_id, project_id, run_id, status, report_type, trace_id, payload) "
            "VALUES ('report_scope', 'tenant_scope', 'project_scope', 'run_scope_report', "
            "'completed', 'label-insight', 'trace_report_scope', :payload)"
        ),
        {"payload": json.dumps({})},
    )


def _scope_parameters(metric_result_id: str = "metric_valid") -> dict[str, object]:
    return {
        "scope_id": f"scope_{metric_result_id}",
        "metric_result_id": metric_result_id,
        "taxonomy_mode": "normalized",
        "source_versions": json.dumps(["lv_source"]),
        "target_version": "lv_target",
        "mapping_bundle_id": "bundle_scope",
        "mapping_bundle_sha": "2" * 64,
        "fact_namespace": "insight-metrics",
        "fact_set_id": "fact_set_scope",
        "fact_set_sha": "6" * 64,
        "fact_generation": 1,
        "metric_definitions": json.dumps({"tagged_reception_count": "3"}),
        "applicability": "required",
        "comparability": "comparable",
        "reasons": json.dumps([]),
        "scope_sha": "7" * 64,
        "source_sha": "8" * 64,
        "content_sha": "9" * 64,
        "payload": json.dumps({}),
    }


SCOPE_INSERT = text(
    "INSERT INTO metric_result_label_scopes "
    "(metric_scope_id, tenant_id, project_id, metric_result_id, taxonomy_mode, "
    "source_label_version_ids, target_label_version_id, mapping_bundle_id, "
    "mapping_bundle_sha256, fact_namespace, fact_set_id, fact_set_manifest_sha256, "
    "fact_set_generation, fact_as_of, metric_definition_versions, timezone, "
    "period_boundary, denominator_definition, label_version_applicability, "
    "comparability_status, comparability_reason_codes, scope_sha256, "
    "source_manifest_sha256, content_sha256, root_trace_id, action_trace_id, trace_id, "
    "payload) VALUES "
    "(:scope_id, 'tenant_scope', 'project_scope', :metric_result_id, :taxonomy_mode, "
    ":source_versions, :target_version, :mapping_bundle_id, :mapping_bundle_sha, "
    ":fact_namespace, :fact_set_id, :fact_set_sha, :fact_generation, CURRENT_TIMESTAMP, "
    ":metric_definitions, 'Asia/Shanghai', '[start,end)', 'eligible_receptions', "
    ":applicability, :comparability, :reasons, :scope_sha, :source_sha, :content_sha, "
    "'root_scope', 'action_scope', 'trace_scope', :payload)"
)


def _expect_scope_error(engine: Engine, **overrides: object) -> None:
    parameters = _scope_parameters(str(overrides.pop("metric_result_id", "metric_invalid")))
    parameters.update(overrides)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(SCOPE_INSERT, parameters)


def test_0036_expands_legacy_metrics_and_enforces_frozen_label_scope(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_metric_scope.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_metric_result(connection, "metric_legacy")
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_EXPAND)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_EXPAND
        inspector = inspect(engine)
        assert SCOPE_TABLES <= set(inspector.get_table_names())
        result_columns = {
            column["name"]: column for column in inspector.get_columns("metric_results")
        }
        assert METRIC_RESULT_EXPAND_COLUMNS <= set(result_columns)
        assert all(
            result_columns[name]["nullable"] is True for name in METRIC_RESULT_EXPAND_COLUMNS
        )

        with engine.begin() as connection:
            legacy = connection.execute(
                text(
                    "SELECT content_sha256, source_manifest_sha256, scope_sha256, "
                    "root_trace_id, action_trace_id FROM metric_results "
                    "WHERE metric_result_id = 'metric_legacy'"
                )
            ).one()
            assert legacy == (None, None, None, None, None)
            _insert_dependencies(connection)
            for result_id in (
                "metric_valid",
                "metric_bad_mode",
                "metric_bad_binding",
                "metric_bad_bundle_hash",
                "metric_bad_fact_hash",
                "metric_bad_generation",
                "metric_bad_applicability",
                "metric_bad_comparability",
                "metric_bad_hash",
            ):
                _insert_metric_result(connection, result_id)
            connection.execute(SCOPE_INSERT, _scope_parameters())
            connection.execute(
                text(
                    "INSERT INTO insight_report_metric_bindings "
                    "(report_metric_binding_id, tenant_id, project_id, report_id, "
                    "metric_result_ids, result_count, metric_scope_sha256, content_sha256, "
                    "root_trace_id, action_trace_id, trace_id, payload) VALUES "
                    "('report_binding_scope', 'tenant_scope', 'project_scope', 'report_scope', "
                    ":result_ids, 1, :scope_sha, :content_sha, 'root_report_binding', "
                    "'action_report_binding', 'trace_report_binding', :payload)"
                ),
                {
                    "result_ids": json.dumps(["metric_valid"]),
                    "scope_sha": "7" * 64,
                    "content_sha": "a" * 64,
                    "payload": json.dumps({}),
                },
            )

        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_mode",
            taxonomy_mode="latest",
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_binding",
            taxonomy_mode="native",
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_bundle_hash",
            mapping_bundle_sha="b" * 64,
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_fact_hash",
            fact_set_sha="c" * 64,
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_generation",
            fact_generation=0,
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_applicability",
            applicability="none",
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_comparability",
            comparability="unknown",
        )
        _expect_scope_error(
            engine,
            metric_result_id="metric_bad_hash",
            content_sha="short",
        )

        with pytest.raises(DBAPIError, match="append-only metric_results"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE metric_results SET status = 'snapshot' "
                        "WHERE metric_result_id = 'metric_legacy'"
                    )
                )
        for table_name, key_column, key_value in (
            ("metric_result_label_scopes", "metric_scope_id", "scope_metric_valid"),
            (
                "insight_report_metric_bindings",
                "report_metric_binding_id",
                "report_binding_scope",
            ),
        ):
            with pytest.raises(DBAPIError, match=f"append-only {table_name}"):
                with engine.begin() as connection:
                    connection.execute(
                        text(f"DELETE FROM {table_name} WHERE {key_column} = :key_value"),
                        {"key_value": key_value},
                    )
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_BEFORE)
    engine = _sqlite_engine(database_url)
    try:
        assert _revision(engine) == REVISION_BEFORE
        inspector = inspect(engine)
        assert not (SCOPE_TABLES & set(inspector.get_table_names()))
        assert not (
            METRIC_RESULT_EXPAND_COLUMNS
            & {column["name"] for column in inspector.get_columns("metric_results")}
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT status FROM metric_results WHERE metric_result_id = 'metric_legacy'"
                    )
                )
                == "materialized"
            )
    finally:
        engine.dispose()
