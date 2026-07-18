"""Pure callback-signature primitives with rotation and replay protection.

The module deliberately performs no storage, clock, network, or configuration I/O.
Callers own those boundaries and inject an atomic nonce store during verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from urllib.parse import parse_qsl, quote

CALLBACK_SIGNATURE_VERSION = "v2"
MIN_CALLBACK_KEY_BYTES = 32
_SIGNATURE_PATTERN = re.compile(r"v2=[0-9a-f]{64}\Z")
_KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_NONCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")
_METHOD_PATTERN = re.compile(r"[A-Z]+\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_INSECURE_KEY_MARKERS = (
    b"auris-demo",
    b"auris-dev",
    b"changeme",
    b"change-me",
    b"example",
    b"placeholder",
    b"replace-with",
)


class CallbackSignatureError(ValueError):
    """A stable, non-sensitive rejection suitable for boundary translation."""

    _MESSAGES = {
        "CALLBACK_SIGNATURE_INVALID_INPUT": "callback signature input is invalid",
        "CALLBACK_SIGNATURE_INVALID": "callback signature is invalid",
        "CALLBACK_SIGNATURE_KEY_REJECTED": "callback signing key is not accepted",
        "CALLBACK_SIGNATURE_WEAK_KEY": "callback signing key is not acceptable",
        "CALLBACK_SIGNATURE_TIMESTAMP_REJECTED": "callback signature timestamp is not accepted",
        "CALLBACK_SIGNATURE_REPLAYED": "callback signature replay is not accepted",
        "CALLBACK_SIGNATURE_REPLAY_STORE_UNAVAILABLE": (
            "callback replay protection is unavailable"
        ),
    }

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(self._MESSAGES.get(code, "callback signature was rejected"))


class CallbackKeyState(StrEnum):
    """Lifecycle state for a callback signing key."""

    ACTIVE = "active"
    OVERLAP = "overlap"
    RETIRED = "retired"


@dataclass(frozen=True)
class CallbackKeyBinding:
    """A key identifier bound to secret material and a verification window."""

    key_id: str
    secret: bytes = field(repr=False)
    state: CallbackKeyState = CallbackKeyState.ACTIVE
    not_before: int | None = None
    not_after: int | None = None

    def __post_init__(self) -> None:
        if not _KEY_ID_PATTERN.fullmatch(self.key_id):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if not isinstance(self.secret, bytes):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_WEAK_KEY")
        if (
            len(self.secret) < MIN_CALLBACK_KEY_BYTES
            or len(set(self.secret)) < 8
            or not self.secret.strip()
            or any(marker in self.secret.lower() for marker in _INSECURE_KEY_MARKERS)
        ):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_WEAK_KEY")
        if not isinstance(self.state, CallbackKeyState):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        for boundary in (self.not_before, self.not_after):
            if boundary is not None and (
                isinstance(boundary, bool) or not isinstance(boundary, int)
            ):
                raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if (
            self.not_before is not None
            and self.not_after is not None
            and self.not_before > self.not_after
        ):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")

    def accepts_timestamp(self, timestamp: int) -> bool:
        return (self.not_before is None or timestamp >= self.not_before) and (
            self.not_after is None or timestamp <= self.not_after
        )


class CallbackKeyring:
    """Immutable keyring with one explicit active signing key."""

    def __init__(
        self,
        bindings: Sequence[CallbackKeyBinding],
        *,
        active_key_id: str,
    ) -> None:
        indexed: dict[str, CallbackKeyBinding] = {}
        for binding in bindings:
            if binding.key_id in indexed:
                raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
            indexed[binding.key_id] = binding
        active = indexed.get(active_key_id)
        if (
            active is None
            or active.state is not CallbackKeyState.ACTIVE
            or sum(binding.state is CallbackKeyState.ACTIVE for binding in indexed.values()) != 1
        ):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_KEY_REJECTED")
        self._bindings = indexed
        self._active_key_id = active_key_id

    @property
    def active_key(self) -> CallbackKeyBinding:
        return self._bindings[self._active_key_id]

    def signing_key(self, *, key_id: str, timestamp: int) -> CallbackKeyBinding:
        binding = self.active_key
        if binding.key_id != key_id or not binding.accepts_timestamp(timestamp):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_KEY_REJECTED")
        return binding

    def verification_key(self, *, key_id: str, timestamp: int) -> CallbackKeyBinding:
        binding = self._bindings.get(key_id)
        if (
            binding is None
            or binding.state is CallbackKeyState.RETIRED
            or not binding.accepts_timestamp(timestamp)
        ):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_KEY_REJECTED")
        return binding


def parse_callback_keyring(
    raw_bindings: str,
    *,
    active_key_id: str,
) -> CallbackKeyring:
    """Parse the explicit JSON keyring shared by senders and callback receivers.

    The object keys are public key identifiers. Secret material is never included
    in validation errors, and unknown fields are rejected so a rotation typo cannot
    silently weaken the accepted-key window.
    """

    try:
        configured = json.loads(raw_bindings)
    except (TypeError, ValueError):
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT") from None
    if not isinstance(configured, dict) or not configured:
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
    bindings: list[CallbackKeyBinding] = []
    allowed_fields = {"secret", "state", "not_before", "not_after"}
    for key_id, value in configured.items():
        if not isinstance(key_id, str) or not isinstance(value, dict):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if set(value) - allowed_fields:
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        secret = value.get("secret")
        state = value.get("state")
        if not isinstance(secret, str) or not isinstance(state, str):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        try:
            key_state = CallbackKeyState(state)
        except ValueError:
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT") from None
        bindings.append(
            CallbackKeyBinding(
                key_id=key_id,
                secret=secret.encode("utf-8"),
                state=key_state,
                not_before=value.get("not_before"),
                not_after=value.get("not_after"),
            )
        )
    return CallbackKeyring(bindings, active_key_id=active_key_id)


def _validate_bound_text(value: str, *, maximum_length: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum_length
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
    return value


def _query_pairs(query: str | Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    if isinstance(query, str):
        if any(ord(character) < 32 or ord(character) == 127 for character in query):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if _INVALID_PERCENT_ESCAPE.search(query):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        try:
            pairs = parse_qsl(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                encoding="utf-8",
                errors="strict",
            )
        except (UnicodeDecodeError, ValueError):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT") from None
    else:
        pairs = list(query)
    normalized: list[tuple[str, str]] = []
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        normalized.append((name, value))
    return tuple(normalized)


def canonicalize_callback_query(query: str | Sequence[tuple[str, str]]) -> str:
    """Return a deterministic RFC 3986 query while preserving repeated keys."""

    encoded = [
        (quote(name, safe="-._~"), quote(value, safe="-._~")) for name, value in _query_pairs(query)
    ]
    encoded.sort()
    return "&".join(f"{name}={value}" for name, value in encoded)


@dataclass(frozen=True)
class CallbackSignatureRequest:
    """All request dimensions covered by an Auris callback v2 signature."""

    method: str
    path: str
    query: str | Sequence[tuple[str, str]]
    tenant_id: str
    project_id: str
    idempotency_key: str
    timestamp: int
    nonce: str
    key_id: str
    body: bytes = field(repr=False)
    version: str = CALLBACK_SIGNATURE_VERSION
    canonical_query: str = field(init=False)
    body_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        method = self.method.upper() if isinstance(self.method, str) else ""
        if not _METHOD_PATTERN.fullmatch(method):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or "?" in self.path
            or "#" in self.path
            or any(ord(character) < 32 or ord(character) == 127 for character in self.path)
        ):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        _validate_bound_text(self.tenant_id)
        _validate_bound_text(self.project_id)
        _validate_bound_text(self.idempotency_key)
        if isinstance(self.timestamp, bool) or not isinstance(self.timestamp, int):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if not isinstance(self.nonce, str) or not _NONCE_PATTERN.fullmatch(self.nonce):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if not isinstance(self.key_id, str) or not _KEY_ID_PATTERN.fullmatch(self.key_id):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if not isinstance(self.body, bytes):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        if self.version != CALLBACK_SIGNATURE_VERSION:
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "query", _query_pairs(self.query))
        object.__setattr__(self, "canonical_query", canonicalize_callback_query(self.query))
        object.__setattr__(self, "body_sha256", hashlib.sha256(self.body).hexdigest())

    def canonical_bytes(self) -> bytes:
        canonical = "\n".join(
            (
                "auris-flow-callback",
                f"version:{self.version}",
                f"method:{self.method}",
                f"path:{self.path}",
                f"query:{self.canonical_query}",
                f"tenant:{self.tenant_id}",
                f"project:{self.project_id}",
                f"idempotency:{self.idempotency_key}",
                f"timestamp:{self.timestamp}",
                f"nonce:{self.nonce}",
                f"key-id:{self.key_id}",
                f"body-sha256:{self.body_sha256}",
            )
        )
        return canonical.encode("utf-8")


class CallbackNonceReplayStore(Protocol):
    """Atomic nonce store; ``True`` means this key/nonce pair was newly claimed."""

    def claim(self, *, key_id: str, nonce: str, expires_at: int) -> bool: ...


@dataclass(frozen=True)
class CallbackVerificationResult:
    verified: bool
    key_id: str
    body_sha256: str


def sign_callback(request: CallbackSignatureRequest, keyring: CallbackKeyring) -> str:
    """Sign a canonical request with the sole active key."""

    binding = keyring.signing_key(key_id=request.key_id, timestamp=request.timestamp)
    digest = hmac.new(binding.secret, request.canonical_bytes(), hashlib.sha256).hexdigest()
    return f"{CALLBACK_SIGNATURE_VERSION}={digest}"


def verify_callback_signature(
    request: CallbackSignatureRequest,
    signature: str,
    keyring: CallbackKeyring,
    *,
    now: int,
    tolerance_seconds: int,
    nonce_store: CallbackNonceReplayStore,
) -> CallbackVerificationResult:
    """Verify signature freshness and atomically reject nonce reuse."""

    if (
        isinstance(now, bool)
        or not isinstance(now, int)
        or isinstance(tolerance_seconds, bool)
        or not isinstance(tolerance_seconds, int)
        or tolerance_seconds < 0
    ):
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
    if abs(now - request.timestamp) > tolerance_seconds:
        raise CallbackSignatureError("CALLBACK_SIGNATURE_TIMESTAMP_REJECTED")
    binding = keyring.verification_key(key_id=request.key_id, timestamp=request.timestamp)
    if not isinstance(signature, str) or not _SIGNATURE_PATTERN.fullmatch(signature):
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID")
    supplied_digest = signature.removeprefix(f"{CALLBACK_SIGNATURE_VERSION}=")
    expected_digest = hmac.new(
        binding.secret,
        request.canonical_bytes(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_digest, supplied_digest):
        raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID")
    try:
        claimed = nonce_store.claim(
            key_id=request.key_id,
            nonce=request.nonce,
            expires_at=request.timestamp + tolerance_seconds,
        )
    except Exception:
        raise CallbackSignatureError("CALLBACK_SIGNATURE_REPLAY_STORE_UNAVAILABLE") from None
    if not claimed:
        raise CallbackSignatureError("CALLBACK_SIGNATURE_REPLAYED")
    return CallbackVerificationResult(
        verified=True,
        key_id=request.key_id,
        body_sha256=request.body_sha256,
    )


class CallbackIdempotencyOutcome(StrEnum):
    NEW = "new"
    REPLAY_ALLOWED = "replay_allowed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class CallbackIdempotencyBinding:
    """Persistable request identity; it intentionally excludes the body itself."""

    idempotency_key: str
    body_sha256: str

    def __post_init__(self) -> None:
        _validate_bound_text(self.idempotency_key)
        if not _SHA256_PATTERN.fullmatch(self.body_sha256):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")

    @classmethod
    def from_body(cls, *, idempotency_key: str, body: bytes) -> CallbackIdempotencyBinding:
        if not isinstance(body, bytes):
            raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID_INPUT")
        return cls(
            idempotency_key=idempotency_key,
            body_sha256=hashlib.sha256(body).hexdigest(),
        )


def decide_callback_idempotency(
    *,
    existing: CallbackIdempotencyBinding | None,
    candidate: CallbackIdempotencyBinding,
) -> CallbackIdempotencyOutcome:
    """Classify a retry without accepting a new body under an existing key."""

    if existing is None or existing.idempotency_key != candidate.idempotency_key:
        return CallbackIdempotencyOutcome.NEW
    if existing.body_sha256 == candidate.body_sha256:
        return CallbackIdempotencyOutcome.REPLAY_ALLOWED
    return CallbackIdempotencyOutcome.CONFLICT
