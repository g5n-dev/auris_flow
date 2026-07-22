from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import RunRecord, StorageObject
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STORAGE_OBJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
MAX_COMPLETION_OBJECTS = 32
MAX_STORAGE_OBJECT_SIZE_BYTES = 5 * 1024 * 1024 * 1024
AUDIO_PENDING_COMPLETION_STATUS = "pending_completion_binding"
AUDIO_REJECTED_COMPLETION_STATUS = "rejected"

_DESCRIPTOR_FIELDS = frozenset(
    {
        "storage_object_id",
        "role",
        "provider",
        "bucket",
        "object_key",
        "content_type",
        "size_bytes",
        "content_sha256",
        "etag",
        "version_id",
        # These fields are intentionally accepted but never trusted. The server
        # replaces them with values frozen on the run and request context.
        "tenant_id",
        "project_id",
        "source_type",
        "source_id",
        "status",
        "trace_id",
        "object_key_sha256",
    }
)

_ROLE_CONTENT_TYPES: dict[str, frozenset[str]] = {
    "manifest": frozenset({"application/json"}),
    "provider_artifact": frozenset(
        {"application/json", "application/octet-stream", "application/zip"}
    ),
    "eval_result": frozenset({"application/json", "application/x-ndjson"}),
    "word_timestamps": frozenset(
        {"application/json", "application/x-ndjson", "application/parquet"}
    ),
    "diagnostics": frozenset({"application/json", "application/x-ndjson"}),
    "badcase_evidence": frozenset({"application/json", "application/x-ndjson"}),
    "asset_materialization": frozenset(
        {
            "application/json",
            "application/x-ndjson",
            "application/parquet",
            "application/vnd.apache.parquet",
        }
    ),
}


@dataclass(frozen=True)
class _ExpectedObject:
    role: str
    content_sha256: str | None


@dataclass(frozen=True)
class _Descriptor:
    storage_object_id: str
    role: str
    provider: str
    bucket: str
    object_key: str
    content_type: str
    size_bytes: int
    content_sha256: str
    etag: str | None
    version_id: str | None


def _text(raw: Any, *, field: str, max_length: int) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            f"storage_objects.{field} 必须是非空字符串",
            422,
            details=[{"field": field}],
        )
    value = raw.strip()
    if len(value) > max_length or any(ord(char) < 32 for char in value):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            f"storage_objects.{field} 长度或字符无效",
            422,
            details=[{"field": field}],
        )
    return value


def _validate_frozen_bindings(
    record: RunRecord,
    result_ref: dict[str, Any],
    fields: tuple[str, ...],
) -> None:
    mismatches = [
        {
            "field": field,
            "expected": record.payload.get(field),
            "actual": result_ref.get(field),
        }
        for field in fields
        if result_ref.get(field) != record.payload.get(field)
    ]
    if mismatches:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_BINDING_MISMATCH",
            "对象登记描述符未绑定运行发起时冻结的热词版本、数据集或产物",
            409,
            details=mismatches,
        )


