from __future__ import annotations

import re
from collections.abc import AsyncIterator
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.api.deps import ContextDep, PaginationDep, SessionDep, SignedCompletionContextDep
from app.core.completion_signature import (
    KEY_ID_HEADER,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    SIGNATURE_ID_HEADER,
    SIGNATURE_MODE_HEADER,
    SOURCE_HEADER,
    TIMESTAMP_HEADER,
)
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.http_headers import content_disposition_header
from app.core.project_membership import (
    conflicting_project_member_identities,
    duplicate_project_member_user_ids,
    project_member_user_id,
    user_has_project_membership,
)
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import Project, RunRecord, Tenant
from app.schemas import (
    ApiErrorEnvelope,
    ExternalCallbackRequest,
    ExternalRunCompletionReceiptRequest,
    ExternalRunProgressReceiptRequest,
    KnowledgeBuildRequest,
    KnowledgeRecallRequest,
    RunCompletionReceiptRequest,
    RunReleaseDecisionRequest,
    TaskRunRetryRequest,
    parse_payload,
)
from app.schemas.public_runs import (
    ExportJob,
    PublicRunDetail,
    PublicRunEnvelope,
    RunCompletionReceiptPendingResponse,
)
from app.services.adapters import object_storage_client_for_provider
from app.services.audio_import_progress_service import apply_audio_import_progress
from app.services.audit_service import record_audit
from app.services.connector_import_service import prepare_platform_audio_connector_payload
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.knowledge_recall_service import recall_knowledge_index
from app.services.outbox_service import enqueue_event
from app.services.platform_connection_service import reject_plaintext_credentials
from app.services.public_run_projection_service import public_run_projection
from app.services.release_gate_service import (
    decide_release_gate,
    prepare_settings_publish,
)
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_data,
    list_resource_page,
    page_limit,
    patch_idempotent_json_resource,
    status_counts,
)
from app.services.run_service import (
    complete_run_from_receipt,
    create_run,
    get_run,
    list_run_page,
    retry_run,
)
from app.services.scene_profile_service import bind_active_scene_profile_lock

router = APIRouter(tags=["generic"])


def _document_external_completion_hmac_headers(
    timestamp: Annotated[
        str,
        Header(
            alias=TIMESTAMP_HEADER,
            min_length=1,
            description="签名时间戳；接受 Unix 秒或带时区的 RFC 3339 时间。",
        ),
    ],
    nonce: Annotated[
        str,
        Header(
            alias=NONCE_HEADER,
            min_length=1,
            max_length=128,
            description="单次请求随机值；同一签名 key 下重放会被拒绝。",
        ),
    ],
    source: Annotated[
        Literal["dagster", "object_storage", "external_callback"],
        Header(
            alias=SOURCE_HEADER,
            description="已绑定签名 key 且与请求体 adapter 一致的真实执行来源。",
        ),
    ],
    signature_mode: Annotated[
        Literal["hmac-sha256"],
        Header(
            alias=SIGNATURE_MODE_HEADER,
            description="完成回执签名算法；当前仅支持 hmac-sha256。",
        ),
    ],
    signature: Annotated[
        str,
        Header(
            alias=SIGNATURE_HEADER,
            pattern=r"^(?:sha256=)?[0-9A-Fa-f]{64}$",
            description="规范请求消息的 HMAC-SHA256；接受 sha256= 前缀或裸十六进制。",
        ),
    ],
    key_id: Annotated[
        str | None,
        Header(
            alias=KEY_ID_HEADER,
            min_length=1,
            max_length=128,
            description=("规范签名 key 标识；必须与弃用的 X-Auris-Signature-Id 至少提供一个。"),
        ),
    ] = None,
    signature_id: Annotated[
        str | None,
        Header(
            alias=SIGNATURE_ID_HEADER,
            min_length=1,
            max_length=128,
            deprecated=True,
            description="兼容旧客户端的 key 标识；新客户端应使用 X-Auris-Key-Id。",
        ),
    ] = None,
) -> None:
    """Expose headers in OpenAPI; SignedCompletionContextDep performs verification."""


ExternalCompletionHmacHeadersDep = Annotated[
    None,
    Depends(_document_external_completion_hmac_headers),
]

SCENE_LOCK_EXEMPT_EXPORT_MODULES = frozenset({"tenants", "projects", "settings"})
EXPORT_PUBLIC_FIELDS = frozenset(
    {
        "id",
        "run_id",
        "export_job_id",
        "run_type",
        "status",
        "format",
        "target",
        "object_id",
        "scene_profile_id",
        "scene_profile_version_id",
        "scene_profile_snapshot_sha256",
        "scope",
        "download_ref",
        "trace_id",
        "next_actions",
    }
)
EXPORT_FORMAT_EXTENSIONS = {
    "csv": "csv",
    "json": "json",
    "jsonl": "jsonl",
    "parquet": "parquet",
}
EXPORT_MEDIA_TYPE_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+"
    r"(?: *; *[!-~][ -~]*)?$"
)


class _ExportObjectStreamTruncatedError(RuntimeError):
    """Raised after headers are sent when object storage ends a stream early."""


class _ExportStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        upstream_stream: Any,
        status_code: int,
        headers: dict[str, str],
    ) -> None:
        super().__init__(content, status_code=status_code, headers=headers)
        self.upstream_stream = upstream_stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self.upstream_stream.close)


