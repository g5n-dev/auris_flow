from __future__ import annotations

import hashlib
import logging
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

from fastapi import APIRouter, Header, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.api.deps import ContextDep, PaginationDep, SessionDep
from app.core.audio_playback import create_audio_playback_grant
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.http_headers import content_disposition_header
from app.core.http_transport import open_url_no_redirect as urlopen
from app.core.logging import get_logger, log_event
from app.core.rbac import require_any_role
from app.core.response import collection_envelope, envelope
from app.models import (
    AsrAnnotationCorrection,
    AudioRecording,
    ListeningAnnotation,
    StorageObject,
)
from app.schemas import (
    AsrTranscriptCorrectionRequest,
    AudioIntelligenceRunRequest,
    AudioRecordingObjectRequest,
    parse_payload,
)
from app.schemas.manual_label_drafts import (
    ManualLabelDraftCreateRequest,
    ManualLabelDraftRebaseRequest,
    ManualLabelDraftSubmitRequest,
)
from app.schemas.public_runs import PublicRunDetail, PublicRunEnvelope
from app.services.adapters import object_storage_client_for_provider
from app.services.asr_annotation_correction_service import (
    ASR_CORRECTION_WRITE_ROLES,
    record_asr_annotation_correction,
)
from app.services.audio_intelligence_service import audio_intelligence_output_assets
from app.services.audio_object_verification import (
    MAX_WAV_PROBE_BYTES,
    require_streamed_checksum_size,
    stream_verified_sha256,
    verify_remote_audio_object,
)
from app.services.audio_playback_service import (
    AUDIO_PLAYBACK_READ_ROLES,
    authorize_audio_playback_grant,
)
from app.services.audio_review_projection_service import (
    persist_listening_annotation_projection,
    persist_voiceprint_enrollment_projection,
)
from app.services.audio_task_binding_service import resolve_audio_hotword_task_binding
from app.services.audit_service import record_audit
from app.services.hotword_service import get_hotword_version, validate_hotword_execution
from app.services.idempotency_service import (
    replay_or_conflict,
    request_hash,
    save_idempotency_result,
)
from app.services.manual_label_draft_service import (
    create_manual_label_draft,
    is_manual_label_draft_projection,
    rebase_manual_label_draft,
    submit_manual_label_draft,
)
from app.services.outbox_service import enqueue_event
from app.services.read_policy_service import require_voiceprint_sensitive_read
from app.services.resource_service import (
    create_idempotent_json_resource,
    get_resource,
    list_resource_data,
    list_resource_page,
    patch_idempotent_json_resource,
    status_counts,
    upsert_idempotent_json_resource,
    upsert_resource,
)
from app.services.run_service import create_run

router = APIRouter(tags=["audio-sessions"])
settings = get_settings()
logger = get_logger("audio_sessions")

SYNTHETIC_WAV_DATA_SIZE = 32_000
SYNTHETIC_WAV_HEADER_SIZE = 44

MANUAL_LABEL_DRAFT_WRITE_ROLES = (
    "project_admin",
    "asset_manager",
    "review_arbitrator",
)
_MANUAL_LABEL_CREATE_HTTP_OPERATION = "http.manual_label_drafts.create"
_MANUAL_LABEL_SUBMIT_HTTP_OPERATION = "http.manual_label_drafts.submit"
_MANUAL_LABEL_REBASE_HTTP_OPERATION = "http.manual_label_drafts.rebase"

_AUDIO_PLAYBACK_COMMON_RESPONSE_HEADERS: dict[str, dict[str, Any]] = {
    "Accept-Ranges": {
        "schema": {"type": "string", "enum": ["bytes"]},
    },
    "Content-Length": {
        "schema": {"type": "integer"},
    },
    "ETag": {
        "schema": {"type": "string"},
    },
}
_AUDIO_PLAYBACK_PARTIAL_RESPONSE_HEADERS: dict[str, dict[str, Any]] = {
    **_AUDIO_PLAYBACK_COMMON_RESPONSE_HEADERS,
    "Content-Range": {
        "required": True,
        "schema": {
            "type": "string",
            "example": "bytes 0-65535/320044",
        },
    },
}
_AUDIO_PLAYBACK_UNSATISFIABLE_RESPONSE_HEADERS: dict[str, dict[str, Any]] = {
    "Accept-Ranges": {
        "schema": {"type": "string", "enum": ["bytes"]},
    },
    "Content-Range": {
        "required": True,
        "schema": {
            "type": "string",
            "example": "bytes */320044",
        },
    },
}
_AUDIO_BINARY_CONTENT: dict[str, dict[str, Any]] = {
    "audio/wav": {
        "schema": {
            "type": "string",
            "format": "binary",
        }
    }
}
_AUDIO_PLAYBACK_GET_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "完整音频字节",
        "headers": _AUDIO_PLAYBACK_COMMON_RESPONSE_HEADERS,
        "content": _AUDIO_BINARY_CONTENT,
    },
    206: {
        "description": "满足单区间请求的音频字节",
        "headers": _AUDIO_PLAYBACK_PARTIAL_RESPONSE_HEADERS,
        "content": _AUDIO_BINARY_CONTENT,
    },
    416: {
        "description": "多区间、格式错误或不可满足的 Range",
        "headers": _AUDIO_PLAYBACK_UNSATISFIABLE_RESPONSE_HEADERS,
    },
}
_AUDIO_PLAYBACK_HEAD_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "完整音频表示的元数据，不包含响应正文",
        "headers": {
            **_AUDIO_PLAYBACK_COMMON_RESPONSE_HEADERS,
            "Content-Length": {
                "required": True,
                "schema": {"type": "integer"},
            },
        },
    },
    206: {
        "description": "满足单区间请求的音频元数据，不包含响应正文",
        "headers": {
            **_AUDIO_PLAYBACK_PARTIAL_RESPONSE_HEADERS,
            "Content-Length": {
                "required": True,
                "schema": {"type": "integer"},
            },
        },
    },
    416: {
        "description": "多区间、格式错误或不可满足的 Range",
        "headers": _AUDIO_PLAYBACK_UNSATISFIABLE_RESPONSE_HEADERS,
    },
}
_AUDIO_RANGE_HEADER = Header(
    default=None,
    alias="Range",
    description=(
        "RFC 9110 单字节区间。支持闭区间、开放尾端和后缀区间；多区间与无效或不可满足区间返回 416。"
    ),
)
_AUDIO_IF_RANGE_HEADER = Header(
    default=None,
    alias="If-Range",
    description="登记对象的 ETag；不匹配时忽略 Range 并返回完整表示。",
)


async def _begin_manual_label_http_operation(
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    *,
    operation: str,
) -> tuple[str, dict[str, Any] | None]:
    body_hash = await request_hash(request)
    replay = replay_or_conflict(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
    )
    if replay is not None:
        session.commit()
    return body_hash, replay


def _complete_manual_label_http_operation(
    session: SessionDep,
    ctx: ContextDep,
    *,
    operation: str,
    body_hash: str,
    status_code: int,
    data: dict[str, Any],
) -> dict[str, Any]:
    result = envelope(data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=status_code,
        response_json=result,
    )
    session.commit()
    return result


class _AudioObjectStreamTruncatedError(RuntimeError):
    """Raised after headers are sent when object storage ends a stream early."""


class _ObjectStorageStreamingResponse(StreamingResponse):
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


def _real_object_storage_enabled() -> bool:
    mode = settings.auris_object_storage_adapter.lower().strip()
    if mode == "real":
        return True
    if mode == "local" and settings.app_env in {"local", "test", "ci"}:
        return False
    raise ApiError(
        "AUDIO_STORAGE_MODE_INVALID",
        "当前环境未配置真实对象存储，禁止降级为合成音频",
        503,
        retryable=False,
    )


def _require_server_audio_inference_policy(
    *,
    provider: str | None,
    model: str | None,
) -> None:
    if settings.auris_dagster_adapter.strip().lower() != "real":
        return
    configured_provider = settings.auris_audio_inference_provider.strip()
    allowed_models = {
        value.strip()
        for value in settings.auris_audio_inference_allowed_models.split(",")
        if value.strip()
    }
    if provider != configured_provider or model not in allowed_models:
        raise ApiError(
            "AUDIO_INFERENCE_POLICY_VIOLATION",
            "音频推理 Provider 或模型不在服务端批准策略内",
            422,
            retryable=False,
        )


