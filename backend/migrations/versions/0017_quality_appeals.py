"""add the single-case quality appeal ledger

Revision ID: 0017_quality_appeals
Revises: 0016_outbox_reconciliation_state
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0017_quality_appeals"
down_revision = "0016_outbox_reconciliation_state"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    with op.batch_alter_table("human_review_decisions") as batch:
        batch.create_unique_constraint(
            "uq_human_review_decisions_scope_terminal_binding",
            ["tenant_id", "project_id", "decision_id", "terminal_review_task_id"],
        )

    op.create_table(
        "quality_appeals",
        sa.Column("appeal_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("source_decision_id", sa.String(128), nullable=False),
        sa.Column("source_review_task_id", sa.String(128), nullable=False),
        sa.Column("review_task_id", sa.String(128), nullable=False),
        sa.Column("appeal_decision_id", sa.String(128), nullable=True),
        sa.Column("source_result_sha256", sa.String(64), nullable=False),
        sa.Column("source_decider_id", sa.String(64), nullable=False),
        sa.Column("source_trace_id", sa.String(128), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        sa.Column("appellant_id", sa.String(64), nullable=False),
        sa.Column("evidence_refs", json_type(), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(32), server_default="submitted", nullable=False),
        sa.Column("reviewer_id", sa.String(64), nullable=True),
        sa.Column("decision", sa.String(32), nullable=True),
        sa.Column("decision_reason", sa.String(2000), nullable=True),
        sa.Column("withdrawal_reason", sa.String(1000), nullable=True),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("claimed_at", utc_datetime_type(), nullable=True),
        sa.Column("resolved_at", utc_datetime_type(), nullable=True),
        sa.Column("withdrawn_at", utc_datetime_type(), nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "appeal_id",
            name="uq_quality_appeals_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_decision_id",
            name="uq_quality_appeals_scope_source_decision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_task_id",
            name="uq_quality_appeals_scope_review_task",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "appeal_decision_id",
            name="uq_quality_appeals_scope_appeal_decision",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "source_decision_id",
                "source_review_task_id",
            ],
            [
                "human_review_decisions.tenant_id",
                "human_review_decisions.project_id",
                "human_review_decisions.decision_id",
                "human_review_decisions.terminal_review_task_id",
            ],
            name="fk_quality_appeals_scope_terminal_decision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "review_task_id"],
            [
                "human_review_tasks.tenant_id",
                "human_review_tasks.project_id",
                "human_review_tasks.review_task_id",
            ],
            name="fk_quality_appeals_scope_review_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "appeal_decision_id", "review_task_id"],
            [
                "human_review_decisions.tenant_id",
                "human_review_decisions.project_id",
                "human_review_decisions.decision_id",
                "human_review_decisions.terminal_review_task_id",
            ],
            name="fk_quality_appeals_scope_appeal_decision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'under_review', 'resolved', 'withdrawn')",
            name="ck_quality_appeals_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN "
            "('original_upheld', 'original_overturned', 'original_remanded')",
            name="ck_quality_appeals_decision",
        ),
        sa.CheckConstraint(
            "(status = 'resolved' AND decision IS NOT NULL AND resolved_at IS NOT NULL "
            "AND appeal_decision_id IS NOT NULL) OR "
            "(status <> 'resolved' AND decision IS NULL AND resolved_at IS NULL "
            "AND appeal_decision_id IS NULL)",
            name="ck_quality_appeals_resolution_state",
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL) OR "
            "(status <> 'withdrawn' AND withdrawn_at IS NULL)",
            name="ck_quality_appeals_withdrawal_state",
        ),
    )
    op.create_index(
        "ix_quality_appeals_scope_status",
        "quality_appeals",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_quality_appeals_scope_appellant",
        "quality_appeals",
        ["tenant_id", "project_id", "appellant_id"],
    )
    op.create_index(
        "ix_quality_appeals_scope_reviewer",
        "quality_appeals",
        ["tenant_id", "project_id", "reviewer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_quality_appeals_scope_reviewer", table_name="quality_appeals")
    op.drop_index("ix_quality_appeals_scope_appellant", table_name="quality_appeals")
    op.drop_index("ix_quality_appeals_scope_status", table_name="quality_appeals")
    op.drop_table("quality_appeals")

    with op.batch_alter_table("human_review_decisions") as batch:
        batch.drop_constraint(
            "uq_human_review_decisions_scope_terminal_binding",
            type_="unique",
        )
