from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError

from sqlalchemy.orm import Session, object_session

from app.core.config import get_settings
from app.core.context import RequestContext
from app.core.errors import ApiError
from app.models import RunRecord, StorageObject
from app.services.adapters import object_storage_client_for_provider

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

AUDIO_RESULT_MANIFEST_SCHEMA = "auris-flow-audio-result-manifest-v1"
AUDIO_RESULT_RECEIPT_SCHEMA = "auris-flow-audio-result-receipt-v1"
AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION = "auris-flow-audio-input-integrity-v1"
MAX_AUDIO_RESULT_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_AUDIO_RESULT_SEGMENTS = 10_000
MAX_AUDIO_RESULT_ANALYSES = 5
MAX_AUDIO_RESULT_LABELS = 100
MAX_AUDIO_SEGMENT_MILLISECONDS = 604_800_000

_AUDIO_RESULT_RECEIPT_FIELDS = frozenset(
    {
        "manifest_version",
        "status",
        "result_manifest_schema",
        "result_manifest_sha256",
        "result_manifest_storage_object_id",
        "result_manifest_object_key_sha256",
        "result_manifest_version_id_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
        "execution_contract",
        "execution_envelope_sha256",
        "input_integrity_manifest_sha256",
        "inference_binding_sha256",
        "requested_capabilities",
        "storage_objects",
    }
)
_AUDIO_RESULT_DESCRIPTOR_FIELDS = frozenset(
    {
        "storage_object_id",
        "role",
        "provider",
        "bucket",
        "object_key",
        "version_id",
        "content_type",
        "size_bytes",
        "content_sha256",
    }
)
_AUDIO_RESULT_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "execution_contract",
        "execution_envelope_sha256",
        "tenant_id",
        "project_id",
        "trace_id",
        "run_id",
        "dispatch_idempotency_key",
        "outbox_fencing_token",
        "audio_session_id",
        "recording_id",
        "input_object",
        "inference",
        "capabilities",
        "input_integrity",
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
        "provider_result",
    }
)
_AUDIO_INPUT_OBJECT_FIELDS = frozenset(
    {
        "storage_object_id",
        "storage_provider",
        "bucket",
        "object_key",
        "version_id",
        "content_sha256",
        "content_length",
        "content_type",
    }
)
_AUDIO_INPUT_INTEGRITY_FIELDS = frozenset(
    {
        "manifest_version",
        "status",
        "execution_envelope_sha256",
        "storage_object_id_sha256",
        "object_version_id_sha256",
        "expected_content_sha256",
        "observed_content_sha256",
        "content_length",
    }
)
_AUDIO_PROVIDER_RESULT_FIELDS = frozenset({"transcript", "analyses", "review_outputs"})
_AUDIO_TRANSCRIPT_FIELDS = frozenset({"language", "text", "segments"})
_AUDIO_SEGMENT_FIELDS = frozenset({"start_ms", "end_ms", "speaker", "text", "confidence"})
_AUDIO_ANALYSIS_FIELDS = frozenset({"capability", "summary", "score", "labels"})
_AUDIO_LABEL_FIELDS = frozenset({"label", "score"})
_AUDIO_REVIEW_OUTPUT_FIELDS = frozenset({"event_links", "label_candidates"})
_AUDIO_REVIEW_EVENT_LINK_FIELDS = frozenset(
    {
        "source_event_id",
        "document_ref",
        "relation_type",
        "confidence",
        "evidence_window",
    }
)
_AUDIO_REVIEW_LABEL_CANDIDATE_FIELDS = frozenset(
    {
        "label",
        "value_or_action",
        "confidence",
    }
)
_AUDIO_RAW_LOCATOR_FIELDS = frozenset(
    {
        "storage_objects",
        "storage_provider",
        "provider",
        "bucket",
        "object_key",
        "version_id",
        "endpoint",
    }
)


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
    if (
        _real_object_storage_enabled()
        and storage_object.source_type == "audio_recording"
        and storage_object.status not in {"verified", "active"}
    ):
        raise ApiError(
            "STORAGE_OBJECT_NOT_VERIFIED",
            f"{purpose} 引用的录音对象尚未完成内容验证",
            409,
            details=[
                {
                    **details[0],
                    "status": storage_object.status,
                    "allowed_statuses": ["active", "verified"],
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


def _audio_manifest_error(
    suffix: str,
    message: str,
    *,
    status_code: int = 409,
    retryable: bool = False,
) -> ApiError:
    return ApiError(
        f"AUDIO_RESULT_MANIFEST_{suffix}",
        message,
        status_code,
        retryable=retryable,
    )


def _manifest_mapping(
    raw: object,
    *,
    fields: frozenset[str],
    name: str,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 必须是对象")
    if set(raw) != fields:
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 字段集合无效")
    return dict(raw)


def _manifest_text(
    raw: object,
    *,
    name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(raw, str) or len(raw) > maximum or (not allow_empty and not raw):
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 字段无效")
    if "\x00" in raw or any(ord(char) < 0x20 and char not in "\t\n\r" for char in raw):
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 字段无效")
    return raw


def _manifest_sha256(raw: object, *, name: str) -> str:
    value = _manifest_text(raw, name=name, maximum=64)
    if SHA256_PATTERN.fullmatch(value) is None:
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 必须是 SHA-256")
    return value


def _manifest_score(raw: object, *, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 评分无效")
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 评分无效")
    return value


def _manifest_millisecond(raw: object, *, name: str) -> int:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not 0 <= raw <= MAX_AUDIO_SEGMENT_MILLISECONDS
    ):
        raise _audio_manifest_error("CONTRACT_INVALID", f"{name} 时间值无效")
    return raw


def _manifest_canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _audio_manifest_error("CONTRACT_INVALID", "音频结果 manifest 无法规范化") from exc


def _manifest_canonical_sha256(value: object) -> str:
    return hashlib.sha256(_manifest_canonical_bytes(value)).hexdigest()


def _manifest_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _audio_manifest_error("JSON_INVALID", "音频结果 manifest 包含重复字段")
        result[key] = value
    return result


def _manifest_invalid_constant(_value: str) -> None:
    raise _audio_manifest_error("JSON_INVALID", "音频结果 manifest 包含非有限数值")


def _decode_audio_result_manifest(body: bytes) -> dict[str, Any]:
    try:
        raw = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_manifest_json_object,
            parse_constant=_manifest_invalid_constant,
        )
    except ApiError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _audio_manifest_error("JSON_INVALID", "音频结果 manifest 不是严格 JSON") from exc
    return _manifest_mapping(
        raw,
        fields=_AUDIO_RESULT_MANIFEST_FIELDS,
        name="音频结果 manifest",
    )


def _hash_matches(actual: str, expected: str) -> bool:
    return hmac.compare_digest(actual.encode("ascii"), expected.encode("ascii"))


def _validate_audio_manifest_receipt_bindings(
    record: RunRecord,
    receipt: dict[str, Any],
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    capabilities = payload.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or len(set(capabilities)) != len(capabilities)
        or any(
            not isinstance(capability, str) or capability not in DEFAULT_OUTPUT_ASSETS
            for capability in capabilities
        )
    ):
        raise _audio_manifest_error("RUN_BINDING_INVALID", "音频运行缺少冻结的能力绑定")
    if receipt.get("requested_capabilities") != capabilities:
        raise _audio_manifest_error("BINDING_MISMATCH", "音频结果能力绑定不一致")

    execution_contract = _manifest_text(
        payload.get("execution_contract"),
        name="execution_contract",
        maximum=128,
    )
    provider = _manifest_text(payload.get("provider"), name="provider", maximum=128)
    model = _manifest_text(payload.get("model_version"), name="model_version", maximum=128)
    dispatch = payload.get("dispatch")
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    details = dispatch.get("details")
    details = details if isinstance(details, dict) else {}
    dispatch_idempotency_key = _manifest_text(
        details.get("dispatch_idempotency_key"),
        name="dispatch_idempotency_key",
        maximum=256,
    )
    fencing_token = _manifest_text(
        details.get("fencing_token"),
        name="outbox_fencing_token",
        maximum=64,
    )
    envelope_sha256 = _manifest_sha256(
        details.get("execution_envelope_sha256"),
        name="execution_envelope_sha256",
    )
    if receipt.get("execution_contract") != execution_contract:
        raise _audio_manifest_error("BINDING_MISMATCH", "音频执行契约绑定不一致")
    if receipt.get("execution_envelope_sha256") != envelope_sha256:
        raise _audio_manifest_error("BINDING_MISMATCH", "音频执行信封绑定不一致")
    expected_inference_hash = hashlib.sha256(f"{provider}\n{model}".encode()).hexdigest()
    if receipt.get("inference_binding_sha256") != expected_inference_hash:
        raise _audio_manifest_error("BINDING_MISMATCH", "音频推理模型绑定不一致")

    input_object = _manifest_mapping(
        payload.get("input_object"),
        fields=_AUDIO_INPUT_OBJECT_FIELDS,
        name="冻结输入对象",
    )
    return (
        capabilities,
        input_object,
        {
            "execution_contract": execution_contract,
            "provider": provider,
            "model": model,
            "dispatch_idempotency_key": dispatch_idempotency_key,
            "fencing_token": fencing_token,
            "execution_envelope_sha256": envelope_sha256,
        },
    )


def _validate_audio_manifest_descriptor(
    record: RunRecord,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    raw_descriptors = receipt.get("storage_objects")
    if not isinstance(raw_descriptors, list) or len(raw_descriptors) != 1:
        raise _audio_manifest_error("DESCRIPTOR_INVALID", "音频结果必须包含唯一 manifest 描述符")
    descriptor = _manifest_mapping(
        raw_descriptors[0],
        fields=_AUDIO_RESULT_DESCRIPTOR_FIELDS,
        name="音频结果 manifest 描述符",
    )
    manifest_sha256 = _manifest_sha256(
        receipt.get("result_manifest_sha256"),
        name="result_manifest_sha256",
    )
    storage_object_id = _manifest_text(
        descriptor.get("storage_object_id"),
        name="storage_object_id",
        maximum=128,
    )
    expected_storage_object_id = f"sto_audio_manifest_{manifest_sha256[:32]}"
    if (
        receipt.get("result_manifest_storage_object_id") != storage_object_id
        or storage_object_id != expected_storage_object_id
        or descriptor.get("role") != "manifest"
        or descriptor.get("content_type") != "application/json"
        or descriptor.get("content_sha256") != manifest_sha256
    ):
        raise _audio_manifest_error("DESCRIPTOR_MISMATCH", "音频结果 manifest 描述符绑定不一致")

    provider = _manifest_text(descriptor.get("provider"), name="provider", maximum=32)
    if provider not in {"minio", "s3"}:
        raise _audio_manifest_error(
            "PROVIDER_UNSUPPORTED",
            "音频结果 manifest 的对象存储 provider 不支持精确版本读取",
            status_code=422,
        )
    _manifest_text(descriptor.get("bucket"), name="bucket", maximum=255)
    object_key = _manifest_text(
        descriptor.get("object_key"),
        name="object_key",
        maximum=1024,
    )
    expected_prefix = (
        f"tenants/{record.tenant_id}/projects/{record.project_id}/runs/{record.run_id}/"
        "audio-intelligence/"
    )
    key_parts = object_key.split("/")
    if (
        not object_key.startswith(expected_prefix)
        or not object_key.endswith(".json")
        or object_key.startswith("/")
        or "\\" in object_key
        or any(part in {"", ".", ".."} for part in key_parts)
    ):
        raise _audio_manifest_error("LOCATOR_INVALID", "音频结果 manifest 路径不在运行隔离前缀内")
    object_key_sha256 = _manifest_sha256(
        receipt.get("result_manifest_object_key_sha256"),
        name="result_manifest_object_key_sha256",
    )
    if not _hash_matches(hashlib.sha256(object_key.encode()).hexdigest(), object_key_sha256):
        raise _audio_manifest_error("LOCATOR_MISMATCH", "音频结果 manifest 路径哈希不一致")

    version_id = _manifest_text(
        descriptor.get("version_id"),
        name="version_id",
        maximum=1024,
    )
    if (
        version_id.casefold() == "null"
        or version_id != version_id.strip()
        or any(ord(char) < 0x20 for char in version_id)
    ):
        raise _audio_manifest_error("VERSION_INVALID", "音频结果 manifest 缺少精确版本")
    version_sha256 = _manifest_sha256(
        receipt.get("result_manifest_version_id_sha256"),
        name="result_manifest_version_id_sha256",
    )
    if not _hash_matches(hashlib.sha256(version_id.encode()).hexdigest(), version_sha256):
        raise _audio_manifest_error("VERSION_MISMATCH", "音频结果 manifest 版本哈希不一致")

    size_bytes = descriptor.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= MAX_AUDIO_RESULT_MANIFEST_BYTES
    ):
        raise _audio_manifest_error("SIZE_INVALID", "音频结果 manifest 大小无效")
    return descriptor


def _read_exact_audio_result_manifest(descriptor: dict[str, Any]) -> bytes:
    provider = str(descriptor["provider"])
    bucket = str(descriptor["bucket"])
    object_key = str(descriptor["object_key"])
    version_id = str(descriptor["version_id"])
    try:
        client = object_storage_client_for_provider(provider)
        if not client.allows_bucket(bucket):
            raise _audio_manifest_error(
                "BUCKET_FORBIDDEN",
                "音频结果 manifest bucket 不在允许列表内",
                status_code=403,
            )
        response = client.get_object_version(
            bucket,
            object_key,
            version_id=version_id,
            max_response_bytes=MAX_AUDIO_RESULT_MANIFEST_BYTES,
        )
    except ApiError:
        raise
    except (AttributeError, ValueError) as exc:
        raise _audio_manifest_error(
            "STORAGE_NOT_CONFIGURED",
            "音频结果 manifest 精确版本读取未正确配置",
            status_code=503,
        ) from exc
    except HTTPError as exc:
        status_code = 404 if exc.code == 404 else 502
        raise _audio_manifest_error(
            "READ_FAILED",
            "音频结果 manifest 精确版本读取失败",
            status_code=status_code,
            retryable=exc.code != 404,
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        raise _audio_manifest_error(
            "READ_FAILED",
            "音频结果 manifest 精确版本读取失败",
            status_code=502,
            retryable=True,
        ) from exc

    if not isinstance(response, dict) or response.get("status") != 200:
        raise _audio_manifest_error("RESPONSE_INVALID", "音频结果 manifest 响应状态无效")
    if response.get("version_id") != version_id:
        raise _audio_manifest_error("VERSION_MISMATCH", "音频结果 manifest 响应版本不一致")
    raw_content_type = str(response.get("content_type") or "")
    if raw_content_type.partition(";")[0].strip().casefold() != "application/json":
        raise _audio_manifest_error("CONTENT_TYPE_INVALID", "音频结果 manifest 类型无效")
    try:
        declared_length = int(str(response.get("content_length")))
    except (TypeError, ValueError) as exc:
        raise _audio_manifest_error("SIZE_INVALID", "音频结果 manifest 响应大小无效") from exc
    body = response.get("body")
    if not isinstance(body, bytes):
        raise _audio_manifest_error("BODY_INVALID", "音频结果 manifest 响应体无效")
    if declared_length != descriptor["size_bytes"] or len(body) != descriptor["size_bytes"]:
        raise _audio_manifest_error("SIZE_MISMATCH", "音频结果 manifest 大小不一致")
    observed_sha256 = hashlib.sha256(body).hexdigest()
    if not _hash_matches(observed_sha256, str(descriptor["content_sha256"])):
        raise _audio_manifest_error("HASH_MISMATCH", "音频结果 manifest 内容哈希不一致")
    return body


def _validate_audio_provider_result(
    raw: object,
    *,
    capabilities: list[str],
) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or any(not isinstance(key, str) for key in raw):
        raise _audio_manifest_error("CONTRACT_INVALID", "provider_result 必须是对象")
    observed_fields = set(raw)
    if (
        not {"transcript", "analyses"} <= observed_fields
        or observed_fields - _AUDIO_PROVIDER_RESULT_FIELDS
    ):
        raise _audio_manifest_error("CONTRACT_INVALID", "provider_result 字段集合无效")
    result = dict(raw)
    raw_transcript = result.get("transcript")
    transcript: dict[str, Any] | None = None
    if raw_transcript is None:
        if "asr" in capabilities:
            raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "ASR 结果缺少 transcript")
    else:
        if "asr" not in capabilities:
            raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "未请求 ASR 却返回 transcript")
        values = _manifest_mapping(
            raw_transcript,
            fields=_AUDIO_TRANSCRIPT_FIELDS,
            name="provider_result.transcript",
        )
        language = _manifest_text(values.get("language"), name="language", maximum=35)
        text = _manifest_text(
            values.get("text"),
            name="transcript.text",
            maximum=500_000,
            allow_empty=True,
        )
        raw_segments = values.get("segments")
        if not isinstance(raw_segments, list) or len(raw_segments) > MAX_AUDIO_RESULT_SEGMENTS:
            raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "transcript segments 无效")
        segments: list[dict[str, Any]] = []
        previous_start = -1
        for index, raw_segment in enumerate(raw_segments):
            segment = _manifest_mapping(
                raw_segment,
                fields=_AUDIO_SEGMENT_FIELDS,
                name=f"provider_result.transcript.segments[{index}]",
            )
            start_ms = _manifest_millisecond(segment.get("start_ms"), name="start_ms")
            end_ms = _manifest_millisecond(segment.get("end_ms"), name="end_ms")
            if end_ms <= start_ms or start_ms < previous_start:
                raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "transcript 时间窗无效")
            previous_start = start_ms
            raw_speaker = segment.get("speaker")
            speaker = (
                None
                if raw_speaker is None
                else _manifest_text(raw_speaker, name="speaker", maximum=128)
            )
            segments.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "speaker": speaker,
                    "text": _manifest_text(
                        segment.get("text"),
                        name="segment.text",
                        maximum=8_192,
                        allow_empty=True,
                    ),
                    "confidence": _manifest_score(
                        segment.get("confidence"),
                        name="segment.confidence",
                    ),
                }
            )
        transcript = {"language": language, "text": text, "segments": segments}

    raw_analyses = result.get("analyses")
    if not isinstance(raw_analyses, list) or len(raw_analyses) > MAX_AUDIO_RESULT_ANALYSES:
        raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "provider analyses 无效")
    expected_analyses = set(capabilities) - {"asr"}
    analyses: list[dict[str, Any]] = []
    observed_capabilities: set[str] = set()
    for index, raw_analysis in enumerate(raw_analyses):
        analysis = _manifest_mapping(
            raw_analysis,
            fields=_AUDIO_ANALYSIS_FIELDS,
            name=f"provider_result.analyses[{index}]",
        )
        capability = _manifest_text(
            analysis.get("capability"),
            name="analysis.capability",
            maximum=32,
        )
        if capability not in expected_analyses or capability in observed_capabilities:
            raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "provider 能力结果绑定无效")
        observed_capabilities.add(capability)
        raw_labels = analysis.get("labels")
        if not isinstance(raw_labels, list) or len(raw_labels) > MAX_AUDIO_RESULT_LABELS:
            raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "provider labels 无效")
        labels: list[dict[str, Any]] = []
        observed_labels: set[str] = set()
        for label_index, raw_label in enumerate(raw_labels):
            label = _manifest_mapping(
                raw_label,
                fields=_AUDIO_LABEL_FIELDS,
                name=f"provider_result.analyses[{index}].labels[{label_index}]",
            )
            name = _manifest_text(label.get("label"), name="label", maximum=128)
            if name in observed_labels:
                raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "provider label 重复")
            observed_labels.add(name)
            labels.append(
                {
                    "label": name,
                    "score": _manifest_score(label.get("score"), name="label.score"),
                }
            )
        analyses.append(
            {
                "capability": capability,
                "summary": _manifest_text(
                    analysis.get("summary"),
                    name="analysis.summary",
                    maximum=4_096,
                ),
                "score": _manifest_score(analysis.get("score"), name="analysis.score"),
                "labels": labels,
            }
        )
    if observed_capabilities != expected_analyses:
        raise _audio_manifest_error("PROVIDER_RESULT_INVALID", "provider 能力结果不完整")
    normalized = {"transcript": transcript, "analyses": analyses}
    if "review_outputs" in result:
        normalized["review_outputs"] = _validate_audio_review_outputs(
            result.get("review_outputs"),
            error_factory=lambda message: _audio_manifest_error(
                "PROVIDER_RESULT_INVALID",
                message,
            ),
        )
    return normalized


