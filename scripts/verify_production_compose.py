#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DIR = ROOT / "production"
COMPOSE_FILE = PRODUCTION_DIR / "compose.yaml"
ENV_FILE = PRODUCTION_DIR / ".env.example"
NGINX_CONFIG = PRODUCTION_DIR / "edge" / "nginx.conf"
REQUIRED_SERVICES = frozenset(
    {
        "mysql",
        "db-bootstrap",
        "redis",
        "minio-volume-init",
        "minio",
        "minio-bootstrap",
        "qdrant",
        "qdrant-backup-tool",
        "migrate",
        "keycloak",
        "identity-bootstrap",
        "dagster-storage-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
        "bff",
        "worker",
        "otel-collector",
        "tempo",
        "observability-health",
        "alertmanager",
        "prometheus",
        "grafana",
        "node-exporter",
        "edge",
    }
)
ONE_SHOT_SERVICES = frozenset(
    {
        "db-bootstrap",
        "minio-volume-init",
        "minio-bootstrap",
        "migrate",
        "identity-bootstrap",
        "dagster-storage-bootstrap",
    }
)
PROFILE_UTILITY_SERVICES = frozenset({"qdrant-backup-tool"})
COMPOSITE_HEALTH_COVERED_SERVICES = frozenset({"otel-collector", "tempo"})
PUBLIC_SERVICES = frozenset({"edge", "keycloak", "grafana"})
LOOPBACK_OPERATOR_SERVICES = frozenset({"keycloak", "grafana"})
APP_SERVICES = frozenset({"bff", "worker"})
EXPECTED_NETWORKS_BY_SERVICE = {
    **{name: frozenset({"internal"}) for name in REQUIRED_SERVICES},
    "minio-volume-init": frozenset(),
    "bff": frozenset({"internal", "app-egress"}),
    "worker": frozenset({"internal", "app-egress"}),
    "dagster-code": frozenset({"internal", "app-egress"}),
    "alertmanager": frozenset({"internal", "app-egress"}),
    "edge": frozenset({"internal", "edge"}),
}
EXPECTED_CAPABILITIES_BY_SERVICE = {
    "edge": frozenset({"NET_BIND_SERVICE"}),
    # The official MySQL entrypoint initializes/chowns the named volume as
    # root, then switches to the image's mysql uid/gid.
    "mysql": frozenset({"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"}),
    "minio-volume-init": frozenset({"CHOWN"}),
}
SECRET_NAME_PATTERN = re.compile(
    r"(?:PASSWORD|SECRET|TOKEN|API_KEY|DATABASE_URL|REDIS_URL)$"
)
SHA256_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")
SAFE_ACCESS_LOG_VARIABLES = frozenset(
    {
        "$body_bytes_sent",
        "$request_id",
        "$request_method",
        "$request_time",
        "$status",
        "$time_iso8601",
        "$uri",
    }
)
QUERY_BEARING_LOG_VARIABLES = frozenset(
    {"$args", "$is_args", "$query_string", "$request", "$request_uri"}
)


class ComposePolicyError(RuntimeError):
    pass


def _render_compose(
    *, compose_file: Path = COMPOSE_FILE, env_file: Path = ENV_FILE
) -> dict[str, Any]:
    compose_file = compose_file.resolve()
    env_file = env_file.resolve()
    environment = os.environ.copy()
    environment.update(
        {
            "AURIS_PUBLIC_HOST": "auris.example.com",
            "AURIS_EXTERNAL_CALLBACK_URL": (
                "https://platform.example.com/callbacks/auris-flow"
            ),
            "AURIS_EXTERNAL_CALLBACK_HOST": "platform.example.com",
        }
    )
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "--file",
        str(compose_file),
        "--profile",
        "*",
        "config",
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=compose_file.parent,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ComposePolicyError("docker compose is required") from exc
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or exc.stdout or "compose rendering failed").strip()
        raise ComposePolicyError(diagnostic) from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ComposePolicyError("docker compose returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ComposePolicyError("rendered compose document must be an object")
    return payload


def _environment_map(service: dict[str, Any]) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items() if value is not None}
    raise ComposePolicyError("service environment must render as a map")


def _command_flag_value(service: dict[str, Any], flag: str) -> str | None:
    command = service.get("command") or []
    if not isinstance(command, list):
        return None
    normalized = [str(item) for item in command]
    try:
        index = normalized.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(normalized):
        return None
    return normalized[index + 1]


