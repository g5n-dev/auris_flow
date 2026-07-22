from __future__ import annotations

import hmac
import re
import secrets
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
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
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import (
    Link,
    NonRecordingSpan,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
    TraceState,
    set_span_in_context,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.util.types import AttributeValue
from sqlalchemy.engine import Engine

from app.core.redaction import redact_structured_value
from app.core.secrets import is_production_environment

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
_RAW_REQUEST_ATTRIBUTE_KEYS = frozenset(
    {
        "client.address",
        "client.port",
        "enduser.id",
        "http.client_ip",
        "http.target",
        "http.user_agent",
        "net.peer.ip",
        "net.peer.port",
        "network.peer.address",
        "network.peer.port",
        "url.path",
        "user.id",
        "user_agent.original",
    }
)
_SAFE_HEADER_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_INSTRUMENTED = False
_ACTIVE_PROVIDER: TracerProvider | None = None
_ACTIVE_EXPORTER: SafeSanitizingSpanExporter | None = None
_READINESS_MARKER_TTL_SECONDS = 10.0
_READINESS_PROPAGATION_GRACE_SECONDS = 10.0
_READINESS_LAST_SUCCESS_TTL_SECONDS = 20.0


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
        return urlunsplit((parsed.scheme, f"{host}{port}", "", "", ""))
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
        if key.casefold() in _RAW_REQUEST_ATTRIBUTE_KEYS:
            continue
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
        # SDK auto-instrumentation may copy exception messages into
        # ``Status.description``. Preserve only the bounded status code at the final
        # egress boundary so tokens, SQL values and object paths cannot bypass event
        # and attribute redaction.
        status=Status(span.status.status_code),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class SafeSanitizingSpanExporter(SpanExporter):
    """Redact telemetry at the final egress boundary and contain collector failures."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate
        self._successful_trace_ids: deque[int] = deque(maxlen=128)
        self._trace_lock = Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            sanitized_spans = tuple(_sanitize_span(span) for span in spans)
            result = self._delegate.export(sanitized_spans)
            if result == SpanExportResult.SUCCESS:
                with self._trace_lock:
                    self._successful_trace_ids.extend(
                        span.context.trace_id
                        for span in sanitized_spans
                        if span.context is not None and span.context.is_valid
                    )
            return result
        except Exception:  # noqa: BLE001 - telemetry cannot make domain traffic fail.
            return SpanExportResult.FAILURE

    def exported_trace_successfully(self, trace_id: int) -> bool:
        with self._trace_lock:
            return trace_id in self._successful_trace_ids

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
    exporter: SafeSanitizingSpanExporter | None = None
    error_code: str | None = None
    _readiness_probe_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _readiness_trace_id: str | None = field(default=None, init=False, repr=False)
    _readiness_probe_expires_at: float = field(default=0.0, init=False, repr=False)
    _pipeline_probe_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _pipeline_pending_trace_id: str | None = field(default=None, init=False, repr=False)
    _pipeline_pending_since: float = field(default=0.0, init=False, repr=False)
    _pipeline_last_success_at: float = field(default=0.0, init=False, repr=False)
    _pipeline_cached_ready: bool = field(default=False, init=False, repr=False)
    _pipeline_next_probe_at: float = field(default=0.0, init=False, repr=False)

    def export_readiness_trace(self, *, timeout_millis: int) -> str | None:
        """Export one forced-sampled marker and prove this exact trace was accepted."""

        if not self.enabled or self.provider is None or self.exporter is None:
            return None
        now = time.monotonic()
        if now < self._readiness_probe_expires_at:
            return self._readiness_trace_id
        if not self._readiness_probe_lock.acquire(blocking=False):
            if now < self._readiness_probe_expires_at + 5.0:
                return self._readiness_trace_id
            return None
        try:
            now = time.monotonic()
            if now < self._readiness_probe_expires_at:
                return self._readiness_trace_id
            trace_id = self._export_readiness_trace_uncached(timeout_millis=timeout_millis)
            self._readiness_trace_id = trace_id
            self._readiness_probe_expires_at = now + (
                _READINESS_MARKER_TTL_SECONDS if trace_id else 1.0
            )
            return trace_id
        finally:
            self._readiness_probe_lock.release()

    def _export_readiness_trace_uncached(self, *, timeout_millis: int) -> str | None:
        assert self.provider is not None
        assert self.exporter is not None
        trace_id = secrets.randbits(128) or 1
        span_id = secrets.randbits(64) or 1
        parent = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        parent_context = set_span_in_context(NonRecordingSpan(parent))
        try:
            tracer = self.provider.get_tracer("auris-flow.readiness")
            with tracer.start_as_current_span(
                "auris_flow.observability.pipeline.readiness",
                context=parent_context,
                attributes={"auris.readiness.probe": True},
                record_exception=False,
                set_status_on_exception=False,
            ):
                pass
            if not self.provider.force_flush(timeout_millis=timeout_millis):
                return None
        except Exception:  # noqa: BLE001 - readiness reports only a stable state.
            return None
        if not self.exporter.exported_trace_successfully(trace_id):
            return None
        return trace.format_trace_id(trace_id)

    def readiness_pipeline_is_live(
        self,
        *,
        timeout_millis: int,
        trace_visible: Callable[[str], bool],
    ) -> bool:
        """Prove that one exact BFF marker reached Tempo without probe fan-out.

        A successful observation is cached briefly. When the exporter rotates its
        marker, the previous success may cover only the bounded propagation window;
        a marker that remains invisible for more than ten seconds fails closed.
        Concurrent readiness requests never create parallel Collector/Tempo probes.
        """

        now = time.monotonic()
        if now < self._pipeline_next_probe_at:
            return self._pipeline_cached_ready
        if not self._pipeline_probe_lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if now < self._pipeline_next_probe_at:
                return self._pipeline_cached_ready
            trace_id = self.export_readiness_trace(timeout_millis=timeout_millis)
            if trace_id is None:
                self._pipeline_cached_ready = False
                self._pipeline_next_probe_at = now + 1.0
                return False
            if trace_id != self._pipeline_pending_trace_id:
                self._pipeline_pending_trace_id = trace_id
                self._pipeline_pending_since = now
            try:
                visible = bool(trace_visible(trace_id))
            except Exception:  # noqa: BLE001 - readiness emits only a stable result.
                visible = False
            observed_at = time.monotonic()
            if visible:
                self._pipeline_last_success_at = observed_at
                self._pipeline_cached_ready = True
                self._pipeline_next_probe_at = observed_at + 5.0
                return True

            pending_age = max(0.0, observed_at - self._pipeline_pending_since)
            previous_success_is_fresh = (
                self._pipeline_last_success_at > 0
                and observed_at - self._pipeline_last_success_at
                <= _READINESS_LAST_SUCCESS_TTL_SECONDS
            )
            self._pipeline_cached_ready = (
                pending_age <= _READINESS_PROPAGATION_GRACE_SECONDS and previous_success_is_fresh
            )
            self._pipeline_next_probe_at = observed_at + 1.0
            return self._pipeline_cached_ready
        finally:
            self._pipeline_probe_lock.release()

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


class _PublicBoundarySampler(Sampler):
    """Keep public trace correlation while refusing caller-controlled sampling."""

    def __init__(self, sample_ratio: float, *, decision_key: bytes) -> None:
        ratio_sampler = TraceIdRatioBased(sample_ratio)
        remote_sampler = _KeyedTraceIdRatioSampler(
            sample_ratio,
            decision_key=decision_key,
        )
        self._delegate = ParentBased(
            ratio_sampler,
            remote_parent_sampled=remote_sampler,
            remote_parent_not_sampled=remote_sampler,
        )

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if (
            name == "auris_flow.observability.pipeline.readiness"
            and attributes is not None
            and attributes.get("auris.readiness.probe") is True
        ):
            return SamplingResult(Decision.RECORD_AND_SAMPLE, trace_state=trace_state)
        return self._delegate.should_sample(
            parent_context,
            trace_id,
            name,
            kind,
            attributes,
            links,
            trace_state,
        )

    def get_description(self) -> str:
        return "AurisPublicBoundarySampler"


class _KeyedTraceIdRatioSampler(Sampler):
    """Make remote sampling unpredictable when the caller chooses the trace ID."""

    def __init__(self, sample_ratio: float, *, decision_key: bytes) -> None:
        if not 0.0 <= sample_ratio <= 1.0 or len(decision_key) < 32:
            raise ValueError("invalid keyed sampler configuration")
        self._threshold = int(sample_ratio * (1 << 64))
        self._decision_key = decision_key

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        del parent_context, name, kind, attributes, links
        digest = hmac.digest(
            self._decision_key,
            trace_id.to_bytes(16, byteorder="big", signed=False),
            "sha256",
        )
        decision_value = int.from_bytes(digest[:8], byteorder="big", signed=False)
        decision = Decision.RECORD_AND_SAMPLE if decision_value < self._threshold else Decision.DROP
        return SamplingResult(decision, trace_state=trace_state)

    def get_description(self) -> str:
        return "AurisKeyedTraceIdRatioSampler"


def _public_boundary_sampler(
    sample_ratio: float,
    *,
    decision_key: bytes | None = None,
) -> Sampler:
    return _PublicBoundarySampler(
        sample_ratio,
        decision_key=(decision_key if decision_key is not None else secrets.token_bytes(32)),
    )


def configure_observability(
    app: FastAPI,
    settings: ObservabilitySettings | Any,
    *,
    engine: Engine | None = None,
    exporter_factory: ExporterFactory = OTLPSpanExporter,
) -> ObservabilityRuntime:
    """Configure tracing without creating exporter threads or clients when disabled."""

    global _ACTIVE_EXPORTER, _ACTIVE_PROVIDER, _INSTRUMENTED
    if not bool(getattr(settings, "otel_enabled", False)):
        return ObservabilityRuntime(enabled=False)
    if _INSTRUMENTED:
        existing = getattr(app.state, "observability", None)
        if isinstance(existing, ObservabilityRuntime) and existing.enabled:
            return existing
        if is_production_environment(str(getattr(settings, "app_env", ""))):
            raise RuntimeError("OTEL_CONFIGURATION_FAILED")
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
            sampler=_public_boundary_sampler(float(settings.otel_trace_sample_ratio)),
        )
        safe_exporter = SafeSanitizingSpanExporter(delegate)
        provider.add_span_processor(BatchSpanProcessor(safe_exporter))

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
        _ACTIVE_EXPORTER = safe_exporter
        runtime = ObservabilityRuntime(
            enabled=True,
            provider=provider,
            exporter=safe_exporter,
        )
        app.state.observability = runtime
        return runtime
    except Exception:  # noqa: BLE001 - emit only a stable, non-secret failure code.
        if is_production_environment(str(getattr(settings, "app_env", ""))):
            raise RuntimeError("OTEL_CONFIGURATION_FAILED") from None
        return ObservabilityRuntime(enabled=False, error_code="OTEL_CONFIGURATION_FAILED")


def configure_worker_observability(
    settings: ObservabilitySettings | Any,
    *,
    engine: Engine | None = None,
    exporter_factory: ExporterFactory = OTLPSpanExporter,
) -> ObservabilityRuntime:
    """Configure the same safe exporter and client instrumentation in a worker process."""

    global _ACTIVE_EXPORTER, _ACTIVE_PROVIDER, _INSTRUMENTED
    if not bool(getattr(settings, "otel_enabled", False)):
        return ObservabilityRuntime(enabled=False)
    if _INSTRUMENTED:
        if _ACTIVE_PROVIDER is not None:
            return ObservabilityRuntime(
                enabled=True,
                provider=_ACTIVE_PROVIDER,
                exporter=_ACTIVE_EXPORTER,
            )
        if is_production_environment(str(getattr(settings, "app_env", ""))):
            raise RuntimeError("OTEL_CONFIGURATION_FAILED")
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
        safe_exporter = SafeSanitizingSpanExporter(delegate)
        provider.add_span_processor(BatchSpanProcessor(safe_exporter))
        if engine is not None:
            SQLAlchemyInstrumentor().instrument(
                engine=engine,
                tracer_provider=provider,
                enable_commenter=False,
            )
        _instrument_outbound_clients(provider)
        _INSTRUMENTED = True
        _ACTIVE_PROVIDER = provider
        _ACTIVE_EXPORTER = safe_exporter
        return ObservabilityRuntime(
            enabled=True,
            provider=provider,
            exporter=safe_exporter,
        )
    except Exception:  # noqa: BLE001 - emit only a stable, non-secret failure code.
        if is_production_environment(str(getattr(settings, "app_env", ""))):
            raise RuntimeError("OTEL_CONFIGURATION_FAILED") from None
        return ObservabilityRuntime(enabled=False, error_code="OTEL_CONFIGURATION_FAILED")


@contextmanager
def internal_span(
    name: str,
    *,
    attributes: Mapping[str, AttributeValue] | None = None,
    parent_context: Context | None = None,
) -> Iterator[Span]:
    if _ACTIVE_PROVIDER is None:
        yield trace.get_current_span()
        return
    tracer = _ACTIVE_PROVIDER.get_tracer("auris-flow")
    with tracer.start_as_current_span(
        name,
        context=parent_context,
        attributes=sanitize_span_attributes(attributes),
    ) as span:
        yield span


@contextmanager
def safe_http_client_span(
    *,
    method: str,
    scheme: str,
    host: str,
    port: int,
) -> Iterator[tuple[Span, dict[str, str]]]:
    """Create a low-sensitivity CLIENT span and a W3C trace-context carrier.

    The pinned callback transport deliberately bypasses the instrumented urllib/httpx
    clients. Keep its telemetry surface explicit: request paths, queries, headers and
    bodies never become span names, attributes, events or error descriptions.
    """

    safe_method = method.upper() if re.fullmatch(r"[A-Za-z]{1,16}", method) else "HTTP"
    attributes: dict[str, AttributeValue] = {
        "http.request.method": safe_method,
        "url.scheme": scheme,
        # ``server.address`` is intentionally reserved for the global PII scrubber.
        # This host has already passed the callback target validator, so expose it
        # under a constrained host-only key without weakening global redaction.
        "server.host": host,
        "server.port": port,
    }
    provider = _ACTIVE_PROVIDER
    tracer = (
        provider.get_tracer("auris-flow.pinned-http")
        if provider is not None
        else trace.get_tracer("auris-flow.pinned-http")
    )
    with tracer.start_as_current_span(
        f"HTTP {safe_method}",
        kind=SpanKind.CLIENT,
        attributes=sanitize_span_attributes(attributes),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        trace_headers: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(trace_headers)
        try:
            yield span, trace_headers
        except BaseException:
            if span.is_recording():
                span.set_status(Status(StatusCode.ERROR))
            raise


def record_safe_http_status(span: Span, status_code: int) -> None:
    """Record only the numeric response status; callback redirects are failures."""

    if not span.is_recording():
        return
    span.set_attribute("http.response.status_code", status_code)
    if status_code >= 300:
        span.set_status(Status(StatusCode.ERROR))


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


def current_trace_carrier() -> dict[str, str]:
    """Return the minimal W3C carrier for durable asynchronous propagation."""

    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    traceparent = carrier.get("traceparent")
    if not isinstance(traceparent, str):
        return {}
    result = {"traceparent": traceparent}
    tracestate = carrier.get("tracestate")
    if isinstance(tracestate, str) and tracestate:
        result["tracestate"] = tracestate
    return result


def extract_remote_trace_context(value: object) -> Context | None:
    """Parse a server-owned W3C carrier and fail closed on malformed input."""

    if not isinstance(value, dict) or not value:
        return None
    if not set(value).issubset({"traceparent", "tracestate"}):
        return None
    if any(
        not isinstance(key, str) or not isinstance(item, str) or not item or len(item) > 512
        for key, item in value.items()
    ):
        return None
    traceparent = value.get("traceparent")
    if not isinstance(traceparent, str) or not re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", traceparent
    ):
        return None
    extracted = TraceContextTextMapPropagator().extract(value)
    span_context = trace.get_current_span(extracted).get_span_context()
    if not span_context.is_valid or not span_context.is_remote:
        return None
    return extracted
