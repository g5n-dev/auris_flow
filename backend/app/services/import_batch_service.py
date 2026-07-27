from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import ImportBatch, ImportBatchItem, RunRecord
from app.services.audit_service import record_audit
from app.services.public_run_projection_service import sanitize_public_run_string

_PUBLIC_BATCH_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_PUBLIC_BATCH_ERROR_CODES = frozenset(
    {
        "AUDIO_DOWNLOAD_FAILED",
        "AUDIO_IMPORT_AUDIO_INVALID",
        "AUDIO_IMPORT_BATCH_CANCELLED",
        "AUDIO_IMPORT_BATCH_FAILED",
        "AUDIO_IMPORT_BATCH_PARTIAL",
        "AUDIO_IMPORT_CONFIGURATION_INVALID",
        "AUDIO_IMPORT_CREDENTIAL_INVALID",
        "AUDIO_IMPORT_CREDENTIAL_UNAVAILABLE",
        "AUDIO_IMPORT_CURSOR_INVALID",
        "AUDIO_IMPORT_CURSOR_NOT_STRICTLY_INCREASING",
        "AUDIO_IMPORT_CURSOR_PAGE_MISMATCH",
        "AUDIO_IMPORT_DEADLINE_EXPIRED",
        "AUDIO_IMPORT_DOWNLOAD_FAILED",
        "AUDIO_IMPORT_DUPLICATE_SOURCE_RECORD",
        "AUDIO_IMPORT_EXECUTION_FAILED",
        "AUDIO_IMPORT_HOST_UNAVAILABLE",
        "AUDIO_IMPORT_MANIFEST_INVALID",
        "AUDIO_IMPORT_MANIFEST_PERSISTENCE_FAILED",
        "AUDIO_IMPORT_OBJECT_COLLISION",
        "AUDIO_IMPORT_PRIVATE_ADDRESS_FORBIDDEN",
        "AUDIO_IMPORT_PROGRESS_CALLBACK_FAILED",
        "AUDIO_IMPORT_REDIRECT_FORBIDDEN",
        "AUDIO_IMPORT_RUN_BUDGET_EXCEEDED",
        "AUDIO_IMPORT_SOURCE_LIST_FAILED",
        "AUDIO_IMPORT_SOURCE_RECORD_INVALID",
        "AUDIO_IMPORT_SOURCE_SCOPE_MISMATCH",
        "AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID",
        "AUDIO_IMPORT_STORAGE_FAILED",
        "AUDIO_IMPORT_URL_HOST_NOT_ALLOWED",
        "AUDIO_IMPORT_URL_UNSAFE",
        "AUDIO_URL_EXPIRED",
        "PLATFORM_CREDENTIAL_INVALID",
        "SOURCE_URL_EXPIRED",
    }
)
_BATCH_FAILURE_DEFAULTS = {
    "failed": (
        "AUDIO_IMPORT_BATCH_FAILED",
        "导入批次执行失败；请根据错误码检查配置后重试。",
    ),
    "partial": (
        "AUDIO_IMPORT_BATCH_PARTIAL",
        "导入批次部分失败；成功项已保留，可重试失败项。",
    ),
    "cancelled": (
        "AUDIO_IMPORT_BATCH_CANCELLED",
        "导入批次已取消，可重新创建拉取。",
    ),
}


def _public_batch_failure(batch: ImportBatch) -> tuple[str | None, str | None]:
    defaults = _BATCH_FAILURE_DEFAULTS.get(batch.status)
    if defaults is None:
        return None, None

    payload = batch.payload if isinstance(batch.payload, dict) else {}
    raw_reason = payload.get("reason") or payload.get("task_run_terminal_reason")
    raw_code = payload.get("error_code")
    if raw_code is None and isinstance(raw_reason, str):
        raw_code = raw_reason

    error_code: str | None = None
    if isinstance(raw_code, str):
        candidate = sanitize_public_run_string(raw_code.strip()[:128], field_name="code")
        if (
            _PUBLIC_BATCH_ERROR_CODE_PATTERN.fullmatch(candidate)
            and candidate in _PUBLIC_BATCH_ERROR_CODES
        ):
            error_code = candidate

    reason: str | None = None
    if isinstance(raw_reason, str) and raw_reason.strip():
        candidate = sanitize_public_run_string(
            raw_reason.strip()[:2000],
            field_name="reason",
        ).strip()
        if candidate and not _PUBLIC_BATCH_ERROR_CODE_PATTERN.fullmatch(candidate):
            reason = candidate[:500]

    default_code, default_reason = defaults
    return error_code or default_code, reason or default_reason


