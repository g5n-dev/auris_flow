from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import insert, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.logging import get_logger, log_event
from app.core.rbac import require_any_role
from app.core.response import envelope
from app.core.runtime_guards import (
    FAILURE_INJECTION_KEYS,
    failure_injection_enabled,
    requested_failure_injection_fields,
)
from app.models import (
    ExternalCallbackReceipt,
    IdempotencyRecord,
    ImportBatch,
    InsightReport,
    RunCompletionReceipt,
    RunRecord,
    TraceRef,
)
from app.repositories import RunRecordRepository
from app.services.agentic_execution_service import (
    create_agent_execution_projection,
    record_agent_completion,
)
from app.services.audio_intelligence_service import (
    materialize_audio_intelligence_completion,
    resolve_audio_intelligence_result,
    sanitize_audio_intelligence_result,
)
from app.services.audit_service import record_audit
from app.services.data_asset_materialization_service import materialize_asset_completion
from app.services.idempotency_service import (
    api_error_result,
    raise_replayed_api_error,
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.insight_closure_service import (
    materialize_insight_completion,
    rebind_report_document,
    report_payload,
)
from app.services.outbox_service import enqueue_event
from app.services.prompt_candidate_service import (
    materialize_optimization_prompt_candidates,
    materialize_prompt_candidate,
)
from app.services.public_run_projection_service import (
    PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS as _PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS,
)
from app.services.public_run_projection_service import (
    PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS as _PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS,
)
from app.services.public_run_projection_service import (
    project_public_run_value,
    public_run_payload,
    sanitize_public_run_string,
)
from app.services.resource_service import upsert_resource
from app.services.run_completion_storage_service import (
    hydrate_staged_audio_result_ref,
    register_hotword_completion_storage_objects,
    reject_staged_audio_completion_storage_object,
    stage_audio_completion_storage_object,
)

logger = get_logger("run")

# Compatibility exports for release-policy and contract checks. The policy
# itself lives in the dependency-light projection module above.
PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS = _PUBLIC_RUN_FORBIDDEN_FIELD_FINGERPRINTS
PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS = _PUBLIC_RUN_FORBIDDEN_FIELD_TOKENS

RUN_INITIAL_STATUSES = {"queued", "pending", "running", "blocked"}
RUN_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "queued": {
        "running",
        "submitted",
        "success",
        "failed",
        "blocked",
        "cancelling",
        "cancelled",
    },
    "pending": {
        "running",
        "submitted",
        "success",
        "failed",
        "blocked",
        "cancelling",
        "cancelled",
    },
    "running": {
        "submitted",
        "completion_pending",
        "success",
        "failed",
        "blocked",
        "cancelling",
        "cancelled",
    },
    "submitted": {
        "running",
        "completion_pending",
        "success",
        "failed",
        "blocked",
        "cancelling",
        "cancelled",
    },
    "completion_pending": {"success", "failed", "blocked", "cancelling", "cancelled"},
    "cancelling": {"completion_pending", "failed", "cancelled"},
    "blocked": {"pending", "cancelled"},
    "success": set(),
    "failed": set(),
    "cancelled": set(),
}

RUN_CREATE_ROLE_POLICY: dict[str, tuple[str, ...]] = {
    "audio_ingest": ("project_admin", "asset_manager"),
    "audio_intelligence": ("project_admin", "asset_manager", "model_engineer"),
    "asset_backfill": ("project_admin", "asset_manager"),
    "asset_check_retry": ("project_admin", "asset_manager", "model_engineer"),
    "boundary_sync": ("project_admin", "asset_manager", "review_arbitrator"),
    "eval_feedback": ("project_admin", "model_engineer", "review_arbitrator"),
    "eval_run": ("project_admin", "model_engineer"),
    "export": ("project_admin", "asset_manager"),
    "external_callback": ("project_admin", "asset_manager"),
    "insight_metric_aggregation": ("project_admin", "asset_manager", "model_engineer"),
    "insight_report": ("project_admin", "asset_manager"),
    "knowledge_build": ("project_admin", "asset_manager", "model_engineer"),
    "knowledge_sync": ("project_admin", "asset_manager", "model_engineer"),
    "hotword_analysis": ("project_admin", "model_engineer"),
    "hotword_build": ("project_admin", "model_engineer"),
    "hotword_eval": ("project_admin", "model_engineer"),
    "hotword_publish": ("project_admin",),
    "hotword_rollback": ("model_engineer",),
    "label_optimization": ("project_admin", "model_engineer", "review_arbitrator"),
    "label_extraction": ("project_admin", "model_engineer"),
    "label_publish": ("project_admin", "model_engineer"),
    "release_command": ("system",),
    "platform_sync": ("project_admin", "asset_manager"),
    "provider_test": ("project_admin", "model_engineer"),
    "scene_profile_generation": ("project_admin", "model_engineer"),
    "settings_publish": ("project_admin",),
    "task_run": ("project_admin", "asset_manager", "model_engineer"),
    "task_version_publish": ("project_admin", "model_engineer"),
}
RUN_RETRY_ENTRY_ROLES = tuple(
    sorted(
        {
            role
            for allowed_roles in RUN_CREATE_ROLE_POLICY.values()
            for role in allowed_roles
            if role != "system"
        }
    )
)

RUN_EVENT_TYPES: dict[str, str] = {
    "audio_ingest": "audio_ingest.requested",
    "audio_intelligence": "audio_intelligence.requested",
    "asset_backfill": "backfill.requested",
    "asset_check_retry": "asset_check.retry_requested",
    "boundary_sync": "conversation_boundary.sync_requested",
    "eval_feedback": "agent_run.requested",
    "eval_run": "eval_run.requested",
    "export": "export.requested",
    "external_callback": "external_callback.requested",
    "insight_metric_aggregation": "insight_metric_aggregation.requested",
    "insight_report": "export.requested",
    "knowledge_build": "knowledge_index.build_requested",
    "knowledge_sync": "knowledge_source.sync_requested",
    "hotword_analysis": "hotword_analysis.requested",
    "hotword_build": "hotword_pack_version.build-requested",
    "hotword_eval": "hotword_pack_version.eval-requested",
    "hotword_publish": "hotword_pack_version.publish-requested",
    "hotword_rollback": "hotword_pack_version.rollback-requested",
    "label_optimization": "agent_run.requested",
    "label_extraction": "agent_run.requested",
    "label_publish": "label_version.publish_requested",
    "release_command": "release_deployment.command-requested",
    "scene_profile_generation": "scene_profile.generation-requested",
    "platform_sync": "platform_sync.requested",
    "provider_test": "provider_test.requested",
    "settings_publish": "settings.publish_requested",
    "task_run": "task_run.requested",
    "task_version_publish": "task_version.publish_requested",
}

RUN_RETRYABLE_STATUSES = {"failed", "cancelled"}
RUN_COMPLETION_ROLES = ("project_admin",)
RUN_COMPLETION_EXTERNAL_ID_KEYS = {
    "dagster": "external_run_id",
    "object_storage": "storage_object_id",
    "external_callback": "callback_receipt_id",
}
RUN_SYSTEM_PAYLOAD_KEYS = {
    "adapter_dispatch",
    "business_status",
    "business_completion_required",
    "callback_receipt_id",
    "completed_at",
    "completion_mode",
    "completion_receipt",
    "completion_receipt_id",
    "dead_letter_event_id",
    "dispatch",
    "dispatch_state",
    "download_ref",
    "error",
    "error_code",
    "experiment_completion",
    "external_run_id",
    "failed_event_id",
    "id",
    "import_batch",
    "insight_completion",
    "label_eval_result",
    "materialized_assets",
    "materialized_outputs",
    "metrics",
    "monitor_generation",
    "next_actions",
    "next_retry_at",
    "next_status_sync_at",
    "processed_event_id",
    "prompt_candidate_id",
    "prompt_candidate_status",
    "release_command_result",
    "registered_storage_objects",
    "result_ref",
    "retry_count",
    "retryable",
    "run_id",
    "status",
    "status_history",
    "storage_object_id",
    "task_run_id",
    "trace_id",
    "deadline_at",
}
RUN_FAILURE_SIMULATION_KEYS = FAILURE_INJECTION_KEYS
HOTWORD_FROZEN_RETRY_KEYS = {
    "origin_run_id",
    "hotword_pack_version_id",
    "baseline_version_id",
    "baseline_mode",
    "baseline_ref",
    "evaluated_term_ids",
    "evaluated_terms",
    "evaluated_term_changes",
    "pack_id",
    "prior_current_version_id",
    "eval_dataset_id",
    "content_sha256",
    "manifest_storage_object_id",
    "provider",
    "provider_artifact_ref",
    "artifact_sha256",
    "model_approved_by",
    "project_admin_confirmed_by",
    "confirmation",
    "source_version_id",
    "target_version_id",
    "source_resource_version",
    "target_resource_version",
    "pack_resource_version",
    "source_root_trace_id",
    "target_root_trace_id",
    "pack_root_trace_id",
    "expected_resource_version",
    "reason",
    # 受控回滚的发起人和完整审批载荷也是冻结绑定，重试不得覆写。
    "requested_by",
    "release_gate",
}
RUN_SAFE_RETRY_OVERRIDE_KEYS = {
    "partition_key",
    "run_key",
    "queue",
    "priority",
    "retry_after_seconds",
    "max_attempts",
}


@dataclass(frozen=True)
class RunPage:
    items: list[dict[str, Any]]
    total: int
    limit: int
    next_cursor: str | None


def encode_run_cursor(record: RunRecord) -> str:
    created_at = record.created_at
    if created_at.tzinfo is not None:
        created_at = created_at.astimezone(UTC).replace(tzinfo=None)
    created = created_at.isoformat()
    token = f"run_record|{created}|{record.run_id}".encode()
    return urlsafe_b64encode(token).decode("ascii").rstrip("=")


def decode_run_cursor(cursor: str | int | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    cursor_text = str(cursor)
    try:
        padded = cursor_text + "=" * (-len(cursor_text) % 4)
        raw = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None
    prefix, created_at, run_id = raw.split("|", 2)
    if prefix != "run_record" or not created_at or not run_id:
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400)
    try:
        decoded_created_at = datetime.fromisoformat(created_at)
        if decoded_created_at.tzinfo is not None:
            decoded_created_at = decoded_created_at.astimezone(UTC).replace(tzinfo=None)
        return decoded_created_at, run_id
    except ValueError:
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None


def page_limit(page: dict[str, str | int | None]) -> int:
    return int(page.get("limit") or 50)


def validate_initial_run_status(status: str) -> None:
    if status not in RUN_INITIAL_STATUSES:
        raise ApiError(
            "INVALID_RUN_STATUS",
            f"运行初始状态不合法：{status}",
            409,
            details=[{"status": status, "allowed": sorted(RUN_INITIAL_STATUSES)}],
        )


