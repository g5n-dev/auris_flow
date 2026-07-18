from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request

from app.core.config import Settings, get_settings
from app.core.errors import ApiError

SIGNATURE_VERSION = "auris-completion-v1"
SIGNATURE_HEADER = "X-Auris-Signature"
TIMESTAMP_HEADER = "X-Auris-Timestamp"
NONCE_HEADER = "X-Auris-Nonce"
KEY_ID_HEADER = "X-Auris-Key-Id"
SIGNATURE_ID_HEADER = "X-Auris-Signature-Id"
SOURCE_HEADER = "X-Auris-Source"
SIGNATURE_MODE_HEADER = "X-Auris-Signature-Mode"
HMAC_SIGNATURE_MODE = "hmac-sha256"
LOCAL_COMPLETION_ENVS = frozenset({"local", "test", "ci"})


@dataclass(frozen=True)
class CompletionKeyBinding:
    key_id: str
    secret: str
    allowed_sources: tuple[str, ...]
    allowed_scopes: tuple[tuple[str, str], ...] | None
    binding_mode: str


@dataclass(frozen=True)
class CompletionSignatureVerification:
    key_id: str
    source: str
    tenant_id: str
    project_id: str
    timestamp: str
    nonce: str
    body_sha256: str
    request_sha256: str
    signature_mode: str
    binding_mode: str


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_timestamp(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("COMPLETION_SIGNATURE_INVALID_TIMESTAMP", "完成回执时间戳无效", 401) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _signature_value(raw: str) -> str:
    value = raw.strip()
    if value.startswith("sha256="):
        value = value.removeprefix("sha256=").strip()
    if len(value) != 64:
        raise ApiError("COMPLETION_SIGNATURE_INVALID", "完成回执签名无效", 401)
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ApiError("COMPLETION_SIGNATURE_INVALID", "完成回执签名无效", 401) from exc
    return value


def completion_signature_message(
    *,
    method: str,
    path: str,
    query: str,
    tenant_id: str,
    project_id: str,
    idempotency_key: str,
    timestamp: str,
    nonce: str,
    key_id: str,
    source: str,
    body_sha256: str,
) -> str:
    return "\n".join(
        [
            SIGNATURE_VERSION,
            method.upper(),
            path,
            query,
            tenant_id,
            project_id,
            idempotency_key,
            timestamp,
            nonce,
            key_id,
            source,
            body_sha256,
        ]
    )


def _configuration_error(message: str) -> ApiError:
    return ApiError(
        "COMPLETION_SIGNATURE_KEY_BINDINGS_INVALID",
        message,
        500,
    )


def _binding_string_list(value: object, *, key_id: str, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _configuration_error(f"完成回执 key {key_id} 的 {field} 必须是非空数组")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item.strip() == "*":
            raise _configuration_error(f"完成回执 key {key_id} 的 {field} 必须使用明确值")
        normalized.append(item.strip())
    if len(normalized) != len(set(normalized)):
        raise _configuration_error(f"完成回执 key {key_id} 的 {field} 不能重复")
    return tuple(normalized)


def _binding_scopes(value: object, *, key_id: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise _configuration_error(f"完成回执 key {key_id} 的 allowed_scopes 必须是非空数组")
    scopes: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _configuration_error(f"完成回执 key {key_id} 的 allowed_scopes 配置无效")
        tenant_id = item.get("tenant_id")
        project_id = item.get("project_id")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id.strip()
            or tenant_id.strip() == "*"
            or not isinstance(project_id, str)
            or not project_id.strip()
            or project_id.strip() == "*"
        ):
            raise _configuration_error(f"完成回执 key {key_id} 必须绑定明确的 tenant_id/project_id")
        scopes.append((tenant_id.strip(), project_id.strip()))
    if len(scopes) != len(set(scopes)):
        raise _configuration_error(f"完成回执 key {key_id} 的 allowed_scopes 不能重复")
    return tuple(scopes)


def _parse_key_bindings(settings: Settings) -> dict[str, CompletionKeyBinding]:
    raw = settings.completion_receipt_key_bindings.strip()
    try:
        configured = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _configuration_error("完成回执 key-id 映射不是有效 JSON") from exc
    if not isinstance(configured, dict) or not configured:
        raise _configuration_error("完成回执 key-id 映射必须是非空对象")

    bindings: dict[str, CompletionKeyBinding] = {}
    for raw_key_id, value in configured.items():
        if not isinstance(raw_key_id, str) or not raw_key_id.strip():
            raise _configuration_error("完成回执 key-id 必须是非空字符串")
        key_id = raw_key_id.strip()
        if len(key_id) > 128 or key_id in bindings:
            raise _configuration_error("完成回执 key-id 无效或重复")
        if not isinstance(value, dict):
            raise _configuration_error(f"完成回执 key {key_id} 的配置必须是对象")
        secret = value.get("secret")
        if not isinstance(secret, str) or len(secret.strip()) < 32:
            raise _configuration_error(f"完成回执 key {key_id} 的密钥未配置或长度不足")
        bindings[key_id] = CompletionKeyBinding(
            key_id=key_id,
            secret=secret.strip(),
            allowed_sources=_binding_string_list(
                value.get("allowed_sources"),
                key_id=key_id,
                field="allowed_sources",
            ),
            allowed_scopes=_binding_scopes(value.get("allowed_scopes"), key_id=key_id),
            binding_mode="scoped_key_map",
        )
    return bindings


def _legacy_key_binding(settings: Settings) -> CompletionKeyBinding:
    if settings.app_env.strip().lower() not in LOCAL_COMPLETION_ENVS:
        raise ApiError(
            "COMPLETION_SIGNATURE_KEY_BINDINGS_REQUIRED",
            "生产环境必须配置带来源和租户/项目范围的完成回执 key-id 映射",
            500,
        )
    secret = settings.completion_receipt_secret.strip()
    if len(secret) < 32:
        raise ApiError(
            "COMPLETION_SIGNATURE_NOT_CONFIGURED",
            "完成回执签名密钥未配置或长度不足",
            500,
        )
    key_id = settings.completion_receipt_signature_id.strip()
    allowed_sources = _csv(settings.completion_receipt_allowed_sources)
    if not key_id or len(key_id) > 128 or not allowed_sources:
        raise ApiError(
            "COMPLETION_SIGNATURE_NOT_CONFIGURED",
            "本地完成回执单 key 配置无效",
            500,
        )
    return CompletionKeyBinding(
        key_id=key_id,
        secret=secret,
        allowed_sources=allowed_sources,
        allowed_scopes=None,
        binding_mode="legacy_local_single_key",
    )


def _resolve_key_binding(settings: Settings, key_id: str) -> CompletionKeyBinding:
    if settings.completion_receipt_key_bindings.strip():
        binding = _parse_key_bindings(settings).get(key_id)
    else:
        legacy = _legacy_key_binding(settings)
        binding = legacy if hmac.compare_digest(key_id, legacy.key_id) else None
    if binding is None:
        raise ApiError("COMPLETION_SIGNATURE_KEY_DENIED", "完成回执签名 key 不被允许", 403)
    return binding


async def verify_completion_signature(
    request: Request,
    *,
    tenant_id: str,
    project_id: str,
    idempotency_key: str,
    settings: Settings | None = None,
) -> CompletionSignatureVerification:
    settings = settings or get_settings()
    key_id = (
        request.headers.get(KEY_ID_HEADER) or request.headers.get(SIGNATURE_ID_HEADER) or ""
    ).strip()
    timestamp = (request.headers.get(TIMESTAMP_HEADER) or "").strip()
    nonce = (request.headers.get(NONCE_HEADER) or "").strip()
    source = (request.headers.get(SOURCE_HEADER) or "").strip()
    raw_signature = (request.headers.get(SIGNATURE_HEADER) or "").strip()
    signature_mode = (request.headers.get(SIGNATURE_MODE_HEADER) or HMAC_SIGNATURE_MODE).strip()
    if not key_id or not timestamp or not nonce or not source or not raw_signature:
        raise ApiError(
            "COMPLETION_SIGNATURE_REQUIRED",
            "外部完成回执必须提供签名头",
            401,
        )
    if len(key_id) > 128:
        raise ApiError("COMPLETION_SIGNATURE_KEY_INVALID", "完成回执签名 key-id 无效", 401)
    if len(source) > 128:
        raise ApiError("COMPLETION_SIGNATURE_SOURCE_INVALID", "完成回执来源无效", 401)
    if len(nonce) > 128:
        raise ApiError("COMPLETION_SIGNATURE_NONCE_INVALID", "完成回执 nonce 过长", 401)
    if signature_mode.lower() != HMAC_SIGNATURE_MODE:
        raise ApiError(
            "COMPLETION_SIGNATURE_MODE_UNSUPPORTED",
            "完成回执签名模式不受支持",
            401,
        )
    binding = _resolve_key_binding(settings, key_id)
    signed_at = _parse_timestamp(timestamp)
    if abs(time.time() - signed_at) > settings.completion_receipt_signature_tolerance_seconds:
        raise ApiError("COMPLETION_SIGNATURE_EXPIRED", "完成回执签名已过期", 401)
    body = await request.body()
    body_sha256 = hashlib.sha256(body or b"").hexdigest()
    message = completion_signature_message(
        method=request.method,
        path=request.url.path,
        query=request.url.query,
        tenant_id=tenant_id,
        project_id=project_id,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
        nonce=nonce,
        key_id=key_id,
        source=source,
        body_sha256=body_sha256,
    )
    expected = hmac.new(
        binding.secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    )
    if not hmac.compare_digest(_signature_value(raw_signature), expected.hexdigest()):
        raise ApiError("COMPLETION_SIGNATURE_INVALID", "完成回执签名无效", 401)
    if source not in binding.allowed_sources:
        raise ApiError(
            "COMPLETION_SIGNATURE_SOURCE_DENIED",
            "完成回执来源不在签名 key 的授权范围内",
            403,
            details=[{"source": source, "allowed_sources": sorted(binding.allowed_sources)}],
        )
    if (
        binding.allowed_scopes is not None
        and (
            tenant_id,
            project_id,
        )
        not in binding.allowed_scopes
    ):
        raise ApiError(
            "COMPLETION_SIGNATURE_SCOPE_DENIED",
            "完成回执签名 key 不允许访问当前租户或项目",
            403,
            details=[{"tenant_id": tenant_id, "project_id": project_id}],
        )
    request_fingerprint = {
        "message": message,
        "signature_version": SIGNATURE_VERSION,
    }
    request_sha256 = hashlib.sha256(
        json.dumps(request_fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CompletionSignatureVerification(
        key_id=key_id,
        source=source,
        tenant_id=tenant_id,
        project_id=project_id,
        timestamp=timestamp,
        nonce=nonce,
        body_sha256=body_sha256,
        request_sha256=request_sha256,
        signature_mode=HMAC_SIGNATURE_MODE,
        binding_mode=binding.binding_mode,
    )
