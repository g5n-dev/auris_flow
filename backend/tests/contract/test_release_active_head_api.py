from __future__ import annotations

from app.core.database import SessionLocal
from app.models import PromptAsset, ReleaseBundleHead, ReleaseCommand, ReleaseDeployment, RunRecord
from app.workers.outbox_worker import process_aggregate_events
from tests.contract.test_prompt_release_closed_loop_api import (
    PROJECT_ID,
    TENANT_ID,
    _headers,
    _release_body,
    _seed_release_dependencies,
)


def _command_for_deployment(deployment_id: str) -> ReleaseCommand:
    with SessionLocal() as session:
        command = (
            session.query(ReleaseCommand)
            .filter(
                ReleaseCommand.tenant_id == TENANT_ID,
                ReleaseCommand.project_id == PROJECT_ID,
                ReleaseCommand.deployment_id == deployment_id,
                ReleaseCommand.active_slot == "active",
            )
            .one()
        )
        session.expunge(command)
        return command


def _ack_command(client, auth_headers: dict[str, str], command: ReleaseCommand) -> None:
    assert process_aggregate_events([command.run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, command.run_id)
        assert run is not None
        assert run.status == "submitted"
        dispatch = run.payload["dispatch"]
        external_id = dispatch["details"]["external_run_id"]
    response = client.post(
        f"/api/v1/runs/{command.run_id}/completion-receipts",
        json={
            "status": "success",
            "adapter": "dagster",
            "completion_receipt_id": f"receipt-{command.command_id}",
            "external_id": external_id,
            "result_ref": {
                "release_command_id": command.command_id,
                "command_sha256": command.command_sha256,
                "deployment_id": command.deployment_id,
                "environment": command.environment,
                "action": command.action,
                "bundle_sha256": command.payload["bundle_sha256"],
                "applied": True,
            },
        },
        headers=_headers(
            auth_headers,
            f"ack-{command.command_id}",
            token="system-token",
        ),
    )
    assert response.status_code == 200, response.text


def test_release_creation_and_gray_are_not_terminal_before_trusted_ack(client, auth_headers):
    _seed_release_dependencies()
    created = client.post(
        "/api/v1/release-deployments",
        json=_release_body("rd_two_phase_no_ack"),
        headers=_headers(auth_headers, "release-two-phase-no-ack"),
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["status"] == "pending"

    publish_command = _command_for_deployment("rd_two_phase_no_ack")
    assert publish_command.action == "publish"
    with SessionLocal() as session:
        deployment = session.get(ReleaseDeployment, "rd_two_phase_no_ack")
        assert deployment is not None
        assert deployment.status == "pending"
        assert deployment.rollout_percentage == 0

    _ack_command(client, auth_headers, publish_command)
    current = client.get(
        "/api/v1/release-deployments/rd_two_phase_no_ack",
        headers=auth_headers,
    )
    assert current.status_code == 200
    assert current.json()["data"]["status"] == "shadowing"

    gray = client.post(
        "/api/v1/release-deployments/rd_two_phase_no_ack/transitions",
        json={
            "action": "approve-gray",
            "reason": "批准灰度但等待执行 ACK",
            "expected_status": "shadowing",
            "monitor_metrics": {},
        },
        headers=_headers(auth_headers, "release-gray-no-ack"),
    )
    assert gray.status_code == 202, gray.text
    assert gray.json()["data"]["status"] == "materializing"
    assert gray.json()["data"]["rollout_percentage"] == 0
    gray_command = _command_for_deployment("rd_two_phase_no_ack")
    assert gray_command.action == "approve-gray"


def test_initial_lkg_bootstrap_is_explicit_single_use_and_sets_consistent_head(
    client, auth_headers
):
    _seed_release_dependencies()
    with SessionLocal() as session:
        head = session.get(ReleaseBundleHead, "rbh_prompt_release_production")
        assert head is not None
        session.delete(head)
        legacy = session.get(ReleaseDeployment, "rd_default_stable_target")
        assert legacy is not None
        legacy.status = "blocked"
        legacy.stage = "blocked"
        legacy.rollout_percentage = 0
        legacy.blocked_reasons = [
            {"code": "ROLLBACK_TARGET_REQUIRED", "message": "初始生产 LKG 尚未建立"},
            {"code": "RELEASE_ACTIVE_HEAD_REQUIRED", "message": "active head 尚未建立"},
        ]
        asset = session.get(PromptAsset, "pa_release_contract")
        assert asset is not None
        asset.current_version_id = None
        session.commit()

    bootstrapped = client.post(
        "/api/v1/release-deployments/rd_default_stable_target/bootstrap-active-head",
        json={
            "confirmation": "bootstrap-last-known-good",
            "reason": "首次建立经审计生产 LKG",
            "expected_no_active_head": True,
        },
        headers=_headers(auth_headers, "release-bootstrap-lkg"),
    )
    assert bootstrapped.status_code == 200, bootstrapped.text
    assert bootstrapped.json()["data"]["status"] == "completed"
    with SessionLocal() as session:
        head = (
            session.query(ReleaseBundleHead)
            .filter_by(
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                environment="production",
            )
            .one()
        )
        asset = session.get(PromptAsset, "pa_release_contract")
        assert head.active_deployment_id == "rd_default_stable_target"
        assert head.prompt_version_id == "pv_release_contract"
        assert head.generation == 1
        assert head.bootstrapped is True
        assert asset is not None and asset.current_version_id == head.prompt_version_id

    replay_forbidden = client.post(
        "/api/v1/release-deployments/rd_default_stable_target/bootstrap-active-head",
        json={
            "confirmation": "bootstrap-last-known-good",
            "reason": "不得把 bootstrap 当日常发布",
            "expected_no_active_head": True,
        },
        headers=_headers(auth_headers, "release-bootstrap-lkg-again"),
    )
    assert replay_forbidden.status_code == 409
    assert replay_forbidden.json()["error"]["code"] == "RELEASE_HEAD_ALREADY_EXISTS"
