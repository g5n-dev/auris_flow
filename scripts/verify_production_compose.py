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
        "minio",
        "minio-bootstrap",
        "qdrant",
        "migrate",
        "keycloak",
        "identity-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
        "bff",
        "worker",
        "otel-collector",
        "tempo",
        "prometheus",
        "grafana",
        "node-exporter",
        "edge",
    }
)
ONE_SHOT_SERVICES = frozenset(
    {"db-bootstrap", "minio-bootstrap", "migrate", "identity-bootstrap"}
)
PUBLIC_SERVICES = frozenset({"edge", "keycloak", "grafana"})
LOOPBACK_OPERATOR_SERVICES = frozenset({"keycloak", "grafana"})
APP_SERVICES = frozenset({"bff", "worker"})
EXPECTED_NETWORKS_BY_SERVICE = {
    **{name: frozenset({"internal"}) for name in REQUIRED_SERVICES},
    "bff": frozenset({"internal", "app-egress"}),
    "worker": frozenset({"internal", "app-egress"}),
    "edge": frozenset({"internal", "edge"}),
}
EXPECTED_CAPABILITIES_BY_SERVICE = {
    "edge": frozenset({"NET_BIND_SERVICE"}),
    # The official MySQL entrypoint initializes/chowns the named volume as
    # root, then switches to the image's mysql uid/gid.
    "mysql": frozenset({"CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"}),
}
SECRET_NAME_PATTERN = re.compile(
    r"(?:PASSWORD|SECRET|TOKEN|API_KEY|DATABASE_URL|REDIS_URL)$"
)
SHA256_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")


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


def validate_edge_nginx(source: str) -> list[str]:
    errors: list[str] = []
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

    for name, raw_service in sorted(services.items()):
        if not isinstance(raw_service, dict):
            errors.append(f"{name}: service definition must be an object")
            continue
        errors.extend(_validate_image(name, raw_service, release=release))
        if name not in ONE_SHOT_SERVICES and not raw_service.get("healthcheck"):
            errors.append(f"{name}: long-running service requires a healthcheck")
        if (
            name not in ONE_SHOT_SERVICES
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
        if raw_service.get("network_mode"):
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
            "qdrant",
            "dagster",
        }
        if not expected_checks.issubset(required_checks):
            errors.append(
                f"{name}: all production dependencies must be strict readiness checks"
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
