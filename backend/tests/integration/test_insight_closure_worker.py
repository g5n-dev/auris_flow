from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from time import monotonic, sleep
from typing import Any

import pytest
from sqlalchemy import inspect, select, text

from app.core.database import SessionLocal, engine
from app.models import (
    AuditLog,
    InsightEffect,
    InsightExperiment,
    InsightReport,
    MetricResult,
    OutboxEvent,
    RunRecord,
)
from app.workers.outbox_worker import process_once

pytestmark = pytest.mark.usefixtures("configured_test_legacy_generic_execution")

REPORT_METRICS = ["quote_consistency", "reception_conversion_quality"]
REPORT_EVIDENCE = ["AF-128"]
METRIC_OUTPUTS = {
    "quote_consistency": {"value": 74.2, "unit": "percent", "sample_size": 842},
    "reception_conversion_quality": {"value": 82.4, "unit": "score", "sample_size": 1204},
}
REPORT_SCOPE = {
    "time_range": "2025-05-01/2025-05-31",
    "store_ids": ["polar-center"],
    "model_version": "v2.3.1",
    "label_version": "v1.8.4",
}


def _write_headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _start_metric_run(
    client,
    auth_headers: dict[str, str],
    *,
    key: str,
) -> tuple[dict[str, Any], str]:
    response = client.post(
        "/api/v1/insights/metric-runs",
        json={
            "metric_keys": REPORT_METRICS,
            **REPORT_SCOPE,
            "source": "insight_closure_worker",
        },
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 202, response.text
    run = response.json()["data"]
    assert run["run_type"] == "insight_metric_aggregation"
    adapter, external_id = _dispatch_run(run["run_id"])
    assert adapter == "dagster"
    return run, external_id


def _metric_completion_payload(
    *,
    external_id: str,
    receipt_id: str,
) -> dict[str, Any]:
    return {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": receipt_id,
        "external_id": external_id,
        "result_ref": {
            "metric_results": [
                {"metric_key": metric_key, **METRIC_OUTPUTS[metric_key]}
                for metric_key in REPORT_METRICS
            ]
        },
        "metrics": {"materialized_count": len(REPORT_METRICS)},
    }


def _create_report(
    client,
    auth_headers: dict[str, str],
    *,
    key: str,
    title: str = "报价一致性经营报告",
) -> dict[str, Any]:
    metric_run, external_id = _start_metric_run(
        client,
        auth_headers,
        key=f"{key}-metric-run",
    )
    metric_completion = _complete_run(
        client,
        auth_headers,
        run_id=metric_run["run_id"],
        payload=_metric_completion_payload(
            external_id=external_id,
            receipt_id=f"{key}-metric-completion",
        ),
        key=f"{key}-metric-receipt",
    )
    assert metric_completion.status_code == 200, metric_completion.text
    metric_result_ids = metric_completion.json()["data"]["insight_completion"]["metric_result_ids"]
    response = client.post(
        "/api/v1/insights/reports",
        json={
            "title": title,
            "report_type": "management_summary",
            **REPORT_SCOPE,
            "metric_result_ids": metric_result_ids,
            "evidence_refs": REPORT_EVIDENCE,
            "report_sections": ["north_star", "risk_root_cause", "next_actions"],
            "source": "insight_closure_worker",
        },
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["report_id"], data
    assert data["run_id"], data
    assert data["metric_result_ids"] == metric_result_ids, data
    assert len(data["metric_result_ids"]) == len(REPORT_METRICS), data
    return data


def _report_detail(client, auth_headers: dict[str, str], report_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/insights/reports/{report_id}", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _metric_detail(metric_result_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        metric = session.get(MetricResult, metric_result_id)
        assert metric is not None, f"强表缺少指标快照：{metric_result_id}"
        return {
            **deepcopy(metric.payload),
            "metric_result_id": metric.metric_result_id,
            "status": metric.status,
            "trace_id": metric.trace_id,
        }


def _mark_report_generated(report_id: str) -> None:
    with SessionLocal() as session:
        report = session.get(InsightReport, report_id)
        assert report is not None
        report.status = "generated"
        report.payload = {**report.payload, "status": "generated"}
        session.commit()


def _dispatch_run(run_id: str) -> tuple[str, str]:
    with SessionLocal() as session:
        before = session.get(RunRecord, run_id)
        assert before is not None
        before_status = before.status
    if before_status == "pending":
        deadline = monotonic() + 2.0
        while True:
            process_once()
            with SessionLocal() as session:
                current = session.get(RunRecord, run_id)
                assert current is not None
                if current.status != "pending":
                    break
            if monotonic() >= deadline:
                raise AssertionError(f"run {run_id} remained pending after its dispatch deadline")
            sleep(0.01)

    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted", run.payload
        assert run.payload["business_completion_required"] is True
        dispatch = run.payload["dispatch"]
        adapter = dispatch["adapter"]
        external_key = {
            "dagster": "external_run_id",
            "object_storage": "storage_object_id",
            "external_callback": "callback_receipt_id",
        }[adapter]
        external_id = dispatch["details"][external_key]
        assert external_id
        return adapter, str(external_id)


def _object_storage_reservation(run_id: str) -> dict[str, Any]:
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.status == "submitted", run.payload
        dispatch = run.payload["dispatch"]
        assert dispatch["adapter"] == "object_storage"
        assert dispatch["operation"] == "reserve_object"
        return deepcopy(dispatch["details"])


def _report_success_receipt(
    *,
    reservation: dict[str, Any],
    receipt_id: str,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "adapter": "object_storage",
        "status": "success",
        "completion_receipt_id": receipt_id,
        "external_id": reservation["storage_object_id"],
        "result_ref": {
            "storage_object_id": reservation["storage_object_id"],
            "object_uri": reservation["object_uri"],
            "content_sha256": content_sha256 or reservation["content_sha256"],
            "content_type": reservation["content_type"],
        },
        "metrics": {"section_count": 3, "evidence_count": 1},
    }


def _complete_run(
    client,
    auth_headers: dict[str, str],
    *,
    run_id: str,
    payload: dict[str, Any],
    key: str,
):
    return client.post(
        f"/api/v1/runs/{run_id}/completion-receipts",
        json=payload,
        headers=_write_headers(auth_headers, key),
    )


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise AssertionError(f"expected JSON object, got {type(value)!r}")


def _strong_resource_row(
    table_name: str,
    id_column: str,
    resource_id: str,
) -> dict[str, Any]:
    tables = set(inspect(engine).get_table_names())
    assert table_name in tables, f"{table_name} 必须是 MySQL/SQLite 强表而非 JSON projection"
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    f"SELECT {id_column}, tenant_id, project_id, status, trace_id, payload "
                    f"FROM {table_name} WHERE {id_column} = :resource_id"
                ),
                {"resource_id": resource_id},
            )
            .mappings()
            .one_or_none()
        )
    assert row is not None, f"{table_name}.{resource_id} 未物化"
    return {**dict(row), "payload": _decode_json(row["payload"])}


