#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.callback_signature import (  # noqa: E402
    CallbackIdempotencyBinding,
    CallbackIdempotencyOutcome,
    CallbackSignatureError,
    CallbackSignatureRequest,
    decide_callback_idempotency,
    parse_callback_keyring,
    verify_callback_signature,
)


class CallbackState:
    def __init__(
        self,
        *,
        key_bindings: str,
        active_key_id: str,
        tolerance_seconds: int,
        receipt_log: Path | None,
        base_url: str,
        clock: Callable[[], int | float] | None = None,
    ) -> None:
        if not 1 <= tolerance_seconds <= 900:
            raise ValueError(
                "callback signature tolerance must be between 1 and 900 seconds"
            )
        self.keyring = parse_callback_keyring(
            key_bindings,
            active_key_id=active_key_id,
        )
        self.tolerance_seconds = tolerance_seconds
        self.receipt_log = receipt_log
        self.base_url = base_url.rstrip("/")
        self.receipts: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, CallbackIdempotencyBinding] = {}
        self._nonce_expirations: dict[tuple[str, str], int] = {}
        self._lock = threading.RLock()
        self.clock = clock or time.time

    def claim(self, *, key_id: str, nonce: str, expires_at: int) -> bool:
        """Atomically claim a nonce across concurrent fake receiver threads."""

        now = int(self.clock())
        binding = (key_id, nonce)
        with self._lock:
            expired = [
                key for key, expiry in self._nonce_expirations.items() if expiry < now
            ]
            for key in expired:
                del self._nonce_expirations[key]
            if binding in self._nonce_expirations:
                return False
            self._nonce_expirations[binding] = expires_at
            return True

    def receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.receipts.get(receipt_id)

    def accept_idempotent_receipt(
        self,
        *,
        idempotency_key: str,
        body: bytes,
        receipt: dict[str, Any],
    ) -> tuple[CallbackIdempotencyOutcome, dict[str, Any]]:
        candidate = CallbackIdempotencyBinding.from_body(
            idempotency_key=idempotency_key,
            body=body,
        )
        receipt_id = str(receipt["callback_receipt_id"])
        with self._lock:
            existing = self._idempotency.get(idempotency_key)
            outcome = decide_callback_idempotency(
                existing=existing, candidate=candidate
            )
            if outcome is CallbackIdempotencyOutcome.CONFLICT:
                return outcome, receipt
            if outcome is CallbackIdempotencyOutcome.REPLAY_ALLOWED:
                return outcome, self.receipts[receipt_id]
            self._idempotency[idempotency_key] = candidate
            self.receipts[receipt_id] = receipt
            if self.receipt_log:
                self.receipt_log.parent.mkdir(parents=True, exist_ok=True)
                with self.receipt_log.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n"
                    )
            return outcome, receipt


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _send_json(
    handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]
) -> None:
    data = _json_bytes(payload)
    receipt_id = payload.get("data", {}).get("callback_receipt_id")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    if receipt_id:
        handler.send_header("X-Auris-Callback-Receipt-Id", str(receipt_id))
    handler.end_headers()
    handler.wfile.write(data)


