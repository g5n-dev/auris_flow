"""Idempotently provision the first explicitly approved OIDC operator.

This module is deliberately a database bootstrap, not an OIDC just-in-time
provisioner. Runtime authentication continues to reject every issuer/subject
pair that is not already present in ``oidc_identities``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, NoReturn
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth_models import OidcIdentity, UserSecurityState
from app.core.database import SessionLocal
from app.models import AuditLog, Project, Tenant, TraceRef, User

BOOTSTRAP_ROLE = "project_admin"
BOOTSTRAP_ACTOR = "system:identity-bootstrap"
BOOTSTRAP_ACTION = "oidc_identity.bootstrap"
BOOTSTRAP_SCHEMA = "auris-flow.oidc-identity-bootstrap/v1"
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class IdentityBootstrapDriftError(RuntimeError):
    """A pre-existing row does not match the approved bootstrap manifest."""


@dataclass(frozen=True)
class IdentityBootstrapSpec:
    issuer: str
    subject: str
    identity_id: str
    user_id: str
    username: str
    email: str
    display_name: str
    tenant_id: str
    tenant_name: str
    project_id: str
    project_name: str

    def __post_init__(self) -> None:
        parsed_issuer = urlparse(self.issuer)
        if (
            parsed_issuer.scheme != "https"
            or not parsed_issuer.hostname
            or parsed_issuer.username
            or parsed_issuer.password
            or parsed_issuer.query
            or parsed_issuer.fragment
        ):
            raise ValueError("bootstrap issuer must be an exact HTTPS issuer URL")
        if (
            not self.subject
            or len(self.subject) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in self.subject)
        ):
            raise ValueError("bootstrap subject is invalid")
        for field_name in ("identity_id", "user_id", "tenant_id", "project_id"):
            value = getattr(self, field_name)
            maximum = 128 if field_name == "identity_id" else 64
            if len(value) > maximum or not IDENTIFIER_PATTERN.fullmatch(value):
                raise ValueError(f"bootstrap {field_name} is invalid")
        if not USERNAME_PATTERN.fullmatch(self.username):
            raise ValueError("bootstrap username is invalid")
        if (
            len(self.email) > 254
            or self.email.count("@") != 1
            or any(character.isspace() for character in self.email)
        ):
            raise ValueError("bootstrap email is invalid")
        for field_name in ("display_name", "tenant_name", "project_name"):
            value = getattr(self, field_name)
            if not value.strip() or len(value) > 255 or any(ord(char) < 32 for char in value):
                raise ValueError(f"bootstrap {field_name} is invalid")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> IdentityBootstrapSpec:
        values = environment if environment is not None else os.environ

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        return cls(
            issuer=required("AURIS_BOOTSTRAP_OIDC_ISSUER"),
            subject=required("AURIS_BOOTSTRAP_OIDC_SUBJECT"),
            identity_id=values.get(
                "AURIS_BOOTSTRAP_IDENTITY_ID", "oidc_bootstrap_operator_001"
            ).strip(),
            user_id=values.get("AURIS_BOOTSTRAP_USER_ID", "u_bootstrap_operator_001").strip(),
            username=values.get("AURIS_BOOTSTRAP_USERNAME", "bootstrap-operator").strip(),
            email=values.get("AURIS_BOOTSTRAP_EMAIL", "bootstrap-operator@auris.invalid").strip(),
            display_name=values.get("AURIS_BOOTSTRAP_DISPLAY_NAME", "Bootstrap Operator").strip(),
            tenant_id=values.get("AURIS_BOOTSTRAP_TENANT_ID", "aurora_auto").strip(),
            tenant_name=values.get(
                "AURIS_BOOTSTRAP_TENANT_NAME", "Auris Flow Default Tenant"
            ).strip(),
            project_id=values.get("AURIS_BOOTSTRAP_PROJECT_ID", "sales_qa").strip(),
            project_name=values.get(
                "AURIS_BOOTSTRAP_PROJECT_NAME", "Auris Flow Default Project"
            ).strip(),
        )


@dataclass(frozen=True)
class IdentityBootstrapResult:
    created: bool
    identity_id: str
    user_id: str
    tenant_id: str
    project_id: str
    trace_id: str
    trace_ref_id: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence(spec: IdentityBootstrapSpec) -> dict[str, Any]:
    return {
        "schema_version": BOOTSTRAP_SCHEMA,
        "identity_id": spec.identity_id,
        "issuer_sha256": _sha256(spec.issuer),
        "subject_sha256": _sha256(spec.subject),
        "user_id": spec.user_id,
        "tenant_id": spec.tenant_id,
        "project_id": spec.project_id,
        "roles": [BOOTSTRAP_ROLE],
    }


def _result(spec: IdentityBootstrapSpec, *, created: bool) -> IdentityBootstrapResult:
    evidence_hash = _sha256(json.dumps(_evidence(spec), sort_keys=True, separators=(",", ":")))
    return IdentityBootstrapResult(
        created=created,
        identity_id=spec.identity_id,
        user_id=spec.user_id,
        tenant_id=spec.tenant_id,
        project_id=spec.project_id,
        trace_id=f"trace_oidc_bootstrap_{evidence_hash[:32]}",
        trace_ref_id=f"trace_ref_oidc_bootstrap_{evidence_hash[:32]}",
    )


def _drift(field: str) -> NoReturn:
    raise IdentityBootstrapDriftError(f"identity bootstrap drift detected: {field}")


def _validate_tenant(tenant: Tenant, spec: IdentityBootstrapSpec) -> None:
    if tenant.tenant_code != spec.tenant_id:
        _drift("tenant.tenant_code")
    if tenant.status != "active":
        _drift("tenant.status")


def _bootstrap_member(project: Project, spec: IdentityBootstrapSpec) -> dict[str, Any] | None:
    if not isinstance(project.data, dict):
        _drift("project.data")
    members = project.data.get("members")
    if members is None:
        return None
    if not isinstance(members, list):
        _drift("project.members")
    matching = [
        member
        for member in members
        if isinstance(member, dict) and (member.get("user_id") or member.get("id")) == spec.user_id
    ]
    if len(matching) > 1:
        _drift("project.members.duplicate")
    return matching[0] if matching else None


def _validate_project(project: Project, spec: IdentityBootstrapSpec) -> None:
    if project.tenant_id != spec.tenant_id:
        _drift("project.tenant_id")
    if project.status != "active":
        _drift("project.status")
    member = _bootstrap_member(project, spec)
    if member != {"user_id": spec.user_id, "roles": [BOOTSTRAP_ROLE]}:
        _drift("project.members.roles")
    member_ids = project.data.get("member_user_ids")
    if not isinstance(member_ids, list) or spec.user_id not in member_ids:
        _drift("project.member_user_ids")


def _validate_user(user: User, spec: IdentityBootstrapSpec) -> None:
    if user.tenant_id != spec.tenant_id:
        _drift("user.tenant_id")
    if user.email != spec.email:
        _drift("user.email")
    if user.name != spec.display_name:
        _drift("user.name")
    if user.roles != [BOOTSTRAP_ROLE]:
        _drift("user.roles")
    if not isinstance(user.data, dict):
        _drift("user.data")
    data = user.data
    if data.get("bootstrap_managed") is not True or data.get("oidc_username") != spec.username:
        _drift("user.bootstrap_metadata")


def _validate_security(security: UserSecurityState, spec: IdentityBootstrapSpec) -> None:
    if security.user_id != spec.user_id or security.status != "active":
        _drift("user_security_state.status")
    if security.disabled_at is not None or security.authz_version < 1:
        _drift("user_security_state.version")


def _validate_identity(identity: OidcIdentity, spec: IdentityBootstrapSpec) -> None:
    if not identity.matches(issuer=spec.issuer, subject=spec.subject):
        _drift("identity.issuer_subject")
    expected = (
        spec.identity_id,
        spec.user_id,
        spec.tenant_id,
        spec.project_id,
        "active",
    )
    actual = (
        identity.identity_id,
        identity.user_id,
        identity.tenant_id,
        identity.project_id,
        identity.status,
    )
    if actual != expected:
        _drift("identity.scope")


def _validate_evidence(
    session: Session,
    spec: IdentityBootstrapSpec,
    result: IdentityBootstrapResult,
) -> None:
    expected = _evidence(spec)
    trace = session.get(TraceRef, result.trace_ref_id)
    if (
        trace is None
        or trace.tenant_id != spec.tenant_id
        or trace.project_id != spec.project_id
        or trace.status != "completed"
        or trace.trace_id != result.trace_id
        or trace.payload != expected
    ):
        _drift("trace_evidence")
    audits = session.scalars(
        select(AuditLog).where(
            AuditLog.action == BOOTSTRAP_ACTION,
            AuditLog.object_id == spec.identity_id,
        )
    ).all()
    if len(audits) != 1:
        _drift("audit_evidence")
    audit = audits[0]
    if (
        audit.tenant_id != spec.tenant_id
        or audit.project_id != spec.project_id
        or audit.actor_id != BOOTSTRAP_ACTOR
        or audit.object_type != "oidc_identity"
        or audit.result != "created"
        or audit.trace_id != result.trace_id
        or audit.idempotency_key != f"identity-bootstrap:{spec.identity_id}"
        or audit.before_json is not None
        or audit.after_json != expected
    ):
        _drift("audit_evidence")


def _verify_existing(
    session: Session,
    spec: IdentityBootstrapSpec,
    identity: OidcIdentity,
) -> IdentityBootstrapResult:
    tenant = session.get(Tenant, spec.tenant_id)
    project = session.get(Project, spec.project_id)
    user = session.get(User, spec.user_id)
    security = session.get(UserSecurityState, spec.user_id)
    if tenant is None:
        _drift("tenant.missing")
    if project is None:
        _drift("project.missing")
    if user is None:
        _drift("user.missing")
    if security is None:
        _drift("user_security_state.missing")
    _validate_tenant(tenant, spec)
    _validate_project(project, spec)
    _validate_user(user, spec)
    _validate_security(security, spec)
    _validate_identity(identity, spec)
    result = _result(spec, created=False)
    _validate_evidence(session, spec, result)
    return result


def _prepare_scope(session: Session, spec: IdentityBootstrapSpec) -> None:
    tenant = session.get(Tenant, spec.tenant_id)
    if tenant is None:
        tenant = Tenant(
            tenant_id=spec.tenant_id,
            tenant_code=spec.tenant_id,
            name=spec.tenant_name,
            status="active",
            data={"bootstrap_managed": True},
        )
        session.add(tenant)
    else:
        _validate_tenant(tenant, spec)

    project = session.get(Project, spec.project_id)
    if project is None:
        project = Project(
            project_id=spec.project_id,
            tenant_id=spec.tenant_id,
            name=spec.project_name,
            status="active",
            data={
                "bootstrap_managed": True,
                "member_user_ids": [spec.user_id],
                "members": [{"user_id": spec.user_id, "roles": [BOOTSTRAP_ROLE]}],
            },
        )
        session.add(project)
    else:
        if project.tenant_id != spec.tenant_id or project.status != "active":
            _drift("project.scope")
        member = _bootstrap_member(project, spec)
        if member is not None and member != {
            "user_id": spec.user_id,
            "roles": [BOOTSTRAP_ROLE],
        }:
            _drift("project.members.roles")
        if not isinstance(project.data, dict):
            _drift("project.data")
        data = dict(project.data)
        members = list(data.get("members") or [])
        if member is None:
            members.append({"user_id": spec.user_id, "roles": [BOOTSTRAP_ROLE]})
        raw_member_ids = data.get("member_user_ids")
        if raw_member_ids is not None and not isinstance(raw_member_ids, list):
            _drift("project.member_user_ids")
        member_ids = list(raw_member_ids or [])
        if spec.user_id not in member_ids:
            member_ids.append(spec.user_id)
        project.data = {**data, "members": members, "member_user_ids": member_ids}

    email_owner = session.scalar(select(User).where(User.email == spec.email))
    if email_owner is not None and email_owner.user_id != spec.user_id:
        _drift("user.email_collision")
    user = session.get(User, spec.user_id)
    if user is None:
        session.add(
            User(
                user_id=spec.user_id,
                tenant_id=spec.tenant_id,
                email=spec.email,
                name=spec.display_name,
                roles=[BOOTSTRAP_ROLE],
                data={"bootstrap_managed": True, "oidc_username": spec.username},
            )
        )
    else:
        _validate_user(user, spec)

    session.flush()
    security = session.get(UserSecurityState, spec.user_id)
    if security is None:
        session.add(
            UserSecurityState(
                user_id=spec.user_id,
                status="active",
                authz_version=1,
            )
        )
    else:
        _validate_security(security, spec)


def bootstrap_identity(
    session: Session,
    spec: IdentityBootstrapSpec,
) -> IdentityBootstrapResult:
    """Create or verify the fixed mapping inside the caller's transaction.

    The function never repairs or expands an existing mapping. Any discrepancy
    raises ``IdentityBootstrapDriftError`` so the one-shot container fails closed.
    """

    issuer_hash = _sha256(spec.issuer)
    subject_hash = _sha256(spec.subject)
    by_id = session.get(OidcIdentity, spec.identity_id)
    by_subject = session.scalar(
        select(OidcIdentity).where(
            OidcIdentity.issuer_sha256 == issuer_hash,
            OidcIdentity.subject_sha256 == subject_hash,
        )
    )
    if by_id is not None:
        _validate_identity(by_id, spec)
        if by_subject is None or by_subject.identity_id != by_id.identity_id:
            _drift("identity.subject_index")
        return _verify_existing(session, spec, by_id)
    if by_subject is not None:
        _drift("identity.id_collision")

    result = _result(spec, created=True)
    if session.get(TraceRef, result.trace_ref_id) is not None:
        _drift("trace_evidence.preexisting")
    existing_audit = session.scalar(
        select(AuditLog).where(
            AuditLog.action == BOOTSTRAP_ACTION,
            AuditLog.object_id == spec.identity_id,
        )
    )
    if existing_audit is not None:
        _drift("audit_evidence.preexisting")

    _prepare_scope(session, spec)
    identity = OidcIdentity.create(
        identity_id=spec.identity_id,
        issuer=spec.issuer,
        subject=spec.subject,
        user_id=spec.user_id,
        tenant_id=spec.tenant_id,
        project_id=spec.project_id,
        status="active",
    )
    evidence = _evidence(spec)
    session.add_all(
        [
            identity,
            TraceRef(
                trace_ref_id=result.trace_ref_id,
                tenant_id=spec.tenant_id,
                project_id=spec.project_id,
                status="completed",
                trace_id=result.trace_id,
                payload=evidence,
            ),
            AuditLog(
                tenant_id=spec.tenant_id,
                project_id=spec.project_id,
                actor_id=BOOTSTRAP_ACTOR,
                action=BOOTSTRAP_ACTION,
                object_type="oidc_identity",
                object_id=spec.identity_id,
                result="created",
                trace_id=result.trace_id,
                idempotency_key=f"identity-bootstrap:{spec.identity_id}",
                before_json=None,
                after_json=evidence,
            ),
        ]
    )
    session.flush()
    return result


def main() -> int:
    try:
        spec = IdentityBootstrapSpec.from_environment()
        with SessionLocal.begin() as session:
            result = bootstrap_identity(session, spec)
    except (IdentityBootstrapDriftError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("identity bootstrap database operation failed", file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "status": "created" if result.created else "verified",
                "identity_id": result.identity_id,
                "user_id": result.user_id,
                "tenant_id": result.tenant_id,
                "project_id": result.project_id,
                "trace_id": result.trace_id,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
