from __future__ import annotations

from app.core.database import SessionLocal
from app.models import JsonResource, Project


def test_workspace_context_options_are_authoritative_and_scope_bound(client, auth_headers) -> None:
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id="other_project",
                tenant_id="aurora_auto",
                name="Other Project",
                status="active",
                data={
                    "members": [{"user_id": "u_admin_001", "roles": ["project_admin"]}],
                    "member_user_ids": ["u_admin_001"],
                },
            )
        )
        session.add(
            JsonResource(
                collection="stores",
                resource_key="LEAK-STORE",
                tenant_id="aurora_auto",
                project_id="other_project",
                status="active",
                trace_id="trace-other-store",
                data={"store_id": "LEAK-STORE", "name": "不应泄漏"},
            )
        )

    response = client.get("/api/v1/workspace-context-options", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["scope"] == {
        "tenant_id": "aurora_auto",
        "tenant_name": "极光汽车",
        "project_id": "sales_qa",
        "project_name": "销售话术质检",
    }
    assert [item["store_id"] for item in data["stores"]] == [
        "BJ-AURORA-001",
        "SH-JA-002",
    ]
    assert "LEAK-STORE" not in response.text
    assert {item["id"] for item in data["model_versions"]} == {"asr_v2.3.1"}
    assert {item["id"] for item in data["label_versions"]} >= {
        "label_v1_8_4",
        "label_v1_9_0_rc2",
    }
    assert data["defaults"]["store_id"] == "BJ-AURORA-001"
    assert data["defaults"]["model_version"] == "asr_v2.3.1"
    assert data["defaults"]["label_version"] == "label_v1_8_4"
    assert data["as_of"].endswith("+00:00")
    assert data["trace_id"] == response.json()["meta"]["trace_id"]


def test_workspace_context_options_return_empty_collections_without_fixture_fallback(
    client,
) -> None:
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id="empty_project",
                tenant_id="aurora_auto",
                name="Empty Project",
                status="active",
                data={
                    "members": [{"user_id": "u_admin_001", "roles": ["project_admin"]}],
                    "member_user_ids": ["u_admin_001"],
                },
            )
        )

    response = client.get(
        "/api/v1/workspace-context-options",
        headers={
            "Authorization": "Bearer dev-token",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "empty_project",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["stores"] == []
    assert data["business_dates"] == []
    assert data["model_versions"] == []
    assert data["label_versions"] == []
    assert data["defaults"] == {
        "store_id": None,
        "business_date": None,
        "model_version": None,
        "label_version": None,
    }
    assert data["active_scene_binding"] is None
