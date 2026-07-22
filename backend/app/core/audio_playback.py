from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings
from app.core.errors import ApiError


@dataclass(frozen=True)
class AudioPlaybackGrant:
    tenant_id: str
    project_id: str
    user_id: str
    audio_session_id: str
    storage_object_id: str | None
    storage_provider: str | None
    object_version_id: str | None
    etag: str | None
    expires_at: int
    nonce: str
    auth_session_id: str | None = None


def _base64url_encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    return urlsafe_b64decode(f"{value}{'=' * (-len(value) % 4)}".encode("ascii"))


def _playback_secret(settings: Settings) -> str:
    secret = settings.audio_playback_grant_secret
    if not secret and settings.app_env in {"local", "test", "ci"}:
        secret = settings.auth_token_secret or settings.completion_receipt_secret
    if len(secret) < 32:
        raise ApiError(
            "AUDIO_PLAYBACK_GRANT_NOT_CONFIGURED",
            "音频播放授权签名密钥未配置或长度不足",
            500,
        )
    return secret


def _valid_optional_binding(value: object, *, maximum: int) -> bool:
    return value is None or (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= maximum
        and value.casefold() != "null"
        and not any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    )


def create_audio_playback_grant(
    settings: Settings,
    *,
    tenant_id: str,
    project_id: str,
    user_id: str,
    audio_session_id: str,
    auth_session_id: str | None = None,
    storage_object_id: str | None = None,
    storage_provider: str | None = None,
    object_version_id: str | None = None,
    etag: str | None = None,
    now: int | None = None,
) -> tuple[str, AudioPlaybackGrant]:
    if not _valid_optional_binding(object_version_id, maximum=1024):
        raise ApiError(
            "AUDIO_OBJECT_VERSION_ID_UNAVAILABLE",
            "录音对象版本标识无效，无法签发播放授权",
            409,
        )
    issued_at = int(time.time()) if now is None else now
    ttl_seconds = max(30, min(settings.audio_playback_grant_ttl_seconds, 900))
    expires_at = issued_at + ttl_seconds
    nonce = uuid.uuid4().hex
    payload = {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "user_id": user_id,
        "auth_session_id": auth_session_id,
        "audio_session_id": audio_session_id,
        "storage_object_id": storage_object_id,
        "storage_provider": storage_provider,
        "object_version_id": object_version_id,
        "etag": etag,
        "exp": expires_at,
        "nonce": nonce,
    }
    payload_part = _base64url_encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signing_input = f"apg.v1.{payload_part}".encode()
    signature = _base64url_encode(
        hmac.new(_playback_secret(settings).encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    grant = AudioPlaybackGrant(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        audio_session_id=audio_session_id,
        storage_object_id=storage_object_id,
        storage_provider=storage_provider,
        object_version_id=object_version_id,
        etag=etag,
        expires_at=expires_at,
        nonce=nonce,
        auth_session_id=auth_session_id,
    )
    return f"apg.v1.{payload_part}.{signature}", grant


def verify_audio_playback_grant(
    settings: Settings, token: str, *, now: int | None = None
) -> AudioPlaybackGrant:
    try:
        prefix, version, payload_part, signature = token.split(".", 3)
    except ValueError as exc:
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403) from exc
    if (prefix, version) != ("apg", "v1"):
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403)
    signing_input = f"{prefix}.{version}.{payload_part}".encode()
    expected = _base64url_encode(
        hmac.new(_playback_secret(settings).encode("utf-8"), signing_input, hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected):
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403)
    try:
        raw_payload: Any = json.loads(_base64url_decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403) from exc
    if not isinstance(raw_payload, dict):
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403)
    tenant_id = raw_payload.get("tenant_id")
    project_id = raw_payload.get("project_id")
    user_id = raw_payload.get("user_id")
    auth_session_id = raw_payload.get("auth_session_id")
    audio_session_id = raw_payload.get("audio_session_id")
    storage_object_id = raw_payload.get("storage_object_id")
    storage_provider = raw_payload.get("storage_provider")
    object_version_id = raw_payload.get("object_version_id")
    etag = raw_payload.get("etag")
    nonce = raw_payload.get("nonce")
    expires_at = raw_payload.get("exp")
    current_time = int(time.time()) if now is None else now
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(project_id, str)
        or not project_id
        or not isinstance(user_id, str)
        or not user_id
        or (auth_session_id is not None and not isinstance(auth_session_id, str))
        or not isinstance(audio_session_id, str)
        or not audio_session_id
        or (storage_object_id is not None and not isinstance(storage_object_id, str))
        or (storage_provider is not None and not isinstance(storage_provider, str))
        or not _valid_optional_binding(object_version_id, maximum=1024)
        or (etag is not None and not isinstance(etag, str))
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(expires_at, int)
        or expires_at <= current_time
    ):
        raise ApiError("AUDIO_PLAYBACK_GRANT_INVALID", "音频播放授权无效或已过期", 403)
    return AudioPlaybackGrant(
        tenant_id=tenant_id,
        project_id=project_id,
        user_id=user_id,
        audio_session_id=audio_session_id,
        storage_object_id=storage_object_id,
        storage_provider=storage_provider,
        object_version_id=object_version_id,
        etag=etag,
        expires_at=expires_at,
        nonce=nonce,
        auth_session_id=auth_session_id,
    )
