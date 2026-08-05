from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    project_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class SceneProfile(Base, TimestampMixin):
    __tablename__ = "scene_profiles"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_key",
            name="uq_scene_profiles_scope_key",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            name="uq_scene_profiles_scope_id",
        ),
        CheckConstraint(
            "status IN ('generating', 'draft', 'candidate', 'published', 'archived')",
            name="ck_scene_profiles_status",
        ),
    )

    scene_profile_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    scene_key: Mapped[str] = mapped_column(String(96), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    current_published_version_id: Mapped[str | None] = mapped_column(String(128), index=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class SceneProfileVersion(Base, TimestampMixin):
    __tablename__ = "scene_profile_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_version_id",
            name="uq_scene_profile_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            "version",
            name="uq_scene_profile_versions_scope_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scene_profile_id",
            "scene_profile_version_id",
            name="uq_scene_profile_versions_scope_profile_version_id",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "status IN ('draft', 'candidate', 'blocked', 'validated', 'approved', "
            "'rejected', 'published', 'deprecated')",
            name="ck_scene_profile_versions_status",
        ),
        CheckConstraint(
            "source_type IN ('human', 'model', 'import')",
            name="ck_scene_profile_versions_source_type",
        ),
        CheckConstraint("resource_version > 0", name="ck_scene_profile_versions_resource_version"),
    )

    scene_profile_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    scene_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(128), index=True)
    generated_by_run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128))
    published_by: Mapped[str | None] = mapped_column(String(128))
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_report: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    review_record: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class ProjectSceneProfileBinding(Base, TimestampMixin):
    __tablename__ = "project_scene_profile_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            name="uq_project_scene_bindings_scope_environment",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "environment IN ('development', 'staging', 'production')",
            name="ck_project_scene_bindings_environment",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')", name="ck_project_scene_bindings_status"
        ),
        CheckConstraint("resource_version > 0", name="ck_project_scene_bindings_resource_version"),
    )

    binding_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    environment: Mapped[str] = mapped_column(String(16), nullable=False)
    scene_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    bound_by: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index(
            "ix_auth_sessions_tenant_user_active",
            "tenant_id",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
        Index("ix_auth_sessions_provider_expires", "provider", "expires_at"),
        CheckConstraint("expires_at > issued_at", name="ck_auth_sessions_expiry"),
        CheckConstraint(
            "last_seen_at >= issued_at AND last_seen_at <= expires_at",
            name="ck_auth_sessions_last_seen",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR (revoked_at >= issued_at AND revoked_at <= expires_at)",
            name="ck_auth_sessions_revoked",
        ),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.user_id", ondelete="RESTRICT"), index=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("tenants.tenant_id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlatformConnection(Base, TimestampMixin):
    __tablename__ = "platform_connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "platform_connection_id",
            name="uq_platform_connections_scope_id",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'disabled', 'error', 'needs_reconfiguration')",
            name="ck_platform_connections_status",
        ),
        CheckConstraint(
            "last_test_status IS NULL OR last_test_status IN ('success', 'failed')",
            name="ck_platform_connections_last_test_status",
        ),
        CheckConstraint(
            "resource_version > 0",
            name="ck_platform_connections_resource_version",
        ),
        Index(
            "ix_platform_connections_scope_status_created",
            "tenant_id",
            "project_id",
            "status",
            "created_at",
        ),
    )

    platform_connection_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    external_tenant_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(2048), nullable=False)
    credential_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    store_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    test_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="/")
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="draft")
    resource_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    last_test_status: Mapped[str | None] = mapped_column(String(16))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class JsonResource(Base, TimestampMixin):
    __tablename__ = "json_resources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "collection",
            "resource_key",
            name="uq_json_resources_scope_key",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(512), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RunRecord(Base, TimestampMixin):
    __tablename__ = "run_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "run_id",
            name="uq_run_records_scope_id",
        ),
        Index("ix_run_records_status_deadline", "status", "deadline_at"),
        Index("ix_run_records_status_sync_due", "status", "next_status_sync_at"),
        Index(
            "ix_run_records_monitor_deadline",
            "run_type",
            "status",
            "deadline_at",
        ),
        Index(
            "ix_run_records_monitor_sync_due",
            "run_type",
            "status",
            "next_status_sync_at",
        ),
        Index(
            "ix_run_records_monitor_control_active",
            "tenant_id",
            "project_id",
            "run_key",
            "run_type",
            "status",
            mysql_length={"run_key": 128},
        ),
        Index(
            "ix_run_records_type_status_finished",
            "run_type",
            "status",
            "finished_at",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    run_key: Mapped[str | None] = mapped_column(String(512))
    partition_key: Mapped[str | None] = mapped_column(String(512))
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_status_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monitor_generation: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    engine_status: Mapped[str | None] = mapped_column(String(32), index=True)
    engine_status_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status_version: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    terminal_reason: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "import_batch_id",
            name="uq_import_batches_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "task_run_id",
            name="uq_import_batches_scope_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "task_run_id"],
            [
                "run_records.tenant_id",
                "run_records.project_id",
                "run_records.run_id",
            ],
            name="fk_import_batches_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled')",
            name="ck_import_batches_status",
        ),
        CheckConstraint(
            "current_stage IN "
            "('queued', 'listing', 'downloading', 'verifying', 'materializing', 'completed')",
            name="ck_import_batches_current_stage",
        ),
        CheckConstraint(
            "total_items >= 0 AND succeeded_items >= 0 AND skipped_items >= 0 "
            "AND failed_items >= 0",
            name="ck_import_batches_nonnegative_counts",
        ),
        CheckConstraint(
            "succeeded_items + skipped_items + failed_items <= total_items",
            name="ck_import_batches_count_bounds",
        ),
        Index(
            "ix_import_batches_scope_status_created",
            "tenant_id",
            "project_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_import_batches_scope_task_version",
            "tenant_id",
            "project_id",
            "task_version_id",
            "created_at",
        ),
    )

    import_batch_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    current_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cursor_before: Mapped[str | None] = mapped_column(String(1024))
    cursor_after: Mapped[str | None] = mapped_column(String(1024))
    root_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ImportBatchItem(Base, TimestampMixin):
    __tablename__ = "import_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "import_batch_id",
            "external_record_id",
            name="uq_import_batch_items_scope_external",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "import_batch_id"],
            [
                "import_batches.tenant_id",
                "import_batches.project_id",
                "import_batches.import_batch_id",
            ],
            name="fk_import_batch_items_scope_batch",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'skipped', 'failed')",
            name="ck_import_batch_items_status",
        ),
        Index(
            "ix_import_batch_items_scope_batch_status",
            "tenant_id",
            "project_id",
            "import_batch_id",
            "status",
        ),
        Index(
            "ix_import_batch_items_scope_audio_session",
            "tenant_id",
            "project_id",
            "audio_session_id",
        ),
    )

    import_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    import_batch_id: Mapped[str] = mapped_column(String(128), nullable=False)
    external_record_id: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    error_code: Mapped[str | None] = mapped_column(String(128))
    object_version: Mapped[str | None] = mapped_column(String(512))
    audio_session_id: Mapped[str | None] = mapped_column(String(128))
    root_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class MetricResult(Base, TimestampMixin):
    __tablename__ = "metric_results"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "metric_result_id",
            name="uq_metric_results_scope",
        ),
    )

    metric_result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="snapshot", nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    root_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class MetricResultLabelScope(Base):
    __tablename__ = "metric_result_label_scopes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "metric_result_id",
            name="uq_metric_result_label_scopes_scope_result",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "scope_sha256",
            name="uq_metric_result_label_scopes_scope_hash",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "taxonomy_mode IN ('native', 'normalized', 'recomputed')",
            name="ck_metric_result_label_scopes_mode",
        ),
        CheckConstraint(
            "(taxonomy_mode = 'native' AND target_label_version_id IS NULL "
            "AND mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(taxonomy_mode = 'normalized' AND target_label_version_id IS NOT NULL "
            "AND mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL) OR "
            "(taxonomy_mode = 'recomputed' AND target_label_version_id IS NOT NULL "
            "AND ((mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL)))",
            name="ck_metric_result_label_scopes_mode_binding",
        ),
        CheckConstraint(
            "fact_set_generation > 0",
            name="ck_metric_result_label_scopes_generation",
        ),
        CheckConstraint(
            "label_version_applicability = 'required'",
            name="ck_metric_result_label_scopes_applicability",
        ),
        CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_metric_result_label_scopes_comparability",
        ),
        CheckConstraint(
            "LENGTH(scope_sha256) = 64 AND LENGTH(source_manifest_sha256) = 64 "
            "AND LENGTH(content_sha256) = 64 AND LENGTH(fact_set_manifest_sha256) = 64 "
            "AND (mapping_bundle_sha256 IS NULL OR LENGTH(mapping_bundle_sha256) = 64)",
            name="ck_metric_result_label_scopes_hashes",
        ),
        Index(
            "ix_metric_result_label_scopes_scope_target",
            "tenant_id",
            "project_id",
            "taxonomy_mode",
            "target_label_version_id",
        ),
        Index(
            "ix_metric_result_label_scopes_scope_fact_cutoff",
            "tenant_id",
            "project_id",
            "fact_namespace",
            "fact_set_generation",
            "fact_as_of",
        ),
        Index("ix_metric_result_label_scopes_trace_id", "trace_id"),
    )

    metric_scope_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    taxonomy_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    source_label_version_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    target_label_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mapping_bundle_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mapping_bundle_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_set_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_set_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    fact_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metric_definition_versions: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    period_boundary: Mapped[str] = mapped_column(String(128), nullable=False)
    denominator_definition: Mapped[str] = mapped_column(String(512), nullable=False)
    label_version_applicability: Mapped[str] = mapped_column(String(32), nullable=False)
    comparability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    comparability_reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InsightReport(Base, TimestampMixin):
    __tablename__ = "insight_reports"
    __table_args__ = (
        Index("ix_insight_reports_scope_status", "tenant_id", "project_id", "status"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "report_id",
            name="uq_insight_reports_scope_id",
        ),
        UniqueConstraint("tenant_id", "project_id", "run_id", name="uq_insight_reports_scope_run"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_insight_reports_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )

    report_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generating")
    report_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class InsightReportMetricBinding(Base):
    __tablename__ = "insight_report_metric_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "report_id",
            name="uq_insight_report_metric_bindings_scope_report",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_insight_report_metric_bindings_scope_hash",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "LENGTH(metric_scope_sha256) = 64 AND LENGTH(content_sha256) = 64",
            name="ck_insight_report_metric_bindings_hashes",
        ),
        CheckConstraint(
            "result_count > 0",
            name="ck_insight_report_metric_bindings_result_count",
        ),
        Index("ix_insight_report_metric_bindings_trace_id", "trace_id"),
    )

    report_metric_binding_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    report_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_result_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_scope_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InsightAction(Base, TimestampMixin):
    __tablename__ = "insight_actions"
    __table_args__ = (
        Index("ix_insight_actions_scope_status", "tenant_id", "project_id", "status"),
        Index("ix_insight_actions_scope_report", "tenant_id", "project_id", "report_id"),
        Index(
            "ix_insight_actions_scope_baseline_metric",
            "tenant_id",
            "project_id",
            "baseline_metric_result_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "action_id",
            name="uq_insight_actions_scope_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "report_id"],
            [
                "insight_reports.tenant_id",
                "insight_reports.project_id",
                "insight_reports.report_id",
            ],
            name="fk_insight_actions_scope_report",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_insight_actions_scope_baseline_metric",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )

    action_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    report_id: Mapped[str] = mapped_column(String(128), nullable=False)
    baseline_metric_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    branch: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    review_task_id: Mapped[str | None] = mapped_column(String(128), index=True)
    resource_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class InsightExperiment(Base, TimestampMixin):
    __tablename__ = "insight_experiments"
    __table_args__ = (
        Index("ix_insight_experiments_scope_status", "tenant_id", "project_id", "status"),
        Index(
            "ix_insight_experiments_scope_action",
            "tenant_id",
            "project_id",
            "action_id",
        ),
        Index(
            "ix_insight_experiments_scope_baseline_metric",
            "tenant_id",
            "project_id",
            "baseline_metric_result_id",
        ),
        Index(
            "ix_insight_experiments_scope_outcome_metric",
            "tenant_id",
            "project_id",
            "outcome_metric_result_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            name="uq_insight_experiments_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_run_id",
            name="uq_insight_experiments_scope_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "action_id"],
            [
                "insight_actions.tenant_id",
                "insight_actions.project_id",
                "insight_actions.action_id",
            ],
            name="fk_insight_experiments_scope_action",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "eval_run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_insight_experiments_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_insight_experiments_scope_baseline_metric",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outcome_metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_insight_experiments_scope_outcome_metric",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )

    experiment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    baseline_metric_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_metric_result_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class InsightEffect(Base, TimestampMixin):
    __tablename__ = "insight_effects"
    __table_args__ = (
        Index("ix_insight_effects_scope_action", "tenant_id", "project_id", "action_id"),
        Index(
            "ix_insight_effects_scope_baseline_metric",
            "tenant_id",
            "project_id",
            "baseline_metric_result_id",
        ),
        Index(
            "ix_insight_effects_scope_outcome_metric",
            "tenant_id",
            "project_id",
            "outcome_metric_result_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "effect_id",
            name="uq_insight_effects_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            name="uq_insight_effects_scope_experiment",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "action_id"],
            [
                "insight_actions.tenant_id",
                "insight_actions.project_id",
                "insight_actions.action_id",
            ],
            name="fk_insight_effects_scope_action",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "experiment_id"],
            [
                "insight_experiments.tenant_id",
                "insight_experiments.project_id",
                "insight_experiments.experiment_id",
            ],
            name="fk_insight_effects_scope_experiment",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "baseline_metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_insight_effects_scope_baseline_metric",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outcome_metric_result_id"],
            [
                "metric_results.tenant_id",
                "metric_results.project_id",
                "metric_results.metric_result_id",
            ],
            name="fk_insight_effects_scope_outcome_metric",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
    )

    effect_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(String(128), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    baseline_metric_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome_metric_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(128), nullable=False)
    delta: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_low: Mapped[float | None] = mapped_column(Float)
    confidence_high: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="measured")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ControlledExperiment(Base, TimestampMixin):
    """Version-locked experiment definition shared by task, model and policy experiments."""

    __tablename__ = "controlled_experiments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            name="uq_controlled_experiments_scope_id",
        ),
        Index(
            "ix_controlled_experiments_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_controlled_experiments_scope_scene",
            "tenant_id",
            "project_id",
            "scene_profile_version_id",
        ),
        CheckConstraint(
            "status IN ('draft', 'running', 'paused', 'stopped', 'decided', 'archived')",
            name="ck_controlled_experiments_status",
        ),
        CheckConstraint("resource_version > 0", name="ck_controlled_experiments_version"),
    )

    experiment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    experiment_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type_id: Mapped[str] = mapped_column(String(128), nullable=False)
    control_task_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_task_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    design_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExperimentAssignment(Base, TimestampMixin):
    __tablename__ = "experiment_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            name="uq_experiment_assignments_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "assignment_id",
            name="uq_experiment_assignments_scope_experiment_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "subject_key_sha256",
            name="uq_experiment_assignments_scope_subject",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "assignment_bucket >= 0 AND assignment_bucket < 1000000",
            name="ck_experiment_assignments_bucket",
        ),
        Index(
            "ix_experiment_assignments_scope_arm",
            "tenant_id",
            "project_id",
            "experiment_id",
            "arm_key",
        ),
    )

    assignment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    arm_key: Mapped[str] = mapped_column(String(64), nullable=False)
    assignment_bucket: Mapped[int] = mapped_column(Integer, nullable=False)
    design_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExperimentExposure(Base, TimestampMixin):
    __tablename__ = "experiment_exposures"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_id",
            name="uq_experiment_exposures_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_key_sha256",
            name="uq_experiment_exposures_scope_key",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        Index(
            "ix_experiment_exposures_scope_arm",
            "tenant_id",
            "project_id",
            "experiment_id",
            "arm_key",
        ),
    )

    exposure_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exposure_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    arm_key: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExperimentOutcome(Base, TimestampMixin):
    __tablename__ = "experiment_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "exposure_id",
            "metric_key",
            name="uq_experiment_outcomes_scope_metric",
        ),
        ForeignKeyConstraint(
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
        Index(
            "ix_experiment_outcomes_scope_metric",
            "tenant_id",
            "project_id",
            "experiment_id",
            "metric_key",
        ),
    )

    outcome_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    exposure_id: Mapped[str] = mapped_column(String(128), nullable=False)
    arm_key: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_key: Mapped[str] = mapped_column(String(96), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExperimentMetricSnapshot(Base, TimestampMixin):
    __tablename__ = "experiment_metric_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "experiment_id",
            "snapshot_version",
            name="uq_experiment_metric_snapshots_scope_version",
        ),
        ForeignKeyConstraint(
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
        Index(
            "ix_experiment_metric_snapshots_scope_experiment",
            "tenant_id",
            "project_id",
            "experiment_id",
        ),
    )

    metric_snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_version: Mapped[int] = mapped_column(Integer, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_metric_key: Mapped[str] = mapped_column(String(96), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scene_profile_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scene_profile_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExperimentDecision(Base, TimestampMixin):
    __tablename__ = "experiment_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "decision_id",
            name="uq_experiment_decisions_scope_id",
        ),
        ForeignKeyConstraint(
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
        Index(
            "ix_experiment_decisions_scope_experiment",
            "tenant_id",
            "project_id",
            "experiment_id",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    experiment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    metric_snapshot_id: Mapped[str | None] = mapped_column(String(128), index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class TaskVersionReleaseHead(Base, TimestampMixin):
    """Single production TaskVersion pointer for one scoped task type and channel."""

    __tablename__ = "task_version_release_heads"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "task_type_id",
            "release_channel",
            name="uq_task_version_release_heads_scope_channel",
        ),
        CheckConstraint("generation > 0", name="ck_task_version_release_heads_generation"),
        CheckConstraint("status = 'active'", name="ck_task_version_release_heads_status"),
        Index(
            "ix_task_version_release_heads_scope_active",
            "tenant_id",
            "project_id",
            "task_type_id",
            "release_channel",
            "active_task_version_id",
        ),
    )

    release_head_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type_id: Mapped[str] = mapped_column(String(128), nullable=False)
    release_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="production")
    active_task_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    active_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_task_version_id: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    activated_by_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class DataAsset(Base, TimestampMixin):
    __tablename__ = "data_assets"

    data_asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class StorageObject(Base, TimestampMixin):
    __tablename__ = "storage_objects"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "provider",
            "bucket",
            "object_key_sha256",
            name="uq_storage_objects_scope_locator",
        ),
        Index(
            "ix_storage_objects_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_storage_objects_scope_source",
            "tenant_id",
            "project_id",
            "source_type",
            "source_id",
        ),
    )

    storage_object_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    object_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(128), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    etag: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AudioRecording(Base, TimestampMixin):
    __tablename__ = "audio_recordings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "recording_id",
            name="uq_audio_recordings_scope",
        ),
    )

    recording_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), index=True, default="registered", nullable=False
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AssetPartition(Base, TimestampMixin):
    __tablename__ = "asset_partitions"

    asset_partition_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AssetMaterialization(Base, TimestampMixin):
    __tablename__ = "asset_materializations"

    materialization_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AssetLineageEdge(Base, TimestampMixin):
    __tablename__ = "asset_lineage_edges"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "edge_id",
            name="uq_asset_lineage_edges_scope",
        ),
    )

    edge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="active", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_runs"

    agent_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ToolCall(Base, TimestampMixin):
    __tablename__ = "tool_calls"

    tool_call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class AgentDecision(Base, TimestampMixin):
    __tablename__ = "agent_decisions"

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class TraceRef(Base, TimestampMixin):
    __tablename__ = "trace_refs"

    trace_ref_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ListeningAnnotation(Base, TimestampMixin):
    __tablename__ = "listening_annotations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "annotation_id",
            name="uq_listening_annotations_scope",
        ),
    )

    annotation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    audio_session_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class VoiceprintEnrollment(Base, TimestampMixin):
    __tablename__ = "voiceprint_enrollments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "enrollment_id",
            name="uq_voiceprint_enrollments_scope",
        ),
    )

    enrollment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    voiceprint_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PromptVersionCandidate(Base, TimestampMixin):
    __tablename__ = "prompt_version_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "candidate_id",
            name="uq_prompt_version_candidates_scope",
        ),
    )

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="candidate", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelTaxonomy(Base, TimestampMixin):
    """Strong, scope-bound identity for one governed label taxonomy."""

    __tablename__ = "label_taxonomies"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "taxonomy_id",
            name="uq_label_taxonomies_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            name="uq_label_taxonomies_scope_name",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_taxonomies_scope_hash",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'inactive', 'archived')",
            name="ck_label_taxonomies_status",
        ),
        CheckConstraint(
            "resource_version > 0",
            name="ck_label_taxonomies_resource_version",
        ),
        Index(
            "ix_label_taxonomies_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
    )

    taxonomy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelVersion(Base, TimestampMixin):
    __tablename__ = "label_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            name="uq_label_versions_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "taxonomy_id",
            "semantic_version",
            name="uq_label_versions_scope_taxonomy_semver",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "taxonomy_id",
            "label_version_id",
            name="uq_label_versions_scope_taxonomy_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "taxonomy_id"],
            [
                "label_taxonomies.tenant_id",
                "label_taxonomies.project_id",
                "label_taxonomies.taxonomy_id",
            ],
            name="fk_label_versions_scope_taxonomy",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "taxonomy_id", "base_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.taxonomy_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_versions_scope_base",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "taxonomy_id",
                "replacement_label_version_id",
            ],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.taxonomy_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_versions_scope_replacement",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "artifact_status IS NULL OR artifact_status IN ("
            "'draft', 'candidate', 'validated', 'locked', 'evaluating', "
            "'gate_blocked', 'review_required', 'approved', 'published', "
            "'deprecated', 'archived')",
            name="ck_label_versions_artifact_status",
        ),
        Index(
            "ix_label_versions_scope_artifact_status",
            "tenant_id",
            "project_id",
            "artifact_status",
        ),
        Index(
            "ix_label_versions_scope_taxonomy",
            "tenant_id",
            "project_id",
            "taxonomy_id",
            "semantic_version",
        ),
        Index(
            "ix_label_versions_scope_replacement",
            "tenant_id",
            "project_id",
            "replacement_label_version_id",
        ),
    )

    label_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    taxonomy_id: Mapped[str | None] = mapped_column(String(128))
    semantic_version: Mapped[str | None] = mapped_column(String(64))
    base_label_version_id: Mapped[str | None] = mapped_column(String(128))
    artifact_status: Mapped[str | None] = mapped_column(String(32))
    artifact_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    artifact_deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deprecation_reason: Mapped[str | None] = mapped_column(String(1024))
    replacement_label_version_id: Mapped[str | None] = mapped_column(String(128))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    policy_version_id: Mapped[str | None] = mapped_column(String(128), index=True)
    release_gate_id: Mapped[str | None] = mapped_column(String(128), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelCandidate(Base, TimestampMixin):
    __tablename__ = "label_candidates"

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelConflict(Base, TimestampMixin):
    __tablename__ = "label_conflicts"

    conflict_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="detected", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelPolicyVersion(Base, TimestampMixin):
    __tablename__ = "label_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "canonical_sha256",
            name="uq_label_policy_versions_scope_artifact",
        ),
        Index(
            "ix_label_policy_versions_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
    )

    policy_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_key: Mapped[str] = mapped_column(String(96), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    dsl_version: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="validated")
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelPolicyEvaluation(Base, TimestampMixin):
    __tablename__ = "label_policy_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "target_type",
            "target_id",
            "policy_version_id",
            "facts_sha256",
            name="uq_label_policy_evaluations_replay",
        ),
        Index(
            "ix_label_policy_evaluations_scope_verdict",
            "tenant_id",
            "project_id",
            "verdict",
        ),
    )

    evaluation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="evaluated")
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    facts_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    facts_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelNode(Base, TimestampMixin):
    """Stable taxonomy identity independent from a display name or version."""

    __tablename__ = "label_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "label_id", name="uq_label_nodes_scope_label"),
        Index("ix_label_nodes_scope_status", "tenant_id", "project_id", "status"),
    )

    node_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelVersionItem(Base, TimestampMixin):
    __tablename__ = "label_version_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            name="uq_label_version_items_scope_label",
        ),
        Index(
            "ix_label_version_items_scope_version",
            "tenant_id",
            "project_id",
            "label_version_id",
        ),
        CheckConstraint(
            "definition_sha256 IS NULL OR status IN ('active', 'retired', 'pending-configuration')",
            name="ck_label_version_items_status",
        ),
        Index(
            "ix_label_version_items_scope_status",
            "tenant_id",
            "project_id",
            "label_version_id",
            "status",
            "label_id",
        ),
    )

    label_version_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default="low")
    mutual_exclusion_group: Mapped[str | None] = mapped_column(String(128))
    parent_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    aggregation_rule: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    definition_sha256: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelMappingVersion(Base, TimestampMixin):
    __tablename__ = "label_mapping_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            name="uq_label_mapping_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_label_version_id",
            "target_label_version_id",
            "mapping_version",
            name="uq_label_mapping_versions_scope_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_mapping_versions_scope_hash",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_version_id",
            "target_label_version_id",
            name="uq_label_mapping_versions_scope_pair_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_version_id",
            "target_label_version_id",
            "content_sha256",
            name="uq_label_mapping_versions_scope_edge_binding",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "source_label_version_id <> target_label_version_id",
            name="ck_label_mapping_versions_distinct_pair",
        ),
        CheckConstraint(
            "status IN ('draft', 'validated', 'review_required', 'approved', "
            "'published', 'superseded', 'archived')",
            name="ck_label_mapping_versions_status",
        ),
        CheckConstraint(
            "source_resource_version > 0 AND target_resource_version > 0 AND resource_version > 0",
            name="ck_label_mapping_versions_resource_versions",
        ),
        Index(
            "ix_label_mapping_versions_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_label_mapping_versions_scope_pair",
            "tenant_id",
            "project_id",
            "source_label_version_id",
            "target_label_version_id",
        ),
    )

    mapping_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    source_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingItem(Base, TimestampMixin):
    __tablename__ = "label_mapping_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "source_label_id",
            name="uq_label_mapping_items_source_disposition",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "content_sha256",
            name="uq_label_mapping_items_scope_hash",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "mapping_item_id",
            "target_label_version_id",
            name="uq_label_mapping_items_scope_target_parent",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "relation IN ('identity', 'rename', 'replace', 'merge', 'retire', 'split-recompute')",
            name="ck_label_mapping_items_relation",
        ),
        CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_label_mapping_items_comparability",
        ),
        CheckConstraint(
            "compatibility IN ('exact', 'metric-dependent', 'structural-break', 'not-applicable')",
            name="ck_label_mapping_items_compatibility",
        ),
        CheckConstraint(
            "relation <> 'split-recompute' OR requires_recompute = 1",
            name="ck_label_mapping_items_split_recompute",
        ),
        Index(
            "ix_label_mapping_items_scope_relation",
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "relation",
        ),
    )

    mapping_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    compatibility: Mapped[str] = mapped_column(String(32), nullable=False)
    comparability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_metric_families: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metric_grain: Mapped[str | None] = mapped_column(String(64))
    lineage_key: Mapped[str | None] = mapped_column(String(128))
    reducer: Mapped[str | None] = mapped_column(String(64))
    requires_recompute: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_semantic_sha256: Mapped[str | None] = mapped_column(String(64))
    target_semantic_sha256: Mapped[str | None] = mapped_column(String(64))
    compatibility_evidence_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingItemTarget(Base, TimestampMixin):
    __tablename__ = "label_mapping_item_targets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_item_id",
            "target_label_id",
            name="uq_label_mapping_item_targets_label",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_item_id",
            "target_order",
            name="uq_label_mapping_item_targets_order",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("target_order >= 0", name="ck_label_mapping_item_targets_order"),
        Index(
            "ix_label_mapping_item_targets_scope_item",
            "tenant_id",
            "project_id",
            "mapping_version_id",
            "mapping_item_id",
        ),
        Index(
            "ix_label_mapping_item_targets_scope_target",
            "tenant_id",
            "project_id",
            "target_label_version_id",
            "target_label_id",
        ),
    )

    mapping_item_target_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_order: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingBundle(Base, TimestampMixin):
    __tablename__ = "label_mapping_bundles"
    __table_args__ = (
        Index(
            "uq_label_mapping_bundles_scope_id_hash",
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "canonical_manifest_sha256",
            unique=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            name="uq_label_mapping_bundles_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "canonical_manifest_sha256",
            name="uq_label_mapping_bundles_scope_hash",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "target_label_version_id",
            name="uq_label_mapping_bundles_scope_target_id",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "status IN ('draft', 'compiling', 'validated', 'review_required', 'approved', "
            "'published', 'superseded', 'archived')",
            name="ck_label_mapping_bundles_status",
        ),
        CheckConstraint(
            "resource_version > 0",
            name="ck_label_mapping_bundles_resource_version",
        ),
        Index(
            "ix_label_mapping_bundles_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_label_mapping_bundles_scope_target",
            "tenant_id",
            "project_id",
            "target_label_version_id",
        ),
    )

    mapping_bundle_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_version_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    canonical_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingBundleSource(Base, TimestampMixin):
    __tablename__ = "label_mapping_bundle_sources"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_label_version_id",
            name="uq_label_mapping_bundle_sources_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_order",
            name="uq_label_mapping_bundle_sources_order",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "source_resource_version > 0",
            name="ck_label_mapping_bundle_sources_resource_version",
        ),
        CheckConstraint(
            "source_order >= 0",
            name="ck_label_mapping_bundle_sources_order",
        ),
        Index(
            "ix_label_mapping_bundle_sources_scope_bundle",
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_order",
        ),
    )

    bundle_source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_bundle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_order: Mapped[int] = mapped_column(Integer, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingBundleMember(Base, TimestampMixin):
    __tablename__ = "label_mapping_bundle_members"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "mapping_version_id",
            name="uq_label_mapping_bundle_members_edge",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "edge_order",
            name="uq_label_mapping_bundle_members_order",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("edge_order >= 0", name="ck_label_mapping_bundle_members_order"),
        Index(
            "ix_label_mapping_bundle_members_scope_bundle",
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "edge_order",
        ),
    )

    bundle_member_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_bundle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mapping_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    edge_order: Mapped[int] = mapped_column(Integer, nullable=False)
    edge_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelMappingBundlePath(Base, TimestampMixin):
    __tablename__ = "label_mapping_bundle_paths"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "source_label_version_id",
            "source_label_id",
            "metric_family",
            name="uq_label_mapping_bundle_paths_source_metric",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "path_sha256",
            name="uq_label_mapping_bundle_paths_hash",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "comparability_status IN ('comparable', 'partial', 'structural-break', "
            "'not-applicable')",
            name="ck_label_mapping_bundle_paths_comparability",
        ),
        Index(
            "ix_label_mapping_bundle_paths_scope_target",
            "tenant_id",
            "project_id",
            "mapping_bundle_id",
            "target_label_id",
        ),
    )

    bundle_path_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_bundle_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_id: Mapped[str | None] = mapped_column(String(128))
    metric_family: Mapped[str] = mapped_column(String(96), nullable=False)
    relation_path: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    mapping_version_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metric_grain: Mapped[str | None] = mapped_column(String(64))
    lineage_key: Mapped[str | None] = mapped_column(String(128))
    reducer: Mapped[str | None] = mapped_column(String(64))
    comparability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    requires_recompute: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    path_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelExtractionRun(Base, TimestampMixin):
    __tablename__ = "label_extraction_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "extraction_run_id", name="uq_label_extract_runs_scope"
        ),
        Index("ix_label_extract_runs_scope_status", "tenant_id", "project_id", "status"),
    )

    extraction_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    subject_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelObservation(Base, TimestampMixin):
    """Immutable raw source assertion. Rows are append-only by contract."""

    __tablename__ = "label_observations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "observation_id", name="uq_label_observations_scope"
        ),
        Index(
            "ix_label_observations_bucket",
            "tenant_id",
            "project_id",
            "label_version_id",
            "subject_scope",
            "subject_key",
        ),
        Index(
            "ix_label_observations_evidence",
            "tenant_id",
            "project_id",
            "evidence_sha256",
            "source_family",
        ),
    )

    observation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(256), nullable=False)
    evidence_ref: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_label: Mapped[str] = mapped_column(String(255), nullable=False)
    label_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    source_family: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    calibration_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    calibrated_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="materialized")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelCalibrationVersion(Base, TimestampMixin):
    """Immutable server-side confidence calibrator trained from a locked Gold set."""

    __tablename__ = "label_calibration_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "calibration_version_id",
            name="uq_label_calibration_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            "source_family",
            "version",
            name="uq_label_calibration_versions_scope_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_calibration_versions_scope_hash",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "method IN ('isotonic', 'platt', 'global-conservative')",
            name="ck_label_calibration_versions_method",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'retired')",
            name="ck_label_calibration_versions_status",
        ),
        CheckConstraint("sample_count > 0", name="ck_label_calibration_versions_samples"),
        Index(
            "ix_label_calibration_versions_scope_lookup",
            "tenant_id",
            "project_id",
            "label_version_id",
            "label_id",
            "source_family",
            "status",
        ),
    )

    calibration_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label_id: Mapped[str] = mapped_column(String(128), nullable=False, default="*")
    source_family: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    gold_set_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    training_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelAggregationPolicyVersion(Base, TimestampMixin):
    __tablename__ = "label_aggregation_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "policy_version",
            name="uq_label_agg_policies_scope_version",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "canonical_sha256",
            name="uq_label_agg_policies_scope_hash",
        ),
        Index("ix_label_agg_policies_scope_status", "tenant_id", "project_id", "status"),
    )

    policy_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="l1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    source_weights: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    calibration_versions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    label_definitions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    canonical_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelAggregationRun(Base, TimestampMixin):
    __tablename__ = "label_aggregation_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "aggregation_run_id", name="uq_label_agg_runs_scope"
        ),
        Index("ix_label_agg_runs_scope_status", "tenant_id", "project_id", "status"),
    )

    aggregation_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    aggregate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelAggregate(Base, TimestampMixin):
    __tablename__ = "label_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "aggregation_run_id",
            "bucket_sha256",
            name="uq_label_aggregates_run_bucket",
        ),
        Index(
            "ix_label_aggregates_scope_subject",
            "tenant_id",
            "project_id",
            "subject_scope",
            "subject_key",
        ),
        Index("ix_label_aggregates_scope_decision", "tenant_id", "project_id", "decision"),
    )

    aggregate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregation_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    calibration_version_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    subject_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(256), nullable=False)
    label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    bucket_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    deterministic_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelAggregateMember(Base, TimestampMixin):
    __tablename__ = "label_aggregate_members"
    __table_args__ = (
        UniqueConstraint("aggregate_id", "observation_id", name="uq_label_aggregate_members_pair"),
        Index("ix_label_aggregate_members_aggregate", "aggregate_id", "included"),
    )

    aggregate_member_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    included: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_family: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    calibrated_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    contribution_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    exclusion_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelRecomputeRun(Base, TimestampMixin):
    """Frozen full-recompute request; orchestration details stay server-side."""

    __tablename__ = "label_recompute_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "recompute_run_id", name="uq_label_recompute_runs_scope"
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "candidate_fact_set_id",
            name="uq_label_recompute_runs_scope_candidate",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "target_label_version_id"],
            [
                "label_versions.tenant_id",
                "label_versions.project_id",
                "label_versions.label_version_id",
            ],
            name="fk_label_recompute_runs_scope_target",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
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
            name="fk_label_recompute_runs_scope_mapping_bundle",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_fact_set_id"],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
            ],
            name="fk_label_recompute_runs_scope_source_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "candidate_fact_set_id"],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
            ],
            name="fk_label_recompute_runs_scope_candidate_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('requested', 'running', 'candidate-complete', "
            "'partial-failed', 'failed', 'blocked')",
            name="ck_label_recompute_runs_status",
        ),
        CheckConstraint(
            "(mapping_bundle_id IS NULL AND mapping_bundle_sha256 IS NULL) OR "
            "(mapping_bundle_id IS NOT NULL AND mapping_bundle_sha256 IS NOT NULL)",
            name="ck_label_recompute_runs_mapping_pair",
        ),
        CheckConstraint(
            "source_head_generation > 0 AND budget_units > 0 AND coverage_min >= 0 "
            "AND coverage_min <= 1",
            name="ck_label_recompute_runs_limits",
        ),
        CheckConstraint(
            "LENGTH(source_manifest_sha256) = 64 AND LENGTH(target_content_sha256) = 64 "
            "AND LENGTH(request_sha256) = 64 AND "
            "(mapping_bundle_sha256 IS NULL OR LENGTH(mapping_bundle_sha256) = 64)",
            name="ck_label_recompute_runs_hashes",
        ),
        Index(
            "ix_label_recompute_runs_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index("ix_label_recompute_runs_trace_id", "trace_id"),
    )

    recompute_run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_bundle_id: Mapped[str | None] = mapped_column(String(128))
    mapping_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    source_fact_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    source_head_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_fact_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    fact_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partition_scope: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    asset_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    coverage_policy: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    coverage_min: Mapped[float] = mapped_column(Float, nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_units: Mapped[int] = mapped_column(Integer, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelRecomputeRunItem(Base, TimestampMixin):
    """Retryable partition execution whose manifest is calculated by the BFF."""

    __tablename__ = "label_recompute_run_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "recompute_run_item_id",
            name="uq_label_recompute_run_items_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "recompute_run_id",
            "partition_id",
            name="uq_label_recompute_run_items_partition",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "recompute_run_id"],
            [
                "label_recompute_runs.tenant_id",
                "label_recompute_runs.project_id",
                "label_recompute_runs.recompute_run_id",
            ],
            name="fk_label_recompute_run_items_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "execution_run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_label_recompute_run_items_scope_execution",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_label_recompute_run_items_status",
        ),
        CheckConstraint(
            "attempt_generation > 0 AND row_count >= 0",
            name="ck_label_recompute_run_items_counts",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND completion_receipt_id IS NOT NULL "
            "AND source_manifest_sha256 IS NOT NULL AND result_manifest_sha256 IS NOT NULL "
            "AND content_sha256 IS NOT NULL) OR status <> 'succeeded'",
            name="ck_label_recompute_run_items_completion",
        ),
        Index(
            "ix_label_recompute_run_items_scope_status",
            "tenant_id",
            "project_id",
            "recompute_run_id",
            "status",
        ),
        Index("ix_label_recompute_run_items_trace_id", "trace_id"),
    )

    recompute_run_item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recompute_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    partition_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempt_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    execution_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    completion_receipt_id: Mapped[str | None] = mapped_column(String(128))
    source_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    result_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lineage_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelFactSet(Base, TimestampMixin):
    __tablename__ = "label_fact_sets"
    __table_args__ = (
        Index(
            "uq_label_fact_sets_scope_metric_binding",
            "tenant_id",
            "project_id",
            "fact_set_id",
            "fact_namespace",
            "manifest_sha256",
            "fact_as_of",
            unique=True,
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_id",
            name="uq_label_fact_sets_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "manifest_sha256",
            name="uq_label_fact_sets_scope_hash",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_id",
            "fact_namespace",
            "manifest_sha256",
            name="uq_label_fact_sets_scope_binding",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "status IN ('candidate', 'validated', 'approved', 'published', "
            "'superseded', 'archived')",
            name="ck_label_fact_sets_status",
        ),
        CheckConstraint("row_count >= 0", name="ck_label_fact_sets_row_count"),
        CheckConstraint(
            "LENGTH(partition_manifest_sha256) = 64 AND "
            "LENGTH(source_manifest_sha256) = 64 AND "
            "LENGTH(result_manifest_sha256) = 64 AND LENGTH(manifest_sha256) = 64",
            name="ck_label_fact_sets_hash_lengths",
        ),
        CheckConstraint(
            "(status NOT IN ('approved', 'published')) OR "
            "(approval_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_label_fact_sets_approval",
        ),
        Index(
            "ix_label_fact_sets_scope_status",
            "tenant_id",
            "project_id",
            "fact_namespace",
            "status",
        ),
        Index(
            "ix_label_fact_sets_scope_target",
            "tenant_id",
            "project_id",
            "target_label_version_id",
            "fact_as_of",
        ),
        Index("ix_label_fact_sets_trace_id", "trace_id"),
    )

    fact_set_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    target_label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="candidate", server_default="candidate"
    )
    fact_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partition_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    partition_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelFact(Base, TimestampMixin):
    __tablename__ = "label_facts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "fact_id", name="uq_label_facts_scope"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            "revision",
            name="uq_label_facts_temporal_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            "revision",
            "fact_id",
            name="uq_label_facts_temporal_head_binding",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "human_review_decision_id"],
            [
                "human_review_decisions.tenant_id",
                "human_review_decisions.project_id",
                "human_review_decisions.decision_id",
            ],
            name="fk_label_facts_scope_human_decision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "fact_set_id"],
            [
                "label_fact_sets.tenant_id",
                "label_fact_sets.project_id",
                "label_fact_sets.fact_set_id",
            ],
            name="fk_label_facts_scope_fact_set",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "recompute_run_item_id"],
            [
                "label_recompute_run_items.tenant_id",
                "label_recompute_run_items.project_id",
                "label_recompute_run_items.recompute_run_item_id",
            ],
            name="fk_label_facts_scope_recompute_item",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "ix_label_facts_scope_subject",
            "tenant_id",
            "project_id",
            "subject_scope",
            "subject_key",
            "status",
        ),
        Index(
            "ix_label_facts_temporal_as_of",
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            "recorded_at",
            "revision",
        ),
        Index(
            "ix_label_facts_temporal_occurred",
            "tenant_id",
            "project_id",
            "fact_namespace",
            "occurred_at",
            "label_version_id",
            "label_id",
        ),
        Index(
            "ix_label_facts_temporal_source",
            "tenant_id",
            "project_id",
            "source_kind",
            "human_review_decision_id",
        ),
        Index(
            "ix_label_facts_scope_fact_set",
            "tenant_id",
            "project_id",
            "fact_set_id",
        ),
        CheckConstraint(
            "(status = 'active' AND active_slot = 'active') OR "
            "(status = 'superseded' AND active_slot IS NULL) OR "
            "(status = 'recorded' AND active_slot IS NULL)",
            name="ck_label_facts_append_only_projection",
        ),
        CheckConstraint(
            "revision IS NULL OR revision > 0",
            name="ck_label_facts_temporal_revision",
        ),
        CheckConstraint(
            "(logical_key_sha IS NULL OR LENGTH(logical_key_sha) = 64) AND "
            "(content_sha256 IS NULL OR LENGTH(content_sha256) = 64)",
            name="ck_label_facts_temporal_hashes",
        ),
        CheckConstraint(
            "occurred_at_origin IS NULL OR occurred_at_origin IN "
            "('source', 'legacy-recorded-fallback', 'authorized-backfill')",
            name="ck_label_facts_occurred_origin",
        ),
        CheckConstraint(
            "source_kind IS NULL OR "
            "(source_kind = 'aggregate' AND aggregate_id IS NOT NULL "
            "AND human_review_decision_id IS NULL AND recompute_run_item_id IS NULL) OR "
            "(source_kind = 'human-decision' "
            "AND human_review_decision_id IS NOT NULL AND recompute_run_item_id IS NULL) OR "
            "(source_kind = 'recompute-run-item' AND aggregate_id IS NULL "
            "AND human_review_decision_id IS NULL AND recompute_run_item_id IS NOT NULL)",
            name="ck_label_facts_expand_source",
        ),
        CheckConstraint(
            "source_kind IS NULL OR (fact_namespace IS NOT NULL AND "
            "logical_key_sha IS NOT NULL AND revision IS NOT NULL AND "
            "event_or_segment_id IS NOT NULL AND assertion_slot IS NOT NULL AND "
            "occurred_at IS NOT NULL AND recorded_at IS NOT NULL AND "
            "occurred_at_origin IS NOT NULL AND content_sha256 IS NOT NULL AND "
            "root_trace_id IS NOT NULL AND action_trace_id IS NOT NULL)",
            name="ck_label_facts_temporal_completeness",
        ),
    )

    fact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supersedes_fact_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fact_namespace: Mapped[str | None] = mapped_column(String(128), nullable=True)
    logical_key_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_or_segment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    assertion_slot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    occurred_at_origin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    human_review_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    recompute_run_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fact_set_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    root_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(256), nullable=False)
    label_id: Mapped[str] = mapped_column(String(128), nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    authority: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="recorded")
    active_slot: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    review_decision_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelFactHead(Base, TimestampMixin):
    __tablename__ = "label_fact_heads"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_head_id",
            name="uq_label_fact_heads_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_namespace",
            "logical_key_sha",
            name="uq_label_fact_heads_scope_key",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "current_revision > 0 AND generation > 0",
            name="ck_label_fact_heads_versions",
        ),
        CheckConstraint(
            "LENGTH(logical_key_sha) = 64",
            name="ck_label_fact_heads_logical_hash",
        ),
        Index(
            "ix_label_fact_heads_scope_current",
            "tenant_id",
            "project_id",
            "fact_namespace",
            "current_fact_id",
        ),
        Index("ix_label_fact_heads_trace_id", "trace_id"),
    )

    fact_head_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    logical_key_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    current_fact_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelFactSetHead(Base, TimestampMixin):
    __tablename__ = "label_fact_set_heads"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "fact_set_head_id",
            name="uq_label_fact_set_heads_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "fact_namespace",
            name="uq_label_fact_set_heads_scope_env",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("generation > 0", name="ck_label_fact_set_heads_generation"),
        CheckConstraint("status = 'active'", name="ck_label_fact_set_heads_status"),
        CheckConstraint(
            "(previous_fact_set_id IS NULL AND previous_manifest_sha256 IS NULL) OR "
            "(previous_fact_set_id IS NOT NULL AND previous_manifest_sha256 IS NOT NULL)",
            name="ck_label_fact_set_heads_previous_pair",
        ),
        CheckConstraint(
            "LENGTH(current_manifest_sha256) = 64 AND "
            "(previous_manifest_sha256 IS NULL OR LENGTH(previous_manifest_sha256) = 64)",
            name="ck_label_fact_set_heads_hashes",
        ),
        Index(
            "ix_label_fact_set_heads_scope_current",
            "tenant_id",
            "project_id",
            "environment",
            "current_fact_set_id",
        ),
        Index("ix_label_fact_set_heads_trace_id", "trace_id"),
    )

    fact_set_head_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    current_fact_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_fact_set_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelFactSetHeadEvent(Base):
    __tablename__ = "label_fact_set_head_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "fact_namespace",
            "generation",
            name="uq_label_fact_set_events_generation",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_label_fact_set_events_hash",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "generation > 0",
            name="ck_label_fact_set_events_generation",
        ),
        CheckConstraint(
            "(generation = 1 AND previous_generation IS NULL) OR "
            "(generation > 1 AND previous_generation = generation - 1)",
            name="ck_label_fact_set_events_previous_generation",
        ),
        CheckConstraint(
            "action IN ('bootstrap', 'promote', 'rollback')",
            name="ck_label_fact_set_events_action",
        ),
        CheckConstraint(
            "(old_fact_set_id IS NULL AND old_manifest_sha256 IS NULL) OR "
            "(old_fact_set_id IS NOT NULL AND old_manifest_sha256 IS NOT NULL)",
            name="ck_label_fact_set_events_old_pair",
        ),
        CheckConstraint(
            "(action = 'bootstrap' AND generation = 1 AND old_fact_set_id IS NULL) OR "
            "(action IN ('promote', 'rollback') AND generation > 1 "
            "AND old_fact_set_id IS NOT NULL)",
            name="ck_label_fact_set_events_transition",
        ),
        CheckConstraint(
            "LENGTH(new_manifest_sha256) = 64 AND LENGTH(content_sha256) = 64 AND "
            "(old_manifest_sha256 IS NULL OR LENGTH(old_manifest_sha256) = 64)",
            name="ck_label_fact_set_events_hashes",
        ),
        Index(
            "ix_label_fact_set_events_scope_timeline",
            "tenant_id",
            "project_id",
            "environment",
            "fact_namespace",
            "generation",
        ),
        Index(
            "ix_label_fact_set_events_scope_new_set",
            "tenant_id",
            "project_id",
            "new_fact_set_id",
            "effective_at",
        ),
        Index("ix_label_fact_set_events_trace_id", "trace_id"),
    )

    head_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    fact_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_fact_set_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    old_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    new_fact_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    new_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FeedbackExample(Base, TimestampMixin):
    __tablename__ = "feedback_examples"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_decision_id",
            "target_type",
            "target_id",
            name="uq_feedback_examples_decision_target",
        ),
        Index("ix_feedback_examples_scope_type", "tenant_id", "project_id", "feedback_type"),
    )

    feedback_example_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    review_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(128), nullable=False)
    feedback_type: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    field_diff: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    before_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    after_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    gold_status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelTaxonomySuggestion(Base, TimestampMixin):
    __tablename__ = "label_taxonomy_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            "normalized_label",
            "status",
            name="uq_taxonomy_suggestions_open_label",
        ),
        Index("ix_taxonomy_suggestions_scope_status", "tenant_id", "project_id", "status"),
    )

    suggestion_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_label: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_labels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    observation_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    proposed_action: Mapped[str] = mapped_column(String(32), nullable=False, default="review")
    canonical_target_label_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    review_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PromptAsset(Base, TimestampMixin):
    __tablename__ = "prompt_assets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "prompt_asset_id", name="uq_prompt_assets_scope"
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "capability", "name", name="uq_prompt_assets_name"
        ),
    )

    prompt_asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    current_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class PromptVersion(Base, TimestampMixin):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "prompt_asset_id",
            "version",
            name="uq_prompt_versions_asset_version",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "content_sha256", name="uq_prompt_versions_scope_hash"
        ),
        Index("ix_prompt_versions_scope_status", "tenant_id", "project_id", "status"),
    )

    prompt_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    template_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    generation_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    structured_diff: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_badcase_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class ReleaseDeployment(Base, TimestampMixin):
    __tablename__ = "release_deployments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "project_id", "deployment_id", name="uq_release_deployments_scope"
        ),
        Index("ix_release_deployments_scope_status", "tenant_id", "project_id", "status"),
    )

    deployment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="shadowing")
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregation_policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_dataset_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rollback_target_deployment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rollout_percentage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    blocked_reasons: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    monitor_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ReleaseCommand(Base, TimestampMixin):
    """Two-phase release command awaiting a trusted execution acknowledgement."""

    __tablename__ = "release_commands"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "command_id",
            name="uq_release_commands_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "run_id",
            name="uq_release_commands_scope_run",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "deployment_id",
            "active_slot",
            name="uq_release_commands_active_deployment",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_release_commands_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "action IN ('publish', 'approve-gray', 'promote', 'rollback')",
            name="ck_release_commands_action",
        ),
        CheckConstraint(
            "status IN ('pending', 'materializing', 'completed', 'blocked', 'failed')",
            name="ck_release_commands_status",
        ),
        CheckConstraint(
            "(status IN ('pending', 'materializing') AND active_slot = 'active') "
            "OR (status IN ('completed', 'blocked', 'failed') AND active_slot IS NULL)",
            name="ck_release_commands_active_slot",
        ),
        Index(
            "ix_release_commands_scope_status",
            "tenant_id",
            "project_id",
            "environment",
            "status",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_deployment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    active_slot: Mapped[str | None] = mapped_column(String(16), nullable=True, default="active")
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_deployment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_head_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_head_deployment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_head_bundle_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_by_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completion_receipt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ReleaseBundleHead(Base, TimestampMixin):
    """Unique effective bundle pointer for one tenant/project/environment."""

    __tablename__ = "release_bundle_heads"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            name="uq_release_bundle_heads_scope_environment",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "active_deployment_id",
            name="uq_release_bundle_heads_scope_deployment",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint("generation > 0", name="ck_release_bundle_heads_generation"),
        CheckConstraint("status = 'active'", name="ck_release_bundle_heads_status"),
        Index(
            "ix_release_bundle_heads_scope_active",
            "tenant_id",
            "project_id",
            "environment",
            "active_deployment_id",
        ),
    )

    release_head_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    active_deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    active_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregation_policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_dataset_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    bootstrapped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    activated_by_command_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey(
            "release_commands.command_id",
            name="fk_release_bundle_heads_activated_command",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ReleaseBundleHeadEvent(Base):
    """Append-only activation timeline for the effective release bundle head."""

    __tablename__ = "release_bundle_head_events"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment",
            "generation",
            name="uq_release_bundle_head_events_generation",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_release_bundle_head_events_hash",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("generation > 0", name="ck_release_bundle_head_events_generation"),
        CheckConstraint(
            "(generation = 1 AND previous_generation IS NULL) OR "
            "(generation > 1 AND previous_generation = generation - 1)",
            name="ck_release_bundle_head_events_previous_generation",
        ),
        CheckConstraint(
            "action IN ('bootstrap', 'activate', 'promote', 'start-draining', 'drained', "
            "'rollback', 'deactivate')",
            name="ck_release_bundle_head_events_action",
        ),
        CheckConstraint(
            "activation_status IN ('active', 'draining', 'inactive', 'rolled-back')",
            name="ck_release_bundle_head_events_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="ck_release_bundle_head_events_interval",
        ),
        Index(
            "ix_release_bundle_head_events_scope_timeline",
            "tenant_id",
            "project_id",
            "environment",
            "generation",
        ),
        Index(
            "ix_release_bundle_head_events_scope_label",
            "tenant_id",
            "project_id",
            "new_label_version_id",
            "effective_from",
        ),
    )

    head_event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_generation: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    activation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    old_deployment_id: Mapped[str | None] = mapped_column(String(128))
    new_deployment_id: Mapped[str | None] = mapped_column(String(128))
    old_label_version_id: Mapped[str | None] = mapped_column(String(128))
    new_label_version_id: Mapped[str | None] = mapped_column(String(128))
    old_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    new_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    command_id: Mapped[str | None] = mapped_column(String(128))
    completion_receipt_id: Mapped[str | None] = mapped_column(String(128))
    approval_id: Mapped[str | None] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EvidencePack(Base, TimestampMixin):
    __tablename__ = "evidence_packs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "evidence_pack_id",
            name="uq_evidence_packs_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "evidence_sha256",
            name="uq_evidence_packs_scope_hash",
        ),
        CheckConstraint(
            "status IN ('ready', 'superseded')",
            name="ck_evidence_packs_status",
        ),
        CheckConstraint(
            "LENGTH(audio_sha256) = 64 AND LENGTH(evidence_sha256) = 64",
            name="ck_evidence_packs_hashes",
        ),
        CheckConstraint(
            "window_start_ms >= 0 AND window_end_ms > window_start_ms",
            name="ck_evidence_packs_window",
        ),
        CheckConstraint(
            "resource_version > 0",
            name="ck_evidence_packs_resource_version",
        ),
        Index(
            "ix_evidence_packs_scope_audio_session",
            "tenant_id",
            "project_id",
            "audio_session_id",
        ),
        Index(
            "ix_evidence_packs_scope_recording",
            "tenant_id",
            "project_id",
            "recording_id",
        ),
    )

    evidence_pack_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    audio_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recording_id: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_object_version: Mapped[str] = mapped_column(String(512), nullable=False)
    audio_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    asr_result_id: Mapped[str] = mapped_column(String(256), nullable=False)
    asr_result_version: Mapped[str] = mapped_column(String(128), nullable=False)
    window_start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    source_run_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    resource_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    root_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HumanReviewTask(Base, TimestampMixin):
    __tablename__ = "human_review_tasks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_task_id",
            name="uq_human_review_tasks_scope",
        ),
    )

    review_task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HumanReviewDecision(Base, TimestampMixin):
    __tablename__ = "human_review_decisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "decision_id",
            name="uq_human_review_decisions_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "terminal_review_task_id",
            name="uq_human_review_decisions_terminal_task",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "decision_id",
            "terminal_review_task_id",
            name="uq_human_review_decisions_scope_terminal_binding",
        ),
        Index(
            "ix_human_review_decisions_scope_task",
            "tenant_id",
            "project_id",
            "review_task_id",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    review_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    terminal_review_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class CalibrationRound(Base, TimestampMixin):
    __tablename__ = "calibration_rounds"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            name="uq_cal_rounds_scope_id",
        ),
        CheckConstraint(
            "reviewer_a_id <> reviewer_b_id AND "
            "adjudicator_id <> reviewer_a_id AND adjudicator_id <> reviewer_b_id",
            name="ck_cal_rounds_participants",
        ),
        CheckConstraint(
            "status IN ('in_review', 'ready', 'published')",
            name="ck_cal_rounds_status",
        ),
        CheckConstraint(
            "sample_count > 0 AND paired_submission_count >= 0 AND "
            "paired_submission_count <= sample_count AND agreed_count >= 0 AND "
            "conflict_count >= 0 AND agreed_count + conflict_count = paired_submission_count AND "
            "adjudication_count >= 0 AND adjudication_count <= conflict_count AND "
            "excluded_count >= 0 AND excluded_count <= adjudication_count AND "
            "observed_agreement_ppm BETWEEN 0 AND 1000000 AND "
            "cohen_kappa_micros BETWEEN -1000000 AND 1000000",
            name="ck_cal_rounds_metrics",
        ),
        CheckConstraint(
            "cohen_kappa_defined OR cohen_kappa_micros = 0",
            name="ck_cal_rounds_kappa_defined",
        ),
        CheckConstraint("resource_version > 0", name="ck_cal_rounds_resource_version"),
        CheckConstraint(
            "status = 'in_review' OR "
            "(paired_submission_count = sample_count AND adjudication_count = conflict_count)",
            name="ck_cal_rounds_completion_state",
        ),
        CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR "
            "(status <> 'published' AND published_at IS NULL)",
            name="ck_cal_rounds_publish_state",
        ),
        Index(
            "ix_cal_rounds_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_cal_rounds_scope_dataset",
            "tenant_id",
            "project_id",
            "dataset_id",
            "dataset_version",
        ),
    )

    round_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    label_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_a_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_b_id: Mapped[str] = mapped_column(String(64), nullable=False)
    adjudicator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_review")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    paired_submission_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    agreed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adjudication_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_agreement_ppm: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cohen_kappa_micros: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cohen_kappa_defined: Mapped[bool] = mapped_column(nullable=False, default=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalibrationItem(Base, TimestampMixin):
    __tablename__ = "calibration_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_cal_items_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            "round_id",
            name="uq_cal_items_scope_id_round",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "ordinal",
            name="uq_cal_items_scope_round_pos",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "source_case_id",
            name="uq_cal_items_scope_round_case",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint(
            "status IN ('pending', 'agreed', 'conflicted', 'adjudicated', 'excluded')",
            name="ck_cal_items_status",
        ),
        CheckConstraint(
            "review_outcome IN ('pending', 'agreed', 'conflicted')",
            name="ck_cal_items_review_outcome",
        ),
        CheckConstraint(
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
        CheckConstraint("ordinal >= 0", name="ck_cal_items_ordinal"),
        CheckConstraint("resource_version > 0", name="ck_cal_items_resource_version"),
        CheckConstraint(
            "(adjudication_claimed_by IS NULL AND adjudication_claimed_at IS NULL) OR "
            "(status = 'conflicted' AND adjudication_claimed_by IS NOT NULL AND "
            "adjudication_claimed_at IS NOT NULL)",
            name="ck_cal_items_claim_state",
        ),
        Index(
            "ix_cal_items_scope_round_status",
            "tenant_id",
            "project_id",
            "round_id",
            "status",
        ),
    )

    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_case_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    review_outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    final_value_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    final_value_sha256: Mapped[str | None] = mapped_column(String(64))
    adjudication_claimed_by: Mapped[str | None] = mapped_column(String(64))
    adjudication_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class CalibrationAssignment(Base, TimestampMixin):
    __tablename__ = "calibration_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            name="uq_cal_assign_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "slot",
            name="uq_cal_assign_scope_item_slot",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
            "reviewer_id",
            name="uq_cal_assign_scope_item_reviewer",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_task_id",
            name="uq_cal_assign_scope_review_task",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            "round_id",
            "item_id",
            "reviewer_id",
            name="uq_cal_assign_scope_binding",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("slot IN ('A', 'B')", name="ck_cal_assign_slot"),
        CheckConstraint(
            "status IN ('pending', 'submitted')",
            name="ck_cal_assign_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND submitted_at IS NULL) OR "
            "(status = 'submitted' AND submitted_at IS NOT NULL)",
            name="ck_cal_assign_submit_state",
        ),
        CheckConstraint("resource_version > 0", name="ck_cal_assign_resource_version"),
        Index(
            "ix_cal_assign_scope_reviewer",
            "tenant_id",
            "project_id",
            "reviewer_id",
            "status",
        ),
        Index(
            "ix_cal_assign_scope_round",
            "tenant_id",
            "project_id",
            "round_id",
        ),
    )

    assignment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    slot: Mapped[str] = mapped_column(String(1), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class CalibrationSubmission(Base):
    __tablename__ = "calibration_submissions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "submission_id",
            name="uq_cal_subs_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "assignment_id",
            name="uq_cal_subs_scope_assignment",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "submission_id",
            "round_id",
            "item_id",
            name="uq_cal_subs_scope_binding",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint("resource_version = 1", name="ck_cal_subs_resource_version"),
        Index(
            "ix_cal_subs_scope_round_item",
            "tenant_id",
            "project_id",
            "round_id",
            "item_id",
        ),
    )

    submission_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    assignment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_value_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CalibrationAdjudication(Base):
    __tablename__ = "calibration_adjudications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "adjudication_id",
            name="uq_cal_adjud_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_cal_adjud_scope_item",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "decision IN ('accept_a', 'accept_b', 'revise', 'exclude')",
            name="ck_cal_adjud_decision",
        ),
        CheckConstraint("LENGTH(TRIM(reason)) > 0", name="ck_cal_adjud_reason"),
        CheckConstraint(
            "(decision IN ('accept_a', 'accept_b') AND accepted_submission_id IS NOT NULL "
            "AND value_json IS NOT NULL AND canonical_value_sha256 IS NOT NULL) OR "
            "(decision = 'revise' AND accepted_submission_id IS NULL AND "
            "value_json IS NOT NULL AND canonical_value_sha256 IS NOT NULL) OR "
            "(decision = 'exclude' AND accepted_submission_id IS NULL AND "
            "value_json IS NULL AND canonical_value_sha256 IS NULL)",
            name="ck_cal_adjud_resolution",
        ),
        CheckConstraint("resource_version = 1", name="ck_cal_adjud_resource_version"),
        Index(
            "ix_cal_adjud_scope_round",
            "tenant_id",
            "project_id",
            "round_id",
        ),
    )

    adjudication_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    adjudicator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    accepted_submission_id: Mapped[str | None] = mapped_column(String(128))
    value_json: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    canonical_value_sha256: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GoldSetSeries(Base, TimestampMixin):
    """Serializable version allocator for one tenant/project gold-set series."""

    __tablename__ = "gold_set_series"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_key",
            name="uq_gold_series_scope_key",
        ),
        CheckConstraint("next_version > 0", name="ck_gold_series_next_version"),
        Index(
            "ix_gold_series_scope_key",
            "tenant_id",
            "project_id",
            "gold_set_key",
        ),
    )

    gold_set_series_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gold_set_key: Mapped[str] = mapped_column(String(128), nullable=False)
    next_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class GoldSetVersion(Base):
    __tablename__ = "gold_set_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            name="uq_gold_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "round_id",
            name="uq_gold_versions_scope_round",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_key",
            "version_number",
            name="uq_gold_versions_scope_series",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "round_id",
            name="uq_gold_versions_scope_binding",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint("status = 'published'", name="ck_gold_versions_status"),
        CheckConstraint(
            "version_number > 0 AND sample_count > 0 AND annotation_count > 0 AND "
            "excluded_count >= 0 AND annotation_count + excluded_count = sample_count AND "
            "conflict_count >= 0 AND conflict_count <= sample_count AND "
            "adjudication_count = conflict_count AND excluded_count <= adjudication_count AND "
            "observed_agreement_ppm BETWEEN 0 AND 1000000 AND "
            "cohen_kappa_micros BETWEEN -1000000 AND 1000000",
            name="ck_gold_versions_metrics",
        ),
        CheckConstraint(
            "cohen_kappa_defined OR cohen_kappa_micros = 0",
            name="ck_gold_versions_kappa_defined",
        ),
        CheckConstraint("resource_version = 1", name="ck_gold_versions_resource_version"),
        Index(
            "ix_gold_versions_scope_series",
            "tenant_id",
            "project_id",
            "gold_set_key",
            "version_number",
        ),
    )

    gold_set_version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    gold_set_key: Mapped[str] = mapped_column(String(128), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    label_version: Mapped[str] = mapped_column(String(128), nullable=False)
    rubric_version: Mapped[str] = mapped_column(String(128), nullable=False)
    sample_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    annotation_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="published")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    annotation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    observed_agreement_ppm: Mapped[int] = mapped_column(Integer, nullable=False)
    cohen_kappa_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    cohen_kappa_defined: Mapped[bool] = mapped_column(nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False)
    adjudication_count: Mapped[int] = mapped_column(Integer, nullable=False)
    published_by: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GoldAnnotation(Base):
    __tablename__ = "gold_annotations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_annotation_id",
            name="uq_gold_annotations_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "gold_set_version_id",
            "item_id",
            name="uq_gold_annotations_scope_item",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
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
        CheckConstraint(
            "resolution_source IN ('agreed', 'adjudicated')",
            name="ck_gold_annotations_source",
        ),
        CheckConstraint("resource_version = 1", name="ck_gold_annotations_resource_version"),
        Index(
            "ix_gold_annotations_scope_version",
            "tenant_id",
            "project_id",
            "gold_set_version_id",
        ),
    )

    gold_annotation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    gold_set_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_id: Mapped[str] = mapped_column(String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_case_id: Mapped[str] = mapped_column(String(256), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_value_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_source: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class QualityAppeal(Base, TimestampMixin):
    __tablename__ = "quality_appeals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "appeal_id",
            name="uq_quality_appeals_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_decision_id",
            name="uq_quality_appeals_scope_source_decision",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "review_task_id",
            name="uq_quality_appeals_scope_review_task",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "appeal_decision_id",
            name="uq_quality_appeals_scope_appeal_decision",
        ),
        ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "source_decision_id",
                "source_review_task_id",
            ],
            [
                "human_review_decisions.tenant_id",
                "human_review_decisions.project_id",
                "human_review_decisions.decision_id",
                "human_review_decisions.terminal_review_task_id",
            ],
            name="fk_quality_appeals_scope_terminal_decision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "review_task_id"],
            [
                "human_review_tasks.tenant_id",
                "human_review_tasks.project_id",
                "human_review_tasks.review_task_id",
            ],
            name="fk_quality_appeals_scope_review_task",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "appeal_decision_id", "review_task_id"],
            [
                "human_review_decisions.tenant_id",
                "human_review_decisions.project_id",
                "human_review_decisions.decision_id",
                "human_review_decisions.terminal_review_task_id",
            ],
            name="fk_quality_appeals_scope_appeal_decision",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('submitted', 'under_review', 'resolved', 'withdrawn')",
            name="ck_quality_appeals_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN "
            "('original_upheld', 'original_overturned', 'original_remanded')",
            name="ck_quality_appeals_decision",
        ),
        CheckConstraint(
            "(status = 'resolved' AND decision IS NOT NULL AND resolved_at IS NOT NULL "
            "AND appeal_decision_id IS NOT NULL) OR "
            "(status <> 'resolved' AND decision IS NULL AND resolved_at IS NULL "
            "AND appeal_decision_id IS NULL)",
            name="ck_quality_appeals_resolution_state",
        ),
        CheckConstraint(
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL) OR "
            "(status <> 'withdrawn' AND withdrawn_at IS NULL)",
            name="ck_quality_appeals_withdrawal_state",
        ),
        Index(
            "ix_quality_appeals_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_quality_appeals_scope_appellant",
            "tenant_id",
            "project_id",
            "appellant_id",
        ),
        Index(
            "ix_quality_appeals_scope_reviewer",
            "tenant_id",
            "project_id",
            "reviewer_id",
        ),
    )

    appeal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_review_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    review_task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    appeal_decision_id: Mapped[str | None] = mapped_column(String(128))
    source_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_decider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    appellant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    reviewer_id: Mapped[str | None] = mapped_column(String(64))
    decision: Mapped[str | None] = mapped_column(String(32))
    decision_reason: Mapped[str | None] = mapped_column(String(2000))
    withdrawal_reason: Mapped[str | None] = mapped_column(String(1000))
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeSource(Base, TimestampMixin):
    __tablename__ = "knowledge_sources"

    knowledge_source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class KnowledgeIndex(Base, TimestampMixin):
    __tablename__ = "knowledge_indexes"

    knowledge_index_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class KnowledgeQualityGate(Base, TimestampMixin):
    __tablename__ = "knowledge_quality_gates"

    knowledge_gate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="draft", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class KnowledgeEffect(Base, TimestampMixin):
    __tablename__ = "knowledge_effects"

    effect_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="success", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class EvalDatasetVersion(Base, TimestampMixin):
    """Immutable evaluation dataset snapshot used by release gates.

    ``JsonResource(eval_datasets)`` remains the UI projection. This table is the
    authoritative lock record that binds a versioned manifest to an exact hash.
    """

    __tablename__ = "eval_dataset_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_dataset_id",
            name="uq_eval_dataset_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            "dataset_version",
            name="uq_eval_dataset_versions_scope_name_version",
        ),
        CheckConstraint("sample_count > 0", name="ck_eval_dataset_versions_sample_count"),
        CheckConstraint(
            "resource_version > 0",
            name="ck_eval_dataset_versions_resource_version",
        ),
        CheckConstraint(
            "status IN ('draft', 'locked', 'deprecated', 'archived')",
            name="ck_eval_dataset_versions_status",
        ),
        Index(
            "ix_eval_dataset_versions_scope_status",
            "tenant_id",
            "project_id",
            "capability",
            "status",
        ),
    )

    eval_dataset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    manifest_storage_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    manifest_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manifest_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manifest_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    manifest_etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelEvalResult(Base, TimestampMixin):
    """Immutable, release-gated result for one locked labeling EvalRun."""

    __tablename__ = "label_eval_results"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_run_id",
            name="uq_label_eval_results_scope_run",
        ),
        Index(
            "ix_label_eval_results_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        CheckConstraint(
            "status IN ('passed', 'blocked')",
            name="ck_label_eval_results_status",
        ),
    )

    eval_result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    eval_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sample_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    overall_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    bootstrap_ci: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    gate_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class LabelEvalSuiteResult(Base, TimestampMixin):
    """Immutable per-suite evidence behind a LabelEvalResult."""

    __tablename__ = "label_eval_suite_results"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "eval_result_id",
            "suite",
            name="uq_label_eval_suite_results_scope_suite",
        ),
        Index(
            "ix_label_eval_suite_results_scope_result",
            "tenant_id",
            "project_id",
            "eval_result_id",
        ),
        CheckConstraint("sample_count > 0", name="ck_label_eval_suite_results_sample_count"),
        CheckConstraint(
            "suite IN ('golden', 'boundary', 'adversarial', 'fresh', 'canary', 'regression')",
            name="ck_label_eval_suite_results_suite",
        ),
    )

    suite_result_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    eval_result_id: Mapped[str] = mapped_column(String(128), nullable=False)
    suite: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    suite_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelOptimizationSchedule(Base, TimestampMixin):
    """One durable scheduler configuration and mutex per labeling scope.

    ``scan_claim_token`` is acquired with a conditional UPDATE by the worker.  The
    unique scope constraint means there cannot be two independent active clocks
    for the same tenant/project/label version.
    """

    __tablename__ = "label_optimization_schedules"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "label_version_id",
            name="uq_label_opt_schedules_scope",
        ),
        Index(
            "ix_label_opt_schedules_due",
            "status",
            "next_threshold_scan_at",
            "next_daily_at",
            "next_weekly_at",
        ),
        CheckConstraint(
            "status IN ('active', 'paused')",
            name="ck_label_opt_schedules_status",
        ),
        CheckConstraint(
            "threshold_interval_seconds >= 900",
            name="ck_label_opt_schedules_threshold_interval",
        ),
        CheckConstraint(
            "daily_hour BETWEEN 0 AND 23",
            name="ck_label_opt_schedules_daily_hour",
        ),
        CheckConstraint(
            "weekly_day BETWEEN 0 AND 6",
            name="ck_label_opt_schedules_weekly_day",
        ),
        CheckConstraint(
            "resource_version > 0",
            name="ck_label_opt_schedules_resource_version",
        ),
    )

    schedule_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregation_policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_dataset_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    schedule_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Asia/Shanghai"
    )
    threshold_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=900)
    daily_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    weekly_day: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    next_threshold_scan_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_daily_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_weekly_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_threshold_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_daily_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_weekly_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_run_id: Mapped[str | None] = mapped_column(String(128), index=True)
    baseline_snapshot_id: Mapped[str | None] = mapped_column(String(128))
    scan_claim_token: Mapped[str | None] = mapped_column(String(64))
    scan_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelOptimizationMetricSnapshot(Base, TimestampMixin):
    """Append-only authoritative input to one optimization trigger decision."""

    __tablename__ = "label_optimization_metric_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "schedule_id",
            "snapshot_sha256",
            name="uq_label_opt_metric_snapshots_hash",
        ),
        Index(
            "ix_label_opt_metric_snapshots_scope_window",
            "tenant_id",
            "project_id",
            "label_version_id",
            "window_ended_at",
        ),
        CheckConstraint(
            "snapshot_kind IN ('baseline', 'window')",
            name="ck_label_opt_metric_snapshots_kind",
        ),
        CheckConstraint(
            "window_ended_at >= window_started_at",
            name="ck_label_opt_metric_snapshots_window",
        ),
        CheckConstraint(
            "rejection_count >= 0",
            name="ck_label_opt_metric_snapshots_rejections",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregation_policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_dataset_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reason_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rejection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_records: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)


