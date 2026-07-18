from __future__ import annotations

import hashlib
import json
import socket
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.services import adapters as adapter_module
from app.services.adapters import RealExternalCallbackClient

SECURE_PRODUCTION_SETTINGS = {
    "auth_provider": "signed",
    "allow_dev_auth": False,
    "auth_token_secret": "unit-auth-token-secret-32-characters",
    "audio_playback_grant_secret": "unit-playback-secret-32-characters",
    "completion_receipt_secret": "unit-completion-secret-32-characters",
    "cors_allowed_origins": "https://auris.example.com",
    "trusted_hosts": "auris.example.com",
}
CALLBACK_HOST = "callback.example.com"
CALLBACK_URL = f"https://{CALLBACK_HOST}/callbacks/platform"
CALLBACK_SECRET = "unit-callback-secret-at-least-32-characters"
RECEIPT_ID = "callback_receipt_123"


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "target": "crm_reception_order",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_callback_security",
        "run_id": "external_callback_security",
        "idempotency_key": "idem_callback_security",
        "payload_template": {"evidence_pack_id": "AF-128"},
        **overrides,
    }


def _response(
    *,
    receipt_url: str | None = f"https://{CALLBACK_HOST}/receipts/{RECEIPT_ID}",
    receipt_id: str = RECEIPT_ID,
    status_code: int = 202,
    extra_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "callback_receipt_id": receipt_id,
        "remote_trace_id": "remote_trace_123",
        **(extra_data or {}),
    }
    if receipt_url is not None:
        data["receipt_url"] = receipt_url
    return {
        "status_code": status_code,
        "headers": {"X-Auris-Callback-Receipt-Id": receipt_id},
        "body": json.dumps({"status": "ok", "data": data}).encode("utf-8"),
    }


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(
        host: str,
        port: int,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == CALLBACK_HOST
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", port),
            )
        ]

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", fake_getaddrinfo)


def _production_client(
    *,
    callback_url: str = CALLBACK_URL,
    allowed_hosts: str = CALLBACK_HOST,
) -> RealExternalCallbackClient:
    return RealExternalCallbackClient(
        callback_url=callback_url,
        secret=CALLBACK_SECRET,
        app_env="prod",
        allowed_hosts=allowed_hosts,
    )


def test_production_settings_require_https_and_an_explicit_callback_allowlist() -> None:
    common = {
        **SECURE_PRODUCTION_SETTINGS,
        "auris_external_callback_adapter": "real",
        "external_callback_secret": CALLBACK_SECRET,
    }

    with pytest.raises(ValidationError, match="EXTERNAL_CALLBACK_URL must use HTTPS"):
        Settings(
            app_env="prod",
            external_callback_url="http://callback.example.com/callbacks/platform",
            external_callback_allowed_hosts=CALLBACK_HOST,
            **common,
        )

    with pytest.raises(ValidationError, match="EXTERNAL_CALLBACK_ALLOWED_HOSTS"):
        Settings(
            app_env="prod",
            external_callback_url=CALLBACK_URL,
            external_callback_allowed_hosts="",
            **common,
        )


def test_production_settings_reject_callback_host_outside_allowlist() -> None:
    with pytest.raises(ValidationError, match="callback host must be present"):
        Settings(
            app_env="production",
            auris_external_callback_adapter="real",
            external_callback_url=CALLBACK_URL,
            external_callback_secret=CALLBACK_SECRET,
            external_callback_allowed_hosts="receipts.example.com",
            **SECURE_PRODUCTION_SETTINGS,
        )


