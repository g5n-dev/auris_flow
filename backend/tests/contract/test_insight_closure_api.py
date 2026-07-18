from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.api.routers import insights as insights_router
from app.core.database import SessionLocal
from app.models import InsightReport, MetricResult, OutboxEvent, RunRecord
from app.workers.outbox_worker import process_once

REPORT_METRICS = ["quote_consistency", "reception_conversion_quality"]
REPORT_EVIDENCE = ["AF-128"]
METRIC_OUTPUTS = {
    "quote_consistency": {"value": 74.2, "unit": "percent", "sample_size": 842},
    "reception_conversion_quality": {"value": 82.4, "unit": "score", "sample_size": 1204},
    "crosstalkRisk": {"value": 6.8, "unit": "percent", "sample_size": 7912},
}
REPORT_SCOPE = {
    "time_range": "2025-05-01/2025-05-31",
    "store_ids": ["polar-center"],
    "model_version": "v2.3.1",
    "label_version": "v1.8.4",
}


def _write_headers(auth_headers: dict[str, str], key: str) -> dict[str, str]:
    return {**auth_headers, "Idempotency-Key": key}


def _report_payload(
    *,
    metric_result_ids: list[str],
    title: str = "报价一致性经营报告",
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "report_type": "management_summary",
        **REPORT_SCOPE,
        "metric_result_ids": metric_result_ids,
        "evidence_refs": REPORT_EVIDENCE if evidence_refs is None else evidence_refs,
        "report_sections": ["north_star", "risk_root_cause", "next_actions"],
        "source": "insight_closure_contract",
    }


def _dispatch_metric_run(run_id: str) -> tuple[str, str]:
    for _attempt in range(30):
        with SessionLocal() as session:
            run = session.get(RunRecord, run_id)
            assert run is not None
            if run.status == "submitted":
                dispatch = run.payload["dispatch"]
                return str(dispatch["adapter"]), str(dispatch["details"]["external_run_id"])
        assert process_once() >= 1
    raise AssertionError(f"指标聚合运行未被调度：{run_id}")


def _dispatch_report_run(run_id: str) -> dict[str, Any]:
    for _attempt in range(30):
        with SessionLocal() as session:
            run = session.get(RunRecord, run_id)
            assert run is not None
            if run.status == "submitted":
                dispatch = run.payload["dispatch"]
                assert dispatch["adapter"] == "object_storage"
                assert dispatch["operation"] == "reserve_object"
                return deepcopy(dispatch["details"])
        assert process_once() >= 1
    raise AssertionError(f"洞察报告运行未被调度：{run_id}")


