from __future__ import annotations

import asyncio
from email.message import Message
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Message as ASGIMessage
from starlette.types import Scope

from app.api.routers import audio_sessions as audio_sessions_router
from app.core.config import get_settings
from app.core.errors import ApiError
from app.services import adapters as storage_adapters

PROVIDER_CASES = (
    ("minio", "http://minio.example.test:9000", "path", "s3v4"),
    ("s3", "https://s3.example.test", "virtual", "s3v4"),
    ("obs", "https://obs.example.test", "virtual", "obs"),
    ("oss", "https://oss.example.test", "virtual", "ossv4"),
)
PROVIDERS = tuple(case[0] for case in PROVIDER_CASES)


def test_production_audio_storage_never_falls_back_to_synthetic_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_sessions_router.settings, "app_env", "prod")
    monkeypatch.setattr(
        audio_sessions_router.settings,
        "auris_object_storage_adapter",
        "local",
    )

    with pytest.raises(ApiError) as exc_info:
        audio_sessions_router._real_object_storage_enabled()

    assert exc_info.value.code == "AUDIO_STORAGE_MODE_INVALID"
    assert exc_info.value.status_code == 503


def _factory_for_provider(provider: str) -> Any:
    factory = getattr(storage_adapters, "object_storage_client_for_provider", None)
    assert callable(factory), "object_storage_client_for_provider(provider) must be implemented"
    return factory(provider)


def _provider_env_names(provider: str, setting: str) -> set[str]:
    provider_name = provider.upper()
    aliases = {
        "access_key": ("ACCESS_KEY", "ACCESS_KEY_ID"),
        "secret_key": ("SECRET_KEY", "SECRET_ACCESS_KEY", "ACCESS_KEY_SECRET"),
        "endpoint": ("ENDPOINT",),
        "bucket": ("BUCKET",),
        "region": ("REGION",),
        "addressing_style": ("ADDRESSING_STYLE",),
        "signature_mode": ("SIGNATURE_MODE",),
    }[setting]
    names = {
        name
        for alias in aliases
        for name in (f"OBJECT_STORAGE_{provider_name}_{alias}", f"{provider_name}_{alias}")
    }
    if provider == "minio":
        if setting == "access_key":
            names.add("MINIO_ROOT_USER")
        elif setting == "secret_key":
            names.add("MINIO_ROOT_PASSWORD")
    if provider == "s3":
        aws_names = {
            "access_key": {"AWS_ACCESS_KEY_ID"},
            "secret_key": {"AWS_SECRET_ACCESS_KEY"},
            "endpoint": {"AWS_ENDPOINT_URL_S3"},
            "region": {"AWS_REGION", "AWS_DEFAULT_REGION"},
        }
        names.update(aws_names.get(setting, set()))
    return names


