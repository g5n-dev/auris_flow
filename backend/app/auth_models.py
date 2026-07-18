"""Strong authentication tables kept separate from the large domain model module.

Importing this module registers the tables on ``app.models.Base.metadata``.
The separation lets the OIDC/session boundary evolve without coupling identity
provider concepts to the business-domain models.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, TimestampMixin


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class UserSecurityState(Base, TimestampMixin):
    __tablename__ = "user_security_states"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled', 'suspended')",
            name="ck_user_security_states_status",
        ),
        CheckConstraint("authz_version > 0", name="ck_user_security_states_authz_version"),
        CheckConstraint(
            "(status = 'active' AND disabled_at IS NULL) OR "
            "(status <> 'active' AND disabled_at IS NOT NULL)",
            name="ck_user_security_states_disabled_at",
        ),
    )

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authz_version: Mapped[int] = mapped_column(nullable=False, default=1)


class OidcIdentity(Base, TimestampMixin):
    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer_sha256",
            "subject_sha256",
            name="uq_oidc_identities_issuer_subject",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_oidc_identities_status",
        ),
        Index("ix_oidc_identities_user_id", "user_id"),
        Index("ix_oidc_identities_scope", "tenant_id", "project_id", "status"),
    )

    identity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    issuer_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @classmethod
    def create(
        cls,
        *,
        identity_id: str,
        issuer: str,
        subject: str,
        user_id: str,
        tenant_id: str,
        project_id: str,
        status: str = "active",
    ) -> OidcIdentity:
        return cls(
            identity_id=identity_id,
            issuer=issuer,
            issuer_sha256=_sha256(issuer),
            subject_sha256=_sha256(subject),
            user_id=user_id,
            tenant_id=tenant_id,
            project_id=project_id,
            status=status,
        )

    def matches(self, *, issuer: str, subject: str) -> bool:
        return (
            self.issuer == issuer
            and self.issuer_sha256 == _sha256(issuer)
            and self.subject_sha256 == _sha256(subject)
        )


class OidcAuthorizationState(Base, TimestampMixin):
    __tablename__ = "oidc_authorization_states"
    __table_args__ = (
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_oidc_authorization_states_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= issued_at",
            name="ck_oidc_authorization_states_consumed",
        ),
        Index("ix_oidc_authorization_states_expiry", "expires_at", "consumed_at"),
    )

    state_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    return_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="/")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserAuthSession(Base, TimestampMixin):
    __tablename__ = "browser_auth_sessions"
    __table_args__ = (
        UniqueConstraint("token_sha256", name="uq_browser_auth_sessions_token_sha256"),
        CheckConstraint("expires_at > issued_at", name="ck_browser_auth_sessions_expiry"),
        CheckConstraint(
            "last_seen_at >= issued_at AND last_seen_at <= expires_at",
            name="ck_browser_auth_sessions_last_seen",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= issued_at",
            name="ck_browser_auth_sessions_revoked",
        ),
        Index(
            "ix_browser_auth_sessions_user_active",
            "user_id",
            "revoked_at",
            "expires_at",
        ),
        Index("ix_browser_auth_sessions_identity", "oidc_identity_id", "expires_at"),
    )

    browser_session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    oidc_identity_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey(
            "oidc_identities.identity_id",
            ondelete="RESTRICT",
            onupdate="RESTRICT",
        ),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.user_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenants.tenant_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("projects.project_id", ondelete="RESTRICT", onupdate="RESTRICT"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="oidc_session")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @property
    def session_id(self) -> str:
        return self.browser_session_id
