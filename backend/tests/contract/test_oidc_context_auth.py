from __future__ import annotations

from types import MappingProxyType

from app.auth_models import OidcIdentity, UserSecurityState
from app.core.auth import AuthActor
from app.core.browser_session import create_browser_session
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.oidc import OIDCValidatedClaims
from app.core.oidc_flow import (
    OIDCAuthorizationCallback,
    OIDCAuthorizationRequest,
    OIDCTokenSet,
)

ISSUER = "https://identity.example.test/realms/auris"
OIDC_STATE = "oidc-state-" + ("s" * 48)
OIDC_NONCE = "oidc-nonce-" + ("n" * 48)
OIDC_VERIFIER = "v" * 64


class StubAuthorizationFlow:
    def create_authorization_request(self) -> OIDCAuthorizationRequest:
        return OIDCAuthorizationRequest(
            authorization_url=(
                f"https://identity.example.test/authorize?response_type=code&state={OIDC_STATE}"
            ),
            state=OIDC_STATE,
            nonce=OIDC_NONCE,
            code_verifier=OIDC_VERIFIER,
            code_challenge="challenge",
        )

    def parse_authorization_response(
        self,
        query: str,
        *,
        expected_state: str,
    ) -> OIDCAuthorizationCallback:
        assert f"state={OIDC_STATE}" in query
        assert expected_state == OIDC_STATE
        return OIDCAuthorizationCallback(
            code="one-time-code",
            state=OIDC_STATE,
            issuer=ISSUER,
        )

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCTokenSet:
        assert code == "one-time-code"
        assert code_verifier == OIDC_VERIFIER
        assert expected_nonce == OIDC_NONCE
        claims = OIDCValidatedClaims(
            subject="external-admin-subject",
            issuer=ISSUER,
            audiences=("auris-flow-api",),
            expires_at=2_000_000_000,
            issued_at=1_900_000_000,
            claims=MappingProxyType({"nonce": OIDC_NONCE}),
        )
        return OIDCTokenSet(
            access_token="redacted-access-token",
            id_token="redacted-id-token",
            token_type="Bearer",
            expires_in=300,
            refresh_token=None,
            scope="openid profile email",
            claims=claims,
        )


def _issue_admin_browser_session():
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
                identity_id="oidc_identity_admin",
                issuer=ISSUER,
                subject="external-admin-subject",
                user_id="u_admin_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
            )
        )
    with SessionLocal.begin() as session:
        return create_browser_session(
            session,
            identity_id="oidc_identity_admin",
            ttl_seconds=3600,
        )


def _scope_headers() -> dict[str, str]:
    return {
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Request-Id": "oidc-context-test",
    }


def test_opaque_cookie_authenticates_reads_and_requires_csrf_for_writes(client) -> None:
    issued = _issue_admin_browser_session()
    client.cookies.set("auris_session", issued.raw_token)

    restored = client.get("/api/v1/auth/session", headers=_scope_headers())
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["user_id"] == "u_admin_001"
    assert restored.json()["data"]["roles"] == ["asset_manager", "project_admin"]
    csrf_token = restored.json()["data"]["csrf_token"]

    body = {
        "project_id": "cookie-auth-project",
        "name": "Cookie Auth Project",
        "members": [{"user_id": "u_admin_001", "roles": ["project_admin"]}],
        "member_user_ids": ["u_admin_001"],
    }
    rejected = client.post(
        "/api/v1/projects",
        headers={
            **_scope_headers(),
            "Origin": "http://localhost:5173",
            "Idempotency-Key": "cookie-auth-project-create",
        },
        json=body,
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "CSRF_TOKEN_REQUIRED"

    created = client.post(
        "/api/v1/projects",
        headers={
            **_scope_headers(),
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": "cookie-auth-project-create",
        },
        json=body,
    )
    assert created.status_code == 201, created.text
    assert issued.raw_token not in created.text
    assert csrf_token not in created.text


def test_validated_oidc_bearer_is_mapped_to_provisioned_internal_identity(
    client,
    monkeypatch,
) -> None:
    _issue_admin_browser_session()
    external_actor = AuthActor(
        user_id="external-admin-subject",
        roles=(),
        provider="oidc_bearer",
        issued_at=1_700_000_000,
        expires_at=2_000_000_000,
        oidc_issuer=ISSUER,
        oidc_subject="external-admin-subject",
    )
    monkeypatch.setattr("app.core.context.resolve_actor", lambda _authorization: external_actor)

    response = client.get(
        "/api/v1/auth/session",
        headers={**_scope_headers(), "Authorization": "Bearer validated-by-stub"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["user_id"] == "u_admin_001"
    assert response.json()["data"]["roles"] == ["asset_manager", "project_admin"]


def test_unprovisioned_oidc_subject_is_default_denied_without_identity_leak(
    client,
    monkeypatch,
) -> None:
    external_actor = AuthActor(
        user_id="unprovisioned-sensitive-subject",
        roles=(),
        provider="oidc_bearer",
        oidc_issuer=ISSUER,
        oidc_subject="unprovisioned-sensitive-subject",
    )
    monkeypatch.setattr("app.core.context.resolve_actor", lambda _authorization: external_actor)

    response = client.get(
        "/api/v1/auth/session",
        headers={**_scope_headers(), "Authorization": "Bearer validated-by-stub"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "OIDC_IDENTITY_NOT_PROVISIONED"
    assert "unprovisioned-sensitive-subject" not in response.text


def test_oidc_login_callback_cookie_restore_and_logout_flow(client, monkeypatch) -> None:
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
                identity_id="oidc_identity_admin",
                issuer=ISSUER,
                subject="external-admin-subject",
                user_id="u_admin_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
            )
        )
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(settings, "oidc_session_ttl_seconds", 3600)
    monkeypatch.setattr(settings, "oidc_authorization_state_ttl_seconds", 300)
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: StubAuthorizationFlow(),
    )

    started = client.get(
        "/api/v1/auth/oidc/login?return_path=/insights",
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"].startswith("https://identity.example.test/authorize")
    assert started.headers["cache-control"] == "no-store"

    callback = client.get(
        f"/api/v1/auth/oidc/callback?code=one-time-code&state={OIDC_STATE}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/insights"
    assert "HttpOnly" in callback.headers["set-cookie"]
    assert "redacted-access-token" not in callback.text
    assert "redacted-id-token" not in callback.text

    restored = client.get("/api/v1/auth/session")
    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    assert data["user_id"] == "u_admin_001"
    assert data["provider"] == "oidc_session"
    assert len(data["csrf_token"]) >= 43
    assert restored.headers["cache-control"] == "no-store"

    logged_out = client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": data["csrf_token"],
        },
    )
    assert logged_out.status_code == 200, logged_out.text
    assert logged_out.json()["data"]["status"] == "revoked"
    assert "Max-Age=0" in logged_out.headers["set-cookie"]


def test_oidc_login_rejects_open_redirect_return_path(client, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: StubAuthorizationFlow(),
    )

    response = client.get(
        "/api/v1/auth/oidc/login?return_path=https://attacker.example/steal",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OIDC_RETURN_PATH_INVALID"


def test_local_dev_cookie_can_restore_without_persisted_browser_metadata(client) -> None:
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "annotator@auris.local", "password": "auris-demo"},
    )
    assert login.status_code == 200, login.text
    assert "HttpOnly" in login.headers["set-cookie"]

    restored = client.get("/api/v1/auth/session")

    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["user_id"] == "u_annotator_001"


def test_session_without_credentials_returns_401_before_scope_validation(client) -> None:
    response = client.get("/api/v1/auth/session")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
