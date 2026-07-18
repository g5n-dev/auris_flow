from __future__ import annotations

import pytest
from sqlalchemy import select

from app.api.routers.label_optimization_orchestrator import router
from app.core.database import SessionLocal
from app.main import app
from app.models import (
    AuditLog,
    EvalDatasetVersion,
    JsonResource,
    LabelAggregationPolicyVersion,
    LabelVersion,
    OutboxEvent,
    PromptAsset,
    PromptVersion,
    RunRecord,
)

# Keep this contract runnable while the root task wires the router into ``main.py``.
# Once wired, the path check prevents a duplicate registration.
if not any(
    getattr(route, "path", None) == "/api/v1/label-optimization-trigger-scans"
    for route in app.routes
):
    app.include_router(router, prefix="/api/v1")


@pytest.fixture(autouse=True)
def _seed_locked_bundle(client):
    with SessionLocal() as session:
        session.merge(
            LabelVersion(
                label_version_id="label_v1_9_0_rc2",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                status="published",
                resource_version=1,
                trace_id="trace-opt-seed",
                payload={},
            )
        )
        session.merge(
            PromptAsset(
                prompt_asset_id="prompt_asset_labeling",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                name="标签自动优化",
                capability="labeling",
                label_version_id="label_v1_9_0_rc2",
                status="active",
                current_version_id="prompt_labeling_v7",
                trace_id="trace-opt-seed",
                payload={},
            )
        )
        session.merge(
            PromptVersion(
                prompt_version_id="prompt_labeling_v7",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                prompt_asset_id="prompt_asset_labeling",
                version="7.0.0",
                parent_version_id=None,
                label_version_id="label_v1_9_0_rc2",
                schema_version="label-output-v1",
                model_version="gpt-5-mini-2026-06-01",
                status="approved",
                template_json={"system": "json only"},
                output_schema={"type": "object"},
                generation_params={"temperature": 0},
                structured_diff={},
                source_badcase_refs=[],
                content_sha256="a" * 64,
                trace_id="trace-opt-seed",
            )
        )
        session.merge(
            LabelAggregationPolicyVersion(
                policy_version_id="agg_policy_v3",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                label_version_id="label_v1_9_0_rc2",
                policy_version="3.0.0",
                mode="l1",
                status="active",
                source_weights={"llm": 1.0},
                calibration_versions={},
                thresholds={},
                label_definitions=[{"label_id": "intent", "kind": "categorical"}],
                canonical_sha256="b" * 64,
                trace_id="trace-opt-seed",
                payload={},
            )
        )
        session.merge(
            EvalDatasetVersion(
                eval_dataset_id="evalset_label_holdout_v12",
                tenant_id="aurora_auto",
                project_id="sales_qa",
                name="标签隐藏留出集",
                capability="labeling",
                dataset_version="12.0.0",
                status="locked",
                manifest_storage_object_id="obj-opt-holdout",
                manifest_sha256="c" * 64,
                manifest_provider="local",
                manifest_bucket="auris-test",
                manifest_object_key="eval/label-holdout-v12.jsonl",
                manifest_content_type="application/jsonl",
                manifest_size_bytes=1024,
                manifest_etag="etag-opt-holdout",
                sample_count=240,
                resource_version=1,
                root_trace_id="trace-opt-seed",
                current_trace_id="trace-opt-seed",
                payload={},
            )
        )
        session.commit()
    yield


def _payload(*, reviewed_sample_count: int = 240) -> dict:
    return {
        "label_version_id": "label_v1_9_0_rc2",
        "prompt_version_id": "prompt_labeling_v7",
        "model_version": "gpt-5-mini-2026-06-01",
        "aggregation_policy_version_id": "agg_policy_v3",
        "eval_dataset_version_id": "evalset_label_holdout_v12",
        "budget": {
            "max_rounds": 3,
            "min_candidates_per_round": 2,
            "max_candidates_per_round": 5,
            "max_elapsed_seconds": 7200,
            "max_cost_micros": 8_000_000,
            "min_meaningful_gain_ppm": 20_000,
            "max_consecutive_failed_rounds": 2,
        },
        "metrics_override": {
            "reviewed_sample_count": reviewed_sample_count,
            "human_override_rate_ppm": 81_000,
            "baseline_human_override_rate_ppm": 45_000,
            "conflict_rate_ppm": 61_000,
            "json_validity_ppm": 994_000,
            "critical_recall_ppm": 930_000,
            "baseline_critical_recall_ppm": 960_000,
            "largest_failure_cluster_count": 24,
            "new_feedback_count": 60,
        },
    }


