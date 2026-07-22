#!/usr/bin/env python3
"""Hardened HTTPS protocol endpoints used only by the production path gate.

The embedding endpoint is a small reference semantic protocol implementation;
it is deliberately not a model-quality benchmark.  The callback endpoint uses
the product callback-signature primitives and an atomic in-memory replay store.
Neither endpoint logs or returns credentials, request bodies, cookies or raw
authorization headers.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.core.callback_signature import (
    CallbackIdempotencyBinding,
    CallbackNonceReplayStore,
    CallbackSignatureError,
    CallbackSignatureRequest,
    decide_callback_idempotency,
    parse_callback_keyring,
    verify_callback_signature,
)

MAX_BODY_BYTES = 1024 * 1024
CALLBACK_TOLERANCE_SECONDS = 300
AUDIO_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "execution_contract",
        "execution_envelope_sha256",
        "tenant_id",
        "project_id",
        "trace_id",
        "run_id",
        "dispatch_idempotency_key",
        "outbox_fencing_token",
        "deadline_at",
        "audio_session_id",
        "recording_id",
        "input_object",
        "inference",
        "capabilities",
    }
)
AUDIO_INPUT_FIELDS = frozenset(
    {
        "storage_object_id",
        "storage_provider",
        "bucket",
        "object_key",
        "version_id",
        "content_sha256",
        "content_length",
        "content_type",
    }
)


class GateSupportError(RuntimeError):
    pass


def _read_secret(path: str, *, label: str) -> str:
    candidate = Path(path)
    if not candidate.is_file() or candidate.is_symlink():
        raise GateSupportError(f"{label} file is unavailable")
    value = candidate.read_text(encoding="utf-8").strip()
    if len(value) < 32 or len(value) > 65536:
        raise GateSupportError(f"{label} file has an invalid value")
    return value


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def reference_semantic_vector(text: str, *, dimension: int) -> list[float]:
    """Map a few semantic concepts without feature hashing or quality claims."""

    if not isinstance(text, str) or not text.strip() or not 1 <= dimension <= 64:
        raise GateSupportError("reference semantic input is invalid")
    normalized = text.casefold()
    concept_terms = (
        ("销售", "政策", "报价", "价格", "discount", "pricing", "sales"),
        ("质检", "证据", "复核", "quality", "evidence", "review"),
        ("客户", "接待", "沟通", "customer", "conversation"),
        ("知识", "文档", "faq", "knowledge", "document"),
        ("音频", "语音", "录音", "audio", "speech"),
        ("标签", "标注", "label", "annotation"),
        ("评测", "指标", "evaluation", "metric"),
        ("回调", "任务", "callback", "task"),
    )
    values = [0.0] * dimension
    for index, terms in enumerate(concept_terms[:dimension]):
        values[index] = float(sum(normalized.count(term) for term in terms))
    if not any(values):
        # Unknown text is intentionally represented by one neutral direction;
        # this is not a feature hash and carries no model-quality assertion.
        values[-1] = 1.0
    magnitude = math.sqrt(sum(value * value for value in values)) or 1.0
    return [round(value / magnitude, 8) for value in values]


def reference_audio_response(
    body: bytes,
    *,
    provider: str,
    model: str,
    claimed_request_sha256: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate and answer the protocol contract without claiming model quality."""

    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != AUDIO_REQUEST_FIELDS:
        raise GateSupportError("audio provider request envelope is invalid")
    input_object = payload.get("input_object")
    inference = payload.get("inference")
    capabilities = payload.get("capabilities")
    if (
        payload.get("schema_version") != "auris-flow-audio-provider-request-v1"
        or payload.get("execution_contract") != "auris-flow-audio-intelligence-v1"
        or not isinstance(input_object, dict)
        or set(input_object) != AUDIO_INPUT_FIELDS
        or not isinstance(inference, dict)
        or set(inference) != {"provider", "model"}
        or inference.get("provider") != provider
        or inference.get("model") != model
        or not isinstance(capabilities, list)
        or not capabilities
        or len(set(capabilities)) != len(capabilities)
        or any(
            capability not in {"vad", "asr", "diarization", "voiceprint", "quality"}
            for capability in capabilities
        )
    ):
        raise GateSupportError("audio provider request contract is invalid")
    request_sha256 = _sha256(body)
    if (
        claimed_request_sha256 != request_sha256
        or idempotency_key
        != f"audio-inference:{payload.get('dispatch_idempotency_key', '')}"
    ):
        raise GateSupportError("audio provider request binding is invalid")
    transcript = (
        {
            "language": "zh-CN",
            "text": "reference protocol transcript",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 640,
                    "speaker": "speaker-1",
                    "text": "reference protocol transcript",
                    "confidence": 0.99,
                }
            ],
        }
        if "asr" in capabilities
        else None
    )
    analyses = [
        {
            "capability": capability,
            "summary": "reference protocol result",
            "score": 0.99,
            "labels": [{"label": "reference", "score": 0.99}],
        }
        for capability in capabilities
        if capability != "asr"
    ]
    result = {"transcript": transcript, "analyses": analyses}
    response = {
        **{key: value for key, value in payload.items() if key != "schema_version"},
        "schema_version": "auris-flow-audio-provider-response-v1",
        "request_sha256": request_sha256,
        "result": result,
        "result_sha256": _sha256(_canonical_bytes(result)),
    }
    record = {
        "request_sha256": request_sha256,
        "result_sha256": response["result_sha256"],
        "provider": provider,
        "model": model,
        "capabilities": list(capabilities),
    }
    return response, record


