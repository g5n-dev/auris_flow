from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import uuid
from base64 import b64encode
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request

from app.core.callback_signature import (
    CallbackKeyBinding,
    CallbackKeyring,
    CallbackKeyState,
    CallbackSignatureError,
    CallbackSignatureRequest,
    parse_callback_keyring,
    sign_callback,
)
from app.core.embeddings import (
    EMBEDDING_DOCUMENT_SOURCE_PRECEDENCE,
    EMBEDDING_SPACE_FINGERPRINT_FIELD,
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingResponseError,
    build_embedding_provider,
    embedding_space_v1_fingerprint,
)
from app.core.http_transport import open_url_no_redirect as urlopen
from app.core.observability import (
    current_trace_context,
    internal_span,
    record_safe_http_status,
    safe_http_client_span,
)
from app.core.redaction import redact_structured_value
from app.core.runtime_guards import failure_injection_enabled


@dataclass(frozen=True)
class DispatchResult:
    adapter: str
    operation: str
    status: str = "success"
    details: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = True
    retry_after_seconds: int | None = None


def _stable_receipt_id(prefix: str, payload: dict[str, Any], *keys: str) -> str:
    parts = {key: payload.get(key) for key in keys if payload.get(key) is not None}
    if not parts:
        parts = {
            "run_id": payload.get("run_id"),
            "target": payload.get("target"),
            "aggregate_id": payload.get("aggregate_id"),
        }
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _structured_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda _value: "<non-json-value>",
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_uuid(payload: dict[str, Any], *keys: str) -> str:
    parts = {key: payload.get(key) for key in keys if payload.get(key) is not None}
    raw = json.dumps(parts or payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"auris-flow:{raw}"))


def _sigv4_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sigv4_signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret_key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _oss_v4_signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    date_key = hmac.new(
        f"aliyun_v4{secret_key}".encode(), date_stamp.encode(), hashlib.sha256
    ).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, b"oss", hashlib.sha256).digest()
    return hmac.new(service_key, b"aliyun_v4_request", hashlib.sha256).digest()


SUPPORTED_OBJECT_STORAGE_PROVIDERS = frozenset({"minio", "s3", "obs", "oss"})


def _provider_setting(provider: str, name: str) -> str | None:
    value = os.environ.get(f"OBJECT_STORAGE_{provider.upper()}_{name}")
    return value.strip() if value and value.strip() else None


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _default_addressing_style(provider: str) -> str:
    return "path" if provider == "minio" else "virtual"


def _default_signature_mode(provider: str) -> str:
    if provider == "obs":
        return "obs"
    if provider == "oss":
        return "ossv4"
    return "s3v4"


def _maybe_failure(adapter: str, operation: str, payload: dict[str, Any]) -> DispatchResult | None:
    if not failure_injection_enabled():
        return None
    if not (
        payload.get("simulate_adapter_failure")
        or payload.get("force_adapter_error")
        or payload.get("adapter_error_code")
    ):
        return None
    retry_after = payload.get("retry_after_seconds")
    retry_after_seconds = int(retry_after) if retry_after is not None else None
    retryable = payload.get("adapter_retryable", True)
    return DispatchResult(
        adapter=adapter,
        operation=operation,
        status="failed",
        details={
            "target": payload.get("target"),
            "aggregate_id": payload.get("aggregate_id") or payload.get("run_id"),
        },
        error_code=str(payload.get("adapter_error_code", "ADAPTER_DISPATCH_FAILED")),
        error_message=str(payload.get("adapter_error_message", "adapter dispatch failed")),
        retryable=bool(retryable),
        retry_after_seconds=retry_after_seconds,
    )


def _reconciliation_failure(
    adapter: str,
    operation: str,
    error_code: str,
    error_message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> DispatchResult:
    return DispatchResult(
        adapter=adapter,
        operation=operation,
        status="failed",
        details={"reconciled": False, **(details or {})},
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
        retry_after_seconds=10 if retryable else None,
    )


def _reconciliation_not_found(
    adapter: str,
    operation: str,
    *,
    details: dict[str, Any] | None = None,
) -> DispatchResult:
    return _reconciliation_failure(
        adapter,
        operation,
        "REMOTE_RECEIPT_NOT_FOUND",
        "remote side effect could not be proven; automatic redispatch is disabled",
        retryable=False,
        details={"safe_to_redispatch": False, **(details or {})},
    )


def _qdrant_payload(payload: dict[str, Any]) -> dict[str, Any]:
    qdrant_payload = payload.get("qdrant_payload")
    if isinstance(qdrant_payload, dict):
        scoped_payload = dict(qdrant_payload)
        for scope_key in ("tenant_id", "project_id", "trace_id"):
            scope_value = payload.get(scope_key)
            if scope_value is not None and str(scope_value):
                scoped_payload[scope_key] = scope_value
        return scoped_payload
    return {
        "tenant_id": payload.get("tenant_id"),
        "project_id": payload.get("project_id"),
        "trace_id": payload.get("trace_id"),
        "collection": payload.get("vector_collection") or payload.get("qdrant_collection"),
        "knowledge_index_id": payload.get("knowledge_index_id"),
        "knowledge_source_id": payload.get("knowledge_source_id") or payload.get("source_id"),
        "source_id": payload.get("source_id") or payload.get("knowledge_source_id"),
        "source_type": payload.get("source_type"),
        "asset_key": payload.get("asset_key"),
        "version": payload.get("version"),
        "business_ref": payload.get("business_ref"),
    }


QDRANT_AUTHORIZED_POINT_IDS_FIELD = "_authorized_point_ids"
QDRANT_AUTHORIZED_POINT_IDS_LIMIT = 1024
QDRANT_DISTANCE = "Cosine"


def normalize_real_qdrant_point_id(value: object) -> str | None:
    """Accept only the canonical UUID form emitted by the real adapter."""

    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def validate_real_qdrant_authorized_point_ids(qdrant_payload: dict[str, Any]) -> list[str]:
    if QDRANT_AUTHORIZED_POINT_IDS_FIELD not in qdrant_payload:
        raise ValueError("Qdrant authorized point ids are required")
    raw_point_ids = qdrant_payload[QDRANT_AUTHORIZED_POINT_IDS_FIELD]
    if not isinstance(raw_point_ids, list):
        raise ValueError("Qdrant authorized point ids are invalid")
    if len(raw_point_ids) > QDRANT_AUTHORIZED_POINT_IDS_LIMIT:
        raise ValueError("Qdrant authorized point ids exceed limit")
    normalized_point_ids: set[str] = set()
    for point_id in raw_point_ids:
        normalized = normalize_real_qdrant_point_id(point_id)
        if normalized is None:
            raise ValueError("Qdrant authorized point ids are invalid")
        normalized_point_ids.add(normalized)
    return sorted(normalized_point_ids)


def configured_real_qdrant_embedding_space_fingerprint() -> str:
    try:
        vector_size = int(os.environ.get("QDRANT_VECTOR_SIZE", "8"))
    except ValueError:
        raise EmbeddingConfigurationError("QDRANT_VECTOR_SIZE must be an integer") from None
    provider = build_embedding_provider(dimension=vector_size)
    return embedding_space_v1_fingerprint(provider, distance=QDRANT_DISTANCE)


def real_qdrant_filter_reference(
    qdrant_payload: dict[str, Any],
    *,
    authorized_point_count: int,
) -> dict[str, Any]:
    return {
        "tenant_id": qdrant_payload.get("tenant_id"),
        "project_id": qdrant_payload.get("project_id"),
        "knowledge_index_id": qdrant_payload.get("knowledge_index_id"),
        EMBEDDING_SPACE_FINGERPRINT_FIELD: qdrant_payload.get(EMBEDDING_SPACE_FINGERPRINT_FIELD),
        "authorized_point_count": authorized_point_count,
    }


def _validate_qdrant_payload(qdrant_payload: dict[str, Any]) -> list[str]:
    required_fields = (
        "tenant_id",
        "project_id",
        "trace_id",
        "collection",
        "knowledge_source_id",
        "source_id",
        "source_type",
        "asset_key",
        "version",
        "business_ref",
    )
    return [field_name for field_name in required_fields if not qdrant_payload.get(field_name)]


def _invalid_qdrant_payload(missing_fields: list[str]) -> DispatchResult:
    return DispatchResult(
        adapter="qdrant",
        operation="upsert_payload",
        status="failed",
        details={"missing_fields": missing_fields},
        error_code="QDRANT_PAYLOAD_INVALID",
        error_message=f"Qdrant payload missing required fields: {', '.join(missing_fields)}",
        retryable=False,
    )


def _embedding_document_text(
    payload: dict[str, Any],
    qdrant_payload: dict[str, Any],
    *,
    provider: EmbeddingProvider,
) -> str:
    for source in (payload, qdrant_payload):
        for key in EMBEDDING_DOCUMENT_SOURCE_PRECEDENCE:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if provider.is_semantic:
        raise EmbeddingResponseError(
            "semantic embedding input is missing; provide embedding_text or document content"
        )
    return json.dumps(
        qdrant_payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _object_artifact_body(payload: dict[str, Any], storage_object_id: str) -> tuple[bytes, str]:
    if payload.get("object_type") == "insight_report":
        document = payload.get("report_document")
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != "auris.insight-report.v2"
            or document.get("artifact_state") != "materialized"
        ):
            raise ValueError("insight report requires a governed materialized report_document")
        state = "materialized"
    else:
        document = {
            "storage_object_id": storage_object_id,
            "run_id": payload.get("run_id"),
            "trace_id": payload.get("trace_id"),
            "tenant_id": payload.get("tenant_id"),
            "project_id": payload.get("project_id"),
            "asset_ref": payload.get("asset_ref"),
            "asset_key": payload.get("asset_key"),
            "target": payload.get("target"),
            "artifact_state": "reserved",
        }
        state = "reserved"
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        state,
    )


class DagsterClient(Protocol):
    def submit_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        """Submit a Dagster-compatible run request."""

    def reconcile_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        """Find a previously submitted run without launching another run."""

    def get_run_status(self, external_run_id: str) -> DispatchResult:
        """Read the authoritative Dagster status for a submitted run."""

    def cancel_run(self, external_run_id: str) -> DispatchResult:
        """Safely terminate a non-terminal Dagster run."""


class ObjectStorageClient(Protocol):
    def reserve_object(self, payload: dict[str, Any]) -> DispatchResult:
        """Reserve or materialize an object-storage reference."""

    def get_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """Read an object, optionally using an HTTP Range header."""

    def reconcile_object(self, payload: dict[str, Any]) -> DispatchResult:
        """Verify a deterministic object reference without writing it again."""


class QdrantIndexClient(Protocol):
    def upsert_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        """Upsert vector-index payload metadata."""

    def search_index_payload(
        self, qdrant_payload: dict[str, Any], *, query: str, top_k: int
    ) -> dict[str, Any]:
        """Search vector-index payload metadata."""

    def reconcile_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        """Find a deterministic point without upserting it again."""


class ExternalCallbackClient(Protocol):
    def send_signed_callback(self, payload: dict[str, Any]) -> DispatchResult:
        """Send an external callback with replay protection metadata."""

    def reconcile_callback(self, payload: dict[str, Any]) -> DispatchResult:
        """Find an acknowledgement receipt without resending the callback."""


class LocalDagsterClient:
    def submit_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("dagster", "run_request", payload)
        if failure:
            return failure
        run_key = str(
            payload.get("dispatch_idempotency_key")
            or payload.get("run_key")
            or payload.get("run_id")
            or _stable_receipt_id("dg_key", payload)
        )
        external_run_id = _stable_receipt_id(
            "dg_run",
            {**payload, "effective_run_key": run_key},
            "effective_run_key",
        )
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "external_run_id": external_run_id,
                "run_request_id": _stable_receipt_id(
                    "dg_req", {**payload, "effective_run_key": run_key}, "effective_run_key"
                ),
                "job_name": payload.get("job_name") or payload.get("task_version_id"),
                "partition_key": payload.get("partition_key"),
                "run_key": run_key,
                "request_run_key": payload.get("run_key"),
                "fencing_token": payload.get("outbox_fencing_token"),
            },
        )

    def reconcile_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        dispatched = self.submit_run_request(payload)
        return DispatchResult(
            adapter=dispatched.adapter,
            operation="reconcile_run_request",
            status=dispatched.status,
            details={**dispatched.details, "reconciled": True, "mode": "local"},
            error_code=dispatched.error_code,
            error_message=dispatched.error_message,
            retryable=dispatched.retryable,
            retry_after_seconds=dispatched.retry_after_seconds,
        )

    def get_run_status(self, external_run_id: str) -> DispatchResult:
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details={
                "mode": "local",
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "SUCCESS",
                "can_terminate": False,
            },
        )

    def cancel_run(self, external_run_id: str) -> DispatchResult:
        return DispatchResult(
            adapter="dagster",
            operation="cancel_run",
            details={
                "mode": "local",
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "dagster_status": "CANCELED",
            },
        )


MAX_DAGSTER_GRAPHQL_RESPONSE_BYTES = 1_048_576
DAGSTER_RECONCILIATION_ABSENCE_PROOF = "dagster-exact-dispatch-tag-absent-v1"
AUDIO_INTELLIGENCE_EXECUTION_CONTRACT = "auris-flow-audio-intelligence-v1"
AUDIO_INTELLIGENCE_EXECUTION_ENVELOPE_SCHEMA = "auris-flow-execution-envelope-v1"
AUDIO_INTELLIGENCE_JOB_NAME = "auris_flow_audio_intelligence_v1"
DAGSTER_RUN_REQUEST_EVENT_TYPES = frozenset(
    {
        "task_run.requested",
        "audio_intelligence.requested",
        "backfill.requested",
        "asset_check.retry_requested",
        "conversation_boundary.sync_requested",
        "platform_sync.requested",
        "eval_run.requested",
        "agent_run.requested",
        "insight_metric_aggregation.requested",
        "hotword_analysis.requested",
        "hotword_pack_version.build-requested",
        "hotword_pack_version.eval-requested",
        "hotword_pack_version.publish-requested",
        "release_deployment.command-requested",
        "provider_test.requested",
    }
)
_DAGSTER_CONTROL_PLANE_EVENT_TYPES = DAGSTER_RUN_REQUEST_EVENT_TYPES - {
    "audio_intelligence.requested"
}
_DAGSTER_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_DAGSTER_BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,254}$")
_DAGSTER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUDIO_CAPABILITIES = frozenset({"vad", "asr", "diarization", "voiceprint", "quality"})


