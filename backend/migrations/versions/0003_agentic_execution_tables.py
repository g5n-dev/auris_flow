"""add agentic execution tables

Revision ID: 0003_agentic_execution_tables
Revises: 0002_domain_baseline_tables
Create Date: 2026-07-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_agentic_execution_tables"
down_revision = "0002_domain_baseline_tables"
branch_labels = None
depends_on = None


AGENTIC_TABLES: tuple[tuple[str, str], ...] = (
    ("agent_runs", "agent_run_id"),
    ("tool_calls", "tool_call_id"),
    ("agent_decisions", "decision_id"),
    ("trace_refs", "trace_ref_id"),
)


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def create_agentic_table(table_name: str, primary_key: str) -> None:
    op.create_table(
        table_name,
        sa.Column(primary_key, sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
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
    for table_name, primary_key in AGENTIC_TABLES:
        create_agentic_table(table_name, primary_key)


def downgrade() -> None:
    for table_name, _primary_key in reversed(AGENTIC_TABLES):
        op.drop_index(f"ix_{table_name}_trace", table_name=table_name)
        op.drop_index(f"ix_{table_name}_scope_status", table_name=table_name)
        op.drop_table(table_name)
