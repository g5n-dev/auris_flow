#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AgentDecision,
    AgentRun,
    AssetLineageEdge,
    AssetMaterialization,
    AssetPartition,
    ExternalCallbackReceipt,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    VoiceprintEnrollment,
    OutboxEvent,
    PromptVersionCandidate,
    RunRecord,
    StorageObject,
    ToolCall,
    TraceRef,
)
from app.services.adapters import (  # noqa: E402
    RealObjectStorageClient,
    object_storage_client_for_provider,
)


ARTIFACT_PATH = (
    Path(os.environ["AURIS_E2E_RESULT_PATH"])
    if os.environ.get("AURIS_E2E_RESULT_PATH")
    else ROOT / "prototype/auris-flow-ui/e2e/artifacts/platform-bff-result.json"
)
OUTBOX_RESULT_PATH = (
    Path(os.environ["AURIS_E2E_OUTBOX_RESULT_PATH"])
    if os.environ.get("AURIS_E2E_OUTBOX_RESULT_PATH")
    else ROOT / "prototype/auris-flow-ui/e2e/artifacts/outbox-dispatch-result.json"
)
OUTBOX_DISPATCH_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("AURIS_E2E_OUTBOX_TIMEOUT_SECONDS", "90"))
)
OUTBOX_POLL_INTERVAL_SECONDS = max(
    0.05, float(os.environ.get("AURIS_E2E_OUTBOX_POLL_INTERVAL_SECONDS", "0.2"))
)
WORKER_HEALTH_PATH = os.environ.get("AURIS_E2E_WORKER_HEALTH_PATH")
WORKER_HEARTBEAT_MAX_AGE_SECONDS = max(
    5.0, float(os.environ.get("AURIS_E2E_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30"))
)
FRESHNESS_TOLERANCE_SECONDS = int(
    os.environ.get("AURIS_E2E_RUN_FRESHNESS_TOLERANCE_SECONDS", "60")
)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333").rstrip("/")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
DAGSTER_RECEIPT_LOG = os.environ.get("AURIS_E2E_DAGSTER_RECEIPT_LOG")
CALLBACK_RECEIPT_LOG = os.environ.get("AURIS_E2E_CALLBACK_RECEIPT_LOG")
BFF_URL = os.environ.get("AURIS_E2E_BFF_URL", "").rstrip("/")
EXPECTED_E2E_RUN_ID = os.environ.get("AURIS_E2E_RUN_ID", "").strip()
VERIFY_COMPLETION_RECEIPTS = (
    os.environ.get("AURIS_E2E_VERIFY_COMPLETION_RECEIPTS", "1") != "0"
)
REQUIRE_COMPLETION_RECEIPTS = (
    os.environ.get("AURIS_E2E_REQUIRE_COMPLETION_RECEIPTS", "1") != "0"
)
COMPLETION_ADAPTERS = {"dagster", "object_storage", "external_callback"}
REQUIRED_COMPLETION_ADAPTERS = {
    item.strip()
    for item in os.environ.get(
        "AURIS_E2E_REQUIRED_COMPLETION_ADAPTERS",
        "dagster,object_storage,external_callback",
    ).split(",")
    if item.strip()
}
REQUIRED_DISPATCH_ADAPTERS = {
    item.strip()
    for item in os.environ.get(
        "AURIS_E2E_REQUIRED_DISPATCH_ADAPTERS",
        "dagster,object_storage,external_callback,qdrant",
    ).split(",")
    if item.strip()
}
REQUIRED_DISPATCH_RUN_TYPES = {
    item.strip()
    for item in os.environ.get(
        "AURIS_E2E_REQUIRED_DISPATCH_RUN_TYPES",
        "knowledge_sync,knowledge_build,audio_intelligence",
    ).split(",")
    if item.strip()
}
REQUIRE_QDRANT_RECALL = os.environ.get("AURIS_E2E_REQUIRE_QDRANT_RECALL", "1") != "0"
REQUIRED_AGENTIC_RUN_TYPES = {
    item.strip()
    for item in os.environ.get(
        "AURIS_E2E_REQUIRED_AGENTIC_RUN_TYPES",
        "eval_feedback",
    ).split(",")
    if item.strip()
}
VERIFY_COMPLETION_REPLAY = (
    os.environ.get("AURIS_E2E_VERIFY_COMPLETION_REPLAY", "1") != "0"
)
COMPLETION_RECEIPT_SECRET = (
    os.environ.get("COMPLETION_RECEIPT_SECRET")
    or os.environ.get("AURIS_E2E_COMPLETION_RECEIPT_SECRET")
    or "auris-e2e-completion-secret-32chars-minimum"
)
COMPLETION_RECEIPT_KEY_ID = (
    os.environ.get("COMPLETION_RECEIPT_SIGNATURE_ID")
    or os.environ.get("AURIS_E2E_COMPLETION_RECEIPT_KEY_ID")
    or "auris-e2e-completion"
)
COMPLETION_EXTERNAL_ID_KEYS = {
    "dagster": "external_run_id",
    "object_storage": "storage_object_id",
    "external_callback": "callback_receipt_id",
}
DEFAULT_E2E_HEADERS = {
    "Authorization": "Bearer dev-token",
    "X-Tenant-Id": "aurora_auto",
    "X-Project-Id": "sales_qa",
}
RUN_EVENT_TYPES: dict[str, str] = {
    "audio_ingest": "audio_ingest.requested",
    "audio_intelligence": "audio_intelligence.requested",
    "asset_backfill": "backfill.requested",
    "asset_check_retry": "asset_check.retry_requested",
    "eval_feedback": "agent_run.requested",
    "eval_run": "eval_run.requested",
    "export": "export.requested",
    "external_callback": "external_callback.requested",
    "insight_metric_aggregation": "insight_metric_aggregation.requested",
    "insight_report": "export.requested",
    "knowledge_build": "knowledge_index.build_requested",
    "knowledge_sync": "knowledge_source.sync_requested",
    "label_publish": "label_version.publish_requested",
    "provider_test": "provider_test.requested",
    "release_command": "release_deployment.command-requested",
    "settings_publish": "settings.publish_requested",
    "task_run": "task_run.requested",
    "task_version_publish": "task_version.publish_requested",
}


@dataclass(frozen=True)
class ExpectedRun:
    label: str
    run_id: str
    trace_id: str | None
    adapter: str | None
    expected_status: str = "success"
    expected_business_status: str | None = None


def expected_status_for_adapter(adapter: str | None) -> tuple[str, str | None]:
    if adapter in {"dagster", "external_callback", "object_storage"}:
        return "submitted", "awaiting_completion"
    if adapter == "qdrant":
        return "success", "completed"
    return "success", None


def fail(message: str, detail: Any | None = None) -> NoReturn:
    payload: dict[str, Any] = {"status": "failed", "message": message}
    if detail is not None:
        payload["detail"] = detail
    print(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), file=sys.stderr
    )
    raise SystemExit(1)


def load_artifact() -> dict[str, Any]:
    if not ARTIFACT_PATH.exists():
        fail("UI/BFF E2E artifact does not exist", {"path": str(ARTIFACT_PATH)})
    try:
        result = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(
            "UI/BFF E2E artifact is not valid JSON",
            {"path": str(ARTIFACT_PATH), "error": str(exc)},
        )
    if result.get("status") != "ok":
        fail("UI/BFF E2E artifact is not an ok run", {"status": result.get("status")})
    if EXPECTED_E2E_RUN_ID and result.get("runId") != EXPECTED_E2E_RUN_ID:
        fail(
            "UI/BFF E2E artifact does not belong to the current verification run",
            {
                "expected_run_id": EXPECTED_E2E_RUN_ID,
                "actual_run_id": result.get("runId"),
                "artifact": str(ARTIFACT_PATH),
            },
        )
    return result


def parse_artifact_started_at(result: dict[str, Any]) -> datetime:
    raw_started_at = result.get("startedAt")
    if not isinstance(raw_started_at, str) or not raw_started_at:
        fail(
            "UI/BFF E2E artifact is missing startedAt", {"artifact": str(ARTIFACT_PATH)}
        )
    try:
        return datetime.fromisoformat(raw_started_at.replace("Z", "+00:00")).astimezone(
            UTC
        )
    except ValueError:
        fail(
            "UI/BFF E2E artifact has invalid startedAt",
            {"startedAt": raw_started_at, "artifact": str(ARTIFACT_PATH)},
        )


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def maybe_run(
    value: Any,
    label: str,
    adapter: str | None,
    expected_status: str | None = None,
) -> ExpectedRun | None:
    if not isinstance(value, dict):
        return None
    run_id = value.get("run_id") or value.get("runId") or value.get("id")
    if not isinstance(run_id, str) or not run_id:
        return None
    # A workflow artifact can carry both its definition trace and the concrete
    # RunRecord trace (for example, a controlled experiment plus its sample
    # TaskRun). Outbox verification must bind to the latter.
    trace_id = (
        value.get("run_trace_id")
        or value.get("runTraceId")
        or value.get("trace_id")
        or value.get("traceId")
    )
    default_status, default_business_status = expected_status_for_adapter(adapter)
    return ExpectedRun(
        label=label,
        run_id=run_id,
        trace_id=str(trace_id) if trace_id else None,
        adapter=adapter,
        expected_status=expected_status or default_status,
        expected_business_status=None if expected_status else default_business_status,
    )


def collect_expected_runs(result: dict[str, Any]) -> list[ExpectedRun]:
    expected: list[ExpectedRun] = []

    for item in result.get("uiMutations") or []:
        if not isinstance(item, dict):
            continue
        run_id = item.get("id")
        module = item.get("module")
        adapter_by_prefix = {
            "asset_backfill_": "dagster",
            "eval_run_": "dagster",
            "knowledge_build_": "qdrant",
        }
        for prefix, adapter in adapter_by_prefix.items():
            if isinstance(run_id, str) and run_id.startswith(prefix):
                expected_status, expected_business_status = expected_status_for_adapter(
                    adapter
                )
                expected.append(
                    ExpectedRun(
                        label=f"uiMutations.{module}",
                        run_id=run_id,
                        trace_id=item.get("traceId"),
                        adapter=adapter,
                        expected_status=expected_status,
                        expected_business_status=expected_business_status,
                    )
                )
                break

    dispatch_paths: list[tuple[str, Any, str]] = [
        (
            "canvasToolbarActions.experiment",
            result.get("canvasToolbarActions", {}).get("experiment"),
            "dagster",
        ),
        (
            "canvasToolbarActions.publishGate",
            result.get("canvasToolbarActions", {}).get("publishGate"),
            "projection",
        ),
        (
            "canvasToolbarActions.runOnce",
            result.get("canvasToolbarActions", {}).get("runOnce"),
            "dagster",
        ),
        (
            "domainPageActions.knowledgeSync",
            result.get("domainPageActions", {}).get("knowledgeSync"),
            "qdrant",
        ),
        (
            "domainPageActions.knowledgeIndex",
            result.get("domainPageActions", {}).get("knowledgeIndex"),
            "qdrant",
        ),
        (
            "domainPageActions.settingsProviderTest",
            result.get("domainPageActions", {}).get("settingsProviderTest"),
            "dagster",
        ),
        ("dataExportAction", result.get("dataExportAction"), "object_storage"),
        ("globalExportAction", result.get("globalExportAction"), "object_storage"),
        ("assetPackageExport", result.get("assetPackageExport"), "object_storage"),
        (
            "coreFlows.audioIngest",
            result.get("coreFlows", {}).get("audioIngest"),
            "object_storage",
        ),
        (
            "coreFlows.audioIntelligence",
            result.get("coreFlows", {}).get("audioIntelligence"),
            "dagster",
        ),
        (
            "coreFlows.knowledgeSync",
            result.get("coreFlows", {}).get("knowledgeSync"),
            "qdrant",
        ),
        (
            "coreFlows.assetQualityRetry",
            result.get("coreFlows", {}).get("assetQualityRetry"),
            "dagster",
        ),
        (
            "coreFlows.externalCallback",
            result.get("coreFlows", {}).get("externalCallback"),
            "external_callback",
        ),
        (
            "coreFlows.exportRun",
            result.get("coreFlows", {}).get("exportRun"),
            "object_storage",
        ),
        ("evalRun", result.get("evalRun"), "dagster"),
        ("feedbackTask", result.get("feedbackTask"), "dagster"),
        ("insightMetricRun", result.get("insightMetricRun"), "dagster"),
        ("insightReport", result.get("insightReport"), "object_storage"),
        (
            "coreFlows.settingsPublish",
            result.get("coreFlows", {}).get("settingsPublish"),
            "projection",
        ),
    ]
    for label, value, adapter in dispatch_paths:
        item = maybe_run(value, label, adapter)
        if item:
            expected.append(item)

    audio_import = result.get("coreFlows", {}).get("audioImport")
    if isinstance(audio_import, dict) and audio_import.get("status") != "skipped":
        item = maybe_run(
            audio_import,
            "coreFlows.audioImport",
            "dagster",
            expected_status="success",
        )
        if item:
            expected.append(item)

    label_publish = result.get("coreFlows", {}).get("labelPublish")
    label_transitions = (
        label_publish.get("transitions") if isinstance(label_publish, dict) else None
    )
    if isinstance(label_transitions, dict):
        for transition_key in ("publish", "approveGray", "promote"):
            transition = label_transitions.get(transition_key)
            if not isinstance(transition, dict):
                continue
            command_run_id = transition.get("commandRunId")
            if not isinstance(command_run_id, str) or not command_run_id:
                continue
            expected_status, expected_business_status = expected_status_for_adapter(
                "dagster"
            )
            expected.append(
                ExpectedRun(
                    label=f"coreFlows.labelPublish.{transition_key}",
                    run_id=command_run_id,
                    trace_id=transition.get("commandTraceId"),
                    adapter="dagster",
                    expected_status=expected_status,
                    expected_business_status=expected_business_status,
                )
            )

    blocked_paths: list[tuple[str, Any]] = [
        ("coreFlows.taskPublish", result.get("coreFlows", {}).get("taskPublish")),
    ]
    for label, value in blocked_paths:
        item = maybe_run(value, label, adapter=None, expected_status="blocked")
        if item:
            expected.append(item)

    deduped: dict[str, ExpectedRun] = {}
    for item in expected:
        deduped.setdefault(item.run_id, item)
    return list(deduped.values())


def _expected_event_type(run: RunRecord) -> str | None:
    return RUN_EVENT_TYPES.get(run.run_type)


def _event_for_run(session: Any, run: RunRecord) -> OutboxEvent | None:
    filters = [
        OutboxEvent.aggregate_id == run.run_id,
        OutboxEvent.aggregate_type == run.run_type,
        OutboxEvent.tenant_id == run.tenant_id,
        OutboxEvent.project_id == run.project_id,
    ]
    expected_event_type = _expected_event_type(run)
    if expected_event_type:
        filters.append(OutboxEvent.event_type == expected_event_type)
    return session.scalar(
        select(OutboxEvent)
        .where(*filters)
        .order_by(OutboxEvent.event_id.desc())
        .limit(1)
    )


def wait_for_expected_outbox_dispatches(
    expected_runs: list[ExpectedRun],
) -> dict[str, Any]:
    started = time.monotonic()
    poll_count = 0
    last_observations: list[dict[str, Any]] = []

    while time.monotonic() - started < OUTBOX_DISPATCH_TIMEOUT_SECONDS:
        poll_count += 1
        pending: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        delivery_attempt_count = 0
        with SessionLocal() as session:
            for expected in expected_runs:
                run = session.get(RunRecord, expected.run_id)
                event = _event_for_run(session, run) if run else None
                target_event_status = (
                    "blocked" if expected.expected_status == "blocked" else "processed"
                )
                observation = {
                    "label": expected.label,
                    "run_id": expected.run_id,
                    "run_status": run.status if run else None,
                    "event_id": event.event_id if event else None,
                    "event_status": event.status if event else None,
                    "delivery_state": event.delivery_state if event else None,
                    "attempt_count": event.attempt_count if event else 0,
                    "reconcile_attempt_count": (
                        event.reconcile_attempt_count if event else 0
                    ),
                    "last_error": event.last_error if event else None,
                    "target_event_status": target_event_status,
                }
                observations.append(observation)
                if event:
                    delivery_attempt_count += (
                        event.attempt_count + event.reconcile_attempt_count
                    )
                    if event.status == "dead_letter":
                        fail(
                            "Managed outbox worker dead-lettered an E2E event",
                            observation,
                        )
                    if event.status in {"processed", "blocked"} and (
                        event.status != target_event_status
                    ):
                        fail(
                            "Managed outbox worker reached an unexpected terminal event status",
                            observation,
                        )
                if not event or event.status != target_event_status:
                    pending.append(observation)

        last_observations = observations
        if not pending:
            return {
                "poll_count": poll_count,
                "wait_seconds": round(time.monotonic() - started, 3),
                "event_count": len(observations),
                "delivery_attempt_count": delivery_attempt_count,
            }
        time.sleep(OUTBOX_POLL_INTERVAL_SECONDS)

    fail(
        "Timed out waiting for the managed outbox worker to dispatch E2E events",
        {
            "timeout_seconds": OUTBOX_DISPATCH_TIMEOUT_SECONDS,
            "poll_count": poll_count,
            "observations": last_observations,
        },
    )


