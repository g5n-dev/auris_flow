from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.context import RequestContext, require_context_membership
from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.models import (
    AgentDecision,
    AgentRun,
    AuditLog,
    HumanReviewDecision,
    HumanReviewTask,
    JsonResource,
    OutboxEvent,
    Project,
    RunRecord,
    ToolCall,
    TraceRef,
)


def _session_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
    }


def _login(client, email: str) -> str:
    response = client.post(
        "/api/v1/auth/dev-login",
        json={"email": email, "password": "auris-demo"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def _rewrite_member(project: Project, user_id: str, transform) -> None:
    members = list((project.data or {}).get("members") or [])
    project.data = {
        **(project.data or {}),
        "members": [
            transform(dict(member)) if member.get("user_id") == user_id else member
            for member in members
        ],
    }


def test_server_issued_session_rejects_explicit_empty_project_roles(client):
    with SessionLocal.begin() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        _rewrite_member(
            project,
            "u_annotator_001",
            lambda member: {**member, "roles": []},
        )

    token = _login(client, "annotator@auris.local")
    response = client.get("/api/v1/auth/session", headers=_session_headers(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ROLE_BINDING_EMPTY"
    assert response.json()["error"]["details"] == [
        {"user_id": "u_annotator_001", "roles_state": "explicit_empty"}
    ]


@pytest.mark.parametrize("release_env", ["release", "prod", "production"])
def test_legacy_missing_project_roles_are_local_compatible_but_release_denied(
    client,
    monkeypatch,
    release_env,
):
    with SessionLocal.begin() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None

        def remove_roles(member: dict) -> dict:
            member.pop("roles", None)
            return member

        _rewrite_member(project, "u_annotator_001", remove_roles)

    token = _login(client, "annotator@auris.local")
    compatible = client.get("/api/v1/auth/session", headers=_session_headers(token))
    assert compatible.status_code == 200, compatible.text

    monkeypatch.setattr(get_settings(), "app_env", release_env)
    ctx = RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="u_annotator_001",
        roles=("annotator", "review_arbitrator"),
        request_id="release-role-binding",
        trace_id="trace_release_role_binding",
    )
    with SessionLocal() as session, pytest.raises(ApiError) as raised:
        require_context_membership(session, ctx)

    assert raised.value.code == "PROJECT_ROLE_BINDING_REQUIRED"
    assert raised.value.status_code == 403


def test_sensitive_reads_default_deny_low_privilege_roles(client, auth_headers):
    annotator_headers = _session_headers("annotator-token")
    low_privilege_headers = _session_headers("annotator-b-token")
    model_headers = _session_headers("model-token")

    settings_list = client.get("/api/v1/settings", headers=annotator_headers)
    settings_detail = client.get("/api/v1/settings/model-chain", headers=annotator_headers)
    review_list = client.get("/api/v1/human-review-tasks", headers=model_headers)
    review_detail = client.get(
        "/api/v1/human-review-tasks/hrt_amount_001",
        headers=model_headers,
    )
    voiceprint_index = client.get("/api/v1/voiceprints", headers=model_headers)
    voiceprint_enrollments = client.get(
        "/api/v1/voiceprint-enrollments",
        headers=low_privilege_headers,
    )
    trace = client.get(
        "/api/v1/traces/trace_20250526_122300",
        headers=low_privilege_headers,
    )

    for response in (
        settings_list,
        settings_detail,
        review_list,
        review_detail,
        voiceprint_index,
        voiceprint_enrollments,
        trace,
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "FORBIDDEN"

    assert client.get("/api/v1/settings", headers=model_headers).status_code == 200
    assert (
        client.get(
            "/api/v1/traces/trace_20250526_122300",
            headers=auth_headers,
        ).status_code
        == 200
    )
    assert client.get("/api/v1/voiceprints", headers=auth_headers).status_code == 200


def test_human_review_assignee_isolation_preserves_admin_and_owner_access(
    client,
    auth_headers,
):
    with SessionLocal.begin() as session:
        task = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == "hrt_amount_001",
            )
        )
        assert task is not None
        task.data = {**task.data, "assignee_id": "u_annotator_001"}

    foreign_headers = _session_headers("annotator-b-token")
    owner_headers = _session_headers("annotator-token")
    foreign_list = client.get(
        "/api/v1/human-review-tasks?queue=amount_conflict",
        headers=foreign_headers,
    )
    foreign_detail = client.get(
        "/api/v1/human-review-tasks/hrt_amount_001",
        headers=foreign_headers,
    )
    foreign_ops = client.get("/api/v1/insights/ops-summary", headers=foreign_headers)
    foreign_all_tasks = client.get("/api/v1/human-review-tasks", headers=foreign_headers)

    assert foreign_list.status_code == 200, foreign_list.text
    assert foreign_list.json()["data"]["items"] == []
    assert foreign_detail.status_code == 403
    assert foreign_detail.json()["error"]["code"] == "HUMAN_REVIEW_TASK_FORBIDDEN"
    assert foreign_ops.status_code == 200, foreign_ops.text
    assert foreign_all_tasks.status_code == 200, foreign_all_tasks.text
    visible_task_count = len(foreign_all_tasks.json()["data"]["items"])
    review_metric = next(
        item
        for item in foreign_ops.json()["data"]["metrics"]
        if item["metric_key"] == "human_review"
    )
    assert review_metric["value"] == visible_task_count
    assert (
        client.get(
            "/api/v1/human-review-tasks/hrt_amount_001",
            headers=owner_headers,
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/api/v1/human-review-tasks/hrt_amount_001",
            headers=auth_headers,
        ).status_code
        == 200
    )


def test_trace_filters_human_review_spans_for_roles_without_review_access(
    client,
    auth_headers,
):
    with SessionLocal.begin() as session:
        strong_task = session.get(HumanReviewTask, "hrt_amount_001")
        assert strong_task is not None
        strong_task.payload = {
            **strong_task.payload,
            "assignee_id": "u_annotator_001",
        }
        resource_task = session.scalar(
            select(JsonResource).where(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "human_review_tasks",
                JsonResource.resource_key == "hrt_amount_001",
            )
        )
        assert resource_task is not None
        resource_task.data = {
            **resource_task.data,
            "assignee_id": "u_annotator_001",
        }

    decision = client.post(
        "/api/v1/human-review-tasks/hrt_amount_001/decisions",
        json={"decision": "accepted", "note": "trace visibility contract"},
        headers={**auth_headers, "Idempotency-Key": "trace-review-visibility"},
    )
    assert decision.status_code == 200, decision.text
    decision_id = decision.json()["data"]["decision_id"]
    trace_id = decision.json()["meta"]["trace_id"]

    privileged = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert privileged.status_code == 200, privileged.text
    assert any(
        span.get("kind") == "human_review_decision" and span.get("decision_id") == decision_id
        for span in privileged.json()["data"]["spans"]
    )

    model_trace = client.get(
        f"/api/v1/traces/{trace_id}",
        headers=_session_headers("model-token"),
    )
    assert model_trace.status_code == 200, model_trace.text
    spans = model_trace.json()["data"]["spans"]
    assert not any(
        span.get("kind") in {"human_review_task", "human_review_decision"}
        or span.get("collection") in {"human_review_tasks", "human_review_decisions"}
        or "human_review" in str(span.get("event_type") or "")
        or span.get("object_id") in {"hrt_amount_001", decision_id}
        for span in spans
    )


def test_trace_filters_voiceprint_spans_for_non_sensitive_roles(client, auth_headers):
    payload = {
        "enrollment_id": "vp_sensitive_trace_contract",
        "voiceprint_id": "VP-SENSITIVE-TRACE",
        "quality": {
            "overall": 90,
            "duration": 90,
            "snr": 90,
            "purity": 90,
            "stability": 90,
        },
        "consistency": {"ab": 0.9, "ac": 0.9, "bc": 0.9},
        "min_consistency": 0.9,
    }
    created = client.post(
        "/api/v1/voiceprint-enrollments",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": "voiceprint-sensitive-trace"},
    )
    assert created.status_code == 201, created.text
    trace_id = created.json()["meta"]["trace_id"]

    privileged = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert privileged.status_code == 200, privileged.text
    assert any("voiceprint" in str(span).lower() for span in privileged.json()["data"]["spans"])

    model_trace = client.get(
        f"/api/v1/traces/{trace_id}",
        headers=_session_headers("model-token"),
    )
    assert model_trace.status_code == 200, model_trace.text
    assert not any(
        "voiceprint" in str(span).lower() for span in model_trace.json()["data"]["spans"]
    )


def test_trace_filters_nested_sensitive_agent_and_delivery_references(client, auth_headers):
    trace_id = "trace_nested_sensitive_reference_contract"
    run_id = "run_nested_sensitive_reference_contract"
    agent_run_id = "agent_nested_sensitive_reference_contract"
    safe_run_id = "run_safe_reference_contract"
    safe_agent_run_id = "agent_safe_reference_contract"
    sensitive_id = "VP-NESTED-SENSITIVE"
    with SessionLocal.begin() as session:
        session.add_all(
            [
                RunRecord(
                    run_id=run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    run_type="label_optimization",
                    status="pending",
                    trace_id=trace_id,
                    payload={
                        "affected_objects": [{"type": "voiceprint_profile", "id": sensitive_id}]
                    },
                ),
                RunRecord(
                    run_id=safe_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    run_type="label_optimization",
                    status="pending",
                    trace_id=trace_id,
                    payload={"affected_objects": [{"type": "data_asset", "id": "asset-safe"}]},
                ),
                AgentRun(
                    agent_run_id=agent_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id=trace_id,
                    payload={
                        "source_run_id": run_id,
                        "input_refs": [{"voiceprint": {"id": sensitive_id}}],
                    },
                ),
                AgentRun(
                    agent_run_id=safe_agent_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id=trace_id,
                    payload={
                        "source_run_id": safe_run_id,
                        "input_refs": [{"type": "data_asset", "id": "asset-safe"}],
                    },
                ),
                ToolCall(
                    tool_call_id="tool_nested_sensitive_reference_contract",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="planned",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_run_id,
                        "dispatch": {
                            "result_ref": {
                                "ref_type": "voiceprint-record",
                                "ref_id": sensitive_id,
                            }
                        },
                    },
                ),
                AgentDecision(
                    decision_id="decision_nested_sensitive_reference_contract",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_run_id,
                        "result_ref": {"voiceprint": {"id": sensitive_id}},
                    },
                ),
                TraceRef(
                    trace_ref_id="trace_ref_nested_sensitive_reference_contract",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id=trace_id,
                    payload={
                        "ref_role": "input",
                        "type": "voiceprint_profile",
                        "id": sensitive_id,
                    },
                ),
                AuditLog(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    actor_id="u_admin_001",
                    action="agent.run.created",
                    object_type="label_versions",
                    object_id="lv-sensitive-agent",
                    result="success",
                    trace_id=trace_id,
                    after_json={"voiceprint": {"id": sensitive_id}},
                ),
                OutboxEvent(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    event_type="label_versions.agent-dispatch",
                    aggregate_type="label_versions",
                    aggregate_id="lv-sensitive-agent",
                    status="pending",
                    payload={
                        "trace_id": trace_id,
                        "affected_objects": [
                            {
                                "ref_type": "voiceprint-record",
                                "ref_id": sensitive_id,
                            }
                        ],
                    },
                    dispatch_idempotency_key="dispatch-nested-sensitive-reference-contract",
                ),
            ]
        )

    privileged = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert privileged.status_code == 200, privileged.text
    assert sensitive_id in privileged.text

    model_trace = client.get(
        f"/api/v1/traces/{trace_id}",
        headers=_session_headers("model-token"),
    )
    assert model_trace.status_code == 200, model_trace.text
    assert sensitive_id not in model_trace.text
    assert safe_run_id in model_trace.text
    assert safe_agent_run_id in model_trace.text
    assert not any(
        span.get("id")
        in {
            run_id,
            agent_run_id,
            "tool_nested_sensitive_reference_contract",
            "decision_nested_sensitive_reference_contract",
            "trace_ref_nested_sensitive_reference_contract",
        }
        for span in model_trace.json()["data"]["spans"]
    )


