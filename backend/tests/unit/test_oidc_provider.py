from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]

from app.core.oidc import (
    HTTPXOIDCTransport,
    OIDCConfigurationError,
    OIDCIDTokenValidator,
    OIDCProviderConfig,
    OIDCProviderUnavailableError,
    OIDCTokenValidationError,
    OIDCTokenValidator,
)

ISSUER = "https://identity.example.test/realms/auris"
AUDIENCE = "auris-flow-api"
CLIENT_ID = "auris-flow-web"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
JWKS_URL = "https://identity.example.test/realms/auris/protocol/openid-connect/certs"
NOW = 1_750_000_000


@dataclass
class MutableClock:
    value: float = float(NOW)

    def __call__(self) -> float:
        return self.value


class StubTransport:
    def __init__(self, responses: Mapping[str, list[object]]) -> None:
        self.responses = {url: list(values) for url, values in responses.items()}
        self.calls: list[tuple[str, float]] = []
        self.maximum_bytes: list[tuple[str, int | None]] = []

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        maximum_bytes: int | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append((url, timeout))
        self.maximum_bytes.append((url, maximum_bytes))
        responses = self.responses.get(url)
        if not responses:
            raise RuntimeError("unexpected network request")
        value = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(value, BaseException):
            raise value
        if not isinstance(value, Mapping):
            raise TypeError("response is not an object")
        return value

    def call_count(self, url: str) -> int:
        return sum(called_url == url for called_url, _timeout in self.calls)


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


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "oidc-user-001",
        "aud": AUDIENCE,
        "iat": NOW - 60,
        "exp": NOW + 300,
        "typ": "Bearer",
        "email": "operator@example.test",
    }
    claims.update(overrides)
    return claims


def _rs256_token(
    key: Any,
    *,
    kid: str,
    claims: Mapping[str, object] | None = None,
    header_type: str = "JWT",
) -> str:
    token = JsonWebToken(["RS256"]).encode(
        {"alg": "RS256", "kid": kid, "typ": header_type},
        dict(claims or _claims()),
        key,
    )
    return token.decode("ascii")


def _compact_token(
    header: Mapping[str, object], payload: Mapping[str, object], signature: str
) -> str:
    def encode(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode(header)}.{encode(payload)}.{signature}"


def _public_jwk(key: Any) -> dict[str, Any]:
    return dict(key.as_dict(is_private=False))


def _transport(*jwks_documents: Mapping[str, object]) -> StubTransport:
    return StubTransport(
        {
            DISCOVERY_URL: [{"issuer": ISSUER, "jwks_uri": JWKS_URL}],
            JWKS_URL: list(jwks_documents),
        }
    )


def _validator(
    transport: StubTransport,
    *,
    clock: MutableClock | None = None,
    cache_ttl_seconds: int = 300,
    clock_skew_seconds: int = 30,
) -> OIDCTokenValidator:
    return OIDCTokenValidator(
        OIDCProviderConfig(
            issuer=ISSUER,
            audience=AUDIENCE,
            discovery_url=DISCOVERY_URL,
            jwks_cache_ttl_seconds=cache_ttl_seconds,
            clock_skew_seconds=clock_skew_seconds,
            http_timeout_seconds=2.5,
        ),
        transport=transport,
        clock=clock or MutableClock(),
    )


def _id_token_validator(transport: StubTransport) -> OIDCIDTokenValidator:
    return OIDCIDTokenValidator(
        OIDCProviderConfig(
            issuer=ISSUER,
            audience=CLIENT_ID,
            discovery_url=DISCOVERY_URL,
            jwks_cache_ttl_seconds=300,
            clock_skew_seconds=30,
            http_timeout_seconds=2.5,
        ),
        client_id=CLIENT_ID,
        transport=transport,
        clock=MutableClock(),
    )


@pytest.mark.parametrize("audience", [AUDIENCE, ["another-api", AUDIENCE]])
def test_validates_rs256_token_with_string_or_array_audience(
    rsa_key_a: Any,
    audience: object,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(aud=audience))

    validated = _validator(transport).validate(token)

    assert validated.subject == "oidc-user-001"
    assert validated.issuer == ISSUER
    assert AUDIENCE in validated.audiences
    assert validated.expires_at == NOW + 300
    assert validated.claims["email"] == "operator@example.test"


