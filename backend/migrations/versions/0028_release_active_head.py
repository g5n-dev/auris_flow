"""add two-phase release commands and unique active bundle heads

Revision ID: 0028_release_active_head
Revises: 0027_label_eval_results
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0028_release_active_head"
down_revision = "0027_label_eval_results"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _create_single_completed_head_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_deployments_single_completed_insert "
                "BEFORE INSERT ON release_deployments "
                "WHEN NEW.status = 'completed' AND NEW.rollout_percentage = 100 "
                "BEGIN SELECT RAISE(ABORT, 'multiple active release bundles') "
                "WHERE EXISTS (SELECT 1 FROM release_deployments existing "
                "WHERE existing.tenant_id = NEW.tenant_id "
                "AND existing.project_id = NEW.project_id "
                "AND existing.environment = NEW.environment "
                "AND existing.status = 'completed' "
                "AND existing.rollout_percentage = 100); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_deployments_single_completed_update "
                "BEFORE UPDATE ON release_deployments "
                "WHEN NEW.status = 'completed' AND NEW.rollout_percentage = 100 "
                "BEGIN SELECT RAISE(ABORT, 'multiple active release bundles') "
                "WHERE EXISTS (SELECT 1 FROM release_deployments existing "
                "WHERE existing.tenant_id = NEW.tenant_id "
                "AND existing.project_id = NEW.project_id "
                "AND existing.environment = NEW.environment "
                "AND existing.deployment_id <> NEW.deployment_id "
                "AND existing.status = 'completed' "
                "AND existing.rollout_percentage = 100); END"
            )
        )
    elif dialect in {"mysql", "mariadb"}:
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_deployments_single_completed_insert "
                "BEFORE INSERT ON release_deployments FOR EACH ROW "
                "BEGIN IF NEW.status = 'completed' AND NEW.rollout_percentage = 100 "
                "AND EXISTS (SELECT 1 FROM release_deployments existing "
                "WHERE existing.tenant_id = NEW.tenant_id "
                "AND existing.project_id = NEW.project_id "
                "AND existing.environment = NEW.environment "
                "AND existing.status = 'completed' "
                "AND existing.rollout_percentage = 100) THEN "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'multiple active release bundles'; "
                "END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_release_deployments_single_completed_update "
                "BEFORE UPDATE ON release_deployments FOR EACH ROW "
                "BEGIN IF NEW.status = 'completed' AND NEW.rollout_percentage = 100 "
                "AND EXISTS (SELECT 1 FROM release_deployments existing "
                "WHERE existing.tenant_id = NEW.tenant_id "
                "AND existing.project_id = NEW.project_id "
                "AND existing.environment = NEW.environment "
                "AND existing.deployment_id <> NEW.deployment_id "
                "AND existing.status = 'completed' "
                "AND existing.rollout_percentage = 100) THEN "
                "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'multiple active release bundles'; "
                "END IF; END"
            )
        )


def _drop_single_completed_head_triggers() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_release_deployments_single_completed_update"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_release_deployments_single_completed_insert"))


def upgrade() -> None:
    op.create_table(
        "release_commands",
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("target_deployment_id", sa.String(128), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("active_slot", sa.String(16), nullable=True),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("expected_deployment_status", sa.String(32), nullable=False),
        sa.Column("expected_head_generation", sa.Integer(), nullable=True),
        sa.Column("expected_head_deployment_id", sa.String(128), nullable=True),
        sa.Column("expected_head_bundle_sha256", sa.String(64), nullable=True),
        sa.Column("command_sha256", sa.String(64), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("completed_by_source", sa.String(128), nullable=True),
        sa.Column("completion_receipt_id", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "command_id",
            name="uq_release_commands_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "run_id",
            name="uq_release_commands_scope_run",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "deployment_id",
            "active_slot",
            name="uq_release_commands_active_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "deployment_id"],
            [
                "release_deployments.tenant_id",
                "release_deployments.project_id",
                "release_deployments.deployment_id",
            ],
            name="fk_release_commands_scope_deployment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_deployment_id"],
            [
                "release_deployments.tenant_id",
                "release_deployments.project_id",
                "release_deployments.deployment_id",
            ],
            name="fk_release_commands_scope_target_deployment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_release_commands_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "action IN ('publish', 'approve-gray', 'promote', 'rollback')",
            name="ck_release_commands_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'materializing', 'completed', 'blocked', 'failed')",
            name="ck_release_commands_status",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'materializing') AND active_slot = 'active') "
            "OR (status IN ('completed', 'blocked', 'failed') AND active_slot IS NULL)",
            name="ck_release_commands_active_slot",
        ),
    )
    op.create_index(
        "ix_release_commands_scope_status",
        "release_commands",
        ["tenant_id", "project_id", "environment", "status"],
    )
    op.create_index("ix_release_commands_trace_id", "release_commands", ["trace_id"])

    op.create_table(
        "release_bundle_heads",
        sa.Column("release_head_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("active_deployment_id", sa.String(128), nullable=False),
        sa.Column("active_bundle_sha256", sa.String(64), nullable=False),
        sa.Column("prompt_asset_id", sa.String(128), nullable=False),
        sa.Column("prompt_version_id", sa.String(128), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(128), nullable=False),
        sa.Column("aggregation_policy_version_id", sa.String(128), nullable=False),
        sa.Column("eval_dataset_version_id", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("bootstrapped", sa.Boolean(), nullable=False),
        sa.Column(
            "activated_by_command_id",
            sa.String(128),
            sa.ForeignKey(
                "release_commands.command_id",
                name="fk_release_bundle_heads_activated_command",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            name="uq_release_bundle_heads_scope_environment",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "active_deployment_id",
            name="uq_release_bundle_heads_scope_deployment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "active_deployment_id"],
            [
                "release_deployments.tenant_id",
                "release_deployments.project_id",
                "release_deployments.deployment_id",
            ],
            name="fk_release_bundle_heads_scope_deployment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("generation > 0", name="ck_release_bundle_heads_generation"),
        sa.CheckConstraint("status = 'active'", name="ck_release_bundle_heads_status"),
    )
    op.create_index(
        "ix_release_bundle_heads_scope_active",
        "release_bundle_heads",
        ["tenant_id", "project_id", "environment", "active_deployment_id"],
    )
    op.create_index("ix_release_bundle_heads_trace_id", "release_bundle_heads", ["trace_id"])
    _create_single_completed_head_triggers()


def downgrade() -> None:
    _drop_single_completed_head_triggers()
    op.drop_table("release_bundle_heads")
    op.drop_table("release_commands")
