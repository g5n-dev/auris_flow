#!/usr/bin/env python3
"""Validate the rendered topology for the real audio-import Compose gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLATFORM_NETWORK = "audio-import-gate-platform"
PLATFORM_MEMBERS = frozenset({"audio-import-platform", "bff", "dagster-code"})
PLATFORM_SUBNET = "93.184.216.0/29"
PLATFORM_ADDRESS = "93.184.216.2"
PLATFORM_HOSTNAME = "recordings.audio-import-gate.test"


class GateTopologyError(RuntimeError):
    pass


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateTopologyError(f"{label} must be a mapping")
    return value


def _service_networks(service: dict[str, Any]) -> set[str]:
    raw = service.get("networks")
    if isinstance(raw, list):
        return {str(value) for value in raw}
    if isinstance(raw, dict):
        return {str(value) for value in raw}
    return set()


def _volume_targets(service: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    raw_volumes = service.get("volumes")
    if not isinstance(raw_volumes, list):
        return targets
    for raw in raw_volumes:
        if isinstance(raw, str):
            parts = raw.split(":")
            if len(parts) >= 2:
                targets.add(parts[1])
        elif isinstance(raw, dict):
            target = raw.get("target")
            if isinstance(target, str):
                targets.add(target)
    return targets


def validate_topology(document: dict[str, Any]) -> dict[str, Any]:
    services = _mapping(document.get("services"), "services")
    networks = _mapping(document.get("networks"), "networks")
    required_services = {
        "audio-import-gate-secrets-augment",
        "audio-import-gate-pki-init",
        "audio-import-platform",
        "minio",
        "minio-bootstrap",
        "dagster-code",
        "dagster-webserver",
        "dagster-daemon",
        "bff",
        "worker",
        "audio-import-gate-verifier",
    }
    missing = sorted(required_services - set(services))
    if missing:
        raise GateTopologyError(f"required services are missing: {missing}")

    platform_network = _mapping(
        networks.get(PLATFORM_NETWORK),
        "audio import platform network",
    )
    if (
        platform_network.get("internal") is not True
        or platform_network.get("attachable", False) is not False
        or platform_network.get("driver") != "bridge"
    ):
        raise GateTopologyError("audio import platform network is not isolated")
    ipam = _mapping(platform_network.get("ipam"), "platform network IPAM")
    configs = ipam.get("config")
    if (
        not isinstance(configs, list)
        or len(configs) != 1
        or not isinstance(configs[0], dict)
        or configs[0].get("subnet") != PLATFORM_SUBNET
    ):
        raise GateTopologyError("audio import platform subnet is not exact")

    observed_members = {
        name
        for name, raw_service in services.items()
        if isinstance(raw_service, dict)
        and PLATFORM_NETWORK in _service_networks(raw_service)
    }
    if observed_members != PLATFORM_MEMBERS:
        raise GateTopologyError(
            "audio import platform network members must be exactly "
            "fixture, BFF and Dagster code"
        )

    platform = _mapping(services["audio-import-platform"], "platform fixture")
    platform_network_binding = _mapping(
        _mapping(platform.get("networks"), "platform fixture networks").get(
            PLATFORM_NETWORK
        ),
        "platform fixture network binding",
    )
    aliases = platform_network_binding.get("aliases")
    if (
        platform_network_binding.get("ipv4_address") != PLATFORM_ADDRESS
        or aliases != [PLATFORM_HOSTNAME]
        or platform.get("ports")
    ):
        raise GateTopologyError("HTTPS platform fixture exposure is invalid")
    platform_command = platform.get("command")
    if (
        not isinstance(platform_command, list)
        or "serve" not in platform_command
        or "/run/audio-import-gate-tls" not in platform_command
        or platform.get("read_only") is not True
    ):
        raise GateTopologyError("HTTPS platform fixture command is invalid")
    if _volume_targets(platform) != {
        "/opt/auris-gate/audio_import_platform.py",
        "/run/audio-import-gate-tls",
    }:
        raise GateTopologyError("platform fixture mounts exceed its TLS-only boundary")

    pki = _mapping(services["audio-import-gate-pki-init"], "PKI initializer")
    if (
        pki.get("network_mode") != "none"
        or pki.get("read_only") is not True
        or set(pki.get("cap_drop") or []) != {"ALL"}
    ):
        raise GateTopologyError("audio import gate PKI initializer is not isolated")

    secret_augment = _mapping(
        services["audio-import-gate-secrets-augment"],
        "secret augment service",
    )
    if (
        secret_augment.get("network_mode") != "none"
        or secret_augment.get("read_only") is not True
        or set(secret_augment.get("cap_drop") or []) != {"ALL"}
    ):
        raise GateTopologyError("audio import gate secret initializer is not isolated")

    minio = _mapping(services["minio"], "MinIO")
    if minio.get("ports") or "/run/secrets" not in _volume_targets(minio):
        raise GateTopologyError(
            "real MinIO must remain internal and secret-file configured"
        )
    minio_bootstrap = _mapping(services["minio-bootstrap"], "MinIO bootstrap")
    if "/run/secrets" not in _volume_targets(minio_bootstrap):
        raise GateTopologyError("MinIO bootstrap lacks the ephemeral credential volume")

    bff = _mapping(services["bff"], "BFF")
    bff_environment = _mapping(bff.get("environment"), "BFF environment")
    required_bff_environment = {
        "AURIS_DAGSTER_EXECUTION_MODE": "control-plane-acknowledgement",
        "AURIS_OBJECT_STORAGE_ADAPTER": "real",
        "OBJECT_STORAGE_PROVIDER": "minio",
        "OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
        "OBJECT_STORAGE_BUCKET": "auris-flow",
        "OBJECT_STORAGE_ACCESS_KEY_FILE": "/run/secrets/object_storage_access_key",
        "OBJECT_STORAGE_SECRET_KEY_FILE": "/run/secrets/object_storage_secret_key",
        "PLATFORM_CREDENTIAL_BINDINGS_FILE": (
            "/run/secrets/platform_credential_bindings"
        ),
        "AUDIO_PLAYBACK_GRANT_SECRET_FILE": (
            "/run/secrets/audio_playback_grant_secret"
        ),
        "SSL_CERT_FILE": "/run/audio-import-gate-ca/ca.pem",
    }
    if any(
        bff_environment.get(name) != value
        for name, value in required_bff_environment.items()
    ):
        raise GateTopologyError("BFF real audio-import environment is incomplete")
    if _service_networks(bff) != {"internal", "app-egress", PLATFORM_NETWORK}:
        raise GateTopologyError("BFF network scope is invalid for the gate")
    if "/run/audio-import-gate-tls" in _volume_targets(bff):
        raise GateTopologyError("BFF must never receive the platform TLS private key")

    worker = _mapping(services["worker"], "outbox worker")
    worker_environment = _mapping(
        worker.get("environment"),
        "outbox worker environment",
    )
    if (
        worker_environment.get("AURIS_DAGSTER_EXECUTION_MODE")
        != "control-plane-acknowledgement"
    ):
        raise GateTopologyError(
            "outbox worker must submit the production audio-import job"
        )

    dagster = _mapping(services["dagster-code"], "Dagster code")
    dagster_environment = _mapping(
        dagster.get("environment"),
        "Dagster code environment",
    )
    required_dagster_environment = {
        "AURIS_BFF_INTERNAL_URL": "http://bff:8000",
        "AURIS_PLATFORM_CREDENTIAL_BINDINGS_FILE": (
            "/run/secrets/platform_credential_bindings"
        ),
        "AURIS_PLATFORM_AUDIO_ALLOWED_HOSTS": PLATFORM_HOSTNAME,
        "AURIS_AUDIO_OBJECT_STORAGE_PROVIDER": "minio",
        "AURIS_AUDIO_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
        "AURIS_AUDIO_OBJECT_STORAGE_ALLOWED_BUCKETS": "auris-flow",
        "SSL_CERT_FILE": "/run/audio-import-gate-ca/ca.pem",
    }
    if any(
        dagster_environment.get(name) != value
        for name, value in required_dagster_environment.items()
    ):
        raise GateTopologyError("Dagster real audio-import environment is incomplete")
    if _service_networks(dagster) != {"internal", PLATFORM_NETWORK}:
        raise GateTopologyError("Dagster code network scope is invalid for the gate")
    if "/run/audio-import-gate-tls" in _volume_targets(dagster):
        raise GateTopologyError(
            "Dagster must never receive the platform TLS private key"
        )

    verifier = _mapping(
        services["audio-import-gate-verifier"],
        "audio import verifier",
    )
    verifier_command = verifier.get("command")
    if (
        _service_networks(verifier) != {"internal"}
        or verifier.get("read_only") is not True
        or not isinstance(verifier_command, list)
        or "/opt/auris-gate/verify_audio_import_stack.py" not in verifier_command
    ):
        raise GateTopologyError("audio import verifier boundary is invalid")

    rendered = json.dumps(
        {
            "platform": platform,
            "bff": bff,
            "dagster": dagster,
            "verifier": verifier,
        },
        ensure_ascii=True,
        sort_keys=True,
    ).casefold()
    forbidden_tokens = (
        "fake_dagster_graphql_server",
        "fake_platform_callback_server",
        "auris_object_storage_adapter=local",
        '"auris_object_storage_adapter":"local"',
    )
    if any(token in rendered for token in forbidden_tokens):
        raise GateTopologyError(
            "real audio-import gate contains a fake/local success path"
        )

    return {
        "platform_network": PLATFORM_NETWORK,
        "platform_members": sorted(observed_members),
        "platform_address": PLATFORM_ADDRESS,
        "platform_hostname": PLATFORM_HOSTNAME,
        "object_storage_adapter": bff_environment["AURIS_OBJECT_STORAGE_ADAPTER"],
        "object_storage_provider": bff_environment["OBJECT_STORAGE_PROVIDER"],
        "dagster_callback": dagster_environment["AURIS_BFF_INTERNAL_URL"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compose-model", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        document = json.loads(args.compose_model.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise GateTopologyError("rendered Compose model must be an object")
        evidence = validate_topology(document)
    except (OSError, ValueError, GateTopologyError) as exc:
        print(f"audio import gate topology failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
