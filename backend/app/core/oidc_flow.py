from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

from app.core.oidc import (
    OIDCError,
    OIDCProviderConfig,
    OIDCProviderUnavailableError,
    OIDCTokenValidationError,
    OIDCValidatedClaims,
)
from app.core.oidc_http import OIDCHTTPResponseLimitError, read_bounded_httpx_body

_AUTHORIZATION_RESERVED_PARAMETERS = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "nonce",
        "redirect_uri",
        "response_type",
        "scope",
        "state",
    }
)
_TOKEN_RESERVED_PARAMETERS = frozenset(
    {"client_id", "client_secret", "code", "code_verifier", "grant_type", "redirect_uri"}
)
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_SCOPE = re.compile(r"^[\x21\x23-\x5B\x5D-\x7E]+$")
_MAX_ENDPOINT_BYTES = 4096
_MAX_DISCOVERY_BYTES = 128 * 1024
_MAX_TOKEN_RESPONSE_BYTES = 128 * 1024
_MAX_CALLBACK_BYTES = 16 * 1024
_MAX_TOKEN_BYTES = 16 * 1024


class OIDCFlowError(OIDCError):
    code = "OIDC_FLOW_ERROR"
    public_message = "OIDC authorization flow failed"


class OIDCFlowConfigurationError(OIDCFlowError):
    code = "OIDC_FLOW_CONFIGURATION_INVALID"
    public_message = "OIDC client configuration is invalid"


class OIDCDiscoveryError(OIDCFlowError):
    code = "OIDC_DISCOVERY_INVALID"
    public_message = "OIDC discovery metadata is invalid"


class OIDCAuthorizationResponseError(OIDCFlowError):
    code = "OIDC_AUTHORIZATION_RESPONSE_INVALID"
    public_message = "OIDC authorization response is invalid"


class OIDCAuthorizationDeniedError(OIDCAuthorizationResponseError):
    code = "OIDC_AUTHORIZATION_DENIED"
    public_message = "OIDC authorization was denied"


class OIDCTokenExchangeError(OIDCFlowError):
    code = "OIDC_TOKEN_EXCHANGE_FAILED"
    public_message = "OIDC token exchange failed"


class OIDCTokenResponseError(OIDCFlowError):
    code = "OIDC_TOKEN_RESPONSE_INVALID"
    public_message = "OIDC token response is invalid"


@dataclass(frozen=True, repr=False)
class OIDCFlowHTTPResponse:
    status_code: int
    content_type: str
    body: bytes

    def __repr__(self) -> str:
        body_length = len(self.body) if isinstance(self.body, bytes) else "invalid"
        return (
            "OIDCFlowHTTPResponse("
            f"status_code={self.status_code!r}, content_type={self.content_type!r}, "
            f"body=<redacted {body_length} bytes>)"
        )


@dataclass(frozen=True, repr=False)
class OIDCClientConfig:
    provider: OIDCProviderConfig
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = ()
    client_secret: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, OIDCProviderConfig):
            raise OIDCFlowConfigurationError
        if not _is_nonempty_bounded_text(self.client_id, maximum=512, strip_exact=True):
            raise OIDCFlowConfigurationError
        _validate_redirect_uri(self.redirect_uri)
        if not isinstance(self.scopes, tuple) or len(self.scopes) > 32:
            raise OIDCFlowConfigurationError
        if any(not _is_valid_scope(scope) for scope in self.scopes):
            raise OIDCFlowConfigurationError
        if self.client_secret is not None:
            if (
                not isinstance(self.client_secret, str)
                or not self.client_secret
                or len(self.client_secret.encode("utf-8")) > 4096
                or _contains_control(self.client_secret)
            ):
                raise OIDCFlowConfigurationError
        issuer = urlsplit(self.provider.issuer)
        if issuer.scheme != "https" or issuer.query or issuer.fragment:
            raise OIDCFlowConfigurationError

    def __repr__(self) -> str:
        secret = "<redacted>" if self.client_secret is not None else "None"
        return (
            "OIDCClientConfig("
            f"provider={self.provider!r}, client_id={self.client_id!r}, "
            f"redirect_uri={self.redirect_uri!r}, scopes={self.scopes!r}, "
            f"client_secret={secret})"
        )


@dataclass(frozen=True, repr=False)
class OIDCClientAuthentication:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"OIDCClientAuthentication(client_id={self.client_id!r}, client_secret=<redacted>)"


