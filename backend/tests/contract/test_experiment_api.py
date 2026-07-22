from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from app.core.auth import DevAuthProfile, issue_dev_auth_token
from app.core.completion_signature import completion_signature_message
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models import (
    ExperimentAssignment,
    ExperimentExposure,
    ExperimentOutcome,
    JsonResource,
    Project,
    RunRecord,
    TaskVersionReleaseHead,
    User,
)
from app.workers.outbox_worker import process_aggregate_events

TEST_COMPLETION_HMAC_VALUE = "auris-test-completion-secret-32chars-minimum"
TEST_COMPLETION_KEY_ID = "auris-test-completion"


def _headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _signed_completion_headers(
    *,
    path: str,
    payload: dict,
    idempotency_key: str,
    nonce: str,
) -> dict[str, str]:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    signed_at = datetime.now(UTC).isoformat()
    body_sha256 = hashlib.sha256(encoded).hexdigest()
    message = completion_signature_message(
        method="POST",
        path=path,
        query="",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        idempotency_key=idempotency_key,
        timestamp=signed_at,
        nonce=nonce,
        key_id=TEST_COMPLETION_KEY_ID,
        source="dagster",
        body_sha256=body_sha256,
    )
    signature = hmac.new(
        TEST_COMPLETION_HMAC_VALUE.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Trace-Id": f"trace-{nonce}",
        "X-Request-Id": f"request-{nonce}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
        "X-Auris-Key-Id": TEST_COMPLETION_KEY_ID,
        "X-Auris-Timestamp": signed_at,
        "X-Auris-Nonce": nonce,
        "X-Auris-Source": "dagster",
        "X-Auris-Signature-Mode": "hmac-sha256",
        "X-Auris-Signature": f"sha256={signature}",
    }


def _second_release_admin_token() -> str:
    user_id = "u_annotator_001"
    with SessionLocal.begin() as session:
        user = session.get(User, user_id)
        project = session.get(Project, "sales_qa")
        assert user is not None and project is not None
        user.roles = list(dict.fromkeys([*(user.roles or []), "project_admin"]))
        project_data = dict(project.data)
        project_data["members"] = [
            {
                **member,
                "roles": list(dict.fromkeys([*member.get("roles", []), "project_admin"])),
            }
            if member.get("user_id") == user_id
            else member
            for member in project_data.get("members", [])
        ]
        project.data = project_data
    profile = DevAuthProfile(
        email="experiment-release-admin@auris.local",
        user_id=user_id,
        name="实验发布复核管理员",
        role_label="项目管理员",
        initials="复",
        roles=("annotator", "review_arbitrator", "project_admin"),
    )
    return issue_dev_auth_token(profile, get_settings())[0]


def _seed_candidate_task_version() -> None:
    with SessionLocal.begin() as session:
        existing = (
            session.query(JsonResource)
            .filter(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == "task_version_v4_0_rc2",
            )
            .one_or_none()
        )
        if existing is not None:
            return
        stable = (
            session.query(JsonResource)
            .filter(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == "task_version_v3_2_1",
            )
            .one()
        )
        candidate = dict(stable.data)
        candidate.update(
            {
                "task_version_id": "task_version_v4_0_rc2",
                "version": "v4.0.0-rc2",
                "canvas_variant": "candidate-v4",
                "status": "experiment_ready",
                "trace_id": "trace_candidate_task_version",
            }
        )
        session.add(
            JsonResource(
                tenant_id="aurora_auto",
                project_id="sales_qa",
                collection="task_versions",
                resource_key="task_version_v4_0_rc2",
                status="experiment_ready",
                trace_id="trace_candidate_task_version",
                data=candidate,
            )
        )


def _seed_task_version_clone(
    *,
    task_version_id: str,
    version: str,
    changes: dict | None = None,
) -> None:
    with SessionLocal.begin() as session:
        stable = (
            session.query(JsonResource)
            .filter(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == "task_version_v3_2_1",
            )
            .one()
        )
        candidate = {
            **stable.data,
            "task_version_id": task_version_id,
            "version": version,
            "status": "experiment_ready",
            "trace_id": f"trace_{task_version_id}",
            **(changes or {}),
        }
        session.add(
            JsonResource(
                tenant_id="aurora_auto",
                project_id="sales_qa",
                collection="task_versions",
                resource_key=task_version_id,
                status="experiment_ready",
                trace_id=f"trace_{task_version_id}",
                data=candidate,
            )
        )


