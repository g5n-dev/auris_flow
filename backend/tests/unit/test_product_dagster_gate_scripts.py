from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from app.services import adapters

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


def _load_driver() -> ModuleType:
    path = ROOT / "scripts" / "verify_product_dagster_path.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_payload(*, status: str = "success") -> dict[str, object]:
    return {
        "run_id": "task_run_gate_success",
        "run_type": "task_run",
        "status": status,
        "status_version": 5,
        "trace_id": "trace_gate_success",
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "dispatch": {
            "adapter": "dagster",
            "operation": "run_request",
            "status": "success",
            "details": {
                "mode": "real",
                "external_run_id": "dagster-run-success",
                "response_typename": "LaunchRunSuccess",
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "trace_id": "trace_gate_success",
            },
        },
        "status_history": [
            {"from": "pending", "to": "running", "reason": "outbox_dispatch_started"},
            {"from": "running", "to": "submitted", "reason": "outbox_dispatch_submitted"},
            {"from": "submitted", "to": status, "reason": "completion_receipt"},
        ],
    }


def _confirmed_outbox_event() -> SimpleNamespace:
    return SimpleNamespace(
        event_id=1,
        event_type="task_run.requested",
        aggregate_id="task_run_gate_success",
        tenant_id="aurora_auto",
        project_id="sales_qa",
        status="processed",
        delivery_state="confirmed",
        processed_at=datetime.now(UTC),
        attempt_count=1,
        lease_generation=1,
        dispatch_request_sha256="a" * 64,
        last_error=None,
        dispatch_idempotency_key="outbox:1",
        payload={
            "trace_id": "trace_gate_success",
            "adapter_dispatch": {
                "adapter": "dagster",
                "operation": "run_request",
                "status": "success",
                "details": {
                    "mode": "real",
                    "external_run_id": "dagster-run-success",
                },
            },
        },
    )


def test_product_gate_validates_real_scoped_dispatch_and_terminal_monotonicity() -> None:
    module = _load_driver()

    proof = module.validate_run_projection(
        _run_payload(),
        expected_status="success",
        expected_scope=("aurora_auto", "sales_qa"),
        expected_trace_id="trace_gate_success",
    )

    assert proof == {
        "run_id": "task_run_gate_success",
        "status": "success",
        "status_version": 5,
        "trace_id": "trace_gate_success",
        "dagster_run_id": "dagster-run-success",
        "adapter_mode": "real",
    }

    forged = _run_payload()
    forged["dispatch"]["details"]["tenant_id"] = "other"  # type: ignore[index]
    with pytest.raises(module.GateFailure, match="scope"):
        module.validate_run_projection(
            forged,
            expected_status="success",
            expected_scope=("aurora_auto", "sales_qa"),
            expected_trace_id="trace_gate_success",
        )

    forged_top_level = _run_payload()
    forged_top_level["project_id"] = "outside-project"
    with pytest.raises(module.GateFailure, match="scope"):
        module.validate_run_projection(
            forged_top_level,
            expected_status="success",
            expected_scope=("aurora_auto", "sales_qa"),
            expected_trace_id="trace_gate_success",
        )

    regressed = _run_payload()
    regressed["status_history"].append(  # type: ignore[union-attr]
        {"from": "success", "to": "running", "reason": "invalid regression"}
    )
    with pytest.raises(module.GateFailure, match="terminal"):
        module.validate_run_projection(
            regressed,
            expected_status="success",
            expected_scope=("aurora_auto", "sales_qa"),
            expected_trace_id="trace_gate_success",
        )


def test_product_gate_requires_confirmed_outbox_delivery_and_real_dispatch() -> None:
    module = _load_driver()
    event = _confirmed_outbox_event()

    confirmed = module._require_confirmed_event(
        [event],
        event_type="task_run.requested",
        aggregate_id="task_run_gate_success",
        trace_id="trace_gate_success",
    )
    details = module._require_real_event_dispatch(
        confirmed,
        operation="run_request",
        external_run_id="dagster-run-success",
    )

    assert details["mode"] == "real"

    event.payload["adapter_dispatch"]["details"]["mode"] = "local"
    with pytest.raises(module.GateFailure, match="real Dagster"):
        module._require_real_event_dispatch(
            event,
            operation="run_request",
            external_run_id="dagster-run-success",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_count", 0),
        ("attempt_count", True),
        ("lease_generation", 0),
        ("lease_generation", None),
        ("dispatch_request_sha256", "not-a-sha256"),
        ("last_error", "delivery failed"),
    ],
)
def test_product_gate_rejects_incomplete_outbox_delivery_proof(
    field: str,
    value: object,
) -> None:
    module = _load_driver()
    event = _confirmed_outbox_event()
    setattr(event, field, value)

    with pytest.raises(module.GateFailure, match="delivery proof"):
        module._require_confirmed_event(
            [event],
            event_type="task_run.requested",
            aggregate_id="task_run_gate_success",
            trace_id="trace_gate_success",
        )