def prepare_export_payload(session: SessionDep, ctx: ContextDep, payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ApiError("EXPORT_REQUEST_INVALID", "导出请求必须是 JSON 对象", 422)
    target = payload.get("target")
    module_key = payload.get("module_key")
    object_id = payload.get("object_id")
    governance_module_export = (
        target == "module_view"
        and isinstance(module_key, str)
        and module_key in SCENE_LOCK_EXEMPT_EXPORT_MODULES
        and isinstance(object_id, str)
        and object_id.startswith(f"{module_key}:")
    )
    if governance_module_export:
        return payload
    return bind_active_scene_profile_lock(session, ctx, payload)


def prepare_connector_payload(
    session: SessionDep,
    ctx: ContextDep,
    payload: dict[str, Any],
    *,
    existing_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reject_plaintext_credentials(payload)
    prepared = bind_active_scene_profile_lock(session, ctx, payload)
    platform_audio_payload = prepare_platform_audio_connector_payload(
        session,
        ctx,
        prepared,
        existing_payload=existing_payload,
    )
    if platform_audio_payload is not None:
        return {
            **platform_audio_payload,
            **{
                field: prepared[field]
                for field in (
                    "scene_profile_id",
                    "scene_profile_version_id",
                    "scene_profile_snapshot_sha256",
                )
                if field in prepared
            },
        }
    target_asset_key = prepared.get(
        "target_asset_key",
        (existing_payload or {}).get("target_asset_key"),
    )
    if target_asset_key is None:
        return prepared
    if not isinstance(target_asset_key, str) or not target_asset_key.strip():
        raise ApiError(
            "CONNECTOR_TARGET_ASSET_INVALID",
            "连接器目标必须是当前项目已登记的数据资产",
            422,
        )
    target_asset_key = target_asset_key.strip()
    target_asset = get_resource(session, ctx, "data_assets", target_asset_key)
    if target_asset.data.get("asset_key") != target_asset_key:
        raise ApiError("NOT_FOUND", f"data_assets 不存在：{target_asset_key}", 404)
    if "target_asset_key" in prepared:
        prepared["target_asset_key"] = target_asset_key
    return prepared


def _require_unique_project_members(members: object) -> None:
    conflicts = conflicting_project_member_identities(members)
    if conflicts:
        raise ApiError(
            "PROJECT_MEMBER_IDENTITY_CONFLICT",
            "项目成员 user_id 与 id 别名冲突",
            422,
            details=[{"conflict_count": len(conflicts)}],
        )
    duplicates = duplicate_project_member_user_ids(members)
    if duplicates:
        raise ApiError(
            "PROJECT_MEMBER_DUPLICATE",
            "项目成员 user_id 必须唯一",
            422,
            details=[{"duplicate_user_ids": list(duplicates)}],
        )


KNOWLEDGE_QDRANT_COLLECTION = "knowledge_chunks"
QDRANT_COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
QDRANT_AUTHORITY_FIELDS = frozenset(
    {
        "tenant_id",
        "project_id",
        "trace_id",
        "collection",
        "collection_name",
        "vector_collection",
        "qdrant_collection",
        "qdrant_payload",
        "knowledge_index_id",
        "index_id",
        "index_ref",
        "index_refs",
        "knowledge_source_id",
        "source_id",
        "source_ref",
        "source_refs",
        "source_type",
        "asset_key",
        "version",
        "connector_id",
        "business_ref",
        "affected_objects",
    }
)


def qdrant_caller_fields(payload: dict[str, Any]) -> dict[str, Any]:
    caller_fields = {
        field: value for field, value in payload.items() if field not in QDRANT_AUTHORITY_FIELDS
    }
    scope = caller_fields.get("scope")
    if isinstance(scope, dict):
        caller_fields["scope"] = {
            field: value for field, value in scope.items() if field not in QDRANT_AUTHORITY_FIELDS
        }
    return caller_fields


def knowledge_qdrant_collection(
    source: dict[str, Any], *, index: dict[str, Any] | None = None
) -> str:
    configured_collection = (
        index.get("vector_collection") if index is not None else source.get("vector_collection")
    )
    if configured_collection is None:
        return KNOWLEDGE_QDRANT_COLLECTION
    if not isinstance(configured_collection, str) or not QDRANT_COLLECTION_PATTERN.fullmatch(
        configured_collection
    ):
        raise ApiError(
            "QDRANT_COLLECTION_INVALID",
            "知识索引 collection 名称不符合服务端安全规则",
            422,
            details=[
                {
                    "field": "vector_collection",
                    "message": "collection 只能包含字母、数字、下划线或连字符",
                    "code": "invalid_collection_name",
                }
            ],
        )
    if configured_collection != KNOWLEDGE_QDRANT_COLLECTION:
        raise ApiError(
            "QDRANT_COLLECTION_FORBIDDEN",
            "知识索引不能切换到未授权的 Qdrant collection",
            422,
            details=[
                {
                    "field": "vector_collection",
                    "message": "collection 不在知识域服务端映射中",
                    "code": "collection_not_allowed",
                }
            ],
        )
    return KNOWLEDGE_QDRANT_COLLECTION


def _strong_export_etag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized.startswith("W/"):
        return None
    if normalized.startswith('"') or normalized.endswith('"'):
        if len(normalized) < 2 or not (normalized.startswith('"') and normalized.endswith('"')):
            return None
        normalized = normalized[1:-1]
    if (
        not normalized
        or '"' in normalized
        or any(ord(character) < 0x21 for character in normalized)
    ):
        return None
    return normalized


def _safe_export_content_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if len(normalized) > 255 or EXPORT_MEDIA_TYPE_PATTERN.fullmatch(normalized) is None:
        return None
    return normalized


def _export_locator(record: RunRecord) -> dict[str, Any] | None:
    if record.status != "success":
        return None
    payload = record.payload if isinstance(record.payload, dict) else {}
    dispatch = payload.get("dispatch")
    if not isinstance(dispatch, dict) or dispatch.get("adapter") != "object_storage":
        return None
    details = dispatch.get("details")
    if not isinstance(details, dict):
        return None
    provider = details.get("provider")
    bucket = details.get("bucket")
    object_key = details.get("object_key")
    etag = _strong_export_etag(details.get("etag"))
    content_length = details.get("content_length")
    content_type = _safe_export_content_type(
        details.get("content_type") or payload.get("content_type")
    )
    if (
        not isinstance(provider, str)
        or not provider.strip()
        or not isinstance(bucket, str)
        or not bucket.strip()
        or not isinstance(object_key, str)
        or not isinstance(content_length, int)
        or isinstance(content_length, bool)
        or content_length <= 0
        or etag is None
        or content_type is None
    ):
        return None
    expected_prefix = f"tenants/{record.tenant_id}/projects/{record.project_id}/"
    key_parts = object_key.split("/")
    if (
        not object_key.startswith(expected_prefix)
        or object_key.startswith("/")
        or "\\" in object_key
        or any(part in {"", ".", ".."} for part in key_parts)
    ):
        return None
    settings = get_settings()
    configured_provider = settings.object_storage_provider.strip().lower()
    allowed_buckets = {
        settings.object_storage_bucket.strip(),
        *(
            item.strip()
            for item in settings.object_storage_allowed_buckets.split(",")
            if item.strip()
        ),
    }
    if provider.strip().lower() != configured_provider or bucket not in allowed_buckets:
        return None
    return {
        "provider": provider.strip().lower(),
        "bucket": bucket,
        "object_key": object_key,
        "etag": etag,
        "content_length": content_length,
        "content_type": content_type,
    }


def export_job_payload(record: RunRecord) -> dict[str, Any]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    dispatch = payload.get("dispatch")
    has_reservation = isinstance(dispatch, dict) and dispatch.get("adapter") == "object_storage"
    locator = _export_locator(record)
    download_ref = None
    if has_reservation:
        download_ref = {
            "kind": "bff_download",
            "status": (
                "ready"
                if locator is not None
                else "unavailable"
                if record.status == "success"
                else "reserved"
            ),
            "href": (f"/api/v1/exports/{record.run_id}/download" if locator is not None else None),
            "content_type": (
                locator["content_type"]
                if locator is not None
                else payload.get("content_type", "application/json")
            ),
            "expires_at": payload.get("expires_at"),
        }

    raw_scope = payload.get("scope")
    scope_source = raw_scope if isinstance(raw_scope, dict) else payload
    raw_filter = scope_source.get("filter")
    public_filter = (
        {
            str(key): value
            for key, value in raw_filter.items()
            if isinstance(key, str)
            and (
                isinstance(value, str | int | float | bool)
                or (
                    isinstance(value, list)
                    and all(isinstance(item, str | int | float | bool) for item in value)
                )
            )
        }
        if isinstance(raw_filter, dict)
        else None
    )
    scope = {
        field: scope_source.get(field) if isinstance(scope_source.get(field), str) else None
        for field in ("target", "object_id", "module_key", "active_tab")
    }
    scope["filter"] = public_filter
    raw_next_actions = payload.get("next_actions")
    next_actions = (
        [
            {
                field: action[field]
                for field in ("key", "label", "code", "type", "href", "route", "available_at")
                if isinstance(action.get(field), str)
            }
            for action in raw_next_actions
            if isinstance(action, dict)
            and isinstance(action.get("key"), str)
            and isinstance(action.get("label"), str)
        ]
        if isinstance(raw_next_actions, list)
        else []
    )
    projection = {
        "id": record.run_id,
        "run_id": record.run_id,
        "export_job_id": record.run_id,
        "run_type": record.run_type,
        "status": record.status,
        "format": payload.get("format") if isinstance(payload.get("format"), str) else "jsonl",
        "target": payload.get("target") if isinstance(payload.get("target"), str) else None,
        "object_id": (
            payload.get("object_id") if isinstance(payload.get("object_id"), str) else None
        ),
        "scope": scope,
        "download_ref": download_ref,
        "trace_id": record.trace_id,
        "next_actions": next_actions,
    }
    for scene_field in (
        "scene_profile_id",
        "scene_profile_version_id",
        "scene_profile_snapshot_sha256",
    ):
        if isinstance(payload.get(scene_field), str):
            projection[scene_field] = payload[scene_field]
    return public_run_projection(
        projection,
        allowed_fields=EXPORT_PUBLIC_FIELDS,
        field_name="export_job",
    )


def _scoped_export_record(session: SessionDep, ctx: ContextDep, run_id: str) -> RunRecord:
    # Exports can contain a snapshot of otherwise sensitive project resources.
    # Creation is project-admin-only, so reads and byte streaming must preserve
    # that authority boundary instead of treating possession of a run ID as a
    # download capability.
    require_any_role(ctx, ("project_admin",), "exports.read")
    record = session.get(RunRecord, run_id)
    if (
        record is None
        or record.run_type != "export"
        or record.tenant_id != ctx.tenant_id
        or record.project_id != ctx.project_id
    ):
        raise ApiError("NOT_FOUND", f"导出任务不存在：{run_id}", 404)
    return record


def _parse_export_range(value: str | None, total: int) -> tuple[int, int, bool] | None:
    if total <= 0:
        return None
    if not value:
        return 0, total - 1, False
    if not value.startswith("bytes=") or "," in value:
        return None
    start_raw, separator, end_raw = value.removeprefix("bytes=").partition("-")
    if separator != "-":
        return None
    try:
        if start_raw == "":
            suffix = int(end_raw)
            if suffix <= 0:
                return None
            start = max(total - suffix, 0)
            end = total - 1
        else:
            start = int(start_raw)
            end = int(end_raw) if end_raw else total - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total:
        return None
    return start, min(end, total - 1), True


def _export_etag_header(value: str) -> str:
    return f'"{value}"'


def _close_export_result(result: dict[str, Any] | None) -> None:
    if not isinstance(result, dict):
        return
    stream = result.get("stream")
    if stream is not None and hasattr(stream, "close"):
        stream.close()


def _export_response_headers(
    record: RunRecord,
    locator: dict[str, Any],
    *,
    content_length: int,
) -> dict[str, str]:
    extension = EXPORT_FORMAT_EXTENSIONS.get(str(record.payload.get("format") or "").lower())
    filename_component = re.sub(r"[^A-Za-z0-9._-]+", "-", record.run_id)
    filename_component = filename_component.strip(".-")[:96].rstrip(".-") or "artifact"
    filename = f"export-{filename_component}.{extension or 'bin'}"
    return {
        "Accept-Ranges": "bytes",
        "Content-Type": str(locator["content_type"]),
        "Content-Length": str(content_length),
        "Content-Disposition": content_disposition_header(
            "attachment",
            filename,
            fallback="export-artifact.bin",
        ),
        "ETag": _export_etag_header(str(locator["etag"])),
        "Cache-Control": "private, no-store",
        "Vary": "Range, Authorization, X-Tenant-Id, X-Project-Id",
        "X-Content-Type-Options": "nosniff",
    }


def _stream_export_download(
    record: RunRecord,
    request: Request,
    *,
    head_only: bool,
) -> Response:
    locator = _export_locator(record)
    if locator is None:
        raise ApiError(
            "EXPORT_DOWNLOAD_NOT_READY",
            "导出对象尚未形成可验证的 BFF 下载引用",
            409,
            retryable=record.status != "success",
        )
    total = int(locator["content_length"])
    range_header = request.headers.get("range")
    parsed = _parse_export_range(range_header, total)
    if parsed is None:
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{total}",
                "Cache-Control": "private, no-store",
                "Vary": "Range, Authorization, X-Tenant-Id, X-Project-Id",
            },
        )
    start, end, partial = parsed
    expected_length = end - start + 1 if partial else total
    headers = _export_response_headers(
        record,
        locator,
        content_length=expected_length,
    )
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    result: dict[str, Any] | None = None
    try:
        client = object_storage_client_for_provider(str(locator["provider"]))
        if not client.allows_bucket(str(locator["bucket"])):
            raise ApiError(
                "EXPORT_DOWNLOAD_NOT_READY",
                "导出对象不在服务端允许的存储范围内",
                409,
                retryable=False,
            )
        if head_only:
            result = client.head_object(
                str(locator["bucket"]),
                str(locator["object_key"]),
                if_match=_export_etag_header(str(locator["etag"])),
            )
        else:
            open_object = getattr(client, "open_object", None)
            if not callable(open_object):
                raise ApiError(
                    "EXPORT_DOWNLOAD_UNSUPPORTED",
                    "对象存储 Provider 不支持安全流式下载",
                    502,
                    retryable=True,
                )
            result = open_object(
                str(locator["bucket"]),
                str(locator["object_key"]),
                byte_range=range_header if partial else None,
                if_match=_export_etag_header(str(locator["etag"])),
            )
    except HTTPError as exc:
        if exc.fp is not None:
            exc.close()
        if exc.code == 404:
            raise ApiError("EXPORT_OBJECT_NOT_FOUND", "导出对象不存在", 404) from exc
        if exc.code == 412:
            raise ApiError(
                "EXPORT_OBJECT_VERSION_CHANGED",
                "导出对象版本已变化，必须重新生成导出任务",
                412,
                retryable=False,
            ) from exc
        if exc.code == 416:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total}",
                    "Cache-Control": "private, no-store",
                },
            )
        raise ApiError(
            "EXPORT_OBJECT_FETCH_FAILED",
            "读取导出对象失败",
            502,
            retryable=True,
        ) from exc
    except ApiError:
        raise
    except (OSError, URLError, TimeoutError, ValueError, TypeError) as exc:
        raise ApiError(
            "EXPORT_OBJECT_FETCH_FAILED",
            "读取导出对象失败",
            502,
            retryable=True,
        ) from exc

    if not isinstance(result, dict):
        raise ApiError("EXPORT_OBJECT_INVALID", "导出对象响应无效", 502, retryable=True)
    try:
        upstream_status = int(result.get("status") or 0)
    except (TypeError, ValueError) as exc:
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_INVALID",
            "对象存储返回了无效的状态元数据",
            502,
            retryable=True,
        ) from exc
    expected_status = 206 if partial and not head_only else 200
    if head_only and partial:
        # A conditional HEAD validates the complete immutable object. The BFF
        # still returns Range-parity headers/status to its caller.
        expected_status = 200
    if upstream_status != expected_status:
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_RANGE_INVALID",
            "对象存储返回了不符合下载语义的状态",
            502,
            retryable=True,
        )
    upstream_etag = _strong_export_etag(result.get("etag"))
    if upstream_etag != locator["etag"]:
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_VERSION_CHANGED",
            "导出对象版本已变化，必须重新生成导出任务",
            412,
            retryable=False,
        )
    if result.get("content_type") != locator["content_type"]:
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_CONTENT_TYPE_MISMATCH",
            "对象存储返回的导出对象类型不一致",
            502,
            retryable=True,
        )
    upstream_length = result.get("content_length")
    expected_upstream_length = total if head_only else expected_length
    if str(upstream_length or "") != str(expected_upstream_length):
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_SIZE_MISMATCH",
            "对象存储返回的导出对象长度不一致",
            502,
            retryable=True,
        )
    if not head_only and partial and result.get("content_range") != headers["Content-Range"]:
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_RANGE_INVALID",
            "对象存储返回的导出对象区间不一致",
            502,
            retryable=True,
        )
    if not head_only and not partial and result.get("content_range") not in (None, ""):
        _close_export_result(result)
        raise ApiError(
            "EXPORT_OBJECT_RANGE_INVALID",
            "对象存储为完整下载返回了意外的区间元数据",
            502,
            retryable=True,
        )
    if head_only:
        _close_export_result(result)
        return Response(status_code=206 if partial else 200, headers=headers)

    stream = result.get("stream")
    if stream is None:
        body = result.get("body")
        if isinstance(body, bytes):
            stream = BytesIO(body)
            result["stream"] = stream
    if stream is None or not hasattr(stream, "read") or not hasattr(stream, "close"):
        _close_export_result(result)
        raise ApiError("EXPORT_OBJECT_INVALID", "导出对象响应无效", 502, retryable=True)

    async def iter_export() -> AsyncIterator[bytes]:
        remaining = expected_length
        while remaining > 0:
            chunk = await run_in_threadpool(stream.read, min(64 * 1024, remaining))
            if not chunk or len(chunk) > remaining:
                raise _ExportObjectStreamTruncatedError("对象存储导出流长度与已验证元数据不一致")
            remaining -= len(chunk)
            yield chunk

    return _ExportStreamingResponse(
        iter_export(),
        upstream_stream=stream,
        status_code=206 if partial else 200,
        headers=headers,
    )


