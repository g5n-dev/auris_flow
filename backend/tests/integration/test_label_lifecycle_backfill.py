from __future__ import annotations

from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    JsonResource,
    LabelTaxonomy,
    LabelVersion,
    LabelVersionItem,
    OutboxEvent,
    RunRecord,
)
from app.services.label_lifecycle_compat_service import run_label_lifecycle_backfill_batch


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="tenant_backfill",
        project_id="project_backfill",
        user_id="migration_worker",
        roles=("service",),
        request_id="request_label_lifecycle_backfill",
        trace_id="trace_label_lifecycle_backfill",
        idempotency_key="label-lifecycle-backfill-v1",
        actor_kind="service",
    )


def test_label_lifecycle_backfill_is_reentrant_and_reports_ambiguous_rows() -> None:
    ctx = _context()
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="taxonomies",
                resource_key="taxonomy_sales",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=ctx.trace_id,
                data={
                    "taxonomy_id": "taxonomy_sales",
                    "name": "销售质检",
                    "description": "销售场景标签体系",
                    "status": "active",
                },
            )
        )
        session.add_all(
            [
                LabelVersion(
                    label_version_id="lv_complete",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    status="published",
                    resource_version=1,
                    trace_id=ctx.trace_id,
                    payload={
                        "taxonomy_id": "taxonomy_sales",
                        "version": "v1.0.0",
                        "status": "published",
                        "published_at": "2026-07-18T05:00:00Z",
                        "content_sha256": "a" * 64,
                    },
                ),
                LabelVersion(
                    label_version_id="lv_ambiguous",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    status="shadow",
                    resource_version=1,
                    trace_id=ctx.trace_id,
                    payload={"version": "v2.0.0-rc1", "status": "shadow"},
                ),
            ]
        )
        session.add(
            LabelVersionItem(
                label_version_item_id="lvi_complete_quote",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                label_version_id="lv_complete",
                label_id="label_quote",
                canonical_name="报价",
                aliases=["价格"],
                value_type="boolean",
                risk_level="low",
                mutual_exclusion_group=None,
                parent_ids=[],
                aggregation_rule={"mode": "presence"},
                status="active",
                trace_id=ctx.trace_id,
            )
        )
        session.commit()

        result = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_label_lifecycle_backfill",
            batch_size=10,
        )
        session.commit()

        assert result["status"] == "blocked"
        assert result["scanned_count"] == 2
        assert result["updated_count"] == 2
        assert result["migration_required_count"] == 1
        assert result["ready_for_contract"] is False

        taxonomy = session.get(LabelTaxonomy, "taxonomy_sales")
        assert taxonomy is not None
        assert taxonomy.tenant_id == ctx.tenant_id
        complete = session.get(LabelVersion, "lv_complete")
        assert complete is not None
        assert complete.taxonomy_id == "taxonomy_sales"
        assert complete.semantic_version == "v1.0.0"
        assert complete.artifact_status == "published"
        assert complete.content_sha256 == "a" * 64
        ambiguous = session.get(LabelVersion, "lv_ambiguous")
        assert ambiguous is not None
        assert ambiguous.semantic_version == "v2.0.0-rc1"
        assert ambiguous.taxonomy_id is None
        assert ambiguous.artifact_status is None
        item = session.get(LabelVersionItem, "lvi_complete_quote")
        assert item is not None
        assert item.definition_sha256 is not None

        run = session.get(RunRecord, "run_label_lifecycle_backfill")
        assert run is not None
        assert run.status == "blocked"
        assert run.payload["migration_required"][0]["label_version_id"] == "lv_ambiguous"
        audit_count = (
            session.query(AuditLog).filter_by(object_id="run_label_lifecycle_backfill").count()
        )
        outbox_count = (
            session.query(OutboxEvent)
            .filter_by(aggregate_id="run_label_lifecycle_backfill")
            .count()
        )
        assert audit_count == 1
        assert outbox_count == 1

        replay = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_label_lifecycle_backfill",
            batch_size=10,
        )
        session.commit()
        assert replay["replayed"] is True
        assert (
            session.query(AuditLog).filter_by(object_id="run_label_lifecycle_backfill").count()
            == audit_count
        )
        assert (
            session.query(OutboxEvent)
            .filter_by(aggregate_id="run_label_lifecycle_backfill")
            .count()
            == outbox_count
        )


