"""add server-locked label calibrators and a unique active fact head

Revision ID: 0029_label_calibration_fact_chain
Revises: 0028_release_active_head
Create Date: 2026-07-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0029_label_calibration_fact_chain"
down_revision = "0028_release_active_head"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _create_calibration_immutability_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_calibration_versions_no_{action.lower()} "
                    f"BEFORE {action} ON label_calibration_versions "
                    "BEGIN SELECT RAISE(ABORT, 'append-only label calibration version'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_calibration_versions_no_{action.lower()} "
                    f"BEFORE {action} ON label_calibration_versions FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only label calibration version'"
                )
            )


def _drop_calibration_immutability_triggers() -> None:
    for action in ("update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_label_calibration_versions_no_{action}"))


def _backfill_fact_active_heads() -> None:
    """Collapse legacy duplicate heads deterministically before adding the unique slot."""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT fact_id, tenant_id, project_id, subject_scope, subject_key, label_id "
            "FROM label_facts WHERE status = 'active' "
            "ORDER BY tenant_id, project_id, subject_scope, subject_key, label_id, "
            "created_at DESC, fact_id DESC"
        )
    ).mappings()
    seen: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        key = (
            str(row["tenant_id"]),
            str(row["project_id"]),
            str(row["subject_scope"]),
            str(row["subject_key"]),
            str(row["label_id"]),
        )
        if key in seen:
            connection.execute(
                sa.text(
                    "UPDATE label_facts SET status = 'superseded', active_slot = NULL "
                    "WHERE fact_id = :fact_id"
                ),
                {"fact_id": row["fact_id"]},
            )
        else:
            seen.add(key)
            connection.execute(
                sa.text("UPDATE label_facts SET active_slot = 'active' WHERE fact_id = :fact_id"),
                {"fact_id": row["fact_id"]},
            )


def upgrade() -> None:
    op.create_table(
        "label_calibration_versions",
        sa.Column("calibration_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("label_version_id", sa.String(128), nullable=False),
        sa.Column("label_id", sa.String(128), nullable=False),
        sa.Column("source_family", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("gold_set_version_id", sa.String(128), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("training_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "calibration_version_id",
            name="uq_label_calibration_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            "source_family",
            "version",
            name="uq_label_calibration_versions_scope_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_calibration_versions_scope_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "gold_set_version_id"],
            [
                "gold_set_versions.tenant_id",
                "gold_set_versions.project_id",
                "gold_set_versions.gold_set_version_id",
            ],
            name="fk_label_calibration_versions_scope_gold",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "method IN ('isotonic', 'platt', 'global-conservative')",
            name="ck_label_calibration_versions_method",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_label_calibration_versions_status",
        ),
        sa.CheckConstraint(
            "sample_count > 0",
            name="ck_label_calibration_versions_samples",
        ),
    )
    op.create_index(
        "ix_label_calibration_versions_scope_lookup",
        "label_calibration_versions",
        [
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            "source_family",
            "status",
        ],
    )
    op.create_index(
        "ix_label_calibration_versions_trace_id",
        "label_calibration_versions",
        ["trace_id"],
    )
    _create_calibration_immutability_triggers()

    with op.batch_alter_table("label_facts") as batch:
        batch.add_column(sa.Column("active_slot", sa.String(16), nullable=True))
    _backfill_fact_active_heads()
    with op.batch_alter_table("label_facts") as batch:
        batch.create_check_constraint(
            "ck_label_facts_active_slot",
            "(status = 'active' AND active_slot = 'active') OR "
            "(status = 'superseded' AND active_slot IS NULL)",
        )
    op.create_index(
        "uq_label_facts_active_head",
        "label_facts",
        [
            "tenant_id",
            "project_id",
            "subject_scope",
            "subject_key",
            "label_id",
            "active_slot",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_label_facts_active_head", table_name="label_facts")
    with op.batch_alter_table("label_facts") as batch:
        batch.drop_constraint("ck_label_facts_active_slot", type_="check")
        batch.drop_column("active_slot")
    _drop_calibration_immutability_triggers()
    op.drop_table("label_calibration_versions")
