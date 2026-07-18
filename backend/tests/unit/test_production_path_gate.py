from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
BASE_COMPOSE = ROOT / "production" / "compose.yaml"
GATE_COMPOSE = ROOT / "production" / "tests" / "production-path-gate.compose.yaml"


def _load_gate() -> ModuleType:
    path = ROOT / "scripts" / "verify_production_path_gate.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ready_gate_document() -> dict[str, object]:
    return {
        "x-auris-production-path-gate": {
            "schema_version": "auris.production-path-gate-contract.v1",
            "status": "ready",
            "runtime_driver": "scripts/verify_production_path_runtime.py",
            "source_compose": "production/compose.yaml",
            "required_external_stubs": [
                "production-gate-embedding",
                "production-gate-callback",
            ],
        },
        "services": {
            "bff": {
                "environment": {
                    "APP_ENV": "prod",
                    "AUTH_PROVIDER": "oidc",
                    "ALLOW_DEV_AUTH": "false",
                    "AURIS_DAGSTER_ADAPTER": "real",
                    "AURIS_OBJECT_STORAGE_ADAPTER": "real",
                    "AURIS_QDRANT_ADAPTER": "real",
                    "AURIS_EXTERNAL_CALLBACK_ADAPTER": "real",
                    "AURIS_EMBEDDING_PROVIDER": "http",
                    "EMBEDDING_ENDPOINT": (
                        "https://embedding.production-gate.invalid/v1/embeddings"
                    ),
                    "EXTERNAL_CALLBACK_URL": (
                        "https://callback.production-gate.invalid/callbacks/platform"
                    ),
                    "OIDC_ISSUER": ("https://auris.production-gate.invalid/realms/auris-flow"),
                    "OTEL_ENABLED": "true",
                }
            },
            "worker": {
                "environment": {
                    "APP_ENV": "prod",
                    "AUTH_PROVIDER": "oidc",
                    "ALLOW_DEV_AUTH": "false",
                    "AURIS_DAGSTER_ADAPTER": "real",
                    "AURIS_OBJECT_STORAGE_ADAPTER": "real",
                    "AURIS_QDRANT_ADAPTER": "real",
                    "AURIS_EXTERNAL_CALLBACK_ADAPTER": "real",
                    "AURIS_EMBEDDING_PROVIDER": "http",
                    "EMBEDDING_ENDPOINT": (
                        "https://embedding.production-gate.invalid/v1/embeddings"
                    ),
                    "EXTERNAL_CALLBACK_URL": (
                        "https://callback.production-gate.invalid/callbacks/platform"
                    ),
                    "OIDC_ISSUER": ("https://auris.production-gate.invalid/realms/auris-flow"),
                    "OTEL_ENABLED": "true",
                }
            },
            "production-gate-embedding": {
                "read_only": True,
                "cap_drop": ["ALL"],
                "networks": ["internal"],
            },
            "production-gate-callback": {
                "read_only": True,
                "cap_drop": ["ALL"],
                "networks": ["production-gate-callback"],
            },
            "production-path-verifier": {
                "read_only": True,
                "cap_drop": ["ALL"],
                "networks": ["internal", "production-gate-callback"],
            },
        },
        "networks": {
            "production-gate-callback": {
                "internal": True,
                "ipam": {"config": [{"subnet": "11.250.0.0/29"}]},
            }
        },
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_evidence() -> dict[str, object]:
    trace_id = "trace_production_path_gate_001"
    proof = {"mode": "real", "trace_id": trace_id}
    return {
        "schema_version": "auris.production-path-gate.v1",
        "status": "ok",
        "source_commit": "a" * 40,
        "execution_environment": "production-compose",
        "producer": "scripts/verify_production_path_runtime.py",
        "compose": {
            "base": "production/compose.yaml",
            "overlay": "production/tests/production-path-gate.compose.yaml",
            "base_sha256": _sha256(BASE_COMPOSE),
            "overlay_sha256": _sha256(GATE_COMPOSE),
            "rendered_config_sha256": "b" * 64,
            "services": [
                "bff",
                "worker",
                "mysql",
                "redis",
                "minio",
                "qdrant",
                "keycloak",
                "dagster-code",
                "dagster-webserver",
                "dagster-daemon",
                "otel-collector",
                "tempo",
                "edge",
                "production-gate-embedding",
                "production-gate-callback",
                "production-path-verifier",
            ],
        },
        "identity": {
            "provider": "oidc",
            "grant_type": "authorization_code",
            "pkce_method": "S256",
            "issuer_scheme": "https",
            "discovery_verified": True,
            "jwks_verified": True,
            "code_exchange_verified": True,
            "browser_session_verified": True,
            "dev_auth_enabled": False,
            "trace_id": trace_id,
        },
        "adapters": {
            "dagster": {
                **proof,
                "submitted": True,
                "signed_completion_verified": True,
            },
            "object_storage": {
                **proof,
                "provider": "minio",
                "object_verified": True,
            },
            "qdrant": {
                **proof,
                "embedding_provider": "http",
                "embedding_transport": "https",
                "semantic_embedding": True,
                "point_verified": True,
                "recall_verified": True,
            },
            "external_callback": {
                **proof,
                "transport": "https",
                "signature_mode": "hmac-sha256-v2",
                "signature_verified": True,
                "replay_rejected": True,
            },
        },
        "observability": {
            "otel_enabled": True,
            "collector_export_verified": True,
            "trace_id": trace_id,
            "services": ["auris-flow-bff", "auris-flow-worker", "auris-flow-dagster-code"],
        },
        "trace": {
            "trace_id": trace_id,
            "linked_components": [
                "oidc",
                "bff",
                "mysql",
                "redis",
                "worker",
                "dagster",
                "object_storage",
                "qdrant",
                "external_callback",
                "otel",
            ],
        },
    }


def test_preflight_contract_accepts_only_production_modes() -> None:
    gate = _load_gate()

    assert gate.validate_gate_compose(_ready_gate_document()) == []

    weakened = _ready_gate_document()
    bff = weakened["services"]["bff"]["environment"]  # type: ignore[index]
    bff.update(  # type: ignore[union-attr]
        {
            "AUTH_PROVIDER": "dev",
            "ALLOW_DEV_AUTH": "true",
            "AURIS_QDRANT_ADAPTER": "local",
            "AURIS_EMBEDDING_PROVIDER": "deterministic_test",
            "OTEL_ENABLED": "false",
        }
    )

    errors = gate.validate_gate_compose(weakened)

    assert any("AUTH_PROVIDER must be oidc" in error for error in errors)
    assert any("ALLOW_DEV_AUTH must be false" in error for error in errors)
    assert any("AURIS_QDRANT_ADAPTER must be real" in error for error in errors)
    assert any("AURIS_EMBEDDING_PROVIDER must be http" in error for error in errors)
    assert any("OTEL_ENABLED must be true" in error for error in errors)


def test_checked_in_gate_contract_is_explicitly_blocked_and_not_evidence() -> None:
    gate = _load_gate()
    document = yaml.safe_load(GATE_COMPOSE.read_text(encoding="utf-8"))

    errors = gate.validate_gate_compose(document)

    assert any("contract status must be ready" in error for error in errors)
    assert any("OIDC Authorization Code + PKCE" in error for error in errors)
    assert any("HTTPS semantic embedding" in error for error in errors)
    assert any("signed HTTPS external callback" in error for error in errors)
    assert any("cross-service OTel trace" in error for error in errors)


def test_evidence_validator_rejects_constructed_full_chain_until_runtime_is_activated() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()

    activation_errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )

    assert any("contract status is not ready" in error for error in activation_errors)
    assert any("runtime driver is not implemented" in error for error in activation_errors)
    assert any(
        "raw runtime proof binding is not implemented" in error for error in activation_errors
    )

    for mutation, expected in (
        (("identity", "dev_auth_enabled", True), "dev auth"),
        (("adapters", "qdrant", "embedding_provider", "deterministic_test"), "HTTP embedding"),
        (("adapters", "external_callback", "signature_verified", False), "signature"),
        (("observability", "collector_export_verified", False), "collector"),
    ):
        forged = copy.deepcopy(evidence)
        *parents, field, value = mutation
        target = forged
        for parent in parents:
            target = target[parent]  # type: ignore[index,assignment]
        target[field] = value  # type: ignore[index]
        errors = gate.validate_evidence(forged, root=ROOT, expected_commit="a" * 40)
        assert any(expected in error for error in errors), errors

    split_trace = copy.deepcopy(evidence)
    split_trace["adapters"]["object_storage"]["trace_id"] = "trace_other"  # type: ignore[index]
    errors = gate.validate_evidence(split_trace, root=ROOT, expected_commit="a" * 40)
    assert any("same trace_id" in error for error in errors)