def knowledge_qdrant_payload(
    ctx: ContextDep,
    source: dict[str, Any],
    *,
    index: dict[str, Any] | None = None,
    source_id: str | None = None,
    index_id: str | None = None,
) -> dict[str, Any]:
    authoritative_source_id = (
        source_id or source.get("knowledge_source_id") or source.get("source_id")
    )
    authoritative_index_id = (index_id if index is not None else None) or (
        index.get("knowledge_index_id") if index else None
    )
    collection = knowledge_qdrant_collection(source, index=index)
    version = (
        index.get("version")
        if index
        else source.get("version") or source.get("freshness") or "source-current"
    )
    asset_key = source.get("asset_key") or f"auris/knowledge/{authoritative_source_id}"
    embedding_text = "\n".join(
        str(value).strip()
        for value in (
            source.get("name"),
            source.get("description"),
            source.get("source_type"),
            source.get("connector_id"),
        )
        if value is not None and str(value).strip()
    )
    return {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "trace_id": ctx.trace_id,
        "collection": collection,
        "knowledge_index_id": authoritative_index_id,
        "knowledge_source_id": authoritative_source_id,
        "source_id": authoritative_source_id,
        "source_type": source.get("source_type"),
        "asset_key": asset_key,
        "version": version,
        "embedding_text": embedding_text,
        "business_ref": {
            "connector_id": source.get("connector_id"),
            "source_name": source.get("name"),
            "index_name": index.get("name") if index else None,
            "recall_strategy": index.get("recall_strategy") if index else None,
        },
    }


