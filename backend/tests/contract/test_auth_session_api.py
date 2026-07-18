from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import select

from app.core.auth import (
    DevAuthProfile,
    StaticDevAuthProvider,
    get_dev_auth_profile,
    issue_dev_auth_token,
)
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import AuthSession, Project


def _session_headers(
    token: str,
    *,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": tenant_id,
        "X-Project-Id": project_id,
    }


def test_dev_login_issues_scoped_server_session_without_context_headers(client):
    response = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    token = payload["data"]["access_token"]
    assert token.startswith("auris.v1.")
    assert token not in {"dev-token", "annotator-token", "annotator-b-token"}
    assert payload["data"]["user"] == {
        "user_id": "u_annotator_001",
        "name": "质检运营 A",
        "email": "annotator@auris.local",
        "role": "质检运营",
        "roles": ["annotator", "review_arbitrator"],
        "initials": "A",
        "tenant_id": "aurora_auto",
        "tenant_name": "极光汽车",
        "project_id": "sales_qa",
        "project_name": "销售话术质检",
    }
    assert payload["meta"]["trace_id"]

    actor = StaticDevAuthProvider(get_settings()).authenticate(token)
    assert actor.session_id is not None
    with SessionLocal() as database_session:
        auth_session = database_session.get(AuthSession, actor.session_id)
        assert auth_session is not None
        assert auth_session.user_id == "u_annotator_001"
        assert auth_session.tenant_id == "aurora_auto"
        assert auth_session.provider == "dev_session"
        assert auth_session.revoked_at is None
        assert auth_session.last_seen_at == auth_session.issued_at

    session_response = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )
    assert session_response.status_code == 200, session_response.text
    session_data = session_response.json()["data"]
    assert session_data["user_id"] == "u_annotator_001"
    assert session_data["roles"] == ["annotator", "review_arbitrator"]
    assert session_data["provider"] == "dev_session"


def test_release_approver_dev_login_uses_a_distinct_project_admin_identity(client):
    requester = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "demo.operator@auris.local", "password": "auris-demo"},
    )
    approver = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "release.approver@auris.local", "password": "auris-demo"},
    )

    assert requester.status_code == 200, requester.text
    assert approver.status_code == 200, approver.text
    requester_user = requester.json()["data"]["user"]
    approver_user = approver.json()["data"]["user"]
    assert requester_user["user_id"] == "u_admin_001"
    assert approver_user == {
        "user_id": "u_release_admin_001",
        "name": "发布复核管理员",
        "email": "release.approver@auris.local",
        "role": "项目管理员",
        "roles": ["project_admin"],
        "initials": "审",
        "tenant_id": "aurora_auto",
        "tenant_name": "极光汽车",
        "project_id": "sales_qa",
        "project_name": "销售话术质检",
    }
    assert approver_user["user_id"] != requester_user["user_id"]
    assert approver.json()["data"]["access_token"] != requester.json()["data"]["access_token"]


def test_dev_login_rejects_unknown_account_and_wrong_password(client):
    unknown = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "unknown@auris.local", "password": "auris-demo"},
    )
    wrong_password = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "admin@auris.local", "password": "incorrect"},
    )

    assert unknown.status_code == 401
    assert wrong_password.status_code == 401
    assert unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert wrong_password.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_dev_session_is_bound_to_issued_tenant_and_project(client):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]

    response = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "same_tenant_unassigned",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TOKEN_SCOPE_MISMATCH"


def test_project_admin_dev_session_can_switch_to_created_member_project(client, auth_headers):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "admin@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]
    create = client.post(
        "/api/v1/projects",
        headers={
            **auth_headers,
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "auth-session-created-project",
        },
        json={
            "project_id": "same_tenant_created",
            "name": "同租户新项目",
            "members": [{"user_id": "u_admin_001", "roles": ["project_admin", "asset_manager"]}],
            "member_user_ids": ["u_admin_001"],
        },
    )
    assert create.status_code == 201, create.text

    switched = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "same_tenant_created",
        },
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["data"]["project_id"] == "same_tenant_created"

    missing = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "same_tenant_unassigned",
        },
    )
    assert missing.status_code == 403
    assert missing.json()["error"]["code"] == "PROJECT_NOT_FOUND"


def test_dev_session_cannot_assert_role_missing_from_user_project_binding(client):
    token, _ = issue_dev_auth_token(
        DevAuthProfile(
            email="forged-role@auris.local",
            user_id="u_annotator_001",
            name="角色不匹配",
            role_label="项目管理员",
            initials="X",
            roles=("project_admin",),
        ),
        get_settings(),
    )

    response = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TOKEN_ROLE_MISMATCH"


def test_dev_login_endpoint_is_unavailable_outside_local_test_or_ci(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "allow_dev_auth", True)

    response = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "admin@auris.local", "password": "auris-demo"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DEV_LOGIN_DISABLED"

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": "Bearer dev-token"},
    )
    assert logout.status_code == 404
    assert logout.json()["error"]["code"] == "DEV_LOGOUT_DISABLED"


