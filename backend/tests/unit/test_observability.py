from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)
from prometheus_client import CollectorRegistry
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.logging import LOGGER_NAME, get_logger, log_event
from app.core.metrics import AurisMetrics, is_metrics_client_allowed
from app.core.observability import (
    ObservabilityRuntime,
    SafeSanitizingSpanExporter,
    _instrument_outbound_clients,
    _public_boundary_sampler,
    annotate_current_span,
    configure_observability,
    configure_worker_observability,
    current_trace_carrier,
    current_trace_context,
    extract_remote_trace_context,
    internal_span,
    sanitize_span_attributes,
)
from app.main import app, settings
from app.models import OutboxEvent
from app.repositories import outbox_events as outbox_repository
from app.services.adapters import DispatchResult
from app.workers import outbox_worker


def test_observability_configuration_rejects_unsafe_endpoints_and_open_metrics_cidrs() -> None:
    with pytest.raises(ValidationError, match="OTEL_EXPORTER_OTLP_ENDPOINT"):
        Settings(
            _env_file=None,
            app_env="test",
            otel_enabled=True,
            otel_exporter_otlp_endpoint="https://user:pass@collector.test/v1/traces?token=x",
        )
    with pytest.raises(ValidationError, match="entire internet"):
        Settings(
            _env_file=None,
            app_env="test",
            metrics_enabled=True,
            metrics_trusted_cidrs="0.0.0.0/0",
        )
    with pytest.raises(ValidationError, match="PROMETHEUS_READINESS_URL"):
        Settings(
            _env_file=None,
            app_env="test",
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://otel-collector:4318/v1/traces",
            prometheus_readiness_url="https://user:pass@prometheus.example/ready?token=x",
        )
    with pytest.raises(ValidationError, match="OBSERVABILITY_HEALTH_URL"):
        Settings(
            _env_file=None,
            app_env="test",
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://otel-collector:4318/v1/traces",
            observability_health_url="https://user:pass@health.example/ready?token=x",
        )


def test_disabled_observability_does_not_construct_an_exporter() -> None:
    created = False

    def exporter_factory(*_args: object, **_kwargs: object) -> SpanExporter:
        nonlocal created
        created = True
        raise AssertionError("disabled observability must not create an exporter")

    runtime = configure_observability(
        FastAPI(),
        SimpleNamespace(otel_enabled=False),
        exporter_factory=exporter_factory,
    )

    assert runtime.enabled is False
    assert created is False


def _otel_settings(*, app_env: str) -> SimpleNamespace:
    return SimpleNamespace(
        app_env=app_env,
        app_name="auris-flow-test",
        otel_enabled=True,
        otel_exporter_otlp_endpoint="http://otel-collector:4318/v1/traces",
        otel_exporter_otlp_headers="",
        otel_service_name="auris-flow-test",
        otel_trace_sample_ratio=0.1,
        otel_export_timeout_seconds=3.0,
    )