def _expected_objects(
    record: RunRecord,
    result_ref: dict[str, Any],
) -> dict[str, _ExpectedObject]:
    if record.run_type == "audio_intelligence":
        storage_object_id = result_ref.get("result_manifest_storage_object_id")
        manifest_sha256 = result_ref.get("result_manifest_sha256")
        if (
            not isinstance(storage_object_id, str)
            or not STORAGE_OBJECT_ID_PATTERN.fullmatch(storage_object_id)
            or not isinstance(manifest_sha256, str)
            or not SHA256_PATTERN.fullmatch(manifest_sha256)
        ):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "音频结果 manifest 必须绑定对象 ID 和 SHA-256",
                422,
            )
        return {
            storage_object_id: _ExpectedObject("manifest", manifest_sha256),
        }

    if record.run_type in {"asset_backfill", "asset_check_retry"}:
        raw_asset_ids: list[Any] = []
        direct_id = result_ref.get("storage_object_id")
        if direct_id is not None:
            raw_asset_ids.append(direct_id)
        listed_ids = result_ref.get("storage_object_ids")
        if listed_ids is not None:
            if not isinstance(listed_ids, list):
                raise ApiError(
                    "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                    "数据资产物化的 storage_object_ids 必须是对象 ID 列表",
                    422,
                )
            raw_asset_ids.extend(listed_ids)
        if not raw_asset_ids or any(
            not isinstance(value, str) or not value.strip() for value in raw_asset_ids
        ):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "数据资产物化对象登记要求至少一个 storage_object_id 引用",
                422,
            )
        normalized_asset_ids = [value.strip() for value in raw_asset_ids]
        if len(set(normalized_asset_ids)) != len(normalized_asset_ids):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "数据资产物化对象引用不能重复",
                422,
            )
        return {
            storage_object_id: _ExpectedObject("asset_materialization", None)
            for storage_object_id in normalized_asset_ids
        }

    if record.run_type == "hotword_analysis":
        snapshots = result_ref.get("metric_snapshots")
        candidates = result_ref.get("badcase_candidates", [])
        if not isinstance(snapshots, list) or not isinstance(candidates, list):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "热词分析完成结果必须包含指标快照和 Badcase 候选数组",
                422,
            )
        expected: dict[str, _ExpectedObject] = {}

        def bind(raw_id: Any, role: str) -> None:
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ApiError(
                    "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                    f"热词分析 {role} 必须引用非空 StorageObject ID",
                    422,
                )
            storage_object_id = raw_id.strip()
            prior = expected.get(storage_object_id)
            if prior is not None and prior.role != role:
                raise ApiError(
                    "RUN_COMPLETION_STORAGE_ROLE_MISMATCH",
                    "同一热词分析对象不得同时冒充不同证据角色",
                    409,
                    details=[{"storage_object_id": storage_object_id}],
                )
            expected[storage_object_id] = _ExpectedObject(role, None)

        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise ApiError(
                    "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                    "热词分析指标快照必须是对象",
                    422,
                )
            bind(snapshot.get("diagnostics_storage_object_id"), "diagnostics")
            is_term_level = bool(snapshot.get("standard_term")) or any(
                field in snapshot
                for field in (
                    "expected_count",
                    "correct_count",
                    "weighted_error_count",
                    "recognized_hotword_count",
                    "false_insert_count",
                )
            )
            if is_term_level:
                bind(snapshot.get("word_timestamps_storage_object_id"), "word_timestamps")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ApiError(
                    "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                    "热词分析 Badcase 候选必须是对象",
                    422,
                )
            bind(candidate.get("evidence_storage_object_id"), "badcase_evidence")
        if not expected:
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "热词分析必须产生可验证的诊断或证据对象",
                422,
            )
        return expected

    if record.run_type == "hotword_build":
        _validate_frozen_bindings(
            record,
            result_ref,
            ("hotword_pack_version_id", "content_sha256", "provider"),
        )
        manifest_id = result_ref.get("manifest_storage_object_id")
        artifact_id = result_ref.get("provider_artifact_ref")
        artifact_sha256 = result_ref.get("artifact_sha256")
        if (
            not isinstance(manifest_id, str)
            or not manifest_id.strip()
            or not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or not isinstance(artifact_sha256, str)
            or not SHA256_PATTERN.fullmatch(artifact_sha256.lower())
        ):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "热词构建对象登记要求 manifest、provider 产物及其 SHA-256 引用",
                422,
            )
        content_sha256 = str(record.payload.get("content_sha256") or "").lower()
        if not SHA256_PATTERN.fullmatch(content_sha256):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_FROZEN_HASH_INVALID",
                "热词构建运行缺少冻结的内容 SHA-256",
                409,
            )
        expected = {
            manifest_id.strip(): _ExpectedObject("manifest", content_sha256),
            artifact_id.strip(): _ExpectedObject("provider_artifact", artifact_sha256.lower()),
        }
        if len(expected) != 2:
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "manifest 与 provider 编译产物必须引用两个不同对象",
                422,
            )
        return expected

    if record.run_type == "hotword_eval":
        _validate_frozen_bindings(
            record,
            result_ref,
            (
                "hotword_pack_version_id",
                "baseline_version_id",
                "eval_dataset_id",
                "content_sha256",
                "manifest_storage_object_id",
                "provider",
                "provider_artifact_ref",
                "artifact_sha256",
            ),
        )
        raw_ids = result_ref.get("result_storage_object_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or any(not isinstance(value, str) or not value.strip() for value in raw_ids)
        ):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "热词评测对象登记要求至少一个结果对象引用",
                422,
            )
        normalized_ids = [value.strip() for value in raw_ids]
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ApiError(
                "RUN_COMPLETION_STORAGE_REFERENCES_INVALID",
                "热词评测结果对象引用不能重复",
                422,
            )
        return {
            storage_object_id: _ExpectedObject("eval_result", None)
            for storage_object_id in normalized_ids
        }

    raise ApiError(
        "RUN_COMPLETION_STORAGE_DESCRIPTORS_NOT_ALLOWED",
        f"{record.run_type} 完成回执不允许登记 storage_objects",
        422,
    )