def _numeric(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _voiceprint_min_consistency(consistency: object) -> float:
    if not isinstance(consistency, dict):
        return 0.0
    values = [_numeric(value) for value in consistency.values()]
    return min(values) if values else 0.0


def _voiceprint_quality_gate(body: dict) -> dict:
    raw_quality = body.get("quality")
    raw_consistency = body.get("consistency")
    quality: dict[str, object] = raw_quality if isinstance(raw_quality, dict) else {}
    consistency: dict[str, object] = raw_consistency if isinstance(raw_consistency, dict) else {}
    min_consistency = _numeric(
        body.get("min_consistency"), _voiceprint_min_consistency(consistency)
    )
    checks = [
        {
            "key": "overall",
            "label": "总体质量",
            "value": _numeric(quality.get("overall")),
            "threshold": 85,
            "passed": _numeric(quality.get("overall")) >= 85,
        },
        {
            "key": "duration",
            "label": "有效时长",
            "value": _numeric(quality.get("duration")),
            "threshold": 80,
            "passed": _numeric(quality.get("duration")) >= 80,
        },
        {
            "key": "snr",
            "label": "信噪比",
            "value": _numeric(quality.get("snr")),
            "threshold": 75,
            "passed": _numeric(quality.get("snr")) >= 75,
        },
        {
            "key": "purity",
            "label": "纯净度",
            "value": _numeric(quality.get("purity")),
            "threshold": 75,
            "passed": _numeric(quality.get("purity")) >= 75,
        },
        {
            "key": "stability",
            "label": "稳定性",
            "value": _numeric(quality.get("stability")),
            "threshold": 80,
            "passed": _numeric(quality.get("stability")) >= 80,
        },
        {
            "key": "consistency",
            "label": "样本一致性",
            "value": min_consistency,
            "threshold": 0.82,
            "passed": min_consistency >= 0.82,
        },
    ]
    return {
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
        "min_consistency": min_consistency,
    }


def _voiceprint_enrollment_status(body: dict, ctx: ContextDep) -> str:
    gate = _voiceprint_quality_gate(body)
    if not gate["passed"]:
        return "blocked"
    if "review_arbitrator" in ctx.roles:
        return "enrolled"
    return "pending_review"


def _voiceprint_enrollment_objects(voiceprint_id: str, body: dict) -> list[dict[str, str]]:
    objects = [{"type": "voiceprint", "id": voiceprint_id}]
    field_types = [
        ("audio_session_id", "audio_session"),
        ("recording_id", "recording"),
        ("asset_key", "data_asset"),
        ("voice_asset_key", "data_asset"),
    ]
    for field, object_type in field_types:
        value = body.get(field)
        if isinstance(value, str) and value:
            objects.append({"type": object_type, "id": value})
    samples = body.get("samples")
    if isinstance(samples, list):
        for sample in samples[:8]:
            if isinstance(sample, dict):
                sample_id = sample.get("sample_id") or sample.get("id")
                if isinstance(sample_id, str) and sample_id:
                    objects.append({"type": "voiceprint_sample", "id": sample_id})
    return objects


def _voiceprint_qdrant_payload(
    ctx: ContextDep,
    *,
    enrollment_id: str,
    voiceprint_id: str,
    body: dict,
    gate: dict,
    embedding_ref: dict,
) -> dict:
    collection = embedding_ref.get("collection") or "voiceprint_embeddings"
    version = (
        body.get("voiceprint_version")
        or body.get("model_version")
        or body.get("version")
        or "voiceprint-v1"
    )
    voice_asset_key = (
        body.get("voice_asset_key") or body.get("asset_key") or f"auris/voiceprints/{voiceprint_id}"
    )
    return {
        "tenant_id": ctx.tenant_id,
        "project_id": ctx.project_id,
        "trace_id": ctx.trace_id,
        "collection": collection,
        "knowledge_index_id": "voiceprint_identity_index",
        "knowledge_source_id": "voiceprint_enrollment_templates",
        "source_id": voiceprint_id,
        "source_type": "voiceprint_enrollment",
        "asset_key": voice_asset_key,
        "version": version,
        "voiceprint_id": voiceprint_id,
        "enrollment_id": enrollment_id,
        "embedding_ref": embedding_ref,
        "quality_gate": gate,
        "business_ref": {
            "voiceprint_id": voiceprint_id,
            "enrollment_id": enrollment_id,
            "audio_session_id": body.get("audio_session_id"),
            "recording_id": body.get("recording_id"),
            "speaker_id": body.get("speaker_id"),
            "employee_ref": body.get("employee_ref"),
            "sample_count": len(body.get("samples") or [])
            if isinstance(body.get("samples"), list)
            else 0,
        },
    }


def _runtime_audio_items(
    session: SessionDep, ctx: ContextDep, collection: str, audio_session_id: str
) -> list[dict]:
    items = [
        item
        for item in list_resource_data(session, ctx, collection)
        if item.get("audio_session_id") == audio_session_id
    ]
    return sorted(
        items, key=lambda item: (0 if item.get("source_run_id") else 1, item.get("id", ""))
    )


def _recording_for_session(
    session: SessionDep,
    ctx: ContextDep,
    audio_session_id: str,
    *,
    include_internal_storage_version: bool = False,
) -> dict:
    session_resource = get_resource(session, ctx, "audio_sessions", audio_session_id)
    recording_id = session_resource.data.get("recording_id")
    strong_recording = session.scalar(
        select(AudioRecording).where(
            AudioRecording.recording_id == recording_id,
            AudioRecording.tenant_id == ctx.tenant_id,
            AudioRecording.project_id == ctx.project_id,
        )
    )
    recording = dict(strong_recording.payload) if strong_recording else None
    if recording is None:
        recording = next(
            (
                item
                for item in list_resource_data(session, ctx, "recordings")
                if item.get("recording_id") == recording_id
            ),
            None,
        )
    if not recording:
        raise ApiError("RECORDING_NOT_FOUND", "当前会话没有可调听的录音对象", 404)
    storage_object = _storage_object_for_recording(session, ctx, str(recording_id or ""))
    if storage_object:
        recording = {**recording, "storage_object_id": storage_object.storage_object_id}
        recording["storage_object"] = _storage_object_data(
            storage_object,
            include_internal_version=include_internal_storage_version,
        )
    return recording


def _storage_object_for_recording(
    session: SessionDep, ctx: ContextDep, recording_id: str
) -> StorageObject | None:
    if not recording_id:
        return None
    return session.scalar(
        select(StorageObject)
        .where(
            StorageObject.tenant_id == ctx.tenant_id,
            StorageObject.project_id == ctx.project_id,
            StorageObject.source_type == "audio_recording",
            StorageObject.source_id == recording_id,
            StorageObject.status != "superseded",
        )
        .order_by(StorageObject.updated_at.desc(), StorageObject.storage_object_id.desc())
    )


def _storage_object_data(
    storage_object: StorageObject,
    *,
    include_internal_version: bool = False,
) -> dict[str, Any]:
    data = {
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
        "trace_id": storage_object.trace_id,
    }
    if include_internal_version and isinstance(storage_object.payload, dict):
        object_version_id = storage_object.payload.get("object_version_id")
        if isinstance(object_version_id, str):
            data["object_version_id"] = object_version_id
    return data


def _require_immutable_storage_object_registration(
    existing: StorageObject | None,
    *,
    tenant_id: str,
    project_id: str,
    source_type: str,
    source_id: str,
    provider: str,
    bucket: str,
    object_key: str,
    content_type: str,
    size_bytes: int,
    content_sha256: str,
    etag: str | None,
    object_version_id: str | None,
) -> None:
    if existing is None:
        return
    if existing.tenant_id != tenant_id or existing.project_id != project_id:
        raise ApiError("STORAGE_OBJECT_ID_CONFLICT", "对象标识已被其他作用域占用", 409)
    if existing.source_type != source_type or existing.source_id != source_id:
        raise ApiError(
            "STORAGE_OBJECT_SOURCE_CONFLICT",
            "对象标识已绑定到其他业务来源，请为新来源使用新的对象标识",
            409,
        )

    requested_identity: dict[str, str | int | None] = {
        "provider": provider,
        "bucket": bucket,
        "object_key": object_key,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "content_sha256": content_sha256,
        "etag": etag,
    }
    changed_fields = [
        field
        for field, requested_value in requested_identity.items()
        if (stored_value := getattr(existing, field)) is not None
        and stored_value != requested_value
    ]
    stored_version_id = (
        existing.payload.get("object_version_id") if isinstance(existing.payload, dict) else None
    )
    if stored_version_id is not None and stored_version_id != object_version_id:
        changed_fields.append("object_version_id")
    if changed_fields:
        raise ApiError(
            "STORAGE_OBJECT_IDENTITY_CONFLICT",
            "对象标识对应的存储定位或内容版本不可变，请为新版本使用新的对象标识",
            409,
            details=[{"field": field} for field in changed_fields],
        )


def _storage_response_headers(recording: dict) -> dict[str, str]:
    raw = recording.get("storage_object")
    storage = raw if isinstance(raw, dict) else {}
    headers: dict[str, str] = {}
    storage_object_id = storage.get("storage_object_id") or recording.get("storage_object_id")
    if isinstance(storage_object_id, str) and storage_object_id:
        headers["X-Storage-Object-Id"] = storage_object_id
    provider = storage.get("provider")
    if isinstance(provider, str) and provider:
        headers["X-Storage-Provider"] = provider
    etag = storage.get("etag")
    if isinstance(etag, str) and etag:
        headers["ETag"] = etag if etag.startswith('"') else f'"{etag}"'
    return headers


def _effective_range_header(
    range_header: str | None,
    if_range: str | None,
    recording: dict,
) -> str | None:
    if not range_header:
        return None
    if not if_range:
        return range_header
    response_etag = _storage_response_headers(recording).get("ETag")
    if not response_etag or if_range.strip() != response_etag:
        return None
    return range_header


def _close_object_stream(result: dict[str, Any]) -> None:
    stream = result.get("stream")
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _strong_etag_opaque(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.startswith("W/"):
        raise ApiError(
            "AUDIO_OBJECT_ETAG_WEAK",
            "录音对象必须使用强 ETag 才能进行版本锁定",
            409,
            retryable=False,
        )
    if raw.startswith('"') or raw.endswith('"'):
        if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
            raise ApiError(
                "AUDIO_OBJECT_ETAG_INVALID",
                "录音对象 ETag 格式无效",
                409,
                retryable=False,
            )
        raw = raw[1:-1]
    if not raw or '"' in raw or any(ord(char) < 0x21 for char in raw):
        raise ApiError(
            "AUDIO_OBJECT_ETAG_INVALID",
            "录音对象 ETag 格式无效",
            409,
            retryable=False,
        )
    return raw


def _exact_object_version_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value.casefold() == "null"
        or len(value) > 1024
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ApiError(
            "AUDIO_OBJECT_VERSION_ID_UNAVAILABLE",
            "录音对象缺少已登记的精确版本 ID，无法安全播放",
            409,
            retryable=False,
        )
    return value


def _normalized_audio_content_type(value: object, *, upstream: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 128:
        normalized = ""
    else:
        normalized = value.partition(";")[0].strip().casefold()
    if normalized not in {"audio/wav", "audio/x-wav"}:
        raise ApiError(
            (
                "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH"
                if upstream
                else "AUDIO_OBJECT_CONTENT_TYPE_UNSUPPORTED"
            ),
            (
                "对象存储返回的音频类型与登记元数据不一致"
                if upstream
                else "录音对象的媒体类型不在允许列表中"
            ),
            502 if upstream else 409,
            retryable=False,
        )
    return "audio/wav"


def _require_exact_response_version(result: dict[str, Any], registered_version_id: str) -> None:
    response_version_id = result.get("version_id")
    if not isinstance(response_version_id, str) or not response_version_id:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_VERSION_ID_MISSING",
            "对象存储响应缺少精确版本 ID，无法确认录音版本",
            502,
            retryable=True,
        )
    if response_version_id != registered_version_id:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_VERSION_CHANGED",
            "录音对象版本已变化，请重新登记并获取播放授权",
            412,
            retryable=False,
        )


def _etag_header(value: str) -> str:
    return f'"{value}"'


def _open_object_with_if_match(
    client: Any,
    bucket: str,
    object_key: str,
    *,
    byte_range: str | None,
    registered_etag: str,
    registered_version_id: str,
) -> dict[str, Any]:
    if_match = _etag_header(registered_etag)
    signed_request = getattr(client, "_signed_request", None)
    if callable(signed_request):
        extra_headers = {"If-Match": if_match}
        if byte_range:
            extra_headers["Range"] = byte_range
        request = signed_request(
            "GET",
            f"/{bucket}/{object_key}",
            extra_headers=extra_headers,
            query={"versionId": registered_version_id},
        )
        response = urlopen(request, timeout=5)
        response_etag = response.headers.get("ETag")
        if (
            isinstance(response_etag, str)
            and len(response_etag) >= 2
            and response_etag.startswith('"')
            and response_etag.endswith('"')
        ):
            response_etag = response_etag[1:-1]
        version_header = {
            "minio": "x-amz-version-id",
            "s3": "x-amz-version-id",
            "oss": "x-oss-version-id",
            "obs": "x-obs-version-id",
        }.get(str(getattr(client, "provider", "")))
        return {
            "status": response.status,
            "headers": dict(response.headers.items()),
            # Keep the direct signed-request path aligned with the public
            # object-storage adapter contract: ETag values are opaque and
            # returned without their HTTP header delimiters. The caller still
            # performs strict strong-ETag validation before streaming bytes.
            "etag": response_etag,
            "content_length": response.headers.get("Content-Length"),
            "content_range": response.headers.get("Content-Range"),
            "content_type": response.headers.get("Content-Type"),
            "version_id": response.headers.get(version_header) if version_header else None,
            "stream": response,
        }

    open_object = getattr(client, "open_object", None)
    if callable(open_object):
        return open_object(
            bucket,
            object_key,
            byte_range=byte_range,
            if_match=if_match,
            version_id=registered_version_id,
        )
    return client.get_object(
        bucket,
        object_key,
        byte_range=byte_range,
        if_match=if_match,
        version_id=registered_version_id,
    )


def _head_object_with_if_match(
    client: Any,
    bucket: str,
    object_key: str,
    *,
    registered_etag: str,
    registered_version_id: str,
) -> dict[str, Any]:
    head_object = getattr(client, "head_object", None)
    if not callable(head_object):
        raise ApiError(
            "AUDIO_OBJECT_HEAD_UNSUPPORTED",
            "对象存储 Provider 未提供安全的对象元数据读取能力",
            502,
            retryable=True,
        )
    return head_object(
        bucket,
        object_key,
        if_match=_etag_header(registered_etag),
        version_id=registered_version_id,
    )


def _read_versioned_wav_probe(
    client: Any,
    bucket: str,
    object_key: str,
    *,
    content_length: int,
    registered_etag: str,
    registered_version_id: str,
) -> bytes:
    probe_length = min(content_length, MAX_WAV_PROBE_BYTES)
    try:
        result = _open_object_with_if_match(
            client,
            bucket,
            object_key,
            byte_range=f"bytes=0-{probe_length - 1}",
            registered_etag=registered_etag,
            registered_version_id=registered_version_id,
        )
    except HTTPError as exc:
        if exc.code in {412, 416}:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在验证期间发生变化",
                412,
                retryable=False,
            ) from exc
        raise
    try:
        status = int(result.get("status") or 0)
        if status == 412:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在验证期间发生变化",
                412,
                retryable=False,
            )
        if status != 206:
            raise ApiError(
                "AUDIO_OBJECT_RANGE_UNSUPPORTED",
                "对象存储未按范围返回 WAV 验证数据",
                502,
                retryable=True,
            )
        _require_exact_response_version(result, registered_version_id)
        response_etag = _strong_etag_opaque(result.get("etag"))
        if response_etag is None or response_etag != registered_etag:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在验证期间发生变化",
                412,
                retryable=False,
            )
        stream = result.get("stream")
        if stream is not None and callable(getattr(stream, "read", None)):
            body = stream.read(probe_length + 1)
        else:
            body = result.get("body")
        if not isinstance(body, bytes) or len(body) != probe_length:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "对象存储返回的 WAV 验证数据不完整",
                502,
                retryable=True,
            )
        return body
    finally:
        _close_object_stream(result)


