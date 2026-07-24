#!/usr/bin/env python3
"""Build and atomically publish backup/restore release-gate evidence.

This producer is intentionally small and side-effect free until the final
publication step.  ``verify-backup.sh`` calls it only after the signed backup
has passed offline verification, the isolated restore has completed, and the
exact Compose project and its labelled volumes have been removed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping


SCHEMA_VERSION = "auris.backup-restore-gate.v1"
PRODUCER = "production/scripts/verify-backup.sh"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BACKUP_ID_RE = re.compile(r"^auris-flow-\d{8}T\d{6}Z-[0-9a-f]{12}$")
DRILL_PROJECT_RE = re.compile(r"^auris-flow-restore-drill-[0-9a-f]{12}$")
VIRTUALIZED_DOCKER_MARKERS = (
    "docker desktop",
    "rancher desktop",
    "colima",
    "orbstack",
)
TOOL_PATHS = (
    "production/backup/backup_restore_evidence.py",
    "production/backup/manifest.py",
    "production/backup/restore_network_allocator.py",
    "production/scripts/backup.sh",
    "production/scripts/restore.sh",
    "production/scripts/verify-backup.sh",
    "scripts/release_bundle.py",
    "scripts/run_with_deadline.py",
    "scripts/verify_backup_restore_gate.py",
)
MAX_JSON_BYTES = 1024 * 1024
MAX_PREPARED_INPUT_BYTES = 4 * 1024 * 1024


class EvidenceError(RuntimeError):
    """A fail-closed evidence construction or publication error."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EvidenceError(f"{label} must be a positive integer")
    return value


def _parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise EvidenceError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise EvidenceError(f"{label} must be an RFC3339 UTC timestamp") from exc
    if parsed.microsecond:
        raise EvidenceError(f"{label} must use whole-second precision")
    return parsed


def _interval(
    started_at: str,
    completed_at: str,
    *,
    label: str,
) -> int:
    started = _parse_timestamp(started_at, f"{label} started_at")
    completed = _parse_timestamp(completed_at, f"{label} completed_at")
    duration = int((completed - started).total_seconds())
    if duration < 0:
        raise EvidenceError(f"{label} time ordering is invalid")
    return duration


def _sha256_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise EvidenceError("a release-gate tool binding is missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_observation(
    *,
    platform_name: str,
    docker_context: object,
    docker_info: object,
) -> dict[str, object]:
    if platform_name != "linux":
        raise EvidenceError("formal evidence requires a native Linux host")
    contexts = docker_context
    if not isinstance(contexts, list) or len(contexts) != 1:
        raise EvidenceError("default Docker context observation is invalid")
    context = _mapping(contexts[0], "default Docker context")
    if context.get("Name") != "default":
        raise EvidenceError("formal evidence requires the default Docker context")
    endpoints = _mapping(context.get("Endpoints"), "Docker context endpoints")
    docker_endpoint = _mapping(endpoints.get("docker"), "Docker context endpoint")
    if docker_endpoint.get("Host") != "unix:///var/run/docker.sock":
        raise EvidenceError(
            "formal evidence requires the local rootful Docker socket"
        )

    info = _mapping(docker_info, "Docker daemon observation")
    if info.get("OSType") != "linux":
        raise EvidenceError("Docker daemon must run Linux containers")
    operating_system = info.get("OperatingSystem")
    if (
        not isinstance(operating_system, str)
        or not operating_system.strip()
        or len(operating_system) > 200
        or any(character in operating_system for character in "\r\n\0")
        or any(
            marker in operating_system.casefold()
            for marker in VIRTUALIZED_DOCKER_MARKERS
        )
    ):
        raise EvidenceError("Docker Desktop or a VM-backed runtime is forbidden")
    security_options = info.get("SecurityOptions")
    if not isinstance(security_options, list) or any(
        not isinstance(option, str) for option in security_options
    ):
        raise EvidenceError("Docker security options are not observable")
    if any("rootless" in option.casefold() for option in security_options):
        raise EvidenceError("rootless Docker is forbidden")
    return {
        "platform": "linux",
        "native_linux": True,
        "docker_context": "default",
        "docker_ostype": "linux",
        "docker_operating_system": operating_system,
        "rootless": False,
    }


