from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth_models import (
    BrowserAuthSession,
    OidcIdentity,
    OidcLogoutTokenReplay,
    UserSecurityState,
)
from app.core.browser_session import create_browser_session
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.oidc import (
    OIDCBackChannelLogoutClaims,
    OIDCProviderUnavailableError,
    OIDCTokenValidationError,
)

ISSUER = "https://identity.example.test/realms/auris"
OTHER_ISSUER = "https://identity.example.test/realms/other"


class StubLogoutTokenValidator:
    def __init__(self, claims: OIDCBackChannelLogoutClaims) -> None:
        self.claims = claims
        self.seen_tokens: list[str] = []

    def validate(self, token: str) -> OIDCBackChannelLogoutClaims:
        self.seen_tokens.append(token)
        return self.claims


class RejectingLogoutTokenValidator:
    def validate(self, token: str) -> OIDCBackChannelLogoutClaims:
        assert token
        raise OIDCTokenValidationError


class UnavailableLogoutTokenValidator:
    def validate(self, token: str) -> OIDCBackChannelLogoutClaims:
        assert token
        raise OIDCProviderUnavailableError


def _claims(
    *,
    token_id: str,
    subject: str | None = "external-admin-subject",
    session_id: str | None = None,
    issuer: str = ISSUER,
) -> OIDCBackChannelLogoutClaims:
    now = int(datetime.now(UTC).timestamp())
    return OIDCBackChannelLogoutClaims(
        issuer=issuer,
        audiences=("auris-flow-web",),
        issued_at=now - 5,
        expires_at=now + 120,
        token_id=token_id,
        subject=subject,
        session_id=session_id,
    )


def _provision_identity(
    *,
    identity_id: str,
    issuer: str,
    subject: str,
) -> None:
    with SessionLocal.begin() as session:
        if session.get(UserSecurityState, "u_admin_001") is None:
            session.add(
                UserSecurityState(
                    user_id="u_admin_001",
                    status="active",
                    authz_version=1,
                )
            )
        session.add(
            OidcIdentity.create(
                identity_id=identity_id,
                issuer=issuer,
                subject=subject,
                user_id="u_admin_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
            )
        )


def _issue(
    *,
    identity_id: str,
    provider_session_id: str | None,
) -> tuple[str, str]:
    with SessionLocal.begin() as session:
        issued = create_browser_session(
            session,
            identity_id=identity_id,
            ttl_seconds=3600,
            oidc_session_id=provider_session_id,
        )
        return issued.session_id, issued.raw_token


@pytest.fixture
def oidc_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_provider", "oidc")
    monkeypatch.setattr(settings, "allow_dev_auth", False)
    return settings


def test_backchannel_logout_by_sid_revokes_only_exact_provider_session_without_scope_leak(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    _provision_identity(
        identity_id="oidc_identity_admin",
        issuer=ISSUER,
        subject="external-admin-subject",
    )
    target_id, target_token = _issue(
        identity_id="oidc_identity_admin",
        provider_session_id="provider-session-target",
    )
    other_id, other_token = _issue(
        identity_id="oidc_identity_admin",
        provider_session_id="provider-session-other",
    )
    validator = StubLogoutTokenValidator(
        _claims(
            token_id="logout-event-by-sid",
            session_id="provider-session-target",
        )
    )
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: validator,
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={
            "logout_token": "signed-logout-token",
            "standard-extension-field": "ignored",
        },
    )

    assert response.status_code == 200, response.text
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert "aurora_auto" not in response.text
    assert "sales_qa" not in response.text
    assert target_id not in response.text
    assert validator.seen_tokens == ["signed-logout-token"]
    with SessionLocal() as session:
        assert session.get(BrowserAuthSession, target_id).revoked_at is not None
        assert session.get(BrowserAuthSession, other_id).revoked_at is None
        replay = session.execute(select(OidcLogoutTokenReplay)).scalar_one()
        persisted = repr(replay.__dict__)
        assert "logout-event-by-sid" not in persisted
        assert "provider-session-target" not in persisted
        columns = set(BrowserAuthSession.__table__.columns.keys())
        assert "oidc_session_id_sha256" in columns
        assert {"oidc_session_id", "sid"}.isdisjoint(columns)

    target_check = client.get(
        "/api/v1/auth/session",
        cookies={"auris_session": target_token},
    )
    other_check = client.get(
        "/api/v1/auth/session",
        cookies={"auris_session": other_token},
    )
    assert target_check.status_code == 401
    assert target_check.json()["error"]["code"] == "AUTH_SESSION_REVOKED"
    assert other_check.status_code == 200


