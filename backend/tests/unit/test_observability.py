from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
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
from prometheus_client import CollectorRegistry
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.logging import LOGGER_NAME, get_logger, log_event
from app.core.metrics import AurisMetrics, is_metrics_client_allowed
from app.core.observability import (
    SafeSanitizingSpanExporter,
    _instrument_outbound_clients,
    annotate_current_span,
    configure_observability,
    current_trace_context,
    sanitize_span_attributes,
)
from app.main import app, settings


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
        }
    )

    serialized = json.dumps(attributes, sort_keys=True)
    assert "SELECT" not in serialized
    assert "canary" not in serialized
    assert "user:pass" not in serialized
    assert attributes["url.full"] == "https://example.test/callback"
    assert attributes["http.request.method"] == "POST"


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
                "reconcile_attempt_count INTEGER, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO outbox_events VALUES "
                "(1, 'pending', 0, 0, :oldest), "
                "(2, 'pending', 2, 0, :recent), "
                "(3, 'dead_letter', 5, 1, :recent)"
            ),
            {
                "oldest": (now - timedelta(seconds=90)).replace(tzinfo=None),
                "recent": (now - timedelta(seconds=10)).replace(tzinfo=None),
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
    assert 'route="/api/v1/data-assets/{data_asset_id}"' in payload
    assert "auris_outbox_pending 2.0" in payload
    assert "auris_outbox_dead_letter 1.0" in payload
    assert "auris_outbox_retry_pending 1.0" in payload
    assert "auris_outbox_oldest_pending_age_seconds 90.0" in payload
    assert 'auris_callback_outcomes_total{outcome="retry"} 1.0' in payload
    assert 'auris_worker_processing_total{outcome="success"} 1.0' in payload


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
