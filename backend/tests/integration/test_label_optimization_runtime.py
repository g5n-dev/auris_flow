from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import (
    EvalDatasetVersion,
    FeedbackExample,
    LabelAggregationPolicyVersion,
    LabelEvalResult,
    LabelOptimizationMetricSnapshot,
    LabelOptimizationRound,
    LabelOptimizationSchedule,
    LabelVersion,
    PromptAsset,
    PromptVersion,
    ReleaseDeployment,
    RunRecord,
    StorageObject,
)
from app.services.label_optimization_runtime_service import create_or_update_schedule
from app.workers.label_optimization_worker import run_once

TENANT = "aurora_auto"
PROJECT = "sales_qa"
LABEL_VERSION = "label-runtime-v1"
PARENT_PROMPT = "prompt-runtime-v1"
MODEL_VERSION = "gpt-5-mini-runtime"
POLICY_VERSION = "agg-runtime-v1"
DATASET_VERSION = "eval-runtime-v1"
ASSET_ID = "prompt-asset-runtime"


def _ctx(trace_id: str = "trace-runtime-test") -> RequestContext:
    return RequestContext(
        tenant_id=TENANT,
        project_id=PROJECT,
        user_id="runtime-test",
        roles=("project_admin", "model_engineer"),
        request_id=f"request-{trace_id}",
        trace_id=trace_id,
        idempotency_key=f"idem-{trace_id}",
    )


