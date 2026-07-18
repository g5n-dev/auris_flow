from __future__ import annotations

from collections.abc import Sequence

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from auris_flow_dagster import observability
from auris_flow_dagster.contracts import AurisRunContext
from auris_flow_dagster.observability import (
    SafeSpanExporter,
    configure_observability,
    domain_span,
    remote_parent_context,
    sanitize_attributes,
)


def _scope_with_parent() -> AurisRunContext:
    return AurisRunContext(
        tenant_id="aurora_auto",
        project_id="sales_qa",
        trace_id="trace-dagster-otel",
        run_id="run-dagster-otel",
        dispatch_idempotency_key="dispatch:dagster:otel",
        outbox_fencing_token="9:2",  # noqa: S106 - fencing epoch, not a credential.
        otel_trace_id="0123456789abcdef0123456789abcdef",
        otel_parent_span_id="0123456789abcdef",
        otel_trace_flags="01",
    )


def test_remote_parent_context_reconstructs_the_exact_w3c_parent() -> None:
    parent = remote_parent_context(_scope_with_parent())
    assert parent is not None
    span_context = trace.get_current_span(parent).get_span_context()
    assert trace.format_trace_id(span_context.trace_id) == "0123456789abcdef0123456789abcdef"
    assert trace.format_span_id(span_context.span_id) == "0123456789abcdef"
    assert int(span_context.trace_flags) == 1
    assert span_context.is_remote is True


def test_domain_span_is_a_child_of_the_remote_bff_span(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "_ACTIVE_PROVIDER", provider)

    with domain_span(_scope_with_parent()) as span:
        child_context = span.get_span_context()

    finished_span = exporter.get_finished_spans()[0]
    assert trace.format_trace_id(child_context.trace_id) == "0123456789abcdef0123456789abcdef"
    assert finished_span.parent is not None
    assert trace.format_span_id(finished_span.parent.span_id) == "0123456789abcdef"
    assert finished_span.parent.is_remote is True
    provider.shutdown()


def test_disabled_observability_never_constructs_an_exporter(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "false")
    created = False

    def exporter_factory(**_kwargs: object) -> SpanExporter:
        nonlocal created
        created = True
        raise AssertionError("disabled telemetry must not create an exporter")

    runtime = configure_observability(exporter_factory=exporter_factory)

    assert runtime.enabled is False
    assert created is False


def test_exporter_construction_failure_keeps_code_location_available(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector:4318/v1/traces",
    )

    def exporter_factory(**_kwargs: object) -> SpanExporter:
        raise RuntimeError("collector credential token=canary")

    runtime = configure_observability(exporter_factory=exporter_factory)

    assert runtime.enabled is False
    assert runtime.provider is None
    assert runtime.error_code == "OTEL_CONFIGURATION_FAILED"


def test_dagster_span_attributes_drop_secret_sql_and_url_query() -> None:
    attributes = sanitize_attributes(
        {
            "auris.business_trace_id": "trace-public",
            "db.statement": "SELECT secret FROM users",
            "http.request.header.authorization": "Bearer canary",
            "url.full": "https://user:pass@example.test/callback?token=canary",
        }
    )

    assert attributes == {
        "auris.business_trace_id": "trace-public",
        "url.full": "https://example.test/callback",
    }


def test_exporter_failure_is_contained() -> None:
    export_attempts = 0

    class BrokenExporter(SpanExporter):
        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            nonlocal export_attempts
            export_attempts += 1
            raise RuntimeError("collector unavailable: token=canary")

        def shutdown(self) -> None:
            return None

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(SafeSpanExporter(BrokenExporter())))

    with provider.get_tracer("test").start_as_current_span("domain.operation"):
        domain_result = "success"

    assert domain_result == "success"
    assert export_attempts == 1
    provider.shutdown()
