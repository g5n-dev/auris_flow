from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Base,
    InsightAction,
    InsightEffect,
    InsightExperiment,
    InsightReport,
    MetricResult,
    RunRecord,
)

TENANT_ID = "causal_tenant"
PROJECT_ID = "causal_project"
OTHER_PROJECT_ID = "causal_other_project"


@pytest.fixture
def causal_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_engine(f"sqlite:///{tmp_path / 'insight_causal.sqlite'}", future=True)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seed_valid_causal_graph(engine: Engine) -> None:
    with Session(engine) as session:
        session.add_all(
            [
                RunRecord(
                    run_id="run_report",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    run_type="insight_report",
                    status="success",
                    trace_id="trace_run_report",
                    payload={},
                ),
                RunRecord(
                    run_id="run_eval",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    run_type="evaluation",
                    status="success",
                    trace_id="trace_run_eval",
                    payload={},
                ),
                MetricResult(
                    metric_result_id="metric_baseline",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="snapshot",
                    trace_id="trace_metric_baseline",
                    payload={},
                ),
                MetricResult(
                    metric_result_id="metric_outcome",
                    tenant_id=TENANT_ID,
                    project_id=PROJECT_ID,
                    status="snapshot",
                    trace_id="trace_metric_outcome",
                    payload={},
                ),
            ]
        )
        session.flush()
        session.add(
            InsightReport(
                report_id="report_valid",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                run_id="run_report",
                status="generated",
                report_type="operations",
                trace_id="trace_report",
                payload={},
            )
        )
        session.flush()
        session.add(
            InsightAction(
                action_id="action_valid",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                report_id="report_valid",
                baseline_metric_result_id="metric_baseline",
                action_type="experiment",
                branch="experiment",
                risk_level="low",
                status="measured",
                resource_version=1,
                trace_id="trace_action",
                payload={},
            )
        )
        session.flush()
        session.add(
            InsightExperiment(
                experiment_id="experiment_valid",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                action_id="action_valid",
                eval_run_id="run_eval",
                baseline_metric_result_id="metric_baseline",
                outcome_metric_result_id="metric_outcome",
                status="measured",
                trace_id="trace_experiment",
                payload={},
            )
        )
        session.flush()
        session.add(
            InsightEffect(
                effect_id="effect_valid",
                tenant_id=TENANT_ID,
                project_id=PROJECT_ID,
                action_id="action_valid",
                experiment_id="experiment_valid",
                baseline_metric_result_id="metric_baseline",
                outcome_metric_result_id="metric_outcome",
                metric_key="conversion_rate",
                delta=0.05,
                status="measured",
                trace_id="trace_effect",
                payload={},
            )
        )
        session.commit()


def _assert_insert_rejected(engine: Engine, instance: object) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        session.add(instance)
        session.commit()


def _assert_delete_rejected(engine: Engine, model: type, business_id: str) -> None:
    with Session(engine) as session, pytest.raises(IntegrityError):
        instance = session.get(model, business_id)
        assert instance is not None
        session.delete(instance)
        session.commit()


def test_cross_project_causal_references_are_rejected(causal_engine: Engine) -> None:
    _seed_valid_causal_graph(causal_engine)

    _assert_insert_rejected(
        causal_engine,
        InsightReport(
            report_id="report_cross_project",
            tenant_id=TENANT_ID,
            project_id=OTHER_PROJECT_ID,
            run_id="run_report",
            status="generated",
            report_type="operations",
            trace_id="trace_report_cross_project",
            payload={},
        ),
    )
    _assert_insert_rejected(
        causal_engine,
        InsightAction(
            action_id="action_cross_project",
            tenant_id=TENANT_ID,
            project_id=OTHER_PROJECT_ID,
            report_id="report_valid",
            baseline_metric_result_id="metric_baseline",
            action_type="experiment",
            branch="experiment",
            risk_level="low",
            status="experiment_ready",
            resource_version=1,
            trace_id="trace_action_cross_project",
            payload={},
        ),
    )
    _assert_insert_rejected(
        causal_engine,
        InsightExperiment(
            experiment_id="experiment_cross_project",
            tenant_id=TENANT_ID,
            project_id=OTHER_PROJECT_ID,
            action_id="action_valid",
            eval_run_id="run_eval",
            baseline_metric_result_id="metric_baseline",
            outcome_metric_result_id="metric_outcome",
            status="running",
            trace_id="trace_experiment_cross_project",
            payload={},
        ),
    )
    _assert_insert_rejected(
        causal_engine,
        InsightEffect(
            effect_id="effect_cross_project",
            tenant_id=TENANT_ID,
            project_id=OTHER_PROJECT_ID,
            action_id="action_valid",
            experiment_id="experiment_valid",
            baseline_metric_result_id="metric_baseline",
            outcome_metric_result_id="metric_outcome",
            metric_key="conversion_rate",
            delta=0.01,
            status="measured",
            trace_id="trace_effect_cross_project",
            payload={},
        ),
    )


@pytest.mark.parametrize(
    ("model", "business_id"),
    [
        (RunRecord, "run_report"),
        (RunRecord, "run_eval"),
        (MetricResult, "metric_baseline"),
        (MetricResult, "metric_outcome"),
        (InsightReport, "report_valid"),
        (InsightAction, "action_valid"),
        (InsightExperiment, "experiment_valid"),
    ],
)
def test_referenced_causal_parents_cannot_be_deleted(
    causal_engine: Engine,
    model: type,
    business_id: str,
) -> None:
    _seed_valid_causal_graph(causal_engine)
    _assert_delete_rejected(causal_engine, model, business_id)
