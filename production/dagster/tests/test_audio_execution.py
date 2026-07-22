from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from urllib.request import Request

import pytest

from auris_flow_dagster import audio_integrity as audio_integrity_module
from auris_flow_dagster.audio_integrity import (
    AudioInputIntegrityError,
    S3VersionedAudioVerifier,
)
from auris_flow_dagster.audio_provider import (
    AUDIO_RESULT_MANIFEST_SCHEMA,
    AudioProviderFailure,
    AudioProviderResult,
    AudioResultManifestReceipt,
    AudioResultPersistenceError,
)
from auris_flow_dagster.contracts import (
    AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    AudioExecutionEnvelope,
    AurisContractError,
    validate_audio_execution_envelope,
)
from auris_flow_dagster.runtime import (
    AurisWorkflowError,
    execute_audio_intelligence_and_report,
)


def _envelope(
    valid_context: dict[str, Any],
    body: bytes = b"RIFF" + b"\x00" * 60,
) -> dict[str, Any]:
    return {
        "schema_version": "auris-flow-execution-envelope-v1",
        "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        "tenant_id": valid_context["tenant_id"],
        "project_id": valid_context["project_id"],
        "trace_id": valid_context["trace_id"],
        "run_id": valid_context["run_id"],
        "dispatch_idempotency_key": valid_context["dispatch_idempotency_key"],
        "outbox_fencing_token": valid_context["outbox_fencing_token"],
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
            "provider": "audio_intelligence_default",
            "model": "audio-v2.3.1",
        },
        "capabilities": ["vad", "asr"],
    }


def _audio_context(valid_context: dict[str, Any]) -> dict[str, Any]:
    return {**valid_context, "event_type": "audio_intelligence.requested"}


def _verified_manifest(envelope: AudioExecutionEnvelope) -> dict[str, Any]:
    return {
        "manifest_version": "auris-flow-audio-input-integrity-v1",
        "status": "verified",
        "execution_envelope_sha256": envelope.sha256,
        "storage_object_id_sha256": hashlib.sha256(
            envelope.input_object.storage_object_id.encode("utf-8")
        ).hexdigest(),
        "object_version_id_sha256": hashlib.sha256(
            envelope.input_object.version_id.encode("utf-8")
        ).hexdigest(),
        "expected_content_sha256": envelope.input_object.content_sha256,
        "observed_content_sha256": envelope.input_object.content_sha256,
        "content_length": envelope.input_object.content_length,
    }


def test_audio_execution_envelope_is_strict_and_bound_to_authoritative_scope(
    valid_context: dict[str, Any],
) -> None:
    audio_context = _audio_context(valid_context)
    envelope = _envelope(audio_context)

    parsed = validate_audio_execution_envelope(envelope, auris_context=audio_context)

    assert isinstance(parsed, AudioExecutionEnvelope)
    assert parsed.input_object.version_id == "immutable-version-3"
    assert parsed.inference.model == "audio-v2.3.1"
    assert parsed.sha256 == hashlib.sha256(parsed.canonical_json.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("deadline_at"), "deadline_at"),
        (lambda value: value.__setitem__("tenant_id", "other_tenant"), "tenant_id"),
        (lambda value: value["input_object"].pop("version_id"), "version_id"),
        (
            lambda value: value["input_object"].__setitem__("version_id", "null"),
            "version_id",
        ),
        (lambda value: value["input_object"].pop("content_sha256"), "content_sha256"),
        (
            lambda value: value["input_object"].__setitem__("object_key", "../escape.wav"),
            "object_key",
        ),
        (lambda value: value.__setitem__("deadline_at", "2020-01-01T00:00:00Z"), "expired"),
        (lambda value: value.__setitem__("unexpected", "value"), "unexpected"),
    ],
)
def test_audio_execution_envelope_rejects_missing_forged_expired_or_extra_fields(
    valid_context: dict[str, Any],
    mutation: Any,
    message: str,
) -> None:
    audio_context = _audio_context(valid_context)
    envelope = deepcopy(_envelope(audio_context))
    mutation(envelope)

    with pytest.raises(AurisContractError, match=message):
        validate_audio_execution_envelope(envelope, auris_context=audio_context)