def _review_output_text(value: object, *, field: str, maximum: int) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 0x20 for character in normalized)
    ):
        raise ValueError(f"{field} 无效")
    return normalized


def _review_output_confidence(value: object, *, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
    ):
        raise ValueError(f"{field} 无效")
    return float(value)


def _validate_audio_review_outputs(
    raw: object,
    *,
    error_factory: Any,
) -> dict[str, list[dict[str, Any]]]:
    try:
        if not isinstance(raw, dict) or set(raw) - _AUDIO_REVIEW_OUTPUT_FIELDS:
            raise ValueError("review_outputs 结构无效")
        raw_event_links = raw.get("event_links", [])
        raw_label_candidates = raw.get("label_candidates", [])
        if not isinstance(raw_event_links, list) or len(raw_event_links) > 100:
            raise ValueError("review_outputs.event_links 无效")
        if not isinstance(raw_label_candidates, list) or len(raw_label_candidates) > 100:
            raise ValueError("review_outputs.label_candidates 无效")
        event_links: list[dict[str, Any]] = []
        observed_event_links: set[tuple[str, str, str]] = set()
        for index, item in enumerate(raw_event_links):
            if not isinstance(item, dict) or set(item) != _AUDIO_REVIEW_EVENT_LINK_FIELDS:
                raise ValueError(f"review_outputs.event_links[{index}] 结构无效")
            source_event_id = _review_output_text(
                item.get("source_event_id"),
                field="source_event_id",
                maximum=128,
            )
            document_ref = _review_output_text(
                item.get("document_ref"),
                field="document_ref",
                maximum=128,
            )
            relation_type = _review_output_text(
                item.get("relation_type"),
                field="relation_type",
                maximum=128,
            )
            normalized = {
                "source_event_id": source_event_id,
                "document_ref": document_ref,
                "relation_type": relation_type,
                "confidence": _review_output_confidence(
                    item.get("confidence"),
                    field="confidence",
                ),
                "evidence_window": _review_output_text(
                    item.get("evidence_window"),
                    field="evidence_window",
                    maximum=128,
                ),
            }
            identity = (
                source_event_id,
                document_ref,
                relation_type,
            )
            if identity in observed_event_links:
                raise ValueError("review_outputs.event_links 存在重复业务关联")
            observed_event_links.add(identity)
            event_links.append(normalized)
        label_candidates: list[dict[str, Any]] = []
        observed_labels: set[str] = set()
        for index, item in enumerate(raw_label_candidates):
            if not isinstance(item, dict) or set(item) != _AUDIO_REVIEW_LABEL_CANDIDATE_FIELDS:
                raise ValueError(f"review_outputs.label_candidates[{index}] 结构无效")
            label = _review_output_text(
                item.get("label"),
                field="label",
                maximum=128,
            )
            if label in observed_labels:
                raise ValueError("review_outputs.label_candidates 存在重复标签")
            observed_labels.add(label)
            label_candidates.append(
                {
                    "label": label,
                    "value_or_action": _review_output_text(
                        item.get("value_or_action"),
                        field="value_or_action",
                        maximum=2_000,
                    ),
                    "confidence": _review_output_confidence(
                        item.get("confidence"),
                        field="confidence",
                    ),
                }
            )
    except ValueError as exc:
        raise error_factory(str(exc)) from exc
    return {
        "event_links": event_links,
        "label_candidates": label_candidates,
    }