@router.get("/tenants")
def get_tenants(session: SessionDep, ctx: ContextDep):
    if "system" not in ctx.roles:
        tenant = session.get(Tenant, ctx.tenant_id)
        return collection_envelope([tenant.data] if tenant else [], ctx)
    tenants = [
        tenant.data for tenant in session.scalars(select(Tenant).order_by(Tenant.created_at))
    ]
    return collection_envelope(tenants, ctx)


@router.post("/tenants", status_code=201)
async def post_tenants(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("system",), action="tenants.create")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="tenants.create", body_hash=body_hash)
    if replay is not None:
        return replay
    body = await request.json()
    tenant_id = (
        body.get("tenant_id") or body.get("tenant_code") or body.get("name", "tenant").lower()
    )
    before_tenant = session.get(Tenant, tenant_id)
    before = dict(before_tenant.data) if before_tenant else None
    data = {**body, "tenant_id": tenant_id, "trace_id": ctx.trace_id}
    tenant = Tenant(
        tenant_id=tenant_id,
        tenant_code=body.get("tenant_code", tenant_id),
        name=body.get("name", tenant_id),
        status=body.get("status", "active"),
        data=data,
    )
    session.merge(tenant)
    record_audit(
        session,
        ctx,
        action="tenants.create",
        object_type="tenant",
        object_id=tenant_id,
        before=before,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="tenant.created",
        aggregate_type="tenant",
        aggregate_id=tenant_id,
        payload=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation="tenants.create",
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/tenants/{id}")
def get_tenants_by_id(id: str, session: SessionDep, ctx: ContextDep):
    if id != ctx.tenant_id and "system" not in ctx.roles:
        raise ApiError("FORBIDDEN", "当前上下文无权访问目标租户", 403)
    tenant = session.get(Tenant, id)
    if not tenant:
        raise ApiError("NOT_FOUND", f"租户不存在：{id}", 404)
    return envelope(tenant.data, ctx)


@router.patch("/tenants/{id}")
async def patch_tenants_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("system",), action="tenants.patch")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="tenants.patch", body_hash=body_hash)
    if replay is not None:
        return replay
    tenant = session.get(Tenant, id)
    body = await request.json()
    if tenant:
        before = dict(tenant.data)
        tenant.data = {**tenant.data, **body, "trace_id": ctx.trace_id}
        tenant.status = tenant.data.get("status", tenant.status)
        record_audit(
            session,
            ctx,
            action="tenants.patch",
            object_type="tenant",
            object_id=id,
            before=before,
            after=tenant.data,
        )
        enqueue_event(
            session,
            ctx,
            event_type="tenant.patched",
            aggregate_type="tenant",
            aggregate_id=id,
            payload=tenant.data,
        )
        response = envelope(tenant.data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation="tenants.patch",
            body_hash=body_hash,
            status_code=200,
            response_json=response,
        )
        session.commit()
        return response
    raise ApiError("NOT_FOUND", f"租户不存在：{id}", 404)


