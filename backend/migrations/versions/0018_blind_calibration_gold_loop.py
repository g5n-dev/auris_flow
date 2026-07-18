"""add blind calibration and versioned gold-set persistence

Revision ID: 0018_blind_calibration_gold_loop
Revises: 0017_quality_appeals
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0018_blind_calibration_gold_loop"
down_revision = "0017_quality_appeals"
branch_labels = None
depends_on = None


def utc_datetime_type() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def json_type() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(sa.JSON(), "mysql")


def upgrade() -> None:
    op.create_table(
        "calibration_rounds",
        sa.Column("round_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("label_version", sa.String(128), nullable=False),
        sa.Column("rubric_version", sa.String(128), nullable=False),
        sa.Column("sample_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("reviewer_a_id", sa.String(64), nullable=False),
        sa.Column("reviewer_b_id", sa.String(64), nullable=False),
        sa.Column("adjudicator_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="in_review", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("paired_submission_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("agreed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("conflict_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("adjudication_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("excluded_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_agreement_ppm", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cohen_kappa_micros", sa.Integer(), server_default="0", nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("current_trace_id", sa.String(128), nullable=False),
        sa.Column("published_at", utc_datetime_type(), nullable=True),
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
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            name="uq_cal_rounds_scope_id",
        ),
        sa.CheckConstraint(
            "reviewer_a_id <> reviewer_b_id AND "
            "adjudicator_id <> reviewer_a_id AND adjudicator_id <> reviewer_b_id",
            name="ck_cal_rounds_participants",
        ),
        sa.CheckConstraint(
            "status IN ('in_review', 'ready', 'published')",
            name="ck_cal_rounds_status",
        ),
        sa.CheckConstraint(
            "sample_count > 0 AND paired_submission_count >= 0 AND "
            "paired_submission_count <= sample_count AND agreed_count >= 0 AND "
            "conflict_count >= 0 AND agreed_count + conflict_count = paired_submission_count AND "
            "adjudication_count >= 0 AND adjudication_count <= conflict_count AND "
            "excluded_count >= 0 AND excluded_count <= adjudication_count AND "
            "observed_agreement_ppm BETWEEN 0 AND 1000000 AND "
            "cohen_kappa_micros BETWEEN -1000000 AND 1000000",
            name="ck_cal_rounds_metrics",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_cal_rounds_resource_version",
        ),
        sa.CheckConstraint(
            "status = 'in_review' OR "
            "(paired_submission_count = sample_count AND adjudication_count = conflict_count)",
            name="ck_cal_rounds_completion_state",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR "
            "(status <> 'published' AND published_at IS NULL)",
            name="ck_cal_rounds_publish_state",
        ),
    )
    op.create_index(
        "ix_cal_rounds_scope_status",
        "calibration_rounds",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_cal_rounds_scope_dataset",
        "calibration_rounds",
        ["tenant_id", "project_id", "dataset_id", "dataset_version"],
    )

    op.create_table(
        "calibration_items",
        sa.Column("item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("evidence_ref", sa.String(1024), nullable=False),
        sa.Column("source_case_id", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("review_outcome", sa.String(32), server_default="pending", nullable=False),
        sa.Column("final_value_json", json_type(), nullable=True),
        sa.Column("final_value_sha256", sa.String(64), nullable=True),
        sa.Column("adjudication_claimed_by", sa.String(64), nullable=True),
        sa.Column("adjudication_claimed_at", utc_datetime_type(), nullable=True),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
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
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_cal_items_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            "round_id",
            name="uq_cal_items_scope_id_round",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "ordinal",
            name="uq_cal_items_scope_round_pos",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "source_case_id",
            name="uq_cal_items_scope_round_case",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "round_id"],
            [
                "calibration_rounds.tenant_id",
                "calibration_rounds.project_id",
                "calibration_rounds.round_id",
            ],
            name="fk_cal_items_scope_round",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'agreed', 'conflicted', 'adjudicated', 'excluded')",
            name="ck_cal_items_status",
        ),
        sa.CheckConstraint(
            "review_outcome IN ('pending', 'agreed', 'conflicted')",
            name="ck_cal_items_review_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND review_outcome = 'pending' AND "
            "final_value_json IS NULL AND final_value_sha256 IS NULL) OR "
            "(status = 'agreed' AND review_outcome = 'agreed' AND "
            "final_value_json IS NOT NULL AND final_value_sha256 IS NOT NULL) OR "
            "(status = 'conflicted' AND review_outcome = 'conflicted' AND "
            "final_value_json IS NULL AND final_value_sha256 IS NULL) OR "
            "(status = 'adjudicated' AND review_outcome = 'conflicted' AND "
            "final_value_json IS NOT NULL AND final_value_sha256 IS NOT NULL) OR "
            "(status = 'excluded' AND review_outcome = 'conflicted' AND "
            "final_value_json IS NULL AND final_value_sha256 IS NULL)",
            name="ck_cal_items_resolution_state",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_cal_items_ordinal"),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_cal_items_resource_version",
        ),
        sa.CheckConstraint(
            "(adjudication_claimed_by IS NULL AND adjudication_claimed_at IS NULL) OR "
            "(status = 'conflicted' AND adjudication_claimed_by IS NOT NULL AND "
            "adjudication_claimed_at IS NOT NULL)",
            name="ck_cal_items_claim_state",
        ),
    )
    op.create_index(
        "ix_cal_items_scope_round_status",
        "calibration_items",
        ["tenant_id", "project_id", "round_id", "status"],
    )

    op.create_table(
        "calibration_assignments",
        sa.Column("assignment_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("slot", sa.String(1), nullable=False),
        sa.Column("reviewer_id", sa.String(64), nullable=False),
        sa.Column("review_task_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("submitted_at", utc_datetime_type(), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
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
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            name="uq_cal_assign_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "slot",
            name="uq_cal_assign_scope_item_slot",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "reviewer_id",
            name="uq_cal_assign_scope_item_reviewer",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_task_id",
            name="uq_cal_assign_scope_review_task",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            "round_id",
            "item_id",
            "reviewer_id",
            name="uq_cal_assign_scope_binding",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "item_id", "round_id"],
            [
                "calibration_items.tenant_id",
                "calibration_items.project_id",
                "calibration_items.item_id",
                "calibration_items.round_id",
            ],
            name="fk_cal_assign_scope_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "review_task_id"],
            [
                "human_review_tasks.tenant_id",
                "human_review_tasks.project_id",
                "human_review_tasks.review_task_id",
            ],
            name="fk_cal_assign_scope_review_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("slot IN ('A', 'B')", name="ck_cal_assign_slot"),
        sa.CheckConstraint(
            "status IN ('pending', 'submitted')",
            name="ck_cal_assign_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND submitted_at IS NOT NULL)",
            name="ck_cal_assign_submit_state",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_cal_assign_resource_version",
        ),
    )
    op.create_index(
        "ix_cal_assign_scope_reviewer",
        "calibration_assignments",
        ["tenant_id", "project_id", "reviewer_id", "status"],
    )
    op.create_index(
        "ix_cal_assign_scope_round",
        "calibration_assignments",
        ["tenant_id", "project_id", "round_id"],
    )

    op.create_table(
        "calibration_submissions",
        sa.Column("submission_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("assignment_id", sa.String(128), nullable=False),
        sa.Column("reviewer_id", sa.String(64), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("value_json", json_type(), nullable=False),
        sa.Column("canonical_value_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "submitted_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "submission_id",
            name="uq_cal_subs_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            name="uq_cal_subs_scope_assignment",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "submission_id",
            "round_id",
            "item_id",
            name="uq_cal_subs_scope_binding",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "assignment_id",
                "round_id",
                "item_id",
                "reviewer_id",
            ],
            [
                "calibration_assignments.tenant_id",
                "calibration_assignments.project_id",
                "calibration_assignments.assignment_id",
                "calibration_assignments.round_id",
                "calibration_assignments.item_id",
                "calibration_assignments.reviewer_id",
            ],
            name="fk_cal_subs_scope_assignment_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "item_id", "round_id"],
            [
                "calibration_items.tenant_id",
                "calibration_items.project_id",
                "calibration_items.item_id",
                "calibration_items.round_id",
            ],
            name="fk_cal_subs_scope_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "resource_version = 1",
            name="ck_cal_subs_resource_version",
        ),
    )
    op.create_index(
        "ix_cal_subs_scope_round_item",
        "calibration_submissions",
        ["tenant_id", "project_id", "round_id", "item_id"],
    )

    op.create_table(
        "calibration_adjudications",
        sa.Column("adjudication_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("adjudicator_id", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("accepted_submission_id", sa.String(128), nullable=True),
        sa.Column("value_json", json_type(), nullable=True),
        sa.Column("canonical_value_sha256", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "adjudication_id",
            name="uq_cal_adjud_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_cal_adjud_scope_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "item_id", "round_id"],
            [
                "calibration_items.tenant_id",
                "calibration_items.project_id",
                "calibration_items.item_id",
                "calibration_items.round_id",
            ],
            name="fk_cal_adjud_scope_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "accepted_submission_id",
                "round_id",
                "item_id",
            ],
            [
                "calibration_submissions.tenant_id",
                "calibration_submissions.project_id",
                "calibration_submissions.submission_id",
                "calibration_submissions.round_id",
                "calibration_submissions.item_id",
            ],
            name="fk_cal_adjud_scope_submission_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "decision IN ('accept_a', 'accept_b', 'revise', 'exclude')",
            name="ck_cal_adjud_decision",
        ),
        sa.CheckConstraint(
            "LENGTH(TRIM(reason)) > 0",
            name="ck_cal_adjud_reason",
        ),
        sa.CheckConstraint(
            "(decision IN ('accept_a', 'accept_b') AND accepted_submission_id IS NOT NULL "
            "AND value_json IS NOT NULL AND canonical_value_sha256 IS NOT NULL) OR "
            "(decision = 'revise' AND accepted_submission_id IS NULL AND "
            "value_json IS NOT NULL AND canonical_value_sha256 IS NOT NULL) OR "
            "(decision = 'exclude' AND accepted_submission_id IS NULL AND "
            "value_json IS NULL AND canonical_value_sha256 IS NULL)",
            name="ck_cal_adjud_resolution",
        ),
        sa.CheckConstraint(
            "resource_version = 1",
            name="ck_cal_adjud_resource_version",
        ),
    )
    op.create_index(
        "ix_cal_adjud_scope_round",
        "calibration_adjudications",
        ["tenant_id", "project_id", "round_id"],
    )

    op.create_table(
        "gold_set_versions",
        sa.Column("gold_set_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("gold_set_key", sa.String(128), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("label_version", sa.String(128), nullable=False),
        sa.Column("rubric_version", sa.String(128), nullable=False),
        sa.Column("sample_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("annotation_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="published", nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("annotation_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("observed_agreement_ppm", sa.Integer(), nullable=False),
        sa.Column("cohen_kappa_micros", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("adjudication_count", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "published_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            name="uq_gold_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            name="uq_gold_versions_scope_round",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_key",
            "version_number",
            name="uq_gold_versions_scope_series",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "round_id",
            name="uq_gold_versions_scope_binding",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "round_id"],
            [
                "calibration_rounds.tenant_id",
                "calibration_rounds.project_id",
                "calibration_rounds.round_id",
            ],
            name="fk_gold_versions_scope_round",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("status = 'published'", name="ck_gold_versions_status"),
        sa.CheckConstraint(
            "version_number > 0 AND sample_count > 0 AND annotation_count >= 0 AND "
            "excluded_count >= 0 AND annotation_count + excluded_count = sample_count AND "
            "conflict_count >= 0 AND conflict_count <= sample_count AND "
            "adjudication_count = conflict_count AND excluded_count <= adjudication_count AND "
            "observed_agreement_ppm BETWEEN 0 AND 1000000 AND "
            "cohen_kappa_micros BETWEEN -1000000 AND 1000000",
            name="ck_gold_versions_metrics",
        ),
        sa.CheckConstraint(
            "resource_version = 1",
            name="ck_gold_versions_resource_version",
        ),
    )
    op.create_index(
        "ix_gold_versions_scope_series",
        "gold_set_versions",
        ["tenant_id", "project_id", "gold_set_key", "version_number"],
    )

    op.create_table(
        "gold_annotations",
        sa.Column("gold_annotation_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("gold_set_version_id", sa.String(128), nullable=False),
        sa.Column("round_id", sa.String(128), nullable=False),
        sa.Column("item_id", sa.String(128), nullable=False),
        sa.Column("source_case_id", sa.String(256), nullable=False),
        sa.Column("evidence_ref", sa.String(1024), nullable=False),
        sa.Column("value_json", json_type(), nullable=False),
        sa.Column("canonical_value_sha256", sa.String(64), nullable=False),
        sa.Column("resolution_source", sa.String(32), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            utc_datetime_type(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_annotation_id",
            name="uq_gold_annotations_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "item_id",
            name="uq_gold_annotations_scope_item",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "gold_set_version_id", "round_id"],
            [
                "gold_set_versions.tenant_id",
                "gold_set_versions.project_id",
                "gold_set_versions.gold_set_version_id",
                "gold_set_versions.round_id",
            ],
            name="fk_gold_annotations_scope_version_binding",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "item_id", "round_id"],
            [
                "calibration_items.tenant_id",
                "calibration_items.project_id",
                "calibration_items.item_id",
                "calibration_items.round_id",
            ],
            name="fk_gold_annotations_scope_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "resolution_source IN ('agreed', 'adjudicated')",
            name="ck_gold_annotations_source",
        ),
        sa.CheckConstraint(
            "resource_version = 1",
            name="ck_gold_annotations_resource_version",
        ),
    )
    op.create_index(
        "ix_gold_annotations_scope_version",
        "gold_annotations",
        ["tenant_id", "project_id", "gold_set_version_id"],
    )


def downgrade() -> None:
    op.drop_table("gold_annotations")
    op.drop_table("gold_set_versions")
    op.drop_table("calibration_adjudications")
    op.drop_table("calibration_submissions")
    op.drop_table("calibration_assignments")
    op.drop_table("calibration_items")
    op.drop_table("calibration_rounds")
