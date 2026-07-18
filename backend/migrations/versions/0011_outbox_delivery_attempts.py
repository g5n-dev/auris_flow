"""add append-only outbox delivery attempt ledger

Revision ID: 0011_outbox_delivery_attempts
Revises: 0010_label_policy_engine
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0011_outbox_delivery_attempts"
down_revision = "0010_label_policy_engine"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def lease_generation_type() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "outbox_delivery_attempts",
        sa.Column("attempt_id", sa.String(128), primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("outbox_events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("lease_generation", lease_generation_type(), nullable=False),
        sa.Column("claimed_by", sa.String(128), nullable=False),
        sa.Column("claim_token_sha256", sa.String(64), nullable=False),
        sa.Column("delivery_mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dispatch_idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=True),
        sa.Column("adapter", sa.String(64), nullable=True),
        sa.Column("operation", sa.String(128), nullable=True),
        sa.Column("remote_id", sa.String(256), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("error_message", sa.String(1024), nullable=True),
        sa.Column(
            "started_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", utc_datetime_type(), nullable=True),
        sa.Column("details", json_type(), nullable=False),
        sa.UniqueConstraint(
            "event_id",
            "lease_generation",
            name="uq_outbox_delivery_attempts_event_generation",
        ),
    )
    op.create_index(
        "ix_outbox_delivery_attempts_scope_event",
        "outbox_delivery_attempts",
        ["tenant_id", "project_id", "event_id"],
    )
    op.create_index(
        "ix_outbox_delivery_attempts_status_started",
        "outbox_delivery_attempts",
        ["status", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_delivery_attempts_status_started",
        table_name="outbox_delivery_attempts",
    )
    op.drop_index(
        "ix_outbox_delivery_attempts_scope_event",
        table_name="outbox_delivery_attempts",
    )
    op.drop_table("outbox_delivery_attempts")
