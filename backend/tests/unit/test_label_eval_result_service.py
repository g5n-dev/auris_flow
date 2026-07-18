from __future__ import annotations

import hashlib
import json

from app.core.context import RequestContext
from app.core.database import SessionLocal
from app.models import LabelEvalResult, LabelEvalSuiteResult, RunRecord
from app.services.label_eval_result_service import materialize_label_eval_completion

SUITES = ["golden", "boundary", "adversarial", "fresh", "canary", "regression"]
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        user_id="system-evaluator",
        roles=("system",),
        request_id="pytest-label-eval",
        trace_id="trace_label_eval_result",
        idempotency_key="label-eval-result",
    )


def _metric(*, macro_f1_gain_pp: float = 2.5) -> dict:
    return {
        "macro_f1": 0.91,
        "macro_f1_gain_pp": macro_f1_gain_pp,
        "critical_recall_delta_pp": 0.1,
        "json_valid_rate": 0.999,
        "coverage_rate": 0.98,
        "conflict_rate": 0.02,
        "cost_ratio": 1.05,
        "latency_ratio": 1.08,
        "quality_passed": True,
        "security_passed": True,
        "format_passed": True,
        "cost_passed": True,
        "latency_passed": True,
        "observability_passed": True,
    }


def _result(*, macro_f1_gain_pp: float = 2.5) -> dict:
    suite_rows = [
        {
            "suite": suite,
            "sample_count": 40,
            "sample_manifest_sha256": hashlib.sha256(suite.encode()).hexdigest(),
            "metrics": _metric(macro_f1_gain_pp=macro_f1_gain_pp),
        }
        for suite in SUITES
    ]
    manifest = [
        {
            "suite": item["suite"],
            "sample_count": item["sample_count"],
            "sample_manifest_sha256": item["sample_manifest_sha256"],
        }
        for item in sorted(suite_rows, key=lambda item: item["suite"])
    ]
    return {
        "binding_sha256": SHA_A,
        "dataset_manifest_sha256": SHA_B,
        "dataset_snapshot_sha256": SHA_C,
        "sample_manifest_sha256": _sha(manifest),
        "hidden_holdout_used": True,
        "dev_set_used": False,
        "suites": suite_rows,
        "overall": _metric(macro_f1_gain_pp=macro_f1_gain_pp),
        "paired_bootstrap": {
            "method": "paired-bootstrap-v1",
            "confidence_level": 0.95,
            "resample_count": 10_000,
            "random_seed": 20260715,
            "paired_sample_count": 240,
            "macro_f1_gain_lower_pp": 1.2,
            "macro_f1_gain_upper_pp": 3.6,
            "critical_recall_delta_lower_pp": -0.2,
            "critical_recall_delta_upper_pp": 0.4,
        },
    }


def _record(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
        run_type="eval_run",
        status="running",
        run_key=f"eval:{run_id}",
        partition_key="aurora_auto/sales_qa",
        trace_id="trace_label_eval_result",
        payload={
            "capability": "labeling",
            "binding_sha256": SHA_A,
            "locked_versions": {
                "eval_dataset_version_id": "evalset-labeling-v1",
                "eval_dataset_manifest_sha256": SHA_B,
                "eval_dataset_snapshot_sha256": SHA_C,
            },
        },
    )


def test_materializes_immutable_six_suite_passed_result(monkeypatch):
    monkeypatch.setattr(
        "app.services.label_eval_result_service.revalidate_labeling_eval_manifest",
        lambda *_args, **_kwargs: _args[-1],
    )
    with SessionLocal() as session:
        record = _record("eval_label_result_pass")
        session.add(record)
        session.flush()
        data = materialize_label_eval_completion(
            session,
            _ctx(),
            record,
            {"result_ref": {"labeling_eval_result": _result()}},
        )
        session.commit()

        assert data is not None and data["status"] == "passed"
        assert session.query(LabelEvalResult).count() == 1
        suite_rows = session.query(LabelEvalSuiteResult).all()
        assert len(suite_rows) == 6
        assert all(len(item.sample_manifest_sha256) == 64 for item in suite_rows)
        assert all(item["passed"] for item in data["gate_results"])


def test_valid_result_with_insufficient_gain_is_materialized_as_blocked(monkeypatch):
    monkeypatch.setattr(
        "app.services.label_eval_result_service.revalidate_labeling_eval_manifest",
        lambda *_args, **_kwargs: _args[-1],
    )
    with SessionLocal() as session:
        record = _record("eval_label_result_blocked")
        session.add(record)
        session.flush()
        data = materialize_label_eval_completion(
            session,
            _ctx(),
            record,
            {"result_ref": {"labeling_eval_result": _result(macro_f1_gain_pp=1.0)}},
        )

        assert data is not None and data["status"] == "blocked"
        failed = {item["code"] for item in data["gate_results"] if not item["passed"]}
        assert failed == {"MACRO_F1_GAIN_MIN"}