def test_trace_independently_blocks_every_free_json_sensitive_reference_variant(
    client,
    auth_headers,
):
    trace_id = "trace_free_json_sensitive_variants"
    safe_run_id = "run_free_json_safe"
    sensitive_run_id = "run_free_json_enrollment"
    sensitive_agent_id = "agent_free_json_voiceprints"
    safe_agent_id = "agent_free_json_safe"
    review_task_id = "hrt_free_json_sensitive"
    review_decision_id = "HRD-DECISION-SECRET"
    secret_values = {
        sensitive_run_id,
        "VP-AGENT-WRITE-SECRET",
        "VP-TOOL-SECRET",
        review_decision_id,
        "VP-TRACE-REF-SECRET",
        "VP-AUDIT-SECRET",
        "VP-OUTBOX-SECRET",
        "VP-COLLISION-SECRET",
        "RAW-AGENT-SECRET",
        "RAW-TOOL-SECRET",
        "RAW-DECISION-SECRET",
        "RAW-OUTBOX-SECRET",
    }
    with SessionLocal.begin() as session:
        session.add_all(
            [
                RunRecord(
                    run_id=safe_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    run_type="label_optimization",
                    status="pending",
                    trace_id=trace_id,
                    payload={"affected_objects": [{"type": "data_asset", "id": "asset-safe"}]},
                ),
                RunRecord(
                    run_id=sensitive_run_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    run_type="label_optimization",
                    status="pending",
                    trace_id=trace_id,
                    payload={"enrollment_id": "VP-RUN-SECRET"},
                ),
                AgentRun(
                    agent_run_id=sensitive_agent_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id=trace_id,
                    payload={
                        "source_run_id": safe_run_id,
                        "write_policy": {"voiceprints": [{"id": "VP-AGENT-WRITE-SECRET"}]},
                    },
                ),
                AgentRun(
                    agent_run_id=safe_agent_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id=trace_id,
                    payload={
                        "source_run_id": safe_run_id,
                        "input_refs": [{"type": "data_asset", "id": "asset-safe"}],
                        "write_policy": {"opaque": "RAW-AGENT-SECRET"},
                    },
                ),
                ToolCall(
                    tool_call_id="tool_free_json_sensitive",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="planned",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_id,
                        "dispatch": {
                            "result_ref": {
                                "refType": "voiceprint_profile",
                                "refId": "VP-TOOL-SECRET",
                            }
                        },
                    },
                ),
                ToolCall(
                    tool_call_id="tool_free_json_projection",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_id,
                        "tool": "safe_projection_tool",
                        "dispatch": {"opaque": "RAW-TOOL-SECRET"},
                    },
                ),
                HumanReviewTask(
                    review_task_id=review_task_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="pending",
                    trace_id=trace_id,
                    payload={"queue": "amount_conflict"},
                ),
                HumanReviewDecision(
                    decision_id=review_decision_id,
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    review_task_id=review_task_id,
                    terminal_review_task_id=review_task_id,
                    status="accepted",
                    trace_id=trace_id,
                    payload={"review_task_id": review_task_id},
                ),
                AgentDecision(
                    decision_id="decision_free_json_sensitive",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_id,
                        "result_ref": {"review_decision_id": review_decision_id},
                    },
                ),
                AgentDecision(
                    decision_id="decision_free_json_projection",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="success",
                    trace_id=trace_id,
                    payload={
                        "agent_run_id": safe_agent_id,
                        "decision_type": "safe_projection",
                        "result_ref": {"opaque": "RAW-DECISION-SECRET"},
                    },
                ),
                TraceRef(
                    trace_ref_id="trace_ref_free_json_sensitive",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id=trace_id,
                    payload={
                        "ref_role": "input",
                        "type": "voiceprint_profile",
                        "id": "VP-TRACE-REF-SECRET",
                    },
                ),
                TraceRef(
                    trace_ref_id="trace_ref_normalization_collision",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id=trace_id,
                    payload={
                        "ref_role": "input",
                        "type": "voiceprint_profile",
                        "id": "VP-COLLISION-SECRET",
                        "Type": "data_asset",
                        "Id": "asset-safe",
                    },
                ),
                TraceRef(
                    trace_ref_id="trace_ref_uppercase_prefix_collision",
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    status="active",
                    trace_id=trace_id,
                    payload={
                        "ref_role": "input",
                        "refType": "data_asset",
                        "refId": "asset-safe",
                        "REFType": "voiceprint_profile",
                        "REFId": "VP-UPPER-COLLISION-SECRET",
                    },
                ),
                AuditLog(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    actor_id="u_admin_001",
                    action="voiceprint.updated",
                    object_type="voiceprint_profile",
                    object_id="VP-AUDIT-SECRET",
                    result="success",
                    trace_id=trace_id,
                ),
                OutboxEvent(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    event_type="label_versions.agent-dispatch",
                    aggregate_type="label_versions",
                    aggregate_id="lv-free-json-sensitive",
                    status="pending",
                    payload={
                        "trace_id": trace_id,
                        "adapter_dispatch": {"voiceprints": [{"id": "VP-OUTBOX-SECRET"}]},
                    },
                    dispatch_idempotency_key="dispatch-free-json-sensitive",
                ),
                OutboxEvent(
                    tenant_id="aurora_auto",
                    project_id="sales_qa",
                    event_type="label_versions.safe-dispatch",
                    aggregate_type="label_versions",
                    aggregate_id="lv-free-json-safe",
                    status="pending",
                    payload={
                        "trace_id": trace_id,
                        "adapter_dispatch": {"opaque": "RAW-OUTBOX-SECRET"},
                    },
                    dispatch_idempotency_key="dispatch-free-json-projection",
                ),
            ]
        )

    privileged = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)
    assert privileged.status_code == 200, privileged.text
    assert all(
        secret not in privileged.text
        for secret in {
            "VP-AGENT-WRITE-SECRET",
            "VP-TOOL-SECRET",
            "VP-OUTBOX-SECRET",
            "RAW-AGENT-SECRET",
            "RAW-TOOL-SECRET",
            "RAW-DECISION-SECRET",
            "RAW-OUTBOX-SECRET",
        }
    )
    assert all(
        object_id in privileged.text
        for object_id in {
            sensitive_run_id,
            review_decision_id,
            "VP-TRACE-REF-SECRET",
            "VP-AUDIT-SECRET",
            "VP-COLLISION-SECRET",
        }
    )
    assert "trace_ref_uppercase_prefix_collision" in privileged.text

    model_trace = client.get(
        f"/api/v1/traces/{trace_id}",
        headers=_session_headers("model-token"),
    )
    assert model_trace.status_code == 200, model_trace.text
    assert safe_run_id in model_trace.text
    assert safe_agent_id in model_trace.text
    assert all(
        secret not in model_trace.text for secret in {*secret_values, "VP-UPPER-COLLISION-SECRET"}
    )
    model_spans = model_trace.json()["data"]["spans"]
    visible_projection_ids = {span.get("id") for span in model_spans}
    assert {
        safe_agent_id,
        "tool_free_json_projection",
        "decision_free_json_projection",
    } <= visible_projection_ids
    assert all(
        field not in span
        for span in model_spans
        for field in (
            "adapter_dispatch",
            "dispatch",
            "input_refs",
            "result",
            "result_ref",
            "write_policy",
        )
    )