def _create_normal_action(
    client,
    auth_headers: dict[str, str],
    report: dict[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    _mark_report_generated(report["report_id"])
    response = client.post(
        "/api/v1/insights/actions",
        json={
            "report_id": report["report_id"],
            "metric_result_id": report["metric_result_ids"][1],
            "action_type": "create_training_action",
            "risk_level": "medium",
            "owner": "业务运营",
            "hypothesis": "统一报价话术和单据校验可以提升报价一致率",
            "evidence_refs": REPORT_EVIDENCE,
            "source": "insight_closure_worker",
        },
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["action_id"], data
    assert data["status"] == "experiment_ready", data
    assert data["branch"] == "experiment", data
    return data


def _create_experiment(
    client,
    auth_headers: dict[str, str],
    action: dict[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json={
            "allocation_percent": 20,
            "min_sample_size": 400,
            "duration_days": 7,
            "primary_metric_key": action["metric_key"],
            "hypothesis": "候选策略相较当前策略能够提升主指标且不突破风险护栏",
            "candidate": {"strategy": "统一话术与单据前置校验"},
            "control": {"strategy": "保持当前话术与事后抽检"},
            "guardrails": {"conflict_rate_max": 0.06},
        },
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["experiment_id"], data
    assert data["eval_run_id"] == data["run_id"], data
    assert data["run_type"] == "eval_run", data
    assert data["status"] == "pending", data
    assert data["action_id"] == action["action_id"], data
    return data


def _effect_receipt(
    *,
    action: dict[str, Any],
    experiment: dict[str, Any],
    baseline: dict[str, Any],
    external_id: str,
    receipt_id: str,
) -> dict[str, Any]:
    outcome_value = float(baseline["value"]) + 6.3
    return {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": receipt_id,
        "external_id": external_id,
        "result_ref": {
            "action_id": action["action_id"],
            "experiment_id": experiment["experiment_id"],
            "outcome_metric": {
                "metric_key": baseline["metric_key"],
                "value": outcome_value,
                "unit": baseline["unit"],
                "sample_size": 960,
                "confidence_interval": {
                    "lower": outcome_value - 1.2,
                    "upper": outcome_value + 1.2,
                },
            },
        },
        "metrics": {
            baseline["metric_key"]: outcome_value,
            "sample_size": 960,
            "statistically_significant": True,
            "confidence_interval": {
                "low": outcome_value - 1.2,
                "high": outcome_value + 1.2,
            },
        },
    }


def test_metric_aggregation_completion_materializes_immutable_results_once(client, auth_headers):
    run, external_id = _start_metric_run(
        client,
        auth_headers,
        key="worker-metric-materialize-run",
    )
    payload = _metric_completion_payload(
        external_id=external_id,
        receipt_id="worker-metric-materialize-receipt",
    )
    first = _complete_run(
        client,
        auth_headers,
        run_id=run["run_id"],
        payload=payload,
        key="worker-metric-materialize-complete",
    )
    replay = _complete_run(
        client,
        auth_headers,
        run_id=run["run_id"],
        payload=payload,
        key="worker-metric-materialize-complete",
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["data"] == replay.json()["data"]
    result_ids = first.json()["data"]["insight_completion"]["metric_result_ids"]
    assert len(result_ids) == len(REPORT_METRICS)
    for metric_result_id in result_ids:
        result = _metric_detail(metric_result_id)
        assert result["source_run_id"] == run["run_id"]
        assert result["scope"] == REPORT_SCOPE
        assert result["snapshot_role"] == "aggregation"
        assert result["status"] == "materialized"
        assert result["immutable"] is True
        assert result["sample_size"] >= 1

    with SessionLocal() as session:
        persisted_run = session.get(RunRecord, run["run_id"])
        assert persisted_run is not None
        assert persisted_run.status == "success"
        scoped_results = session.scalars(
            select(MetricResult).where(MetricResult.tenant_id == "aurora_auto")
        ).all()
        assert [
            item.metric_result_id
            for item in scoped_results
            if item.payload.get("source_run_id") == run["run_id"]
        ] == result_ids


@pytest.mark.parametrize(
    ("scenario", "error_code"),
    [
        ("missing_field", "INSIGHT_METRIC_RESULT_INVALID"),
        ("missing_metric", "INSIGHT_METRIC_RESULT_SET_MISMATCH"),
        ("unexpected_metric", "INSIGHT_METRIC_RESULT_SET_MISMATCH"),
        ("duplicate_metric", "INSIGHT_METRIC_RESULT_SET_MISMATCH"),
        ("unit_mismatch", "INSIGHT_METRIC_UNIT_MISMATCH"),
    ],
)
def test_invalid_metric_aggregation_receipt_rolls_back_without_results(
    client,
    auth_headers,
    scenario: str,
    error_code: str,
):
    run, external_id = _start_metric_run(
        client,
        auth_headers,
        key=f"worker-invalid-metric-run-{scenario}",
    )
    payload = _metric_completion_payload(
        external_id=external_id,
        receipt_id=f"worker-invalid-metric-receipt-{scenario}",
    )
    results = payload["result_ref"]["metric_results"]
    if scenario == "missing_field":
        results[0].pop("sample_size")
    elif scenario == "missing_metric":
        results.pop()
    elif scenario == "unexpected_metric":
        results.append(
            {"metric_key": "unrequested_metric", "value": 1.0, "unit": "score", "sample_size": 10}
        )
    elif scenario == "duplicate_metric":
        results.append(dict(results[0]))
    elif scenario == "unit_mismatch":
        results[0]["unit"] = "score"

    response = _complete_run(
        client,
        auth_headers,
        run_id=run["run_id"],
        payload=payload,
        key=f"worker-invalid-metric-complete-{scenario}",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == error_code
    with SessionLocal() as session:
        persisted_run = session.get(RunRecord, run["run_id"])
        assert persisted_run is not None
        assert persisted_run.status == "submitted"
        all_results = session.scalars(select(MetricResult)).all()
        assert not any(item.payload.get("source_run_id") == run["run_id"] for item in all_results)


def test_report_success_completion_materializes_generated_report_once(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-insight-report-success")
    report_id = report["report_id"]
    run_id = report["run_id"]
    baseline_before = [_metric_detail(metric_id) for metric_id in report["metric_result_ids"]]

    for snapshot in baseline_before:
        row = _strong_resource_row(
            "metric_results", "metric_result_id", snapshot["metric_result_id"]
        )
        assert row["tenant_id"] == "aurora_auto"
        assert row["project_id"] == "sales_qa"
        assert row["trace_id"] == snapshot["trace_id"]
        assert row["payload"]["source_run_id"]
        assert row["payload"].get("source_report_id") is None
        assert row["payload"]["snapshot_role"] == "aggregation"
        assert row["payload"]["status"] == "materialized"
        assert row["payload"]["immutable"] is True

    adapter, external_id = _dispatch_run(run_id)
    assert adapter == "object_storage"
    reservation = _object_storage_reservation(run_id)
    assert reservation["storage_object_id"] == external_id
    payload = _report_success_receipt(
        reservation=reservation,
        receipt_id="insight_report_generated_once",
    )
    headers_key = "worker-insight-report-success-receipt"
    first = _complete_run(
        client,
        auth_headers,
        run_id=run_id,
        payload=payload,
        key=headers_key,
    )
    replay = _complete_run(
        client,
        auth_headers,
        run_id=run_id,
        payload=payload,
        key=headers_key,
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["data"] == replay.json()["data"]

    detail = _report_detail(client, auth_headers, report_id)
    assert detail["status"] == "generated"
    assert detail["run_id"] == run_id
    assert detail["result_ref"]["storage_object_id"] == external_id
    assert detail["result_ref"]["content_sha256"] == reservation["content_sha256"]
    assert detail["result_ref"]["artifact_state"] == "materialized"
    assert detail["completed_at"]
    assert detail["metric_result_ids"] == report["metric_result_ids"]
    assert [
        _metric_detail(metric_id) for metric_id in report["metric_result_ids"]
    ] == baseline_before


def test_report_completion_rejects_tampered_frozen_metric_value(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-insight-report-tampered-value")
    _dispatch_run(report["run_id"])
    reservation = _object_storage_reservation(report["run_id"])

    with SessionLocal() as session:
        run = session.get(RunRecord, report["run_id"])
        assert run is not None
        document = deepcopy(run.payload["report_document"])
        document["metric_results"][0]["value"] = 999.0
        metric_body = json.dumps(
            document["metric_results"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        document["generator_proof"]["metric_snapshot_sha256"] = hashlib.sha256(
            metric_body
        ).hexdigest()
        run.payload = {**run.payload, "report_document": document}
        session.commit()

    rejected = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload=_report_success_receipt(
            reservation=reservation,
            receipt_id="insight_report_tampered_metric_value",
        ),
        key="worker-insight-report-tampered-value-receipt",
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "INSIGHT_REPORT_DOCUMENT_HASH_MISMATCH"
    mismatch = rejected.json()["error"]["details"][0]["metric_hash_mismatches"][0]
    assert mismatch["field_mismatches"][0]["field"] == "value"
    assert _report_detail(client, auth_headers, report["report_id"])["status"] == "generating"


@pytest.mark.parametrize(
    ("scenario", "error_code", "status_code"),
    [
        ("arbitrary_json", "INSIGHT_REPORT_ARTIFACT_INVALID", 422),
        ("invalid_checksum", "INSIGHT_REPORT_ARTIFACT_INVALID", 422),
        ("hash_mismatch", "INSIGHT_REPORT_ARTIFACT_HASH_MISMATCH", 409),
        ("uncontrolled_content_type", "INSIGHT_REPORT_ARTIFACT_INVALID", 422),
        ("storage_object_mismatch", "INSIGHT_REPORT_RESERVATION_MISMATCH", 409),
        ("object_uri_mismatch", "INSIGHT_REPORT_RESERVATION_MISMATCH", 409),
        ("content_type_mismatch", "INSIGHT_REPORT_ARTIFACT_INVALID", 422),
    ],
)
def test_report_success_receipt_requires_valid_reserved_artifact_before_generation(
    client,
    auth_headers,
    caplog,
    scenario: str,
    error_code: str,
    status_code: int,
):
    report = _create_report(
        client,
        auth_headers,
        key=f"worker-insight-report-invalid-artifact-{scenario}",
    )
    adapter, _external_id = _dispatch_run(report["run_id"])
    assert adapter == "object_storage"
    reservation = _object_storage_reservation(report["run_id"])
    payload = _report_success_receipt(
        reservation=reservation,
        receipt_id=f"insight_report_invalid_artifact_{scenario}",
    )
    result_ref = payload["result_ref"]
    if scenario == "arbitrary_json":
        payload["result_ref"] = {"message": "rendered"}
    elif scenario == "invalid_checksum":
        result_ref["content_sha256"] = "not-a-sha256"
    elif scenario == "hash_mismatch":
        result_ref["content_sha256"] = "f" * 64
    elif scenario == "uncontrolled_content_type":
        result_ref["content_type"] = "image/png"
    elif scenario == "storage_object_mismatch":
        result_ref["storage_object_id"] = "obj_other_report"
    elif scenario == "object_uri_mismatch":
        result_ref["object_uri"] = "mock://object-storage/obj_other_report"
    elif scenario == "content_type_mismatch":
        result_ref["content_type"] = "application/pdf"

    completion_key = f"worker-insight-report-invalid-artifact-receipt-{scenario}"
    rejected = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload=payload,
        key=completion_key,
    )
    assert rejected.status_code == status_code, rejected.text
    error = rejected.json()["error"]
    assert error["code"] == error_code
    assert error["retryable"] is True
    assert error["trace_id"]
    assert any(
        record.name == "auris_flow.app"
        and '"event": "http.api_error"' in record.getMessage()
        and f'"error_code": "{error_code}"' in record.getMessage()
        and f'"trace_id": "{error["trace_id"]}"' in record.getMessage()
        for record in caplog.records
    )
    if error_code == "INSIGHT_REPORT_RESERVATION_MISMATCH":
        expected_field = {
            "storage_object_mismatch": "storage_object_id",
            "object_uri_mismatch": "object_uri",
        }[scenario]
        assert error["details"][0]["mismatches"][0]["field"] == expected_field

    with SessionLocal() as session:
        persisted_run = session.get(RunRecord, report["run_id"])
        persisted_report = session.get(InsightReport, report["report_id"])
        assert persisted_run is not None
        assert persisted_run.status == "submitted"
        assert "completion_receipt" not in persisted_run.payload
        assert persisted_report is not None
        assert persisted_report.status == "generating"
        assert persisted_report.payload.get("result_ref") is None

    corrected = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload=_report_success_receipt(
            reservation=reservation,
            receipt_id=f"insight_report_corrected_artifact_{scenario}",
        ),
        key=completion_key,
    )
    assert corrected.status_code == 200, corrected.text
    generated = _report_detail(client, auth_headers, report["report_id"])
    assert generated["status"] == "generated"
    assert generated["result_ref"]["storage_object_id"] == reservation["storage_object_id"]


def test_report_placeholder_reservation_cannot_be_marked_generated(client, auth_headers):
    report = _create_report(
        client,
        auth_headers,
        key="worker-insight-report-placeholder-reservation",
    )
    _dispatch_run(report["run_id"])
    reservation = _object_storage_reservation(report["run_id"])
    with SessionLocal() as session:
        run = session.get(RunRecord, report["run_id"])
        assert run is not None
        dispatch = dict(run.payload["dispatch"])
        dispatch["details"] = {
            **dispatch["details"],
            "artifact_state": "reserved",
        }
        run.payload = {**run.payload, "dispatch": dispatch}
        session.commit()

    rejected = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload=_report_success_receipt(
            reservation=reservation,
            receipt_id="insight_report_placeholder_reservation",
        ),
        key="worker-insight-report-placeholder-reservation-receipt",
    )

    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "INSIGHT_REPORT_ARTIFACT_NOT_MATERIALIZED"
    detail = _report_detail(client, auth_headers, report["report_id"])
    assert detail["status"] == "generating"


def test_report_failed_completion_materializes_failed_report(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-insight-report-failed")
    adapter, external_id = _dispatch_run(report["run_id"])
    assert adapter == "object_storage"
    failed = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload={
            "adapter": "object_storage",
            "status": "failed",
            "completion_receipt_id": "insight_report_render_failed",
            "external_id": external_id,
            "result_ref": {"storage_object_id": external_id},
            "metrics": {},
            "note": "报告渲染器返回不可恢复错误",
            "error_code": "REPORT_RENDER_FAILED",
            "retryable": False,
        },
        key="worker-insight-report-failed-receipt",
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["data"]["status"] == "failed"

    detail = _report_detail(client, auth_headers, report["report_id"])
    assert detail["status"] == "failed"
    assert detail["run_id"] == report["run_id"]
    assert detail["failure"]["error_code"] == "REPORT_RENDER_FAILED"
    assert detail["failure"]["retryable"] is False
    assert detail["completed_at"]
    with SessionLocal() as session:
        completion_audit = session.scalar(
            select(AuditLog).where(
                AuditLog.object_id == report["run_id"],
                AuditLog.action == "insight_report.completion_received",
            )
        )
        assert completion_audit is not None
        assert completion_audit.result == "failed"
        assert completion_audit.trace_id == report["trace_id"]
        assert completion_audit.after_json is not None
        assert completion_audit.after_json["error_code"] == "REPORT_RENDER_FAILED"


def test_failed_report_retry_rebinds_projection_and_can_complete(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-insight-report-retry")
    source_run_id = report["run_id"]
    adapter, external_id = _dispatch_run(source_run_id)
    assert adapter == "object_storage"
    failed = _complete_run(
        client,
        auth_headers,
        run_id=source_run_id,
        payload={
            "adapter": "object_storage",
            "status": "failed",
            "completion_receipt_id": "insight_report_retryable_failure",
            "external_id": external_id,
            "result_ref": {"storage_object_id": external_id},
            "metrics": {"render_attempt": 1},
            "note": "临时对象存储故障",
            "error_code": "REPORT_STORAGE_TEMPORARY",
            "retryable": True,
        },
        key="worker-insight-report-retry-failed-receipt",
    )
    assert failed.status_code == 200, failed.text

    retry = client.post(
        f"/api/v1/runs/{source_run_id}/retries",
        json={"reason": "对象存储恢复后人工重试"},
        headers=_write_headers(auth_headers, "worker-insight-report-retry-request"),
    )
    assert retry.status_code == 202, retry.text
    retry_replay = client.post(
        f"/api/v1/runs/{source_run_id}/retries",
        json={"reason": "对象存储恢复后人工重试"},
        headers=_write_headers(auth_headers, "worker-insight-report-retry-request"),
    )
    assert retry_replay.status_code == 202, retry_replay.text
    assert retry_replay.json()["data"] == retry.json()["data"]
    retry_data = retry.json()["data"]
    retry_run_id = retry_data["run_id"]
    assert retry_run_id != source_run_id
    assert retry_data["status"] == "pending"
    assert retry_data["retry_of_run_id"] == source_run_id
    assert "completion_receipt" not in retry_data
    assert "result_ref" not in retry_data
    assert "metrics" not in retry_data
    assert "business_completion_required" not in retry_data

    rebound = _report_detail(client, auth_headers, report["report_id"])
    assert rebound["status"] == "generating"
    assert rebound["run_id"] == retry_run_id
    assert rebound["retry_of_run_id"] == source_run_id
    assert rebound.get("failure") is None
    assert rebound.get("completed_at") is None
    assert rebound["retry_history"][-1]["from_run_id"] == source_run_id
    assert rebound["retry_history"][-1]["to_run_id"] == retry_run_id

    retry_adapter, retry_external_id = _dispatch_run(retry_run_id)
    assert retry_adapter == "object_storage"
    retry_reservation = _object_storage_reservation(retry_run_id)
    assert retry_reservation["storage_object_id"] == retry_external_id
    completed = _complete_run(
        client,
        auth_headers,
        run_id=retry_run_id,
        payload={
            **_report_success_receipt(
                reservation=retry_reservation,
                receipt_id="insight_report_retry_success",
            ),
            "metrics": {"section_count": 3, "render_attempt": 2},
        },
        key="worker-insight-report-retry-success-receipt",
    )
    assert completed.status_code == 200, completed.text
    generated = _report_detail(client, auth_headers, report["report_id"])
    assert generated["status"] == "generated"
    assert generated["run_id"] == retry_run_id
    assert generated["result_ref"]["storage_object_id"] == retry_external_id

    with SessionLocal() as session:
        source_run = session.get(RunRecord, source_run_id)
        retry_run = session.get(RunRecord, retry_run_id)
        assert source_run is not None and source_run.status == "failed"
        assert source_run.payload["completion_receipt"]["completion_receipt_id"] == (
            "insight_report_retryable_failure"
        )
        assert retry_run is not None and retry_run.status == "success"


def test_experiment_completion_materializes_outcome_effect_and_measured_state_once(
    client, auth_headers
):
    report = _create_report(client, auth_headers, key="worker-insight-experiment-report")
    action = _create_normal_action(
        client,
        auth_headers,
        report,
        key="worker-insight-experiment-action",
    )
    experiment = _create_experiment(
        client,
        auth_headers,
        action,
        key="worker-insight-experiment-create",
    )
    baseline = _metric_detail(action["metric_result_id"])
    baseline_value = float(baseline["value"])
    outcome_value = baseline_value + 6.3

    adapter, external_id = _dispatch_run(experiment["run_id"])
    assert adapter == "dagster"
    payload = {
        "adapter": "dagster",
        "status": "success",
        "completion_receipt_id": "insight_experiment_measured_once",
        "external_id": external_id,
        "result_ref": {
            "action_id": action["action_id"],
            "experiment_id": experiment["experiment_id"],
            "outcome_metric": {
                "metric_key": baseline["metric_key"],
                "value": outcome_value,
                "unit": baseline["unit"],
                "sample_size": 960,
                "confidence_interval": {"lower": outcome_value - 1.2, "upper": outcome_value + 1.2},
            },
        },
        "metrics": {
            baseline["metric_key"]: outcome_value,
            "sample_size": 960,
            "statistically_significant": True,
            "confidence_interval": {"low": outcome_value - 1.2, "high": outcome_value + 1.2},
        },
    }
    key = "worker-insight-experiment-completion"
    first = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload=payload,
        key=key,
    )
    replay = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload=payload,
        key=key,
    )
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["data"] == replay.json()["data"]
    completion = first.json()["data"]
    outcome_metric_result_id = completion["insight_completion"]["outcome_metric_result_id"]
    effect_id = completion["insight_completion"]["effect_id"]
    assert outcome_metric_result_id
    assert effect_id

    action_detail = client.get(
        f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers
    )
    assert action_detail.status_code == 200, action_detail.text
    action_data = action_detail.json()["data"]
    assert action_data["status"] == "measured"
    assert action_data["experiment_id"] == experiment["experiment_id"]
    assert action_data["effect_id"] == effect_id
    assert any(
        item["experiment_id"] == experiment["experiment_id"] for item in action_data["experiments"]
    )
    assert any(item["effect_id"] == effect_id for item in action_data["effects"])

    experiment_detail = client.get(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}",
        headers=auth_headers,
    )
    assert experiment_detail.status_code == 200, experiment_detail.text
    experiment_data = experiment_detail.json()["data"]
    assert experiment_data["status"] == "measured"
    assert experiment_data["eval_run_id"] == experiment["run_id"]
    assert experiment_data["outcome_metric_result_id"] == outcome_metric_result_id
    assert experiment_data["effect_id"] == effect_id

    outcome = _metric_detail(outcome_metric_result_id)
    assert outcome["snapshot_role"] == "outcome"
    assert outcome["source_experiment_id"] == experiment["experiment_id"]
    assert outcome["value"] == pytest.approx(outcome_value)
    assert outcome["immutable"] is True

    effects = client.get(
        f"/api/v1/insights/effects?action_id={action['action_id']}", headers=auth_headers
    )
    assert effects.status_code == 200, effects.text
    effect_items = effects.json()["data"]["items"]
    assert len(effect_items) == 1
    effect = effect_items[0]
    assert effect["effect_id"] == effect_id
    assert effect["action_id"] == action["action_id"]
    assert effect["experiment_id"] == experiment["experiment_id"]
    assert effect["baseline_metric_result_id"] == action["metric_result_id"]
    assert effect["outcome_metric_result_id"] == outcome_metric_result_id
    assert effect["delta"] == pytest.approx(6.3)
    assert effect["sample_size"] == 960
    assert effect["statistically_significant"] is True

    outcome_row = _strong_resource_row(
        "metric_results", "metric_result_id", outcome_metric_result_id
    )
    assert outcome_row["tenant_id"] == "aurora_auto"
    assert outcome_row["project_id"] == "sales_qa"
    assert outcome_row["payload"]["source_experiment_id"] == experiment["experiment_id"]
    effect_row = _strong_resource_row("insight_effects", "effect_id", effect_id)
    assert effect_row["tenant_id"] == "aurora_auto"
    assert effect_row["project_id"] == "sales_qa"
    assert effect_row["payload"]["delta"] == pytest.approx(6.3)


def test_failed_insight_experiment_dedicated_retry_reaches_measured_effect(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-experiment-retry-report")
    action = _create_normal_action(
        client,
        auth_headers,
        report,
        key="worker-experiment-retry-action",
    )
    experiment = _create_experiment(
        client,
        auth_headers,
        action,
        key="worker-experiment-retry-create",
    )
    baseline = _metric_detail(action["metric_result_id"])

    running_retry = client.post(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}/retry-attempts",
        json={"reason": "运行中不应创建重试 attempt"},
        headers=_write_headers(auth_headers, "worker-experiment-running-retry"),
    )
    assert running_retry.status_code == 409, running_retry.text
    assert running_retry.json()["error"]["code"] == "INSIGHT_EXPERIMENT_NOT_RETRYABLE"

    adapter, external_id = _dispatch_run(experiment["run_id"])
    assert adapter == "dagster"
    failed = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload={
            "adapter": "dagster",
            "status": "failed",
            "completion_receipt_id": "insight_experiment_failed_before_retry",
            "external_id": external_id,
            "result_ref": {"experiment_id": experiment["experiment_id"]},
            "metrics": {},
            "note": "实验执行失败",
            "error_code": "EXPERIMENT_EXECUTION_FAILED",
            "retryable": True,
        },
        key="worker-experiment-retry-failure-receipt",
    )
    assert failed.status_code == 200, failed.text

    retry = client.post(
        f"/api/v1/runs/{experiment['run_id']}/retries",
        json={"reason": "错误地尝试通用重试"},
        headers=_write_headers(auth_headers, "worker-experiment-generic-retry"),
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "RUN_RETRY_REQUIRES_EXPERIMENT_COMMAND"

    project_id = "insight_retry_isolation_project"
    project = client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "name": "洞察重试隔离项目"},
        headers=_write_headers(auth_headers, "worker-experiment-retry-project-create"),
    )
    assert project.status_code == 201, project.text
    hidden = client.post(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}/retry-attempts",
        json={"reason": "跨项目重试不应定位到实验"},
        headers=_write_headers(
            {**auth_headers, "X-Project-Id": project_id},
            "worker-experiment-cross-project-retry",
        ),
    )
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == "NOT_FOUND"

    retry_headers = _write_headers(auth_headers, "worker-experiment-dedicated-retry")
    retry_payload = {
        "reason": "Dagster 临时执行故障已恢复",
        "source": "insight_operations",
    }
    first_retry = client.post(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}/retry-attempts",
        json=retry_payload,
        headers=retry_headers,
    )
    replay_retry = client.post(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}/retry-attempts",
        json=retry_payload,
        headers=retry_headers,
    )
    assert first_retry.status_code == 202, first_retry.text
    assert replay_retry.status_code == 202, replay_retry.text
    retry_run = first_retry.json()["data"]
    assert replay_retry.json()["data"] == retry_run
    assert retry_run["run_id"] != experiment["run_id"]
    assert retry_run["run_type"] == "eval_run"
    assert retry_run["status"] == "pending"
    assert retry_run["parent_run_id"] == experiment["run_id"]
    assert retry_run["retry_of"] == experiment["run_id"]
    assert retry_run["retry_of_run_id"] == experiment["run_id"]
    assert retry_run["retry_of_trace_id"] == experiment["trace_id"]
    assert retry_run["experiment_id"] == experiment["experiment_id"]
    assert retry_run["action_id"] == action["action_id"]
    assert retry_run["report_id"] == report["report_id"]
    assert retry_run["baseline_metric_result_id"] == action["metric_result_id"]
    assert retry_run["retry_attempt"] == 1
    assert retry_run["attempt_id"]

    experiment_detail = client.get(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}",
        headers=auth_headers,
    )
    assert experiment_detail.status_code == 200, experiment_detail.text
    rebound = experiment_detail.json()["data"]
    assert rebound["status"] == "running"
    assert rebound["eval_run_id"] == retry_run["run_id"]
    assert rebound["retry_attempt"] == 1
    assert rebound["retry_history"][-1]["from_run_id"] == experiment["run_id"]
    assert rebound["retry_history"][-1]["to_run_id"] == retry_run["run_id"]

    action_detail = client.get(
        f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers
    )
    assert action_detail.status_code == 200, action_detail.text
    assert action_detail.json()["data"]["status"] == "experiment_running"
    assert action_detail.json()["data"]["eval_run_id"] == retry_run["run_id"]

    with SessionLocal() as session:
        source_record = session.get(RunRecord, experiment["run_id"])
        retry_record = session.get(RunRecord, retry_run["run_id"])
        assert source_record is not None and source_record.status == "failed"
        assert retry_record is not None and retry_record.status == "pending"
        retry_events = session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == retry_run["run_id"],
                OutboxEvent.event_type == "eval_run.requested",
            )
        ).all()
        assert len(retry_events) == 1
        assert retry_events[0].tenant_id == "aurora_auto"
        assert retry_events[0].project_id == "sales_qa"
        assert retry_events[0].payload["parent_run_id"] == experiment["run_id"]
        retry_audits = session.scalars(
            select(AuditLog).where(
                AuditLog.object_id == experiment["experiment_id"],
                AuditLog.action == "insight_experiment.retry_attempt.create",
            )
        ).all()
        assert len(retry_audits) == 1
        assert retry_audits[0].after_json is not None
        assert retry_audits[0].after_json["eval_run_id"] == retry_run["run_id"]

    retry_adapter, retry_external_id = _dispatch_run(retry_run["run_id"])
    assert retry_adapter == "dagster"
    succeeded = _complete_run(
        client,
        auth_headers,
        run_id=retry_run["run_id"],
        payload=_effect_receipt(
            action=action,
            experiment=experiment,
            baseline=baseline,
            external_id=retry_external_id,
            receipt_id="insight_experiment_retry_succeeded",
        ),
        key="worker-experiment-retry-success-receipt",
    )
    assert succeeded.status_code == 200, succeeded.text
    completion = succeeded.json()["data"]["insight_completion"]
    assert completion["status"] == "measured"
    assert completion["experiment_id"] == experiment["experiment_id"]
    assert completion["effect_id"]

    measured = client.get(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}",
        headers=auth_headers,
    )
    assert measured.status_code == 200, measured.text
    measured_data = measured.json()["data"]
    assert measured_data["status"] == "measured"
    assert measured_data["eval_run_id"] == retry_run["run_id"]
    assert measured_data["effect_id"] == completion["effect_id"]
    outcome = _metric_detail(measured_data["outcome_metric_result_id"])
    assert outcome["source_eval_run_id"] == retry_run["run_id"]


