from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.oidc import (
    OIDCProviderConfig,
    OIDCProviderUnavailableError,
    OIDCTokenValidationError,
    OIDCValidatedClaims,
)
from app.core.oidc_flow import (
    OIDCAuthorizationDeniedError,
    OIDCAuthorizationFlow,
    OIDCAuthorizationResponseError,
    OIDCClientAuthentication,
    OIDCClientConfig,
    OIDCDiscoveryError,
    OIDCFlowConfigurationError,
    OIDCFlowHTTPResponse,
    OIDCTokenExchangeError,
    OIDCTokenResponseError,
)

ISSUER = "https://identity.example.test/realms/auris"
DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/protocol/openid-connect/auth"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
REDIRECT_URI = "https://app.example.test/api/v1/auth/oidc/callback"


def _json_response(
    payload: Mapping[str, object],
    *,
    status_code: int = 200,
) -> OIDCFlowHTTPResponse:
    return OIDCFlowHTTPResponse(
        status_code=status_code,
        content_type="application/json; charset=utf-8",
        body=json.dumps(dict(payload), separators=(",", ":")).encode(),
    )


def _discovery(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "issuer": ISSUER,
        "authorization_endpoint": AUTHORIZATION_ENDPOINT,
        "token_endpoint": TOKEN_ENDPOINT,
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    document.update(overrides)
    return document


class StubTransport:
    def __init__(
        self,
        *,
        discovery_response: OIDCFlowHTTPResponse | BaseException | None = None,
        token_response: OIDCFlowHTTPResponse | BaseException | None = None,
    ) -> None:
        self.discovery_response = discovery_response or _json_response(_discovery())
        self.token_response = token_response or _json_response(
            {
                "token_type": "Bearer",
                "access_token": "opaque-access-token",
                "id_token": "signed-id-token",
                "expires_in": 300,
            }
        )
        self.get_calls: list[tuple[str, float]] = []
        self.post_calls: list[
            tuple[str, Mapping[str, str], OIDCClientAuthentication | None, float]
        ] = []

    def get(self, url: str, *, timeout: float) -> OIDCFlowHTTPResponse:
        self.get_calls.append((url, timeout))
        if isinstance(self.discovery_response, BaseException):
            raise self.discovery_response
        return self.discovery_response

    def post_form(
        self,
        url: str,
        *,
        form: Mapping[str, str],
        client_authentication: OIDCClientAuthentication | None,
        timeout: float,
    ) -> OIDCFlowHTTPResponse:
        self.post_calls.append((url, form, client_authentication, timeout))
        if isinstance(self.token_response, BaseException):
            raise self.token_response
        return self.token_response


class StubTokenValidator:
    def __init__(self, claims: OIDCValidatedClaims | None = None) -> None:
        raw_claims: Mapping[str, Any] = MappingProxyType(
            {
                "iss": ISSUER,
                "sub": "oidc-user-001",
                "aud": "auris-flow-web",
                "exp": 1_900_000_000,
                "nonce": "expected-nonce",
            }
        )
        self.claims = claims or OIDCValidatedClaims(
            subject="oidc-user-001",
            issuer=ISSUER,
            audiences=("auris-flow-web",),
            expires_at=1_900_000_000,
            issued_at=1_800_000_000,
            claims=raw_claims,
        )
        self.tokens: list[str] = []

    def validate(self, token: str, *, now: float | None = None) -> OIDCValidatedClaims:
        assert now is None
        self.tokens.append(token)
        return self.claims


class DeterministicEntropy:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, size: int) -> bytes:
        self.calls.append(size)
        marker = len(self.calls)
        return bytes((index + marker) % 256 for index in range(size))


def _config(*, client_secret: str | None = None, scopes: tuple[str, ...] = ()) -> OIDCClientConfig:
    return OIDCClientConfig(
        provider=OIDCProviderConfig(
            issuer=ISSUER,
            audience="auris-flow-web",
            discovery_url=DISCOVERY_URL,
            http_timeout_seconds=2.5,
        ),
        client_id="auris-flow-web",
        redirect_uri=REDIRECT_URI,
        scopes=scopes,
        client_secret=client_secret,
    )