class LabelOptimizationRound(Base, TimestampMixin):
    """Durable state for one bounded candidate-generation/evaluation round."""

    __tablename__ = "label_optimization_rounds"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "optimization_run_id",
            "round_number",
            name="uq_label_opt_rounds_run_number",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "generation_run_id",
            name="uq_label_opt_rounds_generation_run",
        ),
        Index(
            "ix_label_opt_rounds_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        CheckConstraint(
            "round_number BETWEEN 1 AND 3",
            name="ck_label_opt_rounds_number",
        ),
        CheckConstraint(
            "candidate_count BETWEEN 0 AND 5",
            name="ck_label_opt_rounds_candidate_count",
        ),
        CheckConstraint(
            "cost_spent_micros >= 0",
            name="ck_label_opt_rounds_cost",
        ),
        CheckConstraint(
            "consecutive_failed_rounds >= 0",
            name="ck_label_opt_rounds_failures",
        ),
        CheckConstraint(
            "status IN ('generating-candidates', 'evaluating', 'completed', "
            "'failed', 'blocked', 'awaiting-review')",
            name="ck_label_opt_rounds_status",
        ),
    )

    round_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    optimization_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    label_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregation_policy_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    eval_dataset_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="generating-candidates")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    eval_run_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    selected_prompt_version_id: Mapped[str | None] = mapped_column(String(128))
    latest_gain_ppm: Mapped[int | None] = mapped_column(Integer)
    critical_metric_regressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cost_spent_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    consecutive_failed_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_reason_codes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HotwordPack(Base, TimestampMixin):
    __tablename__ = "hotword_packs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "pack_id",
            name="uq_hotword_packs_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "name",
            "language",
            "domain",
            name="uq_hotword_packs_scope_name",
        ),
        CheckConstraint("resource_version > 0", name="ck_hotword_packs_resource_version"),
        Index(
            "ix_hotword_packs_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
    )

    pack_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    domain: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    current_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    production_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class HotwordPackVersion(Base, TimestampMixin):
    __tablename__ = "hotword_pack_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "version_id",
            name="uq_hotword_pack_versions_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "pack_id",
            "version",
            name="uq_hotword_pack_versions_scope_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "pack_id"],
            ["hotword_packs.tenant_id", "hotword_packs.project_id", "hotword_packs.pack_id"],
            name="fk_hotword_pack_versions_scope_pack",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        ForeignKeyConstraint(
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
        CheckConstraint("resource_version > 0", name="ck_hotword_pack_versions_resource_version"),
        CheckConstraint(
            "status IN ('draft', 'validating', 'ready_for_eval', 'evaluating', "
            "'gate_blocked', 'review_required', 'approved', 'published', 'deprecated', "
            "'rolled_back', 'archived')",
            name="ck_hotword_pack_versions_status",
        ),
        Index(
            "ix_hotword_pack_versions_scope_status",
            "tenant_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_hotword_pack_versions_scope_pack",
            "tenant_id",
            "project_id",
            "pack_id",
        ),
    )

    version_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pack_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_storage_object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    eval_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    eval_locked: Mapped[bool] = mapped_column(nullable=False, default=False)
    model_approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_admin_confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    compiled_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HotwordVersionItem(Base, TimestampMixin):
    __tablename__ = "hotword_version_items"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "item_id",
            name="uq_hotword_version_items_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "version_id",
            "normalized_term",
            name="uq_hotword_version_items_scope_term",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_badcase_id"],
            ["badcases.tenant_id", "badcases.project_id", "badcases.badcase_id"],
            name="fk_hotword_version_items_scope_badcase",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint("weight BETWEEN 0 AND 100", name="ck_hotword_version_items_weight"),
        CheckConstraint("resource_version > 0", name="ck_hotword_version_items_resource_version"),
        CheckConstraint(
            "source_type IN ('manual', 'badcase', 'knowledge_candidate')",
            name="ck_hotword_version_items_source_type",
        ),
        Index(
            "ix_hotword_version_items_scope_version",
            "tenant_id",
            "project_id",
            "version_id",
        ),
    )

    item_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_term: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_term: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False)
    source_badcase_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class HotwordMetricSnapshot(Base, TimestampMixin):
    __tablename__ = "hotword_metric_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "snapshot_id",
            name="uq_hotword_metric_snapshots_scope_id",
        ),
        ForeignKeyConstraint(
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
        Index(
            "ix_hotword_metric_snapshots_scope_bucket",
            "tenant_id",
            "project_id",
            "bucket_start",
            "bucket_end",
        ),
        Index(
            "ix_hotword_metric_snapshots_scope_dimensions",
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ),
        CheckConstraint(
            "expected_count >= 0 AND correct_count >= 0 AND weighted_error_count >= 0 "
            "AND false_insert_count >= 0 AND recognized_hotword_count >= 0 "
            "AND impacted_session_count >= 0 AND correct_count <= expected_count",
            name="ck_hotword_metric_snapshots_counts",
        ),
        CheckConstraint(
            "evidence_confidence BETWEEN 0 AND 1",
            name="ck_hotword_metric_snapshots_evidence_confidence",
        ),
        CheckConstraint(
            "bucket_end > bucket_start",
            name="ck_hotword_metric_snapshots_bucket",
        ),
    )

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    hotword_pack_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    standard_term: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weighted_error_count: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    false_insert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recognized_hotword_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    impacted_session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class Badcase(Base, TimestampMixin):
    __tablename__ = "badcases"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "badcase_id",
            name="uq_badcases_scope",
        ),
        CheckConstraint("resource_version > 0", name="ck_badcases_resource_version"),
        CheckConstraint(
            "error_type IS NULL OR error_type IN ('missing_term', 'misrecognition', "
            "'alias_gap', 'weight_issue', 'false_boost')",
            name="ck_badcases_hotword_error_type",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "hotword_pack_version_id"],
            [
                "hotword_pack_versions.tenant_id",
                "hotword_pack_versions.project_id",
                "hotword_pack_versions.version_id",
            ],
            name="fk_badcases_scope_hotword_version",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "ix_badcases_scope_capability_status",
            "tenant_id",
            "project_id",
            "capability",
            "status",
        ),
        Index(
            "ix_badcases_scope_hotword_version",
            "tenant_id",
            "project_id",
            "hotword_pack_version_id",
        ),
    )

    badcase_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending-attribution")
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    capability: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    standard_term: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recognized_text: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    evidence_storage_object_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hotword_pack_version_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    correct_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weighted_error_count: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    manual_correction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    candidate_state: Mapped[str] = mapped_column(String(32), nullable=False, default="suspected")
    root_cause: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fix_suggestion: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    downstream_impact: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)


