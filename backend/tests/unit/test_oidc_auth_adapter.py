from __future__ import annotations

from types import MappingProxyType

import pytest

from app.core.auth import OIDCAuthProvider
from app.core.config import Settings
from app.core.errors import ApiError
from app.core.oidc import (
    OIDCConfigurationError,
    OIDCProviderUnavailableError,
    OIDCTokenValidationError,
    OIDCValidatedClaims,
)


class StubValidator:
    def __init__(self, result: OIDCValidatedClaims | Exception) -> None:
        self.result = result
        self.seen: list[tuple[str, float | None]] = []

    def validate(self, token: str, *, now: float | None = None) -> OIDCValidatedClaims:
        self.seen.append((token, now))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        auth_provider="oidc",
        oidc_issuer="https://identity.example.test/realms/auris",
        oidc_client_id="auris-flow",
        oidc_audience="auris-flow-api",
        oidc_redirect_uri="https://flow.example.test/api/v1/auth/oidc/callback",
    )


def _claims() -> OIDCValidatedClaims:
    return OIDCValidatedClaims(
        subject="external-subject-001",
        issuer="https://identity.example.test/realms/auris",
        audiences=("auris-flow-api",),
        expires_at=1_800_000_000,
        issued_at=1_799_999_000,
        claims=MappingProxyType({"sub": "external-subject-001"}),
    )


def test_oidc_provider_returns_unprivileged_subject_for_database_mapping() -> None:
    validator = StubValidator(_claims())
    provider = OIDCAuthProvider(_settings(), validator=validator)  # type: ignore[arg-type]

    actor = provider.authenticate("signed-oidc-token", now=1_799_999_100)

    assert actor.user_id == "external-subject-001"
    assert actor.roles == ()
    assert actor.tenant_ids == ()
    assert actor.project_ids == ()
    assert actor.provider == "oidc_bearer"
    assert actor.oidc_issuer == "https://identity.example.test/realms/auris"
    assert actor.oidc_subject == "external-subject-001"
    assert validator.seen == [("signed-oidc-token", 1_799_999_100)]


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (OIDCTokenValidationError(), "UNAUTHORIZED", 401),
        (OIDCProviderUnavailableError(), "OIDC_PROVIDER_UNAVAILABLE", 503),
        (OIDCConfigurationError(), "OIDC_CONFIGURATION_INVALID", 500),
    ],
)
def test_oidc_provider_translates_stable_non_sensitive_errors(
    failure: Exception,
    code: str,
    status: int,
) -> None:
    provider = OIDCAuthProvider(
        _settings(),
        validator=StubValidator(failure),  # type: ignore[arg-type]
    )

    with pytest.raises(ApiError) as captured:
        provider.authenticate("must-not-leak-this-token")

    assert captured.value.code == code
    assert captured.value.status_code == status
    assert "must-not-leak-this-token" not in captured.value.message