def _header(handler: BaseHTTPRequestHandler, name: str) -> str:
    return handler.headers.get(name, "")


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "AurisFakeCallback/1.0"

    @property
    def state(self) -> CallbackState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/healthz"}:
            _send_json(self, 200, {"status": "ok", "service": "fake-platform-callback"})
            return
        if parsed.path.startswith("/receipts/"):
            receipt_id = parsed.path.rsplit("/", 1)[-1]
            receipt = self.state.receipt(receipt_id)
            if not receipt:
                _send_json(self, 404, {"status": "error", "error": "receipt_not_found"})
                return
            _send_json(self, 200, {"status": "ok", "data": receipt})
            return
        _send_json(self, 404, {"status": "error", "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/callbacks"):
            _send_json(self, 404, {"status": "error", "error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        request_sha256 = hashlib.sha256(body).hexdigest()
        timestamp = _header(self, "X-Auris-Timestamp")
        nonce = _header(self, "X-Auris-Nonce")
        key_id = _header(self, "X-Auris-Key-Id")
        signature = _header(self, "X-Auris-Signature")
        signature_version = _header(self, "X-Auris-Signature-Version")
        signature_mode = _header(self, "X-Auris-Signature-Mode")
        tenant_id = _header(self, "X-Auris-Tenant-Id")
        project_id = _header(self, "X-Auris-Project-Id")
        idempotency_key = _header(self, "X-Auris-Idempotency-Key")
        try:
            if signature_version != "v2" or signature_mode != "hmac-sha256-v2":
                raise CallbackSignatureError("CALLBACK_SIGNATURE_INVALID")
            signed_request = CallbackSignatureRequest(
                method="POST",
                path=parsed.path,
                query=parsed.query,
                tenant_id=tenant_id,
                project_id=project_id,
                idempotency_key=idempotency_key,
                timestamp=int(timestamp),
                nonce=nonce,
                key_id=key_id,
                body=body,
            )
            verify_callback_signature(
                signed_request,
                signature,
                self.state.keyring,
                now=int(self.state.clock()),
                tolerance_seconds=self.state.tolerance_seconds,
                nonce_store=self.state,
            )
        except (CallbackSignatureError, TypeError, ValueError):
            _send_json(
                self,
                401,
                {
                    "status": "error",
                    "error": "signature_rejected",
                    "request_sha256": request_sha256,
                },
            )
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _send_json(self, 400, {"status": "error", "error": "invalid_json"})
            return
        if any(
            str(payload.get(field) or "") != expected
            for field, expected in (
                ("tenant_id", tenant_id),
                ("project_id", project_id),
                ("idempotency_key", idempotency_key),
            )
        ):
            _send_json(self, 400, {"status": "error", "error": "signed_scope_mismatch"})
            return
        run_id = str(_header(self, "X-Auris-Run-Id") or payload.get("run_id") or "")
        trace_id = str(
            _header(self, "X-Auris-Trace-Id") or payload.get("trace_id") or ""
        )
        receipt_id = f"callback_receipt_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        receipt_url = f"{self.state.base_url}/receipts/{quote(receipt_id, safe='')}"
        remote_trace_hash = hashlib.sha256((trace_id or run_id).encode()).hexdigest()
        remote_trace_id = f"remote_trace_{remote_trace_hash[:12]}"
        response_payload: dict[str, Any] = {
            "status": "ok",
            "data": {
                "callback_receipt_id": receipt_id,
                "remote_trace_id": remote_trace_id,
                "receipt_url": receipt_url,
                "request_sha256": request_sha256,
            },
        }
        response_sha256 = hashlib.sha256(_json_bytes(response_payload)).hexdigest()
        receipt = {
            "callback_receipt_id": receipt_id,
            "path": parsed.path,
            "method": "POST",
            "received_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "project_id": project_id,
            "trace_id": trace_id,
            "run_id": run_id,
            "target": payload.get("target"),
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "signature_key_id": key_id,
            "signature_mode": signature_mode,
            "signature_version": signature_version,
            "signature_timestamp": int(timestamp),
            "signature_nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            "signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
            "signature_valid": True,
            "remote_trace_id": remote_trace_id,
            "payload_keys": sorted((payload.get("payload") or {}).keys()),
        }
        outcome, receipt = self.state.accept_idempotent_receipt(
            idempotency_key=idempotency_key,
            body=body,
            receipt=receipt,
        )
        if outcome is CallbackIdempotencyOutcome.CONFLICT:
            _send_json(
                self,
                409,
                {
                    "status": "error",
                    "error": "idempotency_body_conflict",
                    "request_sha256": request_sha256,
                },
            )
            return
        response_payload["data"]["request_sha256"] = receipt["request_sha256"]
        _send_json(self, 202, response_payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake platform callback receiver for E2E."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument(
        "--secret",
        default="auris-local-callback-key-material-32-bytes",
        help="Local shortcut used to construct one active v2 key.",
    )
    parser.add_argument("--key-id", default="local-dev-callback")
    parser.add_argument(
        "--key-bindings",
        help="Explicit callback v2 keyring JSON; overrides --secret/--key-id.",
    )
    parser.add_argument("--active-key-id")
    parser.add_argument("--tolerance-seconds", type=int, default=300)
    parser.add_argument("--receipt-log")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_url = f"http://{args.host}:{args.port}"
    key_bindings = args.key_bindings or json.dumps(
        {
            args.key_id: {
                "secret": args.secret,
                "state": "active",
            }
        }
    )
    active_key_id = args.active_key_id or args.key_id
    server = ThreadingHTTPServer((args.host, args.port), CallbackHandler)
    server.state = CallbackState(  # type: ignore[attr-defined]
        key_bindings=key_bindings,
        active_key_id=active_key_id,
        tolerance_seconds=args.tolerance_seconds,
        receipt_log=Path(args.receipt_log) if args.receipt_log else None,
        base_url=base_url,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
