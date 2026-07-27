from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import (
    AssetLineageEdge,
    AssetMaterialization,
    AudioRecording,
    ImportBatch,
    ImportBatchItem,
    JsonResource,
    RunRecord,
    StorageObject,
)
from app.services.audit_service import record_audit
from app.services.connector_import_service import advance_connector_sync_cursor
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

AUDIO_IMPORT_EXECUTION_CONTRACT = "auris-flow-audio-import-v1"
AUDIO_IMPORT_RESULT_SCHEMA = "auris-flow-audio-import-result-v1"
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EXECUTOR_ITEM_STATUSES = frozenset({"succeeded", "failed"})
_SOURCE_FIELDS = frozenset({"started_at", "duration_ms", "store_ref", "agent_ref", "device_ref"})


def _stable_id(prefix: str, *parts: str, length: int = 32) -> str:
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _required_text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
            f"{field} 必须是非空字符串",
            422,
        )
    normalized = value.strip()
    if len(normalized) > maximum or any(ord(character) < 0x20 for character in normalized):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
            f"{field} 长度或字符无效",
            422,
        )
    return normalized


def _source_metadata(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - _SOURCE_FIELDS:
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
            "导入项 source 字段无效",
            422,
        )
    result: dict[str, Any] = {}
    started_at = raw.get("started_at")
    if started_at is not None:
        normalized_started_at = _required_text(
            started_at,
            field="source.started_at",
            maximum=64,
        )
        try:
            parsed = datetime.fromisoformat(normalized_started_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "source.started_at 必须是带时区的 ISO 时间",
                422,
            ) from exc
        if parsed.tzinfo is None:
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "source.started_at 必须包含时区",
                422,
            )
        result["started_at"] = parsed.astimezone(UTC).isoformat()
    duration_ms = raw.get("duration_ms")
    if duration_ms is not None:
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 0 <= duration_ms <= 24 * 60 * 60 * 1000
        ):
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "source.duration_ms 超出允许范围",
                422,
            )
        result["duration_ms"] = duration_ms
    for field in ("store_ref", "agent_ref", "device_ref"):
        if raw.get(field) is not None:
            result[field] = _required_text(
                raw.get(field),
                field=f"source.{field}",
                maximum=256,
            )
    return result


def validate_audio_import_completion_contract(
    record: RunRecord,
    completion_payload: dict[str, Any],
) -> None:
    """Bind an executor result to the exact dispatched audio-import envelope."""

    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
    ):
        return
    result_ref = completion_payload.get("result_ref")
    if not isinstance(result_ref, dict):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_RESULT_INVALID",
            "音频导入完成回执缺少结果对象",
            422,
        )
    if result_ref.get("schema_version") != AUDIO_IMPORT_RESULT_SCHEMA:
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_CONTRACT_INVALID",
            "音频导入完成回执 schema_version 无效",
            422,
        )
    if result_ref.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT:
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
            "音频导入完成回执执行契约与运行绑定不一致",
            409,
        )
    if result_ref.get("import_batch_id") != record.payload.get("import_batch_id"):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
            "音频导入回执未绑定当前批次",
            409,
        )

    dispatch = record.payload.get("dispatch")
    dispatch_details = dispatch.get("details") if isinstance(dispatch, dict) else None
    expected_envelope_sha256 = (
        dispatch_details.get("execution_envelope_sha256")
        if isinstance(dispatch_details, dict)
        else None
    )
    actual_envelope_sha256 = result_ref.get("execution_envelope_sha256")
    if not isinstance(expected_envelope_sha256, str) or not _SHA256_PATTERN.fullmatch(
        expected_envelope_sha256
    ):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BINDING_MISSING",
            "音频导入运行缺少冻结的执行信封摘要",
            409,
        )
    if (
        not isinstance(actual_envelope_sha256, str)
        or not _SHA256_PATTERN.fullmatch(actual_envelope_sha256)
        or not hmac.compare_digest(actual_envelope_sha256, expected_envelope_sha256)
    ):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
            "音频导入完成回执与分发时冻结的执行信封不一致",
            409,
        )

    batch_status = result_ref.get("batch_status")
    if completion_payload.get("status") == "failed":
        if batch_status != "failed":
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_CONTRACT_INVALID",
                "失败的音频导入回执必须声明 failed 批次状态",
                422,
            )
        return

    raw_items = result_ref.get("items")
    if not isinstance(raw_items, list):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
            "音频导入回执的导入项必须是数组",
            422,
        )
    statuses: list[str] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or raw_item.get("status") not in _EXECUTOR_ITEM_STATUSES:
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "执行器导入项状态只能是 succeeded 或 failed",
                422,
            )
        statuses.append(str(raw_item["status"]))
    succeeded = statuses.count("succeeded")
    failed = statuses.count("failed")
    expected_batch_status = (
        "failed" if failed and not succeeded else "partial" if failed else "succeeded"
    )
    if batch_status != expected_batch_status:
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_CONTRACT_INVALID",
            "音频导入回执 batch_status 与导入项结果不一致",
            422,
        )


