from __future__ import annotations

import json
from contextlib import nullcontext

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

import app.identity_bootstrap as identity_bootstrap_module
from app.auth_models import OidcIdentity, UserSecurityState
from app.core.browser_session import (
    authenticate_browser_session,
    create_browser_session,
    find_oidc_identity,
)
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.identity_bootstrap import (
    IdentityBootstrapDriftError,
    IdentityBootstrapResult,
    IdentityBootstrapSpec,
    bootstrap_identity,
)
from app.models import AuditLog, Project, Tenant, TraceRef, User

SPEC = IdentityBootstrapSpec(
    issuer="https://identity.example.test/realms/auris-flow",
    subject="9d1c5cc4-e661-4af6-8a6f-7402d2555c35",
    identity_id="oidc_bootstrap_operator_001",
    user_id="u_bootstrap_operator_001",
    username="bootstrap-operator",
    email="bootstrap-operator@auris.invalid",
    display_name="Bootstrap Operator",
    tenant_id="bootstrap_tenant",
    tenant_name="Bootstrap Tenant",
    project_id="bootstrap_project",
    project_name="Bootstrap Project",
)


def _bootstrap(spec: IdentityBootstrapSpec = SPEC):
    with SessionLocal.begin() as session:
        return bootstrap_identity(session, spec)


def test_first_bootstrap_creates_minimal_live_mapping_trace_and_audit_atomically() -> None:
    result = _bootstrap()

    assert result.created is True
    assert result.identity_id == SPEC.identity_id
    with SessionLocal() as session:
        tenant = session.get(Tenant, SPEC.tenant_id)
        project = session.get(Project, SPEC.project_id)
        user = session.get(User, SPEC.user_id)
        security = session.get(UserSecurityState, SPEC.user_id)
        identity = session.get(OidcIdentity, SPEC.identity_id)
        trace = session.get(TraceRef, result.trace_ref_id)
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "oidc_identity.bootstrap",
                AuditLog.object_id == SPEC.identity_id,
            )
        )

        assert tenant is not None
        assert tenant.status == "active"
        assert project is not None
        assert project.tenant_id == SPEC.tenant_id
        assert project.data["members"] == [{"user_id": SPEC.user_id, "roles": ["project_admin"]}]
        assert user is not None
        assert user.roles == ["project_admin"]
        assert security is not None
        assert security.status == "active"
        assert security.authz_version == 1
        assert identity is not None
        assert identity.matches(issuer=SPEC.issuer, subject=SPEC.subject)
        assert identity.tenant_id == SPEC.tenant_id
        assert identity.project_id == SPEC.project_id
        assert trace is not None
        assert trace.trace_id == result.trace_id
        assert audit is not None
        assert audit.actor_id == "system:identity-bootstrap"
        assert audit.result == "created"

        evidence = json.dumps(
            {"trace": trace.payload, "audit": audit.after_json},
            sort_keys=True,
        )
        assert SPEC.subject not in evidence
        assert identity.subject_sha256 in evidence

    with SessionLocal.begin() as session:
        issued = create_browser_session(
            session,
            identity_id=SPEC.identity_id,
            ttl_seconds=3600,
        )
    with SessionLocal.begin() as session:
        actor = authenticate_browser_session(
            session,
            raw_token=issued.raw_token,
            tenant_id=SPEC.tenant_id,
            project_id=SPEC.project_id,
            method="GET",
            csrf_token=None,
            origin=None,
            allowed_origins=(),
        )
    assert actor.roles == ("project_admin",)


def test_repeat_bootstrap_is_idempotent_and_does_not_duplicate_evidence() -> None:
    first = _bootstrap()
    second = _bootstrap()

    assert first.created is True
    assert second.created is False
    assert second.trace_id == first.trace_id
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(OidcIdentity)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "oidc_identity.bootstrap",
                    AuditLog.object_id == SPEC.identity_id,
                )
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(TraceRef)) == 1


@pytest.mark.parametrize("drift", ["extra-user-role", "missing-project-role"])
def test_repeat_bootstrap_fails_closed_on_permission_drift_without_regranting(
    drift: str,
) -> None:
    _bootstrap()
    with SessionLocal.begin() as session:
        user = session.get(User, SPEC.user_id)
        project = session.get(Project, SPEC.project_id)
        assert user is not None and project is not None
        if drift == "extra-user-role":
            user.roles = ["project_admin", "asset_manager"]
        else:
            project.data = {
                **project.data,
                "members": [{"user_id": SPEC.user_id, "roles": []}],
            }

    with pytest.raises(IdentityBootstrapDriftError):
        _bootstrap()

    with SessionLocal() as session:
        user = session.get(User, SPEC.user_id)
        project = session.get(Project, SPEC.project_id)
        assert user is not None and project is not None
        if drift == "extra-user-role":
            assert user.roles == ["project_admin", "asset_manager"]
        else:
            assert project.data["members"][0]["roles"] == []


