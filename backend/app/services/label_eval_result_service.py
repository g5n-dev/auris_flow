from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import RequestContext
from app.core.errors import ApiError
from app.core.request_identifiers import public_id_from_hex
from app.models import LabelEvalResult, LabelEvalSuiteResult, RunRecord
from app.schemas.evaluation import LabelingEvalCompletionResult
from app.services.audit_service import record_audit
from app.services.eval_binding_service import revalidate_labeling_eval_manifest
from app.services.outbox_service import enqueue_event


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gate(code: str, passed: bool, message: str, **details: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "passed": passed, "message": message}
    if details:
        result["details"] = details
    return result


def _gate_results(result: LabelingEvalCompletionResult) -> list[dict[str, Any]]:
    metrics = result.overall
    bootstrap = result.paired_bootstrap
    gates = [
        _gate(
            "MACRO_F1_MIN",
            metrics.macro_f1 >= 0.88,
            "总体 macro-F1 必须达到 0.88",
            actual=metrics.macro_f1,
            threshold=0.88,
        ),
        _gate(
            "MACRO_F1_GAIN_MIN",
            metrics.macro_f1_gain_pp >= 2.0,
            "macro-F1 提升必须至少 2pp",
            actual=metrics.macro_f1_gain_pp,
            threshold=2.0,
        ),
        _gate(
            "MACRO_F1_BOOTSTRAP_SIGNIFICANT",
            bootstrap.macro_f1_gain_lower_pp > 0,
            "paired bootstrap 的 macro-F1 提升 95% CI 下界必须大于 0",
            lower=bootstrap.macro_f1_gain_lower_pp,
        ),
        _gate(
            "CRITICAL_RECALL_NONINFERIOR",
            bootstrap.critical_recall_delta_lower_pp >= -0.5,
            "关键标签 recall 的 95% CI 下界不得低于 -0.5pp",
            lower=bootstrap.critical_recall_delta_lower_pp,
            threshold=-0.5,
        ),
        _gate(
            "JSON_VALID_RATE_MIN",
            metrics.json_valid_rate >= 0.995,
            "JSON 合法率必须达到 99.5%",
            actual=metrics.json_valid_rate,
            threshold=0.995,
        ),
        _gate(
            "COVERAGE_MIN",
            metrics.coverage_rate >= 0.95,
            "有效覆盖率必须达到 95%",
            actual=metrics.coverage_rate,
            threshold=0.95,
        ),
        _gate(
            "CONFLICT_RATE_MAX",
            metrics.conflict_rate < 0.05,
            "冲突率必须低于 5%",
            actual=metrics.conflict_rate,
            threshold=0.05,
        ),
        _gate(
            "COST_RATIO_MAX",
            metrics.cost_ratio <= 1.10,
            "成本不得超过基线 110%",
            actual=metrics.cost_ratio,
            threshold=1.10,
        ),
        _gate(
            "LATENCY_RATIO_MAX",
            metrics.latency_ratio <= 1.20,
            "延迟不得超过基线 120%",
            actual=metrics.latency_ratio,
            threshold=1.20,
        ),
    ]
    flag_fields = {
        "QUALITY_GATE": metrics.quality_passed,
        "SECURITY_GATE": metrics.security_passed,
        "FORMAT_GATE": metrics.format_passed,
        "COST_GATE": metrics.cost_passed,
        "LATENCY_GATE": metrics.latency_passed,
        "OBSERVABILITY_GATE": metrics.observability_passed,
    }
    gates.extend(
        _gate(code, passed, f"{code} 必须由评测器明确通过") for code, passed in flag_fields.items()
    )
    for suite in result.suites:
        prefix = f"SUITE_{suite.suite.upper()}"
        suite_metrics = suite.metrics
        gates.extend(
            (
                _gate(
                    f"{prefix}_QUALITY",
                    suite_metrics.quality_passed
                    and suite_metrics.coverage_rate >= 0.95
                    and suite_metrics.conflict_rate < 0.05,
                    f"{suite.suite} 套件质量门禁必须通过",
                ),
                _gate(
                    f"{prefix}_SECURITY",
                    suite_metrics.security_passed,
                    f"{suite.suite} 套件安全门禁必须通过",
                ),
                _gate(
                    f"{prefix}_FORMAT",
                    suite_metrics.format_passed and suite_metrics.json_valid_rate >= 0.995,
                    f"{suite.suite} 套件格式门禁必须通过且 JSON 合法率达到 99.5%",
                ),
                _gate(
                    f"{prefix}_COST",
                    suite_metrics.cost_passed and suite_metrics.cost_ratio <= 1.10,
                    f"{suite.suite} 套件成本门禁必须通过且不超过基线 110%",
                ),
                _gate(
                    f"{prefix}_LATENCY",
                    suite_metrics.latency_passed and suite_metrics.latency_ratio <= 1.20,
                    f"{suite.suite} 套件延迟门禁必须通过且不超过基线 120%",
                ),
                _gate(
                    f"{prefix}_OBSERVABILITY",
                    suite_metrics.observability_passed,
                    f"{suite.suite} 套件可观测门禁必须通过",
                ),
            )
        )
    return gates


