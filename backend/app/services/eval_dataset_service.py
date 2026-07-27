from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, TypedDict
from urllib.error import HTTPError, URLError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import server_generated_public_id
from app.models import EvalDatasetVersion, StorageObject
from app.schemas import EvalDatasetVersionCreateRequest
from app.services.adapters import (
    SUPPORTED_OBJECT_STORAGE_PROVIDERS,
    object_storage_client_for_provider,
)
from app.services.audit_service import record_audit
from app.services.outbox_service import enqueue_event
from app.services.resource_service import upsert_resource

LOCKED_DATASET_STATUS = "locked"
VERIFIED_STORAGE_STATUSES = frozenset({"verified", "available"})
EVAL_DATASET_MANIFEST_SOURCE_TYPE = "eval_dataset_manifest"
EVAL_DATASET_MANIFEST_CONTENT_TYPES = frozenset(
    {"application/json", "application/ndjson", "application/x-ndjson"}
)
MOCK_OBJECT_STORAGE_PROVIDERS = frozenset({"mock", "test"})


class _ManifestSnapshot(TypedDict):
    manifest_provider: str
    manifest_bucket: str
    manifest_object_key: str
    manifest_content_type: str
    manifest_size_bytes: int
    manifest_etag: str


def _new_dataset_id() -> str:
    return server_generated_public_id(
        "evalset",
        suffix_length=20,
        separator="-",
    )


def _error_details(storage_object: StorageObject) -> list[dict[str, Any]]:
    return [
        {
            "storage_object_id": storage_object.storage_object_id,
            "provider": storage_object.provider,
            "bucket": storage_object.bucket,
            "object_key": storage_object.object_key,
        }
    ]


def _strong_etag(
    value: object,
    *,
    missing_code: str,
    weak_code: str,
    invalid_code: str,
    details: list[dict[str, Any]],
) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ApiError(
            missing_code,
            "评测集 manifest 必须提供强 ETag",
            409,
            details=details,
            retryable=False,
        )
    if raw[:2].lower() == "w/":
        raise ApiError(
            weak_code,
            "评测集 manifest 不能使用弱 ETag",
            409,
            details=details,
            retryable=False,
        )
    if raw.startswith('"') or raw.endswith('"'):
        if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
            raise ApiError(
                invalid_code,
                "评测集 manifest ETag 格式无效",
                409,
                details=details,
                retryable=False,
            )
        raw = raw[1:-1]
    if (
        not raw
        or '"' in raw
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in raw)
    ):
        raise ApiError(
            invalid_code,
            "评测集 manifest ETag 格式无效",
            409,
            details=details,
            retryable=False,
        )
    return raw


def _normalized_content_type(value: object) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _remote_manifest_head_enabled(provider: str) -> bool:
    if provider in MOCK_OBJECT_STORAGE_PROVIDERS:
        return False
    settings = get_settings()
    if settings.app_env.strip().lower() == "test":
        return False
    return settings.auris_object_storage_adapter.strip().lower() == "real"


