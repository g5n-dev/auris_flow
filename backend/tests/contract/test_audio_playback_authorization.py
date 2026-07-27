from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.auth_models import BrowserAuthSession, OidcIdentity, UserSecurityState
from app.core.audio_playback import create_audio_playback_grant
from app.core.browser_session import create_browser_session
from app.core.database import SessionLocal
from app.main import settings
from app.models import AudioRecording, AuthSession, JsonResource, Project, Tenant, User

AUDIO_SESSION_ID = "S20250526-000128"
SECONDARY_AUDIO_SESSION_ID = "S20250526-SCOPE-SWITCH"


def _issue_playback_url(client, auth_headers, *, key: str) -> str:
    response = client.post(
        f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}/playback-grants",
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["playback_url"])


def _grant_from_url(playback_url: str) -> str:
    grant = playback_url.partition("grant=")[2]
    assert grant
    return grant


def _issue_oidc_browser_session(client):
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
                identity_id="oidc_audio_admin",
                issuer="https://identity.example.test/realms/auris",
                subject="audio-admin-subject",
                user_id="u_admin_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
            )
        )
    with SessionLocal.begin() as session:
        issued = create_browser_session(
            session,
            identity_id="oidc_audio_admin",
            ttl_seconds=3600,
        )
    client.cookies.set(settings.browser_session_cookie_name, issued.raw_token)
    restored = client.get(
        "/api/v1/auth/session",
        headers={
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
        },
    )
    assert restored.status_code == 200, restored.text
    return issued, str(restored.json()["data"]["csrf_token"])


