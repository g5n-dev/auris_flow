from __future__ import annotations

from typing import Any

import pytest

from app.services import adapters
from app.services.adapters import RealDagsterClient


class StubRealDagsterClient(RealDagsterClient):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__(
            graphql_url="http://dagster.example.test/graphql",
            repository_location_name="auris_flow_defs",
            repository_name="__repository__",
            default_job_name="auris_flow_generic_job",
        )
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def _request(self, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(body)
        return self.responses.pop(0)


def test_real_dagster_status_query_returns_engine_terminal_state() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "pipelineRunOrError": {
                        "__typename": "Run",
                        "runId": "dagster-run-success",
                        "status": "SUCCESS",
                        "canTerminate": False,
                    }
                }
            }
        ]
    )

    result = client.get_run_status("dagster-run-success")

    assert result.status == "success"
    assert result.operation == "run_status"
    assert result.details == {
        "mode": "real",
        "graphql_operation": "pipelineRunOrError",
        "external_run_id": "dagster-run-success",
        "dagster_run_id": "dagster-run-success",
        "dagster_status": "SUCCESS",
        "can_terminate": False,
        "response_typename": "Run",
    }
    request = client.requests[0]
    assert "pipelineRunOrError" in request["query"]
    assert request["variables"] == {"runId": "dagster-run-success"}


def test_real_dagster_status_query_fails_closed_for_unknown_run() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "pipelineRunOrError": {
                        "__typename": "RunNotFoundError",
                        "message": "not found",
                    }
                }
            }
        ]
    )

    result = client.get_run_status("missing-run")

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_RUN_NOT_FOUND"
    assert result.retryable is False
    assert result.details["response_typename"] == "RunNotFoundError"


@pytest.mark.parametrize(
    ("run_id", "status", "error_code"),
    [
        ("different-run", "STARTED", "DAGSTER_STATUS_IDENTITY_MISMATCH"),
        ("dagster-run-expected", "FUTURE_STATUS", "DAGSTER_STATUS_RESPONSE_INVALID"),
    ],
)
def test_real_dagster_status_query_rejects_wrong_identity_or_unknown_status(
    run_id: str,
    status: str,
    error_code: str,
) -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "pipelineRunOrError": {
                        "__typename": "Run",
                        "runId": run_id,
                        "status": status,
                        "canTerminate": True,
                    }
                }
            }
        ]
    )

    result = client.get_run_status("dagster-run-expected")

    assert result.status == "failed"
    assert result.error_code == error_code
    assert result.retryable is False


def test_real_dagster_cancel_uses_safe_terminate_and_returns_cancelled_run() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "terminateRun": {
                        "__typename": "TerminateRunSuccess",
                        "run": {
                            "runId": "dagster-run-cancel",
                            "status": "CANCELED",
                        },
                    }
                }
            }
        ]
    )

    result = client.cancel_run("dagster-run-cancel")

    assert result.status == "success"
    assert result.operation == "cancel_run"
    assert result.details["dagster_status"] == "CANCELED"
    assert result.details["response_typename"] == "TerminateRunSuccess"
    request = client.requests[0]
    assert "terminateRun" in request["query"]
    assert request["variables"] == {
        "runId": "dagster-run-cancel",
        "terminatePolicy": "SAFE_TERMINATE",
    }


def test_real_dagster_cancel_rejection_is_not_automatically_retried() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "terminateRun": {
                        "__typename": "TerminateRunFailure",
                        "message": "run is already terminal",
                        "run": {
                            "runId": "dagster-run-terminal",
                            "status": "SUCCESS",
                        },
                    }
                }
            }
        ]
    )

    result = client.cancel_run("dagster-run-terminal")

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_CANCEL_REJECTED"
    assert result.retryable is False
    assert result.details["dagster_status"] == "SUCCESS"


@pytest.mark.parametrize(
    ("run_id", "status", "error_code"),
    [
        ("different-run", "CANCELED", "DAGSTER_CANCEL_IDENTITY_MISMATCH"),
        ("dagster-run-cancel", "FUTURE_STATUS", "DAGSTER_CANCEL_RESPONSE_INVALID"),
    ],
)
def test_real_dagster_cancel_rejects_wrong_identity_or_unknown_status(
    run_id: str,
    status: str,
    error_code: str,
) -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "terminateRun": {
                        "__typename": "TerminateRunSuccess",
                        "run": {"runId": run_id, "status": status},
                    }
                }
            }
        ]
    )

    result = client.cancel_run("dagster-run-cancel")

    assert result.status == "failed"
    assert result.error_code == error_code
    assert result.retryable is False


def test_real_dagster_launch_failure_never_exposes_graphql_stack() -> None:
    canary = "internal-python-stack-canary"
    client = StubRealDagsterClient(
        [
            {
                "errors": [
                    {
                        "message": canary,
                        "extensions": {"stack": [canary, "secret-bearing-frame"]},
                    }
                ]
            }
        ]
    )

    result = client.submit_run_request(
        {
            "run_id": "task-run-stack-guard",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace-stack-guard",
            "event_type": "task_run.requested",
            "execution_mode": "diagnostic",
            "dispatch_idempotency_key": "outbox:task-run-stack-guard",
            "outbox_fencing_token": "701:1",
        }
    )

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_GRAPHQL_ERROR"
    assert result.error_message == "Dagster GraphQL request was rejected"
    assert len(result.details["graphql_error_sha256"]) == 64
    assert "graphql_errors" not in result.details
    assert "response" not in result.details
    assert "graphql_url" not in result.details
    assert canary not in repr(result)