def _set_provider_config(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    *,
    endpoint: str,
    access_key: str,
    secret_key: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    values = {
        "endpoint": endpoint,
        "bucket": f"{provider}-audio",
        "access_key": access_key,
        "secret_key": secret_key,
        "region": f"{provider}-region-1",
        "addressing_style": addressing_style,
        "signature_mode": signature_mode,
    }
    for setting, value in values.items():
        for name in _provider_env_names(provider, setting):
            monkeypatch.setenv(name, value)


def _clear_provider_credentials(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    for setting in ("access_key", "secret_key"):
        for name in _provider_env_names(provider, setting):
            monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider,endpoint,addressing_style,signature_mode", PROVIDER_CASES)
def test_provider_factory_uses_selected_provider_configuration_only(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    for (
        configured_provider,
        configured_endpoint,
        configured_style,
        configured_signature,
    ) in PROVIDER_CASES:
        _set_provider_config(
            monkeypatch,
            configured_provider,
            endpoint=configured_endpoint,
            access_key=f"{configured_provider}-access-only",
            secret_key=f"{configured_provider}-secret-only",
            addressing_style=configured_style,
            signature_mode=configured_signature,
        )
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", "https://generic-must-not-be-used.test")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "generic-access-must-not-leak")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "generic-secret-must-not-leak")

    client = _factory_for_provider(provider)

    assert client.provider == provider
    assert client.endpoint == endpoint
    assert client.bucket == f"{provider}-audio"
    assert client.access_key == f"{provider}-access-only"
    assert client.secret_key == f"{provider}-secret-only"
    assert client.region == f"{provider}-region-1"
    assert client.addressing_style == addressing_style
    assert client.signature_mode == signature_mode


def test_oss_global_configuration_uses_native_provider_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_settings = get_settings()
    monkeypatch.setattr(runtime_settings, "object_storage_provider", "oss")
    monkeypatch.setattr(
        runtime_settings,
        "object_storage_endpoint",
        "https://oss-cn-hangzhou.aliyuncs.com",
    )
    monkeypatch.setattr(runtime_settings, "object_storage_bucket", "oss-audio")
    monkeypatch.setattr(runtime_settings, "object_storage_access_key", "oss-global-access")
    monkeypatch.setattr(runtime_settings, "object_storage_secret_key", "oss-global-secret")
    monkeypatch.setattr(runtime_settings, "object_storage_region", "cn-hangzhou")
    monkeypatch.setattr(runtime_settings, "object_storage_addressing_style", "")
    monkeypatch.setattr(runtime_settings, "object_storage_signature_mode", "")
    monkeypatch.setenv("OBJECT_STORAGE_PROVIDER", "oss")
    monkeypatch.delenv("OBJECT_STORAGE_ADDRESSING_STYLE", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_SIGNATURE_MODE", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_OSS_ADDRESSING_STYLE", raising=False)
    monkeypatch.delenv("OBJECT_STORAGE_OSS_SIGNATURE_MODE", raising=False)

    client = _factory_for_provider("oss")

    assert client.addressing_style == "virtual"
    assert client.signature_mode == "ossv4"


@pytest.mark.parametrize("provider,endpoint,addressing_style,signature_mode", PROVIDER_CASES)
def test_provider_factory_never_falls_back_to_another_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    foreign_access_keys: set[str] = set()
    foreign_secret_keys: set[str] = set()
    for (
        configured_provider,
        configured_endpoint,
        configured_style,
        configured_signature,
    ) in PROVIDER_CASES:
        access_key = f"{configured_provider}-foreign-access"
        secret_key = f"{configured_provider}-foreign-secret"
        _set_provider_config(
            monkeypatch,
            configured_provider,
            endpoint=configured_endpoint,
            access_key=access_key,
            secret_key=secret_key,
            addressing_style=configured_style,
            signature_mode=configured_signature,
        )
        if configured_provider != provider:
            foreign_access_keys.add(access_key)
            foreign_secret_keys.add(secret_key)

    _clear_provider_credentials(monkeypatch, provider)
    foreign_default = next(candidate for candidate in PROVIDERS if candidate != provider)
    monkeypatch.setenv("OBJECT_STORAGE_PROVIDER", foreign_default)
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "generic-foreign-access")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "generic-foreign-secret")
    foreign_access_keys.add("generic-foreign-access")
    foreign_secret_keys.add("generic-foreign-secret")

    with pytest.raises(ValueError) as exc_info:
        _factory_for_provider(provider)

    error_message = str(exc_info.value)
    assert provider in error_message
    assert "access_key" in error_message
    assert "secret_key" in error_message
    assert all(credential not in error_message for credential in foreign_access_keys)
    assert all(credential not in error_message for credential in foreign_secret_keys)


class _PartialObjectResponse:
    status = 206

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.read_count = 0
        self.closed = False
        self.headers = Message()
        self.headers["Accept-Ranges"] = "bytes"
        self.headers["Content-Range"] = "bytes 87-99/100"
        self.headers["Content-Length"] = str(len(body))
        self.headers["Content-Type"] = "audio/wav"
        self.headers["ETag"] = '"range-etag"'

    def __enter__(self) -> _PartialObjectResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        self.read_count += 1
        return self._body

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("provider,endpoint,addressing_style,signature_mode", PROVIDER_CASES)
def test_real_client_builds_provider_url_signs_range_and_preserves_206(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    captured: list[Request] = []
    body = b"partial-audio"

    def fake_urlopen(request: Request, timeout: int) -> _PartialObjectResponse:
        assert timeout == 5
        captured.append(request)
        return _PartialObjectResponse(body)

    monkeypatch.setattr(storage_adapters, "urlopen", fake_urlopen)
    monkeypatch.setenv("AURIS_FIXED_AWS_SIGV4_TIME", "20260710T010203Z")
    client = storage_adapters.RealObjectStorageClient(
        provider=provider,
        endpoint=endpoint,
        bucket="audio-bucket",
        access_key=f"{provider}-access",
        secret_key=f"{provider}-secret",
        region="test-region-1",
        addressing_style=addressing_style,
        signature_mode=signature_mode,
    )

    result = client.get_object(
        "audio-bucket",
        "tenants/tenant-a/call sample.wav",
        byte_range="bytes=-13",
    )

    request = captured[0]
    if addressing_style == "path":
        expected_url = f"{endpoint}/audio-bucket/tenants/tenant-a/call%20sample.wav"
        expected_host = endpoint.split("//", 1)[1]
    else:
        scheme, host = endpoint.split("//", 1)
        expected_url = f"{scheme}//audio-bucket.{host}/tenants/tenant-a/call%20sample.wav"
        expected_host = f"audio-bucket.{host}"
    assert request.full_url == expected_url
    assert request.get_header("Host") == expected_host
    assert request.get_header("Range") == "bytes=-13"
    authorization = request.get_header("Authorization")
    assert authorization is not None
    if signature_mode == "obs":
        assert authorization.startswith(f"OBS {provider}-access:")
    elif signature_mode == "ossv4":
        assert authorization.startswith(
            f"OSS4-HMAC-SHA256 Credential={provider}-access/20260710/"
            "test-region-1/oss/aliyun_v4_request"
        )
        assert "AdditionalHeaders=range" in authorization
        assert request.get_header("X-oss-content-sha256") == "UNSIGNED-PAYLOAD"
        assert request.get_header("X-oss-date") == "20260710T010203Z"
        assert request.get_header("X-oss-s3-compat") is None
    else:
        assert authorization.startswith("AWS4-HMAC-SHA256 ")
        signed_headers = authorization.partition("SignedHeaders=")[2].partition(",")[0]
        assert "range" in signed_headers.split(";")
    assert client.provider == provider
    assert client.signature_mode == signature_mode
    assert result["status"] == 206
    assert result["content_range"] == "bytes 87-99/100"
    assert result["content_length"] == str(len(body))
    assert result["body"] == body


@pytest.mark.parametrize("provider,endpoint,addressing_style,signature_mode", PROVIDER_CASES)
def test_real_client_open_object_defers_audio_body_read(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    response = _PartialObjectResponse(b"partial-audio")
    monkeypatch.setattr(storage_adapters, "urlopen", lambda *_args, **_kwargs: response)
    client = storage_adapters.RealObjectStorageClient(
        provider=provider,
        endpoint=endpoint,
        bucket="audio-bucket",
        access_key=f"{provider}-access",
        secret_key=f"{provider}-secret",
        region="test-region-1",
        addressing_style=addressing_style,
        signature_mode=signature_mode,
    )

    result = client.open_object(
        "audio-bucket",
        "tenants/tenant-a/recording.wav",
        byte_range="bytes=0-12",
    )

    assert result["status"] == 206
    assert result["stream"] is response
    assert response.read_count == 0
    response.close()
    assert response.closed is True


def test_bff_closes_upstream_object_stream_when_consumer_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CancellableStream:
        def __init__(self) -> None:
            self.remaining = 100_000
            self.closed = False

        def read(self, size: int) -> bytes:
            count = min(size, self.remaining)
            self.remaining -= count
            return b"a" * count

        def close(self) -> None:
            self.closed = True

    stream = CancellableStream()

    class StreamingProviderClient:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def open_object(
            self,
            bucket: str,
            object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            assert bucket == "audio-bucket"
            assert object_key == "tenants/tenant-a/recording.wav"
            assert byte_range == "bytes=0-99999"
            assert if_match == '"stream-etag"'
            return {
                "status": 206,
                "etag": "stream-etag",
                "content_length": "100000",
                "content_range": "bytes 0-99999/100000",
                "content_type": "audio/wav",
                "stream": stream,
            }

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: StreamingProviderClient(),
    )
    recording = {
        "recording_id": "recording-stream",
        "file_name": "recording.wav",
        "storage_object": {
            "storage_object_id": "sto_stream",
            "provider": "minio",
            "bucket": "audio-bucket",
            "object_key": "tenants/tenant-a/recording.wav",
            "content_type": "audio/wav",
            "content_length": 100_000,
            "etag": "stream-etag",
            "status": "registered",
        },
    }
    response = audio_sessions_router._object_storage_audio_response(
        recording,
        range_header="bytes=0-99999",
    )
    assert isinstance(response, StreamingResponse)

    async def disconnect_during_first_chunk() -> None:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/audio-playback",
            "raw_path": b"/api/v1/audio-playback",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
        }

        async def receive() -> ASGIMessage:
            return {"type": "http.disconnect"}

        async def send(message: ASGIMessage) -> None:
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

    asyncio.run(disconnect_during_first_chunk())
    assert stream.closed is True


def test_bff_fails_closed_when_upstream_audio_stream_ends_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TruncatedStream:
        def __init__(self) -> None:
            self.read_count = 0
            self.closed = False

        def read(self, _size: int) -> bytes:
            self.read_count += 1
            return b"short" if self.read_count == 1 else b""

        def close(self) -> None:
            self.closed = True

    stream = TruncatedStream()

    class TruncatedProviderClient:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def open_object(
            self,
            bucket: str,
            object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            assert bucket == "audio-bucket"
            assert object_key == "tenants/tenant-a/truncated.wav"
            assert byte_range == "bytes=0-12"
            assert if_match == '"truncated-etag"'
            return {
                "status": 206,
                "etag": "truncated-etag",
                "content_length": "13",
                "content_range": "bytes 0-12/13",
                "content_type": "audio/wav",
                "stream": stream,
            }

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: TruncatedProviderClient(),
    )
    recording = {
        "recording_id": "recording-truncated",
        "file_name": "truncated.wav",
        "storage_object": {
            "storage_object_id": "sto_truncated",
            "provider": "minio",
            "bucket": "audio-bucket",
            "object_key": "tenants/tenant-a/truncated.wav",
            "content_type": "audio/wav",
            "content_length": 13,
            "etag": "truncated-etag",
            "status": "registered",
        },
    }
    response = audio_sessions_router._object_storage_audio_response(
        recording,
        range_header="bytes=0-12",
    )
    assert isinstance(response, StreamingResponse)

    async def consume_body() -> list[ASGIMessage]:
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/audio-playback",
            "raw_path": b"/api/v1/audio-playback",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 50000),
        }
        messages: list[ASGIMessage] = []

        async def receive() -> ASGIMessage:
            return {"type": "http.disconnect"}

        async def send(message: ASGIMessage) -> None:
            messages.append(message)

        with pytest.raises(
            audio_sessions_router._AudioObjectStreamTruncatedError,
            match="Content-Length",
        ):
            await response(scope, receive, send)
        return messages

    messages = asyncio.run(consume_body())
    assert [message.get("body") for message in messages if "body" in message] == [b"short"]
    assert stream.closed is True