def _suite_manifest_document(result: LabelingEvalCompletionResult) -> list[dict[str, Any]]:
    return [
        {
            "suite": suite.suite,
            "sample_count": suite.sample_count,
            "sample_manifest_sha256": suite.sample_manifest_sha256,
        }
        for suite in sorted(result.suites, key=lambda item: item.suite)
    ]


def label_eval_result_integrity_blockers(
    session: Session,
    result: LabelEvalResult,
) -> list[dict[str, Any]]:
    """Verify that a stored ``passed`` row still has all immutable release evidence."""

    blockers: list[dict[str, Any]] = []
    raw = (result.payload or {}).get("result_document")
    if not isinstance(raw, dict):
        return [
            _gate(
                "LABEL_EVAL_RESULT_DOCUMENT_MISSING",
                False,
                "评测强结果缺少原始签名文档，不能作为发布事实",
            )
        ]
    try:
        parsed = LabelingEvalCompletionResult.model_validate(raw)
    except ValidationError as exc:
        return [
            _gate(
                "LABEL_EVAL_RESULT_DOCUMENT_INVALID",
                False,
                "评测强结果原始文档不再满足强 Schema",
                errors=exc.errors(include_url=False),
            )
        ]

    expected_result_sha256 = _canonical_sha256(parsed.model_dump(mode="json"))
    if result.result_sha256 != expected_result_sha256:
        blockers.append(
            _gate(
                "LABEL_EVAL_RESULT_HASH_MISMATCH",
                False,
                "评测强结果内容哈希不一致",
                expected=expected_result_sha256,
                actual=result.result_sha256,
            )
        )
    expected_sample_manifest_sha256 = _canonical_sha256(_suite_manifest_document(parsed))
    if (
        parsed.sample_manifest_sha256 != expected_sample_manifest_sha256
        or result.sample_manifest_sha256 != expected_sample_manifest_sha256
    ):
        blockers.append(
            _gate(
                "LABEL_EVAL_SAMPLE_MANIFEST_MISMATCH",
                False,
                "六套件样本清单哈希不一致",
            )
        )
    if result.binding_sha256 != parsed.binding_sha256:
        blockers.append(
            _gate(
                "LABEL_EVAL_BINDING_HASH_MISMATCH",
                False,
                "评测结果 binding 哈希与原始文档不一致",
            )
        )
    if result.dataset_snapshot_sha256 != parsed.dataset_snapshot_sha256:
        blockers.append(
            _gate(
                "LABEL_EVAL_DATASET_SNAPSHOT_MISMATCH",
                False,
                "评测结果数据集快照与原始文档不一致",
            )
        )
    if result.overall_metrics != parsed.overall.model_dump(mode="json"):
        blockers.append(
            _gate(
                "LABEL_EVAL_OVERALL_METRICS_MISMATCH",
                False,
                "评测总体指标与原始文档不一致",
            )
        )
    if result.bootstrap_ci != parsed.paired_bootstrap.model_dump(mode="json"):
        blockers.append(
            _gate(
                "LABEL_EVAL_BOOTSTRAP_MISMATCH",
                False,
                "paired bootstrap 结果与原始文档不一致",
            )
        )

    expected_gates = _gate_results(parsed)
    if _canonical_sha256(result.gate_results) != _canonical_sha256(expected_gates):
        blockers.append(
            _gate(
                "LABEL_EVAL_GATE_EVIDENCE_MISMATCH",
                False,
                "评测门禁事实与原始文档重算结果不一致",
            )
        )
    expected_status = "passed" if all(gate["passed"] for gate in expected_gates) else "blocked"
    if result.status != expected_status:
        blockers.append(
            _gate(
                "LABEL_EVAL_STATUS_MISMATCH",
                False,
                "评测状态与重算门禁不一致",
                expected=expected_status,
                actual=result.status,
            )
        )

    suite_rows = list(
        session.scalars(
            select(LabelEvalSuiteResult).where(
                LabelEvalSuiteResult.eval_result_id == result.eval_result_id,
                LabelEvalSuiteResult.tenant_id == result.tenant_id,
                LabelEvalSuiteResult.project_id == result.project_id,
            )
        )
    )
    rows_by_suite = {row.suite: row for row in suite_rows}
    expected_suite_names = {suite.suite for suite in parsed.suites}
    if len(suite_rows) != 6 or set(rows_by_suite) != expected_suite_names:
        blockers.append(
            _gate(
                "LABEL_EVAL_SIX_SUITE_EVIDENCE_INCOMPLETE",
                False,
                "评测结果必须保留六个套件且每个套件恰好一条强证据",
            )
        )
    else:
        for suite in parsed.suites:
            row = rows_by_suite[suite.suite]
            suite_document = suite.model_dump(mode="json")
            if (
                row.sample_count != suite.sample_count
                or row.sample_manifest_sha256 != suite.sample_manifest_sha256
                or row.metrics != suite.metrics.model_dump(mode="json")
                or row.suite_sha256 != _canonical_sha256(suite_document)
            ):
                blockers.append(
                    _gate(
                        "LABEL_EVAL_SUITE_EVIDENCE_MISMATCH",
                        False,
                        "评测套件强证据与原始文档不一致",
                        suite=suite.suite,
                    )
                )
    return blockers