def _descriptor(
    raw: Any,
    *,
    record: RunRecord,
    expected: dict[str, _ExpectedObject],
) -> _Descriptor:
    if not isinstance(raw, dict):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            "storage_objects 每一项都必须是对象",
            422,
        )
    unknown_fields = sorted(set(raw) - _DESCRIPTOR_FIELDS)
    if unknown_fields:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            "storage_objects 包含未允许字段",
            422,
            details=[{"unknown_fields": unknown_fields}],
        )

    storage_object_id = _text(
        raw.get("storage_object_id"), field="storage_object_id", max_length=128
    )
    if not STORAGE_OBJECT_ID_PATTERN.fullmatch(storage_object_id):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            "storage_object_id 格式无效",
            422,
        )
    expected_object = expected.get(storage_object_id)
    if expected_object is None:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_REFERENCE_MISMATCH",
            "登记对象 ID 不在完成结果引用中",
            409,
            details=[{"storage_object_id": storage_object_id}],
        )

    role = _text(raw.get("role"), field="role", max_length=32)
    if role != expected_object.role:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_ROLE_MISMATCH",
            "登记对象角色与完成结果引用不一致",
            409,
            details=[
                {
                    "storage_object_id": storage_object_id,
                    "expected": expected_object.role,
                    "actual": role,
                }
            ],
        )

    settings = get_settings()
    provider = _text(raw.get("provider"), field="provider", max_length=32).lower()
    configured_provider = settings.object_storage_provider.strip().lower()
    if not PROVIDER_PATTERN.fullmatch(provider) or provider != configured_provider:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_LOCATOR_INVALID",
            "对象 provider 必须匹配当前项目配置的对象存储 provider",
            422,
            details=[{"expected": configured_provider, "actual": provider}],
        )

    bucket = _text(raw.get("bucket"), field="bucket", max_length=255)
    configured_buckets = {
        settings.object_storage_bucket.strip(),
        *(
            item.strip()
            for item in settings.object_storage_allowed_buckets.split(",")
            if item.strip()
        ),
    }
    if not BUCKET_PATTERN.fullmatch(bucket) or bucket not in configured_buckets:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_LOCATOR_INVALID",
            "对象 bucket 不在当前服务允许列表中",
            422,
            details=[{"bucket": bucket}],
        )

    object_key = _text(raw.get("object_key"), field="object_key", max_length=1024)
    expected_prefix = (
        f"tenants/{record.tenant_id}/projects/{record.project_id}/runs/{record.run_id}/"
    )
    key_parts = object_key.split("/")
    if (
        not object_key.startswith(expected_prefix)
        or object_key.startswith("/")
        or "\\" in object_key
        or any(part in {"", ".", ".."} for part in key_parts)
    ):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_LOCATOR_INVALID",
            "对象 key 必须位于当前租户、项目和运行的隔离前缀下",
            422,
            details=[{"expected_prefix": expected_prefix}],
        )

    content_type = _text(raw.get("content_type"), field="content_type", max_length=128).lower()
    allowed_content_types = _ROLE_CONTENT_TYPES[role]
    if content_type not in allowed_content_types:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_CONTENT_TYPE_INVALID",
            "对象 content_type 与角色不匹配",
            422,
            details=[
                {
                    "role": role,
                    "allowed_content_types": sorted(allowed_content_types),
                    "actual": content_type,
                }
            ],
        )

    size_bytes = raw.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or size_bytes > MAX_STORAGE_OBJECT_SIZE_BYTES
    ):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_SIZE_INVALID",
            "对象 size_bytes 必须处于 1 字节到 5 GiB",
            422,
        )

    content_sha256 = _text(raw.get("content_sha256"), field="content_sha256", max_length=64).lower()
    if not SHA256_PATTERN.fullmatch(content_sha256):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_HASH_INVALID",
            "对象 content_sha256 必须是 64 位十六进制",
            422,
        )
    if (
        expected_object.content_sha256 is not None
        and content_sha256 != expected_object.content_sha256
    ):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_HASH_MISMATCH",
            "对象内容哈希与运行冻结绑定不一致",
            409,
            details=[
                {
                    "storage_object_id": storage_object_id,
                    "expected": expected_object.content_sha256,
                    "actual": content_sha256,
                }
            ],
        )

    raw_etag = raw.get("etag")
    etag = None if raw_etag is None else _text(raw_etag, field="etag", max_length=255)
    raw_version_id = raw.get("version_id")
    version_id = (
        None
        if raw_version_id is None
        else _text(raw_version_id, field="version_id", max_length=1024)
    )
    if record.run_type == "audio_intelligence" and (
        version_id is None or version_id.casefold() == "null"
    ):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_VERSION_REQUIRED",
            "音频结果 manifest 必须绑定精确对象版本",
            422,
        )
    return _Descriptor(
        storage_object_id=storage_object_id,
        role=role,
        provider=provider,
        bucket=bucket,
        object_key=object_key,
        content_type=content_type,
        size_bytes=size_bytes,
        content_sha256=content_sha256,
        etag=etag,
        version_id=version_id,
    )