@pytest.mark.parametrize("provider", ("minio", "obs", "oss"))
def test_audio_range_preserves_provider_416_content_range(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    class UnsatisfiableProviderClient:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def open_object(
            self,
            bucket: str,
            object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            assert bucket == "audio-bucket"
            assert object_key == "tenants/tenant-a/recording.wav"
            assert byte_range == "bytes=90-99"
            assert if_match == '"registered-etag"'
            headers = Message()
            headers["Content-Range"] = "bytes */80"
            raise HTTPError(
                url=f"https://{provider}.example.test/{object_key}",
                code=416,
                msg="Range Not Satisfiable",
                hdrs=headers,
                fp=None,
            )

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: UnsatisfiableProviderClient(),
    )
    recording = {
        "recording_id": "recording-provider-416",
        "file_name": "recording.wav",
        "storage_object": {
            "storage_object_id": f"sto_{provider}_416",
            "provider": provider,
            "bucket": "audio-bucket",
            "object_key": "tenants/tenant-a/recording.wav",
            "content_type": "audio/wav",
            "content_length": 100,
            "etag": "registered-etag",
            "status": "registered",
        },
    }

    response = audio_sessions_router._object_storage_audio_response(
        recording,
        range_header="bytes=90-99",
    )

    assert response.status_code == 416
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Range"] == "bytes */80"
    assert response.headers["X-Storage-Provider"] == provider


def test_oss_uses_native_temporary_credential_header_and_rejects_s3_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURIS_FIXED_OBJECT_STORAGE_TIME", "20260710T010203Z")
    client = storage_adapters.RealObjectStorageClient(
        provider="oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="audio-bucket",
        access_key="oss-access",
        secret_key="oss-secret",
        session_token="oss-session-token",
        region="cn-hangzhou",
        addressing_style="virtual",
        signature_mode="ossv4",
    )

    request = client._signed_request(
        "GET",
        "/audio-bucket/recording.wav",
        extra_headers={"Range": "bytes=0-31"},
    )

    assert request.get_header("X-oss-security-token") == "oss-session-token"
    assert request.get_header("X-amz-security-token") is None
    assert request.get_header("Authorization") == (
        "OSS4-HMAC-SHA256 "
        "Credential=oss-access/20260710/cn-hangzhou/oss/aliyun_v4_request,"
        "AdditionalHeaders=range,"
        "Signature=76aa23caaec3d3d35f25f398c0668b13cba0862a900bf35569afbee453f91642"
    )
    with pytest.raises(ValueError, match="unsupported signature mode"):
        storage_adapters.RealObjectStorageClient(
            provider="oss",
            endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            bucket="audio-bucket",
            access_key="oss-access",
            secret_key="oss-secret",
            region="cn-hangzhou",
            addressing_style="virtual",
            signature_mode="s3v4",
        )


def test_oss_bucket_request_keeps_canonical_resource_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURIS_FIXED_OBJECT_STORAGE_TIME", "20260710T010203Z")
    client = storage_adapters.RealObjectStorageClient(
        provider="oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="audio-bucket",
        access_key="oss-access",
        secret_key="oss-secret",
        region="cn-hangzhou",
        addressing_style="virtual",
        signature_mode="ossv4",
    )

    url, host, canonical_uri, canonical_resource = client._request_target(
        storage_adapters.urlparse(client.endpoint),
        "/audio-bucket",
    )
    request = client._signed_request("HEAD", "/audio-bucket")

    assert url == "https://audio-bucket.oss-cn-hangzhou.aliyuncs.com/"
    assert host == "audio-bucket.oss-cn-hangzhou.aliyuncs.com"
    assert canonical_uri == "/"
    assert canonical_resource == "/audio-bucket/"
    assert request.full_url == url
    authorization = request.get_header("Authorization")
    assert authorization is not None
    assert authorization.startswith("OSS4-HMAC-SHA256 ")


def test_head_bucket_uses_the_readiness_callers_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str, float]] = []

    class Response:
        status = 200
        headers = Message()

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b""

    def fake_urlopen(request: Request, timeout: float) -> Response:
        observed.append((request.method, request.full_url, timeout))
        return Response()

    monkeypatch.setattr(storage_adapters, "urlopen", fake_urlopen)
    client = storage_adapters.RealObjectStorageClient(
        provider="minio",
        endpoint="http://minio.example.test:9000",
        bucket="audio-bucket",
        access_key="minio-access",
        secret_key="minio-secret",
        region="us-east-1",
        addressing_style="path",
        signature_mode="s3v4",
    )

    result = client.head_bucket("audio-bucket", timeout_seconds=0.25)

    assert result["status"] == 200
    assert observed == [("HEAD", "http://minio.example.test:9000/audio-bucket/", 0.25)]


