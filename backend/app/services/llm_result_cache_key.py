from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

CACHE_KEY_SCHEMA_VERSION = "auris.llm-result-cache-key.v1"
CACHE_KEY_PREFIX = "auris:llm-result:v1"
EXCLUDED_RUNTIME_FIELDS = ("request_id", "timestamp", "trace_id")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VOLATILE_KEY_FINGERPRINTS = frozenset(
    {
        "traceid",
        "requestid",
        "timestamp",
        "createdat",
        "updatedat",
    }
)
_SECRET_KEY_FINGERPRINTS = frozenset(
    {
        "apikey",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "secret",
        "password",
    }
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _key_fingerprint(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize_json_value(
    value: Any,
    *,
    reject_runtime_keys: bool,
    path: str,
) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return value
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, list | tuple):
        return [
            _normalize_json_value(
                item,
                reject_runtime_keys=reject_runtime_keys,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError(f"{path} object keys must be strings")
            key = _normalize_text(raw_key)
            if not key:
                raise ValueError(f"{path} object keys must not be blank")
            fingerprint = _key_fingerprint(key)
            if reject_runtime_keys and fingerprint in _VOLATILE_KEY_FINGERPRINTS:
                raise ValueError(
                    f"{path}.{key} is a runtime-only field and cannot enter cache keys"
                )
            if fingerprint in _SECRET_KEY_FINGERPRINTS:
                raise ValueError(f"{path}.{key} is a secret field and cannot enter cache keys")
            if key in normalized:
                raise ValueError(f"{path} contains duplicate keys after Unicode normalization")
            normalized[key] = _normalize_json_value(
                item,
                reject_runtime_keys=reject_runtime_keys,
                path=f"{path}.{key}",
            )
        return normalized
    raise ValueError(f"{path} must contain JSON-compatible values")


def normalized_input_sha256(value: Any) -> str:
    """Hash a normalized LLM input without retaining the input in cache metadata."""

    normalized = _normalize_json_value(
        value,
        reject_runtime_keys=False,
        path="normalized_input",
    )
    return hashlib.sha256(_canonical_json_bytes(normalized)).hexdigest()


class LlmResultCacheKeySpec(BaseModel):
    """Stable, scoped inputs that are allowed to influence an LLM result cache key."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str = Field(min_length=1, max_length=64)
    project_id: str = Field(min_length=1, max_length=64)
    task: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    prompt_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taxonomy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_params: dict[str, Any]
    normalized_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("tenant_id", "project_id", "task", "model")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not normalized:
            raise ValueError("stable cache-key identities must not be blank")
        return normalized

    @field_validator(
        "prompt_content_sha256",
        "taxonomy_sha256",
        "schema_sha256",
        "normalized_input_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("SHA-256 values must be lowercase hexadecimal")
        return value

    @field_validator("generation_params")
    @classmethod
    def normalize_generation_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        normalized = _normalize_json_value(
            value,
            reject_runtime_keys=True,
            path="generation_params",
        )
        if not isinstance(normalized, dict):  # defensive: the annotated field is an object
            raise ValueError("generation_params must be an object")
        return normalized


@dataclass(frozen=True)
class LlmResultCacheKey:
    key: str
    digest_sha256: str
    metadata: dict[str, Any]


def build_llm_result_cache_key(
    spec: LlmResultCacheKeySpec,
    *,
    runtime_context: Mapping[str, Any] | None = None,
) -> LlmResultCacheKey:
    """Build a Redis-compatible key; this function never reads or writes Redis.

    ``runtime_context`` is accepted only to make the exclusion boundary explicit.
    Trace IDs, request IDs and timestamps are intentionally absent from both the
    hashed document and returned metadata.
    """

    _ = runtime_context
    components = spec.model_dump(mode="json")
    stable_document = {
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        **components,
    }
    digest = hashlib.sha256(_canonical_json_bytes(stable_document)).hexdigest()
    generation_params_sha256 = hashlib.sha256(
        _canonical_json_bytes(components["generation_params"])
    ).hexdigest()
    metadata = {
        "schema_version": CACHE_KEY_SCHEMA_VERSION,
        "authority": "cache-only",
        "cache_key_sha256": digest,
        "components": components,
        "generation_params_sha256": generation_params_sha256,
        "excluded_runtime_fields": list(EXCLUDED_RUNTIME_FIELDS),
    }
    return LlmResultCacheKey(
        key=f"{CACHE_KEY_PREFIX}:{digest}",
        digest_sha256=digest,
        metadata=metadata,
    )


__all__ = [
    "CACHE_KEY_SCHEMA_VERSION",
    "EXCLUDED_RUNTIME_FIELDS",
    "LlmResultCacheKey",
    "LlmResultCacheKeySpec",
    "build_llm_result_cache_key",
    "normalized_input_sha256",
]
