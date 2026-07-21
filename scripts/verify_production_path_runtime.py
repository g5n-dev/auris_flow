#!/usr/bin/env python3
"""Run and bind the single-host production Compose release diagnostic.

This host-side driver owns only orchestration.  The unprivileged verifier
container creates field-level captures from the real product path; this module
binds those captures to one clean commit, the exact Compose model, and the
checked-in runtime sources.  No pre-existing artifact is ever reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Sequence


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
TEMP_PARENT = BUILD_DIR / "tmp"
EVIDENCE_PATH = BUILD_DIR / "release-evidence" / "production-path-gate.json"
BASE_COMPOSE = ROOT / "production" / "compose.yaml"
GATE_COMPOSE = ROOT / "production" / "tests" / "production-path-gate.compose.yaml"
GATE_ENV = ROOT / "production" / "tests" / "production-path-gate.env"
RUNTIME_SOURCE_PATHS = (
    "scripts/verify_production_path_runtime.py",
    "production/tests/production_path_verifier.py",
    "production/tests/production_gate_support.py",
    "production/tests/production-path-keycloak-realm.template.json",
    "production/tests/production-path-gate.env",
)
RUNTIME_PAYLOAD_FIELDS = frozenset(
    {"identity", "adapters", "observability", "trace", "raw_proofs", "recovery"}
)
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REPO_DIGEST_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
RFC3339_UTC_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"
)
PROJECT_PATTERN = re.compile(
    r"^auris-production-path-gate-[1-9][0-9]{8,12}-[1-9][0-9]*-[0-9a-f]{8}$"
)
TEMP_NAME_PATTERN = re.compile(r"^auris-production-path-gate\.[A-Za-z0-9_-]{6,64}$")
_PERSONAL_MAC_PATH = "/" + r"Users/[^/\s]+"
_PERSONAL_LINUX_PATH = "/" + r"home/[^/\s]+"
_PERSONAL_WINDOWS_PATH = r"[A-Za-z]:\\" + r"Users\\[^\\\s]+"
PERSONAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'])(?:"
    + "|".join((_PERSONAL_MAC_PATH, _PERSONAL_LINUX_PATH, _PERSONAL_WINDOWS_PATH))
    + r")(?:/|\\)"
)
FORBIDDEN_EVIDENCE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "id_token",
        "key_material",
        "password",
        "raw_token",
        "refresh_token",
        "request_body",
        "response_body",
        "secret",
        "set_cookie",
        "token",
    }
)
SAFE_SECURITY_METADATA_KEYS = frozenset(
    {
        "authorization_endpoint_scheme",
        "token_endpoint_scheme",
        "cookie_name",
        "cookie_secure",
        "cookie_http_only",
        "claim_token_sha256_after",
        "claim_token_sha256_before",
        "session_token_sha256",
        "signature_key_id",
        "rsa_signing_key_ids",
    }
)
MAX_RUNTIME_PAYLOAD_BYTES = 4 * 1024 * 1024
HOST_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "XDG_CONFIG_HOME",
        "TMPDIR",
    }
)
PINNED_EXTERNAL_IMAGES = {
    "MYSQL_IMAGE": "mysql:8.4.5",
    "REDIS_IMAGE": "redis:7.4.2-alpine3.21",
    "MINIO_IMAGE": "minio/minio:RELEASE.2025-04-22T22-12-26Z",
    "MINIO_MC_IMAGE": "minio/mc:RELEASE.2025-04-16T18-13-26Z",
    "QDRANT_IMAGE": "qdrant/qdrant:v1.14.1",
    "KEYCLOAK_IMAGE": "quay.io/keycloak/keycloak:26.2.5",
    "OTEL_COLLECTOR_IMAGE": "otel/opentelemetry-collector-contrib:0.128.0",
    "TEMPO_IMAGE": "grafana/tempo:2.8.0",
    "PROMETHEUS_IMAGE": "prom/prometheus:v3.4.1",
    "GRAFANA_IMAGE": "grafana/grafana:12.0.1",
    "NODE_EXPORTER_IMAGE": "prom/node-exporter:v1.9.1",
}
REQUIRED_RUNNING_SERVICES = (
    "mysql",
    "redis",
    "minio",
    "qdrant",
    "keycloak",
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
    "production-gate-embedding",
    "production-gate-callback",
)
REQUIRED_COMPLETED_SERVICES = (
    "db-bootstrap",
    "minio-bootstrap",
    "migrate",
    "identity-bootstrap",
    "production-path-seed",
)
EXTERNAL_IMAGE_SERVICES = frozenset(
    {
        "mysql",
        "db-bootstrap",
        "redis",
        "minio",
        "minio-bootstrap",
        "qdrant",
        "keycloak",
        "otel-collector",
        "tempo",
        "prometheus",
        "grafana",
        "node-exporter",
    }
)
HOST_RUNTIME_FIELDS = frozenset(
    {
        "schema_version",
        "native_linux",
        "host_platform",
        "docker_endpoint_scheme",
        "docker_endpoint_path",
        "docker_ostype",
        "docker_operating_system",
        "architecture",
        "rootless",
        "cgroup_driver",
        "cgroup_version",
        "storage_driver",
    }
)


class RuntimeGateError(RuntimeError):
    """Sanitized, user-actionable production path failure."""


class FaultStep(NamedTuple):
    name: str
    service: str | None
    host_action: str
    run_during: bool = True


def clean_host_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Keep only host settings required to reach the local Docker daemon."""

    return {key: value for key, value in source.items() if key in HOST_ENV_ALLOWLIST}