def test_non_retryable_failed_insight_experiment_rejects_dedicated_retry(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-nonretryable-report")
    action = _create_normal_action(
        client,
        auth_headers,
        report,
        key="worker-nonretryable-action",
    )
    experiment = _create_experiment(
        client,
        auth_headers,
        action,
        key="worker-nonretryable-experiment",
    )
    _adapter, external_id = _dispatch_run(experiment["run_id"])
    failed = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload={
            "adapter": "dagster",
            "status": "failed",
            "completion_receipt_id": "insight_experiment_nonretryable_failure",
            "external_id": external_id,
            "result_ref": {"experiment_id": experiment["experiment_id"]},
            "metrics": {},
            "note": "实验设计无效，禁止原样重试",
            "error_code": "EXPERIMENT_DESIGN_INVALID",
            "retryable": False,
        },
        key="worker-nonretryable-failure-receipt",
    )
    assert failed.status_code == 200, failed.text

    retry = client.post(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}/retry-attempts",
        json={"reason": "错误地重试不可恢复失败"},
        headers=_write_headers(auth_headers, "worker-nonretryable-dedicated-retry"),
    )
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["code"] == "INSIGHT_EXPERIMENT_NOT_RETRYABLE"

    with SessionLocal() as session:
        experiment_row = session.get(InsightExperiment, experiment["experiment_id"])
        assert experiment_row is not None and experiment_row.status == "failed"
        eval_runs = session.scalars(
            select(RunRecord).where(
                RunRecord.tenant_id == "aurora_auto",
                RunRecord.project_id == "sales_qa",
                RunRecord.run_type == "eval_run",
                RunRecord.payload["insight_experiment_id"].as_string()
                == experiment["experiment_id"],
            )
        ).all()
        assert [record.run_id for record in eval_runs] == [experiment["run_id"]]