def test_validator_uses_separate_response_limits_for_discovery_and_jwks(
    rsa_key_a: Any,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})

    _validator(transport).validate(_rs256_token(rsa_key_a, kid="key-a"))

    assert transport.maximum_bytes == [
        (DISCOVERY_URL, 128 * 1024),
        (JWKS_URL, 512 * 1024),
    ]


def test_default_transport_stops_before_buffering_an_oversized_json_document(
    monkeypatch,
) -> None:
    canary = b"sensitive-tail-must-not-be-consumed"

    class GuardedResponse:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self) -> None:
            self.chunks_seen = 0

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self):
            for chunk in (b'{"x":"12', b'3456789"}', canary):
                self.chunks_seen += 1
                yield chunk

    class ResponseContext:
        def __init__(self, response: GuardedResponse) -> None:
            self.response = response

        def __enter__(self) -> GuardedResponse:
            return self.response

        def __exit__(self, *_args: object) -> None:
            return None

    response = GuardedResponse()
    seen: dict[str, object] = {}

    def fake_stream(method: str, url: str, **kwargs: object) -> ResponseContext:
        seen.update(method=method, url=url, **kwargs)
        return ResponseContext(response)

    def forbidden_get(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("OIDC transport must not use buffering httpx.get")

    monkeypatch.setattr("app.core.oidc.httpx.stream", fake_stream)
    monkeypatch.setattr("app.core.oidc.httpx.get", forbidden_get)

    with pytest.raises(OIDCProviderUnavailableError) as captured:
        HTTPXOIDCTransport().get_json(DISCOVERY_URL, timeout=2.5, maximum_bytes=8)

    assert response.chunks_seen == 2
    assert seen["method"] == "GET"
    assert seen["follow_redirects"] is False
    assert canary.decode() not in str(captured.value)


def test_id_token_accepts_client_id_audience_without_azp_for_single_audience(
    rsa_key_a: Any,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(aud=CLIENT_ID))

    validated = _id_token_validator(transport).validate(token)

    assert validated.audiences == (CLIENT_ID,)


def test_id_token_requires_matching_azp_for_multiple_audiences(rsa_key_a: Any) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(
        rsa_key_a,
        kid="key-a",
        claims=_claims(aud=[CLIENT_ID, AUDIENCE], azp=CLIENT_ID),
    )

    validated = _id_token_validator(transport).validate(token)

    assert validated.audiences == (CLIENT_ID, AUDIENCE)


@pytest.mark.parametrize("azp", [None, "other-client"])
def test_id_token_rejects_missing_or_wrong_azp_for_multiple_audiences(
    rsa_key_a: Any,
    azp: str | None,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    claims = _claims(aud=[CLIENT_ID, AUDIENCE])
    if azp is not None:
        claims["azp"] = azp
    token = _rs256_token(rsa_key_a, kid="key-a", claims=claims)

    with pytest.raises(OIDCTokenValidationError):
        _id_token_validator(transport).validate(token)


def test_id_token_rejects_api_audience_when_client_id_is_absent(rsa_key_a: Any) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(aud=AUDIENCE))

    with pytest.raises(OIDCTokenValidationError):
        _id_token_validator(transport).validate(token)


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://identity.example.test/realms/auris/"),
        ("aud", "different-api"),
        ("aud", ["different-api", "another-api"]),
    ],
)
def test_rejects_non_matching_exact_issuer_or_audience(
    rsa_key_a: Any,
    claim: str,
    value: object,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(**{claim: value}))

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(token)


