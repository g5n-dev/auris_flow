from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
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