@router.get("/projects")
def get_projects(session: SessionDep, ctx: ContextDep):
    projects = [
        project.data
        for project in session.scalars(select(Project).where(Project.tenant_id == ctx.tenant_id))
        if "system" in ctx.roles or user_has_project_membership(project, ctx.user_id)
    ]
    return collection_envelope(projects, ctx)


@router.post("/projects", status_code=201)
async def post_projects(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), action="projects.create")
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="projects.create", body_hash=body_hash)
    if replay is not None:
        return replay
    body = await request.json()
    project_id = body.get("project_id") or body.get("name", "project").lower().replace(" ", "_")
    before_project = session.get(Project, project_id)
    if before_project is not None:
        if before_project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
            raise ApiError("FORBIDDEN", "当前上下文无权覆盖其他租户项目", 403)
        raise ApiError(
            "PROJECT_ALREADY_EXISTS",
            "项目 ID 已存在，请切换项目后通过编辑接口修改",
            409,
        )
    requested_member_ids = body.get("member_user_ids")
    member_user_ids = (
        [
            ctx.user_id,
            *(
                value
                for value in requested_member_ids
                if isinstance(value, str) and value != ctx.user_id
            ),
        ]
        if isinstance(requested_member_ids, list)
        else [ctx.user_id]
    )
    requested_members = body.get("members")
    members = (
        [dict(member) for member in requested_members if isinstance(member, dict)]
        if isinstance(requested_members, list)
        else []
    )
    _require_unique_project_members(members)
    creator_member = next(
        (member for member in members if project_member_user_id(member) == ctx.user_id),
        None,
    )
    if creator_member is None:
        members.append({"user_id": ctx.user_id, "roles": list(ctx.roles)})
    else:
        requested_roles = creator_member.get("roles")
        creator_member["roles"] = list(
            dict.fromkeys(
                [
                    *(requested_roles if isinstance(requested_roles, list) else []),
                    *ctx.roles,
                ]
            )
        )
    _require_unique_project_members(members)
    data = {
        **body,
        "project_id": project_id,
        "tenant_id": ctx.tenant_id,
        "member_user_ids": member_user_ids,
        "members": members,
        "trace_id": ctx.trace_id,
    }
    project = Project(
        project_id=project_id,
        tenant_id=ctx.tenant_id,
        name=body.get("name", project_id),
        status=body.get("status", "active"),
        data=data,
    )
    session.add(project)
    record_audit(
        session,
        ctx,
        action="projects.create",
        object_type="project",
        object_id=project_id,
        before=None,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="project.created",
        aggregate_type="project",
        aggregate_id=project_id,
        payload=data,
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation="projects.create",
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/projects/{id}")
def get_projects_by_id(id: str, session: SessionDep, ctx: ContextDep):
    if id != ctx.project_id and "system" not in ctx.roles:
        raise ApiError(
            "PROJECT_CONTEXT_MISMATCH",
            "目标项目与 X-Project-Id 上下文不一致，请先切换项目",
            403,
        )
    project = session.get(Project, id)
    if not project:
        raise ApiError("NOT_FOUND", f"项目不存在：{id}", 404)
    if project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
        raise ApiError("FORBIDDEN", "当前上下文无权访问目标项目", 403)
    return envelope(project.data, ctx)


@router.patch("/projects/{id}")
async def patch_projects_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), action="projects.patch")
    if id != ctx.project_id and "system" not in ctx.roles:
        raise ApiError(
            "PROJECT_CONTEXT_MISMATCH",
            "目标项目与 X-Project-Id 上下文不一致，请先切换项目",
            403,
        )
    body_hash = await request_hash(request)
    replay = replay_or_conflict(session, ctx, operation="projects.patch", body_hash=body_hash)
    if replay is not None:
        return replay
    project = session.get(Project, id)
    body = await request.json()
    if project:
        if project.tenant_id != ctx.tenant_id and "system" not in ctx.roles:
            raise ApiError("FORBIDDEN", "当前上下文无权修改目标项目", 403)
        if "members" in body:
            _require_unique_project_members(body["members"])
        before = dict(project.data)
        project.data = {**project.data, **body, "trace_id": ctx.trace_id}
        project.status = project.data.get("status", project.status)
        record_audit(
            session,
            ctx,
            action="projects.patch",
            object_type="project",
            object_id=id,
            before=before,
            after=project.data,
        )
        enqueue_event(
            session,
            ctx,
            event_type="project.patched",
            aggregate_type="project",
            aggregate_id=id,
            payload=project.data,
        )
        response = envelope(project.data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation="projects.patch",
            body_hash=body_hash,
            status_code=200,
            response_json=response,
        )
        session.commit()
        return response
    raise ApiError("NOT_FOUND", f"项目不存在：{id}", 404)


