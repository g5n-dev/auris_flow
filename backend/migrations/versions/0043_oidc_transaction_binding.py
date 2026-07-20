"""bind OIDC authorization states to the initiating browser

Revision ID: 0043_oidc_transaction_binding
Revises: 0042_task_run_control_plane
Create Date: 2026-07-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_oidc_transaction_binding"
down_revision = "0042_task_run_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("oidc_authorization_states") as batch:
        batch.add_column(sa.Column("transaction_sha256", sa.String(64), nullable=True))

    # Existing authorization attempts predate browser binding. Invalidate them
    # instead of allowing a compatibility path that would reintroduce login CSRF.
    # The existing state hash is unique and is used only as a non-secret tombstone.
    op.execute(
        sa.text(
            "UPDATE oidc_authorization_states "
            "SET transaction_sha256 = state_sha256, "
            "consumed_at = COALESCE(consumed_at, issued_at)"
        )
    )

    with op.batch_alter_table("oidc_authorization_states") as batch:
        batch.alter_column(
            "transaction_sha256",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.create_unique_constraint(
            "uq_oidc_authorization_states_transaction",
            ["transaction_sha256"],
        )
        batch.create_index(
            "ix_oidc_authorization_states_transaction_pending",
            ["transaction_sha256", "consumed_at", "expires_at"],
            unique=False,
        )


def downgrade() -> None:
    # 0042 has no browser transaction binding.  Invalidate every state created
    # while 0043 was active before removing the binding column; otherwise a
    # rollback would turn a browser-bound pending state into a bearer-style
    # state that the older runtime could consume with the public value alone.
    op.execute(
        sa.text(
            "UPDATE oidc_authorization_states SET consumed_at = COALESCE(consumed_at, issued_at)"
        )
    )
    with op.batch_alter_table("oidc_authorization_states") as batch:
        batch.drop_index("ix_oidc_authorization_states_transaction_pending")
        batch.drop_constraint(
            "uq_oidc_authorization_states_transaction",
            type_="unique",
        )
        batch.drop_column("transaction_sha256")