def _read_versioned_object_sha256(
    client: Any,
    bucket: str,
    object_key: str,
    *,
    content_length: int,
    content_type: str,
    registered_etag: str,
    registered_version_id: str,
) -> str:
    require_streamed_checksum_size(content_length)
    try:
        result = _open_object_with_if_match(
            client,
            bucket,
            object_key,
            byte_range=None,
            registered_etag=registered_etag,
            registered_version_id=registered_version_id,
        )
    except HTTPError as exc:
        if exc.code == 412:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在完整性校验期间发生变化",
                412,
                retryable=False,
            ) from exc
        raise ApiError(
            "AUDIO_OBJECT_VERIFY_FAILED",
            "无法读取录音对象进行完整性校验",
            502,
            retryable=True,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise ApiError(
            "AUDIO_OBJECT_VERIFY_FAILED",
            "无法读取录音对象进行完整性校验",
            502,
            retryable=True,
        ) from exc

    try:
        status = int(result.get("status") or 0)
        if status == 412:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在完整性校验期间发生变化",
                412,
                retryable=False,
            )
        if status != 200:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "对象存储未返回完整的录音对象",
                502,
                retryable=True,
            )
        _require_exact_response_version(result, registered_version_id)
        response_etag = _strong_etag_opaque(result.get("etag"))
        if response_etag is None or response_etag != registered_etag:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象在完整性校验期间发生变化",
                412,
                retryable=False,
            )
        try:
            response_length = int(result.get("content_length") or -1)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "对象存储未返回有效的录音长度",
                502,
                retryable=True,
            ) from exc
        if response_length != content_length:
            raise ApiError(
                "AUDIO_OBJECT_SIZE_MISMATCH",
                "完整性校验读取的录音长度与 HEAD 元数据不一致",
                409,
                retryable=False,
            )
        response_type = str(result.get("content_type") or "").partition(";")[0].strip().lower()
        expected_type = content_type.partition(";")[0].strip().lower()
        if response_type != expected_type:
            raise ApiError(
                "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH",
                "完整性校验读取的录音类型与 HEAD 元数据不一致",
                409,
                retryable=False,
            )
        if result.get("content_range") not in {None, ""}:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "对象存储在完整读取时意外返回了范围响应",
                502,
                retryable=True,
            )
        stream = result.get("stream")
        if stream is None:
            body = result.get("body")
            if isinstance(body, bytes):
                stream = BytesIO(body)
                result = {**result, "stream": stream}
        return stream_verified_sha256(stream, expected_size=content_length)
    finally:
        _close_object_stream(result)


def _parse_byte_range(range_header: str | None, total: int) -> tuple[int, int, bool] | None:
    if total <= 0:
        return None
    if not range_header:
        return (0, total - 1, False)
    if not range_header.startswith("bytes=") or "," in range_header:
        return None
    start_raw, _, end_raw = range_header.removeprefix("bytes=").partition("-")
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
    return (start, min(end, total - 1), True)


@lru_cache(maxsize=32)
def _synthetic_wav_bytes(recording_id: str, file_name: str) -> bytes:
    sample_rate = 8000
    data_size = SYNTHETIC_WAV_DATA_SIZE
    seed = sum(recording_id.encode("utf-8")) % 23
    pcm = bytes((index * 7 + seed) % 256 for index in range(data_size))
    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + data_size),
            b"WAVEfmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16),
            b"data",
            struct.pack("<I", data_size),
        ]
    )
    return header + pcm


def _audio_response_plan(
    total: int,
    *,
    range_header: str | None,
    content_type: str,
    file_name: str,
    source: str,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, int, int, dict[str, str]]:
    parsed = _parse_byte_range(range_header, total)
    normalized_content_type = _normalized_audio_content_type(content_type)
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": normalized_content_type,
        "Content-Disposition": content_disposition_header(
            "inline",
            file_name,
            fallback="recording.wav",
        ),
        "X-Audio-Source": source,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-cache",
        "Vary": "Range, Authorization, X-Tenant-Id, X-Project-Id",
        **(extra_headers or {}),
    }
    if parsed is None:
        return (416, 0, -1, {**common_headers, "Content-Range": f"bytes */{total}"})
    start, end, partial = parsed
    headers = {**common_headers, "Content-Length": str(end - start + 1)}
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return (206 if partial else 200, start, end, headers)


