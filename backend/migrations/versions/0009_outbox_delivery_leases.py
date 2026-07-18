"""add leased outbox delivery and fencing metadata

Revision ID: 0009_outbox_delivery_leases
Revises: 0008_storage_objects_table
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0009_outbox_delivery_leases"
down_revision = "0008_storage_objects_table"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def lease_generation_type() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("dispatch_idempotency_key", sa.String(128), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("dispatch_request_sha256", sa.String(64), nullable=True),
    )
    op.add_column("outbox_events", sa.Column("claim_token", sa.String(64), nullable=True))
    op.add_column("outbox_events", sa.Column("claimed_by", sa.String(128), nullable=True))
    op.add_column(
        "outbox_events",
        sa.Column("claimed_at", utc_datetime_type(), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "lease_generation",
            lease_generation_type(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column("lease_expires_at", utc_datetime_type(), nullable=True),
    )

    connection = op.get_bind()
    if connection.dialect.name == "mysql":
        connection.execute(
            sa.text(
                "UPDATE outbox_events SET dispatch_idempotency_key = "
                "CONCAT('outbox_legacy_', LPAD(event_id, 20, '0')) "
                "WHERE dispatch_idempotency_key IS NULL"
            )
        )
    else:
        connection.execute(
            sa.text(
                "UPDATE outbox_events SET dispatch_idempotency_key = "
                "printf('outbox_legacy_%020d', event_id) "
                "WHERE dispatch_idempotency_key IS NULL"
            )
        )

    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.alter_column(
            "dispatch_idempotency_key",
            existing_type=sa.String(128),
            nullable=False,
        )

    op.drop_index("ix_outbox_events_status", table_name="outbox_events")
    op.create_index(
        "uq_outbox_events_dispatch_idempotency_key",
        "outbox_events",
        ["dispatch_idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_outbox_events_claim",
        "outbox_events",
        ["status", "available_at", "event_id"],
    )
    op.create_index(
        "ix_outbox_events_reclaim",
        "outbox_events",
        ["status", "lease_expires_at", "event_id"],
    )
    op.create_index(
        "ix_outbox_events_scope_aggregate",
        "outbox_events",
        ["tenant_id", "project_id", "aggregate_id", "event_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_scope_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_reclaim", table_name="outbox_events")
    op.drop_index("ix_outbox_events_claim", table_name="outbox_events")
    op.drop_index("uq_outbox_events_dispatch_idempotency_key", table_name="outbox_events")
    op.create_index("ix_outbox_events_status", "outbox_events", ["status"])
    with op.batch_alter_table("outbox_events") as batch_op:
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_generation")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claimed_by")
        batch_op.drop_column("claim_token")
        batch_op.drop_column("dispatch_request_sha256")
        batch_op.drop_column("dispatch_idempotency_key")