def wait_for_aggregate_outbox_event(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    expected_status: str = "processed",
) -> OutboxEvent:
    started = time.monotonic()
    poll_count = 0
    last_observation: dict[str, Any] = {}
    while time.monotonic() - started < OUTBOX_DISPATCH_TIMEOUT_SECONDS:
        poll_count += 1
        with SessionLocal() as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_type == aggregate_type,
                    OutboxEvent.aggregate_id == aggregate_id,
                    OutboxEvent.event_type == event_type,
                    OutboxEvent.tenant_id == DEFAULT_E2E_HEADERS["X-Tenant-Id"],
                    OutboxEvent.project_id == DEFAULT_E2E_HEADERS["X-Project-Id"],
                )
                .order_by(OutboxEvent.event_id.desc())
                .limit(1)
            )
            last_observation = {
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "event_type": event_type,
                "event_id": event.event_id if event else None,
                "status": event.status if event else None,
                "delivery_state": event.delivery_state if event else None,
                "attempt_count": event.attempt_count if event else 0,
                "reconcile_attempt_count": event.reconcile_attempt_count
                if event
                else 0,
                "last_error": event.last_error if event else None,
                "poll_count": poll_count,
            }
            if event and event.status == expected_status:
                session.expunge(event)
                return event
            if event and event.status in {"processed", "blocked", "dead_letter"}:
                fail(
                    "Managed outbox worker reached an unexpected aggregate event status",
                    {**last_observation, "expected_status": expected_status},
                )
        time.sleep(OUTBOX_POLL_INTERVAL_SECONDS)
    fail(
        "Timed out waiting for the managed outbox worker aggregate event",
        {
            **last_observation,
            "expected_status": expected_status,
            "timeout_seconds": OUTBOX_DISPATCH_TIMEOUT_SECONDS,
        },
    )


def observe_managed_worker() -> dict[str, Any] | None:
    if not WORKER_HEALTH_PATH:
        return None
    path = Path(WORKER_HEALTH_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(
            "Managed outbox worker health state could not be read",
            {"path": str(path), "error": str(exc)},
        )
    heartbeat_at = payload.get("heartbeat_at")
    try:
        heartbeat = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
        heartbeat_age = (datetime.now(UTC) - ensure_aware(heartbeat)).total_seconds()
    except ValueError:
        fail(
            "Managed outbox worker health state has an invalid heartbeat",
            {"path": str(path), "heartbeat_at": heartbeat_at},
        )
    pid = payload.get("pid")
    if payload.get("status") != "running" or not isinstance(pid, int) or pid <= 0:
        fail(
            "Managed outbox worker is not running during E2E verification",
            {"path": str(path), "state": payload},
        )
    if heartbeat_age > WORKER_HEARTBEAT_MAX_AGE_SECONDS:
        fail(
            "Managed outbox worker heartbeat is stale",
            {
                "path": str(path),
                "heartbeat_age_seconds": heartbeat_age,
                "max_age_seconds": WORKER_HEARTBEAT_MAX_AGE_SECONDS,
                "state": payload,
            },
        )
    try:
        os.kill(pid, 0)
    except OSError as exc:
        fail(
            "Managed outbox worker process is not alive",
            {"path": str(path), "pid": pid, "error": str(exc)},
        )
    return {
        "path": str(path),
        "worker_id": payload.get("worker_id"),
        "pid": pid,
        "status": payload.get("status"),
        "healthy": payload.get("healthy"),
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": round(heartbeat_age, 3),
        "iteration_count": payload.get("iteration_count"),
        "processed_total": payload.get("processed_total"),
        "consecutive_errors": payload.get("consecutive_errors"),
        "last_error": payload.get("last_error"),
    }


def get_run_and_event(expected: ExpectedRun) -> tuple[RunRecord, OutboxEvent]:
    with SessionLocal() as session:
        run = session.get(RunRecord, expected.run_id)
        if not run:
            fail("Expected E2E run record not found", {"run_id": expected.run_id})
        expected_event_type = _expected_event_type(run)
        event = _event_for_run(session, run)
        if not event:
            fail(
                "Expected E2E outbox event not found",
                {
                    "run_id": expected.run_id,
                    "run_type": run.run_type,
                    "expected_event_type": expected_event_type,
                },
            )
        if event.payload.get("trace_id") != run.trace_id:
            fail(
                "E2E outbox event trace does not match run trace",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "run_trace_id": run.trace_id,
                    "event_trace_id": event.payload.get("trace_id"),
                    "event_id": event.event_id,
                },
            )
        session.expunge(run)
        session.expunge(event)
        return run, event


def read_qdrant_point(collection: str, point_id: str) -> dict[str, Any]:
    url = f"{QDRANT_URL}/collections/{collection}/points/{point_id}"
    request = UrlRequest(
        url,
        headers={"api-key": QDRANT_API_KEY} if QDRANT_API_KEY else {},
        method="GET",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        fail(
            "Real Qdrant dispatch receipt could not be verified against Qdrant",
            {"url": url, "error": str(exc)},
        )
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        fail(
            "Real Qdrant point response is missing result",
            {"url": url, "payload": payload},
        )
    return result


def validate_real_qdrant_point(expected: ExpectedRun, details: dict[str, Any]) -> None:
    if details.get("mode") != "real":
        return
    collection = details.get("collection")
    point_ids = details.get("point_ids")
    expected_payload = details.get("qdrant_payload")
    if (
        not isinstance(collection, str)
        or not isinstance(point_ids, list)
        or not point_ids
    ):
        fail(
            "Real Qdrant dispatch receipt is missing collection or point id",
            {"label": expected.label, "details": details},
        )
    if not isinstance(expected_payload, dict):
        fail(
            "Real Qdrant dispatch receipt is missing qdrant_payload",
            {"label": expected.label, "details": details},
        )
    point = read_qdrant_point(collection, str(point_ids[0]))
    actual_payload = point.get("payload")
    if not isinstance(actual_payload, dict):
        fail(
            "Real Qdrant point is missing payload",
            {
                "label": expected.label,
                "collection": collection,
                "point_id": point_ids[0],
            },
        )
    for key in required_qdrant_payload_fields(expected.run_id, expected.label):
        if actual_payload.get(key) != expected_payload.get(key):
            fail(
                "Real Qdrant point payload does not match dispatch receipt",
                {
                    "label": expected.label,
                    "collection": collection,
                    "point_id": point_ids[0],
                    "field": key,
                    "expected": expected_payload.get(key),
                    "actual": actual_payload.get(key),
                },
            )


def required_qdrant_payload_fields(run_id: str, label: str) -> tuple[str, ...]:
    fields = [
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
    ]
    if run_id.startswith("knowledge_build_") or "knowledgeIndex" in label:
        fields.append("knowledge_index_id")
    return tuple(fields)


def _strong_object_etag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        return None
    if normalized.startswith('"') or normalized.endswith('"'):
        if len(normalized) < 2 or not (
            normalized.startswith('"') and normalized.endswith('"')
        ):
            return None
        normalized = normalized[1:-1]
    if (
        not normalized
        or '"' in normalized
        or any(
            ord(character) < 0x21 or ord(character) == 0x7F for character in normalized
        )
    ):
        return None
    return normalized


def _real_object_storage_binding(label: str, details: dict[str, Any]) -> dict[str, Any]:
    provider_value = details.get("provider")
    provider = provider_value.strip().lower() if isinstance(provider_value, str) else ""
    bucket = details.get("bucket")
    object_key = details.get("object_key")
    object_uri = details.get("object_uri")
    etag = _strong_object_etag(details.get("etag"))
    content_type = details.get("content_type")
    content_sha256 = details.get("content_sha256")
    content_length = details.get("content_length")
    version_value = details.get("version_id")
    version_id = version_value if isinstance(version_value, str) else None

    invalid_fields: list[str] = []
    if not provider or provider_value != provider:
        invalid_fields.append("provider")
    if not isinstance(bucket, str) or not bucket or bucket != bucket.strip():
        invalid_fields.append("bucket")
    if (
        not isinstance(object_key, str)
        or not object_key
        or object_key != object_key.strip()
        or object_key.startswith("/")
        or "\\" in object_key
        or any(part in {"", ".", ".."} for part in object_key.split("/"))
    ):
        invalid_fields.append("object_key")
    if etag is None:
        invalid_fields.append("etag")
    if (
        not isinstance(content_type, str)
        or not content_type
        or content_type != content_type.strip()
        or "/" not in content_type
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in content_type
        )
    ):
        invalid_fields.append("content_type")
    if (
        not isinstance(content_sha256, str)
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        invalid_fields.append("content_sha256")
    if (
        not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length <= 0
    ):
        invalid_fields.append("content_length")
    if version_value is not None and (
        version_id is None
        or not version_id
        or version_id != version_id.strip()
        or version_id.casefold() == "null"
        or len(version_id) > 1024
        or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in version_id
        )
    ):
        invalid_fields.append("version_id")

    if invalid_fields:
        fail(
            "Real object storage receipt has invalid immutable locator fields",
            {"label": label, "invalid_fields": sorted(set(invalid_fields))},
        )

    assert isinstance(bucket, str)
    assert isinstance(object_key, str)
    assert isinstance(content_type, str)
    assert isinstance(content_sha256, str)
    assert isinstance(content_length, int)
    assert etag is not None
    expected_scheme = "s3" if provider in {"minio", "s3"} else provider
    expected_uri = f"{expected_scheme}://{bucket}/{object_key}"
    if object_uri != expected_uri:
        fail(
            "Real object storage receipt object_uri does not match its locator",
            {
                "label": label,
                "expected_object_uri": expected_uri,
                "actual_object_uri": object_uri,
            },
        )
    return {
        "provider": provider,
        "bucket": bucket,
        "object_key": object_key,
        "object_uri": expected_uri,
        "etag": etag,
        "content_type": content_type,
        "content_sha256": content_sha256,
        "content_length": content_length,
        "version_id": version_id,
    }