@dataclass(frozen=True, repr=False)
class OIDCAuthorizationRequest:
    authorization_url: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str

    def __repr__(self) -> str:
        return "OIDCAuthorizationRequest(<sensitive values redacted>)"


@dataclass(frozen=True, repr=False)
class OIDCAuthorizationCallback:
    code: str
    state: str
    issuer: str | None

    def __repr__(self) -> str:
        return "OIDCAuthorizationCallback(<sensitive values redacted>)"


@dataclass(frozen=True, repr=False)
class OIDCTokenSet:
    access_token: str
    id_token: str
    token_type: str
    expires_in: int | None
    refresh_token: str | None
    scope: str | None
    claims: OIDCValidatedClaims

    def __repr__(self) -> str:
        return (
            "OIDCTokenSet("
            f"token_type={self.token_type!r}, expires_in={self.expires_in!r}, "
            f"subject={self.claims.subject!r}, tokens=<redacted>)"
        )


@dataclass(frozen=True)
class OIDCDiscoveryMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str


@dataclass(frozen=True)
class _DiscoveryCache:
    metadata: OIDCDiscoveryMetadata
    expires_at: float


class OIDCFlowHTTPTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> OIDCFlowHTTPResponse:
        """Fetch a response without following redirects."""

    def post_form(
        self,
        url: str,
        *,
        form: Mapping[str, str],
        client_authentication: OIDCClientAuthentication | None,
        timeout: float,
    ) -> OIDCFlowHTTPResponse:
        """POST an application/x-www-form-urlencoded request without redirects."""


class OIDCIDTokenValidator(Protocol):
    def validate(self, token: str, *, now: float | None = None) -> OIDCValidatedClaims:
        """Validate an ID token using the configured issuer, audience, and JWKS."""


