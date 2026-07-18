from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, replace

import pytest

from app.core import callback_signature as signature_module
from app.core.callback_signature import (
    CallbackIdempotencyBinding,
    CallbackIdempotencyOutcome,
    CallbackKeyBinding,
    CallbackKeyring,
    CallbackKeyState,
    CallbackSignatureError,
    CallbackSignatureRequest,
    decide_callback_idempotency,
    sign_callback,
    verify_callback_signature,
)

TIMESTAMP = 1_784_352_000
NEW_SECRET = hashlib.sha256(b"auris-flow-callback-new-key-fixture").digest()
OLD_SECRET = hashlib.sha256(b"auris-flow-callback-old-key-fixture").digest()


@dataclass
class InMemoryNonceStore:
    claimed: set[tuple[str, str]]
    expirations: list[int]

    def __init__(self) -> None:
        self.claimed = set()
        self.expirations = []

    def claim(self, *, key_id: str, nonce: str, expires_at: int) -> bool:
        binding = (key_id, nonce)
        if binding in self.claimed:
            return False
        self.claimed.add(binding)
        self.expirations.append(expires_at)
        return True


def _key(
    key_id: str = "callback-2026-07",
    *,
    secret: bytes = NEW_SECRET,
    state: CallbackKeyState = CallbackKeyState.ACTIVE,
    not_before: int | None = None,
    not_after: int | None = None,
) -> CallbackKeyBinding:
    return CallbackKeyBinding(
        key_id=key_id,
        secret=secret,
        state=state,
        not_before=not_before,
        not_after=not_after,
    )


def _keyring(*bindings: CallbackKeyBinding, active_key_id: str | None = None) -> CallbackKeyring:
    selected = bindings or (_key(),)
    return CallbackKeyring(
        selected,
        active_key_id=active_key_id or selected[0].key_id,
    )


def _request(**overrides: object) -> CallbackSignatureRequest:
    values: dict[str, object] = {
        "method": "post",
        "path": "/api/v1/callbacks/evaluation%2Fdone",
        "query": (("z", "last"), ("tag", "b c"), ("tag", "a"), ("a", "~")),
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "idempotency_key": "idem_callback_001",
        "timestamp": TIMESTAMP,
        "nonce": "dVAx7G1SKxg3kQp9WD0g5w",
        "key_id": "callback-2026-07",
        "body": b'{"result":"accepted"}',
    }
    values.update(overrides)
    return CallbackSignatureRequest(**values)  # type: ignore[arg-type]


def test_v2_canonical_request_binds_every_security_dimension() -> None:
    request = _request()

    assert request.canonical_query == "a=~&tag=a&tag=b%20c&z=last"
    assert request.body_sha256 == hashlib.sha256(request.body).hexdigest()
    assert request.canonical_bytes().decode("utf-8") == (
        "auris-flow-callback\n"
        "version:v2\n"
        "method:POST\n"
        "path:/api/v1/callbacks/evaluation%2Fdone\n"
        "query:a=~&tag=a&tag=b%20c&z=last\n"
        "tenant:aurora_auto\n"
        "project:sales_qa\n"
        "idempotency:idem_callback_001\n"
        f"timestamp:{TIMESTAMP}\n"
        "nonce:dVAx7G1SKxg3kQp9WD0g5w\n"
        "key-id:callback-2026-07\n"
        f"body-sha256:{request.body_sha256}"
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("method", "PUT"),
        ("path", "/api/v1/callbacks/other"),
        ("query", (("a", "different"),)),
        ("tenant_id", "another_tenant"),
        ("project_id", "another_project"),
        ("idempotency_key", "another_idempotency_key"),
        ("timestamp", TIMESTAMP + 1),
        ("nonce", "mqcH3kQp9WD0g5wdVAx7G1"),
        ("body", b'{"result":"different"}'),
    ],
)
def test_v2_signature_changes_when_any_bound_value_changes(
    field_name: str,
    value: object,
) -> None:
    keyring = _keyring()
    original = _request()
    changed = replace(original, **{field_name: value})  # type: ignore[arg-type]

    assert sign_callback(changed, keyring) != sign_callback(original, keyring)