def _batch_for_run(
    session: Session,
    record: RunRecord,
    *,
    lock: bool = True,
) -> ImportBatch:
    query = select(ImportBatch).where(
        ImportBatch.import_batch_id == record.payload.get("import_batch_id"),
        ImportBatch.task_run_id == record.run_id,
        ImportBatch.tenant_id == record.tenant_id,
        ImportBatch.project_id == record.project_id,
    )
    if lock:
        query = query.with_for_update()
    batch = session.scalar(query)
    if batch is None:
        raise ApiError(
            "AUDIO_IMPORT_BATCH_NOT_FOUND",
            "音频导入运行缺少对应批次",
            409,
        )
    return batch


def mark_audio_import_batch_running(session: Session, record: RunRecord) -> None:
    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
    ):
        return
    batch = _batch_for_run(session, record)
    if batch.status == "queued":
        batch.status = "running"
        batch.current_stage = "listing"
        batch.started_at = datetime.now(UTC)


def _storage_for_item(
    session: Session,
    record: RunRecord,
    raw_item: dict[str, Any],
) -> StorageObject:
    storage_object_id = _required_text(
        raw_item.get("storage_object_id"),
        field="storage_object_id",
        maximum=128,
    )
    storage_object = session.scalar(
        select(StorageObject)
        .where(
            StorageObject.storage_object_id == storage_object_id,
            StorageObject.tenant_id == record.tenant_id,
            StorageObject.project_id == record.project_id,
            StorageObject.source_type == record.run_type,
            StorageObject.source_id == record.run_id,
            StorageObject.status == "verified",
        )
        .with_for_update()
    )
    if storage_object is None:
        raise ApiError(
            "AUDIO_IMPORT_STORAGE_OBJECT_NOT_VERIFIED",
            "成功导入项未绑定当前运行的可信录音对象",
            409,
        )
    content_sha256 = _required_text(
        raw_item.get("content_sha256"),
        field="content_sha256",
        maximum=64,
    ).lower()
    object_version = _required_text(
        raw_item.get("object_version"),
        field="object_version",
        maximum=512,
    )
    stored_version = (
        storage_object.payload.get("object_version_id")
        if isinstance(storage_object.payload, dict)
        else None
    )
    if (
        storage_object.content_sha256 != content_sha256
        or stored_version != object_version
        or storage_object.content_type not in {"audio/wav", "audio/x-wav"}
    ):
        raise ApiError(
            "AUDIO_IMPORT_STORAGE_OBJECT_MISMATCH",
            "成功导入项与可信录音对象的哈希、版本或格式不一致",
            409,
        )
    return storage_object


def _upsert_import_item(
    session: Session,
    *,
    record: RunRecord,
    batch: ImportBatch,
    external_record_id: str,
    status: str,
    error_code: str | None,
    object_version: str | None,
    audio_session_id: str | None,
    payload: dict[str, Any],
) -> ImportBatchItem:
    existing = session.scalar(
        select(ImportBatchItem)
        .where(
            ImportBatchItem.tenant_id == record.tenant_id,
            ImportBatchItem.project_id == record.project_id,
            ImportBatchItem.import_batch_id == batch.import_batch_id,
            ImportBatchItem.external_record_id == external_record_id,
        )
        .with_for_update()
    )
    if existing is not None:
        immutable = (
            existing.status,
            existing.error_code,
            existing.object_version,
            existing.audio_session_id,
        )
        requested = (status, error_code, object_version, audio_session_id)
        if immutable != requested:
            raise ApiError(
                "AUDIO_IMPORT_ITEM_CONFLICT",
                "同一批次的外部录音不能被不同结果覆盖",
                409,
            )
        return existing
    item = ImportBatchItem(
        import_item_id=_stable_id(
            "import_item",
            record.tenant_id,
            record.project_id,
            batch.import_batch_id,
            external_record_id,
        ),
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        import_batch_id=batch.import_batch_id,
        external_record_id=external_record_id,
        status=status,
        error_code=error_code,
        object_version=object_version,
        audio_session_id=audio_session_id,
        root_trace_id=batch.root_trace_id,
        trace_id=record.trace_id,
        payload=payload,
    )
    session.add(item)
    return item