@pytest.mark.parametrize(
    ("scenario", "error_code"),
    [
        ("experiment_id_mismatch", "INSIGHT_EFFECT_EXPERIMENT_MISMATCH"),
        ("missing_sample_size", "INSIGHT_EFFECT_SAMPLE_SIZE_INVALID"),
        ("insufficient_sample_size", "INSIGHT_EFFECT_SAMPLE_SIZE_INSUFFICIENT"),
        ("missing_confidence_interval", "INSIGHT_EFFECT_CONFIDENCE_INVALID"),
        ("outcome_outside_confidence_interval", "INSIGHT_EFFECT_CONFIDENCE_INVALID"),
        ("missing_significance", "INSIGHT_EFFECT_SIGNIFICANCE_REQUIRED"),
    ],
)
def test_invalid_experiment_receipt_never_materializes_measured_effect(
    client,
    auth_headers,
    scenario: str,
    error_code: str,
):
    report = _create_report(
        client,
        auth_headers,
        key=f"worker-invalid-effect-report-{scenario}",
    )
    action = _create_normal_action(
        client,
        auth_headers,
        report,
        key=f"worker-invalid-effect-action-{scenario}",
    )
    experiment = _create_experiment(
        client,
        auth_headers,
        action,
        key=f"worker-invalid-effect-experiment-{scenario}",
    )
    baseline = _metric_detail(action["metric_result_id"])
    _adapter, external_id = _dispatch_run(experiment["run_id"])
    payload = _effect_receipt(
        action=action,
        experiment=experiment,
        baseline=baseline,
        external_id=external_id,
        receipt_id=f"invalid-effect-{scenario}",
    )
    if scenario == "experiment_id_mismatch":
        payload["result_ref"]["experiment_id"] = "insight_experiment_wrong"
    elif scenario == "missing_sample_size":
        payload["metrics"].pop("sample_size")
    elif scenario == "insufficient_sample_size":
        payload["metrics"]["sample_size"] = 399
    elif scenario == "missing_confidence_interval":
        payload["metrics"].pop("confidence_interval")
    elif scenario == "outcome_outside_confidence_interval":
        payload["metrics"]["confidence_interval"] = {"low": 0.0, "high": 1.0}
    elif scenario == "missing_significance":
        payload["metrics"].pop("statistically_significant")

    response = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload=payload,
        key=f"worker-invalid-effect-completion-{scenario}",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == error_code

    action_detail = client.get(
        f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers
    )
    experiment_detail = client.get(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}",
        headers=auth_headers,
    )
    assert action_detail.status_code == 200, action_detail.text
    assert experiment_detail.status_code == 200, experiment_detail.text
    assert action_detail.json()["data"]["status"] == "experiment_running"
    assert experiment_detail.json()["data"]["status"] == "running"
    assert experiment_detail.json()["data"].get("effect_id") is None
    with SessionLocal() as session:
        assert session.get(InsightEffect, f"insight_effect_{experiment['experiment_id']}") is None
        run = session.get(RunRecord, experiment["run_id"])
        assert run is not None
        assert run.status == "submitted"