def import_batch_payload(batch: ImportBatch) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    error_code, reason = _public_batch_failure(batch)
    return {
        "import_batch_id": batch.import_batch_id,
        "task_run_id": batch.task_run_id,
        "task_version_id": batch.task_version_id,
        "connector_id": batch.connector_id,
        "status": batch.status,
        "current_stage": batch.current_stage,
        "total_items": batch.total_items,
        "succeeded_items": batch.succeeded_items,
        "skipped_items": batch.skipped_items,
        "failed_items": batch.failed_items,
        "cursor_before": batch.cursor_before,
        "cursor_after": batch.cursor_after,
        "root_trace_id": batch.root_trace_id,
        "trace_id": batch.trace_id,
        "started_at": iso(batch.started_at),
        "finished_at": iso(batch.finished_at),
        "created_at": iso(batch.created_at),
        "updated_at": iso(batch.updated_at),
        "error_code": error_code,
        "reason": reason,
    }


def import_batch_item_payload(item: ImportBatchItem) -> dict[str, Any]:
    return {
        "import_item_id": item.import_item_id,
        "import_batch_id": item.import_batch_id,
        "external_record_id": item.external_record_id,
        "status": item.status,
        "error_code": item.error_code,
        "object_version": item.object_version,
        "audio_session_id": item.audio_session_id,
        "root_trace_id": item.root_trace_id,
        "trace_id": item.trace_id,
    }


def create_import_batch_for_task_run(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
) -> ImportBatch | None:
    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != "auris-flow-audio-import-v1"
    ):
        return None
    batch_id = str(record.payload.get("import_batch_id") or "").strip()
    task_version_id = str(record.payload.get("task_version_id") or "").strip()
    connector_snapshot = record.payload.get("connector_snapshot")
    connector_id = (
        str(connector_snapshot.get("connector_id") or "").strip()
        if isinstance(connector_snapshot, dict)
        else ""
    )
    root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id).strip()
    if not batch_id or not task_version_id or not connector_id or not root_trace_id:
        raise ApiError(
            "IMPORT_BATCH_BINDING_INVALID",
            "导入运行缺少服务端冻结的批次绑定",
            409,
        )
    cursor_policy = (
        connector_snapshot.get("cursor_policy") if isinstance(connector_snapshot, dict) else None
    )
    cursor_before = cursor_policy.get("cursor_value") if isinstance(cursor_policy, dict) else None
    batch = ImportBatch(
        import_batch_id=batch_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        task_run_id=record.run_id,
        task_version_id=task_version_id,
        connector_id=connector_id,
        status="queued",
        current_stage="queued",
        total_items=0,
        succeeded_items=0,
        skipped_items=0,
        failed_items=0,
        cursor_before=str(cursor_before) if cursor_before is not None else None,
        cursor_after=None,
        root_trace_id=root_trace_id,
        trace_id=record.trace_id,
        payload={
            "connector_snapshot_sha256": record.payload.get("connector_snapshot_sha256"),
            "target_asset_key": (
                record.payload.get("target", {}).get("target_asset_key")
                if isinstance(record.payload.get("target"), dict)
                else None
            ),
        },
    )
    session.add(batch)
    session.flush()
    record_audit(
        session,
        ctx,
        action="import_batch.create",
        object_type="import_batch",
        object_id=batch.import_batch_id,
        after=import_batch_payload(batch),
        trace_id=record.trace_id,
    )
    return batch


def get_import_batch(
    session: Session,
    ctx: RequestContext,
    import_batch_id: str,
) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.import_batch_id == import_batch_id,
            ImportBatch.tenant_id == ctx.tenant_id,
            ImportBatch.project_id == ctx.project_id,
        )
    )
    if batch is None:
        raise ApiError("NOT_FOUND", f"导入批次不存在：{import_batch_id}", 404)
    return batch


def list_import_batch_items(
    session: Session,
    ctx: RequestContext,
    import_batch_id: str,
) -> list[ImportBatchItem]:
    get_import_batch(session, ctx, import_batch_id)
    return list(
        session.scalars(
            select(ImportBatchItem)
            .where(
                ImportBatchItem.import_batch_id == import_batch_id,
                ImportBatchItem.tenant_id == ctx.tenant_id,
                ImportBatchItem.project_id == ctx.project_id,
            )
            .order_by(ImportBatchItem.external_record_id, ImportBatchItem.import_item_id)
        )
    )