def _materialize_succeeded_item(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    batch: ImportBatch,
    raw_item: dict[str, Any],
    *,
    external_record_id: str,
    source: dict[str, Any],
) -> tuple[str, str, str, bool]:
    storage_object = _storage_for_item(session, record, raw_item)
    content_sha256 = str(storage_object.content_sha256)
    identity_parts = (
        record.tenant_id,
        record.project_id,
        batch.connector_id,
        external_record_id,
        content_sha256,
    )
    recording_id = _stable_id("rec_import", *identity_parts)
    audio_session_id = _stable_id("audio_session_import", *identity_parts)
    source_record_id = _stable_id(
        "source_record",
        record.tenant_id,
        record.project_id,
        batch.connector_id,
        external_record_id,
        content_sha256,
    )
    connector = record.payload.get("connector_snapshot")
    connector_snapshot = connector if isinstance(connector, dict) else {}
    platform_connection_id = _required_text(
        connector_snapshot.get("platform_connection_id"),
        field="platform_connection_id",
        maximum=128,
    )
    root_trace_id = batch.root_trace_id
    storage_projection = {
        "storage_object_id": storage_object.storage_object_id,
        "provider": storage_object.provider,
        "bucket": storage_object.bucket,
        "object_key": storage_object.object_key,
        "content_type": storage_object.content_type,
        "content_length": storage_object.size_bytes,
        "checksum_sha256": storage_object.content_sha256,
        "etag": storage_object.etag,
        "status": storage_object.status,
        "source_type": storage_object.source_type,
        "source_id": storage_object.source_id,
        "trace_id": root_trace_id,
    }
    recording_payload = {
        "recording_id": recording_id,
        "audio_session_id": audio_session_id,
        "storage_object_id": storage_object.storage_object_id,
        "storage_object": storage_projection,
        "external_record_id": external_record_id,
        "platform_connection_id": platform_connection_id,
        "connector_id": batch.connector_id,
        "import_batch_id": batch.import_batch_id,
        "source_record_id": source_record_id,
        "root_trace_id": root_trace_id,
        "trace_id": root_trace_id,
        **source,
    }
    existing_recording = session.get(AudioRecording, recording_id)
    if existing_recording is not None and (
        existing_recording.tenant_id != record.tenant_id
        or existing_recording.project_id != record.project_id
    ):
        raise ApiError(
            "AUDIO_IMPORT_RECORDING_CONFLICT",
            "导入录音标识已绑定其他作用域",
            409,
        )
    if existing_recording is not None:
        canonical_storage_id = existing_recording.payload.get("storage_object_id")
        canonical_storage = (
            session.get(StorageObject, canonical_storage_id)
            if isinstance(canonical_storage_id, str)
            else None
        )
        session_resource = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == record.tenant_id,
                JsonResource.project_id == record.project_id,
                JsonResource.collection == "audio_sessions",
                JsonResource.resource_key == audio_session_id,
            )
        )
        source_resource = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == record.tenant_id,
                JsonResource.project_id == record.project_id,
                JsonResource.collection == "source_records",
                JsonResource.resource_key == source_record_id,
            )
        )
        if (
            canonical_storage is None
            or canonical_storage.tenant_id != record.tenant_id
            or canonical_storage.project_id != record.project_id
            or canonical_storage.content_sha256 != content_sha256
            or session_resource is None
            or source_resource is None
        ):
            raise ApiError(
                "AUDIO_IMPORT_RECORDING_CONFLICT",
                "既有录音的对象、会话或来源记录不完整",
                409,
            )
        return recording_id, audio_session_id, source_record_id, True
    if existing_recording is None:
        session.add(
            AudioRecording(
                recording_id=recording_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                status="verified",
                trace_id=root_trace_id,
                payload=recording_payload,
            )
        )
    upsert_resource(
        session,
        ctx,
        "recordings",
        recording_id,
        recording_payload,
        status="verified",
        trace_id=root_trace_id,
    )
    session_payload = {
        "id": audio_session_id,
        "audio_session_id": audio_session_id,
        "recording_id": recording_id,
        "status": "ready",
        "started_at": source.get("started_at"),
        "duration_ms": source.get("duration_ms"),
        "store_id": source.get("store_ref"),
        "employee_id": source.get("agent_ref"),
        "device_id": source.get("device_ref"),
        "external_record_id": external_record_id,
        "platform_connection_id": platform_connection_id,
        "connector_id": batch.connector_id,
        "import_batch_id": batch.import_batch_id,
        "source_record_id": source_record_id,
        "root_trace_id": root_trace_id,
        "trace_id": root_trace_id,
        "source": "platform_audio_import",
    }
    upsert_resource(
        session,
        ctx,
        "audio_sessions",
        audio_session_id,
        session_payload,
        status="ready",
        trace_id=root_trace_id,
        audit_action="audio_session.imported",
    )
    source_payload = {
        "id": source_record_id,
        "source_record_id": source_record_id,
        "external_record_id": external_record_id,
        "status": "succeeded",
        "connector_id": batch.connector_id,
        "platform_connection_id": platform_connection_id,
        "import_batch_id": batch.import_batch_id,
        "storage_object_id": storage_object.storage_object_id,
        "recording_id": recording_id,
        "audio_session_id": audio_session_id,
        "root_trace_id": root_trace_id,
        "trace_id": root_trace_id,
        **source,
    }
    upsert_resource(
        session,
        ctx,
        "source_records",
        source_record_id,
        source_payload,
        status="succeeded",
        trace_id=root_trace_id,
    )

    materialization_id = _stable_id("mat_audio_import", *identity_parts)
    materialization = session.get(AssetMaterialization, materialization_id)
    if materialization is not None and (
        materialization.tenant_id != record.tenant_id
        or materialization.project_id != record.project_id
    ):
        raise ApiError(
            "AUDIO_IMPORT_MATERIALIZATION_CONFLICT",
            "音频导入资产物化标识已绑定其他作用域",
            409,
        )
    materialization_payload = {
        "materialization_id": materialization_id,
        "asset_key": (record.payload.get("target") or {}).get("target_asset_key"),
        "partition_key": source.get("started_at"),
        "source_record_id": source_record_id,
        "storage_object_id": storage_object.storage_object_id,
        "audio_session_id": audio_session_id,
        "import_batch_id": batch.import_batch_id,
        "root_trace_id": root_trace_id,
    }
    if materialization is None:
        session.add(
            AssetMaterialization(
                materialization_id=materialization_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                status="materialized",
                trace_id=root_trace_id,
                payload=materialization_payload,
            )
        )
    edge_id = _stable_id("lineage_audio_import", *identity_parts)
    lineage = session.get(AssetLineageEdge, edge_id)
    if lineage is not None and (
        lineage.tenant_id != record.tenant_id or lineage.project_id != record.project_id
    ):
        raise ApiError(
            "AUDIO_IMPORT_LINEAGE_CONFLICT",
            "音频导入血缘标识已绑定其他作用域",
            409,
        )
    if lineage is None:
        session.add(
            AssetLineageEdge(
                edge_id=edge_id,
                tenant_id=record.tenant_id,
                project_id=record.project_id,
                status="active",
                trace_id=root_trace_id,
                payload={
                    "edge_id": edge_id,
                    "source_type": "source_record",
                    "source_id": source_record_id,
                    "target_type": "audio_session",
                    "target_id": audio_session_id,
                    "import_batch_id": batch.import_batch_id,
                    "root_trace_id": root_trace_id,
                },
            )
        )
    return recording_id, audio_session_id, source_record_id, False


