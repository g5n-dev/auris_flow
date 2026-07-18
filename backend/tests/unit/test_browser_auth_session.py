from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auth_models import BrowserAuthSession, OidcIdentity, UserSecurityState
from app.core.auth import AuthActor
from app.core.browser_session import (
    authenticate_browser_session,
    browser_session_csrf_token,
    create_browser_session,
    resolve_oidc_bearer_actor,
    revoke_browser_session,
)
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import Project, Tenant

ISSUER = "https://identity.example.test/realms/auris"


def _provision_identity() -> OidcIdentity:
    with SessionLocal.begin() as session:
        security = session.get(UserSecurityState, "u_annotator_001")
        if security is None:
            session.add(
                UserSecurityState(
                    user_id="u_annotator_001",
                    status="active",
                    authz_version=1,
                )
            )
        identity = session.get(OidcIdentity, "oidc_identity_annotator")
        if identity is None:
            identity = OidcIdentity.create(
                identity_id="oidc_identity_annotator",
                issuer=ISSUER,
                subject="external-subject-001",
                user_id="u_annotator_001",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="active",
            )
            session.add(identity)
    return identity


def _issue(*, now: datetime | None = None):
    _provision_identity()
    with SessionLocal.begin() as session:
        return create_browser_session(
            session,
            identity_id="oidc_identity_annotator",
            ttl_seconds=3600,
            now=now,
        )


def _authenticate(
    raw_token: str,
    *,
    method: str = "GET",
    csrf_token: str | None = None,
    origin: str | None = None,
    tenant_id: str = "aurora_auto",
    project_id: str = "sales_qa",
):
    with SessionLocal.begin() as session:
        return authenticate_browser_session(
            session,
            raw_token=raw_token,
            tenant_id=tenant_id,
            project_id=project_id,
            method=method,
            csrf_token=csrf_token,
            origin=origin,
            allowed_origins=("https://flow.example.test",),
        )


def _add_authorized_secondary_project() -> None:
    with SessionLocal.begin() as session:
        session.add(
            Project(
                project_id="sales_qa_secondary",
                tenant_id="aurora_auto",
                name="同租户第二项目",
                status="active",
                data={
                    "members": [
                        {
                            "user_id": "u_annotator_001",
                            "roles": ["annotator"],
                        }
                    ]
                },
            )
        )


def _add_cross_tenant_project() -> None:
    with SessionLocal.begin() as session:
        session.add(
            Tenant(
                tenant_id="nebula_auto",
                tenant_code="NEBULA",
                name="星云汽车",
                status="active",
                data={},
            )
        )
        session.add(
            Project(
                project_id="nebula_sales_qa",
                tenant_id="nebula_auto",
                name="跨租户项目",
                status="active",
                data={
                    "members": [
                        {
                            "user_id": "u_annotator_001",
                            "roles": ["annotator"],
                        }
                    ]
                },
            )
        )


def test_opaque_session_persists_only_token_and_csrf_hashes() -> None:
    issued = _issue()

    assert len(issued.raw_token) >= 43
    assert len(issued.csrf_token) >= 43
    assert issued.raw_token != issued.csrf_token
    assert issued.expires_at > issued.issued_at

    with SessionLocal() as session:
        record = session.get(BrowserAuthSession, issued.session_id)
        assert record is not None
        assert len(record.token_sha256) == 64
        assert len(record.csrf_sha256) == 64
        assert issued.raw_token not in repr(record)
        assert issued.csrf_token not in repr(record)
        assert record.user_id == "u_annotator_001"
        assert record.tenant_id == "aurora_auto"
        assert record.project_id == "sales_qa"


def test_csrf_token_is_stable_across_concurrent_session_restores() -> None:
    issued = _issue()

    with SessionLocal.begin() as session:
        first = browser_session_csrf_token(
            session,
            raw_token=issued.raw_token,
            session_id=issued.session_id,
        )
    with SessionLocal.begin() as session:
        second = browser_session_csrf_token(
            session,
            raw_token=issued.raw_token,
            session_id=issued.session_id,
        )

    assert first == issued.csrf_token
    assert second == first
    actor = _authenticate(
        issued.raw_token,
        method="POST",
        csrf_token=first,
        origin="https://flow.example.test",
    )
    assert actor.user_id == "u_annotator_001"