def _flow(
    *,
    transport: StubTransport | None = None,
    validator: StubTokenValidator | None = None,
    config: OIDCClientConfig | None = None,
    entropy: DeterministicEntropy | None = None,
) -> OIDCAuthorizationFlow:
    return OIDCAuthorizationFlow(
        config or _config(),
        token_validator=validator or StubTokenValidator(),
        transport=transport or StubTransport(),
        entropy=entropy or DeterministicEntropy(),
    )


def test_builds_authorization_code_request_with_pkce_s256_and_openid_scope() -> None:
    entropy = DeterministicEntropy()
    transport = StubTransport()
    request = _flow(
        transport=transport,
        entropy=entropy,
        config=_config(scopes=("profile", "email", "profile")),
    ).create_authorization_request()

    parsed = urlsplit(request.authorization_url)
    query = parse_qs(parsed.query, strict_parsing=True)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_ENDPOINT
    assert query == {
        "client_id": ["auris-flow-web"],
        "code_challenge": [request.code_challenge],
        "code_challenge_method": ["S256"],
        "nonce": [request.nonce],
        "redirect_uri": [REDIRECT_URI],
        "response_type": ["code"],
        "scope": ["openid profile email"],
        "state": [request.state],
    }
    assert request.state != request.nonce
    assert 43 <= len(request.state) <= 128
    assert 43 <= len(request.nonce) <= 128
    assert 43 <= len(request.code_verifier) <= 128
    assert len(request.code_challenge) == 43
    assert entropy.calls == [32, 32, 64]
    assert transport.get_calls == [(DISCOVERY_URL, 2.5)]


def test_sensitive_values_are_redacted_from_repr() -> None:
    canary_value = "client-secret-must-never-be-rendered"
    config = _config(client_secret=canary_value)
    request = _flow(config=config).create_authorization_request()

    assert canary_value not in repr(config)
    assert "<redacted>" in repr(config)
    assert request.state not in repr(request)
    assert request.nonce not in repr(request)
    assert request.code_verifier not in repr(request)


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": f"{ISSUER}/"},
        {"authorization_endpoint": "http://identity.example.test/authorize"},
        {"authorization_endpoint": "https://attacker.example.test/authorize"},
        {"authorization_endpoint": "https://user:password@identity.example.test/auth"},
        {"authorization_endpoint": f"{AUTHORIZATION_ENDPOINT}#fragment"},
        {"token_endpoint": "http://identity.example.test/token"},
        {"token_endpoint": "https://attacker.example.test/token"},
        {"response_types_supported": ["token"]},
        {"code_challenge_methods_supported": ["plain"]},
        {"id_token_signing_alg_values_supported": ["HS256"]},
    ],
)
def test_rejects_discovery_metadata_that_is_not_exact_https_same_origin(
    overrides: dict[str, object],
) -> None:
    transport = StubTransport(discovery_response=_json_response(_discovery(**overrides)))

    with pytest.raises(OIDCDiscoveryError) as exc_info:
        _flow(transport=transport).create_authorization_request()

    assert exc_info.value.code == "OIDC_DISCOVERY_INVALID"


def test_rejects_duplicate_or_reserved_query_parameters_in_discovered_endpoint() -> None:
    for endpoint in (
        f"{AUTHORIZATION_ENDPOINT}?ui_locales=en&ui_locales=zh",
        f"{AUTHORIZATION_ENDPOINT}?state=provider-controlled",
    ):
        transport = StubTransport(
            discovery_response=_json_response(_discovery(authorization_endpoint=endpoint))
        )

        with pytest.raises(OIDCDiscoveryError):
            _flow(transport=transport).create_authorization_request()


def test_rejects_non_json_duplicate_json_and_network_discovery_responses() -> None:
    responses = (
        OIDCFlowHTTPResponse(200, "text/html", b"<html>login</html>"),
        OIDCFlowHTTPResponse(
            200,
            "application/json",
            (
                b'{"issuer":"https://identity.example.test/realms/auris",'
                b'"issuer":"https://attacker.example.test"}'
            ),
        ),
    )
    for response in responses:
        with pytest.raises(OIDCDiscoveryError):
            _flow(transport=StubTransport(discovery_response=response)).discover()

    with pytest.raises(OIDCProviderUnavailableError) as exc_info:
        _flow(
            transport=StubTransport(discovery_response=TimeoutError("sensitive host detail"))
        ).discover()
    assert exc_info.value.code == "OIDC_PROVIDER_UNAVAILABLE"
    assert "sensitive host detail" not in str(exc_info.value)