def _matches_existing(
    existing: StorageObject,
    *,
    record: RunRecord,
    descriptor: _Descriptor,
    trace_id: str,
) -> bool:
    return all(
        (
            existing.storage_object_id == descriptor.storage_object_id,
            existing.tenant_id == record.tenant_id,
            existing.project_id == record.project_id,
            existing.provider == descriptor.provider,
            existing.bucket == descriptor.bucket,
            existing.object_key == descriptor.object_key,
            existing.object_key_sha256
            == hashlib.sha256(descriptor.object_key.encode("utf-8")).hexdigest(),
            existing.source_type == record.run_type,
            existing.source_id == record.run_id,
            existing.content_type == descriptor.content_type,
            existing.size_bytes == descriptor.size_bytes,
            str(existing.content_sha256 or "").lower() == descriptor.content_sha256,
            existing.etag == descriptor.etag,
            (existing.payload or {}).get("object_version_id") == descriptor.version_id,
            existing.status == "verified",
            existing.trace_id == trace_id,
        )
    )


def _matches_pending_audio_completion(
    existing: StorageObject,
    *,
    record: RunRecord,
    descriptor: _Descriptor,
    completion_receipt_id: str,
    trace_id: str,
) -> bool:
    payload = existing.payload if isinstance(existing.payload, dict) else {}
    return all(
        (
            record.run_type == "audio_intelligence",
            existing.storage_object_id == descriptor.storage_object_id,
            existing.tenant_id == record.tenant_id,
            existing.project_id == record.project_id,
            existing.provider == descriptor.provider,
            existing.bucket == descriptor.bucket,
            existing.object_key == descriptor.object_key,
            existing.object_key_sha256
            == hashlib.sha256(descriptor.object_key.encode("utf-8")).hexdigest(),
            existing.source_type == record.run_type,
            existing.source_id == record.run_id,
            existing.content_type == descriptor.content_type,
            existing.size_bytes == descriptor.size_bytes,
            str(existing.content_sha256 or "").lower() == descriptor.content_sha256,
            existing.etag == descriptor.etag,
            payload.get("object_version_id") == descriptor.version_id,
            payload.get("completion_receipt_id") == completion_receipt_id,
            payload.get("role") == descriptor.role,
            existing.status == AUDIO_PENDING_COMPLETION_STATUS,
            existing.trace_id == trace_id,
        )
    )


def _storage_registration_payload(
    storage_object: StorageObject,
    *,
    role: str,
    root_trace_id: str,
    expose_locator_context: bool,
) -> dict[str, Any]:
    """Build the audit/outbox projection without ever exposing an object key.

    Objects first accepted before a launch binding are even stricter: the bucket
    and provider remain solely in the scoped StorageObject ledger until the
    receipt is either verified or rejected.
    """

    return {
        "storage_object_id": storage_object.storage_object_id,
        "role": role,
        **(
            {
                "provider": storage_object.provider,
                "bucket": storage_object.bucket,
            }
            if expose_locator_context
            else {}
        ),
        "object_key_sha256": storage_object.object_key_sha256,
        "content_type": storage_object.content_type,
        "size_bytes": storage_object.size_bytes,
        "content_sha256": storage_object.content_sha256,
        "source_type": storage_object.source_type,
        "source_id": storage_object.source_id,
        "status": storage_object.status,
        "root_trace_id": root_trace_id,
    }