class AsrAnnotationCorrection(Base, TimestampMixin):
    """Append-only ASR transcript correction eligible only for discovery statistics."""

    __tablename__ = "asr_annotation_corrections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "correction_id",
            name="uq_asr_annotation_corrections_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "annotation_id",
            name="uq_asr_annotation_corrections_scope_annotation",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "correction_fingerprint",
            name="uq_asr_annotation_corrections_scope_fingerprint",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "evidence_storage_object_id",
            name="uq_asr_annotation_corrections_scope_evidence",
        ),
        ForeignKeyConstraint(
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
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_badcase_id"],
            ["badcases.tenant_id", "badcases.project_id", "badcases.badcase_id"],
            name="fk_asr_annotation_corrections_scope_badcase",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        CheckConstraint(
            "status = 'submitted'",
            name="ck_asr_annotation_corrections_status",
        ),
        CheckConstraint(
            "evidence_level = 'discovery'",
            name="ck_asr_annotation_corrections_evidence_level",
        ),
        CheckConstraint(
            "error_type IN ('missing_term', 'misrecognition', 'alias_gap', "
            "'weight_issue', 'false_boost')",
            name="ck_asr_annotation_corrections_error_type",
        ),
        Index(
            "ix_asr_annotation_corrections_scope_observed",
            "tenant_id",
            "project_id",
            "observed_at",
        ),
        Index(
            "ix_asr_annotation_corrections_scope_dimensions",
            "tenant_id",
            "project_id",
            "store_id",
            "provider",
            "model_version",
            "hotword_pack_version_id",
        ),
        Index(
            "ix_asr_annotation_corrections_scope_term",
            "tenant_id",
            "project_id",
            "normalized_term",
        ),
    )

    correction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    annotation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    audio_session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="submitted")
    standard_term: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_term: Mapped[str] = mapped_column(String(255), nullable=False)
    recognized_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    corrected_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    error_type: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_storage_object_id: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_window: Mapped[str] = mapped_column(String(128), nullable=False)
    hotword_pack_version_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_badcase_id: Mapped[str] = mapped_column(String(128), nullable=False)
    store_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_level: Mapped[str] = mapped_column(String(32), nullable=False, default="discovery")
    correction_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    root_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    current_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "operation",
            "idempotency_key",
            name="uq_idempotency_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), default="completed", server_default="completed", nullable=False
    )
    owner_token: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunCompletionReceipt(Base):
    __tablename__ = "run_completion_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "completion_receipt_id",
            name="uq_run_completion_receipts_scope_receipt",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "run_id",
            name="uq_run_completion_receipts_scope_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "run_id"],
            ["run_records.tenant_id", "run_records.project_id", "run_records.run_id"],
            name="fk_run_completion_receipts_scope_run",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        Index(
            "ix_run_completion_receipts_scope_received",
            "tenant_id",
            "project_id",
            "received_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_receipt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_state: Mapped[str] = mapped_column(
        String(16), default="processing", server_default="processing", nullable=False
    )
    processing_token: Mapped[str] = mapped_column(String(64), nullable=False)
    completion_status: Mapped[str | None] = mapped_column(String(32))
    status_code: Mapped[int | None] = mapped_column(Integer)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(256))
    request_body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    signature_key_id: Mapped[str | None] = mapped_column(String(128))
    authenticated_source: Mapped[str | None] = mapped_column(String(128))
    signature_nonce: Mapped[str | None] = mapped_column(String(128))
    signature_request_hash: Mapped[str | None] = mapped_column(String(64))
    signature_body_hash: Mapped[str | None] = mapped_column(String(64))
    signature_mode: Mapped[str | None] = mapped_column(String(64))
    signed_at: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"

    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    object_type: Mapped[str] = mapped_column(String(80), nullable=False)
    object_id: Mapped[str] = mapped_column(String(256), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_idempotency_key",
            name="uq_outbox_events_dispatch_idempotency_key",
        ),
        Index(
            "ix_outbox_events_claim",
            "status",
            "available_at",
            "event_id",
        ),
        Index(
            "ix_outbox_events_reclaim",
            "status",
            "lease_expires_at",
            "event_id",
        ),
        Index(
            "ix_outbox_events_scope_aggregate",
            "tenant_id",
            "project_id",
            "aggregate_id",
            "event_id",
        ),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    dispatch_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_request_sha256: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reconcile_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(32), default="ready", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(1024))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    claim_token: Mapped[str | None] = mapped_column(String(64))
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_generation: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OutboxDeliveryAttempt(Base):
    __tablename__ = "outbox_delivery_attempts"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "lease_generation",
            name="uq_outbox_delivery_attempts_event_generation",
        ),
        Index(
            "ix_outbox_delivery_attempts_scope_event",
            "tenant_id",
            "project_id",
            "event_id",
        ),
        Index(
            "ix_outbox_delivery_attempts_status_started",
            "status",
            "started_at",
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("outbox_events.event_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    claim_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    delivery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatch_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    adapter: Mapped[str | None] = mapped_column(String(64))
    operation: Mapped[str | None] = mapped_column(String(128))
    remote_id: Mapped[str | None] = mapped_column(String(256))
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ExternalCallbackReceipt(Base, TimestampMixin):
    __tablename__ = "external_callback_receipts"

    callback_receipt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), index=True, default="success", nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


def _install_append_only_guards(table: Any) -> None:
    """Install database guards for evidence records that must never mutate."""

    for action in ("UPDATE", "DELETE"):
        suffix = action.lower()
        trigger_name = f"trg_{table.name}_no_{suffix}"
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table.name} "
                "BEGIN SELECT RAISE(ABORT, 'append-only calibration record'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table.name} "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                "SET MESSAGE_TEXT = 'append-only calibration record'"
            ).execute_if(dialect="mysql"),
        )