class HTTPXOIDCFlowTransport:
    """Default network adapter with redirects disabled and non-sensitive failures."""

    def get(self, url: str, *, timeout: float) -> OIDCFlowHTTPResponse:
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"Accept": "application/json"},
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                return _httpx_response(response, maximum_bytes=_MAX_DISCOVERY_BYTES)
        except OIDCHTTPResponseLimitError:
            raise OIDCDiscoveryError from None

    def post_form(
        self,
        url: str,
        *,
        form: Mapping[str, str],
        client_authentication: OIDCClientAuthentication | None,
        timeout: float,
    ) -> OIDCFlowHTTPResponse:
        authentication: httpx.BasicAuth | None = None
        if client_authentication is not None:
            authentication = httpx.BasicAuth(
                client_authentication.client_id,
                client_authentication.client_secret,
            )
        try:
            with httpx.stream(
                "POST",
                url,
                data=form,
                headers={"Accept": "application/json"},
                auth=authentication,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                return _httpx_response(
                    response,
                    maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES,
                )
        except OIDCHTTPResponseLimitError:
            raise OIDCTokenResponseError from None


class _SensitiveForm(Mapping[str, str]):
    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"OIDCTokenForm(keys={tuple(self._values)}, values=<redacted>)"


class _DuplicateJSONKeyError(ValueError):
    pass


class OIDCAuthorizationFlow:
    """Pure OIDC Authorization Code + PKCE client flow.

    State persistence, one-time consumption, browser cookies, and user mapping belong to
    the BFF composition layer. This class keeps protocol work independently testable.
    """

    def __init__(
        self,
        config: OIDCClientConfig,
        *,
        token_validator: OIDCIDTokenValidator,
        transport: OIDCFlowHTTPTransport | None = None,
        entropy: Callable[[int], bytes] = secrets.token_bytes,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._token_validator = token_validator
        self._transport = transport or HTTPXOIDCFlowTransport()
        self._entropy = entropy
        self._clock = clock
        self._lock = RLock()
        self._discovery: _DiscoveryCache | None = None

    def discover(self, *, force_refresh: bool = False) -> OIDCDiscoveryMetadata:
        now = self._current_time()
        with self._lock:
            if (
                not force_refresh
                and self._discovery is not None
                and self._discovery.expires_at > now
            ):
                return self._discovery.metadata
            discovery_url = self.config.provider.discovery_url
            if discovery_url is None:
                raise OIDCFlowConfigurationError
            response = self._get(discovery_url)
            if response.status_code != 200:
                raise OIDCProviderUnavailableError
            document = _parse_json_object(
                response,
                maximum_bytes=_MAX_DISCOVERY_BYTES,
                error_type=OIDCDiscoveryError,
            )
            metadata = self._validate_discovery(document)
            self._discovery = _DiscoveryCache(
                metadata=metadata,
                expires_at=now + self.config.provider.jwks_cache_ttl_seconds,
            )
            return metadata

    def create_authorization_request(self) -> OIDCAuthorizationRequest:
        metadata = self.discover()
        state = self._random_urlsafe(32)
        nonce = self._random_urlsafe(32)
        code_verifier = self._random_urlsafe(64)
        if not _PKCE_VERIFIER.fullmatch(code_verifier):
            raise OIDCFlowConfigurationError
        code_challenge = _base64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
        scopes = _deduplicate(("openid", *self.config.scopes))
        parameters = (
            ("response_type", "code"),
            ("client_id", self.config.client_id),
            ("redirect_uri", self.config.redirect_uri),
            ("scope", " ".join(scopes)),
            ("state", state),
            ("nonce", nonce),
            ("code_challenge", code_challenge),
            ("code_challenge_method", "S256"),
        )
        authorization_url = _append_query(metadata.authorization_endpoint, parameters)
        return OIDCAuthorizationRequest(
            authorization_url=authorization_url,
            state=state,
            nonce=nonce,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )

    def parse_authorization_response(
        self,
        query: str,
        *,
        expected_state: str,
    ) -> OIDCAuthorizationCallback:
        if (
            not _is_nonempty_bounded_text(expected_state, maximum=512)
            or not isinstance(query, str)
            or not query
            or len(query.encode("utf-8")) > _MAX_CALLBACK_BYTES
            or "#" in query
            or _contains_control(query)
        ):
            raise OIDCAuthorizationResponseError
        raw_query = query[1:] if query.startswith("?") else query
        if not raw_query or not _has_valid_percent_encoding(raw_query):
            raise OIDCAuthorizationResponseError
        try:
            pairs = parse_qsl(
                raw_query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=32,
            )
        except (UnicodeError, ValueError):
            raise OIDCAuthorizationResponseError from None
        parameters = _unique_parameters(pairs, OIDCAuthorizationResponseError)
        returned_state = parameters.get("state")
        if not _is_nonempty_bounded_text(returned_state, maximum=512) or not hmac.compare_digest(
            cast(str, returned_state).encode("utf-8"),
            expected_state.encode("utf-8"),
        ):
            raise OIDCAuthorizationResponseError
        returned_issuer = parameters.get("iss")
        if returned_issuer is not None and returned_issuer != self.config.provider.issuer:
            raise OIDCAuthorizationResponseError
        error = parameters.get("error")
        code = parameters.get("code")
        if error is not None:
            if code is not None or not _is_nonempty_bounded_text(error, maximum=256):
                raise OIDCAuthorizationResponseError
            raise OIDCAuthorizationDeniedError
        if not _is_nonempty_bounded_text(code, maximum=8192):
            raise OIDCAuthorizationResponseError
        return OIDCAuthorizationCallback(
            code=cast(str, code),
            state=cast(str, returned_state),
            issuer=returned_issuer,
        )

    def exchange_code(
        self,
        code: str,
        *,
        code_verifier: str,
        expected_nonce: str,
    ) -> OIDCTokenSet:
        if (
            not _is_nonempty_bounded_text(code, maximum=8192)
            or _contains_control(code)
            or not isinstance(code_verifier, str)
            or not _PKCE_VERIFIER.fullmatch(code_verifier)
            or not _is_nonempty_bounded_text(expected_nonce, maximum=512)
            or _contains_control(expected_nonce)
        ):
            raise OIDCTokenExchangeError
        metadata = self.discover()
        form_values = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "code_verifier": code_verifier,
        }
        client_authentication: OIDCClientAuthentication | None = None
        if self.config.client_secret is None:
            form_values["client_id"] = self.config.client_id
        else:
            client_authentication = OIDCClientAuthentication(
                client_id=self.config.client_id,
                client_secret=self.config.client_secret,
            )
        response = self._post_form(
            metadata.token_endpoint,
            form=_SensitiveForm(form_values),
            client_authentication=client_authentication,
        )
        if response.status_code != 200:
            raise OIDCTokenExchangeError
        document = _parse_json_object(
            response,
            maximum_bytes=_MAX_TOKEN_RESPONSE_BYTES,
            error_type=OIDCTokenResponseError,
        )
        if "error" in document:
            raise OIDCTokenExchangeError
        token_type = document.get("token_type")
        access_token = document.get("access_token")
        id_token = document.get("id_token")
        expires_in = document.get("expires_in")
        refresh_token = document.get("refresh_token")
        scope = document.get("scope")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise OIDCTokenResponseError
        if not _is_token(access_token) or not _is_token(id_token):
            raise OIDCTokenResponseError
        if expires_in is not None and (
            isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0
        ):
            raise OIDCTokenResponseError
        if refresh_token is not None and not _is_token(refresh_token):
            raise OIDCTokenResponseError
        if scope is not None and (
            not isinstance(scope, str)
            or not scope
            or len(scope.encode("utf-8")) > 4096
            or _contains_control(scope)
        ):
            raise OIDCTokenResponseError
        claims = self._validate_id_token(cast(str, id_token))
        nonce = claims.claims.get("nonce")
        if not _is_nonempty_bounded_text(nonce, maximum=512) or not hmac.compare_digest(
            cast(str, nonce).encode("utf-8"),
            expected_nonce.encode("utf-8"),
        ):
            raise OIDCTokenValidationError
        return OIDCTokenSet(
            access_token=cast(str, access_token),
            id_token=cast(str, id_token),
            token_type=token_type,
            expires_in=expires_in,
            refresh_token=cast(str | None, refresh_token),
            scope=scope,
            claims=claims,
        )

    def _validate_discovery(self, document: Mapping[str, Any]) -> OIDCDiscoveryMetadata:
        if document.get("issuer") != self.config.provider.issuer:
            raise OIDCDiscoveryError
        authorization_endpoint = document.get("authorization_endpoint")
        token_endpoint = document.get("token_endpoint")
        if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
            raise OIDCDiscoveryError
        _validate_same_origin_https_endpoint(
            authorization_endpoint,
            issuer=self.config.provider.issuer,
            reserved_parameters=_AUTHORIZATION_RESERVED_PARAMETERS,
        )
        _validate_same_origin_https_endpoint(
            token_endpoint,
            issuer=self.config.provider.issuer,
            reserved_parameters=_TOKEN_RESERVED_PARAMETERS,
        )
        if not _metadata_list_contains(document, "response_types_supported", "code"):
            raise OIDCDiscoveryError
        if not _metadata_list_contains(
            document,
            "code_challenge_methods_supported",
            "S256",
        ):
            raise OIDCDiscoveryError
        if not _metadata_list_contains(
            document,
            "id_token_signing_alg_values_supported",
            "RS256",
        ):
            raise OIDCDiscoveryError
        return OIDCDiscoveryMetadata(
            issuer=self.config.provider.issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
        )

    def _random_urlsafe(self, size: int) -> str:
        try:
            raw = self._entropy(size)
        except Exception:
            raise OIDCFlowConfigurationError from None
        if not isinstance(raw, bytes) or len(raw) != size:
            raise OIDCFlowConfigurationError
        return _base64url(raw)

    def _current_time(self) -> float:
        try:
            now = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            raise OIDCFlowConfigurationError from None
        if not math.isfinite(now) or now < 0:
            raise OIDCFlowConfigurationError
        return now

    def _get(self, url: str) -> OIDCFlowHTTPResponse:
        try:
            response = self._transport.get(
                url,
                timeout=float(self.config.provider.http_timeout_seconds),
            )
        except OIDCError:
            raise
        except Exception:
            raise OIDCProviderUnavailableError from None
        if not isinstance(response, OIDCFlowHTTPResponse):
            raise OIDCProviderUnavailableError
        return response

    def _post_form(
        self,
        url: str,
        *,
        form: Mapping[str, str],
        client_authentication: OIDCClientAuthentication | None,
    ) -> OIDCFlowHTTPResponse:
        try:
            response = self._transport.post_form(
                url,
                form=form,
                client_authentication=client_authentication,
                timeout=float(self.config.provider.http_timeout_seconds),
            )
        except OIDCError:
            raise
        except Exception:
            raise OIDCProviderUnavailableError from None
        if not isinstance(response, OIDCFlowHTTPResponse):
            raise OIDCProviderUnavailableError
        return response

    def _validate_id_token(self, token: str) -> OIDCValidatedClaims:
        try:
            return self._token_validator.validate(token)
        except OIDCError:
            raise
        except Exception:
            raise OIDCTokenValidationError from None


def _httpx_response(
    response: httpx.Response,
    *,
    maximum_bytes: int,
) -> OIDCFlowHTTPResponse:
    return OIDCFlowHTTPResponse(
        status_code=response.status_code,
        content_type=response.headers.get("content-type", ""),
        body=read_bounded_httpx_body(response, maximum_bytes=maximum_bytes),
    )


def _parse_json_object(
    response: OIDCFlowHTTPResponse,
    *,
    maximum_bytes: int,
    error_type: type[OIDCFlowError],
) -> Mapping[str, Any]:
    if (
        isinstance(response.status_code, bool)
        or not isinstance(response.status_code, int)
        or not isinstance(response.content_type, str)
        or not _is_json_content_type(response.content_type)
        or not isinstance(response.body, bytes)
        or not response.body
        or len(response.body) > maximum_bytes
    ):
        raise error_type
    try:
        payload = json.loads(
            response.body,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJSONKeyError, ValueError):
        raise error_type from None
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise error_type
    return cast(Mapping[str, Any], payload)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKeyError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _is_json_content_type(value: str) -> bool:
    media_type = value.split(";", 1)[0].strip().lower()
    if "/" not in media_type:
        return False
    category, subtype = media_type.split("/", 1)
    return category == "application" and (subtype == "json" or subtype.endswith("+json"))


def _validate_same_origin_https_endpoint(
    endpoint: str,
    *,
    issuer: str,
    reserved_parameters: frozenset[str],
) -> None:
    if (
        not endpoint
        or len(endpoint.encode("utf-8")) > _MAX_ENDPOINT_BYTES
        or _contains_control(endpoint)
        or "\\" in endpoint
        or not _has_valid_percent_encoding(endpoint)
    ):
        raise OIDCDiscoveryError
    try:
        parsed = urlsplit(endpoint)
        issuer_parsed = urlsplit(issuer)
        _endpoint_port = parsed.port
        _issuer_port = issuer_parsed.port
    except ValueError:
        raise OIDCDiscoveryError from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _origin(parsed) != _origin(issuer_parsed)
    ):
        raise OIDCDiscoveryError
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        raise OIDCDiscoveryError from None
    parameters = _unique_parameters(pairs, OIDCDiscoveryError)
    if reserved_parameters.intersection(parameters):
        raise OIDCDiscoveryError


