from __future__ import annotations

import hashlib
from datetime import UTC
from types import SimpleNamespace

import pytest

from app.core.database import SessionLocal
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import (
    PromptAsset,
    ReleaseBundleHead,
    ReleaseBundleHeadEvent,
    ReleaseDeployment,
)
from app.services.prompt_release_service import (
    _activate_release_head,
    release_head_event_content_sha256,
    release_head_event_hash_document,
)
from tests.contract.test_prompt_release_closed_loop_api import (
    PROJECT_ID,
    TENANT_ID,
    _ack_release_command,
    _headers,
    _release_body,
    _seed_ctx,
    _seed_release_dependencies,
)


def test_bootstrap_appends_generation_one_activation_ledger_event(client, auth_headers):
    _seed_release_dependencies()
    with SessionLocal() as session:
        head = session.get(ReleaseBundleHead, "rbh_prompt_release_production")
        assert head is not None
        session.delete(head)
        deployment = session.get(ReleaseDeployment, "rd_default_stable_target")
        assert deployment is not None
        deployment.status = "blocked"
        deployment.stage = "blocked"
        deployment.rollout_percentage = 0
        deployment.blocked_reasons = [
            {"code": "ROLLBACK_TARGET_REQUIRED", "message": "initial LKG missing"},
            {"code": "RELEASE_ACTIVE_HEAD_REQUIRED", "message": "active head missing"},
        ]
        asset = session.get(PromptAsset, "pa_release_contract")
        assert asset is not None
        asset.current_version_id = None
        session.commit()

    response = client.post(
        "/api/v1/release-deployments/rd_default_stable_target/bootstrap-active-head",
        json={
            "confirmation": "bootstrap-last-known-good",
            "reason": "建立首个可审计的生产 Head",
            "expected_no_active_head": True,
        },
        headers=_headers(auth_headers, "bootstrap-with-ledger"),
    )
    assert response.status_code == 200, response.text

    with SessionLocal() as session:
        event = (
            session.query(ReleaseBundleHeadEvent)
            .filter_by(
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
            )
            .one()
        )
        expected_head_id = public_id_from_hex(
            "rbh",
            hashlib.sha256(f"{TENANT_ID}:{PROJECT_ID}:production".encode()).hexdigest(),
            suffix_length=24,
        )
        assert session.get(ReleaseBundleHead, expected_head_id) is not None
        assert event.head_event_id == public_id_from_hex(
            "rbhe",
            event.content_sha256,
            suffix_length=24,
        )
        assert event.generation == 1
        assert event.previous_generation is None
        assert event.action == "bootstrap"
        assert event.activation_status == "active"
        assert event.old_deployment_id is None
        assert event.new_deployment_id == "rd_default_stable_target"
        assert event.new_label_version_id == "lv_prompt_release_contract"
        assert event.command_id is None
        assert event.completion_receipt_id is None
        assert event.actor_id == "u_admin_001"
        assert event.root_trace_id
        assert len(event.content_sha256) == 64
        assert event.payload["head_event_schema"] == "release-bundle-head-event/v2"
        assert release_head_event_hash_document(event)
        assert release_head_event_content_sha256(event) == event.content_sha256


def test_generation_above_one_without_ledger_anchor_fails_closed():
    _seed_release_dependencies()
    with SessionLocal() as session:
        head = session.get(ReleaseBundleHead, "rbh_prompt_release_production")
        assert head is not None
        head.generation = 2
        session.commit()

    with SessionLocal() as session:
        deployment = session.get(ReleaseDeployment, "rd_default_stable_target")
        assert deployment is not None
        command = SimpleNamespace(
            command_id="rc_ledger_drift",
            action="promote",
            expected_head_generation=2,
            expected_head_deployment_id=deployment.deployment_id,
            expected_head_bundle_sha256=deployment.bundle_sha256,
            requested_by="u_admin_001",
            trace_id="trace_ledger_drift",
        )
        with pytest.raises(ApiError) as exc_info:
            _activate_release_head(
                session,
                _seed_ctx(),
                deployment,
                command=command,  # type: ignore[arg-type]
                bootstrapped=False,
                completion_receipt_id=None,
            )
        assert exc_info.value.code == "RELEASE_ACTIVATION_LEDGER_DRIFT"
        session.rollback()

    with SessionLocal() as session:
        head = session.get(ReleaseBundleHead, "rbh_prompt_release_production")
        assert head is not None and head.generation == 2
        assert session.query(ReleaseBundleHeadEvent).count() == 0


def test_rollback_ack_backfills_legacy_anchor_and_appends_receipt_bound_event(client, auth_headers):
    _seed_release_dependencies()
    created = client.post(
        "/api/v1/release-deployments",
        json=_release_body("rd_ledger_rollback"),
        headers=_headers(auth_headers, "ledger-rollback-create"),
    )
    assert created.status_code == 201, created.text
    _ack_release_command(client, auth_headers, "rd_ledger_rollback")
    requested = client.post(
        "/api/v1/release-deployments/rd_ledger_rollback/transitions",
        json={
            "action": "rollback",
            "reason": "验证回滚 ACK 与不可变激活账本同事务落账",
            "expected_status": "shadowing",
            "monitor_metrics": {},
        },
        headers=_headers(auth_headers, "ledger-rollback-request", token="system-token"),
    )
    assert requested.status_code == 202, requested.text
    _ack_release_command(client, auth_headers, "rd_ledger_rollback")

    with SessionLocal() as session:
        events = (
            session.query(ReleaseBundleHeadEvent)
            .filter_by(
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
            )
            .order_by(ReleaseBundleHeadEvent.generation)
            .all()
        )
        assert [(event.generation, event.action) for event in events] == [
            (1, "bootstrap"),
            (2, "rollback"),
        ]
        anchor, rollback = events
        assert anchor.payload["legacy_anchor_backfill"] is True
        assert anchor.effective_to == rollback.effective_from
        assert rollback.effective_to is None
        assert anchor.payload["interval_semantics"] == "[effective_from,effective_to)"
        assert rollback.payload["previous_head_event_id"] == anchor.head_event_id
        assert rollback.payload["previous_interval_closed_at"] == (
            rollback.effective_from.replace(tzinfo=UTC).isoformat()
        )
        assert rollback.previous_generation == 1
        assert rollback.command_id
        assert rollback.completion_receipt_id == f"receipt-{rollback.command_id}"
        assert rollback.old_deployment_id == "rd_default_stable_target"
        assert rollback.new_deployment_id == "rd_default_stable_target"
        assert release_head_event_content_sha256(anchor) == anchor.content_sha256
        assert release_head_event_content_sha256(rollback) == rollback.content_sha256

    head_response = client.get(
        "/api/v1/release-bundle-heads/production",
        headers=auth_headers,
    )
    assert head_response.status_code == 200, head_response.text
    head_data = head_response.json()["data"]
    assert head_data["ledger_health"]["status"] == "consistent"
    assert [item["generation"] for item in head_data["activation_timeline"]] == [1, 2]
    first_interval, current_interval = head_data["activation_timeline"]
    assert first_interval["effective_to"] == current_interval["effective_from"]
    assert current_interval["effective_to"] is None
