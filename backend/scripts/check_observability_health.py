#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Tracer


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def open_url_no_redirect(request: Request, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


_TIMEOUT_SECONDS = 3.0
_TRACE_QUERY_TIMEOUT_SECONDS = 0.5
_TRACE_PROPAGATION_DEADLINE_SECONDS = 9.0
_TRACE_QUERY_INTERVAL_SECONDS = 0.25
_MONITOR_INTERVAL_SECONDS = 5.0
_MONITOR_FRESHNESS_SECONDS = 25.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_OTLP_ENDPOINT = "http://otel-collector:4318/v1/traces"
_TEMPO_TRACE_URL = "http://tempo:3200/api/traces/{trace_id}"
_SERVER_ADDRESS = ("0.0.0.0", 8080)
_MARKER_SPAN_NAME = "auris_flow.observability.pipeline.readiness"
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class _Endpoint:
    name: str
    url: str
    required_token: bytes | None = None


_ENDPOINTS = (
    _Endpoint("otel-collector", "http://otel-collector:13133/"),
    _Endpoint("tempo", "http://tempo:3200/ready"),
    _Endpoint("prometheus", "http://prometheus:9090/-/ready"),
    _Endpoint("alertmanager", "http://alertmanager:9093/-/ready"),
    _Endpoint(
        "node-exporter",
        "http://node-exporter:9100/metrics",
        b"node_exporter_build_info",
    ),
)


def _read_bounded_response(request: Request, *, timeout: float) -> bytes:
    with open_url_no_redirect(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise RuntimeError("response exceeds the health-probe byte limit")
    return body


def _check(endpoint: _Endpoint) -> None:
    body = _read_bounded_response(
        Request(
            endpoint.url,
            headers={"Accept": "text/plain, application/json"},
            method="GET",
        ),
        timeout=_TIMEOUT_SECONDS,
    )
    if endpoint.required_token is not None and endpoint.required_token not in body:
        raise RuntimeError("required readiness marker is absent")


class _TrackingExporter(SpanExporter):
    def __init__(self) -> None:
        self._delegate = OTLPSpanExporter(endpoint=_OTLP_ENDPOINT, timeout=3.0)
        self._accepted_trace_ids: deque[int] = deque(maxlen=64)
        self._lock = threading.Lock()

    def export(self, spans: Any) -> SpanExportResult:
        try:
            result = self._delegate.export(spans)
        except Exception:  # noqa: BLE001 - the monitor reports a stable health state.
            return SpanExportResult.FAILURE
        if result == SpanExportResult.SUCCESS:
            with self._lock:
                self._accepted_trace_ids.extend(
                    span.context.trace_id
                    for span in spans
                    if span.context is not None and span.context.is_valid
                )
        return result

    def accepted(self, trace_id: int) -> bool:
        with self._lock:
            return trace_id in self._accepted_trace_ids

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:  # noqa: BLE001 - process shutdown must complete.
            return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return bool(self._delegate.force_flush(timeout_millis))
        except Exception:  # noqa: BLE001 - the monitor reports a stable health state.
            return False


def _telemetry_client() -> tuple[TracerProvider, _TrackingExporter, Tracer]:
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": "auris-flow-observability-health",
                "service.instance.role": "readiness-monitor",
            }
        )
    )
    exporter = _TrackingExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter, provider.get_tracer("auris-flow.observability-health")


