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
from typing import Any, Mapping
from urllib.parse import urlparse

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "production" / "compose.yaml"
GATE_COMPOSE = ROOT / "production" / "tests" / "production-path-gate.compose.yaml"
RUNTIME_DRIVER = ROOT / "scripts" / "verify_production_path_runtime.py"
EVIDENCE_PATH = ROOT / "build" / "release-evidence" / "production-path-gate.json"
RELEASE_EVIDENCE_PATH = (
    ROOT / "build" / "release" / "final-runtime" / "production-path-gate.json"
)
CONTRACT_SCHEMA = "auris.production-path-gate-contract.v1"
EVIDENCE_SCHEMA = "auris.production-path-gate.v1"
RELEASE_EVIDENCE_SCHEMA = "auris.production-path-release-gate.v1"
RELEASE_BINDING_SCHEMA = "auris.release-runtime-binding.v1"
RELEASE_IMAGE_LOCK_SCHEMA = "auris.release-image-lock.v1"
# This assertion is reviewable source, not a runtime shortcut: activation also
# requires every checked-in runtime source below, a ready Compose contract and a
# complete raw-proof/recovery envelope whose canonical hashes are recomputed.
RAW_PROOF_BINDING_IMPLEMENTED = True
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RELEASE_TAG_PATTERN = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-rc\.[1-9]\d*)?$"
)
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
        "dagster-storage-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
        "otel-collector",
        "tempo",
        "alertmanager",
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
    "scripts/verify_production_path_gate.py",
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
        "dead_letter_retry",
        "dead_letter_retry_qdrant",
        "dead_letter_retry_trace",
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
    "dead_letter_retry": "mysql",
    "dead_letter_retry_qdrant": "qdrant-http",
    "dead_letter_retry_trace": "tempo-http",
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
    "dead_letter_retry": frozenset(
        {
            "source_run_id_sha256",
            "retry_run_id_sha256",
            "source_event_id",
            "retry_event_id",
            "source_event_aggregate_id_sha256",
            "retry_event_aggregate_id_sha256",
            "source_payload_dead_letter_event_id",
            "retry_payload_retry_of_event_id",
            "retry_payload_retry_of_run_id_sha256",
            "source_trace_id",
            "retry_payload_retry_of_trace_id",
            "source_status_before",
            "source_status_after",
            "source_terminal_reason",
            "source_status_version",
            "source_event_status",
            "source_delivery_state",
            "source_error_code",
            "source_last_error_sha256",
            "source_lease_generation",
            "source_dead_letter_attempt_count",
            "source_snapshot_sha256_before",
            "source_snapshot_sha256_after",
            "source_attempt_ledger_sha256_before",
            "source_attempt_ledger_sha256_after",
            "retry_response_replayed",
            "first_response_sha256",
            "replay_response_sha256",
            "stored_response_sha256",
            "idempotency_record_count",
            "idempotency_state",
            "idempotency_status_code",
            "idempotency_request_sha256",
            "expected_idempotency_request_sha256",
            "idempotency_response_run_id_sha256",
            "idempotency_user_sha256",
            "expected_retry_idempotency_key_sha256",
            "retry_run_count",
            "retry_event_count",
            "retry_dispatch_attempt_count",
            "retry_event_otel_trace_id",
            "retry_dispatch_idempotency_key_sha256",
            "retry_dispatch_request_sha256",
            "retry_attempt_request_sha256",
            "retry_attempt_id_sha256",
            "retry_expected_attempt_id_sha256",
            "retry_point_id_sha256",
            "retry_dispatch_payload_sha256",
            "retry_attempt_payload_sha256",
            "retry_event_status",
            "retry_run_status",
            "retry_trace_inherited",
            "retry_audit_count",
            "retry_audit_actor_sha256",
            "retry_audit_idempotency_key_sha256",
            "retry_audit_trace_matches",
            "retry_audit_lineage_matches",
        }
    )
    | AUTHORITY_FACT_KEYS,
    "dead_letter_retry_qdrant": frozenset(
        {
            "http_status",
            "collection",
            "point_id_sha256",
            "dispatch_point_id_sha256",
            "attempt_point_id_sha256",
            "payload_sha256",
            "dispatch_payload_sha256",
            "attempt_payload_sha256",
            "retry_run_id_sha256",
            "retry_event_id",
            "dispatch_idempotency_key_sha256",
            "dispatch_request_sha256",
            "attempt_request_sha256",
            "attempt_id_sha256",
            "tenant_id",
            "project_id",
            "trace_id",
            "filtered_point_count",
            "point_occurrences",
            "cross_tenant_count",
            "cross_project_count",
            "scope_match",
            "dispatch_receipt_match",
            "attempt_receipt_match",
            "payload_hash_match",
        }
    ),
    "dead_letter_retry_trace": frozenset(
        {
            "http_status",
            "observed_business_trace_id",
            "bff_span_id_sha256",
            "outbox_parent_span_id_sha256",
            "outbox_span_id_sha256",
            "adapter_parent_span_id_sha256",
            "adapter_span_id_sha256",
            "qdrant_parent_span_id_sha256",
            "bff_server_span_count",
            "bff_server_http_method",
            "bff_server_route",
            "outbox_process_span_count",
            "adapter_dispatch_span_count",
            "qdrant_client_span_count",
            "qdrant_write_span_count",
            "otel_trace_id",
            "services",
            "components",
            "component_signals",
            "span_count",
            "client_span_count",
        }
    ),
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
        "dead_letter_retry",
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
        "alertmanager",
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
        "dagster-storage-bootstrap",
        "minio-volume-init",
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
    "minio-volume-init": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    "minio-bootstrap": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
    "qdrant": "qdrant/qdrant:v1.14.1",
    "keycloak": "quay.io/keycloak/keycloak:26.2.5",
    "otel-collector": "otel/opentelemetry-collector-contrib:0.128.0",
    "tempo": "grafana/tempo:2.8.0",
    "alertmanager": "prom/alertmanager:v0.28.1",
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
RELEASE_EVIDENCE_FIELDS = EVIDENCE_FIELDS | frozenset({"release_tag", "release"})
RELEASE_BINDING_FIELDS = frozenset(
    {
        "schema_version",
        "release_tag",
        "source_commit",
        "image_lock",
        "image_lock_sha256",
        "image_lock_schema_version",
        "release_compose",
        "release_compose_sha256",
        "runtime_images",
    }
)
RELEASE_RUNTIME_IMAGE_FIELDS = frozenset(
    {
        "lock_service",
        "configured_image",
        "repo_digest",
        "image_id",
        "container_id_sha256",
    }
)
RELEASE_GATE_IMAGE_ALIASES = {
    "production-gate-callback": "bff",
    "production-gate-embedding": "bff",
    "production-path-seed": "bff",
    "production-path-verifier": "bff",
}
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
    if service in {
        "dagster-storage-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
    }:
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
    compose: dict[str, Any],
    *,
    source_commit: str,
    errors: list[str],
    expected_configured_images: Mapping[str, str] | None = None,
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
    observed_service_names = set(running) | set(completed)
    if expected_configured_images is not None and set(expected_configured_images) != (
        observed_service_names
    ):
        errors.append("release runtime image expectations do not match the inventory")
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
            expected_image = (
                expected_configured_images.get(service)
                if expected_configured_images is not None
                else _expected_service_image(service, source_commit)
            )
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
            if expected_configured_images is not None and isinstance(
                expected_image, str
            ):
                expected_repository = _normalized_image_repository(expected_image)
                expected_digest = expected_image.rsplit("@", 1)[-1]
                if not repo_digests or not any(
                    _normalized_image_repository(str(item)) == expected_repository
                    and str(item).rsplit("@", 1)[-1] == expected_digest
                    for item in repo_digests
                ):
                    errors.append(
                        f"Compose repository digest does not match the release lock: {service}"
                    )
            elif service in EXPECTED_EXTERNAL_SERVICE_IMAGES:
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
    audio_fixture = _mapping(contract.get("audio_inference_fixture"))
    if audio_fixture != {
        "service": "production-gate-embedding",
        "transport": "https",
        "reference_protocol_only": True,
        "model_quality_certified": False,
    }:
        errors.append(
            "gate audio inference fixture must be HTTPS protocol-only and must not certify model quality"
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
    audio_service = _mapping(services.get("production-gate-embedding"))
    audio_environment = _mapping(audio_service.get("environment"))
    if (
        audio_environment.get("PYTHONPATH") != "/app"
        or audio_environment.get("AUDIO_INFERENCE_API_TOKEN_FILE")
        != "/run/secrets/audio_inference_api_token"
        or audio_environment.get("AUDIO_INFERENCE_PROVIDER")
        != "audio_intelligence_default"
        or audio_environment.get("AUDIO_INFERENCE_MODEL") != "audio-v2.3.1"
    ):
        errors.append("production gate audio protocol fixture configuration is invalid")
    callback_environment = _mapping(
        _mapping(services.get("production-gate-callback")).get("environment")
    )
    if callback_environment.get("PYTHONPATH") != "/app":
        errors.append(
            "production gate callback fixture must import the packaged BFF application"
        )
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
    elif proof_id == "dead_letter_retry":
        source_hash = str(facts.get("source_run_id_sha256") or "")
        retry_hash = str(facts.get("retry_run_id_sha256") or "")
        snapshot_before = str(facts.get("source_snapshot_sha256_before") or "")
        snapshot_after = str(facts.get("source_snapshot_sha256_after") or "")
        ledger_before = str(facts.get("source_attempt_ledger_sha256_before") or "")
        ledger_after = str(facts.get("source_attempt_ledger_sha256_after") or "")
        source_event_id = facts.get("source_event_id")
        retry_event_id = facts.get("retry_event_id")
        source_trace_id = facts.get("source_trace_id")
        required_hashes = (
            source_hash,
            retry_hash,
            str(facts.get("source_event_aggregate_id_sha256") or ""),
            str(facts.get("retry_event_aggregate_id_sha256") or ""),
            str(facts.get("retry_payload_retry_of_run_id_sha256") or ""),
            str(facts.get("source_last_error_sha256") or ""),
            snapshot_before,
            snapshot_after,
            ledger_before,
            ledger_after,
            str(facts.get("idempotency_request_sha256") or ""),
            str(facts.get("expected_idempotency_request_sha256") or ""),
            str(facts.get("idempotency_response_run_id_sha256") or ""),
            str(facts.get("idempotency_user_sha256") or ""),
            str(facts.get("expected_retry_idempotency_key_sha256") or ""),
            str(facts.get("retry_audit_actor_sha256") or ""),
            str(facts.get("retry_audit_idempotency_key_sha256") or ""),
            str(facts.get("first_response_sha256") or ""),
            str(facts.get("replay_response_sha256") or ""),
            str(facts.get("stored_response_sha256") or ""),
            str(facts.get("retry_dispatch_idempotency_key_sha256") or ""),
            str(facts.get("retry_dispatch_request_sha256") or ""),
            str(facts.get("retry_attempt_request_sha256") or ""),
            str(facts.get("retry_attempt_id_sha256") or ""),
            str(facts.get("retry_expected_attempt_id_sha256") or ""),
            str(facts.get("retry_point_id_sha256") or ""),
            str(facts.get("retry_dispatch_payload_sha256") or ""),
            str(facts.get("retry_attempt_payload_sha256") or ""),
        )
        require(
            all(bool(SHA256_PATTERN.fullmatch(value)) for value in required_hashes)
            and source_hash != retry_hash,
            "dead-letter source and retry identities are invalid",
        )
        require(
            _positive_int(source_event_id)
            and _positive_int(retry_event_id)
            and source_event_id != retry_event_id,
            "dead-letter source and retry event identities are invalid",
        )
        require(
            facts.get("source_event_aggregate_id_sha256") == source_hash
            and facts.get("retry_event_aggregate_id_sha256") == retry_hash
            and facts.get("source_payload_dead_letter_event_id") == source_event_id
            and facts.get("retry_payload_retry_of_event_id") == source_event_id
            and facts.get("retry_payload_retry_of_run_id_sha256") == source_hash,
            "dead-letter source and retry lineage is not cross-bound",
        )
        require(
            isinstance(source_trace_id, str)
            and BUSINESS_TRACE_ID_PATTERN.fullmatch(source_trace_id) is not None
            and facts.get("retry_payload_retry_of_trace_id") == source_trace_id,
            "dead-letter retry business trace lineage is invalid",
        )
        require(
            facts.get("source_status_before") == "failed"
            and facts.get("source_status_after") == "failed"
            and facts.get("source_terminal_reason") == "outbox_dispatch_dead_letter"
            and _positive_int(facts.get("source_status_version"), minimum=3)
            and facts.get("source_event_status") == "dead_letter"
            and facts.get("source_delivery_state") == "failed"
            and facts.get("source_error_code") == "QDRANT_PAYLOAD_INVALID"
            and facts.get("source_lease_generation") == 1
            and facts.get("source_dead_letter_attempt_count") == 1,
            "source dead-letter terminal decision is invalid",
        )
        require(
            bool(SHA256_PATTERN.fullmatch(snapshot_before))
            and snapshot_after == snapshot_before
            and bool(SHA256_PATTERN.fullmatch(ledger_before))
            and ledger_after == ledger_before,
            "source dead-letter decision or attempt ledger mutated after retry",
        )
        require(
            facts.get("retry_response_replayed") is True
            and facts.get("first_response_sha256")
            == facts.get("replay_response_sha256")
            == facts.get("stored_response_sha256")
            and facts.get("idempotency_record_count") == 1
            and facts.get("idempotency_state") == "completed"
            and facts.get("idempotency_status_code") == 202
            and facts.get("idempotency_request_sha256")
            == facts.get("expected_idempotency_request_sha256")
            and facts.get("idempotency_response_run_id_sha256") == retry_hash
            and facts.get("retry_audit_actor_sha256")
            == facts.get("idempotency_user_sha256")
            and facts.get("retry_audit_idempotency_key_sha256")
            == facts.get("expected_retry_idempotency_key_sha256")
            and facts.get("retry_run_count") == 1
            and facts.get("retry_event_count") == 1
            and facts.get("retry_dispatch_attempt_count") == 1,
            "manual retry was not idempotent or dispatched exactly once",
        )
        require(
            OTEL_TRACE_ID_PATTERN.fullmatch(
                str(facts.get("retry_event_otel_trace_id") or "")
            )
            is not None
            and facts.get("retry_dispatch_request_sha256")
            == facts.get("retry_attempt_request_sha256")
            and facts.get("retry_attempt_id_sha256")
            == facts.get("retry_expected_attempt_id_sha256")
            and facts.get("retry_dispatch_payload_sha256")
            == facts.get("retry_attempt_payload_sha256"),
            "manual retry Outbox and attempt receipts are not directly bound",
        )
        require(
            facts.get("retry_event_status") == "processed"
            and facts.get("retry_run_status") == "success"
            and facts.get("retry_trace_inherited") is True,
            "manual retry did not complete with inherited trace lineage",
        )
        require(
            facts.get("retry_audit_count") == 1
            and facts.get("retry_audit_trace_matches") is True
            and facts.get("retry_audit_lineage_matches") is True,
            "manual retry audit evidence is invalid",
        )
        require(
            _positive_int(facts.get("authoritative_run_count_before"))
            and facts.get("authoritative_run_count_before")
            == facts.get("authoritative_run_count_after"),
            "baseline MySQL authority changed during dead-letter recovery",
        )
    elif proof_id == "dead_letter_retry_qdrant":
        mysql_facts = proof_facts("dead_letter_retry")
        hash_fields = (
            "point_id_sha256",
            "dispatch_point_id_sha256",
            "attempt_point_id_sha256",
            "payload_sha256",
            "dispatch_payload_sha256",
            "attempt_payload_sha256",
            "retry_run_id_sha256",
            "dispatch_idempotency_key_sha256",
            "dispatch_request_sha256",
            "attempt_request_sha256",
            "attempt_id_sha256",
        )
        require(
            all(
                bool(SHA256_PATTERN.fullmatch(str(facts.get(field) or "")))
                for field in hash_fields
            ),
            "Qdrant retry receipt hashes are invalid",
        )
        require(
            facts.get("http_status") == 200
            and isinstance(facts.get("collection"), str)
            and bool(facts.get("collection")),
            "Qdrant retry response is invalid",
        )
        require(
            facts.get("retry_run_id_sha256") == mysql_facts.get("retry_run_id_sha256")
            and facts.get("retry_event_id") == mysql_facts.get("retry_event_id")
            and facts.get("trace_id") == mysql_facts.get("source_trace_id"),
            "Qdrant retry proof is not cross-bound to the MySQL retry",
        )
        require(
            (facts.get("tenant_id"), facts.get("project_id")) == scope
            and facts.get("scope_match") is True
            and facts.get("cross_tenant_count") == 0
            and facts.get("cross_project_count") == 0,
            "Qdrant retry scope isolation is invalid",
        )
        require(
            facts.get("filtered_point_count") == 1
            and facts.get("point_occurrences") == 1
            and facts.get("dispatch_receipt_match") is True
            and facts.get("attempt_receipt_match") is True
            and facts.get("payload_hash_match") is True
            and facts.get("point_id_sha256")
            == facts.get("dispatch_point_id_sha256")
            == facts.get("attempt_point_id_sha256")
            == mysql_facts.get("retry_point_id_sha256")
            and facts.get("payload_sha256")
            == facts.get("dispatch_payload_sha256")
            == facts.get("attempt_payload_sha256")
            == mysql_facts.get("retry_dispatch_payload_sha256")
            and facts.get("dispatch_idempotency_key_sha256")
            == mysql_facts.get("retry_dispatch_idempotency_key_sha256")
            and facts.get("dispatch_request_sha256")
            == mysql_facts.get("retry_dispatch_request_sha256")
            and facts.get("attempt_request_sha256")
            == mysql_facts.get("retry_attempt_request_sha256")
            and facts.get("attempt_id_sha256")
            == mysql_facts.get("retry_attempt_id_sha256")
            and facts.get("dispatch_request_sha256")
            == facts.get("attempt_request_sha256"),
            "Qdrant retry did not prove one cross-bound remote outcome",
        )
    elif proof_id == "dead_letter_retry_trace":
        mysql_facts = proof_facts("dead_letter_retry")
        expected_components = OPERATION_OTEL_COMPONENTS["qdrant"]
        expected_services = OPERATION_OTEL_SERVICES["qdrant"]
        services = _nonempty_strings(facts.get("services"))
        components = _nonempty_strings(facts.get("components"))
        signals = _mapping(facts.get("component_signals"))
        otel_trace_id = str(facts.get("otel_trace_id") or "")
        require(
            facts.get("http_status") == 200
            and OTEL_TRACE_ID_PATTERN.fullmatch(otel_trace_id) is not None
            and int(otel_trace_id, 16) != 0
            and otel_trace_id not in set(operation_otel_trace_ids.values())
            and otel_trace_id == mysql_facts.get("retry_event_otel_trace_id"),
            "dead-letter retry Tempo trace id is invalid or reused",
        )
        require(
            facts.get("observed_business_trace_id")
            == mysql_facts.get("source_trace_id"),
            "dead-letter retry Tempo trace lacks the source business trace",
        )
        lineage_hash_fields = (
            "bff_span_id_sha256",
            "outbox_parent_span_id_sha256",
            "outbox_span_id_sha256",
            "adapter_parent_span_id_sha256",
            "adapter_span_id_sha256",
            "qdrant_parent_span_id_sha256",
        )
        require(
            all(
                bool(SHA256_PATTERN.fullmatch(str(facts.get(field) or "")))
                for field in lineage_hash_fields
            )
            and facts.get("bff_span_id_sha256")
            == facts.get("outbox_parent_span_id_sha256")
            and facts.get("outbox_span_id_sha256")
            == facts.get("adapter_parent_span_id_sha256")
            and facts.get("adapter_span_id_sha256")
            == facts.get("qdrant_parent_span_id_sha256")
            and facts.get("outbox_process_span_count") == 1
            and facts.get("adapter_dispatch_span_count") == 1
            and _positive_int(facts.get("qdrant_client_span_count"))
            and facts.get("qdrant_write_span_count") == 1,
            "dead-letter retry Tempo parent chain is invalid",
        )
        require(
            facts.get("bff_server_span_count") == 1
            and facts.get("bff_server_http_method") == "POST"
            and facts.get("bff_server_route") == "/api/v1/runs/{id}/retries",
            "dead-letter retry Tempo BFF retry server span is invalid",
        )
        require(
            len(services) == len(set(services))
            and set(services) == expected_services
            and len(components) == len(set(components))
            and set(components) == expected_components
            and set(signals) == expected_components
            and _positive_int(facts.get("span_count"))
            and _positive_int(facts.get("client_span_count"))
            and all(
                signals.get(component) == [OTEL_SIGNAL_BY_COMPONENT[component]]
                for component in expected_components
            ),
            "dead-letter retry Tempo trace lacks the exact Qdrant operation chain",
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
        expected_raw_proof_ids = (
            [
                "dead_letter_retry",
                "dead_letter_retry_qdrant",
                "dead_letter_retry_trace",
            ]
            if case_name == "dead_letter_retry"
            else [case_name]
        )
        if raw_proof_value != expected_raw_proof_ids:
            errors.append(f"{label}: raw proof references are incomplete or unordered")
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
    evidence: object,
    *,
    root: Path = ROOT,
    expected_commit: str,
    expected_execution_environment: str = "production-compose",
    expected_compose_base: str = "production/compose.yaml",
    expected_compose_path: Path | None = None,
    expected_configured_images: Mapping[str, str] | None = None,
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
    if payload.get("execution_environment") != expected_execution_environment:
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
    if compose.get("base") != expected_compose_base:
        errors.append(f"evidence must bind {expected_compose_base}")
    if compose.get("overlay") != "production/tests/production-path-gate.compose.yaml":
        errors.append("evidence must bind the production path gate overlay")
    for field, path in (
        ("base_sha256", expected_compose_path or root / expected_compose_base),
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
    _validate_runtime_inventory(
        compose,
        source_commit=expected_commit,
        errors=errors,
        expected_configured_images=expected_configured_images,
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


def _release_image_reference(value: object) -> str | None:
    if not isinstance(value, str) or "${" in value or value.count("@sha256:") != 1:
        return None
    name_and_tag, digest = value.rsplit("@sha256:", 1)
    final_segment = name_and_tag.rsplit("/", 1)[-1]
    if (
        ":" not in final_segment
        or not final_segment.rsplit(":", 1)[1]
        or final_segment.rsplit(":", 1)[1].casefold() == "latest"
        or not SHA256_PATTERN.fullmatch(digest)
    ):
        return None
    return value


def _load_release_document(
    path: Path, *, label: str, errors: list[str]
) -> dict[str, Any]:
    try:
        return _mapping(_load_json(path))
    except ValueError as exc:
        errors.append(f"{label} is unavailable or invalid: {exc}")
        return {}


def validate_release_evidence(
    evidence: object,
    *,
    root: Path = ROOT,
    expected_commit: str,
    expected_release_tag: str,
    release_compose_path: Path | None = None,
    image_lock_path: Path | None = None,
) -> list[str]:
    """Validate the closed prebuilt-release envelope and its full runtime proof.

    The release artifact embeds the same six runtime sections as the local
    ``auris.production-path-gate.v1`` artifact.  After validating the immutable
    release binding, this function projects those sections into the old closed
    schema and invokes :func:`validate_evidence`; proof semantics therefore have
    one implementation and cannot drift between local and release gates.
    """

    errors: list[str] = []
    payload = _mapping(evidence)
    _require_exact_fields(
        payload,
        RELEASE_EVIDENCE_FIELDS,
        label="release evidence top-level",
        errors=errors,
    )
    _scan_evidence_safety(payload, errors=errors)
    if payload.get("schema_version") != RELEASE_EVIDENCE_SCHEMA:
        errors.append(
            f"release evidence schema_version must be {RELEASE_EVIDENCE_SCHEMA}"
        )
    if payload.get("status") != "ok":
        errors.append("release production path evidence status must be ok")
    if not COMMIT_PATTERN.fullmatch(expected_commit):
        errors.append("expected source commit must be an exact lowercase Git SHA")
    if payload.get("source_commit") != expected_commit:
        errors.append("release evidence is not bound to the expected source commit")
    if not RELEASE_TAG_PATTERN.fullmatch(expected_release_tag):
        errors.append("expected release tag must be SemVer or an rc.N prerelease")
    if payload.get("release_tag") != expected_release_tag:
        errors.append("release evidence is not bound to the expected release tag")
    if payload.get("execution_environment") != "production-compose-prebuilt-release":
        errors.append(
            "release evidence must come from the prebuilt production Compose path"
        )
    if payload.get("producer") != "scripts/verify_production_path_runtime.py":
        errors.append(
            "release evidence producer must be the production path runtime driver"
        )

    compose = _mapping(payload.get("compose"))
    _require_exact_fields(
        compose,
        COMPOSE_EVIDENCE_FIELDS,
        label="release Compose evidence",
        errors=errors,
    )
    _validate_host_runtime(compose, errors)
    if compose.get("base") != "production/compose.release.json":
        errors.append("release evidence must bind production/compose.release.json")
    if compose.get("overlay") != "production/tests/production-path-gate.compose.yaml":
        errors.append("release evidence must bind the production path gate overlay")
    if not SHA256_PATTERN.fullmatch(str(compose.get("rendered_config_sha256") or "")):
        errors.append("rendered release Compose config hash is missing")

    release = _mapping(payload.get("release"))
    _require_exact_fields(
        release,
        RELEASE_BINDING_FIELDS,
        label="release runtime binding",
        errors=errors,
    )
    if release.get("schema_version") != RELEASE_BINDING_SCHEMA:
        errors.append("release runtime binding schema is invalid")
    if release.get("release_tag") != expected_release_tag:
        errors.append(
            "release runtime binding tag does not match the requested release"
        )
    if release.get("source_commit") != expected_commit:
        errors.append(
            "release runtime binding commit does not match the requested release"
        )
    if release.get("image_lock") != "build/release/images.lock.json":
        errors.append("release runtime binding image lock path is not canonical")
    if release.get("image_lock_schema_version") != RELEASE_IMAGE_LOCK_SCHEMA:
        errors.append("release runtime binding image lock schema is invalid")
    if release.get("release_compose") != "production/compose.release.json":
        errors.append("release runtime binding Compose path is not canonical")

    resolved_lock = image_lock_path or root / "build" / "release" / "images.lock.json"
    resolved_compose = (
        release_compose_path or root / "production" / "compose.release.json"
    )
    lock_document = _load_release_document(
        resolved_lock,
        label="release image lock",
        errors=errors,
    )
    compose_document = _load_release_document(
        resolved_compose,
        label="rendered release Compose",
        errors=errors,
    )
    if lock_document and set(lock_document) != {
        "schema_version",
        "release_tag",
        "source_commit",
        "images",
    }:
        errors.append("release image lock fields do not match the closed contract")
    if lock_document.get("schema_version") != RELEASE_IMAGE_LOCK_SCHEMA:
        errors.append("release image lock schema is invalid")
    if lock_document.get("release_tag") != expected_release_tag:
        errors.append("release image lock tag does not match the requested release")
    if lock_document.get("source_commit") != expected_commit:
        errors.append("release image lock commit does not match the requested release")

    raw_images = lock_document.get("images")
    images: dict[str, str] = {}
    if not isinstance(raw_images, dict) or not raw_images:
        errors.append("release image lock must contain a non-empty image map")
    else:
        for service, reference in sorted(raw_images.items()):
            validated = _release_image_reference(reference)
            if (
                not isinstance(service, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", service) is None
                or validated is None
            ):
                errors.append("release image lock contains an invalid service image")
                continue
            images[service] = validated

    expected_lock_services = (
        REQUIRED_RUNNING_SERVICES | REQUIRED_COMPLETED_SERVICES
    ) - set(RELEASE_GATE_IMAGE_ALIASES)
    if set(images) != expected_lock_services:
        errors.append(
            "release image lock service set does not match the runtime contract"
        )

    if resolved_lock.is_file() and not resolved_lock.is_symlink():
        image_lock_sha256 = _sha256_file(resolved_lock)
        if release.get("image_lock_sha256") != image_lock_sha256:
            errors.append("release image lock hash does not match the supplied lock")
    if resolved_compose.is_file() and not resolved_compose.is_symlink():
        release_compose_sha256 = _sha256_file(resolved_compose)
        if release.get("release_compose_sha256") != release_compose_sha256:
            errors.append("release Compose hash does not match the supplied document")
        if compose.get("base_sha256") != release_compose_sha256:
            errors.append(
                "release Compose evidence hash does not match the supplied document"
            )
    overlay_path = root / "production" / "tests" / "production-path-gate.compose.yaml"
    if not overlay_path.is_file() or overlay_path.is_symlink():
        errors.append("release gate Compose overlay is missing")
    elif compose.get("overlay_sha256") != _sha256_file(overlay_path):
        errors.append(
            "release gate Compose overlay hash does not match checked-in source"
        )

    release_metadata = _mapping(compose_document.get("x-auris-release"))
    if release_metadata != {
        "schema_version": RELEASE_IMAGE_LOCK_SCHEMA,
        "release_tag": expected_release_tag,
        "source_commit": expected_commit,
    }:
        errors.append("rendered release Compose metadata is not lock-bound")
    compose_services = _mapping(compose_document.get("services"))
    if set(compose_services) != set(images):
        errors.append("rendered release Compose services do not match the image lock")
    for service, reference in images.items():
        service_document = _mapping(compose_services.get(service))
        if service_document.get("image") != reference or "build" in service_document:
            errors.append(f"rendered release Compose image is not immutable: {service}")

    runtime = _mapping(compose.get("runtime"))
    observations = {
        **_mapping(runtime.get("running_services")),
        **_mapping(runtime.get("completed_services")),
    }
    expected_runtime_images: dict[str, str] = {}
    for service in sorted(observations):
        lock_service = RELEASE_GATE_IMAGE_ALIASES.get(service, service)
        reference = images.get(lock_service)
        if reference is None:
            errors.append(f"release runtime service has no image lock entry: {service}")
            continue
        expected_runtime_images[service] = reference
    expected_rendered_services = set(images) | set(RELEASE_GATE_IMAGE_ALIASES)
    if _string_set(compose.get("services")) != expected_rendered_services:
        errors.append(
            "release rendered service inventory does not match the image lock"
        )

    runtime_images = _mapping(release.get("runtime_images"))
    if set(runtime_images) != set(observations):
        errors.append("release runtime image binding set is incomplete")
    for service, observation_value in observations.items():
        observation = _mapping(observation_value)
        binding = _mapping(runtime_images.get(service))
        _require_exact_fields(
            binding,
            RELEASE_RUNTIME_IMAGE_FIELDS,
            label=f"release runtime image binding {service}",
            errors=errors,
        )
        lock_service = RELEASE_GATE_IMAGE_ALIASES.get(service, service)
        expected_image = images.get(lock_service)
        expected_repository = (
            _normalized_image_repository(expected_image)
            if isinstance(expected_image, str)
            else None
        )
        expected_digest = (
            expected_image.rsplit("@", 1)[-1]
            if isinstance(expected_image, str)
            else None
        )
        repo_digest = binding.get("repo_digest")
        if binding.get("lock_service") != lock_service:
            errors.append(f"release runtime lock service is invalid: {service}")
        if binding.get("configured_image") != expected_image:
            errors.append(
                f"release runtime configured image is not lock-bound: {service}"
            )
        if (
            not isinstance(repo_digest, str)
            or not REPO_DIGEST_PATTERN.fullmatch(repo_digest)
            or _normalized_image_repository(repo_digest) != expected_repository
            or repo_digest.rsplit("@", 1)[-1] != expected_digest
        ):
            errors.append(
                f"release runtime repository digest is not lock-bound: {service}"
            )
        for field in ("configured_image", "image_id", "container_id_sha256"):
            if binding.get(field) != observation.get(field):
                errors.append(
                    f"release runtime binding does not match observed {field}: {service}"
                )

    # Reuse the complete v1 validator for every identity, adapter, trace, raw
    # proof and recovery rule. Only the execution substrate/image expectation
    # changes; the old local validator's public defaults remain untouched.
    legacy_projection = {field: payload.get(field) for field in EVIDENCE_FIELDS}
    legacy_projection["schema_version"] = EVIDENCE_SCHEMA
    errors.extend(
        validate_evidence(
            legacy_projection,
            root=root,
            expected_commit=expected_commit,
            expected_execution_environment="production-compose-prebuilt-release",
            expected_compose_base="production/compose.release.json",
            expected_compose_path=resolved_compose,
            expected_configured_images=expected_runtime_images,
        )
    )
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
    release_evidence = subparsers.add_parser("release-evidence")
    release_evidence.add_argument("--artifact", type=Path, required=True)
    release_evidence.add_argument("--expected-commit", required=True)
    release_evidence.add_argument("--expected-release-tag", required=True)
    release_evidence.add_argument("--release-compose", type=Path, required=True)
    release_evidence.add_argument("--image-lock", type=Path, required=True)
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
        expected_artifact = (
            RELEASE_EVIDENCE_PATH
            if args.command == "release-evidence"
            else EVIDENCE_PATH
        )
        if artifact_path != expected_artifact.resolve():
            required_path = (
                "build/release/final-runtime/production-path-gate.json"
                if args.command == "release-evidence"
                else "build/release-evidence/production-path-gate.json"
            )
            return _emit_failure(
                status="blocked",
                blockers=[f"runtime evidence must use {required_path}"],
                exit_code=2,
            )
        if args.command == "release-evidence":
            release_compose_path = (
                (ROOT / args.release_compose).resolve()
                if not args.release_compose.is_absolute()
                else args.release_compose.resolve()
            )
            image_lock_path = (
                (ROOT / args.image_lock).resolve()
                if not args.image_lock.is_absolute()
                else args.image_lock.resolve()
            )
            try:
                release_compose_path.relative_to(ROOT.resolve())
                image_lock_path.relative_to(ROOT.resolve())
            except ValueError:
                return _emit_failure(
                    status="blocked",
                    blockers=[
                        "release validation inputs must remain inside the repository"
                    ],
                    exit_code=2,
                )
            errors = validate_release_evidence(
                _load_json(artifact_path),
                root=ROOT,
                expected_commit=args.expected_commit,
                expected_release_tag=args.expected_release_tag,
                release_compose_path=release_compose_path,
                image_lock_path=image_lock_path,
            )
        else:
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
