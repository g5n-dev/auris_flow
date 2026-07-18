from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


class _ComposeLoader(yaml.SafeLoader):
    pass


def _compose_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _compose_override)


def _load_script(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signed_request() -> tuple[str, dict[str, str], bytes, dict[str, object], datetime]:
    path = "/api/v1/runs/dagster-gate-success/external-completion-receipts"
    body = json.dumps(
        {
            "adapter": "dagster",
            "completion_receipt_id": "dagster:engine-run-001",
            "external_id": "engine-run-001",
            "metrics": {"control_plane_acknowledged": 1},
            "result_ref": {"auris_run_id": "dagster-gate-success"},
            "retryable": True,
            "source": "dagster",
            "status": "success",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    headers = {
        "Idempotency-Key": "dagster-completion:engine-run-001",
        "X-Tenant-Id": "aurora_auto",
        "X-Project-Id": "sales_qa",
        "X-Trace-Id": "trace-dagster-gate-success",
        "X-Auris-Key-Id": "dagster-v1",
        "X-Auris-Timestamp": now.isoformat(),
        "X-Auris-Nonce": "nonce-dagster-gate-001",
        "X-Auris-Source": "dagster",
        "X-Auris-Signature-Mode": "hmac-sha256",
    }
    body_sha256 = hashlib.sha256(body).hexdigest()
    message = "\n".join(
        [
            "auris-completion-v1",
            "POST",
            path,
            "",
            "aurora_auto",
            "sales_qa",
            headers["Idempotency-Key"],
            headers["X-Auris-Timestamp"],
            headers["X-Auris-Nonce"],
            headers["X-Auris-Key-Id"],
            "dagster",
            body_sha256,
        ]
    )
    secret = "unit-dagster-gate-secret-at-least-32-bytes"
    headers["X-Auris-Signature"] = (
        "sha256=" + hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    )
    keyring: dict[str, object] = {
        "dagster-v1": {
            "secret": secret,
            "allowed_sources": ["dagster"],
            "allowed_scopes": [{"tenant_id": "aurora_auto", "project_id": "sales_qa"}],
        }
    }
    return path, headers, body, keyring, now


def test_real_dagster_callback_gate_accepts_valid_scoped_signature_once() -> None:
    module = _load_script("verify_real_dagster_callback_server.py")
    path, headers, body, keyring, now = _signed_request()
    seen_nonces: set[str] = set()

    payload = module.verify_completion_request(
        path=path,
        headers=headers,
        body=body,
        keyring=keyring,
        now=now,
        seen_nonces=seen_nonces,
    )

    assert payload["status"] == "success"
    assert payload["external_id"] == "engine-run-001"
    assert seen_nonces == {"nonce-dagster-gate-001"}
    with pytest.raises(module.GateRequestError, match="replay"):
        module.verify_completion_request(
            path=path,
            headers=headers,
            body=body,
            keyring=keyring,
            now=now,
            seen_nonces=seen_nonces,
        )


def test_real_dagster_callback_gate_rejects_cross_scope_signature() -> None:
    module = _load_script("verify_real_dagster_callback_server.py")
    path, headers, body, keyring, now = _signed_request()
    headers["X-Project-Id"] = "another-project"

    with pytest.raises(module.GateRequestError, match="scope"):
        module.verify_completion_request(
            path=path,
            headers=headers,
            body=body,
            keyring=keyring,
            now=now,
            seen_nonces=set(),
        )


def test_real_dagster_shell_gate_isolated_and_never_starts_protocol_fake() -> None:
    source = (ROOT / "scripts" / "verify_real_dagster.sh").read_text(encoding="utf-8")

    assert "fake_dagster_graphql_server" not in source
    assert "--delay-match" not in source
    assert "production/compose.yaml" in source
    assert "production/tests/dagster-gate.compose.yaml" in source
    assert "auris-dagster-gate-" in source
    assert "--project-name" in source
    assert "--parallel=1" in source
    assert "START_ORDER=(" in source
    assert "dagster-gate-db-bootstrap" in source
    assert "down --volumes --remove-orphans" in source
    assert "${ROOT}/build/tmp" in source
    assert "${TMPDIR:-/tmp}/auris-dagster-gate" not in source
    assert 'export APP_ENV="ci"' in source
    assert 'SOURCE_COMMIT="$(git -C "${ROOT}" rev-parse --verify HEAD^{commit})"' in source
    assert '--source-commit "${SOURCE_COMMIT}"' in source


def test_real_dagster_driver_accepts_only_exact_source_commit() -> None:
    module = _load_script("verify_real_dagster.py")

    assert module.validate_source_commit("a" * 40) == "a" * 40
    assert module.validate_source_commit("B" * 64) == "b" * 64
    for invalid in ("", "abc", "g" * 40, "a" * 39, "a" * 41):
        with pytest.raises(module.GateFailure, match="source commit"):
            module.validate_source_commit(invalid)


def test_real_dagster_driver_rejects_oversized_graphql_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("verify_real_dagster.py")

    class OversizedResponse:
        def __enter__(self) -> OversizedResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            assert size == module.MAX_GRAPHQL_RESPONSE_BYTES + 1
            return b"x" * size

    monkeypatch.setattr(module, "urlopen", lambda *_args, **_kwargs: OversizedResponse())

    with pytest.raises(module.GateFailure, match="request failed"):
        module.graphql_request("http://dagster.example.test/graphql", "query { version }")


def test_real_dagster_compose_overlay_uses_real_services_and_loopback_ports() -> None:
    path = ROOT / "production" / "tests" / "dagster-gate.compose.yaml"
    source = path.read_text(encoding="utf-8")
    document = yaml.load(source, Loader=_ComposeLoader)
    services = document["services"]

    assert "fake_dagster_graphql_server" not in source
    assert services["dagster-code"]["environment"]["AURIS_BFF_INTERNAL_URL"] == (
        "http://dagster-gate-callback:8080"
    )
    assert (
        services["dagster-code"]["depends_on"]["dagster-gate-callback"]["condition"]
        == "service_healthy"
    )
    assert "depends_on: !override" in source
    assert set(services["dagster-code"]["depends_on"]) == {
        "dagster-gate-callback",
        "dagster-gate-db-bootstrap",
    }
    bootstrap = services["dagster-gate-db-bootstrap"]
    assert bootstrap["image"] == "${MYSQL_IMAGE:-mysql:8.4.5}"
    assert bootstrap["secrets"] == []
    assert services["dagster-webserver"]["ports"] == ["127.0.0.1:${AURIS_DAGSTER_GATE_PORT}:3000"]
    callback = services["dagster-gate-callback"]
    assert callback["build"]["dockerfile"] == ("production/tests/dagster-gate-callback.Dockerfile")
    assert callback["ports"] == ["127.0.0.1:${AURIS_DAGSTER_GATE_CALLBACK_PORT}:8080"]
    assert callback["secrets"] == []
    assert "dagster_gate_secrets:/run/secrets:ro" in callback["volumes"]
    assert services["dagster-gate-secrets-init"]["network_mode"] == "none"
    for service_name in ("mysql", "dagster-code", "dagster-webserver", "dagster-daemon"):
        assert services[service_name]["secrets"] == []
        assert "dagster_gate_secrets:/run/secrets:ro" in services[service_name]["volumes"]
    callback_source = (ROOT / "scripts" / "verify_real_dagster_callback_server.py").read_text(
        encoding="utf-8"
    )
    assert "delay_match" not in callback_source


def test_real_dagster_driver_requires_exact_workspace_repository_and_job() -> None:
    module = _load_script("verify_real_dagster.py")
    response = {
        "data": {
            "version": "1.13.1",
            "repositoriesOrError": {
                "__typename": "RepositoryConnection",
                "nodes": [
                    {
                        "name": "__repository__",
                        "location": {"name": "auris_flow_defs"},
                        "pipelines": [{"name": "auris_flow_generic_job"}],
                    }
                ],
            },
        }
    }

    proof = module.validate_workspace(
        response,
        location_name="auris_flow_defs",
        repository_name="__repository__",
        job_name="auris_flow_generic_job",
    )

    assert proof == {
        "dagster_version": "1.13.1",
        "location_name": "auris_flow_defs",
        "repository_name": "__repository__",
        "job_name": "auris_flow_generic_job",
    }
    response["data"]["repositoriesOrError"]["nodes"][0]["pipelines"] = []
    with pytest.raises(module.GateFailure, match="job"):
        module.validate_workspace(
            response,
            location_name="auris_flow_defs",
            repository_name="__repository__",
            job_name="auris_flow_generic_job",
        )
    response["data"]["repositoriesOrError"]["nodes"][0]["pipelines"] = None
    with pytest.raises(module.GateFailure, match="job"):
        module.validate_workspace(
            response,
            location_name="auris_flow_defs",
            repository_name="__repository__",
            job_name="auris_flow_generic_job",
        )


def test_real_dagster_driver_requires_healthy_required_daemons() -> None:
    module = _load_script("verify_real_dagster.py")
    response = {
        "data": {
            "instance": {
                "daemonHealth": {
                    "allDaemonStatuses": [
                        {
                            "daemonType": "SENSOR",
                            "required": True,
                            "healthy": True,
                        },
                        {
                            "daemonType": "SCHEDULER",
                            "required": True,
                            "healthy": True,
                        },
                        {
                            "daemonType": "OPTIONAL_TEST_DAEMON",
                            "required": False,
                            "healthy": None,
                        },
                    ]
                }
            }
        }
    }

    assert module.validate_daemon_health(response) == [
        {"daemon_type": "OPTIONAL_TEST_DAEMON", "required": False, "healthy": None},
        {"daemon_type": "SCHEDULER", "required": True, "healthy": True},
        {"daemon_type": "SENSOR", "required": True, "healthy": True},
    ]
    response["data"]["instance"]["daemonHealth"]["allDaemonStatuses"][0]["healthy"] = False
    with pytest.raises(module.GateFailure, match="SENSOR"):
        module.validate_daemon_health(response)


def test_real_dagster_driver_rejects_any_cancel_completion_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script("verify_real_dagster.py")
    monkeypatch.setattr(
        module,
        "callback_receipts",
        lambda _url: [
            {
                "run_id": "dagster-gate-cancel",
                "status": "success",
                "completion_receipt_id": "dagster:already-accepted",
            }
        ],
    )

    with pytest.raises(module.GateFailure, match="unexpected completion receipt"):
        module.assert_no_completion_receipt(
            "http://127.0.0.1:8080",
            business_run_id="dagster-gate-cancel",
            observation_seconds=0,
        )


def test_real_dagster_driver_binds_success_receipt_to_engine_and_scope() -> None:
    module = _load_script("verify_real_dagster.py")
    receipt = {
        "adapter": "dagster",
        "source": "dagster",
        "run_id": "task-run-001",
        "status": "success",
        "external_id": "dagster-run-001",
        "completion_receipt_id": "dagster:dagster-run-001",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace-task-run-001",
        "key_id": "dagster-v1",
        "body_sha256": "a" * 64,
        "result_ref": {"auris_run_id": "task-run-001"},
        "metrics": {"control_plane_acknowledged": 1},
    }

    assert (
        module.validate_completion_receipt(
            receipt,
            business_run_id="task-run-001",
            dagster_run_id="dagster-run-001",
            trace_id="trace-task-run-001",
            completion_status="success",
        )
        is receipt
    )
    receipt["external_id"] = "wrong-engine-run"
    with pytest.raises(module.GateFailure, match="binding"):
        module.validate_completion_receipt(
            receipt,
            business_run_id="task-run-001",
            dagster_run_id="dagster-run-001",
            trace_id="trace-task-run-001",
            completion_status="success",
        )


def test_real_dagster_gate_payload_matches_outbox_and_cannot_select_job() -> None:
    module = _load_script("verify_real_dagster.py")

    payload = module.run_payload(
        scenario="success",
        suffix="unit",
        execution_mode="caller-selected-mode",
    )

    assert payload["task_version_id"] == "task_version_v3_2_1"
    assert payload["job_name"] == "caller-selected-job-must-be-ignored"
    assert payload["dagster_run_draft"]["job_name"] == "draft-job-must-be-ignored"
    assert payload["run_config"]["execution"] == {"mode": "caller-selected-mode"}
    assert payload["run_config"]["resources"]


def test_release_gate_runs_real_dagster_and_rejects_skip_before_work() -> None:
    source = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    assert "bash scripts/verify_real_dagster.sh" in source
    assert "AURIS_SKIP_REAL_DAGSTER=1 is not allowed" in source
    result = subprocess.run(
        ["bash", "scripts/verify_release.sh"],
        cwd=ROOT,
        env={**os.environ, "AURIS_SKIP_REAL_DAGSTER": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "AURIS_SKIP_REAL_DAGSTER=1 is not allowed" in result.stderr


def test_readiness_and_readme_distinguish_real_dagster_from_protocol_fake() -> None:
    readiness = (ROOT / "scripts" / "check_platform_readiness.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for path in (
        "scripts/verify_real_dagster.sh",
        "scripts/verify_real_dagster.py",
        "scripts/verify_real_dagster_callback_server.py",
        "production/tests/dagster-gate.compose.yaml",
    ):
        assert path in readiness
    assert "bash scripts/verify_real_dagster.sh" in readiness
    assert "AURIS_SKIP_REAL_DAGSTER=1 is not allowed" in readiness
    assert "scripts/fake_dagster_graphql_server.py" in readme
    assert "bash scripts/verify_real_dagster.sh" in readme
    assert "SAFE_TERMINATE" in readme
    assert "Dagster 引擎层" in readme


def test_local_process_dagster_proof_is_explicitly_non_release() -> None:
    source = (ROOT / "scripts" / "verify_real_dagster_local_process.sh").read_text(encoding="utf-8")
    release = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")

    assert "fake_dagster_graphql_server" not in source
    assert "dagster dev" in source
    assert "--execution-environment local-process" in source
    assert "real-dagster-local-process.json" in source
    assert "real-dagster-gate.json" not in source
    assert "verify_real_dagster_local_process.sh" not in release
