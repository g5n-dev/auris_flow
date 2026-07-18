from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import LOGGER_NAME, get_logger, log_event
from app.core.redaction import redact_structured_value


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    (
        ("accessKey", "access-value", "[REDACTED]"),
        ("private-key", "private-value", "[REDACTED]"),
        ("customerPhone", "13800138000", "[REDACTED_PII]"),
        ("phone_key", "13800138000", "[REDACTED_PII]"),
        ("recognizedText", "客户原始话术", "[REDACTED_TEXT length=6]"),
        ("secret_ref", "sec_provider_001", "sec_provider_001"),
        ("tokenCount", 128, 128),
        ("signature_mode", "hmac-sha256", "hmac-sha256"),
        ("signature_hash", "sha256:abc123", "sha256:abc123"),
        ("signing_key_id", "key_release_001", "key_release_001"),
    ),
)
def test_redaction_normalizes_aliases_without_destroying_safe_metadata(
    field: str,
    value: object,
    expected: object,
) -> None:
    assert redact_structured_value({field: value}) == {field: expected}


@pytest.mark.parametrize(
    "secret_value",
    (
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "Basic dXNlcjpwYXNzd29yZA==",
        "eyJhbGciOiJIUzI1NiJ9.cGF5bG9hZA.c2lnbmF0dXJl",
        "-----BEGIN " + "ENCRYPTED PRIVATE KEY-----\nCANARY",
        "github" + "_pat_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "sk" + "-" + "abcdefghijklmnopqrstuvwxyz012345",
        "https://user:password@example.test/audio.wav",
        "https://bucket.example.test/a.wav?X-Amz-Signature=canary",
        "Cookie: session=canary-secret",
    ),
)
def test_redaction_blocks_secrets_hidden_in_neutral_fields(secret_value: str) -> None:
    assert redact_structured_value({"note": secret_value}) == {"note": "[REDACTED_SECRET]"}


def test_redaction_keeps_opaque_trace_reference_with_phone_like_digits() -> None:
    trace_id = "trace_21913830672f4e4695dc94139a262072"
    assert redact_structured_value({"trace_id": trace_id}) == {"trace_id": trace_id}


def test_redaction_scans_untrusted_reference_values() -> None:
    redacted = redact_structured_value(
        {
            "customer_ref": "owner@example.com",
            "object_key": "tenant/138-0013-8000/京A12345.wav",
            "callback_ref": "https://user:pass@example.test/callback",
        }
    )

    assert redacted == {
        "customer_ref": "[REDACTED_EMAIL]",
        "object_key": "tenant/[REDACTED_PHONE]/[REDACTED_PLATE].wav",
        "callback_ref": "[REDACTED_SECRET]",
    }


def test_structured_logging_uses_the_same_redaction_policy() -> None:
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = get_logger("redaction-test")
    root = logging.getLogger(LOGGER_NAME)
    handler = CaptureHandler()
    root.addHandler(handler)
    try:
        log_event(
            logger,
            "redaction.test",
            note="Bearer canary-secret-value",
            customerPhone="13800138000",
            transcript="完整客户转写",
            secret_ref="sec_provider_001",
        )
    finally:
        root.removeHandler(handler)

    payload = json.loads(records[-1].getMessage())
    assert payload["note"] == "[REDACTED_SECRET]"
    assert payload["customerPhone"] == "[REDACTED_PII]"
    assert payload["transcript"] == "[REDACTED_TEXT length=6]"
    assert payload["secret_ref"] == "sec_provider_001"
