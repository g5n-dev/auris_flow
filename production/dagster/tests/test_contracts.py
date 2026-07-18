from __future__ import annotations

import pytest

from auris_flow_dagster.contracts import AurisContractError, validate_auris_context


def test_context_requires_scope_trace_run_idempotency_and_fencing(
    valid_context: dict[str, object],
) -> None:
    for field in (
        "tenant_id",
        "project_id",
        "trace_id",
        "run_id",
        "dispatch_idempotency_key",
        "outbox_fencing_token",
    ):
        candidate = dict(valid_context)
        candidate.pop(field)
        with pytest.raises(AurisContractError, match=field):
            validate_auris_context(candidate)


@pytest.mark.parametrize("token", ["", "0:1", "1:0", "1", "1:-1", "lease:1"])
def test_context_rejects_invalid_fencing_tokens(
    valid_context: dict[str, object], token: str
) -> None:
    candidate = {**valid_context, "outbox_fencing_token": token}
    with pytest.raises(AurisContractError, match="outbox_fencing_token"):
        validate_auris_context(candidate)


def test_context_preserves_only_public_metadata(valid_context: dict[str, object]) -> None:
    raw = {**valid_context, "shared_secret": "must-never-be-projected"}
    scope = validate_auris_context(raw)
    metadata = scope.public_metadata()
    assert metadata["tenant_id"] == "aurora_auto"
    assert "shared_secret" not in metadata
    assert "dispatch_idempotency_key" not in metadata
    assert "outbox_fencing_token" not in metadata


def test_context_accepts_a_complete_w3c_parent_for_cross_process_tracing(
    valid_context: dict[str, object],
) -> None:
    scope = validate_auris_context(
        {
            **valid_context,
            "otel_trace_id": "0123456789abcdef0123456789abcdef",
            "otel_parent_span_id": "0123456789abcdef",
            "otel_trace_flags": "01",
        }
    )

    assert scope.otel_trace_id == "0123456789abcdef0123456789abcdef"
    assert scope.otel_parent_span_id == "0123456789abcdef"
    assert scope.otel_trace_flags == "01"


@pytest.mark.parametrize(
    "updates",
    [
        {"otel_trace_id": "0123456789abcdef0123456789abcdef"},
        {
            "otel_trace_id": "not-hex",
            "otel_parent_span_id": "0123456789abcdef",
            "otel_trace_flags": "01",
        },
        {
            "otel_trace_id": "0123456789abcdef0123456789abcdef",
            "otel_parent_span_id": "0000000000000000",
            "otel_trace_flags": "01",
        },
        {
            "otel_trace_id": "0123456789abcdef0123456789abcdef",
            "otel_parent_span_id": "0123456789abcdef",
            "otel_trace_flags": "ff",
        },
        {
            "otel_trace_id": [],
            "otel_parent_span_id": "0123456789abcdef",
            "otel_trace_flags": "01",
        },
        {
            "otel_trace_id": "0123456789abcdef0123456789abcdef",
            "otel_parent_span_id": {},
            "otel_trace_flags": "01",
        },
        {
            "otel_trace_id": "0123456789abcdef0123456789abcdef",
            "otel_parent_span_id": "0123456789abcdef",
            "otel_trace_flags": ["01"],
        },
    ],
)
def test_context_rejects_partial_or_invalid_w3c_parents(
    valid_context: dict[str, object], updates: dict[str, object]
) -> None:
    with pytest.raises(AurisContractError, match="OpenTelemetry parent"):
        validate_auris_context({**valid_context, **updates})
