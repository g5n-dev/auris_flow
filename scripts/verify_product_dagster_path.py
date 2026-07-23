#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import secrets
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if not (BACKEND_ROOT / "app").is_dir() and Path("/app/app").is_dir():
    BACKEND_ROOT = Path("/app")
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.public_run_projection_service import (  # noqa: E402
    PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS,
    PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS,
)

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})
MAX_RESPONSE_BYTES = 1_048_576
SCOPE = ("aurora_auto", "sales_qa")
PUBLIC_RUN_FORBIDDEN_FIELD_FRAGMENTS = tuple(sorted(PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS))
PUBLIC_RUN_FORBIDDEN_VALUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"dagster",
        r"graphql",
        r"\badapter\b",
        r"\b[a-z0-9._-]+_(?:job|pipeline)\b",
    )
)
SUCCESS_INTERNAL_EVIDENCE_SOURCES = frozenset(
    {"run_records", "outbox_events", "completion_receipts"}
)
CANCELLATION_INTERNAL_EVIDENCE_SOURCES = frozenset({"run_records", "outbox_events"})
SUCCESS_INTERNAL_EVIDENCE_FIELDS = frozenset(
    {
        "adapter_mode",
        "dagster_run_id",
        "evidence_sources",
        "outbox_confirmed",
        "outbox_events",
        "signed_completion",
        "status_sync",
    }
)
CANCELLATION_INTERNAL_EVIDENCE_FIELDS = frozenset(
    {
        "adapter_mode",
        "dagster_run_id",
        "engine_status",
        "evidence_sources",
        "outbox_confirmed",
        "outbox_events",
        "terminate_policy",
    }
)
CANCELLATION_ACK_ENGINE_STATUSES = frozenset(
    {
        "QUEUED",
        "NOT_STARTED",
        "MANAGED",
        "STARTING",
        "STARTED",
        "CANCELING",
        "CANCELED",
    }
)
CANCELLATION_TERMINAL_ENGINE_STATUS = "CANCELED"
_CONFUSABLE_TRANSLATION = str.maketrans(
    {
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Χ": "X",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
    }
)


