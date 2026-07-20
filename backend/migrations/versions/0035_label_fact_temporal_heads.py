"""expand temporal label facts, heads, and fact-set manifests

Revision ID: 0035_label_fact_temporal_heads
Revises: 0034_label_lifecycle_mapping_expand
Create Date: 2026-07-18

This revision is intentionally expand-only.  Existing fact rows are not
backfilled, ``aggregate_id`` stays non-null, and the legacy active-slot writer
continues to work.  A later enforce revision may tighten the source union and
install full LabelFact immutability after scoped backfill and read cutover.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0035_label_fact_temporal_heads"
down_revision = "0034_label_lifecycle_mapping_expand"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _create_fact_sets() -> None:
    op.create_table(
        "label_fact_sets",
        sa.Column("fact_set_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("fact_as_of", _utc_datetime(), nullable=False),
        sa.Column("partition_manifest", sa.JSON(), nullable=False),
        sa.Column("partition_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("result_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", _utc_datetime(), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_id",
            name="uq_label_fact_sets_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "manifest_sha256",
            name="uq_label_fact_sets_scope_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_id",
            "fact_namespace",
            "manifest_sha256",
            name="uq_label_fact_sets_scope_binding",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_fact_sets_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'validated', 'approved', 'published', "
            "'superseded', 'archived')",
            name="ck_label_fact_sets_status",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_label_fact_sets_row_count"),
        sa.CheckConstraint(
            "LENGTH(partition_manifest_sha256) = 64 AND "
            "LENGTH(source_manifest_sha256) = 64 AND "
            "LENGTH(result_manifest_sha256) = 64 AND LENGTH(manifest_sha256) = 64",
            name="ck_label_fact_sets_hash_lengths",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('approved', 'published')) OR "
            "(approval_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_label_fact_sets_approval",
        ),
    )
    op.create_index(
        "ix_label_fact_sets_scope_status",
        "label_fact_sets",
        ["tenant_id", "project_id", "fact_namespace", "status"],
    )
    op.create_index(
        "ix_label_fact_sets_scope_target",
        "label_fact_sets",
        ["tenant_id", "project_id", "target_label_version_id", "fact_as_of"],
    )
    op.create_index("ix_label_fact_sets_trace_id", "label_fact_sets", ["trace_id"])


def _expand_label_facts() -> None:
    with op.batch_alter_table("label_facts") as batch_op:
        batch_op.add_column(sa.Column("fact_namespace", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("logical_key_sha", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("revision", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("event_or_segment_id", sa.String(256), nullable=True))
        batch_op.add_column(sa.Column("assertion_slot", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("occurred_at", _utc_datetime(), nullable=True))
        batch_op.add_column(sa.Column("recorded_at", _utc_datetime(), nullable=True))
        batch_op.add_column(sa.Column("occurred_at_origin", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("source_kind", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("human_review_decision_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("recompute_run_item_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("fact_set_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("content_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("root_trace_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("action_trace_id", sa.String(128), nullable=True))
        batch_op.create_unique_constraint(
            "uq_label_facts_temporal_revision",
            ["tenant_id", "project_id", "fact_namespace", "logical_key_sha", "revision"],
        )
        batch_op.create_unique_constraint(
            "uq_label_facts_temporal_head_binding",
            [
                "tenant_id",
                "project_id",
                "fact_namespace",
                "logical_key_sha",
                "revision",
                "fact_id",
            ],
        )
        batch_op.create_foreign_key(
            "fk_label_facts_scope_human_decision",
            "human_review_decisions",
            ["tenant_id", "project_id", "human_review_decision_id"],
            ["tenant_id", "project_id", "decision_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_label_facts_scope_fact_set",
            "label_fact_sets",
            ["tenant_id", "project_id", "fact_set_id"],
            ["tenant_id", "project_id", "fact_set_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_temporal_revision",
            "revision IS NULL OR revision > 0",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_temporal_hashes",
            "(logical_key_sha IS NULL OR LENGTH(logical_key_sha) = 64) AND "
            "(content_sha256 IS NULL OR LENGTH(content_sha256) = 64)",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_occurred_origin",
            "occurred_at_origin IS NULL OR occurred_at_origin IN "
            "('source', 'legacy-recorded-fallback', 'authorized-backfill')",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_recompute_reserved",
            "recompute_run_item_id IS NULL",
        )
        # ``aggregate_id`` deliberately remains non-null for the legacy writer.
        # Therefore human-decision rows may still carry the reviewed aggregate as
        # a compatibility projection until the later enforce migration.
        batch_op.create_check_constraint(
            "ck_label_facts_expand_source",
            "source_kind IS NULL OR "
            "(source_kind = 'aggregate' AND aggregate_id IS NOT NULL "
            "AND human_review_decision_id IS NULL) OR "
            "(source_kind = 'human-decision' AND human_review_decision_id IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_label_facts_temporal_completeness",
            "source_kind IS NULL OR (fact_namespace IS NOT NULL AND "
            "logical_key_sha IS NOT NULL AND revision IS NOT NULL AND "
            "event_or_segment_id IS NOT NULL AND assertion_slot IS NOT NULL AND "
            "occurred_at IS NOT NULL AND recorded_at IS NOT NULL AND "
            "occurred_at_origin IS NOT NULL AND content_sha256 IS NOT NULL AND "
            "root_trace_id IS NOT NULL AND action_trace_id IS NOT NULL)",
        )
    op.create_index(
        "ix_label_facts_temporal_as_of",
        "label_facts",
        [
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            "recorded_at",
            "revision",
        ],
    )
    op.create_index(
        "ix_label_facts_temporal_occurred",
        "label_facts",
        [
            "tenant_id",
            "project_id",
            "fact_namespace",
            "occurred_at",
            "label_version_id",
            "label_id",
        ],
    )
    op.create_index(
        "ix_label_facts_temporal_source",
        "label_facts",
        ["tenant_id", "project_id", "source_kind", "human_review_decision_id"],
    )
    op.create_index(
        "ix_label_facts_scope_fact_set",
        "label_facts",
        ["tenant_id", "project_id", "fact_set_id"],
    )


def _create_fact_heads() -> None:
    op.create_table(
        "label_fact_heads",
        sa.Column("fact_head_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("logical_key_sha", sa.String(64), nullable=False),
        sa.Column("current_fact_id", sa.String(128), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_head_id",
            name="uq_label_fact_heads_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            name="uq_label_fact_heads_scope_key",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "fact_namespace",
                "logical_key_sha",
                "current_revision",
                "current_fact_id",
            ],
            [
                "label_facts.tenant_id",
                "label_facts.project_id",
                "label_facts.fact_namespace",
                "label_facts.logical_key_sha",
                "label_facts.revision",
                "label_facts.fact_id",
            ],
            name="fk_label_fact_heads_scope_current",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "current_revision > 0 AND generation > 0",
            name="ck_label_fact_heads_versions",
        ),
        sa.CheckConstraint(
            "LENGTH(logical_key_sha) = 64",
            name="ck_label_fact_heads_logical_hash",
        ),
    )
    op.create_index(
        "ix_label_fact_heads_scope_current",
        "label_fact_heads",
        ["tenant_id", "project_id", "fact_namespace", "current_fact_id"],
    )
    op.create_index("ix_label_fact_heads_trace_id", "label_fact_heads", ["trace_id"])


def _create_fact_set_heads() -> None:
    op.create_table(
        "label_fact_set_heads",
        sa.Column("fact_set_head_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("current_fact_set_id", sa.String(128), nullable=False),
        sa.Column("current_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("previous_fact_set_id", sa.String(128), nullable=True),
        sa.Column("previous_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_head_id",
            name="uq_label_fact_set_heads_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "fact_namespace",
            name="uq_label_fact_set_heads_scope_env",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "current_fact_set_id",
                "fact_namespace",
                "current_manifest_sha256",
            ],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
                "label_fact_sets.fact_namespace",
                "label_fact_sets.manifest_sha256",
            ],
            name="fk_label_fact_set_heads_scope_current",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "previous_fact_set_id",
                "fact_namespace",
                "previous_manifest_sha256",
            ],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
                "label_fact_sets.fact_namespace",
                "label_fact_sets.manifest_sha256",
            ],
            name="fk_label_fact_set_heads_scope_previous",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("generation > 0", name="ck_label_fact_set_heads_generation"),
        sa.CheckConstraint("status = 'active'", name="ck_label_fact_set_heads_status"),
        sa.CheckConstraint(
            "(previous_fact_set_id IS NULL AND previous_manifest_sha256 IS NULL) OR "
            "(previous_fact_set_id IS NOT NULL AND previous_manifest_sha256 IS NOT NULL)",
            name="ck_label_fact_set_heads_previous_pair",
        ),
        sa.CheckConstraint(
            "LENGTH(current_manifest_sha256) = 64 AND "
            "(previous_manifest_sha256 IS NULL OR LENGTH(previous_manifest_sha256) = 64)",
            name="ck_label_fact_set_heads_hashes",
        ),
    )
    op.create_index(
        "ix_label_fact_set_heads_scope_current",
        "label_fact_set_heads",
        ["tenant_id", "project_id", "environment", "current_fact_set_id"],
    )
    op.create_index("ix_label_fact_set_heads_trace_id", "label_fact_set_heads", ["trace_id"])


def _create_fact_set_head_events() -> None:
    op.create_table(
        "label_fact_set_head_events",
        sa.Column("head_event_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_generation", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("old_fact_set_id", sa.String(128), nullable=True),
        sa.Column("old_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("new_fact_set_id", sa.String(128), nullable=False),
        sa.Column("new_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=True),
        sa.Column("effective_at", _utc_datetime(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "fact_namespace",
            "generation",
            name="uq_label_fact_set_events_generation",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_fact_set_events_hash",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "old_fact_set_id",
                "fact_namespace",
                "old_manifest_sha256",
            ],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
                "label_fact_sets.fact_namespace",
                "label_fact_sets.manifest_sha256",
            ],
            name="fk_label_fact_set_events_scope_old",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "new_fact_set_id",
                "fact_namespace",
                "new_manifest_sha256",
            ],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
                "label_fact_sets.fact_namespace",
                "label_fact_sets.manifest_sha256",
            ],
            name="fk_label_fact_set_events_scope_new",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_label_fact_set_events_generation",
        ),
        sa.CheckConstraint(
            "(generation = 1 AND previous_generation IS NULL) OR "
            "(generation > 1 AND previous_generation = generation - 1)",
            name="ck_label_fact_set_events_previous_generation",
        ),
        sa.CheckConstraint(
            "action IN ('bootstrap', 'promote', 'rollback')",
            name="ck_label_fact_set_events_action",
        ),
        sa.CheckConstraint(
            "(old_fact_set_id IS NULL AND old_manifest_sha256 IS NULL) OR "
            "(old_fact_set_id IS NOT NULL AND old_manifest_sha256 IS NOT NULL)",
            name="ck_label_fact_set_events_old_pair",
        ),
        sa.CheckConstraint(
            "(action = 'bootstrap' AND generation = 1 AND old_fact_set_id IS NULL) OR "
            "(action IN ('promote', 'rollback') AND generation > 1 "
            "AND old_fact_set_id IS NOT NULL)",
            name="ck_label_fact_set_events_transition",
        ),
        sa.CheckConstraint(
            "LENGTH(new_manifest_sha256) = 64 AND LENGTH(content_sha256) = 64 AND "
            "(old_manifest_sha256 IS NULL OR LENGTH(old_manifest_sha256) = 64)",
            name="ck_label_fact_set_events_hashes",
        ),
    )
    op.create_index(
        "ix_label_fact_set_events_scope_timeline",
        "label_fact_set_head_events",
        ["tenant_id", "project_id", "environment", "fact_namespace", "generation"],
    )
    op.create_index(
        "ix_label_fact_set_events_scope_new_set",
        "label_fact_set_head_events",
        ["tenant_id", "project_id", "new_fact_set_id", "effective_at"],
    )
    op.create_index(
        "ix_label_fact_set_events_trace_id",
        "label_fact_set_head_events",
        ["trace_id"],
    )


def _create_head_event_append_only_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_fact_set_head_events_no_{action.lower()} "
                    f"BEFORE {action} ON label_fact_set_head_events "
                    "BEGIN SELECT RAISE(ABORT, "
                    "'append-only label fact set head event'); END"
                )
            )
    elif dialect in {"mysql", "mariadb"}:
        for action in ("UPDATE", "DELETE"):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_label_fact_set_head_events_no_{action.lower()} "
                    f"BEFORE {action} ON label_fact_set_head_events FOR EACH ROW "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'append-only label fact set head event'"
                )
            )


def _drop_head_event_append_only_triggers() -> None:
    for action in ("update", "delete"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_label_fact_set_head_events_no_{action}"))


def upgrade() -> None:
    _create_fact_sets()
    _expand_label_facts()
    _create_fact_heads()
    _create_fact_set_heads()
    _create_fact_set_head_events()
    _create_head_event_append_only_triggers()


def downgrade() -> None:
    _drop_head_event_append_only_triggers()
    op.drop_table("label_fact_set_head_events")
    op.drop_table("label_fact_set_heads")
    op.drop_table("label_fact_heads")

    with op.batch_alter_table("label_facts") as batch_op:
        batch_op.drop_constraint("ck_label_facts_temporal_completeness", type_="check")
        batch_op.drop_constraint("ck_label_facts_expand_source", type_="check")
        batch_op.drop_constraint("ck_label_facts_recompute_reserved", type_="check")
        batch_op.drop_constraint("ck_label_facts_occurred_origin", type_="check")
        batch_op.drop_constraint("ck_label_facts_temporal_hashes", type_="check")
        batch_op.drop_constraint("ck_label_facts_temporal_revision", type_="check")
        batch_op.drop_constraint("fk_label_facts_scope_fact_set", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_label_facts_scope_human_decision",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_label_facts_temporal_head_binding",
            type_="unique",
        )
        batch_op.drop_constraint("uq_label_facts_temporal_revision", type_="unique")
    # MySQL may use these explicit indexes to enforce the foreign keys above.
    # Remove the constraints first, then their supporting indexes.
    for index_name in (
        "ix_label_facts_scope_fact_set",
        "ix_label_facts_temporal_source",
        "ix_label_facts_temporal_occurred",
        "ix_label_facts_temporal_as_of",
    ):
        op.drop_index(index_name, table_name="label_facts")
    with op.batch_alter_table("label_facts") as batch_op:
        for column_name in (
            "action_trace_id",
            "root_trace_id",
            "content_sha256",
            "fact_set_id",
            "recompute_run_item_id",
            "human_review_decision_id",
            "source_kind",
            "occurred_at_origin",
            "recorded_at",
            "occurred_at",
            "assertion_slot",
            "event_or_segment_id",
            "revision",
            "logical_key_sha",
            "fact_namespace",
        ):
            batch_op.drop_column(column_name)
    op.drop_table("label_fact_sets")
