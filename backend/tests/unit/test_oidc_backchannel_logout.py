from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]

from app.core.oidc import (
    OIDCBackChannelLogoutTokenValidator,
    OIDCProviderConfig,
    OIDCTokenValidationError,
)

ISSUER = "https://identity.example.test/realms/auris"
CLIENT_ID = "auris-flow-web"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = "https://identity.example.test/realms/auris/protocol/openid-connect/certs"
LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
NOW = 1_750_000_000


@dataclass
class MutableClock:
    value: float = float(NOW)

    def __call__(self) -> float:
        return self.value


class StubTransport:
    def __init__(self, jwks_documents: list[Mapping[str, object]]) -> None:
        self.responses = {
            DISCOVERY_URL: [{"issuer": ISSUER, "jwks_uri": JWKS_URL}],
            JWKS_URL: list(jwks_documents),
        }
        self.calls: list[str] = []

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        maximum_bytes: int,
    ) -> Mapping[str, Any]:
        del timeout, maximum_bytes
        self.calls.append(url)
        values = self.responses[url]
        return values.pop(0) if len(values) > 1 else values[0]


@pytest.fixture(scope="module")
def rsa_key_a() -> Any:
    return JsonWebKey.generate_key(
        "RSA",
        2048,
        options={"kid": "key-a", "alg": "RS256", "use": "sig"},
        is_private=True,
    )


@pytest.fixture(scope="module")
def rsa_key_b() -> Any:
    return JsonWebKey.generate_key(
        "RSA",
        2048,
        options={"kid": "key-b", "alg": "RS256", "use": "sig"},
        is_private=True,
    )


def _public_jwk(key: Any) -> dict[str, Any]:
    return dict(key.as_dict(is_private=False))


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "oidc-user-001",
        "aud": CLIENT_ID,
        "iat": NOW - 5,
        "exp": NOW + 60,
        "jti": "logout-event-001",
        "sid": "provider-session-001",
        "events": {LOGOUT_EVENT: {}},
    }
    claims.update(overrides)
    return claims


def _token(
    key: Any,
    *,
    kid: str = "key-a",
    claims: Mapping[str, object] | None = None,
    algorithm: str = "RS256",
) -> str:
    encoded = JsonWebToken([algorithm]).encode(
        {"alg": algorithm, "kid": kid, "typ": "logout+jwt"},
        dict(claims or _claims()),
        key,
    )
    return encoded.decode("ascii")


def _validator(
    transport: StubTransport,
    *,
    clock: MutableClock | None = None,
) -> OIDCBackChannelLogoutTokenValidator:
    return OIDCBackChannelLogoutTokenValidator(
        OIDCProviderConfig(
            issuer=ISSUER,
            audience=CLIENT_ID,
            discovery_url=DISCOVERY_URL,
            jwks_cache_ttl_seconds=300,
            clock_skew_seconds=30,
            http_timeout_seconds=2.5,
        ),
        transport=transport,
        clock=clock or MutableClock(),
    )


def test_valid_logout_token_accepts_sid_and_subject_without_retaining_raw_token(
    rsa_key_a: Any,
) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])
    raw_token = _token(rsa_key_a)

    validated = _validator(transport).validate(raw_token)

    assert validated.issuer == ISSUER
    assert validated.audiences == (CLIENT_ID,)
    assert validated.subject == "oidc-user-001"
    assert validated.session_id == "provider-session-001"
    assert validated.token_id == "logout-event-001"
    assert validated.issued_at == NOW - 5
    assert validated.expires_at == NOW + 60
    assert raw_token not in repr(validated)
    assert "oidc-user-001" not in repr(validated)
    assert "provider-session-001" not in repr(validated)
    assert "logout-event-001" not in repr(validated)


