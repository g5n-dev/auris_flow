from __future__ import annotations

import json
import signal
from pathlib import Path
from typing import Any

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
