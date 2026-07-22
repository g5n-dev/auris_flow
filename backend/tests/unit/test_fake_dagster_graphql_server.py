from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from app.main import probe_dagster_workspace

ROOT = Path(__file__).resolve().parents[3]
FAKE_DAGSTER_SCRIPT = ROOT / "scripts/fake_dagster_graphql_server.py"
UI_BFF_E2E_SCRIPT = ROOT / "scripts/verify_ui_bff_e2e.sh"

READINESS_QUERY = """
query AurisReadinessWorkspace {
  instance { daemonHealth { allDaemonStatuses { healthy } } }
  repositoriesOrError { __typename }
}
""".strip()
LAUNCH_QUERY = """
mutation LaunchAurisRun($executionParams: ExecutionParams!) {
  launchPipelineExecution(executionParams: $executionParams) { __typename }
}
""".strip()
LOOKUP_QUERY = """
query AurisRunByKey($filter: RunsFilter!) {
  runsOrError(filter: $filter, limit: 2) { __typename }
}
""".strip()
DISPATCH_TAG = "auris/dispatch_idempotency_key"


def _load_fake_server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("auris_fake_dagster_server", FAKE_DAGSTER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _running_server(
    module: ModuleType, receipt_log: Path | None = None
) -> Iterator[tuple[Any, str]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.FakeDagsterHandler)
    state = module.FakeDagsterState(receipt_log=receipt_log)
    server.state = state  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield state, f"http://{host}:{port}/graphql"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post_json(url: str, payload: object) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _valid_launch_payload(
    *,
    run_key: str = "dispatch-001",
    location_name: str = "auris_flow_defs",
    repository_name: str = "__repository__",
    job_name: str = "auris_flow_generic_job",
) -> dict[str, Any]:
    return {
        "query": LAUNCH_QUERY,
        "variables": {
            "executionParams": {
                "selector": {
                    "repositoryLocationName": location_name,
                    "repositoryName": repository_name,
                    "pipelineName": job_name,
                },
                "runConfigData": {"auris_context": {"trace_id": "trace-001"}},
                "executionMetadata": {
                    "tags": [
                        {"key": DISPATCH_TAG, "value": run_key},
                        {"key": "trace_id", "value": "trace-001"},
                    ]
                },
            }
        },
    }


def _assert_rejected_without_receipt(
    graphql_url: str,
    state: Any,
    receipt_log: Path,
    payload: object,
) -> None:
    status, response = _post_json(graphql_url, payload)

    assert status == 400
    assert response["errors"][0]["extensions"]["code"].startswith("FAKE_DAGSTER_")
    assert state.receipts == {}
    assert not receipt_log.exists() or receipt_log.read_text(encoding="utf-8") == ""


def test_readiness_returns_healthy_workspace_without_recording_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_fake_server_module()
    monkeypatch.delenv("DAGSTER_REPOSITORY_LOCATION_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_REPOSITORY_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_DEFAULT_JOB_NAME", raising=False)

    with _running_server(module) as (state, graphql_url):
        readiness = probe_dagster_workspace(graphql_url)

    assert readiness == "ok"
    assert state.receipts == {}


def test_readiness_and_launch_honor_custom_workspace_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_fake_server_module()
    monkeypatch.setenv("DAGSTER_REPOSITORY_LOCATION_NAME", "custom_location")
    monkeypatch.setenv("DAGSTER_REPOSITORY_NAME", "custom_repository")
    monkeypatch.setenv("DAGSTER_DEFAULT_JOB_NAME", "custom_job")

    with _running_server(module) as (state, graphql_url):
        assert probe_dagster_workspace(graphql_url) == "ok"
        status, response = _post_json(
            graphql_url,
            _valid_launch_payload(
                location_name="custom_location",
                repository_name="custom_repository",
                job_name="custom_job",
            ),
        )

    assert status == 200
    assert response["data"]["launchPipelineExecution"]["__typename"] == ("LaunchRunSuccess")
    assert len(state.receipts) == 1


def test_launch_then_exact_run_key_lookup_records_only_the_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _load_fake_server_module()
    monkeypatch.delenv("DAGSTER_REPOSITORY_LOCATION_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_REPOSITORY_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_DEFAULT_JOB_NAME", raising=False)
    receipt_log = tmp_path / "receipts.jsonl"

    with _running_server(module, receipt_log) as (state, graphql_url):
        launch_status, launch = _post_json(
            graphql_url, _valid_launch_payload(run_key="dispatch-exact")
        )
        lookup_status, lookup = _post_json(
            graphql_url,
            {
                "query": LOOKUP_QUERY,
                "variables": {
                    "filter": {"tags": [{"key": DISPATCH_TAG, "value": "dispatch-exact"}]}
                },
            },
        )

    run_id = launch["data"]["launchPipelineExecution"]["run"]["runId"]
    assert launch_status == 200
    assert lookup_status == 200
    assert lookup["data"]["runsOrError"]["results"] == [
        {
            "runId": run_id,
            "status": "STARTED",
            "tags": [
                {"key": DISPATCH_TAG, "value": "dispatch-exact"},
                {"key": "trace_id", "value": "trace-001"},
            ],
        }
    ]
    assert len(state.receipts) == 1
    assert len(receipt_log.read_text(encoding="utf-8").splitlines()) == 1


def test_fake_protocol_endpoint_accepts_only_the_allowlisted_audio_domain_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_fake_server_module()
    monkeypatch.delenv("DAGSTER_REPOSITORY_LOCATION_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_REPOSITORY_NAME", raising=False)
    monkeypatch.delenv("DAGSTER_DEFAULT_JOB_NAME", raising=False)

    with _running_server(module) as (state, graphql_url):
        accepted_status, _accepted = _post_json(
            graphql_url,
            _valid_launch_payload(job_name="auris_flow_audio_intelligence_v1"),
        )
        rejected_status, _rejected = _post_json(
            graphql_url,
            _valid_launch_payload(job_name="caller_selected_job"),
        )

    assert accepted_status == 200
    assert rejected_status == 400
    assert {receipt["job_name"] for receipt in state.receipts.values()} == {
        "auris_flow_audio_intelligence_v1"
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="array-payload"),
        pytest.param("not-an-object", id="string-payload"),
        pytest.param(None, id="null-payload"),
        pytest.param(
            {
                "query": "query UnknownOperation { repositoriesOrError { __typename } }",
                "variables": {},
            },
            id="unknown-operation",
        ),
        pytest.param(
            {
                "query": "mutation { launchPipelineExecution { __typename } }",
                "variables": {},
            },
            id="anonymous-operation",
        ),
        pytest.param(
            {
                "query": ('query UnknownOperation { field(arg: "LaunchAurisRun") }'),
                "variables": {},
            },
            id="allowed-name-smuggling",
        ),
        pytest.param(
            {
                "query": READINESS_QUERY,
                "operationName": "LaunchAurisRun",
                "variables": {},
            },
            id="operation-name-mismatch",
        ),
        pytest.param(
            {"query": READINESS_QUERY, "variables": []},
            id="array-variables",
        ),
    ],
)
def test_unknown_or_non_object_graphql_requests_fail_closed(
    payload: object, tmp_path: Path
) -> None:
    module = _load_fake_server_module()
    receipt_log = tmp_path / "receipts.jsonl"

    with _running_server(module, receipt_log) as (state, graphql_url):
        _assert_rejected_without_receipt(graphql_url, state, receipt_log, payload)