for _append_only_table in (
    CalibrationSubmission.__table__,
    CalibrationAdjudication.__table__,
    GoldSetVersion.__table__,
    GoldAnnotation.__table__,
    LabelCalibrationVersion.__table__,
    AsrAnnotationCorrection.__table__,
    LabelOptimizationMetricSnapshot.__table__,
):
    _install_append_only_guards(_append_only_table)


def _install_experiment_fact_guards(table: Any) -> None:
    for action in ("UPDATE", "DELETE"):
        suffix = action.lower()
        trigger_name = f"trg_{table.name}_no_{suffix}"
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table.name} "
                f"BEGIN SELECT RAISE(ABORT, 'append-only {table.name}'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {trigger_name} BEFORE {action} ON {table.name} "
                "FOR EACH ROW SIGNAL SQLSTATE '45000' "
                f"SET MESSAGE_TEXT = 'append-only {table.name}'"
            ).execute_if(dialect="mysql"),
        )


for _experiment_fact_table in (
    ExperimentAssignment.__table__,
    ExperimentExposure.__table__,
    ExperimentOutcome.__table__,
    ExperimentMetricSnapshot.__table__,
    ExperimentDecision.__table__,
):
    _install_experiment_fact_guards(_experiment_fact_table)


for _label_lifecycle_append_only_table in (
    MetricResult.__table__,
    MetricResultLabelScope.__table__,
    InsightReportMetricBinding.__table__,
    LabelMappingItem.__table__,
    LabelMappingItemTarget.__table__,
    LabelMappingBundleSource.__table__,
    LabelMappingBundleMember.__table__,
    LabelMappingBundlePath.__table__,
    LabelFact.__table__,
    LabelFactSetHeadEvent.__table__,
):
    _install_experiment_fact_guards(_label_lifecycle_append_only_table)


