"""expand label artifact lifecycle, mapping bundles, and activation ledger

Revision ID: 0034_label_lifecycle_mapping_expand
Revises: 0033_task_version_release_heads
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0034_label_lifecycle_mapping_expand"
down_revision = "0033_task_version_release_heads"
branch_labels = None
depends_on = None


APPEND_ONLY_TABLES = (
    "label_mapping_items",
    "label_mapping_item_targets",
    "label_mapping_bundle_sources",
    "label_mapping_bundle_members",
    "label_mapping_bundle_paths",
    "release_bundle_head_events",
)


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def _create_append_only_triggers() -> None:
    dialect = op.get_bind().dialect.name
    protected_statuses = "('published', 'superseded', 'archived')"
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
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_item_targets_no_retire "
                "BEFORE INSERT ON label_mapping_item_targets "
                "WHEN EXISTS (SELECT 1 FROM label_mapping_items item "
                "WHERE item.tenant_id = NEW.tenant_id "
                "AND item.project_id = NEW.project_id "
                "AND item.mapping_version_id = NEW.mapping_version_id "
                "AND item.mapping_item_id = NEW.mapping_item_id "
                "AND item.relation = 'retire') "
                "BEGIN SELECT RAISE(ABORT, 'retire mapping cannot have targets'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_items_no_published_insert "
                "BEFORE INSERT ON label_mapping_items "
                "WHEN EXISTS (SELECT 1 FROM label_mapping_versions mapping_artifact "
                "WHERE mapping_artifact.tenant_id = NEW.tenant_id "
                "AND mapping_artifact.project_id = NEW.project_id "
                "AND mapping_artifact.mapping_version_id = NEW.mapping_version_id "
                f"AND mapping_artifact.status IN {protected_statuses}) "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping is closed'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_item_targets_no_published_insert "
                "BEFORE INSERT ON label_mapping_item_targets "
                "WHEN EXISTS (SELECT 1 FROM label_mapping_items item "
                "JOIN label_mapping_versions mapping_artifact "
                "ON mapping_artifact.tenant_id = item.tenant_id "
                "AND mapping_artifact.project_id = item.project_id "
                "AND mapping_artifact.mapping_version_id = item.mapping_version_id "
                "WHERE item.tenant_id = NEW.tenant_id "
                "AND item.project_id = NEW.project_id "
                "AND item.mapping_version_id = NEW.mapping_version_id "
                "AND item.mapping_item_id = NEW.mapping_item_id "
                f"AND mapping_artifact.status IN {protected_statuses}) "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping is closed'); END"
            )
        )
        for table_name in (
            "label_mapping_bundle_sources",
            "label_mapping_bundle_members",
            "label_mapping_bundle_paths",
        ):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table_name}_no_published_insert "
                    f"BEFORE INSERT ON {table_name} "
                    "WHEN EXISTS (SELECT 1 FROM label_mapping_bundles bundle "
                    "WHERE bundle.tenant_id = NEW.tenant_id "
                    "AND bundle.project_id = NEW.project_id "
                    "AND bundle.mapping_bundle_id = NEW.mapping_bundle_id "
                    f"AND bundle.status IN {protected_statuses}) "
                    "BEGIN SELECT RAISE(ABORT, 'published label mapping bundle is closed'); END"
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
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_item_targets_no_retire "
                "BEFORE INSERT ON label_mapping_item_targets FOR EACH ROW "
                "BEGIN IF EXISTS (SELECT 1 FROM label_mapping_items item "
                "WHERE item.tenant_id = NEW.tenant_id "
                "AND item.project_id = NEW.project_id "
                "AND item.mapping_version_id = NEW.mapping_version_id "
                "AND item.mapping_item_id = NEW.mapping_item_id "
                "AND item.relation = 'retire') THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'retire mapping cannot have targets'; END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_items_no_published_insert "
                "BEFORE INSERT ON label_mapping_items FOR EACH ROW "
                "BEGIN IF EXISTS (SELECT 1 FROM label_mapping_versions mapping_artifact "
                "WHERE mapping_artifact.tenant_id = NEW.tenant_id "
                "AND mapping_artifact.project_id = NEW.project_id "
                "AND mapping_artifact.mapping_version_id = NEW.mapping_version_id "
                f"AND mapping_artifact.status IN {protected_statuses}) THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping is closed'; END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_item_targets_no_published_insert "
                "BEFORE INSERT ON label_mapping_item_targets FOR EACH ROW "
                "BEGIN IF EXISTS (SELECT 1 FROM label_mapping_items item "
                "JOIN label_mapping_versions mapping_artifact "
                "ON mapping_artifact.tenant_id = item.tenant_id "
                "AND mapping_artifact.project_id = item.project_id "
                "AND mapping_artifact.mapping_version_id = item.mapping_version_id "
                "WHERE item.tenant_id = NEW.tenant_id "
                "AND item.project_id = NEW.project_id "
                "AND item.mapping_version_id = NEW.mapping_version_id "
                "AND item.mapping_item_id = NEW.mapping_item_id "
                f"AND mapping_artifact.status IN {protected_statuses}) THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping is closed'; END IF; END"
            )
        )
        for table_name in (
            "label_mapping_bundle_sources",
            "label_mapping_bundle_members",
            "label_mapping_bundle_paths",
        ):
            op.execute(
                sa.text(
                    f"CREATE TRIGGER trg_{table_name}_no_published_insert "
                    f"BEFORE INSERT ON {table_name} FOR EACH ROW "
                    "BEGIN IF EXISTS (SELECT 1 FROM label_mapping_bundles bundle "
                    "WHERE bundle.tenant_id = NEW.tenant_id "
                    "AND bundle.project_id = NEW.project_id "
                    "AND bundle.mapping_bundle_id = NEW.mapping_bundle_id "
                    f"AND bundle.status IN {protected_statuses}) THEN "
                    "SIGNAL SQLSTATE '45000' "
                    "SET MESSAGE_TEXT = 'published label mapping bundle is closed'; END IF; END"
                )
            )


def _drop_append_only_triggers() -> None:
    for trigger_name in (
        "trg_label_mapping_items_no_published_insert",
        "trg_label_mapping_item_targets_no_published_insert",
        "trg_label_mapping_bundle_sources_no_published_insert",
        "trg_label_mapping_bundle_members_no_published_insert",
        "trg_label_mapping_bundle_paths_no_published_insert",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_label_mapping_item_targets_no_retire"))
    for table_name in APPEND_ONLY_TABLES:
        for action in ("update", "delete"):
            op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table_name}_no_{action}"))


def _create_published_artifact_triggers() -> None:
    dialect = op.get_bind().dialect.name
    protected_statuses = "('published', 'superseded', 'archived')"
    if dialect == "sqlite":
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_versions_published_update "
                "BEFORE UPDATE ON label_mapping_versions "
                f"WHEN ((OLD.status IN {protected_statuses} OR NEW.status = 'published') AND ("
                "NEW.source_label_version_id <> OLD.source_label_version_id OR "
                "NEW.target_label_version_id <> OLD.target_label_version_id OR "
                "NEW.mapping_version <> OLD.mapping_version OR "
                "NEW.source_resource_version <> OLD.source_resource_version OR "
                "NEW.target_resource_version <> OLD.target_resource_version OR "
                "NEW.content_sha256 <> OLD.content_sha256 OR NEW.payload <> OLD.payload)) OR "
                "(OLD.status = 'published' AND NEW.status NOT IN "
                "('published', 'superseded', 'archived')) OR "
                "(OLD.status = 'superseded' AND NEW.status NOT IN ('superseded', 'archived')) OR "
                "(OLD.status = 'archived' AND NEW.status <> 'archived') "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping is immutable'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_versions_published_delete "
                "BEFORE DELETE ON label_mapping_versions "
                f"WHEN OLD.status IN {protected_statuses} "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping is immutable'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_bundles_published_update "
                "BEFORE UPDATE ON label_mapping_bundles "
                f"WHEN ((OLD.status IN {protected_statuses} OR NEW.status = 'published') AND ("
                "NEW.target_label_version_id <> OLD.target_label_version_id OR "
                "NEW.source_label_version_ids <> OLD.source_label_version_ids OR "
                "NEW.source_manifest_sha256 <> OLD.source_manifest_sha256 OR "
                "NEW.compiler_version <> OLD.compiler_version OR "
                "NEW.canonical_manifest_sha256 <> OLD.canonical_manifest_sha256 OR "
                "NEW.payload <> OLD.payload)) OR "
                "(OLD.status = 'published' AND NEW.status NOT IN "
                "('published', 'superseded', 'archived')) OR "
                "(OLD.status = 'superseded' AND NEW.status NOT IN ('superseded', 'archived')) OR "
                "(OLD.status = 'archived' AND NEW.status <> 'archived') "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping bundle is immutable'); END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_bundles_published_delete "
                "BEFORE DELETE ON label_mapping_bundles "
                f"WHEN OLD.status IN {protected_statuses} "
                "BEGIN SELECT RAISE(ABORT, 'published label mapping bundle is immutable'); END"
            )
        )
    elif dialect in {"mysql", "mariadb"}:
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_versions_published_update "
                "BEFORE UPDATE ON label_mapping_versions FOR EACH ROW "
                f"BEGIN IF ((OLD.status IN {protected_statuses} OR NEW.status = 'published') AND ("
                "NOT (NEW.source_label_version_id <=> OLD.source_label_version_id) OR "
                "NOT (NEW.target_label_version_id <=> OLD.target_label_version_id) OR "
                "NOT (NEW.mapping_version <=> OLD.mapping_version) OR "
                "NOT (NEW.source_resource_version <=> OLD.source_resource_version) OR "
                "NOT (NEW.target_resource_version <=> OLD.target_resource_version) OR "
                "NOT (NEW.content_sha256 <=> OLD.content_sha256) OR "
                "NOT (NEW.payload <=> OLD.payload))) OR "
                "(OLD.status = 'published' AND NEW.status NOT IN "
                "('published', 'superseded', 'archived')) OR "
                "(OLD.status = 'superseded' AND NEW.status NOT IN ('superseded', 'archived')) OR "
                "(OLD.status = 'archived' AND NEW.status <> 'archived') THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping is immutable'; END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_versions_published_delete "
                "BEFORE DELETE ON label_mapping_versions FOR EACH ROW "
                f"BEGIN IF OLD.status IN {protected_statuses} THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping is immutable'; END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_bundles_published_update "
                "BEFORE UPDATE ON label_mapping_bundles FOR EACH ROW "
                f"BEGIN IF ((OLD.status IN {protected_statuses} OR NEW.status = 'published') AND ("
                "NOT (NEW.target_label_version_id <=> OLD.target_label_version_id) OR "
                "NOT (NEW.source_label_version_ids <=> OLD.source_label_version_ids) OR "
                "NOT (NEW.source_manifest_sha256 <=> OLD.source_manifest_sha256) OR "
                "NOT (NEW.compiler_version <=> OLD.compiler_version) OR "
                "NOT (NEW.canonical_manifest_sha256 <=> OLD.canonical_manifest_sha256) OR "
                "NOT (NEW.payload <=> OLD.payload))) OR "
                "(OLD.status = 'published' AND NEW.status NOT IN "
                "('published', 'superseded', 'archived')) OR "
                "(OLD.status = 'superseded' AND NEW.status NOT IN ('superseded', 'archived')) OR "
                "(OLD.status = 'archived' AND NEW.status <> 'archived') THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping bundle is immutable'; END IF; END"
            )
        )
        op.execute(
            sa.text(
                "CREATE TRIGGER trg_label_mapping_bundles_published_delete "
                "BEFORE DELETE ON label_mapping_bundles FOR EACH ROW "
                f"BEGIN IF OLD.status IN {protected_statuses} THEN "
                "SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'published label mapping bundle is immutable'; END IF; END"
            )
        )


def _drop_published_artifact_triggers() -> None:
    for trigger_name in (
        "trg_label_mapping_versions_published_update",
        "trg_label_mapping_versions_published_delete",
        "trg_label_mapping_bundles_published_update",
        "trg_label_mapping_bundles_published_delete",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger_name}"))


def _create_label_taxonomies() -> None:
    op.create_table(
        "label_taxonomies",
        sa.Column("taxonomy_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "taxonomy_id",
            name="uq_label_taxonomies_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            name="uq_label_taxonomies_scope_name",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_taxonomies_scope_hash",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'inactive', 'archived')",
            name="ck_label_taxonomies_status",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_label_taxonomies_resource_version",
        ),
    )
    op.create_index(
        "ix_label_taxonomies_scope_status",
        "label_taxonomies",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index("ix_label_taxonomies_trace_id", "label_taxonomies", ["trace_id"])


def _expand_label_versions() -> None:
    with op.batch_alter_table("label_versions") as batch_op:
        batch_op.add_column(sa.Column("taxonomy_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("semantic_version", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("base_label_version_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("artifact_status", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("artifact_published_at", _utc_datetime(), nullable=True))
        batch_op.add_column(sa.Column("artifact_deprecated_at", _utc_datetime(), nullable=True))
        batch_op.add_column(sa.Column("deprecation_reason", sa.String(1024), nullable=True))
        batch_op.add_column(
            sa.Column("replacement_label_version_id", sa.String(128), nullable=True)
        )
        batch_op.add_column(sa.Column("content_sha256", sa.String(64), nullable=True))
        batch_op.create_unique_constraint(
            "uq_label_versions_scope_taxonomy_semver",
            ["tenant_id", "project_id", "taxonomy_id", "semantic_version"],
        )
        batch_op.create_unique_constraint(
            "uq_label_versions_scope_taxonomy_id",
            ["tenant_id", "project_id", "taxonomy_id", "label_version_id"],
        )
        batch_op.create_foreign_key(
            "fk_label_versions_scope_taxonomy",
            "label_taxonomies",
            ["tenant_id", "project_id", "taxonomy_id"],
            ["tenant_id", "project_id", "taxonomy_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_label_versions_scope_base",
            "label_versions",
            ["tenant_id", "project_id", "taxonomy_id", "base_label_version_id"],
            ["tenant_id", "project_id", "taxonomy_id", "label_version_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_label_versions_scope_replacement",
            "label_versions",
            ["tenant_id", "project_id", "taxonomy_id", "replacement_label_version_id"],
            ["tenant_id", "project_id", "taxonomy_id", "label_version_id"],
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_label_versions_artifact_status",
            "artifact_status IS NULL OR artifact_status IN ("
            "'draft', 'candidate', 'validated', 'locked', 'evaluating', "
            "'gate_blocked', 'review_required', 'approved', 'published', "
            "'deprecated', 'archived')",
        )
    op.create_index(
        "ix_label_versions_scope_artifact_status",
        "label_versions",
        ["tenant_id", "project_id", "artifact_status"],
    )
    op.create_index(
        "ix_label_versions_scope_taxonomy",
        "label_versions",
        ["tenant_id", "project_id", "taxonomy_id", "semantic_version"],
    )
    op.create_index(
        "ix_label_versions_scope_replacement",
        "label_versions",
        ["tenant_id", "project_id", "replacement_label_version_id"],
    )

    with op.batch_alter_table("label_version_items") as batch_op:
        batch_op.add_column(sa.Column("definition_sha256", sa.String(64), nullable=True))
        batch_op.create_check_constraint(
            "ck_label_version_items_status",
            "definition_sha256 IS NULL OR status IN ('active', 'retired', 'pending-configuration')",
        )
    op.create_index(
        "ix_label_version_items_scope_status",
        "label_version_items",
        ["tenant_id", "project_id", "label_version_id", "status", "label_id"],
    )


def _create_mapping_tables() -> None:
    op.create_table(
        "label_mapping_versions",
        sa.Column("mapping_version_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("source_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("mapping_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("source_resource_version", sa.Integer(), nullable=False),
        sa.Column("target_resource_version", sa.Integer(), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", _utc_datetime(), nullable=True),
        sa.Column("published_at", _utc_datetime(), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            name="uq_label_mapping_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_label_version_id",
            "target_label_version_id",
            "mapping_version",
            name="uq_label_mapping_versions_scope_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_mapping_versions_scope_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_version_id",
            "target_label_version_id",
            name="uq_label_mapping_versions_scope_pair_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_version_id",
            "target_label_version_id",
            "content_sha256",
            name="uq_label_mapping_versions_scope_edge_binding",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_mapping_versions_scope_source",
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
            name="fk_label_mapping_versions_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_label_version_id <> target_label_version_id",
            name="ck_label_mapping_versions_distinct_pair",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'validated', 'review_required', 'approved', "
            "'published', 'superseded', 'archived')",
            name="ck_label_mapping_versions_status",
        ),
        sa.CheckConstraint(
            "source_resource_version > 0 AND target_resource_version > 0 AND resource_version > 0",
            name="ck_label_mapping_versions_resource_versions",
        ),
    )
    op.create_index(
        "ix_label_mapping_versions_scope_status",
        "label_mapping_versions",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_mapping_versions_scope_pair",
        "label_mapping_versions",
        ["tenant_id", "project_id", "source_label_version_id", "target_label_version_id"],
    )
    op.create_index(
        "ix_label_mapping_versions_trace_id",
        "label_mapping_versions",
        ["trace_id"],
    )

    op.create_table(
        "label_mapping_items",
        sa.Column("mapping_item_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mapping_version_id", sa.String(128), nullable=False),
        sa.Column("source_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("source_label_id", sa.String(128), nullable=False),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("compatibility", sa.String(32), nullable=False),
        sa.Column("comparability_status", sa.String(32), nullable=False),
        sa.Column("allowed_metric_families", sa.JSON(), nullable=False),
        sa.Column("metric_grain", sa.String(64), nullable=True),
        sa.Column("lineage_key", sa.String(128), nullable=True),
        sa.Column("reducer", sa.String(64), nullable=True),
        sa.Column("requires_recompute", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_semantic_sha256", sa.String(64), nullable=True),
        sa.Column("target_semantic_sha256", sa.String(64), nullable=True),
        sa.Column("compatibility_evidence_ref", sa.JSON(), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_id",
            name="uq_label_mapping_items_source_disposition",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "content_sha256",
            name="uq_label_mapping_items_scope_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "mapping_item_id",
            "target_label_version_id",
            name="uq_label_mapping_items_scope_target_parent",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "mapping_version_id",
                "source_label_version_id",
                "target_label_version_id",
            ],
            [
                "label_mapping_versions.tenant_id",
                "label_mapping_versions.project_id",
                "label_mapping_versions.mapping_version_id",
                "label_mapping_versions.source_label_version_id",
                "label_mapping_versions.target_label_version_id",
            ],
            name="fk_label_mapping_items_scope_version_pair",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_label_version_id", "source_label_id"],
            [
                "label_version_items.tenant_id",
                "label_version_items.project_id",
                "label_version_items.label_version_id",
                "label_version_items.label_id",
            ],
            name="fk_label_mapping_items_scope_source_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "relation IN ('identity', 'rename', 'replace', 'merge', 'retire', 'split-recompute')",
            name="ck_label_mapping_items_relation",
        ),
        sa.CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_label_mapping_items_comparability",
        ),
        sa.CheckConstraint(
            "compatibility IN ('exact', 'metric-dependent', 'structural-break', 'not-applicable')",
            name="ck_label_mapping_items_compatibility",
        ),
        sa.CheckConstraint(
            "relation <> 'split-recompute' OR requires_recompute = 1",
            name="ck_label_mapping_items_split_recompute",
        ),
    )
    op.create_index(
        "ix_label_mapping_items_scope_relation",
        "label_mapping_items",
        ["tenant_id", "project_id", "mapping_version_id", "relation"],
    )
    op.create_index("ix_label_mapping_items_trace_id", "label_mapping_items", ["trace_id"])

    op.create_table(
        "label_mapping_item_targets",
        sa.Column("mapping_item_target_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mapping_version_id", sa.String(128), nullable=False),
        sa.Column("mapping_item_id", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_label_id", sa.String(128), nullable=False),
        sa.Column("target_order", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_item_id",
            "target_label_id",
            name="uq_label_mapping_item_targets_label",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_item_id",
            "target_order",
            name="uq_label_mapping_item_targets_order",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "mapping_version_id",
                "mapping_item_id",
                "target_label_version_id",
            ],
            [
                "label_mapping_items.tenant_id",
                "label_mapping_items.project_id",
                "label_mapping_items.mapping_version_id",
                "label_mapping_items.mapping_item_id",
                "label_mapping_items.target_label_version_id",
            ],
            name="fk_label_mapping_item_targets_scope_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id", "target_label_id"],
            [
                "label_version_items.tenant_id",
                "label_version_items.project_id",
                "label_version_items.label_version_id",
                "label_version_items.label_id",
            ],
            name="fk_label_mapping_item_targets_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "target_order >= 0",
            name="ck_label_mapping_item_targets_order",
        ),
    )
    op.create_index(
        "ix_label_mapping_item_targets_scope_item",
        "label_mapping_item_targets",
        ["tenant_id", "project_id", "mapping_version_id", "mapping_item_id"],
    )
    op.create_index(
        "ix_label_mapping_item_targets_scope_target",
        "label_mapping_item_targets",
        ["tenant_id", "project_id", "target_label_version_id", "target_label_id"],
    )
    op.create_index(
        "ix_label_mapping_item_targets_trace_id",
        "label_mapping_item_targets",
        ["trace_id"],
    )

    op.create_table(
        "label_mapping_bundles",
        sa.Column("mapping_bundle_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("source_label_version_ids", sa.JSON(), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("compiler_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("resource_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("canonical_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(128), nullable=True),
        sa.Column("approved_by", sa.String(64), nullable=True),
        sa.Column("approved_at", _utc_datetime(), nullable=True),
        sa.Column("published_at", _utc_datetime(), nullable=True),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            name="uq_label_mapping_bundles_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "canonical_manifest_sha256",
            name="uq_label_mapping_bundles_scope_hash",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "target_label_version_id",
            name="uq_label_mapping_bundles_scope_target_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_mapping_bundles_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'compiling', 'validated', 'review_required', 'approved', "
            "'published', 'superseded', 'archived')",
            name="ck_label_mapping_bundles_status",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_label_mapping_bundles_resource_version",
        ),
    )
    op.create_index(
        "ix_label_mapping_bundles_scope_status",
        "label_mapping_bundles",
        ["tenant_id", "project_id", "status"],
    )
    op.create_index(
        "ix_label_mapping_bundles_scope_target",
        "label_mapping_bundles",
        ["tenant_id", "project_id", "target_label_version_id"],
    )
    op.create_index("ix_label_mapping_bundles_trace_id", "label_mapping_bundles", ["trace_id"])

    op.create_table(
        "label_mapping_bundle_sources",
        sa.Column("bundle_source_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mapping_bundle_id", sa.String(128), nullable=False),
        sa.Column("source_label_version_id", sa.String(128), nullable=False),
        sa.Column("source_resource_version", sa.Integer(), nullable=False),
        sa.Column("source_order", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_label_version_id",
            name="uq_label_mapping_bundle_sources_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_order",
            name="uq_label_mapping_bundle_sources_order",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "mapping_bundle_id"],
            [
                "label_mapping_bundles.tenant_id",
                "label_mapping_bundles.project_id",
                "label_mapping_bundles.mapping_bundle_id",
            ],
            name="fk_label_mapping_bundle_sources_scope_bundle",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_mapping_bundle_sources_scope_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "source_resource_version > 0",
            name="ck_label_mapping_bundle_sources_resource_version",
        ),
        sa.CheckConstraint(
            "source_order >= 0",
            name="ck_label_mapping_bundle_sources_order",
        ),
    )
    op.create_index(
        "ix_label_mapping_bundle_sources_scope_bundle",
        "label_mapping_bundle_sources",
        ["tenant_id", "project_id", "mapping_bundle_id", "source_order"],
    )
    op.create_index(
        "ix_label_mapping_bundle_sources_trace_id",
        "label_mapping_bundle_sources",
        ["trace_id"],
    )

    op.create_table(
        "label_mapping_bundle_members",
        sa.Column("bundle_member_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mapping_bundle_id", sa.String(128), nullable=False),
        sa.Column("mapping_version_id", sa.String(128), nullable=False),
        sa.Column("source_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("edge_order", sa.Integer(), nullable=False),
        sa.Column("edge_content_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "mapping_version_id",
            name="uq_label_mapping_bundle_members_edge",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "edge_order",
            name="uq_label_mapping_bundle_members_order",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "mapping_bundle_id"],
            [
                "label_mapping_bundles.tenant_id",
                "label_mapping_bundles.project_id",
                "label_mapping_bundles.mapping_bundle_id",
            ],
            name="fk_label_mapping_bundle_members_scope_bundle",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "mapping_version_id",
                "source_label_version_id",
                "target_label_version_id",
                "edge_content_sha256",
            ],
            [
                "label_mapping_versions.tenant_id",
                "label_mapping_versions.project_id",
                "label_mapping_versions.mapping_version_id",
                "label_mapping_versions.source_label_version_id",
                "label_mapping_versions.target_label_version_id",
                "label_mapping_versions.content_sha256",
            ],
            name="fk_label_mapping_bundle_members_scope_edge",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("edge_order >= 0", name="ck_label_mapping_bundle_members_order"),
    )
    op.create_index(
        "ix_label_mapping_bundle_members_scope_bundle",
        "label_mapping_bundle_members",
        ["tenant_id", "project_id", "mapping_bundle_id", "edge_order"],
    )
    op.create_index(
        "ix_label_mapping_bundle_members_trace_id",
        "label_mapping_bundle_members",
        ["trace_id"],
    )

    op.create_table(
        "label_mapping_bundle_paths",
        sa.Column("bundle_path_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("mapping_bundle_id", sa.String(128), nullable=False),
        sa.Column("source_label_version_id", sa.String(128), nullable=False),
        sa.Column("target_label_version_id", sa.String(128), nullable=False),
        sa.Column("source_label_id", sa.String(128), nullable=False),
        sa.Column("target_label_id", sa.String(128), nullable=True),
        sa.Column("metric_family", sa.String(96), nullable=False),
        sa.Column("relation_path", sa.JSON(), nullable=False),
        sa.Column("mapping_version_ids", sa.JSON(), nullable=False),
        sa.Column("metric_grain", sa.String(64), nullable=True),
        sa.Column("lineage_key", sa.String(128), nullable=True),
        sa.Column("reducer", sa.String(64), nullable=True),
        sa.Column("comparability_status", sa.String(32), nullable=False),
        sa.Column("requires_recompute", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("path_sha256", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_label_version_id",
            "source_label_id",
            "metric_family",
            name="uq_label_mapping_bundle_paths_source_metric",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "path_sha256",
            name="uq_label_mapping_bundle_paths_hash",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "mapping_bundle_id",
                "target_label_version_id",
            ],
            [
                "label_mapping_bundles.tenant_id",
                "label_mapping_bundles.project_id",
                "label_mapping_bundles.mapping_bundle_id",
                "label_mapping_bundles.target_label_version_id",
            ],
            name="fk_label_mapping_bundle_paths_scope_bundle_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_label_version_id", "source_label_id"],
            [
                "label_version_items.tenant_id",
                "label_version_items.project_id",
                "label_version_items.label_version_id",
                "label_version_items.label_id",
            ],
            name="fk_label_mapping_bundle_paths_scope_source",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id", "target_label_id"],
            [
                "label_version_items.tenant_id",
                "label_version_items.project_id",
                "label_version_items.label_version_id",
                "label_version_items.label_id",
            ],
            name="fk_label_mapping_bundle_paths_scope_target_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_label_mapping_bundle_paths_comparability",
        ),
    )
    op.create_index(
        "ix_label_mapping_bundle_paths_scope_target",
        "label_mapping_bundle_paths",
        ["tenant_id", "project_id", "mapping_bundle_id", "target_label_id"],
    )
    op.create_index(
        "ix_label_mapping_bundle_paths_trace_id",
        "label_mapping_bundle_paths",
        ["trace_id"],
    )


def _create_activation_ledger() -> None:
    op.create_table(
        "release_bundle_head_events",
        sa.Column("head_event_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_generation", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("activation_status", sa.String(32), nullable=False),
        sa.Column("old_deployment_id", sa.String(128), nullable=True),
        sa.Column("new_deployment_id", sa.String(128), nullable=True),
        sa.Column("old_label_version_id", sa.String(128), nullable=True),
        sa.Column("new_label_version_id", sa.String(128), nullable=True),
        sa.Column("old_bundle_sha256", sa.String(64), nullable=True),
        sa.Column("new_bundle_sha256", sa.String(64), nullable=True),
        sa.Column("effective_from", _utc_datetime(), nullable=False),
        sa.Column("effective_to", _utc_datetime(), nullable=True),
        sa.Column("command_id", sa.String(128), nullable=True),
        sa.Column("completion_receipt_id", sa.String(128), nullable=True),
        sa.Column("approval_id", sa.String(128), nullable=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("root_trace_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "generation",
            name="uq_release_bundle_head_events_generation",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_release_bundle_head_events_hash",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "old_deployment_id"],
            [
                "release_deployments.tenant_id",
                "release_deployments.project_id",
                "release_deployments.deployment_id",
            ],
            name="fk_release_head_events_scope_old_deployment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "new_deployment_id"],
            [
                "release_deployments.tenant_id",
                "release_deployments.project_id",
                "release_deployments.deployment_id",
            ],
            name="fk_release_head_events_scope_new_deployment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "old_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_release_head_events_scope_old_label",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "new_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_release_head_events_scope_new_label",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "command_id"],
            [
                "release_commands.tenant_id",
                "release_commands.project_id",
                "release_commands.command_id",
            ],
            name="fk_release_head_events_scope_command",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "completion_receipt_id"],
            [
                "run_completion_receipts.tenant_id",
                "run_completion_receipts.project_id",
                "run_completion_receipts.completion_receipt_id",
            ],
            name="fk_release_head_events_scope_receipt",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint("generation > 0", name="ck_release_bundle_head_events_generation"),
        sa.CheckConstraint(
            "(generation = 1 AND previous_generation IS NULL) OR "
            "(generation > 1 AND previous_generation = generation - 1)",
            name="ck_release_bundle_head_events_previous_generation",
        ),
        sa.CheckConstraint(
            "action IN ('bootstrap', 'activate', 'promote', 'start-draining', 'drained', "
            "'rollback', 'deactivate')",
            name="ck_release_bundle_head_events_action",
        ),
        sa.CheckConstraint(
            "activation_status IN ('active', 'draining', 'inactive', 'rolled-back')",
            name="ck_release_bundle_head_events_status",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_release_bundle_head_events_interval",
        ),
    )
    op.create_index(
        "ix_release_bundle_head_events_scope_timeline",
        "release_bundle_head_events",
        ["tenant_id", "project_id", "environment", "generation"],
    )
    op.create_index(
        "ix_release_bundle_head_events_scope_label",
        "release_bundle_head_events",
        ["tenant_id", "project_id", "new_label_version_id", "effective_from"],
    )
    op.create_index(
        "ix_release_bundle_head_events_trace_id",
        "release_bundle_head_events",
        ["trace_id"],
    )


def upgrade() -> None:
    _create_label_taxonomies()
    _expand_label_versions()
    _create_mapping_tables()
    _create_activation_ledger()
    _create_append_only_triggers()
    _create_published_artifact_triggers()


def downgrade() -> None:
    _drop_published_artifact_triggers()
    _drop_append_only_triggers()
    op.drop_table("release_bundle_head_events")
    op.drop_table("label_mapping_bundle_paths")
    op.drop_table("label_mapping_bundle_members")
    op.drop_table("label_mapping_bundle_sources")
    op.drop_table("label_mapping_bundles")
    op.drop_table("label_mapping_item_targets")
    op.drop_table("label_mapping_items")
    op.drop_table("label_mapping_versions")

    with op.batch_alter_table("label_version_items") as batch_op:
        batch_op.drop_index("ix_label_version_items_scope_status")
        batch_op.drop_constraint("ck_label_version_items_status", type_="check")
        batch_op.drop_column("definition_sha256")

    op.drop_index("ix_label_versions_scope_replacement", table_name="label_versions")
    op.drop_index("ix_label_versions_scope_taxonomy", table_name="label_versions")
    op.drop_index("ix_label_versions_scope_artifact_status", table_name="label_versions")
    with op.batch_alter_table("label_versions") as batch_op:
        batch_op.drop_constraint("ck_label_versions_artifact_status", type_="check")
        batch_op.drop_constraint("fk_label_versions_scope_replacement", type_="foreignkey")
        batch_op.drop_constraint("fk_label_versions_scope_base", type_="foreignkey")
        batch_op.drop_constraint("fk_label_versions_scope_taxonomy", type_="foreignkey")
        batch_op.drop_constraint("uq_label_versions_scope_taxonomy_id", type_="unique")
        batch_op.drop_constraint("uq_label_versions_scope_taxonomy_semver", type_="unique")
        batch_op.drop_column("content_sha256")
        batch_op.drop_column("replacement_label_version_id")
        batch_op.drop_column("deprecation_reason")
        batch_op.drop_column("artifact_deprecated_at")
        batch_op.drop_column("artifact_published_at")
        batch_op.drop_column("artifact_status")
        batch_op.drop_column("base_label_version_id")
        batch_op.drop_column("semantic_version")
        batch_op.drop_column("taxonomy_id")
    op.drop_table("label_taxonomies")