def _materialize_non_success_source_record(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    batch: ImportBatch,
    *,
    external_record_id: str,
    status: str,
    error_code: str | None,
    source: dict[str, Any],
) -> str:
    source_record_id = _stable_id(
        "source_record",
        record.tenant_id,
        record.project_id,
        batch.connector_id,
        external_record_id,
    )
    connector = record.payload.get("connector_snapshot")
    connector_snapshot = connector if isinstance(connector, dict) else {}
    upsert_resource(
        session,
        ctx,
        "source_records",
        source_record_id,
        {
            "id": source_record_id,
            "source_record_id": source_record_id,
            "external_record_id": external_record_id,
            "status": status,
            "error_code": error_code,
            "connector_id": batch.connector_id,
            "platform_connection_id": connector_snapshot.get("platform_connection_id"),
            "import_batch_id": batch.import_batch_id,
            "root_trace_id": batch.root_trace_id,
            "trace_id": batch.root_trace_id,
            **source,
        },
        status=status,
        trace_id=batch.root_trace_id,
    )
    return source_record_id


def _public_batch(batch: ImportBatch, *, audio_session_ids: list[str]) -> dict[str, Any]:
    return {
        "import_batch_id": batch.import_batch_id,
        "task_run_id": batch.task_run_id,
        "status": batch.status,
        "current_stage": batch.current_stage,
        "total_items": batch.total_items,
        "succeeded_items": batch.succeeded_items,
        "skipped_items": batch.skipped_items,
        "failed_items": batch.failed_items,
        "cursor_before": batch.cursor_before,
        "cursor_after": batch.cursor_after,
        "audio_session_ids": audio_session_ids,
        "root_trace_id": batch.root_trace_id,
        "trace_id": batch.trace_id,
    }


