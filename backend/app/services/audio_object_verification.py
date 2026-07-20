from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import time
from collections.abc import Callable, Mapping
from typing import Any

from app.core.errors import ApiError

MAX_AUDIO_RECORDING_BYTES = 5 * 1024**3
MAX_WAV_PROBE_BYTES = 64 * 1024
MAX_STREAMED_AUDIO_CHECKSUM_BYTES = 512 * 1024**2
AUDIO_CHECKSUM_STREAM_CHUNK_BYTES = 1024 * 1024
AUDIO_CHECKSUM_STREAM_TIMEOUT_SECONDS = 30.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WAV_CONTENT_TYPES = frozenset({"audio/wav", "audio/x-wav"})
_WAV_FORMATS = frozenset({1, 3, 0xFFFE})
_WAVE_SUBFORMAT_SUFFIX = bytes.fromhex("00001000800000aa00389b71")


def _header_value(headers: object, name: str) -> str | None:
    if not isinstance(headers, Mapping):
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered and isinstance(value, str):
            normalized = value.strip()
            return normalized or None
    return None


def _sha256_hex(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    if _SHA256.fullmatch(normalized):
        return normalized
    try:
        decoded = base64.b64decode(value.strip(), validate=True)
    except (ValueError, binascii.Error):
        return None
    return decoded.hex() if len(decoded) == 32 else None


def provider_verified_sha256(remote: Mapping[str, Any]) -> str | None:
    """Return only a provider checksum, never caller-controlled object metadata."""

    direct = _sha256_hex(remote.get("checksum_sha256"))
    if direct:
        return direct
    return _sha256_hex(_header_value(remote.get("headers"), "x-amz-checksum-sha256"))


def require_streamed_checksum_size(expected_size: int) -> None:
    if expected_size > MAX_STREAMED_AUDIO_CHECKSUM_BYTES:
        raise ApiError(
            "AUDIO_OBJECT_CHECKSUM_STREAM_LIMIT_EXCEEDED",
            "对象存储未提供 SHA-256，且录音过大，无法在登记请求内安全流式校验",
            409,
            retryable=False,
        )


def stream_verified_sha256(
    stream: Any,
    *,
    expected_size: int,
    timeout_seconds: float = AUDIO_CHECKSUM_STREAM_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Hash an object stream with hard byte, chunk and elapsed-time bounds."""

    if expected_size < 0:
        raise ValueError("expected checksum stream size must not be negative")
    if timeout_seconds <= 0:
        raise ValueError("checksum stream timeout must be positive")
    require_streamed_checksum_size(expected_size)
    read = getattr(stream, "read", None)
    if not callable(read):
        raise ApiError(
            "AUDIO_OBJECT_VERIFY_FAILED",
            "对象存储未返回可读取的录音数据流",
            502,
            retryable=True,
        )

    deadline = clock() + timeout_seconds
    digest = hashlib.sha256()
    total = 0

    def ensure_deadline() -> None:
        if clock() >= deadline:
            raise ApiError(
                "AUDIO_OBJECT_CHECKSUM_TIMEOUT",
                "录音对象完整性校验超时",
                504,
                retryable=True,
            )

    while total < expected_size:
        ensure_deadline()
        remaining = expected_size - total
        try:
            chunk = read(min(AUDIO_CHECKSUM_STREAM_CHUNK_BYTES, remaining))
        except TimeoutError as exc:
            raise ApiError(
                "AUDIO_OBJECT_CHECKSUM_TIMEOUT",
                "录音对象完整性校验超时",
                504,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "读取录音对象进行完整性校验时失败",
                502,
                retryable=True,
            ) from exc
        if not isinstance(chunk, bytes):
            raise ApiError(
                "AUDIO_OBJECT_VERIFY_FAILED",
                "对象存储返回了无效的录音数据流",
                502,
                retryable=True,
            )
        if not chunk:
            raise ApiError(
                "AUDIO_OBJECT_SIZE_MISMATCH",
                "对象存储返回的录音字节数少于登记大小",
                409,
                retryable=False,
            )
        if len(chunk) > remaining:
            raise ApiError(
                "AUDIO_OBJECT_SIZE_MISMATCH",
                "对象存储返回的录音字节数超过登记大小",
                409,
                retryable=False,
            )
        total += len(chunk)
        digest.update(chunk)

    ensure_deadline()
    try:
        trailing = read(1)
    except TimeoutError as exc:
        raise ApiError(
            "AUDIO_OBJECT_CHECKSUM_TIMEOUT",
            "录音对象完整性校验超时",
            504,
            retryable=True,
        ) from exc
    except OSError as exc:
        raise ApiError(
            "AUDIO_OBJECT_VERIFY_FAILED",
            "读取录音对象进行完整性校验时失败",
            502,
            retryable=True,
        ) from exc
    if not isinstance(trailing, bytes):
        raise ApiError(
            "AUDIO_OBJECT_VERIFY_FAILED",
            "对象存储返回了无效的录音数据流",
            502,
            retryable=True,
        )
    if trailing:
        raise ApiError(
            "AUDIO_OBJECT_SIZE_MISMATCH",
            "对象存储返回的录音字节数超过登记大小",
            409,
            retryable=False,
        )
    return digest.hexdigest()


def validate_wav_probe(prefix: bytes, *, content_length: int) -> dict[str, int]:
    if content_length < 44 or len(prefix) < min(content_length, 44):
        raise ApiError("AUDIO_OBJECT_WAV_INVALID", "录音对象不是完整的 WAV 文件", 409)
    if prefix[:4] != b"RIFF" or prefix[8:12] != b"WAVE":
        raise ApiError("AUDIO_OBJECT_WAV_INVALID", "录音对象缺少 RIFF/WAVE 头", 409)
    riff_size = struct.unpack_from("<I", prefix, 4)[0]
    if riff_size + 8 != content_length:
        raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV RIFF 长度与对象大小不一致", 409)

    offset = 12
    fmt: tuple[int, int, int, int, int] | None = None
    data_size: int | None = None
    while offset + 8 <= len(prefix):
        chunk_id = prefix[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", prefix, offset + 4)[0]
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > content_length:
            raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV chunk 越过对象边界", 409)
        if chunk_id == b"fmt ":
            if chunk_size < 16 or chunk_end > len(prefix):
                raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV fmt chunk 不完整", 409)
            audio_format, channels, sample_rate, byte_rate, block_align, bits = struct.unpack_from(
                "<HHIIHH", prefix, chunk_start
            )
            effective_format = audio_format
            if audio_format == 0xFFFE:
                if chunk_size < 40:
                    raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV extensible fmt 不完整", 409)
                extension_size, valid_bits = struct.unpack_from("<HH", prefix, chunk_start + 16)
                subformat = prefix[chunk_start + 24 : chunk_start + 40]
                if (
                    extension_size < 22
                    or 18 + extension_size > chunk_size
                    or not 1 <= valid_bits <= bits
                    or len(subformat) != 16
                    or subformat[4:] != _WAVE_SUBFORMAT_SUFFIX
                ):
                    raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV extensible 参数无效", 409)
                effective_format = struct.unpack_from("<I", subformat)[0]
            if (
                audio_format not in _WAV_FORMATS
                or effective_format not in {1, 3}
                or not 1 <= channels <= 32
                or not 8_000 <= sample_rate <= 384_000
                or bits not in {8, 16, 24, 32, 64}
                or (effective_format == 3 and bits not in {32, 64})
                or block_align <= 0
                or block_align != channels * bits // 8
                or byte_rate != sample_rate * block_align
            ):
                raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV 音频格式参数不受支持", 409)
            fmt = (audio_format, channels, sample_rate, bits, block_align)
        elif chunk_id == b"data":
            data_size = chunk_size
            break
        if chunk_end > len(prefix):
            break
        offset = chunk_end + (chunk_size % 2)

    if fmt is None or data_size is None:
        raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV 缺少可验证的 fmt 或 data chunk", 409)
    audio_format, channels, sample_rate, bits, block_align = fmt
    if data_size % block_align:
        raise ApiError("AUDIO_OBJECT_WAV_INVALID", "WAV data chunk 未按 frame 对齐", 409)
    return {
        "audio_format": audio_format,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "data_bytes": data_size,
    }


def verify_remote_audio_object(
    remote: Mapping[str, Any],
    *,
    declared_content_length: int,
    declared_sha256: str,
    wav_prefix: bytes,
    declared_content_type: str | None = None,
    verified_sha256: str | None = None,
    verified_checksum_method: str | None = None,
) -> dict[str, Any]:
    try:
        remote_size = int(remote.get("content_length") or 0)
    except (TypeError, ValueError) as exc:
        raise ApiError("AUDIO_OBJECT_METADATA_INVALID", "对象存储缺少有效大小", 409) from exc
    if remote_size != declared_content_length:
        raise ApiError("AUDIO_OBJECT_SIZE_MISMATCH", "登记的录音大小与对象存储不一致", 409)
    if not 44 <= remote_size <= MAX_AUDIO_RECORDING_BYTES:
        raise ApiError("AUDIO_OBJECT_SIZE_INVALID", "录音对象大小超出允许范围", 409)

    content_type = str(remote.get("content_type") or "").partition(";")[0].strip().lower()
    if content_type not in _WAV_CONTENT_TYPES:
        raise ApiError("AUDIO_OBJECT_CONTENT_TYPE_MISMATCH", "对象存储内容类型不是 WAV", 409)
    normalized_declared_type = (
        declared_content_type.partition(";")[0].strip().lower()
        if declared_content_type is not None
        else None
    )
    if normalized_declared_type is not None and content_type != normalized_declared_type:
        raise ApiError(
            "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH",
            "登记的录音类型与对象存储不一致",
            409,
        )
    provider_sha256 = provider_verified_sha256(remote)
    remote_sha256 = provider_sha256 or _sha256_hex(verified_sha256)
    if remote_sha256 is None:
        raise ApiError(
            "AUDIO_OBJECT_CHECKSUM_UNAVAILABLE",
            "对象存储未返回由 Provider 验证的 SHA-256",
            409,
        )
    if remote_sha256 != declared_sha256.lower():
        raise ApiError("AUDIO_OBJECT_CHECKSUM_MISMATCH", "录音 SHA-256 与对象存储不一致", 409)
    wav = validate_wav_probe(wav_prefix, content_length=remote_size)
    checksum_method = (
        "provider_checksum"
        if provider_sha256 is not None
        else verified_checksum_method or "versioned_stream"
    )
    return {
        "mode": (
            "provider_head_and_range"
            if provider_sha256 is not None
            else f"provider_head_range_and_{checksum_method}"
        ),
        "verified": True,
        "content_length": remote_size,
        "content_type": content_type,
        "checksum_sha256": remote_sha256,
        "checksum_verified": True,
        "checksum_method": checksum_method,
        "wav_verified": True,
        "wav": wav,
    }