def _audio_response(
    body: bytes,
    *,
    range_header: str | None,
    content_type: str,
    file_name: str,
    source: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    status_code, start, end, headers = _audio_response_plan(
        len(body),
        range_header=range_header,
        content_type=content_type,
        file_name=file_name,
        source=source,
        extra_headers=extra_headers,
    )
    if status_code == 416:
        return Response(status_code=status_code, headers=headers)
    payload = body[start : end + 1]
    return Response(content=payload, status_code=status_code, headers=headers)


def _audio_head_response(
    total: int,
    *,
    range_header: str | None,
    content_type: str,
    file_name: str,
    source: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    status_code, _start, _end, headers = _audio_response_plan(
        total,
        range_header=range_header,
        content_type=content_type,
        file_name=file_name,
        source=source,
        extra_headers=extra_headers,
    )
    return Response(status_code=status_code, headers=headers)


def _object_storage_audio_response(
    recording: dict,
    *,
    range_header: str | None,
    head_only: bool = False,
) -> Response:
    raw_storage = recording.get("storage_object")
    storage = raw_storage if isinstance(raw_storage, dict) else {}
    if storage.get("status") not in {"verified", "active"}:
        raise ApiError(
            "AUDIO_OBJECT_NOT_READY",
            "录音对象尚未完成登记验证，暂不可播放",
            409,
            retryable=True,
        )
    object_key = str(
        storage.get("object_key")
        or recording.get("object_key")
        or recording.get("audio_url_ref")
        or recording.get("source_url_ref")
        or ""
    ).strip("/")
    if not object_key:
        raise ApiError("AUDIO_OBJECT_REF_MISSING", "录音缺少对象存储引用", 409)
    bucket = str(
        storage.get("bucket")
        or recording.get("bucket")
        or recording.get("storage_bucket")
        or settings.object_storage_bucket
    )
    content_type = _normalized_audio_content_type(
        storage.get("content_type") or recording.get("content_type") or "audio/wav"
    )
    file_name = str(recording.get("file_name") or f"{recording.get('recording_id')}.wav")
    content_disposition = content_disposition_header(
        "inline",
        file_name,
        fallback="recording.wav",
    )
    total = 0
    result: dict[str, Any] | None = None
    try:
        provider = str(
            storage.get("provider") or recording.get("provider") or settings.object_storage_provider
        ).lower()
        registered_etag = _strong_etag_opaque(storage.get("etag"))
        if registered_etag is None:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_UNAVAILABLE",
                "录音对象缺少已登记的强 ETag，无法安全播放",
                409,
                retryable=False,
            )
        registered_version_id = _exact_object_version_id(storage.get("object_version_id"))
        client = object_storage_client_for_provider(provider)
        if not client.allows_bucket(bucket):
            raise ApiError(
                "AUDIO_STORAGE_BUCKET_NOT_ALLOWED",
                "录音对象所在 bucket 不在当前 Provider 允许列表中",
                403,
                retryable=False,
            )
        registered_total = storage.get("content_length")
        if isinstance(registered_total, int) and registered_total > 0:
            total = registered_total
        else:
            head = _head_object_with_if_match(
                client,
                bucket,
                object_key,
                registered_etag=registered_etag,
                registered_version_id=registered_version_id,
            )
            _require_exact_response_version(head, registered_version_id)
            remote_head_type = _normalized_audio_content_type(
                head.get("content_type"),
                upstream=True,
            )
            if remote_head_type != content_type:
                raise ApiError(
                    "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH",
                    "对象存储返回的音频类型与登记元数据不一致",
                    502,
                    retryable=False,
                )
            result = head if head_only else None
            total = int(head.get("content_length") or 0)
        parsed = _parse_byte_range(range_header, total)
        if parsed is None:
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes */{total}",
                    "Content-Type": content_type,
                    "Content-Disposition": content_disposition,
                    "X-Content-Type-Options": "nosniff",
                    **_storage_response_headers(recording),
                },
            )
        start, end, partial = parsed
        if head_only:
            result = result or _head_object_with_if_match(
                client,
                bucket,
                object_key,
                registered_etag=registered_etag,
                registered_version_id=registered_version_id,
            )
        else:
            result = _open_object_with_if_match(
                client,
                bucket,
                object_key,
                byte_range=range_header if partial else None,
                registered_etag=registered_etag,
                registered_version_id=registered_version_id,
            )
        upstream_status = int(result.get("status") or 0)
        if upstream_status == 412:
            _close_object_stream(result)
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象版本已变化，请重新登记并获取播放授权",
                412,
                retryable=False,
            )
        if (head_only and upstream_status != 200) or (
            not head_only
            and ((partial and upstream_status != 206) or (not partial and upstream_status != 200))
        ):
            _close_object_stream(result)
            raise ApiError(
                "AUDIO_OBJECT_RANGE_UNSUPPORTED",
                f"{provider.upper()} 返回了不符合请求语义的状态码",
                502,
                retryable=True,
            )
    except HTTPError as exc:
        if exc.code == 404:
            raise ApiError("AUDIO_OBJECT_NOT_FOUND", "对象存储中未找到录音文件", 404) from exc
        if exc.code == 416:
            upstream_headers = getattr(exc, "headers", None)
            content_range = (
                upstream_headers.get("Content-Range") if upstream_headers is not None else None
            ) or f"bytes */{total}"
            if exc.fp is not None:
                exc.close()
            return Response(
                status_code=416,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": content_range,
                    "Content-Type": content_type,
                    "Content-Disposition": content_disposition,
                    "X-Content-Type-Options": "nosniff",
                    **_storage_response_headers(recording),
                },
            )
        if exc.code == 412:
            if exc.fp is not None:
                exc.close()
            raise ApiError(
                "AUDIO_OBJECT_VERSION_CHANGED",
                "录音对象版本已变化，请重新登记并获取播放授权",
                412,
                retryable=False,
            ) from exc
        raise ApiError(
            "AUDIO_OBJECT_FETCH_FAILED", "读取对象存储录音失败", 502, retryable=True
        ) from exc
    except ValueError as exc:
        raise ApiError(
            "AUDIO_STORAGE_PROVIDER_NOT_CONFIGURED",
            "当前录音的对象存储 Provider 未正确配置",
            503,
            retryable=False,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise ApiError(
            "AUDIO_OBJECT_FETCH_FAILED", "读取对象存储录音失败", 502, retryable=True
        ) from exc
    assert result is not None
    expected_length = end - start + 1 if partial else total
    upstream_length = result.get("content_length")
    expected_upstream_length = total if head_only else expected_length
    if str(upstream_length or "") != str(expected_upstream_length):
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_RANGE_INVALID",
            "对象存储返回的音频区间长度与请求不一致",
            502,
            retryable=True,
        )
    expected_content_range = f"bytes {start}-{end}/{total}"
    upstream_content_range = result.get("content_range")
    if not head_only and partial and upstream_content_range != expected_content_range:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_RANGE_INVALID",
            "对象存储返回的 Content-Range 与登记元数据不一致",
            502,
            retryable=True,
        )
    _require_exact_response_version(result, registered_version_id)
    upstream_etag = _strong_etag_opaque(result.get("etag"))
    if upstream_etag is None:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_ETAG_MISSING",
            "对象存储响应缺少 ETag，无法确认录音版本",
            502,
            retryable=True,
        )
    if registered_etag != upstream_etag:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_VERSION_CHANGED",
            "录音对象版本已变化，请重新登记并获取播放授权",
            412,
            retryable=False,
        )
    try:
        upstream_content_type = _normalized_audio_content_type(
            result.get("content_type"),
            upstream=True,
        )
    except ApiError:
        _close_object_stream(result)
        raise
    if upstream_content_type != content_type:
        _close_object_stream(result)
        raise ApiError(
            "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH",
            "对象存储返回的音频类型与登记元数据不一致",
            502,
            retryable=False,
        )
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
        "Content-Length": str(expected_length),
        "Content-Disposition": content_disposition,
        "X-Audio-Source": "object-storage",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-cache",
        "Vary": "Range, Authorization, X-Tenant-Id, X-Project-Id",
        **_storage_response_headers(recording),
    }
    if partial:
        headers["Content-Range"] = expected_content_range
    if head_only:
        return Response(status_code=206 if partial else 200, headers=headers)
    stream = result.get("stream")
    if stream is not None and hasattr(stream, "read"):

        async def iter_audio() -> AsyncIterator[bytes]:
            remaining = expected_length
            while remaining > 0:
                chunk = await run_in_threadpool(
                    stream.read,
                    min(64 * 1024, remaining),
                )
                if not chunk:
                    log_event(
                        logger,
                        "audio_object_stream_truncated",
                        level=logging.ERROR,
                        recording_id=recording.get("recording_id"),
                        storage_object_id=storage.get("storage_object_id"),
                        expected_length=expected_length,
                        delivered_length=expected_length - remaining,
                    )
                    raise _AudioObjectStreamTruncatedError(
                        "对象存储音频流在 Content-Length 完成前中断"
                    )
                if len(chunk) > remaining:
                    log_event(
                        logger,
                        "audio_object_stream_overflow",
                        level=logging.ERROR,
                        recording_id=recording.get("recording_id"),
                        storage_object_id=storage.get("storage_object_id"),
                        expected_length=expected_length,
                        delivered_length=expected_length - remaining + len(chunk),
                    )
                    raise _AudioObjectStreamTruncatedError(
                        "对象存储音频流超过登记的 Content-Length"
                    )
                remaining -= len(chunk)
                yield chunk

        return _ObjectStorageStreamingResponse(
            iter_audio(),
            upstream_stream=stream,
            status_code=206 if partial else 200,
            headers=headers,
        )
    body = result.get("body")
    if not isinstance(body, bytes) or len(body) != expected_length:
        raise ApiError("AUDIO_OBJECT_INVALID", "对象存储返回的录音内容不可用", 502)
    return Response(content=body, status_code=206 if partial else 200, headers=headers)


@router.get("/audio-sessions/aggregations")
def get_audio_sessions_aggregations(session: SessionDep, ctx: ContextDep):
    sessions = list_resource_data(session, ctx, "audio_sessions")
    requested_asset_keys = {
        asset_key
        for audio_session in sessions
        if isinstance((asset_key := audio_session.get("target_asset_key")), str)
        and asset_key.strip()
    }
    registered_asset_keys: set[str] = set()
    for asset_key in requested_asset_keys:
        try:
            registered_asset = get_resource(session, ctx, "data_assets", asset_key)
        except ApiError as exc:
            if exc.code == "NOT_FOUND":
                continue
            raise
        if registered_asset.data.get("asset_key") == asset_key:
            registered_asset_keys.add(asset_key)
    connector_blocked_reason = "当前会话未绑定本租户项目内已登记的数据资产"
    unknown_group_key = "unknown / started_at-missing-or-invalid"
    grouped_sessions: dict[str, list[dict[str, Any]]] = {}
    for audio_session in sessions:
        requested_asset_key = audio_session.get("target_asset_key")
        target_asset_key = (
            requested_asset_key
            if isinstance(requested_asset_key, str) and requested_asset_key in registered_asset_keys
            else None
        )
        projected_session = {
            **audio_session,
            "target_asset_key": target_asset_key,
            "connector_import": {
                "enabled": target_asset_key is not None,
                "blocked_reason": (
                    None if target_asset_key is not None else connector_blocked_reason
                ),
            },
        }
        started_at = audio_session.get("started_at")
        group_key = unknown_group_key
        if isinstance(started_at, str):
            try:
                started_hour = datetime.fromisoformat(started_at.replace("Z", "+00:00")).replace(
                    minute=0, second=0, microsecond=0
                )
            except ValueError:
                pass
            else:
                group_key = (
                    f"{started_hour:%Y-%m-%d} / {started_hour:%H}:00-"
                    f"{(started_hour.hour + 1) % 24:02d}:00"
                )
        grouped_sessions.setdefault(group_key, []).append(projected_session)

    aggregation_groups = []
    for group_key in sorted(
        grouped_sessions,
        key=lambda value: (value == unknown_group_key, value),
    ):
        children = grouped_sessions[group_key]
        child_statuses = {
            status
            for child in children
            if isinstance((status := child.get("status")), str) and status.strip()
        }
        group_status = (
            next(iter(child_statuses))
            if len(child_statuses) == 1
            else "mixed"
            if child_statuses
            else "unknown"
        )
        aggregation_groups.append(
            {
                "group_key": group_key,
                "count": len(children),
                "status": group_status,
                "children": children,
            }
        )
    return collection_envelope(
        aggregation_groups,
        ctx,
    )


@router.get("/audio-sessions")
def get_audio_sessions(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "audio_sessions", page, status=status)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.get(
    "/audio-sessions/{id}/recording",
    response_class=Response,
    responses=_AUDIO_PLAYBACK_GET_RESPONSES,
)
def get_audio_sessions_by_id_recording(
    id: str,
    request: Request,
    session: SessionDep,
    grant: str | None = None,
    range_header: str | None = _AUDIO_RANGE_HEADER,
    if_range: str | None = _AUDIO_IF_RANGE_HEADER,
):
    return _stream_audio_with_playback_grant(
        grant,
        request,
        session,
        expected_audio_session_id=id,
        range_header=range_header,
        if_range=if_range,
    )


@router.head(
    "/audio-sessions/{id}/recording",
    response_class=Response,
    responses=_AUDIO_PLAYBACK_HEAD_RESPONSES,
)
def head_audio_sessions_by_id_recording(
    id: str,
    request: Request,
    session: SessionDep,
    grant: str | None = None,
    range_header: str | None = _AUDIO_RANGE_HEADER,
    if_range: str | None = _AUDIO_IF_RANGE_HEADER,
):
    return _stream_audio_with_playback_grant(
        grant,
        request,
        session,
        expected_audio_session_id=id,
        head_only=True,
        range_header=range_header,
        if_range=if_range,
    )


