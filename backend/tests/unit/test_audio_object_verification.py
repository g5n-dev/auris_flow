from __future__ import annotations

import base64
import hashlib
import struct

import pytest

from app.core.errors import ApiError
from app.services.audio_object_verification import (
    AUDIO_CHECKSUM_STREAM_CHUNK_BYTES,
    AUDIO_CHECKSUM_STREAM_TIMEOUT_SECONDS,
    MAX_AUDIO_RECORDING_BYTES,
    MAX_STREAMED_AUDIO_CHECKSUM_BYTES,
    provider_verified_sha256,
    stream_verified_sha256,
    verify_remote_audio_object,
)


def _wav(data: bytes = b"\x00\x00" * 8) -> bytes:
    return b"".join(
        [
            b"RIFF",
            struct.pack("<I", 36 + len(data)),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, 1, 16_000, 32_000, 2, 16),
            b"data",
            struct.pack("<I", len(data)),
            data,
        ]
    )


def _remote(body: bytes) -> dict[str, object]:
    digest = hashlib.sha256(body).digest()
    return {
        "content_length": str(len(body)),
        "content_type": "audio/wav",
        "etag": "strong-etag",
        "headers": {"X-Amz-Checksum-Sha256": base64.b64encode(digest).decode("ascii")},
    }


def test_remote_audio_verification_binds_provider_checksum_and_wav_structure() -> None:
    body = _wav()
    digest = hashlib.sha256(body).hexdigest()

    verification = verify_remote_audio_object(
        _remote(body),
        declared_content_length=len(body),
        declared_sha256=digest,
        wav_prefix=body,
    )

    assert verification["checksum_sha256"] == digest
    assert verification["checksum_verified"] is True
    assert verification["wav_verified"] is True
    assert verification["wav"]["sample_rate"] == 16_000


def test_only_provider_checksum_header_is_trusted() -> None:
    digest = "a" * 64
    assert provider_verified_sha256({"headers": {"x-amz-meta-sha256": digest}}) is None
    assert provider_verified_sha256({"checksum_sha256": digest}) == digest


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda remote, body: remote.update(content_type="application/octet-stream"),
            "AUDIO_OBJECT_CONTENT_TYPE_MISMATCH",
        ),
        (
            lambda remote, body: remote.update(headers={}),
            "AUDIO_OBJECT_CHECKSUM_UNAVAILABLE",
        ),
        (
            lambda remote, body: remote.update(checksum_sha256="0" * 64),
            "AUDIO_OBJECT_CHECKSUM_MISMATCH",
        ),
        (
            lambda remote, body: remote.update(content_length=str(len(body) + 1)),
            "AUDIO_OBJECT_SIZE_MISMATCH",
        ),
    ],
)
def test_remote_audio_verification_rejects_untrusted_metadata(mutate, code: str) -> None:
    body = _wav()
    remote = _remote(body)
    mutate(remote, body)
    with pytest.raises(ApiError) as captured:
        verify_remote_audio_object(
            remote,
            declared_content_length=len(body),
            declared_sha256=hashlib.sha256(body).hexdigest(),
            wav_prefix=body,
        )
    assert captured.value.code == code


@pytest.mark.parametrize(
    "body",
    [
        b"not-a-wav" + b"\x00" * 64,
        _wav().replace(b"fmt ", b"JUNK", 1),
    ],
)
def test_remote_audio_verification_rejects_invalid_wav(body: bytes) -> None:
    remote = _remote(body)
    with pytest.raises(ApiError) as captured:
        verify_remote_audio_object(
            remote,
            declared_content_length=len(body),
            declared_sha256=hashlib.sha256(body).hexdigest(),
            wav_prefix=body,
        )
    assert captured.value.code == "AUDIO_OBJECT_WAV_INVALID"


def test_remote_audio_verification_rejects_inconsistent_pcm_frame_geometry() -> None:
    body = bytearray(_wav())
    struct.pack_into("<I", body, 28, 64_000)
    struct.pack_into("<H", body, 32, 4)
    remote = _remote(bytes(body))

    with pytest.raises(ApiError) as captured:
        verify_remote_audio_object(
            remote,
            declared_content_length=len(body),
            declared_sha256=hashlib.sha256(body).hexdigest(),
            wav_prefix=bytes(body),
        )

    assert captured.value.code == "AUDIO_OBJECT_WAV_INVALID"


def test_audio_recording_limit_is_gib_not_tib() -> None:
    assert MAX_AUDIO_RECORDING_BYTES == 5 * 1024**3


def test_versioned_checksum_stream_is_chunked_and_complete() -> None:
    body = b"a" * (AUDIO_CHECKSUM_STREAM_CHUNK_BYTES * 2 + 17)

    class RecordingStream:
        def __init__(self) -> None:
            self.offset = 0
            self.read_sizes: list[int] = []

        def read(self, size: int) -> bytes:
            self.read_sizes.append(size)
            chunk = body[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    stream = RecordingStream()

    digest = stream_verified_sha256(stream, expected_size=len(body))

    assert digest == hashlib.sha256(body).hexdigest()
    assert stream.read_sizes[-1] == 1
    assert max(stream.read_sizes[:-1]) <= AUDIO_CHECKSUM_STREAM_CHUNK_BYTES


def test_versioned_checksum_stream_rejects_objects_above_fallback_limit() -> None:
    class UnexpectedRead:
        def read(self, _size: int) -> bytes:
            raise AssertionError("oversized objects must be rejected before streaming")

    with pytest.raises(ApiError) as captured:
        stream_verified_sha256(
            UnexpectedRead(),
            expected_size=MAX_STREAMED_AUDIO_CHECKSUM_BYTES + 1,
        )

    assert captured.value.code == "AUDIO_OBJECT_CHECKSUM_STREAM_LIMIT_EXCEEDED"


def test_versioned_checksum_stream_has_an_end_to_end_deadline() -> None:
    timestamps = iter((0.0, AUDIO_CHECKSUM_STREAM_TIMEOUT_SECONDS + 1.0))

    with pytest.raises(ApiError) as captured:
        stream_verified_sha256(
            BytesReader(b"audio"),
            expected_size=5,
            clock=lambda: next(timestamps),
        )

    assert captured.value.code == "AUDIO_OBJECT_CHECKSUM_TIMEOUT"
    assert captured.value.retryable is True


class BytesReader:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.offset = 0

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk
