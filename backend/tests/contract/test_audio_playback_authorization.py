from __future__ import annotations

import time

from app.core.audio_playback import create_audio_playback_grant
from app.core.database import SessionLocal
from app.main import settings
from app.models import Project, Tenant, User

AUDIO_SESSION_ID = "S20250526-000128"


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
