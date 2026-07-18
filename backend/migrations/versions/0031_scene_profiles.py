"""add versioned scene profiles and project bindings

Revision ID: 0031_scene_profiles
Revises: 0030_label_optimization_runtime
Create Date: 2026-07-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0031_scene_profiles"
down_revision = "0030_label_optimization_runtime"
branch_labels = None
depends_on = None


def _utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql")


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", _utc_datetime(), server_default=sa.func.now(), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "scene_profiles",
        sa.Column("scene_profile_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("scene_key", sa.String(96), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("current_published_version_id", sa.String(128)),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_key",
            name="uq_scene_profiles_scope_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            name="uq_scene_profiles_scope_id",
        ),
        sa.CheckConstraint(
            "status IN ('generating', 'draft', 'candidate', 'published', 'archived')",
            name="ck_scene_profiles_status",
        ),
    )
    op.create_index(
        "ix_scene_profiles_tenant_id",
        "scene_profiles",
        ["tenant_id"],
    )
    op.create_index(
        "ix_scene_profiles_project_id",
        "scene_profiles",
        ["project_id"],
    )
    op.create_index(
        "ix_scene_profiles_status",
        "scene_profiles",
        ["status"],
    )
    op.create_index(
        "ix_scene_profiles_current_published_version_id",
        "scene_profiles",
        ["current_published_version_id"],
    )
    op.create_index(
        "ix_scene_profiles_trace_id",
        "scene_profiles",
        ["trace_id"],
    )

    op.create_table(
        "scene_profile_versions",
        sa.Column("scene_profile_version_id", sa.String(128), primary_key=True),
        sa.Column("scene_profile_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("parent_version_id", sa.String(128)),
        sa.Column("generated_by_run_id", sa.String(128)),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("reviewed_by", sa.String(128)),
        sa.Column("published_by", sa.String(128)),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("validation_report", sa.JSON(), nullable=False),
        sa.Column("review_record", sa.JSON(), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_version_id",
            name="uq_scene_profile_versions_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            "version",
            name="uq_scene_profile_versions_scope_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            "scene_profile_version_id",
            name="uq_scene_profile_versions_scope_profile_version_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "scene_profile_id"],
            [
                "scene_profiles.tenant_id",
                "scene_profiles.project_id",
                "scene_profiles.scene_profile_id",
            ],
            name="fk_scene_profile_versions_scope_profile",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'candidate', 'blocked', 'validated', 'approved', "
            "'rejected', 'published', 'deprecated')",
            name="ck_scene_profile_versions_status",
        ),
        sa.CheckConstraint(
            "source_type IN ('human', 'model', 'import')",
            name="ck_scene_profile_versions_source_type",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_scene_profile_versions_resource_version",
        ),
    )
    for column in (
        "tenant_id",
        "project_id",
        "status",
        "parent_version_id",
        "generated_by_run_id",
        "trace_id",
    ):
        op.create_index(
            f"ix_scene_profile_versions_{column}",
            "scene_profile_versions",
            [column],
        )

    op.create_table(
        "project_scene_profile_bindings",
        sa.Column("binding_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(16), nullable=False),
        sa.Column("scene_profile_id", sa.String(128), nullable=False),
        sa.Column("scene_profile_version_id", sa.String(128), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("bound_by", sa.String(128), nullable=False),
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            name="uq_project_scene_bindings_scope_environment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "scene_profile_id"],
            [
                "scene_profiles.tenant_id",
                "scene_profiles.project_id",
                "scene_profiles.scene_profile_id",
            ],
            name="fk_project_scene_bindings_scope_profile",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "scene_profile_version_id"],
            [
                "scene_profile_versions.tenant_id",
                "scene_profile_versions.project_id",
                "scene_profile_versions.scene_profile_version_id",
            ],
            name="fk_project_scene_bindings_scope_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "scene_profile_id",
                "scene_profile_version_id",
            ],
            [
                "scene_profile_versions.tenant_id",
                "scene_profile_versions.project_id",
                "scene_profile_versions.scene_profile_id",
                "scene_profile_versions.scene_profile_version_id",
            ],
            name="fk_project_scene_bindings_scope_profile_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        sa.CheckConstraint(
            "environment IN ('development', 'staging', 'production')",
            name="ck_project_scene_bindings_environment",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_project_scene_bindings_status",
        ),
        sa.CheckConstraint(
            "resource_version > 0",
            name="ck_project_scene_bindings_resource_version",
        ),
    )
    for column in ("tenant_id", "project_id", "trace_id"):
        op.create_index(
            f"ix_project_scene_profile_bindings_{column}",
            "project_scene_profile_bindings",
            [column],
        )


def downgrade() -> None:
    op.drop_table("project_scene_profile_bindings")
    op.drop_table("scene_profile_versions")
    op.drop_table("scene_profiles")
