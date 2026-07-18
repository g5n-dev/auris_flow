#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
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

COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled"})
MAX_RESPONSE_BYTES = 1_048_576
SCOPE = ("aurora_auto", "sales_qa")


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


def validate_run_projection(
    payload: dict[str, Any],
    *,
    expected_status: str,
    expected_scope: tuple[str, str],
    expected_trace_id: str,
) -> dict[str, Any]:
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

    dispatch = _mapping(payload.get("dispatch"), "task run dispatch")
    details = _mapping(dispatch.get("details"), "task run dispatch details")
    if (
        dispatch.get("adapter") != "dagster"
        or dispatch.get("operation") != "run_request"
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
        or details.get("response_typename") != "LaunchRunSuccess"
    ):
        raise GateFailure("task run was not submitted through the real Dagster adapter")
    if (
        details.get("tenant_id") != tenant_id
        or details.get("project_id") != project_id
        or details.get("trace_id") != expected_trace_id
    ):
        raise GateFailure("real Dagster dispatch scope or trace binding is invalid")
    dagster_run_id = _text(details.get("external_run_id"), "Dagster run id")
    _validate_terminal_history(payload.get("status_history"), expected_status)
    return {
        "run_id": run_id,
        "status": expected_status,
        "status_version": status_version,
        "trace_id": trace_id,
        "dagster_run_id": dagster_run_id,
        "adapter_mode": "real",
    }


def build_evidence(
    *,
    source_commit: str,
    success: dict[str, Any],
    cancellation: dict[str, Any],
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise GateFailure("source commit must be an exact lowercase Git SHA")
    if (
        success.get("status") != "success"
        or success.get("signed_completion") is not True
        or success.get("outbox_confirmed") is not True
        or cancellation.get("status") != "cancelled"
        or cancellation.get("terminate_policy") != "SAFE_TERMINATE"
        or cancellation.get("outbox_confirmed") is not True
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
                )
                if key in cancellation
            },
        },
    }


class BFFClient:
    def __init__(self, base_url: str, *, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

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
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
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


def _wait_for_run(
    client: BFFClient,
    run_id: str,
    *,
    expected: set[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        data = _response_data(client.request("GET", f"/api/v1/task-runs/{run_id}"))
        last_status = str(data.get("status") or "unknown")
        if last_status in expected:
            return data
        if last_status in TERMINAL_STATUSES:
            raise GateFailure(
                f"task run reached unexpected terminal status {last_status}"
            )
        time.sleep(0.25)
    raise GateFailure(
        f"timed out waiting for task run state; last_status={last_status}"
    )


def _real_dispatch_binding(data: dict[str, Any]) -> tuple[str, str]:
    dispatch = _mapping(data.get("dispatch"), "task run dispatch")
    details = _mapping(dispatch.get("details"), "task run dispatch details")
    if (
        dispatch.get("adapter") != "dagster"
        or dispatch.get("operation") != "run_request"
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
        or details.get("response_typename") != "LaunchRunSuccess"
    ):
        raise GateFailure("BFF task run is not bound to a real Dagster launch")
    return (
        _text(details.get("external_run_id"), "Dagster run id"),
        _text(data.get("trace_id"), "task run trace id"),
    )


def _assert_cross_scope_hidden(client: BFFClient, run_id: str) -> None:
    response = client.request(
        "GET",
        f"/api/v1/task-runs/{run_id}",
        project_id="outside-gate-project",
        expected_status=403,
    )
    error = _mapping(response.get("error"), "cross-scope error envelope")
    if error.get("code") != "PROJECT_NOT_FOUND":
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
    return details


def _database_proof(
    *,
    success_run_id: str,
    success_sync_id: str,
    cancel_run_id: str,
    cancellation_id: str,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import OutboxEvent, RunCompletionReceipt, RunRecord

    deadline = time.monotonic() + timeout_seconds
    last_error = "database proof not evaluated"
    while time.monotonic() < deadline:
        try:
            with SessionLocal() as session:
                success = session.get(RunRecord, success_run_id)
                sync = session.get(RunRecord, success_sync_id)
                cancelled = session.get(RunRecord, cancel_run_id)
                cancellation = session.get(RunRecord, cancellation_id)
                events = list(
                    session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id.in_(
                                [
                                    success_run_id,
                                    success_sync_id,
                                    cancel_run_id,
                                    cancellation_id,
                                ]
                            )
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
                if (
                    success.tenant_id != SCOPE[0]
                    or success.project_id != SCOPE[1]
                    or cancelled.tenant_id != SCOPE[0]
                    or cancelled.project_id != SCOPE[1]
                ):
                    raise GateFailure("RunRecord scope binding is invalid")

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
                cancel_control_event = _require_confirmed_event(
                    events,
                    event_type="task_run.cancel_requested",
                    aggregate_id=cancellation_id,
                    trace_id=cancellation.trace_id,
                )
                cancel_terminal = _require_confirmed_event(
                    events,
                    event_type="task_run.cancelled",
                    aggregate_id=cancel_run_id,
                    trace_id=cancelled.trace_id,
                )

                sync_dispatch = _mapping(
                    sync.payload.get("dispatch"), "status sync dispatch"
                )
                sync_details = _mapping(
                    sync_dispatch.get("details"), "status sync dispatch details"
                )
                if (
                    sync.status != "success"
                    or sync_dispatch.get("adapter") != "dagster"
                    or sync_dispatch.get("operation") != "run_status"
                    or sync_details.get("mode") != "real"
                    or not sync_details.get("dagster_status")
                ):
                    raise GateFailure(
                        "real Dagster status synchronization proof is invalid"
                    )

                success_dispatch = _mapping(
                    success.payload.get("dispatch"), "persisted success dispatch"
                )
                success_dispatch_details = _mapping(
                    success_dispatch.get("details"),
                    "persisted success dispatch details",
                )
                cancel_source_dispatch = _mapping(
                    cancelled.payload.get("dispatch"), "persisted cancellation dispatch"
                )
                cancel_source_details = _mapping(
                    cancel_source_dispatch.get("details"),
                    "persisted cancellation dispatch details",
                )
                success_external_id = _text(
                    success_dispatch_details.get("external_run_id"),
                    "persisted success Dagster run id",
                )
                cancel_external_id = _text(
                    cancel_source_details.get("external_run_id"),
                    "persisted cancellation Dagster run id",
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

                cancel_dispatch = _mapping(
                    cancellation.payload.get("dispatch"), "cancellation dispatch"
                )
                cancel_details = _mapping(
                    cancel_dispatch.get("details"), "cancellation dispatch details"
                )
                if (
                    cancellation.status != "success"
                    or cancel_dispatch.get("adapter") != "dagster"
                    or cancel_dispatch.get("operation") != "cancel_run"
                    or cancel_details.get("mode") != "real"
                    or cancel_details.get("terminate_policy") != "SAFE_TERMINATE"
                    or cancel_details.get("dagster_status")
                    not in {"CANCELED", "CANCELLED"}
                ):
                    raise GateFailure("real Dagster SAFE_TERMINATE proof is invalid")
                _require_real_event_dispatch(
                    cancel_control_event,
                    operation="cancel_run",
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
                    "status_sync": str(sync_details["dagster_status"]),
                    "signed_completion": True,
                    "outbox_confirmed": True,
                    "outbox_events": [
                        _event_summary(success_requested),
                        _event_summary(sync_event),
                        _event_summary(success_terminal),
                    ],
                }
                cancellation_proof = {
                    "terminate_policy": "SAFE_TERMINATE",
                    "engine_status": str(cancel_details["dagster_status"]),
                    "outbox_confirmed": True,
                    "outbox_events": [
                        _event_summary(cancel_requested),
                        _event_summary(cancel_control_event),
                        _event_summary(cancel_terminal),
                    ],
                }
                return success_proof, cancellation_proof
        except GateFailure as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise GateFailure(f"timed out waiting for database evidence: {last_error}")


def run_gate(
    *,
    base_url: str,
    source_commit: str,
    artifact: Path,
    timeout_seconds: float,
    suffix: str,
) -> dict[str, Any]:
    client = BFFClient(base_url, timeout_seconds=min(timeout_seconds, 10.0))
    client.request("GET", "/readyz")

    success_created = _response_data(
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
        timeout_seconds=timeout_seconds,
    )
    success_dagster_run_id, success_trace_id = _real_dispatch_binding(success_submitted)

    sync_created = _response_data(
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
        timeout_seconds=timeout_seconds,
    )
    success_final = _wait_for_run(
        client,
        success_run_id,
        expected={"success"},
        timeout_seconds=timeout_seconds,
    )
    success_api_proof = validate_run_projection(
        success_final,
        expected_status="success",
        expected_scope=SCOPE,
        expected_trace_id=success_trace_id,
    )
    if success_api_proof["dagster_run_id"] != success_dagster_run_id:
        raise GateFailure("success Dagster run binding changed after completion")
    success_version = int(success_api_proof["status_version"])
    _terminal_rejection(
        client,
        path=f"/api/v1/task-runs/{success_run_id}/cancellations",
        body={"reason": "product gate verifies terminal monotonicity"},
        idempotency_key=f"product-dagster-gate:{suffix}:late-cancel",
    )
    success_after_conflict = _response_data(
        client.request("GET", f"/api/v1/task-runs/{success_run_id}")
    )
    if (
        success_after_conflict.get("status") != "success"
        or success_after_conflict.get("status_version") != success_version
    ):
        raise GateFailure("success terminal state changed after rejected cancellation")
    _assert_cross_scope_hidden(client, success_run_id)

    cancel_created = _response_data(
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
        timeout_seconds=timeout_seconds,
    )
    cancel_dagster_run_id, cancel_trace_id = _real_dispatch_binding(cancel_submitted)
    cancellation_created = _response_data(
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
        timeout_seconds=timeout_seconds,
    )
    cancel_final = _wait_for_run(
        client,
        cancel_run_id,
        expected={"cancelled"},
        timeout_seconds=timeout_seconds,
    )
    cancel_api_proof = validate_run_projection(
        cancel_final,
        expected_status="cancelled",
        expected_scope=SCOPE,
        expected_trace_id=cancel_trace_id,
    )
    if cancel_api_proof["dagster_run_id"] != cancel_dagster_run_id:
        raise GateFailure("cancelled Dagster run binding changed after termination")
    cancel_version = int(cancel_api_proof["status_version"])
    _terminal_rejection(
        client,
        path=f"/api/v1/task-runs/{cancel_run_id}/status-syncs",
        body={},
        idempotency_key=f"product-dagster-gate:{suffix}:late-sync",
    )
    cancel_after_conflict = _response_data(
        client.request("GET", f"/api/v1/task-runs/{cancel_run_id}")
    )
    if (
        cancel_after_conflict.get("status") != "cancelled"
        or cancel_after_conflict.get("status_version") != cancel_version
    ):
        raise GateFailure(
            "cancelled terminal state changed after rejected synchronization"
        )
    _assert_cross_scope_hidden(client, cancel_run_id)

    success_db, cancel_db = _database_proof(
        success_run_id=success_run_id,
        success_sync_id=success_sync_id,
        cancel_run_id=cancel_run_id,
        cancellation_id=cancellation_id,
        timeout_seconds=timeout_seconds,
    )
    evidence = build_evidence(
        source_commit=source_commit,
        success={**success_api_proof, **success_db},
        cancellation={**cancel_api_proof, **cancel_db},
    )
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