def _object_key_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot_sha(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.fixture(autouse=True)
def _seed_runtime_bundle():
    now = datetime.now(UTC)
    object_key = f"tenants/{TENANT}/projects/{PROJECT}/eval/runtime-v1.jsonl"
    dataset_snapshot = {
        "eval_dataset_id": DATASET_VERSION,
        "name": "runtime hidden holdout",
        "capability": "labeling",
        "dataset_version": "1.0.0",
        "manifest_storage_object_id": "obj-runtime-eval",
        "manifest_sha256": "3" * 64,
        "manifest_provider": "test",
        "manifest_bucket": "auris-test",
        "manifest_object_key": object_key,
        "manifest_content_type": "application/json",
        "manifest_size_bytes": 4096,
        "manifest_etag": "runtime-eval-etag",
        "sample_count": 240,
    }
    with SessionLocal() as session:
        session.add(
            LabelVersion(
                label_version_id=LABEL_VERSION,
                tenant_id=TENANT,
                project_id=PROJECT,
                status="published",
                resource_version=1,
                trace_id="trace-runtime-seed",
                payload={},
            )
        )
        session.add(
            PromptAsset(
                prompt_asset_id=ASSET_ID,
                tenant_id=TENANT,
                project_id=PROJECT,
                name="runtime labeling prompt",
                capability="labeling",
                label_version_id=LABEL_VERSION,
                status="active",
                current_version_id=PARENT_PROMPT,
                trace_id="trace-runtime-seed",
                payload={},
            )
        )
        session.add(
            PromptVersion(
                prompt_version_id=PARENT_PROMPT,
                tenant_id=TENANT,
                project_id=PROJECT,
                prompt_asset_id=ASSET_ID,
                version="1.0.0",
                parent_version_id=None,
                label_version_id=LABEL_VERSION,
                schema_version="label-output-v1",
                model_version=MODEL_VERSION,
                status="approved",
                template_json={"system": "json only"},
                output_schema={"type": "object"},
                generation_params={"temperature": 0},
                structured_diff={},
                source_badcase_refs=[],
                content_sha256="1" * 64,
                trace_id="trace-runtime-seed",
            )
        )
        session.add(
            LabelAggregationPolicyVersion(
                policy_version_id=POLICY_VERSION,
                tenant_id=TENANT,
                project_id=PROJECT,
                label_version_id=LABEL_VERSION,
                policy_version="1.0.0",
                mode="l1",
                status="active",
                source_weights={"llm": 1.0},
                calibration_versions={},
                thresholds={},
                label_definitions=[{"label_id": "intent", "kind": "categorical"}],
                canonical_sha256="2" * 64,
                trace_id="trace-runtime-seed",
                payload={},
            )
        )
        session.add(
            StorageObject(
                storage_object_id="obj-runtime-eval",
                tenant_id=TENANT,
                project_id=PROJECT,
                provider="test",
                bucket="auris-test",
                object_key=object_key,
                object_key_sha256=_object_key_sha(object_key),
                source_type="eval_dataset_manifest",
                source_id=DATASET_VERSION,
                content_type="application/json",
                size_bytes=4096,
                content_sha256="3" * 64,
                etag="runtime-eval-etag",
                status="verified",
                trace_id="trace-runtime-seed",
                payload={},
            )
        )
        session.add(
            EvalDatasetVersion(
                eval_dataset_id=DATASET_VERSION,
                tenant_id=TENANT,
                project_id=PROJECT,
                name="runtime hidden holdout",
                capability="labeling",
                dataset_version="1.0.0",
                status="locked",
                manifest_storage_object_id="obj-runtime-eval",
                manifest_sha256="3" * 64,
                manifest_provider="test",
                manifest_bucket="auris-test",
                manifest_object_key=object_key,
                manifest_content_type="application/json",
                manifest_size_bytes=4096,
                manifest_etag="runtime-eval-etag",
                sample_count=240,
                resource_version=1,
                root_trace_id="trace-runtime-seed",
                current_trace_id="trace-runtime-seed",
                locked_at=now,
                payload={"snapshot_sha256": _snapshot_sha(dataset_snapshot)},
            )
        )
        session.commit()


def _schedule(*, now: datetime, budget: dict | None = None) -> str:
    with SessionLocal() as session:
        data = create_or_update_schedule(
            session,
            _ctx(),
            request_data={
                "label_version_id": LABEL_VERSION,
                "prompt_version_id": PARENT_PROMPT,
                "model_version": MODEL_VERSION,
                "aggregation_policy_version_id": POLICY_VERSION,
                "eval_dataset_version_id": DATASET_VERSION,
                "schedule_timezone": "Asia/Shanghai",
                "daily_hour": 2,
                "weekly_day": 6,
                "start_immediately": True,
                "budget": budget or {},
            },
            now=now,
        )
        schedule = session.get(LabelOptimizationSchedule, data["schedule_id"])
        assert schedule is not None
        schedule.next_weekly_at = now
        session.commit()
        return schedule.schedule_id


def _materialize_candidates(generation_run_id: str, round_number: int) -> list[str]:
    candidate_ids = [
        f"prompt-runtime-r{round_number}-c1",
        f"prompt-runtime-r{round_number}-c2",
    ]
    with SessionLocal() as session:
        generation = session.get(RunRecord, generation_run_id)
        assert generation is not None
        for index, candidate_id in enumerate(candidate_ids, start=1):
            session.add(
                PromptVersion(
                    prompt_version_id=candidate_id,
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    prompt_asset_id=ASSET_ID,
                    version=f"candidate-{round_number}-{index}",
                    parent_version_id=str(generation.payload["prompt_version_id"]),
                    label_version_id=LABEL_VERSION,
                    schema_version="label-output-v1",
                    model_version=MODEL_VERSION,
                    status="candidate",
                    template_json={"system": f"candidate {round_number}-{index}"},
                    output_schema={"type": "object"},
                    generation_params={"temperature": 0},
                    structured_diff={"system": "changed"},
                    source_badcase_refs=[],
                    content_sha256=f"{round_number}{index}".ljust(64, "0"),
                    trace_id=generation.trace_id,
                )
            )
        generation.status = "success"
        generation.payload = {
            **generation.payload,
            "status": "success",
            "prompt_candidate_ids": candidate_ids,
            "metrics": {"cost_micros": 100_000},
        }
        session.commit()
    return candidate_ids


def _complete_round_evals(round_id: str, *, gain_pp: float, passed: bool = False) -> None:
    with SessionLocal() as session:
        round_record = session.get(LabelOptimizationRound, round_id)
        assert round_record is not None and len(round_record.eval_run_ids) == 2
        for index, eval_run_id in enumerate(round_record.eval_run_ids):
            eval_run = session.get(RunRecord, eval_run_id)
            assert eval_run is not None
            eval_run.status = "success" if passed and index == 0 else "blocked"
            eval_run.payload = {**eval_run.payload, "status": eval_run.status}
            session.add(
                LabelEvalResult(
                    eval_result_id=f"result-{round_id}-{index}",
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    eval_run_id=eval_run_id,
                    status="passed" if passed and index == 0 else "blocked",
                    binding_sha256=str(eval_run.payload["binding_sha256"]),
                    dataset_snapshot_sha256=str(
                        eval_run.payload["locked_versions"]["eval_dataset_snapshot_sha256"]
                    ),
                    sample_manifest_sha256=f"{index + 7}" * 64,
                    result_sha256=f"{index + 8}" * 64,
                    overall_metrics={
                        "macro_f1_gain_pp": gain_pp - index * 0.1,
                        "critical_recall_delta_pp": 0.0,
                    },
                    bootstrap_ci={"critical_recall_delta_lower_pp": 0.0},
                    gate_results=[],
                    trace_id=eval_run.trace_id,
                    payload={},
                )
            )
        session.commit()


def test_schedule_api_persists_locked_bundle_and_replays(client, auth_headers):
    payload = {
        "label_version_id": LABEL_VERSION,
        "prompt_version_id": PARENT_PROMPT,
        "model_version": MODEL_VERSION,
        "aggregation_policy_version_id": POLICY_VERSION,
        "eval_dataset_version_id": DATASET_VERSION,
        "schedule_timezone": "Asia/Shanghai",
        "daily_hour": 2,
        "weekly_day": 6,
        "start_immediately": True,
        "budget": {
            "max_rounds": 3,
            "min_candidates_per_round": 2,
            "max_candidates_per_round": 5,
            "candidates_per_round": 3,
            "max_elapsed_seconds": 7200,
            "min_meaningful_gain_ppm": 20_000,
            "max_consecutive_failed_rounds": 2,
        },
    }
    headers = {**auth_headers, "Idempotency-Key": "label-opt-schedule-runtime"}
    created = client.post(
        "/api/v1/label-optimization-schedules",
        json=payload,
        headers=headers,
    )
    replay = client.post(
        "/api/v1/label-optimization-schedules",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 201, created.text
    assert replay.json() == created.json()
    data = created.json()["data"]
    assert data["budget"]["candidates_per_round"] == 3
    fetched = client.get(
        f"/api/v1/label-optimization-schedules/{data['schedule_id']}",
        headers=auth_headers,
    )
    assert fetched.status_code == 200
    assert fetched.json()["data"]["eval_dataset_version_id"] == DATASET_VERSION


def test_due_schedule_is_exactly_once_under_first_concurrency():
    now = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda worker: run_once(now=now, worker_id=worker),
                ("scheduler-a", "scheduler-b"),
            )
        )

    assert sum(result["failure_count"] for result in results) <= 1
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        roots = session.scalars(
            select(RunRecord).where(
                RunRecord.run_type == "label_optimization",
                RunRecord.tenant_id == TENANT,
                RunRecord.project_id == PROJECT,
            )
        ).all()
        roots = [
            row for row in roots if (row.payload or {}).get("label_version_id") == LABEL_VERSION
        ]
        assert len(roots) == 1
        rounds = session.scalars(select(LabelOptimizationRound)).all()
        assert len(rounds) == 1 and rounds[0].round_number == 1
        snapshots = session.scalars(select(LabelOptimizationMetricSnapshot)).all()
        assert len(snapshots) == 1
        assert schedule.next_threshold_scan_at.replace(tzinfo=UTC) > now