def _emit_storage_object_registered(
    session: Session,
    ctx: RequestContext,
    storage_object: StorageObject,
    *,
    role: str,
    root_trace_id: str,
    expose_locator_context: bool,
) -> None:
    storage_payload = _storage_registration_payload(
        storage_object,
        role=role,
        root_trace_id=root_trace_id,
        expose_locator_context=expose_locator_context,
    )
    lineage_ctx = replace(
        ctx,
        trace_id=root_trace_id,
        correlation_id=ctx.correlation_id or root_trace_id,
    )
    record_audit(
        session,
        ctx,
        action="storage_object.registered",
        object_type="storage_object",
        object_id=storage_object.storage_object_id,
        result="success",
        after=storage_payload,
        trace_id=root_trace_id,
    )
    enqueue_event(
        session,
        lineage_ctx,
        event_type="storage_object.registered",
        aggregate_type="storage_object",
        aggregate_id=storage_object.storage_object_id,
        payload=storage_payload,
    )


def _register_descriptor(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    descriptor: _Descriptor,
    staged_completion_receipt_id: str | None = None,
) -> StorageObject:
    object_key_sha256 = hashlib.sha256(descriptor.object_key.encode("utf-8")).hexdigest()
    root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id or ctx.trace_id)
    existing = session.scalar(
        select(StorageObject)
        .where(
            or_(
                StorageObject.storage_object_id == descriptor.storage_object_id,
                (
                    (StorageObject.tenant_id == record.tenant_id)
                    & (StorageObject.project_id == record.project_id)
                    & (StorageObject.provider == descriptor.provider)
                    & (StorageObject.bucket == descriptor.bucket)
                    & (StorageObject.object_key_sha256 == object_key_sha256)
                ),
            )
        )
        .with_for_update()
    )
    if existing is not None:
        if staged_completion_receipt_id and _matches_pending_audio_completion(
            existing,
            record=record,
            descriptor=descriptor,
            completion_receipt_id=staged_completion_receipt_id,
            trace_id=root_trace_id,
        ):
            existing.status = "verified"
            existing.payload = {
                **(existing.payload or {}),
                "registration_mode": "trusted_staged_run_completion",
                "verified_completion_receipt_id": staged_completion_receipt_id,
                "verified_completion_trace_id": ctx.trace_id,
            }
            _emit_storage_object_registered(
                session,
                ctx,
                existing,
                role=descriptor.role,
                root_trace_id=root_trace_id,
                expose_locator_context=False,
            )
            return existing
        if _matches_existing(
            existing,
            record=record,
            descriptor=descriptor,
            trace_id=root_trace_id,
        ):
            return existing
        raise ApiError(
            "RUN_COMPLETION_STORAGE_COLLISION",
            "对象 ID 或 locator 已被不同内容、来源或作用域占用",
            409,
            details=[
                {
                    "storage_object_id": descriptor.storage_object_id,
                    "existing_storage_object_id": existing.storage_object_id,
                }
            ],
        )

    storage_object = StorageObject(
        storage_object_id=descriptor.storage_object_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        provider=descriptor.provider,
        bucket=descriptor.bucket,
        object_key=descriptor.object_key,
        object_key_sha256=object_key_sha256,
        source_type=record.run_type,
        source_id=record.run_id,
        content_type=descriptor.content_type,
        size_bytes=descriptor.size_bytes,
        content_sha256=descriptor.content_sha256,
        etag=descriptor.etag,
        status="verified",
        trace_id=root_trace_id,
        payload={
            "registration_mode": "trusted_run_completion",
            "role": descriptor.role,
            "run_id": record.run_id,
            "run_type": record.run_type,
            "root_trace_id": root_trace_id,
            "completion_trace_id": ctx.trace_id,
            **(
                {"object_version_id": descriptor.version_id}
                if descriptor.version_id is not None
                else {}
            ),
        },
    )
    try:
        with session.begin_nested():
            session.add(storage_object)
            session.flush([storage_object])
    except IntegrityError as exc:
        collision = session.scalar(
            select(StorageObject).where(
                or_(
                    StorageObject.storage_object_id == descriptor.storage_object_id,
                    (
                        (StorageObject.tenant_id == record.tenant_id)
                        & (StorageObject.project_id == record.project_id)
                        & (StorageObject.provider == descriptor.provider)
                        & (StorageObject.bucket == descriptor.bucket)
                        & (StorageObject.object_key_sha256 == object_key_sha256)
                    ),
                )
            )
        )
        if collision is not None and _matches_existing(
            collision,
            record=record,
            descriptor=descriptor,
            trace_id=root_trace_id,
        ):
            return collision
        raise ApiError(
            "RUN_COMPLETION_STORAGE_COLLISION",
            "对象 ID 或 locator 并发登记冲突",
            409,
            details=[{"storage_object_id": descriptor.storage_object_id}],
        ) from exc
    _emit_storage_object_registered(
        session,
        ctx,
        storage_object,
        role=descriptor.role,
        root_trace_id=root_trace_id,
        expose_locator_context=True,
    )
    return storage_object


