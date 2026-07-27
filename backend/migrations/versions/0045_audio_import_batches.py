"""add durable platform audio import batches and items

Revision ID: 0045_audio_import_batches
Revises: 0044_oidc_backchannel_logout
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0045_audio_import_batches"
down_revision = "0044_oidc_backchannel_logout"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.create_table(
        "import_batches",
        sa.Column("import_batch_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("task_run_id", sa.String(128), nullable=False),
        sa.Column("task_version_id", sa.String(128), nullable=False),
        sa.Column("connector_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("current_stage", sa.String(32), server_default="queued", nullable=False),
        sa.Column("total_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("succeeded_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("skipped_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_items", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cursor_before", sa.String(1024), nullable=True),
        sa.Column("cursor_after", sa.String(1024), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("started_at", utc_datetime_type(), nullable=True),
        sa.Column("finished_at", utc_datetime_type(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "import_batch_id",
            name="uq_import_batches_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "task_run_id",
            name="uq_import_batches_scope_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "task_run_id"],
            [
                "run_records.tenant_id",
                "run_records.project_id",
                "run_records.run_id",
            ],
            name="fk_import_batches_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled')",
            name="ck_import_batches_status",
        ),
        sa.CheckConstraint(
            "current_stage IN "
            "('queued', 'listing', 'downloading', 'verifying', 'materializing', 'completed')",
            name="ck_import_batches_current_stage",
        ),
        sa.CheckConstraint(
            "total_items >= 0 AND succeeded_items >= 0 AND skipped_items >= 0 "
            "AND failed_items >= 0",
            name="ck_import_batches_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "succeeded_items + skipped_items + failed_items <= total_items",
            name="ck_import_batches_count_bounds",
        ),
    )
    op.create_index(
        "ix_import_batches_scope_status_created",
        "import_batches",
        ["tenant_id", "project_id", "status", "created_at"],
    )
    op.create_index(
        "ix_import_batches_scope_task_version",
        "import_batches",
        ["tenant_id", "project_id", "task_version_id", "created_at"],
    )
    op.create_index(
        "ix_import_batches_root_trace_id",
        "import_batches",
        ["root_trace_id"],
    )
    op.create_index(
        "ix_import_batches_trace_id",
        "import_batches",
        ["trace_id"],
    )

    op.create_table(
        "import_batch_items",
        sa.Column("import_item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("import_batch_id", sa.String(128), nullable=False),
        sa.Column("external_record_id", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("object_version", sa.String(512), nullable=True),
        sa.Column("audio_session_id", sa.String(128), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "import_batch_id",
            "external_record_id",
            name="uq_import_batch_items_scope_external",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "import_batch_id"],
            [
                "import_batches.tenant_id",
                "import_batches.project_id",
                "import_batches.import_batch_id",
            ],
            name="fk_import_batch_items_scope_batch",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'skipped', 'failed')",
            name="ck_import_batch_items_status",
        ),
    )
    op.create_index(
        "ix_import_batch_items_scope_batch_status",
        "import_batch_items",
        ["tenant_id", "project_id", "import_batch_id", "status"],
    )
    op.create_index(
        "ix_import_batch_items_scope_audio_session",
        "import_batch_items",
        ["tenant_id", "project_id", "audio_session_id"],
    )
    op.create_index(
        "ix_import_batch_items_root_trace_id",
        "import_batch_items",
        ["root_trace_id"],
    )
    op.create_index(
        "ix_import_batch_items_trace_id",
        "import_batch_items",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_table("import_batch_items")
    op.drop_table("import_batches")