def test_ready_name_and_driver_file_still_cannot_bypass_raw_proof_binding(
    tmp_path: Path,
) -> None:
    gate = _load_gate()
    (tmp_path / "production" / "tests").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "production" / "compose.yaml").write_bytes(BASE_COMPOSE.read_bytes())
    (tmp_path / "production" / "tests" / "production-path-gate.compose.yaml").write_text(
        yaml.safe_dump(_ready_gate_document(), sort_keys=False),
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "verify_production_path_runtime.py").write_text(
        "# name-only fixture; it cannot bind raw proofs\n",
        encoding="utf-8",
    )

    errors = gate.validate_evidence(
        _valid_evidence(),
        root=tmp_path,
        expected_commit="a" * 40,
    )

    assert not any("contract status is not ready" in error for error in errors)
    assert not any("runtime driver is not implemented" in error for error in errors)
    assert any("raw runtime proof binding is not implemented" in error for error in errors)


def test_evidence_validator_rejects_legacy_split_gate_artifacts() -> None:
    gate = _load_gate()
    legacy = {
        "schema_version": "auris.product-dagster-gate.v1",
        "status": "ok",
        "source_commit": "a" * 40,
        "execution_environment": "compose",
        "adapter_mode": "real",
    }

    errors = gate.validate_evidence(legacy, root=ROOT, expected_commit="a" * 40)

    assert any("schema_version" in error for error in errors)
    assert any("single production Compose" in error for error in errors)