def reject_runtime_failure_injection(payload: dict[str, Any]) -> None:
    requested_fields = requested_failure_injection_fields(payload)
    if requested_fields and not failure_injection_enabled():
        raise ApiError(
            "RUNTIME_FAILURE_INJECTION_NOT_ALLOWED",
            "失败注入字段只允许在 test/ci 环境使用",
            400,
            details=[{"fields": requested_fields, "allowed_envs": ["test", "ci"]}],
        )


def transition_run(record: RunRecord, target_status: str, *, reason: str) -> None:
    if target_status == "blocked" and record.payload.get("dispatch_state") == "dispatching":
        raise ApiError(
            "RUN_DISPATCH_IN_PROGRESS",
            "运行已进入外部调度阶段，不能再变更为阻断状态",
            409,
            details=[{"run_id": record.run_id, "status": record.status}],
        )
    allowed = RUN_STATUS_TRANSITIONS.get(record.status, set())
    if target_status not in allowed:
        raise ApiError(
            "INVALID_RUN_TRANSITION",
            f"运行状态不能从 {record.status} 迁移到 {target_status}",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "from": record.status,
                    "to": target_status,
                    "allowed": sorted(allowed),
                }
            ],
        )
    history = list(record.payload.get("status_history", []))
    history.append({"from": record.status, "to": target_status, "reason": reason})
    now = datetime.now(UTC)
    record.status = target_status
    record.status_version = int(record.status_version or 1) + 1
    if target_status == "running" and record.started_at is None:
        record.started_at = now
    if target_status == "submitted" and record.submitted_at is None:
        record.submitted_at = now
        if record.run_type == "task_run" and record.next_status_sync_at is None:
            record.next_status_sync_at = now + timedelta(
                seconds=get_settings().task_run_status_sync_interval_seconds
            )
    if target_status in {"success", "failed", "cancelled"} and record.finished_at is None:
        record.finished_at = now
    if target_status in {"success", "failed", "cancelled"}:
        record.next_status_sync_at = None
    record.payload = {**record.payload, "status": target_status, "status_history": history}


def public_run_response(
    response: dict[str, Any],
    ctx: RequestContext,
) -> dict[str, Any]:
    """Re-project legacy stored responses before they cross the public boundary."""

    projected = project_public_run_value(response, field_name="response")
    if not isinstance(projected, dict):
        return {}
    data = projected.get("data")
    if (
        isinstance(data, dict)
        and isinstance(data.get("run_id"), str)
        and isinstance(data.get("run_type"), str)
    ):
        projected["data"] = {
            **data,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
        }
    return projected


def run_payload(record: RunRecord) -> dict[str, Any]:
    def iso_or_none(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    public_payload = public_run_payload(record.payload)
    return {
        "run_id": record.run_id,
        "id": record.run_id,
        "run_type": record.run_type,
        "run_key": (
            sanitize_public_run_string(record.run_key, field_name="run_key")
            if record.run_key is not None
            else None
        ),
        "partition_key": (
            sanitize_public_run_string(record.partition_key, field_name="partition_key")
            if record.partition_key is not None
            else None
        ),
        "submitted_at": iso_or_none(record.submitted_at),
        "started_at": iso_or_none(record.started_at),
        "finished_at": iso_or_none(record.finished_at),
        "cancel_requested_at": iso_or_none(record.cancel_requested_at),
        "cancel_reason": (
            sanitize_public_run_string(record.cancel_reason, field_name="cancel_reason")
            if record.cancel_reason is not None
            else None
        ),
        "terminal_reason": (
            sanitize_public_run_string(record.terminal_reason, field_name="terminal_reason")
            if record.terminal_reason is not None
            else None
        ),
        **public_payload,
        "affected_objects": public_payload.get("affected_objects", []),
        "next_actions": public_payload.get("next_actions", []),
        "status_history": public_payload.get("status_history", []),
        # Adapter and business projections can add their own lineage fields,
        # but the public run trace is always the persisted RunRecord trace.
        "trace_id": record.trace_id,
        # These control-plane values always come from strong columns. They are
        # repeated after payload so no legacy/internal JSON can shadow them.
        "status": record.status,
        "status_version": record.status_version,
        "deadline_at": iso_or_none(record.deadline_at),
        # Scope always comes from strong columns and cannot be shadowed by a
        # legacy payload copied from another tenant or project.
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
    }


def _sync_eval_run_projection(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
) -> None:
    if record.run_type != "eval_run":
        return
    projection = {
        **run_payload(record),
        "id": record.run_id,
        "run_id": record.run_id,
        "eval_run_id": record.run_id,
        "run_type": record.run_type,
        "status": record.status,
        "trace_id": record.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        "eval_runs",
        record.run_id,
        projection,
        status=record.status,
        trace_id=record.trace_id,
        audit_action="eval_run.projection_synced",
    )


def _completion_receipt_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _completion_external_id(record: RunRecord, adapter: str) -> str | None:
    dispatch = record.payload.get("dispatch")
    details = dispatch.get("details") if isinstance(dispatch, dict) else None
    if not isinstance(details, dict):
        return None
    key = RUN_COMPLETION_EXTERNAL_ID_KEYS.get(adapter)
    value = details.get(key) if key else None
    return str(value) if value else None


def _validate_completion_source_binding(
    record: RunRecord,
    payload: dict[str, Any],
    *,
    strict_external_receipt: bool,
    authenticated_source: str | None,
) -> str:
    dispatch = record.payload.get("dispatch")
    if not isinstance(dispatch, dict):
        raise ApiError(
            "RUN_DISPATCH_RECEIPT_MISSING",
            "运行缺少协议分发回执，不能接收完成回执",
            409,
            details=[{"run_id": record.run_id}],
        )
    dispatch_adapter = str(dispatch.get("adapter") or "")
    if not dispatch_adapter:
        raise ApiError(
            "RUN_DISPATCH_ADAPTER_MISSING",
            "运行分发回执缺少可信执行来源",
            409,
            details=[{"run_id": record.run_id}],
        )

    payload_adapter = payload.get("adapter")
    if strict_external_receipt and not payload_adapter:
        raise ApiError(
            "RUN_COMPLETION_ADAPTER_REQUIRED",
            "外部完成回执必须声明执行来源",
            400,
            details=[{"run_id": record.run_id}],
        )
    declared_adapter = payload_adapter or payload.get("source")
    payload_source = payload.get("source")
    if payload_source and payload_adapter and str(payload_source) != str(payload_adapter):
        raise ApiError(
            "RUN_COMPLETION_PAYLOAD_SOURCE_MISMATCH",
            "完成回执声明的执行来源不一致",
            409,
            details=[{"run_id": record.run_id}],
        )
    if authenticated_source and str(declared_adapter or "") != authenticated_source:
        raise ApiError(
            "RUN_COMPLETION_AUTH_SOURCE_MISMATCH",
            "签名认证来源与完成回执声明不一致",
            403,
            details=[{"run_id": record.run_id}],
        )
    if declared_adapter and str(declared_adapter) != dispatch_adapter:
        raise ApiError(
            "RUN_COMPLETION_ADAPTER_MISMATCH",
            "完成回执来源与运行绑定不一致",
            409,
            details=[{"run_id": record.run_id}],
        )
    return dispatch_adapter


def _validate_completion_receipt(
    record: RunRecord,
    payload: dict[str, Any],
    *,
    strict_external_receipt: bool = False,
    authenticated_source: str | None = None,
) -> tuple[str, str]:
    if record.status not in {"submitted", "running", "completion_pending"}:
        raise ApiError(
            "RUN_COMPLETION_NOT_ALLOWED",
            f"运行状态 {record.status} 不能接收完成回执",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "allowed_statuses": ["completion_pending", "running", "submitted"],
                }
            ],
        )
    if record.payload.get("business_completion_required") is not True:
        raise ApiError(
            "RUN_COMPLETION_NOT_REQUIRED",
            "当前运行不需要外部完成回执",
            409,
            details=[{"run_id": record.run_id, "status": record.status}],
        )
    adapter = _validate_completion_source_binding(
        record,
        payload,
        strict_external_receipt=strict_external_receipt,
        authenticated_source=authenticated_source,
    )
    expected_external_id = _completion_external_id(record, adapter)
    if strict_external_receipt and not expected_external_id:
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_UNAVAILABLE",
            "运行缺少可校验的远端运行引用",
            409,
            details=[{"run_id": record.run_id}],
        )
    actual_external_id = payload.get("external_id")
    if strict_external_receipt and not actual_external_id:
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_REQUIRED",
            "外部完成回执必须携带远端运行引用",
            400,
            details=[{"run_id": record.run_id}],
        )
    if (
        actual_external_id
        and expected_external_id
        and str(actual_external_id) != expected_external_id
    ):
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_MISMATCH",
            "完成回执远端运行引用与运行绑定不一致",
            409,
            details=[{"run_id": record.run_id}],
        )
    if not actual_external_id and expected_external_id and not strict_external_receipt:
        payload["external_id"] = expected_external_id
    if (
        record.run_type == "task_run"
        and record.payload.get("execution_contract") == "auris-flow-audio-import-v1"
    ):
        from app.services.audio_import_completion_service import (
            validate_audio_import_completion_contract,
        )

        validate_audio_import_completion_contract(record, payload)
    return adapter, str(payload.get("external_id") or "")


def _stageable_early_dagster_completion(
    record: RunRecord,
    payload: dict[str, Any],
    *,
    strict_external_receipt: bool,
    authenticated_source: str | None,
) -> bool:
    if not strict_external_receipt or authenticated_source != "dagster":
        return False
    if record.run_type not in {"task_run", "audio_intelligence"} or record.status not in {
        "pending",
        "running",
    }:
        return False
    if isinstance(record.payload.get("dispatch"), dict):
        return False
    declared_adapter = str(payload.get("adapter") or payload.get("source") or "")
    declared_source = str(payload.get("source") or payload.get("adapter") or "")
    if declared_adapter != "dagster" or declared_source != "dagster":
        raise ApiError(
            "RUN_COMPLETION_AUTH_SOURCE_MISMATCH",
            "早到完成回执必须由已验签执行来源提交",
            403,
        )
    if not payload.get("external_id"):
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_REQUIRED",
            "早到完成回执必须携带远端运行引用",
            400,
        )
    return True


