from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.core import config as config_module
from app.core.callback_signature import (
    CallbackSignatureRequest,
    parse_callback_keyring,
    verify_callback_signature,
)
from app.core.config import Settings
from app.services import adapters as adapter_module
from app.services.adapters import RealExternalCallbackClient

TIMESTAMP = 1_784_352_000
NONCE = "callback-v2-adapter-nonce-001"
KEY_ID = "callback-2026-07"
ACTIVE_KEY_MATERIAL = "callback-v2-adapter-key-material-0001"


def _key_bindings() -> str:
    return json.dumps(
        {
            "callback-2026-06": {
                "secret": "callback-v2-overlap-secret-material-001",
                "state": "overlap",
                "not_after": TIMESTAMP + 300,
            },
            KEY_ID: {
                "secret": ACTIVE_KEY_MATERIAL,
                "state": "active",
                "not_before": TIMESTAMP - 300,
            },
        }
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "target": "crm_reception_order",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_callback_v2",
        "run_id": "external_callback_v2",
        "idempotency_key": "idem_callback_v2",
        "payload_template": {"evidence_pack_id": "AF-128"},
        **overrides,
    }


@dataclass
class NonceStore:
    claimed: set[tuple[str, str]]

    def __init__(self) -> None:
        self.claimed = set()

    def claim(self, *, key_id: str, nonce: str, expires_at: int) -> bool:
        del expires_at
        binding = (key_id, nonce)
        if binding in self.claimed:
            return False
        self.claimed.add(binding)
        return True


def test_real_callback_binds_v2_headers_to_the_exact_http_request() -> None:
    client = RealExternalCallbackClient(
        callback_url="http://callback.example.test/callbacks/platform?mode=full&tag=a",
        key_bindings=_key_bindings(),
        active_key_id=KEY_ID,
        app_env="local",
        clock=lambda: TIMESTAMP,
        nonce_factory=lambda: NONCE,
    )
    captured: dict[str, Any] = {}

    def fake_request(body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        captured.update(body=body, headers=headers)
        return {
            "status_code": 202,
            "headers": {"X-Auris-Callback-Receipt-Id": "callback_receipt_v2"},
            "body": json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "callback_receipt_id": "callback_receipt_v2",
                        "receipt_url": (
                            "http://callback.example.test/receipts/callback_receipt_v2"
                        ),
                    },
                }
            ).encode(),
        }

    client._request = fake_request  # type: ignore[method-assign]

    result = client.send_signed_callback(_payload())

    assert result.status == "success"
    headers = captured["headers"]
    assert headers["X-Auris-Signature-Version"] == "v2"
    assert headers["X-Auris-Key-Id"] == KEY_ID
    assert headers["X-Auris-Timestamp"] == str(TIMESTAMP)
    assert headers["X-Auris-Nonce"] == NONCE
    assert headers["X-Auris-Signature"].startswith("v2=")
    assert headers["X-Auris-Signature-Mode"] == "hmac-sha256-v2"
    assert "X-Auris-Signature-Id" not in headers
    assert result.details["signature_key_id"] == KEY_ID
    assert "signature_id" not in result.details
    assert result.details["signature_version"] == "v2"
    assert result.details["callback_url"] == ("http://callback.example.test/callbacks/platform")

    request = CallbackSignatureRequest(
        method="POST",
        path="/callbacks/platform",
        query="mode=full&tag=a",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        idempotency_key="idem_callback_v2",
        timestamp=TIMESTAMP,
        nonce=NONCE,
        key_id=KEY_ID,
        body=captured["body"],
    )
    verified = verify_callback_signature(
        request,
        headers["X-Auris-Signature"],
        parse_callback_keyring(_key_bindings(), active_key_id=KEY_ID),
        now=TIMESTAMP,
        tolerance_seconds=300,
        nonce_store=NonceStore(),
    )
    assert verified.body_sha256 == hashlib.sha256(captured["body"]).hexdigest()


def test_production_client_rejects_legacy_single_secret_before_network() -> None:
    client = RealExternalCallbackClient(
        callback_url="https://callback.example.com/callbacks/platform",
        secret="legacy-callback-secret-material-32-bytes",
        legacy_hmac_enabled=True,
        app_env="production",
        allowed_hosts="callback.example.com",
    )
    opened = False

    def fail_if_opened(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal opened
        opened = True
        raise AssertionError("network must not be opened")

    client._perform_http_request = fail_if_opened  # type: ignore[method-assign]

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_SECURITY_REJECTED"
    assert result.retryable is False
    assert opened is False


def test_explicit_keyring_parser_preserves_overlap_verification() -> None:
    keyring = parse_callback_keyring(_key_bindings(), active_key_id=KEY_ID)

    assert keyring.active_key.key_id == KEY_ID
    assert (
        keyring.verification_key(key_id="callback-2026-06", timestamp=TIMESTAMP).key_id
        == "callback-2026-06"
    )


def test_runtime_factory_uses_explicit_keyring_instead_of_legacy_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(
        _env_file=None,
        app_env="local",
        auris_external_callback_adapter="real",
        external_callback_url="http://127.0.0.1:8089/callbacks/platform",
        external_callback_key_bindings=_key_bindings(),
        external_callback_active_key_id=KEY_ID,
        external_callback_legacy_hmac_enabled=False,
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: configured)

    client = adapter_module._default_external_callback_client()

    assert isinstance(client, RealExternalCallbackClient)
    assert client.keyring is not None
    assert client.keyring.active_key.key_id == KEY_ID
    assert client.legacy_hmac_enabled is False
