from __future__ import annotations

import hmac
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import httpx
from authlib.jose import JsonWebKey, JsonWebToken  # type: ignore[import-untyped]

from app.core.oidc_http import OIDCHTTPResponseLimitError, read_bounded_httpx_body

_ALGORITHM = "RS256"
_ACCESS_TOKEN_HEADER_TYPES = frozenset({"at+jwt", "application/at+jwt"})
_ACCESS_TOKEN_CLAIM_TYPES = frozenset({"access", "access_token", "bearer"})
_MAX_JWKS_KEYS = 100
_MAX_TOKEN_BYTES = 16 * 1024
_MAX_DISCOVERY_RESPONSE_BYTES = 128 * 1024
_MAX_JWKS_RESPONSE_BYTES = 512 * 1024
_PRIVATE_RSA_PARAMETERS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
_BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"
_MAX_OIDC_IDENTIFIER_LENGTH = 512
_MAX_BACKCHANNEL_LOGOUT_LIFETIME_SECONDS = 300


class OIDCError(RuntimeError):
    """Base error with a stable code and a non-sensitive public message."""

    code = "OIDC_ERROR"
    public_message = "OIDC operation failed"

    def __init__(self) -> None:
        super().__init__(self.public_message)


class OIDCConfigurationError(OIDCError):
    code = "OIDC_CONFIGURATION_INVALID"
    public_message = "OIDC provider configuration is invalid"


class OIDCProviderUnavailableError(OIDCError):
    code = "OIDC_PROVIDER_UNAVAILABLE"
    public_message = "OIDC provider is unavailable"


class OIDCTokenValidationError(OIDCError):
    code = "OIDC_TOKEN_INVALID"
    public_message = "OIDC token is invalid"


@dataclass(frozen=True)
class OIDCProviderConfig:
    """Security-sensitive values supplied by application settings at composition time."""

    issuer: str
    audience: str
    discovery_url: str | None = None
    jwks_cache_ttl_seconds: int = 300
    clock_skew_seconds: int = 30
    http_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.issuer or self.issuer != self.issuer.strip():
            raise OIDCConfigurationError
        if not self.audience or self.audience != self.audience.strip():
            raise OIDCConfigurationError
        _validate_absolute_http_url(self.issuer)
        discovery_url = self.discovery_url or (
            f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        )
        _validate_absolute_http_url(discovery_url)
        if _uses_https(self.issuer) and not _uses_https(discovery_url):
            raise OIDCConfigurationError
        if not 1 <= self.jwks_cache_ttl_seconds <= 86_400:
            raise OIDCConfigurationError
        if not 0 <= self.clock_skew_seconds <= 300:
            raise OIDCConfigurationError
        if (
            isinstance(self.http_timeout_seconds, bool)
            or not isinstance(self.http_timeout_seconds, (int, float))
            or not math.isfinite(self.http_timeout_seconds)
            or not 0 < self.http_timeout_seconds <= 30
        ):
            raise OIDCConfigurationError
        object.__setattr__(self, "discovery_url", discovery_url)


@dataclass(frozen=True)
class OIDCValidatedClaims:
    subject: str
    issuer: str
    audiences: tuple[str, ...]
    expires_at: int | float
    issued_at: int | float | None
    claims: Mapping[str, Any]

    @property
    def session_id(self) -> str | None:
        value = self.claims.get("sid")
        return value if isinstance(value, str) else None


@dataclass(frozen=True, repr=False)
class OIDCBackChannelLogoutClaims:
    """Validated selectors only; repr intentionally never exposes IdP identifiers."""

    issuer: str
    audiences: tuple[str, ...]
    issued_at: int | float
    expires_at: int | float
    token_id: str
    subject: str | None
    session_id: str | None

    def __repr__(self) -> str:
        return (
            "OIDCBackChannelLogoutClaims("
            f"issuer=<redacted>, audiences={self.audiences!r}, "
            f"issued_at={self.issued_at!r}, expires_at={self.expires_at!r}, "
            "token_id=<redacted>, subject=<redacted>, session_id=<redacted>)"
        )


class OIDCHttpTransport(Protocol):
    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        maximum_bytes: int,
    ) -> Mapping[str, Any]:
        """Fetch a JSON object without following redirects."""


class HTTPXOIDCTransport:
    """Small default transport; callers can inject a managed client adapter instead."""

    def get_json(
        self,
        url: str,
        *,
        timeout: float,
        maximum_bytes: int,
    ) -> Mapping[str, Any]:
        try:
            with httpx.stream(
                "GET",
                url,
                headers={"Accept": "application/json"},
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                body = read_bounded_httpx_body(response, maximum_bytes=maximum_bytes)
            payload = json.loads(body)
        except (httpx.HTTPError, OIDCHTTPResponseLimitError, UnicodeError, ValueError):
            raise OIDCProviderUnavailableError from None
        if not isinstance(payload, Mapping):
            raise OIDCConfigurationError
        return payload


@dataclass(frozen=True)
class _DiscoveryCache:
    jwks_uri: str
    expires_at: float


@dataclass(frozen=True)
class _JwksCache:
    keys: Mapping[str, Any]
    expires_at: float


class _OIDCSignedTokenValidator:
    """Shared exact-discovery and cached-JWKS validation machinery."""

    def __init__(
        self,
        config: OIDCProviderConfig,
        *,
        transport: OIDCHttpTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._transport = transport or HTTPXOIDCTransport()
        self._clock = clock
        self._jwt = JsonWebToken([_ALGORITHM])
        self._lock = RLock()
        self._discovery: _DiscoveryCache | None = None
        self._jwks: _JwksCache | None = None

    def _decode_signed_claims(
        self,
        token: str,
        *,
        current_time: float,
        claims_options: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        if not isinstance(token, str) or not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise OIDCTokenValidationError

        validated_header: dict[str, Any] = {}

        def load_signing_key(header: Mapping[str, Any], _payload: Mapping[str, Any]) -> Any:
            if header.get("alg") != _ALGORITHM:
                raise OIDCTokenValidationError
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 256:
                raise OIDCTokenValidationError
            validated_header.clear()
            validated_header.update(header)
            return self._key_for_kid(kid, current_time)

        try:
            claims = self._jwt.decode(
                token,
                load_signing_key,
                claims_options=claims_options,
            )
            claims.validate(
                now=int(current_time),
                leeway=self.config.clock_skew_seconds,
            )
            self._validate_token_purpose(validated_header, claims)
            return cast(Mapping[str, Any], claims)
        except OIDCError:
            raise
        except Exception:
            raise OIDCTokenValidationError from None

    def _validate_token_purpose(
        self,
        header: Mapping[str, Any],
        claims: Mapping[str, Any],
    ) -> None:
        del header, claims

    def _current_time(self, override: float | None) -> float:
        try:
            value = float(self._clock() if override is None else override)
        except (TypeError, ValueError, OverflowError):
            raise OIDCConfigurationError from None
        if not math.isfinite(value) or value < 0:
            raise OIDCConfigurationError
        return value

    def _key_for_kid(self, kid: str, now: float) -> Any:
        with self._lock:
            keys = self._get_jwks(now)
            key = keys.get(kid)
            if key is not None:
                return key
            keys = self._get_jwks(now, force_refresh=True)
            key = keys.get(kid)
            if key is None:
                raise OIDCTokenValidationError
            return key

    def _get_jwks(self, now: float, *, force_refresh: bool = False) -> Mapping[str, Any]:
        if not force_refresh and self._jwks is not None and self._jwks.expires_at > now:
            return self._jwks.keys
        jwks_uri = self._get_jwks_uri(now)
        document = self._fetch_json(
            jwks_uri,
            maximum_bytes=_MAX_JWKS_RESPONSE_BYTES,
        )
        keys = _import_jwks(document)
        self._jwks = _JwksCache(
            keys=MappingProxyType(keys),
            expires_at=now + self.config.jwks_cache_ttl_seconds,
        )
        return self._jwks.keys

    def _get_jwks_uri(self, now: float) -> str:
        if self._discovery is not None and self._discovery.expires_at > now:
            return self._discovery.jwks_uri
        discovery_url = self.config.discovery_url
        if discovery_url is None:  # Guaranteed by OIDCProviderConfig.__post_init__.
            raise OIDCConfigurationError
        document = self._fetch_json(
            discovery_url,
            maximum_bytes=_MAX_DISCOVERY_RESPONSE_BYTES,
        )
        if document.get("issuer") != self.config.issuer:
            raise OIDCConfigurationError
        jwks_uri = document.get("jwks_uri")
        if not isinstance(jwks_uri, str):
            raise OIDCConfigurationError
        _validate_absolute_http_url(jwks_uri)
        if _uses_https(self.config.issuer) and not _uses_https(jwks_uri):
            raise OIDCConfigurationError
        self._discovery = _DiscoveryCache(
            jwks_uri=jwks_uri,
            expires_at=now + self.config.jwks_cache_ttl_seconds,
        )
        return jwks_uri

    def _fetch_json(self, url: str, *, maximum_bytes: int) -> Mapping[str, Any]:
        try:
            document = self._transport.get_json(
                url,
                timeout=float(self.config.http_timeout_seconds),
                maximum_bytes=maximum_bytes,
            )
        except OIDCError:
            raise
        except Exception:
            raise OIDCProviderUnavailableError from None
        if not isinstance(document, Mapping):
            raise OIDCConfigurationError
        return document


class OIDCTokenValidator(_OIDCSignedTokenValidator):
    """Validate an OIDC bearer token against exact provider metadata."""

    def validate(self, token: str, *, now: float | None = None) -> OIDCValidatedClaims:
        current_time = self._current_time(now)
        claims = self._decode_signed_claims(
            token,
            current_time=current_time,
            claims_options={
                "iss": {"essential": True, "value": self.config.issuer},
                "sub": {"essential": True},
                "aud": {"essential": True, "value": self.config.audience},
                "exp": {"essential": True},
            },
        )
        return self._build_validated_claims(claims)

    def _validate_token_purpose(
        self,
        header: Mapping[str, Any],
        claims: Mapping[str, Any],
    ) -> None:
        """Require positive evidence that a bearer JWT is an API access token."""

        header_type = header.get("typ")
        claim_type = claims.get("typ")
        token_use = claims.get("token_use")
        if header_type is not None and not isinstance(header_type, str):
            raise OIDCTokenValidationError
        if claim_type is not None and not isinstance(claim_type, str):
            raise OIDCTokenValidationError
        if token_use is not None and not isinstance(token_use, str):
            raise OIDCTokenValidationError

        normalized_header_type = header_type.casefold() if isinstance(header_type, str) else None
        normalized_claim_type = claim_type.casefold() if isinstance(claim_type, str) else None
        normalized_token_use = token_use.casefold() if isinstance(token_use, str) else None
        if normalized_claim_type is not None and normalized_claim_type not in (
            _ACCESS_TOKEN_CLAIM_TYPES
        ):
            raise OIDCTokenValidationError
        if normalized_token_use is not None and normalized_token_use != "access":
            raise OIDCTokenValidationError
        if (
            normalized_header_type in _ACCESS_TOKEN_HEADER_TYPES
            or normalized_claim_type in _ACCESS_TOKEN_CLAIM_TYPES
            or normalized_token_use == "access"
        ):
            return
        raise OIDCTokenValidationError

    def _build_validated_claims(self, claims: Mapping[str, Any]) -> OIDCValidatedClaims:
        issuer = claims.get("iss")
        subject = claims.get("sub")
        audience = claims.get("aud")
        expires_at = claims.get("exp")
        issued_at = claims.get("iat")
        if issuer != self.config.issuer or not isinstance(issuer, str):
            raise OIDCTokenValidationError
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise OIDCTokenValidationError
        audiences = _audiences(audience)
        if self.config.audience not in audiences:
            raise OIDCTokenValidationError
        if not _is_numeric_date(expires_at):
            raise OIDCTokenValidationError
        if issued_at is not None and not _is_numeric_date(issued_at):
            raise OIDCTokenValidationError
        numeric_expires_at = cast(int | float, expires_at)
        numeric_issued_at = cast(int | float | None, issued_at)
        raw_claims = MappingProxyType(dict(claims))
        return OIDCValidatedClaims(
            subject=subject,
            issuer=issuer,
            audiences=audiences,
            expires_at=numeric_expires_at,
            issued_at=numeric_issued_at,
            claims=raw_claims,
        )


class OIDCIDTokenValidator(OIDCTokenValidator):
    """Validate browser ID tokens against the OAuth client, including ``azp``."""

    def __init__(
        self,
        config: OIDCProviderConfig,
        *,
        client_id: str,
        transport: OIDCHttpTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(client_id, str)
            or not client_id
            or client_id != client_id.strip()
            or len(client_id) > 512
            or config.audience != client_id
        ):
            raise OIDCConfigurationError
        self.client_id = client_id
        super().__init__(config, transport=transport, clock=clock)

    def _build_validated_claims(self, claims: Mapping[str, Any]) -> OIDCValidatedClaims:
        validated = super()._build_validated_claims(claims)
        session_id = claims.get("sid")
        if session_id is not None and not _is_bounded_identifier(session_id):
            raise OIDCTokenValidationError
        authorized_party = claims.get("azp")
        if len(validated.audiences) > 1:
            if not isinstance(authorized_party, str) or not hmac.compare_digest(
                authorized_party.encode("utf-8"), self.client_id.encode("utf-8")
            ):
                raise OIDCTokenValidationError
        elif authorized_party is not None and (
            not isinstance(authorized_party, str)
            or not hmac.compare_digest(
                authorized_party.encode("utf-8"), self.client_id.encode("utf-8")
            )
        ):
            raise OIDCTokenValidationError
        return validated

    def _validate_token_purpose(
        self,
        header: Mapping[str, Any],
        claims: Mapping[str, Any],
    ) -> None:
        # ID tokens are selected from the token endpoint's dedicated id_token
        # member and are separately bound to client_id, azp and nonce.
        del header, claims


class OIDCBackChannelLogoutTokenValidator(_OIDCSignedTokenValidator):
    """Validate a standards-based Logout Token without accepting an ID/access token."""

    def validate(
        self,
        token: str,
        *,
        now: float | None = None,
    ) -> OIDCBackChannelLogoutClaims:
        current_time = self._current_time(now)
        claims = self._decode_signed_claims(
            token,
            current_time=current_time,
            claims_options={
                "iss": {"essential": True, "value": self.config.issuer},
                "aud": {"essential": True, "value": self.config.audience},
                "iat": {"essential": True},
                "exp": {"essential": True},
                "jti": {"essential": True},
                "events": {"essential": True},
            },
        )
        issuer = claims.get("iss")
        audiences = _audiences(claims.get("aud"))
        issued_at = claims.get("iat")
        expires_at = claims.get("exp")
        token_id = claims.get("jti")
        subject = claims.get("sub")
        session_id = claims.get("sid")
        events = claims.get("events")
        if issuer != self.config.issuer or not isinstance(issuer, str):
            raise OIDCTokenValidationError
        if self.config.audience not in audiences:
            raise OIDCTokenValidationError
        if not _is_numeric_date(issued_at) or not _is_numeric_date(expires_at):
            raise OIDCTokenValidationError
        numeric_issued_at = cast(int | float, issued_at)
        numeric_expires_at = cast(int | float, expires_at)
        if (
            numeric_issued_at < 0
            or numeric_expires_at <= numeric_issued_at
            or numeric_expires_at - numeric_issued_at > _MAX_BACKCHANNEL_LOGOUT_LIFETIME_SECONDS
            or numeric_issued_at > current_time + self.config.clock_skew_seconds
        ):
            raise OIDCTokenValidationError
        if not _is_bounded_identifier(token_id):
            raise OIDCTokenValidationError
        if subject is not None and not _is_bounded_identifier(subject):
            raise OIDCTokenValidationError
        if session_id is not None and not _is_bounded_identifier(session_id):
            raise OIDCTokenValidationError
        if subject is None and session_id is None:
            raise OIDCTokenValidationError
        if (
            not isinstance(events, Mapping)
            or _BACKCHANNEL_LOGOUT_EVENT not in events
            or not isinstance(events[_BACKCHANNEL_LOGOUT_EVENT], Mapping)
            or "nonce" in claims
        ):
            raise OIDCTokenValidationError
        return OIDCBackChannelLogoutClaims(
            issuer=issuer,
            audiences=audiences,
            issued_at=numeric_issued_at,
            expires_at=numeric_expires_at,
            token_id=cast(str, token_id),
            subject=cast(str | None, subject),
            session_id=cast(str | None, session_id),
        )


def _import_jwks(document: Mapping[str, Any]) -> dict[str, Any]:
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > _MAX_JWKS_KEYS:
        raise OIDCConfigurationError
    keys: dict[str, Any] = {}
    for raw_key in raw_keys:
        if not isinstance(raw_key, Mapping):
            raise OIDCConfigurationError
        kid = raw_key.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > 256 or kid in keys:
            raise OIDCConfigurationError
        _validate_rs256_verification_jwk(raw_key)
        try:
            keys[kid] = JsonWebKey.import_key(dict(raw_key))
        except Exception:
            raise OIDCConfigurationError from None
    return keys


def _validate_rs256_verification_jwk(raw_key: Mapping[str, Any]) -> None:
    if raw_key.get("kty") != "RSA":
        raise OIDCConfigurationError
    algorithm = raw_key.get("alg")
    if algorithm is not None and algorithm != _ALGORITHM:
        raise OIDCConfigurationError
    use = raw_key.get("use")
    if use is not None and use != "sig":
        raise OIDCConfigurationError
    key_ops = raw_key.get("key_ops")
    if key_ops is not None:
        if (
            not isinstance(key_ops, list)
            or not key_ops
            or any(not isinstance(operation, str) for operation in key_ops)
            or "verify" not in key_ops
        ):
            raise OIDCConfigurationError
    if _PRIVATE_RSA_PARAMETERS.intersection(raw_key):
        raise OIDCConfigurationError


def _audiences(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    raise OIDCTokenValidationError


def _is_numeric_date(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _is_bounded_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= _MAX_OIDC_IDENTIFIER_LENGTH
    )


def _validate_absolute_http_url(value: str) -> None:
    try:
        parsed = urlparse(value)
        _port = parsed.port
    except ValueError:
        raise OIDCConfigurationError from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise OIDCConfigurationError


def _uses_https(value: str) -> bool:
    return urlparse(value).scheme == "https"
