from __future__ import annotations

from sqlalchemy import select

from app.auth_models import BrowserAuthSession, OidcIdentity, UserSecurityState
from app.core.browser_session import create_browser_session
from app.core.database import SessionLocal
from app.models import AuditLog, Project

ISSUER = "https://identity.example.test/realms/auris"


def _add_project(
    project_id: str,
    *,
    name: str,
    roles: list[str],
    user_id: str = "u_admin_001",
    status: str = "active",
) -> None:
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id=project_id,
                tenant_id="aurora_auto",
                name=name,
                status=status,
                data={
                    "members": [{"user_id": user_id, "roles": roles}],
                    "member_user_ids": [user_id],
                },
            )
        )


def _issue_browser_session():
    with SessionLocal.begin() as session:
        session.add(
            UserSecurityState(
                user_id="u_admin_001",
                status="active",
                authz_version=1,
            )
        )
        session.add(
            OidcIdentity.create(
                identity_id="oidc_scope_admin",
                issuer=ISSUER,
                subject="scope-admin",
                user_id="u_admin_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
            )
        )
    with SessionLocal.begin() as session:
        return create_browser_session(
            session,
            identity_id="oidc_scope_admin",
            ttl_seconds=3600,
            oidc_session_id="provider-sid",
        )


def _restore(client, raw_token: str) -> dict[str, object]:
    client.cookies.set("auris_session", raw_token)
    response = client.get("/api/v1/auth/session")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_session_lists_only_active_effective_project_memberships(client) -> None:
    _add_project("project_beta", name="项目 B", roles=["project_admin"])
    _add_project("project_empty_roles", name="无有效角色", roles=[])
    _add_project("project_disabled", name="停用项目", roles=["project_admin"], status="disabled")
    issued = _issue_browser_session()

    data = _restore(client, issued.raw_token)

    memberships = data["project_memberships"]
    assert [(item["project_id"], item["project_name"], item["roles"]) for item in memberships] == [
        ("project_beta", "项目 B", ["project_admin"]),
        ("sales_qa", "销售话术质检", ["asset_manager", "project_admin"]),
    ]


def test_scope_transition_rotates_opaque_session_without_extending_expiry(client) -> None:
    _add_project("project_beta", name="项目 B", roles=["project_admin"])
    issued = _issue_browser_session()
    restored = _restore(client, issued.raw_token)

    switched = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": str(restored["csrf_token"]),
        },
        json={"project_id": "project_beta"},
    )

    assert switched.status_code == 200, switched.text
    data = switched.json()["data"]
    assert data["previous_project_id"] == "sales_qa"
    assert data["current_project_id"] == "project_beta"
    assert data["project_id"] == "project_beta"
    assert data["project_name"] == "项目 B"
    assert data["roles"] == ["project_admin"]
    assert data["csrf_token"] != restored["csrf_token"]
    assert "HttpOnly" in switched.headers["set-cookie"]

    with SessionLocal() as session:
        old = session.get(BrowserAuthSession, issued.session_id)
        active = session.scalar(
            select(BrowserAuthSession).where(
                BrowserAuthSession.user_id == "u_admin_001",
                BrowserAuthSession.project_id == "project_beta",
                BrowserAuthSession.revoked_at.is_(None),
            )
        )
        audit = session.scalar(
            select(AuditLog).where(AuditLog.action == "auth.session.scope_transition")
        )
        assert old is not None and old.revoked_at is not None
        assert active is not None
        assert active.browser_session_id != old.browser_session_id
        assert active.issued_at == old.issued_at
        assert active.expires_at == old.expires_at
        assert active.oidc_session_id_sha256 == old.oidc_session_id_sha256
        assert audit is not None
        assert audit.before_json == {"project_id": "sales_qa"}
        assert audit.after_json == {"project_id": "project_beta"}


def test_scope_transition_rejects_replay_and_does_not_reveal_target_state(client) -> None:
    _add_project("project_beta", name="项目 B", roles=["project_admin"])
    _add_project(
        "project_not_member",
        name="不应泄漏",
        roles=["project_admin"],
        user_id="u_annotator_001",
    )
    issued = _issue_browser_session()
    restored = _restore(client, issued.raw_token)
    headers = {
        "Origin": "http://localhost:5173",
        "X-CSRF-Token": str(restored["csrf_token"]),
    }

    missing = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers=headers,
        json={"project_id": "does_not_exist"},
    )
    foreign = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers=headers,
        json={"project_id": "project_not_member"},
    )
    assert missing.status_code == 404
    assert foreign.status_code == 404
    assert missing.json()["error"]["code"] == "AUTH_SCOPE_REJECTED"
    assert foreign.json()["error"]["code"] == "AUTH_SCOPE_REJECTED"
    assert "不应泄漏" not in foreign.text

    switched = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers=headers,
        json={"project_id": "project_beta"},
    )
    assert switched.status_code == 200, switched.text

    replay_client = type(client)(client.app)
    replay_client.cookies.set("auris_session", issued.raw_token)
    replay = replay_client.post(
        "/api/v1/auth/session/scope-transitions",
        headers=headers,
        json={"project_id": "project_beta"},
    )
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "AUTH_SESSION_REVOKED"


def test_scope_transition_enforces_origin_csrf_and_role_semantics(client) -> None:
    _add_project("project_no_effective_role", name="无权限项目", roles=["annotator"])
    issued = _issue_browser_session()
    restored = _restore(client, issued.raw_token)

    missing_origin = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={"X-CSRF-Token": str(restored["csrf_token"])},
        json={"project_id": "project_no_effective_role"},
    )
    invalid_csrf = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": "invalid",
        },
        json={"project_id": "project_no_effective_role"},
    )
    rejected_role = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": str(restored["csrf_token"]),
        },
        json={"project_id": "project_no_effective_role"},
    )
    injected_tenant = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": str(restored["csrf_token"]),
        },
        json={"project_id": "sales_qa", "tenant_id": "another_tenant"},
    )

    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["code"] == "CSRF_ORIGIN_REQUIRED"
    assert invalid_csrf.status_code == 403
    assert invalid_csrf.json()["error"]["code"] == "CSRF_TOKEN_INVALID"
    assert rejected_role.status_code == 403
    assert rejected_role.json()["error"]["code"] == "AUTHORIZATION_REJECTED"
    assert injected_tenant.status_code == 422


def test_same_project_transition_is_successful_without_rotation(client) -> None:
    issued = _issue_browser_session()
    restored = _restore(client, issued.raw_token)

    response = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": str(restored["csrf_token"]),
        },
        json={"project_id": "sales_qa"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["current_project_id"] == "sales_qa"
    assert response.json()["data"]["csrf_token"] == restored["csrf_token"]
    assert "set-cookie" not in response.headers
    with SessionLocal() as session:
        persisted = session.get(BrowserAuthSession, issued.session_id)
        assert persisted is not None and persisted.revoked_at is None