class AtomicNonceStore(CallbackNonceReplayStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claims: dict[tuple[str, str], int] = {}

    def claim(self, *, key_id: str, nonce: str, expires_at: int) -> bool:
        now = int(time.time())
        with self._lock:
            self._claims = {
                key: expiry for key, expiry in self._claims.items() if expiry >= now
            }
            key = (key_id, nonce)
            if key in self._claims:
                return False
            self._claims[key] = expires_at
            return True


@dataclass
class SupportState:
    mode: str
    control_secret: str = field(repr=False)
    embedding_api_key: str = field(default="", repr=False)
    embedding_model: str = ""
    embedding_dimension: int = 8
    audio_api_token: str = field(default="", repr=False)
    audio_provider: str = ""
    audio_model: str = ""
    callback_keyring: Any | None = field(default=None, repr=False)
    nonce_store: AtomicNonceStore = field(default_factory=AtomicNonceStore, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    embedding_requests: list[dict[str, Any]] = field(default_factory=list)
    audio_requests: list[dict[str, Any]] = field(default_factory=list)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency: dict[str, CallbackIdempotencyBinding] = field(default_factory=dict)
    last_callback: tuple[CallbackSignatureRequest, str] | None = field(
        default=None, repr=False
    )
    timeout_next: bool = False
    replay_rejected: bool = False
    replay_error_code: str | None = None


class GateSupportHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AurisProductionGate"
    sys_version = ""

    @property
    def state(self) -> SupportState:
        return self.server.state  # type: ignore[attr-defined,no-any-return]

    def log_message(self, _format: str, *_args: object) -> None:
        return None

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = _canonical_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _body(self) -> bytes:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise GateSupportError("request body length is invalid") from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise GateSupportError("request body exceeds the gate limit")
        body = self.rfile.read(length)
        if len(body) != length:
            raise GateSupportError("request body is incomplete")
        return body

    def _authorized_control(self) -> bool:
        supplied = self.headers.get("X-Auris-Gate-Control", "")
        return bool(supplied) and hmac.compare_digest(
            supplied, self.state.control_secret
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz" and not parsed.query:
            self._send(200, {"status": "ok", "mode": self.state.mode})
            return
        if self.state.mode == "callback" and parsed.path.startswith("/receipts/"):
            receipt_id = parsed.path.removeprefix("/receipts/")
            with self.state.lock:
                receipt = self.state.receipts.get(receipt_id)
            if receipt is None:
                self._send(404, {"status": "not_found"})
            else:
                self._send(200, {"status": "ok", "data": receipt})
            return
        if not self._authorized_control():
            self._send(404, {"status": "not_found"})
            return
        if parsed.path == "/proofs" and not parsed.query:
            self._send(200, {"status": "ok", "data": self._proofs()})
            return
        self._send(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        try:
            if self.state.mode == "embedding" and parsed.path == "/v1/embeddings":
                self._embedding()
                return
            if (
                self.state.mode == "embedding"
                and parsed.path == "/v1/audio-intelligence"
            ):
                self._audio_inference()
                return
            if self.state.mode == "callback" and parsed.path == "/callbacks/platform":
                self._callback(parsed.query)
                return
            if parsed.path == "/control/timeout-next" and self._authorized_control():
                with self.state.lock:
                    self.state.timeout_next = True
                self._send(200, {"status": "armed"})
                return
            if parsed.path == "/control/replay-last" and self._authorized_control():
                self._replay_last()
                return
        except (
            GateSupportError,
            CallbackSignatureError,
            ValueError,
            json.JSONDecodeError,
        ):
            self._send(400, {"status": "rejected"})
            return
        self._send(404, {"status": "not_found"})

    def _embedding(self) -> None:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.state.embedding_api_key}"
        if not hmac.compare_digest(supplied, expected):
            self._send(401, {"status": "unauthorized"})
            return
        body = self._body()
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "input",
            "model",
            "input_type",
        }:
            raise GateSupportError("embedding payload is invalid")
        inputs = payload.get("input")
        purpose = payload.get("input_type")
        if (
            not isinstance(inputs, list)
            or len(inputs) != 1
            or not isinstance(inputs[0], str)
            or payload.get("model") != self.state.embedding_model
            or purpose not in {"document", "query"}
        ):
            raise GateSupportError("embedding request contract is invalid")
        vector = reference_semantic_vector(
            inputs[0], dimension=self.state.embedding_dimension
        )
        record = {
            "request_sha256": _sha256(body),
            "input_sha256": _sha256(inputs[0].strip().encode("utf-8")),
            "purpose": purpose,
            "dimension": len(vector),
            "tls": True,
        }
        with self.state.lock:
            self.state.embedding_requests.append(record)
        self._send(
            200,
            {
                "model": self.state.embedding_model,
                "data": [{"index": 0, "embedding": vector}],
                "usage": {"input_count": 1},
            },
        )

    def _audio_inference(self) -> None:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.state.audio_api_token}"
        if not hmac.compare_digest(supplied, expected):
            self._send(401, {"status": "unauthorized"})
            return
        body = self._body()
        response, record = reference_audio_response(
            body,
            provider=self.state.audio_provider,
            model=self.state.audio_model,
            claimed_request_sha256=self.headers.get("X-Auris-Request-SHA256", ""),
            idempotency_key=self.headers.get("Idempotency-Key", ""),
        )
        with self.state.lock:
            self.state.audio_requests.append(record)
        self._send(200, response)

    def _callback(self, query: str) -> None:
        body = self._body()
        try:
            timestamp = int(self.headers.get("X-Auris-Timestamp", ""))
        except ValueError as exc:
            raise GateSupportError("callback timestamp is invalid") from exc
        request = CallbackSignatureRequest(
            method="POST",
            path="/callbacks/platform",
            query=query,
            tenant_id=self.headers.get("X-Auris-Tenant-Id", ""),
            project_id=self.headers.get("X-Auris-Project-Id", ""),
            idempotency_key=self.headers.get("X-Auris-Idempotency-Key", ""),
            timestamp=timestamp,
            nonce=self.headers.get("X-Auris-Nonce", ""),
            key_id=self.headers.get("X-Auris-Key-Id", ""),
            body=body,
            version=self.headers.get("X-Auris-Signature-Version", ""),
        )
        signature = self.headers.get("X-Auris-Signature", "")
        if self.headers.get("X-Auris-Signature-Mode") != "hmac-sha256-v2":
            raise GateSupportError("callback signature mode is invalid")
        result = verify_callback_signature(
            request,
            signature,
            self.state.callback_keyring,
            now=int(time.time()),
            tolerance_seconds=CALLBACK_TOLERANCE_SECONDS,
            nonce_store=self.state.nonce_store,
        )
        candidate = CallbackIdempotencyBinding.from_body(
            idempotency_key=request.idempotency_key,
            body=body,
        )
        receipt_id = (
            "callback_receipt_"
            + hashlib.sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:16]
        )
        host = self.headers.get("Host", "callback.production-gate.invalid:8443")
        receipt_scheme = (
            "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        )
        receipt = {
            "callback_receipt_id": receipt_id,
            "receipt_url": f"{receipt_scheme}://{host}/receipts/{receipt_id}",
            "remote_trace_id": f"remote_{self.headers.get('X-Auris-Trace-Id', '')}",
            "tenant_id": request.tenant_id,
            "project_id": request.project_id,
            "trace_id": self.headers.get("X-Auris-Trace-Id", ""),
            "run_id": self.headers.get("X-Auris-Run-Id", ""),
            "target": json.loads(body.decode("utf-8")).get("target"),
            "idempotency_key": request.idempotency_key,
            "request_sha256": result.body_sha256,
            "signature_verified": result.verified,
            "signature_mode": "hmac-sha256-v2",
        }
        with self.state.lock:
            existing_binding = self.state.idempotency.get(request.idempotency_key)
            decision = decide_callback_idempotency(
                existing=existing_binding,
                candidate=candidate,
            )
            if decision.value == "conflict":
                self._send(409, {"status": "conflict"})
                return
            existing_receipt = self.state.receipts.get(receipt_id)
            if existing_receipt is None:
                self.state.idempotency[request.idempotency_key] = candidate
                self.state.receipts[receipt_id] = receipt
            else:
                receipt = existing_receipt
            self.state.last_callback = (request, signature)
            should_timeout = self.state.timeout_next
            self.state.timeout_next = False
        if should_timeout:
            # Persist the receipt first, then lose only the transport response so
            # the product must reconcile instead of repeating the remote write.
            time.sleep(6.0)
        self.send_response(200)
        response = _canonical_bytes({"status": "ok", "data": receipt})
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("X-Auris-Callback-Receipt-Id", receipt_id)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def _replay_last(self) -> None:
        with self.state.lock:
            last = self.state.last_callback
        if last is None:
            self._send(409, {"status": "no_callback"})
            return
        request, signature = last
        try:
            verify_callback_signature(
                request,
                signature,
                self.state.callback_keyring,
                now=int(time.time()),
                tolerance_seconds=CALLBACK_TOLERANCE_SECONDS,
                nonce_store=self.state.nonce_store,
            )
        except CallbackSignatureError as exc:
            rejected = exc.code == "CALLBACK_SIGNATURE_REPLAYED"
            with self.state.lock:
                self.state.replay_rejected = rejected
                self.state.replay_error_code = exc.code
            self._send(
                409 if rejected else 400,
                {"status": "replay_rejected" if rejected else "rejected"},
            )
            return
        self._send(500, {"status": "replay_accepted"})

    def _proofs(self) -> dict[str, Any]:
        with self.state.lock:
            if self.state.mode == "embedding":
                requests = list(self.state.embedding_requests)
                audio_requests = list(self.state.audio_requests)
                return {
                    "transport": "https",
                    "provider": "reference-semantic-protocol",
                    "reference_protocol_only": True,
                    "model_quality_certified": False,
                    "request_count": len(requests),
                    "request_hashes": [item["request_sha256"] for item in requests],
                    "purposes": sorted({str(item["purpose"]) for item in requests}),
                    "dimension": self.state.embedding_dimension,
                    "audio_inference": {
                        "transport": "https",
                        "provider": self.state.audio_provider,
                        "model": self.state.audio_model,
                        "reference_protocol_only": True,
                        "model_quality_certified": False,
                        "request_count": len(audio_requests),
                        "request_hashes": [
                            item["request_sha256"] for item in audio_requests
                        ],
                        "result_hashes": [
                            item["result_sha256"] for item in audio_requests
                        ],
                    },
                }
            receipts = list(self.state.receipts.values())
            return {
                "transport": "https",
                "signature_mode": "hmac-sha256-v2",
                "verified_receipt_count": len(receipts),
                "receipt_ids": sorted(
                    str(item["callback_receipt_id"]) for item in receipts
                ),
                "request_hashes": sorted(
                    str(item["request_sha256"]) for item in receipts
                ),
                "replay_rejected": self.state.replay_rejected,
                "replay_error_code": self.state.replay_error_code,
            }


class GateSupportServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], state: SupportState) -> None:
        super().__init__(address, GateSupportHandler)
        self.state = state