def _validate_object_response_metadata(
    *,
    label: str,
    operation: str,
    response: object,
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(response, dict):
        fail(
            f"Real object storage {operation} returned an invalid response",
            {"label": label},
        )
    try:
        status = int(response.get("status") or 0)
        content_length = int(response.get("content_length"))
    except (TypeError, ValueError):
        fail(
            f"Real object storage {operation} returned invalid status or length metadata",
            {"label": label},
        )
    mismatches: list[str] = []
    if status != 200:
        mismatches.append("status")
    if _strong_object_etag(response.get("etag")) != binding["etag"]:
        mismatches.append("etag")
    if content_length != binding["content_length"]:
        mismatches.append("content_length")
    if response.get("content_type") != binding["content_type"]:
        mismatches.append("content_type")
    expected_version = binding.get("version_id")
    if expected_version is not None and response.get("version_id") != expected_version:
        mismatches.append("version_id")
    if operation == "GET" and response.get("content_range") not in (None, ""):
        mismatches.append("content_range")
    if mismatches:
        fail(
            f"Real object storage {operation} metadata does not match dispatch receipt",
            {"label": label, "mismatches": mismatches},
        )
    return response


def validate_real_object_storage_object(
    expected: ExpectedRun, details: dict[str, Any]
) -> None:
    if details.get("mode") != "real":
        return
    binding = _real_object_storage_binding(expected.label, details)
    bucket = binding["bucket"]
    object_key = binding["object_key"]
    conditional_etag = f'"{binding["etag"]}"'
    try:
        client = object_storage_client_for_provider(binding["provider"])
        if not client.allows_bucket(bucket):
            fail(
                "Real object storage receipt bucket is not allowed for its provider",
                {
                    "label": expected.label,
                    "provider": binding["provider"],
                    "bucket": bucket,
                },
            )
        head = client.head_object(
            bucket,
            object_key,
            if_match=conditional_etag,
            version_id=binding["version_id"],
        )
        response = client.get_object(
            bucket,
            object_key,
            if_match=conditional_etag,
            version_id=binding["version_id"],
        )
    except (OSError, URLError, HTTPError, TimeoutError, TypeError, ValueError) as exc:
        fail(
            "Real object storage dispatch receipt could not be verified against object storage",
            {
                "label": expected.label,
                "provider": binding["provider"],
                "bucket": bucket,
                "object_key": object_key,
                "error": str(exc),
            },
        )
    _validate_object_response_metadata(
        label=expected.label,
        operation="HEAD",
        response=head,
        binding=binding,
    )
    response = _validate_object_response_metadata(
        label=expected.label,
        operation="GET",
        response=response,
        binding=binding,
    )
    body = response.get("body")
    if not isinstance(body, bytes):
        fail(
            "Real object storage GET did not return a byte body",
            {"label": expected.label, "bucket": bucket, "object_key": object_key},
        )
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if actual_sha256 != binding["content_sha256"]:
        fail(
            "Real object storage object content hash does not match dispatch receipt",
            {
                "label": expected.label,
                "bucket": bucket,
                "object_key": object_key,
                "expected_sha256": binding["content_sha256"],
                "actual_sha256": actual_sha256,
            },
        )
    if len(body) != binding["content_length"]:
        fail(
            "Real object storage object length does not match dispatch receipt",
            {
                "label": expected.label,
                "bucket": bucket,
                "object_key": object_key,
                "expected_length": binding["content_length"],
                "actual_length": len(body),
            },
        )


def read_dagster_receipt(receipt_url: str) -> dict[str, Any]:
    try:
        with urlopen(receipt_url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        fail(
            "Real Dagster dispatch receipt could not be verified against Dagster protocol endpoint",
            {"receipt_url": receipt_url, "error": str(exc)},
        )
    receipt = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(receipt, dict):
        fail(
            "Real Dagster receipt endpoint did not return a data object",
            {"receipt_url": receipt_url, "payload": payload},
        )
    return receipt


def read_dagster_receipt_log() -> list[dict[str, Any]]:
    if not DAGSTER_RECEIPT_LOG:
        return []
    path = Path(DAGSTER_RECEIPT_LOG)
    if not path.exists():
        fail("Real Dagster receipt log does not exist", {"path": str(path)})
    receipts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(
                "Real Dagster receipt log contains invalid JSON",
                {"line": line, "error": str(exc)},
            )
        if isinstance(receipt, dict):
            receipts.append(receipt)
    return receipts


def read_callback_receipt(receipt_url: str) -> dict[str, Any]:
    try:
        with urlopen(receipt_url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
        fail(
            "Real external callback receipt could not be verified against callback endpoint",
            {"receipt_url": receipt_url, "error": str(exc)},
        )
    receipt = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(receipt, dict):
        fail(
            "Real external callback receipt endpoint did not return a data object",
            {"receipt_url": receipt_url, "payload": payload},
        )
    return receipt


def read_callback_receipt_log() -> list[dict[str, Any]]:
    if not CALLBACK_RECEIPT_LOG:
        return []
    path = Path(CALLBACK_RECEIPT_LOG)
    if not path.exists():
        fail("Real external callback receipt log does not exist", {"path": str(path)})
    receipts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as exc:
            fail(
                "Real external callback receipt log contains invalid JSON",
                {"line": line, "error": str(exc)},
            )
        if isinstance(receipt, dict):
            receipts.append(receipt)
    return receipts


def tag_map(tags: Any) -> dict[str, str]:
    if not isinstance(tags, list):
        return {}
    mapped = {}
    for item in tags:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        value = item.get("value")
        if isinstance(key, str):
            mapped[key] = "" if value is None else str(value)
    return mapped


def validate_real_dagster_run(expected: ExpectedRun, details: dict[str, Any]) -> None:
    if details.get("mode") != "real":
        return
    required = [
        key
        for key in (
            "external_run_id",
            "dagster_run_id",
            "run_request_id",
            "job_name",
            "run_key",
            "run_id",
            "graphql_payload_sha256",
            "request_sha256",
            "response_typename",
        )
        if not details.get(key)
    ]
    if required:
        fail(
            "Real Dagster dispatch receipt is missing required fields",
            {"label": expected.label, "missing": required, "details": details},
        )
    protocol_receipt = details.get("protocol_receipt")
    if not isinstance(protocol_receipt, dict) or not protocol_receipt.get(
        "receipt_url"
    ):
        fail(
            "Real Dagster dispatch receipt is missing protocol receipt URL",
            {"label": expected.label, "details": details},
        )
    receipt = read_dagster_receipt(str(protocol_receipt["receipt_url"]))
    if receipt.get("run_id") != details.get("external_run_id"):
        fail(
            "Real Dagster protocol receipt run id does not match dispatch receipt",
            {
                "label": expected.label,
                "expected": details.get("external_run_id"),
                "actual": receipt.get("run_id"),
            },
        )
    if receipt.get("path") != "/graphql":
        fail(
            "Real Dagster protocol receipt did not capture /graphql request",
            {"label": expected.label, "receipt": receipt},
        )
    if receipt.get("request_sha256") != details.get("graphql_payload_sha256"):
        fail(
            "Real Dagster request hash does not match dispatch receipt",
            {
                "label": expected.label,
                "expected": details.get("graphql_payload_sha256"),
                "actual": receipt.get("request_sha256"),
            },
        )
    if receipt.get("run_key") != details.get("run_key"):
        fail(
            "Real Dagster run key does not match dispatch receipt",
            {
                "label": expected.label,
                "expected": details.get("run_key"),
                "actual": receipt.get("run_key"),
            },
        )
    if receipt.get("job_name") != details.get("job_name"):
        fail(
            "Real Dagster job name does not match dispatch receipt",
            {
                "label": expected.label,
                "expected": details.get("job_name"),
                "actual": receipt.get("job_name"),
            },
        )
    tags = tag_map(receipt.get("tags"))
    expected_tags = {
        "tenant_id": details.get("tenant_id"),
        "project_id": details.get("project_id"),
        "trace_id": details.get("trace_id") or expected.trace_id,
        "run_id": details.get("run_id") or expected.run_id,
    }
    for key, value in expected_tags.items():
        expected_value = str(value or "")
        if not expected_value or tags.get(key) != expected_value:
            fail(
                "Real Dagster protocol tags do not match dispatch receipt",
                {
                    "label": expected.label,
                    "tag": key,
                    "expected": expected_value,
                    "actual": tags.get(key),
                    "tags": tags,
                },
            )
    log_receipts = read_dagster_receipt_log()
    if log_receipts and not any(
        item.get("run_id") == details.get("external_run_id")
        and item.get("request_sha256") == details.get("graphql_payload_sha256")
        for item in log_receipts
    ):
        fail(
            "Real Dagster receipt was not found in protocol log",
            {
                "label": expected.label,
                "external_run_id": details.get("external_run_id"),
                "receipt_log": DAGSTER_RECEIPT_LOG,
            },
        )


def validate_real_external_callback(
    expected: ExpectedRun, details: dict[str, Any]
) -> None:
    if details.get("mode") != "real":
        return
    required = [
        key
        for key in (
            "callback_receipt_id",
            "callback_url",
            "idempotency_key",
            "remote_trace_id",
            "request_sha256",
            "response_sha256",
            "receipt_url",
            "signature_key_id",
            "signature_mode",
            "signature_version",
            "signature_sha256",
            "status_code",
            "tenant_id",
            "project_id",
            "trace_id",
            "run_id",
        )
        if not details.get(key)
    ]
    if required:
        fail(
            "Real external callback dispatch receipt is missing required fields",
            {"label": expected.label, "missing": required, "details": details},
        )
    receipt = read_callback_receipt(str(details["receipt_url"]))
    expected_fields = {
        "callback_receipt_id": details.get("callback_receipt_id"),
        "tenant_id": details.get("tenant_id"),
        "project_id": details.get("project_id"),
        "trace_id": details.get("trace_id") or expected.trace_id,
        "run_id": details.get("run_id") or expected.run_id,
        "idempotency_key": details.get("idempotency_key"),
        "request_sha256": details.get("request_sha256"),
        "response_sha256": details.get("response_sha256"),
        "signature_key_id": details.get("signature_key_id"),
        "signature_mode": details.get("signature_mode"),
        "signature_version": details.get("signature_version"),
        "signature_sha256": details.get("signature_sha256"),
        "remote_trace_id": details.get("remote_trace_id"),
    }
    for key, expected_value in expected_fields.items():
        if str(receipt.get(key) or "") != str(expected_value or ""):
            fail(
                "Real external callback receipt does not match dispatch receipt",
                {
                    "label": expected.label,
                    "field": key,
                    "expected": expected_value,
                    "actual": receipt.get(key),
                    "receipt": receipt,
                },
            )
    if receipt.get("signature_valid") is not True:
        fail(
            "Real external callback receipt did not validate signature",
            {"label": expected.label, "receipt": receipt},
        )
    if receipt.get("path") != "/callbacks/platform":
        fail(
            "Real external callback receipt did not capture platform callback path",
            {"label": expected.label, "receipt": receipt},
        )
    log_receipts = read_callback_receipt_log()
    if log_receipts and not any(
        item.get("callback_receipt_id") == details.get("callback_receipt_id")
        and item.get("request_sha256") == details.get("request_sha256")
        for item in log_receipts
    ):
        fail(
            "Real external callback receipt was not found in protocol log",
            {
                "label": expected.label,
                "callback_receipt_id": details.get("callback_receipt_id"),
                "receipt_log": CALLBACK_RECEIPT_LOG,
            },
        )
    with SessionLocal() as session:
        stored_receipt = session.get(
            ExternalCallbackReceipt, str(details["callback_receipt_id"])
        )
        if not stored_receipt:
            fail(
                "Real external callback receipt was not persisted in the database",
                {
                    "label": expected.label,
                    "callback_receipt_id": details.get("callback_receipt_id"),
                },
            )
        stored_payload = stored_receipt.payload
        if stored_receipt.tenant_id != details.get("tenant_id"):
            fail(
                "Persisted external callback receipt tenant does not match dispatch receipt",
                {
                    "label": expected.label,
                    "expected": details.get("tenant_id"),
                    "actual": stored_receipt.tenant_id,
                },
            )
        if stored_receipt.project_id != details.get("project_id"):
            fail(
                "Persisted external callback receipt project does not match dispatch receipt",
                {
                    "label": expected.label,
                    "expected": details.get("project_id"),
                    "actual": stored_receipt.project_id,
                },
            )
        if stored_receipt.trace_id != details.get("trace_id"):
            fail(
                "Persisted external callback receipt trace does not match dispatch receipt",
                {
                    "label": expected.label,
                    "expected": details.get("trace_id"),
                    "actual": stored_receipt.trace_id,
                },
            )
        if stored_payload.get("run_id") != expected.run_id:
            fail(
                "Persisted external callback receipt run does not match browser artifact",
                {
                    "label": expected.label,
                    "expected": expected.run_id,
                    "actual": stored_payload.get("run_id"),
                },
            )


def has_expected_e2e_completion(expected: ExpectedRun, run: RunRecord) -> bool:
    completion = run.payload.get("completion_receipt")
    return (
        expected.expected_status == "submitted"
        and expected.adapter in COMPLETION_ADAPTERS
        and run.status == "success"
        and isinstance(completion, dict)
        and completion.get("completion_receipt_id")
        == expected_completion_receipt_id(run)
        and completion.get("external_id")
        == expected_completion_external_id(expected, run)
    )


def validate_dispatch(
    expected: ExpectedRun, run: RunRecord, event: OutboxEvent, started_at: datetime
) -> dict[str, Any]:
    freshness_floor = started_at - timedelta(seconds=FRESHNESS_TOLERANCE_SECONDS)
    if ensure_aware(run.created_at) < freshness_floor:
        fail(
            "E2E run record is older than the current browser artifact",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "run_created_at": ensure_aware(run.created_at).isoformat(),
                "artifact_started_at": started_at.isoformat(),
                "freshness_tolerance_seconds": FRESHNESS_TOLERANCE_SECONDS,
                "hint": "A stale idempotency replay was probably reused; prefix UI write keys with the E2E run id.",
            },
        )
    e2e_completed = has_expected_e2e_completion(expected, run)
    if run.status != expected.expected_status and not e2e_completed:
        fail(
            "E2E run ended in unexpected status",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "expected_status": expected.expected_status,
                "actual_status": run.status,
                "run_payload": run.payload,
                "event_status": event.status,
                "event_error": event.last_error,
            },
        )
    if (
        expected.expected_business_status
        and run.payload.get("business_status") != expected.expected_business_status
        and not e2e_completed
    ):
        fail(
            "E2E run has unexpected business status",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "expected_business_status": expected.expected_business_status,
                "actual_business_status": run.payload.get("business_status"),
                "run_payload": run.payload,
            },
        )
    if expected.trace_id and run.trace_id != expected.trace_id:
        fail(
            "E2E run trace does not match browser artifact",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "expected_trace_id": expected.trace_id,
                "actual_trace_id": run.trace_id,
            },
        )

    if expected.expected_status == "blocked":
        if event.status != "blocked":
            fail(
                "Blocked E2E run did not block its outbox event",
                {
                    "label": expected.label,
                    "run_id": expected.run_id,
                    "event_id": event.event_id,
                    "event_status": event.status,
                    "event_error": event.last_error,
                },
            )
        return {
            "label": expected.label,
            "run_id": expected.run_id,
            "run_type": run.run_type,
            "status": run.status,
            "event_id": event.event_id,
            "event_status": event.status,
        }

    if event.status != "processed":
        fail(
            "Dispatchable E2E run did not process its outbox event",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "event_id": event.event_id,
                "event_status": event.status,
                "event_error": event.last_error,
                "event_payload": event.payload,
            },
        )
    dispatch = run.payload.get("dispatch") or event.payload.get("adapter_dispatch")
    if not isinstance(dispatch, dict):
        fail(
            "Processed E2E run is missing adapter dispatch receipt",
            {"label": expected.label, "run_id": expected.run_id},
        )
    if dispatch.get("adapter") != expected.adapter:
        fail(
            "E2E dispatch used unexpected adapter",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "expected_adapter": expected.adapter,
                "dispatch": dispatch,
            },
        )
    details = dispatch.get("details")
    if not isinstance(details, dict):
        fail(
            "E2E dispatch receipt is missing details",
            {"label": expected.label, "run_id": expected.run_id, "dispatch": dispatch},
        )

    if expected.adapter == "dagster":
        missing = [
            key for key in ("external_run_id", "run_request_id") if not details.get(key)
        ]
    elif expected.adapter == "qdrant":
        missing = [
            key
            for key in ("collection", "point_ids", "qdrant_payload")
            if not details.get(key)
        ]
        payload = (
            details.get("qdrant_payload")
            if isinstance(details.get("qdrant_payload"), dict)
            else {}
        )
        missing += [
            key
            for key in required_qdrant_payload_fields(run.run_id, expected.label)
            if not payload.get(key)
        ]
    elif expected.adapter == "object_storage":
        missing = [
            key for key in ("storage_object_id", "object_uri") if not details.get(key)
        ]
        if details.get("mode") == "real":
            missing += [
                key
                for key in (
                    "provider",
                    "bucket",
                    "object_key",
                    "etag",
                    "content_type",
                    "content_sha256",
                    "content_length",
                )
                if not details.get(key)
            ]
    elif expected.adapter == "external_callback":
        if details.get("mode") == "real":
            missing = [
                key
                for key in (
                    "callback_receipt_id",
                    "receipt_url",
                    "request_sha256",
                    "response_sha256",
                    "signature_key_id",
                    "signature_mode",
                    "signature_version",
                    "signature_sha256",
                )
                if not details.get(key)
            ]
        else:
            missing = [
                key
                for key in ("callback_receipt_id", "signature_id", "signature_mode")
                if not details.get(key)
            ]
    else:
        missing = []
    if missing:
        fail(
            "E2E dispatch receipt is missing required adapter fields",
            {
                "label": expected.label,
                "run_id": expected.run_id,
                "adapter": expected.adapter,
                "missing": missing,
                "dispatch": dispatch,
            },
        )
    if expected.adapter == "qdrant":
        validate_real_qdrant_point(expected, details)
    if expected.adapter == "object_storage":
        validate_real_object_storage_object(expected, details)
    if expected.adapter == "dagster":
        validate_real_dagster_run(expected, details)
    if expected.adapter == "external_callback":
        validate_real_external_callback(expected, details)

    return {
        "label": expected.label,
        "run_id": expected.run_id,
        "run_type": run.run_type,
        "status": run.status,
        "business_status": run.payload.get("business_status"),
        "event_id": event.event_id,
        "event_status": event.status,
        "adapter": dispatch.get("adapter"),
        "operation": dispatch.get("operation"),
        "receipt_keys": sorted(details.keys()),
    }