def _issue_oidc_playback_url(client, *, key: str) -> tuple[str, str]:
    issued, csrf_token = _issue_oidc_browser_session(client)
    response = client.post(
        f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}/playback-grants",
        headers={
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "sales_qa",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": key,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["playback_url"]), csrf_token


def test_legacy_recording_requires_grant_and_accepts_matching_short_lived_grant(
    client,
    auth_headers,
):
    legacy_url = f"/api/v1/audio-sessions/{AUDIO_SESSION_ID}/recording"

    bypass = client.get(legacy_url, headers={**auth_headers, "Range": "bytes=0-15"})
    assert bypass.status_code == 403
    assert bypass.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REQUIRED"

    playback_url = _issue_playback_url(client, auth_headers, key="legacy-grant-required")
    allowed = client.get(
        legacy_url,
        params={"grant": _grant_from_url(playback_url)},
        headers={"Range": "bytes=0-15"},
    )
    assert allowed.status_code == 206
    assert allowed.headers["Content-Range"].startswith("bytes 0-15/")
    assert allowed.headers["Cache-Control"] == "private, no-store"


def test_playback_grant_expiry_and_tampering_return_403(client, auth_headers):
    expired_token, _grant = create_audio_playback_grant(
        settings,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_admin_001",
        audio_session_id=AUDIO_SESSION_ID,
        now=int(time.time()) - 901,
    )
    expired = client.get("/api/v1/audio-playback", params={"grant": expired_token})
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_INVALID"

    playback_url = _issue_playback_url(client, auth_headers, key="tampered-grant-forbidden")
    tampered = client.get(f"{playback_url}x")
    assert tampered.status_code == 403
    assert tampered.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_INVALID"


def test_playback_grant_rejects_cross_tenant_and_wrong_session_scope(client, auth_headers):
    with SessionLocal() as session:
        session.add(
            Tenant(
                tenant_id="foreign_tenant",
                tenant_code="foreign_tenant",
                name="Foreign tenant",
                status="active",
                data={},
            )
        )
        session.add(
            Project(
                project_id="foreign_audio",
                tenant_id="foreign_tenant",
                name="Foreign audio",
                status="active",
                data={
                    "member_user_ids": ["u_admin_001"],
                    "members": [{"user_id": "u_admin_001", "roles": ["project_admin"]}],
                },
            )
        )
        session.commit()

    cross_tenant_token, _grant = create_audio_playback_grant(
        settings,
        tenant_id="foreign_tenant",
        project_id="foreign_audio",
        user_id="u_admin_001",
        audio_session_id=AUDIO_SESSION_ID,
    )
    cross_tenant = client.get(
        "/api/v1/audio-playback",
        params={"grant": cross_tenant_token},
    )
    assert cross_tenant.status_code == 403
    assert cross_tenant.json()["error"]["code"] == "MEMBERSHIP_REQUIRED"

    playback_url = _issue_playback_url(client, auth_headers, key="wrong-session-grant")
    wrong_session = client.get(
        "/api/v1/audio-sessions/S20250526-OTHER/recording",
        params={"grant": _grant_from_url(playback_url)},
    )
    assert wrong_session.status_code == 403
    assert wrong_session.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_SCOPE_MISMATCH"


def test_playback_grant_rechecks_current_audio_read_role(client, auth_headers):
    playback_url = _issue_playback_url(client, auth_headers, key="playback-role-recheck")

    with SessionLocal() as session:
        user = session.get(User, "u_admin_001")
        project = session.get(Project, "sales_qa")
        assert user is not None
        assert project is not None
        user.roles = ["model_engineer"]
        project.data = {
            **project.data,
            "member_user_ids": ["u_admin_001"],
            "members": [{"user_id": "u_admin_001", "roles": ["model_engineer"]}],
        }
        session.commit()

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"


def test_playback_grant_is_revoked_with_issuing_login_session(client):
    login = client.post(
        "/api/v1/auth/dev-login",
        json={"email": "admin@auris.local", "password": "auris-demo"},
    )
    assert login.status_code == 200, login.text
    token = login.json()["data"]["access_token"]
    session_headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
    }
    playback_url = _issue_playback_url(
        client,
        session_headers,
        key="playback-session-revocation",
    )

    logout = client.post("/api/v1/auth/logout", headers=session_headers)
    assert logout.status_code == 200, logout.text

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_oidc_cookie_session_grant_streams_and_logout_revokes_playback(client):
    playback_url, csrf_token = _issue_oidc_playback_url(
        client,
        key="oidc-cookie-playback-session",
    )

    allowed = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert allowed.status_code == 206, allowed.text
    assert allowed.content.startswith(b"RIFF")

    logout = client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf_token,
        },
    )
    assert logout.status_code == 200, logout.text

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_oidc_scope_transition_keeps_range_and_head_playback_in_target_project(
    client,
):
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id="audio_scope_project",
                tenant_id="aurora_auto",
                name="Audio Scope Project",
                status="active",
                data={
                    "member_user_ids": ["u_admin_001"],
                    "members": [
                        {
                            "user_id": "u_admin_001",
                            "roles": ["project_admin"],
                        }
                    ],
                },
            )
        )
        session.add(
            JsonResource(
                collection="audio_sessions",
                resource_key=SECONDARY_AUDIO_SESSION_ID,
                tenant_id="aurora_auto",
                project_id="audio_scope_project",
                status="active",
                trace_id="trace_audio_scope_project",
                data={
                    "audio_session_id": SECONDARY_AUDIO_SESSION_ID,
                    "recording_id": "REC-SCOPE-SWITCH",
                },
            )
        )
        session.add(
            AudioRecording(
                recording_id="REC-SCOPE-SWITCH",
                tenant_id="aurora_auto",
                project_id="audio_scope_project",
                status="registered",
                trace_id="trace_audio_scope_project",
                payload={
                    "recording_id": "REC-SCOPE-SWITCH",
                    "file_name": "scope-switch.wav",
                },
            )
        )

    issued, csrf_token = _issue_oidc_browser_session(client)
    switched = client.post(
        "/api/v1/auth/session/scope-transitions",
        headers={
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": csrf_token,
        },
        json={"project_id": "audio_scope_project"},
    )
    assert switched.status_code == 200, switched.text
    switched_data = switched.json()["data"]
    assert switched_data["project_id"] == "audio_scope_project"

    grant = client.post(
        f"/api/v1/audio-sessions/{SECONDARY_AUDIO_SESSION_ID}/playback-grants",
        headers={
            "X-Tenant-Id": "aurora_auto",
            "X-Project-Id": "audio_scope_project",
            "Origin": "http://localhost:5173",
            "X-CSRF-Token": switched_data["csrf_token"],
            "Idempotency-Key": "oidc-scope-transition-audio-playback",
        },
    )
    assert grant.status_code == 201, grant.text
    playback_url = grant.json()["data"]["playback_url"]

    partial = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert partial.status_code == 206, partial.text
    assert partial.content.startswith(b"RIFF")
    assert partial.headers["Accept-Ranges"] == "bytes"
    assert partial.headers["Content-Range"].startswith("bytes 0-15/")

    metadata = client.head(playback_url, headers={"Range": "bytes=0-15"})
    assert metadata.status_code == 206, metadata.text
    assert metadata.content == b""
    assert metadata.headers["Accept-Ranges"] == "bytes"
    assert metadata.headers["Content-Range"].startswith("bytes 0-15/")

    with SessionLocal() as session:
        identity = session.get(OidcIdentity, "oidc_audio_admin")
        active_browser_session = session.scalar(
            select(BrowserAuthSession).where(
                BrowserAuthSession.user_id == issued.user_id,
                BrowserAuthSession.project_id == "audio_scope_project",
                BrowserAuthSession.revoked_at.is_(None),
            )
        )
        assert identity is not None
        assert identity.project_id == "sales_qa"
        assert active_browser_session is not None