@router.post("/audio-sessions/{id}/playback-grants", status_code=201)
async def post_audio_sessions_by_id_playback_grants(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    require_any_role(
        ctx,
        AUDIO_PLAYBACK_READ_ROLES,
        action="audio_recordings.create_playback_grant",
    )
    recording = _recording_for_session(
        session,
        ctx,
        id,
        include_internal_storage_version=True,
    )
    raw_storage = recording.get("storage_object")
    storage = raw_storage if isinstance(raw_storage, dict) else {}
    object_version_id = storage.get("object_version_id")
    if _real_object_storage_enabled():
        if (
            not storage.get("storage_object_id")
            or not storage.get("provider")
            or not storage.get("etag")
        ):
            raise ApiError(
                "AUDIO_OBJECT_VERSION_ID_UNAVAILABLE",
                "录音对象尚未完成精确版本登记，无法签发播放授权",
                409,
                retryable=False,
            )
        object_version_id = _exact_object_version_id(object_version_id)
    body_hash = await request_hash(request)
    operation = f"audio_recordings.create_playback_grant:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    token, grant = create_audio_playback_grant(
        settings,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        user_id=ctx.user_id,
        audio_session_id=id,
        auth_session_id=ctx.auth_session_id,
        storage_object_id=str(storage.get("storage_object_id") or "") or None,
        storage_provider=str(storage.get("provider") or "") or None,
        object_version_id=(
            object_version_id if isinstance(object_version_id, str) and object_version_id else None
        ),
        etag=str(storage.get("etag") or "") or None,
    )
    response_data = {
        "audio_session_id": id,
        "storage_object_id": grant.storage_object_id,
        "playback_url": f"{settings.api_prefix}/audio-playback?grant={token}",
        "expires_at": datetime.fromtimestamp(grant.expires_at, UTC).isoformat(),
        "expires_in_seconds": max(grant.expires_at - int(datetime.now(UTC).timestamp()), 0),
        "status": "active",
        "trace_id": ctx.trace_id,
    }
    record_audit(
        session,
        ctx,
        action="audio_recordings.create_playback_grant",
        object_type="audio_session",
        object_id=id,
        after={
            "audio_session_id": id,
            "storage_object_id": grant.storage_object_id,
            "expires_at": response_data["expires_at"],
            "grant_nonce": grant.nonce,
        },
    )
    response = envelope(response_data, ctx)
    save_idempotency_result(
        session,
        ctx,
        operation=operation,
        body_hash=body_hash,
        status_code=201,
        response_json=response,
    )
    session.commit()
    return response


def _stream_audio_with_playback_grant(
    token: str | None,
    request: Request,
    session: SessionDep,
    *,
    expected_audio_session_id: str | None = None,
    head_only: bool = False,
    range_header: str | None = None,
    if_range: str | None = None,
) -> Response:
    playback_grant, ctx = authorize_audio_playback_grant(
        session,
        settings,
        token,
        request_id=getattr(request.state, "request_id", "audio-playback"),
        trace_id=getattr(request.state, "trace_id", "trace_audio_playback"),
        expected_audio_session_id=expected_audio_session_id,
    )
    recording = _recording_for_session(
        session,
        ctx,
        playback_grant.audio_session_id,
        include_internal_storage_version=True,
    )
    raw_storage = recording.get("storage_object")
    storage = raw_storage if isinstance(raw_storage, dict) else {}
    if playback_grant.storage_object_id and (
        storage.get("storage_object_id") != playback_grant.storage_object_id
        or storage.get("provider") != playback_grant.storage_provider
        or str(storage.get("object_version_id") or "")
        != str(playback_grant.object_version_id or "")
        or str(storage.get("etag") or "") != str(playback_grant.etag or "")
    ):
        raise ApiError(
            "AUDIO_PLAYBACK_GRANT_STALE",
            "录音对象已更新，请重新获取播放授权",
            409,
            retryable=True,
        )
    range_header = _effective_range_header(range_header, if_range, recording)
    real_object_storage = _real_object_storage_enabled()
    if real_object_storage:
        if (
            not playback_grant.storage_object_id
            or not playback_grant.storage_provider
            or not playback_grant.etag
            or not playback_grant.object_version_id
        ):
            raise ApiError(
                "AUDIO_PLAYBACK_GRANT_STALE",
                "录音播放授权未绑定精确对象版本，请重新获取播放授权",
                409,
                retryable=True,
            )
        response = _object_storage_audio_response(
            recording,
            range_header=range_header,
            head_only=head_only,
        )
    else:
        file_name = str(
            recording.get("file_name")
            or f"{recording.get('recording_id') or playback_grant.audio_session_id}.wav"
        )
        if head_only:
            response = _audio_head_response(
                SYNTHETIC_WAV_HEADER_SIZE + SYNTHETIC_WAV_DATA_SIZE,
                range_header=range_header,
                content_type="audio/wav",
                file_name=file_name,
                source="mock-range-stream",
                extra_headers=_storage_response_headers(recording),
            )
        else:
            response = _audio_response(
                _synthetic_wav_bytes(
                    str(recording.get("recording_id") or playback_grant.audio_session_id),
                    file_name,
                ),
                range_header=range_header,
                content_type="audio/wav",
                file_name=file_name,
                source="mock-range-stream",
                extra_headers=_storage_response_headers(recording),
            )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    record_audit(
        session,
        ctx,
        action="audio_recordings.stream",
        object_type="audio_session",
        object_id=playback_grant.audio_session_id,
        after={
            "storage_object_id": recording.get("storage_object_id"),
            "range": range_header,
            "status_code": response.status_code,
            "grant_nonce": playback_grant.nonce,
        },
    )
    session.commit()
    return response


@router.get(
    "/audio-playback",
    response_class=Response,
    responses=_AUDIO_PLAYBACK_GET_RESPONSES,
)
def get_audio_playback(
    grant: str,
    request: Request,
    session: SessionDep,
    range_header: str | None = _AUDIO_RANGE_HEADER,
    if_range: str | None = _AUDIO_IF_RANGE_HEADER,
):
    return _stream_audio_with_playback_grant(
        grant,
        request,
        session,
        range_header=range_header,
        if_range=if_range,
    )


@router.head(
    "/audio-playback",
    response_class=Response,
    responses=_AUDIO_PLAYBACK_HEAD_RESPONSES,
)
def head_audio_playback(
    grant: str,
    request: Request,
    session: SessionDep,
    range_header: str | None = _AUDIO_RANGE_HEADER,
    if_range: str | None = _AUDIO_IF_RANGE_HEADER,
):
    return _stream_audio_with_playback_grant(
        grant,
        request,
        session,
        head_only=True,
        range_header=range_header,
        if_range=if_range,
    )