class DagsterExecutionContractError(ValueError):
    """Sanitized, non-retryable failure before a Dagster request is emitted."""

    def __init__(self, field: str) -> None:
        super().__init__(f"invalid Dagster execution contract field: {field}")
        self.field = field


def _required_dagster_text(
    source: dict[str, Any],
    field: str,
    *,
    maximum: int = 256,
    pattern: re.Pattern[str] = _DAGSTER_IDENTIFIER_PATTERN,
) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DagsterExecutionContractError(field)
    normalized = value.strip()
    if len(normalized) > maximum or not pattern.fullmatch(normalized):
        raise DagsterExecutionContractError(field)
    return normalized


def _required_bounded_printable_text(source: dict[str, Any], field: str, *, maximum: int) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise DagsterExecutionContractError(field)
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(char) < 0x21 for char in normalized):
        raise DagsterExecutionContractError(field)
    return normalized


def _audio_execution_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    event_type = _required_dagster_text(payload, "event_type", maximum=128)
    if event_type != "audio_intelligence.requested":
        raise DagsterExecutionContractError("event_type")
    contract = _required_dagster_text(payload, "execution_contract", maximum=128)
    if contract != AUDIO_INTELLIGENCE_EXECUTION_CONTRACT:
        raise DagsterExecutionContractError("execution_contract")

    tenant_id = _required_dagster_text(payload, "tenant_id", maximum=128)
    project_id = _required_dagster_text(payload, "project_id", maximum=128)
    deadline_at = _required_bounded_printable_text(payload, "execution_deadline_at", maximum=64)
    try:
        parsed_deadline = datetime.fromisoformat(deadline_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DagsterExecutionContractError("execution_deadline_at") from exc
    if parsed_deadline.tzinfo is None or parsed_deadline.astimezone(UTC) <= datetime.now(UTC):
        raise DagsterExecutionContractError("execution_deadline_at")
    normalized_deadline = parsed_deadline.astimezone(UTC).isoformat()

    raw_input = payload.get("input_object")
    if not isinstance(raw_input, dict):
        raise DagsterExecutionContractError("input_object")
    storage_provider = _required_dagster_text(raw_input, "storage_provider", maximum=32)
    if storage_provider not in SUPPORTED_OBJECT_STORAGE_PROVIDERS:
        raise DagsterExecutionContractError("input_object.storage_provider")
    bucket = _required_dagster_text(
        raw_input,
        "bucket",
        maximum=255,
        pattern=_DAGSTER_BUCKET_PATTERN,
    )
    object_key = _required_bounded_printable_text(raw_input, "object_key", maximum=1024)
    object_parts = object_key.strip("/").split("/")
    expected_prefix = ["tenants", tenant_id, "projects", project_id]
    if (
        object_key != object_key.strip("/")
        or object_parts[:4] != expected_prefix
        or any(part in {"", ".", ".."} for part in object_parts)
    ):
        raise DagsterExecutionContractError("input_object.object_key")
    version_id = _required_bounded_printable_text(raw_input, "version_id", maximum=1024)
    if version_id.casefold() == "null":
        raise DagsterExecutionContractError("input_object.version_id")
    content_sha256 = _required_dagster_text(
        raw_input,
        "content_sha256",
        maximum=64,
        pattern=_DAGSTER_SHA256_PATTERN,
    )
    raw_content_length = raw_input.get("content_length")
    if (
        isinstance(raw_content_length, bool)
        or not isinstance(raw_content_length, int)
        or not 44 <= raw_content_length <= 5 * 1024**3
    ):
        raise DagsterExecutionContractError("input_object.content_length")
    content_type = _required_bounded_printable_text(raw_input, "content_type", maximum=128)
    if content_type not in {"audio/wav", "audio/x-wav"}:
        raise DagsterExecutionContractError("input_object.content_type")

    raw_capabilities = payload.get("capabilities")
    if (
        not isinstance(raw_capabilities, list)
        or not raw_capabilities
        or len(raw_capabilities) > len(_AUDIO_CAPABILITIES)
        or any(
            not isinstance(item, str) or item not in _AUDIO_CAPABILITIES
            for item in raw_capabilities
        )
        or len(set(raw_capabilities)) != len(raw_capabilities)
    ):
        raise DagsterExecutionContractError("capabilities")

    return {
        "schema_version": AUDIO_INTELLIGENCE_EXECUTION_ENVELOPE_SCHEMA,
        "execution_contract": contract,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "trace_id": _required_dagster_text(payload, "trace_id"),
        "run_id": _required_dagster_text(payload, "run_id"),
        "dispatch_idempotency_key": _required_dagster_text(payload, "dispatch_idempotency_key"),
        "outbox_fencing_token": _required_dagster_text(
            payload,
            "outbox_fencing_token",
            maximum=64,
            pattern=re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$"),
        ),
        "deadline_at": normalized_deadline,
        "audio_session_id": _required_dagster_text(payload, "audio_session_id"),
        "recording_id": _required_dagster_text(payload, "recording_id"),
        "input_object": {
            "storage_object_id": _required_dagster_text(raw_input, "storage_object_id"),
            "storage_provider": storage_provider,
            "bucket": bucket,
            "object_key": object_key,
            "version_id": version_id,
            "content_sha256": content_sha256,
            "content_length": raw_content_length,
            "content_type": content_type,
        },
        "inference": {
            "provider": _required_dagster_text(payload, "provider", maximum=128),
            "model": _required_dagster_text(payload, "model_version", maximum=128),
        },
        "capabilities": list(raw_capabilities),
    }


DAGSTER_LAUNCH_MUTATION = """
mutation LaunchAurisRun($executionParams: ExecutionParams!) {
  launchPipelineExecution(executionParams: $executionParams) {
    __typename
    ... on LaunchRunSuccess {
      run {
        runId
        status
      }
    }
    ... on PythonError {
      message
      stack
    }
    ... on RunConfigValidationInvalid {
      errors {
        message
        reason
      }
    }
  }
}
""".strip()

DAGSTER_RUN_BY_KEY_QUERY = """
query AurisRunByKey($filter: RunsFilter!) {
  runsOrError(filter: $filter, limit: 2) {
    __typename
    ... on Runs {
      results {
        runId
        status
        tags {
          key
          value
        }
      }
    }
    ... on PythonError {
      message
      stack
    }
  }
}
""".strip()

DAGSTER_RUN_STATUS_QUERY = """
query AurisRunStatus($runId: ID!) {
  pipelineRunOrError(runId: $runId) {
    __typename
    ... on Run {
      runId
      status
      canTerminate
    }
    ... on RunNotFoundError {
      message
    }
    ... on PythonError {
      message
      stack
    }
  }
}
""".strip()

DAGSTER_RUN_STATUSES = frozenset(
    {
        "QUEUED",
        "NOT_STARTED",
        "MANAGED",
        "STARTING",
        "STARTED",
        "SUCCESS",
        "FAILURE",
        "CANCELING",
        "CANCELED",
    }
)

DAGSTER_CANCEL_RUN_MUTATION = """
mutation CancelAurisRun($runId: String!, $terminatePolicy: TerminateRunPolicy!) {
  terminateRun(runId: $runId, terminatePolicy: $terminatePolicy) {
    __typename
    ... on TerminateRunSuccess {
      run {
        runId
        status
      }
    }
    ... on TerminateRunFailure {
      message
      run {
        runId
        status
      }
    }
    ... on RunNotFoundError {
      message
      runId
    }
    ... on PythonError {
      message
      stack
    }
  }
}
""".strip()


class RealDagsterClient:
    def __init__(
        self,
        graphql_url: str | None = None,
        repository_location_name: str | None = None,
        repository_name: str | None = None,
        default_job_name: str | None = None,
        bearer_token: str | None = None,
        execution_mode: str | None = None,
    ) -> None:
        self.graphql_url = (
            graphql_url or os.environ.get("DAGSTER_GRAPHQL_URL") or "http://127.0.0.1:3000/graphql"
        )
        self.repository_location_name = (
            repository_location_name
            or os.environ.get("DAGSTER_REPOSITORY_LOCATION_NAME")
            or "auris_flow_defs"
        )
        self.repository_name = (
            repository_name or os.environ.get("DAGSTER_REPOSITORY_NAME") or "__repository__"
        )
        self.default_job_name = (
            default_job_name
            or os.environ.get("DAGSTER_DEFAULT_JOB_NAME")
            or "auris_flow_generic_job"
        )
        self.bearer_token = bearer_token or os.environ.get("DAGSTER_GRAPHQL_BEARER_TOKEN")
        selected_execution_mode = execution_mode or "control-plane-acknowledgement"
        allowed_execution_modes = {
            "control-plane-acknowledgement",
            "ci-cancel-delay",
            "ci-intentional-failure",
        }
        if selected_execution_mode not in allowed_execution_modes:
            raise ValueError("Dagster execution mode is not supported")
        if selected_execution_mode.startswith("ci-") and os.environ.get("APP_ENV") != "ci":
            raise ValueError("CI-only Dagster execution mode is disabled")
        self.execution_mode = selected_execution_mode

    def submit_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("dagster", "run_request", payload)
        if failure:
            return failure
        try:
            job_name = self._job_name(payload)
            run_config = self._run_config(payload)
        except DagsterExecutionContractError as exc:
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "invalid_field": exc.field,
                },
                error_code="DAGSTER_EXECUTION_CONTRACT_INVALID",
                error_message="Dagster execution contract is invalid",
                retryable=False,
            )
        run_key = str(
            payload.get("dispatch_idempotency_key")
            or payload.get("run_key")
            or payload.get("run_id")
            or _stable_receipt_id("dg_key", payload)
        )
        execution_params = {
            "selector": {
                "repositoryLocationName": self.repository_location_name,
                "repositoryName": self.repository_name,
                "pipelineName": job_name,
            },
            "runConfigData": run_config,
            "executionMetadata": {
                "tags": self._tags(payload, run_key, run_config=run_config),
            },
        }
        graphql_payload = {
            "query": DAGSTER_LAUNCH_MUTATION,
            "variables": {"executionParams": execution_params},
        }
        graphql_payload_sha256 = hashlib.sha256(
            json.dumps(
                graphql_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            response = self._request(graphql_payload)
        except (OSError, URLError, HTTPError, TimeoutError, ValueError):
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "launchPipelineExecution",
                    "job_name": job_name,
                    "run_key": run_key,
                    "graphql_payload_sha256": graphql_payload_sha256,
                    "request_sha256": graphql_payload_sha256,
                },
                error_code="DAGSTER_RUN_REQUEST_FAILED",
                error_message="Dagster GraphQL request failed",
                retryable=True,
                retry_after_seconds=10,
            )

        graph_errors = response.get("errors") if isinstance(response, dict) else None
        if graph_errors:
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "launchPipelineExecution",
                    "job_name": job_name,
                    "run_key": run_key,
                    "graphql_payload_sha256": graphql_payload_sha256,
                    "request_sha256": graphql_payload_sha256,
                    "graphql_error_sha256": _structured_sha256(graph_errors),
                },
                error_code="DAGSTER_GRAPHQL_ERROR",
                error_message="Dagster GraphQL request was rejected",
                retryable=True,
                retry_after_seconds=10,
            )

        launch = (
            response.get("data", {}).get("launchPipelineExecution")
            if isinstance(response, dict)
            else None
        )
        if not isinstance(launch, dict):
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "launchPipelineExecution",
                    "job_name": job_name,
                    "run_key": run_key,
                    "graphql_payload_sha256": graphql_payload_sha256,
                    "request_sha256": graphql_payload_sha256,
                    "response_sha256": _structured_sha256(response),
                },
                error_code="DAGSTER_RESPONSE_INVALID",
                error_message="Dagster response missing launchPipelineExecution",
                retryable=True,
                retry_after_seconds=10,
            )

        response_typename = str(launch.get("__typename") or "Unknown")
        if response_typename != "LaunchRunSuccess":
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "launchPipelineExecution",
                    "job_name": job_name,
                    "run_key": run_key,
                    "graphql_payload_sha256": graphql_payload_sha256,
                    "request_sha256": graphql_payload_sha256,
                    "response_typename": response_typename,
                    "response_sha256": _structured_sha256(launch),
                },
                error_code="DAGSTER_RUN_REJECTED",
                error_message="Dagster rejected run request",
                retryable=response_typename == "PythonError",
                retry_after_seconds=10 if response_typename == "PythonError" else None,
            )

        run_data = launch.get("run")
        run: dict[str, Any] = run_data if isinstance(run_data, dict) else {}
        external_run_id = str(run.get("runId") or "")
        if not external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="run_request",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "launchPipelineExecution",
                    "job_name": job_name,
                    "run_key": run_key,
                    "graphql_payload_sha256": graphql_payload_sha256,
                    "request_sha256": graphql_payload_sha256,
                    "response_typename": response_typename,
                    "response_sha256": _structured_sha256(launch),
                },
                error_code="DAGSTER_RUN_ID_MISSING",
                error_message="Dagster LaunchRunSuccess missing runId",
                retryable=True,
                retry_after_seconds=10,
            )

        protocol_receipt = {}
        extensions = response.get("extensions") if isinstance(response, dict) else None
        if isinstance(extensions, dict) and isinstance(
            extensions.get("auris_protocol_receipt"), dict
        ):
            protocol_receipt = extensions["auris_protocol_receipt"]

        execution_envelope = run_config.get("execution_envelope")
        envelope_sha256 = (
            _structured_sha256(execution_envelope) if isinstance(execution_envelope, dict) else None
        )
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={
                "mode": "real",
                "graphql_operation": "launchPipelineExecution",
                "external_run_id": external_run_id,
                "dagster_run_id": external_run_id,
                "run_request_id": _stable_receipt_id("dg_req", payload, "run_id", "run_key"),
                "job_name": job_name,
                "partition_key": payload.get("partition_key"),
                "run_key": run_key,
                "run_id": payload.get("run_id"),
                "trace_id": payload.get("trace_id"),
                "tenant_id": payload.get("tenant_id"),
                "project_id": payload.get("project_id"),
                "graphql_payload_sha256": graphql_payload_sha256,
                "request_sha256": graphql_payload_sha256,
                **(
                    {"execution_envelope_sha256": envelope_sha256}
                    if envelope_sha256 is not None
                    else {}
                ),
                "response_typename": response_typename,
                "dagster_run_status": run.get("status"),
                "protocol_receipt": protocol_receipt,
            },
        )

    def reconcile_run_request(self, payload: dict[str, Any]) -> DispatchResult:
        run_key = str(
            payload.get("dispatch_idempotency_key")
            or payload.get("run_key")
            or payload.get("run_id")
            or _stable_receipt_id("dg_key", payload)
        )
        graphql_payload = {
            "query": DAGSTER_RUN_BY_KEY_QUERY,
            "variables": {
                "filter": {"tags": [{"key": "auris/dispatch_idempotency_key", "value": run_key}]}
            },
        }
        try:
            response = self._request(graphql_payload)
        except (OSError, URLError, HTTPError, TimeoutError, ValueError):
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_FAILED",
                "Dagster reconciliation request failed",
                retryable=True,
                details={"run_key": run_key},
            )
        runs_or_error = (
            response.get("data", {}).get("runsOrError") if isinstance(response, dict) else None
        )
        if not isinstance(runs_or_error, dict):
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_RESPONSE_INVALID",
                "Dagster response missing runsOrError",
                retryable=True,
                details={"run_key": run_key},
            )
        results = runs_or_error.get("results")
        if not isinstance(results, list):
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_RESPONSE_INVALID",
                "Dagster reconciliation result list is invalid",
                retryable=True,
                details={"run_key": run_key},
            )
        if not results:
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_ABSENT",
                "Dagster exact dispatch tag is absent",
                retryable=True,
                details={
                    "run_key": run_key,
                    "absence_proof": DAGSTER_RECONCILIATION_ABSENCE_PROOF,
                },
            )
        if len(results) != 1:
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_AMBIGUOUS",
                "Dagster reconciliation matched multiple runs",
                retryable=False,
                details={"run_key": run_key, "result_count": len(results)},
            )
        run = results[0]
        if not isinstance(run, dict):
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_RESPONSE_INVALID",
                "Dagster reconciliation result is invalid",
                retryable=False,
                details={"run_key": run_key},
            )
        run_id = run.get("runId")
        run_status = run.get("status")
        if (
            not isinstance(run_id, str)
            or not run_id.strip()
            or run_id != run_id.strip()
            or not isinstance(run_status, str)
            or run_status not in DAGSTER_RUN_STATUSES
        ):
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_RESPONSE_INVALID",
                "Dagster reconciliation result identity or status is invalid",
                retryable=False,
                details={"run_key": run_key},
            )
        tags = run.get("tags")
        idempotency_tags = (
            [
                tag
                for tag in tags
                if isinstance(tag, dict) and tag.get("key") == "auris/dispatch_idempotency_key"
            ]
            if isinstance(tags, list)
            else []
        )
        if len(idempotency_tags) != 1 or idempotency_tags[0].get("value") != run_key:
            return _reconciliation_failure(
                "dagster",
                "reconcile_run_request",
                "DAGSTER_RECONCILIATION_IDENTITY_MISMATCH",
                "Dagster reconciliation result is not bound to the requested run key",
                retryable=False,
                details={"run_key": run_key},
            )
        return DispatchResult(
            adapter="dagster",
            operation="reconcile_run_request",
            details={
                "mode": "real",
                "reconciled": True,
                "external_run_id": run_id,
                "dagster_run_id": run_id,
                "dagster_status": run_status,
                "run_key": run_key,
            },
        )

    def get_run_status(self, external_run_id: str) -> DispatchResult:
        graphql_payload = {
            "query": DAGSTER_RUN_STATUS_QUERY,
            "variables": {"runId": external_run_id},
        }
        try:
            response = self._request(graphql_payload)
        except (OSError, URLError, HTTPError, TimeoutError, ValueError):
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "pipelineRunOrError",
                    "external_run_id": external_run_id,
                    "dagster_run_id": external_run_id,
                },
                error_code="DAGSTER_STATUS_QUERY_FAILED",
                error_message="Dagster status request failed",
                retryable=True,
                retry_after_seconds=5,
            )

        graph_errors = response.get("errors") if isinstance(response, dict) else None
        run_or_error = (
            response.get("data", {}).get("pipelineRunOrError")
            if isinstance(response, dict)
            else None
        )
        if graph_errors or not isinstance(run_or_error, dict):
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "pipelineRunOrError",
                    "external_run_id": external_run_id,
                    "dagster_run_id": external_run_id,
                    "graphql_error_sha256": _structured_sha256(graph_errors or []),
                },
                error_code="DAGSTER_STATUS_RESPONSE_INVALID",
                error_message="Dagster status response is invalid",
                retryable=True,
                retry_after_seconds=5,
            )

        response_typename = str(run_or_error.get("__typename") or "Unknown")
        details = {
            "mode": "real",
            "graphql_operation": "pipelineRunOrError",
            "external_run_id": external_run_id,
            "dagster_run_id": external_run_id,
            "dagster_status": None,
            "can_terminate": bool(run_or_error.get("canTerminate", False)),
            "response_typename": response_typename,
        }
        if response_typename not in {"Run", "PipelineRun"}:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                details=details,
                error_code=(
                    "DAGSTER_RUN_NOT_FOUND"
                    if response_typename in {"RunNotFoundError", "PipelineRunNotFoundError"}
                    else "DAGSTER_STATUS_QUERY_REJECTED"
                ),
                error_message="Dagster rejected status query",
                retryable=response_typename == "PythonError",
                retry_after_seconds=5 if response_typename == "PythonError" else None,
            )
        returned_run_id = run_or_error.get("runId")
        if returned_run_id != external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                details=details,
                error_code="DAGSTER_STATUS_IDENTITY_MISMATCH",
                error_message="Dagster status response is bound to another run",
                retryable=False,
            )
        returned_status = run_or_error.get("status")
        if not isinstance(returned_status, str) or returned_status not in DAGSTER_RUN_STATUSES:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                details=details,
                error_code="DAGSTER_STATUS_RESPONSE_INVALID",
                error_message="Dagster status response contains an invalid run status",
                retryable=False,
            )
        details["dagster_status"] = returned_status
        return DispatchResult(
            adapter="dagster",
            operation="run_status",
            details=details,
        )

    def cancel_run(self, external_run_id: str) -> DispatchResult:
        graphql_payload = {
            "query": DAGSTER_CANCEL_RUN_MUTATION,
            "variables": {
                "runId": external_run_id,
                "terminatePolicy": "SAFE_TERMINATE",
            },
        }
        try:
            response = self._request(graphql_payload)
        except (OSError, URLError, HTTPError, TimeoutError, ValueError):
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "terminateRun",
                    "external_run_id": external_run_id,
                    "dagster_run_id": external_run_id,
                    "terminate_policy": "SAFE_TERMINATE",
                },
                error_code="DAGSTER_CANCEL_REQUEST_FAILED",
                error_message="Dagster cancellation request failed",
                retryable=True,
                retry_after_seconds=5,
            )

        graph_errors = response.get("errors") if isinstance(response, dict) else None
        terminate = (
            response.get("data", {}).get("terminateRun") if isinstance(response, dict) else None
        )
        if graph_errors or not isinstance(terminate, dict):
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                details={
                    "mode": "real",
                    "graphql_operation": "terminateRun",
                    "external_run_id": external_run_id,
                    "dagster_run_id": external_run_id,
                    "terminate_policy": "SAFE_TERMINATE",
                    "graphql_error_sha256": _structured_sha256(graph_errors or []),
                },
                error_code="DAGSTER_CANCEL_RESPONSE_INVALID",
                error_message="Dagster cancellation response is invalid",
                retryable=True,
                retry_after_seconds=5,
            )

        response_typename = str(terminate.get("__typename") or "Unknown")
        run_data = terminate.get("run")
        run = run_data if isinstance(run_data, dict) else {}
        details = {
            "mode": "real",
            "graphql_operation": "terminateRun",
            "external_run_id": external_run_id,
            "dagster_run_id": external_run_id,
            "dagster_status": None,
            "terminate_policy": "SAFE_TERMINATE",
            "response_typename": response_typename,
        }
        returned_run_id = run.get("runId")
        returned_status = run.get("status")
        if run:
            if returned_run_id != external_run_id:
                return DispatchResult(
                    adapter="dagster",
                    operation="cancel_run",
                    status="failed",
                    details=details,
                    error_code="DAGSTER_CANCEL_IDENTITY_MISMATCH",
                    error_message="Dagster cancellation response is bound to another run",
                    retryable=False,
                )
            if not isinstance(returned_status, str) or returned_status not in DAGSTER_RUN_STATUSES:
                return DispatchResult(
                    adapter="dagster",
                    operation="cancel_run",
                    status="failed",
                    details=details,
                    error_code="DAGSTER_CANCEL_RESPONSE_INVALID",
                    error_message="Dagster cancellation response contains an invalid run status",
                    retryable=False,
                )
            details["dagster_status"] = returned_status
        if response_typename != "TerminateRunSuccess":
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                details=details,
                error_code=(
                    "DAGSTER_RUN_NOT_FOUND"
                    if response_typename == "RunNotFoundError"
                    else "DAGSTER_CANCEL_REJECTED"
                ),
                error_message="Dagster rejected cancellation",
                retryable=response_typename == "PythonError",
                retry_after_seconds=5 if response_typename == "PythonError" else None,
            )
        if returned_run_id != external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                details=details,
                error_code="DAGSTER_CANCEL_IDENTITY_MISMATCH",
                error_message="Dagster cancellation response is bound to another run",
                retryable=False,
            )
        if not isinstance(returned_status, str) or returned_status not in DAGSTER_RUN_STATUSES:
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                details=details,
                error_code="DAGSTER_CANCEL_RESPONSE_INVALID",
                error_message="Dagster cancellation response contains an invalid run status",
                retryable=False,
            )
        return DispatchResult(
            adapter="dagster",
            operation="cancel_run",
            details=details,
        )

    def _job_name(self, payload: dict[str, Any]) -> str:
        event_type = _required_dagster_text(payload, "event_type", maximum=128)
        if event_type == "audio_intelligence.requested":
            if payload.get("execution_contract") != AUDIO_INTELLIGENCE_EXECUTION_CONTRACT:
                raise DagsterExecutionContractError("execution_contract")
            if self.execution_mode != "control-plane-acknowledgement":
                raise DagsterExecutionContractError("execution_mode")
            return AUDIO_INTELLIGENCE_JOB_NAME
        if event_type not in _DAGSTER_CONTROL_PLANE_EVENT_TYPES:
            raise DagsterExecutionContractError("event_type")
        return self.default_job_name

    def _run_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        otel_context = current_trace_context()
        authoritative_context = {
            "tenant_id": payload.get("tenant_id"),
            "project_id": payload.get("project_id"),
            "trace_id": payload.get("trace_id"),
            "run_id": payload.get("run_id"),
            "event_type": payload.get("event_type"),
            "partition_key": payload.get("partition_key"),
            "task_version_id": payload.get("task_version_id"),
            "task_version_behavior_sha256": payload.get("task_version_behavior_sha256"),
            "task_version_binding_sha256": payload.get("task_version_binding_sha256"),
            "expected_executed_bundle_sha256": payload.get("expected_executed_bundle_sha256"),
            "asset_key": payload.get("asset_key"),
            "provider": payload.get("provider"),
            "dispatch_idempotency_key": payload.get("dispatch_idempotency_key"),
            "outbox_fencing_token": payload.get("outbox_fencing_token"),
            "experiment_id": payload.get("experiment_id"),
            "experiment_arm": payload.get("experiment_arm"),
            "experiment_design_sha256": payload.get("experiment_design_sha256"),
            **{
                "otel_trace_id": otel_context.get("otel_trace_id"),
                "otel_parent_span_id": otel_context.get("otel_span_id"),
                "otel_trace_flags": otel_context.get("otel_trace_flags"),
            },
        }
        if payload.get("event_type") == "audio_intelligence.requested":
            return {
                "auris_context": authoritative_context,
                "execution_envelope": _audio_execution_envelope(payload),
            }
        return {
            "auris_context": authoritative_context,
            "execution": {"mode": self.execution_mode},
        }

    def _tags(
        self,
        payload: dict[str, Any],
        run_key: str,
        *,
        run_config: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        execution_envelope = (run_config or {}).get("execution_envelope")
        tag_values = {
            "auris/dispatch_idempotency_key": run_key,
            "auris/execution_contract": (
                execution_envelope.get("execution_contract")
                if isinstance(execution_envelope, dict)
                else None
            ),
            "auris/execution_envelope_sha256": (
                _structured_sha256(execution_envelope)
                if isinstance(execution_envelope, dict)
                else None
            ),
            "tenant_id": payload.get("tenant_id"),
            "project_id": payload.get("project_id"),
            "trace_id": payload.get("trace_id"),
            "run_id": payload.get("run_id"),
            "event_type": payload.get("event_type"),
            "aggregate_type": payload.get("aggregate_type"),
            "partition_key": payload.get("partition_key"),
            "run_key": run_key,
            "request_run_key": payload.get("run_key"),
            "dispatch_idempotency_key": payload.get("dispatch_idempotency_key"),
            "outbox_fencing_token": payload.get("outbox_fencing_token"),
            "task_version_binding_sha256": payload.get("task_version_binding_sha256"),
            "experiment_id": payload.get("experiment_id"),
            "experiment_arm": payload.get("experiment_arm"),
        }
        return [
            {"key": key, "value": str(value)}
            for key, value in tag_values.items()
            if value is not None and str(value)
        ]

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(self.graphql_url, data=data, method="POST", headers=headers)
        with urlopen(request, timeout=5) as response:
            raw_bytes = response.read(MAX_DAGSTER_GRAPHQL_RESPONSE_BYTES + 1)
        if len(raw_bytes) > MAX_DAGSTER_GRAPHQL_RESPONSE_BYTES:
            raise ValueError("Dagster GraphQL response exceeds size limit")
        raw = raw_bytes.decode("utf-8")
        return json.loads(raw) if raw else {}


class LocalObjectStorageClient:
    def reserve_object(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("object_storage", "reserve_object", payload)
        if failure:
            return failure
        storage_object_id = _stable_receipt_id(
            "obj",
            payload,
            "run_id",
            "asset_ref",
            "asset_key",
            "target",
            "object_id",
        )
        body, artifact_state = _object_artifact_body(payload, storage_object_id)
        return DispatchResult(
            adapter="object_storage",
            operation="reserve_object",
            details={
                "storage_object_id": storage_object_id,
                "asset_ref": payload.get("asset_ref"),
                "content_type": payload.get("content_type", "application/json"),
                "object_uri": f"mock://object-storage/{storage_object_id}",
                "artifact_state": artifact_state,
                "content_sha256": hashlib.sha256(body).hexdigest(),
                "content_length": len(body),
                "verified": {"method": "deterministic-local-render", "status": 200},
            },
        )

    def get_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        body = json.dumps(
            {
                "bucket": bucket,
                "object_key": object_key,
                "byte_range": byte_range,
                "if_match": if_match,
                "version_id": version_id,
                "mode": "local",
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "status": 200,
            "headers": {"Accept-Ranges": "bytes", "Content-Length": str(len(body))},
            "content_length": str(len(body)),
            "content_type": "application/json",
            "body": body,
        }

    def reconcile_object(self, payload: dict[str, Any]) -> DispatchResult:
        dispatched = self.reserve_object(payload)
        return DispatchResult(
            adapter=dispatched.adapter,
            operation="reconcile_object",
            status=dispatched.status,
            details={**dispatched.details, "reconciled": True, "mode": "local"},
            error_code=dispatched.error_code,
            error_message=dispatched.error_message,
            retryable=dispatched.retryable,
            retry_after_seconds=dispatched.retry_after_seconds,
        )


class RealObjectStorageClient:
    def __init__(
        self,
        endpoint: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        *,
        provider: str | None = None,
        addressing_style: str | None = None,
        signature_mode: str | None = None,
        session_token: str | None = None,
        allowed_buckets: str | None = None,
    ) -> None:
        self.provider = (provider or os.environ.get("OBJECT_STORAGE_PROVIDER") or "minio").lower()
        if self.provider not in SUPPORTED_OBJECT_STORAGE_PROVIDERS:
            raise ValueError(f"unsupported object storage provider: {self.provider}")
        self.endpoint = (
            endpoint or os.environ.get("OBJECT_STORAGE_ENDPOINT") or "http://127.0.0.1:9000"
        ).rstrip("/")
        self.bucket = bucket or os.environ.get("OBJECT_STORAGE_BUCKET") or "auris-flow-local"
        self.access_key = (
            access_key
            or os.environ.get("OBJECT_STORAGE_ACCESS_KEY")
            or os.environ.get("AWS_ACCESS_KEY_ID")
            or "minioadmin"
        )
        self.secret_key = (
            secret_key
            or os.environ.get("OBJECT_STORAGE_SECRET_KEY")
            or os.environ.get("AWS_SECRET_ACCESS_KEY")
            or "minioadmin"
        )
        self.region = region or os.environ.get("OBJECT_STORAGE_REGION") or "us-east-1"
        self.addressing_style = addressing_style or _default_addressing_style(self.provider)
        if self.addressing_style not in {"path", "virtual"}:
            raise ValueError(
                f"unsupported object storage addressing style: {self.addressing_style}"
            )
        if self.provider == "oss" and self.addressing_style != "virtual":
            raise ValueError("OSS access requires virtual-host addressing")
        self.signature_mode = signature_mode or _default_signature_mode(self.provider)
        if self.provider == "obs":
            allowed_signature_modes = {"obs"}
        elif self.provider == "oss":
            allowed_signature_modes = {"ossv4"}
        else:
            allowed_signature_modes = {"s3v4"}
        if self.signature_mode not in allowed_signature_modes:
            raise ValueError(
                f"unsupported signature mode {self.signature_mode!r} for {self.provider}"
            )
        self.session_token = session_token
        configured_buckets = {
            item.strip() for item in (allowed_buckets or "").split(",") if item.strip()
        }
        self.allowed_buckets = frozenset(configured_buckets or {self.bucket})

    def reserve_object(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("object_storage", "reserve_object", payload)
        if failure:
            return failure
        storage_object_id = _stable_receipt_id(
            "obj",
            payload,
            "run_id",
            "asset_ref",
            "asset_key",
            "target",
            "object_id",
        )
        object_key = self._object_key(payload, storage_object_id)
        content_type = str(payload.get("content_type") or "application/json")
        body, artifact_state = _object_artifact_body(payload, storage_object_id)
        content_sha256 = hashlib.sha256(body).hexdigest()
        try:
            self._ensure_bucket()
            put_response = self._request(
                "PUT",
                f"/{self.bucket}/{object_key}",
                body=body,
                content_type=content_type,
            )
            self.head_object(self.bucket, object_key)
            if artifact_state == "materialized":
                stored = self.get_object(self.bucket, object_key)
                stored_body = stored.get("body") if isinstance(stored, dict) else None
                if (
                    not isinstance(stored_body, bytes)
                    or hashlib.sha256(stored_body).hexdigest() != content_sha256
                ):
                    raise ValueError("materialized object content hash mismatch after write")
        except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
            return DispatchResult(
                adapter="object_storage",
                operation="reserve_object",
                status="failed",
                details={
                    "mode": "real",
                    "endpoint": self.endpoint,
                    "bucket": self.bucket,
                    "object_key": object_key,
                    "storage_object_id": storage_object_id,
                },
                error_code="OBJECT_STORAGE_WRITE_FAILED",
                error_message=str(exc),
                retryable=True,
                retry_after_seconds=10,
            )
        return DispatchResult(
            adapter="object_storage",
            operation="reserve_object",
            details={
                "mode": "real",
                "storage_object_id": storage_object_id,
                "asset_ref": payload.get("asset_ref"),
                "content_type": content_type,
                "object_uri": (
                    f"{'s3' if self.provider in {'minio', 's3'} else self.provider}://"
                    f"{self.bucket}/{object_key}"
                ),
                "bucket": self.bucket,
                "object_key": object_key,
                "endpoint": self.endpoint,
                "protocol": (
                    "obs"
                    if self.signature_mode == "obs"
                    else "oss"
                    if self.signature_mode == "ossv4"
                    else "s3"
                    if self.provider in {"minio", "s3"}
                    else self.provider
                ),
                "provider": self.provider,
                "tenant_id": payload.get("tenant_id"),
                "project_id": payload.get("project_id"),
                "trace_id": payload.get("trace_id"),
                "run_id": payload.get("run_id"),
                "content_sha256": content_sha256,
                "etag": put_response.get("etag"),
                "content_length": len(body),
                "artifact_state": artifact_state,
                "verified": {
                    "method": "PutObject+HeadObject+GetObject"
                    if artifact_state == "materialized"
                    else "PutObject+HeadObject",
                    "status": 200,
                },
            },
        )

    def head_object(
        self,
        bucket: str,
        object_key: str,
        *,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        # S3-compatible providers return their validated SHA-256 only when
        # checksum mode is requested. This header is signed and does not trust
        # caller-controlled x-amz-meta-* values.
        extra_headers: dict[str, str] = {}
        if self.provider in {"minio", "s3"}:
            extra_headers["x-amz-checksum-mode"] = "ENABLED"
        if if_match:
            extra_headers["If-Match"] = if_match
        return self._request(
            "HEAD",
            f"/{bucket}/{object_key}",
            extra_headers=extra_headers or None,
            query=self._exact_version_query(version_id),
        )

    def head_bucket(
        self,
        bucket: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Verify authenticated bucket access without reading or listing objects."""

        return self._request("HEAD", f"/{bucket}", timeout_seconds=timeout_seconds)

    def reconcile_object(self, payload: dict[str, Any]) -> DispatchResult:
        storage_object_id = _stable_receipt_id(
            "obj",
            payload,
            "run_id",
            "asset_ref",
            "asset_key",
            "target",
            "object_id",
        )
        object_key = self._object_key(payload, storage_object_id)
        try:
            head = self.head_object(self.bucket, object_key)
        except HTTPError as exc:
            if exc.code == 404:
                return _reconciliation_not_found(
                    "object_storage",
                    "reconcile_object",
                    details={
                        "storage_object_id": storage_object_id,
                        "bucket": self.bucket,
                        "object_key": object_key,
                    },
                )
            return _reconciliation_failure(
                "object_storage",
                "reconcile_object",
                "OBJECT_STORAGE_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"bucket": self.bucket, "object_key": object_key},
            )
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            return _reconciliation_failure(
                "object_storage",
                "reconcile_object",
                "OBJECT_STORAGE_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"bucket": self.bucket, "object_key": object_key},
            )
        return DispatchResult(
            adapter="object_storage",
            operation="reconcile_object",
            details={
                "mode": "real",
                "reconciled": True,
                "storage_object_id": storage_object_id,
                "content_type": payload.get("content_type", "application/json"),
                "object_uri": (
                    f"{'s3' if self.provider in {'minio', 's3'} else self.provider}://"
                    f"{self.bucket}/{object_key}"
                ),
                "bucket": self.bucket,
                "object_key": object_key,
                "provider": self.provider,
                "etag": head.get("etag") if isinstance(head, dict) else None,
            },
        )

    def allows_bucket(self, bucket: str) -> bool:
        return bucket in self.allowed_buckets

    def get_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if byte_range:
            headers["Range"] = byte_range
        if if_match:
            headers["If-Match"] = if_match
        return self._request(
            "GET",
            f"/{bucket}/{object_key}",
            extra_headers=headers or None,
            query=self._exact_version_query(version_id),
        )

    def get_object_version(
        self,
        bucket: str,
        object_key: str,
        *,
        version_id: str,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        """Read one immutable S3/MinIO object version with a bounded body."""

        if self.provider not in {"minio", "s3"} or self.signature_mode != "s3v4":
            raise ValueError("exact version reads are supported only for MinIO/S3")
        if not self.allows_bucket(bucket):
            raise ValueError("exact version read bucket is not allowed")
        query = self._exact_version_query(version_id)
        assert query is not None
        if not 0 < max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("exact version response bound is invalid")
        return self._request(
            "GET",
            f"/{bucket}/{object_key}",
            query=query,
            max_response_bytes=max_response_bytes,
        )

    def open_object(
        self,
        bucket: str,
        object_key: str,
        *,
        byte_range: str | None = None,
        if_match: str | None = None,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if byte_range:
            headers["Range"] = byte_range
        if if_match:
            headers["If-Match"] = if_match
        request = self._signed_request(
            "GET",
            f"/{bucket}/{object_key}",
            extra_headers=headers,
            query=self._exact_version_query(version_id),
        )
        response = urlopen(request, timeout=5)
        version_header = {
            "minio": "x-amz-version-id",
            "s3": "x-amz-version-id",
            "oss": "x-oss-version-id",
            "obs": "x-obs-version-id",
        }[self.provider]
        return {
            "status": response.status,
            "headers": dict(response.headers.items()),
            "etag": response.headers.get("ETag", "").strip('"'),
            "content_length": response.headers.get("Content-Length"),
            "content_range": response.headers.get("Content-Range"),
            "content_type": response.headers.get("Content-Type"),
            "version_id": response.headers.get(version_header),
            "stream": response,
        }

    @staticmethod
    def _exact_version_query(version_id: str | None) -> dict[str, str] | None:
        if version_id is None:
            return None
        if (
            not isinstance(version_id, str)
            or not version_id
            or version_id.casefold() == "null"
            or len(version_id) > 1024
            or version_id != version_id.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in version_id)
        ):
            raise ValueError("exact version id is invalid")
        return {"versionId": version_id}

    def _ensure_bucket(self) -> None:
        if self.provider != "minio":
            return
        try:
            self._request("HEAD", f"/{self.bucket}")
            return
        except HTTPError as exc:
            if exc.code not in {404, 405}:
                raise
        self._request("PUT", f"/{self.bucket}", body=b"")

    def _object_key(self, payload: dict[str, Any], storage_object_id: str) -> str:
        tenant_id = str(payload.get("tenant_id") or "unknown_tenant").strip("/")
        project_id = str(payload.get("project_id") or "unknown_project").strip("/")
        run_id = str(payload.get("run_id") or storage_object_id).strip("/")
        raw_ref = str(
            payload.get("object_key")
            or payload.get("asset_ref")
            or payload.get("asset_key")
            or payload.get("target")
            or "object"
        ).strip("/")
        safe_ref = "/".join(quote(part, safe="-_.~") for part in raw_ref.split("/") if part)
        if not safe_ref:
            safe_ref = "object"
        key = f"tenants/{tenant_id}/projects/{project_id}/{safe_ref}"
        if "." not in key.rsplit("/", 1)[-1]:
            key = f"{key}/{run_id}-{storage_object_id}.json"
        return key

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float = 5.0,
        query: dict[str, str] | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0:
            raise ValueError("object storage request timeout must be positive")
        request = self._signed_request(
            method,
            path,
            body=body,
            content_type=content_type,
            extra_headers=extra_headers,
            query=query,
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            if method == "HEAD":
                raw = b""
            elif max_response_bytes is None:
                raw = response.read()
            else:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise ValueError("object storage response exceeds configured bound")
            version_header = {
                "minio": "x-amz-version-id",
                "s3": "x-amz-version-id",
                "oss": "x-oss-version-id",
                "obs": "x-obs-version-id",
            }[self.provider]
            return {
                "status": response.status,
                "headers": dict(response.headers.items()),
                "etag": response.headers.get("ETag", "").strip('"'),
                "version_id": response.headers.get(version_header),
                "content_length": response.headers.get("Content-Length"),
                "content_range": response.headers.get("Content-Range"),
                "content_type": response.headers.get("Content-Type"),
                "body": raw,
            }

    def _signed_request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> Request:
        parsed = urlparse(self.endpoint)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"invalid object storage endpoint: {self.endpoint}")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("object storage endpoint must not contain path, query, or fragment")
        url, host, canonical_uri, canonical_resource = self._request_target(parsed, path)
        canonical_query = "&".join(
            f"{quote(str(key), safe='-_.~')}={quote(str(value), safe='-_.~')}"
            for key, value in sorted((query or {}).items())
        )
        if canonical_query:
            url = f"{url}?{canonical_query}"
        payload = body or b""
        payload_hash = hashlib.sha256(payload).hexdigest()
        timestamp = (
            os.environ.get("AURIS_FIXED_OBJECT_STORAGE_TIME")
            or os.environ.get("AURIS_FIXED_AWS_SIGV4_TIME")
            or _sigv4_timestamp()
        )
        date_stamp = timestamp[:8]
        headers = {"Host": host}
        if body is not None:
            headers["Content-Type"] = content_type
        for key, value in (extra_headers or {}).items():
            if value:
                headers[key] = value
        if self.signature_mode == "obs":
            headers["Date"] = format_datetime(datetime.now(UTC), usegmt=True)
            if self.session_token:
                headers["x-obs-security-token"] = self.session_token
            headers["Authorization"] = self._obs_authorization_header(
                method=method,
                canonical_resource=(
                    f"{canonical_resource}?{canonical_query}"
                    if canonical_query
                    else canonical_resource
                ),
                headers=headers,
            )
        elif self.signature_mode == "ossv4":
            headers["Date"] = format_datetime(datetime.now(UTC), usegmt=True)
            headers["x-oss-content-sha256"] = "UNSIGNED-PAYLOAD"
            headers["x-oss-date"] = timestamp
            if body is not None:
                headers["Content-Length"] = str(len(payload))
            if self.session_token:
                headers["x-oss-security-token"] = self.session_token
            headers["Authorization"] = self._oss_authorization_header(
                method=method,
                canonical_resource=canonical_resource,
                headers=headers,
                date_stamp=date_stamp,
                timestamp=timestamp,
                canonical_query=canonical_query,
                additional_headers=tuple(
                    sorted(
                        key.lower()
                        for key in (extra_headers or {})
                        if key.lower() not in {"content-md5", "content-type"}
                        and not key.lower().startswith("x-oss-")
                    )
                ),
            )
        else:
            headers["x-amz-content-sha256"] = payload_hash
            headers["x-amz-date"] = timestamp
            if self.session_token:
                headers["x-amz-security-token"] = self.session_token
            headers["Authorization"] = self._authorization_header(
                method=method,
                canonical_uri=canonical_uri,
                canonical_query=canonical_query,
                headers=headers,
                payload_hash=payload_hash,
                date_stamp=date_stamp,
                timestamp=timestamp,
            )
        return Request(
            url, data=payload if method != "HEAD" else None, method=method, headers=headers
        )

    def _request_target(self, parsed: Any, path: str) -> tuple[str, str, str, str]:
        parts = [part for part in path.strip("/").split("/") if part]
        if not parts:
            raise ValueError("object storage request path must contain a bucket")
        bucket, object_parts = parts[0], parts[1:]
        encoded_bucket = quote(bucket, safe="-_.~")
        encoded_object = "/".join(quote(part, safe="-_.~") for part in object_parts)
        canonical_resource = f"/{encoded_bucket}/"
        if encoded_object:
            canonical_resource = f"{canonical_resource}{encoded_object}"

        if self.addressing_style == "path":
            host = parsed.netloc
            canonical_uri = canonical_resource
        else:
            hostname = parsed.hostname
            if not hostname:
                raise ValueError(f"invalid object storage endpoint: {self.endpoint}")
            if hostname == bucket or hostname.startswith(f"{bucket}."):
                virtual_hostname = hostname
            else:
                virtual_hostname = f"{bucket}.{hostname}"
            host = virtual_hostname
            if parsed.port:
                host = f"{host}:{parsed.port}"
            canonical_uri = f"/{encoded_object}" if encoded_object else "/"
        url = f"{parsed.scheme}://{host}{canonical_uri}"
        return url, host, canonical_uri, canonical_resource

    def _obs_authorization_header(
        self,
        *,
        method: str,
        canonical_resource: str,
        headers: dict[str, str],
    ) -> str:
        canonical_obs_headers = "".join(
            f"{key.lower()}:{' '.join(str(value).strip().split())}\n"
            for key, value in sorted(headers.items(), key=lambda item: item[0].lower())
            if key.lower().startswith("x-obs-")
        )
        string_to_sign = "\n".join(
            [
                method,
                str(headers.get("Content-MD5") or ""),
                str(headers.get("Content-Type") or ""),
                str(headers.get("Date") or ""),
                f"{canonical_obs_headers}{canonical_resource}",
            ]
        )
        signature = b64encode(
            hmac.new(
                self.secret_key.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("ascii")
        return f"OBS {self.access_key}:{signature}"

    def _oss_authorization_header(
        self,
        *,
        method: str,
        canonical_resource: str,
        headers: dict[str, str],
        date_stamp: str,
        timestamp: str,
        canonical_query: str = "",
        additional_headers: tuple[str, ...] = (),
    ) -> str:
        required_names = {
            key.lower()
            for key in headers
            if key.lower() in {"content-md5", "content-type"} or key.lower().startswith("x-oss-")
        }
        normalized_headers = {
            key.lower(): " ".join(str(value).strip().split()) for key, value in headers.items()
        }
        additional_names = sorted(
            {
                name.lower()
                for name in additional_headers
                if name.lower() in normalized_headers
                and name.lower() not in required_names
                and name.lower() not in {"authorization", "host"}
            }
        )
        signed_names = sorted(required_names | set(additional_names))
        canonical_headers = "".join(f"{name}:{normalized_headers[name]}\n" for name in signed_names)
        additional_header_value = ";".join(additional_names)
        hashed_payload = "UNSIGNED-PAYLOAD"
        canonical_request = "\n".join(
            [
                method,
                canonical_resource,
                canonical_query,
                canonical_headers,
                additional_header_value,
                hashed_payload,
            ]
        )
        credential_scope = f"{date_stamp}/{self.region}/oss/aliyun_v4_request"
        string_to_sign = "\n".join(
            [
                "OSS4-HMAC-SHA256",
                timestamp,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _oss_v4_signing_key(self.secret_key, date_stamp, self.region)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        additional_fragment = (
            f",AdditionalHeaders={additional_header_value}" if additional_header_value else ""
        )
        return (
            "OSS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}"
            f"{additional_fragment},Signature={signature}"
        )

    def _authorization_header(
        self,
        *,
        method: str,
        canonical_uri: str,
        headers: dict[str, str],
        payload_hash: str,
        date_stamp: str,
        timestamp: str,
        canonical_query: str = "",
    ) -> str:
        canonical_header_items = {
            key.lower(): " ".join(str(value).strip().split())
            for key, value in headers.items()
            if key.lower() != "authorization"
        }
        signed_headers = ";".join(sorted(canonical_header_items))
        canonical_headers = "".join(
            f"{key}:{canonical_header_items[key]}\n" for key in sorted(canonical_header_items)
        )
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                timestamp,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = _sigv4_signing_key(self.secret_key, date_stamp, self.region)
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )


def object_storage_client_for_provider(provider: str) -> RealObjectStorageClient:
    normalized = provider.lower().strip()
    if normalized not in SUPPORTED_OBJECT_STORAGE_PROVIDERS:
        raise ValueError(f"unsupported object storage provider: {normalized}")

    from app.core.config import get_settings

    runtime_settings = get_settings()
    configured_default = (
        (os.environ.get("OBJECT_STORAGE_PROVIDER") or runtime_settings.object_storage_provider)
        .lower()
        .strip()
    )
    use_global = normalized == configured_default

    endpoint = _provider_setting(normalized, "ENDPOINT")
    access_key = _provider_setting(normalized, "ACCESS_KEY")
    secret_key = _provider_setting(normalized, "SECRET_KEY")
    region = _provider_setting(normalized, "REGION")
    bucket = _provider_setting(normalized, "BUCKET")
    addressing_style = _provider_setting(normalized, "ADDRESSING_STYLE")
    signature_mode = _provider_setting(normalized, "SIGNATURE_MODE")
    session_token = _provider_setting(normalized, "SESSION_TOKEN")
    allowed_buckets = _provider_setting(normalized, "ALLOWED_BUCKETS")

    if use_global:
        endpoint = endpoint or runtime_settings.object_storage_endpoint
        access_key = access_key or runtime_settings.object_storage_access_key
        secret_key = secret_key or runtime_settings.object_storage_secret_key
        region = region or runtime_settings.object_storage_region
        bucket = bucket or runtime_settings.object_storage_bucket
        addressing_style = addressing_style or runtime_settings.object_storage_addressing_style
        signature_mode = signature_mode or runtime_settings.object_storage_signature_mode
        session_token = session_token or runtime_settings.object_storage_session_token
        allowed_buckets = allowed_buckets or runtime_settings.object_storage_allowed_buckets

    if normalized == "s3":
        access_key = access_key or _first_env("AWS_ACCESS_KEY_ID")
        secret_key = secret_key or _first_env("AWS_SECRET_ACCESS_KEY")
        session_token = session_token or _first_env("AWS_SESSION_TOKEN")
        region = region or _first_env("AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"
        endpoint = endpoint or f"https://s3.{region}.amazonaws.com"
    elif normalized == "obs":
        access_key = access_key or _first_env("OBS_ACCESS_KEY_ID", "OBS_ACCESS_KEY")
        secret_key = secret_key or _first_env("OBS_SECRET_ACCESS_KEY", "OBS_SECRET_KEY")
        session_token = session_token or _first_env("OBS_SECURITY_TOKEN")
    elif normalized == "oss":
        access_key = access_key or _first_env("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY")
        secret_key = secret_key or _first_env("OSS_ACCESS_KEY_SECRET", "OSS_SECRET_KEY")
        session_token = session_token or _first_env("OSS_SESSION_TOKEN", "OSS_SECURITY_TOKEN")

    missing = [
        name
        for name, value in (
            ("endpoint", endpoint),
            ("access_key", access_key),
            ("secret_key", secret_key),
            ("region", region),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"object storage provider {normalized} is not configured: {', '.join(missing)}"
        )

    return RealObjectStorageClient(
        endpoint=endpoint,
        bucket=bucket or "auris-flow-local",
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        provider=normalized,
        addressing_style=addressing_style or _default_addressing_style(normalized),
        signature_mode=signature_mode or _default_signature_mode(normalized),
        session_token=session_token,
        allowed_buckets=allowed_buckets,
    )


class LocalQdrantIndexClient:
    def upsert_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("qdrant", "upsert_payload", payload)
        if failure:
            return failure
        qdrant_payload = _qdrant_payload(payload)
        missing_fields = _validate_qdrant_payload(qdrant_payload)
        if missing_fields:
            return _invalid_qdrant_payload(missing_fields)
        collection = qdrant_payload.get("collection")
        source_id = qdrant_payload.get("source_id")
        point_id = _stable_receipt_id(
            "qdrant_point",
            qdrant_payload,
            "tenant_id",
            "project_id",
            "knowledge_index_id",
            "knowledge_source_id",
            "source_id",
            "source_type",
            "version",
            "collection",
        )
        return DispatchResult(
            adapter="qdrant",
            operation="upsert_payload",
            details={
                "collection": collection,
                "source_id": source_id,
                "point_ids": [point_id],
                "point_count": 1,
                "qdrant_payload": qdrant_payload,
            },
        )

    def search_index_payload(
        self, qdrant_payload: dict[str, Any], *, query: str, top_k: int
    ) -> dict[str, Any]:
        collection = str(qdrant_payload.get("collection") or "knowledge_chunks")
        return {
            "mode": "local_qdrant_projection",
            "collection": collection,
            "points": [],
            "filter": {
                "tenant_id": qdrant_payload.get("tenant_id"),
                "project_id": qdrant_payload.get("project_id"),
                "knowledge_index_id": qdrant_payload.get("knowledge_index_id"),
            },
            "query": query,
            "top_k": top_k,
        }

    def reconcile_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        dispatched = self.upsert_index_payload(payload)
        return DispatchResult(
            adapter=dispatched.adapter,
            operation="reconcile_index_payload",
            status=dispatched.status,
            details={**dispatched.details, "reconciled": True, "mode": "local"},
            error_code=dispatched.error_code,
            error_message=dispatched.error_message,
            retryable=dispatched.retryable,
            retry_after_seconds=dispatched.retry_after_seconds,
        )


MAX_QDRANT_RESPONSE_BYTES = 4 * 1024 * 1024


def _qdrant_request_operation(method: str, path: str) -> str:
    """Map a private Qdrant path to a stable operation without exporting identifiers."""

    path_only = path.partition("?")[0]
    segments = path_only.strip("/").split("/")
    normalized_method = method.upper()
    if len(segments) == 2 and segments[0] == "collections":
        if normalized_method == "GET":
            return "collection.get"
        if normalized_method == "PUT":
            return "collection.create"
    if (
        len(segments) == 3
        and segments[0] == "collections"
        and segments[2] == "points"
        and normalized_method == "PUT"
    ):
        return "points.upsert"
    if len(segments) == 4 and segments[0] == "collections" and segments[2] == "points":
        if segments[3] == "search" and normalized_method == "POST":
            return "points.search"
        if normalized_method == "GET":
            return "point.get"
    return "other"


class RealQdrantIndexClient:
    def __init__(
        self,
        base_url: str | None = None,
        vector_size: int | None = None,
        api_key: str | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("QDRANT_URL") or "http://127.0.0.1:6333"
        ).rstrip("/")
        configured_size = vector_size or int(os.environ.get("QDRANT_VECTOR_SIZE", "8"))
        self.embedding_provider = embedding_provider or build_embedding_provider(
            dimension=configured_size
        )
        if self.embedding_provider.dimension != configured_size:
            raise EmbeddingConfigurationError("QDRANT_VECTOR_SIZE must match EMBEDDING_DIMENSION")
        self.vector_size = self.embedding_provider.dimension
        self.distance = QDRANT_DISTANCE
        self.embedding_space_fingerprint = embedding_space_v1_fingerprint(
            self.embedding_provider,
            distance=self.distance,
        )
        self.api_key = api_key or os.environ.get("QDRANT_API_KEY")

    def upsert_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("qdrant", "upsert_payload", payload)
        if failure:
            return failure
        qdrant_payload = _qdrant_payload(payload)
        missing_fields = _validate_qdrant_payload(qdrant_payload)
        if missing_fields:
            return _invalid_qdrant_payload(missing_fields)

        collection = str(qdrant_payload["collection"])
        point_id = _stable_uuid(
            qdrant_payload,
            "tenant_id",
            "project_id",
            "trace_id",
            "knowledge_index_id",
            "knowledge_source_id",
            "source_id",
            "source_type",
            "version",
            "collection",
        )
        recorded_qdrant_payload = {
            **qdrant_payload,
            EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
        }
        try:
            document_text = _embedding_document_text(
                payload,
                qdrant_payload,
                provider=self.embedding_provider,
            )
            vector = self.embedding_provider.embed(document_text, purpose="document")
            self._ensure_collection(collection)
            upsert_response = self._request(
                "PUT",
                f"/collections/{collection}/points?wait=true",
                {
                    "points": [
                        {
                            "id": point_id,
                            "vector": vector,
                            "payload": recorded_qdrant_payload,
                        }
                    ]
                },
            )
        except (EmbeddingConfigurationError, EmbeddingResponseError) as exc:
            return DispatchResult(
                adapter="qdrant",
                operation="upsert_payload",
                status="failed",
                details={
                    "mode": "real",
                    "qdrant_url": self.base_url,
                    "collection": collection,
                    "embedding_provider": self.embedding_provider.provider_name,
                    "embedding_model": self.embedding_provider.model_name,
                    EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
                },
                error_code="EMBEDDING_GENERATION_FAILED",
                error_message=str(exc),
                retryable=True,
                retry_after_seconds=10,
            )
        except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
            return DispatchResult(
                adapter="qdrant",
                operation="upsert_payload",
                status="failed",
                details={"mode": "real", "qdrant_url": self.base_url, "collection": collection},
                error_code="QDRANT_UPSERT_FAILED",
                error_message=str(exc),
                retryable=True,
                retry_after_seconds=10,
            )

        result = upsert_response.get("result") if isinstance(upsert_response, dict) else {}
        if not isinstance(result, dict):
            result = {}
        return DispatchResult(
            adapter="qdrant",
            operation="upsert_payload",
            details={
                "mode": "real",
                "collection": collection,
                "source_id": qdrant_payload.get("source_id"),
                "point_ids": [point_id],
                "point_count": 1,
                "qdrant_payload": recorded_qdrant_payload,
                "qdrant_url": self.base_url,
                "vector_size": self.vector_size,
                "embedding_provider": self.embedding_provider.provider_name,
                "embedding_model": self.embedding_provider.model_name,
                "semantic_embedding": self.embedding_provider.is_semantic,
                EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
                "operation_id": result.get("operation_id"),
                "upsert_status": result.get("status") or upsert_response.get("status"),
            },
        )

    def search_index_payload(
        self, qdrant_payload: dict[str, Any], *, query: str, top_k: int
    ) -> dict[str, Any]:
        authorized_point_ids = validate_real_qdrant_authorized_point_ids(qdrant_payload)
        if (
            qdrant_payload.get(EMBEDDING_SPACE_FINGERPRINT_FIELD)
            != self.embedding_space_fingerprint
        ):
            raise ValueError("Qdrant embedding space fingerprint does not match")
        collection = str(qdrant_payload["collection"])
        filter_reference = real_qdrant_filter_reference(
            qdrant_payload,
            authorized_point_count=len(authorized_point_ids),
        )
        if not authorized_point_ids:
            return {
                "mode": "real_qdrant_authority_empty",
                "collection": collection,
                "qdrant_url": self.base_url,
                "points": [],
                "filter": filter_reference,
                "vector_size": self.vector_size,
                "embedding_provider": self.embedding_provider.provider_name,
                "embedding_model": self.embedding_provider.model_name,
                "semantic_embedding": self.embedding_provider.is_semantic,
                EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
            }
        filter_must: list[dict[str, Any]] = [
            {"key": "tenant_id", "match": {"value": qdrant_payload["tenant_id"]}},
            {"key": "project_id", "match": {"value": qdrant_payload["project_id"]}},
        ]
        if qdrant_payload.get("knowledge_index_id"):
            filter_must.append(
                {
                    "key": "knowledge_index_id",
                    "match": {"value": qdrant_payload["knowledge_index_id"]},
                }
            )
        filter_must.extend(
            [
                {
                    "key": EMBEDDING_SPACE_FINGERPRINT_FIELD,
                    "match": {"value": self.embedding_space_fingerprint},
                },
                {"has_id": authorized_point_ids},
            ]
        )
        search_payload = {
            "vector": self.embedding_provider.embed(query, purpose="query"),
            "limit": top_k,
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": filter_must},
        }
        result = self._request("POST", f"/collections/{collection}/points/search", search_payload)
        points = result.get("result") if isinstance(result, dict) else []
        return {
            "mode": "real_qdrant",
            "collection": collection,
            "qdrant_url": self.base_url,
            "points": points if isinstance(points, list) else [],
            "filter": filter_reference,
            "vector_size": self.vector_size,
            "embedding_provider": self.embedding_provider.provider_name,
            "embedding_model": self.embedding_provider.model_name,
            "semantic_embedding": self.embedding_provider.is_semantic,
            EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
        }

    def reconcile_index_payload(self, payload: dict[str, Any]) -> DispatchResult:
        qdrant_payload = _qdrant_payload(payload)
        missing_fields = _validate_qdrant_payload(qdrant_payload)
        if missing_fields:
            return _invalid_qdrant_payload(missing_fields)
        collection = str(qdrant_payload["collection"])
        point_id = _stable_uuid(
            qdrant_payload,
            "tenant_id",
            "project_id",
            "trace_id",
            "knowledge_index_id",
            "knowledge_source_id",
            "source_id",
            "source_type",
            "version",
            "collection",
        )
        recorded_qdrant_payload = {
            **qdrant_payload,
            EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
        }
        try:
            response = self._request(
                "GET", f"/collections/{collection}/points/{quote(point_id, safe='')}"
            )
        except HTTPError as exc:
            if exc.code == 404:
                return _reconciliation_not_found(
                    "qdrant",
                    "reconcile_index_payload",
                    details={"collection": collection, "point_id": point_id},
                )
            return _reconciliation_failure(
                "qdrant",
                "reconcile_index_payload",
                "QDRANT_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"collection": collection, "point_id": point_id},
            )
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            return _reconciliation_failure(
                "qdrant",
                "reconcile_index_payload",
                "QDRANT_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"collection": collection, "point_id": point_id},
            )
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or str(result.get("id") or "") != point_id:
            return _reconciliation_not_found(
                "qdrant",
                "reconcile_index_payload",
                details={"collection": collection, "point_id": point_id},
            )
        return DispatchResult(
            adapter="qdrant",
            operation="reconcile_index_payload",
            details={
                "mode": "real",
                "reconciled": True,
                "collection": collection,
                "point_id": point_id,
                "point_ids": [point_id],
                "point_count": 1,
                "qdrant_payload": recorded_qdrant_payload,
                EMBEDDING_SPACE_FINGERPRINT_FIELD: self.embedding_space_fingerprint,
            },
        )

    def _ensure_collection(self, collection: str) -> None:
        try:
            self._request("GET", f"/collections/{collection}")
            return
        except HTTPError as exc:
            if exc.code != 404:
                raise
        self._request(
            "PUT",
            f"/collections/{collection}",
            {"vectors": {"size": self.vector_size, "distance": self.distance}},
        )

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with internal_span(
            "qdrant.request",
            attributes={
                "auris.qdrant.operation": _qdrant_request_operation(method, path),
                "http.request.method": method.upper(),
            },
        ):
            with urlopen(request, timeout=5) as response:
                raw_bytes = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
        if len(raw_bytes) > MAX_QDRANT_RESPONSE_BYTES:
            raise ValueError("qdrant response is too large")
        raw = raw_bytes.decode("utf-8")
        return json.loads(raw) if raw else {}


class LocalExternalCallbackClient:
    def send_signed_callback(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("external_callback", "send_signed_callback", payload)
        if failure:
            return failure
        callback_receipt_id = _stable_receipt_id(
            "callback_receipt",
            payload,
            "run_id",
            "target",
            "dispatch_idempotency_key",
        )
        return DispatchResult(
            adapter="external_callback",
            operation="send_signed_callback",
            details={
                "callback_receipt_id": callback_receipt_id,
                "target": payload.get("target"),
                "idempotency_key": payload.get("dispatch_idempotency_key")
                or payload.get("idempotency_key"),
                "fencing_token": payload.get("outbox_fencing_token"),
                "signature_mode": "mock-hmac-sha256",
                "signature_id": _stable_receipt_id("sig", payload, "run_id", "target"),
            },
        )

    def reconcile_callback(self, payload: dict[str, Any]) -> DispatchResult:
        dispatched = self.send_signed_callback(payload)
        return DispatchResult(
            adapter=dispatched.adapter,
            operation="reconcile_callback",
            status=dispatched.status,
            details={**dispatched.details, "reconciled": True, "mode": "local"},
            error_code=dispatched.error_code,
            error_message=dispatched.error_message,
            retryable=dispatched.retryable,
            retry_after_seconds=dispatched.retry_after_seconds,
        )


_CALLBACK_PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production", "release"})
_CALLBACK_RECEIPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_CALLBACK_RESPONSE_BYTES = 1_048_576


class _ExternalCallbackSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class _ValidatedCallbackTarget:
    url: str
    scheme: str
    host: str
    port: int
    request_target: str
    resolved_addresses: tuple[str, ...]

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        return host if self.port == default_port else f"{host}:{self.port}"


def _normalize_callback_hostname(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if (
        not host
        or any(character.isspace() for character in host)
        or any(character in host for character in "/@?#\\%")
    ):
        raise _ExternalCallbackSecurityError("callback hostname is invalid")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError as exc:
        if ":" in host:
            raise _ExternalCallbackSecurityError("callback hostname is invalid") from exc
        try:
            normalized = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise _ExternalCallbackSecurityError("callback hostname is invalid") from exc
        if any(not label or len(label) > 63 for label in normalized.split(".")):
            raise _ExternalCallbackSecurityError("callback hostname is invalid") from None
        return normalized


def _callback_ip_is_forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _callback_ip_is_forbidden(mapped)
    return not address.is_global


def _callback_epoch_time() -> int:
    return int(datetime.now(UTC).timestamp())


class RealExternalCallbackClient:
    def __init__(
        self,
        callback_url: str | None = None,
        secret: str | None = None,
        app_env: str | None = None,
        allowed_hosts: str | tuple[str, ...] | list[str] | None = None,
        key_bindings: str | None = None,
        active_key_id: str | None = None,
        legacy_hmac_enabled: bool | None = None,
        signature_tolerance_seconds: int = 300,
        clock: Callable[[], int | float] | None = None,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.callback_url = (
            callback_url
            or os.environ.get("EXTERNAL_CALLBACK_URL")
            or os.environ.get("AURIS_EXTERNAL_CALLBACK_URL")
            or "http://127.0.0.1:8089/callbacks/platform"
        ).strip()
        self.app_env = (app_env or os.environ.get("APP_ENV") or "local").strip().lower()
        configured_secret = (
            secret
            or os.environ.get("EXTERNAL_CALLBACK_SECRET")
            or os.environ.get("AURIS_EXTERNAL_CALLBACK_SECRET")
            or "auris-dev-callback-secret"
        )
        configured_bindings = (
            key_bindings
            if key_bindings is not None
            else os.environ.get("EXTERNAL_CALLBACK_KEY_BINDINGS", "")
        ).strip()
        configured_active_key_id = (
            active_key_id
            if active_key_id is not None
            else os.environ.get("EXTERNAL_CALLBACK_ACTIVE_KEY_ID", "")
        ).strip()
        env_legacy = os.environ.get("EXTERNAL_CALLBACK_LEGACY_HMAC_ENABLED", "").strip().lower()
        self.legacy_hmac_enabled = (
            legacy_hmac_enabled
            if legacy_hmac_enabled is not None
            else env_legacy in {"1", "true", "yes", "on"}
        )
        self._explicit_keyring = bool(configured_bindings)
        self.keyring: CallbackKeyring | None
        if configured_bindings:
            self.keyring = parse_callback_keyring(
                configured_bindings,
                active_key_id=configured_active_key_id,
            )
        elif self.legacy_hmac_enabled or (secret is not None and not self.production_mode):
            legacy_secret = configured_secret.strip().encode("utf-8")
            self.keyring = CallbackKeyring(
                (
                    CallbackKeyBinding(
                        key_id="local-dev-callback",
                        secret=legacy_secret,
                        state=CallbackKeyState.ACTIVE,
                    ),
                ),
                active_key_id="local-dev-callback",
            )
        else:
            self.keyring = None
        self.signature_tolerance_seconds = signature_tolerance_seconds
        self._clock = clock or _callback_epoch_time
        self._nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        configured_hosts = (
            allowed_hosts
            if allowed_hosts is not None
            else os.environ.get("EXTERNAL_CALLBACK_ALLOWED_HOSTS", "")
        )
        raw_hosts = (
            configured_hosts.replace("\n", ",").split(",")
            if isinstance(configured_hosts, str)
            else configured_hosts
        )
        self.allowed_hosts = frozenset(
            _normalize_callback_hostname(host) for host in raw_hosts if host.strip()
        )

    @property
    def production_mode(self) -> bool:
        return self.app_env in _CALLBACK_PRODUCTION_ENVIRONMENTS

    def send_signed_callback(self, payload: dict[str, Any]) -> DispatchResult:
        failure = _maybe_failure("external_callback", "send_signed_callback", payload)
        if failure:
            return failure

        body_bytes = self._callback_body_bytes(payload)
        request_sha256 = hashlib.sha256(body_bytes).hexdigest()
        idempotency_key = self._idempotency_key(payload)
        signature_id = (
            self.keyring.active_key.key_id if self.keyring is not None else "unconfigured"
        )
        signature = ""
        signed_request: CallbackSignatureRequest | None = None
        try:
            signed_request, signature = self._signed_callback_request(
                body=body_bytes,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            signature_id = signed_request.key_id
            headers = {
                "Content-Type": "application/json",
                "X-Auris-Signature-Version": signed_request.version,
                "X-Auris-Timestamp": str(signed_request.timestamp),
                "X-Auris-Nonce": signed_request.nonce,
                "X-Auris-Key-Id": signed_request.key_id,
                "X-Auris-Signature": signature,
                "X-Auris-Signature-Mode": "hmac-sha256-v2",
                "X-Auris-Idempotency-Key": idempotency_key,
                "X-Auris-Tenant-Id": str(payload.get("tenant_id") or ""),
                "X-Auris-Project-Id": str(payload.get("project_id") or ""),
                "X-Auris-Trace-Id": str(payload.get("trace_id") or ""),
                "X-Auris-Run-Id": str(payload.get("run_id") or ""),
                "X-Auris-Fencing-Token": str(payload.get("outbox_fencing_token") or ""),
            }
            response = self._request(body_bytes, headers)
        except (CallbackSignatureError, _ExternalCallbackSecurityError) as exc:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url_sha256": hashlib.sha256(
                        self.callback_url.encode("utf-8")
                    ).hexdigest(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_SECURITY_REJECTED",
                error_message=str(exc),
                retryable=False,
            )
        except HTTPError as exc:
            response_body = exc.read()
            return self._http_error_result(
                payload=payload,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                signature_id=signature_id,
                status_code=exc.code,
                response_body=response_body,
            )
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_SEND_FAILED",
                error_message=str(exc),
                retryable=True,
                retry_after_seconds=10,
            )

        response_body_value = response.get("body") if isinstance(response, dict) else b""
        response_body = response_body_value if isinstance(response_body_value, bytes) else b""
        status_code = self._status_code(response)
        if 300 <= status_code < 400:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "status_code": status_code,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_REDIRECT_REJECTED",
                error_message="external callback redirects are forbidden",
                retryable=False,
            )
        if not 200 <= status_code < 300:
            return self._http_error_result(
                payload=payload,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                signature_id=signature_id,
                status_code=status_code,
                response_body=response_body,
            )

        response_sha256 = hashlib.sha256(response_body).hexdigest() if response_body else None
        try:
            response_json = json.loads(response_body.decode("utf-8")) if response_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "status_code": status_code,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_RESPONSE_INVALID",
                error_message=f"callback endpoint returned invalid JSON: {exc}",
                retryable=True,
                retry_after_seconds=10,
            )

        response_data = response_json.get("data") if isinstance(response_json, dict) else {}
        if not isinstance(response_data, dict):
            response_data = {}
        body_receipt_id = str(response_data.get("callback_receipt_id") or "")
        header_receipt_id = self._response_header(
            response.get("headers"), "X-Auris-Callback-Receipt-Id"
        )
        if body_receipt_id and header_receipt_id and body_receipt_id != header_receipt_id:
            return self._invalid_receipt_result(
                payload,
                idempotency_key,
                request_sha256,
                response_sha256,
                status_code,
                signature_id,
                "callback receipt identifiers disagree",
            )
        callback_receipt_id = body_receipt_id or header_receipt_id
        if not callback_receipt_id:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "status_code": status_code,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_RECEIPT_MISSING",
                error_message="callback endpoint did not return callback_receipt_id",
                retryable=True,
                retry_after_seconds=10,
            )
        try:
            receipt_target = self._validate_receipt_url(
                response_data.get("receipt_url"),
                callback_receipt_id,
                resolve=self.production_mode,
            )
        except _ExternalCallbackSecurityError as exc:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "status_code": status_code,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                    "receipt_url_sha256": hashlib.sha256(
                        str(response_data.get("receipt_url") or "").encode("utf-8")
                    ).hexdigest(),
                },
                error_code="EXTERNAL_CALLBACK_RECEIPT_URL_INVALID",
                error_message=str(exc),
                retryable=False,
            )

        protocol_receipt = redact_structured_value(
            response_data, field_name="external_callback_protocol_receipt"
        )
        if not isinstance(protocol_receipt, dict):
            protocol_receipt = {}
        return DispatchResult(
            adapter="external_callback",
            operation="send_signed_callback",
            details={
                "mode": "real",
                "callback_url": self._callback_url_for_audit(),
                "http_method": "POST",
                "status_code": status_code,
                "callback_receipt_id": callback_receipt_id,
                "remote_trace_id": protocol_receipt.get("remote_trace_id"),
                "receipt_url": receipt_target.url,
                "target": payload.get("target"),
                "idempotency_key": idempotency_key,
                "tenant_id": payload.get("tenant_id"),
                "project_id": payload.get("project_id"),
                "trace_id": payload.get("trace_id"),
                "run_id": payload.get("run_id"),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "signature_key_id": signature_id,
                "signature_mode": "hmac-sha256-v2",
                "signature_version": "v2",
                "fencing_token": payload.get("outbox_fencing_token"),
                "signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
                "protocol_receipt": protocol_receipt,
            },
        )

    def reconcile_callback(self, payload: dict[str, Any]) -> DispatchResult:
        idempotency_key = self._idempotency_key(payload)
        receipt_id = f"callback_receipt_{hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]}"
        try:
            receipt_url = self._expected_receipt_url(receipt_id)
            response = self._request_url(receipt_url, method="GET")
        except _ExternalCallbackSecurityError as exc:
            return DispatchResult(
                adapter="external_callback",
                operation="reconcile_callback",
                status="failed",
                details={"callback_receipt_id": receipt_id},
                error_code="EXTERNAL_CALLBACK_SECURITY_REJECTED",
                error_message=str(exc),
                retryable=False,
            )
        except HTTPError as exc:
            if exc.code == 404:
                return _reconciliation_not_found(
                    "external_callback",
                    "reconcile_callback",
                    details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
                )
            if 300 <= exc.code < 400:
                return DispatchResult(
                    adapter="external_callback",
                    operation="reconcile_callback",
                    status="failed",
                    details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
                    error_code="EXTERNAL_CALLBACK_REDIRECT_REJECTED",
                    error_message="external callback receipt redirects are forbidden",
                    retryable=False,
                )
            return _reconciliation_failure(
                "external_callback",
                "reconcile_callback",
                "EXTERNAL_CALLBACK_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )
        except (OSError, URLError, TimeoutError, ValueError) as exc:
            return _reconciliation_failure(
                "external_callback",
                "reconcile_callback",
                "EXTERNAL_CALLBACK_RECONCILIATION_FAILED",
                str(exc),
                retryable=True,
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )

        status_code = self._status_code(response)
        if 300 <= status_code < 400:
            return DispatchResult(
                adapter="external_callback",
                operation="reconcile_callback",
                status="failed",
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
                error_code="EXTERNAL_CALLBACK_REDIRECT_REJECTED",
                error_message="external callback receipt redirects are forbidden",
                retryable=False,
            )
        if status_code == 404:
            return _reconciliation_not_found(
                "external_callback",
                "reconcile_callback",
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )
        if not 200 <= status_code < 300:
            return _reconciliation_failure(
                "external_callback",
                "reconcile_callback",
                "EXTERNAL_CALLBACK_RECONCILIATION_FAILED",
                f"callback receipt endpoint returned HTTP {status_code}",
                retryable=status_code >= 500 or status_code in {408, 425, 429},
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )
        raw_value = response.get("body") if isinstance(response, dict) else b""
        raw = raw_value if isinstance(raw_value, bytes) else b""
        try:
            response_json = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _reconciliation_failure(
                "external_callback",
                "reconcile_callback",
                "EXTERNAL_CALLBACK_RECONCILIATION_RESPONSE_INVALID",
                str(exc),
                retryable=True,
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )
        receipt = response_json.get("data") if isinstance(response_json, dict) else None
        if not isinstance(receipt, dict) or receipt.get("callback_receipt_id") != receipt_id:
            return _reconciliation_not_found(
                "external_callback",
                "reconcile_callback",
                details={"callback_receipt_id": receipt_id, "receipt_url": receipt_url},
            )

        expected_request_sha256 = hashlib.sha256(self._callback_body_bytes(payload)).hexdigest()
        expected_fields = {
            "tenant_id": payload.get("tenant_id"),
            "project_id": payload.get("project_id"),
            "trace_id": payload.get("trace_id"),
            "run_id": payload.get("run_id"),
            "target": payload.get("target"),
            "idempotency_key": idempotency_key,
            "request_sha256": expected_request_sha256,
        }
        mismatched_fields = sorted(
            field
            for field, expected in expected_fields.items()
            if expected is not None and str(receipt.get(field) or "") != str(expected)
        )
        if mismatched_fields:
            return DispatchResult(
                adapter="external_callback",
                operation="reconcile_callback",
                status="failed",
                details={
                    "callback_receipt_id": receipt_id,
                    "receipt_url": receipt_url,
                    "mismatched_fields": mismatched_fields,
                },
                error_code="EXTERNAL_CALLBACK_RECEIPT_INVALID",
                error_message="callback receipt does not match the locally derived request",
                retryable=False,
            )
        remote_trace_id = redact_structured_value(
            receipt.get("remote_trace_id"), field_name="remote_trace_id"
        )
        return DispatchResult(
            adapter="external_callback",
            operation="reconcile_callback",
            details={
                "mode": "real",
                "reconciled": True,
                "callback_receipt_id": receipt_id,
                "receipt_url": receipt_url,
                "remote_trace_id": remote_trace_id,
                "request_sha256": expected_request_sha256,
                "tenant_id": payload.get("tenant_id"),
                "project_id": payload.get("project_id"),
                "trace_id": payload.get("trace_id"),
                "run_id": payload.get("run_id"),
                "idempotency_key": idempotency_key,
            },
        )

    def _idempotency_key(self, payload: dict[str, Any]) -> str:
        return str(
            payload.get("dispatch_idempotency_key")
            or payload.get("idempotency_key")
            or _stable_receipt_id("cb_key", payload, "run_id", "target")
        )

    def _callback_url_for_audit(self) -> str:
        parsed = urlparse(self.callback_url)
        return parsed._replace(query="", fragment="").geturl()

    def _callback_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        callback_payload = payload.get("payload_template")
        if not isinstance(callback_payload, dict):
            callback_payload = {}
        redacted_payload = redact_structured_value(
            callback_payload, field_name="external_callback_payload"
        )
        if not isinstance(redacted_payload, dict):
            redacted_payload = {}
        return {
            "target": payload.get("target"),
            "tenant_id": payload.get("tenant_id"),
            "project_id": payload.get("project_id"),
            "trace_id": payload.get("trace_id"),
            "run_id": payload.get("run_id"),
            "idempotency_key": self._idempotency_key(payload),
            "payload": redacted_payload,
        }

    def _callback_body_bytes(self, payload: dict[str, Any]) -> bytes:
        return json.dumps(
            self._callback_body(payload),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _signed_callback_request(
        self,
        *,
        body: bytes,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[CallbackSignatureRequest, str]:
        if self.production_mode and self.legacy_hmac_enabled:
            raise _ExternalCallbackSecurityError(
                "legacy callback HMAC mode is forbidden in production"
            )
        if self.production_mode and not self._explicit_keyring:
            raise _ExternalCallbackSecurityError(
                "explicit callback key bindings are required in production"
            )
        if self.keyring is None:
            raise _ExternalCallbackSecurityError("callback signing keyring is not configured")
        if not 1 <= self.signature_tolerance_seconds <= 900:
            raise _ExternalCallbackSecurityError(
                "callback signature tolerance must be between 1 and 900 seconds"
            )
        parsed = urlparse(self.callback_url)
        timestamp = int(self._clock())
        request = CallbackSignatureRequest(
            method="POST",
            path=parsed.path or "/",
            query=parsed.query,
            tenant_id=str(payload.get("tenant_id") or ""),
            project_id=str(payload.get("project_id") or ""),
            idempotency_key=idempotency_key,
            timestamp=timestamp,
            nonce=self._nonce_factory(),
            key_id=self.keyring.active_key.key_id,
            body=body,
        )
        return request, sign_callback(request, self.keyring)

    def _request(self, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
        return self._request_url(
            self.callback_url,
            method="POST",
            body=body,
            headers=headers,
            purpose="callback",
        )

    def _request_url(
        self,
        url: str,
        *,
        method: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        purpose: str = "receipt",
    ) -> dict[str, Any]:
        target = self._validate_outbound_url(url, purpose=purpose, resolve=True)
        return self._perform_http_request(
            target,
            method=method,
            body=body,
            headers=headers or {},
        )

    def _perform_http_request(
        self,
        target: _ValidatedCallbackTarget,
        *,
        method: str,
        body: bytes | None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        if not target.resolved_addresses:
            raise _ExternalCallbackSecurityError("callback target was not resolved")
        selected_address = target.resolved_addresses[0]
        connection: HTTPConnection
        if target.scheme == "https":
            connection = HTTPSConnection(target.host, target.port, timeout=5)
        else:
            connection = HTTPConnection(target.host, target.port, timeout=5)

        def create_pinned_connection(
            _address: tuple[str, int],
            timeout: float | None = None,
            source_address: tuple[str, int] | None = None,
        ) -> socket.socket:
            return socket.create_connection(
                (selected_address, target.port),
                timeout,
                source_address,
            )

        connection.__dict__["_create_connection"] = create_pinned_connection
        request_headers = {
            key: value
            for key, value in headers.items()
            if key.casefold() not in {"traceparent", "tracestate"}
        }
        request_headers["Host"] = target.authority
        with safe_http_client_span(
            method=method,
            scheme=target.scheme,
            host=target.host,
            port=target.port,
        ) as (span, trace_headers):
            request_headers.update(trace_headers)
            try:
                connection.request(
                    method,
                    target.request_target,
                    body=body,
                    headers=request_headers,
                )
                response = connection.getresponse()
                record_safe_http_status(span, response.status)
                response_body = response.read(_MAX_CALLBACK_RESPONSE_BYTES + 1)
                if len(response_body) > _MAX_CALLBACK_RESPONSE_BYTES:
                    raise _ExternalCallbackSecurityError("callback response exceeds the size limit")
                return {
                    "status_code": response.status,
                    "headers": dict(response.getheaders()),
                    "body": response_body,
                }
            finally:
                connection.close()

    def _validate_outbound_url(
        self,
        url: str,
        *,
        purpose: str,
        resolve: bool,
    ) -> _ValidatedCallbackTarget:
        if not isinstance(url, str) or not url or url != url.strip():
            raise _ExternalCallbackSecurityError(f"{purpose} URL must be a non-empty absolute URL")
        if any(ord(character) < 32 for character in url):
            raise _ExternalCallbackSecurityError(f"{purpose} URL contains control characters")
        try:
            parsed = urlparse(url)
            scheme = parsed.scheme.lower()
            host = _normalize_callback_hostname(parsed.hostname or "")
            port = parsed.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise _ExternalCallbackSecurityError(f"{purpose} URL is invalid") from exc
        if scheme not in {"http", "https"}:
            raise _ExternalCallbackSecurityError(f"{purpose} URL must use HTTP or HTTPS")
        if self.production_mode and scheme != "https":
            raise _ExternalCallbackSecurityError(f"{purpose} URL must use HTTPS in production")
        if parsed.username is not None or parsed.password is not None:
            raise _ExternalCallbackSecurityError(f"{purpose} URL must not contain credentials")
        if parsed.fragment:
            raise _ExternalCallbackSecurityError(f"{purpose} URL must not contain a fragment")
        if not 1 <= port <= 65535:
            raise _ExternalCallbackSecurityError(f"{purpose} URL port is invalid")
        if self.production_mode and self.legacy_hmac_enabled:
            raise _ExternalCallbackSecurityError(
                "legacy callback HMAC mode is forbidden in production"
            )
        if self.production_mode and not self._explicit_keyring:
            raise _ExternalCallbackSecurityError(
                "explicit callback key bindings are required in production"
            )
        if self.production_mode and not self.allowed_hosts:
            raise _ExternalCallbackSecurityError(
                "EXTERNAL_CALLBACK_ALLOWED_HOSTS is required in production"
            )
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise _ExternalCallbackSecurityError(f"{purpose} host is not allowlisted")

        resolved_addresses: tuple[str, ...] = ()
        try:
            literal_address = ipaddress.ip_address(host)
        except ValueError:
            literal_address = None
        if literal_address is not None:
            if self.production_mode and _callback_ip_is_forbidden(literal_address):
                raise _ExternalCallbackSecurityError(
                    f"{purpose} target must not use a non-public IP address"
                )
            resolved_addresses = (literal_address.compressed,)
        elif resolve:
            try:
                address_info = socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
            except OSError as exc:
                raise _ExternalCallbackSecurityError(
                    f"{purpose} hostname could not be resolved"
                ) from exc
            addresses: list[str] = []
            for entry in address_info:
                try:
                    address = ipaddress.ip_address(entry[4][0])
                except (ValueError, IndexError, TypeError) as exc:
                    raise _ExternalCallbackSecurityError(
                        f"{purpose} hostname returned an invalid address"
                    ) from exc
                if self.production_mode and _callback_ip_is_forbidden(address):
                    raise _ExternalCallbackSecurityError(
                        f"{purpose} hostname resolved to a non-public IP address"
                    )
                if address.compressed not in addresses:
                    addresses.append(address.compressed)
            if not addresses:
                raise _ExternalCallbackSecurityError(
                    f"{purpose} hostname did not resolve to an address"
                )
            resolved_addresses = tuple(addresses)

        request_target = parsed.path or "/"
        if parsed.params:
            request_target = f"{request_target};{parsed.params}"
        if parsed.query:
            request_target = f"{request_target}?{parsed.query}"
        return _ValidatedCallbackTarget(
            url=url,
            scheme=scheme,
            host=host,
            port=port,
            request_target=request_target,
            resolved_addresses=resolved_addresses,
        )

    def _expected_receipt_url(self, receipt_id: str) -> str:
        if not _CALLBACK_RECEIPT_ID_PATTERN.fullmatch(receipt_id):
            raise _ExternalCallbackSecurityError("callback receipt identifier is invalid")
        callback_target = self._validate_outbound_url(
            self.callback_url,
            purpose="callback",
            resolve=False,
        )
        return (
            f"{callback_target.scheme}://{callback_target.authority}"
            f"/receipts/{quote(receipt_id, safe='')}"
        )

    def _validate_receipt_url(
        self,
        receipt_url: object,
        receipt_id: str,
        *,
        resolve: bool,
    ) -> _ValidatedCallbackTarget:
        if not isinstance(receipt_url, str) or not receipt_url:
            raise _ExternalCallbackSecurityError("callback receipt URL is required")
        expected_url = self._expected_receipt_url(receipt_id)
        expected_target = self._validate_outbound_url(
            expected_url,
            purpose="receipt",
            resolve=False,
        )
        receipt_target = self._validate_outbound_url(
            receipt_url,
            purpose="receipt",
            resolve=resolve,
        )
        if (
            receipt_target.scheme,
            receipt_target.host,
            receipt_target.port,
            receipt_target.request_target,
        ) != (
            expected_target.scheme,
            expected_target.host,
            expected_target.port,
            expected_target.request_target,
        ):
            raise _ExternalCallbackSecurityError(
                "callback receipt URL must match the locally derived same-origin URL"
            )
        return receipt_target

    def _http_error_result(
        self,
        *,
        payload: dict[str, Any],
        idempotency_key: str,
        request_sha256: str,
        signature_id: str,
        status_code: int,
        response_body: bytes,
    ) -> DispatchResult:
        if 300 <= status_code < 400:
            return DispatchResult(
                adapter="external_callback",
                operation="send_signed_callback",
                status="failed",
                details={
                    "mode": "real",
                    "callback_url": self._callback_url_for_audit(),
                    "target": payload.get("target"),
                    "idempotency_key": idempotency_key,
                    "request_sha256": request_sha256,
                    "status_code": status_code,
                    "signature_key_id": signature_id,
                    "signature_mode": "hmac-sha256-v2",
                    "signature_version": "v2",
                },
                error_code="EXTERNAL_CALLBACK_REDIRECT_REJECTED",
                error_message="external callback redirects are forbidden",
                retryable=False,
            )
        response_sha256 = hashlib.sha256(response_body).hexdigest() if response_body else None
        retryable = status_code >= 500 or status_code in {0, 408, 409, 425, 429}
        return DispatchResult(
            adapter="external_callback",
            operation="send_signed_callback",
            status="failed",
            details={
                "mode": "real",
                "callback_url": self._callback_url_for_audit(),
                "target": payload.get("target"),
                "idempotency_key": idempotency_key,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "status_code": status_code,
                "signature_key_id": signature_id,
                "signature_mode": "hmac-sha256-v2",
                "signature_version": "v2",
            },
            error_code="EXTERNAL_CALLBACK_HTTP_ERROR",
            error_message=f"callback endpoint returned HTTP {status_code}",
            retryable=retryable,
            retry_after_seconds=10 if retryable else None,
        )

    def _invalid_receipt_result(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        request_sha256: str,
        response_sha256: str | None,
        status_code: int,
        signature_id: str,
        message: str,
    ) -> DispatchResult:
        return DispatchResult(
            adapter="external_callback",
            operation="send_signed_callback",
            status="failed",
            details={
                "mode": "real",
                "callback_url": self._callback_url_for_audit(),
                "target": payload.get("target"),
                "idempotency_key": idempotency_key,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "status_code": status_code,
                "signature_key_id": signature_id,
                "signature_mode": "hmac-sha256-v2",
                "signature_version": "v2",
            },
            error_code="EXTERNAL_CALLBACK_RECEIPT_INVALID",
            error_message=message,
            retryable=False,
        )

    @staticmethod
    def _status_code(response: object) -> int:
        if not isinstance(response, dict):
            return 0
        try:
            return int(response.get("status_code") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _response_header(headers: object, name: str) -> str:
        if not isinstance(headers, dict):
            return ""
        lowered_name = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered_name:
                return str(value or "")
        return ""


def configured_real_qdrant_client() -> RealQdrantIndexClient:
    """Build the real client from the validated runtime settings.

    Production Compose injects the API key through ``QDRANT_API_KEY_FILE``.
    ``Settings`` resolves that file safely; bypassing it here would silently
    create an unauthenticated Worker client while readiness remained green.
    """

    from app.core.config import get_settings

    runtime_settings = get_settings()
    return RealQdrantIndexClient(
        base_url=runtime_settings.qdrant_url,
        vector_size=runtime_settings.embedding_dimension,
        api_key=runtime_settings.qdrant_api_key,
    )


def _default_qdrant_client() -> QdrantIndexClient:
    if os.environ.get("AURIS_QDRANT_ADAPTER", "").lower() == "real":
        return configured_real_qdrant_client()
    return LocalQdrantIndexClient()


def _default_object_storage_client() -> ObjectStorageClient:
    from app.core.config import get_settings

    runtime_settings = get_settings()
    if runtime_settings.auris_object_storage_adapter.lower() == "real":
        return object_storage_client_for_provider(runtime_settings.object_storage_provider)
    return LocalObjectStorageClient()


def _default_dagster_client() -> DagsterClient:
    if os.environ.get("AURIS_DAGSTER_ADAPTER", "").lower() == "real":
        return RealDagsterClient(
            execution_mode=os.environ.get("AURIS_DAGSTER_EXECUTION_MODE") or None
        )
    return LocalDagsterClient()


def _default_external_callback_client() -> ExternalCallbackClient:
    from app.core.config import get_settings

    runtime_settings = get_settings()
    if runtime_settings.auris_external_callback_adapter.strip().lower() == "real":
        return RealExternalCallbackClient(
            callback_url=(
                os.environ.get("AURIS_EXTERNAL_CALLBACK_URL")
                or runtime_settings.external_callback_url
            ),
            secret=(
                (
                    os.environ.get("AURIS_EXTERNAL_CALLBACK_SECRET")
                    or runtime_settings.external_callback_secret
                )
                if runtime_settings.external_callback_legacy_hmac_enabled
                else None
            ),
            key_bindings=runtime_settings.external_callback_key_bindings,
            active_key_id=runtime_settings.external_callback_active_key_id,
            legacy_hmac_enabled=runtime_settings.external_callback_legacy_hmac_enabled,
            signature_tolerance_seconds=(
                runtime_settings.external_callback_signature_tolerance_seconds
            ),
            app_env=runtime_settings.app_env,
            allowed_hosts=runtime_settings.external_callback_allowed_hosts,
        )
    return LocalExternalCallbackClient()


@dataclass(frozen=True)
class AdapterRegistry:
    dagster: DagsterClient = field(default_factory=_default_dagster_client)
    object_storage: ObjectStorageClient = field(default_factory=_default_object_storage_client)
    qdrant: QdrantIndexClient = field(default_factory=_default_qdrant_client)
    external_callback: ExternalCallbackClient = field(
        default_factory=_default_external_callback_client
    )


def dispatch_event(
    event_type: str,
    aggregate_type: str,
    payload: dict[str, Any],
    registry: AdapterRegistry | None = None,
) -> DispatchResult:
    adapters = registry or AdapterRegistry()
    if event_type == "task_run.cancel_requested":
        if payload.get("engine_dispatch_required") is False:
            return DispatchResult(
                adapter="control_plane",
                operation="cancel_pending_run",
                details={
                    "external_run_id": None,
                    "dagster_status": "CANCELED",
                    "mode": "local_control",
                },
            )
        external_run_id = str(payload.get("external_run_id") or "")
        if not external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="cancel_run",
                status="failed",
                error_code="TASK_RUN_ENGINE_BINDING_REQUIRED",
                error_message="Task-run cancellation requires an engine binding",
                retryable=False,
            )
        return adapters.dagster.cancel_run(external_run_id)
    if event_type == "task_run.status_sync_requested":
        external_run_id = str(payload.get("external_run_id") or "")
        if not external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                error_code="TASK_RUN_ENGINE_BINDING_REQUIRED",
                error_message="Task-run status synchronization requires an engine binding",
                retryable=False,
            )
        return adapters.dagster.get_run_status(external_run_id)
    if event_type in DAGSTER_RUN_REQUEST_EVENT_TYPES:
        return adapters.dagster.submit_run_request(payload)
    if event_type in {"knowledge_source.sync_requested", "knowledge_index.build_requested"}:
        return adapters.qdrant.upsert_index_payload(payload)
    if event_type == "voiceprint_enrollments.upserted" and payload.get("status") == "enrolled":
        return adapters.qdrant.upsert_index_payload(payload)
    if event_type in {"audio_ingest.requested", "export.requested"}:
        return adapters.object_storage.reserve_object(payload)
    if event_type == "external_callback.requested":
        return adapters.external_callback.send_signed_callback(payload)
    if event_type == "label_version.publish_requested":
        return DispatchResult(
            adapter="label_policy",
            operation="materialize_release",
            details={
                "label_version_id": payload.get("label_version_id"),
                "policy_evaluation_id": payload.get("release_policy_evaluation_id"),
            },
        )
    if event_type == "hotword_pack_version.rollback-requested":
        return DispatchResult(
            adapter="projection",
            operation="materialize_hotword_rollback",
            details={
                "pack_id": payload.get("pack_id"),
                "source_version_id": payload.get("source_version_id"),
                "target_version_id": payload.get("target_version_id"),
            },
        )
    subject = payload.get("subject")
    aggregate_id = (
        subject.get("id")
        if isinstance(subject, dict) and isinstance(subject.get("id"), str)
        else payload.get("aggregate_id") or payload.get("id")
    )
    return DispatchResult(
        adapter="projection",
        operation="record_event",
        details={
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        },
    )


def reconcile_event(
    event_type: str,
    aggregate_type: str,
    payload: dict[str, Any],
    registry: AdapterRegistry | None = None,
) -> DispatchResult:
    """Prove a prior remote side effect without repeating the write operation."""
    adapters = registry or AdapterRegistry()
    if event_type == "task_run.cancel_requested":
        if payload.get("engine_dispatch_required") is False:
            return DispatchResult(
                adapter="control_plane",
                operation="reconcile_cancel_pending_run",
                details={
                    "external_run_id": None,
                    "dagster_status": "CANCELED",
                    "mode": "local_control",
                    "reconciled": True,
                },
            )
        external_run_id = str(payload.get("external_run_id") or "")
        if not external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                error_code="TASK_RUN_ENGINE_BINDING_REQUIRED",
                error_message="Task-run cancellation reconciliation requires an engine binding",
                retryable=False,
            )
        return adapters.dagster.get_run_status(external_run_id)
    if event_type == "task_run.status_sync_requested":
        external_run_id = str(payload.get("external_run_id") or "")
        if not external_run_id:
            return DispatchResult(
                adapter="dagster",
                operation="run_status",
                status="failed",
                error_code="TASK_RUN_ENGINE_BINDING_REQUIRED",
                error_message="Task-run status synchronization requires an engine binding",
                retryable=False,
            )
        return adapters.dagster.get_run_status(external_run_id)
    if event_type in DAGSTER_RUN_REQUEST_EVENT_TYPES:
        return adapters.dagster.reconcile_run_request(payload)
    if event_type in {"knowledge_source.sync_requested", "knowledge_index.build_requested"}:
        return adapters.qdrant.reconcile_index_payload(payload)
    if event_type == "voiceprint_enrollments.upserted" and payload.get("status") == "enrolled":
        return adapters.qdrant.reconcile_index_payload(payload)
    if event_type in {"audio_ingest.requested", "export.requested"}:
        return adapters.object_storage.reconcile_object(payload)
    if event_type == "external_callback.requested":
        return adapters.external_callback.reconcile_callback(payload)
    if event_type == "label_version.publish_requested":
        return DispatchResult(
            adapter="label_policy",
            operation="reconcile_materialized_release",
            details={
                "reconciled": True,
                "label_version_id": payload.get("label_version_id"),
                "policy_evaluation_id": payload.get("release_policy_evaluation_id"),
            },
        )
    if event_type == "hotword_pack_version.rollback-requested":
        return DispatchResult(
            adapter="projection",
            operation="reconcile_hotword_rollback",
            details={
                "reconciled": True,
                "pack_id": payload.get("pack_id"),
                "source_version_id": payload.get("source_version_id"),
                "target_version_id": payload.get("target_version_id"),
            },
        )
    subject = payload.get("subject")
    aggregate_id = (
        subject.get("id")
        if isinstance(subject, dict) and isinstance(subject.get("id"), str)
        else payload.get("aggregate_id") or payload.get("id")
    )
    return DispatchResult(
        adapter="projection",
        operation="reconcile_recorded_event",
        details={
            "reconciled": True,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
        },
    )