def test_parses_callback_with_constant_time_state_and_optional_exact_issuer() -> None:
    callback = _flow().parse_authorization_response(
        "code=authorization-code&state=expected-state&"
        "iss=https%3A%2F%2Fidentity.example.test%2Frealms%2Fauris&session_state=opaque",
        expected_state="expected-state",
    )

    assert callback.code == "authorization-code"
    assert callback.state == "expected-state"
    assert callback.issuer == ISSUER
    assert callback.code not in repr(callback)


@pytest.mark.parametrize(
    "query",
    [
        "code=one&code=two&state=expected-state",
        "code=value&state=one&state=two",
        "code=value&st%61te=expected-state&state=expected-state",
        "code=&state=expected-state",
        "code=value",
        "state=expected-state",
        "code=value&state=wrong-state",
        "code=value&state=expected-state&iss=https%3A%2F%2Fattacker.example.test",
        "code=value&state=expected-state%ZZ",
        "code=authorization%00code&state=expected-state",
        "code=value&state=expected-state#fragment",
        "error=access_denied&code=value&state=expected-state",
    ],
)
def test_rejects_malformed_duplicate_or_mixed_authorization_response(query: str) -> None:
    with pytest.raises(OIDCAuthorizationResponseError) as exc_info:
        _flow().parse_authorization_response(query, expected_state="expected-state")

    assert exc_info.value.code == "OIDC_AUTHORIZATION_RESPONSE_INVALID"


def test_maps_provider_authorization_error_to_stable_non_sensitive_error() -> None:
    query = (
        "error=access_denied&state=expected-state&"
        "error_description=client-secret-must-not-be-returned"
    )

    with pytest.raises(OIDCAuthorizationDeniedError) as exc_info:
        _flow().parse_authorization_response(query, expected_state="expected-state")

    assert exc_info.value.code == "OIDC_AUTHORIZATION_DENIED"
    assert "client-secret" not in str(exc_info.value)


def test_exchanges_code_and_validates_id_token_nonce() -> None:
    transport = StubTransport()
    validator = StubTokenValidator()
    token_set = _flow(transport=transport, validator=validator).exchange_code(
        "authorization-code",
        code_verifier="A" * 43,
        expected_nonce="expected-nonce",
    )

    assert token_set.access_token == "opaque-access-token"
    assert token_set.id_token == "signed-id-token"
    assert token_set.token_type == "Bearer"
    assert token_set.expires_in == 300
    assert token_set.claims.subject == "oidc-user-001"
    assert validator.tokens == ["signed-id-token"]
    assert len(transport.post_calls) == 1
    url, form, authentication, timeout = transport.post_calls[0]
    assert url == TOKEN_ENDPOINT
    assert dict(form) == {
        "client_id": "auris-flow-web",
        "code": "authorization-code",
        "code_verifier": "A" * 43,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    assert authentication is None
    assert timeout == 2.5
    assert "opaque-access-token" not in repr(token_set)
    assert "signed-id-token" not in repr(token_set)


def test_uses_redacted_basic_client_authentication_when_secret_is_configured() -> None:
    canary_value = "client-secret-must-never-be-rendered"
    transport = StubTransport()

    _flow(transport=transport, config=_config(client_secret=canary_value)).exchange_code(
        "authorization-code",
        code_verifier="B" * 43,
        expected_nonce="expected-nonce",
    )

    _url, form, authentication, _timeout = transport.post_calls[0]
    assert "client_id" not in form
    assert authentication is not None
    assert authentication.client_id == "auris-flow-web"
    assert authentication.client_secret == canary_value
    assert canary_value not in repr(authentication)


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "access", "id_token": "id"},
        {"token_type": "MAC", "access_token": "access", "id_token": "id"},
        {"token_type": "Bearer", "id_token": "id"},
        {"token_type": "Bearer", "access_token": "access"},
        {
            "token_type": "Bearer",
            "access_token": "access",
            "id_token": "id",
            "expires_in": True,
        },
    ],
)
def test_rejects_incomplete_or_invalid_token_response(payload: Mapping[str, object]) -> None:
    transport = StubTransport(token_response=_json_response(payload))

    with pytest.raises(OIDCTokenResponseError) as exc_info:
        _flow(transport=transport).exchange_code(
            "authorization-code",
            code_verifier="C" * 43,
            expected_nonce="expected-nonce",
        )

    assert exc_info.value.code == "OIDC_TOKEN_RESPONSE_INVALID"