def test_logout_is_http_idempotent_and_revoked_token_is_rejected_by_context(client):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]

    first = client.post(
        "/api/v1/auth/logout",
        headers=_session_headers(token),
    )
    second = client.post(
        "/api/v1/auth/logout",
        headers=_session_headers(token),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["data"] == second.json()["data"]
    assert first.json()["data"]["status"] == "revoked"
    assert first.json()["data"]["session_id"].startswith("dev_")
    assert first.json()["data"]["revoked_at"].endswith("+00:00")
    assert first.json()["meta"]["trace_id"]
    assert first.json()["meta"]["request_id"]

    rejected = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "AUTH_SESSION_REVOKED"


def test_logout_revokes_only_current_session(client):
    first_login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    second_login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    first_token = first_login.json()["data"]["access_token"]
    second_token = second_login.json()["data"]["access_token"]

    revoked = client.post(
        "/api/v1/auth/logout",
        headers=_session_headers(first_token),
    )
    active = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {second_token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )

    assert revoked.status_code == 200, revoked.text
    assert active.status_code == 200, active.text
    first_session_id = revoked.json()["data"]["session_id"]
    second_actor = StaticDevAuthProvider(get_settings()).authenticate(second_token)
    assert first_session_id != second_actor.session_id
    with SessionLocal() as database_session:
        first_record = database_session.get(AuthSession, first_session_id)
        second_record = database_session.get(AuthSession, second_actor.session_id)
        assert first_record is not None and first_record.revoked_at is not None
        assert second_record is not None and second_record.revoked_at is None


def test_context_rejects_expired_or_subject_mismatched_dev_session(client, monkeypatch):
    settings = get_settings()
    profile = get_dev_auth_profile("annotator@auris.local")
    assert profile is not None
    monkeypatch.setattr(settings, "dev_auth_session_ttl_seconds", 300)
    expired_token, _ = issue_dev_auth_token(profile, settings, now=int(time.time()) - 301)

    expired = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {expired_token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"

    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]
    actor = StaticDevAuthProvider(settings).authenticate(token)
    with SessionLocal.begin() as database_session:
        auth_session = database_session.execute(
            select(AuthSession).where(AuthSession.session_id == actor.session_id)
        ).scalar_one()
        auth_session.provider = "tampered_provider"

    mismatched = client.get(
        "/api/v1/auth/session",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )
    assert mismatched.status_code == 401
    assert mismatched.json()["error"]["code"] == "AUTH_SESSION_SUBJECT_MISMATCH"


def test_logout_requires_and_validates_explicit_context_scope(client):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]

    missing_tenant = client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Project-Id": "sales_qa",
        },
    )
    missing_project = client.post(
        "/api/v1/auth/logout",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Tenant-Id": "aurora_auto",
        },
    )
    wrong_tenant = client.post(
        "/api/v1/auth/logout",
        headers=_session_headers(token, tenant_id="another_tenant"),
    )
    wrong_project = client.post(
        "/api/v1/auth/logout",
        headers=_session_headers(token, project_id="another_project"),
    )

    assert missing_tenant.status_code == 400
    assert missing_tenant.json()["error"]["code"] == "CONTEXT_MISSING_TENANT"
    assert missing_project.status_code == 400
    assert missing_project.json()["error"]["code"] == "CONTEXT_MISSING_PROJECT"
    assert wrong_tenant.status_code == 403
    assert wrong_tenant.json()["error"]["code"] == "TOKEN_SCOPE_MISMATCH"
    assert wrong_project.status_code == 403
    assert wrong_project.json()["error"]["code"] == "TOKEN_SCOPE_MISMATCH"

    valid = client.post("/api/v1/auth/logout", headers=_session_headers(token))
    assert valid.status_code == 200, valid.text


def test_dev_session_last_seen_is_persisted_at_most_once_per_interval(client):
    settings = get_settings()
    profile = get_dev_auth_profile("annotator@auris.local")
    assert profile is not None
    issued_at = int(time.time()) - 120
    token, _ = issue_dev_auth_token(profile, settings, now=issued_at)
    actor = StaticDevAuthProvider(settings).authenticate(token)
    assert actor.session_id is not None

    with SessionLocal() as database_session:
        original = database_session.get(AuthSession, actor.session_id)
        assert original is not None
        original_last_seen = original.last_seen_at

    first = client.get("/api/v1/auth/session", headers=_session_headers(token))
    assert first.status_code == 200, first.text
    with SessionLocal() as database_session:
        refreshed = database_session.get(AuthSession, actor.session_id)
        assert refreshed is not None
        refreshed_last_seen = refreshed.last_seen_at
    assert refreshed_last_seen > original_last_seen

    second = client.get("/api/v1/auth/session", headers=_session_headers(token))
    assert second.status_code == 200, second.text
    with SessionLocal() as database_session:
        throttled = database_session.get(AuthSession, actor.session_id)
        assert throttled is not None
        assert throttled.last_seen_at == refreshed_last_seen


def test_fresh_dev_session_allows_concurrent_reads_without_heartbeat_writes(client):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]
    start = Barrier(2)

    def read_session() -> int:
        start.wait()
        return client.get("/api/v1/auth/session", headers=_session_headers(token)).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _index: read_session(), range(2)))

    assert statuses == [200, 200]


def test_explicit_project_member_roles_reject_unbound_token_role(client):
    with SessionLocal.begin() as database_session:
        project = database_session.get(Project, "sales_qa")
        assert project is not None
        members = list((project.data or {}).get("members") or [])
        project.data = {
            **(project.data or {}),
            "members": [
                {
                    **member,
                    "roles": ["annotator"],
                }
                if member.get("user_id") == "u_annotator_001"
                else member
                for member in members
            ],
        }

    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    token = login.json()["data"]["access_token"]
    response = client.get("/api/v1/auth/session", headers=_session_headers(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TOKEN_PROJECT_ROLE_MISMATCH"
    assert response.json()["error"]["details"] == [{"unbound_roles": ["review_arbitrator"]}]
