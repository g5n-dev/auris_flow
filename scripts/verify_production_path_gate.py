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
# This assertion is reviewable source, not a runtime shortcut: activation also
# requires every checked-in runtime source below, a ready Compose contract and a
# complete raw-proof/recovery envelope whose canonical hashes are recomputed.
RAW_PROOF_BINDING_IMPLEMENTED = False
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OTEL_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
BUSINESS_TRACE_ID_PATTERN = re.compile(r"^trace_[A-Za-z0-9._:-]{8,120}$")
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
REQUIRED_RUNTIME_SOURCES = (
    "scripts/verify_production_path_runtime.py",
    "production/tests/production_path_verifier.py",
    "production/tests/production_gate_support.py",
    "production/tests/production-path-keycloak-realm.template.json",
)
REQUIRED_OPERATION_TRACES = frozenset(
    {"oidc", "dagster", "object_storage", "qdrant", "external_callback"}
)
REQUIRED_RAW_PROOFS = frozenset(
    {
        "oidc_discovery",
        "oidc_jwks",
        "oidc_code_exchange",
        "browser_session",
        "mysql_authority",
        "dagster_graphql",
        "dagster_completion",
        "embedding_https",
        "qdrant_point",
        "qdrant_recall",
        "minio_object",
        "callback_delivery",
        "callback_replay",
        "tempo_trace",
        "mysql_restart",
        "worker_crash",
        "duplicate_delivery",
        "callback_timeout",
        "qdrant_outage",
        "redis_outage",
    }
)
RAW_PROOF_SOURCES = frozenset(
    {
        "bff-response",
        "compose-runtime",
        "dagster-graphql",
        "https-response",
        "minio-s3",
        "mysql",
        "qdrant-http",
        "tempo-http",
    }
)
RAW_PROOF_SOURCE_BY_ID = {
    "oidc_discovery": "https-response",
    "oidc_jwks": "https-response",
    "oidc_code_exchange": "mysql",
    "browser_session": "mysql",
    "mysql_authority": "mysql",
    "dagster_graphql": "dagster-graphql",
    "dagster_completion": "mysql",
    "embedding_https": "https-response",
    "qdrant_point": "qdrant-http",
    "qdrant_recall": "bff-response",
    "minio_object": "minio-s3",
    "callback_delivery": "https-response",
    "callback_replay": "https-response",
    "tempo_trace": "tempo-http",
    "mysql_restart": "compose-runtime",
    "worker_crash": "compose-runtime",
    "duplicate_delivery": "mysql",
    "callback_timeout": "mysql",
    "qdrant_outage": "compose-runtime",
    "redis_outage": "compose-runtime",
}
REQUIRED_RECOVERY_CASES = frozenset(
    {
        "mysql_restart",
        "worker_crash",
        "duplicate_delivery",
        "callback_timeout",
        "qdrant_outage",
        "redis_outage",
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
    security_opt = _string_set(service.get("security_opt"))
    if security_opt != {"no-new-privileges:true"}:
        errors.append(f"{service_name}: no-new-privileges must be enabled")
    user = str(service.get("user") or "").strip().lower()
    if not user or user in {"0", "0:0", "root", "root:root"}:
        errors.append(f"{service_name}: an explicit non-root user is required")
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
    adapter: dict[str, Any], *, label: str, expected_trace_id: object, errors: list[str]
) -> None:
    if adapter.get("mode") != "real":
        errors.append(f"{label}: adapter mode must be real")
    if adapter.get("trace_id") != expected_trace_id:
        errors.append(f"{label}: adapter trace_id must match its operation trace")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: object, *, minimum: int = 1) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= minimum