def _nginx_block_bodies(source: str, directive: str) -> list[str]:
    blocks: list[str] = []
    pattern = re.compile(rf"(?m)^\s*{re.escape(directive)}\s*\{{")
    for match in pattern.finditer(source):
        opening_brace = source.find("{", match.start(), match.end())
        depth = 1
        cursor = opening_brace + 1
        while cursor < len(source) and depth:
            if source[cursor] == "{":
                depth += 1
            elif source[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth == 0:
            blocks.append(source[opening_brace + 1 : cursor - 1])
    return blocks


def _direct_nginx_access_logs(server_body: str) -> list[str]:
    directives: list[str] = []
    depth = 0
    for line in server_body.splitlines():
        stripped = line.strip()
        if depth == 0 and re.match(r"^access_log\b", stripped):
            directives.append(stripped)
        depth += line.count("{") - line.count("}")
    return directives


def _nginx_location_body(source: str, path: str, *, exact: bool) -> str | None:
    modifier = r"=\s+" if exact else ""
    match = re.search(
        rf"(?m)^\s*location\s+{modifier}{re.escape(path)}\s*\{{",
        source,
    )
    if match is None:
        return None
    opening_brace = source.find("{", match.start(), match.end())
    depth = 1
    cursor = opening_brace + 1
    while cursor < len(source) and depth:
        if source[cursor] == "{":
            depth += 1
        elif source[cursor] == "}":
            depth -= 1
        cursor += 1
    return source[opening_brace + 1 : cursor - 1] if depth == 0 else None


def _nginx_header_binding(body: str, header: str, value: str) -> bool:
    return (
        re.search(
            rf"(?im)^\s*proxy_set_header\s+{re.escape(header)}\s+{re.escape(value)}\s*;",
            body,
        )
        is not None
    )


def validate_edge_nginx(source: str) -> list[str]:
    errors: list[str] = []
    log_format = re.search(
        r"\blog_format\s+auris_safe_json\s+escape=json\s+"
        r"(?P<quote>['\"])(?P<body>[^\n]*)(?P=quote)\s*;",
        source,
    )
    if log_format is None:
        errors.append("edge nginx: auris_safe_json access log format is required")
    else:
        log_body = log_format.group("body")
        variables = set(re.findall(r"\$[A-Za-z0-9_]+", log_body))
        if variables & QUERY_BEARING_LOG_VARIABLES:
            errors.append(
                "edge nginx: access log format contains query-bearing variables"
            )
        if variables != SAFE_ACCESS_LOG_VARIABLES:
            errors.append(
                "edge nginx: access log format must use only the approved safe variables"
            )
        try:
            rendered_log = json.loads(re.sub(r"\$[A-Za-z0-9_]+", "value", log_body))
        except json.JSONDecodeError:
            errors.append("edge nginx: auris_safe_json must be valid JSON")
        else:
            if (
                not isinstance(rendered_log, dict)
                or rendered_log.get("path") != "value"
            ):
                errors.append("edge nginx: auris_safe_json path must use $uri")

    server_blocks = _nginx_block_bodies(source, "server")
    access_log_directives = re.findall(
        r"(?m)^\s*(access_log\s+[^;]+;)",
        source,
    )
    if any(
        re.fullmatch(
            r"access_log\s+(?:off|\S+\s+auris_safe_json)\s*;",
            directive,
        )
        is None
        for directive in access_log_directives
    ):
        errors.append(
            "edge nginx: access_log directives must use auris_safe_json or off"
        )
    if not server_blocks or any(
        len(access_logs := _direct_nginx_access_logs(server)) != 1
        or re.fullmatch(
            r"access_log\s+\S+\s+auris_safe_json\s*;",
            access_logs[0],
        )
        is None
        for server in server_blocks
    ):
        errors.append(
            "edge nginx: every server must override access_log with auris_safe_json"
        )

    forwarded_for_values = re.findall(
        r"proxy_set_header\s+X-Forwarded-For\s+([^;]+);",
        source,
    )
    proxy_pass_count = len(re.findall(r"\bproxy_pass\s+[^;]+;", source))
    if (
        not forwarded_for_values
        or len(forwarded_for_values) != proxy_pass_count
        or any(value.strip() != "$remote_addr" for value in forwarded_for_values)
        or "$proxy_add_x_forwarded_for" in source
    ):
        errors.append(
            "edge nginx: every X-Forwarded-For header must overwrite with $remote_addr"
        )

    readiness_location = _nginx_location_body(source, "/readyz", exact=True)
    if readiness_location is None:
        errors.append("edge nginx: exact readiness location is required")
    else:
        lock_timeout = re.search(
            r"(?m)^\s*proxy_cache_lock_timeout\s+(\d+)s\s*;",
            readiness_location,
        )
        if lock_timeout is None or int(lock_timeout.group(1)) < 6:
            errors.append(
                "edge nginx: readiness cache lock timeout must be at least 6 seconds"
            )
        if (
            re.search(
                r"(?m)^\s*proxy_cache_lock_age\s+6s\s*;",
                readiness_location,
            )
            is None
        ):
            errors.append(
                "edge nginx: readiness cache lock age must be exactly 6 seconds"
            )
        for header in ("traceparent", "tracestate", "baggage"):
            if not _nginx_header_binding(readiness_location, header, '""'):
                errors.append(f"edge nginx: readiness must clear untrusted {header}")

    for path, exact in (("/api/v1/audio-playback", True), ("/api/", False)):
        business_location = _nginx_location_body(source, path, exact=exact)
        if business_location is None:
            errors.append(f"edge nginx: business location is required: {path}")
            continue
        if not _nginx_header_binding(
            business_location,
            "traceparent",
            "$http_traceparent",
        ) or _nginx_header_binding(business_location, "traceparent", '""'):
            errors.append(
                f"edge nginx: business traceparent must be preserved for {path}"
            )
        for header in ("tracestate", "baggage"):
            if not _nginx_header_binding(business_location, header, '""'):
                errors.append(
                    f"edge nginx: business {header} must be cleared for {path}"
                )

    audio_location = re.search(
        r"location\s*=\s*/api/v1/audio-playback\s*\{(?P<body>[^{}]*)\}",
        source,
        re.DOTALL,
    )
    if audio_location is None:
        errors.append("edge nginx: exact audio playback grant location is required")
    else:
        body = audio_location.group("body")
        if not re.search(r"\baccess_log\s+off\s*;", body):
            errors.append(
                "edge nginx: audio playback grant location must disable access logging"
            )
        if not re.search(r"\bproxy_pass\s+http://bff:8000\s*;", body):
            errors.append("edge nginx: audio playback location must proxy to the BFF")
        if not re.search(
            r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr\s*;",
            body,
        ):
            errors.append(
                "edge nginx: audio playback location must overwrite X-Forwarded-For"
            )
    return errors


def _validate_image(name: str, service: dict[str, Any], *, release: bool) -> list[str]:
    errors: list[str] = []
    image = str(service.get("image") or "")
    if not image:
        return [f"{name}: image is required"]
    normalized = image.partition("@")[0]
    tag = normalized.rsplit(":", 1)[1] if ":" in normalized.rsplit("/", 1)[-1] else ""
    if not tag or tag == "latest":
        errors.append(f"{name}: image must use an explicit non-latest tag")
    if release and not SHA256_REFERENCE.search(image):
        errors.append(f"{name}: release image must be pinned by sha256 digest")
    if release and service.get("build"):
        errors.append(f"{name}: release compose must consume prebuilt images")
    return errors


def validate_compose(document: dict[str, Any], *, release: bool = False) -> list[str]:
    errors: list[str] = []
    services = document.get("services")
    if not isinstance(services, dict):
        return ["compose services map is missing"]
    missing = sorted(REQUIRED_SERVICES - set(services))
    if missing:
        errors.append("missing production services: " + ", ".join(missing))
    unexpected = sorted(set(services) - REQUIRED_SERVICES)
    if unexpected:
        errors.append("unexpected production services: " + ", ".join(unexpected))

    backup_trust_secret_files = {
        "backup_manifest_signing_private_key": (
            "backup_manifest_signing_private_key.pem"
        ),
        "backup_manifest_signing_public_key": (
            "backup_manifest_signing_public_key.pem"
        ),
    }
    backup_trust = document.get("x-auris-backup-manifest-trust")
    backup_private_raw = str(
        backup_trust.get("private_key_file") if isinstance(backup_trust, dict) else ""
    )
    backup_public_raw = str(
        backup_trust.get("public_key_file") if isinstance(backup_trust, dict) else ""
    )
    backup_private_path = Path(backup_private_raw)
    backup_public_path = Path(backup_public_raw)
    if (
        not isinstance(backup_trust, dict)
        or set(backup_trust)
        != {"algorithm", "exposure", "private_key_file", "public_key_file"}
        or backup_trust.get("algorithm") != "ed25519"
        or backup_trust.get("exposure") != "host-backup-tools-only"
        or backup_private_path.name
        != backup_trust_secret_files["backup_manifest_signing_private_key"]
        or backup_public_path.name
        != backup_trust_secret_files["backup_manifest_signing_public_key"]
        or backup_private_path.parent != backup_public_path.parent
        or ".." in backup_private_path.parts
        or ".." in backup_public_path.parts
        or any(character in backup_private_raw for character in "\x00\r\n\t")
        or any(character in backup_public_raw for character in "\x00\r\n\t")
    ):
        errors.append(
            "backup manifest trust keys must use dedicated deployment secret files"
        )
    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            continue
        mounted = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in (raw_service.get("secrets") or [])
        }
        if mounted & set(backup_trust_secret_files):
            errors.append(
                f"{service_name}: backup manifest trust keys must not be mounted"
            )

    for name, raw_service in sorted(services.items()):
        if not isinstance(raw_service, dict):
            errors.append(f"{name}: service definition must be an object")
            continue
        errors.extend(_validate_image(name, raw_service, release=release))
        if (
            name not in ONE_SHOT_SERVICES
            and name not in PROFILE_UTILITY_SERVICES
            and name not in COMPOSITE_HEALTH_COVERED_SERVICES
            and not raw_service.get("healthcheck")
        ):
            errors.append(f"{name}: long-running service requires a healthcheck")
        if (
            name not in ONE_SHOT_SERVICES
            and name not in PROFILE_UTILITY_SERVICES
            and raw_service.get("restart") != "unless-stopped"
        ):
            errors.append(f"{name}: restart policy must be unless-stopped")
        cap_drop = raw_service.get("cap_drop") or []
        if "ALL" not in cap_drop:
            errors.append(f"{name}: Linux capabilities must be dropped")
        cap_add = set(raw_service.get("cap_add") or [])
        expected_cap_add = EXPECTED_CAPABILITIES_BY_SERVICE.get(name, frozenset())
        if cap_add != expected_cap_add:
            if expected_cap_add:
                errors.append(
                    f"{name}: cap_add must contain exactly "
                    + ", ".join(sorted(expected_cap_add))
                )
            else:
                errors.append(f"{name}: added Linux capabilities are forbidden")
        network_mode = raw_service.get("network_mode")
        if network_mode and not (
            name == "minio-volume-init" and network_mode == "none"
        ):
            errors.append(f"{name}: host network mode is forbidden")
        if raw_service.get("privileged") is True:
            errors.append(f"{name}: privileged mode is forbidden")
        if raw_service.get("extra_hosts"):
            errors.append(f"{name}: extra_hosts is forbidden")
        expected_networks = EXPECTED_NETWORKS_BY_SERVICE.get(name)
        actual_networks = set(raw_service.get("networks") or {})
        if expected_networks is not None and actual_networks != expected_networks:
            errors.append(
                f"{name}: networks must be exactly "
                + ", ".join(sorted(expected_networks))
            )
        security_options = raw_service.get("security_opt") or []
        if "no-new-privileges:true" not in security_options:
            errors.append(f"{name}: no-new-privileges is required")
        ports = raw_service.get("ports") or []
        if ports and name not in PUBLIC_SERVICES:
            errors.append(f"{name}: internal service must not publish host ports")
        if ports and name in LOOPBACK_OPERATOR_SERVICES:
            for port in ports:
                host_ip = port.get("host_ip") if isinstance(port, dict) else None
                if host_ip not in {"127.0.0.1", "::1"}:
                    errors.append(f"{name}: operator port must bind to loopback")

        environment = _environment_map(raw_service)
        for key, value in environment.items():
            if SECRET_NAME_PATTERN.search(key) and not key.endswith("_FILE"):
                errors.append(
                    f"{name}: secret-like setting {key} must use a file reference"
                )
            if value and (
                "changeme" in value.casefold() or "minioadmin" in value.casefold()
            ):
                errors.append(f"{name}: demo credential marker found in environment")

    qdrant_tool = services.get("qdrant-backup-tool")
    if isinstance(qdrant_tool, dict):
        qdrant_tool_secrets = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in (qdrant_tool.get("secrets") or [])
        }
        qdrant_tool_environment = _environment_map(qdrant_tool)
        if qdrant_tool.get("profiles") != ["backup-tools"]:
            errors.append(
                "qdrant-backup-tool: must be disabled by the backup-tools profile"
            )
        if qdrant_tool.get("restart") != "no":
            errors.append("qdrant-backup-tool: restart policy must be no")
        if qdrant_tool.get("read_only") is not True:
            errors.append("qdrant-backup-tool: root filesystem must be read-only")
        if qdrant_tool_secrets != {"qdrant_api_key"}:
            errors.append("qdrant-backup-tool: only the Qdrant API key may be mounted")
        if qdrant_tool_environment != {
            "AURIS_BACKUP_QDRANT_API_KEY_FILE": "/run/secrets/qdrant_api_key",
            "AURIS_BACKUP_QDRANT_URL": "http://qdrant:6333",
        }:
            errors.append(
                "qdrant-backup-tool: environment must contain only the fixed Qdrant endpoint and key file"
            )
        dependency = (qdrant_tool.get("depends_on") or {}).get("qdrant")
        if not isinstance(dependency, dict) or dependency.get("condition") != (
            "service_healthy"
        ):
            errors.append(
                "qdrant-backup-tool: Qdrant dependency must use service_healthy"
            )

    for name in APP_SERVICES:
        service = services.get(name)
        if not isinstance(service, dict):
            continue
        environment = _environment_map(service)
        for adapter in (
            "AURIS_OBJECT_STORAGE_ADAPTER",
            "AURIS_QDRANT_ADAPTER",
            "AURIS_DAGSTER_ADAPTER",
            "AURIS_EXTERNAL_CALLBACK_ADAPTER",
        ):
            if environment.get(adapter) != "real":
                errors.append(f"{name}: {adapter} must be real")
        if environment.get("AUTH_PROVIDER") != "oidc":
            errors.append(f"{name}: AUTH_PROVIDER must be oidc")
        if environment.get("AURIS_EMBEDDING_PROVIDER") != "http":
            errors.append(f"{name}: AURIS_EMBEDDING_PROVIDER must be http")
        if environment.get("OTEL_ENABLED", "").lower() != "true":
            errors.append(f"{name}: OTEL_ENABLED must be true")
        if not environment.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
            errors.append(f"{name}: OTEL_EXPORTER_OTLP_ENDPOINT is required")
        expected_observability_readiness = {
            "OTEL_COLLECTOR_HEALTH_URL": "http://otel-collector:13133/",
            "TEMPO_READINESS_URL": "http://tempo:3200/ready",
            "PROMETHEUS_READINESS_URL": "http://prometheus:9090/-/ready",
            "ALERTMANAGER_READINESS_URL": "http://alertmanager:9093/-/ready",
            "NODE_EXPORTER_METRICS_URL": "http://node-exporter:9100/metrics",
            "OBSERVABILITY_HEALTH_URL": "http://observability-health:8080/ready",
        }
        for variable, expected_url in expected_observability_readiness.items():
            if environment.get(variable) != expected_url:
                errors.append(
                    f"{name}: {variable} must probe the internal observability dependency"
                )
        if environment.get("METRICS_ENABLED", "").lower() != "true":
            errors.append(f"{name}: METRICS_ENABLED must be true")
        required_checks = set(
            environment.get("REQUIRED_DEPENDENCY_CHECKS", "").split(",")
        )
        expected_checks = {
            "auth",
            "database",
            "redis",
            "object_storage",
            "observability",
            "qdrant",
            "dagster",
        }
        if not expected_checks.issubset(required_checks):
            errors.append(
                f"{name}: all production dependencies must be strict readiness checks"
            )

    dagster_storage_bootstrap = services.get("dagster-storage-bootstrap")
    if isinstance(dagster_storage_bootstrap, dict):
        mounted_sources = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in (dagster_storage_bootstrap.get("secrets") or [])
        }
        bootstrap_environment = _environment_map(dagster_storage_bootstrap)
        bootstrap_dependencies = dagster_storage_bootstrap.get("depends_on") or {}
        database_dependency = (
            bootstrap_dependencies.get("db-bootstrap")
            if isinstance(bootstrap_dependencies, dict)
            else None
        )
        if dagster_storage_bootstrap.get("restart") != "no":
            errors.append("dagster-storage-bootstrap: restart policy must be no")
        if dagster_storage_bootstrap.get("read_only") is not True:
            errors.append(
                "dagster-storage-bootstrap: root filesystem must be read-only"
            )
        if dagster_storage_bootstrap.get("command") != ["storage-bootstrap"]:
            errors.append(
                "dagster-storage-bootstrap: dedicated entrypoint role is required"
            )
        if bootstrap_environment != {"DAGSTER_HOME": "/opt/dagster/home"}:
            errors.append(
                "dagster-storage-bootstrap: environment must contain only DAGSTER_HOME"
            )
        if mounted_sources != {"dagster_database_url"}:
            errors.append(
                "dagster-storage-bootstrap: only the Dagster database URL may be mounted"
            )
        if dagster_storage_bootstrap.get("volumes"):
            errors.append("dagster-storage-bootstrap: persistent volumes are forbidden")
        if dagster_storage_bootstrap.get("tmpfs") != ["/tmp:size=32m,mode=1777"]:
            errors.append(
                "dagster-storage-bootstrap: bounded temporary storage is required"
            )
        if (
            not isinstance(database_dependency, dict)
            or database_dependency.get("condition") != "service_completed_successfully"
            or set(bootstrap_dependencies) != {"db-bootstrap"}
        ):
            errors.append(
                "dagster-storage-bootstrap: database bootstrap dependency must complete successfully"
            )

    for name in ("dagster-code", "dagster-webserver", "dagster-daemon"):
        service = services.get(name)
        if not isinstance(service, dict):
            continue
        dependency = (service.get("depends_on") or {}).get("dagster-storage-bootstrap")
        if (
            not isinstance(dependency, dict)
            or dependency.get("condition") != "service_completed_successfully"
        ):
            errors.append(
                f"{name}: Dagster storage bootstrap dependency must complete successfully"
            )

    observability_health = services.get("observability-health")
    if isinstance(observability_health, dict):
        expected_dependencies = {
            "alertmanager",
            "node-exporter",
            "otel-collector",
            "prometheus",
            "tempo",
        }
        dependencies = observability_health.get("depends_on") or {}
        if set(dependencies) != expected_dependencies or any(
            not isinstance(dependency, dict)
            or dependency.get("condition") != "service_started"
            for dependency in dependencies.values()
        ):
            errors.append(
                "observability-health: all telemetry endpoints must be probed "
                "after service start"
            )
        health_test = (observability_health.get("healthcheck") or {}).get("test")
        if health_test != [
            "CMD",
            "python",
            "/app/scripts/check_observability_health.py",
            "--check-server",
        ]:
            errors.append(
                "observability-health: the bounded live endpoint probe is required"
            )
        if observability_health.get("command") != [
            "python",
            "/app/scripts/check_observability_health.py",
            "--serve",
        ]:
            errors.append(
                "observability-health: the background pipeline monitor is required"
            )

    for name in (*APP_SERVICES, "dagster-code"):
        service = services.get(name)
        if not isinstance(service, dict):
            continue
        dependency = (service.get("depends_on") or {}).get("observability-health")
        if (
            not isinstance(dependency, dict)
            or dependency.get("condition") != "service_healthy"
        ):
            errors.append(
                f"{name}: observability-health dependency must use service_healthy"
            )

    for name in COMPOSITE_HEALTH_COVERED_SERVICES:
        service = services.get(name)
        if isinstance(service, dict) and service.get("healthcheck"):
            errors.append(
                f"{name}: shallow local healthcheck must defer to observability-health"
            )

    dagster_code = services.get("dagster-code")
    if isinstance(dagster_code, dict):
        environment = _environment_map(dagster_code)
        provider = environment.get("AURIS_AUDIO_INFERENCE_PROVIDER", "")
        models = {
            value.strip()
            for value in environment.get(
                "AURIS_AUDIO_INFERENCE_ALLOWED_MODELS", ""
            ).split(",")
            if value.strip()
        }
        endpoint = urlsplit(environment.get("AURIS_AUDIO_INFERENCE_ENDPOINT", ""))
        if not provider:
            errors.append("dagster-code: audio inference provider is required")
        if not models or "*" in models:
            errors.append(
                "dagster-code: explicit audio inference model allowlist is required"
            )
        if endpoint.scheme != "https" or not endpoint.hostname:
            errors.append("dagster-code: audio inference endpoint must use HTTPS")
        if environment.get("AURIS_AUDIO_INFERENCE_API_TOKEN_FILE") != (
            "/run/secrets/audio_inference_api_token"
        ):
            errors.append(
                "dagster-code: audio inference credential must use its secret file"
            )
        if environment.get("AURIS_AUDIO_RESULT_BUCKET") not in set(
            environment.get("AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS", "").split(",")
        ):
            errors.append(
                "dagster-code: audio result bucket must be explicitly allowlisted"
            )
        mounted_sources = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in (dagster_code.get("secrets") or [])
        }
        if "audio_inference_api_token" not in mounted_sources:
            errors.append("dagster-code: audio inference credential secret is required")

    for name, raw_service in services.items():
        if name == "dagster-code" or not isinstance(raw_service, dict):
            continue
        mounted_sources = {
            str(item.get("source") or "") if isinstance(item, dict) else str(item)
            for item in (raw_service.get("secrets") or [])
        }
        if "audio_inference_api_token" in mounted_sources:
            errors.append(
                f"{name}: audio inference credential is restricted to dagster-code"
            )

    edge_service = services.get("edge")
    if isinstance(edge_service, dict):
        edge_dependencies = edge_service.get("depends_on") or {}
        bff_dependency = (
            edge_dependencies.get("bff")
            if isinstance(edge_dependencies, dict)
            else None
        )
        if (
            not isinstance(bff_dependency, dict)
            or bff_dependency.get("condition") != "service_started"
        ):
            errors.append("edge: BFF dependency must use service_started")
        keycloak_dependency = (
            edge_dependencies.get("keycloak")
            if isinstance(edge_dependencies, dict)
            else None
        )
        if (
            not isinstance(keycloak_dependency, dict)
            or keycloak_dependency.get("condition") != "service_healthy"
        ):
            errors.append("edge: Keycloak dependency must use service_healthy")
        https_targets = {
            str(port.get("target", ""))
            for port in (edge_service.get("ports") or [])
            if isinstance(port, dict) and str(port.get("published", "")) == "443"
        }
        if https_targets != {"443"}:
            errors.append("edge: internal HTTPS target must be 443")

    bff_service = services.get("bff")
    if isinstance(bff_service, dict) and isinstance(edge_service, dict):
        edge_networks = edge_service.get("networks") or {}
        edge_internal = (
            edge_networks.get("internal") if isinstance(edge_networks, dict) else None
        )
        edge_internal_ip = (
            str(edge_internal.get("ipv4_address") or "")
            if isinstance(edge_internal, dict)
            else ""
        )
        trusted_proxy = _command_flag_value(
            bff_service,
            "--forwarded-allow-ips",
        )
        bff_command = [str(item) for item in (bff_service.get("command") or [])]
        bff_environment = _environment_map(bff_service)
        if bff_command.count("--no-access-log") != 1 or "--access-log" in bff_command:
            errors.append("bff: Uvicorn access logging must be disabled")
        if any(
            argument in {"--workers", "-w"}
            or argument.startswith("--workers=")
            or re.fullmatch(r"-w(?:=)?\d+", argument) is not None
            for argument in bff_command
        ):
            errors.append("bff: Uvicorn CLI worker override is forbidden")
        if bff_environment.get("WEB_CONCURRENCY") != "1":
            errors.append("bff: WEB_CONCURRENCY must be 1 in prod/release")
        if bff_command.count("--proxy-headers") != 1:
            errors.append("bff: proxy headers must be explicitly enabled")
        if not edge_internal_ip:
            errors.append("edge: a static internal IPv4 address is required")
        if (
            bff_command.count("--forwarded-allow-ips") != 1
            or not trusted_proxy
            or trusted_proxy != edge_internal_ip
        ):
            errors.append(
                "bff: forwarded proxy trust must equal the static edge internal IP"
            )

    alertmanager_service = services.get("alertmanager")
    if isinstance(alertmanager_service, dict):
        command = [
            str(argument) for argument in (alertmanager_service.get("command") or [])
        ]
        if any(
            marker in argument.casefold()
            for argument in command
            for marker in ("http://", "https://", "webhook")
        ):
            errors.append("alertmanager: command must not contain notification URLs")
        if "--enable-feature=utf8-strict-mode" not in command:
            errors.append("alertmanager: UTF-8 strict mode must be enabled")

        mounted_secrets = alertmanager_service.get("secrets") or []
        has_webhook_secret = any(
            isinstance(secret, dict)
            and secret.get("source") == "alertmanager_webhook_url"
            and secret.get("target") == "alertmanager_webhook_url"
            for secret in mounted_secrets
        )
        declared_secrets = document.get("secrets") or {}
        if (
            not has_webhook_secret
            or not isinstance(declared_secrets, dict)
            or "alertmanager_webhook_url" not in declared_secrets
        ):
            errors.append(
                "alertmanager: webhook URL must use the dedicated Docker secret"
            )

    prometheus_service = services.get("prometheus")
    if isinstance(prometheus_service, dict):
        dependencies = prometheus_service.get("depends_on") or {}
        alertmanager_dependency = (
            dependencies.get("alertmanager") if isinstance(dependencies, dict) else None
        )
        if (
            not isinstance(alertmanager_dependency, dict)
            or alertmanager_dependency.get("condition") != "service_healthy"
        ):
            errors.append("prometheus: Alertmanager dependency must be healthy")

    networks = document.get("networks") or {}
    if not isinstance(networks, dict):
        errors.append("compose networks map is missing")
        networks = {}
    unexpected_networks = sorted(set(networks) - {"internal", "app-egress", "edge"})
    if unexpected_networks:
        errors.append("unexpected compose networks: " + ", ".join(unexpected_networks))
    internal = networks.get("internal") if isinstance(networks, dict) else None
    if not isinstance(internal, dict) or internal.get("internal") is not True:
        errors.append("internal network must set internal: true")
    internal_subnets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    if isinstance(internal, dict):
        ipam = internal.get("ipam") or {}
        configurations = (ipam.get("config") or []) if isinstance(ipam, dict) else []
        for configuration in configurations:
            subnet = (
                configuration.get("subnet") if isinstance(configuration, dict) else None
            )
            if not subnet:
                continue
            try:
                internal_subnets.append(ipaddress.ip_network(str(subnet), strict=True))
            except ValueError:
                errors.append("internal network must use a valid canonical subnet")
    if not internal_subnets:
        errors.append(
            "internal network must define an IPAM subnet for static edge trust"
        )
    if isinstance(edge_service, dict):
        edge_networks = edge_service.get("networks") or {}
        edge_internal = (
            edge_networks.get("internal") if isinstance(edge_networks, dict) else None
        )
        edge_internal_ip = (
            str(edge_internal.get("ipv4_address") or "")
            if isinstance(edge_internal, dict)
            else ""
        )
        if edge_internal_ip and internal_subnets:
            try:
                edge_address = ipaddress.ip_address(edge_internal_ip)
            except ValueError:
                errors.append("edge: static internal address must be a valid IP")
            else:
                if edge_address.version != 4:
                    errors.append("edge: static internal address must be IPv4")
                if not any(edge_address in subnet for subnet in internal_subnets):
                    errors.append(
                        "edge: static internal address must belong to internal subnet"
                    )
    app_egress = networks.get("app-egress")
    if (
        not isinstance(app_egress, dict)
        or app_egress.get("driver") != "bridge"
        or app_egress.get("internal", False) is not False
        or app_egress.get("external", False) is not False
        or app_egress.get("attachable", False) is not False
    ):
        errors.append("app-egress network must be a non-internal bridge")
    edge_network = networks.get("edge")
    if (
        not isinstance(edge_network, dict)
        or edge_network.get("internal", False) is not False
        or edge_network.get("external", False) is not False
        or edge_network.get("attachable", False) is not False
    ):
        errors.append("edge network must be managed and non-internal")
    volumes = document.get("volumes") or {}
    for required_volume in (
        "mysql_data",
        "redis_data",
        "minio_data",
        "qdrant_data",
        "keycloak_data",
        "dagster_home",
        "dagster_compute",
        "tempo_data",
        "alertmanager_data",
        "prometheus_data",
        "grafana_data",
    ):
        if required_volume not in volumes:
            errors.append(f"named volume is missing: {required_volume}")
    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate the production Compose security policy"
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="also require digest-pinned prebuilt images",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=COMPOSE_FILE,
        help="Compose document to validate (generated release documents are supported).",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ENV_FILE,
        help="Environment defaults used while Docker Compose renders the document.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        document = _render_compose(
            compose_file=args.compose_file,
            env_file=args.env_file,
        )
        errors = validate_compose(document, release=args.release)
        errors.extend(validate_edge_nginx(NGINX_CONFIG.read_text(encoding="utf-8")))
    except ComposePolicyError as exc:
        print(f"production compose policy failed: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"production edge policy failed: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    mode = "release" if args.release else "candidate"
    print(
        f"production compose policy ok ({mode}, {len(document['services'])} services)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