def _verify_remote_manifest(snapshot: _ManifestSnapshot, storage_object: StorageObject) -> None:
    provider = snapshot["manifest_provider"]
    if not _remote_manifest_head_enabled(provider):
        return
    details = _error_details(storage_object)
    try:
        client = object_storage_client_for_provider(provider)
        if not client.allows_bucket(snapshot["manifest_bucket"]):
            raise ApiError(
                "EVAL_DATASET_MANIFEST_BUCKET_FORBIDDEN",
                "评测集 manifest bucket 不在 Provider 允许列表中",
                403,
                details=details,
                retryable=False,
            )
        remote = client.head_object(
            snapshot["manifest_bucket"],
            snapshot["manifest_object_key"],
        )
    except ApiError:
        raise
    except HTTPError as exc:
        if exc.code == 404:
            raise ApiError(
                "EVAL_DATASET_MANIFEST_REMOTE_NOT_FOUND",
                "评测集 manifest 远端对象不存在",
                404,
                details=details,
                retryable=False,
            ) from exc
        raise ApiError(
            "EVAL_DATASET_MANIFEST_REMOTE_VERIFY_FAILED",
            "评测集 manifest 无法通过对象存储 HEAD 校验",
            502,
            details=details,
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_PROVIDER_NOT_CONFIGURED",
            "评测集 manifest 的对象存储 Provider 未正确配置",
            503,
            details=details,
            retryable=False,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_REMOTE_VERIFY_FAILED",
            "评测集 manifest 无法通过对象存储 HEAD 校验",
            502,
            details=details,
            retryable=True,
        ) from exc

    raw_remote_size = remote.get("content_length") if isinstance(remote, dict) else None
    try:
        if raw_remote_size is None:
            raise ValueError("missing Content-Length")
        remote_size = int(str(raw_remote_size))
        if remote_size <= 0:
            raise ValueError("invalid Content-Length")
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_REMOTE_SIZE_REQUIRED",
            "评测集 manifest 的远端对象缺少有效 Content-Length",
            409,
            details=details,
            retryable=False,
        ) from exc
    if remote_size != snapshot["manifest_size_bytes"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_SIZE_DRIFT",
            "评测集 manifest 的远端对象大小已发生漂移",
            409,
            details=[
                {
                    **details[0],
                    "expected_size_bytes": snapshot["manifest_size_bytes"],
                    "actual_size_bytes": remote_size,
                }
            ],
            retryable=False,
        )

    remote_etag = _strong_etag(
        remote.get("etag") if isinstance(remote, dict) else None,
        missing_code="EVAL_DATASET_MANIFEST_REMOTE_ETAG_REQUIRED",
        weak_code="EVAL_DATASET_MANIFEST_REMOTE_ETAG_WEAK",
        invalid_code="EVAL_DATASET_MANIFEST_REMOTE_ETAG_INVALID",
        details=details,
    )
    if remote_etag != snapshot["manifest_etag"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_ETAG_DRIFT",
            "评测集 manifest 的远端 ETag 已发生漂移",
            409,
            details=[
                {
                    **details[0],
                    "expected_etag": snapshot["manifest_etag"],
                    "actual_etag": remote_etag,
                }
            ],
            retryable=False,
        )

    remote_content_type = _normalized_content_type(
        remote.get("content_type") if isinstance(remote, dict) else None
    )
    if remote_content_type and remote_content_type != snapshot["manifest_content_type"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_CONTENT_TYPE_DRIFT",
            "评测集 manifest 的远端 Content-Type 已发生漂移",
            409,
            details=[
                {
                    **details[0],
                    "expected_content_type": snapshot["manifest_content_type"],
                    "actual_content_type": remote_content_type,
                }
            ],
            retryable=False,
        )


