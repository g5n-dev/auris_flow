from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from auris_flow_dagster.contracts import AurisRunContext


@pytest.fixture
def scope() -> AurisRunContext:
    return AurisRunContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        trace_id="trace-dagster-001",
        run_id="run_task_001",
        dispatch_idempotency_key="outbox:task:001",
        outbox_fencing_token="456:2",  # noqa: S106 - lease epoch, not a credential
        event_type="task_run.requested",
        partition_key="aurora_auto/2026-07-18/001",
        task_version_id="task_version_v3_2_1",
    )


@pytest.fixture
def valid_context(scope: AurisRunContext) -> dict[str, Any]:
    return {
        "tenant_id": scope.tenant_id,
        "project_id": scope.project_id,
        "trace_id": scope.trace_id,
        "run_id": scope.run_id,
        "dispatch_idempotency_key": scope.dispatch_idempotency_key,
        "outbox_fencing_token": scope.outbox_fencing_token,
        "event_type": scope.event_type,
        "partition_key": scope.partition_key,
        "task_version_id": scope.task_version_id,
    }


@pytest.fixture
def keyring_file(tmp_path: Path) -> Path:
    path = tmp_path / "completion_receipt_key_bindings"
    path.write_text(
        """{
          "dagster-2026-01": {
            "secret": "unit-only-dagster-signing-value-000000000001",
            "allowed_sources": ["dagster"],
            "allowed_scopes": [
              {"tenant_id": "aurora_auto", "project_id": "sales_qa"}
            ]
          }
        }""",
        encoding="utf-8",
    )
    return path