@router.get("/connectors")
def get_connectors(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "connectors", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.get("/connectors/{id}")
def get_connectors_by_id(id: str, session: SessionDep, ctx: ContextDep) -> dict[str, Any]:
    return envelope(get_resource(session, ctx, "connectors", id).data, ctx)


@router.post("/connectors", status_code=201)
async def post_connectors(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "connectors",
        key_prefix="connector",
        status="draft",
        reject_existing=True,
        prepare_payload=lambda payload: prepare_connector_payload(session, ctx, payload),
    )


@router.patch("/connectors/{id}")
async def patch_connectors_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(
        session,
        ctx,
        request,
        "connectors",
        id,
        prepare_payload=lambda resource, payload: prepare_connector_payload(
            session,
            ctx,
            payload,
            existing_payload=resource.data,
        ),
    )


@router.get("/data-sources/{source_id}/records")
def get_data_source_records(source_id: str, session: SessionDep, ctx: ContextDep):
    records = [
        item
        for item in list_resource_data(session, ctx, "data_source_records", limit=500)
        if item.get("source_id") == source_id
    ]
    return collection_envelope(records, ctx, total=len(records), limit=len(records))


@router.post("/audio-ingest/recordings", status_code=202)
async def post_audio_ingest_recordings(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="audio_ingest",
        event_type="audio_ingest.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/authenticated-events")
def get_authenticated_events(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "documents", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/platform-sync-jobs", status_code=202)
async def post_platform_sync_jobs(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="platform_sync",
        event_type="platform_sync.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/knowledge-sources")
def get_knowledge_sources(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "knowledge_sources", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.get("/knowledge-sources/{id}")
def get_knowledge_sources_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "knowledge_sources", id).data, ctx)


@router.post("/knowledge-sources/{id}/sync-runs", status_code=202)
async def post_knowledge_sources_by_id_sync_runs(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    source = get_resource(session, ctx, "knowledge_sources", id).data
    qdrant_payload = knowledge_qdrant_payload(ctx, source, source_id=id)
    body = qdrant_caller_fields(await request.json())
    return await create_run(
        session,
        ctx,
        request,
        run_type="knowledge_sync",
        event_type="knowledge_source.sync_requested",
        payload={
            **body,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "trace_id": ctx.trace_id,
            "knowledge_source_id": id,
            "source_id": id,
            "source_type": source.get("source_type"),
            "asset_key": qdrant_payload["asset_key"],
            "version": qdrant_payload["version"],
            "vector_collection": qdrant_payload["collection"],
            "qdrant_payload": qdrant_payload,
            "affected_objects": [{"type": "knowledge_source", "id": id}],
        },
        status="pending",
    )


@router.get("/knowledge-indexes")
def get_knowledge_indexes(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "knowledge_indexes", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.get("/knowledge-indexes/{id}")
def get_knowledge_indexes_by_id(id: str, session: SessionDep, ctx: ContextDep):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    gates = [
        item
        for item in list_resource_data(session, ctx, "knowledge_quality_gates", limit=200)
        if item.get("knowledge_index_id") == id
    ]
    effect = next(
        (
            item
            for item in list_resource_data(session, ctx, "knowledge_effects", limit=200)
            if item.get("knowledge_index_id") == id
        ),
        None,
    )
    return envelope({**index, "quality_gates": gates, "effect": effect}, ctx)


@router.post("/knowledge-indexes/{id}/build-runs", status_code=202)
async def post_knowledge_indexes_by_id_build_runs(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    source_id = str(index.get("source_id"))
    source = get_resource(session, ctx, "knowledge_sources", str(source_id)).data
    qdrant_payload = knowledge_qdrant_payload(
        ctx,
        source,
        index=index,
        source_id=source_id,
        index_id=id,
    )
    body = qdrant_caller_fields(
        parse_payload(KnowledgeBuildRequest, await request.json()).model_dump(exclude_none=True)
    )
    return await create_run(
        session,
        ctx,
        request,
        run_type="knowledge_build",
        event_type="knowledge_index.build_requested",
        payload={
            **body,
            "tenant_id": ctx.tenant_id,
            "project_id": ctx.project_id,
            "trace_id": ctx.trace_id,
            "knowledge_index_id": id,
            "source_id": source_id,
            "knowledge_source_id": source_id,
            "source_type": source.get("source_type"),
            "asset_key": qdrant_payload["asset_key"],
            "version": qdrant_payload["version"],
            "vector_collection": qdrant_payload["collection"],
            "qdrant_payload": qdrant_payload,
            "affected_objects": [{"type": "knowledge_index", "id": id}],
        },
        status="pending",
    )


@router.post("/knowledge-indexes/{id}/recall")
async def post_knowledge_indexes_by_id_recall(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    index = get_resource(session, ctx, "knowledge_indexes", id).data
    source_id = str(index.get("source_id"))
    source = get_resource(session, ctx, "knowledge_sources", str(source_id)).data
    qdrant_payload = knowledge_qdrant_payload(
        ctx,
        source,
        index=index,
        source_id=source_id,
        index_id=id,
    )
    body = parse_payload(KnowledgeRecallRequest, await request.json())
    for field_name, expected in (("tenant_id", ctx.tenant_id), ("project_id", ctx.project_id)):
        requested = body.scope.get(field_name)
        if requested is not None and requested != expected:
            raise ApiError(
                "KNOWLEDGE_RECALL_SCOPE_FORBIDDEN",
                f"知识召回不能覆盖当前 {field_name}",
                403,
            )
    result = recall_knowledge_index(
        session,
        ctx,
        knowledge_index_id=id,
        qdrant_payload=qdrant_payload,
        query=body.query,
        top_k=body.top_k,
    )
    return envelope(result, ctx)


@router.get("/knowledge-indexes/{id}/quality-gates")
def get_knowledge_indexes_by_id_quality_gates(
    id: str, session: SessionDep, ctx: ContextDep, page: PaginationDep
):
    get_resource(session, ctx, "knowledge_indexes", id)
    items = [
        item
        for item in list_resource_data(session, ctx, "knowledge_quality_gates", limit=200)
        if item.get("knowledge_index_id") == id
    ]
    return collection_envelope(
        items[: page_limit(page)],
        ctx,
        limit=page_limit(page),
        meta={"status_counts": status_counts(items)},
    )


@router.get("/knowledge-indexes/{id}/effects")
def get_knowledge_indexes_by_id_effects(id: str, session: SessionDep, ctx: ContextDep):
    get_resource(session, ctx, "knowledge_indexes", id)
    effect = next(
        (
            item
            for item in list_resource_data(session, ctx, "knowledge_effects", limit=200)
            if item.get("knowledge_index_id") == id
        ),
        None,
    )
    return envelope(effect or {"knowledge_index_id": id, "status": "empty"}, ctx)


@router.get("/settings")
def get_settings_list(session: SessionDep, ctx: ContextDep, page: PaginationDep):
    resource_page = list_resource_page(session, ctx, "settings", page)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.get("/settings/{id}")
def get_settings_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "settings", id).data, ctx)


@router.patch("/settings/{id}")
async def patch_settings_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(
        session,
        ctx,
        request,
        "settings",
        id,
        status="draft",
        operation=f"settings.patch:{id}",
    )


@router.post("/settings/drafts", status_code=201)
async def post_settings_drafts(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "settings_drafts",
        key_prefix="settings_draft",
        status="draft",
    )


@router.get("/settings/drafts/{id}")
def get_settings_drafts_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "settings_drafts", id).data, ctx)


@router.post("/settings/publish-requests", status_code=202)
async def post_settings_publish_requests(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), "settings.publish")
    body = await request.json()
    return await create_run(
        session,
        ctx,
        request,
        run_type="settings_publish",
        event_type="settings.publish_requested",
        payload=body,
        status="blocked",
        prepare_payload=lambda payload: prepare_settings_publish(session, ctx, payload),
    )