def test_candidate_materialization_creates_locked_eval_runs():
    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    first = run_once(now=now, worker_id="scheduler-candidate")
    assert first["processed_count"] == 1
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        root_run_id = schedule.active_run_id
    candidate_ids = _materialize_candidates(root_run_id, 1)

    run_once(now=now + timedelta(minutes=1), worker_id="scheduler-candidate")

    with SessionLocal() as session:
        round_record = session.scalar(select(LabelOptimizationRound))
        assert round_record is not None
        assert round_record.status == "evaluating"
        assert round_record.candidate_ids == candidate_ids
        assert len(round_record.eval_run_ids) == 2
        eval_runs = session.scalars(
            select(RunRecord).where(RunRecord.run_id.in_(round_record.eval_run_ids))
        ).all()
        assert {run.run_type for run in eval_runs} == {"eval_run"}
        assert {run.payload["prompt_version_id"] for run in eval_runs} == set(candidate_ids)
        assert all(
            run.payload["locked_versions"]["optimization_run_id"] == root_run_id
            for run in eval_runs
        )


def test_successful_generation_with_no_candidates_blocks_instead_of_hanging():
    now = datetime(2026, 7, 15, 9, 30, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    run_once(now=now, worker_id="scheduler-empty-candidates")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        generation = session.get(RunRecord, schedule.active_run_id)
        assert generation is not None
        generation.status = "success"
        generation.payload = {
            **generation.payload,
            "status": "success",
            "prompt_candidate_ids": [],
        }
        session.commit()

    run_once(now=now + timedelta(minutes=1), worker_id="scheduler-empty-candidates")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None
        assert round_record.status == "blocked"
        assert round_record.stop_reason_codes == ["candidate_count_out_of_range"]


def test_passing_candidate_is_selected_over_higher_gain_blocked_candidate():
    now = datetime(2026, 7, 15, 9, 45, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    run_once(now=now, worker_id="scheduler-safe-candidate")
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        root_run_id = schedule.active_run_id
    candidate_ids = _materialize_candidates(root_run_id, 1)
    run_once(now=now + timedelta(minutes=1), worker_id="scheduler-safe-candidate")

    with SessionLocal() as session:
        round_record = session.scalar(select(LabelOptimizationRound))
        assert round_record is not None and len(round_record.eval_run_ids) == 2
        result_specs = (
            ("blocked", 4.0, -1.0),
            ("passed", 2.5, 0.0),
        )
        for index, (status, gain_pp, recall_lower_pp) in enumerate(result_specs):
            eval_run = session.get(RunRecord, round_record.eval_run_ids[index])
            assert eval_run is not None
            eval_run.status = "blocked" if status == "blocked" else "success"
            eval_run.payload = {**eval_run.payload, "status": eval_run.status}
            session.add(
                LabelEvalResult(
                    eval_result_id=f"result-safe-selection-{index}",
                    tenant_id=TENANT,
                    project_id=PROJECT,
                    eval_run_id=eval_run.run_id,
                    status=status,
                    binding_sha256=str(eval_run.payload["binding_sha256"]),
                    dataset_snapshot_sha256=str(
                        eval_run.payload["locked_versions"]["eval_dataset_snapshot_sha256"]
                    ),
                    sample_manifest_sha256=f"{index + 4}" * 64,
                    result_sha256=f"{index + 6}" * 64,
                    overall_metrics={
                        "macro_f1_gain_pp": gain_pp,
                        "critical_recall_delta_pp": recall_lower_pp,
                    },
                    bootstrap_ci={
                        "critical_recall_delta_lower_pp": recall_lower_pp,
                    },
                    gate_results=[],
                    trace_id=eval_run.trace_id,
                    payload={},
                )
            )
        session.commit()

    run_once(now=now + timedelta(minutes=2), worker_id="scheduler-safe-candidate")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None
        assert round_record.status == "awaiting-review"
        assert round_record.selected_prompt_version_id == candidate_ids[1]
        assert round_record.latest_gain_ppm == 25_000
        assert round_record.critical_metric_regressed is False
        assert not session.scalars(select(ReleaseDeployment)).all()


def test_failed_generation_stops_with_time_budget_reason():
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    run_once(now=now, worker_id="scheduler-generation-timeout")
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        generation = session.get(RunRecord, schedule.active_run_id)
        assert generation is not None
        generation.status = "failed"
        generation.payload = {
            **generation.payload,
            "status": "failed",
            "error_code": "candidate_generation_failed",
        }
        session.commit()

    run_once(now=now + timedelta(hours=2), worker_id="scheduler-generation-timeout")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None and round_record.status == "blocked"
        assert "time_budget_exceeded" in round_record.stop_reason_codes
        assert "repeated_failure" not in round_record.stop_reason_codes


def test_queued_generation_is_hard_stopped_at_wall_clock_budget():
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    run_once(now=now, worker_id="scheduler-stuck-generation")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        generation_run_id = schedule.active_run_id
        generation = session.get(RunRecord, generation_run_id)
        assert generation is not None and generation.status in {"queued", "submitted"}

    run_once(now=now + timedelta(hours=2), worker_id="scheduler-stuck-generation")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        generation = session.get(RunRecord, generation_run_id)
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None and round_record.status == "blocked"
        assert round_record.stop_reason_codes == ["time_budget_exceeded"]
        assert generation is not None and generation.status == "blocked"
        assert generation.payload["error_code"] == "time_budget_exceeded"


def test_queued_evals_are_hard_stopped_at_wall_clock_budget():
    now = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    run_once(now=now, worker_id="scheduler-stuck-evals")
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        generation_run_id = schedule.active_run_id
    _materialize_candidates(generation_run_id, 1)
    run_once(now=now + timedelta(minutes=1), worker_id="scheduler-stuck-evals")

    with SessionLocal() as session:
        round_record = session.scalar(select(LabelOptimizationRound))
        assert round_record is not None and round_record.status == "evaluating"
        eval_run_ids = list(round_record.eval_run_ids)
        assert len(eval_run_ids) == 2

    run_once(now=now + timedelta(hours=2), worker_id="scheduler-stuck-evals")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        eval_runs = [session.get(RunRecord, run_id) for run_id in eval_run_ids]
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None and round_record.status == "blocked"
        assert round_record.stop_reason_codes == ["time_budget_exceeded"]
        assert all(run is not None and run.status == "blocked" for run in eval_runs)


def test_configured_cost_budget_fails_closed_when_generation_cost_is_missing():
    now = datetime(2026, 7, 15, 10, 30, tzinfo=UTC)
    schedule_id = _schedule(
        now=now,
        budget={
            "max_rounds": 3,
            "min_candidates_per_round": 2,
            "max_candidates_per_round": 5,
            "candidates_per_round": 2,
            "max_elapsed_seconds": 7200,
            "max_cost_micros": 2_000_000,
            "min_meaningful_gain_ppm": 20_000,
            "max_consecutive_failed_rounds": 2,
        },
    )
    run_once(now=now, worker_id="scheduler-missing-cost")
    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id
        generation_run_id = schedule.active_run_id
    _materialize_candidates(generation_run_id, 1)
    with SessionLocal() as session:
        generation = session.get(RunRecord, generation_run_id)
        assert generation is not None
        generation.payload = {
            key: value for key, value in generation.payload.items() if key != "metrics"
        }
        session.commit()

    run_once(now=now + timedelta(minutes=1), worker_id="scheduler-missing-cost")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        round_record = session.scalar(select(LabelOptimizationRound))
        assert schedule is not None and schedule.active_run_id is None
        assert round_record is not None and round_record.status == "blocked"
        assert round_record.stop_reason_codes == ["cost_metric_missing"]
        assert not round_record.eval_run_ids


def test_three_round_budget_stops_without_auto_publish():
    now = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)
    schedule_id = _schedule(
        now=now,
        budget={
            "max_rounds": 3,
            "min_candidates_per_round": 2,
            "max_candidates_per_round": 5,
            "candidates_per_round": 2,
            "max_elapsed_seconds": 7200,
            "min_meaningful_gain_ppm": 20_000,
            "max_consecutive_failed_rounds": 2,
        },
    )
    run_once(now=now, worker_id="scheduler-budget")

    for round_number in (1, 2, 3):
        with SessionLocal() as session:
            round_record = session.scalar(
                select(LabelOptimizationRound)
                .where(LabelOptimizationRound.round_number == round_number)
                .order_by(LabelOptimizationRound.created_at.desc())
            )
            assert round_record is not None
            generation_run_id = round_record.generation_run_id
            round_id = round_record.round_id
        _materialize_candidates(generation_run_id, round_number)
        tick = now + timedelta(minutes=round_number * 10)
        run_once(now=tick, worker_id="scheduler-budget")
        _complete_round_evals(round_id, gain_pp=3.0, passed=False)
        run_once(now=tick + timedelta(minutes=1), worker_id="scheduler-budget")

    with SessionLocal() as session:
        schedule = session.get(LabelOptimizationSchedule, schedule_id)
        assert schedule is not None and schedule.active_run_id is None
        rounds = session.scalars(
            select(LabelOptimizationRound).order_by(LabelOptimizationRound.round_number)
        ).all()
        assert [row.round_number for row in rounds] == [1, 2, 3]
        assert [row.status for row in rounds] == ["completed", "completed", "blocked"]
        assert "max_rounds_reached" in rounds[-1].stop_reason_codes
        assert not session.scalars(select(ReleaseDeployment)).all()


def test_metric_snapshot_rejects_free_text_reason_and_counts_invalid_json():
    now = datetime(2026, 7, 15, 11, 0, tzinfo=UTC)
    schedule_id = _schedule(now=now)
    with SessionLocal() as session:
        session.add(
            FeedbackExample(
                feedback_example_id="feedback-free-text",
                tenant_id=TENANT,
                project_id=PROJECT,
                review_decision_id="decision-free-text",
                review_task_id="task-free-text",
                target_type="label_aggregate",
                target_id="aggregate-free-text",
                feedback_type="human_modified",
                reason_code="客户说这个标签看起来不对",
                field_diff={},
                before_json={"label_version_id": LABEL_VERSION},
                after_json={"label_version_id": LABEL_VERSION},
                gold_status="candidate",
                trace_id="trace-free-text",
                created_at=now - timedelta(hours=1),
            )
        )
        session.add(
            RunRecord(
                run_id="extract-invalid-json",
                tenant_id=TENANT,
                project_id=PROJECT,
                run_type="label_extraction",
                status="failed",
                trace_id="trace-invalid-json",
                payload={
                    "error_code": "INVALID_JSON",
                    "label_version_id": LABEL_VERSION,
                },
                created_at=now - timedelta(minutes=30),
                updated_at=now - timedelta(minutes=30),
            )
        )
        session.commit()

    run_once(now=now, worker_id="scheduler-metrics")

    with SessionLocal() as session:
        snapshot = session.scalar(
            select(LabelOptimizationMetricSnapshot).where(
                LabelOptimizationMetricSnapshot.schedule_id == schedule_id
            )
        )
        assert snapshot is not None
        assert snapshot.rejection_count == 2
        assert snapshot.reason_counts["invalid_json_output"] == 1
        assert {item["reason_code"] for item in snapshot.rejected_records} == {
            "invalid_reason_code",
            "invalid_json_output",
        }
        assert all("客户" not in str(item) for item in snapshot.rejected_records)