def test_fixed_identity_collision_and_unknown_subject_fail_closed() -> None:
    _bootstrap()
    conflicting = IdentityBootstrapSpec(
        **{**SPEC.as_dict(), "subject": "different-external-subject"}
    )

    with pytest.raises(IdentityBootstrapDriftError, match="identity"):
        _bootstrap(conflicting)

    with SessionLocal() as session:
        identity = session.get(OidcIdentity, SPEC.identity_id)
        assert identity is not None
        assert identity.matches(issuer=SPEC.issuer, subject=SPEC.subject)
        with pytest.raises(ApiError) as unknown:
            find_oidc_identity(
                session,
                issuer=SPEC.issuer,
                subject="unprovisioned-subject",
            )
    assert unknown.value.code == "OIDC_IDENTITY_NOT_PROVISIONED"
    assert unknown.value.status_code == 403


def test_duplicate_bootstrap_audit_is_detected_as_drift() -> None:
    result = _bootstrap()
    with SessionLocal.begin() as session:
        session.add(
            AuditLog(
                tenant_id=SPEC.tenant_id,
                project_id=SPEC.project_id,
                actor_id="system:identity-bootstrap",
                action="oidc_identity.bootstrap",
                object_type="oidc_identity",
                object_id=SPEC.identity_id,
                result="created",
                trace_id=f"{result.trace_id}_duplicate",
                idempotency_key="identity-bootstrap:duplicate-unit-fixture",
                before_json=None,
                after_json={},
            )
        )

    with pytest.raises(IdentityBootstrapDriftError, match="audit_evidence"):
        _bootstrap()


def test_conflicting_email_rolls_back_new_scope() -> None:
    with SessionLocal.begin() as session:
        session.add(
            User(
                user_id="existing_user",
                tenant_id="aurora_auto",
                email=SPEC.email,
                name="Existing User",
                roles=["annotator"],
                data={},
            )
        )

    with pytest.raises(IdentityBootstrapDriftError, match="email"):
        _bootstrap()

    with SessionLocal() as session:
        assert session.get(Tenant, SPEC.tenant_id) is None
        assert session.get(Project, SPEC.project_id) is None
        assert session.get(OidcIdentity, SPEC.identity_id) is None


def test_environment_loader_and_cli_output_never_disclose_subject(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {
        "AURIS_BOOTSTRAP_OIDC_ISSUER": SPEC.issuer,
        "AURIS_BOOTSTRAP_OIDC_SUBJECT": SPEC.subject,
        "AURIS_BOOTSTRAP_TENANT_ID": SPEC.tenant_id,
        "AURIS_BOOTSTRAP_PROJECT_ID": SPEC.project_id,
    }
    loaded = IdentityBootstrapSpec.from_environment(environment)
    assert loaded.subject == SPEC.subject
    assert loaded.identity_id == "oidc_bootstrap_operator_001"
    assert loaded.user_id == "u_bootstrap_operator_001"

    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    class FakeSessionLocal:
        @staticmethod
        def begin():
            return nullcontext(object())

    result = IdentityBootstrapResult(
        created=True,
        identity_id=loaded.identity_id,
        user_id=loaded.user_id,
        tenant_id=loaded.tenant_id,
        project_id=loaded.project_id,
        trace_id="trace_bootstrap_unit",
        trace_ref_id="trace_ref_bootstrap_unit",
    )
    monkeypatch.setattr(identity_bootstrap_module, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(identity_bootstrap_module, "bootstrap_identity", lambda *_: result)

    assert identity_bootstrap_module.main() == 0
    output = capsys.readouterr()
    assert '"status": "created"' in output.out
    assert SPEC.subject not in output.out
    assert output.err == ""


def test_cli_configuration_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("AURIS_BOOTSTRAP_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("AURIS_BOOTSTRAP_OIDC_SUBJECT", SPEC.subject)

    assert identity_bootstrap_module.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "AURIS_BOOTSTRAP_OIDC_ISSUER is required\n"
    assert SPEC.subject not in output.err


def test_cli_database_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AURIS_BOOTSTRAP_OIDC_ISSUER", SPEC.issuer)
    monkeypatch.setenv("AURIS_BOOTSTRAP_OIDC_SUBJECT", SPEC.subject)

    class FakeSessionLocal:
        @staticmethod
        def begin():
            return nullcontext(object())

    def fail_database(*_: object) -> None:
        raise SQLAlchemyError

    monkeypatch.setattr(identity_bootstrap_module, "SessionLocal", FakeSessionLocal)
    monkeypatch.setattr(identity_bootstrap_module, "bootstrap_identity", fail_database)

    assert identity_bootstrap_module.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "identity bootstrap database operation failed\n"
    assert SPEC.subject not in output.err


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "http://identity.example.test/realms/auris"),
        ("subject", ""),
        ("user_id", "invalid/user"),
        ("username", "invalid/user"),
        ("email", "not-an-email"),
        ("display_name", " "),
    ],
)
def test_bootstrap_spec_rejects_ambiguous_or_unsafe_values(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        IdentityBootstrapSpec(**{**SPEC.as_dict(), field: value})