def test_observability_initialization_failure_is_fail_closed_in_production(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.observability._INSTRUMENTED", False)

    def exporter_factory(*_args: object, **_kwargs: object) -> SpanExporter:
        raise RuntimeError("collector token=must-not-leak")

    with pytest.raises(RuntimeError, match="^OTEL_CONFIGURATION_FAILED$"):
        configure_observability(
            FastAPI(),
            _otel_settings(app_env="prod"),
            exporter_factory=exporter_factory,
        )


def test_observability_initialization_failure_remains_contained_outside_production(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.observability._INSTRUMENTED", False)

    def exporter_factory(*_args: object, **_kwargs: object) -> SpanExporter:
        raise RuntimeError("collector token=must-not-leak")

    runtime = configure_observability(
        FastAPI(),
        _otel_settings(app_env="test"),
        exporter_factory=exporter_factory,
    )

    assert runtime.enabled is False
    assert runtime.error_code == "OTEL_CONFIGURATION_FAILED"


def test_worker_observability_initialization_failure_is_fail_closed_in_production(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.core.observability._INSTRUMENTED", False)

    def exporter_factory(*_args: object, **_kwargs: object) -> SpanExporter:
        raise RuntimeError("collector token=must-not-leak")

    with pytest.raises(RuntimeError, match="^OTEL_CONFIGURATION_FAILED$"):
        configure_worker_observability(
            _otel_settings(app_env="release"),
            exporter_factory=exporter_factory,
        )


def test_all_outbound_http_transports_are_instrumented(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Instrumentor:
        def __init__(self, name: str) -> None:
            self.name = name

        def instrument(self, *, tracer_provider: object) -> None:
            calls.append((self.name, tracer_provider))

    provider = object()
    monkeypatch.setattr(
        "app.core.observability.RedisInstrumentor",
        lambda: Instrumentor("redis"),
    )
    monkeypatch.setattr(
        "app.core.observability.HTTPXClientInstrumentor",
        lambda: Instrumentor("httpx"),
    )
    monkeypatch.setattr(
        "app.core.observability.URLLibInstrumentor",
        lambda: Instrumentor("urllib"),
    )

    _instrument_outbound_clients(provider)  # type: ignore[arg-type]

    assert calls == [("redis", provider), ("httpx", provider), ("urllib", provider)]


def test_trace_context_is_attached_to_business_logs_and_span_attributes() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger(LOGGER_NAME)
    handler = CaptureHandler()
    root.addHandler(handler)
    try:
        with tracer.start_as_current_span("request"):
            annotate_current_span(
                business_trace_id="trace_business_001",
                request_id="request_001",
            )
            log_event(
                get_logger("observability-test"),
                "observability.test",
                trace_id="trace_business_001",
                request_id="request_001",
            )
            context = current_trace_context()
    finally:
        root.removeHandler(handler)
        provider.shutdown()

    payload = json.loads(records[-1].getMessage())
    span = exporter.get_finished_spans()[0]
    assert payload["otel_trace_id"] == context["otel_trace_id"]
    assert payload["otel_span_id"] == context["otel_span_id"]
    assert len(payload["otel_trace_id"]) == 32
    assert len(payload["otel_span_id"]) == 16
    assert span.attributes["auris.business_trace_id"] == "trace_business_001"
    assert span.attributes["auris.request_id"] == "request_001"


def test_server_trace_carrier_creates_a_worker_child_span(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("trace-propagation-test")
    monkeypatch.setattr("app.core.observability._ACTIVE_PROVIDER", provider)

    with tracer.start_as_current_span("bff.request") as request_span:
        carrier = current_trace_carrier()
        request_context = request_span.get_span_context()

    remote_parent = extract_remote_trace_context(carrier)
    with internal_span("outbox.adapter.dispatch", parent_context=remote_parent):
        pass

    provider.shutdown()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    worker_span = spans["outbox.adapter.dispatch"]
    traceparent = carrier["traceparent"].split("-")
    assert traceparent[:3] == [
        "00",
        f"{request_context.trace_id:032x}",
        f"{request_context.span_id:016x}",
    ]
    assert int(traceparent[3], 16) & 1 == 1
    assert worker_span.context.trace_id == request_context.trace_id
    assert worker_span.parent is not None
    assert worker_span.parent.span_id == request_context.span_id


def _claimed_projection_event(carrier: object, *, aggregate_id: str):
    with SessionLocal.begin() as session:
        session.add(
            OutboxEvent(
                tenant_id="aurora_auto",
                project_id="sales_qa",
                event_type="task_run.succeeded",
                aggregate_type="task_run",
                aggregate_id=aggregate_id,
                payload={
                    "trace_id": f"trace-{aggregate_id}",
                    "request_id": f"request-{aggregate_id}",
                    "otel_trace_context": carrier,
                },
                dispatch_idempotency_key=f"outbox-{aggregate_id}",
            )
        )
    with SessionLocal.begin() as session:
        claims = outbox_repository.claim_events(
            session,
            worker_id="worker-owned-trace-test",
            limit=1,
            lease_seconds=60,
            max_attempts_cap=3,
            aggregate_ids=[aggregate_id],
        )
        assert len(claims) == 1
        return claims[0]


def test_worker_process_span_uses_owned_durable_carrier_as_single_trace_parent(
    monkeypatch,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("worker-durable-trace-test")
    monkeypatch.setattr("app.core.observability._ACTIVE_PROVIDER", provider)

    with tracer.start_as_current_span("bff.request") as request_span:
        carrier = current_trace_carrier()
        request_context = request_span.get_span_context()
    claim = _claimed_projection_event(carrier, aggregate_id="task_trace_parent_001")

    monkeypatch.setattr(
        outbox_worker,
        "dispatch_event",
        lambda *_args: DispatchResult(adapter="projection", operation="publish"),
    )
    outbox_worker._process_claim(claim)

    provider.shutdown()
    spans = {span.name: span for span in exporter.get_finished_spans()}
    process_span = spans["outbox.process"]
    adapter_span = spans["outbox.adapter.dispatch"]
    assert process_span.context.trace_id == request_context.trace_id
    assert process_span.parent is not None
    assert process_span.parent.span_id == request_context.span_id
    assert adapter_span.context.trace_id == process_span.context.trace_id
    assert adapter_span.parent is not None
    assert adapter_span.parent.span_id == process_span.context.span_id


def test_durable_trace_carrier_lookup_is_owned_and_malformed_values_fail_closed() -> None:
    carrier = {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}
    claim = _claimed_projection_event(carrier, aggregate_id="task_trace_fence_001")

    with SessionLocal() as session:
        assert outbox_repository.owned_claim_trace_carrier(session, claim) == carrier
        assert (
            outbox_repository.owned_claim_trace_carrier(
                session,
                replace(claim, claim_token="forged-token"),
            )
            is None
        )
        assert (
            outbox_repository.owned_claim_trace_carrier(
                session,
                replace(claim, lease_generation=claim.lease_generation + 1),
            )
            is None
        )
        assert (
            outbox_repository.owned_claim_trace_carrier(
                session,
                replace(claim, claimed_by="different-worker"),
            )
            is None
        )

    with SessionLocal.begin() as session:
        event = session.get(OutboxEvent, claim.event_id)
        assert event is not None
        event.payload = {**event.payload, "otel_trace_context": {"traceparent": "attacker"}}
    assert outbox_worker._claim_parent_context(claim) is None


@pytest.mark.parametrize(
    "carrier",
    [
        None,
        {},
        {"traceparent": "attacker"},
        {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
        {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01", "secret": "x"},
    ],
)
def test_remote_trace_context_rejects_missing_malformed_or_unexpected_fields(carrier) -> None:
    assert extract_remote_trace_context(carrier) is None


def test_span_export_redaction_drops_sql_query_auth_cookie_and_secret_values() -> None:
    attributes = sanitize_span_attributes(
        {
            "db.statement": "SELECT * FROM users WHERE token='canary'",
            "url.full": "https://user:pass@example.test/callback?token=canary",
            "url.query": "token=canary",
            "http.request.header.authorization": "Bearer canary",
            "http.request.header.cookie": "session=canary",
            "api_secret": "canary-secret",
            "http.request.method": "POST",
            "http.route": "/api/v1/data-assets/{data_asset_id}",
            "http.target": "/api/v1/data-assets/customer-13800138000?token=canary",
            "url.path": "/private/customer-13800138000.wav",
            "client.address": "203.0.113.99",
            "client.port": 49152,
            "enduser.id": "user-sensitive-001",
            "user_agent.original": "AurisClient token=canary-user-agent",
        }
    )

    serialized = json.dumps(attributes, sort_keys=True)
    assert "SELECT" not in serialized
    assert "canary" not in serialized
    assert "user:pass" not in serialized
    assert attributes["url.full"] == "https://example.test"
    assert attributes["http.request.method"] == "POST"
    assert attributes["http.route"] == "/api/v1/data-assets/{data_asset_id}"


def test_final_exporter_removes_exception_description_and_raw_request_identity() -> None:
    delegate = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SafeSanitizingSpanExporter(delegate)))
    tracer = provider.get_tracer("egress-redaction-test")

    with pytest.raises(RuntimeError, match="must-not-leave"):
        with tracer.start_as_current_span(
            "http.request",
            attributes={
                "http.route": "/api/v1/data-assets/{data_asset_id}",
                "http.target": "/api/v1/data-assets/private-asset?token=must-not-leave",
                "client.address": "203.0.113.77",
                "user_agent.original": "AurisClient token=must-not-leave",
            },
        ):
            raise RuntimeError("must-not-leave token=secret SQL=SELECT path=/objects/private.wav")

    span = delegate.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description is None
    assert span.attributes == {
        "http.route": "/api/v1/data-assets/{data_asset_id}",
    }
    serialized = repr(span)
    assert "must-not-leave" not in serialized
    assert "203.0.113.77" not in serialized
    provider.shutdown()


def test_public_boundary_sampler_does_not_trust_remote_sampled_bit() -> None:
    sampler = _public_boundary_sampler(0.01, decision_key=b"k" * 32)
    decisions = []
    for flags in (TraceFlags.DEFAULT, TraceFlags.SAMPLED):
        remote_parent = SpanContext(
            trace_id=int("12" * 16, 16),
            span_id=int("34" * 8, 16),
            is_remote=True,
            trace_flags=TraceFlags(flags),
            trace_state=TraceState(),
        )
        decisions.append(
            sampler.should_sample(
                set_span_in_context(NonRecordingSpan(remote_parent)),
                1,
                "public.request",
                SpanKind.SERVER,
            ).decision
        )

    assert decisions[0] == decisions[1]
    assert decisions[0].is_sampled() is False
    marker_decision = sampler.should_sample(
        None,
        int("78" * 16, 16),
        "auris_flow.observability.pipeline.readiness",
        SpanKind.INTERNAL,
        {"auris.readiness.probe": True},
    ).decision
    assert marker_decision.is_sampled() is True


def test_public_boundary_sampling_uses_a_secret_key_not_caller_chosen_low_bits() -> None:
    trace_id = 1
    remote_parent = SpanContext(
        trace_id=trace_id,
        span_id=int("34" * 8, 16),
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState(),
    )
    parent_context = set_span_in_context(NonRecordingSpan(remote_parent))

    decision_a = (
        _public_boundary_sampler(
            0.5,
            decision_key=b"a" * 32,
        )
        .should_sample(
            parent_context,
            trace_id,
            "public.request",
            SpanKind.SERVER,
        )
        .decision
    )
    decision_b = (
        _public_boundary_sampler(
            0.5,
            decision_key=b"b" * 32,
        )
        .should_sample(
            parent_context,
            trace_id,
            "public.request",
            SpanKind.SERVER,
        )
        .decision
    )

    assert decision_a.is_sampled() is False
    assert decision_b.is_sampled() is True


def test_exporter_failure_is_contained() -> None:
    class BrokenExporter(SpanExporter):
        def export(self, spans: object) -> SpanExportResult:
            raise RuntimeError("collector unavailable: token=canary")

        def shutdown(self) -> None:
            return None

    exporter = SafeSanitizingSpanExporter(BrokenExporter())
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with provider.get_tracer("business").start_as_current_span("business.operation"):
        business_result = "success"

    assert business_result == "success"
    provider.shutdown()


@pytest.mark.parametrize(
    ("export_result", "expects_trace_id"),
    [
        (SpanExportResult.SUCCESS, True),
        (SpanExportResult.FAILURE, False),
    ],
)
def test_runtime_readiness_trace_requires_this_trace_to_export_successfully(
    export_result: SpanExportResult,
    expects_trace_id: bool,
) -> None:
    export_calls = 0

    class ResultExporter(SpanExporter):
        def export(self, _spans: object) -> SpanExportResult:
            nonlocal export_calls
            export_calls += 1
            return export_result

        def shutdown(self) -> None:
            return None

    safe_exporter = SafeSanitizingSpanExporter(ResultExporter())
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(safe_exporter))
    runtime = ObservabilityRuntime(
        enabled=True,
        provider=provider,
        exporter=safe_exporter,
    )

    trace_id = runtime.export_readiness_trace(timeout_millis=100)
    cached_trace_id = runtime.export_readiness_trace(timeout_millis=100)

    assert (trace_id is not None) is expects_trace_id
    assert cached_trace_id == trace_id
    assert export_calls == 1
    if trace_id is not None:
        assert len(trace_id) == 32
        assert int(trace_id, 16) > 0
    provider.shutdown()


def test_runtime_readiness_requires_the_exact_bff_marker_in_tempo(monkeypatch) -> None:
    now = [100.0]
    marker = ["a" * 32]
    visible: set[str] = set()
    runtime = ObservabilityRuntime(enabled=True)
    monkeypatch.setattr("app.core.observability.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        runtime,
        "export_readiness_trace",
        lambda *, timeout_millis: marker[0],
    )

    def lookup(trace_id: str) -> bool:
        return trace_id in visible

    assert (
        runtime.readiness_pipeline_is_live(
            timeout_millis=100,
            trace_visible=lookup,
        )
        is False
    )

    now[0] += 1.1
    visible.add(marker[0])
    assert runtime.readiness_pipeline_is_live(
        timeout_millis=100,
        trace_visible=lookup,
    )

    now[0] += 10.1
    marker[0] = "b" * 32
    assert runtime.readiness_pipeline_is_live(
        timeout_millis=100,
        trace_visible=lookup,
    ), "one proven marker may cover only the bounded propagation interval"

    now[0] += 10.1
    assert (
        runtime.readiness_pipeline_is_live(
            timeout_millis=100,
            trace_visible=lookup,
        )
        is False
    )


def test_runtime_readiness_probe_is_singleflight_and_lock_contention_fails_closed(
    monkeypatch,
) -> None:
    runtime = ObservabilityRuntime(enabled=True)
    lookup_started = Event()
    allow_lookup_to_finish = Event()
    lookup_calls = 0
    monkeypatch.setattr(
        runtime,
        "export_readiness_trace",
        lambda *, timeout_millis: "c" * 32,
    )

    def blocked_lookup(_trace_id: str) -> bool:
        nonlocal lookup_calls
        lookup_calls += 1
        lookup_started.set()
        assert allow_lookup_to_finish.wait(timeout=1.0)
        return False

    with ThreadPoolExecutor(max_workers=1) as executor:
        leader = executor.submit(
            runtime.readiness_pipeline_is_live,
            timeout_millis=100,
            trace_visible=blocked_lookup,
        )
        assert lookup_started.wait(timeout=1.0)
        assert (
            runtime.readiness_pipeline_is_live(
                timeout_millis=100,
                trace_visible=blocked_lookup,
            )
            is False
        )
        allow_lookup_to_finish.set()
        assert leader.result(timeout=1.0) is False

    assert lookup_calls == 1


def test_metrics_client_policy_uses_socket_peer_and_explicit_cidrs() -> None:
    assert is_metrics_client_allowed("127.0.0.1", "") is True
    assert is_metrics_client_allowed("172.20.0.4", "") is True
    assert is_metrics_client_allowed("203.0.113.9", "") is False
    assert is_metrics_client_allowed("203.0.113.9", "203.0.113.0/24") is True
    assert is_metrics_client_allowed("not-an-ip", "0.0.0.0/0") is False


def test_metrics_use_bounded_labels_and_refresh_outbox_and_pool_state() -> None:
    registry = CollectorRegistry()
    metrics = AurisMetrics(registry=registry)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE outbox_events ("
                "event_id INTEGER PRIMARY KEY, status VARCHAR(32), attempt_count INTEGER, "
                "reconcile_attempt_count INTEGER, event_type VARCHAR(128), created_at DATETIME, "
                "processed_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO outbox_events VALUES "
                "(1, 'pending', 0, 0, 'task_run.requested', :oldest, NULL), "
                "(2, 'pending', 2, 0, 'task_run.requested', :recent, NULL), "
                "(3, 'dead_letter', 5, 1, 'task_run.requested', :recent, :now), "
                "(4, 'processed', 1, 0, 'task_run.requested', :delivery_fast, :now), "
                "(5, 'processed', 1, 0, 'task_run.requested', :delivery_slow, :now)"
            ),
            {
                "oldest": (now - timedelta(seconds=90)).replace(tzinfo=None),
                "recent": (now - timedelta(seconds=10)).replace(tzinfo=None),
                "delivery_fast": (now - timedelta(seconds=30)).replace(tzinfo=None),
                "delivery_slow": (now - timedelta(seconds=90)).replace(tzinfo=None),
                "now": now.replace(tzinfo=None),
            },
        )
        connection.execute(
            text(
                "CREATE TABLE outbox_delivery_attempts ("
                "attempt_id INTEGER PRIMARY KEY, event_id INTEGER, status VARCHAR(64))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE run_records ("
                "run_id VARCHAR(128) PRIMARY KEY, run_type VARCHAR(64), status VARCHAR(32), "
                "created_at DATETIME, finished_at DATETIME, deadline_at DATETIME, "
                "next_status_sync_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO run_records VALUES "
                "('task_success', 'task_run', 'success', :success_created, "
                ":success_finished, NULL, NULL), "
                "('task_failed', 'task_run', 'failed', :failed_created, "
                ":failed_finished, NULL, NULL), "
                "('task_running', 'task_run', 'running', :active_created, NULL, "
                ":overdue, :overdue), "
                "('task_run_cancellation_auto_001', 'task_run_cancellation', "
                "'pending', :active_created, NULL, NULL, NULL), "
                "('task_run_status_sync_auto_001', 'task_run_status_sync', "
                "'success', :active_created, :success_finished, NULL, NULL)"
            ),
            {
                "success_created": (now - timedelta(seconds=1200)).replace(tzinfo=None),
                "success_finished": (now - timedelta(seconds=600)).replace(tzinfo=None),
                "failed_created": (now - timedelta(seconds=1800)).replace(tzinfo=None),
                "failed_finished": (now - timedelta(seconds=600)).replace(tzinfo=None),
                "active_created": (now - timedelta(seconds=3600)).replace(tzinfo=None),
                "overdue": (now - timedelta(seconds=10)).replace(tzinfo=None),
            },
        )

    sensitive_id = "asset_customer_13800138000"
    metrics.observe_http(
        method="GET",
        route="/api/v1/data-assets/{data_asset_id}",
        status_code=401,
        duration_seconds=0.125,
    )
    metrics.record_worker_processing("success")
    metrics.record_callback_outcome("retry")
    metrics.record_rate_limit_decision(allowed=True, backend="redis")
    metrics.record_rate_limit_decision(allowed=False, backend="redis-unavailable")
    metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    payload = metrics.render().decode("utf-8")

    assert sensitive_id not in payload
    assert "tenant_id" not in payload
    assert "project_id" not in payload
    assert "user_id" not in payload
    assert "run_id" not in payload
    assert "trace_id" not in payload
    assert 'route="/api/v1/data-assets/{data_asset_id}"' in payload
    assert "auris_outbox_pending 2.0" in payload
    assert "auris_outbox_dead_letter 1.0" in payload
    assert "auris_outbox_dead_letter_recent 1.0" in payload
    assert "auris_outbox_retry_pending 1.0" in payload
    assert "auris_outbox_oldest_pending_age_seconds 90.0" in payload
    assert "auris_outbox_delivery_duration_window_seconds_count 2.0" in payload
    assert "auris_outbox_delivery_duration_window_seconds_sum 120.0" in payload
    assert 'auris_outbox_delivery_duration_window_seconds_bucket{le="60"} 1.0' in payload
    assert 'auris_outbox_delivery_duration_window_seconds_bucket{le="120"} 2.0' in payload
    assert 'auris_callback_outcomes_total{outcome="retry"} 1.0' in payload
    assert 'auris_worker_processing_total{outcome="success"} 1.0' in payload
    assert 'auris_rate_limit_decisions_total{outcome="allowed"} 1.0' in payload
    assert 'auris_rate_limit_decisions_total{outcome="backend_unavailable"} 1.0' in payload
    assert 'auris_task_run_active{status="running"} 1.0' in payload
    assert 'auris_task_run_terminal{outcome="success"} 1.0' in payload
    assert 'auris_task_run_terminal{outcome="failed"} 1.0' in payload
    assert (
        'auris_task_run_duration_window_seconds_bucket{le="900",outcome="success"} 1.0' in payload
    )
    assert 'auris_task_run_duration_window_seconds_bucket{le="900",outcome="failed"} 0.0' in payload
    assert 'auris_task_run_duration_window_seconds_sum{outcome="failed"} 1200.0' in payload
    assert (
        'auris_task_run_monitor_actions{action="deadline_cancel",outcome="active"} 1.0' in payload
    )
    assert 'auris_task_run_monitor_actions{action="status_sync",outcome="success"} 1.0' in payload
    assert "auris_task_run_deadline_overdue 1.0" in payload
    assert "auris_task_run_status_sync_overdue 1.0" in payload


def _outcome_total(
    registry: CollectorRegistry,
    metric_name: str,
    outcome: str,
) -> float:
    return float(registry.get_sample_value(metric_name, {"outcome": outcome}) or 0.0)


def _create_operational_metrics_ledgers(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE outbox_events ("
                "event_id INTEGER PRIMARY KEY, status VARCHAR(32), attempt_count INTEGER, "
                "reconcile_attempt_count INTEGER, event_type VARCHAR(128), created_at DATETIME, "
                "processed_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE outbox_delivery_attempts ("
                "attempt_id INTEGER PRIMARY KEY, event_id INTEGER, status VARCHAR(64))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE run_records ("
                "run_id VARCHAR(128) PRIMARY KEY, run_type VARCHAR(64), status VARCHAR(32), "
                "created_at DATETIME, finished_at DATETIME, deadline_at DATETIME, "
                "next_status_sync_at DATETIME)"
            )
        )


def test_attempt_ledger_first_refresh_sets_baseline_then_counts_only_new_outcome() -> None:
    clock = [100.0]
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(
        registry=registry,
        monotonic_clock=lambda: clock[0],
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _create_operational_metrics_ledgers(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events VALUES "
                "(1, 'processed', 2, 0, 'external_callback.requested', :created, :created), "
                "(2, 'processed', 1, 0, 'task_run.requested', :created, :created)"
            ),
            {"created": now.replace(tzinfo=None)},
        )
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempts VALUES "
                "(1, 1, 'retry_scheduled'), (2, 1, 'succeeded'), (3, 2, 'succeeded')"
            )
        )

    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "retry") == 0
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "retry") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 0

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempts "
                "(attempt_id, event_id, status) VALUES (4, 1, 'dead_letter')"
            )
        )

    clock[0] = 109.999
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "dead_letter") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "dead_letter") == 0

    clock[0] = 110.0
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )

    assert _outcome_total(registry, "auris_callback_outcomes_total", "dead_letter") == 1
    assert _outcome_total(registry, "auris_worker_processing_total", "dead_letter") == 1
    assert _outcome_total(registry, "auris_callback_outcomes_total", "retry") == 0
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "retry") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 0