def _support_state(mode: str) -> SupportState:
    control_secret = _read_secret(
        os.environ["AURIS_GATE_CONTROL_SECRET_FILE"],
        label="gate control secret",
    )
    if mode == "embedding":
        api_key = _read_secret(
            os.environ["EMBEDDING_API_KEY_FILE"],
            label="embedding API key",
        )
        dimension = int(os.environ.get("EMBEDDING_DIMENSION", "8"))
        model = os.environ.get("EMBEDDING_MODEL", "").strip()
        audio_api_token = _read_secret(
            os.environ["AUDIO_INFERENCE_API_TOKEN_FILE"],
            label="audio inference API token",
        )
        audio_provider = os.environ.get("AUDIO_INFERENCE_PROVIDER", "").strip()
        audio_model = os.environ.get("AUDIO_INFERENCE_MODEL", "").strip()
        if not model or not audio_provider or not audio_model:
            raise GateSupportError("reference protocol model configuration is missing")
        return SupportState(
            mode=mode,
            control_secret=control_secret,
            embedding_api_key=api_key,
            embedding_model=model,
            embedding_dimension=dimension,
            audio_api_token=audio_api_token,
            audio_provider=audio_provider,
            audio_model=audio_model,
        )
    key_bindings = _read_secret(
        os.environ["EXTERNAL_CALLBACK_KEY_BINDINGS_FILE"],
        label="callback key bindings",
    )
    active_key_id = os.environ.get("EXTERNAL_CALLBACK_ACTIVE_KEY_ID", "").strip()
    return SupportState(
        mode=mode,
        control_secret=control_secret,
        callback_keyring=parse_callback_keyring(
            key_bindings,
            active_key_id=active_key_id,
        ),
    )


