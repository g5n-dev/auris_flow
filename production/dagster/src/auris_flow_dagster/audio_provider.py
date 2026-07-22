from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import ssl
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener

from auris_flow_dagster.contracts import AudioExecutionEnvelope
from auris_flow_dagster.http_transport import RejectRedirectHandler as _RejectRedirectHandler

AUDIO_PROVIDER_REQUEST_SCHEMA = "auris-flow-audio-provider-request-v1"
AUDIO_PROVIDER_RESPONSE_SCHEMA = "auris-flow-audio-provider-response-v1"
AUDIO_RESULT_MANIFEST_SCHEMA = "auris-flow-audio-result-manifest-v1"
AUDIO_RESULT_RECEIPT_SCHEMA = "auris-flow-audio-result-receipt-v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_MAX_SECRET_BYTES = 65_536
_MAX_TRANSCRIPT_CHARACTERS = 500_000
_MAX_SEGMENTS = 10_000
_MAX_SEGMENT_MILLISECONDS = 604_800_000
_MAX_ANALYSES = 5
_MAX_LABELS = 100
_READ_CHUNK_BYTES = 65_536
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_REQUEST_FIELDS = frozenset(
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
_RESPONSE_FIELDS = _REQUEST_FIELDS | frozenset({"request_sha256", "result", "result_sha256"})
_RESULT_FIELDS = frozenset({"transcript", "analyses"})
_TRANSCRIPT_FIELDS = frozenset({"language", "text", "segments"})
_SEGMENT_FIELDS = frozenset({"start_ms", "end_ms", "speaker", "text", "confidence"})
_ANALYSIS_FIELDS = frozenset({"capability", "summary", "score", "labels"})
_LABEL_FIELDS = frozenset({"label", "score"})


def _https_opener_without_redirects(context: ssl.SSLContext) -> Callable[..., Any]:
    opener = build_opener(
        HTTPSHandler(context=context),
        _RejectRedirectHandler(),
    )

    def open_request(
        request: Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> Any:
        del context
        return opener.open(request, timeout=timeout)

    return open_request


def _opener_without_redirects() -> Callable[..., Any]:
    return build_opener(_RejectRedirectHandler()).open


class AudioProviderFailure(RuntimeError):
    """Sanitized failure carrying only a stable completion code and retry policy."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AudioProviderContractError(AudioProviderFailure):
    """A secret-free, non-retryable provider response contract failure."""

    def __init__(self, message: str = "audio inference provider response is invalid") -> None:
        super().__init__(
            message,
            code="AUDIO_PROVIDER_RESPONSE_INVALID",
            retryable=False,
        )


class AudioResultPersistenceError(AudioProviderFailure):
    """A secret-free failure to durably persist the validated provider result."""

    def __init__(self, message: str = "audio result manifest persistence failed") -> None:
        super().__init__(
            message,
            code="AUDIO_RESULT_PERSISTENCE_FAILED",
            retryable=True,
        )


@dataclass(frozen=True)
class AudioProviderResult:
    request_sha256: str
    response_sha256: str
    result_sha256: str
    provider_result: Mapping[str, Any]


@dataclass(frozen=True)
class AudioResultManifestReceipt:
    manifest_schema: str
    manifest_sha256: str
    object_key_sha256: str
    object_version_id_sha256: str
    provider_request_sha256: str
    provider_response_sha256: str
    provider_result_sha256: str
    storage_object_id: str
    storage_provider: str = field(repr=False)
    bucket: str = field(repr=False)
    object_key: str = field(repr=False)
    object_version_id: str = field(repr=False)
    content_length: int

    def public_mapping(self) -> dict[str, str]:
        return {
            "manifest_version": AUDIO_RESULT_RECEIPT_SCHEMA,
            "status": "materialized",
            "result_manifest_schema": self.manifest_schema,
            "result_manifest_sha256": self.manifest_sha256,
            "result_manifest_storage_object_id": self.storage_object_id,
            "result_manifest_object_key_sha256": self.object_key_sha256,
            "result_manifest_version_id_sha256": self.object_version_id_sha256,
            "provider_request_sha256": self.provider_request_sha256,
            "provider_response_sha256": self.provider_response_sha256,
            "provider_result_sha256": self.provider_result_sha256,
        }

    def internal_callback_mapping(self) -> dict[str, Any]:
        """Return the exact locator only inside the signed executor-to-BFF body."""

        return {
            **self.public_mapping(),
            "storage_objects": [
                {
                    "storage_object_id": self.storage_object_id,
                    "role": "manifest",
                    "provider": self.storage_provider,
                    "bucket": self.bucket,
                    "object_key": self.object_key,
                    "version_id": self.object_version_id,
                    "content_type": "application/json",
                    "size_bytes": self.content_length,
                    "content_sha256": self.manifest_sha256,
                }
            ],
        }


def _configuration_failure() -> AudioProviderFailure:
    return AudioProviderFailure(
        "audio inference provider configuration is invalid",
        code="AUDIO_PROVIDER_CONFIGURATION_INVALID",
        retryable=False,
    )


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AudioProviderContractError() from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _strict_mapping(
    raw: object,
    *,
    fields: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise AudioProviderContractError()
    if set(raw) != fields:
        raise AudioProviderContractError()
    return raw


def _text(
    raw: object,
    *,
    maximum: int,
    allow_empty: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(raw, str) or len(raw) > maximum or (not allow_empty and not raw):
        raise AudioProviderContractError()
    if "\x00" in raw or any(ord(char) < 0x20 and char not in "\t\n\r" for char in raw):
        raise AudioProviderContractError()
    if pattern is not None and pattern.fullmatch(raw) is None:
        raise AudioProviderContractError()
    return raw


def _score(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise AudioProviderContractError()
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise AudioProviderContractError()
    return value


def _millisecond(raw: object) -> int:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not 0 <= raw <= _MAX_SEGMENT_MILLISECONDS
    ):
        raise AudioProviderContractError()
    return raw


def build_audio_provider_request(envelope: AudioExecutionEnvelope) -> dict[str, Any]:
    mapping = envelope.as_mapping()
    request = {
        **mapping,
        "schema_version": AUDIO_PROVIDER_REQUEST_SCHEMA,
        "execution_envelope_sha256": envelope.sha256,
    }
    if set(request) != _REQUEST_FIELDS:
        raise AudioProviderContractError()
    return request


def _validated_result(
    raw: object,
    *,
    capabilities: tuple[str, ...],
) -> dict[str, Any]:
    result = _strict_mapping(raw, fields=_RESULT_FIELDS)
    raw_transcript = result.get("transcript")
    transcript: dict[str, Any] | None
    if raw_transcript is None:
        if "asr" in capabilities:
            raise AudioProviderContractError()
        transcript = None
    else:
        if "asr" not in capabilities:
            raise AudioProviderContractError()
        values = _strict_mapping(raw_transcript, fields=_TRANSCRIPT_FIELDS)
        raw_segments = values.get("segments")
        if not isinstance(raw_segments, list) or len(raw_segments) > _MAX_SEGMENTS:
            raise AudioProviderContractError()
        segments: list[dict[str, Any]] = []
        previous_start = -1
        for raw_segment in raw_segments:
            segment = _strict_mapping(raw_segment, fields=_SEGMENT_FIELDS)
            start_ms = _millisecond(segment.get("start_ms"))
            end_ms = _millisecond(segment.get("end_ms"))
            if end_ms <= start_ms or start_ms < previous_start:
                raise AudioProviderContractError()
            previous_start = start_ms
            raw_speaker = segment.get("speaker")
            speaker = (
                None
                if raw_speaker is None
                else _text(raw_speaker, maximum=128, pattern=_PROVIDER_NAME)
            )
            segments.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker": speaker,
                    "text": _text(
                        segment.get("text"),
                        maximum=8_192,
                        allow_empty=True,
                    ),
                    "confidence": _score(segment.get("confidence")),
                }
            )
        transcript = {
            "language": _text(values.get("language"), maximum=35, pattern=_LANGUAGE),
            "text": _text(
                values.get("text"),
                maximum=_MAX_TRANSCRIPT_CHARACTERS,
                allow_empty=True,
            ),
            "segments": segments,
        }

    raw_analyses = result.get("analyses")
    if not isinstance(raw_analyses, list) or len(raw_analyses) > _MAX_ANALYSES:
        raise AudioProviderContractError()
    expected_analyses = set(capabilities) - {"asr"}
    analyses: list[dict[str, Any]] = []
    observed_capabilities: set[str] = set()
    for raw_analysis in raw_analyses:
        analysis = _strict_mapping(raw_analysis, fields=_ANALYSIS_FIELDS)
        capability = _text(
            analysis.get("capability"),
            maximum=32,
            pattern=_PROVIDER_NAME,
        )
        if capability not in expected_analyses or capability in observed_capabilities:
            raise AudioProviderContractError()
        observed_capabilities.add(capability)
        raw_labels = analysis.get("labels")
        if not isinstance(raw_labels, list) or len(raw_labels) > _MAX_LABELS:
            raise AudioProviderContractError()
        labels: list[dict[str, Any]] = []
        observed_labels: set[str] = set()
        for raw_label in raw_labels:
            label = _strict_mapping(raw_label, fields=_LABEL_FIELDS)
            name = _text(label.get("label"), maximum=128)
            if name in observed_labels:
                raise AudioProviderContractError()
            observed_labels.add(name)
            labels.append({"label": name, "score": _score(label.get("score"))})
        analyses.append(
            {
                "capability": capability,
                "summary": _text(analysis.get("summary"), maximum=4_096),
                "score": _score(analysis.get("score")),
                "labels": labels,
            }
        )
    if observed_capabilities != expected_analyses:
        raise AudioProviderContractError()
    return {"transcript": transcript, "analyses": analyses}


def validate_audio_provider_response(
    raw: object,
    *,
    envelope: AudioExecutionEnvelope,
    request_sha256: str,
    response_sha256: str | None = None,
) -> AudioProviderResult:
    response = _strict_mapping(raw, fields=_RESPONSE_FIELDS)
    request = build_audio_provider_request(envelope)
    expected_request_sha256 = canonical_sha256(request)
    if (
        not _SHA256.fullmatch(request_sha256)
        or not hmac.compare_digest(request_sha256, expected_request_sha256)
        or response.get("schema_version") != AUDIO_PROVIDER_RESPONSE_SCHEMA
        or response.get("request_sha256") != request_sha256
    ):
        raise AudioProviderContractError()
    for field_name, expected in request.items():
        if field_name == "schema_version":
            continue
        if response.get(field_name) != expected:
            raise AudioProviderContractError()

    result = _validated_result(response.get("result"), capabilities=envelope.capabilities)
    result_sha256 = canonical_sha256(result)
    raw_result_sha256 = response.get("result_sha256")
    if (
        not isinstance(raw_result_sha256, str)
        or not _SHA256.fullmatch(raw_result_sha256)
        or not hmac.compare_digest(raw_result_sha256, result_sha256)
    ):
        raise AudioProviderContractError()
    observed_response_sha256 = response_sha256 or canonical_sha256(response)
    if not _SHA256.fullmatch(observed_response_sha256):
        raise AudioProviderContractError()
    return AudioProviderResult(
        request_sha256=request_sha256,
        response_sha256=observed_response_sha256,
        result_sha256=result_sha256,
        provider_result=result,
    )


def _read_secret(path: str | Path, *, minimum: int) -> str:
    secret_path = Path(path)
    try:
        metadata = secret_path.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_SECRET_BYTES:
            raise _configuration_failure()
        raw = secret_path.read_bytes()
        if b"\x00" in raw:
            raise _configuration_failure()
        value = raw.decode("utf-8").rstrip("\r\n")
    except AudioProviderFailure:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise _configuration_failure() from exc
    folded = value.casefold()
    if (
        not minimum <= len(value) <= 16_384
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in value)
        or any(marker in folded for marker in ("changeme", "replace-with", "example-token"))
    ):
        raise _configuration_failure()
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AudioProviderContractError()
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise AudioProviderContractError()


def _read_bounded_json_response(response: Any, *, maximum: int) -> tuple[object, str]:
    raw_content_type = str(response.headers.get("Content-Type") or "")
    if raw_content_type.partition(";")[0].strip().casefold() != "application/json":
        raise AudioProviderContractError()
    raw_length = response.headers.get("Content-Length")
    if raw_length not in {None, ""}:
        try:
            declared_length = int(str(raw_length))
        except ValueError as exc:
            raise AudioProviderContractError() from exc
        if declared_length < 0 or declared_length > maximum:
            raise AudioProviderContractError()
    body = bytearray()
    while True:
        chunk = response.read(min(_READ_CHUNK_BYTES, maximum + 1 - len(body)))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AudioProviderContractError()
        body.extend(chunk)
        if len(body) > maximum:
            raise AudioProviderContractError()
    if raw_length not in {None, ""} and len(body) != declared_length:
        raise AudioProviderContractError()
    try:
        payload = json.loads(
            bytes(body).decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_constant,
        )
    except AudioProviderContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AudioProviderContractError() from exc
    return payload, hashlib.sha256(body).hexdigest()


class HTTPSAudioInferenceProvider:
    def __init__(
        self,
        *,
        provider: str | None = None,
        allowed_models: str | None = None,
        endpoint: str | None = None,
        token_file: str | Path | None = None,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        environment: str | None = None,
    ) -> None:
        self.provider = str(
            provider
            if provider is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_PROVIDER") or ""
        ).strip()
        configured_models = (
            allowed_models
            if allowed_models is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_ALLOWED_MODELS", "")
        )
        model_items = [item.strip() for item in configured_models.split(",")]
        self.allowed_models = frozenset(item for item in model_items if item)
        configured_endpoint = str(
            endpoint
            if endpoint is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_ENDPOINT") or ""
        ).strip()
        parsed = urlsplit(configured_endpoint)
        if (
            not _PROVIDER_NAME.fullmatch(self.provider)
            or not self.allowed_models
            or "*" in self.allowed_models
            or len(self.allowed_models) > 32
            or any(_PROVIDER_NAME.fullmatch(model) is None for model in self.allowed_models)
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path in {"", "/"}
        ):
            raise _configuration_failure()
        configured_environment = (
            (environment if environment is not None else os.environ.get("APP_ENV", "prod"))
            .strip()
            .casefold()
        )
        hostname = parsed.hostname.casefold()
        if configured_environment in {"prod", "production", "release"} and (
            hostname in {"example.com", "localhost"}
            or hostname.endswith((".example.com", ".example.test", ".test"))
        ):
            raise _configuration_failure()
        self.endpoint = configured_endpoint
        configured_token_file = (
            token_file
            if token_file is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_API_TOKEN_FILE") or ""
        )
        self.token_file = Path(configured_token_file)
        _read_secret(self.token_file, minimum=32)
        raw_timeout: float | str = (
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_TIMEOUT_SECONDS", "30")
        )
        try:
            configured_timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise _configuration_failure() from exc
        if (
            isinstance(raw_timeout, bool)
            or not math.isfinite(configured_timeout)
            or not 0 < configured_timeout <= 120
        ):
            raise _configuration_failure()
        self.timeout_seconds = configured_timeout
        raw_maximum: int | str = (
            max_response_bytes
            if max_response_bytes is not None
            else os.environ.get("AURIS_AUDIO_INFERENCE_MAX_RESPONSE_BYTES", "1048576")
        )
        try:
            configured_maximum = int(raw_maximum)
        except (TypeError, ValueError) as exc:
            raise _configuration_failure() from exc
        if isinstance(raw_maximum, bool) or not 64 <= configured_maximum <= 4 * 1024 * 1024:
            raise _configuration_failure()
        self.max_response_bytes = configured_maximum
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ssl_context = ssl.create_default_context()
        self._opener = opener or _https_opener_without_redirects(self._ssl_context)

    def infer(self, envelope: AudioExecutionEnvelope) -> AudioProviderResult:
        if (
            envelope.inference.provider != self.provider
            or envelope.inference.model not in self.allowed_models
        ):
            raise _configuration_failure()
        if self._clock().astimezone(UTC) >= envelope.deadline_at:
            raise AudioProviderFailure(
                "audio inference deadline is expired",
                code="AUDIO_PROVIDER_DEADLINE_EXPIRED",
                retryable=False,
            )
        request_payload = build_audio_provider_request(envelope)
        request_body = canonical_json_bytes(request_payload)
        request_sha256 = hashlib.sha256(request_body).hexdigest()
        token = _read_secret(self.token_file, minimum=32)
        request = Request(  # noqa: S310 - constructor requires an exact HTTPS endpoint
            self.endpoint,
            data=request_body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Idempotency-Key": f"audio-inference:{envelope.dispatch_idempotency_key}",
                "X-Auris-Request-SHA256": request_sha256,
            },
        )
        try:
            with self._opener(
                request,
                timeout=self.timeout_seconds,
                context=self._ssl_context,
            ) as response:
                if int(response.status) != 200:
                    raise AudioProviderFailure(
                        "audio inference provider returned an invalid status",
                        code="AUDIO_PROVIDER_RESPONSE_INVALID",
                        retryable=False,
                    )
                payload, response_sha256 = _read_bounded_json_response(
                    response,
                    maximum=self.max_response_bytes,
                )
        except AudioProviderFailure:
            raise
        except HTTPError as exc:
            retryable = exc.code in {408, 425, 429} or 500 <= exc.code <= 599
            raise AudioProviderFailure(
                "audio inference provider request failed",
                code=(
                    "AUDIO_PROVIDER_UNAVAILABLE" if retryable else "AUDIO_PROVIDER_REQUEST_REJECTED"
                ),
                retryable=retryable,
            ) from None
        except (TimeoutError, URLError, OSError, ssl.SSLError):
            raise AudioProviderFailure(
                "audio inference provider is unavailable",
                code="AUDIO_PROVIDER_UNAVAILABLE",
                retryable=True,
            ) from None
        if self._clock().astimezone(UTC) >= envelope.deadline_at:
            raise AudioProviderFailure(
                "audio inference deadline expired during provider execution",
                code="AUDIO_PROVIDER_DEADLINE_EXPIRED",
                retryable=False,
            )
        return validate_audio_provider_response(
            payload,
            envelope=envelope,
            request_sha256=request_sha256,
            response_sha256=response_sha256,
        )


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode(),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("ascii"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


class S3AudioResultManifestStore:
    def __init__(
        self,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        region: str | None = None,
        bucket: str | None = None,
        allowed_buckets: str | None = None,
        access_key_file: str | Path | None = None,
        secret_key_file: str | Path | None = None,
        timeout_seconds: float = 5.0,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage_provider = (
            str(
                provider
                if provider is not None
                else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_PROVIDER") or ""
            )
            .strip()
            .casefold()
        )
        if self.storage_provider not in {"minio", "s3"}:
            raise AudioResultPersistenceError()
        self.endpoint = (
            str(
                endpoint
                if endpoint is not None
                else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT") or ""
            )
            .strip()
            .rstrip("/")
        )
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
            raise AudioResultPersistenceError()
        environment = os.environ.get("APP_ENV", "prod").strip().casefold()
        if (
            environment in {"prod", "production", "release"}
            and parsed.scheme == "http"
            and parsed.hostname not in {"minio", "127.0.0.1", "localhost"}
        ):
            raise AudioResultPersistenceError()
        self._parsed_endpoint = parsed
        self.region = str(
            region
            if region is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_REGION") or ""
        ).strip()
        self.bucket = str(
            bucket if bucket is not None else os.environ.get("AURIS_AUDIO_RESULT_BUCKET") or ""
        ).strip()
        configured_allowed = (
            allowed_buckets
            if allowed_buckets is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS", "")
        )
        bucket_allowlist = {item.strip() for item in configured_allowed.split(",") if item.strip()}
        if (
            not self.region
            or not self.bucket
            or self.bucket not in bucket_allowlist
            or "*" in bucket_allowlist
        ):
            raise AudioResultPersistenceError()
        self.access_key_file = Path(
            access_key_file
            if access_key_file is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE") or ""
        )
        self.secret_key_file = Path(
            secret_key_file
            if secret_key_file is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE") or ""
        )
        try:
            _read_secret(self.access_key_file, minimum=8)
            _read_secret(self.secret_key_file, minimum=16)
        except AudioProviderFailure as exc:
            raise AudioResultPersistenceError() from exc
        if not 0 < timeout_seconds <= 30:
            raise AudioResultPersistenceError()
        self.timeout_seconds = timeout_seconds
        self._opener = opener or _opener_without_redirects()
        self._clock = clock or (lambda: datetime.now(UTC))

    def persist(
        self,
        *,
        envelope: AudioExecutionEnvelope,
        integrity_manifest: Mapping[str, Any],
        result: AudioProviderResult,
    ) -> AudioResultManifestReceipt:
        if (
            integrity_manifest.get("status") != "verified"
            or integrity_manifest.get("execution_envelope_sha256") != envelope.sha256
        ):
            raise AudioResultPersistenceError()
        manifest = {
            "schema_version": AUDIO_RESULT_MANIFEST_SCHEMA,
            "execution_contract": envelope.execution_contract,
            "execution_envelope_sha256": envelope.sha256,
            "tenant_id": envelope.tenant_id,
            "project_id": envelope.project_id,
            "trace_id": envelope.trace_id,
            "run_id": envelope.run_id,
            "dispatch_idempotency_key": envelope.dispatch_idempotency_key,
            "outbox_fencing_token": envelope.outbox_fencing_token,
            "audio_session_id": envelope.audio_session_id,
            "recording_id": envelope.recording_id,
            "input_object": envelope.as_mapping()["input_object"],
            "inference": envelope.as_mapping()["inference"],
            "capabilities": list(envelope.capabilities),
            "input_integrity": dict(integrity_manifest),
            "provider_request_sha256": result.request_sha256,
            "provider_response_sha256": result.response_sha256,
            "provider_result_sha256": result.result_sha256,
            "provider_result": dict(result.provider_result),
        }
        body = canonical_json_bytes(manifest)
        if len(body) > 4 * 1024 * 1024:
            raise AudioResultPersistenceError()
        manifest_sha256 = hashlib.sha256(body).hexdigest()
        object_key = (
            f"tenants/{envelope.tenant_id}/projects/{envelope.project_id}/"
            f"runs/{envelope.run_id}/audio-intelligence/{result.request_sha256}.json"
        )
        now = self._clock().astimezone(UTC)
        if now >= envelope.deadline_at:
            raise AudioResultPersistenceError()
        request = self._request(
            object_key=object_key,
            body=body,
            content_sha256=manifest_sha256,
            now=now,
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                if int(response.status) not in {200, 201}:
                    raise AudioResultPersistenceError()
                version_id = str(response.headers.get("x-amz-version-id") or "").strip()
        except AudioProviderFailure:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise AudioResultPersistenceError() from None
        if (
            not version_id
            or version_id.casefold() == "null"
            or len(version_id) > 1_024
            or version_id != version_id.strip()
            or any(ord(character) < 0x20 for character in version_id)
        ):
            raise AudioResultPersistenceError()
        storage_object_id = f"sto_audio_manifest_{manifest_sha256[:32]}"
        return AudioResultManifestReceipt(
            manifest_schema=AUDIO_RESULT_MANIFEST_SCHEMA,
            manifest_sha256=manifest_sha256,
            object_key_sha256=hashlib.sha256(object_key.encode()).hexdigest(),
            object_version_id_sha256=hashlib.sha256(version_id.encode()).hexdigest(),
            provider_request_sha256=result.request_sha256,
            provider_response_sha256=result.response_sha256,
            provider_result_sha256=result.result_sha256,
            storage_object_id=storage_object_id,
            storage_provider=self.storage_provider,
            bucket=self.bucket,
            object_key=object_key,
            object_version_id=version_id,
            content_length=len(body),
        )

    def _request(
        self,
        *,
        object_key: str,
        body: bytes,
        content_sha256: str,
        now: datetime,
    ) -> Request:
        try:
            access_key = _read_secret(self.access_key_file, minimum=8)
            secret_key = _read_secret(self.secret_key_file, minimum=16)
        except AudioProviderFailure as exc:
            raise AudioResultPersistenceError() from exc
        encoded_bucket = quote(self.bucket, safe="-_.~")
        encoded_key = "/".join(quote(part, safe="-_.~") for part in object_key.split("/"))
        canonical_uri = f"/{encoded_bucket}/{encoded_key}"
        host = str(self._parsed_endpoint.netloc)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = timestamp[:8]
        canonical_headers = (
            "content-type:application/json\n"
            f"host:{host}\n"
            f"x-amz-content-sha256:{content_sha256}\n"
            f"x-amz-date:{timestamp}\n"
        )
        signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [
                "PUT",
                canonical_uri,
                "",
                canonical_headers,
                signed_headers,
                content_sha256,
            ]
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                timestamp,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(secret_key, date_stamp, self.region),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{scope},"
            f"SignedHeaders={signed_headers},"
            f"Signature={signature}"
        )
        return Request(  # noqa: S310 - endpoint and bucket are strictly configured above
            f"{self.endpoint}{canonical_uri}",
            data=body,
            method="PUT",
            headers={
                "Authorization": authorization,
                "Content-Type": "application/json",
                "Host": host,
                "x-amz-content-sha256": content_sha256,
                "x-amz-date": timestamp,
            },
        )


__all__ = [
    "AUDIO_PROVIDER_REQUEST_SCHEMA",
    "AUDIO_PROVIDER_RESPONSE_SCHEMA",
    "AUDIO_RESULT_MANIFEST_SCHEMA",
    "AUDIO_RESULT_RECEIPT_SCHEMA",
    "AudioProviderContractError",
    "AudioProviderFailure",
    "AudioProviderResult",
    "AudioResultManifestReceipt",
    "AudioResultPersistenceError",
    "HTTPSAudioInferenceProvider",
    "S3AudioResultManifestStore",
    "build_audio_provider_request",
    "canonical_json_bytes",
    "canonical_sha256",
    "validate_audio_provider_response",
]