def test_backchannel_logout_by_subject_revokes_all_sessions_for_exact_issuer_only(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    _provision_identity(
        identity_id="oidc_identity_admin",
        issuer=ISSUER,
        subject="shared-subject",
    )
    _provision_identity(
        identity_id="oidc_identity_other_issuer",
        issuer=OTHER_ISSUER,
        subject="shared-subject",
    )
    first_id, _ = _issue(
        identity_id="oidc_identity_admin",
        provider_session_id="provider-session-a",
    )
    second_id, _ = _issue(
        identity_id="oidc_identity_admin",
        provider_session_id="provider-session-b",
    )
    other_issuer_id, _ = _issue(
        identity_id="oidc_identity_other_issuer",
        provider_session_id="provider-session-c",
    )
    validator = StubLogoutTokenValidator(
        _claims(
            token_id="logout-event-by-subject",
            subject="shared-subject",
            session_id=None,
        )
    )
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: validator,
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "signed-subject-logout-token"},
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        assert session.get(BrowserAuthSession, first_id).revoked_at is not None
        assert session.get(BrowserAuthSession, second_id).revoked_at is not None
        assert session.get(BrowserAuthSession, other_issuer_id).revoked_at is None


def test_backchannel_logout_with_both_selectors_requires_both_to_match(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    _provision_identity(
        identity_id="oidc_identity_admin",
        issuer=ISSUER,
        subject="external-admin-subject",
    )
    session_id, _ = _issue(
        identity_id="oidc_identity_admin",
        provider_session_id="provider-session-target",
    )
    validator = StubLogoutTokenValidator(
        _claims(
            token_id="logout-event-mismatched-subject",
            subject="different-subject",
            session_id="provider-session-target",
        )
    )
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: validator,
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "signed-mismatched-selector-token"},
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        assert session.get(BrowserAuthSession, session_id).revoked_at is None


def test_backchannel_logout_persists_jti_replay_guard_and_rejects_duplicate(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    validator = StubLogoutTokenValidator(
        _claims(
            token_id="duplicate-logout-event",
            subject="late-provisioned-subject",
        )
    )
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: validator,
    )

    first = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "signed-duplicate-token"},
    )
    _provision_identity(
        identity_id="oidc_identity_late",
        issuer=ISSUER,
        subject="late-provisioned-subject",
    )
    late_session_id, _ = _issue(
        identity_id="oidc_identity_late",
        provider_session_id="late-provider-session",
    )
    duplicate = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "signed-duplicate-token"},
    )

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "OIDC_LOGOUT_TOKEN_INVALID"
    assert "duplicate-logout-event" not in duplicate.text
    assert "signed-duplicate-token" not in duplicate.text
    with SessionLocal() as session:
        assert len(session.execute(select(OidcLogoutTokenReplay)).scalars().all()) == 1
        assert session.get(BrowserAuthSession, late_session_id).revoked_at is None