def _create_metric_results(
    client,
    auth_headers: dict[str, str],
    *,
    key: str,
    metric_keys: list[str] | None = None,
    scope: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    requested = metric_keys or REPORT_METRICS
    effective_scope = scope or REPORT_SCOPE
    created = client.post(
        "/api/v1/insights/metric-runs",
        json={
            "metric_keys": requested,
            **effective_scope,
            "source": "insight_closure_contract",
        },
        headers=_write_headers(auth_headers, f"{key}-metric-run"),
    )
    assert created.status_code == 202, created.text
    run = created.json()["data"]
    assert run["run_type"] == "insight_metric_aggregation"
    assert run["status"] == "pending"
    assert run["metric_keys"] == requested
    adapter, external_id = _dispatch_metric_run(run["run_id"])
    assert adapter == "dagster"
    completed = client.post(
        f"/api/v1/runs/{run['run_id']}/completion-receipts",
        json={
            "adapter": "dagster",
            "status": "success",
            "completion_receipt_id": f"{key}-metric-completion",
            "external_id": external_id,
            "result_ref": {
                "metric_results": [
                    {"metric_key": metric_key, **METRIC_OUTPUTS[metric_key]}
                    for metric_key in requested
                ]
            },
            "metrics": {"materialized_count": len(requested)},
        },
        headers=_write_headers(auth_headers, f"{key}-metric-receipt"),
    )
    assert completed.status_code == 200, completed.text
    completion = completed.json()["data"]["insight_completion"]
    assert completion["status"] == "materialized"
    assert completion["source_run_id"] == run["run_id"]
    assert len(completion["metric_result_ids"]) == len(requested)
    return completion["metric_result_ids"], run


def _create_report(
    client,
    auth_headers: dict[str, str],
    *,
    key: str,
    title: str = "报价一致性经营报告",
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    metric_result_ids, metric_run = _create_metric_results(
        client,
        auth_headers,
        key=key,
    )
    response = client.post(
        "/api/v1/insights/reports",
        json=_report_payload(
            metric_result_ids=metric_result_ids,
            title=title,
            evidence_refs=evidence_refs,
        ),
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["report_id"], data
    assert data["run_id"], data
    assert data["run_type"] == "insight_report", data
    assert data["status"] == "pending", data
    assert data["content_type"] == "application/json", data
    assert data["metric_result_ids"] == metric_result_ids, data
    assert data["metric_refs"] == REPORT_METRICS, data
    assert all(
        _metric_row(item)[2]["source_run_id"] == metric_run["run_id"] for item in metric_result_ids
    )
    assert len(data["metric_result_ids"]) == len(REPORT_METRICS), data
    assert len(set(data["metric_result_ids"])) == len(REPORT_METRICS), data
    return data


def _get_report(client, auth_headers: dict[str, str], report_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/insights/reports/{report_id}", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _metric_row(metric_result_id: str) -> tuple[str, str, dict[str, Any]]:
    with SessionLocal() as session:
        metric = session.get(MetricResult, metric_result_id)
        assert metric is not None, f"强表缺少指标快照：{metric_result_id}"
        return metric.tenant_id, metric.project_id, deepcopy(metric.payload)


def _mark_report_generated(report_id: str) -> None:
    with SessionLocal() as session:
        report = session.get(InsightReport, report_id)
        assert report is not None
        report.status = "generated"
        report.payload = {**report.payload, "status": "generated"}
        session.commit()


def _seed_foreign_project_report(*, project_id: str) -> dict[str, Any]:
    """Create the smallest valid foreign-scope report for authorization checks.

    Cross-project authorization must be tested independently from SceneProfile
    setup. A newly-created project intentionally has no inherited scene or
    automotive metric catalog, so asking it to run metrics would mask the
    authorization assertion behind the scene binding gate.
    """
    run_id = f"run_{project_id}_report"
    report_id = f"report_{project_id}"
    metric_result_ids = [f"metric_{project_id}_primary", f"metric_{project_id}_secondary"]
    with SessionLocal.begin() as session:
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id=project_id,
                run_type="insight_report",
                status="success",
                trace_id=f"trace_{project_id}",
                payload={"source": "cross-project-authorization-fixture"},
            )
        )
        session.flush()
        session.add(
            InsightReport(
                report_id=report_id,
                tenant_id="aurora_auto",
                project_id=project_id,
                run_id=run_id,
                status="generated",
                report_type="management_summary",
                trace_id=f"trace_{project_id}",
                payload={
                    "metric_result_ids": metric_result_ids,
                    "evidence_refs": REPORT_EVIDENCE,
                    "source": "cross-project-authorization-fixture",
                },
            )
        )
    return {
        "report_id": report_id,
        "metric_result_ids": metric_result_ids,
        "evidence_refs": REPORT_EVIDENCE,
    }


def _normal_action_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report["report_id"],
        "metric_result_id": report["metric_result_ids"][1],
        "action_type": "create_training_action",
        "risk_level": "medium",
        "owner": "业务运营",
        "hypothesis": "统一报价话术和单据校验可以提升报价一致率",
        "evidence_refs": REPORT_EVIDENCE,
        "source": "insight_closure_contract",
    }


def _high_risk_action_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_id": report["report_id"],
        "metric_result_id": report["metric_result_ids"][0],
        "action_type": "create_operation_action",
        "risk_level": "high",
        "owner": "价格策略组",
        "hypothesis": "收紧自动回写阈值可以减少金额冲突",
        "evidence_refs": REPORT_EVIDENCE,
        "source": "insight_closure_contract",
    }


