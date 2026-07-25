from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.core.callback_signature import (
    CallbackSignatureRequest,
    parse_callback_keyring,
    sign_callback,
)
from app.services.adapters import RealExternalCallbackClient

TIMESTAMP = 1_784_352_000
KEY_ID = "callback-fake-2026-07"
KEY_MATERIAL = "callback-fake-key-material-2026-07-active!"


def _load_fake_server_module() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts/fake_platform_callback_server.py"
    spec = importlib.util.spec_from_file_location("auris_fake_callback_server", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bindings() -> str:
    return json.dumps(
        {
            "callback-fake-2026-06": {
                "secret": "callback-fake-key-material-2026-06-overlap!",
                "state": "overlap",
                "not_after": TIMESTAMP + 300,
            },
            KEY_ID: {
                "secret": KEY_MATERIAL,
                "state": "active",
            },
        }
    )


@contextmanager
def _running_server(
    module: ModuleType,
    *,
    bindings: str | None = None,
) -> Iterator[tuple[Any, str]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.CallbackHandler)
    port = int(server.server_address[1])
    base_url = f"http://127.0.0.1:{port}"
    state = module.CallbackState(
        key_bindings=bindings or _bindings(),
        active_key_id=KEY_ID,
        tolerance_seconds=60,
        receipt_log=None,
        base_url=base_url,
        clock=lambda: TIMESTAMP,
    )
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _client(base_url: str, *, nonces: Iterator[str]) -> RealExternalCallbackClient:
    return RealExternalCallbackClient(
        callback_url=f"{base_url}/callbacks/platform?source=e2e",
        key_bindings=_bindings(),
        active_key_id=KEY_ID,
        app_env="local",
        clock=lambda: TIMESTAMP,
        nonce_factory=lambda: next(nonces),
    )


def _payload(*, value: str = "one") -> dict[str, Any]:
    return {
        "target": "crm_reception_order",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_fake_callback",
        "run_id": "fake_callback_run",
        "idempotency_key": "fake_callback_idempotency",
        "payload_template": {"value": value},
    }


def _post_with_old_key(base_url: str, *, nonce: str) -> int:
    body = json.dumps(_payload(), ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    old_key_id = "callback-fake-2026-06"
    old_signing_bindings = json.dumps(
        {
            old_key_id: {
                "secret": "callback-fake-key-material-2026-06-overlap!",
                "state": "active",
            }
        }
    )
    signed_request = CallbackSignatureRequest(
        method="POST",
        path="/callbacks/platform",
        query="source=e2e",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        idempotency_key="fake_callback_idempotency",
        timestamp=TIMESTAMP,
        nonce=nonce,
        key_id=old_key_id,
        body=body,
    )
    signature = sign_callback(
        signed_request,
        parse_callback_keyring(old_signing_bindings, active_key_id=old_key_id),
    )
    request = Request(
        f"{base_url}/callbacks/platform?source=e2e",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Auris-Signature-Version": "v2",
            "X-Auris-Signature": signature,
            "X-Auris-Signature-Mode": "hmac-sha256-v2",
            "X-Auris-Key-Id": old_key_id,
            "X-Auris-Timestamp": str(TIMESTAMP),
            "X-Auris-Nonce": nonce,
            "X-Auris-Tenant-Id": "aurora_auto",
            "X-Auris-Project-Id": "sales_qa",
            "X-Auris-Idempotency-Key": "fake_callback_idempotency",
        },
    )
    try:
        with urlopen(request, timeout=2) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)


def test_fake_receiver_atomically_rejects_a_replayed_nonce() -> None:
    module = _load_fake_server_module()
    with _running_server(module) as (state, base_url):
        client = _client(
            base_url,
            nonces=iter(("fake-callback-replay-nonce-01", "fake-callback-replay-nonce-01")),
        )

        accepted = client.send_signed_callback(_payload())
        replayed = client.send_signed_callback(_payload())

    assert accepted.status == "success"
    assert replayed.status == "failed"
    assert replayed.details["status_code"] == 401
    assert len(state.receipts) == 1
    receipt = next(iter(state.receipts.values()))
    assert receipt["signature_key_id"] == KEY_ID
    assert receipt["signature_mode"] == "hmac-sha256-v2"
    assert receipt["signature_valid"] is True


def test_fake_receiver_returns_409_for_same_idempotency_key_with_different_body() -> None:
    module = _load_fake_server_module()
    with _running_server(module) as (state, base_url):
        client = _client(
            base_url,
            nonces=iter(("fake-callback-idempotency-01", "fake-callback-idempotency-02")),
        )

        accepted = client.send_signed_callback(_payload(value="one"))
        conflicted = client.send_signed_callback(_payload(value="two"))

    assert accepted.status == "success"
    assert conflicted.status == "failed"
    assert conflicted.details["status_code"] == 409
    assert len(state.receipts) == 1
    receipt = next(iter(state.receipts.values()))
    assert receipt["request_sha256"] == accepted.details["request_sha256"]


def test_callback_recovers_remote_execution_after_transport_response_is_lost() -> None:
    module = _load_fake_server_module()
    with _running_server(module) as (state, base_url):
        state.drop_next_response_after_persist = True
        client = _client(
            base_url,
            nonces=iter(("fake-callback-response-loss-01",)),
        )
        payload = _payload()

        sent = client.send_signed_callback(payload)

        assert sent.status == "failed"
        assert sent.retryable is True
        assert len(state.receipts) == 1
        remote_receipt = next(iter(state.receipts.values()))
        assert sent.details["delivery_id"].startswith("callback_delivery_")
        assert remote_receipt["delivery_id"] == sent.details["delivery_id"]
        assert remote_receipt["callback_receipt_id"] != sent.details["delivery_id"]

        reconciled = client.reconcile_callback(payload)

    assert reconciled.status == "success"
    assert reconciled.details["reconciled"] is True
    assert reconciled.details["delivery_id"] == sent.details["delivery_id"]
    assert reconciled.details["callback_receipt_id"] == remote_receipt["callback_receipt_id"]


def test_fake_receiver_accepts_overlap_key_and_rejects_it_after_retirement() -> None:
    module = _load_fake_server_module()
    with _running_server(module) as (_state, base_url):
        overlap_status = _post_with_old_key(
            base_url,
            nonce="fake-callback-overlap-nonce-01",
        )

    retired_bindings = json.dumps(
        {
            "callback-fake-2026-06": {
                "secret": "callback-fake-key-material-2026-06-overlap!",
                "state": "retired",
            },
            KEY_ID: {
                "secret": KEY_MATERIAL,
                "state": "active",
            },
        }
    )
    with _running_server(module, bindings=retired_bindings) as (_state, base_url):
        retired_status = _post_with_old_key(
            base_url,
            nonce="fake-callback-retired-nonce-01",
        )

    assert overlap_status == 202
    assert retired_status == 401
