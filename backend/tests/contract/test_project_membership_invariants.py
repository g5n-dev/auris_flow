from __future__ import annotations

from app.core.database import SessionLocal
from app.models import Project


def _duplicate_members() -> list[dict[str, object]]:
    return [
        {"user_id": "u_admin_001", "roles": ["project_admin"]},
        {"id": "u_admin_001", "roles": ["asset_manager"]},
    ]


def test_project_create_rejects_duplicate_member_identities(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/projects",
        headers={**auth_headers, "Idempotency-Key": "duplicate-project-member-create"},
        json={
            "project_id": "duplicate-member-project",
            "name": "Duplicate Member Project",
            "members": _duplicate_members(),
            "member_user_ids": ["u_admin_001"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROJECT_MEMBER_DUPLICATE"
    with SessionLocal() as session:
        assert session.get(Project, "duplicate-member-project") is None


def test_project_patch_rejects_duplicate_member_identities(client, auth_headers) -> None:
    with SessionLocal() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        original_members = project.data["members"]

    response = client.patch(
        "/api/v1/projects/sales_qa",
        headers={**auth_headers, "Idempotency-Key": "duplicate-project-member-patch"},
        json={"members": _duplicate_members()},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROJECT_MEMBER_DUPLICATE"
    with SessionLocal() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        assert project.data["members"] == original_members


def test_project_write_rejects_conflicting_user_id_aliases(client, auth_headers) -> None:
    response = client.post(
        "/api/v1/projects",
        headers={**auth_headers, "Idempotency-Key": "conflicting-project-member-create"},
        json={
            "project_id": "conflicting-member-project",
            "name": "Conflicting Member Project",
            "members": [
                {
                    "user_id": "u_admin_001",
                    "id": "u_annotator_001",
                    "roles": ["project_admin"],
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROJECT_MEMBER_IDENTITY_CONFLICT"
