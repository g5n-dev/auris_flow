from __future__ import annotations

import hashlib
import hmac
import os
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from auris_flow_dagster.contracts import AudioExecutionEnvelope
from auris_flow_dagster.http_transport import RejectRedirectHandler as _RejectRedirectHandler

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_MAX_SECRET_BYTES = 65_536
_CHUNK_BYTES = 1024 * 1024
AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION = "auris-flow-audio-input-integrity-v1"


class AudioInputIntegrityError(RuntimeError):
    """Secret-free exact-version input verification failure."""


def _read_secret(path: str | Path, *, name: str) -> str:
    secret_path = Path(path)
    try:
        metadata = secret_path.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_SECRET_BYTES
        ):
            raise AudioInputIntegrityError(f"{name} secret file is invalid")
        raw = secret_path.read_bytes()
        if b"\x00" in raw:
            raise AudioInputIntegrityError(f"{name} secret file is invalid")
        value = raw.decode("utf-8").rstrip("\r\n")
    except AudioInputIntegrityError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise AudioInputIntegrityError(f"{name} secret file is unavailable") from exc
    if not value or "\n" in value or "\r" in value:
        raise AudioInputIntegrityError(f"{name} secret file is invalid")
    return value


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode(),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("ascii"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


class S3VersionedAudioVerifier:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        region: str | None = None,
        allowed_buckets: str | None = None,
        access_key_file: str | Path | None = None,
        secret_key_file: str | Path | None = None,
        timeout_seconds: float = 5.0,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        configured_endpoint = (
            str(endpoint)
            if endpoint is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT") or ""
        )
        self.endpoint = configured_endpoint.strip().rstrip("/")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise AudioInputIntegrityError("audio object storage endpoint is invalid")
        environment = os.environ.get("APP_ENV", "prod").strip().lower()
        if (
            environment in {"prod", "production", "release"}
            and parsed.scheme == "http"
            and parsed.hostname not in {"minio", "127.0.0.1", "localhost"}
        ):
            raise AudioInputIntegrityError(
                "plaintext audio object storage is limited to the internal MinIO service"
            )
        self._parsed_endpoint = parsed
        configured_region = (
            region
            if region is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_REGION") or ""
        )
        self.region = configured_region.strip()
        if not self.region:
            raise AudioInputIntegrityError("audio object storage region is required")
        configured_buckets = allowed_buckets
        if configured_buckets is None:
            configured_buckets = os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS", "")
        self.allowed_buckets = frozenset(
            item.strip() for item in configured_buckets.split(",") if item.strip()
        )
        if not self.allowed_buckets or "*" in self.allowed_buckets:
            raise AudioInputIntegrityError("audio object storage bucket allowlist is invalid")
        configured_access_key_file: str | Path = (
            access_key_file
            if access_key_file is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE") or ""
        )
        configured_secret_key_file: str | Path = (
            secret_key_file
            if secret_key_file is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE") or ""
        )
        self.access_key_file = Path(configured_access_key_file)
        self.secret_key_file = Path(configured_secret_key_file)
        if not 0 < timeout_seconds <= 30:
            raise AudioInputIntegrityError("audio object storage timeout is invalid")
        self.timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(_RejectRedirectHandler()).open
        self._clock = clock or (lambda: datetime.now(UTC))

    def verify(self, envelope: AudioExecutionEnvelope) -> dict[str, Any]:
        source = envelope.input_object
        if source.storage_provider not in {"minio", "s3"}:
            raise AudioInputIntegrityError("audio storage provider is not supported by this job")
        if source.bucket not in self.allowed_buckets:
            raise AudioInputIntegrityError("audio input bucket is not allowed")
        now = self._clock().astimezone(UTC)
        if now >= envelope.deadline_at:
            raise AudioInputIntegrityError("audio execution deadline is expired")

        request = self._request(envelope, now=now)
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                if int(response.status) != 200:
                    raise AudioInputIntegrityError("audio object storage returned invalid status")
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(str(raw_length))
                except (TypeError, ValueError) as exc:
                    raise AudioInputIntegrityError(
                        "audio object response content length is invalid"
                    ) from exc
                if content_length != source.content_length:
                    raise AudioInputIntegrityError("audio object content length mismatch")
                response_version = str(response.headers.get("x-amz-version-id") or "").strip()
                if response_version != source.version_id:
                    raise AudioInputIntegrityError("audio object version mismatch")
                content_type = str(response.headers.get("Content-Type") or "").partition(";")[0]
                if content_type.strip().lower() != source.content_type.lower():
                    raise AudioInputIntegrityError("audio object content type mismatch")

                observed_length = 0
                digest = hashlib.sha256()
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise AudioInputIntegrityError("audio object response body is invalid")
                    observed_length += len(chunk)
                    if observed_length > source.content_length:
                        raise AudioInputIntegrityError("audio object body exceeds declared length")
                    digest.update(chunk)
        except AudioInputIntegrityError:
            raise
        except HTTPError as exc:
            raise AudioInputIntegrityError(
                f"audio object storage rejected exact-version read (HTTP {exc.code})"
            ) from exc
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            raise AudioInputIntegrityError("audio object exact-version read failed") from exc

        if observed_length != source.content_length:
            raise AudioInputIntegrityError("audio object body length mismatch")
        observed_sha256 = digest.hexdigest()
        if not hmac.compare_digest(observed_sha256, source.content_sha256):
            raise AudioInputIntegrityError("audio object content hash mismatch")
        if self._clock().astimezone(UTC) >= envelope.deadline_at:
            raise AudioInputIntegrityError("audio execution deadline expired during input read")

        return {
            "manifest_version": AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION,
            "status": "verified",
            "execution_envelope_sha256": envelope.sha256,
            "storage_object_id_sha256": hashlib.sha256(
                source.storage_object_id.encode("utf-8")
            ).hexdigest(),
            "object_version_id_sha256": hashlib.sha256(
                source.version_id.encode("utf-8")
            ).hexdigest(),
            "expected_content_sha256": source.content_sha256,
            "observed_content_sha256": observed_sha256,
            "content_length": observed_length,
        }

    def _request(self, envelope: AudioExecutionEnvelope, *, now: datetime) -> Request:
        source = envelope.input_object
        access_key = _read_secret(self.access_key_file, name="access key")
        secret_key = _read_secret(self.secret_key_file, name="secret key")
        encoded_bucket = quote(source.bucket, safe="-_.~")
        encoded_key = "/".join(quote(part, safe="-_.~") for part in source.object_key.split("/"))
        canonical_uri = f"/{encoded_bucket}/{encoded_key}"
        encoded_version = quote(source.version_id, safe="-_.~")
        canonical_query = f"versionId={encoded_version}"
        host = str(self._parsed_endpoint.netloc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = timestamp[:8]
        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{_EMPTY_SHA256}\nx-amz-date:{timestamp}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                "GET",
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                _EMPTY_SHA256,
            ]
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(secret_key, date_stamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{scope},"
            f"SignedHeaders={signed_headers},"
            f"Signature={signature}"
        )
        url = f"{self.endpoint}{canonical_uri}?{canonical_query}"
        return Request(  # noqa: S310 - endpoint and bucket are strictly allowlisted above
            url,
            method="GET",
            headers={
                "Host": host,
                "x-amz-content-sha256": _EMPTY_SHA256,
                "x-amz-date": timestamp,
                "Authorization": authorization,
            },
        )


def safe_integrity_failure_manifest(
    envelope: AudioExecutionEnvelope,
    *,
    status: str,
) -> Mapping[str, Any]:
    return {
        "manifest_version": AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION,
        "status": status,
        "execution_envelope_sha256": envelope.sha256,
        "content_length": envelope.input_object.content_length,
    }
