"""separate remote delivery outcome from reconciliation attempts

Revision ID: 0016_outbox_reconciliation_state
Revises: 0015_idempotency_and_completion_receipts
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_outbox_reconciliation_state"
down_revision = "0015_idempotency_and_completion_receipts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column(
            "reconcile_attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "outbox_events",
        sa.Column(
            "delivery_state",
            sa.String(32),
            nullable=False,
            server_default="ready",
        ),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "delivery_state")
    op.drop_column("outbox_events", "reconcile_attempt_count")
