from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import stat
import tempfile
import wave
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener

from auris_flow_dagster.audio_provider import canonical_json_bytes
from auris_flow_dagster.callback import CompletionCallbackClient
from auris_flow_dagster.contracts import AudioImportEnvelope, AurisRunContext
from auris_flow_dagster.http_transport import RejectRedirectHandler
from auris_flow_dagster.runtime import AurisWorkflowError

AUDIO_IMPORT_MANIFEST_SCHEMA = "auris-flow-audio-import-manifest-v1"
AUDIO_IMPORT_RESULT_SCHEMA = "auris-flow-audio-import-result-v1"

_READ_CHUNK_BYTES = 64 * 1024
_MAX_SECRET_BYTES = 65_536
_DEFAULT_MAX_LISTING_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_AUDIO_BYTES = 512 * 1024 * 1024
_DEFAULT_MAX_PAGES = 10_000
_DEFAULT_MAX_RECORDS = 250
_DEFAULT_MAX_TOTAL_AUDIO_BYTES = 2 * 1024**3
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
_CREDENTIAL_REF = re.compile(r"^[A-Za-z][A-Za-z0-9._:/-]{2,511}$")
_VERSION_ID = re.compile(r"^[^\x00-\x20]{1,1024}$")
_STRONG_ETAG = re.compile(r"^[\x21\x23-\x7e]{1,255}$")
_AUDIO_TYPES = frozenset({"audio/wav", "audio/x-wav"})
_SAFE_AUTH_HEADERS = frozenset({"authorization", "x-api-key", "x-auth-token"})


class AudioImportFailure(RuntimeError):
    """A sanitized source, download, or persistence failure."""

    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ImportSourceRecord:
    external_record_id: str
    audio_url: str = field(repr=False)
    source_metadata: Mapping[str, Any]
    cursor_value: str


@dataclass(frozen=True)
class ImportSourcePage:
    records: tuple[ImportSourceRecord, ...]
    next_cursor: str | None


@dataclass
class DownloadedAudio:
    stream: IO[bytes] = field(repr=False)
    content_length: int
    content_type: str
    content_sha256: str

    def close(self) -> None:
        self.stream.close()