@pytest.mark.parametrize("selector", ["subject", "session"])
def test_logout_token_accepts_exactly_one_standard_session_selector(
    rsa_key_a: Any,
    selector: str,
) -> None:
    claims = _claims()
    claims.pop("sid" if selector == "subject" else "sub")
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])

    validated = _validator(transport).validate(_token(rsa_key_a, claims=claims))

    assert (validated.subject is not None) is (selector == "subject")
    assert (validated.session_id is not None) is (selector == "session")


@pytest.mark.parametrize("claim", ["iss", "aud", "iat", "exp", "jti", "events"])
def test_logout_token_rejects_missing_required_claim(rsa_key_a: Any, claim: str) -> None:
    claims = _claims()
    claims.pop(claim)
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(_token(rsa_key_a, claims=claims))


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://identity.example.test/realms/wrong"},
        {"aud": "different-client"},
        {"aud": ["different-client", "another-client"]},
    ],
)
def test_logout_token_rejects_wrong_issuer_or_missing_client_audience(
    rsa_key_a: Any,
    overrides: Mapping[str, object],
) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(_token(rsa_key_a, claims=_claims(**overrides)))


def test_logout_token_rejects_non_rs256_algorithm(rsa_key_a: Any) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])
    raw_token = _token(
        b"an-independent-hs256-secret-with-sufficient-length",
        claims=_claims(),
        algorithm="HS256",
    )

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(raw_token)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sub": None, "sid": None},
        {"iat": NOW + 31},
        {"exp": NOW - 31},
        {"exp": NOW - 10, "iat": NOW},
        {"iat": NOW, "exp": NOW + 301},
        {"jti": ""},
        {"jti": "j" * 513},
        {"sub": ""},
        {"sid": ""},
        {"events": []},
        {"events": {LOGOUT_EVENT: "not-an-object"}},
        {"events": {"different-event": {}}},
        {"nonce": "id-token-nonce"},
    ],
)
def test_logout_token_rejects_invalid_timing_identity_and_event_shape(
    rsa_key_a: Any,
    overrides: Mapping[str, object],
) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(_token(rsa_key_a, claims=_claims(**overrides)))


@pytest.mark.parametrize(
    "ordinary_claims",
    [
        {
            "iss": ISSUER,
            "sub": "oidc-user-001",
            "aud": CLIENT_ID,
            "iat": NOW - 5,
            "exp": NOW + 60,
            "nonce": "authentication-response-nonce",
        },
        {
            "iss": ISSUER,
            "sub": "oidc-user-001",
            "aud": CLIENT_ID,
            "iat": NOW - 5,
            "exp": NOW + 60,
            "jti": "ordinary-access-token",
            "scope": "openid profile",
        },
    ],
)
def test_logout_validator_cannot_be_confused_with_id_or_access_tokens(
    rsa_key_a: Any,
    ordinary_claims: Mapping[str, object],
) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(_token(rsa_key_a, claims=ordinary_claims))


def test_logout_token_rejects_wrong_signature_without_exposing_token(
    rsa_key_a: Any,
    rsa_key_b: Any,
) -> None:
    transport = StubTransport([{"keys": [_public_jwk(rsa_key_a)]}])
    raw_token = _token(rsa_key_b)

    with pytest.raises(OIDCTokenValidationError) as captured:
        _validator(transport).validate(raw_token)

    assert raw_token not in str(captured.value)
    assert raw_token not in repr(captured.value)


def test_logout_token_unknown_kid_refreshes_jwks_once(
    rsa_key_a: Any,
    rsa_key_b: Any,
) -> None:
    transport = StubTransport(
        [
            {"keys": [_public_jwk(rsa_key_a)]},
            {"keys": [_public_jwk(rsa_key_b)]},
        ]
    )
    validator = _validator(transport)
    validator.validate(_token(rsa_key_a))

    validated = validator.validate(_token(rsa_key_b, kid="key-b"))

    assert validated.token_id == "logout-event-001"
    assert transport.calls.count(JWKS_URL) == 2