@pytest.mark.parametrize(
    "case",
    [
        "missing-variables",
        "missing-execution-params",
        "non-object-execution-params",
        "missing-selector",
        "non-object-selector",
        "missing-selector-field",
        "blank-selector-field",
        "wrong-workspace-selector",
        "missing-run-config",
        "non-object-run-config",
        "missing-execution-metadata",
        "missing-tags",
        "non-array-tags",
        "empty-tags",
        "non-object-tag",
        "blank-tag-key",
        "missing-dispatch-tag",
        "duplicate-dispatch-tag",
    ],
)
def test_malformed_launch_requests_fail_closed_without_receipts(case: str, tmp_path: Path) -> None:
    module = _load_fake_server_module()
    receipt_log = tmp_path / "receipts.jsonl"
    payload = _valid_launch_payload()
    variables = payload["variables"]
    execution_params = variables["executionParams"]

    if case == "missing-variables":
        payload.pop("variables")
    elif case == "missing-execution-params":
        variables.pop("executionParams")
    elif case == "non-object-execution-params":
        variables["executionParams"] = []
    elif case == "missing-selector":
        execution_params.pop("selector")
    elif case == "non-object-selector":
        execution_params["selector"] = []
    elif case == "missing-selector-field":
        execution_params["selector"].pop("pipelineName")
    elif case == "blank-selector-field":
        execution_params["selector"]["repositoryName"] = " "
    elif case == "wrong-workspace-selector":
        execution_params["selector"]["repositoryLocationName"] = "other"
    elif case == "missing-run-config":
        execution_params.pop("runConfigData")
    elif case == "non-object-run-config":
        execution_params["runConfigData"] = []
    elif case == "missing-execution-metadata":
        execution_params.pop("executionMetadata")
    elif case == "missing-tags":
        execution_params["executionMetadata"].pop("tags")
    elif case == "non-array-tags":
        execution_params["executionMetadata"]["tags"] = {}
    elif case == "empty-tags":
        execution_params["executionMetadata"]["tags"] = []
    elif case == "non-object-tag":
        execution_params["executionMetadata"]["tags"] = ["bad"]
    elif case == "blank-tag-key":
        execution_params["executionMetadata"]["tags"] = [{"key": " ", "value": "value"}]
    elif case == "missing-dispatch-tag":
        execution_params["executionMetadata"]["tags"] = [{"key": "trace_id", "value": "trace-001"}]
    elif case == "duplicate-dispatch-tag":
        execution_params["executionMetadata"]["tags"].append(
            {"key": DISPATCH_TAG, "value": "dispatch-002"}
        )
    else:  # pragma: no cover - parametrization guard
        raise AssertionError(case)

    with _running_server(module, receipt_log) as (state, graphql_url):
        _assert_rejected_without_receipt(graphql_url, state, receipt_log, payload)


