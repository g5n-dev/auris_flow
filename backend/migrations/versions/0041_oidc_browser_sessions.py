"""add provisioned OIDC identities and opaque browser sessions

Revision ID: 0041_oidc_browser_sessions
Revises: 0040_label_recomputation_fact_sets
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0041_oidc_browser_sessions"
down_revision = "0040_label_recomputation_fact_sets"
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
    op.create_table(
        "user_security_states",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("disabled_at", utc_datetime_type(), nullable=True),
        sa.Column("authz_version", sa.Integer(), server_default="1", nullable=False),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_user_security_states_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'suspended')",
            name="ck_user_security_states_status",
        ),
        sa.CheckConstraint(
            "authz_version > 0",
            name="ck_user_security_states_authz_version",
        ),
        sa.CheckConstraint(
            "(status = 'active' AND disabled_at IS NULL) OR "
            "(status <> 'active' AND disabled_at IS NOT NULL)",
            name="ck_user_security_states_disabled_at",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO user_security_states "
            "(user_id, status, disabled_at, authz_version) "
            "SELECT user_id, 'active', NULL, 1 FROM users"
        )
    )

    op.create_table(
        "oidc_identities",
        sa.Column("identity_id", sa.String(128), primary_key=True),
        sa.Column("issuer", sa.String(512), nullable=False),
        sa.Column("issuer_sha256", sa.String(64), nullable=False),
        sa.Column("subject_sha256", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("last_login_at", utc_datetime_type(), nullable=True),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_oidc_identities_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_oidc_identities_tenant",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_oidc_identities_project",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.UniqueConstraint(
            "issuer_sha256",
            "subject_sha256",
            name="uq_oidc_identities_issuer_subject",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_oidc_identities_status",
        ),
    )
    op.create_index("ix_oidc_identities_user_id", "oidc_identities", ["user_id"])
    op.create_index(
        "ix_oidc_identities_scope",
        "oidc_identities",
        ["tenant_id", "project_id", "status"],
    )

    op.create_table(
        "oidc_authorization_states",
        sa.Column("state_sha256", sa.String(64), primary_key=True),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("return_path", sa.String(1024), server_default="/", nullable=False),
        sa.Column("issued_at", utc_datetime_type(), nullable=False),
        sa.Column("expires_at", utc_datetime_type(), nullable=False),
        sa.Column("consumed_at", utc_datetime_type(), nullable=True),
        *timestamp_columns(),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_oidc_authorization_states_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= issued_at",
            name="ck_oidc_authorization_states_consumed",
        ),
    )
    op.create_index(
        "ix_oidc_authorization_states_expiry",
        "oidc_authorization_states",
        ["expires_at", "consumed_at"],
    )

    op.create_table(
        "browser_auth_sessions",
        sa.Column("browser_session_id", sa.String(128), primary_key=True),
        sa.Column("token_sha256", sa.String(64), nullable=False),
        sa.Column("csrf_sha256", sa.String(64), nullable=False),
        sa.Column("oidc_identity_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), server_default="oidc_session", nullable=False),
        sa.Column("issued_at", utc_datetime_type(), nullable=False),
        sa.Column("expires_at", utc_datetime_type(), nullable=False),
        sa.Column("revoked_at", utc_datetime_type(), nullable=True),
        sa.Column("last_seen_at", utc_datetime_type(), nullable=False),
        *timestamp_columns(),
        sa.ForeignKeyConstraint(
            ["oidc_identity_id"],
            ["oidc_identities.identity_id"],
            name="fk_browser_auth_sessions_identity",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_browser_auth_sessions_user",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_browser_auth_sessions_tenant",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_browser_auth_sessions_project",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.UniqueConstraint(
            "token_sha256",
            name="uq_browser_auth_sessions_token_sha256",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_browser_auth_sessions_expiry",
        ),
        sa.CheckConstraint(
            "last_seen_at >= issued_at AND last_seen_at <= expires_at",
            name="ck_browser_auth_sessions_last_seen",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_browser_auth_sessions_revoked",
        ),
    )
    op.create_index(
        "ix_browser_auth_sessions_user_active",
        "browser_auth_sessions",
        ["user_id", "revoked_at", "expires_at"],
    )
    op.create_index(
        "ix_browser_auth_sessions_identity",
        "browser_auth_sessions",
        ["oidc_identity_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_auth_sessions_identity", table_name="browser_auth_sessions")
    op.drop_index("ix_browser_auth_sessions_user_active", table_name="browser_auth_sessions")
    op.drop_table("browser_auth_sessions")
    op.drop_index(
        "ix_oidc_authorization_states_expiry",
        table_name="oidc_authorization_states",
    )
    op.drop_table("oidc_authorization_states")
    op.drop_index("ix_oidc_identities_scope", table_name="oidc_identities")
    op.drop_index("ix_oidc_identities_user_id", table_name="oidc_identities")
    op.drop_table("oidc_identities")
    op.drop_table("user_security_states")
