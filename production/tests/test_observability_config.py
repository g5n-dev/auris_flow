from __future__ import annotations

import ast
import base64
import importlib.util
import json
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = REPOSITORY_ROOT / "production" / "observability"
COMPOSE_CONFIG = REPOSITORY_ROOT / "production" / "compose.yaml"
OTEL_COLLECTOR_CONFIG = OBSERVABILITY / "otel-collector.yaml"
PROMETHEUS_CONFIG = OBSERVABILITY / "prometheus.yaml"
ALERTMANAGER_CONFIG = OBSERVABILITY / "alertmanager.yaml"
ALERTMANAGER_ENTRYPOINT = OBSERVABILITY / "alertmanager-entrypoint.sh"
ALERT_RULES = OBSERVABILITY / "alerts.yaml"
ALERT_RULE_TESTS = OBSERVABILITY / "alerts.test.yaml"
DASHBOARD = OBSERVABILITY / "grafana" / "dashboards" / "production-overview.json"
NGINX_CONFIG = REPOSITORY_ROOT / "production" / "edge" / "nginx.conf"
EDGE_DOCKERFILE = REPOSITORY_ROOT / "production" / "edge" / "Dockerfile"
METRICS_SOURCE = REPOSITORY_ROOT / "backend" / "app" / "core" / "metrics.py"
OBSERVABILITY_HEALTH_PROBE = (
    REPOSITORY_ROOT / "backend" / "scripts" / "check_observability_health.py"
)
BACKUP_SCRIPT = REPOSITORY_ROOT / "production" / "scripts" / "backup.sh"
RULE_VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_observability_rules.sh"
ALERTMANAGER_VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_alertmanager_config.sh"
RELEASE_VERIFIER = REPOSITORY_ROOT / "scripts" / "verify_release.sh"
INIT_SECRETS = REPOSITORY_ROOT / "production" / "scripts" / "init-secrets.sh"
PRODUCTION_README = REPOSITORY_ROOT / "production" / "README.md"
DEPLOYMENT_BUNDLE_README = (
    REPOSITORY_ROOT / "production" / "deployment-bundle.README.md"
)
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


