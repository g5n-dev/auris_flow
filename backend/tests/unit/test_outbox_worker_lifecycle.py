from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.observability import (
    current_trace_carrier,
    current_trace_context,
    extract_remote_trace_context,
    internal_span,
)
from app.services.adapters import DispatchResult
from app.workers import outbox_worker


class RecordingStopEvent:
    def __init__(self) -> None:
        self.requested = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.requested

    def set(self) -> None:
        self.requested = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.requested


def test_dispatch_continues_the_server_trace_into_the_adapter(monkeypatch) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("outbox-trace-propagation-test")
    monkeypatch.setattr("app.core.observability._ACTIVE_PROVIDER", provider)
    observed: dict[str, str] = {}

    def record_adapter_context(
        event_type: str,
        aggregate_type: str,
        payload: dict[str, Any],
    ) -> DispatchResult:
        assert event_type == "task_run.requested"
        assert aggregate_type == "task_run"
        assert payload["trace_id"] == "trace_business_001"
        observed.update(current_trace_context())
        return DispatchResult(
            adapter="dagster",
            operation="run_request",
            details={"mode": "real"},
        )

    monkeypatch.setattr(outbox_worker, "dispatch_event", record_adapter_context)
    with tracer.start_as_current_span("bff.request") as request_span:
        request_context = request_span.get_span_context()
        carrier = current_trace_carrier()

    with internal_span(
        "outbox.process",
        parent_context=extract_remote_trace_context(carrier),
    ):
        result = outbox_worker._dispatch_prepared(
            outbox_worker.PreparedDelivery(
                payload={
                    "event_type": "task_run.requested",
                    "aggregate_type": "task_run",
                    "trace_id": "trace_business_001",
                    "otel_trace_context": carrier,
                },
                request_sha256="a" * 64,
                blocked=False,
            )
        )
    provider.shutdown()

    assert result.details["mode"] == "real"
    assert observed["otel_trace_id"] == f"{request_context.trace_id:032x}"
    spans = {span.name: span for span in exporter.get_finished_spans()}
    process_span = spans["outbox.process"]
    dispatch_span = spans["outbox.adapter.dispatch"]
    assert process_span.parent is not None
    assert process_span.parent.span_id == request_context.span_id
    assert dispatch_span.parent is not None
    assert dispatch_span.parent.span_id == process_span.context.span_id
    assert dispatch_span.context.trace_id == request_context.trace_id


