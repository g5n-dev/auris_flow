#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.adapters import RealDagsterClient  # noqa: E402


WORKSPACE_QUERY = """
query AurisRealDagsterWorkspace {
  version
  repositoriesOrError {
    __typename
    ... on RepositoryConnection {
      nodes {
        name
        location { name }
        pipelines { name }
      }
    }
    ... on PythonError { message }
  }
}
""".strip()

DAEMON_HEALTH_QUERY = """
query AurisRealDagsterDaemonHealth {
  instance {
    daemonHealth {
      allDaemonStatuses {
        daemonType
        required
        healthy
        lastHeartbeatTime
      }
    }
  }
}
""".strip()

TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "CANCELED"})
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
MAX_GRAPHQL_RESPONSE_BYTES = 1_048_576
DAGSTER_HEARTBEAT_MAX_AGE_SECONDS = 90.0
DAGSTER_HEARTBEAT_FUTURE_SKEW_SECONDS = 5.0


class GateFailure(RuntimeError):
    """A release-gate failure without secret-bearing response bodies."""


def validate_source_commit(value: str) -> str:
    normalized = value.strip().lower()
    if COMMIT_PATTERN.fullmatch(normalized) is None:
        raise GateFailure("real Dagster gate requires an exact source commit")
    return normalized