def test_product_gate_builds_commit_bound_sanitized_evidence() -> None:
    module = _load_driver()
    source_commit = "a" * 40

    evidence = module.build_evidence(
        source_commit=source_commit,
        success={
            "run_id": "task_run_success",
            "dagster_run_id": "dagster-success",
            "trace_id": "trace-success",
            "status": "success",
            "status_version": 5,
            "status_sync": "SUCCESS",
            "signed_completion": True,
            "outbox_confirmed": True,
        },
        cancellation={
            "run_id": "task_run_cancel",
            "dagster_run_id": "dagster-cancel",
            "trace_id": "trace-cancel",
            "status": "cancelled",
            "status_version": 5,
            "terminate_policy": "SAFE_TERMINATE",
            "outbox_confirmed": True,
        },
    )

    assert evidence["schema_version"] == "auris.product-dagster-gate.v1"
    assert evidence["status"] == "ok"
    assert evidence["source_commit"] == source_commit
    assert evidence["execution_environment"] == "compose"
    assert evidence["adapter_mode"] == "real"
    encoded = json.dumps(evidence, sort_keys=True)
    assert "/Users/" not in encoded
    assert "secret" not in encoded.lower()

    injected = module.build_evidence(
        source_commit=source_commit,
        success={
            **evidence["scenarios"]["success"],
            "internal_database_url": "mysql://user:secret@database/gate",
        },
        cancellation={
            **evidence["scenarios"]["cancellation"],
            "local_path": "/srv/private/worktree",
        },
    )
    injected_json = json.dumps(injected, sort_keys=True)
    assert "internal_database_url" not in injected_json
    assert "mysql://" not in injected_json
    assert "local_path" not in injected_json
    assert "/srv/private/" not in injected_json

    with pytest.raises(module.GateFailure, match="source commit"):
        module.build_evidence(
            source_commit="not-a-commit",
            success=evidence["scenarios"]["success"],
            cancellation=evidence["scenarios"]["cancellation"],
        )


def test_product_gate_sanitizes_unexpected_internal_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    module = _load_driver()
    canary = "internal-product-gate-password-canary-never-print"
    artifact = tmp_path / "product-dagster-gate.json"
    monkeypatch.setattr(
        module,
        "run_gate",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(canary)),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_product_dagster_path.py",
            "--base-url",
            "http://bff:8000",
            "--source-commit",
            "a" * 40,
            "--artifact",
            str(artifact),
            "--run-suffix",
            "unit",
        ],
    )

    assert module.main() == 1
    captured = capsys.readouterr()
    assert "internal verifier failure" in captured.err
    assert canary not in captured.err
    assert not artifact.exists()


def test_product_gate_compose_uses_bff_worker_and_real_dagster_without_fake() -> None:
    path = ROOT / "production" / "tests" / "dagster-product-gate.compose.yaml"
    source = path.read_text(encoding="utf-8")
    document = yaml.load(source, Loader=_ComposeLoader)
    services = document["services"]

    assert "fake_dagster_graphql_server" not in source
    assert services["dagster-code"]["environment"]["AURIS_BFF_INTERNAL_URL"] == ("http://bff:8000")
    assert services["bff"]["environment"]["AURIS_DAGSTER_ADAPTER"] == "real"
    assert services["worker"]["environment"]["AURIS_DAGSTER_ADAPTER"] == "real"
    assert services["worker"]["environment"]["AURIS_DAGSTER_EXECUTION_MODE"] == ("ci-cancel-delay")
    assert services["bff"]["environment"]["APP_ENV"] == "ci"
    assert services["worker"]["environment"]["APP_ENV"] == "ci"
    assert (
        "../doc/backend-spec/seed-fixture-v0.1.json:"
        "/doc/backend-spec/seed-fixture-v0.1.json:ro"
        in services["dagster-product-gate-seed"]["volumes"]
    )
    assert services["bff"]["ports"] == ["127.0.0.1:${AURIS_PRODUCT_DAGSTER_GATE_BFF_PORT}:8000"]
    verifier = services["dagster-product-gate-verifier"]
    assert verifier["read_only"] is True
    assert verifier["cap_drop"] == ["ALL"]
    assert verifier["environment"]["AURIS_PRODUCT_GATE_SOURCE_COMMIT"]
    assert verifier["environment"]["PYTHONPATH"] == "/app"
    assert "product_dagster_gate_artifacts:/artifacts" not in verifier["volumes"]