def build_native_linux_host_observation(
    *,
    platform_name: str,
    docker_endpoint: str,
    docker_info: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a closed proof that the release gate ran on the supported host."""

    if platform_name != "linux":
        raise RuntimeGateError(
            "formal production evidence requires a native Linux release host"
        )
    if docker_endpoint != "unix:///var/run/docker.sock":
        raise RuntimeGateError(
            "formal production evidence requires the local rootful Docker socket"
        )
    if docker_info.get("OSType") != "linux":
        raise RuntimeGateError("Docker daemon is not running Linux containers")
    docker_operating_system = docker_info.get("OperatingSystem")
    if (
        not isinstance(docker_operating_system, str)
        or not docker_operating_system.strip()
        or len(docker_operating_system) > 200
        or any(character in docker_operating_system for character in "\r\n\0")
        or any(
            marker in docker_operating_system.casefold()
            for marker in ("docker desktop", "rancher desktop", "colima", "orbstack")
        )
    ):
        raise RuntimeGateError(
            "Docker Desktop or VM-backed desktop daemon is unsupported"
        )
    raw_architecture = docker_info.get("Architecture")
    if not isinstance(raw_architecture, str):
        raise RuntimeGateError("Docker host architecture is unsupported")
    architecture = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(raw_architecture)
    if architecture is None:
        raise RuntimeGateError("Docker host architecture is unsupported")
    security_options = docker_info.get("SecurityOptions")
    if not isinstance(security_options, list) or any(
        not isinstance(option, str) for option in security_options
    ):
        raise RuntimeGateError("Docker security options are not observable")
    if any("rootless" in option.casefold() for option in security_options):
        raise RuntimeGateError("rootless Docker is outside the production baseline")
    cgroup_driver = docker_info.get("CgroupDriver")
    cgroup_version = docker_info.get("CgroupVersion")
    storage_driver = docker_info.get("Driver")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (cgroup_driver, cgroup_version, storage_driver)
    ):
        raise RuntimeGateError("Docker host drivers are not fully observable")
    observation = {
        "schema_version": "auris.production-path.host-runtime.v1",
        "native_linux": True,
        "host_platform": "linux",
        "docker_endpoint_scheme": "unix",
        "docker_endpoint_path": "/var/run/docker.sock",
        "docker_ostype": "linux",
        "docker_operating_system": docker_operating_system,
        "architecture": architecture,
        "rootless": False,
        "cgroup_driver": cgroup_driver,
        "cgroup_version": cgroup_version,
        "storage_driver": storage_driver,
    }
    _scan_evidence_value(observation)
    return observation


def validate_host_runtime(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != HOST_RUNTIME_FIELDS:
        raise RuntimeGateError("native Linux host runtime evidence is invalid")
    if (
        value.get("schema_version") != "auris.production-path.host-runtime.v1"
        or value.get("native_linux") is not True
        or value.get("host_platform") != "linux"
        or value.get("docker_endpoint_scheme") != "unix"
        or value.get("docker_endpoint_path") != "/var/run/docker.sock"
        or value.get("docker_ostype") != "linux"
        or not isinstance(value.get("docker_operating_system"), str)
        or not value["docker_operating_system"].strip()
        or len(value["docker_operating_system"]) > 200
        or any(character in value["docker_operating_system"] for character in "\r\n\0")
        or any(
            marker in value["docker_operating_system"].casefold()
            for marker in ("docker desktop", "rancher desktop", "colima", "orbstack")
        )
        or value.get("architecture") not in {"amd64", "arm64"}
        or value.get("rootless") is not False
        or any(
            not isinstance(value.get(field), str) or not value[field].strip()
            for field in ("cgroup_driver", "cgroup_version", "storage_driver")
        )
    ):
        raise RuntimeGateError("native Linux host runtime evidence is invalid")
    _scan_evidence_value(value)
    return value


def _normalized_image_repository(reference: str) -> str:
    repository = reference.split("@", 1)[0]
    final_slash = repository.rfind("/")
    final_colon = repository.rfind(":")
    if final_colon > final_slash:
        repository = repository[:final_colon]
    for prefix in ("docker.io/library/", "index.docker.io/library/"):
        if repository.startswith(prefix):
            return repository.removeprefix(prefix)
    return repository.removeprefix("docker.io/")


def fault_plan() -> tuple[FaultStep, ...]:
    return (
        FaultStep("mysql_restart", "mysql", "restart", run_during=False),
        FaultStep("worker_crash", "worker", "kill-start"),
        FaultStep("duplicate_delivery", "worker", "stop-start"),
        FaultStep("callback_timeout", None, "verifier-only"),
        FaultStep("qdrant_outage", "qdrant", "stop-start"),
        FaultStep("redis_outage", "redis", "stop-start"),
    )


def build_host_observation(
    dependency: str,
    *,
    before: Mapping[str, str] | None = None,
    after: Mapping[str, str] | None = None,
) -> dict[str, str]:
    known = {item.name for item in fault_plan()}
    if dependency not in known:
        raise RuntimeGateError("host observation dependency is invalid")
    observation = {
        "schema_version": "auris.production-path.host-observation.v1",
        "dependency": dependency,
    }
    if dependency not in {"mysql_restart", "worker_crash"}:
        return observation
    before_values = dict(before or {})
    after_values = dict(after or {})
    before_id = before_values.get("container_id", "")
    after_id = after_values.get("container_id", "")
    started_before = before_values.get("started_at", "")
    started_after = after_values.get("started_at", "")
    if (
        not before_id
        or not after_id
        or not RFC3339_UTC_PATTERN.fullmatch(started_before)
        or not RFC3339_UTC_PATTERN.fullmatch(started_after)
        or started_before == started_after
    ):
        raise RuntimeGateError("host restart observation is incomplete")
    return {
        **observation,
        "container_id_sha256": _sha256_bytes(after_id.encode("utf-8")),
        "started_at_before": started_before,
        "started_at_after": started_after,
    }


def build_runtime_service_observation(
    service: str,
    container: Mapping[str, Any],
    image_document: Mapping[str, Any],
    *,
    expected_state: str,
    require_repo_digest: bool,
) -> dict[str, Any]:
    container_id = container.get("Id")
    image_id = container.get("Image")
    configured_image = (
        container.get("Config", {}).get("Image")
        if isinstance(container.get("Config"), Mapping)
        else None
    )
    state = container.get("State")
    if (
        not isinstance(container_id, str)
        or not container_id
        or not isinstance(image_id, str)
        or not IMAGE_ID_PATTERN.fullmatch(image_id)
        or image_document.get("Id") != image_id
        or not isinstance(configured_image, str)
        or not configured_image
        or not isinstance(state, Mapping)
    ):
        raise RuntimeGateError(f"runtime observation is invalid for {service}")
    operating_system = image_document.get("Os")
    architecture = image_document.get("Architecture")
    if operating_system != "linux" or architecture not in {"amd64", "arm64"}:
        raise RuntimeGateError(f"runtime image platform is unsupported for {service}")
    raw_repo_digests = image_document.get("RepoDigests") or []
    if not isinstance(raw_repo_digests, list) or any(
        not isinstance(item, str) or not REPO_DIGEST_PATTERN.fullmatch(item)
        for item in raw_repo_digests
    ):
        raise RuntimeGateError(f"runtime repository digests are invalid for {service}")
    repo_digests = sorted(set(raw_repo_digests))
    if require_repo_digest and not repo_digests:
        raise RuntimeGateError(
            f"external runtime image has no registry digest: {service}"
        )
    configured_repository = _normalized_image_repository(configured_image)
    if require_repo_digest and not any(
        _normalized_image_repository(item) == configured_repository
        for item in repo_digests
    ):
        raise RuntimeGateError(
            f"external runtime digest does not match configured repository: {service}"
        )

    status = state.get("Status")
    observation: dict[str, Any] = {
        "container_id_sha256": _sha256_bytes(container_id.encode("utf-8")),
        "configured_image": configured_image,
        "image_id": image_id,
        "repo_digests": repo_digests,
        "os": operating_system,
        "architecture": architecture,
        "state": status,
    }
    if expected_state == "running":
        health = state.get("Health")
        if status != "running" or not isinstance(health, Mapping):
            raise RuntimeGateError(
                f"runtime service is not health-observable: {service}"
            )
        health_status = health.get("Status")
        if health_status != "healthy":
            raise RuntimeGateError(f"runtime service is not healthy: {service}")
        observation["health"] = health_status
    elif expected_state == "completed":
        exit_code = state.get("ExitCode")
        if status != "exited" or exit_code != 0:
            raise RuntimeGateError(
                f"one-shot runtime service did not complete: {service}"
            )
        observation["exit_code"] = exit_code
    else:
        raise RuntimeGateError("runtime observation expected state is invalid")
    return observation


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _regular_file(path: Path, *, label: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise RuntimeGateError(f"{label} must be a regular non-symlink file")


def _real_directory(path: Path, *, label: str) -> None:
    if not path.is_dir() or path.is_symlink():
        raise RuntimeGateError(f"{label} must be a real directory")


def _scan_evidence_value(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise RuntimeGateError("runtime evidence keys must be strings")
            normalized = raw_key.casefold().replace("-", "_")
            parts = set(normalized.split("_"))
            sensitive_term = bool(
                parts
                & {
                    "authorization",
                    "body",
                    "cookie",
                    "credential",
                    "credentials",
                    "header",
                    "headers",
                    "password",
                    "secret",
                    "token",
                }
            ) or any(
                marker in normalized
                for marker in (
                    "api_key",
                    "access_key",
                    "encryption_key",
                    "key_material",
                    "private_key",
                    "signing_key",
                )
            )
            if normalized in FORBIDDEN_EVIDENCE_KEYS or (
                sensitive_term and normalized not in SAFE_SECURITY_METADATA_KEYS
            ):
                raise RuntimeGateError(
                    "runtime evidence contains a forbidden sensitive field: "
                    + ".".join((*path, raw_key))
                )
            _scan_evidence_value(item, path=(*path, raw_key))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_evidence_value(item, path=(*path, str(index)))
        return
    if isinstance(value, str) and PERSONAL_PATH_PATTERN.search(value):
        raise RuntimeGateError("runtime evidence contains a personal absolute path")


def validate_runtime_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != RUNTIME_PAYLOAD_FIELDS:
        raise RuntimeGateError(
            "runtime verifier payload must contain exactly the six contract sections"
        )
    for field in sorted(RUNTIME_PAYLOAD_FIELDS):
        if not isinstance(payload.get(field), dict):
            raise RuntimeGateError(f"runtime verifier section is invalid: {field}")
    _scan_evidence_value(payload)
    encoded = _canonical_bytes(payload)
    if len(encoded) > MAX_RUNTIME_PAYLOAD_BYTES:
        raise RuntimeGateError("runtime verifier payload exceeds 4 MiB")
    return payload


def validate_runtime_inventory(
    inventory: object,
    *,
    defined_services: set[str],
    host_architecture: str,
) -> dict[str, dict[str, dict[str, Any]]]:
    if host_architecture not in {"amd64", "arm64"}:
        raise RuntimeGateError("native Linux host architecture is invalid")
    if not isinstance(inventory, dict) or set(inventory) != {
        "running_services",
        "completed_services",
    }:
        raise RuntimeGateError("runtime service inventory has an invalid envelope")
    running = inventory.get("running_services")
    completed = inventory.get("completed_services")
    if not isinstance(running, dict) or not isinstance(completed, dict):
        raise RuntimeGateError("runtime service inventory sections are invalid")
    if set(running) != set(REQUIRED_RUNNING_SERVICES):
        raise RuntimeGateError(
            "runtime service inventory is missing required running services"
        )
    if set(completed) != set(REQUIRED_COMPLETED_SERVICES):
        raise RuntimeGateError(
            "runtime service inventory is missing required one-shot services"
        )
    names = set(running) | set(completed)
    if (
        set(running) & set(completed)
        or any(not isinstance(name, str) or not name for name in names)
        or not names.issubset(defined_services)
    ):
        raise RuntimeGateError("runtime service inventory does not match Compose")
    common_fields = {
        "container_id_sha256",
        "configured_image",
        "image_id",
        "repo_digests",
        "os",
        "architecture",
        "state",
    }
    for section_name, section, state_field in (
        ("running", running, "health"),
        ("completed", completed, "exit_code"),
    ):
        for service, raw_observation in section.items():
            if not isinstance(raw_observation, dict) or set(raw_observation) != (
                common_fields | {state_field}
            ):
                raise RuntimeGateError(
                    f"{section_name} runtime observation is invalid: {service}"
                )
            container_hash = raw_observation.get("container_id_sha256")
            image_id = raw_observation.get("image_id")
            configured_image = raw_observation.get("configured_image")
            repo_digests = raw_observation.get("repo_digests")
            if (
                not isinstance(container_hash, str)
                or not SHA256_PATTERN.fullmatch(container_hash)
                or not isinstance(image_id, str)
                or not IMAGE_ID_PATTERN.fullmatch(image_id)
                or not isinstance(configured_image, str)
                or not configured_image
                or not isinstance(repo_digests, list)
                or repo_digests != sorted(set(repo_digests))
                or any(
                    not isinstance(item, str) or not REPO_DIGEST_PATTERN.fullmatch(item)
                    for item in repo_digests
                )
                or raw_observation.get("os") != "linux"
                or raw_observation.get("architecture") not in {"amd64", "arm64"}
            ):
                raise RuntimeGateError(
                    f"{section_name} runtime image binding is invalid: {service}"
                )
            if raw_observation.get("architecture") != host_architecture:
                raise RuntimeGateError(
                    "runtime container architecture must match the native Linux host"
                )
            if service in EXTERNAL_IMAGE_SERVICES:
                configured_repository = _normalized_image_repository(configured_image)
                if not repo_digests or not any(
                    _normalized_image_repository(item) == configured_repository
                    for item in repo_digests
                ):
                    raise RuntimeGateError(
                        f"external runtime image digest is unbound: {service}"
                    )
            if section_name == "running" and (
                raw_observation.get("state") != "running"
                or raw_observation.get("health") != "healthy"
            ):
                raise RuntimeGateError(f"runtime service is not healthy: {service}")
            if section_name == "completed" and (
                raw_observation.get("state") != "exited"
                or raw_observation.get("exit_code") != 0
            ):
                raise RuntimeGateError(f"one-shot runtime service failed: {service}")
    _scan_evidence_value(inventory)
    return inventory


def write_json_once(path: Path, payload: object) -> None:
    parent = path.parent
    _real_directory(parent, label="artifact parent")
    if path.exists() or path.is_symlink():
        raise RuntimeGateError("artifact target already exists")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    temporary = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() or path.is_symlink():
            raise RuntimeGateError("artifact target appeared during atomic write")
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise RuntimeGateError(
                "artifact target appeared during atomic write"
            ) from None
        except OSError as exc:
            raise RuntimeGateError("artifact atomic publish failed") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def build_evidence(
    *,
    root: Path,
    source_commit: str,
    base_compose: Path,
    gate_compose: Path,
    rendered_config: bytes,
    services: Sequence[str],
    host_runtime: object,
    runtime_inventory: object,
    runtime_payload: object,
) -> dict[str, Any]:
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise RuntimeGateError("source commit must be an exact lowercase Git SHA")
    expected_base = root / "production" / "compose.yaml"
    expected_gate = root / "production" / "tests" / "production-path-gate.compose.yaml"
    if base_compose.resolve() != expected_base.resolve():
        raise RuntimeGateError(
            "base Compose path is not the checked-in production model"
        )
    if gate_compose.resolve() != expected_gate.resolve():
        raise RuntimeGateError("gate Compose path is not the checked-in overlay")
    _regular_file(base_compose, label="base Compose")
    _regular_file(gate_compose, label="gate Compose")
    try:
        rendered = json.loads(rendered_config)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeGateError("rendered Compose model is invalid JSON") from None
    if not isinstance(rendered, dict) or not isinstance(rendered.get("services"), dict):
        raise RuntimeGateError("rendered Compose model has no service map")
    service_set = set(services)
    if (
        not service_set
        or any(not isinstance(item, str) or not item for item in services)
        or service_set != set(rendered["services"])
    ):
        raise RuntimeGateError(
            "runtime service inventory does not match rendered Compose"
        )

    verified_runtime = validate_runtime_payload(runtime_payload)
    verified_host_runtime = validate_host_runtime(host_runtime)
    verified_inventory = validate_runtime_inventory(
        runtime_inventory,
        defined_services=service_set,
        host_architecture=verified_host_runtime["architecture"],
    )
    runtime_sources: dict[str, str] = {}
    for relative in RUNTIME_SOURCE_PATHS:
        source = root / relative
        _regular_file(source, label=f"runtime source {relative}")
        runtime_sources[relative] = _sha256_file(source)

    return {
        "schema_version": "auris.production-path-gate.v1",
        "status": "ok",
        "source_commit": source_commit,
        "execution_environment": "production-compose",
        "producer": "scripts/verify_production_path_runtime.py",
        "compose": {
            "base": "production/compose.yaml",
            "overlay": "production/tests/production-path-gate.compose.yaml",
            "base_sha256": _sha256_file(base_compose),
            "overlay_sha256": _sha256_file(gate_compose),
            "rendered_config_sha256": _sha256_bytes(rendered_config),
            "services": sorted(service_set),
            "host_runtime": verified_host_runtime,
            "runtime": verified_inventory,
        },
        "runtime_sources": runtime_sources,
        **verified_runtime,
    }


def valid_project_name(value: str) -> bool:
    return bool(PROJECT_PATTERN.fullmatch(value))


def safe_temp_root(path: Path, *, parent: Path) -> bool:
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return bool(
        path.parent.resolve() == resolved_parent
        and resolved_path.parent == resolved_parent
        and TEMP_NAME_PATTERN.fullmatch(path.name)
        and path.is_dir()
        and not path.is_symlink()
    )


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: int,
    capture: bool = False,
    label: str,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(env),
            check=True,
            timeout=timeout,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeGateError(f"timed out while running {label}") from exc
    except subprocess.CalledProcessError as exc:
        if capture and exc.stderr:
            sys.stderr.buffer.write(exc.stderr[-16_384:])
        raise RuntimeGateError(f"failed while running {label}") from exc


def collect_native_linux_host_observation(
    env: Mapping[str, str],
) -> dict[str, Any]:
    if (
        env.get("DOCKER_HOST")
        or env.get("DOCKER_TLS_VERIFY")
        or env.get("DOCKER_CERT_PATH")
    ):
        raise RuntimeGateError(
            "formal production evidence refuses a remote or TLS-overridden Docker daemon"
        )
    context_name = env.get("DOCKER_CONTEXT", "default")
    if context_name != "default":
        raise RuntimeGateError(
            "formal production evidence requires the default local Docker context"
        )
    context_result = _run(
        ("docker", "context", "inspect", "default"),
        cwd=ROOT,
        env=env,
        timeout=30,
        capture=True,
        label="inspect the local Docker context",
    )
    info_result = _run(
        ("docker", "info", "--format", "{{json .}}"),
        cwd=ROOT,
        env=env,
        timeout=30,
        capture=True,
        label="inspect the Docker daemon",
    )
    try:
        context_payload = json.loads(context_result.stdout)
        endpoint = context_payload[0]["Endpoints"]["docker"]["Host"]
        docker_info = json.loads(info_result.stdout)
    except (IndexError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeGateError("Docker host observation is invalid") from None
    if not isinstance(endpoint, str) or not isinstance(docker_info, dict):
        raise RuntimeGateError("Docker host observation is invalid")
    return build_native_linux_host_observation(
        platform_name=sys.platform,
        docker_endpoint=endpoint,
        docker_info=docker_info,
    )


class ComposeRuntime:
    def __init__(
        self,
        *,
        root: Path,
        base_compose: Path,
        gate_compose: Path,
        project_name: str,
        env: Mapping[str, str],
        command_timeout: int,
        wait_timeout: int,
    ) -> None:
        self.root = root
        self.env = dict(env)
        self.command_timeout = command_timeout
        self.wait_timeout = wait_timeout
        self.command = (
            "docker",
            "compose",
            "--env-file",
            str(root / "production" / "tests" / "production-path-gate.env"),
            "--parallel",
            "2",
            "--project-name",
            project_name,
            "--project-directory",
            str(root / "production"),
            "--file",
            str(base_compose),
            "--file",
            str(gate_compose),
        )

    def run(
        self,
        *arguments: str,
        capture: bool = False,
        timeout: int | None = None,
        label: str,
    ) -> subprocess.CompletedProcess[bytes]:
        return _run(
            (*self.command, *arguments),
            cwd=self.root,
            env=self.env,
            timeout=timeout or self.command_timeout,
            capture=capture,
            label=label,
        )

    def render(self) -> bytes:
        result = self.run(
            "config",
            "--format",
            "json",
            capture=True,
            label="render production Compose",
        )
        return result.stdout

    def start(self) -> None:
        self.run(
            "up",
            "--detach",
            "--build",
            "--wait",
            "--wait-timeout",
            str(self.wait_timeout),
            "production-gate-embedding",
            "production-gate-callback",
            "edge",
            "worker",
            "dagster-daemon",
            "grafana",
            timeout=max(self.command_timeout, self.wait_timeout + 900),
            label="start isolated production Compose",
        )

    def _runtime_documents(
        self, service: str, *, include_stopped: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        arguments = ["ps", "--quiet"]
        if include_stopped:
            arguments.append("--all")
        arguments.append(service)
        container_result = self.run(
            *arguments,
            capture=True,
            label=f"resolve {service} runtime container",
        )
        container_id = container_result.stdout.decode("ascii", errors="strict").strip()
        if not container_id or "\n" in container_id:
            raise RuntimeGateError(f"could not resolve one {service} runtime container")
        inspected = _run(
            ("docker", "inspect", container_id),
            cwd=self.root,
            env=self.env,
            timeout=30,
            capture=True,
            label=f"inspect {service} runtime container",
        )
        try:
            container_payload = json.loads(inspected.stdout)
            container_document = container_payload[0]
            image_id = container_document["Image"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeGateError(f"{service} runtime container is invalid") from None
        image_result = _run(
            ("docker", "image", "inspect", str(image_id)),
            cwd=self.root,
            env=self.env,
            timeout=30,
            capture=True,
            label=f"inspect {service} runtime image",
        )
        try:
            image_payload = json.loads(image_result.stdout)
            image_document = image_payload[0]
        except (IndexError, TypeError, json.JSONDecodeError):
            raise RuntimeGateError(f"{service} runtime image is invalid") from None
        if not isinstance(container_document, dict) or not isinstance(
            image_document, dict
        ):
            raise RuntimeGateError(f"{service} runtime inspection is invalid")
        return container_document, image_document

    def runtime_inventory(self) -> dict[str, dict[str, dict[str, Any]]]:
        running: dict[str, dict[str, Any]] = {}
        completed: dict[str, dict[str, Any]] = {}
        for service in REQUIRED_RUNNING_SERVICES:
            container, image_document = self._runtime_documents(
                service, include_stopped=False
            )
            running[service] = build_runtime_service_observation(
                service,
                container,
                image_document,
                expected_state="running",
                require_repo_digest=service in EXTERNAL_IMAGE_SERVICES,
            )
        for service in REQUIRED_COMPLETED_SERVICES:
            container, image_document = self._runtime_documents(
                service, include_stopped=True
            )
            completed[service] = build_runtime_service_observation(
                service,
                container,
                image_document,
                expected_state="completed",
                require_repo_digest=service in EXTERNAL_IMAGE_SERVICES,
            )
        return {"running_services": running, "completed_services": completed}

    def wait_service(self, service: str) -> None:
        self.run(
            "up",
            "--detach",
            "--no-build",
            "--no-deps",
            "--wait",
            "--wait-timeout",
            str(self.wait_timeout),
            service,
            timeout=self.wait_timeout + 60,
            label=f"wait for {service}",
        )

    def service_observation(self, service: str) -> dict[str, str]:
        container_result = self.run(
            "ps",
            "--quiet",
            service,
            capture=True,
            label=f"resolve {service} container",
        )
        container_id = container_result.stdout.decode("ascii", errors="strict").strip()
        if not container_id or "\n" in container_id:
            raise RuntimeGateError(f"could not resolve one {service} container")
        inspected = _run(
            ("docker", "inspect", container_id),
            cwd=self.root,
            env=self.env,
            timeout=30,
            capture=True,
            label=f"inspect {service} container",
        )
        try:
            payload = json.loads(inspected.stdout)
            item = payload[0]
            exact_id = item["Id"]
            started_at = item["State"]["StartedAt"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            raise RuntimeGateError(
                f"{service} container observation is invalid"
            ) from None
        if not isinstance(exact_id, str) or not isinstance(started_at, str):
            raise RuntimeGateError(f"{service} container observation is invalid")
        return {"container_id": exact_id, "started_at": started_at}

    def verifier_phase(self, phase: str, dependency: str, suffix: str) -> None:
        if phase not in {
            "initial",
            "fault-prepare",
            "fault-during",
            "fault-verify",
            "finalize",
        }:
            raise RuntimeGateError("unknown verifier phase")
        self.run(
            "run",
            "--rm",
            "--no-deps",
            "production-path-verifier",
            "python",
            "/opt/auris-gate/production_path_verifier.py",
            "--phase",
            phase,
            "--dependency",
            dependency,
            "--artifact-dir",
            "/artifacts",
            "--run-suffix",
            suffix,
            timeout=max(self.command_timeout, 600),
            label=f"production verifier {phase}/{dependency}",
        )

    def down(self) -> None:
        self.run(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "30",
            timeout=180,
            label="tear down isolated production Compose",
        )


def _generate_tls(directory: Path, *, env: Mapping[str, str]) -> None:
    directory.mkdir(mode=0o700)
    ca_key = directory / "ca-key.pem"
    ca_cert = directory / "ca.pem"
    leaf_key = directory / "privkey.pem"
    leaf_csr = directory / "leaf.csr"
    leaf_cert = directory / "leaf.pem"
    extension = directory / "leaf.ext"
    extension.write_text(
        "\n".join(
            (
                "basicConstraints=critical,CA:FALSE",
                "keyUsage=critical,digitalSignature,keyEncipherment",
                "extendedKeyUsage=serverAuth",
                "subjectAltName=DNS:auris-production-gate.invalid,"
                "DNS:embedding.production-gate.invalid,"
                "DNS:callback.production-gate.invalid",
                "",
            )
        ),
        encoding="ascii",
    )
    _run(
        (
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=Auris Flow Production Gate CA",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
        ),
        cwd=directory,
        env=env,
        timeout=60,
        label="generate production gate CA",
    )
    _run(
        (
            "openssl",
            "req",
            "-new",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-subj",
            "/CN=auris-production-gate.invalid",
            "-keyout",
            str(leaf_key),
            "-out",
            str(leaf_csr),
        ),
        cwd=directory,
        env=env,
        timeout=60,
        label="generate production gate leaf key",
    )
    _run(
        (
            "openssl",
            "x509",
            "-req",
            "-sha256",
            "-days",
            "2",
            "-in",
            str(leaf_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-extfile",
            str(extension),
            "-out",
            str(leaf_cert),
        ),
        cwd=directory,
        env=env,
        timeout=60,
        label="sign production gate TLS certificate",
    )
    (directory / "fullchain.pem").write_bytes(
        leaf_cert.read_bytes() + ca_cert.read_bytes()
    )
    ca_key.chmod(0o600)
    # These two files are bind-mounted individually into short-lived,
    # unprivileged support containers.  The enclosing temporary directory is
    # host-private; world-readable file mode is required for a remapped UID on
    # native Linux and never exposes the CA signing key.
    leaf_key.chmod(0o444)
    for public in (ca_cert, leaf_cert, directory / "fullchain.pem"):
        public.chmod(0o444)


def write_control_secret(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeGateError("production gate control secret already exists")
    path.write_text(secrets.token_urlsafe(48) + "\n", encoding="ascii")
    path.chmod(0o444)


def _initialize_runtime_environment(
    temp_root: Path, *, source_commit: str
) -> dict[str, str]:
    secrets_dir = temp_root / "secrets"
    metrics_dir = temp_root / "runtime-metrics"
    artifacts_dir = temp_root / "artifacts"
    tls_dir = temp_root / "tls"
    for directory, mode in (
        (secrets_dir, 0o700),
        (metrics_dir, 0o755),
        (artifacts_dir, 0o777),
    ):
        directory.mkdir(mode=mode)
    env = clean_host_environment(os.environ)
    env.update(PINNED_EXTERNAL_IMAGES)
    env.update(
        {
            "APP_ENV": "prod",
            "AURIS_PUBLIC_HOST": "auris-production-gate.invalid",
            "AURIS_EXTERNAL_CALLBACK_URL": (
                "https://callback.production-gate.invalid:8443/callbacks/platform"
            ),
            "AURIS_EXTERNAL_CALLBACK_HOST": "callback.production-gate.invalid",
            "AURIS_EMBEDDING_ENDPOINT": (
                "https://embedding.production-gate.invalid:8443/v1/embeddings"
            ),
            "AURIS_EMBEDDING_MODEL": "auris-production-gate-reference-semantic-v1",
            "AURIS_EMBEDDING_DIMENSION": "8",
            "AURIS_OTEL_TRACE_SAMPLE_RATIO": "1",
            "AURIS_SECRETS_DIR": str(secrets_dir),
            "AURIS_TLS_DIR": str(tls_dir),
            "AURIS_RUNTIME_METRICS_DIR": str(metrics_dir),
            "AURIS_PRODUCTION_GATE_TLS_DIR": str(tls_dir),
            "AURIS_PRODUCTION_GATE_ARTIFACT_DIR": str(artifacts_dir),
            "AURIS_PRODUCTION_GATE_SOURCE_COMMIT": source_commit,
            "AURIS_PRODUCTION_GATE_DEPENDENCY": "none",
            "AURIS_PRODUCTION_GATE_PHASE": "initial",
            "AURIS_HTTP_PORT": f"127.0.0.1:{_free_loopback_port()}",
            "AURIS_HTTPS_PORT": f"127.0.0.1:{_free_loopback_port()}",
            "AURIS_KEYCLOAK_ADMIN_PORT": f"127.0.0.1:{_free_loopback_port()}",
            "AURIS_GRAFANA_PORT": f"127.0.0.1:{_free_loopback_port()}",
        }
    )
    _generate_tls(tls_dir, env=env)
    control_secret = temp_root / "control-secret"
    write_control_secret(control_secret)
    env["AURIS_PRODUCTION_GATE_CONTROL_SECRET"] = str(control_secret)
    _run(
        ("bash", str(ROOT / "production" / "scripts" / "init-secrets.sh")),
        cwd=ROOT,
        env=env,
        timeout=120,
        label="initialize one-time production secrets",
    )
    return env


def _load_runtime_payload(path: Path) -> dict[str, Any]:
    _regular_file(path, label="runtime verifier payload")
    if path.stat().st_size > MAX_RUNTIME_PAYLOAD_BYTES:
        raise RuntimeGateError("runtime verifier payload exceeds 4 MiB")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeGateError("runtime verifier payload is invalid JSON") from None
    return validate_runtime_payload(payload)


def _current_head(root: Path) -> str:
    result = _run(
        ("git", "rev-parse", "--verify", "HEAD^{commit}"),
        cwd=root,
        env=os.environ,
        timeout=30,
        capture=True,
        label="resolve release source commit",
    )
    return result.stdout.decode("ascii", errors="strict").strip()


def _require_clean_source(root: Path, expected_commit: str) -> None:
    if _current_head(root) != expected_commit:
        raise RuntimeGateError("source commit changed before the production diagnostic")
    result = _run(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        cwd=root,
        env=os.environ,
        timeout=30,
        capture=True,
        label="check release source tree",
    )
    if result.stdout:
        raise RuntimeGateError(
            "production diagnostic requires a clean committed source tree"
        )


def run_gate(
    *,
    base_compose: Path,
    gate_compose: Path,
    source_commit: str,
    artifact: Path,
    command_timeout: int,
    wait_timeout: int,
) -> None:
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise RuntimeGateError("source commit must be an exact lowercase Git SHA")
    if base_compose.resolve() != BASE_COMPOSE.resolve():
        raise RuntimeGateError(
            "production path driver requires production/compose.yaml"
        )
    if gate_compose.resolve() != GATE_COMPOSE.resolve():
        raise RuntimeGateError(
            "production path driver requires the checked-in gate overlay"
        )
    if artifact.resolve() != EVIDENCE_PATH.resolve():
        raise RuntimeGateError(
            "production path artifact must use the canonical evidence path"
        )
    if artifact.exists() or artifact.is_symlink():
        raise RuntimeGateError("production path artifact already exists")
    _require_clean_source(ROOT, source_commit)
    host_runtime = collect_native_linux_host_observation(
        clean_host_environment(os.environ)
    )
    _real_directory(BUILD_DIR, label="build directory")
    if not TEMP_PARENT.exists():
        TEMP_PARENT.mkdir(mode=0o700)
    _real_directory(TEMP_PARENT, label="production gate temporary parent")

    temp_root = Path(
        tempfile.mkdtemp(prefix="auris-production-path-gate.", dir=TEMP_PARENT)
    )
    project_name = (
        f"auris-production-path-gate-{int(temp_root.stat().st_ctime)}-"
        f"{os.getpid()}-{secrets.token_hex(4)}"
    )
    if not valid_project_name(project_name):
        raise RuntimeGateError(
            "generated Compose project name is outside the cleanup policy"
        )

    runtime: ComposeRuntime | None = None
    down_complete = False
    try:
        env = _initialize_runtime_environment(temp_root, source_commit=source_commit)
        suffix = f"{os.getpid()}-{secrets.token_hex(6)}"
        env.update(
            {
                "AURIS_PRODUCTION_GATE_RUN_SUFFIX": suffix,
                "AURIS_BFF_IMAGE": f"auris-flow-production-gate-bff:{source_commit[:12]}",
                "AURIS_DAGSTER_IMAGE": (
                    f"auris-flow-production-gate-dagster:{source_commit[:12]}"
                ),
                "AURIS_EDGE_IMAGE": f"auris-flow-production-gate-edge:{source_commit[:12]}",
            }
        )
        runtime = ComposeRuntime(
            root=ROOT,
            base_compose=base_compose,
            gate_compose=gate_compose,
            project_name=project_name,
            env=env,
            command_timeout=command_timeout,
            wait_timeout=wait_timeout,
        )
        rendered_config = runtime.render()
        try:
            rendered_document = json.loads(rendered_config)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeGateError("rendered Compose output is invalid") from None
        rendered_services = sorted(
            str(item) for item in dict(rendered_document.get("services") or {})
        )
        runtime.start()
        initial_runtime_inventory = runtime.runtime_inventory()
        runtime.verifier_phase("initial", "none", suffix)

        for step in fault_plan():
            before_observation = (
                runtime.service_observation(step.service)
                if step.name in {"mysql_restart", "worker_crash"}
                and step.service is not None
                else None
            )
            runtime.verifier_phase("fault-prepare", step.name, suffix)
            if step.host_action == "restart":
                assert step.service is not None
                runtime.run(
                    "restart",
                    step.service,
                    label=f"restart {step.service}",
                )
                runtime.wait_service(step.service)
            elif step.host_action == "kill-start":
                assert step.service is not None
                runtime.run(
                    "kill",
                    "--signal",
                    "SIGKILL",
                    step.service,
                    label=f"kill {step.service}",
                )
                if step.run_during:
                    runtime.verifier_phase("fault-during", step.name, suffix)
                runtime.wait_service(step.service)
            elif step.host_action == "stop-start":
                assert step.service is not None
                runtime.run("stop", step.service, label=f"stop {step.service}")
                if step.run_during:
                    runtime.verifier_phase("fault-during", step.name, suffix)
                runtime.wait_service(step.service)
            elif step.host_action == "verifier-only":
                if step.run_during:
                    runtime.verifier_phase("fault-during", step.name, suffix)
            else:  # pragma: no cover - fixed, reviewable fault plan.
                raise RuntimeGateError("unsupported production fault action")
            after_observation = (
                runtime.service_observation(step.service)
                if step.name in {"mysql_restart", "worker_crash"}
                and step.service is not None
                else None
            )
            write_json_once(
                temp_root / "artifacts" / f"host-{suffix}-{step.name}.json",
                build_host_observation(
                    step.name,
                    before=before_observation,
                    after=after_observation,
                ),
            )
            runtime.verifier_phase("fault-verify", step.name, suffix)

        runtime.verifier_phase("finalize", "none", suffix)
        final_runtime_inventory = runtime.runtime_inventory()
        if final_runtime_inventory != initial_runtime_inventory:
            raise RuntimeGateError(
                "runtime service or image inventory changed during the diagnostic"
            )
        runtime_payload = _load_runtime_payload(
            temp_root / "artifacts" / f"runtime-{suffix}.json"
        )
        evidence = build_evidence(
            root=ROOT,
            source_commit=source_commit,
            base_compose=base_compose,
            gate_compose=gate_compose,
            rendered_config=rendered_config,
            services=rendered_services,
            host_runtime=host_runtime,
            runtime_inventory=final_runtime_inventory,
            runtime_payload=runtime_payload,
        )
        _require_clean_source(ROOT, source_commit)
        runtime.down()
        down_complete = True
        write_json_once(artifact, evidence)
    finally:
        if runtime is not None and not down_complete:
            try:
                runtime.down()
            except RuntimeGateError:
                pass
        if safe_temp_root(temp_root, parent=TEMP_PARENT):
            shutil.rmtree(temp_root)
        elif temp_root.exists():
            raise RuntimeGateError(
                "refusing to clean an unexpected production gate path"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the isolated Auris Flow production Compose release diagnostic."
    )
    parser.add_argument("--base-compose", type=Path, required=True)
    parser.add_argument("--gate-compose", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=int(os.getenv("AURIS_PRODUCTION_GATE_COMMAND_TIMEOUT", "900")),
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=int(os.getenv("AURIS_PRODUCTION_GATE_WAIT_TIMEOUT", "600")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 30 <= args.command_timeout <= 3600 or not 30 <= args.wait_timeout <= 1800:
        print(
            "production path timeouts are outside the allowed bounds", file=sys.stderr
        )
        return 2
    try:
        run_gate(
            base_compose=args.base_compose.resolve(),
            gate_compose=args.gate_compose.resolve(),
            source_commit=args.source_commit,
            artifact=args.artifact.resolve(),
            command_timeout=args.command_timeout,
            wait_timeout=args.wait_timeout,
        )
    except RuntimeGateError as exc:
        print(f"production path gate failed: {exc}", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - release evidence must fail closed and stay sanitized.
        print("production path gate failed: internal driver failure", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
