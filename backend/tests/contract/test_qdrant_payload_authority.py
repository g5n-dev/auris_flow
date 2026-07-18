from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import JsonResource

FORGED_QDRANT_FIELDS = {
    "tenant_id": "forged_tenant",
    "project_id": "forged_project",
    "trace_id": "forged_trace",
    "collection": "other_collection",
    "vector_collection": "other_collection",
    "qdrant_collection": "../other_collection",
    "knowledge_index_id": "forged_index",
    "knowledge_source_id": "forged_source",
    "source_id": "forged_source",
    "source_type": "forged_source_type",
    "asset_key": "forged/asset",
    "version": "forged-version",
    "business_ref": {"connector_id": "forged_connector"},
    "affected_objects": [{"type": "knowledge_source", "id": "forged_source"}],
    "qdrant_payload": {
        "tenant_id": "forged_tenant",
        "project_id": "forged_project",
        "trace_id": "forged_trace",
        "collection": "knowledge_chunks/../../other_collection",
        "knowledge_index_id": "forged_index",
        "knowledge_source_id": "forged_source",
        "source_id": "forged_source",
        "source_type": "forged_source_type",
        "asset_key": "forged/asset",
        "version": "forged-version",
        "business_ref": {"connector_id": "forged_connector"},
    },
}


def assert_authoritative_scope(response_body: dict, auth_headers: dict[str, str]) -> None:
    data = response_body["data"]
    qdrant_payload = data["qdrant_payload"]

    assert data["tenant_id"] == auth_headers["X-Tenant-Id"]
    assert data["project_id"] == auth_headers["X-Project-Id"]
    assert data["trace_id"] == response_body["meta"]["trace_id"]
    assert qdrant_payload["tenant_id"] == auth_headers["X-Tenant-Id"]
    assert qdrant_payload["project_id"] == auth_headers["X-Project-Id"]
    assert qdrant_payload["trace_id"] == response_body["meta"]["trace_id"]
    assert qdrant_payload["collection"] == "knowledge_chunks"
    assert data["vector_collection"] == "knowledge_chunks"
    assert "collection" not in data
    assert "qdrant_collection" not in data


def test_knowledge_source_sync_ignores_forged_qdrant_authority(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-sources/ks_sales_policy/sync-runs",
        json={"reason": "authority-test", **FORGED_QDRANT_FIELDS},
        headers={**auth_headers, "Idempotency-Key": "qdrant-sync-authority"},
    )

    assert response.status_code == 202
    body = response.json()
    assert_authoritative_scope(body, auth_headers)
    data = body["data"]
    qdrant_payload = data["qdrant_payload"]
    assert data["reason"] == "authority-test"
    assert data["knowledge_source_id"] == "ks_sales_policy"
    assert data["source_id"] == "ks_sales_policy"
    assert data["source_type"] == "sop_faq_product_docs"
    assert data["asset_key"] == "auris/knowledge/ks_sales_policy"
    assert data["affected_objects"] == [{"type": "knowledge_source", "id": "ks_sales_policy"}]
    assert "knowledge_index_id" not in data
    assert qdrant_payload["knowledge_index_id"] is None
    assert qdrant_payload["knowledge_source_id"] == "ks_sales_policy"
    assert qdrant_payload["source_id"] == "ks_sales_policy"
    assert qdrant_payload["business_ref"]["connector_id"] == "conn_platform_auth"


def test_knowledge_index_build_ignores_forged_qdrant_authority(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={
            "reason": "authority-test",
            "chunk_policy": "semantic-v2",
            **FORGED_QDRANT_FIELDS,
        },
        headers={**auth_headers, "Idempotency-Key": "qdrant-build-authority"},
    )

    assert response.status_code == 202
    body = response.json()
    assert_authoritative_scope(body, auth_headers)
    data = body["data"]
    qdrant_payload = data["qdrant_payload"]
    assert data["reason"] == "authority-test"
    assert data["chunk_policy"] == "semantic-v2"
    assert data["knowledge_index_id"] == "ki_sales_policy_v1"
    assert data["knowledge_source_id"] == "ks_sales_policy"
    assert data["source_id"] == "ks_sales_policy"
    assert data["affected_objects"] == [{"type": "knowledge_index", "id": "ki_sales_policy_v1"}]
    assert qdrant_payload["knowledge_index_id"] == "ki_sales_policy_v1"
    assert qdrant_payload["knowledge_source_id"] == "ks_sales_policy"
    assert qdrant_payload["source_id"] == "ks_sales_policy"
    assert qdrant_payload["version"] == "kb-index-v3.2"


@pytest.mark.parametrize(
    ("collection", "error_code"),
    [
        ("other_collection", "QDRANT_COLLECTION_FORBIDDEN"),
        ("knowledge_chunks/../../other_collection", "QDRANT_COLLECTION_INVALID"),
    ],
)
def test_knowledge_index_build_rejects_unsafe_server_collection(
    client, auth_headers, collection, error_code
):
    with SessionLocal() as session:
        resource = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "knowledge_indexes",
                JsonResource.resource_key == "ki_sales_policy_v1",
                JsonResource.tenant_id == auth_headers["X-Tenant-Id"],
                JsonResource.project_id == auth_headers["X-Project-Id"],
            )
        )
        assert resource is not None
        resource.data = {**resource.data, "vector_collection": collection}
        session.commit()

    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
        json={"reason": "invalid-server-collection"},
        headers={
            **auth_headers,
            "Idempotency-Key": f"qdrant-invalid-collection-{error_code}",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == error_code


def test_knowledge_recall_rejects_forged_qdrant_payload(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={
            "query": "报价金额冲突处理 SOP",
            "qdrant_payload": FORGED_QDRANT_FIELDS["qdrant_payload"],
        },
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_knowledge_recall_ignores_cross_collection_scope(client, auth_headers):
    response = client.post(
        "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
        json={
            "query": "报价金额冲突处理 SOP",
            "scope": {
                "collection": "other_collection",
                "qdrant_collection": "../other_collection",
                "trace_id": "forged_trace",
            },
        },
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["collection"] == "knowledge_chunks"
    assert body["data"]["recall_trace_id"] == body["meta"]["trace_id"]
    assert body["data"]["filter"] == {
        "tenant_id": auth_headers["X-Tenant-Id"],
        "project_id": auth_headers["X-Project-Id"],
        "knowledge_index_id": "ki_sales_policy_v1",
    }