def _nonempty_strings(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    if any(not isinstance(item, str) or not item for item in value):
        return []
    return value


def _validate_proof_facts(
    proof_id: str,
    facts: dict[str, Any],
    *,
    payload: dict[str, Any],
    errors: list[str],
) -> None:
    """Validate concrete observations; a boolean-only claim is never evidence."""

    label = f"raw proof {proof_id} capture facts"

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(f"{label}: {message}")

    trace = _mapping(payload.get("trace"))
    operation_traces = _mapping(trace.get("operation_trace_ids"))
    scope = ("aurora_auto", "sales_qa")
    if proof_id == "oidc_discovery":
        require(facts.get("http_status") == 200, "discovery HTTP status must be 200")
        require(_https_url(facts.get("issuer")), "issuer must be an HTTPS URL")
        for field in (
            "authorization_endpoint_scheme",
            "token_endpoint_scheme",
            "jwks_uri_scheme",
        ):
            require(facts.get(field) == "https", f"{field} must be https")
    elif proof_id == "oidc_jwks":
        require(facts.get("http_status") == 200, "JWKS HTTP status must be 200")
        require(
            bool(_nonempty_strings(facts.get("rsa_signing_key_ids"))),
            "at least one RSA signing key id is required",
        )
    elif proof_id == "oidc_code_exchange":
        require(
            facts.get("grant_type") == "authorization_code", "grant type is invalid"
        )
        require(facts.get("pkce_method") == "S256", "PKCE method is invalid")
        require(
            facts.get("consumed_state_count") == 1, "one consumed state is required"
        )
        require(
            facts.get("browser_session_count") == 1, "one browser session is required"
        )
        require(
            facts.get("trace_id") == operation_traces.get("oidc"),
            "OIDC trace binding is invalid",
        )
    elif proof_id == "browser_session":
        require(
            facts.get("cookie_name") == "__Host-auris_session", "cookie name is invalid"
        )
        require(facts.get("cookie_secure") is True, "Secure cookie flag is required")
        require(
            facts.get("cookie_http_only") is True, "HttpOnly cookie flag is required"
        )
        require(facts.get("provider") == "oidc_session", "session provider is invalid")
        require(
            facts.get("active_session_count") == 1, "one active session is required"
        )
        require(
            bool(
                SHA256_PATTERN.fullmatch(str(facts.get("session_token_sha256") or ""))
            ),
            "opaque session token hash is invalid",
        )
        require(
            facts.get("trace_id") == operation_traces.get("oidc"),
            "browser session trace binding is invalid",
        )
    elif proof_id == "mysql_authority":
        require(
            (facts.get("tenant_id"), facts.get("project_id")) == scope,
            "authoritative scope is invalid",
        )
        run_ids = _nonempty_strings(facts.get("authoritative_run_ids"))
        require(bool(run_ids), "authoritative run ids are required")
        require(
            facts.get("authoritative_run_count") == len(run_ids),
            "authoritative run count does not match run ids",
        )
        require(
            _positive_int(facts.get("processed_outbox_count")),
            "processed outbox count must be positive",
        )
    elif proof_id == "dagster_graphql":
        require(
            facts.get("graphql_operation") == "pipelineRunOrError",
            "GraphQL operation is invalid",
        )
        require(
            facts.get("response_typename") == "Run", "Dagster response type is invalid"
        )
        require(
            isinstance(facts.get("dagster_run_id"), str)
            and bool(facts.get("dagster_run_id")),
            "Dagster run id is required",
        )
        require(
            facts.get("dagster_status") == "SUCCESS", "Dagster run is not successful"
        )
        require(
            facts.get("trace_id") == operation_traces.get("dagster"),
            "Dagster trace binding is invalid",
        )
    elif proof_id == "dagster_completion":
        require(facts.get("receipt_count") == 1, "one completion receipt is required")
        require(
            facts.get("processing_state") == "completed", "receipt is not completed"
        )
        require(
            facts.get("completion_status") == "success", "completion status is invalid"
        )
        require(
            facts.get("signature_mode") == "hmac-sha256-v2", "signature mode is invalid"
        )
        require(
            isinstance(facts.get("signature_key_id"), str)
            and bool(facts.get("signature_key_id")),
            "signature key id is required",
        )
        require(
            facts.get("run_trace_id") == operation_traces.get("dagster"),
            "completion trace binding is invalid",
        )
    elif proof_id == "embedding_https":
        require(facts.get("transport") == "https", "embedding transport is not HTTPS")
        require(facts.get("tls_verified") is True, "embedding TLS was not verified")
        require(
            facts.get("provider") == "reference-semantic-protocol",
            "embedding provider is invalid",
        )
        require(
            isinstance(facts.get("model"), str) and bool(facts.get("model")),
            "embedding model id is required",
        )
        require(
            _positive_int(facts.get("request_count"), minimum=2),
            "two embedding calls are required",
        )
        require(
            set(_nonempty_strings(facts.get("purposes"))) == {"document", "query"},
            "document and query purposes are required",
        )
        require(_positive_int(facts.get("dimension")), "embedding dimension is invalid")
        require(
            facts.get("reference_protocol_only") is True,
            "protocol-only marker is required",
        )
        require(
            facts.get("model_quality_certified") is False,
            "model quality must not be certified",
        )
    elif proof_id == "qdrant_point":
        require(facts.get("http_status") == 200, "Qdrant point HTTP status must be 200")
        require(
            all(
                isinstance(facts.get(field), str) and facts.get(field)
                for field in ("collection", "point_id")
            ),
            "collection and point id are required",
        )
        require(facts.get("point_count") == 1, "exactly one point must be observed")
        require(
            (facts.get("tenant_id"), facts.get("project_id")) == scope,
            "Qdrant scope is invalid",
        )
        require(
            facts.get("trace_id") == operation_traces.get("qdrant"),
            "Qdrant trace binding is invalid",
        )
        require(
            _positive_int(facts.get("vector_dimension")), "vector dimension is invalid"
        )
    elif proof_id == "qdrant_recall":
        point_ids = _nonempty_strings(facts.get("point_ids"))
        require(facts.get("http_status") == 200, "recall HTTP status must be 200")
        require(bool(point_ids), "authorized recalled point ids are required")
        require(
            facts.get("authorized_hit_count") == len(point_ids),
            "recall count does not match point ids",
        )
        require(
            facts.get("trace_id") == operation_traces.get("qdrant"),
            "recall trace binding is invalid",
        )
    elif proof_id == "minio_object":
        require(facts.get("bucket") == "auris-flow", "MinIO bucket is invalid")
        require(
            isinstance(facts.get("object_key"), str) and bool(facts.get("object_key")),
            "object key is required",
        )
        require(facts.get("http_status") == 200, "MinIO GET status must be 200")
        expected_hash = str(facts.get("expected_content_sha256") or "")
        observed_hash = str(facts.get("observed_content_sha256") or "")
        require(
            bool(SHA256_PATTERN.fullmatch(expected_hash)),
            "expected content hash is invalid",
        )
        require(
            expected_hash == observed_hash, "stored object content hash does not match"
        )
        require(_positive_int(facts.get("content_length")), "stored object is empty")
        require(
            facts.get("trace_id") == operation_traces.get("object_storage"),
            "object trace binding is invalid",
        )
    elif proof_id == "callback_delivery":
        require(facts.get("transport") == "https", "callback transport is not HTTPS")
        require(facts.get("tls_verified") is True, "callback TLS was not verified")
        require(
            facts.get("signature_mode") == "hmac-sha256-v2",
            "callback signature mode is invalid",
        )
        require(
            facts.get("signature_verified") is True,
            "callback signature is not verified",
        )
        require(
            facts.get("verified_receipt_count") == 1, "one callback receipt is required"
        )
        require(
            isinstance(facts.get("receipt_id"), str) and bool(facts.get("receipt_id")),
            "callback receipt id is required",
        )
        require(
            facts.get("trace_id") == operation_traces.get("external_callback"),
            "callback trace binding is invalid",
        )
    elif proof_id == "callback_replay":
        require(facts.get("http_status") == 409, "replay must return HTTP 409")
        require(
            facts.get("error_code") == "CALLBACK_SIGNATURE_REPLAYED",
            "replay error code is invalid",
        )
        require(
            facts.get("replay_rejected") is True, "callback replay was not rejected"
        )
    elif proof_id == "tempo_trace":
        require(facts.get("http_status") == 200, "Tempo trace HTTP status must be 200")
        require(
            facts.get("otel_trace_id") == trace.get("otel_trace_id"),
            "Tempo trace id is invalid",
        )
        require(
            {"auris-flow-bff", "auris-flow-worker", "auris-flow-dagster-code"}.issubset(
                set(_nonempty_strings(facts.get("services")))
            ),
            "Tempo trace does not include BFF, Worker and Dagster code",
        )
    elif proof_id == "mysql_restart":
        require(
            bool(SHA256_PATTERN.fullmatch(str(facts.get("container_id_sha256") or ""))),
            "container id hash is invalid",
        )
        require(
            facts.get("started_at_before") != facts.get("started_at_after"),
            "restart timestamps did not change",
        )
        require(facts.get("ready_status_after") == 200, "readiness did not recover")
        require(
            _positive_int(facts.get("authoritative_run_count_before"))
            and facts.get("authoritative_run_count_before")
            == facts.get("authoritative_run_count_after"),
            "authoritative MySQL run count changed across restart",
        )
    elif proof_id == "worker_crash":
        require(
            bool(SHA256_PATTERN.fullmatch(str(facts.get("container_id_sha256") or ""))),
            "container id hash is invalid",
        )
        require(
            facts.get("started_at_before") != facts.get("started_at_after"),
            "worker restart timestamps did not change",
        )
        require(_positive_int(facts.get("event_id")), "outbox event id is invalid")
        require(
            facts.get("event_status_before") == "pending",
            "event was not pending while Worker was down",
        )
        require(
            facts.get("event_status_after") == "processed",
            "event was not processed after Worker restart",
        )
        require(
            facts.get("remote_run_count") == 1,
            "worker crash produced duplicate remote runs",
        )
    elif proof_id == "duplicate_delivery":
        require(_positive_int(facts.get("event_id")), "outbox event id is invalid")
        require(
            _positive_int(facts.get("delivery_attempt_count"), minimum=2),
            "multiple delivery attempts were not observed",
        )
        require(
            facts.get("dispatch_attempt_count") == 1,
            "exactly one dispatch attempt is required",
        )
        require(
            facts.get("reconcile_attempt_count") == 1,
            "exactly one reconciliation attempt is required",
        )
        require(facts.get("remote_receipt_count") == 1, "remote receipt was duplicated")
        require(
            facts.get("business_outcome_count") == 1, "business outcome was duplicated"
        )
    elif proof_id == "callback_timeout":
        require(_positive_int(facts.get("event_id")), "outbox event id is invalid")
        require(
            facts.get("first_attempt_status") == "outcome_unknown",
            "timeout did not create an unknown outcome",
        )
        require(
            facts.get("final_attempt_status") == "success", "callback did not recover"
        )
        require(
            facts.get("final_delivery_mode") == "reconcile",
            "callback was not reconciled",
        )
        require(
            facts.get("remote_receipt_count") == 1,
            "callback reconciliation duplicated the receipt",
        )
    elif proof_id in {"qdrant_outage", "redis_outage"}:
        require(
            facts.get("ready_status_during") == 503,
            "readiness did not fail during outage",
        )
        require(
            facts.get("ready_status_after") == 200,
            "readiness did not recover after outage",
        )
        require(
            _positive_int(facts.get("authoritative_run_count_before"))
            and facts.get("authoritative_run_count_before")
            == facts.get("authoritative_run_count_after"),
            "MySQL authority changed during dependency outage",
        )
        if proof_id == "qdrant_outage":
            require(
                isinstance(facts.get("point_id"), str) and bool(facts.get("point_id")),
                "preserved point id is required",
            )
            require(
                facts.get("point_present_after") is True,
                "Qdrant point was not recovered",
            )


def _validate_raw_proofs(payload: dict[str, Any], errors: list[str]) -> set[str]:
    raw = _mapping(payload.get("raw_proofs"))
    if raw.get("schema_version") != "auris.production-path.raw-proofs.v1":
        errors.append("raw proofs schema_version is invalid")
    records = _mapping(raw.get("records"))
    record_names = set(records)
    if record_names != REQUIRED_RAW_PROOFS:
        errors.append(
            "raw proofs inventory must exactly match the production path contract"
        )
    for proof_id, value in records.items():
        proof = _mapping(value)
        label = f"raw proof {proof_id}"
        expected_source = RAW_PROOF_SOURCE_BY_ID.get(proof_id)
        if (
            proof.get("source") not in RAW_PROOF_SOURCES
            or proof.get("source") != expected_source
        ):
            errors.append(f"{label}: source is invalid for this proof")
        if proof.get("media_type") != "application/json":
            errors.append(f"{label}: media_type must be application/json")
        capture = _mapping(proof.get("capture"))
        if capture.get("schema_version") != "auris.production-path.capture.v1":
            errors.append(f"{label}: capture schema_version is invalid")
        if (
            capture.get("proof_id") != proof_id
            or capture.get("source") != expected_source
        ):
            errors.append(f"{label}: capture identity does not match its record")
        if proof.get("capture_sha256") != _canonical_sha256(capture):
            errors.append(f"{label}: capture_sha256 does not match embedded capture")
        facts = _mapping(proof.get("facts"))
        if not facts:
            errors.append(f"{label}: sanitized facts are required")
        if capture.get("observations") != facts:
            errors.append(
                f"{label}: facts must be derived exactly from capture observations"
            )
        if proof.get("facts_sha256") != _canonical_sha256(facts):
            errors.append(f"{label}: facts_sha256 does not match sanitized facts")
        if proof_id in REQUIRED_RAW_PROOFS:
            _validate_proof_facts(proof_id, facts, payload=payload, errors=errors)
    if raw.get("bundle_sha256") != _canonical_sha256(records):
        errors.append("raw proofs bundle_sha256 does not match the proof records")
    return record_names


def _validate_recovery_matrix(
    payload: dict[str, Any], *, proof_ids: set[str], errors: list[str]
) -> None:
    recovery = _mapping(payload.get("recovery"))
    if set(recovery) != REQUIRED_RECOVERY_CASES:
        errors.append("recovery matrix must exactly match the required fault cases")
    for case_name, value in recovery.items():
        case = _mapping(value)
        label = f"recovery {case_name}"
        for field in (
            "proven",
            "authority_consistent",
            "no_duplicate_business_outcome",
        ):
            _require_boolean(case, field, label=label, errors=errors)
        raw_proof_ids = _string_set(case.get("raw_proof_ids"))
        if not raw_proof_ids or case_name not in raw_proof_ids:
            errors.append(f"{label}: its named raw proof is required")
        if not raw_proof_ids.issubset(proof_ids):
            errors.append(f"{label}: references unknown raw proof ids")


def _runtime_activation_errors(root: Path) -> list[str]:
    errors: list[str] = []
    contract_path = root / "production" / "tests" / "production-path-gate.compose.yaml"
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
    for relative in REQUIRED_RUNTIME_SOURCES:
        source = root / relative
        if not source.is_file() or source.is_symlink():
            errors.append(f"production path runtime source is missing: {relative}")
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

    runtime_sources = _mapping(payload.get("runtime_sources"))
    if set(runtime_sources) != set(REQUIRED_RUNTIME_SOURCES):
        errors.append(
            "runtime source hash inventory must exactly match the production path contract"
        )
    for relative in REQUIRED_RUNTIME_SOURCES:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            continue
        if runtime_sources.get(relative) != _sha256_file(path):
            errors.append(
                f"runtime source hash does not match checked-in input: {relative}"
            )

    trace = _mapping(payload.get("trace"))
    primary_trace_id = trace.get("primary_business_trace_id")
    otel_trace_id = trace.get("otel_trace_id")
    if not BUSINESS_TRACE_ID_PATTERN.fullmatch(str(primary_trace_id or "")):
        errors.append(
            "production path evidence requires a primary server business trace_id"
        )
    if not OTEL_TRACE_ID_PATTERN.fullmatch(str(otel_trace_id or "")):
        errors.append("production path evidence requires an exact OTel trace id")
    operation_traces = _mapping(trace.get("operation_trace_ids"))
    if set(operation_traces) != REQUIRED_OPERATION_TRACES:
        errors.append("production path evidence requires every operation trace id")
    for operation, operation_trace_id in operation_traces.items():
        if not BUSINESS_TRACE_ID_PATTERN.fullmatch(str(operation_trace_id or "")):
            errors.append(f"operation trace id is invalid: {operation}")
    if operation_traces.get("dagster") != primary_trace_id:
        errors.append("primary business trace must be the real Dagster operation trace")
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
    if identity.get("trace_id") != operation_traces.get("oidc"):
        errors.append("identity proof must bind to the OIDC operation trace")

    adapters = _mapping(payload.get("adapters"))
    dagster = _mapping(adapters.get("dagster"))
    _validate_adapter_trace(
        dagster,
        label="dagster",
        expected_trace_id=operation_traces.get("dagster"),
        errors=errors,
    )
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
        expected_trace_id=operation_traces.get("object_storage"),
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
    _validate_adapter_trace(
        qdrant,
        label="qdrant",
        expected_trace_id=operation_traces.get("qdrant"),
        errors=errors,
    )
    if qdrant.get("embedding_provider") != "http":
        errors.append("Qdrant proof must use the HTTP embedding provider")
    if qdrant.get("embedding_transport") != "https":
        errors.append("Qdrant HTTP embedding transport must use HTTPS")
    for field in ("semantic_embedding", "point_verified", "recall_verified"):
        _require_boolean(qdrant, field, label="qdrant", errors=errors)
    if qdrant.get("reference_protocol_only") is not True:
        errors.append("Qdrant proof must identify the gate embedding as protocol-only")
    if qdrant.get("model_quality_certified") is not False:
        errors.append(
            "production path gate must not claim embedding model quality certification"
        )

    callback = _mapping(adapters.get("external_callback"))
    _validate_adapter_trace(
        callback,
        label="external callback",
        expected_trace_id=operation_traces.get("external_callback"),
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
    if observability.get("business_trace_id") != primary_trace_id:
        errors.append("observability proof must bind the primary business trace")
    if observability.get("otel_trace_id") != otel_trace_id:
        errors.append("observability proof must bind the primary OTel trace")
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

    raw_proof_ids = _validate_raw_proofs(payload, errors)
    _validate_recovery_matrix(payload, proof_ids=raw_proof_ids, errors=errors)

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
