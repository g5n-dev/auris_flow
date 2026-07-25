from __future__ import annotations

import json
import logging

import pytest

from app.core import redaction as redaction_module
from app.core.context import RequestContext
from app.core.logging import LOGGER_NAME, get_logger, log_event
from app.core.redaction import redact_structured_value, trusted_sha256


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


def test_redaction_preserves_strict_sha256_proofs_with_phone_like_digits() -> None:
    digest = "a13800138000b" + ("c" * 51)

    assert redact_structured_value(
        {
            "release_gate_proof": {
                "request_sha256": trusted_sha256(digest),
                "decision_sha256": trusted_sha256(digest.upper()),
                "note": f"审计摘要旁的普通文本仍需脱敏：{digest}",
            }
        }
    ) == {
        "release_gate_proof": {
            "request_sha256": digest,
            "decision_sha256": digest.upper(),
            "note": "审计摘要旁的普通文本仍需脱敏：a[REDACTED_PHONE]b" + ("c" * 51),
        }
    }


@pytest.mark.parametrize(
    ("field_name", "invalid_digest"),
    (
        ("request_sha256", "a13800138000b" + ("c" * 51)),
        ("note_sha256", "a13800138000b" + ("c" * 51)),
        ("customer_sha256", "a13800138000b" + ("c" * 51)),
        ("private_key_sha256", "a13800138000b" + ("c" * 51)),
        ("request_sha256", "a13800138000b" + ("c" * 50)),
        ("request_sha256", "a13800138000b" + ("c" * 52)),
        ("request_sha256", "g13800138000b" + ("c" * 51)),
    ),
)
def test_redaction_does_not_trust_unmarked_or_noncanonical_sha256_values(
    field_name: str,
    invalid_digest: str,
) -> None:
    redacted = redact_structured_value({field_name: invalid_digest})

    assert "13800138000" not in redacted[field_name]
    assert "[REDACTED_PHONE]" in redacted[field_name]


@pytest.mark.parametrize(
    "invalid_digest",
    (
        "a13800138000b" + ("c" * 50),
        "a13800138000b" + ("c" * 52),
        "g13800138000b" + ("c" * 51),
    ),
)
def test_trusted_sha256_rejects_invalid_values(invalid_digest: str) -> None:
    with pytest.raises(ValueError, match="exactly 64 hexadecimal"):
        trusted_sha256(invalid_digest)


def test_redaction_scans_untrusted_reference_values() -> None:
    redacted = redact_structured_value(
        {
            "customer_ref": "owner@example.com",
            "object_key": "tenant/138-0013-8000/京A12345.wav",
            "asset_key": "customer/13800138000",
            "partition_key": "tenant/13800138000",
            "callback_ref": "https://user:pass@example.test/callback",
        }
    )

    assert redacted == {
        "customer_ref": "[REDACTED_EMAIL]",
        "object_key": "tenant/[REDACTED_PHONE]/[REDACTED_PLATE].wav",
        "asset_key": "customer/[REDACTED_PHONE]",
        "partition_key": "tenant/[REDACTED_PHONE]",
        "callback_ref": "[REDACTED_SECRET]",
    }


def test_redaction_bounds_every_content_regex_input(monkeypatch: pytest.MonkeyPatch) -> None:
    scanned_lengths: list[int] = []

    class RegexProbe:
        def search(self, value: str) -> None:
            scanned_lengths.append(len(value))
            return None

        def sub(self, replacement: str, value: str) -> str:
            del replacement
            scanned_lengths.append(len(value))
            return value

    probe = RegexProbe()
    for pattern_name in (
        "AUTHORIZATION_VALUE_PATTERN",
        "JWT_PATTERN",
        "PEM_PATTERN",
        "API_KEY_PATTERN",
        "DANGEROUS_URL_PATTERN",
        "COOKIE_VALUE_PATTERN",
        "EMAIL_PATTERN",
        "PHONE_PATTERN",
        "IDENTITY_PATTERN",
        "LICENSE_PLATE_PATTERN",
        "VIN_PATTERN",
    ):
        monkeypatch.setattr(redaction_module, pattern_name, probe)

    boundary_value = "a" * redaction_module.MAX_REGEX_SCAN_LENGTH
    assert redact_structured_value({"description": boundary_value}) == {
        "description": f"{boundary_value[:300]}...[TRUNCATED]"
    }
    assert scanned_lengths
    assert max(scanned_lengths) <= redaction_module.MAX_REGEX_SCAN_LENGTH


def test_redaction_rejects_oversized_adversarial_text_before_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedRegexCall:
        def search(self, value: str) -> None:
            raise AssertionError(f"unexpected regex search for {len(value)} bytes")

        def sub(self, replacement: str, value: str) -> str:
            raise AssertionError(
                f"unexpected regex substitution for {len(value)} bytes using {replacement}"
            )

    probe = UnexpectedRegexCall()
    for pattern_name in (
        "AUTHORIZATION_VALUE_PATTERN",
        "JWT_PATTERN",
        "PEM_PATTERN",
        "API_KEY_PATTERN",
        "DANGEROUS_URL_PATTERN",
        "COOKIE_VALUE_PATTERN",
        "EMAIL_PATTERN",
        "PHONE_PATTERN",
        "IDENTITY_PATTERN",
        "LICENSE_PLATE_PATTERN",
        "VIN_PATTERN",
    ):
        monkeypatch.setattr(redaction_module, pattern_name, probe)

    oversized = (
        ("a" * redaction_module.MAX_REGEX_SCAN_LENGTH)
        + "@"
        + ("." * redaction_module.MAX_REGEX_SCAN_LENGTH)
        + "!"
    )

    assert redact_structured_value({"description": oversized}) == {
        "description": f"[REDACTED_TEXT length={len(oversized)}]"
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


def test_structured_logging_context_cannot_be_overridden_by_caller_fields() -> None:
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    ctx = RequestContext(
        tenant_id="tenant-authoritative",
        project_id="project-authoritative",
        user_id="user-authoritative",
        roles=("project_admin",),
        request_id="request-authoritative",
        trace_id="trace-authoritative",
        idempotency_key="idem-authoritative",
    )
    root = logging.getLogger(LOGGER_NAME)
    handler = CaptureHandler()
    root.addHandler(handler)
    try:
        log_event(
            get_logger("context-binding-test"),
            "context.binding.test",
            ctx=ctx,
            tenant_id="tenant-forged",
            project_id="project-forged",
            user_id="user-forged",
            request_id="request-forged",
            trace_id="trace-forged",
            idempotency_key="idem-forged",
        )
    finally:
        root.removeHandler(handler)

    payload = json.loads(records[-1].getMessage())
    assert payload["tenant_id"] == ctx.tenant_id
    assert payload["project_id"] == ctx.project_id
    assert payload["user_id"] == ctx.user_id
    assert payload["request_id"] == ctx.request_id
    assert payload["trace_id"] == ctx.trace_id
    assert payload["idempotency_key"] == ctx.idempotency_key
