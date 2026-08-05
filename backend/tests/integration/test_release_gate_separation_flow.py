from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

import app.services.release_gate_service as release_gate_service
from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import AuditLog, OutboxEvent, Project, RunRecord, User
from app.services.release_gate_service import revalidate_control_plane_release
from app.workers.outbox_worker import process_aggregate_events

SECOND_ADMIN_ID = "u_annotator_001"

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")


def _headers(
    auth_headers: dict[str, str],
    *,
    key: str,
    token: str = "dev-token",
) -> dict[str, str]:
    return {
        **auth_headers,
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": key,
    }


def _promote_second_admin() -> str:
    with SessionLocal.begin() as session:
        user = session.get(User, SECOND_ADMIN_ID)
        project = session.get(Project, "sales_qa")
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project_data = deepcopy(project.data)
        project_data["members"] = [
            {
                **member,
                "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
            }
            if member.get("user_id") == SECOND_ADMIN_ID
            else member
            for member in project_data.get("members", [])
        ]
        project.data = project_data

    profile = DevAuthProfile(
        email="release-integration-admin@auris.local",
        user_id=SECOND_ADMIN_ID,
        name="发布集成复核管理员",
        role_label="项目管理员",
        initials="审",
        roles=("annotator", "review_arbitrator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def _request_task_publish(client, auth_headers, *, suffix: str) -> str:
    version_id = f"task_version_release_integrity_{suffix}"
    created = client.post(
        "/api/v1/task-versions",
        json={
            "task_version_id": version_id,
            "task_type_id": "task_sales_quality",
            "version": f"release-integrity-{suffix}",
        },
        headers=_headers(auth_headers, key=f"release-integrity-version-{suffix}"),
    )
    assert created.status_code == 201, created.text
    requested = client.post(
        f"/api/v1/task-versions/{version_id}/publish",
        json={"reason": "验证发布审批不可篡改"},
        headers=_headers(auth_headers, key=f"release-integrity-request-{suffix}"),
    )
    assert requested.status_code == 202, requested.text
    return requested.json()["data"]["run_id"]


def test_settings_publish_distinct_admin_succeeds_and_replay_is_actor_bound(
    client,
    auth_headers,
) -> None:
    second_admin_token = _promote_second_admin()
    draft_id = "settings_draft_distinct_admin_release"
    draft = client.post(
        "/api/v1/settings/drafts",
        json={
            "settings_draft_id": draft_id,
            "setting_id": "model-chain",
            "changes": {"provider": "distinct_admin_provider"},
            "reason": "双人发布集成验证",
        },
        headers=_headers(auth_headers, key="distinct-admin-settings-draft"),
    )
    assert draft.status_code == 201, draft.text
    requested = client.post(
        "/api/v1/settings/publish-requests",
        json={"draft_id": draft_id},
        headers=_headers(
            auth_headers,
            key="distinct-admin-settings-request",
            token=second_admin_token,
        ),
    )
    assert requested.status_code == 202, requested.text
    run_id = requested.json()["data"]["run_id"]
    assert requested.json()["data"]["release_gate"]["requested_by"] == SECOND_ADMIN_ID
    assert process_aggregate_events([run_id]) == 0

    decision_reason = "主管理员完成独立复核，联系 13800138000。" + ("复核说明" * 80)
    decision_body = {"decision": "approved", "reason": decision_reason}
    decision_headers = _headers(auth_headers, key="distinct-admin-settings-approve")
    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json=decision_body,
        headers=decision_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["release_gate"]["decision"]["actor_id"] == ("u_admin_001")
    public_reason = approved.json()["data"]["release_gate"]["decision"]["reason"]
    assert public_reason != decision_reason
    assert "13800138000" not in public_reason
    assert public_reason.endswith("[TRUNCATED]")

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.payload["release_gate"]["decision"]["reason"] == decision_reason
        audit = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "settings_publish.release_gate_decided",
                AuditLog.object_id == run_id,
            )
            .one()
        )
        assert audit.after_json == {
            "release_gate_proof": {
                "request_sha256": approved.json()["data"]["release_gate"]["request_sha256"],
                "status": "approved",
                "decision_sha256": approved.json()["data"]["release_gate"]["decision"][
                    "decision_sha256"
                ],
                "decision_value": "approved",
                "actor_id": "u_admin_001",
            }
        }
        assert "13800138000" not in str(audit.before_json)
        assert "13800138000" not in str(audit.after_json)

    same_actor_replay = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json=decision_body,
        headers=decision_headers,
    )
    assert same_actor_replay.status_code == 200, same_actor_replay.text
    assert same_actor_replay.json() == approved.json()

    cross_actor_replay = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json=decision_body,
        headers=_headers(
            auth_headers,
            key="distinct-admin-settings-approve",
            token=second_admin_token,
        ),
    )
    assert cross_actor_replay.status_code == 409
    assert cross_actor_replay.json()["error"]["code"] == (
        "RELEASE_DECISION_IDEMPOTENCY_ACTOR_CONFLICT"
    )

    assert process_aggregate_events([run_id]) == 1
    setting = client.get("/api/v1/settings/model-chain", headers=auth_headers)
    assert setting.status_code == 200, setting.text
    assert setting.json()["data"]["provider"] == "distinct_admin_provider"
    assert setting.json()["data"]["published_by"] == "u_admin_001"


