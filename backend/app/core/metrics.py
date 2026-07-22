from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Condition
from time import monotonic
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

_HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_WORKER_OUTCOMES = frozenset({"blocked", "dead_letter", "failure", "retry", "success"})
_CALLBACK_OUTCOMES = frozenset({"dead_letter", "failure", "retry", "success"})
_RATE_LIMIT_OUTCOMES = frozenset(
    {"allowed", "backend_unavailable", "fallback_allowed", "fallback_limited", "limited"}
)
_TASK_RUN_ACTIVE_STATUSES = (
    "blocked",
    "cancelling",
    "completion_pending",
    "pending",
    "queued",
    "running",
    "submitted",
    "other",
)
_TASK_RUN_TERMINAL_OUTCOMES = ("cancelled", "failed", "success")
_TASK_RUN_MONITOR_ACTIONS = {
    "task_run_cancellation": ("task_run_cancellation_auto_%", "deadline_cancel"),
    "task_run_status_sync": ("task_run_status_sync_auto_%", "status_sync"),
}
_TASK_RUN_MONITOR_OUTCOMES = ("active", "failure", "other", "success")
_TASK_RUN_DURATION_BUCKETS_SECONDS = (
    1,
    5,
    15,
    30,
    60,
    300,
    900,
    1800,
    3600,
    7200,
    14400,
    86400,
)
_TASK_RUN_DURATION_WINDOW = timedelta(hours=24)
_OUTBOX_DELIVERY_DURATION_BUCKETS_SECONDS = (
    1,
    5,
    15,
    30,
    60,
    120,
    300,
    600,
    1800,
    3600,
    7200,
    14400,
    86400,
)
_OUTBOX_DELIVERY_DURATION_WINDOW = timedelta(hours=24)
_RECENT_DEAD_LETTER_WINDOW = timedelta(minutes=5)
_OPERATIONAL_SNAPSHOT_TTL_SECONDS = 10.0
_FAILED_OPERATIONAL_SNAPSHOT_TTL_SECONDS = 5.0
_OPERATIONAL_SNAPSHOT_WAIT_SECONDS = 1.0
_ROUTE_TEMPLATE = re.compile(r"^/[A-Za-z0-9_{}./:-]{0,240}$")
_INTERNAL_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _csv_items(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.replace("\n", ",").split(",") if item.strip())


def parse_metrics_networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in _csv_items(value):
        networks.append(ipaddress.ip_network(item, strict=False))
    return tuple(networks)


def is_metrics_client_allowed(client_host: str | None, trusted_cidrs: str) -> bool:
    """Authorize a scrape from the actual socket peer, never a forwarded header."""

    if not client_host:
        return False
    try:
        address = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if any(
        address.version == network.version and address in network for network in _INTERNAL_NETWORKS
    ):
        return True
    try:
        trusted = parse_metrics_networks(trusted_cidrs)
    except ValueError:
        return False
    return any(address.version == network.version and address in network for network in trusted)


def _method_label(method: str) -> str:
    normalized = method.strip().upper()
    return normalized if normalized in _HTTP_METHODS else "OTHER"


def _route_label(route: str | None) -> str:
    if not route or not _ROUTE_TEMPLATE.fullmatch(route):
        return "__unmatched__"
    return route


def _status_class(status_code: int) -> str:
    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "unknown"


def _bounded_outcome(value: str, allowed: frozenset[str]) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in allowed else "other"