@router.post("/settings/provider-tests", status_code=202)
async def post_settings_provider_tests(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_run(
        session,
        ctx,
        request,
        run_type="provider_test",
        event_type="provider_test.requested",
        payload=await request.json(),
        status="pending",
    )


@router.get("/output-sinks/platform-callbacks")
def get_platform_callbacks(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    status: str | None = None,
):
    run_page = list_run_page(
        session,
        ctx,
        page,
        run_type="external_callback",
        status=status,
    )
    return collection_envelope(
        run_page.items,
        ctx,
        total=run_page.total,
        limit=run_page.limit,
        next_cursor=run_page.next_cursor,
    )


@router.post("/output-sinks/platform-callbacks", status_code=202)
async def post_platform_callbacks(
    request: Request,
    _body: ExternalCallbackRequest,
    session: SessionDep,
    ctx: ContextDep,
):
    require_any_role(
        ctx, ("project_admin", "asset_manager"), "output_sinks.platform_callbacks.create"
    )
    body = parse_payload(ExternalCallbackRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await create_run(
        session,
        ctx,
        request,
        run_type="external_callback",
        event_type="external_callback.requested",
        payload=body,
        status="pending",
    )


@router.post(
    "/output-sinks/platform-callbacks/{id}/completion-receipts",
    responses={
        202: {"model": RunCompletionReceiptPendingResponse},
    },
)
async def post_platform_callbacks_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(session, ctx, request, id, body)


@router.post(
    "/runs/{id}/completion-receipts",
    responses={
        202: {"model": RunCompletionReceiptPendingResponse},
    },
)
async def post_runs_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(session, ctx, request, id, body)


@router.get("/runs/{id}")
def get_runs_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_run(session, ctx, id), ctx)


@router.post("/runs/{id}/decisions")
async def post_runs_by_id_decisions(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunReleaseDecisionRequest, await request.json()).model_dump()
    return await decide_release_gate(session, ctx, request, id, body)


@router.post("/runs/{id}/retries", status_code=202)
async def post_runs_by_id_retries(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    body = parse_payload(TaskRunRetryRequest, await request.json()).model_dump(exclude_none=True)
    return await retry_run(session, ctx, request, id, body)


@router.post(
    "/runs/{id}/external-completion-receipts",
    response_model=PublicRunEnvelope[PublicRunDetail],
    responses={
        202: {"model": RunCompletionReceiptPendingResponse},
        422: {"model": ApiErrorEnvelope, "description": "请求参数校验失败"},
    },
)
async def post_runs_by_id_external_completion_receipts(
    id: str,
    body: ExternalRunCompletionReceiptRequest,
    request: Request,
    session: SessionDep,
    ctx: SignedCompletionContextDep,
    _signature_headers: ExternalCompletionHmacHeadersDep,
):
    return await complete_run_from_receipt(
        session,
        ctx,
        request,
        id,
        body.model_dump(exclude_none=True),
        strict_external_receipt=True,
        completion_auth=getattr(request.state, "completion_signature", None),
    )


@router.post(
    "/runs/{id}/external-progress-receipts",
    status_code=202,
    responses={
        422: {"model": ApiErrorEnvelope, "description": "请求参数校验失败"},
    },
)
async def post_runs_by_id_external_progress_receipts(
    id: str,
    body: ExternalRunProgressReceiptRequest,
    request: Request,
    session: SessionDep,
    ctx: SignedCompletionContextDep,
    _signature_headers: ExternalCompletionHmacHeadersDep,
) -> dict[str, Any]:
    body_hash = await request_hash(request)
    operation = f"audio_import.external_progress:{id}"
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        return replay
    data = apply_audio_import_progress(
        session,
        ctx,
        run_id=id,
        payload=body.model_dump(),
        completion_auth=getattr(request.state, "completion_signature", None),
    )
    response = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=202,
        response_json=response,
    )
    session.commit()
    return response


@router.get("/work-items")
def get_work_items(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "work_items", page, status=status)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
    )


