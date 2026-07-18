from __future__ import annotations

import socket
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from app.core import observability
from app.services import adapters as adapter_module
from app.services.adapters import RealExternalCallbackClient


class _Response:
    def __init__(self, *, status: int = 202, body: bytes = b"{}") -> None:
        self.status = status
        self._body = body

    def read(self, _limit: int) -> bytes:
        return self._body

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Content-Type", "application/json")]


class _Connection:
    instances: list[_Connection] = []
    response_status = 202
    failure: BaseException | None = None

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.closed = False
        self.request_call: dict[str, Any] = {}
        self.__class__.instances.append(self)

    def request(
        self,
        method: str,
        target: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
    ) -> None:
        self.request_call = {
            "method": method,
            "target": target,
            "body": body,
            "headers": dict(headers),
        }
        if self.failure is not None:
            raise self.failure

    def getresponse(self) -> _Response:
        return _Response(status=self.response_status)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def callback_spans(monkeypatch: pytest.MonkeyPatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(observability, "_ACTIVE_PROVIDER", provider)
    monkeypatch.setattr(adapter_module, "HTTPConnection", _Connection)
    monkeypatch.setattr(
        adapter_module.socket,
        "getaddrinfo",
        lambda host, port, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", port),
            )
        ],
    )
    _Connection.instances = []
    _Connection.response_status = 202
    _Connection.failure = None
    try:
        yield provider, exporter
    finally:
        provider.shutdown()


def _target(client: RealExternalCallbackClient):
    return client._validate_outbound_url(
        "http://callback.example.test:8089/callbacks/platform?token=query-canary",
        purpose="callback",
        resolve=True,
    )