def test_trigger_scan_creates_locked_queued_run_with_audit_outbox_and_replays(
    client,
    auth_headers,
):
    headers = {**auth_headers, "Idempotency-Key": "label-opt-trigger-001"}
    response = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers=headers,
        json=_payload(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    data = body["data"]
    assert data["triggered"] is True
    assert data["status"] == "queued"
    assert data["stage"] == "queued"
    assert data["run_id"]
    assert len(data["trigger_hash"]) == 64
    assert data["blocked_reasons"] == []
    assert data["next_action"]["code"] == "generate-prompt-candidates"
    assert data["locked_versions"] == {
        "label_version_id": "label_v1_9_0_rc2",
        "prompt_version_id": "prompt_labeling_v7",
        "model_version": "gpt-5-mini-2026-06-01",
        "aggregation_policy_version_id": "agg_policy_v3",
        "eval_dataset_version_id": "evalset_label_holdout_v12",
    }
    assert data["metrics"]["reviewed_sample_count"] == 240
    assert data["metrics_source"] == "request_override"

    replay = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers=headers,
        json=_payload(),
    )
    assert replay.status_code == 201
    assert replay.json() == body

    with SessionLocal() as session:
        runs = session.scalars(
            select(RunRecord).where(RunRecord.run_type == "label_optimization")
        ).all()
        orchestrated_runs = [run for run in runs if (run.payload or {}).get("scan_id")]
        assert len(orchestrated_runs) == 1
        run = orchestrated_runs[0]
        assert run.status == "queued"
        assert run.payload["trigger_hash"] == data["trigger_hash"]
        assert run.payload["locked_versions"] == data["locked_versions"]
        assert run.payload["budget"]["max_rounds"] == 3
        assert run.payload["next_action"]["code"] == "generate-prompt-candidates"

        scan = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "label_optimization_trigger_scans",
                JsonResource.resource_key == data["scan_id"],
            )
        )
        assert scan is not None
        assert scan.status == "queued"
        assert scan.trace_id == body["meta"]["trace_id"]

        event_types = set(session.scalars(select(OutboxEvent.event_type)).all())
        assert "label_optimization.trigger_scan.completed" in event_types
        assert "agent_run.requested" in event_types
        audit_actions = set(session.scalars(select(AuditLog.action)).all())
        assert "label_optimization.trigger_scan" in audit_actions
        assert "label_optimization.create" in audit_actions


def test_trigger_scan_blocks_active_scope_and_never_reports_false_success(
    client,
    auth_headers,
):
    first = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers={**auth_headers, "Idempotency-Key": "label-opt-trigger-first"},
        json=_payload(),
    )
    assert first.status_code == 201
    assert first.json()["data"]["triggered"] is True

    second = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers={**auth_headers, "Idempotency-Key": "label-opt-trigger-second"},
        json=_payload(reviewed_sample_count=241),
    )
    assert second.status_code == 201, second.text
    data = second.json()["data"]
    assert data["triggered"] is False
    assert data["status"] == "blocked"
    assert data["stage"] == "blocked"
    assert data["run_id"] is None
    assert "active_run_exists" in data["blocked_reasons"]
    assert "cooldown_active" in data["blocked_reasons"]
    assert data["next_action"]["code"] == "wait-for-safety-gate"

    with SessionLocal() as session:
        assert (
            session.scalar(select(RunRecord).where(RunRecord.run_type == "label_optimization"))
            is not None
        )
        runs = session.scalars(
            select(RunRecord).where(RunRecord.run_type == "label_optimization")
        ).all()
        assert len([run for run in runs if (run.payload or {}).get("scan_id")]) == 1


def test_trigger_scan_get_accepts_scan_or_run_id_and_is_scope_safe(client, auth_headers):
    created = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers={**auth_headers, "Idempotency-Key": "label-opt-trigger-get"},
        json=_payload(),
    ).json()["data"]

    by_scan = client.get(
        f"/api/v1/label-optimization-trigger-scans/{created['scan_id']}",
        headers=auth_headers,
    )
    by_run = client.get(
        f"/api/v1/label-optimization-trigger-scans/{created['run_id']}",
        headers=auth_headers,
    )
    assert by_scan.status_code == 200
    assert by_run.status_code == 200
    assert by_scan.json()["data"]["run_id"] == created["run_id"]
    assert by_run.json()["data"]["scan_id"] == created["scan_id"]
    assert by_run.json()["data"]["metrics"] == created["metrics"]

    missing = client.get(
        "/api/v1/label-optimization-trigger-scans/not-in-this-scope",
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_trigger_scan_requires_idempotency_role_and_strict_budget(client, auth_headers):
    missing_key = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers=auth_headers,
        json=_payload(),
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    annotator_headers = {
        **auth_headers,
        "Authorization": "Bearer annotator-token",
        "Idempotency-Key": "label-opt-trigger-forbidden",
    }
    forbidden = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers=annotator_headers,
        json=_payload(),
    )
    assert forbidden.status_code == 403

    invalid = _payload()
    invalid["budget"] = {**invalid["budget"], "max_rounds": 4}
    invalid_response = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers={**auth_headers, "Idempotency-Key": "label-opt-trigger-invalid"},
        json=invalid,
    )
    assert invalid_response.status_code == 422


def test_trigger_scan_rejects_same_idempotency_key_with_changed_request(
    client,
    auth_headers,
):
    headers = {**auth_headers, "Idempotency-Key": "label-opt-trigger-conflict"}
    assert (
        client.post(
            "/api/v1/label-optimization-trigger-scans",
            headers=headers,
            json=_payload(),
        ).status_code
        == 201
    )
    changed = _payload()
    changed["model_version"] = "gpt-5-mini-2026-07-01"
    conflict = client.post(
        "/api/v1/label-optimization-trigger-scans",
        headers=headers,
        json=changed,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