@pytest.mark.parametrize("event_type", [None, "task_run.requested"])
def test_audio_execution_envelope_rejects_missing_or_wrong_context_event_type(
    valid_context: dict[str, Any],
    event_type: str | None,
) -> None:
    audio_context = _audio_context(valid_context)
    envelope = _envelope(audio_context)
    if event_type is None:
        audio_context.pop("event_type")
    else:
        audio_context["event_type"] = event_type

    with pytest.raises(AurisContractError, match="event_type"):
        validate_audio_execution_envelope(envelope, auris_context=audio_context)


class _Response:
    status = 200

    def __init__(self, body: bytes, *, version_id: str) -> None:
        self._body = body
        headers = Message()
        headers["Content-Length"] = str(len(body))
        headers["Content-Type"] = "audio/wav"
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


def _secret(path: Any, value: str) -> str:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o400)
    return str(path)


def test_versioned_audio_verifier_reads_exact_version_and_recomputes_sha256(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    body = b"RIFFstrict-versioned-wave-payload" + b"\x00" * 32
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context, body),
        auris_context=audio_context,
    )
    requests: list[Request] = []

    def opener(request: Request, *, timeout: float) -> _Response:
        assert timeout == 5
        requests.append(request)
        return _Response(body, version_id=envelope.input_object.version_id)

    verifier = S3VersionedAudioVerifier(
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret(tmp_path / "access", "unit-access-key"),
        secret_key_file=_secret(tmp_path / "secret", "unit-secret-value-with-32-characters"),
        opener=opener,
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )

    manifest = verifier.verify(envelope)

    assert manifest["status"] == "verified"
    assert manifest["expected_content_sha256"] == hashlib.sha256(body).hexdigest()
    assert manifest["observed_content_sha256"] == hashlib.sha256(body).hexdigest()
    assert manifest["content_length"] == len(body)
    assert "object_key" not in manifest
    assert requests[0].full_url.endswith("?versionId=immutable-version-3")
    assert requests[0].get_header("Authorization").startswith("AWS4-HMAC-SHA256 ")


def test_versioned_audio_verifier_fails_closed_on_content_mismatch(
    tmp_path: Any,
    valid_context: dict[str, Any],
) -> None:
    expected = b"RIFFexpected" + b"\x00" * 52
    observed = b"RIFFtampered" + b"\x00" * 52
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context, expected),
        auris_context=audio_context,
    )

    verifier = S3VersionedAudioVerifier(
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret(tmp_path / "access", "unit-access-key"),
        secret_key_file=_secret(tmp_path / "secret", "unit-secret-value-with-32-characters"),
        opener=lambda *_args, **_kwargs: _Response(
            observed,
            version_id=envelope.input_object.version_id,
        ),
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )

    with pytest.raises(AudioInputIntegrityError, match="content hash mismatch") as failure:
        verifier.verify(envelope)

    assert expected.decode("ascii") not in str(failure.value)
    assert observed.decode("ascii") not in str(failure.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_versioned_audio_verifier_default_opener_rejects_redirect_before_sigv4_replay(
    tmp_path: Any,
    valid_context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context),
        auris_context=audio_context,
    )
    requests: list[Request] = []
    installed_handlers: list[object] = []

    class RedirectingDirector:
        def open(self, request: Request, *, timeout: float) -> Any:
            del timeout
            requests.append(request)
            handler = next(
                item
                for item in installed_handlers
                if isinstance(item, audio_integrity_module._RejectRedirectHandler)
            )
            redirect = getattr(handler, f"http_error_{status}")
            redirect(
                request,
                None,
                status,
                "redirect",
                {"Location": "http://attacker.invalid/sigv4-replay"},
            )
            pytest.fail("redirect handler must raise before issuing a second request")

    def fake_build_opener(*handlers: object) -> RedirectingDirector:
        installed_handlers.extend(handlers)
        return RedirectingDirector()

    monkeypatch.setattr(audio_integrity_module, "build_opener", fake_build_opener)
    verifier = S3VersionedAudioVerifier(
        endpoint="http://minio:9000",
        region="us-east-1",
        allowed_buckets="auris-flow",
        access_key_file=_secret(tmp_path / "access", "unit-access-key"),
        secret_key_file=_secret(
            tmp_path / "secret",
            "unit-secret-value-with-32-characters",
        ),
        clock=lambda: datetime(2026, 7, 21, 10, 0, tzinfo=UTC),
    )

    with pytest.raises(AudioInputIntegrityError):
        verifier.verify(envelope)

    assert len(requests) == 1
    assert requests[0].get_header("Authorization", "").startswith("AWS4-HMAC-SHA256 ")