def _validated_completion_descriptors(
    record: RunRecord,
    raw_result_ref: Any,
) -> list[_Descriptor]:
    if not isinstance(raw_result_ref, dict):
        return []
    if "storage_objects" not in raw_result_ref:
        if record.run_type == "hotword_analysis":
            raise ApiError(
                "RUN_COMPLETION_STORAGE_DESCRIPTORS_REQUIRED",
                "热词分析完成回执必须原子登记诊断、词级时间戳和 Badcase 证据对象",
                422,
            )
        return []
    raw_descriptors = raw_result_ref.get("storage_objects")
    if (
        not isinstance(raw_descriptors, list)
        or not raw_descriptors
        or len(raw_descriptors) > MAX_COMPLETION_OBJECTS
    ):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTORS_INVALID",
            f"storage_objects 必须包含 1 到 {MAX_COMPLETION_OBJECTS} 个描述符",
            422,
        )

    expected = _expected_objects(record, raw_result_ref)
    descriptors = [_descriptor(raw, record=record, expected=expected) for raw in raw_descriptors]
    descriptor_ids = [item.storage_object_id for item in descriptors]
    if len(set(descriptor_ids)) != len(descriptor_ids):
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTORS_INVALID",
            "storage_objects 不能重复登记同一对象",
            422,
        )
    missing_ids = sorted(set(expected) - set(descriptor_ids))
    if missing_ids:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_MISSING",
            "完成结果引用的对象缺少登记描述符",
            422,
            details=[{"missing_storage_object_ids": missing_ids}],
        )
    return descriptors


def _validate_pending_audio_receipt_binding(
    raw_result_ref: Any,
    descriptor: _Descriptor,
) -> None:
    if not isinstance(raw_result_ref, dict):
        raise ApiError(
            "AUDIO_RESULT_MANIFEST_RECEIPT_INVALID",
            "早到音频完成回执缺少 manifest 引用",
            422,
        )
    manifest_sha256 = str(raw_result_ref.get("result_manifest_sha256") or "")
    object_key_sha256 = str(raw_result_ref.get("result_manifest_object_key_sha256") or "")
    version_id_sha256 = str(raw_result_ref.get("result_manifest_version_id_sha256") or "")
    expected_storage_object_id = f"sto_audio_manifest_{manifest_sha256[:32]}"
    expected_key_sha256 = hashlib.sha256(descriptor.object_key.encode("utf-8")).hexdigest()
    expected_version_sha256 = hashlib.sha256(str(descriptor.version_id).encode("utf-8")).hexdigest()
    if (
        raw_result_ref.get("manifest_version") != "auris-flow-audio-result-receipt-v1"
        or raw_result_ref.get("status") != "materialized"
        or raw_result_ref.get("result_manifest_schema") != "auris-flow-audio-result-manifest-v1"
        or descriptor.storage_object_id != expected_storage_object_id
        or raw_result_ref.get("result_manifest_storage_object_id") != descriptor.storage_object_id
        or descriptor.role != "manifest"
        or descriptor.content_type != "application/json"
        or descriptor.content_sha256 != manifest_sha256
        or not hmac.compare_digest(object_key_sha256, expected_key_sha256)
        or not hmac.compare_digest(version_id_sha256, expected_version_sha256)
    ):
        raise ApiError(
            "AUDIO_RESULT_MANIFEST_PENDING_BINDING_MISMATCH",
            "早到音频 manifest 描述符与签名回执哈希绑定不一致",
            409,
        )


def stage_audio_completion_storage_object(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    raw_result_ref: Any,
    *,
    completion_receipt_id: str,
) -> StorageObject:
    """Persist an untrusted locator behind the run scope until launch binding exists."""

    if record.run_type != "audio_intelligence":
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTORS_NOT_ALLOWED",
            "只有音频智能运行允许暂存早到结果对象",
            422,
        )
    descriptors = _validated_completion_descriptors(record, raw_result_ref)
    if len(descriptors) != 1:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTORS_REQUIRED",
            "早到音频完成回执必须携带唯一 manifest 对象描述符",
            422,
        )
    descriptor = descriptors[0]
    _validate_pending_audio_receipt_binding(raw_result_ref, descriptor)
    if descriptor.etag is not None:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_DESCRIPTOR_INVALID",
            "早到音频 manifest 描述符不得携带未纳入结果协议的 etag",
            422,
        )
    object_key_sha256 = hashlib.sha256(descriptor.object_key.encode("utf-8")).hexdigest()
    root_trace_id = str(record.payload.get("root_trace_id") or record.trace_id or ctx.trace_id)
    existing = session.scalar(
        select(StorageObject)
        .where(
            or_(
                StorageObject.storage_object_id == descriptor.storage_object_id,
                (
                    (StorageObject.tenant_id == record.tenant_id)
                    & (StorageObject.project_id == record.project_id)
                    & (StorageObject.provider == descriptor.provider)
                    & (StorageObject.bucket == descriptor.bucket)
                    & (StorageObject.object_key_sha256 == object_key_sha256)
                ),
            )
        )
        .with_for_update()
    )
    if existing is not None:
        if _matches_pending_audio_completion(
            existing,
            record=record,
            descriptor=descriptor,
            completion_receipt_id=completion_receipt_id,
            trace_id=root_trace_id,
        ):
            return existing
        raise ApiError(
            "RUN_COMPLETION_STORAGE_COLLISION",
            "早到音频对象 ID 或 locator 已被其他运行、作用域或回执占用",
            409,
            details=[{"storage_object_id": descriptor.storage_object_id}],
        )

    pending = StorageObject(
        storage_object_id=descriptor.storage_object_id,
        tenant_id=record.tenant_id,
        project_id=record.project_id,
        provider=descriptor.provider,
        bucket=descriptor.bucket,
        object_key=descriptor.object_key,
        object_key_sha256=object_key_sha256,
        source_type=record.run_type,
        source_id=record.run_id,
        content_type=descriptor.content_type,
        size_bytes=descriptor.size_bytes,
        content_sha256=descriptor.content_sha256,
        etag=descriptor.etag,
        status=AUDIO_PENDING_COMPLETION_STATUS,
        trace_id=root_trace_id,
        payload={
            "registration_mode": "signed_completion_pending_binding",
            "role": descriptor.role,
            "run_id": record.run_id,
            "run_type": record.run_type,
            "root_trace_id": root_trace_id,
            "completion_receipt_id": completion_receipt_id,
            "completion_trace_id": ctx.trace_id,
            "object_version_id": descriptor.version_id,
        },
    )
    try:
        with session.begin_nested():
            session.add(pending)
            session.flush([pending])
    except IntegrityError as exc:
        raise ApiError(
            "RUN_COMPLETION_STORAGE_COLLISION",
            "早到音频对象并发暂存冲突",
            409,
            details=[{"storage_object_id": descriptor.storage_object_id}],
        ) from exc
    return pending


