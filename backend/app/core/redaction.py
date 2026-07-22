from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SECRET_LEAF_FIELDS = {
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "signature",
    "signed_url",
    "presigned_url",
}
SAFE_SECRET_REFERENCE_FIELDS = {
    "credential_ref",
    "key_id",
    "secret_ref",
    "signature_key_id",
    "signing_key_id",
}
SENSITIVE_TEXT_FIELDS = {
    "audio_bytes",
    "corrected_text",
    "full_transcript",
    "model_input",
    "model_output",
    "prompt_input",
    "prompt_output",
    "raw_audio",
    "raw_transcript",
    "recognized_text",
    "request_body",
    "response_body",
    "transcript",
    "transcript_preview",
    "transcript_text",
    "utterance",
    "utterance_text",
}
PII_VALUE_TOKENS = {
    "address",
    "email",
    "mobile",
    "phone",
    "plate",
    "telephone",
    "vin",
    "wechat",
}
PII_SUBJECT_TOKENS = {
    "contact",
    "contacts",
    "customer",
    "customers",
    "person",
    "speaker",
}
PII_COMPOUND_FIELDS = {
    "document_number",
    "id_card",
    "identity_number",
    "order_no",
    "order_number",
}
SAFE_REFERENCE_FIELDS = {
    "evidence_id",
    "root_trace_id",
    "run_id",
    "source_id",
    "storage_object_id",
    "trace_id",
}
UNTRUSTED_REFERENCE_FIELDS = {"object_key", "url", "uri"}
SAFE_LONG_TEXT_FIELDS = {"description", "diff_summary", "reason", "summary"}

MAX_TEXT_LENGTH = 300
MAX_LIST_ITEMS = 50
MAX_DICT_ITEMS = 100
MAX_DEPTH = 12
MAX_NODES = 1_000
MAX_SERIALIZED_BYTES = 65_536

EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:(?:\+?86)[ -]?)?1[3-9](?:[ -]?\d){9}(?!\d)")
IDENTITY_PATTERN = re.compile(r"(?<![0-9A-Za-z])\d{17}[0-9Xx](?![0-9A-Za-z])")
LICENSE_PLATE_PATTERN = re.compile(
    r"(?<![A-Z0-9])"
    r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼]"
    r"[A-Z][A-Z0-9]{5,6}"
    r"(?![A-Z0-9])",
    re.IGNORECASE,
)
VIN_PATTERN = re.compile(
    r"(?<![A-Z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9]))"
    r"(?=[A-HJ-NPR-Z0-9]*\d)[A-HJ-NPR-Z0-9]{17}",
    re.IGNORECASE,
)
AUTHORIZATION_VALUE_PATTERN = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
PEM_PATTERN = re.compile(
    r"-----BEGIN(?: ENCRYPTED)? [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
API_KEY_PATTERN = re.compile(
    r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{16,})\b"
)
DANGEROUS_URL_PATTERN = re.compile(
    r"(?i)(?:https?://[^/\s:@]+:[^/\s@]+@|[?&](?:x-amz-signature|signature|"
    r"ossaccesskeyid|accesskeyid|token|secret)=[^&#\s]+)"
)
COOKIE_VALUE_PATTERN = re.compile(
    r"(?i)\b(?:cookie|set-cookie)\s*:\s*[^\r\n]+|\b(?:session|sessionid)=[^;\s]+"
)
INTERNAL_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/|-]{0,511}$")
SHA256_VALUE_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class TrustedSha256(str):
    """Server-validated digest marker for integrity-sensitive audit fields."""


def trusted_sha256(value: str) -> TrustedSha256:
    if SHA256_VALUE_PATTERN.fullmatch(value) is None:
        raise ValueError("trusted SHA-256 values must be exactly 64 hexadecimal characters")
    return TrustedSha256(value)


@dataclass
class _RedactionState:
    nodes: int = 0


def normalize_field_name(field_name: object) -> str:
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(field_name).strip())
    normalized = re.sub(r"[^0-9a-zA-Z_]+", "_", raw.lower())
    return re.sub(r"_+", "_", normalized).strip("_")


def _leaf_field(field_name: str) -> str:
    return field_name.rsplit("_", 1)[-1] if field_name else ""


def _field_tokens(field_name: str) -> set[str]:
    return {token for token in field_name.split("_") if token}


def _matches_field_name(field_name: str, expected: str) -> bool:
    return field_name == expected or field_name.endswith(f"_{expected}")


def _is_reference_field(field_name: str) -> bool:
    leaf = _leaf_field(field_name)
    if any(_matches_field_name(field_name, name) for name in UNTRUSTED_REFERENCE_FIELDS):
        return False
    return (
        any(_matches_field_name(field_name, name) for name in SAFE_REFERENCE_FIELDS)
        or any(_matches_field_name(field_name, name) for name in SAFE_SECRET_REFERENCE_FIELDS)
        or leaf.endswith(("_id", "_key", "_ref"))
        or field_name.endswith(("_id", "_key", "_ref"))
    )