def _authority_counts(document: Mapping[str, Any]) -> dict[str, object]:
    counts = _mapping(document.get("tenant_independent_counts"), "backup counts")
    mysql = _mapping(counts.get("mysql"), "MySQL backup counts")
    mysql_tables = _mapping(mysql.get("tables"), "MySQL table counts")
    rows_total = _positive_int(mysql.get("rows_total"), "MySQL rows_total")
    tables_total = _positive_int(len(mysql_tables), "MySQL tables_total")
    business_rows_total = _positive_int(
        mysql_tables.get("auris_flow.json_resources"),
        "MySQL json_resources business rows",
    )

    minio = _mapping(counts.get("minio"), "MinIO backup counts")
    minio_counts = {
        name: _positive_int(minio.get(name), f"MinIO {name}")
        for name in ("object_keys", "versions", "content_bytes")
    }

    qdrant = _mapping(counts.get("qdrant"), "Qdrant backup counts")
    if qdrant.get("included") is not True:
        raise EvidenceError("formal restore proof requires Qdrant snapshots")
    qdrant_collections = _mapping(
        qdrant.get("collections"), "Qdrant collection counts"
    )
    collections_total = _positive_int(
        len(qdrant_collections), "Qdrant collections_total"
    )
    points_total = _positive_int(qdrant.get("points_total"), "Qdrant points_total")
    return {
        "mysql": {
            "business_rows_total": business_rows_total,
            "tables_total": tables_total,
            "rows_total": rows_total,
        },
        "minio": minio_counts,
        "qdrant": {
            "collections": collections_total,
            "points_total": points_total,
        },
    }


def _signed_backup_identity(
    signed_manifest: Mapping[str, Any],
    verified_manifest: Mapping[str, Any],
) -> tuple[dict[str, object], dict[str, object], str]:
    backup_id = signed_manifest.get("backup_id")
    source = _mapping(signed_manifest.get("source"), "backup source")
    source_commit = source.get("git_commit")
    release_tag = source.get("release_version")
    release_metadata = _mapping(
        source.get("release_metadata"), "signed release metadata"
    )
    release_metadata_sha256 = source.get("release_metadata_sha256")
    manifest_sha256 = verified_manifest.get("manifest_sha256")
    if (
        not isinstance(backup_id, str)
        or BACKUP_ID_RE.fullmatch(backup_id) is None
        or not isinstance(source_commit, str)
        or COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(release_tag, str)
        or not isinstance(release_metadata_sha256, str)
        or SHA256_RE.fullmatch(release_metadata_sha256) is None
        or not isinstance(manifest_sha256, str)
        or SHA256_RE.fullmatch(manifest_sha256) is None
    ):
        raise EvidenceError("signed backup identity is invalid")
    expected_summary = {
        "backup_id": backup_id,
        "git_commit": source_commit,
        "release_version": release_tag,
        "release_metadata_sha256": release_metadata_sha256,
    }
    for name, expected in expected_summary.items():
        if verified_manifest.get(name) != expected:
            raise EvidenceError("verified backup summary does not match its manifest")
    if verified_manifest.get("status") != "verified":
        raise EvidenceError("backup manifest signature was not verified")
    if (
        release_metadata.get("release_tag") != release_tag
        or release_metadata.get("source_commit") != source_commit
    ):
        raise EvidenceError("backup and signed release identities do not match")
    compose = _mapping(release_metadata.get("compose"), "release Compose binding")
    image_lock = _mapping(
        release_metadata.get("image_lock"), "release image-lock binding"
    )
    compose_sha256 = compose.get("sha256")
    image_lock_sha256 = image_lock.get("sha256")
    if (
        not isinstance(compose_sha256, str)
        or SHA256_RE.fullmatch(compose_sha256) is None
        or not isinstance(image_lock_sha256, str)
        or SHA256_RE.fullmatch(image_lock_sha256) is None
    ):
        raise EvidenceError("signed release file bindings are invalid")
    release = {
        "release_tag": release_tag,
        "signed_release_metadata_verified": True,
        "release_metadata_sha256": release_metadata_sha256,
        "compose_sha256": compose_sha256,
        "image_lock_sha256": image_lock_sha256,
    }
    boundary = _mapping(
        signed_manifest.get("storage_boundary"), "backup storage boundary"
    )
    boundary_mode = boundary.get("mode")
    boundary_assertion = boundary.get("operator_assertion")
    supported_boundaries = {
        "encrypted-external": (
            "encrypted-at-rest-and-copied-off-host",
            True,
        ),
        "ephemeral-ci-drill": (
            "ephemeral-runner-recovery-drill-not-retained",
            False,
        ),
    }
    expected_boundary = supported_boundaries.get(str(boundary_mode))
    if (
        signed_manifest.get("schema_version")
        != "auris-flow.backup-manifest/v4"
        or expected_boundary is None
        or boundary_assertion != expected_boundary[0]
        or boundary.get("contains_sensitive_data") is not True
        or boundary.get("repository_never_contains_backup_payloads") is not True
    ):
        raise EvidenceError("backup storage boundary is invalid")
    backup = {
        "backup_id": backup_id,
        "manifest_sha256": manifest_sha256,
        "manifest_signature_verified": True,
        "storage_boundary": boundary_mode,
        "off_host_retained": expected_boundary[1],
        "source_project": "auris-flow",
        "authority_counts": _authority_counts(signed_manifest),
    }
    return release, backup, source_commit


