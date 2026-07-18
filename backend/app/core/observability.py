from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Link, Span
from opentelemetry.util.types import AttributeValue
from sqlalchemy.engine import Engine

from app.core.redaction import redact_structured_value

_SENSITIVE_ATTRIBUTE_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "db.statement",
    "db.query",
    "password",
    "private_key",
    "secret",
    "set-cookie",
    "sql",
    "token",
    "url.query",
)
_URL_ATTRIBUTE_KEYS = frozenset({"http.url", "url.full", "url.original"})
_SAFE_HEADER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_INSTRUMENTED = False
_ACTIVE_PROVIDER: TracerProvider | None = None


class ObservabilitySettings(Protocol):
    otel_enabled: bool
    otel_exporter_otlp_endpoint: str
    otel_exporter_otlp_headers: str
    otel_service_name: str
    otel_trace_sample_ratio: float
    otel_export_timeout_seconds: float
    app_env: str
    app_name: str


ExporterFactory = Callable[..., SpanExporter]


def _instrument_outbound_clients(provider: TracerProvider) -> None:
    """Instrument every transport used by BFF/Worker adapters.

    Most production adapters use ``urllib.request`` while newer integrations use
    HTTPX. Keeping both prevents Dagster, Qdrant, object-storage callbacks and the
    semantic embedding provider from disappearing from an otherwise complete trace.
    """

    RedisInstrumentor().instrument(tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    URLLibInstrumentor().instrument(tracer_provider=provider)


def _safe_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value))
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            return "[REDACTED]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{host}{port}", parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[REDACTED]"


def sanitize_span_attributes(
    attributes: Mapping[str, AttributeValue] | None,
) -> dict[str, AttributeValue]:
    if not attributes:
        return {}
    sanitized: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        normalized = key.casefold().replace("-", "_")
        if key in _URL_ATTRIBUTE_KEYS:
            sanitized[key] = _safe_url(value)
            continue
        if any(part.replace("-", "_") in normalized for part in _SENSITIVE_ATTRIBUTE_PARTS):
            continue
        redacted = redact_structured_value({key: value}, field_name="otel_attributes")
        safe_value = redacted.get(key) if isinstance(redacted, dict) else "[REDACTED]"
        if isinstance(safe_value, (str, bool, int, float)):
            sanitized[key] = safe_value
        elif isinstance(safe_value, (tuple, list)) and all(
            isinstance(item, (str, bool, int, float)) for item in safe_value
        ):
            sanitized[key] = cast(AttributeValue, tuple(safe_value))
        else:
            sanitized[key] = "[REDACTED]"
    return sanitized


def _sanitize_event(event: Event) -> Event:
    attributes = sanitize_span_attributes(event.attributes)
    if event.name == "exception":
        attributes = {
            key: value
            for key, value in attributes.items()
            if key in {"exception.escaped", "exception.type"}
        }
    return Event(event.name, attributes=attributes, timestamp=event.timestamp)


def _sanitize_link(link: Link) -> Link:
    return Link(link.context, attributes=sanitize_span_attributes(link.attributes))


