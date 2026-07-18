from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import UniqueConstraint, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from app.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REVISION_0025 = "0025_eval_dataset_object_lock"
REVISION_0026 = "0026_label_closed_loop"
REVISION_0027 = "0027_label_eval_results"

EXPECTED_EVAL_TABLES = {"label_eval_results", "label_eval_suite_results"}

# Base metadata reflects the current head, while this test intentionally stops
# at 0026.  Keep every later alteration explicit so the 0026 snapshot remains
# an exact equality check instead of silently weakening it to a subset check.
POST_0026_COLUMNS = {
    "label_facts": {
        "active_slot",
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
    },
    "label_version_items": {"definition_sha256"},
}
POST_0026_INDEXES = {
    "label_facts": {
        "uq_label_facts_active_head",
        "ix_label_facts_temporal_as_of",
        "ix_label_facts_temporal_occurred",
        "ix_label_facts_temporal_source",
        "ix_label_facts_scope_fact_set",
    },
    "label_version_items": {"ix_label_version_items_scope_status"},
}
POST_0026_UNIQUE_CONSTRAINTS = {
    "label_facts": {
        "uq_label_facts_temporal_revision",
        "uq_label_facts_temporal_head_binding",
    },
}

EXPECTED_TABLES = {
    "label_nodes",
    "label_version_items",
    "label_extraction_runs",
    "label_observations",
    "label_aggregation_policy_versions",
    "label_aggregation_runs",
    "label_aggregates",
    "label_aggregate_members",
    "label_facts",
    "feedback_examples",
    "label_taxonomy_suggestions",
    "prompt_assets",
    "prompt_versions",
    "release_deployments",
}

EXPECTED_COLUMNS = {
    "label_observations": {
        "observation_id",
        "tenant_id",
        "project_id",
        "extraction_run_id",
        "subject_scope",
        "subject_key",
        "evidence_ref",
        "evidence_sha256",
        "label_version_id",
        "raw_label",
        "label_id",
        "value_type",
        "value_json",
        "source_family",
        "source_type",
        "model_version",
        "prompt_version_id",
        "schema_version",
        "calibration_version_id",
        "raw_confidence",
        "calibrated_confidence",
        "input_sha256",
        "output_sha256",
        "status",
        "trace_id",
        "payload",
        "created_at",
        "updated_at",
    },
    "label_aggregates": {
        "aggregate_id",
        "aggregation_run_id",
        "policy_version_id",
        "calibration_version_ids",
        "subject_scope",
        "subject_key",
        "label_id",
        "value_type",
        "value_json",
        "score",
        "margin",
        "risk_level",
        "decision",
        "reason_codes",
        "explanation",
        "bucket_sha256",
        "deterministic_hash",
        "review_task_id",
        "trace_id",
    },
    "prompt_versions": {
        "prompt_version_id",
        "prompt_asset_id",
        "parent_version_id",
        "label_version_id",
        "schema_version",
        "model_version",
        "template_json",
        "output_schema",
        "generation_params",
        "structured_diff",
        "source_badcase_refs",
        "content_sha256",
        "trace_id",
    },
    "release_deployments": {
        "deployment_id",
        "environment",
        "status",
        "stage",
        "label_version_id",
        "prompt_version_id",
        "model_version",
        "aggregation_policy_version_id",
        "eval_dataset_version_id",
        "eval_run_id",
        "rollback_target_deployment_id",
        "bundle_sha256",
        "rollout_percentage",
        "blocked_reasons",
        "monitor_metrics",
        "approved_by",
        "trace_id",
    },
}

EXPECTED_INDEXES = {
    "label_nodes": {
        "ix_label_nodes_scope_status",
        "ix_label_nodes_trace_id",
    },
    "label_observations": {
        "ix_label_observations_bucket",
        "ix_label_observations_evidence",
        "ix_label_observations_trace_id",
    },
    "label_aggregates": {
        "ix_label_aggregates_scope_subject",
        "ix_label_aggregates_scope_decision",
        "ix_label_aggregates_trace_id",
    },
    "feedback_examples": {
        "ix_feedback_examples_scope_type",
        "ix_feedback_examples_trace_id",
    },
    "prompt_versions": {
        "ix_prompt_versions_scope_status",
        "ix_prompt_versions_trace_id",
    },
    "release_deployments": {
        "ix_release_deployments_scope_status",
        "ix_release_deployments_trace_id",
    },
}