@pytest.mark.parametrize(
    "variables",
    [
        pytest.param({}, id="missing-filter"),
        pytest.param({"filter": []}, id="non-object-filter"),
        pytest.param({"filter": {}}, id="missing-tags"),
        pytest.param({"filter": {"tags": []}}, id="empty-tags"),
        pytest.param({"filter": {"tags": {}}}, id="non-array-tags"),
        pytest.param(
            {"filter": {"tags": [{"key": "trace_id", "value": "trace"}]}},
            id="missing-dispatch-tag",
        ),
        pytest.param(
            {
                "filter": {
                    "tags": [
                        {"key": DISPATCH_TAG, "value": "one"},
                        {"key": DISPATCH_TAG, "value": "two"},
                    ]
                }
            },
            id="duplicate-dispatch-tag",
        ),
    ],
)
def test_malformed_run_lookup_fails_closed_without_receipts(
    variables: object, tmp_path: Path
) -> None:
    module = _load_fake_server_module()
    receipt_log = tmp_path / "receipts.jsonl"
    payload = {"query": LOOKUP_QUERY, "variables": variables}

    with _running_server(module, receipt_log) as (state, graphql_url):
        _assert_rejected_without_receipt(graphql_url, state, receipt_log, payload)


def test_ui_bff_gate_declares_bounded_deadline_curl_and_cleanup_contract() -> None:
    script = UI_BFF_E2E_SCRIPT.read_text(encoding="utf-8")

    assert "AURIS_UI_BFF_E2E_TIMEOUT_SECONDS" in script
    assert "scripts/run_with_deadline.py" in script
    assert "AURIS_UI_BFF_E2E_DEADLINE_CHILD" in script
    assert "--connect-timeout" in script
    assert "--max-time" in script
    assert "CLEANUP_STARTED" in script
    assert "trap - EXIT INT TERM" in script
    assert "handle_signal 130" in script
    assert "handle_signal 143" in script
    assert "kill -TERM" in script
    assert "kill -KILL" in script
    assert "VITE_DEMO_MODE=false" in script


