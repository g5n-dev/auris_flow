from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.urllib import URLLibInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    TraceFlags,
    TraceState,
    set_span_in_context,
)
from opentelemetry.util.types import AttributeValue

from auris_flow_dagster.contracts import AurisRunContext

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
_ACTIVE_PROVIDER: TracerProvider | None = None

ExporterFactory = Callable[..., SpanExporter]


def _safe_url(value: object) -> str:
    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or not parsed.hostname:
            return "[REDACTED]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path, "", ""))
    except (TypeError, ValueError):
        return "[REDACTED]"


def sanitize_attributes(
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
        if isinstance(value, str | bool | int | float):
            sanitized[key] = value
        elif isinstance(value, tuple | list) and all(
            isinstance(item, str | bool | int | float) for item in value
        ):
            sanitized[key] = tuple(value)
        else:
            sanitized[key] = "[REDACTED]"
    return sanitized


def _sanitize_event(event: Event) -> Event:
    attributes = sanitize_attributes(event.attributes)
    if event.name == "exception":
        attributes = {
            key: value
            for key, value in attributes.items()
            if key in {"exception.escaped", "exception.type"}
        }
    return Event(event.name, attributes=attributes, timestamp=event.timestamp)


def _sanitize_span(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=span.name.split("?", 1)[0][:128],
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=sanitize_attributes(span.attributes),
        events=tuple(_sanitize_event(event) for event in span.events),
        links=tuple(
            Link(link.context, attributes=sanitize_attributes(link.attributes))
            for link in span.links
        ),
        kind=span.kind,
        status=span.status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSpanExporter(SpanExporter):
    """Apply final-egress redaction and keep telemetry failure out of domain execution."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            return self._delegate.export(tuple(_sanitize_span(span) for span in spans))
        except Exception:  # noqa: BLE001 - telemetry cannot fail a Dagster run.
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:  # noqa: BLE001 - shutdown is best-effort.
            return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001 - telemetry cannot fail a Dagster run.
            return False


@dataclass
class DagsterObservabilityRuntime:
    enabled: bool
    provider: TracerProvider | None = None
    error_code: str | None = None


def _enabled(raw: str) -> bool:
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


def _parse_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw.replace("\n", ",").split(","):
        if not item.strip():
            continue
        key, separator, value = item.partition("=")
        if not separator or not _SAFE_HEADER_NAME.fullmatch(key.strip()) or not value.strip():
            raise ValueError("invalid OTLP header binding")
        headers[key.strip()] = value.strip()
    return headers


def _validated_endpoint(raw: str) -> str:
    parsed = urlsplit(raw.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid OTLP endpoint")
    if parsed.scheme == "http" and parsed.hostname not in {
        "otel-collector",
        "127.0.0.1",
        "localhost",
    }:
        raise ValueError("plaintext OTLP is limited to the internal collector")
    return raw.strip()


def configure_observability(
    *, exporter_factory: ExporterFactory = OTLPSpanExporter
) -> DagsterObservabilityRuntime:
    global _ACTIVE_PROVIDER
    if not _enabled(os.getenv("OTEL_ENABLED", "false")):
        return DagsterObservabilityRuntime(enabled=False)
    if _ACTIVE_PROVIDER is not None:
        return DagsterObservabilityRuntime(enabled=True, provider=_ACTIVE_PROVIDER)
    try:
        endpoint = _validated_endpoint(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""))
        sample_ratio = float(os.getenv("OTEL_TRACE_SAMPLE_RATIO", "0.1"))
        timeout_seconds = float(os.getenv("OTEL_EXPORT_TIMEOUT_SECONDS", "3"))
        if not 0 <= sample_ratio <= 1 or not 0 < timeout_seconds <= 30:
            raise ValueError("invalid OTel sampling or timeout")
        delegate = exporter_factory(
            endpoint=endpoint,
            headers=_parse_headers(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")) or None,
            timeout=timeout_seconds,
        )
        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "auris-flow-dagster-code"),
                    "deployment.environment.name": os.getenv("APP_ENV", "prod"),
                }
            ),
            sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
        )
        provider.add_span_processor(BatchSpanProcessor(SafeSpanExporter(delegate)))
        URLLibInstrumentor().instrument(tracer_provider=provider)
        _ACTIVE_PROVIDER = provider
        return DagsterObservabilityRuntime(enabled=True, provider=provider)
    except Exception:  # noqa: BLE001 - observability cannot prevent code-location startup.
        return DagsterObservabilityRuntime(
            enabled=False,
            error_code="OTEL_CONFIGURATION_FAILED",
        )


def remote_parent_context(scope: AurisRunContext) -> Context | None:
    if not (scope.otel_trace_id and scope.otel_parent_span_id and scope.otel_trace_flags):
        return None
    span_context = SpanContext(
        trace_id=int(scope.otel_trace_id, 16),
        span_id=int(scope.otel_parent_span_id, 16),
        is_remote=True,
        trace_flags=TraceFlags(int(scope.otel_trace_flags, 16)),
        trace_state=TraceState(),
    )
    return set_span_in_context(NonRecordingSpan(span_context))


@contextmanager
def domain_span(scope: AurisRunContext) -> Iterator[Span]:
    if _ACTIVE_PROVIDER is None:
        yield trace.get_current_span()
        return
    tracer = _ACTIVE_PROVIDER.get_tracer("auris-flow-dagster")
    with tracer.start_as_current_span(
        "auris_flow.domain.execute",
        context=remote_parent_context(scope),
        attributes=sanitize_attributes(
            {
                "auris.business_trace_id": scope.trace_id,
                "auris.tenant_id": scope.tenant_id,
                "auris.project_id": scope.project_id,
                "auris.run_id": scope.run_id,
            }
        ),
    ) as span:
        yield span