def bff_json_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    trace_id: str | None = None,
    auth_token: str | None = None,
    include_default_auth: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not BFF_URL:
        fail(
            "AURIS_E2E_BFF_URL is required to verify completion receipts through the BFF",
            {"path": path},
        )
    encoded_payload = None
    headers = {
        **{
            key: value
            for key, value in DEFAULT_E2E_HEADERS.items()
            if include_default_auth or key != "Authorization"
        },
        "X-Request-Id": (
            f"e2e-completion-{hashlib.sha256(path.encode()).hexdigest()[:12]}"
        ),
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if payload is not None:
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    if extra_headers:
        headers.update(extra_headers)
    request = UrlRequest(
        f"{BFF_URL}{path}",
        data=encoded_payload,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        fail(
            "BFF request failed while verifying completion receipt",
            {
                "method": method,
                "path": path,
                "status": exc.code,
                "body": error_body,
            },
        )
    except (OSError, URLError, TimeoutError, ValueError) as exc:
        fail(
            "BFF request could not be completed while verifying completion receipt",
            {"method": method, "path": path, "error": str(exc)},
        )


def completion_endpoint(run: RunRecord) -> str:
    return f"/api/v1/runs/{run.run_id}/external-completion-receipts"


def signed_completion_headers(
    *,
    method: str,
    path: str,
    payload: dict[str, Any],
    idempotency_key: str,
    source: str,
) -> dict[str, str]:
    if len(COMPLETION_RECEIPT_SECRET) < 32:
        fail(
            "Completion receipt signing secret is not configured for E2E",
            {"secret_length": len(COMPLETION_RECEIPT_SECRET)},
        )
    encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body_sha256 = hashlib.sha256(encoded_payload).hexdigest()
    timestamp = datetime.now(UTC).isoformat()
    nonce = (
        f"nonce_{hashlib.sha256(f'{path}:{idempotency_key}'.encode()).hexdigest()[:24]}"
    )
    message = "\n".join(
        [
            "auris-completion-v1",
            method.upper(),
            path,
            "",
            DEFAULT_E2E_HEADERS["X-Tenant-Id"],
            DEFAULT_E2E_HEADERS["X-Project-Id"],
            idempotency_key,
            timestamp,
            nonce,
            COMPLETION_RECEIPT_KEY_ID,
            source,
            body_sha256,
        ]
    )
    signature = hmac.new(
        COMPLETION_RECEIPT_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Auris-Key-Id": COMPLETION_RECEIPT_KEY_ID,
        "X-Auris-Timestamp": timestamp,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": source,
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def dispatch_details(run: RunRecord) -> dict[str, Any]:
    dispatch = run.payload.get("dispatch")
    details = dispatch.get("details") if isinstance(dispatch, dict) else None
    if not isinstance(details, dict):
        fail(
            "Run is missing dispatch details",
            {"run_id": run.run_id, "payload": run.payload},
        )
    return details


def expected_completion_external_id(expected: ExpectedRun, run: RunRecord) -> str:
    details = dispatch_details(run)
    adapter = expected.adapter
    key = COMPLETION_EXTERNAL_ID_KEYS.get(str(adapter))
    external_id = details.get(key) if key else None
    if not isinstance(external_id, str) or not external_id:
        fail(
            "Completion receipt cannot determine external id from dispatch details",
            {
                "label": expected.label,
                "run_id": run.run_id,
                "adapter": adapter,
                "external_id_key": key,
                "details": details,
            },
        )
    return external_id


def expected_completion_receipt_id(run: RunRecord) -> str:
    return f"e2e_complete_{run.run_id}"


def audio_intelligence_completion_result(run: RunRecord) -> dict[str, Any]:
    payload = run.payload if isinstance(run.payload, dict) else {}
    capabilities = payload.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, list) else []
    result: dict[str, Any] = {
        "source": "ui_bff_e2e",
        "run_id": run.run_id,
        "audio_session_id": payload.get("audio_session_id"),
        "recording_id": payload.get("recording_id"),
        "capability_statuses": {
            capability: {"status": "success"} for capability in capabilities
        },
    }
    if "vad" in capabilities:
        result["vad_segments"] = [
            {"start_ms": 30_000, "end_ms": 300_000, "confidence": 0.96}
        ]
    if "diarization" in capabilities:
        result["speaker_turns"] = [
            {
                "speaker": "销售A",
                "start_ms": 30_000,
                "end_ms": 128_000,
                "confidence": 0.92,
            },
            {
                "speaker": "客户",
                "start_ms": 128_000,
                "end_ms": 186_000,
                "confidence": 0.88,
            },
        ]
    if "asr" in capabilities:
        result["asr_segments"] = [
            {
                "speaker": "销售A",
                "start_ms": 30_780,
                "end_ms": 38_200,
                "text": "可以优惠 3.5 万，落地大概 28.19 万左右",
                "confidence": 0.91,
            }
        ]
    if "voiceprint" in capabilities:
        result.update(
            {
                "speaker_ref": "sales_a",
                "voiceprint_quality_score": 88,
                "voiceprint_embedding_ref": {
                    "collection": "voiceprint_embeddings",
                    "point_id": f"vp_{run.run_id}",
                    "vector_dim": 512,
                },
            }
        )
    if "quality" in capabilities:
        result.update({"snr_db": 23.8, "crosstalk_risk": "medium"})
    return result


def asset_materialization_completion_result(run: RunRecord) -> dict[str, Any]:
    payload = run.payload if isinstance(run.payload, dict) else {}
    storage_object_id = (
        f"sto_asset_{hashlib.sha256(run.run_id.encode()).hexdigest()[:24]}"
    )
    provider = os.environ.get("OBJECT_STORAGE_PROVIDER", "minio").strip().lower()
    bucket = os.environ.get("OBJECT_STORAGE_BUCKET", "auris-flow-local").strip()
    object_key = (
        f"tenants/{run.tenant_id}/projects/{run.project_id}/runs/{run.run_id}/"
        f"{storage_object_id}.jsonl"
    )
    content_sha256 = hashlib.sha256(
        f"asset-materialization:{run.run_id}".encode("utf-8")
    ).hexdigest()
    return {
        "source": "ui_bff_e2e",
        "run_id": run.run_id,
        "asset_key": payload.get("asset_key") or payload.get("target_asset_key"),
        "partition_key": run.partition_key or payload.get("partition_key"),
        "storage_object_id": storage_object_id,
        "storage_objects": [
            {
                "storage_object_id": storage_object_id,
                "role": "asset_materialization",
                "provider": provider,
                "bucket": bucket,
                "object_key": object_key,
                "content_type": "application/x-ndjson",
                "size_bytes": 256,
                "content_sha256": content_sha256,
                "etag": f"e2e-{storage_object_id}",
            }
        ],
    }


def completion_payload(expected: ExpectedRun, run: RunRecord) -> dict[str, Any]:
    details = dispatch_details(run)
    adapter = expected.adapter
    external_id = expected_completion_external_id(expected, run)
    result_ref = {
        "source": "ui_bff_e2e",
        "run_id": run.run_id,
        "object_uri": details.get("object_uri"),
        "external_run_id": details.get("external_run_id"),
        "callback_receipt_id": details.get("callback_receipt_id"),
    }
    if run.run_type == "audio_intelligence":
        result_ref = audio_intelligence_completion_result(run)
    elif run.run_type in {"asset_backfill", "asset_check_retry"}:
        result_ref = asset_materialization_completion_result(run)
    expected_bundle_sha256 = str(
        (run.payload or {}).get("expected_executed_bundle_sha256") or ""
    )
    if expected_bundle_sha256:
        result_ref = {
            **result_ref,
            "executed_task_version_binding_sha256": expected_bundle_sha256,
        }
    return {
        "adapter": adapter,
        "status": "success",
        "completion_receipt_id": expected_completion_receipt_id(run),
        "external_id": external_id,
        "result_ref": result_ref,
        "metrics": {"e2e_completion": True},
        "note": f"E2E completion receipt for {expected.label}",
    }


def verify_export_reserved(run: RunRecord) -> None:
    details = dispatch_details(run)
    response = bff_json_request(
        "GET", f"/api/v1/exports/{run.run_id}", trace_id=run.trace_id
    )
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        fail(
            "Export detail response is missing data before completion",
            {"run_id": run.run_id, "response": response},
        )
    download_ref = data.get("download_ref")
    if not isinstance(download_ref, dict):
        fail(
            "Submitted export detail is missing download_ref",
            {"run_id": run.run_id, "data": data},
        )
    if data.get("status") != "submitted" or download_ref.get("status") != "reserved":
        fail(
            "Export was not reserved before completion receipt",
            {
                "run_id": run.run_id,
                "status": data.get("status"),
                "download_ref": download_ref,
            },
        )
    if (
        download_ref.get("kind") != "bff_download"
        or download_ref.get("href") is not None
        or download_ref.get("content_type") != details.get("content_type")
    ):
        fail(
            "Reserved export does not expose the safe BFF download boundary",
            {
                "run_id": run.run_id,
                "expected_content_type": details.get("content_type"),
                "download_ref": download_ref,
            },
        )
    forbidden = {
        "storage_object_id",
        "object_uri",
        "provider",
        "bucket",
        "object_key",
        "etag",
    }
    leaked = sorted(forbidden.intersection(download_ref))
    if leaked:
        fail(
            "Reserved export leaked an internal object-storage locator",
            {"run_id": run.run_id, "leaked_fields": leaked},
        )


def _verify_real_export_download(
    run_id: str,
    details: dict[str, Any],
    trace_id: str,
    href: str,
) -> None:
    binding = _real_object_storage_binding(run_id, details)
    expected_length = binding["content_length"]
    expected_content_type = binding["content_type"]
    expected_etag = f'"{binding["etag"]}"'
    trace_headers = {"X-Trace-Id": trace_id}

    head = bff_binary_request("HEAD", href, extra_headers=trace_headers)
    head_headers = head.get("headers") if isinstance(head, dict) else None
    head_body = head.get("body") if isinstance(head, dict) else None
    if not isinstance(head_headers, dict):
        fail(
            "Completed real export HEAD response is missing headers",
            {"run_id": run_id, "response": head},
        )
    head_mismatches: list[str] = []
    if head.get("status") != 200:
        head_mismatches.append("status")
    if str(head_headers.get("accept-ranges") or "").lower() != "bytes":
        head_mismatches.append("accept-ranges")
    if head_headers.get("content-type") != expected_content_type:
        head_mismatches.append("content-type")
    if head_headers.get("content-length") != str(expected_length):
        head_mismatches.append("content-length")
    if head_headers.get("etag") != expected_etag:
        head_mismatches.append("etag")
    if head_body != b"":
        head_mismatches.append("body")
    if head_mismatches:
        fail(
            "Completed real export HEAD response does not match its immutable receipt",
            {"run_id": run_id, "mismatches": head_mismatches},
        )

    range_header = f"bytes=0-{expected_length - 1}"
    expected_content_range = f"bytes 0-{expected_length - 1}/{expected_length}"
    ranged = bff_binary_request(
        "GET",
        href,
        extra_headers={**trace_headers, "Range": range_header},
    )
    range_headers = ranged.get("headers") if isinstance(ranged, dict) else None
    range_body = ranged.get("body") if isinstance(ranged, dict) else None
    if not isinstance(range_headers, dict):
        fail(
            "Completed real export Range response is missing headers",
            {"run_id": run_id, "response": ranged},
        )
    range_mismatches: list[str] = []
    if ranged.get("status") != 206:
        range_mismatches.append("status")
    if str(range_headers.get("accept-ranges") or "").lower() != "bytes":
        range_mismatches.append("accept-ranges")
    if range_headers.get("content-range") != expected_content_range:
        range_mismatches.append("content-range")
    if range_headers.get("content-type") != expected_content_type:
        range_mismatches.append("content-type")
    if range_headers.get("content-length") != str(expected_length):
        range_mismatches.append("content-length")
    if range_headers.get("etag") != expected_etag:
        range_mismatches.append("etag")
    if not isinstance(range_body, bytes):
        range_mismatches.append("body_type")
    else:
        if len(range_body) != expected_length:
            range_mismatches.append("body_length")
        if hashlib.sha256(range_body).hexdigest() != binding["content_sha256"]:
            range_mismatches.append("body_sha256")
    if range_mismatches:
        fail(
            "Completed real export Range response does not match its immutable receipt",
            {"run_id": run_id, "mismatches": range_mismatches},
        )


def verify_export_ready(run_id: str, details: dict[str, Any], trace_id: str) -> None:
    real_mode = details.get("mode") == "real"
    if real_mode:
        _real_object_storage_binding(run_id, details)
    response = bff_json_request("GET", f"/api/v1/exports/{run_id}", trace_id=trace_id)
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        fail(
            "Export detail response is missing data",
            {"run_id": run_id, "response": response},
        )
    download_ref = data.get("download_ref")
    if not isinstance(download_ref, dict):
        fail(
            "Completed export detail is missing download_ref",
            {"run_id": run_id, "data": data},
        )
    has_streamable_locator = all(
        details.get(field) not in {None, ""}
        for field in ("provider", "bucket", "object_key", "etag", "content_length")
    )
    if real_mode and not has_streamable_locator:
        fail(
            "Completed real export is missing a streamable immutable locator",
            {"run_id": run_id},
        )
    expected_download_status = "ready" if has_streamable_locator else "unavailable"
    expected_href = (
        f"/api/v1/exports/{run_id}/download" if has_streamable_locator else None
    )
    if (
        data.get("status") != "success"
        or download_ref.get("kind") != "bff_download"
        or download_ref.get("status") != expected_download_status
        or download_ref.get("href") != expected_href
        or download_ref.get("content_type") != details.get("content_type")
    ):
        fail(
            "Completed export did not expose the expected safe BFF download boundary",
            {
                "run_id": run_id,
                "status": data.get("status"),
                "expected_download_status": expected_download_status,
                "expected_href": expected_href,
                "expected_content_type": details.get("content_type"),
                "download_ref": download_ref,
            },
        )
    forbidden = {
        "storage_object_id",
        "object_uri",
        "provider",
        "bucket",
        "object_key",
        "etag",
    }
    leaked = sorted(forbidden.intersection(download_ref))
    if leaked:
        fail(
            "Completed export leaked an internal object-storage locator",
            {"run_id": run_id, "leaked_fields": leaked},
        )
    if real_mode:
        assert isinstance(expected_href, str)
        _verify_real_export_download(run_id, details, trace_id, expected_href)


def verify_trace_completion(run: RunRecord) -> None:
    response = bff_json_request(
        "GET", f"/api/v1/traces/{run.trace_id}", trace_id=run.trace_id
    )
    data = response.get("data") if isinstance(response, dict) else None
    spans = data.get("spans") if isinstance(data, dict) else None
    if not isinstance(spans, list):
        fail(
            "Trace response is missing spans after completion",
            {"run_id": run.run_id, "trace_id": run.trace_id, "response": response},
        )
    has_success_run = any(
        isinstance(span, dict)
        and span.get("kind") == "run"
        and span.get("run_id") == run.run_id
        and span.get("status") == "success"
        for span in spans
    )
    has_completion_audit = any(
        isinstance(span, dict)
        and span.get("kind") == "audit"
        and span.get("object_id") == run.run_id
        and str(span.get("action") or "").endswith(".completion_received")
        for span in spans
    )
    if not has_success_run or not has_completion_audit:
        fail(
            "Trace does not show completed run and completion audit",
            {
                "run_id": run.run_id,
                "trace_id": run.trace_id,
                "has_success_run": has_success_run,
                "has_completion_audit": has_completion_audit,
                "spans": spans,
            },
        )


def bff_binary_request(
    method: str,
    path: str,
    *,
    extra_headers: dict[str, str] | None = None,
    include_default_context: bool = True,
) -> dict[str, Any]:
    if not BFF_URL:
        fail(
            "AURIS_E2E_BFF_URL is required to verify binary BFF responses",
            {"path": path},
        )
    headers = {
        **(DEFAULT_E2E_HEADERS if include_default_context else {}),
        "X-Request-Id": f"e2e-binary-{hashlib.sha1(path.encode()).hexdigest()[:12]}",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = UrlRequest(f"{BFF_URL}{path}", headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return {
                "status": response.status,
                "headers": {
                    key.lower(): value for key, value in response.headers.items()
                },
                "body": response.read(),
            }
    except HTTPError as exc:
        return {
            "status": exc.code,
            "headers": {key.lower(): value for key, value in exc.headers.items()},
            "body": exc.read(),
        }
    except (OSError, URLError, TimeoutError) as exc:
        fail(
            "BFF binary request could not be completed",
            {"method": method, "path": path, "error": str(exc)},
        )


def validate_completion_result(
    expected: ExpectedRun, before: RunRecord, response: dict[str, Any]
) -> dict[str, Any]:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        fail(
            "Completion receipt response is missing data",
            {"label": expected.label, "run_id": before.run_id, "response": response},
        )
    if data.get("status") != "success":
        fail(
            "Completion receipt response did not report success",
            {"label": expected.label, "run_id": before.run_id, "data": data},
        )
    with SessionLocal() as session:
        run = session.get(RunRecord, before.run_id)
        if not run:
            fail(
                "Completed run disappeared",
                {"label": expected.label, "run_id": before.run_id},
            )
        if run.status != "success":
            fail(
                "Completion receipt did not persist success status",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "status": run.status,
                    "payload": run.payload,
                },
            )
        if run.payload.get("business_status") != "completed":
            fail(
                "Completion receipt did not persist completed business status",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "business_status": run.payload.get("business_status"),
                    "payload": run.payload,
                },
            )
        if run.payload.get("business_completion_required") is not False:
            fail(
                "Completion receipt did not clear business completion requirement",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "payload": run.payload,
                },
            )
        completion = run.payload.get("completion_receipt")
        if not isinstance(completion, dict):
            fail(
                "Completed run is missing completion_receipt",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "payload": run.payload,
                },
            )
        expected_receipt_id = expected_completion_receipt_id(before)
        expected_external_id = expected_completion_external_id(expected, before)
        if completion.get("completion_receipt_id") != expected_receipt_id:
            fail(
                "Completion receipt id does not match synthetic E2E receipt id",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "expected_completion_receipt_id": expected_receipt_id,
                    "completion": completion,
                },
            )
        if completion.get("external_id") != expected_external_id:
            fail(
                "Completion receipt external id does not match dispatch details",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "expected_external_id": expected_external_id,
                    "completion": completion,
                },
            )
        if completion.get("status") != "success":
            fail(
                "Completion receipt status is not success",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "completion": completion,
                },
            )
        if completion.get("adapter") != expected.adapter:
            fail(
                "Completion receipt adapter does not match expected adapter",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "expected_adapter": expected.adapter,
                    "completion": completion,
                },
            )
        if run.payload.get("dispatch_state") != "completed":
            fail(
                "Completion receipt did not mark dispatch_state completed",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "dispatch_state": run.payload.get("dispatch_state"),
                    "payload": run.payload,
                },
            )
        completion_mode = run.payload.get("completion_mode")
        if completion_mode not in {"completion_receipt", "async_materialization"}:
            fail(
                "Completion receipt did not mark completion_mode",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "completion_mode": completion_mode,
                    "payload": run.payload,
                },
            )
        status_history = [
            item
            for item in run.payload.get("status_history") or []
            if isinstance(item, dict)
        ]
        if completion_mode == "async_materialization":
            required_transitions = (
                (
                    "submitted",
                    "completion_pending",
                    "dagster_success_requires_async_materialization",
                ),
                (
                    "completion_pending",
                    "success",
                    "async_completion_materialized",
                ),
            )
        else:
            required_transitions = (
                (
                    "submitted",
                    "success",
                    f"{expected.adapter}_completion_received",
                ),
            )
        missing_transitions = [
            {
                "from": source,
                "to": target,
                "reason": reason,
            }
            for source, target, reason in required_transitions
            if not any(
                item.get("from") == source
                and item.get("to") == target
                and item.get("reason") == reason
                for item in status_history
            )
        ]
        if missing_transitions:
            fail(
                "Completed run status history is missing required completion transitions",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "completion_mode": completion_mode,
                    "missing_transitions": missing_transitions,
                    "status_history": status_history,
                },
            )
        if before.run_type == "external_callback":
            receipt = session.get(
                ExternalCallbackReceipt, str(completion.get("external_id") or "")
            )
            if not receipt or not isinstance(receipt.payload, dict):
                fail(
                    "Completed external callback run is missing persisted callback receipt",
                    {
                        "label": expected.label,
                        "run_id": before.run_id,
                        "completion": completion,
                    },
                )
            if (
                receipt.tenant_id != run.tenant_id
                or receipt.project_id != run.project_id
            ):
                fail(
                    "External callback receipt scope does not match completed run",
                    {
                        "label": expected.label,
                        "run_id": before.run_id,
                        "receipt_tenant_id": receipt.tenant_id,
                        "run_tenant_id": run.tenant_id,
                        "receipt_project_id": receipt.project_id,
                        "run_project_id": run.project_id,
                    },
                )
            if receipt.trace_id != before.trace_id:
                fail(
                    "External callback receipt trace does not match completed run",
                    {
                        "label": expected.label,
                        "run_id": before.run_id,
                        "receipt_trace_id": receipt.trace_id,
                        "run_trace_id": before.trace_id,
                    },
                )
            ack = receipt.payload.get("completion_ack")
            if not isinstance(ack, dict) or ack.get("run_id") != before.run_id:
                fail(
                    "External callback completion ack was not persisted",
                    {
                        "label": expected.label,
                        "run_id": before.run_id,
                        "callback_receipt_id": receipt.callback_receipt_id,
                        "payload": receipt.payload,
                    },
                )
            expected_ack = {
                "completion_receipt_id": completion.get("completion_receipt_id"),
                "status": "success",
                "result_ref": completion.get("result_ref"),
                "metrics": completion.get("metrics"),
            }
            for key, expected_value in expected_ack.items():
                if ack.get(key) != expected_value:
                    fail(
                        "External callback completion ack does not match run completion receipt",
                        {
                            "label": expected.label,
                            "run_id": before.run_id,
                            "field": key,
                            "expected": expected_value,
                            "actual": ack.get(key),
                            "ack": ack,
                            "completion": completion,
                        },
                    )
            if not ack.get("received_at"):
                fail(
                    "External callback completion ack is missing received_at",
                    {
                        "label": expected.label,
                        "run_id": before.run_id,
                        "ack": ack,
                    },
                )
        if before.run_type == "export":
            verify_export_ready(
                before.run_id, dispatch_details(before), before.trace_id
            )
        verify_trace_completion(run)
        return {
            "label": expected.label,
            "run_id": run.run_id,
            "run_type": run.run_type,
            "adapter": expected.adapter,
            "status": run.status,
            "business_status": run.payload.get("business_status"),
            "completion_mode": completion_mode,
            "completion_receipt_id": completion.get("completion_receipt_id"),
            "external_id": completion.get("external_id"),
        }