def test_run_forever_recovers_with_backoff_and_writes_observable_state(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stop_event = RecordingStopEvent()
    outcomes: list[Exception | int] = [
        RuntimeError("database unavailable"),
        RuntimeError("database still unavailable"),
        0,
        0,
        3,
    ]
    calls: list[tuple[int, str | None]] = []

    def fake_process_once(limit: int, *, worker_id: str | None = None) -> int:
        calls.append((limit, worker_id))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == 3:
            stop_event.set()
        return outcome

    monkeypatch.setattr(outbox_worker, "process_once", fake_process_once)
    health_path = tmp_path / "outbox-worker-health.json"

    state = outbox_worker.run_forever(
        limit=7,
        worker_id="lifecycle-test-worker",
        stop_event=stop_event,  # type: ignore[arg-type]
        poll_interval_seconds=0.25,
        max_idle_wait_seconds=1.0,
        error_backoff_base_seconds=0.5,
        error_backoff_max_seconds=2.0,
        heartbeat_interval_seconds=60.0,
        health_path=health_path,
    )

    assert calls == [(7, "lifecycle-test-worker")] * 5
    assert stop_event.waits == [0.5, 1.0, 0.25, 0.5]
    assert state.status == "stopped"
    assert state.iteration_count == 5
    assert state.processed_total == 3
    assert state.consecutive_errors == 0
    assert state.last_error == "RuntimeError: database still unavailable"

    health = json.loads(health_path.read_text(encoding="utf-8"))
    assert health["status"] == "stopped"
    assert health["healthy"] is False
    assert health["worker_id"] == "lifecycle-test-worker"
    assert health["pid"] > 0
    assert health["iteration_count"] == 5
    assert health["processed_total"] == 3
    assert health["shutdown_requested"] is True
    assert health["started_at"]
    assert health["heartbeat_at"]


def test_task_run_monitor_failure_does_not_block_outbox_dispatch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stop_event = RecordingStopEvent()
    process_calls: list[tuple[int, str | None]] = []

    def failing_monitor(*, worker_id: str | None = None) -> int:
        assert worker_id == "monitor-isolation-worker"
        stop_event.set()
        raise RuntimeError("monitor database query failed")

    def successful_process_once(limit: int, *, worker_id: str | None = None) -> int:
        process_calls.append((limit, worker_id))
        return 2

    monkeypatch.setattr(outbox_worker, "monitor_task_runs_once", failing_monitor)
    monkeypatch.setattr(outbox_worker, "process_once", successful_process_once)

    state = outbox_worker.run_forever(
        limit=9,
        worker_id="monitor-isolation-worker",
        stop_event=stop_event,  # type: ignore[arg-type]
        health_path=tmp_path / "monitor-isolation-health.json",
    )

    assert process_calls == [(9, "monitor-isolation-worker")]
    assert state.processed_total == 2
    assert state.last_processed_at is not None
    assert state.monitor_status == "degraded"
    assert state.monitor_consecutive_errors == 1
    assert state.last_monitor_success_at is None
    assert state.last_monitor_error == "RuntimeError: monitor database query failed"
    assert state.last_monitor_error_at is not None
    health = json.loads((tmp_path / "monitor-isolation-health.json").read_text(encoding="utf-8"))
    assert health["healthy"] is False
    assert health["monitor_status"] == "degraded"
    assert health["monitor_consecutive_errors"] == 1
    assert health["last_monitor_error"] == "RuntimeError: monitor database query failed"


def test_monitor_health_recovers_only_after_a_later_monitor_success(
    monkeypatch,
    tmp_path: Path,
) -> None:
    stop_event = RecordingStopEvent()
    monitor_calls = 0
    process_calls = 0
    snapshots: list[dict[str, Any]] = []
    original_publish = outbox_worker._publish_worker_health
    monotonic_value = -10.0

    def advancing_monotonic() -> float:
        nonlocal monotonic_value
        monotonic_value += 10.0
        return monotonic_value

    def monitor(*, worker_id: str | None = None) -> int:
        nonlocal monitor_calls
        assert worker_id == "monitor-recovery-worker"
        monitor_calls += 1
        if monitor_calls == 1:
            raise RuntimeError("monitor temporarily unavailable")
        stop_event.set()
        return 0

    def process(limit: int, *, worker_id: str | None = None) -> int:
        nonlocal process_calls
        assert limit == 5
        assert worker_id == "monitor-recovery-worker"
        process_calls += 1
        return 1

    def capture_health(state, path) -> None:
        snapshots.append(dict(state.as_payload()))
        original_publish(state, path)

    monkeypatch.setattr(outbox_worker.time, "monotonic", advancing_monotonic)
    monkeypatch.setattr(outbox_worker, "monitor_task_runs_once", monitor)
    monkeypatch.setattr(outbox_worker, "process_once", process)
    monkeypatch.setattr(outbox_worker, "_publish_worker_health", capture_health)

    state = outbox_worker.run_forever(
        limit=5,
        worker_id="monitor-recovery-worker",
        stop_event=stop_event,  # type: ignore[arg-type]
        health_path=tmp_path / "monitor-recovery-health.json",
    )

    assert monitor_calls == 2
    assert process_calls == 2
    degraded = next(
        snapshot
        for snapshot in snapshots
        if snapshot["status"] == "running" and snapshot["monitor_status"] == "degraded"
    )
    recovered = next(
        snapshot
        for snapshot in snapshots
        if snapshot["status"] == "running" and snapshot["monitor_status"] == "healthy"
    )
    assert degraded["healthy"] is False
    assert degraded["consecutive_errors"] == 0
    assert recovered["healthy"] is True
    assert recovered["monitor_consecutive_errors"] == 0
    assert recovered["last_monitor_success_at"] is not None
    assert recovered["last_monitor_error"] == "RuntimeError: monitor temporarily unavailable"
    assert state.monitor_status == "healthy"


def test_graceful_shutdown_signals_request_stop_and_restore_handlers(monkeypatch) -> None:
    previous: dict[signal.Signals, Any] = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    installed: dict[signal.Signals, Any] = {}
    calls: list[tuple[signal.Signals, Any]] = []

    monkeypatch.setattr(signal, "getsignal", lambda signum: previous[signum])

    def fake_signal(signum: signal.Signals, handler: Any) -> None:
        calls.append((signum, handler))
        installed[signum] = handler

    monkeypatch.setattr(signal, "signal", fake_signal)
    stop_event = RecordingStopEvent()

    with outbox_worker.graceful_shutdown_signals(stop_event):  # type: ignore[arg-type]
        assert callable(installed[signal.SIGINT])
        assert callable(installed[signal.SIGTERM])
        installed[signal.SIGTERM](signal.SIGTERM, None)
        assert stop_event.is_set()

    assert calls[-2:] == [
        (signal.SIGINT, previous[signal.SIGINT]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]