@dataclass(frozen=True)
class ImportObjectReceipt:
    storage_object_id: str
    role: str
    provider: str
    bucket: str = field(repr=False)
    object_key: str = field(repr=False)
    version_id: str = field(repr=False)
    etag: str = field(repr=False)
    content_type: str
    size_bytes: int
    content_sha256: str
    created: bool

    def as_mapping(self) -> dict[str, Any]:
        return {
            "storage_object_id": self.storage_object_id,
            "role": self.role,
            "provider": self.provider,
            "bucket": self.bucket,
            "object_key": self.object_key,
            "version_id": self.version_id,
            "etag": self.etag,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class _ComparableCursor:
    value: str
    kind: Literal["datetime", "integer", "text"]
    order: int | str


@dataclass(frozen=True)
class _ValidatedHttpsTarget:
    url: str
    hostname: str
    port: int
    authority: str
    addresses: tuple[str, ...]


class CredentialResolver(Protocol):
    def resolve(self, envelope: AudioImportEnvelope) -> Mapping[str, str]: ...


class AudioImportSource(Protocol):
    def fetch_page(
        self,
        envelope: AudioImportEnvelope,
        *,
        cursor: str | None,
    ) -> ImportSourcePage: ...

    def download_audio(
        self,
        envelope: AudioImportEnvelope,
        record: ImportSourceRecord,
    ) -> DownloadedAudio: ...


class AudioImportObjectStore(Protocol):
    def persist_audio(
        self,
        *,
        envelope: AudioImportEnvelope,
        record: ImportSourceRecord,
        audio: DownloadedAudio,
    ) -> ImportObjectReceipt: ...

    def persist_manifest(
        self,
        *,
        envelope: AudioImportEnvelope,
        body: bytes,
        content_sha256: str,
    ) -> ImportObjectReceipt: ...


class FileBearerCredentialResolver:
    """Resolve a credential only from an exact execution-scope and origin binding."""

    def __init__(
        self,
        *,
        bindings_file: str | Path | None = None,
    ) -> None:
        configured_bindings = (
            bindings_file
            if bindings_file is not None
            else os.environ.get("AURIS_PLATFORM_CREDENTIAL_BINDINGS_FILE")
        )
        self.bindings_file = Path(configured_bindings) if configured_bindings is not None else None

    def resolve(self, envelope: AudioImportEnvelope) -> Mapping[str, str]:
        credential_ref = envelope.connector.credential_ref
        if not _CREDENTIAL_REF.fullmatch(credential_ref):
            raise AudioImportFailure(
                "platform credential reference is invalid",
                code="AUDIO_IMPORT_CREDENTIAL_INVALID",
                retryable=False,
            )
        if self.bindings_file is None:
            raise AudioImportFailure(
                "platform credential binding is unavailable",
                code="AUDIO_IMPORT_CREDENTIAL_UNAVAILABLE",
                retryable=True,
            )
        return self._resolve_binding(envelope)

    def _resolve_binding(self, envelope: AudioImportEnvelope) -> Mapping[str, str]:
        path = self.bindings_file
        if path is None:
            raise AudioImportFailure(
                "platform credential binding is unavailable",
                code="AUDIO_IMPORT_CREDENTIAL_UNAVAILABLE",
                retryable=True,
            )
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_SECRET_BYTES:
                raise AudioImportFailure(
                    "platform credential binding is invalid",
                    code="AUDIO_IMPORT_CREDENTIAL_INVALID",
                    retryable=False,
                )
            raw = path.read_bytes()
            bindings = json.loads(raw.decode("utf-8"))
        except AudioImportFailure:
            raise
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise AudioImportFailure(
                "platform credential binding is unavailable",
                code="AUDIO_IMPORT_CREDENTIAL_UNAVAILABLE",
                retryable=True,
            ) from exc
        credential_ref = envelope.connector.credential_ref
        binding = bindings.get(credential_ref) if isinstance(bindings, Mapping) else None
        raw_headers = binding.get("headers") if isinstance(binding, Mapping) else None
        required_binding_fields = frozenset(
            {
                "tenant_id",
                "project_id",
                "platform_connection_id",
                "platform_tenant_ref",
                "base_url",
                "headers",
            }
        )
        if (
            not isinstance(binding, Mapping)
            or set(binding) != required_binding_fields
            or any(not isinstance(name, str) for name in binding)
            or any(
                not isinstance(binding.get(name), str)
                for name in required_binding_fields - {"headers"}
            )
            or not hmac.compare_digest(str(binding.get("tenant_id") or ""), envelope.tenant_id)
            or not hmac.compare_digest(str(binding.get("project_id") or ""), envelope.project_id)
            or not hmac.compare_digest(
                str(binding.get("platform_connection_id") or ""),
                envelope.connector.platform_connection_id,
            )
            or not hmac.compare_digest(
                str(binding.get("platform_tenant_ref") or ""),
                envelope.connector.platform_scope.tenant_ref,
            )
            or not hmac.compare_digest(
                str(binding.get("base_url") or ""),
                envelope.connector.base_url,
            )
            or not isinstance(raw_headers, Mapping)
            or not 1 <= len(raw_headers) <= 3
            or any(not isinstance(name, str) for name in raw_headers)
        ):
            raise AudioImportFailure(
                "platform credential binding is invalid",
                code="AUDIO_IMPORT_CREDENTIAL_INVALID",
                retryable=False,
            )
        headers: dict[str, str] = {}
        for name, value in raw_headers.items():
            if (
                name.casefold() not in _SAFE_AUTH_HEADERS
                or not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 16 * 1024
                or "\r" in value
                or "\n" in value
            ):
                raise AudioImportFailure(
                    "platform credential binding is invalid",
                    code="AUDIO_IMPORT_CREDENTIAL_INVALID",
                    retryable=False,
                )
            headers[name] = value
        return headers


def _default_host_resolver(hostname: str) -> set[str]:
    try:
        return {
            str(address[4][0])
            for address in socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise AudioImportFailure(
            "platform host resolution failed",
            code="AUDIO_IMPORT_HOST_UNAVAILABLE",
            retryable=True,
        ) from exc


def _validated_https_url(
    raw_url: str,
    *,
    host_resolver: Callable[[str], set[str]],
    allowed_hosts: frozenset[str] | None,
) -> _ValidatedHttpsTarget:
    if (
        not isinstance(raw_url, str)
        or not raw_url
        or len(raw_url) > 8_192
        or any(ord(character) < 0x21 for character in raw_url)
    ):
        raise AudioImportFailure(
            "platform URL is unsafe",
            code="AUDIO_IMPORT_URL_UNSAFE",
            retryable=False,
        )
    try:
        raw_url.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AudioImportFailure(
            "platform URL is unsafe",
            code="AUDIO_IMPORT_URL_UNSAFE",
            retryable=False,
        ) from exc
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise AudioImportFailure(
            "platform URL is unsafe",
            code="AUDIO_IMPORT_URL_UNSAFE",
            retryable=False,
        ) from exc
    if parsed.scheme != "https":
        raise AudioImportFailure(
            "platform audio URL must use HTTPS",
            code="AUDIO_IMPORT_URL_UNSAFE",
            retryable=False,
        )
    hostname = (parsed.hostname or "").casefold()
    if (
        not hostname
        or parsed.username
        or parsed.password
        or port is not None
        and not 1 <= port <= 65_535
        or parsed.fragment
    ):
        raise AudioImportFailure(
            "platform URL is unsafe",
            code="AUDIO_IMPORT_URL_UNSAFE",
            retryable=False,
        )
    if allowed_hosts is not None and hostname not in allowed_hosts:
        raise AudioImportFailure(
            "platform audio URL host is not allowed",
            code="AUDIO_IMPORT_URL_HOST_NOT_ALLOWED",
            retryable=False,
        )
    addresses = host_resolver(hostname)
    if not addresses:
        raise AudioImportFailure(
            "platform host resolution failed",
            code="AUDIO_IMPORT_HOST_UNAVAILABLE",
            retryable=True,
        )
    try:
        parsed_addresses = [ipaddress.ip_address(value) for value in addresses]
    except ValueError as exc:
        raise AudioImportFailure(
            "platform host resolution failed",
            code="AUDIO_IMPORT_HOST_UNAVAILABLE",
            retryable=True,
        ) from exc
    if any(not address.is_global for address in parsed_addresses):
        raise AudioImportFailure(
            "private platform address is forbidden",
            code="AUDIO_IMPORT_PRIVATE_ADDRESS_FORBIDDEN",
            retryable=False,
        )
    normalized_addresses = tuple(
        address.compressed
        for address in sorted(
            parsed_addresses,
            key=lambda value: (value.version, int(value)),
        )
    )
    effective_port = port or 443
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = authority_host if effective_port == 443 else f"{authority_host}:{effective_port}"
    return _ValidatedHttpsTarget(
        url=raw_url,
        hostname=hostname,
        port=effective_port,
        authority=authority,
        addresses=normalized_addresses,
    )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a verified IP while authenticating the frozen logical hostname."""

    def __init__(
        self,
        *,
        hostname: str,
        pinned_address: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(
            host=hostname,
            port=port,
            timeout=timeout,
            context=context,
        )
        self._pinned_address = ipaddress.ip_address(pinned_address)
        self._tls_context = context

    def connect(self) -> None:
        family = socket.AF_INET6 if self._pinned_address.version == 6 else socket.AF_INET
        raw_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw_socket.settimeout(self.timeout)
            if self._pinned_address.version == 6:
                destination: tuple[Any, ...] = (
                    self._pinned_address.compressed,
                    self.port,
                    0,
                    0,
                )
            else:
                destination = (self._pinned_address.compressed, self.port)
            raw_socket.connect(destination)
            self.sock = self._tls_context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


class _PinnedHTTPSResponse:
    def __init__(self, response: Any, connection: Any) -> None:
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = response.headers

    def __enter__(self) -> _PinnedHTTPSResponse:
        return self

    def __exit__(self, *_args: object) -> Literal[False]:
        try:
            close_response = getattr(self._response, "close", None)
            if callable(close_response):
                close_response()
        finally:
            self._connection.close()
        return False

    def read(self, size: int = -1) -> bytes:
        body = self._response.read(size)
        if not isinstance(body, bytes):
            raise OSError("pinned HTTPS response body is invalid")
        return body


def _mapping_value(raw: object, path: str) -> object:
    current = raw
    for component in path.split("."):
        if not isinstance(current, Mapping) or component not in current:
            raise AudioImportFailure(
                "platform response field mapping is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        current = current[component]
    return current


def _comparable_cursor(raw: object) -> _ComparableCursor:
    if isinstance(raw, bool):
        raise AudioImportFailure(
            "platform cursor value is invalid",
            code="AUDIO_IMPORT_CURSOR_INVALID",
            retryable=False,
        )
    if isinstance(raw, int):
        if not -(2**63) <= raw <= 2**63 - 1:
            raise AudioImportFailure(
                "platform cursor value is invalid",
                code="AUDIO_IMPORT_CURSOR_INVALID",
                retryable=False,
            )
        return _ComparableCursor(value=str(raw), kind="integer", order=raw)
    if not isinstance(raw, str):
        raise AudioImportFailure(
            "platform cursor value is invalid",
            code="AUDIO_IMPORT_CURSOR_INVALID",
            retryable=False,
        )
    value = raw.strip()
    if not value or len(value) > 1_024 or any(ord(character) < 0x20 for character in value):
        raise AudioImportFailure(
            "platform cursor value is invalid",
            code="AUDIO_IMPORT_CURSOR_INVALID",
            retryable=False,
        )
    if re.fullmatch(r"-?(?:0|[1-9][0-9]{0,18})", value):
        parsed_integer = int(value)
        if -(2**63) <= parsed_integer <= 2**63 - 1:
            return _ComparableCursor(
                value=str(parsed_integer),
                kind="integer",
                order=parsed_integer,
            )
    try:
        parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    if parsed_datetime is not None and parsed_datetime.tzinfo is not None:
        normalized_datetime = parsed_datetime.astimezone(UTC).isoformat()
        return _ComparableCursor(
            value=normalized_datetime,
            kind="datetime",
            order=normalized_datetime,
        )
    return _ComparableCursor(value=value, kind="text", order=value)


def _cursor_precedes(candidate: _ComparableCursor, current: _ComparableCursor) -> bool:
    if candidate.kind != current.kind:
        raise AudioImportFailure(
            "platform cursor types are inconsistent",
            code="AUDIO_IMPORT_CURSOR_INVALID",
            retryable=False,
        )
    if isinstance(candidate.order, int) and isinstance(current.order, int):
        return candidate.order < current.order
    if isinstance(candidate.order, str) and isinstance(current.order, str):
        return candidate.order < current.order
    raise AudioImportFailure(
        "platform cursor types are inconsistent",
        code="AUDIO_IMPORT_CURSOR_INVALID",
        retryable=False,
    )


def _ensure_deadline(
    deadline_at: datetime,
    *,
    clock: Callable[[], datetime],
    message: str,
) -> None:
    if clock().astimezone(UTC) >= deadline_at:
        raise AudioImportFailure(
            message,
            code="AUDIO_IMPORT_DEADLINE_EXPIRED",
            retryable=False,
        )


def _remaining_timeout(
    deadline_at: datetime,
    *,
    clock: Callable[[], datetime],
    maximum: float,
    message: str,
) -> float:
    now = clock().astimezone(UTC)
    remaining = (deadline_at - now).total_seconds()
    if remaining <= 0:
        raise AudioImportFailure(
            message,
            code="AUDIO_IMPORT_DEADLINE_EXPIRED",
            retryable=False,
        )
    return min(maximum, remaining)


def _source_records(payload: Mapping[str, Any]) -> object:
    records: object = payload.get("records")
    if records is None:
        records = payload.get("items")
    if records is None:
        data = payload.get("data")
        records = data.get("records") if isinstance(data, Mapping) else data
    return records


def _read_bounded_response(
    response: Any,
    *,
    maximum: int,
    code: str,
    deadline_at: datetime,
    clock: Callable[[], datetime],
) -> bytes:
    raw_length = response.headers.get("Content-Length")
    if raw_length is not None:
        try:
            declared_length = int(str(raw_length))
        except (TypeError, ValueError) as exc:
            raise AudioImportFailure(
                "platform response size is invalid",
                code=code,
                retryable=False,
            ) from exc
        if not 0 <= declared_length <= maximum:
            raise AudioImportFailure(
                "platform response size exceeds limit",
                code=code,
                retryable=False,
            )
    body = bytearray()
    while True:
        _ensure_deadline(
            deadline_at,
            clock=clock,
            message="audio import deadline expired during listing",
        )
        chunk = response.read(min(_READ_CHUNK_BYTES, maximum + 1 - len(body)))
        _ensure_deadline(
            deadline_at,
            clock=clock,
            message="audio import deadline expired during listing",
        )
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise AudioImportFailure(
                "platform response body is invalid",
                code=code,
                retryable=False,
            )
        body.extend(chunk)
        if len(body) > maximum:
            raise AudioImportFailure(
                "platform response size exceeds limit",
                code=code,
                retryable=False,
            )
    return bytes(body)


class PlatformAudioSourceClient:
    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver | None = None,
        opener: Callable[..., Any] | None = None,
        host_resolver: Callable[[str], set[str]] | None = None,
        connection_factory: Callable[..., Any] | None = None,
        allowed_audio_hosts: set[str] | frozenset[str] | None = None,
        timeout_seconds: float = 5.0,
        max_listing_response_bytes: int = _DEFAULT_MAX_LISTING_BYTES,
        max_audio_bytes: int = _DEFAULT_MAX_AUDIO_BYTES,
        max_total_audio_bytes: int = _DEFAULT_MAX_TOTAL_AUDIO_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise AudioImportFailure(
                "platform timeout is invalid",
                code="AUDIO_IMPORT_CONFIGURATION_INVALID",
                retryable=False,
            )
        if not 64 <= max_listing_response_bytes <= 64 * 1024 * 1024:
            raise AudioImportFailure(
                "platform listing size limit is invalid",
                code="AUDIO_IMPORT_CONFIGURATION_INVALID",
                retryable=False,
            )
        if not 44 <= max_audio_bytes <= 5 * 1024**3:
            raise AudioImportFailure(
                "platform audio size limit is invalid",
                code="AUDIO_IMPORT_CONFIGURATION_INVALID",
                retryable=False,
            )
        if not 44 <= max_total_audio_bytes <= 5 * 1024**3:
            raise AudioImportFailure(
                "platform total audio byte budget is invalid",
                code="AUDIO_IMPORT_CONFIGURATION_INVALID",
                retryable=False,
            )
        configured_hosts: set[str] = set()
        if allowed_audio_hosts is not None:
            configured_hosts.update(value.strip().casefold() for value in allowed_audio_hosts)
        else:
            configured_hosts.update(
                value.strip().casefold()
                for value in os.environ.get("AURIS_PLATFORM_AUDIO_ALLOWED_HOSTS", "").split(",")
                if value.strip()
            )
        if "*" in configured_hosts:
            raise AudioImportFailure(
                "platform audio host allowlist is invalid",
                code="AUDIO_IMPORT_CONFIGURATION_INVALID",
                retryable=False,
            )
        self.credential_resolver = credential_resolver or FileBearerCredentialResolver()
        self._opener = opener
        self._host_resolver = host_resolver or _default_host_resolver
        self._connection_factory = connection_factory
        self._ssl_context = ssl.create_default_context()
        self.allowed_audio_hosts = frozenset(configured_hosts)
        self.timeout_seconds = timeout_seconds
        self.max_listing_response_bytes = max_listing_response_bytes
        self.max_audio_bytes = max_audio_bytes
        self.max_total_audio_bytes = max_total_audio_bytes
        self._reserved_audio_bytes = 0
        self._clock = clock or (lambda: datetime.now(UTC))

    def _open_platform_request(
        self,
        request: Request,
        *,
        timeout: float,
        target: _ValidatedHttpsTarget,
    ) -> Any:
        if self._opener is not None:
            return self._opener(request, timeout=timeout)
        if self._connection_factory is None:
            connection: Any = _PinnedHTTPSConnection(
                hostname=target.hostname,
                pinned_address=target.addresses[0],
                port=target.port,
                timeout=timeout,
                context=self._ssl_context,
            )
        else:
            connection = self._connection_factory(
                hostname=target.hostname,
                pinned_address=target.addresses[0],
                port=target.port,
                timeout=timeout,
            )
        parsed = urlsplit(target.url)
        request_target = parsed.path or "/"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"
        headers = {
            str(name): str(value)
            for name, value in request.header_items()
            if str(name).casefold() != "host"
        }
        headers["Host"] = target.authority
        try:
            connection.request(
                request.get_method(),
                request_target,
                request.data,
                headers,
            )
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return _PinnedHTTPSResponse(response, connection)

    def map_record(
        self,
        envelope: AudioImportEnvelope,
        raw_record: object,
    ) -> ImportSourceRecord:
        if not isinstance(raw_record, Mapping):
            raise AudioImportFailure(
                "platform source record is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        mapping = envelope.connector.field_mapping
        raw_external_id = _mapping_value(raw_record, mapping["external_record_id"])
        if isinstance(raw_external_id, bool) or not isinstance(raw_external_id, str | int):
            raise AudioImportFailure(
                "platform external recording id is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        external_record_id = str(raw_external_id).strip()
        if (
            not external_record_id
            or len(external_record_id) > 512
            or any(ord(character) < 0x20 for character in external_record_id)
        ):
            raise AudioImportFailure(
                "platform external recording id is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        raw_audio_url = _mapping_value(raw_record, mapping["audio_url"])
        if not isinstance(raw_audio_url, str) or not raw_audio_url:
            raise AudioImportFailure(
                "platform audio URL is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        raw_started_at = _mapping_value(raw_record, mapping["started_at"])
        if not isinstance(raw_started_at, str):
            raise AudioImportFailure(
                "platform recording time is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        try:
            started_at = datetime.fromisoformat(raw_started_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AudioImportFailure(
                "platform recording time is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            ) from exc
        if started_at.tzinfo is None:
            raise AudioImportFailure(
                "platform recording time is invalid",
                code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                retryable=False,
            )
        cursor = _comparable_cursor(
            _mapping_value(raw_record, envelope.connector.cursor_policy.field)
        )
        source_metadata: dict[str, Any] = {"started_at": started_at.astimezone(UTC).isoformat()}
        for output_name in ("duration_ms", "store_ref", "agent_ref", "device_ref"):
            mapping_path = mapping.get(output_name)
            if mapping_path is None:
                continue
            value = _mapping_value(raw_record, mapping_path)
            if output_name == "duration_ms":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 24 * 60 * 60 * 1_000
                ):
                    raise AudioImportFailure(
                        "platform recording duration is invalid",
                        code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                        retryable=False,
                    )
                source_metadata[output_name] = value
            else:
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 256
                    or any(ord(character) < 0x20 for character in value)
                ):
                    raise AudioImportFailure(
                        "platform source reference is invalid",
                        code="AUDIO_IMPORT_SOURCE_RECORD_INVALID",
                        retryable=False,
                    )
                source_metadata[output_name] = value.strip()
        allowed_store_refs = envelope.connector.platform_scope.store_refs
        if allowed_store_refs and source_metadata.get("store_ref") not in allowed_store_refs:
            raise AudioImportFailure(
                "platform source record is outside the frozen store scope",
                code="AUDIO_IMPORT_SOURCE_SCOPE_MISMATCH",
                retryable=False,
            )
        return ImportSourceRecord(
            external_record_id=external_record_id,
            audio_url=raw_audio_url,
            source_metadata=source_metadata,
            cursor_value=cursor.value,
        )

    def fetch_page(
        self,
        envelope: AudioImportEnvelope,
        *,
        cursor: str | None,
    ) -> ImportSourcePage:
        connector = envelope.connector
        _ensure_deadline(
            envelope.deadline_at,
            clock=self._clock,
            message="audio import deadline expired before listing",
        )
        hostname = (urlsplit(connector.base_url).hostname or "").casefold()
        query: list[tuple[str, str | int]] = [("limit", connector.pagination.page_size)]
        effective_cursor = cursor or (
            connector.cursor_policy.initial_window_start.astimezone(UTC).isoformat()
        )
        query.append((connector.pagination.cursor_param, effective_cursor))
        request_url = f"{connector.base_url}{connector.request_path}?{urlencode(query, doseq=True)}"
        target = _validated_https_url(
            request_url,
            host_resolver=self._host_resolver,
            allowed_hosts=frozenset({hostname}),
        )
        auth_headers = self.credential_resolver.resolve(envelope)
        headers = {
            "Accept": "application/json",
            "User-Agent": "auris-flow-audio-import/1.0",
        }
        for name, value in auth_headers.items():
            if (
                not isinstance(name, str)
                or not _HEADER_NAME.fullmatch(name)
                or name.casefold() not in _SAFE_AUTH_HEADERS
                or not isinstance(value, str)
                or not value
                or "\r" in value
                or "\n" in value
            ):
                raise AudioImportFailure(
                    "platform credential headers are invalid",
                    code="AUDIO_IMPORT_CREDENTIAL_INVALID",
                    retryable=False,
                )
            headers[name] = value
        request = Request(  # noqa: S310 - URL is HTTPS, scoped and SSRF-checked above.
            request_url,
            method="GET",
            headers=headers,
        )
        try:
            response_or_error = self._open_platform_request(
                request,
                timeout=_remaining_timeout(
                    envelope.deadline_at,
                    clock=self._clock,
                    maximum=self.timeout_seconds,
                    message="audio import deadline expired before listing",
                ),
                target=target,
            )
            if isinstance(response_or_error, HTTPError):
                raise response_or_error
            with response_or_error as response:
                if int(response.status) != 200:
                    raise AudioImportFailure(
                        "platform listing returned invalid status",
                        code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                        retryable=False,
                    )
                content_type = (
                    str(response.headers.get("Content-Type") or "")
                    .partition(";")[0]
                    .strip()
                    .casefold()
                )
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise AudioImportFailure(
                        "platform listing content type is invalid",
                        code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                        retryable=False,
                    )
                body = _read_bounded_response(
                    response,
                    maximum=self.max_listing_response_bytes,
                    code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                    deadline_at=envelope.deadline_at,
                    clock=self._clock,
                )
        except AudioImportFailure:
            raise
        except HTTPError as exc:
            raise AudioImportFailure(
                "platform listing request failed",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=exc.code in {408, 425, 429} or 500 <= exc.code <= 599,
            ) from None
        except (OSError, TimeoutError, URLError, ValueError):
            raise AudioImportFailure(
                "platform listing is unavailable",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=True,
            ) from None
        _ensure_deadline(
            envelope.deadline_at,
            clock=self._clock,
            message="audio import deadline expired during listing",
        )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AudioImportFailure(
                "platform listing response is invalid",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            ) from exc
        if not isinstance(payload, Mapping):
            raise AudioImportFailure(
                "platform listing response is invalid",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            )
        raw_records = _source_records(payload)
        if not isinstance(raw_records, list) or len(raw_records) > connector.pagination.page_size:
            raise AudioImportFailure(
                "platform listing records are invalid",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            )
        records = tuple(self.map_record(envelope, raw) for raw in raw_records)
        try:
            raw_next_cursor = _mapping_value(
                payload,
                connector.pagination.next_cursor_path,
            )
        except AudioImportFailure as exc:
            raise AudioImportFailure(
                "platform pagination response is invalid",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            ) from exc
        if raw_next_cursor is None:
            next_cursor = None
        elif (
            not isinstance(raw_next_cursor, str)
            or not raw_next_cursor
            or len(raw_next_cursor) > 1_024
            or any(ord(character) < 0x20 for character in raw_next_cursor)
        ):
            raise AudioImportFailure(
                "platform pagination cursor is invalid",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            )
        else:
            next_cursor = raw_next_cursor
        return ImportSourcePage(records=records, next_cursor=next_cursor)

    def download_audio(
        self,
        envelope: AudioImportEnvelope,
        record: ImportSourceRecord,
    ) -> DownloadedAudio:
        _ensure_deadline(
            envelope.deadline_at,
            clock=self._clock,
            message="audio import deadline expired before download",
        )
        source_host = (urlsplit(envelope.connector.base_url).hostname or "").casefold()
        allowed_hosts = frozenset({source_host, *self.allowed_audio_hosts})
        target = _validated_https_url(
            record.audio_url,
            host_resolver=self._host_resolver,
            allowed_hosts=allowed_hosts,
        )
        request = Request(  # noqa: S310 - URL is HTTPS, allowlisted and SSRF-checked above.
            target.url,
            method="GET",
            headers={
                "Accept": "audio/wav, audio/x-wav",
                "User-Agent": "auris-flow-audio-import/1.0",
            },
        )
        try:
            response_or_error = self._open_platform_request(
                request,
                timeout=_remaining_timeout(
                    envelope.deadline_at,
                    clock=self._clock,
                    maximum=self.timeout_seconds,
                    message="audio import deadline expired before download",
                ),
                target=target,
            )
            if isinstance(response_or_error, HTTPError):
                raise response_or_error
            with response_or_error as response:
                status = int(response.status)
                if status in {301, 302, 303, 307, 308}:
                    raise AudioImportFailure(
                        "platform audio redirect is forbidden",
                        code="AUDIO_IMPORT_REDIRECT_FORBIDDEN",
                        retryable=False,
                    )
                if status != 200:
                    raise AudioImportFailure(
                        "platform audio request returned invalid status",
                        code="AUDIO_IMPORT_DOWNLOAD_FAILED",
                        retryable=False,
                    )
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(str(raw_length))
                except (TypeError, ValueError) as exc:
                    raise AudioImportFailure(
                        "platform audio size is invalid",
                        code="AUDIO_IMPORT_AUDIO_INVALID",
                        retryable=False,
                    ) from exc
                if not 44 <= content_length <= self.max_audio_bytes:
                    raise AudioImportFailure(
                        "platform audio size exceeds limit",
                        code="AUDIO_IMPORT_AUDIO_INVALID",
                        retryable=False,
                    )
                if self._reserved_audio_bytes + content_length > self.max_total_audio_bytes:
                    raise AudioImportFailure(
                        "audio import run byte budget exceeded",
                        code="AUDIO_IMPORT_RUN_BUDGET_EXCEEDED",
                        retryable=False,
                    )
                self._reserved_audio_bytes += content_length
                content_type = (
                    str(response.headers.get("Content-Type") or "")
                    .partition(";")[0]
                    .strip()
                    .casefold()
                )
                if content_type not in _AUDIO_TYPES:
                    raise AudioImportFailure(
                        "platform audio content type is invalid",
                        code="AUDIO_IMPORT_AUDIO_INVALID",
                        retryable=False,
                    )
                stream = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - ownership is returned.
                    max_size=1024 * 1024,
                    mode="w+b",
                )
                digest = hashlib.sha256()
                observed_length = 0
                try:
                    while True:
                        _ensure_deadline(
                            envelope.deadline_at,
                            clock=self._clock,
                            message="audio import deadline expired during download",
                        )
                        chunk = response.read(_READ_CHUNK_BYTES)
                        _ensure_deadline(
                            envelope.deadline_at,
                            clock=self._clock,
                            message="audio import deadline expired during download",
                        )
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise AudioImportFailure(
                                "platform audio body is invalid",
                                code="AUDIO_IMPORT_AUDIO_INVALID",
                                retryable=False,
                            )
                        observed_length += len(chunk)
                        if (
                            observed_length > content_length
                            or observed_length > self.max_audio_bytes
                        ):
                            raise AudioImportFailure(
                                "platform audio size exceeds limit",
                                code="AUDIO_IMPORT_AUDIO_INVALID",
                                retryable=False,
                            )
                        digest.update(chunk)
                        stream.write(chunk)
                except Exception:
                    stream.close()
                    raise
        except AudioImportFailure:
            raise
        except HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                raise AudioImportFailure(
                    "platform audio redirect is forbidden",
                    code="AUDIO_IMPORT_REDIRECT_FORBIDDEN",
                    retryable=False,
                ) from None
            raise AudioImportFailure(
                "platform audio download failed",
                code="AUDIO_IMPORT_DOWNLOAD_FAILED",
                retryable=exc.code in {408, 425, 429} or 500 <= exc.code <= 599,
            ) from None
        except (OSError, TimeoutError, URLError, ValueError):
            raise AudioImportFailure(
                "platform audio download failed",
                code="AUDIO_IMPORT_DOWNLOAD_FAILED",
                retryable=True,
            ) from None
        if observed_length != content_length:
            stream.close()
            raise AudioImportFailure(
                "platform audio body size is invalid",
                code="AUDIO_IMPORT_AUDIO_INVALID",
                retryable=False,
            )
        stream.seek(0)
        header = stream.read(12)
        if (
            len(header) != 12
            or header[:4] != b"RIFF"
            or header[8:12] != b"WAVE"
            or int.from_bytes(header[4:8], "little") + 8 != content_length
        ):
            stream.close()
            raise AudioImportFailure(
                "platform audio WAV header is invalid",
                code="AUDIO_IMPORT_AUDIO_INVALID",
                retryable=False,
            )
        try:
            stream.seek(0)
            with wave.open(stream, "rb") as reader:
                if (
                    reader.getnchannels() not in {1, 2}
                    or not 1 <= reader.getsampwidth() <= 4
                    or not 1 <= reader.getframerate() <= 384_000
                    or reader.getnframes() <= 0
                    or reader.getcomptype() != "NONE"
                ):
                    raise wave.Error("unsupported WAV parameters")
        except (EOFError, wave.Error):
            stream.close()
            raise AudioImportFailure(
                "platform audio WAV structure is invalid",
                code="AUDIO_IMPORT_AUDIO_INVALID",
                retryable=False,
            ) from None
        stream.seek(0)
        try:
            _ensure_deadline(
                envelope.deadline_at,
                clock=self._clock,
                message="audio import deadline expired during download",
            )
        except AudioImportFailure:
            stream.close()
            raise
        return DownloadedAudio(
            stream=stream,
            content_length=content_length,
            content_type=content_type,
            content_sha256=digest.hexdigest(),
        )


def _read_storage_secret(path: Path, *, minimum: int) -> str:
    try:
        metadata = path.stat()
        raw = path.read_bytes()
        value = raw.decode("utf-8").rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise AudioImportFailure(
            "audio import object storage credential is unavailable",
            code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
            retryable=False,
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not minimum <= metadata.st_size <= _MAX_SECRET_BYTES
        or not value
        or b"\x00" in raw
        or "\n" in value
        or "\r" in value
    ):
        raise AudioImportFailure(
            "audio import object storage credential is invalid",
            code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
            retryable=False,
        )
    return value


def _s3_signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"AWS4{secret_key}".encode(),
        date_stamp.encode("ascii"),
        hashlib.sha256,
    ).digest()
    region_key = hmac.new(date_key, region.encode("ascii"), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _canonical_strong_etag(raw: object) -> str:
    value = str(raw or "").strip()
    if value[:2].casefold() == "w/":
        raise AudioImportFailure(
            "audio import object ETag must be strong",
            code="AUDIO_IMPORT_STORAGE_FAILED",
            retryable=False,
        )
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    if not value or value.casefold() == "null" or not _STRONG_ETAG.fullmatch(value):
        raise AudioImportFailure(
            "audio import object ETag is missing or invalid",
            code="AUDIO_IMPORT_STORAGE_FAILED",
            retryable=False,
        )
    return value


def _stream_chunks(
    stream: IO[bytes],
    *,
    deadline_at: datetime,
    clock: Callable[[], datetime],
) -> Iterator[bytes]:
    while True:
        _ensure_deadline(
            deadline_at,
            clock=clock,
            message="audio import deadline expired during object write",
        )
        chunk = stream.read(_READ_CHUNK_BYTES)
        _ensure_deadline(
            deadline_at,
            clock=clock,
            message="audio import deadline expired during object write",
        )
        if not chunk:
            return
        if not isinstance(chunk, bytes):
            raise AudioImportFailure(
                "audio import upload stream is invalid",
                code="AUDIO_IMPORT_STORAGE_FAILED",
                retryable=False,
            )
        yield chunk


class S3VersionedAudioImportStore:
    """Content-addressed, conditional, exact-version MinIO/S3 writer."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
        region: str | None = None,
        allowed_buckets: str | None = None,
        access_key_file: str | Path | None = None,
        secret_key_file: str | Path | None = None,
        timeout_seconds: float = 15.0,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = (
            str(
                provider
                if provider is not None
                else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_PROVIDER") or ""
            )
            .strip()
            .casefold()
        )
        if self.provider not in {"minio", "s3"}:
            raise AudioImportFailure(
                "audio import object storage provider is invalid",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )
        self.endpoint = (
            str(
                endpoint
                if endpoint is not None
                else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT") or ""
            )
            .strip()
            .rstrip("/")
        )
        try:
            parsed = urlsplit(self.endpoint)
            parsed_port = parsed.port
        except ValueError as exc:
            raise AudioImportFailure(
                "audio import object storage endpoint is invalid",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            ) from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed_port is None
            and parsed.scheme == "http"
            and parsed.hostname not in {"minio", "localhost", "127.0.0.1"}
        ):
            raise AudioImportFailure(
                "audio import object storage endpoint is invalid",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )
        environment = os.environ.get("APP_ENV", "prod").strip().casefold()
        if (
            environment in {"prod", "production", "release"}
            and parsed.scheme == "http"
            and parsed.hostname not in {"minio", "localhost", "127.0.0.1"}
        ):
            raise AudioImportFailure(
                "plaintext audio import storage is forbidden",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )
        self._parsed_endpoint = parsed
        self.region = str(
            region
            if region is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_REGION") or ""
        ).strip()
        configured_allowed = (
            allowed_buckets
            if allowed_buckets is not None
            else os.environ.get("AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS", "")
        )
        self.allowed_buckets = frozenset(
            value.strip() for value in configured_allowed.split(",") if value.strip()
        )
        if not self.region or not self.allowed_buckets or "*" in self.allowed_buckets:
            raise AudioImportFailure(
                "audio import object storage allowlist is invalid",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )
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
        _read_storage_secret(self.access_key_file, minimum=8)
        _read_storage_secret(self.secret_key_file, minimum=16)
        if not 0 < timeout_seconds <= 120:
            raise AudioImportFailure(
                "audio import object storage timeout is invalid",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )
        self.timeout_seconds = timeout_seconds
        self._opener = opener or build_opener(RejectRedirectHandler()).open
        self._clock = clock or (lambda: datetime.now(UTC))

    def persist_audio(
        self,
        *,
        envelope: AudioImportEnvelope,
        record: ImportSourceRecord,
        audio: DownloadedAudio,
    ) -> ImportObjectReceipt:
        self._validate_target(envelope)
        external_digest = hashlib.sha256(record.external_record_id.encode("utf-8")).hexdigest()
        object_key = (
            f"{envelope.target.object_prefix}recordings/{external_digest[:32]}/"
            f"{audio.content_sha256}.wav"
        )
        audio.stream.seek(0)
        return self._persist(
            envelope=envelope,
            role="raw_audio",
            object_key=object_key,
            stream=audio.stream,
            content_type=audio.content_type,
            content_length=audio.content_length,
            content_sha256=audio.content_sha256,
        )

    def persist_manifest(
        self,
        *,
        envelope: AudioImportEnvelope,
        body: bytes,
        content_sha256: str,
    ) -> ImportObjectReceipt:
        self._validate_target(envelope)
        if hashlib.sha256(body).hexdigest() != content_sha256:
            raise AudioImportFailure(
                "audio import manifest hash is invalid",
                code="AUDIO_IMPORT_MANIFEST_INVALID",
                retryable=False,
            )
        object_key = f"{envelope.target.object_prefix}manifests/{content_sha256}.json"
        return self._persist(
            envelope=envelope,
            role="manifest",
            object_key=object_key,
            stream=tempfile.SpooledTemporaryFile(mode="w+b"),
            content_type="application/json",
            content_length=len(body),
            content_sha256=content_sha256,
            inline_body=body,
        )

    def _validate_target(self, envelope: AudioImportEnvelope) -> None:
        if (
            envelope.target.storage_provider != self.provider
            or envelope.target.bucket not in self.allowed_buckets
        ):
            raise AudioImportFailure(
                "audio import target is not allowed",
                code="AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
                retryable=False,
            )

    def _persist(
        self,
        *,
        envelope: AudioImportEnvelope,
        role: str,
        object_key: str,
        stream: IO[bytes],
        content_type: str,
        content_length: int,
        content_sha256: str,
        inline_body: bytes | None = None,
    ) -> ImportObjectReceipt:
        try:
            if inline_body is not None:
                stream.write(inline_body)
                stream.seek(0)
            existing = self._head(
                envelope=envelope,
                object_key=object_key,
                content_type=content_type,
                content_length=content_length,
                content_sha256=content_sha256,
                role=role,
            )
            if existing is not None:
                return existing
            stream.seek(0)
            request = self._request(
                method="PUT",
                bucket=envelope.target.bucket,
                object_key=object_key,
                content_type=content_type,
                content_length=content_length,
                content_sha256=content_sha256,
                body=_stream_chunks(
                    stream,
                    deadline_at=envelope.deadline_at,
                    clock=self._clock,
                ),
                conditional=True,
            )
            try:
                response_or_error = self._opener(
                    request,
                    timeout=_remaining_timeout(
                        envelope.deadline_at,
                        clock=self._clock,
                        maximum=self.timeout_seconds,
                        message="audio import deadline expired before object write",
                    ),
                )
                if isinstance(response_or_error, HTTPError):
                    raise response_or_error
                with response_or_error as response:
                    if int(response.status) not in {200, 201}:
                        raise AudioImportFailure(
                            "audio import object write failed",
                            code="AUDIO_IMPORT_STORAGE_FAILED",
                            retryable=True,
                        )
                    version_id = str(response.headers.get("x-amz-version-id") or "").strip()
                    etag = _canonical_strong_etag(response.headers.get("ETag"))
            except HTTPError as exc:
                if exc.code == 412:
                    concurrent = self._head(
                        envelope=envelope,
                        object_key=object_key,
                        content_type=content_type,
                        content_length=content_length,
                        content_sha256=content_sha256,
                        role=role,
                    )
                    if concurrent is not None:
                        return concurrent
                raise
            if not _VERSION_ID.fullmatch(version_id) or version_id.casefold() == "null":
                raise AudioImportFailure(
                    "audio import object version is missing",
                    code="AUDIO_IMPORT_STORAGE_FAILED",
                    retryable=True,
                )
            return self._receipt(
                envelope=envelope,
                object_key=object_key,
                version_id=version_id,
                etag=etag,
                role=role,
                content_type=content_type,
                content_length=content_length,
                content_sha256=content_sha256,
                created=True,
            )
        except AudioImportFailure:
            raise
        except (HTTPError, URLError, OSError, TimeoutError, ValueError):
            raise AudioImportFailure(
                "audio import object storage is unavailable",
                code="AUDIO_IMPORT_STORAGE_FAILED",
                retryable=True,
            ) from None
        finally:
            if inline_body is not None:
                stream.close()

    def _head(
        self,
        *,
        envelope: AudioImportEnvelope,
        object_key: str,
        content_type: str,
        content_length: int,
        content_sha256: str,
        role: str,
    ) -> ImportObjectReceipt | None:
        request = self._request(
            method="HEAD",
            bucket=envelope.target.bucket,
            object_key=object_key,
            content_type=None,
            content_length=0,
            content_sha256=_EMPTY_SHA256,
            body=None,
            conditional=False,
        )
        try:
            response_or_error = self._opener(
                request,
                timeout=_remaining_timeout(
                    envelope.deadline_at,
                    clock=self._clock,
                    maximum=self.timeout_seconds,
                    message="audio import deadline expired before object lookup",
                ),
            )
            if isinstance(response_or_error, HTTPError):
                raise response_or_error
            with response_or_error as response:
                if int(response.status) == 404:
                    return None
                if int(response.status) != 200:
                    raise AudioImportFailure(
                        "audio import object lookup failed",
                        code="AUDIO_IMPORT_STORAGE_FAILED",
                        retryable=True,
                    )
                observed_type = (
                    str(response.headers.get("Content-Type") or "")
                    .partition(";")[0]
                    .strip()
                    .casefold()
                )
                raw_length = response.headers.get("Content-Length")
                version_id = str(response.headers.get("x-amz-version-id") or "").strip()
                etag = _canonical_strong_etag(response.headers.get("ETag"))
                observed_sha256 = str(
                    response.headers.get("x-amz-meta-content-sha256") or ""
                ).strip()
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        try:
            observed_length = int(str(raw_length))
        except (TypeError, ValueError) as exc:
            raise AudioImportFailure(
                "audio import object metadata is invalid",
                code="AUDIO_IMPORT_STORAGE_FAILED",
                retryable=False,
            ) from exc
        if (
            observed_type != content_type
            or observed_length != content_length
            or not hmac.compare_digest(observed_sha256, content_sha256)
            or not _VERSION_ID.fullmatch(version_id)
            or version_id.casefold() == "null"
        ):
            raise AudioImportFailure(
                "audio import object identity collision",
                code="AUDIO_IMPORT_OBJECT_COLLISION",
                retryable=False,
            )
        return self._receipt(
            envelope=envelope,
            object_key=object_key,
            version_id=version_id,
            etag=etag,
            role=role,
            content_type=content_type,
            content_length=content_length,
            content_sha256=content_sha256,
            created=False,
        )

    def _receipt(
        self,
        *,
        envelope: AudioImportEnvelope,
        object_key: str,
        version_id: str,
        etag: str,
        role: str,
        content_type: str,
        content_length: int,
        content_sha256: str,
        created: bool,
    ) -> ImportObjectReceipt:
        object_identity = "\n".join(
            [
                self.provider,
                envelope.target.bucket,
                object_key,
                content_sha256,
            ]
        )
        return ImportObjectReceipt(
            storage_object_id=(
                f"sto_audio_import_{hashlib.sha256(object_identity.encode()).hexdigest()[:32]}"
            ),
            role=role,
            provider=self.provider,
            bucket=envelope.target.bucket,
            object_key=object_key,
            version_id=version_id,
            etag=etag,
            content_type=content_type,
            size_bytes=content_length,
            content_sha256=content_sha256,
            created=created,
        )

    def _request(
        self,
        *,
        method: str,
        bucket: str,
        object_key: str,
        content_type: str | None,
        content_length: int,
        content_sha256: str,
        body: Iterator[bytes] | None,
        conditional: bool,
    ) -> Request:
        access_key = _read_storage_secret(self.access_key_file, minimum=8)
        secret_key = _read_storage_secret(self.secret_key_file, minimum=16)
        encoded_bucket = quote(bucket, safe="-_.~")
        encoded_key = "/".join(quote(part, safe="-_.~") for part in object_key.split("/"))
        canonical_uri = f"/{encoded_bucket}/{encoded_key}"
        host = str(self._parsed_endpoint.netloc)
        now = self._clock().astimezone(UTC)
        timestamp = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = timestamp[:8]
        signed_header_values: list[tuple[str, str]] = [("host", host)]
        if content_type is not None:
            signed_header_values.append(("content-length", str(content_length)))
            signed_header_values.append(("content-type", content_type))
            if conditional:
                signed_header_values.append(("if-none-match", "*"))
            signed_header_values.append(("x-amz-meta-content-sha256", content_sha256))
        signed_header_values.extend(
            [
                ("x-amz-content-sha256", content_sha256),
                ("x-amz-date", timestamp),
            ]
        )
        signed_header_values.sort(key=lambda item: item[0])
        canonical_headers = "".join(f"{name}:{value}\n" for name, value in signed_header_values)
        signed_headers = ";".join(name for name, _value in signed_header_values)
        canonical_request = "\n".join(
            [
                method,
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
            _s3_signing_key(secret_key, date_stamp, self.region),
            string_to_sign.encode(),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={access_key}/{scope},"
            f"SignedHeaders={signed_headers},"
            f"Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Host": host,
            "x-amz-content-sha256": content_sha256,
            "x-amz-date": timestamp,
        }
        if content_type is not None:
            headers.update(
                {
                    "Content-Length": str(content_length),
                    "Content-Type": content_type,
                    "x-amz-meta-content-sha256": content_sha256,
                }
            )
            if conditional:
                headers["If-None-Match"] = "*"
        return Request(  # noqa: S310 - endpoint/bucket are configured and allowlisted.
            f"{self.endpoint}{canonical_uri}",
            data=body,
            method=method,
            headers=headers,
        )


def _failed_item(
    record: ImportSourceRecord,
    *,
    failure: AudioImportFailure,
) -> dict[str, Any]:
    return {
        "external_record_id": record.external_record_id,
        "status": "failed",
        "error_code": failure.code,
        "retryable": failure.retryable,
        "source": dict(record.source_metadata),
    }


def _successful_item(
    record: ImportSourceRecord,
    *,
    receipt: ImportObjectReceipt,
) -> dict[str, Any]:
    return {
        "external_record_id": record.external_record_id,
        "status": "succeeded",
        "storage_object_id": receipt.storage_object_id,
        "content_sha256": receipt.content_sha256,
        "object_version": receipt.version_id,
        "etag": receipt.etag,
        "source": dict(record.source_metadata),
    }


def execute_audio_import(
    *,
    envelope: AudioImportEnvelope,
    source_client: AudioImportSource,
    object_store: AudioImportObjectStore,
    progress: Callable[[str], None] | None = None,
    max_pages: int = _DEFAULT_MAX_PAGES,
    max_records: int = _DEFAULT_MAX_RECORDS,
    max_total_audio_bytes: int = _DEFAULT_MAX_TOTAL_AUDIO_BYTES,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Discover, copy, verify and materialize an immutable import manifest."""

    if (
        not 1 <= max_pages <= _DEFAULT_MAX_PAGES
        or not 1 <= max_records <= _DEFAULT_MAX_RECORDS
        or not 44 <= max_total_audio_bytes <= 5 * 1024**3
    ):
        raise AudioImportFailure(
            "audio import execution limits are invalid",
            code="AUDIO_IMPORT_CONFIGURATION_INVALID",
            retryable=False,
        )
    cursor = envelope.connector.cursor_policy.cursor_value
    initial_watermark = (
        cursor
        if cursor is not None
        else envelope.connector.cursor_policy.initial_window_start.astimezone(UTC).isoformat()
    )
    high_watermark = _comparable_cursor(initial_watermark)
    observed_cursors: set[str] = {high_watermark.value}
    last_cursor_candidate = high_watermark.value
    items: list[dict[str, Any]] = []
    audio_receipts: list[ImportObjectReceipt] = []
    observed_external_ids: set[str] = set()
    total_audio_bytes = 0
    page_count = 0
    downloading_reported = False

    while True:
        page_count += 1
        if page_count > max_pages:
            raise AudioImportFailure(
                "audio import pagination exceeds limit",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            )
        try:
            page = source_client.fetch_page(envelope, cursor=cursor)
        except AudioImportFailure:
            raise
        except Exception:
            raise AudioImportFailure(
                "audio import source listing failed",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=True,
            ) from None
        if len(items) + len(page.records) > max_records:
            raise AudioImportFailure(
                "audio import record count exceeds limit",
                code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                retryable=False,
            )
        page_high_watermark = high_watermark
        page_external_ids: set[str] = set()
        for record in page.records:
            if (
                record.external_record_id in observed_external_ids
                or record.external_record_id in page_external_ids
            ):
                raise AudioImportFailure(
                    "platform page contains a duplicate external recording id",
                    code="AUDIO_IMPORT_DUPLICATE_SOURCE_RECORD",
                    retryable=False,
                )
            page_external_ids.add(record.external_record_id)
            record_watermark = _comparable_cursor(record.cursor_value)
            if not _cursor_precedes(page_high_watermark, record_watermark):
                raise AudioImportFailure(
                    "platform cursor watermark is not strictly increasing",
                    code="AUDIO_IMPORT_CURSOR_NOT_STRICTLY_INCREASING",
                    retryable=False,
                )
            page_high_watermark = record_watermark
        normalized_next_cursor: str | None = None
        if page.next_cursor is not None:
            if not page.records:
                raise AudioImportFailure(
                    "platform returned a cursor for an empty page",
                    code="AUDIO_IMPORT_CURSOR_PAGE_MISMATCH",
                    retryable=False,
                )
            next_watermark = _comparable_cursor(page.next_cursor)
            if (
                next_watermark.kind != page_high_watermark.kind
                or next_watermark.order != page_high_watermark.order
            ):
                raise AudioImportFailure(
                    "platform next cursor does not match the page watermark",
                    code="AUDIO_IMPORT_CURSOR_PAGE_MISMATCH",
                    retryable=False,
                )
            normalized_next_cursor = next_watermark.value
            if normalized_next_cursor in observed_cursors:
                raise AudioImportFailure(
                    "audio import pagination cursor repeated",
                    code="AUDIO_IMPORT_SOURCE_LIST_FAILED",
                    retryable=False,
                )
        high_watermark = page_high_watermark
        last_cursor_candidate = high_watermark.value
        observed_external_ids.update(page_external_ids)
        if not downloading_reported and progress is not None:
            try:
                progress("downloading")
            except Exception:
                raise AudioImportFailure(
                    "audio import progress callback failed",
                    code="AUDIO_IMPORT_PROGRESS_CALLBACK_FAILED",
                    retryable=True,
                ) from None
            downloading_reported = True
        for record in page.records:
            audio: DownloadedAudio | None = None
            try:
                audio = source_client.download_audio(envelope, record)
                if total_audio_bytes + audio.content_length > max_total_audio_bytes:
                    raise AudioImportFailure(
                        "audio import run byte budget exceeded",
                        code="AUDIO_IMPORT_RUN_BUDGET_EXCEEDED",
                        retryable=False,
                    )
                total_audio_bytes += audio.content_length
                receipt = object_store.persist_audio(
                    envelope=envelope,
                    record=record,
                    audio=audio,
                )
                item = _successful_item(record, receipt=receipt)
                audio_receipts.append(receipt)
            except AudioImportFailure as failure:
                if failure.code == "AUDIO_IMPORT_RUN_BUDGET_EXCEEDED":
                    raise
                item = _failed_item(record, failure=failure)
            except Exception:
                item = _failed_item(
                    record,
                    failure=AudioImportFailure(
                        "audio import item failed",
                        code="AUDIO_IMPORT_STORAGE_FAILED",
                        retryable=True,
                    ),
                )
            finally:
                if audio is not None:
                    audio.close()
            items.append(item)
        if page.next_cursor is None:
            break
        if normalized_next_cursor is None:
            raise AudioImportFailure(
                "platform pagination cursor is invalid",
                code="AUDIO_IMPORT_CURSOR_PAGE_MISMATCH",
                retryable=False,
            )
        observed_cursors.add(normalized_next_cursor)
        if len(items) + envelope.connector.pagination.page_size > max_records:
            break
        cursor = normalized_next_cursor

    if progress is not None:
        try:
            progress("verifying")
        except Exception:
            raise AudioImportFailure(
                "audio import progress callback failed",
                code="AUDIO_IMPORT_PROGRESS_CALLBACK_FAILED",
                retryable=True,
            ) from None
    succeeded = sum(item["status"] == "succeeded" for item in items)
    # Executor results only report observed transfer outcomes. Cross-batch
    # deduplication is authoritative in the BFF and may project succeeded as skipped.
    skipped = 0
    failed = sum(item["status"] == "failed" for item in items)
    batch_status = (
        "failed"
        if failed and not succeeded and not skipped
        else "partial"
        if failed
        else "succeeded"
    )
    manifest = {
        "schema_version": AUDIO_IMPORT_MANIFEST_SCHEMA,
        "execution_contract": envelope.execution_contract,
        "execution_envelope_sha256": envelope.sha256,
        "tenant_id": envelope.tenant_id,
        "project_id": envelope.project_id,
        "trace_id": envelope.trace_id,
        "root_trace_id": envelope.root_trace_id,
        "run_id": envelope.run_id,
        "import_batch_id": envelope.import_batch_id,
        "connector_id": envelope.connector.connector_id,
        "connector_version": envelope.connector.connector_version,
        "platform_connection_id": envelope.connector.platform_connection_id,
        "connector_snapshot_sha256": hashlib.sha256(
            canonical_json_bytes(envelope.as_mapping()["connector"])
        ).hexdigest(),
        "target_asset_key": envelope.target.target_asset_key,
        "batch_status": batch_status,
        "next_cursor_candidate": last_cursor_candidate,
        "items": items,
    }
    manifest_body = canonical_json_bytes(manifest)
    if len(manifest_body) > _MAX_MANIFEST_BYTES:
        raise AudioImportFailure(
            "audio import manifest exceeds size limit",
            code="AUDIO_IMPORT_MANIFEST_INVALID",
            retryable=False,
        )
    manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
    try:
        manifest_receipt = object_store.persist_manifest(
            envelope=envelope,
            body=manifest_body,
            content_sha256=manifest_sha256,
        )
    except AudioImportFailure:
        raise
    except Exception:
        raise AudioImportFailure(
            "audio import manifest persistence failed",
            code="AUDIO_IMPORT_MANIFEST_PERSISTENCE_FAILED",
            retryable=True,
        ) from None
    metrics: dict[str, Any] = {
        "total": len(items),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "next_cursor_candidate": last_cursor_candidate,
    }
    internal_result_ref = {
        "schema_version": AUDIO_IMPORT_RESULT_SCHEMA,
        "execution_contract": envelope.execution_contract,
        "execution_envelope_sha256": envelope.sha256,
        "import_batch_id": envelope.import_batch_id,
        "batch_status": batch_status,
        "manifest": manifest_receipt.as_mapping(),
        "manifest_storage_object_id": manifest_receipt.storage_object_id,
        "manifest_sha256": manifest_sha256,
        "items": items,
        "next_cursor_candidate": last_cursor_candidate,
        "storage_objects": [
            *(receipt.as_mapping() for receipt in audio_receipts),
            manifest_receipt.as_mapping(),
        ],
    }
    public_result_ref = {
        "schema_version": AUDIO_IMPORT_RESULT_SCHEMA,
        "execution_contract": envelope.execution_contract,
        "execution_envelope_sha256": envelope.sha256,
        "import_batch_id": envelope.import_batch_id,
        "batch_status": batch_status,
        "manifest_storage_object_id": manifest_receipt.storage_object_id,
        "manifest_sha256": manifest_sha256,
        "next_cursor_candidate": last_cursor_candidate,
    }
    return public_result_ref, internal_result_ref, metrics


def execute_audio_import_and_report(
    *,
    scope: AurisRunContext,
    dagster_run_id: str,
    envelope: AudioImportEnvelope,
    callback: CompletionCallbackClient,
    source_client: AudioImportSource | None = None,
    object_store: AudioImportObjectStore | None = None,
) -> Mapping[str, Any]:
    """Run the import and deliver its exact-version manifest through signed completion."""

    def report_progress(stage: str) -> None:
        callback.post_progress(
            scope,
            dagster_run_id=dagster_run_id,
            import_batch_id=envelope.import_batch_id,
            stage=stage,
            deadline_at=envelope.deadline_at,
        )

    try:
        public_result, internal_result, metrics = execute_audio_import(
            envelope=envelope,
            source_client=source_client or PlatformAudioSourceClient(),
            object_store=object_store or S3VersionedAudioImportStore(),
            progress=report_progress,
        )
    except AudioImportFailure as failure:
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                result_ref={
                    "schema_version": AUDIO_IMPORT_RESULT_SCHEMA,
                    "execution_contract": envelope.execution_contract,
                    "execution_envelope_sha256": envelope.sha256,
                    "import_batch_id": envelope.import_batch_id,
                    "batch_status": "failed",
                },
                metrics={
                    "total": 0,
                    "succeeded": 0,
                    "skipped": 0,
                    "failed": 0,
                    "next_cursor_candidate": None,
                },
                error_code=failure.code,
                retryable=failure.retryable,
            )
        except Exception:
            raise AurisWorkflowError(
                "Auris Flow audio import and completion callback failed"
            ) from None
        raise AurisWorkflowError("Auris Flow audio import execution failed") from None
    try:
        callback.post(
            scope,
            dagster_run_id=dagster_run_id,
            status="success",
            result_ref=internal_result,
            metrics=metrics,
        )
    except Exception:
        raise AurisWorkflowError("Auris Flow audio import result callback failed") from None
    return public_result


__all__ = [
    "AUDIO_IMPORT_MANIFEST_SCHEMA",
    "AUDIO_IMPORT_RESULT_SCHEMA",
    "AudioImportFailure",
    "DownloadedAudio",
    "FileBearerCredentialResolver",
    "ImportObjectReceipt",
    "ImportSourcePage",
    "ImportSourceRecord",
    "PlatformAudioSourceClient",
    "S3VersionedAudioImportStore",
    "execute_audio_import",
    "execute_audio_import_and_report",
]