def test_backchannel_logout_opportunistically_cleans_a_bounded_expired_replay_batch(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        session.add_all(
            [
                OidcLogoutTokenReplay(
                    logout_event_sha256=f"{index:064x}",
                    issuer_sha256=f"{index + 1:064x}",
                    jti_sha256=f"{index + 2:064x}",
                    issued_at=now - timedelta(hours=2),
                    expires_at=now - timedelta(hours=1),
                )
                for index in range(300)
            ]
        )
    validator = StubLogoutTokenValidator(
        _claims(
            token_id="fresh-logout-event-after-cleanup",
            subject="unprovisioned-subject",
        )
    )
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: validator,
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "signed-fresh-token"},
    )

    assert response.status_code == 200, response.text
    with SessionLocal() as session:
        remaining = session.execute(select(OidcLogoutTokenReplay)).scalars().all()
        # Cleanup is opportunistic and bounded so a request can never turn into
        # an unbounded delete, while repeated valid deliveries converge to one row.
        assert 1 < len(remaining) < 301
        assert any(
            replay.logout_event_sha256
            not in {f"{index:064x}" for index in range(300)}
            for replay in remaining
        )


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Content-Type": "application/json"}, '{"logout_token":"token"}'),
        (
            {"Content-Type": "application/x-www-form-urlencoded"},
            "logout_token=one&logout_token=two",
        ),
        ({"Content-Type": "application/x-www-form-urlencoded"}, "unknown=value"),
        (
            {"Content-Type": "application/x-www-form-urlencoded"},
            "logout_token=" + ("x" * (16 * 1024 + 1)),
        ),
    ],
)
def test_backchannel_logout_rejects_malformed_or_oversized_form_without_echo(
    client,
    monkeypatch,
    oidc_settings,
    headers: dict[str, str],
    body: str,
) -> None:
    del oidc_settings
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: RejectingLogoutTokenValidator(),
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        headers=headers,
        content=body,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OIDC_LOGOUT_TOKEN_INVALID"
    assert body not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_backchannel_logout_rejects_invalid_signed_token_without_echo(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: RejectingLogoutTokenValidator(),
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "sensitive-invalid-logout-token"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OIDC_LOGOUT_TOKEN_INVALID"
    assert "sensitive-invalid-logout-token" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_backchannel_logout_maps_provider_failure_to_standard_non_leaking_400(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: UnavailableLogoutTokenValidator(),
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "sensitive-token-needing-jwks"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OIDC_LOGOUT_TOKEN_INVALID"
    assert "OIDC_PROVIDER_UNAVAILABLE" not in response.text
    assert "sensitive-token-needing-jwks" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_backchannel_logout_maps_database_failure_to_standard_non_leaking_400(
    client,
    monkeypatch,
    oidc_settings,
) -> None:
    del oidc_settings
    monkeypatch.setattr(
        "app.api.routers.auth.get_oidc_backchannel_logout_validator",
        lambda: StubLogoutTokenValidator(
            _claims(token_id="database-failure-event")
        ),
    )

    def fail_processing(*_args, **_kwargs) -> None:
        raise SQLAlchemyError("database failure with sensitive-token-marker")

    monkeypatch.setattr(
        "app.api.routers.auth.process_backchannel_logout",
        fail_processing,
    )

    response = client.post(
        "/api/v1/auth/oidc/back-channel-logout",
        data={"logout_token": "sensitive-token-during-database-failure"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OIDC_LOGOUT_TOKEN_INVALID"
    assert "database failure" not in response.text
    assert "sensitive-token-during-database-failure" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_backchannel_logout_route_is_public_and_documented_as_form_post(client) -> None:
    runtime = client.get("/openapi.json").json()
    operation = runtime["paths"]["/api/v1/auth/oidc/back-channel-logout"]["post"]

    assert operation["security"] == []
    request_body = operation["requestBody"]["content"]["application/x-www-form-urlencoded"]
    assert request_body["schema"]["required"] == ["logout_token"]
    assert request_body["schema"]["properties"]["logout_token"]["writeOnly"] is True
    assert set(operation["responses"]) >= {"200", "400"}
    assert "503" not in operation["responses"]