class GateFailure(RuntimeError):
    """A fail-closed product gate error that never includes remote response bodies."""


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateFailure(f"{label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise GateFailure(f"{label} is missing")
    return value


def _validate_terminal_history(history: object, final_status: str) -> None:
    if not isinstance(history, list) or not history:
        raise GateFailure("task run status history is missing")
    prior_target: str | None = None
    terminal_seen = False
    for entry in history:
        item = _mapping(entry, "task run status history entry")
        source = _text(item.get("from"), "task run status history source")
        target = _text(item.get("to"), "task run status history target")
        if prior_target is not None and source != prior_target:
            raise GateFailure("task run status history is not contiguous")
        if terminal_seen:
            raise GateFailure("task run status history exits a terminal state")
        terminal_seen = target in TERMINAL_STATUSES
        prior_target = target
    if prior_target != final_status or not terminal_seen:
        raise GateFailure("task run status history does not prove its terminal state")


def _assert_engine_neutral_projection(value: object, *, path: str = "data") -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise GateFailure(
                    f"public run projection is not engine-neutral at {path}"
                )
            key = raw_key
            canonical_key = unicodedata.normalize("NFKC", key)
            if key != canonical_key or not re.fullmatch(
                r"[A-Za-z0-9_.:/ -]+", canonical_key
            ):
                raise GateFailure(
                    f"public run projection is not engine-neutral at {path}.{key}"
                )
            fingerprint = re.sub(r"[^a-z0-9]+", "", canonical_key.casefold())
            if fingerprint in PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS or any(
                fragment in fingerprint
                for fragment in PUBLIC_RUN_FORBIDDEN_FIELD_FRAGMENTS
            ):
                raise GateFailure(
                    f"public run projection is not engine-neutral at {path}.{key}"
                )
            _assert_engine_neutral_projection(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_engine_neutral_projection(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        normalized_value = unicodedata.normalize("NFKC", value)
        visible_value = "".join(
            char for char in normalized_value if unicodedata.category(char) != "Cf"
        ).translate(_CONFUSABLE_TRANSLATION)
        if any(
            pattern.search(visible_value)
            for pattern in PUBLIC_RUN_FORBIDDEN_VALUE_PATTERNS
        ):
            raise GateFailure(f"public run projection is not engine-neutral at {path}")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise GateFailure(f"public run projection is not engine-neutral at {path}")


def validate_run_projection(
    payload: dict[str, Any],
    *,
    expected_status: str,
    expected_scope: tuple[str, str],
    expected_trace_id: str,
) -> dict[str, Any]:
    _assert_engine_neutral_projection(payload)
    run_id = _text(payload.get("run_id"), "task run id")
    if (
        payload.get("run_type") != "task_run"
        or payload.get("status") != expected_status
    ):
        raise GateFailure("task run terminal projection is invalid")
    trace_id = _text(payload.get("trace_id"), "task run trace id")
    if trace_id != expected_trace_id:
        raise GateFailure("task run trace binding is invalid")
    tenant_id, project_id = expected_scope
    if payload.get("tenant_id") != tenant_id or payload.get("project_id") != project_id:
        raise GateFailure("task run scope binding is invalid")
    status_version = payload.get("status_version")
    if (
        isinstance(status_version, bool)
        or not isinstance(status_version, int)
        or status_version < 3
    ):
        raise GateFailure("task run status version is invalid")

    _validate_terminal_history(payload.get("status_history"), expected_status)
    return {
        "run_id": run_id,
        "status": expected_status,
        "status_version": status_version,
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
    }


def _require_internal_evidence_shape(
    evidence: dict[str, Any],
    *,
    allowed_fields: frozenset[str],
    expected_sources: frozenset[str],
    expected_event_types: frozenset[str],
    scenario: str,
) -> None:
    if not set(evidence).issubset(allowed_fields):
        raise GateFailure(f"{scenario} internal evidence contains unexpected fields")
    if frozenset(evidence.get("evidence_sources") or ()) != expected_sources:
        raise GateFailure(f"{scenario} internal evidence sources are incomplete")
    events = evidence.get("outbox_events")
    if not isinstance(events, list) or len(events) != len(expected_event_types):
        raise GateFailure(f"{scenario} outbox event evidence is incomplete")
    observed_types: set[str] = set()
    for raw_event in events:
        event = _mapping(raw_event, f"{scenario} outbox event evidence")
        event_type = _text(event.get("event_type"), "outbox event type")
        if (
            event_type in observed_types
            or event.get("status") != "processed"
            or event.get("delivery_state") != "confirmed"
            or isinstance(event.get("event_id"), bool)
            or not isinstance(event.get("event_id"), int)
            or int(event["event_id"]) < 1
            or isinstance(event.get("attempt_count"), bool)
            or not isinstance(event.get("attempt_count"), int)
            or int(event["attempt_count"]) < 1
            or not _text(
                event.get("aggregate_id"),
                "outbox event aggregate id",
            )
            or not _text(
                event.get("dispatch_idempotency_key"),
                "outbox dispatch idempotency key",
            )
        ):
            raise GateFailure(f"{scenario} outbox event evidence is invalid")
        observed_types.add(event_type)
    if observed_types != expected_event_types:
        raise GateFailure(f"{scenario} outbox event types are incomplete")


def build_evidence(
    *,
    source_commit: str,
    success_projection: dict[str, Any],
    success_internal: dict[str, Any],
    cancellation_projection: dict[str, Any],
    cancellation_internal: dict[str, Any],
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise GateFailure("source commit must be an exact lowercase Git SHA")
    try:
        _assert_engine_neutral_projection(success_projection, path="success_projection")
        _assert_engine_neutral_projection(
            cancellation_projection, path="cancellation_projection"
        )
    except GateFailure as exc:
        raise GateFailure(
            "public projection contains internal engine evidence"
        ) from exc

    _require_internal_evidence_shape(
        success_internal,
        allowed_fields=SUCCESS_INTERNAL_EVIDENCE_FIELDS,
        expected_sources=SUCCESS_INTERNAL_EVIDENCE_SOURCES,
        expected_event_types=frozenset(
            {
                "task_run.requested",
                "task_run.status_sync_requested",
                "task_run.succeeded",
            }
        ),
        scenario="success",
    )
    _require_internal_evidence_shape(
        cancellation_internal,
        allowed_fields=CANCELLATION_INTERNAL_EVIDENCE_FIELDS,
        expected_sources=CANCELLATION_INTERNAL_EVIDENCE_SOURCES,
        expected_event_types=frozenset(
            {
                "task_run.requested",
                "task_run.cancel_requested",
                "task_run.status_sync_requested",
                "task_run.cancelled",
            }
        ),
        scenario="cancellation",
    )

    success = {**success_projection, **success_internal}
    cancellation = {**cancellation_projection, **cancellation_internal}
    if (
        success.get("status") != "success"
        or success.get("adapter_mode") != "real"
        or not _text(success.get("dagster_run_id"), "success engine run id")
        or not _text(success.get("status_sync"), "success synchronized engine status")
        or success.get("signed_completion") is not True
        or success.get("outbox_confirmed") is not True
        or frozenset(success.get("evidence_sources") or ())
        != SUCCESS_INTERNAL_EVIDENCE_SOURCES
        or cancellation.get("status") != "cancelled"
        or cancellation.get("adapter_mode") != "real"
        or not _text(cancellation.get("dagster_run_id"), "cancellation engine run id")
        or cancellation.get("terminate_policy") != "SAFE_TERMINATE"
        or not _text(
            cancellation.get("engine_status"),
            "cancellation engine status",
        )
        or cancellation.get("outbox_confirmed") is not True
        or frozenset(cancellation.get("evidence_sources") or ())
        != CANCELLATION_INTERNAL_EVIDENCE_SOURCES
    ):
        raise GateFailure("product Dagster scenario proof is incomplete")
    return {
        "schema_version": "auris.product-dagster-gate.v1",
        "status": "ok",
        "source_commit": source_commit,
        "execution_environment": "compose",
        "adapter_mode": "real",
        "verified_at": datetime.now(UTC).isoformat(),
        "scope": {"tenant_id": SCOPE[0], "project_id": SCOPE[1]},
        "scenarios": {
            "success": {
                key: success[key]
                for key in (
                    "run_id",
                    "dagster_run_id",
                    "trace_id",
                    "status",
                    "status_version",
                    "adapter_mode",
                    "status_sync",
                    "signed_completion",
                    "outbox_confirmed",
                    "outbox_events",
                    "evidence_sources",
                )
                if key in success
            },
            "cancellation": {
                key: cancellation[key]
                for key in (
                    "run_id",
                    "dagster_run_id",
                    "trace_id",
                    "status",
                    "status_version",
                    "adapter_mode",
                    "terminate_policy",
                    "engine_status",
                    "outbox_confirmed",
                    "outbox_events",
                    "evidence_sources",
                )
                if key in cancellation
            },
        },
    }


def _remaining_seconds(
    deadline: float,
    *,
    stage: str,
    maximum: float | None = None,
) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GateFailure(f"product Dagster gate deadline exhausted during {stage}")
    return min(remaining, maximum) if maximum is not None else remaining


class BFFClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        deadline: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.deadline = deadline

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        project_id: str = SCOPE[1],
        expected_status: int = 200,
    ) -> dict[str, Any]:
        data = None
        headers = {
            "Authorization": "Bearer dev-token",
            "X-Tenant-Id": SCOPE[0],
            "X-Project-Id": project_id,
            "X-Request-Id": f"product-dagster-gate-{secrets.token_hex(8)}",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        request_timeout = self.timeout_seconds
        if self.deadline is not None:
            request_timeout = _remaining_seconds(
                self.deadline,
                stage="BFF request",
                maximum=request_timeout,
            )
        try:
            with urlopen(request, timeout=request_timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, URLError, TimeoutError) as exc:
            raise GateFailure("BFF request failed") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise GateFailure("BFF response exceeds the gate limit")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError) as exc:
            raise GateFailure("BFF response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise GateFailure("BFF response envelope is invalid")
        if status != expected_status:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else "UNKNOWN"
            raise GateFailure(
                f"BFF returned an unexpected status (HTTP {status}, code={code})"
            )
        return payload


def _response_data(response: dict[str, Any]) -> dict[str, Any]:
    return _mapping(response.get("data"), "BFF data envelope")


def _public_run_data(response: dict[str, Any]) -> dict[str, Any]:
    data = _response_data(response)
    _assert_engine_neutral_projection(data)
    return data


def _wait_for_run(
    client: BFFClient,
    run_id: str,
    *,
    expected: set[str],
    deadline: float,
) -> dict[str, Any]:
    last_status = "unknown"
    while time.monotonic() < deadline:
        data = _public_run_data(client.request("GET", f"/api/v1/task-runs/{run_id}"))
        last_status = str(data.get("status") or "unknown")
        if last_status in expected:
            return data
        if last_status in TERMINAL_STATUSES:
            raise GateFailure(
                f"task run reached unexpected terminal status {last_status}"
            )
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise GateFailure(
        f"timed out waiting for task run state; last_status={last_status}"
    )


def _assert_cross_scope_hidden(
    client: BFFClient,
    run_id: str,
    *,
    project_id: str,
) -> None:
    response = client.request(
        "GET",
        f"/api/v1/task-runs/{run_id}",
        project_id=project_id,
        expected_status=404,
    )
    error = _mapping(response.get("error"), "cross-scope error envelope")
    if error.get("code") != "NOT_FOUND":
        raise GateFailure("cross-scope task run lookup did not fail closed")


def _terminal_rejection(
    client: BFFClient,
    *,
    path: str,
    body: dict[str, Any],
    idempotency_key: str,
) -> None:
    response = client.request(
        "POST",
        path,
        body=body,
        idempotency_key=idempotency_key,
        expected_status=409,
    )
    error = _mapping(response.get("error"), "terminal rejection envelope")
    if error.get("code") != "RUN_ALREADY_TERMINAL":
        raise GateFailure(
            "terminal run mutation did not return the stable conflict code"
        )


def _event_summary(event: Any) -> dict[str, Any]:
    return {
        "event_id": int(event.event_id),
        "event_type": str(event.event_type),
        "aggregate_id": str(event.aggregate_id),
        "status": str(event.status),
        "delivery_state": str(event.delivery_state),
        "attempt_count": int(event.attempt_count),
        "dispatch_idempotency_key": str(event.dispatch_idempotency_key),
    }


def _require_confirmed_event(
    events: list[Any],
    *,
    event_type: str,
    aggregate_id: str,
    trace_id: str,
) -> Any:
    matches = [
        event
        for event in events
        if event.event_type == event_type and event.aggregate_id == aggregate_id
    ]
    if len(matches) != 1:
        raise GateFailure(f"outbox event proof is missing or duplicated: {event_type}")
    event = matches[0]
    payload = _mapping(event.payload, "outbox payload")
    attempt_count = event.attempt_count
    lease_generation = event.lease_generation
    if (
        event.tenant_id != SCOPE[0]
        or event.project_id != SCOPE[1]
        or event.status != "processed"
        or event.delivery_state != "confirmed"
        or event.processed_at is None
        or isinstance(attempt_count, bool)
        or not isinstance(attempt_count, int)
        or attempt_count < 1
        or isinstance(lease_generation, bool)
        or not isinstance(lease_generation, int)
        or lease_generation < 1
        or not isinstance(event.dispatch_request_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", event.dispatch_request_sha256)
        or event.last_error is not None
        or payload.get("trace_id") != trace_id
    ):
        raise GateFailure(
            f"outbox scope, trace, or delivery proof is invalid: {event_type}"
        )
    return event


def _require_real_event_dispatch(
    event: Any,
    *,
    operation: str,
    external_run_id: str,
) -> dict[str, Any]:
    payload = _mapping(event.payload, "outbox payload")
    dispatch = _mapping(payload.get("adapter_dispatch"), "outbox adapter dispatch")
    details = _mapping(dispatch.get("details"), "outbox adapter dispatch details")
    if (
        dispatch.get("adapter") != "dagster"
        or dispatch.get("operation") != operation
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
        or details.get("external_run_id") != external_run_id
    ):
        raise GateFailure(f"outbox did not confirm real Dagster {operation}")
    if payload.get("run_id") != event.aggregate_id:
        raise GateFailure(f"outbox Dagster {operation} run binding is invalid")
    if operation == "run_request" and (
        details.get("tenant_id") != event.tenant_id
        or details.get("project_id") != event.project_id
        or details.get("trace_id") != payload.get("trace_id")
        or details.get("run_id") != event.aggregate_id
    ):
        raise GateFailure("outbox Dagster launch scope or trace binding is invalid")
    if operation in {"run_status", "cancel_run"} and (
        payload.get("external_run_id") != external_run_id
    ):
        raise GateFailure(f"outbox Dagster {operation} source binding is invalid")
    return details


def _require_record_projection_binding(
    record: Any,
    projection: dict[str, Any],
) -> None:
    payload = _mapping(record.payload, "persisted RunRecord payload")
    if (
        record.run_type != "task_run"
        or record.run_id != projection.get("run_id")
        or record.tenant_id != projection.get("tenant_id")
        or record.project_id != projection.get("project_id")
        or record.trace_id != projection.get("trace_id")
        or record.status != projection.get("status")
        or record.status_version != projection.get("status_version")
        or payload.get("run_id") != record.run_id
        or payload.get("trace_id") != record.trace_id
    ):
        raise GateFailure("persisted RunRecord projection binding is invalid")


def _require_real_record_dispatch(
    record: Any,
    *,
    operation: str,
    expected_run_type: str,
    expected_external_run_id: str | None,
    source_record: Any | None = None,
) -> dict[str, Any]:
    payload = _mapping(record.payload, "persisted RunRecord payload")
    dispatch = _mapping(payload.get("dispatch"), "persisted RunRecord dispatch")
    details = _mapping(dispatch.get("details"), "persisted RunRecord dispatch details")
    external_run_id = details.get("external_run_id")
    if (
        record.tenant_id != SCOPE[0]
        or record.project_id != SCOPE[1]
        or record.run_type != expected_run_type
        or payload.get("run_id") != record.run_id
        or payload.get("trace_id") != record.trace_id
        or dispatch.get("adapter") != "dagster"
        or dispatch.get("operation") != operation
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
        or not isinstance(external_run_id, str)
        or not external_run_id
        or (
            expected_external_run_id is not None
            and external_run_id != expected_external_run_id
        )
    ):
        raise GateFailure(
            f"persisted RunRecord does not prove real Dagster {operation}"
        )
    if operation == "run_request" and (
        details.get("tenant_id") != record.tenant_id
        or details.get("project_id") != record.project_id
        or details.get("trace_id") != record.trace_id
        or details.get("run_id") != record.run_id
    ):
        raise GateFailure("persisted Dagster launch scope or trace binding is invalid")
    if source_record is not None and (
        source_record.run_type != "task_run"
        or source_record.tenant_id != record.tenant_id
        or source_record.project_id != record.project_id
        or record.run_key != source_record.run_id
        or payload.get("source_run_id") != source_record.run_id
        or payload.get("source_trace_id") != source_record.trace_id
        or payload.get("external_run_id") != external_run_id
    ):
        raise GateFailure(f"persisted Dagster {operation} source binding is invalid")
    return details


def _require_real_async_cancellation_proof(
    *,
    source: Any,
    cancellation: Any,
    status_sync_controls: list[Any],
    events: list[Any],
    external_run_id: str,
) -> dict[str, Any]:
    if (
        source.tenant_id != SCOPE[0]
        or source.project_id != SCOPE[1]
        or source.run_type != "task_run"
        or source.status != "cancelled"
        or source.engine_status != CANCELLATION_TERMINAL_ENGINE_STATUS
    ):
        raise GateFailure("real Dagster cancellation did not reach terminal CANCELED")

    cancel_details = _require_real_record_dispatch(
        cancellation,
        operation="cancel_run",
        expected_run_type="task_run_cancellation",
        expected_external_run_id=external_run_id,
        source_record=source,
    )
    acknowledged_status = cancel_details.get("dagster_status")
    if (
        cancellation.status != "success"
        or cancel_details.get("response_typename") != "TerminateRunSuccess"
        or cancel_details.get("terminate_policy") != "SAFE_TERMINATE"
        or acknowledged_status not in CANCELLATION_ACK_ENGINE_STATUSES
    ):
        raise GateFailure("real Dagster SAFE_TERMINATE acknowledgement is invalid")
    cancel_control_event = _require_confirmed_event(
        events,
        event_type="task_run.cancel_requested",
        aggregate_id=cancellation.run_id,
        trace_id=cancellation.trace_id,
    )
    _require_real_event_dispatch(
        cancel_control_event,
        operation="cancel_run",
        external_run_id=external_run_id,
    )

    bound_monitor_controls: list[Any] = []
    for control in status_sync_controls:
        payload = control.payload if isinstance(control.payload, dict) else {}
        generation = payload.get("monitor_generation")
        if (
            control.tenant_id == source.tenant_id
            and control.project_id == source.project_id
            and control.run_type == "task_run_status_sync"
            and control.run_key == source.run_id
            and control.trace_id == source.trace_id
            and isinstance(control.run_id, str)
            and control.run_id.startswith("task_run_status_sync_auto_")
            and payload.get("monitor_kind") == "missed_callback_reconcile"
            and payload.get("monitor_control_id") == control.run_id
            and isinstance(generation, int)
            and not isinstance(generation, bool)
            and generation >= 1
        ):
            bound_monitor_controls.append(control)
    if not bound_monitor_controls:
        raise GateFailure(
            "real Dagster monitor status synchronization proof is missing"
        )

    terminal_monitor_proofs: list[tuple[Any, dict[str, Any], Any]] = []
    has_valid_monitor_dispatch = False
    has_terminal_monitor_dispatch = False
    for control in bound_monitor_controls:
        try:
            sync_details = _require_real_record_dispatch(
                control,
                operation="run_status",
                expected_run_type="task_run_status_sync",
                expected_external_run_id=external_run_id,
                source_record=source,
            )
        except GateFailure:
            continue
        if control.status != "success":
            continue
        has_valid_monitor_dispatch = True
        if sync_details.get("dagster_status") != CANCELLATION_TERMINAL_ENGINE_STATUS:
            continue
        has_terminal_monitor_dispatch = True
        try:
            sync_event = _require_confirmed_event(
                events,
                event_type="task_run.status_sync_requested",
                aggregate_id=control.run_id,
                trace_id=control.trace_id,
            )
            _require_real_event_dispatch(
                sync_event,
                operation="run_status",
                external_run_id=external_run_id,
            )
        except GateFailure:
            continue
        terminal_monitor_proofs.append((control, sync_details, sync_event))
    if not terminal_monitor_proofs:
        if has_valid_monitor_dispatch and not has_terminal_monitor_dispatch:
            raise GateFailure("real Dagster monitor did not prove terminal CANCELED")
        raise GateFailure(
            "real Dagster monitor status synchronization proof is missing"
        )
    if len(terminal_monitor_proofs) != 1:
        raise GateFailure(
            "real Dagster monitor status synchronization proof is duplicated"
        )

    terminal_event = _require_confirmed_event(
        events,
        event_type="task_run.cancelled",
        aggregate_id=source.run_id,
        trace_id=source.trace_id,
    )
    status_sync_control, status_sync_details, status_sync_event = (
        terminal_monitor_proofs[0]
    )
    return {
        "acknowledged_engine_status": str(acknowledged_status),
        "engine_status": str(status_sync_details["dagster_status"]),
        "cancel_control_event": cancel_control_event,
        "status_sync_control": status_sync_control,
        "status_sync_event": status_sync_event,
        "terminal_event": terminal_event,
    }


def _database_proof(
    *,
    success_run_id: str,
    success_sync_id: str,
    cancel_run_id: str,
    cancellation_id: str,
    success_projection: dict[str, Any],
    cancellation_projection: dict[str, Any],
    deadline: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import OutboxEvent, RunCompletionReceipt, RunRecord

    last_error = "database proof not evaluated"
    while time.monotonic() < deadline:
        try:
            with SessionLocal() as session:

                def scoped_run(run_id: str) -> Any:
                    return session.scalar(
                        select(RunRecord).where(
                            RunRecord.run_id == run_id,
                            RunRecord.tenant_id == SCOPE[0],
                            RunRecord.project_id == SCOPE[1],
                        )
                    )

                success = scoped_run(success_run_id)
                sync = scoped_run(success_sync_id)
                cancelled = scoped_run(cancel_run_id)
                cancellation = scoped_run(cancellation_id)
                cancellation_syncs = list(
                    session.scalars(
                        select(RunRecord).where(
                            RunRecord.tenant_id == SCOPE[0],
                            RunRecord.project_id == SCOPE[1],
                            RunRecord.run_type == "task_run_status_sync",
                            RunRecord.run_key == cancel_run_id,
                        )
                    )
                )
                events = list(
                    session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.tenant_id == SCOPE[0],
                            OutboxEvent.project_id == SCOPE[1],
                            OutboxEvent.aggregate_id.in_(
                                [
                                    success_run_id,
                                    success_sync_id,
                                    cancel_run_id,
                                    cancellation_id,
                                    *(control.run_id for control in cancellation_syncs),
                                ]
                            ),
                        )
                    )
                )
                receipt = session.scalar(
                    select(RunCompletionReceipt).where(
                        RunCompletionReceipt.run_id == success_run_id,
                        RunCompletionReceipt.tenant_id == SCOPE[0],
                        RunCompletionReceipt.project_id == SCOPE[1],
                    )
                )
                if not all((success, sync, cancelled, cancellation, receipt)):
                    raise GateFailure(
                        "RunRecord or completion receipt proof is missing"
                    )
                assert success is not None
                assert sync is not None
                assert cancelled is not None
                assert cancellation is not None
                assert receipt is not None

                _require_record_projection_binding(success, success_projection)
                _require_record_projection_binding(cancelled, cancellation_projection)

                success_requested = _require_confirmed_event(
                    events,
                    event_type="task_run.requested",
                    aggregate_id=success_run_id,
                    trace_id=success.trace_id,
                )
                success_terminal = _require_confirmed_event(
                    events,
                    event_type="task_run.succeeded",
                    aggregate_id=success_run_id,
                    trace_id=success.trace_id,
                )
                sync_event = _require_confirmed_event(
                    events,
                    event_type="task_run.status_sync_requested",
                    aggregate_id=success_sync_id,
                    trace_id=sync.trace_id,
                )
                cancel_requested = _require_confirmed_event(
                    events,
                    event_type="task_run.requested",
                    aggregate_id=cancel_run_id,
                    trace_id=cancelled.trace_id,
                )

                success_dispatch_details = _require_real_record_dispatch(
                    success,
                    operation="run_request",
                    expected_run_type="task_run",
                    expected_external_run_id=None,
                )
                cancel_source_details = _require_real_record_dispatch(
                    cancelled,
                    operation="run_request",
                    expected_run_type="task_run",
                    expected_external_run_id=None,
                )
                success_external_id = _text(
                    success_dispatch_details.get("external_run_id"),
                    "persisted success Dagster run id",
                )
                cancel_external_id = _text(
                    cancel_source_details.get("external_run_id"),
                    "persisted cancellation Dagster run id",
                )
                sync_details = _require_real_record_dispatch(
                    sync,
                    operation="run_status",
                    expected_run_type="task_run_status_sync",
                    expected_external_run_id=success_external_id,
                    source_record=success,
                )
                if sync.status != "success" or not sync_details.get("dagster_status"):
                    raise GateFailure(
                        "real Dagster status synchronization proof is invalid"
                    )
                _require_real_event_dispatch(
                    success_requested,
                    operation="run_request",
                    external_run_id=success_external_id,
                )
                _require_real_event_dispatch(
                    sync_event,
                    operation="run_status",
                    external_run_id=success_external_id,
                )
                _require_real_event_dispatch(
                    cancel_requested,
                    operation="run_request",
                    external_run_id=cancel_external_id,
                )

                cancellation_dispatch_proof = _require_real_async_cancellation_proof(
                    source=cancelled,
                    cancellation=cancellation,
                    status_sync_controls=cancellation_syncs,
                    events=events,
                    external_run_id=cancel_external_id,
                )

                if (
                    receipt.processing_state != "completed"
                    or receipt.completion_status != "success"
                    or receipt.adapter != "dagster"
                    or receipt.source != "dagster"
                    or receipt.authenticated_source != "dagster"
                    or receipt.signature_key_id != "dagster-v1"
                    or receipt.signature_mode != "hmac-sha256"
                    or receipt.external_id != success_external_id
                    or receipt.run_trace_id != success.trace_id
                    or not receipt.signature_nonce
                    or not receipt.signature_body_hash
                    or receipt.completed_at is None
                ):
                    raise GateFailure(
                        "signed Dagster completion receipt proof is invalid"
                    )

                success_proof = {
                    "dagster_run_id": success_external_id,
                    "adapter_mode": "real",
                    "status_sync": str(sync_details["dagster_status"]),
                    "signed_completion": True,
                    "outbox_confirmed": True,
                    "evidence_sources": sorted(SUCCESS_INTERNAL_EVIDENCE_SOURCES),
                    "outbox_events": [
                        _event_summary(success_requested),
                        _event_summary(sync_event),
                        _event_summary(success_terminal),
                    ],
                }
                cancellation_proof = {
                    "dagster_run_id": cancel_external_id,
                    "adapter_mode": "real",
                    "terminate_policy": "SAFE_TERMINATE",
                    "engine_status": str(cancellation_dispatch_proof["engine_status"]),
                    "outbox_confirmed": True,
                    "evidence_sources": sorted(CANCELLATION_INTERNAL_EVIDENCE_SOURCES),
                    "outbox_events": [
                        _event_summary(cancel_requested),
                        _event_summary(
                            cancellation_dispatch_proof["cancel_control_event"]
                        ),
                        _event_summary(
                            cancellation_dispatch_proof["status_sync_event"]
                        ),
                        _event_summary(cancellation_dispatch_proof["terminal_event"]),
                    ],
                }
                return success_proof, cancellation_proof
        except GateFailure as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.25, remaining))
    raise GateFailure(f"timed out waiting for database evidence: {last_error}")


def run_gate(
    *,
    base_url: str,
    source_commit: str,
    artifact: Path,
    timeout_seconds: float,
    suffix: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    client = BFFClient(
        base_url,
        timeout_seconds=min(timeout_seconds, 10.0),
        deadline=deadline,
    )
    client.request("GET", "/readyz")
    isolation_project_id = (
        "product_gate_isolation_"
        + hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:12]
    )
    client.request(
        "POST",
        "/api/v1/projects",
        body={
            "project_id": isolation_project_id,
            "name": "Product gate isolation scope",
            "status": "active",
        },
        idempotency_key=f"product-dagster-gate:{suffix}:isolation-project",
        expected_status=201,
    )

    success_created = _public_run_data(
        client.request(
            "POST",
            "/api/v1/task-runs",
            body={
                "task_version_id": "task_version_v3_2_1",
                "trigger_type": "manual",
                "partition_key": f"product-dagster-gate/{suffix}/success",
            },
            idempotency_key=f"product-dagster-gate:{suffix}:success",
            expected_status=202,
        )
    )
    success_run_id = _text(success_created.get("run_id"), "success task run id")
    success_submitted = _wait_for_run(
        client,
        success_run_id,
        expected={"submitted"},
        deadline=deadline,
    )
    success_trace_id = _text(success_submitted.get("trace_id"), "task run trace id")

    sync_created = _public_run_data(
        client.request(
            "POST",
            f"/api/v1/task-runs/{success_run_id}/status-syncs",
            body={},
            idempotency_key=f"product-dagster-gate:{suffix}:status-sync",
            expected_status=202,
        )
    )
    success_sync_id = _text(sync_created.get("run_id"), "status sync control id")
    _wait_for_run(
        client,
        success_sync_id,
        expected={"success"},
        deadline=deadline,
    )
    success_final = _wait_for_run(
        client,
        success_run_id,
        expected={"success"},
        deadline=deadline,
    )
    success_api_proof = validate_run_projection(
        success_final,
        expected_status="success",
        expected_scope=SCOPE,
        expected_trace_id=success_trace_id,
    )
    success_version = int(success_api_proof["status_version"])
    _terminal_rejection(
        client,
        path=f"/api/v1/task-runs/{success_run_id}/cancellations",
        body={"reason": "product gate verifies terminal monotonicity"},
        idempotency_key=f"product-dagster-gate:{suffix}:late-cancel",
    )
    success_after_conflict = _public_run_data(
        client.request("GET", f"/api/v1/task-runs/{success_run_id}")
    )
    if (
        success_after_conflict.get("status") != "success"
        or success_after_conflict.get("status_version") != success_version
    ):
        raise GateFailure("success terminal state changed after rejected cancellation")
    _assert_cross_scope_hidden(
        client,
        success_run_id,
        project_id=isolation_project_id,
    )

    cancel_created = _public_run_data(
        client.request(
            "POST",
            "/api/v1/task-runs",
            body={
                "task_version_id": "task_version_v3_2_1",
                "trigger_type": "manual",
                "partition_key": f"product-dagster-gate/{suffix}/cancel",
            },
            idempotency_key=f"product-dagster-gate:{suffix}:cancel-run",
            expected_status=202,
        )
    )
    cancel_run_id = _text(cancel_created.get("run_id"), "cancellation task run id")
    cancel_submitted = _wait_for_run(
        client,
        cancel_run_id,
        expected={"submitted"},
        deadline=deadline,
    )
    cancel_trace_id = _text(cancel_submitted.get("trace_id"), "task run trace id")
    cancellation_created = _public_run_data(
        client.request(
            "POST",
            f"/api/v1/task-runs/{cancel_run_id}/cancellations",
            body={"reason": "product release gate cancellation"},
            idempotency_key=f"product-dagster-gate:{suffix}:cancel-control",
            expected_status=202,
        )
    )
    cancellation_id = _text(
        cancellation_created.get("run_id"), "cancellation control id"
    )
    _wait_for_run(
        client,
        cancellation_id,
        expected={"success"},
        deadline=deadline,
    )
    cancel_final = _wait_for_run(
        client,
        cancel_run_id,
        expected={"cancelled"},
        deadline=deadline,
    )
    cancel_api_proof = validate_run_projection(
        cancel_final,
        expected_status="cancelled",
        expected_scope=SCOPE,
        expected_trace_id=cancel_trace_id,
    )
    cancel_version = int(cancel_api_proof["status_version"])
    _terminal_rejection(
        client,
        path=f"/api/v1/task-runs/{cancel_run_id}/status-syncs",
        body={},
        idempotency_key=f"product-dagster-gate:{suffix}:late-sync",
    )
    cancel_after_conflict = _public_run_data(
        client.request("GET", f"/api/v1/task-runs/{cancel_run_id}")
    )
    if (
        cancel_after_conflict.get("status") != "cancelled"
        or cancel_after_conflict.get("status_version") != cancel_version
    ):
        raise GateFailure(
            "cancelled terminal state changed after rejected synchronization"
        )
    _assert_cross_scope_hidden(
        client,
        cancel_run_id,
        project_id=isolation_project_id,
    )

    success_db, cancel_db = _database_proof(
        success_run_id=success_run_id,
        success_sync_id=success_sync_id,
        cancel_run_id=cancel_run_id,
        cancellation_id=cancellation_id,
        success_projection=success_api_proof,
        cancellation_projection=cancel_api_proof,
        deadline=deadline,
    )
    evidence = build_evidence(
        source_commit=source_commit,
        success_projection=success_api_proof,
        success_internal=success_db,
        cancellation_projection=cancel_api_proof,
        cancellation_internal=cancel_db,
    )
    _remaining_seconds(deadline, stage="evidence publication")
    artifact.parent.mkdir(parents=True, exist_ok=True)
    temporary = artifact.with_name(f".{artifact.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(artifact)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Auris Flow's BFF/Worker product path against real Dagster."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--run-suffix", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0 or args.timeout_seconds > 600:
        print(
            "Product Dagster gate timeout must be between 1 and 600 seconds.",
            file=sys.stderr,
        )
        return 2
    try:
        run_gate(
            base_url=args.base_url,
            source_commit=args.source_commit,
            artifact=args.artifact,
            timeout_seconds=args.timeout_seconds,
            suffix=args.run_suffix,
        )
    except GateFailure as exc:
        try:
            args.artifact.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"product Dagster gate failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - release evidence must fail closed and stay sanitized.
        try:
            args.artifact.unlink(missing_ok=True)
        except OSError:
            pass
        print("product Dagster gate failed: internal verifier failure", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
