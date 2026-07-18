from __future__ import annotations

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import LabelTaxonomy, LabelVersion
from app.services.label_review_projection_service import sync_label_review_projection


def _context() -> RequestContext:
    return RequestContext(
        tenant_id="tenant_lifecycle_dual_write",
        project_id="project_lifecycle_dual_write",
        user_id="label_admin",
        roles=("admin",),
        request_id="request_lifecycle_dual_write",
        trace_id="trace_lifecycle_dual_write",
        idempotency_key="lifecycle-dual-write-v1",
    )


def test_taxonomy_and_label_version_projection_dual_write_strong_fields() -> None:
    ctx = _context()
    with SessionLocal() as session:
        taxonomy = sync_label_review_projection(
            session,
            ctx,
            "taxonomies",
            "taxonomy_service",
            {
                "taxonomy_id": "taxonomy_service",
                "name": "服务质检",
                "description": "服务场景",
                "status": "active",
            },
            status="active",
        )
        version = sync_label_review_projection(
            session,
            ctx,
            "label_versions",
            "lv_service_v1",
            {
                "label_version_id": "lv_service_v1",
                "taxonomy_id": "taxonomy_service",
                "semantic_version": "v1.0.0",
                "artifact_status": "draft",
                "content_sha256": "c" * 64,
                "status": "draft",
            },
            status="draft",
        )
        session.commit()

        assert isinstance(taxonomy, LabelTaxonomy)
        assert taxonomy.content_sha256
        assert isinstance(version, LabelVersion)
        assert version.taxonomy_id == "taxonomy_service"
        assert version.semantic_version == "v1.0.0"
        assert version.artifact_status == "draft"
        assert version.content_sha256 == "c" * 64


def test_label_version_api_uses_legacy_compatible_strong_field_reader(
    client,
    auth_headers: dict[str, str],
) -> None:
    response = client.get(
        "/api/v1/label-versions/label_v1_8_4",
        headers=auth_headers,
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == "v1.8.4"
    assert data["semantic_version"] == "v1.8.4"
    assert data["status"] == "published"
    assert data["artifact_status"] == "published"
