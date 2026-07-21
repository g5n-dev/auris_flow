from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load_verifier() -> ModuleType:
    path = ROOT / "production" / "tests" / "production_path_verifier.py"
    spec = importlib.util.spec_from_file_location("production_path_verifier_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase_and_dependency_protocol_is_exact() -> None:
    verifier = _load_verifier()

    assert verifier.PHASES == (
        "initial",
        "fault-prepare",
        "fault-during",
        "fault-verify",
        "finalize",
    )
    assert verifier.DEPENDENCIES == (
        "mysql_restart",
        "worker_crash",
        "duplicate_delivery",
        "callback_timeout",
        "qdrant_outage",
        "redis_outage",
    )
    verifier.validate_phase_dependency("initial", "none")
    verifier.validate_phase_dependency("finalize", "none")
    verifier.validate_phase_dependency("fault-prepare", "worker_crash")
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_phase_dependency("initial", "mysql_restart")
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_phase_dependency("fault-verify", "none")

    parsed = verifier.parse_args(
        [
            "--phase",
            "fault-verify",
            "--dependency",
            "worker_crash",
            "--artifact-dir",
            "/artifacts",
            "--run-suffix",
            "gate-1",
        ]
    )
    assert parsed.dependency == "worker_crash"


def test_capture_record_binds_exact_observations_and_canonical_hashes() -> None:
    verifier = _load_verifier()
    observations = {
        "http_status": 200,
        "issuer": "https://auris-production-gate.invalid/realms/auris-flow",
        "authorization_endpoint_scheme": "https",
        "token_endpoint_scheme": "https",
        "jwks_uri_scheme": "https",
    }

    record = verifier.capture_record("oidc_discovery", observations)

    assert record["source"] == "https-response"
    assert record["media_type"] == "application/json"
    assert record["facts"] == observations
    assert record["capture"]["observations"] == observations
    assert record["capture_sha256"] == verifier.canonical_sha256(record["capture"])
    assert record["facts_sha256"] == verifier.canonical_sha256(observations)
    with pytest.raises(verifier.VerifierFailure):
        verifier.capture_record("unknown-proof", observations)
    with pytest.raises(verifier.VerifierFailure):
        verifier.capture_record(
            "oidc_discovery",
            {**observations, "unexpected": True},
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"token": "opaque"},
        {"nested": {"cookie": "opaque"}},
        {"request_body": {"username": "operator"}},
        {"response_body": "hidden"},
        {"path": "/" + "Users/example/private/evidence.json"},
        {"path": "/" + "home/example/private/evidence.json"},
        {"password": "example"},
        {"key_material": "example"},
        {"qdrant_api_key": "example"},
        {"credential_value": "example"},
        {"request_headers": {"x-example": "value"}},
        {"private_key_value": "example"},
        {"access_key_value": "example"},
        {"signing_key_value": "example"},
        {"encryption_key_value": "example"},
    ],
)
def test_artifact_hygiene_rejects_credentials_bodies_and_personal_paths(
    payload: dict[str, object],
) -> None:
    verifier = _load_verifier()

    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_artifact_value(payload)


def test_artifact_hygiene_allows_contractual_hash_and_cookie_metadata() -> None:
    verifier = _load_verifier()
    payload = {
        "cookie_name": "__Host-auris_session",
        "cookie_secure": True,
        "cookie_http_only": True,
        "session_token_sha256": "a" * 64,
        "signature_key_id": "dagster-v1",
        "rsa_signing_key_ids": ["keycloak-v1"],
        "object_key": "tenants/t/projects/p/audio/object.json",
    }

    verifier.validate_artifact_value(payload)


