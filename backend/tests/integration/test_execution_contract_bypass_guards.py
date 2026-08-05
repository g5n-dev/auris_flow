from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from sqlalchemy import func, select

import app.services.run_service as run_service
from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    HotwordPackVersion,
    IdempotencyRecord,
    JsonResource,
    OutboxEvent,
    RunRecord,
)
from app.services.execution_contract_registry import execution_contract_registry


def _headers(auth_headers: Mapping[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _persistent_counts() -> dict[str, int]:
    with SessionLocal() as session:
        return {
            "runs": session.scalar(select(func.count()).select_from(RunRecord)) or 0,
            "outbox": session.scalar(select(func.count()).select_from(OutboxEvent)) or 0,
            "audits": session.scalar(select(func.count()).select_from(AuditLog)) or 0,
            "idempotency": (
                session.scalar(select(func.count()).select_from(IdempotencyRecord)) or 0
            ),
            "json_resources": (session.scalar(select(func.count()).select_from(JsonResource)) or 0),
        }


def _assert_contract_error(response, *, event_type: str, run_type: str) -> None:
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "EXECUTION_CONTRACT_NOT_CONFIGURED"
    assert error["retryable"] is False
    assert error["details"] == [
        {
            "event_type": event_type,
            "run_type": run_type,
            "requested_contract": None,
        }
    ]


def test_hotword_build_preflight_rolls_back_version_and_all_write_side_effects(
    client,
    auth_headers,
) -> None:
    pack = client.post(
        "/api/v1/hotword-packs",
        json={"name": "未配置构建执行器", "language": "zh-CN", "domain": "contract-guard"},
        headers=_headers(auth_headers, "guard-build-pack"),
    )
    assert pack.status_code == 201, pack.text
    version = client.post(
        f"/api/v1/hotword-packs/{pack.json()['data']['pack_id']}/versions",
        json={"version": "v1", "task_type_id": "task_sales_quality"},
        headers=_headers(auth_headers, "guard-build-version"),
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["data"]["version_id"]
    before = _persistent_counts()

    response = client.patch(
        f"/api/v1/hotword-pack-versions/{version_id}",
        json={
            "expected_resource_version": 1,
            "status": "validating",
            "provider": "auris-audio-stack",
        },
        headers=_headers(auth_headers, "guard-build-request"),
    )

    _assert_contract_error(
        response,
        event_type="hotword_pack_version.build-requested",
        run_type="hotword_build",
    )
    assert _persistent_counts() == before
    with SessionLocal() as session:
        stored = session.get(HotwordPackVersion, version_id)
        assert stored is not None
        assert stored.status == "draft"
        assert stored.resource_version == 1
        assert (stored.payload or {}).get("build_run_id") is None


@pytest.mark.parametrize(
    ("case", "method", "path", "payload", "event_type", "run_type"),
    [
        (
            "analysis",
            "post",
            "/api/v1/hotword-analysis-runs",
            {},
            "hotword_analysis.requested",
            "hotword_analysis",
        ),
        (
            "evaluation",
            "post",
            "/api/v1/hotword-pack-versions/hotword_missing/eval-runs",
            {
                "eval_dataset_id": "evalset_missing",
                "provider": "auris-audio-stack",
                "expected_resource_version": 1,
            },
            "hotword_pack_version.eval-requested",
            "hotword_eval",
        ),
        (
            "publish",
            "post",
            "/api/v1/hotword-pack-versions/hotword_missing/publish",
            {
                "expected_resource_version": 1,
                "eval_run_id": "hweval_missing",
                "confirmation": "publish",
            },
            "hotword_pack_version.publish-requested",
            "hotword_publish",
        ),
        (
            "label-optimization",
            "post",
            "/api/v1/label-optimization-trigger-scans",
            {
                "label_version_id": "label_missing",
                "prompt_version_id": "prompt_missing",
                "model_version": "model-missing",
                "aggregation_policy_version_id": "policy_missing",
                "eval_dataset_version_id": "evalset_missing",
            },
            "agent_run.requested",
            "label_optimization",
        ),
    ],
)
def test_unconfigured_business_execution_preflight_has_no_persistent_side_effects(
    client,
    auth_headers,
    case: str,
    method: str,
    path: str,
    payload: dict[str, Any],
    event_type: str,
    run_type: str,
) -> None:
    before = _persistent_counts()

    response = client.request(
        method,
        path,
        json=payload,
        headers=_headers(auth_headers, f"guard-{case}"),
    )

    _assert_contract_error(response, event_type=event_type, run_type=run_type)
    assert _persistent_counts() == before


def test_external_actor_cannot_forge_diagnostic_generic_execution_in_production(
    client,
    auth_headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        run_service,
        "is_production_environment",
        lambda _app_env: True,
    )
    before = _persistent_counts()

    response = client.post(
        "/api/v1/settings/provider-tests",
        json={"provider": "self_hosted", "execution_mode": "diagnostic"},
        headers=_headers(auth_headers, "guard-forged-diagnostic"),
    )

    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["code"] == "DIAGNOSTIC_EXECUTION_FORBIDDEN"
    assert error["retryable"] is False
    assert error["details"] == [
        {
            "event_type": "provider_test.requested",
            "run_type": "provider_test",
        }
    ]
    assert _persistent_counts() == before


def test_contract_preflight_precedes_stale_idempotency_replay(
    client,
    auth_headers,
    configured_test_business_execution_contracts,
) -> None:
    del configured_test_business_execution_contracts
    headers = _headers(auth_headers, "guard-analysis-stale-replay")
    created = client.post("/api/v1/hotword-analysis-runs", json={}, headers=headers)
    assert created.status_code == 202, created.text

    configured_contracts = execution_contract_registry._contracts
    execution_contract_registry._contracts = tuple(
        contract
        for contract in configured_contracts
        if (
            contract.event_type,
            contract.run_type,
        )
        != ("hotword_analysis.requested", "hotword_analysis")
    )
    try:
        before = _persistent_counts()
        replay = client.post("/api/v1/hotword-analysis-runs", json={}, headers=headers)

        _assert_contract_error(
            replay,
            event_type="hotword_analysis.requested",
            run_type="hotword_analysis",
        )
        assert _persistent_counts() == before
    finally:
        execution_contract_registry._contracts = configured_contracts
