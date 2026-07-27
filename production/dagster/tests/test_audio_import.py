from __future__ import annotations

import hashlib
import io
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from auris_flow_dagster import audio_import as audio_import_module
from auris_flow_dagster.audio_import import (
    AudioImportFailure,
    DownloadedAudio,
    FileBearerCredentialResolver,
    ImportObjectReceipt,
    PlatformAudioSourceClient,
    S3VersionedAudioImportStore,
    execute_audio_import,
    execute_audio_import_and_report,
)
from auris_flow_dagster.contracts import (
    AUDIO_IMPORT_EXECUTION_CONTRACT,
    AudioImportEnvelope,
    AurisContractError,
    validate_audio_import_envelope,
)
from auris_flow_dagster.runtime import AurisWorkflowError


def _wav_body(payload: bytes = b"test-audio") -> bytes:
    data_size = len(payload)
    return (
        b"RIFF"
        + (36 + data_size).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little")
        + (8_000).to_bytes(4, "little")
        + (16_000).to_bytes(4, "little")
        + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
        + payload
    )


def _import_context(valid_context: dict[str, Any]) -> dict[str, Any]:
    return {**valid_context, "event_type": "task_run.requested"}


def _import_envelope(valid_context: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": AUDIO_IMPORT_EXECUTION_CONTRACT,
        "tenant_id": valid_context["tenant_id"],
        "project_id": valid_context["project_id"],
        "trace_id": valid_context["trace_id"],
        "root_trace_id": "root_trace_import_001",
        "run_id": valid_context["run_id"],
        "dispatch_idempotency_key": valid_context["dispatch_idempotency_key"],
        "outbox_fencing_token": valid_context["outbox_fencing_token"],
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "import_batch_id": "import_batch_001",
        "connector": {
            "connector_id": "connector_platform_001",
            "connector_version": "connector_version_003",
            "platform_connection_id": "platform_connection_001",
            "platform_scope": {
                "tenant_ref": "external_tenant_001",
                "store_refs": ["store-001"],
            },
            "source_type": "platform_audio_url_api",
            "base_url": "https://platform.example.test",
            "request_path": "/api/v1/recordings",
            "credential_ref": "platform_primary",
            "pagination": {
                "mode": "cursor",
                "next_cursor_path": "data.next_cursor",
                "cursor_param": "cursor",
                "page_size": 2,
            },
            "field_mapping": {
                "external_record_id": "recordingId",
                "audio_url": "audioUrl",
                "started_at": "startedAt",
                "duration_ms": "durationMs",
                "store_ref": "storeId",
            },
            "cursor_policy": {
                "field": "updatedAt",
                "initial_window_start": "2026-07-20T00:00:00+00:00",
                "cursor_value": "2026-07-20T00:00:00+00:00",
            },
        },
        "target": {
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_prefix": (
                "tenants/aurora_auto/projects/sales_qa/runs/run_task_001/audio-import/"
            ),
            "target_asset_key": "auris/sources/platform_audio",
            "dedupe_policy": "external_id_checksum",
        },
    }


def test_audio_import_envelope_is_strict_bound_and_canonical(
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)

    parsed = validate_audio_import_envelope(_import_envelope(context), auris_context=context)

    assert isinstance(parsed, AudioImportEnvelope)
    assert parsed.import_batch_id == "import_batch_001"
    assert parsed.trace_id == valid_context["trace_id"]
    assert parsed.root_trace_id == "root_trace_import_001"
    assert parsed.connector.credential_ref == "platform_primary"
    assert parsed.target.object_prefix.endswith("/runs/run_task_001/audio-import/")
    assert parsed.sha256 == hashlib.sha256(parsed.canonical_json.encode()).hexdigest()
    assert "credential_ref='platform_primary'" not in repr(parsed)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.__setitem__("root_trace_id", "\n"), "root_trace_id"),
        (lambda value: value.__setitem__("tenant_id", "other_tenant"), "tenant_id"),
        (
            lambda value: value["target"].__setitem__(
                "object_prefix",
                "tenants/aurora_auto/projects/other/runs/run_task_001/audio-import/",
            ),
            "object_prefix",
        ),
        (
            lambda value: value["connector"].__setitem__(
                "base_url", "https://user:secret@platform.example.test"
            ),
            "base_url",
        ),
        (
            lambda value: value["connector"].__setitem__("request_path", "/api/../admin"),
            "request_path",
        ),
        (
            lambda value: value["connector"]["field_mapping"].pop("audio_url"),
            "audio_url",
        ),
        (
            lambda value: value["connector"]["pagination"].__setitem__("page_size", 0),
            "page_size",
        ),
        (
            lambda value: value["connector"]["pagination"].__setitem__("page_size", 251),
            "page_size",
        ),
        (lambda value: value.__setitem__("unexpected", "value"), "unexpected"),
    ],
)
def test_audio_import_envelope_rejects_scope_drift_unsafe_source_and_extra_fields(
    valid_context: dict[str, Any],
    mutation: Any,
    message: str,
) -> None:
    context = _import_context(valid_context)
    envelope = deepcopy(_import_envelope(context))
    mutation(envelope)

    with pytest.raises(AurisContractError, match=message):
        validate_audio_import_envelope(envelope, auris_context=context)


class _HTTPResponse:
    status = 200

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        content_length: int | None = None,
        status: int = 200,
    ) -> None:
        self.status = status
        self._body = body
        headers = Message()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body) if content_length is None else content_length)
        self.headers = headers

    def __enter__(self) -> _HTTPResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result, self._body = self._body, b""
            return result
        result, self._body = self._body[:size], self._body[size:]
        return result