def validate_pending_materialization_result(
    expected: ExpectedRun,
    before: RunRecord,
    response: dict[str, Any],
) -> None:
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        fail(
            "Asynchronous completion receipt response is missing data",
            {"label": expected.label, "run_id": before.run_id, "response": response},
        )
    expected_receipt_id = expected_completion_receipt_id(before)
    mismatches: list[str] = []
    if data.get("run_id") != before.run_id:
        mismatches.append("run_id")
    if data.get("status") != "completion_pending":
        mismatches.append("status")
    if data.get("business_status") != "materializing":
        mismatches.append("business_status")
    if data.get("receipt_state") != "materializing":
        mismatches.append("receipt_state")
    if data.get("completion_receipt_id") != expected_receipt_id:
        mismatches.append("completion_receipt_id")
    if mismatches:
        fail(
            "Completion receipt did not expose the asynchronous materialization boundary",
            {
                "label": expected.label,
                "run_id": before.run_id,
                "mismatches": mismatches,
                "expected_completion_receipt_id": expected_receipt_id,
                "data": data,
            },
        )

    with SessionLocal() as session:
        run = session.get(RunRecord, before.run_id)
        event = (
            session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.aggregate_type == before.run_type,
                    OutboxEvent.aggregate_id == before.run_id,
                    OutboxEvent.event_type == "execution.materialization.requested",
                    OutboxEvent.tenant_id == before.tenant_id,
                    OutboxEvent.project_id == before.project_id,
                )
                .order_by(OutboxEvent.event_id.desc())
                .limit(1)
            )
            if run
            else None
        )
        if (
            run is None
            or run.status != "completion_pending"
            or run.payload.get("business_status") != "materializing"
            or run.payload.get("business_completion_required") is not True
            or run.payload.get("completion_mode") != "async_materialization"
            or event is None
        ):
            fail(
                "Asynchronous completion boundary was not durably persisted",
                {
                    "label": expected.label,
                    "run_id": before.run_id,
                    "run_status": run.status if run else None,
                    "payload": run.payload if run else None,
                    "materialization_event_id": event.event_id if event else None,
                },
            )


def complete_submitted_run(
    expected: ExpectedRun, run: RunRecord
) -> dict[str, Any] | None:
    if not VERIFY_COMPLETION_RECEIPTS:
        return None
    if expected.adapter not in COMPLETION_ADAPTERS:
        return None
    if run.status not in {"submitted", "success"}:
        return None
    if run.status == "success":
        completion = run.payload.get("completion_receipt")
        if not isinstance(completion, dict) or (
            completion.get("completion_receipt_id")
            != expected_completion_receipt_id(run)
        ):
            return None
        # Some browser flows must finish an upstream async run before the next
        # governed UI action becomes enabled. Validate that persisted receipt
        # directly; replay coverage is still exercised by later submitted runs.
        return validate_completion_result(
            expected,
            run,
            {"data": {"run_id": run.run_id, "status": "success"}},
        )
    if run.status == "submitted" and run.run_type == "export":
        verify_export_reserved(run)
    payload = completion_payload(expected, run)
    idempotency_key = f"e2e-completion-{run.run_id}"
    path = completion_endpoint(run)
    signature_headers = signed_completion_headers(
        method="POST",
        path=path,
        payload=payload,
        idempotency_key=idempotency_key,
        source=str(expected.adapter),
    )
    response = bff_json_request(
        "POST",
        path,
        payload=payload,
        idempotency_key=idempotency_key,
        trace_id=run.trace_id,
        include_default_auth=False,
        extra_headers=signature_headers,
    )
    response_data = response.get("data") if isinstance(response, dict) else None
    is_async_materialization = bool(
        isinstance(response_data, dict)
        and response_data.get("status") == "completion_pending"
        and response_data.get("receipt_state") == "materializing"
    )
    if is_async_materialization:
        validate_pending_materialization_result(expected, run, response)
        wait_for_aggregate_outbox_event(
            aggregate_type=run.run_type,
            aggregate_id=run.run_id,
            event_type="execution.materialization.requested",
        )
        completed_readback = bff_json_request(
            "GET",
            f"/api/v1/runs/{run.run_id}",
            trace_id=run.trace_id,
        )
        result = validate_completion_result(expected, run, completed_readback)
    else:
        result = validate_completion_result(expected, run, response)
    if VERIFY_COMPLETION_REPLAY:
        replay = bff_json_request(
            "POST",
            path,
            payload=payload,
            idempotency_key=idempotency_key,
            trace_id=run.trace_id,
            include_default_auth=False,
            extra_headers=signature_headers,
        )
        replay_data = replay.get("data") if isinstance(replay, dict) else None
        if not isinstance(replay_data, dict) or replay_data.get("run_id") != run.run_id:
            fail(
                "Completion receipt replay did not return the completed run",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "replay": replay,
                },
            )
        expected_replay_status = (
            "completion_pending" if is_async_materialization else "success"
        )
        if replay_data.get("status") != expected_replay_status:
            fail(
                "Completion receipt replay did not preserve its original receipt response",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "expected_status": expected_replay_status,
                    "replay_data": replay_data,
                },
            )
        if is_async_materialization:
            if replay_data.get("receipt_state") != "materializing" or replay_data.get(
                "completion_receipt_id"
            ) != expected_completion_receipt_id(run):
                fail(
                    "Asynchronous completion replay did not preserve the staged receipt",
                    {
                        "label": expected.label,
                        "run_id": run.run_id,
                        "replay_data": replay_data,
                    },
                )
            completed_readback = bff_json_request(
                "GET",
                f"/api/v1/runs/{run.run_id}",
                trace_id=run.trace_id,
            )
            completed_data = (
                completed_readback.get("data")
                if isinstance(completed_readback, dict)
                else None
            )
            if (
                not isinstance(completed_data, dict)
                or completed_data.get("status") != "success"
                or completed_data.get("business_status") != "completed"
            ):
                fail(
                    "Asynchronous completion replay regressed the materialized run",
                    {
                        "label": expected.label,
                        "run_id": run.run_id,
                        "completed_readback": completed_readback,
                    },
                )
    return result


def assert_completion_coverage(
    expected_runs: list[ExpectedRun], completed: list[dict[str, Any]]
) -> None:
    if not REQUIRE_COMPLETION_RECEIPTS:
        return
    if not VERIFY_COMPLETION_RECEIPTS:
        fail(
            "Completion receipt verification is disabled but required",
            {"required_adapters": sorted(REQUIRED_COMPLETION_ADAPTERS)},
        )
    completed_adapters = {
        str(item.get("adapter"))
        for item in completed
        if isinstance(item.get("adapter"), str)
    }
    missing_adapters = REQUIRED_COMPLETION_ADAPTERS - completed_adapters
    if missing_adapters:
        fail(
            "E2E completion receipt coverage is missing required adapters",
            {
                "missing_adapters": sorted(missing_adapters),
                "completed_adapters": sorted(completed_adapters),
                "completed": completed,
            },
        )

    residual = []
    with SessionLocal() as session:
        for expected in expected_runs:
            if expected.adapter not in COMPLETION_ADAPTERS:
                continue
            run = session.get(RunRecord, expected.run_id)
            if not run:
                continue
            if (
                run.status == "submitted"
                and run.payload.get("business_completion_required") is True
            ):
                residual.append(
                    {
                        "label": expected.label,
                        "run_id": run.run_id,
                        "run_type": run.run_type,
                        "adapter": expected.adapter,
                        "business_status": run.payload.get("business_status"),
                    }
                )
    if residual:
        fail(
            "E2E left completion-capable runs awaiting completion",
            {"residual_submitted_runs": residual},
        )


def assert_dispatch_coverage(checked: list[dict[str, Any]]) -> None:
    dispatches = [item for item in checked if item.get("adapter")]
    adapters = {
        str(item.get("adapter"))
        for item in dispatches
        if isinstance(item.get("adapter"), str)
    }
    missing_adapters = REQUIRED_DISPATCH_ADAPTERS - adapters
    if missing_adapters:
        fail(
            "E2E dispatch coverage is missing required adapters",
            {
                "missing_adapters": sorted(missing_adapters),
                "required_adapters": sorted(REQUIRED_DISPATCH_ADAPTERS),
                "observed_adapters": sorted(adapters),
                "dispatches": dispatches,
            },
        )

    run_types = {
        str(item.get("run_type"))
        for item in dispatches
        if isinstance(item.get("run_type"), str)
    }
    missing_run_types = REQUIRED_DISPATCH_RUN_TYPES - run_types
    if missing_run_types:
        fail(
            "E2E dispatch coverage is missing required run types",
            {
                "missing_run_types": sorted(missing_run_types),
                "required_run_types": sorted(REQUIRED_DISPATCH_RUN_TYPES),
                "observed_run_types": sorted(run_types),
                "dispatches": dispatches,
            },
        )


def verify_qdrant_recall_coverage(
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]],
) -> list[dict[str, Any]]:
    if not REQUIRE_QDRANT_RECALL:
        return []
    checked: list[dict[str, Any]] = []
    for expected, run, _event in run_event_pairs:
        if expected.adapter != "qdrant" or run.run_type != "knowledge_build":
            continue
        details = dispatch_details(run)
        qdrant_payload = details.get("qdrant_payload")
        if not isinstance(qdrant_payload, dict):
            fail(
                "Qdrant build dispatch is missing qdrant_payload before recall",
                {"label": expected.label, "run_id": run.run_id, "details": details},
            )
        index_id = qdrant_payload.get("knowledge_index_id")
        if not isinstance(index_id, str) or not index_id:
            fail(
                "Qdrant build dispatch is missing knowledge_index_id before recall",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "payload": qdrant_payload,
                },
            )
        response = bff_json_request(
            "POST",
            f"/api/v1/knowledge-indexes/{index_id}/recall",
            payload={"query": "报价金额冲突处理 SOP", "top_k": 3},
            trace_id=run.trace_id,
        )
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            fail(
                "Knowledge recall response is missing data",
                {"label": expected.label, "run_id": run.run_id, "response": response},
            )
        hits = data.get("hits")
        if (
            data.get("knowledge_index_id") != index_id
            or data.get("collection") != qdrant_payload.get("collection")
            or not isinstance(hits, list)
            or not hits
        ):
            fail(
                "Knowledge recall did not return hits for the built Qdrant index",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "expected_index_id": index_id,
                    "expected_collection": qdrant_payload.get("collection"),
                    "data": data,
                },
            )
        matching_hit = next(
            (
                hit
                for hit in hits
                if isinstance(hit, dict)
                and hit.get("knowledge_index_id") == index_id
                and hit.get("knowledge_source_id")
                == qdrant_payload.get("knowledge_source_id")
                and hit.get("source_id") == qdrant_payload.get("source_id")
                and hit.get("source_type") == qdrant_payload.get("source_type")
                and hit.get("asset_key") == qdrant_payload.get("asset_key")
                and hit.get("version") == qdrant_payload.get("version")
            ),
            None,
        )
        if not isinstance(matching_hit, dict):
            fail(
                "Knowledge recall hits do not contain the built Qdrant payload",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "expected_payload": qdrant_payload,
                    "hits": hits,
                },
            )
        forbidden_fields = {"vector", "qdrant_url", "raw_qdrant_response"}
        leaked_fields = sorted(forbidden_fields & set(matching_hit.keys()))
        if leaked_fields:
            fail(
                "Knowledge recall exposed raw vector-store fields",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "leaked_fields": leaked_fields,
                    "hit": matching_hit,
                },
            )
        if not isinstance(matching_hit.get("business_ref"), dict) or not isinstance(
            matching_hit.get("evidence_ref"), dict
        ):
            fail(
                "Knowledge recall hit is missing business or evidence references",
                {"label": expected.label, "run_id": run.run_id, "hit": matching_hit},
            )
        checked.append(
            {
                "label": expected.label,
                "run_id": run.run_id,
                "knowledge_index_id": index_id,
                "mode": data.get("mode"),
                "collection": data.get("collection"),
                "hit_count": data.get("hit_count"),
                "asset_key": matching_hit.get("asset_key"),
                "trace_id": data.get("recall_trace_id"),
            }
        )
    if not checked:
        fail(
            "E2E did not verify Qdrant-backed knowledge recall",
            {"required_run_type": "knowledge_build"},
        )
    return checked


