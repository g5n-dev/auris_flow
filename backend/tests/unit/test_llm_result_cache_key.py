from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.services.llm_result_cache_key import (
    LlmResultCacheKeySpec,
    build_llm_result_cache_key,
    normalized_input_sha256,
)


def _spec(**overrides: object) -> LlmResultCacheKeySpec:
    values: dict[str, object] = {
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "task": "label-extraction",
        "model": "provider/model-v1",
        "prompt_content_sha256": "a" * 64,
        "taxonomy_sha256": "b" * 64,
        "schema_sha256": "c" * 64,
        "generation_params": {
            "temperature": 0,
            "max_tokens": 512,
            "response_format": {"type": "json_schema"},
        },
        "normalized_input_sha256": normalized_input_sha256(
            {"transcript": "\uff21\r\n  customer asked for refund  "}
        ),
    }
    values.update(overrides)
    return LlmResultCacheKeySpec.model_validate(values)


def test_cache_key_is_stable_and_explicitly_excludes_runtime_context() -> None:
    spec = _spec()

    first = build_llm_result_cache_key(
        spec,
        runtime_context={
            "trace_id": "trace-a",
            "request_id": "request-a",
            "timestamp": "2026-07-15T01:00:00Z",
        },
    )
    second = build_llm_result_cache_key(
        spec,
        runtime_context={
            "trace_id": "trace-b",
            "request_id": "request-b",
            "timestamp": "2026-07-15T02:00:00Z",
        },
    )

    assert first.key == second.key
    assert first.key.startswith("auris:llm-result:v1:")
    assert first.metadata["excluded_runtime_fields"] == [
        "request_id",
        "timestamp",
        "trace_id",
    ]
    serialized_metadata = repr(first.metadata)
    assert "trace-a" not in serialized_metadata
    assert "request-a" not in serialized_metadata
    assert "2026-07-15" not in serialized_metadata
    assert first.metadata["authority"] == "cache-only"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("task", "label-aggregation"),
        ("model", "provider/model-v2"),
        ("prompt_content_sha256", "d" * 64),
        ("taxonomy_sha256", "e" * 64),
        ("schema_sha256", "f" * 64),
        ("generation_params", {"temperature": 0.1, "max_tokens": 512}),
        ("normalized_input_sha256", "1" * 64),
        ("tenant_id", "another_tenant"),
        ("project_id", "another_project"),
    ),
)
def test_each_stable_component_changes_the_cache_key(field: str, value: object) -> None:
    baseline = build_llm_result_cache_key(_spec()).key
    changed = build_llm_result_cache_key(_spec(**{field: value})).key

    assert changed != baseline


def test_generation_parameter_order_does_not_change_key() -> None:
    first = _spec(
        generation_params={
            "temperature": 0,
            "stop": ["END", "STOP"],
            "response_format": {"schema": "v1", "type": "json"},
        }
    )
    second = _spec(
        generation_params={
            "response_format": {"type": "json", "schema": "v1"},
            "stop": ["END", "STOP"],
            "temperature": 0,
        }
    )

    assert build_llm_result_cache_key(first).key == build_llm_result_cache_key(second).key


@pytest.mark.parametrize("volatile", ("trace_id", "requestId", "timestamp", "created-at"))
def test_volatile_fields_are_rejected_inside_generation_parameters(volatile: str) -> None:
    with pytest.raises(ValidationError, match="runtime-only field"):
        _spec(generation_params={"temperature": 0, volatile: "must-not-enter-key"})


@pytest.mark.parametrize("secret", ("api_key", "authorization", "access-token"))
def test_secret_fields_are_rejected_inside_generation_parameters(secret: str) -> None:
    with pytest.raises(ValidationError, match="secret field"):
        _spec(generation_params={"temperature": 0, secret: "secret"})


def test_non_finite_generation_parameters_are_rejected() -> None:
    with pytest.raises(ValidationError, match="finite JSON number"):
        _spec(generation_params={"temperature": math.inf})


def test_normalized_input_hash_normalizes_unicode_newlines_and_outer_whitespace() -> None:
    composed = {"text": "A\nhello"}
    compatibility_and_crlf = {"text": "  \uff21\r\nhello  "}

    assert normalized_input_sha256(composed) == normalized_input_sha256(compatibility_and_crlf)


def test_runtime_fields_are_not_accepted_as_stable_key_spec_fields() -> None:
    with pytest.raises(ValidationError):
        LlmResultCacheKeySpec.model_validate(
            {
                **_spec().model_dump(mode="json"),
                "trace_id": "must-be-runtime-context-only",
            }
        )
