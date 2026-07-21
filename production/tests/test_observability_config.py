from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = REPOSITORY_ROOT / "production" / "observability"
COMPOSE_CONFIG = REPOSITORY_ROOT / "production" / "compose.yaml"
OTEL_COLLECTOR_CONFIG = OBSERVABILITY / "otel-collector.yaml"
PROMETHEUS_CONFIG = OBSERVABILITY / "prometheus.yaml"
ALERT_RULES = OBSERVABILITY / "alerts.yaml"
DASHBOARD = OBSERVABILITY / "grafana" / "dashboards" / "production-overview.json"
NGINX_CONFIG = REPOSITORY_ROOT / "production" / "edge" / "nginx.conf"
EDGE_DOCKERFILE = REPOSITORY_ROOT / "production" / "edge" / "Dockerfile"
METRICS_SOURCE = REPOSITORY_ROOT / "backend" / "app" / "core" / "metrics.py"
BACKUP_SCRIPT = REPOSITORY_ROOT / "production" / "scripts" / "backup.sh"
RUNBOOKS = REPOSITORY_ROOT / "doc" / "runbooks"

_AURIS_METRIC_PATTERN = re.compile(r"\bauris_[a-z0-9_:]+\b")
_PROMQL_MATCHER_LABEL_PATTERN = re.compile(
    r"(?:^|[,{])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=~|!~|!=|=)",
)
_PROMQL_GROUP_LABEL_PATTERN = re.compile(r"\b(?:by|without)\s*\(([^)]*)\)")
_ALLOWED_METRIC_LABELS = frozenset(
    {
        "action",
        "dependency",
        "le",
        "method",
        "outcome",
        "reason",
        "route",
        "status",
        "status_class",
    }
)
_FORBIDDEN_HIGH_CARDINALITY_LABELS = frozenset(
    {
        "audio_id",
        "customer_id",
        "event_id",
        "id",
        "project_id",
        "request_id",
        "session_id",
        "span_id",
        "tenant_id",
        "trace_id",
        "user_id",
    }
)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects duplicate keys instead of silently overriding them."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path) -> object:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)


def _registered_metric_definitions() -> dict[str, tuple[str, frozenset[str]]]:
    tree = ast.parse(
        METRICS_SOURCE.read_text(encoding="utf-8"), filename=str(METRICS_SOURCE)
    )
    definitions: dict[str, tuple[str, frozenset[str]]] = {}
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if not isinstance(call.func, ast.Name) or call.func.id not in {
            "Counter",
            "Gauge",
            "Histogram",
        }:
            continue
        if not call.args or not isinstance(call.args[0], ast.Constant):
            continue
        name = call.args[0].value
        if not isinstance(name, str) or not name.startswith("auris_"):
            continue
        labels: frozenset[str] = frozenset()
        if len(call.args) >= 3:
            label_value = ast.literal_eval(call.args[2])
            assert isinstance(label_value, tuple)
            labels = frozenset(label_value)
        definitions[name] = (call.func.id, labels)
    return definitions


def _exposed_auris_metric_names() -> set[str]:
    names: set[str] = set()
    for name, (kind, _) in _registered_metric_definitions().items():
        if kind == "Counter":
            names.add(f"{name}_total")
        elif kind == "Histogram":
            names.update({f"{name}_bucket", f"{name}_count", f"{name}_sum"})
        else:
            names.add(name)
    names.update(
        _AURIS_METRIC_PATTERN.findall(BACKUP_SCRIPT.read_text(encoding="utf-8"))
    )
    return names


def _rules(document: object) -> list[dict[str, object]]:
    assert isinstance(document, dict)
    groups = document.get("groups")
    assert isinstance(groups, list) and groups
    rules: list[dict[str, object]] = []
    for group in groups:
        assert isinstance(group, dict)
        assert isinstance(group.get("name"), str)
        group_rules = group.get("rules")
        assert isinstance(group_rules, list) and group_rules
        assert all(isinstance(rule, dict) for rule in group_rules)
        rules.extend(group_rules)
    return rules