def finalize_audio_import_batch_from_task_terminal(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    *,
    reason: str,
) -> bool:
    """Project an operational TaskRun failure/cancellation into its import batch once."""

    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
        or record.status not in {"failed", "cancelled"}
    ):
        return False
    batch = _batch_for_run(session, record)
    if batch.status in {"partial", "succeeded", "failed", "cancelled"}:
        # A signed business completion (or a previous invocation of this helper)
        # is immutable and wins over any later operational observation.
        return False

    before = {"status": batch.status, "current_stage": batch.current_stage}
    terminal_status = record.status
    terminal_reason = str(reason).strip()[:500] or f"task_run_{terminal_status}"
    now = datetime.now(UTC)
    batch.status = terminal_status
    batch.current_stage = "completed"
    batch.cursor_after = None
    if batch.started_at is None:
        batch.started_at = now
    batch.finished_at = now
    batch.payload = {
        **batch.payload,
        "task_run_terminal_status": terminal_status,
        "task_run_terminal_reason": terminal_reason,
    }
    result = {
        **_public_batch(batch, audio_session_ids=[]),
        "task_run_terminal_status": terminal_status,
        "task_run_terminal_reason": terminal_reason,
    }
    event_ctx = replace(
        ctx,
        trace_id=batch.root_trace_id,
        parent_trace_id=(
            ctx.trace_id if ctx.trace_id != batch.root_trace_id else ctx.parent_trace_id
        ),
        correlation_id=batch.root_trace_id,
    )
    action = f"audio_import.batch_{terminal_status}"
    record_audit(
        session,
        event_ctx,
        action=action,
        object_type="import_batch",
        object_id=batch.import_batch_id,
        result=terminal_status,
        before=before,
        after=result,
        trace_id=batch.root_trace_id,
    )
    enqueue_event(
        session,
        event_ctx,
        event_type=action,
        aggregate_type="import_batch",
        aggregate_id=batch.import_batch_id,
        payload=result,
    )
    return True