def _validate_audio_result_manifest(
    record: RunRecord,
    receipt: dict[str, Any],
    manifest: dict[str, Any],
    *,
    capabilities: list[str],
    input_object: dict[str, Any],
    bindings: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("schema_version") != AUDIO_RESULT_MANIFEST_SCHEMA:
        raise _audio_manifest_error("SCHEMA_INVALID", "音频结果 manifest schema 无效")
    expected_values = {
        "execution_contract": bindings["execution_contract"],
        "execution_envelope_sha256": bindings["execution_envelope_sha256"],
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "trace_id": record.trace_id,
        "run_id": record.run_id,
        "dispatch_idempotency_key": bindings["dispatch_idempotency_key"],
        "outbox_fencing_token": bindings["fencing_token"],
        "audio_session_id": record.payload.get("audio_session_id"),
        "recording_id": record.payload.get("recording_id"),
        "input_object": input_object,
        "inference": {"provider": bindings["provider"], "model": bindings["model"]},
        "capabilities": capabilities,
    }
    if any(manifest.get(field) != expected for field, expected in expected_values.items()):
        raise _audio_manifest_error("BINDING_MISMATCH", "音频结果 manifest 与冻结运行不一致")

    integrity = _manifest_mapping(
        manifest.get("input_integrity"),
        fields=_AUDIO_INPUT_INTEGRITY_FIELDS,
        name="input_integrity",
    )
    integrity_sha256 = _manifest_sha256(
        receipt.get("input_integrity_manifest_sha256"),
        name="input_integrity_manifest_sha256",
    )
    if not _hash_matches(_manifest_canonical_sha256(integrity), integrity_sha256):
        raise _audio_manifest_error("INPUT_INTEGRITY_MISMATCH", "输入完整性清单哈希不一致")
    expected_integrity = {
        "manifest_version": AUDIO_INPUT_INTEGRITY_MANIFEST_VERSION,
        "status": "verified",
        "execution_envelope_sha256": bindings["execution_envelope_sha256"],
        "storage_object_id_sha256": hashlib.sha256(
            str(input_object["storage_object_id"]).encode()
        ).hexdigest(),
        "object_version_id_sha256": hashlib.sha256(
            str(input_object["version_id"]).encode()
        ).hexdigest(),
        "expected_content_sha256": input_object["content_sha256"],
        "observed_content_sha256": input_object["content_sha256"],
        "content_length": input_object["content_length"],
    }
    if integrity != expected_integrity:
        raise _audio_manifest_error("INPUT_INTEGRITY_MISMATCH", "输入完整性清单绑定不一致")

    for field in (
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
    ):
        manifest_hash = _manifest_sha256(manifest.get(field), name=field)
        receipt_hash = _manifest_sha256(receipt.get(field), name=field)
        if not _hash_matches(manifest_hash, receipt_hash):
            raise _audio_manifest_error("PROVIDER_HASH_MISMATCH", "provider 证据哈希不一致")
    provider_result = _validate_audio_provider_result(
        manifest.get("provider_result"),
        capabilities=capabilities,
    )
    if not _hash_matches(
        _manifest_canonical_sha256(provider_result),
        str(manifest["provider_result_sha256"]),
    ):
        raise _audio_manifest_error("PROVIDER_HASH_MISMATCH", "provider 结果哈希不一致")
    return provider_result


def _audio_domain_result_from_provider(
    record: RunRecord,
    provider_result: dict[str, Any],
    *,
    capabilities: list[str],
) -> dict[str, Any]:
    transcript = provider_result.get("transcript")
    raw_segments = transcript.get("segments") if isinstance(transcript, dict) else []
    segments = raw_segments if isinstance(raw_segments, list) else []
    asr_segments = [
        {
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "text": segment["text"],
            "confidence": segment["confidence"],
        }
        for segment in segments
        if str(segment.get("text") or "").strip()
    ]
    vad_segments = [
        {
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "confidence": segment["confidence"],
        }
        for segment in segments
    ]
    speaker_turns = [
        {
            "start_ms": segment["start_ms"],
            "end_ms": segment["end_ms"],
            "speaker": segment["speaker"],
            "confidence": segment["confidence"],
        }
        for segment in segments
        if str(segment.get("speaker") or "").strip()
    ]
    outputs = {
        "vad": ("vad_segments", vad_segments, "provider_returned_no_speech_segments"),
        "asr": ("asr_segments", asr_segments, "provider_returned_no_transcript_segments"),
        "diarization": (
            "speaker_turns",
            speaker_turns,
            "provider_returned_no_speaker_turns",
        ),
    }
    result: dict[str, Any] = {
        "audio_session_id": record.payload.get("audio_session_id"),
        "recording_id": record.payload.get("recording_id"),
        "capability_statuses": {},
    }
    if "review_outputs" in provider_result:
        result["review_outputs"] = provider_result["review_outputs"]
    for capability in capabilities:
        if capability in outputs:
            output_key, items, empty_reason = outputs[capability]
            result[output_key] = items
            result["capability_statuses"][capability] = (
                {"status": "success"} if items else {"status": "no_content", "reason": empty_reason}
            )
        elif capability in {"voiceprint", "quality"}:
            result["capability_statuses"][capability] = {
                "status": "no_content",
                "reason": f"provider_protocol_has_no_structured_{capability}_output",
            }
    return result


def resolve_audio_intelligence_result(
    record: RunRecord,
    raw_result_ref: object,
) -> dict[str, Any]:
    """Resolve a v1 immutable manifest receipt or validate the legacy inline result."""

    if not isinstance(raw_result_ref, dict):
        return validate_audio_intelligence_result(record, raw_result_ref)
    manifest_version = raw_result_ref.get("manifest_version")
    if manifest_version is None:
        return validate_audio_intelligence_result(record, raw_result_ref)
    if manifest_version != AUDIO_RESULT_RECEIPT_SCHEMA:
        raise _audio_manifest_error(
            "RECEIPT_SCHEMA_INVALID",
            "音频结果回执 schema 无效",
            status_code=422,
        )
    receipt = _manifest_mapping(
        raw_result_ref,
        fields=_AUDIO_RESULT_RECEIPT_FIELDS,
        name="音频结果回执",
    )
    if (
        receipt.get("status") != "materialized"
        or receipt.get("result_manifest_schema") != AUDIO_RESULT_MANIFEST_SCHEMA
    ):
        raise _audio_manifest_error("RECEIPT_INVALID", "音频结果回执状态或 schema 无效")
    for field in (
        "result_manifest_sha256",
        "result_manifest_object_key_sha256",
        "result_manifest_version_id_sha256",
        "provider_request_sha256",
        "provider_response_sha256",
        "provider_result_sha256",
        "execution_envelope_sha256",
        "input_integrity_manifest_sha256",
        "inference_binding_sha256",
    ):
        _manifest_sha256(receipt.get(field), name=field)

    payload = record.payload if isinstance(record.payload, dict) else {}
    if payload.get("hotword_pack_version_id") or payload.get("return_word_timestamps") is True:
        raise _audio_manifest_error(
            "EVIDENCE_UNSUPPORTED",
            "当前 provider 结果协议不能证明受治理热词或词级时间戳产物",
            status_code=422,
        )
    capabilities, input_object, bindings = _validate_audio_manifest_receipt_bindings(
        record,
        receipt,
    )
    descriptor = _validate_audio_manifest_descriptor(record, receipt)
    body = _read_exact_audio_result_manifest(descriptor)
    manifest_sha256 = hashlib.sha256(body).hexdigest()
    if not _hash_matches(manifest_sha256, str(receipt["result_manifest_sha256"])):
        raise _audio_manifest_error("HASH_MISMATCH", "音频结果 manifest 回执哈希不一致")
    manifest = _decode_audio_result_manifest(body)
    provider_result = _validate_audio_result_manifest(
        record,
        receipt,
        manifest,
        capabilities=capabilities,
        input_object=input_object,
        bindings=bindings,
    )
    domain_result = _audio_domain_result_from_provider(
        record,
        provider_result,
        capabilities=capabilities,
    )
    return validate_audio_intelligence_result(record, domain_result)


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
    if "review_outputs" in result_ref:
        result_ref["review_outputs"] = _validate_audio_review_outputs(
            result_ref.get("review_outputs"),
            error_factory=lambda message: ApiError(
                "AUDIO_REVIEW_OUTPUT_INVALID",
                message,
                422,
            ),
        )
    return result_ref


def sanitize_audio_intelligence_result(result_ref: dict[str, Any]) -> dict[str, Any]:
    sanitized = {
        key: value for key, value in result_ref.items() if key not in _AUDIO_RAW_LOCATOR_FIELDS
    }
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
    *,
    validated_result_ref: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    payload = record.payload if isinstance(record.payload, dict) else {}
    result_ref = (
        validate_audio_intelligence_result(
            record,
            completion_receipt.get("result_ref"),
        )
        if validated_result_ref is None
        else validate_audio_intelligence_result(record, validated_result_ref)
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
    from app.services.audio_evidence_review_service import (
        materialize_audio_evidence_review,
    )

    materialized.extend(
        materialize_audio_evidence_review(
            session,
            ctx,
            record,
            result_ref,
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
    # Keep this dependency local: resource_service reaches label lifecycle
    # projections that import run_service, while run_service owns completion
    # orchestration and imports this module.
    from app.services.resource_service import upsert_resource

    payload = record.payload if isinstance(record.payload, dict) else {}
    root_trace_id = str(payload.get("root_trace_id") or record.trace_id)
    rooted_ctx = (
        ctx
        if ctx.trace_id == root_trace_id
        else replace(
            ctx,
            trace_id=root_trace_id,
            parent_trace_id=record.trace_id,
            correlation_id=root_trace_id,
        )
    )
    resource_data = {
        "id": resource_key,
        **data,
        "status": status,
        "source_run_id": record.run_id,
        "root_trace_id": root_trace_id,
        "current_trace_id": record.trace_id,
        "trace_id": root_trace_id,
    }
    upsert_resource(
        session,
        rooted_ctx,
        collection,
        resource_key,
        resource_data,
        status=status,
        trace_id=root_trace_id,
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