def build_evidence(
    *,
    root: Path,
    platform_name: str,
    signed_manifest: object,
    verified_manifest: object,
    docker_context: object,
    docker_info: object,
    drill_project: str,
    restore_subnet: str,
    edge_internal_ip: str,
    backup_verification_started_at: str,
    backup_verification_completed_at: str,
    restore_started_at: str,
    restore_completed_at: str,
    cleanup_started_at: str,
    cleanup_completed_at: str,
    verified_at: str,
) -> dict[str, object]:
    """Build evidence only from authenticated backup and observed runtime facts."""

    if DRILL_PROJECT_RE.fullmatch(drill_project) is None:
        raise EvidenceError("restore drill project name is unsafe")
    try:
        network = ipaddress.ip_network(restore_subnet, strict=True)
        edge_address = ipaddress.ip_address(edge_internal_ip)
    except ValueError as exc:
        raise EvidenceError("restore drill network allocation is invalid") from exc
    if (
        not isinstance(network, ipaddress.IPv4Network)
        or network.prefixlen != 24
        or not network.is_private
        or not isinstance(edge_address, ipaddress.IPv4Address)
        or edge_address != network.network_address + 10
    ):
        raise EvidenceError("restore drill network allocation is invalid")
    manifest = _mapping(signed_manifest, "signed backup manifest")
    summary = _mapping(verified_manifest, "verified backup summary")
    release, backup, source_commit = _signed_backup_identity(manifest, summary)
    backup_duration = _interval(
        backup_verification_started_at,
        backup_verification_completed_at,
        label="backup verification",
    )
    restore_duration = _interval(
        restore_started_at,
        restore_completed_at,
        label="restore",
    )
    cleanup_duration = _interval(
        cleanup_started_at,
        cleanup_completed_at,
        label="cleanup",
    )
    backup_complete = _parse_timestamp(
        backup_verification_completed_at, "backup verification completed_at"
    )
    restore_start = _parse_timestamp(restore_started_at, "restore started_at")
    restore_complete = _parse_timestamp(restore_completed_at, "restore completed_at")
    cleanup_start = _parse_timestamp(cleanup_started_at, "cleanup started_at")
    cleanup_complete = _parse_timestamp(cleanup_completed_at, "cleanup completed_at")
    verification_complete = _parse_timestamp(verified_at, "verified_at")
    if (
        restore_start < backup_complete
        or cleanup_start < restore_complete
        or verification_complete < cleanup_complete
    ):
        raise EvidenceError("backup/restore/cleanup time ordering is invalid")
    repository_root = root.resolve()
    tool_bindings = {
        relative: _sha256_file(repository_root / relative)
        for relative in TOOL_PATHS
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "source_commit": source_commit,
        "execution_environment": "native-linux-compose",
        "producer": PRODUCER,
        "release": release,
        "host": _host_observation(
            platform_name=platform_name,
            docker_context=docker_context,
            docker_info=docker_info,
        ),
        "backup": {
            **backup,
            "verification_started_at": backup_verification_started_at,
            "verification_completed_at": backup_verification_completed_at,
            "verification_duration_seconds": backup_duration,
        },
        "restore": {
            "project_name": drill_project,
            "network_subnet": str(network),
            "edge_internal_ip": str(edge_address),
            "started_at": restore_started_at,
            "completed_at": restore_completed_at,
            "duration_seconds": restore_duration,
            "qdrant_mode": "snapshot",
            "empty_target_verified": {
                "mysql": True,
                "minio": True,
                "qdrant": True,
            },
            "consistency": {
                "mysql_counts_match": True,
                "minio_versions_and_sha256_match": True,
                "qdrant_fingerprints_match": True,
            },
        },
        "cleanup": {
            "started_at": cleanup_started_at,
            "completed_at": cleanup_completed_at,
            "duration_seconds": cleanup_duration,
            "containers_removed": True,
            "volumes_removed": True,
            "networks_removed": True,
        },
        "tool_bindings": tool_bindings,
        "verified_at": verified_at,
    }