def _stageable_cancelling_dagster_completion(
    record: RunRecord,
    payload: dict[str, Any],
    *,
    strict_external_receipt: bool,
    authenticated_source: str | None,
) -> bool:
    """Accept a signed Dagster success receipt while cancellation is unresolved.

    Staging does not mutate the TaskRun or its status-version fence. The receipt
    becomes authoritative only if a fenced cancel/status query subsequently
    observes Dagster SUCCESS; an observed cancellation or failure rejects it.
    """

    if (
        not strict_external_receipt
        or authenticated_source != "dagster"
        or record.run_type != "task_run"
        or record.status != "cancelling"
        or payload.get("status") != "success"
    ):
        return False
    if record.payload.get("business_completion_required") is not True:
        raise ApiError(
            "RUN_COMPLETION_NOT_REQUIRED",
            "当前任务运行不需要外部完成回执",
            409,
            details=[{"run_id": record.run_id, "status": record.status}],
        )
    adapter = _validate_completion_source_binding(
        record,
        payload,
        strict_external_receipt=True,
        authenticated_source="dagster",
    )
    expected_external_id = _completion_external_id(record, adapter)
    actual_external_id = str(payload.get("external_id") or "")
    if not expected_external_id:
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_UNAVAILABLE",
            "运行缺少可校验的远端运行引用",
            409,
            details=[{"run_id": record.run_id}],
        )
    if not actual_external_id:
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_REQUIRED",
            "外部完成回执必须携带远端运行引用",
            400,
            details=[{"run_id": record.run_id}],
        )
    if not hmac.compare_digest(actual_external_id, expected_external_id):
        raise ApiError(
            "RUN_COMPLETION_EXTERNAL_ID_MISMATCH",
            "取消竞争中的完成回执与可信运行绑定不一致",
            409,
            details=[{"run_id": record.run_id}],
        )
    return True


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


def _require_signed_completion_auth(
    ctx: RequestContext,
    completion_auth: dict[str, Any] | None,
) -> str:
    if not completion_auth or completion_auth.get("auth_mode") != "signed_external_completion":
        raise ApiError(
            "COMPLETION_SIGNATURE_EVIDENCE_MISSING",
            "外部完成回执缺少签名认证证据",
            401,
        )
    key_id = _optional_text(completion_auth.get("signature_key_id"))
    source = _optional_text(completion_auth.get("authenticated_source"))
    tenant_id = _optional_text(completion_auth.get("authenticated_tenant_id"))
    project_id = _optional_text(completion_auth.get("authenticated_project_id"))
    if not key_id or not source or not tenant_id or not project_id:
        raise ApiError(
            "COMPLETION_SIGNATURE_EVIDENCE_MISSING",
            "外部完成回执缺少签名来源或范围证据",
            401,
        )
    if tenant_id != ctx.tenant_id or project_id != ctx.project_id:
        raise ApiError(
            "COMPLETION_SIGNATURE_SCOPE_MISMATCH",
            "完成回执认证范围与请求上下文不一致",
            403,
            details=[
                {
                    "tenant_id": ctx.tenant_id,
                    "project_id": ctx.project_id,
                    "authenticated_tenant_id": tenant_id,
                    "authenticated_project_id": project_id,
                }
            ],
        )
    return source


def _discard_pending_completion_nonce(
    session: Session,
    ctx: RequestContext,
    *,
    operation: str,
    nonce: str,
) -> None:
    for pending in tuple(session.new):
        if not isinstance(pending, IdempotencyRecord):
            continue
        if (
            pending.tenant_id == ctx.tenant_id
            and pending.project_id == ctx.project_id
            and pending.operation == operation
            and pending.idempotency_key == nonce
        ):
            session.expunge(pending)


def _claim_completion_nonce(
    session: Session,
    ctx: RequestContext,
    completion_auth: dict[str, Any] | None,
) -> None:
    if not completion_auth:
        return
    nonce = _optional_text(completion_auth.get("nonce"))
    request_sha256 = _optional_text(completion_auth.get("request_sha256"))
    signature_key_id = _optional_text(completion_auth.get("signature_key_id"))
    if not nonce or not request_sha256 or not signature_key_id:
        raise ApiError(
            "COMPLETION_SIGNATURE_EVIDENCE_MISSING",
            "签名完成回执缺少防重放证据",
            401,
        )
    operation = f"signed_completion_nonce:{signature_key_id}"
    _discard_pending_completion_nonce(
        session,
        ctx,
        operation=operation,
        nonce=nonce,
    )
    try:
        replay = replay_or_conflict(
            session,
            ctx,
            operation=operation,
            body_hash=request_sha256,
            idempotency_key=nonce,
        )
    except ApiError as exc:
        if exc.code in {"IDEMPOTENCY_KEY_CONFLICT", "IDEMPOTENCY_REQUEST_IN_PROGRESS"}:
            raise ApiError(
                "COMPLETION_SIGNATURE_REPLAY",
                "完成回执 nonce 已被不同请求使用",
                409,
            ) from exc
        raise
    if replay is None:
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=request_sha256,
            status_code=202,
            response_json={"data": {"nonce": nonce, "status": "accepted"}},
            idempotency_key=nonce,
        )


def _insert_completion_receipt_if_absent(
    session: Session,
    values: dict[str, Any],
) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        sqlite_statement = sqlite_insert(RunCompletionReceipt).values(**values)
        session.execute(sqlite_statement.on_conflict_do_nothing())
        return
    if dialect in {"mysql", "mariadb"}:
        mysql_statement = mysql_insert(RunCompletionReceipt).values(**values)
        session.execute(
            mysql_statement.on_duplicate_key_update(
                processing_token=RunCompletionReceipt.processing_token
            )
        )
        return

    connection = session.connection()
    try:
        with connection.begin_nested():
            connection.execute(insert(RunCompletionReceipt).values(**values))
    except IntegrityError:
        pass