def test_idempotent_json_write_reuses_equal_bytes_and_rejects_drift(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    target = tmp_path / "state-gate.json"
    payload = {"schema_version": "auris.production-path.state.v1", "run_suffix": "gate-1"}

    verifier.write_json_idempotent(target, payload)
    original = target.read_bytes()
    verifier.write_json_idempotent(target, payload)
    assert target.read_bytes() == original
    with pytest.raises(verifier.VerifierFailure):
        verifier.write_json_idempotent(target, {**payload, "run_suffix": "gate-2"})

    link = tmp_path / "linked.json"
    link.symlink_to(target)
    with pytest.raises(verifier.VerifierFailure):
        verifier.write_json_idempotent(link, payload)


def test_host_observation_is_exactly_scoped_and_contains_no_raw_container_id(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    target = tmp_path / "host-gate-mysql_restart.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": "auris.production-path.host-observation.v1",
                "dependency": "mysql_restart",
                "container_id_sha256": "b" * 64,
                "started_at_before": "2026-07-21T01:00:00Z",
                "started_at_after": "2026-07-21T01:01:00Z",
            }
        ),
        encoding="utf-8",
    )

    observed = verifier.load_host_observation(target, "mysql_restart")
    assert observed["container_id_sha256"] == "b" * 64
    target.write_text(
        json.dumps({**observed, "container_id": "raw-container-id"}), encoding="utf-8"
    )
    with pytest.raises(verifier.VerifierFailure):
        verifier.load_host_observation(target, "mysql_restart")


