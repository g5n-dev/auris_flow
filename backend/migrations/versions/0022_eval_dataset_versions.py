"""add immutable evaluation dataset version snapshots

Revision ID: 0022_eval_dataset_versions
Revises: 0021_hotword_governance
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0022_eval_dataset_versions"
down_revision = "0021_hotword_governance"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "eval_dataset_versions",
        sa.Column("eval_dataset_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("manifest_storage_object_id", sa.String(128), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        sa.Column("locked_at", utc_datetime_type(), nullable=True),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("created_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_dataset_id",
            name="uq_eval_dataset_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            "dataset_version",
            name="uq_eval_dataset_versions_scope_name_version",
        ),
        sa.CheckConstraint(
            "sample_count > 0",
            name="ck_eval_dataset_versions_sample_count",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_eval_dataset_versions_resource_version",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'locked', 'deprecated', 'archived')",
            name="ck_eval_dataset_versions_status",
        ),
    )
    op.create_index(
        "ix_eval_dataset_versions_scope_status",
        "eval_dataset_versions",
        ["tenant_id", "project_id", "capability", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_eval_dataset_versions_scope_status",
        table_name="eval_dataset_versions",
    )
    op.drop_table("eval_dataset_versions")
