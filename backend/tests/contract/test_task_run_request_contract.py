from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models import OutboxEvent, RunRecord
from app.schemas.requests import TaskRunRequest

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "doc/backend-spec/openapi-v0.1.yaml"


def _openapi_task_run_schema() -> dict[str, Any]:
    document = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    return document["components"]["schemas"]["CreateTaskRunRequest"]


def _allows_null(schema: dict[str, Any]) -> bool:
    return any(
        isinstance(branch, dict) and branch.get("type") == "null"
        for branch in schema.get("anyOf", [])
    )


def _string_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    return next(
        branch
        for branch in schema.get("anyOf", [])
        if isinstance(branch, dict) and branch.get("type") == "string"
    )


def test_task_run_model_and_documented_request_schema_have_identical_public_shape() -> None:
    runtime = TaskRunRequest.model_json_schema()
    documented = _openapi_task_run_schema()

    assert runtime["additionalProperties"] is False
    assert documented["additionalProperties"] is False
    assert set(runtime["properties"]) == set(documented["properties"])
    assert runtime["required"] == documented["required"] == ["task_version_id"]

    runtime_nullable = {
        name for name, schema in runtime["properties"].items() if _allows_null(schema)
    }
    documented_nullable = {
        name for name, schema in documented["properties"].items() if schema.get("nullable") is True
    }
    assert runtime_nullable == documented_nullable

    assert runtime["properties"]["trigger_type"]["default"] == "manual"
    assert documented["properties"]["trigger_type"]["default"] == "manual"
    assert (
        runtime["properties"]["trigger_type"]["enum"]
        == documented["properties"]["trigger_type"]["enum"]
    )

    for field in ("run_key", "experiment_id", "experiment_subject_key"):
        runtime_field = _string_schema(runtime["properties"][field])
        documented_field = documented["properties"][field]
        assert runtime_field.get("minLength") == documented_field.get("minLength")
        assert runtime_field.get("maxLength") == documented_field.get("maxLength")


@pytest.mark.parametrize(
    "payload",
    [
        {"task_version_id": "task_version_v3_2_1", "trigger_type": "retry"},
        {"task_version_id": "task_version_v3_2_1", "run_key": "r" * 257},
        {
            "task_version_id": "task_version_v3_2_1",
            "execution_mode": "experiment",
            "experiment_id": "x",
            "experiment_subject_key": "subject-1",
        },
        {
            "task_version_id": "task_version_v3_2_1",
            "execution_mode": "experiment",
            "experiment_id": "x" * 129,
            "experiment_subject_key": "subject-1",
        },
        {
            "task_version_id": "task_version_v3_2_1",
            "experiment_id": "",
            "experiment_subject_key": "",
        },
    ],
)
def test_task_run_model_rejects_values_outside_the_documented_contract(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TaskRunRequest.model_validate(payload)


def test_task_run_model_accepts_documented_boundaries_defaults_and_nulls() -> None:
    minimum = TaskRunRequest.model_validate(
        {
            "task_version_id": "task_version_v3_2_1",
            "partition_key": None,
            "run_key": None,
        }
    )
    assert minimum.trigger_type == "manual"
    assert minimum.execution_mode == "production"
    assert minimum.model_dump(exclude_none=True) == {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        "execution_mode": "production",
    }

    maximum = TaskRunRequest.model_validate(
        {
            "task_version_id": "task_version_v3_2_1",
            "trigger_type": "backfill",
            "execution_mode": "experiment",
            "run_key": "r" * 256,
            "experiment_id": "e" * 128,
            "experiment_subject_key": "s" * 512,
        }
    )
    assert maximum.run_key == "r" * 256
    assert maximum.experiment_id == "e" * 128


def test_task_run_model_keeps_task_version_required_and_non_nullable() -> None:
    with pytest.raises(ValidationError):
        TaskRunRequest.model_validate({})
    with pytest.raises(ValidationError):
        TaskRunRequest.model_validate({"task_version_id": None})


@pytest.mark.parametrize(
    "field,value",
    [
        ("trigger_type", "retry"),
        ("run_key", "r" * 257),
        ("experiment_id", "x"),
        ("experiment_id", "x" * 129),
    ],
)
def test_task_run_api_rejects_contract_violations_without_side_effects(
    client,
    auth_headers,
    field: str,
    value: object,
) -> None:
    with SessionLocal() as session:
        run_count_before = session.scalar(select(func.count()).select_from(RunRecord))
        outbox_count_before = session.scalar(select(func.count()).select_from(OutboxEvent))

    payload: dict[str, object] = {
        "task_version_id": "task_version_v3_2_1",
        "trigger_type": "manual",
        field: value,
    }
    if field == "experiment_id":
        payload.update(
            execution_mode="experiment",
            experiment_subject_key="contract-subject",
        )

    response = client.post(
        "/api/v1/task-runs",
        json=payload,
        headers={**auth_headers, "Idempotency-Key": f"invalid-task-run-{field}-{len(str(value))}"},
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(RunRecord)) == run_count_before
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == outbox_count_before
