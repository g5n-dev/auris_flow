from __future__ import annotations

import pytest
from dagster import DagsterInvalidConfigError, validate_run_config

from auris_flow_dagster.contracts import AurisContractError
from auris_flow_dagster.definitions import (
    auris_flow_generic_job,
    defs,
    map_auris_run_config,
)


def test_real_client_top_level_run_config_maps_to_domain_op(
    valid_context: dict[str, object],
) -> None:
    incoming = {
        "auris_context": valid_context,
        "execution": {"mode": "control-plane-acknowledgement"},
    }
    mapped = map_auris_run_config(incoming)
    op_config = mapped["ops"]["execute_auris_flow_domain_work"]["config"]
    assert op_config["auris_context"] == valid_context
    assert op_config["execution"] == incoming["execution"]
    assert validate_run_config(auris_flow_generic_job, run_config=incoming)["ops"]


def test_dagster_config_mapping_fails_closed_before_domain_work(
    valid_context: dict[str, object],
) -> None:
    invalid = {"auris_context": {**valid_context, "tenant_id": None}}
    with pytest.raises(AurisContractError, match="tenant_id"):
        map_auris_run_config(invalid)
    with pytest.raises(DagsterInvalidConfigError):
        validate_run_config(auris_flow_generic_job, run_config={})


def test_definitions_expose_only_domain_named_generic_job() -> None:
    repository = defs.get_repository_def()
    assert repository.name == "__repository__"
    assert repository.has_job("auris_flow_generic_job")
    assert "dagster" not in auris_flow_generic_job.description.lower()