def _manifest_snapshot(
    storage_object: StorageObject,
    ctx: RequestContext,
) -> _ManifestSnapshot:
    details = _error_details(storage_object)
    if storage_object.source_type != EVAL_DATASET_MANIFEST_SOURCE_TYPE:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_SOURCE_TYPE_INVALID",
            "评测集 manifest 的 source_type 无效",
            409,
            details=[
                {
                    **details[0],
                    "expected_source_type": EVAL_DATASET_MANIFEST_SOURCE_TYPE,
                    "actual_source_type": storage_object.source_type,
                }
            ],
        )
    provider = str(storage_object.provider or "").strip().lower()
    settings = get_settings()
    provider_allowed = provider in SUPPORTED_OBJECT_STORAGE_PROVIDERS or (
        provider in MOCK_OBJECT_STORAGE_PROVIDERS and settings.app_env.strip().lower() == "test"
    )
    if not provider_allowed:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_PROVIDER_INVALID",
            "评测集 manifest 的对象存储 Provider 无效",
            409,
            details=details,
        )

    bucket = str(storage_object.bucket or "")
    bucket_parts_valid = (
        3 <= len(bucket) <= 255
        and bucket == bucket.strip()
        and bucket[0].isalnum()
        and bucket[-1].isalnum()
        and all(
            character.isascii() and (character.isalnum() or character in ".-_")
            for character in bucket
        )
        and ".." not in bucket
    )
    if not bucket_parts_valid:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_BUCKET_INVALID",
            "评测集 manifest 的 bucket 命名无效",
            409,
            details=details,
        )

    object_key = str(storage_object.object_key or "")
    object_key_parts = object_key.split("/")
    expected_prefix = f"tenants/{ctx.tenant_id}/projects/{ctx.project_id}/"
    object_key_valid = (
        1 <= len(object_key) <= 1024
        and object_key == object_key.strip()
        and not object_key.startswith("/")
        and "\\" not in object_key
        and all(part not in {"", ".", ".."} for part in object_key_parts)
        and all(0x20 <= ord(character) != 0x7F for character in object_key)
        and object_key.startswith(expected_prefix)
    )
    expected_key_hash = hashlib.sha256(object_key.encode("utf-8")).hexdigest()
    if (
        not object_key_valid
        or str(storage_object.object_key_sha256 or "").lower() != expected_key_hash
    ):
        raise ApiError(
            "EVAL_DATASET_MANIFEST_KEY_FORBIDDEN",
            "评测集 manifest 的 object_key 不属于当前租户和项目命名空间",
            409,
            details=[{**details[0], "expected_prefix": expected_prefix}],
        )

    content_type = _normalized_content_type(storage_object.content_type)
    if content_type not in EVAL_DATASET_MANIFEST_CONTENT_TYPES:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_CONTENT_TYPE_INVALID",
            "评测集 manifest 的 Content-Type 无效",
            409,
            details=[
                {
                    **details[0],
                    "actual_content_type": storage_object.content_type,
                    "allowed_content_types": sorted(EVAL_DATASET_MANIFEST_CONTENT_TYPES),
                }
            ],
        )

    size_bytes = storage_object.size_bytes
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes <= 0:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_EMPTY",
            "评测集 manifest 不能为空",
            409,
            details=details,
        )
    etag = _strong_etag(
        storage_object.etag,
        missing_code="EVAL_DATASET_MANIFEST_ETAG_REQUIRED",
        weak_code="EVAL_DATASET_MANIFEST_ETAG_WEAK",
        invalid_code="EVAL_DATASET_MANIFEST_ETAG_INVALID",
        details=details,
    )
    snapshot: _ManifestSnapshot = {
        "manifest_provider": provider,
        "manifest_bucket": bucket,
        "manifest_object_key": object_key,
        "manifest_content_type": content_type,
        "manifest_size_bytes": size_bytes,
        "manifest_etag": etag,
    }
    return snapshot


def _manifest_object(
    session: Session,
    ctx: RequestContext,
    *,
    storage_object_id: str,
    expected_sha256: str,
) -> tuple[StorageObject, _ManifestSnapshot]:
    storage_object = session.get(StorageObject, storage_object_id)
    if storage_object is None:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_NOT_FOUND",
            "评测集 manifest 对象不存在",
            404,
            details=[{"storage_object_id": storage_object_id}],
        )
    if storage_object.tenant_id != ctx.tenant_id or storage_object.project_id != ctx.project_id:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_SCOPE_FORBIDDEN",
            "不能引用其他租户或项目的评测集 manifest",
            403,
        )
    if storage_object.status not in VERIFIED_STORAGE_STATUSES:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_NOT_VERIFIED",
            "评测集 manifest 必须先通过对象存储校验",
            409,
            details=[{"status": storage_object.status}],
        )
    actual_sha256 = str(storage_object.content_sha256 or "").lower()
    if not actual_sha256 or actual_sha256 != expected_sha256.lower():
        raise ApiError(
            "EVAL_DATASET_MANIFEST_HASH_MISMATCH",
            "评测集 manifest SHA-256 与登记对象不一致",
            409,
            details=[
                {
                    "expected_sha256": expected_sha256.lower(),
                    "actual_sha256": actual_sha256 or None,
                }
            ],
        )
    snapshot = _manifest_snapshot(
        storage_object,
        ctx,
    )
    return storage_object, snapshot


