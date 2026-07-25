from __future__ import annotations

import re
import uuid
from typing import TypeGuard

REQUEST_IDENTIFIER_PATTERN_TEXT = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
REQUEST_IDENTIFIER_PATTERN = re.compile(REQUEST_IDENTIFIER_PATTERN_TEXT)
MAX_REQUEST_IDENTIFIER_LENGTH = 128


def is_safe_request_identifier(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and REQUEST_IDENTIFIER_PATTERN.fullmatch(value) is not None


def sanitized_request_id(value: str | None) -> str:
    """Return a bounded caller request ID or replace it with a server-generated ID."""

    if is_safe_request_identifier(value):
        return value
    return str(uuid.uuid4())


def safe_idempotency_key_for_response(value: str | None) -> str | None:
    """Never reflect an invalid or oversized idempotency key in an error envelope."""

    return value if is_safe_request_identifier(value) else None
