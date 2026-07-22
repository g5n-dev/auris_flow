#!/usr/bin/env python3
"""In-container verifier for the single production Compose release path.

The verifier deliberately persists only field-level observations.  Credentials,
cookies, CSRF values, raw HTTP bodies and local paths live in memory for the
shortest possible time and can never become runtime evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from sqlalchemy import Engine, create_engine, text

PHASES = (
    "initial",
    "fault-prepare",
    "fault-during",
    "fault-verify",
    "finalize",
)
DEPENDENCIES = (
    "mysql_restart",
    "worker_crash",
    "duplicate_delivery",
    "callback_timeout",
    "dead_letter_retry",
    "qdrant_outage",
    "redis_outage",
)
SCOPE = ("aurora_auto", "sales_qa")
OTEL_OPERATIONS = (
    "oidc",
    "dagster",
    "object_storage",
    "qdrant",
    "external_callback",
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
CAPTURE_SCHEMA = "auris.production-path.capture.v1"
STATE_SCHEMA = "auris.production-path.state.v1"
FAULT_VERIFICATION_CHECKPOINT_SCHEMA = (
    "auris.production-path.fault-verification-checkpoint.v1"
)
HOST_OBSERVATION_SCHEMA = "auris.production-path.host-observation.v1"
RAW_PROOFS_SCHEMA = "auris.production-path.raw-proofs.v1"
RUNTIME_FIELDS = frozenset(
    {"identity", "adapters", "observability", "trace", "raw_proofs", "recovery"}
)
PROOF_SOURCES = {
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
        }
    )
    | OPERATION_OTEL_FACT_KEYS,
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
BASELINE_PROOFS = (
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
)
REQUIRED_PROOFS = frozenset(PROOF_SOURCES)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
BUSINESS_TRACE_PATTERN = re.compile(r"^trace_[A-Za-z0-9._:-]{8,120}$")
SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TRACEPARENT_PATTERN = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
PERSONAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'])(?:/"
    r"Users/[^/\s]+|/"
    r"home/[^/\s]+|[A-Za-z]:\\"
    r"Users\\[^\\\s]+)(?:/|\\)"
)
FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "id_token",
        "key_material",
        "password",
        "raw_token",
        "refresh_token",
        "request_body",
        "response_body",
        "secret",
        "set_cookie",
        "token",
    }
)
SENSITIVE_ARTIFACT_KEY_MARKERS = (
    "access_key",
    "api_key",
    "authorization",
    "body",
    "cookie",
    "credential",
    "encryption_key",
    "headers",
    "key_material",
    "password",
    "private_key",
    "secret",
    "signing_key",
    "token",
)
SAFE_SENSITIVE_ARTIFACT_METADATA_KEYS = frozenset(
    {
        "authorization_endpoint_scheme",
        "cookie_http_only",
        "cookie_name",
        "cookie_secure",
        "claim_token_sha256_after",
        "claim_token_sha256_before",
        "rsa_signing_key_ids",
        "session_token_sha256",
        "signature_key_id",
        "token_endpoint_scheme",
    }
)
MAX_HTTP_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024


class VerifierFailure(RuntimeError):
    """A sanitized fail-closed error; never include remote bodies or credentials."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expected_request_sha256(method: str, path: str, body: Mapping[str, object]) -> str:
    body_bytes = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    fingerprint = {
        "method": method.upper(),
        "path": path,
        "query": [],
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }
    encoded = json.dumps(
        fingerprint,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerifierFailure(f"{label} is invalid")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerifierFailure(f"{label} is missing")
    return value


def _positive_int(value: object, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VerifierFailure(f"{label} is invalid")
    return value


def _json_mapping(value: object, label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise VerifierFailure(f"{label} is invalid") from None
    return _mapping(value, label)


def validate_phase_dependency(phase: str, dependency: str) -> None:
    if phase not in PHASES:
        raise VerifierFailure("verifier phase is invalid")
    if phase in {"initial", "finalize"}:
        if dependency != "none":
            raise VerifierFailure("non-fault phase must use dependency=none")
        return
    if dependency not in DEPENDENCIES:
        raise VerifierFailure("fault phase dependency is invalid")


def validate_artifact_value(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise VerifierFailure("artifact keys must be strings")
            normalized = re.sub(r"[^a-z0-9]+", "_", raw_key.casefold()).strip("_")
            if normalized not in SAFE_SENSITIVE_ARTIFACT_METADATA_KEYS and (
                normalized in FORBIDDEN_ARTIFACT_KEYS
                or any(
                    marker in normalized for marker in SENSITIVE_ARTIFACT_KEY_MARKERS
                )
            ):
                raise VerifierFailure("artifact contains a forbidden sensitive field")
            validate_artifact_value(item, path=(*path, raw_key))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_artifact_value(item, path=(*path, str(index)))
        return
    if isinstance(value, str) and PERSONAL_PATH_PATTERN.search(value):
        raise VerifierFailure("artifact contains a personal absolute path")


def capture_record(proof_id: str, observations: dict[str, Any]) -> dict[str, Any]:
    source = PROOF_SOURCES.get(proof_id)
    expected_fact_keys = PROOF_FACT_KEYS.get(proof_id)
    if (
        source is None
        or expected_fact_keys is None
        or not isinstance(observations, dict)
        or set(observations) != expected_fact_keys
    ):
        raise VerifierFailure("capture identity or observations are invalid")
    capture = {
        "schema_version": CAPTURE_SCHEMA,
        "proof_id": proof_id,
        "source": source,
        "observations": observations,
    }
    record = {
        "source": source,
        "media_type": "application/json",
        "capture": capture,
        "capture_sha256": canonical_sha256(capture),
        "facts_sha256": canonical_sha256(observations),
        "facts": observations,
    }
    validate_artifact_value(record)
    if len(canonical_bytes(record)) > MAX_ARTIFACT_BYTES:
        raise VerifierFailure("capture exceeds the evidence size limit")
    return record


def _json_file_bytes(payload: object) -> bytes:
    validate_artifact_value(payload)
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise VerifierFailure("artifact exceeds the evidence size limit")
    return encoded


def _require_artifact_parent(path: Path) -> None:
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise VerifierFailure("artifact parent is not a real directory")


def _atomic_replace(path: Path, encoded: bytes) -> None:
    _require_artifact_parent(path)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        path.chmod(0o644)
    except OSError as exc:
        raise VerifierFailure("artifact atomic write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_json_idempotent(path: Path, payload: object) -> None:
    encoded = _json_file_bytes(payload)
    _require_artifact_parent(path)
    if path.is_symlink():
        raise VerifierFailure("artifact target must not be a symlink")
    if path.exists():
        if not path.is_file():
            raise VerifierFailure("artifact target is not a regular file")
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise VerifierFailure("artifact read failed") from exc
        if current != encoded:
            raise VerifierFailure("existing artifact differs from the observed facts")
        return
    _atomic_replace(path, encoded)


def replace_json_state(path: Path, prior: object, updated: object) -> None:
    if path.is_symlink() or not path.is_file():
        raise VerifierFailure("state transition target is invalid")
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifierFailure("state transition source is invalid") from exc
    if current != prior:
        raise VerifierFailure("state changed concurrently or is inconsistent")
    _atomic_replace(path, _json_file_bytes(updated))


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerifierFailure(f"{label} is missing")
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise VerifierFailure(f"{label} exceeds the size limit")
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerifierFailure(f"{label} is invalid") from exc
    result = _mapping(payload, label)
    validate_artifact_value(result)
    return result


def load_host_observation(path: Path, dependency: str) -> dict[str, Any]:
    if dependency not in {"mysql_restart", "worker_crash"}:
        raise VerifierFailure("host observation dependency is invalid")
    payload = load_json_file(path, "host observation")
    expected_fields = {
        "schema_version",
        "dependency",
        "container_id_sha256",
        "started_at_before",
        "started_at_after",
    }
    if set(payload) != expected_fields:
        raise VerifierFailure("host observation fields are invalid")
    if (
        payload.get("schema_version") != HOST_OBSERVATION_SCHEMA
        or payload.get("dependency") != dependency
        or not SHA256_PATTERN.fullmatch(str(payload.get("container_id_sha256") or ""))
        or not isinstance(payload.get("started_at_before"), str)
        or not isinstance(payload.get("started_at_after"), str)
        or payload.get("started_at_before") == payload.get("started_at_after")
    ):
        raise VerifierFailure("host observation facts are invalid")
    return payload


def otel_trace_id_from_carrier(carrier: object) -> str:
    value = _mapping(carrier, "OTel carrier").get("traceparent")
    match = TRACEPARENT_PATTERN.fullmatch(str(value or ""))
    if not match:
        raise VerifierFailure("OTel trace carrier is invalid")
    trace_id, span_id, flags = match.groups()
    if int(trace_id, 16) == 0 or int(span_id, 16) == 0 or int(flags, 16) & 1 != 1:
        raise VerifierFailure("OTel trace carrier is not a sampled valid context")
    return trace_id


def _new_otel_trace_id() -> str:
    while True:
        trace_id = secrets.token_hex(16)
        if int(trace_id, 16) != 0:
            return trace_id


def _new_operation_otel_trace_ids() -> dict[str, str]:
    operation_trace_ids: dict[str, str] = {}
    allocated: set[str] = set()
    for operation in OTEL_OPERATIONS:
        while True:
            trace_id = _new_otel_trace_id()
            if trace_id not in allocated:
                allocated.add(trace_id)
                operation_trace_ids[operation] = trace_id
                break
    return operation_trace_ids


def sampled_traceparent(otel_trace_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", otel_trace_id) or int(otel_trace_id, 16) == 0:
        raise VerifierFailure("OTel gate trace id is invalid")
    while True:
        parent_span_id = secrets.token_hex(8)
        if int(parent_span_id, 16) != 0:
            return f"00-{otel_trace_id}-{parent_span_id}-01"


def runtime_fragment(sections: object) -> dict[str, Any]:
    payload = _mapping(sections, "runtime fragment")
    if set(payload) != RUNTIME_FIELDS:
        raise VerifierFailure("runtime fragment must contain exactly six sections")
    if any(not isinstance(payload.get(field), dict) for field in RUNTIME_FIELDS):
        raise VerifierFailure("runtime fragment section is invalid")
    validate_artifact_value(payload)
    return payload


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_selected_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and self.action is None:
            action = attributes.get("action")
            if action:
                self.action = html.unescape(action)
                self._in_selected_form = True
            return
        if tag == "input" and self._in_selected_form:
            name = attributes.get("name")
            if name:
                self.fields[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_selected_form:
            self._in_selected_form = False


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise VerifierFailure("HTTPS endpoint is invalid")
    port = parsed.port
    return f"https://{parsed.hostname}" + (f":{port}" if port and port != 443 else "")


def _scheme(url: object) -> str:
    if not isinstance(url, str):
        return ""
    return urlsplit(url).scheme


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    raise VerifierFailure("timestamp observation is invalid")


def _iso_or_none(value: object) -> str | None:
    return None if value is None else _iso(value)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DatabaseProbe:
    def __init__(self, database_url: str) -> None:
        try:
            self.engine: Engine = create_engine(database_url, pool_pre_ping=True)
        except Exception as exc:
            raise VerifierFailure("database probe configuration failed") from exc

    def rows(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> list[dict[str, Any]]:
        try:
            with self.engine.connect() as connection:
                result = connection.execute(text(statement), dict(parameters or {}))
                return [dict(row) for row in result.mappings().all()]
        except Exception as exc:
            raise VerifierFailure("database observation failed") from exc

    def one(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        rows = self.rows(statement, parameters)
        if len(rows) != 1:
            raise VerifierFailure("database observation cardinality is invalid")
        return rows[0]

    def execute(
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> int:
        try:
            with self.engine.begin() as connection:
                result = connection.execute(text(statement), dict(parameters or {}))
                return int(result.rowcount or 0)
        except Exception as exc:
            raise VerifierFailure("database fault injection failed") from exc


class GateConfig:
    def __init__(self, artifact_dir: Path, run_suffix: str) -> None:
        if not SUFFIX_PATTERN.fullmatch(run_suffix):
            raise VerifierFailure("run suffix is invalid")
        if not artifact_dir.is_dir() or artifact_dir.is_symlink():
            raise VerifierFailure("artifact directory is invalid")
        self.artifact_dir = artifact_dir
        self.run_suffix = run_suffix
        self.public_url = os.environ.get("AURIS_GATE_PUBLIC_URL", "").rstrip("/")
        self.callback_url = os.environ.get("AURIS_GATE_CALLBACK_URL", "").rstrip("/")
        self.embedding_url = os.environ.get("AURIS_GATE_EMBEDDING_URL", "").rstrip("/")
        self.tempo_url = os.environ.get("AURIS_GATE_TEMPO_URL", "").rstrip("/")
        self.qdrant_url = os.environ.get("QDRANT_URL", "").rstrip("/")
        self.object_endpoint = os.environ.get("OBJECT_STORAGE_ENDPOINT", "").rstrip("/")
        self.object_bucket = os.environ.get("OBJECT_STORAGE_BUCKET", "")
        self.source_commit = os.environ.get("AURIS_GATE_SOURCE_COMMIT", "")
        self.ca_file = os.environ.get("SSL_CERT_FILE", "")
        self.timeout_seconds = float(
            os.environ.get("AURIS_GATE_TIMEOUT_SECONDS", "240")
        )
        if (
            any(
                not value
                for value in (
                    self.public_url,
                    self.callback_url,
                    self.embedding_url,
                    self.tempo_url,
                    self.qdrant_url,
                    self.object_endpoint,
                    self.object_bucket,
                    self.ca_file,
                )
            )
            or _origin(self.public_url) != self.public_url
            or _origin(self.callback_url) != self.callback_url
            or _origin(self.embedding_url) != self.embedding_url
            or not COMMIT_PATTERN.fullmatch(self.source_commit)
            or not 30 <= self.timeout_seconds <= 600
        ):
            raise VerifierFailure("production gate environment is invalid")
        self.database = DatabaseProbe(
            _read_secret_env_file("DATABASE_URL_FILE", "database URL")
        )

    def state_path(self) -> Path:
        return self.artifact_dir / f"state-{self.run_suffix}.json"

    def capture_path(self, proof_id: str) -> Path:
        if proof_id not in REQUIRED_PROOFS:
            raise VerifierFailure("capture proof id is invalid")
        return self.artifact_dir / f"capture-{self.run_suffix}-{proof_id}.json"

    def host_observation_path(self, dependency: str) -> Path:
        if dependency not in DEPENDENCIES:
            raise VerifierFailure("host observation dependency is invalid")
        return self.artifact_dir / f"host-{self.run_suffix}-{dependency}.json"

    def runtime_path(self) -> Path:
        return self.artifact_dir / f"runtime-{self.run_suffix}.json"


def _read_secret_file(path_value: str, label: str) -> str:
    path = Path(path_value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise VerifierFailure(f"{label} reference is invalid")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise VerifierFailure(f"{label} could not be read") from exc
    if not value:
        raise VerifierFailure(f"{label} is empty")
    return value


def _read_secret_env_file(name: str, label: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise VerifierFailure(f"{label} reference is missing")
    return _read_secret_file(value, label)


class BrowserClient:
    def __init__(self, config: GateConfig) -> None:
        self.config = config
        self.client = httpx.Client(
            verify=config.ca_file,
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json"},
        )
        self.csrf_value: str | None = None

    def close(self) -> None:
        self.csrf_value = None
        self.client.cookies.clear()
        self.client.close()

    def _check_size(self, response: httpx.Response, label: str) -> None:
        content_length = response.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > MAX_HTTP_BYTES
        ):
            raise VerifierFailure(f"{label} response exceeds the size limit")
        if len(response.content) > MAX_HTTP_BYTES:
            raise VerifierFailure(f"{label} response exceeds the size limit")

    def raw(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
        data: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        label: str,
    ) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                url,
                headers=dict(headers or {}),
                content=content,
                data=dict(data) if data is not None else None,
                json=dict(json_body) if json_body is not None else None,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise VerifierFailure(f"{label} request failed") from exc
        self._check_size(response, label)
        if response.status_code not in expected:
            code = "UNKNOWN"
            try:
                payload = response.json()
                error = payload.get("error") if isinstance(payload, dict) else None
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
            except (ValueError, UnicodeDecodeError):
                pass
            raise VerifierFailure(
                f"{label} returned an unexpected status (HTTP {response.status_code}, code={code})"
            )
        return response

    def json(
        self,
        method: str,
        url: str,
        *,
        expected: set[int] | frozenset[int] = frozenset({200}),
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        label: str,
    ) -> tuple[int, dict[str, Any]]:
        response = self.raw(
            method,
            url,
            expected=set(expected),
            headers=headers,
            json_body=json_body,
            label=label,
        )
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise VerifierFailure(f"{label} response is not JSON") from exc
        return response.status_code, _mapping(payload, f"{label} response")

    def bff_headers(
        self,
        *,
        write: bool = False,
        idempotency_key: str | None = None,
        otel_trace_id: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Tenant-Id": SCOPE[0],
            "X-Project-Id": SCOPE[1],
            "X-Request-Id": f"production-gate-{secrets.token_hex(12)}",
        }
        if write:
            if not self.csrf_value:
                raise VerifierFailure("browser CSRF session is unavailable")
            headers["X-CSRF-Token"] = self.csrf_value
            headers["Origin"] = self.config.public_url
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
        if otel_trace_id is not None:
            headers["traceparent"] = sampled_traceparent(otel_trace_id)
        return headers

    def bff(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        expected: int = 200,
        idempotency_key: str | None = None,
        otel_trace_id: str | None = None,
        label: str,
    ) -> dict[str, Any]:
        write = method.upper() not in {"GET", "HEAD", "OPTIONS"}
        _, payload = self.json(
            method,
            f"{self.config.public_url}{path}",
            expected={expected},
            headers=self.bff_headers(
                write=write,
                idempotency_key=idempotency_key,
                otel_trace_id=otel_trace_id,
            ),
            json_body=body,
            label=label,
        )
        return payload


def _response_data(payload: dict[str, Any], label: str) -> dict[str, Any]:
    return _mapping(payload.get("data"), f"{label} data")


def _response_trace(payload: dict[str, Any], label: str) -> str:
    meta = _mapping(payload.get("meta"), f"{label} meta")
    trace_id = _nonempty_text(meta.get("trace_id"), f"{label} trace id")
    if not BUSINESS_TRACE_PATTERN.fullmatch(trace_id):
        raise VerifierFailure(f"{label} trace id is invalid")
    return trace_id


def _load_state(config: GateConfig) -> dict[str, Any]:
    state = load_json_file(config.state_path(), "verifier state")
    if (
        state.get("schema_version") != STATE_SCHEMA
        or state.get("run_suffix") != config.run_suffix
        or state.get("source_commit") != config.source_commit
        or state.get("scope") != {"tenant_id": SCOPE[0], "project_id": SCOPE[1]}
        or not isinstance(state.get("captures"), dict)
        or not isinstance(state.get("operations"), dict)
        or not isinstance(state.get("faults"), dict)
        or not isinstance(state.get("completed_phases"), list)
    ):
        raise VerifierFailure("verifier state identity is invalid")
    return state


def _phase_marker(phase: str, dependency: str) -> str:
    return phase if dependency == "none" else f"{phase}:{dependency}"


def _phase_completed(state: dict[str, Any], phase: str, dependency: str) -> bool:
    return _phase_marker(phase, dependency) in state["completed_phases"]


def _complete_phase(
    config: GateConfig,
    state: dict[str, Any],
    phase: str,
    dependency: str,
) -> dict[str, Any]:
    marker = _phase_marker(phase, dependency)
    if marker in state["completed_phases"]:
        return state
    updated = json.loads(json.dumps(state))
    updated["completed_phases"] = [*state["completed_phases"], marker]
    updated["updated_at"] = _utc_now()
    validate_artifact_value(updated)
    replace_json_state(config.state_path(), state, updated)
    return updated


def _validate_capture_record(record: object, proof_id: str) -> dict[str, Any]:
    value = _mapping(record, "capture record")
    source = PROOF_SOURCES.get(proof_id)
    capture = _mapping(value.get("capture"), "capture")
    facts = _mapping(value.get("facts"), "capture facts")
    if (
        set(value)
        != {
            "source",
            "media_type",
            "capture",
            "capture_sha256",
            "facts_sha256",
            "facts",
        }
        or value.get("source") != source
        or value.get("media_type") != "application/json"
        or capture.get("schema_version") != CAPTURE_SCHEMA
        or capture.get("proof_id") != proof_id
        or capture.get("source") != source
        or capture.get("observations") != facts
        or value.get("capture_sha256") != canonical_sha256(capture)
        or value.get("facts_sha256") != canonical_sha256(facts)
    ):
        raise VerifierFailure("capture record binding is invalid")
    validate_artifact_value(value)
    return value


def _write_capture(
    config: GateConfig,
    state: dict[str, Any],
    proof_id: str,
    observations: dict[str, Any],
) -> dict[str, Any]:
    record = capture_record(proof_id, observations)
    write_json_idempotent(config.capture_path(proof_id), record)
    captures = state["captures"]
    existing = captures.get(proof_id)
    digest = record["capture_sha256"]
    if existing is not None and existing != digest:
        raise VerifierFailure("state capture binding is inconsistent")
    captures[proof_id] = digest
    return record


def _read_capture(
    config: GateConfig, state: dict[str, Any], proof_id: str
) -> dict[str, Any]:
    record = _validate_capture_record(
        load_json_file(config.capture_path(proof_id), f"{proof_id} capture"),
        proof_id,
    )
    if state["captures"].get(proof_id) != record["capture_sha256"]:
        raise VerifierFailure("state does not bind the capture")
    return record


def _safe_json_response(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise VerifierFailure(f"{label} response is not JSON") from exc
    return _mapping(payload, f"{label} response")


def _authorize_oidc(
    config: GateConfig,
    *,
    otel_trace_id: str | None = None,
) -> tuple[BrowserClient, dict[str, dict[str, Any]], str]:
    browser = BrowserClient(config)
    public_origin = config.public_url

    def traced_headers(headers: Mapping[str, str]) -> dict[str, str]:
        result = dict(headers)
        if otel_trace_id is not None:
            result["traceparent"] = sampled_traceparent(otel_trace_id)
        return result

    try:
        discovery_response = browser.raw(
            "GET",
            f"{public_origin}/realms/auris-flow/.well-known/openid-configuration",
            expected={200},
            label="OIDC discovery",
        )
        discovery = _safe_json_response(discovery_response, "OIDC discovery")
        issuer = _nonempty_text(discovery.get("issuer"), "OIDC issuer")
        authorization_endpoint = _nonempty_text(
            discovery.get("authorization_endpoint"), "OIDC authorization endpoint"
        )
        token_endpoint = _nonempty_text(
            discovery.get("token_endpoint"), "OIDC token endpoint"
        )
        jwks_uri = _nonempty_text(discovery.get("jwks_uri"), "OIDC JWKS URI")
        if issuer != f"{public_origin}/realms/auris-flow" or any(
            _origin(item) != public_origin
            for item in (issuer, authorization_endpoint, token_endpoint, jwks_uri)
        ):
            raise VerifierFailure(
                "OIDC discovery endpoints are not bound to the gate origin"
            )
        jwks_response = browser.raw("GET", jwks_uri, expected={200}, label="OIDC JWKS")
        jwks = _safe_json_response(jwks_response, "OIDC JWKS")
        keys = jwks.get("keys")
        if not isinstance(keys, list):
            raise VerifierFailure("OIDC JWKS key set is invalid")
        signing_key_ids = sorted(
            {
                str(item["kid"])
                for item in keys
                if isinstance(item, dict)
                and item.get("kty") == "RSA"
                and item.get("use") in {None, "sig"}
                and isinstance(item.get("kid"), str)
                and item["kid"]
            }
        )
        if not signing_key_ids:
            raise VerifierFailure("OIDC JWKS has no RSA signing key")

        login_response = browser.raw(
            "GET",
            f"{public_origin}/api/v1/auth/oidc/login?return_path=%2F",
            expected={303},
            headers=traced_headers({"Accept": "text/html"}),
            label="OIDC login start",
        )
        authorize_url = urljoin(
            public_origin, login_response.headers.get("location", "")
        )
        if _origin(authorize_url) != public_origin:
            raise VerifierFailure("OIDC authorization redirect left the gate origin")
        authorize_query = parse_qs(
            urlsplit(authorize_url).query, keep_blank_values=True
        )
        states = authorize_query.get("state", [])
        if (
            authorize_query.get("response_type") != ["code"]
            or authorize_query.get("code_challenge_method") != ["S256"]
            or len(authorize_query.get("code_challenge", [])) != 1
            or len(states) != 1
        ):
            raise VerifierFailure("OIDC Authorization Code + PKCE request is invalid")
        raw_state = states[0]
        state_sha256 = hashlib.sha256(raw_state.encode("utf-8")).hexdigest()
        stored_state = config.database.one(
            "SELECT state_sha256, consumed_at FROM oidc_authorization_states "
            "WHERE state_sha256=:state_sha256",
            {"state_sha256": state_sha256},
        )
        if (
            stored_state.get("state_sha256") != state_sha256
            or stored_state.get("consumed_at") is not None
        ):
            raise VerifierFailure(
                "OIDC authorization state was not persisted exactly once"
            )

        form_response = browser.raw(
            "GET",
            authorize_url,
            expected={200},
            headers={"Accept": "text/html"},
            label="Keycloak login form",
        )
        try:
            form_html = form_response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VerifierFailure("Keycloak login form encoding is invalid") from exc
        parser = LoginFormParser()
        parser.feed(form_html)
        if not parser.action:
            raise VerifierFailure("Keycloak login form action is missing")
        form_url = urljoin(authorize_url, parser.action)
        if _origin(form_url) != public_origin:
            raise VerifierFailure("Keycloak login form left the gate origin")
        form_fields = dict(parser.fields)
        form_fields["username"] = "bootstrap-operator"
        form_fields["password"] = _read_secret_env_file(
            "AURIS_GATE_BOOTSTRAP_PASSWORD_FILE", "bootstrap credential"
        )
        credential_response = browser.raw(
            "POST",
            form_url,
            expected={302, 303},
            headers={
                "Accept": "text/html",
                "Origin": public_origin,
                "Referer": authorize_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=form_fields,
            label="Keycloak credential submission",
        )
        form_fields.clear()
        callback_url = urljoin(
            public_origin, credential_response.headers.get("location", "")
        )
        callback_parts = urlsplit(callback_url)
        callback_states = parse_qs(callback_parts.query, keep_blank_values=True).get(
            "state", []
        )
        if (
            _origin(callback_url) != public_origin
            or callback_parts.path != "/api/v1/auth/oidc/callback"
            or len(callback_states) != 1
            or hashlib.sha256(callback_states[0].encode("utf-8")).hexdigest()
            != state_sha256
        ):
            raise VerifierFailure("OIDC callback redirect is invalid")
        callback_response = browser.raw(
            "GET",
            callback_url,
            expected={303},
            headers=traced_headers({"Accept": "text/html"}),
            label="OIDC callback",
        )
        callback_url = ""
        raw_state = ""
        callback_states.clear()

        session_set_cookies = [
            value
            for value in callback_response.headers.get_list("set-cookie")
            if value.startswith("__Host-auris_session=")
        ]
        if len(session_set_cookies) != 1:
            raise VerifierFailure("OIDC callback session cookie metadata is invalid")
        cookie_metadata = session_set_cookies[0].casefold()
        session_values = [
            cookie.value
            for cookie in browser.client.cookies.jar
            if cookie.name == "__Host-auris_session" and isinstance(cookie.value, str)
        ]
        if len(session_values) != 1:
            raise VerifierFailure(
                "OIDC callback did not issue exactly one opaque session"
            )
        raw_session_value = session_values[0]
        if raw_session_value.startswith("auris.v1."):
            raise VerifierFailure("OIDC callback issued a development bearer session")
        session_sha256 = hashlib.sha256(raw_session_value.encode("utf-8")).hexdigest()

        session_payload = browser.bff(
            "GET",
            "/api/v1/auth/session",
            otel_trace_id=otel_trace_id,
            label="browser session restore",
        )
        session_data = _response_data(session_payload, "browser session restore")
        csrf_value = session_data.pop("csrf_token", None)
        if not isinstance(csrf_value, str) or not csrf_value:
            raise VerifierFailure("browser session CSRF value is unavailable")
        browser.csrf_value = csrf_value
        session_trace_id = _response_trace(session_payload, "browser session restore")
        oidc_trace_id = _nonempty_text(
            callback_response.headers.get("x-trace-id"), "OIDC callback trace id"
        )
        if not BUSINESS_TRACE_PATTERN.fullmatch(oidc_trace_id):
            raise VerifierFailure("OIDC callback trace id is invalid")
        if (
            session_data.get("provider") != "oidc_session"
            or session_data.get("tenant_id") != SCOPE[0]
            or session_data.get("project_id") != SCOPE[1]
        ):
            raise VerifierFailure("OIDC browser session scope is invalid")
        if session_trace_id == oidc_trace_id:
            raise VerifierFailure(
                "OIDC callback and session restore traces unexpectedly coincide"
            )

        remaining_state = config.database.one(
            "SELECT COUNT(*) AS count FROM oidc_authorization_states "
            "WHERE state_sha256=:state_sha256",
            {"state_sha256": state_sha256},
        )
        session_row = config.database.one(
            "SELECT COUNT(*) AS count, MIN(provider) AS provider, "
            "MIN(token_sha256) AS token_sha256 FROM browser_auth_sessions "
            "WHERE token_sha256=:token_sha256 AND revoked_at IS NULL "
            "AND expires_at > CURRENT_TIMESTAMP",
            {"token_sha256": session_sha256},
        )
        if remaining_state.get("count") != 0 or session_row.get("count") != 1:
            raise VerifierFailure(
                "OIDC state consumption or browser session persistence is invalid"
            )
        if (
            session_row.get("provider") != "oidc_session"
            or session_row.get("token_sha256") != session_sha256
        ):
            raise VerifierFailure("persisted browser session binding is invalid")

        observations = {
            "oidc_discovery": {
                "http_status": discovery_response.status_code,
                "issuer": issuer,
                "authorization_endpoint_scheme": _scheme(authorization_endpoint),
                "token_endpoint_scheme": _scheme(token_endpoint),
                "jwks_uri_scheme": _scheme(jwks_uri),
            },
            "oidc_jwks": {
                "http_status": jwks_response.status_code,
                "rsa_signing_key_ids": signing_key_ids,
            },
            "oidc_code_exchange": {
                "grant_type": "authorization_code",
                "pkce_method": "S256",
                "consumed_state_count": 1,
                "browser_session_count": 1,
                "trace_id": oidc_trace_id,
            },
            "browser_session": {
                "cookie_name": "__Host-auris_session",
                "cookie_secure": "; secure" in cookie_metadata,
                "cookie_http_only": "; httponly" in cookie_metadata,
                "provider": "oidc_session",
                "active_session_count": 1,
                "session_token_sha256": session_sha256,
                "trace_id": oidc_trace_id,
            },
        }
        if (
            not observations["browser_session"]["cookie_secure"]
            or not observations["browser_session"]["cookie_http_only"]
        ):
            raise VerifierFailure("browser session cookie hardening is incomplete")
        raw_session_value = ""
        return browser, observations, oidc_trace_id
    except Exception:
        browser.close()
        raise


def _wait_run(
    browser: BrowserClient,
    run_id: str,
    *,
    expected: set[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    terminal = {"success", "failed", "cancelled"}
    last_status = "unknown"
    while time.monotonic() < deadline:
        payload = browser.bff("GET", f"/api/v1/runs/{run_id}", label="run observation")
        data = _response_data(payload, "run observation")
        last_status = str(data.get("status") or "unknown")
        if last_status in expected:
            return data
        if last_status in terminal:
            raise VerifierFailure(
                f"run reached unexpected terminal status {last_status}"
            )
        time.sleep(0.25)
    raise VerifierFailure(f"run observation timed out at status {last_status}")


def _run_row(config: GateConfig, run_id: str) -> dict[str, Any]:
    row = config.database.one(
        "SELECT run_id, tenant_id, project_id, run_type, status, run_key, "
        "partition_key, trace_id, submitted_at, started_at, finished_at, "
        "deadline_at, next_status_sync_at, monitor_generation, engine_status, "
        "engine_status_observed_at, status_version, cancel_requested_at, "
        "cancel_reason, terminal_reason, created_at, updated_at, payload "
        "FROM run_records WHERE run_id=:run_id",
        {"run_id": run_id},
    )
    row["payload"] = _json_mapping(row.get("payload"), "persisted run payload")
    return row


def _outbox_row(config: GateConfig, run_id: str) -> dict[str, Any]:
    row = config.database.one(
        "SELECT event_id, tenant_id, project_id, event_type, aggregate_type, "
        "aggregate_id, status, payload, dispatch_idempotency_key, "
        "dispatch_request_sha256, attempt_count, reconcile_attempt_count, "
        "delivery_state, last_error, available_at, "
        "(claim_token IS NULL) AS claim_token_cleared, "
        "(claimed_by IS NULL) AS claimed_by_cleared, "
        "(claimed_at IS NULL) AS claimed_at_cleared, lease_generation, "
        "(lease_expires_at IS NULL) AS lease_expires_at_cleared, "
        "processed_at, created_at FROM outbox_events "
        "WHERE tenant_id=:tenant_id AND project_id=:project_id "
        "AND aggregate_id=:run_id",
        {"tenant_id": SCOPE[0], "project_id": SCOPE[1], "run_id": run_id},
    )
    row["payload"] = _json_mapping(row.get("payload"), "persisted outbox payload")
    return row


def _dead_letter_source_snapshot(
    run: Mapping[str, Any],
    event: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
) -> str:
    """Hash the immutable source decision without persisting business payloads."""

    run_payload = _json_mapping(run.get("payload"), "dead-letter run payload")
    event_payload = _json_mapping(event.get("payload"), "dead-letter event payload")
    return canonical_sha256(
        {
            "run_status": run.get("status"),
            "run_id": run.get("run_id"),
            "run_tenant_id": run.get("tenant_id"),
            "run_project_id": run.get("project_id"),
            "run_type": run.get("run_type"),
            "run_key": run.get("run_key"),
            "run_partition_key": run.get("partition_key"),
            "run_status_version": run.get("status_version"),
            "run_terminal_reason": run.get("terminal_reason"),
            "run_submitted_at": _iso_or_none(run.get("submitted_at")),
            "run_started_at": _iso_or_none(run.get("started_at")),
            "run_finished_at": _iso(run.get("finished_at")),
            "run_deadline_at": _iso_or_none(run.get("deadline_at")),
            "run_next_status_sync_at": _iso_or_none(run.get("next_status_sync_at")),
            "run_monitor_generation": run.get("monitor_generation"),
            "run_engine_status": run.get("engine_status"),
            "run_engine_status_observed_at": _iso_or_none(
                run.get("engine_status_observed_at")
            ),
            "run_cancel_requested_at": _iso_or_none(run.get("cancel_requested_at")),
            "run_cancel_reason": run.get("cancel_reason"),
            "run_created_at": _iso(run.get("created_at")),
            "run_updated_at": _iso(run.get("updated_at")),
            "run_trace_id": run.get("trace_id"),
            "run_payload_sha256": canonical_sha256(run_payload),
            "event_id": event.get("event_id"),
            "event_aggregate_type": event.get("aggregate_type"),
            "event_aggregate_id": event.get("aggregate_id"),
            "event_status": event.get("status"),
            "event_delivery_state": event.get("delivery_state"),
            "event_attempt_count": event.get("attempt_count"),
            "event_reconcile_attempt_count": event.get("reconcile_attempt_count"),
            "event_dispatch_idempotency_key_sha256": _sha256_text(
                _nonempty_text(
                    event.get("dispatch_idempotency_key"),
                    "dead-letter dispatch idempotency key",
                )
            ),
            "event_dispatch_request_sha256": event.get("dispatch_request_sha256"),
            "event_last_error_sha256": _sha256_text(
                _nonempty_text(event.get("last_error"), "dead-letter last error")
            ),
            "event_available_at": _iso(event.get("available_at")),
            "event_claim_token_cleared": bool(event.get("claim_token_cleared")),
            "event_claimed_by_cleared": bool(event.get("claimed_by_cleared")),
            "event_claimed_at_cleared": bool(event.get("claimed_at_cleared")),
            "event_lease_generation": event.get("lease_generation"),
            "event_lease_expires_at_cleared": bool(
                event.get("lease_expires_at_cleared")
            ),
            "event_processed_at": _iso(event.get("processed_at")),
            "event_created_at": _iso(event.get("created_at")),
            "event_payload_sha256": canonical_sha256(event_payload),
            "attempt_ledger_sha256": _attempt_ledger_sha256(attempts),
        }
    )


def _attempt_ledger_sha256(attempts: Sequence[Mapping[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for attempt in attempts:
        details = _json_mapping(attempt.get("details"), "outbox attempt details")
        claimed_by = _nonempty_text(attempt.get("claimed_by"), "attempt worker")
        error_message = attempt.get("error_message")
        remote_id = attempt.get("remote_id")
        normalized.append(
            {
                "attempt_id_sha256": _sha256_text(
                    _nonempty_text(attempt.get("attempt_id"), "attempt id")
                ),
                "event_id": attempt.get("event_id"),
                "tenant_id": attempt.get("tenant_id"),
                "project_id": attempt.get("project_id"),
                "attempt_number": attempt.get("attempt_number"),
                "lease_generation": attempt.get("lease_generation"),
                "claimed_by_sha256": _sha256_text(claimed_by),
                "claim_token_sha256": attempt.get("claim_token_sha256"),
                "delivery_mode": attempt.get("delivery_mode"),
                "status": attempt.get("status"),
                "dispatch_idempotency_key_sha256": _sha256_text(
                    _nonempty_text(
                        attempt.get("dispatch_idempotency_key"),
                        "attempt dispatch idempotency key",
                    )
                ),
                "request_sha256": attempt.get("request_sha256"),
                "adapter": attempt.get("adapter"),
                "operation": attempt.get("operation"),
                "remote_id_sha256": (
                    _sha256_text(remote_id)
                    if isinstance(remote_id, str) and remote_id
                    else None
                ),
                "error_code": attempt.get("error_code"),
                "error_message_sha256": (
                    _sha256_text(error_message)
                    if isinstance(error_message, str) and error_message
                    else None
                ),
                "started_at": _iso(attempt.get("started_at")),
                "completed_at": _iso(attempt.get("completed_at")),
                "details_sha256": canonical_sha256(details),
            }
        )
    return canonical_sha256(normalized)


def _trace_bound_outbox_row(
    config: GateConfig,
    run_id: str,
    *,
    expected_otel_trace_id: str,
) -> dict[str, Any]:
    row = _outbox_row(config, run_id)
    persisted_trace_id = otel_trace_id_from_carrier(
        row["payload"].get("otel_trace_context")
    )
    if persisted_trace_id != expected_otel_trace_id:
        raise VerifierFailure("outbox did not preserve its operation trace context")
    return row


def _dispatch_details(run: Mapping[str, Any], *, adapter: str) -> dict[str, Any]:
    payload = _json_mapping(run.get("payload"), "run payload")
    dispatch = _mapping(payload.get("dispatch"), "run dispatch")
    details = _mapping(dispatch.get("details"), "run dispatch details")
    if (
        dispatch.get("adapter") != adapter
        or dispatch.get("status") != "success"
        or details.get("mode") != "real"
    ):
        raise VerifierFailure(f"{adapter} real dispatch proof is invalid")
    return details


def validate_dispatch_binding(
    details: Mapping[str, Any],
    *,
    trace_id: str,
    run_id: str,
    provider: str | None = None,
) -> None:
    if (
        details.get("mode") != "real"
        or details.get("tenant_id") != SCOPE[0]
        or details.get("project_id") != SCOPE[1]
        or details.get("trace_id") != trace_id
        or details.get("run_id") != run_id
        or not BUSINESS_TRACE_PATTERN.fullmatch(trace_id)
        or (provider is not None and details.get("provider") != provider)
    ):
        raise VerifierFailure("real adapter dispatch scope or lineage is invalid")


def validate_callback_dispatch_binding(
    details: Mapping[str, Any],
    *,
    trace_id: str,
    run_id: str,
    receipt_id: str,
) -> None:
    validate_dispatch_binding(details, trace_id=trace_id, run_id=run_id)
    protocol_receipt = _mapping(
        details.get("protocol_receipt"), "callback protocol receipt"
    )
    request_sha256 = details.get("request_sha256")
    if (
        details.get("callback_receipt_id") != receipt_id
        or details.get("signature_mode") != "hmac-sha256-v2"
        or not isinstance(request_sha256, str)
        or not SHA256_PATTERN.fullmatch(request_sha256)
        or protocol_receipt.get("callback_receipt_id") != receipt_id
        or protocol_receipt.get("tenant_id") != SCOPE[0]
        or protocol_receipt.get("project_id") != SCOPE[1]
        or protocol_receipt.get("trace_id") != trace_id
        or protocol_receipt.get("run_id") != run_id
        or protocol_receipt.get("request_sha256") != request_sha256
        or protocol_receipt.get("signature_verified") is not True
        or protocol_receipt.get("signature_mode") != "hmac-sha256-v2"
    ):
        raise VerifierFailure("callback receiver receipt lineage is invalid")


def _create_and_complete_run(
    browser: BrowserClient,
    *,
    path: str,
    body: Mapping[str, object],
    idempotency_key: str,
    otel_trace_id: str,
    timeout_seconds: float,
    label: str,
) -> tuple[str, str, dict[str, Any]]:
    created = browser.bff(
        "POST",
        path,
        body=body,
        expected=202,
        idempotency_key=idempotency_key,
        otel_trace_id=otel_trace_id,
        label=f"{label} creation",
    )
    data = _response_data(created, f"{label} creation")
    run_id = _nonempty_text(data.get("run_id"), f"{label} run id")
    trace_id = _nonempty_text(data.get("trace_id"), f"{label} trace id")
    if not BUSINESS_TRACE_PATTERN.fullmatch(trace_id):
        raise VerifierFailure(f"{label} business trace id is invalid")
    final = _wait_run(
        browser,
        run_id,
        expected={"success"},
        timeout_seconds=timeout_seconds,
    )
    return run_id, trace_id, final


RUN_QUERY = """
query AurisProductionGateRun($runId: ID!) {
  pipelineRunOrError(runId: $runId) {
    __typename
    ... on Run { runId status }
  }
}
""".strip()

RUNS_BY_KEY_QUERY = """
query AurisProductionGateRunsByKey($filter: RunsFilter!) {
  runsOrError(filter: $filter, limit: 2) {
    __typename
    ... on Runs {
      results {
        runId
        status
        tags { key value }
      }
    }
  }
}
""".strip()


def _dagster_graphql(browser: BrowserClient, dagster_run_id: str) -> dict[str, Any]:
    _, payload = browser.json(
        "POST",
        "http://dagster-webserver:3000/graphql",
        expected={200},
        json_body={"query": RUN_QUERY, "variables": {"runId": dagster_run_id}},
        label="Dagster GraphQL",
    )
    if payload.get("errors"):
        raise VerifierFailure("Dagster GraphQL returned errors")
    data = _mapping(payload.get("data"), "Dagster GraphQL data")
    run = _mapping(data.get("pipelineRunOrError"), "Dagster GraphQL run")
    if (
        run.get("__typename") != "Run"
        or run.get("runId") != dagster_run_id
        or run.get("status") != "SUCCESS"
    ):
        raise VerifierFailure("Dagster GraphQL did not prove a successful real run")
    return run


def validate_dagster_run_count(
    payload: object, run_key: str, expected_run_id: str
) -> int:
    response = _mapping(payload, "Dagster runs response")
    data = _mapping(response.get("data"), "Dagster runs data")
    runs = _mapping(data.get("runsOrError"), "Dagster runs result")
    results = runs.get("results")
    if runs.get("__typename") != "Runs" or not isinstance(results, list):
        raise VerifierFailure("Dagster remote run count response is invalid")
    exact: list[dict[str, Any]] = []
    for raw_run in results:
        run = _mapping(raw_run, "Dagster remote run")
        tags = run.get("tags")
        matching_tags = (
            [
                tag
                for tag in tags
                if isinstance(tag, dict)
                and tag.get("key") == "auris/dispatch_idempotency_key"
                and tag.get("value") == run_key
            ]
            if isinstance(tags, list)
            else []
        )
        if len(matching_tags) == 1:
            exact.append(run)
    if (
        len(exact) != 1
        or exact[0].get("runId") != expected_run_id
        or exact[0].get("status") != "SUCCESS"
    ):
        raise VerifierFailure(
            "Dagster dispatch key did not resolve to one successful remote run"
        )
    return 1


def _dagster_remote_run_count(
    browser: BrowserClient,
    *,
    run_key: str,
    expected_run_id: str,
) -> int:
    _, payload = browser.json(
        "POST",
        "http://dagster-webserver:3000/graphql",
        expected={200},
        json_body={
            "query": RUNS_BY_KEY_QUERY,
            "variables": {
                "filter": {
                    "tags": [
                        {
                            "key": "auris/dispatch_idempotency_key",
                            "value": run_key,
                        }
                    ]
                }
            },
        },
        label="Dagster remote run count",
    )
    return validate_dagster_run_count(payload, run_key, expected_run_id)


def _support_proofs(
    browser: BrowserClient, config: GateConfig, mode: str
) -> dict[str, Any]:
    if mode not in {"callback", "embedding"}:
        raise VerifierFailure("support proof mode is invalid")
    base = config.callback_url if mode == "callback" else config.embedding_url
    control = _read_secret_env_file("AURIS_GATE_CONTROL_SECRET_FILE", "gate control")
    _, payload = browser.json(
        "GET",
        f"{base}/proofs",
        expected={200},
        headers={"X-Auris-Gate-Control": control},
        label=f"{mode} support proof",
    )
    return support_proof_data(payload)


def support_proof_data(payload: object) -> dict[str, Any]:
    envelope = _mapping(payload, "support proof envelope")
    if set(envelope) != {"status", "data"} or envelope.get("status") != "ok":
        raise VerifierFailure("support proof envelope is invalid")
    data = _mapping(envelope.get("data"), "support proof data")
    if not data:
        raise VerifierFailure("support proof data is empty")
    return data


def _support_control(
    browser: BrowserClient,
    config: GateConfig,
    path: str,
    *,
    expected: int,
) -> tuple[int, dict[str, Any]]:
    control = _read_secret_env_file("AURIS_GATE_CONTROL_SECRET_FILE", "gate control")
    return browser.json(
        "POST",
        f"{config.callback_url}{path}",
        expected={expected},
        headers={"X-Auris-Gate-Control": control},
        label="callback support control",
    )


def _qdrant_point_observation(
    browser: BrowserClient,
    config: GateConfig,
    *,
    collection: str,
    point_id: str,
    expected_trace_id: str,
) -> dict[str, Any]:
    api_key = _read_secret_env_file("QDRANT_API_KEY_FILE", "Qdrant API credential")
    status, payload = browser.json(
        "GET",
        f"{config.qdrant_url}/collections/{collection}/points/{point_id}?with_payload=true&with_vector=true",
        expected={200},
        headers={"api-key": api_key},
        label="Qdrant point",
    )
    result = _mapping(payload.get("result"), "Qdrant point result")
    point_payload = _mapping(result.get("payload"), "Qdrant point payload")
    vector = result.get("vector")
    if (
        str(result.get("id") or "") != point_id
        or point_payload.get("tenant_id") != SCOPE[0]
        or point_payload.get("project_id") != SCOPE[1]
        or point_payload.get("trace_id") != expected_trace_id
        or not isinstance(vector, list)
        or not vector
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in vector
        )
    ):
        raise VerifierFailure("Qdrant point scope, trace or vector is invalid")
    return {
        "http_status": status,
        "collection": collection,
        "point_id": point_id,
        "point_count": 1,
        "tenant_id": SCOPE[0],
        "project_id": SCOPE[1],
        "trace_id": expected_trace_id,
        "vector_dimension": len(vector),
    }


def _qdrant_filtered_points(
    browser: BrowserClient,
    config: GateConfig,
    *,
    collection: str,
    point_id: str,
    tenant_id: str,
    project_id: str,
) -> tuple[int, list[dict[str, Any]], object]:
    api_key = _read_secret_env_file("QDRANT_API_KEY_FILE", "Qdrant API credential")
    status, payload = browser.json(
        "POST",
        f"{config.qdrant_url}/collections/{collection}/points/scroll",
        expected={200},
        headers={"api-key": api_key},
        json_body={
            "limit": 2,
            "with_payload": True,
            "with_vector": False,
            "filter": {
                "must": [
                    {"key": "tenant_id", "match": {"value": tenant_id}},
                    {"key": "project_id", "match": {"value": project_id}},
                    {"has_id": [point_id]},
                ]
            },
        },
        label="Qdrant scoped retry point",
    )
    result = _mapping(payload.get("result"), "Qdrant scoped retry result")
    raw_points = result.get("points")
    if not isinstance(raw_points, list) or any(
        not isinstance(point, dict) for point in raw_points
    ):
        raise VerifierFailure("Qdrant scoped retry points are invalid")
    return status, raw_points, result.get("next_page_offset")


def _qdrant_retry_observation(
    browser: BrowserClient,
    config: GateConfig,
    *,
    retry_run_id: str,
    retry_event: Mapping[str, Any],
    retry_attempt: Mapping[str, Any],
    retry_details: Mapping[str, Any],
    expected_trace_id: str,
) -> dict[str, Any]:
    collection = _nonempty_text(
        retry_details.get("collection"), "dead-letter retry collection"
    )
    point_ids = retry_details.get("point_ids")
    if (
        not isinstance(point_ids, list)
        or len(point_ids) != 1
        or not isinstance(point_ids[0], str)
        or not point_ids[0]
    ):
        raise VerifierFailure("dead-letter retry point receipt is invalid")
    point_id = point_ids[0]
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,254}", collection) is None
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            point_id,
        )
        is None
    ):
        raise VerifierFailure("dead-letter retry Qdrant identity is invalid")
    expected_payload = _mapping(
        retry_details.get("qdrant_payload"), "dead-letter retry Qdrant payload"
    )
    api_key = _read_secret_env_file("QDRANT_API_KEY_FILE", "Qdrant API credential")
    status, exact_document = browser.json(
        "GET",
        f"{config.qdrant_url}/collections/{collection}/points/{point_id}"
        "?with_payload=true&with_vector=true",
        expected={200},
        headers={"api-key": api_key},
        label="Qdrant exact retry point",
    )
    exact_point = _mapping(exact_document.get("result"), "Qdrant exact retry point")
    remote_payload = _mapping(
        exact_point.get("payload"), "Qdrant exact retry point payload"
    )
    vector = exact_point.get("vector")
    if (
        str(exact_point.get("id") or "") != point_id
        or not isinstance(vector, list)
        or not vector
        or any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in vector
        )
    ):
        raise VerifierFailure("Qdrant exact retry point is invalid")

    _, scoped_points, next_offset = _qdrant_filtered_points(
        browser,
        config,
        collection=collection,
        point_id=point_id,
        tenant_id=SCOPE[0],
        project_id=SCOPE[1],
    )
    _, cross_tenant_points, cross_tenant_offset = _qdrant_filtered_points(
        browser,
        config,
        collection=collection,
        point_id=point_id,
        tenant_id=f"{SCOPE[0]}_forbidden",
        project_id=SCOPE[1],
    )
    _, cross_project_points, cross_project_offset = _qdrant_filtered_points(
        browser,
        config,
        collection=collection,
        point_id=point_id,
        tenant_id=SCOPE[0],
        project_id=f"{SCOPE[1]}_forbidden",
    )
    scoped_ids = [str(point.get("id") or "") for point in scoped_points]
    if (
        next_offset is not None
        or cross_tenant_offset is not None
        or cross_project_offset is not None
        or len(scoped_points) != 1
        or scoped_ids != [point_id]
        or cross_tenant_points
        or cross_project_points
    ):
        raise VerifierFailure("Qdrant retry scope or cardinality proof is invalid")
    scoped_payload = _mapping(
        scoped_points[0].get("payload"), "Qdrant scoped retry payload"
    )
    payload_hash = canonical_sha256(remote_payload)
    payload_hash_match = (
        payload_hash == canonical_sha256(expected_payload)
        and canonical_sha256(scoped_payload) == payload_hash
    )

    event_payload = _json_mapping(
        retry_event.get("payload"), "dead-letter retry event payload"
    )
    attempt_details = _json_mapping(
        retry_attempt.get("details"), "dead-letter retry attempt details"
    )
    attempt_dispatch = _mapping(
        attempt_details.get("dispatch_details"), "dead-letter retry dispatch receipt"
    )
    attempt_point_ids = attempt_dispatch.get("point_ids")
    attempt_payload = _mapping(
        attempt_dispatch.get("qdrant_payload"),
        "dead-letter retry attempt Qdrant payload",
    )
    dispatch_receipt_match = (
        canonical_sha256(attempt_dispatch) == canonical_sha256(retry_details)
        and attempt_dispatch.get("collection") == collection
        and attempt_point_ids == [point_id]
    )
    attempt_receipt_match = (
        retry_attempt.get("event_id") == retry_event.get("event_id")
        and retry_attempt.get("dispatch_idempotency_key")
        == retry_event.get("dispatch_idempotency_key")
        and retry_attempt.get("request_sha256")
        == retry_event.get("dispatch_request_sha256")
        and retry_attempt.get("status") == "succeeded"
        and retry_attempt.get("delivery_mode") == "dispatch"
        and retry_attempt.get("adapter") == "qdrant"
        and retry_attempt.get("operation") == "upsert_payload"
    )
    scope_match = (
        remote_payload.get("tenant_id") == SCOPE[0]
        and remote_payload.get("project_id") == SCOPE[1]
        and remote_payload.get("trace_id") == expected_trace_id
        and event_payload.get("tenant_id") == SCOPE[0]
        and event_payload.get("project_id") == SCOPE[1]
        and event_payload.get("trace_id") == expected_trace_id
    )
    if not all(
        (payload_hash_match, dispatch_receipt_match, attempt_receipt_match, scope_match)
    ):
        raise VerifierFailure("Qdrant retry receipts are not cross-bound")
    return {
        "http_status": status,
        "collection": collection,
        "point_id_sha256": _sha256_text(point_id),
        "dispatch_point_id_sha256": _sha256_text(point_id),
        "attempt_point_id_sha256": _sha256_text(
            _nonempty_text(
                attempt_point_ids[0]
                if isinstance(attempt_point_ids, list) and attempt_point_ids
                else None,
                "retry attempt point id",
            )
        ),
        "payload_sha256": payload_hash,
        "dispatch_payload_sha256": canonical_sha256(expected_payload),
        "attempt_payload_sha256": canonical_sha256(attempt_payload),
        "retry_run_id_sha256": _sha256_text(retry_run_id),
        "retry_event_id": retry_event.get("event_id"),
        "dispatch_idempotency_key_sha256": _sha256_text(
            _nonempty_text(
                retry_event.get("dispatch_idempotency_key"),
                "retry dispatch idempotency key",
            )
        ),
        "dispatch_request_sha256": retry_event.get("dispatch_request_sha256"),
        "attempt_request_sha256": retry_attempt.get("request_sha256"),
        "attempt_id_sha256": _sha256_text(
            _nonempty_text(retry_attempt.get("attempt_id"), "retry attempt id")
        ),
        "tenant_id": SCOPE[0],
        "project_id": SCOPE[1],
        "trace_id": expected_trace_id,
        "filtered_point_count": len(scoped_points),
        "point_occurrences": scoped_ids.count(point_id),
        "cross_tenant_count": len(cross_tenant_points),
        "cross_project_count": len(cross_project_points),
        "scope_match": scope_match,
        "dispatch_receipt_match": dispatch_receipt_match,
        "attempt_receipt_match": attempt_receipt_match,
        "payload_hash_match": payload_hash_match,
    }


def _minio_object_observation(
    config: GateConfig,
    *,
    details: dict[str, Any],
    trace_id: str,
) -> dict[str, Any]:
    try:
        from app.services.adapters import RealObjectStorageClient
    except ImportError as exc:
        raise VerifierFailure("real object storage client is unavailable") from exc
    bucket = _nonempty_text(details.get("bucket"), "MinIO bucket")
    object_key = _nonempty_text(details.get("object_key"), "MinIO object key")
    expected_hash = _nonempty_text(details.get("content_sha256"), "MinIO expected hash")
    expected_length = _positive_int(
        details.get("content_length"), "MinIO expected length"
    )
    if bucket != config.object_bucket or not SHA256_PATTERN.fullmatch(expected_hash):
        raise VerifierFailure("MinIO dispatch binding is invalid")
    client = RealObjectStorageClient(
        endpoint=config.object_endpoint,
        bucket=bucket,
        access_key=_read_secret_env_file(
            "OBJECT_STORAGE_ACCESS_KEY_FILE", "object storage access credential"
        ),
        secret_key=_read_secret_env_file(
            "OBJECT_STORAGE_SECRET_KEY_FILE", "object storage signing credential"
        ),
        provider="minio",
        addressing_style="path",
        signature_mode="s3v4",
    )
    try:
        response = client.get_object(bucket, object_key)
    except Exception as exc:
        raise VerifierFailure("MinIO object read failed") from exc
    body = response.get("body") if isinstance(response, dict) else None
    if not isinstance(body, bytes):
        raise VerifierFailure("MinIO object response is invalid")
    observed_hash = hashlib.sha256(body).hexdigest()
    observed_length = len(body)
    body = b""
    if observed_hash != expected_hash or observed_length != expected_length:
        raise VerifierFailure(
            "MinIO object content does not match its dispatch receipt"
        )
    return {
        "bucket": bucket,
        "object_key": object_key,
        "http_status": int(response.get("status") or 0),
        "expected_content_sha256": expected_hash,
        "observed_content_sha256": observed_hash,
        "content_length": observed_length,
        "trace_id": trace_id,
    }


def _completion_observation(
    config: GateConfig, run_id: str, trace_id: str
) -> dict[str, Any]:
    row = config.database.one(
        "SELECT COUNT(*) AS count, MIN(processing_state) AS processing_state, "
        "MIN(completion_status) AS completion_status, MIN(signature_mode) AS signature_mode, "
        "MIN(signature_key_id) AS signature_key_id, MIN(run_trace_id) AS run_trace_id "
        "FROM run_completion_receipts WHERE tenant_id=:tenant_id AND project_id=:project_id "
        "AND run_id=:run_id",
        {"tenant_id": SCOPE[0], "project_id": SCOPE[1], "run_id": run_id},
    )
    if (
        row.get("count") != 1
        or row.get("processing_state") != "completed"
        or row.get("completion_status") != "success"
        or row.get("signature_mode") != "hmac-sha256-v2"
        or not row.get("signature_key_id")
        or row.get("run_trace_id") != trace_id
    ):
        raise VerifierFailure("signed Dagster completion receipt is invalid")
    return {
        "receipt_count": 1,
        "processing_state": "completed",
        "completion_status": "success",
        "signature_mode": "hmac-sha256-v2",
        "signature_key_id": str(row["signature_key_id"]),
        "run_trace_id": trace_id,
    }


def _authoritative_counts(
    config: GateConfig, run_ids: Sequence[str]
) -> tuple[list[str], int]:
    provided = [item for item in run_ids if isinstance(item, str) and item]
    normalized = sorted(set(provided))
    if (
        not normalized
        or len(provided) != len(run_ids)
        or len(normalized) != len(provided)
    ):
        raise VerifierFailure("authoritative run inventory is empty or duplicated")
    placeholders = ",".join(f":run_{index}" for index in range(len(normalized)))
    parameters: dict[str, object] = {
        "tenant_id": SCOPE[0],
        "project_id": SCOPE[1],
        **{f"run_{index}": run_id for index, run_id in enumerate(normalized)},
    }
    rows = config.database.rows(
        "SELECT run_id FROM run_records WHERE tenant_id=:tenant_id "
        "AND project_id=:project_id AND run_id IN ("
        + placeholders
        + ") ORDER BY run_id",
        parameters,
    )
    observed_ids = [str(row["run_id"]) for row in rows]
    if observed_ids != normalized:
        raise VerifierFailure("MySQL authoritative run inventory is incomplete")
    processed_rows = config.database.rows(
        "SELECT aggregate_id, COUNT(*) AS event_count FROM outbox_events "
        "WHERE tenant_id=:tenant_id "
        "AND project_id=:project_id AND aggregate_id IN (" + placeholders + ") "
        "AND status='processed' GROUP BY aggregate_id ORDER BY aggregate_id",
        parameters,
    )
    processed_ids = [str(row.get("aggregate_id") or "") for row in processed_rows]
    if processed_ids != normalized or any(
        row.get("event_count") != 1 for row in processed_rows
    ):
        raise VerifierFailure(
            "each authoritative run must have exactly one processed outbox event"
        )
    return observed_ids, len(processed_rows)


def validate_qdrant_recall_binding(written_point_id: str, hits: object) -> list[str]:
    if not written_point_id or not isinstance(hits, list) or not hits:
        raise VerifierFailure("Qdrant recall binding is invalid")
    recalled_point_ids: list[str] = []
    for hit in hits:
        if not isinstance(hit, dict):
            raise VerifierFailure("Qdrant recall contains a malformed hit")
        point_id = hit.get("point_id")
        if not isinstance(point_id, str) or not point_id:
            raise VerifierFailure("Qdrant recall contains a malformed point id")
        recalled_point_ids.append(point_id)
    if (
        len(set(recalled_point_ids)) != len(recalled_point_ids)
        or recalled_point_ids.count(written_point_id) != 1
    ):
        raise VerifierFailure(
            "Qdrant recall must uniquely include the point written by this run"
        )
    return recalled_point_ids


def _initial_phase(config: GateConfig) -> None:
    if config.state_path().exists() or config.state_path().is_symlink():
        existing_state = _load_state(config)
        if not _phase_completed(existing_state, "initial", "none"):
            raise VerifierFailure("existing initial state is incomplete")
        for proof_id in BASELINE_PROOFS:
            _read_capture(config, existing_state, proof_id)
        return
    existing_captures = [
        proof_id
        for proof_id in BASELINE_PROOFS
        if config.capture_path(proof_id).exists()
    ]
    if existing_captures:
        raise VerifierFailure("orphaned initial captures require a new run suffix")

    operation_otel_trace_ids = _new_operation_otel_trace_ids()
    otel_trace_id = operation_otel_trace_ids["dagster"]
    browser, oidc_observations, oidc_trace_id = _authorize_oidc(
        config,
        otel_trace_id=operation_otel_trace_ids["oidc"],
    )
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA,
        "run_suffix": config.run_suffix,
        "source_commit": config.source_commit,
        "scope": {"tenant_id": SCOPE[0], "project_id": SCOPE[1]},
        "captures": {},
        "operations": {},
        "faults": {},
        "completed_phases": [],
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    try:
        for proof_id, facts in oidc_observations.items():
            _write_capture(config, state, proof_id, facts)

        dagster_created = browser.bff(
            "POST",
            "/api/v1/task-runs",
            body={
                "task_version_id": "task_version_v3_2_1",
                "trigger_type": "manual",
                "execution_mode": "production",
                "partition_key": f"production-gate/{config.run_suffix}/dagster",
            },
            expected=202,
            idempotency_key=f"production-gate:{config.run_suffix}:dagster",
            otel_trace_id=otel_trace_id,
            label="Dagster task run creation",
        )
        dagster_data = _response_data(dagster_created, "Dagster task run creation")
        dagster_run_record_id = _nonempty_text(
            dagster_data.get("run_id"), "Dagster task run id"
        )
        dagster_trace_id = _nonempty_text(
            dagster_data.get("trace_id"), "Dagster trace id"
        )
        dagster_final = _wait_run(
            browser,
            dagster_run_record_id,
            expected={"success"},
            timeout_seconds=config.timeout_seconds,
        )
        dagster_row = _run_row(config, dagster_run_record_id)
        dagster_details = _dispatch_details(dagster_row, adapter="dagster")
        real_dagster_run_id = _nonempty_text(
            dagster_details.get("external_run_id"), "real Dagster run id"
        )
        if (
            dagster_final.get("trace_id") != dagster_trace_id
            or dagster_details.get("trace_id") != dagster_trace_id
        ):
            raise VerifierFailure("Dagster business trace binding is invalid")
        graph_run = _dagster_graphql(browser, real_dagster_run_id)
        _trace_bound_outbox_row(
            config,
            dagster_run_record_id,
            expected_otel_trace_id=operation_otel_trace_ids["dagster"],
        )
        _write_capture(
            config,
            state,
            "dagster_graphql",
            {
                "graphql_operation": "pipelineRunOrError",
                "response_typename": graph_run["__typename"],
                "dagster_run_id": real_dagster_run_id,
                "dagster_status": graph_run["status"],
                "trace_id": dagster_trace_id,
            },
        )
        _write_capture(
            config,
            state,
            "dagster_completion",
            _completion_observation(config, dagster_run_record_id, dagster_trace_id),
        )

        knowledge_run_id, qdrant_trace_id, knowledge_final = _create_and_complete_run(
            browser,
            path="/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
            body={
                "reason": "production path semantic protocol verification",
                "chunk_policy": "production-gate-single-chunk",
                "embedding_text": "销售报价审批规则与客户承诺边界",
            },
            idempotency_key=f"production-gate:{config.run_suffix}:knowledge",
            otel_trace_id=operation_otel_trace_ids["qdrant"],
            timeout_seconds=config.timeout_seconds,
            label="knowledge build",
        )
        knowledge_row = _run_row(config, knowledge_run_id)
        _trace_bound_outbox_row(
            config,
            knowledge_run_id,
            expected_otel_trace_id=operation_otel_trace_ids["qdrant"],
        )
        qdrant_details = _dispatch_details(knowledge_row, adapter="qdrant")
        point_ids = qdrant_details.get("point_ids")
        if (
            not isinstance(point_ids, list)
            or len(point_ids) != 1
            or not isinstance(point_ids[0], str)
            or knowledge_final.get("trace_id") != qdrant_trace_id
            or qdrant_details.get("semantic_embedding") is not True
            or qdrant_details.get("embedding_provider") != "http"
        ):
            raise VerifierFailure("real semantic Qdrant dispatch is invalid")
        collection = _nonempty_text(
            qdrant_details.get("collection"), "Qdrant collection"
        )
        point_id = point_ids[0]
        qdrant_point = _qdrant_point_observation(
            browser,
            config,
            collection=collection,
            point_id=point_id,
            expected_trace_id=qdrant_trace_id,
        )
        _write_capture(config, state, "qdrant_point", qdrant_point)
        recall_response = browser.bff(
            "POST",
            "/api/v1/knowledge-indexes/ki_sales_policy_v1/recall",
            body={
                "query": "客户报价承诺需要什么审批",
                "top_k": 5,
                "scope": {"tenant_id": SCOPE[0], "project_id": SCOPE[1]},
            },
            expected=200,
            idempotency_key=f"production-gate:{config.run_suffix}:recall",
            otel_trace_id=operation_otel_trace_ids["qdrant"],
            label="knowledge recall",
        )
        recall_data = _response_data(recall_response, "knowledge recall")
        hits = recall_data.get("hits")
        if (
            not isinstance(hits, list)
            or not hits
            or recall_data.get("mode") != "real_qdrant"
        ):
            raise VerifierFailure("real Qdrant recall returned no authorized hit")
        recalled_point_ids = validate_qdrant_recall_binding(point_id, hits)
        recall_trace_id = _nonempty_text(
            recall_data.get("recall_trace_id"), "Qdrant recall trace id"
        )
        recalled_lineage_traces = {
            str(hit.get("trace_id"))
            for hit in hits
            if isinstance(hit, dict) and isinstance(hit.get("trace_id"), str)
        }
        if recalled_lineage_traces != {qdrant_trace_id}:
            raise VerifierFailure(
                "Qdrant recall did not preserve the point trace lineage"
            )
        _write_capture(
            config,
            state,
            "qdrant_recall",
            {
                "http_status": 200,
                "point_ids": recalled_point_ids,
                "authorized_hit_count": len(hits),
                "written_point_id": point_id,
                "written_point_occurrences": recalled_point_ids.count(point_id),
                "trace_id": qdrant_trace_id,
            },
        )
        qdrant_operation_trace_id = qdrant_trace_id
        embedding_proof = _support_proofs(browser, config, "embedding")
        _write_capture(
            config,
            state,
            "embedding_https",
            {
                "transport": embedding_proof.get("transport"),
                "tls_verified": True,
                "provider": embedding_proof.get("provider"),
                "model": _nonempty_text(
                    qdrant_details.get("embedding_model"), "embedding model"
                ),
                "request_count": _positive_int(
                    embedding_proof.get("request_count"),
                    "embedding request count",
                    minimum=2,
                ),
                "purposes": embedding_proof.get("purposes"),
                "dimension": _positive_int(
                    embedding_proof.get("dimension"), "embedding dimension"
                ),
                "reference_protocol_only": embedding_proof.get(
                    "reference_protocol_only"
                ),
                "model_quality_certified": embedding_proof.get(
                    "model_quality_certified"
                ),
            },
        )

        object_run_id, object_trace_id, _ = _create_and_complete_run(
            browser,
            path="/api/v1/audio-ingest/recordings",
            body={
                "asset_ref": f"production-gate/audio/{config.run_suffix}",
                "content_type": "application/json",
                "target": "audio-ingest-minio-verification",
            },
            idempotency_key=f"production-gate:{config.run_suffix}:audio-ingest",
            otel_trace_id=operation_otel_trace_ids["object_storage"],
            timeout_seconds=config.timeout_seconds,
            label="audio ingest",
        )
        object_row = _run_row(config, object_run_id)
        _trace_bound_outbox_row(
            config,
            object_run_id,
            expected_otel_trace_id=operation_otel_trace_ids["object_storage"],
        )
        object_details = _dispatch_details(object_row, adapter="object_storage")
        validate_dispatch_binding(
            object_details,
            trace_id=object_trace_id,
            run_id=object_run_id,
            provider="minio",
        )
        minio_observation = _minio_object_observation(
            config, details=object_details, trace_id=object_trace_id
        )
        _write_capture(config, state, "minio_object", minio_observation)

        callback_run_id, callback_trace_id, _ = _create_and_complete_run(
            browser,
            path="/api/v1/output-sinks/platform-callbacks",
            body={
                "target": "production-gate-receipt",
                "payload_template": {"evidence_ref": f"gate-{config.run_suffix}"},
            },
            idempotency_key=f"production-gate:{config.run_suffix}:callback",
            otel_trace_id=operation_otel_trace_ids["external_callback"],
            timeout_seconds=config.timeout_seconds,
            label="external callback",
        )
        callback_row = _run_row(config, callback_run_id)
        callback_event = _trace_bound_outbox_row(
            config,
            callback_run_id,
            expected_otel_trace_id=operation_otel_trace_ids["external_callback"],
        )
        callback_details = _dispatch_details(callback_row, adapter="external_callback")
        callback_receipt_id = _nonempty_text(
            callback_details.get("callback_receipt_id"), "callback receipt id"
        )
        validate_callback_dispatch_binding(
            callback_details,
            trace_id=callback_trace_id,
            run_id=callback_run_id,
            receipt_id=callback_receipt_id,
        )
        callback_proof = _support_proofs(browser, config, "callback")
        receipt_ids = callback_proof.get("receipt_ids")
        if (
            not isinstance(receipt_ids, list)
            or receipt_ids.count(callback_receipt_id) != 1
        ):
            raise VerifierFailure("callback receiver receipt binding is invalid")
        _write_capture(
            config,
            state,
            "callback_delivery",
            {
                "transport": callback_proof.get("transport"),
                "tls_verified": True,
                "signature_mode": callback_proof.get("signature_mode"),
                "signature_verified": callback_details.get("signature_mode")
                == "hmac-sha256-v2",
                "verified_receipt_count": 1,
                "receipt_id": callback_receipt_id,
                "trace_id": callback_trace_id,
            },
        )
        replay_status, _ = _support_control(
            browser, config, "/control/replay-last", expected=409
        )
        replay_proof = _support_proofs(browser, config, "callback")
        _write_capture(
            config,
            state,
            "callback_replay",
            {
                "http_status": replay_status,
                "error_code": replay_proof.get("replay_error_code"),
                "replay_rejected": replay_proof.get("replay_rejected"),
            },
        )

        authoritative_ids, processed_outbox_count = _authoritative_counts(
            config,
            [dagster_run_record_id, knowledge_run_id, object_run_id, callback_run_id],
        )
        if len(authoritative_ids) != 4 or processed_outbox_count != 4:
            raise VerifierFailure(
                "baseline requires four authoritative runs and four processed events"
            )
        _write_capture(
            config,
            state,
            "mysql_authority",
            {
                "tenant_id": SCOPE[0],
                "project_id": SCOPE[1],
                "authoritative_run_ids": authoritative_ids,
                "authoritative_run_count": len(authoritative_ids),
                "processed_outbox_count": processed_outbox_count,
            },
        )
        state["operations"] = {
            "trace_ids": {
                "oidc": oidc_trace_id,
                "dagster": dagster_trace_id,
                "object_storage": object_trace_id,
                "qdrant": qdrant_operation_trace_id,
                "external_callback": callback_trace_id,
            },
            "lineage_trace_ids": {"qdrant_point": qdrant_trace_id},
            "read_trace_ids": {"qdrant_recall": recall_trace_id},
            "otel_trace_id": otel_trace_id,
            "operation_otel_trace_ids": operation_otel_trace_ids,
            "run_ids": {
                "dagster": dagster_run_record_id,
                "qdrant": knowledge_run_id,
                "object_storage": object_run_id,
                "external_callback": callback_run_id,
            },
            "authoritative_run_ids": authoritative_ids,
            "dagster_run_id": real_dagster_run_id,
            "qdrant": {"collection": collection, "point_id": point_id},
            "object_storage": {
                "bucket": minio_observation["bucket"],
                "object_key": minio_observation["object_key"],
                "content_sha256": minio_observation["observed_content_sha256"],
            },
            "external_callback": {
                "run_id": callback_run_id,
                "event_id": int(callback_event["event_id"]),
                "receipt_id": callback_receipt_id,
            },
        }
        state["completed_phases"] = ["initial"]
        state["updated_at"] = _utc_now()
        write_json_idempotent(config.state_path(), state)
    finally:
        browser.close()


def _copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _persist_phase_state(
    config: GateConfig,
    prior: dict[str, Any],
    updated: dict[str, Any],
    phase: str,
    dependency: str,
) -> None:
    marker = _phase_marker(phase, dependency)
    if marker not in updated["completed_phases"]:
        updated["completed_phases"].append(marker)
    updated["updated_at"] = _utc_now()
    validate_artifact_value(updated)
    replace_json_state(config.state_path(), prior, updated)


def persist_inflight_fault_trace(
    config: GateConfig,
    prior: dict[str, Any],
    state: dict[str, Any],
    dependency: str,
) -> tuple[str, dict[str, Any]]:
    fault = _mapping(state["faults"].get(dependency), "fault state")
    persisted = fault.get("retry_otel_trace_id")
    if persisted is not None:
        trace_id = _nonempty_text(persisted, "persisted retry OTel trace id")
        if not re.fullmatch(r"[0-9a-f]{32}", trace_id) or int(trace_id, 16) == 0:
            raise VerifierFailure("persisted retry OTel trace id is invalid")
        return trace_id, prior
    trace_id = _new_otel_trace_id()
    fault["retry_otel_trace_id"] = trace_id
    state["updated_at"] = _utc_now()
    validate_artifact_value(state)
    replace_json_state(config.state_path(), prior, state)
    return trace_id, _copy(state)


def build_fault_verification_checkpoint(
    dependency: str,
    proofs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    proof_ids = recovery_proof_ids(dependency)
    if list(proofs) != proof_ids:
        raise VerifierFailure(
            "fault verification checkpoint proof inventory is invalid"
        )
    copied_proofs: dict[str, dict[str, Any]] = {}
    recovery_facts: dict[str, Any] = {}
    for proof_id in proof_ids:
        observations = dict(
            _mapping(proofs.get(proof_id), "fault verification checkpoint proof")
        )
        # capture_record enforces the exact versioned field set and the evidence
        # redaction policy before these facts become resumable state.
        capture_record(proof_id, observations)
        if any(
            key in recovery_facts and recovery_facts[key] != value
            for key, value in observations.items()
        ):
            raise VerifierFailure("fault verification checkpoint bindings conflict")
        recovery_facts.update(observations)
        copied_proofs[proof_id] = observations
    recovery_case_from_facts(dependency, recovery_facts)
    checkpoint = {
        "schema_version": FAULT_VERIFICATION_CHECKPOINT_SCHEMA,
        "dependency": dependency,
        "proof_ids": proof_ids,
        "proofs_sha256": canonical_sha256(copied_proofs),
        "proofs": copied_proofs,
    }
    validate_artifact_value(checkpoint)
    return checkpoint


def _validated_fault_verification_checkpoint(
    dependency: str, value: object
) -> dict[str, dict[str, Any]]:
    checkpoint = _mapping(value, "fault verification checkpoint")
    proof_ids = recovery_proof_ids(dependency)
    proofs = _mapping(checkpoint.get("proofs"), "fault verification checkpoint proofs")
    if (
        set(checkpoint)
        != {"schema_version", "dependency", "proof_ids", "proofs_sha256", "proofs"}
        or checkpoint.get("schema_version") != FAULT_VERIFICATION_CHECKPOINT_SCHEMA
        or checkpoint.get("dependency") != dependency
        or checkpoint.get("proof_ids") != proof_ids
        or list(proofs) != proof_ids
        or checkpoint.get("proofs_sha256") != canonical_sha256(proofs)
    ):
        raise VerifierFailure("fault verification checkpoint is invalid")
    # Rebuild instead of trusting a self-declared hash. This repeats the closed
    # field, redaction and cross-proof recovery checks on every process entry.
    rebuilt = build_fault_verification_checkpoint(dependency, proofs)
    if rebuilt != checkpoint:
        raise VerifierFailure("fault verification checkpoint is invalid")
    return {
        proof_id: dict(_mapping(proofs[proof_id], "fault verification proof"))
        for proof_id in proof_ids
    }


def _complete_fault_verification_checkpoint(
    config: GateConfig,
    state: dict[str, Any],
    dependency: str,
) -> None:
    fault = _mapping(state["faults"].get(dependency), "fault state")
    proofs = _validated_fault_verification_checkpoint(
        dependency, fault.get("verification_checkpoint")
    )
    prior = _copy(state)
    for proof_id in recovery_proof_ids(dependency):
        _write_capture(config, state, proof_id, proofs[proof_id])
    fault.pop("verification_checkpoint", None)
    fault["verified_at"] = _utc_now()
    _persist_phase_state(config, prior, state, "fault-verify", dependency)


def _authority_run_count(config: GateConfig, state: dict[str, Any]) -> int:
    run_ids = state["operations"].get("authoritative_run_ids")
    if not isinstance(run_ids, list):
        raise VerifierFailure("authoritative run state is invalid")
    observed, _ = _authoritative_counts(config, run_ids)
    return len(observed)


def readiness_observation(
    status_code: int,
    payload: object,
    *,
    target_dependency: str | None,
    expect_ready: bool,
) -> dict[str, Any]:
    envelope = _mapping(payload, "readiness envelope")
    data = _mapping(envelope.get("data"), "readiness data")
    checks = _mapping(data.get("checks"), "readiness checks")
    missing = _mapping(data.get("missing_required"), "missing readiness checks")
    raw_required = data.get("required_checks")
    if (
        set(envelope) != {"status", "data"}
        or not isinstance(raw_required, list)
        or not raw_required
        or any(not isinstance(item, str) or not item for item in raw_required)
        or len(set(raw_required)) != len(raw_required)
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in checks.items()
        )
        or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in missing.items()
        )
    ):
        raise VerifierFailure("readiness response shape is invalid")
    required = set(raw_required)
    if any(checks.get(item) is None for item in required):
        raise VerifierFailure("readiness response omits a required dependency")
    if target_dependency is not None and target_dependency not in {"qdrant", "redis"}:
        raise VerifierFailure("readiness target dependency is invalid")

    if expect_ready:
        valid = (
            status_code == 200
            and envelope.get("status") == "ok"
            and data.get("status") == "success"
            and not missing
            and all(checks.get(item) == "ok" for item in required)
            and (target_dependency is None or target_dependency in required)
        )
    else:
        valid = (
            status_code == 503
            and envelope.get("status") == "degraded"
            and data.get("status") == "failed"
            and target_dependency is not None
            and target_dependency in required
            and checks.get(target_dependency) == "not_ready"
            and missing == {target_dependency: "not_ready"}
            and all(
                checks.get(item) == "ok"
                for item in required
                if item != target_dependency
            )
        )
    if not valid:
        raise VerifierFailure(
            "readiness response does not prove the expected dependency state"
        )

    observation: dict[str, Any] = {
        "http_status": status_code,
        "envelope_status": envelope.get("status"),
        "data_status": data.get("status"),
        "missing_required": sorted(missing),
    }
    if target_dependency is not None:
        observation.update(
            {
                "target_dependency": target_dependency,
                "target_status": checks.get(target_dependency),
                "target_required": target_dependency in required,
            }
        )
    return observation


def _ready_observation(
    browser: BrowserClient,
    config: GateConfig,
    *,
    target_dependency: str | None = None,
    expect_ready: bool,
) -> dict[str, Any]:
    expected_status = 200 if expect_ready else 503
    response = browser.raw(
        "GET",
        f"{config.public_url}/readyz",
        expected={expected_status},
        label="production readiness",
    )
    payload = _safe_json_response(response, "production readiness")
    return readiness_observation(
        response.status_code,
        payload,
        target_dependency=target_dependency,
        expect_ready=expect_ready,
    )


def _ready_status(
    browser: BrowserClient, config: GateConfig, expected: set[int]
) -> int:
    if expected != {200}:
        raise VerifierFailure("readiness status helper only accepts a healthy response")
    observation = _ready_observation(
        browser,
        config,
        expect_ready=True,
    )
    return int(observation["http_status"])


def _attempt_rows(config: GateConfig, event_id: int) -> list[dict[str, Any]]:
    rows = config.database.rows(
        "SELECT attempt_id, event_id, tenant_id, project_id, attempt_number, "
        "lease_generation, claimed_by, claim_token_sha256, delivery_mode, status, "
        "dispatch_idempotency_key, request_sha256, adapter, operation, remote_id, "
        "error_code, error_message, started_at, completed_at, details "
        "FROM outbox_delivery_attempts "
        "WHERE tenant_id=:tenant_id AND project_id=:project_id AND event_id=:event_id "
        "ORDER BY lease_generation, started_at, attempt_id",
        {"tenant_id": SCOPE[0], "project_id": SCOPE[1], "event_id": event_id},
    )
    for row in rows:
        row["details"] = _json_mapping(row.get("details"), "outbox attempt details")
    return rows


def fencing_observation_from_claims(
    first_claim: object,
    second_claim: object,
    *,
    stale_owner: object | None,
    current_owner: object | None,
) -> dict[str, Any]:
    event_id = _positive_int(getattr(first_claim, "event_id", None), "claim event id")
    second_event_id = _positive_int(
        getattr(second_claim, "event_id", None), "replacement claim event id"
    )
    generation_before = _positive_int(
        getattr(first_claim, "lease_generation", None), "lease generation before"
    )
    generation_after = _positive_int(
        getattr(second_claim, "lease_generation", None), "lease generation after"
    )
    first_token = getattr(first_claim, "claim_token", None)
    second_token = getattr(second_claim, "claim_token", None)
    current_event_id = getattr(current_owner, "event_id", None)
    stale_owner_rejected = stale_owner is None
    new_owner_accepted = current_owner is not None and current_event_id == event_id
    if (
        event_id != second_event_id
        or generation_after != generation_before + 1
        or not isinstance(first_token, str)
        or not first_token
        or not isinstance(second_token, str)
        or not second_token
        or first_token == second_token
        or not stale_owner_rejected
        or not new_owner_accepted
    ):
        raise VerifierFailure("outbox lease fencing observation is invalid")
    return {
        "stale_owner_rejected": stale_owner_rejected,
        "new_owner_accepted": new_owner_accepted,
        "lease_generation_before": generation_before,
        "lease_generation_after": generation_after,
        "claim_token_sha256_before": hashlib.sha256(
            first_token.encode("utf-8")
        ).hexdigest(),
        "claim_token_sha256_after": hashlib.sha256(
            second_token.encode("utf-8")
        ).hexdigest(),
    }


def _exercise_duplicate_lease_fencing(
    config: GateConfig,
    *,
    run_id: str,
    event_id: int,
) -> dict[str, Any]:
    try:
        from datetime import timedelta

        from app.core.database import SessionLocal
        from app.models import OutboxEvent
        from app.repositories.outbox_events import (
            claim_events,
            database_utc_now,
            lock_owned_claim,
        )

        with SessionLocal() as session:
            first_claims = claim_events(
                session,
                worker_id=f"production-gate-stale-{config.run_suffix}",
                limit=1,
                lease_seconds=30,
                max_attempts_cap=8,
                aggregate_ids=[run_id],
            )
            if len(first_claims) != 1 or first_claims[0].event_id != event_id:
                raise VerifierFailure(
                    "first fencing claim did not bind the target event"
                )
            first_claim = first_claims[0]
            session.commit()

        with SessionLocal() as session:
            event = session.get(OutboxEvent, event_id)
            if (
                event is None
                or event.tenant_id != SCOPE[0]
                or event.project_id != SCOPE[1]
                or event.aggregate_id != run_id
                or event.claim_token != first_claim.claim_token
                or event.lease_generation != first_claim.lease_generation
            ):
                raise VerifierFailure("first fencing claim scope is invalid")
            event.lease_expires_at = database_utc_now(session) - timedelta(seconds=1)
            session.commit()

        with SessionLocal() as session:
            second_claims = claim_events(
                session,
                worker_id=f"production-gate-current-{config.run_suffix}",
                limit=1,
                lease_seconds=30,
                max_attempts_cap=8,
                aggregate_ids=[run_id],
            )
            if len(second_claims) != 1 or second_claims[0].event_id != event_id:
                raise VerifierFailure(
                    "replacement fencing claim did not bind the target event"
                )
            second_claim = second_claims[0]
            session.commit()

        with SessionLocal() as session:
            stale_owner = lock_owned_claim(session, first_claim)

        with SessionLocal() as session:
            current_owner = lock_owned_claim(session, second_claim)
            observation = fencing_observation_from_claims(
                first_claim,
                second_claim,
                stale_owner=stale_owner,
                current_owner=current_owner,
            )
            if current_owner is None:
                raise VerifierFailure("replacement fencing owner disappeared")
            current_owner.lease_expires_at = database_utc_now(session) - timedelta(
                seconds=1
            )
            session.commit()
        return observation
    except VerifierFailure:
        raise
    except Exception as exc:
        raise VerifierFailure("outbox lease fencing observation failed") from exc


def _wait_outbox(
    config: GateConfig,
    run_id: str,
    *,
    statuses: set[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = "unknown"
    while time.monotonic() < deadline:
        row = _outbox_row(config, run_id)
        last = str(row.get("status") or "unknown")
        if last in statuses:
            return row
        if last == "dead_letter":
            raise VerifierFailure("outbox event reached dead letter")
        time.sleep(0.2)
    raise VerifierFailure(f"outbox observation timed out at status {last}")


def _fresh_browser(config: GateConfig) -> BrowserClient:
    browser, _, _ = _authorize_oidc(config)
    return browser


def _fault_prepare(config: GateConfig, dependency: str) -> None:
    state = _load_state(config)
    if not _phase_completed(state, "initial", "none"):
        raise VerifierFailure("fault preparation requires a completed initial phase")
    if _phase_completed(state, "fault-prepare", dependency):
        return
    prior = _copy(state)
    faults = state["faults"]
    if dependency in faults:
        raise VerifierFailure("fault state already exists without a completed phase")
    fault: dict[str, Any] = {
        "dependency": dependency,
        "authoritative_run_count_before": _authority_run_count(config, state),
        "prepared_at": _utc_now(),
    }

    if dependency == "worker_crash":
        fault["event_status_before"] = "not_created"
    elif dependency == "duplicate_delivery":
        callback = _mapping(
            state["operations"].get("external_callback"), "callback operation state"
        )
        event_id = _positive_int(callback.get("event_id"), "callback event id")
        event = config.database.one(
            "SELECT event_id, status, delivery_state, attempt_count, reconcile_attempt_count "
            "FROM outbox_events WHERE event_id=:event_id",
            {"event_id": event_id},
        )
        attempts = _attempt_rows(config, event_id)
        if (
            event.get("status") != "processed"
            or event.get("delivery_state") != "confirmed"
            or len(attempts) != 1
            or attempts[0].get("delivery_mode") != "dispatch"
            or attempts[0].get("status") != "succeeded"
        ):
            raise VerifierFailure(
                "duplicate-delivery source event is not a confirmed single delivery"
            )
        fault.update(
            {
                "event_id": event_id,
                "run_id": _nonempty_text(callback.get("run_id"), "callback run id"),
                "receipt_id": _nonempty_text(
                    callback.get("receipt_id"), "callback receipt id"
                ),
                "delivery_attempt_count_before": 1,
            }
        )
    elif dependency == "callback_timeout":
        browser = _fresh_browser(config)
        try:
            _support_control(browser, config, "/control/timeout-next", expected=200)
            created = browser.bff(
                "POST",
                "/api/v1/output-sinks/platform-callbacks",
                body={
                    "target": "production-gate-timeout-reconcile",
                    "payload_template": {"fault": "callback-timeout"},
                },
                expected=202,
                idempotency_key=f"production-gate:{config.run_suffix}:callback-timeout",
                label="callback timeout run creation",
            )
            data = _response_data(created, "callback timeout run creation")
            run_id = _nonempty_text(data.get("run_id"), "callback timeout run id")
            trace_id = _nonempty_text(data.get("trace_id"), "callback timeout trace id")
            event = _outbox_row(config, run_id)
            event_id = _positive_int(event.get("event_id"), "callback timeout event id")
            deadline = time.monotonic() + min(config.timeout_seconds, 60)
            observed_unknown = False
            while time.monotonic() < deadline:
                event = _outbox_row(config, run_id)
                attempts = _attempt_rows(config, event_id)
                observed_unknown = any(
                    item.get("status") == "reconcile_retry_scheduled"
                    and item.get("delivery_mode") == "dispatch"
                    for item in attempts
                )
                if observed_unknown:
                    break
                time.sleep(0.2)
            if not observed_unknown:
                raise VerifierFailure(
                    "callback timeout did not produce an unknown remote outcome"
                )
            fault.update({"event_id": event_id, "run_id": run_id, "trace_id": trace_id})
        finally:
            browser.close()
    elif dependency in {
        "dead_letter_retry",
        "qdrant_outage",
        "redis_outage",
        "mysql_restart",
    }:
        if dependency == "qdrant_outage":
            qdrant = _mapping(
                state["operations"].get("qdrant"), "Qdrant operation state"
            )
            fault["point_id"] = _nonempty_text(
                qdrant.get("point_id"), "Qdrant point id"
            )
            fault["collection"] = _nonempty_text(
                qdrant.get("collection"), "Qdrant collection"
            )
    else:
        raise VerifierFailure("fault preparation dependency is unsupported")
    faults[dependency] = fault
    _persist_phase_state(config, prior, state, "fault-prepare", dependency)


def _fault_during(config: GateConfig, dependency: str) -> None:
    state = _load_state(config)
    if not _phase_completed(state, "fault-prepare", dependency):
        raise VerifierFailure("fault-during requires its preparation phase")
    if _phase_completed(state, "fault-during", dependency):
        return
    prior = _copy(state)
    fault = _mapping(state["faults"].get(dependency), "fault state")

    if dependency == "worker_crash":
        browser = _fresh_browser(config)
        try:
            created = browser.bff(
                "POST",
                "/api/v1/task-runs",
                body={
                    "task_version_id": "task_version_v3_2_1",
                    "trigger_type": "manual",
                    "execution_mode": "production",
                    "partition_key": f"production-gate/{config.run_suffix}/worker-crash",
                },
                expected=202,
                idempotency_key=f"production-gate:{config.run_suffix}:worker-crash",
                label="worker crash task creation",
            )
            data = _response_data(created, "worker crash task creation")
            run_id = _nonempty_text(data.get("run_id"), "worker crash run id")
            trace_id = _nonempty_text(data.get("trace_id"), "worker crash trace id")
            event = _outbox_row(config, run_id)
            if (
                event.get("status") != "pending"
                or int(event.get("attempt_count") or 0) != 0
            ):
                raise VerifierFailure(
                    "outbox did not remain pending while Worker was down"
                )
            fault.update(
                {
                    "event_id": _positive_int(
                        event.get("event_id"), "worker crash event id"
                    ),
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "event_status_before": "pending",
                }
            )
        finally:
            browser.close()
    elif dependency in {"qdrant_outage", "redis_outage"}:
        browser = BrowserClient(config)
        try:
            target_dependency = dependency.removesuffix("_outage")
            fault["readiness_during"] = _ready_observation(
                browser,
                config,
                target_dependency=target_dependency,
                expect_ready=False,
            )
        finally:
            browser.close()
    elif dependency == "duplicate_delivery":
        event_id = _positive_int(fault.get("event_id"), "duplicate event id")
        run_id = _nonempty_text(fault.get("run_id"), "duplicate run id")
        changed = config.database.execute(
            "UPDATE outbox_events SET status='pending', delivery_state='outcome_unknown', "
            "processed_at=NULL, available_at=CURRENT_TIMESTAMP, claim_token=NULL, claimed_by=NULL, "
            "claimed_at=NULL, lease_expires_at=NULL WHERE event_id=:event_id "
            "AND tenant_id=:tenant_id AND project_id=:project_id "
            "AND aggregate_id=:run_id AND status='processed' AND delivery_state='confirmed'",
            {
                "event_id": event_id,
                "tenant_id": SCOPE[0],
                "project_id": SCOPE[1],
                "run_id": run_id,
            },
        )
        if changed != 1:
            raise VerifierFailure(
                "duplicate-delivery fault injection did not update one event"
            )
        fault.update(
            _exercise_duplicate_lease_fencing(
                config,
                run_id=run_id,
                event_id=event_id,
            )
        )
        fault["delivery_attempts_observed_during"] = len(
            _attempt_rows(config, event_id)
        )
    elif dependency == "callback_timeout":
        event_id = _positive_int(fault.get("event_id"), "fault event id")
        attempts = _attempt_rows(config, event_id)
        fault["delivery_attempts_observed_during"] = len(attempts)
    elif dependency == "dead_letter_retry":
        browser = _fresh_browser(config)
        try:
            created = browser.bff(
                "POST",
                "/api/v1/knowledge-indexes/ki_sales_policy_v1/build-runs",
                body={
                    "reason": "production gate dead-letter recovery",
                    "chunk_policy": "production-gate-dead-letter",
                    "embedding_text": "死信人工重试必须保持作用域、追踪与幂等绑定",
                },
                expected=202,
                idempotency_key=(
                    f"production-gate:{config.run_suffix}:dead-letter-source"
                ),
                label="dead-letter source run creation",
            )
            data = _response_data(created, "dead-letter source run creation")
            run_id = _nonempty_text(data.get("run_id"), "dead-letter source run id")
            trace_id = _nonempty_text(
                data.get("trace_id"), "dead-letter source trace id"
            )
            event = _outbox_row(config, run_id)
            event_payload = _json_mapping(
                event.get("payload"), "dead-letter source event payload"
            )
            qdrant_payload = _mapping(
                event_payload.get("qdrant_payload"),
                "dead-letter source Qdrant payload",
            )
            event_data = _mapping(
                event_payload.get("data"), "dead-letter source event data"
            )
            data_qdrant_payload = _mapping(
                event_data.get("qdrant_payload"),
                "dead-letter source event data Qdrant payload",
            )
            if (
                event.get("status") != "pending"
                or int(event.get("attempt_count") or 0) != 0
                or not data_qdrant_payload.get("business_ref")
            ):
                raise VerifierFailure(
                    "dead-letter source event was not fenced while Worker was down"
                )
            event_id = _positive_int(
                event.get("event_id"), "dead-letter source event id"
            )
            if qdrant_payload.get("business_ref"):
                changed = config.database.execute(
                    "UPDATE outbox_events SET "
                    "payload=JSON_REMOVE(payload, '$.qdrant_payload.business_ref') "
                    "WHERE event_id=:event_id AND tenant_id=:tenant_id "
                    "AND project_id=:project_id AND aggregate_id=:run_id "
                    "AND event_type='knowledge_index.build_requested' "
                    "AND status='pending' AND attempt_count=0",
                    {
                        "event_id": event_id,
                        "tenant_id": SCOPE[0],
                        "project_id": SCOPE[1],
                        "run_id": run_id,
                    },
                )
                if changed != 1:
                    raise VerifierFailure(
                        "dead-letter source fault injection did not update one event"
                    )
            injected_event = _outbox_row(config, run_id)
            injected_payload = _json_mapping(
                injected_event.get("payload"), "injected dead-letter source event"
            )
            injected_qdrant = _mapping(
                injected_payload.get("qdrant_payload"),
                "injected dead-letter Qdrant payload",
            )
            injected_data = _mapping(
                injected_payload.get("data"), "injected dead-letter source data"
            )
            injected_data_qdrant = _mapping(
                injected_data.get("qdrant_payload"),
                "injected dead-letter source data Qdrant payload",
            )
            if (
                injected_event.get("event_id") != event_id
                or injected_event.get("status") != "pending"
                or int(injected_event.get("attempt_count") or 0) != 0
                or injected_qdrant.get("business_ref") is not None
                or not injected_data_qdrant.get("business_ref")
            ):
                raise VerifierFailure("dead-letter source fault state is inconsistent")
            fault.update(
                {
                    "source_run_id": run_id,
                    "source_trace_id": trace_id,
                    "source_event_id": event_id,
                }
            )
        finally:
            browser.close()
    else:
        raise VerifierFailure("fault-during dependency is unsupported")
    _persist_phase_state(config, prior, state, "fault-during", dependency)


def _fault_verify(config: GateConfig, dependency: str) -> None:
    state = _load_state(config)
    if not _phase_completed(state, "fault-prepare", dependency):
        raise VerifierFailure("fault verification requires its preparation phase")
    if _phase_completed(state, "fault-verify", dependency):
        for proof_id in recovery_proof_ids(dependency):
            _read_capture(config, state, proof_id)
        return
    prior = _copy(state)
    fault = _mapping(state["faults"].get(dependency), "fault state")
    if fault.get("verification_checkpoint") is not None:
        _complete_fault_verification_checkpoint(config, state, dependency)
        return
    before_count = _positive_int(
        fault.get("authoritative_run_count_before"), "authoritative run count before"
    )
    browser = BrowserClient(config)
    supplemental_facts: dict[str, dict[str, Any]] = {}
    try:
        if dependency == "mysql_restart":
            host = load_host_observation(
                config.host_observation_path(dependency), dependency
            )
            ready_after = _ready_status(browser, config, {200})
            facts = {
                "container_id_sha256": host["container_id_sha256"],
                "started_at_before": host["started_at_before"],
                "started_at_after": host["started_at_after"],
                "ready_status_after": ready_after,
            }
        elif dependency == "worker_crash":
            host = load_host_observation(
                config.host_observation_path(dependency), dependency
            )
            authenticated = _fresh_browser(config)
            try:
                run_id = _nonempty_text(fault.get("run_id"), "worker crash run id")
                final = _wait_run(
                    authenticated,
                    run_id,
                    expected={"success"},
                    timeout_seconds=config.timeout_seconds,
                )
                event = _wait_outbox(
                    config,
                    run_id,
                    statuses={"processed"},
                    timeout_seconds=config.timeout_seconds,
                )
                details = _dispatch_details(_run_row(config, run_id), adapter="dagster")
                remote_id = _nonempty_text(
                    details.get("external_run_id"), "worker crash remote run"
                )
                outbox_payload = _json_mapping(
                    event.get("payload"), "worker crash outbox payload"
                )
                dispatch_key = _nonempty_text(
                    outbox_payload.get("dispatch_idempotency_key"),
                    "worker crash dispatch key",
                )
                remote_run_count = _dagster_remote_run_count(
                    authenticated,
                    run_key=dispatch_key,
                    expected_run_id=remote_id,
                )
                if final.get("status") != "success":
                    raise VerifierFailure("worker crash task did not recover")
                facts = {
                    "container_id_sha256": host["container_id_sha256"],
                    "started_at_before": host["started_at_before"],
                    "started_at_after": host["started_at_after"],
                    "event_id": _positive_int(
                        event.get("event_id"), "worker crash event id"
                    ),
                    "event_status_before": fault.get("event_status_before"),
                    "event_status_after": event.get("status"),
                    "remote_run_count": remote_run_count,
                }
            finally:
                authenticated.close()
        elif dependency == "duplicate_delivery":
            if not _phase_completed(state, "fault-during", dependency):
                raise VerifierFailure(
                    "duplicate delivery lacks its fenced during-fault observation"
                )
            event_id = _positive_int(fault.get("event_id"), "duplicate event id")
            run_id = _nonempty_text(fault.get("run_id"), "duplicate run id")
            event = _wait_outbox(
                config,
                run_id,
                statuses={"processed"},
                timeout_seconds=config.timeout_seconds,
            )
            attempts = _attempt_rows(config, event_id)
            dispatch_attempts = [
                item for item in attempts if item.get("delivery_mode") == "dispatch"
            ]
            reconcile_attempts = [
                item for item in attempts if item.get("delivery_mode") == "reconcile"
            ]
            callback_proof = _support_proofs(browser, config, "callback")
            receipt_ids = callback_proof.get("receipt_ids")
            receipt_id = _nonempty_text(fault.get("receipt_id"), "duplicate receipt id")
            remote_receipt_count = (
                receipt_ids.count(receipt_id) if isinstance(receipt_ids, list) else 0
            )
            run_count = config.database.one(
                "SELECT COUNT(*) AS count FROM run_records WHERE run_id=:run_id "
                "AND tenant_id=:tenant_id AND project_id=:project_id AND status='success'",
                {"run_id": run_id, "tenant_id": SCOPE[0], "project_id": SCOPE[1]},
            )
            facts = {
                "event_id": event_id,
                "delivery_attempt_count": len(attempts),
                "dispatch_attempt_count": len(dispatch_attempts),
                "reconcile_attempt_count": len(reconcile_attempts),
                "remote_receipt_count": remote_receipt_count,
                "business_outcome_count": int(run_count.get("count") or 0),
                "stale_owner_rejected": fault.get("stale_owner_rejected"),
                "new_owner_accepted": fault.get("new_owner_accepted"),
                "lease_generation_before": fault.get("lease_generation_before"),
                "lease_generation_after": fault.get("lease_generation_after"),
                "claim_token_sha256_before": fault.get("claim_token_sha256_before"),
                "claim_token_sha256_after": fault.get("claim_token_sha256_after"),
            }
            if event.get("status") != "processed":
                raise VerifierFailure("duplicate delivery did not recover")
        elif dependency == "callback_timeout":
            event_id = _positive_int(fault.get("event_id"), "callback timeout event id")
            run_id = _nonempty_text(fault.get("run_id"), "callback timeout run id")
            authenticated = _fresh_browser(config)
            try:
                _wait_run(
                    authenticated,
                    run_id,
                    expected={"success"},
                    timeout_seconds=config.timeout_seconds,
                )
            finally:
                authenticated.close()
            event = _wait_outbox(
                config,
                run_id,
                statuses={"processed"},
                timeout_seconds=config.timeout_seconds,
            )
            attempts = _attempt_rows(config, event_id)
            if len(attempts) < 2:
                raise VerifierFailure(
                    "callback timeout lacks dispatch and reconciliation attempts"
                )
            first, final = attempts[0], attempts[-1]
            row = _run_row(config, run_id)
            details = _dispatch_details(row, adapter="external_callback")
            receipt_id = _nonempty_text(
                details.get("callback_receipt_id"), "callback timeout receipt id"
            )
            callback_proof = _support_proofs(browser, config, "callback")
            receipt_ids = callback_proof.get("receipt_ids")
            facts = {
                "event_id": event_id,
                "first_attempt_status": (
                    "outcome_unknown"
                    if first.get("status") == "reconcile_retry_scheduled"
                    else str(first.get("status") or "")
                ),
                "final_attempt_status": (
                    "success"
                    if final.get("status") == "succeeded"
                    else str(final.get("status") or "")
                ),
                "final_delivery_mode": final.get("delivery_mode"),
                "remote_receipt_count": (
                    receipt_ids.count(receipt_id)
                    if isinstance(receipt_ids, list)
                    else 0
                ),
            }
            if event.get("delivery_state") != "confirmed":
                raise VerifierFailure(
                    "callback timeout reconciliation was not confirmed"
                )
        elif dependency == "dead_letter_retry":
            source_run_id = _nonempty_text(
                fault.get("source_run_id"), "dead-letter source run id"
            )
            source_trace_id = _nonempty_text(
                fault.get("source_trace_id"), "dead-letter source trace id"
            )
            source_event_id = _positive_int(
                fault.get("source_event_id"), "dead-letter source event id"
            )
            retry_reason = "operator restored the derived Qdrant payload"
            retry_key = f"production-gate:{config.run_suffix}:dead-letter-retry"
            retry_body = {"reason": retry_reason}
            retry_otel_trace_id, prior = persist_inflight_fault_trace(
                config, prior, state, dependency
            )
            authenticated = _fresh_browser(config)
            try:
                _wait_run(
                    authenticated,
                    source_run_id,
                    expected={"failed"},
                    timeout_seconds=config.timeout_seconds,
                )
                source_event = _wait_outbox(
                    config,
                    source_run_id,
                    statuses={"dead_letter"},
                    timeout_seconds=config.timeout_seconds,
                )
                source_run = _run_row(config, source_run_id)
                source_attempts = _attempt_rows(config, source_event_id)
                source_payload = _json_mapping(
                    source_run.get("payload"), "dead-letter source run payload"
                )
                source_event_payload = _json_mapping(
                    source_event.get("payload"), "dead-letter source event payload"
                )
                source_event_data = _mapping(
                    source_event_payload.get("data"), "dead-letter source event data"
                )
                source_event_data_qdrant = _mapping(
                    source_event_data.get("qdrant_payload"),
                    "dead-letter source event data Qdrant payload",
                )
                source_event_qdrant = _mapping(
                    source_event_payload.get("qdrant_payload"),
                    "dead-letter source event Qdrant payload",
                )
                if len(source_attempts) != 1:
                    raise VerifierFailure(
                        "source event did not produce exactly one delivery attempt"
                    )
                source_attempt = source_attempts[0]
                source_attempt_details = _json_mapping(
                    source_attempt.get("details"), "dead-letter source attempt details"
                )
                failed_dispatch = _mapping(
                    source_attempt_details.get("failed_dispatch"),
                    "dead-letter source failed dispatch",
                )
                failed_dispatch_details = _mapping(
                    failed_dispatch.get("details"),
                    "dead-letter source failure details",
                )
                if (
                    source_event.get("event_id") != source_event_id
                    or source_event.get("tenant_id") != SCOPE[0]
                    or source_event.get("project_id") != SCOPE[1]
                    or source_event.get("event_type")
                    != "knowledge_index.build_requested"
                    or source_event.get("aggregate_type") != "knowledge_build"
                    or source_event.get("aggregate_id") != source_run_id
                    or source_event.get("delivery_state") != "failed"
                    or source_event.get("attempt_count") != 1
                    or source_event.get("reconcile_attempt_count") != 0
                    or source_event.get("lease_generation") != 1
                    or not all(
                        bool(source_event.get(field))
                        for field in (
                            "claim_token_cleared",
                            "claimed_by_cleared",
                            "claimed_at_cleared",
                            "lease_expires_at_cleared",
                        )
                    )
                    or not isinstance(source_event.get("last_error"), str)
                    or not str(source_event["last_error"]).startswith(
                        "QDRANT_PAYLOAD_INVALID:"
                    )
                    or source_event.get("processed_at") is None
                    or source_run.get("tenant_id") != SCOPE[0]
                    or source_run.get("project_id") != SCOPE[1]
                    or source_run.get("run_type") != "knowledge_build"
                    or source_run.get("status") != "failed"
                    or source_run.get("terminal_reason")
                    != "outbox_dispatch_dead_letter"
                    or source_payload.get("dead_letter_event_id") != source_event_id
                    or source_event_qdrant.get("business_ref") is not None
                    or not source_event_data_qdrant.get("business_ref")
                    or source_attempt.get("event_id") != source_event_id
                    or source_attempt.get("tenant_id") != SCOPE[0]
                    or source_attempt.get("project_id") != SCOPE[1]
                    or source_attempt.get("attempt_number") != 1
                    or source_attempt.get("lease_generation") != 1
                    or source_attempt.get("status") != "dead_letter"
                    or source_attempt.get("delivery_mode") != "dispatch"
                    or source_attempt.get("adapter") != "qdrant"
                    or source_attempt.get("operation") != "upsert_payload"
                    or source_attempt.get("error_code") != "QDRANT_PAYLOAD_INVALID"
                    or not isinstance(source_attempt.get("error_message"), str)
                    or not source_attempt.get("error_message")
                    or source_attempt.get("remote_id") is not None
                    or source_attempt.get("started_at") is None
                    or source_attempt.get("completed_at") is None
                    or not isinstance(source_attempt.get("claimed_by"), str)
                    or not source_attempt.get("claimed_by")
                    or SHA256_PATTERN.fullmatch(
                        str(source_attempt.get("claim_token_sha256") or "")
                    )
                    is None
                    or SHA256_PATTERN.fullmatch(
                        str(source_event.get("dispatch_request_sha256") or "")
                    )
                    is None
                    or source_attempt.get("dispatch_idempotency_key")
                    != source_event.get("dispatch_idempotency_key")
                    or source_attempt.get("request_sha256")
                    != source_event.get("dispatch_request_sha256")
                    or failed_dispatch.get("adapter") != "qdrant"
                    or failed_dispatch.get("operation") != "upsert_payload"
                    or failed_dispatch.get("status") != "failed"
                    or failed_dispatch.get("error_code") != "QDRANT_PAYLOAD_INVALID"
                    or failed_dispatch.get("retryable") is not False
                    or failed_dispatch_details.get("missing_fields") != ["business_ref"]
                ):
                    raise VerifierFailure(
                        "source event did not reach one terminal Qdrant dead letter"
                    )
                source_attempt_ledger_before = _attempt_ledger_sha256(source_attempts)
                source_snapshot_before = _dead_letter_source_snapshot(
                    source_run, source_event, source_attempts
                )
                retry_response = authenticated.bff(
                    "POST",
                    f"/api/v1/runs/{source_run_id}/retries",
                    body=retry_body,
                    expected=202,
                    idempotency_key=retry_key,
                    otel_trace_id=retry_otel_trace_id,
                    label="dead-letter manual retry",
                )
                retry_data = _response_data(retry_response, "dead-letter manual retry")
                retry_run_id = _nonempty_text(
                    retry_data.get("run_id"), "dead-letter retry run id"
                )
                replay_response = authenticated.bff(
                    "POST",
                    f"/api/v1/runs/{source_run_id}/retries",
                    body=retry_body,
                    expected=202,
                    idempotency_key=retry_key,
                    label="dead-letter manual retry replay",
                )
                replay_data = _response_data(
                    replay_response, "dead-letter manual retry replay"
                )
                if (
                    retry_run_id == source_run_id
                    or replay_data.get("run_id") != retry_run_id
                    or canonical_sha256(retry_response)
                    != canonical_sha256(replay_response)
                    or retry_data.get("retry_of_run_id") != source_run_id
                    or retry_data.get("retry_of_event_id") != source_event_id
                    or retry_data.get("retry_of_trace_id") != source_trace_id
                    or retry_data.get("trace_id") != source_trace_id
                ):
                    raise VerifierFailure(
                        "dead-letter retry did not preserve identity and trace fencing"
                    )
                retry_final = _wait_run(
                    authenticated,
                    retry_run_id,
                    expected={"success"},
                    timeout_seconds=config.timeout_seconds,
                )
            finally:
                authenticated.close()

            retry_event = _wait_outbox(
                config,
                retry_run_id,
                statuses={"processed"},
                timeout_seconds=config.timeout_seconds,
            )
            retry_event_id = _positive_int(
                retry_event.get("event_id"), "dead-letter retry event id"
            )
            retry_attempts = _attempt_rows(config, retry_event_id)
            retry_run = _run_row(config, retry_run_id)
            retry_details = _dispatch_details(retry_run, adapter="qdrant")
            retry_payload = _json_mapping(
                retry_run.get("payload"), "dead-letter retry run payload"
            )
            retry_event_payload = _json_mapping(
                retry_event.get("payload"), "dead-letter retry event payload"
            )
            retry_event_data = _mapping(
                retry_event_payload.get("data"), "dead-letter retry event data"
            )
            retry_event_qdrant = _mapping(
                retry_event_payload.get("qdrant_payload"),
                "dead-letter retry event Qdrant payload",
            )
            retry_event_data_qdrant = _mapping(
                retry_event_data.get("qdrant_payload"),
                "dead-letter retry event data Qdrant payload",
            )
            retry_event_subject = _mapping(
                retry_event_payload.get("subject"), "dead-letter retry event subject"
            )
            retry_event_otel_trace_id = otel_trace_id_from_carrier(
                retry_event_payload.get("otel_trace_context")
            )
            point_ids = retry_details.get("point_ids")
            if len(retry_attempts) != 1:
                raise VerifierFailure(
                    "dead-letter retry did not produce exactly one delivery attempt"
                )
            retry_attempt = retry_attempts[0]
            retry_attempt_details = _json_mapping(
                retry_attempt.get("details"), "dead-letter retry attempt details"
            )
            retry_attempt_dispatch = _mapping(
                retry_attempt_details.get("dispatch_details"),
                "dead-letter retry attempt dispatch",
            )
            retry_attempt_payload = _mapping(
                retry_attempt_dispatch.get("qdrant_payload"),
                "dead-letter retry attempt Qdrant payload",
            )
            retry_attempt_point_ids = retry_attempt_dispatch.get("point_ids")
            expected_retry_attempt_id = f"outbox_attempt_{retry_event_id}_1"
            if (
                retry_event_id == source_event_id
                or retry_event.get("tenant_id") != SCOPE[0]
                or retry_event.get("project_id") != SCOPE[1]
                or retry_event.get("event_type") != "knowledge_index.build_requested"
                or retry_event.get("aggregate_type") != "knowledge_build"
                or retry_event.get("aggregate_id") != retry_run_id
                or retry_event.get("status") != "processed"
                or retry_event.get("delivery_state") != "confirmed"
                or retry_event.get("attempt_count") != 1
                or retry_event.get("reconcile_attempt_count") != 0
                or retry_event.get("lease_generation") != 1
                or retry_event.get("last_error") is not None
                or retry_event.get("processed_at") is None
                or not all(
                    bool(retry_event.get(field))
                    for field in (
                        "claim_token_cleared",
                        "claimed_by_cleared",
                        "claimed_at_cleared",
                        "lease_expires_at_cleared",
                    )
                )
                or SHA256_PATTERN.fullmatch(
                    str(retry_event.get("dispatch_request_sha256") or "")
                )
                is None
                or retry_event_subject
                != {"type": "knowledge_build", "id": retry_run_id}
                or retry_event_payload.get("dispatch_idempotency_key")
                != retry_event.get("dispatch_idempotency_key")
                or retry_event_otel_trace_id != retry_otel_trace_id
                or retry_attempt.get("attempt_id") != expected_retry_attempt_id
                or retry_attempt.get("event_id") != retry_event_id
                or retry_attempt.get("tenant_id") != SCOPE[0]
                or retry_attempt.get("project_id") != SCOPE[1]
                or retry_attempt.get("attempt_number") != 1
                or retry_attempt.get("lease_generation") != 1
                or retry_attempt.get("status") != "succeeded"
                or retry_attempt.get("delivery_mode") != "dispatch"
                or retry_attempt.get("adapter") != "qdrant"
                or retry_attempt.get("operation") != "upsert_payload"
                or retry_attempt.get("error_code") is not None
                or retry_attempt.get("error_message") is not None
                or retry_attempt.get("started_at") is None
                or retry_attempt.get("completed_at") is None
                or not isinstance(retry_attempt.get("claimed_by"), str)
                or not retry_attempt.get("claimed_by")
                or SHA256_PATTERN.fullmatch(
                    str(retry_attempt.get("claim_token_sha256") or "")
                )
                is None
                or retry_attempt.get("dispatch_idempotency_key")
                != retry_event.get("dispatch_idempotency_key")
                or retry_attempt.get("request_sha256")
                != retry_event.get("dispatch_request_sha256")
                or retry_payload.get("retry_of_run_id") != source_run_id
                or retry_payload.get("retry_of_event_id") != source_event_id
                or retry_payload.get("retry_of_trace_id") != source_trace_id
                or retry_event_payload.get("retry_of_run_id") != source_run_id
                or retry_event_payload.get("retry_of_event_id") != source_event_id
                or retry_event_payload.get("retry_of_trace_id") != source_trace_id
                or not retry_event_qdrant.get("business_ref")
                or not retry_event_data_qdrant.get("business_ref")
                or retry_run.get("tenant_id") != SCOPE[0]
                or retry_run.get("project_id") != SCOPE[1]
                or retry_run.get("run_type") != "knowledge_build"
                or not isinstance(point_ids, list)
                or len(point_ids) != 1
                or not isinstance(point_ids[0], str)
                or retry_attempt_point_ids != point_ids
                or canonical_sha256(retry_attempt_payload)
                != canonical_sha256(
                    _mapping(
                        retry_details.get("qdrant_payload"),
                        "dead-letter retry dispatch Qdrant payload",
                    )
                )
            ):
                raise VerifierFailure(
                    "dead-letter retry produced duplicate delivery or remote outcomes"
                )
            supplemental_facts["dead_letter_retry_qdrant"] = _qdrant_retry_observation(
                browser,
                config,
                retry_run_id=retry_run_id,
                retry_event=retry_event,
                retry_attempt=retry_attempt,
                retry_details=retry_details,
                expected_trace_id=source_trace_id,
            )
            supplemental_facts["dead_letter_retry_trace"] = _wait_tempo_operation(
                browser,
                config,
                otel_trace_id=retry_otel_trace_id,
                operation="qdrant",
                expected_business_trace_id=source_trace_id,
                expected_retry_path=f"/api/v1/runs/{source_run_id}/retries",
            )
            lineage_count = config.database.one(
                "SELECT COUNT(*) AS count FROM run_records "
                "WHERE tenant_id=:tenant_id AND project_id=:project_id "
                "AND JSON_UNQUOTE(JSON_EXTRACT(payload, '$.retry_of_run_id'))="
                ":source_run_id",
                {
                    "tenant_id": SCOPE[0],
                    "project_id": SCOPE[1],
                    "source_run_id": source_run_id,
                },
            )
            retry_event_count = config.database.one(
                "SELECT COUNT(*) AS count FROM outbox_events "
                "WHERE tenant_id=:tenant_id AND project_id=:project_id "
                "AND aggregate_id=:retry_run_id "
                "AND event_type='knowledge_index.build_requested'",
                {
                    "tenant_id": SCOPE[0],
                    "project_id": SCOPE[1],
                    "retry_run_id": retry_run_id,
                },
            )
            idempotency_rows = config.database.rows(
                "SELECT user_id, request_hash, status_code, response_json, state "
                "FROM idempotency_records WHERE tenant_id=:tenant_id "
                "AND project_id=:project_id AND operation=:operation "
                "AND idempotency_key=:idempotency_key",
                {
                    "tenant_id": SCOPE[0],
                    "project_id": SCOPE[1],
                    "operation": f"retry:knowledge_build:{source_run_id}",
                    "idempotency_key": retry_key,
                },
            )
            audit_rows = config.database.rows(
                "SELECT actor_id, object_type, result, trace_id, idempotency_key, "
                "before_json, after_json FROM audit_logs "
                "WHERE tenant_id=:tenant_id AND project_id=:project_id "
                "AND object_id=:retry_run_id AND action='knowledge_build.create' "
                "ORDER BY audit_id",
                {
                    "tenant_id": SCOPE[0],
                    "project_id": SCOPE[1],
                    "retry_run_id": retry_run_id,
                },
            )
            if len(idempotency_rows) != 1 or len(audit_rows) != 1:
                raise VerifierFailure(
                    "dead-letter retry persistence evidence is incomplete"
                )
            idempotency = idempotency_rows[0]
            idempotency_response = _json_mapping(
                idempotency.get("response_json"), "retry idempotency response"
            )
            idempotency_data = _mapping(
                idempotency_response.get("data"), "retry idempotency response data"
            )
            audit = audit_rows[0]
            audit_after = _json_mapping(audit.get("after_json"), "retry audit after")
            audit_lineage_matches = (
                audit.get("object_type") == "knowledge_build"
                and audit.get("result") == "success"
                and audit.get("before_json") is None
                and audit_after.get("run_id") == retry_run_id
                and audit_after.get("status") == "pending"
                and audit_after.get("trace_id") == source_trace_id
                and audit_after.get("retry_of_run_id") == source_run_id
                and audit_after.get("retry_of_event_id") == source_event_id
                and audit_after.get("retry_of_trace_id") == source_trace_id
                and audit_after.get("retry_reason") == retry_reason
                and audit_after.get("retry_attempt") == 1
                and audit_after.get("trigger_type") == "retry"
            )
            idempotency_lineage_matches = (
                idempotency_data.get("run_id") == retry_run_id
                and idempotency_data.get("status") == "pending"
                and idempotency_data.get("status_version") == 1
                and idempotency_data.get("trace_id") == source_trace_id
                and idempotency_data.get("retry_of_run_id") == source_run_id
                and idempotency_data.get("retry_of_event_id") == source_event_id
                and idempotency_data.get("retry_of_trace_id") == source_trace_id
            )
            source_run_after = _run_row(config, source_run_id)
            source_event_after = _outbox_row(config, source_run_id)
            source_attempts_after = _attempt_rows(config, source_event_id)
            source_attempt_ledger_after = _attempt_ledger_sha256(source_attempts_after)
            source_snapshot_after = _dead_letter_source_snapshot(
                source_run_after, source_event_after, source_attempts_after
            )
            facts = {
                "source_run_id_sha256": _sha256_text(source_run_id),
                "retry_run_id_sha256": _sha256_text(retry_run_id),
                "source_event_id": source_event_id,
                "retry_event_id": retry_event_id,
                "source_event_aggregate_id_sha256": _sha256_text(
                    _nonempty_text(
                        source_event_after.get("aggregate_id"),
                        "source event aggregate id",
                    )
                ),
                "retry_event_aggregate_id_sha256": _sha256_text(
                    _nonempty_text(
                        retry_event.get("aggregate_id"), "retry event aggregate id"
                    )
                ),
                "source_payload_dead_letter_event_id": source_payload.get(
                    "dead_letter_event_id"
                ),
                "retry_payload_retry_of_event_id": retry_payload.get(
                    "retry_of_event_id"
                ),
                "retry_payload_retry_of_run_id_sha256": _sha256_text(
                    _nonempty_text(
                        retry_payload.get("retry_of_run_id"), "retry source run id"
                    )
                ),
                "source_trace_id": source_trace_id,
                "retry_payload_retry_of_trace_id": retry_payload.get(
                    "retry_of_trace_id"
                ),
                "source_status_before": source_run.get("status"),
                "source_status_after": source_run_after.get("status"),
                "source_terminal_reason": source_run_after.get("terminal_reason"),
                "source_status_version": source_run_after.get("status_version"),
                "source_event_status": source_event_after.get("status"),
                "source_delivery_state": source_event_after.get("delivery_state"),
                "source_error_code": source_attempts[0].get("error_code"),
                "source_last_error_sha256": _sha256_text(
                    _nonempty_text(
                        source_event_after.get("last_error"), "source last error"
                    )
                ),
                "source_lease_generation": source_event_after.get("lease_generation"),
                "source_dead_letter_attempt_count": len(source_attempts),
                "source_snapshot_sha256_before": source_snapshot_before,
                "source_snapshot_sha256_after": source_snapshot_after,
                "source_attempt_ledger_sha256_before": source_attempt_ledger_before,
                "source_attempt_ledger_sha256_after": source_attempt_ledger_after,
                "retry_response_replayed": canonical_sha256(retry_response)
                == canonical_sha256(replay_response),
                "first_response_sha256": canonical_sha256(retry_response),
                "replay_response_sha256": canonical_sha256(replay_response),
                "stored_response_sha256": canonical_sha256(idempotency_response),
                "idempotency_record_count": len(idempotency_rows),
                "idempotency_state": idempotency.get("state"),
                "idempotency_status_code": idempotency.get("status_code"),
                "idempotency_request_sha256": idempotency.get("request_hash"),
                "expected_idempotency_request_sha256": _expected_request_sha256(
                    "POST", f"/api/v1/runs/{source_run_id}/retries", retry_body
                ),
                "idempotency_response_run_id_sha256": _sha256_text(
                    _nonempty_text(
                        idempotency_data.get("run_id"),
                        "idempotency response retry run id",
                    )
                ),
                "idempotency_user_sha256": _sha256_text(
                    _nonempty_text(idempotency.get("user_id"), "idempotency user")
                ),
                "expected_retry_idempotency_key_sha256": _sha256_text(retry_key),
                "retry_run_count": int(lineage_count.get("count") or 0),
                "retry_event_count": int(retry_event_count.get("count") or 0),
                "retry_dispatch_attempt_count": len(retry_attempts),
                "retry_event_otel_trace_id": retry_event_otel_trace_id,
                "retry_dispatch_idempotency_key_sha256": _sha256_text(
                    _nonempty_text(
                        retry_event.get("dispatch_idempotency_key"),
                        "retry dispatch idempotency key",
                    )
                ),
                "retry_dispatch_request_sha256": retry_event.get(
                    "dispatch_request_sha256"
                ),
                "retry_attempt_request_sha256": retry_attempt.get("request_sha256"),
                "retry_attempt_id_sha256": _sha256_text(
                    _nonempty_text(retry_attempt.get("attempt_id"), "retry attempt id")
                ),
                "retry_expected_attempt_id_sha256": _sha256_text(
                    expected_retry_attempt_id
                ),
                "retry_point_id_sha256": _sha256_text(
                    _nonempty_text(point_ids[0], "retry point id")
                ),
                "retry_dispatch_payload_sha256": canonical_sha256(
                    _mapping(
                        retry_details.get("qdrant_payload"),
                        "retry dispatch Qdrant payload",
                    )
                ),
                "retry_attempt_payload_sha256": canonical_sha256(retry_attempt_payload),
                "retry_event_status": retry_event.get("status"),
                "retry_run_status": retry_final.get("status"),
                "retry_trace_inherited": retry_run.get("trace_id") == source_trace_id,
                "retry_audit_count": len(audit_rows),
                "retry_audit_actor_sha256": _sha256_text(
                    _nonempty_text(audit.get("actor_id"), "retry audit actor")
                ),
                "retry_audit_idempotency_key_sha256": _sha256_text(
                    _nonempty_text(
                        audit.get("idempotency_key"), "retry audit idempotency key"
                    )
                ),
                "retry_audit_trace_matches": audit.get("trace_id") == source_trace_id,
                "retry_audit_lineage_matches": bool(
                    audit_lineage_matches
                    and idempotency_lineage_matches
                    and audit.get("actor_id") == idempotency.get("user_id")
                    and audit.get("idempotency_key") == retry_key
                ),
            }
        elif dependency in {"qdrant_outage", "redis_outage"}:
            if not _phase_completed(state, "fault-during", dependency):
                raise VerifierFailure(
                    "dependency outage lacks a during-fault observation"
                )
            target_dependency = dependency.removesuffix("_outage")
            readiness_during = _mapping(
                fault.get("readiness_during"), "during-fault readiness observation"
            )
            readiness_after = _ready_observation(
                browser,
                config,
                target_dependency=target_dependency,
                expect_ready=True,
            )
            facts = {
                "ready_status_during": readiness_during.get("http_status"),
                "ready_status_after": readiness_after.get("http_status"),
                "failed_dependency_during": readiness_during.get("target_dependency"),
                "failed_dependency_status_during": readiness_during.get(
                    "target_status"
                ),
                "missing_required_during": readiness_during.get("missing_required"),
                "recovered_dependency_status_after": readiness_after.get(
                    "target_status"
                ),
                "missing_required_after": readiness_after.get("missing_required"),
            }
            if dependency == "qdrant_outage":
                point = _qdrant_point_observation(
                    browser,
                    config,
                    collection=_nonempty_text(
                        fault.get("collection"), "Qdrant collection"
                    ),
                    point_id=_nonempty_text(fault.get("point_id"), "Qdrant point id"),
                    expected_trace_id=_nonempty_text(
                        _mapping(
                            state["operations"].get("lineage_trace_ids"),
                            "lineage trace state",
                        ).get("qdrant_point"),
                        "Qdrant point trace",
                    ),
                )
                facts["point_id"] = point["point_id"]
                facts["point_present_after"] = True
        else:
            raise VerifierFailure("fault verification dependency is unsupported")

        # Every recovery case must carry explicit authority observations. There
        # is intentionally no fallback for a missing after value.
        facts["authoritative_run_count_before"] = before_count
        facts["authoritative_run_count_after"] = _authority_run_count(config, state)
        recovery_facts = dict(facts)
        for proof_id in recovery_proof_ids(dependency):
            if proof_id != dependency:
                proof_observations = supplemental_facts.get(proof_id, {})
                if any(
                    key in recovery_facts and recovery_facts[key] != value
                    for key, value in proof_observations.items()
                ):
                    raise VerifierFailure("recovery proof identity bindings conflict")
                recovery_facts.update(proof_observations)
        recovery_case_from_facts(dependency, recovery_facts)
        proof_observations = {
            proof_id: (
                facts if proof_id == dependency else supplemental_facts[proof_id]
            )
            for proof_id in recovery_proof_ids(dependency)
        }
        fault["verification_checkpoint"] = build_fault_verification_checkpoint(
            dependency, proof_observations
        )
        state["updated_at"] = _utc_now()
        validate_artifact_value(state)
        replace_json_state(config.state_path(), prior, state)
        _complete_fault_verification_checkpoint(config, state, dependency)
    finally:
        browser.close()


def _otlp_attribute_value(value: object) -> object:
    if not isinstance(value, dict):
        return None
    for key in (
        "stringValue",
        "string_value",
        "intValue",
        "int_value",
        "doubleValue",
        "double_value",
        "boolValue",
        "bool_value",
    ):
        if key in value:
            return value[key]
    return None


def _otlp_attributes(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, list):
        raise VerifierFailure(f"{label} attributes are invalid")
    attributes: dict[str, object] = {}
    for raw_attribute in value:
        attribute = _mapping(raw_attribute, f"{label} attribute")
        key = attribute.get("key")
        parsed = _otlp_attribute_value(attribute.get("value"))
        if not isinstance(key, str) or not key or parsed is None or key in attributes:
            raise VerifierFailure(f"{label} attribute is invalid")
        attributes[key] = parsed
    return attributes


def _otlp_span_groups(batch: Mapping[str, Any]) -> list[object]:
    for key in ("scopeSpans", "scope_spans", "instrumentationLibrarySpans"):
        value = batch.get(key)
        if isinstance(value, list):
            return value
    raise VerifierFailure("Tempo resource span groups are invalid")


def _client_span(kind: object) -> bool:
    return kind in {3, "3", "CLIENT", "SPAN_KIND_CLIENT"}


def _server_span(kind: object) -> bool:
    return kind in {2, "2", "SERVER", "SPAN_KIND_SERVER"}


def _safe_span_hosts(attributes: Mapping[str, object]) -> set[str]:
    hosts: set[str] = set()
    for key in (
        "server.address",
        "server.host",
        "net.peer.name",
        "network.peer.address",
    ):
        value = attributes.get(key)
        if isinstance(value, str) and value and value != "[REDACTED]":
            hosts.add(value.casefold().rstrip("."))
    for key in ("http.url", "url.full", "url.original"):
        value = attributes.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            hosts.add(parsed.hostname.casefold().rstrip("."))
    return hosts


def tempo_trace_facts(
    payload: object,
    otel_trace_id: str,
    operation: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{32}", otel_trace_id) or int(otel_trace_id, 16) == 0:
        raise VerifierFailure("Tempo trace id is invalid")
    if operation not in OTEL_OPERATIONS:
        raise VerifierFailure("Tempo operation is invalid")
    root = _mapping(payload, "Tempo trace payload")
    batches = root.get("batches")
    if not isinstance(batches, list) or not batches:
        raise VerifierFailure("Tempo trace has no resource spans")

    service_components = {
        "auris-flow-bff": "bff",
        "auris-flow-worker": "worker",
        "auris-flow-dagster-code": "dagster",
    }
    target_hosts = {
        "oidc": "auris-production-gate.invalid",
        "object_storage": "minio",
        "qdrant": "qdrant",
        "external_callback": "callback.production-gate.invalid",
    }
    host_operations = {host: name for name, host in target_hosts.items()}
    required_components = OPERATION_OTEL_COMPONENTS[operation]
    required_services = OPERATION_OTEL_SERVICES[operation]
    component_signals: dict[str, set[str]] = {
        component: set() for component in required_components
    }
    component_signals["otel"].add("tempo.trace")
    services_with_spans: set[str] = set()
    span_count = 0
    client_span_count = 0

    for raw_batch in batches:
        batch = _mapping(raw_batch, "Tempo resource spans")
        resource = _mapping(batch.get("resource"), "Tempo resource")
        resource_attributes = _otlp_attributes(
            resource.get("attributes"), "Tempo resource"
        )
        service_name = resource_attributes.get("service.name")
        groups = _otlp_span_groups(batch)
        batch_span_count = 0
        for raw_group in groups:
            group = _mapping(raw_group, "Tempo scope spans")
            spans = group.get("spans")
            if not isinstance(spans, list):
                raise VerifierFailure("Tempo scope spans are invalid")
            for raw_span in spans:
                span = _mapping(raw_span, "Tempo span")
                trace_id = span.get("traceId", span.get("trace_id"))
                if (
                    not isinstance(trace_id, str)
                    or trace_id.casefold() != otel_trace_id
                ):
                    raise VerifierFailure("Tempo returned a span from another trace")
                span_name = span.get("name")
                if not isinstance(span_name, str) or not span_name:
                    raise VerifierFailure("Tempo span name is invalid")
                attributes = _otlp_attributes(span.get("attributes", []), "Tempo span")
                batch_span_count += 1
                span_count += 1
                if (
                    service_name == "auris-flow-worker"
                    and span_name == "outbox.process"
                    and "outbox" in required_components
                ):
                    component_signals["outbox"].add("span.name=outbox.process")
                if not _client_span(span.get("kind")):
                    continue
                client_span_count += 1
                if service_name not in required_services:
                    continue
                db_system = attributes.get(
                    "db.system.name", attributes.get("db.system")
                )
                if isinstance(db_system, str):
                    normalized_db = db_system.casefold()
                    if normalized_db in required_components & {"mysql", "redis"}:
                        component_signals[normalized_db].add(
                            f"db.system={normalized_db}"
                        )
                for host in _safe_span_hosts(attributes):
                    target_operation = host_operations.get(host)
                    if target_operation is None:
                        continue
                    if target_operation != operation:
                        raise VerifierFailure(
                            "Tempo operation contains another operation target"
                        )
                    target_service = (
                        "auris-flow-bff" if operation == "oidc" else "auris-flow-worker"
                    )
                    if service_name == target_service:
                        component_signals[operation].add(f"client.host={host}")
        if batch_span_count:
            if (
                not isinstance(service_name, str)
                or service_name not in required_services
            ):
                raise VerifierFailure("Tempo operation contains an unexpected service")
            services_with_spans.add(service_name)
            component = service_components[service_name]
            if component in required_components:
                component_signals[component].add(f"service.name={service_name}")

    if services_with_spans != set(required_services):
        raise VerifierFailure("Tempo trace is missing a required service")
    missing_components = sorted(
        component for component, signals in component_signals.items() if not signals
    )
    if missing_components:
        raise VerifierFailure(
            "Tempo trace is missing required sanitized component signals"
        )
    return {
        "otel_trace_id": otel_trace_id,
        "services": sorted(services_with_spans),
        "components": sorted(required_components),
        "component_signals": {
            component: sorted(signals)
            for component, signals in sorted(component_signals.items())
        },
        "span_count": span_count,
        "client_span_count": client_span_count,
    }


def _tempo_span_id(span: Mapping[str, Any], field: str, label: str) -> str:
    snake_field = "span_id" if field == "spanId" else "parent_span_id"
    value = span.get(field, span.get(snake_field))
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-f]{16}", value.casefold()) is None
        or int(value, 16) == 0
    ):
        raise VerifierFailure(f"{label} span identity is invalid")
    return value.casefold()


def _safe_span_paths(attributes: Mapping[str, object]) -> set[str]:
    paths: set[str] = set()
    for key in ("http.url", "url.full", "url.original"):
        value = attributes.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            paths.add(parsed.path)
    return paths


RETRY_ROUTE_TEMPLATE = "/api/v1/runs/{id}/retries"


def _retry_server_binding(
    entry: Mapping[str, Any], expected_retry_path: str | None
) -> tuple[str, str] | None:
    if not _server_span(entry.get("kind")):
        return None
    attributes = _mapping(entry.get("attributes"), "Tempo retry server attributes")
    raw_method = attributes.get("http.request.method", attributes.get("http.method"))
    name = entry.get("name")
    if raw_method is None and isinstance(name, str) and " " in name:
        raw_method = name.split(" ", 1)[0]
    method = str(raw_method or "").upper()
    if method != "POST":
        return None

    route = attributes.get("http.route")
    if route == RETRY_ROUTE_TEMPLATE:
        return method, RETRY_ROUTE_TEMPLATE
    if route is not None:
        return None
    # Some pinned OpenTelemetry/FastAPI combinations expose only the sanitized
    # span name or concrete URL path. Accept only the exact expected path; never
    # use a broad suffix/regex that could bind another retry endpoint.
    controlled_paths: set[str] = set()
    for key in ("url.path", "http.target"):
        value = attributes.get(key)
        if isinstance(value, str):
            controlled_paths.add(value.split("?", 1)[0])
    controlled_paths.update(_safe_span_paths(attributes))
    if isinstance(name, str) and name.startswith("POST "):
        controlled_paths.add(name.removeprefix("POST ").split("?", 1)[0])
    if RETRY_ROUTE_TEMPLATE in controlled_paths:
        return method, RETRY_ROUTE_TEMPLATE
    if expected_retry_path is not None and expected_retry_path in controlled_paths:
        return method, RETRY_ROUTE_TEMPLATE
    return None


def _tempo_retry_lineage_facts(
    payload: object,
    expected_trace_id: str,
    expected_retry_path: str | None = None,
) -> dict[str, Any]:
    if expected_retry_path is not None and (
        re.fullmatch(r"/api/v1/runs/[A-Za-z0-9_-]{1,160}/retries", expected_retry_path)
        is None
    ):
        raise VerifierFailure("Tempo retry expected path is invalid")
    root = _mapping(payload, "Tempo trace payload")
    batches = root.get("batches")
    if not isinstance(batches, list):
        raise VerifierFailure("Tempo trace has no resource spans")
    entries: list[dict[str, Any]] = []
    for raw_batch in batches:
        batch = _mapping(raw_batch, "Tempo resource spans")
        resource = _mapping(batch.get("resource"), "Tempo resource")
        resource_attributes = _otlp_attributes(
            resource.get("attributes"), "Tempo resource"
        )
        service_name = resource_attributes.get("service.name")
        if not isinstance(service_name, str) or not service_name:
            raise VerifierFailure("Tempo retry span service is invalid")
        for raw_group in _otlp_span_groups(batch):
            group = _mapping(raw_group, "Tempo scope spans")
            spans = group.get("spans")
            if not isinstance(spans, list):
                raise VerifierFailure("Tempo scope spans are invalid")
            for raw_span in spans:
                span = _mapping(raw_span, "Tempo span")
                attributes = _otlp_attributes(span.get("attributes", []), "Tempo span")
                span_id = _tempo_span_id(span, "spanId", "Tempo retry")
                parent_value = span.get("parentSpanId", span.get("parent_span_id"))
                parent_span_id = (
                    _tempo_span_id(span, "parentSpanId", "Tempo retry parent")
                    if parent_value not in {None, ""}
                    else None
                )
                entries.append(
                    {
                        "service": service_name,
                        "name": span.get("name"),
                        "kind": span.get("kind"),
                        "span_id": span_id,
                        "parent_span_id": parent_span_id,
                        "attributes": attributes,
                    }
                )

    outbox_spans = [
        entry
        for entry in entries
        if entry["service"] == "auris-flow-worker"
        and entry["name"] == "outbox.process"
        and entry["attributes"].get("auris.business_trace_id") == expected_trace_id
    ]
    adapter_spans = [
        entry
        for entry in entries
        if entry["service"] == "auris-flow-worker"
        and entry["name"] == "outbox.adapter.dispatch"
        and entry["attributes"].get("auris.business_trace_id") == expected_trace_id
    ]
    qdrant_spans = [
        entry
        for entry in entries
        if entry["service"] == "auris-flow-worker"
        and _client_span(entry["kind"])
        and "qdrant" in _safe_span_hosts(entry["attributes"])
    ]
    qdrant_write_spans = [
        entry
        for entry in qdrant_spans
        if str(
            entry["attributes"].get(
                "http.request.method", entry["attributes"].get("http.method", "")
            )
        ).upper()
        == "PUT"
        and any(
            path.endswith("/points") for path in _safe_span_paths(entry["attributes"])
        )
    ]
    if len(outbox_spans) != 1 or len(adapter_spans) != 1 or not qdrant_spans:
        raise VerifierFailure("Tempo retry trace cardinality is invalid")
    if len(qdrant_write_spans) != 1:
        raise VerifierFailure("Tempo retry trace lacks one Qdrant point write")
    outbox = outbox_spans[0]
    adapter = adapter_spans[0]
    bff_retry_spans = [
        entry
        for entry in entries
        if entry["service"] == "auris-flow-bff"
        and _retry_server_binding(entry, expected_retry_path) is not None
    ]
    if (
        len(bff_retry_spans) != 1
        or bff_retry_spans[0]["span_id"] != outbox["parent_span_id"]
        or adapter["parent_span_id"] != outbox["span_id"]
        or any(entry["parent_span_id"] != adapter["span_id"] for entry in qdrant_spans)
    ):
        raise VerifierFailure("Tempo retry trace parent chain is invalid")
    bff = bff_retry_spans[0]
    server_method, server_route = _retry_server_binding(bff, expected_retry_path) or (
        "",
        "",
    )
    return {
        "observed_business_trace_id": expected_trace_id,
        "bff_span_id_sha256": _sha256_text(bff["span_id"]),
        "outbox_parent_span_id_sha256": _sha256_text(outbox["parent_span_id"]),
        "outbox_span_id_sha256": _sha256_text(outbox["span_id"]),
        "adapter_parent_span_id_sha256": _sha256_text(adapter["parent_span_id"]),
        "adapter_span_id_sha256": _sha256_text(adapter["span_id"]),
        "qdrant_parent_span_id_sha256": _sha256_text(
            qdrant_write_spans[0]["parent_span_id"]
        ),
        "bff_server_span_count": len(bff_retry_spans),
        "bff_server_http_method": server_method,
        "bff_server_route": server_route,
        "outbox_process_span_count": len(outbox_spans),
        "adapter_dispatch_span_count": len(adapter_spans),
        "qdrant_client_span_count": len(qdrant_spans),
        "qdrant_write_span_count": len(qdrant_write_spans),
    }


def _wait_tempo_operation(
    browser: BrowserClient,
    config: GateConfig,
    *,
    otel_trace_id: str,
    operation: str,
    expected_business_trace_id: str,
    expected_retry_path: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + config.timeout_seconds
    while time.monotonic() < deadline:
        response = browser.raw(
            "GET",
            f"{config.tempo_url}/api/traces/{otel_trace_id}",
            expected={200, 404},
            label="Tempo dead-letter retry trace query",
        )
        if response.status_code == 200:
            payload = _safe_json_response(
                response, "Tempo dead-letter retry trace query"
            )
            try:
                facts = tempo_trace_facts(payload, otel_trace_id, operation)
                lineage_facts = _tempo_retry_lineage_facts(
                    payload,
                    expected_business_trace_id,
                    expected_retry_path,
                )
            except VerifierFailure:
                pass
            else:
                return {"http_status": 200, **lineage_facts, **facts}
        time.sleep(0.5)
    raise VerifierFailure("Tempo did not prove the dead-letter retry operation")


def _tempo_observation(
    browser: BrowserClient,
    config: GateConfig,
    operation_otel_trace_ids: Mapping[str, str],
) -> dict[str, Any]:
    if set(operation_otel_trace_ids) != set(OTEL_OPERATIONS):
        raise VerifierFailure("operation OTel trace inventory is invalid")
    deadline = time.monotonic() + config.timeout_seconds
    pending = set(OTEL_OPERATIONS)
    operations: dict[str, dict[str, Any]] = {}
    while pending and time.monotonic() < deadline:
        for operation in OTEL_OPERATIONS:
            if operation not in pending:
                continue
            otel_trace_id = operation_otel_trace_ids[operation]
            response = browser.raw(
                "GET",
                f"{config.tempo_url}/api/traces/{otel_trace_id}",
                expected={200, 404},
                label="Tempo operation trace query",
            )
            if response.status_code != 200:
                continue
            payload = _safe_json_response(response, "Tempo operation trace query")
            try:
                operations[operation] = tempo_trace_facts(
                    payload,
                    otel_trace_id,
                    operation,
                )
            except VerifierFailure:
                continue
            pending.remove(operation)
        if pending:
            time.sleep(0.5)
    if pending:
        raise VerifierFailure("Tempo did not prove every independent operation trace")

    services = sorted(
        {service for facts in operations.values() for service in facts["services"]}
    )
    components = sorted(
        {
            component
            for facts in operations.values()
            for component in facts["components"]
        }
    )
    return {
        "http_status": 200,
        "otel_trace_id": operation_otel_trace_ids["dagster"],
        "operation_otel_trace_ids": dict(operation_otel_trace_ids),
        "operations": operations,
        "services": services,
        "components": components,
    }


def _all_faults_verified(state: dict[str, Any]) -> None:
    missing = [
        dependency
        for dependency in DEPENDENCIES
        if not _phase_completed(state, "fault-verify", dependency)
    ]
    if missing:
        raise VerifierFailure("finalize requires every recovery case")


def recovery_proof_ids(dependency: str) -> list[str]:
    if dependency not in DEPENDENCIES:
        raise VerifierFailure("recovery dependency is invalid")
    if dependency == "dead_letter_retry":
        return [
            "dead_letter_retry",
            "dead_letter_retry_qdrant",
            "dead_letter_retry_trace",
        ]
    return [dependency]


def recovery_case_from_facts(
    dependency: str, facts: Mapping[str, Any]
) -> dict[str, Any]:
    if dependency not in DEPENDENCIES:
        raise VerifierFailure("recovery dependency is invalid")
    before_count = _positive_int(
        facts.get("authoritative_run_count_before"),
        "authoritative run count before",
    )
    after_count = _positive_int(
        facts.get("authoritative_run_count_after"),
        "authoritative run count after",
    )
    authority_consistent = before_count == after_count
    proven = False
    no_duplicate_business_outcome = False

    if dependency == "mysql_restart":
        started_before = facts.get("started_at_before")
        started_after = facts.get("started_at_after")
        proven = (
            isinstance(facts.get("container_id_sha256"), str)
            and SHA256_PATTERN.fullmatch(str(facts["container_id_sha256"])) is not None
            and isinstance(started_before, str)
            and bool(started_before)
            and isinstance(started_after, str)
            and bool(started_after)
            and started_before != started_after
            and facts.get("ready_status_after") == 200
        )
        no_duplicate_business_outcome = authority_consistent
    elif dependency == "worker_crash":
        started_before = facts.get("started_at_before")
        started_after = facts.get("started_at_after")
        proven = (
            isinstance(facts.get("container_id_sha256"), str)
            and SHA256_PATTERN.fullmatch(str(facts["container_id_sha256"])) is not None
            and isinstance(started_before, str)
            and bool(started_before)
            and isinstance(started_after, str)
            and bool(started_after)
            and started_before != started_after
            and _positive_int(facts.get("event_id"), "worker crash event id") > 0
            and facts.get("event_status_before") == "pending"
            and facts.get("event_status_after") == "processed"
            and facts.get("remote_run_count") == 1
        )
        no_duplicate_business_outcome = (
            authority_consistent and facts.get("remote_run_count") == 1
        )
    elif dependency == "duplicate_delivery":
        token_sha256_before = facts.get("claim_token_sha256_before")
        token_sha256_after = facts.get("claim_token_sha256_after")
        lease_generation_before = _positive_int(
            facts.get("lease_generation_before"), "lease generation before"
        )
        lease_generation_after = _positive_int(
            facts.get("lease_generation_after"), "lease generation after"
        )
        proven = (
            _positive_int(facts.get("event_id"), "duplicate delivery event id") > 0
            and _positive_int(
                facts.get("delivery_attempt_count"),
                "duplicate delivery attempt count",
                minimum=2,
            )
            >= 2
            and facts.get("dispatch_attempt_count") == 1
            and facts.get("reconcile_attempt_count") == 1
            and facts.get("remote_receipt_count") == 1
            and facts.get("business_outcome_count") == 1
            and facts.get("stale_owner_rejected") is True
            and facts.get("new_owner_accepted") is True
            and lease_generation_after == lease_generation_before + 1
            and isinstance(token_sha256_before, str)
            and SHA256_PATTERN.fullmatch(token_sha256_before) is not None
            and isinstance(token_sha256_after, str)
            and SHA256_PATTERN.fullmatch(token_sha256_after) is not None
            and token_sha256_before != token_sha256_after
        )
        no_duplicate_business_outcome = (
            authority_consistent
            and facts.get("remote_receipt_count") == 1
            and facts.get("business_outcome_count") == 1
            and facts.get("stale_owner_rejected") is True
        )
    elif dependency == "callback_timeout":
        proven = (
            _positive_int(facts.get("event_id"), "callback timeout event id") > 0
            and facts.get("first_attempt_status") == "outcome_unknown"
            and facts.get("final_attempt_status") == "success"
            and facts.get("final_delivery_mode") == "reconcile"
            and facts.get("remote_receipt_count") == 1
        )
        no_duplicate_business_outcome = (
            authority_consistent and facts.get("remote_receipt_count") == 1
        )
    elif dependency == "dead_letter_retry":
        source_hash = facts.get("source_run_id_sha256")
        retry_hash = facts.get("retry_run_id_sha256")
        snapshot_before = facts.get("source_snapshot_sha256_before")
        snapshot_after = facts.get("source_snapshot_sha256_after")
        ledger_before = facts.get("source_attempt_ledger_sha256_before")
        ledger_after = facts.get("source_attempt_ledger_sha256_after")
        source_event_id = _positive_int(
            facts.get("source_event_id"), "dead-letter source event id"
        )
        retry_event_id = _positive_int(
            facts.get("retry_event_id"), "dead-letter retry event id"
        )
        source_trace_id = facts.get("source_trace_id")
        required_hashes = (
            source_hash,
            retry_hash,
            facts.get("source_event_aggregate_id_sha256"),
            facts.get("retry_event_aggregate_id_sha256"),
            facts.get("retry_payload_retry_of_run_id_sha256"),
            facts.get("source_last_error_sha256"),
            snapshot_before,
            snapshot_after,
            ledger_before,
            ledger_after,
            facts.get("idempotency_request_sha256"),
            facts.get("expected_idempotency_request_sha256"),
            facts.get("idempotency_response_run_id_sha256"),
            facts.get("idempotency_user_sha256"),
            facts.get("expected_retry_idempotency_key_sha256"),
            facts.get("retry_audit_actor_sha256"),
            facts.get("retry_audit_idempotency_key_sha256"),
            facts.get("first_response_sha256"),
            facts.get("replay_response_sha256"),
            facts.get("stored_response_sha256"),
            facts.get("retry_dispatch_idempotency_key_sha256"),
            facts.get("retry_dispatch_request_sha256"),
            facts.get("retry_attempt_request_sha256"),
            facts.get("retry_attempt_id_sha256"),
            facts.get("retry_expected_attempt_id_sha256"),
            facts.get("retry_point_id_sha256"),
            facts.get("retry_dispatch_payload_sha256"),
            facts.get("retry_attempt_payload_sha256"),
            facts.get("point_id_sha256"),
            facts.get("dispatch_point_id_sha256"),
            facts.get("attempt_point_id_sha256"),
            facts.get("payload_sha256"),
            facts.get("dispatch_payload_sha256"),
            facts.get("attempt_payload_sha256"),
            facts.get("dispatch_idempotency_key_sha256"),
            facts.get("dispatch_request_sha256"),
            facts.get("attempt_request_sha256"),
            facts.get("attempt_id_sha256"),
            facts.get("bff_span_id_sha256"),
            facts.get("outbox_parent_span_id_sha256"),
            facts.get("outbox_span_id_sha256"),
            facts.get("adapter_parent_span_id_sha256"),
            facts.get("adapter_span_id_sha256"),
            facts.get("qdrant_parent_span_id_sha256"),
        )
        proven = (
            all(
                isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None
                for value in required_hashes
            )
            and source_hash != retry_hash
            and source_event_id != retry_event_id
            and facts.get("source_event_aggregate_id_sha256") == source_hash
            and facts.get("retry_event_aggregate_id_sha256") == retry_hash
            and facts.get("source_payload_dead_letter_event_id") == source_event_id
            and facts.get("retry_payload_retry_of_event_id") == source_event_id
            and facts.get("retry_payload_retry_of_run_id_sha256") == source_hash
            and isinstance(source_trace_id, str)
            and BUSINESS_TRACE_PATTERN.fullmatch(source_trace_id) is not None
            and facts.get("retry_payload_retry_of_trace_id") == source_trace_id
            and facts.get("source_status_before") == "failed"
            and facts.get("source_status_after") == "failed"
            and facts.get("source_terminal_reason") == "outbox_dispatch_dead_letter"
            and _positive_int(
                facts.get("source_status_version"), "dead-letter source status version"
            )
            >= 3
            and facts.get("source_event_status") == "dead_letter"
            and facts.get("source_delivery_state") == "failed"
            and facts.get("source_error_code") == "QDRANT_PAYLOAD_INVALID"
            and facts.get("source_lease_generation") == 1
            and facts.get("source_dead_letter_attempt_count") == 1
            and snapshot_after == snapshot_before
            and ledger_after == ledger_before
            and facts.get("retry_response_replayed") is True
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
            and facts.get("retry_dispatch_attempt_count") == 1
            and facts.get("retry_event_status") == "processed"
            and facts.get("retry_run_status") == "success"
            and facts.get("retry_trace_inherited") is True
            and facts.get("retry_audit_count") == 1
            and facts.get("retry_audit_trace_matches") is True
            and facts.get("retry_audit_lineage_matches") is True
            and facts.get("retry_dispatch_request_sha256")
            == facts.get("retry_attempt_request_sha256")
            and facts.get("retry_attempt_id_sha256")
            == facts.get("retry_expected_attempt_id_sha256")
            and facts.get("retry_dispatch_payload_sha256")
            == facts.get("retry_attempt_payload_sha256")
            and facts.get("http_status") == 200
            and isinstance(facts.get("collection"), str)
            and bool(facts.get("collection"))
            and facts.get("tenant_id") == SCOPE[0]
            and facts.get("project_id") == SCOPE[1]
            and facts.get("trace_id") == source_trace_id
            and facts.get("filtered_point_count") == 1
            and facts.get("point_occurrences") == 1
            and facts.get("cross_tenant_count") == 0
            and facts.get("cross_project_count") == 0
            and facts.get("scope_match") is True
            and facts.get("dispatch_receipt_match") is True
            and facts.get("attempt_receipt_match") is True
            and facts.get("payload_hash_match") is True
            and facts.get("dispatch_request_sha256")
            == facts.get("attempt_request_sha256")
            and facts.get("point_id_sha256")
            == facts.get("dispatch_point_id_sha256")
            == facts.get("attempt_point_id_sha256")
            == facts.get("retry_point_id_sha256")
            and facts.get("payload_sha256")
            == facts.get("dispatch_payload_sha256")
            == facts.get("attempt_payload_sha256")
            == facts.get("retry_dispatch_payload_sha256")
            and facts.get("dispatch_idempotency_key_sha256")
            == facts.get("retry_dispatch_idempotency_key_sha256")
            and facts.get("dispatch_request_sha256")
            == facts.get("retry_dispatch_request_sha256")
            and facts.get("attempt_request_sha256")
            == facts.get("retry_attempt_request_sha256")
            and facts.get("attempt_id_sha256") == facts.get("retry_attempt_id_sha256")
            and facts.get("observed_business_trace_id") == source_trace_id
            and re.fullmatch(r"[0-9a-f]{32}", str(facts.get("otel_trace_id") or ""))
            is not None
            and facts.get("otel_trace_id") == facts.get("retry_event_otel_trace_id")
            and facts.get("bff_span_id_sha256")
            == facts.get("outbox_parent_span_id_sha256")
            and facts.get("outbox_span_id_sha256")
            == facts.get("adapter_parent_span_id_sha256")
            and facts.get("adapter_span_id_sha256")
            == facts.get("qdrant_parent_span_id_sha256")
            and facts.get("bff_server_span_count") == 1
            and facts.get("bff_server_http_method") == "POST"
            and facts.get("bff_server_route") == RETRY_ROUTE_TEMPLATE
            and facts.get("outbox_process_span_count") == 1
            and facts.get("adapter_dispatch_span_count") == 1
            and _positive_int(
                facts.get("qdrant_client_span_count"), "retry Qdrant client span count"
            )
            >= 1
            and facts.get("qdrant_write_span_count") == 1
            and facts.get("services") == sorted(OPERATION_OTEL_SERVICES["qdrant"])
            and facts.get("components") == sorted(OPERATION_OTEL_COMPONENTS["qdrant"])
            and _positive_int(facts.get("span_count"), "retry trace span count") >= 1
            and _positive_int(
                facts.get("client_span_count"), "retry trace client span count"
            )
            >= 1
        )
        no_duplicate_business_outcome = bool(
            authority_consistent
            and facts.get("retry_run_count") == 1
            and facts.get("retry_event_count") == 1
            and facts.get("retry_dispatch_attempt_count") == 1
            and facts.get("filtered_point_count") == 1
            and facts.get("point_occurrences") == 1
            and facts.get("cross_tenant_count") == 0
            and facts.get("cross_project_count") == 0
            and snapshot_after == snapshot_before
            and ledger_after == ledger_before
        )
    else:
        target_dependency = dependency.removesuffix("_outage")
        proven = (
            facts.get("ready_status_during") == 503
            and facts.get("ready_status_after") == 200
            and facts.get("failed_dependency_during") == target_dependency
            and facts.get("failed_dependency_status_during") == "not_ready"
            and facts.get("missing_required_during") == [target_dependency]
            and facts.get("recovered_dependency_status_after") == "ok"
            and facts.get("missing_required_after") == []
        )
        if dependency == "qdrant_outage":
            proven = (
                proven
                and isinstance(facts.get("point_id"), str)
                and bool(facts.get("point_id"))
                and facts.get("point_present_after") is True
            )
        no_duplicate_business_outcome = authority_consistent

    if not (proven and authority_consistent and no_duplicate_business_outcome):
        raise VerifierFailure(f"{dependency} recovery facts are not proven")
    return {
        "proven": proven,
        "authority_consistent": authority_consistent,
        "no_duplicate_business_outcome": no_duplicate_business_outcome,
        "raw_proof_ids": recovery_proof_ids(dependency),
    }


def linked_components_from_facts(
    proof_facts: Mapping[str, Any],
    *,
    trace_ids: Mapping[str, Any],
    operation_otel_trace_ids: Mapping[str, Any],
    primary_otel_trace_id: str,
) -> list[str]:
    expected_operations = set(OTEL_OPERATIONS)
    if set(trace_ids) != expected_operations:
        raise VerifierFailure("operation trace inventory is invalid")
    if (
        set(operation_otel_trace_ids) != expected_operations
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{32}", value)
            or int(value, 16) == 0
            for value in operation_otel_trace_ids.values()
        )
        or len(set(operation_otel_trace_ids.values())) != len(OTEL_OPERATIONS)
        or operation_otel_trace_ids.get("dagster") != primary_otel_trace_id
    ):
        raise VerifierFailure("operation OTel trace inventory is invalid")

    def facts_for(proof_id: str) -> dict[str, Any]:
        return _mapping(proof_facts.get(proof_id), f"{proof_id} facts")

    lineage_requirements = {
        "oidc": (
            ("oidc_code_exchange", "trace_id"),
            ("browser_session", "trace_id"),
        ),
        "dagster": (
            ("dagster_graphql", "trace_id"),
            ("dagster_completion", "run_trace_id"),
        ),
        "object_storage": (("minio_object", "trace_id"),),
        "qdrant": (
            ("qdrant_point", "trace_id"),
            ("qdrant_recall", "trace_id"),
        ),
        "external_callback": (("callback_delivery", "trace_id"),),
    }
    components: set[str] = set()
    for component, requirements in lineage_requirements.items():
        expected_trace_id = trace_ids.get(component)
        if not isinstance(
            expected_trace_id, str
        ) or not BUSINESS_TRACE_PATTERN.fullmatch(expected_trace_id):
            raise VerifierFailure(f"{component} operation trace is invalid")
        if any(
            facts_for(proof_id).get(field) != expected_trace_id
            for proof_id, field in requirements
        ):
            raise VerifierFailure(f"{component} proof trace binding is invalid")
        components.add(component)

    qdrant_point = facts_for("qdrant_point")
    qdrant_recall = facts_for("qdrant_recall")
    written_point_id = qdrant_point.get("point_id")
    recalled_point_ids = qdrant_recall.get("point_ids")
    if (
        not isinstance(written_point_id, str)
        or not written_point_id
        or not isinstance(recalled_point_ids, list)
        or not recalled_point_ids
        or any(not isinstance(item, str) or not item for item in recalled_point_ids)
        or len(set(recalled_point_ids)) != len(recalled_point_ids)
        or qdrant_recall.get("authorized_hit_count") != len(recalled_point_ids)
        or qdrant_recall.get("written_point_id") != written_point_id
        or qdrant_recall.get("written_point_occurrences") != 1
        or recalled_point_ids.count(written_point_id) != 1
    ):
        raise VerifierFailure("Qdrant point and recall captures are not cross-bound")

    signal_by_component = {
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
    tempo = facts_for("tempo_trace")
    tempo_trace_ids = _mapping(
        tempo.get("operation_otel_trace_ids"), "Tempo operation trace ids"
    )
    tempo_operations = _mapping(tempo.get("operations"), "Tempo operations")
    top_services = tempo.get("services")
    top_components = tempo.get("components")
    if (
        tempo.get("http_status") != 200
        or tempo.get("otel_trace_id") != primary_otel_trace_id
        or tempo_trace_ids != dict(operation_otel_trace_ids)
        or set(tempo_operations) != expected_operations
        or not isinstance(top_services, list)
        or any(not isinstance(item, str) or not item for item in top_services)
        or len(top_services) != len(set(top_services))
        or not isinstance(top_components, list)
        or any(not isinstance(item, str) or not item for item in top_components)
        or len(top_components) != len(set(top_components))
    ):
        raise VerifierFailure("Tempo aggregate trace facts are invalid")

    observed_services: set[str] = set()
    observed_components: set[str] = set()
    for operation in OTEL_OPERATIONS:
        operation_facts = _mapping(
            tempo_operations.get(operation), f"Tempo {operation} operation"
        )
        services_value = operation_facts.get("services")
        components_value = operation_facts.get("components")
        component_signals = _mapping(
            operation_facts.get("component_signals"),
            f"Tempo {operation} component signals",
        )
        expected_components = OPERATION_OTEL_COMPONENTS[operation]
        expected_services = OPERATION_OTEL_SERVICES[operation]
        if (
            set(operation_facts) != OPERATION_OTEL_FACT_KEYS
            or operation_facts.get("otel_trace_id")
            != operation_otel_trace_ids[operation]
            or not isinstance(services_value, list)
            or any(not isinstance(item, str) or not item for item in services_value)
            or len(services_value) != len(set(services_value))
            or set(services_value) != expected_services
            or not isinstance(components_value, list)
            or any(not isinstance(item, str) or not item for item in components_value)
            or len(components_value) != len(set(components_value))
            or set(components_value) != expected_components
            or set(component_signals) != expected_components
            or _positive_int(
                operation_facts.get("span_count"),
                f"Tempo {operation} span count",
            )
            < 1
            or _positive_int(
                operation_facts.get("client_span_count"),
                f"Tempo {operation} client span count",
            )
            < 1
            or any(
                component_signals.get(component) != [signal_by_component[component]]
                for component in expected_components
            )
        ):
            raise VerifierFailure(f"Tempo {operation} trace facts are invalid")
        observed_services.update(expected_services)
        observed_components.update(expected_components)

    if (
        set(top_services) != observed_services
        or set(top_components) != observed_components
    ):
        raise VerifierFailure("Tempo aggregate unions are invalid")
    if not components.issubset(observed_components):
        raise VerifierFailure("lineage proof names an unexpected component")
    return sorted(observed_components)


def persist_finalized_runtime(
    config: GateConfig,
    *,
    prior: dict[str, Any],
    state: dict[str, Any],
    runtime: dict[str, Any],
) -> None:
    # The immutable runtime is the completion payload. Persist it before the
    # mutable marker so a crash can always retry without a finalized/missing
    # artifact split-brain state.
    write_json_idempotent(config.runtime_path(), runtime)
    replace_json_state(config.state_path(), prior, state)


def _finalize_phase(config: GateConfig) -> None:
    state = _load_state(config)
    if _phase_completed(state, "finalize", "none"):
        runtime = runtime_fragment(
            load_json_file(config.runtime_path(), "runtime fragment")
        )
        if (
            set(_mapping(runtime["raw_proofs"].get("records"), "raw proof records"))
            != REQUIRED_PROOFS
        ):
            raise VerifierFailure("final runtime proof inventory is incomplete")
        return
    _all_faults_verified(state)
    prior = _copy(state)
    trace_ids = _mapping(state["operations"].get("trace_ids"), "operation trace state")
    if set(trace_ids) != {
        "oidc",
        "dagster",
        "object_storage",
        "qdrant",
        "external_callback",
    } or any(
        not BUSINESS_TRACE_PATTERN.fullmatch(str(value)) for value in trace_ids.values()
    ):
        raise VerifierFailure("operation trace inventory is invalid")
    otel_trace_id = _nonempty_text(
        state["operations"].get("otel_trace_id"), "OTel trace id"
    )
    if not re.fullmatch(r"[0-9a-f]{32}", otel_trace_id) or int(otel_trace_id, 16) == 0:
        raise VerifierFailure("OTel trace id is invalid")
    operation_otel_trace_ids = _mapping(
        state["operations"].get("operation_otel_trace_ids"),
        "operation OTel trace state",
    )
    if (
        set(operation_otel_trace_ids) != set(OTEL_OPERATIONS)
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9a-f]{32}", value)
            or int(value, 16) == 0
            for value in operation_otel_trace_ids.values()
        )
        or len(set(operation_otel_trace_ids.values())) != len(OTEL_OPERATIONS)
        or operation_otel_trace_ids.get("dagster") != otel_trace_id
    ):
        raise VerifierFailure("operation OTel trace inventory is invalid")

    browser = BrowserClient(config)
    try:
        tempo_facts = _tempo_observation(
            browser,
            config,
            operation_otel_trace_ids,
        )
    finally:
        browser.close()
    _write_capture(config, state, "tempo_trace", tempo_facts)

    records: dict[str, Any] = {}
    proof_facts: dict[str, Any] = {}
    for proof_id in sorted(REQUIRED_PROOFS):
        records[proof_id] = _read_capture(config, state, proof_id)
        proof_facts[proof_id] = _mapping(
            records[proof_id].get("facts"), f"{proof_id} capture facts"
        )
    raw_proofs = {
        "schema_version": RAW_PROOFS_SCHEMA,
        "records": records,
        "bundle_sha256": canonical_sha256(records),
    }

    recovery: dict[str, dict[str, Any]] = {}
    for dependency in DEPENDENCIES:
        combined_facts: dict[str, Any] = {}
        for proof_id in recovery_proof_ids(dependency):
            candidate = _mapping(proof_facts.get(proof_id), f"{proof_id} facts")
            if any(
                key in combined_facts and combined_facts[key] != value
                for key, value in candidate.items()
            ):
                raise VerifierFailure("final recovery proof identity bindings conflict")
            combined_facts.update(candidate)
        recovery[dependency] = recovery_case_from_facts(dependency, combined_facts)
    linked_components = linked_components_from_facts(
        proof_facts,
        trace_ids=trace_ids,
        operation_otel_trace_ids=operation_otel_trace_ids,
        primary_otel_trace_id=otel_trace_id,
    )
    runtime = runtime_fragment(
        {
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
                "trace_id": trace_ids["oidc"],
            },
            "adapters": {
                "dagster": {
                    "mode": "real",
                    "trace_id": trace_ids["dagster"],
                    "submitted": True,
                    "signed_completion_verified": True,
                },
                "object_storage": {
                    "mode": "real",
                    "provider": "minio",
                    "trace_id": trace_ids["object_storage"],
                    "object_verified": True,
                },
                "qdrant": {
                    "mode": "real",
                    "trace_id": trace_ids["qdrant"],
                    "embedding_provider": "http",
                    "embedding_transport": "https",
                    "semantic_embedding": True,
                    "point_verified": True,
                    "recall_verified": True,
                    "reference_protocol_only": True,
                    "model_quality_certified": False,
                },
                "external_callback": {
                    "mode": "real",
                    "trace_id": trace_ids["external_callback"],
                    "transport": "https",
                    "signature_mode": "hmac-sha256-v2",
                    "signature_verified": True,
                    "replay_rejected": True,
                },
            },
            "observability": {
                "otel_enabled": True,
                "collector_export_verified": True,
                "business_trace_id": trace_ids["dagster"],
                "otel_trace_id": otel_trace_id,
                "services": tempo_facts["services"],
            },
            "trace": {
                "primary_business_trace_id": trace_ids["dagster"],
                "otel_trace_id": otel_trace_id,
                "operation_otel_trace_ids": operation_otel_trace_ids,
                "operation_trace_ids": trace_ids,
                "linked_components": linked_components,
            },
            "raw_proofs": raw_proofs,
            "recovery": recovery,
        }
    )
    state["completed_phases"].append("finalize")
    state["updated_at"] = _utc_now()
    persist_finalized_runtime(config, prior=prior, state=state, runtime=runtime)


def run_phase(
    *,
    phase: str,
    dependency: str,
    artifact_dir: Path,
    run_suffix: str,
) -> None:
    validate_phase_dependency(phase, dependency)
    config = GateConfig(artifact_dir, run_suffix)
    if phase == "initial":
        _initial_phase(config)
    elif phase == "fault-prepare":
        _fault_prepare(config, dependency)
    elif phase == "fault-during":
        _fault_during(config, dependency)
    elif phase == "fault-verify":
        _fault_verify(config, dependency)
    else:
        _finalize_phase(config)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the real Auris Flow production Compose path in phases."
    )
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument(
        "--dependency",
        choices=("none", *DEPENDENCIES),
        default=os.environ.get("AURIS_GATE_DEPENDENCY", "none"),
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--run-suffix", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dependency = args.dependency
    try:
        run_phase(
            phase=args.phase,
            dependency=dependency,
            artifact_dir=args.artifact_dir,
            run_suffix=args.run_suffix,
        )
    except VerifierFailure as exc:
        print(f"production path verifier failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(
            "production path verifier failed: internal verifier failure",
            file=sys.stderr,
        )
        return 1
    print(f"production path verifier phase passed: {args.phase}/{dependency}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
