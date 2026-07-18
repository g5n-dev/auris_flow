"""freeze immutable label metric scopes and report result bindings

Revision ID: 0036_label_metric_snapshot_scopes
Revises: 0035_label_fact_temporal_heads
Create Date: 2026-07-18

The five ``metric_results`` columns are intentionally nullable so the legacy
writer remains compatible during the expand phase.  New label-derived metric
snapshots and report bindings are insert-only evidence ledgers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0036_label_metric_snapshot_scopes"
down_revision = "0035_label_fact_temporal_heads"
branch_labels = None
depends_on = None


APPEND_ONLY_TABLES = (
    "metric_results",
    "metric_result_label_scopes",
    "insight_report_metric_bindings",
)


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _create_append_only_triggers() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table_name in APPEND_ONLY_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER trg_{table_name}_no_{action.lower()} "
                        f"BEFORE {action} ON {table_name} "
                        f"BEGIN SELECT RAISE(ABORT, 'append-only {table_name}'); END"
                    )
                )
    elif dialect in {"mysql", "mariadb"}:
        for table_name in APPEND_ONLY_TABLES:
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    sa.text(
                        f"CREATE TRIGGER trg_{table_name}_no_{action.lower()} "
                        f"BEFORE {action} ON {table_name} FOR EACH ROW "
                        "SIGNAL SQLSTATE '45000' "
                        f"SET MESSAGE_TEXT = 'append-only {table_name}'"
                    )
                )


def _drop_append_only_triggers() -> None:
    for table_name in APPEND_ONLY_TABLES:
        for action in ("update", "delete"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_{action}"))


def _expand_metric_results() -> None:
    with op.batch_alter_table("metric_results") as batch_op:
        batch_op.add_column(sa.Column("content_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("source_manifest_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("scope_sha256", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("root_trace_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("action_trace_id", sa.String(128), nullable=True))


def _create_strong_parent_keys() -> None:
    # Unique indexes avoid a SQLite table rewrite, which would otherwise discard
    # the lifecycle triggers already installed on these parent tables.
    op.create_index(
        "uq_label_mapping_bundles_scope_id_hash",
        "label_mapping_bundles",
        [
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "canonical_manifest_sha256",
        ],
        unique=True,
    )
    op.create_index(
        "uq_label_fact_sets_scope_metric_binding",
        "label_fact_sets",
        [
            "tenant_id",
            "project_id",
            "fact_set_id",
            "fact_namespace",
            "manifest_sha256",
            "fact_as_of",
        ],
        unique=True,
    )


def _create_metric_result_label_scopes() -> None:
    op.create_table(
        "metric_result_label_scopes",
        sa.Column("metric_scope_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("metric_result_id", sa.String(128), nullable=False),
        sa.Column("taxonomy_mode", sa.String(32), nullable=False),
        sa.Column("source_label_version_ids", sa.JSON(), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=True),
        sa.Column("mapping_bundle_id", sa.String(128), nullable=True),
        sa.Column("mapping_bundle_sha256", sa.String(64), nullable=True),
        sa.Column("fact_namespace", sa.String(128), nullable=False),
        sa.Column("fact_set_id", sa.String(128), nullable=False),
        sa.Column("fact_set_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("fact_set_generation", sa.Integer(), nullable=False),
        sa.Column("fact_as_of", _utc_datetime(), nullable=False),
        sa.Column("metric_definition_versions", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("period_boundary", sa.String(128), nullable=False),
        sa.Column("denominator_definition", sa.String(512), nullable=False),
        sa.Column("label_version_applicability", sa.String(32), nullable=False),
        sa.Column("comparability_status", sa.String(32), nullable=False),
        sa.Column("comparability_reason_codes", sa.JSON(), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            _utc_datetime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "metric_result_id",
            name="uq_metric_result_label_scopes_scope_result",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scope_sha256",
            name="uq_metric_result_label_scopes_scope_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_metric_result_label_scopes_scope_result",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_metric_result_label_scopes_scope_target_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "mapping_bundle_id",
                "mapping_bundle_sha256",
            ],
            [
                "label_mapping_bundles.tenant_id",
                "label_mapping_bundles.project_id",
                "label_mapping_bundles.mapping_bundle_id",
                "label_mapping_bundles.canonical_manifest_sha256",
            ],
            name="fk_metric_result_label_scopes_scope_mapping_bundle",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "fact_set_id",
                "fact_namespace",
                "fact_set_manifest_sha256",
                "fact_as_of",
            ],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
                "label_fact_sets.fact_namespace",
                "label_fact_sets.manifest_sha256",
                "label_fact_sets.fact_as_of",
            ],
            name="fk_metric_result_label_scopes_scope_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "taxonomy_mode IN ('native', 'normalized', 'recomputed')",
            name="ck_metric_result_label_scopes_mode",
        ),
        sa.CheckConstraint(
            "(taxonomy_mode = 'native' AND target_label_version_id IS NULL "
            "AND mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(taxonomy_mode = 'normalized' AND target_label_version_id IS NOT NULL "
            "AND mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL) OR "
            "(taxonomy_mode = 'recomputed' AND target_label_version_id IS NOT NULL "
            "AND ((mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL)))",
            name="ck_metric_result_label_scopes_mode_binding",
        ),
        sa.CheckConstraint(
            "fact_set_generation > 0",
            name="ck_metric_result_label_scopes_generation",
        ),
        sa.CheckConstraint(
            "label_version_applicability = 'required'",
            name="ck_metric_result_label_scopes_applicability",
        ),
        sa.CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_metric_result_label_scopes_comparability",
        ),
        sa.CheckConstraint(
            "LENGTH(scope_sha256) = 64 AND LENGTH(source_manifest_sha256) = 64 "
            "AND LENGTH(content_sha256) = 64 AND LENGTH(fact_set_manifest_sha256) = 64 "
            "AND (mapping_bundle_sha256 IS NULL OR LENGTH(mapping_bundle_sha256) = 64)",
            name="ck_metric_result_label_scopes_hashes",
        ),
    )
    op.create_index(
        "ix_metric_result_label_scopes_scope_target",
        "metric_result_label_scopes",
        ["tenant_id", "project_id", "taxonomy_mode", "target_label_version_id"],
    )
    op.create_index(
        "ix_metric_result_label_scopes_scope_fact_cutoff",
        "metric_result_label_scopes",
        [
            "tenant_id",
            "project_id",
            "fact_namespace",
            "fact_set_generation",
            "fact_as_of",
        ],
    )
    op.create_index(
        "ix_metric_result_label_scopes_trace_id",
        "metric_result_label_scopes",
        ["trace_id"],
    )


def _create_insight_report_metric_bindings() -> None:
    op.create_table(
        "insight_report_metric_bindings",
        sa.Column("report_metric_binding_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("report_id", sa.String(128), nullable=False),
        sa.Column("metric_result_ids", sa.JSON(), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("metric_scope_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("action_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            _utc_datetime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "report_id",
            name="uq_insight_report_metric_bindings_scope_report",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_insight_report_metric_bindings_scope_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "report_id"],
            [
                "insight_reports.tenant_id",
                "insight_reports.project_id",
                "insight_reports.report_id",
            ],
            name="fk_insight_report_metric_bindings_scope_report",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "LENGTH(metric_scope_sha256) = 64 AND LENGTH(content_sha256) = 64",
            name="ck_insight_report_metric_bindings_hashes",
        ),
        sa.CheckConstraint(
            "result_count > 0",
            name="ck_insight_report_metric_bindings_result_count",
        ),
    )
    op.create_index(
        "ix_insight_report_metric_bindings_trace_id",
        "insight_report_metric_bindings",
        ["trace_id"],
    )


def upgrade() -> None:
    _expand_metric_results()
    _create_strong_parent_keys()
    _create_metric_result_label_scopes()
    _create_insight_report_metric_bindings()
    _create_append_only_triggers()


def downgrade() -> None:
    _drop_append_only_triggers()
    op.drop_table("insight_report_metric_bindings")
    op.drop_table("metric_result_label_scopes")
    op.drop_index(
        "uq_label_fact_sets_scope_metric_binding",
        table_name="label_fact_sets",
    )
    op.drop_index(
        "uq_label_mapping_bundles_scope_id_hash",
        table_name="label_mapping_bundles",
    )
    with op.batch_alter_table("metric_results") as batch_op:
        for column_name in (
            "action_trace_id",
            "root_trace_id",
            "scope_sha256",
            "source_manifest_sha256",
            "content_sha256",
        ):
            batch_op.drop_column(column_name)
