from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.core.database import SessionLocal
from app.models import OutboxEvent, RunRecord

ROOT = Path(__file__).resolve().parents[3]


def _load_helper() -> ModuleType:
    path = ROOT / "scripts" / "process_e2e_outbox_run.py"
    spec = importlib.util.spec_from_file_location("auris_process_e2e_outbox_run", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _insert_dispatch(
    *,
    event_status: str = "processed",
    processed_event_id_offset: int = 0,
    dispatch: dict | None = None,
) -> tuple[str, int, dict]:
    run_id = "label_opt_e2e_dispatch_oracle"
    dispatch = dispatch or {
        "adapter": "dagster",
        "operation": "run_request",
        "status": "success",
        "details": {
            "external_run_id": "fake_dagster_run_e2e_oracle",
            "run_id": run_id,
        },
    }
    with SessionLocal.begin() as session:
        event = OutboxEvent(
            tenant_id="aurora_auto",
            project_id="sales_qa",
            event_type="agent_run.requested",
            aggregate_type="label_optimization",
            aggregate_id=run_id,
            status=event_status,
            payload={"adapter_dispatch": dispatch},
            dispatch_idempotency_key="outbox_v1_e2e_dispatch_oracle",
            attempt_count=1,
        )
        session.add(event)
        session.flush()
        event_id = event.event_id
        session.add(
            RunRecord(
                run_id=run_id,
                tenant_id="aurora_auto",
                project_id="sales_qa",
                run_type="label_optimization",
                status="submitted",
                trace_id="trace_e2e_dispatch_oracle",
                payload={
                    "business_status": "awaiting_completion",
                    "business_completion_required": True,
                    "processed_event_id": event_id + processed_event_id_offset,
                    "dispatch": dispatch,
                },
            )
        )
    return run_id, event_id, dispatch


def test_read_only_dispatch_evidence_is_scoped_and_does_not_process(monkeypatch) -> None:
    helper = _load_helper()
    run_id, event_id, dispatch = _insert_dispatch()
    monkeypatch.setattr(
        helper,
        "process_aggregate_events",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only evidence must not compete with the managed worker")
        ),
    )

    evidence = helper.read_run_dispatch_evidence(
        run_id,
        tenant_id="aurora_auto",
        project_id="sales_qa",
    )

    assert evidence == {
        "run_id": run_id,
        "run_type": "label_optimization",
        "run_status": "submitted",
        "business_status": "awaiting_completion",
        "business_completion_required": True,
        "event_id": event_id,
        "event_status": "processed",
        "adapter": "dagster",
        "external_id": "fake_dagster_run_e2e_oracle",
        "dispatch": dispatch,
    }


@pytest.mark.parametrize(
    ("tenant_id", "project_id"),
    [("other_tenant", "sales_qa"), ("aurora_auto", "other_project")],
)
def test_read_only_dispatch_evidence_rejects_scope_mismatch(
    tenant_id: str,
    project_id: str,
) -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch()

    with pytest.raises(SystemExit, match="scoped run not found"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )


def test_read_only_dispatch_evidence_rejects_unprocessed_event() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(event_status="pending")

    with pytest.raises(SystemExit, match="not processed"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


def test_read_only_dispatch_evidence_rejects_processed_event_drift() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(processed_event_id_offset=1000)

    with pytest.raises(SystemExit, match="processed outbox event not found"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


def test_read_only_dispatch_evidence_requires_adapter_external_identity() -> None:
    helper = _load_helper()
    run_id, _event_id, _dispatch = _insert_dispatch(
        dispatch={
            "adapter": "dagster",
            "operation": "run_request",
            "status": "success",
            "details": {},
        }
    )

    with pytest.raises(SystemExit, match="external identity"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )


@pytest.mark.parametrize("dispatch_status", [None, "failed"])
def test_read_only_dispatch_evidence_requires_explicit_success_status(
    dispatch_status: str | None,
) -> None:
    helper = _load_helper()
    dispatch = {
        "adapter": "dagster",
        "operation": "run_request",
        "details": {"external_run_id": "fake_dagster_run_e2e_oracle"},
    }
    if dispatch_status is not None:
        dispatch["status"] = dispatch_status
    run_id, _event_id, _dispatch = _insert_dispatch(dispatch=dispatch)

    with pytest.raises(SystemExit, match="external identity"):
        helper.read_run_dispatch_evidence(
            run_id,
            tenant_id="aurora_auto",
            project_id="sales_qa",
        )