class _BearerResolver:
    def resolve(self, envelope: AudioImportEnvelope) -> dict[str, str]:
        assert envelope.connector.credential_ref == "platform_primary"
        assert envelope.connector.platform_connection_id == "platform_connection_001"
        return {"Authorization": "Bearer unit-only-secret"}


def _parsed_envelope(valid_context: dict[str, Any]) -> AudioImportEnvelope:
    context = _import_context(valid_context)
    return validate_audio_import_envelope(
        _import_envelope(context),
        auris_context=context,
    )


def test_file_credential_resolver_uses_opaque_binding_without_path_interpolation(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    credential_ref = "secret://platform/recordings-reader"
    envelope = replace(
        envelope,
        connector=replace(envelope.connector, credential_ref=credential_ref),
    )
    binding_file = tmp_path / "platform-credential-bindings.json"
    binding_file.write_text(
        json.dumps(
            {
                credential_ref: {
                    "tenant_id": envelope.tenant_id,
                    "project_id": envelope.project_id,
                    "platform_connection_id": envelope.connector.platform_connection_id,
                    "platform_tenant_ref": envelope.connector.platform_scope.tenant_ref,
                    "base_url": envelope.connector.base_url,
                    "headers": {
                        "Authorization": "Bearer unit-only-platform-secret",
                        "X-Auth-Token": "unit-only-secondary-secret",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    binding_file.chmod(0o400)

    resolver = FileBearerCredentialResolver(bindings_file=binding_file)
    resolved = resolver.resolve(envelope)

    assert resolved == {
        "Authorization": "Bearer unit-only-platform-secret",
        "X-Auth-Token": "unit-only-secondary-secret",
    }
    assert credential_ref not in str(binding_file)

    mismatched = replace(
        envelope,
        connector=replace(
            envelope.connector,
            base_url="https://credential-exfiltration.example.test",
        ),
    )
    with pytest.raises(AudioImportFailure, match="binding"):
        resolver.resolve(mismatched)


def test_audio_import_scope_requires_store_mapping_and_rejects_out_of_scope_record(
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)
    missing_mapping = _import_envelope(context)
    missing_mapping["connector"]["field_mapping"].pop("store_ref")
    with pytest.raises(AurisContractError, match="store_ref"):
        validate_audio_import_envelope(missing_mapping, auris_context=context)

    envelope = _parsed_envelope(valid_context)
    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        opener=lambda *_args, **_kwargs: pytest.fail("out-of-scope record must not be requested"),
        host_resolver=lambda _host: {"8.8.8.8"},
    )
    with pytest.raises(AudioImportFailure, match="scope"):
        client.map_record(
            envelope,
            {
                "recordingId": "ext-other-store",
                "audioUrl": "https://platform.example.test/audio.wav",
                "startedAt": "2026-07-20T10:00:00+00:00",
                "updatedAt": "2026-07-20T10:00:01+00:00",
                "durationMs": 1,
                "storeId": "store-outside-frozen-scope",
            },
        )


def test_platform_client_paginates_maps_records_and_stream_validates_wav(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    wav = _wav_body()
    requests: list[Request] = []
    pages = [
        {
            "data": {
                "records": [
                    {
                        "recordingId": "ext-001",
                        "audioUrl": "https://cdn.example.test/audio/001.wav?sig=redacted",
                        "startedAt": "2026-07-20T10:00:00+00:00",
                        "updatedAt": "2026-07-20T10:00:01+00:00",
                        "durationMs": 1_250,
                        "storeId": "store-001",
                    }
                ],
                "next_cursor": "2026-07-20T10:00:01+00:00",
            },
        },
        {"data": {"records": [], "next_cursor": None}},
    ]

    def opener(request: Request, *, timeout: float) -> _HTTPResponse:
        assert timeout == 5
        requests.append(request)
        if request.full_url.startswith("https://platform.example.test/"):
            body = json.dumps(pages.pop(0)).encode()
            return _HTTPResponse(body, content_type="application/json")
        return _HTTPResponse(wav, content_type="audio/wav")

    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        opener=opener,
        host_resolver=lambda _host: {"8.8.8.8"},
        allowed_audio_hosts={"cdn.example.test"},
        clock=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    page_one = client.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )
    downloaded = client.download_audio(envelope, page_one.records[0])

    assert page_one.next_cursor == "2026-07-20T10:00:01+00:00"
    assert page_one.records[0].external_record_id == "ext-001"
    assert page_one.records[0].cursor_value == "2026-07-20T10:00:01+00:00"
    assert page_one.records[0].source_metadata == {
        "started_at": "2026-07-20T10:00:00+00:00",
        "duration_ms": 1_250,
        "store_ref": "store-001",
    }
    assert downloaded.content_sha256 == hashlib.sha256(wav).hexdigest()
    assert downloaded.content_length == len(wav)
    assert downloaded.stream.read() == wav
    downloaded.close()
    assert "cursor=2026-07-20T00%3A00%3A00%2B00%3A00" in requests[0].full_url
    assert requests[0].get_header("Authorization") == "Bearer unit-only-secret"
    assert requests[1].get_header("Authorization") is None
    assert "sig=redacted" not in repr(page_one.records[0])


def test_platform_default_transport_pins_verified_ip_without_second_logical_dns_resolution(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    resolver_calls: list[str] = []
    connection_values: list[dict[str, Any]] = []
    request_values: list[dict[str, Any]] = []

    def host_resolver(hostname: str) -> set[str]:
        resolver_calls.append(hostname)
        if len(resolver_calls) > 1:
            return {"127.0.0.1"}
        return {"8.8.8.8"}

    class FakePinnedConnection:
        def request(
            self,
            method: str,
            target: str,
            body: Any,
            headers: dict[str, str],
        ) -> None:
            request_values.append(
                {
                    "method": method,
                    "target": target,
                    "body": body,
                    "headers": headers,
                }
            )

        def getresponse(self) -> _HTTPResponse:
            return _HTTPResponse(
                json.dumps({"data": {"records": [], "next_cursor": None}}).encode(),
                content_type="application/json",
            )

        def close(self) -> None:
            connection_values[-1]["closed"] = True

    def connection_factory(
        *,
        hostname: str,
        pinned_address: str,
        port: int,
        timeout: float,
    ) -> FakePinnedConnection:
        connection_values.append(
            {
                "hostname": hostname,
                "pinned_address": pinned_address,
                "port": port,
                "timeout": timeout,
                "closed": False,
            }
        )
        return FakePinnedConnection()

    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        host_resolver=host_resolver,
        connection_factory=connection_factory,
        clock=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    page = client.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )

    assert page.records == ()
    assert resolver_calls == ["platform.example.test"]
    assert connection_values == [
        {
            "hostname": "platform.example.test",
            "pinned_address": "8.8.8.8",
            "port": 443,
            "timeout": 5.0,
            "closed": True,
        }
    ]
    assert request_values[0]["method"] == "GET"
    assert request_values[0]["target"].startswith("/api/v1/recordings?")
    assert request_values[0]["headers"]["Host"] == "platform.example.test"
    assert request_values[0]["headers"]["Authorization"] == "Bearer unit-only-secret"


def test_pinned_https_connection_uses_verified_ip_and_logical_hostname_for_tls_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_values: dict[str, Any] = {}
    tls_values: dict[str, Any] = {}

    class FakeSocket:
        def settimeout(self, timeout: float) -> None:
            socket_values["timeout"] = timeout

        def connect(self, destination: tuple[Any, ...]) -> None:
            socket_values["destination"] = destination

        def close(self) -> None:
            socket_values["closed"] = True

    class FakeContext:
        def wrap_socket(self, raw_socket: Any, *, server_hostname: str) -> object:
            tls_values["raw_socket"] = raw_socket
            tls_values["server_hostname"] = server_hostname
            return object()

    raw_socket = FakeSocket()
    monkeypatch.setattr(
        audio_import_module.socket,
        "socket",
        lambda family, kind: socket_values.update({"family": family, "kind": kind}) or raw_socket,
    )
    connection = audio_import_module._PinnedHTTPSConnection(
        hostname="platform.example.test",
        pinned_address="8.8.8.8",
        port=443,
        timeout=4.5,
        context=FakeContext(),  # type: ignore[arg-type]
    )

    connection.connect()

    assert socket_values == {
        "family": audio_import_module.socket.AF_INET,
        "kind": audio_import_module.socket.SOCK_STREAM,
        "timeout": 4.5,
        "destination": ("8.8.8.8", 443),
    }
    assert tls_values == {
        "raw_socket": raw_socket,
        "server_hostname": "platform.example.test",
    }


@pytest.mark.parametrize(
    ("audio_url", "resolved", "message"),
    [
        ("https://user:secret@cdn.example.test/audio.wav", {"8.8.8.8"}, "unsafe"),
        ("https://cdn.example.test/audio.wav", {"127.0.0.1"}, "private"),
        ("http://cdn.example.test/audio.wav", {"8.8.8.8"}, "HTTPS"),
        ("https://not-allowed.example.test/audio.wav", {"8.8.8.8"}, "allowed"),
    ],
)
def test_platform_client_rejects_credential_private_plaintext_and_unallowed_audio_urls(
    valid_context: dict[str, Any],
    audio_url: str,
    resolved: set[str],
    message: str,
) -> None:
    envelope = _parsed_envelope(valid_context)
    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        opener=lambda *_args, **_kwargs: pytest.fail("unsafe URL must not be requested"),
        host_resolver=lambda _host: resolved,
        allowed_audio_hosts={"cdn.example.test"},
    )
    source = replace(
        client.map_record(
            envelope,
            {
                "recordingId": "ext-unsafe",
                "audioUrl": "https://cdn.example.test/safe.wav",
                "startedAt": "2026-07-20T10:00:00+00:00",
                "updatedAt": "2026-07-20T10:00:01+00:00",
                "durationMs": 1,
                "storeId": "store-001",
            },
        ),
        audio_url=audio_url,
    )

    with pytest.raises(AudioImportFailure, match=message):
        client.download_audio(envelope, source)


def test_platform_client_rejects_redirect_oversize_and_non_wav_before_storage(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    source_payload = {
        "recordingId": "ext-invalid",
        "audioUrl": "https://cdn.example.test/audio.wav",
        "startedAt": "2026-07-20T10:00:00+00:00",
        "updatedAt": "2026-07-20T10:00:01+00:00",
        "durationMs": 1,
        "storeId": "store-001",
    }

    def client_for(response: Any, *, maximum: int = 128) -> PlatformAudioSourceClient:
        return PlatformAudioSourceClient(
            credential_resolver=_BearerResolver(),
            opener=lambda *_args, **_kwargs: response,
            host_resolver=lambda _host: {"8.8.8.8"},
            allowed_audio_hosts={"cdn.example.test"},
            max_audio_bytes=maximum,
        )

    redirect = HTTPError(
        "https://cdn.example.test/audio.wav",
        302,
        "redirect",
        Message(),
        None,
    )
    redirect_client = client_for(redirect)
    redirect_source = redirect_client.map_record(envelope, source_payload)
    with pytest.raises(AudioImportFailure, match="redirect"):
        redirect_client.download_audio(envelope, redirect_source)

    oversized_client = client_for(
        _HTTPResponse(_wav_body(), content_type="audio/wav", content_length=129)
    )
    oversized_source = oversized_client.map_record(envelope, source_payload)
    with pytest.raises(AudioImportFailure, match="size"):
        oversized_client.download_audio(envelope, oversized_source)

    invalid_client = client_for(_HTTPResponse(b"not a wave file" * 4, content_type="audio/wav"))
    invalid_source = invalid_client.map_record(envelope, source_payload)
    with pytest.raises(AudioImportFailure, match="WAV"):
        invalid_client.download_audio(envelope, invalid_source)

    inconsistent_riff = bytearray(_wav_body())
    inconsistent_riff[4:8] = (1).to_bytes(4, "little")
    inconsistent_client = client_for(
        _HTTPResponse(bytes(inconsistent_riff), content_type="audio/wav")
    )
    inconsistent_source = inconsistent_client.map_record(envelope, source_payload)
    with pytest.raises(AudioImportFailure, match="WAV"):
        inconsistent_client.download_audio(envelope, inconsistent_source)


def test_platform_client_stops_slow_download_at_frozen_deadline(
    valid_context: dict[str, Any],
) -> None:
    envelope = replace(
        _parsed_envelope(valid_context),
        deadline_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC),
    )
    source_payload = {
        "recordingId": "ext-slow",
        "audioUrl": "https://cdn.example.test/audio.wav",
        "startedAt": "2026-07-20T10:00:00+00:00",
        "updatedAt": "2026-07-20T10:00:01+00:00",
        "durationMs": 1,
        "storeId": "store-001",
    }
    wav = _wav_body(b"x" * (256 * 1024))
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]

    class SlowResponse(_HTTPResponse):
        read_calls = 0

        def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            chunk = super().read(size)
            if self.read_calls == 1:
                now[0] = datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)
            return chunk

    response = SlowResponse(wav, content_type="audio/wav")
    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        opener=lambda *_args, **_kwargs: response,
        host_resolver=lambda _host: {"8.8.8.8"},
        allowed_audio_hosts={"cdn.example.test"},
        clock=lambda: now[0],
    )
    source = client.map_record(envelope, source_payload)

    with pytest.raises(AudioImportFailure, match="deadline"):
        client.download_audio(envelope, source)

    assert response.read_calls == 1


def test_platform_client_reserves_failed_item_bytes_before_reading_body(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    source_payload = {
        "recordingId": "ext-over-budget",
        "audioUrl": "https://cdn.example.test/audio.wav",
        "startedAt": "2026-07-20T10:00:00+00:00",
        "updatedAt": "2026-07-20T10:00:01+00:00",
        "durationMs": 1,
        "storeId": "store-001",
    }
    wav = _wav_body(b"x" * 64)

    class CountingResponse(_HTTPResponse):
        read_calls = 0

        def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            return super().read(size)

    response = CountingResponse(wav, content_type="audio/wav")
    client = PlatformAudioSourceClient(
        credential_resolver=_BearerResolver(),
        opener=lambda *_args, **_kwargs: response,
        host_resolver=lambda _host: {"8.8.8.8"},
        allowed_audio_hosts={"cdn.example.test"},
        max_total_audio_bytes=len(wav) - 1,
    )
    source = client.map_record(envelope, source_payload)

    with pytest.raises(AudioImportFailure, match="budget"):
        client.download_audio(envelope, source)

    assert response.read_calls == 0


class _RecordingCallback:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.progress_calls: list[dict[str, Any]] = []

    def post(self, _scope: Any, **values: Any) -> dict[str, Any]:
        self.calls.append(values)
        return {"data": {"status": values["status"]}}

    def post_progress(self, _scope: Any, **values: Any) -> dict[str, Any]:
        self.progress_calls.append(values)
        return {"data": {"current_stage": values["stage"]}}


class _FakeSource:
    def __init__(self, wav: bytes) -> None:
        self.wav = wav
        self.page_calls: list[str | None] = []

    def fetch_page(
        self,
        envelope: AudioImportEnvelope,
        *,
        cursor: str | None,
    ) -> Any:
        del envelope
        self.page_calls.append(cursor)
        if len(self.page_calls) == 1:
            from auris_flow_dagster.audio_import import ImportSourcePage, ImportSourceRecord

            records = (
                ImportSourceRecord(
                    external_record_id="ext-001",
                    audio_url="https://cdn.example.test/001.wav",
                    source_metadata={"started_at": "2026-07-20T10:00:00+00:00"},
                    cursor_value="2026-07-20T10:00:01+00:00",
                ),
                ImportSourceRecord(
                    external_record_id="ext-002",
                    audio_url="https://cdn.example.test/002.wav",
                    source_metadata={"started_at": "2026-07-20T10:01:00+00:00"},
                    cursor_value="2026-07-20T10:01:01+00:00",
                ),
            )
            return ImportSourcePage(
                records=records,
                next_cursor="2026-07-20T10:01:01+00:00",
            )
        from auris_flow_dagster.audio_import import ImportSourcePage

        return ImportSourcePage(records=(), next_cursor=None)

    def download_audio(self, _envelope: Any, record: Any) -> Any:
        from auris_flow_dagster.audio_import import DownloadedAudio

        if record.external_record_id == "ext-002":
            raise AudioImportFailure(
                "audio download failed",
                code="AUDIO_IMPORT_DOWNLOAD_FAILED",
                retryable=True,
            )
        return DownloadedAudio(
            stream=io.BytesIO(self.wav),
            content_length=len(self.wav),
            content_type="audio/wav",
            content_sha256=hashlib.sha256(self.wav).hexdigest(),
        )


class _FakeStore:
    def __init__(self) -> None:
        self.audio_calls: list[dict[str, Any]] = []
        self.manifest_bodies: list[bytes] = []

    def persist_audio(self, **values: Any) -> ImportObjectReceipt:
        self.audio_calls.append(values)
        body = values["audio"].stream.read()
        assert hashlib.sha256(body).hexdigest() == values["audio"].content_sha256
        return ImportObjectReceipt(
            storage_object_id="sto_audio_001",
            role="raw_audio",
            provider="minio",
            bucket="auris-flow",
            object_key=(
                "tenants/aurora_auto/projects/sales_qa/runs/run_task_001/"
                f"audio-import/recordings/{values['audio'].content_sha256}.wav"
            ),
            version_id="version-audio-001",
            etag="audio-etag-001",
            content_type="audio/wav",
            size_bytes=len(body),
            content_sha256=values["audio"].content_sha256,
            created=True,
        )

    def persist_manifest(
        self,
        *,
        envelope: AudioImportEnvelope,
        body: bytes,
        content_sha256: str,
    ) -> ImportObjectReceipt:
        self.manifest_bodies.append(body)
        assert hashlib.sha256(body).hexdigest() == content_sha256
        return ImportObjectReceipt(
            storage_object_id="sto_manifest_001",
            role="manifest",
            provider="minio",
            bucket=envelope.target.bucket,
            object_key=f"{envelope.target.object_prefix}manifests/{content_sha256}.json",
            version_id="version-manifest-001",
            etag="manifest-etag-001",
            content_type="application/json",
            size_bytes=len(body),
            content_sha256=content_sha256,
            created=True,
        )


def test_audio_import_reports_versioned_objects_items_metrics_and_partial_manifest(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)
    envelope = validate_audio_import_envelope(
        _import_envelope(context),
        auris_context=context,
    )
    callback = _RecordingCallback()
    source = _FakeSource(_wav_body())
    store = _FakeStore()

    public_result = execute_audio_import_and_report(
        scope=replace(scope, event_type="task_run.requested"),
        dagster_run_id="dagster-import-partial",
        envelope=envelope,
        callback=callback,  # type: ignore[arg-type]
        source_client=source,  # type: ignore[arg-type]
        object_store=store,  # type: ignore[arg-type]
    )

    result_ref = callback.calls[0]["result_ref"]
    assert callback.calls[0]["status"] == "success"
    assert callback.progress_calls == [
        {
            "dagster_run_id": "dagster-import-partial",
            "import_batch_id": "import_batch_001",
            "stage": "downloading",
            "deadline_at": envelope.deadline_at,
        },
        {
            "dagster_run_id": "dagster-import-partial",
            "import_batch_id": "import_batch_001",
            "stage": "verifying",
            "deadline_at": envelope.deadline_at,
        },
    ]
    assert callback.calls[0]["metrics"] == {
        "total": 2,
        "succeeded": 1,
        "skipped": 0,
        "failed": 1,
        "next_cursor_candidate": "2026-07-20T10:01:01+00:00",
    }
    assert result_ref["schema_version"] == "auris-flow-audio-import-result-v1"
    assert result_ref["execution_contract"] == "auris-flow-audio-import-v1"
    assert result_ref["execution_envelope_sha256"] == envelope.sha256
    assert result_ref["import_batch_id"] == "import_batch_001"
    assert result_ref["batch_status"] == "partial"
    assert result_ref["manifest"]["storage_object_id"] == "sto_manifest_001"
    assert result_ref["manifest_sha256"] == hashlib.sha256(store.manifest_bodies[0]).hexdigest()
    assert len(result_ref["items"]) == 2
    assert result_ref["items"][0]["status"] == "succeeded"
    assert result_ref["items"][0]["storage_object_id"] == "sto_audio_001"
    assert result_ref["items"][0]["object_version"] == "version-audio-001"
    assert result_ref["items"][0]["etag"] == "audio-etag-001"
    assert result_ref["items"][1] == {
        "external_record_id": "ext-002",
        "status": "failed",
        "error_code": "AUDIO_IMPORT_DOWNLOAD_FAILED",
        "retryable": True,
        "source": {"started_at": "2026-07-20T10:01:00+00:00"},
    }
    assert {item["status"] for item in result_ref["items"]} <= {
        "succeeded",
        "failed",
    }
    assert {value["role"] for value in result_ref["storage_objects"]} == {
        "raw_audio",
        "manifest",
    }
    assert public_result["manifest_sha256"] == result_ref["manifest_sha256"]
    assert "storage_objects" not in public_result
    manifest = json.loads(store.manifest_bodies[0])
    assert manifest["schema_version"] == "auris-flow-audio-import-manifest-v1"
    assert manifest["batch_status"] == "partial"
    assert manifest["execution_envelope_sha256"] == envelope.sha256
    assert manifest["items"][0]["external_record_id"] == "ext-001"
    assert manifest["items"][0]["etag"] == "audio-etag-001"
    assert result_ref["manifest"]["etag"] == "manifest-etag-001"
    assert {value["etag"] for value in result_ref["storage_objects"]} == {
        "audio-etag-001",
        "manifest-etag-001",
    }
    assert "audio_url" not in repr(manifest)
    assert "unit-only-secret" not in repr(callback.calls)


def test_audio_import_all_item_failures_are_operational_success_with_failed_batch(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)
    envelope = validate_audio_import_envelope(
        _import_envelope(context),
        auris_context=context,
    )
    callback = _RecordingCallback()

    class AllFailSource(_FakeSource):
        def download_audio(self, _envelope: Any, _record: Any) -> Any:
            raise AudioImportFailure(
                "audio download failed",
                code="AUDIO_IMPORT_DOWNLOAD_FAILED",
                retryable=True,
            )

    execute_audio_import_and_report(
        scope=replace(scope, event_type="task_run.requested"),
        dagster_run_id="dagster-import-all-failed",
        envelope=envelope,
        callback=callback,  # type: ignore[arg-type]
        source_client=AllFailSource(_wav_body()),  # type: ignore[arg-type]
        object_store=_FakeStore(),  # type: ignore[arg-type]
    )

    assert callback.calls[0]["status"] == "success"
    assert callback.calls[0]["result_ref"]["batch_status"] == "failed"
    assert callback.calls[0]["metrics"]["failed"] == 2
    assert {item["error_code"] for item in callback.calls[0]["result_ref"]["items"]} == {
        "AUDIO_IMPORT_DOWNLOAD_FAILED"
    }


def test_audio_import_progress_delivery_failure_reports_failed_completion_without_download(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)

    class ProgressFailingCallback(_RecordingCallback):
        def post_progress(self, _scope: Any, **_values: Any) -> dict[str, Any]:
            raise RuntimeError("sensitive transport failure")

    callback = ProgressFailingCallback()

    with pytest.raises(AurisWorkflowError, match="audio import execution failed"):
        execute_audio_import_and_report(
            scope=replace(scope, event_type="task_run.requested"),
            dagster_run_id="dagster-import-progress-failed",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
            source_client=_FakeSource(_wav_body()),  # type: ignore[arg-type]
            object_store=_FakeStore(),  # type: ignore[arg-type]
        )

    assert callback.calls[0]["status"] == "failed"
    assert callback.calls[0]["error_code"] == "AUDIO_IMPORT_PROGRESS_CALLBACK_FAILED"
    assert callback.calls[0]["result_ref"] == {
        "schema_version": "auris-flow-audio-import-result-v1",
        "execution_contract": "auris-flow-audio-import-v1",
        "execution_envelope_sha256": envelope.sha256,
        "import_batch_id": "import_batch_001",
        "batch_status": "failed",
    }
    assert "sensitive transport failure" not in repr(callback.calls)


def test_audio_import_empty_window_is_succeeded_with_immutable_manifest(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)
    envelope = validate_audio_import_envelope(
        _import_envelope(context),
        auris_context=context,
    )
    callback = _RecordingCallback()

    class EmptySource(_FakeSource):
        def fetch_page(self, _envelope: Any, *, cursor: str | None) -> Any:
            from auris_flow_dagster.audio_import import ImportSourcePage

            assert cursor == "2026-07-20T00:00:00+00:00"
            return ImportSourcePage(records=(), next_cursor=None)

    store = _FakeStore()
    execute_audio_import_and_report(
        scope=replace(scope, event_type="task_run.requested"),
        dagster_run_id="dagster-import-empty",
        envelope=envelope,
        callback=callback,  # type: ignore[arg-type]
        source_client=EmptySource(_wav_body()),  # type: ignore[arg-type]
        object_store=store,  # type: ignore[arg-type]
    )

    result_ref = callback.calls[0]["result_ref"]
    assert callback.calls[0]["status"] == "success"
    assert result_ref["batch_status"] == "succeeded"
    assert result_ref["items"] == []
    assert callback.calls[0]["metrics"] == {
        "total": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "next_cursor_candidate": "2026-07-20T00:00:00+00:00",
    }
    assert json.loads(store.manifest_bodies[0])["items"] == []


def test_audio_import_stops_at_safe_batch_boundary_without_advancing_past_next_page(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    source = _FakeSource(_wav_body())

    _public, internal, metrics = execute_audio_import(
        envelope=envelope,
        source_client=source,
        object_store=_FakeStore(),
        max_records=2,
    )

    assert source.page_calls == ["2026-07-20T00:00:00+00:00"]
    assert metrics["total"] == 2
    assert metrics["next_cursor_candidate"] == "2026-07-20T10:01:01+00:00"
    assert internal["next_cursor_candidate"] == "2026-07-20T10:01:01+00:00"


def test_audio_import_rejects_cursor_watermark_regression_before_download(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)

    class RegressingSource(_FakeSource):
        def fetch_page(self, _envelope: Any, *, cursor: str | None) -> Any:
            from auris_flow_dagster.audio_import import ImportSourcePage, ImportSourceRecord

            assert cursor == "2026-07-20T00:00:00+00:00"
            return ImportSourcePage(
                records=(
                    ImportSourceRecord(
                        external_record_id="ext-regression",
                        audio_url="https://cdn.example.test/regression.wav",
                        source_metadata={"started_at": "2026-07-19T23:59:58+00:00"},
                        cursor_value="2026-07-19T23:59:59+00:00",
                    ),
                ),
                next_cursor=None,
            )

        def download_audio(self, _envelope: Any, _record: Any) -> Any:
            pytest.fail("regressed cursor record must not be downloaded")

    with pytest.raises(AudioImportFailure, match="cursor"):
        execute_audio_import(
            envelope=envelope,
            source_client=RegressingSource(_wav_body()),
            object_store=_FakeStore(),
        )


@pytest.mark.parametrize(
    ("record_specs", "next_cursor", "message"),
    [
        (
            [
                ("ext-001", "2026-07-20T10:00:01+00:00"),
                ("ext-002", "2026-07-20T10:00:01+00:00"),
            ],
            None,
            "cursor",
        ),
        (
            [("ext-001", "2026-07-20T10:00:01+00:00")],
            "2026-07-20T10:00:02+00:00",
            "cursor",
        ),
        (
            [],
            "2026-07-20T10:00:01+00:00",
            "cursor",
        ),
        (
            [
                ("ext-duplicate", "2026-07-20T10:00:01+00:00"),
                ("ext-duplicate", "2026-07-20T10:00:02+00:00"),
            ],
            None,
            "duplicate",
        ),
    ],
    ids=(
        "same-watermark",
        "next-cursor-mismatch",
        "empty-page-with-cursor",
        "duplicate-external-id",
    ),
)
def test_audio_import_rejects_ambiguous_persistent_cursor_pages_before_download(
    valid_context: dict[str, Any],
    record_specs: list[tuple[str, str]],
    next_cursor: str | None,
    message: str,
) -> None:
    envelope = _parsed_envelope(valid_context)

    class InvalidCursorSource(_FakeSource):
        def fetch_page(self, _envelope: Any, *, cursor: str | None) -> Any:
            from auris_flow_dagster.audio_import import ImportSourcePage, ImportSourceRecord

            assert cursor == "2026-07-20T00:00:00+00:00"
            return ImportSourcePage(
                records=tuple(
                    ImportSourceRecord(
                        external_record_id=external_record_id,
                        audio_url=f"https://cdn.example.test/{external_record_id}.wav",
                        source_metadata={"started_at": cursor_value},
                        cursor_value=cursor_value,
                    )
                    for external_record_id, cursor_value in record_specs
                ),
                next_cursor=next_cursor,
            )

        def download_audio(self, _envelope: Any, _record: Any) -> Any:
            pytest.fail("ambiguous cursor page must fail before download")

    with pytest.raises(AudioImportFailure, match=message):
        execute_audio_import(
            envelope=envelope,
            source_client=InvalidCursorSource(_wav_body()),
            object_store=_FakeStore(),
        )


def test_audio_import_uses_content_addressed_idempotent_audio_key(
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    wav = _wav_body(b"same-content")
    source = _FakeSource(wav)
    store = _FakeStore()
    page = source.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )
    downloaded = source.download_audio(envelope, page.records[0])

    receipt = store.persist_audio(
        envelope=envelope,
        record=page.records[0],
        audio=downloaded,
    )

    external_digest = hashlib.sha256(b"ext-001").hexdigest()[:32]
    assert external_digest in receipt.object_key or receipt.content_sha256 in receipt.object_key


class _StorageResponse:
    status = 200

    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "audio/wav",
        content_length: int = 0,
        content_sha256: str = "",
        version_id: str = "version-001",
        etag: str | None = '"etag-001"',
    ) -> None:
        self.status = status
        headers = Message()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(content_length)
        headers["x-amz-meta-content-sha256"] = content_sha256
        headers["x-amz-version-id"] = version_id
        if etag is not None:
            headers["ETag"] = etag
        self.headers = headers

    def __enter__(self) -> _StorageResponse:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _secret_file(path: Any, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)
    return str(path)


def test_s3_import_store_conditionally_streams_content_addressed_object_and_requires_version(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    wav = _wav_body(b"stream-to-versioned-minio")
    content_sha256 = hashlib.sha256(wav).hexdigest()
    source = _FakeSource(wav)
    page = source.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )
    audio = source.download_audio(envelope, page.records[0])
    requests: list[Request] = []
    uploaded = bytearray()

    def opener(request: Request, *, timeout: float) -> Any:
        assert timeout == 15
        requests.append(request)
        if request.method == "HEAD":
            return HTTPError(request.full_url, 404, "missing", Message(), None)
        assert request.method == "PUT"
        assert request.get_header("If-none-match") == "*"
        assert request.get_header("X-amz-meta-content-sha256") == content_sha256
        assert request.get_header("Content-length") == str(len(wav))
        assert request.data is not None
        for chunk in request.data:
            uploaded.extend(chunk)
        return _StorageResponse(
            version_id="exact-version-007",
            etag='"exact-etag-007"',
        )

    store = S3VersionedAudioImportStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret_file(tmp_path / "access", "unit-access"),
        secret_key_file=_secret_file(
            tmp_path / "secret",
            "unit-secret-value-with-at-least-32-characters",
        ),
        opener=opener,
        clock=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    receipt = store.persist_audio(
        envelope=envelope,
        record=page.records[0],
        audio=audio,
    )
    audio.close()

    external_digest = hashlib.sha256(b"ext-001").hexdigest()[:32]
    assert [request.method for request in requests] == ["HEAD", "PUT"]
    assert bytes(uploaded) == wav
    assert receipt.created is True
    assert receipt.version_id == "exact-version-007"
    assert receipt.etag == "exact-etag-007"
    assert receipt.object_key == (
        f"{envelope.target.object_prefix}recordings/{external_digest}/{content_sha256}.wav"
    )
    assert receipt.content_sha256 == content_sha256


def test_s3_import_store_stops_streaming_at_frozen_deadline(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = replace(
        _parsed_envelope(valid_context),
        deadline_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC),
    )
    wav = _wav_body(b"x" * (128 * 1024))
    now = [datetime(2026, 7, 20, 8, 0, tzinfo=UTC)]

    class DeadlineStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            now[0] = datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)
            return chunk

    audio = DownloadedAudio(
        stream=DeadlineStream(wav),
        content_length=len(wav),
        content_type="audio/wav",
        content_sha256=hashlib.sha256(wav).hexdigest(),
    )
    source = _FakeSource(wav)
    record = source.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    ).records[0]
    uploaded = bytearray()

    def opener(request: Request, *, timeout: float) -> Any:
        assert timeout > 0
        if request.method == "HEAD":
            return HTTPError(request.full_url, 404, "missing", Message(), None)
        assert request.data is not None
        for chunk in request.data:
            uploaded.extend(chunk)
        return _StorageResponse(version_id="must-not-be-returned")

    store = S3VersionedAudioImportStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret_file(tmp_path / "access", "unit-access"),
        secret_key_file=_secret_file(
            tmp_path / "secret",
            "unit-secret-value-with-at-least-32-characters",
        ),
        opener=opener,
        clock=lambda: now[0],
    )

    with pytest.raises(AudioImportFailure, match="deadline"):
        store.persist_audio(envelope=envelope, record=record, audio=audio)

    audio.close()
    assert uploaded == b""


