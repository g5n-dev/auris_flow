"""expand run records for reliable task-run controls

Revision ID: 0042_task_run_control_plane
Revises: 0041_oidc_browser_sessions
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0042_task_run_control_plane"
down_revision = "0041_oidc_browser_sessions"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    # Expand-only: nullable control columns avoid rewriting existing rows.
    with op.batch_alter_table("run_records") as batch:
        batch.add_column(sa.Column("submitted_at", utc_datetime_type(), nullable=True))
        batch.add_column(sa.Column("started_at", utc_datetime_type(), nullable=True))
        batch.add_column(sa.Column("finished_at", utc_datetime_type(), nullable=True))
        batch.add_column(sa.Column("deadline_at", utc_datetime_type(), nullable=True))
        batch.add_column(sa.Column("next_status_sync_at", utc_datetime_type(), nullable=True))
        batch.add_column(
            sa.Column("monitor_generation", sa.Integer(), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("engine_status", sa.String(32), nullable=True))
        batch.add_column(sa.Column("engine_status_observed_at", utc_datetime_type(), nullable=True))
        batch.add_column(
            sa.Column("status_version", sa.Integer(), server_default="1", nullable=False)
        )
        batch.add_column(sa.Column("cancel_requested_at", utc_datetime_type(), nullable=True))
        batch.add_column(sa.Column("cancel_reason", sa.String(500), nullable=True))
        batch.add_column(sa.Column("terminal_reason", sa.String(500), nullable=True))
        batch.create_index(
            "ix_run_records_status_deadline", ["status", "deadline_at"], unique=False
        )
        batch.create_index(
            "ix_run_records_status_sync_due", ["status", "next_status_sync_at"], unique=False
        )
        batch.create_index(
            "ix_run_records_monitor_deadline",
            ["run_type", "status", "deadline_at"],
            unique=False,
        )
        batch.create_index(
            "ix_run_records_monitor_sync_due",
            ["run_type", "status", "next_status_sync_at"],
            unique=False,
        )
        batch.create_index(
            "ix_run_records_monitor_control_active",
            ["tenant_id", "project_id", "run_key", "run_type", "status"],
            unique=False,
            mysql_length={"run_key": 128},
        )
        batch.create_index(
            "ix_run_records_type_status_finished",
            ["run_type", "status", "finished_at"],
            unique=False,
        )
        batch.create_index("ix_run_records_engine_status", ["engine_status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("run_records") as batch:
        batch.drop_index("ix_run_records_engine_status")
        batch.drop_index("ix_run_records_type_status_finished")
        batch.drop_index("ix_run_records_monitor_control_active")
        batch.drop_index("ix_run_records_monitor_sync_due")
        batch.drop_index("ix_run_records_monitor_deadline")
        batch.drop_index("ix_run_records_status_sync_due")
        batch.drop_index("ix_run_records_status_deadline")
        batch.drop_column("terminal_reason")
        batch.drop_column("cancel_reason")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("status_version")
        batch.drop_column("engine_status_observed_at")
        batch.drop_column("engine_status")
        batch.drop_column("monitor_generation")
        batch.drop_column("next_status_sync_at")
        batch.drop_column("deadline_at")
        batch.drop_column("finished_at")
        batch.drop_column("started_at")
        batch.drop_column("submitted_at")