def _load_validator(root: Path) -> ModuleType:
    validator = root / "scripts" / "verify_backup_restore_gate.py"
    if not validator.is_file() or validator.is_symlink():
        raise EvidenceError("backup/restore evidence validator is missing or unsafe")
    spec = importlib.util.spec_from_file_location(
        "verify_backup_restore_gate_for_emitter", validator
    )
    if spec is None or spec.loader is None:
        raise EvidenceError("backup/restore evidence validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish_validated_evidence(
    *,
    evidence: Mapping[str, Any],
    root: Path,
    output: Path,
) -> None:
    """Validate then atomically publish without replacing an existing artifact."""

    if not output.is_absolute():
        raise EvidenceError("--output must be an absolute path")
    parent = output.parent
    if not parent.is_dir() or parent.is_symlink():
        raise EvidenceError("evidence output parent must be a real directory")
    if output.exists() or output.is_symlink():
        raise EvidenceError("evidence output already exists")
    expected_commit = evidence.get("source_commit")
    if not isinstance(expected_commit, str):
        raise EvidenceError("evidence source commit is invalid")
    validator = _load_validator(root.resolve())
    errors = validator.validate_evidence(
        evidence,
        root=root.resolve(),
        expected_commit=expected_commit,
    )
    if errors:
        raise EvidenceError(
            "backup/restore evidence validation failed: " + "; ".join(errors)
        )
    payload = (
        json.dumps(evidence, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_path: Path | None = None
    file_descriptor: int | None = None
    published = False
    try:
        file_descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{output.name}.",
            dir=parent,
        )
        temporary_path = Path(raw_path)
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "wb", closefd=True) as handle:
            file_descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, output)
        except FileExistsError as exc:
            raise EvidenceError("evidence output already exists") from exc
        published = True
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except EvidenceError:
        if published:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
        raise
    except OSError as exc:
        if published:
            try:
                output.unlink()
            except FileNotFoundError:
                pass
            except OSError as rollback_exc:
                raise EvidenceError(
                    "evidence publication failed and rollback was not possible"
                ) from rollback_exc
        raise EvidenceError("evidence publication was not durable") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _load_json(path: Path, label: str) -> object:
    if not path.is_file() or path.is_symlink():
        raise EvidenceError(f"{label} is missing or unsafe")
    if path.stat().st_size > MAX_JSON_BYTES:
        raise EvidenceError(f"{label} exceeds the size limit")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is invalid JSON") from exc