def test_s3_import_store_reuses_exact_existing_version_without_put(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _parsed_envelope(valid_context)
    wav = _wav_body(b"idempotent")
    digest = hashlib.sha256(wav).hexdigest()
    source = _FakeSource(wav)
    page = source.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )
    audio = source.download_audio(envelope, page.records[0])
    requests: list[Request] = []

    def opener(request: Request, *, timeout: float) -> _StorageResponse:
        del timeout
        requests.append(request)
        assert request.method == "HEAD"
        return _StorageResponse(
            content_length=len(wav),
            content_sha256=digest,
            version_id="existing-version-009",
            etag='"existing-etag-009"',
        )

    store = S3VersionedAudioImportStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret_file(tmp_path / "access", "unit-access"),
        secret_key_file=_secret_file(
            tmp_path / "secret",
            "unit-secret-value-with-at-least-32-characters",
        ),
        opener=opener,
        clock=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    receipt = store.persist_audio(
        envelope=envelope,
        record=page.records[0],
        audio=audio,
    )
    audio.close()

    assert [request.method for request in requests] == ["HEAD"]
    assert receipt.created is False
    assert receipt.version_id == "existing-version-009"
    assert receipt.etag == "existing-etag-009"


@pytest.mark.parametrize("etag", [None, "", 'W/"weak-etag"'])
def test_s3_import_store_rejects_missing_or_weak_etag_on_exact_object_receipt(
    tmp_path: Any,
    valid_context: dict[str, Any],
    etag: str | None,
) -> None:
    envelope = _parsed_envelope(valid_context)
    wav = _wav_body(b"etag-contract")
    source = _FakeSource(wav)
    page = source.fetch_page(
        envelope,
        cursor=envelope.connector.cursor_policy.cursor_value,
    )
    audio = source.download_audio(envelope, page.records[0])

    def opener(request: Request, *, timeout: float) -> Any:
        del timeout
        if request.method == "HEAD":
            return HTTPError(request.full_url, 404, "missing", Message(), None)
        return _StorageResponse(version_id="exact-version-etag-contract", etag=etag)

    store = S3VersionedAudioImportStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret_file(tmp_path / "access", "unit-access"),
        secret_key_file=_secret_file(
            tmp_path / "secret",
            "unit-secret-value-with-at-least-32-characters",
        ),
        opener=opener,
        clock=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    with pytest.raises(AudioImportFailure, match="ETag"):
        store.persist_audio(
            envelope=envelope,
            record=page.records[0],
            audio=audio,
        )

    audio.close()


def test_audio_import_repeated_cursor_fails_closed_and_reports_sanitized_failure(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    context = _import_context(valid_context)
    envelope = validate_audio_import_envelope(
        _import_envelope(context),
        auris_context=context,
    )
    callback = _RecordingCallback()

    class RepeatingSource(_FakeSource):
        def fetch_page(self, envelope: Any, *, cursor: str | None) -> Any:
            from auris_flow_dagster.audio_import import ImportSourcePage

            del envelope, cursor
            return ImportSourcePage(records=(), next_cursor="same-cursor")

    with pytest.raises(Exception, match="audio import"):
        execute_audio_import_and_report(
            scope=replace(scope, event_type="task_run.requested"),
            dagster_run_id="dagster-import-repeated-cursor",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
            source_client=RepeatingSource(_wav_body()),  # type: ignore[arg-type]
            object_store=_FakeStore(),  # type: ignore[arg-type]
        )

    assert callback.calls[0]["status"] == "failed"
    assert callback.calls[0]["error_code"] == "AUDIO_IMPORT_CURSOR_PAGE_MISMATCH"
    assert "same-cursor" not in repr(callback.calls)
