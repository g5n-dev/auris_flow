from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import ImportBatch, RunRecord
from app.services.audit_service import record_audit

AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
ACTIVE_RUN_STATUSES = frozenset({"submitted", "running", "completion_pending"})
TERMINAL_BATCH_STATUSES = frozenset({"partial", "succeeded", "failed", "cancelled"})
EXPECTED_PREVIOUS_STAGE = {
    "downloading": "listing",
    "verifying": "downloading",
}


def _scoped_run(
    session: Session,
    ctx: RequestContext,
    run_id: str,
) -> RunRecord:
    record = session.scalar(
        select(RunRecord)
        .where(
            RunRecord.run_id == run_id,
            RunRecord.tenant_id == ctx.tenant_id,
            RunRecord.project_id == ctx.project_id,
        )
        .with_for_update()
    )
    if record is None:
        raise ApiError("NOT_FOUND", f"运行不存在：{run_id}", 404)
    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
    ):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_NOT_ALLOWED",
            "当前运行不是平台音频导入任务",
            409,
        )
    return record


def _external_dagster_run_id(record: RunRecord) -> str:
    dispatch = record.payload.get("dispatch")
    if not isinstance(dispatch, dict) or dispatch.get("adapter") != "dagster":
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_MISSING",
            "音频导入运行缺少可信 Dagster 分发绑定",
            409,
        )
    details = dispatch.get("details")
    if not isinstance(details, dict):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_MISSING",
            "音频导入运行缺少可信 Dagster 分发绑定",
            409,
        )
    dagster_run_id = str(details.get("dagster_run_id") or "").strip()
    external_run_id = str(details.get("external_run_id") or "").strip()
    if (
        not dagster_run_id
        or not external_run_id
        or not hmac.compare_digest(dagster_run_id, external_run_id)
    ):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_DISPATCH_BINDING_INVALID",
            "音频导入运行的 Dagster 分发绑定不完整",
            409,
        )
    return dagster_run_id


def _scoped_batch(
    session: Session,
    record: RunRecord,
    import_batch_id: str,
) -> ImportBatch:
    bound_batch_id = str(record.payload.get("import_batch_id") or "").strip()
    if not bound_batch_id or not hmac.compare_digest(import_batch_id, bound_batch_id):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_BATCH_MISMATCH",
            "阶段回执与运行绑定的导入批次不一致",
            409,
        )
    batch = session.scalar(
        select(ImportBatch)
        .where(
            ImportBatch.import_batch_id == import_batch_id,
            ImportBatch.task_run_id == record.run_id,
            ImportBatch.tenant_id == record.tenant_id,
            ImportBatch.project_id == record.project_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_BATCH_NOT_FOUND",
            "阶段回执对应的导入批次不存在",
            409,
        )
    return batch


def _validate_receipt_binding(
    record: RunRecord,
    payload: dict[str, Any],
    *,
    authenticated_source: str | None,
) -> str:
    if authenticated_source != "dagster":
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_AUTH_SOURCE_MISMATCH",
            "阶段回执必须由已验签的 Dagster 来源提交",
            403,
        )
    expected_external_id = _external_dagster_run_id(record)
    actual_external_id = str(payload["external_id"])
    if not hmac.compare_digest(actual_external_id, expected_external_id):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_EXTERNAL_ID_MISMATCH",
            "阶段回执的远端运行引用与 Dagster 分发绑定不一致",
            409,
        )
    expected_receipt_id = f"dagster:{expected_external_id}:{payload['stage']}"
    if not hmac.compare_digest(
        str(payload["progress_receipt_id"]),
        expected_receipt_id,
    ):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_RECEIPT_ID_MISMATCH",
            "阶段回执 ID 与远端运行和阶段绑定不一致",
            409,
        )
    return expected_external_id