def _write_fake_gate_commands(
    tmp_path: Path, *, health_body: str = '{"status":"ok"}'
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "npm-started"
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(health_body)}\n",
        encoding="utf-8",
    )
    npm = fake_bin / "npm"
    npm.write_text(
        """#!/bin/sh
set -eu
: > "${AURIS_FAKE_NPM_STARTED:?}"
trap 'exit 130' INT
trap 'exit 143' TERM
while :; do
  sleep 0.05
done
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    npm.chmod(0o755)
    return fake_bin, marker


def _fake_gate_environment(
    tmp_path: Path,
    *,
    timeout_seconds: str = "30",
    health_body: str = '{"status":"ok"}',
) -> tuple[dict[str, str], Path]:
    fake_bin, marker = _write_fake_gate_commands(tmp_path, health_body=health_body)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PYTHON": sys.executable,
            "AURIS_E2E_URL": "http://fake-ui.invalid/",
            "AURIS_E2E_AUTOSTART": "0",
            "AURIS_E2E_FORCE_AUTOSTART": "0",
            "AURIS_UI_BFF_E2E_TIMEOUT_SECONDS": timeout_seconds,
            "AURIS_FAKE_NPM_STARTED": str(marker),
        }
    )
    environment.pop("AURIS_UI_BFF_E2E_DEADLINE_CHILD", None)
    return environment, marker


def test_ui_bff_gate_rejects_html_spa_fallback_as_external_health(
    tmp_path: Path,
) -> None:
    environment, marker = _fake_gate_environment(
        tmp_path,
        timeout_seconds="2",
        health_body="<!doctype html><title>Vite SPA fallback</title>",
    )

    completed = subprocess.run(
        ["bash", str(UI_BFF_E2E_SCRIPT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )

    assert completed.returncode == 1
    assert "target is not reachable" in completed.stderr
    assert not marker.exists()


def _wait_for_file(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"gate exited before fake npm started: {process.returncode}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.02)
    raise AssertionError("fake npm did not start before test deadline")


@pytest.mark.parametrize(
    ("requested_signal", "expected_status"),
    [(signal.SIGINT, 130), (signal.SIGTERM, 143)],
)
def test_ui_bff_gate_preserves_signal_exit_codes(
    requested_signal: signal.Signals, expected_status: int, tmp_path: Path
) -> None:
    environment, marker = _fake_gate_environment(tmp_path)
    process = subprocess.Popen(
        ["bash", str(UI_BFF_E2E_SCRIPT)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_file(marker, process)
        process.send_signal(requested_signal)
        stdout, stderr = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)

    assert process.returncode == expected_status, (stdout, stderr)


def test_ui_bff_gate_enforces_whole_gate_wall_clock_deadline(
    tmp_path: Path,
) -> None:
    environment, marker = _fake_gate_environment(tmp_path, timeout_seconds="1")

    completed = subprocess.run(
        ["bash", str(UI_BFF_E2E_SCRIPT)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )

    assert marker.exists()
    assert completed.returncode == 124
    assert "UI/BFF E2E gate exceeded 1s deadline" in completed.stderr
