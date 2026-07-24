"""add durable OIDC back-channel logout replay and session binding

Revision ID: 0044_oidc_backchannel_logout
Revises: 0043_oidc_transaction_binding
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0044_oidc_backchannel_logout"
down_revision = "0043_oidc_transaction_binding"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    with op.batch_alter_table("browser_auth_sessions") as batch:
        batch.add_column(
            sa.Column("oidc_session_id_sha256", sa.String(64), nullable=True)
        )
        batch.create_index(
            "ix_browser_auth_sessions_oidc_sid_active",
            ["oidc_session_id_sha256", "revoked_at", "expires_at"],
            unique=False,
        )

    op.create_table(
        "oidc_logout_token_replays",
        sa.Column("logout_event_sha256", sa.String(64), primary_key=True),
        sa.Column("issuer_sha256", sa.String(64), nullable=False),
        sa.Column("jti_sha256", sa.String(64), nullable=False),
        sa.Column("issued_at", utc_datetime_type(), nullable=False),
        sa.Column("expires_at", utc_datetime_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "issuer_sha256",
            "jti_sha256",
            name="uq_oidc_logout_token_replays_issuer_jti",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_oidc_logout_token_replays_expiry",
        ),
    )
    op.create_index(
        "ix_oidc_logout_token_replays_expiry",
        "oidc_logout_token_replays",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_table("oidc_logout_token_replays")
    with op.batch_alter_table("browser_auth_sessions") as batch:
        batch.drop_index("ix_browser_auth_sessions_oidc_sid_active")
        batch.drop_column("oidc_session_id_sha256")