def _prepared_input(
    *,
    backup_root: Path,
    verified_manifest_json: Path,
    docker_context_json: Path,
    docker_info_json: Path,
) -> dict[str, object]:
    return {
        "signed_manifest": _load_json(
            backup_root / "manifest.json", "signed backup manifest"
        ),
        "verified_manifest": _load_json(
            verified_manifest_json, "verified backup summary"
        ),
        "docker_context": _load_json(
            docker_context_json, "Docker context observation"
        ),
        "docker_info": _load_json(
            docker_info_json, "Docker daemon observation"
        ),
    }


def _load_prepared_stdin() -> Mapping[str, Any]:
    payload = sys.stdin.buffer.read(MAX_PREPARED_INPUT_BYTES + 1)
    if len(payload) > MAX_PREPARED_INPUT_BYTES:
        raise EvidenceError("prepared evidence input exceeds the size limit")
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("prepared evidence input is invalid JSON") from exc
    prepared = _mapping(document, "prepared evidence input")
    if set(prepared) != {
        "signed_manifest",
        "verified_manifest",
        "docker_context",
        "docker_info",
    }:
        raise EvidenceError("prepared evidence input shape is invalid")
    return prepared


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Emit native-Linux backup/restore release-gate evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-input")
    prepare.add_argument("--backup-root", type=Path, required=True)
    prepare.add_argument("--verified-manifest-json", type=Path, required=True)
    prepare.add_argument("--docker-context-json", type=Path, required=True)
    prepare.add_argument("--docker-info-json", type=Path, required=True)
    verify_host = subparsers.add_parser("verify-host")
    verify_host.add_argument("--docker-context-json", type=Path, required=True)
    verify_host.add_argument("--docker-info-json", type=Path, required=True)
    emit = subparsers.add_parser("emit-gate")
    emit.add_argument("--root", type=Path, required=True)
    emit.add_argument("--drill-project", required=True)
    emit.add_argument("--restore-subnet", required=True)
    emit.add_argument("--edge-internal-ip", required=True)
    emit.add_argument("--backup-verification-started-at", required=True)
    emit.add_argument("--backup-verification-completed-at", required=True)
    emit.add_argument("--restore-started-at", required=True)
    emit.add_argument("--restore-completed-at", required=True)
    emit.add_argument("--cleanup-started-at", required=True)
    emit.add_argument("--cleanup-completed-at", required=True)
    emit.add_argument("--verified-at", required=True)
    emit.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        if args.command == "prepare-input":
            prepared = _prepared_input(
                backup_root=args.backup_root.resolve(),
                verified_manifest_json=args.verified_manifest_json.resolve(),
                docker_context_json=args.docker_context_json.resolve(),
                docker_info_json=args.docker_info_json.resolve(),
            )
            print(
                json.dumps(
                    prepared,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "verify-host":
            host = _host_observation(
                platform_name=sys.platform,
                docker_context=_load_json(
                    args.docker_context_json.resolve(),
                    "Docker context observation",
                ),
                docker_info=_load_json(
                    args.docker_info_json.resolve(),
                    "Docker daemon observation",
                ),
            )
            print(json.dumps(host, ensure_ascii=True, sort_keys=True))
            return 0
        root = args.root.resolve()
        prepared_input = _load_prepared_stdin()
        evidence = build_evidence(
            root=root,
            platform_name=sys.platform,
            signed_manifest=prepared_input["signed_manifest"],
            verified_manifest=prepared_input["verified_manifest"],
            docker_context=prepared_input["docker_context"],
            docker_info=prepared_input["docker_info"],
            drill_project=args.drill_project,
            restore_subnet=args.restore_subnet,
            edge_internal_ip=args.edge_internal_ip,
            backup_verification_started_at=args.backup_verification_started_at,
            backup_verification_completed_at=args.backup_verification_completed_at,
            restore_started_at=args.restore_started_at,
            restore_completed_at=args.restore_completed_at,
            cleanup_started_at=args.cleanup_started_at,
            cleanup_completed_at=args.cleanup_completed_at,
            verified_at=args.verified_at,
        )
        publish_validated_evidence(
            evidence=evidence,
            root=root,
            output=args.output,
        )
    except EvidenceError as exc:
        print(f"backup/restore evidence failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "ok",
                "release_evidence": True,
                "source_commit": evidence["source_commit"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