def _experiment_payload() -> dict:
    return {
        "experiment_id": "exp_task_canvas_v4",
        "name": "任务画布候选版本实验",
        "experiment_kind": "task_version",
        "variant_dimension": "workflow",
        "task_type_id": "task_sales_quality",
        "hypothesis": "候选版本在不放大串音风险的前提下提升报价一致率",
        "allocation_unit": "audio_session",
        "arms": [
            {
                "arm_key": "control",
                "task_version_id": "task_version_v3_2_1",
                "allocation_ppm": 500_000,
            },
            {
                "arm_key": "candidate",
                "task_version_id": "task_version_v4_0_rc2",
                "allocation_ppm": 500_000,
            },
        ],
        "primary_metric": {
            "metric_key": "quote_consistency",
            "direction": "increase",
            "minimum_effect": 0.05,
        },
        "guardrails": [
            {
                "metric_key": "crosstalkRisk",
                "direction": "decrease",
                "maximum_regression": 0.02,
            }
        ],
        "min_sample_size_per_arm": 3,
        "confidence_level": 0.95,
    }


def _create_and_start(client, auth_headers: dict[str, str]) -> dict:
    _seed_candidate_task_version()
    created = client.post(
        "/api/v1/experiments",
        json=_experiment_payload(),
        headers=_headers(auth_headers, "experiment-create"),
    )
    assert created.status_code == 201, created.text
    experiment = created.json()["data"]
    assert experiment["status"] == "draft"
    assert experiment["scene_profile_id"] == "scene_auto_sales_quality"
    assert experiment["scene_profile_version_id"] == "scenev_auto_sales_quality_v1"
    assert len(experiment["design_sha256"]) == 64
    assert experiment["variant_dimension"] == "workflow"
    assert experiment["actual_changed_dimensions"] == ["workflow"]
    assert len(experiment["variant_diff_sha256"]) == 64
    assert all(len(arm["task_version_behavior_sha256"]) == 64 for arm in experiment["arms"])
    assert all(len(arm["task_version_binding_sha256"]) == 64 for arm in experiment["arms"])

    started = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/start",
        json={"expected_resource_version": experiment["resource_version"]},
        headers=_headers(auth_headers, "experiment-start"),
    )
    assert started.status_code == 200, started.text
    return started.json()["data"]