def verify_asset_backfill_materializations(
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]],
) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    for expected, run, _event in run_event_pairs:
        if run.run_type not in {"asset_backfill", "asset_check_retry"}:
            continue
        with SessionLocal() as session:
            refreshed = session.get(RunRecord, run.run_id)
            if not refreshed:
                fail(
                    "Asset backfill run disappeared before materialization check",
                    {"label": expected.label, "run_id": run.run_id},
                )
            if refreshed.status != "success":
                fail(
                    "Asset backfill run did not complete before materialization check",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "status": refreshed.status,
                        "payload": refreshed.payload,
                    },
                )
            materialized_assets = refreshed.payload.get("materialized_assets")
            if not isinstance(materialized_assets, list) or not materialized_assets:
                fail(
                    "Asset backfill completion did not record materialized assets",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "payload": refreshed.payload,
                    },
                )
            materialized = materialized_assets[0]
            if not isinstance(materialized, dict):
                fail(
                    "Asset backfill materialized asset record is malformed",
                    {"label": expected.label, "run_id": refreshed.run_id},
                )
            asset_key = materialized.get("asset_key")
            materialization_id = materialized.get("materialization_id")
            partition_key = materialized.get("partition_key")
            if not all(
                isinstance(value, str) and value
                for value in (asset_key, materialization_id, partition_key)
            ):
                fail(
                    "Asset backfill materialized asset is missing key identifiers",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "materialized_asset": materialized,
                    },
                )
            record = session.get(AssetMaterialization, materialization_id)
            if not record:
                fail(
                    "Asset materialization strong row was not found",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "materialization_id": materialization_id,
                    },
                )
            if (
                record.tenant_id != DEFAULT_E2E_HEADERS["X-Tenant-Id"]
                or record.project_id != DEFAULT_E2E_HEADERS["X-Project-Id"]
                or record.trace_id != refreshed.trace_id
                or record.payload.get("asset_key") != asset_key
                or record.payload.get("run_id") != refreshed.run_id
            ):
                fail(
                    "Asset materialization strong row does not match completed run",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "trace_id": refreshed.trace_id,
                        "record": {
                            "tenant_id": record.tenant_id,
                            "project_id": record.project_id,
                            "trace_id": record.trace_id,
                            "payload": record.payload,
                        },
                    },
                )
            partitions = list(
                session.scalars(
                    select(AssetPartition).where(
                        AssetPartition.tenant_id == DEFAULT_E2E_HEADERS["X-Tenant-Id"],
                        AssetPartition.project_id
                        == DEFAULT_E2E_HEADERS["X-Project-Id"],
                    )
                )
            )
            partition = next(
                (
                    item
                    for item in partitions
                    if item.payload.get("asset_key") == asset_key
                    and item.payload.get("partition_key") == partition_key
                ),
                None,
            )
            if not partition:
                fail(
                    "Asset partition strong row was not found for asset partition",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "asset_key": asset_key,
                        "partition_key": partition_key,
                        "materialization_id": materialization_id,
                        "observed_partitions": [
                            {
                                "asset_partition_id": item.asset_partition_id,
                                "status": item.status,
                                "payload": item.payload,
                            }
                            for item in partitions
                        ],
                    },
                )
            current_partition_materialization_id = partition.payload.get(
                "materialization_id"
            )
            if not isinstance(current_partition_materialization_id, str):
                fail(
                    "Asset partition does not point to a materialization",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "partition": partition.payload,
                    },
                )
            if current_partition_materialization_id != materialization_id:
                current_materialization = session.get(
                    AssetMaterialization, current_partition_materialization_id
                )
                if not current_materialization or (
                    current_materialization.payload.get("asset_key") != asset_key
                    or current_materialization.payload.get("partition_key")
                    != partition_key
                ):
                    fail(
                        "Asset partition points to an invalid latest materialization",
                        {
                            "label": expected.label,
                            "run_id": refreshed.run_id,
                            "expected_asset_key": asset_key,
                            "expected_partition_key": partition_key,
                            "partition": partition.payload,
                        },
                    )
            projection = session.scalar(
                select(JsonResource).where(
                    JsonResource.collection == "data_assets",
                    JsonResource.resource_key == asset_key,
                    JsonResource.tenant_id == DEFAULT_E2E_HEADERS["X-Tenant-Id"],
                    JsonResource.project_id == DEFAULT_E2E_HEADERS["X-Project-Id"],
                )
            )
            if not projection:
                fail(
                    "Data asset projection was not found after materialization",
                    {"label": expected.label, "asset_key": asset_key},
                )
            latest_projection_materialization_id = projection.data.get(
                "latest_materialization_id"
            )
            latest_projection_run_id = projection.data.get("latest_run_id")
            latest_projection_partition_key = projection.data.get(
                "latest_partition_key"
            )
            latest_projection_materialization = (
                session.get(
                    AssetMaterialization,
                    latest_projection_materialization_id,
                )
                if isinstance(latest_projection_materialization_id, str)
                else None
            )
            if (
                latest_projection_materialization is None
                or latest_projection_materialization.tenant_id
                != DEFAULT_E2E_HEADERS["X-Tenant-Id"]
                or latest_projection_materialization.project_id
                != DEFAULT_E2E_HEADERS["X-Project-Id"]
                or latest_projection_materialization.payload.get("asset_key")
                != asset_key
                or latest_projection_materialization.payload.get("run_id")
                != latest_projection_run_id
                or latest_projection_materialization.payload.get("partition_key")
                != latest_projection_partition_key
            ):
                fail(
                    "Data asset projection does not point at a valid latest materialization",
                    {
                        "label": expected.label,
                        "asset_key": asset_key,
                        "completed_materialization_id": materialization_id,
                        "current_partition_materialization_id": current_partition_materialization_id,
                        "latest_projection_materialization_id": latest_projection_materialization_id,
                        "latest_projection_run_id": latest_projection_run_id,
                        "latest_projection_partition_key": latest_projection_partition_key,
                        "latest_projection_materialization": (
                            latest_projection_materialization.payload
                            if latest_projection_materialization is not None
                            else None
                        ),
                        "projection": projection.data,
                    },
                )
            lineage_edges = list(
                session.scalars(
                    select(AssetLineageEdge).where(
                        AssetLineageEdge.tenant_id
                        == DEFAULT_E2E_HEADERS["X-Tenant-Id"],
                        AssetLineageEdge.project_id
                        == DEFAULT_E2E_HEADERS["X-Project-Id"],
                        AssetLineageEdge.payload["materialization_id"].as_string()
                        == materialization_id,
                    )
                )
            )
            if not lineage_edges:
                fail(
                    "Asset materialization did not create dynamic lineage edges",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "asset_key": asset_key,
                        "materialization_id": materialization_id,
                    },
                )
            if not any(
                edge.payload.get("source_asset_key") == asset_key
                or edge.payload.get("target_asset_key") == asset_key
                for edge in lineage_edges
            ):
                fail(
                    "Dynamic lineage edges do not reference materialized asset",
                    {
                        "label": expected.label,
                        "asset_key": asset_key,
                        "edges": [edge.payload for edge in lineage_edges],
                    },
                )

        encoded_asset_key = quote(str(asset_key), safe="")
        materializations = bff_json_request(
            "GET",
            f"/api/v1/data-assets/{encoded_asset_key}/materializations",
            trace_id=run.trace_id,
        )
        materialization_items = materializations.get("data", {}).get("items")
        if not isinstance(materialization_items, list) or not any(
            isinstance(item, dict)
            and item.get("materialization_id") == materialization_id
            for item in materialization_items
        ):
            fail(
                "Data asset materializations endpoint does not expose completed run",
                {
                    "label": expected.label,
                    "asset_key": asset_key,
                    "materialization_id": materialization_id,
                    "response": materializations,
                },
            )
        partitions = bff_json_request(
            "GET",
            f"/api/v1/data-assets/{encoded_asset_key}/partitions",
            trace_id=run.trace_id,
        )
        partition_items = partitions.get("data", {}).get("items")
        if not isinstance(partition_items, list) or not any(
            isinstance(item, dict)
            and item.get("partition_key") == partition_key
            and item.get("materialization_id") == current_partition_materialization_id
            for item in partition_items
        ):
            fail(
                "Data asset partitions endpoint does not expose current partition state",
                {
                    "label": expected.label,
                    "asset_key": asset_key,
                    "materialization_id": materialization_id,
                    "current_partition_materialization_id": current_partition_materialization_id,
                    "response": partitions,
                },
            )
        lineage = bff_json_request(
            "GET",
            f"/api/v1/data-assets/{encoded_asset_key}/lineage",
            trace_id=run.trace_id,
        )
        lineage_data = lineage.get("data", {})
        lineage_edges_payload = lineage_data.get("edges")
        lineage_nodes = lineage_data.get("nodes")
        if not isinstance(lineage_edges_payload, list) or not any(
            isinstance(edge, dict)
            and edge.get("materialization_id") == materialization_id
            and (edge.get("from") == asset_key or edge.get("to") == asset_key)
            for edge in lineage_edges_payload
        ):
            fail(
                "Data asset lineage endpoint does not expose dynamic materialization edges",
                {
                    "label": expected.label,
                    "asset_key": asset_key,
                    "materialization_id": materialization_id,
                    "response": lineage,
                },
            )
        if not isinstance(lineage_nodes, list) or not any(
            isinstance(node, dict)
            and node.get("node_type") == "materialization"
            and node.get("asset_key") == materialization_id
            for node in lineage_nodes
        ):
            fail(
                "Data asset lineage endpoint does not expose materialization node",
                {
                    "label": expected.label,
                    "asset_key": asset_key,
                    "materialization_id": materialization_id,
                    "response": lineage,
                },
            )
        trace = bff_json_request(
            "GET", f"/api/v1/traces/{run.trace_id}", trace_id=run.trace_id
        )
        spans = trace.get("data", {}).get("spans")
        if not isinstance(spans, list) or not any(
            isinstance(span, dict)
            and span.get("kind") == "materialization"
            and span.get("materialization_id") == materialization_id
            for span in spans
        ):
            fail(
                "Trace does not contain asset materialization span",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "trace_id": run.trace_id,
                    "materialization_id": materialization_id,
                    "trace": trace,
                },
            )
        if not isinstance(spans, list) or not any(
            isinstance(span, dict)
            and span.get("kind") == "asset_lineage_edge"
            and span.get("materialization_id") == materialization_id
            for span in spans
        ):
            fail(
                "Trace does not contain asset lineage edge span",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "trace_id": run.trace_id,
                    "materialization_id": materialization_id,
                    "trace": trace,
                },
            )
        checked.append(
            {
                "label": expected.label,
                "run_id": run.run_id,
                "asset_key": asset_key,
                "partition_key": partition_key,
                "materialization_id": materialization_id,
                "current_partition_materialization_id": current_partition_materialization_id,
                "trace_id": run.trace_id,
            }
        )
    if not checked:
        fail(
            "E2E did not verify asset materialization",
            {"required_run_types": ["asset_backfill", "asset_check_retry"]},
        )
    return checked


def verify_audio_intelligence_materialization(
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]],
) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    required_collections = {
        "vad_segments",
        "speaker_turns",
        "asr_segments",
        "voiceprint_samples",
        "audio_quality_reports",
    }
    for expected, run, _event in run_event_pairs:
        if run.run_type != "audio_intelligence":
            continue
        with SessionLocal() as session:
            refreshed = session.get(RunRecord, run.run_id)
            if not refreshed:
                fail(
                    "Audio intelligence run disappeared before materialization check",
                    {"label": expected.label, "run_id": run.run_id},
                )
            if refreshed.status != "success":
                fail(
                    "Audio intelligence run did not complete before materialization check",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "status": refreshed.status,
                        "payload": refreshed.payload,
                    },
                )
            materialized = refreshed.payload.get("materialized_outputs")
            if not isinstance(materialized, list):
                fail(
                    "Audio intelligence completion did not record materialized outputs",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "payload": refreshed.payload,
                    },
                )
            materialized_collections = {
                item.get("collection")
                for item in materialized
                if isinstance(item, dict)
            }
            missing_collections = required_collections - materialized_collections
            if missing_collections:
                fail(
                    "Audio intelligence materialized outputs are incomplete",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "missing_collections": sorted(missing_collections),
                        "materialized_outputs": materialized,
                    },
                )
            audio_session_id = str(refreshed.payload.get("audio_session_id") or "")
        response = bff_json_request(
            "GET",
            f"/api/v1/audio-sessions/{audio_session_id}",
            trace_id=run.trace_id,
        )
        data = response.get("data") if isinstance(response, dict) else None
        if not isinstance(data, dict):
            fail(
                "Audio session detail response is missing data after audio intelligence completion",
                {"label": expected.label, "run_id": run.run_id, "response": response},
            )
        missing_fields = [
            field
            for field in required_collections
            if not any(
                isinstance(item, dict) and item.get("source_run_id") == run.run_id
                for item in data.get(field, [])
            )
        ]
        if missing_fields:
            fail(
                "Audio session detail is missing materialized audio intelligence tracks",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "audio_session_id": audio_session_id,
                    "missing_fields": missing_fields,
                    "data": data,
                },
            )
        checked.append(
            {
                "label": expected.label,
                "run_id": run.run_id,
                "audio_session_id": audio_session_id,
                "collections": sorted(required_collections),
                "trace_id": run.trace_id,
            }
        )
    if not checked:
        fail(
            "E2E did not verify audio intelligence materialization",
            {"required_run_type": "audio_intelligence"},
        )
    return checked


