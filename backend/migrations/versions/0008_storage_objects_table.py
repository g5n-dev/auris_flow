"""add governed storage object metadata

Revision ID: 0008_storage_objects_table
Revises: 0007_asset_lineage_edges_table
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_storage_objects_table"
down_revision = "0007_asset_lineage_edges_table"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "storage_objects",
        sa.Column("storage_object_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.String(1024), nullable=False),
        sa.Column("object_key_sha256", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="registered"),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "provider",
            "bucket",
            "object_key_sha256",
            name="uq_storage_objects_scope_locator",
        ),
    )
    op.create_index(
        "ix_storage_objects_scope_status",
        "storage_objects",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_storage_objects_scope_source",
        "storage_objects",
        ["tenant_id", "project_id", "source_type", "source_id"],
    )
    op.create_index("ix_storage_objects_trace_id", "storage_objects", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_storage_objects_trace_id", table_name="storage_objects")
    op.drop_index("ix_storage_objects_scope_source", table_name="storage_objects")
    op.drop_index("ix_storage_objects_scope_status", table_name="storage_objects")
    op.drop_table("storage_objects")
