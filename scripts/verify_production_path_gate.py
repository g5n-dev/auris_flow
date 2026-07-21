#!/usr/bin/env python3
"""Fail-closed contract and evidence validator for the production Compose path.

This module deliberately does not manufacture runtime evidence.  A ``ready``
checked-in contract only activates the diagnostic; the runtime driver must still
prove every required component in one isolated production Compose project.
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
RAW_PROOF_BINDING_IMPLEMENTED = True
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_DIGEST_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
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
        "prometheus",
        "grafana",
        "node-exporter",
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
        "outbox",
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
    "production/tests/production-path-gate.env",
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
AUTHORITY_FACT_KEYS = frozenset(
    {"authoritative_run_count_before", "authoritative_run_count_after"}
)
PROOF_FACT_KEYS = {
    "oidc_discovery": frozenset(
        {
            "http_status",
            "issuer",
            "authorization_endpoint_scheme",
            "token_endpoint_scheme",
            "jwks_uri_scheme",
        }
    ),
    "oidc_jwks": frozenset({"http_status", "rsa_signing_key_ids"}),
    "oidc_code_exchange": frozenset(
        {
            "grant_type",
            "pkce_method",
            "consumed_state_count",
            "browser_session_count",
            "trace_id",
        }
    ),
    "browser_session": frozenset(
        {
            "cookie_name",
            "cookie_secure",
            "cookie_http_only",
            "provider",
            "active_session_count",
            "session_token_sha256",
            "trace_id",
        }
    ),
    "mysql_authority": frozenset(
        {
            "tenant_id",
            "project_id",
            "authoritative_run_ids",
            "authoritative_run_count",
            "processed_outbox_count",
        }
    ),
    "dagster_graphql": frozenset(
        {
            "graphql_operation",
            "response_typename",
            "dagster_run_id",
            "dagster_status",
            "trace_id",
        }
    ),
    "dagster_completion": frozenset(
        {
            "receipt_count",
            "processing_state",
            "completion_status",
            "signature_mode",
            "signature_key_id",
            "run_trace_id",
        }
    ),
    "embedding_https": frozenset(
        {
            "transport",
            "tls_verified",
            "provider",
            "model",
            "request_count",
            "purposes",
            "dimension",
            "reference_protocol_only",
            "model_quality_certified",
        }
    ),
    "qdrant_point": frozenset(
        {
            "http_status",
            "collection",
            "point_id",
            "point_count",
            "tenant_id",
            "project_id",
            "trace_id",
            "vector_dimension",
        }
    ),
    "qdrant_recall": frozenset(
        {
            "http_status",
            "point_ids",
            "authorized_hit_count",
            "written_point_id",
            "written_point_occurrences",
            "trace_id",
        }
    ),
    "minio_object": frozenset(
        {
            "bucket",
            "object_key",
            "http_status",
            "expected_content_sha256",
            "observed_content_sha256",
            "content_length",
            "trace_id",
        }
    ),
    "callback_delivery": frozenset(
        {
            "transport",
            "tls_verified",
            "signature_mode",
            "signature_verified",
            "verified_receipt_count",
            "receipt_id",
            "trace_id",
        }
    ),
    "callback_replay": frozenset({"http_status", "error_code", "replay_rejected"}),
    "tempo_trace": frozenset(
        {
            "http_status",
            "otel_trace_id",
            "operation_otel_trace_ids",
            "operations",
            "services",
            "components",
        }
    ),
    "mysql_restart": frozenset(
        {
            "container_id_sha256",
            "started_at_before",
            "started_at_after",
            "ready_status_after",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "worker_crash": frozenset(
        {
            "container_id_sha256",
            "started_at_before",
            "started_at_after",
            "event_id",
            "event_status_before",
            "event_status_after",
            "remote_run_count",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "duplicate_delivery": frozenset(
        {
            "event_id",
            "delivery_attempt_count",
            "dispatch_attempt_count",
            "reconcile_attempt_count",
            "remote_receipt_count",
            "business_outcome_count",
            "stale_owner_rejected",
            "new_owner_accepted",
            "lease_generation_before",
            "lease_generation_after",
            "claim_token_sha256_before",
            "claim_token_sha256_after",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "callback_timeout": frozenset(
        {
            "event_id",
            "first_attempt_status",
            "final_attempt_status",
            "final_delivery_mode",
            "remote_receipt_count",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "qdrant_outage": frozenset(
        {
            "ready_status_during",
            "ready_status_after",
            "failed_dependency_during",
            "failed_dependency_status_during",
            "missing_required_during",
            "recovered_dependency_status_after",
            "missing_required_after",
            "point_id",
            "point_present_after",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "redis_outage": frozenset(
        {
            "ready_status_during",
            "ready_status_after",
            "failed_dependency_during",
            "failed_dependency_status_during",
            "missing_required_during",
            "recovered_dependency_status_after",
            "missing_required_after",
        }
    )
    | AUTHORITY_FACT_KEYS,
}
OPERATION_OTEL_FACT_KEYS = frozenset(
    {
        "otel_trace_id",
        "services",
        "components",
        "component_signals",
        "span_count",
        "client_span_count",
    }
)
OPERATION_OTEL_COMPONENTS = {
    "oidc": frozenset({"bff", "mysql", "oidc", "otel"}),
    "dagster": frozenset(
        {"bff", "mysql", "redis", "outbox", "worker", "dagster", "otel"}
    ),
    "object_storage": frozenset(
        {"bff", "mysql", "outbox", "worker", "object_storage", "otel"}
    ),
    "qdrant": frozenset({"bff", "mysql", "outbox", "worker", "qdrant", "otel"}),
    "external_callback": frozenset(
        {"bff", "mysql", "outbox", "worker", "external_callback", "otel"}
    ),
}
OPERATION_OTEL_SERVICES = {
    "oidc": frozenset({"auris-flow-bff"}),
    "dagster": frozenset(
        {"auris-flow-bff", "auris-flow-worker", "auris-flow-dagster-code"}
    ),
    "object_storage": frozenset({"auris-flow-bff", "auris-flow-worker"}),
    "qdrant": frozenset({"auris-flow-bff", "auris-flow-worker"}),
    "external_callback": frozenset({"auris-flow-bff", "auris-flow-worker"}),
}
OTEL_SIGNAL_BY_COMPONENT = {
    "bff": "service.name=auris-flow-bff",
    "worker": "service.name=auris-flow-worker",
    "dagster": "service.name=auris-flow-dagster-code",
    "mysql": "db.system=mysql",
    "redis": "db.system=redis",
    "outbox": "span.name=outbox.process",
    "object_storage": "client.host=minio",
    "qdrant": "client.host=qdrant",
    "external_callback": "client.host=callback.production-gate.invalid",
    "oidc": "client.host=auris-production-gate.invalid",
    "otel": "tempo.trace",
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
REQUIRED_RUNNING_SERVICES = frozenset(
    {
        "mysql",
        "redis",
        "minio",
        "qdrant",
        "keycloak",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
        "bff",
        "worker",
        "otel-collector",
        "tempo",
        "prometheus",
        "grafana",
        "node-exporter",
        "edge",
        "production-gate-embedding",
        "production-gate-callback",
    }
)
REQUIRED_COMPLETED_SERVICES = frozenset(
    {
        "db-bootstrap",
        "minio-bootstrap",
        "migrate",
        "identity-bootstrap",
        "production-path-seed",
    }
)
EXPECTED_EXTERNAL_SERVICE_IMAGES = {
    "mysql": "mysql:8.4.5",
    "db-bootstrap": "mysql:8.4.5",
    "redis": "redis:7.4.2-alpine3.21",
    "minio": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    "minio-bootstrap": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
    "qdrant": "qdrant/qdrant:v1.14.1",
    "keycloak": "quay.io/keycloak/keycloak:26.2.5",
    "otel-collector": "otel/opentelemetry-collector-contrib:0.128.0",
    "tempo": "grafana/tempo:2.8.0",
    "prometheus": "prom/prometheus:v3.4.1",
    "grafana": "grafana/grafana:12.0.1",
    "node-exporter": "prom/node-exporter:v1.9.1",
}
SAFE_SECURITY_METADATA_KEYS = frozenset(
    {
        "authorization_endpoint_scheme",
        "token_endpoint_scheme",
        "cookie_name",
        "cookie_secure",
        "cookie_http_only",
        "claim_token_sha256_after",
        "claim_token_sha256_before",
        "session_token_sha256",
        "signature_key_id",
        "rsa_signing_key_ids",
    }
)
_PERSONAL_MAC_PATH = "/" + r"Users/[^/\s]+"
_PERSONAL_LINUX_PATH = "/" + r"home/[^/\s]+"
_PERSONAL_WINDOWS_PATH = r"[A-Za-z]:\\" + r"Users\\[^\\\s]+"
PERSONAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'])(?:"
    + "|".join((_PERSONAL_MAC_PATH, _PERSONAL_LINUX_PATH, _PERSONAL_WINDOWS_PATH))
    + r")(?:/|\\)"
)
EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "source_commit",
        "execution_environment",
        "producer",
        "compose",
        "runtime_sources",
        "identity",
        "adapters",
        "observability",
        "trace",
        "raw_proofs",
        "recovery",
    }
)
COMPOSE_EVIDENCE_FIELDS = frozenset(
    {
        "base",
        "overlay",
        "base_sha256",
        "overlay_sha256",
        "rendered_config_sha256",
        "services",
        "host_runtime",
        "runtime",
    }
)
HOST_RUNTIME_FIELDS = frozenset(
    {
        "schema_version",
        "native_linux",
        "host_platform",
        "docker_endpoint_scheme",
        "docker_endpoint_path",
        "docker_ostype",
        "docker_operating_system",
        "architecture",
        "rootless",
        "cgroup_driver",
        "cgroup_version",
        "storage_driver",
    }
)
IDENTITY_FIELDS = frozenset(
    {
        "provider",
        "grant_type",
        "pkce_method",
        "issuer_scheme",
        "discovery_verified",
        "jwks_verified",
        "code_exchange_verified",
        "browser_session_verified",
        "dev_auth_enabled",
        "trace_id",
    }
)
ADAPTER_FIELDS = frozenset({"dagster", "object_storage", "qdrant", "external_callback"})
ADAPTER_SECTION_FIELDS = {
    "dagster": frozenset(
        {"mode", "trace_id", "submitted", "signed_completion_verified"}
    ),
    "object_storage": frozenset({"mode", "provider", "trace_id", "object_verified"}),
    "qdrant": frozenset(
        {
            "mode",
            "trace_id",
            "embedding_provider",
            "embedding_transport",
            "semantic_embedding",
            "point_verified",
            "recall_verified",
            "reference_protocol_only",
            "model_quality_certified",
        }
    ),
    "external_callback": frozenset(
        {
            "mode",
            "trace_id",
            "transport",
            "signature_mode",
            "signature_verified",
            "replay_rejected",
        }
    ),
}
OBSERVABILITY_FIELDS = frozenset(
    {
        "otel_enabled",
        "collector_export_verified",
        "business_trace_id",
        "otel_trace_id",
        "services",
    }
)
TRACE_FIELDS = frozenset(
    {
        "primary_business_trace_id",
        "otel_trace_id",
        "operation_otel_trace_ids",
        "operation_trace_ids",
        "linked_components",
    }
)
RAW_PROOFS_FIELDS = frozenset({"schema_version", "bundle_sha256", "records"})
RAW_PROOF_RECORD_FIELDS = frozenset(
    {"source", "media_type", "capture", "capture_sha256", "facts_sha256", "facts"}
)
CAPTURE_FIELDS = frozenset({"schema_version", "proof_id", "source", "observations"})
RECOVERY_FIELDS = frozenset(
    {"proven", "authority_consistent", "no_duplicate_business_outcome", "raw_proof_ids"}
)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _require_exact_fields(
    value: dict[str, Any], expected: frozenset[str], *, label: str, errors: list[str]
) -> None:
    if set(value) != expected:
        errors.append(f"{label} fields do not match the closed evidence contract")


def _validate_host_runtime(compose: dict[str, Any], errors: list[str]) -> None:
    host = _mapping(compose.get("host_runtime"))
    _require_exact_fields(
        host,
        HOST_RUNTIME_FIELDS,
        label="native Linux host runtime",
        errors=errors,
    )
    if (
        host.get("schema_version") != "auris.production-path.host-runtime.v1"
        or host.get("native_linux") is not True
        or host.get("host_platform") != "linux"
        or host.get("docker_endpoint_scheme") != "unix"
        or host.get("docker_endpoint_path") != "/var/run/docker.sock"
        or host.get("docker_ostype") != "linux"
        or not isinstance(host.get("docker_operating_system"), str)
        or not host["docker_operating_system"].strip()
        or len(host["docker_operating_system"]) > 200
        or any(character in host["docker_operating_system"] for character in "\r\n\0")
        or any(
            marker in host["docker_operating_system"].casefold()
            for marker in ("docker desktop", "rancher desktop", "colima", "orbstack")
        )
        or host.get("architecture") not in {"amd64", "arm64"}
        or host.get("rootless") is not False
        or any(
            not isinstance(host.get(field), str) or not str(host[field]).strip()
            for field in ("cgroup_driver", "cgroup_version", "storage_driver")
        )
    ):
        errors.append(
            "native Linux host evidence must prove a local rootful Linux Docker daemon"
        )


def _scan_evidence_safety(
    value: object, *, errors: list[str], path: tuple[str, ...] = ()
) -> None:
    if isinstance(value, dict):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                errors.append("production evidence contains a non-string field name")
                continue
            normalized = raw_key.casefold().replace("-", "_")
            parts = set(normalized.split("_"))
            sensitive = bool(
                parts
                & {
                    "authorization",
                    "body",
                    "cookie",
                    "credential",
                    "credentials",
                    "header",
                    "headers",
                    "password",
                    "secret",
                    "token",
                }
            ) or any(
                marker in normalized
                for marker in (
                    "api_key",
                    "access_key",
                    "encryption_key",
                    "key_material",
                    "private_key",
                    "signing_key",
                )
            )
            if sensitive and normalized not in SAFE_SECURITY_METADATA_KEYS:
                errors.append(
                    "production evidence contains a forbidden sensitive field: "
                    + ".".join((*path, raw_key))
                )
            _scan_evidence_safety(item, errors=errors, path=(*path, raw_key))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_evidence_safety(item, errors=errors, path=(*path, str(index)))
        return
    if isinstance(value, str) and PERSONAL_PATH_PATTERN.search(value):
        errors.append("production evidence contains a personal absolute path")


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return set()
    return set(value)


def _normalized_image_repository(reference: str) -> str:
    repository = reference.split("@", 1)[0]
    final_slash = repository.rfind("/")
    final_colon = repository.rfind(":")
    if final_colon > final_slash:
        repository = repository[:final_colon]
    for prefix in ("docker.io/library/", "index.docker.io/library/"):
        if repository.startswith(prefix):
            return repository.removeprefix(prefix)
    return repository.removeprefix("docker.io/")


def _expected_service_image(service: str, source_commit: str) -> str | None:
    external = EXPECTED_EXTERNAL_SERVICE_IMAGES.get(service)
    if external is not None:
        return external
    suffix = source_commit[:12]
    if service in {"dagster-code", "dagster-webserver", "dagster-daemon"}:
        return f"auris-flow-production-gate-dagster:{suffix}"
    if service == "edge":
        return f"auris-flow-production-gate-edge:{suffix}"
    if service in {
        "bff",
        "worker",
        "migrate",
        "identity-bootstrap",
        "production-path-seed",
        "production-gate-embedding",
        "production-gate-callback",
    }:
        return f"auris-flow-production-gate-bff:{suffix}"
    return None


def _validate_runtime_inventory(
    compose: dict[str, Any], *, source_commit: str, errors: list[str]
) -> None:
    host_architecture = _mapping(compose.get("host_runtime")).get("architecture")
    runtime = _mapping(compose.get("runtime"))
    if set(runtime) != {"running_services", "completed_services"}:
        errors.append("Compose runtime inventory envelope is invalid")
    running = _mapping(runtime.get("running_services"))
    completed = _mapping(runtime.get("completed_services"))
    if set(running) != REQUIRED_RUNNING_SERVICES:
        errors.append("Compose running service inventory is incomplete")
    if set(completed) != REQUIRED_COMPLETED_SERVICES:
        errors.append("Compose one-shot service inventory is incomplete")
    common_fields = {
        "container_id_sha256",
        "configured_image",
        "image_id",
        "repo_digests",
        "os",
        "architecture",
        "state",
    }
    architectures: set[str] = set()
    for section_name, section, state_field in (
        ("running", running, "health"),
        ("completed", completed, "exit_code"),
    ):
        for service, raw_observation in section.items():
            observation = _mapping(raw_observation)
            if set(observation) != common_fields | {state_field}:
                errors.append(
                    f"Compose {section_name} observation is invalid: {service}"
                )
                continue
            container_hash = str(observation.get("container_id_sha256") or "")
            image_id = str(observation.get("image_id") or "")
            configured_image = observation.get("configured_image")
            repo_digests = observation.get("repo_digests")
            architecture = observation.get("architecture")
            if not SHA256_PATTERN.fullmatch(container_hash):
                errors.append(f"Compose container identity is invalid: {service}")
            if not IMAGE_ID_PATTERN.fullmatch(image_id):
                errors.append(f"Compose image identity is invalid: {service}")
            expected_image = _expected_service_image(service, source_commit)
            if configured_image != expected_image:
                errors.append(
                    f"Compose configured image is not pinned by the gate: {service}"
                )
            if observation.get("os") != "linux" or architecture not in {
                "amd64",
                "arm64",
            }:
                errors.append(f"Compose image platform is invalid: {service}")
            elif isinstance(architecture, str):
                architectures.add(architecture)
            if (
                not isinstance(repo_digests, list)
                or repo_digests != sorted(set(repo_digests))
                or any(
                    not isinstance(item, str) or not REPO_DIGEST_PATTERN.fullmatch(item)
                    for item in repo_digests
                )
            ):
                errors.append(
                    f"Compose repository digest inventory is invalid: {service}"
                )
                repo_digests = []
            if service in EXPECTED_EXTERNAL_SERVICE_IMAGES:
                expected_repository = _normalized_image_repository(
                    EXPECTED_EXTERNAL_SERVICE_IMAGES[service]
                )
                if not repo_digests or not any(
                    _normalized_image_repository(str(item)) == expected_repository
                    for item in repo_digests
                ):
                    errors.append(f"Compose repository digest is unbound: {service}")
            if section_name == "running" and (
                observation.get("state") != "running"
                or observation.get("health") != "healthy"
            ):
                errors.append(f"Compose runtime service is not healthy: {service}")
            if section_name == "completed" and (
                observation.get("state") != "exited"
                or observation.get("exit_code") != 0
            ):
                errors.append(
                    f"Compose one-shot service did not exit successfully: {service}"
                )
    if len(architectures) > 1:
        errors.append("Compose runtime mixes multiple container architectures")
    if (
        isinstance(host_architecture, str)
        and architectures
        and architectures != {host_architecture}
    ):
        errors.append(
            "Compose runtime container architecture must match the native Linux host"
        )


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
    operation_otel_trace_ids = _mapping(trace.get("operation_otel_trace_ids"))
    proof_records = _mapping(_mapping(payload.get("raw_proofs")).get("records"))

    def proof_facts(record_id: str) -> dict[str, Any]:
        return _mapping(_mapping(proof_records.get(record_id)).get("facts"))

    scope = ("aurora_auto", "sales_qa")
    if proof_id in REQUIRED_RECOVERY_CASES:
        authority_count = proof_facts("mysql_authority").get("authoritative_run_count")
        require(
            authority_count == 4
            and facts.get("authoritative_run_count_before") == authority_count
            and facts.get("authoritative_run_count_after") == authority_count,
            "authoritative MySQL run count changed across recovery",
        )
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
        require(
            len(run_ids) == 4 and len(set(run_ids)) == 4,
            "exactly four unique authoritative run ids are required",
        )
        require(
            facts.get("authoritative_run_count") == len(run_ids) == 4,
            "authoritative run count does not match run ids",
        )
        require(
            facts.get("processed_outbox_count") == len(run_ids) == 4,
            "processed outbox count must match all authoritative runs",
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
        written_point_id = proof_facts("qdrant_point").get("point_id")
        require(facts.get("http_status") == 200, "recall HTTP status must be 200")
        require(
            bool(point_ids) and len(point_ids) == len(set(point_ids)),
            "authorized recalled point ids must be unique",
        )
        require(
            facts.get("authorized_hit_count") == len(point_ids),
            "recall count does not match point ids",
        )
        require(
            isinstance(written_point_id, str)
            and bool(written_point_id)
            and facts.get("written_point_id") == written_point_id
            and facts.get("written_point_occurrences") == 1
            and point_ids.count(written_point_id) == 1,
            "Qdrant written point and recall are not cross-bound",
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
            facts.get("operation_otel_trace_ids") == operation_otel_trace_ids,
            "Tempo operation OTel trace ids are invalid",
        )
        operations = _mapping(facts.get("operations"))
        require(
            set(operations) == REQUIRED_OPERATION_TRACES,
            "Tempo operation inventory is invalid",
        )
        observed_services: set[str] = set()
        observed_components: set[str] = set()
        for operation in sorted(REQUIRED_OPERATION_TRACES):
            operation_facts = _mapping(operations.get(operation))
            expected_components = OPERATION_OTEL_COMPONENTS[operation]
            expected_services = OPERATION_OTEL_SERVICES[operation]
            services = _nonempty_strings(operation_facts.get("services"))
            components = _nonempty_strings(operation_facts.get("components"))
            signals = _mapping(operation_facts.get("component_signals"))
            valid_operation = (
                set(operation_facts) == OPERATION_OTEL_FACT_KEYS
                and operation_facts.get("otel_trace_id")
                == operation_otel_trace_ids.get(operation)
                and len(services) == len(set(services))
                and set(services) == expected_services
                and len(components) == len(set(components))
                and set(components) == expected_components
                and set(signals) == expected_components
                and _positive_int(operation_facts.get("span_count"))
                and _positive_int(operation_facts.get("client_span_count"))
                and all(
                    signals.get(component) == [OTEL_SIGNAL_BY_COMPONENT[component]]
                    for component in expected_components
                )
            )
            require(valid_operation, f"Tempo {operation} operation trace is invalid")
            observed_services.update(expected_services)
            observed_components.update(expected_components)
        top_services = _nonempty_strings(facts.get("services"))
        top_components = _nonempty_strings(facts.get("components"))
        require(
            len(top_services) == len(set(top_services))
            and set(top_services) == observed_services
            and len(top_components) == len(set(top_components))
            and set(top_components) == observed_components,
            "Tempo aggregate service/component unions are invalid",
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
        token_before = str(facts.get("claim_token_sha256_before") or "")
        token_after = str(facts.get("claim_token_sha256_after") or "")
        require(
            facts.get("stale_owner_rejected") is True,
            "stale lease owner was not rejected",
        )
        require(
            facts.get("new_owner_accepted") is True,
            "new lease owner was not accepted",
        )
        generation_before = facts.get("lease_generation_before")
        generation_after = facts.get("lease_generation_after")
        require(
            isinstance(generation_before, int)
            and not isinstance(generation_before, bool)
            and generation_before >= 1
            and isinstance(generation_after, int)
            and not isinstance(generation_after, bool)
            and generation_after >= 1
            and generation_after == generation_before + 1,
            "lease fencing generation did not advance exactly once",
        )
        require(
            bool(SHA256_PATTERN.fullmatch(token_before))
            and bool(SHA256_PATTERN.fullmatch(token_after))
            and token_before != token_after,
            "lease claim token hashes are invalid or reused",
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
        target_dependency = proof_id.removesuffix("_outage")
        require(
            facts.get("ready_status_during") == 503,
            "readiness did not fail during outage",
        )
        require(
            facts.get("ready_status_after") == 200,
            "readiness did not recover after outage",
        )
        require(
            facts.get("failed_dependency_during") == target_dependency
            and facts.get("failed_dependency_status_during") == "not_ready"
            and facts.get("missing_required_during") == [target_dependency]
            and facts.get("recovered_dependency_status_after") == "ok"
            and facts.get("missing_required_after") == [],
            "readiness outage target and recovery facts are invalid",
        )
        require(
            _positive_int(facts.get("authoritative_run_count_before"))
            and facts.get("authoritative_run_count_before")
            == facts.get("authoritative_run_count_after"),
            "MySQL authority changed during dependency outage",
        )
        if proof_id == "qdrant_outage":
            point_id = proof_facts("qdrant_point").get("point_id")
            require(
                isinstance(point_id, str)
                and bool(point_id)
                and facts.get("point_id") == point_id,
                "preserved Qdrant point is not cross-bound",
            )
            require(
                facts.get("point_present_after") is True,
                "Qdrant point was not recovered",
            )


def _validate_raw_proofs(payload: dict[str, Any], errors: list[str]) -> set[str]:
    raw = _mapping(payload.get("raw_proofs"))
    _require_exact_fields(
        raw, RAW_PROOFS_FIELDS, label="raw proofs envelope", errors=errors
    )
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
        _require_exact_fields(
            proof,
            RAW_PROOF_RECORD_FIELDS,
            label=f"{label} record",
            errors=errors,
        )
        expected_source = RAW_PROOF_SOURCE_BY_ID.get(proof_id)
        if (
            proof.get("source") not in RAW_PROOF_SOURCES
            or proof.get("source") != expected_source
        ):
            errors.append(f"{label}: source is invalid for this proof")
        if proof.get("media_type") != "application/json":
            errors.append(f"{label}: media_type must be application/json")
        capture = _mapping(proof.get("capture"))
        _require_exact_fields(
            capture,
            CAPTURE_FIELDS,
            label=f"{label} capture",
            errors=errors,
        )
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
        expected_fact_keys = PROOF_FACT_KEYS.get(proof_id)
        if expected_fact_keys is not None and set(facts) != expected_fact_keys:
            errors.append(
                f"{label}: capture facts fields do not match the closed contract"
            )
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
        _require_exact_fields(case, RECOVERY_FIELDS, label=label, errors=errors)
        for field in (
            "proven",
            "authority_consistent",
            "no_duplicate_business_outcome",
        ):
            _require_boolean(case, field, label=label, errors=errors)
        raw_proof_value = case.get("raw_proof_ids")
        raw_proof_ids = _string_set(raw_proof_value)
        if raw_proof_value != [case_name]:
            errors.append(f"{label}: must reference exactly its named raw proof")
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
    _require_exact_fields(
        payload, EVIDENCE_FIELDS, label="evidence top-level", errors=errors
    )
    _scan_evidence_safety(payload, errors=errors)
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
    _require_exact_fields(
        compose,
        COMPOSE_EVIDENCE_FIELDS,
        label="Compose evidence",
        errors=errors,
    )
    _validate_host_runtime(compose, errors)
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
    _validate_runtime_inventory(compose, source_commit=expected_commit, errors=errors)

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
    _require_exact_fields(trace, TRACE_FIELDS, label="trace", errors=errors)
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
    operation_otel_traces = _mapping(trace.get("operation_otel_trace_ids"))
    operation_otel_values = list(operation_otel_traces.values())
    if set(operation_otel_traces) != REQUIRED_OPERATION_TRACES:
        errors.append("production path evidence requires every operation OTel trace id")
    if (
        any(
            not isinstance(value, str)
            or not OTEL_TRACE_ID_PATTERN.fullmatch(value)
            or int(value, 16) == 0
            for value in operation_otel_values
        )
        or len(operation_otel_values) != len(set(operation_otel_values))
        or operation_otel_traces.get("dagster") != otel_trace_id
    ):
        errors.append(
            "production path evidence requires five distinct operation OTel trace ids"
        )
    linked_components = trace.get("linked_components")
    linked_set = _string_set(linked_components)
    if (
        not isinstance(linked_components, list)
        or len(linked_components) != len(linked_set)
        or linked_set != REQUIRED_TRACE_COMPONENTS
    ):
        errors.append(
            "trace linked components must exactly match the proven component union"
        )

    identity = _mapping(payload.get("identity"))
    _require_exact_fields(identity, IDENTITY_FIELDS, label="identity", errors=errors)
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
    _require_exact_fields(adapters, ADAPTER_FIELDS, label="adapters", errors=errors)
    dagster = _mapping(adapters.get("dagster"))
    _require_exact_fields(
        dagster,
        ADAPTER_SECTION_FIELDS["dagster"],
        label="dagster adapter",
        errors=errors,
    )
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
    _require_exact_fields(
        object_storage,
        ADAPTER_SECTION_FIELDS["object_storage"],
        label="object storage adapter",
        errors=errors,
    )
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
    _require_exact_fields(
        qdrant,
        ADAPTER_SECTION_FIELDS["qdrant"],
        label="qdrant adapter",
        errors=errors,
    )
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
    _require_exact_fields(
        callback,
        ADAPTER_SECTION_FIELDS["external_callback"],
        label="external callback adapter",
        errors=errors,
    )
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
    _require_exact_fields(
        observability, OBSERVABILITY_FIELDS, label="observability", errors=errors
    )
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
    observed_services = _nonempty_strings(observability.get("services"))
    observed_set = set(observed_services)
    required_observed = {
        "auris-flow-bff",
        "auris-flow-worker",
        "auris-flow-dagster-code",
    }
    if observed_set != required_observed or len(observed_services) != len(
        required_observed
    ):
        errors.append(
            "cross-service OTel trace must include exactly BFF, Worker and Dagster code"
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