def verify_audio_recording_range_stream() -> dict[str, Any]:
    audio_session_id = "S20250526-000128"
    recording_id = "rec_A_1001_20250526_122300"
    storage_object_id = f"sto_{recording_id}"
    object_key = (
        "tenants/aurora_auto/projects/sales_qa/audio/raw/2025-05-26/"
        "A-1001_20250526_122300.wav"
    )
    processed_registration_events = 0
    real_storage = os.environ.get("AURIS_OBJECT_STORAGE_ADAPTER", "").lower() == "real"
    storage_client: RealObjectStorageClient | None = None
    bucket = ""
    body = b""
    original_etag = ""
    if real_storage:
        storage_client = RealObjectStorageClient()
        bucket = os.environ.get("OBJECT_STORAGE_BUCKET", "auris-flow-local")
        sample_rate = 8000
        pcm = b"".join(
            struct.pack("<h", 1200 if (index // 80) % 2 == 0 else -1200)
            for index in range(sample_rate)
        )
        body = b"".join(
            [
                b"RIFF",
                struct.pack("<I", 36 + len(pcm)),
                b"WAVEfmt ",
                struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
                b"data",
                struct.pack("<I", len(pcm)),
                pcm,
            ]
        )
        with SessionLocal() as session:
            pre_registered = session.get(StorageObject, storage_object_id) is not None
        if not pre_registered:
            try:
                storage_client._ensure_bucket()
                put_result = storage_client._request(
                    "PUT",
                    f"/{bucket}/{object_key}",
                    body=body,
                    content_type="audio/wav",
                )
                uploaded_etag = str(put_result.get("etag") or "")
            except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
                fail(
                    "Could not seed real object storage audio fixture for Range verification",
                    {"bucket": bucket, "object_key": object_key, "error": str(exc)},
                )
            registration = bff_json_request(
                "PUT",
                f"/api/v1/audio-sessions/{audio_session_id}/recording-object",
                payload={
                    "storage_object_id": storage_object_id,
                    "provider": "minio",
                    "bucket": bucket,
                    "object_key": object_key,
                    "content_type": "audio/wav",
                    "content_length": len(body),
                    "checksum_sha256": hashlib.sha256(body).hexdigest(),
                    "etag": put_result.get("etag"),
                },
                idempotency_key="e2e-register-audio-range-object",
                trace_id="trace_e2e_audio_range_object",
            )
            registered = (
                registration.get("data") if isinstance(registration, dict) else None
            )
            registered_storage = (
                registered.get("storage_object")
                if isinstance(registered, dict)
                else None
            )
            if (
                not isinstance(registered_storage, dict)
                or registered_storage.get("storage_object_id") != storage_object_id
                or registered_storage.get("etag") != uploaded_etag
            ):
                fail(
                    "BFF did not register the real audio storage object",
                    {"registration": registration},
                )

        registration_event = wait_for_aggregate_outbox_event(
            aggregate_type="storage_object",
            aggregate_id=storage_object_id,
            event_type="audio_recording.object_registered",
        )
        processed_registration_events = registration_event.attempt_count
        if processed_registration_events != 1:
            fail(
                "Audio storage registration outbox event was not processed exactly once",
                {
                    "storage_object_id": storage_object_id,
                    "event_id": registration_event.event_id,
                    "attempt_count": registration_event.attempt_count,
                    "reconcile_attempt_count": registration_event.reconcile_attempt_count,
                },
            )
        with SessionLocal() as session:
            storage_record = session.get(StorageObject, storage_object_id)
            registered_version_id = (
                storage_record.payload.get("object_version_id")
                if storage_record is not None
                and isinstance(storage_record.payload, dict)
                else None
            )
            if (
                not storage_record
                or storage_record.tenant_id != "aurora_auto"
                or storage_record.project_id != "sales_qa"
                or storage_record.provider != "minio"
                or storage_record.bucket != bucket
                or storage_record.object_key != object_key
                or storage_record.object_key_sha256
                != hashlib.sha256(object_key.encode()).hexdigest()
                or storage_record.source_type != "audio_recording"
                or storage_record.source_id != recording_id
                or storage_record.content_type != "audio/wav"
                or storage_record.content_sha256 != hashlib.sha256(body).hexdigest()
                or storage_record.size_bytes != len(body)
                or storage_record.status != "verified"
                or not storage_record.etag
                or not isinstance(registered_version_id, str)
                or not registered_version_id
            ):
                fail(
                    "MySQL storage object metadata does not match the uploaded WAV",
                    {
                        "storage_object_id": storage_object_id,
                        "registered": bool(storage_record),
                    },
                )
            original_etag = storage_record.etag
        try:
            registered_remote = storage_client.head_object(
                bucket,
                object_key,
                version_id=registered_version_id,
            )
        except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
            fail(
                "Could not resolve the registered exact audio object version",
                {
                    "storage_object_id": storage_object_id,
                    "object_version_id": registered_version_id,
                    "error": str(exc),
                },
            )
        if (
            str(registered_remote.get("etag") or "").strip('"') != original_etag
            or str(registered_remote.get("content_length") or "") != str(len(body))
            or registered_remote.get("version_id") != registered_version_id
        ):
            fail(
                "Registered exact audio object version does not match MySQL authority",
                {
                    "storage_object_id": storage_object_id,
                    "object_version_id": registered_version_id,
                    "remote": registered_remote,
                },
            )

    playback_grant = bff_json_request(
        "POST",
        f"/api/v1/audio-sessions/{audio_session_id}/playback-grants",
        idempotency_key=f"e2e-audio-range-playback-grant-{audio_session_id}",
    )
    playback_url = playback_grant.get("data", {}).get("playback_url")
    if not isinstance(playback_url, str) or "grant=" not in playback_url:
        fail("Audio playback grant response is missing playback_url", playback_grant)

    whole = bff_binary_request("GET", playback_url, include_default_context=False)
    if whole["status"] != 200 or whole["headers"].get("accept-ranges") != "bytes":
        fail("Audio recording stream does not advertise byte ranges", whole)
    whole_body = whole.get("body")
    if not isinstance(whole_body, bytes) or not whole_body.startswith(b"RIFF"):
        fail("Audio recording stream did not return WAV bytes", whole)

    partial = bff_binary_request(
        "GET",
        playback_url,
        extra_headers={"Range": "bytes=0-15"},
        include_default_context=False,
    )
    if partial["status"] != 206:
        fail("Audio recording Range request did not return 206", partial)
    headers = partial.get("headers", {})
    if headers.get("accept-ranges") != "bytes" or not str(
        headers.get("content-range") or ""
    ).startswith("bytes 0-15/"):
        fail("Audio recording Range response is missing byte-range headers", partial)
    if partial.get("body") != whole_body[:16]:
        fail(
            "Audio recording Range response body does not match requested slice",
            partial,
        )
    if real_storage:
        if headers.get("x-storage-object-id") != storage_object_id:
            fail(
                "Audio Range response is not linked to MySQL storage metadata", partial
            )

    invalid = bff_binary_request(
        "GET",
        playback_url,
        extra_headers={"Range": "bytes=999999-1000000"},
        include_default_context=False,
    )
    if invalid["status"] != 416 or not str(
        invalid.get("headers", {}).get("content-range") or ""
    ).startswith("bytes */"):
        fail("Audio recording invalid Range did not return 416", invalid)
    replacement_current_version_changed = False
    registered_version_continuity_status = 0
    registered_version_body_match = False
    if real_storage and storage_client is not None:
        replacement = bytearray(body)
        replacement[-1] ^= 0x01
        registered_version_response: dict[str, Any] | None = None
        replacement_etag = ""
        replacement_error: str | None = None
        restore_error: str | None = None
        restored_etag = ""
        try:
            replacement_result = storage_client._request(
                "PUT",
                f"/{bucket}/{object_key}",
                body=bytes(replacement),
                content_type="audio/wav",
            )
            replacement_etag = str(replacement_result.get("etag") or "")
            registered_version_response = bff_binary_request(
                "GET",
                playback_url,
                include_default_context=False,
            )
        except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
            replacement_error = str(exc)
        finally:
            try:
                restore_result = storage_client._request(
                    "PUT",
                    f"/{bucket}/{object_key}",
                    body=body,
                    content_type="audio/wav",
                )
                restored_etag = str(restore_result.get("etag") or "")
            except (OSError, URLError, HTTPError, TimeoutError, ValueError) as exc:
                restore_error = str(exc)

        if replacement_error or restore_error:
            fail(
                "Could not verify or restore same-size audio object replacement",
                {
                    "replacement_error": replacement_error,
                    "restore_error": restore_error,
                    "storage_object_id": storage_object_id,
                },
            )
        if not replacement_etag or replacement_etag == original_etag:
            fail(
                "Same-size replacement did not create a distinct provider version",
                {
                    "original_etag": original_etag,
                    "replacement_etag": replacement_etag,
                },
            )
        replacement_current_version_changed = True
        if restored_etag != original_etag:
            fail(
                "Real audio fixture latest content was not restored after replacement",
                {
                    "expected_etag": original_etag,
                    "restored_etag": restored_etag,
                },
            )
        registered_version_continuity_status = int(
            registered_version_response.get("status", 0)
            if registered_version_response
            else 0
        )
        registered_version_body_match = bool(
            registered_version_response
            and registered_version_response.get("body") == body
            and registered_version_response.get("headers", {}).get("accept-ranges")
            == "bytes"
            and registered_version_response.get("headers", {}).get(
                "x-storage-object-id"
            )
            == storage_object_id
        )
        if (
            registered_version_continuity_status != 200
            or not registered_version_body_match
        ):
            fail(
                "Exact-version audio playback did not survive a newer current object version",
                {
                    "response": registered_version_response,
                    "original_etag": original_etag,
                    "replacement_etag": replacement_etag,
                },
            )
    return {
        "audio_session_id": audio_session_id,
        "status": "ok",
        "source": whole["headers"].get("x-audio-source"),
        "content_length": len(whole_body),
        "range_status": partial["status"],
        "invalid_range_status": invalid["status"],
        "storage_object_id": storage_object_id,
        "playback_grant_status": playback_grant.get("data", {}).get("status"),
        "metadata_registered": real_storage,
        "registration_event_processed": processed_registration_events,
        "replacement_current_version_changed": replacement_current_version_changed,
        "registered_version_continuity_status": registered_version_continuity_status,
        "registered_version_body_match": registered_version_body_match,
    }


def verify_agentic_execution_trace(
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]],
) -> list[dict[str, Any]]:
    checked: list[dict[str, Any]] = []
    for expected, run, _event in run_event_pairs:
        if run.run_type not in REQUIRED_AGENTIC_RUN_TYPES:
            continue
        with SessionLocal() as session:
            refreshed = session.get(RunRecord, run.run_id)
            if not refreshed:
                fail(
                    "Agentic run disappeared before trace check",
                    {"label": expected.label, "run_id": run.run_id},
                )
            if refreshed.status != "success":
                fail(
                    "Agentic run did not complete before trace check",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "status": refreshed.status,
                        "payload": refreshed.payload,
                    },
                )
            agent_run_id = refreshed.payload.get("agent_run_id") or refreshed.run_id
            if not isinstance(agent_run_id, str) or not agent_run_id:
                fail(
                    "Agentic run payload is missing agent_run_id",
                    {"label": expected.label, "run_id": refreshed.run_id},
                )
            agent = session.get(AgentRun, agent_run_id)
            if not agent:
                fail(
                    "AgentRun strong row was not found",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "agent_run_id": agent_run_id,
                    },
                )
            if (
                agent.tenant_id != DEFAULT_E2E_HEADERS["X-Tenant-Id"]
                or agent.project_id != DEFAULT_E2E_HEADERS["X-Project-Id"]
                or agent.trace_id != refreshed.trace_id
                or agent.status != "success"
                or agent.payload.get("source_run_id") != refreshed.run_id
            ):
                fail(
                    "AgentRun strong row does not match completed run",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "agent": {
                            "tenant_id": agent.tenant_id,
                            "project_id": agent.project_id,
                            "trace_id": agent.trace_id,
                            "status": agent.status,
                            "payload": agent.payload,
                        },
                    },
                )
            if (
                not isinstance(agent.payload.get("input_refs"), list)
                or not agent.payload["input_refs"]
            ):
                fail(
                    "AgentRun is missing controlled input references",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "agent": agent.payload,
                    },
                )
            write_policy = agent.payload.get("write_policy")
            if not isinstance(write_policy, dict) or "source_asset" not in (
                write_policy.get("forbidden_writes") or []
            ):
                fail(
                    "AgentRun write policy does not prevent online/source overwrites",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "write_policy": write_policy,
                    },
                )
            tools = list(
                session.scalars(
                    select(ToolCall).where(
                        ToolCall.trace_id == refreshed.trace_id,
                        ToolCall.tenant_id == refreshed.tenant_id,
                        ToolCall.project_id == refreshed.project_id,
                        ToolCall.payload["agent_run_id"].as_string() == agent_run_id,
                    )
                )
            )
            tool_keys = {
                item.payload.get("key")
                for item in tools
                if isinstance(item.payload, dict)
            }
            if "dispatch_agent_run" not in tool_keys:
                fail(
                    "Agentic run is missing dispatch tool call",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "tool_keys": sorted(str(key) for key in tool_keys if key),
                    },
                )
            if not any(item.status == "success" for item in tools):
                fail(
                    "Agentic tool calls never reached success",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "tools": [item.payload for item in tools],
                    },
                )
            decisions = list(
                session.scalars(
                    select(AgentDecision).where(
                        AgentDecision.trace_id == refreshed.trace_id,
                        AgentDecision.tenant_id == refreshed.tenant_id,
                        AgentDecision.project_id == refreshed.project_id,
                        AgentDecision.payload["agent_run_id"].as_string()
                        == agent_run_id,
                    )
                )
            )
            if not decisions or not any(
                item.status == "success"
                and isinstance(item.payload.get("result_ref"), dict)
                and item.payload["result_ref"]
                for item in decisions
            ):
                fail(
                    "Agentic completion decision with result reference was not persisted",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "decisions": [item.payload for item in decisions],
                    },
                )
            refs = list(
                session.scalars(
                    select(TraceRef).where(
                        TraceRef.trace_id == refreshed.trace_id,
                        TraceRef.tenant_id == refreshed.tenant_id,
                        TraceRef.project_id == refreshed.project_id,
                        TraceRef.payload["agent_run_id"].as_string() == agent_run_id,
                    )
                )
            )
            ref_roles = {
                item.payload.get("ref_role")
                for item in refs
                if isinstance(item.payload, dict)
            }
            if not {"input", "result"} <= ref_roles:
                fail(
                    "Agentic trace refs do not include both input and result references",
                    {
                        "label": expected.label,
                        "run_id": refreshed.run_id,
                        "ref_roles": sorted(str(role) for role in ref_roles if role),
                        "refs": [item.payload for item in refs],
                    },
                )
            prompt_candidates = []
            if refreshed.run_type == "eval_feedback":
                prompt_candidates = list(
                    session.scalars(
                        select(PromptVersionCandidate).where(
                            PromptVersionCandidate.trace_id == refreshed.trace_id,
                            PromptVersionCandidate.tenant_id == refreshed.tenant_id,
                            PromptVersionCandidate.project_id == refreshed.project_id,
                        )
                    )
                )
                if not prompt_candidates:
                    fail(
                        "Eval feedback completion did not materialize a prompt candidate",
                        {"label": expected.label, "run_id": refreshed.run_id},
                    )
                candidate = prompt_candidates[0]
                if (
                    candidate.status != "candidate"
                    or candidate.payload.get("source_run_id") != refreshed.run_id
                    or "production_prompt"
                    not in candidate.payload.get("write_policy", {}).get(
                        "forbidden_writes", []
                    )
                ):
                    fail(
                        "Prompt candidate does not preserve safe draft semantics",
                        {
                            "label": expected.label,
                            "run_id": refreshed.run_id,
                            "candidate": candidate.payload,
                        },
                    )
                detail = bff_json_request(
                    "GET",
                    f"/api/v1/prompt-version-candidates/{candidate.candidate_id}",
                    trace_id=refreshed.trace_id,
                )
                if detail.get("data", {}).get("source_run_id") != refreshed.run_id:
                    fail(
                        "Prompt candidate BFF detail does not match eval feedback run",
                        {
                            "label": expected.label,
                            "run_id": refreshed.run_id,
                            "candidate_detail": detail,
                        },
                    )

        trace = bff_json_request(
            "GET", f"/api/v1/traces/{run.trace_id}", trace_id=run.trace_id
        )
        spans = trace.get("data", {}).get("spans")
        if not isinstance(spans, list):
            fail(
                "Agentic trace response is missing spans",
                {"label": expected.label, "run_id": run.run_id, "trace": trace},
            )
        span_kinds = {span.get("kind") for span in spans if isinstance(span, dict)}
        missing_kinds = {
            "agent_run",
            "tool_call",
            "agent_decision",
            "trace_ref",
        } - span_kinds
        if (
            run.run_type == "eval_feedback"
            and "prompt_version_candidate" not in span_kinds
        ):
            missing_kinds.add("prompt_version_candidate")
        if missing_kinds:
            fail(
                "Agentic trace is missing execution spans",
                {
                    "label": expected.label,
                    "run_id": run.run_id,
                    "missing_kinds": sorted(missing_kinds),
                    "trace": trace,
                },
            )
        checked.append(
            {
                "label": expected.label,
                "run_id": run.run_id,
                "run_type": run.run_type,
                "agent_run_id": agent_run_id,
                "trace_id": run.trace_id,
                "tool_count": len(tools),
                "trace_ref_count": len(refs),
                "prompt_candidate_count": len(prompt_candidates)
                if run.run_type == "eval_feedback"
                else 0,
            }
        )
    missing_types = REQUIRED_AGENTIC_RUN_TYPES - {
        item["run_type"] for item in checked if isinstance(item.get("run_type"), str)
    }
    if missing_types:
        fail(
            "E2E did not verify required agentic execution run types",
            {
                "missing_run_types": sorted(missing_types),
                "required_run_types": sorted(REQUIRED_AGENTIC_RUN_TYPES),
                "checked": checked,
            },
        )
    return checked


def verify_voiceprint_enrollment_gate(result: dict[str, Any]) -> dict[str, Any]:
    gate = result.get("voiceprintEnrollmentGate")
    if not isinstance(gate, dict):
        fail("UI/BFF artifact is missing voiceprintEnrollmentGate")
    expected = {
        "status": "blocked",
        "reasonCode": "VOICEPRINT_CANDIDATE_READ_MODEL_UNAVAILABLE",
        "postCount": 0,
    }
    if any(gate.get(key) != value for key, value in expected.items()):
        fail(
            "Voiceprint enrollment must fail closed until the candidate read model is authoritative",
            {"expected": expected, "actual": gate},
        )
    return gate


def verify_approved_voiceprint_qdrant_index(started_at: datetime) -> dict[str, Any]:
    enrollment_id = (
        "vp_e2e_vector_recall_"
        f"{hashlib.sha1(started_at.isoformat().encode()).hexdigest()[:12]}"
    )
    voiceprint_id = "VP-E2E-VOICEPRINT"
    payload = {
        "enrollment_id": enrollment_id,
        "voiceprint_id": voiceprint_id,
        "employee_ref": "销售A / A-1001",
        "speaker_id": "spk_e2e_voiceprint",
        "audio_session_id": "S20250526-000128",
        "recording_id": "A-1001_20250526_122300",
        "asset_key": "auris/audio/raw_recordings",
        "voice_asset_key": "auris/voiceprint/enrollment_templates",
        "quality": {
            "overall": 91,
            "duration": 94,
            "snr": 88,
            "purity": 90,
            "stability": 92,
        },
        "consistency": {"ab": 0.91, "ac": 0.89, "bc": 0.9},
        "samples": [
            {"sample_id": "A", "window": "12:23:42-12:24:12"},
            {"sample_id": "B", "window": "12:24:48-12:25:18"},
        ],
    }
    response = bff_json_request(
        "POST",
        "/api/v1/voiceprint-enrollments",
        payload=payload,
        idempotency_key=f"e2e-approved-voiceprint-qdrant-{enrollment_id}",
        auth_token="annotator-token",
    )
    data = response.get("data")
    if not isinstance(data, dict):
        fail("Approved voiceprint enrollment response is missing data", response)
    trace_id = response.get("meta", {}).get("trace_id")
    if (
        data.get("id") != enrollment_id
        or data.get("voiceprint_id") != voiceprint_id
        or data.get("status") != "enrolled"
        or not trace_id
    ):
        fail(
            "Approved voiceprint enrollment did not enter enrolled state",
            {"response": response, "expected_enrollment_id": enrollment_id},
        )
    processed_event = wait_for_aggregate_outbox_event(
        aggregate_type="voiceprint_enrollments",
        aggregate_id=enrollment_id,
        event_type="voiceprint_enrollments.upserted",
    )
    processed = processed_event.attempt_count

    with SessionLocal() as session:
        projection = session.get(VoiceprintEnrollment, enrollment_id)
        if not projection:
            fail("Approved voiceprint enrollment projection was not found", response)
        if (
            projection.voiceprint_id != voiceprint_id
            or projection.status != "enrolled"
            or projection.trace_id != trace_id
        ):
            fail(
                "Approved voiceprint enrollment projection does not match BFF response",
                {
                    "response": response,
                    "projection": {
                        "voiceprint_id": projection.voiceprint_id,
                        "status": projection.status,
                        "trace_id": projection.trace_id,
                    },
                },
            )
        event = session.scalar(
            select(OutboxEvent)
            .where(
                OutboxEvent.aggregate_type == "voiceprint_enrollments",
                OutboxEvent.aggregate_id == enrollment_id,
                OutboxEvent.event_type == "voiceprint_enrollments.upserted",
                OutboxEvent.tenant_id == DEFAULT_E2E_HEADERS["X-Tenant-Id"],
                OutboxEvent.project_id == DEFAULT_E2E_HEADERS["X-Project-Id"],
            )
            .order_by(OutboxEvent.event_id.desc())
            .limit(1)
        )
        if not event:
            fail("Approved voiceprint enrollment outbox event was not found", response)
        if event.status != "processed":
            fail(
                "Approved voiceprint enrollment outbox event was not processed",
                {
                    "event_id": event.event_id,
                    "status": event.status,
                    "last_error": event.last_error,
                    "processed_worker_count": processed,
                    "payload": event.payload,
                },
            )
        dispatch = event.payload.get("adapter_dispatch")
        if not isinstance(dispatch, dict) or dispatch.get("adapter") != "qdrant":
            fail(
                "Approved voiceprint enrollment was not dispatched to Qdrant",
                {
                    "event_id": event.event_id,
                    "dispatch": dispatch,
                    "payload": event.payload,
                },
            )
        details = dispatch.get("details")
        if not isinstance(details, dict):
            fail("Approved voiceprint Qdrant dispatch is missing details", dispatch)
        qdrant_payload = details.get("qdrant_payload")
        if not isinstance(qdrant_payload, dict):
            fail(
                "Approved voiceprint Qdrant dispatch is missing qdrant_payload", details
            )
        for key, expected in {
            "collection": "voiceprint_embeddings",
            "voiceprint_id": voiceprint_id,
            "enrollment_id": enrollment_id,
            "source_type": "voiceprint_enrollment",
            "tenant_id": DEFAULT_E2E_HEADERS["X-Tenant-Id"],
            "project_id": DEFAULT_E2E_HEADERS["X-Project-Id"],
            "trace_id": trace_id,
        }.items():
            if qdrant_payload.get(key) != expected:
                fail(
                    "Approved voiceprint Qdrant payload does not match expected field",
                    {
                        "field": key,
                        "expected": expected,
                        "actual": qdrant_payload.get(key),
                        "qdrant_payload": qdrant_payload,
                    },
                )
        if details.get("mode") == "real":
            collection = details.get("collection")
            point_ids = details.get("point_ids")
            if (
                not isinstance(collection, str)
                or not isinstance(point_ids, list)
                or not point_ids
            ):
                fail(
                    "Approved voiceprint real Qdrant receipt is missing point id",
                    details,
                )
            point = read_qdrant_point(collection, str(point_ids[0]))
            actual_payload = point.get("payload")
            if not isinstance(actual_payload, dict):
                fail("Approved voiceprint Qdrant point is missing payload", point)
            for key in (
                "tenant_id",
                "project_id",
                "trace_id",
                "collection",
                "voiceprint_id",
                "enrollment_id",
                "source_type",
            ):
                if actual_payload.get(key) != qdrant_payload.get(key):
                    fail(
                        "Approved voiceprint real Qdrant point payload mismatch",
                        {
                            "field": key,
                            "expected": qdrant_payload.get(key),
                            "actual": actual_payload.get(key),
                            "point": point,
                        },
                    )

    trace = bff_json_request(
        "GET", f"/api/v1/traces/{trace_id}", trace_id=str(trace_id)
    )
    spans = trace.get("data", {}).get("spans")
    if not isinstance(spans, list) or not any(
        isinstance(span, dict)
        and span.get("kind") == "voiceprint_enrollment"
        and span.get("id") == enrollment_id
        and span.get("status") == "enrolled"
        for span in spans
    ):
        fail(
            "Approved voiceprint trace is missing strong enrollment span",
            {"trace": trace, "enrollment_id": enrollment_id},
        )

    return {
        "id": enrollment_id,
        "voiceprint_id": voiceprint_id,
        "trace_id": trace_id,
        "event_processed": True,
        "adapter": "qdrant",
        "processed_worker_count": processed,
    }