def _load_observability_health_probe():
    module_name = "auris_observability_health_probe_test"
    spec = importlib.util.spec_from_file_location(
        module_name, OBSERVABILITY_HEALTH_PROBE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


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


def _nginx_prefix_location_body(config: str, path: str) -> str:
    match = re.search(rf"location\s+{re.escape(path)}\s*\{{", config)
    assert match is not None, f"missing nginx prefix location for {path}"
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


def test_alertmanager_routes_every_alert_to_a_secret_backed_generic_webhook() -> None:
    document = _load_yaml(ALERTMANAGER_CONFIG)
    assert isinstance(document, dict)
    route = document.get("route")
    assert isinstance(route, dict)
    receiver_name = route.get("receiver")
    assert isinstance(receiver_name, str) and receiver_name.casefold() not in {
        "",
        "null",
    }

    receivers = document.get("receivers")
    assert isinstance(receivers, list) and receivers
    receiver = next(
        item
        for item in receivers
        if isinstance(item, dict) and item.get("name") == receiver_name
    )
    webhook_configs = receiver.get("webhook_configs")
    assert isinstance(webhook_configs, list) and len(webhook_configs) == 1
    webhook = webhook_configs[0]
    assert isinstance(webhook, dict)
    assert webhook.get("url_file") == "/run/secrets/alertmanager_webhook_url"
    assert "url" not in webhook
    assert webhook.get("send_resolved") is True
    assert webhook.get("max_alerts") == 100
    assert webhook.get("http_config") == {
        "follow_redirects": False,
        "enable_http2": True,
    }
    assert not re.search(
        r"https?://",
        ALERTMANAGER_CONFIG.read_text(encoding="utf-8"),
        re.IGNORECASE,
    )


def test_prometheus_routes_alerts_to_and_scrapes_the_internal_alertmanager() -> None:
    prometheus = _load_yaml(PROMETHEUS_CONFIG)
    assert isinstance(prometheus, dict)
    assert prometheus.get("alerting") == {
        "alertmanagers": [
            {
                "static_configs": [
                    {"targets": ["alertmanager:9093"]},
                ]
            }
        ]
    }
    scrape_configs = prometheus.get("scrape_configs")
    assert isinstance(scrape_configs, list)
    jobs = {config["job_name"]: config for config in scrape_configs}
    assert jobs["alertmanager"]["static_configs"] == [
        {"targets": ["alertmanager:9093"]}
    ]


def test_compose_alertmanager_is_hardened_persistent_and_secret_backed() -> None:
    compose = yaml.safe_load(COMPOSE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    services = compose.get("services")
    assert isinstance(services, dict)
    alertmanager = services.get("alertmanager")
    assert isinstance(alertmanager, dict)

    assert alertmanager.get("image") == (
        "${ALERTMANAGER_IMAGE:-prom/alertmanager:v0.28.1}"
    )
    assert alertmanager.get("user") == "65534:65534"
    assert alertmanager.get("read_only") is True
    assert alertmanager.get("init") is True
    assert alertmanager.get("restart") == "unless-stopped"
    assert alertmanager.get("cap_drop") == ["ALL"]
    assert alertmanager.get("security_opt") == ["no-new-privileges:true"]
    assert set(alertmanager.get("networks", [])) == {"internal", "app-egress"}
    assert not alertmanager.get("ports")
    assert not alertmanager.get("environment")
    assert alertmanager.get("volumes") == ["alertmanager_data:/alertmanager"]

    command = alertmanager.get("command")
    assert isinstance(command, list)
    assert "--config.file=/etc/alertmanager/alertmanager.yaml" in command
    assert "--storage.path=/alertmanager" in command
    assert "--cluster.listen-address=" in command
    assert "--enable-feature=utf8-strict-mode" in command
    assert not any("webhook" in str(argument).casefold() for argument in command)

    assert alertmanager.get("configs") == [
        {
            "source": "alertmanager_config",
            "target": "/etc/alertmanager/alertmanager.yaml",
            "mode": 0o444,
        },
        {
            "source": "alertmanager_entrypoint",
            "target": "/etc/alertmanager/alertmanager-entrypoint.sh",
            "mode": 0o555,
        },
    ]
    assert alertmanager.get("secrets") == [
        {
            "source": "alertmanager_webhook_url",
            "target": "alertmanager_webhook_url",
            "uid": "65534",
            "gid": "65534",
            "mode": 0o400,
        }
    ]
    assert alertmanager.get("healthcheck", {}).get("test") == [
        "CMD",
        "/bin/wget",
        "-q",
        "-O",
        "/dev/null",
        "http://127.0.0.1:9093/-/ready",
    ]

    prometheus = services.get("prometheus")
    assert isinstance(prometheus, dict)
    assert prometheus.get("depends_on", {}).get("alertmanager") == {
        "condition": "service_healthy"
    }
    assert compose.get("configs", {}).get("alertmanager_config") == {
        "file": "./observability/alertmanager.yaml"
    }
    assert compose.get("configs", {}).get("alertmanager_entrypoint") == {
        "file": "./observability/alertmanager-entrypoint.sh"
    }
    assert compose.get("secrets", {}).get("alertmanager_webhook_url") == {
        "file": "${AURIS_SECRETS_DIR:-./secrets}/alertmanager_webhook_url"
    }
    assert "alertmanager_data" in compose.get("volumes", {})


def test_alertmanager_webhook_secret_is_operator_supplied_and_fails_closed() -> None:
    entrypoint = ALERTMANAGER_ENTRYPOINT.read_text(encoding="utf-8")
    init_secrets = INIT_SECRETS.read_text(encoding="utf-8")
    readme = PRODUCTION_README.read_text(encoding="utf-8")

    assert "alertmanager_webhook_url" in entrypoint
    assert "[ ! -s" in entrypoint
    assert "exit 2" in entrypoint
    assert "alertmanager_webhook_url" not in init_secrets
    assert "alertmanager_webhook_url" in readme
    assert "${EDITOR:?set EDITOR}" in readme
    assert "chmod 444" in readme


def test_deployment_bundle_requires_alertmanager_secret_and_starts_service() -> None:
    readme = DEPLOYMENT_BUNDLE_README.read_text(encoding="utf-8")

    assert "production/secrets/alertmanager_webhook_url" in readme
    assert "init-secrets.sh" in readme
    assert readme.index("init-secrets.sh") < readme.index(
        "production/secrets/alertmanager_webhook_url"
    )
    assert "test -s production/secrets/alertmanager_webhook_url" in readme
    assert "chmod 444 production/secrets/alertmanager_webhook_url" in readme
    assert "mysql redis minio qdrant tempo node-exporter alertmanager" in " ".join(
        readme.split()
    )


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
    assert collector.get("depends_on") == {"tempo": {"condition": "service_started"}}


def test_observability_health_uses_live_endpoints_instead_of_version_commands() -> None:
    compose = yaml.safe_load(COMPOSE_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(compose, dict)
    services = compose.get("services")
    assert isinstance(services, dict)

    source = OBSERVABILITY_HEALTH_PROBE.read_text(encoding="utf-8")
    for endpoint in (
        "http://otel-collector:13133/",
        "http://tempo:3200/ready",
        "http://prometheus:9090/-/ready",
        "http://alertmanager:9093/-/ready",
        "http://node-exporter:9100/metrics",
    ):
        assert endpoint in source
    assert "open_url_no_redirect" in source
    assert "node_exporter_build_info" in source
    assert "http://tempo:3200/api/traces/{trace_id}" in source
    assert "ThreadingHTTPServer" in source

    probe = services.get("observability-health")
    assert isinstance(probe, dict)
    assert probe.get("image") == "${AURIS_BFF_IMAGE:-auris-flow-bff:dev}"
    assert probe.get("read_only") is True
    assert probe.get("cap_drop") == ["ALL"]
    assert probe.get("healthcheck", {}).get("test") == [
        "CMD",
        "python",
        "/app/scripts/check_observability_health.py",
        "--check-server",
    ]
    assert probe.get("command") == [
        "python",
        "/app/scripts/check_observability_health.py",
        "--serve",
    ]
    assert probe.get("expose") == ["8080"]
    assert set(probe.get("depends_on", {})) == {
        "alertmanager",
        "node-exporter",
        "otel-collector",
        "prometheus",
        "tempo",
    }
    assert all(
        dependency == {"condition": "service_started"}
        for dependency in probe["depends_on"].values()
    )

    for service_name in ("bff", "worker", "dagster-code"):
        assert services[service_name]["depends_on"]["observability-health"] == {
            "condition": "service_healthy"
        }
    assert "healthcheck" not in services["otel-collector"]
    assert "healthcheck" not in services["tempo"]
    prometheus_health = services["prometheus"]["healthcheck"]["test"]
    assert "--version" not in " ".join(prometheus_health)
    assert "http://127.0.0.1:9090/-/ready" in prometheus_health

    prometheus = _load_yaml(PROMETHEUS_CONFIG)
    assert isinstance(prometheus, dict)
    scrape_configs = prometheus.get("scrape_configs")
    assert isinstance(scrape_configs, list)
    jobs = {config["job_name"]: config for config in scrape_configs}
    assert jobs["tempo"]["static_configs"] == [{"targets": ["tempo:3200"]}]

    rules = {str(rule["alert"]): rule for rule in _rules(_load_yaml(ALERT_RULES))}
    target_down = str(rules["AurisTargetDown"]["expr"])
    for job in ("alertmanager", "prometheus", "tempo"):
        assert job in target_down


def test_observability_pipeline_monitor_recovers_in_background(monkeypatch) -> None:
    probe = _load_observability_health_probe()
    first_attempt = threading.Event()
    recovered_attempt = threading.Event()
    calls = 0

    def deep_probe(_exporter, _tracer) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_attempt.set()
            raise KeyError("simulated unexpected collector response")
        recovered_attempt.set()

    monkeypatch.setattr(probe, "_deep_probe", deep_probe)
    monkeypatch.setattr(probe, "_MONITOR_INTERVAL_SECONDS", 0.01)
    monitor = probe._PipelineMonitor(object(), object())
    monitor.start()
    try:
        assert first_attempt.wait(timeout=1.0)
        assert recovered_attempt.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        ready, age = monitor.snapshot()
        while not ready and time.monotonic() < deadline:
            time.sleep(0.01)
            ready, age = monitor.snapshot()
        assert ready is True
        assert 0.0 <= age < 1.0
    finally:
        monitor.stop()


def _tempo_marker_payload(
    *,
    trace_id: str,
    service_name: str = "auris-flow-observability-health",
    span_name: str = "auris_flow.observability.pipeline.readiness",
) -> dict[str, object]:
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": service_name},
                        }
                    ]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "name": span_name,
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_tempo_marker_validation_requires_exact_trace_span_and_service() -> None:
    probe = _load_observability_health_probe()
    trace_id = "12" * 16
    valid = _tempo_marker_payload(trace_id=trace_id)

    assert probe._tempo_contains_marker(
        valid,
        trace_id=trace_id,
        expected_service="auris-flow-observability-health",
    )
    base64_payload = _tempo_marker_payload(
        trace_id=base64.b64encode(bytes.fromhex(trace_id)).decode("ascii")
    )
    assert probe._tempo_contains_marker(
        base64_payload,
        trace_id=trace_id,
        expected_service="auris-flow-observability-health",
    )
    for invalid in (
        {},
        {"batches": []},
        _tempo_marker_payload(trace_id="34" * 16),
        _tempo_marker_payload(trace_id=trace_id, service_name="wrong-service"),
        _tempo_marker_payload(trace_id=trace_id, span_name="wrong.span"),
    ):
        assert not probe._tempo_contains_marker(
            invalid,
            trace_id=trace_id,
            expected_service="auris-flow-observability-health",
        )


def test_tempo_visibility_rejects_an_unrelated_success_payload(monkeypatch) -> None:
    probe = _load_observability_health_probe()
    requested_trace_id = "56" * 16
    unrelated = _tempo_marker_payload(trace_id="78" * 16)
    monkeypatch.setattr(
        probe,
        "_read_bounded_response",
        lambda *_args, **_kwargs: json.dumps(unrelated).encode("utf-8"),
    )

    assert (
        probe._tempo_trace_is_visible(
            requested_trace_id,
            expected_service="auris-flow-observability-health",
        )
        is False
    )


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
        "http.target",
        "url.path",
        "url.query",
        "client.address",
        "client.port",
        "network.peer.address",
        "network.peer.port",
        "net.peer.ip",
        "net.peer.port",
        "http.client_ip",
        "http.user_agent",
        "user_agent.original",
        "enduser.id",
        "user.id",
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
        "AurisOutboxP95DeliveryLatency": (
            "auris_outbox_delivery_duration_window_seconds_bucket",
            "> 60",
            "10m",
        ),
        "AurisMetricsCollectionFailed": (
            "auris_metrics_collection_success",
            "== 0",
            "5m",
        ),
        "AurisOutboxDeadLetters": (
            "auris_outbox_dead_letter_recent",
            "> 0",
            "1m",
        ),
        "AurisOutboxDeadLettersUnresolved": (
            "auris_outbox_dead_letter",
            "> 0",
            "10m",
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
        "AurisDependencyNotReady": (
            "auris_dependency_ready",
            "== 0",
            "2m",
        ),
        "AurisTelemetryExportFailures": (
            "otelcol_exporter_send_failed_spans_total",
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

    backup_expression = " ".join(str(rules["AurisBackupStale"]["expr"]).split())
    assert "auris_backup_last_success_timestamp_seconds" in backup_expression
    assert "absent(auris_backup_last_success_timestamp_seconds)" in backup_expression


def test_promtool_rule_scenarios_cover_required_failure_and_recovery_signals() -> None:
    document = _load_yaml(ALERT_RULE_TESTS)
    assert isinstance(document, dict)
    assert document.get("rule_files") == ["alerts.yaml"]
    tests = document.get("tests")
    assert isinstance(tests, list) and tests

    covered_alerts = {
        evaluation["alertname"]
        for test in tests
        if isinstance(test, dict)
        for evaluation in test.get("alert_rule_test", [])
        if isinstance(evaluation, dict)
        if isinstance(evaluation.get("alertname"), str)
        and isinstance(evaluation.get("exp_alerts"), list)
        and evaluation["exp_alerts"]
    }
    required_alerts = {
        "AurisDependencyNotReady",
        "AurisOutboxDeadLetters",
        "AurisOutboxDeadLettersUnresolved",
        "AurisOutboxP95DeliveryLatency",
        "AurisAuthenticationFailureSpike",
        "AurisHostDiskCritical",
        "AurisBackupStale",
        "AurisTelemetryExportFailures",
    }
    assert required_alerts <= covered_alerts

    recovered_alerts = {
        evaluation["alertname"]
        for test in tests
        if isinstance(test, dict)
        for evaluation in test.get("alert_rule_test", [])
        if isinstance(evaluation, dict)
        and isinstance(evaluation.get("alertname"), str)
        and evaluation.get("exp_alerts") == []
    }
    assert required_alerts <= recovered_alerts


def test_release_gate_executes_pinned_promtool_in_a_sandboxed_container() -> None:
    source = RULE_VERIFIER.read_text(encoding="utf-8")
    release = RELEASE_VERIFIER.read_text(encoding="utf-8")

    assert "prom/prometheus:v3.4.1" in source
    assert "--network none" in source
    assert "--read-only" in source
    assert "--cap-drop ALL" in source
    assert "no-new-privileges:true" in source
    assert "check config prometheus.yaml" in source
    assert "check rules alerts.yaml" in source
    assert "test rules alerts.test.yaml" in source
    assert "bash scripts/verify_observability_rules.sh" in release


def test_release_gate_executes_pinned_utf8_strict_amtool_and_missing_secret_probe() -> (
    None
):
    source = ALERTMANAGER_VERIFIER.read_text(encoding="utf-8")
    release = RELEASE_VERIFIER.read_text(encoding="utf-8")

    assert (
        "prom/alertmanager:v0.28.1@sha256:"
        "27c475db5fb156cab31d5c18a4251ac7ed567746a2483ff264516437a39b15ba" in source
    )
    assert "--network none" in source
    assert "--read-only" in source
    assert "--cap-drop ALL" in source
    assert "no-new-privileges:true" in source
    assert "--entrypoint /bin/amtool" in source
    assert "check-config" in source
    assert "utf8-strict-mode" in source
    assert "alertmanager missing-secret probe unexpectedly succeeded" in source
    assert "bash scripts/verify_alertmanager_config.sh" in release


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
    assert re.search(r"\blimit_req\s+zone=auris_readiness\b", readiness)
    assert re.search(r"\bproxy_cache\s+auris_readyz\s*;", readiness)
    assert re.search(r"\bproxy_cache_valid\s+200\s+5s\s*;", readiness)
    assert re.search(r"\bproxy_cache_lock\s+on\s*;", readiness)
    lock_timeout = re.search(r"\bproxy_cache_lock_timeout\s+(\d+)s\s*;", readiness)
    assert lock_timeout is not None
    assert int(lock_timeout.group(1)) >= 6
    assert re.search(r"\bproxy_cache_lock_age\s+6s\s*;", readiness)
    for header in ("traceparent", "tracestate", "baggage"):
        assert re.search(
            rf'\bproxy_set_header\s+{header}\s+""\s*;',
            readiness,
        )


def test_edge_preserves_business_traceparent_but_drops_untrusted_trace_metadata() -> (
    None
):
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    audio = _nginx_location_body(config, "/api/v1/audio-playback")
    api = _nginx_prefix_location_body(config, "/api/")

    for location in (audio, api):
        assert re.search(
            r"\bproxy_set_header\s+traceparent\s+\$http_traceparent\s*;",
            location,
        )
        assert not re.search(
            r'\bproxy_set_header\s+traceparent\s+""\s*;',
            location,
        )
        for header in ("tracestate", "baggage"):
            assert re.search(
                rf'\bproxy_set_header\s+{header}\s+""\s*;',
                location,
            )


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