def test_active_key_signs_and_constant_time_verification_claims_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = _keyring()
    request = _request()
    signature = sign_callback(request, keyring)
    nonce_store = InMemoryNonceStore()
    original_compare = hmac.compare_digest
    compared: list[tuple[str, str]] = []

    def record_compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr(signature_module.hmac, "compare_digest", record_compare)

    result = verify_callback_signature(
        request,
        signature,
        keyring,
        now=TIMESTAMP + 10,
        tolerance_seconds=60,
        nonce_store=nonce_store,
    )

    assert result.verified is True
    assert result.key_id == request.key_id
    assert result.body_sha256 == request.body_sha256
    assert compared and compared[0][0] != ""
    assert nonce_store.claimed == {(request.key_id, request.nonce)}
    assert nonce_store.expirations == [TIMESTAMP + 60]


def test_rotated_keyring_verifies_old_and_new_keys_during_overlap() -> None:
    old_active = _key("callback-2026-06", secret=OLD_SECRET)
    old_ring = _keyring(old_active)
    old_request = _request(key_id=old_active.key_id, nonce="old-key-overlap-nonce-01")
    old_signature = sign_callback(old_request, old_ring)

    old_overlap = replace(
        old_active,
        state=CallbackKeyState.OVERLAP,
        not_after=TIMESTAMP + 120,
    )
    new_active = _key(not_before=TIMESTAMP - 120)
    rotated_ring = _keyring(old_overlap, new_active, active_key_id=new_active.key_id)

    old_result = verify_callback_signature(
        old_request,
        old_signature,
        rotated_ring,
        now=TIMESTAMP,
        tolerance_seconds=60,
        nonce_store=InMemoryNonceStore(),
    )
    new_signature = sign_callback(_request(), rotated_ring)
    new_result = verify_callback_signature(
        _request(),
        new_signature,
        rotated_ring,
        now=TIMESTAMP,
        tolerance_seconds=60,
        nonce_store=InMemoryNonceStore(),
    )

    assert old_result.verified is True
    assert new_result.verified is True


def test_retired_expired_and_unknown_keys_fail_with_same_non_leaking_error() -> None:
    old_active = _key("private-old-key-id", secret=OLD_SECRET)
    old_request = _request(key_id=old_active.key_id, nonce="old-retired-key-nonce")
    old_signature = sign_callback(old_request, _keyring(old_active))
    retired_ring = _keyring(
        replace(old_active, state=CallbackKeyState.RETIRED),
        _key(),
        active_key_id="callback-2026-07",
    )
    expired_ring = _keyring(
        replace(
            old_active,
            state=CallbackKeyState.OVERLAP,
            not_after=TIMESTAMP - 1,
        ),
        _key(),
        active_key_id="callback-2026-07",
    )
    unknown_request = _request(
        key_id="sensitive-unknown-key-id",
        nonce="unknown-key-nonce-001",
    )
    unknown_signature = "v2=" + ("0" * 64)

    failures: list[CallbackSignatureError] = []
    for request, signature, ring in (
        (old_request, old_signature, retired_ring),
        (old_request, old_signature, expired_ring),
        (unknown_request, unknown_signature, retired_ring),
    ):
        with pytest.raises(CallbackSignatureError) as error:
            verify_callback_signature(
                request,
                signature,
                ring,
                now=TIMESTAMP,
                tolerance_seconds=60,
                nonce_store=InMemoryNonceStore(),
            )
        failures.append(error.value)

    assert {failure.code for failure in failures} == {"CALLBACK_SIGNATURE_KEY_REJECTED"}
    assert len({str(failure) for failure in failures}) == 1
    assert "private-old-key-id" not in str(failures[0])
    assert "sensitive-unknown-key-id" not in str(failures[-1])


@pytest.mark.parametrize(
    "weak_secret",
    [b"short", b"a" * 32, b" " * 64],
)
def test_key_binding_rejects_weak_secrets_without_exposing_them(
    weak_secret: bytes,
) -> None:
    with pytest.raises(CallbackSignatureError) as error:
        _key(secret=weak_secret)

    assert error.value.code == "CALLBACK_SIGNATURE_WEAK_KEY"
    assert repr(weak_secret) not in str(error.value)
    assert "secret" not in str(error.value).lower()