def test_validators_fail_closed_on_non_string_inventories() -> None:
    gate = _load_gate()
    malformed_contract = _ready_gate_document()
    malformed_contract["x-auris-production-path-gate"][  # type: ignore[index]
        "required_external_stubs"
    ] = [{}]

    contract_errors = gate.validate_gate_compose(malformed_contract)

    assert any("HTTPS embedding and callback" in error for error in contract_errors)

    malformed_evidence = _valid_evidence()
    malformed_evidence["compose"]["services"] = [{}]  # type: ignore[index]
    malformed_evidence["trace"]["linked_components"] = [{}]  # type: ignore[index]
    malformed_evidence["observability"]["services"] = [{}]  # type: ignore[index]

    evidence_errors = gate.validate_evidence(
        malformed_evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )

    assert any("missing services" in error for error in evidence_errors)
    assert any("missing components" in error for error in evidence_errors)
    assert any("BFF, Worker and Dagster" in error for error in evidence_errors)


def test_shell_entrypoint_is_fail_closed_and_not_wired_as_release_success() -> None:
    shell_path = ROOT / "scripts" / "verify_production_path.sh"
    source = shell_path.read_text(encoding="utf-8")

    assert "AURIS_SKIP_PRODUCTION_PATH_GATE" in source
    assert "production/compose.yaml" in source
    assert "production/tests/production-path-gate.compose.yaml" in source
    assert "verify_production_path_runtime.py" in source
    assert "verify_production_path_gate.py" in source
    assert "verify_production_compose.py" in source
    assert "verify_release.sh" not in source
    assert 'if [ -L "${ROOT}/build" ]' in source
    assert 'if [ -L "${EVIDENCE_DIR}" ]' in source
    assert "mkdir -p" not in source
    assert 'rm -f -- "${ARTIFACT}"' not in source
    assert "fake_dagster_graphql_server" not in source
    assert "deterministic_test" not in source

    skipped = subprocess.run(
        ["bash", str(shell_path)],
        cwd=ROOT,
        env={**os.environ, "AURIS_SKIP_PRODUCTION_PATH_GATE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert skipped.returncode == 2
    assert "not allowed" in skipped.stderr

    release_source = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
    assert "bash scripts/verify_production_path.sh" in release_source
    assert release_source.index("bash scripts/verify_production_path.sh") < release_source.index(
        "scripts/generate_supply_chain_evidence.py"
    )


@pytest.mark.parametrize("target", ["build", "build/release-evidence"])
@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_shell_rejects_unsafe_evidence_parent_before_preflight(
    tmp_path: Path,
    target: str,
    kind: str,
) -> None:
    repository = tmp_path / "production-path-fixture"
    (repository / "scripts").mkdir(parents=True)
    (repository / "production" / "tests").mkdir(parents=True)
    for relative in (
        "scripts/verify_production_path.sh",
        "scripts/verify_production_path_gate.py",
        "production/tests/production-path-gate.compose.yaml",
    ):
        destination = repository / relative
        destination.write_bytes((ROOT / relative).read_bytes())
        destination.chmod((ROOT / relative).stat().st_mode)
    (repository / "production" / "compose.yaml").write_text(
        "services: {}\n",
        encoding="utf-8",
    )
    unsafe = repository / target
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    if kind == "symlink":
        outside = tmp_path / f"outside-{target.replace('/', '-')}"
        outside.mkdir()
        unsafe.symlink_to(outside, target_is_directory=True)
    else:
        unsafe.write_text("not a directory\n", encoding="utf-8")

    completed = subprocess.run(
        ["bash", "scripts/verify_production_path.sh"],
        cwd=repository,
        env={key: value for key, value in os.environ.items() if key != "PYTHON"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "must be a real directory" in completed.stderr
    assert "blocked capability" not in completed.stderr


def test_preflight_cli_reports_blockers_before_runtime_or_docker() -> None:
    completed = subprocess.run(
        [
            str(ROOT / "backend" / ".venv" / "bin" / "python"),
            "scripts/verify_production_path_gate.py",
            "preflight",
            "--compose",
            "production/tests/production-path-gate.compose.yaml",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    output = json.loads(completed.stderr)
    assert output["status"] == "blocked"
    assert output["release_evidence"] is False
    assert len(output["blockers"]) >= 4


def test_evidence_cli_rejects_constructed_ok_artifact_while_activation_is_blocked(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "evidence-cli-fixture"
    (repository / "scripts").mkdir(parents=True)
    (repository / "production" / "tests").mkdir(parents=True)
    (repository / "build" / "release-evidence").mkdir(parents=True)
    for relative in (
        "scripts/verify_production_path_gate.py",
        "production/compose.yaml",
        "production/tests/production-path-gate.compose.yaml",
    ):
        destination = repository / relative
        destination.write_bytes((ROOT / relative).read_bytes())
    artifact = repository / "build" / "release-evidence" / "production-path-gate.json"
    artifact.write_text(
        json.dumps(_valid_evidence(), ensure_ascii=True),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            str(ROOT / "backend" / ".venv" / "bin" / "python"),
            "scripts/verify_production_path_gate.py",
            "evidence",
            "--artifact",
            "build/release-evidence/production-path-gate.json",
            "--expected-commit",
            "a" * 40,
        ],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    output = json.loads(completed.stderr)
    assert output["release_evidence"] is False
    assert any("contract status is not ready" in item for item in output["blockers"])
    assert any("runtime driver is not implemented" in item for item in output["blockers"])
    assert any(
        "raw runtime proof binding is not implemented" in item for item in output["blockers"]
    )


def test_finalizer_mandates_strict_production_path_evidence_behind_blocked_gate() -> None:
    documentation = (ROOT / "production" / "tests" / "production-path-gate.md").read_text(
        encoding="utf-8"
    )
    release = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
    finalizer = (ROOT / "scripts" / "finalize_release_evidence.py").read_text(encoding="utf-8")

    assert "列为强制 core evidence" in documentation
    assert "前置 hard-fail" in documentation
    assert "严格复用同一运行证明校验器" in documentation
    assert '"production-path-gate.json"' in finalizer
    assert "validate_production_path_evidence" in finalizer
    assert release.index("bash scripts/verify_production_path.sh") < release.index(
        "scripts/finalize_release_evidence.py"
    )