def test_control_plane_release_outbox_is_not_claimable_before_approval(
    client,
    auth_headers,
) -> None:
    second_admin_token = _promote_second_admin()
    run_id = _request_task_publish(client, auth_headers, suffix="decision-fence")

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "blocked"
        assert event.status == "blocked"
        assert event.delivery_state == "confirmed"
        assert event.attempt_count == 0
        assert event.claim_token is None
        assert event.claimed_by is None

    # A fast worker must not obtain an old pre-decision snapshot and race the
    # approval transaction. The decision is the only operation that requeues
    # this durable event.
    assert process_aggregate_events([run_id]) == 0

    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "独立管理员确认发布证据与回滚点"},
        headers=_headers(
            auth_headers,
            key="release-integrity-decision-fence-approve",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "pending"
    assert approved.json()["data"]["release_gate"]["status"] == "approved"

    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert event.status == "pending"
        assert event.delivery_state == "ready"

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "success"
        assert run.payload["release_gate"]["status"] == "approved"


def test_release_gate_rejects_non_human_system_actor_without_mutating_state(
    client,
    auth_headers,
) -> None:
    run_id = _request_task_publish(client, auth_headers, suffix="human-approver-only")

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        before_run = (run.status, deepcopy(run.payload))
        before_event = (
            event.status,
            event.delivery_state,
            deepcopy(event.payload),
            event.processed_at,
        )
        before_audit_count = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "task_version_publish.release_gate_decided",
                AuditLog.object_id == run_id,
            )
            .count()
        )

    rejected = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "服务身份不得代替自然人审批"},
        headers=_headers(
            auth_headers,
            key="release-integrity-system-actor-rejected",
            token="system-token",
        ),
    )

    assert rejected.status_code == 403, rejected.text
    assert rejected.json()["error"]["code"] == "RELEASE_APPROVAL_HUMAN_ADMIN_REQUIRED"
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert (run.status, run.payload) == before_run
        assert (
            event.status,
            event.delivery_state,
            event.payload,
            event.processed_at,
        ) == before_event
        assert (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "task_version_publish.release_gate_decided",
                AuditLog.object_id == run_id,
            )
            .count()
            == before_audit_count
        )


def test_release_decision_does_not_clear_an_active_outbox_lease(
    client,
    auth_headers,
) -> None:
    second_admin_token = _promote_second_admin()
    run_id = _request_task_publish(client, auth_headers, suffix="active-lease")
    claim_fixture = "active-release-claim-fixture"

    with SessionLocal.begin() as session:
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        event.status = "processing"
        event.delivery_state = "dispatching"
        event.claim_token = claim_fixture
        event.claimed_by = "release-gate-test-worker"
        event.claimed_at = datetime.now(UTC)
        event.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        event.lease_generation += 1

    decision = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "不能覆盖仍活跃的 Worker 租约"},
        headers=_headers(
            auth_headers,
            key="release-integrity-active-lease-approve",
            token=second_admin_token,
        ),
    )
    assert decision.status_code == 409, decision.text
    assert decision.json()["error"]["code"] == "RELEASE_GATE_EVENT_IN_FLIGHT"

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "blocked"
        assert run.payload["release_gate"]["status"] == "awaiting_decision"
        assert event.status == "processing"
        assert event.claim_token == claim_fixture
        assert event.claimed_by == "release-gate-test-worker"


def test_decision_api_rejects_tampered_requested_by(client, auth_headers) -> None:
    run_id = _request_task_publish(client, auth_headers, suffix="api-tamper")
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        gate = deepcopy(run.payload["release_gate"])
        gate["requested_by"] = SECOND_ADMIN_ID
        run.payload = {**run.payload, "release_gate": gate}

    tampered = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "篡改发起人后不得通过"},
        headers=_headers(auth_headers, key="release-integrity-tampered-requester"),
    )
    assert tampered.status_code == 409
    assert tampered.json()["error"]["code"] == "RELEASE_GATE_STALE"
    assert tampered.json()["error"]["details"][0]["reason"] == ("release_request_binding_changed")


