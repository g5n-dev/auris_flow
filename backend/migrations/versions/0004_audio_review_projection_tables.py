"""add audio review projection tables

Revision ID: 0004_audio_review_projection_tables
Revises: 0003_agentic_execution_tables
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_audio_review_projection_tables"
down_revision = "0003_agentic_execution_tables"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def create_projection_table(
    table_name: str, primary_key: str, domain_column: tuple[str, int] | None = None
) -> None:
    domain_columns: list[sa.Column] = []
    if domain_column:
        column_name, size = domain_column
        domain_columns.append(sa.Column(column_name, sa.String(size), nullable=False))
    op.create_table(
        table_name,
        sa.Column(primary_key, sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        *domain_columns,
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
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
            primary_key,
            name=f"uq_{table_name}_scope",
        ),
    )
    op.create_index(
        f"ix_{table_name}_scope_status",
        table_name,
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(f"ix_{table_name}_trace", table_name, ["trace_id"])


def upgrade() -> None:
    create_projection_table("listening_annotations", "annotation_id", ("audio_session_id", 128))
    op.create_index(
        "ix_listening_annotations_session",
        "listening_annotations",
        ["tenant_id", "project_id", "audio_session_id"],
    )
    create_projection_table("voiceprint_enrollments", "enrollment_id", ("voiceprint_id", 128))
    op.create_index(
        "ix_voiceprint_enrollments_voiceprint",
        "voiceprint_enrollments",
        ["tenant_id", "project_id", "voiceprint_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_voiceprint_enrollments_voiceprint", table_name="voiceprint_enrollments")
    op.drop_index("ix_voiceprint_enrollments_trace", table_name="voiceprint_enrollments")
    op.drop_index("ix_voiceprint_enrollments_scope_status", table_name="voiceprint_enrollments")
    op.drop_table("voiceprint_enrollments")
    op.drop_index("ix_listening_annotations_session", table_name="listening_annotations")
    op.drop_index("ix_listening_annotations_trace", table_name="listening_annotations")
    op.drop_index("ix_listening_annotations_scope_status", table_name="listening_annotations")
    op.drop_table("listening_annotations")
