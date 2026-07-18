from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError

from app.models import (
    Base,
    InsightReportMetricBinding,
    MetricResult,
    MetricResultLabelScope,
)


def _constraint_names(model: Any, kind: type[Any]) -> set[str]:
    return {
        str(constraint.name)
        for constraint in model.__table__.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def _index_names(model: Any) -> set[str]:
    return {str(index.name) for index in model.__table__.indexes if index.name is not None}


def test_label_metric_scope_models_freeze_metric_and_report_bindings() -> None:
    assert {
        "content_sha256",
        "source_manifest_sha256",
        "scope_sha256",
        "root_trace_id",
        "action_trace_id",
    } <= set(MetricResult.__table__.columns.keys())

    assert MetricResultLabelScope.__tablename__ == "metric_result_label_scopes"
    assert {
        "uq_metric_result_label_scopes_scope_result",
        "uq_metric_result_label_scopes_scope_hash",
    } <= _constraint_names(MetricResultLabelScope, UniqueConstraint)
    assert {
        "fk_metric_result_label_scopes_scope_result",
        "fk_metric_result_label_scopes_scope_target_version",
        "fk_metric_result_label_scopes_scope_mapping_bundle",
        "fk_metric_result_label_scopes_scope_fact_set",
    } <= _constraint_names(MetricResultLabelScope, ForeignKeyConstraint)
    assert {
        "ck_metric_result_label_scopes_mode",
        "ck_metric_result_label_scopes_mode_binding",
        "ck_metric_result_label_scopes_generation",
        "ck_metric_result_label_scopes_applicability",
        "ck_metric_result_label_scopes_comparability",
        "ck_metric_result_label_scopes_hashes",
    } <= _constraint_names(MetricResultLabelScope, CheckConstraint)
    assert {
        "ix_metric_result_label_scopes_scope_target",
        "ix_metric_result_label_scopes_scope_fact_cutoff",
        "ix_metric_result_label_scopes_trace_id",
    } <= _index_names(MetricResultLabelScope)
    assert "updated_at" not in MetricResultLabelScope.__table__.columns

    assert InsightReportMetricBinding.__tablename__ == "insight_report_metric_bindings"
    assert {
        "uq_insight_report_metric_bindings_scope_report",
        "uq_insight_report_metric_bindings_scope_hash",
    } <= _constraint_names(InsightReportMetricBinding, UniqueConstraint)
    assert "fk_insight_report_metric_bindings_scope_report" in _constraint_names(
        InsightReportMetricBinding,
        ForeignKeyConstraint,
    )
    assert {
        "ck_insight_report_metric_bindings_hashes",
        "ck_insight_report_metric_bindings_result_count",
    } <= _constraint_names(InsightReportMetricBinding, CheckConstraint)
    assert "ix_insight_report_metric_bindings_trace_id" in _index_names(InsightReportMetricBinding)
    assert "updated_at" not in InsightReportMetricBinding.__table__.columns


def test_create_all_installs_metric_snapshot_append_only_guards() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    guarded_tables = {
        "metric_results",
        "metric_result_label_scopes",
        "insight_report_metric_bindings",
    }
    with engine.begin() as connection:
        trigger_rows = connection.execute(
            text(
                "SELECT tbl_name, name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name IN "
                "('metric_results', 'metric_result_label_scopes', "
                "'insight_report_metric_bindings')"
            )
        ).all()
        triggers_by_table = {
            table: {name for row_table, name in trigger_rows if row_table == table}
            for table in guarded_tables
        }
        assert triggers_by_table == {
            table: {
                f"trg_{table}_no_update",
                f"trg_{table}_no_delete",
            }
            for table in guarded_tables
        }

        connection.execute(
            text(
                "INSERT INTO metric_results "
                "(metric_result_id, tenant_id, project_id, status, trace_id, payload, "
                "content_sha256, source_manifest_sha256, scope_sha256, root_trace_id, "
                "action_trace_id) VALUES "
                "('metric-label-1', 'tenant-a', 'project-a', 'materialized', 'trace-a', "
                "'{}', :content_sha, :source_sha, :scope_sha, 'root-a', 'action-a')"
            ),
            {
                "content_sha": "a" * 64,
                "source_sha": "b" * 64,
                "scope_sha": "c" * 64,
            },
        )

    with pytest.raises(IntegrityError, match="append-only metric_results"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE metric_results SET status = 'snapshot' "
                    "WHERE metric_result_id = 'metric-label-1'"
                )
            )