def test_rejects_token_response_wrong_content_type_duplicate_keys_http_error_or_network() -> None:
    responses: tuple[OIDCFlowHTTPResponse | BaseException, ...] = (
        OIDCFlowHTTPResponse(200, "text/html", b"not-json"),
        OIDCFlowHTTPResponse(
            200,
            "application/json",
            b'{"token_type":"Bearer","access_token":"one","access_token":"two"}',
        ),
        _json_response({"error": "invalid_grant"}, status_code=400),
        TimeoutError("client-secret-must-not-leak"),
    )
    expected_errors = (
        OIDCTokenResponseError,
        OIDCTokenResponseError,
        OIDCTokenExchangeError,
        OIDCProviderUnavailableError,
    )

    for response, expected_error in zip(responses, expected_errors, strict=True):
        with pytest.raises(expected_error) as exc_info:
            _flow(transport=StubTransport(token_response=response)).exchange_code(
                "authorization-code",
                code_verifier="D" * 43,
                expected_nonce="expected-nonce",
            )
        assert "client-secret" not in str(exc_info.value)


def test_http_response_repr_does_not_render_token_body() -> None:
    canary_value = "access-token-must-not-be-rendered"
    response = OIDCFlowHTTPResponse(200, "application/json", canary_value.encode())

    assert canary_value not in repr(response)
    assert "<redacted" in repr(response)


def test_rejects_nonce_mismatch_after_signed_token_validation() -> None:
    claims = OIDCValidatedClaims(
        subject="oidc-user-001",
        issuer=ISSUER,
        audiences=("auris-flow-web",),
        expires_at=1_900_000_000,
        issued_at=None,
        claims=MappingProxyType({"nonce": "different-nonce"}),
    )

    with pytest.raises(OIDCTokenValidationError) as exc_info:
        _flow(validator=StubTokenValidator(claims)).exchange_code(
            "authorization-code",
            code_verifier="E" * 43,
            expected_nonce="expected-nonce",
        )

    assert exc_info.value.code == "OIDC_TOKEN_INVALID"


@pytest.mark.parametrize(
    ("client_id", "redirect_uri", "scopes", "client_secret"),
    [
        ("", REDIRECT_URI, (), None),
        (" client ", REDIRECT_URI, (), None),
        ("client", "http://app.example.test/callback", (), None),
        ("client", "https://user:password@app.example.test/callback", (), None),
        ("client", f"{REDIRECT_URI}#fragment", (), None),
        ("client", REDIRECT_URI, ("profile email",), None),
        ("client", REDIRECT_URI, (), ""),
    ],
)
def test_rejects_unsafe_client_configuration(
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    client_secret: str | None,
) -> None:
    with pytest.raises(OIDCFlowConfigurationError) as exc_info:
        OIDCClientConfig(
            provider=OIDCProviderConfig(issuer=ISSUER, audience="audience"),
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            client_secret=client_secret,
        )

    assert exc_info.value.code == "OIDC_FLOW_CONFIGURATION_INVALID"


def test_allows_http_redirect_only_for_loopback_development_clients() -> None:
    config = OIDCClientConfig(
        provider=OIDCProviderConfig(issuer=ISSUER, audience="audience"),
        client_id="client",
        redirect_uri="http://127.0.0.1:8765/callback",
    )

    assert config.redirect_uri == "http://127.0.0.1:8765/callback"


def test_rejects_oidc_issuer_with_query_component() -> None:
    provider = OIDCProviderConfig(
        issuer=f"{ISSUER}?tenant=attacker-controlled",
        audience="audience",
    )

    with pytest.raises(OIDCFlowConfigurationError):
        OIDCClientConfig(
            provider=provider,
            client_id="client",
            redirect_uri=REDIRECT_URI,
        )