def test_traceparent_parser_extracts_only_exact_sampled_trace_id() -> None:
    verifier = _load_verifier()

    assert (
        verifier.otel_trace_id_from_carrier(
            {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
        )
        == "0123456789abcdef0123456789abcdef"
    )
    for malformed in (
        {},
        {"traceparent": "00-0123-0123456789abcdef-01"},
        {"traceparent": "00-00000000000000000000000000000000-0123456789abcdef-01"},
        {"traceparent": "00-0123456789abcdef0123456789abcdef-0000000000000000-01"},
        {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-00"},
    ):
        with pytest.raises(verifier.VerifierFailure):
            verifier.otel_trace_id_from_carrier(malformed)


def test_support_proof_envelope_is_strictly_unwrapped() -> None:
    verifier = _load_verifier()

    assert verifier.support_proof_data(
        {"status": "ok", "data": {"transport": "https", "request_count": 2}}
    ) == {"transport": "https", "request_count": 2}
    for malformed in (
        {"data": {"transport": "https"}},
        {"status": "ok", "data": []},
        {"status": "ok", "data": {}},
        {"status": "ok", "data": {"transport": "https"}, "extra": True},
    ):
        with pytest.raises(verifier.VerifierFailure):
            verifier.support_proof_data(malformed)


def test_dagster_run_count_requires_one_exact_remote_identity() -> None:
    verifier = _load_verifier()
    run_key = "outbox_v1_" + ("a" * 64)
    remote_id = "dagster-run-1"
    response = {
        "data": {
            "runsOrError": {
                "__typename": "Runs",
                "results": [
                    {
                        "runId": remote_id,
                        "status": "SUCCESS",
                        "tags": [
                            {
                                "key": "auris/dispatch_idempotency_key",
                                "value": run_key,
                            }
                        ],
                    }
                ],
            }
        }
    }

    assert verifier.validate_dagster_run_count(response, run_key, remote_id) == 1
    results = cast(
        list[dict[str, object]],
        response["data"]["runsOrError"]["results"],  # type: ignore[index]
    )
    results.append(
        {
            "runId": "dagster-run-2",
            "status": "SUCCESS",
            "tags": [{"key": "auris/dispatch_idempotency_key", "value": run_key}],
        }
    )
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_dagster_run_count(response, run_key, remote_id)


def test_real_dispatch_binding_requires_exact_scope_trace_and_provider() -> None:
    verifier = _load_verifier()
    tenant_id, project_id = verifier.SCOPE
    details = {
        "mode": "real",
        "provider": "minio",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "trace_id": "trace_production_gate_object",
        "run_id": "run-object-1",
    }

    verifier.validate_dispatch_binding(
        details,
        trace_id="trace_production_gate_object",
        run_id="run-object-1",
        provider="minio",
    )
    for changed in (
        {**details, "provider": "mock"},
        {**details, "tenant_id": "tenant_other"},
        {**details, "project_id": "project_other"},
        {**details, "trace_id": "trace_production_gate_other"},
    ):
        with pytest.raises(verifier.VerifierFailure):
            verifier.validate_dispatch_binding(
                changed,
                trace_id="trace_production_gate_object",
                run_id="run-object-1",
                provider="minio",
            )


def test_callback_dispatch_binding_requires_receiver_receipt_lineage() -> None:
    verifier = _load_verifier()
    tenant_id, project_id = verifier.SCOPE
    receipt_id = "callback_receipt_1234"
    trace_id = "trace_production_gate_callback"
    run_id = "run-callback-1"
    request_sha256 = "c" * 64
    details = {
        "mode": "real",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "trace_id": trace_id,
        "run_id": run_id,
        "callback_receipt_id": receipt_id,
        "request_sha256": request_sha256,
        "signature_mode": "hmac-sha256-v2",
        "protocol_receipt": {
            "callback_receipt_id": receipt_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "trace_id": trace_id,
            "run_id": run_id,
            "request_sha256": request_sha256,
            "signature_verified": True,
            "signature_mode": "hmac-sha256-v2",
        },
    }

    verifier.validate_callback_dispatch_binding(
        details,
        trace_id=trace_id,
        run_id=run_id,
        receipt_id=receipt_id,
    )
    invalid = {
        **details,
        "protocol_receipt": {
            **details["protocol_receipt"],  # type: ignore[dict-item]
            "trace_id": "trace_production_gate_other",
        },
    }
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_callback_dispatch_binding(
            invalid,
            trace_id=trace_id,
            run_id=run_id,
            receipt_id=receipt_id,
        )


def test_final_runtime_is_persisted_before_finalize_state_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _load_verifier()
    calls: list[tuple[str, Path]] = []

    class StubConfig:
        def runtime_path(self) -> Path:
            return Path("/artifacts/runtime-gate.json")

        def state_path(self) -> Path:
            return Path("/artifacts/state-gate.json")

    monkeypatch.setattr(
        verifier,
        "write_json_idempotent",
        lambda path, _payload: calls.append(("runtime", path)),
    )
    monkeypatch.setattr(
        verifier,
        "replace_json_state",
        lambda path, _prior, _state: calls.append(("state", path)),
    )

    verifier.persist_finalized_runtime(
        StubConfig(),
        prior={"completed_phases": []},
        state={"completed_phases": ["finalize"]},
        runtime={"identity": {}},
    )

    assert calls == [
        ("runtime", Path("/artifacts/runtime-gate.json")),
        ("state", Path("/artifacts/state-gate.json")),
    ]


def test_readiness_observation_proves_the_exact_failed_dependency() -> None:
    verifier = _load_verifier()
    payload = {
        "status": "degraded",
        "data": {
            "status": "failed",
            "checks": {
                "auth": "ok",
                "database": "ok",
                "redis": "ok",
                "object_storage": "ok",
                "qdrant": "not_ready",
                "dagster": "ok",
            },
            "required_checks": [
                "auth",
                "database",
                "redis",
                "object_storage",
                "qdrant",
                "dagster",
            ],
            "missing_required": {"qdrant": "not_ready"},
        },
    }

    observed = verifier.readiness_observation(
        503,
        payload,
        target_dependency="qdrant",
        expect_ready=False,
    )

    assert observed == {
        "http_status": 503,
        "envelope_status": "degraded",
        "data_status": "failed",
        "target_dependency": "qdrant",
        "target_status": "not_ready",
        "target_required": True,
        "missing_required": ["qdrant"],
    }
    wrong_dependency = json.loads(json.dumps(payload))
    wrong_dependency["data"]["checks"]["qdrant"] = "ok"
    wrong_dependency["data"]["checks"]["redis"] = "not_ready"
    wrong_dependency["data"]["missing_required"] = {"redis": "not_ready"}
    with pytest.raises(verifier.VerifierFailure):
        verifier.readiness_observation(
            503,
            wrong_dependency,
            target_dependency="qdrant",
            expect_ready=False,
        )


@pytest.mark.parametrize(
    ("dependency", "case_facts"),
    [
        (
            "mysql_restart",
            {
                "container_id_sha256": "a" * 64,
                "started_at_before": "2026-07-21T01:00:00Z",
                "started_at_after": "2026-07-21T01:01:00Z",
                "ready_status_after": 200,
            },
        ),
        (
            "worker_crash",
            {
                "container_id_sha256": "b" * 64,
                "started_at_before": "2026-07-21T01:00:00Z",
                "started_at_after": "2026-07-21T01:01:00Z",
                "event_id": 41,
                "event_status_before": "pending",
                "event_status_after": "processed",
                "remote_run_count": 1,
            },
        ),
        (
            "duplicate_delivery",
            {
                "event_id": 42,
                "delivery_attempt_count": 2,
                "dispatch_attempt_count": 1,
                "reconcile_attempt_count": 1,
                "remote_receipt_count": 1,
                "business_outcome_count": 1,
                "stale_owner_rejected": True,
                "new_owner_accepted": True,
                "lease_generation_before": 7,
                "lease_generation_after": 8,
                "claim_token_sha256_before": "a" * 64,
                "claim_token_sha256_after": "b" * 64,
            },
        ),
        (
            "callback_timeout",
            {
                "event_id": 43,
                "first_attempt_status": "outcome_unknown",
                "final_attempt_status": "success",
                "final_delivery_mode": "reconcile",
                "remote_receipt_count": 1,
            },
        ),
        (
            "qdrant_outage",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "failed_dependency_during": "qdrant",
                "failed_dependency_status_during": "not_ready",
                "missing_required_during": ["qdrant"],
                "recovered_dependency_status_after": "ok",
                "missing_required_after": [],
                "point_id": "point-1",
                "point_present_after": True,
            },
        ),
        (
            "redis_outage",
            {
                "ready_status_during": 503,
                "ready_status_after": 200,
                "failed_dependency_during": "redis",
                "failed_dependency_status_during": "not_ready",
                "missing_required_during": ["redis"],
                "recovered_dependency_status_after": "ok",
                "missing_required_after": [],
            },
        ),
    ],
)
def test_recovery_matrix_is_strictly_derived_from_each_capture(
    dependency: str,
    case_facts: dict[str, object],
) -> None:
    verifier = _load_verifier()
    facts: dict[str, Any] = {
        **case_facts,
        "authoritative_run_count_before": 4,
        "authoritative_run_count_after": 4,
    }

    assert verifier.recovery_case_from_facts(dependency, facts) == {
        "proven": True,
        "authority_consistent": True,
        "no_duplicate_business_outcome": True,
        "raw_proof_ids": [dependency],
    }
    facts.pop("authoritative_run_count_after")
    with pytest.raises(verifier.VerifierFailure):
        verifier.recovery_case_from_facts(dependency, facts)


def test_linked_components_require_tempo_derived_database_and_redis_spans() -> None:
    verifier = _load_verifier()
    trace_ids = {
        "oidc": "trace_production_gate_oidc",
        "dagster": "trace_production_gate_dagster",
        "object_storage": "trace_production_gate_object",
        "qdrant": "trace_production_gate_qdrant",
        "external_callback": "trace_production_gate_callback",
    }
    operation_otel_trace_ids = {
        "oidc": "a" * 32,
        "dagster": "b" * 32,
        "object_storage": "c" * 32,
        "qdrant": "d" * 32,
        "external_callback": "e" * 32,
    }

    def operation_facts(
        operation: str,
        *,
        services: list[str],
        signals: dict[str, list[str]],
    ) -> dict[str, object]:
        return {
            "otel_trace_id": operation_otel_trace_ids[operation],
            "services": services,
            "components": sorted(signals),
            "component_signals": signals,
            "span_count": 8,
            "client_span_count": 3,
        }

    operations = {
        "oidc": operation_facts(
            "oidc",
            services=["auris-flow-bff"],
            signals={
                "bff": ["service.name=auris-flow-bff"],
                "mysql": ["db.system=mysql"],
                "oidc": ["client.host=auris-production-gate.invalid"],
                "otel": ["tempo.trace"],
            },
        ),
        "dagster": operation_facts(
            "dagster",
            services=[
                "auris-flow-bff",
                "auris-flow-dagster-code",
                "auris-flow-worker",
            ],
            signals={
                "bff": ["service.name=auris-flow-bff"],
                "dagster": ["service.name=auris-flow-dagster-code"],
                "mysql": ["db.system=mysql"],
                "otel": ["tempo.trace"],
                "outbox": ["span.name=outbox.process"],
                "redis": ["db.system=redis"],
                "worker": ["service.name=auris-flow-worker"],
            },
        ),
        "object_storage": operation_facts(
            "object_storage",
            services=["auris-flow-bff", "auris-flow-worker"],
            signals={
                "bff": ["service.name=auris-flow-bff"],
                "mysql": ["db.system=mysql"],
                "object_storage": ["client.host=minio"],
                "otel": ["tempo.trace"],
                "outbox": ["span.name=outbox.process"],
                "worker": ["service.name=auris-flow-worker"],
            },
        ),
        "qdrant": operation_facts(
            "qdrant",
            services=["auris-flow-bff", "auris-flow-worker"],
            signals={
                "bff": ["service.name=auris-flow-bff"],
                "mysql": ["db.system=mysql"],
                "otel": ["tempo.trace"],
                "outbox": ["span.name=outbox.process"],
                "qdrant": ["client.host=qdrant"],
                "worker": ["service.name=auris-flow-worker"],
            },
        ),
        "external_callback": operation_facts(
            "external_callback",
            services=["auris-flow-bff", "auris-flow-worker"],
            signals={
                "bff": ["service.name=auris-flow-bff"],
                "external_callback": ["client.host=callback.production-gate.invalid"],
                "mysql": ["db.system=mysql"],
                "otel": ["tempo.trace"],
                "outbox": ["span.name=outbox.process"],
                "worker": ["service.name=auris-flow-worker"],
            },
        ),
    }
    facts: dict[str, Any] = {
        "oidc_code_exchange": {"trace_id": trace_ids["oidc"]},
        "browser_session": {"trace_id": trace_ids["oidc"]},
        "dagster_graphql": {"trace_id": trace_ids["dagster"]},
        "dagster_completion": {"run_trace_id": trace_ids["dagster"]},
        "minio_object": {"trace_id": trace_ids["object_storage"]},
        "qdrant_point": {
            "trace_id": trace_ids["qdrant"],
            "point_id": "point-written",
        },
        "qdrant_recall": {
            "trace_id": trace_ids["qdrant"],
            "point_ids": ["point-written"],
            "authorized_hit_count": 1,
            "written_point_id": "point-written",
            "written_point_occurrences": 1,
        },
        "callback_delivery": {"trace_id": trace_ids["external_callback"]},
        "tempo_trace": {
            "http_status": 200,
            "otel_trace_id": operation_otel_trace_ids["dagster"],
            "operation_otel_trace_ids": operation_otel_trace_ids,
            "operations": operations,
            "services": [
                "auris-flow-bff",
                "auris-flow-dagster-code",
                "auris-flow-worker",
            ],
            "components": [
                "bff",
                "dagster",
                "external_callback",
                "mysql",
                "object_storage",
                "oidc",
                "otel",
                "outbox",
                "qdrant",
                "redis",
                "worker",
            ],
        },
    }

    linked = verifier.linked_components_from_facts(
        facts,
        trace_ids=trace_ids,
        operation_otel_trace_ids=operation_otel_trace_ids,
        primary_otel_trace_id=operation_otel_trace_ids["dagster"],
    )

    assert linked == [
        "bff",
        "dagster",
        "external_callback",
        "mysql",
        "object_storage",
        "oidc",
        "otel",
        "outbox",
        "qdrant",
        "redis",
        "worker",
    ]
    facts["qdrant_recall"]["written_point_id"] = "point-other"
    with pytest.raises(verifier.VerifierFailure):
        verifier.linked_components_from_facts(
            facts,
            trace_ids=trace_ids,
            operation_otel_trace_ids=operation_otel_trace_ids,
            primary_otel_trace_id=operation_otel_trace_ids["dagster"],
        )

    isolated_facts = json.loads(json.dumps(facts))
    isolated_facts["qdrant_recall"]["written_point_id"] = "point-written"
    isolated_facts["tempo_trace"]["operations"]["qdrant"]["components"].remove("qdrant")
    isolated_facts["tempo_trace"]["operations"]["qdrant"]["component_signals"].pop("qdrant")
    with pytest.raises(verifier.VerifierFailure):
        verifier.linked_components_from_facts(
            isolated_facts,
            trace_ids=trace_ids,
            operation_otel_trace_ids=operation_otel_trace_ids,
            primary_otel_trace_id=operation_otel_trace_ids["dagster"],
        )


def test_authority_rejects_four_runs_with_only_three_processed_outbox_events() -> None:
    verifier = _load_verifier()
    run_ids = [f"run-{index}" for index in range(4)]

    class Database:
        def rows(self, sql: str, _parameters: dict[str, object]) -> list[dict[str, object]]:
            if "FROM run_records" in sql:
                return [{"run_id": run_id} for run_id in run_ids]
            assert "GROUP BY aggregate_id" in sql
            return [{"aggregate_id": run_id, "event_count": 1} for run_id in run_ids[:3]]

    class Config:
        database = Database()

    with pytest.raises(verifier.VerifierFailure):
        verifier._authoritative_counts(Config(), run_ids)


def test_qdrant_recall_must_uniquely_include_the_just_written_point() -> None:
    verifier = _load_verifier()
    hits = [
        {"point_id": "point-written", "trace_id": "trace_production_gate_qdrant"},
        {"point_id": "point-existing", "trace_id": "trace_production_gate_qdrant"},
    ]

    assert verifier.validate_qdrant_recall_binding("point-written", hits) == [
        "point-written",
        "point-existing",
    ]
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_qdrant_recall_binding("point-missing", hits)
    with pytest.raises(verifier.VerifierFailure):
        verifier.validate_qdrant_recall_binding(
            "point-written",
            [hits[0], hits[0]],
        )


def test_tempo_trace_components_come_from_client_spans_and_sanitized_attributes() -> None:
    verifier = _load_verifier()
    otel_trace_id = "d" * 32

    def attribute(key: str, value: str) -> dict[str, object]:
        return {"key": key, "value": {"stringValue": value}}

    def span(
        *attributes: dict[str, object],
        kind: str = "SPAN_KIND_CLIENT",
        name: str = "dependency.call",
    ) -> dict[str, object]:
        return {
            "traceId": otel_trace_id,
            "spanId": "e" * 16,
            "name": name,
            "kind": kind,
            "attributes": list(attributes),
        }

    def batch(service: str, spans: list[dict[str, object]]) -> dict[str, object]:
        return {
            "resource": {"attributes": [attribute("service.name", service)]},
            "scopeSpans": [{"spans": spans}],
        }

    payload = {
        "batches": [
            batch(
                "auris-flow-bff",
                [
                    span(attribute("db.system", "mysql")),
                ],
            ),
            batch(
                "auris-flow-worker",
                [
                    span(kind="SPAN_KIND_INTERNAL", name="outbox.process"),
                    span(
                        attribute(
                            "url.full",
                            "https://callback.production-gate.invalid/receipts/private",
                        )
                    ),
                ],
            ),
        ]
    }

    facts = verifier.tempo_trace_facts(
        payload,
        otel_trace_id,
        "external_callback",
    )

    assert facts["components"] == [
        "bff",
        "external_callback",
        "mysql",
        "otel",
        "outbox",
        "worker",
    ]
    assert facts["component_signals"]["mysql"] == ["db.system=mysql"]
    assert facts["component_signals"]["outbox"] == ["span.name=outbox.process"]
    assert facts["component_signals"]["external_callback"] == [
        "client.host=callback.production-gate.invalid"
    ]
    assert "receipts/private" not in json.dumps(facts)

    server_only = json.loads(json.dumps(payload))
    server_only["batches"][1]["scopeSpans"][0]["spans"][1]["kind"] = "SPAN_KIND_SERVER"
    with pytest.raises(verifier.VerifierFailure):
        verifier.tempo_trace_facts(
            server_only,
            otel_trace_id,
            "external_callback",
        )

    wrong_service = json.loads(json.dumps(payload))
    callback_span = wrong_service["batches"][1]["scopeSpans"][0]["spans"].pop()
    wrong_service["batches"][0]["scopeSpans"][0]["spans"].append(callback_span)
    with pytest.raises(verifier.VerifierFailure):
        verifier.tempo_trace_facts(
            wrong_service,
            otel_trace_id,
            "external_callback",
        )


def test_traceparent_uses_the_gate_trace_id_and_a_sampled_nonzero_parent() -> None:
    verifier = _load_verifier()
    otel_trace_id = "d" * 32

    traceparent = verifier.sampled_traceparent(otel_trace_id)

    version, trace_id, parent_id, flags = traceparent.split("-")
    assert (version, trace_id, flags) == ("00", otel_trace_id, "01")
    assert len(parent_id) == 16
    assert int(parent_id, 16) != 0


def test_fencing_observation_hashes_claim_tokens_and_rejects_stale_owner() -> None:
    verifier = _load_verifier()
    old = SimpleNamespace(
        event_id=51,
        claim_token="old-opaque-claim",
        lease_generation=7,
    )
    new = SimpleNamespace(
        event_id=51,
        claim_token="new-opaque-claim",
        lease_generation=8,
    )
    current_owner = SimpleNamespace(event_id=51)

    facts = verifier.fencing_observation_from_claims(
        old,
        new,
        stale_owner=None,
        current_owner=current_owner,
    )

    assert facts == {
        "stale_owner_rejected": True,
        "new_owner_accepted": True,
        "lease_generation_before": 7,
        "lease_generation_after": 8,
        "claim_token_sha256_before": hashlib.sha256(b"old-opaque-claim").hexdigest(),
        "claim_token_sha256_after": hashlib.sha256(b"new-opaque-claim").hexdigest(),
    }
    assert "opaque-claim" not in json.dumps(facts)
    verifier.validate_artifact_value(facts)
    with pytest.raises(verifier.VerifierFailure):
        verifier.fencing_observation_from_claims(
            old,
            SimpleNamespace(
                event_id=51,
                claim_token="another-claim",
                lease_generation=7,
            ),
            stale_owner=None,
            current_owner=current_owner,
        )


def test_runtime_fragment_has_exact_driver_owned_boundary() -> None:
    verifier = _load_verifier()
    sections = {
        "identity": {"provider": "oidc"},
        "adapters": {"dagster": {"mode": "real"}},
        "observability": {"otel_enabled": True},
        "trace": {"primary_business_trace_id": "trace_production_gate"},
        "raw_proofs": {"schema_version": "auris.production-path.raw-proofs.v1"},
        "recovery": {"mysql_restart": {"proven": True}},
    }

    assert verifier.runtime_fragment(sections) == sections
    with pytest.raises(verifier.VerifierFailure):
        verifier.runtime_fragment({**sections, "compose": {}})
