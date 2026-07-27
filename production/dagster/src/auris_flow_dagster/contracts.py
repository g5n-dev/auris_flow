from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

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
AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
AUDIO_IMPORT_EVENT_TYPE = "task_run.requested"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BUCKET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,254}$")
_MAPPING_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")
_QUERY_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$")
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
_AUDIO_IMPORT_ENVELOPE_FIELDS = frozenset(
    {
        "schema_version",
        "execution_contract",
        "tenant_id",
        "project_id",
        "trace_id",
        "root_trace_id",
        "run_id",
        "dispatch_idempotency_key",
        "outbox_fencing_token",
        "deadline_at",
        "import_batch_id",
        "connector",
        "target",
    }
)
_AUDIO_IMPORT_CONNECTOR_FIELDS = frozenset(
    {
        "connector_id",
        "connector_version",
        "platform_connection_id",
        "platform_scope",
        "source_type",
        "base_url",
        "request_path",
        "credential_ref",
        "pagination",
        "field_mapping",
        "cursor_policy",
    }
)
_AUDIO_IMPORT_PLATFORM_SCOPE_FIELDS = frozenset({"tenant_ref", "store_refs"})
_AUDIO_IMPORT_PAGINATION_FIELDS = frozenset(
    {"mode", "page_size", "cursor_param", "next_cursor_path"}
)
_AUDIO_IMPORT_MAPPING_REQUIRED = frozenset({"external_record_id", "audio_url", "started_at"})
_AUDIO_IMPORT_MAPPING_OPTIONAL = frozenset({"duration_ms", "store_ref", "agent_ref", "device_ref"})
_AUDIO_IMPORT_CURSOR_REQUIRED = frozenset({"field", "initial_window_start"})
_AUDIO_IMPORT_CURSOR_OPTIONAL = frozenset({"cursor_value"})
_AUDIO_IMPORT_TARGET_FIELDS = frozenset(
    {
        "storage_provider",
        "bucket",
        "object_prefix",
        "target_asset_key",
        "dedupe_policy",
    }
)


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


@dataclass(frozen=True)
class AudioImportPlatformScope:
    tenant_ref: str
    store_refs: tuple[str, ...]


@dataclass(frozen=True)
class AudioImportPagination:
    mode: str
    page_size: int
    cursor_param: str
    next_cursor_path: str


@dataclass(frozen=True)
class AudioImportCursorPolicy:
    field: str
    initial_window_start: datetime
    cursor_value: str | None = None


@dataclass(frozen=True)
class AudioImportConnectorSnapshot:
    connector_id: str
    connector_version: str
    platform_connection_id: str
    platform_scope: AudioImportPlatformScope
    source_type: str
    base_url: str
    request_path: str
    credential_ref: str = field(repr=False)
    pagination: AudioImportPagination
    field_mapping: Mapping[str, str]
    cursor_policy: AudioImportCursorPolicy


@dataclass(frozen=True)
class AudioImportTarget:
    storage_provider: str
    bucket: str
    object_prefix: str
    target_asset_key: str
    dedupe_policy: str


