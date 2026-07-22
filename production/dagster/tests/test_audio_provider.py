from __future__ import annotations

import hashlib
import json
import ssl
from copy import deepcopy
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

import auris_flow_dagster.audio_provider as audio_provider_module
from auris_flow_dagster.audio_provider import (
    AUDIO_PROVIDER_REQUEST_SCHEMA,
    AUDIO_PROVIDER_RESPONSE_SCHEMA,
    AUDIO_RESULT_MANIFEST_SCHEMA,
    AudioProviderContractError,
    AudioProviderFailure,
    HTTPSAudioInferenceProvider,
    S3AudioResultManifestStore,
    build_audio_provider_request,
    canonical_sha256,
    validate_audio_provider_response,
)
from auris_flow_dagster.contracts import (
    AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    AudioExecutionEnvelope,
    validate_audio_execution_envelope,
)
from auris_flow_dagster.runtime import configured_audio_runtime_dependencies


def _envelope(valid_context: dict[str, Any]) -> AudioExecutionEnvelope:
    scope = {**valid_context, "event_type": "audio_intelligence.requested"}
    body = b"RIFF" + b"\x00" * 60
    raw = {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        "tenant_id": scope["tenant_id"],
        "project_id": scope["project_id"],
        "trace_id": scope["trace_id"],
        "run_id": scope["run_id"],
        "dispatch_idempotency_key": scope["dispatch_idempotency_key"],
        "outbox_fencing_token": scope["outbox_fencing_token"],
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "audio_session_id": "audio_session_001",
        "recording_id": "recording_001",
        "input_object": {
            "storage_object_id": "sto_audio_001",
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_key": "tenants/aurora_auto/projects/sales_qa/audio/recording.wav",
            "version_id": "immutable-version-3",
            "content_sha256": hashlib.sha256(body).hexdigest(),
            "content_length": len(body),
            "content_type": "audio/wav",
        },
        "inference": {
            "provider": "acme_speech",
            "model": "audio-v2.3.1",
        },
        "capabilities": ["vad", "asr"],
    }
    return validate_audio_execution_envelope(raw, auris_context=scope)


def _result() -> dict[str, Any]:
    return {
        "transcript": {
            "language": "zh-CN",
            "text": "hello world",
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 640,
                    "speaker": "speaker-1",
                    "text": "hello world",
                    "confidence": 0.97,
                }
            ],
        },
        "analyses": [
            {
                "capability": "vad",
                "summary": "speech present",
                "score": 0.98,
                "labels": [{"label": "speech", "score": 0.98}],
            }
        ],
    }


def _response_payload(envelope: AudioExecutionEnvelope) -> dict[str, Any]:
    request = build_audio_provider_request(envelope)
    result = _result()
    return {
        **{key: value for key, value in request.items() if key != "schema_version"},
        "schema_version": AUDIO_PROVIDER_RESPONSE_SCHEMA,
        "request_sha256": canonical_sha256(request),
        "result": result,
        "result_sha256": canonical_sha256(result),
    }


class _Response:
    status = 200

    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        version_id: str | None = None,
    ) -> None:
        self._body = body
        headers = Message()
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = content_type
        if version_id is not None:
            headers["x-amz-version-id"] = version_id
        self.headers = headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result, self._body = self._body, b""
            return result
        result, self._body = self._body[:size], self._body[size:]
        return result


def _secret(
    tmp_path: Any,
    value: str = "unit-only-provider-token-value-00000001",
    *,
    name: str = "provider-token",
) -> str:
    path = tmp_path / name
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)
    return str(path)


