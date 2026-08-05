#!/usr/bin/env python3
"""Deterministic HTTPS platform fixture for the real audio-import stack gate.

This is a network fixture, not an in-process adapter fake: both the BFF probe
and the Dagster import job must resolve the fixture hostname, validate TLS,
authenticate, paginate, and download the returned WAV objects over HTTPS.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import ssl
import struct
import sys
import wave
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

PLATFORM_HOSTNAME = "recordings.audio-import-gate.test"
INFERENCE_HOSTNAME = "audio-inference.audio-import-gate.test"
PLATFORM_PORT = 8443
PLATFORM_ORIGIN = f"https://{PLATFORM_HOSTNAME}:{PLATFORM_PORT}"
PLATFORM_BEARER_TOKEN = "audio-import-gate-fixture-auth"
PLATFORM_AUDIO_TOKEN = "audio-import-gate-fixture-download"
PLATFORM_RECORD_COUNT = 3


def _fixture_wav_bytes(index: int, *, browser_dataset: bool) -> bytes:
    """Return a small, valid, deterministic mono PCM WAV for one source record."""

    if not 1 <= index <= PLATFORM_RECORD_COUNT:
        raise ValueError("fixture WAV index is outside the gate dataset")
    frame_rate = 8_000
    frame_count = (1_200 if browser_dataset else 800) + index * 80
    frequency = (700 if browser_dataset else 300) + index * 70
    frames = bytearray()
    for frame in range(frame_count):
        sample = int(10_000 * math.sin(2 * math.pi * frequency * frame / frame_rate))
        frames.extend(struct.pack("<h", sample))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(frame_rate)
        wav.writeframes(bytes(frames))
    return output.getvalue()


def fixture_wav_bytes(index: int) -> bytes:
    return _fixture_wav_bytes(index, browser_dataset=False)


def browser_fixture_wav_bytes(index: int) -> bytes:
    return _fixture_wav_bytes(index, browser_dataset=True)


def _fixture_records(
    *,
    origin: str,
    identity_prefix: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index in range(1, PLATFORM_RECORD_COUNT + 1):
        records.append(
            {
                "recording_id": f"{identity_prefix}-{index:03d}",
                "download_url": (
                    f"{origin}/audio/{identity_prefix}-{index:03d}.wav"
                    f"?token={PLATFORM_AUDIO_TOKEN}"
                ),
                "started_at": f"2026-07-27T00:00:{index:02d}+00:00",
                "updated_at": f"2026-07-27T00:00:{index:02d}+00:00",
                "duration_ms": 100 + index * 10,
                "store_id": "BJ-AURORA-001",
                "employee": {"badge": f"GATE-{index:03d}"},
                "device_id": f"gate-device-{index:03d}",
            }
        )
    return records


def fixture_records(origin: str = PLATFORM_ORIGIN) -> list[dict[str, Any]]:
    return _fixture_records(origin=origin, identity_prefix="audio-import-gate")


def browser_fixture_records(
    origin: str = PLATFORM_ORIGIN,
) -> list[dict[str, Any]]:
    return _fixture_records(origin=origin, identity_prefix="audio-import-browser")


def _write_private(path: Path, body: bytes, *, mode: int) -> None:
    path.write_bytes(body)
    path.chmod(mode)


def initialize_pki(ca_dir: Path, tls_dir: Path) -> None:
    """Generate an ephemeral CA and a hostname-bound leaf certificate."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Auris audio import gate CA")]
    )
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, PLATFORM_HOSTNAME)]
    )
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(PLATFORM_HOSTNAME),
                    x509.DNSName(INFERENCE_HOSTNAME),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_private(
        ca_dir / "ca.pem",
        ca_certificate.public_bytes(serialization.Encoding.PEM),
        mode=0o444,
    )
    _write_private(
        tls_dir / "server.pem",
        server_certificate.public_bytes(serialization.Encoding.PEM),
        mode=0o444,
    )
    # This key exists only in an isolated, ephemeral test volume mounted by the
    # fixture server. World-readability lets the non-root server read a
    # root-created named-volume file without granting it write access.
    _write_private(
        tls_dir / "server-key.pem",
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        mode=0o444,
    )


class AudioImportPlatformHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AurisAudioImportGatePlatform/1.0"

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        *,
        content_type: str,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, body, content_type="application/json")

    def _listing(
        self,
        query: dict[str, list[str]],
        *,
        records: list[dict[str, Any]],
    ) -> None:
        if self.headers.get("Authorization") != f"Bearer {PLATFORM_BEARER_TOKEN}":
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            limit = int((query.get("limit") or ["0"])[0])
        except ValueError:
            limit = 0
        if not 1 <= limit <= PLATFORM_RECORD_COUNT:
            self._send_json(400, {"error": "invalid_limit"})
            return
        cursor = (query.get("cursor") or [""])[0]
        try:
            parsed_cursor = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError:
            self._send_json(400, {"error": "invalid_cursor"})
            return
        if parsed_cursor.tzinfo is None:
            self._send_json(400, {"error": "invalid_cursor"})
            return
        normalized_cursor = parsed_cursor.astimezone(UTC)
        remaining = [
            record
            for record in records
            if datetime.fromisoformat(str(record["updated_at"])).astimezone(UTC)
            > normalized_cursor
        ]
        selected = remaining[:limit]
        next_cursor = (
            selected[-1]["updated_at"]
            if selected and len(remaining) > len(selected)
            else None
        )
        self._send_json(
            200,
            {
                "records": selected,
                "next_cursor": next_cursor,
            },
        )

    def _audio(self, path: str, query: dict[str, list[str]]) -> None:
        if (query.get("token") or [""])[0] != PLATFORM_AUDIO_TOKEN:
            self._send_json(403, {"error": "invalid_download_token"})
            return
        gate_prefix = "/audio/audio-import-gate-"
        browser_prefix = "/audio/audio-import-browser-"
        suffix = ".wav"
        if path.startswith(gate_prefix):
            prefix = gate_prefix
            fixture = fixture_wav_bytes
        elif path.startswith(browser_prefix):
            prefix = browser_prefix
            fixture = browser_fixture_wav_bytes
        else:
            self._send_json(404, {"error": "not_found"})
            return
        if not path.endswith(suffix):
            self._send_json(404, {"error": "not_found"})
            return
        raw_index = path[len(prefix) : -len(suffix)]
        try:
            index = int(raw_index)
            body = fixture(index)
        except ValueError:
            self._send_json(404, {"error": "not_found"})
            return
        self._send_bytes(200, body, content_type="audio/wav")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/healthz":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "audio-import-gate-platform",
                    "transport": "https",
                },
            )
            return
        if parsed.path == "/v1/recordings":
            self._listing(query, records=fixture_records())
            return
        if parsed.path == "/v1/browser-recordings":
            self._listing(query, records=browser_fixture_records())
            return
        if parsed.path.startswith("/audio/"):
            self._audio(parsed.path, query)
            return
        self._send_json(404, {"error": "not_found"})

    def log_message(self, format_string: str, *args: object) -> None:
        del format_string, args
        parsed = urlsplit(self.path)
        sys.stderr.write(
            f'audio-import-platform method={self.command} path="{parsed.path}"\n'
        )


def serve(tls_dir: Path) -> None:
    certificate = tls_dir / "server.pem"
    private_key = tls_dir / "server-key.pem"
    if not certificate.is_file() or not private_key.is_file():
        raise SystemExit("audio import gate TLS material is unavailable")
    server = ThreadingHTTPServer(("0.0.0.0", PLATFORM_PORT), AudioImportPlatformHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    pki = subparsers.add_parser("pki-init")
    pki.add_argument("--ca-dir", type=Path, required=True)
    pki.add_argument("--tls-dir", type=Path, required=True)
    server = subparsers.add_parser("serve")
    server.add_argument("--tls-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "pki-init":
        initialize_pki(args.ca_dir, args.tls_dir)
        return 0
    serve(args.tls_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
