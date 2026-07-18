"""make idempotency atomic and persist completion receipt inbox

Revision ID: 0015_idempotency_and_completion_receipts
Revises: 0014_insight_causal_foreign_keys
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0015_idempotency_and_completion_receipts"
down_revision = "0014_insight_causal_foreign_keys"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def _assert_idempotency_scope_is_migratable() -> None:
    records = sa.table(
        "idempotency_records",
        sa.column("tenant_id", sa.String(64)),
        sa.column("project_id", sa.String(64)),
        sa.column("operation", sa.String(128)),
        sa.column("idempotency_key", sa.String(128)),
    )
    collision = (
        op.get_bind()
        .execute(
            sa.select(
                records.c.tenant_id,
                records.c.project_id,
                records.c.operation,
                records.c.idempotency_key,
            )
            .group_by(
                records.c.tenant_id,
                records.c.project_id,
                records.c.operation,
                records.c.idempotency_key,
            )
            .having(sa.func.count() > 1)
            .limit(1)
        )
        .first()
    )
    if collision is not None:
        scope = "/".join(str(value) for value in collision)
        raise RuntimeError(
            "idempotency scope collision must be resolved before migration 0015: " + scope
        )


def upgrade() -> None:
    _assert_idempotency_scope_is_migratable()
    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_constraint("uq_idempotency_scope", type_="unique")
        batch.add_column(
            sa.Column(
                "state",
                sa.String(16),
                server_default="completed",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("owner_token", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "updated_at",
                utc_datetime_type(),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
        batch.create_unique_constraint(
            "uq_idempotency_scope",
            ["tenant_id", "project_id", "operation", "idempotency_key"],
        )

    op.create_table(
        "run_completion_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("completion_receipt_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.Column(
            "processing_state",
            sa.String(16),
            server_default="processing",
            nullable=False,
        ),
        sa.Column("processing_token", sa.String(64), nullable=False),
        sa.Column("completion_status", sa.String(32), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("adapter", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("external_id", sa.String(256), nullable=True),
        sa.Column("request_body", json_type(), nullable=False),
        sa.Column("response_json", json_type(), nullable=True),
        sa.Column("signature_key_id", sa.String(128), nullable=True),
        sa.Column("authenticated_source", sa.String(128), nullable=True),
        sa.Column("signature_nonce", sa.String(128), nullable=True),
        sa.Column("signature_request_hash", sa.String(64), nullable=True),
        sa.Column("signature_body_hash", sa.String(64), nullable=True),
        sa.Column("signature_mode", sa.String(64), nullable=True),
        sa.Column("signed_at", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("request_trace_id", sa.String(128), nullable=False),
        sa.Column("run_trace_id", sa.String(128), nullable=False),
        sa.Column(
            "received_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", utc_datetime_type(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "completion_receipt_id",
            name="uq_run_completion_receipts_scope_receipt",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "run_id",
            name="uq_run_completion_receipts_scope_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_run_completion_receipts_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "ix_run_completion_receipts_scope_received",
        "run_completion_receipts",
        ["tenant_id", "project_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_completion_receipts_scope_received",
        table_name="run_completion_receipts",
    )
    op.drop_table("run_completion_receipts")

    with op.batch_alter_table("idempotency_records") as batch:
        batch.drop_constraint("uq_idempotency_scope", type_="unique")
        batch.drop_column("updated_at")
        batch.drop_column("owner_token")
        batch.drop_column("state")
        batch.create_unique_constraint(
            "uq_idempotency_scope",
            ["tenant_id", "project_id", "user_id", "operation", "idempotency_key"],
        )