def test_worker_revalidation_rejects_forged_self_approval(client, auth_headers) -> None:
    run_id = _request_task_publish(client, auth_headers, suffix="worker-tamper")
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        gate = deepcopy(run.payload["release_gate"])
        gate["status"] = "approved"
        gate["decision"] = {
            "value": "approved",
            "reason": "伪造自批载荷",
            "actor_id": "u_admin_001",
            "roles": ["project_admin"],
            "decided_at": "2026-07-14T00:00:00+00:00",
            "trace_id": run.trace_id,
        }
        run.payload = {**run.payload, "release_gate": gate}
        session.flush()

        checked = revalidate_control_plane_release(session, run)

    assert checked == {
        "allowed": False,
        "reason": "release_approval_separation_failed",
    }


def test_worker_retries_until_committed_release_audit_is_visible(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    second_admin_token = _promote_second_admin()
    run_id = _request_task_publish(client, auth_headers, suffix="audit-visibility")
    assert process_aggregate_events([run_id]) == 0

    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "独立管理员确认发布证据"},
        headers=_headers(
            auth_headers,
            key="release-integrity-audit-visibility-approve",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text

    original_audit_match = release_gate_service._release_decision_audit_matches
    audit_reads = 0

    def delayed_audit_match(*args, **kwargs):
        nonlocal audit_reads
        audit_reads += 1
        if audit_reads == 1:
            return None
        return original_audit_match(*args, **kwargs)

    monkeypatch.setattr(
        release_gate_service,
        "_release_decision_audit_matches",
        delayed_audit_match,
    )

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal.begin() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "running"
        assert run.payload["dispatch_state"] == "retry_wait"
        assert run.payload["error_code"] == "RELEASE_DECISION_AUDIT_NOT_VISIBLE"
        assert event.status == "pending"
        assert event.delivery_state == "ready"
        assert event.last_error is not None
        assert "RELEASE_DECISION_AUDIT_NOT_VISIBLE" in event.last_error
        event.available_at = datetime.now(UTC) - timedelta(seconds=2)

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "success"
        assert (
            run.payload["release_materialization"]["resource_id"] == run.payload["task_version_id"]
        )
        assert event.status == "processed"
        assert audit_reads >= 2


def test_worker_accepts_canonical_sha256_proof_with_phone_like_digits(
    client,
    auth_headers,
    monkeypatch,
) -> None:
    digest = "a13800138000b" + ("c" * 51)
    monkeypatch.setattr(
        release_gate_service,
        "_release_decision_sha256",
        lambda *_args, **_kwargs: digest,
    )
    second_admin_token = _promote_second_admin()
    run_id = _request_task_publish(client, auth_headers, suffix="sha256-phone-canary")
    assert process_aggregate_events([run_id]) == 0

    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "独立管理员确认摘要完整性"},
        headers=_headers(
            auth_headers,
            key="release-integrity-sha256-phone-canary-approve",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["release_gate"]["decision"]["decision_sha256"] == digest
    with SessionLocal() as session:
        audit = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "task_version_publish.release_gate_decided",
                AuditLog.object_id == run_id,
            )
            .one()
        )
        assert audit.after_json["release_gate_proof"]["decision_sha256"] == digest

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "success"


def test_worker_fail_closes_on_release_audit_proof_mismatch(client, auth_headers) -> None:
    second_admin_token = _promote_second_admin()
    run_id = _request_task_publish(client, auth_headers, suffix="audit-mismatch")
    assert process_aggregate_events([run_id]) == 0

    approved = client.post(
        f"/api/v1/runs/{run_id}/decisions",
        json={"decision": "approved", "reason": "独立管理员确认发布证据"},
        headers=_headers(
            auth_headers,
            key="release-integrity-audit-mismatch-approve",
            token=second_admin_token,
        ),
    )
    assert approved.status_code == 200, approved.text

    with SessionLocal.begin() as session:
        audit = (
            session.query(AuditLog)
            .filter(
                AuditLog.action == "task_version_publish.release_gate_decided",
                AuditLog.object_id == run_id,
            )
            .one()
        )
        after_json = deepcopy(audit.after_json)
        after_json["release_gate_proof"]["decision_sha256"] = "f" * 64
        audit.after_json = after_json

    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        event = session.query(OutboxEvent).filter(OutboxEvent.aggregate_id == run_id).one()
        assert run is not None
        assert run.status == "blocked"
        assert run.payload["release_dispatch_gate"]["reason"] == ("release_decision_audit_mismatch")
        assert event.status == "blocked"
        assert event.delivery_state == "confirmed"
        assert event.attempt_count == 1
        assert event.last_error == "run is blocked by release gate or human confirmation"