def test_https_provider_sends_closed_bound_idempotent_request_and_validates_tls(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _envelope(valid_context)
    requests: list[Request] = []
    contexts: list[ssl.SSLContext] = []

    def opener(
        request: Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> _Response:
        assert timeout == 7
        requests.append(request)
        contexts.append(context)
        payload = _response_payload(envelope)
        return _Response(json.dumps(payload).encode("utf-8"))

    provider = HTTPSAudioInferenceProvider(
        provider="acme_speech",
        allowed_models="audio-v2.3.1",
        endpoint="https://inference.vendor.test/v1/audio-intelligence",
        token_file=_secret(tmp_path),
        timeout_seconds=7,
        opener=opener,
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        environment="ci",
    )

    first = provider.infer(envelope)
    second = provider.infer(envelope)

    assert first.request_sha256 == second.request_sha256
    assert first.result_sha256 == canonical_sha256(_result())
    assert requests[0].data == requests[1].data
    assert requests[0].get_header("Idempotency-key") == (
        "audio-inference:" + envelope.dispatch_idempotency_key
    )
    assert requests[0].get_header("Authorization") == (
        "Bearer unit-only-provider-token-value-00000001"
    )
    request_body = json.loads((requests[0].data or b"").decode("utf-8"))
    assert request_body["schema_version"] == AUDIO_PROVIDER_REQUEST_SCHEMA
    assert request_body["input_object"]["version_id"] == "immutable-version-3"
    assert request_body["input_object"]["content_sha256"] == (envelope.input_object.content_sha256)
    assert request_body["deadline_at"] == envelope.deadline_at.isoformat()
    assert contexts[0].verify_mode == ssl.CERT_REQUIRED
    assert contexts[0].check_hostname is True


def test_provider_request_contains_only_immutable_object_identity_not_access_credentials(
    valid_context: dict[str, Any],
) -> None:
    request = build_audio_provider_request(_envelope(valid_context))

    assert set(request["input_object"]) == {
        "storage_object_id",
        "storage_provider",
        "bucket",
        "object_key",
        "version_id",
        "content_sha256",
        "content_length",
        "content_type",
    }
    forbidden_keys = {
        "access_key",
        "authorization",
        "bearer_token",
        "credential",
        "endpoint",
        "password",
        "presigned_url",
        "secret",
        "secret_key",
        "url",
    }

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            assert not (set(value) & forbidden_keys)
            for nested in value.values():
                assert_closed(nested)
        elif isinstance(value, list):
            for nested in value:
                assert_closed(nested)
        elif isinstance(value, str):
            assert not value.casefold().startswith(("https://", "http://"))

    assert_closed(request)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_provider_default_opener_rejects_redirect_without_replaying_bearer(
    tmp_path: Any,
    valid_context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    requests: list[Request] = []
    installed_handlers: list[object] = []

    class RedirectingDirector:
        def open(self, request: Request, *, timeout: float) -> Any:
            assert timeout == 30
            requests.append(request)
            headers = Message()
            headers["Location"] = "https://attacker.invalid/credential-collector"
            handler = next(
                item
                for item in installed_handlers
                if isinstance(item, audio_provider_module._RejectRedirectHandler)
            )
            redirect = getattr(handler, f"http_error_{status}")
            response = redirect(request, None, status, "redirect", headers)
            requests.append(
                Request(  # noqa: S310 - fixed HTTPS URL models a forbidden redirect
                    headers["Location"]
                )
            )
            return response

    def fake_build_opener(*handlers: object) -> RedirectingDirector:
        installed_handlers.extend(handlers)
        return RedirectingDirector()

    monkeypatch.setattr(audio_provider_module, "build_opener", fake_build_opener)
    provider = HTTPSAudioInferenceProvider(
        provider="acme_speech",
        allowed_models="audio-v2.3.1",
        endpoint="https://inference.vendor.invalid/v1/audio-intelligence",
        token_file=_secret(tmp_path),
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
        environment="ci",
    )

    with pytest.raises(AudioProviderFailure) as raised:
        provider.infer(_envelope(valid_context))

    assert raised.value.code == "AUDIO_PROVIDER_REQUEST_REJECTED"
    assert raised.value.retryable is False
    assert len(requests) == 1
    assert requests[0].full_url == "https://inference.vendor.invalid/v1/audio-intelligence"
    assert requests[0].get_header("Authorization") == (
        "Bearer unit-only-provider-token-value-00000001"
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("tenant_id", "other-tenant"),
        lambda payload: payload["inference"].__setitem__("model", "other-model"),
        lambda payload: payload["input_object"].__setitem__("version_id", "latest"),
        lambda payload: payload["input_object"].__setitem__("content_sha256", "0" * 64),
        lambda payload: payload.__setitem__("request_sha256", "0" * 64),
        lambda payload: payload.__setitem__("unexpected", "not-allowed"),
    ],
)
def test_provider_response_rejects_binding_tampering_or_unknown_fields(
    valid_context: dict[str, Any],
    mutation: Any,
) -> None:
    envelope = _envelope(valid_context)
    payload = deepcopy(_response_payload(envelope))
    mutation(payload)

    with pytest.raises(AudioProviderContractError):
        validate_audio_provider_response(
            payload,
            envelope=envelope,
            request_sha256=canonical_sha256(build_audio_provider_request(envelope)),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["result"]["transcript"]["segments"][0].__setitem__(
            "confidence", float("nan")
        ),
        lambda payload: payload["result"]["transcript"]["segments"][0].__setitem__(
            "end_ms", 604_800_001
        ),
        lambda payload: payload["result"]["analyses"][0].__setitem__("score", 1.01),
        lambda payload: payload["result"]["transcript"].__setitem__("text", "x" * 500_001),
        lambda payload: payload["result"]["analyses"][0].__setitem__("secret", "not-allowed"),
    ],
)
def test_provider_response_rejects_nonfinite_oversized_or_malicious_results(
    valid_context: dict[str, Any],
    mutation: Any,
) -> None:
    envelope = _envelope(valid_context)
    payload = deepcopy(_response_payload(envelope))
    mutation(payload)
    try:
        payload["result_sha256"] = canonical_sha256(payload["result"])
    except AudioProviderContractError:
        payload["result_sha256"] = "0" * 64

    with pytest.raises(AudioProviderContractError):
        validate_audio_provider_response(
            payload,
            envelope=envelope,
            request_sha256=canonical_sha256(build_audio_provider_request(envelope)),
        )


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (TimeoutError("secret timeout detail"), "AUDIO_PROVIDER_UNAVAILABLE", True),
        (URLError("secret dns detail"), "AUDIO_PROVIDER_UNAVAILABLE", True),
        (
            HTTPError("https://hidden", 503, "secret upstream detail", Message(), None),
            "AUDIO_PROVIDER_UNAVAILABLE",
            True,
        ),
        (
            HTTPError("https://hidden", 400, "secret request detail", Message(), None),
            "AUDIO_PROVIDER_REQUEST_REJECTED",
            False,
        ),
    ],
)
def test_provider_classifies_transport_and_http_failures_without_leaking_details(
    tmp_path: Any,
    valid_context: dict[str, Any],
    failure: Exception,
    code: str,
    retryable: bool,
) -> None:
    envelope = _envelope(valid_context)

    def opener(*_args: Any, **_kwargs: Any) -> _Response:
        raise failure

    provider = HTTPSAudioInferenceProvider(
        provider="acme_speech",
        allowed_models="audio-v2.3.1",
        endpoint="https://inference.vendor.test/v1/audio-intelligence",
        token_file=_secret(tmp_path),
        opener=opener,
        environment="ci",
    )

    with pytest.raises(AudioProviderFailure) as raised:
        provider.infer(envelope)

    assert raised.value.code == code
    assert raised.value.retryable is retryable
    assert "secret" not in str(raised.value).lower()
    assert "hidden" not in str(raised.value).lower()