def test_production_callback_rejects_plain_http_without_opening_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client(callback_url="http://callback.example.com/callbacks/platform")
    opened = False

    def fail_if_opened(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal opened
        opened = True
        raise AssertionError("network must not be opened")

    monkeypatch.setattr(client, "_perform_http_request", fail_if_opened)

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_SECURITY_REJECTED"
    assert result.retryable is False
    assert opened is False


@pytest.mark.parametrize(
    ("callback_url", "allowed_host"),
    [
        ("https://127.0.0.1/callbacks/platform", "127.0.0.1"),
        ("https://[::1]/callbacks/platform", "::1"),
        ("https://169.254.169.254/latest/meta-data", "169.254.169.254"),
        ("https://10.20.30.40/callbacks/platform", "10.20.30.40"),
    ],
)
def test_production_callback_rejects_non_public_literal_addresses(
    callback_url: str,
    allowed_host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client(callback_url=callback_url, allowed_hosts=allowed_host)
    monkeypatch.setattr(
        client,
        "_perform_http_request",
        lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
    )

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_SECURITY_REJECTED"
    assert result.retryable is False


def test_production_callback_rejects_dns_answers_that_include_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def private_getaddrinfo(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            )
        ]

    monkeypatch.setattr(adapter_module.socket, "getaddrinfo", private_getaddrinfo)
    client = _production_client()
    monkeypatch.setattr(
        client,
        "_perform_http_request",
        lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
    )

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_SECURITY_REJECTED"
    assert result.retryable is False


def test_production_callback_enforces_exact_host_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client(allowed_hosts="other.example.com")
    monkeypatch.setattr(
        client,
        "_perform_http_request",
        lambda *_args, **_kwargs: pytest.fail("network must not be opened"),
    )

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_SECURITY_REJECTED"


def test_callback_does_not_follow_redirects(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client()
    calls: list[tuple[str, str]] = []

    def redirect(target: Any, *, method: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append((method, target.url))
        return {
            "status_code": 302,
            "headers": {"Location": "http://169.254.169.254/latest/meta-data"},
            "body": b"",
        }

    monkeypatch.setattr(client, "_perform_http_request", redirect)

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_REDIRECT_REJECTED"
    assert result.retryable is False
    assert calls == [("POST", CALLBACK_URL)]


def test_callback_payload_uses_shared_redaction_before_signing(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client()
    captured: dict[str, Any] = {}
    payload_template = {
        "api_key": "unit-callback-sensitive-value",
        "customer_phone": "13800138000",
        "transcript": "客户要求回拨并提供身份证号",
        "nested": {"authorization": "Bearer callback-secret-token"},
        "evidence_pack_id": "AF-128",
    }

    def accept(
        _target: Any,
        *,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        captured.update(method=method, body=body, headers=headers)
        return _response()

    monkeypatch.setattr(client, "_perform_http_request", accept)

    result = client.send_signed_callback(_payload(payload_template=payload_template))

    assert result.status == "success"
    body = json.loads(captured["body"])
    assert body["payload"] == {
        "api_key": "[REDACTED]",
        "customer_phone": "[REDACTED_PII]",
        "transcript": "[REDACTED_TEXT length=13]",
        "nested": {"authorization": "[REDACTED]"},
        "evidence_pack_id": "AF-128",
    }
    assert payload_template["customer_phone"] == "13800138000"
    assert result.details["request_sha256"] == hashlib.sha256(captured["body"]).hexdigest()


@pytest.mark.parametrize(
    "receipt_url",
    [
        None,
        f"http://{CALLBACK_HOST}/receipts/{RECEIPT_ID}",
        f"https://127.0.0.1/receipts/{RECEIPT_ID}",
        f"https://evil.example/receipts/{RECEIPT_ID}",
        f"https://{CALLBACK_HOST}@127.0.0.1/receipts/{RECEIPT_ID}",
        f"https://{CALLBACK_HOST}/other/{RECEIPT_ID}",
        f"https://{CALLBACK_HOST}/receipts/{RECEIPT_ID}?next=http://127.0.0.1",
    ],
)
def test_callback_rejects_untrusted_remote_receipt_url(
    receipt_url: str | None,
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client()
    monkeypatch.setattr(
        client,
        "_perform_http_request",
        lambda *_args, **_kwargs: _response(receipt_url=receipt_url),
    )

    result = client.send_signed_callback(_payload())

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_RECEIPT_URL_INVALID"
    assert result.retryable is False
    assert receipt_url not in result.details.values()


def test_reconcile_rejects_receipt_that_only_self_attests_mismatched_scope(
    public_dns: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _production_client()
    payload = _payload()
    expected_id = (
        "callback_receipt_"
        + hashlib.sha256(payload["idempotency_key"].encode("utf-8")).hexdigest()[:16]
    )

    def forged_receipt(
        _target: Any,
        *,
        method: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert method == "GET"
        return {
            "status_code": 200,
            "headers": {},
            "body": json.dumps(
                {
                    "status": "ok",
                    "data": {
                        "callback_receipt_id": expected_id,
                        "tenant_id": "forged_tenant",
                        "project_id": payload["project_id"],
                        "trace_id": payload["trace_id"],
                        "run_id": payload["run_id"],
                        "target": payload["target"],
                        "idempotency_key": payload["idempotency_key"],
                        "request_sha256": "0" * 64,
                    },
                }
            ).encode("utf-8"),
        }

    monkeypatch.setattr(client, "_perform_http_request", forged_receipt)

    result = client.reconcile_callback(payload)

    assert result.status == "failed"
    assert result.error_code == "EXTERNAL_CALLBACK_RECEIPT_INVALID"
    assert result.retryable is False


def test_local_mode_keeps_loopback_http_callback_for_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback_url = "http://127.0.0.1:8089/callbacks/platform"
    receipt_url = f"http://127.0.0.1:8089/receipts/{RECEIPT_ID}"
    client = RealExternalCallbackClient(
        callback_url=callback_url,
        secret="auris-dev-callback-secret",
        app_env="local",
        allowed_hosts="",
    )
    monkeypatch.setattr(
        client,
        "_perform_http_request",
        lambda *_args, **_kwargs: _response(receipt_url=receipt_url),
    )

    result = client.send_signed_callback(_payload())

    assert result.status == "success"
    assert result.details["receipt_url"] == receipt_url
