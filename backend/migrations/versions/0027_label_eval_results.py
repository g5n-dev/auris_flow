"""add immutable labeling evaluation gate results

Revision ID: 0027_label_eval_results
Revises: 0026_label_closed_loop
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0027_label_eval_results"
down_revision = "0026_label_closed_loop"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _immutable_triggers(table: str) -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, 'append-only {table}'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table}_no_{action.lower()} "
                    f"BEFORE {action} ON {table} FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    f"SET MESSAGE_TEXT = 'append-only {table}'"
                )
            )


def _drop_immutable_triggers(table: str) -> None:
    for action in ("update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_no_{action}"))


def upgrade() -> None:
    op.create_table(
        "label_eval_results",
        sa.Column("eval_result_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("eval_run_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("dataset_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("sample_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("overall_metrics", sa.JSON(), nullable=False),
        sa.Column("bootstrap_ci", sa.JSON(), nullable=False),
        sa.Column("gate_results", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_run_id",
            name="uq_label_eval_results_scope_run",
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'blocked')",
            name="ck_label_eval_results_status",
        ),
    )
    op.create_index(
        "ix_label_eval_results_scope_status",
        "label_eval_results",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_eval_results_trace_id", "label_eval_results", ["trace_id"])

    op.create_table(
        "label_eval_suite_results",
        sa.Column("suite_result_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("eval_result_id", sa.String(128), nullable=False),
        sa.Column("suite", sa.String(32), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("sample_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("suite_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_result_id",
            "suite",
            name="uq_label_eval_suite_results_scope_suite",
        ),
        sa.CheckConstraint(
            "sample_count > 0",
            name="ck_label_eval_suite_results_sample_count",
        ),
        sa.CheckConstraint(
            "suite IN ('golden', 'boundary', 'adversarial', 'fresh', 'canary', 'regression')",
            name="ck_label_eval_suite_results_suite",
        ),
    )
    op.create_index(
        "ix_label_eval_suite_results_scope_result",
        "label_eval_suite_results",
        ["tenant_id", "project_id", "eval_result_id"],
    )
    op.create_index(
        "ix_label_eval_suite_results_trace_id",
        "label_eval_suite_results",
        ["trace_id"],
    )
    _immutable_triggers("label_eval_results")
    _immutable_triggers("label_eval_suite_results")


def downgrade() -> None:
    _drop_immutable_triggers("label_eval_suite_results")
    _drop_immutable_triggers("label_eval_results")
    op.drop_table("label_eval_suite_results")
    op.drop_table("label_eval_results")