def _is_secret_field(field_name: str) -> bool:
    if any(_matches_field_name(field_name, name) for name in SAFE_SECRET_REFERENCE_FIELDS):
        return False
    return any(_matches_field_name(field_name, suffix) for suffix in SECRET_LEAF_FIELDS)


def _is_pii_field(field_name: str) -> bool:
    if any(field_name.endswith(name) for name in PII_COMPOUND_FIELDS):
        return True
    tokens = _field_tokens(field_name)
    if tokens & PII_VALUE_TOKENS:
        return True
    return "name" in tokens and bool(tokens & PII_SUBJECT_TOKENS)


def _is_sensitive_text_field(field_name: str) -> bool:
    return any(
        field_name == name or field_name.endswith(f"_{name}") for name in SENSITIVE_TEXT_FIELDS
    )


def _contains_secret_value(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (
            AUTHORIZATION_VALUE_PATTERN,
            JWT_PATTERN,
            PEM_PATTERN,
            API_KEY_PATTERN,
            DANGEROUS_URL_PATTERN,
            COOKIE_VALUE_PATTERN,
        )
    )


def _redact_inline_pii(value: str) -> str:
    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
    redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    redacted = IDENTITY_PATTERN.sub("[REDACTED_IDENTITY]", redacted)
    redacted = LICENSE_PLATE_PATTERN.sub("[REDACTED_PLATE]", redacted)
    return VIN_PATTERN.sub("[REDACTED_VIN]", redacted)


def _redacted_text_marker(value: Any) -> str:
    length = len(value) if isinstance(value, (str, bytes, bytearray)) else None
    return f"[REDACTED_TEXT length={length}]" if length is not None else "[REDACTED_TEXT]"


def _redact(
    value: Any,
    *,
    field_path: str,
    depth: int,
    state: _RedactionState,
) -> Any:
    state.nodes += 1
    if state.nodes > MAX_NODES:
        return "[TRUNCATED_NODE_BUDGET]"
    if _is_secret_field(field_path):
        return "[REDACTED]"
    if _is_pii_field(field_path):
        return "[REDACTED_PII]"
    if _is_sensitive_text_field(field_path) and not _is_reference_field(field_path):
        return _redacted_text_marker(value)
    if depth >= MAX_DEPTH:
        return "[TRUNCATED_DEPTH]"
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        item_count = 0
        for key, item in value.items():
            if item_count >= MAX_DICT_ITEMS:
                break
            key_string = str(key)
            child_path = normalize_field_name(
                f"{field_path}.{key_string}" if field_path else key_string
            )
            redacted[key_string] = _redact(
                item,
                field_path=child_path,
                depth=depth + 1,
                state=state,
            )
            item_count += 1
        if len(value) > MAX_DICT_ITEMS:
            redacted["__truncated_fields__"] = len(value) - MAX_DICT_ITEMS
        return redacted
    if isinstance(value, (list, tuple)):
        redacted_items = [
            _redact(
                item,
                field_path=field_path,
                depth=depth + 1,
                state=state,
            )
            for item in value[:MAX_LIST_ITEMS]
        ]
        if len(value) > MAX_LIST_ITEMS:
            redacted_items.append({"__truncated_items__": len(value) - MAX_LIST_ITEMS})
        return redacted_items
    if isinstance(value, (bytes, bytearray)):
        return f"[REDACTED_BINARY length={len(value)}]"
    if isinstance(value, str):
        if _contains_secret_value(value):
            return "[REDACTED_SECRET]"
        if isinstance(value, TrustedSha256):
            # Only internal code can attach this marker after strict format
            # validation. Arbitrary user-controlled *_sha256 field names do
            # not gain an exemption from inline PII redaction.
            return str(value)
        if any(
            _matches_field_name(field_path, name) for name in SAFE_REFERENCE_FIELDS
        ) and INTERNAL_REFERENCE_PATTERN.fullmatch(value):
            # Trace/run/storage references are opaque correlation identifiers.
            # Phone-number substitution must not corrupt their digits.
            return value
        redacted_text = _redact_inline_pii(value)
        if _is_reference_field(field_path) and INTERNAL_REFERENCE_PATTERN.fullmatch(redacted_text):
            return redacted_text
        if len(redacted_text) > MAX_TEXT_LENGTH:
            if _leaf_field(field_path) in SAFE_LONG_TEXT_FIELDS:
                return f"{redacted_text[:MAX_TEXT_LENGTH]}...[TRUNCATED]"
            return _redacted_text_marker(value)
        return redacted_text
    return value


def redact_structured_value(value: Any, *, field_name: str | None = None) -> Any:
    redacted = _redact(
        value,
        field_path=normalize_field_name(field_name or ""),
        depth=0,
        state=_RedactionState(),
    )
    serialized = json.dumps(redacted, ensure_ascii=False, default=str).encode("utf-8")
    if len(serialized) <= MAX_SERIALIZED_BYTES:
        return redacted
    return {
        "__redacted_payload__": {
            "reason": "serialized_byte_budget",
            "bytes": len(serialized),
        }
    }
