"""create core auris flow tables

Revision ID: 0001_core_tables
Revises:
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_core_tables"
down_revision = None
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("tenant_code", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("data", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("data", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "users",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("roles", json_type(), nullable=False),
        sa.Column("data", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "json_resources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("collection", sa.String(80), nullable=False),
        sa.Column("resource_key", sa.String(512), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=True, index=True),
        sa.Column("trace_id", sa.String(128), nullable=True, index=True),
        sa.Column("data", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "collection",
            "resource_key",
            name="uq_json_resources_scope_key",
        ),
    )
    op.create_index(
        "ix_json_resources_collection_status", "json_resources", ["collection", "status"]
    )
    op.create_table(
        "run_records",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("run_type", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("run_key", sa.String(512), nullable=True),
        sa.Column("partition_key", sa.String(512), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False, index=True),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_json", json_type(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "user_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )
    op.create_table(
        "audit_logs",
        sa.Column("audit_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("object_type", sa.String(80), nullable=False),
        sa.Column("object_id", sa.String(256), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("before_json", json_type(), nullable=True),
        sa.Column("after_json", json_type(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        sa.Column("project_id", sa.String(64), nullable=False, index=True),
        sa.Column("event_type", sa.String(128), nullable=False, index=True),
        sa.Column("aggregate_type", sa.String(80), nullable=False),
        sa.Column("aggregate_id", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", index=True),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(1024), nullable=True),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("outbox_events")
    op.drop_table("audit_logs")
    op.drop_table("idempotency_records")
    op.drop_table("run_records")
    op.drop_index("ix_json_resources_collection_status", table_name="json_resources")
    op.drop_table("json_resources")
    op.drop_table("users")
    op.drop_table("projects")
    op.drop_table("tenants")
