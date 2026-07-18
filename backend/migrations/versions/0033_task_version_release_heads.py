"""add scoped task version production release heads

Revision ID: 0033_task_version_release_heads
Revises: 0032_controlled_experiments
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0033_task_version_release_heads"
down_revision = "0032_controlled_experiments"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "task_version_release_heads",
        sa.Column("release_head_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("task_type_id", sa.String(128), nullable=False),
        sa.Column("release_channel", sa.String(32), server_default="production", nullable=False),
        sa.Column("active_task_version_id", sa.String(128), nullable=False),
        sa.Column("active_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("previous_task_version_id", sa.String(128)),
        sa.Column("generation", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("activated_by_run_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "task_type_id",
            "release_channel",
            name="uq_task_version_release_heads_scope_channel",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_task_version_release_heads_generation",
        ),
        sa.CheckConstraint(
            "status = 'active'",
            name="ck_task_version_release_heads_status",
        ),
    )
    op.create_index(
        "ix_task_version_release_heads_scope_active",
        "task_version_release_heads",
        [
            "tenant_id",
            "project_id",
            "task_type_id",
            "release_channel",
            "active_task_version_id",
        ],
    )
    op.create_index(
        "ix_task_version_release_heads_trace_id",
        "task_version_release_heads",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_version_release_heads_trace_id",
        table_name="task_version_release_heads",
    )
    op.drop_index(
        "ix_task_version_release_heads_scope_active",
        table_name="task_version_release_heads",
    )
    op.drop_table("task_version_release_heads")