def _completion_receipt_by_id(
    session: Session,
    ctx: RequestContext,
    completion_receipt_id: str,
) -> RunCompletionReceipt | None:
    return session.scalar(
        select(RunCompletionReceipt)
        .where(
            RunCompletionReceipt.tenant_id == ctx.tenant_id,
            RunCompletionReceipt.project_id == ctx.project_id,
            RunCompletionReceipt.completion_receipt_id == completion_receipt_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _completion_receipt_by_run(
    session: Session,
    ctx: RequestContext,
    run_id: str,
) -> RunCompletionReceipt | None:
    return session.scalar(
        select(RunCompletionReceipt)
        .where(
            RunCompletionReceipt.tenant_id == ctx.tenant_id,
            RunCompletionReceipt.project_id == ctx.project_id,
            RunCompletionReceipt.run_id == run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _claim_completion_receipt(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    completion_receipt_id: str,
    receipt_hash: str,
    payload: dict[str, Any],
    request_body: dict[str, Any],
    completion_auth: dict[str, Any] | None,
    adapter: str,
    authenticated_source: str | None,
) -> tuple[RunCompletionReceipt, bool]:
    source = authenticated_source or str(payload.get("source") or adapter)
    processing_token = uuid.uuid4().hex
    auth = completion_auth or {}
    _insert_completion_receipt_if_absent(
        session,
        {
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "completion_receipt_id": completion_receipt_id,
            "run_id": record.run_id,
            "receipt_hash": receipt_hash,
            "processing_state": "processing",
            "processing_token": processing_token,
            "completion_status": None,
            "status_code": None,
            "adapter": adapter,
            "source": source,
            "external_id": _optional_text(payload.get("external_id")),
            "request_body": _json_copy(request_body),
            "response_json": None,
            "signature_key_id": _optional_text(auth.get("signature_key_id")),
            "authenticated_source": _optional_text(auth.get("authenticated_source")),
            "signature_nonce": _optional_text(auth.get("nonce")),
            "signature_request_hash": _optional_text(auth.get("request_sha256")),
            "signature_body_hash": _optional_text(auth.get("body_sha256")),
            "signature_mode": _optional_text(auth.get("signature_mode")),
            "signed_at": _optional_text(auth.get("signed_at")),
            "request_id": ctx.request_id,
            "request_trace_id": ctx.trace_id,
            "run_trace_id": record.trace_id,
        },
    )
    receipt = _completion_receipt_by_id(session, ctx, completion_receipt_id)
    if receipt is None:
        receipt = _completion_receipt_by_run(session, ctx, record.run_id)
    if receipt is None:
        raise RuntimeError("completion receipt reservation was not persisted")
    return receipt, receipt.processing_token == processing_token


def _finalize_completion_receipt(
    receipt: RunCompletionReceipt,
    *,
    adapter: str,
    external_id: str,
    completion_status: str,
    response: dict[str, Any],
) -> None:
    receipt.adapter = adapter
    receipt.source = receipt.source or adapter
    receipt.external_id = external_id or None
    receipt.processing_state = "completed"
    receipt.completion_status = completion_status
    receipt.status_code = 200
    receipt.response_json = _json_copy(response)
    receipt.completed_at = datetime.now(UTC)


def _replay_claimed_completion_receipt(
    session: Session,
    ctx: RequestContext,
    receipt: RunCompletionReceipt,
    *,
    run_id: str,
    completion_receipt_id: str,
    receipt_hash: str,
    operation: str,
    body_hash: str,
) -> dict[str, Any]:
    if receipt.completion_receipt_id != completion_receipt_id:
        raise ApiError(
            "RUN_ALREADY_COMPLETED",
            "运行已接收完成回执，不能被另一条回执覆盖",
            409,
            details=[
                {
                    "run_id": run_id,
                    "existing_completion_receipt_id": receipt.completion_receipt_id,
                }
            ],
        )
    if receipt.run_id != run_id or receipt.receipt_hash != receipt_hash:
        raise ApiError(
            "RUN_COMPLETION_RECEIPT_CONFLICT",
            "同一个 completion_receipt_id 不能复用到不同运行或完成结果",
            409,
            details=[
                {
                    "run_id": run_id,
                    "existing_run_id": receipt.run_id,
                    "completion_receipt_id": completion_receipt_id,
                }
            ],
        )
    if receipt.processing_state == "rejected":
        raise ApiError(
            "RUN_COMPLETION_RECEIPT_REJECTED",
            "完成回执已被可信运行绑定拒绝，不能重放或重新绑定",
            409,
            details=[{"run_id": run_id, "completion_receipt_id": completion_receipt_id}],
        )
    if receipt.processing_state != "completed" or receipt.response_json is None:
        raise ApiError(
            "RUN_COMPLETION_RECEIPT_IN_PROGRESS",
            "完成回执仍在处理中",
            409,
            details=[{"run_id": run_id, "completion_receipt_id": completion_receipt_id}],
            retryable=True,
        )
    response = public_run_response(_json_copy(receipt.response_json), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=receipt.status_code or 200,
        response_json=response,
    )
    session.commit()
    log_event(
        logger,
        "run.completion_receipt_replayed",
        ctx=ctx,
        run_id=run_id,
        completion_receipt_id=completion_receipt_id,
    )
    return response


def _link_completion_trace_to_run_root(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
) -> str:
    """Attach a completion action trace to the server-frozen run lineage."""

    run_payload = record.payload or {}
    root_trace_id = str(run_payload.get("root_trace_id") or record.trace_id or ctx.trace_id)
    if root_trace_id == ctx.trace_id:
        return root_trace_id

    digest = hashlib.sha256(
        (
            f"{ctx.tenant_id}|{ctx.project_id}|{ctx.trace_id}|{record.run_id}|{root_trace_id}"
        ).encode()
    ).hexdigest()[:24]
    session.merge(
        TraceRef(
            trace_ref_id=f"trace_ref_completion_{digest}",
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            status="active",
            trace_id=ctx.trace_id,
            payload={
                "root_trace_id": root_trace_id,
                "parent_trace_id": record.trace_id,
                "correlation_id": root_trace_id,
                "request_id": ctx.request_id,
                "ref_role": "run_completion",
                "type": "trace_context",
                "id": root_trace_id,
                "source": "run_record",
                "source_field": "run.root_trace_id",
                "source_run_id": record.run_id,
                "source_run_type": record.run_type,
            },
        )
    )
    return root_trace_id


def apply_staged_dagster_completion(
    session: Session,
    record: RunRecord,
    *,
    processing_states: tuple[str, ...] = ("pending_binding",),
) -> bool:
    """Bind and apply a signed receipt that arrived before Dagster launch finalized.

    The receipt is already authenticated and durable. It becomes authoritative only
    after the launch receipt supplies the exact external Dagster run id.
    """

    allowed_states = tuple(
        state for state in processing_states if state in {"pending_binding", "pending_cancel"}
    )
    if not allowed_states:
        return False
    receipt = session.scalar(
        select(RunCompletionReceipt)
        .where(
            RunCompletionReceipt.tenant_id == record.tenant_id,
            RunCompletionReceipt.project_id == record.project_id,
            RunCompletionReceipt.run_id == record.run_id,
            RunCompletionReceipt.processing_state.in_(allowed_states),
        )
        .with_for_update()
    )
    if receipt is None:
        return False
    staged_processing_state = receipt.processing_state
    payload = _json_copy(receipt.request_body)
    ctx = RequestContext(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        user_id=f"ext:{(receipt.signature_key_id or 'dagster')[:48]}",
        roles=("system", "external_completion_client"),
        request_id=receipt.request_id,
        trace_id=receipt.request_trace_id,
        idempotency_key=f"dagster-completion:{receipt.external_id or record.run_id}",
        parent_trace_id=record.trace_id,
        correlation_id=record.trace_id,
        actor_kind="service",
    )
    expected_external_id = _completion_external_id(record, "dagster")
    actual_external_id = str(payload.get("external_id") or receipt.external_id or "")

    def reject(code: str, message: str) -> bool:
        reject_staged_audio_completion_storage_object(
            session,
            record,
            payload.get("result_ref"),
            completion_receipt_id=receipt.completion_receipt_id,
            rejection_code=code,
        )
        receipt.processing_state = "rejected"
        receipt.completion_status = "rejected"
        receipt.status_code = 409
        receipt.response_json = {
            "error": {
                "code": code,
                "message": message,
                "status": 409,
                "trace_id": receipt.request_trace_id,
            }
        }
        receipt.completed_at = datetime.now(UTC)
        record_audit(
            session,
            ctx,
            action=f"{record.run_type}.completion_rejected",
            object_type=record.run_type,
            object_id=record.run_id,
            result="failed",
            after={
                "code": code,
                "completion_receipt_id": receipt.completion_receipt_id,
                "expected_external_id": expected_external_id,
                "actual_external_id": actual_external_id,
            },
            trace_id=record.trace_id,
        )
        return False

    if not expected_external_id or not hmac.compare_digest(
        actual_external_id, expected_external_id
    ):
        return reject(
            "RUN_COMPLETION_EXTERNAL_ID_MISMATCH",
            "早到完成回执与可信 Dagster launch binding 不一致",
        )
    if record.status not in {"submitted", "completion_pending"}:
        return reject(
            "RUN_COMPLETION_NOT_ALLOWED",
            f"运行状态 {record.status} 不能应用早到完成回执",
        )

    audio_domain_result: dict[str, Any] | None = None
    try:
        status = str(payload.get("status") or "success")
        target_status = "failed" if status == "failed" else "success"
        if target_status == "success" and record.run_type == "audio_intelligence":
            payload["result_ref"] = hydrate_staged_audio_result_ref(
                session,
                record,
                payload.get("result_ref"),
                completion_receipt_id=receipt.completion_receipt_id,
            )
        adapter, external_id = _validate_completion_receipt(
            record,
            payload,
            strict_external_receipt=True,
            authenticated_source="dagster",
        )
        if target_status == "success":
            _validate_experiment_bundle_execution(record, payload)
        if target_status == "success" and record.run_type == "audio_intelligence":
            audio_domain_result = resolve_audio_intelligence_result(
                record,
                payload.get("result_ref"),
            )
    except ApiError as exc:
        return reject(exc.code, exc.message)

    completion_root_trace_id = _link_completion_trace_to_run_root(session, ctx, record)
    completion_reason = (
        "dagster_completion_won_cancellation_race"
        if staged_processing_state == "pending_cancel"
        else "dagster_staged_completion_bound"
    )
    is_audio_import = (
        record.run_type == "task_run"
        and record.payload.get("execution_contract") == "auris-flow-audio-import-v1"
    )
    if target_status == "success":
        from app.services.audio_import_progress_service import (
            mark_audio_import_batch_materializing,
        )

        mark_audio_import_batch_materializing(
            session,
            ctx,
            record,
            completion_receipt_id=receipt.completion_receipt_id,
            result_ref=payload.get("result_ref"),
        )
    if not is_audio_import:
        transition_run(record, target_status, reason=completion_reason)
    registered_storage_objects = (
        register_hotword_completion_storage_objects(
            session,
            ctx,
            record,
            payload.get("result_ref"),
            staged_completion_receipt_id=receipt.completion_receipt_id,
        )
        if target_status == "success"
        else []
    )
    raw_result_ref = payload.get("result_ref") or {}
    persisted_result_ref = (
        sanitize_audio_intelligence_result(raw_result_ref)
        if record.run_type == "audio_intelligence" and isinstance(raw_result_ref, dict)
        else raw_result_ref
    )
    completion_receipt: dict[str, Any] = {
        "completion_receipt_id": receipt.completion_receipt_id,
        "receipt_hash": receipt.receipt_hash,
        "adapter": adapter,
        "external_id": external_id,
        "source": "dagster",
        "status": target_status,
        "result_ref": persisted_result_ref,
        "metrics": payload.get("metrics") or {},
        "note": payload.get("note"),
        "error_code": payload.get("error_code"),
        "retryable": bool(payload.get("retryable", True)),
        "received_at": receipt.received_at.isoformat(),
        "trace_id": receipt.request_trace_id,
        "run_trace_id": record.trace_id,
        "root_trace_id": completion_root_trace_id,
        "auth": {
            "auth_mode": "signed_external_completion",
            "signature_key_id": receipt.signature_key_id,
            "authenticated_source": receipt.authenticated_source,
            "nonce": receipt.signature_nonce,
            "request_sha256": receipt.signature_request_hash,
            "body_sha256": receipt.signature_body_hash,
            "signature_mode": receipt.signature_mode,
            "signed_at": receipt.signed_at,
        },
        "registered_storage_objects": registered_storage_objects,
    }
    experiment_completion: dict[str, Any] | None = None
    if target_status == "success" and record.run_type == "task_run":
        from app.services.experiment_service import materialize_task_experiment_completion

        experiment_completion = materialize_task_experiment_completion(
            session,
            ctx,
            record,
            completion_receipt,
        )
    import_batch_completion: dict[str, Any] | None = None
    if is_audio_import:
        from app.services.audio_import_completion_service import (
            materialize_audio_import_completion,
        )

        import_batch_completion = materialize_audio_import_completion(
            session,
            ctx,
            record,
            completion_receipt,
        )
        if import_batch_completion is not None and import_batch_completion["status"] == "failed":
            target_status = "failed"
        completion_receipt["business_status"] = target_status
        transition_run(record, target_status, reason=completion_reason)
    record.payload = {
        **record.payload,
        "status": target_status,
        "business_status": "failed" if target_status == "failed" else "completed",
        "dispatch_state": "failed" if target_status == "failed" else "completed",
        "business_completion_required": False,
        "completion_mode": (
            "cancellation_race_completion_receipt"
            if staged_processing_state == "pending_cancel"
            else "staged_completion_receipt"
        ),
        "completion_receipt": completion_receipt,
        "result_ref": persisted_result_ref,
        "metrics": payload.get("metrics") or {},
        "registered_storage_objects": registered_storage_objects,
        "experiment_completion": experiment_completion,
        "import_batch": import_batch_completion,
        "next_actions": [
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": (
                    f"traces/{completion_root_trace_id}"
                    if is_audio_import
                    else f"traces/{record.trace_id}"
                ),
            },
            *(
                [
                    {
                        "key": "view_import_batch",
                        "label": "查看同步批次",
                        "route": (f"import-batches/{import_batch_completion['import_batch_id']}"),
                    },
                    *(
                        [
                            {
                                "key": "view_audio_session",
                                "label": "查看新会话",
                                "route": (
                                    "audio-sessions/"
                                    f"{import_batch_completion['audio_session_ids'][0]}"
                                ),
                            }
                        ]
                        if import_batch_completion.get("audio_session_ids")
                        else []
                    ),
                ]
                if import_batch_completion is not None
                else [
                    {
                        "key": "view_result" if target_status == "success" else "retry",
                        "label": ("查看结果" if target_status == "success" else "创建重试运行"),
                    }
                ]
            ),
            *(
                [{"key": "retry", "label": "重试失败项"}]
                if target_status == "failed" and import_batch_completion is not None
                else []
            ),
        ],
    }
    if target_status == "failed":
        audio_import_items_failed = bool(
            is_audio_import
            and import_batch_completion is not None
            and import_batch_completion["status"] == "failed"
            and completion_receipt["status"] == "success"
        )
        record.terminal_reason = str(
            payload.get("error_code")
            or (
                "AUDIO_IMPORT_ALL_ITEMS_FAILED"
                if audio_import_items_failed
                else "DAGSTER_COMPLETION_REPORTED_FAILURE"
            )
        )
        record.payload = {
            **record.payload,
            "error": payload.get("note")
            or (
                "所有音频导入项均失败"
                if audio_import_items_failed
                else "completion receipt reported failure"
            ),
            "error_code": record.terminal_reason,
            "retryable": bool(payload.get("retryable", True)),
        }
    if target_status == "success" and record.run_type == "audio_intelligence":
        assert audio_domain_result is not None
        materialized_outputs = materialize_audio_intelligence_completion(
            session,
            ctx,
            record,
            completion_receipt,
            validated_result_ref=audio_domain_result,
        )
        record.payload = {
            **record.payload,
            "completion_receipt": completion_receipt,
            "materialized_outputs": materialized_outputs,
        }
    record_agent_completion(session, record, completion_receipt)
    record_audit(
        session,
        ctx,
        action=f"{record.run_type}.completion_received",
        object_type=record.run_type,
        object_id=record.run_id,
        result=target_status,
        after=public_run_payload(record.payload),
        trace_id=record.trace_id,
    )
    response = envelope(run_payload(record), ctx)
    _finalize_completion_receipt(
        receipt,
        adapter=adapter,
        external_id=external_id,
        completion_status=target_status,
        response=response,
    )
    if record.run_type == "task_run":
        from app.services.task_run_control_service import emit_task_run_terminal_event

        emit_task_run_terminal_event(
            session,
            ctx,
            record,
            reason=completion_reason,
        )
    return True


def resolve_cancellation_race_dagster_completion(
    session: Session,
    record: RunRecord,
    *,
    observed_status: str,
) -> bool:
    """Resolve a signed success receipt staged behind a cancellation fence."""

    normalized = observed_status.upper()
    if normalized == "SUCCESS":
        return apply_staged_dagster_completion(
            session,
            record,
            processing_states=("pending_cancel",),
        )
    if normalized not in {"CANCELED", "CANCELLED", "FAILURE"}:
        return False

    receipt = session.scalar(
        select(RunCompletionReceipt)
        .where(
            RunCompletionReceipt.tenant_id == record.tenant_id,
            RunCompletionReceipt.project_id == record.project_id,
            RunCompletionReceipt.run_id == record.run_id,
            RunCompletionReceipt.processing_state == "pending_cancel",
        )
        .with_for_update()
    )
    if receipt is None:
        return False
    code = (
        "RUN_COMPLETION_CANCELLED_BEFORE_APPLY"
        if normalized in {"CANCELED", "CANCELLED"}
        else "RUN_COMPLETION_ENGINE_FAILED_BEFORE_APPLY"
    )
    message = (
        "Dagster cancellation completed before the staged success receipt could be applied"
        if normalized in {"CANCELED", "CANCELLED"}
        else "Dagster failed before the staged success receipt could be applied"
    )
    receipt.processing_state = "rejected"
    receipt.completion_status = "rejected"
    receipt.status_code = 409
    receipt.response_json = {
        "error": {
            "code": code,
            "message": message,
            "status": 409,
            "trace_id": receipt.request_trace_id,
        }
    }
    receipt.completed_at = datetime.now(UTC)
    ctx = RequestContext(
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        user_id=f"ext:{(receipt.signature_key_id or 'dagster')[:48]}",
        roles=("system", "external_completion_client"),
        request_id=receipt.request_id,
        trace_id=receipt.request_trace_id,
        idempotency_key=f"dagster-completion:{receipt.external_id or record.run_id}",
        parent_trace_id=record.trace_id,
        correlation_id=record.trace_id,
        actor_kind="service",
    )
    record_audit(
        session,
        ctx,
        action="task_run.completion_rejected",
        object_type="task_run",
        object_id=record.run_id,
        result="failed",
        after={
            "code": code,
            "completion_receipt_id": receipt.completion_receipt_id,
            "observed_engine_status": normalized,
        },
        trace_id=record.trace_id,
    )
    return True


def _validate_experiment_bundle_execution(
    record: RunRecord,
    payload: dict[str, Any],
) -> None:
    expected = str(record.payload.get("expected_executed_bundle_sha256") or "")
    if record.run_type != "task_run" or not record.payload.get("experiment_id") or not expected:
        return
    result_ref = payload.get("result_ref")
    actual = str(
        result_ref.get("executed_task_version_binding_sha256")
        if isinstance(result_ref, dict)
        else ""
    )
    if len(actual) != 64:
        raise ApiError(
            "EXPERIMENT_EXECUTED_BUNDLE_PROOF_REQUIRED",
            "实验成功回执必须携带实际执行的 TaskVersion bundle 内容证明",
            409,
            details=[{"expected_executed_bundle_sha256": expected}],
        )
    if not hmac.compare_digest(actual, expected):
        raise ApiError(
            "EXPERIMENT_EXECUTED_BUNDLE_MISMATCH",
            "实验回执中的实际执行 bundle 与分配版本不一致",
            409,
            details=[
                {
                    "expected_executed_bundle_sha256": expected,
                    "actual_executed_bundle_sha256": actual,
                }
            ],
        )


async def complete_run_from_receipt(
    session: Session,
    ctx: RequestContext,
    request: Request,
    run_id: str,
    payload: dict[str, Any],
    *,
    response_data: Callable[[RunRecord], dict[str, Any]] | None = None,
    strict_external_receipt: bool = False,
    completion_auth: dict[str, Any] | None = None,
) -> dict[str, Any] | JSONResponse:
    authenticated_source: str | None = None
    if strict_external_receipt:
        require_any_role(
            ctx,
            ("external_completion_client",),
            action="runs.external_complete",
        )
        authenticated_source = _require_signed_completion_auth(ctx, completion_auth)
    else:
        if get_settings().app_env not in {"local", "test", "ci"}:
            raise ApiError(
                "MANUAL_COMPLETION_RECEIPT_DISABLED",
                "生产环境只接受签名外部完成回执",
                403,
            )
        require_any_role(ctx, RUN_COMPLETION_ROLES, action="runs.complete")
    body_hash = await request_hash(request)
    raw_request_body = await request.json()
    receipt_request_body = raw_request_body if isinstance(raw_request_body, dict) else payload
    operation = f"complete:run:{run_id}"
    record = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if not record:
        raise ApiError("NOT_FOUND", f"运行不存在：{run_id}", 404)
    if (
        record.run_type == "release_command"
        and not strict_external_receipt
        and "system" not in ctx.roles
    ):
        raise ApiError(
            "RELEASE_COMMAND_COMPLETION_SYSTEM_ONLY",
            "发布执行回执只能由本地 system 测试身份或生产签名执行器提交",
            403,
        )

    stage_early_receipt = _stageable_early_dagster_completion(
        record,
        payload,
        strict_external_receipt=strict_external_receipt,
        authenticated_source=authenticated_source,
    )
    stage_cancellation_receipt = _stageable_cancelling_dagster_completion(
        record,
        payload,
        strict_external_receipt=strict_external_receipt,
        authenticated_source=authenticated_source,
    )
    _claim_completion_nonce(session, ctx, completion_auth)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        public_replay = public_run_response(replay, ctx)
        raise_replayed_api_error(public_replay)
        replay_data = public_replay.get("data")
        if isinstance(replay_data, dict) and replay_data.get("receipt_state") in {
            "pending_binding",
            "pending_cancellation_resolution",
        }:
            return JSONResponse(status_code=202, content=public_replay)
        return public_replay

    if record.run_type == "task_run" and record.status in {"success", "failed", "cancelled"}:
        error = ApiError(
            "RUN_COMPLETION_NOT_ALLOWED",
            f"运行状态 {record.status} 不能接收完成回执",
            409,
            details=[{"run_id": record.run_id, "status": record.status}],
        )
        record_audit(
            session,
            ctx,
            action="task_run.completion_rejected",
            object_type="task_run",
            object_id=record.run_id,
            result="failed",
            before={"status": record.status},
            after={
                "code": error.code,
                "status": record.status,
                "completion_receipt_id": payload.get("completion_receipt_id"),
                "external_id": payload.get("external_id"),
            },
            trace_id=record.trace_id,
        )
        error_response = api_error_result(ctx, error)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=error.status_code,
            response_json=error_response,
        )
        session.commit()
        raise error

    adapter = (
        "dagster"
        if stage_early_receipt or stage_cancellation_receipt
        else _validate_completion_source_binding(
            record,
            payload,
            strict_external_receipt=strict_external_receipt,
            authenticated_source=authenticated_source,
        )
    )

    raw_receipt_id = payload.get("completion_receipt_id") or payload.get("receipt_id")
    if strict_external_receipt and not raw_receipt_id:
        raise ApiError(
            "RUN_COMPLETION_RECEIPT_ID_REQUIRED",
            "外部完成回执必须携带 completion_receipt_id",
            400,
            details=[{"run_id": run_id}],
        )
    receipt_id = str(raw_receipt_id or f"completion_{uuid.uuid4().hex[:12]}")
    receipt_hash = _completion_receipt_hash({**receipt_request_body, "run_id": run_id})
    legacy_receipt_hash = _completion_receipt_hash({**payload, "run_id": run_id})
    stored_receipt_request_body = receipt_request_body
    if record.run_type == "audio_intelligence":
        stored_receipt_request_body = dict(receipt_request_body)
        raw_result_ref = stored_receipt_request_body.get("result_ref")
        if isinstance(raw_result_ref, dict):
            stored_receipt_request_body["result_ref"] = sanitize_audio_intelligence_result(
                dict(raw_result_ref)
            )
    inbox_receipt, claimed = _claim_completion_receipt(
        session,
        ctx,
        record,
        completion_receipt_id=receipt_id,
        receipt_hash=receipt_hash,
        payload=payload,
        request_body=stored_receipt_request_body,
        completion_auth=completion_auth,
        adapter=adapter,
        authenticated_source=authenticated_source,
    )
    if not claimed:
        return _replay_claimed_completion_receipt(
            session,
            ctx,
            inbox_receipt,
            run_id=run_id,
            completion_receipt_id=receipt_id,
            receipt_hash=receipt_hash,
            operation=operation,
            body_hash=body_hash,
        )

    if stage_early_receipt or stage_cancellation_receipt:
        if (
            stage_early_receipt
            and record.run_type == "audio_intelligence"
            and str(payload.get("status") or "success") == "success"
        ):
            stage_audio_completion_storage_object(
                session,
                ctx,
                record,
                payload.get("result_ref"),
                completion_receipt_id=receipt_id,
            )
        if str(payload.get("status") or "success") == "success":
            from app.services.audio_import_progress_service import (
                mark_audio_import_batch_materializing,
            )

            mark_audio_import_batch_materializing(
                session,
                ctx,
                record,
                completion_receipt_id=receipt_id,
                result_ref=payload.get("result_ref"),
            )
        public_receipt_state = (
            "pending_cancellation_resolution" if stage_cancellation_receipt else "pending_binding"
        )
        persisted_receipt_state = (
            "pending_cancel" if stage_cancellation_receipt else "pending_binding"
        )
        response = envelope(
            {
                "run_id": record.run_id,
                "status": record.status,
                "completion_receipt_id": receipt_id,
                "receipt_state": public_receipt_state,
                "trace_id": record.trace_id,
            },
            ctx,
        )
        inbox_receipt.processing_state = persisted_receipt_state
        inbox_receipt.status_code = 202
        inbox_receipt.response_json = _json_copy(response)
        record_audit(
            session,
            ctx,
            action=(
                "task_run.completion_staged_during_cancellation"
                if stage_cancellation_receipt
                else f"{record.run_type}.completion_staged"
            ),
            object_type=record.run_type,
            object_id=record.run_id,
            result=persisted_receipt_state,
            after={
                "completion_receipt_id": receipt_id,
                "external_id": payload.get("external_id"),
                "receipt_state": public_receipt_state,
            },
            trace_id=record.trace_id,
        )
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=202,
            response_json=response,
        )
        session.commit()
        return JSONResponse(status_code=202, content=response)

    completion_root_trace_id = _link_completion_trace_to_run_root(session, ctx, record)

    # Lazily backfill a receipt persisted in the legacy run payload before the
    # dedicated inbox table existed.
    existing_receipt = record.payload.get("completion_receipt")
    if isinstance(existing_receipt, dict):
        if existing_receipt.get("completion_receipt_id") == receipt_id:
            if existing_receipt.get("receipt_hash") not in {
                receipt_hash,
                legacy_receipt_hash,
            }:
                raise ApiError(
                    "RUN_COMPLETION_RECEIPT_CONFLICT",
                    "同一个 completion_receipt_id 不能复用到不同完成结果",
                    409,
                    details=[{"run_id": run_id, "completion_receipt_id": receipt_id}],
                )
            _sync_eval_run_projection(session, ctx, record)
            response = envelope((response_data or run_payload)(record), ctx)
            _finalize_completion_receipt(
                inbox_receipt,
                adapter=str(existing_receipt.get("adapter") or inbox_receipt.adapter),
                external_id=str(existing_receipt.get("external_id") or ""),
                completion_status=str(existing_receipt.get("status") or record.status),
                response=response,
            )
            save_idempotency_result(
                session,
                ctx,
                operation=operation,
                body_hash=body_hash,
                status_code=200,
                response_json=response,
            )
            session.commit()
            return response
        raise ApiError(
            "RUN_ALREADY_COMPLETED",
            "运行已接收完成回执，不能被另一条回执覆盖",
            409,
            details=[
                {
                    "run_id": run_id,
                    "existing_completion_receipt_id": existing_receipt.get("completion_receipt_id"),
                }
            ],
        )

    adapter, external_id = _validate_completion_receipt(
        record,
        payload,
        strict_external_receipt=strict_external_receipt,
        authenticated_source=authenticated_source,
    )
    status = str(payload.get("status") or "success")
    target_status = "failed" if status == "failed" else "success"
    if target_status == "success":
        _validate_experiment_bundle_execution(record, payload)
    audio_domain_result: dict[str, Any] | None = None
    if target_status == "success" and record.run_type == "audio_intelligence":
        audio_domain_result = resolve_audio_intelligence_result(record, payload.get("result_ref"))
    label_eval_result: dict[str, Any] | None = None
    if target_status == "success" and record.run_type == "eval_run":
        from app.services.label_eval_result_service import materialize_label_eval_completion

        label_eval_result = materialize_label_eval_completion(
            session,
            ctx,
            record,
            {
                "result_ref": payload.get("result_ref") or {},
                "metrics": payload.get("metrics") or {},
            },
        )
        if label_eval_result is not None and label_eval_result["status"] == "blocked":
            target_status = "blocked"
    registered_storage_objects: list[dict[str, Any]] = []
    if target_status == "success":
        registered_storage_objects = register_hotword_completion_storage_objects(
            session,
            ctx,
            record,
            payload.get("result_ref"),
        )
    release_command_result: dict[str, Any] | None = None
    if record.run_type == "release_command":
        from app.services.prompt_release_service import (
            materialize_release_command_completion,
            materialize_release_command_failure,
        )

        release_receipt = {
            "completion_receipt_id": receipt_id,
            "source": authenticated_source or payload.get("source") or adapter,
            "result_ref": payload.get("result_ref") or {},
            "error_code": payload.get("error_code"),
            "note": payload.get("note"),
        }
        release_command_result = (
            materialize_release_command_completion(session, ctx, record, release_receipt)
            if target_status == "success"
            else materialize_release_command_failure(session, ctx, record, release_receipt)
        )
        if release_command_result.get("status") == "blocked":
            target_status = "blocked"
    raw_completion_result_ref = payload.get("result_ref") or {}
    persisted_completion_result_ref = raw_completion_result_ref
    if record.run_type == "audio_intelligence" and isinstance(raw_completion_result_ref, dict):
        persisted_completion_result_ref = sanitize_audio_intelligence_result(
            raw_completion_result_ref
        )
    is_audio_import = (
        record.run_type == "task_run"
        and record.payload.get("execution_contract") == "auris-flow-audio-import-v1"
    )
    if target_status == "success":
        from app.services.audio_import_progress_service import (
            mark_audio_import_batch_materializing,
        )

        mark_audio_import_batch_materializing(
            session,
            ctx,
            record,
            completion_receipt_id=receipt_id,
            result_ref=raw_completion_result_ref,
        )
    if not is_audio_import:
        transition_run(record, target_status, reason=f"{adapter}_completion_received")
    completion_receipt: dict[str, Any] = {
        "completion_receipt_id": receipt_id,
        "receipt_hash": receipt_hash,
        "adapter": adapter,
        "external_id": external_id,
        "source": authenticated_source or payload.get("source") or adapter,
        "status": target_status,
        "result_ref": persisted_completion_result_ref,
        "metrics": payload.get("metrics") or {},
        "note": payload.get("note"),
        "error_code": payload.get("error_code"),
        "retryable": bool(payload.get("retryable", True)),
        "received_at": datetime.now(UTC).isoformat(),
        "trace_id": ctx.trace_id,
        "run_trace_id": record.trace_id,
        "root_trace_id": completion_root_trace_id,
        "auth": completion_auth or {"auth_mode": "project_admin_manual"},
        "registered_storage_objects": registered_storage_objects,
    }
    experiment_completion: dict[str, Any] | None = None
    if target_status == "success" and record.run_type == "task_run":
        from app.services.experiment_service import materialize_task_experiment_completion

        experiment_completion = materialize_task_experiment_completion(
            session,
            ctx,
            record,
            completion_receipt,
        )
    import_batch_completion: dict[str, Any] | None = None
    if is_audio_import:
        from app.services.audio_import_completion_service import (
            materialize_audio_import_completion,
        )

        import_batch_completion = materialize_audio_import_completion(
            session,
            ctx,
            record,
            completion_receipt,
        )
        if import_batch_completion is not None and import_batch_completion["status"] == "failed":
            target_status = "failed"
        completion_receipt["business_status"] = target_status
        transition_run(record, target_status, reason=f"{adapter}_completion_received")
    record.payload = {
        **record.payload,
        "status": target_status,
        "business_status": (
            "failed"
            if target_status == "failed"
            else "blocked"
            if target_status == "blocked"
            else "completed"
        ),
        "dispatch_state": (
            "completed"
            if target_status == "success"
            else "blocked"
            if target_status == "blocked"
            else "failed"
        ),
        "business_completion_required": False,
        "completion_mode": "completion_receipt",
        "completion_receipt": completion_receipt,
        "result_ref": persisted_completion_result_ref or record.payload.get("result_ref") or {},
        "metrics": payload.get("metrics") or record.payload.get("metrics") or {},
        "registered_storage_objects": registered_storage_objects,
        "label_eval_result": label_eval_result,
        "release_command_result": release_command_result,
        "experiment_completion": experiment_completion,
        "import_batch": import_batch_completion,
        "next_actions": [
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": (
                    f"traces/{completion_root_trace_id}"
                    if is_audio_import
                    else f"traces/{record.trace_id}"
                ),
            },
            *(
                [
                    {
                        "key": "view_import_batch",
                        "label": "查看同步批次",
                        "route": (f"import-batches/{import_batch_completion['import_batch_id']}"),
                    },
                    *(
                        [
                            {
                                "key": "view_audio_session",
                                "label": "查看新会话",
                                "route": (
                                    "audio-sessions/"
                                    f"{import_batch_completion['audio_session_ids'][0]}"
                                ),
                            }
                        ]
                        if import_batch_completion.get("audio_session_ids")
                        else []
                    ),
                ]
                if import_batch_completion is not None
                else [{"key": "view_result", "label": "查看结果"}]
            ),
        ]
        if target_status == "success"
        else [
            *(
                [
                    {
                        "key": "view_import_batch",
                        "label": "查看同步批次",
                        "route": (f"import-batches/{import_batch_completion['import_batch_id']}"),
                    }
                ]
                if import_batch_completion is not None
                else []
            ),
            {
                "key": "review_eval_gates" if target_status == "blocked" else "retry",
                "label": (
                    "查看评测阻断项"
                    if target_status == "blocked"
                    else "重试失败项"
                    if is_audio_import
                    else "创建重试运行"
                ),
            },
            {
                "key": "view_trace",
                "label": "查看 Trace",
                "route": (
                    f"traces/{completion_root_trace_id}"
                    if is_audio_import
                    else f"traces/{record.trace_id}"
                ),
            },
        ],
    }
    if adapter == "external_callback":
        callback_receipt = session.get(ExternalCallbackReceipt, external_id)
        if (
            callback_receipt
            and callback_receipt.tenant_id == record.tenant_id
            and callback_receipt.project_id == record.project_id
            and isinstance(callback_receipt.payload, dict)
            and callback_receipt.payload.get("run_id") == record.run_id
        ):
            callback_receipt.payload = {
                **callback_receipt.payload,
                "completion_ack": {
                    "run_id": record.run_id,
                    "completion_receipt_id": completion_receipt["completion_receipt_id"],
                    "status": target_status,
                    "result_ref": completion_receipt["result_ref"],
                    "metrics": completion_receipt["metrics"],
                    "received_at": completion_receipt["received_at"],
                },
            }
    if target_status == "failed":
        audio_import_items_failed = bool(
            is_audio_import
            and import_batch_completion is not None
            and import_batch_completion["status"] == "failed"
            and completion_receipt["status"] == "success"
        )
        record.payload = {
            **record.payload,
            "error": payload.get("note")
            or (
                "所有音频导入项均失败"
                if audio_import_items_failed
                else "completion receipt reported failure"
            ),
            "error_code": payload.get("error_code")
            or (
                "AUDIO_IMPORT_ALL_ITEMS_FAILED"
                if audio_import_items_failed
                else "COMPLETION_RECEIPT_FAILED"
            ),
            "retryable": bool(payload.get("retryable", True)),
        }
    if target_status == "success" and record.run_type == "audio_intelligence":
        assert audio_domain_result is not None
        materialized_outputs = materialize_audio_intelligence_completion(
            session,
            ctx,
            record,
            completion_receipt,
            validated_result_ref=audio_domain_result,
        )
        record.payload = {
            **record.payload,
            "completion_receipt": completion_receipt,
            "materialized_outputs": materialized_outputs,
        }
    if target_status == "success" and record.run_type == "label_extraction":
        from app.services.label_closed_loop_service import (
            materialize_label_extraction_completion,
        )

        label_observations = materialize_label_extraction_completion(
            session, ctx, record, completion_receipt
        )
        record.payload = {
            **record.payload,
            "materialized_observation_ids": [item["observation_id"] for item in label_observations],
            "materialized_observation_count": len(label_observations),
        }
    if target_status == "success" and record.run_type in {"asset_backfill", "asset_check_retry"}:
        materialization_receipt = completion_receipt
        if registered_storage_objects:
            materialization_result_ref = dict(completion_receipt["result_ref"])
            materialization_result_ref.pop("storage_objects", None)
            materialization_receipt = {
                **completion_receipt,
                "result_ref": materialization_result_ref,
            }
        materialized_assets = materialize_asset_completion(
            session,
            ctx,
            record,
            materialization_receipt,
        )
        record.payload = {**record.payload, "materialized_assets": materialized_assets}
    if target_status == "success" and record.run_type == "hotword_analysis":
        from app.services.hotword_service import materialize_hotword_analysis_completion

        hotword_metrics = materialize_hotword_analysis_completion(
            session, ctx, record, completion_receipt
        )
        record.payload = {**record.payload, "hotword_metrics": hotword_metrics}
    if target_status == "success" and record.run_type == "hotword_build":
        from app.services.hotword_service import materialize_hotword_build_completion

        hotword_build = materialize_hotword_build_completion(
            session, ctx, record, completion_receipt
        )
        record.payload = {**record.payload, "hotword_build": hotword_build}
    if target_status == "success" and record.run_type == "hotword_eval":
        from app.services.hotword_service import materialize_hotword_eval_completion

        hotword_eval = materialize_hotword_eval_completion(session, ctx, record, completion_receipt)
        record.payload = {**record.payload, "hotword_eval": hotword_eval}
    if target_status == "success" and record.run_type == "hotword_publish":
        from app.services.hotword_service import materialize_hotword_publish_completion

        hotword_publish = materialize_hotword_publish_completion(
            session, ctx, record, completion_receipt
        )
        record.payload = {**record.payload, "hotword_publish": hotword_publish}
    if target_status == "success" and record.run_type == "scene_profile_generation":
        from app.services.scene_profile_service import (
            materialize_scene_profile_generation_completion,
        )

        scene_profile_version = materialize_scene_profile_generation_completion(
            session,
            record,
            completion_receipt,
        )
        if scene_profile_version is not None:
            record.payload = {
                **record.payload,
                "scene_profile_version_id": scene_profile_version.scene_profile_version_id,
                "scene_profile_manifest_sha256": scene_profile_version.manifest_sha256,
                "candidate_status": scene_profile_version.status,
            }
    insight_completion = materialize_insight_completion(session, ctx, record, completion_receipt)
    if insight_completion:
        record.payload = {**record.payload, "insight_completion": insight_completion}
    record_agent_completion(session, record, completion_receipt)
    optimization_prompt_candidates = materialize_optimization_prompt_candidates(
        session,
        ctx,
        record,
        completion_receipt,
    )
    if optimization_prompt_candidates:
        record.payload = {
            **record.payload,
            "result_ref": {
                **(record.payload.get("result_ref") or {}),
                "prompt_candidate_ids": [
                    candidate.candidate_id for candidate in optimization_prompt_candidates
                ],
            },
        }
    prompt_candidate = materialize_prompt_candidate(session, record, completion_receipt)
    if prompt_candidate:
        record.payload = {
            **record.payload,
            "prompt_candidate_id": prompt_candidate.candidate_id,
            "prompt_candidate_status": prompt_candidate.status,
            "result_ref": {
                **(record.payload.get("result_ref") or {}),
                "prompt_candidate_id": prompt_candidate.candidate_id,
            },
        }
    _sync_eval_run_projection(session, ctx, record)
    record_audit(
        session,
        ctx,
        action=f"{record.run_type}.completion_received",
        object_type=record.run_type,
        object_id=record.run_id,
        result=target_status,
        after=record.payload,
        trace_id=record.trace_id,
    )
    if record.run_type == "task_run":
        from app.services.task_run_control_service import emit_task_run_terminal_event

        emit_task_run_terminal_event(
            session,
            ctx,
            record,
            reason=f"{adapter}_completion_received",
        )
    response = envelope((response_data or run_payload)(record), ctx)
    _finalize_completion_receipt(
        inbox_receipt,
        adapter=adapter,
        external_id=external_id,
        completion_status=target_status,
        response=response,
    )
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=200,
        response_json=response,
    )
    session.commit()
    log_event(
        logger,
        "run.completion_received",
        ctx=ctx,
        run_type=record.run_type,
        run_id=record.run_id,
        target_status=target_status,
        adapter=adapter,
        external_id=external_id,
    )
    return response