def test_client_trace_collision_creates_distinct_server_roots_and_link_records(
    client,
    auth_headers,
):
    client_trace_id = "trace_client_collision"
    client_correlation_id = "correlation_client_collision"

    def create_work_item(index: int):
        return client.post(
            "/api/v1/work-items",
            json={"id": f"work_trace_collision_{index}", "title": f"Trace collision {index}"},
            headers={
                **auth_headers,
                "Idempotency-Key": f"trace-collision-{index}",
                "X-Trace-Id": client_trace_id,
                "X-Correlation-Id": client_correlation_id,
            },
        )

    first = create_work_item(1)
    second = create_work_item(2)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text

    first_root = first.json()["meta"]["trace_id"]
    second_root = second.json()["meta"]["trace_id"]
    assert first_root != second_root
    assert client_trace_id not in {first_root, second_root}
    for response, root in ((first, first_root), (second, second_root)):
        assert response.headers["X-Trace-Id"] == root
        assert response.json()["data"]["trace_id"] == root
        assert response.json()["data"]["parent_trace_id"] == client_trace_id
        assert response.json()["data"]["correlation_id"] == client_correlation_id

    with SessionLocal() as session:
        links = list(
            session.scalars(
                select(TraceRef).where(TraceRef.trace_id.in_([first_root, second_root]))
            )
        )
    assert {link.trace_id for link in links} == {first_root, second_root}
    assert {link.payload["root_trace_id"] for link in links} == {first_root, second_root}
    assert {link.payload["parent_trace_id"] for link in links} == {client_trace_id}
    assert {link.payload["correlation_id"] for link in links} == {client_correlation_id}

    first_trace = client.get(f"/api/v1/traces/{first_root}", headers=auth_headers)
    second_trace = client.get(f"/api/v1/traces/{second_root}", headers=auth_headers)
    client_trace = client.get(f"/api/v1/traces/{client_trace_id}", headers=auth_headers)
    assert first_trace.status_code == 200
    assert second_trace.status_code == 200
    assert client_trace.status_code == 200
    for response, root in ((first_trace, first_root), (second_trace, second_root)):
        link_span = next(
            span for span in response.json()["data"]["spans"] if span["kind"] == "trace_ref"
        )
        assert link_span["root_trace_id"] == root
        assert link_span["parent_trace_id"] == client_trace_id
        assert link_span["correlation_id"] == client_correlation_id
    assert client_trace.json()["data"]["spans"] == []


def test_invalid_client_trace_is_rejected_under_a_server_generated_root(client, auth_headers):
    invalid_trace = "x" * 129
    response = client.get(
        "/api/v1/settings",
        headers={**auth_headers, "X-Trace-Id": invalid_trace},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_TRACE_CONTEXT"
    assert response.json()["error"]["trace_id"] != invalid_trace
    assert response.headers["X-Trace-Id"] == response.json()["error"]["trace_id"]


def test_middleware_uses_server_root_before_business_context(client):
    client_trace = "trace_untrusted_early_request"
    response = client.get(
        "/route-that-does-not-exist",
        headers={"X-Trace-Id": client_trace},
    )

    assert response.status_code == 404
    root_trace = response.headers["X-Trace-Id"]
    assert root_trace.startswith("trace_")
    assert root_trace != client_trace