def serve(mode: str, *, listen: str, port: int) -> None:
    if mode not in {"embedding", "callback"} or not 1 <= port <= 65535:
        raise GateSupportError("gate support server arguments are invalid")
    cert_file = os.environ["AURIS_GATE_CERT_FILE"]
    key_file = os.environ["AURIS_GATE_KEY_FILE"]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    server = GateSupportServer((listen, port), _support_state(mode))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever(poll_interval=0.2)


def healthcheck(*, url: str, ca_file: str, server_name: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.port:
        raise GateSupportError("healthcheck URL is invalid")
    context = ssl.create_default_context(cafile=ca_file)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=2) as raw:
        with context.wrap_socket(raw, server_hostname=server_name) as tls:
            request = (
                f"GET {parsed.path or '/'} HTTP/1.1\r\n"
                f"Host: {server_name}\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            tls.sendall(request)
            response = tls.recv(4096)
    if not response.startswith(b"HTTP/1.1 200") or b'"status":"ok"' not in response:
        raise GateSupportError("gate support healthcheck failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for mode in ("embedding", "callback"):
        server = subparsers.add_parser(mode)
        server.add_argument("--listen", required=True)
        server.add_argument("--port", type=int, required=True)
    check = subparsers.add_parser("healthcheck")
    check.add_argument("--url", required=True)
    check.add_argument("--ca-file", required=True)
    check.add_argument("--server-name", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "healthcheck":
        healthcheck(url=args.url, ca_file=args.ca_file, server_name=args.server_name)
        return 0
    serve(args.command, listen=args.listen, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
