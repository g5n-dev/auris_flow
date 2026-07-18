"""add governed label policy artifacts and evaluations

Revision ID: 0010_label_policy_engine
Revises: 0009_outbox_delivery_leases
Create Date: 2026-07-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_label_policy_engine"
down_revision = "0009_outbox_delivery_leases"
branch_labels = None
depends_on = None


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    with op.batch_alter_table("label_versions") as batch_op:
        batch_op.add_column(
            sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("policy_version_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("release_gate_id", sa.String(128), nullable=True))
    op.create_index("ix_label_versions_policy_version_id", "label_versions", ["policy_version_id"])
    op.create_index("ix_label_versions_release_gate_id", "label_versions", ["release_gate_id"])

    with op.batch_alter_table("label_candidates") as batch_op:
        batch_op.add_column(
            sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1")
        )

    op.create_table(
        "label_policy_versions",
        sa.Column("policy_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("policy_key", sa.String(96), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("dsl_version", sa.String(16), nullable=False),
        sa.Column("policy_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="validated"),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("canonical_sha256", sa.String(64), nullable=False),
        sa.Column("compiler_version", sa.String(64), nullable=False),
        sa.Column("source_json", json_type(), nullable=False),
        sa.Column("canonical_json", json_type(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "canonical_sha256",
            name="uq_label_policy_versions_scope_artifact",
        ),
    )
    op.create_index(
        "ix_label_policy_versions_scope_status",
        "label_policy_versions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_policy_versions_trace_id",
        "label_policy_versions",
        ["trace_id"],
    )

    op.create_table(
        "label_policy_evaluations",
        sa.Column("evaluation_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=True),
        sa.Column("policy_version_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="evaluated"),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("policy_sha256", sa.String(64), nullable=False),
        sa.Column("facts_sha256", sa.String(64), nullable=False),
        sa.Column("decision_sha256", sa.String(64), nullable=False),
        sa.Column("facts_json", json_type(), nullable=False),
        sa.Column("decision_json", json_type(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "target_type",
            "target_id",
            "policy_version_id",
            "facts_sha256",
            name="uq_label_policy_evaluations_replay",
        ),
    )
    op.create_index(
        "ix_label_policy_evaluations_scope_verdict",
        "label_policy_evaluations",
        ["tenant_id", "project_id", "verdict"],
    )
    op.create_index(
        "ix_label_policy_evaluations_trace_id",
        "label_policy_evaluations",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_label_policy_evaluations_trace_id", table_name="label_policy_evaluations")
    op.drop_index(
        "ix_label_policy_evaluations_scope_verdict", table_name="label_policy_evaluations"
    )
    op.drop_table("label_policy_evaluations")
    op.drop_index("ix_label_policy_versions_trace_id", table_name="label_policy_versions")
    op.drop_index("ix_label_policy_versions_scope_status", table_name="label_policy_versions")
    op.drop_table("label_policy_versions")
    with op.batch_alter_table("label_candidates") as batch_op:
        batch_op.drop_column("resource_version")
    op.drop_index("ix_label_versions_release_gate_id", table_name="label_versions")
    op.drop_index("ix_label_versions_policy_version_id", table_name="label_versions")
    with op.batch_alter_table("label_versions") as batch_op:
        batch_op.drop_column("release_gate_id")
        batch_op.drop_column("policy_version_id")
        batch_op.drop_column("resource_version")