@pytest.mark.parametrize("provider", ("s3", "obs", "oss"))
def test_cloud_providers_never_create_or_probe_buckets(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    signature_mode = "obs" if provider == "obs" else "ossv4" if provider == "oss" else "s3v4"
    client = storage_adapters.RealObjectStorageClient(
        provider=provider,
        endpoint=f"https://{provider}.example.test",
        bucket="audio-bucket",
        access_key=f"{provider}-access",
        secret_key=f"{provider}-secret",
        region="test-region-1",
        addressing_style="virtual",
        signature_mode=signature_mode,
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: calls.append((*args, kwargs)))

    client._ensure_bucket()

    assert calls == []


def test_oss_v4_signer_matches_official_algorithm_with_literal_test_secret() -> None:
    client = storage_adapters.RealObjectStorageClient(
        provider="oss",
        endpoint="https://oss-cn-hangzhou.aliyuncs.com",
        bucket="examplebucket",
        access_key="LTAI****************",
        secret_key="yourAccessKeySecret",
        region="cn-hangzhou",
        addressing_style="virtual",
        signature_mode="ossv4",
    )
    headers = {
        "Host": "examplebucket.oss-cn-hangzhou.aliyuncs.com",
        "Content-Disposition": "attachment",
        "Content-Length": "3",
        "Content-MD5": "ICy5YqxZB1uWSwcVLSNLcA==",
        "Content-Type": "text/plain",
        "x-oss-content-sha256": "UNSIGNED-PAYLOAD",
        "x-oss-date": "20250411T064124Z",
    }

    authorization = client._oss_authorization_header(
        method="PUT",
        canonical_resource="/examplebucket/exampleobject",
        headers=headers,
        date_stamp="20250411",
        timestamp="20250411T064124Z",
        additional_headers=("content-disposition", "content-length"),
    )

    assert authorization == (
        "OSS4-HMAC-SHA256 "
        "Credential=LTAI****************/20250411/cn-hangzhou/oss/aliyun_v4_request,"
        "AdditionalHeaders=content-disposition;content-length,"
        "Signature=d3694c2dfc5371ee6acd35e88c4871ac95a7ba01d3a2f476768fe61218590097"
    )


class _ProviderRangeClient:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == f"{self.provider}-audio"

    def head_object(self, _bucket: str, _object_key: str) -> dict[str, Any]:
        raise AssertionError("registered content_length should avoid a HEAD request")

    def get_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((bucket, object_key, byte_range, if_match))
        body = b"partial-audio"
        return {
            "status": 206,
            "headers": {
                "Accept-Ranges": "bytes",
                "Content-Range": "bytes 87-99/100",
                "Content-Length": str(len(body)),
                "Content-Type": "audio/wav",
            },
            "content_length": str(len(body)),
            "content_range": "bytes 87-99/100",
            "content_type": "audio/wav",
            "etag": f"{self.provider}-range-etag",
            "body": body,
        }


class _VersionedRegistrationClient:
    def __init__(self) -> None:
        self.etag = "provider-etag-v1"

    def allows_bucket(self, bucket: str) -> bool:
        return bucket == "minio-audio"

    def head_object(self, _bucket: str, _object_key: str) -> dict[str, Any]:
        return {"content_length": "100", "etag": self.etag}

    def open_object(
        self,
        _bucket: str,
        _object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
    ) -> dict[str, Any]:
        assert byte_range == "bytes=0-12"
        assert if_match == '"provider-etag-v1"'
        body = b"x" * 13
        return {
            "status": 206,
            "etag": self.etag,
            "content_length": str(len(body)),
            "content_range": "bytes 0-12/100",
            "content_type": "audio/wav",
            "stream": BytesIO(body),
        }


def test_registration_persists_provider_etag_and_rejects_replaced_same_size_object(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_client = _VersionedRegistrationClient()
    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: provider_client,
    )
    monkeypatch.setattr(
        audio_sessions_router.settings,
        "auris_object_storage_adapter",
        "real",
    )
    registration = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json={
            "storage_object_id": "sto_provider_etag_binding",
            "provider": "minio",
            "bucket": "minio-audio",
            "object_key": (
                "tenants/aurora_auto/projects/sales_qa/audio/raw/provider-etag-binding.wav"
            ),
            "content_type": "audio/wav",
            "content_length": 100,
            "checksum_sha256": "d" * 64,
        },
        headers={**auth_headers, "Idempotency-Key": "register-provider-etag-binding"},
    )
    assert registration.status_code == 200, registration.text
    assert registration.json()["data"]["storage_object"]["etag"] == "provider-etag-v1"

    grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={**auth_headers, "Idempotency-Key": "grant-provider-etag-binding"},
    )
    assert grant.status_code == 201, grant.text

    provider_client.etag = "provider-etag-v2"
    stale = client.get(
        grant.json()["data"]["playback_url"],
        headers={"Range": "bytes=0-12"},
    )
    assert stale.status_code == 412, stale.text
    assert stale.json()["error"]["code"] == "AUDIO_OBJECT_VERSION_CHANGED"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_audio_range_route_uses_registered_provider_and_preserves_upstream_partial_response(
    client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    object_key = (
        f"tenants/aurora_auto/projects/sales_qa/audio/raw/{provider}/recording-provider-range.wav"
    )
    registration = client.put(
        "/api/v1/audio-sessions/S20250526-000128/recording-object",
        json={
            "storage_object_id": f"sto_provider_range_{provider}",
            "provider": provider,
            "bucket": f"{provider}-audio",
            "object_key": object_key,
            "content_type": "audio/wav",
            "content_length": 100,
            "checksum_sha256": "a" * 64,
            "etag": f"{provider}-range-etag",
        },
        headers={
            **auth_headers,
            "Idempotency-Key": f"register-provider-range-{provider}",
        },
    )
    assert registration.status_code == 200

    grant = client.post(
        "/api/v1/audio-sessions/S20250526-000128/playback-grants",
        headers={
            **auth_headers,
            "Idempotency-Key": f"grant-provider-range-{provider}",
        },
    )
    assert grant.status_code == 201

    provider_client = _ProviderRangeClient(provider)
    selected_providers: list[str] = []

    def client_for_provider(selected_provider: str) -> _ProviderRangeClient:
        selected_providers.append(selected_provider)
        return provider_client

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        client_for_provider,
        raising=False,
    )
    monkeypatch.setattr(
        audio_sessions_router.settings,
        "auris_object_storage_adapter",
        "real",
    )

    response = client.get(
        grant.json()["data"]["playback_url"],
        headers={"Range": "bytes=-13"},
    )

    assert selected_providers == [provider]
    assert response.status_code == 206
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Content-Range"] == "bytes 87-99/100"
    assert response.headers["Content-Length"] == str(len(b"partial-audio"))
    assert response.headers["X-Storage-Provider"] == provider
    assert response.content == b"partial-audio"
    assert provider_client.calls == [
        (f"{provider}-audio", object_key, "bytes=-13", f'"{provider}-range-etag"')
    ]

    invalid = client.get(
        grant.json()["data"]["playback_url"],
        headers={"Range": "bytes=100-120"},
    )
    assert invalid.status_code == 416
    assert invalid.headers["Accept-Ranges"] == "bytes"
    assert invalid.headers["Content-Range"] == "bytes */100"
    assert provider_client.calls == [
        (f"{provider}-audio", object_key, "bytes=-13", f'"{provider}-range-etag"')
    ]


