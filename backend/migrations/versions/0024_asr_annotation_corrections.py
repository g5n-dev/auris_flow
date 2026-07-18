"""add append-only ASR annotation correction observations

Revision ID: 0024_asr_annotation_corrections
Revises: 0023_hotword_production_activation
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0024_asr_annotation_corrections"
down_revision = "0023_hotword_production_activation"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "asr_annotation_corrections",
        sa.Column("correction_id", sa.String(128), primary_key=True),
        sa.Column("annotation_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("audio_session_id", sa.String(128), nullable=False),
        sa.Column("observed_at", utc_datetime_type(), nullable=False),
        sa.Column("status", sa.String(32), server_default="submitted", nullable=False),
        sa.Column("standard_term", sa.String(255), nullable=False),
        sa.Column("normalized_term", sa.String(255), nullable=False),
        sa.Column("recognized_text", sa.String(1000), nullable=False),
        sa.Column("corrected_text", sa.String(1000), nullable=False),
        sa.Column("error_type", sa.String(32), nullable=False),
        sa.Column("evidence_storage_object_id", sa.String(128), nullable=False),
        sa.Column("evidence_window", sa.String(128), nullable=False),
        sa.Column("hotword_pack_version_id", sa.String(128), nullable=False),
        sa.Column("source_badcase_id", sa.String(128), nullable=False),
        sa.Column("store_id", sa.String(128), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("model_version", sa.String(128), nullable=True),
        sa.Column("evidence_level", sa.String(32), server_default="discovery", nullable=False),
        sa.Column("correction_fingerprint", sa.String(64), nullable=False),
        sa.Column("semantic_sha256", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("source_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("created_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", utc_datetime_type(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "correction_id",
            name="uq_asr_annotation_corrections_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "annotation_id",
            name="uq_asr_annotation_corrections_scope_annotation",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "correction_fingerprint",
            name="uq_asr_annotation_corrections_scope_fingerprint",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "evidence_storage_object_id",
            name="uq_asr_annotation_corrections_scope_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            [
                "hotword_pack_versions.tenant_id",
                "hotword_pack_versions.project_id",
                "hotword_pack_versions.version_id",
            ],
            name="fk_asr_annotation_corrections_scope_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_badcase_id"],
            ["badcases.tenant_id", "badcases.project_id", "badcases.badcase_id"],
            name="fk_asr_annotation_corrections_scope_badcase",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status = 'submitted'",
            name="ck_asr_annotation_corrections_status",
        ),
        sa.CheckConstraint(
            "evidence_level = 'discovery'",
            name="ck_asr_annotation_corrections_evidence_level",
        ),
        sa.CheckConstraint(
            "error_type IN ('missing_term', 'misrecognition', 'alias_gap', "
            "'weight_issue', 'false_boost')",
            name="ck_asr_annotation_corrections_error_type",
        ),
    )
    op.create_index(
        "ix_asr_annotation_corrections_scope_observed",
        "asr_annotation_corrections",
        ["tenant_id", "project_id", "observed_at"],
    )
    op.create_index(
        "ix_asr_annotation_corrections_scope_dimensions",
        "asr_annotation_corrections",
        [
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ],
    )
    op.create_index(
        "ix_asr_annotation_corrections_scope_term",
        "asr_annotation_corrections",
        ["tenant_id", "project_id", "normalized_term"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_asr_annotation_corrections_no_{action.lower()} "
                    f"BEFORE {action} ON asr_annotation_corrections "
                    "BEGIN SELECT RAISE(ABORT, 'append-only ASR correction'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_asr_annotation_corrections_no_{action.lower()} "
                    f"BEFORE {action} ON asr_annotation_corrections FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only ASR correction'"
                )
            )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect in {"mysql", "mariadb"}:
        for action in ("update", "delete"):
            op.execute(
                sa.text(f"DROP TRIGGER IF EXISTS trg_asr_annotation_corrections_no_{action}")
            )
    op.drop_index(
        "ix_asr_annotation_corrections_scope_term",
        table_name="asr_annotation_corrections",
    )
    op.drop_index(
        "ix_asr_annotation_corrections_scope_dimensions",
        table_name="asr_annotation_corrections",
    )
    op.drop_index(
        "ix_asr_annotation_corrections_scope_observed",
        table_name="asr_annotation_corrections",
    )
    op.drop_table("asr_annotation_corrections")