@pytest.mark.parametrize("timestamp", [TIMESTAMP - 61, TIMESTAMP + 61])
def test_timestamp_outside_tolerance_is_rejected_without_claiming_nonce(
    timestamp: int,
) -> None:
    request = _request(timestamp=timestamp)
    keyring = _keyring()
    nonce_store = InMemoryNonceStore()

    with pytest.raises(CallbackSignatureError) as error:
        verify_callback_signature(
            request,
            sign_callback(request, keyring),
            keyring,
            now=TIMESTAMP,
            tolerance_seconds=60,
            nonce_store=nonce_store,
        )

    assert error.value.code == "CALLBACK_SIGNATURE_TIMESTAMP_REJECTED"
    assert nonce_store.claimed == set()


def test_replayed_nonce_is_rejected_after_a_valid_signature() -> None:
    request = _request()
    keyring = _keyring()
    signature = sign_callback(request, keyring)
    nonce_store = InMemoryNonceStore()

    verify_callback_signature(
        request,
        signature,
        keyring,
        now=TIMESTAMP,
        tolerance_seconds=60,
        nonce_store=nonce_store,
    )
    with pytest.raises(CallbackSignatureError) as error:
        verify_callback_signature(
            request,
            signature,
            keyring,
            now=TIMESTAMP,
            tolerance_seconds=60,
            nonce_store=nonce_store,
        )

    assert error.value.code == "CALLBACK_SIGNATURE_REPLAYED"


def test_invalid_signature_is_generic_and_does_not_consume_nonce() -> None:
    request = _request()
    nonce_store = InMemoryNonceStore()
    attacker_signature = "v2=" + ("f" * 64)

    with pytest.raises(CallbackSignatureError) as error:
        verify_callback_signature(
            request,
            attacker_signature,
            _keyring(),
            now=TIMESTAMP,
            tolerance_seconds=60,
            nonce_store=nonce_store,
        )

    assert error.value.code == "CALLBACK_SIGNATURE_INVALID"
    assert attacker_signature not in str(error.value)
    assert request.key_id not in str(error.value)
    assert nonce_store.claimed == set()


def test_idempotency_decision_allows_same_hash_and_rejects_body_conflict() -> None:
    original = CallbackIdempotencyBinding.from_body(
        idempotency_key="idem_callback_001",
        body=b'{"result":"accepted"}',
    )
    same_retry = CallbackIdempotencyBinding.from_body(
        idempotency_key="idem_callback_001",
        body=b'{"result":"accepted"}',
    )
    conflicting_retry = CallbackIdempotencyBinding.from_body(
        idempotency_key="idem_callback_001",
        body=b'{"result":"rejected"}',
    )
    unrelated = CallbackIdempotencyBinding.from_body(
        idempotency_key="idem_callback_002",
        body=b'{"result":"rejected"}',
    )

    assert (
        decide_callback_idempotency(existing=None, candidate=original)
        is CallbackIdempotencyOutcome.NEW
    )
    assert (
        decide_callback_idempotency(existing=original, candidate=same_retry)
        is CallbackIdempotencyOutcome.REPLAY_ALLOWED
    )
    assert (
        decide_callback_idempotency(existing=original, candidate=conflicting_retry)
        is CallbackIdempotencyOutcome.CONFLICT
    )
    assert (
        decide_callback_idempotency(existing=original, candidate=unrelated)
        is CallbackIdempotencyOutcome.NEW
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("method", "POST\nX-Forged: true"),
        ("path", "https://callback.example/callback"),
        ("path", "/callbacks?admin=true"),
        ("tenant_id", "tenant\nproject:forged"),
        ("project_id", ""),
        ("idempotency_key", ""),
        ("nonce", "too-short"),
    ],
)
def test_canonical_request_rejects_ambiguous_or_injected_fields(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(CallbackSignatureError):
        _request(**{field_name: value})