@router.post("/work-items", status_code=201)
async def post_work_items(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "work_items",
        key_prefix="work_item",
        status="draft",
    )


@router.get("/work-items/{id}")
def get_work_items_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "work_items", id).data, ctx)


@router.patch("/work-items/{id}")
async def patch_work_items_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(session, ctx, request, "work_items", id)


@router.post(
    "/exports",
    status_code=202,
    response_model=PublicRunEnvelope[ExportJob],
    response_model_exclude_unset=True,
)
async def post_exports(request: Request, session: SessionDep, ctx: ContextDep):
    require_any_role(ctx, ("project_admin",), "exports.create")
    body = prepare_export_payload(session, ctx, await request.json())
    response = await create_run(
        session,
        ctx,
        request,
        run_type="export",
        event_type="export.requested",
        payload=body,
        status="pending",
    )
    data = response.get("data")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str):
        raise ApiError("EXPORT_RESPONSE_INVALID", "导出任务缺少运行标识", 500)
    return {**response, "data": export_job_payload(_scoped_export_record(session, ctx, run_id))}


@router.get(
    "/exports/{id}",
    response_model=PublicRunEnvelope[ExportJob],
    response_model_exclude_unset=True,
)
def get_exports_by_id(id: str, session: SessionDep, ctx: ContextDep):
    record = _scoped_export_record(session, ctx, id)
    return envelope(export_job_payload(record), ctx)


@router.get("/exports/{id}/download")
def get_exports_by_id_download(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    return _stream_export_download(
        _scoped_export_record(session, ctx, id),
        request,
        head_only=False,
    )


@router.head("/exports/{id}/download")
def head_exports_by_id_download(
    id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    return _stream_export_download(
        _scoped_export_record(session, ctx, id),
        request,
        head_only=True,
    )


@router.post(
    "/exports/{id}/completion-receipts",
    response_model=PublicRunEnvelope[ExportJob],
    response_model_exclude_unset=True,
)
async def post_exports_by_id_completion_receipts(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    body = parse_payload(RunCompletionReceiptRequest, await request.json()).model_dump(
        exclude_none=True
    )
    return await complete_run_from_receipt(
        session, ctx, request, id, body, response_data=export_job_payload
    )