def test_recent_dead_letter_gauge_survives_process_restart_before_first_scrape() -> None:
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(registry=registry)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _create_operational_metrics_ledgers(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events VALUES "
                "(1, 'dead_letter', 5, 1, 'external_callback.requested', :created, :processed)"
            ),
            {
                "created": (now - timedelta(minutes=30)).replace(tzinfo=None),
                "processed": (now - timedelta(minutes=1)).replace(tzinfo=None),
            },
        )
        connection.execute(
            text("INSERT INTO outbox_delivery_attempts VALUES (1, 1, 'dead_letter')")
        )

    # This is a new metrics process: its reconstructed counters intentionally
    # baseline at zero, while the authority-backed recent gauge must still see
    # the dead letter which preceded the first scrape.
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )

    assert _outcome_total(registry, "auris_worker_processing_total", "dead_letter") == 0
    assert registry.get_sample_value("auris_outbox_dead_letter_recent") == 1


def test_attempt_ledger_retention_rebaselines_then_counts_new_growth() -> None:
    clock = [200.0]
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(
        registry=registry,
        monotonic_clock=lambda: clock[0],
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    _create_operational_metrics_ledgers(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_events VALUES "
                "(1, 'processed', 3, 0, 'external_callback.requested', :created, :created)"
            ),
            {"created": now.replace(tzinfo=None)},
        )
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempts VALUES "
                "(1, 1, 'succeeded'), (2, 1, 'succeeded'), (3, 1, 'succeeded')"
            )
        )

    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 0

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM outbox_delivery_attempts WHERE attempt_id IN (2, 3)"))
    clock[0] = 210.0
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 0
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 0

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempts VALUES "
                "(4, 1, 'succeeded'), (5, 1, 'succeeded')"
            )
        )
    clock[0] = 220.0
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 2
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 2

    clock[0] = 230.0
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )
    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 2
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 2

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO outbox_delivery_attempts "
                "(attempt_id, event_id, status) VALUES (6, 1, 'succeeded')"
            )
        )
    clock[0] = 240.0
    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
        now=now,
    )

    assert _outcome_total(registry, "auris_callback_outcomes_total", "success") == 3
    assert _outcome_total(registry, "auris_worker_processing_total", "success") == 3