class _RecordingCallback:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, _scope: Any, **values: Any) -> dict[str, Any]:
        self.calls.append(values)
        return {"data": {"status": values["status"]}}


class _SuccessfulProvider:
    def infer(self, _envelope: AudioExecutionEnvelope) -> AudioProviderResult:
        return AudioProviderResult(
            request_sha256="1" * 64,
            response_sha256="2" * 64,
            result_sha256="3" * 64,
            provider_result={
                "transcript": {
                    "language": "zh-CN",
                    "text": "sensitive transcript must stay in the internal manifest",
                    "segments": [],
                },
                "analyses": [],
            },
        )


class _SuccessfulStore:
    def persist(self, **values: Any) -> AudioResultManifestReceipt:
        assert values["integrity_manifest"]["status"] == "verified"
        assert isinstance(values["result"], AudioProviderResult)
        return AudioResultManifestReceipt(
            manifest_schema=AUDIO_RESULT_MANIFEST_SCHEMA,
            manifest_sha256="4" * 64,
            object_key_sha256="5" * 64,
            object_version_id_sha256="6" * 64,
            provider_request_sha256="1" * 64,
            provider_response_sha256="2" * 64,
            provider_result_sha256="3" * 64,
            storage_object_id="sto_audio_manifest_test",
            storage_provider="minio",
            bucket="auris-flow",
            object_key=(
                "tenants/aurora_auto/projects/sales_qa/runs/run_123/"
                "audio-intelligence/manifest.json"
            ),
            object_version_id="result-version-7",
            content_length=512,
        )


def test_audio_job_persists_real_provider_result_before_success_callback(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    callback = _RecordingCallback()
    audio_context = _audio_context(valid_context)
    raw_envelope = _envelope(audio_context)
    raw_envelope["inference"]["provider"] = "acme_speech"
    envelope = validate_audio_execution_envelope(raw_envelope, auris_context=audio_context)
    audio_scope = replace(scope, event_type="audio_intelligence.requested")

    result_ref = execute_audio_intelligence_and_report(
        scope=audio_scope,
        dagster_run_id="dagster-audio-success",
        envelope=envelope,
        callback=callback,  # type: ignore[arg-type]
        verifier=lambda _envelope: _verified_manifest(envelope),
        provider=_SuccessfulProvider(),  # type: ignore[arg-type]
        manifest_store=_SuccessfulStore(),  # type: ignore[arg-type]
    )

    assert result_ref != callback.calls[0]["result_ref"]
    assert "storage_objects" not in result_ref
    assert callback.calls[0]["result_ref"]["storage_objects"][0]["version_id"] == (
        "result-version-7"
    )
    assert callback.calls[0]["status"] == "success"
    assert callback.calls[0]["metrics"] == {
        "input_integrity_verified": 1,
        "provider_response_validated": 1,
        "domain_outputs_materialized": 1,
    }
    serialized = repr(callback.calls[0])
    assert "sensitive transcript" not in serialized
    assert envelope.input_object.object_key not in serialized
    assert envelope.input_object.version_id not in serialized
    assert "endpoint" not in serialized.casefold()
    assert result_ref["result_manifest_sha256"] == "4" * 64


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (
            AudioProviderFailure(
                "audio inference provider is unavailable",
                code="AUDIO_PROVIDER_UNAVAILABLE",
                retryable=True,
            ),
            "AUDIO_PROVIDER_UNAVAILABLE",
            True,
        ),
        (
            AudioResultPersistenceError(),
            "AUDIO_RESULT_PERSISTENCE_FAILED",
            True,
        ),
    ],
)
def test_audio_job_reports_sanitized_provider_or_persistence_failure(
    scope: Any,
    valid_context: dict[str, Any],
    failure: AudioProviderFailure,
    code: str,
    retryable: bool,
) -> None:
    callback = _RecordingCallback()
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context),
        auris_context=audio_context,
    )
    audio_scope = replace(scope, event_type="audio_intelligence.requested")

    class FailingProvider:
        def infer(self, _envelope: AudioExecutionEnvelope) -> AudioProviderResult:
            if isinstance(failure, AudioResultPersistenceError):
                return _SuccessfulProvider().infer(_envelope)
            raise failure

    class FailingStore:
        def persist(self, **_values: Any) -> AudioResultManifestReceipt:
            raise failure

    with pytest.raises(AurisWorkflowError, match="audio intelligence execution failed"):
        execute_audio_intelligence_and_report(
            scope=audio_scope,
            dagster_run_id="dagster-audio-failure",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
            verifier=lambda _envelope: _verified_manifest(envelope),
            provider=FailingProvider(),  # type: ignore[arg-type]
            manifest_store=FailingStore(),  # type: ignore[arg-type]
        )

    receipt = callback.calls[0]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == code
    assert receipt["retryable"] is retryable
    assert receipt["result_ref"]["status"] == "input_verified_execution_failed"
    assert envelope.input_object.object_key not in repr(receipt)