def _origin(parsed: Any) -> tuple[str, str, int]:
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    hostname = parsed.hostname
    if not isinstance(hostname, str):
        raise OIDCDiscoveryError
    return parsed.scheme.lower(), hostname.lower(), port


def _append_query(endpoint: str, parameters: Sequence[tuple[str, str]]) -> str:
    parsed = urlsplit(endpoint)
    try:
        existing = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        raise OIDCDiscoveryError from None
    query = urlencode((*existing, *parameters), quote_via=quote, safe="~")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _unique_parameters(
    pairs: Sequence[tuple[str, str]],
    error_type: type[OIDCFlowError],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in pairs:
        if not name or name in result or _contains_control(name) or _contains_control(value):
            raise error_type
        result[name] = value
    return result


def _metadata_list_contains(
    document: Mapping[str, Any],
    field: str,
    required_value: str,
) -> bool:
    values = document.get(field)
    return (
        isinstance(values, list)
        and bool(values)
        and all(isinstance(value, str) and value for value in values)
        and required_value in values
    )


def _validate_redirect_uri(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > _MAX_ENDPOINT_BYTES
        or _contains_control(value)
        or "\\" in value
        or not _has_valid_percent_encoding(value)
    ):
        raise OIDCFlowConfigurationError
    try:
        parsed = urlsplit(value)
        _port = parsed.port
    except ValueError:
        raise OIDCFlowConfigurationError from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OIDCFlowConfigurationError
    if parsed.scheme == "https":
        return
    if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
        raise OIDCFlowConfigurationError


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized == "::1":
        return True
    parts = normalized.split(".")
    if len(parts) != 4:
        return False
    try:
        octets = tuple(int(part) for part in parts)
    except ValueError:
        return False
    return (
        all(0 <= octet <= 255 for octet in octets)
        and octets[0] == 127
        and all(part == str(octet) for part, octet in zip(parts, octets, strict=True))
    )


def _is_valid_scope(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= 256
        and bool(_SCOPE.fullmatch(value))
    )


def _is_nonempty_bounded_text(
    value: object,
    *,
    maximum: int,
    strip_exact: bool = False,
) -> bool:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        return False
    return not strip_exact or value == value.strip()


def _is_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= _MAX_TOKEN_BYTES
        and not _contains_control(value)
    )


def _contains_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _has_valid_percent_encoding(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value):
            return False
        if value[index + 1] not in "0123456789abcdefABCDEF":
            return False
        if value[index + 2] not in "0123456789abcdefABCDEF":
            return False
        index += 3
    return True


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