@pytest.mark.parametrize(
    "purpose_claims",
    [
        {"typ": "ID"},
        {"token_use": "id", "typ": None},
        {"typ": None},
    ],
)
def test_api_bearer_rejects_id_or_undifferentiated_tokens(
    rsa_key_a: Any,
    purpose_claims: dict[str, object],
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    claims = _claims(**purpose_claims)
    if claims.get("typ") is None:
        claims.pop("typ", None)
    token = _rs256_token(rsa_key_a, kid="key-a", claims=claims)

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(token)


@pytest.mark.parametrize(
    "purpose_claims",
    [
        {"typ": "Bearer"},
        {"typ": None, "token_use": "access"},
    ],
)
def test_api_bearer_accepts_explicit_access_token_purpose(
    rsa_key_a: Any,
    purpose_claims: dict[str, object],
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    claims = _claims(**purpose_claims)
    if claims.get("typ") is None:
        claims.pop("typ", None)
    token = _rs256_token(rsa_key_a, kid="key-a", claims=claims)

    assert _validator(transport).validate(token).subject == "oidc-user-001"


def test_api_bearer_accepts_rfc9068_access_token_header(rsa_key_a: Any) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    claims = _claims()
    claims.pop("typ")
    token = _rs256_token(
        rsa_key_a,
        kid="key-a",
        claims=claims,
        header_type="at+jwt",
    )

    assert _validator(transport).validate(token).subject == "oidc-user-001"


@pytest.mark.parametrize("claim", ["iss", "sub", "aud", "exp"])
def test_rejects_missing_required_claim(rsa_key_a: Any, claim: str) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    claims = _claims()
    claims.pop(claim)
    token = _rs256_token(rsa_key_a, kid="key-a", claims=claims)

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(token)


def test_applies_expiration_clock_skew_leeway(rsa_key_a: Any) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    validator = _validator(transport, clock_skew_seconds=30)
    within_leeway = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(exp=NOW - 29))
    outside_leeway = _rs256_token(rsa_key_a, kid="key-a", claims=_claims(exp=NOW - 31))

    assert validator.validate(within_leeway).expires_at == NOW - 29
    with pytest.raises(OIDCTokenValidationError):
        validator.validate(outside_leeway)


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda key: _compact_token({"alg": "none", "kid": "key-a"}, _claims(), ""),
        lambda key: (
            JsonWebToken(["HS256"])
            .encode(
                {"alg": "HS256", "kid": "key-a", "typ": "JWT"},
                _claims(),
                b"attacker-controlled-shared-secret-value",
            )
            .decode("ascii")
        ),
    ],
)
def test_rejects_none_and_hmac_algorithm_confusion(rsa_key_a: Any, token_factory: Any) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})

    with pytest.raises(OIDCTokenValidationError):
        _validator(transport).validate(token_factory(rsa_key_a))

    assert transport.call_count(DISCOVERY_URL) == 0
    assert transport.call_count(JWKS_URL) == 0


def test_rejects_wrong_rs256_signature_without_exposing_token(
    rsa_key_a: Any,
    rsa_key_b: Any,
) -> None:
    transport = _transport({"keys": [_public_jwk(rsa_key_a)]})
    token = _rs256_token(rsa_key_b, kid="key-a")

    with pytest.raises(OIDCTokenValidationError) as captured:
        _validator(transport).validate(token)

    assert token not in str(captured.value)
    assert token not in repr(captured.value)


def test_caches_discovery_and_jwks_by_kid_until_ttl(rsa_key_a: Any) -> None:
    clock = MutableClock()
    transport = _transport(
        {"keys": [_public_jwk(rsa_key_a)]},
        {"keys": [_public_jwk(rsa_key_a)]},
    )
    validator = _validator(transport, clock=clock, cache_ttl_seconds=60)
    token = _rs256_token(rsa_key_a, kid="key-a")

    validator.validate(token)
    clock.value += 59
    validator.validate(token)

    assert transport.call_count(DISCOVERY_URL) == 1
    assert transport.call_count(JWKS_URL) == 1

    clock.value += 2
    validator.validate(token)

    assert transport.call_count(DISCOVERY_URL) == 2
    assert transport.call_count(JWKS_URL) == 2