def test_oidc_playback_grant_rechecks_user_security_state(client):
    playback_url, _csrf_token = _issue_oidc_playback_url(
        client,
        key="oidc-playback-user-disabled",
    )

    with SessionLocal.begin() as session:
        security = session.get(UserSecurityState, "u_admin_001")
        assert security is not None
        security.status = "disabled"
        security.disabled_at = datetime.now(UTC)
        security.authz_version += 1

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_oidc_playback_grant_rechecks_identity_and_session_expiry(client):
    playback_url, _csrf_token = _issue_oidc_playback_url(
        client,
        key="oidc-playback-identity-disabled",
    )

    with SessionLocal.begin() as session:
        identity = session.get(OidcIdentity, "oidc_audio_admin")
        assert identity is not None
        identity.status = "disabled"

    disabled = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"

    # A distinct fixture verifies that the signed grant cannot outlive its
    # parent browser session even while the grant's own TTL remains valid.
    client.cookies.clear()
    with SessionLocal.begin() as session:
        identity = session.get(OidcIdentity, "oidc_audio_admin")
        assert identity is not None
        identity.status = "active"
        browser = session.execute(select(BrowserAuthSession)).scalar_one()
        browser.issued_at = datetime.now(UTC) - timedelta(hours=2)
        browser.last_seen_at = browser.issued_at
        browser.expires_at = datetime.now(UTC) - timedelta(hours=1)

    expired = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_oidc_playback_grant_rechecks_current_effective_project_roles(client):
    playback_url, _csrf_token = _issue_oidc_playback_url(
        client,
        key="oidc-playback-role-downgrade",
    )

    with SessionLocal.begin() as session:
        user = session.get(User, "u_admin_001")
        project = session.get(Project, "sales_qa")
        security = session.get(UserSecurityState, "u_admin_001")
        assert user is not None
        assert project is not None
        assert security is not None
        user.roles = ["model_engineer"]
        project.data = {
            **project.data,
            "member_user_ids": ["u_admin_001"],
            "members": [{"user_id": "u_admin_001", "roles": ["model_engineer"]}],
        }
        security.authz_version += 1

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


@pytest.mark.parametrize(
    ("resource_type", "resource_id"),
    ((Tenant, "aurora_auto"), (Project, "sales_qa")),
)
def test_oidc_playback_grant_rechecks_active_tenant_and_project(
    client,
    resource_type,
    resource_id,
):
    playback_url, _csrf_token = _issue_oidc_playback_url(
        client,
        key=f"oidc-playback-disabled-{resource_id}",
    )

    with SessionLocal.begin() as session:
        resource = session.get(resource_type, resource_id)
        assert resource is not None
        resource.status = "disabled"

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"


def test_playback_parent_session_id_collision_fails_closed(client):
    playback_url, _csrf_token = _issue_oidc_playback_url(
        client,
        key="oidc-playback-session-type-collision",
    )

    now = datetime.now(UTC)
    with SessionLocal.begin() as session:
        browser = session.execute(select(BrowserAuthSession)).scalar_one()
        session.add(
            AuthSession(
                session_id=browser.browser_session_id,
                user_id=browser.user_id,
                tenant_id=browser.tenant_id,
                provider="dev_session",
                issued_at=now,
                expires_at=now + timedelta(hours=1),
                last_seen_at=now,
            )
        )

    denied = client.get(playback_url, headers={"Range": "bytes=0-15"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "AUDIO_PLAYBACK_GRANT_REVOKED"
