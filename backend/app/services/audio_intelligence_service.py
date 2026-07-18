from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any
from urllib.error import HTTPError, URLError

from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import RunRecord, StorageObject
from app.services.adapters import object_storage_client_for_provider
from app.services.resource_service import upsert_resource

DEFAULT_OUTPUT_ASSETS = {
    "vad": "auris/audio/voice_segments",
    "asr": "auris/model/asr_transcripts",
    "diarization": "auris/audio/speaker_turns",
    "voiceprint": "auris/audio/voiceprint_samples",
    "quality": "auris/audio/audio_quality",
}

TIMED_OUTPUT_KEYS = {
    "vad": "vad_segments",
    "asr": "asr_segments",
    "diarization": "speaker_turns",
}

STORAGE_OBJECT_READY_STATUSES = frozenset(
    {"registered", "ready", "uploaded", "completed", "available", "verified"}
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _real_object_storage_enabled() -> bool:
    return get_settings().auris_object_storage_adapter.strip().lower() == "real"


def _normalized_etag(value: object) -> str:
    return str(value or "").strip().strip('"').lower()


def _verify_remote_storage_object(storage_object: StorageObject, *, purpose: str) -> None:
    if not _real_object_storage_enabled():
        return
    details = [
        {
            "storage_object_id": storage_object.storage_object_id,
            "purpose": purpose,
            "provider": storage_object.provider,
            "bucket": storage_object.bucket,
            "object_key": storage_object.object_key,
        }
    ]
    try:
        client = object_storage_client_for_provider(storage_object.provider)
        if not client.allows_bucket(storage_object.bucket):
            raise ApiError(
                "STORAGE_OBJECT_BUCKET_FORBIDDEN",
                f"{purpose} 引用的 bucket 不在 Provider 允许列表中",
                403,
                details=details,
                retryable=False,
            )
        remote = client.head_object(storage_object.bucket, storage_object.object_key)
    except ApiError:
        raise
    except HTTPError as exc:
        if exc.code == 404:
            raise ApiError(
                "STORAGE_OBJECT_REMOTE_NOT_FOUND",
                f"{purpose} 引用的远端对象不存在",
                404,
                details=details,
                retryable=False,
            ) from exc
        raise ApiError(
            "STORAGE_OBJECT_REMOTE_VERIFY_FAILED",
            f"{purpose} 无法通过对象存储 HEAD 校验",
            502,
            details=details,
            retryable=True,
        ) from exc
    except ValueError as exc:
        raise ApiError(
            "STORAGE_OBJECT_PROVIDER_NOT_CONFIGURED",
            f"{purpose} 引用的对象存储 Provider 未正确配置",
            503,
            details=details,
            retryable=False,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise ApiError(
            "STORAGE_OBJECT_REMOTE_VERIFY_FAILED",
            f"{purpose} 无法通过对象存储 HEAD 校验",
            502,
            details=details,
            retryable=True,
        ) from exc

    raw_remote_size = remote.get("content_length") if isinstance(remote, dict) else None
    try:
        if raw_remote_size is None:
            raise ValueError("missing Content-Length")
        remote_size = int(str(raw_remote_size))
    except (TypeError, ValueError) as exc:
        raise ApiError(
            "STORAGE_OBJECT_REMOTE_METADATA_INCOMPLETE",
            f"{purpose} 的远端对象缺少有效 Content-Length",
            409,
            details=details,
            retryable=False,
        ) from exc
    if remote_size != storage_object.size_bytes:
        raise ApiError(
            "STORAGE_OBJECT_REMOTE_SIZE_MISMATCH",
            f"{purpose} 的远端对象大小与完成回执不一致",
            409,
            details=[
                {
                    **details[0],
                    "descriptor_size_bytes": storage_object.size_bytes,
                    "remote_size_bytes": remote_size,
                }
            ],
            retryable=False,
        )
    descriptor_etag = _normalized_etag(storage_object.etag)
    remote_etag = _normalized_etag(remote.get("etag") if isinstance(remote, dict) else None)
    if descriptor_etag and remote_etag and descriptor_etag != remote_etag:
        raise ApiError(
            "STORAGE_OBJECT_REMOTE_ETAG_MISMATCH",
            f"{purpose} 的远端对象 ETag 与完成回执不一致",
            409,
            details=details,
            retryable=False,
        )


def validate_scoped_storage_object_reference(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    storage_object_id: str,
    purpose: str,
    expected_content_sha256: str | None = None,
) -> StorageObject:
    normalized_id = storage_object_id.strip()
    storage_object = session.get(StorageObject, normalized_id)
    details = [{"storage_object_id": normalized_id, "purpose": purpose}]
    if storage_object is None:
        raise ApiError(
            "STORAGE_OBJECT_NOT_FOUND",
            f"{purpose} 引用的对象存储记录不存在",
            404,
            details=details,
        )
    if storage_object.tenant_id != tenant_id or storage_object.project_id != project_id:
        raise ApiError(
            "STORAGE_OBJECT_SCOPE_FORBIDDEN",
            f"{purpose} 引用不属于当前租户和项目",
            403,
            details=details,
        )
    if storage_object.status not in STORAGE_OBJECT_READY_STATUSES:
        raise ApiError(
            "STORAGE_OBJECT_NOT_READY",
            f"{purpose} 引用的对象尚未完成上传或登记",
            409,
            details=[
                {
                    **details[0],
                    "status": storage_object.status,
                    "allowed_statuses": sorted(STORAGE_OBJECT_READY_STATUSES),
                }
            ],
        )

    missing_fields: list[str] = []
    for field in ("provider", "bucket", "object_key", "object_key_sha256", "content_type"):
        if not str(getattr(storage_object, field, "") or "").strip():
            missing_fields.append(field)
    if storage_object.size_bytes is None or storage_object.size_bytes <= 0:
        missing_fields.append("size_bytes")
    content_sha256 = str(storage_object.content_sha256 or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(content_sha256):
        missing_fields.append("content_sha256")
    if missing_fields:
        raise ApiError(
            "STORAGE_OBJECT_METADATA_INCOMPLETE",
            f"{purpose} 引用缺少可验证的对象元数据",
            409,
            details=[{**details[0], "missing_or_invalid_fields": missing_fields}],
        )

    expected_locator_hash = hashlib.sha256(storage_object.object_key.encode("utf-8")).hexdigest()
    if storage_object.object_key_sha256.lower() != expected_locator_hash:
        raise ApiError(
            "STORAGE_OBJECT_LOCATOR_INVALID",
            f"{purpose} 引用的对象路径哈希不一致",
            409,
            details=details,
        )
    if expected_content_sha256 is not None:
        normalized_expected_hash = expected_content_sha256.strip().lower()
        if not SHA256_PATTERN.fullmatch(normalized_expected_hash):
            raise ApiError(
                "STORAGE_OBJECT_EXPECTED_HASH_INVALID",
                f"{purpose} 缺少有效的预期内容哈希",
                422,
                details=details,
            )
        if content_sha256 != normalized_expected_hash:
            raise ApiError(
                "STORAGE_OBJECT_CONTENT_HASH_MISMATCH",
                f"{purpose} 引用的对象内容哈希与完成回执不一致",
                409,
                details=details,
            )
    _verify_remote_storage_object(storage_object, purpose=purpose)
    return storage_object


def _attached_storage_session(record: RunRecord) -> Session:
    session = object_session(record)
    if session is None:
        raise ApiError(
            "STORAGE_OBJECT_VALIDATION_UNAVAILABLE",
            "对象存储引用校验要求运行记录处于数据库事务中",
            409,
        )
    return session


def validate_audio_intelligence_result(
    record: RunRecord,
    raw_result_ref: object,
) -> dict[str, Any]:
    if not isinstance(raw_result_ref, dict):
        raise ApiError(
            "AUDIO_INTELLIGENCE_RESULT_REQUIRED",
            "音频智能完成回执必须包含 result_ref",
            422,
        )
    result_ref = dict(raw_result_ref)
    payload = record.payload if isinstance(record.payload, dict) else {}
    capabilities = payload.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, list) else []
    if not capabilities:
        raise ApiError(
            "AUDIO_INTELLIGENCE_CAPABILITIES_MISSING",
            "音频智能运行缺少受控能力列表",
            409,
        )
    _validate_scope_reference(
        "audio_session_id",
        payload.get("audio_session_id"),
        result_ref.get("audio_session_id"),
    )
    _validate_scope_reference(
        "recording_id",
        payload.get("recording_id"),
        result_ref.get("recording_id"),
    )

    raw_statuses = result_ref.get("capability_statuses")
    if not isinstance(raw_statuses, dict):
        raise ApiError(
            "AUDIO_CAPABILITY_STATUSES_REQUIRED",
            "完成回执必须逐项声明能力执行状态",
            422,
        )
    for capability in capabilities:
        if capability not in DEFAULT_OUTPUT_ASSETS:
            raise ApiError(
                "AUDIO_CAPABILITY_UNSUPPORTED",
                f"不支持的音频能力：{capability}",
                422,
            )
        state = _capability_state(raw_statuses, capability)
        if capability in TIMED_OUTPUT_KEYS:
            output_key = TIMED_OUTPUT_KEYS[capability]
            items = result_ref.get(output_key)
            if not isinstance(items, list):
                raise ApiError(
                    "AUDIO_CAPABILITY_OUTPUT_REQUIRED",
                    f"{capability} 必须返回 {output_key} 数组",
                    422,
                )
            if state == "success" and not items:
                raise ApiError(
                    "AUDIO_CAPABILITY_EMPTY_SUCCESS",
                    f"{capability} 不能以空输出声明成功",
                    422,
                )
            if state == "no_content" and items:
                raise ApiError(
                    "AUDIO_CAPABILITY_NO_CONTENT_CONFLICT",
                    f"{capability} 声明 no_content 时输出必须为空",
                    422,
                )
            _validate_timed_items(capability, items)
        elif capability == "voiceprint" and state == "success":
            _validate_voiceprint_result(result_ref)
        elif capability == "quality" and state == "success":
            _validate_quality_result(result_ref)
    hotword_pack_version_id = payload.get("hotword_pack_version_id")
    if isinstance(hotword_pack_version_id, str) and hotword_pack_version_id:
        diagnostics = result_ref.get("hotword_diagnostics")
        if not isinstance(diagnostics, dict):
            raise ApiError(
                "HOTWORD_DIAGNOSTICS_REQUIRED",
                "绑定热词包版本的完成回执必须包含 hotword_diagnostics",
                422,
            )
        if diagnostics.get("hotword_pack_version_id") != hotword_pack_version_id:
            raise ApiError(
                "HOTWORD_DIAGNOSTICS_VERSION_MISMATCH",
                "热词诊断版本与运行请求不一致",
                409,
            )
        for field in ("matched_terms", "missed_terms", "false_boosted_terms"):
            if not isinstance(diagnostics.get(field), list):
                raise ApiError(
                    "HOTWORD_DIAGNOSTICS_INVALID",
                    f"热词诊断缺少 {field} 数组",
                    422,
                )
        diagnostics_storage_object_id = diagnostics.get(
            "diagnostics_storage_object_id"
        ) or result_ref.get("diagnostics_storage_object_id")
        if (
            not isinstance(diagnostics_storage_object_id, str)
            or not diagnostics_storage_object_id.strip()
        ):
            raise ApiError(
                "HOTWORD_DIAGNOSTICS_STORAGE_OBJECT_REQUIRED",
                "完整热词诊断必须写入对象存储并返回引用",
                422,
            )
        validate_scoped_storage_object_reference(
            _attached_storage_session(record),
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            storage_object_id=diagnostics_storage_object_id,
            purpose="热词诊断",
        )
    if payload.get("return_word_timestamps") is True:
        storage_object_id = result_ref.get("word_timestamps_storage_object_id")
        if not isinstance(storage_object_id, str) or not storage_object_id.strip():
            raise ApiError(
                "WORD_TIMESTAMPS_STORAGE_OBJECT_REQUIRED",
                "请求词级时间戳时完成回执必须返回对象存储引用",
                422,
            )
        validate_scoped_storage_object_reference(
            _attached_storage_session(record),
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            storage_object_id=storage_object_id,
            purpose="词级时间戳",
        )
    return result_ref


def sanitize_audio_intelligence_result(result_ref: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(result_ref)
    diagnostics = sanitized.get("hotword_diagnostics")
    if isinstance(diagnostics, dict):
        storage_object_id = diagnostics.get("diagnostics_storage_object_id") or sanitized.get(
            "diagnostics_storage_object_id"
        )
        sanitized["hotword_diagnostics"] = {
            "hotword_pack_version_id": diagnostics.get("hotword_pack_version_id"),
            "matched_count": len(diagnostics.get("matched_terms") or []),
            "missed_count": len(diagnostics.get("missed_terms") or []),
            "false_boosted_count": len(diagnostics.get("false_boosted_terms") or []),
            "provider_artifact_version": diagnostics.get("provider_artifact_version"),
            "diagnostics_storage_object_id": storage_object_id,
        }
    return sanitized


def _validate_scope_reference(field: str, expected: object, actual: object) -> None:
    if not isinstance(actual, str) or not actual:
        raise ApiError(
            "AUDIO_RESULT_SCOPE_REQUIRED",
            f"完成回执缺少 {field}",
            422,
        )
    if isinstance(expected, str) and expected and actual != expected:
        raise ApiError(
            "AUDIO_RESULT_SCOPE_MISMATCH",
            f"完成回执 {field} 与运行输入不一致",
            409,
            details=[{"field": field, "expected": expected, "actual": actual}],
        )


def _capability_state(statuses: dict[str, Any], capability: str) -> str:
    raw_state = statuses.get(capability)
    if not isinstance(raw_state, dict):
        raise ApiError(
            "AUDIO_CAPABILITY_STATUS_REQUIRED",
            f"完成回执缺少 {capability} 能力状态",
            422,
        )
    status = raw_state.get("status")
    if status not in {"success", "no_content"}:
        raise ApiError(
            "AUDIO_CAPABILITY_STATUS_INVALID",
            f"{capability} 状态必须是 success 或 no_content",
            422,
        )
    if status == "no_content" and not str(raw_state.get("reason") or "").strip():
        raise ApiError(
            "AUDIO_CAPABILITY_EMPTY_REASON_REQUIRED",
            f"{capability} no_content 必须说明原因",
            422,
        )
    return str(status)


def _validate_timed_items(capability: str, items: list[Any]) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ApiError(
                "AUDIO_SEGMENT_INVALID",
                f"{capability} 第 {index + 1} 项必须是对象",
                422,
            )
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int | float)
            or not isinstance(end_ms, int | float)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise ApiError(
                "AUDIO_SEGMENT_WINDOW_INVALID",
                f"{capability} 第 {index + 1} 项时间窗无效",
                422,
            )
        confidence = item.get("confidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
        ):
            raise ApiError(
                "AUDIO_SEGMENT_CONFIDENCE_INVALID",
                f"{capability} 第 {index + 1} 项置信度无效",
                422,
            )
        if capability == "asr" and not str(item.get("text") or "").strip():
            raise ApiError(
                "ASR_SEGMENT_TEXT_REQUIRED",
                f"ASR 第 {index + 1} 项缺少文本",
                422,
            )
        if capability == "diarization" and not str(item.get("speaker") or "").strip():
            raise ApiError(
                "DIARIZATION_SPEAKER_REQUIRED",
                f"说话人分离第 {index + 1} 项缺少 speaker",
                422,
            )


def _validate_voiceprint_result(result_ref: dict[str, Any]) -> None:
    if not str(result_ref.get("speaker_ref") or "").strip():
        raise ApiError("VOICEPRINT_SPEAKER_REQUIRED", "声纹输出缺少 speaker_ref", 422)
    score = result_ref.get("voiceprint_quality_score")
    if isinstance(score, bool) or not isinstance(score, int | float) or not 0 <= score <= 100:
        raise ApiError("VOICEPRINT_QUALITY_INVALID", "声纹质量评分无效", 422)
    embedding_ref = result_ref.get("voiceprint_embedding_ref")
    if not isinstance(embedding_ref, dict):
        raise ApiError("VOICEPRINT_EMBEDDING_REQUIRED", "声纹输出缺少向量引用", 422)
    vector_dim = embedding_ref.get("vector_dim")
    if (
        isinstance(vector_dim, bool)
        or not isinstance(vector_dim, int)
        or not 1 <= vector_dim <= 4096
        or not str(embedding_ref.get("collection") or "").strip()
        or not str(embedding_ref.get("point_id") or "").strip()
    ):
        raise ApiError("VOICEPRINT_EMBEDDING_INVALID", "声纹向量引用不完整", 422)


def _validate_quality_result(result_ref: dict[str, Any]) -> None:
    snr_db = result_ref.get("snr_db")
    if isinstance(snr_db, bool) or not isinstance(snr_db, int | float):
        raise ApiError("AUDIO_QUALITY_SNR_REQUIRED", "音频质量输出缺少有效 SNR", 422)
    if result_ref.get("crosstalk_risk") not in {"low", "medium", "high", "critical"}:
        raise ApiError("AUDIO_QUALITY_CROSSTALK_INVALID", "串音风险枚举无效", 422)


def audio_intelligence_output_assets(capabilities: Sequence[str]) -> list[dict[str, Any]]:
    return [
        {
            "capability": capability,
            "asset_key": DEFAULT_OUTPUT_ASSETS[capability],
            "status": "pending",
        }
        for capability in capabilities
        if capability in DEFAULT_OUTPUT_ASSETS
    ]


def materialize_audio_intelligence_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    result_ref = validate_audio_intelligence_result(
        record,
        completion_receipt.get("result_ref"),
    )
    audio_session_id = str(
        payload.get("audio_session_id") or result_ref.get("audio_session_id") or ""
    )
    recording_id = str(payload.get("recording_id") or result_ref.get("recording_id") or "")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = []

    materialized: list[dict[str, Any]] = []
    if "vad" in capabilities:
        capability_status = _capability_state(result_ref["capability_statuses"], "vad")
        materialized.append(
            _upsert_audio_resource(
                session,
                ctx,
                record,
                "vad_segments",
                f"{audio_session_id}:vad:{record.run_id}",
                {
                    "audio_session_id": audio_session_id,
                    "recording_id": recording_id,
                    "segments": result_ref["vad_segments"],
                    "empty_reason": _empty_reason(result_ref, "vad"),
                    "asset_key": DEFAULT_OUTPUT_ASSETS["vad"],
                },
                status=capability_status,
            )
        )
    if "diarization" in capabilities:
        capability_status = _capability_state(result_ref["capability_statuses"], "diarization")
        materialized.append(
            _upsert_audio_resource(
                session,
                ctx,
                record,
                "speaker_turns",
                f"{audio_session_id}:speaker:{record.run_id}",
                {
                    "audio_session_id": audio_session_id,
                    "recording_id": recording_id,
                    "turns": result_ref["speaker_turns"],
                    "empty_reason": _empty_reason(result_ref, "diarization"),
                    "asset_key": DEFAULT_OUTPUT_ASSETS["diarization"],
                },
                status=capability_status,
            )
        )
    if "asr" in capabilities:
        capability_status = _capability_state(result_ref["capability_statuses"], "asr")
        diagnostics = result_ref.get("hotword_diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        materialized.append(
            _upsert_audio_resource(
                session,
                ctx,
                record,
                "asr_segments",
                f"{audio_session_id}:asr:{record.run_id}",
                {
                    "audio_session_id": audio_session_id,
                    "recording_id": recording_id,
                    "segments": result_ref["asr_segments"],
                    "empty_reason": _empty_reason(result_ref, "asr"),
                    "asset_key": DEFAULT_OUTPUT_ASSETS["asr"],
                    "hotword_pack_version_id": payload.get("hotword_pack_version_id"),
                    "word_timestamps_storage_object_id": result_ref.get(
                        "word_timestamps_storage_object_id"
                    ),
                    "hotword_diagnostics": {
                        "matched_count": len(diagnostics.get("matched_terms") or []),
                        "missed_count": len(diagnostics.get("missed_terms") or []),
                        "false_boosted_count": len(diagnostics.get("false_boosted_terms") or []),
                        "provider_artifact_version": diagnostics.get("provider_artifact_version"),
                        "diagnostics_storage_object_id": diagnostics.get(
                            "diagnostics_storage_object_id"
                        )
                        or result_ref.get("diagnostics_storage_object_id"),
                    }
                    if diagnostics
                    else None,
                },
                status=capability_status,
            )
        )
    if "voiceprint" in capabilities:
        capability_status = _capability_state(result_ref["capability_statuses"], "voiceprint")
        embedding_ref = result_ref.get("voiceprint_embedding_ref") or {}
        materialized.append(
            _upsert_audio_resource(
                session,
                ctx,
                record,
                "voiceprint_samples",
                f"{audio_session_id}:voiceprint:{record.run_id}",
                {
                    "audio_session_id": audio_session_id,
                    "recording_id": recording_id,
                    "speaker_ref": result_ref.get("speaker_ref"),
                    "quality_score": result_ref.get("voiceprint_quality_score"),
                    "embedding_ref": embedding_ref,
                    "empty_reason": _empty_reason(result_ref, "voiceprint"),
                    "asset_key": DEFAULT_OUTPUT_ASSETS["voiceprint"],
                },
                status=capability_status,
            )
        )
    if "quality" in capabilities:
        capability_status = _capability_state(result_ref["capability_statuses"], "quality")
        materialized.append(
            _upsert_audio_resource(
                session,
                ctx,
                record,
                "audio_quality_reports",
                f"{audio_session_id}:quality:{record.run_id}",
                {
                    "audio_session_id": audio_session_id,
                    "recording_id": recording_id,
                    "snr_db": result_ref.get("snr_db"),
                    "crosstalk_risk": result_ref.get("crosstalk_risk"),
                    "empty_reason": _empty_reason(result_ref, "quality"),
                    "asset_key": DEFAULT_OUTPUT_ASSETS["quality"],
                },
                status=capability_status,
            )
        )
    return materialized


def _upsert_audio_resource(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    collection: str,
    resource_key: str,
    data: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    resource_data = {
        "id": resource_key,
        **data,
        "status": status,
        "source_run_id": record.run_id,
        "trace_id": record.trace_id,
    }
    upsert_resource(
        session,
        ctx,
        collection,
        resource_key,
        resource_data,
        status=status,
        trace_id=record.trace_id,
        audit_action=f"{collection}.materialize",
    )
    return {
        "collection": collection,
        "id": resource_key,
        "asset_key": data.get("asset_key"),
        "status": status,
    }


def _empty_reason(result_ref: dict[str, Any], capability: str) -> str | None:
    statuses = result_ref.get("capability_statuses")
    state = statuses.get(capability) if isinstance(statuses, dict) else None
    if isinstance(state, dict) and state.get("status") == "no_content":
        return str(state.get("reason") or "") or None
    return None
