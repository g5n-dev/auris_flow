#!/usr/bin/env python3
"""Fail-closed contract and evidence validator for the production Compose path.

This module deliberately does not manufacture runtime evidence.  The checked-in
contract remains ``blocked`` until a runtime driver can prove every required
component in one isolated production Compose project.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "production" / "compose.yaml"
GATE_COMPOSE = ROOT / "production" / "tests" / "production-path-gate.compose.yaml"
RUNTIME_DRIVER = ROOT / "scripts" / "verify_production_path_runtime.py"
EVIDENCE_PATH = ROOT / "build" / "release-evidence" / "production-path-gate.json"
CONTRACT_SCHEMA = "auris.production-path-gate-contract.v1"
EVIDENCE_SCHEMA = "auris.production-path-gate.v1"
# Deliberately false until the runtime driver binds raw OIDC, adapter and OTel
# proofs to the sanitized evidence envelope.  Flipping only the YAML contract
# or adding a file named like the driver must never activate release evidence.
RAW_PROOF_BINDING_IMPLEMENTED = False
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_BASE_SERVICES = frozenset(
    {
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
    }
)
REQUIRED_GATE_SERVICES = frozenset(
    {
        "production-gate-embedding",
        "production-gate-callback",
        "production-path-verifier",
    }
)
REQUIRED_TRACE_COMPONENTS = frozenset(
    {
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
    }
)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return set()
    return set(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        _port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _service_environment(
    services: dict[str, Any], service_name: str, errors: list[str]
) -> dict[str, Any]:
    service = _mapping(services.get(service_name))
    if not service:
        errors.append(f"{service_name}: service override is required")
        return {}
    environment = _mapping(service.get("environment"))
    if not environment:
        errors.append(f"{service_name}: explicit production environment is required")
    return environment


def _require_service_hardening(
    services: dict[str, Any], service_name: str, errors: list[str]
) -> None:
    service = _mapping(services.get(service_name))
    if not service:
        errors.append(f"{service_name}: gate service is required")
        return
    if service.get("read_only") is not True:
        errors.append(f"{service_name}: read_only must be true")
    if service.get("cap_drop") != ["ALL"]:
        errors.append(f"{service_name}: cap_drop must contain only ALL")
    if service.get("privileged") is True or service.get("network_mode") == "host":
        errors.append(f"{service_name}: privileged or host networking is forbidden")


def _validate_callback_test_network(
    document: dict[str, Any], errors: list[str]
) -> None:
    network = _mapping(
        _mapping(document.get("networks")).get("production-gate-callback")
    )
    if not network:
        errors.append(
            "production-gate-callback network is required for the signed HTTPS callback receiver"
        )
        return
    if network.get("internal") is not True:
        errors.append(
            "production-gate-callback network must be isolated (internal: true)"
        )
    configs = _mapping(network.get("ipam")).get("config")
    subnet = configs[0].get("subnet") if isinstance(configs, list) and configs else None
    try:
        parsed = ipaddress.ip_network(str(subnet), strict=True)
    except ValueError:
        errors.append(
            "production-gate-callback network requires an exact isolated subnet"
        )
        return
    if not parsed.network_address.is_global:
        errors.append(
            "production-gate-callback test subnet must be globally classified so the production SSRF guard is exercised without a product bypass"
        )


def validate_gate_compose(document: object) -> list[str]:
    """Validate a gate overlay without treating it as runtime evidence."""

    errors: list[str] = []
    root = _mapping(document)
    if not root:
        return ["production path gate Compose contract must be a YAML object"]
    contract = _mapping(root.get("x-auris-production-path-gate"))
    if contract.get("schema_version") != CONTRACT_SCHEMA:
        errors.append(f"gate contract schema_version must be {CONTRACT_SCHEMA}")
    if contract.get("status") != "ready":
        errors.append(
            "gate contract status must be ready; blocked contracts are not evidence"
        )
    if contract.get("source_compose") != "production/compose.yaml":
        errors.append("gate contract must extend production/compose.yaml")
    if contract.get("runtime_driver") != "scripts/verify_production_path_runtime.py":
        errors.append("gate contract must name the commit-bound runtime driver")
    required_stubs = contract.get("required_external_stubs")
    stub_set = _string_set(required_stubs)
    if stub_set != REQUIRED_GATE_SERVICES - {"production-path-verifier"}:
        errors.append(
            "gate contract must require HTTPS embedding and callback test endpoints"
        )

    missing_capabilities = contract.get("missing_capabilities")
    if isinstance(missing_capabilities, list):
        for capability in missing_capabilities:
            if isinstance(capability, str) and capability.strip():
                errors.append(f"blocked capability: {capability.strip()}")

    services = _mapping(root.get("services"))
    required_environment = {
        "APP_ENV": "prod",
        "AUTH_PROVIDER": "oidc",
        "ALLOW_DEV_AUTH": "false",
        "AURIS_DAGSTER_ADAPTER": "real",
        "AURIS_OBJECT_STORAGE_ADAPTER": "real",
        "AURIS_QDRANT_ADAPTER": "real",
        "AURIS_EXTERNAL_CALLBACK_ADAPTER": "real",
        "AURIS_EMBEDDING_PROVIDER": "http",
        "OTEL_ENABLED": "true",
    }
    for service_name in ("bff", "worker"):
        environment = _service_environment(services, service_name, errors)
        for name, expected in required_environment.items():
            if environment.get(name) != expected:
                errors.append(f"{service_name}: {name} must be {expected}")
        if not _https_url(environment.get("OIDC_ISSUER")):
            errors.append(
                f"{service_name}: OIDC_ISSUER must be HTTPS for OIDC Authorization Code + PKCE"
            )
        if not _https_url(environment.get("EMBEDDING_ENDPOINT")):
            errors.append(
                f"{service_name}: HTTPS semantic embedding endpoint is required"
            )
        if not _https_url(environment.get("EXTERNAL_CALLBACK_URL")):
            errors.append(
                f"{service_name}: signed HTTPS external callback endpoint is required"
            )

    for service_name in sorted(REQUIRED_GATE_SERVICES):
        _require_service_hardening(services, service_name, errors)
    _validate_callback_test_network(root, errors)

    serialized = json.dumps(root, ensure_ascii=True, sort_keys=True).lower()
    for marker in (
        "fake_dagster_graphql_server",
        "deterministic_test",
        '"auris_dagster_adapter": "local"',
        '"auris_object_storage_adapter": "local"',
        '"auris_qdrant_adapter": "local"',
        '"auris_external_callback_adapter": "local"',
    ):
        if marker in serialized:
            errors.append(
                f"gate contract contains a forbidden production fallback: {marker}"
            )
    return errors


def _require_boolean(
    value: dict[str, Any], field: str, *, label: str, errors: list[str]
) -> None:
    if value.get(field) is not True:
        errors.append(f"{label}: {field} must be proven true")


def _validate_adapter_trace(
    adapter: dict[str, Any], *, label: str, trace_id: object, errors: list[str]
) -> None:
    if adapter.get("mode") != "real":
        errors.append(f"{label}: adapter mode must be real")
    if adapter.get("trace_id") != trace_id:
        errors.append("all production path proofs must bind to the same trace_id")


def _runtime_activation_errors(root: Path) -> list[str]:
    errors: list[str] = []
    contract_path = root / "production" / "tests" / "production-path-gate.compose.yaml"
    driver_path = root / "scripts" / "verify_production_path_runtime.py"
    try:
        contract_document = _load_yaml(contract_path)
    except ValueError:
        errors.append("production path checked-in contract is unavailable or unsafe")
    else:
        contract = _mapping(
            _mapping(contract_document).get("x-auris-production-path-gate")
        )
        if contract.get("status") != "ready":
            errors.append("production path checked-in contract status is not ready")
        elif validate_gate_compose(contract_document):
            errors.append("production path checked-in ready contract is invalid")
    if not driver_path.is_file() or driver_path.is_symlink():
        errors.append("production path runtime driver is not implemented")
    if not RAW_PROOF_BINDING_IMPLEMENTED:
        errors.append("production path raw runtime proof binding is not implemented")
    return errors


def validate_evidence(
    evidence: object, *, root: Path = ROOT, expected_commit: str
) -> list[str]:
    """Validate one runtime artifact; legacy split artifacts can never satisfy it."""

    errors: list[str] = []
    errors.extend(_runtime_activation_errors(root))
    payload = _mapping(evidence)
    if payload.get("schema_version") != EVIDENCE_SCHEMA:
        errors.append(f"evidence schema_version must be {EVIDENCE_SCHEMA}")
    if payload.get("status") != "ok":
        errors.append("production path evidence status must be ok")
    if not COMMIT_PATTERN.fullmatch(expected_commit):
        errors.append("expected source commit must be an exact lowercase Git SHA")
    if payload.get("source_commit") != expected_commit:
        errors.append(
            "production path evidence is not bound to the expected source commit"
        )
    if payload.get("execution_environment") != "production-compose":
        errors.append(
            "evidence must come from one single production Compose project, not legacy split gates"
        )
    if payload.get("producer") != "scripts/verify_production_path_runtime.py":
        errors.append("evidence producer must be the production path runtime driver")

    compose = _mapping(payload.get("compose"))
    if compose.get("base") != "production/compose.yaml":
        errors.append("evidence must bind production/compose.yaml")
    if compose.get("overlay") != "production/tests/production-path-gate.compose.yaml":
        errors.append("evidence must bind the production path gate overlay")
    for field, path in (
        ("base_sha256", root / "production" / "compose.yaml"),
        (
            "overlay_sha256",
            root / "production" / "tests" / "production-path-gate.compose.yaml",
        ),
    ):
        if not path.is_file():
            errors.append(f"compose input is missing: {path.relative_to(root)}")
            continue
        if compose.get(field) != _sha256_file(path):
            errors.append(f"compose {field} does not match the checked-in input")
    if not SHA256_PATTERN.fullmatch(str(compose.get("rendered_config_sha256") or "")):
        errors.append("rendered production Compose config hash is missing")
    services = compose.get("services")
    service_set = _string_set(services)
    missing_services = sorted(
        (REQUIRED_BASE_SERVICES | REQUIRED_GATE_SERVICES) - service_set
    )
    if missing_services:
        errors.append(
            "single production Compose evidence is missing services: "
            + ", ".join(missing_services)
        )

    trace = _mapping(payload.get("trace"))
    trace_id = trace.get("trace_id")
    if not isinstance(trace_id, str) or not trace_id.startswith("trace_"):
        errors.append("production path evidence requires a server trace_id")
    linked_components = trace.get("linked_components")
    linked_set = _string_set(linked_components)
    missing_components = sorted(REQUIRED_TRACE_COMPONENTS - linked_set)
    if missing_components:
        errors.append(
            "trace evidence is missing components: " + ", ".join(missing_components)
        )

    identity = _mapping(payload.get("identity"))
    if identity.get("provider") != "oidc":
        errors.append("identity proof must use OIDC")
    if identity.get("grant_type") != "authorization_code":
        errors.append("identity proof must use Authorization Code")
    if identity.get("pkce_method") != "S256":
        errors.append("identity proof must use PKCE S256")
    if identity.get("issuer_scheme") != "https":
        errors.append("identity issuer must use HTTPS")
    for field in (
        "discovery_verified",
        "jwks_verified",
        "code_exchange_verified",
        "browser_session_verified",
    ):
        _require_boolean(identity, field, label="identity", errors=errors)
    if identity.get("dev_auth_enabled") is not False:
        errors.append("identity proof must show dev auth disabled")
    if identity.get("trace_id") != trace_id:
        errors.append("all production path proofs must bind to the same trace_id")

    adapters = _mapping(payload.get("adapters"))
    dagster = _mapping(adapters.get("dagster"))
    _validate_adapter_trace(dagster, label="dagster", trace_id=trace_id, errors=errors)
    _require_boolean(dagster, "submitted", label="dagster", errors=errors)
    _require_boolean(
        dagster,
        "signed_completion_verified",
        label="dagster",
        errors=errors,
    )

    object_storage = _mapping(adapters.get("object_storage"))
    _validate_adapter_trace(
        object_storage,
        label="object storage",
        trace_id=trace_id,
        errors=errors,
    )
    if object_storage.get("provider") != "minio":
        errors.append("object storage proof must use MinIO")
    _require_boolean(
        object_storage,
        "object_verified",
        label="object storage",
        errors=errors,
    )

    qdrant = _mapping(adapters.get("qdrant"))
    _validate_adapter_trace(qdrant, label="qdrant", trace_id=trace_id, errors=errors)
    if qdrant.get("embedding_provider") != "http":
        errors.append("Qdrant proof must use the HTTP embedding provider")
    if qdrant.get("embedding_transport") != "https":
        errors.append("Qdrant HTTP embedding transport must use HTTPS")
    for field in ("semantic_embedding", "point_verified", "recall_verified"):
        _require_boolean(qdrant, field, label="qdrant", errors=errors)

    callback = _mapping(adapters.get("external_callback"))
    _validate_adapter_trace(
        callback,
        label="external callback",
        trace_id=trace_id,
        errors=errors,
    )
    if callback.get("transport") != "https":
        errors.append("external callback transport must use HTTPS")
    if callback.get("signature_mode") != "hmac-sha256-v2":
        errors.append("external callback must use hmac-sha256-v2 signature mode")
    for field in ("signature_verified", "replay_rejected"):
        _require_boolean(callback, field, label="external callback", errors=errors)

    observability = _mapping(payload.get("observability"))
    _require_boolean(
        observability, "otel_enabled", label="observability", errors=errors
    )
    _require_boolean(
        observability,
        "collector_export_verified",
        label="observability collector",
        errors=errors,
    )
    if observability.get("trace_id") != trace_id:
        errors.append("all production path proofs must bind to the same trace_id")
    observed_services = observability.get("services")
    observed_set = _string_set(observed_services)
    required_observed = {
        "auris-flow-bff",
        "auris-flow-worker",
        "auris-flow-dagster-code",
    }
    if not required_observed.issubset(observed_set):
        errors.append(
            "cross-service OTel trace must include BFF, Worker and Dagster code"
        )

    serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True).lower()
    for marker in (
        "fake_dagster_graphql_server",
        "deterministic_test",
        "mock://",
        "local_qdrant_projection",
    ):
        if marker in serialized:
            errors.append(f"runtime evidence contains a forbidden fallback: {marker}")
    return errors


def _load_yaml(path: Path) -> object:
    if not path.is_file() or path.is_symlink():
        raise ValueError("gate Compose contract must be a regular file")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("gate Compose contract exceeds 1 MiB")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise ValueError("gate Compose contract is invalid") from None


def _load_json(path: Path) -> object:
    if not path.is_file() or path.is_symlink():
        raise ValueError("production path evidence must be a regular file")
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("production path evidence exceeds 4 MiB")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("production path evidence is invalid JSON") from None


def _emit_failure(*, status: str, blockers: list[str], exit_code: int) -> int:
    print(
        json.dumps(
            {
                "status": status,
                "release_evidence": False,
                "blockers": blockers,
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the fail-closed Auris Flow production path gate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--compose", type=Path, required=True)
    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--artifact", type=Path, required=True)
    evidence.add_argument("--expected-commit", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "preflight":
            compose_path = (
                (ROOT / args.compose).resolve()
                if not args.compose.is_absolute()
                else args.compose.resolve()
            )
            if compose_path != GATE_COMPOSE.resolve():
                return _emit_failure(
                    status="blocked",
                    blockers=[
                        "preflight must validate the checked-in production path gate contract"
                    ],
                    exit_code=2,
                )
            errors = validate_gate_compose(_load_yaml(compose_path))
            if errors:
                return _emit_failure(status="blocked", blockers=errors, exit_code=2)
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "release_evidence": False,
                        "message": "preflight only; runtime evidence is still required",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                )
            )
            return 0
        artifact_path = (
            (ROOT / args.artifact).resolve()
            if not args.artifact.is_absolute()
            else args.artifact.resolve()
        )
        if artifact_path != EVIDENCE_PATH.resolve():
            return _emit_failure(
                status="blocked",
                blockers=[
                    "runtime evidence must use build/release-evidence/production-path-gate.json"
                ],
                exit_code=2,
            )
        errors = validate_evidence(
            _load_json(artifact_path),
            root=ROOT,
            expected_commit=args.expected_commit,
        )
        if errors:
            return _emit_failure(status="failed", blockers=errors, exit_code=1)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "release_evidence": True,
                    "artifact": str(artifact_path.relative_to(ROOT)),
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    except ValueError as exc:
        return _emit_failure(status="blocked", blockers=[str(exc)], exit_code=2)


if __name__ == "__main__":
    raise SystemExit(main())