def graphql_request(
    url: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    body = json.dumps(
        {"query": query, "variables": variables or {}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(MAX_GRAPHQL_RESPONSE_BYTES + 1)
        if len(raw) > MAX_GRAPHQL_RESPONSE_BYTES:
            raise ValueError("GraphQL response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
    except (
        OSError,
        URLError,
        HTTPError,
        TimeoutError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise GateFailure("real Dagster GraphQL request failed") from exc
    if not isinstance(payload, dict):
        raise GateFailure("real Dagster GraphQL response is invalid")
    return payload


def validate_workspace(
    response: dict[str, Any],
    *,
    location_name: str,
    repository_name: str,
    job_name: str,
) -> dict[str, str]:
    data = response.get("data")
    repositories = data.get("repositoriesOrError") if isinstance(data, dict) else None
    version = data.get("version") if isinstance(data, dict) else None
    if (
        not isinstance(version, str)
        or not version
        or not isinstance(repositories, dict)
        or repositories.get("__typename") != "RepositoryConnection"
    ):
        raise GateFailure("real Dagster workspace query was rejected")
    nodes = repositories.get("nodes")
    if not isinstance(nodes, list):
        raise GateFailure("real Dagster workspace repository list is invalid")
    for node in nodes:
        if not isinstance(node, dict):
            continue
        location = node.get("location")
        pipelines = node.get("pipelines")
        pipeline_items = pipelines if isinstance(pipelines, list) else []
        pipeline_names = {
            str(item.get("name"))
            for item in pipeline_items
            if isinstance(item, dict) and item.get("name")
        }
        if (
            node.get("name") == repository_name
            and isinstance(location, dict)
            and location.get("name") == location_name
            and job_name in pipeline_names
        ):
            return {
                "dagster_version": version,
                "location_name": location_name,
                "repository_name": repository_name,
                "job_name": job_name,
            }
    raise GateFailure("required real Dagster workspace repository or job is missing")


def wait_for_workspace(
    graphql_url: str,
    *,
    location_name: str,
    repository_name: str,
    job_name: str,
    timeout_seconds: float,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not queried"
    while time.monotonic() < deadline:
        try:
            return validate_workspace(
                graphql_request(graphql_url, WORKSPACE_QUERY),
                location_name=location_name,
                repository_name=repository_name,
                job_name=job_name,
            )
        except GateFailure as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise GateFailure(f"timed out waiting for real Dagster workspace: {last_error}")


def validate_daemon_health(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data")
    instance = data.get("instance") if isinstance(data, dict) else None
    daemon_health = instance.get("daemonHealth") if isinstance(instance, dict) else None
    statuses = (
        daemon_health.get("allDaemonStatuses")
        if isinstance(daemon_health, dict)
        else None
    )
    if not isinstance(statuses, list) or not statuses:
        raise GateFailure("real Dagster daemon health list is missing")

    normalized: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    required_count = 0
    heartbeat_now = time.time()
    for status in statuses:
        if not isinstance(status, dict):
            raise GateFailure("real Dagster daemon health entry is invalid")
        daemon_type = status.get("daemonType")
        required = status.get("required")
        healthy = status.get("healthy")
        last_heartbeat = status.get("lastHeartbeatTime")
        if (
            not isinstance(daemon_type, str)
            or not daemon_type
            or daemon_type in seen_types
            or not isinstance(required, bool)
            or (healthy is not None and not isinstance(healthy, bool))
        ):
            raise GateFailure("real Dagster daemon health entry is invalid")
        if last_heartbeat is not None and (
            isinstance(last_heartbeat, bool)
            or not isinstance(last_heartbeat, (int, float))
            or not math.isfinite(float(last_heartbeat))
        ):
            raise GateFailure("real Dagster daemon heartbeat is invalid")
        seen_types.add(daemon_type)
        if required:
            required_count += 1
            if healthy is not True:
                raise GateFailure(
                    f"required real Dagster daemon is unhealthy: {daemon_type}"
                )
            if (
                last_heartbeat is None
                or float(last_heartbeat)
                < heartbeat_now - DAGSTER_HEARTBEAT_MAX_AGE_SECONDS
                or float(last_heartbeat)
                > heartbeat_now + DAGSTER_HEARTBEAT_FUTURE_SKEW_SECONDS
            ):
                raise GateFailure(
                    f"required real Dagster daemon heartbeat is untrusted: {daemon_type}"
                )
        normalized.append(
            {
                "daemon_type": daemon_type,
                "required": required,
                "healthy": healthy,
                "last_heartbeat_time": (
                    float(last_heartbeat) if last_heartbeat is not None else None
                ),
            }
        )
    if required_count == 0:
        raise GateFailure("real Dagster reported no required daemons")
    return sorted(normalized, key=lambda item: item["daemon_type"])


def wait_for_daemon_health(
    graphql_url: str, *, timeout_seconds: float
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not queried"
    while time.monotonic() < deadline:
        try:
            return validate_daemon_health(
                graphql_request(graphql_url, DAEMON_HEALTH_QUERY)
            )
        except GateFailure as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise GateFailure(
        f"timed out waiting for real Dagster required daemons: {last_error}"
    )


def run_payload(*, scenario: str, suffix: str, execution_mode: str) -> dict[str, Any]:
    run_id = f"dagster-gate-{scenario}-{suffix}"
    return {
        "run_id": run_id,
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": f"trace-{run_id}",
        "event_type": "task_run.requested",
        "task_version_id": "task_version_v3_2_1",
        "task_version_behavior_sha256": "a" * 64,
        "task_version_binding_sha256": "b" * 64,
        "expected_executed_bundle_sha256": "c" * 64,
        "dispatch_idempotency_key": f"dagster-gate:{scenario}:{suffix}",
        "outbox_fencing_token": "1:1",
        "job_name": "caller-selected-job-must-be-ignored",
        "dagster_run_draft": {"job_name": "draft-job-must-be-ignored"},
        "run_config": {
            "execution": {"mode": execution_mode},
            "resources": {"caller_control": {"config": {"value": "must-not-forward"}}},
            "auris_context": {"tenant_id": "forged-tenant-must-not-forward"},
        },
    }


def submit_run(client: RealDagsterClient, payload: dict[str, Any]) -> dict[str, Any]:
    result = client.submit_run_request(payload)
    if result.status != "success":
        raise GateFailure(f"real Dagster run submission failed: {result.error_code}")
    details = dict(result.details)
    external_run_id = details.get("external_run_id")
    if (
        details.get("mode") != "real"
        or not isinstance(external_run_id, str)
        or not external_run_id
        or details.get("response_typename") != "LaunchRunSuccess"
        or details.get("protocol_receipt") not in ({}, None)
        or details.get("job_name") != client.default_job_name
    ):
        raise GateFailure(
            "real Dagster launch receipt is incomplete or contains test-stub proof"
        )
    reconciled = client.reconcile_run_request(payload)
    if (
        reconciled.status != "success"
        or reconciled.details.get("external_run_id") != external_run_id
    ):
        raise GateFailure(
            "real Dagster run-key reconciliation did not find the submitted run"
        )
    return {
        "business_run_id": payload["run_id"],
        "trace_id": payload["trace_id"],
        "run_key": payload["dispatch_idempotency_key"],
        "dagster_run_id": external_run_id,
        "launch_status": details.get("dagster_run_status"),
        "response_typename": details.get("response_typename"),
        "selected_job_name": details.get("job_name"),
        "reconciled": True,
    }


def wait_for_status(
    client: RealDagsterClient,
    run_id: str,
    expected: set[str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = client.get_run_status(run_id)
        if result.status == "success":
            last = dict(result.details)
            status = str(last.get("dagster_status") or "")
            if status in expected:
                return last
            if status in TERMINAL_STATUSES:
                raise GateFailure(
                    f"real Dagster run reached {status}, expected {sorted(expected)}"
                )
        elif not result.retryable:
            raise GateFailure(f"real Dagster status query failed: {result.error_code}")
        time.sleep(0.2)
    raise GateFailure(
        "timed out waiting for real Dagster status; "
        f"expected={sorted(expected)}, last={last.get('dagster_status')}"
    )


def callback_receipts(callback_url: str) -> list[dict[str, Any]]:
    try:
        with urlopen(f"{callback_url.rstrip('/')}/receipts", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        URLError,
        HTTPError,
        TimeoutError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise GateFailure("Dagster completion callback proof is unavailable") from exc
    receipts = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(receipts, list):
        raise GateFailure("Dagster completion callback proof is invalid")
    return [item for item in receipts if isinstance(item, dict)]


def validate_completion_receipt(
    receipt: dict[str, Any],
    *,
    business_run_id: str,
    dagster_run_id: str,
    trace_id: str,
    completion_status: str,
) -> dict[str, Any]:
    expected = {
        "adapter": "dagster",
        "source": "dagster",
        "run_id": business_run_id,
        "status": completion_status,
        "external_id": dagster_run_id,
        "completion_receipt_id": f"dagster:{dagster_run_id}",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": trace_id,
        "key_id": "dagster-v1",
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise GateFailure("signed Dagster completion receipt binding is invalid")
    body_sha256 = receipt.get("body_sha256")
    if (
        not isinstance(body_sha256, str)
        or len(body_sha256) != 64
        or any(character not in "0123456789abcdef" for character in body_sha256)
    ):
        raise GateFailure("signed Dagster completion receipt digest is invalid")
    if completion_status == "success":
        result_ref = receipt.get("result_ref")
        metrics = receipt.get("metrics")
        if (
            not isinstance(result_ref, dict)
            or result_ref.get("auris_run_id") != business_run_id
            or not isinstance(metrics, dict)
            or metrics.get("control_plane_acknowledged") != 1
        ):
            raise GateFailure("Dagster success completion result binding is invalid")
    elif (
        receipt.get("error_code") != "DAGSTER_WORKFLOW_FAILED"
        or receipt.get("retryable") is not False
    ):
        raise GateFailure("Dagster failure completion semantics are invalid")
    return receipt


def wait_for_receipt(
    callback_url: str,
    *,
    business_run_id: str,
    dagster_run_id: str,
    trace_id: str,
    completion_status: str,
    timeout_seconds: float = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for receipt in callback_receipts(callback_url):
            if receipt.get("run_id") == business_run_id:
                return validate_completion_receipt(
                    receipt,
                    business_run_id=business_run_id,
                    dagster_run_id=dagster_run_id,
                    trace_id=trace_id,
                    completion_status=completion_status,
                )
        time.sleep(0.2)
    raise GateFailure(
        f"timed out waiting for signed Dagster {completion_status} completion receipt"
    )


def assert_no_completion_receipt(
    callback_url: str,
    *,
    business_run_id: str,
    observation_seconds: float,
) -> None:
    deadline = time.monotonic() + max(0.0, observation_seconds)
    while True:
        matches = [
            receipt
            for receipt in callback_receipts(callback_url)
            if receipt.get("run_id") == business_run_id
        ]
        if matches:
            raise GateFailure(
                f"unexpected completion receipt for canceled run {business_run_id}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def verify_terminal_persistence(
    client: RealDagsterClient, prior: dict[str, Any]
) -> list[dict[str, str]]:
    expected_by_key = {
        "success": "SUCCESS",
        "failure": "FAILURE",
        "cancel": "CANCELED",
    }
    proofs: list[dict[str, str]] = []
    scenarios = prior.get("scenarios")
    if not isinstance(scenarios, dict):
        raise GateFailure("prior real Dagster artifact has no scenarios")
    for key, expected_status in expected_by_key.items():
        scenario = scenarios.get(key)
        run_id = scenario.get("dagster_run_id") if isinstance(scenario, dict) else None
        if not isinstance(run_id, str) or not run_id:
            raise GateFailure("prior real Dagster artifact is missing a run id")
        observed = wait_for_status(
            client, run_id, {expected_status}, timeout_seconds=30
        )
        proofs.append(
            {"dagster_run_id": run_id, "dagster_status": observed["dagster_status"]}
        )
    return proofs


def initial_phase(
    client: RealDagsterClient,
    failure_client: RealDagsterClient,
    cancel_client: RealDagsterClient,
    *,
    callback_url: str,
    suffix: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    success_payload = run_payload(
        scenario="success",
        suffix=suffix,
        execution_mode="control-plane-acknowledgement",
    )
    success = submit_run(client, success_payload)
    success_status = wait_for_status(
        client, success["dagster_run_id"], {"SUCCESS"}, timeout_seconds=timeout_seconds
    )
    success_receipt = wait_for_receipt(
        callback_url,
        business_run_id=success["business_run_id"],
        dagster_run_id=success["dagster_run_id"],
        trace_id=success["trace_id"],
        completion_status="success",
    )

    failure_payload = run_payload(
        scenario="failure",
        suffix=suffix,
        execution_mode="gate-intentional-unsupported-mode",
    )
    failure = submit_run(failure_client, failure_payload)
    failure_status = wait_for_status(
        client, failure["dagster_run_id"], {"FAILURE"}, timeout_seconds=timeout_seconds
    )
    failure_receipt = wait_for_receipt(
        callback_url,
        business_run_id=failure["business_run_id"],
        dagster_run_id=failure["dagster_run_id"],
        trace_id=failure["trace_id"],
        completion_status="failed",
    )

    cancel_payload = run_payload(
        scenario="cancel",
        suffix=suffix,
        execution_mode="ci-cancel-delay",
    )
    cancel = submit_run(cancel_client, cancel_payload)
    wait_for_status(
        client, cancel["dagster_run_id"], {"STARTED"}, timeout_seconds=timeout_seconds
    )
    assert_no_completion_receipt(
        callback_url,
        business_run_id=cancel["business_run_id"],
        observation_seconds=1.0,
    )
    wait_for_status(
        client, cancel["dagster_run_id"], {"STARTED"}, timeout_seconds=timeout_seconds
    )
    cancellation = cancel_client.cancel_run(cancel["dagster_run_id"])
    if cancellation.status != "success":
        raise GateFailure(
            f"real Dagster SAFE_TERMINATE failed: {cancellation.error_code}"
        )
    cancel_status = wait_for_status(
        cancel_client,
        cancel["dagster_run_id"],
        {"CANCELED"},
        timeout_seconds=timeout_seconds,
    )
    assert_no_completion_receipt(
        callback_url,
        business_run_id=cancel["business_run_id"],
        observation_seconds=2.0,
    )

    return {
        "success": {
            **success,
            "dagster_status": success_status["dagster_status"],
            "completion_receipt_id": success_receipt["completion_receipt_id"],
            "completion_body_sha256": success_receipt["body_sha256"],
        },
        "failure": {
            **failure,
            "dagster_status": failure_status["dagster_status"],
            "completion_receipt_id": failure_receipt["completion_receipt_id"],
            "completion_body_sha256": failure_receipt["body_sha256"],
        },
        "cancel": {
            **cancel,
            "dagster_status": cancel_status["dagster_status"],
            "terminate_policy": cancellation.details.get("terminate_policy"),
            "execution_mode": "ci-cancel-delay",
            "completion_receipt_absent_after_cancel": True,
            "proof_scope": "dagster-engine-only",
        },
    }


def recovery_phase(
    client: RealDagsterClient,
    *,
    callback_url: str,
    suffix: str,
    timeout_seconds: float,
    prior: dict[str, Any],
) -> dict[str, Any]:
    persisted = verify_terminal_persistence(client, prior)
    prior_scenarios = prior.get("scenarios")
    canceled = (
        prior_scenarios.get("cancel") if isinstance(prior_scenarios, dict) else None
    )
    canceled_business_run_id = (
        canceled.get("business_run_id") if isinstance(canceled, dict) else None
    )
    if not isinstance(canceled_business_run_id, str) or not canceled_business_run_id:
        raise GateFailure("prior canceled run proof is invalid")
    assert_no_completion_receipt(
        callback_url,
        business_run_id=canceled_business_run_id,
        observation_seconds=21.0,
    )
    payload = run_payload(
        scenario="recovery",
        suffix=suffix,
        execution_mode="control-plane-acknowledgement",
    )
    recovery = submit_run(client, payload)
    status = wait_for_status(
        client, recovery["dagster_run_id"], {"SUCCESS"}, timeout_seconds=timeout_seconds
    )
    receipt = wait_for_receipt(
        callback_url,
        business_run_id=recovery["business_run_id"],
        dagster_run_id=recovery["dagster_run_id"],
        trace_id=recovery["trace_id"],
        completion_status="success",
    )
    return {
        "persisted_terminal_runs": persisted,
        "canceled_completion_receipt_absent_after_restart": True,
        "post_restart_submission": {
            **recovery,
            "dagster_status": status["dagster_status"],
            "completion_receipt_id": receipt["completion_receipt_id"],
            "completion_body_sha256": receipt["body_sha256"],
        },
    }


def _write_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graphql-url", required=True)
    parser.add_argument("--callback-url", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--execution-environment",
        choices=("compose", "local-process"),
        required=True,
    )
    parser.add_argument("--phase", choices=("initial", "recovery"), required=True)
    parser.add_argument("--prior-artifact", type=Path)
    parser.add_argument("--run-suffix", default=f"{int(time.time())}-{os.getpid()}")
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    if not 10 <= args.timeout_seconds <= 300:
        raise SystemExit("real Dagster timeout must be between 10 and 300 seconds")

    source_commit = validate_source_commit(args.source_commit)
    location_name = "auris_flow_defs"
    repository_name = "__repository__"
    job_name = "auris_flow_generic_job"
    client_kwargs = {
        "graphql_url": args.graphql_url,
        "repository_location_name": location_name,
        "repository_name": repository_name,
        "default_job_name": job_name,
    }
    client = RealDagsterClient(
        **client_kwargs,
    )
    workspace = wait_for_workspace(
        args.graphql_url,
        location_name=location_name,
        repository_name=repository_name,
        job_name=job_name,
        timeout_seconds=args.timeout_seconds,
    )
    daemon_health = wait_for_daemon_health(
        args.graphql_url,
        timeout_seconds=args.timeout_seconds,
    )
    started_at = datetime.now(UTC).isoformat()
    if args.phase == "initial":
        artifact = {
            "schema_version": "auris.real-dagster-gate.v1",
            "status": "initial-ok",
            "source_commit": source_commit,
            "execution_environment": args.execution_environment,
            "started_at": started_at,
            "workspace": workspace,
            "daemon_health": daemon_health,
            "excluded_scope": [
                "product-bff-cancel-and-reconcile-routes",
                "run-record-submitted-versus-callback-ordering",
                "business-state-transition-after-engine-cancel",
            ],
            "scenarios": initial_phase(
                client,
                RealDagsterClient(
                    **client_kwargs, execution_mode="ci-intentional-failure"
                ),
                RealDagsterClient(**client_kwargs, execution_mode="ci-cancel-delay"),
                callback_url=args.callback_url,
                suffix=args.run_suffix,
                timeout_seconds=args.timeout_seconds,
            ),
            "completed_at": datetime.now(UTC).isoformat(),
        }
    else:
        if args.prior_artifact is None:
            raise SystemExit("--prior-artifact is required for the recovery phase")
        try:
            prior = json.loads(args.prior_artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit("prior real Dagster artifact is invalid") from exc
        if not isinstance(prior, dict) or prior.get("status") != "initial-ok":
            raise SystemExit("prior real Dagster artifact is not an initial-ok result")
        if prior.get("execution_environment") != args.execution_environment:
            raise SystemExit(
                "prior real Dagster artifact execution environment does not match"
            )
        if prior.get("source_commit") != source_commit:
            raise SystemExit("prior real Dagster artifact source commit does not match")
        artifact = {
            **prior,
            "status": "ok",
            "workspace_after_restart": workspace,
            "daemon_health_after_restart": daemon_health,
            "recovery": recovery_phase(
                client,
                callback_url=args.callback_url,
                suffix=f"{args.run_suffix}-after-restart",
                timeout_seconds=args.timeout_seconds,
                prior=prior,
            ),
            "completed_at": datetime.now(UTC).isoformat(),
        }
    _write_artifact(args.artifact, artifact)
    print(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except GateFailure as exc:
        print(f"real Dagster gate failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