def _install_label_fact_contract_insert_guard(table: Any) -> None:
    """Reject legacy mutable Fact projections after the Contract migration."""

    event.listen(
        table,
        "after_create",
        DDL(
            "CREATE TRIGGER trg_label_facts_contract_insert "
            "BEFORE INSERT ON label_facts "
            "WHEN NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
            "OR NEW.active_slot IS NOT NULL "
            "OR (NEW.source_kind = 'human-decision' "
            "AND NEW.aggregate_id IS NOT NULL) "
            "BEGIN SELECT RAISE(ABORT, "
            "'label_facts contract requires recorded rows'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            "CREATE TRIGGER trg_label_facts_contract_insert "
            "BEFORE INSERT ON label_facts FOR EACH ROW "
            "BEGIN IF NEW.source_kind IS NULL OR NEW.status <> 'recorded' "
            "OR NEW.active_slot IS NOT NULL "
            "OR (NEW.source_kind = 'human-decision' "
            "AND NEW.aggregate_id IS NOT NULL) THEN SIGNAL SQLSTATE '45000' "
            "SET MESSAGE_TEXT = 'label_facts contract requires recorded rows'; END IF; END"
        ).execute_if(dialect="mysql"),
    )


_install_label_fact_contract_insert_guard(LabelFact.__table__)