def _promql_labels(expression: str) -> set[str]:
    labels = set(_PROMQL_MATCHER_LABEL_PATTERN.findall(expression))
    for group in _PROMQL_GROUP_LABEL_PATTERN.findall(expression):
        labels.update(label.strip() for label in group.split(",") if label.strip())
    return labels


def _markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    slug_counts: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        heading = re.sub(r"[`*_~]", "", heading).casefold().strip()
        slug = re.sub(r"[^\w\s-]", "", heading)
        slug = re.sub(r"\s+", "-", slug).strip("-")
        duplicate = slug_counts[slug]
        slug_counts[slug] += 1
        anchors.add(f"{slug}-{duplicate}" if duplicate else slug)
    return anchors


def _nginx_location_body(config: str, path: str) -> str:
    match = re.search(rf"location\s+=\s+{re.escape(path)}\s*\{{", config)
    assert match is not None, f"missing exact nginx location for {path}"
    opening_brace = config.find("{", match.start())
    depth = 0
    for index in range(opening_brace, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[opening_brace + 1 : index]
    raise AssertionError(f"unterminated nginx location for {path}")


def test_prometheus_config_and_alert_rules_are_strictly_parseable() -> None:
    prometheus = _load_yaml(PROMETHEUS_CONFIG)
    alerts = _load_yaml(ALERT_RULES)

    assert isinstance(prometheus, dict)
    assert prometheus["rule_files"] == ["/etc/prometheus/alerts.yaml"]
    scrape_configs = prometheus.get("scrape_configs")
    assert isinstance(scrape_configs, list)
    jobs = {config["job_name"]: config for config in scrape_configs}
    assert jobs["auris-flow-bff"]["metrics_path"] == "/metrics"
    assert jobs["auris-flow-bff"]["static_configs"] == [{"targets": ["bff:8000"]}]
    assert "edge" not in {
        target
        for config in scrape_configs
        for static_config in config.get("static_configs", [])
        for target in static_config.get("targets", [])
    }
    assert _rules(alerts)


def test_compose_wires_application_and_dagster_traces_to_the_internal_collector() -> (
    None
):
    # Compose intentionally overrides selected values inherited from YAML anchors
    # (for example one-shot restart policies), so use the standard merge-aware
    # loader here. Duplicate-key rejection remains active for the standalone
    # observability documents, where overrides are not part of the format.
    compose = yaml.safe_load(COMPOSE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    services = compose.get("services")
    assert isinstance(services, dict)

    expected_service_names = {
        "bff": "auris-flow-bff",
        "worker": "auris-flow-worker",
        "dagster-code": "auris-flow-dagster-code",
    }
    for service_name, otel_service_name in expected_service_names.items():
        service = services.get(service_name)
        assert isinstance(service, dict)
        environment = service.get("environment")
        assert isinstance(environment, dict)
        assert environment.get("OTEL_ENABLED") == "true"
        assert (
            environment.get("OTEL_EXPORTER_OTLP_ENDPOINT")
            == "http://otel-collector:4318/v1/traces"
        )
        assert environment.get("OTEL_SERVICE_NAME") == otel_service_name

        dependencies = service.get("depends_on")
        assert isinstance(dependencies, dict)
        assert dependencies.get("otel-collector") == {"condition": "service_started"}

    collector = services.get("otel-collector")
    assert isinstance(collector, dict)
    assert not collector.get("ports"), (
        "collector endpoints must remain on the internal network"
    )
    assert collector.get("depends_on") == {"tempo": {"condition": "service_healthy"}}


def test_collector_redacts_sensitive_attributes_before_exporting_to_tempo() -> None:
    collector = _load_yaml(OTEL_COLLECTOR_CONFIG)
    assert isinstance(collector, dict)

    processors = collector.get("processors")
    assert isinstance(processors, dict)
    redaction = processors.get("attributes/redact")
    assert isinstance(redaction, dict)
    actions = redaction.get("actions")
    assert isinstance(actions, list)
    deleted_attributes = {
        action.get("key")
        for action in actions
        if isinstance(action, dict) and action.get("action") == "delete"
    }
    assert {
        "http.request.header.authorization",
        "http.request.header.cookie",
        "http.response.header.set-cookie",
        "db.statement",
        "url.query",
    } <= deleted_attributes

    service = collector.get("service")
    assert isinstance(service, dict)
    pipelines = service.get("pipelines")
    assert isinstance(pipelines, dict)
    traces = pipelines.get("traces")
    assert isinstance(traces, dict)
    assert traces.get("receivers") == ["otlp"]
    assert "attributes/redact" in traces.get("processors", [])
    assert traces.get("exporters") == ["otlp/tempo"]

    exporters = collector.get("exporters")
    assert isinstance(exporters, dict)
    tempo = exporters.get("otlp/tempo")
    assert isinstance(tempo, dict)
    assert tempo.get("endpoint") == "tempo:4317"


def test_collector_internal_metrics_use_the_pinned_version_reader_schema() -> None:
    collector = _load_yaml(OTEL_COLLECTOR_CONFIG)
    assert isinstance(collector, dict)
    service = collector.get("service")
    assert isinstance(service, dict)
    telemetry = service.get("telemetry")
    assert isinstance(telemetry, dict)
    metrics = telemetry.get("metrics")
    assert isinstance(metrics, dict)
    assert "address" not in metrics
    assert metrics.get("readers") == [
        {
            "pull": {
                "exporter": {
                    "prometheus": {
                        "host": "0.0.0.0",
                        "port": 8888,
                    }
                }
            }
        }
    ]


def test_alerts_only_reference_existing_low_cardinality_auris_metrics() -> None:
    definitions = _registered_metric_definitions()
    assert definitions
    for name, (_, labels) in definitions.items():
        assert labels <= _ALLOWED_METRIC_LABELS, (
            f"{name} has unbounded or unknown labels: {labels}"
        )
        assert not labels & _FORBIDDEN_HIGH_CARDINALITY_LABELS

    known_metrics = _exposed_auris_metric_names()
    alert_rules = _rules(_load_yaml(ALERT_RULES))
    assert len({rule["alert"] for rule in alert_rules}) == len(alert_rules)
    for rule in alert_rules:
        assert rule.get("for")
        alert_labels = rule.get("labels")
        assert isinstance(alert_labels, dict)
        assert alert_labels.get("severity") in {"warning", "critical"}
        expression = rule.get("expr")
        assert isinstance(expression, str) and expression.strip()
        referenced = set(_AURIS_METRIC_PATTERN.findall(expression))
        assert referenced <= known_metrics, (
            f"{rule['alert']} references Auris metrics not emitted by the application or "
            f"backup textfile collector: {sorted(referenced - known_metrics)}"
        )
        assert not _promql_labels(expression) & _FORBIDDEN_HIGH_CARDINALITY_LABELS


def test_slo_and_metrics_collection_alerts_cover_emitted_operational_signals() -> None:
    rules = {str(rule["alert"]): rule for rule in _rules(_load_yaml(ALERT_RULES))}

    expected_by_alert = {
        "AurisApiP95Latency": (
            "auris_http_request_duration_seconds_bucket",
            "> 0.75",
            "10m",
        ),
        "AurisOutboxDeliveryDelayed": (
            "auris_outbox_oldest_pending_age_seconds",
            "> 300",
            "5m",
        ),
        "AurisMetricsCollectionFailed": (
            "auris_metrics_collection_success",
            "== 0",
            "5m",
        ),
        "AurisOutboxDeadLetters": (
            "auris_worker_processing_total",
            "increase(",
            "1m",
        ),
        "AurisTaskRunP95Duration": (
            "auris_task_run_duration_window_seconds_bucket",
            "> 900",
            "10m",
        ),
        "AurisTaskRunDeadlineOverdue": (
            "auris_task_run_deadline_overdue",
            "> 0",
            "5m",
        ),
        "AurisTaskRunStatusSyncOverdue": (
            "auris_task_run_status_sync_overdue",
            "> 0",
            "5m",
        ),
        "AurisRateLimitBackendUnavailable": (
            "auris_rate_limit_decisions_total",
            "increase(",
            "2m",
        ),
    }
    assert expected_by_alert.keys() <= rules.keys()
    for alert_name, (metric_name, threshold, pending_for) in expected_by_alert.items():
        rule = rules[alert_name]
        expression = str(rule["expr"])
        assert metric_name in expression
        assert threshold in " ".join(expression.split())
        assert rule["for"] == pending_for

    for alert_name in ("AurisApiHighErrorRate", "AurisApiP95Latency"):
        assert 'route=~"/api/v1/.*"' in str(rules[alert_name]["expr"])

    assert 'outcome=~"failure|retry|dead_letter"' in str(
        rules["AurisCallbackFailureRate"]["expr"]
    )


def test_capacity_alerts_only_use_writable_real_host_filesystems() -> None:
    rules = {str(rule["alert"]): rule for rule in _rules(_load_yaml(ALERT_RULES))}
    for alert_name in (
        "AurisHostDiskWillFill",
        "AurisHostDiskCritical",
        "AurisHostDiskWillFillIn24Hours",
    ):
        expression = str(rules[alert_name]["expr"])
        assert "node_filesystem_avail_bytes" in expression
        assert "node_filesystem_readonly" in expression
        assert "squashfs" in expression


def test_node_exporter_scrapes_the_host_filesystem_backing_named_volumes() -> None:
    prometheus = _load_yaml(PROMETHEUS_CONFIG)
    assert isinstance(prometheus, dict)
    scrape_configs = prometheus.get("scrape_configs")
    assert isinstance(scrape_configs, list)
    jobs = {config["job_name"]: config for config in scrape_configs}
    assert jobs["node"]["static_configs"] == [{"targets": ["node-exporter:9100"]}]

    compose = yaml.safe_load(COMPOSE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    node_exporter = compose["services"]["node-exporter"]
    assert not node_exporter.get("ports")
    assert "--path.rootfs=/host" in node_exporter["command"]
    host_mounts = [
        volume
        for volume in node_exporter["volumes"]
        if isinstance(volume, dict) and volume.get("target") == "/host"
    ]
    assert host_mounts == [
        {
            "type": "bind",
            "source": "/",
            "target": "/host",
            "read_only": True,
            "bind": {"propagation": "rslave"},
        }
    ]
    metrics_mounts = [
        volume
        for volume in node_exporter["volumes"]
        if isinstance(volume, dict)
        and volume.get("target") == "/var/lib/node_exporter/textfile_collector"
    ]
    assert metrics_mounts == [
        {
            "type": "bind",
            "source": "${AURIS_RUNTIME_METRICS_DIR:-./runtime-metrics}",
            "target": "/var/lib/node_exporter/textfile_collector",
            "read_only": True,
        }
    ]


def test_every_alert_runbook_link_resolves_to_an_existing_markdown_anchor() -> None:
    for rule in _rules(_load_yaml(ALERT_RULES)):
        annotations = rule.get("annotations")
        assert isinstance(annotations, dict)
        runbook = annotations.get("runbook")
        assert isinstance(runbook, str) and runbook
        parsed = urlsplit(runbook)
        assert not parsed.scheme and not parsed.netloc and not parsed.query
        assert parsed.fragment, (
            f"{rule['alert']} must link to a specific runbook anchor"
        )

        relative_path = Path(unquote(parsed.path))
        assert not relative_path.is_absolute() and ".." not in relative_path.parts
        target = (RUNBOOKS / relative_path).resolve()
        assert target.is_relative_to(RUNBOOKS.resolve())
        assert target.is_file(), f"missing runbook for {rule['alert']}: {target}"
        fragment = unquote(parsed.fragment).casefold()
        assert fragment in _markdown_anchors(target), (
            f"missing runbook anchor for {rule['alert']}: {runbook}"
        )


def test_grafana_dashboard_core_panels_use_real_auris_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panels = dashboard.get("panels")
    assert isinstance(panels, list) and panels
    assert len({panel["id"] for panel in panels}) == len(panels)
    assert len({panel["title"] for panel in panels}) == len(panels)

    expressions_by_title: dict[str, str] = {}
    all_expressions: list[str] = []
    for panel in panels:
        assert panel.get("datasource", {}).get("uid") == "prometheus"
        targets = panel.get("targets")
        assert isinstance(targets, list) and targets
        expressions = [target["expr"] for target in targets]
        assert all(
            isinstance(expression, str) and expression for expression in expressions
        )
        expressions_by_title[panel["title"]] = "\n".join(expressions)
        all_expressions.extend(expressions)

    core_metrics_by_panel = {
        "API request rate": "auris_http_requests_total",
        "API P95 latency": "auris_http_request_duration_seconds_bucket",
        "Outbox delivery": "auris_outbox_pending",
        "Dependency health": "auris_dependency_ready",
        "TaskRun completion": "auris_task_run_terminal",
        "TaskRun P95 duration (24h)": "auris_task_run_duration_window_seconds_bucket",
        "Rate-limit decisions": "auris_rate_limit_decisions_total",
        "Callback outcomes": "auris_callback_outcomes_total",
        "Storage filesystem free ratio": "node_filesystem_avail_bytes",
    }
    assert core_metrics_by_panel.keys() <= expressions_by_title.keys()
    for panel_title, metric in core_metrics_by_panel.items():
        assert metric in expressions_by_title[panel_title]

    referenced = {
        metric
        for expression in all_expressions
        for metric in _AURIS_METRIC_PATTERN.findall(expression)
    }
    assert referenced <= _exposed_auris_metric_names()
    assert not set().union(*(_promql_labels(expr) for expr in all_expressions)) & (
        _FORBIDDEN_HIGH_CARDINALITY_LABELS
    )


def test_edge_blocks_metrics_and_proxies_strict_readiness_to_bff() -> None:
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    metrics = _nginx_location_body(config, "/metrics")
    readiness = _nginx_location_body(config, "/readyz")

    assert re.search(r"\breturn\s+404\s*;", metrics)
    assert "proxy_pass" not in metrics
    assert re.search(r"\bproxy_pass\s+http://bff:8000/readyz\s*;", readiness)
    assert re.search(r"\bproxy_read_timeout\s+5s\s*;", readiness)
    assert re.search(r"\bproxy_set_header\s+X-Forwarded-Proto\s+https\s*;", readiness)


def test_edge_breaks_oidc_readiness_cycle_on_internal_https_port() -> None:
    compose = yaml.safe_load(COMPOSE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    services = compose.get("services")
    assert isinstance(services, dict)
    edge = services.get("edge")
    assert isinstance(edge, dict)

    # The BFF resolves the public OIDC issuer to this internal network alias. The
    # edge must therefore be able to start before BFF readiness succeeds and
    # must listen on the issuer's default HTTPS port inside the Compose network.
    assert edge.get("depends_on", {}).get("bff") == {"condition": "service_started"}
    assert edge.get("depends_on", {}).get("keycloak") == {
        "condition": "service_healthy"
    }
    assert edge.get("cap_drop") == ["ALL"]
    assert edge.get("cap_add") == ["NET_BIND_SERVICE"]
    assert "${AURIS_HTTPS_PORT:-443}:443" in edge.get("ports", [])

    nginx_config = NGINX_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^\s*listen\s+443\s+ssl\s*;", nginx_config, re.MULTILINE)
    dockerfile = EDGE_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^EXPOSE\s+8080\s+443\s*$", dockerfile, re.MULTILINE)
