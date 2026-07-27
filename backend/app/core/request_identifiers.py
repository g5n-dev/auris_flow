from __future__ import annotations

import re
import uuid
from typing import TypeGuard

REQUEST_IDENTIFIER_PATTERN_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
REQUEST_IDENTIFIER_PATTERN = re.compile(REQUEST_IDENTIFIER_PATTERN_TEXT)
MAX_REQUEST_IDENTIFIER_LENGTH = 128
_LOWER_HEX_PATTERN = re.compile(r"^[0-9a-f]+$")
_PUBLIC_IDENTIFIER_HEX_TO_ALPHA = str.maketrans(
    "0123456789abcdef",
    "abcdefghijklmnop",
)


def is_safe_request_identifier(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and REQUEST_IDENTIFIER_PATTERN.fullmatch(value) is not None


def sanitized_request_id(value: str | None) -> str:
    """Return a bounded caller request ID or replace it with a server-generated ID."""

    if is_safe_request_identifier(value):
        return value
    return server_generated_public_id("request", suffix_length=32)


def public_suffix_from_hex(
    hex_value: str,
    *,
    suffix_length: int,
) -> str:
    """Encode trusted hexadecimal entropy without decimal runs."""

    if (
        not isinstance(hex_value, str)
        or _LOWER_HEX_PATTERN.fullmatch(hex_value) is None
        or isinstance(suffix_length, bool)
        or not isinstance(suffix_length, int)
        or not 1 <= suffix_length <= len(hex_value)
    ):
        raise ValueError("invalid public identifier suffix configuration")
    return hex_value[:suffix_length].translate(_PUBLIC_IDENTIFIER_HEX_TO_ALPHA)


def public_id_from_hex(
    prefix: str,
    hex_value: str,
    *,
    suffix_length: int,
    separator: str = "_",
) -> str:
    """Encode trusted hexadecimal entropy as a PII-safe public identifier."""

    if (
        not isinstance(prefix, str)
        or not prefix
        or REQUEST_IDENTIFIER_PATTERN.fullmatch(prefix) is None
        or separator not in {"_", "-"}
    ):
        raise ValueError("invalid public identifier encoding configuration")
    suffix = public_suffix_from_hex(hex_value, suffix_length=suffix_length)
    generated = f"{prefix}{separator}{suffix}"
    if REQUEST_IDENTIFIER_PATTERN.fullmatch(generated) is None:
        raise ValueError("encoded public identifier exceeds the public contract")
    return generated


def server_generated_public_id(
    prefix: str,
    *,
    suffix_length: int,
    separator: str = "_",
) -> str:
    """Return an opaque ID whose random suffix cannot resemble a phone number."""

    return public_id_from_hex(
        prefix,
        uuid.uuid4().hex,
        suffix_length=suffix_length,
        separator=separator,
    )


def server_generated_public_suffix(*, suffix_length: int) -> str:
    """Return random entropy suitable for composing correlated public IDs."""

    return public_suffix_from_hex(uuid.uuid4().hex, suffix_length=suffix_length)


def safe_idempotency_key_for_response(value: str | None) -> str | None:
    """Never reflect an invalid or oversized idempotency key in an error envelope."""

    return value if is_safe_request_identifier(value) else None