def hydrate_staged_audio_result_ref(
    session: Session,
    record: RunRecord,
    sanitized_result_ref: Any,
    *,
    completion_receipt_id: str,
) -> dict[str, Any]:
    """Reconstruct the sole internal locator after the trusted launch is committed."""

    if record.run_type != "audio_intelligence" or not isinstance(sanitized_result_ref, dict):
        raise ApiError(
            "AUDIO_RESULT_MANIFEST_PENDING_OBJECT_INVALID",
            "早到音频完成回执缺少受控 manifest 引用",
            409,
        )
    storage_object_id = sanitized_result_ref.get("result_manifest_storage_object_id")
    if not isinstance(storage_object_id, str) or not storage_object_id:
        raise ApiError(
            "AUDIO_RESULT_MANIFEST_PENDING_OBJECT_INVALID",
            "早到音频完成回执缺少 manifest StorageObject ID",
            409,
        )
    pending = session.scalar(
        select(StorageObject)
        .where(
            StorageObject.storage_object_id == storage_object_id,
            StorageObject.tenant_id == record.tenant_id,
            StorageObject.project_id == record.project_id,
            StorageObject.source_type == record.run_type,
            StorageObject.source_id == record.run_id,
        )
        .with_for_update()
    )
    payload = pending.payload if pending is not None and isinstance(pending.payload, dict) else {}
    version_id = payload.get("object_version_id")
    if (
        pending is None
        or pending.status != AUDIO_PENDING_COMPLETION_STATUS
        or payload.get("completion_receipt_id") != completion_receipt_id
        or payload.get("role") != "manifest"
        or not isinstance(version_id, str)
        or not version_id
        or pending.object_key_sha256
        != sanitized_result_ref.get("result_manifest_object_key_sha256")
        or hashlib.sha256(version_id.encode("utf-8")).hexdigest()
        != sanitized_result_ref.get("result_manifest_version_id_sha256")
        or str(pending.content_sha256 or "").lower()
        != sanitized_result_ref.get("result_manifest_sha256")
    ):
        raise ApiError(
            "AUDIO_RESULT_MANIFEST_PENDING_OBJECT_MISMATCH",
            "暂存 manifest 与当前租户、项目、运行或签名回执绑定不一致",
            409,
            details=[{"storage_object_id": storage_object_id}],
        )
    hydrated = {
        **sanitized_result_ref,
        "storage_objects": [
            {
                "storage_object_id": pending.storage_object_id,
                "role": "manifest",
                "provider": pending.provider,
                "bucket": pending.bucket,
                "object_key": pending.object_key,
                "version_id": version_id,
                "content_type": pending.content_type,
                "size_bytes": pending.size_bytes,
                "content_sha256": pending.content_sha256,
            }
        ],
    }
    # Run the full descriptor policy again after reconstructing the private
    # locator so a mutated ledger row cannot bypass prefix, bucket or hash gates.
    _validated_completion_descriptors(record, hydrated)
    return hydrated


def reject_staged_audio_completion_storage_object(
    session: Session,
    record: RunRecord,
    sanitized_result_ref: Any,
    *,
    completion_receipt_id: str,
    rejection_code: str,
) -> None:
    if record.run_type != "audio_intelligence" or not isinstance(sanitized_result_ref, dict):
        return
    storage_object_id = sanitized_result_ref.get("result_manifest_storage_object_id")
    if not isinstance(storage_object_id, str) or not storage_object_id:
        return
    pending = session.scalar(
        select(StorageObject)
        .where(
            StorageObject.storage_object_id == storage_object_id,
            StorageObject.tenant_id == record.tenant_id,
            StorageObject.project_id == record.project_id,
            StorageObject.source_type == record.run_type,
            StorageObject.source_id == record.run_id,
            StorageObject.status == AUDIO_PENDING_COMPLETION_STATUS,
        )
        .with_for_update()
    )
    if pending is None:
        return
    payload = pending.payload if isinstance(pending.payload, dict) else {}
    if payload.get("completion_receipt_id") != completion_receipt_id:
        return
    pending.status = AUDIO_REJECTED_COMPLETION_STATUS
    pending.payload = {
        **payload,
        "rejection_code": rejection_code,
    }


def register_hotword_completion_storage_objects(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    raw_result_ref: Any,
    *,
    staged_completion_receipt_id: str | None = None,
) -> list[dict[str, Any]]:
    """Register trusted completion artifacts in the completion transaction.

    Legacy build/eval and already-governed asset workers may pre-register objects. Analysis
    requires descriptors; every supplied reference is bound to the immutable RunRecord.
    """

    descriptors = _validated_completion_descriptors(record, raw_result_ref)
    if not descriptors:
        return []

    registered = [
        _register_descriptor(
            session,
            ctx,
            record,
            descriptor,
            staged_completion_receipt_id=staged_completion_receipt_id,
        )
        for descriptor in descriptors
    ]
    return [
        {
            "storage_object_id": item.storage_object_id,
            "source_type": item.source_type,
            "source_id": item.source_id,
            "status": item.status,
            "trace_id": item.trace_id,
        }
        for item in registered
    ]