def test_label_lifecycle_backfill_resumes_from_persisted_cursor() -> None:
    ctx = RequestContext(
        tenant_id="tenant_backfill_resume",
        project_id="project_backfill_resume",
        user_id="migration_worker",
        roles=("service",),
        request_id="request_backfill_resume",
        trace_id="trace_backfill_resume",
        idempotency_key="label-lifecycle-backfill-resume-v1",
        actor_kind="service",
    )
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="taxonomies",
                resource_key="taxonomy_resume",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=ctx.trace_id,
                data={
                    "taxonomy_id": "taxonomy_resume",
                    "name": "断点续跑标签体系",
                    "status": "active",
                },
            )
        )
        for index in (1, 2):
            session.add(
                LabelVersion(
                    label_version_id=f"lv_resume_{index}",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    status="draft",
                    resource_version=1,
                    trace_id=ctx.trace_id,
                    payload={
                        "taxonomy_id": "taxonomy_resume",
                        "version": f"v{index}.0.0",
                        "status": "draft",
                        "content_sha256": str(index) * 64,
                    },
                )
            )
        session.commit()

        first = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_backfill_resume",
            batch_size=1,
        )
        session.commit()
        second = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_backfill_resume",
            batch_size=1,
        )
        session.commit()
        replay = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_backfill_resume",
            batch_size=1,
        )

        assert first["status"] == "running"
        assert first["scanned_count"] == 1
        assert second["status"] == "success"
        assert second["scanned_count"] == 2
        assert second["updated_count"] == 2
        assert second["ready_for_contract"] is True
        assert replay["replayed"] is True
        assert session.query(AuditLog).filter_by(object_id="run_backfill_resume").count() == 2
        assert session.query(OutboxEvent).filter_by(aggregate_id="run_backfill_resume").count() == 2


def test_backfill_quarantines_unresolved_taxonomy_without_fk_failure() -> None:
    ctx = RequestContext(
        tenant_id="tenant_backfill_quarantine",
        project_id="project_backfill_quarantine",
        user_id="migration_worker",
        roles=("service",),
        request_id="request_backfill_quarantine",
        trace_id="trace_backfill_quarantine",
        idempotency_key="label-lifecycle-backfill-quarantine-v1",
        actor_kind="service",
    )
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="taxonomies",
                resource_key="taxonomy_missing_name",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=ctx.trace_id,
                data={"taxonomy_id": "taxonomy_missing_name", "status": "active"},
            )
        )
        session.add(
            LabelVersion(
                label_version_id="lv_quarantine",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="draft",
                resource_version=1,
                trace_id=ctx.trace_id,
                payload={
                    "taxonomy_id": "taxonomy_missing_name",
                    "version": "v1.0.0",
                    "status": "draft",
                    "content_sha256": "e" * 64,
                },
            )
        )
        session.commit()

        result = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_backfill_quarantine",
            batch_size=10,
        )
        session.commit()

        record = session.get(LabelVersion, "lv_quarantine")
        assert result["status"] == "blocked"
        assert record is not None
        assert record.taxonomy_id is None
        assert record.semantic_version == "v1.0.0"
        assert record.artifact_status == "draft"
        assert record.content_sha256 == "e" * 64
        assert any(
            "taxonomy_id" in issue.get("missing_fields", [])
            for issue in result["migration_required"]
        )


def test_backfill_quarantines_duplicate_semantic_version_per_taxonomy() -> None:
    ctx = RequestContext(
        tenant_id="tenant_backfill_duplicate",
        project_id="project_backfill_duplicate",
        user_id="migration_worker",
        roles=("service",),
        request_id="request_backfill_duplicate",
        trace_id="trace_backfill_duplicate",
        idempotency_key="label-lifecycle-backfill-duplicate-v1",
        actor_kind="service",
    )
    with SessionLocal() as session:
        session.add(
            JsonResource(
                collection="taxonomies",
                resource_key="taxonomy_duplicate",
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status="active",
                trace_id=ctx.trace_id,
                data={
                    "taxonomy_id": "taxonomy_duplicate",
                    "name": "重复版本标签体系",
                    "status": "active",
                },
            )
        )
        for suffix, content in (("a", "a"), ("b", "b")):
            session.add(
                LabelVersion(
                    label_version_id=f"lv_duplicate_{suffix}",
                    tenant_id=ctx.tenant_id,
                    project_id=ctx.project_id,
                    status="draft",
                    resource_version=1,
                    trace_id=ctx.trace_id,
                    payload={
                        "taxonomy_id": "taxonomy_duplicate",
                        "version": "v1.0.0",
                        "status": "draft",
                        "content_sha256": content * 64,
                    },
                )
            )
        session.commit()

        result = run_label_lifecycle_backfill_batch(
            session,
            ctx,
            run_id="run_backfill_duplicate",
            batch_size=10,
        )
        session.commit()

        assert result["status"] == "blocked"
        assert result["updated_count"] == 1
        assert any(
            issue.get("reason_code") == "STRONG_FIELD_CONSTRAINT_VIOLATION"
            for issue in result["migration_required"]
        )
        materialized = session.scalars(
            select(LabelVersion).where(
                LabelVersion.tenant_id == ctx.tenant_id,
                LabelVersion.project_id == ctx.project_id,
                LabelVersion.semantic_version == "v1.0.0",
            )
        ).all()
        assert len(materialized) == 1
