from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from auris_flow_dagster.audio_integrity import (
    AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION,
    AudioInputIntegrityError,
    S3VersionedAudioVerifier,
    safe_integrity_failure_manifest,
)
from auris_flow_dagster.audio_provider import (
    AudioProviderFailure,
    AudioProviderResult,
    AudioResultManifestReceipt,
    HTTPSAudioInferenceProvider,
    S3AudioResultManifestStore,
    canonical_sha256,
)
from auris_flow_dagster.callback import CompletionCallbackClient
from auris_flow_dagster.contracts import AudioExecutionEnvelope, AurisRunContext

WorkflowResult = tuple[Mapping[str, Any], Mapping[str, Any]]
Workflow = Callable[[AurisRunContext, Mapping[str, Any]], WorkflowResult]
AudioInputVerifier = Callable[[AudioExecutionEnvelope], Mapping[str, Any]]
_VERIFIED_AUDIO_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "status",
        "execution_envelope_sha256",
        "storage_object_id_sha256",
        "object_version_id_sha256",
        "expected_content_sha256",
        "observed_content_sha256",
        "content_length",
    }
)


class AurisWorkflowError(RuntimeError):
    """Sanitized workflow failure suitable for Dagster event logs."""


class AudioInferenceProvider(Protocol):
    def infer(self, envelope: AudioExecutionEnvelope) -> AudioProviderResult: ...


class AudioResultManifestStore(Protocol):
    def persist(
        self,
        *,
        envelope: AudioExecutionEnvelope,
        integrity_manifest: Mapping[str, Any],
        result: AudioProviderResult,
    ) -> AudioResultManifestReceipt: ...


def configured_audio_runtime_dependencies(
    *,
    environment: str | None = None,
) -> tuple[AudioInferenceProvider | None, AudioResultManifestStore | None]:
    """Build production dependencies during code-location import.

    Prod/release code servers must not become healthy with a missing HTTPS provider,
    model allowlist, credential file, or writable versioned result-store configuration.
    Non-production test jobs retain explicit dependency injection.
    """

    active_environment = (
        (environment if environment is not None else os.environ.get("APP_ENV", "local"))
        .strip()
        .casefold()
    )
    if active_environment not in {"prod", "production", "release"}:
        return None, None
    return HTTPSAudioInferenceProvider(), S3AudioResultManifestStore()


def _validated_audio_integrity_manifest(
    raw: object,
    *,
    envelope: AudioExecutionEnvelope,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _VERIFIED_AUDIO_MANIFEST_FIELDS:
        raise AudioInputIntegrityError("audio input verifier evidence is invalid")
    storage_object_id_sha256 = hashlib.sha256(
        envelope.input_object.storage_object_id.encode("utf-8")
    ).hexdigest()
    object_version_id_sha256 = hashlib.sha256(
        envelope.input_object.version_id.encode("utf-8")
    ).hexdigest()
    expected_values: dict[str, Any] = {
        "manifest_version": AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION,
        "status": "verified",
        "execution_envelope_sha256": envelope.sha256,
        "storage_object_id_sha256": storage_object_id_sha256,
        "object_version_id_sha256": object_version_id_sha256,
        "expected_content_sha256": envelope.input_object.content_sha256,
        "observed_content_sha256": envelope.input_object.content_sha256,
        "content_length": envelope.input_object.content_length,
    }
    if any(raw.get(field) != expected for field, expected in expected_values.items()):
        raise AudioInputIntegrityError("audio input verifier evidence is invalid")
    # Rebuild from the exact contract instead of forwarding a verifier-owned mapping.
    return expected_values


def acknowledge_domain_workflow(
    scope: AurisRunContext,
    execution: Mapping[str, Any],
) -> WorkflowResult:
    """Validate orchestration without pretending to perform domain-specific inference.

    Product-specific jobs can replace this callable while preserving the same completion
    contract. The generic job deliberately emits only control-plane evidence.
    """

    requested_mode = execution.get("mode", "control-plane-acknowledgement")
    if requested_mode == "ci-cancel-delay":
        if os.getenv("APP_ENV") != "ci":
            raise ValueError("CI-only Auris Flow execution mode is disabled")
        raw_delay = execution.get("delay_seconds", 20)
        if (
            isinstance(raw_delay, bool)
            or not isinstance(raw_delay, int | float)
            or not 1 <= raw_delay <= 30
        ):
            raise ValueError("CI cancel delay must be between 1 and 30 seconds")
        time.sleep(float(raw_delay))
    elif requested_mode == "ci-intentional-failure":
        if os.getenv("APP_ENV") != "ci":
            raise ValueError("CI-only Auris Flow execution mode is disabled")
        raise ValueError("intentional CI workflow failure")
    elif requested_mode != "control-plane-acknowledgement":
        raise ValueError("unsupported Auris Flow execution mode")
    return (
        {
            "execution_contract": "auris-flow-generic-v1",
            "auris_run_id": scope.run_id,
            "trace_id": scope.trace_id,
        },
        {"control_plane_acknowledged": 1},
    )


def execute_and_report(
    *,
    scope: AurisRunContext,
    dagster_run_id: str,
    execution: Mapping[str, Any],
    callback: CompletionCallbackClient,
    workflow: Workflow = acknowledge_domain_workflow,
) -> Mapping[str, Any]:
    result_ref: Mapping[str, Any] = {}
    metrics: Mapping[str, Any] = {}
    workflow_failed = False
    try:
        result_ref, metrics = workflow(scope, execution)
    except Exception:
        workflow_failed = True

    if workflow_failed:
        callback_failed = False
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                error_code="DAGSTER_WORKFLOW_FAILED",
                retryable=False,
            )
        except Exception:
            callback_failed = True
        message = (
            "Auris Flow domain execution and completion callback failed"
            if callback_failed
            else "Auris Flow domain execution failed"
        )
        raise AurisWorkflowError(message) from None

    callback.post(
        scope,
        dagster_run_id=dagster_run_id,
        status="success",
        result_ref=result_ref,
        metrics=metrics,
    )
    return result_ref


def execute_audio_intelligence_and_report(
    *,
    scope: AurisRunContext,
    dagster_run_id: str,
    envelope: AudioExecutionEnvelope,
    callback: CompletionCallbackClient,
    verifier: AudioInputVerifier | None = None,
    provider: AudioInferenceProvider | None = None,
    manifest_store: AudioResultManifestStore | None = None,
) -> Mapping[str, Any]:
    """Verify exact input, invoke the configured HTTPS provider, persist, then report."""

    try:
        verify = verifier or S3VersionedAudioVerifier().verify
        raw_manifest = verify(envelope)
        manifest = _validated_audio_integrity_manifest(raw_manifest, envelope=envelope)
    except Exception:
        failure_manifest = safe_integrity_failure_manifest(
            envelope,
            status="input_integrity_failed",
        )
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                result_ref=failure_manifest,
                metrics={
                    "input_integrity_verified": 0,
                    "domain_outputs_materialized": 0,
                },
                error_code="AUDIO_INPUT_INTEGRITY_FAILED",
                retryable=False,
            )
        except Exception:
            raise AurisWorkflowError(
                "Auris Flow audio input integrity and completion callback failed"
            ) from None
        raise AurisWorkflowError("Auris Flow audio input integrity verification failed") from None

    execution_evidence = {
        "manifest_version": manifest["manifest_version"],
        "status": "input_verified_execution_failed",
        "execution_envelope_sha256": manifest["execution_envelope_sha256"],
        "storage_object_id_sha256": manifest["storage_object_id_sha256"],
        "object_version_id_sha256": manifest["object_version_id_sha256"],
        "content_length": manifest["content_length"],
        "content_sha256_verified": True,
        "execution_contract": envelope.execution_contract,
        "inference_binding_sha256": hashlib.sha256(
            (f"{envelope.inference.provider}\n{envelope.inference.model}").encode()
        ).hexdigest(),
        "requested_capabilities": list(envelope.capabilities),
    }

    provider_unconfigured = provider is None and not any(
        (os.environ.get(name) or "").strip()
        for name in (
            "AURIS_AUDIO_INFERENCE_PROVIDER",
            "AURIS_AUDIO_INFERENCE_ENDPOINT",
            "AURIS_AUDIO_INFERENCE_API_TOKEN_FILE",
        )
    )
    try:
        active_provider = provider or HTTPSAudioInferenceProvider()
        provider_result = active_provider.infer(envelope)
        active_store = manifest_store or S3AudioResultManifestStore()
        result_receipt = active_store.persist(
            envelope=envelope,
            integrity_manifest=manifest,
            result=provider_result,
        )
    except AudioProviderFailure as failure:
        error_code = failure.code
        retryable = failure.retryable
        if provider_unconfigured:
            error_code = "AUDIO_INTELLIGENCE_PROVIDER_NOT_CONFIGURED"
            retryable = False
            execution_evidence["status"] = "input_verified_provider_unavailable"
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                result_ref=execution_evidence,
                metrics={
                    "input_integrity_verified": 1,
                    "provider_response_validated": 0,
                    "domain_outputs_materialized": 0,
                },
                error_code=error_code,
                retryable=retryable,
            )
        except Exception:
            raise AurisWorkflowError(
                "Auris Flow audio intelligence execution and completion callback failed"
            ) from None
        if provider_unconfigured:
            raise AurisWorkflowError(
                "Auris Flow audio intelligence provider is not configured"
            ) from None
        raise AurisWorkflowError("Auris Flow audio intelligence execution failed") from None
    except Exception:
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                result_ref=execution_evidence,
                metrics={
                    "input_integrity_verified": 1,
                    "provider_response_validated": 0,
                    "domain_outputs_materialized": 0,
                },
                error_code="AUDIO_PROVIDER_EXECUTION_FAILED",
                retryable=False,
            )
        except Exception:
            raise AurisWorkflowError(
                "Auris Flow audio intelligence execution and completion callback failed"
            ) from None
        raise AurisWorkflowError("Auris Flow audio intelligence execution failed") from None

    result_ref = {
        **result_receipt.public_mapping(),
        "execution_contract": envelope.execution_contract,
        "execution_envelope_sha256": envelope.sha256,
        "input_integrity_manifest_sha256": canonical_sha256(manifest),
        "inference_binding_sha256": hashlib.sha256(
            (f"{envelope.inference.provider}\n{envelope.inference.model}").encode()
        ).hexdigest(),
        "requested_capabilities": list(envelope.capabilities),
    }
    internal_result_ref = {
        **result_ref,
        "storage_objects": result_receipt.internal_callback_mapping()["storage_objects"],
    }
    try:
        callback.post(
            scope,
            dagster_run_id=dagster_run_id,
            status="success",
            result_ref=internal_result_ref,
            metrics={
                "input_integrity_verified": 1,
                "provider_response_validated": 1,
                "domain_outputs_materialized": 1,
            },
        )
    except Exception:
        raise AurisWorkflowError("Auris Flow audio result completion callback failed") from None
    return result_ref
