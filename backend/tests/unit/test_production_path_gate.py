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

        reconciled = client.reconcile_callback(payload)
        assert reconciled.status == "success", reconciled
        assert reconciled.details["reconciled"] is True
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


def _valid_evidence() -> dict[str, object]:
    trace_ids = {
        "oidc": "trace_production_path_oidc_001",
        "dagster": "trace_production_path_dagster_001",
        "object_storage": "trace_production_path_object_001",
        "qdrant": "trace_production_path_qdrant_001",
        "external_callback": "trace_production_path_callback_001",
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
                "authoritative_run_ids": ["task_run_gate", "knowledge_build_gate"],
                "authoritative_run_count": 2,
                "processed_outbox_count": 2,
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
                "otel_trace_id": "d" * 32,
                "services": [
                    "auris-flow-bff",
                    "auris-flow-worker",
                    "auris-flow-dagster-code",
                ],
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
                "authoritative_run_count_before": 2,
                "authoritative_run_count_after": 2,
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
            },
        ),
        "qdrant_outage": raw_proof(
            "qdrant_outage",
            "compose-runtime",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "point_id": "12345678-1234-5678-1234-567812345678",
                "point_present_after": True,
                "authoritative_run_count_before": 2,
                "authoritative_run_count_after": 2,
            },
        ),
        "redis_outage": raw_proof(
            "redis_outage",
            "compose-runtime",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "authoritative_run_count_before": 2,
                "authoritative_run_count_after": 2,
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
        return {
            "proven": True,
            "authority_consistent": True,
            "no_duplicate_business_outcome": True,
            "raw_proof_ids": [proof_id],
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
        "runtime_sources": {
            relative: (_sha256(ROOT / relative) if (ROOT / relative).is_file() else "e" * 64)
            for relative in (
                "scripts/verify_production_path_runtime.py",
                "production/tests/production_path_verifier.py",
                "production/tests/production_gate_support.py",
                "production/tests/production-path-keycloak-realm.template.json",
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
            "otel_trace_id": "d" * 32,
            "operation_trace_ids": trace_ids,
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
            "qdrant_outage": recovery("qdrant_outage"),
            "redis_outage": recovery("redis_outage"),
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


def test_callback_nonce_store_claim_is_atomic_and_replay_rejecting() -> None:
    support = _load_gate_support()
    nonce_store = support.AtomicNonceStore()

    assert nonce_store.claim(key_id="callback-v1", nonce="n" * 32, expires_at=2**31)
    assert not nonce_store.claim(key_id="callback-v1", nonce="n" * 32, expires_at=2**31)
    assert nonce_store.claim(key_id="callback-v2", nonce="n" * 32, expires_at=2**31)


def test_checked_in_gate_contract_remains_blocked_until_runtime_is_complete() -> None:
    gate = _load_gate()
    document = yaml.safe_load(GATE_COMPOSE.read_text(encoding="utf-8"))

    errors = gate.validate_gate_compose(document)

    assert any("contract status must be ready" in error for error in errors)
    assert any("real Compose diagnostic" in error for error in errors)
    assert any("raw recovery proofs" in error for error in errors)


def test_evidence_validator_rejects_even_complete_shape_until_runtime_is_activated() -> None:
    gate = _load_gate()
    evidence = _valid_evidence()

    activation_errors = gate.validate_evidence(
        evidence,
        root=ROOT,
        expected_commit="a" * 40,
    )
    assert any("contract status is not ready" in error for error in activation_errors)
    assert any("runtime source is missing" in error for error in activation_errors)
    assert any(
        "raw runtime proof binding is not implemented" in error for error in activation_errors
    )

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


def test_preflight_cli_reports_current_runtime_blockers() -> None:
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
    assert any("real Compose diagnostic" in item for item in output["blockers"])


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
    assert any("runtime source is missing" in item for item in output["blockers"])
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