def retry_payload_from_record(
    record: RunRecord, ctx: RequestContext, payload: dict[str, Any]
) -> dict[str, Any]:
    excluded = RUN_SYSTEM_PAYLOAD_KEYS | RUN_FAILURE_SIMULATION_KEYS
    base_payload = {key: value for key, value in record.payload.items() if key not in excluded}
    raw_overrides = payload.get("payload_overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ApiError(
            "VALIDATION_ERROR",
            "payload_overrides 必须是对象",
            422,
            details=[{"field": "payload_overrides", "message": "must be an object"}],
        )
    overrides = {key: value for key, value in raw_overrides.items() if key not in excluded}
    if record.run_type in {
        "hotword_build",
        "hotword_eval",
        "hotword_publish",
        "hotword_rollback",
    }:
        frozen_overrides = sorted(set(overrides) & HOTWORD_FROZEN_RETRY_KEYS)
        if frozen_overrides:
            raise ApiError(
                "HOTWORD_RETRY_BINDING_OVERRIDE_FORBIDDEN",
                "热词构建、评测、发布或回滚重试必须继承原运行冻结绑定",
                409,
                details=[{"fields": frozen_overrides}],
            )
    semantic_overrides = sorted(set(overrides) - RUN_SAFE_RETRY_OVERRIDE_KEYS)
    if semantic_overrides:
        raise ApiError(
            "RUN_RETRY_SEMANTIC_OVERRIDE_FORBIDDEN",
            "运行重试必须继承原运行冻结的业务输入，只允许调整调度参数",
            409,
            details=[
                {
                    "fields": semantic_overrides,
                    "allowed_fields": sorted(RUN_SAFE_RETRY_OVERRIDE_KEYS),
                }
            ],
        )

    retry_source = {"type": "run_record", "id": record.run_id, "status": record.status}
    affected_objects = list(base_payload.get("affected_objects", []))
    if retry_source not in affected_objects:
        affected_objects.append(retry_source)

    source_event_id = record.payload.get("dead_letter_event_id") or record.payload.get(
        "failed_event_id"
    )
    retry_reason = payload.get("reason") or "manual retry"
    retry_payload = {
        **base_payload,
        **overrides,
        "affected_objects": affected_objects,
        "retry_of_run_id": record.run_id,
        "retry_of_event_id": source_event_id,
        "retry_of_trace_id": record.trace_id,
        "retry_reason": retry_reason,
        "retry_attempt": max(
            int(record.payload.get("retry_attempt") or 0),
            int(record.payload.get("retry_count") or 0),
        )
        + 1,
        "trigger_type": "retry",
        "run_key": overrides.get("run_key")
        or f"{record.run_key or record.run_id}:retry:{ctx.request_id}",
    }
    return retry_payload