def _sanitize_span(span: ReadableSpan) -> ReadableSpan:
    name = span.name.split("?", 1)[0][:128]
    return ReadableSpan(
        name=name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=sanitize_span_attributes(span.attributes),
        events=tuple(_sanitize_event(event) for event in span.events),
        links=tuple(_sanitize_link(link) for link in span.links),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSanitizingSpanExporter(SpanExporter):
    """Redact telemetry at the final egress boundary and contain collector failures."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._delegate.export(tuple(_sanitize_span(span) for span in spans))
        except Exception:  # noqa: BLE001 - telemetry cannot make domain traffic fail.
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:  # noqa: BLE001 - telemetry shutdown is best-effort.
            return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001 - telemetry cannot make domain traffic fail.
            return False


@dataclass
class ObservabilityRuntime:
    enabled: bool
    provider: TracerProvider | None = None
    error_code: str | None = None

    def shutdown(self) -> None:
        if self.provider is None:
            return
        try:
            self.provider.shutdown()
        except Exception:  # noqa: BLE001 - application shutdown must continue.
            return None


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.replace("\n", ",").split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        if not separator or not _SAFE_HEADER_NAME.fullmatch(key.strip()) or not value.strip():
            raise ValueError("invalid OTLP header binding")
        headers[key.strip()] = value.strip()
    return headers


def configure_observability(
    app: FastAPI,
    settings: ObservabilitySettings | Any,
    *,
    engine: Engine | None = None,
    exporter_factory: ExporterFactory = OTLPSpanExporter,
) -> ObservabilityRuntime:
    """Configure tracing without creating exporter threads or clients when disabled."""

    global _ACTIVE_PROVIDER, _INSTRUMENTED
    if not bool(getattr(settings, "otel_enabled", False)):
        return ObservabilityRuntime(enabled=False)
    if _INSTRUMENTED:
        existing = getattr(app.state, "observability", None)
        if isinstance(existing, ObservabilityRuntime):
            return existing
        return ObservabilityRuntime(enabled=False, error_code="OTEL_ALREADY_INSTRUMENTED")
    try:
        endpoint = str(settings.otel_exporter_otlp_endpoint).strip()
        headers = _parse_otlp_headers(str(settings.otel_exporter_otlp_headers))
        delegate = exporter_factory(
            endpoint=endpoint,
            headers=headers or None,
            timeout=float(settings.otel_export_timeout_seconds),
        )
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": str(settings.otel_service_name or settings.app_name),
                    "deployment.environment.name": str(settings.app_env),
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(float(settings.otel_trace_sample_ratio))),
        )
        provider.add_span_processor(BatchSpanProcessor(SafeSanitizingSpanExporter(delegate)))

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="^/healthz$,^/metrics$",
            http_capture_headers_server_request=[],
            http_capture_headers_server_response=[],
            exclude_spans=["receive", "send"],
        )
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(
                engine=engine,
                tracer_provider=provider,
                enable_commenter=False,
            )
        _instrument_outbound_clients(provider)
        _INSTRUMENTED = True
        _ACTIVE_PROVIDER = provider
        runtime = ObservabilityRuntime(enabled=True, provider=provider)
        app.state.observability = runtime
        return runtime
    except Exception:  # noqa: BLE001 - observability cannot prevent API startup.
        return ObservabilityRuntime(enabled=False, error_code="OTEL_CONFIGURATION_FAILED")


def configure_worker_observability(
    settings: ObservabilitySettings | Any,
    *,
    engine: Engine | None = None,
    exporter_factory: ExporterFactory = OTLPSpanExporter,
) -> ObservabilityRuntime:
    """Configure the same safe exporter and client instrumentation in a worker process."""

    global _ACTIVE_PROVIDER, _INSTRUMENTED
    if not bool(getattr(settings, "otel_enabled", False)):
        return ObservabilityRuntime(enabled=False)
    if _INSTRUMENTED:
        return ObservabilityRuntime(enabled=False, error_code="OTEL_ALREADY_INSTRUMENTED")
    try:
        delegate = exporter_factory(
            endpoint=str(settings.otel_exporter_otlp_endpoint).strip(),
            headers=_parse_otlp_headers(str(settings.otel_exporter_otlp_headers)) or None,
            timeout=float(settings.otel_export_timeout_seconds),
        )
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": str(settings.otel_service_name or settings.app_name),
                    "deployment.environment.name": str(settings.app_env),
                    "service.instance.role": "worker",
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(float(settings.otel_trace_sample_ratio))),
        )
        provider.add_span_processor(BatchSpanProcessor(SafeSanitizingSpanExporter(delegate)))
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(
                engine=engine,
                tracer_provider=provider,
                enable_commenter=False,
            )
        _instrument_outbound_clients(provider)
        _INSTRUMENTED = True
        _ACTIVE_PROVIDER = provider
        return ObservabilityRuntime(enabled=True, provider=provider)
    except Exception:  # noqa: BLE001 - telemetry cannot prevent worker startup.
        return ObservabilityRuntime(enabled=False, error_code="OTEL_CONFIGURATION_FAILED")


@contextmanager
def internal_span(
    name: str,
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
) -> Iterator[Span]:
    if _ACTIVE_PROVIDER is None:
        yield trace.get_current_span()
        return
    tracer = _ACTIVE_PROVIDER.get_tracer("auris-flow")
    with tracer.start_as_current_span(
        name, attributes=sanitize_span_attributes(attributes)
    ) as span:
        yield span


def annotate_current_span(*, business_trace_id: str, request_id: str) -> None:
    span = trace.get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("auris.business_trace_id", business_trace_id)
    span.set_attribute("auris.request_id", request_id)


def current_trace_context() -> dict[str, str]:
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "otel_trace_id": trace.format_trace_id(context.trace_id),
        "otel_span_id": trace.format_span_id(context.span_id),
        "otel_trace_flags": f"{int(context.trace_flags):02x}",
    }