def _monitor_outcome(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in {"pending", "queued", "running", "submitted"}:
        return "active"
    if normalized == "success":
        return "success"
    if normalized in {"blocked", "cancelled", "failed"}:
        return "failure"
    return "other"


def _as_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


class AurisMetrics:
    """Low-cardinality process metrics shared by the API and asynchronous workers."""

    def __init__(
        self,
        *,
        registry: CollectorRegistry | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._callback_db_counts: dict[str, int] = {}
        self._worker_db_counts: dict[str, int] = {}
        self._monotonic_clock = monotonic_clock
        self._operational_snapshot_condition = Condition()
        self._operational_snapshot_refreshing = False
        self._operational_snapshot_expires_at = float("-inf")
        self.http_requests = Counter(
            "auris_http_requests",
            "Auris Flow HTTP requests grouped only by method, route template, and status class.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "auris_http_request_duration_seconds",
            "Auris Flow HTTP request duration grouped only by method, route template, "
            "and status class.",
            ("method", "route", "status_class"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "auris_auth_failures",
            "Authentication failures with a bounded reason label.",
            ("reason",),
            registry=self.registry,
        )
        self.dependency_ready = Gauge(
            "auris_dependency_ready",
            "Whether a configured readiness dependency passed its latest probe.",
            ("dependency",),
            registry=self.registry,
        )
        self.outbox_pending = Gauge(
            "auris_outbox_pending",
            "Current pending outbox event count.",
            registry=self.registry,
        )
        self.outbox_dead_letter = Gauge(
            "auris_outbox_dead_letter",
            "Current dead-letter outbox event count.",
            registry=self.registry,
        )
        self.outbox_dead_letter_recent = Gauge(
            "auris_outbox_dead_letter_recent",
            "Dead-letter Outbox events whose authoritative processing timestamp is within "
            "the trailing five minutes.",
            registry=self.registry,
        )
        self.outbox_retry_pending = Gauge(
            "auris_outbox_retry_pending",
            "Current pending outbox events which have consumed a dispatch or reconcile attempt.",
            registry=self.registry,
        )
        self.outbox_oldest_pending_age = Gauge(
            "auris_outbox_oldest_pending_age_seconds",
            "Age in seconds of the oldest pending outbox event.",
            registry=self.registry,
        )
        self.outbox_delivery_duration_window_bucket = Gauge(
            "auris_outbox_delivery_duration_window_seconds_bucket",
            "Cumulative delivery-duration buckets for successfully processed Outbox events "
            "in the trailing 24 hours.",
            ("le",),
            registry=self.registry,
        )
        self.outbox_delivery_duration_window_count = Gauge(
            "auris_outbox_delivery_duration_window_seconds_count",
            "Successfully processed Outbox events with a valid creation-to-processing "
            "duration in the trailing 24 hours.",
            registry=self.registry,
        )
        self.outbox_delivery_duration_window_sum = Gauge(
            "auris_outbox_delivery_duration_window_seconds_sum",
            "Summed creation-to-processing duration for successfully processed Outbox "
            "events in the trailing 24 hours.",
            registry=self.registry,
        )
        self.callback_outcomes = Counter(
            "auris_callback_outcomes",
            "External callback outcomes with a bounded outcome label.",
            ("outcome",),
            registry=self.registry,
        )
        self.worker_processing = Counter(
            "auris_worker_processing",
            "Asynchronous worker processing outcomes with a bounded outcome label.",
            ("outcome",),
            registry=self.registry,
        )
        self.rate_limit_decisions = Counter(
            "auris_rate_limit_decisions",
            "BFF rate-limit decisions with a bounded outcome label.",
            ("outcome",),
            registry=self.registry,
        )
        self.task_run_active = Gauge(
            "auris_task_run_active",
            "Current active TaskRun rows grouped by a bounded lifecycle status.",
            ("status",),
            registry=self.registry,
        )
        self.task_run_terminal = Gauge(
            "auris_task_run_terminal",
            "Authoritative cumulative TaskRun terminal rows grouped by outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self.task_run_duration_window_bucket = Gauge(
            "auris_task_run_duration_window_seconds_bucket",
            "Cumulative duration buckets for TaskRuns finished in the trailing 24 hours.",
            ("outcome", "le"),
            registry=self.registry,
        )
        self.task_run_duration_window_count = Gauge(
            "auris_task_run_duration_window_seconds_count",
            "TaskRuns with a valid acceptance-to-terminal duration in the trailing 24 hours.",
            ("outcome",),
            registry=self.registry,
        )
        self.task_run_duration_window_sum = Gauge(
            "auris_task_run_duration_window_seconds_sum",
            "Summed acceptance-to-terminal TaskRun duration in the trailing 24 hours.",
            ("outcome",),
            registry=self.registry,
        )
        self.task_run_monitor_actions = Gauge(
            "auris_task_run_monitor_actions",
            "Authoritative automatic TaskRun monitor controls by bounded action and outcome.",
            ("action", "outcome"),
            registry=self.registry,
        )
        self.task_run_deadline_overdue = Gauge(
            "auris_task_run_deadline_overdue",
            "Current non-terminal TaskRuns whose server-side deadline has elapsed.",
            registry=self.registry,
        )
        self.task_run_status_sync_overdue = Gauge(
            "auris_task_run_status_sync_overdue",
            "Current TaskRuns whose scheduled engine status reconciliation is overdue.",
            registry=self.registry,
        )
        self.db_pool_checked_out = Gauge(
            "auris_db_pool_checked_out",
            "Current checked-out database connections.",
            registry=self.registry,
        )
        self.db_pool_size = Gauge(
            "auris_db_pool_size",
            "Configured database connection pool size when available.",
            registry=self.registry,
        )
        self.db_pool_overflow = Gauge(
            "auris_db_pool_overflow",
            "Current database pool overflow when available.",
            registry=self.registry,
        )
        self.collection_success = Gauge(
            "auris_metrics_collection_success",
            "Whether the latest operational metric collection succeeded.",
            registry=self.registry,
        )

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST

    def observe_http(
        self,
        *,
        method: str,
        route: str | None,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        labels = (
            _method_label(method),
            _route_label(route),
            _status_class(status_code),
        )
        self.http_requests.labels(*labels).inc()
        self.http_duration.labels(*labels).observe(max(0.0, float(duration_seconds)))
        if status_code == 401:
            self.auth_failures.labels("unauthorized").inc()

    def set_dependency_readiness(self, checks: dict[str, str]) -> None:
        allowed_dependencies = {
            "auth",
            "dagster",
            "database",
            "object_storage",
            "observability",
            "qdrant",
            "redis",
        }
        for dependency in sorted(allowed_dependencies):
            if dependency in checks:
                self.dependency_ready.labels(dependency).set(1 if checks[dependency] == "ok" else 0)

    def record_worker_processing(self, outcome: str) -> None:
        self.worker_processing.labels(_bounded_outcome(outcome, _WORKER_OUTCOMES)).inc()

    def record_callback_outcome(self, outcome: str) -> None:
        self.callback_outcomes.labels(_bounded_outcome(outcome, _CALLBACK_OUTCOMES)).inc()

    def record_rate_limit_decision(self, *, allowed: bool, backend: str) -> None:
        normalized_backend = backend.strip().lower()
        if normalized_backend == "redis-unavailable":
            outcome = "backend_unavailable"
        elif normalized_backend == "redis":
            outcome = "allowed" if allowed else "limited"
        else:
            outcome = "fallback_allowed" if allowed else "fallback_limited"
        self.rate_limit_decisions.labels(_bounded_outcome(outcome, _RATE_LIMIT_OUTCOMES)).inc()

    def refresh_operational_metrics(
        self,
        *,
        session_factory: Callable[[], Session],
        engine: Engine,
        now: datetime | None = None,
    ) -> None:
        if not self._claim_operational_snapshot_refresh():
            return

        collection_succeeded = False
        try:
            observed_at = (now or datetime.now(UTC)).astimezone(UTC)
            collection_succeeded = self._collect_operational_metrics(
                session_factory=session_factory,
                engine=engine,
                observed_at=observed_at,
            )
        except Exception:  # noqa: BLE001 - a scrape failure must never affect the service.
            self.collection_success.set(0)
        finally:
            self._complete_operational_snapshot_refresh(succeeded=collection_succeeded)

    def _claim_operational_snapshot_refresh(self) -> bool:
        with self._operational_snapshot_condition:
            while True:
                if self._monotonic_clock() < self._operational_snapshot_expires_at:
                    return False
                if not self._operational_snapshot_refreshing:
                    self._operational_snapshot_refreshing = True
                    return True
                notified = self._operational_snapshot_condition.wait(
                    timeout=_OPERATIONAL_SNAPSHOT_WAIT_SECONDS
                )
                if not notified:
                    # A stuck collector must not hold every concurrent /metrics
                    # request indefinitely. The waiter serves the last snapshot;
                    # the owner still publishes or fails its refresh when it exits.
                    return False

    def _complete_operational_snapshot_refresh(self, *, succeeded: bool) -> None:
        ttl_seconds = (
            _OPERATIONAL_SNAPSHOT_TTL_SECONDS
            if succeeded
            else _FAILED_OPERATIONAL_SNAPSHOT_TTL_SECONDS
        )
        with self._operational_snapshot_condition:
            self._operational_snapshot_expires_at = self._monotonic_clock() + ttl_seconds
            self._operational_snapshot_refreshing = False
            self._operational_snapshot_condition.notify_all()

    def _collect_operational_metrics(
        self,
        *,
        session_factory: Callable[[], Session],
        engine: Engine,
        observed_at: datetime,
    ) -> bool:
        try:
            with session_factory() as session:
                pending = int(
                    session.scalar(
                        text("SELECT COUNT(*) FROM outbox_events WHERE status = 'pending'")
                    )
                    or 0
                )
                dead_letter = int(
                    session.scalar(
                        text("SELECT COUNT(*) FROM outbox_events WHERE status = 'dead_letter'")
                    )
                    or 0
                )
                query_observed_at = (
                    observed_at.replace(tzinfo=None)
                    if engine.dialect.name in {"mysql", "sqlite"}
                    else observed_at
                )
                recent_dead_letter = int(
                    session.scalar(
                        text(
                            "SELECT COUNT(*) FROM outbox_events "
                            "WHERE status = 'dead_letter' AND processed_at >= :cutoff"
                        ),
                        {"cutoff": query_observed_at - _RECENT_DEAD_LETTER_WINDOW},
                    )
                    or 0
                )
                retry_pending = int(
                    session.scalar(
                        text(
                            "SELECT COUNT(*) FROM outbox_events "
                            "WHERE status = 'pending' "
                            "AND (attempt_count > 0 OR reconcile_attempt_count > 0)"
                        )
                    )
                    or 0
                )
                oldest = _as_utc_datetime(
                    session.scalar(
                        text("SELECT MIN(created_at) FROM outbox_events WHERE status = 'pending'")
                    )
                )
            self.outbox_pending.set(pending)
            self.outbox_dead_letter.set(dead_letter)
            self.outbox_dead_letter_recent.set(recent_dead_letter)
            self.outbox_retry_pending.set(retry_pending)
            age_seconds = max(0.0, (observed_at - oldest).total_seconds()) if oldest else 0.0
            self.outbox_oldest_pending_age.set(age_seconds)
            outbox_delivery_metrics_collected = self._refresh_outbox_delivery_metrics(
                session_factory,
                engine=engine,
                observed_at=observed_at,
            )
            outcome_metrics_collected = self._refresh_outcome_counters(session_factory)
            task_run_metrics_collected = self._refresh_task_run_metrics(
                session_factory,
                engine=engine,
                observed_at=observed_at,
            )
            self._refresh_pool(engine)
            succeeded = (
                outbox_delivery_metrics_collected
                and outcome_metrics_collected
                and task_run_metrics_collected
            )
            self.collection_success.set(1 if succeeded else 0)
            return succeeded
        except Exception:  # noqa: BLE001 - a scrape failure must never affect the service.
            self.collection_success.set(0)
            return False

    def _refresh_pool(self, engine: Engine) -> None:
        pool: Any = engine.pool
        for metric, method_name in (
            (self.db_pool_checked_out, "checkedout"),
            (self.db_pool_size, "size"),
            (self.db_pool_overflow, "overflow"),
        ):
            method = getattr(pool, method_name, None)
            if callable(method):
                metric.set(max(0, int(method())))

    def _refresh_outbox_delivery_metrics(
        self,
        session_factory: Callable[[], Session],
        *,
        engine: Engine,
        observed_at: datetime,
    ) -> bool:
        query_observed_at = (
            observed_at.replace(tzinfo=None)
            if engine.dialect.name in {"mysql", "sqlite"}
            else observed_at
        )
        try:
            with session_factory() as session:
                row = self._outbox_delivery_duration_row(
                    session,
                    dialect_name=engine.dialect.name,
                    cutoff=query_observed_at - _OUTBOX_DELIVERY_DURATION_WINDOW,
                )
        except Exception:  # noqa: BLE001 - a partial metric view must fail closed.
            return False

        count = int(row[0])
        duration_sum = max(0.0, float(row[1]))
        self.outbox_delivery_duration_window_count.set(count)
        self.outbox_delivery_duration_window_sum.set(duration_sum)
        for index, upper_bound in enumerate(_OUTBOX_DELIVERY_DURATION_BUCKETS_SECONDS):
            self.outbox_delivery_duration_window_bucket.labels(str(upper_bound)).set(
                int(row[index + 2])
            )
        self.outbox_delivery_duration_window_bucket.labels("+Inf").set(count)
        return True

    @staticmethod
    def _outbox_delivery_duration_row(
        session: Session,
        *,
        dialect_name: str,
        cutoff: datetime,
    ) -> Any:
        if dialect_name == "mysql":
            duration_expression = "TIMESTAMPDIFF(MICROSECOND, created_at, processed_at) / 1000000.0"
        elif dialect_name == "sqlite":
            duration_expression = (
                "ROUND((julianday(processed_at) - julianday(created_at)) * 86400.0, 3)"
            )
        else:
            raise RuntimeError(f"unsupported operational metrics dialect: {dialect_name}")
        buckets = ", ".join(
            f"SUM(CASE WHEN duration_seconds <= {upper_bound} THEN 1 ELSE 0 END) AS bucket_{index}"
            for index, upper_bound in enumerate(_OUTBOX_DELIVERY_DURATION_BUCKETS_SECONDS)
        )
        query = text(
            "SELECT COUNT(*), COALESCE(SUM(duration_seconds), 0), "
            f"{buckets} FROM ("
            f"SELECT {duration_expression} AS duration_seconds "
            "FROM outbox_events WHERE status = 'processed' "
            "AND created_at IS NOT NULL AND processed_at IS NOT NULL "
            "AND processed_at >= :cutoff"
            ") AS outbox_delivery_durations WHERE duration_seconds >= 0"
        )
        return session.execute(query, {"cutoff": cutoff}).one()

    def _refresh_outcome_counters(self, session_factory: Callable[[], Session]) -> bool:
        try:
            with session_factory() as session:
                callback_rows = session.execute(
                    text(
                        "SELECT attempts.status, COUNT(*) "
                        "FROM outbox_delivery_attempts AS attempts "
                        "INNER JOIN outbox_events AS events "
                        "ON events.event_id = attempts.event_id "
                        "WHERE events.event_type = 'external_callback.requested' "
                        "GROUP BY attempts.status"
                    )
                ).all()
                attempt_rows = session.execute(
                    text("SELECT status, COUNT(*) FROM outbox_delivery_attempts GROUP BY status")
                ).all()
        except Exception:  # noqa: BLE001 - older schemas may not have delivery attempts yet.
            return False

        callback_mapping = {
            "blocked": "failure",
            "dead_letter": "dead_letter",
            "failed": "failure",
            "reconcile_retry_scheduled": "retry",
            "retry_scheduled": "retry",
            "succeeded": "success",
        }
        callback_counts = {outcome: 0 for outcome in _CALLBACK_OUTCOMES}
        for status, count in callback_rows:
            outcome = callback_mapping.get(str(status))
            if outcome:
                callback_counts[outcome] += int(count)

        worker_mapping = {
            "blocked": "blocked",
            "dead_letter": "dead_letter",
            "reconcile_retry_scheduled": "retry",
            "retry_scheduled": "retry",
            "succeeded": "success",
        }
        worker_counts = {outcome: 0 for outcome in _WORKER_OUTCOMES}
        for status, count in attempt_rows:
            outcome = worker_mapping.get(str(status))
            if outcome:
                worker_counts[outcome] += int(count)

        self._advance_db_counters(
            self.callback_outcomes,
            self._callback_db_counts,
            callback_counts,
        )
        self._advance_db_counters(
            self.worker_processing,
            self._worker_db_counts,
            worker_counts,
        )
        return True

    def _refresh_task_run_metrics(
        self,
        session_factory: Callable[[], Session],
        *,
        engine: Engine,
        observed_at: datetime,
    ) -> bool:
        query_observed_at = (
            observed_at.replace(tzinfo=None)
            if engine.dialect.name in {"mysql", "sqlite"}
            else observed_at
        )
        try:
            with session_factory() as session:
                state_rows = session.execute(
                    text(
                        "SELECT status, COUNT(*) FROM run_records "
                        "WHERE run_type = 'task_run' GROUP BY status"
                    )
                ).all()
                monitor_rows = session.execute(
                    text(
                        "SELECT run_type, status, COUNT(*) FROM run_records WHERE "
                        "(run_type = 'task_run_cancellation' "
                        "AND run_id LIKE 'task_run_cancellation_auto_%') OR "
                        "(run_type = 'task_run_status_sync' "
                        "AND run_id LIKE 'task_run_status_sync_auto_%') "
                        "GROUP BY run_type, status"
                    )
                ).all()
                deadline_overdue = int(
                    session.scalar(
                        text(
                            "SELECT COUNT(*) FROM run_records "
                            "WHERE run_type = 'task_run' "
                            "AND status IN ('blocked', 'cancelling', 'completion_pending', "
                            "'pending', 'queued', 'running', 'submitted') "
                            "AND deadline_at IS NOT NULL AND deadline_at <= :observed_at"
                        ),
                        {"observed_at": query_observed_at},
                    )
                    or 0
                )
                status_sync_overdue = int(
                    session.scalar(
                        text(
                            "SELECT COUNT(*) FROM run_records "
                            "WHERE run_type = 'task_run' "
                            "AND status IN ('cancelling', 'completion_pending', "
                            "'running', 'submitted') "
                            "AND next_status_sync_at IS NOT NULL "
                            "AND next_status_sync_at <= :observed_at"
                        ),
                        {"observed_at": query_observed_at},
                    )
                    or 0
                )
                duration_rows = self._task_run_duration_rows(
                    session,
                    dialect_name=engine.dialect.name,
                    cutoff=query_observed_at - _TASK_RUN_DURATION_WINDOW,
                )
        except Exception:  # noqa: BLE001 - a partial metric view must fail closed.
            return False

        state_counts = {str(status): int(count) for status, count in state_rows}
        known_statuses = set(_TASK_RUN_ACTIVE_STATUSES) | set(_TASK_RUN_TERMINAL_OUTCOMES)
        state_counts["other"] = sum(
            count for status, count in state_counts.items() if status not in known_statuses
        )
        for status in _TASK_RUN_ACTIVE_STATUSES:
            self.task_run_active.labels(status).set(state_counts.get(status, 0))
        for outcome in _TASK_RUN_TERMINAL_OUTCOMES:
            self.task_run_terminal.labels(outcome).set(state_counts.get(outcome, 0))

        monitor_counts: dict[tuple[str, str], int] = {}
        for run_type, status, count in monitor_rows:
            key = (str(run_type), _monitor_outcome(str(status)))
            monitor_counts[key] = monitor_counts.get(key, 0) + int(count)
        for run_type, (_, action) in _TASK_RUN_MONITOR_ACTIONS.items():
            for outcome in _TASK_RUN_MONITOR_OUTCOMES:
                self.task_run_monitor_actions.labels(action, outcome).set(
                    monitor_counts.get((run_type, outcome), 0)
                )

        durations = {str(row[0]): row for row in duration_rows}
        for outcome in _TASK_RUN_TERMINAL_OUTCOMES:
            row = durations.get(outcome)
            count = int(row[1]) if row else 0
            duration_sum = max(0.0, float(row[2])) if row else 0.0
            self.task_run_duration_window_count.labels(outcome).set(count)
            self.task_run_duration_window_sum.labels(outcome).set(duration_sum)
            for index, upper_bound in enumerate(_TASK_RUN_DURATION_BUCKETS_SECONDS):
                bucket_count = int(row[index + 3]) if row else 0
                self.task_run_duration_window_bucket.labels(outcome, str(upper_bound)).set(
                    bucket_count
                )
            self.task_run_duration_window_bucket.labels(outcome, "+Inf").set(count)

        self.task_run_deadline_overdue.set(deadline_overdue)
        self.task_run_status_sync_overdue.set(status_sync_overdue)
        return True

    @staticmethod
    def _task_run_duration_rows(
        session: Session,
        *,
        dialect_name: str,
        cutoff: datetime,
    ) -> list[Any]:
        if dialect_name == "mysql":
            duration_expression = "TIMESTAMPDIFF(MICROSECOND, created_at, finished_at) / 1000000.0"
        elif dialect_name == "sqlite":
            duration_expression = (
                "ROUND((julianday(finished_at) - julianday(created_at)) * 86400.0, 3)"
            )
        else:
            raise RuntimeError(f"unsupported operational metrics dialect: {dialect_name}")
        buckets = ", ".join(
            f"SUM(CASE WHEN duration_seconds <= {upper_bound} THEN 1 ELSE 0 END) AS bucket_{index}"
            for index, upper_bound in enumerate(_TASK_RUN_DURATION_BUCKETS_SECONDS)
        )
        query = text(
            "SELECT outcome, COUNT(*), COALESCE(SUM(duration_seconds), 0), "
            f"{buckets} FROM ("
            f"SELECT status AS outcome, {duration_expression} AS duration_seconds "
            "FROM run_records WHERE run_type = 'task_run' "
            "AND status IN ('cancelled', 'failed', 'success') "
            "AND created_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND finished_at >= :cutoff"
            ") AS task_run_durations WHERE duration_seconds >= 0 GROUP BY outcome"
        )
        return list(session.execute(query, {"cutoff": cutoff}).all())

    @staticmethod
    def _advance_db_counters(
        counter: Counter,
        previous: dict[str, int],
        current: dict[str, int],
    ) -> None:
        for outcome, count in current.items():
            prior = previous.get(outcome)
            if prior is None:
                previous[outcome] = count
                continue
            if count > prior:
                counter.labels(outcome).inc(count - prior)
            previous[outcome] = count

    def render(self) -> bytes:
        return generate_latest(self.registry)


metrics = AurisMetrics()