def _create_action(
    client,
    auth_headers: dict[str, str],
    payload: dict[str, Any],
    *,
    key: str,
) -> dict[str, Any]:
    _mark_report_generated(payload["report_id"])
    response = client.post(
        "/api/v1/insights/actions",
        json=payload,
        headers=_write_headers(auth_headers, key),
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["action_id"], data
    assert data["report_id"] == payload["report_id"], data
    assert data["metric_result_id"] == payload["metric_result_id"], data
    return data


def _experiment_payload(metric_key: str) -> dict[str, Any]:
    return {
        "allocation_percent": 20,
        "min_sample_size": 400,
        "duration_days": 7,
        "primary_metric_key": metric_key,
        "hypothesis": "候选策略相较当前策略能够提升主指标且不突破风险护栏",
        "candidate": {"strategy": "统一话术与单据前置校验"},
        "control": {"strategy": "保持当前话术与事后抽检"},
        "guardrails": {"conflict_rate_max": 0.06},
    }


def test_metric_run_is_idempotent_and_enqueues_governed_dagster_outbox(client, auth_headers):
    payload = {
        "metric_keys": REPORT_METRICS,
        **REPORT_SCOPE,
        "source": "insight_closure_contract",
    }
    headers = _write_headers(auth_headers, "insight-metric-run-idempotent")
    first = client.post("/api/v1/insights/metric-runs", json=payload, headers=headers)
    replay = client.post("/api/v1/insights/metric-runs", json=payload, headers=headers)
    assert first.status_code == 202, first.text
    assert replay.status_code == 202, replay.text
    assert first.json()["data"]["run_id"] == replay.json()["data"]["run_id"]
    run_id = first.json()["data"]["run_id"]
    with SessionLocal() as session:
        run = session.get(RunRecord, run_id)
        assert run is not None
        assert run.run_type == "insight_metric_aggregation"
        assert run.payload["metric_scope"] == REPORT_SCOPE
        event = session.query(OutboxEvent).filter_by(aggregate_id=run_id).one()
        assert event.event_type == "insight_metric_aggregation.requested"
        assert event.aggregate_type == "insight_metric_aggregation"

    changed = client.post(
        "/api/v1/insights/metric-runs",
        json={**payload, "metric_keys": ["quote_consistency"]},
        headers=headers,
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    unknown = client.post(
        "/api/v1/insights/metric-runs",
        json={**payload, "metric_keys": ["unregistered_metric"]},
        headers=_write_headers(auth_headers, "insight-metric-run-unknown"),
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "INSIGHT_METRIC_UNKNOWN"


def test_report_creation_reuses_materialized_metrics_and_exposes_causal_detail(
    client, auth_headers
):
    report = _create_report(client, auth_headers, key="insight-report-create-detail")
    detail = _get_report(client, auth_headers, report["report_id"])

    assert detail["report_id"] == report["report_id"]
    assert detail["run_id"] == report["run_id"]
    assert detail["status"] == "generating"
    assert detail["metric_result_ids"] == report["metric_result_ids"]
    assert len(detail["metric_results"]) == len(REPORT_METRICS)
    assert {item["metric_result_id"] for item in detail["metric_results"]} == set(
        report["metric_result_ids"]
    )
    assert {item["metric_key"] for item in detail["metric_results"]} == set(REPORT_METRICS)
    assert all(item["immutable"] is True for item in detail["metric_results"])
    document = detail["report_document"]
    assert document["schema_version"] == "auris.insight-report.v2"
    assert document["artifact_state"] == "materialized"
    assert document["report_id"] == report["report_id"]
    assert document["run_id"] == report["run_id"]
    assert document["tenant_id"] == "aurora_auto"
    assert document["project_id"] == "sales_qa"
    assert [item["value"] for item in document["metric_results"]] == [74.2, 82.4]
    assert [item["sample_size"] for item in document["metric_results"]] == [842, 1204]
    assert document["evidence"][0]["evidence_ref"] == "AF-128"
    assert "金额冲突证据" in document["evidence"][0]["summary"]
    assert [item["section_id"] for item in document["sections"]] == [
        "north_star",
        "risk_root_cause",
        "next_actions",
    ]
    assert all(item["section_version"] == 1 for item in document["sections"])
    assert "74.2 percent" in document["sections"][0]["summary"]
    assert document["generator_proof"]["generation_mode"] == ("deterministic-governed-snapshot")
    with SessionLocal() as session:
        event = session.query(OutboxEvent).filter_by(aggregate_id=report["run_id"]).one()
        assert event.payload["report_document"] == document
    assert all(item["snapshot_role"] == "aggregation" for item in detail["metric_results"])
    assert all(item["status"] == "materialized" for item in detail["metric_results"])

    for metric_result_id in report["metric_result_ids"]:
        tenant_id, project_id, result = _metric_row(metric_result_id)
        assert tenant_id == "aurora_auto"
        assert project_id == "sales_qa"
        assert result["metric_result_id"] == metric_result_id
        assert result.get("source_report_id") is None
        assert result["source_run_id"]
        assert result["immutable"] is True
        assert result["value"] is not None
        assert result["sample_size"] >= 1
        assert result["scope"] == REPORT_SCOPE
        assert result["trace_id"]


def test_report_completion_api_requires_governed_reserved_artifact(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-report-artifact-contract")
    reservation = _dispatch_report_run(report["run_id"])

    invalid = client.post(
        f"/api/v1/runs/{report['run_id']}/completion-receipts",
        json={
            "adapter": "object_storage",
            "status": "success",
            "completion_receipt_id": "insight-report-artifact-invalid",
            "external_id": reservation["storage_object_id"],
            "result_ref": {"rendered": True},
        },
        headers=_write_headers(auth_headers, "insight-report-artifact-invalid-receipt"),
    )
    assert invalid.status_code == 422, invalid.text
    assert invalid.json()["error"]["code"] == "INSIGHT_REPORT_ARTIFACT_INVALID"
    assert invalid.json()["error"]["retryable"] is True
    assert _get_report(client, auth_headers, report["report_id"])["status"] == "generating"

    result_ref = {
        "storage_object_id": reservation["storage_object_id"],
        "object_uri": reservation["object_uri"],
        "content_sha256": reservation["content_sha256"],
        "content_type": "application/json",
    }
    completed = client.post(
        f"/api/v1/runs/{report['run_id']}/completion-receipts",
        json={
            "adapter": "object_storage",
            "status": "success",
            "completion_receipt_id": "insight-report-artifact-valid",
            "external_id": reservation["storage_object_id"],
            "result_ref": result_ref,
            "metrics": {"section_count": 3},
        },
        headers=_write_headers(auth_headers, "insight-report-artifact-valid-receipt"),
    )
    assert completed.status_code == 200, completed.text
    generated = _get_report(client, auth_headers, report["report_id"])
    assert generated["status"] == "generated"
    assert {key: generated["result_ref"][key] for key in result_ref} == result_ref
    assert generated["result_ref"]["artifact_state"] == "materialized"
    assert generated["result_ref"]["content_length"] == reservation["content_length"]


def test_metrics_endpoint_never_fabricates_unmaterialized_catalog_values(client, auth_headers):
    empty_scope = client.get(
        "/api/v1/insights/metrics?time_range=never-materialized",
        headers=auth_headers,
    )
    assert empty_scope.status_code == 200, empty_scope.text
    assert empty_scope.json()["data"]["items"] == []

    metric_result_ids, _run = _create_metric_results(
        client,
        auth_headers,
        key="insight-metrics-list-materialized",
    )
    query = client.get(
        "/api/v1/insights/metrics"
        "?time_range=2025-05-01%2F2025-05-31"
        "&store_id=polar-center&model_version=v2.3.1&label_version=v1.8.4",
        headers=auth_headers,
    )
    assert query.status_code == 200, query.text
    items = query.json()["data"]["items"]
    assert {item["metric_result_id"] for item in items} == set(metric_result_ids)
    assert all(item["status"] == "materialized" for item in items)
    assert all(item["source_run_id"] for item in items)
    assert all(item["sample_size"] >= 1 for item in items)


def test_metrics_endpoint_normalizes_and_forwards_every_label_scope_filter(
    client,
    auth_headers,
    monkeypatch,
):
    captured: dict[str, Any] = {}

    def capture_filters(_session, _ctx, **filters):
        captured.update(filters)
        return []

    monkeypatch.setattr(insights_router, "current_metric_payloads", capture_filters)
    response = client.get(
        "/api/v1/insights/metrics",
        params=[
            ("time_range", "30d"),
            ("label_version_applicability", "required"),
            ("taxonomy_mode", "normalized"),
            ("source_label_version_id", "source-v2"),
            ("source_label_version_id", "source-v1"),
            ("target_label_version_id", "target-v3"),
            ("mapping_bundle_id", "mapping-bundle-v3"),
            ("fact_set_generation", "42"),
            ("fact_as_of", "2026-07-18T18:00:00+08:00"),
        ],
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert captured["label_version_applicability"] == "required"
    assert captured["taxonomy_mode"] == "normalized"
    assert captured["source_label_version_ids"] == ["source-v1", "source-v2"]
    assert captured["target_label_version_id"] == "target-v3"
    assert captured["mapping_bundle_id"] == "mapping-bundle-v3"
    assert captured["fact_set_generation"] == 42
    assert captured["fact_as_of"] == datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    assert captured["fact_as_of"].utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "params",
    [
        {"label_version_applicability": "client-guessed"},
        {"taxonomy_mode": "latest"},
        {"source_label_version_id": " "},
        {"target_label_version_id": " "},
        {"mapping_bundle_id": " "},
        {"fact_set_generation": "0"},
        {"fact_as_of": "not-a-date"},
        {"fact_as_of": "2026-07-18T10:00:00"},
    ],
)
def test_metrics_endpoint_rejects_invalid_label_scope_filters(
    client,
    auth_headers,
    params,
):
    response = client.get(
        "/api/v1/insights/metrics",
        params=params,
        headers=auth_headers,
    )

    assert response.status_code == 422, response.text


def test_report_requires_materialized_metric_ids_and_matching_scope(client, auth_headers):
    missing_ids = client.post(
        "/api/v1/insights/reports",
        json={
            "title": "禁止按定义现场计算",
            "report_type": "daily",
            **REPORT_SCOPE,
            "metric_refs": REPORT_METRICS,
        },
        headers=_write_headers(auth_headers, "insight-report-reject-metric-refs"),
    )
    assert missing_ids.status_code == 422, missing_ids.text
    assert missing_ids.json()["error"]["code"] == "VALIDATION_ERROR"

    metric_result_ids, _run = _create_metric_results(
        client,
        auth_headers,
        key="insight-report-scope-mismatch-source",
    )
    mismatch = client.post(
        "/api/v1/insights/reports",
        json={
            **_report_payload(metric_result_ids=metric_result_ids),
            "store_ids": ["other-store"],
        },
        headers=_write_headers(auth_headers, "insight-report-scope-mismatch"),
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "INSIGHT_METRIC_SCOPE_MISMATCH"

    unknown = client.post(
        "/api/v1/insights/reports",
        json=_report_payload(metric_result_ids=["metric_result_missing"]),
        headers=_write_headers(auth_headers, "insight-report-missing-result"),
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "INSIGHT_METRIC_RESULT_NOT_FOUND"


def test_report_rejects_metric_snapshots_from_different_scene_profiles(client, auth_headers):
    metric_result_ids, _run = _create_metric_results(
        client,
        auth_headers,
        key="insight-report-scene-mismatch-source",
    )
    with SessionLocal.begin() as session:
        source = session.get(MetricResult, metric_result_ids[1])
        assert source is not None
        drifted_id = f"{source.metric_result_id}_scene_drift"
        session.add(
            MetricResult(
                metric_result_id=drifted_id,
                tenant_id=source.tenant_id,
                project_id=source.project_id,
                status=source.status,
                trace_id=source.trace_id,
                payload={
                    **source.payload,
                    "metric_result_id": drifted_id,
                    "id": drifted_id,
                    "scene_profile_id": "scene_profile_other",
                    "scene_profile_version_id": "scene_profile_other_v1",
                    "scene_profile_snapshot_sha256": "f" * 64,
                },
            )
        )
        metric_result_ids[1] = drifted_id

    rejected = client.post(
        "/api/v1/insights/reports",
        json=_report_payload(metric_result_ids=metric_result_ids),
        headers=_write_headers(auth_headers, "insight-report-scene-mismatch"),
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error"]["code"] == "INSIGHT_SCENE_PROFILE_MISMATCH"


def test_metric_snapshot_is_project_scoped_and_immutable(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-metric-scope-source")
    metric_result_id = report["metric_result_ids"][0]
    tenant_id, project_id, original = _metric_row(metric_result_id)
    assert tenant_id == "aurora_auto"
    assert project_id == "sales_qa"

    project_id = "insight_isolation_project"
    project = client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "name": "洞察隔离项目"},
        headers=_write_headers(auth_headers, "insight-isolation-project-create"),
    )
    assert project.status_code == 201, project.text
    other_project_headers = {
        **auth_headers,
        "X-Project-Id": project_id,
        "X-Request-Id": "insight-isolation-read",
    }

    hidden = client.get(
        f"/api/v1/insights/reports/{report['report_id']}", headers=other_project_headers
    )
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == "NOT_FOUND"

    mutation = client.patch(
        f"/api/v1/metric-results/{metric_result_id}",
        json={"value": 100.0},
        headers=_write_headers(auth_headers, "insight-metric-immutable-write"),
    )
    assert mutation.status_code == 404, mutation.text
    assert _metric_row(metric_result_id)[2] == original


def test_action_requires_report_and_metric_and_rejects_invalid_references(client, auth_headers):
    missing = client.post(
        "/api/v1/insights/actions",
        json={
            "action_type": "create_training_action",
            "risk_level": "medium",
            "owner": "业务运营",
        },
        headers=_write_headers(auth_headers, "insight-action-missing-cause"),
    )
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"

    invalid = client.post(
        "/api/v1/insights/actions",
        json={
            "report_id": "insight_report_missing",
            "metric_result_id": "metric_result_missing",
            "action_type": "create_training_action",
            "risk_level": "medium",
            "owner": "业务运营",
        },
        headers=_write_headers(auth_headers, "insight-action-invalid-cause"),
    )
    assert invalid.status_code == 404, invalid.text
    assert invalid.json()["error"]["code"] == "NOT_FOUND"

    first_report = _create_report(client, auth_headers, key="insight-action-report-first")
    second_report = _create_report(
        client,
        auth_headers,
        key="insight-action-report-second",
        title="成交推进经营报告",
    )
    _mark_report_generated(first_report["report_id"])
    unbound = _normal_action_payload(first_report)
    unbound["metric_result_id"] = second_report["metric_result_ids"][0]
    mismatch = client.post(
        "/api/v1/insights/actions",
        json=unbound,
        headers=_write_headers(auth_headers, "insight-action-unbound-metric"),
    )
    assert mismatch.status_code == 409, mismatch.text
    assert mismatch.json()["error"]["code"] == "INSIGHT_METRIC_NOT_IN_REPORT"


def test_action_requires_generated_report(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-action-generating-report")
    response = client.post(
        "/api/v1/insights/actions",
        json=_normal_action_payload(report),
        headers=_write_headers(auth_headers, "insight-action-generating-blocked"),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "INSIGHT_REPORT_NOT_GENERATED"
    assert response.json()["error"]["details"][0]["status"] == "generating"


def test_actions_branch_by_risk_and_create_human_review_when_required(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-action-branch-report")

    normal = _create_action(
        client,
        auth_headers,
        _normal_action_payload(report),
        key="insight-action-normal-branch",
    )
    assert normal["status"] == "experiment_ready"
    assert normal["branch"] == "experiment"
    assert normal.get("review_task_id") is None

    high_risk = _create_action(
        client,
        auth_headers,
        _high_risk_action_payload(report),
        key="insight-action-review-branch",
    )
    assert high_risk["status"] == "pending_review"
    assert high_risk["branch"] == "human_review"
    assert high_risk["review_task_id"]

    review = client.get(
        f"/api/v1/human-review-tasks/{high_risk['review_task_id']}", headers=auth_headers
    )
    assert review.status_code == 200, review.text
    review_data = review.json()["data"]
    assert review_data["status"] == "pending"
    assert {"type": "work_item", "id": high_risk["action_id"]} in review_data["target_refs"]


def test_client_branch_can_only_raise_server_governance(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-governance-floor-report")

    critical_payload = _normal_action_payload(report)
    critical_payload.update({"risk_level": "critical", "branch": "experiment"})
    critical = _create_action(
        client,
        auth_headers,
        critical_payload,
        key="insight-governance-critical-downgrade",
    )
    assert critical["branch"] == "human_review"
    assert critical["minimum_governance_branch"] == "human_review"
    assert critical["requested_branch"] == "experiment"
    assert critical["branch_escalated"] is True
    assert critical["status"] == "pending_review"

    quote_payload = _high_risk_action_payload(report)
    quote_payload.update({"risk_level": "low", "branch": "experiment"})
    quote = _create_action(
        client,
        auth_headers,
        quote_payload,
        key="insight-governance-quote-downgrade",
    )
    assert quote["branch"] == "human_review"
    assert quote["minimum_governance_branch"] == "human_review"
    assert quote["status"] == "pending_review"

    crosstalk_metric_ids, _run = _create_metric_results(
        client,
        auth_headers,
        key="insight-governance-crosstalk",
        metric_keys=["crosstalkRisk"],
    )
    crosstalk_response = client.post(
        "/api/v1/insights/reports",
        json=_report_payload(
            metric_result_ids=crosstalk_metric_ids,
            title="串音风险经营报告",
            evidence_refs=[],
        ),
        headers=_write_headers(auth_headers, "insight-governance-crosstalk-report"),
    )
    assert crosstalk_response.status_code == 202, crosstalk_response.text
    crosstalk_report = crosstalk_response.json()["data"]
    crosstalk_payload = {
        "report_id": crosstalk_report["report_id"],
        "metric_result_id": crosstalk_report["metric_result_ids"][0],
        "action_type": "create_operation_action",
        "risk_level": "low",
        "branch": "experiment",
        "owner": "音频质量",
        "hypothesis": "降低串音可以提升证据可信度",
        "evidence_refs": [],
        "source": "insight_closure_contract",
    }
    crosstalk = _create_action(
        client,
        auth_headers,
        crosstalk_payload,
        key="insight-governance-crosstalk-downgrade",
    )
    assert crosstalk["branch"] == "human_review"
    assert crosstalk["minimum_governance_branch"] == "human_review"
    assert crosstalk["status"] == "pending_review"

    raised_payload = _normal_action_payload(report)
    raised_payload["branch"] = "human_review"
    raised = _create_action(
        client,
        auth_headers,
        raised_payload,
        key="insight-governance-client-raise",
    )
    assert raised["minimum_governance_branch"] == "experiment"
    assert raised["branch"] == "human_review"
    assert raised["branch_escalated"] is False


def test_action_rejects_cross_project_reference_and_idempotency_payload_change(
    client, auth_headers
):
    report = _create_report(client, auth_headers, key="insight-action-idempotent-report")
    _mark_report_generated(report["report_id"])
    payload = _normal_action_payload(report)
    headers = _write_headers(auth_headers, "insight-action-idempotent")
    first = client.post("/api/v1/insights/actions", json=payload, headers=headers)
    replay = client.post("/api/v1/insights/actions", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    assert replay.status_code == 201, replay.text
    assert first.json()["data"]["action_id"] == replay.json()["data"]["action_id"]

    changed = client.post(
        "/api/v1/insights/actions",
        json={**payload, "hypothesis": "相同幂等键下被篡改的假设"},
        headers=headers,
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    project_id = "insight_cross_reference_project"
    project = client.post(
        "/api/v1/projects",
        json={"project_id": project_id, "name": "洞察跨项目引用测试"},
        headers=_write_headers(auth_headers, "insight-cross-project-create"),
    )
    assert project.status_code == 201, project.text
    other_report = _seed_foreign_project_report(project_id=project_id)
    cross_scope = client.post(
        "/api/v1/insights/actions",
        json=_normal_action_payload(other_report),
        headers=_write_headers(auth_headers, "insight-cross-project-action"),
    )
    assert cross_scope.status_code == 404, cross_scope.text
    assert cross_scope.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "missing_field",
    ["hypothesis", "candidate", "control", "guardrails", "min_sample_size"],
)
def test_experiment_rejects_incomplete_design(
    client,
    auth_headers,
    missing_field: str,
):
    report = _create_report(
        client,
        auth_headers,
        key=f"insight-incomplete-experiment-report-{missing_field}",
    )
    action = _create_action(
        client,
        auth_headers,
        _normal_action_payload(report),
        key=f"insight-incomplete-experiment-action-{missing_field}",
    )
    payload = _experiment_payload(action["metric_key"])
    payload.pop(missing_field)
    response = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json=payload,
        headers=_write_headers(
            auth_headers,
            f"insight-incomplete-experiment-create-{missing_field}",
        ),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("field", "empty_value"),
    [
        ("hypothesis", "   "),
        ("candidate", {}),
        ("control", {}),
        ("guardrails", {}),
    ],
)
def test_experiment_rejects_present_but_empty_design_fields(
    client,
    auth_headers,
    field: str,
    empty_value: Any,
):
    report = _create_report(
        client,
        auth_headers,
        key=f"insight-empty-experiment-report-{field}",
    )
    action = _create_action(
        client,
        auth_headers,
        _normal_action_payload(report),
        key=f"insight-empty-experiment-action-{field}",
    )
    payload = _experiment_payload(action["metric_key"])
    payload[field] = empty_value
    response = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json=payload,
        headers=_write_headers(auth_headers, f"insight-empty-experiment-create-{field}"),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_pending_review_action_rejects_experiment_until_approved(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-review-experiment-report")
    action = _create_action(
        client,
        auth_headers,
        _high_risk_action_payload(report),
        key="insight-review-experiment-action",
    )
    experiment_payload = _experiment_payload(action["metric_key"])

    blocked = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json=experiment_payload,
        headers=_write_headers(auth_headers, "insight-review-experiment-blocked"),
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "INSIGHT_ACTION_REVIEW_REQUIRED"

    decision = client.post(
        f"/api/v1/human-review-tasks/{action['review_task_id']}/decisions",
        json={"decision": "approved", "reason": "实验只做灰度，不直接回写生产"},
        headers=_write_headers(auth_headers, "insight-review-experiment-approved"),
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["data"]["decision"] == "accepted"

    action_detail = client.get(
        f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers
    )
    assert action_detail.status_code == 200, action_detail.text
    assert action_detail.json()["data"]["status"] == "experiment_ready"
    assert (
        action_detail.json()["data"]["review_decision_id"] == decision.json()["data"]["decision_id"]
    )

    created = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json=experiment_payload,
        headers=_write_headers(auth_headers, "insight-review-experiment-create"),
    )
    assert created.status_code == 202, created.text
    data = created.json()["data"]
    assert data["experiment_id"]
    assert data["eval_run_id"] == data["run_id"]
    assert data["run_type"] == "eval_run"
    assert data["status"] == "pending"
    assert data["action_id"] == action["action_id"]


def test_normal_action_can_create_traceable_eval_experiment(client, auth_headers):
    report = _create_report(client, auth_headers, key="insight-normal-experiment-report")
    action = _create_action(
        client,
        auth_headers,
        _normal_action_payload(report),
        key="insight-normal-experiment-action",
    )
    response = client.post(
        f"/api/v1/insights/actions/{action['action_id']}/experiments",
        json=_experiment_payload(action["metric_key"]),
        headers=_write_headers(auth_headers, "insight-normal-experiment-create"),
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    assert data["experiment_id"]
    assert data["eval_run_id"] == data["run_id"]
    assert data["run_type"] == "eval_run"
    assert data["status"] == "pending"
    assert data["action_id"] == action["action_id"]
    assert data["report_id"] == report["report_id"]
    assert data["baseline_metric_result_id"] == action["metric_result_id"]

    detail = client.get(f"/api/v1/insights/actions/{action['action_id']}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "experiment_running"
    assert any(
        item["experiment_id"] == data["experiment_id"]
        for item in detail.json()["data"]["experiments"]
    )
