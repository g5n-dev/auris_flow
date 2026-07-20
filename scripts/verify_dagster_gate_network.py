#!/usr/bin/env python3
"""Fail closed when the test-only Dagster host bridge exceeds its narrow scope."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

MAX_COMPOSE_MODEL_BYTES = 8 * 1024 * 1024
HOST_NETWORK = "dagster-gate-host"
INTERNAL_NETWORK = "internal"
EXPECTED_MEMBERS = {"dagster-gate-callback", "dagster-webserver"}
PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")


class GateFailure(RuntimeError):
    """A rendered Compose model violates the isolated gate topology."""


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GateFailure(f"{label} must be a mapping")
    return value


def _service_networks(service: dict[str, Any], *, service_name: str) -> set[str]:
    networks = service.get("networks")
    if networks is None:
        return set()
    if isinstance(networks, dict) and all(isinstance(key, str) for key in networks):
        return set(networks)
    if isinstance(networks, list) and all(isinstance(item, str) for item in networks):
        return set(networks)
    raise GateFailure(f"{service_name} network set is invalid")


def _require_loopback_port(
    service: dict[str, Any],
    *,
    service_name: str,
    published: int,
    target: int,
) -> str:
    expected = {
        "mode": "ingress",
        "host_ip": "127.0.0.1",
        "target": target,
        "published": str(published),
        "protocol": "tcp",
    }
    ports = service.get("ports")
    if ports != [expected]:
        raise GateFailure(f"{service_name} must publish exactly one loopback-only port")
    return f"127.0.0.1:{published}:{target}/tcp"


def validate_compose_model(
    model: object,
    *,
    project_name: str,
    webserver_port: int,
    callback_port: int,
) -> dict[str, object]:
    """Validate the normalized JSON emitted by ``docker compose config``."""

    if not PROJECT_NAME_RE.fullmatch(project_name):
        raise GateFailure("Compose project name is invalid")
    for label, port in (
        ("webserver", webserver_port),
        ("callback", callback_port),
    ):
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise GateFailure(f"{label} port is invalid")

    document = _mapping(model, label="Compose model")
    if document.get("name") != project_name:
        raise GateFailure("Compose model is not bound to the requested project")

    networks = _mapping(document.get("networks"), label="Compose networks")
    host_network = _mapping(networks.get(HOST_NETWORK), label="Dagster host network")
    if host_network.get("driver") != "bridge":
        raise GateFailure("Dagster host network must use the bridge driver")
    if host_network.get("internal", False) is not False:
        raise GateFailure("Dagster host network must be non-internal")
    if host_network.get("external", False) is not False:
        raise GateFailure("Dagster host network must not be external")
    if host_network.get("attachable", False) is not False:
        raise GateFailure("Dagster host network must not be attachable")
    expected_network_name = f"{project_name}_{HOST_NETWORK}"
    if host_network.get("name") != expected_network_name:
        raise GateFailure("Dagster host network name must remain project-scoped")

    services = _mapping(document.get("services"), label="Compose services")
    service_networks: dict[str, set[str]] = {}
    members: set[str] = set()
    for service_name, raw_service in services.items():
        service = _mapping(raw_service, label=f"{service_name} service")
        attached = _service_networks(service, service_name=service_name)
        service_networks[service_name] = attached
        if HOST_NETWORK in attached:
            members.add(service_name)
    if members != EXPECTED_MEMBERS:
        raise GateFailure(
            "Dagster host network members must be exactly callback and webserver"
        )

    expected_networks = {INTERNAL_NETWORK, HOST_NETWORK}
    for service_name in sorted(EXPECTED_MEMBERS):
        if service_networks[service_name] != expected_networks:
            raise GateFailure(
                f"{service_name} network set must retain only internal and host access"
            )

    callback = _mapping(services["dagster-gate-callback"], label="callback service")
    webserver = _mapping(services["dagster-webserver"], label="webserver service")
    ports = {
        "dagster-gate-callback": _require_loopback_port(
            callback,
            service_name="dagster-gate-callback",
            published=callback_port,
            target=8080,
        ),
        "dagster-webserver": _require_loopback_port(
            webserver,
            service_name="dagster-webserver",
            published=webserver_port,
            target=3000,
        ),
    }
    return {
        "network_name": expected_network_name,
        "members": sorted(members),
        "ports": ports,
    }


def _positive_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def _load_model(path: Path) -> object:
    try:
        size = path.stat().st_size
        if size > MAX_COMPOSE_MODEL_BYTES:
            raise GateFailure("rendered Compose model exceeds the gate limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except GateFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GateFailure("rendered Compose model is unavailable or invalid") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-model", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--webserver-port", type=_positive_port, required=True)
    parser.add_argument("--callback-port", type=_positive_port, required=True)
    args = parser.parse_args()
    try:
        proof = validate_compose_model(
            _load_model(args.compose_model),
            project_name=args.project_name,
            webserver_port=args.webserver_port,
            callback_port=args.callback_port,
        )
    except GateFailure as exc:
        print(f"Dagster gate network policy failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(proof, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
