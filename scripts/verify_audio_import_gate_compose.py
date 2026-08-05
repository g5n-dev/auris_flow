#!/usr/bin/env python3
"""Validate the rendered topology for the real audio-import Compose gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLATFORM_NETWORK = "audio-import-gate-platform"
PLATFORM_MEMBERS = frozenset(
    {
        "audio-import-platform",
        "audio-import-inference",
        "bff",
        "dagster-code",
    }
)
PLATFORM_SUBNET = "93.184.216.0/29"
PLATFORM_ADDRESS = "93.184.216.2"
PLATFORM_HOSTNAME = "recordings.audio-import-gate.test"
INFERENCE_HOSTNAME = "audio-inference.audio-import-gate.test"
INFERENCE_ENDPOINT = f"https://{INFERENCE_HOSTNAME}:8443/v1/audio-intelligence"
SHARED_STORAGE_ENVIRONMENT = {
    "AURIS_OBJECT_STORAGE_ADAPTER": "real",
    "OBJECT_STORAGE_PROVIDER": "minio",
    "OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
    "OBJECT_STORAGE_BUCKET": "auris-flow",
    "OBJECT_STORAGE_REGION": "us-east-1",
    "OBJECT_STORAGE_ALLOWED_BUCKETS": "auris-flow",
    "OBJECT_STORAGE_ACCESS_KEY_FILE": "/run/secrets/object_storage_access_key",
    "OBJECT_STORAGE_SECRET_KEY_FILE": "/run/secrets/object_storage_secret_key",
}


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


def _validate_shared_storage_scope(
    bff_environment: dict[str, Any],
    worker_environment: dict[str, Any],
) -> None:
    for label, environment in (
        ("BFF", bff_environment),
        ("outbox worker", worker_environment),
    ):
        mismatches = {
            name: {"expected": expected, "actual": environment.get(name)}
            for name, expected in SHARED_STORAGE_ENVIRONMENT.items()
            if environment.get(name) != expected
        }
        if mismatches:
            raise GateTopologyError(
                f"{label} real audio-import storage scope is incomplete: {mismatches}"
            )
    drift = {
        name: {
            "bff": bff_environment.get(name),
            "worker": worker_environment.get(name),
        }
        for name in SHARED_STORAGE_ENVIRONMENT
        if bff_environment.get(name) != worker_environment.get(name)
    }
    if drift:
        raise GateTopologyError(
            f"BFF and outbox worker audio-import storage scopes drift: {drift}"
        )


def validate_topology(document: dict[str, Any]) -> dict[str, Any]:
    services = _mapping(document.get("services"), "services")
    networks = _mapping(document.get("networks"), "networks")
    required_services = {
        "audio-import-gate-secrets-augment",
        "audio-import-gate-pki-init",
        "audio-import-gate-platform-connection-seed",
        "audio-import-platform",
        "audio-import-inference",
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

    inference = _mapping(
        services["audio-import-inference"],
        "audio inference fixture",
    )
    inference_network_binding = _mapping(
        _mapping(
            inference.get("networks"),
            "audio inference fixture networks",
        ).get(PLATFORM_NETWORK),
        "audio inference fixture network binding",
    )
    if (
        inference_network_binding.get("aliases") != [INFERENCE_HOSTNAME]
        or inference.get("ports")
        or inference.get("read_only") is not True
        or set(inference.get("cap_drop") or []) != {"ALL"}
    ):
        raise GateTopologyError("HTTPS audio inference fixture exposure is invalid")
    inference_environment = _mapping(
        inference.get("environment"),
        "audio inference fixture environment",
    )
    required_inference_environment = {
        "PYTHONPATH": "/app",
        "AURIS_GATE_CERT_FILE": "/run/audio-import-gate-tls/server.pem",
        "AURIS_GATE_KEY_FILE": "/run/audio-import-gate-tls/server-key.pem",
        "AURIS_GATE_CONTROL_SECRET_FILE": ("/run/secrets/audio_inference_gate_control"),
        "AUDIO_INFERENCE_API_TOKEN_FILE": ("/run/secrets/audio_inference_api_token"),
        "AUDIO_INFERENCE_PROVIDER": "audio_intelligence_default",
        "AUDIO_INFERENCE_MODEL": "audio-v2.3.1",
    }
    if any(
        inference_environment.get(name) != value
        for name, value in required_inference_environment.items()
    ):
        raise GateTopologyError("audio inference fixture contract is incomplete")
    inference_targets = _volume_targets(inference)
    if not {
        "/opt/auris-gate/production_gate_support.py",
        "/run/secrets",
        "/run/audio-import-gate-ca",
        "/run/audio-import-gate-tls",
    }.issubset(inference_targets):
        raise GateTopologyError("audio inference fixture lacks TLS or secret mounts")

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

    platform_connection_seed = _mapping(
        services["audio-import-gate-platform-connection-seed"],
        "platform connection seed",
    )
    seed_command = platform_connection_seed.get("command")
    if (
        platform_connection_seed.get("read_only") is not True
        or set(platform_connection_seed.get("cap_drop") or []) != {"ALL"}
        or platform_connection_seed.get("ports")
        or _service_networks(platform_connection_seed) != {"internal"}
        or not isinstance(seed_command, list)
        or "/opt/auris-gate/audio_import_gate_seed.py" not in seed_command
        or _volume_targets(platform_connection_seed)
        != {"/opt/auris-gate/audio_import_gate_seed.py", "/run/secrets"}
    ):
        raise GateTopologyError("platform connection gate seed boundary is invalid")

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
        **SHARED_STORAGE_ENVIRONMENT,
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
    bff_dependencies = _mapping(bff.get("depends_on"), "BFF dependencies")
    platform_seed_dependency = _mapping(
        bff_dependencies.get("audio-import-gate-platform-connection-seed"),
        "BFF platform connection seed dependency",
    )
    if platform_seed_dependency.get("condition") != "service_completed_successfully":
        raise GateTopologyError(
            "BFF must wait for the strong platform connection gate seed"
        )
    if "/run/audio-import-gate-tls" in _volume_targets(bff):
        raise GateTopologyError("BFF must never receive the platform TLS private key")

    worker = _mapping(services["worker"], "outbox worker")
    worker_environment = _mapping(
        worker.get("environment"),
        "outbox worker environment",
    )
    required_worker_environment = {
        "AURIS_DAGSTER_EXECUTION_MODE": "control-plane-acknowledgement",
        **SHARED_STORAGE_ENVIRONMENT,
    }
    if any(
        worker_environment.get(name) != value
        for name, value in required_worker_environment.items()
    ):
        raise GateTopologyError(
            "outbox worker must share the frozen audio-import execution and storage scope"
        )
    _validate_shared_storage_scope(bff_environment, worker_environment)

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
        "AURIS_AUDIO_INFERENCE_ENDPOINT": INFERENCE_ENDPOINT,
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
            "inference": inference,
            "bff": bff,
            "worker": worker,
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
        "inference_hostname": INFERENCE_HOSTNAME,
        "inference_endpoint": INFERENCE_ENDPOINT,
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
