from __future__ import annotations

import httpx
import pytest

from app.core.oidc_http import OIDCHTTPResponseLimitError, read_bounded_httpx_body


class RecordingByteStream(httpx.SyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.chunks_seen = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.chunks_seen += 1
            yield chunk


def _response(
    stream: RecordingByteStream,
    *,
    content_length: int | None = None,
) -> httpx.Response:
    headers = {}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    return httpx.Response(
        200,
        headers=headers,
        stream=stream,
        request=httpx.Request("GET", "https://identity.example.test/oidc-document"),
    )


def test_bounded_reader_returns_a_complete_response_within_the_limit() -> None:
    stream = RecordingByteStream(b'{"issuer":', b'"https://identity.example.test"}')
    response = _response(stream)

    try:
        body = read_bounded_httpx_body(response, maximum_bytes=128 * 1024)
    finally:
        response.close()

    assert body == b'{"issuer":"https://identity.example.test"}'
    assert stream.chunks_seen == 2


def test_bounded_reader_rejects_declared_oversize_before_consuming_the_stream() -> None:
    stream = RecordingByteStream(b"must-not-be-consumed")
    response = _response(stream, content_length=9)

    try:
        with pytest.raises(OIDCHTTPResponseLimitError):
            read_bounded_httpx_body(response, maximum_bytes=8)
    finally:
        response.close()

    assert stream.chunks_seen == 0


def test_bounded_reader_stops_at_the_first_chunk_crossing_the_limit() -> None:
    stream = RecordingByteStream(b"12345678", b"9", b"sensitive-tail")
    response = _response(stream)

    try:
        with pytest.raises(OIDCHTTPResponseLimitError):
            read_bounded_httpx_body(response, maximum_bytes=8)
    finally:
        response.close()

    assert stream.chunks_seen == 2
