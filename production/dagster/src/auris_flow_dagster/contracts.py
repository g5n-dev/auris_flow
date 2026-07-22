from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_RESOURCE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_FENCING_TOKEN = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
_OTEL_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_OTEL_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")
_OTEL_TRACE_FLAGS = frozenset({"00", "01"})
_REQUIRED_FIELDS = (
    "tenant_id",
    "project_id",
    "trace_id",
    "run_id",
    "dispatch_idempotency_key",
    "outbox_fencing_token",
)
AUDIO_INTELLIGENCE_EXECUTION_CONTRACT = "auris-flow-audio-intelligence-v1"
AUDIO_INTELLIGENCE_EVENT_TYPE = "audio_intelligence.requested"
AUDIO_EXECUTION_ENVELOPE_SCHEMA = "auris-flow-execution-envelope-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,254}$")
_AUDIO_CAPABILITIES = frozenset({"vad", "asr", "diarization", "voiceprint", "quality"})
_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "execution_contract",
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
_INPUT_OBJECT_FIELDS = frozenset(
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
_INFERENCE_FIELDS = frozenset({"provider", "model"})


class AurisContractError(ValueError):
    """A public, secret-free contract validation error."""


@dataclass(frozen=True)
class AurisRunContext:
    tenant_id: str
    project_id: str
    trace_id: str
    run_id: str
    dispatch_idempotency_key: str
    outbox_fencing_token: str
    event_type: str | None = None
    partition_key: str | None = None
    task_version_id: str | None = None
    asset_key: str | None = None
    otel_trace_id: str | None = None
    otel_parent_span_id: str | None = None
    otel_trace_flags: str | None = None

    def public_metadata(self) -> dict[str, str]:
        values = {
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "partition_key": self.partition_key,
            "task_version_id": self.task_version_id,
            "asset_key": self.asset_key,
        }
        return {key: value for key, value in values.items() if value}


@dataclass(frozen=True)
class AudioInputObject:
    storage_object_id: str
    storage_provider: str
    bucket: str
    object_key: str
    version_id: str
    content_sha256: str
    content_length: int
    content_type: str


@dataclass(frozen=True)
class AudioInferenceBinding:
    provider: str
    model: str


@dataclass(frozen=True)
class AudioExecutionEnvelope:
    schema_version: str
    execution_contract: str
    tenant_id: str
    project_id: str
    trace_id: str
    run_id: str
    dispatch_idempotency_key: str
    outbox_fencing_token: str
    deadline_at: datetime
    audio_session_id: str
    recording_id: str
    input_object: AudioInputObject
    inference: AudioInferenceBinding
    capabilities: tuple[str, ...]

    def as_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_contract": self.execution_contract,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "dispatch_idempotency_key": self.dispatch_idempotency_key,
            "outbox_fencing_token": self.outbox_fencing_token,
            "deadline_at": self.deadline_at.astimezone(UTC).isoformat(),
            "audio_session_id": self.audio_session_id,
            "recording_id": self.recording_id,
            "input_object": {
                "storage_object_id": self.input_object.storage_object_id,
                "storage_provider": self.input_object.storage_provider,
                "bucket": self.input_object.bucket,
                "object_key": self.input_object.object_key,
                "version_id": self.input_object.version_id,
                "content_sha256": self.input_object.content_sha256,
                "content_length": self.input_object.content_length,
                "content_type": self.input_object.content_type,
            },
            "inference": {
                "provider": self.inference.provider,
                "model": self.inference.model,
            },
            "capabilities": list(self.capabilities),
        }

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.as_mapping(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def _required_identifier(
    raw: Mapping[str, Any],
    field: str,
    *,
    maximum: int = 256,
    pattern: re.Pattern[str] = _IDENTIFIER,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AurisContractError(f"auris_context.{field} is required")
    normalized = value.strip()
    if len(normalized) > maximum or not pattern.fullmatch(normalized):
        raise AurisContractError(f"auris_context.{field} is invalid")
    return normalized


def _optional_identifier(raw: Mapping[str, Any], field: str, *, maximum: int = 512) -> str | None:
    value = raw.get(field)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AurisContractError(f"auris_context.{field} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or not _IDENTIFIER.fullmatch(normalized):
        raise AurisContractError(f"auris_context.{field} is invalid")
    return normalized


def validate_auris_context(raw: object) -> AurisRunContext:
    if not isinstance(raw, Mapping):
        raise AurisContractError("auris_context must be an object")
    missing = [field for field in _REQUIRED_FIELDS if not raw.get(field)]
    if missing:
        raise AurisContractError(f"auris_context is missing required fields: {', '.join(missing)}")

    fencing_token = _required_identifier(raw, "outbox_fencing_token", maximum=64)
    if not _FENCING_TOKEN.fullmatch(fencing_token):
        raise AurisContractError("auris_context.outbox_fencing_token is invalid")

    trace_id = raw.get("otel_trace_id")
    parent_span_id = raw.get("otel_parent_span_id")
    trace_flags = raw.get("otel_trace_flags")
    otel_values = (trace_id, parent_span_id, trace_flags)
    has_otel_parent = any(
        value is not None and (not isinstance(value, str) or value != "") for value in otel_values
    )
    normalized_trace_id: str | None = None
    normalized_parent_span_id: str | None = None
    normalized_trace_flags: str | None = None
    if has_otel_parent:
        if (
            not isinstance(trace_id, str)
            or not _OTEL_TRACE_ID.fullmatch(trace_id)
            or trace_id == "0" * 32
            or not isinstance(parent_span_id, str)
            or not _OTEL_SPAN_ID.fullmatch(parent_span_id)
            or parent_span_id == "0" * 16
            or not isinstance(trace_flags, str)
            or trace_flags not in _OTEL_TRACE_FLAGS
        ):
            raise AurisContractError("auris_context OpenTelemetry parent is invalid")
        normalized_trace_id = trace_id
        normalized_parent_span_id = parent_span_id
        normalized_trace_flags = trace_flags

    return AurisRunContext(
        tenant_id=_required_identifier(raw, "tenant_id", maximum=128, pattern=_RESOURCE_IDENTIFIER),
        project_id=_required_identifier(
            raw, "project_id", maximum=128, pattern=_RESOURCE_IDENTIFIER
        ),
        trace_id=_required_identifier(raw, "trace_id", maximum=256, pattern=_RESOURCE_IDENTIFIER),
        run_id=_required_identifier(raw, "run_id", maximum=256, pattern=_RESOURCE_IDENTIFIER),
        dispatch_idempotency_key=_required_identifier(
            raw,
            "dispatch_idempotency_key",
            maximum=256,
        ),
        outbox_fencing_token=fencing_token,
        event_type=_optional_identifier(raw, "event_type", maximum=128),
        partition_key=_optional_identifier(raw, "partition_key"),
        task_version_id=_optional_identifier(raw, "task_version_id", maximum=256),
        asset_key=_optional_identifier(raw, "asset_key", maximum=512),
        otel_trace_id=normalized_trace_id,
        otel_parent_span_id=normalized_parent_span_id,
        otel_trace_flags=normalized_trace_flags,
    )


def _strict_mapping(raw: object, *, name: str, fields: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise AurisContractError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in raw):
        raise AurisContractError(f"{name} contains invalid field names")
    keys = set(raw)
    missing = sorted(fields - keys)
    extra = sorted(keys - fields)
    if missing:
        raise AurisContractError(f"{name} is missing required fields: {', '.join(missing)}")
    if extra:
        raise AurisContractError(f"{name} contains unexpected fields: {', '.join(extra)}")
    return raw


def _envelope_text(
    raw: Mapping[str, Any],
    field: str,
    *,
    maximum: int = 256,
    pattern: re.Pattern[str] | None = _IDENTIFIER,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AurisContractError(f"execution_envelope.{field} is required")
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(char) < 0x21 for char in normalized):
        raise AurisContractError(f"execution_envelope.{field} is invalid")
    if pattern is not None and not pattern.fullmatch(normalized):
        raise AurisContractError(f"execution_envelope.{field} is invalid")
    return normalized


def validate_audio_execution_envelope(
    raw: object,
    *,
    auris_context: Mapping[str, Any] | AurisRunContext,
    now: datetime | None = None,
) -> AudioExecutionEnvelope:
    values = _strict_mapping(raw, name="execution_envelope", fields=_ENVELOPE_FIELDS)
    scope = (
        auris_context
        if isinstance(auris_context, AurisRunContext)
        else validate_auris_context(auris_context)
    )
    if scope.event_type != AUDIO_INTELLIGENCE_EVENT_TYPE:
        raise AurisContractError("auris_context.event_type must be audio_intelligence.requested")
    schema_version = _envelope_text(values, "schema_version", maximum=64)
    if schema_version != AUDIO_EXECUTION_ENVELOPE_SCHEMA:
        raise AurisContractError("execution_envelope.schema_version is invalid")
    execution_contract = _envelope_text(values, "execution_contract", maximum=128)
    if execution_contract != AUDIO_INTELLIGENCE_EXECUTION_CONTRACT:
        raise AurisContractError("execution_envelope.execution_contract is invalid")

    bound_values = {
        "tenant_id": scope.tenant_id,
        "project_id": scope.project_id,
        "trace_id": scope.trace_id,
        "run_id": scope.run_id,
        "dispatch_idempotency_key": scope.dispatch_idempotency_key,
        "outbox_fencing_token": scope.outbox_fencing_token,
    }
    normalized_bound: dict[str, str] = {}
    for field, expected in bound_values.items():
        value = _envelope_text(values, field)
        if value != expected:
            raise AurisContractError(f"execution_envelope.{field} does not match auris_context")
        normalized_bound[field] = value

    raw_deadline = _envelope_text(values, "deadline_at", maximum=64, pattern=None)
    try:
        deadline_at = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AurisContractError("execution_envelope.deadline_at is invalid") from exc
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if deadline_at.tzinfo is None:
        raise AurisContractError("execution_envelope.deadline_at must include a timezone")
    deadline_at = deadline_at.astimezone(UTC)
    if deadline_at <= current:
        raise AurisContractError("execution_envelope deadline is expired")

    input_values = _strict_mapping(
        values.get("input_object"),
        name="execution_envelope.input_object",
        fields=_INPUT_OBJECT_FIELDS,
    )
    storage_provider = _envelope_text(
        input_values,
        "storage_provider",
        maximum=32,
    )
    if storage_provider not in {"minio", "s3", "obs", "oss"}:
        raise AurisContractError("execution_envelope.input_object.storage_provider is invalid")
    bucket = _envelope_text(input_values, "bucket", maximum=255, pattern=_BUCKET)
    object_key = _envelope_text(input_values, "object_key", maximum=1024, pattern=None)
    parts = object_key.strip("/").split("/")
    if (
        object_key != object_key.strip("/")
        or parts[:4] != ["tenants", scope.tenant_id, "projects", scope.project_id]
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise AurisContractError("execution_envelope.input_object.object_key is invalid")
    version_id = _envelope_text(
        input_values,
        "version_id",
        maximum=1024,
        pattern=None,
    )
    if version_id.casefold() == "null":
        raise AurisContractError("execution_envelope.input_object.version_id is invalid")
    content_sha256 = _envelope_text(
        input_values,
        "content_sha256",
        maximum=64,
        pattern=_SHA256,
    )
    content_length = input_values.get("content_length")
    if (
        isinstance(content_length, bool)
        or not isinstance(content_length, int)
        or not 44 <= content_length <= 5 * 1024**3
    ):
        raise AurisContractError("execution_envelope.input_object.content_length is invalid")
    content_type = _envelope_text(
        input_values,
        "content_type",
        maximum=128,
        pattern=None,
    )
    if content_type not in {"audio/wav", "audio/x-wav"}:
        raise AurisContractError("execution_envelope.input_object.content_type is invalid")

    inference_values = _strict_mapping(
        values.get("inference"),
        name="execution_envelope.inference",
        fields=_INFERENCE_FIELDS,
    )
    raw_capabilities = values.get("capabilities")
    if (
        not isinstance(raw_capabilities, list)
        or not raw_capabilities
        or len(raw_capabilities) > len(_AUDIO_CAPABILITIES)
        or any(
            not isinstance(capability, str) or capability not in _AUDIO_CAPABILITIES
            for capability in raw_capabilities
        )
        or len(set(raw_capabilities)) != len(raw_capabilities)
    ):
        raise AurisContractError("execution_envelope.capabilities is invalid")

    return AudioExecutionEnvelope(
        schema_version=schema_version,
        execution_contract=execution_contract,
        tenant_id=normalized_bound["tenant_id"],
        project_id=normalized_bound["project_id"],
        trace_id=normalized_bound["trace_id"],
        run_id=normalized_bound["run_id"],
        dispatch_idempotency_key=normalized_bound["dispatch_idempotency_key"],
        outbox_fencing_token=normalized_bound["outbox_fencing_token"],
        deadline_at=deadline_at,
        audio_session_id=_envelope_text(values, "audio_session_id"),
        recording_id=_envelope_text(values, "recording_id"),
        input_object=AudioInputObject(
            storage_object_id=_envelope_text(input_values, "storage_object_id"),
            storage_provider=storage_provider,
            bucket=bucket,
            object_key=object_key,
            version_id=version_id,
            content_sha256=content_sha256,
            content_length=content_length,
            content_type=content_type,
        ),
        inference=AudioInferenceBinding(
            provider=_envelope_text(inference_values, "provider", maximum=128),
            model=_envelope_text(inference_values, "model", maximum=128),
        ),
        capabilities=tuple(raw_capabilities),
    )