def _assign_and_observe(
    client,
    auth_headers: dict[str, str],
    experiment_id: str,
    subject_key: str,
    metric_values: dict[str, float],
    *,
    observation_key: str | None = None,
) -> dict:
    observation_key = observation_key or subject_key
    operation_digest = hashlib.sha256(observation_key.encode("utf-8")).hexdigest()[:20]
    assigned = client.post(
        f"/api/v1/experiments/{experiment_id}/assignments",
        json={"subject_key": subject_key},
        headers=_headers(auth_headers, f"assign-{subject_key}"),
    )
    assert assigned.status_code == 201, assigned.text
    assignment = assigned.json()["data"]
    assert "subject_key" not in assignment
    assert len(assignment["subject_key_sha256"]) == 64

    replay = client.post(
        f"/api/v1/experiments/{experiment_id}/assignments",
        json={"subject_key": subject_key},
        headers=_headers(auth_headers, f"assign-{subject_key}"),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["data"]["assignment_id"] == assignment["assignment_id"]

    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "execution_mode": "experiment",
            "experiment_id": experiment_id,
            "experiment_subject_key": subject_key,
            "partition_key": "aurora_auto/tests/controlled-experiment",
            "run_key": f"experiment-observation-{operation_digest}",
        },
        headers=_headers(auth_headers, f"task-run-{operation_digest}"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    assert run["experiment_assignment_id"] == assignment["assignment_id"]
    assert run["experiment_arm"] == assignment["arm_key"]
    assert subject_key not in str(run)

    external_run_id = _dispatch_task_run(run["run_id"])
    path = f"/api/v1/runs/{run['run_id']}/external-completion-receipts"
    payload = {
        "status": "success",
        "adapter": "dagster",
        "completion_receipt_id": f"receipt-{operation_digest}",
        "external_id": external_run_id,
        "result_ref": {
            "evidence_refs": [f"evidence:{operation_digest}"],
            "executed_task_version_binding_sha256": run["expected_executed_bundle_sha256"],
        },
        "metrics": metric_values,
        "source": "dagster",
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    completed = client.post(
        path,
        content=encoded,
        headers=_signed_completion_headers(
            path=path,
            payload=payload,
            idempotency_key=f"complete-{operation_digest}",
            nonce=f"nonce-{operation_digest}",
        ),
    )
    assert completed.status_code == 200, completed.text
    experiment_completion = completed.json()["data"]["experiment_completion"]
    assert experiment_completion["source_kind"] == "signed_task_run_completion"
    assert experiment_completion["completion_receipt_id"] == payload["completion_receipt_id"]
    return assignment


def test_experiment_closes_assignment_metric_and_decision_loop(client, auth_headers):
    experiment = _create_and_start(client, auth_headers)
    experiment_id = experiment["experiment_id"]
    assert experiment["status"] == "running"

    arm_counts = {"control": 0, "candidate": 0}
    index = 0
    while min(arm_counts.values()) < 3 and index < 100:
        subject_key = f"session-{index:03d}"
        assignment = client.post(
            f"/api/v1/experiments/{experiment_id}/assignments",
            json={"subject_key": subject_key},
            headers=_headers(auth_headers, f"probe-{subject_key}"),
        )
        assert assignment.status_code == 201, assignment.text
        arm = assignment.json()["data"]["arm_key"]
        if arm_counts[arm] < 3:
            primary = 0.0 if arm == "control" else 1.0
            guardrail = 0.2 if arm == "control" else 0.0
            _assign_and_observe(
                client,
                auth_headers,
                experiment_id,
                subject_key,
                {"quote_consistency": primary, "crosstalkRisk": guardrail},
            )
            arm_counts[arm] += 1
        index += 1
    assert arm_counts == {"control": 3, "candidate": 3}

    computed = client.post(
        f"/api/v1/experiments/{experiment_id}/metric-snapshots",
        json={},
        headers=_headers(auth_headers, "experiment-compute"),
    )
    assert computed.status_code == 201, computed.text
    snapshot = computed.json()["data"]
    assert snapshot["verdict"] == "promote"
    assert snapshot["sample_sizes"] == {"control": 3, "candidate": 3}
    assert snapshot["primary_metric"]["control_value"] == 0.0
    assert snapshot["primary_metric"]["candidate_value"] == 1.0
    assert snapshot["primary_metric"]["delta"] == 1.0
    assert snapshot["guardrails"][0]["status"] == "pass"
    assert snapshot["evidence_sha256"]
    assert snapshot["fact_source"] == "signed_task_run_completion"
    assert snapshot["source_run_count"] == 6
    assert snapshot["completion_receipt_count"] == 6
    assert snapshot["calculator_engine"] == "auris.experiment.metric-engine/v2"
    assert snapshot["sample_ratio_diagnostic"]["status"] == "pass"
    assert snapshot["sample_ratio_diagnostic"]["detected"] is False

    decision = client.post(
        f"/api/v1/experiments/{experiment_id}/decisions",
        json={
            "decision": "promote_candidate",
            "metric_snapshot_id": snapshot["metric_snapshot_id"],
            "expected_resource_version": snapshot["experiment_resource_version"],
            "reason": "主指标显著提升且守护指标未退化",
        },
        headers=_headers(auth_headers, "experiment-promote"),
    )
    assert decision.status_code == 201, decision.text
    result = decision.json()["data"]
    assert result["decision"] == "promote_candidate"
    assert result["experiment_status"] == "decided"
    assert result["next_action"]["type"] == "task_version_release"
    assert result["next_action"]["task_version_id"] == "task_version_v4_0_rc2"

    requested = client.post(
        "/api/v1/task-versions/task_version_v4_0_rc2/publish",
        json={
            "source": "controlled_experiment",
            "experiment_id": experiment_id,
            "metric_snapshot_id": snapshot["metric_snapshot_id"],
            "design_sha256": experiment["design_sha256"],
            "reason": "实验晋级后进入独立发布门禁",
        },
        headers=_headers(auth_headers, "experiment-release-request"),
    )
    assert requested.status_code == 202, requested.text
    release_run = requested.json()["data"]
    assert release_run["status"] == "blocked"
    assert release_run["experiment_attestation"]["decision_id"] == result["decision_id"]
    assert release_run["release_gate"]["target"]["expected_head_task_version_id"] == (
        "task_version_v3_2_1"
    )

    assert process_aggregate_events([release_run["run_id"]]) == 1
    approved = client.post(
        f"/api/v1/runs/{release_run['run_id']}/decisions",
        json={"decision": "approved", "reason": "独立管理员确认实验事实与发布影响"},
        headers={
            **auth_headers,
            "Authorization": f"Bearer {_second_release_admin_token()}",
            "Idempotency-Key": "experiment-release-approve",
        },
    )
    assert approved.status_code == 200, approved.text
    assert process_aggregate_events([release_run["run_id"]]) == 1

    candidate = client.get("/api/v1/task-versions/task_version_v4_0_rc2", headers=auth_headers)
    control = client.get("/api/v1/task-versions/task_version_v3_2_1", headers=auth_headers)
    assert candidate.status_code == 200 and candidate.json()["data"]["status"] == "published"
    assert control.status_code == 200 and control.json()["data"]["status"] == "deprecated"

    detail = client.get(f"/api/v1/experiments/{experiment_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["status"] == "decided"
    assert data["counts"]["assignments"] >= 6
    assert data["counts"]["exposures"] == 6
    assert data["counts"]["outcomes"] == 12
    assert data["counts"]["metric_snapshots"] == 1
    assert data["counts"]["decisions"] == 1

    with SessionLocal() as session:
        rows = (
            session.query(ExperimentAssignment)
            .filter(ExperimentAssignment.experiment_id == experiment_id)
            .all()
        )
        assert rows
        assert all("session-" not in str(row.payload) for row in rows)
        head = session.query(TaskVersionReleaseHead).one()
        assert head.task_type_id == "task_sales_quality"
        assert head.active_task_version_id == "task_version_v4_0_rc2"
        assert head.previous_task_version_id == "task_version_v3_2_1"
        assert head.generation == 1


def test_experiment_candidate_is_frozen_and_repeated_exposures_count_one_subject(
    client, auth_headers
):
    experiment = _create_and_start(client, auth_headers)
    experiment_id = experiment["experiment_id"]

    mutated = client.patch(
        "/api/v1/task-versions/task_version_v4_0_rc2",
        json={"canvas_variant": "candidate-drifted"},
        headers=_headers(auth_headers, "experiment-candidate-drift"),
    )
    assert mutated.status_code == 409, mutated.text
    assert mutated.json()["error"]["code"] == "TASK_VERSION_IMMUTABLE"

    assignment = None
    for index in range(3):
        current = _assign_and_observe(
            client,
            auth_headers,
            experiment_id,
            "repeat-subject",
            {"quote_consistency": 0.8 + index * 0.01, "crosstalkRisk": 0.1},
            observation_key=f"repeat-subject-{index}",
        )
        assignment = current if assignment is None else assignment
        assert current["assignment_id"] == assignment["assignment_id"]

    snapshot = client.post(
        f"/api/v1/experiments/{experiment_id}/metric-snapshots",
        json={},
        headers=_headers(auth_headers, "repeat-subject-snapshot"),
    )
    assert snapshot.status_code == 201, snapshot.text
    sample_sizes = snapshot.json()["data"]["sample_sizes"]
    assert assignment is not None
    assert sample_sizes[assignment["arm_key"]] == 1
    assert (
        sample_sizes[{"control": "candidate", "candidate": "control"}[assignment["arm_key"]]] == 0
    )


def test_experiment_blocks_selective_completion_sample_ratio_mismatch(client, auth_headers):
    _seed_candidate_task_version()
    payload = _experiment_payload()
    payload["experiment_id"] = "exp_selective_completion_srm"
    payload["name"] = "选择性完成样本比例诊断"
    payload["arms"][0]["allocation_ppm"] = 900_000
    payload["arms"][1]["allocation_ppm"] = 100_000
    created = client.post(
        "/api/v1/experiments",
        json=payload,
        headers=_headers(auth_headers, "experiment-srm-create"),
    )
    assert created.status_code == 201, created.text
    experiment = created.json()["data"]
    started = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/start",
        json={"expected_resource_version": experiment["resource_version"]},
        headers=_headers(auth_headers, "experiment-srm-start"),
    )
    assert started.status_code == 200, started.text

    completed = {"control": 0, "candidate": 0}
    index = 0
    while min(completed.values()) < 4 and index < 250:
        subject_key = f"srm-subject-{index:03d}"
        assignment = client.post(
            f"/api/v1/experiments/{experiment['experiment_id']}/assignments",
            json={"subject_key": subject_key},
            headers=_headers(auth_headers, f"srm-probe-{index}"),
        )
        assert assignment.status_code == 201, assignment.text
        arm = assignment.json()["data"]["arm_key"]
        if completed[arm] < 4:
            _assign_and_observe(
                client,
                auth_headers,
                experiment["experiment_id"],
                subject_key,
                {
                    "quote_consistency": 0.0 if arm == "control" else 1.0,
                    "crosstalkRisk": 0.2 if arm == "control" else 0.0,
                },
                observation_key=f"srm-observation-{index}",
            )
            completed[arm] += 1
        index += 1
    assert completed == {"control": 4, "candidate": 4}

    computed = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/metric-snapshots",
        json={},
        headers=_headers(auth_headers, "experiment-srm-compute"),
    )
    assert computed.status_code == 201, computed.text
    snapshot = computed.json()["data"]
    assert snapshot["sample_sizes"] == {"control": 4, "candidate": 4}
    assert snapshot["primary_metric"]["status"] == "pass"
    assert snapshot["verdict"] == "blocked_sample_ratio"
    assert snapshot["sample_ratio_diagnostic"]["detected"] is True
    assert snapshot["sample_ratio_diagnostic"]["analysis_sample"]["detected"] is True

    blocked = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/decisions",
        json={
            "decision": "promote_candidate",
            "metric_snapshot_id": snapshot["metric_snapshot_id"],
            "expected_resource_version": snapshot["experiment_resource_version"],
            "reason": "选择性完成不得形成候选版本晋级依据",
        },
        headers=_headers(auth_headers, "experiment-srm-promote"),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "EXPERIMENT_GATE_NOT_PASSED"


def test_experiment_rejects_stale_metric_snapshot_after_new_outcome(client, auth_headers):
    experiment = _create_and_start(client, auth_headers)
    experiment_id = experiment["experiment_id"]
    arm_counts = {"control": 0, "candidate": 0}
    index = 0
    while min(arm_counts.values()) < 3 and index < 100:
        subject_key = f"stale-{index:03d}"
        assignment = client.post(
            f"/api/v1/experiments/{experiment_id}/assignments",
            json={"subject_key": subject_key},
            headers=_headers(auth_headers, f"stale-probe-{index}"),
        )
        assert assignment.status_code == 201, assignment.text
        arm = assignment.json()["data"]["arm_key"]
        if arm_counts[arm] < 3:
            _assign_and_observe(
                client,
                auth_headers,
                experiment_id,
                subject_key,
                {
                    "quote_consistency": 0.0 if arm == "control" else 1.0,
                    "crosstalkRisk": 0.2 if arm == "control" else 0.0,
                },
            )
            arm_counts[arm] += 1
        index += 1
    snapshot = client.post(
        f"/api/v1/experiments/{experiment_id}/metric-snapshots",
        json={},
        headers=_headers(auth_headers, "stale-snapshot-create"),
    ).json()["data"]
    assert snapshot["verdict"] == "promote"

    extra_index = index
    while True:
        subject_key = f"stale-extra-{extra_index:03d}"
        assigned = client.post(
            f"/api/v1/experiments/{experiment_id}/assignments",
            json={"subject_key": subject_key},
            headers=_headers(auth_headers, f"stale-extra-probe-{extra_index}"),
        )
        assert assigned.status_code == 201, assigned.text
        if assigned.json()["data"]["arm_key"] == "candidate":
            _assign_and_observe(
                client,
                auth_headers,
                experiment_id,
                subject_key,
                {"quote_consistency": 0.0, "crosstalkRisk": 1.0},
            )
            break
        extra_index += 1

    stale = client.post(
        f"/api/v1/experiments/{experiment_id}/decisions",
        json={
            "decision": "promote_candidate",
            "metric_snapshot_id": snapshot["metric_snapshot_id"],
            "expected_resource_version": snapshot["experiment_resource_version"],
            "reason": "不得使用新增结果之前的旧快照",
        },
        headers=_headers(auth_headers, "stale-snapshot-promote"),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "EXPERIMENT_METRIC_SNAPSHOT_STALE"


def test_experiment_rejects_unknown_scene_metric_and_early_promotion(client, auth_headers):
    _seed_candidate_task_version()
    invalid = _experiment_payload()
    invalid["experiment_id"] = "exp_unknown_metric"
    invalid["primary_metric"]["metric_key"] = "hardcoded-automotive-only-metric"
    response = client.post(
        "/api/v1/experiments",
        json=invalid,
        headers=_headers(auth_headers, "experiment-invalid-metric"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "EXPERIMENT_METRIC_NOT_IN_SCENE"

    experiment = _create_and_start(client, auth_headers)
    computed = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/metric-snapshots",
        json={},
        headers=_headers(auth_headers, "experiment-empty-compute"),
    )
    assert computed.status_code == 201, computed.text
    snapshot = computed.json()["data"]
    assert snapshot["verdict"] == "insufficient_sample"

    blocked = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/decisions",
        json={
            "decision": "promote_candidate",
            "metric_snapshot_id": snapshot["metric_snapshot_id"],
            "expected_resource_version": snapshot["experiment_resource_version"],
            "reason": "尝试提前晋级",
        },
        headers=_headers(auth_headers, "experiment-early-promote"),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "EXPERIMENT_GATE_NOT_PASSED"


def test_experiment_rejects_direct_or_unsupported_outcome_sources(client, auth_headers):
    experiment = _create_and_start(client, auth_headers)
    direct = client.post(
        f"/api/v1/experiments/{experiment['experiment_id']}/outcomes",
        json={
            "exposure_id": "forged-exposure",
            "metric_values": {"quote_consistency": 1.0, "crosstalkRisk": 0.0},
            "occurred_at": datetime.now(UTC).isoformat(),
            "evidence_refs": ["forged:evidence"],
        },
        headers=_headers(auth_headers, "experiment-forged-outcome"),
    )
    assert direct.status_code == 403, direct.text
    assert direct.json()["error"]["code"] == "EXPERIMENT_OUTCOME_DIRECT_WRITE_FORBIDDEN"

    unsupported = _experiment_payload()
    unsupported["experiment_id"] = "exp_unsupported_model_version"
    unsupported["experiment_kind"] = "model_version"
    rejected = client.post(
        "/api/v1/experiments",
        json=unsupported,
        headers=_headers(auth_headers, "experiment-unsupported-kind"),
    )
    assert rejected.status_code == 422, rejected.text


def test_experiment_rejects_empty_or_mixed_treatment_dimensions(client, auth_headers):
    _seed_task_version_clone(
        task_version_id="task_version_no_effect",
        version="v3.2.1-copy",
    )
    no_effect = _experiment_payload()
    no_effect["experiment_id"] = "exp_no_effect"
    no_effect["arms"][1]["task_version_id"] = "task_version_no_effect"
    rejected_empty = client.post(
        "/api/v1/experiments",
        json=no_effect,
        headers=_headers(auth_headers, "experiment-no-effect"),
    )
    assert rejected_empty.status_code == 409, rejected_empty.text
    assert rejected_empty.json()["error"]["code"] == "EXPERIMENT_NO_EFFECTIVE_TREATMENT"

    _seed_task_version_clone(
        task_version_id="task_version_mixed_treatment",
        version="v4.1.0-rc1",
        changes={
            "canvas_variant": "candidate-mixed",
            "model_version": "model-v-next",
        },
    )
    mixed = _experiment_payload()
    mixed["experiment_id"] = "exp_mixed_treatment"
    mixed["arms"][1]["task_version_id"] = "task_version_mixed_treatment"
    rejected_mixed = client.post(
        "/api/v1/experiments",
        json=mixed,
        headers=_headers(auth_headers, "experiment-mixed-treatment"),
    )
    assert rejected_mixed.status_code == 409, rejected_mixed.text
    error = rejected_mixed.json()["error"]
    assert error["code"] == "EXPERIMENT_VARIANT_DIMENSION_MISMATCH"
    assert error["details"] == [
        {
            "declared_variant_dimension": "workflow",
            "actual_changed_dimensions": ["model", "workflow"],
            "diff_sha256": error["details"][0]["diff_sha256"],
        }
    ]
    assert len(error["details"][0]["diff_sha256"]) == 64


def test_experiment_accepts_an_explicit_model_only_treatment(client, auth_headers):
    _seed_task_version_clone(
        task_version_id="task_version_model_candidate",
        version="v3.2.1-model-rc1",
        changes={"model_version": "model-v-next"},
    )
    payload = _experiment_payload()
    payload["experiment_id"] = "exp_model_only"
    payload["variant_dimension"] = "model"
    payload["arms"][1]["task_version_id"] = "task_version_model_candidate"
    created = client.post(
        "/api/v1/experiments",
        json=payload,
        headers=_headers(auth_headers, "experiment-model-only"),
    )
    assert created.status_code == 201, created.text
    experiment = created.json()["data"]
    assert experiment["variant_dimension"] == "model"
    assert experiment["actual_changed_dimensions"] == ["model"]
    assert len(experiment["variant_diff_sha256"]) == 64


def _dispatch_task_run(run_id: str) -> str:
    assert process_aggregate_events([run_id]) == 1
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted"
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "dagster"
        return str(dispatch["details"]["external_run_id"])


def test_experiment_task_run_materializes_exposure_and_outcomes(client, auth_headers):
    experiment = _create_and_start(client, auth_headers)
    subject_key = "audio-session-runtime-001"
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "execution_mode": "experiment",
            "experiment_id": experiment["experiment_id"],
            "experiment_subject_key": subject_key,
            "partition_key": "aurora_auto/BJ-AURORA-001/2025-05-26/experiment",
            "run_key": "experiment-runtime-run-001",
        },
        headers=_headers(auth_headers, "experiment-runtime-run-create"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    assert run["execution_mode"] == "experiment"
    assert run["experiment_id"] == experiment["experiment_id"]
    assert run["experiment_arm"] in {"control", "candidate"}
    assert (
        run["task_version_id"]
        == {
            "control": "task_version_v3_2_1",
            "candidate": "task_version_v4_0_rc2",
        }[run["experiment_arm"]]
    )
    assert len(run["experiment_subject_key_sha256"]) == 64
    assert "experiment_subject_key" not in run
    assert run["external_outputs_enabled"] is False

    external_run_id = _dispatch_task_run(run["run_id"])
    path = f"/api/v1/runs/{run['run_id']}/external-completion-receipts"
    payload = {
        "status": "success",
        "adapter": "dagster",
        "completion_receipt_id": "experiment-runtime-receipt-001",
        "external_id": external_run_id,
        "result_ref": {
            "evidence_refs": ["evidence:runtime-001"],
            "executed_task_version_binding_sha256": run["expected_executed_bundle_sha256"],
        },
        "metrics": {"quote_consistency": 0.91, "crosstalkRisk": 0.08},
        "source": "dagster",
    }
    completed = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_signed_completion_headers(
            path=path,
            payload=payload,
            idempotency_key="experiment-runtime-run-complete",
            nonce="nonce-experiment-runtime-run-complete",
        ),
    )
    assert completed.status_code == 200, completed.text
    completion = completed.json()["data"]
    assert completion["status"] == "success"
    experiment_completion = completion["experiment_completion"]
    assert experiment_completion["experiment_id"] == experiment["experiment_id"]
    assert experiment_completion["assignment_id"] == run["experiment_assignment_id"]
    assert experiment_completion["exposure_id"] == run["experiment_exposure_id"]
    assert experiment_completion["arm_key"] == run["experiment_arm"]
    assert experiment_completion["metric_keys"] == ["crosstalkRisk", "quote_consistency"]
    assert experiment_completion["design_sha256"] == experiment["design_sha256"]
    assert experiment_completion["source_kind"] == "signed_task_run_completion"
    assert experiment_completion["completion_receipt_id"] == payload["completion_receipt_id"]
    assert len(experiment_completion["completion_receipt_sha256"]) == 64
    assert len(experiment_completion["outcome_ids"]) == 2

    run_readback = client.get(f"/api/v1/runs/{run['run_id']}", headers=auth_headers)
    assert run_readback.status_code == 200, run_readback.text
    readback_data = run_readback.json()["data"]
    with SessionLocal() as session:
        stored_run = session.get(RunRecord, run["run_id"])
        assert stored_run is not None
        assert readback_data["trace_id"] == stored_run.trace_id
        assert (
            readback_data["completion_receipt"]["completion_receipt_id"]
            == payload["completion_receipt_id"]
        )
        assert "run_trace_id" not in readback_data["completion_receipt"]

    detail = client.get(f"/api/v1/experiments/{experiment['experiment_id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["counts"] == {
        "assignments": 1,
        "exposures": 1,
        "outcomes": 2,
        "metric_snapshots": 0,
        "decisions": 0,
    }

    with SessionLocal() as session:
        stored_run = session.get(RunRecord, run["run_id"])
        assert stored_run is not None
        assert subject_key not in str(stored_run.payload)
        assert session.query(ExperimentAssignment).count() == 1
        assert session.query(ExperimentExposure).count() == 1
        assert session.query(ExperimentOutcome).count() == 2


def test_experiment_task_run_rejects_missing_or_mismatched_bundle_proof(client, auth_headers):
    experiment = _create_and_start(client, auth_headers)
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "execution_mode": "experiment",
            "experiment_id": experiment["experiment_id"],
            "experiment_subject_key": "audio-session-bundle-proof",
            "run_key": "experiment-runtime-bundle-proof",
        },
        headers=_headers(auth_headers, "experiment-runtime-bundle-proof-create"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    external_run_id = _dispatch_task_run(run["run_id"])
    path = f"/api/v1/runs/{run['run_id']}/external-completion-receipts"

    for suffix, result_ref, expected_code in (
        (
            "missing",
            {"evidence_refs": ["evidence:bundle-proof"]},
            "EXPERIMENT_EXECUTED_BUNDLE_PROOF_REQUIRED",
        ),
        (
            "mismatch",
            {
                "evidence_refs": ["evidence:bundle-proof"],
                "executed_task_version_binding_sha256": "0" * 64,
            },
            "EXPERIMENT_EXECUTED_BUNDLE_MISMATCH",
        ),
    ):
        payload = {
            "status": "success",
            "adapter": "dagster",
            "completion_receipt_id": f"experiment-bundle-proof-{suffix}",
            "external_id": external_run_id,
            "result_ref": result_ref,
            "metrics": {"quote_consistency": 0.91, "crosstalkRisk": 0.08},
            "source": "dagster",
        }
        blocked = client.post(
            path,
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=_signed_completion_headers(
                path=path,
                payload=payload,
                idempotency_key=f"experiment-bundle-proof-{suffix}",
                nonce=f"nonce-experiment-bundle-proof-{suffix}",
            ),
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == expected_code

    with SessionLocal() as session:
        stored_run = session.get(RunRecord, run["run_id"])
        assert stored_run is not None
        assert stored_run.status == "submitted"
        assert "completion_receipt" not in stored_run.payload
        assert session.query(ExperimentOutcome).count() == 0


def test_running_experiment_keeps_frozen_historical_arm_after_release_head_moves(
    client, auth_headers
):
    experiment = _create_and_start(client, auth_headers)
    with SessionLocal.begin() as session:
        control = (
            session.query(JsonResource)
            .filter(
                JsonResource.tenant_id == "aurora_auto",
                JsonResource.project_id == "sales_qa",
                JsonResource.collection == "task_versions",
                JsonResource.resource_key == "task_version_v3_2_1",
            )
            .one()
        )
        control.status = "deprecated"
        control.data = {
            **control.data,
            "status": "deprecated",
            "deprecated_at": datetime.now(UTC).isoformat(),
            "deprecated_by": "u_release_admin_001",
            "replaced_by_task_version_id": "task_version_next_production",
            "replacement_run_id": "task_version_publish_next",
            "trace_id": "trace_release_head_moved",
        }

    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "manual",
            "execution_mode": "experiment",
            "experiment_id": experiment["experiment_id"],
            "experiment_subject_key": "audio-session-after-head-move",
            "run_key": "experiment-run-after-head-move",
        },
        headers=_headers(auth_headers, "experiment-run-after-head-move"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    assert run["experiment_id"] == experiment["experiment_id"]
    assert run["task_version_id"] in {
        "task_version_v3_2_1",
        "task_version_v4_0_rc2",
    }

    mutation = client.patch(
        "/api/v1/task-versions/task_version_v3_2_1",
        json={"canvas_variant": "must-not-change"},
        headers=_headers(auth_headers, "deprecated-task-version-mutation"),
    )
    assert mutation.status_code == 409, mutation.text
    assert mutation.json()["error"]["code"] == "TASK_VERSION_IMMUTABLE"


def test_experiment_task_run_blocks_incomplete_metric_receipt_without_partial_outcomes(
    client, auth_headers
):
    experiment = _create_and_start(client, auth_headers)
    created = client.post(
        "/api/v1/task-runs",
        json={
            "task_version_id": "task_version_v3_2_1",
            "execution_mode": "experiment",
            "experiment_id": experiment["experiment_id"],
            "experiment_subject_key": "audio-session-runtime-missing-metric",
            "run_key": "experiment-runtime-run-missing-metric",
        },
        headers=_headers(auth_headers, "experiment-runtime-missing-create"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    run_id = run["run_id"]
    external_run_id = _dispatch_task_run(run_id)

    path = f"/api/v1/runs/{run_id}/external-completion-receipts"
    payload = {
        "status": "success",
        "adapter": "dagster",
        "completion_receipt_id": "experiment-runtime-missing-receipt",
        "external_id": external_run_id,
        "result_ref": {
            "executed_task_version_binding_sha256": run["expected_executed_bundle_sha256"]
        },
        "metrics": {"quote_consistency": 0.91},
        "source": "dagster",
    }
    blocked = client.post(
        path,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_signed_completion_headers(
            path=path,
            payload=payload,
            idempotency_key="experiment-runtime-missing-complete",
            nonce="nonce-experiment-runtime-missing-complete",
        ),
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "EXPERIMENT_COMPLETION_METRICS_REQUIRED"
    assert blocked.json()["error"]["details"] == [{"metric_keys": ["crosstalkRisk"]}]

    with SessionLocal() as session:
        stored_run = session.get(RunRecord, run_id)
        assert stored_run is not None
        assert stored_run.status == "submitted"
        assert "completion_receipt" not in stored_run.payload
        assert session.query(ExperimentOutcome).count() == 0


def test_experiment_list_uses_stable_cursor_pagination(client, auth_headers):
    _seed_candidate_task_version()
    for index in range(3):
        payload = _experiment_payload()
        payload["experiment_id"] = f"exp_pagination_{index}"
        created = client.post(
            "/api/v1/experiments",
            json=payload,
            headers=_headers(auth_headers, f"experiment-pagination-create-{index}"),
        )
        assert created.status_code == 201, created.text

    first = client.get("/api/v1/experiments?limit=1", headers=auth_headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["meta"]["total"] == 3
    assert first_body["meta"]["limit"] == 1
    assert first_body["meta"]["next_cursor"]

    second = client.get(
        f"/api/v1/experiments?limit=1&cursor={first_body['meta']['next_cursor']}",
        headers=auth_headers,
    )
    assert second.status_code == 200, second.text
    assert (
        second.json()["data"]["items"][0]["experiment_id"]
        != first_body["data"]["items"][0]["experiment_id"]
    )

    invalid = client.get("/api/v1/experiments?cursor=not-a-cursor", headers=auth_headers)
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"