def _snapshot_document(dataset: EvalDatasetVersion) -> dict[str, Any]:
    document = {
        "eval_dataset_id": dataset.eval_dataset_id,
        "name": dataset.name,
        "capability": dataset.capability,
        "dataset_version": dataset.dataset_version,
        "manifest_storage_object_id": dataset.manifest_storage_object_id,
        "manifest_sha256": dataset.manifest_sha256,
        "manifest_provider": dataset.manifest_provider,
        "manifest_bucket": dataset.manifest_bucket,
        "manifest_object_key": dataset.manifest_object_key,
        "manifest_content_type": dataset.manifest_content_type,
        "manifest_size_bytes": dataset.manifest_size_bytes,
        "manifest_etag": dataset.manifest_etag,
        "sample_count": dataset.sample_count,
    }
    payload = dataset.payload or {}
    if payload.get("snapshot_schema_version") == "v2":
        document["source"] = payload.get("source")
        document["provenance_sha256"] = payload.get("provenance_sha256")
    return document


def _legacy_snapshot_document(dataset: EvalDatasetVersion) -> dict[str, Any]:
    return {
        "eval_dataset_id": dataset.eval_dataset_id,
        "name": dataset.name,
        "capability": dataset.capability,
        "dataset_version": dataset.dataset_version,
        "manifest_storage_object_id": dataset.manifest_storage_object_id,
        "manifest_sha256": dataset.manifest_sha256,
        "sample_count": dataset.sample_count,
    }


