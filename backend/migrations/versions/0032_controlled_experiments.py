"""add controlled experiment assignments, facts, metrics and decisions

Revision ID: 0032_controlled_experiments
Revises: 0031_scene_profiles
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0032_controlled_experiments"
down_revision = "0031_scene_profiles"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


APPEND_ONLY_TABLES = (
    "experiment_assignments",
    "experiment_exposures",
    "experiment_outcomes",
    "experiment_metric_snapshots",
    "experiment_decisions",
)


def _create_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER trg_{table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} "
                        f"BEGIN SELECT RAISE(ABORT, 'append-only {table}'); END"
                    )
                )
    elif dialect in {"mysql", "mariadb"}:
        for table in APPEND_ONLY_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER trg_{table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} FOR EACH ROW "
                        "SIGNAL SQLSTATE '45000' "
                        f"SET MESSAGE_TEXT = 'append-only {table}'"
                    )
                )


def _drop_immutability_triggers() -> None:
    for table in APPEND_ONLY_TABLES:
        for action in ("update", "delete"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_no_{action}"))


def upgrade() -> None:
    op.create_table(
        "controlled_experiments",
        sa.Column("experiment_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("experiment_kind", sa.String(64), nullable=False),
        sa.Column("task_type_id", sa.String(128), nullable=False),
        sa.Column("control_task_version_id", sa.String(128), nullable=False),
        sa.Column("candidate_task_version_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_version_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("design_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", _utc_datetime()),
        sa.Column("ended_at", _utc_datetime()),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "experiment_id", name="uq_controlled_experiments_scope_id"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'stopped', 'decided', 'archived')",
            name="ck_controlled_experiments_status",
        ),
        sa.CheckConstraint("resource_version > 0", name="ck_controlled_experiments_version"),
    )
    op.create_index(
        "ix_controlled_experiments_scope_status",
        "controlled_experiments",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_controlled_experiments_scope_scene",
        "controlled_experiments",
        ["tenant_id", "project_id", "scene_profile_version_id"],
    )
    op.create_index("ix_controlled_experiments_trace_id", "controlled_experiments", ["trace_id"])

    op.create_table(
        "experiment_assignments",
        sa.Column("assignment_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("subject_key_sha256", sa.String(64), nullable=False),
        sa.Column("arm_key", sa.String(64), nullable=False),
        sa.Column("assignment_bucket", sa.Integer(), nullable=False),
        sa.Column("design_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "assignment_id", name="uq_experiment_assignments_scope_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "assignment_id",
            name="uq_experiment_assignments_scope_experiment_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "subject_key_sha256",
            name="uq_experiment_assignments_scope_subject",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id"],
            [
                "controlled_experiments.tenant_id",
                "controlled_experiments.project_id",
                "controlled_experiments.experiment_id",
            ],
            name="fk_experiment_assignments_scope_experiment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "assignment_bucket >= 0 AND assignment_bucket < 1000000",
            name="ck_experiment_assignments_bucket",
        ),
    )
    op.create_index(
        "ix_experiment_assignments_scope_arm",
        "experiment_assignments",
        ["tenant_id", "project_id", "experiment_id", "arm_key"],
    )
    op.create_index("ix_experiment_assignments_trace_id", "experiment_assignments", ["trace_id"])

    op.create_table(
        "experiment_exposures",
        sa.Column("exposure_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("exposure_key_sha256", sa.String(64), nullable=False),
        sa.Column("arm_key", sa.String(64), nullable=False),
        sa.Column("occurred_at", _utc_datetime(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_id",
            name="uq_experiment_exposures_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_key_sha256",
            name="uq_experiment_exposures_scope_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id"],
            [
                "controlled_experiments.tenant_id",
                "controlled_experiments.project_id",
                "controlled_experiments.experiment_id",
            ],
            name="fk_experiment_exposures_scope_experiment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id", "assignment_id"],
            [
                "experiment_assignments.tenant_id",
                "experiment_assignments.project_id",
                "experiment_assignments.experiment_id",
                "experiment_assignments.assignment_id",
            ],
            name="fk_experiment_exposures_scope_assignment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "ix_experiment_exposures_scope_arm",
        "experiment_exposures",
        ["tenant_id", "project_id", "experiment_id", "arm_key"],
    )
    op.create_index("ix_experiment_exposures_trace_id", "experiment_exposures", ["trace_id"])

    op.create_table(
        "experiment_outcomes",
        sa.Column("outcome_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("exposure_id", sa.String(128), nullable=False),
        sa.Column("arm_key", sa.String(64), nullable=False),
        sa.Column("metric_key", sa.String(96), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("occurred_at", _utc_datetime(), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_id",
            "metric_key",
            name="uq_experiment_outcomes_scope_metric",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id", "exposure_id"],
            [
                "experiment_exposures.tenant_id",
                "experiment_exposures.project_id",
                "experiment_exposures.experiment_id",
                "experiment_exposures.exposure_id",
            ],
            name="fk_experiment_outcomes_scope_exposure",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "ix_experiment_outcomes_scope_metric",
        "experiment_outcomes",
        ["tenant_id", "project_id", "experiment_id", "metric_key"],
    )
    op.create_index("ix_experiment_outcomes_trace_id", "experiment_outcomes", ["trace_id"])

    op.create_table(
        "experiment_metric_snapshots",
        sa.Column("metric_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("snapshot_version", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("primary_metric_key", sa.String(96), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("scene_profile_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_version_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_snapshot_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "snapshot_version",
            name="uq_experiment_metric_snapshots_scope_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id"],
            [
                "controlled_experiments.tenant_id",
                "controlled_experiments.project_id",
                "controlled_experiments.experiment_id",
            ],
            name="fk_experiment_metric_snapshots_scope_experiment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "ix_experiment_metric_snapshots_scope_experiment",
        "experiment_metric_snapshots",
        ["tenant_id", "project_id", "experiment_id"],
    )
    op.create_index(
        "ix_experiment_metric_snapshots_trace_id", "experiment_metric_snapshots", ["trace_id"]
    )

    op.create_table(
        "experiment_decisions",
        sa.Column("decision_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("experiment_id", sa.String(128), nullable=False),
        sa.Column("metric_snapshot_id", sa.String(128)),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "decision_id", name="uq_experiment_decisions_scope_id"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id"],
            [
                "controlled_experiments.tenant_id",
                "controlled_experiments.project_id",
                "controlled_experiments.experiment_id",
            ],
            name="fk_experiment_decisions_scope_experiment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )
    op.create_index(
        "ix_experiment_decisions_scope_experiment",
        "experiment_decisions",
        ["tenant_id", "project_id", "experiment_id"],
    )
    op.create_index(
        "ix_experiment_decisions_metric_snapshot_id",
        "experiment_decisions",
        ["metric_snapshot_id"],
    )
    op.create_index("ix_experiment_decisions_trace_id", "experiment_decisions", ["trace_id"])
    _create_immutability_triggers()


def downgrade() -> None:
    _drop_immutability_triggers()
    op.drop_table("experiment_decisions")
    op.drop_table("experiment_metric_snapshots")
    op.drop_table("experiment_outcomes")
    op.drop_table("experiment_exposures")
    op.drop_table("experiment_assignments")
    op.drop_table("controlled_experiments")