EXPECTED_UNIQUE_CONSTRAINTS = {
    "label_nodes": {"uq_label_nodes_scope_label"},
    "label_version_items": {"uq_label_version_items_scope_label"},
    "label_observations": {"uq_label_observations_scope"},
    "label_aggregation_policy_versions": {
        "uq_label_agg_policies_scope_version",
        "uq_label_agg_policies_scope_hash",
    },
    "label_aggregates": {"uq_label_aggregates_run_bucket"},
    "label_aggregate_members": {"uq_label_aggregate_members_pair"},
    "feedback_examples": {"uq_feedback_examples_decision_target"},
    "prompt_versions": {
        "uq_prompt_versions_asset_version",
        "uq_prompt_versions_scope_hash",
    },
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


def _revision(database_url: str) -> str:
    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            return str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()


def _assert_0026_schema(database_url: str) -> None:
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert EXPECTED_TABLES <= tables
        # Expand-only means the pre-existing business truth tables remain intact.
        assert {"label_versions", "human_review_tasks", "outbox_events"} <= tables

        for table_name in EXPECTED_TABLES:
            model_table = Base.metadata.tables[table_name]
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            expected_columns = set(model_table.columns.keys()) - POST_0026_COLUMNS.get(
                table_name, set()
            )
            assert actual_columns == expected_columns

            actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            expected_model_indexes = {
                str(index.name) for index in model_table.indexes if index.name is not None
            } - POST_0026_INDEXES.get(table_name, set())
            assert actual_indexes == expected_model_indexes

            actual_unique_constraints = {
                constraint["name"] for constraint in inspector.get_unique_constraints(table_name)
            }
            expected_unique_constraints = {
                str(constraint.name)
                for constraint in model_table.constraints
                if isinstance(constraint, UniqueConstraint) and constraint.name is not None
            } - POST_0026_UNIQUE_CONSTRAINTS.get(table_name, set())
            assert actual_unique_constraints == expected_unique_constraints

        for table_name, expected_columns in EXPECTED_COLUMNS.items():
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            assert expected_columns <= columns

        for table_name, expected_indexes in EXPECTED_INDEXES.items():
            indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            assert expected_indexes <= indexes

        for table_name, expected_constraints in EXPECTED_UNIQUE_CONSTRAINTS.items():
            constraints = {
                constraint["name"] for constraint in inspector.get_unique_constraints(table_name)
            }
            assert expected_constraints <= constraints
    finally:
        engine.dispose()


def test_label_closed_loop_0025_to_0026_is_expand_only_and_reversible(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_closed_loop.sqlite'}"

    _alembic(database_url, "upgrade", REVISION_0025)
    engine = create_engine(database_url, future=True)
    try:
        assert not (EXPECTED_TABLES & set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_0026)
    assert _revision(database_url) == REVISION_0026
    _assert_0026_schema(database_url)

    _alembic(database_url, "downgrade", REVISION_0025)
    assert _revision(database_url) == REVISION_0025
    engine = create_engine(database_url, future=True)
    try:
        tables_after_downgrade = set(inspect(engine).get_table_names())
        assert not (EXPECTED_TABLES & tables_after_downgrade)
        assert {"label_versions", "human_review_tasks", "outbox_events"} <= tables_after_downgrade
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_0026)
    assert _revision(database_url) == REVISION_0026
    _assert_0026_schema(database_url)


def test_label_eval_0026_to_0027_is_expand_only_append_only_and_reversible(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'label_eval_results.sqlite'}"
    _alembic(database_url, "upgrade", REVISION_0026)
    engine = create_engine(database_url, future=True)
    try:
        assert not (EXPECTED_EVAL_TABLES & set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()

    _alembic(database_url, "upgrade", REVISION_0027)
    assert _revision(database_url) == REVISION_0027
    engine = create_engine(database_url, future=True)
    try:
        inspector = inspect(engine)
        assert EXPECTED_EVAL_TABLES <= set(inspector.get_table_names())
        for table_name in EXPECTED_EVAL_TABLES:
            model_table = Base.metadata.tables[table_name]
            actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
            assert actual_columns == set(model_table.columns.keys())
            actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            assert actual_indexes == {index.name for index in model_table.indexes}
            actual_uniques = {
                constraint["name"] for constraint in inspector.get_unique_constraints(table_name)
            }
            expected_uniques = {
                constraint.name
                for constraint in model_table.constraints
                if isinstance(constraint, UniqueConstraint)
            }
            assert actual_uniques == expected_uniques

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO label_eval_results "
                    "(eval_result_id, tenant_id, project_id, eval_run_id, status, "
                    "binding_sha256, dataset_snapshot_sha256, sample_manifest_sha256, "
                    "result_sha256, overall_metrics, bootstrap_ci, gate_results, trace_id, "
                    "payload) VALUES "
                    "('ler_migration', 'tenant_migration', 'project_migration', "
                    "'eval_run_migration', 'passed', :sha, :sha, :sha, :sha, :metrics, "
                    ":bootstrap, :gates, 'trace_eval_migration', :payload)"
                ),
                {
                    "sha": "a" * 64,
                    "metrics": json.dumps({"macro_f1": 0.9}),
                    "bootstrap": json.dumps({"confidence_level": 0.95}),
                    "gates": json.dumps([{"code": "ALL", "passed": True}]),
                    "payload": json.dumps({"suite_count": 6}),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO label_eval_suite_results "
                    "(suite_result_id, tenant_id, project_id, eval_result_id, suite, "
                    "sample_count, sample_manifest_sha256, metrics, suite_sha256, trace_id) "
                    "VALUES ('lesr_migration', 'tenant_migration', 'project_migration', "
                    "'ler_migration', 'golden', 10, :sha, :metrics, :sha, "
                    "'trace_eval_migration')"
                ),
                {"sha": "a" * 64, "metrics": json.dumps({"macro_f1": 0.9})},
            )

        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE label_eval_results SET status = 'blocked' "
                        "WHERE eval_result_id = 'ler_migration'"
                    )
                )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM label_eval_suite_results "
                        "WHERE suite_result_id = 'lesr_migration'"
                    )
                )
    finally:
        engine.dispose()

    _alembic(database_url, "downgrade", REVISION_0026)
    assert _revision(database_url) == REVISION_0026
    engine = create_engine(database_url, future=True)
    try:
        assert not (EXPECTED_EVAL_TABLES & set(inspect(engine).get_table_names()))
        assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