@router.put("/audio-sessions/{id}/recording-object")
async def put_audio_sessions_by_id_recording_object(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    require_any_role(
        ctx,
        ("project_admin", "asset_manager"),
        action="audio_recordings.register_object",
    )
    session_resource = get_resource(session, ctx, "audio_sessions", id)
    recording_id = session_resource.data.get("recording_id")
    if not isinstance(recording_id, str) or not recording_id:
        raise ApiError("RECORDING_NOT_FOUND", "当前会话没有可登记的录音对象", 404)

    body_hash = await request_hash(request)
    operation = f"audio_recordings.register_object:{id}"
    replay = replay_or_conflict(session, ctx, operation=operation, body_hash=body_hash)
    if replay is not None:
        return replay
    body = parse_payload(AudioRecordingObjectRequest, await request.json())
    storage_data = body.model_dump()
    expected_prefix = f"tenants/{ctx.tenant_id}/projects/{ctx.project_id}/"
    if not body.object_key.startswith(expected_prefix):
        raise ApiError(
            "AUDIO_OBJECT_SCOPE_MISMATCH",
            "录音对象路径必须位于当前租户和项目命名空间",
            409,
        )

    storage_verification: dict[str, Any] = {"mode": "declared", "verified": False}
    verified_etag = body.etag.strip('"') if body.etag else None
    object_version_id: str | None = None
    object_status = "registered"
    if _real_object_storage_enabled():
        try:
            provider_client = object_storage_client_for_provider(body.provider)
            if not provider_client.allows_bucket(body.bucket):
                raise ApiError(
                    "AUDIO_STORAGE_BUCKET_NOT_ALLOWED",
                    "录音对象所在 bucket 不在当前 Provider 允许列表中",
                    403,
                    retryable=False,
                )
            remote = provider_client.head_object(body.bucket, body.object_key)
        except HTTPError as exc:
            if exc.code == 404:
                raise ApiError("AUDIO_OBJECT_NOT_FOUND", "对象存储中未找到待登记录音", 404) from exc
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "无法验证对象存储中的录音元数据",
                502,
                retryable=True,
            ) from exc
        except ValueError as exc:
            raise ApiError(
                "AUDIO_STORAGE_PROVIDER_NOT_CONFIGURED",
                "当前录音的对象存储 Provider 未正确配置",
                503,
                retryable=False,
            ) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "无法验证对象存储中的录音元数据",
                502,
                retryable=True,
            ) from exc
        remote_size = int(remote.get("content_length") or 0)
        remote_etag = _strong_etag_opaque(remote.get("etag"))
        if remote_size != body.content_length:
            raise ApiError(
                "AUDIO_OBJECT_SIZE_MISMATCH",
                "登记的录音大小与对象存储元数据不一致",
                409,
            )
        supplied_etag = _strong_etag_opaque(body.etag) if body.etag else None
        if supplied_etag and remote_etag and supplied_etag != remote_etag:
            raise ApiError(
                "AUDIO_OBJECT_ETAG_MISMATCH",
                "登记的录音 ETag 与对象存储元数据不一致",
                409,
            )
        if not remote_etag:
            raise ApiError(
                "AUDIO_OBJECT_VERSION_UNAVAILABLE",
                "对象存储未返回可绑定的 ETag，无法安全登记录音版本",
                409,
                retryable=False,
            )
        raw_version_id = remote.get("version_id")
        if (
            not isinstance(raw_version_id, str)
            or not raw_version_id.strip()
            or raw_version_id.strip().casefold() == "null"
        ):
            raise ApiError(
                "AUDIO_OBJECT_VERSION_ID_UNAVAILABLE",
                "对象存储未返回精确版本 ID，无法安全登记录音版本",
                409,
                retryable=False,
            )
        object_version_id = _exact_object_version_id(raw_version_id.strip())
        wav_probe = _read_versioned_wav_probe(
            provider_client,
            body.bucket,
            body.object_key,
            content_length=remote_size,
            registered_etag=remote_etag,
            registered_version_id=object_version_id,
        )
        try:
            verification = verify_remote_audio_object(
                remote,
                declared_content_length=body.content_length,
                declared_sha256=body.checksum_sha256,
                wav_prefix=wav_probe,
                declared_content_type=body.content_type,
            )
        except ApiError as exc:
            if exc.code != "AUDIO_OBJECT_CHECKSUM_UNAVAILABLE":
                raise
            if len(wav_probe) == remote_size:
                verified_sha256 = hashlib.sha256(wav_probe).hexdigest()
                checksum_method = "versioned_range"
            else:
                verified_sha256 = _read_versioned_object_sha256(
                    provider_client,
                    body.bucket,
                    body.object_key,
                    content_length=remote_size,
                    content_type=body.content_type,
                    registered_etag=remote_etag,
                    registered_version_id=object_version_id,
                )
                checksum_method = "versioned_full_stream"
            verification = verify_remote_audio_object(
                remote,
                declared_content_length=body.content_length,
                declared_sha256=body.checksum_sha256,
                wav_prefix=wav_probe,
                declared_content_type=body.content_type,
                verified_sha256=verified_sha256,
                verified_checksum_method=checksum_method,
            )
        storage_verification = {
            **verification,
            "etag": remote_etag,
            "object_version_id": object_version_id,
        }
        verified_etag = remote_etag
        object_status = "verified"
    storage_data["etag"] = verified_etag

    existing = session.get(StorageObject, body.storage_object_id)
    _require_immutable_storage_object_registration(
        existing,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        source_type="audio_recording",
        source_id=recording_id,
        provider=body.provider,
        bucket=body.bucket,
        object_key=body.object_key,
        content_type=body.content_type,
        size_bytes=body.content_length,
        content_sha256=body.checksum_sha256,
        etag=verified_etag,
        object_version_id=object_version_id,
    )
    object_key_sha256 = hashlib.sha256(body.object_key.encode("utf-8")).hexdigest()
    locator = session.scalar(
        select(StorageObject).where(
            StorageObject.tenant_id == ctx.tenant_id,
            StorageObject.project_id == ctx.project_id,
            StorageObject.provider == body.provider,
            StorageObject.bucket == body.bucket,
            StorageObject.object_key_sha256 == object_key_sha256,
        )
    )
    if locator and locator.storage_object_id != body.storage_object_id:
        raise ApiError("STORAGE_OBJECT_LOCATOR_CONFLICT", "对象路径已登记到其他对象标识", 409)

    previous_source = _storage_object_for_recording(session, ctx, recording_id)
    if previous_source and previous_source.storage_object_id != body.storage_object_id:
        previous_source.status = "superseded"
        previous_source.payload = {**previous_source.payload, "status": "superseded"}

    before = _storage_object_data(existing) if existing else None
    object_payload = {
        **storage_data,
        **({"object_version_id": object_version_id} if object_version_id is not None else {}),
        "source_type": "audio_recording",
        "source_id": recording_id,
        "status": object_status,
        "trace_id": ctx.trace_id,
        "verification": storage_verification,
    }
    public_storage_verification = {
        key: value for key, value in storage_verification.items() if key != "object_version_id"
    }
    public_object_payload = {
        key: value for key, value in object_payload.items() if key != "object_version_id"
    }
    public_object_payload["verification"] = public_storage_verification
    if existing:
        target = existing
        target.provider = body.provider
        target.bucket = body.bucket
        target.object_key = body.object_key
        target.object_key_sha256 = object_key_sha256
        target.source_type = "audio_recording"
        target.source_id = recording_id
        target.content_type = body.content_type
        target.size_bytes = body.content_length
        target.content_sha256 = body.checksum_sha256
        target.etag = verified_etag
        target.status = object_status
        target.trace_id = ctx.trace_id
        target.payload = object_payload
    else:
        target = StorageObject(
            storage_object_id=body.storage_object_id,
            tenant_id=ctx.tenant_id,
            project_id=ctx.project_id,
            provider=body.provider,
            bucket=body.bucket,
            object_key=body.object_key,
            object_key_sha256=object_key_sha256,
            source_type="audio_recording",
            source_id=recording_id,
            content_type=body.content_type,
            size_bytes=body.content_length,
            content_sha256=body.checksum_sha256,
            etag=verified_etag,
            status=object_status,
            trace_id=ctx.trace_id,
            payload=object_payload,
        )
        session.add(target)

    strong_recording = session.get(AudioRecording, recording_id)
    if strong_recording and (
        strong_recording.tenant_id != ctx.tenant_id or strong_recording.project_id != ctx.project_id
    ):
        raise ApiError("RECORDING_SCOPE_CONFLICT", "录音标识已被其他作用域占用", 409)
    existing_projection: dict[str, Any] = next(
        (
            item
            for item in list_resource_data(session, ctx, "recordings")
            if item.get("recording_id") == recording_id
        ),
        {},
    )
    recording_payload = {
        **existing_projection,
        **(strong_recording.payload if strong_recording else {}),
        "recording_id": recording_id,
        "storage_object_id": body.storage_object_id,
        "storage_object": public_object_payload,
        "trace_id": ctx.trace_id,
    }
    if strong_recording:
        strong_recording.status = object_status
        strong_recording.trace_id = ctx.trace_id
        strong_recording.payload = recording_payload
    else:
        session.add(
            AudioRecording(
                recording_id=recording_id,
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                status=object_status,
                trace_id=ctx.trace_id,
                payload=recording_payload,
            )
        )
    upsert_resource(
        session,
        ctx,
        "recordings",
        recording_id,
        recording_payload,
        status=object_status,
        trace_id=ctx.trace_id,
    )
    response_data = {
        "audio_session_id": id,
        "recording_id": recording_id,
        "status": object_status,
        "storage_object": public_object_payload,
        "affected_objects": [
            {"type": "audio_session", "id": id},
            {"type": "audio_recording", "id": recording_id},
            {"type": "storage_object", "id": body.storage_object_id},
        ],
        "next_actions": [
            {
                "key": "stream_recording",
                "label": "调听录音",
                "route": f"audio-sessions/{id}/recording",
            }
        ],
        "trace_id": ctx.trace_id,
    }
    record_audit(
        session,
        ctx,
        action="audio_recordings.register_object",
        object_type="storage_object",
        object_id=body.storage_object_id,
        before=before,
        after=response_data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="audio_recording.object_registered",
        aggregate_type="storage_object",
        aggregate_id=body.storage_object_id,
        payload=response_data,
    )
    response = envelope(response_data, ctx)
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


@router.get("/audio-sessions/{id}")
def get_audio_sessions_by_id(id: str, session: SessionDep, ctx: ContextDep):
    session_resource = get_resource(session, ctx, "audio_sessions", id)
    data = dict(session_resource.data)
    related = {
        "recording": _recording_for_session(session, ctx, id),
        "boundaries": [
            item
            for item in list_resource_data(session, ctx, "conversation_boundaries")
            if item.get("audio_session_id") == id
        ],
        "evidence_packs": [
            item
            for item in list_resource_data(session, ctx, "evidence_packs")
            if item.get("audio_session_id") == id
        ],
        "asr_segments": _runtime_audio_items(session, ctx, "asr_segments", id),
        "vad_segments": _runtime_audio_items(session, ctx, "vad_segments", id),
        "speaker_turns": _runtime_audio_items(session, ctx, "speaker_turns", id),
        "voiceprint_samples": _runtime_audio_items(session, ctx, "voiceprint_samples", id),
        "audio_quality_reports": _runtime_audio_items(session, ctx, "audio_quality_reports", id),
        "event_links": [
            item
            for item in list_resource_data(session, ctx, "event_links")
            if item.get("audio_session_id") == id
        ],
        "listening_annotations": [
            item
            for item in list_resource_data(session, ctx, "listening_annotations", limit=200)
            if item.get("audio_session_id") == id
        ],
    }
    data.update(related)
    return envelope(data, ctx)


@router.get("/audio-sessions/{id}/annotations")
def get_audio_sessions_by_id_annotations(id: str, session: SessionDep, ctx: ContextDep):
    get_resource(session, ctx, "audio_sessions", id)
    items = [
        item
        for item in list_resource_data(session, ctx, "listening_annotations", limit=200)
        if item.get("audio_session_id") == id
    ]
    return collection_envelope(
        sorted(items, key=lambda item: str(item.get("id") or "")),
        ctx,
        total=len(items),
        limit=len(items),
        next_cursor=None,
        meta={"status_counts": status_counts(items)},
    )


@router.post("/audio-sessions/{id}/annotations", status_code=201)
async def post_audio_sessions_by_id_annotations(
    id: str,
    request: Request,
    response: Response,
    session: SessionDep,
    ctx: ContextDep,
):
    audio_session = get_resource(session, ctx, "audio_sessions", id)
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("VALIDATION_ERROR", "标注请求必须是 JSON object", 422)
    annotation_id = body.get("id") or body.get("annotation_id")
    if not isinstance(annotation_id, str) or not annotation_id.strip():
        raise ApiError("ANNOTATION_ID_REQUIRED", "标注草稿必须包含 annotation_id", 400)
    if body.get("audio_session_id") not in (None, id):
        raise ApiError("AUDIO_SESSION_MISMATCH", "标注草稿与当前音频会话不一致", 409)

    if body.get("annotation_kind") == "label-fact-draft":
        require_any_role(
            ctx,
            MANUAL_LABEL_DRAFT_WRITE_ROLES,
            action="manual_label_drafts.create",
        )
        manual_body = {key: value for key, value in body.items() if key != "audio_session_id"}
        command = parse_payload(ManualLabelDraftCreateRequest, manual_body)
        body_hash, replay = await _begin_manual_label_http_operation(
            request,
            session,
            ctx,
            operation=_MANUAL_LABEL_CREATE_HTTP_OPERATION,
        )
        if replay is not None:
            return replay
        data = create_manual_label_draft(
            session,
            ctx,
            audio_session_id=id,
            request=command,
        )
        return _complete_manual_label_http_operation(
            session,
            ctx,
            operation=_MANUAL_LABEL_CREATE_HTTP_OPERATION,
            body_hash=body_hash,
            status_code=201,
            data=data,
        )

    if body.get("annotation_kind") == "asr-transcript-correction":
        require_any_role(
            ctx,
            ASR_CORRECTION_WRITE_ROLES,
            action="asr_annotation.corrections.submit",
        )
        correction_body = parse_payload(AsrTranscriptCorrectionRequest, body)
        body_hash = await request_hash(request)
        operation = "asr_annotation.corrections.submit"
        replay = replay_or_conflict(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
        )
        if replay is not None:
            replay_data = replay.get("data")
            if isinstance(replay_data, dict) and replay_data.get("deduplicated") is True:
                response.status_code = 200
            return replay

        correction_data, deduplicated = record_asr_annotation_correction(
            session,
            ctx,
            audio_session_id=id,
            audio_session_data=dict(audio_session.data),
            body=correction_body,
        )
        # ListeningAnnotation/JsonResource remain latest read projections only. The
        # append-only correction table is authoritative and raw before/after text is
        # deliberately excluded from generic audit and Outbox payloads.
        projection_data = dict(correction_data)
        upsert_resource(
            session,
            ctx,
            "listening_annotations",
            str(correction_data["annotation_id"]),
            projection_data,
            status="submitted",
            trace_id=ctx.trace_id,
        )
        persist_listening_annotation_projection(
            session,
            ctx,
            annotation_id=str(correction_data["annotation_id"]),
            audio_session_id=id,
            status="submitted",
            payload=projection_data,
        )
        status_code = 200 if deduplicated else 201
        response.status_code = status_code
        result = envelope(correction_data, ctx)
        save_idempotency_result(
            session,
            ctx,
            operation=operation,
            body_hash=body_hash,
            status_code=status_code,
            response_json=result,
        )
        session.commit()
        return result

    immutable_correction = session.scalar(
        select(AsrAnnotationCorrection.correction_id).where(
            AsrAnnotationCorrection.tenant_id == ctx.tenant_id,
            AsrAnnotationCorrection.project_id == ctx.project_id,
            AsrAnnotationCorrection.annotation_id == annotation_id.strip(),
        )
    )
    if immutable_correction is not None:
        raise ApiError(
            "ASR_CORRECTION_IMMUTABLE",
            "已提交的 ASR 标注修正不可通过普通标签接口覆盖",
            409,
        )
    manual_draft = session.scalar(
        select(ListeningAnnotation).where(
            ListeningAnnotation.tenant_id == ctx.tenant_id,
            ListeningAnnotation.project_id == ctx.project_id,
            ListeningAnnotation.audio_session_id == id,
            ListeningAnnotation.annotation_id == annotation_id.strip(),
        )
    )
    if manual_draft is not None and is_manual_label_draft_projection(manual_draft):
        raise ApiError(
            "MANUAL_LABEL_DRAFT_IMMUTABLE",
            "人工标签 draft 不可通过通用标注接口覆盖；请提交或显式 rebase",
            409,
        )
    return await upsert_idempotent_json_resource(
        session,
        ctx,
        request,
        "listening_annotations",
        annotation_id.strip(),
        status="draft",
        operation="listening_annotations.upsert",
        status_code=201,
        extra_data={
            "audio_session_id": id,
            "affected_objects": [
                {"type": "audio_session", "id": id},
                {"type": "listening_annotation", "id": annotation_id.strip()},
            ],
            "next_actions": [
                {
                    "key": "review_track",
                    "label": "回到标签轨道",
                    "route": f"audio-sessions/{id}/annotations",
                }
            ],
        },
        after_upsert=lambda data: persist_listening_annotation_projection(
            session,
            ctx,
            annotation_id=annotation_id.strip(),
            audio_session_id=id,
            status=str(data.get("status") or "draft"),
            payload=data,
        ),
    )


