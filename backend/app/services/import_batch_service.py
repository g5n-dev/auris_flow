from __future__ import annotations

import re
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
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
        "AUDIO_IMPORT_ITEM_FAILED",
        "PLATFORM_CREDENTIAL_INVALID",
        "SOURCE_URL_EXPIRED",
    }
)
_IMPORT_FAILURE_RECOVERY: dict[str, tuple[str, bool]] = {
    "AUDIO_DOWNLOAD_FAILED": (
        "确认外部平台音频地址可访问、网络已恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_DOWNLOAD_FAILED": (
        "确认外部平台音频地址可访问、网络已恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_HOST_UNAVAILABLE": (
        "外部平台暂时不可用；确认服务恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_DEADLINE_EXPIRED": (
        "本次拉取超过执行时限；缩小首次时间窗口或确认平台恢复后重试。",
        True,
    ),
    "AUDIO_IMPORT_RUN_BUDGET_EXCEEDED": (
        "本次拉取超过任务资源上限；缩小时间窗口后重新发布配置。",
        False,
    ),
    "AUDIO_IMPORT_STORAGE_FAILED": (
        "内部对象存储暂时写入失败；确认存储恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_MANIFEST_PERSISTENCE_FAILED": (
        "导入清单暂时无法保存；确认对象存储恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_PROGRESS_CALLBACK_FAILED": (
        "导入回执暂时未写入平台；确认服务恢复后重试失败项。",
        True,
    ),
    "AUDIO_URL_EXPIRED": (
        "源音频地址已过期；请先在外部平台刷新地址，再重试失败项。",
        True,
    ),
    "SOURCE_URL_EXPIRED": (
        "源音频地址已过期；请先在外部平台刷新地址，再重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_CREDENTIAL_INVALID": (
        "平台凭证已失效；更新凭证引用并通过连通性测试后再重试。",
        True,
    ),
    "AUDIO_IMPORT_CREDENTIAL_UNAVAILABLE": (
        "平台凭证当前不可用；恢复密钥服务或凭证引用后再重试。",
        True,
    ),
    "PLATFORM_CREDENTIAL_INVALID": (
        "平台凭证已失效；更新凭证引用并通过连通性测试后再重试。",
        True,
    ),
    "AUDIO_IMPORT_AUDIO_INVALID": (
        "源文件不是支持的音频格式或内容已损坏；请先修复源文件。",
        False,
    ),
    "AUDIO_IMPORT_SOURCE_RECORD_INVALID": (
        "源记录缺少必填字段或字段格式不合法；请修复字段映射或源数据。",
        False,
    ),
    "AUDIO_IMPORT_CONFIGURATION_INVALID": (
        "导入配置不合法；请修复配置、重新测试并发布新版本。",
        False,
    ),
    "AUDIO_IMPORT_CURSOR_INVALID": (
        "平台游标格式不合法；请修复游标配置并发布新版本。",
        False,
    ),
    "AUDIO_IMPORT_CURSOR_NOT_STRICTLY_INCREASING": (
        "平台游标没有向前推进；请检查分页接口和游标字段。",
        False,
    ),
    "AUDIO_IMPORT_CURSOR_PAGE_MISMATCH": (
        "分页回执与请求游标不一致；请检查平台分页接口。",
        False,
    ),
    "AUDIO_IMPORT_MANIFEST_INVALID": (
        "导入清单未通过完整性校验；请检查源记录和平台接口。",
        False,
    ),
    "AUDIO_IMPORT_OBJECT_COLLISION": (
        "同一对象标识对应了不同内容；请检查外部录音 ID 和去重规则。",
        False,
    ),
    "AUDIO_IMPORT_DUPLICATE_SOURCE_RECORD": (
        "源清单包含重复的外部录音 ID；请先修复源数据。",
        False,
    ),
    "AUDIO_IMPORT_SOURCE_SCOPE_MISMATCH": (
        "源记录超出当前平台租户或门店范围；请修复平台范围配置。",
        False,
    ),
    "AUDIO_IMPORT_PRIVATE_ADDRESS_FORBIDDEN": (
        "音频地址不符合网络安全策略；请改用允许的 HTTPS 公网地址。",
        False,
    ),
    "AUDIO_IMPORT_REDIRECT_FORBIDDEN": (
        "音频下载发生了不允许的重定向；请修复源地址或域名白名单。",
        False,
    ),
    "AUDIO_IMPORT_URL_HOST_NOT_ALLOWED": (
        "音频域名不在当前连接允许范围；请修复连接配置并发布新版本。",
        False,
    ),
    "AUDIO_IMPORT_URL_UNSAFE": (
        "音频地址未通过安全校验；请修复源地址或连接配置。",
        False,
    ),
    "AUDIO_IMPORT_STORAGE_CONFIGURATION_INVALID": (
        "内部对象存储配置不完整；请联系平台管理员修复后再拉取。",
        False,
    ),
    "AUDIO_IMPORT_BATCH_PARTIAL": (
        "成功项已保留；修复失败原因后可重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_BATCH_FAILED": (
        "本批次未完成；请根据失败建议修复后重试。",
        True,
    ),
    "AUDIO_IMPORT_BATCH_CANCELLED": (
        "本批次已取消；确认仍需导入后可重新拉取。",
        True,
    ),
    "AUDIO_IMPORT_EXECUTION_FAILED": (
        "导入执行未完成；确认执行服务恢复后重试失败项。",
        True,
    ),
    "AUDIO_IMPORT_SOURCE_LIST_FAILED": (
        "平台录音清单读取失败；确认平台接口恢复后重试。",
        True,
    ),
    "AUDIO_IMPORT_ITEM_FAILED": (
        "该记录未完成导入；请查看平台连接和源记录后再处理。",
        False,
    ),
}
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
IMPORT_BATCH_STATUSES = frozenset(
    {"queued", "running", "partial", "succeeded", "failed", "cancelled"}
)
IMPORT_BATCH_ITEM_STATUSES = frozenset({"queued", "running", "succeeded", "skipped", "failed"})


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


def _public_failure_recovery(
    raw_error_code: str | None,
) -> tuple[str | None, str | None, bool]:
    if raw_error_code is None:
        return None, None, False
    normalized = raw_error_code.strip().upper()
    if (
        not _PUBLIC_BATCH_ERROR_CODE_PATTERN.fullmatch(normalized)
        or normalized not in _PUBLIC_BATCH_ERROR_CODES
    ):
        normalized = "AUDIO_IMPORT_ITEM_FAILED"
    recovery_suggestion, retryable = _IMPORT_FAILURE_RECOVERY.get(
        normalized,
        _IMPORT_FAILURE_RECOVERY["AUDIO_IMPORT_ITEM_FAILED"],
    )
    return normalized, recovery_suggestion, retryable


def _lineage_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lineage_attempt(value: Any, *, default: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        attempt = int(value)
    except (TypeError, ValueError):
        return default
    return attempt if 1 <= attempt <= 10_000 else default


def _batch_retry_lineage(
    session: Session,
    record: RunRecord,
    *,
    import_batch_id: str,
) -> dict[str, Any]:
    source_run_id = str(record.payload.get("retry_of_run_id") or "").strip()
    if not source_run_id:
        return {
            "source_task_run_id": None,
            "source_import_batch_id": None,
            "root_task_run_id": record.run_id,
            "root_import_batch_id": import_batch_id,
            "attempt": 1,
        }

    source_record = session.scalar(
        select(RunRecord).where(
            RunRecord.run_id == source_run_id,
            RunRecord.tenant_id == record.tenant_id,
            RunRecord.project_id == record.project_id,
            RunRecord.run_type == "task_run",
        )
    )
    source_batch = session.scalar(
        select(ImportBatch).where(
            ImportBatch.task_run_id == source_run_id,
            ImportBatch.tenant_id == record.tenant_id,
            ImportBatch.project_id == record.project_id,
        )
    )
    if (
        source_record is None
        or source_batch is None
        or source_record.payload.get("execution_contract") != "auris-flow-audio-import-v1"
    ):
        raise ApiError(
            "IMPORT_BATCH_RETRY_LINEAGE_INVALID",
            "导入重试缺少同一租户项目内的真实来源批次",
            409,
        )

    source_lineage = _lineage_payload(source_batch.payload.get("retry_lineage"))
    source_attempt = _lineage_attempt(
        source_lineage.get("attempt"),
        default=_lineage_attempt(source_record.payload.get("retry_attempt"), default=0) + 1,
    )
    attempt = _lineage_attempt(record.payload.get("retry_attempt"), default=0) + 1
    if attempt != source_attempt + 1:
        raise ApiError(
            "IMPORT_BATCH_RETRY_LINEAGE_INVALID",
            "导入重试次数与服务端来源链不一致",
            409,
        )
    return {
        "source_task_run_id": source_record.run_id,
        "source_import_batch_id": source_batch.import_batch_id,
        "root_task_run_id": str(source_lineage.get("root_task_run_id") or source_record.run_id),
        "root_import_batch_id": str(
            source_lineage.get("root_import_batch_id") or source_batch.import_batch_id
        ),
        "attempt": attempt,
    }


def import_batch_retry_lineage(batch: ImportBatch) -> dict[str, Any]:
    raw = _lineage_payload(batch.payload.get("retry_lineage"))
    return {
        "source_task_run_id": raw.get("source_task_run_id"),
        "source_import_batch_id": raw.get("source_import_batch_id"),
        "root_task_run_id": raw.get("root_task_run_id") or batch.task_run_id,
        "root_import_batch_id": raw.get("root_import_batch_id") or batch.import_batch_id,
        "attempt": _lineage_attempt(raw.get("attempt")),
    }


def import_item_retry_lineage(
    session: Session,
    *,
    record: RunRecord,
    batch: ImportBatch,
    import_item_id: str,
    external_record_id: str,
) -> dict[str, Any]:
    batch_lineage = import_batch_retry_lineage(batch)
    source_batch_id = batch_lineage.get("source_import_batch_id")
    source_item: ImportBatchItem | None = None
    if isinstance(source_batch_id, str) and source_batch_id:
        source_item = session.scalar(
            select(ImportBatchItem).where(
                ImportBatchItem.import_batch_id == source_batch_id,
                ImportBatchItem.external_record_id == external_record_id,
                ImportBatchItem.tenant_id == record.tenant_id,
                ImportBatchItem.project_id == record.project_id,
            )
        )
    if source_item is None:
        return {
            "source_import_batch_id": source_batch_id,
            "source_import_item_id": None,
            "root_import_batch_id": batch.import_batch_id,
            "root_import_item_id": import_item_id,
            "attempt": batch_lineage["attempt"],
        }
    source_lineage = _lineage_payload(source_item.payload.get("retry_lineage"))
    return {
        "source_import_batch_id": source_item.import_batch_id,
        "source_import_item_id": source_item.import_item_id,
        "root_import_batch_id": (
            source_lineage.get("root_import_batch_id") or source_item.import_batch_id
        ),
        "root_import_item_id": (
            source_lineage.get("root_import_item_id") or source_item.import_item_id
        ),
        "attempt": batch_lineage["attempt"],
    }


def import_batch_payload(batch: ImportBatch) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    error_code, reason = _public_batch_failure(batch)
    error_code, recovery_suggestion, retryable = _public_failure_recovery(error_code)
    payload = batch.payload if isinstance(batch.payload, dict) else {}
    raw_audio_session_ids = payload.get("materialized_audio_session_ids")
    audio_session_ids = (
        [value for value in raw_audio_session_ids if isinstance(value, str) and value.strip()]
        if isinstance(raw_audio_session_ids, list)
        else []
    )
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
        "recovery_suggestion": recovery_suggestion,
        "retryable": retryable,
        "retry_lineage": import_batch_retry_lineage(batch),
        "audio_session_ids": audio_session_ids,
    }


def import_batch_item_payload(item: ImportBatchItem) -> dict[str, Any]:
    error_code, recovery_suggestion, retryable = _public_failure_recovery(item.error_code)
    payload = item.payload if isinstance(item.payload, dict) else {}
    lineage = _lineage_payload(payload.get("retry_lineage"))
    return {
        "import_item_id": item.import_item_id,
        "import_batch_id": item.import_batch_id,
        "external_record_id": item.external_record_id,
        "status": item.status,
        "error_code": error_code,
        "object_version": item.object_version,
        "audio_session_id": item.audio_session_id,
        "root_trace_id": item.root_trace_id,
        "trace_id": item.trace_id,
        "recovery_suggestion": recovery_suggestion,
        "retryable": retryable,
        "retry_lineage": {
            "source_import_batch_id": lineage.get("source_import_batch_id"),
            "source_import_item_id": lineage.get("source_import_item_id"),
            "root_import_batch_id": (lineage.get("root_import_batch_id") or item.import_batch_id),
            "root_import_item_id": (lineage.get("root_import_item_id") or item.import_item_id),
            "attempt": _lineage_attempt(lineage.get("attempt")),
        },
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
    retry_lineage = _batch_retry_lineage(
        session,
        record,
        import_batch_id=batch_id,
    )
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
            "retry_lineage": retry_lineage,
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


def _encode_page_cursor(kind: str, offset: int) -> str:
    return urlsafe_b64encode(f"{kind}:{offset}".encode("ascii")).decode("ascii").rstrip("=")


def _decode_page_cursor(kind: str, cursor: str | int | None) -> int:
    if cursor in (None, "", 0):
        return 0
    if isinstance(cursor, int):
        if cursor < 0:
            raise ApiError("INVALID_CURSOR", "cursor 不能为负数", 400)
        return cursor
    try:
        text = str(cursor)
        padded = text + "=" * (-len(text) % 4)
        decoded = urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400) from None
    prefix, _, raw_offset = decoded.partition(":")
    if prefix != kind or not raw_offset.isdigit():
        raise ApiError("INVALID_CURSOR", "cursor 格式无效", 400)
    return int(raw_offset)


def list_import_batches(
    session: Session,
    ctx: RequestContext,
    *,
    connector_id: str | None = None,
    task_version_id: str | None = None,
    target_asset_key: str | None = None,
    status: str | None = None,
    cursor: str | int | None = None,
    limit: int = 50,
) -> tuple[list[ImportBatch], int, str | None]:
    conditions = [
        ImportBatch.tenant_id == ctx.tenant_id,
        ImportBatch.project_id == ctx.project_id,
    ]
    if connector_id:
        conditions.append(ImportBatch.connector_id == connector_id)
    if task_version_id:
        conditions.append(ImportBatch.task_version_id == task_version_id)
    if target_asset_key:
        conditions.append(ImportBatch.payload["target_asset_key"].as_string() == target_asset_key)
    if status:
        if status not in IMPORT_BATCH_STATUSES:
            raise ApiError("VALIDATION_ERROR", "导入批次状态筛选不合法", 422)
        conditions.append(ImportBatch.status == status)
    offset = _decode_page_cursor("import_batch", cursor)
    total = int(
        session.scalar(select(func.count()).select_from(ImportBatch).where(*conditions)) or 0
    )
    records = list(
        session.scalars(
            select(ImportBatch)
            .where(*conditions)
            .order_by(
                ImportBatch.created_at.desc(),
                ImportBatch.import_batch_id.desc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
    )
    visible = records[:limit]
    next_cursor = (
        _encode_page_cursor("import_batch", offset + limit) if len(records) > limit else None
    )
    return visible, total, next_cursor


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


def list_import_batch_items_page(
    session: Session,
    ctx: RequestContext,
    import_batch_id: str,
    *,
    status: str | None = None,
    cursor: str | int | None = None,
    limit: int = 50,
) -> tuple[list[ImportBatchItem], int, str | None]:
    get_import_batch(session, ctx, import_batch_id)
    conditions = [
        ImportBatchItem.import_batch_id == import_batch_id,
        ImportBatchItem.tenant_id == ctx.tenant_id,
        ImportBatchItem.project_id == ctx.project_id,
    ]
    if status:
        if status not in IMPORT_BATCH_ITEM_STATUSES:
            raise ApiError("VALIDATION_ERROR", "导入项状态筛选不合法", 422)
        conditions.append(ImportBatchItem.status == status)
    offset = _decode_page_cursor("import_batch_item", cursor)
    total = int(
        session.scalar(select(func.count()).select_from(ImportBatchItem).where(*conditions)) or 0
    )
    records = list(
        session.scalars(
            select(ImportBatchItem)
            .where(*conditions)
            .order_by(
                ImportBatchItem.created_at.desc(),
                ImportBatchItem.import_item_id.desc(),
            )
            .offset(offset)
            .limit(limit + 1)
        )
    )
    visible = records[:limit]
    next_cursor = (
        _encode_page_cursor("import_batch_item", offset + limit) if len(records) > limit else None
    )
    return visible, total, next_cursor