def test_failed_report_invalidates_unfinished_action_experiment_and_run(client, auth_headers):
    report = _create_report(client, auth_headers, key="worker-upstream-failure-report")
    action = _create_normal_action(
        client,
        auth_headers,
        report,
        key="worker-upstream-failure-action",
    )
    experiment = _create_experiment(
        client,
        auth_headers,
        action,
        key="worker-upstream-failure-experiment",
    )
    _experiment_adapter, experiment_external_id = _dispatch_run(experiment["run_id"])
    report_adapter, report_external_id = _dispatch_run(report["run_id"])
    assert report_adapter == "object_storage"

    failed = _complete_run(
        client,
        auth_headers,
        run_id=report["run_id"],
        payload={
            "adapter": "object_storage",
            "status": "failed",
            "completion_receipt_id": "upstream-report-failed-after-action",
            "external_id": report_external_id,
            "result_ref": {"storage_object_id": report_external_id},
            "metrics": {},
            "note": "报告证据聚合失败",
            "error_code": "REPORT_EVIDENCE_AGGREGATION_FAILED",
            "retryable": False,
        },
        key="worker-upstream-failure-completion",
    )
    assert failed.status_code == 200, failed.text
    completion = failed.json()["data"]["insight_completion"]
    assert completion["status"] == "failed"
    assert completion["invalidated_actions"] == 1
    assert completion["invalidated_experiments"] == 1
    assert completion["cancelled_runs"] == 1

    action_detail = client.get(
        f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers
    )
    experiment_detail = client.get(
        f"/api/v1/insights/experiments/{experiment['experiment_id']}",
        headers=auth_headers,
    )
    assert action_detail.status_code == 200, action_detail.text
    assert experiment_detail.status_code == 200, experiment_detail.text
    assert action_detail.json()["data"]["status"] == "blocked_upstream_failed"
    assert action_detail.json()["data"]["blocked_reason"] == "upstream_report_failed"
    assert experiment_detail.json()["data"]["status"] == "invalidated"
    assert experiment_detail.json()["data"]["invalidated_reason"] == "upstream_report_failed"

    with SessionLocal() as session:
        run = session.get(RunRecord, experiment["run_id"])
        assert run is not None
        assert run.status == "cancelled"
        persisted_experiment = session.get(InsightExperiment, experiment["experiment_id"])
        assert persisted_experiment is not None
        assert persisted_experiment.outcome_metric_result_id is None
        assert session.get(InsightEffect, f"insight_effect_{experiment['experiment_id']}") is None

    blocked_receipt = _effect_receipt(
        action=action,
        experiment=experiment,
        baseline=_metric_detail(action["metric_result_id"]),
        external_id=experiment_external_id,
        receipt_id="effect-after-upstream-failure",
    )
    blocked = _complete_run(
        client,
        auth_headers,
        run_id=experiment["run_id"],
        payload=blocked_receipt,
        key="worker-effect-after-upstream-failure",
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "RUN_COMPLETION_NOT_ALLOWED"
