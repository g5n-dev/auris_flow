#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


class CallbackState:
    def __init__(self, *, secret: str, receipt_log: Path | None, base_url: str) -> None:
        self.secret = secret
        self.receipt_log = receipt_log
        self.base_url = base_url.rstrip("/")
        self.receipts: dict[str, dict[str, Any]] = {}


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


def _signature(secret: str, timestamp: str, request_sha256: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{request_sha256}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


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
            receipt = self.state.receipts.get(receipt_id)
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
        raw_signature = _header(self, "X-Auris-Signature")
        expected_signature = _signature(self.state.secret, timestamp, request_sha256)
        actual_signature = raw_signature.removeprefix("sha256=")
        signature_valid = bool(timestamp) and hmac.compare_digest(
            actual_signature, expected_signature
        )
        if not signature_valid:
            _send_json(
                self,
                401,
                {
                    "status": "error",
                    "error": "signature_invalid",
                    "request_sha256": request_sha256,
                },
            )
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _send_json(self, 400, {"status": "error", "error": "invalid_json"})
            return
        run_id = str(_header(self, "X-Auris-Run-Id") or payload.get("run_id") or "")
        trace_id = str(
            _header(self, "X-Auris-Trace-Id") or payload.get("trace_id") or ""
        )
        idempotency_key = str(
            _header(self, "X-Auris-Idempotency-Key")
            or payload.get("idempotency_key")
            or request_sha256
        )
        receipt_id = f"callback_receipt_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        receipt_url = f"{self.state.base_url}/receipts/{quote(receipt_id, safe='')}"
        remote_trace_id = f"remote_trace_{hashlib.sha256((trace_id or run_id).encode()).hexdigest()[:12]}"
        response_payload = {
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
            "tenant_id": _header(self, "X-Auris-Tenant-Id") or payload.get("tenant_id"),
            "project_id": _header(self, "X-Auris-Project-Id")
            or payload.get("project_id"),
            "trace_id": trace_id,
            "run_id": run_id,
            "target": payload.get("target"),
            "idempotency_key": idempotency_key,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "signature_id": _header(self, "X-Auris-Signature-Id"),
            "signature_mode": _header(self, "X-Auris-Signature-Mode"),
            "signature_sha256": hashlib.sha256(
                actual_signature.encode("utf-8")
            ).hexdigest(),
            "signature_valid": True,
            "remote_trace_id": remote_trace_id,
            "payload_keys": sorted((payload.get("payload") or {}).keys()),
        }
        self.state.receipts[receipt_id] = receipt
        if self.state.receipt_log:
            self.state.receipt_log.parent.mkdir(parents=True, exist_ok=True)
            with self.state.receipt_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n"
                )
        _send_json(self, 202, response_payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fake platform callback receiver for E2E."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--secret", default="auris-dev-callback-secret")
    parser.add_argument("--receipt-log")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    base_url = f"http://{args.host}:{args.port}"
    server = ThreadingHTTPServer((args.host, args.port), CallbackHandler)
    server.state = CallbackState(  # type: ignore[attr-defined]
        secret=args.secret,
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