@dataclass(frozen=True)
class AudioImportEnvelope:
    schema_version: str
    execution_contract: str
    tenant_id: str
    project_id: str
    trace_id: str
    root_trace_id: str
    run_id: str
    dispatch_idempotency_key: str
    outbox_fencing_token: str
    deadline_at: datetime
    import_batch_id: str
    connector: AudioImportConnectorSnapshot
    target: AudioImportTarget

    def as_mapping(self) -> dict[str, Any]:
        cursor_policy: dict[str, Any] = {
            "field": self.connector.cursor_policy.field,
            "initial_window_start": (
                self.connector.cursor_policy.initial_window_start.astimezone(UTC).isoformat()
            ),
        }
        if self.connector.cursor_policy.cursor_value is not None:
            cursor_policy["cursor_value"] = self.connector.cursor_policy.cursor_value
        return {
            "schema_version": self.schema_version,
            "execution_contract": self.execution_contract,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "root_trace_id": self.root_trace_id,
            "run_id": self.run_id,
            "dispatch_idempotency_key": self.dispatch_idempotency_key,
            "outbox_fencing_token": self.outbox_fencing_token,
            "deadline_at": self.deadline_at.astimezone(UTC).isoformat(),
            "import_batch_id": self.import_batch_id,
            "connector": {
                "connector_id": self.connector.connector_id,
                "connector_version": self.connector.connector_version,
                "platform_connection_id": self.connector.platform_connection_id,
                "platform_scope": {
                    "tenant_ref": self.connector.platform_scope.tenant_ref,
                    "store_refs": list(self.connector.platform_scope.store_refs),
                },
                "source_type": self.connector.source_type,
                "base_url": self.connector.base_url,
                "request_path": self.connector.request_path,
                "credential_ref": self.connector.credential_ref,
                "pagination": {
                    "mode": self.connector.pagination.mode,
                    "page_size": self.connector.pagination.page_size,
                    "cursor_param": self.connector.pagination.cursor_param,
                    "next_cursor_path": self.connector.pagination.next_cursor_path,
                },
                "field_mapping": dict(self.connector.field_mapping),
                "cursor_policy": cursor_policy,
            },
            "target": {
                "storage_provider": self.target.storage_provider,
                "bucket": self.target.bucket,
                "object_prefix": self.target.object_prefix,
                "target_asset_key": self.target.target_asset_key,
                "dedupe_policy": self.target.dedupe_policy,
            },
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
    for field_name, expected in bound_values.items():
        value = _envelope_text(values, field_name)
        if value != expected:
            raise AurisContractError(
                f"execution_envelope.{field_name} does not match auris_context"
            )
        normalized_bound[field_name] = value

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


def _strict_mapping_subset(
    raw: object,
    *,
    name: str,
    required: frozenset[str],
    optional: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise AurisContractError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in raw):
        raise AurisContractError(f"{name} contains invalid field names")
    keys = set(raw)
    missing = sorted(required - keys)
    extra = sorted(keys - required - optional)
    if missing:
        raise AurisContractError(f"{name} is missing required fields: {', '.join(missing)}")
    if extra:
        raise AurisContractError(f"{name} contains unexpected fields: {', '.join(extra)}")
    return raw


def _import_text(
    raw: Mapping[str, Any],
    field_name: str,
    *,
    location: str,
    maximum: int = 256,
    pattern: re.Pattern[str] | None = _IDENTIFIER,
) -> str:
    value = raw.get(field_name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.strip()) > maximum
        or any(ord(character) < 0x21 for character in value.strip())
    ):
        raise AurisContractError(f"{location}.{field_name} is invalid")
    normalized = value.strip()
    if pattern is not None and not pattern.fullmatch(normalized):
        raise AurisContractError(f"{location}.{field_name} is invalid")
    return normalized


def _import_datetime(
    raw: Mapping[str, Any],
    field_name: str,
    *,
    location: str,
) -> datetime:
    raw_value = _import_text(
        raw,
        field_name,
        location=location,
        maximum=64,
        pattern=None,
    )
    try:
        value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AurisContractError(f"{location}.{field_name} is invalid") from exc
    if value.tzinfo is None:
        raise AurisContractError(f"{location}.{field_name} must include a timezone")
    return value.astimezone(UTC)


