from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from auris_flow_dagster.contracts import AurisRunContext
from auris_flow_dagster.runtime import AurisWorkflowError, execute_and_report


class RecordingCallback:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(self, scope: AurisRunContext, **values: Any) -> dict[str, Any]:
        self.calls.append({"scope": scope, **values})
        return {"data": {"status": values["status"]}}


def test_successful_workflow_posts_success_receipt(scope: AurisRunContext) -> None:
    callback = RecordingCallback()

    def workflow(
        received_scope: AurisRunContext,
        execution: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        assert received_scope == scope
        assert execution["mode"] == "control-plane-acknowledgement"
        return {"object_id": "result-001"}, {"processed": 3}

    result = execute_and_report(
        scope=scope,
        dagster_run_id="dg-success",
        execution={"mode": "control-plane-acknowledgement"},
        callback=callback,  # type: ignore[arg-type]
        workflow=workflow,
    )

    assert result == {"object_id": "result-001"}
    assert callback.calls == [
        {
            "scope": scope,
            "dagster_run_id": "dg-success",
            "status": "success",
            "result_ref": {"object_id": "result-001"},
            "metrics": {"processed": 3},
        }
    ]


def test_failed_workflow_posts_sanitized_failure_then_reraises(scope: AurisRunContext) -> None:
    callback = RecordingCallback()
    canary = "never-return-this-sensitive-runtime-value"

    def workflow(
        _scope: AurisRunContext,
        _execution: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        raise RuntimeError(canary)

    with pytest.raises(AurisWorkflowError) as failure:
        execute_and_report(
            scope=scope,
            dagster_run_id="dg-failed",
            execution={},
            callback=callback,  # type: ignore[arg-type]
            workflow=workflow,
        )

    assert canary not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert callback.calls[0]["status"] == "failed"
    assert callback.calls[0]["error_code"] == "DAGSTER_WORKFLOW_FAILED"
    assert canary not in repr(callback.calls)
