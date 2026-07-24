from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth_models import (
    BrowserAuthSession,
    OidcAuthorizationState,
    OidcIdentity,
    UserSecurityState,
)
from app.core.auth import AuthActor
from app.core.browser_session import create_browser_session
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.oidc import OIDCValidatedClaims
from app.core.oidc_flow import (
    OIDCAuthorizationCallback,
    OIDCAuthorizationRequest,
    OIDCTokenExchangeError,
    OIDCTokenSet,
)
from app.main import app
from app.models import Project

ISSUER = "https://identity.example.test/realms/auris"
OIDC_STATE = "oidc-state-" + ("s" * 48)
OIDC_NONCE = "oidc-nonce-" + ("n" * 48)
OIDC_VERIFIER = "v" * 64
LOCAL_TRANSACTION_COOKIE = "auris_oidc_transaction"
PRODUCTION_TRANSACTION_COOKIE = "__Host-auris_oidc_transaction"


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
            claims=MappingProxyType(
                {
                    "nonce": OIDC_NONCE,
                    "sid": "provider-browser-session-sensitive",
                }
            ),
        )
        return OIDCTokenSet(
            access_token="redacted-access-token",
            id_token="redacted-id-token",
            token_type="Bearer",
            expires_in=300,
            refresh_token="redacted-refresh-token",
            scope="openid profile email",
            claims=claims,
        )


class FailingTokenExchangeFlow(StubAuthorizationFlow):
    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCTokenSet:
        raise OIDCTokenExchangeError


class UnexpectedTokenExchangeFlow(StubAuthorizationFlow):
    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCTokenSet:
        raise RuntimeError("unexpected-provider-detail-must-not-leak")


def _issue_admin_browser_session(*, now: datetime | None = None):
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
            now=now,
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


