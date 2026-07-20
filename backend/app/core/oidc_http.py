"""Bounded HTTP response helpers for untrusted OIDC provider documents."""

from __future__ import annotations

import httpx


class OIDCHTTPResponseLimitError(RuntimeError):
    """An OIDC response cannot be read safely within its configured bound."""


def read_bounded_httpx_body(
    response: httpx.Response,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read decoded response chunks without ever growing the buffer past the limit."""

    if isinstance(maximum_bytes, bool) or not isinstance(maximum_bytes, int):
        raise ValueError("maximum_bytes must be an integer")
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")

    declared_length = response.headers.get("content-length")
    if declared_length is not None:
        if not declared_length.isascii() or not declared_length.isdecimal():
            raise OIDCHTTPResponseLimitError
        if int(declared_length) > maximum_bytes:
            raise OIDCHTTPResponseLimitError

    body = bytearray()
    for chunk in response.iter_bytes():
        if not isinstance(chunk, bytes) or len(chunk) > maximum_bytes - len(body):
            raise OIDCHTTPResponseLimitError
        body.extend(chunk)
    return bytes(body)