def verify_audio_import_closed_loop(
    result: dict[str, Any],
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]],
) -> dict[str, Any]:
    evidence = result.get("audioImportClosedLoop")
    if not isinstance(evidence, dict):
        fail("Audio import closed-loop browser evidence is missing")
    if evidence.get("status") == "skipped":
        if evidence.get("reasonCode") != "REAL_AUDIO_IMPORT_FIXTURE_REQUIRED":
            fail(
                "Audio import closed-loop skip is not a governed prerequisite skip",
                evidence,
            )
        return {
            "status": "skipped",
            "reason_code": evidence.get("reasonCode"),
        }

    run_id = evidence.get("taskRunId")
    import_batch_id = evidence.get("importBatchId")
    audio_session_id = evidence.get("audioSessionId")
    root_trace_id = evidence.get("rootTraceId")
    task_version_id = evidence.get("taskVersionId")
    connector_id = evidence.get("connectorId")
    platform_connection_id = evidence.get("platformConnectionId")
    target_asset_key = evidence.get("targetAssetKey")
    required_browser_evidence = {
        "taskRunId": run_id,
        "importBatchId": import_batch_id,
        "audioSessionId": audio_session_id,
        "rootTraceId": root_trace_id,
        "taskVersionId": task_version_id,
        "connectorId": connector_id,
        "platformConnectionId": platform_connection_id,
        "targetAssetKey": target_asset_key,
    }
    if (
        any(
            not isinstance(value, str) or not value
            for value in required_browser_evidence.values()
        )
        or evidence.get("status") != "succeeded"
        or evidence.get("executionMode") != "production"
        or evidence.get("previewCount") != 3
        or evidence.get("playbackGrantStatus") != 201
        or evidence.get("playbackStatus") != 206
        or not isinstance(evidence.get("connectorWriteCount"), int)
        or not 1 <= evidence.get("connectorWriteCount") <= 2
        or evidence.get("pageRefreshRecovered") is not True
        or evidence.get("rootTraceReadable") is not True
        or evidence.get("legacyPlatformSyncRequests") != 0
    ):
        fail(
            "Audio import browser evidence does not prove the production P0 chain",
            {"evidence": evidence},
        )

    pair = next(
        (
            (expected, run, event)
            for expected, run, event in run_event_pairs
            if expected.label == "coreFlows.audioImport" and run.run_id == run_id
        ),
        None,
    )
    if pair is None:
        fail(
            "Audio import TaskRun is missing from outbox dispatch verification",
            {"run_id": run_id},
        )
    _expected, run, event = pair
    dispatch = run.payload.get("dispatch") or event.payload.get("adapter_dispatch")
    dispatch_details = (
        dispatch.get("details")
        if isinstance(dispatch, dict) and isinstance(dispatch.get("details"), dict)
        else {}
    )
    completion = run.payload.get("completion_receipt")
    completion_auth = (
        completion.get("auth")
        if isinstance(completion, dict) and isinstance(completion.get("auth"), dict)
        else {}
    )
    result_ref = (
        completion.get("result_ref")
        if isinstance(completion, dict)
        and isinstance(completion.get("result_ref"), dict)
        else {}
    )
    connector_snapshot = (
        run.payload.get("connector_snapshot")
        if isinstance(run.payload.get("connector_snapshot"), dict)
        else {}
    )
    target = (
        run.payload.get("target") if isinstance(run.payload.get("target"), dict) else {}
    )
    if (
        run.run_type != "task_run"
        or run.status != "success"
        or run.trace_id != root_trace_id
        or run.payload.get("execution_mode") != "production"
        or run.payload.get("execution_contract") != "auris-flow-audio-import-v1"
        or run.payload.get("task_version_id") != task_version_id
        or run.payload.get("import_batch_id") != import_batch_id
        or run.payload.get("root_trace_id") != root_trace_id
        or connector_snapshot.get("connector_id") != connector_id
        or connector_snapshot.get("platform_connection_id") != platform_connection_id
        or target.get("target_asset_key") != target_asset_key
        or event.event_type != "task_run.requested"
        or not isinstance(dispatch, dict)
        or dispatch.get("adapter") != "dagster"
        or dispatch_details.get("job_name") != "auris_flow_audio_import_v1"
        or not isinstance(completion, dict)
        or completion.get("adapter") != "dagster"
        or completion.get("source") != "dagster"
        or completion.get("status") != "success"
        or completion.get("root_trace_id") != root_trace_id
        or completion_auth.get("auth_mode") != "signed_external_completion"
        or completion_auth.get("authenticated_source") != "dagster"
        or not completion_auth.get("signature_key_id")
        or not completion_auth.get("signature_mode")
        or result_ref.get("schema_version") != "auris-flow-audio-import-result-v1"
        or result_ref.get("execution_contract") != "auris-flow-audio-import-v1"
        or result_ref.get("import_batch_id") != import_batch_id
    ):
        fail(
            "Audio import TaskRun did not complete through the allowlisted signed Dagster contract",
            {
                "run_id": run_id,
                "run_type": run.run_type,
                "run_status": run.status,
                "run_trace_id": run.trace_id,
                "run_payload": run.payload,
                "event_type": event.event_type,
            },
        )

    with SessionLocal() as session:
        batch = session.get(ImportBatch, str(import_batch_id))
        items = list(
            session.scalars(
                select(ImportBatchItem).where(
                    ImportBatchItem.tenant_id == run.tenant_id,
                    ImportBatchItem.project_id == run.project_id,
                    ImportBatchItem.import_batch_id == import_batch_id,
                )
            )
        )
        if (
            batch is None
            or batch.tenant_id != run.tenant_id
            or batch.project_id != run.project_id
            or batch.task_run_id != run_id
            or batch.task_version_id != task_version_id
            or batch.connector_id != connector_id
            or batch.status != "succeeded"
            or batch.current_stage != "completed"
            or batch.root_trace_id != root_trace_id
            or batch.failed_items != 0
            or batch.succeeded_items < 1
            or batch.total_items != len(items)
            or batch.total_items != evidence.get("total")
            or batch.succeeded_items != evidence.get("succeeded")
            or batch.skipped_items != evidence.get("duplicates")
            or batch.failed_items != evidence.get("failed")
            or batch.payload.get("completion_receipt_id")
            != completion.get("completion_receipt_id")
        ):
            fail(
                "Audio ImportBatch does not match the completed production TaskRun",
                {
                    "evidence": evidence,
                    "batch": {
                        "id": batch.import_batch_id if batch else None,
                        "task_run_id": batch.task_run_id if batch else None,
                        "task_version_id": batch.task_version_id if batch else None,
                        "connector_id": batch.connector_id if batch else None,
                        "status": batch.status if batch else None,
                        "current_stage": batch.current_stage if batch else None,
                        "total_items": batch.total_items if batch else None,
                        "succeeded_items": batch.succeeded_items if batch else None,
                        "skipped_items": batch.skipped_items if batch else None,
                        "failed_items": batch.failed_items if batch else None,
                        "root_trace_id": batch.root_trace_id if batch else None,
                        "payload": batch.payload if batch else None,
                    },
                },
            )

        succeeded_items = [item for item in items if item.status == "succeeded"]
        if not succeeded_items:
            fail(
                "Audio import has no newly materialized successful item",
                {"items": items},
            )
        verified_storage_ids: list[str] = []
        materialized_session_ids: list[str] = []
        for item in succeeded_items:
            storage_object_id = item.payload.get("storage_object_id")
            storage_object = (
                session.get(StorageObject, storage_object_id)
                if isinstance(storage_object_id, str) and storage_object_id
                else None
            )
            session_resource = (
                session.scalar(
                    select(JsonResource).where(
                        JsonResource.tenant_id == run.tenant_id,
                        JsonResource.project_id == run.project_id,
                        JsonResource.collection == "audio_sessions",
                        JsonResource.resource_key == item.audio_session_id,
                    )
                )
                if item.audio_session_id
                else None
            )
            stored_version = (
                storage_object.payload.get("object_version_id")
                if storage_object is not None
                and isinstance(storage_object.payload, dict)
                else None
            )
            if (
                item.root_trace_id != root_trace_id
                or not item.external_record_id
                or not item.object_version
                or not item.audio_session_id
                or storage_object is None
                or storage_object.tenant_id != run.tenant_id
                or storage_object.project_id != run.project_id
                or storage_object.source_type != "task_run"
                or storage_object.source_id != run_id
                or storage_object.status != "verified"
                or stored_version != item.object_version
                or not storage_object.etag
                or not storage_object.content_sha256
                or session_resource is None
                or session_resource.data.get("import_batch_id") != import_batch_id
                or session_resource.data.get("platform_connection_id")
                != platform_connection_id
                or session_resource.data.get("root_trace_id") != root_trace_id
            ):
                fail(
                    "Audio import item is missing exact-version storage or scoped AudioSession materialization",
                    {
                        "item_id": item.import_item_id,
                        "item_status": item.status,
                        "item_object_version": item.object_version,
                        "item_audio_session_id": item.audio_session_id,
                        "storage_object_id": storage_object_id,
                        "storage_status": storage_object.status
                        if storage_object
                        else None,
                        "storage_version": stored_version,
                        "session": session_resource.data if session_resource else None,
                    },
                )
            verified_storage_ids.append(str(storage_object_id))
            materialized_session_ids.append(str(item.audio_session_id))

        manifest_storage_object_id = batch.payload.get("manifest_storage_object_id")
        manifest_storage_object = (
            session.get(StorageObject, manifest_storage_object_id)
            if isinstance(manifest_storage_object_id, str)
            else None
        )
        if (
            manifest_storage_object is None
            or manifest_storage_object.tenant_id != run.tenant_id
            or manifest_storage_object.project_id != run.project_id
            or manifest_storage_object.source_type != "task_run"
            or manifest_storage_object.source_id != run_id
            or manifest_storage_object.status != "verified"
            or manifest_storage_object.content_sha256
            != batch.payload.get("manifest_sha256")
        ):
            fail(
                "Audio import manifest is not a verified exact-run storage object",
                {
                    "manifest_storage_object_id": manifest_storage_object_id,
                    "manifest_sha256": batch.payload.get("manifest_sha256"),
                },
            )

    if audio_session_id not in materialized_session_ids:
        fail(
            "Browser-opened AudioSession was not materialized by the verified import batch",
            {
                "audio_session_id": audio_session_id,
                "materialized_session_ids": materialized_session_ids,
            },
        )
    return {
        "status": "verified",
        "task_run_id": run_id,
        "task_version_id": task_version_id,
        "import_batch_id": import_batch_id,
        "audio_session_id": audio_session_id,
        "root_trace_id": root_trace_id,
        "dagster_job": dispatch_details.get("job_name"),
        "completion_receipt_id": completion.get("completion_receipt_id"),
        "verified_audio_storage_objects": verified_storage_ids,
        "manifest_storage_object_id": manifest_storage_object_id,
        "playback_status": evidence.get("playbackStatus"),
    }


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        fail("DATABASE_URL is required for E2E outbox dispatch verification")
    if os.environ.get("AURIS_E2E_SEED_REAL_AUDIO_FIXTURE") == "1":
        worker_observation = observe_managed_worker()
        if worker_observation is None:
            fail(
                "AURIS_E2E_WORKER_HEALTH_PATH is required to seed the real audio fixture"
            )
        summary = {
            "status": "ok",
            "managed_worker": worker_observation,
            "audio_fixture": verify_audio_recording_range_stream(),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    result = load_artifact()
    started_at = parse_artifact_started_at(result)
    expected_runs = collect_expected_runs(result)
    if not expected_runs:
        fail("No E2E run receipts found to verify", {"artifact": str(ARTIFACT_PATH)})

    worker_observation = observe_managed_worker()
    dispatch_wait = wait_for_expected_outbox_dispatches(expected_runs)
    checked = []
    completed = []
    run_event_pairs: list[tuple[ExpectedRun, RunRecord, OutboxEvent]] = []
    for expected in expected_runs:
        run, event = get_run_and_event(expected)
        checked.append(validate_dispatch(expected, run, event, started_at))
        run_event_pairs.append((expected, run, event))
    assert_dispatch_coverage(checked)
    checked_qdrant_recall = verify_qdrant_recall_coverage(run_event_pairs)
    for expected, run, _event in run_event_pairs:
        completion = complete_submitted_run(expected, run)
        if completion:
            completed.append(completion)
    assert_completion_coverage(expected_runs, completed)
    checked_asset_materializations = verify_asset_backfill_materializations(
        run_event_pairs
    )
    checked_audio_intelligence = verify_audio_intelligence_materialization(
        run_event_pairs
    )
    checked_audio_import = verify_audio_import_closed_loop(result, run_event_pairs)
    checked_audio_range_stream = verify_audio_recording_range_stream()
    checked_agentic_execution = verify_agentic_execution_trace(run_event_pairs)
    checked_voiceprint_enrollment = verify_voiceprint_enrollment_gate(result)
    checked_voiceprint_qdrant = verify_approved_voiceprint_qdrant_index(started_at)

    dispatchable = [item for item in checked if item.get("adapter")]
    blocked = [item for item in checked if item.get("status") == "blocked"]
    summary = {
        "status": "ok",
        "e2e_run_id": result.get("runId"),
        "artifact": str(ARTIFACT_PATH),
        "outbox_result_path": str(OUTBOX_RESULT_PATH),
        "artifact_started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "database_url": os.environ["DATABASE_URL"].split("@")[-1],
        "worker_attempted_events": dispatch_wait["event_count"],
        "worker_delivery_attempts": dispatch_wait["delivery_attempt_count"],
        "worker_dispatch_wait": dispatch_wait,
        "managed_worker": worker_observation,
        "checked_dispatches": dispatchable,
        "checked_qdrant_recall": checked_qdrant_recall,
        "checked_asset_materializations": checked_asset_materializations,
        "checked_audio_intelligence": checked_audio_intelligence,
        "checked_audio_import": checked_audio_import,
        "checked_audio_range_stream": checked_audio_range_stream,
        "checked_agentic_execution": checked_agentic_execution,
        "checked_voiceprint_enrollment": checked_voiceprint_enrollment,
        "checked_voiceprint_qdrant": checked_voiceprint_qdrant,
        "checked_completion_receipts": completed,
        "checked_blocked_runs": blocked,
        "coverage": {
            "dispatch_count": len(dispatchable),
            "completion_receipt_count": len(completed),
            "blocked_run_count": len(blocked),
            "asset_materialization_count": len(checked_asset_materializations),
            "audio_intelligence_count": len(checked_audio_intelligence),
            "audio_import_closed_loop": (
                1 if checked_audio_import.get("status") == "verified" else 0
            ),
            "audio_range_stream": 1,
            "agentic_execution_count": len(checked_agentic_execution),
            "qdrant_recall_count": len(checked_qdrant_recall),
        },
    }
    OUTBOX_RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTBOX_RESULT_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