def test_audio_job_reports_verified_input_but_fails_closed_without_real_provider(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    callback = _RecordingCallback()
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context),
        auris_context=audio_context,
    )
    audio_scope = replace(scope, event_type="audio_intelligence.requested")

    with pytest.raises(AurisWorkflowError, match="provider is not configured"):
        execute_audio_intelligence_and_report(
            scope=audio_scope,
            dagster_run_id="dagster-audio-001",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
            verifier=lambda _envelope: _verified_manifest(envelope),
        )

    assert len(callback.calls) == 1
    receipt = callback.calls[0]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "AUDIO_INTELLIGENCE_PROVIDER_NOT_CONFIGURED"
    assert receipt["retryable"] is False
    assert receipt["result_ref"]["status"] == "input_verified_provider_unavailable"
    assert receipt["metrics"] == {
        "input_integrity_verified": 1,
        "provider_response_validated": 0,
        "domain_outputs_materialized": 0,
    }
    assert envelope.input_object.content_sha256 not in repr(receipt)
    assert "control_plane_acknowledged" not in repr(receipt)


def test_audio_job_rejects_verifier_extra_fields_without_propagating_them(
    scope: Any,
    valid_context: dict[str, Any],
) -> None:
    callback = _RecordingCallback()
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context),
        auris_context=audio_context,
    )
    audio_scope = replace(scope, event_type="audio_intelligence.requested")
    raw_manifest = {
        **_verified_manifest(envelope),
        "secret": "must-not-enter-completion-receipt",
    }

    with pytest.raises(AurisWorkflowError, match="integrity verification failed"):
        execute_audio_intelligence_and_report(
            scope=audio_scope,
            dagster_run_id="dagster-audio-extra-manifest-field",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
            verifier=lambda _envelope: raw_manifest,
        )

    assert callback.calls[0]["error_code"] == "AUDIO_INPUT_INTEGRITY_FAILED"
    assert "must-not-enter-completion-receipt" not in repr(callback.calls[0])


def test_audio_job_reports_sanitized_integrity_failure_when_reader_is_unconfigured(
    scope: Any,
    valid_context: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    callback = _RecordingCallback()
    audio_context = _audio_context(valid_context)
    envelope = validate_audio_execution_envelope(
        _envelope(audio_context),
        auris_context=audio_context,
    )
    audio_scope = replace(scope, event_type="audio_intelligence.requested")
    for name in (
        "AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT",
        "AURIS_AUDIO_OBJECT_STORAGE_REGION",
        "AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS",
        "AURIS_AUDIO_OBJECT_STORAGE_ACCESS_KEY_FILE",
        "AURIS_AUDIO_OBJECT_STORAGE_SECRET_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(AurisWorkflowError, match="integrity verification failed") as failure:
        execute_audio_intelligence_and_report(
            scope=audio_scope,
            dagster_run_id="dagster-audio-unconfigured",
            envelope=envelope,
            callback=callback,  # type: ignore[arg-type]
        )

    assert failure.value.__cause__ is None
    assert len(callback.calls) == 1
    receipt = callback.calls[0]
    assert receipt["status"] == "failed"
    assert receipt["error_code"] == "AUDIO_INPUT_INTEGRITY_FAILED"
    assert receipt["result_ref"]["status"] == "input_integrity_failed"
    assert envelope.input_object.content_sha256 not in repr(receipt)
    assert "secret" not in repr(receipt).lower()