def _emit_pipeline_marker(exporter: _TrackingExporter, tracer: Tracer) -> str:
    with tracer.start_as_current_span(
        _MARKER_SPAN_NAME,
        attributes={"auris.readiness.probe": True},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        context = span.get_span_context()
        trace_id = context.trace_id
    if not context.is_valid or not exporter.accepted(trace_id):
        raise RuntimeError("OTLP marker was not accepted by the collector")
    return f"{trace_id:032x}"


def _trace_id_matches(value: object, expected_trace_id: str) -> bool:
    if not isinstance(value, str):
        return False
    if value.casefold() == expected_trace_id:
        return True
    try:
        return base64.b64decode(value, validate=True).hex() == expected_trace_id
    except (binascii.Error, ValueError):
        return False


def _resource_service_name(resource: object) -> str | None:
    if not isinstance(resource, dict):
        return None
    attributes = resource.get("attributes")
    if not isinstance(attributes, list) or len(attributes) > 1024:
        return None
    for attribute in attributes:
        if not isinstance(attribute, dict) or attribute.get("key") != "service.name":
            continue
        value = attribute.get("value")
        if not isinstance(value, dict):
            return None
        for field_name in ("stringValue", "string_value"):
            candidate = value.get(field_name)
            if isinstance(candidate, str):
                return candidate
    return None


def _tempo_contains_marker(
    payload: object,
    *,
    trace_id: str,
    expected_service: str,
) -> bool:
    """Validate the exact marker in Tempo's bounded OTLP-JSON response."""

    if not _TRACE_ID_PATTERN.fullmatch(trace_id) or not isinstance(payload, dict):
        return False
    batches = payload.get("batches")
    if not isinstance(batches, list) or not batches or len(batches) > 1024:
        return False
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        if _resource_service_name(batch.get("resource")) != expected_service:
            continue
        span_groups = batch.get("scopeSpans")
        if span_groups is None:
            span_groups = batch.get("instrumentationLibrarySpans")
        if not isinstance(span_groups, list) or len(span_groups) > 1024:
            continue
        for group in span_groups:
            spans = group.get("spans") if isinstance(group, dict) else None
            if not isinstance(spans, list) or len(spans) > 4096:
                continue
            for span in spans:
                if not isinstance(span, dict):
                    continue
                if span.get("name") != _MARKER_SPAN_NAME:
                    continue
                encoded_trace_id = span.get("traceId", span.get("trace_id"))
                if _trace_id_matches(encoded_trace_id, trace_id):
                    return True
    return False


def _tempo_trace_is_visible(trace_id: str, *, expected_service: str) -> bool:
    if not _TRACE_ID_PATTERN.fullmatch(trace_id):
        return False
    request = Request(
        _TEMPO_TRACE_URL.format(trace_id=trace_id),
        headers={"Accept": "application/json"},
        method="GET",
    )
    body = _read_bounded_response(
        request,
        timeout=_TRACE_QUERY_TIMEOUT_SECONDS,
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return _tempo_contains_marker(
        payload,
        trace_id=trace_id,
        expected_service=expected_service,
    )


def _wait_for_tempo_trace(trace_id: str) -> None:
    deadline = time.monotonic() + _TRACE_PROPAGATION_DEADLINE_SECONDS
    while True:
        try:
            if _tempo_trace_is_visible(
                trace_id,
                expected_service="auris-flow-observability-health",
            ):
                return
        except (HTTPError, URLError, OSError, RuntimeError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("OTLP marker did not reach Tempo before the deadline") from None
        time.sleep(_TRACE_QUERY_INTERVAL_SECONDS)


def _deep_probe(exporter: _TrackingExporter, tracer: Tracer) -> None:
    for endpoint in _ENDPOINTS:
        _check(endpoint)
    _wait_for_tempo_trace(_emit_pipeline_marker(exporter, tracer))


class _PipelineMonitor:
    def __init__(self, exporter: _TrackingExporter, tracer: Tracer) -> None:
        self._exporter = exporter
        self._tracer = tracer
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = False
        self._updated_at = 0.0
        self._thread = threading.Thread(
            target=self._run,
            name="observability-pipeline-monitor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)

    def snapshot(self) -> tuple[bool, float]:
        with self._lock:
            if self._updated_at <= 0:
                return False, 0.0
            age = time.monotonic() - self._updated_at
            ready = self._ready and age <= _MONITOR_FRESHNESS_SECONDS
            return ready, max(age, 0.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _deep_probe(self._exporter, self._tracer)
                ready = True
            except Exception:  # noqa: BLE001 - survive malformed dependency responses.
                ready = False
            with self._lock:
                self._ready = ready
                self._updated_at = time.monotonic()
            self._stop.wait(_MONITOR_INTERVAL_SECONDS)


def _handler(monitor: _PipelineMonitor) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AurisObservabilityHealth/1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            if self.path == "/ready":
                ready, age = monitor.snapshot()
                self._respond(
                    200 if ready else 503,
                    {
                        "status": "ok" if ready else "not_ready",
                        "probe_age_seconds": round(age, 3),
                    },
                )
                return
            trace_match = re.fullmatch(r"/traces/([0-9a-f]{32})", self.path)
            if trace_match is None:
                self.send_error(404)
                return
            trace_id = trace_match.group(1)
            try:
                visible = _tempo_trace_is_visible(
                    trace_id,
                    expected_service="auris-flow-bff",
                )
            except Exception:  # noqa: BLE001 - return only a stable internal status.
                visible = False
            self._respond(
                200 if visible else 503,
                {
                    "status": "ok" if visible else "not_ready",
                    "trace_id": trace_id,
                },
            )

        def _respond(self, status: int, body: dict[str, object]) -> None:
            payload = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    return Handler


def _serve() -> int:
    provider, exporter, tracer = _telemetry_client()
    monitor = _PipelineMonitor(exporter, tracer)
    server = ThreadingHTTPServer(_SERVER_ADDRESS, _handler(monitor))
    monitor.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
        monitor.stop()
        provider.shutdown()
    return 0


def _run_once() -> int:
    provider, exporter, tracer = _telemetry_client()
    try:
        _deep_probe(exporter, tracer)
    except (HTTPError, URLError, OSError, RuntimeError, ValueError) as exc:
        print(f"observability pipeline is not ready: {type(exc).__name__}")
        return 1
    finally:
        provider.shutdown()
    print("observability pipeline is ready")
    return 0


def _check_server() -> int:
    try:
        _read_bounded_response(
            Request(
                "http://127.0.0.1:8080/ready",
                headers={"Accept": "application/json"},
                method="GET",
            ),
            timeout=1.0,
        )
    except (HTTPError, URLError, OSError, RuntimeError):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--check-server", action="store_true")
    args = parser.parse_args()
    if args.serve:
        return _serve()
    if args.check_server:
        return _check_server()
    return _run_once()


if __name__ == "__main__":
    sys.exit(main())