def test_real_dagster_network_failure_never_exposes_url_or_exception() -> None:
    canary = "https://user:password@dagster.invalid/graphql?token=canary"

    class FailingRealDagsterClient(RealDagsterClient):
        def _request(self, body: dict[str, Any]) -> dict[str, Any]:
            del body
            raise OSError(canary)

    client = FailingRealDagsterClient(graphql_url=canary)
    result = client.submit_run_request(
        {
            "run_id": "task-run-network-guard",
            "tenant_id": "aurora_auto",
            "project_id": "sales_qa",
            "trace_id": "trace-network-guard",
            "event_type": "task_run.requested",
            "execution_mode": "diagnostic",
            "dispatch_idempotency_key": "outbox:task-run-network-guard",
            "outbox_fencing_token": "702:1",
        }
    )

    assert result.error_code == "DAGSTER_RUN_REQUEST_FAILED"
    assert result.error_message == "Dagster GraphQL request failed"
    assert "graphql_url" not in result.details
    assert canary not in repr(result)


def test_real_dagster_reconciliation_fails_closed_for_duplicate_run_key() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [
                            {"runId": "dagster-run-one", "status": "STARTED"},
                            {"runId": "dagster-run-two", "status": "QUEUED"},
                        ],
                    }
                }
            }
        ]
    )

    result = client.reconcile_run_request({"dispatch_idempotency_key": "duplicate-run-key"})

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_RECONCILIATION_AMBIGUOUS"
    assert result.retryable is False
    assert result.details == {
        "reconciled": False,
        "run_key": "duplicate-run-key",
        "result_count": 2,
    }
    assert "dagster-run-one" not in repr(result)
    assert "dagster-run-two" not in repr(result)
    assert "limit: 2" in client.requests[0]["query"]


def test_real_dagster_reconciliation_returns_exact_tag_absence_proof() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [],
                    }
                }
            }
        ]
    )

    result = client.reconcile_run_request(
        {"dispatch_idempotency_key": "temporarily-unavailable-run-key"}
    )

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_RECONCILIATION_ABSENT"
    assert result.retryable is True
    assert result.details == {
        "reconciled": False,
        "run_key": "temporarily-unavailable-run-key",
        "absence_proof": "dagster-exact-dispatch-tag-absent-v1",
    }
    assert client.requests[0]["variables"] == {
        "filter": {
            "tags": [
                {
                    "key": "auris/dispatch_idempotency_key",
                    "value": "temporarily-unavailable-run-key",
                }
            ]
        }
    }


def test_real_dagster_reconciliation_requires_exact_idempotency_tag_and_status() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [
                            {
                                "runId": "dagster-run-one",
                                "status": "STARTED",
                                "tags": [
                                    {
                                        "key": "auris/dispatch_idempotency_key",
                                        "value": "wrong-run-key",
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [
                            {
                                "runId": "dagster-run-two",
                                "status": "FUTURE_STATUS",
                                "tags": [
                                    {
                                        "key": "auris/dispatch_idempotency_key",
                                        "value": "expected-run-key",
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        ]
    )

    wrong_tag = client.reconcile_run_request({"dispatch_idempotency_key": "expected-run-key"})
    wrong_status = client.reconcile_run_request({"dispatch_idempotency_key": "expected-run-key"})

    assert wrong_tag.status == "failed"
    assert wrong_tag.error_code == "DAGSTER_RECONCILIATION_IDENTITY_MISMATCH"
    assert wrong_tag.retryable is False
    assert wrong_status.status == "failed"
    assert wrong_status.error_code == "DAGSTER_RECONCILIATION_RESPONSE_INVALID"
    assert wrong_status.retryable is False


def test_real_dagster_reconciliation_accepts_one_exactly_tagged_run() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [
                            {
                                "runId": "dagster-run-one",
                                "status": "STARTED",
                                "tags": [
                                    {"key": "tenant_id", "value": "aurora_auto"},
                                    {
                                        "key": "auris/dispatch_idempotency_key",
                                        "value": "expected-run-key",
                                    },
                                ],
                            }
                        ],
                    }
                }
            }
        ]
    )

    result = client.reconcile_run_request({"dispatch_idempotency_key": "expected-run-key"})

    assert result.status == "success"
    assert result.details["external_run_id"] == "dagster-run-one"
    assert result.details["dagster_status"] == "STARTED"


def test_real_dagster_reconciliation_rejects_noncanonical_run_id() -> None:
    client = StubRealDagsterClient(
        [
            {
                "data": {
                    "runsOrError": {
                        "__typename": "Runs",
                        "results": [
                            {
                                "runId": " dagster-run-one ",
                                "status": "STARTED",
                                "tags": [
                                    {
                                        "key": "auris/dispatch_idempotency_key",
                                        "value": "expected-run-key",
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        ]
    )

    result = client.reconcile_run_request({"dispatch_idempotency_key": "expected-run-key"})

    assert result.status == "failed"
    assert result.error_code == "DAGSTER_RECONCILIATION_RESPONSE_INVALID"
    assert result.retryable is False


def test_real_dagster_request_rejects_oversized_graphql_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OversizedResponse:
        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            assert size == adapters.MAX_DAGSTER_GRAPHQL_RESPONSE_BYTES + 1
            return b"x" * size

    monkeypatch.setattr(adapters, "urlopen", lambda *_args, **_kwargs: OversizedResponse())
    client = RealDagsterClient(graphql_url="http://dagster.example.test/graphql")

    with pytest.raises(ValueError, match="exceeds size limit"):
        client._request({"query": "query { version }"})