def test_authentication_uses_current_user_and_project_roles() -> None:
    issued = _issue()

    actor = _authenticate(issued.raw_token)

    assert actor.user_id == "u_annotator_001"
    assert actor.roles == ("annotator", "review_arbitrator")
    assert actor.provider == "oidc_session"
    assert actor.tenant_ids == ("aurora_auto",)
    assert actor.project_ids == ("sales_qa",)
    assert actor.session_id == issued.session_id

    with SessionLocal.begin() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        project.data = {
            **project.data,
            "members": [
                {
                    **member,
                    "roles": ["annotator"],
                }
                if member.get("user_id") == "u_annotator_001"
                else member
                for member in project.data["members"]
            ],
        }

    downgraded = _authenticate(issued.raw_token)
    assert downgraded.roles == ("annotator",)

    with SessionLocal.begin() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        project.data = {
            **project.data,
            "members": [
                {
                    **member,
                    "roles": ["project_admin"],
                }
                if member.get("user_id") == "u_annotator_001"
                else member
                for member in project.data["members"]
            ],
        }

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token)
    assert captured.value.code == "AUTHORIZATION_REJECTED"
    assert captured.value.status_code == 403


def test_duplicate_project_member_role_bindings_fail_closed() -> None:
    issued = _issue()
    with SessionLocal.begin() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        project.data = {
            **project.data,
            "members": [
                {"user_id": "u_annotator_001", "roles": ["review_arbitrator"]},
                {"id": "u_annotator_001", "roles": ["annotator"]},
            ],
        }

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token)

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


@pytest.mark.parametrize(
    ("csrf_token", "origin", "expected_code"),
    [
        (None, "https://flow.example.test", "CSRF_TOKEN_REQUIRED"),
        ("wrong-token", "https://flow.example.test", "CSRF_TOKEN_INVALID"),
        ("valid", None, "CSRF_ORIGIN_REQUIRED"),
        ("valid", "https://attacker.example", "CSRF_ORIGIN_REJECTED"),
    ],
)
def test_cookie_authenticated_writes_fail_closed_on_csrf_or_origin(
    csrf_token: str | None,
    origin: str | None,
    expected_code: str,
) -> None:
    issued = _issue()
    supplied = issued.csrf_token if csrf_token == "valid" else csrf_token

    with pytest.raises(ApiError) as captured:
        _authenticate(
            issued.raw_token,
            method="POST",
            csrf_token=supplied,
            origin=origin,
        )

    assert captured.value.code == expected_code

    actor = _authenticate(
        issued.raw_token,
        method="POST",
        csrf_token=issued.csrf_token,
        origin="https://flow.example.test",
    )
    assert actor.user_id == "u_annotator_001"


def test_disabled_identity_user_tenant_or_project_is_rejected_without_scope_leak() -> None:
    issued = _issue()

    with SessionLocal.begin() as session:
        security = session.get(UserSecurityState, "u_annotator_001")
        assert security is not None
        security.status = "disabled"
        security.disabled_at = datetime.now(UTC)

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token)
    assert captured.value.code == "AUTH_SUBJECT_DISABLED"
    assert captured.value.status_code == 401
    assert "u_annotator_001" not in captured.value.message


@pytest.mark.parametrize(
    ("disabled_target", "expected_code", "expected_status"),
    [
        ("identity", "AUTH_SUBJECT_DISABLED", 401),
        ("tenant", "AUTH_SCOPE_REJECTED", 404),
        ("project", "AUTH_SCOPE_REJECTED", 404),
    ],
)
def test_live_identity_tenant_and_project_disablement_fails_closed_without_leak(
    disabled_target: str,
    expected_code: str,
    expected_status: int,
) -> None:
    issued = _issue()

    with SessionLocal.begin() as session:
        if disabled_target == "identity":
            target = session.get(OidcIdentity, "oidc_identity_annotator")
        elif disabled_target == "tenant":
            target = session.get(Tenant, "aurora_auto")
        else:
            target = session.get(Project, "sales_qa")
        assert target is not None
        target.status = "disabled"

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token)

    assert captured.value.code == expected_code
    assert captured.value.status_code == expected_status
    assert "aurora_auto" not in captured.value.message
    assert "sales_qa" not in captured.value.message


