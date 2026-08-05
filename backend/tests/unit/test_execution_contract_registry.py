from __future__ import annotations

import pytest

from app.services.execution_contract_registry import (
    AUDIO_IMPORT_COMPLETION_SCHEMA,
    AUDIO_IMPORT_EXECUTION_CONTRACT,
    AUDIO_IMPORT_JOB_NAME,
    AUDIO_INTELLIGENCE_COMPLETION_SCHEMA,
    AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    AUDIO_INTELLIGENCE_JOB_NAME,
    ExecutionContractNotConfiguredError,
    execution_contract_registry,
)


def test_registry_exposes_only_the_two_materialized_production_contracts() -> None:
    audio_import = execution_contract_registry.require(
        event_type="task_run.requested",
        run_type="task_run",
        payload={
            "execution_mode": "production",
            "execution_contract": AUDIO_IMPORT_EXECUTION_CONTRACT,
        },
    )
    audio_intelligence = execution_contract_registry.require(
        event_type="audio_intelligence.requested",
        run_type="audio_intelligence",
        payload={
            "execution_mode": "production",
            "execution_contract": AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
        },
    )

    assert audio_import.dagster_job_name == AUDIO_IMPORT_JOB_NAME
    assert audio_import.input_schema == "auris-flow-execution-envelope-v1"
    assert audio_import.completion_schema == AUDIO_IMPORT_COMPLETION_SCHEMA
    assert audio_import.materializer_name == "audio_import"
    assert audio_intelligence.dagster_job_name == AUDIO_INTELLIGENCE_JOB_NAME
    assert audio_intelligence.input_schema == "auris-flow-execution-envelope-v1"
    assert audio_intelligence.completion_schema == AUDIO_INTELLIGENCE_COMPLETION_SCHEMA
    assert audio_intelligence.materializer_name == "audio_intelligence"
    assert {contract.contract_id for contract in execution_contract_registry.contracts} == {
        AUDIO_IMPORT_EXECUTION_CONTRACT,
        AUDIO_INTELLIGENCE_EXECUTION_CONTRACT,
    }


@pytest.mark.parametrize(
    ("event_type", "run_type", "payload"),
    [
        (
            "task_run.requested",
            "task_run",
            {"execution_mode": "production"},
        ),
        (
            "task_run.requested",
            "task_run",
            {
                "execution_mode": "production",
                "execution_contract": "caller-selected-contract",
            },
        ),
        (
            "insight_metric_aggregation.requested",
            "insight_metric_aggregation",
            {"execution_mode": "production"},
        ),
        (
            "provider_test.requested",
            "provider_test",
            {"execution_mode": "production"},
        ),
    ],
)
def test_registry_fails_closed_for_unconfigured_production_business_execution(
    event_type: str,
    run_type: str,
    payload: dict[str, str],
) -> None:
    with pytest.raises(ExecutionContractNotConfiguredError) as error:
        execution_contract_registry.require(
            event_type=event_type,
            run_type=run_type,
            payload=payload,
        )

    assert error.value.code == "EXECUTION_CONTRACT_NOT_CONFIGURED"
    assert error.value.event_type == event_type
    assert error.value.run_type == run_type


def test_registry_keeps_generic_job_available_only_for_diagnostic_execution() -> None:
    for event_type, run_type in (
        ("task_run.requested", "task_run"),
        ("provider_test.requested", "provider_test"),
    ):
        assert (
            execution_contract_registry.resolve(
                event_type=event_type,
                run_type=run_type,
                payload={"execution_mode": "diagnostic"},
            )
            is None
        )