def test_unknown_kid_forces_exactly_one_jwks_refresh(rsa_key_a: Any, rsa_key_b: Any) -> None:
    transport = _transport(
        {"keys": [_public_jwk(rsa_key_a)]},
        {"keys": [_public_jwk(rsa_key_a)]},
    )
    validator = _validator(transport)
    validator.validate(_rs256_token(rsa_key_a, kid="key-a"))

    with pytest.raises(OIDCTokenValidationError):
        validator.validate(_rs256_token(rsa_key_b, kid="key-b"))

    assert transport.call_count(DISCOVERY_URL) == 1
    assert transport.call_count(JWKS_URL) == 2


def test_unknown_kid_refresh_accepts_rotated_signing_key(rsa_key_a: Any, rsa_key_b: Any) -> None:
    transport = _transport(
        {"keys": [_public_jwk(rsa_key_a)]},
        {"keys": [_public_jwk(rsa_key_b)]},
    )
    validator = _validator(transport)
    validator.validate(_rs256_token(rsa_key_a, kid="key-a"))

    validated = validator.validate(_rs256_token(rsa_key_b, kid="key-b"))

    assert validated.subject == "oidc-user-001"
    assert transport.call_count(JWKS_URL) == 2


def test_rejects_discovery_document_with_non_exact_issuer(rsa_key_a: Any) -> None:
    transport = StubTransport(
        {
            DISCOVERY_URL: [{"issuer": f"{ISSUER}/", "jwks_uri": JWKS_URL}],
            JWKS_URL: [{"keys": [_public_jwk(rsa_key_a)]}],
        }
    )

    with pytest.raises(OIDCConfigurationError):
        _validator(transport).validate(_rs256_token(rsa_key_a, kid="key-a"))

    assert transport.call_count(JWKS_URL) == 0


def test_rejects_duplicate_kid_in_jwks(rsa_key_a: Any) -> None:
    public_key = _public_jwk(rsa_key_a)
    transport = _transport({"keys": [public_key, dict(public_key)]})

    with pytest.raises(OIDCConfigurationError):
        _validator(transport).validate(_rs256_token(rsa_key_a, kid="key-a"))


@pytest.mark.parametrize(
    "jwk_override",
    [
        {"alg": "PS256"},
        {"kty": "oct", "k": "c2hhcmVkLXNlY3JldA"},
        {"use": "enc"},
        {"key_ops": ["sign"]},
    ],
)
def test_rejects_jwks_keys_not_usable_for_rs256_verification(
    rsa_key_a: Any,
    jwk_override: Mapping[str, object],
) -> None:
    public_key = _public_jwk(rsa_key_a)
    public_key.update(jwk_override)
    transport = _transport({"keys": [public_key]})

    with pytest.raises(OIDCConfigurationError):
        _validator(transport).validate(_rs256_token(rsa_key_a, kid="key-a"))


@pytest.mark.parametrize("failure_url", [DISCOVERY_URL, JWKS_URL])
def test_network_failure_is_fail_closed(rsa_key_a: Any, failure_url: str) -> None:
    responses: dict[str, list[object]] = {
        DISCOVERY_URL: [{"issuer": ISSUER, "jwks_uri": JWKS_URL}],
        JWKS_URL: [{"keys": [_public_jwk(rsa_key_a)]}],
    }
    responses[failure_url] = [RuntimeError("network is unavailable")]
    transport = StubTransport(responses)

    with pytest.raises(OIDCProviderUnavailableError) as captured:
        _validator(transport).validate(_rs256_token(rsa_key_a, kid="key-a"))

    assert "network is unavailable" not in str(captured.value)


@pytest.mark.parametrize(
    "config_overrides",
    [
        {"issuer": ""},
        {"audience": ""},
        {"jwks_cache_ttl_seconds": 0},
        {"clock_skew_seconds": -1},
        {"clock_skew_seconds": 301},
        {"http_timeout_seconds": 0},
    ],
)
def test_rejects_unsafe_provider_configuration(config_overrides: Mapping[str, object]) -> None:
    config: dict[str, object] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "discovery_url": DISCOVERY_URL,
    }
    config.update(config_overrides)

    with pytest.raises(OIDCConfigurationError):
        OIDCProviderConfig(**config)  # type: ignore[arg-type]