def apply_audio_import_progress(
    session: Session,
    ctx: RequestContext,
    *,
    run_id: str,
    payload: dict[str, Any],
    completion_auth: dict[str, Any] | None,
) -> dict[str, Any]:
    if not hmac.compare_digest(str(payload["tenant_id"]), ctx.tenant_id) or not hmac.compare_digest(
        str(payload["project_id"]), ctx.project_id
    ):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_SCOPE_MISMATCH",
            "阶段回执声明的租户或项目与签名上下文不一致",
            403,
        )
    if not hmac.compare_digest(str(payload["task_run_id"]), run_id):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_RUN_MISMATCH",
            "阶段回执声明的 TaskRun 与请求路径不一致",
            409,
        )
    record = _scoped_run(session, ctx, run_id)
    authenticated_source = (
        str(completion_auth.get("authenticated_source") or "")
        if isinstance(completion_auth, dict)
        else None
    )
    external_run_id = _validate_receipt_binding(
        record,
        payload,
        authenticated_source=authenticated_source,
    )
    batch = _scoped_batch(
        session,
        record,
        str(payload["import_batch_id"]),
    )

    if (
        record.status not in ACTIVE_RUN_STATUSES
        or batch.status in TERMINAL_BATCH_STATUSES
        or batch.current_stage == "completed"
    ):
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_TERMINAL",
            "导入批次已进入终态，不能再修改执行阶段",
            409,
        )
    if batch.status != "running":
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_BATCH_NOT_RUNNING",
            "导入批次尚未进入运行态，不能接收执行阶段",
            409,
        )

    target_stage = str(payload["stage"])
    previous_stage = batch.current_stage
    applied = previous_stage != target_stage
    if applied and previous_stage != EXPECTED_PREVIOUS_STAGE[target_stage]:
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_OUT_OF_ORDER",
            "导入阶段回执乱序，不能回退或跳过真实执行阶段",
            409,
            details=[
                {
                    "current_stage": previous_stage,
                    "received_stage": target_stage,
                    "expected_previous_stage": EXPECTED_PREVIOUS_STAGE[target_stage],
                }
            ],
        )

    if applied:
        batch.current_stage = target_stage
        batch.payload = {
            **(batch.payload or {}),
            "external_progress": {
                "progress_receipt_id": payload["progress_receipt_id"],
                "external_run_id": external_run_id,
                "current_stage": target_stage,
                "received_at": datetime.now(UTC).isoformat(),
                "signature_key_id": (
                    completion_auth.get("signature_key_id")
                    if isinstance(completion_auth, dict)
                    else None
                ),
            },
        }

    record_audit(
        session,
        ctx,
        action="audio_import.progress_received",
        object_type="import_batch",
        object_id=batch.import_batch_id,
        result="success",
        before={
            "status": batch.status,
            "current_stage": previous_stage,
        },
        after={
            "status": batch.status,
            "current_stage": batch.current_stage,
            "applied": applied,
            "progress_receipt_id": payload["progress_receipt_id"],
            "external_run_id": external_run_id,
        },
        trace_id=batch.root_trace_id,
    )
    return {
        "run_id": record.run_id,
        "import_batch_id": batch.import_batch_id,
        "status": batch.status,
        "current_stage": batch.current_stage,
        "applied": applied,
        "root_trace_id": batch.root_trace_id,
    }


def mark_audio_import_batch_materializing(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    completion_receipt_id: str,
    result_ref: object,
) -> bool:
    """Expose the gap between a trusted engine receipt and business materialization."""

    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
    ):
        return False
    batch_id = str(record.payload.get("import_batch_id") or "").strip()
    if (
        not isinstance(result_ref, dict)
        or not batch_id
        or not isinstance(result_ref.get("import_batch_id"), str)
        or not hmac.compare_digest(
            str(result_ref.get("import_batch_id")),
            batch_id,
        )
    ):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BATCH_MISMATCH",
            "完成回执与 TaskRun 绑定的导入批次不一致",
            409,
        )
    batch = session.scalar(
        select(ImportBatch)
        .where(
            ImportBatch.import_batch_id == batch_id,
            ImportBatch.task_run_id == record.run_id,
            ImportBatch.tenant_id == record.tenant_id,
            ImportBatch.project_id == record.project_id,
        )
        .with_for_update()
    )
    if batch is None:
        raise ApiError(
            "AUDIO_IMPORT_PROGRESS_BATCH_NOT_FOUND",
            "音频导入运行缺少对应批次，不能进入物化阶段",
            409,
        )
    if batch.status in TERMINAL_BATCH_STATUSES or batch.current_stage == "completed":
        return False
    if batch.current_stage == "materializing":
        return False

    previous_status = batch.status
    previous_stage = batch.current_stage
    batch.status = "running"
    batch.current_stage = "materializing"
    if batch.started_at is None:
        batch.started_at = datetime.now(UTC)
    batch.payload = {
        **(batch.payload or {}),
        "materialization": {
            "completion_receipt_id": completion_receipt_id,
            "status": "pending",
            "started_at": datetime.now(UTC).isoformat(),
        },
    }
    record_audit(
        session,
        ctx,
        action="audio_import.materialization_started",
        object_type="import_batch",
        object_id=batch.import_batch_id,
        before={
            "status": previous_status,
            "current_stage": previous_stage,
        },
        after={
            "status": batch.status,
            "current_stage": batch.current_stage,
            "completion_receipt_id": completion_receipt_id,
        },
        trace_id=batch.root_trace_id,
    )
    return True