def _install_release_head_interval_guards(table: Any) -> None:
    """Keep activation events immutable except for one ``effective_to`` closure."""

    immutable_columns = (
        "head_event_id",
        "tenant_id",
        "project_id",
        "environment",
        "generation",
        "previous_generation",
        "action",
        "activation_status",
        "old_deployment_id",
        "new_deployment_id",
        "old_label_version_id",
        "new_label_version_id",
        "old_bundle_sha256",
        "new_bundle_sha256",
        "effective_from",
        "command_id",
        "completion_receipt_id",
        "approval_id",
        "content_sha256",
        "actor_id",
        "root_trace_id",
        "trace_id",
        "payload",
        "created_at",
    )
    sqlite_equal = " AND ".join(f"OLD.{column} IS NEW.{column}" for column in immutable_columns)
    mysql_equal = " AND ".join(f"OLD.{column} <=> NEW.{column}" for column in immutable_columns)
    event.listen(
        table,
        "after_create",
        DDL(
            "CREATE TRIGGER trg_release_bundle_head_events_interval_update "
            "BEFORE UPDATE ON release_bundle_head_events "
            "WHEN NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL "
            "AND NEW.effective_to >= OLD.effective_from AND "
            f"{sqlite_equal}) BEGIN SELECT RAISE(ABORT, "
            "'release head interval permits one effective_to closure only'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            "CREATE TRIGGER trg_release_bundle_head_events_interval_update "
            "BEFORE UPDATE ON release_bundle_head_events FOR EACH ROW "
            "BEGIN IF NOT (OLD.effective_to IS NULL AND NEW.effective_to IS NOT NULL "
            "AND NEW.effective_to >= OLD.effective_from AND "
            f"{mysql_equal}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
            "'release head interval permits one effective_to closure only'; END IF; END"
        ).execute_if(dialect="mysql"),
    )
    sqlite_continuity_trigger = (
        "CREATE TRIGGER trg_release_bundle_head_events_interval_insert "
        "BEFORE INSERT ON release_bundle_head_events "
        "WHEN NEW.effective_to IS NOT NULL OR "
        "(NEW.generation = 1 AND EXISTS (SELECT 1 FROM release_bundle_head_events prior "
        "WHERE prior.tenant_id = NEW.tenant_id AND prior.project_id = NEW.project_id "
        "AND prior.environment = NEW.environment)) OR "
        "(NEW.generation > 1 AND NOT EXISTS (SELECT 1 FROM release_bundle_head_events prior "
        "WHERE prior.tenant_id = NEW.tenant_id AND prior.project_id = NEW.project_id "
        "AND prior.environment = NEW.environment "
        "AND prior.generation = NEW.previous_generation "
        "AND prior.effective_to = NEW.effective_from "
        "AND prior.new_deployment_id IS NEW.old_deployment_id "
        "AND prior.new_label_version_id IS NEW.old_label_version_id "
        "AND prior.new_bundle_sha256 IS NEW.old_bundle_sha256)) "
        "BEGIN SELECT RAISE(ABORT, "
        "'release head activation intervals must be continuous'); END"
    )
    mysql_continuity_trigger = (
        "CREATE TRIGGER trg_release_bundle_head_events_interval_insert "
        "BEFORE INSERT ON release_bundle_head_events FOR EACH ROW "
        "BEGIN IF NEW.effective_to IS NOT NULL OR "
        "(NEW.generation = 1 AND EXISTS (SELECT 1 FROM release_bundle_head_events prior "
        "WHERE prior.tenant_id = NEW.tenant_id AND prior.project_id = NEW.project_id "
        "AND prior.environment = NEW.environment)) OR "
        "(NEW.generation > 1 AND NOT EXISTS (SELECT 1 FROM release_bundle_head_events prior "
        "WHERE prior.tenant_id = NEW.tenant_id AND prior.project_id = NEW.project_id "
        "AND prior.environment = NEW.environment "
        "AND prior.generation = NEW.previous_generation "
        "AND prior.effective_to = NEW.effective_from "
        "AND prior.new_deployment_id <=> NEW.old_deployment_id "
        "AND prior.new_label_version_id <=> NEW.old_label_version_id "
        "AND prior.new_bundle_sha256 <=> NEW.old_bundle_sha256)) "
        "THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = "
        "'release head activation intervals must be continuous'; END IF; END"
    )
    event.listen(
        table,
        "after_create",
        DDL(sqlite_continuity_trigger).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(mysql_continuity_trigger).execute_if(dialect="mysql"),
    )
    for dialect, statement in (
        (
            "sqlite",
            "CREATE TRIGGER trg_release_bundle_head_events_no_delete "
            "BEFORE DELETE ON release_bundle_head_events "
            "BEGIN SELECT RAISE(ABORT, 'append-only release_bundle_head_events'); END",
        ),
        (
            "mysql",
            "CREATE TRIGGER trg_release_bundle_head_events_no_delete "
            "BEFORE DELETE ON release_bundle_head_events FOR EACH ROW "
            "SIGNAL SQLSTATE '45000' "
            "SET MESSAGE_TEXT = 'append-only release_bundle_head_events'",
        ),
    ):
        event.listen(table, "after_create", DDL(statement).execute_if(dialect=dialect))


_install_release_head_interval_guards(ReleaseBundleHeadEvent.__table__)


def _install_single_active_release_guard(table: Any) -> None:
    """Prevent two completed/100% deployments in one governed environment."""

    event.listen(
        table,
        "after_create",
        DDL(
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
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
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
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
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
        ).execute_if(dialect="mysql"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
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
        ).execute_if(dialect="mysql"),
    )


_install_single_active_release_guard(ReleaseDeployment.__table__)