def test_metrics_collection_reports_failure_when_outcome_ledger_cannot_be_read() -> None:
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(registry=registry)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE outbox_events ("
                "event_id INTEGER PRIMARY KEY, status VARCHAR(32), attempt_count INTEGER, "
                "reconcile_attempt_count INTEGER, event_type VARCHAR(128), created_at DATETIME)"
            )
        )

    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
    )
    payload = operational_metrics.render().decode("utf-8")

    assert "auris_metrics_collection_success 0.0" in payload


def test_metrics_collection_reports_failure_when_task_run_ledger_cannot_be_read() -> None:
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(registry=registry)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE outbox_events ("
                "event_id INTEGER PRIMARY KEY, status VARCHAR(32), attempt_count INTEGER, "
                "reconcile_attempt_count INTEGER, event_type VARCHAR(128), created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE outbox_delivery_attempts ("
                "attempt_id INTEGER PRIMARY KEY, event_id INTEGER, status VARCHAR(64))"
            )
        )

    operational_metrics.refresh_operational_metrics(
        session_factory=factory,
        engine=engine,
    )
    payload = operational_metrics.render().decode("utf-8")

    assert "auris_metrics_collection_success 0.0" in payload


def test_operational_metrics_refresh_is_singleflight_and_ttl_cached(monkeypatch) -> None:
    clock = [100.0]
    registry = CollectorRegistry()
    operational_metrics = AurisMetrics(
        registry=registry,
        monotonic_clock=lambda: clock[0],
    )
    workers = 8
    ready = Barrier(workers)
    refresh_started = Event()
    release_refresh = Event()
    calls_lock = Lock()
    collection_calls = 0

    def collect_snapshot(**_kwargs: object) -> bool:
        nonlocal collection_calls
        with calls_lock:
            collection_calls += 1
        refresh_started.set()
        assert release_refresh.wait(timeout=5)
        operational_metrics.callback_outcomes.labels("success").inc()
        operational_metrics.collection_success.set(1)
        return True

    monkeypatch.setattr(
        operational_metrics,
        "_collect_operational_metrics",
        collect_snapshot,
    )

    def scrape() -> bytes:
        ready.wait(timeout=5)
        operational_metrics.refresh_operational_metrics(
            session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            engine=object(),  # type: ignore[arg-type]
        )
        return operational_metrics.render()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(scrape) for _ in range(workers)]
        assert refresh_started.wait(timeout=5)
        release_refresh.set()
        payloads = [future.result(timeout=5) for future in futures]

    assert collection_calls == 1
    assert all(b'auris_callback_outcomes_total{outcome="success"} 1.0' in p for p in payloads)

    clock[0] = 109.999
    operational_metrics.refresh_operational_metrics(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        engine=object(),  # type: ignore[arg-type]
    )
    assert collection_calls == 1

    clock[0] = 110.0
    operational_metrics.refresh_operational_metrics(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        engine=object(),  # type: ignore[arg-type]
    )
    assert collection_calls == 2
    assert b'auris_callback_outcomes_total{outcome="success"} 2.0' in operational_metrics.render()