@router.post(
    "/audio-sessions/{id}/annotations/{annotation_id}/submissions",
    status_code=201,
)
async def post_manual_label_draft_submission(
    id: str,
    annotation_id: str,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    get_resource(session, ctx, "audio_sessions", id)
    require_any_role(
        ctx,
        MANUAL_LABEL_DRAFT_WRITE_ROLES,
        action="manual_label_drafts.submit",
    )
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise ApiError("VALIDATION_ERROR", "人工标签提交请求必须是 JSON object", 422)
    command = parse_payload(ManualLabelDraftSubmitRequest, raw_body)
    body_hash, replay = await _begin_manual_label_http_operation(
        request,
        session,
        ctx,
        operation=_MANUAL_LABEL_SUBMIT_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = submit_manual_label_draft(
        session,
        ctx,
        audio_session_id=id,
        annotation_id=annotation_id,
        request=command,
    )
    return _complete_manual_label_http_operation(
        session,
        ctx,
        operation=_MANUAL_LABEL_SUBMIT_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=201,
        data=data,
    )


@router.post("/audio-sessions/{id}/annotations/{annotation_id}/rebases")
async def post_manual_label_draft_rebase(
    id: str,
    annotation_id: str,
    request: Request,
    response: Response,
    session: SessionDep,
    ctx: ContextDep,
):
    get_resource(session, ctx, "audio_sessions", id)
    require_any_role(
        ctx,
        MANUAL_LABEL_DRAFT_WRITE_ROLES,
        action="manual_label_drafts.rebase",
    )
    raw_body = await request.json()
    if not isinstance(raw_body, dict):
        raise ApiError("VALIDATION_ERROR", "人工标签 rebase 请求必须是 JSON object", 422)
    command = parse_payload(ManualLabelDraftRebaseRequest, raw_body)
    status_code = 200 if command.action == "preview" else 201
    response.status_code = status_code
    body_hash, replay = await _begin_manual_label_http_operation(
        request,
        session,
        ctx,
        operation=_MANUAL_LABEL_REBASE_HTTP_OPERATION,
    )
    if replay is not None:
        return replay
    data = rebase_manual_label_draft(
        session,
        ctx,
        audio_session_id=id,
        annotation_id=annotation_id,
        request=command,
    )
    return _complete_manual_label_http_operation(
        session,
        ctx,
        operation=_MANUAL_LABEL_REBASE_HTTP_OPERATION,
        body_hash=body_hash,
        status_code=status_code,
        data=data,
    )


@router.post(
    "/audio-sessions/{id}/intelligence-runs",
    status_code=202,
    response_model=PublicRunEnvelope[PublicRunDetail],
)
async def post_audio_sessions_by_id_intelligence_runs(
    id: str,
    body: AudioIntelligenceRunRequest,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
):
    session_data = get_resource(session, ctx, "audio_sessions", id).data
    session_recording_id = session_data.get("recording_id")
    if body.recording_id is not None and body.recording_id != session_recording_id:
        raise ApiError(
            "AUDIO_SESSION_RECORDING_MISMATCH",
            "音频智能任务的录音必须与路径中的音频会话绑定一致",
            409,
            retryable=False,
        )
    body_data = body.model_dump(exclude_none=True)
    task_binding = resolve_audio_hotword_task_binding(
        session,
        ctx,
        execution_mode=body.execution_mode,
        task_version_id=body.task_version_id,
        hotword_pack_version_id=body.hotword_pack_version_id,
        provider=body.provider,
        provider_explicit="provider" in body.model_fields_set,
        model_version=body.model_version,
        model_version_explicit="model_version" in body.model_fields_set,
        language=body.language,
    )
    effective_hotword_version_id = (
        str(task_binding.get("hotword_pack_version_id") or "")
        if task_binding is not None
        else body.hotword_pack_version_id
    )
    if not effective_hotword_version_id:
        effective_hotword_version_id = None
    if effective_hotword_version_id and "asr" not in body.capabilities:
        raise ApiError(
            "HOTWORD_ASR_CAPABILITY_REQUIRED",
            "绑定热词包版本时必须请求 ASR 能力",
            422,
        )
    effective_provider = (
        str(task_binding["provider"]) if task_binding is not None else body.provider
    )
    effective_language = (
        str(task_binding["language"]) if task_binding is not None else body.language
    )
    effective_model_version = (
        str(task_binding["model_version"]) if task_binding is not None else body.model_version
    )
    hotword_provider = validate_hotword_execution(
        session,
        ctx,
        version_id=effective_hotword_version_id,
        execution_mode=body.execution_mode,
        provider=effective_provider,
        language=effective_language,
    )
    body_data["language"] = effective_language
    body_data["model_version"] = effective_model_version
    if effective_hotword_version_id:
        body_data["hotword_pack_version_id"] = effective_hotword_version_id
    if task_binding is not None:
        body_data["task_version_id"] = task_binding["task_version_id"]
        body_data["task_version_snapshot"] = task_binding["task_version_snapshot"]
    final_provider = hotword_provider or effective_provider
    body_data["provider"] = final_provider
    _require_server_audio_inference_policy(
        provider=final_provider,
        model=effective_model_version,
    )
    recording_id = session_recording_id
    storage_object = _storage_object_for_recording(session, ctx, str(recording_id or ""))
    input_object: dict[str, Any] | None = None
    if storage_object is not None:
        storage_payload = storage_object.payload if isinstance(storage_object.payload, dict) else {}
        object_version_id = storage_payload.get("object_version_id")
        content_sha256 = str(storage_object.content_sha256 or "").strip().lower()
        if (
            storage_object.status in {"verified", "active"}
            and isinstance(object_version_id, str)
            and object_version_id.strip()
            and object_version_id.strip().casefold() != "null"
            and len(content_sha256) == 64
            and all(character in "0123456789abcdef" for character in content_sha256)
            and isinstance(storage_object.size_bytes, int)
            and 44 <= storage_object.size_bytes <= 5 * 1024**3
        ):
            input_object = {
                "storage_object_id": storage_object.storage_object_id,
                "storage_provider": storage_object.provider,
                "bucket": storage_object.bucket,
                "object_key": storage_object.object_key,
                "version_id": object_version_id.strip(),
                "content_sha256": content_sha256,
                "content_length": storage_object.size_bytes,
                "content_type": storage_object.content_type,
            }
    if settings.auris_dagster_adapter.strip().lower() == "real" and input_object is None:
        raise ApiError(
            "AUDIO_EXECUTION_INPUT_VERSION_REQUIRED",
            "真实音频执行要求已验证且绑定精确版本 ID 的录音对象",
            409,
            retryable=False,
        )
    capabilities = list(dict.fromkeys(body.capabilities))
    output_assets = audio_intelligence_output_assets(capabilities)
    non_production = body.execution_mode in {"shadow", "diagnostic"}
    root_trace_id = (
        str(task_binding["root_trace_id"])
        if task_binding is not None
        else get_hotword_version(session, ctx, effective_hotword_version_id).root_trace_id
        if effective_hotword_version_id
        else ctx.trace_id
    )
    payload = {
        **body_data,
        "audio_session_id": id,
        "recording_id": recording_id,
        "capabilities": capabilities,
        "output_assets": output_assets,
        "execution_contract": "auris-flow-audio-intelligence-v1",
        "execution_deadline_at": (
            datetime.now(UTC) + timedelta(seconds=settings.task_run_default_deadline_seconds)
        ).isoformat(),
        **({"input_object": input_object} if input_object is not None else {}),
        "job_name": "audio_intelligence_pipeline",
        "root_trace_id": root_trace_id,
        "external_outputs_enabled": (
            task_binding["external_outputs_enabled"]
            if task_binding is not None
            else not non_production
        ),
        "writeback_mode": (
            task_binding["writeback_mode"]
            if task_binding is not None
            else "disabled"
            if non_production
            else "configured"
        ),
        "callback_mode": (
            task_binding["callback_mode"]
            if task_binding is not None
            else "disabled"
            if non_production
            else "configured"
        ),
        "run_key": (
            f"audio-intelligence:{id}:{effective_model_version}:"
            f"{body_data.get('task_version_id') or body.execution_mode}"
        ),
        "partition_key": body_data.get("partition_key")
        or f"{ctx.tenant_id}/{ctx.project_id}/{recording_id or id}",
        "affected_objects": [
            {"type": "audio_session", "id": id},
            *(
                [{"type": "recording", "id": recording_id}]
                if isinstance(recording_id, str) and recording_id
                else []
            ),
            *(
                [
                    {
                        "type": "hotword_pack_version",
                        "id": effective_hotword_version_id,
                    }
                ]
                if effective_hotword_version_id
                else []
            ),
            *(
                [
                    {
                        "type": "task_version",
                        "id": task_binding["task_version_id"],
                    }
                ]
                if task_binding is not None
                else []
            ),
            *[
                {
                    "type": "data_asset",
                    "id": asset["asset_key"],
                    "capability": asset["capability"],
                }
                for asset in output_assets
            ],
        ],
        "next_actions": [
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{root_trace_id}"},
            {"key": "view_audio_session", "label": "回到调听会话", "route": f"audio-sessions/{id}"},
        ],
    }
    return await create_run(
        session,
        ctx,
        request,
        run_type="audio_intelligence",
        event_type="audio_intelligence.requested",
        payload=payload,
        status="pending",
    )


@router.get("/voiceprints")
def get_voiceprints(session: SessionDep, ctx: ContextDep):
    require_voiceprint_sensitive_read(ctx)
    recordings = list_resource_data(session, ctx, "recordings")
    enrollments = list_resource_data(session, ctx, "voiceprint_enrollments", limit=200)
    enrollment_by_voiceprint = {
        item.get("voiceprint_id"): item
        for item in enrollments
        if isinstance(item.get("voiceprint_id"), str)
    }
    items = [
        {
            "voiceprint_id": f"vp_{recording['recording_id']}",
            "recording_id": recording["recording_id"],
            "file_name": recording["file_name"],
            "status": "enrollable" if index == 0 else "review",
            "quality_score": 88 if index == 0 else 72,
            "enrollment": enrollment_by_voiceprint.get(f"vp_{recording['recording_id']}"),
            "trace_id": ctx.trace_id,
        }
        for index, recording in enumerate(recordings)
    ]
    known_voiceprints = {item["voiceprint_id"] for item in items}
    for enrollment in enrollments:
        voiceprint_id = enrollment.get("voiceprint_id")
        if not isinstance(voiceprint_id, str) or voiceprint_id in known_voiceprints:
            continue
        items.append(
            {
                "voiceprint_id": voiceprint_id,
                "recording_id": enrollment.get("recording_id"),
                "file_name": enrollment.get("wav_file"),
                "status": enrollment.get("status", "pending_review"),
                "quality_score": enrollment.get("quality", {}).get("overall")
                if isinstance(enrollment.get("quality"), dict)
                else None,
                "enrollment": enrollment,
                "trace_id": enrollment.get("trace_id") or ctx.trace_id,
            }
        )
    return collection_envelope(items, ctx, meta={"status_counts": status_counts(items)})


@router.get("/voiceprint-enrollments")
def get_voiceprint_enrollments(
    session: SessionDep,
    ctx: ContextDep,
    page: PaginationDep,
    voiceprint_id: str | None = None,
    status: str | None = None,
):
    resource_page = list_resource_page(session, ctx, "voiceprint_enrollments", page, status=status)
    items = [
        item
        for item in resource_page.items
        if voiceprint_id is None or item.get("voiceprint_id") == voiceprint_id
    ]
    return collection_envelope(
        items,
        ctx,
        total=len(items),
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(items)},
    )


@router.get("/voiceprint-enrollments/{id}")
def get_voiceprint_enrollments_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "voiceprint_enrollments", id).data, ctx)


