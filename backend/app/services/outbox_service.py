from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.logging import get_logger, log_event
from app.core.observability import current_trace_carrier
from app.models import OutboxEvent
from app.repositories.outbox_events import insert_or_get_event

logger = get_logger("outbox")

_RESERVED_ENVELOPE_FIELDS = frozenset(
    {
        "actor_id",
        "business_payload_sha256",
        "correlation_id",
        "data",
        "dispatch_idempotency_key",
        "event_id",
        "event_type",
        "event_version",
        "idempotency_key",
        "occurred_at",
        "otel_trace_context",
        "project_id",
        "request_id",
        "resource_version_identity",
        "subject",
        "tenant_id",
        "trace_id",
    }
)


class OutboxEventConflictError(RuntimeError):
    """The same business event/version was enqueued with different content."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _business_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _RESERVED_ENVELOPE_FIELDS}


def _resource_version_identity(
    business_payload: dict[str, Any],
    *,
    payload_sha256: str,
) -> tuple[str, Any]:
    version_fields = {
        key: business_payload[key]
        for key in sorted(business_payload)
        if key == "resource_version" or key.endswith("_resource_version")
    }
    if not version_fields and "version" in business_payload:
        version_fields = {"version": business_payload["version"]}
    if not version_fields:
        marker = f"payload_sha256:{payload_sha256}"
        return marker, marker

    marker = f"fields:{_canonical_json(version_fields)}"
    if "resource_version" in version_fields:
        display_value: Any = version_fields["resource_version"]
    elif len(version_fields) == 1:
        display_value = next(iter(version_fields.values()))
    else:
        display_value = marker
    return marker, display_value


def _business_event_identity(
    ctx: RequestContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    resource_version_identity: str,
) -> tuple[str, str]:
    digest = _sha256(
        {
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "resource_version": resource_version_identity,
        }
    )
    return f"evt_v1_{digest}", f"outbox_v1_{digest}"


def _assert_equivalent_event(
    event: OutboxEvent,
    *,
    tenant_id: str,
    project_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    business_event_id: str,
    payload_sha256: str,
) -> None:
    actual_payload_hash = event.payload.get("business_payload_sha256")
    equivalent = (
        event.tenant_id == tenant_id
        and event.project_id == project_id
        and event.event_type == event_type
        and event.aggregate_type == aggregate_type
        and event.aggregate_id == aggregate_id
        and event.payload.get("event_id") == business_event_id
        and actual_payload_hash == payload_sha256
    )
    if equivalent:
        return
    raise OutboxEventConflictError(
        "outbox resource version already exists with a different business payload"
    )


def enqueue_event(
    session: Session,
    ctx: RequestContext,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    business_payload = _business_payload(payload)
    payload_sha256 = _sha256(business_payload)
    resource_version_identity, resource_version = _resource_version_identity(
        business_payload,
        payload_sha256=payload_sha256,
    )
    business_event_id, dispatch_idempotency_key = _business_event_identity(
        ctx,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        resource_version_identity=resource_version_identity,
    )
    event_payload = {
        # Business fields are accepted first. Reserved delivery and scope fields below
        # are authoritative and cannot be replaced by caller-controlled JSON.
        **business_payload,
        "event_id": business_event_id,
        "event_version": "0.1",
        "event_type": event_type,
        "occurred_at": datetime.now(UTC).isoformat(),
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "request_id": ctx.request_id,
        "trace_id": ctx.trace_id,
        "actor_id": ctx.user_id,
        "idempotency_key": ctx.idempotency_key,
        "dispatch_idempotency_key": dispatch_idempotency_key,
        "resource_version": resource_version,
        "resource_version_identity": resource_version_identity,
        "business_payload_sha256": payload_sha256,
        "correlation_id": ctx.trace_id,
        "subject": {"type": aggregate_type, "id": aggregate_id},
        "data": business_payload,
        **({"otel_trace_context": carrier} if (carrier := current_trace_carrier()) else {}),
    }
    candidate = OutboxEvent(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=event_payload,
        dispatch_idempotency_key=dispatch_idempotency_key,
    )
    event, created = insert_or_get_event(session, candidate)
    _assert_equivalent_event(
        event,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        business_event_id=business_event_id,
        payload_sha256=payload_sha256,
    )
    log_event(
        logger,
        "outbox.enqueued" if created else "outbox.duplicate_reused",
        ctx=ctx,
        event_id=event.event_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        dispatch_idempotency_key=dispatch_idempotency_key,
    )
    return event
