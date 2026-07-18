"""add governed insight report, action, experiment and effect tables

Revision ID: 0012_insight_action_closure
Revises: 0011_outbox_delivery_attempts
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_insight_action_closure"
down_revision = "0011_outbox_delivery_attempts"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    op.create_table(
        "insight_reports",
        sa.Column("report_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="generating"),
        sa.Column("report_type", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "run_id", name="uq_insight_reports_scope_run"
        ),
    )
    op.create_index(
        "ix_insight_reports_scope_status",
        "insight_reports",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_insight_reports_trace_id", "insight_reports", ["trace_id"])

    op.create_table(
        "insight_actions",
        sa.Column("action_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("baseline_metric_result_id", sa.String(128), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("branch", sa.String(32), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("review_task_id", sa.String(128), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamps(),
    )
    op.create_index(
        "ix_insight_actions_scope_status",
        "insight_actions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_insight_actions_scope_report",
        "insight_actions",
        ["tenant_id", "project_id", "report_id"],
    )
    op.create_index("ix_insight_actions_review_task_id", "insight_actions", ["review_task_id"])
    op.create_index("ix_insight_actions_trace_id", "insight_actions", ["trace_id"])

    op.create_table(
        "insight_experiments",
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("eval_run_id", sa.String(128), nullable=False),
        sa.Column("baseline_metric_result_id", sa.String(128), nullable=False),
        sa.Column("outcome_metric_result_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_run_id",
            name="uq_insight_experiments_scope_run",
        ),
    )
    op.create_index(
        "ix_insight_experiments_scope_status",
        "insight_experiments",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_insight_experiments_trace_id", "insight_experiments", ["trace_id"])

    op.create_table(
        "insight_effects",
        sa.Column("effect_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("baseline_metric_result_id", sa.String(128), nullable=False),
        sa.Column("outcome_metric_result_id", sa.String(128), nullable=False),
        sa.Column("metric_key", sa.String(128), nullable=False),
        sa.Column("delta", sa.Float(), nullable=False),
        sa.Column("confidence_low", sa.Float(), nullable=True),
        sa.Column("confidence_high", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="measured"),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            name="uq_insight_effects_scope_experiment",
        ),
    )
    op.create_index(
        "ix_insight_effects_scope_action",
        "insight_effects",
        ["tenant_id", "project_id", "action_id"],
    )
    op.create_index("ix_insight_effects_trace_id", "insight_effects", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_insight_effects_trace_id", table_name="insight_effects")
    op.drop_index("ix_insight_effects_scope_action", table_name="insight_effects")
    op.drop_table("insight_effects")
    op.drop_index("ix_insight_experiments_trace_id", table_name="insight_experiments")
    op.drop_index("ix_insight_experiments_scope_status", table_name="insight_experiments")
    op.drop_table("insight_experiments")
    op.drop_index("ix_insight_actions_trace_id", table_name="insight_actions")
    op.drop_index("ix_insight_actions_review_task_id", table_name="insight_actions")
    op.drop_index("ix_insight_actions_scope_report", table_name="insight_actions")
    op.drop_index("ix_insight_actions_scope_status", table_name="insight_actions")
    op.drop_table("insight_actions")
    op.drop_index("ix_insight_reports_trace_id", table_name="insight_reports")
    op.drop_index("ix_insight_reports_scope_status", table_name="insight_reports")
    op.drop_table("insight_reports")