def test_bearer_session_restore_infers_provisioned_scope_and_ignores_an_invalid_cookie(
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
    client.cookies.set("auris_session", "invalid-cookie-with-enough-opaque-token-length")

    response = client.get(
        "/api/v1/auth/session",
        headers={"Authorization": "Bearer validated-by-stub"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["user_id"] == "u_admin_001"
    assert response.json()["data"]["tenant_id"] == "aurora_auto"
    assert response.json()["data"]["project_id"] == "sales_qa"


def test_invalid_bearer_never_falls_back_to_a_valid_cookie_or_bypasses_csrf(
    client,
    monkeypatch,
) -> None:
    issued = _issue_admin_browser_session()
    client.cookies.set("auris_session", issued.raw_token)

    def reject_bearer(_authorization: str | None) -> AuthActor:
        raise ApiError("UNAUTHORIZED", "无效或过期 token", 401)

    monkeypatch.setattr("app.core.context.resolve_actor", reject_bearer)
    response = client.post(
        "/api/v1/projects",
        headers={
            **_scope_headers(),
            "Authorization": "Bearer invalid",
            "Idempotency-Key": "invalid-bearer-must-not-use-cookie",
            "Origin": "http://localhost:5173",
        },
        json={"project_id": "must-not-exist", "name": "Must Not Exist"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    with SessionLocal() as session:
        assert session.get(Project, "must-not-exist") is None


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
    monkeypatch.setattr(settings, "allow_dev_auth", False)
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
    transaction_secret = client.cookies.get(LOCAL_TRANSACTION_COOKIE)
    assert transaction_secret is not None
    transaction_cookie_header = started.headers["set-cookie"]
    assert f"{LOCAL_TRANSACTION_COOKIE}=" in transaction_cookie_header
    assert "HttpOnly" in transaction_cookie_header
    assert "SameSite=lax" in transaction_cookie_header
    assert "Domain=" not in transaction_cookie_header

    with SessionLocal() as session:
        state_record = session.execute(select(OidcAuthorizationState)).scalar_one()
        assert state_record.transaction_sha256 != transaction_secret
        assert transaction_secret not in repr(state_record.__dict__)

    callback = client.get(
        f"/api/v1/auth/oidc/callback?code=one-time-code&state={OIDC_STATE}&iss={ISSUER}",
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "/insights"
    assert "HttpOnly" in callback.headers["set-cookie"]
    assert f'{LOCAL_TRANSACTION_COOKIE}=""' in callback.headers["set-cookie"]
    assert "Max-Age=0" in callback.headers["set-cookie"]
    assert "redacted-access-token" not in callback.text
    assert "redacted-id-token" not in callback.text
    assert "redacted-refresh-token" not in callback.text

    with SessionLocal() as session:
        persisted = session.execute(select(BrowserAuthSession)).scalar_one()
        persisted_values = repr(persisted.__dict__)
        persisted_columns = set(BrowserAuthSession.__table__.columns.keys())
    assert "redacted-access-token" not in persisted_values
    assert "redacted-id-token" not in persisted_values
    assert "redacted-refresh-token" not in persisted_values
    assert "provider-browser-session-sensitive" not in persisted_values
    assert persisted.oidc_session_id_sha256 is not None
    assert {"access_token", "id_token", "refresh_token"}.isdisjoint(persisted_columns)
    assert {"oidc_session_id", "sid"}.isdisjoint(persisted_columns)

    restored = client.get("/api/v1/auth/session")
    assert restored.status_code == 200, restored.text
    data = restored.json()["data"]
    assert data["user_id"] == "u_admin_001"
    assert data["provider"] == "oidc_session"
    assert len(data["csrf_token"]) >= 43
    assert restored.headers["cache-control"] == "no-store"

    playback_grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": data["csrf_token"],
            "Idempotency-Key": "oidc-code-pkce-playback",
        },
    )
    assert playback_grant.status_code == 201, playback_grant.text
    playback_url = playback_grant.json()["data"]["playback_url"]
    streamed = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert streamed.status_code == 206, streamed.text
    assert streamed.content.startswith(b"RIFF")

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

    revoked_stream = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert revoked_stream.status_code == 403
    assert revoked_stream.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_oidc_callback_rejects_state_from_another_browser_without_consuming_it(
    client,
    monkeypatch,
) -> None:
    _issue_admin_browser_session()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: StubAuthorizationFlow(),
    )

    started = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert started.status_code == 303
    initiating_secret = client.cookies.get(LOCAL_TRANSACTION_COOKIE)
    assert initiating_secret is not None
    client.cookies.delete(LOCAL_TRANSACTION_COOKIE)

    callback_url = f"/api/v1/auth/oidc/callback?code=one-time-code&state={OIDC_STATE}&iss={ISSUER}"
    rejected = client.get(callback_url, follow_redirects=False)
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "OIDC_STATE_INVALID"
    assert f'{LOCAL_TRANSACTION_COOKIE}=""' in rejected.headers["set-cookie"]
    assert "Max-Age=0" in rejected.headers["set-cookie"]
    with SessionLocal() as session:
        assert session.execute(select(OidcAuthorizationState)).scalar_one().consumed_at is None

    client.cookies.set(LOCAL_TRANSACTION_COOKIE, initiating_secret)
    accepted = client.get(callback_url, follow_redirects=False)
    assert accepted.status_code == 303, accepted.text


def test_oidc_callback_clears_transaction_cookie_when_token_exchange_fails(
    client,
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: FailingTokenExchangeFlow(),
    )
    started = client.get("/api/v1/auth/oidc/login", follow_redirects=False)
    assert started.status_code == 303

    rejected = client.get(
        f"/api/v1/auth/oidc/callback?code=one-time-code&state={OIDC_STATE}&iss={ISSUER}",
        follow_redirects=False,
    )

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "OIDC_TOKEN_EXCHANGE_FAILED"
    assert f'{LOCAL_TRANSACTION_COOKIE}=""' in rejected.headers["set-cookie"]
    assert "Max-Age=0" in rejected.headers["set-cookie"]


def test_oidc_callback_clears_transaction_cookie_before_state_validation(
    client,
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    client.cookies.set(LOCAL_TRANSACTION_COOKIE, "transaction-" + ("z" * 48))

    rejected = client.get("/api/v1/auth/oidc/callback", follow_redirects=False)

    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "OIDC_STATE_INVALID"
    assert f'{LOCAL_TRANSACTION_COOKIE}=""' in rejected.headers["set-cookie"]
    assert "Max-Age=0" in rejected.headers["set-cookie"]


def test_oidc_callback_clears_transaction_cookie_on_an_unexpected_server_error(
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: UnexpectedTokenExchangeFlow(),
    )

    with TestClient(app, raise_server_exceptions=False) as error_client:
        started = error_client.get("/api/v1/auth/oidc/login", follow_redirects=False)
        assert started.status_code == 303
        rejected = error_client.get(
            f"/api/v1/auth/oidc/callback?code=one-time-code&state={OIDC_STATE}&iss={ISSUER}",
            follow_redirects=False,
        )

    assert rejected.status_code == 500
    assert rejected.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "unexpected-provider-detail-must-not-leak" not in rejected.text
    assert f'{LOCAL_TRANSACTION_COOKIE}=""' in rejected.headers["set-cookie"]
    assert "Max-Age=0" in rejected.headers["set-cookie"]


def test_production_oidc_transaction_cookie_is_secure_host_only_and_short_lived(
    client,
    monkeypatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(settings, "oidc_authorization_state_ttl_seconds", 300)
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: StubAuthorizationFlow(),
    )

    started = client.get("/api/v1/auth/oidc/login", follow_redirects=False)

    assert started.status_code == 303
    cookie = started.headers["set-cookie"]
    assert f"{PRODUCTION_TRANSACTION_COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=300" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie


def test_cross_site_or_concurrent_session_restore_does_not_invalidate_existing_csrf(
    client,
) -> None:
    issued = _issue_admin_browser_session()
    client.cookies.set("auris_session", issued.raw_token)

    first = client.get("/api/v1/auth/session", headers=_scope_headers())
    navigated = client.get(
        "/api/v1/auth/session",
        headers={
            **_scope_headers(),
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-Mode": "navigate",
        },
    )

    assert first.status_code == 200, first.text
    assert navigated.status_code == 200, navigated.text
    first_csrf = first.json()["data"]["csrf_token"]
    assert navigated.json()["data"]["csrf_token"] == first_csrf

    logged_out = client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": first_csrf,
        },
    )
    assert logged_out.status_code == 200, logged_out.text


def test_expired_browser_session_requires_a_new_code_pkce_login(client, monkeypatch) -> None:
    expired = _issue_admin_browser_session(now=datetime.now(UTC) - timedelta(hours=2))
    client.cookies.set("auris_session", expired.raw_token)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_authorization_flow",
        lambda: StubAuthorizationFlow(),
    )

    restored = client.get("/api/v1/auth/session", headers=_scope_headers())
    assert restored.status_code == 401
    assert restored.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"

    restarted = client.get(
        "/api/v1/auth/oidc/login?return_path=/insights",
        follow_redirects=False,
    )
    assert restarted.status_code == 303
    assert restarted.headers["location"].startswith("https://identity.example.test/authorize")


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
