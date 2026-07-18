"""add revocable authentication sessions

Revision ID: 0020_auth_sessions
Revises: 0019_calibration_hardening
Create Date: 2026-07-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0020_auth_sessions"
down_revision = "0019_calibration_hardening"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("issued_at", utc_datetime_type(), nullable=False),
        sa.Column("expires_at", utc_datetime_type(), nullable=False),
        sa.Column("revoked_at", utc_datetime_type(), nullable=True),
        sa.Column("last_seen_at", utc_datetime_type(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_auth_sessions_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_auth_sessions_tenant",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("expires_at > issued_at", name="ck_auth_sessions_expiry"),
        sa.CheckConstraint(
            "last_seen_at >= issued_at AND last_seen_at <= expires_at",
            name="ck_auth_sessions_last_seen",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR (revoked_at >= issued_at AND revoked_at <= expires_at)",
            name="ck_auth_sessions_revoked",
        ),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_tenant_id", "auth_sessions", ["tenant_id"])
    op.create_index(
        "ix_auth_sessions_tenant_user_active",
        "auth_sessions",
        ["tenant_id", "user_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_auth_sessions_provider_expires",
        "auth_sessions",
        ["provider", "expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.drop_constraint("fk_auth_sessions_user", "auth_sessions", type_="foreignkey")
        op.drop_constraint("fk_auth_sessions_tenant", "auth_sessions", type_="foreignkey")
    op.drop_index("ix_auth_sessions_provider_expires", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_tenant_user_active", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_tenant_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
