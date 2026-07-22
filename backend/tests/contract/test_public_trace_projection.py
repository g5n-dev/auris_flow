from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from app.api.routers.traces import _public_trace_scalar, _public_trace_span
from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.json_keys import json_key_fingerprint, normalize_json_key
from app.models import (
    AgentRun,
    AuditLog,
    OutboxDeliveryAttempt,
    OutboxEvent,
    PromptVersionCandidate,
    RunRecord,
    ToolCall,
)

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "doc/backend-spec/openapi-v0.1.yaml"
API_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "doc/backend-spec/api-contract.md"

FORBIDDEN_TRACE_FIELD_FINGERPRINTS = frozenset(
    {
        "adapter",
        "adapterdispatch",
        "dispatch",
        "dispatchidempotencykey",
        "operation",
        "remoteid",
        "requesthash",
        "requestsha256",
    }
)
FORBIDDEN_TRACE_FIELD_TOKENS = frozenset({"adapter", "dispatch", "operation"})
RAW_EVIDENCE_CANARIES = frozenset(
    {
        "adapter-canary-must-stay-in-db",
        "dispatch-canary-must-stay-in-db",
        "operation-canary-must-stay-in-db",
        "remote-canary-must-stay-in-db",
        "request-hash-canary-must-stay-in-db",
    }
)