def _snapshot_sha256(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _provenance_sha256(provenance: dict[str, Any]) -> str:
    return _snapshot_sha256(provenance)


def _assert_public_provenance(dataset: EvalDatasetVersion) -> str | None:
    payload = dataset.payload or {}
    source = payload.get("source")
    provenance = payload.get("provenance")
    stored_sha256 = str(payload.get("provenance_sha256") or "")
    if source != "public_dataset":
        if provenance is not None or stored_sha256:
            raise ApiError(
                "EVAL_DATASET_PROVENANCE_SOURCE_MISMATCH",
                "非公开评测集不能携带公开数据来源快照",
                409,
            )
        return None
    if not isinstance(provenance, dict) or not stored_sha256:
        raise ApiError(
            "EVAL_DATASET_PUBLIC_PROVENANCE_REQUIRED",
            "公开评测集缺少不可变来源快照",
            409,
        )
    actual_sha256 = _provenance_sha256(provenance)
    if actual_sha256 != stored_sha256:
        raise ApiError(
            "EVAL_DATASET_PROVENANCE_DRIFT",
            "公开评测集来源、许可证或 split 快照已发生漂移",
            409,
        )
    if provenance.get("prepared_manifest_sha256") != dataset.manifest_sha256:
        raise ApiError(
            "EVAL_DATASET_PROVENANCE_MANIFEST_MISMATCH",
            "公开评测集来源快照未绑定当前 manifest",
            409,
        )
    return stored_sha256


def _stored_manifest_snapshot(dataset: EvalDatasetVersion) -> _ManifestSnapshot | None:
    values = (
        dataset.manifest_provider,
        dataset.manifest_bucket,
        dataset.manifest_object_key,
        dataset.manifest_content_type,
        dataset.manifest_size_bytes,
        dataset.manifest_etag,
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ApiError(
            "EVAL_DATASET_MANIFEST_SNAPSHOT_INCOMPLETE",
            "评测集 manifest 冻结快照不完整",
            409,
            details=[{"eval_dataset_id": dataset.eval_dataset_id}],
        )
    return {
        "manifest_provider": str(dataset.manifest_provider),
        "manifest_bucket": str(dataset.manifest_bucket),
        "manifest_object_key": str(dataset.manifest_object_key),
        "manifest_content_type": str(dataset.manifest_content_type),
        "manifest_size_bytes": int(dataset.manifest_size_bytes or 0),
        "manifest_etag": str(dataset.manifest_etag),
    }


def _assert_manifest_snapshot(
    dataset: EvalDatasetVersion,
    actual: _ManifestSnapshot,
) -> str:
    stored = _stored_manifest_snapshot(dataset)
    stored_snapshot_sha256 = str((dataset.payload or {}).get("snapshot_sha256") or "")
    if stored is None:
        if not (dataset.payload or {}).get("seeded"):
            raise ApiError(
                "EVAL_DATASET_MANIFEST_SNAPSHOT_INCOMPLETE",
                "评测集 manifest 尚未冻结真实对象快照",
                409,
                details=[{"eval_dataset_id": dataset.eval_dataset_id}],
            )
        legacy_snapshot_sha256 = _snapshot_sha256(_legacy_snapshot_document(dataset))
        if stored_snapshot_sha256 != legacy_snapshot_sha256:
            raise ApiError(
                "EVAL_DATASET_SNAPSHOT_DRIFT",
                "历史评测集元数据快照已发生漂移",
                409,
            )
        return legacy_snapshot_sha256

    if stored["manifest_size_bytes"] != actual["manifest_size_bytes"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_SIZE_DRIFT",
            "评测集 manifest 的登记大小已发生漂移",
            409,
            details=[
                {
                    "eval_dataset_id": dataset.eval_dataset_id,
                    "expected_size_bytes": stored["manifest_size_bytes"],
                    "actual_size_bytes": actual["manifest_size_bytes"],
                }
            ],
        )
    if stored["manifest_etag"] != actual["manifest_etag"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_ETAG_DRIFT",
            "评测集 manifest 的登记 ETag 已发生漂移",
            409,
            details=[
                {
                    "eval_dataset_id": dataset.eval_dataset_id,
                    "expected_etag": stored["manifest_etag"],
                    "actual_etag": actual["manifest_etag"],
                }
            ],
        )
    locator_drift: list[str] = []
    if stored["manifest_provider"] != actual["manifest_provider"]:
        locator_drift.append("manifest_provider")
    if stored["manifest_bucket"] != actual["manifest_bucket"]:
        locator_drift.append("manifest_bucket")
    if stored["manifest_object_key"] != actual["manifest_object_key"]:
        locator_drift.append("manifest_object_key")
    if locator_drift:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_LOCATOR_DRIFT",
            "评测集 manifest 的 Provider、bucket 或 object_key 已发生漂移",
            409,
            details=[
                {
                    "eval_dataset_id": dataset.eval_dataset_id,
                    "drift_fields": locator_drift,
                }
            ],
        )
    if stored["manifest_content_type"] != actual["manifest_content_type"]:
        raise ApiError(
            "EVAL_DATASET_MANIFEST_CONTENT_TYPE_DRIFT",
            "评测集 manifest 的登记 Content-Type 已发生漂移",
            409,
            details=[{"eval_dataset_id": dataset.eval_dataset_id}],
        )

    expected_snapshot_sha256 = _snapshot_sha256(_snapshot_document(dataset))
    if stored_snapshot_sha256 != expected_snapshot_sha256:
        raise ApiError(
            "EVAL_DATASET_SNAPSHOT_DRIFT",
            "评测集内容快照已发生漂移",
            409,
        )
    return expected_snapshot_sha256


def eval_dataset_data(dataset: EvalDatasetVersion) -> dict[str, Any]:
    document = _snapshot_document(dataset)
    payload = dataset.payload or {}
    return {
        **document,
        "id": dataset.eval_dataset_id,
        "dataset_id": dataset.eval_dataset_id,
        "status": dataset.status,
        "locked": dataset.status == LOCKED_DATASET_STATUS,
        "resource_version": dataset.resource_version,
        "snapshot_sha256": payload.get("snapshot_sha256"),
        "source": payload.get("source"),
        "provenance": payload.get("provenance"),
        "provenance_sha256": payload.get("provenance_sha256"),
        "root_trace_id": dataset.root_trace_id,
        "trace_id": dataset.current_trace_id,
        "locked_at": dataset.locked_at.isoformat() if dataset.locked_at else None,
        "metadata": payload.get("metadata") or {},
    }


def create_eval_dataset_version(
    session: Session,
    ctx: RequestContext,
    body: EvalDatasetVersionCreateRequest,
) -> dict[str, Any]:
    dataset_id = body.eval_dataset_id or _new_dataset_id()
    if session.get(EvalDatasetVersion, dataset_id) is not None:
        raise ApiError("EVAL_DATASET_ALREADY_EXISTS", "评测集版本已存在", 409)
    storage_object, manifest_snapshot = _manifest_object(
        session,
        ctx,
        storage_object_id=body.manifest_storage_object_id,
        expected_sha256=body.manifest_sha256,
    )
    _verify_remote_manifest(manifest_snapshot, storage_object)
    duplicate = session.scalar(
        select(EvalDatasetVersion.eval_dataset_id).where(
            EvalDatasetVersion.tenant_id == ctx.tenant_id,
            EvalDatasetVersion.project_id == ctx.project_id,
            EvalDatasetVersion.name == body.name,
            EvalDatasetVersion.dataset_version == body.dataset_version,
        )
    )
    if duplicate is not None:
        raise ApiError(
            "EVAL_DATASET_VERSION_ALREADY_EXISTS",
            "同名评测集版本已存在",
            409,
        )
    provenance = body.provenance.model_dump(mode="json") if body.provenance else None
    provenance_sha256 = _provenance_sha256(provenance) if provenance else None
    dataset = EvalDatasetVersion(
        eval_dataset_id=dataset_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        name=body.name,
        capability=body.capability,
        dataset_version=body.dataset_version,
        status="draft",
        manifest_storage_object_id=body.manifest_storage_object_id,
        manifest_sha256=body.manifest_sha256,
        **manifest_snapshot,
        sample_count=body.sample_count,
        resource_version=1,
        root_trace_id=ctx.trace_id,
        current_trace_id=ctx.trace_id,
        payload={
            "source": body.source,
            "metadata": body.metadata,
            "provenance": provenance,
            "provenance_sha256": provenance_sha256,
            "snapshot_schema_version": "v2",
        },
    )
    dataset.payload = {
        **dataset.payload,
        "snapshot_sha256": _snapshot_sha256(_snapshot_document(dataset)),
    }
    session.add(dataset)
    session.flush()
    data = eval_dataset_data(dataset)
    upsert_resource(
        session,
        ctx,
        "eval_datasets",
        dataset_id,
        data,
        status="draft",
        trace_id=ctx.trace_id,
    )
    record_audit(
        session,
        ctx,
        action="eval_dataset.created",
        object_type="eval_dataset",
        object_id=dataset_id,
        after=data,
        trace_id=dataset.root_trace_id,
    )
    return data


def get_eval_dataset_version(
    session: Session,
    ctx: RequestContext,
    dataset_id: str,
    *,
    for_update: bool = False,
) -> EvalDatasetVersion:
    query = select(EvalDatasetVersion).where(
        EvalDatasetVersion.eval_dataset_id == dataset_id,
        EvalDatasetVersion.tenant_id == ctx.tenant_id,
        EvalDatasetVersion.project_id == ctx.project_id,
    )
    if for_update:
        query = query.with_for_update()
    dataset = session.scalar(query)
    if dataset is None:
        raise ApiError("EVAL_DATASET_NOT_FOUND", "评测集版本不存在", 404)
    return dataset


def lock_eval_dataset_version(
    session: Session,
    ctx: RequestContext,
    dataset_id: str,
    *,
    expected_resource_version: int,
) -> dict[str, Any]:
    dataset = get_eval_dataset_version(session, ctx, dataset_id, for_update=True)
    if dataset.resource_version != expected_resource_version:
        raise ApiError(
            "EVAL_DATASET_VERSION_CONFLICT",
            "评测集版本已变化，请刷新后重试",
            409,
            details=[
                {
                    "expected_resource_version": expected_resource_version,
                    "actual_resource_version": dataset.resource_version,
                }
            ],
        )
    if dataset.status != "draft":
        raise ApiError(
            "EVAL_DATASET_NOT_DRAFT",
            "只有草稿评测集可以锁定",
            409,
            details=[{"status": dataset.status}],
        )
    storage_object, manifest_snapshot = _manifest_object(
        session,
        ctx,
        storage_object_id=dataset.manifest_storage_object_id,
        expected_sha256=dataset.manifest_sha256,
    )
    before = eval_dataset_data(dataset)
    provenance_sha256 = _assert_public_provenance(dataset)
    expected_snapshot = _assert_manifest_snapshot(dataset, manifest_snapshot)
    _verify_remote_manifest(manifest_snapshot, storage_object)
    dataset.status = LOCKED_DATASET_STATUS
    dataset.resource_version += 1
    dataset.current_trace_id = ctx.trace_id
    dataset.locked_at = datetime.now(UTC)
    session.flush()
    data = eval_dataset_data(dataset)
    upsert_resource(
        session,
        ctx,
        "eval_datasets",
        dataset_id,
        data,
        status=LOCKED_DATASET_STATUS,
        trace_id=ctx.trace_id,
    )
    record_audit(
        session,
        ctx,
        action="eval_dataset.locked",
        object_type="eval_dataset",
        object_id=dataset_id,
        before=before,
        after=data,
        trace_id=dataset.root_trace_id,
    )
    enqueue_event(
        session,
        ctx,
        event_type="eval_dataset.locked",
        aggregate_type="eval_dataset",
        aggregate_id=dataset_id,
        payload={
            "eval_dataset_id": dataset_id,
            "dataset_version": dataset.dataset_version,
            "manifest_storage_object_id": dataset.manifest_storage_object_id,
            "manifest_sha256": dataset.manifest_sha256,
            **manifest_snapshot,
            "snapshot_sha256": expected_snapshot,
            "provenance_sha256": provenance_sha256,
            "sample_count": dataset.sample_count,
            "root_trace_id": dataset.root_trace_id,
        },
    )
    return data


def locked_eval_dataset_snapshot(
    session: Session,
    ctx: RequestContext,
    dataset_id: str,
    *,
    required_capability: str | None = None,
) -> dict[str, Any]:
    dataset = get_eval_dataset_version(session, ctx, dataset_id)
    if dataset.status != LOCKED_DATASET_STATUS or dataset.locked_at is None:
        raise ApiError(
            "EVAL_DATASET_NOT_LOCKED",
            "评测运行只能引用已锁定的评测集版本",
            409,
            details=[{"status": dataset.status}],
        )
    if required_capability and dataset.capability != required_capability:
        raise ApiError(
            "EVAL_DATASET_CAPABILITY_MISMATCH",
            "评测集能力与当前评测任务不匹配",
            409,
            details=[
                {
                    "expected_capability": required_capability,
                    "actual_capability": dataset.capability,
                }
            ],
        )
    storage_object, manifest_snapshot = _manifest_object(
        session,
        ctx,
        storage_object_id=dataset.manifest_storage_object_id,
        expected_sha256=dataset.manifest_sha256,
    )
    _assert_public_provenance(dataset)
    snapshot_sha256 = _assert_manifest_snapshot(dataset, manifest_snapshot)
    _verify_remote_manifest(manifest_snapshot, storage_object)
    return {
        **eval_dataset_data(dataset),
        **manifest_snapshot,
        "snapshot_sha256": snapshot_sha256,
    }