def _insight_report_retry_hook(
    session: Session,
    ctx: RequestContext,
    source_record: RunRecord,
    *,
    reason: str,
) -> Callable[[RunRecord], None] | None:
    if source_record.run_type != "insight_report":
        return None

    report = session.scalar(
        select(InsightReport)
        .where(
            InsightReport.run_id == source_record.run_id,
            InsightReport.tenant_id == ctx.tenant_id,
            InsightReport.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if report is None:
        raise ApiError(
            "INSIGHT_REPORT_PROJECTION_MISSING",
            "报告重试缺少与原运行绑定的强投影",
            409,
            details=[{"run_id": source_record.run_id}],
        )
    if report.status != "failed":
        raise ApiError(
            "INSIGHT_REPORT_NOT_RETRYABLE",
            "只有失败的报告投影可以重新绑定重试运行",
            409,
            details=[{"report_id": report.report_id, "status": report.status}],
        )

    def prepare_retry_record(new_record: RunRecord) -> None:
        previous_payload = report.payload if isinstance(report.payload, dict) else {}
        report_document = rebind_report_document(
            dict(new_record.payload.get("report_document") or {}),
            run_id=new_record.run_id,
            trace_id=new_record.trace_id,
        )
        new_record.payload = {**new_record.payload, "report_document": report_document}
        raw_history = previous_payload.get("retry_history")
        retry_history = list(raw_history) if isinstance(raw_history, list) else []
        retry_history.append(
            {
                "from_run_id": source_record.run_id,
                "to_run_id": new_record.run_id,
                "reason": reason,
                "requested_at": datetime.now(UTC).isoformat(),
                "trace_id": new_record.trace_id,
            }
        )
        clean_payload = {
            key: value
            for key, value in previous_payload.items()
            if key
            not in {
                "completed_at",
                "completion_receipt_id",
                "failure",
                "metrics",
                "result_ref",
            }
        }
        report.run_id = new_record.run_id
        report.status = "generating"
        report.trace_id = new_record.trace_id
        report.payload = {
            **clean_payload,
            "run_id": new_record.run_id,
            "status": "generating",
            "trace_id": new_record.trace_id,
            "report_document": report_document,
            "retry_of_run_id": source_record.run_id,
            "retry_history": retry_history,
        }
        upsert_resource(
            session,
            ctx,
            "insight_reports",
            report.report_id,
            report_payload(report),
            status="generating",
            trace_id=new_record.trace_id,
            audit_action="insight_report.retry_rebound",
        )

    return prepare_retry_record


async def create_run(
    session: Session,
    ctx: RequestContext,
    request: Request,
    *,
    run_type: str,
    event_type: str,
    payload: dict[str, Any],
    status: str = "pending",
    idempotency_operation: str | None = None,
    prepare_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    prepare_record: Callable[[RunRecord], None] | None = None,
    run_trace_id: str | None = None,
) -> dict[str, Any]:
    validate_initial_run_status(status)
    reject_runtime_failure_injection(payload)
    require_any_role(
        ctx,
        RUN_CREATE_ROLE_POLICY.get(run_type, ("project_admin",)),
        action=f"{run_type}.create",
    )
    body_hash = await request_hash(request)
    operation = idempotency_operation or f"create:{run_type}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return public_run_response(replay, ctx)
    if prepare_payload is not None:
        payload = prepare_payload(payload)

    if run_type == "task_run":
        # Task-run lifecycle controls are server-owned even for internal retries.
        # Public requests also use a strict schema, but stripping here prevents a
        # future internal caller from copying stale or forged monitor state.
        payload = {
            key: value
            for key, value in payload.items()
            if key not in {"deadline_at", "next_status_sync_at", "monitor_generation"}
        }

    run_id = (
        payload.get("run_id") or payload.get("task_run_id") or f"{run_type}_{uuid.uuid4().hex[:12]}"
    )
    # New RunRecord/Outbox pairs trace the current request; an explicit override
    # is reserved for retries that continue their source run's causal trace. A
    # long-lived domain lineage stays in payload.root_trace_id and must not
    # implicitly replace either trace.
    trace_id = run_trace_id or ctx.trace_id
    now = datetime.now(UTC)
    deadline_at = (
        now + timedelta(seconds=get_settings().task_run_default_deadline_seconds)
        if run_type == "task_run"
        else None
    )
    log_event(
        logger,
        "run.create.start",
        ctx=ctx,
        run_type=run_type,
        run_id=run_id,
        event_type=event_type,
        status=status,
    )
    record = RunRecord(
        run_id=run_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=run_type,
        status=status,
        run_key=payload.get("run_key")
        or payload.get("task_version_id")
        or payload.get("dataset_id"),
        partition_key=payload.get("partition_key") or payload.get("sample_set"),
        trace_id=trace_id,
        deadline_at=deadline_at,
        next_status_sync_at=None,
        monitor_generation=0,
        created_at=now,
        updated_at=now,
        payload={
            **payload,
            "run_id": run_id,
            "status": status,
            "trace_id": trace_id,
            **(
                {
                    "deadline_at": deadline_at.isoformat(),
                    "next_status_sync_at": None,
                    "monitor_generation": 0,
                }
                if deadline_at is not None
                else {}
            ),
            "affected_objects": payload.get("affected_objects", []),
            "next_actions": payload.get(
                "next_actions",
                [{"key": "view_trace", "label": "查看 Trace", "route": f"traces/{trace_id}"}],
            ),
        },
    )
    session.add(record)
    # Causal projections may have composite foreign keys to this run. Flush the
    # parent first so SQLAlchemy and MySQL cannot reorder child inserts ahead of it.
    session.flush()
    agent_projection = create_agent_execution_projection(
        session, ctx, record, event_type=event_type
    )
    if agent_projection:
        record.payload = {**record.payload, **agent_projection}
    if prepare_record is not None:
        prepare_record(record)
    _sync_eval_run_projection(session, ctx, record)
    event_ctx = (
        ctx
        if trace_id == ctx.trace_id
        else replace(
            ctx,
            trace_id=trace_id,
            parent_trace_id=ctx.trace_id,
            correlation_id=ctx.correlation_id or trace_id,
        )
    )
    event = enqueue_event(
        session,
        event_ctx,
        event_type=event_type,
        aggregate_type=run_type,
        aggregate_id=run_id,
        payload=record.payload,
    )
    if status == "blocked":
        # A run that already requires an explicit human decision must not leave
        # a claimable event behind. Otherwise a fast worker can claim the
        # pre-decision snapshot while the approver concurrently requeues the
        # same event, allowing stale gate state to overwrite the approval.
        # The decision transaction is the sole path that moves this event back
        # to pending/ready through _reset_outbox_for_approval().
        event.status = "blocked"
        event.delivery_state = "confirmed"
        event.processed_at = now
        event.last_error = "run is blocked by release gate or human confirmation"
    record_audit(
        session,
        ctx,
        action=f"{run_type}.create",
        object_type=run_type,
        object_id=run_id,
        after=record.payload,
        trace_id=trace_id,
    )
    response = envelope(run_payload(record), ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response_json=response,
    )
    session.commit()
    log_event(
        logger,
        "run.create.committed",
        ctx=ctx,
        run_type=run_type,
        run_id=run_id,
        event_type=event_type,
        status=status,
    )
    return response


async def retry_run(
    session: Session,
    ctx: RequestContext,
    request: Request,
    run_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    # Reject actors that cannot retry any run type before reading scoped run
    # existence or state. The concrete run type still passes its narrower
    # create policy after lookup.
    require_any_role(ctx, RUN_RETRY_ENTRY_ROLES, action="runs.retry")
    actor_roles = set(ctx.roles)
    allowed_run_types = tuple(
        run_type
        for run_type in RUN_EVENT_TYPES
        if "system" in actor_roles
        or actor_roles.intersection(RUN_CREATE_ROLE_POLICY.get(run_type, ("project_admin",)))
    )
    record = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
            RunRecord.run_type.in_(allowed_run_types),
        )
    )
    if not record:
        raise ApiError("NOT_FOUND", f"运行不存在：{run_id}", 404)
    is_audio_import_retry = (
        record.run_type == "task_run"
        and record.payload.get("execution_contract") == "auris-flow-audio-import-v1"
    )
    partial_audio_import_retry = False
    if is_audio_import_retry and record.status == "success":
        import_batch_id = str(record.payload.get("import_batch_id") or "").strip()
        source_batch = session.scalar(
            select(ImportBatch)
            .where(
                ImportBatch.import_batch_id == import_batch_id,
                ImportBatch.task_run_id == record.run_id,
                ImportBatch.tenant_id == ctx.tenant_id,
                ImportBatch.project_id == ctx.project_id,
            )
            .with_for_update()
        )
        partial_audio_import_retry = bool(
            source_batch is not None
            and source_batch.status == "partial"
            and source_batch.current_stage == "completed"
        )
    if record.status not in RUN_RETRYABLE_STATUSES and not partial_audio_import_retry:
        raise ApiError(
            "RUN_NOT_RETRYABLE",
            f"运行状态 {record.status} 不能重试",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "status": record.status,
                    "allowed_statuses": sorted(RUN_RETRYABLE_STATUSES),
                }
            ],
        )
    event_type = RUN_EVENT_TYPES.get(record.run_type)
    if not event_type:
        raise ApiError(
            "RUN_RETRY_UNSUPPORTED",
            f"运行类型 {record.run_type} 暂不支持重试",
            409,
            details=[{"run_id": record.run_id, "run_type": record.run_type}],
        )
    if record.run_type == "eval_run" and isinstance(
        record.payload.get("insight_experiment_id"), str
    ):
        raise ApiError(
            "RUN_RETRY_REQUIRES_EXPERIMENT_COMMAND",
            "洞察实验评测必须通过实验重试命令重建因果投影，禁止通用运行重试",
            409,
            details=[
                {
                    "run_id": record.run_id,
                    "insight_experiment_id": record.payload.get("insight_experiment_id"),
                }
            ],
        )
    operation = f"retry:{record.run_type}:{record.run_id}"
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return public_run_response(replay, ctx)
    retry_payload = retry_payload_from_record(record, ctx, payload)
    retry_reason = str(retry_payload.get("retry_reason") or "manual retry")
    prepare_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    prepare_record = _insight_report_retry_hook(
        session,
        ctx,
        record,
        reason=retry_reason,
    )
    if is_audio_import_retry:
        from app.services.import_batch_service import create_import_batch_for_task_run
        from app.services.task_execution_policy import enforce_task_execution_policy

        # A failed/partial audio-import batch never advances its live cursor.
        # Re-applying the published immutable TaskVersion therefore reads the
        # same source window; external-id + checksum dedupe turns that full
        # window retry into an effective failed-item retry without trusting a
        # caller-supplied list of object locators.
        def prepare_audio_import_retry_payload(
            candidate: dict[str, Any],
        ) -> dict[str, Any]:
            return enforce_task_execution_policy(
                session,
                ctx,
                {
                    **candidate,
                    "execution_mode": "production",
                    "trigger_type": "retry",
                },
            )

        def prepare_audio_import_retry_record(retry_record: RunRecord) -> None:
            create_import_batch_for_task_run(
                session,
                ctx,
                retry_record,
            )

        prepare_payload = prepare_audio_import_retry_payload
        prepare_record = prepare_audio_import_retry_record
    log_event(
        logger,
        "run.retry.requested",
        ctx=ctx,
        run_type=record.run_type,
        source_run_id=record.run_id,
        source_status=record.status,
        retry_of_event_id=retry_payload.get("retry_of_event_id"),
    )
    return await create_run(
        session,
        ctx,
        request,
        run_type=record.run_type,
        event_type=event_type,
        payload=retry_payload,
        status="pending",
        idempotency_operation=operation,
        prepare_payload=prepare_payload,
        prepare_record=prepare_record,
        # Audio-import retries create a new root trace and retain causal
        # linkage through retry_of_trace_id. Other legacy retries continue
        # their source trace for backward compatibility.
        run_trace_id=None if is_audio_import_retry else record.trace_id,
    )


