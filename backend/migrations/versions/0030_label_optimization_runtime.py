"""add durable label optimization scheduler and bounded rounds

Revision ID: 0030_label_optimization_runtime
Revises: 0029_label_calibration_fact_chain
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0030_label_optimization_runtime"
down_revision = "0029_label_calibration_fact_chain"
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
        "label_optimization_schedules",
        sa.Column("schedule_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("aggregation_policy_version_id", sa.String(128), nullable=False),
        sa.Column("eval_dataset_version_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "schedule_timezone", sa.String(64), server_default="Asia/Shanghai", nullable=False
        ),
        sa.Column("threshold_interval_seconds", sa.Integer(), server_default="900", nullable=False),
        sa.Column("daily_hour", sa.Integer(), server_default="2", nullable=False),
        sa.Column("weekly_day", sa.Integer(), server_default="6", nullable=False),
        sa.Column("next_threshold_scan_at", _utc_datetime(), nullable=False),
        sa.Column("next_daily_at", _utc_datetime(), nullable=False),
        sa.Column("next_weekly_at", _utc_datetime(), nullable=False),
        sa.Column("last_threshold_scanned_at", _utc_datetime()),
        sa.Column("last_daily_started_at", _utc_datetime()),
        sa.Column("last_weekly_started_at", _utc_datetime()),
        sa.Column("active_run_id", sa.String(128)),
        sa.Column("baseline_snapshot_id", sa.String(128)),
        sa.Column("scan_claim_token", sa.String(64)),
        sa.Column("scan_claimed_at", _utc_datetime()),
        sa.Column("budget", sa.JSON(), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            name="uq_label_opt_schedules_scope",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')",
            name="ck_label_opt_schedules_status",
        ),
        sa.CheckConstraint(
            "threshold_interval_seconds >= 900",
            name="ck_label_opt_schedules_threshold_interval",
        ),
        sa.CheckConstraint(
            "daily_hour BETWEEN 0 AND 23",
            name="ck_label_opt_schedules_daily_hour",
        ),
        sa.CheckConstraint(
            "weekly_day BETWEEN 0 AND 6",
            name="ck_label_opt_schedules_weekly_day",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_label_opt_schedules_resource_version",
        ),
    )
    op.create_index(
        "ix_label_opt_schedules_due",
        "label_optimization_schedules",
        ["status", "next_threshold_scan_at", "next_daily_at", "next_weekly_at"],
    )
    op.create_index(
        "ix_label_opt_schedules_active_run_id",
        "label_optimization_schedules",
        ["active_run_id"],
    )
    op.create_index(
        "ix_label_opt_schedules_trace_id",
        "label_optimization_schedules",
        ["trace_id"],
    )

    op.create_table(
        "label_optimization_metric_snapshots",
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("schedule_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("aggregation_policy_version_id", sa.String(128), nullable=False),
        sa.Column("eval_dataset_version_id", sa.String(128), nullable=False),
        sa.Column("snapshot_kind", sa.String(16), nullable=False),
        sa.Column("window_started_at", _utc_datetime(), nullable=False),
        sa.Column("window_ended_at", _utc_datetime(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reason_counts", sa.JSON(), nullable=False),
        sa.Column("rejection_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rejected_records", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "schedule_id",
            "snapshot_sha256",
            name="uq_label_opt_metric_snapshots_hash",
        ),
        sa.CheckConstraint(
            "snapshot_kind IN ('baseline', 'window')",
            name="ck_label_opt_metric_snapshots_kind",
        ),
        sa.CheckConstraint(
            "window_ended_at >= window_started_at",
            name="ck_label_opt_metric_snapshots_window",
        ),
        sa.CheckConstraint(
            "rejection_count >= 0",
            name="ck_label_opt_metric_snapshots_rejections",
        ),
    )
    op.create_index(
        "ix_label_opt_metric_snapshots_scope_window",
        "label_optimization_metric_snapshots",
        ["tenant_id", "project_id", "label_version_id", "window_ended_at"],
    )
    op.create_index(
        "ix_label_opt_metric_snapshots_trace_id",
        "label_optimization_metric_snapshots",
        ["trace_id"],
    )

    op.create_table(
        "label_optimization_rounds",
        sa.Column("round_id", sa.String(128), primary_key=True),
        sa.Column("schedule_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("optimization_run_id", sa.String(128), nullable=False),
        sa.Column("generation_run_id", sa.String(128), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("aggregation_policy_version_id", sa.String(128), nullable=False),
        sa.Column("eval_dataset_version_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="generating-candidates", nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("candidate_ids", sa.JSON(), nullable=False),
        sa.Column("eval_run_ids", sa.JSON(), nullable=False),
        sa.Column("selected_prompt_version_id", sa.String(128)),
        sa.Column("latest_gain_ppm", sa.Integer()),
        sa.Column(
            "critical_metric_regressed", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("cost_spent_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("consecutive_failed_rounds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stop_reason_codes", sa.JSON(), nullable=False),
        sa.Column("started_at", _utc_datetime(), nullable=False),
        sa.Column("completed_at", _utc_datetime()),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "optimization_run_id",
            "round_number",
            name="uq_label_opt_rounds_run_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "generation_run_id",
            name="uq_label_opt_rounds_generation_run",
        ),
        sa.CheckConstraint(
            "round_number BETWEEN 1 AND 3",
            name="ck_label_opt_rounds_number",
        ),
        sa.CheckConstraint(
            "candidate_count BETWEEN 0 AND 5",
            name="ck_label_opt_rounds_candidate_count",
        ),
        sa.CheckConstraint(
            "cost_spent_micros >= 0",
            name="ck_label_opt_rounds_cost",
        ),
        sa.CheckConstraint(
            "consecutive_failed_rounds >= 0",
            name="ck_label_opt_rounds_failures",
        ),
        sa.CheckConstraint(
            "status IN ('generating-candidates', 'evaluating', 'completed', "
            "'failed', 'blocked', 'awaiting-review')",
            name="ck_label_opt_rounds_status",
        ),
    )
    op.create_index(
        "ix_label_opt_rounds_scope_status",
        "label_optimization_rounds",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_opt_rounds_trace_id",
        "label_optimization_rounds",
        ["trace_id"],
    )
    _immutable_triggers("label_optimization_metric_snapshots")


def downgrade() -> None:
    _drop_immutable_triggers("label_optimization_metric_snapshots")
    op.drop_table("label_optimization_rounds")
    op.drop_table("label_optimization_metric_snapshots")
    op.drop_table("label_optimization_schedules")
