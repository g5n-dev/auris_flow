from __future__ import annotations

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import (
    AuditLog,
    JsonResource,
    LabelVersion,
    OutboxEvent,
    Project,
    RunRecord,
)
from app.workers import outbox_worker

LABEL_VERSION_ID = "label_v1_9_0_rc2"
EVAL_RUN_ID = "evalrun_label_v190_shadow"
EVAL_DATASET_ID = "evalset_quote_risk_v12"
EVAL_DATASET_VERSION = "v12"
OPTIMIZATION_RUN_ID = "lor_label_v190_rc2"


def _prepare_authoritative_release_gates() -> None:
    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        assert label_version is not None
        label_version.payload = {
            **label_version.payload,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "impacted_assets_confirmed": True,
            "downstream_incompatible_count": 0,
        }

        eval_run = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == EVAL_RUN_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert eval_run is not None
        eval_run.status = "success"
        eval_run.data = {
            **eval_run.data,
            "eval_run_id": EVAL_RUN_ID,
            "status": "success",
            "label_version_id": LABEL_VERSION_ID,
            "candidate_version": LABEL_VERSION_ID,
            "optimization_run_id": OPTIMIZATION_RUN_ID,
            "dataset_id": EVAL_DATASET_ID,
            "dataset_version": EVAL_DATASET_VERSION,
            "metrics": {
                **(eval_run.data.get("metrics") or {}),
                "metric_schema_version": "label-eval-metrics/1",
                "eligible_count": 1500,
                "processed_count": 1500,
                "skipped_count": 0,
                "invalid_count": 0,
                "abstain_count": 0,
                "duplicate_count": 0,
                "confusion_matrix": {
                    "true_positive": 233,
                    "false_positive": 30,
                    "false_negative": 30,
                    "true_negative": 1207,
                },
                "labeling_f1": 88.6,
                "conflict_rate": 4.0,
                "json_validity": 99.9,
                "blocking_regression_count": 0,
                "blocking_badcase_count": 0,
            },
        }

        dataset = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_datasets",
                JsonResource.resource_key == EVAL_DATASET_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert dataset is not None
        dataset.status = "locked"
        dataset.data = {
            **dataset.data,
            "dataset_id": EVAL_DATASET_ID,
            "dataset_version": EVAL_DATASET_VERSION,
            "status": "locked",
            "locked": True,
        }
        session.commit()


def _request_gray_release(client, auth_headers, *, key: str) -> str:
    response = client.post(
        f"/api/v1/label-versions/{LABEL_VERSION_ID}/publish",
        json={"gray_traffic_ppm": 100_000, "eval_run_id": EVAL_RUN_ID},
        headers={**auth_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202
    run = response.json()["data"]
    assert run["status"] == "pending"
    assert run["release_policy_verdict"] == "gray_only"
    return str(run["run_id"])


def test_label_release_worker_materializes_version_projection_and_audit(
    client,
    auth_headers,
):
    _prepare_authoritative_release_gates()
    run_id = _request_gray_release(client, auth_headers, key="release-worker-materialize")

    assert outbox_worker.process_aggregate_events([run_id]) == 1
    assert outbox_worker.process_aggregate_events([run_id]) == 0

    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        run = session.get(RunRecord, run_id)
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == run_id))
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == LABEL_VERSION_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        audit = session.scalar(
            select(AuditLog).where(
                AuditLog.action == "label_version.gray_released",
                AuditLog.object_id == LABEL_VERSION_ID,
                AuditLog.trace_id == run.trace_id,
            )
        )

        assert label_version is not None
        assert run is not None
        assert event is not None
        assert projection is not None
        assert audit is not None
        assert label_version.status == "gray_releasing"
        assert label_version.payload["release_run_id"] == run_id
        assert label_version.payload["gray_traffic_ppm"] == 100_000
        assert projection.status == "gray_releasing"
        assert projection.data["release_run_id"] == run_id
        assert run.status == "success"
        assert run.payload["dispatch_state"] == "completed"
        assert event.status == "processed"
        assert event.payload["adapter_dispatch"]["adapter"] == "label_policy"
        assert event.payload["adapter_dispatch"]["details"]["label_release"] == {
            "allowed": True,
            "reason_code": "RELEASE_FACTS_REVALIDATED",
            "label_version_id": LABEL_VERSION_ID,
            "policy_version_id": run.payload["release_policy_version_id"],
            "evaluation_id": run.payload["release_policy_evaluation_id"],
            "verdict": "gray_only",
            "facts_sha256": run.payload["release_policy_facts_sha256"],
            "decision_sha256": run.payload["release_policy_decision_sha256"],
            "materialized": True,
            "status": "gray_releasing",
        }


def test_label_release_worker_blocks_when_authoritative_facts_change_after_approval(
    client,
    auth_headers,
):
    _prepare_authoritative_release_gates()
    run_id = _request_gray_release(client, auth_headers, key="release-worker-facts-drift")

    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        eval_run = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "eval_runs",
                JsonResource.resource_key == EVAL_RUN_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )
        assert label_version is not None
        assert eval_run is not None
        original_status = label_version.status
        eval_run.data = {
            **eval_run.data,
            "metrics": {
                **(eval_run.data.get("metrics") or {}),
                "conflict_rate": 99.0,
            },
        }
        session.commit()

    assert outbox_worker.process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        run = session.get(RunRecord, run_id)
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == run_id))
        projection = session.scalar(
            select(JsonResource).where(
                JsonResource.collection == "label_versions",
                JsonResource.resource_key == LABEL_VERSION_ID,
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
            )
        )

        assert label_version is not None
        assert run is not None
        assert event is not None
        assert projection is not None
        assert label_version.status == original_status
        assert "release_run_id" not in label_version.payload
        assert projection.status == original_status
        assert run.status == "blocked"
        assert run.payload["dispatch_state"] == "release_gate_blocked"
        assert run.payload["release_dispatch_gate"]["reason_code"] == ("RELEASE_FACTS_CHANGED")
        assert event.status == "blocked"
        assert event.payload["release_dispatch_gate"]["current_verdict"] == "block"


def test_label_release_worker_rechecks_current_project_role(client, auth_headers):
    _prepare_authoritative_release_gates()
    run_id = _request_gray_release(client, auth_headers, key="release-worker-role-revoked")

    with SessionLocal() as session:
        project = session.get(Project, "sales_qa")
        assert project is not None
        project.data = {
            **project.data,
            "members": [
                {
                    **member,
                    "roles": ["asset_manager"],
                }
                if member.get("user_id") == "u_admin_001"
                else member
                for member in project.data.get("members", [])
            ],
        }
        session.commit()

    assert outbox_worker.process_aggregate_events([run_id]) == 1

    with SessionLocal() as session:
        label_version = session.get(LabelVersion, LABEL_VERSION_ID)
        run = session.get(RunRecord, run_id)
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.aggregate_id == run_id))
        assert label_version is not None
        assert run is not None
        assert event is not None
        assert label_version.status != "gray_releasing"
        assert run.status == "blocked"
        assert run.payload["release_dispatch_gate"] == {
            "allowed": False,
            "reason_code": "RELEASE_APPROVER_ROLE_REVOKED",
            "current_roles": ["asset_manager"],
        }
        assert event.status == "blocked"
