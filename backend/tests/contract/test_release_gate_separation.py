from __future__ import annotations

from copy import deepcopy

from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import Project, User

SECOND_ADMIN_ID = "u_annotator_001"


def _headers(
    auth_headers: dict[str, str],
    *,
    key: str,
    token: str = "dev-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _promote_second_admin() -> str:
    with SessionLocal.begin() as session:
        user = session.get(User, SECOND_ADMIN_ID)
        project = session.get(Project, "sales_qa")
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project_data = deepcopy(project.data)
        members = []
        for member in project_data.get("members", []):
            if member.get("user_id") == SECOND_ADMIN_ID:
                member = {
                    **member,
                    "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
                }
            members.append(member)
        project_data["members"] = members
        project.data = project_data

    profile = DevAuthProfile(
        email="release-second-admin@auris.local",
        user_id=SECOND_ADMIN_ID,
        name="发布复核管理员",
        role_label="项目管理员",
        initials="复",
        roles=("annotator", "review_arbitrator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def _request_task_publish(client, auth_headers, *, suffix: str):
    version_id = f"task_version_release_separation_{suffix}"
    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": version_id,
            "task_type_id": "task_sales_quality",
            "version": f"release-separation-{suffix}",
        },
        headers=_headers(auth_headers, key=f"release-separation-version-{suffix}"),
    )
    assert created.status_code == 201, created.text
    requested = client.post(
        f"/api/v1/task-versions/{version_id}/publish",
        json={
            "reason": "验证发布职责分离",
            "requested_by": SECOND_ADMIN_ID,
            "release_gate": {
                "status": "approved",
                "requested_by": SECOND_ADMIN_ID,
            },
        },
        headers=_headers(auth_headers, key=f"release-separation-request-{suffix}"),
    )
    assert requested.status_code == 202, requested.text
    return requested


def test_task_publish_freezes_requester_and_requires_distinct_project_admin(
    client,
    auth_headers,
) -> None:
    requested = _request_task_publish(client, auth_headers, suffix="task")
    data = requested.json()["data"]
    run_id = data["run_id"]

    assert data["requested_by"] == "u_admin_001"
    assert data["release_gate"]["requested_by"] == "u_admin_001"
    assert data["release_gate"]["status"] == "awaiting_decision"
    assert data["release_gate"]["required_roles"] == ["project_admin"]
    assert data["release_gate"]["separation_of_duties"] == "different_natural_person"

    non_admin = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "非管理员不得批准"},
        headers=_headers(
            auth_headers,
            key="release-separation-non-admin",
            token="model-token",
        ),
    )
    assert non_admin.status_code == 403
    assert non_admin.json()["error"]["code"] == "FORBIDDEN"

    self_approval = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "发起人不得自批"},
        headers=_headers(auth_headers, key="release-separation-self-approval"),
    )
    assert self_approval.status_code == 409
    assert self_approval.json()["error"]["code"] == ("RELEASE_APPROVAL_SEPARATION_REQUIRED")

    second_admin_token = _promote_second_admin()
    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "不同自然人完成发布复核"},
        headers=_headers(
            auth_headers,
            key="release-separation-distinct-approval",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text
    decision = approved.json()["data"]["release_gate"]["decision"]
    assert decision["actor_id"] == SECOND_ADMIN_ID
    assert "project_admin" in decision["roles"]


def test_settings_publish_rejects_requester_self_approval(client, auth_headers) -> None:
    draft_id = "settings_draft_release_separation"
    draft = client.post(
        "/api/v1/settings/drafts",
        json={
            "settings_draft_id": draft_id,
            "setting_id": "model-chain",
            "changes": {"provider": "release_separation_provider"},
            "reason": "验证高风险配置发布职责分离",
        },
        headers=_headers(auth_headers, key="settings-release-separation-draft"),
    )
    assert draft.status_code == 201, draft.text
    requested = client.post(
        "/api/v1/settings/publish-requests",
        json={"draft_id": draft_id, "requested_by": SECOND_ADMIN_ID},
        headers=_headers(auth_headers, key="settings-release-separation-request"),
    )
    assert requested.status_code == 202, requested.text
    data = requested.json()["data"]
    assert data["requested_by"] == "u_admin_001"
    assert data["release_gate"]["requested_by"] == "u_admin_001"

    self_approval = client.post(
        f"/api/v1/runs/{data['run_id']}/decisions",
        json={"decision": "approved", "reason": "配置发起人不得自批"},
        headers=_headers(auth_headers, key="settings-release-separation-self"),
    )
    assert self_approval.status_code == 409
    assert self_approval.json()["error"]["code"] == ("RELEASE_APPROVAL_SEPARATION_REQUIRED")