def _registered_range_recording(provider: str, etag: str) -> dict[str, object]:
    return {
        "recording_id": f"recording-{provider}-integrity",
        "file_name": "recording.wav",
        "storage_object": {
            "storage_object_id": f"sto_{provider}_integrity",
            "provider": provider,
            "bucket": "audio-bucket",
            "object_key": "tenants/tenant-a/projects/project-a/audio/recording.wav",
            "content_type": "audio/wav",
            "content_length": 100,
            "etag": etag,
            "status": "verified",
        },
    }


@pytest.mark.parametrize("provider", PROVIDERS)
def test_audio_range_sends_if_match_and_accepts_exact_quoted_multipart_etag(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    calls: list[tuple[str | None, str | None]] = []

    class ExactEtagProvider:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def get_object(
            self,
            _bucket: str,
            _object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            calls.append((byte_range, if_match))
            return {
                "status": 206,
                "etag": '"AbC123-9"',
                "content_length": "13",
                "content_range": "bytes 87-99/100",
                "content_type": "audio/wav",
                "body": b"partial-audio",
            }

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: ExactEtagProvider(),
    )
    response = audio_sessions_router._object_storage_audio_response(
        _registered_range_recording(provider, '"AbC123-9"'),
        range_header="bytes=87-99",
    )

    assert response.status_code == 206
    assert response.headers["ETag"] == '"AbC123-9"'
    assert calls == [("bytes=87-99", '"AbC123-9"')]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("upstream_etag", "expected_code"),
    [
        (None, "AUDIO_OBJECT_ETAG_MISSING"),
        ("abc123-9", "AUDIO_OBJECT_VERSION_CHANGED"),
    ],
)
def test_audio_range_fails_closed_when_upstream_etag_is_missing_or_case_changed(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    upstream_etag: str | None,
    expected_code: str,
) -> None:
    class InvalidEtagProvider:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def get_object(
            self,
            _bucket: str,
            _object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            assert byte_range == "bytes=87-99"
            assert if_match == '"AbC123-9"'
            return {
                "status": 206,
                "etag": upstream_etag,
                "content_length": "13",
                "content_range": "bytes 87-99/100",
                "content_type": "audio/wav",
                "body": b"partial-audio",
            }

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: InvalidEtagProvider(),
    )
    with pytest.raises(ApiError) as exc_info:
        audio_sessions_router._object_storage_audio_response(
            _registered_range_recording(provider, '"AbC123-9"'),
            range_header="bytes=87-99",
        )

    assert exc_info.value.code == expected_code
    assert exc_info.value.status_code in {412, 502}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_audio_range_maps_provider_if_match_412_to_version_changed(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    class PreconditionFailedProvider:
        def allows_bucket(self, bucket: str) -> bool:
            return bucket == "audio-bucket"

        def get_object(
            self,
            _bucket: str,
            object_key: str,
            *,
            byte_range: str | None = None,
            if_match: str | None = None,
        ) -> dict[str, object]:
            assert byte_range == "bytes=87-99"
            assert if_match == '"AbC123-9"'
            raise HTTPError(
                url=f"https://{provider}.example.test/{object_key}",
                code=412,
                msg="Precondition Failed",
                hdrs=Message(),
                fp=None,
            )

    monkeypatch.setattr(
        audio_sessions_router,
        "object_storage_client_for_provider",
        lambda _provider: PreconditionFailedProvider(),
    )
    with pytest.raises(ApiError) as exc_info:
        audio_sessions_router._object_storage_audio_response(
            _registered_range_recording(provider, "AbC123-9"),
            range_header="bytes=87-99",
        )

    assert exc_info.value.code == "AUDIO_OBJECT_VERSION_CHANGED"
    assert exc_info.value.status_code == 412
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("provider,endpoint,addressing_style,signature_mode", PROVIDER_CASES)
def test_real_provider_request_carries_signed_if_match_condition(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    addressing_style: str,
    signature_mode: str,
) -> None:
    captured: list[Request] = []

    def fake_urlopen(request: Request, timeout: int) -> _PartialObjectResponse:
        assert timeout == 5
        captured.append(request)
        return _PartialObjectResponse(b"partial-audio")

    monkeypatch.setattr(audio_sessions_router, "urlopen", fake_urlopen, raising=False)
    client = storage_adapters.RealObjectStorageClient(
        provider=provider,
        endpoint=endpoint,
        bucket="audio-bucket",
        access_key=f"{provider}-access",
        secret_key=f"{provider}-secret",
        region="test-region-1",
        addressing_style=addressing_style,
        signature_mode=signature_mode,
    )

    result = audio_sessions_router._open_object_with_if_match(
        client,
        "audio-bucket",
        "tenants/tenant-a/recording.wav",
        byte_range="bytes=87-99",
        registered_etag="range-etag",
    )

    assert captured[0].get_header("Range") == "bytes=87-99"
    assert captured[0].get_header("If-match") == '"range-etag"'
    assert result["etag"] == "range-etag"
    result["stream"].close()
