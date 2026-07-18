"""add asset lineage edges table

Revision ID: 0007_asset_lineage_edges_table
Revises: 0006_knowledge_effects_table
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_asset_lineage_edges_table"
down_revision = "0006_knowledge_effects_table"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "asset_lineage_edges",
        sa.Column("edge_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
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
            "edge_id",
            name="uq_asset_lineage_edges_scope",
        ),
    )
    op.create_index(
        "ix_asset_lineage_edges_scope_status",
        "asset_lineage_edges",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_asset_lineage_edges_trace", "asset_lineage_edges", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_asset_lineage_edges_trace", table_name="asset_lineage_edges")
    op.drop_index("ix_asset_lineage_edges_scope_status", table_name="asset_lineage_edges")
    op.drop_table("asset_lineage_edges")
