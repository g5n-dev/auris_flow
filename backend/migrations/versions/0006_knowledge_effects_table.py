"""add knowledge effects table

Revision ID: 0006_knowledge_effects_table
Revises: 0005_prompt_version_candidates_table
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_knowledge_effects_table"
down_revision = "0005_prompt_version_candidates_table"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "knowledge_effects",
        sa.Column("effect_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="success"),
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
            "effect_id",
            name="uq_knowledge_effects_scope",
        ),
    )
    op.create_index(
        "ix_knowledge_effects_scope_status",
        "knowledge_effects",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_knowledge_effects_trace", "knowledge_effects", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_effects_trace", table_name="knowledge_effects")
    op.drop_index("ix_knowledge_effects_scope_status", table_name="knowledge_effects")
    op.drop_table("knowledge_effects")