def test_pinned_callback_injects_child_traceparent_and_exports_only_safe_http_attributes(
    callback_spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = callback_spans
    client = RealExternalCallbackClient(
        callback_url="http://127.0.0.1:8089/callbacks/platform",
        secret="unit-callback-key-material-at-least-32-bytes",
        app_env="local",
    )
    sensitive_headers = {
        "Authorization": "Bearer authorization-canary",
        "Cookie": "session=cookie-canary",
        "X-Auris-Signature": "v2=hmac-canary",
        "Traceparent": "forged-traceparent-canary",
    }
    sensitive_body = b'{"secret":"body-canary"}'
    tracer = provider.get_tracer("callback-parent-test")

    with tracer.start_as_current_span("outbox.dispatch") as parent:
        parent_context = parent.get_span_context()
        result = client._perform_http_request(
            _target(client),
            method="POST",
            body=sensitive_body,
            headers=sensitive_headers,
        )

    assert result["status_code"] == 202
    connection = _Connection.instances[-1]
    traceparent = connection.request_call["headers"]["traceparent"]
    spans = exporter.get_finished_spans()
    callback_span = next(span for span in spans if span.name == "HTTP POST")
    assert traceparent == (
        f"00-{trace.format_trace_id(parent_context.trace_id)}-"
        f"{trace.format_span_id(callback_span.context.span_id)}-"
        f"{int(callback_span.context.trace_flags):02x}"
    )
    assert callback_span.parent is not None
    assert callback_span.parent.span_id == parent_context.span_id
    assert callback_span.kind is SpanKind.CLIENT
    assert callback_span.attributes == {
        "http.request.method": "POST",
        "url.scheme": "http",
        "server.host": "callback.example.test",
        "server.port": 8089,
        "http.response.status_code": 202,
    }
    serialized_span = repr(callback_span)
    for canary in (
        "query-canary",
        "authorization-canary",
        "cookie-canary",
        "hmac-canary",
        "body-canary",
        "/callbacks/platform",
    ):
        assert canary not in serialized_span
    assert connection.request_call["headers"]["X-Auris-Signature"] == "v2=hmac-canary"
    assert "Traceparent" not in connection.request_call["headers"]
    assert connection.closed is True


def test_pinned_callback_failure_sets_error_without_exception_details(
    callback_spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = callback_spans
    client = RealExternalCallbackClient(
        callback_url="http://127.0.0.1:8089/callbacks/platform",
        secret="unit-callback-key-material-at-least-32-bytes",
        app_env="local",
    )
    _Connection.failure = OSError("network failed: Bearer failure-secret-canary")
    tracer = provider.get_tracer("callback-parent-test")

    with tracer.start_as_current_span("outbox.dispatch"):
        with pytest.raises(OSError, match="network failed"):
            client._perform_http_request(
                _target(client),
                method="GET",
                body=None,
                headers={"Cookie": "failure-cookie-canary"},
            )

    spans = exporter.get_finished_spans()
    callback_span = next(span for span in spans if span.name == "HTTP GET")
    assert callback_span.status.status_code is StatusCode.ERROR
    assert callback_span.status.description is None
    assert callback_span.events == ()
    assert callback_span.attributes == {
        "http.request.method": "GET",
        "url.scheme": "http",
        "server.host": "callback.example.test",
        "server.port": 8089,
    }
    serialized_span = repr(callback_span)
    assert "failure-secret-canary" not in serialized_span
    assert "failure-cookie-canary" not in serialized_span
    assert _Connection.instances[-1].closed is True


def test_pinned_callback_http_failure_records_numeric_status_only(
    callback_spans: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = callback_spans
    client = RealExternalCallbackClient(
        callback_url="http://127.0.0.1:8089/callbacks/platform",
        secret="unit-callback-key-material-at-least-32-bytes",
        app_env="local",
    )
    _Connection.response_status = 503

    with provider.get_tracer("callback-parent-test").start_as_current_span("outbox.dispatch"):
        result = client._perform_http_request(
            _target(client),
            method="POST",
            body=b'{"secret":"http-failure-body-canary"}',
            headers={"Authorization": "Bearer http-failure-auth-canary"},
        )

    assert result["status_code"] == 503
    callback_span = next(span for span in exporter.get_finished_spans() if span.name == "HTTP POST")
    assert callback_span.status.status_code is StatusCode.ERROR
    assert callback_span.status.description is None
    assert callback_span.events == ()
    assert callback_span.attributes == {
        "http.request.method": "POST",
        "url.scheme": "http",
        "server.host": "callback.example.test",
        "server.port": 8089,
        "http.response.status_code": 503,
    }
    serialized_span = repr(callback_span)
    assert "http-failure-body-canary" not in serialized_span
    assert "http-failure-auth-canary" not in serialized_span


def test_trace_instrumentation_keeps_the_validated_dns_address_pin(
    callback_spans: tuple[TracerProvider, InMemorySpanExporter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _exporter = callback_spans
    client = RealExternalCallbackClient(
        callback_url="http://callback.example.test:8089/callbacks/platform",
        secret="unit-callback-key-material-at-least-32-bytes",
        app_env="local",
    )
    created: list[tuple[tuple[str, int], float | None, tuple[str, int] | None]] = []
    sentinel = object()

    def fake_create_connection(
        address: tuple[str, int],
        timeout: float | None,
        source_address: tuple[str, int] | None,
    ) -> Any:
        created.append((address, timeout, source_address))
        return sentinel

    monkeypatch.setattr(adapter_module.socket, "create_connection", fake_create_connection)
    with provider.get_tracer("callback-parent-test").start_as_current_span("outbox.dispatch"):
        client._perform_http_request(
            _target(client),
            method="GET",
            body=None,
            headers={},
        )

    pinned_connect = _Connection.instances[-1].__dict__["_create_connection"]
    result = pinned_connect(("forged-rebind.example", 9999), 4.0, ("127.0.0.1", 0))
    assert result is sentinel
    assert created == [(("93.184.216.34", 8089), 4.0, ("127.0.0.1", 0))]