def _asset_manager_only_token() -> str:
    profile = DevAuthProfile(
        email="asset-manager-trace-policy@auris.local",
        user_id="u_admin_001",
        name="Asset Manager Trace Policy",
        role_label="资产管理员",
        initials="资",
        roles=("asset_manager",),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


@pytest.mark.parametrize(
    ("field", "value", "forbidden"),
    (
        ("action", "Ｄａｇｓｔｅｒ", "dagster"),
        ("action", "Graph\u200bQL", "graphql"),
        ("action", "Dаgster", "dagster"),  # Cyrillic small a.
        ("action", "Bearer trace-secret-canary", "trace-secret-canary"),
        (
            "route",
            "https://trace-user:trace-password-canary@internal.example/path",
            "trace-password-canary",
        ),
    ),
)
def test_trace_scalar_reuses_unicode_and_credential_safe_run_value_policy(
    field: str,
    value: str,
    forbidden: str,
) -> None:
    projected = _public_trace_scalar(field, value)

    assert isinstance(projected, str)
    normalized = "".join(
        char
        for char in __import__("unicodedata").normalize("NFKC", projected)
        if __import__("unicodedata").category(char) != "Cf"
    ).casefold()
    assert forbidden not in normalized


@pytest.mark.parametrize(
    "unsafe_route",
    (
        "//attacker.example/trace",
        "https://attacker.example/trace",
        "traces/trace-safe\r\nLocation:https://attacker.example",
        "traces/%2e%2e/admin",
        "https：//attacker.example/trace",
    ),
)
def test_trace_next_actions_omit_non_local_routes(unsafe_route: str) -> None:
    projected = _public_trace_span(
        {
            "kind": "outbox",
            "id": "event-public-route-guard",
            "next_actions": [
                {
                    "key": "view_trace",
                    "label": "View trace",
                    "route": unsafe_route,
                }
            ],
        }
    )

    assert projected["next_actions"] == [{"key": "view_trace", "label": "View trace"}]


def test_trace_next_actions_preserve_existing_relative_route_contract() -> None:
    projected = _public_trace_span(
        {
            "kind": "outbox",
            "id": "event-public-route-compatible",
            "next_actions": [
                {
                    "key": "view_trace",
                    "label": "View trace",
                    "route": "traces/trace-public-compatible",
                }
            ],
        }
    )

    assert projected["next_actions"] == [
        {
            "key": "view_trace",
            "label": "View trace",
            "route": "traces/trace-public-compatible",
        }
    ]


def _assert_public_trace_is_engine_neutral(value: Any, *, path: str = "data") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = normalize_json_key(str(key))
            fingerprint = json_key_fingerprint(str(key))
            tokens = frozenset(part for part in normalized.split("_") if part)
            assert fingerprint not in FORBIDDEN_TRACE_FIELD_FINGERPRINTS, (
                f"forbidden public trace field at {path}.{key}"
            )
            assert not FORBIDDEN_TRACE_FIELD_TOKENS.intersection(tokens), (
                f"forbidden public trace field token at {path}.{key}"
            )
            assert not ("request" in tokens and {"hash", "sha", "sha256"}.intersection(tokens)), (
                f"request hash leaked at {path}.{key}"
            )
            _assert_public_trace_is_engine_neutral(nested, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_public_trace_is_engine_neutral(nested, path=f"{path}[{index}]")


@pytest.mark.parametrize("token", ["dev-token", "model-token"])
def test_trace_projection_hides_recursive_execution_evidence_for_every_role(
    client,
    auth_headers,
    token: str,
):
    trace_id = f"trace_public_projection_{token}"
    run_id = f"run_public_projection_{token}"
    agent_run_id = f"agent_public_projection_{token}"
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="label_optimization",
                status="running",
                trace_id=trace_id,
                payload={
                    "status": "running",
                    "dispatch": {
                        "adapter": "adapter-canary-must-stay-in-db",
                        "operation": "operation-canary-must-stay-in-db",
                        "details": {"remote_id": "remote-canary-must-stay-in-db"},
                    },
                },
            )
        )
        event = OutboxEvent(
            tenant_id="aurora_auto",
            project_id="sales_qa",
            event_type="label_optimization.requested",
            aggregate_type="label_versions",
            aggregate_id=run_id,
            status="pending",
            payload={
                "trace_id": trace_id,
                "adapter_dispatch": {
                    "payload": {
                        "dispatch": "dispatch-canary-must-stay-in-db",
                        "requestHash": "request-hash-canary-must-stay-in-db",
                    }
                },
                "retry_after_seconds": 15,
            },
            dispatch_idempotency_key=f"dispatch-key-{token}",
            last_error="DAGSTER_TIMEOUT: temporary execution failure",
            attempt_count=2,
        )
        session.add(event)
        session.flush()
        session.add_all(
            [
                AgentRun(
                    agent_run_id=agent_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="running",
                    trace_id=trace_id,
                    payload={"source_run_id": run_id},
                ),
                ToolCall(
                    tool_call_id=f"tool_public_projection_{token}",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="failed",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": agent_run_id,
                        "source_run_id": run_id,
                        "dispatch": {"opaque": "dispatch-canary-must-stay-in-db"},
                    },
                ),
                OutboxDeliveryAttempt(
                    attempt_id=f"attempt_public_projection_{token}",
                    event_id=event.event_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    attempt_number=2,
                    lease_generation=2,
                    claimed_by="worker-public-projection",
                    claim_token_sha256="a" * 64,
                    delivery_mode="dispatch",
                    status="failed",
                    dispatch_idempotency_key=f"dispatch-key-{token}",
                    request_sha256="b" * 64,
                    adapter="adapter-canary-must-stay-in-db",
                    operation="operation-canary-must-stay-in-db",
                    remote_id="remote-canary-must-stay-in-db",
                    error_code="DAGSTER_TIMEOUT",
                    error_message="temporary execution failure",
                ),
                AuditLog(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    actor_id="u_admin_001",
                    action="label_optimization.updated",
                    object_type="label_versions",
                    object_id=run_id,
                    result="success",
                    trace_id=trace_id,
                    after_json={
                        "adapter_dispatch": {
                            "remote_id": "remote-canary-must-stay-in-db",
                            "requestHash": "request-hash-canary-must-stay-in-db",
                        }
                    },
                ),
            ]
        )

    response = client.get(
        f"/api/v1/traces/{trace_id}",
        headers={**auth_headers, "Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    _assert_public_trace_is_engine_neutral(data)
    assert all(canary not in response.text for canary in RAW_EVIDENCE_CANARIES)
    assert "dagster" not in response.text.casefold()
    assert data["trace_id"] == trace_id
    assert data["tenant_id"] == "aurora_auto"
    assert data["project_id"] == "sales_qa"
    spans = data["spans"]
    assert any(
        span.get("kind") == "run"
        and span.get("run_id") == run_id
        and span.get("status") == "running"
        for span in spans
    )
    outbox = next(span for span in spans if span.get("kind") == "outbox")
    assert outbox["aggregate_id"] == run_id
    assert outbox["status"] == "pending"
    assert outbox["attempt_count"] == 2
    assert outbox["retryable"] is True
    assert outbox["retry_after_seconds"] == 15
    assert outbox["error_code"] == "EXECUTION_TIMEOUT"
    assert outbox["next_actions"][0]["key"] == "retry_scheduled"
    attempt = next(span for span in spans if span.get("kind") == "outbox_delivery_attempt")
    assert attempt["event_id"] == outbox["id"]
    assert attempt["status"] == "failed"
    assert attempt["attempt_number"] == 2
    assert attempt["error_code"] == "EXECUTION_TIMEOUT"

    with SessionLocal() as session:
        persisted_event = session.get(OutboxEvent, outbox["id"])
        persisted_attempt = session.get(
            OutboxDeliveryAttempt,
            f"attempt_public_projection_{token}",
        )
        persisted_audit = (
            session.query(AuditLog)
            .filter(AuditLog.trace_id == trace_id, AuditLog.object_id == run_id)
            .one()
        )
        assert persisted_event is not None
        assert persisted_attempt is not None
        assert (
            persisted_event.payload["adapter_dispatch"]["payload"]["requestHash"]
            == "request-hash-canary-must-stay-in-db"
        )
        assert persisted_attempt.adapter == "adapter-canary-must-stay-in-db"
        assert persisted_attempt.operation == "operation-canary-must-stay-in-db"
        assert persisted_attempt.remote_id == "remote-canary-must-stay-in-db"
        assert persisted_attempt.error_code == "DAGSTER_TIMEOUT"
        assert (
            persisted_audit.after_json["adapter_dispatch"]["remote_id"]
            == "remote-canary-must-stay-in-db"
        )


def test_trace_prompt_candidates_reuse_sensitive_collection_policy_and_scope(
    client,
    auth_headers,
) -> None:
    trace_id = "trace_prompt_candidate_sensitive_projection"
    scoped_candidate_id = "candidate-trace-sensitive-scoped"
    cross_project_candidate_id = "candidate-trace-sensitive-cross-project"
    cross_tenant_candidate_id = "candidate-trace-sensitive-cross-tenant"
    with SessionLocal.begin() as session:
        session.add_all(
            [
                PromptVersionCandidate(
                    candidate_id=scoped_candidate_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="candidate",
                    trace_id=trace_id,
                    payload={
                        "source_run_id": "source-run-trace-sensitive-canary",
                        "base_prompt_version": "base-prompt-trace-sensitive-canary",
                        "change_set_id": "change-set-trace-sensitive-canary",
                    },
                ),
                PromptVersionCandidate(
                    candidate_id=cross_project_candidate_id,
                    tenant_id="aurora_auto",
                    project_id="foreign-project",
                    status="candidate",
                    trace_id=trace_id,
                    payload={"source_run_id": "cross-project-source-canary"},
                ),
                PromptVersionCandidate(
                    candidate_id=cross_tenant_candidate_id,
                    tenant_id="foreign-tenant",
                    project_id="sales_qa",
                    status="candidate",
                    trace_id=trace_id,
                    payload={"source_run_id": "cross-tenant-source-canary"},
                ),
                OutboxEvent(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    event_type="prompt-version-candidate.created",
                    aggregate_type="prompt_version_candidate",
                    aggregate_id=scoped_candidate_id,
                    status="pending",
                    payload={
                        "trace_id": trace_id,
                        "prompt_candidate_id": scoped_candidate_id,
                    },
                    dispatch_idempotency_key="prompt-candidate-trace-sensitive-event",
                ),
            ]
        )

    privileged = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    model = client.get(
        f"/api/v1/traces/{trace_id}",
        headers={**auth_headers, "Authorization": "Bearer model-token"},
    )
    asset_manager = client.get(
        f"/api/v1/traces/{trace_id}",
        headers={**auth_headers, "Authorization": f"Bearer {_asset_manager_only_token()}"},
    )

    for response in (privileged, model, asset_manager):
        assert response.status_code == 200, response.text
        assert cross_project_candidate_id not in response.text
        assert cross_tenant_candidate_id not in response.text
        assert "cross-project-source-canary" not in response.text
        assert "cross-tenant-source-canary" not in response.text

    for response in (privileged, model):
        candidates = [
            span
            for span in response.json()["data"]["spans"]
            if span.get("kind") == "prompt_version_candidate"
        ]
        assert candidates == [
            {
                "kind": "prompt_version_candidate",
                "id": scoped_candidate_id,
                "candidate_id": scoped_candidate_id,
                "status": "candidate",
                "source_run_id": "source-run-trace-sensitive-canary",
                "base_prompt_version": "base-prompt-trace-sensitive-canary",
                "change_set_id": "change-set-trace-sensitive-canary",
                "object_id": scoped_candidate_id,
            }
        ]

    asset_payload = asset_manager.json()["data"]
    assert not any(
        span.get("kind") == "prompt_version_candidate" for span in asset_payload["spans"]
    )
    for canary in (
        scoped_candidate_id,
        "source-run-trace-sensitive-canary",
        "base-prompt-trace-sensitive-canary",
        "change-set-trace-sensitive-canary",
    ):
        assert canary not in asset_manager.text


def test_trace_contract_documents_only_domain_projection_and_offline_evidence():
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    span_schema = document["components"]["schemas"]["TraceSpan"]
    api_contract = API_CONTRACT_PATH.read_text(encoding="utf-8")
    normalized_contract = " ".join(api_contract.split())

    _assert_public_trace_is_engine_neutral(span_schema.get("properties", {}), path="TraceSpan")
    assert "公开领域追踪入口" in api_contract
    assert all(
        source in normalized_contract for source in ("DB", "Outbox", "Audit", "离线 verifier")
    )
    assert "内部排障入口" not in api_contract
