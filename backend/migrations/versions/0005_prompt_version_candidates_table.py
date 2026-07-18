"""add prompt version candidate table

Revision ID: 0005_prompt_version_candidates_table
Revises: 0004_audio_review_projection_tables
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_prompt_version_candidates_table"
down_revision = "0004_audio_review_projection_tables"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "prompt_version_candidates",
        sa.Column("candidate_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
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
            "candidate_id",
            name="uq_prompt_version_candidates_scope",
        ),
    )
    op.create_index(
        "ix_prompt_version_candidates_scope_status",
        "prompt_version_candidates",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_prompt_version_candidates_trace", "prompt_version_candidates", ["trace_id"])


def downgrade() -> None:
    op.drop_index("ix_prompt_version_candidates_trace", table_name="prompt_version_candidates")
    op.drop_index(
        "ix_prompt_version_candidates_scope_status",
        table_name="prompt_version_candidates",
    )
    op.drop_table("prompt_version_candidates")