def validate_audio_import_envelope(
    raw: object,
    *,
    auris_context: Mapping[str, Any] | AurisRunContext,
    now: datetime | None = None,
) -> AudioImportEnvelope:
    """Validate the server-built platform-audio import contract.

    The contract deliberately accepts neither a job name nor arbitrary run config. The BFF
    chooses the fixed job from ``execution_contract`` and freezes every source/target binding.
    """

    values = _strict_mapping(
        raw,
        name="execution_envelope",
        fields=_AUDIO_IMPORT_ENVELOPE_FIELDS,
    )
    scope = (
        auris_context
        if isinstance(auris_context, AurisRunContext)
        else validate_auris_context(auris_context)
    )
    if scope.event_type != AUDIO_IMPORT_EVENT_TYPE:
        raise AurisContractError("auris_context.event_type must be task_run.requested")
    schema_version = _import_text(
        values,
        "schema_version",
        location="execution_envelope",
        maximum=64,
    )
    if schema_version != AUDIO_EXECUTION_ENVELOPE_SCHEMA:
        raise AurisContractError("execution_envelope.schema_version is invalid")
    execution_contract = _import_text(
        values,
        "execution_contract",
        location="execution_envelope",
        maximum=128,
    )
    if execution_contract != AUDIO_IMPORT_EXECUTION_CONTRACT:
        raise AurisContractError("execution_envelope.execution_contract is invalid")

    expected_bindings = {
        "tenant_id": scope.tenant_id,
        "project_id": scope.project_id,
        "trace_id": scope.trace_id,
        "run_id": scope.run_id,
        "dispatch_idempotency_key": scope.dispatch_idempotency_key,
        "outbox_fencing_token": scope.outbox_fencing_token,
    }
    normalized_bindings: dict[str, str] = {}
    for field_name, expected in expected_bindings.items():
        normalized = _import_text(
            values,
            field_name,
            location="execution_envelope",
        )
        if normalized != expected:
            raise AurisContractError(
                f"execution_envelope.{field_name} does not match auris_context"
            )
        normalized_bindings[field_name] = normalized

    deadline_at = _import_datetime(
        values,
        "deadline_at",
        location="execution_envelope",
    )
    if deadline_at <= (now or datetime.now(UTC)).astimezone(UTC):
        raise AurisContractError("execution_envelope deadline is expired")

    connector_values = _strict_mapping(
        values.get("connector"),
        name="execution_envelope.connector",
        fields=_AUDIO_IMPORT_CONNECTOR_FIELDS,
    )
    platform_scope_values = _strict_mapping(
        connector_values.get("platform_scope"),
        name="execution_envelope.connector.platform_scope",
        fields=_AUDIO_IMPORT_PLATFORM_SCOPE_FIELDS,
    )
    tenant_ref = _import_text(
        platform_scope_values,
        "tenant_ref",
        location="execution_envelope.connector.platform_scope",
        maximum=256,
    )
    raw_store_refs = platform_scope_values.get("store_refs")
    if (
        not isinstance(raw_store_refs, list)
        or len(raw_store_refs) > 100
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 256
            or not _IDENTIFIER.fullmatch(value.strip())
            for value in raw_store_refs
        )
        or len({value.strip() for value in raw_store_refs}) != len(raw_store_refs)
    ):
        raise AurisContractError(
            "execution_envelope.connector.platform_scope.store_refs is invalid"
        )
    store_refs = tuple(value.strip() for value in raw_store_refs)

    source_type = _import_text(
        connector_values,
        "source_type",
        location="execution_envelope.connector",
        maximum=64,
    )
    if source_type != "platform_audio_url_api":
        raise AurisContractError("execution_envelope.connector.source_type is invalid")

    base_url = _import_text(
        connector_values,
        "base_url",
        location="execution_envelope.connector",
        maximum=2_048,
        pattern=None,
    ).rstrip("/")
    try:
        parsed_base_url = urlsplit(base_url)
        base_url_port = parsed_base_url.port
    except ValueError as exc:
        raise AurisContractError("execution_envelope.connector.base_url is invalid") from exc
    if (
        parsed_base_url.scheme != "https"
        or not parsed_base_url.hostname
        or parsed_base_url.username
        or parsed_base_url.password
        or base_url_port is not None
        and not 1 <= base_url_port <= 65_535
        or parsed_base_url.path not in {"", "/"}
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        raise AurisContractError("execution_envelope.connector.base_url is invalid")

    request_path = _import_text(
        connector_values,
        "request_path",
        location="execution_envelope.connector",
        maximum=1_024,
        pattern=None,
    )
    parsed_request_path = urlsplit(request_path)
    request_parts = request_path.split("/")
    if (
        not request_path.startswith("/")
        or request_path.startswith("//")
        or parsed_request_path.scheme
        or parsed_request_path.netloc
        or parsed_request_path.query
        or parsed_request_path.fragment
        or "\\" in request_path
        or any(part in {".", ".."} for part in request_parts)
    ):
        raise AurisContractError("execution_envelope.connector.request_path is invalid")

    pagination_values = _strict_mapping(
        connector_values.get("pagination"),
        name="execution_envelope.connector.pagination",
        fields=_AUDIO_IMPORT_PAGINATION_FIELDS,
    )
    pagination_mode = _import_text(
        pagination_values,
        "mode",
        location="execution_envelope.connector.pagination",
        maximum=32,
    )
    if pagination_mode != "cursor":
        raise AurisContractError("execution_envelope.connector.pagination.mode is invalid")
    raw_page_size = pagination_values.get("page_size")
    if (
        isinstance(raw_page_size, bool)
        or not isinstance(raw_page_size, int)
        or not 1 <= raw_page_size <= 250
    ):
        raise AurisContractError("execution_envelope.connector.pagination.page_size is invalid")
    cursor_param = _import_text(
        pagination_values,
        "cursor_param",
        location="execution_envelope.connector.pagination",
        maximum=128,
        pattern=_QUERY_PARAMETER,
    )
    next_cursor_path = _import_text(
        pagination_values,
        "next_cursor_path",
        location="execution_envelope.connector.pagination",
        maximum=256,
        pattern=_MAPPING_PATH,
    )

    field_mapping_values = _strict_mapping_subset(
        connector_values.get("field_mapping"),
        name="execution_envelope.connector.field_mapping",
        required=_AUDIO_IMPORT_MAPPING_REQUIRED,
        optional=_AUDIO_IMPORT_MAPPING_OPTIONAL,
    )
    field_mapping: dict[str, str] = {}
    for field_name, raw_mapping_path in field_mapping_values.items():
        if (
            not isinstance(raw_mapping_path, str)
            or len(raw_mapping_path) > 256
            or not _MAPPING_PATH.fullmatch(raw_mapping_path)
        ):
            raise AurisContractError(
                f"execution_envelope.connector.field_mapping.{field_name} is invalid"
            )
        field_mapping[field_name] = raw_mapping_path
    if store_refs and "store_ref" not in field_mapping:
        raise AurisContractError(
            "execution_envelope.connector.field_mapping.store_ref is required "
            "for a bounded store scope"
        )

    cursor_values = _strict_mapping_subset(
        connector_values.get("cursor_policy"),
        name="execution_envelope.connector.cursor_policy",
        required=_AUDIO_IMPORT_CURSOR_REQUIRED,
        optional=_AUDIO_IMPORT_CURSOR_OPTIONAL,
    )
    cursor_field = _import_text(
        cursor_values,
        "field",
        location="execution_envelope.connector.cursor_policy",
        maximum=256,
        pattern=_MAPPING_PATH,
    )
    initial_window_start = _import_datetime(
        cursor_values,
        "initial_window_start",
        location="execution_envelope.connector.cursor_policy",
    )
    raw_cursor_value = cursor_values.get("cursor_value")
    cursor_value: str | None
    if raw_cursor_value is None:
        cursor_value = None
    elif (
        not isinstance(raw_cursor_value, str)
        or not raw_cursor_value
        or len(raw_cursor_value) > 1_024
        or any(ord(character) < 0x20 for character in raw_cursor_value)
    ):
        raise AurisContractError(
            "execution_envelope.connector.cursor_policy.cursor_value is invalid"
        )
    else:
        cursor_value = raw_cursor_value

    target_values = _strict_mapping(
        values.get("target"),
        name="execution_envelope.target",
        fields=_AUDIO_IMPORT_TARGET_FIELDS,
    )
    storage_provider = _import_text(
        target_values,
        "storage_provider",
        location="execution_envelope.target",
        maximum=32,
    )
    if storage_provider not in {"minio", "s3"}:
        raise AurisContractError("execution_envelope.target.storage_provider is invalid")
    target_bucket = _import_text(
        target_values,
        "bucket",
        location="execution_envelope.target",
        maximum=255,
        pattern=_BUCKET,
    )
    object_prefix = _import_text(
        target_values,
        "object_prefix",
        location="execution_envelope.target",
        maximum=1_024,
        pattern=None,
    )
    expected_prefix = (
        f"tenants/{scope.tenant_id}/projects/{scope.project_id}/runs/{scope.run_id}/audio-import/"
    )
    if object_prefix != expected_prefix:
        raise AurisContractError("execution_envelope.target.object_prefix is invalid")
    target_asset_key = _import_text(
        target_values,
        "target_asset_key",
        location="execution_envelope.target",
        maximum=512,
    )
    dedupe_policy = _import_text(
        target_values,
        "dedupe_policy",
        location="execution_envelope.target",
        maximum=64,
    )
    if dedupe_policy != "external_id_checksum":
        raise AurisContractError("execution_envelope.target.dedupe_policy is invalid")

    return AudioImportEnvelope(
        schema_version=schema_version,
        execution_contract=execution_contract,
        tenant_id=normalized_bindings["tenant_id"],
        project_id=normalized_bindings["project_id"],
        trace_id=normalized_bindings["trace_id"],
        root_trace_id=_import_text(
            values,
            "root_trace_id",
            location="execution_envelope",
        ),
        run_id=normalized_bindings["run_id"],
        dispatch_idempotency_key=normalized_bindings["dispatch_idempotency_key"],
        outbox_fencing_token=normalized_bindings["outbox_fencing_token"],
        deadline_at=deadline_at,
        import_batch_id=_import_text(
            values,
            "import_batch_id",
            location="execution_envelope",
        ),
        connector=AudioImportConnectorSnapshot(
            connector_id=_import_text(
                connector_values,
                "connector_id",
                location="execution_envelope.connector",
            ),
            connector_version=_import_text(
                connector_values,
                "connector_version",
                location="execution_envelope.connector",
            ),
            platform_connection_id=_import_text(
                connector_values,
                "platform_connection_id",
                location="execution_envelope.connector",
            ),
            platform_scope=AudioImportPlatformScope(
                tenant_ref=tenant_ref,
                store_refs=store_refs,
            ),
            source_type=source_type,
            base_url=base_url,
            request_path=request_path,
            credential_ref=_import_text(
                connector_values,
                "credential_ref",
                location="execution_envelope.connector",
                maximum=512,
            ),
            pagination=AudioImportPagination(
                mode=pagination_mode,
                page_size=raw_page_size,
                cursor_param=cursor_param,
                next_cursor_path=next_cursor_path,
            ),
            field_mapping=field_mapping,
            cursor_policy=AudioImportCursorPolicy(
                field=cursor_field,
                initial_window_start=initial_window_start,
                cursor_value=cursor_value,
            ),
        ),
        target=AudioImportTarget(
            storage_provider=storage_provider,
            bucket=target_bucket,
            object_prefix=object_prefix,
            target_asset_key=target_asset_key,
            dedupe_policy=dedupe_policy,
        ),
    )
