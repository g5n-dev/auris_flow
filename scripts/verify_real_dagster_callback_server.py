#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import threading
from collections.abc import Mapping, MutableSet
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


SIGNATURE_VERSION = "auris-completion-v1"
MAX_BODY_BYTES = 1_048_576
MAX_CLOCK_SKEW_SECONDS = 300
_PATH = re.compile(
    r"^/api/v1/runs/(?P<run_id>[A-Za-z0-9][A-Za-z0-9._:-]{0,255})/"
    r"external-completion-receipts$"
)
_SAFE_HEADER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class GateRequestError(ValueError):
    """A secret-free completion request validation failure."""


def _header(headers: Mapping[str, str], name: str) -> str:
    expected = name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected:
            normalized = str(value).strip()
            if normalized:
                return normalized
    raise GateRequestError(f"required header is missing: {name}")


def _binding_for_request(
    keyring: Mapping[str, object],
    *,
    key_id: str,
    source: str,
    tenant_id: str,
    project_id: str,
) -> str:
    raw_binding = keyring.get(key_id)
    if not isinstance(raw_binding, Mapping):
        raise GateRequestError("completion key id is not configured")
    secret = raw_binding.get("secret")
    sources = raw_binding.get("allowed_sources")
    scopes = raw_binding.get("allowed_scopes")
    if not isinstance(secret, str) or len(secret) < 32:
        raise GateRequestError("completion key binding is invalid")
    if not isinstance(sources, list) or source not in sources:
        raise GateRequestError("completion source is not allowed")
    if not isinstance(scopes, list) or not any(
        isinstance(scope, Mapping)
        and scope.get("tenant_id") == tenant_id
        and scope.get("project_id") == project_id
        for scope in scopes
    ):
        raise GateRequestError("completion scope is not allowed")
    return secret


def verify_completion_request(
    *,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    keyring: Mapping[str, object],
    now: datetime,
    seen_nonces: MutableSet[str],
) -> dict[str, Any]:
    match = _PATH.fullmatch(path)
    if match is None:
        raise GateRequestError("completion path is invalid")
    if not body or len(body) > MAX_BODY_BYTES:
        raise GateRequestError("completion body size is invalid")

    tenant_id = _header(headers, "X-Tenant-Id")
    project_id = _header(headers, "X-Project-Id")
    source = _header(headers, "X-Auris-Source")
    key_id = _header(headers, "X-Auris-Key-Id")
    timestamp = _header(headers, "X-Auris-Timestamp")
    nonce = _header(headers, "X-Auris-Nonce")
    idempotency_key = _header(headers, "Idempotency-Key")
    signature_mode = _header(headers, "X-Auris-Signature-Mode")
    signature = _header(headers, "X-Auris-Signature")
    if source != "dagster" or signature_mode != "hmac-sha256":
        raise GateRequestError("completion signature mode or source is invalid")
    if any(
        _SAFE_HEADER.fullmatch(value) is None
        for value in (tenant_id, project_id, source, key_id, nonce, idempotency_key)
    ):
        raise GateRequestError("completion header value is invalid")
    if nonce in seen_nonces:
        raise GateRequestError("completion nonce replay detected")

    try:
        signed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateRequestError("completion timestamp is invalid") from exc
    if signed_at.tzinfo is None or now.tzinfo is None:
        raise GateRequestError("completion timestamp must include timezone")
    if abs((now.astimezone(UTC) - signed_at.astimezone(UTC)).total_seconds()) > (
        MAX_CLOCK_SKEW_SECONDS
    ):
        raise GateRequestError("completion timestamp is outside the allowed window")

    secret = _binding_for_request(
        keyring,
        key_id=key_id,
        source=source,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    body_sha256 = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        [
            SIGNATURE_VERSION,
            "POST",
            path,
            "",
            tenant_id,
            project_id,
            idempotency_key,
            timestamp,
            nonce,
            key_id,
            source,
            body_sha256,
        ]
    )
    expected = (
        "sha256="
        + hmac.new(
            secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(signature, expected):
        raise GateRequestError("completion signature is invalid")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GateRequestError("completion body is invalid") from exc
    if not isinstance(payload, dict):
        raise GateRequestError("completion body must be an object")
    if payload.get("adapter") != "dagster" or payload.get("source") != "dagster":
        raise GateRequestError("completion payload source is invalid")
    if payload.get("status") not in {"success", "failed"}:
        raise GateRequestError("completion payload status is invalid")
    external_id = payload.get("external_id")
    receipt_id = payload.get("completion_receipt_id")
    if (
        not isinstance(external_id, str)
        or not external_id
        or receipt_id != f"dagster:{external_id}"
        or idempotency_key != f"dagster-completion:{external_id}"
    ):
        raise GateRequestError("completion external id binding is invalid")

    seen_nonces.add(nonce)
    return {
        **payload,
        "run_id": match.group("run_id"),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "trace_id": _header(headers, "X-Trace-Id"),
        "key_id": key_id,
        "nonce": nonce,
        "body_sha256": body_sha256,
    }


class GateState:
    def __init__(
        self,
        *,
        keyring: Mapping[str, object],
    ) -> None:
        self.keyring = keyring
        self.receipts: list[dict[str, Any]] = []
        self.seen_nonces: set[str] = set()
        self.lock = threading.Lock()


class GateCallbackHandler(BaseHTTPRequestHandler):
    server_version = "AurisRealDagsterGateCallback/1.0"

    @property
    def state(self) -> GateState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_json(self, status: int, payload: Mapping[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "service": "dagster-gate-callback"})
            return
        if self.path == "/receipts":
            with self.state.lock:
                receipts = list(self.state.receipts)
            self._send_json(200, {"data": receipts, "count": len(receipts)})
            return
        self._send_json(404, {"error": {"code": "NOT_FOUND"}})

    def do_POST(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._send_json(400, {"error": {"code": "INVALID_BODY_SIZE"}})
            return
        body = self.rfile.read(content_length)
        try:
            with self.state.lock:
                receipt = verify_completion_request(
                    path=self.path,
                    headers=dict(self.headers.items()),
                    body=body,
                    keyring=self.state.keyring,
                    now=datetime.now(UTC),
                    seen_nonces=self.state.seen_nonces,
                )
                receipt["received_at"] = datetime.now(UTC).isoformat()
                self.state.receipts.append(receipt)
        except GateRequestError:
            self._send_json(403, {"error": {"code": "INVALID_COMPLETION_SIGNATURE"}})
            return

        self._send_json(
            200,
            {
                "data": {
                    "accepted": True,
                    "completion_receipt_id": receipt["completion_receipt_id"],
                }
            },
        )


def _load_keyring(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Dagster gate callback keyring is invalid") from exc
    if not isinstance(payload, dict) or not payload:
        raise SystemExit("Dagster gate callback keyring must be a non-empty object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--keyring-file", default="/run/secrets/completion_receipt_key_bindings"
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), GateCallbackHandler)
    server.daemon_threads = True
    server.state = GateState(  # type: ignore[attr-defined]
        keyring=_load_keyring(Path(args.keyring_file)),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
