"""add domain baseline tables

Revision ID: 0002_domain_baseline_tables
Revises: 0001_core_tables
Create Date: 2026-07-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_domain_baseline_tables"
down_revision = "0001_core_tables"
branch_labels = None
depends_on = None


DOMAIN_TABLES: tuple[tuple[str, str], ...] = (
    ("task_versions", "task_version_id"),
    ("task_run_steps", "task_run_step_id"),
    ("audio_sessions", "audio_session_id"),
    ("audio_recordings", "recording_id"),
    ("conversation_boundaries", "boundary_id"),
    ("evidence_packs", "evidence_pack_id"),
    ("label_versions", "label_version_id"),
    ("label_candidates", "candidate_id"),
    ("label_conflicts", "conflict_id"),
    ("human_review_tasks", "review_task_id"),
    ("human_review_decisions", "decision_id"),
    ("knowledge_sources", "knowledge_source_id"),
    ("knowledge_chunks", "chunk_id"),
    ("knowledge_indexes", "knowledge_index_id"),
    ("knowledge_quality_gates", "knowledge_gate_id"),
    ("eval_datasets", "eval_dataset_id"),
    ("eval_cases", "eval_case_id"),
    ("eval_runs", "eval_run_id"),
    ("metric_results", "metric_result_id"),
    ("badcases", "badcase_id"),
    ("data_assets", "data_asset_id"),
    ("asset_partitions", "asset_partition_id"),
    ("asset_materializations", "materialization_id"),
    ("external_callback_receipts", "callback_receipt_id"),
)


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def create_domain_table(table_name: str, primary_key: str) -> None:
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
    for table_name, primary_key in DOMAIN_TABLES:
        create_domain_table(table_name, primary_key)


def downgrade() -> None:
    for table_name, _primary_key in reversed(DOMAIN_TABLES):
        op.drop_index(f"ix_{table_name}_trace", table_name=table_name)
        op.drop_index(f"ix_{table_name}_scope_status", table_name=table_name)
        op.drop_table(table_name)