def label_eval_result_data(result: LabelEvalResult) -> dict[str, Any]:
    return {
        "eval_result_id": result.eval_result_id,
        "eval_run_id": result.eval_run_id,
        "status": result.status,
        "binding_sha256": result.binding_sha256,
        "dataset_snapshot_sha256": result.dataset_snapshot_sha256,
        "sample_manifest_sha256": result.sample_manifest_sha256,
        "result_sha256": result.result_sha256,
        "overall_metrics": result.overall_metrics,
        "bootstrap_ci": result.bootstrap_ci,
        "gate_results": result.gate_results,
        "trace_id": result.trace_id,
    }


def materialize_label_eval_completion(
    session: Session,
    ctx: RequestContext,
    record: RunRecord,
    completion_receipt: dict[str, Any],
) -> dict[str, Any] | None:
    if record.run_type != "eval_run" or record.payload.get("capability") != "labeling":
        return None
    revalidate_labeling_eval_manifest(session, ctx, record.payload or {})
    raw = (completion_receipt.get("result_ref") or {}).get("labeling_eval_result")
    if not isinstance(raw, dict):
        raise ApiError(
            "LABEL_EVAL_RESULT_REQUIRED",
            "标签评测成功回执必须携带强类型 labeling_eval_result",
            422,
        )
    try:
        result = LabelingEvalCompletionResult.model_validate(raw)
    except ValidationError as exc:
        raise ApiError(
            "LABEL_EVAL_RESULT_INVALID",
            "标签评测结果 Schema 不合法",
            422,
            details=[{"errors": exc.errors(include_url=False)}],
        ) from exc

    binding_sha256 = str(record.payload.get("binding_sha256") or "")
    locked_versions = record.payload.get("locked_versions") or {}
    expected_snapshot_sha256 = str(locked_versions.get("eval_dataset_snapshot_sha256") or "")
    mismatches: list[dict[str, Any]] = []
    expected_values = {
        "binding_sha256": (binding_sha256, result.binding_sha256),
        "dataset_manifest_sha256": (
            str(locked_versions.get("eval_dataset_manifest_sha256") or ""),
            result.dataset_manifest_sha256,
        ),
        "dataset_snapshot_sha256": (
            expected_snapshot_sha256,
            result.dataset_snapshot_sha256,
        ),
    }
    for field, (expected, actual) in expected_values.items():
        if not expected or expected != actual:
            mismatches.append({"field": field, "expected": expected, "actual": actual})
    calculated_sample_manifest_sha256 = _canonical_sha256(_suite_manifest_document(result))
    if calculated_sample_manifest_sha256 != result.sample_manifest_sha256:
        mismatches.append(
            {
                "field": "sample_manifest_sha256",
                "expected": calculated_sample_manifest_sha256,
                "actual": result.sample_manifest_sha256,
            }
        )
    if mismatches:
        raise ApiError(
            "LABEL_EVAL_RESULT_BINDING_MISMATCH",
            "标签评测结果与锁定 Bundle 或样本清单不一致",
            409,
            details=mismatches,
        )

    existing = session.scalar(
        select(LabelEvalResult).where(
            LabelEvalResult.eval_run_id == record.run_id,
            LabelEvalResult.tenant_id == ctx.tenant_id,
            LabelEvalResult.project_id == ctx.project_id,
        )
    )
    raw_document = result.model_dump(mode="json")
    result_sha256 = _canonical_sha256(raw_document)
    if existing is not None:
        if existing.result_sha256 != result_sha256:
            raise ApiError(
                "LABEL_EVAL_RESULT_CONFLICT",
                "同一 EvalRun 不能物化不同评测结果",
                409,
            )
        return label_eval_result_data(existing)

    gates = _gate_results(result)
    status = "passed" if all(bool(gate["passed"]) for gate in gates) else "blocked"
    eval_result_id = public_id_from_hex("ler", result_sha256, suffix_length=24)
    row = LabelEvalResult(
        eval_result_id=eval_result_id,
        tenant_id=ctx.tenant_id,
        project_id=ctx.project_id,
        eval_run_id=record.run_id,
        status=status,
        binding_sha256=result.binding_sha256,
        dataset_snapshot_sha256=result.dataset_snapshot_sha256,
        sample_manifest_sha256=result.sample_manifest_sha256,
        result_sha256=result_sha256,
        overall_metrics=result.overall.model_dump(mode="json"),
        bootstrap_ci=result.paired_bootstrap.model_dump(mode="json"),
        gate_results=gates,
        trace_id=ctx.trace_id,
        payload={
            "hidden_holdout_used": result.hidden_holdout_used,
            "dev_set_used": result.dev_set_used,
            "suite_count": len(result.suites),
            "paired_bootstrap_method": result.paired_bootstrap.method,
            "paired_sample_count": result.paired_bootstrap.paired_sample_count,
            "result_document": raw_document,
        },
    )
    session.add(row)
    for suite in result.suites:
        suite_document = suite.model_dump(mode="json")
        suite_sha256 = _canonical_sha256(suite_document)
        session.add(
            LabelEvalSuiteResult(
                suite_result_id=public_id_from_hex(
                    "lesr",
                    suite_sha256,
                    suffix_length=24,
                ),
                tenant_id=ctx.tenant_id,
                project_id=ctx.project_id,
                eval_result_id=eval_result_id,
                suite=suite.suite,
                sample_count=suite.sample_count,
                sample_manifest_sha256=suite.sample_manifest_sha256,
                metrics=suite.metrics.model_dump(mode="json"),
                suite_sha256=suite_sha256,
                trace_id=ctx.trace_id,
            )
        )
    session.flush()
    data = label_eval_result_data(row)
    record_audit(
        session,
        ctx,
        action="label_eval_result.materialized",
        object_type="label_eval_result",
        object_id=eval_result_id,
        after=data,
    )
    enqueue_event(
        session,
        ctx,
        event_type="label_eval_result.materialized",
        aggregate_type="label_eval_result",
        aggregate_id=eval_result_id,
        payload=data,
    )
    return data