def test_provider_rejects_plaintext_weak_secret_and_oversized_response(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    token_file = _secret(tmp_path)
    with pytest.raises(AudioProviderFailure, match="configuration"):
        HTTPSAudioInferenceProvider(
            provider="acme_speech",
            allowed_models="audio-v2.3.1",
            endpoint="http://inference.vendor.test/v1/audio-intelligence",
            token_file=token_file,
            environment="ci",
        )
    with pytest.raises(AudioProviderFailure, match="configuration"):
        HTTPSAudioInferenceProvider(
            provider="acme_speech",
            allowed_models="audio-v2.3.1",
            endpoint="https://inference.vendor.test/v1/audio-intelligence",
            token_file=_secret(tmp_path, "weak", name="weak-provider-token"),
            environment="ci",
        )

    envelope = _envelope(valid_context)
    provider = HTTPSAudioInferenceProvider(
        provider="acme_speech",
        allowed_models="audio-v2.3.1",
        endpoint="https://inference.vendor.test/v1/audio-intelligence",
        token_file=token_file,
        max_response_bytes=128,
        opener=lambda *_args, **_kwargs: _Response(b"x" * 129),
        environment="ci",
    )
    with pytest.raises(AudioProviderFailure) as raised:
        provider.infer(envelope)
    assert raised.value.code == "AUDIO_PROVIDER_RESPONSE_INVALID"
    assert raised.value.retryable is False


def test_provider_rejects_model_outside_explicit_server_allowlist(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _envelope(valid_context)
    provider = HTTPSAudioInferenceProvider(
        provider="acme_speech",
        allowed_models="approved-audio-v9",
        endpoint="https://inference.vendor.test/v1/audio-intelligence",
        token_file=_secret(tmp_path),
        opener=lambda *_args, **_kwargs: pytest.fail("network must not be reached"),
        environment="ci",
    )

    with pytest.raises(AudioProviderFailure) as raised:
        provider.infer(envelope)

    assert raised.value.code == "AUDIO_PROVIDER_CONFIGURATION_INVALID"
    assert raised.value.retryable is False


def test_result_manifest_store_writes_canonical_versioned_internal_manifest(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    envelope = _envelope(valid_context)
    response = _response_payload(envelope)
    result = validate_audio_provider_response(
        response,
        envelope=envelope,
        request_sha256=canonical_sha256(build_audio_provider_request(envelope)),
    )
    requests: list[Request] = []

    def opener(request: Request, *, timeout: float) -> _Response:
        assert timeout == 5
        requests.append(request)
        return _Response(b"", version_id="result-version-7")

    store = S3AudioResultManifestStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        bucket="auris-flow",
        allowed_buckets="auris-flow",
        access_key_file=_secret(
            tmp_path,
            "unit-only-access-key-value-00000001",
            name="access",
        ),
        secret_key_file=_secret(
            tmp_path,
            "unit-only-secret-key-value-00000001",
            name="secret",
        ),
        opener=opener,
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )
    integrity = {
        "manifest_version": "auris-flow-audio-input-integrity-v1",
        "status": "verified",
        "execution_envelope_sha256": envelope.sha256,
        "storage_object_id_sha256": hashlib.sha256(
            envelope.input_object.storage_object_id.encode()
        ).hexdigest(),
        "object_version_id_sha256": hashlib.sha256(
            envelope.input_object.version_id.encode()
        ).hexdigest(),
        "expected_content_sha256": envelope.input_object.content_sha256,
        "observed_content_sha256": envelope.input_object.content_sha256,
        "content_length": envelope.input_object.content_length,
    }

    receipt = store.persist(envelope=envelope, integrity_manifest=integrity, result=result)

    manifest = json.loads((requests[0].data or b"").decode("utf-8"))
    assert manifest["schema_version"] == AUDIO_RESULT_MANIFEST_SCHEMA
    assert manifest["input_object"]["version_id"] == "immutable-version-3"
    assert manifest["provider_result"]["transcript"]["text"] == "hello world"
    assert receipt.manifest_sha256 == hashlib.sha256(requests[0].data or b"").hexdigest()
    assert receipt.object_version_id_sha256 == hashlib.sha256(b"result-version-7").hexdigest()
    assert "result-version-7" not in repr(receipt)
    assert envelope.input_object.object_key not in repr(receipt)
    assert requests[0].get_header("X-amz-content-sha256") == receipt.manifest_sha256
    public = receipt.public_mapping()
    internal = receipt.internal_callback_mapping()
    assert "storage_objects" not in public
    assert public["result_manifest_storage_object_id"].startswith("sto_audio_manifest_")
    assert internal["storage_objects"] == [
        {
            "storage_object_id": public["result_manifest_storage_object_id"],
            "role": "manifest",
            "provider": "minio",
            "bucket": "auris-flow",
            "object_key": requests[0].full_url.removeprefix("http://minio:9000/auris-flow/"),
            "version_id": "result-version-7",
            "content_type": "application/json",
            "size_bytes": len(requests[0].data or b""),
            "content_sha256": receipt.manifest_sha256,
        }
    ]
    assert "result-version-7" not in repr(public)
    assert internal["storage_objects"][0]["version_id"] == "result-version-7"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_result_store_default_opener_rejects_redirect_without_replaying_sigv4(
    tmp_path: Any,
    valid_context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    envelope = _envelope(valid_context)
    response = _response_payload(envelope)
    result = validate_audio_provider_response(
        response,
        envelope=envelope,
        request_sha256=canonical_sha256(build_audio_provider_request(envelope)),
    )
    requests: list[Request] = []
    installed_handlers: list[object] = []

    class RedirectingDirector:
        def open(self, request: Request, *, timeout: float) -> Any:
            assert timeout == 5
            requests.append(request)
            headers = Message()
            headers["Location"] = "http://attacker.invalid/sigv4-collector"
            handler = next(
                item
                for item in installed_handlers
                if isinstance(item, audio_provider_module._RejectRedirectHandler)
            )
            redirect = getattr(handler, f"http_error_{status}")
            response = redirect(request, None, status, "redirect", headers)
            requests.append(
                Request(  # noqa: S310 - fixed HTTP URL models a forbidden redirect
                    headers["Location"]
                )
            )
            return response

    def fake_build_opener(*handlers: object) -> RedirectingDirector:
        installed_handlers.extend(handlers)
        return RedirectingDirector()

    monkeypatch.setattr(audio_provider_module, "build_opener", fake_build_opener)
    store = S3AudioResultManifestStore(
        provider="minio",
        endpoint="http://minio:9000",
        region="us-east-1",
        bucket="auris-flow",
        allowed_buckets="auris-flow",
        access_key_file=_secret(
            tmp_path,
            "unit-only-access-key-value-00000001",
            name="access",
        ),
        secret_key_file=_secret(
            tmp_path,
            "unit-only-secret-key-value-00000001",
            name="secret",
        ),
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )
    integrity = {
        "status": "verified",
        "execution_envelope_sha256": envelope.sha256,
    }

    with pytest.raises(AudioProviderFailure) as raised:
        store.persist(envelope=envelope, integrity_manifest=integrity, result=result)

    assert raised.value.code == "AUDIO_RESULT_PERSISTENCE_FAILED"
    assert raised.value.retryable is True
    assert len(requests) == 1
    assert requests[0].full_url.startswith("http://minio:9000/")
    assert requests[0].get_header("Authorization", "").startswith("AWS4-HMAC-SHA256 ")


def test_production_dependencies_are_validated_before_code_location_health(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AURIS_AUDIO_INFERENCE_PROVIDER",
        "AURIS_AUDIO_INFERENCE_ALLOWED_MODELS",
        "AURIS_AUDIO_INFERENCE_ENDPOINT",
        "AURIS_AUDIO_INFERENCE_API_TOKEN_FILE",
        "AURIS_AUDIO_OBJECT_STORAGE_PROVIDER",
        "AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT",
        "AURIS_AUDIO_OBJECT_STORAGE_REGION",
        "AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS",
        "AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE",
        "AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE",
        "AURIS_AUDIO_RESULT_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(AudioProviderFailure):
        configured_audio_runtime_dependencies(environment="prod")

    access = _secret(tmp_path, "unit-only-access-key-value-00000001", name="access")
    secret = _secret(tmp_path, "unit-only-secret-key-value-00000001", name="secret")
    token = _secret(tmp_path, name="audio-provider-token")
    values = {
        "AURIS_AUDIO_INFERENCE_PROVIDER": "acme_speech",
        "AURIS_AUDIO_INFERENCE_ALLOWED_MODELS": "audio-v2.3.1",
        "AURIS_AUDIO_INFERENCE_ENDPOINT": (
            "https://audio.production-gate.invalid:8443/v1/audio-intelligence"
        ),
        "AURIS_AUDIO_INFERENCE_API_TOKEN_FILE": token,
        "AURIS_AUDIO_OBJECT_STORAGE_PROVIDER": "minio",
        "AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
        "AURIS_AUDIO_OBJECT_STORAGE_REGION": "us-east-1",
        "AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS": "auris-flow",
        "AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE": access,
        "AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE": secret,
        "AURIS_AUDIO_RESULT_BUCKET": "auris-flow",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    provider, store = configured_audio_runtime_dependencies(environment="prod")

    assert isinstance(provider, HTTPSAudioInferenceProvider)
    assert isinstance(store, S3AudioResultManifestStore)