def get_run(session: Session, ctx: RequestContext, run_id: str) -> dict[str, Any]:
    record = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
        )
    )
    if not record:
        raise ApiError("NOT_FOUND", f"运行不存在：{run_id}", 404)
    return run_payload(record)


def list_runs(
    session: Session,
    ctx: RequestContext,
    *,
    run_type: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    records = RunRecordRepository(session).list(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=run_type,
        status=status,
        limit=100,
    )
    return [run_payload(record) for record in records]


def list_run_page(
    session: Session,
    ctx: RequestContext,
    page: dict[str, str | int | None],
    *,
    run_type: str | None = None,
    status: str | None = None,
) -> RunPage:
    limit = page_limit(page)
    cursor_created_at, cursor_run_id = decode_run_cursor(page.get("cursor"))
    repo = RunRecordRepository(session)
    records = repo.list(
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        run_type=run_type,
        status=status,
        cursor_created_at=cursor_created_at,
        cursor_run_id=cursor_run_id,
        limit=limit + 1,
    )
    visible = records[:limit]
    return RunPage(
        items=[run_payload(record) for record in visible],
        total=repo.count(
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            run_type=run_type,
            status=status,
        ),
        limit=limit,
        next_cursor=encode_run_cursor(visible[-1]) if len(records) > limit and visible else None,
    )
