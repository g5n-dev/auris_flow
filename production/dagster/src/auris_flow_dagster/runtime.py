from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from auris_flow_dagster.callback import CompletionCallbackClient
from auris_flow_dagster.contracts import AurisRunContext

WorkflowResult = tuple[Mapping[str, Any], Mapping[str, Any]]
Workflow = Callable[[AurisRunContext, Mapping[str, Any]], WorkflowResult]


class AurisWorkflowError(RuntimeError):
    """Sanitized workflow failure suitable for Dagster event logs."""


def acknowledge_domain_workflow(
    scope: AurisRunContext,
    execution: Mapping[str, Any],
) -> WorkflowResult:
    """Validate orchestration without pretending to perform domain-specific inference.

    Product-specific jobs can replace this callable while preserving the same completion
    contract. The generic job deliberately emits only control-plane evidence.
    """

    requested_mode = execution.get("mode", "control-plane-acknowledgement")
    if requested_mode == "ci-cancel-delay":
        if os.getenv("APP_ENV") != "ci":
            raise ValueError("CI-only Auris Flow execution mode is disabled")
        raw_delay = execution.get("delay_seconds", 20)
        if (
            isinstance(raw_delay, bool)
            or not isinstance(raw_delay, int | float)
            or not 1 <= raw_delay <= 30
        ):
            raise ValueError("CI cancel delay must be between 1 and 30 seconds")
        time.sleep(float(raw_delay))
    elif requested_mode == "ci-intentional-failure":
        if os.getenv("APP_ENV") != "ci":
            raise ValueError("CI-only Auris Flow execution mode is disabled")
        raise ValueError("intentional CI workflow failure")
    elif requested_mode != "control-plane-acknowledgement":
        raise ValueError("unsupported Auris Flow execution mode")
    return (
        {
            "execution_contract": "auris-flow-generic-v1",
            "auris_run_id": scope.run_id,
            "trace_id": scope.trace_id,
        },
        {"control_plane_acknowledged": 1},
    )


def execute_and_report(
    *,
    scope: AurisRunContext,
    dagster_run_id: str,
    execution: Mapping[str, Any],
    callback: CompletionCallbackClient,
    workflow: Workflow = acknowledge_domain_workflow,
) -> Mapping[str, Any]:
    result_ref: Mapping[str, Any] = {}
    metrics: Mapping[str, Any] = {}
    workflow_failed = False
    try:
        result_ref, metrics = workflow(scope, execution)
    except Exception:
        workflow_failed = True

    if workflow_failed:
        callback_failed = False
        try:
            callback.post(
                scope,
                dagster_run_id=dagster_run_id,
                status="failed",
                error_code="DAGSTER_WORKFLOW_FAILED",
                retryable=False,
            )
        except Exception:
            callback_failed = True
        message = (
            "Auris Flow domain execution and completion callback failed"
            if callback_failed
            else "Auris Flow domain execution failed"
        )
        raise AurisWorkflowError(message) from None

    callback.post(
        scope,
        dagster_run_id=dagster_run_id,
        status="success",
        result_ref=result_ref,
        metrics=metrics,
    )
    return result_ref