@router.post("/voiceprint-enrollments", status_code=201)
async def post_voiceprint_enrollments(request: Request, session: SessionDep, ctx: ContextDep):
    body = await request.json()
    voiceprint_id = body.get("voiceprint_id")
    if not isinstance(voiceprint_id, str) or not voiceprint_id.strip():
        raise ApiError("VOICEPRINT_ID_REQUIRED", "声纹入库必须包含 voiceprint_id", 400)
    enrollment_id = body.get("id") or body.get("enrollment_id")
    if not isinstance(enrollment_id, str) or not enrollment_id.strip():
        enrollment_id = f"{voiceprint_id.strip()}_enrollment"
    gate = _voiceprint_quality_gate(body)
    status = _voiceprint_enrollment_status(body, ctx)
    embedding_ref = body.get("embedding_ref")
    if not isinstance(embedding_ref, dict):
        embedding_ref = {}
    requested_vector_dim = embedding_ref.get("vector_dim")
    vector_dim = (
        requested_vector_dim
        if isinstance(requested_vector_dim, int)
        and not isinstance(requested_vector_dim, bool)
        and 1 <= requested_vector_dim <= 8192
        else 512
    )
    requested_active_dims = embedding_ref.get("active_dims")
    embedding_ref = {
        "collection": "voiceprint_embeddings",
        "vector_dim": vector_dim,
        **(
            {"active_dims": requested_active_dims}
            if isinstance(requested_active_dims, int)
            and not isinstance(requested_active_dims, bool)
            and 0 <= requested_active_dims <= vector_dim
            else {}
        ),
        "status": (
            "dedicated_vector_provider_required" if status == "enrolled" else "reference_only"
        ),
        "indexing_enabled": False,
        "blocked_reason": "dedicated_voiceprint_vector_provider_required",
    }
    extra_data = {
        "voiceprint_id": voiceprint_id.strip(),
        "enrollment_id": enrollment_id.strip(),
        "quality_gate": {
            **gate,
            "required_role": "review_arbitrator",
            "reviewer_role_present": "review_arbitrator" in ctx.roles,
        },
        "confirm_state": "confirmed"
        if status == "enrolled"
        else "pending_review"
        if status == "pending_review"
        else "blocked",
        "embedding_ref": embedding_ref,
        "qdrant_payload": _voiceprint_qdrant_payload(
            ctx,
            enrollment_id=enrollment_id.strip(),
            voiceprint_id=voiceprint_id.strip(),
            body=body,
            gate=gate,
            embedding_ref=embedding_ref,
        ),
        "affected_objects": _voiceprint_enrollment_objects(voiceprint_id.strip(), body),
        "next_actions": [
            {
                "key": "human_review",
                "label": "进入声纹人工复核",
                "route": "human-review-tasks?queue=voiceprint_enrollment",
            }
            if status == "pending_review"
            else {
                "key": "quality_repair",
                "label": "补采或修正样本",
                "route": "data?tab=people",
            }
            if status == "blocked"
            else {
                "key": "view_voiceprint",
                "label": "查看声纹基线",
                "route": f"voiceprints/{voiceprint_id.strip()}",
            },
            {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
        ],
    }
    return await upsert_idempotent_json_resource(
        session,
        ctx,
        request,
        "voiceprint_enrollments",
        enrollment_id.strip(),
        status=status,
        operation="voiceprint_enrollments.upsert",
        status_code=201,
        extra_data=extra_data,
        after_upsert=lambda data: persist_voiceprint_enrollment_projection(
            session,
            ctx,
            enrollment_id=enrollment_id.strip(),
            voiceprint_id=voiceprint_id.strip(),
            status=str(data.get("status") or status),
            payload=data,
        ),
    )


@router.get("/event-links")
def get_event_links(
    session: SessionDep, ctx: ContextDep, page: PaginationDep, status: str | None = None
):
    resource_page = list_resource_page(session, ctx, "event_links", page, status=status)
    return collection_envelope(
        resource_page.items,
        ctx,
        total=resource_page.total,
        limit=resource_page.limit,
        next_cursor=resource_page.next_cursor,
        meta={"status_counts": status_counts(resource_page.items)},
    )


@router.post("/event-links", status_code=201)
async def post_event_links(request: Request, session: SessionDep, ctx: ContextDep):
    return await create_idempotent_json_resource(
        session,
        ctx,
        request,
        "event_links",
        key_prefix="event_link",
        status="pending",
    )


@router.get("/event-links/{id}")
def get_event_links_by_id(id: str, session: SessionDep, ctx: ContextDep):
    return envelope(get_resource(session, ctx, "event_links", id).data, ctx)


@router.patch("/event-links/{id}")
async def patch_event_links_by_id(id: str, request: Request, session: SessionDep, ctx: ContextDep):
    return await patch_idempotent_json_resource(session, ctx, request, "event_links", id)


@router.get("/data-aggregation-views")
def get_data_aggregation_views(ctx: ContextDep):
    return collection_envelope(
        [
            {
                "id": "view_audio_event_space_person",
                "priority": ["time", "event", "space", "person"],
                "status": "active",
                "trace_id": ctx.trace_id,
            }
        ],
        ctx,
    )


@router.patch("/data-aggregation-views/{id}")
async def patch_data_aggregation_views_by_id(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    return await upsert_idempotent_json_resource(
        session,
        ctx,
        request,
        "data_aggregation_views",
        id,
        status="success",
        operation="data_aggregation_views.patch",
    )


@router.patch("/conversation-boundaries/{id}")
async def patch_conversation_boundaries_by_id(
    id: str, request: Request, session: SessionDep, ctx: ContextDep
):
    patched = await patch_idempotent_json_resource(
        session,
        ctx,
        request,
        "conversation_boundaries",
        id,
        status="pending_sync",
    )
    patched_data = patched.get("data")
    boundary: dict[str, Any] = dict(patched_data) if isinstance(patched_data, dict) else {}
    audio_session_id = boundary.get("audio_session_id") or id
    start_ms = _numeric(boundary.get("start_ms"))
    end_ms = _numeric(boundary.get("end_ms"))
    sync_run = await create_run(
        session,
        ctx,
        request,
        run_type="boundary_sync",
        event_type="conversation_boundary.sync_requested",
        payload={
            "boundary_id": id,
            "audio_session_id": audio_session_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "decision": boundary.get("decision") or "manual_confirmed",
            "merged_slice_ids": boundary.get("merged_slice_ids") or [],
            "split_slice_ids": boundary.get("split_slice_ids") or [],
            "extension_ids": boundary.get("extension_ids") or [],
            "job_name": "conversation_boundary_sync_pipeline",
            "run_key": f"conversation-boundary:{id}:{int(start_ms)}:{int(end_ms)}",
            "partition_key": f"{ctx.tenant_id}/{ctx.project_id}/{audio_session_id}",
            "affected_objects": [
                {"type": "conversation_boundary", "id": id},
                {"type": "audio_session", "id": str(audio_session_id)},
                {"type": "data_asset", "id": "auris/audio/voice_segments"},
                {"type": "data_asset", "id": "auris/model/asr_transcripts"},
                {"type": "data_asset", "id": "auris/label/segment_annotations"},
            ],
            "next_actions": [
                {"key": "view_trace", "label": "查看 Trace", "route": f"traces/{ctx.trace_id}"},
                {
                    "key": "wait_boundary_sync",
                    "label": "等待下游时间索引和资产状态同步",
                    "route": f"audio-sessions/{audio_session_id}",
                },
            ],
        },
        status="pending",
        idempotency_operation=f"conversation_boundary_sync:{id}",
    )
    sync_run_data = sync_run.get("data")
    run_data: dict[str, Any] = dict(sync_run_data) if isinstance(sync_run_data, dict) else {}
    return envelope(
        {
            "id": id,
            "status": "pending_sync",
            "message": "边界已保存，已创建下游同步运行",
            "trace_id": ctx.trace_id,
            "boundary": boundary,
            "run_id": run_data.get("run_id"),
            "run_type": run_data.get("run_type"),
            "sync_run": run_data,
            "affected_objects": run_data.get("affected_objects", []),
            "next_actions": run_data.get(
                "next_actions",
                [{"key": "view_audio_session", "label": "返回调听"}],
            ),
        },
        ctx,
    )
