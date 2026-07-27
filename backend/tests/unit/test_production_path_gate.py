from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from app.services.adapters import RealExternalCallbackClient

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


def _load_gate_support() -> ModuleType:
    path = ROOT / "production" / "tests" / "production_gate_support.py"
    spec = importlib.util.spec_from_file_location("production_gate_support_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_runtime_verifier() -> ModuleType:
    path = ROOT / "production" / "tests" / "production_path_verifier.py"
    spec = importlib.util.spec_from_file_location("production_path_verifier_contract", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_callback_client_reconciles_without_private_gate_control_header() -> None:
    """The real adapter signs POSTs, then reconciles with an ordinary same-origin GET."""

    support = _load_gate_support()
    key_id = "callback-production-gate-test"
    key_material = "callback-production-gate-active-key-material-2026-07"
    key_bindings = json.dumps({key_id: {"secret": key_material, "state": "active"}})
    control_secret = "gate-control-test-" + ("x" * 32)
    state = support.SupportState(
        mode="callback",
        control_secret=control_secret,
        callback_keyring=support.parse_callback_keyring(
            key_bindings,
            active_key_id=key_id,
        ),
    )
    server = support.GateSupportServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        client = RealExternalCallbackClient(
            callback_url=f"{base_url}/callbacks/platform",
            key_bindings=key_bindings,
            active_key_id=key_id,
            app_env="local",
            nonce_factory=lambda: "production-gate-receipt-nonce-001",
        )
        payload = {
            "target": "crm_reception_order",
            "tenant_id": "tenant_production_gate",
            "project_id": "project_production_gate",
            "trace_id": "trace_production_gate_callback",
            "run_id": "run_production_gate_callback",
            "dispatch_idempotency_key": "callback-production-gate-idempotency-001",
            "payload_template": {"evidence_pack_id": "evidence_production_gate"},
        }

        sent = client.send_signed_callback(payload)
        assert sent.status == "success", sent
        assert sent.details["delivery_id"] != sent.details["callback_receipt_id"]

        reconciled = client.reconcile_callback(payload)
        assert reconciled.status == "success", reconciled
        assert reconciled.details["reconciled"] is True
        assert reconciled.details["delivery_id"] == sent.details["delivery_id"]
        assert reconciled.details["callback_receipt_id"] == sent.details["callback_receipt_id"]

        # Gate introspection remains private even though the simulated external
        # provider's receipt protocol is available to the real callback adapter.
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("GET", "/proofs")
        response = connection.getresponse()
        response.read()
        assert response.status == 404
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "GET",
            "/proofs",
            headers={"X-Auris-Gate-Control": control_secret},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request("POST", "/control/timeout-next", body=b"")
        response = connection.getresponse()
        response.read()
        assert response.status == 404
        connection.close()

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.request(
            "POST",
            "/control/timeout-next",
            body=b"",
            headers={"X-Auris-Gate-Control": control_secret},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 200
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
            "audio_inference_fixture": {
                "service": "production-gate-embedding",
                "transport": "https",
                "reference_protocol_only": True,
                "model_quality_certified": False,
            },
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
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "networks": ["internal"],
                "environment": {
                    "AUDIO_INFERENCE_API_TOKEN_FILE": ("/run/secrets/audio_inference_api_token"),
                    "AUDIO_INFERENCE_PROVIDER": "audio_intelligence_default",
                    "AUDIO_INFERENCE_MODEL": "audio-v2.3.1",
                },
            },
            "production-gate-callback": {
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "networks": ["production-gate-callback"],
            },
            "production-path-verifier": {
                "user": "10001:10001",
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
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


def _rehash_raw_proof(gate: ModuleType, evidence: dict[str, object], proof_id: str) -> None:
    raw_proofs = evidence["raw_proofs"]
    assert isinstance(raw_proofs, dict)
    records = raw_proofs["records"]
    assert isinstance(records, dict)
    record = records[proof_id]
    assert isinstance(record, dict)
    facts = record["facts"]
    capture = record["capture"]
    assert isinstance(capture, dict)
    capture["observations"] = facts
    record["facts_sha256"] = gate._canonical_sha256(facts)
    record["capture_sha256"] = gate._canonical_sha256(capture)
    raw_proofs["bundle_sha256"] = gate._canonical_sha256(records)


def _valid_evidence() -> dict[str, object]:
    trace_ids = {
        "oidc": "trace_production_path_oidc_001",
        "dagster": "trace_production_path_dagster_001",
        "object_storage": "trace_production_path_object_001",
        "qdrant": "trace_production_path_qdrant_001",
        "external_callback": "trace_production_path_callback_001",
    }

    operation_otel_trace_ids = {
        "oidc": "a" * 32,
        "dagster": "d" * 32,
        "object_storage": "b" * 32,
        "qdrant": "c" * 32,
        "external_callback": "e" * 32,
    }
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
    operation_components = {
        "oidc": {"bff", "mysql", "oidc", "otel"},
        "dagster": {"bff", "mysql", "redis", "outbox", "worker", "dagster", "otel"},
        "object_storage": {
            "bff",
            "mysql",
            "outbox",
            "worker",
            "object_storage",
            "otel",
        },
        "qdrant": {"bff", "mysql", "outbox", "worker", "qdrant", "otel"},
        "external_callback": {
            "bff",
            "mysql",
            "outbox",
            "worker",
            "external_callback",
            "otel",
        },
    }
    operation_services = {
        "oidc": {"auris-flow-bff"},
        "dagster": {
            "auris-flow-bff",
            "auris-flow-worker",
            "auris-flow-dagster-code",
        },
        "object_storage": {"auris-flow-bff", "auris-flow-worker"},
        "qdrant": {"auris-flow-bff", "auris-flow-worker"},
        "external_callback": {"auris-flow-bff", "auris-flow-worker"},
    }
    tempo_operations = {
        operation: {
            "otel_trace_id": operation_otel_trace_ids[operation],
            "services": sorted(operation_services[operation]),
            "components": sorted(operation_components[operation]),
            "component_signals": {
                component: [signal_by_component[component]]
                for component in sorted(operation_components[operation])
            },
            "span_count": 8,
            "client_span_count": 4,
        }
        for operation in operation_otel_trace_ids
    }

    def raw_proof(proof_id: str, source: str, facts: dict[str, object]) -> dict[str, object]:
        capture = {
            "schema_version": "auris.production-path.capture.v1",
            "proof_id": proof_id,
            "source": source,
            "observations": facts,
        }
        facts_encoded = json.dumps(
            facts,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        capture_encoded = json.dumps(
            capture,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "source": source,
            "media_type": "application/json",
            "capture": capture,
            "capture_sha256": hashlib.sha256(capture_encoded).hexdigest(),
            "facts_sha256": hashlib.sha256(facts_encoded).hexdigest(),
            "facts": facts,
        }

    raw_records = {
        "oidc_discovery": raw_proof(
            "oidc_discovery",
            "https-response",
            {
                "http_status": 200,
                "issuer": "https://auris-production-gate.invalid/realms/auris-flow",
                "authorization_endpoint_scheme": "https",
                "token_endpoint_scheme": "https",
                "jwks_uri_scheme": "https",
            },
        ),
        "oidc_jwks": raw_proof(
            "oidc_jwks",
            "https-response",
            {"http_status": 200, "rsa_signing_key_ids": ["gate-rsa-key"]},
        ),
        "oidc_code_exchange": raw_proof(
            "oidc_code_exchange",
            "mysql",
            {
                "grant_type": "authorization_code",
                "pkce_method": "S256",
                "consumed_state_count": 1,
                "browser_session_count": 1,
                "trace_id": trace_ids["oidc"],
            },
        ),
        "browser_session": raw_proof(
            "browser_session",
            "mysql",
            {
                "cookie_name": "__Host-auris_session",
                "cookie_secure": True,
                "cookie_http_only": True,
                "provider": "oidc_session",
                "active_session_count": 1,
                "session_token_sha256": "1" * 64,
                "trace_id": trace_ids["oidc"],
            },
        ),
        "mysql_authority": raw_proof(
            "mysql_authority",
            "mysql",
            {
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "authoritative_run_ids": [
                    "task_run_gate",
                    "knowledge_build_gate",
                    "object_ingest_gate",
                    "callback_gate",
                ],
                "authoritative_run_count": 4,
                "processed_outbox_count": 4,
            },
        ),
        "dagster_graphql": raw_proof(
            "dagster_graphql",
            "dagster-graphql",
            {
                "graphql_operation": "pipelineRunOrError",
                "response_typename": "Run",
                "dagster_run_id": "01234567-89ab-cdef-0123-456789abcdef",
                "dagster_status": "SUCCESS",
                "trace_id": trace_ids["dagster"],
            },
        ),
        "dagster_completion": raw_proof(
            "dagster_completion",
            "mysql",
            {
                "receipt_count": 1,
                "processing_state": "completed",
                "completion_status": "success",
                "signature_mode": "hmac-sha256-v2",
                "signature_key_id": "dagster-v1",
                "run_trace_id": trace_ids["dagster"],
            },
        ),
        "embedding_https": raw_proof(
            "embedding_https",
            "https-response",
            {
                "transport": "https",
                "tls_verified": True,
                "provider": "reference-semantic-protocol",
                "model": "auris-production-gate-reference-semantic-v1",
                "request_count": 2,
                "purposes": ["document", "query"],
                "dimension": 8,
                "reference_protocol_only": True,
                "model_quality_certified": False,
            },
        ),
        "qdrant_point": raw_proof(
            "qdrant_point",
            "qdrant-http",
            {
                "http_status": 200,
                "collection": "knowledge_chunks_v1",
                "point_id": "12345678-1234-5678-1234-567812345678",
                "point_count": 1,
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "trace_id": trace_ids["qdrant"],
                "vector_dimension": 8,
            },
        ),
        "qdrant_recall": raw_proof(
            "qdrant_recall",
            "bff-response",
            {
                "http_status": 200,
                "authorized_hit_count": 1,
                "point_ids": ["12345678-1234-5678-1234-567812345678"],
                "written_point_id": "12345678-1234-5678-1234-567812345678",
                "written_point_occurrences": 1,
                "trace_id": trace_ids["qdrant"],
            },
        ),
        "minio_object": raw_proof(
            "minio_object",
            "minio-s3",
            {
                "bucket": "auris-flow",
                "object_key": "aurora_auto/sales_qa/gate.json",
                "http_status": 200,
                "expected_content_sha256": "2" * 64,
                "observed_content_sha256": "2" * 64,
                "content_length": 128,
                "trace_id": trace_ids["object_storage"],
            },
        ),
        "callback_delivery": raw_proof(
            "callback_delivery",
            "https-response",
            {
                "transport": "https",
                "tls_verified": True,
                "signature_mode": "hmac-sha256-v2",
                "signature_verified": True,
                "verified_receipt_count": 1,
                "receipt_id": "callback_receipt_0123456789abcdef",
                "trace_id": trace_ids["external_callback"],
            },
        ),
        "callback_replay": raw_proof(
            "callback_replay",
            "https-response",
            {
                "http_status": 409,
                "error_code": "CALLBACK_SIGNATURE_REPLAYED",
                "replay_rejected": True,
            },
        ),
        "tempo_trace": raw_proof(
            "tempo_trace",
            "tempo-http",
            {
                "http_status": 200,
                "otel_trace_id": operation_otel_trace_ids["dagster"],
                "operation_otel_trace_ids": operation_otel_trace_ids,
                "operations": tempo_operations,
                "services": sorted(set().union(*operation_services.values())),
                "components": sorted(set().union(*operation_components.values())),
            },
        ),
        "mysql_restart": raw_proof(
            "mysql_restart",
            "compose-runtime",
            {
                "container_id_sha256": "3" * 64,
                "started_at_before": "2026-01-01T00:00:00Z",
                "started_at_after": "2026-01-01T00:01:00Z",
                "ready_status_after": 200,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "worker_crash": raw_proof(
            "worker_crash",
            "compose-runtime",
            {
                "container_id_sha256": "4" * 64,
                "started_at_before": "2026-01-01T00:00:00Z",
                "started_at_after": "2026-01-01T00:01:00Z",
                "event_id": 42,
                "event_status_before": "pending",
                "event_status_after": "processed",
                "remote_run_count": 1,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "duplicate_delivery": raw_proof(
            "duplicate_delivery",
            "mysql",
            {
                "event_id": 43,
                "delivery_attempt_count": 2,
                "dispatch_attempt_count": 1,
                "reconcile_attempt_count": 1,
                "remote_receipt_count": 1,
                "business_outcome_count": 1,
                "stale_owner_rejected": True,
                "new_owner_accepted": True,
                "lease_generation_before": 7,
                "lease_generation_after": 8,
                "claim_token_sha256_before": "5" * 64,
                "claim_token_sha256_after": "6" * 64,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "callback_timeout": raw_proof(
            "callback_timeout",
            "mysql",
            {
                "event_id": 43,
                "first_attempt_status": "outcome_unknown",
                "final_attempt_status": "success",
                "final_delivery_mode": "reconcile",
                "remote_receipt_count": 1,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "dead_letter_retry": raw_proof(
            "dead_letter_retry",
            "mysql",
            {
                "source_run_id_sha256": "7" * 64,
                "retry_run_id_sha256": "8" * 64,
                "source_event_id": 44,
                "retry_event_id": 45,
                "source_event_aggregate_id_sha256": "7" * 64,
                "retry_event_aggregate_id_sha256": "8" * 64,
                "source_payload_dead_letter_event_id": 44,
                "retry_payload_retry_of_event_id": 44,
                "retry_payload_retry_of_run_id_sha256": "7" * 64,
                "source_trace_id": "trace_dead_letter_retry_001",
                "retry_payload_retry_of_trace_id": "trace_dead_letter_retry_001",
                "source_status_before": "failed",
                "source_status_after": "failed",
                "source_terminal_reason": "outbox_dispatch_dead_letter",
                "source_status_version": 3,
                "source_event_status": "dead_letter",
                "source_delivery_state": "failed",
                "source_error_code": "QDRANT_PAYLOAD_INVALID",
                "source_last_error_sha256": "9" * 64,
                "source_lease_generation": 1,
                "source_dead_letter_attempt_count": 1,
                "source_snapshot_sha256_before": "a" * 64,
                "source_snapshot_sha256_after": "a" * 64,
                "source_attempt_ledger_sha256_before": "b" * 64,
                "source_attempt_ledger_sha256_after": "b" * 64,
                "retry_response_replayed": True,
                "first_response_sha256": "5" * 64,
                "replay_response_sha256": "5" * 64,
                "stored_response_sha256": "5" * 64,
                "idempotency_record_count": 1,
                "idempotency_state": "completed",
                "idempotency_status_code": 202,
                "idempotency_request_sha256": "c" * 64,
                "expected_idempotency_request_sha256": "c" * 64,
                "idempotency_response_run_id_sha256": "8" * 64,
                "idempotency_user_sha256": "d" * 64,
                "expected_retry_idempotency_key_sha256": "e" * 64,
                "retry_run_count": 1,
                "retry_event_count": 1,
                "retry_dispatch_attempt_count": 1,
                "retry_event_otel_trace_id": "f" * 32,
                "retry_dispatch_idempotency_key_sha256": "2" * 64,
                "retry_dispatch_request_sha256": "3" * 64,
                "retry_attempt_request_sha256": "3" * 64,
                "retry_attempt_id_sha256": "4" * 64,
                "retry_expected_attempt_id_sha256": "4" * 64,
                "retry_point_id_sha256": "f" * 64,
                "retry_dispatch_payload_sha256": "1" * 64,
                "retry_attempt_payload_sha256": "1" * 64,
                "retry_event_status": "processed",
                "retry_run_status": "success",
                "retry_trace_inherited": True,
                "retry_audit_count": 1,
                "retry_audit_actor_sha256": "d" * 64,
                "retry_audit_idempotency_key_sha256": "e" * 64,
                "retry_audit_trace_matches": True,
                "retry_audit_lineage_matches": True,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "dead_letter_retry_qdrant": raw_proof(
            "dead_letter_retry_qdrant",
            "qdrant-http",
            {
                "http_status": 200,
                "collection": "knowledge_chunks_v1",
                "point_id_sha256": "f" * 64,
                "dispatch_point_id_sha256": "f" * 64,
                "attempt_point_id_sha256": "f" * 64,
                "payload_sha256": "1" * 64,
                "dispatch_payload_sha256": "1" * 64,
                "attempt_payload_sha256": "1" * 64,
                "retry_run_id_sha256": "8" * 64,
                "retry_event_id": 45,
                "dispatch_idempotency_key_sha256": "2" * 64,
                "dispatch_request_sha256": "3" * 64,
                "attempt_request_sha256": "3" * 64,
                "attempt_id_sha256": "4" * 64,
                "tenant_id": "aurora_auto",
                "project_id": "sales_qa",
                "trace_id": "trace_dead_letter_retry_001",
                "filtered_point_count": 1,
                "point_occurrences": 1,
                "cross_tenant_count": 0,
                "cross_project_count": 0,
                "scope_match": True,
                "dispatch_receipt_match": True,
                "attempt_receipt_match": True,
                "payload_hash_match": True,
            },
        ),
        "dead_letter_retry_trace": raw_proof(
            "dead_letter_retry_trace",
            "tempo-http",
            {
                "http_status": 200,
                "observed_business_trace_id": "trace_dead_letter_retry_001",
                "bff_span_id_sha256": "5" * 64,
                "outbox_parent_span_id_sha256": "5" * 64,
                "outbox_span_id_sha256": "6" * 64,
                "adapter_parent_span_id_sha256": "6" * 64,
                "adapter_span_id_sha256": "7" * 64,
                "qdrant_parent_span_id_sha256": "7" * 64,
                "bff_server_span_count": 1,
                "bff_server_http_method": "POST",
                "bff_server_route": "/api/v1/runs/{id}/retries",
                "outbox_process_span_count": 1,
                "adapter_dispatch_span_count": 1,
                "qdrant_client_span_count": 2,
                "qdrant_write_span_count": 1,
                "otel_trace_id": "f" * 32,
                "services": ["auris-flow-bff", "auris-flow-worker"],
                "components": [
                    "bff",
                    "mysql",
                    "otel",
                    "outbox",
                    "qdrant",
                    "worker",
                ],
                "component_signals": {
                    "bff": ["service.name=auris-flow-bff"],
                    "mysql": ["db.system=mysql"],
                    "otel": ["tempo.trace"],
                    "outbox": ["span.name=outbox.process"],
                    "qdrant": ["client.host=qdrant"],
                    "worker": ["service.name=auris-flow-worker"],
                },
                "span_count": 8,
                "client_span_count": 3,
            },
        ),
        "qdrant_outage": raw_proof(
            "qdrant_outage",
            "compose-runtime",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "failed_dependency_during": "qdrant",
                "failed_dependency_status_during": "not_ready",
                "missing_required_during": ["qdrant"],
                "recovered_dependency_status_after": "ok",
                "missing_required_after": [],
                "point_id": "12345678-1234-5678-1234-567812345678",
                "point_present_after": True,
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
        "redis_outage": raw_proof(
            "redis_outage",
            "compose-runtime",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "failed_dependency_during": "redis",
                "failed_dependency_status_during": "not_ready",
                "missing_required_during": ["redis"],
                "recovered_dependency_status_after": "ok",
                "missing_required_after": [],
                "authoritative_run_count_before": 4,
                "authoritative_run_count_after": 4,
            },
        ),
    }
    raw_bundle_hash = hashlib.sha256(
        json.dumps(
            raw_records,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    def recovery(proof_id: str) -> dict[str, object]:
        proof_ids = (
            [
                "dead_letter_retry",
                "dead_letter_retry_qdrant",
                "dead_letter_retry_trace",
            ]
            if proof_id == "dead_letter_retry"
            else [proof_id]
        )
        return {
            "proven": True,
            "authority_consistent": True,
            "no_duplicate_business_outcome": True,
            "raw_proof_ids": proof_ids,
        }

    defined_services = [
        "bff",
        "worker",
        "mysql",
        "db-bootstrap",
        "redis",
        "minio-volume-init",
        "minio",
        "minio-bootstrap",
        "qdrant",
        "migrate",
        "keycloak",
        "identity-bootstrap",
        "production-path-seed",
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
        "production-gate-embedding",
        "production-gate-callback",
        "production-path-verifier",
    ]
    external_images = {
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

    def configured_image(service: str) -> str:
        if service in external_images:
            return external_images[service]
        if service.startswith("dagster-"):
            return "auris-flow-production-gate-dagster:aaaaaaaaaaaa"
        if service == "edge":
            return "auris-flow-production-gate-edge:aaaaaaaaaaaa"
        return "auris-flow-production-gate-bff:aaaaaaaaaaaa"

    def runtime_observation(service: str, *, completed: bool = False) -> dict[str, object]:
        image = configured_image(service)
        repository = image.rsplit(":", 1)[0]
        return {
            "container_id_sha256": hashlib.sha256(service.encode()).hexdigest(),
            "configured_image": image,
            "image_id": "sha256:" + hashlib.sha256(f"image:{service}".encode()).hexdigest(),
            "repo_digests": (
                [f"{repository}@sha256:" + hashlib.sha256(f"repo:{service}".encode()).hexdigest()]
                if service in external_images
                else []
            ),
            "os": "linux",
            "architecture": "amd64",
            "state": "exited" if completed else "running",
            **({"exit_code": 0} if completed else {"health": "healthy"}),
        }

    running_services = {
        service: runtime_observation(service)
        for service in (
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
        )
    }
    completed_services = {
        service: runtime_observation(service, completed=True)
        for service in (
            "db-bootstrap",
            "dagster-storage-bootstrap",
            "minio-volume-init",
            "minio-bootstrap",
            "migrate",
            "identity-bootstrap",
            "production-path-seed",
        )
    }

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
            "services": defined_services,
            "host_runtime": {
                "schema_version": "auris.production-path.host-runtime.v1",
                "native_linux": True,
                "host_platform": "linux",
                "docker_endpoint_scheme": "unix",
                "docker_endpoint_path": "/var/run/docker.sock",
                "docker_ostype": "linux",
                "docker_operating_system": "Ubuntu 24.04 LTS",
                "architecture": "amd64",
                "rootless": False,
                "cgroup_driver": "systemd",
                "cgroup_version": "2",
                "storage_driver": "overlay2",
            },
            "runtime": {
                "running_services": running_services,
                "completed_services": completed_services,
            },
        },
        "runtime_sources": {
            relative: (_sha256(ROOT / relative) if (ROOT / relative).is_file() else "e" * 64)
            for relative in (
                "scripts/verify_production_path_runtime.py",
                "scripts/verify_production_path_gate.py",
                "production/tests/production_path_verifier.py",
                "production/tests/production_gate_support.py",
                "production/tests/production-path-keycloak-realm.template.json",
                "production/tests/production-path-gate.env",
            )
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
                "trace_id": trace_ids["object_storage"],
                "provider": "minio",
                "object_verified": True,
            },
            "qdrant": {
                "mode": "real",
                "trace_id": trace_ids["qdrant"],
                "embedding_provider": "http",
                "embedding_transport": "https",
                "semantic_embedding": True,
                "reference_protocol_only": True,
                "model_quality_certified": False,
                "point_verified": True,
                "recall_verified": True,
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
            "otel_trace_id": "d" * 32,
            "services": ["auris-flow-bff", "auris-flow-worker", "auris-flow-dagster-code"],
        },
        "trace": {
            "primary_business_trace_id": trace_ids["dagster"],
            "otel_trace_id": operation_otel_trace_ids["dagster"],
            "operation_otel_trace_ids": operation_otel_trace_ids,
            "operation_trace_ids": trace_ids,
            "linked_components": [
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
            ],
        },
        "raw_proofs": {
            "schema_version": "auris.production-path.raw-proofs.v1",
            "bundle_sha256": raw_bundle_hash,
            "records": raw_records,
        },
        "recovery": {
            "mysql_restart": recovery("mysql_restart"),
            "worker_crash": recovery("worker_crash"),
            "duplicate_delivery": recovery("duplicate_delivery"),
            "callback_timeout": recovery("callback_timeout"),
            "dead_letter_retry": recovery("dead_letter_retry"),
            "qdrant_outage": recovery("qdrant_outage"),
            "redis_outage": recovery("redis_outage"),
        },
    }


def _valid_release_evidence(gate: ModuleType, root: Path) -> tuple[dict[str, object], Path, Path]:
    for relative in (
        "production/compose.yaml",
        "production/tests/production-path-gate.compose.yaml",
        *gate.REQUIRED_RUNTIME_SOURCES,
    ):
        source = ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    evidence = _valid_evidence()
    compose = evidence["compose"]
    assert isinstance(compose, dict)
    runtime = compose["runtime"]
    assert isinstance(runtime, dict)
    running = runtime["running_services"]
    completed = runtime["completed_services"]
    assert isinstance(running, dict)
    assert isinstance(completed, dict)
    observations = {**running, **completed}
    lock_services = set(observations) - set(gate.RELEASE_GATE_IMAGE_ALIASES)
    release_tag = "v1.0.0-rc.1"
    source_commit = "a" * 40
    images = {
        service: (
            f"registry.example/auris/{service}:{release_tag}@sha256:"
            + hashlib.sha256(f"release:{service}".encode()).hexdigest()
        )
        for service in sorted(lock_services)
    }
    runtime_images: dict[str, object] = {}
    for service, raw_observation in observations.items():
        assert isinstance(raw_observation, dict)
        lock_service = gate.RELEASE_GATE_IMAGE_ALIASES.get(service, service)
        configured_image = images[lock_service]
        repository, digest = configured_image.rsplit("@", 1)
        repository = repository.rsplit(":", 1)[0]
        repo_digest = f"{repository}@{digest}"
        raw_observation["configured_image"] = configured_image
        raw_observation["repo_digests"] = [repo_digest]
        runtime_images[service] = {
            "lock_service": lock_service,
            "configured_image": configured_image,
            "repo_digest": repo_digest,
            "image_id": raw_observation["image_id"],
            "container_id_sha256": raw_observation["container_id_sha256"],
        }

    image_lock = root / "build" / "release" / "images.lock.json"
    image_lock.parent.mkdir(parents=True, exist_ok=True)
    image_lock.write_text(
        json.dumps(
            {
                "schema_version": gate.RELEASE_IMAGE_LOCK_SCHEMA,
                "release_tag": release_tag,
                "source_commit": source_commit,
                "images": images,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    release_compose = root / "production" / "compose.release.json"
    release_compose.write_text(
        json.dumps(
            {
                "services": {
                    service: {"image": reference} for service, reference in images.items()
                },
                "x-auris-release": {
                    "schema_version": gate.RELEASE_IMAGE_LOCK_SCHEMA,
                    "release_tag": release_tag,
                    "source_commit": source_commit,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    evidence["schema_version"] = gate.RELEASE_EVIDENCE_SCHEMA
    evidence["release_tag"] = release_tag
    evidence["execution_environment"] = "production-compose-prebuilt-release"
    compose["base"] = "production/compose.release.json"
    compose["base_sha256"] = _sha256(release_compose)
    compose["overlay_sha256"] = _sha256(
        root / "production" / "tests" / "production-path-gate.compose.yaml"
    )
    evidence["runtime_sources"] = {
        relative: _sha256(root / relative) for relative in gate.REQUIRED_RUNTIME_SOURCES
    }
    evidence["release"] = {
        "schema_version": gate.RELEASE_BINDING_SCHEMA,
        "release_tag": release_tag,
        "source_commit": source_commit,
        "image_lock": "build/release/images.lock.json",
        "image_lock_sha256": _sha256(image_lock),
        "image_lock_schema_version": gate.RELEASE_IMAGE_LOCK_SCHEMA,
        "release_compose": "production/compose.release.json",
        "release_compose_sha256": _sha256(release_compose),
        "runtime_images": runtime_images,
    }
    return evidence, release_compose, image_lock


def test_release_evidence_reuses_full_runtime_semantic_validator(tmp_path: Path) -> None:
    gate = _load_gate()
    evidence, release_compose, image_lock = _valid_release_evidence(gate, tmp_path)

    errors = gate.validate_release_evidence(
        evidence,
        root=tmp_path,
        expected_commit="a" * 40,
        expected_release_tag="v1.0.0-rc.1",
        release_compose_path=release_compose,
        image_lock_path=image_lock,
    )

    assert errors == []


def test_release_evidence_cli_accepts_generated_and_transported_release_compose(
    tmp_path: Path,
) -> None:
    gate = _load_gate()
    evidence, release_compose, image_lock = _valid_release_evidence(gate, tmp_path)
    artifact = tmp_path / "build" / "release" / "final-runtime" / "production-path-gate.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
    transported_compose = (
        tmp_path / "build" / "release" / "deployment" / "production" / "compose.yaml"
    )
    transported_compose.parent.mkdir(parents=True)
    transported_compose.write_bytes(release_compose.read_bytes())

    for compose_path in (release_compose, transported_compose):
        completed = subprocess.run(
            [
                str(ROOT / "backend" / ".venv" / "bin" / "python"),
                "scripts/verify_production_path_gate.py",
                "release-evidence",
                "--artifact",
                "build/release/final-runtime/production-path-gate.json",
                "--expected-commit",
                "a" * 40,
                "--expected-release-tag",
                "v1.0.0-rc.1",
                "--release-compose",
                str(compose_path.relative_to(tmp_path)),
                "--image-lock",
                str(image_lock.relative_to(tmp_path)),
            ],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["release_evidence"] is True


def test_release_evidence_rejects_six_minimal_runtime_sections(tmp_path: Path) -> None:
    gate = _load_gate()
    evidence, release_compose, image_lock = _valid_release_evidence(gate, tmp_path)
    for section in (
        "identity",
        "adapters",
        "observability",
        "trace",
        "raw_proofs",
        "recovery",
    ):
        evidence[section] = {}

    errors = gate.validate_release_evidence(
        evidence,
        root=tmp_path,
        expected_commit="a" * 40,
        expected_release_tag="v1.0.0-rc.1",
        release_compose_path=release_compose,
        image_lock_path=image_lock,
    )

    assert errors
    assert any("raw proof" in error for error in errors)
    assert any("identity" in error for error in errors)


@pytest.mark.parametrize(
    ("proof_id", "field", "replacement"),
    [
        ("dead_letter_retry", "source_status_after", "success"),
        ("dead_letter_retry_qdrant", "point_occurrences", 2),
        ("dead_letter_retry_trace", "qdrant_write_span_count", 2),
    ],
)
def test_release_evidence_rejects_coherently_rehashed_dead_letter_proof_tampering(
    tmp_path: Path,
    proof_id: str,
    field: str,
    replacement: object,
) -> None:
    gate = _load_gate()
    evidence, release_compose, image_lock = _valid_release_evidence(gate, tmp_path)
    raw_proofs = evidence["raw_proofs"]
    assert isinstance(raw_proofs, dict)
    records = raw_proofs["records"]
    assert isinstance(records, dict)
    record = records[proof_id]
    assert isinstance(record, dict)
    facts = record["facts"]
    assert isinstance(facts, dict)
    facts[field] = replacement
    _rehash_raw_proof(gate, evidence, proof_id)

    errors = gate.validate_release_evidence(
        evidence,
        root=tmp_path,
        expected_commit="a" * 40,
        expected_release_tag="v1.0.0-rc.1",
        release_compose_path=release_compose,
        image_lock_path=image_lock,
    )

    assert errors
    assert any(proof_id in error for error in errors)


@pytest.mark.parametrize(
    "proof_id",
    ["dead_letter_retry", "dead_letter_retry_qdrant", "dead_letter_retry_trace"],
)
def test_release_evidence_rejects_missing_dead_letter_proof(tmp_path: Path, proof_id: str) -> None:
    gate = _load_gate()
    evidence, release_compose, image_lock = _valid_release_evidence(gate, tmp_path)
    raw_proofs = evidence["raw_proofs"]
    assert isinstance(raw_proofs, dict)
    records = raw_proofs["records"]
    assert isinstance(records, dict)
    del records[proof_id]
    raw_proofs["bundle_sha256"] = gate._canonical_sha256(records)

    errors = gate.validate_release_evidence(
        evidence,
        root=tmp_path,
        expected_commit="a" * 40,
        expected_release_tag="v1.0.0-rc.1",
        release_compose_path=release_compose,
        image_lock_path=image_lock,
    )

    assert errors
    assert any("raw proofs inventory" in error for error in errors)


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


def test_reference_embedding_is_semantic_protocol_only_not_feature_hashing() -> None:
    support = _load_gate_support()

    sales = support.reference_semantic_vector("销售政策和报价", dimension=8)
    quality = support.reference_semantic_vector("质检证据复核", dimension=8)
    unknown_a = support.reference_semantic_vector("unmapped-alpha", dimension=8)
    unknown_b = support.reference_semantic_vector("unmapped-bravo", dimension=8)

    assert len(sales) == len(quality) == 8
    assert sales[0] > 0 and sales[1] == 0
    assert quality[1] > 0 and quality[0] == 0
    assert unknown_a == unknown_b
    assert unknown_a[-1] == 1.0


def test_reference_audio_fixture_enforces_closed_hash_bound_protocol_only() -> None:
    support = _load_gate_support()
    request_payload = {
        "schema_version": "auris-flow-audio-provider-request-v1",
        "execution_contract": "auris-flow-audio-intelligence-v1",
        "execution_envelope_sha256": "a" * 64,
        "tenant_id": "aurora_auto",
        "project_id": "sales_qa",
        "trace_id": "trace_audio_gate_001",
        "run_id": "run_audio_gate_001",
        "dispatch_idempotency_key": "outbox:audio:gate:001",
        "outbox_fencing_token": "1:1",
        "deadline_at": "2099-07-21T12:00:00+00:00",
        "audio_session_id": "audio_session_001",
        "recording_id": "recording_001",
        "input_object": {
            "storage_object_id": "sto_audio_001",
            "storage_provider": "minio",
            "bucket": "auris-flow",
            "object_key": "tenants/aurora_auto/projects/sales_qa/audio/input.wav",
            "version_id": "exact-version-1",
            "content_sha256": "b" * 64,
            "content_length": 64,
            "content_type": "audio/wav",
        },
        "inference": {
            "provider": "audio_intelligence_default",
            "model": "audio-v2.3.1",
        },
        "capabilities": ["vad", "asr"],
    }
    body = support._canonical_bytes(request_payload)
    request_sha256 = hashlib.sha256(body).hexdigest()
    payload, record = support.reference_audio_response(
        body,
        provider="audio_intelligence_default",
        model="audio-v2.3.1",
        claimed_request_sha256=request_sha256,
        idempotency_key="audio-inference:outbox:audio:gate:001",
    )

    assert payload["request_sha256"] == request_sha256
    assert payload["input_object"] == request_payload["input_object"]
    assert (
        payload["result_sha256"]
        == hashlib.sha256(support._canonical_bytes(payload["result"])).hexdigest()
    )
    assert record["request_sha256"] == request_sha256

    forged = copy.deepcopy(request_payload)
    forged["input_object"]["unknown"] = "rejected"
    with pytest.raises(support.GateSupportError):
        support.reference_audio_response(
            support._canonical_bytes(forged),
            provider="audio_intelligence_default",
            model="audio-v2.3.1",
            claimed_request_sha256=request_sha256,
            idempotency_key="audio-inference:outbox:audio:gate:001",
        )


def test_callback_nonce_store_claim_is_atomic_and_replay_rejecting() -> None:
    support = _load_gate_support()
    nonce_store = support.AtomicNonceStore()

    assert nonce_store.claim(key_id="callback-v1", nonce="n" * 32, expires_at=2**31)
    assert not nonce_store.claim(key_id="callback-v1", nonce="n" * 32, expires_at=2**31)
    assert nonce_store.claim(key_id="callback-v2", nonce="n" * 32, expires_at=2**31)


def test_checked_in_gate_contract_is_ready_for_the_runtime_diagnostic() -> None:
    gate = _load_gate()
    document = yaml.safe_load(GATE_COMPOSE.read_text(encoding="utf-8"))

    errors = gate.validate_gate_compose(document)

    assert errors == []


def test_release_gate_mirrors_the_runtime_verifier_closed_proof_contract() -> None:
    gate = _load_gate()
    verifier = _load_runtime_verifier()

    runtime_driver_path = ROOT / "scripts" / "verify_production_path_runtime.py"
    driver_spec = importlib.util.spec_from_file_location(
        "production_path_runtime_contract", runtime_driver_path
    )
    assert driver_spec and driver_spec.loader
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)

    assert gate.RAW_PROOF_SOURCE_BY_ID == verifier.PROOF_SOURCES
    assert gate.PROOF_FACT_KEYS == verifier.PROOF_FACT_KEYS
    assert gate.OPERATION_OTEL_FACT_KEYS == verifier.OPERATION_OTEL_FACT_KEYS
    assert gate.OPERATION_OTEL_COMPONENTS == verifier.OPERATION_OTEL_COMPONENTS
    assert gate.OPERATION_OTEL_SERVICES == verifier.OPERATION_OTEL_SERVICES
    assert gate.HOST_RUNTIME_FIELDS == driver.HOST_RUNTIME_FIELDS


def test_minio_volume_initializer_is_bound_as_an_external_one_shot() -> None:
    gate = _load_gate()
    runtime_driver_path = ROOT / "scripts" / "verify_production_path_runtime.py"
    driver_spec = importlib.util.spec_from_file_location(
        "production_path_runtime_minio_init_contract", runtime_driver_path
    )
    assert driver_spec and driver_spec.loader
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)

    minio_image = "minio/minio:RELEASE.2025-04-22T22-12-26Z"
    assert "minio-volume-init" in gate.REQUIRED_COMPLETED_SERVICES
    assert gate.EXPECTED_EXTERNAL_SERVICE_IMAGES["minio-volume-init"] == minio_image
    assert "minio-volume-init" in driver.REQUIRED_COMPLETED_SERVICES
    assert "minio-volume-init" in driver.EXTERNAL_IMAGE_SERVICES


def test_dagster_storage_bootstrap_is_bound_as_a_first_party_one_shot() -> None:
    gate = _load_gate()
    runtime_driver_path = ROOT / "scripts" / "verify_production_path_runtime.py"
    driver_spec = importlib.util.spec_from_file_location(
        "production_path_runtime_dagster_bootstrap_contract", runtime_driver_path
    )
    assert driver_spec and driver_spec.loader
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)

    assert "dagster-storage-bootstrap" in gate.REQUIRED_BASE_SERVICES
    assert "dagster-storage-bootstrap" in gate.REQUIRED_COMPLETED_SERVICES
    assert "dagster-storage-bootstrap" not in gate.EXPECTED_EXTERNAL_SERVICE_IMAGES
    assert "dagster-storage-bootstrap" in driver.REQUIRED_COMPLETED_SERVICES
    assert "dagster-storage-bootstrap" not in driver.EXTERNAL_IMAGE_SERVICES


def test_alertmanager_is_bound_as_a_required_external_runtime_service() -> None:
    gate = _load_gate()
    runtime_driver_path = ROOT / "scripts" / "verify_production_path_runtime.py"
    driver_spec = importlib.util.spec_from_file_location(
        "production_path_runtime_alertmanager_contract", runtime_driver_path
    )
    assert driver_spec and driver_spec.loader
    driver = importlib.util.module_from_spec(driver_spec)
    driver_spec.loader.exec_module(driver)

    alertmanager_image = "prom/alertmanager:v0.28.1"
    assert "alertmanager" in gate.REQUIRED_RUNNING_SERVICES
    assert gate.EXPECTED_EXTERNAL_SERVICE_IMAGES["alertmanager"] == alertmanager_image
    assert "alertmanager" in driver.REQUIRED_RUNNING_SERVICES
    assert "alertmanager" in driver.EXTERNAL_IMAGE_SERVICES
    assert driver.PINNED_EXTERNAL_IMAGES["ALERTMANAGER_IMAGE"] == alertmanager_image


def test_release_gate_rejects_rehashed_extra_proof_fields() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    raw = evidence["raw_proofs"]
    assert isinstance(raw, dict)
    records = raw["records"]
    assert isinstance(records, dict)
    record = records["dagster_graphql"]
    assert isinstance(record, dict)
    facts = record["facts"]
    assert isinstance(facts, dict)
    facts["unreviewed_claim"] = True
    _rehash_raw_proof(gate, evidence, "dagster_graphql")

    errors = gate.validate_evidence(evidence, root=ROOT, expected_commit="a" * 40)

    assert any("dagster_graphql" in error and "fields" in error for error in errors)


def test_release_gate_rejects_rehashed_cross_proof_and_recovery_forgeries() -> None:
    gate = _load_gate()

    authority = _valid_evidence()
    authority_raw = authority["raw_proofs"]
    assert isinstance(authority_raw, dict)
    authority_records = authority_raw["records"]
    assert isinstance(authority_records, dict)
    authority_proof = authority_records["mysql_authority"]
    assert isinstance(authority_proof, dict)
    authority_facts = authority_proof["facts"]
    assert isinstance(authority_facts, dict)
    authority_facts["processed_outbox_count"] = 3
    _rehash_raw_proof(gate, authority, "mysql_authority")
    errors = gate.validate_evidence(authority, root=ROOT, expected_commit="a" * 40)
    assert any("processed outbox" in error for error in errors)

    recall = _valid_evidence()
    recall_raw = recall["raw_proofs"]
    assert isinstance(recall_raw, dict)
    recall_records = recall_raw["records"]
    assert isinstance(recall_records, dict)
    recall_proof = recall_records["qdrant_recall"]
    assert isinstance(recall_proof, dict)
    recall_facts = recall_proof["facts"]
    assert isinstance(recall_facts, dict)
    recall_facts["written_point_id"] = "forged-point"
    _rehash_raw_proof(gate, recall, "qdrant_recall")
    errors = gate.validate_evidence(recall, root=ROOT, expected_commit="a" * 40)
    assert any("cross-bound" in error for error in errors)

    duplicate_retry = _valid_evidence()
    duplicate_retry_facts = duplicate_retry["raw_proofs"]["records"][  # type: ignore[index]
        "dead_letter_retry"
    ]["facts"]
    assert isinstance(duplicate_retry_facts, dict)
    duplicate_retry_facts["retry_run_count"] = 2
    _rehash_raw_proof(gate, duplicate_retry, "dead_letter_retry")
    errors = gate.validate_evidence(
        duplicate_retry,
        root=ROOT,
        expected_commit="a" * 40,
    )
    assert any("exactly once" in error for error in errors)

    outage = _valid_evidence()
    outage_raw = outage["raw_proofs"]
    assert isinstance(outage_raw, dict)
    outage_records = outage_raw["records"]
    assert isinstance(outage_records, dict)
    outage_proof = outage_records["qdrant_outage"]
    assert isinstance(outage_proof, dict)
    outage_facts = outage_proof["facts"]
    assert isinstance(outage_facts, dict)
    outage_facts["failed_dependency_during"] = "redis"
    outage_facts["missing_required_during"] = ["redis"]
    _rehash_raw_proof(gate, outage, "qdrant_outage")
    errors = gate.validate_evidence(outage, root=ROOT, expected_commit="a" * 40)
    assert any("outage target" in error for error in errors)

    fencing = _valid_evidence()
    fencing_raw = fencing["raw_proofs"]
    assert isinstance(fencing_raw, dict)
    fencing_records = fencing_raw["records"]
    assert isinstance(fencing_records, dict)
    fencing_proof = fencing_records["duplicate_delivery"]
    assert isinstance(fencing_proof, dict)
    fencing_facts = fencing_proof["facts"]
    assert isinstance(fencing_facts, dict)
    fencing_facts["stale_owner_rejected"] = False
    _rehash_raw_proof(gate, fencing, "duplicate_delivery")
    errors = gate.validate_evidence(fencing, root=ROOT, expected_commit="a" * 40)
    assert any("stale lease owner" in error for error in errors)

    tempo = _valid_evidence()
    tempo_raw = tempo["raw_proofs"]
    assert isinstance(tempo_raw, dict)
    tempo_records = tempo_raw["records"]
    assert isinstance(tempo_records, dict)
    tempo_proof = tempo_records["tempo_trace"]
    assert isinstance(tempo_proof, dict)
    tempo_facts = tempo_proof["facts"]
    assert isinstance(tempo_facts, dict)
    operations = tempo_facts["operations"]
    assert isinstance(operations, dict)
    qdrant_operation = operations["qdrant"]
    assert isinstance(qdrant_operation, dict)
    components = qdrant_operation["components"]
    assert isinstance(components, list)
    components.remove("qdrant")
    signals = qdrant_operation["component_signals"]
    assert isinstance(signals, dict)
    signals.pop("qdrant")
    _rehash_raw_proof(gate, tempo, "tempo_trace")
    errors = gate.validate_evidence(tempo, root=ROOT, expected_commit="a" * 40)
    assert any("Tempo qdrant" in error for error in errors)


@pytest.mark.parametrize(
    ("proof_id", "field", "value", "message"),
    [
        (
            "dead_letter_retry",
            "retry_event_id",
            44,
            "event identities",
        ),
        (
            "dead_letter_retry",
            "source_attempt_ledger_sha256_after",
            "0" * 64,
            "attempt ledger",
        ),
        (
            "dead_letter_retry",
            "idempotency_record_count",
            2,
            "exactly once",
        ),
        (
            "dead_letter_retry_qdrant",
            "cross_tenant_count",
            1,
            "scope isolation",
        ),
    ],
)
def test_release_gate_rejects_rehashed_dead_letter_identity_forgeries(
    proof_id: str, field: str, value: object, message: str
) -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    facts = evidence["raw_proofs"]["records"][proof_id]["facts"]  # type: ignore[index]
    assert isinstance(facts, dict)
    facts[field] = value
    _rehash_raw_proof(gate, evidence, proof_id)

    errors = gate.validate_evidence(evidence, root=ROOT, expected_commit="a" * 40)

    assert any(message in error for error in errors)


def test_release_gate_rejects_rehashed_dead_letter_trace_without_qdrant_signal() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    facts = evidence["raw_proofs"]["records"]["dead_letter_retry_trace"][  # type: ignore[index]
        "facts"
    ]
    assert isinstance(facts, dict)
    components = facts["components"]
    signals = facts["component_signals"]
    assert isinstance(components, list) and isinstance(signals, dict)
    components.remove("qdrant")
    signals.pop("qdrant")
    _rehash_raw_proof(gate, evidence, "dead_letter_retry_trace")

    errors = gate.validate_evidence(evidence, root=ROOT, expected_commit="a" * 40)

    assert any("exact Qdrant operation chain" in error for error in errors)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bff_server_span_count", 2),
        ("bff_server_http_method", "GET"),
        ("bff_server_route", "/api/v1/task-runs/{id}/retries"),
    ],
)
def test_release_gate_rejects_dead_letter_trace_without_exact_bff_server_entry(
    field: str,
    value: object,
) -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    facts = evidence["raw_proofs"]["records"]["dead_letter_retry_trace"][  # type: ignore[index]
        "facts"
    ]
    assert isinstance(facts, dict)
    facts[field] = value
    _rehash_raw_proof(gate, evidence, "dead_letter_retry_trace")

    errors = gate.validate_evidence(evidence, root=ROOT, expected_commit="a" * 40)

    assert any("BFF retry server span" in error for error in errors)


def test_release_gate_requires_native_linux_and_closed_evidence_envelopes() -> None:
    gate = _load_gate()

    missing_host = _valid_evidence()
    compose = missing_host["compose"]
    assert isinstance(compose, dict)
    compose.pop("host_runtime")
    errors = gate.validate_evidence(missing_host, root=ROOT, expected_commit="a" * 40)
    assert any("native Linux host" in error for error in errors)

    extra_top_level = _valid_evidence()
    extra_top_level["approval_reference"] = "self-asserted"
    errors = gate.validate_evidence(extra_top_level, root=ROOT, expected_commit="a" * 40)
    assert any("top-level fields" in error for error in errors)

    extra_identity = _valid_evidence()
    identity = extra_identity["identity"]
    assert isinstance(identity, dict)
    identity["unreviewed_claim"] = True
    errors = gate.validate_evidence(extra_identity, root=ROOT, expected_commit="a" * 40)
    assert any("identity fields" in error for error in errors)

    extra_record = _valid_evidence()
    raw = extra_record["raw_proofs"]
    assert isinstance(raw, dict)
    records = raw["records"]
    assert isinstance(records, dict)
    record = records["dagster_graphql"]
    assert isinstance(record, dict)
    record["confidence"] = 1
    raw["bundle_sha256"] = gate._canonical_sha256(records)
    errors = gate.validate_evidence(extra_record, root=ROOT, expected_commit="a" * 40)
    assert any("dagster_graphql" in error and "record fields" in error for error in errors)

    extra_component = _valid_evidence()
    trace = extra_component["trace"]
    assert isinstance(trace, dict)
    linked = trace["linked_components"]
    assert isinstance(linked, list)
    linked.append("self_asserted_component")
    errors = gate.validate_evidence(extra_component, root=ROOT, expected_commit="a" * 40)
    assert any("linked components" in error for error in errors)

    duplicate_otel = _valid_evidence()
    duplicate_trace = duplicate_otel["trace"]
    assert isinstance(duplicate_trace, dict)
    operation_otel_ids = duplicate_trace["operation_otel_trace_ids"]
    assert isinstance(operation_otel_ids, dict)
    operation_otel_ids["qdrant"] = operation_otel_ids["dagster"]
    errors = gate.validate_evidence(duplicate_otel, root=ROOT, expected_commit="a" * 40)
    assert any("distinct operation OTel trace ids" in error for error in errors)


def test_evidence_validator_accepts_complete_shape_after_runtime_is_activated() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()

    activation_errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )
    assert activation_errors == []

    missing_runtime = copy.deepcopy(evidence)
    missing_runtime["compose"]["runtime"]["running_services"].pop("dagster-daemon")  # type: ignore[index]
    errors = gate.validate_evidence(missing_runtime, root=ROOT, expected_commit="a" * 40)
    assert any("running service inventory" in error for error in errors)

    unhealthy_runtime = copy.deepcopy(evidence)
    unhealthy_runtime["compose"]["runtime"]["running_services"]["redis"][  # type: ignore[index]
        "health"
    ] = "unhealthy"
    errors = gate.validate_evidence(unhealthy_runtime, root=ROOT, expected_commit="a" * 40)
    assert any("not healthy" in error for error in errors)

    unbound_image = copy.deepcopy(evidence)
    unbound_image["compose"]["runtime"]["running_services"]["redis"][  # type: ignore[index]
        "repo_digests"
    ] = ["attacker/redis@sha256:" + "f" * 64]
    errors = gate.validate_evidence(unbound_image, root=ROOT, expected_commit="a" * 40)
    assert any("repository digest" in error for error in errors)

    for mutation, expected in (
        (("identity", "dev_auth_enabled", True), "dev auth"),
        (("adapters", "qdrant", "embedding_provider", "deterministic_test"), "HTTP embedding"),
        (("adapters", "external_callback", "signature_verified", False), "signature"),
        (("observability", "collector_export_verified", False), "collector"),
        (("recovery", "worker_crash", "proven", False), "worker_crash"),
    ):
        forged = copy.deepcopy(evidence)
        *parents, field, value = mutation
        target = forged
        for parent in parents:
            target = target[parent]  # type: ignore[index,assignment]
        target[field] = value  # type: ignore[index]
        errors = gate.validate_evidence(forged, root=ROOT, expected_commit="a" * 40)
        assert any(expected in error for error in errors), errors

    forged_raw = copy.deepcopy(evidence)
    forged_raw["raw_proofs"]["records"]["tempo_trace"]["facts"]["services"] = []  # type: ignore[index]
    errors = gate.validate_evidence(forged_raw, root=ROOT, expected_commit="a" * 40)
    assert any("facts_sha256" in error or "bundle_sha256" in error for error in errors)

    sensitive_raw = copy.deepcopy(evidence)
    sensitive_record = sensitive_raw["raw_proofs"]["records"]["qdrant_point"]  # type: ignore[index]
    sensitive_record["facts"]["qdrant_api_key"] = "must-never-be-uploaded"  # type: ignore[index]
    sensitive_record["capture"]["observations"] = sensitive_record["facts"]  # type: ignore[index]
    sensitive_record["facts_sha256"] = gate._canonical_sha256(sensitive_record["facts"])
    sensitive_record["capture_sha256"] = gate._canonical_sha256(sensitive_record["capture"])
    sensitive_raw["raw_proofs"]["bundle_sha256"] = gate._canonical_sha256(  # type: ignore[index]
        sensitive_raw["raw_proofs"]["records"]  # type: ignore[index]
    )
    errors = gate.validate_evidence(sensitive_raw, root=ROOT, expected_commit="a" * 40)
    assert any("sensitive field" in error for error in errors)

    forged_capture = copy.deepcopy(evidence)
    forged_capture["raw_proofs"]["records"]["mysql_restart"]["capture"][  # type: ignore[index]
        "observations"
    ]["ready_status_after"] = 503
    errors = gate.validate_evidence(forged_capture, root=ROOT, expected_commit="a" * 40)
    assert any("capture_sha256" in error for error in errors)

    forged_source_binding = copy.deepcopy(evidence)
    forged_source_binding["runtime_sources"][  # type: ignore[index]
        "production/tests/production_gate_support.py"
    ] = "0" * 64
    errors = gate.validate_evidence(
        forged_source_binding,
        root=ROOT,
        expected_commit="a" * 40,
    )
    assert any("runtime source hash" in error for error in errors)

    boolean_only_recovery = copy.deepcopy(evidence)
    proof = boolean_only_recovery["raw_proofs"]["records"]["mysql_restart"]  # type: ignore[index]
    proof["facts"] = {"recovered": True}
    proof["capture"]["observations"] = proof["facts"]  # type: ignore[index]
    proof["facts_sha256"] = gate._canonical_sha256(proof["facts"])
    proof["capture_sha256"] = gate._canonical_sha256(proof["capture"])
    boolean_only_recovery["raw_proofs"]["bundle_sha256"] = gate._canonical_sha256(  # type: ignore[index]
        boolean_only_recovery["raw_proofs"]["records"]  # type: ignore[index]
    )
    errors = gate.validate_evidence(
        boolean_only_recovery,
        root=ROOT,
        expected_commit="a" * 40,
    )
    assert any("mysql_restart" in error and "capture facts" in error for error in errors)


def test_ready_name_and_driver_file_still_cannot_bypass_required_runtime_sources(
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
    assert any("runtime source is missing" in error for error in errors)


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
    assert any("linked components" in error for error in evidence_errors)
    assert any("BFF, Worker and Dagster" in error for error in evidence_errors)


def test_evidence_validator_rejects_extra_observability_services() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    evidence["observability"]["services"].append("unexpected-service")  # type: ignore[index,union-attr]

    errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )

    assert any("exactly BFF, Worker and Dagster" in error for error in errors)


def test_evidence_validator_rejects_extra_recovery_proof_references() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    evidence["recovery"]["mysql_restart"]["raw_proof_ids"].append(  # type: ignore[index,union-attr]
        "worker_crash"
    )

    errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )

    assert any("raw proof references are incomplete or unordered" in error for error in errors)


def test_evidence_validator_binds_container_architecture_to_native_host() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()
    evidence["compose"]["host_runtime"]["architecture"] = "arm64"  # type: ignore[index]

    errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )

    assert any("must match the native Linux host" in error for error in errors)


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


def test_preflight_cli_accepts_the_ready_runtime_contract() -> None:
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

    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["status"] == "ready"
    assert output["release_evidence"] is False


def test_evidence_cli_rejects_constructed_artifact_without_commit_bound_runtime_sources(
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
    assert any("runtime source is missing" in item for item in output["blockers"])


def test_finalizer_mandates_strict_production_path_runtime_evidence() -> None:
    documentation = (ROOT / "production" / "tests" / "production-path-gate.md").read_text(
        encoding="utf-8"
    )
    release = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
    finalizer = (ROOT / "scripts" / "finalize_release_evidence.py").read_text(encoding="utf-8")

    assert "列为强制 core evidence" in documentation
    assert "一次性运行证据" in documentation
    assert "严格复用同一运行证明校验器" in documentation
    assert '"production-path-gate.json"' in finalizer
    assert "validate_production_path_evidence" in finalizer
    assert release.index("bash scripts/verify_production_path.sh") < release.index(
        "scripts/finalize_release_evidence.py"
    )