def test_operational_metrics_waiter_serves_stale_snapshot_when_refresher_hangs(
    monkeypatch,
) -> None:
    operational_metrics = AurisMetrics(registry=CollectorRegistry())
    refresh_started = Event()
    release_refresh = Event()
    collection_calls = 0

    def collect_snapshot(**_kwargs: object) -> bool:
        nonlocal collection_calls
        collection_calls += 1
        refresh_started.set()
        assert release_refresh.wait(timeout=5)
        return True

    monkeypatch.setattr(
        operational_metrics,
        "_collect_operational_metrics",
        collect_snapshot,
    )
    monkeypatch.setattr(
        "app.core.metrics._OPERATIONAL_SNAPSHOT_WAIT_SECONDS",
        0.02,
        raising=False,
    )

    def refresh() -> None:
        operational_metrics.refresh_operational_metrics(
            session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            engine=object(),  # type: ignore[arg-type]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(refresh)
        assert refresh_started.wait(timeout=5)
        waiter = executor.submit(refresh)
        try:
            waiter.result(timeout=0.25)
        finally:
            release_refresh.set()
        owner.result(timeout=5)

    assert collection_calls == 1


def test_failed_operational_metrics_refresh_is_short_ttl_cached(monkeypatch) -> None:
    clock = [200.0]
    operational_metrics = AurisMetrics(
        registry=CollectorRegistry(),
        monotonic_clock=lambda: clock[0],
    )
    collection_calls = 0

    def fail_collection(**_kwargs: object) -> bool:
        nonlocal collection_calls
        collection_calls += 1
        operational_metrics.collection_success.set(0)
        return False

    monkeypatch.setattr(
        operational_metrics,
        "_collect_operational_metrics",
        fail_collection,
    )

    for _ in range(6):
        operational_metrics.refresh_operational_metrics(
            session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            engine=object(),  # type: ignore[arg-type]
        )
    assert collection_calls == 1
    assert b"auris_metrics_collection_success 0.0" in operational_metrics.render()

    clock[0] = 204.999
    operational_metrics.refresh_operational_metrics(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        engine=object(),  # type: ignore[arg-type]
    )
    assert collection_calls == 1

    clock[0] = 205.0
    operational_metrics.refresh_operational_metrics(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        engine=object(),  # type: ignore[arg-type]
    )
    assert collection_calls == 2


def test_metrics_endpoint_is_disabled_and_denies_untrusted_peers(monkeypatch) -> None:
    monkeypatch.setattr(settings, "metrics_enabled", False)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/metrics").status_code == 404

    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_trusted_cidrs", "10.0.0.0/8")
    with TestClient(app, client=("203.0.113.9", 50000)) as client:
        response = client.get("/metrics", headers={"X-Forwarded-For": "10.0.0.2"})
    assert response.status_code == 403
    assert "auris_http_requests_total" not in response.text


def test_metrics_endpoint_allows_internal_peer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_trusted_cidrs", "")

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "auris_http_requests_total" in response.text