def test_oidc_browser_session_cannot_switch_to_another_authorized_project() -> None:
    issued = _issue()
    _add_authorized_secondary_project()

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token, project_id="sales_qa_secondary")

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


def test_oidc_browser_session_cannot_cross_tenant_even_with_project_membership() -> None:
    issued = _issue()
    _add_cross_tenant_project()

    with pytest.raises(ApiError) as captured:
        _authenticate(
            issued.raw_token,
            tenant_id="nebula_auto",
            project_id="nebula_sales_qa",
        )

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


def test_oidc_bearer_cannot_switch_to_another_authorized_project() -> None:
    _provision_identity()
    _add_authorized_secondary_project()
    actor = AuthActor(
        user_id="external-subject-001",
        roles=(),
        provider="oidc_bearer",
        issued_at=1_900_000_000,
        expires_at=2_000_000_000,
        oidc_issuer=ISSUER,
        oidc_subject="external-subject-001",
    )

    with SessionLocal.begin() as session, pytest.raises(ApiError) as captured:
        resolve_oidc_bearer_actor(
            session,
            actor,
            tenant_id="aurora_auto",
            project_id="sales_qa_secondary",
        )

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


def test_oidc_bearer_cannot_cross_tenant_even_with_project_membership() -> None:
    _provision_identity()
    _add_cross_tenant_project()
    actor = AuthActor(
        user_id="external-subject-001",
        roles=(),
        provider="oidc_bearer",
        issued_at=1_900_000_000,
        expires_at=2_000_000_000,
        oidc_issuer=ISSUER,
        oidc_subject="external-subject-001",
    )

    with SessionLocal.begin() as session, pytest.raises(ApiError) as captured:
        resolve_oidc_bearer_actor(
            session,
            actor,
            tenant_id="nebula_auto",
            project_id="nebula_sales_qa",
        )

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


def test_existing_session_is_rejected_when_identity_project_binding_changes() -> None:
    issued = _issue()
    _add_authorized_secondary_project()
    with SessionLocal.begin() as session:
        identity = session.get(OidcIdentity, "oidc_identity_annotator")
        assert identity is not None
        identity.project_id = "sales_qa_secondary"

    with pytest.raises(ApiError) as captured:
        _authenticate(issued.raw_token)

    assert captured.value.code == "AUTH_SCOPE_REJECTED"
    assert captured.value.status_code == 404


def test_expired_revoked_unknown_and_cross_project_sessions_fail_closed() -> None:
    now = datetime.now(UTC)
    issued = _issue(now=now - timedelta(hours=2))

    with pytest.raises(ApiError) as expired:
        _authenticate(issued.raw_token)
    assert expired.value.code == "AUTH_SESSION_EXPIRED"

    active = _issue()
    with pytest.raises(ApiError) as wrong_project:
        _authenticate(active.raw_token, project_id="same_tenant_unassigned")
    assert wrong_project.value.code == "AUTH_SCOPE_REJECTED"
    assert wrong_project.value.status_code == 404

    with SessionLocal.begin() as session:
        receipt = revoke_browser_session(session, raw_token=active.raw_token)
        replay = revoke_browser_session(session, raw_token=active.raw_token)
    assert receipt.session_id == active.session_id
    assert replay.revoked_at == receipt.revoked_at

    with pytest.raises(ApiError) as revoked:
        _authenticate(active.raw_token)
    assert revoked.value.code == "AUTH_SESSION_REVOKED"

    with pytest.raises(ApiError) as unknown:
        _authenticate("not-a-real-session-token-with-sufficient-length")
    assert unknown.value.code == "AUTH_SESSION_INVALID"


def test_oidc_identity_hashes_exact_issuer_and_subject() -> None:
    identity = _provision_identity()

    assert identity.issuer == ISSUER
    assert len(identity.issuer_sha256) == 64
    assert len(identity.subject_sha256) == 64
    assert identity.matches(issuer=ISSUER, subject="external-subject-001")
    assert not identity.matches(issuer=f"{ISSUER}/", subject="external-subject-001")
