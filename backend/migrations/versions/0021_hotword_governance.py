"""add ASR hotword governance strong tables and badcase projection

Revision ID: 0021_hotword_governance
Revises: 0020_auth_sessions
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0021_hotword_governance"
down_revision = "0020_auth_sessions"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def timestamp_columns() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "hotword_packs",
        sa.Column("pack_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("language", sa.String(32), nullable=False),
        sa.Column("domain", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="active", nullable=False),
        sa.Column("current_version_id", sa.String(128), nullable=True),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint("tenant_id", "project_id", "pack_id", name="uq_hotword_packs_scope_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            "language",
            "domain",
            name="uq_hotword_packs_scope_name",
        ),
        sa.CheckConstraint("resource_version > 0", name="ck_hotword_packs_resource_version"),
    )
    op.create_index(
        "ix_hotword_packs_scope_status",
        "hotword_packs",
        ["tenant_id", "project_id", "status"],
    )

    op.create_table(
        "hotword_pack_versions",
        sa.Column("version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("pack_id", sa.String(128), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("baseline_version_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), server_default="draft", nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=True),
        sa.Column("manifest_storage_object_id", sa.String(128), nullable=True),
        sa.Column("eval_run_id", sa.String(128), nullable=True),
        sa.Column("eval_locked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("model_approved_by", sa.String(64), nullable=True),
        sa.Column("project_admin_confirmed_by", sa.String(64), nullable=True),
        sa.Column("provider_artifact_ref", sa.String(1024), nullable=True),
        sa.Column("compiled_provider", sa.String(128), nullable=True),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        sa.Column("published_at", utc_datetime_type(), nullable=True),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "version_id",
            name="uq_hotword_pack_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "pack_id",
            "version",
            name="uq_hotword_pack_versions_scope_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "pack_id"],
            ["hotword_packs.tenant_id", "hotword_packs.project_id", "hotword_packs.pack_id"],
            name="fk_hotword_pack_versions_scope_pack",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "baseline_version_id"],
            [
                "hotword_pack_versions.tenant_id",
                "hotword_pack_versions.project_id",
                "hotword_pack_versions.version_id",
            ],
            name="fk_hotword_pack_versions_scope_baseline",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "resource_version > 0", name="ck_hotword_pack_versions_resource_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'validating', 'ready_for_eval', 'evaluating', "
            "'gate_blocked', 'review_required', 'approved', 'published', 'deprecated', "
            "'rolled_back', 'archived')",
            name="ck_hotword_pack_versions_status",
        ),
    )
    op.create_index(
        "ix_hotword_pack_versions_scope_status",
        "hotword_pack_versions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_hotword_pack_versions_scope_pack",
        "hotword_pack_versions",
        ["tenant_id", "project_id", "pack_id"],
    )

    op.create_table(
        "hotword_version_items",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("version_id", sa.String(128), nullable=False),
        sa.Column("canonical_term", sa.String(255), nullable=False),
        sa.Column("normalized_term", sa.String(255), nullable=False),
        sa.Column("aliases", json_type(), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("source_badcase_id", sa.String(128), nullable=True),
        sa.Column("source_type", sa.String(32), server_default="manual", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_hotword_version_items_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "version_id",
            "normalized_term",
            name="uq_hotword_version_items_scope_term",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "version_id"],
            [
                "hotword_pack_versions.tenant_id",
                "hotword_pack_versions.project_id",
                "hotword_pack_versions.version_id",
            ],
            name="fk_hotword_version_items_scope_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_badcase_id"],
            ["badcases.tenant_id", "badcases.project_id", "badcases.badcase_id"],
            name="fk_hotword_version_items_scope_badcase",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("weight BETWEEN 0 AND 100", name="ck_hotword_version_items_weight"),
        sa.CheckConstraint(
            "resource_version > 0", name="ck_hotword_version_items_resource_version"
        ),
        sa.CheckConstraint(
            "source_type IN ('manual', 'badcase', 'knowledge_candidate')",
            name="ck_hotword_version_items_source_type",
        ),
    )
    op.create_index(
        "ix_hotword_version_items_scope_version",
        "hotword_version_items",
        ["tenant_id", "project_id", "version_id"],
    )

    op.create_table(
        "hotword_metric_snapshots",
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("bucket_start", utc_datetime_type(), nullable=False),
        sa.Column("bucket_end", utc_datetime_type(), nullable=False),
        sa.Column("store_id", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("hotword_pack_version_id", sa.String(128), nullable=True),
        sa.Column("standard_term", sa.String(255), nullable=True),
        sa.Column("expected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("correct_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("weighted_error_count", sa.Float(), server_default="0", nullable=False),
        sa.Column("false_insert_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("recognized_hotword_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("impacted_session_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("evidence_confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        *timestamp_columns(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "snapshot_id",
            name="uq_hotword_metric_snapshots_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            [
                "hotword_pack_versions.tenant_id",
                "hotword_pack_versions.project_id",
                "hotword_pack_versions.version_id",
            ],
            name="fk_hotword_metric_snapshots_scope_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "expected_count >= 0 AND correct_count >= 0 AND weighted_error_count >= 0 "
            "AND false_insert_count >= 0 AND recognized_hotword_count >= 0 "
            "AND impacted_session_count >= 0 AND correct_count <= expected_count",
            name="ck_hotword_metric_snapshots_counts",
        ),
        sa.CheckConstraint(
            "evidence_confidence BETWEEN 0 AND 1",
            name="ck_hotword_metric_snapshots_evidence_confidence",
        ),
        sa.CheckConstraint(
            "bucket_end > bucket_start",
            name="ck_hotword_metric_snapshots_bucket",
        ),
    )
    op.create_index(
        "ix_hotword_metric_snapshots_scope_bucket",
        "hotword_metric_snapshots",
        ["tenant_id", "project_id", "bucket_start", "bucket_end"],
    )
    op.create_index(
        "ix_hotword_metric_snapshots_scope_dimensions",
        "hotword_metric_snapshots",
        [
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ],
    )

    with op.batch_alter_table("badcases") as batch_op:
        batch_op.add_column(sa.Column("capability", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("error_type", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("standard_term", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("recognized_text", sa.String(1000), nullable=True))
        batch_op.add_column(sa.Column("evidence_ref", sa.String(1024), nullable=True))
        batch_op.add_column(sa.Column("evidence_storage_object_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("evidence_level", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("hotword_pack_version_id", sa.String(128), nullable=True))
        batch_op.add_column(
            sa.Column("expected_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("correct_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("weighted_error_count", sa.Float(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("manual_correction_count", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("priority_score", sa.Float(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("candidate_state", sa.String(32), server_default="suspected", nullable=False)
        )
        batch_op.add_column(sa.Column("root_cause", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("fix_suggestion", sa.String(1000), nullable=True))
        batch_op.add_column(sa.Column("downstream_impact", json_type(), nullable=True))
        batch_op.add_column(
            sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False)
        )
        batch_op.add_column(sa.Column("root_trace_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("current_trace_id", sa.String(128), nullable=True))

    op.execute(
        sa.text(
            "UPDATE badcases SET root_trace_id = COALESCE(trace_id, 'trace_legacy_hotword'), "
            "current_trace_id = COALESCE(trace_id, 'trace_legacy_hotword')"
        )
    )
    op.execute(sa.text("UPDATE badcases SET downstream_impact = '{}'"))
    with op.batch_alter_table("badcases") as batch_op:
        batch_op.alter_column("root_trace_id", existing_type=sa.String(128), nullable=False)
        batch_op.alter_column("current_trace_id", existing_type=sa.String(128), nullable=False)
        batch_op.alter_column("downstream_impact", existing_type=json_type(), nullable=False)
        batch_op.create_check_constraint("ck_badcases_resource_version", "resource_version > 0")
        batch_op.create_check_constraint(
            "ck_badcases_hotword_error_type",
            "error_type IS NULL OR error_type IN ('missing_term', 'misrecognition', "
            "'alias_gap', 'weight_issue', 'false_boost')",
        )
        batch_op.create_foreign_key(
            "fk_badcases_scope_hotword_version",
            "hotword_pack_versions",
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            ["tenant_id", "project_id", "version_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_index(
            "ix_badcases_scope_capability_status",
            ["tenant_id", "project_id", "capability", "status"],
        )
        batch_op.create_index(
            "ix_badcases_scope_hotword_version",
            ["tenant_id", "project_id", "hotword_pack_version_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("badcases") as batch_op:
        batch_op.drop_constraint("fk_badcases_scope_hotword_version", type_="foreignkey")
        batch_op.drop_index("ix_badcases_scope_hotword_version")
        batch_op.drop_index("ix_badcases_scope_capability_status")
        batch_op.drop_constraint("ck_badcases_hotword_error_type", type_="check")
        batch_op.drop_constraint("ck_badcases_resource_version", type_="check")
        for column in (
            "current_trace_id",
            "root_trace_id",
            "resource_version",
            "downstream_impact",
            "fix_suggestion",
            "root_cause",
            "candidate_state",
            "priority_score",
            "manual_correction_count",
            "weighted_error_count",
            "correct_count",
            "expected_count",
            "evidence_level",
            "hotword_pack_version_id",
            "evidence_storage_object_id",
            "evidence_ref",
            "recognized_text",
            "standard_term",
            "error_type",
            "capability",
        ):
            batch_op.drop_column(column)

    op.drop_index(
        "ix_hotword_metric_snapshots_scope_dimensions",
        table_name="hotword_metric_snapshots",
    )
    op.drop_index(
        "ix_hotword_metric_snapshots_scope_bucket",
        table_name="hotword_metric_snapshots",
    )
    op.drop_table("hotword_metric_snapshots")
    op.drop_index("ix_hotword_version_items_scope_version", table_name="hotword_version_items")
    op.drop_table("hotword_version_items")
    op.drop_index("ix_hotword_pack_versions_scope_pack", table_name="hotword_pack_versions")
    op.drop_index("ix_hotword_pack_versions_scope_status", table_name="hotword_pack_versions")
    op.drop_table("hotword_pack_versions")
    op.drop_index("ix_hotword_packs_scope_status", table_name="hotword_packs")
    op.drop_table("hotword_packs")