def materialize_audio_import_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        record.run_type != "task_run"
        or record.payload.get("execution_contract") != AUDIO_IMPORT_EXECUTION_CONTRACT
    ):
        return None
    batch = _batch_for_run(session, record)
    completion_status = str(completion_receipt.get("status") or "failed")
    now = datetime.now(UTC)
    if completion_status != "success":
        batch.status = "failed"
        batch.current_stage = "completed"
        batch.cursor_after = None
        if batch.started_at is None:
            batch.started_at = now
        batch.finished_at = now
        batch.payload = {
            **batch.payload,
            "error_code": completion_receipt.get("error_code") or "AUDIO_IMPORT_EXECUTION_FAILED",
            "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
        }
        result = _public_batch(batch, audio_session_ids=[])
        record_audit(
            session,
            ctx,
            action="audio_import.batch_failed",
            object_type="import_batch",
            object_id=batch.import_batch_id,
            result="failed",
            after=result,
            trace_id=batch.root_trace_id,
        )
        enqueue_event(
            session,
            ctx,
            event_type="audio_import.batch_failed",
            aggregate_type="import_batch",
            aggregate_id=batch.import_batch_id,
            payload=result,
        )
        return result

    raw_result = completion_receipt.get("result_ref")
    if not isinstance(raw_result, dict):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_RESULT_INVALID",
            "音频导入完成回执缺少结果对象",
            422,
        )
    if raw_result.get("import_batch_id") != batch.import_batch_id:
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_BINDING_MISMATCH",
            "音频导入回执未绑定当前批次",
            409,
        )
    batch.current_stage = "materializing"
    raw_items = raw_result.get("items")
    if not isinstance(raw_items, list):
        raise ApiError(
            "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
            "音频导入回执的导入项必须是数组",
            422,
        )
    audio_session_ids: list[str] = []
    statuses: list[str] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "音频导入项必须是对象",
                422,
            )
        external_record_id = _required_text(
            raw_item.get("external_record_id"),
            field="external_record_id",
            maximum=512,
        )
        status = str(raw_item.get("status") or "")
        if status not in _EXECUTOR_ITEM_STATUSES:
            raise ApiError(
                "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                "音频导入项状态无效",
                422,
            )
        source = _source_metadata(raw_item.get("source"))
        error_code: str | None = None
        object_version: str | None = None
        audio_session_id: str | None = None
        materialized_status = status
        item_payload: dict[str, Any] = {"source": source}
        if status == "succeeded":
            object_version = _required_text(
                raw_item.get("object_version"),
                field="object_version",
                maximum=512,
            )
            (
                recording_id,
                audio_session_id,
                source_record_id,
                already_materialized,
            ) = _materialize_succeeded_item(
                session,
                ctx,
                record,
                batch,
                raw_item,
                external_record_id=external_record_id,
                source=source,
            )
            if already_materialized:
                materialized_status = "skipped"
            audio_session_ids.append(audio_session_id)
            item_payload.update(
                {
                    "storage_object_id": raw_item.get("storage_object_id"),
                    "recording_id": recording_id,
                    "source_record_id": source_record_id,
                    "deduplicated": already_materialized,
                }
            )
        else:
            error_code = _required_text(
                raw_item.get("error_code"),
                field="error_code",
                maximum=128,
            )
            if not _ERROR_CODE_PATTERN.fullmatch(error_code):
                raise ApiError(
                    "AUDIO_IMPORT_COMPLETION_ITEMS_INVALID",
                    "音频导入失败项 error_code 无效",
                    422,
                )
            source_record_id = _materialize_non_success_source_record(
                session,
                ctx,
                record,
                batch,
                external_record_id=external_record_id,
                status=status,
                error_code=error_code,
                source=source,
            )
            item_payload["source_record_id"] = source_record_id
        _upsert_import_item(
            session,
            record=record,
            batch=batch,
            external_record_id=external_record_id,
            status=materialized_status,
            error_code=error_code,
            object_version=object_version,
            audio_session_id=audio_session_id,
            payload=item_payload,
        )
        statuses.append(materialized_status)

    total = len(statuses)
    succeeded = statuses.count("succeeded")
    skipped = statuses.count("skipped")
    failed = statuses.count("failed")
    batch.total_items = total
    batch.succeeded_items = succeeded
    batch.skipped_items = skipped
    batch.failed_items = failed
    batch.finished_at = now
    if failed == 0:
        batch.status = "succeeded"
        candidate = raw_result.get("next_cursor_candidate")
        batch.cursor_after = (
            _required_text(candidate, field="next_cursor_candidate", maximum=1024)
            if candidate is not None
            else batch.cursor_before
        )
        advance_connector_sync_cursor(
            session,
            ctx,
            batch.connector_id,
            expected_cursor=batch.cursor_before,
            next_cursor=batch.cursor_after,
            import_batch_id=batch.import_batch_id,
            trace_id=batch.root_trace_id,
        )
    elif succeeded + skipped > 0:
        batch.status = "partial"
        batch.cursor_after = None
    else:
        batch.status = "failed"
        batch.cursor_after = None
    batch.payload = {
        **batch.payload,
        "completion_receipt_id": completion_receipt.get("completion_receipt_id"),
        "manifest_storage_object_id": raw_result.get("manifest_storage_object_id"),
        "manifest_sha256": raw_result.get("manifest_sha256"),
        "materialized_audio_session_ids": audio_session_ids,
    }
    batch.current_stage = "completed"
    result = _public_batch(batch, audio_session_ids=audio_session_ids)
    record_audit(
        session,
        ctx,
        action="audio_import.batch_materialized",
        object_type="import_batch",
        object_id=batch.import_batch_id,
        result=batch.status,
        after=result,
        trace_id=batch.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="audio_import.batch_materialized",
        aggregate_type="import_batch",
        aggregate_id=batch.import_batch_id,
        payload=result,
    )
    return result
