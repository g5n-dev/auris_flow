from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from datetime import UTC, datetime
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

    def __init__(self, *, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self._callback_db_counts: dict[str, int] = {}
        self._worker_db_counts: dict[str, int] = {}
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

    def refresh_operational_metrics(
        self,
        *,
        session_factory: Callable[[], Session],
        engine: Engine,
        now: datetime | None = None,
    ) -> None:
        observed_at = (now or datetime.now(UTC)).astimezone(UTC)
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
            self.outbox_retry_pending.set(retry_pending)
            age_seconds = max(0.0, (observed_at - oldest).total_seconds()) if oldest else 0.0
            self.outbox_oldest_pending_age.set(age_seconds)
            self._refresh_outcome_counters(session_factory)
            self._refresh_pool(engine)
            self.collection_success.set(1)
        except Exception:  # noqa: BLE001 - a scrape failure must never affect the service.
            self.collection_success.set(0)

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

    def _refresh_outcome_counters(self, session_factory: Callable[[], Session]) -> None:
        try:
            with session_factory() as session:
                callback_rows = session.execute(
                    text(
                        "SELECT CASE WHEN status = 'pending' "
                        "AND (attempt_count > 0 OR reconcile_attempt_count > 0) "
                        "THEN 'retry' ELSE status END AS outcome_status, COUNT(*) "
                        "FROM outbox_events WHERE event_type = 'external_callback.requested' "
                        "GROUP BY CASE WHEN status = 'pending' "
                        "AND (attempt_count > 0 OR reconcile_attempt_count > 0) "
                        "THEN 'retry' ELSE status END"
                    )
                ).all()
                attempt_rows = session.execute(
                    text("SELECT status, COUNT(*) FROM outbox_delivery_attempts GROUP BY status")
                ).all()
        except Exception:  # noqa: BLE001 - older schemas may not have delivery attempts yet.
            return

        callback_mapping = {
            "blocked": "failure",
            "dead_letter": "dead_letter",
            "processed": "success",
            "retry": "retry",
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

    @staticmethod
    def _advance_db_counters(
        counter: Counter,
        previous: dict[str, int],
        current: dict[str, int],
    ) -> None:
        for outcome, count in current.items():
            prior = previous.get(outcome, 0)
            if count > prior:
                counter.labels(outcome).inc(count - prior)
            previous[outcome] = count

    def render(self) -> bytes:
        return generate_latest(self.registry)


metrics = AurisMetrics()