def test_product_gate_shell_is_clean_tree_commit_bound_and_fail_closed() -> None:
    source = (ROOT / "scripts" / "verify_product_dagster_path.sh").read_text(encoding="utf-8")

    assert "fake_dagster_graphql_server" not in source
    assert "production/compose.yaml" in source
    assert "production/tests/dagster-gate.compose.yaml" in source
    assert "production/tests/dagster-product-gate.compose.yaml" in source
    assert 'git -C "${ROOT}" diff --quiet --' in source
    assert 'git -C "${ROOT}" diff --cached --quiet --' in source
    assert 'git -C "${ROOT}" ls-files --others --exclude-standard' in source
    assert 'git -C "${ROOT}" rev-parse --verify HEAD' in source
    assert "AURIS_PRODUCT_GATE_SOURCE_COMMIT" in source
    assert "build/release-evidence/product-dagster-gate.json" in source
    assert "down --volumes --remove-orphans" in source
    assert "AURIS_SKIP_PRODUCT_DAGSTER_GATE" in source
    assert 'DEADLINE_RUNNER="${ROOT}/scripts/run_with_deadline.py"' in source
    assert 'compose_with_deadline "${COMPOSE_WAIT_DEADLINE}" "start ${service}"' in source
    assert 'compose_with_deadline "${BUILD_TIMEOUT}"' in source
    assert 'compose_with_deadline "${RUN_COMMAND_DEADLINE}"' in source
    assert '"${COMPOSE[@]}" up --detach --no-build --wait' not in source


def test_product_gate_shell_rejects_skip_and_dirty_source_before_docker(
    tmp_path: Path,
) -> None:
    skipped = subprocess.run(
        ["bash", "scripts/verify_product_dagster_path.sh"],
        cwd=ROOT,
        env={**os.environ, "AURIS_SKIP_PRODUCT_DAGSTER_GATE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert skipped.returncode == 2
    assert "not allowed" in skipped.stderr

    marker = ROOT / "backend" / "tests" / f"product-gate-dirty-{tmp_path.name}.fixture"
    marker.write_text("force an isolated untracked-source rejection\n", encoding="utf-8")
    try:
        dirty = subprocess.run(
            ["bash", "scripts/verify_product_dagster_path.sh"],
            cwd=ROOT,
            env={
                key: value
                for key, value in os.environ.items()
                if key != "AURIS_SKIP_PRODUCT_DAGSTER_GATE"
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        marker.unlink(missing_ok=True)
    assert dirty.returncode == 2
    assert any(
        message in dirty.stderr
        for message in (
            "clean worktree",
            "empty Git index",
            "refuses untracked release inputs",
        )
    )
    assert "Docker Engine is required" not in dirty.stderr


def test_product_gate_shell_rejects_result_path_traversal_before_docker() -> None:
    traversal = subprocess.run(
        ["bash", "scripts/verify_product_dagster_path.sh"],
        cwd=ROOT,
        env={
            **os.environ,
            "AURIS_PRODUCT_DAGSTER_GATE_RESULT": str(
                ROOT / "build" / "release-evidence" / ".." / "escaped.json"
            ),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert traversal.returncode == 2
    assert "must stay under build/release-evidence" in traversal.stderr
    assert "Docker Engine is required" not in traversal.stderr


def test_default_real_dagster_client_accepts_ci_only_gate_mode_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "ci")
    monkeypatch.setenv("AURIS_DAGSTER_ADAPTER", "real")
    monkeypatch.setenv("AURIS_DAGSTER_EXECUTION_MODE", "ci-cancel-delay")

    client = adapters._default_dagster_client()

    assert isinstance(client, adapters.RealDagsterClient)
    assert client.execution_mode == "ci-cancel-delay"


def test_default_real_dagster_client_rejects_ci_gate_mode_outside_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("AURIS_DAGSTER_ADAPTER", "real")
    monkeypatch.setenv("AURIS_DAGSTER_EXECUTION_MODE", "ci-cancel-delay")

    with pytest.raises(ValueError, match="CI-only"):
        adapters._default_dagster_client()
